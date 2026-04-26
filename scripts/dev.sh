#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVER_DIR="$ROOT_DIR/server"
CLIENT_DIR="$ROOT_DIR/client"
SERVER_PYTHON="$SERVER_DIR/.venv/bin/python"
SERVER_HOST="${SERVER_HOST:-0.0.0.0}"
SERVER_PORT="${SERVER_PORT:-8000}"
CLIENT_HOST="${CLIENT_HOST:-0.0.0.0}"
CLIENT_PORT="${CLIENT_PORT:-5173}"

SERVER_PID=""
CLIENT_PID=""

log() {
  printf '[paper-agent] %s\n' "$*"
}

cleanup() {
  local exit_code=$?
  trap - INT TERM EXIT

  if [[ -n "$CLIENT_PID" ]] && kill -0 "$CLIENT_PID" 2>/dev/null; then
    log "stopping client pid=$CLIENT_PID"
    kill "$CLIENT_PID" 2>/dev/null || true
  fi

  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    log "stopping server pid=$SERVER_PID"
    kill "$SERVER_PID" 2>/dev/null || true
  fi

  wait "$CLIENT_PID" "$SERVER_PID" 2>/dev/null || true
  exit "$exit_code"
}

require_file() {
  local path="$1"
  local message="$2"
  if [[ ! -e "$path" ]]; then
    printf 'Error: %s\n' "$message" >&2
    exit 1
  fi
}

require_command() {
  local command="$1"
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'Error: required command not found: %s\n' "$command" >&2
    exit 1
  fi
}

require_file "$SERVER_PYTHON" "server virtualenv not found at $SERVER_PYTHON. Create it with: cd server && python -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
require_file "$CLIENT_DIR/package.json" "client/package.json not found"
require_command npm

if [[ ! -d "$CLIENT_DIR/node_modules" ]]; then
  log "client/node_modules not found; running npm install"
  (cd "$CLIENT_DIR" && npm install)
fi

trap cleanup INT TERM EXIT

log "starting server on http://$SERVER_HOST:$SERVER_PORT using $SERVER_PYTHON"
(
  cd "$SERVER_DIR"
  "$SERVER_PYTHON" -m uvicorn app.main:app --reload --host "$SERVER_HOST" --port "$SERVER_PORT"
) &
SERVER_PID=$!

log "starting client on http://$CLIENT_HOST:$CLIENT_PORT"
(
  cd "$CLIENT_DIR"
  npm run dev -- --host "$CLIENT_HOST" --port "$CLIENT_PORT"
) &
CLIENT_PID=$!

log "server pid=$SERVER_PID, client pid=$CLIENT_PID"
log "press Ctrl-C to stop both processes"

wait -n "$SERVER_PID" "$CLIENT_PID"
