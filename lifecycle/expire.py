"""过期强平：定期检查并 MARKET 平仓过期持仓。"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("lifecycle.expire")


def expire_open_positions() -> None:
    from db import get_config, get_open_expired_positions
    from lifecycle.close import _record_closed_position
    from trader import (
        _detect_hedge_mode,
        cancel_all_orders,
        get_mark_price,
        place_order,
    )

    if not get_config("binance_api_key", ""):
        return
    if get_config("enabled", "false").lower() != "true":
        return

    expired = get_open_expired_positions()
    if not expired:
        return

    for pos in expired:
        symbol = pos["symbol"]
        side = pos["side"]
        qty = pos.get("quantity")
        if not qty:
            continue

        logger.info("Expiring pos id=%s %s %s", pos["id"], side, symbol)
        hedge = _detect_hedge_mode()
        position_side = (side if hedge else None)
        close_side = "SELL" if side == "LONG" else "BUY"
        close_price: Optional[float] = None
        market_ok = False

        try:
            cancel_all_orders(symbol, pos)
        except Exception as exc:
            logger.warning("expire cancel_all_orders %s: %s", symbol, exc)
        try:
            params: Dict[str, Any] = {
                "symbol": symbol, "side": close_side, "type": "MARKET",
                "quantity": qty, "reduceOnly": "true",
            }
            if position_side:
                params["positionSide"] = position_side
                params.pop("reduceOnly", None)
            resp = place_order(params)
            avg = resp.get("avgPrice")
            if avg and float(avg) > 0:
                close_price = float(avg)
            market_ok = True
        except Exception as exc:
            logger.critical("expire close FAILED pos=%s %s: %s", pos["id"], symbol, exc)

        if not market_ok:
            continue

        if close_price is None:
            try:
                close_price = get_mark_price(symbol)
            except Exception:
                close_price = 0.0

        _record_closed_position(pos, "expired", close_price)
