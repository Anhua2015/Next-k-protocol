#!/usr/bin/env bash
# start.sh — 启动 Next K Protocol（币安实盘交易 API）
# 用法：./start.sh
# 环境变量覆盖：PORT=9000 ./start.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PID_DIR="$SCRIPT_DIR/.pid"
LOG_DIR="$SCRIPT_DIR/logs"
PID_FILE="$PID_DIR/api.pid"
LOG_FILE="$LOG_DIR/api.log"
ENV_FILE="$SCRIPT_DIR/.env.oi"
ENV_EXAMPLE="$SCRIPT_DIR/.env.oi.example"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[next-k-protocol]${NC} $*"; }
warn()  { echo -e "${YELLOW}[next-k-protocol]${NC} $*"; }
error() { echo -e "${RED}[next-k-protocol]${NC} $*" >&2; }

is_running() {
    local pid_file="$1"
    [[ -f "$pid_file" ]] || return 1
    local pid
    pid=$(cat "$pid_file")
    kill -0 "$pid" 2>/dev/null
}

if is_running "$PID_FILE"; then
    warn "API 进程已在运行（PID=$(cat "$PID_FILE")），跳过启动。"
    warn "如需重启，请先运行：./stop.sh"
    exit 0
fi

info "检查 Python 版本..."

PYTHON_BIN=""
for py in python3.11 python3 python; do
    if command -v "$py" &>/dev/null; then
        ver=$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [[ "$major" -eq 3 && "$minor" -ge 11 ]]; then
            PYTHON_BIN="$py"
            break
        fi
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    error "未找到 Python 3.11+。请先安装 Python 3.11 或更高版本。"
    exit 1
fi
info "使用 Python: $PYTHON_BIN ($(${PYTHON_BIN} --version 2>&1))"

if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
    info "创建虚拟环境：$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
else
    info "复用已有虚拟环境：$VENV_DIR"
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
PYTHON_VENV="$VENV_DIR/bin/python"

info "安装依赖（$REQUIREMENTS）..."
"$PYTHON_VENV" -m pip install --quiet --upgrade pip
"$PYTHON_VENV" -m pip install --quiet -r "$REQUIREMENTS"
info "依赖安装完成。"

if [[ ! -f "$ENV_FILE" ]]; then
    if [[ -f "$ENV_EXAMPLE" ]]; then
        warn ".env.oi 不存在，从 .env.oi.example 复制..."
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        warn "请编辑 $ENV_FILE 并设置 PROTOCOL_MAINTENANCE_TOKEN 等必要变量后重新启动。"
    else
        warn ".env.oi 和 .env.oi.example 均不存在，将使用默认配置启动。"
    fi
fi

if [[ -f "$ENV_FILE" ]]; then
    while IFS='=' read -r key value; do
        key=$(echo "$key" | xargs)
        [[ -z "$key" || "$key" == \#* ]] && continue
        [[ "$key" != *[[:space:]]* ]] || continue
        value=$(echo "$value" | sed 's/[[:space:]]*#.*//' | xargs)
        if [[ -z "${!key+x}" ]]; then
            export "$key"="$value"
        fi
    done < <(grep -v '^\s*#' "$ENV_FILE" | grep '=')
fi

PORT="${PORT:-8001}"

mkdir -p "$PID_DIR" "$LOG_DIR"

info "启动 Next K Protocol（端口 $PORT）..."
nohup "$PYTHON_VENV" -m uvicorn main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --log-level info \
    >> "$LOG_FILE" 2>&1 &
API_PID=$!
echo "$API_PID" > "$PID_FILE"
info "API 进程已启动（PID=$API_PID），日志：$LOG_FILE"

info "等待 API 就绪..."
WAIT_MAX=30
WAIT_COUNT=0
while [[ $WAIT_COUNT -lt $WAIT_MAX ]]; do
    if curl -sf "http://localhost:${PORT}/api/binance/health" >/dev/null 2>&1; then
        info "API 就绪：http://localhost:${PORT}"
        break
    fi
    if ! kill -0 "$API_PID" 2>/dev/null; then
        error "API 进程意外退出。请检查日志：$LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
    sleep 1
    WAIT_COUNT=$((WAIT_COUNT + 1))
done

if [[ $WAIT_COUNT -ge $WAIT_MAX ]]; then
    warn "API 未在 ${WAIT_MAX}s 内响应，可能仍在加载中。请检查：$LOG_FILE"
fi

echo ""
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo -e "${GREEN}  Next K Protocol 启动成功${NC}"
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo -e "  API 地址     : http://localhost:${PORT}"
echo -e "  Swagger 文档 : http://localhost:${PORT}/docs"
echo -e "  健康检查     : http://localhost:${PORT}/api/binance/health"
echo -e "  API 日志     : $LOG_FILE"
echo -e "  停止服务     : ./stop.sh"
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo ""
