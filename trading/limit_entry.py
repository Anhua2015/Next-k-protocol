"""LIMIT 限价单入场（FVG prox 等）。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("trading.limit_entry")


@dataclass
class LimitEntryResult:
    ok: bool
    submitted: bool = False
    filled_immediately: bool = False
    entry_order_id: str = ""
    entry_price: float = 0.0
    qty: float = 0.0
    position_side: Optional[str] = None
    error: str = ""


def finalize_filled_limit_entry(
    signal_row: Dict[str, Any],
    *,
    order: Dict[str, Any],
    tick_size: str,
    mark_px: float,
    source: str,
) -> bool:
    """pending LIMIT 成交后补 SL/TP。"""
    from trading.stop_limit_entry import finalize_filled_entry

    return finalize_filled_entry(
        signal_row,
        order=order,
        tick_size=tick_size,
        mark_px=mark_px,
        source=source,
    )


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
    mark_px: float,
    source: str,
    play: str,
) -> LimitEntryResult:
    """执行 LIMIT 限价单入场；成交后挂 SL/TP，未成交写入 submitted。"""
    from binance.exchange_info import round_price as _round_price, round_quantity as _round_quantity
    from db import update_signal_execution, update_signal_status
    from trader import get_order, place_order

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
        if sl_price is not None and float(sl_price) > 0:
            from trading.protective import validate_sl_distance

            pre_sl = _round_price(float(sl_price), tick_size)
            try:
                validate_sl_distance(side, pre_sl, mark_px, tick_size)
            except ValueError as exc:
                msg = f"sl_preflight: {exc}"
                logger.info("LIMIT reject %s %s: %s", side, symbol, msg)
                update_signal_status(signal_log_id, "error", msg)
                return LimitEntryResult(ok=False, error=msg)

        raw_qty = margin * leverage / limit_price
        qty = _round_quantity(raw_qty, step_size)
        if qty <= 0:
            raise ValueError(f"computed qty={qty}")
        if qty * limit_price < min_notional:
            raise ValueError(f"notional {qty * limit_price:.2f} < min {min_notional}")

        entry_params: Dict[str, Any] = {
            "symbol": symbol,
            "side": order_side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": qty,
            "price": limit_price,
            "newOrderRespType": "ACK",
        }
        if position_side:
            entry_params["positionSide"] = position_side
        entry_resp = place_order(entry_params)
        entry_order_id = str(entry_resp.get("orderId", ""))
        if not entry_order_id:
            raise ValueError(f"LIMIT response missing orderId: {entry_resp}")

        order = get_order(symbol, entry_order_id)
        status = str(order.get("status") or "").upper()
        actual_entry = float(order.get("avgPrice") or 0)
        exec_qty = float(order.get("executedQty") or 0)

        signal["_entry_order_id"] = entry_order_id
        signal["margin_usdt"] = margin
        signal["leverage"] = leverage

        if status == "FILLED" or (exec_qty > 0 and actual_entry > 0):
            from trading.stop_limit_entry import _attach_protective

            ok, err = _attach_protective(
                signal,
                symbol=symbol,
                side=side,
                qty=exec_qty or qty,
                actual_entry=actual_entry or limit_price,
                tick_size=tick_size,
                mark_px=mark_px,
                position_side=position_side,
                source=source,
                play=play,
                entry_type="LIMIT",
            )
            if not ok:
                return LimitEntryResult(ok=False, error=err)
            return LimitEntryResult(
                ok=True,
                filled_immediately=True,
                entry_order_id=entry_order_id,
                entry_price=actual_entry or limit_price,
                qty=exec_qty or qty,
                position_side=position_side,
            )

        update_signal_execution(
            signal_log_id,
            status="submitted",
            result={
                "ok": True,
                "entry_order_id": entry_order_id,
                "quantity": qty,
                "entry_price": limit_price,
                "entry_type": "LIMIT",
                "entry_is_algo": False,
                "notional_usdt": margin * leverage,
                "margin_usdt": margin,
                "leverage": leverage,
                "side": side,
                "sl_price": sl_price,
                "tp_price": tp_price,
                "or_high": signal.get("or_high"),
                "or_low": signal.get("or_low"),
                "sl_risk_dist": signal.get("sl_risk_dist"),
            },
        )
        from observability.metrics import TRADES_OPENED

        TRADES_OPENED.labels(source=source, side=side, entry_type="LIMIT").inc()
        logger.info(
            "LIMIT submitted: %s %s qty=%s price=%.6f order=%s",
            side,
            symbol,
            qty,
            limit_price,
            entry_order_id,
        )
        return LimitEntryResult(
            ok=True,
            submitted=True,
            entry_order_id=entry_order_id,
            entry_price=limit_price,
            qty=qty,
            position_side=position_side,
        )
    except Exception as exc:
        logger.error("LIMIT entry %s %s failed: %s", side, symbol, exc)
        update_signal_status(signal_log_id, "error", f"LIMIT entry: {exc}")
        return LimitEntryResult(ok=False, error=str(exc))
