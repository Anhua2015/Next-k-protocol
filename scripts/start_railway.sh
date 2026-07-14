#!/usr/bin/env bash
# Railway / same Protocol container: Bitget grid Worker (:8080) + Protocol FastAPI ($PORT)
# Public: uvicorn exposes /grid-bot/* → Worker. next-k-api GRID_URL=https://<protocol>/grid-bot
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GRID_DIR="$ROOT/vendor/bitget-grid"
GRID_PORT="${BITGET_GRID_PORT:-8080}"
API_PORT="${PORT:-8001}"
GRID_PID=""

export BITGET_GRID_INTERNAL_URL="${BITGET_GRID_INTERNAL_URL:-http://127.0.0.1:${GRID_PORT}}"
export GRID_WORKER_ENABLED="${GRID_WORKER_ENABLED:-1}"

cleanup() {
  if [[ -n "${GRID_PID}" ]] && kill -0 "${GRID_PID}" 2>/dev/null; then
    echo "[protocol] stopping grid worker pid=${GRID_PID}"
    kill "${GRID_PID}" 2>/dev/null || true
    wait "${GRID_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

want_grid() {
  local v="${GRID_WORKER_ENABLED:-1}"
  case "${v,,}" in
    0|false|no|off) return 1 ;;
  esac
  return 0
}

start_grid_worker() {
  if ! want_grid; then
    echo "[protocol] grid worker skipped (GRID_WORKER_ENABLED off)"
    return 0
  fi
  if [[ ! -f "$GRID_DIR/server.js" ]]; then
    echo "[protocol] grid worker skipped (missing $GRID_DIR/server.js)"
    return 0
  fi
  if [[ -z "${BITGET_API_KEY:-}" || -z "${BITGET_API_SECRET:-}" || -z "${BITGET_PASSPHRASE:-}" ]]; then
    echo "[protocol] grid worker skipped (need BITGET_API_KEY / SECRET / PASSPHRASE)"
    return 0
  fi
  if ! command -v node >/dev/null 2>&1; then
    echo "[protocol] ERROR: node missing — rebuild Docker image with Node"
    return 1
  fi

  echo "[protocol] starting Bitget grid worker on 127.0.0.1:${GRID_PORT} (node $(node -v))"
  (
    cd "$GRID_DIR"
    exec env \
      PORT="${GRID_PORT}" \
      BITGET_GRID_PORT="${GRID_PORT}" \
      GRID_AUTH_TOKEN="${GRID_AUTH_TOKEN:-}" \
      node server.js
  ) &
  GRID_PID=$!

  local i=0
  while [[ $i -lt 40 ]]; do
    if ! kill -0 "${GRID_PID}" 2>/dev/null; then
      echo "[protocol] grid worker exited during boot"
      GRID_PID=""
      return 1
    fi
    if curl -sf "http://127.0.0.1:${GRID_PORT}/api/health" >/dev/null 2>&1; then
      echo "[protocol] grid worker ready → ${BITGET_GRID_INTERNAL_URL}"
      echo "[protocol] public path for next-k-api GRID_URL: https://<this-host>/grid-bot"
      return 0
    fi
    sleep 0.5
    i=$((i + 1))
  done
  echo "[protocol] WARN: grid health slow; FastAPI still starting"
  return 0
}

start_grid_worker || {
  if [[ "${GRID_WORKER_REQUIRED:-0}" == "1" ]]; then
    echo "[protocol] GRID_WORKER_REQUIRED=1 and worker failed — abort"
    exit 1
  fi
  echo "[protocol] continuing without grid worker"
}

echo "[protocol] starting uvicorn on 0.0.0.0:${API_PORT}"
exec python -m uvicorn main:app --host 0.0.0.0 --port "${API_PORT}"
