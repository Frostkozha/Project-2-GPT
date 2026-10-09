# Development guidance

Use this existing checkout. Cloud tasks are isolated; do not create a Git worktree unless the user requests one.

Read README.md and docs/project-plan.md before changing the ingestion boundary. Conversion completion, human review, source permissions, and retriever index activation are separate states. Never add an automatic extraction fallback, LLM rewriting, runtime model download, or index activation to make an error disappear.

Use Python 3.12 and `uv sync --frozen --extra layout --group dev`. Run `.venv/bin/pytest` and `.venv/bin/ruff check pdf_ingest tests` for relevant changes. The integration test is explicitly skipped without verified local Docling artifacts; report the skip and never substitute a mock to claim real extraction succeeded.

Keep models, imported sources, private staging and outputs ignored. Do not log source contents, credential values, raw parser exceptions or full process environments. Hash and validate trusted model artifacts; preserve TLS/checksum verification.

Workers must enforce Linux resource and syscall limits before parsing PDFs. Trusted dependency initialization may precede isolation, but no source parsing may do so; filters must synchronize across existing threads. A unsupported platform, failed limit installation, missing model or unsupported OCR sandbox is a failure, not permission to relax checks.

Canonical blocks are the import authority. Display Markdown, figures, formulas, placeholders and review messages must not become retriever evidence. Current source registration must independently bind original file hash, identity, permissions, approval and eligibility. Test revoked sources, modified outputs, partial sources and late cancellation results whenever changing review/import/jobs.
