"""Model-backed smoke test, distinct from adapter/contract tests.

Skipped explicitly without provisioned local artifacts. No fixture engine is
substituted for Docling, and no model download happens from a test.
"""
import hashlib
from pathlib import Path

import pytest

from pdf_ingest.jobs import convert
from pdf_ingest.schema import IngestError, Limits, SourceSpec
from pdf_ingest.serialize import validate_bundle


@pytest.mark.integration
def test_real_docling_native_worker(tmp_path):
    from pdf_ingest.provisioning import validate_models
    models = Path(__file__).resolve().parents[1] / '.models'
    try:
        validate_models(models, 'native_layout_v1')
    except IngestError:
        pytest.skip('Provisioned local Docling artifacts unavailable; model-backed extraction unverified.')
    from reportlab.pdfgen.canvas import Canvas
    imports = tmp_path / 'imports'
    imports.mkdir()
    pdf = imports / 'course.pdf'
    canvas = Canvas(str(pdf))
    canvas.setFont('Helvetica-Bold', 20)
    canvas.drawString(72, 760, 'Synthetic tissue notes')
    canvas.setFont('Helvetica', 12)
    canvas.drawString(72, 710, 'This is not patient data. Value: -0.5 mg; no change.')
    canvas.showPage()
    canvas.setFont('Helvetica', 12)
    canvas.drawString(72, 720, 'A second physical page with source provenance.')
    canvas.save()
    source = SourceSpec(source_id='live-fixture', source_version='1', title='Synthetic notes',
                        assignment='developer', rights_reference='synthetic', synthetic=True,
                        expected_source_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest())
    result = convert(pdf, imports, tmp_path / 'outputs', source, 'native_layout_v1', models,
                     Limits(memory_bytes=8 * 1024**3, job_timeout_seconds=240., page_timeout_seconds=120.))
    assert result.status == 'completed', result
    manifest, blocks, report = validate_bundle(result.bundle_path)
    assert manifest.requested_pages == [1, 2]
    assert all(page.block_ids for page in manifest.page_inventory)
    assert any('second physical page' in block.text_normalized for block in blocks)
    critical = 'Value: -0.5 mg; no change.'
    if not any(critical in block.text_normalized for block in blocks):
        assert any(issue.category in {'SYMBOL_UNCERTAIN', 'TEXT_LAYER_UNRELIABLE', 'EXTRACTOR_DISAGREEMENT'}
                   and 1 in issue.pdf_page_indices for issue in report.issues)
    assert manifest.review_state != 'approved'
