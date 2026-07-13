"""币安订单相关：下单、条件单、撤单、查询。"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from binance.client import BinanceClient

logger = logging.getLogger("binance.orders")

_ALGO_TERMINAL_STATUSES = frozenset(
    {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "FAILED", "FINISHED"}
)


def place_order(client: BinanceClient, params: Dict[str, Any]) -> Dict[str, Any]:
    return client.request("POST", "/fapi/v1/order", params)


def place_algo_order(client: BinanceClient, params: Dict[str, Any]) -> Dict[str, Any]:
    return client.request("POST", "/fapi/v1/algoOrder", params)


def build_entry_stop_limit_algo_params(
    *,
    symbol: str,
    side: str,
    qty: float,
    trigger_price: float,
    limit_price: float,
    position_side: Optional[str] = None,
    working_type: str = "CONTRACT_PRICE",
) -> Dict[str, Any]:
    """STOP (stop-limit) entry via Algo Service — required since 2025-12-09 (-4120).

    Uses CONTRACT_PRICE trigger (last contract trade) for OR breakout entries.
    SL/TP protective orders use MARK_PRICE separately — intentional asymmetry.
    """
    params: Dict[str, Any] = {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": side,
        "type": "STOP",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": limit_price,
        "triggerPrice": trigger_price,
        "workingType": working_type,
        "newOrderRespType": "RESULT",
    }
    if position_side:
        params["positionSide"] = position_side
    return params


def _fallback_entry_avg_price(algo: Dict[str, Any], actual_price: float) -> float:
    if actual_price > 0:
        return actual_price
    for key in ("avgPrice", "price", "triggerPrice"):
        try:
            v = float(algo.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return 0.0


def normalize_algo_entry_order(algo: Dict[str, Any]) -> Dict[str, Any]:
    """Map algo order payload to regular order shape for entry reconcile."""
    status = str(algo.get("algoStatus") or algo.get("status") or "").upper()
    try:
        actual_qty = float(algo.get("actualQty") or 0)
    except (TypeError, ValueError):
        actual_qty = 0.0
    try:
        raw_actual_price = float(algo.get("actualPrice") or 0)
    except (TypeError, ValueError):
        raw_actual_price = 0.0
    actual_price = _fallback_entry_avg_price(algo, raw_actual_price)

    order_id = algo.get("actualOrderId") or algo.get("algoId") or algo.get("orderId")

    if actual_qty > 0:
        return {
            "status": "FILLED",
            "executedQty": actual_qty,
            "avgPrice": actual_price,
            "orderId": order_id,
        }

    if status in _ALGO_TERMINAL_STATUSES:
        return {"status": "CANCELED", "executedQty": 0, "avgPrice": 0, "orderId": order_id}

    return {"status": "NEW", "executedQty": 0, "avgPrice": 0, "orderId": order_id}


def get_algo_order(
    client: BinanceClient,
    algo_id: str,
    *,
    retries: int = 3,
    retry_delay_sec: float = 0.25,
) -> Dict[str, Any]:
    """Fetch algo order; retry -2013 (propagation delay right after place)."""
    last_exc: Optional[Exception] = None
    for attempt in range(max(1, retries)):
        try:
            return client.request("GET", "/fapi/v1/algoOrder", {"algoId": algo_id})
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            code = None
            try:
                code = exc.response.json().get("code")
            except Exception:
                pass
            if code == -2013 and attempt < retries - 1:
                time.sleep(retry_delay_sec)
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("get_algo_order: unreachable")


def cancel_algo_order(client: BinanceClient, algo_id: str) -> bool:
    try:
        client.request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
        return True
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if 400 <= code < 500:
            logger.info("cancel_algo_order %s: already gone (%s)", algo_id, code)
            return True
        logger.error("cancel_algo_order %s: http %s", algo_id, code)
        return False
    except Exception as exc:
        logger.error("cancel_algo_order %s: %s", algo_id, exc)
        return False


def cancel_order_by_id(client: BinanceClient, symbol: str, order_id: str) -> bool:
    try:
        client.request("DELETE", "/fapi/v1/order",
                       {"symbol": symbol, "orderId": order_id})
        return True
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if 400 <= code < 500:
            logger.info("cancel_order_by_id %s %s: already gone (%s)", symbol, order_id, code)
            return True
        logger.error("cancel_order_by_id %s %s: http %s", symbol, order_id, code)
        return False
    except Exception as exc:
        logger.error("cancel_order_by_id %s %s: %s", symbol, order_id, exc)
        return False


def get_open_algo_orders(client: BinanceClient, symbol: str) -> List[Any]:
    try:
        data = client.request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})
        if isinstance(data, list):
            return data
        return data.get("orders", []) if isinstance(data, dict) else []
    except Exception as exc:
        logger.warning("get_open_algo_orders %s: %s", symbol, exc)
        return []


def cancel_all_orders(
    client: BinanceClient, symbol: str,
    pos: Optional[Dict[str, Any]] = None,
) -> bool:
    try:
        client.request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    except Exception as exc:
        logger.warning("cancel_all_orders (regular) %s: %s", symbol, exc)

    algo_ok = True
    handled: set = set()

    if pos:
        for key in ("sl_order_id", "tp_order_id"):
            aid = pos.get(key)
            if not aid:
                continue
            aid = str(aid)
            if aid in handled:
                continue
            handled.add(aid)
            ok = cancel_algo_order(client, aid)
            if not ok:
                algo_ok = False

    try:
        for o in get_open_algo_orders(client, symbol):
            aid = o.get("algoId") or o.get("clientAlgoId")
            if not aid:
                continue
            aid = str(aid)
            if aid in handled:
                continue
            handled.add(aid)
            ok = cancel_algo_order(client, aid)
            if not ok:
                algo_ok = False
    except Exception as exc:
        logger.error("cancel_all_orders (algo list) %s: %s", symbol, exc)
        algo_ok = False

    return algo_ok
