"""Private protocol; a parser process never publishes a live output bundle."""
from __future__ import annotations

import hashlib
import base64
import io
import json
import os
import resource
import sys
import time
from pathlib import Path

from .preflight import page_deadline, preflight
from .sandbox import apply_limits, isolate
from .schema import IngestError, Limits, SourceSpec


def run(request: dict) -> dict:
    started = time.monotonic()
    source = SourceSpec.model_validate_json(json.dumps(request["source"]))
    limits = Limits.model_validate_json(json.dumps(request["limits"]))
    # Bound even trusted initialization; syscall isolation follows import
    # prewarming, and precedes all source/model parsing.
    apply_limits(limits)
    if request.get("operation") != "preview" and request.get("profile") == "native_layout_v1":
        # Trusted dependency imports may create native thread pools or open
        # /dev/null read-write (dill). Prewarm before installing TSYNC seccomp;
        # no source/model is parsed or downloaded during this import phase.
        from .extract import prepare_runtime
        prepare_runtime()
    isolate(limits)
    if request.get("operation") == "preview":
        checked = preflight(Path(request["pdf_path"]), Path(request["import_root"]), source, limits,
                            (request["page"], request["page"]))
        if checked.source_sha256 != request["source_sha256"]:
            raise IngestError("HASH_MISMATCH", "The staged source hash changed.")
        with page_deadline(limits.page_timeout_seconds):
            import pypdfium2 as pdfium
            document = pdfium.PdfDocument(str(checked.validated_path))
            page = document[request["page"] - 1]
            bitmap = page.render(scale=1.5)
            image = bitmap.to_pil()
            encoded = io.BytesIO()
            image.save(encoded, format="PNG")
            payload = encoded.getvalue()
            image.close()
            bitmap.close()
            page.close()
            document.close()
        return {"ok": True, "preview_png": base64.b64encode(payload).decode("ascii"),
                "png_sha256": hashlib.sha256(payload).hexdigest(), "pdf_page_index": request["page"]}
    from .extract import extract_document
    if request["profile"] == "english_ocr_v1":
        raise IngestError("OCR_UNAVAILABLE", "English OCR requires a bounded temporary-file adapter; this Linux worker supports the read-only native profile.")
    checked = preflight(Path(request["pdf_path"]), Path(request["import_root"]), source, limits,
                        tuple(request["page_range"]) if request["page_range"] else None)
    if checked.source_sha256 != request["source_sha256"]:
        raise IngestError("HASH_MISMATCH", "The staged source hash changed.")
    blocks, issues, inventory, revisions = extract_document(
        checked.validated_path, source, request["profile"], Path(request["models_dir"]),
        request["fingerprint"], checked.requested_pages, checked.page_inventory,
        threads=limits.worker_threads, page_timeout_seconds=limits.page_timeout_seconds,
    )
    elapsed = time.monotonic() - started
    return {
        "ok": True,
        "source_sha256": checked.source_sha256,
        "source_page_count": checked.source_page_count,
        "requested_pages": checked.requested_pages,
        "page_inventory": [page.model_dump(mode="json") for page in inventory],
        "blocks": [block.model_dump(mode="json") for block in blocks],
        "issues": [issue.model_dump(mode="json") for issue in issues],
        "model_revisions": revisions,
        "metrics": {"elapsed_seconds": elapsed,
                    "seconds_per_page": elapsed / len(checked.requested_pages),
                    "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024},
    }


def main() -> int:
    # Parent creates the one writable file before isolating and passes its fd.
    import ctypes
    import signal
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:  # PR_SET_PDEATHSIG
        return 75
    result_fd = int(sys.argv[2])
    try:
        with Path(sys.argv[1]).open("rb") as stream:
            request = json.load(stream)
        result = run(request)
    except IngestError as exc:
        result = {"ok": False, "code": exc.code, "message": exc.message}
    except (MemoryError, OSError):
        result = {"ok": False, "code": "RESOURCE_LIMIT", "message": "The worker exhausted or could not enforce its resource budget."}
    except Exception:
        result = {"ok": False, "code": "PARSE_FAILED", "message": "The isolated parser failed; no output was published."}
    try:
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        view = memoryview(payload)
        while view:
            written = os.write(result_fd, view)
            view = view[written:]
        os.close(result_fd)
    except (MemoryError, OSError):
        return 75
    return 0


if __name__ == "__main__":
    # The complete protocol has been written and its descriptor closed. Skip
    # trusted library temp-directory finalizers that cannot mutate files under
    # the filter; the parent owns private scratch cleanup and OS frees threads.
    os._exit(main())
