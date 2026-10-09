"""Permission and path checks, followed by worker-only PDF diagnostics."""
from __future__ import annotations

import hashlib
import math
import os
import signal
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .schema import IngestError, Limits, PageInventory, SourceSpec


@dataclass
class PreflightResult:
    source_sha256: str
    source_page_count: int
    requested_pages: list[int]
    page_inventory: list[PageInventory]
    validated_path: Path


def check_source_rights(source: SourceSpec) -> None:
    """Conversion needs acquisition rights; content signoff gates later import."""
    if not source.eligible:
        raise IngestError("RIGHTS_UNCONFIRMED", "The source is currently ineligible.")
    if not source.synthetic and (not source.rights_confirmed or not source.rights_reference.strip()):
        raise IngestError("RIGHTS_UNCONFIRMED", "Confirmed processing rights and a rights reference are required.")


def _relative_path(pdf_path: Path, import_root: Path) -> tuple[Path, Path]:
    raw = str(pdf_path)
    if "://" in raw or raw.startswith(("file:", "http:", "https:")):
        raise IngestError("INVALID_INPUT", "A local PDF path is required.")
    if ".." in Path(raw).parts:
        raise IngestError("PATH_DENIED", "Path traversal is not permitted.")
    try:
        root = Path(import_root).resolve(strict=True)
        if not root.is_dir():
            raise OSError("not a directory")
        candidate = Path(pdf_path) if Path(pdf_path).is_absolute() else root / pdf_path
        relative = candidate.relative_to(root)
    except (OSError, ValueError):
        raise IngestError("PATH_DENIED", "The PDF must be inside the configured import root.") from None
    if not relative.parts or candidate.suffix.lower() != ".pdf":
        raise IngestError("INVALID_INPUT", "An existing .pdf file is required.")
    return root, relative


def open_input(pdf_path: Path, import_root: Path, source: SourceSpec, limits: Limits) -> tuple[int, Path]:
    """Open through directory descriptors: symlink and replacement races fail closed.

    This never invokes a PDF parser. The returned descriptor is the authority for
    the subsequent private copy, rather than reopening a previously checked path.
    """
    check_source_rights(source)
    root, relative = _relative_path(pdf_path, import_root)
    directory_fd = None
    file_fd = None
    try:
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in relative.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise IngestError("INVALID_INPUT", "The input must be a regular PDF file.")
        if metadata.st_size <= 0:
            raise IngestError("INVALID_INPUT", "The PDF is empty.")
        if metadata.st_size > limits.max_file_bytes:
            raise IngestError("FILE_LIMIT", "The PDF exceeds the configured file-size limit.")
        return file_fd, root / relative
    except IngestError:
        if file_fd is not None:
            os.close(file_fd)
        raise
    except OSError:
        if file_fd is not None:
            os.close(file_fd)
        raise IngestError("PATH_DENIED", "The PDF is unavailable or contains a symlink.") from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def validate_input_path(pdf_path: Path, import_root: Path, source: SourceSpec, limits: Limits) -> Path:
    descriptor, validated = open_input(pdf_path, import_root, source, limits)
    os.close(descriptor)
    return validated


@contextmanager
def page_deadline(seconds: float):
    """Worker main-thread deadline, including parsing and native diagnostics."""
    watchdog_fd = os.environ.get("PDF_INGEST_PAGE_WATCHDOG_FD")
    if watchdog_fd is not None:
        os.write(int(watchdog_fd), b"page\n")
    def expired(_signum, _frame):
        raise IngestError("DEADLINE_EXCEEDED", "The per-page processing deadline was exceeded.")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def preflight(pdf_path: Path, import_root: Path, source: SourceSpec, limits: Limits,
              page_range: tuple[int, int] | None = None) -> PreflightResult:
    """Call only inside the isolated worker after parent scope/rights checks."""
    validated = validate_input_path(pdf_path, import_root, source, limits)
    try:
        with validated.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise IngestError("INVALID_INPUT", "The input does not have a PDF signature.")
            stream.seek(0)
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if source.expected_source_sha256 is not None and digest != source.expected_source_sha256:
            raise IngestError("HASH_MISMATCH", "The PDF does not match the registered source-file hash.")
        from pypdf import PdfReader
        with page_deadline(limits.page_timeout_seconds):
            reader = PdfReader(str(validated), strict=True)
            if reader.is_encrypted:
                raise IngestError("ENCRYPTED_SOURCE", "Authorized local decryption is required before conversion.")
            count = len(reader.pages)
        if count < 1:
            raise IngestError("PARSE_FAILED", "The PDF has no physical pages.")
        if count > limits.max_pages:
            raise IngestError("PAGE_LIMIT", "The PDF exceeds the configured physical-page limit.")
        start, end = page_range or (1, count)
        if start < 1 or end < start or end > count:
            raise IngestError("INVALID_INPUT", "The requested page range is outside the source.")
        if end - start + 1 > limits.max_range_pages:
            raise IngestError("PAGE_LIMIT", "The requested range exceeds the configured range limit.")
        requested = list(range(start, end + 1))
        labels = None
        with page_deadline(limits.page_timeout_seconds):
            try:
                labels = reader.page_labels
            except (KeyError, ValueError, TypeError):
                pass
        inventory = []
        for index in requested:
            with page_deadline(limits.page_timeout_seconds):
                page = reader.pages[index - 1]
                width, height = float(page.mediabox.width), float(page.mediabox.height)
                rotation = int(page.rotation or 0) % 360
                if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                    raise IngestError("INVALID_PROVENANCE", "A page has invalid dimensions.")
                if rotation not in (0, 90, 180, 270):
                    raise IngestError("INVALID_PROVENANCE", "A page has unsupported rotation.")
                try:
                    text = page.extract_text() or ""
                except (ValueError, TypeError, KeyError):
                    text = ""
                inventory.append(PageInventory(
                    pdf_page_index=index, width=width, height=height, rotation=rotation,
                    printed_label=str(labels[index - 1]) if labels and index <= len(labels) else None,
                    printed_label_verified=False, native_text_chars=len(text),
                    native_text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    status="unresolved", reason="Awaiting structured extraction and page accounting.",
                ))
        return PreflightResult(digest, count, requested, inventory, validated)
    except IngestError:
        raise
    except (MemoryError, OSError) as exc:
        if isinstance(exc, MemoryError) or getattr(exc, "errno", None) in (12, 24, 27):
            raise IngestError("RESOURCE_LIMIT", "PDF diagnostics exceeded a worker resource limit.") from None
        raise IngestError("PARSE_FAILED", "The PDF could not be parsed.") from None
    except Exception:
        raise IngestError("PARSE_FAILED", "The PDF could not be parsed.") from None
