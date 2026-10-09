"""Pinned, offline Docling standard-PDF adapter with conservative text handling.

Conversion runs in the isolated worker. It deliberately does not provide a
pypdf fallback: native reference extraction below produces diagnostics only.
"""
from __future__ import annotations

import os
import importlib
import warnings
from pathlib import Path
from typing import Any

from .normalize import normalize_text
from .preflight import page_deadline
from .provenance import normalize_bbox, stable_block_id, text_hash
from .provisioning import load_model_ledger, validate_models
from .quality import attach_issue, check_quality, issue_for
from .schema import (
    Block, HeadingBlock, HeadingPayload, IngestError, Issue, Origin,
    PageInventory, SourceSpec, TableBlock, TableCell, TablePayload, TextBlock,
    table_text,
)


def prepare_runtime() -> None:
    """Import pinned trusted dependencies before installing the syscall filter.

    No PDF, model configuration or model weights are opened here. Some trusted
    Python packages initialize writable /dev/null streams on first import; the
    sandbox then synchronizes its restrictions across any initialized threads.
    """
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    try:
        for module in (
            "torch", "numpy", "PIL.Image", "pypdfium2",
            "docling.document_converter", "docling.pipeline.standard_pdf_pipeline",
            "docling.models.inference_engines.object_detection.transformers_engine",
            "transformers.models.rt_detr.modeling_rt_detr",
            "transformers.models.rt_detr.image_processing_rt_detr",
            "docling_ibm_models.tableformer.data_management.tf_predictor",
        ):
            importlib.import_module(module)
    except Exception as exc:
        raise IngestError("MODEL_UNAVAILABLE", "Pinned native-layout dependencies could not initialize before worker isolation.") from exc


def _make_converter(models_dir: Path, profile: str, threads: int, page_timeout_seconds: float):
    """No default model resolution, external plugins, remote services or VLMs."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"
    ledger = load_model_ledger(models_dir)
    try:
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.object_detection_engine_options import TransformersObjectDetectionEngineOptions
        from docling.datamodel.pipeline_options import (
            LayoutObjectDetectionOptions, TableFormerMode, TableStructureOptions,
            TesseractCliOcrOptions, ThreadedPdfPipelineOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.models.stages.page_assemble.page_assemble_model import PageAssembleModel, PageAssembleOptions
        from docling.models.stages.reading_order.readingorder_model import ReadingOrderModel, ReadingOrderOptions
        from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
        from docling_core.types.doc import ProvenanceItem

        class ConservativePageAssembleModel(PageAssembleModel):
            def sanitize_text(self, lines: list[str]) -> str:
                # Docling's stock method dehyphenates and changes quote, slash,
                # bullet and ligature characters. Preserve the detected lines.
                return "\n".join(lines)

        class ConservativeReadingOrderModel(ReadingOrderModel):
            def _merge_elements(self, element, merged_elem, new_item, page_height):
                # The upstream method deletes soft/hard hyphens between
                # clusters. Preserve text and all page/region origins instead.
                if not isinstance(merged_elem, type(element)) or merged_elem.label != new_item.label:
                    raise IngestError("INVALID_PROVENANCE", "Docling attempted to merge incompatible source blocks.")
                start = len(new_item.orig) + 1
                new_item.text += "\n" + merged_elem.text
                new_item.orig += "\n" + merged_elem.text
                new_item.prov.append(ProvenanceItem(page_no=merged_elem.page_no, charspan=(start, start + len(merged_elem.text)), bbox=merged_elem.cluster.bbox.to_bottom_left_origin(page_height)))
                if new_item.hyperlink != merged_elem.hyperlink:
                    new_item.hyperlink = None

        class ConservativePdfPipeline(StandardPdfPipeline):
            def _init_models(self) -> None:
                super()._init_models()
                self.assemble_model = ConservativePageAssembleModel(PageAssembleOptions())
                self.reading_order_model = ConservativeReadingOrderModel(ReadingOrderOptions(use_page_separators=self.pipeline_options.use_reading_order_separators))

        layout = LayoutObjectDetectionOptions(engine_options=TransformersObjectDetectionEngineOptions(compile_model=False))
        layout.model_spec = layout.model_spec.model_copy(update={"revision": ledger["models"]["docling-project/docling-layout-heron"]})
        options = ThreadedPdfPipelineOptions(
            artifacts_path=Path(models_dir),
            accelerator_options=AcceleratorOptions(num_threads=threads, device=AcceleratorDevice.CPU),
            do_ocr=profile == "english_ocr_v1",
            do_table_structure=True,
            table_structure_options=TableStructureOptions(mode=TableFormerMode.ACCURATE, do_cell_matching=True),
            layout_options=layout,
            enable_remote_services=False,
            allow_external_plugins=False,
            do_code_enrichment=False,
            do_formula_enrichment=False,
            do_picture_classification=False,
            do_picture_description=False,
            do_chart_extraction=False,
            generate_page_images=False,
            generate_picture_images=False,
            generate_parsed_pages=False,
            document_timeout=page_timeout_seconds,
        )
        if profile == "english_ocr_v1":
            options.ocr_options = TesseractCliOcrOptions(lang=["eng"], tesseract_cmd=ledger["ocr"]["engine"], path=str(models_dir / "ocr"), force_full_page_ocr=True)
        else:
            # Construct a disabled, explicit local engine. OcrAutoOptions may
            # probe/download alternatives even when assumptions later change.
            options.ocr_options = TesseractCliOcrOptions(lang=["eng"])
        return DocumentConverter(allowed_formats=[InputFormat.PDF], format_options={InputFormat.PDF: PdfFormatOption(pipeline_cls=ConservativePdfPipeline, backend=PyPdfiumDocumentBackend, pipeline_options=options)})
    except IngestError:
        raise
    except (ImportError, ValueError, AttributeError, TypeError) as exc:
        raise IngestError("MODEL_UNAVAILABLE", "The pinned local Docling standard pipeline could not be configured; install its layout dependencies and provision matching models.") from exc


def _enum(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def _origins(item: Any, document: Any, requested: set[int], inventory: dict[int, PageInventory], fallback_page: int) -> list[Origin]:
    output: list[Origin] = []
    raw_provenance = getattr(item, "prov", [])
    if not raw_provenance:
        # Preserve the observed page without inventing a region box. Quality
        # checks attach the mandatory missing-box issue.
        return [Origin(pdf_page_index=fallback_page, raw_coordinate_system="Docling provenance unavailable", transformation="page known from explicit original-source page_range; region unavailable")]
    for prov in raw_provenance:
        page_no = getattr(prov, "page_no", None)
        # Docling standard pipeline is verified 1-based, with original source
        # page_no retained through page_range. Never guess a zero-based offset.
        if type(page_no) is not int or page_no not in requested:
            raise IngestError("INVALID_PROVENANCE", "Docling returned a page locator outside the original requested page indices.")
        page = inventory[page_no]
        doc_page = document.pages.get(page_no)
        if doc_page is not None:
            width, height = float(doc_page.size.width), float(doc_page.size.height)
        else:
            width, height = (page.height, page.width) if page.rotation in {90, 270} else (page.width, page.height)
        raw_box = getattr(prov, "bbox", None)
        normalized = None
        raw_system = "Docling bbox unavailable"
        if raw_box is not None:
            origin = _enum(getattr(raw_box, "coord_origin", ""))
            if origin in {"topleft", "top-left", "top_left"}:
                coordinate_origin = "top-left"
            elif origin in {"bottomleft", "bottom-left", "bottom_left"}:
                coordinate_origin = "bottom-left"
            else:
                raise IngestError("INVALID_PROVENANCE", "Docling returned an unverified bounding-box coordinate convention.")
            raw_system = f"Docling/PyPdfium {coordinate_origin} points in already rotation-corrected display frame"
            try:
                box = (float(raw_box.l), min(float(raw_box.t), float(raw_box.b)), float(raw_box.r), max(float(raw_box.t), float(raw_box.b)))
                # Backend reports display-frame coordinates, so applying the
                # PDF /Rotate a second time would corrupt citation locators.
                normalized = normalize_bbox(box, width, height, origin=coordinate_origin, rotation=0)
            except (ValueError, TypeError, AttributeError):
                normalized = None
        origin_record = Origin(pdf_page_index=page_no, printed_label=page.printed_label if page.printed_label_verified else None, bbox_top_left_normalized=normalized, raw_coordinate_system=raw_system, transformation="bottom-left to top-left when applicable; divide by Docling display-page dimensions; PDF rotation already applied by PyPdfium backend")
        if origin_record not in output:
            output.append(origin_record)
    return output


def _footnotes(item: Any, document: Any) -> list[str]:
    output = []
    for ref in getattr(item, "footnotes", []) or []:
        target = ref.resolve(document)
        text, _ = normalize_text(getattr(target, "text", ""))
        output.append(text)
    return output


def _table(item: Any, document: Any) -> tuple[TablePayload, str, list[str]]:
    data = item.data
    raw_cells: list[TableCell] = []
    cells: list[TableCell] = []
    normalizations: list[str] = []
    for cell in data.table_cells:
        raw = cell.text
        normalized, changes = normalize_text(raw)
        normalizations.extend(name for name in changes if name not in normalizations)
        fields = dict(row=int(cell.start_row_offset_idx), column=int(cell.start_col_offset_idx), row_span=int(cell.row_span), column_span=int(cell.col_span), column_header=bool(cell.column_header), row_header=bool(cell.row_header))
        raw_cells.append(TableCell(text=raw, **fields))
        cells.append(TableCell(text=normalized, **fields))
    footnotes = _footnotes(item, document)
    payload = TablePayload(rows=int(data.num_rows), columns=int(data.num_cols), cells=cells, footnotes=footnotes)
    raw_payload = TablePayload(rows=payload.rows, columns=payload.columns, cells=raw_cells, footnotes=[getattr(ref.resolve(document), "text", "") for ref in getattr(item, "footnotes", []) or []])
    raw_text = table_text(raw_payload)
    _, all_changes = normalize_text(raw_text)
    normalizations.extend(name for name in all_changes if name not in normalizations)
    return payload, raw_text, normalizations


def _adapt_document(document: Any, source: SourceSpec, fingerprint: str, requested: set[int], inventory: dict[int, PageInventory], fallback_page: int, method: str, sections: list[str]) -> tuple[list[Block], list[Issue], list[str]]:
    from docling_core.types.doc import ContentLayer
    blocks: list[Block] = []
    issues: list[Issue] = []
    seen_refs: set[str] = set()
    # Docling defaults to body only. Furniture must also survive in canonical
    # provenance until a reviewer confirms its classification.
    items = list(document.iterate_items(included_content_layers=set(ContentLayer)))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=DeprecationWarning)
        furniture = getattr(document, "furniture", None)
    if furniture is not None:
        items.extend(document.iterate_items(root=furniture, included_content_layers=set(ContentLayer)))
    footnote_refs = {ref.cref for item, _ in items for ref in getattr(item, "footnotes", []) or [] if _enum(getattr(item, "label", "")) == "table"}
    for item, _depth in items:
        anchor = getattr(item, "self_ref", "")
        if anchor in seen_refs or anchor in footnote_refs:
            continue
        seen_refs.add(anchor)
        label = _enum(getattr(item, "label", ""))
        if not label:
            continue
        origins = _origins(item, document, requested, inventory, fallback_page)
        # Docling preserves original formula/list wording in orig while its
        # display text may omit a formula or remove an enumeration marker.
        raw = getattr(item, "orig", None)
        if not isinstance(raw, str):
            raw = getattr(item, "text", "")
        normalized, changes = normalize_text(raw)
        table = None
        table_error = False
        if label == "table":
            try:
                table, raw, changes = _table(item, document)
                normalized = table_text(table)
                kind = "table"
            except (AttributeError, ValueError, TypeError):
                # Invalid structure cannot become a flattened evidence block.
                raw = "\n".join(str(getattr(cell, "text", "")) for cell in getattr(getattr(item, "data", None), "table_cells", []))
                normalized, changes = normalize_text(raw)
                kind, table_error = "placeholder", True
        elif label in {"title", "section_header"}:
            kind = "heading"
        elif label == "list_item":
            kind = "list"
        elif label == "caption":
            kind = "caption"
        elif label in {"picture", "chart"}:
            kind = "figure"
        elif label == "formula":
            kind = "formula"
        elif label in {"page_header", "page_footer"} or _enum(getattr(item, "content_layer", "")) == "furniture":
            kind = "furniture"
        elif _enum(getattr(item, "content_layer", "")) in {"background", "invisible", "notes"}:
            kind = "placeholder"
        elif label in {"text", "paragraph", "footnote", "reference", "code"}:
            kind = "paragraph"
        else:
            kind = "placeholder"
        if kind == "heading":
            level = 1 if label == "title" else min(6, max(1, int(getattr(item, "level", 2))))
            sections = sections[:level - 1] + [normalized]
        values = dict(block_id=stable_block_id(source, origins, {"docling_ref": anchor, "label": label}, normalized, fingerprint), source_id=source.source_id, source_version=source.source_version, section_path=list(sections), text_raw=raw, text_normalized=normalized, origins=origins, extraction_method=method, index_candidate=kind not in {"formula", "figure", "furniture", "placeholder"}, text_sha256=text_hash(normalized), normalizations=changes)
        if kind == "heading":
            block = HeadingBlock(heading=HeadingPayload(level=level), **values)
        elif kind == "table":
            block = TableBlock(table=table, **values)
        else:
            block = TextBlock(kind=kind, **values)
        blocks.append(block)
        if table_error:
            attach_issue(issues, blocks, issue_for("TABLE_STRUCTURE_UNRESOLVED", "The extractor produced a table without valid non-overlapping cell structure; raw cell text is preserved for review only.", pages=sorted({o.pdf_page_index for o in origins}), blocks=[block.block_id]))
    return blocks, issues, sections


def _is_visually_blank(pdf_path: Path, page_index: int) -> bool:
    """Confirm only an exactly white rendered page; never infer blank from text."""
    import pypdfium2
    document = pypdfium2.PdfDocument(pdf_path)
    try:
        page = document[page_index - 1]
        try:
            bitmap = page.render(scale=1.0)
            try:
                image = bitmap.to_pil().convert("RGB")
                try:
                    return all(minimum == 255 and maximum == 255 for minimum, maximum in image.getextrema())
                finally:
                    image.close()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()


def extract_document(pdf_path: Path, source: SourceSpec, profile: str, models_dir: Path, fingerprint: str, pages: list[int], inventory: list[PageInventory], threads: int = 2, page_timeout_seconds: float = 120) -> tuple[list[Block], list[Issue], list[PageInventory], dict[str, str]]:
    revisions = validate_models(models_dir, profile)
    if profile == "english_ocr_v1" and os.environ.get("PDF_INGEST_READONLY_SANDBOX") == "1":
        raise IngestError("RESOURCE_LIMIT", "English Tesseract CLI requires temporary files; this deployment's enforced read-only parser sandbox does not support that profile.")
    if not pages or pages != sorted(set(pages)) or {p.pdf_page_index for p in inventory} != set(pages):
        raise IngestError("INVALID_PROVENANCE", "Extraction requires a complete, unique inventory for every requested original page.")
    page_map = {page.pdf_page_index: page.model_copy(deep=True) for page in inventory}
    blocks: list[Block] = []
    issues: list[Issue] = []
    sections: list[str] = []
    native_reference: dict[int, str] = {}
    try:
        with page_deadline(page_timeout_seconds):
            converter = _make_converter(Path(models_dir), profile, threads, page_timeout_seconds)
            from pypdf import PdfReader
            reader = PdfReader(pdf_path, strict=True)
        for page_index in pages:
            with page_deadline(page_timeout_seconds):
                result = converter.convert(Path(pdf_path), page_range=(page_index, page_index), raises_on_error=False)
                status = _enum(result.status)
                if status not in {"success", "partial_success"}:
                    if any("timeout" in str(getattr(error, "error_message", "")).lower() for error in getattr(result, "errors", [])):
                        raise IngestError("DEADLINE_EXCEEDED", "Docling exceeded the per-page processing deadline.")
                    raise IngestError("PARSE_FAILED", "The selected Docling profile could not convert a source page; no fallback was attempted.")
                if set(result.document.pages) != {page_index}:
                    raise IngestError("INVALID_PROVENANCE", "Docling did not retain the original requested 1-based physical page index.")
                page_blocks, page_issues, sections = _adapt_document(result.document, source, fingerprint, set(pages), page_map, page_index, "english_ocr" if profile == "english_ocr_v1" else "native_layout", sections)
                blocks.extend(page_blocks)
                issues.extend(page_issues)
                page = page_map[page_index]
                on_page = [block for block in blocks if any(o.pdf_page_index == page_index for o in block.origins)]
                if any(block.index_candidate for block in on_page):
                    page.status, page.reason = "extracted", None
                elif on_page and all(block.kind in {"figure", "formula", "furniture"} for block in on_page):
                    page.status, page.reason = "non_text", "Only non-evidence regions or page furniture were detected; individual region issues remain subject to review."
                elif not on_page and page.native_text_chars == 0 and _is_visually_blank(Path(pdf_path), page_index):
                    page.status, page.reason = "confirmed_blank", "Reference renderer produced an exactly white RGB page at 72 DPI; no body or non-text region was detected."
                else:
                    page.status, page.reason = "unresolved", "The selected profile did not account for visible page content."
                if status == "partial_success" or getattr(result, "errors", []):
                    attach_issue(issues, blocks, issue_for("PAGE_UNACCOUNTED", "Docling reported partial conversion or diagnostic errors on this page; compare every region against the source.", pages=[page_index], blocks=[block.block_id for block in on_page], severity="error"))
                try:
                    native_reference[page_index] = reader.pages[page_index - 1].extract_text() or ""
                except (ValueError, TypeError, KeyError):
                    attach_issue(issues, blocks, issue_for("TEXT_LAYER_UNRELIABLE", "Native reference diagnostics failed for this page; source review is required.", pages=[page_index], blocks=[block.block_id for block in on_page]))
        # Reconcile all origins after extraction, including multi-page blocks.
        for page in page_map.values():
            page.block_ids = [block.block_id for block in blocks if any(origin.pdf_page_index == page.pdf_page_index for origin in block.origins)]
        final_inventory = [page_map[index] for index in pages]
        for issue in check_quality(blocks, final_inventory, profile, native_reference):
            attach_issue(issues, blocks, issue)
        return blocks, issues, final_inventory, revisions
    except IngestError:
        raise
    except (MemoryError, OSError) as exc:
        if isinstance(exc, MemoryError) or getattr(exc, "errno", None) in {12, 24, 27}:
            raise IngestError("RESOURCE_LIMIT", "Extraction exceeded an enforced worker resource limit.") from None
        raise IngestError("PARSE_FAILED", "Local extraction failed while reading required artifacts or source data.") from None
    except Exception:
        # Parser exceptions sometimes contain private source text or paths.
        raise IngestError("PARSE_FAILED", "Local Docling extraction failed; no runtime download or fallback was attempted.") from None
