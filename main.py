"""Next K Protocol — 币安实盘交易 API 服务。

独立于 next-k-api，通过 HTTP 接口接收 ORB 信号并执行币安合约交易。
支持 Railway 一键部署，Swagger /docs 交互文档。

启动方式：
    uvicorn main:app --host 0.0.0.0 --port 8001
    ./start.sh

环境变量（.env.oi 或系统环境变量）：
    BINANCE_API_KEY             币安 API Key
    BINANCE_API_SECRET          币安 API Secret
    BINANCE_TESTNET             测试网开关（启动时写入 DB，覆盖已有值）
    BINANCE_ENABLED             全局交易开关 true/false（启动时写入 DB，覆盖已有值）
    DATA_DIR                    数据目录（默认当前目录）
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from env_loader import load_env_oi

from observability.logging_setup import configure_logging
configure_logging()
logger = logging.getLogger(__name__)

load_env_oi()

PORT = int(os.environ.get("PORT", 8001))
# CORS 白名单：通过 PROTOCOL_CORS_ORIGINS 环境变量配置（逗号分隔）。
# 默认仅允许本地（开发）；生产环境必须配置实际前端域名，例如：
#   PROTOCOL_CORS_ORIGINS=https://app.example.com,https://staging.example.com
def _parse_cors_origins() -> list[str]:
    raw = os.getenv("PROTOCOL_CORS_ORIGINS", "").strip()
    if not raw:
        return [
            "http://localhost",
            "http://localhost:8000",
            "http://localhost:8001",
            "http://127.0.0.1",
            "http://127.0.0.1:8000",
            "http://127.0.0.1:8001",
            "http://localhost:5173",
            "http://localhost:5500",
        ]
    return [o.strip() for o in raw.split(",") if o.strip()]


CORS_ORIGINS = _parse_cors_origins()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Next K Protocol...")

    import db
    db.init_db()
    db.apply_env_config_overrides()
    logger.info("Database initialized: %s", str(db.DB_PATH))

    # Initialize Binance HTTP client (Phase 1)
    from binance.client import init_client
    from db import get_config
    init_client(
        base_url_fn=lambda: (
            "https://testnet.binancefuture.com"
            if get_config("testnet", "false").lower() == "true"
            else "https://fapi.binance.com"
        ),
        api_key_fn=lambda: os.getenv("BINANCE_API_KEY", "").strip(),
        secret_fn=lambda: os.getenv("BINANCE_API_SECRET", "").strip(),
    )
    logger.info("Binance HTTP client initialized")

    yield
    logger.info("Next K Protocol shutting down")


app = FastAPI(
    title="Next K Protocol",
    description="币安合约实盘交易 API 服务。接收交易请求并执行开仓，当前持仓直接读取币安实时数据。",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    # 临时放开到 * 以便线上前端能跨域访问；建议尽快配 PROTOCOL_CORS_ORIGINS
    # 环境变量后改回白名单（参考 _parse_cors_origins 注释）。
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

from router import router
app.include_router(router)

from routers.metrics import router as metrics_router
app.include_router(metrics_router)

logger.info("Routes registered: /api/binance/*")
logger.info("Swagger docs: http://0.0.0.0:%d/docs", PORT)
logger.info("Health check: http://0.0.0.0:%d/api/binance/health", PORT)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
