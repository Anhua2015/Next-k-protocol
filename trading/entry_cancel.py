"""Cancel pending STOP / LIMIT entry orders by api_signal_id."""
from __future__ import annotations

import json
import logging
from typing import List

logger = logging.getLogger("trading.entry_cancel")


def cancel_pending_entry_by_api_id(source: str, api_signal_id: str, reason: str) -> bool:
    """Cancel a submitted STOP_LIMIT entry. Returns True if cancelled or already gone."""
    from db import get_signal_by_api_id, update_signal_status

    row = get_signal_by_api_id(source, api_signal_id)
    if not row:
        return False
    status = str(row.get("status") or "").lower()
    if status != "submitted":
        return False

    try:
        result = json.loads(row.get("result_json") or "{}")
    except json.JSONDecodeError:
        result = {}

    entry_order_id = str(result.get("entry_order_id") or "")
    symbol = str(row.get("symbol") or "")
    if not entry_order_id:
        update_signal_status(int(row["id"]), "cancelled", reason)
        return True

    from trader import cancel_algo_order, cancel_order_by_id

    try:
        if result.get("entry_is_algo"):
            cancel_algo_order(entry_order_id)
        elif symbol:
            cancel_order_by_id(symbol, entry_order_id)
    except Exception as exc:
        logger.warning("cancel pending entry %s %s: %s", symbol, entry_order_id, exc)

    update_signal_status(int(row["id"]), "cancelled", reason)
    logger.info("cancelled pending entry %s %s reason=%s", symbol, api_signal_id, reason)
    return True


def cancel_pending_entries_by_api_ids(
    source: str,
    api_signal_ids: List[str],
    reason: str,
) -> int:
    n = 0
    for api_id in api_signal_ids:
        if cancel_pending_entry_by_api_id(source, str(api_id), reason):
            n += 1
    return n
