"""FastAPI 路由 — 币安实盘交易 API。

接口文档通过 Swagger /docs 自动展示。

接口分组：
- 健康检查：GET /api/binance/health
- 状态查询：GET /api/binance/status
- 信号接入：POST /api/binance/signals/ingest
- 信号日志：GET /api/binance/signals
- 持仓管理：GET /api/binance/positions（实时读取币安当前持仓）
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

import db as _db
from models import (
    AccountSummaryOut,
    CancelPendingEntriesRequest,
    LivePositionOut,
    SignalIngestRequest,
    SignalIngestResult,
    SignalLogOut,
    StatusOut,
    TradFiSignOut,
)

logger = logging.getLogger("router")

router = APIRouter(
    prefix="/api/binance",
    tags=["币安实盘交易"],
)

# ---------------------------------------------------------------------------
# Health（公开）
# ---------------------------------------------------------------------------


@router.get(
    "/health",
    summary="健康检查（无需鉴权）",
    description="服务存活探针，用于 Railway 部署检测和负载均衡健康检查。",
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
    description="返回网络模式、当前持仓数、执行暂停状态等信息。",
)
async def get_status():
    """查询服务运行状态摘要。

    返回字段说明：
    - **testnet**：是否连接币安测试网（BINANCE_TESTNET 环境变量）。
    - **open_positions**：当前正在运行的持仓数量。
    - **execution_paused**：连续鉴权失败等安全原因暂停执行。
    - **api_key_set**：币安 API Key 是否已配置。
    - **db_path**：SQLite 数据库文件路径。
    """
    from trader import is_execution_paused, list_live_positions

    testnet = os.getenv("BINANCE_TESTNET", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )
    paused = is_execution_paused()
    try:
        open_positions_list = list_live_positions()
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error(
            "status positions probe failed: type=%s upstream_status=%s",
            type(exc).__name__,
            status_code,
        )
        detail = "status_positions_failed"
        if status_code:
            detail = f"status_positions_failed_upstream_{status_code}"
        raise HTTPException(status_code=502, detail=detail) from exc
    logger.info(
        "status: testnet=%s open=%d paused=%s",
        testnet, len(open_positions_list), paused,
    )
    return StatusOut(
        testnet=testnet,
        open_positions=len(open_positions_list),
        api_key_set=bool(os.getenv("BINANCE_API_KEY", "").strip()),
        execution_paused=paused,
        db_path=str(_db.DB_PATH),
    )


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
        result_data = process_signal_batch(body.signals, _db)

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
    _db.delete_signals_older_than(keep_hours=24.0)
    rows = _db.list_signals(
        limit=limit,
        offset=offset,
        source=source,
        action=action,
        status=status,
        profile_id=profile_id,
    )
    return rows


@router.get(
    "/signals/lookup",
    summary="按 api_signal_id 查询单条信号",
)
async def lookup_signal(
    source: str = Query(..., description="信号来源，如 orb"),
    api_signal_id: str = Query(..., description="原始信号 ID"),
):
    row = _db.get_signal_by_api_id(source, api_signal_id)
    if not row:
        raise HTTPException(status_code=404, detail="signal_not_found")
    return row


@router.post(
    "/maintenance/reconcile-entries",
    summary="对账 pending STOP/LIMIT 入场单",
)
async def reconcile_entries():
    from trading.entry_reconcile import reconcile_pending_entry_orders

    with _db._db_write_lock:
        promoted = reconcile_pending_entry_orders()
    return {"ok": True, "promoted": int(promoted or 0)}


@router.post(
    "/maintenance/cancel-pending-entries",
    summary="撤销 pending STOP 入场单",
)
async def cancel_pending_entries(body: CancelPendingEntriesRequest):
    from trading.entry_cancel import cancel_pending_entries_by_api_ids

    with _db._db_write_lock:
        cancelled = cancel_pending_entries_by_api_ids(
            body.source,
            body.api_signal_ids,
            body.reason,
        )
    return {"ok": True, "cancelled": int(cancelled or 0)}


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Internal ops（隐藏，不在 Swagger 展示）
# ---------------------------------------------------------------------------


@router.api_route(
    "/_internal/sign-tradfi-perps",
    methods=["GET", "POST"],
    response_model=TradFiSignOut,
    include_in_schema=False,
)
async def sign_tradfi_perps():
    """一键签署 Binance TradFi-Perps 协议（CRCL/PAYP 等股票永续必需）。"""
    from trader import sign_tradfi_perps_agreement

    if not os.getenv("BINANCE_API_KEY", "").strip():
        raise HTTPException(status_code=400, detail="binance_api_key_missing")

    try:
        result = sign_tradfi_perps_agreement()
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error(
            "sign tradfi perps failed: type=%s upstream_status=%s",
            type(exc).__name__,
            status_code,
        )
        detail = "sign_tradfi_perps_failed"
        if status_code:
            detail = f"sign_tradfi_perps_failed_upstream_{status_code}"
        raise HTTPException(status_code=502, detail=detail) from exc

    return TradFiSignOut(ok=True, result=str(result or "SUCCESS"))
