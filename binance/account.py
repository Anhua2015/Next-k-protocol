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
        try:
            code = e.response.json().get("code")
            # -4046: already ISOLATED; -4067: open orders block re-set (same effective state)
            if code in (-4046, -4067):
                return
        except Exception:
            pass
        if "No need to change margin type" in (e.response.text or ""):
            return
        raise


def get_live_position(client: BinanceClient, symbol: str) -> Optional[Dict[str, Any]]:
    rows = client.request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    for r in rows:
        if r["symbol"] == symbol and float(r["positionAmt"]) != 0:
            return r
    return None


def list_live_positions(client: BinanceClient) -> list[Dict[str, Any]]:
    rows = client.request("GET", "/fapi/v2/positionRisk")
    positions: list[Dict[str, Any]] = []
    for row in rows:
        amt = float(row.get("positionAmt") or 0)
        if amt == 0:
            continue

        def _opt_float(key: str) -> Optional[float]:
            raw = row.get(key)
            if raw in (None, ""):
                return None
            return float(raw)

        leverage = _opt_float("leverage")
        positions.append(
            {
                "symbol": row["symbol"],
                "side": "LONG" if amt > 0 else "SHORT",
                "quantity": abs(amt),
                "entry_price": _opt_float("entryPrice"),
                "mark_price": _opt_float("markPrice"),
                "unrealized_pnl_usdt": float(
                    row.get("unRealizedProfit")
                    or row.get("unrealizedProfit")
                    or 0
                ),
                "leverage": int(leverage) if leverage is not None else None,
                "liquidation_price": _opt_float("liquidationPrice"),
                "margin_type": (str(row.get("marginType") or "").upper() or None),
            }
        )
    return positions


def get_order(client: BinanceClient, symbol: str, order_id: str) -> Dict[str, Any]:
    return client.request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})


def get_account_summary(client: BinanceClient) -> Dict[str, Any]:
    data = client.request("GET", "/fapi/v2/account")
    assets = data.get("assets") or []
    usdt = None
    for row in assets:
        if str(row.get("asset") or "").upper() == "USDT":
            usdt = row
            break
    if usdt is None:
        raise RuntimeError("USDT asset not found in futures account")
    return {
        "asset": "USDT",
        "wallet_balance_usdt": float(usdt.get("walletBalance") or 0),
        "available_balance_usdt": float(data.get("availableBalance") or usdt.get("availableBalance") or 0),
        "unrealized_pnl_usdt": float(usdt.get("unrealizedProfit") or data.get("totalUnrealizedProfit") or 0),
    }
