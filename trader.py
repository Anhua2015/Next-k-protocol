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
    get_config,
    set_config,
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
    get_account_summary as _account_summary_fn,
    list_live_positions as _list_live_positions_fn,
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
    validate_tp_distance as _validate_tp_distance_fn,
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
def get_account_summary():      return _account_summary_fn(_resolve_client())
def list_live_positions():      return _list_live_positions_fn(_resolve_client())
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

def _validate_tp_distance(side, tp, mark, tick):
    return _validate_tp_distance_fn(side, tp, mark, tick)

def _build_protective(s, cs, sp, q, ps, k):
    from trading.protective import build_protective_params
    return build_protective_params(s, cs, sp, q, ps, k)

def _place_protective(s, cs, sp, q, ps, ts, k):
    params = _build_protective(s, cs, sp, q, ps, k)
    logger.info(
        "place protective order symbol=%s kind=%s trigger=%s qty=%s close_side=%s position_side=%s",
        s,
        k,
        params.get("triggerPrice"),
        params.get("quantity"),
        cs,
        ps or "BOTH",
    )
    resp = place_algo_order(params)
    logger.info(
        "placed protective order symbol=%s kind=%s algo_id=%s order_id=%s trigger=%s qty=%s",
        s,
        k,
        resp.get("algoId") or "",
        resp.get("orderId") or "",
        params.get("triggerPrice"),
        params.get("quantity"),
    )
    return resp


_TERMINAL_ALGO_STATUSES = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "FAILED"}


def _position_side_for_live_pos(pos: Dict[str, Any], hedge_mode: bool) -> Optional[str]:
    if not hedge_mode:
        return None
    raw = str(pos.get("positionSide") or "").upper()
    return raw or None


def _algo_order_matches(
    order: Dict[str, Any],
    *,
    kind: str,
    close_side: Optional[str],
    position_side: Optional[str],
    actual_side: Optional[str] = None,
    reference_price: Optional[float] = None,
) -> tuple[bool, Optional[str]]:
    order_type = str(
        order.get("type")
        or order.get("origType")
        or order.get("algoType")
        or ""
    ).upper()
    if kind == "SL":
        if "STOP" in order_type and "TAKE_PROFIT" not in order_type:
            pass
        elif "TAKE_PROFIT" in order_type:
            return False, "type_mismatch"
        else:
            trigger_match, reason = _algo_order_matches_by_trigger(
                order,
                kind=kind,
                actual_side=actual_side,
                reference_price=reference_price,
            )
            if not trigger_match:
                return False, reason
    else:
        if "TAKE_PROFIT" in order_type:
            pass
        elif "STOP" in order_type:
            return False, "type_mismatch"
        else:
            trigger_match, reason = _algo_order_matches_by_trigger(
                order,
                kind=kind,
                actual_side=actual_side,
                reference_price=reference_price,
            )
            if not trigger_match:
                return False, reason

    status = str(order.get("status") or order.get("algoStatus") or "").upper()
    if status in _TERMINAL_ALGO_STATUSES:
        return False, "terminal_status"

    if close_side:
        order_side = str(order.get("side") or "").upper()
        if order_side and order_side != close_side:
            return False, "side_mismatch"

    if position_side:
        order_pos_side = str(order.get("positionSide") or "").upper()
        if order_pos_side and order_pos_side != str(position_side).upper():
            return False, "position_side_mismatch"

    return True, None


def _algo_order_trigger_price(order: Dict[str, Any]) -> Optional[float]:
    raw = order.get("triggerPrice")
    if raw in (None, ""):
        raw = order.get("stopPrice")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _algo_order_matches_by_trigger(
    order: Dict[str, Any],
    *,
    kind: str,
    actual_side: Optional[str],
    reference_price: Optional[float],
) -> tuple[bool, Optional[str]]:
    if not actual_side:
        return False, "actual_side_missing"
    if reference_price is None or reference_price <= 0:
        return False, "reference_price_missing"

    trigger_price = _algo_order_trigger_price(order)
    if trigger_price is None:
        return False, "trigger_price_missing"

    side = str(actual_side).upper()
    if side == "LONG":
        is_tp = trigger_price > reference_price
    elif side == "SHORT":
        is_tp = trigger_price < reference_price
    else:
        return False, "actual_side_invalid"

    if kind == "TP":
        return (True, None) if is_tp else (False, "trigger_kind_mismatch")
    return (False, "trigger_kind_mismatch") if is_tp else (True, None)


def _should_resolve_algo_order_detail(order: Dict[str, Any]) -> bool:
    order_type = str(order.get("type") or "").upper()
    orig_type = str(order.get("origType") or "").upper()
    algo_type = str(order.get("algoType") or "").upper()
    return algo_type == "CONDITIONAL" and not order_type and not orig_type


def _resolve_algo_order_for_matching(order: Dict[str, Any]) -> Dict[str, Any]:
    if not _should_resolve_algo_order_detail(order):
        return order

    algo_id = order.get("algoId") or order.get("clientAlgoId")
    if not algo_id:
        return order

    try:
        detail = get_algo_order(str(algo_id))
    except Exception as exc:
        logger.warning(
            "algoOrder detail fetch failed algo_id=%s symbol=%s error=%s",
            algo_id,
            order.get("symbol") or "",
            exc,
        )
        return order

    resolved = dict(order)
    if isinstance(detail, dict):
        resolved.update(detail)
    logger.info("algoOrder detail %s", _algo_order_diag(resolved))
    return resolved


def _open_protective_algo_orders(
    symbol: str,
    *,
    kind: str,
    close_side: Optional[str],
    position_side: Optional[str],
    actual_side: Optional[str] = None,
    reference_price: Optional[float] = None,
) -> list[Dict[str, Any]]:
    raw_orders = get_open_algo_orders(symbol)
    logger.info("openAlgoOrders raw symbol=%s count=%d", symbol, len(raw_orders))
    matched: list[Dict[str, Any]] = []
    for order in raw_orders:
        logger.info("openAlgoOrders raw %s", _algo_order_diag(order))
        order_for_match = _resolve_algo_order_for_matching(order)
        matched_order, reason = _algo_order_matches(
            order_for_match,
            kind=kind,
            close_side=close_side,
            position_side=position_side,
            actual_side=actual_side,
            reference_price=reference_price,
        )
        if not matched_order:
            logger.info(
                "protective order skipped symbol=%s kind=%s %s reason=%s",
                symbol,
                kind,
                _algo_order_diag(order_for_match),
                reason or "unknown",
            )
            continue
        matched.append(order_for_match)
    return matched


def _algo_order_id(order: Dict[str, Any]) -> str:
    return str(order.get("algoId") or order.get("clientAlgoId") or "unknown")


def _live_pos_diag(pos: Dict[str, Any]) -> str:
    return (
        "symbol={symbol} position_amt={position_amt} position_side={position_side} "
        "entry_price={entry_price} mark_price={mark_price} unrealized_pnl={unrealized_pnl}"
    ).format(
        symbol=pos.get("symbol") or "",
        position_amt=pos.get("positionAmt") or pos.get("quantity") or "",
        position_side=pos.get("positionSide") or "",
        entry_price=pos.get("entryPrice") or "",
        mark_price=pos.get("markPrice") or "",
        unrealized_pnl=pos.get("unRealizedProfit") or pos.get("unrealizedProfit") or "",
    )


def _algo_order_diag(order: Dict[str, Any]) -> str:
    return (
        "algo_id={algo_id} symbol={symbol} side={side} position_side={position_side} "
        "type={type_} orig_type={orig_type} algo_type={algo_type} status={status} "
        "trigger_price={trigger_price} stop_price={stop_price}"
    ).format(
        algo_id=_algo_order_id(order),
        symbol=order.get("symbol") or "",
        side=order.get("side") or "",
        position_side=order.get("positionSide") or "",
        type_=order.get("type") or "",
        orig_type=order.get("origType") or "",
        algo_type=order.get("algoType") or "",
        status=order.get("status") or order.get("algoStatus") or "",
        trigger_price=order.get("triggerPrice") or "",
        stop_price=order.get("stopPrice") or "",
    )


def _cancel_open_protective_orders(
    symbol: str,
    *,
    kind: str,
    close_side: Optional[str],
    position_side: Optional[str],
    actual_side: Optional[str] = None,
    reference_price: Optional[float] = None,
) -> None:
    matched = _open_protective_algo_orders(
        symbol,
        kind=kind,
        close_side=close_side,
        position_side=position_side,
        actual_side=actual_side,
        reference_price=reference_price,
    )
    logger.info(
        "cancel protective orders symbol=%s kind=%s close_side=%s position_side=%s count=%d algo_ids=%s",
        symbol,
        kind,
        close_side or "",
        position_side or "BOTH",
        len(matched),
        ",".join(_algo_order_id(order) for order in matched) or "-",
    )
    for order in matched:
        algo_id = order.get("algoId") or order.get("clientAlgoId")
        if algo_id:
            logger.info(
                "cancel protective order symbol=%s kind=%s algo_id=%s status=%s",
                symbol,
                kind,
                algo_id,
                str(order.get("status") or order.get("algoStatus") or "").upper() or "UNKNOWN",
            )
            ok = cancel_algo_order(str(algo_id))
            logger.info(
                "cancel protective order result symbol=%s kind=%s algo_id=%s ok=%s",
                symbol,
                kind,
                algo_id,
                ok,
            )

    remaining = _open_protective_algo_orders(
        symbol,
        kind=kind,
        close_side=close_side,
        position_side=position_side,
        actual_side=actual_side,
        reference_price=reference_price,
    )
    if remaining:
        logger.error(
            "stale protective orders remain symbol=%s kind=%s close_side=%s position_side=%s algo_ids=%s",
            symbol,
            kind,
            close_side or "",
            position_side or "BOTH",
            ",".join(_algo_order_id(order) for order in remaining),
        )
        ids = [
            _algo_order_id(order)
            for order in remaining
        ]
        raise RuntimeError(
            f"stale_{kind.lower()}_orders_remain:{','.join(ids)}"
        )
    logger.info(
        "cancel protective orders cleared symbol=%s kind=%s close_side=%s position_side=%s",
        symbol,
        kind,
        close_side or "",
        position_side or "BOTH",
    )


def _close_live_position(signal: Dict[str, Any]) -> bool:
    signal_log_id = signal["signal_log_id"]
    symbol = signal["symbol"]
    requested_side = str(signal["side"]).upper()
    source = signal.get("source", "") or ""

    live_pos = get_live_position(symbol)
    if not live_pos:
        update_signal_status(signal_log_id, "error", "live_position_missing")
        return False

    amt = float(live_pos.get("positionAmt") or 0)
    if amt == 0:
        update_signal_status(signal_log_id, "error", "live_position_missing")
        return False

    actual_side = "LONG" if amt > 0 else "SHORT"
    if requested_side != actual_side:
        update_signal_status(
            signal_log_id,
            "error",
            f"side_mismatch:{requested_side}->{actual_side}",
        )
        return False

    hedge_mode = _detect_hedge_mode()
    position_side = _position_side_for_live_pos(live_pos, hedge_mode)
    qty = abs(amt)

    try:
        cancel_all_orders(symbol)
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": "SELL" if actual_side == "LONG" else "BUY",
            "type": "MARKET",
            "quantity": qty,
            "reduceOnly": "true",
        }
        if position_side:
            params["positionSide"] = position_side
            params.pop("reduceOnly", None)
        resp = place_order(params)
    except Exception as exc:
        logger.error("close live position failed %s %s: %s", actual_side, symbol, exc)
        update_signal_status(signal_log_id, "error", f"close_failed: {exc}")
        return False

    update_signal_status(signal_log_id, "traded", None)
    from repos.signals_repo import update_execution

    update_execution(
        signal_log_id,
        status="traded",
        result={
            "source": source,
            "action": "close",
            "symbol": symbol,
            "side": actual_side,
            "quantity": qty,
            "close_price": signal.get("close_price"),
            "order": resp,
        },
        payload={
            "symbol": symbol,
            "side": requested_side,
            "close_price": signal.get("close_price"),
            "client_ref": signal.get("client_ref") or "",
        },
    )
    return True


def _update_live_stop_loss(signal: Dict[str, Any]) -> bool:
    signal_log_id = signal["signal_log_id"]
    symbol = signal["symbol"]
    side = str(signal["side"]).upper()
    new_sl_price = signal.get("sl_price")
    if new_sl_price is None:
        update_signal_status(signal_log_id, "error", "missing_sl_price")
        return False

    live_pos = get_live_position(symbol)
    if not live_pos:
        update_signal_status(signal_log_id, "error", "live_position_missing")
        return False

    amt = float(live_pos.get("positionAmt") or 0)
    if amt == 0:
        update_signal_status(signal_log_id, "error", "live_position_missing")
        return False

    actual_side = "LONG" if amt > 0 else "SHORT"
    if side != actual_side:
        update_signal_status(
            signal_log_id,
            "error",
            f"side_mismatch:{side}->{actual_side}",
        )
        return False

    hedge_mode = _detect_hedge_mode()
    position_side = _position_side_for_live_pos(live_pos, hedge_mode)
    qty = abs(amt)
    mark_px = float(live_pos.get("markPrice") or get_mark_price(symbol) or 0)
    close_side = "SELL" if actual_side == "LONG" else "BUY"
    logger.info(
        "update_sl context symbol=%s requested_side=%s actual_side=%s hedge_mode=%s "
        "close_side=%s position_side=%s qty=%s mark_price=%s new_sl=%s live_pos={%s}",
        symbol,
        side,
        actual_side,
        hedge_mode,
        close_side,
        position_side or "BOTH",
        qty,
        mark_px,
        new_sl_price,
        _live_pos_diag(live_pos),
    )
    try:
        step_size, tick_size, _min_notional = _get_filters(symbol)
        _validate_sl_distance(actual_side, float(new_sl_price), mark_px, tick_size)
        _cancel_open_protective_orders(
            symbol,
            kind="SL",
            close_side=close_side,
            position_side=position_side,
            actual_side=actual_side,
            reference_price=mark_px,
        )
        resp = _place_protective(
            symbol,
            close_side,
            _round_price(float(new_sl_price), tick_size),
            _round_quantity(qty, step_size),
            position_side,
            tick_size,
            "SL",
        )
    except Exception as exc:
        logger.error("update live stop failed %s %s: %s", actual_side, symbol, exc)
        update_signal_status(signal_log_id, "error", f"update_sl_failed: {exc}")
        return False

    update_signal_status(signal_log_id, "traded", None)
    from repos.signals_repo import update_execution

    update_execution(
        signal_log_id,
        status="traded",
        result={
            "action": "update_sl",
            "symbol": symbol,
            "side": actual_side,
            "quantity": qty,
            "mark_price": mark_px,
            "new_sl_price": float(new_sl_price),
            "sl_order": resp,
        },
        payload={
            "symbol": symbol,
            "side": side,
            "new_sl_price": float(new_sl_price),
            "client_ref": signal.get("client_ref") or "",
        },
    )
    return True


def _update_live_take_profit(signal: Dict[str, Any]) -> bool:
    signal_log_id = signal["signal_log_id"]
    symbol = signal["symbol"]
    side = str(signal["side"]).upper()
    new_tp_price = signal.get("tp_price")
    if new_tp_price is None:
        update_signal_status(signal_log_id, "error", "missing_tp_price")
        return False

    live_pos = get_live_position(symbol)
    if not live_pos:
        update_signal_status(signal_log_id, "error", "live_position_missing")
        return False

    amt = float(live_pos.get("positionAmt") or 0)
    if amt == 0:
        update_signal_status(signal_log_id, "error", "live_position_missing")
        return False

    actual_side = "LONG" if amt > 0 else "SHORT"
    if side != actual_side:
        update_signal_status(
            signal_log_id,
            "error",
            f"side_mismatch:{side}->{actual_side}",
        )
        return False

    hedge_mode = _detect_hedge_mode()
    position_side = _position_side_for_live_pos(live_pos, hedge_mode)
    qty = abs(amt)
    mark_px = float(live_pos.get("markPrice") or get_mark_price(symbol) or 0)
    close_side = "SELL" if actual_side == "LONG" else "BUY"
    logger.info(
        "update_tp context symbol=%s requested_side=%s actual_side=%s hedge_mode=%s "
        "close_side=%s position_side=%s qty=%s mark_price=%s new_tp=%s live_pos={%s}",
        symbol,
        side,
        actual_side,
        hedge_mode,
        close_side,
        position_side or "BOTH",
        qty,
        mark_px,
        new_tp_price,
        _live_pos_diag(live_pos),
    )
    try:
        step_size, tick_size, _min_notional = _get_filters(symbol)
        _validate_tp_distance(actual_side, float(new_tp_price), mark_px, tick_size)
        _cancel_open_protective_orders(
            symbol,
            kind="TP",
            close_side=close_side,
            position_side=position_side,
            actual_side=actual_side,
            reference_price=mark_px,
        )
        resp = _place_protective(
            symbol,
            close_side,
            _round_price(float(new_tp_price), tick_size),
            _round_quantity(qty, step_size),
            position_side,
            tick_size,
            "TP",
        )
    except Exception as exc:
        logger.error("update live TP failed %s %s: %s", actual_side, symbol, exc)
        update_signal_status(signal_log_id, "error", f"update_tp_failed: {exc}")
        return False

    update_signal_status(signal_log_id, "traded", None)
    from repos.signals_repo import update_execution

    update_execution(
        signal_log_id,
        status="traded",
        result={
            "action": "update_tp",
            "symbol": symbol,
            "side": actual_side,
            "quantity": qty,
            "mark_price": mark_px,
            "new_tp_price": float(new_tp_price),
            "tp_order": resp,
        },
        payload={
            "symbol": symbol,
            "side": side,
            "new_tp_price": float(new_tp_price),
            "client_ref": signal.get("client_ref") or "",
        },
    )
    return True


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
    action = str(signal.get("action", "") or "").lower()

    logger.info("execute_trade: source=%s symbol=%s side=%s play=%s id=%s",
                source, symbol, side, play, signal_log_id)

    if action == "close":
        return _close_live_position(signal)
    if action == "update_sl":
        return _update_live_stop_loss(signal)
    if action == "update_tp":
        return _update_live_take_profit(signal)

    # Resolve margin/leverage
    try:
        margin = float(signal.get("margin_usdt", 0) or 0)
        leverage = int(float(signal.get("leverage", 0) or 0))
    except (TypeError, ValueError) as exc:
        logger.error("config parse failed %s: %s", symbol, exc)
        update_signal_status(signal_log_id, "error", f"bad signal leverage/margin: {exc}")
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

    entry_type = get_config("entry_type", "MARKET").upper()
    if action == "rolling":
        entry_type = "MARKET"

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
