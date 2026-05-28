"""positions 表全量 CRUD。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from repos.config_repo import _compute_expire_at, _resolve_expire_hours
from repos.connection import get_db

logger = logging.getLogger("repos.positions")


def insert_position(
    signal_log_id: int,
    symbol: str,
    side: str,
    entry_order_id: Optional[str],
    sl_order_id: Optional[str],
    tp_order_id: Optional[str],
    entry_price: Optional[float],
    sl_price: Optional[float],
    tp_price: Optional[float],
    quantity: Optional[float],
    notional_usdt: Optional[float],
    leverage: Optional[int],
    opened_at: str,
    play: Optional[str] = None,
    source: str = "",
) -> int:
    expire_hours = _resolve_expire_hours(play, source=source)
    expire_at = _compute_expire_at(expire_hours)
    with get_db(write=True) as conn:
        cur = conn.execute(
            """INSERT INTO positions
               (signal_log_id, symbol, side, entry_order_id, sl_order_id,
                tp_order_id, entry_price, sl_price, tp_price, quantity,
                notional_usdt, leverage, opened_at, expire_at, status, play, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)""",
            (
                signal_log_id, symbol, side, entry_order_id, sl_order_id,
                tp_order_id, entry_price, sl_price, tp_price, quantity,
                notional_usdt, leverage, opened_at, expire_at, play, source,
            ),
        )
        return cur.lastrowid


def update_position_closed(
    position_id: int, close_reason: str, close_price: float,
    closed_at: str, pnl_usdt: float, pnl_pct: float,
) -> None:
    with get_db(write=True) as conn:
        conn.execute(
            """UPDATE positions
               SET status='closed', close_reason=?, close_price=?,
                   closed_at=?, pnl_usdt=?, pnl_pct=?
               WHERE id=? AND status='open'""",
            (close_reason, close_price, closed_at, pnl_usdt, pnl_pct, position_id),
        )


def list_positions(
    status: Optional[str] = None, limit: int = 100, offset: int = 0,
) -> List[Dict[str, Any]]:
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM positions ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
    return [dict(r) for r in rows]


def get_open_positions() -> List[Dict[str, Any]]:
    return list_positions(status="open", limit=500)


def get_open_expired_positions() -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM positions "
            "WHERE status='open' AND expire_at IS NOT NULL AND expire_at <= ?",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_position_for_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM positions "
            "WHERE status IN ('open','pending_entry') AND symbol=? LIMIT 1",
            (symbol,),
        ).fetchone()
    return dict(row) if row else None


def get_position_by_id(position_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE id=?", (position_id,)
        ).fetchone()
    return dict(row) if row else None


# -- Pending-entry helpers ----------------------------------------------------

def insert_pending_position(
    *,
    signal_log_id: int,
    symbol: str,
    side: str,
    entry_order_id: str,
    sl_price: Optional[float],
    tp_price: Optional[float],
    quantity: Optional[float],
    notional_usdt: Optional[float],
    leverage: Optional[int],
    opened_at: str,
    entry_deadline: str,
    play: Optional[str] = None,
    source: str = "",
    entry_price: Optional[float] = None,
) -> int:
    with get_db(write=True) as conn:
        cur = conn.execute(
            """INSERT INTO positions
               (signal_log_id, symbol, side, entry_order_id, sl_order_id,
                tp_order_id, entry_price, sl_price, tp_price, quantity,
                notional_usdt, leverage, opened_at, expire_at, status,
                play, source, entry_deadline)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'pending_entry',?,?,?)""",
            (
                signal_log_id, symbol, side, entry_order_id, None,
                None, entry_price, sl_price, tp_price, quantity,
                notional_usdt, leverage, opened_at,
                play, source, entry_deadline,
            ),
        )
        return cur.lastrowid


def get_pending_entries() -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='pending_entry' ORDER BY id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def promote_pending_to_open(
    position_id: int, *, entry_price: float, quantity: float,
    sl_order_id: Optional[str], tp_order_id: Optional[str], expire_at: str,
) -> None:
    with get_db(write=True) as conn:
        conn.execute(
            """UPDATE positions
               SET status='open', entry_price=?, quantity=?,
                   sl_order_id=?, tp_order_id=?, expire_at=?, entry_deadline=NULL
               WHERE id=? AND status='pending_entry'""",
            (entry_price, quantity, sl_order_id, tp_order_id, expire_at, position_id),
        )


def cancel_pending_position(position_id: int, reason: str) -> None:
    closed_at = datetime.now(timezone.utc).isoformat()
    with get_db(write=True) as conn:
        conn.execute(
            """UPDATE positions
               SET status='cancelled_pending', close_reason=?, closed_at=?
               WHERE id=? AND status='pending_entry'""",
            (reason, closed_at, position_id),
        )


def compute_pending_deadline(timeout_sec: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=timeout_sec)).isoformat()


def compute_expire_at(expire_hours: float) -> str:
    return _compute_expire_at(expire_hours)


def resolve_expire_hours(play: Optional[str], source: str = "") -> float:
    return _resolve_expire_hours(play, source=source)
