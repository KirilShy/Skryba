#!/usr/bin/env bash
# Start Skryba and open it in your browser.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "No virtualenv found. Run:"
  echo "  uv venv --python 3.13 .venv"
  echo "  uv pip install --python .venv/bin/python -r pyproject.toml"
  exit 1
fi

# Load secrets from .env if present (ANTHROPIC_API_KEY, OPENROUTER_API_KEY, HF_TOKEN).
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

# Is that a Skryba already serving, rather than an unrelated process? Then there
# is nothing to start — just open it.
is_skryba() { curl -fsS --max-time 2 "http://127.0.0.1:$1/api/capabilities" 2>/dev/null | grep -q '"models"'; }

WANTED="${PORT:-8420}"
PORT="$WANTED"

if port_busy "$PORT"; then
  if is_skryba "$PORT"; then
    echo "Skryba is already running on http://127.0.0.1:${PORT}"
    open "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || true
    exit 0
  fi
  # Someone else holds the port. Walk up until we find one that is free.
  for _ in $(seq 1 40); do
    PORT=$((PORT + 1))
    port_busy "$PORT" || break
  done
  if port_busy "$PORT"; then
    echo "Could not find a free port in ${WANTED}-${PORT}." >&2
    exit 1
  fi
  holder=$(lsof -nP -iTCP:"$WANTED" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1}')
  echo "Port ${WANTED} is taken by ${holder:-another process}; using ${PORT} instead."
fi

echo "Skryba -> http://127.0.0.1:${PORT}"
( sleep 1.5; open "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || true ) &
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" "$@"
