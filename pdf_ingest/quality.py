"""Conservative, deterministic review signals; these are not accuracy scores."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from .schema import Block, Issue, PageInventory, TableBlock

# Preserve each numeric token/sign and medical negation in the comparison.
# Greek symbols, micro signs and superscripts have their own exact counts.
_CRITICAL = re.compile(r"(?:[−-]?\d+(?:[.,]\d+)*(?:[eE][+-]?\d+)?)|(?:<=|>=|≤|≥|[<>])|(?:\b(?:no|not|without|never|unless|mg|kg|µg|μg|mL|mmol|cm|mm|mol|IU)\b)|[α-ωΑ-Ωµμ⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]", re.IGNORECASE)
_BAD_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffd]")
_UNEXPECTED_SCRIPT = re.compile(r"[\u0400-\u052f\u0590-\u08ff\u0900-\u1fff\u3040-\u30ff\u3400-\u9fff]")


def issue_for(category: str, explanation: str, *, pages: list[int], blocks: list[str] | None = None, severity: str = "warning") -> Issue:
    refs = sorted(set(blocks or []))
    page_refs = sorted(set(pages))
    anchor = json.dumps([category, page_refs, refs, explanation], sort_keys=True, separators=(",", ":"))
    return Issue(issue_id="issue-" + hashlib.sha256(anchor.encode()).hexdigest()[:24], severity=severity, category=category, pdf_page_indices=page_refs, block_ids=refs, explanation=explanation)


def attach_issue(issues: list[Issue], blocks: list[Block], issue: Issue) -> None:
    if not any(existing.issue_id == issue.issue_id for existing in issues):
        issues.append(issue)
    for block in blocks:
        if block.block_id in issue.block_ids and issue.issue_id not in block.issues:
            block.issues.append(issue.issue_id)


def check_quality(blocks: list[Block], inventory: list[PageInventory], profile: str, native_text_by_page: dict[int, str] | None = None) -> list[Issue]:
    """Flag omissions and uncertain evidence without repairing extracted text.

    Native reference extraction is diagnostic only. Whitespace/order differences
    may cause review, and agreement never grants approval.
    """
    issues: list[Issue] = []
    for block in blocks:
        pages = sorted({origin.pdf_page_index for origin in block.origins})
        refs = [block.block_id]
        if any(origin.bbox_top_left_normalized is None for origin in block.origins):
            attach_issue(issues, blocks, issue_for("MISSING_BBOX", "The extractor did not supply a valid region locator; inspect the source page.", pages=pages, blocks=refs))
        if _BAD_CONTROL.search(block.text_normalized):
            block.extraction_quality = "unresolved"
            attach_issue(issues, blocks, issue_for("SYMBOL_UNCERTAIN", "Replacement or control characters suggest an unreliable text layer; verify symbols and wording against the page.", pages=pages, blocks=refs))
        if _UNEXPECTED_SCRIPT.search(block.text_normalized):
            attach_issue(issues, blocks, issue_for("UNEXPECTED_SCRIPT", "Unexpected script in an English source requires review; no translation was applied.", pages=pages, blocks=refs))
        if block.extraction_method == "english_ocr" and block.index_candidate:
            attach_issue(issues, blocks, issue_for("OCR_REVIEW_REQUIRED", "English OCR transcription requires individual source review, including numbers, symbols, units and negations.", pages=pages, blocks=refs))
        if isinstance(block, TableBlock):
            merged = any(cell.row_span > 1 or cell.column_span > 1 for cell in block.table.cells)
            coverage = sum(cell.row_span * cell.column_span for cell in block.table.cells)
            category = "TABLE_STRUCTURE_UNRESOLVED" if merged or coverage != block.table.rows * block.table.columns else "TABLE_REVIEW_REQUIRED"
            message = "Merged or incomplete table structure requires an approved deterministic structured-text representation." if category == "TABLE_STRUCTURE_UNRESOLVED" else "Verify table cell, header, unit and footnote associations against the source."
            attach_issue(issues, blocks, issue_for(category, message, pages=pages, blocks=refs))
        if block.kind in {"figure", "formula", "placeholder"}:
            block.index_candidate = False
            block.extraction_quality = "unresolved"
            attach_issue(issues, blocks, issue_for("UNRESOLVED_CONTENT", "This region is retained for source review and cannot become textual evidence without a separately reviewed transcription.", pages=pages, blocks=refs))
        if block.kind == "furniture":
            block.index_candidate = False
            attach_issue(issues, blocks, issue_for("FURNITURE_REVIEW_REQUIRED", "Docling classified this text as page furniture. Confirm the exclusion against the source; proximity to a margin alone does not justify removal.", pages=pages, blocks=refs))
    for page in inventory:
        on_page = [block for block in blocks if any(origin.pdf_page_index == page.pdf_page_index for origin in block.origins)]
        evidence = [block for block in on_page if block.index_candidate]
        refs = [block.block_id for block in on_page]
        text = "\n".join(block.text_normalized for block in on_page if block.kind not in {"figure", "placeholder"})
        if page.status == "unresolved":
            attach_issue(issues, blocks, issue_for("PAGE_UNACCOUNTED", "The page has visible content that the selected profile did not fully account for; inspect, explicitly exclude, or retry with an evaluated profile.", pages=[page.pdf_page_index], blocks=refs))
        if profile == "native_layout_v1" and page.native_text_chars == 0 and page.status not in {"confirmed_blank", "excluded"}:
            attach_issue(issues, blocks, issue_for("OCR_REQUIRED", "The native diagnostic found no text layer. This is a review signal, not confirmation of a scan or blank page; select English OCR explicitly when appropriate.", pages=[page.pdf_page_index], blocks=refs))
        if evidence and len(re.sub(r"\s", "", text)) < 20:
            attach_issue(issues, blocks, issue_for("SPARSE_TEXT", "Very little text was extracted; compare with the rendered page. Sparse text alone does not establish a blank page.", pages=[page.pdf_page_index], blocks=refs))
        reference = (native_text_by_page or {}).get(page.pdf_page_index)
        if reference is not None and profile == "native_layout_v1" and reference.strip():
            compact_source = re.sub(r"\s", "", reference)
            compact_extracted = re.sub(r"\s", "", text)
            if compact_source != compact_extracted:
                attach_issue(issues, blocks, issue_for("TEXT_LAYER_UNRELIABLE", "Native reference text and layout extraction disagree beyond whitespace. The reference is diagnostic only; verify omissions and reading order against the page.", pages=[page.pdf_page_index], blocks=refs))
            if Counter(_CRITICAL.findall(reference)) != Counter(_CRITICAL.findall(text)):
                attach_issue(issues, blocks, issue_for("SYMBOL_UNCERTAIN", "Exact counts of numbers, signs, units, Greek symbols or negations differ from the native diagnostic; individual verification is required.", pages=[page.pdf_page_index], blocks=refs))
        # Multiple disjoint horizontal columns are a review signal. Do not infer
        # that the model's ordering is accurate from bounding boxes alone.
        regions = [o.bbox_top_left_normalized for b in evidence for o in b.origins if o.pdf_page_index == page.pdf_page_index and o.bbox_top_left_normalized]
        left = [box for box in regions if box[2] < .55]
        right = [box for box in regions if box[0] > .45]
        if any(max(left_box[1], right_box[1]) < min(left_box[3], right_box[3]) for left_box in left for right_box in right):
            attach_issue(issues, blocks, issue_for("READING_ORDER_UNCERTAIN", "Side-by-side text regions suggest multiple columns; verify order and paragraph boundaries against the page.", pages=[page.pdf_page_index], blocks=[b.block_id for b in evidence]))
    return issues
