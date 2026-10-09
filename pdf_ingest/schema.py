"""Strict canonical records for a reviewed, traceable PDF derivative.

These records describe extraction and provenance; they do not grant source rights
or approve evidence.  Cross-record integrity checks live in ``serialize``.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

Profile = Literal["native_layout_v1", "english_ocr_v1"]
ExtractionMethod = Literal["native_layout", "english_ocr", "reviewed_transcription"]
ExtractionQuality = Literal["unreviewed", "approved", "excluded", "unresolved"]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SourceId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")]
RecordId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class IngestError(ValueError):
    """Operator-safe failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


def _timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value


class SourceSpec(StrictModel):
    tenant_id: SourceId = "local"
    source_id: SourceId
    source_version: SourceId
    title: Annotated[str, Field(min_length=1)]
    assignment: Annotated[str, Field(min_length=1)]
    rights_reference: str
    rights_confirmed: bool = False
    content_approved: bool = False
    content_approval_reference: str | None = None
    eligible: bool = True
    synthetic: bool = False
    expected_source_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def check_source(self) -> "SourceSpec":
        if not self.synthetic and not self.rights_reference.strip():
            raise ValueError("real sources require a rights reference")
        if self.content_approved and not (self.content_approval_reference or "").strip():
            raise ValueError("content approval requires its reference")
        return self


class Origin(StrictModel):
    pdf_page_index: PositiveInt
    printed_label: str | None = None
    bbox_top_left_normalized: tuple[float, float, float, float] | None = None
    coordinate_system: Literal["top-left normalized rotation-corrected"] = "top-left normalized rotation-corrected"
    raw_coordinate_system: str | None = None
    transformation: str | None = None

    @field_validator("bbox_top_left_normalized")
    @classmethod
    def check_bbox(cls, value: tuple[float, float, float, float] | None):
        if value is not None:
            x0, y0, x1, y1 = value
            if not all(math.isfinite(v) and 0 <= v <= 1 for v in value):
                raise ValueError("box coordinates must be finite and in [0,1]")
            if x0 > x1 or y0 > y1:
                raise ValueError("box corners are reversed")
        return value


class MarkdownSpan(StrictModel):
    start_line: PositiveInt
    end_line: PositiveInt

    @model_validator(mode="after")
    def check_order(self) -> "MarkdownSpan":
        if self.end_line < self.start_line:
            raise ValueError("line span is reversed")
        return self


class HeadingPayload(StrictModel):
    level: Annotated[int, Field(ge=1, le=6)]


class TableCell(StrictModel):
    text: str
    row: NonNegativeInt
    column: NonNegativeInt
    row_span: PositiveInt = 1
    column_span: PositiveInt = 1
    column_header: bool = False
    row_header: bool = False


class TablePayload(StrictModel):
    rows: PositiveInt
    columns: PositiveInt
    cells: list[TableCell]
    footnotes: list[str] = Field(default_factory=list)
    approved_representation: str | None = None

    @model_validator(mode="after")
    def check_cells(self) -> "TablePayload":
        if self.rows * self.columns > 1000000:
            raise ValueError("table exceeds the supported million-position grid limit")
        occupied: set[tuple[int, int]] = set()
        for cell in self.cells:
            if cell.row + cell.row_span > self.rows or cell.column + cell.column_span > self.columns:
                raise ValueError("table cell lies outside the grid")
            for row in range(cell.row, cell.row + cell.row_span):
                for col in range(cell.column, cell.column + cell.column_span):
                    if (row, col) in occupied:
                        raise ValueError("table cells overlap")
                    occupied.add((row, col))
        if self.approved_representation is not None and not self.approved_representation.strip():
            raise ValueError("approved representation cannot be empty")
        return self


def table_text(table: TablePayload) -> str:
    """Canonical grid text; review may explicitly replace it with structured prose."""
    if table.approved_representation is not None:
        return table.approved_representation
    grid = [["" for _ in range(table.columns)] for _ in range(table.rows)]
    for cell in sorted(table.cells, key=lambda c: (c.row, c.column)):
        grid[cell.row][cell.column] = cell.text
    lines = ["\t".join(row) for row in grid]
    return "\n".join(lines + table.footnotes)


class BlockBase(StrictModel):
    schema_version: Literal["pdf-block-0.1"] = "pdf-block-0.1"
    block_id: RecordId
    source_id: SourceId
    source_version: SourceId
    section_path: list[str] = Field(default_factory=list)
    text_raw: str
    text_normalized: str
    origins: Annotated[list[Origin], Field(min_length=1)]
    extraction_method: ExtractionMethod
    extraction_quality: ExtractionQuality = "unreviewed"
    index_candidate: bool = True
    markdown_span: MarkdownSpan | None = None
    issues: list[RecordId] = Field(default_factory=list)
    text_sha256: Sha256
    normalizations: list[str] = Field(default_factory=list)

    @field_validator("issues")
    @classmethod
    def unique_issues(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("duplicate block issue references")
        return value


class HeadingBlock(BlockBase):
    kind: Literal["heading"] = "heading"
    heading: HeadingPayload


class TableBlock(BlockBase):
    kind: Literal["table"] = "table"
    table: TablePayload


class TextBlock(BlockBase):
    kind: Literal["paragraph", "list", "caption", "formula", "figure", "furniture", "placeholder"]


Block = Annotated[HeadingBlock | TableBlock | TextBlock, Field(discriminator="kind")]
BLOCK_ADAPTER = TypeAdapter(Block)


class PageInventory(StrictModel):
    pdf_page_index: PositiveInt
    width: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    height: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    rotation: Literal[0, 90, 180, 270]
    printed_label: str | None = None
    printed_label_verified: bool = False
    native_text_chars: NonNegativeInt = 0
    native_text_sha256: Sha256 | None = None
    status: Literal["extracted", "confirmed_blank", "non_text", "excluded", "unresolved"] = "unresolved"
    reason: str | None = None
    block_ids: list[RecordId] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_page(self) -> "PageInventory":
        if self.printed_label_verified and self.printed_label is None:
            raise ValueError("verified page label must be present")
        if self.status in {"confirmed_blank", "non_text", "excluded"} and not (self.reason or "").strip():
            raise ValueError("non-extracted page status requires a reason")
        if len(set(self.block_ids)) != len(self.block_ids):
            raise ValueError("duplicate page block references")
        return self


class Issue(StrictModel):
    issue_id: RecordId
    severity: Literal["info", "warning", "error"] = "warning"
    category: Annotated[str, Field(min_length=1)]
    pdf_page_indices: list[PositiveInt] = Field(default_factory=list)
    block_ids: list[RecordId] = Field(default_factory=list)
    explanation: Annotated[str, Field(min_length=1)]
    resolution: Literal["approved", "excluded"] | None = None
    reviewer: str | None = None
    resolved_at: str | None = None
    correction_ids: list[RecordId] = Field(default_factory=list)

    @field_validator("resolved_at")
    @classmethod
    def check_time(cls, value):
        return None if value is None else _timestamp(value)

    @model_validator(mode="after")
    def check_resolution(self) -> "Issue":
        if self.resolution is not None and (not (self.reviewer or "").strip() or self.resolved_at is None):
            raise ValueError("resolved issues require reviewer and timestamp")
        if self.resolution is None and (self.reviewer is not None or self.resolved_at is not None):
            raise ValueError("unresolved issues cannot carry resolution metadata")
        for values in (self.pdf_page_indices, self.block_ids, self.correction_ids):
            if len(values) != len(set(values)):
                raise ValueError("duplicate issue references")
        return self


class ApprovalRecord(StrictModel):
    reviewer: Annotated[str, Field(min_length=1)]
    timestamp: str
    method: Literal["direct", "source_level_sampled"]
    block_ids: list[RecordId] = Field(default_factory=list)
    sampled_pages: list[PositiveInt] = Field(default_factory=list)
    content_approval_reference: Annotated[str, Field(min_length=1)]
    permitted_subset_reference: str | None = None

    _valid_timestamp = field_validator("timestamp")(_timestamp)

    @model_validator(mode="after")
    def check_scope(self) -> "ApprovalRecord":
        if len(self.block_ids) != len(set(self.block_ids)) or len(self.sampled_pages) != len(set(self.sampled_pages)):
            raise ValueError("duplicate approval scope references")
        return self


class ReviewReport(StrictModel):
    issues: list[Issue] = Field(default_factory=list)
    approvals: list[ApprovalRecord] = Field(default_factory=list)


class Manifest(StrictModel):
    schema_version: Literal["pdf-bundle-0.1"] = "pdf-bundle-0.1"
    conversion_id: RecordId
    source: SourceSpec
    source_sha256: Sha256
    source_page_count: PositiveInt
    requested_pages: Annotated[list[PositiveInt], Field(min_length=1)]
    profile: Profile
    conversion_fingerprint: Sha256
    dependency_lock_sha256: Sha256
    converter_version: Annotated[str, Field(min_length=1)]
    model_revisions: dict[str, str]
    normalization_version: Annotated[str, Field(min_length=1)]
    serialization_version: Annotated[str, Field(min_length=1)]
    created_at: str
    job_status: Literal["queued", "running", "completed", "blocked", "failed", "cancelled"] = "completed"
    review_state: Literal["unreviewed", "needs_review", "approved", "excluded"] = "unreviewed"
    output_hashes: dict[str, Sha256] = Field(default_factory=dict)
    page_inventory: list[PageInventory]
    approval_references: list[str] = Field(default_factory=list)
    parent_conversion_id: RecordId | None = None
    idempotency_key: Sha256
    metrics: dict[str, Any] = Field(default_factory=dict)

    _valid_created_at = field_validator("created_at")(_timestamp)

    @model_validator(mode="after")
    def check_manifest(self) -> "Manifest":
        if self.requested_pages != sorted(set(self.requested_pages)):
            raise ValueError("requested pages must be sorted and unique")
        if self.requested_pages[-1] > self.source_page_count:
            raise ValueError("requested page exceeds source page count")
        for name in self.output_hashes:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or name in {".", "..", "manifest.json"}:
                raise ValueError("output hash names must be safe derivative filenames")
        return self


class Correction(StrictModel):
    correction_id: RecordId
    block_id: RecordId
    original_text: str
    new_text: str
    reason: Annotated[str, Field(min_length=1)]
    reviewer: Annotated[str, Field(min_length=1)]
    timestamp: str

    _valid_timestamp = field_validator("timestamp")(_timestamp)


class Limits(StrictModel):
    max_file_bytes: PositiveInt = 104857600
    max_pages: PositiveInt = 2000
    max_range_pages: PositiveInt = 2000
    job_timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 1800.0
    page_timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 120.0
    memory_bytes: PositiveInt
    scratch_bytes: PositiveInt = 1073741824
    worker_threads: Annotated[int, Field(ge=1, le=64)] = 2
