# Reviewed corpus evaluation

`pdf-ingest evaluate annotations.json` scores fixed visual ground truth against
canonical bundles. It does not approve sources, activate an index, or demonstrate
improved medical answer accuracy. The test suite uses synthetic contract fixtures;
it is not the proposed 30-PDF, 200-page extraction benchmark.

Create annotations by comparing the original permissioned or synthetic PDF with
its local rendered preview. Keep related documents and pages in one development
or final group. Freeze final annotations and targets before the restricted final
test; do not tune extraction against those same documents. A final annotation file
must set `split` to `final` and provide an ISO 8601 `frozen_at` with timezone.

An annotation file and its completed bundle directories share a containing folder.
Bundle paths must be relative and cannot escape that folder. Compute
`source_sha256` from the unchanged original PDF, independently of its manifest.
Expected identities, profile, hash and synthetic/permission status are checked
before the bundle contributes to verified corpus counts. Every completed bundle
also passes the same schema, derivative-hash, Markdown and provenance checks used
by the importer. Distinct PDF hashes and `(PDF hash, physical page)` pairs prevent
duplicate conversions from inflating the reported corpus size.

Minimal example (replace the illustrative source hash with the actual hash):

```json
{
  "schema_version": "pdf-evaluation-0.1",
  "corpus_id": "development-fixtures-1",
  "split": "development",
  "documents": [
    {
      "document_id": "fixture-1-native",
      "document_type": "digital_single_column",
      "group_id": "lecture-fixture-1",
      "source_id": "fixture-1",
      "source_version": "1",
      "source_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
      "synthetic": true,
      "reviewer": "Developer",
      "annotation_reference": "synthetic-ground-truth-v1",
      "profile": "native_layout_v1",
      "status": "completed",
      "bundle": "conversion_fixture-1_1_example",
      "pages": [
        {
          "pdf_page_index": 1,
          "reference_text": "Dose ≤0.5 mg; no fever.",
          "block_ids": ["replace-with-actual-stable-block-id"]
        }
      ],
      "critical_fixtures": [
        {
          "block_id": "replace-with-actual-stable-block-id",
          "expected_text": "Dose ≤0.5 mg; no fever."
        }
      ],
      "citations": [
        {
          "block_id": "replace-with-actual-stable-block-id",
          "origins": [
            {
              "pdf_page_index": 1,
              "printed_label": null,
              "bbox_top_left_normalized": [0.1, 0.1, 0.9, 0.3]
            }
          ]
        }
      ]
    }
  ]
}
```

Every document requires an ID, type, group, source identity, synthetic status,
reviewer, annotation reference, profile, job status and at least one annotated
physical page. Real sources additionally require `rights_confirmed: true` and a
nonempty `rights_reference`. Completed jobs require a bundle and original source
hash. For blocked, failed or cancelled jobs, omit `bundle` and retain the ground
truth pages and other annotations. These outcomes remain in the report and metric
denominators; omission cannot improve apparent extraction quality.
Keep the actual returned job results with the annotations: unsuccessful status
records are operator-provided observations, because there is no completed bundle
to verify for those jobs.

The optional records provide separate measurements:

- `reading_order`: objects with `before` and `after` block IDs for each adjacent
  ground-truth pair. Both blocks must exist in the expected order; intervening
  furniture does not change their relative ordering.
- `tables`: objects with `block_id` and a complete canonical `table` payload:
  `rows`, `columns`, `cells`, and `footnotes`. Each cell carries exact `text`, `row`,
  `column`, optional positive `row_span`/`column_span` (default 1), and boolean
  `column_header`/`row_header` (default false). Text, location, spans and header
  flags must all match. Extra cells count against accuracy. Dimensions and
  footnotes are reported separately.
- `citations`: objects with `block_id` and all expected `origins`. Physical page
  indices and verified printed labels must match exactly. A supplied normalized
  bounding box is compared exactly; omitted boxes are outside that annotation's
  coordinate test. Annotate bounding boxes from independently reviewed fixtures.
- `critical_fixtures`: exact entire expected block strings. An incorrect string
  counts as an explicit rejection only when the block is `unresolved` with a
  linked unresolved issue, or `excluded` with a linked resolved exclusion. An
  ordinary warning or missing block does not count as a safe rejection. Rejections
  remain errors in CER and are reported separately from exact preservation.
- `resources`: measured `elapsed_seconds`, `peak_rss_bytes`, `peak_vram_bytes`,
  and `review_minutes`. Missing elapsed time and peak-memory measurements are
  read from valid bundle metrics when available. Explicit operator measurements
  take precedence. Unmeasured VRAM/review time remains unavailable, not zero.

Page text uses canonical block order joined by a single LF. By default it includes
headings, paragraphs, lists, captions and tables with an origin on the page and
omits furniture, figures, formula placeholders and review placeholders. To score
a reviewed selection, list its `block_ids`; reference text must match that exact
scope and separator convention. Excluded canonical text remains measurable and
excluded block/page counts remain visible. A multi-page block has no per-character
page mapping: allocate its complete reference text once using explicit block IDs
and put `block_ids: []` on other annotated pages when appropriate. Do not describe
that measurement as per-page fragment accuracy.

CER is exact Unicode code-point Levenshtein edit distance divided by reference
character count. There is **no** Unicode, whitespace, capitalization, decimal,
minus-sign, Greek-letter, unit or negation normalization in the metric. This keeps
clinically important differences visible. Empty references have no defined CER;
their inserted character counts still contribute to corpus edit totals. Failed
and invalid bundles are scored as empty outputs. Page accounting counts inventoried
physical pages, with unresolved and excluded statuses reported separately.

Results include counts and page-CER distributions by document type and conversion
profile, exact table associations, citation origins, ordered adjacent pairs,
critical-string preservation/rejection and job outcomes. Resources report raw job
elapsed time, seconds per requested page for valid bundles, peak RAM/VRAM, and
review minutes per annotated page, with observation count/minimum/median/mean/p95/
maximum. The harness reports the actual verified corpus size and whether it meets
the proposed 30-distinct-PDF and 200-distinct-annotated-page minimum. Minimum size
alone establishes no release or content approval.

The plan's proposed thresholds remain unmeasured until an appropriate fixed corpus
is supplied: 100% page/citation accounting; exact preservation or explicit rejection
of critical fixtures; digital CER ≤0.5%; clear-scan OCR CER ≤2%; simple-table
cell/header associations ≥99%; and correctly ordered annotated adjacent pairs
≥98%. Report exclusions and difficult cases alongside supported pages. Review the
individual pages and tails of the distributions: a low average can conceal one
dangerous number error.
