"""Binance Futures 执行层。

Phase 1 重构：HTTP/签名/交易所信息/账户操作已提取到 binance/ 包。
trader.py 保留业务逻辑和 facade 重新导出。
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
# Phase 2 imports
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
    emergency_close as _emergency_close,
    validate_sl_distance as _validate_sl_distance,
)

logger = logging.getLogger("trader")

# Lazy client — safe for test reloads (init_client() called in main.py lifespan).
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
        logger.warning(
            "%s pos=%s auth-fail %d/%d",
            context, pos_id, count, _SYNC_AUTH_FAIL_THRESHOLD,
        )


# -- Facade wrappers (auto-inject client) ------------------------------------

def _get_filters(symbol):           return _filters_fn(_resolve_client(), symbol)
def get_mark_price(symbol):         return _mark_px_fn(_resolve_client(), symbol)
def get_symbol_info(symbol):        return _sym_info_fn(_resolve_client(), symbol)
def _detect_hedge_mode():           return _hedge_fn(_resolve_client())
def get_live_position(symbol):      return _live_pos_fn(_resolve_client(), symbol)
def get_order(symbol, order_id):    return _get_order_fn(_resolve_client(), symbol, order_id)
def set_leverage(symbol, leverage): return _set_lev_fn(_resolve_client(), symbol, leverage)
def set_margin_type(symbol):        return _set_margin_fn(_resolve_client(), symbol)

# Phase 2 wrappers
def place_order(p):                 return _place_order(_resolve_client(), p)
def place_algo_order(p):            return _place_algo(_resolve_client(), p)
def get_algo_order(aid):            return _get_algo(_resolve_client(), aid)
def cancel_algo_order(aid):         return _cancel_algo(_resolve_client(), aid)
def cancel_order_by_id(s, oid):     return _cancel_oid(_resolve_client(), s, oid)
def get_open_algo_orders(s):        return _get_open_algos(_resolve_client(), s)
def cancel_all_orders(s, p=None):   return _cancel_all(_resolve_client(), s, p)
def _build_protective(s, cs, sp, q, ps, k):   return __import__("trading.protective", fromlist=["build_protective_params"]).build_protective_params(s, cs, sp, q, ps, k)
def _place_protective(s, cs, sp, q, ps, ts, k): return place_algo_order(_build_protective(s, cs, sp, q, ps, k))

# _emergency_close and _validate_sl_distance are already imported — they take client as 1st arg
# Replace the imported versions with auto-inject wrappers
_emergency_close_raw = _emergency_close
_validate_sl_distance_raw = _validate_sl_distance

def _emergency_close_wrapped(symbol, side, qty, position_side):
    return _emergency_close_raw(_resolve_client(), symbol, side, qty, position_side)

def _validate_sl_distance_wrapped(side, sl_price, mark_px, tick):
    return _validate_sl_distance_raw(side, sl_price, mark_px, tick)

_emergency_close = _emergency_close_wrapped
_validate_sl_distance = _validate_sl_distance_wrapped


# -- execute_trade ------------------------------------------------------------

def execute_trade(signal: Dict[str, Any]) -> bool:
    signal_log_id = signal["signal_log_id"]
    symbol = signal["symbol"]
    side = signal["side"]
    source = signal.get("source", "") or ""
    play = signal.get("play", "") or ""

    logger.info("execute_trade: source=%s symbol=%s side=%s play=%s signal_log_id=%s",
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
                logger.error("LIMIT entry %s %s: signal missing entry_price", side, symbol)
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
            logger.info("LIMIT placed: %s %s qty=%s price=%.6f order=%s deadline=%s",
                        side, symbol, qty, limit_price, entry_order_id, deadline)
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
            logger.warning("entry avgPrice missing for %s order=%s; using mark=%.6f",
                           symbol, entry_order_id, mark_px)

        logger.info("entry filled: %s %s qty=%s entry=%.6f order=%s",
                    side, symbol, qty, actual_entry, entry_order_id)
    except Exception as exc:
        logger.error("entry %s %s failed: %s", side, symbol, exc)
        update_signal_status(signal_log_id, "error", f"entry: {exc}")
        return False

    final_sl_p: Optional[float] = None
    final_tp_p: Optional[float] = None
    if sl_price is not None:
        final_sl_p = _round_price(sl_price, tick_size)
    if tp_price is not None:
        final_tp_p = _round_price(tp_price, tick_size)

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
        else:
            logger.info("SL skipped: %s %s (no SL price)", side, symbol)

        if final_tp_p is not None:
            tp_resp = _place_protective(
                symbol, close_side, final_tp_p, qty, position_side, tick_size, "TP")
            tp_order_id = str(tp_resp.get("algoId", "") or tp_resp.get("orderId", ""))
            logger.info("TP placed: %s %s tp=%.6f algoId=%s",
                        side, symbol, final_tp_p, tp_order_id)
        else:
            logger.info("TP skipped: %s %s (no TP price)", side, symbol)
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


# -- close_position -----------------------------------------------------------

def close_position(
    source: str, symbol: str, side: str,
    exit_rule: str, close_price: Optional[float] = None,
) -> bool:
    logger.info("close_position: source=%s symbol=%s side=%s rule=%s close=%s",
                source, symbol, side, exit_rule, close_price)

    pos = get_open_position_for_symbol(symbol)
    if pos is None:
        logger.warning("close_position %s %s: no open position", side, symbol)
        return False

    if pos.get("status") == "pending_entry":
        entry_oid = pos.get("entry_order_id")
        logger.info("close_position %s %s: pending_entry, cancelling limit order=%s",
                    side, symbol, entry_oid)
        if entry_oid:
            cancel_order_by_id(symbol, str(entry_oid))
        cancel_pending_position(pos["id"], reason=exit_rule)
        sl_id = pos.get("signal_log_id")
        if sl_id:
            update_signal_status(int(sl_id), "closed", "paper_close_pending")
        return True

    if side.upper() != pos["side"].upper():
        logger.warning("close_position side mismatch: req=%s db=%s", side, pos["side"])

    qty = pos.get("quantity")
    if not qty:
        logger.warning("close_position pos=%s has no quantity", pos["id"])
        return False

    hedge = _detect_hedge_mode()
    position_side = (pos["side"] if hedge else None)
    close_side = "SELL" if pos["side"] == "LONG" else "BUY"

    actual_close: Optional[float] = None
    market_ok = False
    try:
        params: Dict[str, Any] = {
            "symbol": symbol, "side": close_side, "type": "MARKET",
            "quantity": qty, "reduceOnly": "true",
        }
        if position_side:
            params["positionSide"] = position_side
            params.pop("reduceOnly", None)
        resp = place_order(params)
        avg = resp.get("avgPrice")
        if avg and float(avg) > 0:
            actual_close = float(avg)
            market_ok = True
        logger.info("close_position %s %s: MARKET filled qty=%s price=%s",
                    side, symbol, qty, actual_close)
    except Exception as exc:
        logger.error("close_position MARKET failed %s %s: %s", side, symbol, exc)

    if not market_ok:
        logger.critical("close_position ABORTED %s %s: MARKET order failed", side, symbol)
        return False

    try:
        cancel_all_orders(symbol, pos)
    except Exception as exc:
        logger.error("close_position cancel_all_orders %s %s: %s", side, symbol, exc)

    _record_closed_position(pos, exit_rule, actual_close)
    logger.info("close_position done: %s %s source=%s rule=%s close=%.6f",
                side, symbol, source, exit_rule, actual_close or 0)
    return True


# -- sync_open_positions ------------------------------------------------------

def sync_open_positions() -> None:
    if not get_config("binance_api_key", ""):
        return
    if get_config("enabled", "false").lower() != "true":
        return

    for pos in get_open_positions():
        try:
            if get_live_position(pos["symbol"]) is not None:
                _reset_auth_fail_count()
                continue

            close_reason = "unknown"
            close_price: Optional[float] = None
            saw_pending_algo = False

            for order_id, reason in [
                (pos["tp_order_id"], "tp"), (pos["sl_order_id"], "sl"),
            ]:
                if not order_id:
                    continue
                algo_seen = False
                try:
                    a = get_algo_order(order_id)
                    algo_seen = True
                    algo_status = (a.get("algoStatus") or "").upper()
                    actual_px = a.get("actualPrice")
                    actual_oid = a.get("actualOrderId")
                    triggered = bool(
                        (actual_px and float(actual_px) > 0)
                        or (actual_oid and str(actual_oid) not in ("", "0"))
                        or algo_status in ("TRIGGERED", "FILLED", "EXECUTED")
                    )
                    if triggered:
                        close_reason = reason
                        if actual_px and float(actual_px) > 0:
                            close_price = float(actual_px)
                        else:
                            trig_px = a.get("triggerPrice")
                            if trig_px:
                                close_price = float(trig_px)
                        break
                    if algo_status in ("WORKING", "NEW", "PENDING"):
                        saw_pending_algo = True
                        continue
                except Exception as exc:
                    logger.debug("get_algo_order(%s) failed: %s", order_id, exc)
                if algo_seen:
                    continue
                try:
                    o = get_order(pos["symbol"], order_id)
                    if o.get("status") == "FILLED":
                        close_reason = reason
                        avg = o.get("avgPrice")
                        if avg and float(avg) > 0:
                            close_price = float(avg)
                        elif o.get("stopPrice"):
                            close_price = float(o["stopPrice"])
                        break
                    if o.get("status") in ("NEW", "PARTIALLY_FILLED"):
                        saw_pending_algo = True
                except Exception as exc:
                    logger.debug("get_order(%s,%s) failed: %s", pos["symbol"], order_id, exc)

            if close_reason == "unknown" and saw_pending_algo:
                close_reason = "manual"

            if close_reason == "unknown" and not pos.get("tp_order_id"):
                if pos.get("sl_order_id"):
                    try:
                        sl_info = get_algo_order(pos["sl_order_id"])
                        s = (sl_info.get("algoStatus") or "").upper()
                        if s in ("CANCELLED", "EXPIRED", "CANCELED"):
                            close_reason = "paper_close"
                    except Exception:
                        close_reason = "paper_close"
                else:
                    close_reason = "paper_close"

            if close_reason == "unknown":
                all_cancelled = True
                for oid in (pos.get("tp_order_id"), pos.get("sl_order_id")):
                    if not oid:
                        all_cancelled = False
                        break
                    try:
                        info = get_algo_order(oid)
                        if (info.get("algoStatus") or "").upper() not in (
                            "CANCELLED", "EXPIRED", "CANCELED"):
                            all_cancelled = False
                            break
                    except Exception:
                        pass
                close_reason = "paper_close" if all_cancelled else "external"

            if close_price is None:
                try:
                    close_price = get_mark_price(pos["symbol"])
                except Exception:
                    close_price = None

            try:
                cancel_all_orders(pos["symbol"], pos)
            except Exception as exc:
                logger.warning("sync cancel_all_orders %s: %s", pos["symbol"], exc)

            _record_closed_position(pos, close_reason, close_price)
            _reset_auth_fail_count()

        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in (401, 403):
                _handle_auth_fail("sync", pos["id"])
            else:
                logger.warning("sync pos=%s: %s", pos["id"], exc)
        except Exception as exc:
            logger.warning("sync pos=%s: %s", pos["id"], exc)


# -- reconcile_pending_entries ------------------------------------------------

def reconcile_pending_entries() -> None:
    if not get_config("binance_api_key", ""):
        return
    if get_config("enabled", "false").lower() != "true":
        return

    pending = get_pending_entries()
    if not pending:
        return

    for pos in pending:
        try:
            _reconcile_one_pending(pos)
            _reset_auth_fail_count()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in (401, 403):
                _handle_auth_fail("reconcile", pos["id"])
            else:
                logger.warning("reconcile pos=%s: %s", pos["id"], exc)
        except Exception as exc:
            logger.warning("reconcile pos=%s: %s", pos["id"], exc)


def _reconcile_one_pending(pos: Dict[str, Any]) -> None:
    pos_id = pos["id"]
    symbol = pos["symbol"]
    entry_order_id = pos.get("entry_order_id")
    deadline = pos.get("entry_deadline")
    signal_log_id = pos.get("signal_log_id")

    if not entry_order_id:
        logger.error("reconcile pos=%s %s: missing entry_order_id", pos_id, symbol)
        cancel_pending_position(pos_id, reason="error_no_orderid")
        if signal_log_id:
            update_signal_status(int(signal_log_id), "error", "pending without entry_order_id")
        return

    past_deadline = bool(deadline and _now_utc() >= deadline)
    info = get_order(symbol, str(entry_order_id))
    status = (info.get("status") or "").upper()
    executed_qty = float(info.get("executedQty") or 0)
    avg_price = float(info.get("avgPrice") or 0)

    if status == "FILLED":
        _promote_pending(pos, fill_qty=executed_qty, fill_price=avg_price)
        return

    if status == "PARTIALLY_FILLED":
        if not past_deadline:
            return
        if executed_qty > 0 and avg_price > 0:
            cancel_order_by_id(symbol, str(entry_order_id))
            _promote_pending(pos, fill_qty=executed_qty, fill_price=avg_price)
            return
        cancel_order_by_id(symbol, str(entry_order_id))
        cancel_pending_position(pos_id, reason="timeout_no_fill")
        if signal_log_id:
            update_signal_status(int(signal_log_id), "cancelled_pending", "limit timeout")
        return

    if status in ("CANCELED", "EXPIRED", "REJECTED"):
        cancel_pending_position(pos_id, reason="rejected")
        if signal_log_id:
            update_signal_status(int(signal_log_id), "cancelled_pending", f"order {status}")
        return

    if not past_deadline:
        return

    cancel_order_by_id(symbol, str(entry_order_id))
    cancel_pending_position(pos_id, reason="timeout")
    if signal_log_id:
        update_signal_status(int(signal_log_id), "cancelled_pending", "limit timeout")


def _promote_pending(
    pos: Dict[str, Any], *, fill_qty: float, fill_price: float,
) -> None:
    pos_id = pos["id"]
    symbol = pos["symbol"]
    side = pos["side"]
    sl_price = pos.get("sl_price")
    tp_price = pos.get("tp_price")
    play = pos.get("play")
    source = pos.get("source") or ""
    signal_log_id = pos.get("signal_log_id")

    if fill_qty <= 0 or fill_price <= 0:
        logger.error("promote pos=%s %s: invalid fill", pos_id, symbol)
        cancel_pending_position(pos_id, reason="invalid_fill")
        if signal_log_id:
            update_signal_status(int(signal_log_id), "error", "invalid fill in promote")
        return

    try:
        _, tick_size, _ = _get_filters(symbol)
    except Exception as exc:
        logger.error("promote pos=%s %s: get filters failed: %s", pos_id, symbol, exc)
        return

    hedge = _detect_hedge_mode()
    position_side = side if hedge else None
    close_side = "SELL" if side == "LONG" else "BUY"

    final_sl_p = _round_price(float(sl_price), tick_size) if sl_price is not None else None
    final_tp_p = _round_price(float(tp_price), tick_size) if tp_price is not None else None

    if final_sl_p is not None:
        try:
            mark_px = get_mark_price(symbol)
            _validate_sl_distance(side, final_sl_p, mark_px, tick_size)
        except (ValueError, Exception) as exc:
            logger.warning("promote SL validation failed %s: %s", symbol, exc)

    sl_order_id = ""
    tp_order_id = ""
    try:
        if final_sl_p is not None:
            sl_resp = _place_protective(
                symbol, close_side, final_sl_p, fill_qty, position_side, tick_size, "SL")
            sl_order_id = str(sl_resp.get("algoId", "") or sl_resp.get("orderId", ""))
        if final_tp_p is not None:
            tp_resp = _place_protective(
                symbol, close_side, final_tp_p, fill_qty, position_side, tick_size, "TP")
            tp_order_id = str(tp_resp.get("algoId", "") or tp_resp.get("orderId", ""))
    except Exception as exc:
        logger.error("promote SL/TP failed pos=%s %s: %s — emergency close",
                     pos_id, symbol, exc)
        try:
            cancel_all_orders(symbol)
        except Exception:
            pass
        _emergency_close(symbol, side, fill_qty, position_side)
        cancel_pending_position(pos_id, reason="sltp_failed")
        if signal_log_id:
            update_signal_status(int(signal_log_id), "error", f"SL/TP failed in promote: {exc}")
        return

    expire_at = compute_expire_at(resolve_expire_hours(play, source=source))
    promote_pending_to_open(
        pos_id, entry_price=fill_price, quantity=fill_qty,
        sl_order_id=sl_order_id, tp_order_id=tp_order_id, expire_at=expire_at,
    )
    if signal_log_id:
        update_signal_status(int(signal_log_id), "traded")
    logger.info("promote pos=%s %s %s qty=%.6f entry=%.6f sl=%s tp=%s",
                pos_id, side, symbol, fill_qty, fill_price, final_sl_p, final_tp_p)


# -- expire_open_positions ----------------------------------------------------

def expire_open_positions() -> None:
    if not get_config("binance_api_key", ""):
        return
    if get_config("enabled", "false").lower() != "true":
        return

    expired = get_open_expired_positions()
    if not expired:
        return

    for pos in expired:
        symbol = pos["symbol"]
        side = pos["side"]
        qty = pos.get("quantity")
        if not qty:
            continue

        logger.info("Expiring pos id=%s %s %s", pos["id"], side, symbol)
        hedge = _detect_hedge_mode()
        position_side = (side if hedge else None)
        close_side = "SELL" if side == "LONG" else "BUY"
        close_price: Optional[float] = None
        market_ok = False

        try:
            cancel_all_orders(symbol, pos)
        except Exception as exc:
            logger.warning("expire cancel_all_orders %s: %s", symbol, exc)
        try:
            params: Dict[str, Any] = {
                "symbol": symbol, "side": close_side, "type": "MARKET",
                "quantity": qty, "reduceOnly": "true",
            }
            if position_side:
                params["positionSide"] = position_side
                params.pop("reduceOnly", None)
            resp = place_order(params)
            avg = resp.get("avgPrice")
            if avg and float(avg) > 0:
                close_price = float(avg)
            market_ok = True
        except Exception as exc:
            logger.critical("expire close FAILED pos=%s %s: %s", pos["id"], symbol, exc)

        if not market_ok:
            continue

        if close_price is None:
            try:
                close_price = get_mark_price(symbol)
            except Exception:
                close_price = 0.0

        _record_closed_position(pos, "expired", close_price)


def _record_closed_position(
    pos: Dict[str, Any], close_reason: str, close_price: Optional[float],
) -> None:
    entry = pos.get("entry_price")
    qty = pos.get("quantity")
    lev = pos.get("leverage") or 1
    side = pos.get("side")

    if entry is None or qty is None or close_price is None or entry <= 0:
        logger.warning("record_closed pos=%s incomplete data; pnl=0", pos["id"])
        update_position_closed(
            position_id=pos["id"], close_reason=close_reason,
            close_price=close_price or 0.0, closed_at=_now_utc(),
            pnl_usdt=0.0, pnl_pct=0.0,
        )
        return

    if side == "LONG":
        pnl = qty * (close_price - entry)
        ret = close_price / entry - 1.0
    else:
        pnl = qty * (entry - close_price)
        ret = (entry - close_price) / entry if entry > 0 else 0.0

    pnl_pct = ret * lev * 100.0
    update_position_closed(
        position_id=pos["id"], close_reason=close_reason,
        close_price=close_price, closed_at=_now_utc(),
        pnl_usdt=round(pnl, 4), pnl_pct=round(pnl_pct, 4),
    )
    signal_log_id = pos.get("signal_log_id")
    if signal_log_id:
        update_signal_status(signal_log_id, "closed", close_reason)
    logger.info("Closed %s %s reason=%s close=%.6f pnl=%.4f pct=%.2f%%",
                side, pos.get("symbol"), close_reason, close_price, pnl, pnl_pct)
