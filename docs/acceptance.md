# Plan v0.1 acceptance coverage

This matrix distinguishes implemented policy/adapter contracts from measured PDF
accuracy. Synthetic canonical fixtures, generated PDFs, actual Linux kernel
controls and mocked Docling objects test different boundaries. A passing mocked
adapter test does not show that Docling correctly extracts a representative PDF.
The fixed 30-PDF/200-annotated-page benchmark remains outstanding; the evaluation
harness and annotation requirements are in [evaluation.md](evaluation.md).

| Scenario | Implemented boundary and evidence | Remaining validation or scope |
|---|---|---|
| T01 Clean digital PDF | Native Docling adapter, heading/body mapping and complete inventory; `test_extract.py` page-range adapter test; bundle round trip | Model-backed smoke and representative accuracy are separate from mocked fixtures |
| T02 Two columns | Bounding-box overlap triggers reading-order review; native/layout disagreement also flags uncertainty; evaluation scores annotated ordered pairs | Representative multi-column PDFs and visual review are required |
| T03 Physical page 12 / printed label 8 | Verified-label versioning and original page origins; `test_review_import.py` verified-label test, `test_extract.py` original-index test | Automated PDF labels begin unverified; operator must compare source |
| T04 Page range 12–20 | `test_preflight.py` retains original indices; extraction adapter rejects renumbered Docling pages | Faculty subset permission is required before importing an incomplete source |
| T05 Multi-page paragraph | Canonical schema/serializer preserves multiple origins; `test_extract.py` multi-origin fixture, `test_bundle.py` round trip | Pagewise extraction does not heuristically join separate page-local paragraphs |
| T06 Scan with native profile | Missing text-layer diagnostics raise `OCR_REQUIRED`; unresolved pages cannot approve/import | Actual scan corpus remains unmeasured |
| T07 Explicit English OCR | Pinned English-only options, local model/engine/data checks and individual-review contract | Current read-only worker rejects temporary-file OCR with `OCR_UNAVAILABLE`; end-to-end OCR is deferred |
| T08 Missing model | Missing/checksum-changed/symlinked local artifacts fail before converter construction; `test_extract.py` model tests | Runtime never provisions or silently falls back |
| T09 Mixed/broken text layer | Every requested page is inventoried, mismatches and sparse text produce review issues | Mixed native/scan corpus remains unmeasured |
| T10 Blank/figure-only page | Native renderer confirms exact-white blank pages; figures/formulas are retained without invented text; inventory carries explicit status/reason | Sparse text is never proof of blankness; visual review remains required |
| T11 Furniture / legitimate margin text | Classified furniture is retained and flagged for review; a versioned `reclassify` operation restores reviewed paragraph text without rewriting; `test_review_import.py` legitimate-margin regression | Only paragraph/furniture reclassification is supported; formulas and images cannot become evidence through this operation |
| T12 Critical number/unit/symbol/negation | Conservative NFC/LF normalization; exact-symbol diagnostics; independent Unicode edit-distance and critical-string evaluation tests | Representative source accuracy and dangerous-error tails remain unmeasured |
| T13 Simple table / footnote | Typed cell/header/span/footnote payloads and table canonical-text checks; table fixtures in `test_extract.py` and `test_bundle.py` | Actual table association accuracy requires reviewed table annotations |
| T14 Merged headers | Span structure remains canonical; import requires a separately reviewed deterministic representation; `test_review_import.py` merged-table test | No automatic flattened-table approval |
| T15 Unsupported formula/handwriting/chart | Formula/figure/placeholder regions stay non-evidence and unresolved; adapter/import tests | No formula reconstruction, handwriting recognition or image interpretation |
| T16 Source HTML/scripts/links | Serializer escapes source markup and link/image syntax; `test_bundle.py` safe-display test | Canonical `<`/`>` characters remain intact |
| T17 Wrong source hash/version/locator | Current trusted source SHA is required at approval/import, identities rechecked, origins and spans deeply validated; source-hash and provenance tests | Current registry is an explicit operator input, not a live external registry integration |
| T18 Direct Markdown edit | Output hashes plus deterministic Markdown re-render reject direct edits, even with a rewritten derivative hash | Canonical corrections must use the supported versioned operation |
| T19 Reviewed correction | New immutable conversion version, original raw text, patch history, regenerated IDs/offsets/hashes and invalidated approvals; review tests | An operator remains responsible for source-faithful wording |
| T20 Completed / unapproved | Conversion and approval are separate; importer rejects missing current content sign-off and bundle approval | No automatic activation or indexing |
| T21 Revocation | Current rights/content/eligibility are independently checked after past approval; parameterized review/import tests | Calling retriever must supply its current registry state |
| T22 Unchanged duplicate | Identity-aware fingerprints and deep validation before cache reuse; permissions rechecked | Cache tests and real duplicate smoke must be reported separately from extraction accuracy |
| T23 Profile/model/range change | Fingerprint includes model/dependency/code revisions and source metadata/range; correction versions are not reused | Each changed version remains subject to review |
| T24 Limits / hang | Descriptor-based file-size checks, page/range limits, actual seccomp/AS/file caps and real parent-watchdog tests | RAM/VRAM and throughput benchmark on intended deployment hardware remains outstanding |
| T25 Encrypted/malformed/path escape | Real generated encrypted/malformed PDFs and parser-free rights/path checks in preflight tests | Authorized decryption remains a separate local operator action |
| T26 Independent batch failures | Batch command reports every item and continues; CLI batch test; terminal conversion failures are also recorded in SQLite | Malformed batch registrations are reported individually before a conversion job can be created; retain the batch result |
| T27 Markers/placeholders/furniture | Import reads canonical text only and excludes non-evidence kinds; import/display fixture tests | It does not parse Markdown markers into passages |
| T28 Approved retriever import | Canonical plain text, immutable source version, IDs, hierarchy, original origins and approval/subset metadata reach passage dictionaries | Chunking, embedding and snapshot activation belong to the existing retriever |
| T29 Incomplete accounting | Missing/duplicate/out-of-range requested-page inventory, blocks and locators fail deep validation; provenance tests | Explicit page ranges are partial sources and require documented faculty scope |
| T30 Cancellation / late success | Durable cancellation, process-group termination, parent death handling, deadline watchdog and final transactional publication check; real child-process tests | Failed/cancelled staging never publishes an importable bundle |

The production source corpus, final review sign-off, scan extension, Windows
sandbox and intended-hardware benchmarks are not supplied by these contract tests.
The selected implementation currently supports the tested Linux native profile.
Windows conversion fails closed rather than claiming equivalent isolation.
