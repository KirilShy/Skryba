#!/usr/bin/env bash
# Start the transcriber and open it in your browser.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "No virtualenv found. Run:  uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -e ."
  exit 1
fi

PORT="${PORT:-8420}"

# Load secrets from .env if present (ANTHROPIC_API_KEY, HF_TOKEN).
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

( sleep 1.5; open "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || true ) &
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" "$@"
