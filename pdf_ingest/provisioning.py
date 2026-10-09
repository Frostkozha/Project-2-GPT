"""Explicit, checksum-verified model provisioning; runtime never downloads.

The private ledger binds Hugging Face commit IDs to all required local files.
Large-file checksums and Git blob IDs come from the Hub's HTTPS metadata; the
download is checked against that metadata before it becomes a usable model.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .schema import IngestError

DOCLING_VERSION = "2.137.0"
LEDGER_VERSION = "pdf-ingest-models-1"
MODEL_SPECS = (
    ("docling-project/docling-layout-heron", "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8", ("config.json", "preprocessor_config.json", "model.safetensors")),
    ("docling-project/docling-models", "fc0f2d45e2218ea24bce5045f58a389aed16dc23", ("model_artifacts/tableformer/accurate/*",)),
)


def _hash_file(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob_hash(path: Path) -> str:
    digest = hashlib.sha1()
    digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_file(root: Path, name: str) -> Path:
    rel = PurePosixPath(name)
    if rel.is_absolute() or not rel.parts or any(p in {"", ".", ".."} for p in rel.parts):
        raise IngestError("MODEL_UNAVAILABLE", "Model ledger contains an unsafe file reference.")
    path = root.joinpath(*rel.parts)
    if any(p.is_symlink() for p in (path, *path.parents) if p != root.parent):
        raise IngestError("MODEL_UNAVAILABLE", "Model files must be local regular files without symlinks.")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise IngestError("MODEL_UNAVAILABLE", "Required local model artifact is missing.")
    return path


def _docling_installed() -> None:
    try:
        version = importlib.metadata.version("docling")
    except importlib.metadata.PackageNotFoundError as exc:
        raise IngestError("MODEL_UNAVAILABLE", "Install the pinned layout dependency extra before provisioning or conversion.") from exc
    if version != DOCLING_VERSION:
        raise IngestError("MODEL_UNAVAILABLE", "The installed Docling version does not match the adapter's pinned version.")


def load_model_ledger(models_dir: Path) -> dict[str, Any]:
    """Read private runtime configuration after checking every persisted hash."""
    _docling_installed()
    root = Path(models_dir)
    try:
        ledger_path = _safe_file(root, "models.json")
        if ledger_path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("oversized ledger")
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        if set(ledger) != {"schema_version", "docling_version", "models", "files", "ocr"}:
            raise ValueError("unexpected fields")
        if ledger["schema_version"] != LEDGER_VERSION or ledger["docling_version"] != DOCLING_VERSION:
            raise ValueError("unsupported ledger")
        models, files = ledger["models"], ledger["files"]
        if not isinstance(models, dict) or not isinstance(files, dict) or not files:
            raise ValueError("invalid ledger")
        if set(models) != {spec[0] for spec in MODEL_SPECS}:
            raise ValueError("unsupported model set")
        for repo_id, pinned_revision, _ in MODEL_SPECS:
            if models.get(repo_id) != pinned_revision:
                raise ValueError("model revision does not match this pinned profile")
        required = {
            "docling-project--docling-layout-heron/config.json",
            "docling-project--docling-layout-heron/preprocessor_config.json",
            "docling-project--docling-layout-heron/model.safetensors",
            "docling-project--docling-models/model_artifacts/tableformer/accurate/tm_config.json",
        }
        if not required.issubset(files):
            raise ValueError("incomplete artifact set")
        if not any(name.startswith("docling-project--docling-models/model_artifacts/tableformer/accurate/") and name.endswith((".safetensors", ".pt", ".pth")) for name in files):
            raise ValueError("missing table weights")
        for name, expected in files.items():
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("invalid checksum")
            if _hash_file(_safe_file(root, name)) != expected:
                raise ValueError("checksum mismatch")
        return ledger
    except IngestError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise IngestError("MODEL_UNAVAILABLE", "Local model ledger is missing, incomplete, or fails checksum validation; run provision-models explicitly.") from exc


def _validate_ocr(root: Path, ledger: dict[str, Any]) -> dict[str, str]:
    ocr = ledger.get("ocr")
    if not isinstance(ocr, dict) or set(ocr) != {"engine", "engine_sha256", "version", "eng_sha256"}:
        raise IngestError("OCR_UNAVAILABLE", "English OCR requires a separately provisioned local Tesseract engine and English data.")
    try:
        engine = Path(ocr["engine"])
        if not engine.is_absolute() or not engine.is_file() or not os.access(engine, os.X_OK):
            raise ValueError("missing engine")
        if _hash_file(engine) != ocr["engine_sha256"]:
            raise ValueError("engine changed")
        traineddata = _safe_file(root, "ocr/eng.traineddata")
        if _hash_file(traineddata) != ocr["eng_sha256"]:
            raise ValueError("English data changed")
        result = subprocess.run([str(engine), "--version"], check=True, capture_output=True, text=True, timeout=10)
        version = result.stdout.splitlines()[0]
        if version != ocr["version"]:
            raise ValueError("engine version changed")
    except (OSError, ValueError, subprocess.SubprocessError, IndexError, KeyError) as exc:
        raise IngestError("OCR_UNAVAILABLE", "The pinned Tesseract engine or English trained data is unavailable or changed.") from exc
    return {"tesseract": version, "tesseract_engine_sha256": ocr["engine_sha256"], "tesseract_eng_sha256": ocr["eng_sha256"]}


def validate_models(models_dir: Path, profile: str = "native_layout_v1") -> dict[str, str]:
    if profile not in {"native_layout_v1", "english_ocr_v1"}:
        raise IngestError("INVALID_INPUT", "Unsupported conversion profile; no automatic fallback is available.")
    ledger = load_model_ledger(models_dir)
    revisions = dict(ledger["models"])
    revisions["artifact_ledger_sha256"] = hashlib.sha256(json.dumps(ledger["files"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    revisions["docling"] = DOCLING_VERSION
    if profile == "english_ocr_v1":
        revisions.update(_validate_ocr(Path(models_dir), ledger))
    return revisions


def _provision_ocr(stage: Path, tessdata_dir: Path | None) -> dict[str, str]:
    executable = shutil.which("tesseract")
    if not executable:
        raise IngestError("OCR_UNAVAILABLE", "Install a local Tesseract CLI engine before provisioning English OCR.")
    engine = Path(executable).resolve()
    try:
        version = subprocess.run([str(engine), "--version"], check=True, capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
        if not version.startswith("tesseract 5."):
            raise ValueError("Tesseract 5 required")
        if tessdata_dir is None:
            listing = subprocess.run([str(engine), "--list-langs"], check=True, capture_output=True, text=True, timeout=10)
            match = re.search(r'List of available languages in "([^"]+)"', listing.stdout + listing.stderr)
            if not match:
                raise ValueError("cannot locate English data")
            tessdata_dir = Path(match.group(1))
        source_data = Path(tessdata_dir) / "eng.traineddata"
        if not source_data.is_file():
            raise ValueError("English data missing")
        (stage / "ocr").mkdir()
        shutil.copyfile(source_data, stage / "ocr/eng.traineddata")
        return {"engine": str(engine), "engine_sha256": _hash_file(engine), "version": version, "eng_sha256": _hash_file(stage / "ocr/eng.traineddata")}
    except (OSError, ValueError, subprocess.SubprocessError, IndexError) as exc:
        raise IngestError("OCR_UNAVAILABLE", "English Tesseract 5 data could not be provisioned; specify a local tessdata directory.") from exc


def provision_models(models_dir: Path, *, with_ocr: bool = False, tessdata_dir: Path | None = None) -> dict[str, str]:
    """Network-enabled operator action, never invoked by the converter.

    Existing complete artifacts are reused. A nonempty unknown destination is
    preserved and rejected, so provisioning cannot overwrite operator files.
    """
    _docling_installed()
    destination = Path(models_dir)
    if (destination / "models.json").is_file():
        return validate_models(destination, "english_ocr_v1" if with_ocr else "native_layout_v1")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise IngestError("MODEL_UNAVAILABLE", "Model destination is nonempty without a valid ledger; choose an empty directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Use the documented plain HTTPS transport. Its artifacts still undergo
        # the same authoritative hash verification below; no Xet cache is needed.
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ.setdefault("HF_HOME", str(destination.parent / ".huggingface-cache"))
        from huggingface_hub import HfApi, snapshot_download
        from huggingface_hub.utils import disable_progress_bars
        disable_progress_bars()
        # Upstream download warnings can contain signed CDN query strings or
        # token suggestions. The CLI returns a stable, operator-safe error.
        logging.getLogger("huggingface_hub").setLevel(logging.CRITICAL)
        api = HfApi()
        with tempfile.TemporaryDirectory(prefix=".model-provision-", dir=destination.parent) as temporary:
            stage = Path(temporary) / "artifacts"
            stage.mkdir()
            revisions: dict[str, str] = {}
            hashes: dict[str, str] = {}
            import fnmatch
            for repo_id, requested_revision, patterns in MODEL_SPECS:
                info = api.model_info(repo_id, revision=requested_revision, files_metadata=True)
                commit = info.sha
                if commit != requested_revision:
                    raise ValueError("Hub did not return an immutable commit")
                folder = stage / repo_id.replace("/", "--")
                snapshot_download(repo_id=repo_id, revision=commit, local_dir=folder, cache_dir=Path(temporary) / "hub-cache", allow_patterns=list(patterns), max_workers=2)
                selected = [s for s in info.siblings or [] if any(fnmatch.fnmatchcase(s.rfilename, p) for p in patterns)]
                if not selected:
                    raise ValueError("empty model download")
                for sibling in selected:
                    local_path = _safe_file(folder, sibling.rfilename)
                    if sibling.lfs is not None:
                        expected = sibling.lfs.sha256
                        actual = _hash_file(local_path)
                    else:
                        expected = sibling.blob_id
                        actual = _git_blob_hash(local_path)
                    if not expected or expected != actual:
                        raise ValueError("official model checksum mismatch")
                    hashes[local_path.relative_to(stage).as_posix()] = _hash_file(local_path)
                revisions[repo_id] = commit
            ledger = {"schema_version": LEDGER_VERSION, "docling_version": DOCLING_VERSION, "models": revisions, "files": hashes, "ocr": _provision_ocr(stage, tessdata_dir) if with_ocr else None}
            (stage / "models.json").write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            validate_models(stage, "english_ocr_v1" if with_ocr else "native_layout_v1")
            if destination.exists():
                destination.rmdir()  # only the previously checked empty directory
            stage.rename(destination)
    except IngestError:
        raise
    except Exception as exc:
        raise IngestError("MODEL_UNAVAILABLE", "Explicit model provisioning failed. Check HTTPS access to Hugging Face and its artifact CDN; TLS and checksum verification remain enabled.") from exc
    return validate_models(destination, "english_ocr_v1" if with_ocr else "native_layout_v1")
