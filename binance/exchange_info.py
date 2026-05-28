"""币安 exchangeInfo 缓存 + 交易对过滤器查询。

缓存 TTL 5min；提供 mark price、filters、精度取整工具函数。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

from binance.client import BinanceClient

logger = logging.getLogger("binance.exchange_info")

EXCHANGE_INFO_TTL_SEC = 300

_exch_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_exch_cache_lock = threading.Lock()


def _get_exchange_info(client: BinanceClient) -> Dict[str, Any]:
    base = client._base_url()
    with _exch_cache_lock:
        entry = _exch_cache.get(base)
        if entry and (time.time() - entry[0]) < EXCHANGE_INFO_TTL_SEC:
            return entry[1]
    data = client.request("GET", "/fapi/v1/exchangeInfo", signed=False)
    with _exch_cache_lock:
        _exch_cache[base] = (time.time(), data)
    return data


def get_symbol_info(client: BinanceClient, symbol: str) -> Dict[str, Any]:
    data = _get_exchange_info(client)
    for s in data.get("symbols", []):
        if s["symbol"] == symbol:
            return s
    raise ValueError(f"Symbol {symbol} not found in exchangeInfo")


def get_mark_price(client: BinanceClient, symbol: str) -> float:
    data = client.request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, signed=False)
    return float(data["markPrice"])


def get_filters(client: BinanceClient, symbol: str) -> Tuple[str, str, float]:
    """返回 (stepSize, tickSize, minNotional)。"""
    info = get_symbol_info(client, symbol)
    step_size: Optional[str] = None
    tick_size: Optional[str] = None
    min_notional: Optional[float] = None
    for f in info.get("filters", []):
        ft = f["filterType"]
        if ft == "LOT_SIZE":
            step_size = f["stepSize"]
        elif ft == "PRICE_FILTER":
            tick_size = f["tickSize"]
        elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
            v = f.get("notional") or f.get("minNotional")
            if v:
                try:
                    min_notional = float(v)
                except ValueError:
                    pass
    if step_size is None or tick_size is None:
        raise ValueError(f"exchangeInfo for {symbol} missing LOT_SIZE/PRICE_FILTER")
    if min_notional is None:
        min_notional = 5.0
    return step_size, tick_size, min_notional


def round_quantity(qty: float, step_size: str) -> float:
    precision = len(step_size.rstrip("0").split(".")[-1]) if "." in step_size else 0
    return round(qty, precision)


def round_price(price: float, tick_size: str) -> float:
    precision = len(tick_size.rstrip("0").split(".")[-1]) if "." in tick_size else 0
    return round(price, precision)
