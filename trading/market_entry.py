"""MARKET 市价单入场。"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("trading.market_entry")

from dataclasses import dataclass


@dataclass
class MarketEntryResult:
    ok: bool
    position_id: int = 0
    qty: float = 0.0
    entry_price: float = 0.0
    entry_order_id: str = ""
    position_side: Optional[str] = None
    error: str = ""


def open_market(
    signal: Dict[str, Any],
    symbol: str,
    side: str,
    margin: float,
    leverage: int,
    step_size: str,
    tick_size: str,
    min_notional: float,
    hedge: bool,
    mark_px: float,
    source: str,
    play: str,
) -> MarketEntryResult:
    """执行 MARKET 市价单入场，返回 MarketEntryResult。"""
    from binance.time_sync import now_utc as _now_utc
    from binance.exchange_info import round_price as _round_price, round_quantity as _round_quantity
    from db import insert_position, update_signal_execution, update_signal_status
    from trader import (
        _emergency_close,
        _place_protective,
        _validate_sl_distance,
        cancel_all_orders,
        get_order,
        place_order,
    )

    signal_log_id = signal["signal_log_id"]
    order_side = "BUY" if side == "LONG" else "SELL"
    position_side = side if hedge else None

    # Compute SL/TP
    sl_price = None
    tp_price = None
    try:
        sl_price = float(signal["sl_price"]) if signal.get("sl_price") is not None else None
        tp_price = float(signal["tp_price"]) if signal.get("tp_price") is not None else None
    except (TypeError, ValueError):
        pass

    qty: float = 0.0
    actual_entry: float = 0.0
    entry_order_id = ""

    try:
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

        logger.info("entry filled: %s %s qty=%s entry=%.6f order=%s",
                    side, symbol, qty, actual_entry, entry_order_id)
    except Exception as exc:
        logger.error("entry %s %s failed: %s", side, symbol, exc)
        update_signal_status(signal_log_id, "error", f"entry: {exc}")
        return MarketEntryResult(ok=False, error=str(exc))

    # SL/TP
    close_side = "SELL" if side == "LONG" else "BUY"
    final_sl_p = _round_price(sl_price, tick_size) if sl_price else None
    final_tp_p = _round_price(tp_price, tick_size) if tp_price else None

    if final_sl_p is not None:
        try:
            _validate_sl_distance(side, final_sl_p, mark_px, tick_size)
        except ValueError as exc:
            logger.warning("SL validation failed %s: %s", symbol, exc)

    sl_order_id = ""
    tp_order_id = ""
    try:
        if final_sl_p is not None:
            sl_resp = _place_protective(symbol, close_side, final_sl_p, qty, position_side, tick_size, "SL")
            sl_order_id = str(sl_resp.get("algoId", "") or sl_resp.get("orderId", ""))
        if final_tp_p is not None:
            tp_resp = _place_protective(symbol, close_side, final_tp_p, qty, position_side, tick_size, "TP")
            tp_order_id = str(tp_resp.get("algoId", "") or tp_resp.get("orderId", ""))
    except Exception as exc:
        logger.error("SL/TP placement failed %s %s: %s", side, symbol, exc)
        try:
            cancel_all_orders(symbol)
        except Exception:
            pass
        _emergency_close(symbol, side, qty, position_side)
        update_signal_status(signal_log_id, "error", f"SL/TP failed: {exc}")
        return MarketEntryResult(ok=False, error=str(exc))

    position_id = insert_position(
        signal_log_id=signal_log_id, symbol=symbol, side=side,
        entry_order_id=entry_order_id, sl_order_id=sl_order_id, tp_order_id=tp_order_id,
        entry_price=actual_entry, sl_price=final_sl_p, tp_price=final_tp_p,
        quantity=qty, notional_usdt=margin * leverage, leverage=leverage,
        opened_at=_now_utc(), play=play, source=source,
        profile_id=signal.get("profile_id"), client_ref=signal.get("client_ref") or "",
    )
    update_signal_execution(
        signal_log_id,
        status="traded",
        position_id=position_id,
        result={
            "ok": True,
            "position_id": position_id,
            "entry_order_id": entry_order_id,
            "quantity": qty,
            "entry_price": actual_entry,
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
        },
    )
    from observability.metrics import TRADES_OPENED
    TRADES_OPENED.labels(source=source, side=side, entry_type="MARKET").inc()
    logger.info("Opened %s %s source=%s qty=%s entry=%.6f sl=%.6f tp=%.6f",
                side, symbol, source, qty, actual_entry, final_sl_p, final_tp_p)
    return MarketEntryResult(ok=True, position_id=position_id, qty=qty, entry_price=actual_entry,
                             entry_order_id=entry_order_id, position_side=position_side)
