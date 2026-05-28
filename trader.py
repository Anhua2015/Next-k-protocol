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
    if count >= _SYNC_AUTH_FAIL_THRESHOLD:
        set_config("enabled", "false")
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


# -- execute_trade (stays here until Phase 4) --------------------------------

def execute_trade(signal: Dict[str, Any]) -> bool:
    signal_log_id = signal["signal_log_id"]
    symbol = signal["symbol"]
    side = signal["side"]
    source = signal.get("source", "") or ""
    play = signal.get("play", "") or ""

    logger.info("execute_trade: source=%s symbol=%s side=%s play=%s id=%s",
                source, symbol, side, play, signal_log_id)

    if source in ("momentum", "jiezhen"):
        margin = float(get_source_config(source, "margin_usdt", "100"))
        leverage = int(get_source_config(source, "leverage", "10"))
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

    try:
        sl_price = float(signal["sl_price"]) if signal.get("sl_price") is not None else None
        tp_price = float(signal["tp_price"]) if signal.get("tp_price") is not None else None
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

    qty: float = 0.0
    actual_entry: float = 0.0
    position_side: Optional[str] = None
    entry_order_id = ""

    try:
        step_size, tick_size, min_notional = _get_filters(symbol)
        set_margin_type(symbol)
        set_leverage(symbol, leverage)

        hedge = _detect_hedge_mode()
        if hedge:
            position_side = "LONG" if side == "LONG" else "SHORT"

        mark_px = get_mark_price(symbol)
        order_side = "BUY" if side == "LONG" else "SELL"
        close_side = "SELL" if side == "LONG" else "BUY"

        if entry_type == "LIMIT":
            signal_entry = signal.get("entry_price")
            if signal_entry is None or float(signal_entry) <= 0:
                logger.error("LIMIT entry %s %s: missing entry_price", side, symbol)
                update_signal_status(signal_log_id, "error", "limit needs entry_price")
                return False
            limit_price_raw = float(signal_entry)
            limit_price = _round_price(limit_price_raw, tick_size)
            raw_qty = margin * leverage / limit_price
            qty = _round_quantity(raw_qty, step_size)
            if qty <= 0:
                raise ValueError(f"computed qty={qty}")
            if qty * limit_price < min_notional:
                raise ValueError(f"notional {qty * limit_price:.2f} < min {min_notional}")

            entry_params: Dict[str, Any] = {
                "symbol": symbol, "side": order_side, "type": "LIMIT",
                "timeInForce": "GTC", "quantity": qty, "price": limit_price,
                "newOrderRespType": "ACK",
            }
            if position_side:
                entry_params["positionSide"] = position_side
            entry_resp = place_order(entry_params)
            entry_order_id = str(entry_resp.get("orderId", ""))
            if not entry_order_id:
                raise ValueError(f"LIMIT response missing orderId: {entry_resp}")

            timeout_sec = float(get_source_config(
                source, "limit_entry_timeout_sec",
                get_config("limit_entry_timeout_sec", "30"),
            ))
            deadline = compute_pending_deadline(timeout_sec)
            insert_pending_position(
                signal_log_id=signal_log_id, symbol=symbol, side=side,
                entry_order_id=entry_order_id, entry_price=limit_price,
                sl_price=sl_price, tp_price=tp_price,
                quantity=qty, notional_usdt=margin, leverage=leverage,
                opened_at=_now_utc(), entry_deadline=deadline,
                play=play, source=source,
            )
            update_signal_status(signal_log_id, "pending_entry")
            logger.info("LIMIT placed: %s %s qty=%s price=%.6f order=%s",
                        side, symbol, qty, limit_price, entry_order_id)
            return True

        # MARKET
        raw_qty = margin * leverage / mark_px
        qty = _round_quantity(raw_qty, step_size)
        if qty <= 0:
            raise ValueError(f"computed qty={qty}")
        if qty * mark_px < min_notional:
            raise ValueError(f"notional {qty * mark_px:.2f} < min {min_notional}")

        entry_params = {
            "symbol": symbol, "side": order_side, "type": "MARKET",
            "quantity": qty, "newOrderRespType": "RESULT",
        }
        if position_side:
            entry_params["positionSide"] = position_side
        entry_resp = place_order(entry_params)
        entry_order_id = str(entry_resp.get("orderId", ""))
        actual_entry = float(entry_resp.get("avgPrice") or 0)
        if actual_entry <= 0 and entry_order_id:
            try:
                detail = get_order(symbol, entry_order_id)
                actual_entry = float(detail.get("avgPrice") or 0)
            except Exception as exc:
                logger.warning("get_order after entry %s: %s", symbol, exc)
        if actual_entry <= 0:
            actual_entry = mark_px
            logger.warning("entry avgPrice missing for %s; using mark=%.6f",
                           symbol, mark_px)

        logger.info("entry filled: %s %s qty=%s entry=%.6f order=%s",
                    side, symbol, qty, actual_entry, entry_order_id)
    except Exception as exc:
        logger.error("entry %s %s failed: %s", side, symbol, exc)
        update_signal_status(signal_log_id, "error", f"entry: {exc}")
        return False

    final_sl_p = _round_price(sl_price, tick_size) if sl_price else None
    final_tp_p = _round_price(tp_price, tick_size) if tp_price else None
    logger.info("signal SL/TP: %s %s source=%s entry=%.6f sl=%s tp=%s",
                side, symbol, source, actual_entry, final_sl_p, final_tp_p)

    if final_sl_p is not None:
        try:
            _validate_sl_distance(side, final_sl_p, mark_px, tick_size)
        except ValueError as exc:
            logger.warning("SL validation failed %s: %s", symbol, exc)

    sl_order_id = ""
    tp_order_id = ""
    try:
        if final_sl_p is not None:
            sl_resp = _place_protective(
                symbol, close_side, final_sl_p, qty, position_side, tick_size, "SL")
            sl_order_id = str(sl_resp.get("algoId", "") or sl_resp.get("orderId", ""))
            logger.info("SL placed: %s %s sl=%.6f algoId=%s",
                        side, symbol, final_sl_p, sl_order_id)
        if final_tp_p is not None:
            tp_resp = _place_protective(
                symbol, close_side, final_tp_p, qty, position_side, tick_size, "TP")
            tp_order_id = str(tp_resp.get("algoId", "") or tp_resp.get("orderId", ""))
            logger.info("TP placed: %s %s tp=%.6f algoId=%s",
                        side, symbol, final_tp_p, tp_order_id)
    except Exception as exc:
        logger.error("SL/TP placement failed %s %s: %s", side, symbol, exc)
        try:
            cancel_all_orders(symbol)
        except Exception:
            pass
        _emergency_close(symbol, side, qty, position_side)
        update_signal_status(signal_log_id, "error", f"SL/TP failed: {exc}")
        return False

    insert_position(
        signal_log_id=signal_log_id, symbol=symbol, side=side,
        entry_order_id=entry_order_id, sl_order_id=sl_order_id, tp_order_id=tp_order_id,
        entry_price=actual_entry, sl_price=final_sl_p, tp_price=final_tp_p,
        quantity=qty, notional_usdt=margin, leverage=leverage,
        opened_at=_now_utc(), play=play, source=source,
    )
    update_signal_status(signal_log_id, "traded")
    logger.info("Opened %s %s source=%s qty=%s entry=%.6f sl=%.6f tp=%.6f",
                side, symbol, source, qty, actual_entry, final_sl_p, final_tp_p)
    return True


# -- Lifecycle re-exports (Phase 3) ------------------------------------------

from lifecycle.close import _record_closed_position, close_position  # noqa: E402
from lifecycle.sync import sync_open_positions                       # noqa: E402
from lifecycle.reconcile import (                                    # noqa: E402
    _promote_pending,
    _reconcile_one_pending,
    reconcile_pending_entries,
)
from lifecycle.expire import expire_open_positions                   # noqa: E402
