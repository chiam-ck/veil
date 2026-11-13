#!/usr/bin/env bash
# Update only Veil; never print expanded Compose configuration or credentials.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo 'Create the private .env from .env.example first.' >&2; exit 1; }
[[ -x .venv/bin/python ]] || { echo 'Run scripts/setup.sh for the verification environment first.' >&2; exit 1; }
[[ -f .veil/tls/server.crt && -f .veil/tls/server.key && -f .veil/tls/ca.crt ]] || { echo 'Generate the deployment TLS identity first.' >&2; exit 1; }
chmod 600 .env
(
    set -a
    source .env
    set +a
    .venv/bin/python - <<'PY'
from veil.llm import LLMSettings
settings=LLMSettings.from_env()
if not settings.allow_network:raise SystemExit('Enable the approved model endpoint before deploying document processing.')
print('Provider configuration validated; credentials omitted.')
PY
)
docker compose config --quiet
docker compose build veil-api
.venv/bin/python - <<'PY'
import sqlite3,time
from pathlib import Path
p=Path('.veil/jobs.sqlite3')
if p.exists():
    deadline=time.monotonic()+660
    while True:
        with sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True) as db:
            active=db.execute("SELECT count(*) FROM jobs WHERE status='processing'").fetchone()[0]
        if not active:break
        if time.monotonic()>deadline:raise SystemExit('Active jobs remain; no services were restarted. Retry after completion.')
        print('Waiting for active jobs before restarting Veil…',flush=True);time.sleep(10)
PY
docker compose up -d --no-build --wait --wait-timeout 120
.venv/bin/python - <<'PY'
import ssl,urllib.request
context=ssl.create_default_context(cafile='.veil/tls/ca.crt')
with urllib.request.urlopen('https://127.0.0.1:8787/api/health',context=context) as response:
    assert response.status==200
print('Veil HTTPS health verified.')
PY
