> Current deployment: LLM-only document detection, configured with
> `VEIL_LLM_ENDPOINT`, `VEIL_LLM_MODEL`, `VEIL_LLM_API_KEY`, and
> `VEIL_LLM_ALLOW_NETWORK=true` in the server's private `.env`.
> OCR, editing and storage stay local; extracted text goes to the configured LLM.
> Both API and worker receive these settings. No detector comparison UI is shipped.
> Run `.venv/bin/python scripts/test_live_llm.py` for real provider acceptance tests.

# Veil developer guide

Veil is an on-prem document PII redaction service with a browser workspace, REST API, reusable Python engine and the `veil` CLI. English, Simplified/Traditional Chinese and mixed-language documents use the same processing engine. OCR, document editing and storage run on this server. Extracted text is inspected by the configured LLM endpoint, which can be on the private network or an approved hosted provider. Initial OCR dependency/model downloads occur during setup.

## Anonymous private workspaces

No individual sign-in is required. On first visit, Veil issues an opaque browser cookie and assigns each upload/batch/revision to that private workspace. Every document operation checks workspace ownership on the server. Copying a job or batch link to another browser profile grants no access. Tabs in the same browser profile share access; this does not verify a person's identity. Server administrators and the configured model provider remain separate trust boundaries.

The cookie is HttpOnly, Secure and SameSite=Strict over HTTPS. Only a token digest is stored on the server. Access expires after 24 hours of inactivity, with a maximum lifetime of 7 days. Ending the workspace revokes its credential and clears views in other open tabs. Clearing browser cookies also loses access; there is no account recovery. File retention still runs separately, normally 24 hours after each job finishes. A valid cookie can be copied by someone controlling the browser/device, so use separate browser profiles on shared devices.

Old jobs created before workspace ownership was introduced remain inaccessible through the API, including to the first person opening an old link. They are not automatically assigned or deleted and retain their normal cleanup deadlines. Upload a source again to create an owned result.

## HTTPS deployment

The Compose service serves HTTPS directly on port 8787. Before first startup, generate a private CA and server certificate with the approved hostname/IP as a SAN:

```bash
cd /path/to/veil
.venv/bin/python scripts/setup_workspace_tls.py --host veil.example.internal
docker compose up -d --build
```

Trust the **public** `.veil/tls/ca.crt` on each client device; the same certificate is available at `/workspace-ca.crt`. Verify its fingerprint through an administrator before trusting it. Both `.key` files must remain private. The setup script refuses to overwrite an existing server identity. This deployment requires certificate renewal before its one-year server certificate expires. HTTP document access over LAN is rejected. Local development may use HTTP only over an actual loopback connection; it uses a separate cookie and cannot access the HTTPS browser workspace. Forwarded identity/protocol headers are not trusted in this direct-TLS deployment.

Example deployment URLs (replace `veil.example.internal` with your approved hostname):

- Team browser: `https://veil.example.internal:8787`
- Server-side browser/API: `https://127.0.0.1:8787`
- API schema: `/openapi.json`
- Browser guide: `/developer-guide.html`

Use your deployment's approved private hostname or address. Workstation reachability depends on the team's network rules; examples in this guide contain no actual deployment paths or LAN addresses.

```bash
cd /path/to/veil
docker compose up -d --build
docker compose ps
# Stop only Veil, preserving its job volume:
docker compose down
```

To enable the private-interface pilot override, configure `COMPOSE_FILE=compose.yaml:compose.pilot.yaml`, `VEIL_PILOT_ADDRESS` with your deployment address, and the allowed HTTP hostnames in the private `.env`. `compose.pilot.yaml` adds only the private interface binding; the base Compose file retains loopback. An installation without the pilot override publishes loopback by default. Never substitute `0.0.0.0` casually for the pilot address. Add a real deployment hostname to `VEIL_ALLOWED_HOSTS` when using DNS/reverse proxying. The app validates Host and same-origin browser requests.

The API and worker use the bridge network. OCR asset checksums, the configured model, provider calls, token counts and timings are recorded in reports. Use `VEIL_WORKERS=1` and `VEIL_LLM_CONCURRENCY=1` for a single-inference endpoint; other providers can use a higher configured limit. Uploading 50 files does not request 50 worker processes. The API upload spool has a bounded 1 GiB tmpfs for concurrent individual uploads; ZIP assembly uses a temporary file on the deployment data volume, capped separately. Model RAM and large document workloads should be measured before increasing concurrency.

## Browser batches

Select or drag in 1–50 DOCX, PPTX, XLSX or PDF files. Each file is limited to 30 MiB by default. Invalid or oversized files must be removed before starting. Pause/resume affects pending uploads; it does not cancel accepted jobs. Queue-full responses are retried automatically. Processing continues after accepted files reach the server, even if the browser closes.

Each row shows its own outcome. Select View for counts, stage timings, item-specific review tasks and actions, sanitised previews and downloads. Informational notices are collapsed separately and do not gate completion. Use Re-run detection on a finished job to apply the current model and criteria as a new revision. See [review criteria](REVIEW_CRITERIA.md). Original extracted text stays behind the explicit Reveal control. A batch can contain completed, review_required, blocked and failed outcomes; finishing a batch is not an all-clear verdict.

The ZIP contains numbered job folders with sanitised documents/text and processing reports. Blocked/failed jobs contribute reports only. Originals and original extraction are never bundled. A partial ZIP is explicitly labelled partial and includes only currently finished results. Deleted/expired jobs are identified in the batch summary. The configurable ZIP limit defaults to 2 GiB; per-file downloads remain available above it.

Original filenames stay in the browser tab and are sent to the API as generic upload names. Use Save filename index to save a sensitive mapping on your workstation if you need it. The server ZIP uses ordinals/job IDs. A bookmarked batch link restores server-side statuses; filenames and unsubmitted source files cannot be restored from the server. Start a new batch for files that never reached the service.

A quote that does not match its source is rechecked once as an isolated segment (at most eight rechecks per document). An invalid retry still blocks output; the response is never accepted by fuzzy matching. Malformed JSON, incomplete response structures and provider outages pause jobs as `waiting_provider`; the shared worker runs a synthetic compatibility check and resumes the same job automatically when the provider recovers. No partial output is available while waiting. The default retry window is 15 minutes with at most three failed document attempts, then a value-free failed report. Source mismatches and refusals still block output. `GET /api/provider/status` returns cached readiness without triggering inference. See [provider configuration and recovery](app/README.md). Historical results keep their original status; use Re-run detection to create a new revision after a fix.

Default retention is 24 hours for finished jobs, configurable through `VEIL_RETENTION_HOURS`. Originals are stored separately and never overwritten. Batch metadata expires under the same retention scheme. Explicit job deletion removes its stored data; it does not cancel already processing jobs.

## Correct missed PII and re-run

Open a finished document using View. Under the sanitised preview, select missed text and choose **Use selected text**, or enter it in **Missed sensitive text**. Choose **Add phrase** for each correction, then **Apply manual redactions**. Literal matches ignore case; spacing must match. Every matching occurrence is redacted, including hidden/metadata text inspected by the pipeline. These corrections run locally without a model request and regenerate both the document and text preview. PDF matches are masked in the flattened output; OCR text that was never extracted requires source editing and another upload.

**Re-run detection** inspects the retained original with the currently configured model. Pending added phrases are included, and earlier detections/manual corrections stay mandatory even if the new model misses them. Blocked/failed jobs can also be re-run; unsupported content and source/preservation validation still apply. A completed revision is not a zero-PII guarantee.

Both actions create a new job ID and update that document's batch position to the new revision, so later batch ZIPs use its result. The previous output remains available through **Previous result** until its own cleanup deadline; **Newer result** opens its successor. Changes do not overwrite retained originals or previous downloads. Each revision has its own normal retention period. If the original has expired/deleted, upload it again.

Correction phrases and retained detection values stay with private original data and follow the job's retention/deletion policy. They are excluded from reports, logs and ZIPs. Phrase drafts are held only in browser memory. A revision request uses the API, for example:

```http
POST /api/jobs/JOB_ID/reprocess
Content-Type: application/json

{"mode":"manual","redactions":["SYNTHETIC MISSED TEXT"]}
```

Use `"mode":"rerun"` with an optional phrase list for another model pass. HTTP 202 returns the new `id`, `batch_id` and `parent_id`. Only finished jobs can create revisions; 409 indicates an unavailable source, a phrase mismatch or a newer existing revision, and 429 indicates queue capacity. Requests allow up to 100 phrases of 1–512 characters each. `/#job=JOB_ID` opens a retained individual revision.

## CLI setup and endpoint

The tested project runtime is Python 3.11. Install from the repository on your server:

```bash
cd /path/to/veil
PYTHON_BIN=/path/to/python3.11 ./scripts/setup.sh
source .venv/bin/activate
veil --help
export VEIL_SERVER=https://veil.example.internal:8787
export VEIL_CA_BUNDLE=/path/to/veil-workspace-ca.crt
```

A workstation with the repository and tested environment can call the same endpoint. The CLI owns its own anonymous workspace; it does not inherit the browser workspace. Its cookie jar is saved in a private per-server configuration file (permissions 600), so subsequent status/report/download commands retain access. Set `VEIL_WORKSPACE_COOKIE_FILE` to choose another private cookie file. Never share or log that credential file. Dataset generation and benchmarking run on the machine invoking those commands; redaction/status/report/download commands call the configured API. The default endpoint remains `https://127.0.0.1:8787` for server-side convenience, and does not imply that Veil is a single-user desktop tool.

```bash
veil redact ./report.pdf --output ./sanitised
veil redact ./documents --recursive --output ./sanitised
veil redact ./report.docx --async --json
veil status JOB_ID --json
veil report JOB_ID --json
veil download JOB_ID --output ./downloaded
veil delete JOB_ID --json
# Override the endpoint on an individual command:
veil redact ./report.xlsx --server https://veil.example.internal:8787 --output ./sanitised
```

`redact` waits and downloads by default; `--async` returns accepted job IDs. Recursive CLI batches preserve relative parent directories and place each result under an opaque job ID. CLI directory processing is sequential and is not limited to 50 files; the 50-document limit applies to each browser/API batch group. Existing outputs and originals are never silently overwritten. The output directory must be outside a recursively processed input tree.

Exit codes: 0 = all completed or async submissions accepted; 2 = at least one review_required/blocked; 1 = failure; 64 = command usage. Batch JSON reports each file. `--json` emits one object on stdout; progress stays on stderr. Status/report/download commands return 0 for retrieval success, so inspect the job status separately. CLI JSON includes caller-side input paths; handle those as potentially sensitive.

```bash
veil datasets generate --suite multilingual --output ./datasets-new --json
veil benchmark ./datasets-new --output ./benchmark-new --concurrency 2 --json
pytest -q
```

Keep benchmark directories new/empty. Historical stripping-profile measurements are not current preservation-profile certifications; read PRESERVATION.md and the applicable benchmark notes.

## REST API and batch contract

API clients first obtain their own anonymous workspace, retain its cookie jar, and send `X-Veil-Request: 1` on mutations. This required custom header, strict cookies and same-origin checks reject cross-site form requests. Cookie credentials are never accepted in URLs or request ownership fields. Keep the cookie jar private:

```bash
umask 077
curl --cacert /path/to/veil-workspace-ca.crt -c ./veil.cookies https://veil.example.internal:8787/api/workspace
# For every following curl command, retain the cookie jar and trust the CA:
# --cacert /path/to/veil-workspace-ca.crt -b ./veil.cookies -c ./veil.cookies -H 'X-Veil-Request: 1'
```

The existing single-file API paths remain available within the caller's workspace:

```bash
curl --cacert /path/to/veil-workspace-ca.crt -b ./veil.cookies -c ./veil.cookies -H 'X-Veil-Request: 1' -F 'file=@report.pdf;filename=upload.pdf' https://veil.example.internal:8787/api/jobs
curl --cacert /path/to/veil-workspace-ca.crt -b ./veil.cookies https://veil.example.internal:8787/api/jobs/JOB_ID
curl --cacert /path/to/veil-workspace-ca.crt -b ./veil.cookies https://veil.example.internal:8787/api/jobs/JOB_ID/report
curl --cacert /path/to/veil-workspace-ca.crt -b ./veil.cookies -c ./veil.cookies -H 'X-Veil-Request: 1' -o sanitised.pdf https://veil.example.internal:8787/api/jobs/JOB_ID/download/sanitised.pdf
```

For batches, create the group, then submit each file in its own bounded request. This avoids a 1.5 GiB multipart request for 50 maximum-size documents.

```bash
curl --cacert /path/to/veil-workspace-ca.crt -b ./veil.cookies -c ./veil.cookies -H 'X-Veil-Request: 1' -H 'Content-Type: application/json' -d '{"count":2}' https://veil.example.internal:8787/api/batches
# Use the returned opaque batch ID; positions are zero-based and unique:
curl --cacert /path/to/veil-workspace-ca.crt -b ./veil.cookies -c ./veil.cookies -H 'X-Veil-Request: 1' -F 'batch_id=BATCH_ID' -F 'batch_index=0' -F 'file=@one.docx;filename=upload.docx' https://veil.example.internal:8787/api/jobs
curl --cacert /path/to/veil-workspace-ca.crt -b ./veil.cookies -c ./veil.cookies -H 'X-Veil-Request: 1' -F 'batch_id=BATCH_ID' -F 'batch_index=1' -F 'file=@two.pdf;filename=upload.pdf' https://veil.example.internal:8787/api/jobs
curl --cacert /path/to/veil-workspace-ca.crt -b ./veil.cookies https://veil.example.internal:8787/api/batches/BATCH_ID
curl --cacert /path/to/veil-workspace-ca.crt -b ./veil.cookies -c ./veil.cookies -H 'X-Veil-Request: 1' -o batch.zip https://veil.example.internal:8787/api/batches/BATCH_ID/download
# Explicit partial download while other documents remain outstanding:
curl --cacert /path/to/veil-workspace-ca.crt -b ./veil.cookies -c ./veil.cookies -H 'X-Veil-Request: 1' -o partial.zip 'https://veil.example.internal:8787/api/batches/BATCH_ID/download?partial=true'
```

| Endpoint | Behavior |
|---|---|
| `GET /api/workspace` | Initialize/refresh an anonymous cookie workspace; no document access token in the body. |
| `POST /api/workspace/end` | Revoke current browser workspace access (204); stored files keep their retention policy. |
| `GET /api/config` | Public deployment limits and supported formats; no model credentials or source content. |
| `POST /api/batches` | JSON integer count 1–50; returns batch ID (201). |
| `POST /api/jobs` | File plus optional paired batch_id/batch_index; returns job ID (202). |
| `GET /api/batches/{id}` | Ordinals, per-job states/counts/timings and aggregate counts. No original filenames. |
| `GET /api/batches/{id}/download` | Finished-result ZIP; 409 if incomplete unless partial=true; 413 above bundle limit. |
| `GET /api/jobs/{id}/report` | Value-free report when terminal. |
| `POST /api/jobs/{id}/reprocess` | New manual/rerun revision from the retained original (202); updates its batch position. |
| `DELETE /api/jobs/{id}` | Delete queued/finished job; active processing/receiving deletion returns 409. |

HTTP 401 means the workspace cookie is missing/expired; 404 covers unknown, ownerless and other-workspace objects alike. All previews, source reveal, reports, ZIPs, revisions, deletions and batch uploads enforce ownership. HTTP 429 means queue capacity is full; honor `Retry-After` and retry. A network failure can occur after acceptance: reconcile the batch index through batch status before retrying. Duplicate positions return 409 and never overwrite an existing job. An interrupted/invalid upload releases its slot. Unsupported file types (415), oversized requests (413), and invalid batch positions/counts do not create successful jobs. Failed/blocked document processing still has a report.

## MCP status and future adapter

**Veil does not currently ship an MCP server or MCP tools.** There is no `veil mcp` command, `/mcp` endpoint or runnable MCP client configuration in this release. The working integration interfaces are the CLI, REST API and reusable engine. Browser batching is implemented independently of MCP.

A future MCP adapter should wrap this REST API and reuse processing, not duplicate redaction logic. Suggested tool mappings are `submit_document` → POST /api/jobs, `inspect_job` → GET status/report, and `download_sanitised` → the allowed artifact endpoints. A future adapter must constrain file access, keep processing on-prem, enforce deployment authorization and avoid returning original text/PII to an external model. These mappings are an architectural guide, not implemented tools. Do not give an MCP client filesystem or original-text access on the assumption that this bridge already exists.

## Processing boundaries

Supported Office formulas, comments and tracked changes are retained and PII redacted in place. Unsupported formula constructs block instead of being deleted; some caches require recalculation/review. Opaque retained content is flagged. PDF output remains flattened, losing searchable text and interactive structures. No interface status guarantees zero PII or full fidelity. See PRESERVATION.md for supported operations and limits.

## Release and dependency maintenance

Veil 1.0 is AGPL-3.0-only. Preserve the visible source link and corresponding-source offer. Runtime, build and development dependencies have separate hash-pinned locks. CI scans all tracked source paths and Git history, audits dependencies, tests processing/ownership and builds the image. Local evidence, credentials, documents, cookie jars, private keys and deployment certificates must not enter Git. See SECURITY.md and RELEASING.md.

Anonymous workspace creation is limited by `VEIL_WORKSPACE_CREATIONS_PER_HOUR` (default 60 per connection peer) and `VEIL_MAX_WORKSPACES` (default 10,000). Reusing a valid cookie does not consume a new-workspace allowance. These controls complement perimeter rate limits and storage monitoring. The direct-TLS deployment does not trust forwarded client IP headers.
