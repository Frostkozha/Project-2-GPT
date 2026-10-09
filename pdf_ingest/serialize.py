"""Safe Markdown derivatives and atomic, independently verifiable bundles."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import string
import tempfile

from .normalize import NORMALIZATION_VERSION, normalize_text
from .provenance import text_hash
from .schema import (
    BLOCK_ADAPTER, Block, Correction, IngestError, Manifest, MarkdownSpan,
    ReviewReport, TableBlock, table_text,
)

SERIALIZATION_VERSION = "markdown-v1"
REQUIRED_FILES = frozenset({"document.md", "blocks.jsonl", "manifest.json", "review_report.json"})
HASHED_FILES = frozenset({"document.md", "blocks.jsonl", "review_report.json", "corrections.jsonl"})
_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024


def escape_markdown(text: str) -> str:
    """Render source text literally: HTML, images and links cannot become active."""
    entities = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}
    return "".join(entities.get(c, "\\" + c if c in string.punctuation else c) for c in text)


def _comment(text: str) -> str:
    # Escaping the closing angle bracket also prevents source-supplied -->.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", " ").replace("\n", " ")


def _table_lines(block: TableBlock) -> list[str]:
    table = block.table
    grid = [["" for _ in range(table.columns)] for _ in range(table.rows)]
    for cell in table.cells:
        grid[cell.row][cell.column] = escape_markdown(cell.text).replace("\n", "<br>")
    def row(cells):
        return "| " + " | ".join(cells) + " |"
    first_is_header = any(c.row == 0 and c.column_header for c in table.cells)
    lines = []
    if any(c.row_span > 1 or c.column_span > 1 for c in table.cells):
        lines.append("Merged table: review display; canonical cell spans are retained in blocks.jsonl.")
        lines.append("")
    lines.append(row(grid[0] if first_is_header else [""] * table.columns))
    lines.append(row(["---"] * table.columns))
    lines.extend(row(cells) for cells in (grid[1:] if first_is_header else grid))
    if table.footnotes:
        lines.append("")
        for footnote in table.footnotes:
            lines.extend(escape_markdown(footnote).split("\n"))
    if table.approved_representation is not None:
        lines.extend(["", "Approved structured-text representation:", ""])
        lines.extend(escape_markdown(table.approved_representation).split("\n"))
    return lines


def render_markdown(manifest: Manifest, blocks: list[Block]) -> str:
    """Render only canonical records and recompute inclusive content line spans."""
    inventory = {p.pdf_page_index: p for p in manifest.page_inventory}
    lines = [
        f"<!-- source-id: {_comment(manifest.source.source_id)}; source-version: {_comment(manifest.source.source_version)} -->",
        f"<!-- conversion-id: {_comment(manifest.conversion_id)} -->",
        "", "# " + escape_markdown(manifest.source.title), "",
    ]
    seen_pages: set[int] = set()
    current_page: int | None = None

    def add_page(page_number: int):
        page = inventory.get(page_number)
        label = page.printed_label if page and page.printed_label_verified else "unverified"
        lines.append(f"<!-- pdf-page: {page_number}; printed-label: {_comment(label or 'unverified')} -->")
        lines.append("")
        seen_pages.add(page_number)

    for block in blocks:
        page_number = block.origins[0].pdf_page_index
        if page_number != current_page:
            add_page(page_number)
            current_page = page_number
        if block.extraction_quality == "excluded":
            block.markdown_span = None
            lines.extend([f"<!-- excluded-block-id: {_comment(block.block_id)} -->", ""])
            continue
        lines.append(f"<!-- block-id: {_comment(block.block_id)} -->")
        if len(block.origins) > 1:
            pages = ",".join(str(o.pdf_page_index) for o in block.origins)
            lines.append(f"<!-- origin-pages: {pages} -->")
        if block.kind == "table":
            content = _table_lines(block)
        else:
            prefix = "#" * block.heading.level + " " if block.kind == "heading" else ""
            content = (prefix + escape_markdown(block.text_normalized)).split("\n")
        start = len(lines) + 1
        lines.extend(content)
        block.markdown_span = MarkdownSpan(start_line=start, end_line=len(lines))
        lines.append("")
        seen_pages.update(o.pdf_page_index for o in block.origins)
    for page_number in manifest.requested_pages:
        if page_number not in seen_pages:
            add_page(page_number)
            page = inventory.get(page_number)
            lines.extend([f"<!-- page-status: {_comment(page.status if page else 'unresolved')} -->", ""])
    return "\n".join(lines) + "\n"


def _json_bytes(model) -> bytes:
    return (json.dumps(model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                       indent=2, allow_nan=False) + "\n").encode("utf-8")


def _jsonl_bytes(records) -> bytes:
    return "".join(json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False) + "\n" for record in records).encode("utf-8")


def _read_bytes(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_ARTIFACT_BYTES:
                raise IngestError("INVALID_INPUT", "bundle artifact is unsupported or too large")
            return stream.read(_MAX_ARTIFACT_BYTES + 1)
    except IngestError:
        raise
    except OSError as exc:
        raise IngestError("INVALID_INPUT", "bundle artifact is missing or unsafe") from exc


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _parse_json(payload: bytes):
    return json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON value")))


def _validate_json(model, payload: bytes):
    _parse_json(payload)  # Explicit duplicate-key/NaN rejection before Pydantic.
    return model.model_validate_json(payload)


def _read_jsonl(path: Path, adapter) -> list:
    records = []
    for line in _read_bytes(path).splitlines():
        if not line.strip():
            raise ValueError("blank JSONL record")
        _parse_json(line)
        records.append(adapter.validate_json(line) if hasattr(adapter, "validate_json") else adapter.model_validate_json(line))
    return records


def read_corrections(bundle: Path) -> list[Correction]:
    path = Path(bundle) / "corrections.jsonl"
    if not path.exists() and not path.is_symlink():
        return []
    try:
        return _read_jsonl(path, Correction)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise IngestError("INVALID_INPUT", "correction history is malformed") from exc


def load_bundle(bundle: Path) -> tuple[Manifest, list[Block], ReviewReport]:
    """Load strict schemas; use validate_bundle before trusting or importing."""
    bundle = Path(bundle)
    if bundle.is_symlink() or not bundle.is_dir():
        raise IngestError("INVALID_INPUT", "bundle must be a local directory")
    try:
        manifest = _validate_json(Manifest, _read_bytes(bundle / "manifest.json"))
        blocks = _read_jsonl(bundle / "blocks.jsonl", BLOCK_ADAPTER)
        report = _validate_json(ReviewReport, _read_bytes(bundle / "review_report.json"))
        return manifest, blocks, report
    except IngestError:
        raise
    except (ValueError, TypeError, UnicodeError) as exc:
        raise IngestError("INVALID_INPUT", "bundle records do not match the strict schema") from exc


def _integrity(manifest: Manifest, blocks: list[Block], report: ReviewReport, corrections: list[Correction]):
    def fail(message):
        return IngestError("INVALID_PROVENANCE", message)
    if manifest.job_status != "completed":
        raise fail("only completed extraction bundles are usable")
    if manifest.normalization_version != NORMALIZATION_VERSION or manifest.serialization_version != SERIALIZATION_VERSION:
        raise fail("unsupported normalization or serialization version")
    ids = [b.block_id for b in blocks]
    if len(set(ids)) != len(ids):
        raise fail("block IDs are not unique")
    by_id = {b.block_id: b for b in blocks}
    pages = [p.pdf_page_index for p in manifest.page_inventory]
    if len(set(pages)) != len(pages) or set(pages) != set(manifest.requested_pages):
        raise fail("every requested source page must be inventoried exactly once")
    inventory = {p.pdf_page_index: p for p in manifest.page_inventory}
    issue_ids = [i.issue_id for i in report.issues]
    if len(set(issue_ids)) != len(issue_ids):
        raise fail("issue IDs are not unique")
    issue_map = {i.issue_id: i for i in report.issues}
    correction_ids = [c.correction_id for c in corrections]
    if len(set(correction_ids)) != len(correction_ids):
        raise fail("correction IDs are not unique")
    for issue in report.issues:
        if not set(issue.block_ids) <= set(by_id) or not set(issue.pdf_page_indices) <= set(inventory):
            raise fail("issue references an unknown block or source page")
        if not set(issue.correction_ids) <= set(correction_ids):
            raise fail("issue references an unknown correction")
        for block_id in issue.block_ids:
            if issue.issue_id not in by_id[block_id].issues:
                raise fail("issue and block references must be reciprocal")
    expected_pages = {page: set() for page in inventory}
    missing_box_categories = {"MISSING_BBOX", "MISSING_BOUNDING_BOX", "INVALID_PROVENANCE", "PROVENANCE_MISSING"}
    for block in blocks:
        if block.source_id != manifest.source.source_id or block.source_version != manifest.source.source_version:
            raise fail("block source identity does not match the manifest")
        if text_hash(block.text_normalized) != block.text_sha256:
            raise IngestError("HASH_MISMATCH", "canonical text hash does not match")
        if normalize_text(block.text_normalized)[0] != block.text_normalized:
            raise fail("canonical text must use NFC Unicode and LF line endings")
        profile_method = "native_layout" if manifest.profile == "native_layout_v1" else "english_ocr"
        if block.extraction_method not in {profile_method, "reviewed_transcription"}:
            raise fail("block extraction method does not match the selected profile")
        if block.kind == "table" and block.text_normalized != table_text(block.table):
            raise fail("canonical table text does not match its structured representation")
        if not set(block.issues) <= set(issue_map):
            raise fail("block references an unknown issue")
        for issue_id in block.issues:
            if block.block_id not in issue_map[issue_id].block_ids:
                raise fail("block and issue references must be reciprocal")
        for origin in block.origins:
            if origin.pdf_page_index not in inventory:
                raise fail("block origin is outside the requested source pages")
            page = inventory[origin.pdf_page_index]
            if origin.printed_label is not None and (not page.printed_label_verified or origin.printed_label != page.printed_label):
                raise fail("origin page label lacks a verified source mapping")
            if origin.bbox_top_left_normalized is None and not any(issue_map[i].category in missing_box_categories for i in block.issues):
                raise fail("missing block location requires a specific review issue")
            expected_pages[origin.pdf_page_index].add(block.block_id)
    for page in manifest.page_inventory:
        if set(page.block_ids) != expected_pages[page.pdf_page_index]:
            raise fail("page inventory and block origins disagree")
        if page.status == "extracted" and not page.block_ids:
            raise fail("extracted page must reference extracted blocks")
        if page.status == "confirmed_blank" and page.block_ids:
            raise fail("confirmed blank page cannot contain extracted blocks")
        if page.status == "unresolved" and not any(page.pdf_page_index in i.pdf_page_indices for i in report.issues):
            raise fail("unresolved page requires a review issue")
    for approval in report.approvals:
        if not set(approval.block_ids) <= set(by_id) or not set(approval.sampled_pages) <= set(inventory):
            raise fail("approval references unknown blocks or pages")
    if manifest.review_state == "approved":
        if not report.approvals or any(i.resolution is None for i in report.issues):
            raise fail("approved bundle requires review approvals and resolved issues")
        if any(p.status == "unresolved" for p in manifest.page_inventory):
            raise fail("approved bundle cannot have unresolved source pages")


def validate_bundle(bundle: Path) -> tuple[Manifest, list[Block], ReviewReport]:
    """Verify the whole bundle, including exact Markdown rendering and mappings."""
    bundle = Path(bundle)
    manifest, blocks, report = load_bundle(bundle)
    expected = set(REQUIRED_FILES) - {"manifest.json"}
    if (bundle / "corrections.jsonl").exists() or (bundle / "corrections.jsonl").is_symlink():
        expected.add("corrections.jsonl")
    if set(manifest.output_hashes) != expected:
        raise IngestError("HASH_MISMATCH", "manifest must hash exactly the canonical derivative files")
    for name, digest in manifest.output_hashes.items():
        if hashlib.sha256(_read_bytes(bundle / name)).hexdigest() != digest:
            raise IngestError("HASH_MISMATCH", "bundle derivative hash does not match")
    corrections = read_corrections(bundle)
    _integrity(manifest, blocks, report, corrections)
    recorded_spans = [b.markdown_span.model_dump() if b.markdown_span else None for b in blocks]
    rendered = render_markdown(manifest, blocks).encode("utf-8")
    actual_spans = [b.markdown_span.model_dump() if b.markdown_span else None for b in blocks]
    if recorded_spans != actual_spans:
        raise IngestError("INVALID_PROVENANCE", "Markdown spans do not match the rendered canonical blocks")
    if _read_bytes(bundle / "document.md") != rendered:
        raise IngestError("HASH_MISMATCH", "Markdown is not the deterministic canonical rendering")
    return manifest, blocks, report


def _write_file(path: Path, payload: bytes):
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _stage(parent: Path, manifest: Manifest, blocks: list[Block], report: ReviewReport,
           corrections: list[Correction] | None) -> Path:
    stage = Path(tempfile.mkdtemp(prefix=".bundle-staging-", dir=parent))
    try:
        # Round-trip all records to catch callers bypassing assignment validation.
        _validate_json(Manifest, _json_bytes(manifest))
        _validate_json(ReviewReport, _json_bytes(report))
        for block in blocks:
            block.text_sha256 = text_hash(block.text_normalized)
            BLOCK_ADAPTER.validate_json(block.model_dump_json())
        for correction in corrections or []:
            _validate_json(Correction, _json_bytes(correction))
        rendered = render_markdown(manifest, blocks).encode("utf-8")
        payloads = {"document.md": rendered, "blocks.jsonl": _jsonl_bytes(blocks), "review_report.json": _json_bytes(report)}
        if corrections:
            payloads["corrections.jsonl"] = _jsonl_bytes(corrections)
        manifest.output_hashes = {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()}
        for name, payload in payloads.items():
            _write_file(stage / name, payload)
        _write_file(stage / "manifest.json", _json_bytes(manifest))
        validate_bundle(stage)
        return stage
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _rename_atomic(source: Path, destination: Path, exchange: bool = False):
    """Linux renameat2 gives atomic no-overwrite publication and replacement."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise IngestError("RESOURCE_LIMIT", "atomic bundle publication requires Linux renameat2")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    flag = 2 if exchange else 1  # RENAME_EXCHANGE / RENAME_NOREPLACE
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), flag) != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError("bundle destination already exists")
        raise OSError(error, "atomic bundle publication failed")
    descriptor = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_bundle(destination: Path, manifest: Manifest, blocks: list[Block], report: ReviewReport,
                 corrections: list[Correction] | None = None) -> Path:
    """Validate in a private staging directory, then publish once atomically."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("bundle destination already exists")
    stage = _stage(destination.parent, manifest, blocks, report, corrections)
    try:
        _rename_atomic(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return destination


def rewrite_bundle(bundle: Path, manifest: Manifest, blocks: list[Block], report: ReviewReport,
                   corrections: list[Correction] | None = None) -> Path:
    """Replace a reviewed bundle atomically, preserving correction history.

    Corrections to canonical source text must use a new conversion directory;
    this helper serves recorded review resolutions and approvals.
    """
    bundle = Path(bundle)
    old_manifest, old_blocks, old_report = validate_bundle(bundle)
    if manifest.conversion_id != old_manifest.conversion_id or manifest.source != old_manifest.source:
        raise IngestError("INVALID_PROVENANCE", "review rewrite cannot change conversion or source identity")
    old_corrections = read_corrections(bundle)
    if corrections is None:
        corrections = old_corrections
    elif corrections[:len(old_corrections)] != old_corrections:
        raise IngestError("INVALID_PROVENANCE", "correction history must remain append-only")
    # Approval is a review-state operation; changing evidence needs a new version.
    block_review_fields = {"extraction_quality", "index_candidate", "markdown_span", "issues"}
    if [b.model_dump(exclude=block_review_fields) for b in blocks] != [b.model_dump(exclude=block_review_fields) for b in old_blocks]:
        raise IngestError("INVALID_PROVENANCE", "canonical block changes require a new conversion version")
    manifest_review_fields = {"review_state", "approval_references", "output_hashes", "page_inventory"}
    if manifest.model_dump(exclude=manifest_review_fields) != old_manifest.model_dump(exclude=manifest_review_fields):
        raise IngestError("INVALID_PROVENANCE", "extraction manifest changes require a new conversion version")
    if [p.model_dump(exclude={"status", "reason"}) for p in manifest.page_inventory] != [p.model_dump(exclude={"status", "reason"}) for p in old_manifest.page_inventory]:
        raise IngestError("INVALID_PROVENANCE", "source page provenance changes require a new conversion version")
    resolution_fields = {"resolution", "reviewer", "resolved_at"}
    if [i.model_dump(exclude=resolution_fields) for i in report.issues] != [i.model_dump(exclude=resolution_fields) for i in old_report.issues]:
        raise IngestError("INVALID_PROVENANCE", "extraction issue changes require a new conversion version")
    stage = _stage(bundle.parent, manifest, blocks, report, corrections)
    try:
        preview = bundle / "previews"
        if preview.exists():
            if preview.is_symlink() or any(path.is_symlink() for path in preview.rglob("*")):
                raise IngestError("INVALID_INPUT", "local previews must not contain symlinks")
            shutil.copytree(preview, stage / "previews")
        _rename_atomic(stage, bundle, exchange=True)
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return bundle
