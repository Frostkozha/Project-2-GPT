import pytest

from pdf_ingest.import_adapter import iter_passages
from pdf_ingest.provenance import text_hash
from pdf_ingest.review import (
    approve_bundle, approve_table_representation, correct_bundle,
    reclassify_block, resolve_issue, review_status, structured_table_representation,
    verify_page_label,
)
from pdf_ingest.schema import (
    ApprovalRecord, IngestError, Issue, Manifest, Origin, PageInventory,
    ReviewReport, SourceSpec, TableBlock, TableCell, TablePayload, TextBlock,
    table_text,
)
from pdf_ingest.serialize import read_corrections, validate_bundle, write_bundle


STAMP = "2026-10-09T00:00:00+00:00"


def source(**changes):
    values = dict(
        source_id="synthetic-course", source_version="1", title="Synthetic course",
        assignment="Anatomy", rights_reference="fixture-rights", rights_confirmed=True,
        content_approved=True, content_approval_reference="faculty-fixture-1", synthetic=True,
        expected_source_sha256="1" * 64,
    )
    values.update(changes)
    return SourceSpec(**values)


def text_block(identifier="block-1", text="No dose exceeds 0.5 mg; α ≥ 2.", page=1, kind="paragraph", candidate=True):
    return TextBlock(
        block_id=identifier, source_id="synthetic-course", source_version="1", kind=kind,
        text_raw=text, text_normalized=text, text_sha256=text_hash(text),
        origins=[Origin(pdf_page_index=page, bbox_top_left_normalized=(0.1, 0.1, 0.8, 0.5))],
        extraction_method="native_layout", index_candidate=candidate,
    )


def merged_table(identifier="merged-1", page=1):
    table = TablePayload(rows=2, columns=2, cells=[
        TableCell(text="Dose (mg)", row=0, column=0, column_span=2, column_header=True),
        TableCell(text="A", row=1, column=0), TableCell(text="0.5", row=1, column=1),
    ], footnotes=["Do not exceed the stated dose."])
    text = table_text(table)
    return TableBlock(
        block_id=identifier, source_id="synthetic-course", source_version="1", table=table,
        text_raw=text, text_normalized=text, text_sha256=text_hash(text),
        origins=[Origin(pdf_page_index=page, bbox_top_left_normalized=(0.1, 0.1, 0.8, 0.5))],
        extraction_method="native_layout",
    )


def make_bundle(tmp_path, blocks=None, issues=None, pages=None, source_record=None, rotations=None):
    blocks = [text_block()] if blocks is None else blocks
    issues = [] if issues is None else issues
    pages = [1] if pages is None else pages
    for issue in issues:
        for block in blocks:
            if block.block_id in issue.block_ids:
                block.issues.append(issue.issue_id)
    inventory = [PageInventory(
        pdf_page_index=page, width=612.0, height=792.0,
        rotation=(rotations or {}).get(page, 0), native_text_chars=100,
        status="extracted", block_ids=[block.block_id for block in blocks if any(origin.pdf_page_index == page for origin in block.origins)],
    ) for page in pages]
    manifest = Manifest(
        conversion_id="fixture-conversion", source=source_record or source(),
        source_sha256="1" * 64, source_page_count=max(pages), requested_pages=pages,
        profile="native_layout_v1", conversion_fingerprint="2" * 64,
        dependency_lock_sha256="3" * 64, converter_version="0.1.0",
        model_revisions={"layout": "fixture"}, normalization_version="nfc-lf-v1",
        serialization_version="markdown-v1", created_at=STAMP,
        review_state="needs_review" if issues else "unreviewed", page_inventory=inventory,
        idempotency_key="4" * 64,
    )
    return write_bundle(tmp_path / "original", manifest, blocks, ReviewReport(issues=issues))


def test_completed_is_not_approval(tmp_path):
    bundle = make_bundle(tmp_path)
    with pytest.raises(IngestError, match="BUNDLE_UNAPPROVED"):
        list(iter_passages(bundle, source()))


def test_current_trusted_source_hash_required_and_must_match(tmp_path):
    bundle = make_bundle(tmp_path)
    with pytest.raises(IngestError, match="HASH_MISMATCH"):
        approve_bundle(bundle, source(expected_source_sha256=None), "reviewer")
    approve_bundle(bundle, source(), "reviewer")
    with pytest.raises(IngestError, match="HASH_MISMATCH"):
        list(iter_passages(bundle, source(expected_source_sha256="9" * 64)))
    manifest, blocks, report = validate_bundle(bundle)
    manifest.source_sha256 = "8" * 64
    forged = write_bundle(tmp_path / "wrong-source-hash", manifest, blocks, report)
    with pytest.raises(IngestError, match="HASH_MISMATCH"):
        list(iter_passages(forged, source()))


def test_import_reads_canonical_text_and_never_non_evidence(tmp_path):
    blocks = [text_block()] + [text_block(f"marker-{kind}", f"REVIEW PLACEHOLDER {kind}", kind=kind) for kind in ("figure", "formula", "furniture", "placeholder")]
    bundle = make_bundle(tmp_path, blocks=blocks)
    approve_bundle(bundle, source(), "reviewer")
    passages = list(iter_passages(bundle, source()))
    assert len(passages) == 1
    assert passages[0]["text"] == blocks[0].text_normalized
    assert passages[0]["origins"][0]["pdf_page_index"] == 1
    assert "<!--" not in passages[0]["text"]
    assert set(passages[0]) == {"text", "tenant_id", "source_id", "source_version", "title", "assignment", "block_id", "section_path", "origins", "conversion_id", "source_page_count", "requested_pages", "permitted_subset_reference", "approval_references"}


@pytest.mark.parametrize("changes,code", [
    ({"eligible": False}, "SOURCE_INELIGIBLE"),
    ({"rights_confirmed": False}, "RIGHTS_UNCONFIRMED"),
    ({"content_approved": False, "content_approval_reference": None}, "CONTENT_UNAPPROVED"),
    ({"tenant_id": "another-tenant"}, "SOURCE_MISMATCH"),
    ({"source_version": "2"}, "SOURCE_MISMATCH"),
    ({"title": "another-title"}, "SOURCE_MISMATCH"),
    ({"rights_reference": "different-rights"}, "SOURCE_MISMATCH"),
    ({"content_approval_reference": "faculty-replaced"}, "STALE_CONTENT_APPROVAL"),
])
def test_current_source_rechecked_after_approval(tmp_path, changes, code):
    bundle = make_bundle(tmp_path)
    approve_bundle(bundle, source(), "reviewer")
    with pytest.raises(IngestError, match=code):
        list(iter_passages(bundle, source(**changes)))


def test_private_conversion_can_later_receive_current_content_signoff(tmp_path):
    bundle = make_bundle(tmp_path, source_record=source(content_approved=False, content_approval_reference=None))
    with pytest.raises(IngestError, match="CONTENT_UNAPPROVED"):
        approve_bundle(bundle, source(content_approved=False, content_approval_reference=None), "reviewer")
    approve_bundle(bundle, source(), "reviewer")
    assert len(list(iter_passages(bundle, source()))) == 1
    manifest, _, _ = validate_bundle(bundle)
    assert manifest.source.content_approved is False  # Registration history remains immutable.


def test_unresolved_issue_blocks_approval_and_resolution_is_audited(tmp_path):
    issue = Issue(issue_id="numeric-warning", category="SYMBOL_UNCERTAIN", explanation="Compare source number.", pdf_page_indices=[1], block_ids=["block-1"])
    bundle = make_bundle(tmp_path, issues=[issue])
    with pytest.raises(IngestError, match="UNRESOLVED_ISSUES"):
        approve_bundle(bundle, source(), "reviewer")
    resolve_issue(bundle, "numeric-warning", "approved", "source-reviewer")
    _, _, report = validate_bundle(bundle)
    assert report.issues[0].reviewer == "source-reviewer"
    assert report.issues[0].resolved_at is not None
    approve_bundle(bundle, source(), "faculty-reviewer")
    assert len(list(iter_passages(bundle, source()))) == 1
    resolve_issue(bundle, "numeric-warning", "approved", "new-reviewer")
    assert review_status(bundle)["review_state"] == "needs_review"
    with pytest.raises(IngestError, match="BUNDLE_UNAPPROVED"):
        list(iter_passages(bundle, source()))


def test_evidence_exclusion_requires_individual_resolution_and_faculty_subset(tmp_path):
    issue = Issue(issue_id="omit-block", category="READING_ORDER_UNCERTAIN", explanation="Omit explicitly permitted block.", pdf_page_indices=[1], block_ids=["block-2"])
    bundle = make_bundle(tmp_path, blocks=[text_block(), text_block("block-2", "Omitted material")], issues=[issue])
    resolve_issue(bundle, "omit-block", "excluded", "operator")
    with pytest.raises(IngestError, match="faculty permitted subset"):
        approve_bundle(bundle, source(), "reviewer")
    approve_bundle(bundle, source(), "reviewer", permitted_subset_reference="faculty-subset-with-omissions")
    assert [item["block_id"] for item in iter_passages(bundle, source())] == ["block-1"]


def test_false_candidate_flag_cannot_silently_omit_evidence(tmp_path):
    bundle = make_bundle(tmp_path, blocks=[text_block(), text_block("omitted", "Hidden evidence", candidate=False)])
    with pytest.raises(IngestError, match="UNREVIEWED_EXCLUSION"):
        approve_bundle(bundle, source(), "reviewer", permitted_subset_reference="subset")


def test_exclusion_can_be_reversed_by_individual_review(tmp_path):
    issue = Issue(issue_id="omit-block", category="READING_ORDER_UNCERTAIN", explanation="Compare omitted block.", pdf_page_indices=[1], block_ids=["block-2"])
    bundle = make_bundle(tmp_path, blocks=[text_block(), text_block("block-2", "Recovered material")], issues=[issue])
    resolve_issue(bundle, "omit-block", "excluded", "operator")
    resolve_issue(bundle, "omit-block", "approved", "operator")
    approve_bundle(bundle, source(), "reviewer")
    assert len(list(iter_passages(bundle, source()))) == 2


def test_empty_text_cannot_be_approved_as_evidence(tmp_path):
    bundle = make_bundle(tmp_path, blocks=[text_block(text="")])
    with pytest.raises(IngestError, match="UNRESOLVED_CONTENT"):
        approve_bundle(bundle, source(), "reviewer")


def test_partial_direct_scope_cannot_mark_whole_bundle_approved(tmp_path):
    bundle = make_bundle(tmp_path, blocks=[text_block(), text_block("block-2")])
    with pytest.raises(IngestError, match="INCOMPLETE_REVIEW_SCOPE"):
        approve_bundle(bundle, source(), "reviewer", block_ids=["block-1"])
    assert review_status(bundle)["review_state"] == "unreviewed"


def test_small_source_sample_requires_every_requested_page(tmp_path):
    bundle = make_bundle(tmp_path, blocks=[text_block(f"block-{p}", page=p) for p in range(1, 5)], pages=list(range(1, 5)))
    with pytest.raises(IngestError, match="INSUFFICIENT_REVIEW_SAMPLE"):
        approve_bundle(bundle, source(), "reviewer", method="source_level_sampled", sampled_pages=[1, 2, 3])
    approve_bundle(bundle, source(), "reviewer", method="source_level_sampled", sampled_pages=[1, 2, 3, 4])
    assert len(list(iter_passages(bundle, source()))) == 4


def test_large_source_sample_requires_twenty_and_page_types(tmp_path):
    pages = list(range(1, 26))
    bundle = make_bundle(tmp_path, blocks=[text_block(f"block-{p}", page=p) for p in pages], pages=pages, rotations={25: 90})
    with pytest.raises(IngestError, match="INSUFFICIENT_REVIEW_SAMPLE"):
        approve_bundle(bundle, source(), "reviewer", method="source_level_sampled", sampled_pages=list(range(1, 20)))
    with pytest.raises(IngestError, match="every source page type"):
        approve_bundle(bundle, source(), "reviewer", method="source_level_sampled", sampled_pages=list(range(1, 21)))
    approve_bundle(bundle, source(), "reviewer", method="source_level_sampled", sampled_pages=list(range(1, 20)) + [25])
    assert len(list(iter_passages(bundle, source()))) == 25


def test_sampling_uses_individual_direct_audit_for_flagged_blocks(tmp_path):
    issue = Issue(issue_id="check-symbol", category="SYMBOL_UNCERTAIN", explanation="Compare α.", pdf_page_indices=[1], block_ids=["block-1"])
    bundle = make_bundle(tmp_path, blocks=[text_block(), text_block("block-2", page=2)], issues=[issue], pages=[1, 2])
    resolve_issue(bundle, "check-symbol", "approved", "symbol-reviewer")
    approve_bundle(bundle, source(), "sample-reviewer", method="source_level_sampled", sampled_pages=[1, 2])
    _, _, report = validate_bundle(bundle)
    sampled = next(item for item in report.approvals if item.method == "source_level_sampled")
    direct = next(item for item in report.approvals if item.method == "direct")
    assert sampled.block_ids == ["block-2"]
    assert direct.block_ids == ["block-1"]
    assert direct.reviewer == "symbol-reviewer"


def test_merged_table_cannot_be_flattened_before_representation_review(tmp_path):
    bundle = make_bundle(tmp_path, blocks=[merged_table()])
    with pytest.raises(IngestError, match="merged-table representation"):
        approve_bundle(bundle, source(), "reviewer")
    with pytest.raises(IngestError, match="structured table-representation"):
        correct_bundle(bundle, "merged-1", "A: 0.5 mg", "Compared source", "reviewer", tmp_path)
    representation = "Dose (mg): A = 0.5.\nFootnote: Do not exceed the stated dose."
    updated = approve_table_representation(bundle, "merged-1", representation, "Compared merged header and footnote", "table-reviewer", tmp_path)
    _, blocks, report = validate_bundle(updated)
    assert blocks[0].text_raw == merged_table().text_raw
    assert blocks[0].table.cells[0].column_span == 2
    assert not report.approvals
    approve_bundle(updated, source(), "reviewer")
    assert list(iter_passages(updated, source()))[0]["text"] == representation


def test_structured_table_proposal_preserves_span_header_and_footnote_metadata():
    table = merged_table().table
    representation = structured_table_representation(table)
    assert representation == structured_table_representation(table)
    assert '"column_span":2' in representation
    assert '"column_header":true' in representation
    assert '"text":"Dose (mg)"' in representation
    assert "Footnote 1:" in representation and table.footnotes[0] in representation
    assert table.approved_representation is None


def test_legitimate_margin_text_can_be_reclassified_without_rewriting(tmp_path):
    block = text_block("margin", "No treatment is recommended.", kind="furniture", candidate=False)
    bundle = make_bundle(tmp_path, blocks=[block])
    updated = reclassify_block(bundle, "margin", "paragraph", "Verified legitimate margin paragraph", "operator", tmp_path)
    _, blocks, report = validate_bundle(updated)
    assert blocks[0].text_raw == block.text_raw
    assert blocks[0].text_normalized == block.text_normalized
    assert blocks[0].kind == "paragraph" and blocks[0].index_candidate
    assert report.issues[0].category == "BLOCK_RECLASSIFIED"
    assert not report.approvals
    approve_bundle(updated, source(), "reviewer")
    assert list(iter_passages(updated, source()))[0]["text"] == block.text_normalized
    furniture = reclassify_block(updated, blocks[0].block_id, "furniture", "Verified page furniture", "operator", tmp_path)
    assert not validate_bundle(furniture)[1][0].index_candidate


def test_reclassification_cannot_turn_formula_or_image_into_evidence(tmp_path):
    bundle = make_bundle(tmp_path, blocks=[text_block("formula", "Unresolved formula", kind="formula", candidate=False)])
    with pytest.raises(IngestError, match="Only original paragraph/furniture"):
        reclassify_block(bundle, "formula", "paragraph", "Unsupported change", "operator", tmp_path)


def test_correction_is_immutable_new_version_and_preserves_raw(tmp_path):
    bundle = make_bundle(tmp_path)
    approve_bundle(bundle, source(), "reviewer")
    before = {path.name: path.read_bytes() for path in bundle.iterdir() if path.is_file()}
    updated = correct_bundle(bundle, "block-1", "No dose exceeds 0.25 mg.\nα ≥ 2.", "Compared original page number", "operator", tmp_path)
    manifest, blocks, report = validate_bundle(updated)
    assert {path.name: path.read_bytes() for path in bundle.iterdir() if path.is_file()} == before
    assert manifest.parent_conversion_id == "fixture-conversion"
    assert manifest.conversion_id != "fixture-conversion"
    assert manifest.conversion_fingerprint != "2" * 64
    assert blocks[0].block_id != "block-1"
    assert blocks[0].text_raw == text_block().text_raw
    assert blocks[0].text_normalized == "No dose exceeds 0.25 mg.\nα ≥ 2."
    assert blocks[0].markdown_span.end_line - blocks[0].markdown_span.start_line == 1
    assert blocks[0].text_sha256 == text_hash(blocks[0].text_normalized)
    assert not report.approvals and manifest.approval_references == []
    history = read_corrections(updated)
    assert history[0].original_text == text_block().text_normalized
    assert history[0].new_text == blocks[0].text_normalized
    with pytest.raises(IngestError, match="BUNDLE_UNAPPROVED"):
        list(iter_passages(updated, source()))
    again = correct_bundle(updated, blocks[0].block_id, "No dose exceeds 0.125 mg.", "Second source comparison", "operator", tmp_path)
    assert len(read_corrections(again)) == 2
    assert validate_bundle(again)[1][0].text_raw == text_block().text_raw


def test_corrected_flagged_block_reopens_resolution_and_links_patch(tmp_path):
    issue = Issue(issue_id="numeric-warning", category="SYMBOL_UNCERTAIN", explanation="Compare source number.", pdf_page_indices=[1], block_ids=["block-1"])
    bundle = make_bundle(tmp_path, issues=[issue])
    resolve_issue(bundle, "numeric-warning", "approved", "reviewer")
    approve_bundle(bundle, source(), "reviewer")
    updated = correct_bundle(bundle, "block-1", "0.25 mg", "Correct number from PDF", "operator", tmp_path)
    _, blocks, report = validate_bundle(updated)
    assert report.issues[0].resolution is None
    assert report.issues[0].block_ids == [blocks[0].block_id]
    assert report.issues[0].correction_ids == [read_corrections(updated)[0].correction_id]


def test_verified_printed_label_preserves_physical_page_and_invalidates_approval(tmp_path):
    bundle = make_bundle(tmp_path, blocks=[text_block(page=12)], pages=[12])
    approve_bundle(bundle, source(), "reviewer", permitted_subset_reference="faculty-page-12-excerpt")
    updated = verify_page_label(bundle, 12, "8", "label-reviewer", tmp_path)
    manifest, blocks, report = validate_bundle(updated)
    assert manifest.requested_pages == [12]
    assert manifest.page_inventory[0].printed_label == "8"
    assert manifest.page_inventory[0].printed_label_verified
    assert blocks[0].origins[0].pdf_page_index == 12
    assert blocks[0].origins[0].printed_label == "8"
    assert report.issues[-1].category == "PRINTED_LABEL_VERIFIED"
    assert report.issues[-1].reviewer == "label-reviewer"
    assert not report.approvals
    approve_bundle(updated, source(), "reviewer", permitted_subset_reference="faculty-page-12-excerpt")
    assert list(iter_passages(updated, source()))[0]["origins"][0]["printed_label"] == "8"


def test_page_range_requires_faculty_subset_and_exports_omissions_scope(tmp_path):
    bundle = make_bundle(tmp_path, blocks=[text_block(page=12)], pages=[12])
    with pytest.raises(IngestError, match="faculty permitted subset"):
        approve_bundle(bundle, source(), "reviewer")
    approve_bundle(bundle, source(), "reviewer", permitted_subset_reference="faculty-approved-page-12-excerpt")
    passage = list(iter_passages(bundle, source()))[0]
    assert passage["source_page_count"] == 12
    assert passage["requested_pages"] == [12]
    assert passage["permitted_subset_reference"] == "faculty-approved-page-12-excerpt"


def test_empty_unresolved_page_cannot_be_declared_extracted_by_issue_resolution(tmp_path):
    issue = Issue(issue_id="scan-page", category="OCR_REQUIRED", explanation="Native extraction yielded no evidence.", pdf_page_indices=[1], block_ids=["placeholder"])
    bundle = make_bundle(tmp_path, blocks=[text_block("placeholder", "OCR required", kind="placeholder", candidate=False), text_block("block-2", page=2)], issues=[issue], pages=[1, 2])
    manifest, blocks, report = validate_bundle(bundle)
    manifest.page_inventory[0].status = "unresolved"
    unresolved = write_bundle(tmp_path / "unresolved", manifest, blocks, report)
    resolve_issue(unresolved, "scan-page", "approved", "operator")
    assert validate_bundle(unresolved)[0].page_inventory[0].status == "unresolved"
    with pytest.raises(IngestError, match="PAGE_UNACCOUNTED"):
        approve_bundle(unresolved, source(), "reviewer")
    resolve_issue(unresolved, "scan-page", "excluded", "operator")
    approve_bundle(unresolved, source(), "reviewer", permitted_subset_reference="faculty-permits-native-page-2-only")
    assert [passage["block_id"] for passage in iter_passages(unresolved, source())] == ["block-2"]


def test_markdown_edit_rejected_even_after_approval(tmp_path):
    bundle = make_bundle(tmp_path)
    approve_bundle(bundle, source(), "reviewer")
    with (bundle / "document.md").open("a") as file:
        file.write("Unauthorized prose\n")
    with pytest.raises(IngestError, match="HASH_MISMATCH"):
        list(iter_passages(bundle, source()))


def test_import_independently_rejects_forged_incomplete_review_scope(tmp_path):
    bundle = make_bundle(tmp_path, blocks=[text_block(), text_block("block-2")])
    manifest, blocks, report = validate_bundle(bundle)
    manifest.review_state = "approved"
    manifest.approval_references = ["faculty-fixture-1"]
    for block in blocks:
        block.extraction_quality = "approved"
    report.approvals = [ApprovalRecord(reviewer="reviewer", timestamp=STAMP, method="direct", block_ids=["block-1"], content_approval_reference="faculty-fixture-1")]
    forged = write_bundle(tmp_path / "forged", manifest, blocks, report)
    with pytest.raises(IngestError, match="INCOMPLETE_REVIEW_SCOPE"):
        list(iter_passages(forged, source()))
