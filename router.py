"""FastAPI 路由 — 币安实盘交易 API。

接口文档通过 Swagger /docs 自动展示。

接口分组：
- 健康检查：GET /api/binance/health
- 状态查询：GET /api/binance/status
- 配置管理：GET/POST /api/binance/config
- 信号接入：POST /api/binance/signals/ingest
- 信号日志：GET /api/binance/signals
- 持仓管理：GET /api/binance/positions（实时读取币安当前持仓）
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

import db as _db
from trader import execute_trade
from models import (
    AccountSummaryOut,
    ClosePositionRequest,
    ConfigUpdate,
    LivePositionOut,
    SignalIngestRequest,
    SignalIngestResult,
    SignalLogOut,
    StatusOut,
    UpdateSlRequest,
)

logger = logging.getLogger("router")

router = APIRouter(
    prefix="/api/binance",
    tags=["币安实盘交易"],
)

_ENV_ONLY_KEYS = {"binance_api_key", "binance_api_secret"}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _model_payload(body) -> dict:
    return body.model_dump() if hasattr(body, "model_dump") else body.dict()


def _safe_log_trade_event(**kwargs) -> None:
    try:
        _db.log_trade_event(**kwargs)
    except Exception as exc:
        logger.warning(
            "trade event log failed: action=%s source=%s type=%s",
            kwargs.get("action"), kwargs.get("source"), type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Health（公开）
# ---------------------------------------------------------------------------


@router.get(
    "/health",
    summary="健康检查（无需鉴权）",
    description="服务存活探针，用于 Railway 部署检测和负载均衡健康检查。不校验 token。",
    include_in_schema=True,
)
async def health():
    """公开的健康检查端点，返回服务状态和版本信息。

    可用于：
    - Railway / Vercel 部署的健康检查路径
    - 负载均衡器的存活探针
    - 前端连接测试
    """
    db_ok = True
    try:
        with _db.get_db() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        db_ok = False
        logger.warning("health check: DB probe failed: %s", exc)

    status = "ok" if db_ok else "degraded"
    return {
        "status": status,
        "module": "next-k-protocol",
        "version": "1.0.0",
        "db": "ok" if db_ok else "fail",
    }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@router.get(
    "/status",
    response_model=StatusOut,
    summary="获取服务状态",
    description="返回交易启用状态、网络模式、当前持仓数、配置概要等信息。",
)
async def get_status():
    """查询服务运行状态和关键配置摘要。

    返回字段说明：
    - **enabled**：交易是否启用，'true' 或 'false'。false 时不会执行任何交易。
    - **testnet**：是否使用币安测试网。true=测试网（testnet.binancefuture.com），false=主网。
    - **open_positions**：当前正在运行的持仓数量。
    - **max_positions**：历史配置项，ingest 不再据此拦截信号。
    - **position_expire_hours**：持仓过期时限（小时），到期自动强平。
    - **api_key_set**：币安 API Key 是否已配置。
    - **db_path**：SQLite 数据库文件路径。
    """
    cfg = _db.get_all_config()
    from trader import list_live_positions

    open_positions_list = list_live_positions()
    logger.info(
        "status: enabled=%s testnet=%s open=%d max=%s",
        cfg.get("enabled", "false"), cfg.get("testnet", "false"),
        len(open_positions_list), cfg.get("max_positions", "8"),
    )
    return StatusOut(
        enabled=cfg.get("enabled", "false"),
        testnet=cfg.get("testnet", "false"),
        open_positions=len(open_positions_list),
        max_positions=cfg.get("max_positions", "8"),
        api_key_set=bool(os.getenv("BINANCE_API_KEY", "").strip()),
        db_path=str(_db.DB_PATH),
        strategy_positions={},
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@router.get(
    "/config",
    summary="读取交易配置",
    description="返回可通过 API 管理的交易配置键值对。币安 API Key/Secret 仅支持环境变量配置，不会出现在返回中。",
)
async def get_config():
    """获取完整的交易配置。

    配置项说明：
    - **enabled**: 交易开关，'true'/'false'
    - **testnet**: 测试网开关，'true'/'false'
    - **max_positions**: 全局最大持仓数
    """
    cfg = _db.get_all_config()
    return {k: v for k, v in cfg.items() if k not in _ENV_ONLY_KEYS}


@router.post(
    "/config",
    summary="更新交易配置",
    description="批量更新一个或多个配置项。币安 API Key/Secret 仅支持通过环境变量或部署平台配置，不可通过接口更新。",
)
async def update_config(body: ConfigUpdate):
    """批量更新配置键值对。

    使用示例：
    ```json
    {
      "pairs": {
        "enabled": "true",
        "entry_type": "MARKET",
        "max_positions": "8"
      }
    }
    ```

    注意：
    - 空字符串的值不会被写入（与缺失相同）
    - 币安 API Key/Secret 仅支持通过 `.env.oi`、系统环境变量或 Railway 配置
    """
    blocked = sorted(k for k in body.pairs if k in _ENV_ONLY_KEYS)
    if blocked:
        raise HTTPException(status_code=400, detail="binance_credentials_env_only")
    logger.info("config update keys=%s", list(body.pairs.keys()))
    _db.set_config_batch(body.pairs)
    return {"ok": True, "updated": list(body.pairs.keys())}


@router.get(
    "/account/summary",
    response_model=AccountSummaryOut,
    summary="读取币安合约账户摘要",
)
async def account_summary():
    from trader import get_account_summary

    try:
        raw = get_account_summary()
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error(
            "account summary failed: type=%s upstream_status=%s",
            type(exc).__name__,
            status_code,
        )
        detail = "account_summary_failed"
        if status_code:
            detail = f"account_summary_failed_upstream_{status_code}"
        raise HTTPException(status_code=502, detail=detail) from exc

    return raw


# ---------------------------------------------------------------------------
# Signal ingest（接收 next-k-api 推送的信号）
# ---------------------------------------------------------------------------


@router.post(
    "/signals/ingest",
    response_model=SignalIngestResult,
    summary="接收并处理交易信号",
    description="由 next-k-api 调用，批量推送交易信号。Protocol 仅记录并转发币安，不做策略侧限制（仅 api_signal_id 去重）。",
)
async def ingest_signals(body: SignalIngestRequest):
    """接收交易信号并转发币安执行。

    处理流程（每条信号）：
    1. **去重**：同一 source+api_signal_id 不重复处理（防 HTTP 重试双开）
    2. **执行**：按 action 调用币安 API（MARKET 开仓/平仓 + SL/TP 条件单）

    SL/TP、仓位数量等均由 next-k-api 在信号中算好；Protocol 不做二次策略判断。

    返回 SignalIngestResult，包含处理汇总和每条信号的详情。

    请求示例：
    ```json
    {
      "signals": [
        {
          "source": "orb",
          "api_signal_id": "orb-coin-20260607-1",
          "symbol": "BTCUSDT",
          "side": "LONG",
          "entry_price": 67250.5,
          "sl_price": 66500.0,
          "margin_usdt": 1000.0,
          "leverage": 5.0
        }
      ]
    }
    ```
    """
    from ingest.pipeline import process_signal_batch

    with _db._db_write_lock:
        result_data = process_signal_batch(body.signals, _db, 0, {}, {})

    return result_data


# ---------------------------------------------------------------------------
# Signal log
# ---------------------------------------------------------------------------


@router.get(
    "/signals",
    response_model=List[SignalLogOut],
    summary="查询信号日志",
    description="返回信号处理日志，按时间倒序。可查看每条信号的接收和处理结果。",
)
async def list_signals(
    limit: int = Query(100, ge=1, le=1000, description="每页条数，最大 1000"),
    offset: int = Query(0, ge=0, description="分页偏移量"),
    source: Optional[str] = Query(None, description="按信号来源过滤"),
    action: Optional[str] = Query(None, description="按信号动作过滤"),
    status: Optional[str] = Query(None, description="按处理状态过滤"),
    profile_id: Optional[int] = Query(None, description="按策略 profile_id 过滤"),
):
    """查询信号处理日志。

    返回字段说明：
    - **source**: 信号来源，如 'orb'
    - **api_signal_id**: 原始信号 ID（可用于去重追溯）
    - **status**: 处理状态
      - 'traded'：已成功执行
      - 'duplicate'：重复 api_signal_id
      - 'error'：处理失败（skip_reason 中有详细错误信息）
    - **skip_reason**: 跳过或失败的具体原因
    """
    rows = _db.list_signals(
        limit=limit,
        offset=offset,
        source=source,
        action=action,
        status=status,
        profile_id=profile_id,
    )
    return rows


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


@router.post(
    "/positions/close",
    summary="平仓（由 next-k-api trail 退出触发）",
    description="取消 SL/TP 条件单，MARKET 平仓，记录 PnL。由 next-k-api 纸面 trail 检查触发退出后调用。",
)
async def close_position(body: ClosePositionRequest):
    raise HTTPException(status_code=410, detail="position_lifecycle_removed")


@router.put(
    "/positions/{position_id}/sl",
    summary="动态修改止损价（Moss Quant 移动止损）",
    description="取消当前 SL 条件单，以新价格重新下 STOP_MARKET 条件单并更新持仓记录。",
)
async def update_position_sl(position_id: int, body: UpdateSlRequest):
    raise HTTPException(status_code=410, detail="position_lifecycle_removed")


@router.get(
    "/positions",
    response_model=List[LivePositionOut],
    summary="查询持仓列表",
    description="直接返回币安当前非零持仓列表。",
)
async def list_positions(
    status: Optional[str] = Query(
        None,
        description="仅支持 open；其他历史状态已下线",
    ),
    limit: int = Query(100, ge=1, le=1000, description="每页条数"),
    offset: int = Query(0, ge=0, description="分页偏移量"),
):
    """查询当前实时持仓；历史持仓和本地 PnL 汇总已下线。"""
    if status and status != "open":
        raise HTTPException(status_code=410, detail="historical_positions_removed")

    from trader import list_live_positions

    try:
        rows = list_live_positions()
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error(
            "list positions failed: type=%s upstream_status=%s",
            type(exc).__name__,
            status_code,
        )
        detail = "positions_failed"
        if status_code:
            detail = f"positions_failed_upstream_{status_code}"
        raise HTTPException(status_code=502, detail=detail) from exc
    return rows[offset : offset + limit]
