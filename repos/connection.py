"""Protocol SQLite 连接、事务边界和幂等迁移。

每次 ``get_db`` 都创建短连接并启用 WAL。写事务还会获取进程内 RLock，配合 ingest
路由的批量锁减少 SQLite 写竞争。数据库只保存执行审计，不保存 Binance 密钥。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger("repos.connection")

_DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
DB_PATH = _DATA_DIR / "binance.db"
_db_write_lock = threading.RLock()

_PERF_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_signals_log_dedup ON signals_log(source, api_signal_id)",
    "CREATE INDEX IF NOT EXISTS idx_signals_log_status ON signals_log(status)",
    "CREATE INDEX IF NOT EXISTS idx_signals_log_source_action ON signals_log(source, action, status)",
    "CREATE INDEX IF NOT EXISTS idx_signals_log_profile ON signals_log(source, profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_income_events_time ON income_events(time_ms)",
    "CREATE INDEX IF NOT EXISTS idx_income_events_type_time ON income_events(income_type, time_ms)",
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
    profile_id     INTEGER,
    client_ref     TEXT    DEFAULT '',
    action         TEXT    DEFAULT 'open',
    position_id    INTEGER,
    payload_json   TEXT,
    result_json    TEXT,
    UNIQUE(source, api_signal_id)
);

CREATE TABLE IF NOT EXISTS income_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    DEFAULT '',
    income_type  TEXT    NOT NULL,
    income       REAL    NOT NULL,
    asset        TEXT    NOT NULL DEFAULT 'USDT',
    time_ms      INTEGER NOT NULL,
    tran_id      TEXT    NOT NULL,
    trade_id     TEXT    DEFAULT '',
    info         TEXT    DEFAULT '',
    raw_json     TEXT,
    synced_at    TEXT    NOT NULL,
    UNIQUE(income_type, tran_id)
);

CREATE TABLE IF NOT EXISTS income_sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


@contextmanager
def get_db(write: bool = False) -> Generator[sqlite3.Connection, None, None]:
    """提供自动 commit/rollback/close 的连接上下文。"""
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
        for table, column, ddl in [
            ("signals_log", "profile_id", "ALTER TABLE signals_log ADD COLUMN profile_id INTEGER"),
            ("signals_log", "client_ref", "ALTER TABLE signals_log ADD COLUMN client_ref TEXT DEFAULT ''"),
            ("signals_log", "action", "ALTER TABLE signals_log ADD COLUMN action TEXT DEFAULT 'open'"),
            ("signals_log", "position_id", "ALTER TABLE signals_log ADD COLUMN position_id INTEGER"),
            ("signals_log", "payload_json", "ALTER TABLE signals_log ADD COLUMN payload_json TEXT"),
            ("signals_log", "result_json", "ALTER TABLE signals_log ADD COLUMN result_json TEXT"),
        ]:
            try:
                conn.execute(ddl)
                logger.info("migrated: %s.%s column added", table, column)
            except Exception:
                pass
        # Performance indexes (Phase 8)
        for idx_sql in _PERF_INDEXES:
            try:
                conn.execute(idx_sql)
            except Exception:
                pass
