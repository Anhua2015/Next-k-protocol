"""Config 表读写。"""
from __future__ import annotations

import os
from typing import Dict, Optional

from repos.connection import get_db

# 库中无对应键时的读取默认值（不影响已写入的 false）
_SOURCE_ENABLED_DEFAULTS: Dict[str, str] = {
    "moss_quant": "true",
}


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


def source_enabled(source: str) -> bool:
    if not source:
        return False
    default = _SOURCE_ENABLED_DEFAULTS.get(source, "false")
    return get_config(f"src_{source}_enabled", default).lower() == "true"


def apply_env_config_overrides() -> None:
    """部署环境变量显式设置时覆盖 DB（用于 Railway 等，可纠正已有 Volume 里的 false）。"""
    for env_key, config_key in (
        ("BINANCE_ENABLED", "enabled"),
        ("SRC_MOSS_QUANT_ENABLED", "src_moss_quant_enabled"),
    ):
        raw = os.getenv(env_key, "").strip()
        if not raw:
            continue
        if raw.lower() in ("1", "true", "yes", "on"):
            set_config(config_key, "true")
        elif raw.lower() in ("0", "false", "no", "off"):
            set_config(config_key, "false")


def get_source_config(source: str, key_suffix: str, default: str = "") -> str:
    if not source:
        return default
    return get_config(f"src_{source}_{key_suffix}", default)


def count_open_by_source(source: str) -> int:
    if not source:
        return 0
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM positions "
            "WHERE status IN ('open','pending_entry') AND source=?",
            (source,),
        ).fetchone()
    return row["cnt"] if row else 0


def count_open_by_play(play: Optional[str]) -> int:
    if not play:
        return 0
    p = str(play).strip().upper()
    if p.startswith("PLAY01"):
        prefix = "PLAY01%"
    elif p.startswith("PLAY02"):
        prefix = "PLAY02%"
    elif p.startswith("PLAY03"):
        prefix = "PLAY03%"
    else:
        return 0
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM positions "
            "WHERE status IN ('open','pending_entry') AND play LIKE ?",
            (prefix,),
        ).fetchone()
    return row["cnt"] if row else 0


def count_open_total() -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM positions "
            "WHERE status IN ('open','pending_entry')"
        ).fetchone()
    return row["cnt"] if row else 0


def _compute_expire_at(expire_hours: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=expire_hours)).isoformat()


def _resolve_expire_hours(play: Optional[str], source: str = "") -> float:
    # 动量/接针：按 source 读取
    if source and source in ("momentum", "jiezhen"):
        val = get_source_config(source, "expire_hours", "")
        if val:
            try:
                return float(val)
            except ValueError:
                pass
    # ZCT VWAP：按 play 读取
    if play:
        p = str(play).strip().upper()
        if p.startswith("PLAY01"):
            val = get_config("expire_hours_play01", "")
        elif p.startswith("PLAY02"):
            val = get_config("expire_hours_play02", "")
        elif p.startswith("PLAY03"):
            val = get_config("expire_hours_play03", "")
        else:
            val = ""
        if val:
            try:
                return float(val)
            except ValueError:
                pass
    if source:
        val = get_source_config(source, "expire_hours", "")
        if val:
            try:
                return float(val)
            except ValueError:
                pass
    return 4.0
