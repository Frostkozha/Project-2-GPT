"""Durable queue, validated idempotency and isolated, cancellable conversion."""
from __future__ import annotations

import fcntl
import base64
import hashlib
import importlib.metadata
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .preflight import check_source_rights, open_input, validate_input_path
from .schema import (BLOCK_ADAPTER, IngestError, Issue, Limits, Manifest, PageInventory,
                     ReviewReport, SourceSpec)

QUEUE_CAPACITY = 8
_BLOCKED = {"INVALID_INPUT", "PATH_DENIED", "RIGHTS_UNCONFIRMED", "ENCRYPTED_SOURCE", "FILE_LIMIT", "PAGE_LIMIT"}


@dataclass
class JobResult:
    job_id: str
    status: str
    bundle_path: Path | None = None
    code: str | None = None
    message: str | None = None
    reused: bool = False

    def model_dump(self, mode="python") -> dict:
        result = asdict(self)
        if mode == "json" and result["bundle_path"] is not None:
            result["bundle_path"] = str(result["bundle_path"])
        return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_start(pid: int) -> str | None:
    try:
        # Comm can contain spaces and parentheses; starttime is field 22.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _connect(output_root: Path) -> sqlite3.Connection:
    output_root.mkdir(parents=True, exist_ok=True)
    database = output_root / ".jobs.sqlite3"
    if database.is_symlink():
        raise IngestError("PATH_DENIED", "The job database may not be a symlink.")
    connection = sqlite3.connect(database, timeout=10, isolation_level=None)
    os.chmod(database, 0o600)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY, status TEXT NOT NULL, idempotency_key TEXT,
        source_id TEXT, source_version TEXT, created_at TEXT, updated_at TEXT,
        bundle_name TEXT, code TEXT, message TEXT, parent_pid INTEGER,
        parent_start TEXT, worker_pid INTEGER, worker_start TEXT
    )""")
    return connection


def _finish(connection, job_id, status, code=None, message=None) -> None:
    connection.execute("""UPDATE jobs SET status=?,code=?,message=?,updated_at=?,worker_pid=NULL,worker_start=NULL
                          WHERE job_id=? AND status!='cancelled'""", (status, code, message, _now(), job_id))


def _record_terminal(connection, job_id, source, status, code=None, message=None, key=None, bundle_name=None) -> None:
    """Record early path/rights/model failures as well as queued outcomes."""
    connection.execute("""INSERT OR IGNORE INTO jobs
        (job_id,status,idempotency_key,source_id,source_version,created_at,updated_at,bundle_name,code,message,parent_pid,parent_start)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job_id, status, key, source.source_id, source.source_version, _now(), _now(), bundle_name, code, message, os.getpid(), _pid_start(os.getpid())))
    connection.execute("UPDATE jobs SET idempotency_key=COALESCE(?,idempotency_key),bundle_name=COALESCE(?,bundle_name) WHERE job_id=? AND status!='cancelled'", (key, bundle_name, job_id))
    _finish(connection, job_id, status, code, message)


def _queue_job(connection, job_id, source) -> None:
    connection.execute("BEGIN IMMEDIATE")
    if connection.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0] >= QUEUE_CAPACITY:
        connection.rollback()
        raise IngestError("QUEUE_FULL", "The eight-job queue is full.")
    connection.execute("INSERT INTO jobs(job_id,status,source_id,source_version,created_at,updated_at,parent_pid,parent_start) VALUES(?,?,?,?,?,?,?,?)",
                       (job_id, "queued", source.source_id, source.source_version, _now(), _now(), os.getpid(), _pid_start(os.getpid())))
    connection.commit()


def _cancelled(connection, job_id) -> bool:
    row = connection.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return row is None or row["status"] == "cancelled"


def _terminate_group(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def cancel_job(output_root: Path, job_id: str) -> JobResult:
    connection = _connect(Path(output_root))
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            connection.rollback()
            return JobResult(job_id, "blocked", code="INVALID_INPUT", message="The job does not exist.")
        if row["status"] not in {"queued", "running"}:
            connection.rollback()
            return JobResult(job_id, row["status"], code="JOB_FINISHED", message="The job has already finished.")
        connection.execute("UPDATE jobs SET status='cancelled',code='CANCELLED',message=?,updated_at=? WHERE job_id=?",
                           ("The operator cancelled the job.", _now(), job_id))
        connection.commit()
        pid = row["worker_pid"]
        if pid and _pid_start(pid) == row["worker_start"]:
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return JobResult(job_id, "cancelled", code="CANCELLED", message="The operator cancelled the job.")
    finally:
        connection.close()


def list_jobs(output_root: Path) -> list[dict]:
    connection = _connect(Path(output_root))
    try:
        rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        for row in rows:
            if row["status"] in {"queued", "running"} and _pid_start(row["parent_pid"]) != row["parent_start"]:
                if row["worker_pid"] and _pid_start(row["worker_pid"]) == row["worker_start"]:
                    try:
                        if os.getpgid(row["worker_pid"]) == row["worker_pid"]:
                            os.killpg(row["worker_pid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                _finish(connection, row["job_id"], "failed", "WORKER_EXITED", "The job owner exited; retry explicitly.")
        rows = connection.execute("SELECT job_id,status,source_id,source_version,created_at,updated_at,bundle_name,code,message FROM jobs ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _hash_json(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _fingerprints(source, digest, profile, revisions, page_range) -> tuple[str, str, str]:
    root = Path(__file__).resolve().parent.parent
    lock = next((path for path in [root / "uv.lock", Path(__file__).parent / "dependency.lock", root / "requirements.lock"] if path.is_file()), None)
    if lock is None:
        raise IngestError("MODEL_UNAVAILABLE", "A dependency lockfile is required for reproducible conversion.")
    lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()
    code = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((root / "pdf_ingest").glob("*.py"))}
    dependencies = {}
    for name in ("pydantic", "pypdf", "pypdfium2", "docling", "docling-core",
                 "docling-ibm-models", "docling-parse", "torch", "torchvision",
                 "transformers", "huggingface-hub", "numpy", "pillow"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    fingerprint = _hash_json({"profile": profile, "model_revisions": revisions, "lock": lock_hash,
                              "installed": dependencies, "python": sys.version.split()[0],
                              "code": code, "normalization": "nfc-lf-v1", "serialization": "markdown-v1"})
    identity = _hash_json({"source": source.model_dump(mode="json"), "source_sha256": digest,
                          "page_range": page_range, "conversion_fingerprint": fingerprint})
    return fingerprint, identity, lock_hash


def _find_reusable(connection, output_root, key, source) -> Path | None:
    from .serialize import validate_bundle
    # Current permissions are checked even if a structurally valid bundle exists.
    check_source_rights(source)
    superseded = set()
    # Review versions are written by the review API, independently of job rows.
    # An unchanged original must stop being reusable once it has a correction
    # descendant, otherwise a rerun could silently undo a human correction.
    for path in output_root.glob("conversion_*/manifest.json"):
        try:
            if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
                continue
            descendant = Manifest.model_validate_json(path.read_bytes())
            if descendant.parent_conversion_id and (path.parent / "corrections.jsonl").is_file():
                validate_bundle(path.parent)
                superseded.add(descendant.parent_conversion_id)
        except (OSError, ValueError):
            continue
    for row in connection.execute("SELECT bundle_name FROM jobs WHERE status='completed' AND idempotency_key=? AND bundle_name IS NOT NULL ORDER BY created_at DESC", (key,)):
        name = row["bundle_name"]
        if Path(name).name != name:
            continue
        candidate = output_root / name
        try:
            manifest, _blocks, report = validate_bundle(candidate)
            if (manifest.idempotency_key == key and manifest.source == source
                    and manifest.review_state in {"unreviewed", "needs_review"}
                    and not report.approvals and manifest.parent_conversion_id is None
                    and not any(issue.resolution is not None for issue in report.issues)
                    and all(block.extraction_quality in {"unreviewed", "unresolved"} for block in _blocks)
                    and manifest.conversion_id not in superseded
                    and not (candidate / "corrections.jsonl").exists()):
                return candidate
        except (ValueError, OSError):
            continue
    return None


def _execute(connection, job_id, staging, request, limits) -> dict:
    request_path = staging / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    result_path = staging / "result.json"
    result_fd = os.open(result_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    watchdog_read, watchdog_write = os.pipe()
    os.set_blocking(watchdog_read, False)
    # Source parsers do not inherit API keys, Git credentials or application
    # secrets. Shared library paths/locales suffice for the local native profile.
    environment = {name: os.environ[name] for name in ("PATH", "LANG", "LC_ALL", "TZ", "LD_LIBRARY_PATH") if name in os.environ}
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        environment[name] = str(limits.worker_threads)
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                        "PDF_INGEST_PAGE_WATCHDOG_FD": str(watchdog_write), "TMPDIR": str(staging)})
    package_root = str(Path(__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = package_root
    process = None
    try:
        started = time.monotonic()
        process = subprocess.Popen([sys.executable, "-m", "pdf_ingest.worker", str(request_path), str(result_fd)],
                                   pass_fds=(result_fd, watchdog_write), start_new_session=True, env=environment,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   cwd=staging)
        os.close(result_fd)
        result_fd = -1
        os.close(watchdog_write)
        watchdog_write = -1
        connection.execute("UPDATE jobs SET worker_pid=?,worker_start=? WHERE job_id=? AND status='running'", (process.pid, _pid_start(process.pid), job_id))
        page_deadline = float("inf")
        while process.poll() is None:
            if _cancelled(connection, job_id):
                _terminate_group(process)
                raise IngestError("CANCELLED", "The operator cancelled the job.")
            current = time.monotonic()
            try:
                if os.read(watchdog_read, 4096):
                    page_deadline = current + limits.page_timeout_seconds
            except BlockingIOError:
                pass
            if current - started >= limits.job_timeout_seconds or current >= page_deadline:
                _terminate_group(process)
                raise IngestError("DEADLINE_EXCEEDED", "The earliest page or job deadline was exceeded.")
            time.sleep(0.025)
        if _cancelled(connection, job_id):
            raise IngestError("CANCELLED", "The operator cancelled the job.")
        if process.returncode != 0:
            raise IngestError("RESOURCE_LIMIT", "The worker terminated without a complete result within its resource budget.")
        if result_path.stat().st_size > request["limits"]["scratch_bytes"]:
            raise IngestError("RESOURCE_LIMIT", "The worker exceeded its result-storage budget.")
        try:
            result = json.loads(result_path.read_bytes())
        except (ValueError, OSError):
            raise IngestError("PARSE_FAILED", "The worker did not return a valid result.") from None
        if not result.get("ok"):
            raise IngestError(result.get("code", "PARSE_FAILED"), result.get("message", "The isolated parser failed."))
        return result
    finally:
        if process is not None and process.poll() is None:
            _terminate_group(process)
        for fd in (result_fd, watchdog_read, watchdog_write):
            if fd >= 0:
                os.close(fd)


def convert(pdf_path: Path, import_root: Path, output_root: Path, source: SourceSpec,
            profile: str, models_dir: Path, limits: Limits, page_range: tuple[int, int] | None = None,
            force: bool = False) -> JobResult:
    job_id = uuid.uuid4().hex
    output_root = Path(output_root).resolve()
    connection = None
    lock_fd = None
    try:
        if sys.platform != "linux":
            raise IngestError("RESOURCE_LIMIT", "Conversion currently requires the tested Linux syscall sandbox.")
        validate_input_path(Path(pdf_path), Path(import_root), source, limits)
        if profile not in {"native_layout_v1", "english_ocr_v1"}:
            raise IngestError("INVALID_INPUT", "Select an explicitly supported conversion profile.")
        if page_range is not None and (len(page_range) != 2 or page_range[0] < 1 or page_range[1] < page_range[0]):
            raise IngestError("INVALID_INPUT", "The page range must be positive and ordered.")
        from .provisioning import validate_models
        revisions = validate_models(Path(models_dir), profile)
        connection = _connect(output_root)
        # Recover abandoned records before counting queue slots.
        list_jobs(output_root)
        # Reserve a bounded queue slot before copying potentially large input.
        _queue_job(connection, job_id, source)
        private_root = output_root / ".private"
        if private_root.is_symlink():
            raise IngestError("PATH_DENIED", "The private staging directory may not be a symlink.")
        private_root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(private_root, 0o700)
        with tempfile.TemporaryDirectory(prefix=f"job-{job_id}-", dir=private_root) as directory:
            staging = Path(directory)
            staged_pdf = staging / "source.pdf"
            descriptor, _validated = open_input(Path(pdf_path), Path(import_root), source, limits)
            copied = 0
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb") as original, staged_pdf.open("xb") as private:
                while chunk := original.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > limits.max_file_bytes:
                        raise IngestError("FILE_LIMIT", "The PDF grew beyond the file-size limit during staging.")
                    if copied + 65536 >= limits.scratch_bytes:
                        raise IngestError("RESOURCE_LIMIT", "The scratch budget cannot hold the private source copy.")
                    private.write(chunk)
                    digest.update(chunk)
            os.chmod(staged_pdf, 0o400)
            if _cancelled(connection, job_id):
                raise IngestError("CANCELLED", "The operator cancelled the job.")
            if source.expected_source_sha256 is not None and digest.hexdigest() != source.expected_source_sha256:
                raise IngestError("HASH_MISMATCH", "The PDF does not match the registered source-file hash.")
            fingerprint, key, lock_hash = _fingerprints(source, digest.hexdigest(), profile, revisions, page_range)
            connection.execute("UPDATE jobs SET idempotency_key=?,updated_at=? WHERE job_id=? AND status='queued'", (key, _now(), job_id))
            if not force and (cached := _find_reusable(connection, output_root, key, source)):
                _record_terminal(connection, job_id, source, "completed", code="REUSED", key=key, bundle_name=cached.name)
                return JobResult(job_id, "completed", cached, reused=True)
            global_lock = Path(tempfile.gettempdir()) / f"pdf-ingest-worker-{os.getuid()}.lock"
            lock_fd = os.open(global_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            while True:
                if _cancelled(connection, job_id):
                    return JobResult(job_id, "cancelled", code="CANCELLED", message="The operator cancelled the job.")
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.05)
            # Recheck after queue wait, including duplicate requests queued together.
            check_source_rights(source)
            if not force and (cached := _find_reusable(connection, output_root, key, source)):
                _finish(connection, job_id, "completed")
                connection.execute("UPDATE jobs SET bundle_name=? WHERE job_id=?", (cached.name, job_id))
                return JobResult(job_id, "completed", cached, reused=True)
            changed = connection.execute("UPDATE jobs SET status='running',updated_at=? WHERE job_id=? AND status='queued'", (_now(), job_id)).rowcount
            if changed != 1:
                raise IngestError("CANCELLED", "The operator cancelled the job.")
            # Reserve source + request + parent serialization. Only result fd is
            # writable in worker, and its hard quota is a conservative partition.
            result_budget = (limits.scratch_bytes - copied - 65536) // 16
            if result_budget < 4096:
                raise IngestError("RESOURCE_LIMIT", "The scratch budget is too small for bounded extraction output.")
            worker_limits = limits.model_copy(update={"scratch_bytes": result_budget})
            request = {"pdf_path": str(staged_pdf), "import_root": str(staging),
                       "source": source.model_dump(mode="json"), "limits": worker_limits.model_dump(mode="json"),
                       "profile": profile, "models_dir": str(Path(models_dir).resolve()), "page_range": page_range,
                       "fingerprint": fingerprint, "source_sha256": digest.hexdigest()}
            if len(json.dumps(request).encode()) > 65536:
                raise IngestError("RESOURCE_LIMIT", "The source metadata exceeds the bounded request budget.")
            result = _execute(connection, job_id, staging, request, limits)
            blocks = [BLOCK_ADAPTER.validate_json(json.dumps(record)) for record in result["blocks"]]
            inventory = [PageInventory.model_validate_json(json.dumps(record)) for record in result["page_inventory"]]
            issues = [Issue.model_validate_json(json.dumps(record)) for record in result["issues"]]
            if result["source_sha256"] != digest.hexdigest() or result["model_revisions"] != revisions:
                raise IngestError("HASH_MISMATCH", "The source or model fingerprint changed while conversion ran.")
            manifest = Manifest(
                conversion_id=job_id, source=source, source_sha256=digest.hexdigest(),
                source_page_count=result["source_page_count"], requested_pages=result["requested_pages"],
                profile=profile, conversion_fingerprint=fingerprint, dependency_lock_sha256=lock_hash,
                converter_version="0.1.0", model_revisions=result["model_revisions"], normalization_version="nfc-lf-v1",
                serialization_version="markdown-v1", created_at=_now(), review_state="needs_review" if issues else "unreviewed",
                page_inventory=inventory, idempotency_key=key, metrics=result["metrics"],
            )
            from .serialize import _rename_atomic, validate_bundle, write_bundle
            prepared = staging / "bundle"
            write_bundle(prepared, manifest, blocks, ReviewReport(issues=issues))
            validate_bundle(prepared)
            consumed = sum(path.stat().st_size for path in staging.rglob("*") if path.is_file())
            if consumed > limits.scratch_bytes:
                raise IngestError("RESOURCE_LIMIT", "The completed staging output exceeds the scratch budget.")
            destination = output_root / f"conversion_{source.source_id}_{source.source_version}_{job_id}"
            # Cancellation and publication share the same SQLite write lock.
            # Cancel either commits before this guard, or observes completed state.
            connection.execute("BEGIN IMMEDIATE")
            if _cancelled(connection, job_id):
                connection.rollback()
                raise IngestError("CANCELLED", "The operator cancelled the job.")
            check_source_rights(source)
            if destination.exists():
                connection.rollback()
                raise IngestError("INVALID_INPUT", "The versioned output destination already exists.")
            _rename_atomic(prepared, destination)
            connection.execute("UPDATE jobs SET status='completed',bundle_name=?,updated_at=?,worker_pid=NULL,worker_start=NULL WHERE job_id=? AND status='running'",
                               (destination.name, _now(), job_id))
            connection.commit()
            return JobResult(job_id, "completed", destination)
    except IngestError as exc:
        status = "cancelled" if exc.code == "CANCELLED" else "blocked" if exc.code in _BLOCKED else "failed"
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
        else:
            try:
                connection = _connect(output_root)
            except (IngestError, OSError, sqlite3.Error):
                pass
        if connection is not None:
            _record_terminal(connection, job_id, source, status, exc.code, exc.message)
        return JobResult(job_id, status, code=exc.code, message=exc.message)
    except (OSError, ValueError, sqlite3.Error):
        if connection is not None:
            if connection.in_transaction:
                connection.rollback()
            _record_terminal(connection, job_id, source, "failed", "PARSE_FAILED", "The job failed validation; no output was published.")
        else:
            try:
                connection = _connect(output_root)
                _record_terminal(connection, job_id, source, "failed", "PARSE_FAILED", "The job failed validation; no output was published.")
            except (IngestError, OSError, sqlite3.Error):
                pass
        return JobResult(job_id, "failed", code="PARSE_FAILED", message="The job failed validation; no output was published.")
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if connection is not None:
            connection.close()


# Small aliases for callers using command-oriented names.
cancel = cancel_job


def render_preview(pdf_path: Path, import_root: Path, source: SourceSpec, limits: Limits,
                   page: int, output_png: Path) -> Path:
    """Render a physical PDF page with the same offline, read-only worker.

    Preview is a local PNG derivative only and creates no evidence approvals.
    A durable job row permits cancellation through the regular jobs command.
    """
    if page < 1:
        raise IngestError("INVALID_INPUT", "The physical page index must be positive.")
    if sys.platform != "linux":
        raise IngestError("RESOURCE_LIMIT", "Preview rendering requires the tested Linux syscall sandbox.")
    validate_input_path(Path(pdf_path), Path(import_root), source, limits)
    destination = Path(output_png).absolute()
    if destination.suffix.lower() != ".png":
        raise IngestError("INVALID_INPUT", "The preview output must have a .png extension.")
    if destination.exists() or destination.is_symlink():
        raise IngestError("INVALID_INPUT", "Preview output already exists; select a new path.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(destination.parent)
    job_id = uuid.uuid4().hex
    lock_fd = None
    try:
        list_jobs(destination.parent)
        _queue_job(connection, job_id, source)
        global_lock = Path(tempfile.gettempdir()) / f"pdf-ingest-worker-{os.getuid()}.lock"
        lock_fd = os.open(global_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        while True:
            if _cancelled(connection, job_id):
                raise IngestError("CANCELLED", "The operator cancelled the job.")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.05)
        connection.execute("UPDATE jobs SET status='running',updated_at=? WHERE job_id=? AND status='queued'", (_now(), job_id))
        with tempfile.TemporaryDirectory(prefix=".preview-", dir=destination.parent) as directory:
            staging = Path(directory)
            descriptor, _validated = open_input(Path(pdf_path), Path(import_root), source, limits)
            digest = hashlib.sha256()
            copied = 0
            staged_pdf = staging / "source.pdf"
            with os.fdopen(descriptor, "rb") as original, staged_pdf.open("xb") as private:
                while chunk := original.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > limits.max_file_bytes:
                        raise IngestError("FILE_LIMIT", "The source grew beyond the file-size limit.")
                    if copied + 65536 >= limits.scratch_bytes:
                        raise IngestError("RESOURCE_LIMIT", "The scratch budget cannot hold the preview source copy.")
                    private.write(chunk)
                    digest.update(chunk)
            os.chmod(staged_pdf, 0o400)
            if source.expected_source_sha256 is not None and digest.hexdigest() != source.expected_source_sha256:
                raise IngestError("HASH_MISMATCH", "The PDF does not match the registered source-file hash.")
            result_budget = (limits.scratch_bytes - copied - 65536) // 4
            if result_budget < 4096:
                raise IngestError("RESOURCE_LIMIT", "The scratch budget is too small for a bounded page preview.")
            worker_limits = limits.model_copy(update={"scratch_bytes": result_budget})
            request = {"operation": "preview", "pdf_path": str(staged_pdf), "import_root": str(staging),
                       "source": source.model_dump(mode="json"), "limits": worker_limits.model_dump(mode="json"),
                       "page": page, "source_sha256": digest.hexdigest()}
            if len(json.dumps(request).encode()) > 65536:
                raise IngestError("RESOURCE_LIMIT", "The source metadata exceeds the bounded request budget.")
            result = _execute(connection, job_id, staging, request, limits)
            payload = base64.b64decode(result["preview_png"], validate=True)
            if not payload.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(payload).hexdigest() != result["png_sha256"] or result["pdf_page_index"] != page:
                raise IngestError("HASH_MISMATCH", "The preview result is invalid.")
            staged_png = staging / "preview.png"
            with staged_png.open("xb") as stream:
                os.chmod(staged_png, 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            connection.execute("BEGIN IMMEDIATE")
            if _cancelled(connection, job_id):
                connection.rollback()
                raise IngestError("CANCELLED", "The operator cancelled the job.")
            from .serialize import _rename_atomic
            _rename_atomic(staged_png, destination)
            _finish(connection, job_id, "completed")
            connection.commit()
            return destination
    except IngestError as exc:
        if connection.in_transaction:
            connection.rollback()
        status = "cancelled" if exc.code == "CANCELLED" else "blocked" if exc.code in _BLOCKED else "failed"
        _finish(connection, job_id, status, exc.code, exc.message)
        raise
    except (OSError, ValueError, sqlite3.Error):
        if connection.in_transaction:
            connection.rollback()
        _finish(connection, job_id, "failed", "PARSE_FAILED", "The preview failed; no image was published.")
        raise IngestError("PARSE_FAILED", "The preview failed; no image was published.") from None
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        connection.close()
