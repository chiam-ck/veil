# Changelog

## 1.0.0 — 2026-11-13

First public source release under AGPL-3.0-only.

- LLM-only PII detection for Office/PDF documents, local OCR/editing/storage, bilingual text and batches of up to 50 files.
- Private anonymous browser workspaces with server-enforced ownership, protected cookies, expiry/revocation and direct HTTPS deployment.
- Manual exact-phrase corrections and model re-runs that retain earlier redactions, create revisions and update batch exports.
- Cached synthetic provider checks, explicit API-option negotiation, shared inference limits, input/output budgets and bounded outage recovery.
- Preserved supported Office formulas/comments/revisions with actionable review findings and flattened PDF output.
- Bounded multipart parsing, strict Host/origin checks, workspace-creation limits, private CLI credentials and strengthened response headers.
- Updated security-sensitive dependencies, hash-pinned runtime/build/development locks, minimal runtime image, source/secret release checks and CI.
- Generic `.env.example`, deployment-neutral documentation, third-party notices and source offer. Local documents, secrets, keys, sessions and evidence are excluded from release source.

Detection/OCR accuracy depends on the configured model and input. Completed processing is not a zero-PII guarantee.
