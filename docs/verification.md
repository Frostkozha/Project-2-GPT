# Verification and handoff

Validated on 9 October 2026 in the selected Linux cloud workspace with Python 3.12.14.

- Full automated suite: **127 passed, 1 skipped**, in 7.83 seconds. One upstream Docling fixture deprecation warning.
- Ruff: passed for `pdf_ingest`, `tests`, and `scripts`.
- Frozen installation: passed; 108 installed packages checked.
- Wheel build: passed, including the pinned dependency lock artifact. Installed-wheel fingerprint lookup also passed.
- Real generated two-page PDFs: bounded offline PNG preview succeeded; the rendered source text was visually inspected.
- Real Linux controls: network/write/fork denial, existing-thread synchronization, memory/file limits, one active worker, eight queued jobs, cancellation and page/job watchdog tests passed.
- Trusted Docling/RTDETR/TableFormer dependencies initialize with the pinned CPU toolchain; tensor computation succeeds under the synchronized sandbox.

## Outstanding work

The model-backed digital extraction smoke test is explicitly skipped because local model weights could not be provisioned. The current proxy denies HTTPS downloads from `us.aws.cdn.hf.co` with HTTP 403. No alternative extractor, fake model or relaxed checksum/TLS setting was used to claim success.

The saved cloud configuration draft contains the installation script and these custom model-download destinations: `huggingface.co`, `cdn-lfs.huggingface.co`, `cdn-lfs-us-1.huggingface.co`, `cas-bridge.xethub.hf.co`, `us.aws.cdn.hf.co`. Review and save the draft in environment settings, then publish the environment. Draft persistence does not mean those changes are applied or published. After network access is applied, run:

```bash
cd /workspace/Project-2-GPT
.venv/bin/pdf-ingest provision-models --models-dir .models
.venv/bin/pdf-ingest doctor --models-dir .models
.venv/bin/pytest tests/test_live_layout.py -q
```

The installation script's dependency step is verified; its model provisioning step remains blocked in the current instance. No service needs to remain running: CLI workers start on demand.

English OCR is explicitly unavailable in the chosen read-only worker because the Tesseract CLI adapter requires writable temporary files. The adapter/provisioning boundary exists, but end-to-end OCR needs a separately validated worker extension. Windows isolation, the reviewed 30-PDF/200-page evaluation corpus, extraction accuracy targets and intended-hardware benchmarks remain outstanding.

Source code and tests are in the existing checkout, without a GitHub push or index activation. The original plan is preserved in `docs/project-plan.md`. See `README.md` for operation, `docs/acceptance.md` for T01–T30 coverage, and `docs/evaluation.md` for corpus annotations.
