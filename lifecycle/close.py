"""平仓 + PnL 记录。"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from binance.time_sync import now_utc as _now_utc

logger = logging.getLogger("lifecycle.close")


def _close_event_action(close_reason: str) -> str:
    return {
        "tp": "exchange_tp",
        "sl": "exchange_sl",
        "external": "external_close",
        "paper_close": "external_close",
        "manual": "external_close",
    }.get(close_reason, "external_close")


def _log_closed_position_event(
    pos: Dict[str, Any],
    close_reason: str,
    close_price: Optional[float],
    pnl_usdt: float,
    pnl_pct: float,
) -> None:
    from db import log_trade_event

    action = _close_event_action(close_reason)
    log_trade_event(
        source=pos.get("source") or "",
        action=action,
        symbol=pos.get("symbol") or "",
        side=pos.get("side") or "",
        api_signal_id=f"position_{pos['id']}_{action}",
        status="closed",
        skip_reason=close_reason,
        profile_id=pos.get("profile_id"),
        position_id=pos.get("id"),
        client_ref=pos.get("client_ref") or "",
        payload={"close_reason": close_reason},
        result={
            "close_price": close_price,
            "pnl_usdt": pnl_usdt,
            "pnl_pct": pnl_pct,
            "skip_reason": close_reason,
        },
    )


def _record_closed_position(
    pos: Dict[str, Any], close_reason: str, close_price: Optional[float],
) -> None:
    from db import update_position_closed, update_signal_status
    entry = pos.get("entry_price")
    qty = pos.get("quantity")
    lev = pos.get("leverage") or 1
    side = pos.get("side")

    if entry is None or qty is None or close_price is None or entry <= 0:
        logger.warning("record_closed pos=%s incomplete data; pnl=0", pos["id"])
        update_position_closed(
            position_id=pos["id"], close_reason=close_reason,
            close_price=close_price or 0.0, closed_at=_now_utc(),
            pnl_usdt=0.0, pnl_pct=0.0,
        )
        _log_closed_position_event(pos, close_reason, close_price or 0.0, 0.0, 0.0)
        return

    if side == "LONG":
        pnl = qty * (close_price - entry)
        ret = close_price / entry - 1.0
    else:
        pnl = qty * (entry - close_price)
        ret = (entry - close_price) / entry if entry > 0 else 0.0

    pnl_pct = ret * lev * 100.0
    update_position_closed(
        position_id=pos["id"], close_reason=close_reason,
        close_price=close_price, closed_at=_now_utc(),
        pnl_usdt=round(pnl, 4), pnl_pct=round(pnl_pct, 4),
    )
    _log_closed_position_event(
        pos, close_reason, close_price, round(pnl, 4), round(pnl_pct, 4),
    )
    signal_log_id = pos.get("signal_log_id")
    if signal_log_id:
        update_signal_status(signal_log_id, "closed", close_reason)
    logger.info("Closed %s %s reason=%s close=%.6f pnl=%.4f pct=%.2f%%",
                side, pos.get("symbol"), close_reason, close_price, pnl, pnl_pct)
    from observability.metrics import POSITIONS_CLOSED
    POSITIONS_CLOSED.labels(
        source=pos.get("source", ""), close_reason=close_reason).inc()


def _get_latest_open_for_symbol_source(
    symbol: str, source: str,
) -> Optional[Dict[str, Any]]:
    """查询指定 symbol+source 的最新 open 持仓（ORDER BY id DESC）。"""
    from repos.connection import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM positions "
            "WHERE status IN ('open','pending_entry') AND symbol=? AND source=? "
            "ORDER BY id DESC LIMIT 1",
            (symbol, source),
        ).fetchone()
    return dict(row) if row else None


def close_position(
    source: str, symbol: str, side: str,
    exit_rule: str, close_price: Optional[float] = None,
    position_id: Optional[int] = None,
) -> bool:
    """平仓：取消 SL/TP 条件单 + MARKET 平仓 + 记录 PnL。"""
    from db import (
        cancel_pending_position, get_open_position_for_symbol,
        get_position_by_id, update_signal_status,
    )
    from trader import (
        _detect_hedge_mode,
        cancel_algo_order,
        cancel_all_orders,
        cancel_order_by_id,
        place_order,
    )

    logger.info("close_position: source=%s symbol=%s side=%s rule=%s close=%s pos_id=%s",
                source, symbol, side, exit_rule, close_price, position_id)

    if position_id:
        pos = get_position_by_id(position_id)
    elif source == "moss_quant":
        # moss_quant 同 symbol 可有多仓（滚仓），取最新 open 持仓
        pos = _get_latest_open_for_symbol_source(symbol, source)
    else:
        pos = get_open_position_for_symbol(symbol)
    if pos is None:
        logger.warning("close_position %s %s: no open position", side, symbol)
        return False

    if pos.get("status") == "pending_entry":
        entry_oid = pos.get("entry_order_id")
        logger.info("close_position %s %s: pending_entry, cancelling limit order=%s",
                    side, symbol, entry_oid)
        if entry_oid:
            cancel_order_by_id(symbol, str(entry_oid))
        cancel_pending_position(pos["id"], reason=exit_rule)
        sl_id = pos.get("signal_log_id")
        if sl_id:
            update_signal_status(int(sl_id), "closed", "paper_close_pending")
        return True

    if side.upper() != pos["side"].upper():
        logger.warning("close_position side mismatch: req=%s db=%s", side, pos["side"])

    qty = pos.get("quantity")
    if not qty:
        logger.warning("close_position pos=%s has no quantity", pos["id"])
        return False

    hedge = _detect_hedge_mode()
    position_side = (pos["side"] if hedge else None)
    close_side2 = "SELL" if pos["side"] == "LONG" else "BUY"

    actual_close: Optional[float] = None
    market_ok = False
    try:
        params: Dict[str, Any] = {
            "symbol": symbol, "side": close_side2, "type": "MARKET",
            "quantity": qty, "reduceOnly": "true",
        }
        if position_side:
            params["positionSide"] = position_side
            params.pop("reduceOnly", None)
        resp = place_order(params)
        avg = resp.get("avgPrice")
        if avg and float(avg) > 0:
            actual_close = float(avg)
            market_ok = True
        logger.info("close_position %s %s: MARKET filled qty=%s price=%s",
                    side, symbol, qty, actual_close)
    except Exception as exc:
        logger.error("close_position MARKET failed %s %s: %s", side, symbol, exc)

    if not market_ok:
        logger.critical("close_position ABORTED %s %s: MARKET order failed", side, symbol)
        return False

    try:
        # moss_quant / 指定 position_id：精确取消，不误伤同 symbol 其他仓
        if position_id or source == "moss_quant":
            sl_id = pos.get("sl_order_id")
            tp_id = pos.get("tp_order_id")
            for aid in (sl_id, tp_id):
                if aid:
                    try:
                        cancel_algo_order(str(aid))
                    except Exception as exc:
                        logger.warning("close_position cancel algo %s failed: %s", aid, exc)
        else:
            cancel_all_orders(symbol, pos)
    except Exception as exc:
        logger.error("close_position cancel orders %s %s: %s", side, symbol, exc)

    _record_closed_position(pos, exit_rule, actual_close)
    logger.info("close_position done: %s %s source=%s rule=%s close=%.6f",
                side, symbol, source, exit_rule, actual_close or 0)
    return True
