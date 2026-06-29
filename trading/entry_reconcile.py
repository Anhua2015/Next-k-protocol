"""Pending STOP 入场单对账：成交 → 补 SL/TP；超时 → 撤单。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

logger = logging.getLogger("trading.entry_reconcile")

_PENDING_TTL_HOURS = 8.0


def _cancel_oco_peer(*, filled_row: Dict[str, Any], result: Dict[str, Any]) -> None:
    peer_api_id = str(result.get("oco_peer_api_id") or "").strip()
    if not peer_api_id:
        return
    from db import get_signal_by_api_id, update_signal_status

    peer = get_signal_by_api_id(str(filled_row.get("source") or ""), peer_api_id)
    if not peer:
        return
    peer_status = str(peer.get("status") or "").lower()
    if peer_status != "submitted":
        return
    try:
        peer_result = json.loads(peer.get("result_json") or "{}")
    except json.JSONDecodeError:
        peer_result = {}
    entry_order_id = str(peer_result.get("entry_order_id") or "")
    symbol = str(peer.get("symbol") or "")
    if entry_order_id and symbol:
        from trader import cancel_algo_order, cancel_order_by_id

        try:
            if peer_result.get("entry_is_algo"):
                cancel_algo_order(entry_order_id)
            else:
                cancel_order_by_id(symbol, entry_order_id)
        except Exception as exc:
            logger.warning("cancel oco peer %s %s: %s", symbol, peer_api_id, exc)
    update_signal_status(int(peer["id"]), "cancelled", "oco_peer_filled")


def reconcile_pending_entry_orders() -> int:
    """处理 status=submitted 的 STOP 入场单。返回 promote 成功数。"""
    from db import list_signals, update_signal_status
    from binance.orders import normalize_algo_entry_order
    from trader import cancel_algo_order, cancel_order_by_id, get_algo_order, get_mark_price, get_order, _get_filters

    rows = list_signals(limit=200, status="submitted", action="open")
    if not rows:
        return 0

    promoted = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            result = json.loads(row.get("result_json") or "{}")
        except json.JSONDecodeError:
            result = {}
        if str(result.get("entry_type") or "").upper() not in ("STOP_LIMIT", "STOP"):
            continue

        entry_order_id = str(result.get("entry_order_id") or "")
        symbol = str(row.get("symbol") or "")
        if not entry_order_id or not symbol:
            continue

        received_raw = str(row.get("received_at") or "")
        try:
            received_at = datetime.fromisoformat(received_raw.replace("Z", "+00:00"))
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
        except ValueError:
            received_at = now

        try:
            if result.get("entry_is_algo"):
                raw = get_algo_order(entry_order_id)
                order = normalize_algo_entry_order(raw)
            else:
                order = get_order(symbol, entry_order_id)
        except Exception as exc:
            msg = str(exc)
            if "-2013" in msg or "order does not exist" in msg.lower():
                logger.debug("reconcile get_order %s %s (not yet visible): %s", symbol, entry_order_id, exc)
            else:
                logger.warning("reconcile get_order %s %s: %s", symbol, entry_order_id, exc)
            continue

        status = str(order.get("status") or "").upper()
        if status == "FILLED":
            step_size, tick_size, _ = _get_filters(symbol)
            mark_px = get_mark_price(symbol)
            from trading.stop_limit_entry import finalize_filled_entry

            if finalize_filled_entry(
                row,
                order=order,
                tick_size=tick_size,
                mark_px=mark_px,
                source=str(row.get("source") or ""),
            ):
                _cancel_oco_peer(filled_row=row, result=result)
                promoted += 1
            continue

        if status in ("CANCELED", "EXPIRED", "REJECTED"):
            update_signal_status(int(row["id"]), "cancelled", status.lower())
            continue

        if now - received_at > timedelta(hours=_PENDING_TTL_HOURS) and status in ("NEW", "PARTIALLY_FILLED"):
            try:
                if result.get("entry_is_algo"):
                    cancel_algo_order(entry_order_id)
                else:
                    cancel_order_by_id(symbol, entry_order_id)
            except Exception as exc:
                logger.warning("cancel stale entry %s %s: %s", symbol, entry_order_id, exc)
            update_signal_status(int(row["id"]), "cancelled", "entry_ttl_expired")

    if promoted:
        logger.info("reconcile_pending_entry_orders promoted=%d", promoted)
    return promoted
