from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from pdf_ingest.evaluation import character_edit_distance, evaluate_corpus
from pdf_ingest.provenance import text_hash
from pdf_ingest.schema import (
    Issue,
    Manifest,
    Origin,
    PageInventory,
    ReviewReport,
    SourceSpec,
    TableBlock,
    TableCell,
    TablePayload,
    TextBlock,
    table_text,
)
from pdf_ingest.serialize import validate_bundle, write_bundle


def _block(block_id: str, text: str, **changes):
    values = dict(
        block_id=block_id,
        source_id="fixture",
        source_version="1",
        kind="paragraph",
        text_raw=text,
        text_normalized=text,
        text_sha256=text_hash(text),
        origins=[Origin(pdf_page_index=1, bbox_top_left_normalized=(0.1, 0.1, 0.8, 0.3))],
        extraction_method="native_layout",
    )
    values.update(changes)
    return TextBlock(**values)


def _bundle(tmp_path: Path, blocks, issues=None, metrics=None) -> Path:
    manifest = Manifest(
        conversion_id="conversion-1",
        source=SourceSpec(
            source_id="fixture", source_version="1", title="Fixture", assignment="evaluation",
            rights_reference="synthetic", synthetic=True,
        ),
        source_sha256=text_hash("synthetic PDF source"),
        source_page_count=1,
        requested_pages=[1],
        profile="native_layout_v1",
        conversion_fingerprint="1" * 64,
        dependency_lock_sha256="2" * 64,
        converter_version="0.1.0",
        model_revisions={"layout": "fixture"},
        normalization_version="nfc-lf-v1",
        serialization_version="markdown-v1",
        created_at="2026-10-09T12:00:00Z",
        review_state="needs_review" if issues else "unreviewed",
        page_inventory=[PageInventory(
            pdf_page_index=1, width=100.0, height=100.0, rotation=0,
            status="extracted", block_ids=[block.block_id for block in blocks],
        )],
        idempotency_key="3" * 64,
        metrics=metrics or {},
    )
    destination = tmp_path / "bundle"
    write_bundle(destination, manifest, blocks, ReviewReport(issues=issues or []))
    return destination


def _document(**changes):
    values = dict(
        document_id="digital-1", document_type="digital_single_column", group_id="lecture-1",
        source_id="fixture", source_version="1", synthetic=True,
        source_sha256=text_hash("synthetic PDF source"),
        reviewer="Test reviewer", annotation_reference="synthetic-fixture-v1",
        profile="native_layout_v1", status="completed", bundle="bundle",
        pages=[{"pdf_page_index": 1, "reference_text": "Dose ≤0.5 mg; no fever."}],
    )
    values.update(changes)
    return values


def _annotations(tmp_path: Path, documents) -> Path:
    path = tmp_path / "annotations.json"
    path.write_text(json.dumps({"schema_version": "pdf-evaluation-0.1", "corpus_id": "fixture-corpus", "documents": documents}), encoding="utf-8")
    return path


def _dp(reference: str, actual: str) -> int:
    row = list(range(len(actual) + 1))
    for i, char in enumerate(reference, start=1):
        previous, row = row, [i]
        for j, other in enumerate(actual, start=1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (char != other)))
    return row[-1]


@pytest.mark.parametrize("reference,actual,expected", [
    ("kitten", "sitting", 3), ("", "dose", 4), ("dose", "", 4),
    ("µg", "μg", 1), ("≤5", "<5", 1), ("−2", "-2", 1),
    ("no fever", "fever", 3), ("é", "e\u0301", 2), ("2.5 mg", "25 mg", 1),
])
def test_cer_never_folds_clinical_symbols_or_unicode(reference, actual, expected):
    assert character_edit_distance(reference, actual) == expected


def test_bit_vector_distance_matches_independent_dynamic_programming():
    rng = random.Random(912)
    alphabet = "abc µμ−≤<>012.\n"
    for _ in range(300):
        reference = "".join(rng.choice(alphabet) for _ in range(rng.randrange(45)))
        actual = "".join(rng.choice(alphabet) for _ in range(rng.randrange(45)))
        assert character_edit_distance(reference, actual) == _dp(reference, actual)


def test_reports_actual_scores_counts_and_unmeasured_resources(tmp_path):
    reference = "Dose ≤0.5 mg; no fever."
    actual = "Dose <0.5 mg; no fever."
    _bundle(tmp_path, [_block("body", actual)], metrics={"elapsed_seconds": 2.0, "peak_rss_bytes": 123456})
    completed = _document(critical_fixtures=[{"block_id": "body", "expected_text": reference}], citations=[{
        "block_id": "body", "origins": [{"pdf_page_index": 1, "bbox_top_left_normalized": [0.1, 0.1, 0.8, 0.3]}],
    }])
    failed = _document(
        document_id="scan-failed", source_id="failed-fixture", status="failed", bundle=None,
        document_type="clear_scan", profile="english_ocr_v1",
        pages=[{"pdf_page_index": 1, "reference_text": "5.0 µg"}],
    )
    report = evaluate_corpus(_annotations(tmp_path, [completed, failed]))
    assert report["job_counts"] == {"completed": 1, "blocked": 0, "failed": 1, "cancelled": 0}
    assert report["metrics"]["cer"]["character_edits"] == 1 + len("5.0 µg")
    assert report["metrics"]["cer"]["reference_characters"] == len(reference) + len("5.0 µg")
    assert report["metrics"]["cer"]["missing_output_pages"] == 1
    assert report["metrics"]["citations"]["accuracy"] == 1.0
    assert report["metrics"]["critical_fixtures"]["missing_or_incorrect"] == 1
    assert report["corpus"]["verified_distinct_pdf_files"] == 1
    assert report["corpus"]["verified_distinct_annotated_pages"] == 1
    assert report["corpus"]["proposed_minimum_corpus_met"] is False
    assert report["metrics"]["resources"]["peak_vram_bytes"]["observations"] == 0
    assert report["metrics"]["resources"]["seconds_per_requested_page"]["mean"] == 2.0
    assert actual not in json.dumps(report)
    assert "bundle" not in report["documents"][0]


def test_tampered_bundle_cannot_contribute_verified_corpus_or_hide_errors(tmp_path):
    destination = _bundle(tmp_path, [_block("body", "Dose ≤0.5 mg; no fever.")])
    (destination / "document.md").write_text("tampered", encoding="utf-8")
    report = evaluate_corpus(_annotations(tmp_path, [_document()]))
    assert report["invalid_completed_bundles"] == 1
    assert report["job_counts"]["completed"] == 1
    assert report["corpus"]["verified_distinct_pdf_files"] == 0
    assert report["metrics"]["cer"]["value"] == 1.0
    assert report["metrics"]["page_accounting"]["accuracy"] == 0.0
    assert report["errors"] == [{"document_id": "digital-1", "code": "INVALID_BUNDLE"}]


def test_reading_order_citations_and_table_associations_are_separate_measures(tmp_path):
    payload = TablePayload(rows=2, columns=2, cells=[
        TableCell(text="Drug", row=0, column=0, column_header=True),
        TableCell(text="Dose", row=0, column=1, column_header=True),
        TableCell(text="Fixture", row=1, column=0),
        TableCell(text="0.5 mg", row=1, column=1),
    ], footnotes=["Synthetic footnote"])
    text = table_text(payload)
    base = _block("table", text).model_dump(exclude={"kind"})
    table = TableBlock(**base, table=payload)
    _bundle(tmp_path, [_block("before", "first"), _block("header", "Page 1", kind="furniture", index_candidate=False), _block("after", "second"), table])
    expected = payload.model_dump(mode="json")
    expected["cells"][1]["column_header"] = False
    doc = _document(
        pages=[{"pdf_page_index": 1, "reference_text": "first\nsecond\n" + text}],
        reading_order=[{"before": "before", "after": "after"}, {"before": "after", "after": "before"}],
        tables=[{"block_id": "table", "table": expected}],
        citations=[{"block_id": "before", "origins": [{"pdf_page_index": 1, "bbox_top_left_normalized": [0.2, 0.1, 0.8, 0.3]}]}],
    )
    report = evaluate_corpus(_annotations(tmp_path, [doc]))["metrics"]
    assert report["cer"]["value"] == 0.0
    assert report["reading_order"]["accuracy"] == 0.5
    assert report["citations"]["accuracy"] == 0.0
    assert report["tables"]["cell_header_accuracy"] == 0.75
    assert report["tables"]["matching_footnote_sets"] == 1
    assert report["exclusions"]["non_index_candidate_blocks"] == 1


def test_critical_rejection_requires_explicit_block_state_and_linked_issue(tmp_path):
    issue = Issue(issue_id="uncertain-number", category="SYMBOL_UNCERTAIN", block_ids=["body"], pdf_page_indices=[1], explanation="Synthetic number needs source review.")
    block = _block("body", "Dose <0.5 mg; no fever.", extraction_quality="unresolved", issues=[issue.issue_id])
    _bundle(tmp_path, [block], [issue])
    doc = _document(critical_fixtures=[{"block_id": "body", "expected_text": "Dose ≤0.5 mg; no fever."}])
    metrics = evaluate_corpus(_annotations(tmp_path, [doc]))["metrics"]
    assert metrics["critical_fixtures"]["explicit_unresolved_or_excluded_rejections"] == 1
    assert metrics["critical_fixtures"]["exact_or_rejected_accuracy"] == 1.0
    assert metrics["cer"]["value"] > 0


@pytest.mark.parametrize("bundle", ["../outside", "/tmp/outside"])
def test_bundle_paths_cannot_escape_annotation_directory(tmp_path, bundle):
    report = evaluate_corpus(_annotations(tmp_path, [_document(bundle=bundle)]))
    assert report["invalid_completed_bundles"] == 1
    assert report["corpus"]["verified_completed_jobs"] == 0


def test_duplicate_source_pages_and_unpermissioned_ground_truth_are_rejected(tmp_path):
    doc = _document()
    with pytest.raises(ValueError, match="INVALID_EVALUATION"):
        evaluate_corpus(_annotations(tmp_path, [doc, _document(document_id="other-job")]))
    doc.update(synthetic=False, rights_confirmed=False, rights_reference="pending")
    with pytest.raises(ValueError, match="INVALID_EVALUATION"):
        evaluate_corpus(_annotations(tmp_path, [doc]))


def test_empty_reference_insertions_are_counted_without_fabricated_cer(tmp_path):
    _bundle(tmp_path, [_block("body", "unexpected")])
    report = evaluate_corpus(_annotations(tmp_path, [_document(pages=[{"pdf_page_index": 1, "reference_text": ""}])]))["metrics"]["cer"]
    assert report["character_edits"] == len("unexpected")
    assert report["value"] is None
    assert report["empty_reference_pages"] == 1


def test_reference_annotations_bind_original_source_hash(tmp_path):
    _bundle(tmp_path, [_block("body", "Dose ≤0.5 mg; no fever.")])
    report = evaluate_corpus(_annotations(tmp_path, [_document(source_sha256="f" * 64)]))
    assert report["invalid_completed_bundles"] == 1
    assert report["corpus"]["verified_distinct_pdf_files"] == 0


def test_duplicate_pdf_bytes_never_inflate_distinct_document_or_page_counts(tmp_path):
    original = _bundle(tmp_path, [_block("body", "Dose ≤0.5 mg; no fever.")])
    manifest, blocks, review = validate_bundle(original)
    manifest.source.source_id = "other-source"
    manifest.conversion_id = "conversion-2"
    for block in blocks:
        block.source_id = "other-source"
    write_bundle(tmp_path / "other-bundle", manifest, blocks, review)
    docs = [_document(), _document(document_id="other-job", source_id="other-source", bundle="other-bundle")]
    report = evaluate_corpus(_annotations(tmp_path, docs))
    assert report["corpus"]["verified_completed_jobs"] == 2
    assert report["corpus"]["annotated_pages"] == 2
    assert report["corpus"]["verified_distinct_pdf_files"] == 1
    assert report["corpus"]["verified_distinct_annotated_pages"] == 1


def test_ambiguous_duplicate_json_keys_are_rejected(tmp_path):
    path = tmp_path / "annotations.json"
    path.write_text('{"corpus_id":"first","corpus_id":"second","documents":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="INVALID_EVALUATION"):
        evaluate_corpus(path)


def test_final_corpus_requires_explicit_freeze_timestamp(tmp_path):
    path = _annotations(tmp_path, [_document(status="blocked", bundle=None)])
    content = json.loads(path.read_text())
    content["split"] = "final"
    path.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValueError, match="INVALID_EVALUATION"):
        evaluate_corpus(path)
    content["frozen_at"] = "2026-10-09T12:00:00Z"
    path.write_text(json.dumps(content), encoding="utf-8")
    assert evaluate_corpus(path)["split"] == "final"
