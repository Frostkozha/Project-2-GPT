"""Synthetic contract checks, not measurements of PDF extraction accuracy."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pdf_ingest.normalize import normalize_text
from pdf_ingest.provenance import normalize_bbox, stable_block_id, text_hash
from pdf_ingest.schema import (
    BLOCK_ADAPTER, ApprovalRecord, Correction,
    IngestError, Issue, Manifest, Origin, PageInventory, ReviewReport,
    SourceSpec, TableBlock, TableCell, TablePayload, TextBlock, table_text,
)
from pdf_ingest.serialize import (
    escape_markdown, read_corrections, rewrite_bundle, validate_bundle, write_bundle,
)


def sample_records(text="Dose 0.5 mg; α ≤ 2; do not increase."):
    source = SourceSpec(source_id="fixture", source_version="1", title="Fixture source",
                        assignment="synthetic development", rights_reference="", synthetic=True)
    origins = [Origin(pdf_page_index=12, printed_label="8", bbox_top_left_normalized=(0.1, 0.2, 0.9, 0.4)),
               Origin(pdf_page_index=13, bbox_top_left_normalized=(0.1, 0.0, 0.9, 0.1))]
    block = TextBlock(block_id="paragraph-a", source_id=source.source_id,
                      source_version=source.source_version, kind="paragraph", text_raw=text,
                      text_normalized=text, origins=origins, extraction_method="native_layout",
                      text_sha256=text_hash(text))
    inventory = [PageInventory(pdf_page_index=12, width=600.0, height=800.0, rotation=0,
                               printed_label="8", printed_label_verified=True, status="extracted", block_ids=[block.block_id]),
                 PageInventory(pdf_page_index=13, width=600.0, height=800.0, rotation=0,
                               status="extracted", block_ids=[block.block_id]),
                 PageInventory(pdf_page_index=14, width=600.0, height=800.0, rotation=0,
                               status="confirmed_blank", reason="Visually verified synthetic blank page")]
    manifest = Manifest(conversion_id="conversion-fixture", source=source,
                        source_sha256=text_hash("original fixture PDF"), source_page_count=14,
                        requested_pages=[12, 13, 14], profile="native_layout_v1",
                        conversion_fingerprint=text_hash("converter fingerprint"),
                        dependency_lock_sha256=text_hash("dependency lock"), converter_version="0.1.0",
                        model_revisions={"layout": "fixture-only"}, normalization_version="nfc-lf-v1",
                        serialization_version="markdown-v1", created_at="2026-10-09T12:00:00Z",
                        page_inventory=inventory, idempotency_key=text_hash("idempotency"))
    return manifest, [block], ReviewReport()


def rehash(bundle: Path, filename: str):
    manifest_file = bundle / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    manifest["output_hashes"][filename] = hashlib.sha256((bundle / filename).read_bytes()).hexdigest()
    manifest_file.write_text(json.dumps(manifest))


def test_normalization_preserves_critical_text_and_whitespace():
    raw = "  Dose −0.5 µg; α ≤ 2; >= 1; not increased.  \r\nCafe\u0301\rsecond\tcolumn"
    normalized, changes = normalize_text(raw)
    assert normalized == "  Dose −0.5 µg; α ≤ 2; >= 1; not increased.  \nCafé\nsecond\tcolumn"
    assert changes == ["line_endings_lf", "unicode_nfc"]
    assert normalize_text(normalized) == (normalized, [])


@pytest.mark.parametrize("rotation,expected", [
    (0, (0.1, 0.2, 0.4, 0.6)), (90, (0.4, 0.1, 0.8, 0.4)),
    (180, (0.6, 0.4, 0.9, 0.8)), (270, (0.2, 0.6, 0.6, 0.9)),
])
def test_bottom_left_rotation_normalization(rotation, expected):
    assert normalize_bbox((10, 80, 40, 160), 100, 200, rotation=rotation) == pytest.approx(expected)


@pytest.mark.parametrize("bbox,width,height,rotation", [
    ((-1, 0, 2, 4), 10, 10, 0), ((0, 0, float("nan"), 1), 10, 10, 0),
    ((4, 0, 1, 2), 10, 10, 0), ((0, 0, 1, 1), 0, 10, 0), ((0, 0, 1, 1), 10, 10, 45),
])
def test_invalid_boxes_rejected(bbox, width, height, rotation):
    with pytest.raises(ValueError):
        normalize_bbox(bbox, width, height, rotation=rotation)


def test_stable_ids_include_identity_content_origin_and_fingerprint():
    manifest, blocks, _ = sample_records()
    block = blocks[0]
    identity = stable_block_id(manifest.source, block.origins, "anchor", block.text_normalized, "fp")
    assert identity == stable_block_id(manifest.source, block.origins, "anchor", block.text_normalized, "fp")
    variants = [
        stable_block_id(manifest.source, block.origins, "anchor", "changed", "fp"),
        stable_block_id(manifest.source, block.origins, "anchor", block.text_normalized, "fp2"),
        stable_block_id(manifest.source.model_copy(update={"source_id": "another"}), block.origins, "anchor", block.text_normalized, "fp"),
    ]
    assert len(set([identity] + variants)) == 4


def test_bundle_round_trip_preserves_physical_pages_and_multiorigins(tmp_path):
    manifest, blocks, report = sample_records()
    bundle = write_bundle(tmp_path / "bundle", manifest, blocks, report)
    loaded, canonical, _ = validate_bundle(bundle)
    assert loaded.requested_pages == [12, 13, 14]
    assert [o.pdf_page_index for o in canonical[0].origins] == [12, 13]
    assert canonical[0].origins[0].printed_label == "8"
    assert canonical[0].text_sha256 == text_hash(canonical[0].text_normalized)
    rendered_lines = (bundle / "document.md").read_text().splitlines()
    span = canonical[0].markdown_span
    assert rendered_lines[span.start_line - 1:span.end_line] == [escape_markdown(canonical[0].text_normalized)]
    assert "pdf-page: 14" in (bundle / "document.md").read_text()
    assert set(loaded.output_hashes) == {"document.md", "blocks.jsonl", "review_report.json"}


def test_safe_source_display_cannot_activate_html_images_or_remote_links(tmp_path):
    text = '<script>alert(1)</script> ![x](https://example.com/a.png) <img src="https://example.com">'
    manifest, blocks, report = sample_records(text)
    bundle = write_bundle(tmp_path / "bundle", manifest, blocks, report)
    markdown = (bundle / "document.md").read_text()
    assert "<script>" not in markdown
    assert "<img " not in markdown
    assert "![x](" not in markdown
    assert "https://example.com" not in markdown
    assert "&lt;script&gt;" in markdown
    assert validate_bundle(bundle)[1][0].text_normalized == text


def test_canonical_derivative_edit_rejected_even_after_hash_rewrite(tmp_path):
    manifest, blocks, report = sample_records()
    bundle = write_bundle(tmp_path / "bundle", manifest, blocks, report)
    (bundle / "document.md").write_text("Altered dose 50 mg.\n")
    with pytest.raises(IngestError, match="HASH_MISMATCH"):
        validate_bundle(bundle)
    rehash(bundle, "document.md")
    with pytest.raises(IngestError, match="deterministic canonical rendering"):
        validate_bundle(bundle)


def test_false_line_spans_rejected_even_with_consistent_file_hash(tmp_path):
    manifest, blocks, report = sample_records()
    bundle = write_bundle(tmp_path / "bundle", manifest, blocks, report)
    block = json.loads((bundle / "blocks.jsonl").read_text())
    block["markdown_span"] = {"start_line": 1, "end_line": 1}
    (bundle / "blocks.jsonl").write_text(json.dumps(block) + "\n")
    rehash(bundle, "blocks.jsonl")
    with pytest.raises(IngestError, match="Markdown spans"):
        validate_bundle(bundle)


@pytest.mark.parametrize("mutation", ["missing_page", "wrong_source", "wrong_block_page", "missing_page_block", "unverified_label"])
def test_provenance_accounting_failures_block_publication(tmp_path, mutation):
    manifest, blocks, report = sample_records()
    if mutation == "missing_page":
        manifest.page_inventory = manifest.page_inventory[:-1]
    elif mutation == "wrong_source":
        blocks[0].source_id = "another-source"
    elif mutation == "wrong_block_page":
        blocks[0].origins[1].pdf_page_index = 11
    elif mutation == "missing_page_block":
        manifest.page_inventory[0].block_ids = []
    else:
        manifest.page_inventory[0].printed_label_verified = False
    with pytest.raises(IngestError, match="INVALID_PROVENANCE"):
        write_bundle(tmp_path / "bundle", manifest, blocks, report)
    assert not (tmp_path / "bundle").exists()
    assert not list(tmp_path.glob(".bundle-staging-*"))


def test_missing_box_requires_specific_attached_review_issue(tmp_path):
    manifest, blocks, report = sample_records()
    blocks[0].origins[0].bbox_top_left_normalized = None
    with pytest.raises(IngestError, match="missing block location"):
        write_bundle(tmp_path / "missing", manifest, blocks, report)
    issue = Issue(issue_id="missing-box", category="MISSING_BBOX", pdf_page_indices=[12],
                  block_ids=[blocks[0].block_id], explanation="Box not available; verify source location")
    report.issues = [issue]
    blocks[0].issues = [issue.issue_id]
    manifest.review_state = "needs_review"
    write_bundle(tmp_path / "review-needed", manifest, blocks, report)


def test_merged_table_grid_footnotes_and_source_symbols_preserved(tmp_path):
    manifest, blocks, report = sample_records()
    table = TablePayload(rows=3, columns=2, cells=[
        TableCell(text="Drug | dose", row=0, column=0, column_span=2, column_header=True),
        TableCell(text="α", row=1, column=0, row_header=True), TableCell(text="0.5 µg", row=1, column=1),
        TableCell(text="not increased", row=2, column=0), TableCell(text="≤ 2", row=2, column=1),
    ], footnotes=["* Per kg; do not infer a missing unit."])
    base = blocks[0].model_dump(exclude={"kind"})
    base.update(text_raw=table_text(table), text_normalized=table_text(table), text_sha256=text_hash(table_text(table)))
    blocks = [TableBlock(**base, table=table)]
    bundle = write_bundle(tmp_path / "bundle", manifest, blocks, report)
    markdown = (bundle / "document.md").read_text()
    loaded_table = validate_bundle(bundle)[1][0]
    assert loaded_table.table.cells[0].column_span == 2
    assert loaded_table.table.cells[1].row_header
    assert "Drug \\| dose" in markdown
    assert "review display" in markdown
    assert "0.5 µg" in loaded_table.text_normalized
    assert "Per kg" in loaded_table.text_normalized
    assert loaded_table.markdown_span.end_line > loaded_table.markdown_span.start_line


def test_table_structure_and_canonical_text_are_checked(tmp_path):
    with pytest.raises(ValidationError, match="overlap"):
        TablePayload(rows=1, columns=2, cells=[TableCell(text="Merged", row=0, column=0, column_span=2),
                                             TableCell(text="Overlap", row=0, column=1)])
    manifest, blocks, report = sample_records()
    base = blocks[0].model_dump(exclude={"kind"})
    block = TableBlock(**base, table=TablePayload(rows=1, columns=1, cells=[TableCell(text="0.5 mg", row=0, column=0)]))
    with pytest.raises(IngestError, match="canonical table text"):
        write_bundle(tmp_path / "bundle", manifest, [block], report)


def test_existing_destination_never_overwritten(tmp_path):
    manifest, blocks, report = sample_records()
    bundle = write_bundle(tmp_path / "bundle", manifest, blocks, report)
    original = (bundle / "document.md").read_bytes()
    with pytest.raises(FileExistsError):
        write_bundle(bundle, manifest, blocks, report)
    assert (bundle / "document.md").read_bytes() == original


def test_atomic_review_rewrite_preserves_historical_correction_audit(tmp_path):
    manifest, blocks, report = sample_records()
    correction = Correction(correction_id="patch-historical", block_id="prior-version-block",
                            original_text="0.6 mg", new_text="0.5 mg", reason="Verified PDF numeral",
                            reviewer="Fixture reviewer", timestamp="2026-10-09T12:00:00Z")
    bundle = write_bundle(tmp_path / "bundle", manifest, blocks, report, [correction])
    manifest, blocks, report = validate_bundle(bundle)
    report.approvals = [ApprovalRecord(reviewer="Fixture reviewer", timestamp="2026-10-09T12:30:00Z",
                                      method="direct", block_ids=[blocks[0].block_id], content_approval_reference="faculty-fixture")]
    blocks[0].extraction_quality = "approved"
    manifest.review_state = "approved"
    rewrite_bundle(bundle, manifest, blocks, report)
    assert validate_bundle(bundle)[0].review_state == "approved"
    assert read_corrections(bundle) == [correction]


def test_same_version_rewrite_cannot_change_evidence_or_remove_audit(tmp_path):
    manifest, blocks, report = sample_records()
    correction = Correction(correction_id="patch-historical", block_id="prior-version-block",
                            original_text="0.6 mg", new_text="0.5 mg", reason="Verified PDF numeral",
                            reviewer="Fixture reviewer", timestamp="2026-10-09T12:00:00Z")
    bundle = write_bundle(tmp_path / "bundle", manifest, blocks, report, [correction])
    manifest, blocks, report = validate_bundle(bundle)
    with pytest.raises(IngestError, match="append-only"):
        rewrite_bundle(bundle, manifest, blocks, report, corrections=[])
    blocks[0].text_normalized = "New medical prose"
    blocks[0].text_sha256 = text_hash(blocks[0].text_normalized)
    with pytest.raises(IngestError, match="new conversion version"):
        rewrite_bundle(bundle, manifest, blocks, report)
    assert validate_bundle(bundle)[1][0].text_normalized != "New medical prose"


def test_unsupported_profile_method_and_table_grid_fail_closed(tmp_path):
    manifest, blocks, report = sample_records()
    blocks[0].extraction_method = "english_ocr"
    with pytest.raises(IngestError, match="selected profile"):
        write_bundle(tmp_path / "bundle", manifest, blocks, report)
    with pytest.raises(ValidationError, match="grid limit"):
        TablePayload(rows=1000000, columns=1000000, cells=[])


def test_schema_rejects_unknown_fields_fake_hashes_and_numeric_coercion():
    manifest, blocks, _ = sample_records()
    dumped = blocks[0].model_dump(mode="json")
    dumped["text_sha256"] = "fixture-only"
    with pytest.raises(ValidationError):
        BLOCK_ADAPTER.validate_json(json.dumps(dumped))
    dumped["text_sha256"] = text_hash(blocks[0].text_normalized)
    dumped["secret_path"] = "/private/source.pdf"
    with pytest.raises(ValidationError):
        BLOCK_ADAPTER.validate_json(json.dumps(dumped))
    with pytest.raises(ValidationError):
        Origin(pdf_page_index="12")
    with pytest.raises(ValidationError):
        SourceSpec(source_id="fixture", source_version="1", title="Fixture", assignment="dev",
                   rights_reference="", synthetic=True, expected_source_sha256="fixture-only")


def test_duplicate_json_keys_and_symlink_artifacts_rejected(tmp_path):
    manifest, blocks, report = sample_records()
    bundle = write_bundle(tmp_path / "bundle", manifest, blocks, report)
    record = (bundle / "manifest.json").read_text()
    (bundle / "manifest.json").write_text(record.replace('"conversion_id":', '"conversion_id": "duplicate", "conversion_id":', 1))
    with pytest.raises(IngestError, match="strict schema"):
        validate_bundle(bundle)
    (bundle / "manifest.json").write_text(record)
    (bundle / "document.md").unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("private fixture")
    (bundle / "document.md").symlink_to(outside)
    with pytest.raises(IngestError, match="unsafe"):
        validate_bundle(bundle)
