#!/usr/bin/env bash
# stop.sh — 停止 Next K Protocol
# 用法：./stop.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$SCRIPT_DIR/.pid"
PID_FILE="$PID_DIR/api.pid"
GRACEFUL_TIMEOUT=15

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[next-k-protocol]${NC} $*"; }
warn()  { echo -e "${YELLOW}[next-k-protocol]${NC} $*"; }
error() { echo -e "${RED}[next-k-protocol]${NC} $*" >&2; }

if [[ ! -f "$PID_FILE" ]]; then
    warn "未找到 PID 文件（$PID_FILE），服务可能未在运行。"
    exit 0
fi

PID=$(cat "$PID_FILE")

if ! kill -0 "$PID" 2>/dev/null; then
    warn "进程（PID=$PID）已不在运行，清理 PID 文件。"
    rm -f "$PID_FILE"
    exit 0
fi

info "API 进程（PID=$PID）：发送 SIGTERM..."
kill -TERM "$PID" 2>/dev/null || true

elapsed=0
while kill -0 "$PID" 2>/dev/null; do
    if [[ $elapsed -ge $GRACEFUL_TIMEOUT ]]; then
        warn "API 进程（PID=$PID）在 ${GRACEFUL_TIMEOUT}s 内未退出，强制 SIGKILL..."
        kill -KILL "$PID" 2>/dev/null || true
        break
    fi
    sleep 1
    elapsed=$((elapsed + 1))
done

if kill -0 "$PID" 2>/dev/null; then
    error "API 进程（PID=$PID）SIGKILL 后仍在运行，请手动处理：kill -9 $PID"
else
    info "API 进程（PID=$PID）已停止。"
fi

rm -f "$PID_FILE"

if [[ -d "$PID_DIR" ]] && [[ -z "$(ls -A "$PID_DIR" 2>/dev/null)" ]]; then
    rmdir "$PID_DIR" 2>/dev/null || true
fi

echo ""
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo -e "${GREEN}  Next K Protocol 已停止${NC}"
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo -e "  启动服务：./start.sh"
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo ""
