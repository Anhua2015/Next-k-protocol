"""Config 表读写（遗留表，交易参数已改由 next-k-api 信号控制）。"""
from __future__ import annotations

from typing import Dict

from repos.connection import get_db


def get_config(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row and row["value"] else default


def get_all_config() -> Dict[str, str]:
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_config(key: str, value: str) -> None:
    with get_db(write=True) as conn:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def set_config_batch(pairs: Dict[str, str]) -> None:
    with get_db(write=True) as conn:
        for k, v in pairs.items():
            conn.execute(
                "INSERT INTO config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, v),
            )
