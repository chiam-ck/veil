# Veil 1.0

Veil redacts personal data from DOCX, PPTX, XLSX and PDF documents using an administrator-configured, OpenAI-compatible LLM. OCR, document editing and storage run on your server. Extracted text goes to your approved model endpoint.

The browser workspace supports batches of up to 50 documents, previews, review findings, individual downloads and batch ZIPs. English, Simplified Chinese, Traditional Chinese and mixed-language text use the same pipeline. Detection can miss personal data; completion is not a zero-PII guarantee.

## Features

- Anonymous private browser workspaces: every document operation checks server-side ownership. A copied link grants no access to another browser profile.
- HTTPS with private certificates, protected cookies, same-origin request checks and bounded session creation.
- Manual corrections: select missed text, add phrases and regenerate the document locally. Re-run detection with the current model while preserving earlier redactions.
- Provider compatibility checks, cached negotiated API options, bounded inference concurrency and automatic outage recovery.
- Supported Office structures are preserved and redacted in place. Unsupported or unverified content is blocked or flagged for review. PDF output is flattened.
- Configurable cleanup of originals, extracted text, outputs and reports; 24 hours after each job finishes by default.
- REST API and CLI with the same ownership and processing rules.

## Quick start

Requirements: Docker Engine with Compose, Python 3 for certificate setup, and OpenSSL. The Python application itself is tested on Python 3.11. No GPU is required.

```bash
cp .env.example .env
chmod 600 .env
```

Edit the private `.env` with your approved `VEIL_LLM_ENDPOINT`, `VEIL_LLM_MODEL`, `VEIL_LLM_API_KEY` and `VEIL_LLM_ALLOW_NETWORK=true`. The example deliberately disables model network access until you opt in. Its endpoint/model values are placeholders, not a hosted service supplied by Veil.

```bash
# Use your approved hostname/IP instead of localhost for access from other devices.
python3 scripts/setup_workspace_tls.py --host localhost
docker compose up -d --build
docker compose ps
curl --cacert .veil/tls/ca.crt https://127.0.0.1:8787/api/health
```

Trust the **public** `.veil/tls/ca.crt` on client devices, then open **https://localhost:8787**. Verify its fingerprint through your administrator. The public certificate is also available at `/workspace-ca.crt`; private keys are never served. Do not bypass certificate validation in normal clients.

The base Compose file publishes loopback only. For an approved private interface, set `COMPOSE_FILE=compose.yaml:compose.pilot.yaml`, fill `VEIL_PILOT_ADDRESS` and `VEIL_ALLOWED_HOSTS`, and generate a certificate containing that hostname/IP. Set `VEIL_UID` and `VEIL_GID` to your host user's numeric IDs if they differ from 1000, so the private data directory remains writable without broadening its permissions.

Provider credentials can alternatively use a private file: add `compose.secrets.yaml` to `COMPOSE_FILE` and create the file referenced by `VEIL_LLM_SECRET_FILE`. Keep that file private and outside version control. The `.env` example documents both paths.

## Private workspaces and retention

The browser receives an opaque HttpOnly, Secure, SameSite=Strict cookie. Only a token digest is stored on the server. Uploads, batches and revisions belong to that workspace; previews, source reveal, reports, downloads, ZIPs, reprocessing and deletion all enforce ownership.

Tabs in the same browser profile share access. Use separate profiles on shared computers. **End workspace** revokes the credential and clears other open tabs. Clearing cookies or exceeding 24 hours idle / 7 days absolute loses access; there is no account recovery. Server administrators and the configured model provider remain separate trust boundaries.

Files follow their own retention deadline, normally 24 hours after each job finishes. Ending a workspace does not immediately delete its retained files. Cleanup runs while the worker supervisor is running; stopped services and external backups require separate operational handling. Explicit deletion can remove queued or finished jobs sooner. Downloads on a workstation are outside server retention.

Upgrades never assign old ownerless jobs to the first visitor opening their links. Those jobs remain inaccessible through the API and retain their normal cleanup deadlines; upload an original again to create an owned result.

## Correct missed PII

Open a finished result, select text in its preview, choose **Use selected text**, then **Add phrase** and **Apply manual redactions**. Literal matching ignores case and preserves exact spacing. The original is processed again locally with mandatory earlier redactions; both the real document and text preview are regenerated.

**Re-run detection** uses the current model and retains prior automatic/manual redactions. Each action creates a revision and updates its batch position. Previous outputs remain available until their own cleanup deadline. Text that OCR never extracted requires source editing and another upload.

## CLI and API

```bash
PYTHON_BIN=python3.11 ./scripts/setup.sh
source .venv/bin/activate
export VEIL_SERVER=https://localhost:8787
export VEIL_CA_BUNDLE=/path/to/veil-workspace-ca.crt
veil redact ./report.docx --output ./sanitised
veil status JOB_ID --json
veil download JOB_ID --output ./downloaded
```

The CLI has its own anonymous workspace, stored in a private per-server cookie jar. It cannot use a browser-owned link without that browser credential. Never share cookie jars. API clients first request `GET /api/workspace`, retain its cookie, and send `X-Veil-Request: 1` on mutations. The API schema is `/openapi.json`.

See the [developer guide](DEVELOPER_GUIDE.md) for endpoint contracts, TLS setup, retries, CLI exit codes and deployment details; [provider configuration](app/README.md) for budgets and compatibility; [preservation limits](PRESERVATION.md) and [review criteria](REVIEW_CRITERIA.md) before sharing results.

## Security and operations

Use an approved private network. Anonymous workspace isolation does not provide verified identities, enterprise authorization or a general-purpose public upload service. Apply perimeter rate limits, storage monitoring and backups appropriate to your deployment. Provider coverage and OCR quality must be evaluated on representative documents.

The image runs as a non-root user with a read-only root filesystem, dropped capabilities and resource limits. TLS is served directly; forwarded identity/protocol headers are not trusted. No source text or provider credentials are included in ordinary reports or access logs. Workspace creation is bounded by a per-peer hourly quota and a global capacity; document uploads and batch exports have separate configured bounds.

See [SECURITY.md](SECURITY.md) for the threat model and private vulnerability reporting. This release is not a DLP certification or a promise of perfect detection.

## Development and verification

```bash
PYTHON_BIN=python3.11 ./scripts/setup.sh
.venv/bin/pytest -q
.venv/bin/python scripts/release_check.py --tracked
.venv/bin/pip-audit -r requirements.lock --no-deps --disable-pip
```

Tests use a synthetic provider for transport/preservation checks. To test your real configured model with synthetic documents:

```bash
VEIL_CA_BUNDLE=.veil/tls/ca.crt .venv/bin/python scripts/test_live_llm.py \
  --server https://localhost:8787 --output evidence/llm-live
```

Live tests create real jobs. Optional browser checks require Playwright installed under `.browser` and Chrome; set `VEIL_CHROME_EXECUTABLE` and `VEIL_TEST_URL` for your environment. Their certificate-validation bypass is for automated private-CA testing only. No real credentials are required by CI.

The source export is an explicit allowlist. Runtime data, `.env` variants, keys/certificates, cookie jars, documents, screenshots, logs, historical evidence, generated builds and local agent/cloud state are excluded. Enable the supplied pre-commit guard with `git config core.hooksPath .githooks`. Release checks inspect actual Git index blobs as well as all tracked paths; ignore rules do not remove files already committed. See [RELEASING.md](RELEASING.md).

## License

Veil is **AGPL-3.0-only**. See [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY.md). PyMuPDF/MuPDF also has AGPL/commercial licensing; Veil's source license does not grant a commercial dependency license. Source for this version, including build instructions, is available in the matching GitHub release. Preserve the source offer when deploying modified versions to network users.
