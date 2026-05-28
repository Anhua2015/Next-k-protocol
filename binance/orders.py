"""币安订单相关：下单、条件单、撤单、查询。"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from binance.client import BinanceClient

logger = logging.getLogger("binance.orders")


def place_order(client: BinanceClient, params: Dict[str, Any]) -> Dict[str, Any]:
    return client.request("POST", "/fapi/v1/order", params)


def place_algo_order(client: BinanceClient, params: Dict[str, Any]) -> Dict[str, Any]:
    return client.request("POST", "/fapi/v1/algoOrder", params)


def get_algo_order(client: BinanceClient, algo_id: str) -> Dict[str, Any]:
    return client.request("GET", "/fapi/v1/algoOrder", {"algoId": algo_id})


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
