"""Score fixed, source-reviewed annotations against validated canonical bundles.

This measures extraction, never grants review approval. Character error rate uses
exact Unicode code points without case, whitespace, symbol, or numeric folding.
Failed and invalid outputs remain in denominators and reliability counts.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from .schema import (
    Block,
    IngestError,
    Manifest,
    Origin,
    ReviewReport,
    SourceId,
    Sha256,
    StrictModel,
    TablePayload,
)


class AnnotatedPage(StrictModel):
    pdf_page_index: Annotated[int, Field(gt=0)]
    reference_text: str
    block_ids: list[str] | None = None

    @model_validator(mode="after")
    def unique_blocks(self):
        if self.block_ids is not None and len(self.block_ids) != len(set(self.block_ids)):
            raise ValueError("duplicate annotated block IDs")
        return self


class AdjacentPair(StrictModel):
    before: str
    after: str

    @model_validator(mode="after")
    def different_blocks(self):
        if self.before == self.after:
            raise ValueError("reading order requires two distinct blocks")
        return self


class AnnotatedTable(StrictModel):
    block_id: str
    table: TablePayload


class AnnotatedCitation(StrictModel):
    block_id: str
    origins: Annotated[list[Origin], Field(min_length=1)]


class CriticalFixture(StrictModel):
    block_id: str
    expected_text: Annotated[str, Field(min_length=1)]


class ResourceObservation(StrictModel):
    """Explicit operator observations; unset measurements are never zero."""

    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    peak_rss_bytes: Annotated[int, Field(ge=0)] | None = None
    peak_vram_bytes: Annotated[int, Field(ge=0)] | None = None
    review_minutes: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None


class AnnotatedDocument(StrictModel):
    document_id: SourceId
    document_type: Annotated[str, Field(min_length=1)]
    group_id: Annotated[str, Field(min_length=1)]
    source_id: SourceId
    source_version: SourceId
    source_sha256: Sha256 | None = None
    synthetic: bool
    rights_confirmed: bool = False
    rights_reference: str | None = None
    reviewer: Annotated[str, Field(min_length=1)]
    annotation_reference: Annotated[str, Field(min_length=1)]
    profile: Literal["native_layout_v1", "english_ocr_v1"]
    status: Literal["completed", "blocked", "failed", "cancelled"]
    bundle: str | None = None
    pages: Annotated[list[AnnotatedPage], Field(min_length=1)]
    reading_order: list[AdjacentPair] = Field(default_factory=list)
    tables: list[AnnotatedTable] = Field(default_factory=list)
    citations: list[AnnotatedCitation] = Field(default_factory=list)
    critical_fixtures: list[CriticalFixture] = Field(default_factory=list)
    resources: ResourceObservation = Field(default_factory=ResourceObservation)

    @model_validator(mode="after")
    def check_document(self):
        if not self.synthetic and (not self.rights_confirmed or not (self.rights_reference or "").strip()):
            raise ValueError("real evaluation sources require confirmed rights")
        if self.status == "completed" and not self.bundle:
            raise ValueError("completed evaluations require a bundle")
        if self.status == "completed" and self.source_sha256 is None:
            raise ValueError("completed evaluations require the annotated original PDF hash")
        if self.status != "completed" and self.bundle is not None:
            raise ValueError("unsuccessful jobs cannot supply completed bundles")
        if len({page.pdf_page_index for page in self.pages}) != len(self.pages):
            raise ValueError("duplicate annotated page")
        for records in (self.tables, self.citations, self.critical_fixtures):
            if len({record.block_id for record in records}) != len(records):
                raise ValueError("duplicate annotated block")
        if len({(pair.before, pair.after) for pair in self.reading_order}) != len(self.reading_order):
            raise ValueError("duplicate annotated adjacency")
        return self


class CorpusAnnotations(StrictModel):
    schema_version: Literal["pdf-evaluation-0.1"] = "pdf-evaluation-0.1"
    corpus_id: SourceId
    split: Literal["development", "final"] = "development"
    frozen_at: str | None = None
    documents: Annotated[list[AnnotatedDocument], Field(min_length=1, max_length=10000)]

    @model_validator(mode="after")
    def unique_documents(self):
        if len({doc.document_id for doc in self.documents}) != len(self.documents):
            raise ValueError("duplicate document ID")
        pages: set[tuple[str, str, int]] = set()
        for doc in self.documents:
            for page in doc.pages:
                key = (doc.source_id, doc.source_version, page.pdf_page_index)
                if key in pages:
                    raise ValueError("source pages cannot be evaluated twice in one corpus")
                pages.add(key)
        if self.split == "final":
            from .schema import _timestamp

            if self.frozen_at is None:
                raise ValueError("final annotations require a frozen timestamp")
            _timestamp(self.frozen_at)
        return self


def character_edit_distance(reference: str, actual: str) -> int:
    """Exact Levenshtein distance using Myers' bit-vector algorithm.

    Python's arbitrary precision integers avoid an O(n*m) Python loop while
    retaining exact character comparisons for long annotated pages.
    """
    if len(reference) > len(actual):
        reference, actual = actual, reference
    length = len(reference)
    if not length:
        return len(actual)
    char_masks: dict[str, int] = {}
    for position, char in enumerate(reference):
        char_masks[char] = char_masks.get(char, 0) | (1 << position)
    mask = (1 << length) - 1
    high_bit = 1 << (length - 1)
    positive, negative, distance = mask, 0, length
    for char in actual:
        equality = char_masks.get(char, 0)
        vertical = equality | negative
        horizontal = (((equality & positive) + positive) ^ positive) | equality
        positive_horizontal = negative | ~(horizontal | positive)
        negative_horizontal = positive & horizontal
        if positive_horizontal & high_bit:
            distance += 1
        elif negative_horizontal & high_bit:
            distance -= 1
        positive_horizontal = (positive_horizontal << 1) | 1
        negative_horizontal <<= 1
        positive = (negative_horizontal | ~(vertical | positive_horizontal)) & mask
        negative = (positive_horizontal & vertical) & mask
    return distance


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _distribution(values: list[float]) -> dict:
    if not values:
        return {"observations": 0, "minimum": None, "median": None, "mean": None, "p95": None, "maximum": None}
    ordered = sorted(values)
    return {
        "observations": len(values),
        "minimum": ordered[0],
        "median": statistics.median(ordered),
        "mean": statistics.fmean(ordered),
        "p95": ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
        "maximum": ordered[-1],
    }


class _Scores:
    def __init__(self):
        self.counts: Counter = Counter()
        self.page_cer: list[float] = []
        self.resources: dict[str, list[float]] = {
            "elapsed_seconds": [],
            "seconds_per_requested_page": [],
            "peak_rss_bytes": [],
            "peak_vram_bytes": [],
            "review_minutes_per_annotated_page": [],
        }

    def merge(self, other: "_Scores") -> None:
        self.counts.update(other.counts)
        self.page_cer.extend(other.page_cer)
        for name in self.resources:
            self.resources[name].extend(other.resources[name])

    def report(self) -> dict:
        c = self.counts
        return {
            "cer": {
                "character_edits": c["edits"],
                "reference_characters": c["reference_chars"],
                "value": _ratio(c["edits"], c["reference_chars"]),
                "scored_pages": c["pages"],
                "missing_output_pages": c["missing_output_pages"],
                "empty_reference_pages": c["empty_reference_pages"],
                "page_distribution": _distribution(self.page_cer),
            },
            "page_accounting": {
                "annotated_pages": c["pages"],
                "accounted_annotated_pages": c["accounted_pages"],
                "accuracy": _ratio(c["accounted_pages"], c["pages"]),
                "resolved_annotated_pages": c["resolved_annotated_pages"],
                "requested_pages_in_valid_bundles": c["requested_pages"],
                "accounted_requested_pages": c["accounted_requested_pages"],
                "unresolved_requested_pages": c["unresolved_requested_pages"],
                "excluded_requested_pages": c["excluded_requested_pages"],
            },
            "citations": {
                "annotated_blocks": c["citations"],
                "matching_origins": c["citation_matches"],
                "accuracy": _ratio(c["citation_matches"], c["citations"]),
            },
            "reading_order": {
                "adjacent_pairs": c["order_pairs"],
                "correct_pairs": c["correct_pairs"],
                "missing_block_pairs": c["missing_pairs"],
                "accuracy": _ratio(c["correct_pairs"], c["order_pairs"]),
            },
            "tables": {
                "annotated_tables": c["tables"],
                "matching_dimensions": c["table_dimensions"],
                "reference_cells": c["cells"],
                "extra_output_cells": c["extra_cells"],
                "exact_cell_header_matches": c["cell_matches"],
                "cell_header_accuracy": _ratio(c["cell_matches"], c["cells"] + c["extra_cells"]),
                "matching_footnote_sets": c["footnote_matches"],
            },
            "critical_fixtures": {
                "annotated_blocks": c["critical"],
                "exact": c["critical_exact"],
                "explicit_unresolved_or_excluded_rejections": c["critical_rejected"],
                "missing_or_incorrect": c["critical"] - c["critical_exact"] - c["critical_rejected"],
                "exact_or_rejected_accuracy": _ratio(c["critical_exact"] + c["critical_rejected"], c["critical"]),
            },
            "exclusions": {
                "excluded_blocks": c["excluded_blocks"],
                "unresolved_blocks": c["unresolved_blocks"],
                "non_index_candidate_blocks": c["non_candidates"],
            },
            "resources": {name: _distribution(values) for name, values in self.resources.items()},
        }


def _origins_match(expected: list[Origin], actual: list[Origin]) -> bool:
    if len(expected) != len(actual):
        return False
    for a, b in zip(expected, actual, strict=True):
        if a.pdf_page_index != b.pdf_page_index or a.printed_label != b.printed_label:
            return False
        if a.bbox_top_left_normalized is not None and a.bbox_top_left_normalized != b.bbox_top_left_normalized:
            return False
    return True


def _score_document(doc: AnnotatedDocument, manifest: Manifest | None, blocks: list[Block], report: ReviewReport | None) -> _Scores:
    result = _Scores()
    c = result.counts
    by_id = {block.block_id: block for block in blocks}
    positions = {block.block_id: position for position, block in enumerate(blocks)}
    inventory = {page.pdf_page_index: page for page in manifest.page_inventory} if manifest else {}
    for page in doc.pages:
        c["pages"] += 1
        c["reference_chars"] += len(page.reference_text)
        selected = [
            block for block in blocks
            if any(origin.pdf_page_index == page.pdf_page_index for origin in block.origins)
            and (page.block_ids is None and block.kind not in {"furniture", "figure", "placeholder", "formula"}
                 or page.block_ids is not None and block.block_id in page.block_ids)
        ]
        actual = "\n".join(block.text_normalized for block in selected)
        edits = character_edit_distance(page.reference_text, actual)
        c["edits"] += edits
        if page.reference_text:
            result.page_cer.append(edits / len(page.reference_text))
        else:
            c["empty_reference_pages"] += 1
        if manifest is None or page.pdf_page_index not in inventory:
            c["missing_output_pages"] += 1
        if page.pdf_page_index in inventory:
            c["accounted_pages"] += 1
            c["resolved_annotated_pages"] += inventory[page.pdf_page_index].status != "unresolved"
    if manifest:
        c["requested_pages"] += len(manifest.requested_pages)
        c["accounted_requested_pages"] += len(manifest.page_inventory)
        c["unresolved_requested_pages"] += sum(page.status == "unresolved" for page in manifest.page_inventory)
        c["excluded_requested_pages"] += sum(page.status == "excluded" for page in manifest.page_inventory)
    for pair in doc.reading_order:
        c["order_pairs"] += 1
        if pair.before not in positions or pair.after not in positions:
            c["missing_pairs"] += 1
        elif positions[pair.before] < positions[pair.after]:
            c["correct_pairs"] += 1
    for citation in doc.citations:
        c["citations"] += 1
        block = by_id.get(citation.block_id)
        if block is not None and _origins_match(citation.origins, block.origins):
            c["citation_matches"] += 1
    for table in doc.tables:
        c["tables"] += 1
        expected_cells = {(cell.row, cell.column): cell for cell in table.table.cells}
        c["cells"] += len(expected_cells)
        block = by_id.get(table.block_id)
        if block is None or block.kind != "table":
            continue
        actual_cells = {(cell.row, cell.column): cell for cell in block.table.cells}
        c["extra_cells"] += len(set(actual_cells) - set(expected_cells))
        c["cell_matches"] += sum(actual_cells.get(key) == cell for key, cell in expected_cells.items())
        c["table_dimensions"] += (table.table.rows, table.table.columns) == (block.table.rows, block.table.columns)
        c["footnote_matches"] += table.table.footnotes == block.table.footnotes
    for fixture in doc.critical_fixtures:
        c["critical"] += 1
        block = by_id.get(fixture.block_id)
        if block is None:
            continue
        if block.text_normalized == fixture.expected_text:
            c["critical_exact"] += 1
            continue
        linked = [issue for issue in report.issues if fixture.block_id in issue.block_ids] if report else []
        unresolved = block.extraction_quality == "unresolved" and any(issue.resolution is None for issue in linked)
        excluded = block.extraction_quality == "excluded" and any(issue.resolution == "excluded" for issue in linked)
        if unresolved or excluded:
            c["critical_rejected"] += 1
    c["excluded_blocks"] += sum(block.extraction_quality == "excluded" for block in blocks)
    c["unresolved_blocks"] += sum(block.extraction_quality == "unresolved" for block in blocks)
    c["non_candidates"] += sum(not block.index_candidate for block in blocks)
    observations = doc.resources.model_dump(exclude_none=True)
    if manifest:
        for name in ("elapsed_seconds", "peak_rss_bytes", "peak_vram_bytes"):
            if name not in observations:
                value = manifest.metrics.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                    observations[name] = value
    for name, value in observations.items():
        output_name = "review_minutes_per_annotated_page" if name == "review_minutes" else name
        result.resources[output_name].append(value / len(doc.pages) if name == "review_minutes" else value)
        if name == "elapsed_seconds" and manifest:
            result.resources["seconds_per_requested_page"].append(value / len(manifest.requested_pages))
    return result


def evaluate_corpus(annotation_file: Path) -> dict:
    """Read a versioned annotation file and return JSON-serializable measurements.

    Bundle paths are relative to, and must remain inside, the annotation folder.
    Invalid bundles and failed jobs are scored as missing outputs, preserving
    their reference text and fixture denominators. No source content is returned.
    """
    from .serialize import validate_bundle

    try:
        if annotation_file.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("annotation file too large")
        payload = annotation_file.read_bytes()
        def unique_keys(pairs):
            values = {}
            for name, value in pairs:
                if name in values:
                    raise ValueError("duplicate annotation key")
                values[name] = value
            return values
        json.loads(payload, object_pairs_hook=unique_keys)
        corpus = CorpusAnnotations.model_validate_json(payload)
    except (OSError, ValueError, ValidationError) as exc:
        raise IngestError("INVALID_EVALUATION", "Annotations are missing or do not satisfy the evaluation schema.") from exc
    root = annotation_file.resolve().parent
    scores = _Scores()
    profiles: dict[str, _Scores] = {}
    types: dict[str, _Scores] = {}
    job_counts = Counter({name: 0 for name in ("completed", "blocked", "failed", "cancelled")})
    errors: list[dict] = []
    documents: list[dict] = []
    source_hashes: set[str] = set()
    annotated_verified_pages: set[tuple[str, int]] = set()
    verified_jobs = 0
    for doc in corpus.documents:
        job_counts[doc.status] += 1
        manifest, blocks, review = None, [], None
        if doc.status == "completed":
            try:
                relative = Path(doc.bundle)
                bundle = (root / relative).resolve()
                if relative.is_absolute() or not bundle.is_relative_to(root) or bundle == root:
                    raise ValueError("bundle path is not contained")
                manifest, blocks, review = validate_bundle(bundle)
                if (manifest.source.source_id, manifest.source.source_version, manifest.profile, manifest.source.synthetic) != (
                    doc.source_id, doc.source_version, doc.profile, doc.synthetic
                ) or (not doc.synthetic and not manifest.source.rights_confirmed):
                    raise ValueError("source annotation mismatch")
                if manifest.source_sha256 != doc.source_sha256:
                    raise ValueError("original source hash differs from annotated source")
                if not {page.pdf_page_index for page in doc.pages}.issubset(manifest.requested_pages):
                    raise ValueError("annotated pages lie outside conversion range")
                source_hashes.add(manifest.source_sha256)
                annotated_verified_pages.update((manifest.source_sha256, page.pdf_page_index) for page in doc.pages)
                verified_jobs += 1
            except (OSError, ValueError, ValidationError):
                manifest, blocks, review = None, [], None
                errors.append({"document_id": doc.document_id, "code": "INVALID_BUNDLE"})
        local = _score_document(doc, manifest, blocks, review)
        scores.merge(local)
        profiles.setdefault(doc.profile, _Scores()).merge(local)
        types.setdefault(doc.document_type, _Scores()).merge(local)
        documents.append({"document_id": doc.document_id, "status": doc.status, "bundle_valid": manifest is not None, "metrics": local.report()})
    return {
        "schema_version": "pdf-evaluation-result-0.1",
        "corpus_id": corpus.corpus_id,
        "split": corpus.split,
        "frozen_at": corpus.frozen_at,
        "metric_policy": "exact Unicode code-point Levenshtein; no normalization; page blocks joined by LF",
        "corpus": {
            "annotated_jobs": len(corpus.documents),
            "verified_completed_jobs": verified_jobs,
            "verified_distinct_pdf_files": len(source_hashes),
            "annotated_pages": sum(len(doc.pages) for doc in corpus.documents),
            "verified_distinct_annotated_pages": len(annotated_verified_pages),
            "proposed_minimum_pdf_files": 30,
            "proposed_minimum_annotated_pages": 200,
            "proposed_minimum_corpus_met": len(source_hashes) >= 30 and len(annotated_verified_pages) >= 200,
        },
        "job_counts": dict(job_counts),
        "invalid_completed_bundles": len(errors),
        "metrics": scores.report(),
        "by_profile": {key: value.report() for key, value in sorted(profiles.items())},
        "by_document_type": {key: value.report() for key, value in sorted(types.items())},
        "documents": documents,
        "errors": errors,
        "limitations": [
            "Annotation rights and visual ground truth remain operator attestations.",
            "Job counts use annotated outcome records; failed/blocked/cancelled statuses have no completed bundle to verify.",
            "Distinct PDF hashes and annotated pages are counted only after bundle integrity and identity checks.",
            "Failed/invalid outputs remain in CER, reading-order, citation, table, and critical-fixture denominators.",
            "Corpus minimums alone do not grant source approval or establish clinically safe extraction.",
            "Throughput uses requested pages of valid bundles; review time uses annotated pages; unavailable measurements stay null.",
        ],
    }
