"""signals_log 表读写。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from repos.connection import get_db


def _cutoff_received_at(*, keep_hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=float(keep_hours))
    return dt.isoformat()


def delete_signals_older_than(*, keep_hours: float = 24.0) -> int:
    """Delete signal log rows older than keep_hours (by received_at)."""
    cutoff = _cutoff_received_at(keep_hours=keep_hours)
    with get_db(write=True) as conn:
        cur = conn.execute(
            "DELETE FROM signals_log WHERE received_at IS NOT NULL AND received_at < ?",
            (cutoff,),
        )
        return int(cur.rowcount or 0)


def insert_signal(
    source: str,
    api_signal_id: str,
    symbol: str,
    side: str,
    entry_price: Optional[float],
    sl_price: Optional[float],
    tp_price: Optional[float],
    confidence: Optional[str],
    regime: Optional[str],
    notional_usdt: Optional[float],
    received_at: str,
    status: str = "received",
    skip_reason: Optional[str] = None,
    play: Optional[str] = None,
    profile_id: Optional[int] = None,
    client_ref: Optional[str] = "",
    action: str = "open",
    position_id: Optional[int] = None,
    payload_json: Optional[str] = None,
    result_json: Optional[str] = None,
) -> Optional[int]:
    try:
        with get_db(write=True) as conn:
            cur = conn.execute(
                """INSERT INTO signals_log
                   (source, api_signal_id, symbol, side, entry_price, sl_price,
                    tp_price, confidence, regime, notional_usdt, received_at,
                    status, skip_reason, play, profile_id, client_ref, action,
                    position_id, payload_json, result_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source, api_signal_id, symbol, side, entry_price, sl_price,
                    tp_price, confidence, regime, notional_usdt, received_at,
                    status, skip_reason, play, profile_id, client_ref or "",
                    action or "open", position_id, payload_json, result_json,
                ),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def update_status(
    signal_id: int, status: str, skip_reason: Optional[str] = None,
) -> None:
    with get_db(write=True) as conn:
        conn.execute(
            "UPDATE signals_log SET status=?, skip_reason=? WHERE id=?",
            (status, skip_reason, signal_id),
        )


def update_execution(
    signal_id: int,
    *,
    status: str,
    position_id: Optional[int] = None,
    result: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    skip_reason: Optional[str] = None,
) -> None:
    assignments = ["status=?"]
    params: List[Any] = [status]
    if position_id is not None:
        assignments.append("position_id=?")
        params.append(position_id)
    if result is not None:
        assignments.append("result_json=?")
        params.append(json.dumps(result))
    if payload is not None:
        assignments.append("payload_json=?")
        params.append(json.dumps(payload))
    if skip_reason is not None:
        assignments.append("skip_reason=?")
        params.append(skip_reason)
    params.append(signal_id)
    with get_db(write=True) as conn:
        conn.execute(
            f"UPDATE signals_log SET {', '.join(assignments)} WHERE id=?",
            params,
        )


def list_signals(
    limit: int = 100,
    offset: int = 0,
    source: Optional[str] = None,
    action: Optional[str] = None,
    status: Optional[str] = None,
    profile_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    filters = []
    params: List[Any] = []
    if source:
        filters.append("source=?")
        params.append(source)
    if action:
        filters.append("action=?")
        params.append(action)
    if status:
        filters.append("status=?")
        params.append(status)
    if profile_id is not None:
        filters.append("profile_id=?")
        params.append(profile_id)

    where = f" WHERE {' AND '.join(filters)}" if filters else ""
    params.extend([limit, offset])
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM signals_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]
