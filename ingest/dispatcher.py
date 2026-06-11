"""信号分发：调用 trader.execute_trade 执行开仓。"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger("ingest.dispatcher")


def dispatch(sig: Any, signal_log_id: int) -> Dict[str, Any]:
    """执行交易并返回 detail 字典。"""
    from trader import execute_trade

    symbol = sig.symbol
    side = sig.side
    source = sig.source

    detail = {
        "api_signal_id": sig.api_signal_id,
        "symbol": symbol, "side": side, "source": source,
    }

    action = (getattr(sig, "action", None) or "").lower()
    if not action and "rolling" in (sig.play or "").lower():
        action = "rolling"
    if not action:
        action = "open"
    try:
        signal_dict = {
            "signal_log_id": signal_log_id,
            "symbol": symbol,
            "side": side,
            "source": source,
            "margin_usdt": (
                float(sig.margin_usdt) if sig.margin_usdt is not None else None
            ),
            "leverage": (
                float(sig.leverage) if getattr(sig, "leverage", None) is not None else None
            ),
            "entry_price": float(sig.entry_price) if sig.entry_price is not None else None,
            "sl_price": float(sig.sl_price) if sig.sl_price is not None else None,
            "tp_price": float(sig.tp_price) if sig.tp_price is not None else None,
            "close_price": (
                float(sig.close_price) if sig.close_price is not None else None
            ),
            "play": sig.play or "",
            "profile_id": sig.profile_id,
            "client_ref": sig.client_ref or "",
            "action": action,
        }
        ok = execute_trade(signal_dict)
        detail["action"] = "traded" if ok else "error"
        if not ok:
            detail["error"] = "execute_trade returned False"
        return detail
    except Exception as exc:
        logger.error("dispatch execute_trade %s %s: %s", side, symbol, exc)
        import db as _d
        _d.update_signal_status(signal_log_id, "error", str(exc))
        detail["action"] = "error"
        detail["error"] = str(exc)
        return detail
