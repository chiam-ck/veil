# LLM-only document detection

Veil uses one administrator-configured OpenAI-compatible model for all document
PII detection. OCR, document editing, source-quote validation and storage run
locally. No local NER packages, NER model downloads, comparison API or comparison
page are required or shipped.

Configure the server's private `.env`:

```sh
VEIL_LLM_ENDPOINT=https://your-approved-provider.example/v1
VEIL_LLM_MODEL=your-model-id
VEIL_LLM_API_KEY=YOUR_PRIVATE_KEY
VEIL_LLM_ALLOW_NETWORK=true
VEIL_WORKERS=1
VEIL_LLM_CONCURRENCY=1
VEIL_LLM_INPUT_TOKEN_BUDGET=4096
VEIL_LLM_MAX_OUTPUT_TOKENS=8192
```

`VEIL_DETECTOR=llm` remains an accepted compatibility setting; other modes are
rejected. The legacy `LLM_API_KEY` and `LLM_MODEL` aliases remain supported.
`VEIL_LLM_API_KEY_FILE` supports a privately mounted secret when configured in
the service environment; workers reload a changed mounted key before pending work. The browser receives configuration status only, never
credentials. `.env` is excluded from builds. `scripts/dev.sh` loads it for local
development. Recreate containers after changing environment values.

Generate a private HTTPS identity with `scripts/setup_workspace_tls.py --host YOUR_APPROVED_HOSTNAME` before the first Compose startup, then run `docker compose up -d --build`. Trust `.veil/tls/ca.crt` on clients and open the HTTPS workspace. See the developer guide for browser ownership, cookie lifetime and CLI cookie persistence. The developer guide identifies the deployment as ON-PREM. `/api/llm/config` exposes configuration status. `/api/provider/status`, `/api/config` and `/api/health` include cached readiness. Reading status never sends a model request; API health remains a liveness check.

Recognised standard Office paths and field-specific formatting values are
validated locally. Unknown values, free text, metadata, comments, headers,
footers, revisions and hidden content still pass through detection. Exact text
and context are deduplicated within each document. Batches use an input budget
with a safety ceiling of 256 segments; oversized values use overlapping chunks.
The budget is a conservative UTF-8 byte estimate including instructions, JSON
and any strict response schema, independent of the model tokenizer. Its default
is 4096, configurable from 2048 to 32768. Reserve output/context space separately:
output starts at 4096 tokens and can retry once up to `VEIL_LLM_MAX_OUTPUT_TOKENS` (default 8192, allowed 128–8192). Both requests respect this cap; choose it with the model context window in mind.

Every batch must acknowledge every supplied segment. Every detected quote must
match its own source exactly. A quote mismatch gets one isolated recheck for
that segment, at most eight per document. A failed recheck blocks output;
refusals and exhausted output budgets also block output. Malformed JSON and incomplete responses pause processing for provider recovery; they never publish partial output. No response is accepted by fuzzy matching or silently ignored.

`VEIL_WORKERS` controls document workers. `VEIL_LLM_CONCURRENCY` (1–4) caps all
in-flight requests to the same endpoint across workers sharing `VEIL_DATA`.
OS file locks release on process exit. Use 1 for a single-inference endpoint.
HTTP 429 gets bounded short retries, honoring a usable Retry-After. Persistent
HTTP/authentication/connection errors enter shared provider recovery. Connections
are reused. A provider adapter removes `response_format` or `temperature` only
when explicitly rejected, and switches `max_tokens` to `max_completion_tokens`
when explicitly requested. Unrelated HTTP errors never disable validation.
Prompt-only replies may use one exact enclosing JSON Markdown fence; surrounding
prose, duplicate keys and invented quotes are still rejected.

Before pending documents are claimed, the worker checks both batch and standalone
response contracts using two small synthetic requests, including a negative case.
These checks do not contain uploaded document text. Readiness and negotiated options
are cached across workers for `VEIL_PROVIDER_CHECK_TTL` seconds (default 300),
scoped to endpoint, model, credential fingerprint, prompt version and token budgets.
After expiry or a provider failure, capabilities are learned again. Idle workers do
not probe; the checks add a small overhead to the first job after cache expiry.
Passing the probe checks compatibility, not general detection accuracy.

During an outage, accepted jobs show **Waiting for model** and resume with their
original job IDs and batch positions when the check passes. Partial attempt output
is discarded; retained originals are unchanged. Probes back off from
`VEIL_PROVIDER_RETRY_SECONDS` (default 5) to at most 60 seconds. Waiting is bounded
by `VEIL_PROVIDER_WAIT_TIMEOUT` (default 900 seconds), with at most three failed
document attempts. Exhaustion produces a failed report without a sanitised artifact.
Normal terminal-job retention then applies. Historical failed/blocked jobs are not
rewritten. Source mismatches, refusals and document preservation failures continue
to block output rather than trigger provider recovery.

Reports include negotiated options, parameter fallbacks and exact fence-unwrapping
counts. Synthetic preflight usage is excluded from document metrics.

Reports include provider calls, reported tokens, cache hits, local structural
validation counts, queue time, model time and isolated rechecks. Effective output
tokens/second includes prompt processing and HTTP overhead, not only decoding.
Historical results keep their original reports; resubmit to use current behavior.

Verify the deployed provider with synthetic documents:

```sh
VEIL_CA_BUNDLE=.veil/tls/ca.crt .venv/bin/python scripts/test_live_llm.py --server https://127.0.0.1:8787 \
  --output evidence/llm-only/live
```

This tests real DOCX/PPTX/XLSX/PDF uploads, bilingual DOCX detection, exact-quote
redaction, reports, reopened downloads and batch ZIPs. The ordinary pytest suite
uses an explicitly synthetic OpenAI-compatible oracle for preservation and
transport checks; it is not a model-accuracy benchmark.
