"""Config 表读写。"""
from __future__ import annotations

import os
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


def apply_env_config_overrides() -> None:
    """部署环境变量显式设置时覆盖 DB（用于 Railway 等，可纠正已有 Volume 里的 false）。"""
    for env_key, config_key in (("BINANCE_ENABLED", "enabled"),):
        raw = os.getenv(env_key, "").strip()
        if not raw:
            continue
        if raw.lower() in ("1", "true", "yes", "on"):
            set_config(config_key, "true")
        elif raw.lower() in ("0", "false", "no", "off"):
            set_config(config_key, "false")
