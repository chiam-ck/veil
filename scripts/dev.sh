#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi
if [[ -f .veil/tls/server.crt && -f .veil/tls/server.key ]]; then
    export VEIL_TLS_CERT_FILE="$(pwd)/.veil/tls/server.crt"
    export VEIL_TLS_KEY_FILE="$(pwd)/.veil/tls/server.key"
fi
mkdir -p .veil
chmod 700 .veil
.venv/bin/veil-worker &
worker_pid=$!
trap 'kill "$worker_pid" 2>/dev/null || true; wait "$worker_pid" 2>/dev/null || true' EXIT INT TERM
.venv/bin/veil-api
