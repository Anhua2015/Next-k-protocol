"""数据库连接 + 初始化。"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Generator

logger = logging.getLogger("repos.connection")

_DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
DB_PATH = _DATA_DIR / "binance.db"
_db_write_lock = threading.RLock()

DEFAULT_CONFIG: Dict[str, str] = {
    "enabled": "false",
    "testnet": "false",
    "margin_usdt": "100",
    "leverage": "10",
    "entry_type": "MARKET",
    "max_positions": "8",
    "max_positions_play01": "5",
    "max_positions_play02": "5",
    "max_positions_play03": "5",
    "zct_vwap_expire_hours": "12",
    "zct_vwap_play01_expire_hours": "12",
    "zct_vwap_play02_expire_hours": "12",
    "zct_vwap_play03_expire_hours": "12",
    "momentum_expire_hours": "12",
    "jiezhen_expire_hours": "12",
    "src_zct_vwap_enabled": "true",
    "src_momentum_enabled": "true",
    "src_jiezhen_enabled": "true",
    "limit_entry_timeout_sec": "30",
    "zct_limit_entry_timeout_sec": "30",
    "momentum_limit_entry_timeout_sec": "30",
    "jiezhen_limit_entry_timeout_sec": "30",
}

_ENV_TO_CONFIG: Dict[str, str] = {
    "BINANCE_API_KEY": "binance_api_key",
    "BINANCE_API_SECRET": "binance_api_secret",
    "BINANCE_TESTNET": "testnet",
}

_PERF_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_signals_log_dedup ON signals_log(source, api_signal_id)",
    "CREATE INDEX IF NOT EXISTS idx_signals_log_status ON signals_log(status)",
    "CREATE INDEX IF NOT EXISTS idx_positions_status_symbol ON positions(status, symbol)",
    "CREATE INDEX IF NOT EXISTS idx_positions_status_play ON positions(status, play)",
    "CREATE INDEX IF NOT EXISTS idx_positions_status_source ON positions(status, source)",
    "CREATE INDEX IF NOT EXISTS idx_positions_expire_at ON positions(status, expire_at)",
    "CREATE INDEX IF NOT EXISTS idx_positions_closed_at ON positions(closed_at)",
]

DDL = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS signals_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT    NOT NULL DEFAULT '',
    api_signal_id  TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    side           TEXT    NOT NULL,
    entry_price    REAL,
    sl_price       REAL,
    tp_price       REAL,
    confidence     TEXT,
    regime         TEXT,
    notional_usdt  REAL,
    received_at    TEXT    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'received',
    skip_reason    TEXT,
    play           TEXT    DEFAULT '',
    UNIQUE(source, api_signal_id)
);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_log_id   INTEGER,
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
    leverage        INTEGER DEFAULT 1,
    opened_at       TEXT    NOT NULL,
    expire_at       TEXT,
    status          TEXT    NOT NULL DEFAULT 'open',
    close_reason    TEXT,
    close_price     REAL,
    closed_at       TEXT,
    pnl_usdt        REAL,
    pnl_pct         REAL,
    play            TEXT    DEFAULT '',
    source          TEXT    DEFAULT '',
    entry_deadline  TEXT
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
        try:
            conn.execute("ALTER TABLE positions ADD COLUMN source TEXT")
            logger.info("migrated: positions.source column added")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE positions ADD COLUMN entry_deadline TEXT")
            logger.info("migrated: positions.entry_deadline column added")
        except Exception:
            pass
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
        # Performance indexes (Phase 8)
        for idx_sql in _PERF_INDEXES:
            try:
                conn.execute(idx_sql)
            except Exception:
                pass
