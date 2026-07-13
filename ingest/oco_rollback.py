"""OCO preplace: rollback submitted legs when the pair fails to arm."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger("ingest.oco_rollback")


def _leg_action_from_batch(
    api_id: str,
    *,
    by_id: Dict[str, Dict[str, Any]],
    source: str,
    db: Any,
) -> str:
    detail = by_id.get(api_id) or {}
    action = str(detail.get("action") or "").lower()
    if action == "duplicate":
        row = db.get_signal_by_api_id(source, api_id)
        return str((row or {}).get("status") or "duplicate").lower()
    if action:
        return action
    row = db.get_signal_by_api_id(source, api_id)
    return str((row or {}).get("status") or "").lower()


def rollback_incomplete_oco_batch(signals, details: List[Dict[str, Any]], db: Any) -> int:
    """If an OCO pair has a failure and a still-submitted peer, cancel the peer."""
    from trading.entry_cancel import cancel_pending_entry_by_api_id

    if not signals or not details:
        return 0

    by_id: Dict[str, Dict[str, Any]] = {}
    sig_by_id: Dict[str, Any] = {}
    for sig, detail in zip(signals, details):
        api_id = str(getattr(sig, "api_signal_id", "") or "")
        if api_id:
            by_id[api_id] = detail
            sig_by_id[api_id] = sig

    seen: Set[Tuple[str, str]] = set()
    cancelled = 0

    for sig in signals:
        peer_id = str(getattr(sig, "oco_peer_api_id", None) or "").strip()
        api_id = str(getattr(sig, "api_signal_id", "") or "")
        if not peer_id or not api_id:
            continue
        pair = tuple(sorted((api_id, peer_id)))
        if pair in seen:
            continue
        seen.add(pair)

        source = str(getattr(sig, "source", "") or "")
        states = {
            leg_id: _leg_action_from_batch(leg_id, by_id=by_id, source=source, db=db)
            for leg_id in pair
        }

        if any(states.get(l) == "traded" for l in pair):
            continue

        has_failure = any(states.get(l) == "error" for l in pair)
        submitted = [l for l in pair if states.get(l) == "submitted"]
        if not has_failure or not submitted:
            continue

        for leg_id in submitted:
            if cancel_pending_entry_by_api_id(source, leg_id, "oco_incomplete"):
                cancelled += 1
                by_id[leg_id] = {**(by_id.get(leg_id) or {}), "action": "cancelled"}

        logger.info(
            "oco rollback source=%s pair=%s states=%s cancelled=%d",
            source,
            pair,
            states,
            len(submitted),
        )

    return cancelled
