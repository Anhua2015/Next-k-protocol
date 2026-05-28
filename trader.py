"""Binance Futures 执行层。

Phase 1-3 重构：
- HTTP/签名/交易所信息 → binance/
- 订单/条件单/protective → binance/ + trading/
- 生命周期(同步/对账/过期/平仓) → lifecycle/

trader.py 保留：execute_trade + facade 重新导出 + auth fail 状态。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

import httpx

from db import (
    cancel_pending_position,
    compute_expire_at,
    compute_pending_deadline,
    get_config,
    get_open_expired_positions,
    get_open_position_for_symbol,
    get_open_positions,
    get_pending_entries,
    get_source_config,
    insert_pending_position,
    insert_position,
    promote_pending_to_open,
    resolve_expire_hours,
    set_config,
    update_position_closed,
    update_signal_status,
)

# -- binance/ facade (constants + lazy client ref) ---------------------------
from binance.client import (
    BACKOFF_BASE_SEC, LIVE_BASE, MAX_RETRIES,
    RETRY_CODES, RETRY_STATUSES, TEST_BASE,
)
from binance.exchange_info import (
    EXCHANGE_INFO_TTL_SEC,
    get_filters as _filters_fn,
    get_mark_price as _mark_px_fn,
    get_symbol_info as _sym_info_fn,
    round_price as _round_price,
    round_quantity as _round_quantity,
)
from binance.account import (
    detect_hedge_mode as _hedge_fn,
    get_live_position as _live_pos_fn,
    get_order as _get_order_fn,
    set_leverage as _set_lev_fn,
    set_margin_type as _set_margin_fn,
)
from binance.time_sync import (
    RECV_WINDOW_MS, SERVER_TIME_RESYNC_SEC,
    now_utc as _now_utc, server_timestamp_ms,
)
from binance.orders import (
    cancel_algo_order as _cancel_algo,
    cancel_all_orders as _cancel_all,
    cancel_order_by_id as _cancel_oid,
    get_algo_order as _get_algo,
    get_open_algo_orders as _get_open_algos,
    place_algo_order as _place_algo,
    place_order as _place_order,
)
from trading.protective import (
    emergency_close as _emergency_close_fn,
    validate_sl_distance as _validate_sl_distance_fn,
)

logger = logging.getLogger("trader")

# Lazy client — safe for test reloads.
_client: Any = None


def _resolve_client():
    global _client
    if _client is not None:
        return _client
    from binance.client import client
    _client = client
    if _client is None:
        raise RuntimeError("Binance client not initialized")
    return _client


# -- Auth fail state (trader-owned) ------------------------------------------
_SYNC_AUTH_FAIL_COUNT = 0
_SYNC_AUTH_FAIL_THRESHOLD = 20
_SYNC_AUTH_FAIL_LOCK = threading.Lock()


def _reset_auth_fail_count() -> None:
    global _SYNC_AUTH_FAIL_COUNT
    with _SYNC_AUTH_FAIL_LOCK:
        _SYNC_AUTH_FAIL_COUNT = 0


def _handle_auth_fail(context: str, pos_id: Any) -> None:
    global _SYNC_AUTH_FAIL_COUNT
    with _SYNC_AUTH_FAIL_LOCK:
        _SYNC_AUTH_FAIL_COUNT += 1
        count = _SYNC_AUTH_FAIL_COUNT
    from observability.metrics import AUTH_FAIL
    AUTH_FAIL.inc()
    if count >= _SYNC_AUTH_FAIL_THRESHOLD:
        set_config("enabled", "false")
        from observability.metrics import TRADING_DISABLED_AUTO
        TRADING_DISABLED_AUTO.inc()
        logger.critical(
            "Binance auth failed %d times; DISABLED trading (%s pos=%s)",
            count, context, pos_id,
        )
    else:
        logger.warning("%s pos=%s auth-fail %d/%d",
                       context, pos_id, count, _SYNC_AUTH_FAIL_THRESHOLD)


# -- Facade wrappers (auto-inject client) ------------------------------------

def _get_filters(s):            return _filters_fn(_resolve_client(), s)
def get_mark_price(s):          return _mark_px_fn(_resolve_client(), s)
def get_symbol_info(s):         return _sym_info_fn(_resolve_client(), s)
def _detect_hedge_mode():       return _hedge_fn(_resolve_client())
def get_live_position(s):       return _live_pos_fn(_resolve_client(), s)
def get_order(s, oid):          return _get_order_fn(_resolve_client(), s, oid)
def set_leverage(s, lev):       return _set_lev_fn(_resolve_client(), s, lev)
def set_margin_type(s):         return _set_margin_fn(_resolve_client(), s)

def place_order(p):             return _place_order(_resolve_client(), p)
def place_algo_order(p):        return _place_algo(_resolve_client(), p)
def get_algo_order(aid):        return _get_algo(_resolve_client(), aid)
def cancel_algo_order(aid):     return _cancel_algo(_resolve_client(), aid)
def cancel_order_by_id(s, o):   return _cancel_oid(_resolve_client(), s, o)
def get_open_algo_orders(s):    return _get_open_algos(_resolve_client(), s)
def cancel_all_orders(s, p=None): return _cancel_all(_resolve_client(), s, p)

def _emergency_close(sym, side, qty, ps):
    return _emergency_close_fn(_resolve_client(), sym, side, qty, ps)

def _validate_sl_distance(side, sl, mark, tick):
    return _validate_sl_distance_fn(side, sl, mark, tick)

def _build_protective(s, cs, sp, q, ps, k):
    from trading.protective import build_protective_params
    return build_protective_params(s, cs, sp, q, ps, k)

def _place_protective(s, cs, sp, q, ps, ts, k):
    return place_algo_order(_build_protective(s, cs, sp, q, ps, k))


# -- execute_trade (Phase 4: orchestrator, delegates to trading/) ------------

def execute_trade(signal: Dict[str, Any]) -> bool:
    """开仓调度：读配置→设杠杆/保证金→dispatch MARKET/LIMIT。"""
    from trading.market_entry import open_market
    from trading.limit_entry import open_limit

    signal_log_id = signal["signal_log_id"]
    symbol = signal["symbol"]
    side = signal["side"]
    source = signal.get("source", "") or ""
    play = signal.get("play", "") or ""

    logger.info("execute_trade: source=%s symbol=%s side=%s play=%s id=%s",
                source, symbol, side, play, signal_log_id)

    # Resolve margin/leverage
    if source in ("momentum", "jiezhen"):
        margin = float(get_source_config(source, "margin_usdt", "100"))
        leverage = int(get_source_config(source, "leverage", "10"))
    elif source == "moss_quant":
        notional = float(signal.get("notional_usdt", 0) or 0)
        leverage = int(get_source_config(source, "leverage", "10"))
        if notional <= 0:
            logger.error("moss_quant signal missing notional_usdt %s", symbol)
            update_signal_status(signal_log_id, "error", "missing notional_usdt")
            return False
        margin = notional / leverage
    else:
        try:
            margin = float(get_config("margin_usdt", "100"))
            leverage = int(get_config("leverage", "10"))
        except (TypeError, ValueError) as exc:
            logger.error("config parse failed %s: %s", symbol, exc)
            update_signal_status(signal_log_id, "error", f"bad config: {exc}")
            return False

    logger.info("execute_trade %s: source=%s margin=%.0f leverage=%d",
                symbol, source, margin, leverage)

    # Validate signal
    try:
        float(signal.get("sl_price")) if signal.get("sl_price") is not None else None
        float(signal.get("tp_price")) if signal.get("tp_price") is not None else None
    except (TypeError, ValueError) as exc:
        logger.error("signal SL/TP parse failed %s: %s", symbol, exc)
        update_signal_status(signal_log_id, "error", f"bad signal SL/TP: {exc}")
        return False

    if margin <= 0 or leverage <= 0:
        update_signal_status(signal_log_id, "error",
                             f"invalid margin={margin} leverage={leverage}")
        return False

    if get_config("enabled", "false").lower() != "true":
        update_signal_status(signal_log_id, "skipped_disabled", "trading disabled")
        return False

    entry_type = get_source_config(
        source, "entry_type", get_config("entry_type", "MARKET"),
    ).upper()

    # Setup: filters + leverage + margin type + hedge mode
    try:
        step_size, tick_size, min_notional = _get_filters(symbol)
        set_margin_type(symbol)
        set_leverage(symbol, leverage)
        hedge = _detect_hedge_mode()
        mark_px = get_mark_price(symbol)
    except Exception as exc:
        logger.error("setup failed %s %s: %s", side, symbol, exc)
        update_signal_status(signal_log_id, "error", f"setup: {exc}")
        return False

    # Dispatch
    if entry_type == "LIMIT":
        result = open_limit(
            signal, symbol, side, margin, leverage,
            step_size, tick_size, min_notional, hedge, source, play,
        )
        return result.ok
    else:
        result = open_market(
            signal, symbol, side, margin, leverage,
            step_size, tick_size, min_notional, hedge, mark_px, source, play,
        )
        return result.ok


# -- Lifecycle re-exports (Phase 3) ------------------------------------------

from lifecycle.close import _record_closed_position, close_position  # noqa: E402
from lifecycle.sync import sync_open_positions                       # noqa: E402
from lifecycle.reconcile import (                                    # noqa: E402
    _promote_pending,
    _reconcile_one_pending,
    reconcile_pending_entries,
)
from lifecycle.expire import expire_open_positions                   # noqa: E402
