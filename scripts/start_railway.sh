#!/usr/bin/env bash
# Railway / same container: 3xx-wangge (absolute) on :8080 + Protocol FastAPI on $PORT
# Browser hits Protocol public URL → middleware proxies to wangge (dashboard + AI).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WANGGE_DIR="$ROOT/vendor/wangge"
WANGGE_PORT="${WANGGE_PORT:-8080}"
API_PORT="${PORT:-8001}"
WANGGE_PID=""

export WANGGE_INTERNAL_URL="${WANGGE_INTERNAL_URL:-http://127.0.0.1:${WANGGE_PORT}}"
export WANGGE_ENABLED="${WANGGE_ENABLED:-1}"

cleanup() {
  if [[ -n "${WANGGE_PID}" ]] && kill -0 "${WANGGE_PID}" 2>/dev/null; then
    echo "[protocol] stopping wangge pid=${WANGGE_PID}"
    kill "${WANGGE_PID}" 2>/dev/null || true
    wait "${WANGGE_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

start_wangge() {
  local v="${WANGGE_ENABLED:-1}"
  case "${v,,}" in
    0|false|no|off)
      echo "[protocol] wangge skipped (WANGGE_ENABLED off)"
      return 0
      ;;
  esac
  if [[ ! -f "$WANGGE_DIR/src/server.js" ]]; then
    echo "[protocol] ERROR: missing $WANGGE_DIR/src/server.js"
    return 1
  fi
  if ! command -v node >/dev/null 2>&1; then
    echo "[protocol] ERROR: node not installed"
    return 1
  fi

  if [[ ! -d "$WANGGE_DIR/node_modules" ]]; then
    echo "[protocol] npm install in vendor/wangge …"
    (cd "$WANGGE_DIR" && npm install --omit=dev)
  fi

  echo "[protocol] starting wangge (3xx absolute) on 127.0.0.1:${WANGGE_PORT}"
  (
    cd "$WANGGE_DIR"
    # Must not inherit Railway public PORT
    exec env PORT="${WANGGE_PORT}" HOST=127.0.0.1 node src/server.js
  ) &
  WANGGE_PID=$!

  local i=0
  while [[ $i -lt 60 ]]; do
    if ! kill -0 "${WANGGE_PID}" 2>/dev/null; then
      echo "[protocol] wangge exited during boot"
      WANGGE_PID=""
      return 1
    fi
    if curl -sf "http://127.0.0.1:${WANGGE_PORT}/" >/dev/null 2>&1 \
      || curl -sf "http://127.0.0.1:${WANGGE_PORT}/api/overview" >/dev/null 2>&1; then
      echo "[protocol] wangge ready → ${WANGGE_INTERNAL_URL}"
      return 0
    fi
    sleep 0.5
    i=$((i + 1))
  done
  echo "[protocol] WARN: wangge health slow; continuing"
  return 0
}

start_wangge || {
  if [[ "${WANGGE_REQUIRED:-1}" == "1" ]]; then
    echo "[protocol] WANGGE_REQUIRED=1 and wangge failed — abort"
    exit 1
  fi
  echo "[protocol] continuing without wangge"
}

echo "[protocol] starting uvicorn on 0.0.0.0:${API_PORT}"
exec python -m uvicorn main:app --host 0.0.0.0 --port "${API_PORT}"
