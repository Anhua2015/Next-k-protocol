"""SL/TP 条件单下单 + 紧急平仓。

所有函数通过 client 参数注入 BinanceClient。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from binance.client import BinanceClient
from binance.orders import cancel_all_orders, place_algo_order, place_order

logger = logging.getLogger("trading.protective")


def build_protective_params(
    symbol: str, close_side: str, stop_price: float, qty: float,
    position_side: Optional[str], kind: str,
) -> Dict[str, Any]:
    """构建只减仓的条件单参数。

    单向模式使用 ``reduceOnly``；双向持仓模式必须指定 ``positionSide``，此时 Binance
    不接受 reduceOnly，因此两者互斥。
    """
    order_type = "STOP_MARKET" if kind == "SL" else "TAKE_PROFIT_MARKET"
    params: Dict[str, Any] = {
        "algoType": "CONDITIONAL", "symbol": symbol, "side": close_side,
        "type": order_type, "triggerPrice": stop_price,
        "workingType": "MARK_PRICE", "quantity": qty, "priceProtect": "false",
    }
    if position_side:
        params["positionSide"] = position_side
    else:
        params["reduceOnly"] = "true"
    return params


def place_sl(
    client: BinanceClient, symbol: str, close_side: str,
    stop_price: float, qty: float, position_side: Optional[str],
) -> Dict[str, Any]:
    params = build_protective_params(symbol, close_side, stop_price, qty, position_side, "SL")
    return place_algo_order(client, params)


def place_tp(
    client: BinanceClient, symbol: str, close_side: str,
    stop_price: float, qty: float, position_side: Optional[str],
) -> Dict[str, Any]:
    params = build_protective_params(symbol, close_side, stop_price, qty, position_side, "TP")
    return place_algo_order(client, params)


def place_sl_tp(
    client: BinanceClient, symbol: str, close_side: str,
    sl_price: Optional[float], tp_price: Optional[float],
    qty: float, position_side: Optional[str],
) -> tuple:
    """下单 SL 和/或 TP 条件单。返回 (sl_order_id, tp_order_id)。"""
    sl_id = ""
    tp_id = ""
    if sl_price is not None:
        r = place_sl(client, symbol, close_side, sl_price, qty, position_side)
        sl_id = str(r.get("algoId", "") or r.get("orderId", ""))
        logger.info("SL placed: %s %s sl=%.6f algoId=%s", "protect", symbol, sl_price, sl_id)
    if tp_price is not None:
        r = place_tp(client, symbol, close_side, tp_price, qty, position_side)
        tp_id = str(r.get("algoId", "") or r.get("orderId", ""))
        logger.info("TP placed: %s %s tp=%.6f algoId=%s", "protect", symbol, tp_price, tp_id)
    return sl_id, tp_id


def emergency_close(
    client: BinanceClient, symbol: str, side: str,
    qty: float, position_side: Optional[str],
) -> Optional[str]:
    """保护单失败后的最后安全网。

    如果本函数也失败，日志会明确写 ``POSITION IS NAKED``，这是需要立即人工处理的
    最高优先级故障。
    """
    close_side = "SELL" if side == "LONG" else "BUY"
    params: Dict[str, Any] = {
        "symbol": symbol, "side": close_side, "type": "MARKET",
        "quantity": qty, "reduceOnly": "true",
    }
    if position_side:
        params["positionSide"] = position_side
        params.pop("reduceOnly", None)
    try:
        resp = place_order(client, params)
        oid = str(resp.get("orderId", ""))
        logger.error("EMERGENCY close %s %s qty=%s order=%s", side, symbol, qty, oid)
        return oid
    except Exception as exc:
        logger.critical(
            "EMERGENCY close FAILED %s %s qty=%s — POSITION IS NAKED: %s",
            side, symbol, qty, exc,
        )
        return None


def validate_sl_distance(
    side: str, sl_price: float, mark_px: float, tick: str,
) -> None:
    """验证 SL 距离 mark price 不能过近。"""
    try:
        tick_f = float(tick)
    except ValueError:
        tick_f = 0.0
    margin = max(tick_f * 2.0, mark_px * 0.0005)
    if side == "LONG" and sl_price >= mark_px - margin:
        raise ValueError(
            f"SL {sl_price} too close to mark {mark_px} (need <= {mark_px - margin:.6f})")
    if side == "SHORT" and sl_price <= mark_px + margin:
        raise ValueError(
            f"SL {sl_price} too close to mark {mark_px} (need >= {mark_px + margin:.6f})")


def validate_tp_distance(
    side: str, tp_price: float, mark_px: float, tick: str,
) -> None:
    """验证 TP 距离 mark price 不能过近。"""
    try:
        tick_f = float(tick)
    except ValueError:
        tick_f = 0.0
    margin = max(tick_f * 2.0, mark_px * 0.0005)
    if side == "LONG" and tp_price <= mark_px + margin:
        raise ValueError(
            f"TP {tp_price} too close to mark {mark_px} (need >= {mark_px + margin:.6f})")
    if side == "SHORT" and tp_price >= mark_px - margin:
        raise ValueError(
            f"TP {tp_price} too close to mark {mark_px} (need <= {mark_px - margin:.6f})")
