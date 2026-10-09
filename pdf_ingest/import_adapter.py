"""Canonical reviewed passage boundary; this module never publishes an index."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .review import EVIDENCE_KINDS, assert_importable
from .schema import SourceSpec


def iter_passages(bundle: Path, current_source: SourceSpec) -> Iterator[dict]:
    """Yield canonical plain text and original origins after all import gates.

    Markdown comments, display placeholders, formulas, figures and page
    furniture are not read or emitted. The retriever remains responsible for
    chunking, embedding and snapshot activation.
    """
    manifest, blocks, report = assert_importable(Path(bundle), current_source)
    subset_reference = next((record.permitted_subset_reference for record in report.approvals if record.permitted_subset_reference), None)
    for block in blocks:
        if block.kind not in EVIDENCE_KINDS or not block.index_candidate or block.extraction_quality != "approved":
            continue
        yield {
            "text": block.text_normalized,
            "tenant_id": manifest.source.tenant_id,
            "source_id": manifest.source.source_id,
            "source_version": manifest.source.source_version,
            "title": manifest.source.title,
            "assignment": manifest.source.assignment,
            "block_id": block.block_id,
            "section_path": list(block.section_path),
            "origins": [origin.model_dump(mode="json") for origin in block.origins],
            "conversion_id": manifest.conversion_id,
            "source_page_count": manifest.source_page_count,
            "requested_pages": list(manifest.requested_pages),
            "permitted_subset_reference": subset_reference,
            "approval_references": list(manifest.approval_references),
        }
