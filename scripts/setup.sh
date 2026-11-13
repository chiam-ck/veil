#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
mkdir -p .veil
chmod 700 .veil
.venv/bin/python -c 'from veil.engine import Engine; e=Engine(); print("Local OCR models ready; configure the LLM endpoint before processing",round(e.cold_start,3))'
