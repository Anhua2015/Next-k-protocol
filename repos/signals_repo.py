"""signals_log 表读写。"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from repos.connection import get_db


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
) -> Optional[int]:
    try:
        with get_db(write=True) as conn:
            cur = conn.execute(
                """INSERT INTO signals_log
                   (source, api_signal_id, symbol, side, entry_price, sl_price,
                    tp_price, confidence, regime, notional_usdt, received_at,
                    status, skip_reason, play)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source, api_signal_id, symbol, side, entry_price, sl_price,
                    tp_price, confidence, regime, notional_usdt, received_at,
                    status, skip_reason, play,
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


def list_signals(
    limit: int = 100, offset: int = 0, source: Optional[str] = None,
) -> List[Dict[str, Any]]:
    with get_db() as conn:
        if source:
            rows = conn.execute(
                "SELECT * FROM signals_log WHERE source=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (source, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM signals_log ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    return [dict(r) for r in rows]
