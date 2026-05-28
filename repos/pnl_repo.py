"""PnL 汇总查询。"""
from __future__ import annotations

from typing import Any, Dict

from repos.connection import get_db


def pnl_summary() -> Dict[str, Any]:
    with get_db() as conn:
        row = conn.execute(
            """SELECT
               COALESCE(COUNT(*), 0) AS total,
               COALESCE(SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END), 0) AS wins,
               COALESCE(SUM(CASE WHEN pnl_usdt <= 0 THEN 1 ELSE 0 END), 0) AS losses,
               COALESCE(SUM(pnl_usdt), 0.0) AS total_pnl,
               COALESCE(AVG(pnl_usdt), 0.0) AS avg_pnl
               FROM positions WHERE status='closed'"""
        ).fetchone()
        daily = conn.execute(
            """SELECT DATE(closed_at) AS day, COALESCE(SUM(pnl_usdt), 0.0) AS pnl
               FROM positions WHERE status='closed' AND closed_at IS NOT NULL
               GROUP BY day ORDER BY day DESC LIMIT 30"""
        ).fetchall()
    return {
        "total": int(row["total"]),
        "wins": int(row["wins"]),
        "losses": int(row["losses"]),
        "total_pnl": round(float(row["total_pnl"]), 4),
        "avg_pnl": round(float(row["avg_pnl"]), 4),
        "daily": [dict(r) for r in daily],
    }
