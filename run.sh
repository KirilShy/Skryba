#!/usr/bin/env bash
# One-click launcher: pulls the latest dev (only onto a clean tree), reinstalls
# dependencies if anything changed, and (re)starts the server — detached, so
# closing the window that launched this doesn't kill it. Safe to run
# repeatedly: if nothing changed and the server's already up, it just opens
# the browser. Mirrors run.ps1's behavior for parity with the Windows launcher.
set -uo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8420}"
PID_FILE=".server.pid"
OUT_LOG=".server.stdout.log"
ERR_LOG=".server.stderr.log"
PULL_LOG=".autopull.log"

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
is_skryba() { curl -fsS --max-time 2 "http://127.0.0.1:$1/api/capabilities" 2>/dev/null | grep -q '"models"'; }
running_pid() {
  [ -f "$PID_FILE" ] || return 1
  local pid; pid=$(cat "$PID_FILE" 2>/dev/null)
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && echo "$pid"
}

# Pull the latest — but only onto a clean tree, so an unattended double-click
# never clobbers in-progress local edits.
pulled=false
if git rev-parse --git-dir >/dev/null 2>&1; then
  branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
  if [ -z "$(git status --porcelain 2>/dev/null)" ] && [ -n "$branch" ]; then
    before=$(git rev-parse HEAD 2>/dev/null)
    { git fetch origin "$branch" && git merge --ff-only "origin/$branch"; } >"$PULL_LOG" 2>&1
    after=$(git rev-parse HEAD 2>/dev/null)
    [ "$before" != "$after" ] && pulled=true
  else
    echo "Skipped auto-pull: working tree not clean." >"$PULL_LOG"
  fi
fi

if [ ! -x .venv/bin/python ]; then
  echo "No virtualenv found. Run:"
  echo "  uv venv --python 3.13 .venv"
  echo "  uv pip install --python .venv/bin/python -r pyproject.toml"
  exit 1
fi

# New commits landed — reinstall in case a dependency changed. Cheap when
# nothing actually did; uv resolves and no-ops almost instantly.
if $pulled && command -v uv >/dev/null 2>&1; then
  uv pip install --python .venv/bin/python -r pyproject.toml >>"$PULL_LOG" 2>&1
fi

# Load secrets from .env if present (ANTHROPIC_API_KEY, OPENROUTER_API_KEY, HF_TOKEN).
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

pid=$(running_pid || true)
if [ -n "$pid" ] && $pulled; then
  kill "$pid" 2>/dev/null
  sleep 1
  pid=""
fi

if [ -z "$pid" ] && port_busy "$PORT" && ! is_skryba "$PORT"; then
  # Someone else holds the port. Walk up until we find one that is free.
  wanted="$PORT"
  for _ in $(seq 1 40); do
    PORT=$((PORT + 1))
    port_busy "$PORT" || break
  done
  if port_busy "$PORT"; then
    echo "Could not find a free port in ${wanted}-${PORT}." >&2
    exit 1
  fi
  holder=$(lsof -nP -iTCP:"$wanted" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1}')
  echo "Port ${wanted} is taken by ${holder:-another process}; using ${PORT} instead."
fi

if [ -z "$pid" ]; then
  nohup .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
    >"$OUT_LOG" 2>"$ERR_LOG" &
  echo $! >"$PID_FILE"
  for _ in $(seq 1 30); do
    is_skryba "$PORT" && break
    sleep 0.5
  done
  if ! is_skryba "$PORT"; then
    echo "Skryba didn't come up. Last lines of ${ERR_LOG}:" >&2
    tail -n 20 "$ERR_LOG" >&2 2>/dev/null
    exit 1
  fi
fi

echo "Skryba -> http://127.0.0.1:${PORT}"
open "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || true
