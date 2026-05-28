"""对账 pending_entry：成交→promote，超时→取消。"""
from __future__ import annotations

import logging

import httpx

from binance.time_sync import now_utc as _now_utc

logger = logging.getLogger("lifecycle.reconcile")


def reconcile_pending_entries() -> None:
    from db import get_config, get_pending_entries
    from trader import _handle_auth_fail, _reset_auth_fail_count

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
    from db import cancel_pending_position, update_signal_status
    from trader import cancel_order_by_id, get_order

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
    from db import (
        cancel_pending_position, compute_expire_at,
        promote_pending_to_open, resolve_expire_hours, update_signal_status,
    )
    from trader import (
        _detect_hedge_mode,
        _emergency_close,
        _get_filters,
        _place_protective,
        _round_price,
        _validate_sl_distance,
        cancel_all_orders,
        get_mark_price,
    )

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
