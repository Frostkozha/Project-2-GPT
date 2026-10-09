"""Adapter contract tests use real Docling records, not measured layout accuracy."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdf_ingest.extract import _adapt_document, _make_converter, extract_document
from pdf_ingest.provisioning import DOCLING_VERSION, LEDGER_VERSION, MODEL_SPECS, load_model_ledger, provision_models, validate_models
from pdf_ingest.quality import check_quality
from pdf_ingest.schema import IngestError, PageInventory, SourceSpec, TableBlock

core = pytest.importorskip("docling_core.types.doc")


def source() -> SourceSpec:
    return SourceSpec(source_id="fixture", source_version="1", title="Synthetic source", assignment="tests", rights_reference="fixture", synthetic=True)


def inventory(index: int = 12, *, rotation: int = 0, verified: bool = False) -> PageInventory:
    return PageInventory(pdf_page_index=index, width=200.0, height=400.0, rotation=rotation, printed_label="8", printed_label_verified=verified, native_text_chars=80, status="extracted")


def prov(page: int = 12, *, origin=None):
    origin = origin or core.CoordOrigin.BOTTOMLEFT
    if origin == core.CoordOrigin.BOTTOMLEFT:
        box = core.BoundingBox(l=20, t=360, r=180, b=320, coord_origin=origin)
    else:
        box = core.BoundingBox(l=20, t=40, r=180, b=80, coord_origin=origin)
    return core.ProvenanceItem(page_no=page, bbox=box, charspan=(0, 100))


def document(*indices: int):
    doc = core.DoclingDocument(name="fixture")
    for index in indices or (12,):
        doc.add_page(index, core.Size(width=200.0, height=400.0))
    return doc


def adapt(doc, page_inventory=None):
    pages = page_inventory or {12: inventory()}
    return _adapt_document(doc, source(), "a" * 64, set(pages), pages, min(pages), "native_layout", [])


def test_original_page_indices_labels_and_bottom_left_coordinates():
    doc = document(12)
    doc.add_heading("Cell biology", level=2, prov=prov())
    doc.add_text(core.DocItemLabel.TEXT, "No increase: −2.5 µg/mL; α ≥ 3; cm².", prov=prov())
    blocks, _, sections = adapt(doc, {12: inventory(verified=True)})
    assert sections == ["Cell biology"]
    assert blocks[1].text_raw == blocks[1].text_normalized == "No increase: −2.5 µg/mL; α ≥ 3; cm²."
    origin = blocks[1].origins[0]
    assert origin.pdf_page_index == 12
    assert origin.printed_label == "8"
    assert origin.bbox_top_left_normalized == (0.1, 0.1, 0.9, 0.2)
    assert blocks[1].section_path == ["Cell biology"]
    assert blocks[1].text_sha256 == hashlib.sha256(blocks[1].text_normalized.encode()).hexdigest()


def test_unverified_printed_label_never_becomes_block_locator():
    doc = document(12)
    doc.add_text(core.DocItemLabel.TEXT, "A paragraph with original physical page index.", prov=prov())
    blocks, _, _ = adapt(doc)
    assert blocks[0].origins[0].printed_label is None


def test_backend_rotation_is_not_applied_twice():
    doc = document(12)
    doc.add_text(core.DocItemLabel.TEXT, "Already in display coordinates.", prov=prov(origin=core.CoordOrigin.TOPLEFT))
    blocks, _, _ = adapt(doc, {12: inventory(rotation=180)})
    assert blocks[0].origins[0].bbox_top_left_normalized == (0.1, 0.1, 0.9, 0.2)
    assert "rotation already applied" in blocks[0].origins[0].transformation


def test_multi_page_paragraph_retains_every_origin():
    doc = document(12, 13)
    paragraph = doc.add_text(core.DocItemLabel.TEXT, "A paragraph continues across a physical page boundary.", prov=prov(12))
    paragraph.prov.append(prov(13))
    blocks, _, _ = adapt(doc, {12: inventory(12), 13: inventory(13)})
    assert [o.pdf_page_index for o in blocks[0].origins] == [12, 13]


def test_unverified_page_number_convention_fails_closed():
    doc = document(1)
    doc.add_text(core.DocItemLabel.TEXT, "Wrongly renumbered range.", prov=prov(1))
    with pytest.raises(IngestError) as error:
        adapt(doc)
    assert error.value.code == "INVALID_PROVENANCE"


def test_tables_keep_merged_cells_header_flags_units_and_footnotes():
    doc = document(12)
    footnote = doc.add_text(core.DocItemLabel.FOOTNOTE, "* Values in µg/mL.", prov=prov())
    table = doc.add_table(core.TableData(num_rows=2, num_cols=2, table_cells=[
        core.TableCell(text="Dose", start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=0, end_col_offset_idx=2, col_span=2, column_header=True),
        core.TableCell(text="−2.5", start_row_offset_idx=1, end_row_offset_idx=2, start_col_offset_idx=0, end_col_offset_idx=1),
        core.TableCell(text="No increase", start_row_offset_idx=1, end_row_offset_idx=2, start_col_offset_idx=1, end_col_offset_idx=2),
    ]), prov=prov())
    table.footnotes.append(footnote.get_ref())
    blocks, _, _ = adapt(doc)
    assert len(blocks) == 1
    block = blocks[0]
    assert isinstance(block, TableBlock)
    assert block.table.cells[0].column_span == 2
    assert block.table.cells[0].column_header
    assert block.table.footnotes == ["* Values in µg/mL."]
    assert block.text_normalized == "Dose\t\n−2.5\tNo increase\n* Values in µg/mL."
    issues = check_quality(blocks, [inventory()], "native_layout_v1")
    assert "TABLE_STRUCTURE_UNRESOLVED" in {issue.category for issue in issues}
    assert issues[0].issue_id in block.issues


def test_formula_figure_and_furniture_preserved_but_not_evidence():
    doc = document(12)
    # Stock Docling keeps unresolved formula wording in orig, leaving text empty.
    doc.add_text(core.DocItemLabel.FORMULA, "", orig="E = mc²", prov=prov())
    doc.add_picture(prov=prov())
    doc.add_text(core.DocItemLabel.PAGE_HEADER, "Potentially meaningful margin text", prov=prov(), content_layer=core.ContentLayer.FURNITURE)
    blocks, _, _ = adapt(doc)
    assert {block.kind for block in blocks} == {"formula", "figure", "furniture"}
    assert all(not block.index_candidate for block in blocks)
    issues = check_quality(blocks, [inventory()], "native_layout_v1")
    assert {issue.category for issue in issues} >= {"UNRESOLVED_CONTENT", "FURNITURE_REVIEW_REQUIRED"}
    assert any(block.text_raw == "Potentially meaningful margin text" for block in blocks)
    assert next(block.text_raw for block in blocks if block.kind == "formula") == "E = mc²"


def test_missing_bbox_is_null_with_review_issue():
    doc = document(12)
    doc.add_text(core.DocItemLabel.TEXT, "Native source text without region metadata.")
    blocks, _, _ = adapt(doc)
    assert blocks[0].origins[0].bbox_top_left_normalized is None
    issues = check_quality(blocks, [inventory()], "native_layout_v1")
    assert "MISSING_BBOX" in {issue.category for issue in issues}


def test_critical_numeric_symbol_and_negation_disagreement_requires_review():
    doc = document(12)
    doc.add_text(core.DocItemLabel.TEXT, "Increase 2.5 mg; α >= 3.", prov=prov())
    blocks, _, _ = adapt(doc)
    issues = check_quality(blocks, [inventory()], "native_layout_v1", {12: "No increase −2.5 µg; α ≥ 3."})
    assert {issue.category for issue in issues} >= {"TEXT_LAYER_UNRELIABLE", "SYMBOL_UNCERTAIN"}
    assert blocks[0].text_normalized == "Increase 2.5 mg; α >= 3."


def test_english_ocr_requires_individual_review():
    doc = document(12)
    doc.add_text(core.DocItemLabel.TEXT, "A clear printed English text transcription.", prov=prov())
    blocks, _, _ = _adapt_document(doc, source(), "a" * 64, {12}, {12: inventory()}, 12, "english_ocr", [])
    issues = check_quality(blocks, [inventory()], "english_ocr_v1")
    assert "OCR_REVIEW_REQUIRED" in {issue.category for issue in issues}


def write_models(root: Path):
    names = [
        "docling-project--docling-layout-heron/config.json",
        "docling-project--docling-layout-heron/preprocessor_config.json",
        "docling-project--docling-layout-heron/model.safetensors",
        "docling-project--docling-models/model_artifacts/tableformer/accurate/tm_config.json",
        "docling-project--docling-models/model_artifacts/tableformer/accurate/tableformer.safetensors",
    ]
    files = {}
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test-only-artifact")
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    ledger = {"schema_version": LEDGER_VERSION, "docling_version": DOCLING_VERSION, "models": {repo: revision for repo, revision, _ in MODEL_SPECS}, "files": files, "ocr": None}
    (root / "models.json").write_text(json.dumps(ledger))
    return ledger


def test_missing_models_fail_before_any_converter_or_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("pdf_ingest.extract._make_converter", lambda *_: pytest.fail("model failure must precede converter construction"))
    with pytest.raises(IngestError) as error:
        extract_document(tmp_path / "source.pdf", source(), "native_layout_v1", tmp_path / "models", "a" * 64, [12], [inventory()])
    assert error.value.code == "MODEL_UNAVAILABLE"


def test_local_model_hashes_enforced_and_ocr_unavailable(tmp_path):
    write_models(tmp_path)
    revisions = validate_models(tmp_path)
    assert revisions["docling"] == DOCLING_VERSION
    with pytest.raises(IngestError) as error:
        validate_models(tmp_path, "english_ocr_v1")
    assert error.value.code == "OCR_UNAVAILABLE"
    (tmp_path / "docling-project--docling-layout-heron/model.safetensors").write_bytes(b"changed")
    with pytest.raises(IngestError) as error:
        load_model_ledger(tmp_path)
    assert error.value.code == "MODEL_UNAVAILABLE"


def test_local_model_symlink_escape_rejected(tmp_path):
    write_models(tmp_path)
    model = tmp_path / "docling-project--docling-layout-heron/model.safetensors"
    model.unlink()
    outside = tmp_path.parent / "outside-weight-test"
    outside.write_bytes(b"test-only-artifact")
    model.symlink_to(outside)
    with pytest.raises(IngestError) as error:
        validate_models(tmp_path)
    assert error.value.code == "MODEL_UNAVAILABLE"


@pytest.mark.parametrize("corrupt_download", [False, True])
def test_provisioning_checks_authoritative_hashes_before_publication(tmp_path, monkeypatch, corrupt_download):
    import huggingface_hub
    payload = b"synthetic checksum test artifact; never used for inference"
    files = {
        "docling-project/docling-layout-heron": ["config.json", "preprocessor_config.json", "model.safetensors"],
        "docling-project/docling-models": ["model_artifacts/tableformer/accurate/tm_config.json", "model_artifacts/tableformer/accurate/tableformer_accurate.safetensors"],
    }
    calls = []

    class Api:
        def model_info(self, repo_id, revision, files_metadata):
            assert files_metadata is True
            assert revision == next(commit for repo, commit, _ in MODEL_SPECS if repo == repo_id)
            calls.append((repo_id, revision))
            siblings = []
            for name in files[repo_id]:
                lfs = SimpleNamespace(sha256=hashlib.sha256(payload).hexdigest()) if name.endswith(".safetensors") else None
                blob = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()
                siblings.append(SimpleNamespace(rfilename=name, lfs=lfs, blob_id=blob))
            return SimpleNamespace(sha=revision, siblings=siblings)

    def snapshot_download(*, repo_id, revision, local_dir, cache_dir, allow_patterns, max_workers):
        assert len(revision) == 40
        assert cache_dir.is_relative_to(tmp_path)
        assert max_workers == 2
        for name in files[repo_id]:
            target = local_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"corrupted" if corrupt_download and name == "model.safetensors" else payload)
        return str(local_dir)

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    destination = tmp_path / "models"
    if corrupt_download:
        with pytest.raises(IngestError) as error:
            provision_models(destination)
        assert error.value.code == "MODEL_UNAVAILABLE"
        assert not destination.exists()
    else:
        revisions = provision_models(destination)
        assert len(calls) == 2
        assert revisions["docling-project/docling-layout-heron"] == MODEL_SPECS[0][1]
        assert (destination / "models.json").is_file()


def test_real_pinned_pipeline_options_disable_rewriting_and_remote_features(tmp_path, monkeypatch):
    ledger = write_models(tmp_path)
    monkeypatch.setattr("pdf_ingest.extract.load_model_ledger", lambda *_: ledger)
    converter = _make_converter(tmp_path, "native_layout_v1", 2, 120)
    option = converter.format_to_options[__import__("docling.datamodel.base_models", fromlist=["InputFormat"]).InputFormat.PDF]
    options = option.pipeline_options
    assert options.do_ocr is False
    assert options.do_table_structure is True
    assert options.enable_remote_services is False
    assert options.do_formula_enrichment is False
    assert options.do_picture_description is False
    assert options.artifacts_path == tmp_path
    assert option.pipeline_cls.__name__ == "ConservativePdfPipeline"
    assert option.backend.__name__ == "PyPdfiumDocumentBackend"
    assert options.layout_options.engine_options.compile_model is False


def test_conservative_assembly_preserves_fraction_slashes_quotes_and_hyphens(tmp_path, monkeypatch):
    ledger = write_models(tmp_path)
    monkeypatch.setattr("pdf_ingest.extract.load_model_ledger", lambda *_: ledger)
    from docling.datamodel.base_models import InputFormat
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
    monkeypatch.setattr(StandardPdfPipeline, "_init_models", lambda self: None)
    option = _make_converter(tmp_path, "native_layout_v1", 2, 120).format_to_options[InputFormat.PDF]
    pipeline = option.pipeline_cls(option.pipeline_options)
    assert pipeline.assemble_model.sanitize_text(["ﬁ cross-", "section 1⁄2 ‘dose’ • α µg"]) == "ﬁ cross-\nsection 1⁄2 ‘dose’ • α µg"
    doc = document(12, 13)
    item = doc.add_text(core.DocItemLabel.TEXT, "cross-", prov=prov(12))
    element = SimpleNamespace(label=core.DocItemLabel.TEXT)
    continuation = SimpleNamespace(label=core.DocItemLabel.TEXT, text="section", page_no=13, cluster=SimpleNamespace(bbox=prov(13).bbox), hyperlink=None)
    pipeline.reading_order_model._merge_elements(element, continuation, item, 400.)
    assert item.text == item.orig == "cross-\nsection"
    assert [p.page_no for p in item.prov] == [12, 13]


def test_list_enumeration_preserved_from_original_extraction():
    doc = document(12)
    doc.add_text(core.DocItemLabel.LIST_ITEM, "No increase 2.5 mg", orig="1. No increase 2.5 mg", prov=prov())
    blocks, _, _ = adapt(doc)
    assert blocks[0].kind == "list"
    assert blocks[0].text_raw == blocks[0].text_normalized == "1. No increase 2.5 mg"


def test_extract_page_range_keeps_original_indices_with_diagnostic_comparison(tmp_path, monkeypatch):
    from reportlab.pdfgen.canvas import Canvas
    pdf = tmp_path / "fixture.pdf"
    canvas = Canvas(str(pdf))
    for index in range(1, 13):
        canvas.drawString(50, 700, "No increase 2.5 mg on original page 12." if index == 12 else f"Page {index}")
        canvas.showPage()
    canvas.save()
    doc = document(12)
    doc.add_text(core.DocItemLabel.TEXT, "No increase 2.5 mg on original page 12.", prov=prov())
    calls = []

    class Converter:
        def convert(self, path, *, page_range, raises_on_error):
            calls.append(page_range)
            return SimpleNamespace(status="success", document=doc, errors=[])

    monkeypatch.setattr("pdf_ingest.extract.validate_models", lambda *_: {"fixture": "local"})
    monkeypatch.setattr("pdf_ingest.extract._make_converter", lambda *_: Converter())
    blocks, issues, pages, _ = extract_document(pdf, source(), "native_layout_v1", tmp_path, "a" * 64, [12], [inventory()])
    assert calls == [(12, 12)]
    assert blocks[0].origins[0].pdf_page_index == 12
    assert pages[0].status == "extracted"
    assert pages[0].block_ids == [blocks[0].block_id]
    assert "SYMBOL_UNCERTAIN" not in {issue.category for issue in issues}


def test_page_timeout_stops_adapter_without_fallback(tmp_path, monkeypatch):
    from reportlab.pdfgen.canvas import Canvas
    pdf = tmp_path / "fixture.pdf"
    canvas = Canvas(str(pdf))
    canvas.drawString(50, 700, "A fixture paragraph.")
    canvas.save()

    class Converter:
        def convert(self, *_args, **_kwargs):
            time.sleep(.2)
            pytest.fail("deadline must stop hung extraction")

    monkeypatch.setattr("pdf_ingest.extract.validate_models", lambda *_: {})
    monkeypatch.setattr("pdf_ingest.extract._make_converter", lambda *_: Converter())
    with pytest.raises(IngestError) as error:
        extract_document(pdf, source(), "native_layout_v1", tmp_path, "a" * 64, [1], [inventory(1)], page_timeout_seconds=.05)
    assert error.value.code == "DEADLINE_EXCEEDED"
