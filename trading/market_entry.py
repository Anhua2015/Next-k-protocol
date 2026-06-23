"""MARKET 市价入场及初始保护单事务。

这里把“入场 + SL/TP”视作一个安全事务：入场成功但保护单失败时，立即撤销残余订单并
调用紧急 MARKET 平仓。返回失败意味着调用方不应把本次信号视为已完成交易。
"""
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
    """执行市价开仓、确认成交价、创建保护单并写入审计结果。"""
    from binance.time_sync import now_utc as _now_utc
    from binance.exchange_info import round_price as _round_price, round_quantity as _round_quantity
    from db import update_signal_execution, update_signal_status
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

    # SL/TP 已由策略层计算；这里只做数字解析和交易所 tickSize 取整。
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
        # Protocol 接口使用保证金和杠杆，换算成标的数量后必须按 LOT_SIZE 取整。
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

    # 入场已完成。从此处开始任何异常都可能产生裸仓，必须走 emergency_close。
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

    update_signal_execution(
        signal_log_id,
        status="traded",
        result={
            "ok": True,
            "entry_order_id": entry_order_id,
            "quantity": qty,
            "entry_price": actual_entry,
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
            "notional_usdt": margin * leverage,
        },
    )
    from observability.metrics import TRADES_OPENED
    TRADES_OPENED.labels(source=source, side=side, entry_type="MARKET").inc()
    sl_log = f"{final_sl_p:.6f}" if final_sl_p is not None else "-"
    tp_log = f"{final_tp_p:.6f}" if final_tp_p is not None else "-"
    logger.info(
        "Opened %s %s source=%s qty=%s entry=%.6f sl=%s tp=%s",
        side, symbol, source, qty, actual_entry, sl_log, tp_log,
    )
    return MarketEntryResult(ok=True, qty=qty, entry_price=actual_entry,
                             entry_order_id=entry_order_id, position_side=position_side)
