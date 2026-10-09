# PDF ingestion

A local CLI implementing the traceable digital-PDF MVP in [Plan v0.1](docs/project-plan.md). It produces readable Markdown and canonical source blocks, keeps physical PDF page indices, and requires explicit review before exporting passages to a retriever. Conversion never publishes or activates an index.

## Install

The supported worker platform is **Linux, Python 3.12, CPU, libseccomp**. Windows is not supported by this worker sandbox. Use a Linux VM or WSL2 only after validating its seccomp/resource-limit capabilities; support is not claimed merely because Python runs there.

```bash
cd /workspace/Project-2-GPT
UV_CACHE_DIR="$PWD/.cache/uv" uv sync --frozen --extra layout --group dev
.venv/bin/pdf-ingest --help
```

`uv.lock` pins the full dependency graph and artifact hashes. The layout extra pins Docling and CPU-only PyTorch. Installing only core dependencies supports schemas, review and import, but does not provide the default extractor. There is no automatic pypdf fallback.

Model provisioning is an explicit network-enabled installation step:

```bash
HF_HUB_DISABLE_XET=1 .venv/bin/pdf-ingest provision-models --models-dir .models
.venv/bin/pdf-ingest doctor --models-dir .models
```

The provisioner resolves each requested upstream model revision to an immutable commit, verifies official LFS SHA256 or Git blob checksums, and records a local artifact hash ledger. Network policy must permit `huggingface.co` and its download CDN (currently `cdn-lfs.huggingface.co`, `cdn-lfs-us-1.huggingface.co`, `cas-bridge.xethub.hf.co`, `us.aws.cdn.hf.co`). Additional redirects must be diagnosed rather than disabling TLS/checksums. Public model downloads do not require a token. Conversion only loads local, validated models and fails if they are missing or modified.

## Register and convert

Place permissioned PDFs under an operator-controlled import root. Do not supply URLs, patient records, arbitrary student uploads, symlinks or paths outside that root. A source registration contains metadata and approval references, never passwords or credentials. See [the synthetic example](docs/source.synthetic.json). For real sources, set `synthetic=false` and record confirmed processing rights. Content approval is a separate later requirement. Record the original file's exact SHA-256 as `expected_source_sha256` in the trusted registration (`sha256sum imports/course.pdf`); it is required at approval/import, and checked during conversion when supplied.

```bash
mkdir -p imports outputs
# Put your local PDF in imports/course.pdf and create its source.json registration.
.venv/bin/pdf-ingest convert course.pdf \
  --source source.json --import-root imports --output-root outputs \
  --models-dir .models --profile native_layout_v1
```

For a self-contained developer example, run `.venv/bin/python scripts/make_demo.py imports`, then convert `demo.pdf` with `--source imports/demo-source.json`. This creates synthetic source data and a hash-bound registration with content approval disabled.

Optional `--page-range 12:20` retains original pages 12–20. It does not renumber them. `--max-range-pages` limits selected pages independently of the 2,000-page source limit. Defaults are 100 MiB/file, 30 minutes/job, 120 seconds/page, two worker threads, one active worker and eight queued jobs. The CLI chooses an address-space limit using current RAM/cgroup headroom, capped at 8 GiB; `--memory-mib` makes the cap explicit. This is a limit, not a memory benchmark. `--scratch-mib` bounds the worker result file; the parser cannot create other writable files.

The Linux worker installs seccomp network/filesystem restrictions and hard address-space, CPU, output-file and descriptor limits before parsing. The parent stages a private source copy and never invokes a parser on unchecked input. Deadline/cancellation termination prevents late results from being published. The selected sandbox is a local parser boundary, not a multi-tenant hosting service.

Successful extraction creates:

```text
conversion_<source>_<version>_<conversion-id>/
  document.md
  blocks.jsonl
  manifest.json
  review_report.json
  corrections.jsonl     # after a versioned correction
```

All four initial files are mandatory. The original PDF remains unchanged. Canonical records retain raw extraction text, conservative NFC/LF normalization, source version, stable IDs, page origins, normalized boxes, table cells/spans and review issues. Markdown escapes source HTML and Markdown controls; links/images in source text cannot become active resources through this serializer. Missing coordinates, uncertain reading order, scan-like pages, formulas and complex tables require review. pypdf text diagnostics are checks, not the layout authority. Printed labels remain unverified until an operator verifies them against the source.

`completed` describes a structurally validated extraction. It does not mean `approved`. If extraction fails, no completed/importable bundle is published. A repeated unchanged validated extraction can be reused, but current permissions are checked again; corrections and changed fingerprints create a new conversion version.

## Review, correct and approve

```bash
.venv/bin/pdf-ingest validate outputs/conversion_...
.venv/bin/pdf-ingest review-status outputs/conversion_...
.venv/bin/pdf-ingest resolve outputs/conversion_... \
  --issue-id ISSUE_ID --resolution approved --reviewer reviewer-id
.venv/bin/pdf-ingest correct outputs/conversion_... \
  --block-id BLOCK_ID --text-file corrected-source-text.txt \
  --reason "Compared the decimal with the original page" --reviewer reviewer-id \
  --output-root outputs
```

Compare the original PDF's rendered pages with the blocks. Optional local previews use the same offline resource boundary, without Docling models:

```bash
.venv/bin/pdf-ingest preview course.pdf --source source.json --import-root imports \
  --page 12 --output outputs/page-12.png
.venv/bin/pdf-ingest verify-label outputs/conversion_... \
  --page 12 --label 8 --reviewer reviewer-id --output-root outputs
```

Label verification creates a new unapproved version, changes only verified printed-label provenance, and records an audit issue. It never changes the physical page index. `reclassify` with `--kind paragraph` or `--kind furniture`, a block ID, reason and reviewer corrects a paragraph/furniture misclassification in a new version; it cannot turn figures/formulas into invented prose.

A correction is source transcription, not new medical prose. `correct` creates a new conversion, preserves raw extraction and append-only correction history, regenerates Markdown/offsets/hashes, and invalidates previous approvals. Editing `document.md` directly invalidates the bundle. Use `table-representation` with the same arguments to record a deterministic reviewed table representation, especially for merged cells. The library also provides `structured_table_representation` with cell coordinates, spans, header flags, text and footnotes. Ordinary table correction cannot silently flatten spans.

Resolve every flagged issue individually. Explicit exclusions and any selected page range omitting source pages require a faculty record defining the permitted subset and omissions; use `--permitted-subset-reference` at approval. Initial policy blocks whole-source import with unresolved evidence. Source signoff must include a rendered sample of at least 20 otherwise unflagged pages, or all selected pages when fewer, plus every flagged/OCR/table/formula block. `source_level_sampled` records sampled pages and validates its scope; `direct` records an operator's attestation of direct review of the approved blocks. Neither a CLI command nor an automated test proves a person looked at the source.

Once the trusted current registration has `rights_confirmed=true`, `content_approved=true`, an explicit `content_approval_reference`, and `eligible=true`:

```bash
.venv/bin/pdf-ingest approve outputs/conversion_... \
  --source source.json --reviewer reviewer-id --method direct
# Or use --method source_level_sampled --sampled-pages 1,2,3,...
.venv/bin/pdf-ingest export outputs/conversion_... \
  --source source.json --output reviewed-passages.jsonl
```

The retriever adapter reads canonical text, not Markdown. It rechecks hashes, current tenant/source/version, approval scope, issue resolutions, rights/content approval and source eligibility. It omits furniture, markers, unresolved placeholders and non-evidence regions. Passage records carry source/version, block ID, section path and all page origins. They are input to an existing retriever's chunking/embedding/immutable-snapshot workflow; this repository has no live retriever or index activation endpoint.

The registration file is a trusted operator input. This MVP does not implement a faculty identity provider or signed permissions registry. Integrate those through `SourceSpec` and `iter_passages` before using it in a service; do not accept approval booleans from untrusted uploaders. Hashes detect derivative edits against the manifest, not an attacker rewriting the entire trusted local bundle and approvals.

## Jobs and independent batches

```bash
.venv/bin/pdf-ingest jobs --output-root outputs
.venv/bin/pdf-ingest cancel JOB_ID --output-root outputs
.venv/bin/pdf-ingest batch registrations.json \
  --import-root imports --output-root outputs --models-dir .models
```

A batch file is a JSON array of `{ "pdf": "course.pdf", "source": { ...SourceSpec... }, "page_range": "12:20" }`. Every file gets an independent outcome; the process exits nonzero if any registration does not complete. Job records are local SQLite state. Only one process claims an active conversion; queued calls wait for that claim. Use `--force` for an explicit new conversion record.

## OCR and evaluation

`english_ocr_v1` is an explicit English-only adapter, with local engine/data checks. The current hard read-only Linux worker **does not run Tesseract's temporary-file adapter** and returns `OCR_UNAVAILABLE`; it does not switch to native text or weaken its storage boundary. OCR is a separate extension, as in the plan. `provision-models --with-ocr` can record a locally installed Tesseract 5 engine and English data for a future compatible worker. Formula reconstruction, handwriting and image interpretation are outside the MVP.

Run the automated checks:

```bash
.venv/bin/pytest
.venv/bin/ruff check pdf_ingest tests
```

Contract tests use synthetic records, generated PDFs and a mocked extraction boundary where appropriate. They validate security/provenance/review behavior; they do not measure Docling extraction accuracy. A model-backed test must use provisioned local models and actual PDFs. Run corpus measurements with `pdf-ingest evaluate annotations.json`; the strict annotation format and metric definitions are documented in [evaluation.md](docs/evaluation.md). The proposed 30-PDF/200-page evaluation and clinical-symbol, CER, table and reading-order targets remain unmeasured until a reviewed corpus is supplied and evaluated. No improvement in tutor answer accuracy is claimed.
