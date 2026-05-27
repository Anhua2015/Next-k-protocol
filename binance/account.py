"""币安账户相关操作：杠杆、保证金、持仓查询、hedge 模式检测。"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

import httpx

from binance.client import BinanceClient

logger = logging.getLogger("binance.account")

_hedge_mode_cache: Optional[bool] = None
_hedge_mode_lock = threading.Lock()


def detect_hedge_mode(client: BinanceClient) -> bool:
    global _hedge_mode_cache
    with _hedge_mode_lock:
        if _hedge_mode_cache is not None:
            return _hedge_mode_cache
    try:
        data = client.request("GET", "/fapi/v1/positionSide/dual")
        with _hedge_mode_lock:
            _hedge_mode_cache = bool(data.get("dualSidePosition"))
            return _hedge_mode_cache
    except Exception as exc:
        logger.warning("hedge-mode detect failed (assume one-way): %s", exc)
        with _hedge_mode_lock:
            _hedge_mode_cache = False
            return False


def set_leverage(client: BinanceClient, symbol: str, leverage: int) -> None:
    try:
        client.request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
    except httpx.HTTPStatusError as e:
        try:
            if e.response.json().get("code") == -4028:
                return
        except Exception:
            pass
        raise


def set_margin_type(client: BinanceClient, symbol: str) -> None:
    try:
        client.request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
    except httpx.HTTPStatusError as e:
        if "No need to change margin type" in (e.response.text or ""):
            return
        raise


def get_live_position(client: BinanceClient, symbol: str) -> Optional[Dict[str, Any]]:
    rows = client.request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    for r in rows:
        if r["symbol"] == symbol and float(r["positionAmt"]) != 0:
            return r
    return None


def get_order(client: BinanceClient, symbol: str, order_id: str) -> Dict[str, Any]:
    return client.request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
