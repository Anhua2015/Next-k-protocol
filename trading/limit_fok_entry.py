"""LIMIT FOK 入场及初始保护单事务。

相比 MARKET，LIMIT FOK 能把最大可接受入场价写进交易所订单：

- LONG 只允许在 ``entry_price + max_slippage`` 内买入；
- SHORT 只允许在 ``entry_price - max_slippage`` 内卖出；
- FOK 未完全成交会直接过期，不留下挂单。

成交后沿用 MARKET 入场相同的保护单事务：SL/TP 失败则紧急平仓。
"""
from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

logger = logging.getLogger("trading.limit_fok_entry")


@dataclass
class LimitFokEntryResult:
    ok: bool
    position_id: int = 0
    qty: float = 0.0
    entry_price: float = 0.0
    entry_order_id: str = ""
    position_side: str | None = None
    error: str = ""


def _tick_precision(tick_size: str) -> int:
    s = str(tick_size)
    if "." not in s:
        return 0
    return len(s.rstrip("0").split(".")[-1])


def _price_to_tick(price: float, tick_size: str, *, side: str) -> float:
    """按保护方向取整。

    BUY 是最高可接受价，向下取整避免超过滑点上限；SELL 是最低可接受价，向上取整
    避免低于滑点下限。
    """
    p = Decimal(str(price))
    tick = Decimal(str(tick_size))
    rounding = ROUND_FLOOR if side == "BUY" else ROUND_CEILING
    units = (p / tick).to_integral_value(rounding=rounding)
    rounded = units * tick
    return float(f"{rounded:.{_tick_precision(tick_size)}f}")


def _entry_slippage_bps(side: str, signal_entry: float, actual_entry: float) -> float:
    if signal_entry <= 0 or actual_entry <= 0:
        return 0.0
    if side == "LONG":
        return (actual_entry - signal_entry) / signal_entry * 10_000
    return (signal_entry - actual_entry) / signal_entry * 10_000


def _spread_bps(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2
    if bid <= 0 or ask <= 0 or mid <= 0:
        return 0.0
    return (ask - bid) / mid * 10_000


def open_limit_fok(
    signal: dict[str, Any],
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
    *,
    max_slippage_bps: float,
    max_spread_bps: float,
) -> LimitFokEntryResult:
    """执行带滑点上限的 LIMIT FOK 开仓、确认成交并创建保护单。"""
    from binance.exchange_info import (
        round_price as _round_price,
    )
    from binance.exchange_info import (
        round_quantity as _round_quantity,
    )
    from db import update_signal_execution, update_signal_status
    from trader import (
        _emergency_close,
        _place_protective,
        _validate_sl_distance,
        cancel_all_orders,
        get_book_ticker,
        get_order,
        place_order,
    )

    signal_log_id = signal["signal_log_id"]
    order_side = "BUY" if side == "LONG" else "SELL"
    position_side = side if hedge else None

    signal_entry_raw = signal.get("entry_price")
    if signal_entry_raw is None or float(signal_entry_raw) <= 0:
        msg = "LIMIT_FOK needs entry_price"
        logger.error("%s %s %s", msg, side, symbol)
        update_signal_status(signal_log_id, "error", msg)
        return LimitFokEntryResult(ok=False, error=msg)
    signal_entry = float(signal_entry_raw)
    max_slip = max(0.0, float(max_slippage_bps or 0.0))

    sl_price = None
    tp_price = None
    try:
        sl_price = float(signal["sl_price"]) if signal.get("sl_price") is not None else None
        tp_price = float(signal["tp_price"]) if signal.get("tp_price") is not None else None
    except (TypeError, ValueError):
        pass

    if side == "LONG":
        limit_raw = signal_entry * (1 + max_slip / 10_000)
    else:
        limit_raw = signal_entry * (1 - max_slip / 10_000)
    limit_price = _price_to_tick(limit_raw, tick_size, side=order_side)

    qty: float = 0.0
    actual_entry: float = 0.0
    entry_order_id = ""

    try:
        raw_qty = margin * leverage / limit_price
        qty = _round_quantity(raw_qty, step_size)
        if qty <= 0:
            raise ValueError(f"computed qty={qty}")
        if qty * limit_price < min_notional:
            raise ValueError(f"notional {qty * limit_price:.2f} < min {min_notional}")

        book = get_book_ticker(symbol)
        bid = float(book["bid_price"])
        ask = float(book["ask_price"])
        spread = _spread_bps(bid, ask)
        if max_spread_bps > 0 and spread > float(max_spread_bps):
            raise ValueError(
                f"spread_guard: spread={spread:.2f}bps > max={float(max_spread_bps):.2f}bps "
                f"bid={bid} ask={ask}"
            )
        if side == "LONG" and ask > limit_price:
            raise ValueError(
                f"slippage_guard: ask={ask} > limit={limit_price} "
                f"entry={signal_entry} max={max_slip:.2f}bps"
            )
        if side == "SHORT" and bid < limit_price:
            raise ValueError(
                f"slippage_guard: bid={bid} < limit={limit_price} "
                f"entry={signal_entry} max={max_slip:.2f}bps"
            )

        entry_params: dict[str, Any] = {
            "symbol": symbol,
            "side": order_side,
            "type": "LIMIT",
            "timeInForce": "FOK",
            "quantity": qty,
            "price": limit_price,
            "newOrderRespType": "RESULT",
        }
        if position_side:
            entry_params["positionSide"] = position_side
        entry_resp = place_order(entry_params)
        entry_order_id = str(entry_resp.get("orderId", ""))
        status = str(entry_resp.get("status") or "").upper()
        executed_qty = float(entry_resp.get("executedQty") or 0)
        actual_entry = float(entry_resp.get("avgPrice") or 0)
        if status and status != "FILLED":
            raise ValueError(f"LIMIT_FOK not filled: status={status} executedQty={executed_qty}")
        if actual_entry <= 0 and entry_order_id:
            try:
                detail = get_order(symbol, entry_order_id)
                status = str(detail.get("status") or status).upper()
                executed_qty = float(detail.get("executedQty") or executed_qty)
                actual_entry = float(detail.get("avgPrice") or 0)
            except Exception as exc:
                logger.warning("get_order after LIMIT_FOK entry %s: %s", symbol, exc)
        if executed_qty <= 0:
            raise ValueError(f"LIMIT_FOK not filled: executedQty={executed_qty}")
        if actual_entry <= 0:
            actual_entry = limit_price

        actual_slip = _entry_slippage_bps(side, signal_entry, actual_entry)
        if actual_slip > max_slip + 0.01:
            # 理论上 LIMIT 不应越过价格上限；如果交易所返回异常成交价，立即撤离。
            logger.error(
                "LIMIT_FOK filled beyond guard %s %s actual_slippage=%.2fbps max=%.2fbps",
                side,
                symbol,
                actual_slip,
                max_slip,
            )
            _emergency_close(symbol, side, qty, position_side)
            update_signal_status(signal_log_id, "error", f"filled beyond slippage guard: {actual_slip:.2f}bps")
            return LimitFokEntryResult(ok=False, error="filled beyond slippage guard")

        logger.info(
            "LIMIT_FOK filled: %s %s qty=%s entry=%.6f limit=%.6f slippage=%.2fbps order=%s",
            side,
            symbol,
            qty,
            actual_entry,
            limit_price,
            actual_slip,
            entry_order_id,
        )
    except Exception as exc:
        logger.error("LIMIT_FOK entry %s %s failed: %s", side, symbol, exc)
        update_signal_status(signal_log_id, "error", f"LIMIT_FOK entry: {exc}")
        return LimitFokEntryResult(ok=False, error=str(exc))

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
        with suppress(Exception):
            cancel_all_orders(symbol)
        _emergency_close(symbol, side, qty, position_side)
        update_signal_status(signal_log_id, "error", f"SL/TP failed: {exc}")
        return LimitFokEntryResult(ok=False, error=str(exc))

    update_signal_execution(
        signal_log_id,
        status="traded",
        result={
            "ok": True,
            "entry_order_id": entry_order_id,
            "quantity": qty,
            "entry_price": actual_entry,
            "signal_entry_price": signal_entry,
            "entry_limit_price": limit_price,
            "entry_slippage_bps": _entry_slippage_bps(side, signal_entry, actual_entry),
            "entry_type": "LIMIT_FOK",
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
            "notional_usdt": margin * leverage,
        },
    )
    from observability.metrics import TRADES_OPENED
    TRADES_OPENED.labels(source=source, side=side, entry_type="LIMIT_FOK").inc()
    sl_log = f"{final_sl_p:.6f}" if final_sl_p is not None else "-"
    tp_log = f"{final_tp_p:.6f}" if final_tp_p is not None else "-"
    logger.info(
        "Opened %s %s source=%s qty=%s entry=%.6f sl=%s tp=%s entry_type=LIMIT_FOK",
        side, symbol, source, qty, actual_entry, sl_log, tp_log,
    )
    return LimitFokEntryResult(ok=True, qty=qty, entry_price=actual_entry,
                               entry_order_id=entry_order_id, position_side=position_side)
