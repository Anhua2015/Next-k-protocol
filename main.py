"""Next K Protocol — 币安实盘交易 API 服务。

独立于 next-k-api，通过 HTTP 接口接收 ZCT 信号并执行币安合约交易。
支持 Railway 一键部署，Swagger /docs 交互文档。

启动方式：
    uvicorn main:app --host 0.0.0.0 --port 8001
    ./start.sh

环境变量（.env.oi 或系统环境变量）：
    PROTOCOL_MAINTENANCE_TOKEN  鉴权 token
    BINANCE_API_KEY             币安 API Key
    BINANCE_API_SECRET          币安 API Secret
    BINANCE_TESTNET             测试网开关
    DATA_DIR                    数据目录（默认当前目录）
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from env_loader import load_env_oi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

load_env_oi()

PORT = int(os.environ.get("PORT", 8001))
EMBED_SCHEDULER = os.getenv("EMBED_SCHEDULER", "1").strip().lower() in (
    "1", "true", "yes", "on",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Next K Protocol...")

    import db
    db.init_db()
    logger.info("Database initialized: %s", str(db.DB_PATH))

    if EMBED_SCHEDULER:
        import pytz
        tz = pytz.timezone("Asia/Shanghai")
        sch = BackgroundScheduler(timezone=tz)
        from scheduler import register_jobs
        register_jobs(sch)
        sch.start()
        app.state.scheduler = sch
        logger.info("Embedded scheduler started (Asia/Shanghai)")
    else:
        logger.info("Embedded scheduler disabled (EMBED_SCHEDULER != 1)")

    yield

    sch = getattr(app.state, "scheduler", None)
    if sch is not None:
        sch.shutdown(wait=False)
        app.state.scheduler = None
    logger.info("Next K Protocol shutting down")


app = FastAPI(
    title="Next K Protocol",
    description="币安合约实盘交易 API 服务。接收 ZCT VWAP 信号，自动执行开仓/止损/止盈，管理持仓生命周期。",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

from router import router
app.include_router(router)

logger.info("Routes registered: /api/binance/*")
logger.info("Swagger docs: http://0.0.0.0:%d/docs", PORT)
logger.info("Health check: http://0.0.0.0:%d/api/binance/health", PORT)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
