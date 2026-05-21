"""SQLite 数据库层 — 配置、信号日志、持仓记录的持久化。

数据库文件：$DATA_DIR/binance.db（默认在服务目录下）。
使用 WAL 模式 + 进程级 RLock 串行化写入。

close_reason 取值：'tp' | 'sl' | 'expired' | 'manual' | 'unknown'
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

_DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent))
DB_PATH = _DATA_DIR / "binance.db"

_db_write_lock = threading.RLock()

DEFAULT_CONFIG: Dict[str, str] = {
    "binance_api_key": "",
    "binance_api_secret": "",
    "testnet": "false",
    "enabled": "false",
    "margin_usdt": "100",
    "max_positions": "8",
    "max_positions_play01": "5",
    "max_positions_play02": "5",
    "max_positions_play03": "5",
    "leverage": "10",
    "enabled_sources": "zct_vwap",
    "position_expire_hours": "4",
    "expire_hours_play01": "5",
    "expire_hours_play02": "4",
    "expire_hours_play03": "3",
}

_ENV_TO_CONFIG: Dict[str, str] = {
    "BINANCE_API_KEY": "binance_api_key",
    "BINANCE_API_SECRET": "binance_api_secret",
    "BINANCE_TESTNET": "testnet",
    "BINANCE_MARGIN_USDT": "margin_usdt",
    "BINANCE_LEVERAGE": "leverage",
    "BINANCE_MAX_POSITIONS": "max_positions",
    "BINANCE_MAX_POSITIONS_PLAY01": "max_positions_play01",
    "BINANCE_MAX_POSITIONS_PLAY02": "max_positions_play02",
    "BINANCE_MAX_POSITIONS_PLAY03": "max_positions_play03",
    "BINANCE_EXPIRE_HOURS": "position_expire_hours",
    "BINANCE_EXPIRE_HOURS_PLAY01": "expire_hours_play01",
    "BINANCE_EXPIRE_HOURS_PLAY02": "expire_hours_play02",
    "BINANCE_EXPIRE_HOURS_PLAY03": "expire_hours_play03",
}

DDL = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS signals_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT    NOT NULL,
    api_signal_id   TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    entry_price     REAL,
    sl_price        REAL,
    tp_price        REAL,
    confidence      REAL,
    regime          TEXT,
    notional_usdt   REAL,
    received_at     TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'received',
    skip_reason     TEXT,
    play            TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_signal_source_id
    ON signals_log (source, api_signal_id);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_log_id   INTEGER REFERENCES signals_log(id),
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    entry_order_id  TEXT,
    sl_order_id     TEXT,
    tp_order_id     TEXT,
    entry_price     REAL,
    sl_price        REAL,
    tp_price        REAL,
    quantity        REAL,
    notional_usdt   REAL,
    leverage        INTEGER,
    opened_at       TEXT    NOT NULL,
    expire_at       TEXT,
    status          TEXT    NOT NULL DEFAULT 'open',
    close_reason    TEXT,
    close_price     REAL,
    closed_at       TEXT,
    pnl_usdt        REAL,
    pnl_pct         REAL,
    play            TEXT
);
"""


@contextmanager
def get_db(write: bool = False) -> Generator[sqlite3.Connection, None, None]:
    if write:
        _db_write_lock.acquire()
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if write:
            _db_write_lock.release()


def init_db() -> None:
    with get_db(write=True) as conn:
        conn.executescript(DDL)
        for env_key, config_key in _ENV_TO_CONFIG.items():
            env_val = os.getenv(env_key, "").strip()
            if env_val:
                conn.execute(
                    "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                    (config_key, env_val),
                )
        for k, v in DEFAULT_CONFIG.items():
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v)
            )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_config(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ).fetchone()
    val = row["value"] if row else ""
    if val:
        return val
    if key == "margin_usdt":
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM config WHERE key = ?", ("position_size_usdt",)
            ).fetchone()
        if row and row["value"]:
            return row["value"]
    return default


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


# ---------------------------------------------------------------------------
# signals_log
# ---------------------------------------------------------------------------

def insert_signal(
    source: str,
    api_signal_id: str,
    symbol: str,
    side: str,
    entry_price: Optional[float],
    sl_price: Optional[float],
    tp_price: Optional[float],
    confidence: Optional[float],
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


def update_signal_status(
    signal_id: int, status: str, skip_reason: Optional[str] = None
) -> None:
    with get_db(write=True) as conn:
        conn.execute(
            "UPDATE signals_log SET status=?, skip_reason=? WHERE id=?",
            (status, skip_reason, signal_id),
        )


def list_signals(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM signals_log ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# positions
# ---------------------------------------------------------------------------

def _compute_expire_at(expire_hours: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=expire_hours)
    ).isoformat()


def _resolve_expire_hours(play: Optional[str]) -> float:
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
    return float(get_config("position_expire_hours", "4"))


def count_open_by_play(play: Optional[str]) -> int:
    if not play:
        return 0
    p = str(play).strip().upper()
    prefix = ""
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
            "SELECT COUNT(*) as cnt FROM positions WHERE status='open' AND play LIKE ?",
            (prefix,),
        ).fetchone()
    return row["cnt"] if row else 0


def count_open_total() -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM positions WHERE status='open'"
        ).fetchone()
    return row["cnt"] if row else 0


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
) -> int:
    expire_hours = _resolve_expire_hours(play)
    expire_at = _compute_expire_at(expire_hours)
    with get_db(write=True) as conn:
        cur = conn.execute(
            """INSERT INTO positions
               (signal_log_id, symbol, side, entry_order_id, sl_order_id,
                tp_order_id, entry_price, sl_price, tp_price, quantity,
                notional_usdt, leverage, opened_at, expire_at, status, play)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?)""",
            (
                signal_log_id, symbol, side, entry_order_id, sl_order_id,
                tp_order_id, entry_price, sl_price, tp_price, quantity,
                notional_usdt, leverage, opened_at, expire_at, play,
            ),
        )
        return cur.lastrowid


def update_position_closed(
    position_id: int,
    close_reason: str,
    close_price: float,
    closed_at: str,
    pnl_usdt: float,
    pnl_pct: float,
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
    status: Optional[str] = None, limit: int = 100, offset: int = 0
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
            "SELECT * FROM positions WHERE status='open' AND symbol=? LIMIT 1",
            (symbol,),
        ).fetchone()
    return dict(row) if row else None


def get_position_by_id(position_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE id=?", (position_id,)
        ).fetchone()
    return dict(row) if row else None


def pnl_summary() -> Dict[str, Any]:
    with get_db() as conn:
        row = conn.execute(
            """SELECT
               COALESCE(COUNT(*), 0)                                        AS total,
               COALESCE(SUM(CASE WHEN pnl_usdt > 0 THEN 1 ELSE 0 END), 0)  AS wins,
               COALESCE(SUM(CASE WHEN pnl_usdt <= 0 THEN 1 ELSE 0 END), 0) AS losses,
               COALESCE(SUM(pnl_usdt), 0.0)                                AS total_pnl,
               COALESCE(AVG(pnl_usdt), 0.0)                                AS avg_pnl
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
