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
    from binance.exchange_info import round_price as _round_price, round_quantity as _round_quantity
    from db import update_signal_status
    from trader import get_order, place_order
    from trading.stop_limit_entry import _attach_protective

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

        signal["_entry_order_id"] = entry_order_id
        logger.info("entry filled: %s %s qty=%s entry=%.6f order=%s",
                    side, symbol, qty, actual_entry, entry_order_id)
    except Exception as exc:
        logger.error("entry %s %s failed: %s", side, symbol, exc)
        update_signal_status(signal_log_id, "error", f"entry: {exc}")
        return MarketEntryResult(ok=False, error=str(exc))

    ok, err = _attach_protective(
        signal,
        symbol=symbol,
        side=side,
        qty=qty,
        actual_entry=actual_entry,
        tick_size=tick_size,
        mark_px=mark_px,
        position_side=position_side,
        source=source,
        play=play,
        entry_type="MARKET",
    )
    if not ok:
        return MarketEntryResult(ok=False, error=err)

    return MarketEntryResult(
        ok=True,
        qty=qty,
        entry_price=actual_entry,
        entry_order_id=entry_order_id,
        position_side=position_side,
    )
