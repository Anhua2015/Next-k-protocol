"""FastAPI 路由 — 币安实盘交易 API。

所有接口（除 /api/binance/health）都需要鉴权（X-Maintenance-Token 或 Bearer token）。
接口文档通过 Swagger /docs 自动展示。

接口分组：
- 健康检查：GET /api/binance/health（公开）
- 状态查询：GET /api/binance/status
- 配置管理：GET/POST /api/binance/config
- 信号接入：POST /api/binance/signals/ingest
- 信号日志：GET /api/binance/signals
- 持仓管理：GET /api/binance/positions、GET /api/binance/positions/{id}
- PnL 统计：GET /api/binance/pnl/summary
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

import db as _db
from auth import require_auth
from trader import close_position as do_close
from trader import execute_trade
from models import (
    AccountSummaryOut,
    ClosePositionRequest,
    ConfigUpdate,
    PnlSummaryOut,
    PositionOut,
    SignalIngestRequest,
    SignalIngestResult,
    SignalLogOut,
    StatusOut,
    UpdateSlRequest,
)

_VALID_SOURCES = ("zct_vwap", "momentum", "jiezhen", "moss_quant")

logger = logging.getLogger("router")

router = APIRouter(
    prefix="/api/binance",
    tags=["币安实盘交易"],
)

_SENSITIVE_KEYS = {"binance_api_key", "binance_api_secret"}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    description="返回交易启用状态、网络模式、当前持仓数、配置概要等信息。需要鉴权。",
    dependencies=[Depends(require_auth)],
)
async def get_status():
    """查询服务运行状态和关键配置摘要。

    返回字段说明：
    - **enabled**：交易是否启用，'true' 或 'false'。false 时不会执行任何交易。
    - **testnet**：是否使用币安测试网。true=测试网（testnet.binancefuture.com），false=主网。
    - **open_positions**：当前正在运行的持仓数量。
    - **max_positions**：全局最大同时持仓数，达到上限后新信号会被跳过。
    - **position_expire_hours**：持仓过期时限（小时），到期自动强平。
    - **api_key_set**：币安 API Key 是否已配置。
    - **db_path**：SQLite 数据库文件路径。
    """
    cfg = _db.get_all_config()
    open_positions_list = _db.get_open_positions()
    strategy_positions: dict = {}
    for src in _VALID_SOURCES:
        strategy_positions[src] = _db.count_open_by_source(src)
    logger.info(
        "status: enabled=%s testnet=%s open=%d max=%s strategies=%s",
        cfg.get("enabled", "false"), cfg.get("testnet", "false"),
        len(open_positions_list), cfg.get("max_positions", "8"), strategy_positions,
    )
    return StatusOut(
        enabled=cfg.get("enabled", "false"),
        testnet=cfg.get("testnet", "false"),
        open_positions=len(open_positions_list),
        max_positions=cfg.get("max_positions", "8"),
        api_key_set=bool(cfg.get("binance_api_key", "").strip()),
        db_path=str(_db.DB_PATH),
        strategy_positions=strategy_positions,
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@router.get(
    "/config",
    summary="读取交易配置",
    description="返回所有配置键值对。敏感字段（binance_api_key/binance_api_secret）的值会脱敏为 '****'。需要鉴权。",
    dependencies=[Depends(require_auth)],
)
async def get_config():
    """获取完整的交易配置。

    配置项说明：
    - **enabled**: 交易开关，'true'/'false'
    - **testnet**: 测试网开关，'true'/'false'
    - **binance_api_key**: 币安 API Key（脱敏显示）
    - **binance_api_secret**: 币安 API Secret（脱敏显示）
    - **margin_usdt**: 单笔保证金（USDT），实际名义敞口 = 保证金 × 杠杆
    - **leverage**: 杠杆倍数
    - **max_positions**: 全局最大持仓数
    - **max_positions_play01/02/03**: 各策略最大持仓数
    - **position_expire_hours**: 全局持仓过期时限（小时）
    - **expire_hours_play01/02/03**: 各策略过期时限（小时）
    - **enabled_sources**: 启用的信号来源，逗号分隔
    """
    cfg = _db.get_all_config()
    masked = {k: ("****" if k in _SENSITIVE_KEYS and v else v) for k, v in cfg.items()}
    return masked


@router.post(
    "/config",
    summary="更新交易配置",
    description="批量更新一个或多个配置项。敏感字段（binance_api_key/binance_api_secret）的值会在日志和返回中脱敏。需要鉴权。",
    dependencies=[Depends(require_auth)],
)
async def update_config(body: ConfigUpdate):
    """批量更新配置键值对。

    使用示例：
    ```json
    {
      "pairs": {
        "enabled": "true",
        "margin_usdt": "200",
        "leverage": "10"
      }
    }
    ```

    注意：
    - 空字符串的值不会被写入（与缺失相同）
    - API Key/Secret 写入后立即生效，无需重启
    - 敏感字段的值会在日志中显示为 '****'
    """
    sanitized = {k: ("****" if k in _SENSITIVE_KEYS else v) for k, v in body.pairs.items()}
    logger.info("config update keys=%s", list(sanitized.keys()))
    _db.set_config_batch(body.pairs)
    return {"ok": True, "updated": list(body.pairs.keys())}


@router.get(
    "/account/summary",
    response_model=AccountSummaryOut,
    summary="读取币安合约账户摘要",
    dependencies=[Depends(require_auth)],
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

    def _int_cfg(key: str, default: str) -> int:
        try:
            return int(_db.get_config(key, default))
        except ValueError:
            return int(default)

    return {
        **raw,
        "moss_quant": {
            "enabled": _db.get_config("src_moss_quant_enabled", "false").lower() == "true",
            "leverage": _int_cfg("src_moss_quant_leverage", "10"),
            "max_positions": _int_cfg("src_moss_quant_max_positions", "10"),
            "entry_type": _db.get_config("src_moss_quant_entry_type", "MARKET"),
        },
    }


# ---------------------------------------------------------------------------
# Signal ingest（接收 next-k-api 推送的信号）
# ---------------------------------------------------------------------------


@router.post(
    "/signals/ingest",
    response_model=SignalIngestResult,
    summary="接收并处理交易信号",
    description="由 next-k-api 在 ZCT VWAP 扫描完成后调用，批量推送新信号。服务根据去重、持仓冲突、仓位上限等条件决定是否开仓。需要鉴权。",
    dependencies=[Depends(require_auth)],
)
async def ingest_signals(body: SignalIngestRequest):
    """接收交易信号并自动判断是否开仓。支持 ZCT VWAP / 动量 / 接针三种策略。

    处理流程（每条信号）：
    1. **来源验证**：source 必须在 zct_vwap/momentum/jiezhen 中
    2. **策略开关**：对应 src_{source}_enabled 必须为 true
    3. **去重检查**：同一 source+api_signal_id 的信号不会重复处理
    4. **交易开关**：enabled=false 时所有信号跳过
    5. **持仓冲突**：同一 symbol 已有开仓时跳过（一币一仓）
    6. **仓位上限**：ZCT 按 play 检查，动量/接针按 source 检查，最后全局上限
    7. **执行开仓**：调用币安 API 下 MARKET 市价单 → SL/TP 条件单
       SL/TP 均由 next-k-api 在信号中计算好，Protocol 不做二次计算

    返回 SignalIngestResult，包含处理汇总和每条信号的详情。

    请求示例：
    ```json
    {
      "signals": [
        {
          "source": "zct_vwap",
          "api_signal_id": "12345",
          "symbol": "BTCUSDT",
          "side": "LONG",
          "entry_price": 67250.5,
          "sl_price": 66500.0,
          "tp_price": 68500.0,
          "confidence": "high",
          "regime": "TREND_UP",
          "play": "PLAY01"
        },
        {
          "source": "momentum",
          "api_signal_id": "67890",
          "symbol": "ETHUSDT",
          "side": "SHORT",
          "entry_price": 3200.0,
          "sl_price": 3264.0,
          "tp_price": 3072.0
        }
      ]
    }
    ```
    """
    # Phase 5: delegate to ingest/ pipeline.
    # Build ctx config once, then process batch.
    try:
        max_pos = int(_db.get_config("max_positions", "8"))
    except ValueError:
        max_pos = 8

    play_max: dict = {}
    for pn in ("play01", "play02", "play03"):
        try:
            play_max[pn] = int(_db.get_config(f"max_positions_{pn}", "5"))
        except ValueError:
            play_max[pn] = 5

    source_max: dict = {}
    for src in ("momentum", "jiezhen", "moss_quant"):
        try:
            source_max[src] = int(_db.get_source_config(src, "max_positions", "5"))
        except ValueError:
            source_max[src] = 5

    import os as _os
    from ingest.pipeline import process_signal_batch

    lockless = _os.getenv("INGEST_LOCKLESS_EXECUTE", "false").lower() in (
        "1", "true", "yes", "on",
    )
    # NOTE: lockless mode (future): guards inside lock, execute outside lock.
    # Currently both paths are identical — lockless refactoring requires
    # intent-status infrastructure (Phase 5 spec §9.7).
    if lockless:
        logger.warning("INGEST_LOCKLESS_EXECUTE=true is not yet implemented; using locked path")

    with _db._db_write_lock:
        result_data = process_signal_batch(
            body.signals, _db, max_pos, play_max, source_max,
        )

    return result_data


# ---------------------------------------------------------------------------
# Signal log
# ---------------------------------------------------------------------------


@router.get(
    "/signals",
    response_model=List[SignalLogOut],
    summary="查询信号日志",
    description="返回信号处理日志，按时间倒序。可查看每条信号的接收和处理结果。需要鉴权。",
    dependencies=[Depends(require_auth)],
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
    - **source**: 信号来源，如 'zct_vwap'
    - **api_signal_id**: 原始信号 ID（可用于去重追溯）
    - **status**: 处理状态
      - 'traded'：已成功开仓
      - 'received'：已接收但尚未处理
      - 'skipped_disabled'：交易已禁用
      - 'skipped_position_exists'：同币种已有持仓
      - 'skipped_max_positions'：已达仓位上限
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
    description="取消 SL/TP 条件单，MARKET 平仓，记录 PnL。由 next-k-api 纸面 trail 检查触发退出后调用。需要鉴权。",
    dependencies=[Depends(require_auth)],
)
async def close_position(body: ClosePositionRequest):
    """接收平仓指令并执行。

    处理流程：
    1. 根据 symbol 找到 open 持仓
    2. 取消该 symbol 的所有条件单（SL/TP）
    3. MARKET 平仓
    4. 计算并记录 PnL

    请求示例：
    ```json
    {
      "source": "momentum",
      "api_signal_id": "67890",
      "symbol": "ETHUSDT",
      "side": "SHORT",
      "exit_rule": "trail_tier1",
      "close_price": 3150.0
    }
    ```
    """
    logger.info(
        "close_position request: source=%s symbol=%s side=%s exit_rule=%s close_price=%s signal_id=%s",
        body.source, body.symbol, body.side, body.exit_rule, body.close_price, body.api_signal_id,
    )

    try:
        ok = do_close(
            source=body.source,
            symbol=body.symbol,
            side=body.side,
            exit_rule=body.exit_rule,
            close_price=body.close_price,
            position_id=body.position_id,
        )
        if ok:
            return {"ok": True, "symbol": body.symbol, "action": "closed"}
        else:
            raise HTTPException(status_code=404, detail="no open position for symbol")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("close_position failed %s %s: %s", body.side, body.symbol, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.put(
    "/positions/{position_id}/sl",
    summary="动态修改止损价（Moss Quant 移动止损）",
    description="取消当前 SL 条件单，以新价格重新下 STOP_MARKET 条件单并更新持仓记录。需要鉴权。",
    dependencies=[Depends(require_auth)],
)
async def update_position_sl(position_id: int, body: UpdateSlRequest):
    """动态修改持仓的止损价格。

    处理流程：
    1. 根据 position_id 找到持仓
    2. 校验持仓状态为 open 且存在 sl_order_id
    3. 取消旧 SL 条件单
    4. 以新 sl_price 重新下 STOP_MARKET 条件单
    5. 更新 positions 表 sl_price

    请求示例：
    ```json
    {
      "new_sl_price": 91200.0
    }
    ```
    """
    from binance.exchange_info import round_price
    from trader import cancel_algo_order, place_algo_order, get_symbol_info

    pos = _db.get_position_by_id(position_id)
    if not pos:
        raise HTTPException(status_code=404, detail="position not found")
    if pos["status"] != "open":
        raise HTTPException(status_code=400, detail="position not open")

    symbol = pos["symbol"]
    side = pos["side"]
    old_sl_id = pos.get("sl_order_id")
    qty = pos.get("quantity")
    if not qty:
        raise HTTPException(status_code=400, detail="position has no quantity")
    position_side = side if _db.get_config("hedge_mode", "").lower() == "true" else None

    close_side = "SELL" if side == "LONG" else "BUY"
    new_sl = body.new_sl_price
    if new_sl <= 0:
        raise HTTPException(status_code=400, detail="invalid sl_price")

    try:
        tick_size, _, _ = get_symbol_info(symbol)
        new_sl = round_price(new_sl, tick_size)
    except Exception:
        pass

    # 取消旧 SL
    if old_sl_id:
        try:
            cancel_algo_order(old_sl_id)
            logger.info("update_sl: cancelled old SL %s for pos=%s", old_sl_id, position_id)
        except Exception as exc:
            logger.error("update_sl: cancel old SL %s failed: %s", old_sl_id, exc)
            raise HTTPException(status_code=502, detail=f"cancel old SL failed: {exc}")

    # 下新 STOP_MARKET 条件单
    try:
        prot_params = {
            "symbol": symbol, "side": close_side, "type": "STOP_MARKET",
            "stopPrice": new_sl, "quantity": qty, "newOrderRespType": "RESULT",
            "reduceOnly": "true", "workingType": "MARK_PRICE",
        }
        if position_side:
            prot_params["positionSide"] = position_side
        new_sl_resp = place_algo_order(prot_params)
        new_sl_id = str(new_sl_resp.get("algoId", "") or new_sl_resp.get("orderId", ""))
    except Exception as exc:
        logger.error("update_sl: place new SL failed pos=%s: %s", position_id, exc)
        raise HTTPException(status_code=500, detail=f"place new SL failed: {exc}")

    # 更新数据库（下单成功后才写，保证 DB 与交易所一致）
    try:
        from repos.connection import get_db
        with get_db(write=True) as conn:
            conn.execute(
                "UPDATE positions SET sl_price=?, sl_order_id=? WHERE id=?",
                (new_sl, new_sl_id, position_id),
            )
    except Exception as exc:
        logger.error("update_sl: db update failed pos=%s: %s", position_id, exc)
        raise HTTPException(status_code=500, detail="DB update failed after SL placed")

    logger.info("update_sl: pos=%s symbol=%s old_sl_id=%s new_sl=%.6f new_sl_id=%s",
                position_id, symbol, old_sl_id, new_sl, new_sl_id)
    return {
        "ok": True,
        "position_id": position_id,
        "symbol": symbol,
        "old_sl_order_id": old_sl_id,
        "new_sl_order_id": new_sl_id,
        "new_sl_price": new_sl,
    }


@router.get(
    "/positions",
    response_model=List[PositionOut],
    summary="查询持仓列表",
    description="返回持仓记录，支持按状态过滤。需要鉴权。",
    dependencies=[Depends(require_auth)],
)
async def list_positions(
    status: Optional[str] = Query(
        None,
        description="持仓状态过滤：'open'（当前持仓）| 'closed'（已平仓）| 不传（全部）",
    ),
    limit: int = Query(100, ge=1, le=1000, description="每页条数"),
    offset: int = Query(0, ge=0, description="分页偏移量"),
    source: Optional[str] = Query(None, description="按信号来源过滤"),
    profile_id: Optional[int] = Query(None, description="按策略 profile_id 过滤"),
):
    """查询持仓列表，包含完整 P&L 明细。

    返回字段说明：
    - **entry_price**：入场成交均价（USDT）
    - **sl_price / tp_price**：止损/止盈触发价格
    - **quantity**：持仓数量（合约张数）
    - **pnl_usdt**：已实现盈亏（USDT），正数为盈利
    - **pnl_pct**：杠杆收益率百分比 = (收益率 × 杠杆 × 100)%
    - **close_reason**：平仓原因
      - 'tp'：止盈触发
      - 'sl'：止损触发
      - 'expired'：持仓到期自动强平
      - 'manual'：手动平仓
      - 'unknown'：未知原因
    - **expire_at**：持仓过期时间（UTC），到期后由 scheduler 自动强平
    """
    if status and status not in ("open", "closed", "pending_entry", "cancelled_pending"):
        raise HTTPException(status_code=400, detail="status must be 'open' | 'closed' | 'pending_entry' | 'cancelled_pending'")
    rows = _db.list_positions(
        status=status,
        limit=limit,
        offset=offset,
        source=source,
        profile_id=profile_id,
    )
    return rows


@router.get(
    "/positions/{position_id}",
    response_model=PositionOut,
    summary="查询单条持仓详情",
    description="根据持仓 ID 获取完整的持仓记录和 P&L 明细。需要鉴权。",
    dependencies=[Depends(require_auth)],
)
async def get_position(
    position_id: int = Path(..., description="持仓主键 ID"),
):
    """获取单条持仓的完整信息。

    包含入场/平仓价格、SL/TP 价格、PnL 计算详情等。
    如果持仓不存在返回 404。
    """
    pos = _db.get_position_by_id(position_id)
    if pos is None:
        raise HTTPException(status_code=404, detail="position_not_found")
    return pos


# ---------------------------------------------------------------------------
# PnL summary
# ---------------------------------------------------------------------------


@router.get(
    "/pnl/summary",
    response_model=PnlSummaryOut,
    summary="PnL 盈亏汇总",
    description="返回累计交易统计：总笔数、胜率、累计盈亏、平均盈亏、近 30 日日 P&L。需要鉴权。",
    dependencies=[Depends(require_auth)],
)
async def pnl_summary():
    """获取完整的盈亏统计汇总。

    P&L 计算公式：
    - **LONG**: pnl_usdt = qty × (close_price - entry_price)
    - **SHORT**: pnl_usdt = qty × (entry_price - close_price)
    - **pnl_pct** = (收益率 × 杠杆 × 100)%

    统计说明：
    - **total**：已平仓交易总笔数
    - **wins**：盈利笔数（pnl_usdt > 0）
    - **losses**：亏损笔数（pnl_usdt <= 0）
    - **total_pnl**：累计总盈亏（USDT）
    - **avg_pnl**：平均单笔盈亏（USDT）
    - **daily**：近 30 天每日 PnL 明细（按 closed_at 日期分组，UTC）
    """
    return _db.pnl_summary()
