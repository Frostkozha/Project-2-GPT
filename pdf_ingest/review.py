"""Human review, immutable correction versions, and fail-closed import policy.

The caller supplies the current source registry record. This module does not
grant rights, content approval, or activate an index.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Iterator
import uuid

from .normalize import normalize_text
from .provenance import stable_block_id, text_hash
from .schema import (
    ApprovalRecord,
    Correction,
    IngestError,
    Issue,
    SourceSpec,
    TablePayload,
    table_text,
)
from .serialize import read_corrections, rewrite_bundle, validate_bundle, write_bundle


EVIDENCE_KINDS = frozenset({"heading", "paragraph", "list", "caption", "table"})
NON_EVIDENCE_KINDS = frozenset({"formula", "figure", "furniture", "placeholder"})
_SOURCE_IDENTITY = (
    "tenant_id", "source_id", "source_version", "title", "assignment",
    "rights_reference", "synthetic",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required(value: str | None, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IngestError("REVIEW_REQUIRED", f"A nonempty {field} is required.")
    return value


def _fail(code: str, message: str) -> None:
    raise IngestError(code, message)


@contextmanager
def _review_lock(bundle: Path) -> Iterator[None]:
    """Coordinate writers using a lock that survives bundle replacement."""
    import fcntl

    bundle = Path(bundle)
    if bundle.is_symlink():
        _fail("INVALID_INPUT", "Review cannot mutate a symlinked bundle.")
    locks = bundle.parent / ".review-locks"
    locks.mkdir(exist_ok=True, mode=0o700)
    name = hashlib.sha256(str(bundle.absolute()).encode()).hexdigest() + ".lock"
    with (locks / name).open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _corrections(bundle: Path) -> list[Correction]:
    return read_corrections(bundle)


def _validate_current_source(manifest, current_source: SourceSpec) -> str:
    current = SourceSpec.model_validate(current_source.model_dump())
    if any(getattr(current, key) != getattr(manifest.source, key) for key in _SOURCE_IDENTITY):
        _fail("SOURCE_MISMATCH", "The current source identity or registered metadata differs from this bundle.")
    if current.expected_source_sha256 is None or current.expected_source_sha256 != manifest.source_sha256:
        _fail("HASH_MISMATCH", "Approval and import require a trusted current source-register SHA-256 matching this bundle's source-file hash.")
    if not current.eligible:
        _fail("SOURCE_INELIGIBLE", "The current source is ineligible or revoked.")
    if not current.rights_confirmed:
        _fail("RIGHTS_UNCONFIRMED", "Current source processing and excerpt rights are not confirmed.")
    if not current.content_approved:
        _fail("CONTENT_UNAPPROVED", "Current faculty content approval is required for import.")
    return _required(current.content_approval_reference, "content approval reference")


def _issue_block_ids(issue, blocks) -> set[str]:
    if issue.block_ids:
        return set(issue.block_ids)
    pages = set(issue.pdf_page_indices)
    return {block.block_id for block in blocks if any(origin.pdf_page_index in pages for origin in block.origins)}


def _flagged_ids(report, blocks) -> set[str]:
    return set().union(*(_issue_block_ids(issue, blocks) for issue in report.issues)) if report.issues else set()


def _check_issue_audits(report) -> None:
    for issue in report.issues:
        if issue.resolution not in {"approved", "excluded"}:
            _fail("UNRESOLVED_ISSUES", "Every issue must have an individual reviewer resolution.")
        _required(issue.reviewer, "issue reviewer")
        _required(issue.resolved_at, "issue resolution timestamp")


def _page_types(manifest, blocks) -> dict[int, set[str]]:
    types: dict[int, set[str]] = {}
    for page in manifest.page_inventory:
        categories = {"status:" + page.status}
        categories.add("native-text" if page.native_text_chars else "no-native-text")
        if page.rotation % 360:
            categories.add("rotated")
        types[page.pdf_page_index] = categories
    for block in blocks:
        for origin in block.origins:
            categories = types[origin.pdf_page_index]
            categories.add("kind:" + block.kind)
            categories.add("method:" + block.extraction_method)
            if block.kind == "table":
                categories.add("merged-table" if _is_merged_table(block) else "rectangular-table")
    return types


def _check_sample(manifest, blocks, sampled_pages: list[int]) -> None:
    if len(sampled_pages) != len(set(sampled_pages)):
        _fail("INVALID_REVIEW_SCOPE", "Sampled page indices must be unique.")
    requested = set(manifest.requested_pages)
    sampled = set(sampled_pages)
    if not sampled <= requested or any(type(page) is not int for page in sampled_pages):
        _fail("INVALID_REVIEW_SCOPE", "Sampled pages must be requested physical PDF indices.")
    if len(sampled) < min(20, len(requested)):
        _fail("INSUFFICIENT_REVIEW_SAMPLE", "Sample at least 20 requested pages, or every requested page when fewer than 20.")
    types = _page_types(manifest, blocks)
    required_types = set().union(*(types[page] for page in requested))
    seen_types = set().union(*(types[page] for page in sampled)) if sampled else set()
    if not required_types <= seen_types:
        _fail("INSUFFICIENT_REVIEW_SAMPLE", "The visual sample must represent every source page type, including table/OCR/rotated and non-text pages.")


def _is_merged_table(block) -> bool:
    return block.kind == "table" and any(cell.row_span > 1 or cell.column_span > 1 for cell in block.table.cells)


def _check_exclusions(manifest, blocks, report, subset_reference: str | None) -> None:
    omitted = {block.block_id for block in blocks if block.kind in EVIDENCE_KINDS and not block.index_candidate}
    excluded_pages = {page.pdf_page_index for page in manifest.page_inventory if page.status == "excluded"}
    # The manifest already enforces unique, ordered in-range positive indices.
    # Equal counts therefore mean full coverage, without allocating a possibly
    # enormous list from an untrusted page-count field.
    partial_page_range = len(manifest.requested_pages) != manifest.source_page_count
    if partial_page_range or omitted or excluded_pages or any(issue.resolution == "excluded" for issue in report.issues):
        _required(subset_reference, "faculty permitted subset reference documenting omissions")
    individually_excluded = set().union(*(
        _issue_block_ids(issue, blocks) for issue in report.issues if issue.resolution == "excluded"
    )) if report.issues else set()
    for block in blocks:
        if block.block_id in omitted and (block.extraction_quality != "excluded" or block.block_id not in individually_excluded):
            _fail("UNREVIEWED_EXCLUSION", "Evidence omissions require an individual reviewed exclusion.")
        if block.kind in EVIDENCE_KINDS and block.index_candidate and (block.extraction_quality == "excluded" or block.block_id in individually_excluded):
            _fail("UNREVIEWED_EXCLUSION", "Reviewed excluded evidence cannot remain an import candidate.")
        if block.kind in EVIDENCE_KINDS and block.index_candidate and any(origin.pdf_page_index in excluded_pages for origin in block.origins):
            _fail("UNREVIEWED_EXCLUSION", "An excluded page cannot retain candidate evidence.")


def _check_pages(manifest) -> None:
    for page in manifest.page_inventory:
        if page.status == "unresolved":
            _fail("PAGE_UNACCOUNTED", "Every requested page must be reconciled before source approval.")
        if page.status == "excluded" and not page.reason:
            _fail("UNREVIEWED_EXCLUSION", "An excluded page needs a recorded reason.")


def _check_table_representations(blocks) -> None:
    for block in blocks:
        if block.kind in EVIDENCE_KINDS and block.index_candidate and not block.text_normalized.strip():
            _fail("UNRESOLVED_CONTENT", "Empty candidate text cannot be approved as source evidence.")
        if block.index_candidate and _is_merged_table(block):
            if not (block.table.approved_representation or "").strip():
                _fail("TABLE_STRUCTURE_UNRESOLVED", "A reviewed deterministic merged-table representation is required.")
            if block.text_normalized != table_text(block.table):
                _fail("TABLE_STRUCTURE_UNRESOLVED", "The canonical table text differs from its approved representation.")


def _validated_coverage(manifest, blocks, report, content_reference: str) -> set[str]:
    known = {block.block_id for block in blocks}
    candidates = {block.block_id for block in blocks if block.kind in EVIDENCE_KINDS and block.index_candidate}
    flagged = _flagged_ids(report, blocks)
    covered: set[str] = set()
    subset_references = set()
    for approval in report.approvals:
        _required(approval.reviewer, "approval reviewer")
        _required(approval.timestamp, "approval timestamp")
        if approval.content_approval_reference != content_reference:
            _fail("STALE_CONTENT_APPROVAL", "A review approval refers to a different current faculty content approval.")
        if len(approval.block_ids) != len(set(approval.block_ids)) or not set(approval.block_ids) <= known:
            _fail("INVALID_REVIEW_SCOPE", "Approval scope contains unknown or duplicate block IDs.")
        scope = set(approval.block_ids)
        if not scope <= candidates:
            _fail("INVALID_REVIEW_SCOPE", "Approval scope must contain only candidate evidence blocks.")
        if approval.method == "source_level_sampled":
            _check_sample(manifest, blocks, approval.sampled_pages)
            if scope & flagged:
                _fail("INVALID_REVIEW_SCOPE", "Flagged blocks require direct review and cannot be covered by sampling.")
        elif approval.method != "direct":
            _fail("INVALID_REVIEW_SCOPE", "Unknown review method.")
        if approval.permitted_subset_reference:
            subset_references.add(approval.permitted_subset_reference)
        covered |= scope
    if len(subset_references) > 1:
        _fail("INVALID_REVIEW_SCOPE", "Current approvals disagree about the faculty-permitted source subset.")
    _check_exclusions(manifest, blocks, report, next(iter(subset_references), None))
    if not candidates <= covered:
        _fail("INCOMPLETE_REVIEW_SCOPE", "Every candidate evidence block needs current approval coverage.")
    return covered


def assert_importable(bundle: Path, current_source: SourceSpec):
    """Validate files and the current review/permissions gate; return canonical models."""
    manifest, blocks, report = validate_bundle(Path(bundle))
    reference = _validate_current_source(manifest, current_source)
    if manifest.job_status != "completed" or manifest.review_state != "approved":
        _fail("BUNDLE_UNAPPROVED", "Only completed, reviewed and approved bundles may be imported.")
    _check_issue_audits(report)
    _check_pages(manifest)
    _check_table_representations(blocks)
    covered = _validated_coverage(manifest, blocks, report, reference)
    required_references = {reference} | {record.permitted_subset_reference for record in report.approvals if record.permitted_subset_reference}
    if not required_references <= set(manifest.approval_references) or not report.approvals:
        _fail("INVALID_REVIEW_SCOPE", "A source-level content sign-off reference is missing.")
    for block in blocks:
        if block.kind in NON_EVIDENCE_KINDS and block.index_candidate:
            _fail("NON_EVIDENCE_CANDIDATE", "Non-evidence regions cannot be candidate passages.")
        if block.block_id in covered and block.extraction_quality != "approved":
            _fail("BUNDLE_UNAPPROVED", "A candidate block lacks approved extraction quality.")
    return manifest, blocks, report


def review_status(bundle: Path) -> dict:
    manifest, blocks, report = validate_bundle(Path(bundle))
    return {
        "conversion_id": manifest.conversion_id,
        "review_state": manifest.review_state,
        "job_status": manifest.job_status,
        "requested_page_count": len(manifest.requested_pages),
        "candidate_blocks": sum(block.index_candidate and block.kind in EVIDENCE_KINDS for block in blocks),
        "approved_blocks": sum(block.extraction_quality == "approved" for block in blocks),
        "excluded_blocks": sum(block.extraction_quality == "excluded" for block in blocks),
        "unresolved_issues": [issue.issue_id for issue in report.issues if issue.resolution is None],
        "unresolved_pages": [page.pdf_page_index for page in manifest.page_inventory if page.status == "unresolved"],
        "approvals": [approval.model_dump(mode="json") for approval in report.approvals],
    }


def resolve_issue(bundle: Path, issue_id: str, resolution: str, reviewer: str) -> None:
    _required(reviewer, "reviewer")
    if resolution not in {"approved", "excluded"}:
        _fail("INVALID_INPUT", "Issue resolution must be approved or excluded.")
    with _review_lock(Path(bundle)):
        manifest, blocks, report = validate_bundle(Path(bundle))
        issue = next((item for item in report.issues if item.issue_id == issue_id), None)
        if issue is None:
            _fail("INVALID_INPUT", "Unknown issue ID.")
        resolved = issue.model_copy(update={"resolution": resolution, "reviewer": reviewer, "resolved_at": _now()})
        report.issues = [resolved if item.issue_id == issue_id else item for item in report.issues]
        issue = resolved
        # A changed review decision always invalidates prior scope approvals.
        report.approvals = []
        manifest.approval_references = []
        manifest.review_state = "needs_review"
        affected = _issue_block_ids(issue, blocks)
        if resolution == "excluded":
            if not affected and not issue.pdf_page_indices:
                _fail("INVALID_REVIEW_SCOPE", "Exclusion must identify affected blocks or physical pages.")
            for block in blocks:
                if block.block_id in affected:
                    block.index_candidate = False
                    block.extraction_quality = "excluded"
            for page in manifest.page_inventory:
                if page.pdf_page_index in issue.pdf_page_indices:
                    remaining = any(block.index_candidate and block.kind in EVIDENCE_KINDS and any(
                        origin.pdf_page_index == page.pdf_page_index for origin in block.origins
                    ) for block in blocks)
                    if not remaining:
                        page.reason = "Reviewed exclusion: " + issue.issue_id
                        page.status = "excluded"
        else:
            still_excluded = set().union(*(
                _issue_block_ids(item, blocks) for item in report.issues if item.resolution == "excluded"
            )) if report.issues else set()
            for block in blocks:
                if block.block_id in affected and block.block_id not in still_excluded and block.kind in EVIDENCE_KINDS and block.extraction_quality == "excluded":
                    block.index_candidate = True
                    block.extraction_quality = "unreviewed"
            for page in manifest.page_inventory:
                if page.status == "excluded" and any(block.index_candidate and block.kind in EVIDENCE_KINDS and any(
                    origin.pdf_page_index == page.pdf_page_index for origin in block.origins
                ) for block in blocks):
                    page.status = "extracted"
                    page.reason = "Reviewed exclusion reversed by individual source review."
        for block in blocks:
            if block.extraction_quality == "approved":
                block.extraction_quality = "unreviewed"
        # Resolving an extraction warning cannot invent text for an empty page.
        for page in manifest.page_inventory:
            relevant = [item for item in report.issues if page.pdf_page_index in item.pdf_page_indices]
            if page.status == "unresolved" and relevant and all(item.resolution == "approved" for item in relevant):
                if any(block.index_candidate and block.kind in EVIDENCE_KINDS and any(
                    origin.pdf_page_index == page.pdf_page_index for origin in block.origins
                ) for block in blocks):
                    page.status = "extracted"
                    page.reason = "Extraction issues individually reconciled by review."
        rewrite_bundle(Path(bundle), manifest, blocks, report)


def approve_bundle(
    bundle: Path,
    current_source: SourceSpec,
    reviewer: str,
    method: str = "direct",
    block_ids: list[str] | None = None,
    sampled_pages: list[int] | None = None,
    permitted_subset_reference: str | None = None,
) -> None:
    _required(reviewer, "reviewer")
    if method not in {"direct", "source_level_sampled"}:
        _fail("INVALID_REVIEW_SCOPE", "Review method must be direct or source_level_sampled.")
    with _review_lock(Path(bundle)):
        manifest, blocks, report = validate_bundle(Path(bundle))
        reference = _validate_current_source(manifest, current_source)
        if manifest.job_status != "completed":
            _fail("BUNDLE_UNAPPROVED", "Only completed extractions may be reviewed for import.")
        _check_issue_audits(report)
        _check_pages(manifest)
        _check_exclusions(manifest, blocks, report, permitted_subset_reference)
        _check_table_representations(blocks)
        candidates = {block.block_id for block in blocks if block.kind in EVIDENCE_KINDS and block.index_candidate}
        if not candidates:
            _fail("NO_APPROVABLE_EVIDENCE", "This bundle has no candidate evidence to approve.")
        flagged = _flagged_ids(report, blocks) & candidates
        scope = list(block_ids) if block_ids is not None else sorted(candidates - flagged if method == "source_level_sampled" else candidates)
        report.approvals = [ApprovalRecord(
            reviewer=reviewer, timestamp=_now(), method=method, block_ids=scope,
            sampled_pages=list(sampled_pages or []), content_approval_reference=reference,
            permitted_subset_reference=permitted_subset_reference,
        )]
        if method == "source_level_sampled":
            # Issue resolution is the direct source comparison for flagged blocks.
            # Preserve its actual reviewer and timestamp rather than imply sampling
            # manually inspected those blocks.
            audits: dict[tuple[str, str], set[str]] = {}
            for issue in report.issues:
                if issue.resolution == "approved":
                    ids = _issue_block_ids(issue, blocks) & flagged
                    if ids:
                        audits.setdefault((issue.reviewer, issue.resolved_at), set()).update(ids)
            for (issue_reviewer, timestamp), ids in sorted(audits.items()):
                report.approvals.append(ApprovalRecord(
                    reviewer=issue_reviewer, timestamp=timestamp, method="direct",
                    block_ids=sorted(ids), sampled_pages=[], content_approval_reference=reference,
                    permitted_subset_reference=permitted_subset_reference,
                ))
        _validated_coverage(manifest, blocks, report, reference)
        for block in blocks:
            if block.kind in NON_EVIDENCE_KINDS:
                block.index_candidate = False
                block.extraction_quality = "excluded"
            elif block.block_id in candidates:
                block.extraction_quality = "approved"
        manifest.review_state = "approved"
        manifest.approval_references = [reference]
        if permitted_subset_reference:
            manifest.approval_references.append(permitted_subset_reference)
        rewrite_bundle(Path(bundle), manifest, blocks, report)


def _derive_version(manifest, blocks, report, change: dict) -> dict[str, str]:
    conversion_id = uuid.uuid4().hex
    previous_id = manifest.conversion_id
    old_fingerprint = manifest.conversion_fingerprint
    manifest.conversion_id = conversion_id
    manifest.parent_conversion_id = previous_id
    manifest.created_at = _now()
    manifest.conversion_fingerprint = hashlib.sha256(json.dumps({
        "parent": old_fingerprint, "conversion": conversion_id, "change": change,
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    manifest.idempotency_key = hashlib.sha256((manifest.idempotency_key + manifest.conversion_fingerprint).encode()).hexdigest()
    remap: dict[str, str] = {}
    for block in blocks:
        old_id = block.block_id
        block.block_id = stable_block_id(manifest.source, block.origins, old_id, block.text_normalized, manifest.conversion_fingerprint)
        remap[old_id] = block.block_id
        if block.extraction_quality != "excluded":
            block.extraction_quality = "unreviewed"
        block.markdown_span = None
    report.issues = [issue.model_copy(update={
        "block_ids": [remap[item] for item in issue.block_ids],
        "resolution": None, "reviewer": None, "resolved_at": None,
    }) for issue in report.issues]
    for page in manifest.page_inventory:
        page.block_ids = [remap[item] for item in page.block_ids]
    report.approvals = []
    manifest.approval_references = []
    manifest.review_state = "needs_review" if report.issues else "unreviewed"
    return remap


def _version_destination(output_root: Path, manifest) -> Path:
    return Path(output_root) / f"conversion_{manifest.source.source_id}_{manifest.source.source_version}_{manifest.conversion_id}"


def _new_correction_version(bundle: Path, block_id: str, new_text: str, reason: str, reviewer: str, output_root: Path, *, table: bool) -> Path:
    _required(reason, "correction reason")
    _required(reviewer, "reviewer")
    _required(new_text, "corrected source text")
    manifest, blocks, report = validate_bundle(Path(bundle))
    target = next((block for block in blocks if block.block_id == block_id), None)
    if target is None:
        _fail("INVALID_INPUT", "Unknown correction block ID.")
    if table != (target.kind == "table"):
        _fail("INVALID_INPUT", "Table corrections require the structured table-representation operation.")
    if target.kind in NON_EVIDENCE_KINDS:
        _fail("NON_EVIDENCE_CANDIDATE", "A non-evidence region cannot be converted into evidence by a text correction.")
    normalized, transformations = normalize_text(new_text)
    original_text = target.text_normalized
    if table:
        target.table.approved_representation = normalized
        target.text_normalized = table_text(target.table)
    else:
        target.text_normalized = normalized
    target.extraction_method = "reviewed_transcription"
    target.normalizations = transformations
    target.text_sha256 = text_hash(target.text_normalized)
    _derive_version(manifest, blocks, report, {
        "block": block_id, "text": target.text_normalized,
        "reason": reason, "reviewer": reviewer,
    })
    correction_id = uuid.uuid4().hex
    updated_issues = []
    for issue in report.issues:
        issue_blocks = list(issue.block_ids)
        correction_ids = list(issue.correction_ids)
        if target.block_id in issue_blocks:
            correction_ids.append(correction_id)
        updated_issues.append(issue.model_copy(update={
            "block_ids": issue_blocks, "resolution": None, "reviewer": None,
            "resolved_at": None, "correction_ids": correction_ids,
        }))
    report.issues = updated_issues
    history = _corrections(Path(bundle))
    history.append(Correction(
        correction_id=correction_id, block_id=target.block_id,
        original_text=original_text, new_text=target.text_normalized,
        reason=reason, reviewer=reviewer, timestamp=_now(),
    ))
    return write_bundle(_version_destination(output_root, manifest), manifest, blocks, report, corrections=history)


def correct_bundle(bundle: Path, block_id: str, new_text: str, reason: str, reviewer: str, output_root: Path) -> Path:
    """Create a new traceable version; never alter the original extraction text."""
    with _review_lock(Path(bundle)):
        return _new_correction_version(bundle, block_id, new_text, reason, reviewer, output_root, table=False)


def approve_table_representation(bundle: Path, block_id: str, representation: str, reason: str, reviewer: str, output_root: Path) -> Path:
    """Record a reviewed structured-text table representation as a new version.

    This does not approve the bundle. Cell spans/headers/footnotes and raw text
    remain available for comparison; issue resolution and source sign-off follow.
    """
    with _review_lock(Path(bundle)):
        return _new_correction_version(bundle, block_id, representation, reason, reviewer, output_root, table=True)


def structured_table_representation(table: TablePayload) -> str:
    """Generate reviewable cell/span/header text without flattening merged cells.

    This is a display/import proposal only. A reviewer must compare it with the
    rendered source and explicitly save it using approve_table_representation.
    JSON string escaping preserves cell boundaries even when source cells
    contain newlines, quotes or punctuation.
    """
    lines = [f"Table grid: {table.rows} rows; {table.columns} columns. Coordinates are zero-based."]
    for cell in sorted(table.cells, key=lambda value: (value.row, value.column)):
        lines.append("Cell: " + json.dumps(cell.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    for index, footnote in enumerate(table.footnotes, start=1):
        lines.append(f"Footnote {index}: " + json.dumps(footnote, ensure_ascii=False))
    return normalize_text("\n".join(lines))[0]


def reclassify_block(bundle: Path, block_id: str, kind: str, reason: str, reviewer: str, output_root: Path) -> Path:
    """Version a reviewed distinction between body text and page furniture."""
    _required(reason, "reclassification reason")
    _required(reviewer, "reviewer")
    if kind not in {"paragraph", "furniture"}:
        _fail("INVALID_INPUT", "Review reclassification supports paragraph or furniture only.")
    with _review_lock(Path(bundle)):
        manifest, blocks, report = validate_bundle(Path(bundle))
        target = next((block for block in blocks if block.block_id == block_id), None)
        if target is None or target.kind not in {"paragraph", "furniture"}:
            _fail("INVALID_INPUT", "Only original paragraph/furniture text can be reclassified by this operation.")
        old_kind = target.kind
        target.kind = kind
        target.index_candidate = kind == "paragraph"
        target.extraction_quality = "unreviewed" if kind == "paragraph" else "excluded"
        _derive_version(manifest, blocks, report, {
            "block": block_id, "old_kind": old_kind, "kind": kind,
            "reason": reason, "reviewer": reviewer,
        })
        audit_id = "classify-" + uuid.uuid4().hex
        report.issues.append(Issue(
            issue_id=audit_id, severity="info", category="BLOCK_RECLASSIFIED",
            pdf_page_indices=sorted({origin.pdf_page_index for origin in target.origins}),
            block_ids=[target.block_id],
            explanation=f"Reviewed block classification: {old_kind} -> {kind}. Reason: {reason}",
            resolution="approved", reviewer=reviewer, resolved_at=_now(),
        ))
        target.issues.append(audit_id)
        manifest.review_state = "needs_review"
        return write_bundle(_version_destination(output_root, manifest), manifest, blocks, report, corrections=_corrections(Path(bundle)))


def verify_page_label(bundle: Path, pdf_page_index: int, printed_label: str, reviewer: str, output_root: Path) -> Path:
    """Record an operator-verified physical-to-printed-page mapping in a new version."""
    _required(printed_label, "printed page label")
    _required(reviewer, "reviewer")
    with _review_lock(Path(bundle)):
        manifest, blocks, report = validate_bundle(Path(bundle))
        page = next((item for item in manifest.page_inventory if item.pdf_page_index == pdf_page_index), None)
        if page is None or type(pdf_page_index) is not int:
            _fail("INVALID_INPUT", "Page-label verification requires a requested physical PDF page.")
        old_label = page.printed_label
        old_verified = page.printed_label_verified
        page.printed_label = printed_label
        page.printed_label_verified = True
        for block in blocks:
            block.origins = [origin.model_copy(update={"printed_label": printed_label})
                if origin.pdf_page_index == pdf_page_index else origin for origin in block.origins]
        _derive_version(manifest, blocks, report, {
            "physical_page": pdf_page_index, "old_label": old_label,
            "old_label_verified": old_verified, "printed_label": printed_label,
            "reviewer": reviewer,
        })
        label_issue_id = "label-" + uuid.uuid4().hex
        report.issues.append(Issue(
            issue_id=label_issue_id,
            severity="info", category="PRINTED_LABEL_VERIFIED",
            pdf_page_indices=[pdf_page_index], block_ids=list(page.block_ids),
            explanation=f"Operator verified physical PDF page {pdf_page_index}: previous label {old_label!r} (verified={old_verified}); printed label {printed_label!r}.",
            resolution="approved", reviewer=reviewer, resolved_at=_now(),
        ))
        for block in blocks:
            if block.block_id in page.block_ids:
                block.issues.append(label_issue_id)
        manifest.review_state = "needs_review"
        return write_bundle(_version_destination(output_root, manifest), manifest, blocks, report, corrections=_corrections(Path(bundle)))
