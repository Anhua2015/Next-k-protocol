"""Next K Protocol — 币安实盘交易执行服务。

独立于 next-k-api，通过 HTTP 接口接收 ORB 信号并执行币安合约交易。
支持 Railway 一键部署，Swagger /docs 交互文档。

启动方式：
    uvicorn main:app --host 0.0.0.0 --port 8001
    ./start.sh

环境变量（.env.oi 或系统环境变量）：
    BINANCE_API_KEY             币安 API Key
    BINANCE_API_SECRET          币安 API Secret
    BINANCE_TESTNET             是否连接测试网（true/false，仅选网络，不控制是否下单）
    DATA_DIR                    数据目录（默认当前目录）

架构边界：

- next-k-api 决定是否交易、方向、保证金、杠杆、SL 和 TP；
- Protocol 负责幂等去重、交易所精度适配、安全下单和执行审计；
- Protocol 不重新判断策略质量，避免纸面层与实盘层出现两套决策。
"""

from __future__ import annotations

import logging
import os
import asyncio
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
# 默认保持兼容开放；生产环境应配置实际前端域名，例如：
#   PROTOCOL_CORS_ORIGINS=https://app.example.com,https://staging.example.com
def _parse_cors_origins() -> list[str]:
    raw = os.getenv("PROTOCOL_CORS_ORIGINS", "").strip()
    if not raw:
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


CORS_ORIGINS = _parse_cors_origins()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """初始化数据库与 Binance HTTP 单例，并在进程退出时释放连接池。"""
    logger.info("Starting Next K Protocol...")
    pnl_task = None

    import db
    db.init_db()
    logger.info("Database initialized: %s", str(db.DB_PATH))

    def _binance_testnet() -> bool:
        return os.getenv("BINANCE_TESTNET", "false").strip().lower() in (
            "1", "true", "yes", "on",
        )

    # 客户端通过闭包动态读取环境变量，密钥不会写入数据库或全局配置表。
    from binance.client import init_client
    init_client(
        base_url_fn=lambda: (
            "https://testnet.binancefuture.com"
            if _binance_testnet()
            else "https://fapi.binance.com"
        ),
        api_key_fn=lambda: os.getenv("BINANCE_API_KEY", "").strip(),
        secret_fn=lambda: os.getenv("BINANCE_API_SECRET", "").strip(),
    )
    logger.info("Binance HTTP client initialized")

    from pnl_auto_sync import pnl_auto_sync_loop
    pnl_task = asyncio.create_task(pnl_auto_sync_loop(), name="pnl-auto-sync")

    try:
        yield
    finally:
        if pnl_task is not None and not pnl_task.done():
            pnl_task.cancel()
            try:
                await pnl_task
            except asyncio.CancelledError:
                logger.info("PnL auto sync task cancelled")
    from binance.client import client as binance_client
    if binance_client is not None:
        binance_client.close()
        logger.info("Binance HTTP client closed")
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
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Maintenance-Token"],
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
