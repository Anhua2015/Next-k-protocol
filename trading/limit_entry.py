"""LIMIT 限价单入场。"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from dataclasses import dataclass

logger = logging.getLogger("trading.limit_entry")


@dataclass
class LimitEntryResult:
    ok: bool
    position_id: int = 0
    error: str = ""


def open_limit(
    signal: Dict[str, Any],
    symbol: str,
    side: str,
    margin: float,
    leverage: int,
    step_size: str,
    tick_size: str,
    min_notional: float,
    hedge: bool,
    source: str,
    play: str,
) -> LimitEntryResult:
    """执行 LIMIT 限价单入场，写入 pending_entry。"""
    from binance.time_sync import now_utc as _now_utc
    from binance.exchange_info import round_price as _round_price, round_quantity as _round_quantity
    from db import (
        compute_pending_deadline, get_config, get_source_config,
        insert_pending_position, update_signal_execution, update_signal_status,
    )
    from trader import place_order

    signal_log_id = signal["signal_log_id"]
    order_side = "BUY" if side == "LONG" else "SELL"
    position_side = side if hedge else None

    signal_entry = signal.get("entry_price")
    if signal_entry is None or float(signal_entry) <= 0:
        logger.error("LIMIT entry %s %s: signal missing entry_price", side, symbol)
        update_signal_status(signal_log_id, "error", "limit needs entry_price")
        return LimitEntryResult(ok=False, error="missing entry_price")

    sl_price = None
    tp_price = None
    try:
        sl_price = float(signal["sl_price"]) if signal.get("sl_price") is not None else None
        tp_price = float(signal["tp_price"]) if signal.get("tp_price") is not None else None
    except (TypeError, ValueError):
        pass

    try:
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
        position_id = insert_pending_position(
            signal_log_id=signal_log_id, symbol=symbol, side=side,
            entry_order_id=entry_order_id, entry_price=limit_price,
            sl_price=sl_price, tp_price=tp_price,
            quantity=qty, notional_usdt=signal.get("notional_usdt") or margin, leverage=leverage,
            opened_at=_now_utc(), entry_deadline=deadline,
            play=play, source=source,
            profile_id=signal.get("profile_id"), client_ref=signal.get("client_ref") or "",
        )
        update_signal_execution(
            signal_log_id,
            status="pending_entry",
            position_id=position_id,
            result={
                "ok": True,
                "position_id": position_id,
                "entry_order_id": entry_order_id,
                "quantity": qty,
                "entry_price": limit_price,
                "pending_entry": True,
                "entry_deadline": deadline,
            },
        )
        from observability.metrics import TRADES_OPENED
        TRADES_OPENED.labels(source=source, side=side, entry_type="LIMIT").inc()
        logger.info("LIMIT placed: %s %s qty=%s price=%.6f order=%s",
                    side, symbol, qty, limit_price, entry_order_id)
        return LimitEntryResult(ok=True, position_id=position_id)
    except Exception as exc:
        logger.error("LIMIT entry %s %s failed: %s", side, symbol, exc)
        update_signal_status(signal_log_id, "error", f"LIMIT entry: {exc}")
        return LimitEntryResult(ok=False, error=str(exc))
