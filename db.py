"""SQLite 数据库层 — 配置、信号日志、持仓记录的持久化。

数据库文件：$DATA_DIR/binance.db（默认在服务目录下）。
使用 WAL 模式 + 进程级 RLock 串行化写入。

close_reason 取值：'tp' | 'sl' | 'expired' | 'manual' | 'unknown'
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent))
DB_PATH = _DATA_DIR / "binance.db"

_db_write_lock = threading.RLock()

DEFAULT_CONFIG: Dict[str, str] = {
    # 全局
    "binance_api_key": "",
    "binance_api_secret": "",
    "testnet": "false",
    "enabled": "false",
    "max_positions": "8",
    # ZCT VWAP（保留 play 体系）
    "margin_usdt": "100",
    "leverage": "10",
    "src_zct_vwap_enabled": "false",
    "max_positions_play01": "5",
    "max_positions_play02": "5",
    "max_positions_play03": "5",
    "expire_hours_play01": "5",
    "expire_hours_play02": "4",
    "expire_hours_play03": "3",
    # 动量
    "src_momentum_enabled": "false",
    "src_momentum_margin_usdt": "100",
    "src_momentum_leverage": "10",
    "src_momentum_max_positions": "2",
    "src_momentum_expire_hours": "4",
    "src_momentum_entry_type": "LIMIT",
    # 接针
    "src_jiezhen_enabled": "false",
    "src_jiezhen_margin_usdt": "100",
    "src_jiezhen_leverage": "10",
    "src_jiezhen_max_positions": "3",
    "src_jiezhen_expire_hours": "4",
    # 入场单类型 (MARKET / LIMIT)；per-source 可用 src_{source}_entry_type 覆盖
    "entry_type": "MARKET",
    # LIMIT 单未成交超时秒数；per-source 可用 src_{source}_limit_entry_timeout_sec 覆盖
    "limit_entry_timeout_sec": "30",
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
    "BINANCE_EXPIRE_HOURS_PLAY01": "expire_hours_play01",
    "BINANCE_EXPIRE_HOURS_PLAY02": "expire_hours_play02",
    "BINANCE_EXPIRE_HOURS_PLAY03": "expire_hours_play03",
    # 动量
    "BINANCE_SRC_MOMENTUM_ENABLED": "src_momentum_enabled",
    "BINANCE_SRC_MOMENTUM_MARGIN_USDT": "src_momentum_margin_usdt",
    "BINANCE_SRC_MOMENTUM_LEVERAGE": "src_momentum_leverage",
    "BINANCE_SRC_MOMENTUM_MAX_POSITIONS": "src_momentum_max_positions",
    "BINANCE_SRC_MOMENTUM_EXPIRE_HOURS": "src_momentum_expire_hours",
    # 接针
    "BINANCE_SRC_JIEZHEN_ENABLED": "src_jiezhen_enabled",
    "BINANCE_SRC_JIEZHEN_MARGIN_USDT": "src_jiezhen_margin_usdt",
    "BINANCE_SRC_JIEZHEN_LEVERAGE": "src_jiezhen_leverage",
    "BINANCE_SRC_JIEZHEN_MAX_POSITIONS": "src_jiezhen_max_positions",
    "BINANCE_SRC_JIEZHEN_EXPIRE_HOURS": "src_jiezhen_expire_hours",
    # 入场单类型 / 限价超时
    "BINANCE_ENTRY_TYPE": "entry_type",
    "BINANCE_LIMIT_ENTRY_TIMEOUT_SEC": "limit_entry_timeout_sec",
    "BINANCE_SRC_MOMENTUM_ENTRY_TYPE": "src_momentum_entry_type",
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
    play            TEXT,
    source          TEXT,
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
        # 存量 DB 迁移：positions 表加 source 列
        try:
            conn.execute("ALTER TABLE positions ADD COLUMN source TEXT")
            logger.info("migrated: positions.source column added")
        except Exception:
            pass
        # 存量 DB 迁移：positions 表加 entry_deadline 列（LIMIT 单挂单截止时间）
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


def source_enabled(source: str) -> bool:
    """检查策略是否启用。zct_vwap 查 src_zct_vwap_enabled，其余查 src_{source}_enabled。"""
    if not source:
        return False
    key = f"src_{source}_enabled"
    return get_config(key, "false").lower() == "true"


def get_source_config(source: str, key_suffix: str, default: str = "") -> str:
    """获取策略配置: get_source_config('momentum', 'margin_usdt', '100')"""
    if not source:
        return default
    key = f"src_{source}_{key_suffix}"
    return get_config(key, default)


def count_open_by_source(source: str) -> int:
    """按 source 字段统计当前持仓数（含 pending_entry，避免限价挂单与已成交仓重复占名额）。"""
    if not source:
        return 0
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM positions "
            "WHERE status IN ('open','pending_entry') AND source=?",
            (source,),
        ).fetchone()
    return row["cnt"] if row else 0


def _resolve_expire_hours(play: Optional[str], source: str = "") -> float:
    """计算持仓过期时间。

    ZCT VWAP: 按 play(PLAY01/02/03) 分别读取 expire_hours_play*。
    动量/接针: 按 source 读取 src_{source}_expire_hours。
    """
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
    # 回退：各策略自己的 expire_hours
    if source:
        val = get_source_config(source, "expire_hours", "")
        if val:
            try:
                return float(val)
            except ValueError:
                pass
    return 4.0


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
    logger.info("insert_position: symbol=%s side=%s source=%s play=%s expire_hours=%.1f",
                 symbol, side, source, play, expire_hours)
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
    """返回 open 或 pending_entry 状态下该 symbol 的持仓行（用于平仓 / 占名额校验）。"""
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


# ---------------------------------------------------------------------------
# Pending-entry helpers (LIMIT 单挂单中状态)
# ---------------------------------------------------------------------------

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
    """写入 status='pending_entry' 行：限价单已下，等待成交。

    sl_order_id / tp_order_id 留空，等 reconcile_pending_entries 在 entry 成交后补。
    quantity 写计划数量；成交后 reconcile 用真实 executedQty 覆盖。
    """
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
    """返回所有 pending_entry 仓位（reconcile_pending_entries 轮询用）。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='pending_entry' ORDER BY id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def promote_pending_to_open(
    position_id: int,
    *,
    entry_price: float,
    quantity: float,
    sl_order_id: Optional[str],
    tp_order_id: Optional[str],
    expire_at: str,
) -> None:
    """entry 成交后把 pending_entry 行升格为 open，并补真实成交价 / 数量 / SL-TP id / expire_at。"""
    with get_db(write=True) as conn:
        conn.execute(
            """UPDATE positions
               SET status='open',
                   entry_price=?,
                   quantity=?,
                   sl_order_id=?,
                   tp_order_id=?,
                   expire_at=?,
                   entry_deadline=NULL
               WHERE id=? AND status='pending_entry'""",
            (entry_price, quantity, sl_order_id, tp_order_id, expire_at, position_id),
        )


def cancel_pending_position(position_id: int, reason: str) -> None:
    """限价单超时 / 被拒 / 平仓中断时，把 pending_entry 行置为 cancelled_pending（保留行供审计）。"""
    closed_at = datetime.now(timezone.utc).isoformat()
    with get_db(write=True) as conn:
        conn.execute(
            """UPDATE positions
               SET status='cancelled_pending',
                   close_reason=?,
                   closed_at=?
               WHERE id=? AND status='pending_entry'""",
            (reason, closed_at, position_id),
        )


def compute_pending_deadline(timeout_sec: float) -> str:
    """生成 pending_entry 的 entry_deadline ISO 字符串。"""
    return (
        datetime.now(timezone.utc) + timedelta(seconds=timeout_sec)
    ).isoformat()


def compute_expire_at(expire_hours: float) -> str:
    """暴露给 trader.py 的 promote 流程使用，复用现有内部计算。"""
    return _compute_expire_at(expire_hours)


def resolve_expire_hours(play: Optional[str], source: str = "") -> float:
    """暴露内部 _resolve_expire_hours 给 trader.py 用于 promote 时计算 expire_at。"""
    return _resolve_expire_hours(play, source=source)



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
