from pathlib import Path

import pytest
from pypdf import PdfWriter
from reportlab.pdfgen import canvas

from pdf_ingest.preflight import preflight, validate_input_path
from pdf_ingest.schema import IngestError, Limits, SourceSpec


def source(**changes):
    return SourceSpec(source_id="fixture", source_version="1", title="Synthetic", assignment="Test", rights_reference="", synthetic=True, **changes)


def limits(**changes):
    return Limits(memory_bytes=1024**3, **changes)


def make_pdf(path, pages=1):
    document = canvas.Canvas(str(path))
    for number in range(pages):
        document.drawString(50, 700, f"Synthetic page {number + 1}: 5 mg, not 50 mg.")
        document.showPage()
    document.save()


def test_scope_rejected_before_any_parser(tmp_path, monkeypatch):
    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", lambda *args, **kwargs: pytest.fail("parser was invoked"))
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.pdf"
    make_pdf(outside)
    for candidate in (outside, Path("../outside.pdf"), Path("https://example.com/source.pdf")):
        with pytest.raises(IngestError) as failure:
            validate_input_path(candidate, allowed, source(), limits())
        assert failure.value.code in {"PATH_DENIED", "INVALID_INPUT"}
    link = allowed / "escaped.pdf"
    link.symlink_to(outside)
    with pytest.raises(IngestError, match="PATH_DENIED"):
        validate_input_path(link, allowed, source(), limits())


def test_rights_and_size_rejected_before_parser(tmp_path, monkeypatch):
    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", lambda *args, **kwargs: pytest.fail("parser was invoked"))
    pdf = tmp_path / "source.pdf"
    make_pdf(pdf)
    real = SourceSpec(source_id="real", source_version="1", title="Source", assignment="Test", rights_reference="faculty-record", rights_confirmed=False)
    with pytest.raises(IngestError, match="RIGHTS_UNCONFIRMED"):
        validate_input_path(pdf, tmp_path, real, limits())
    with pytest.raises(IngestError, match="FILE_LIMIT"):
        validate_input_path(pdf, tmp_path, source(), limits(max_file_bytes=1))


def test_ranges_keep_original_physical_indices_and_unverified_labels(tmp_path):
    pdf = tmp_path / "source.pdf"
    make_pdf(pdf, 4)
    result = preflight(pdf, tmp_path, source(), limits(), (3, 4))
    assert result.source_page_count == 4
    assert result.requested_pages == [3, 4]
    assert [page.pdf_page_index for page in result.page_inventory] == [3, 4]
    assert all(not page.printed_label_verified for page in result.page_inventory)
    assert all(page.status == "unresolved" for page in result.page_inventory)
    assert all(page.native_text_chars > 0 for page in result.page_inventory)


def test_blank_is_not_inferred_from_missing_text(tmp_path):
    pdf = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    with pdf.open("wb") as stream:
        writer.write(stream)
    result = preflight(pdf, tmp_path, source(), limits())
    assert result.page_inventory[0].native_text_chars == 0
    assert result.page_inventory[0].status == "unresolved"


def test_encrypted_and_malformed_have_specific_codes(tmp_path):
    encrypted = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    writer.encrypt("secret-never-logged")
    with encrypted.open("wb") as stream:
        writer.write(stream)
    with pytest.raises(IngestError, match="ENCRYPTED_SOURCE") as failure:
        preflight(encrypted, tmp_path, source(), limits())
    assert "secret-never-logged" not in str(failure.value)
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.7\nmalformed-private-content")
    with pytest.raises(IngestError, match="PARSE_FAILED") as failure:
        preflight(bad, tmp_path, source(), limits())
    assert "malformed-private-content" not in str(failure.value)


def test_page_and_range_limits(tmp_path):
    pdf = tmp_path / "source.pdf"
    make_pdf(pdf, 3)
    with pytest.raises(IngestError, match="PAGE_LIMIT"):
        preflight(pdf, tmp_path, source(), limits(max_pages=2))
    with pytest.raises(IngestError, match="PAGE_LIMIT"):
        preflight(pdf, tmp_path, source(), limits(max_range_pages=1), (1, 2))
    with pytest.raises(IngestError, match="INVALID_INPUT"):
        preflight(pdf, tmp_path, source(), limits(), (1, 4))
