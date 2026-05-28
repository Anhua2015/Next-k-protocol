"""后台任务注册 — 持仓同步与过期平仓。

注册到 APScheduler（内嵌于 main.py）：
  - sync_positions: 每 30s 检测 SL/TP 触发，更新 DB
  - reconcile_pending: 每 5s 处理 LIMIT entry 挂单（成交则下 SL/TP 转 open，超时则撤单）
  - expire_positions: 每 5min 强平过期持仓
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def register_jobs(sch: Any) -> None:
    from trader import expire_open_positions, reconcile_pending_entries, sync_open_positions

    sch.add_job(
        sync_open_positions,
        "interval",
        seconds=30,
        id="sync_positions",
        max_instances=1,
        replace_existing=True,
    )
    sch.add_job(
        reconcile_pending_entries,
        "interval",
        seconds=5,
        id="reconcile_pending",
        max_instances=1,
        replace_existing=True,
    )
    sch.add_job(
        expire_open_positions,
        "interval",
        minutes=5,
        id="expire_positions",
        max_instances=1,
        replace_existing=True,
    )
    sch.add_job(
        _cleanup_stale_intents,
        "interval",
        minutes=5,
        id="cleanup_intents",
        max_instances=1,
        replace_existing=True,
    )
    logger.info("Binance jobs registered (sync=30s, reconcile_pending=5s, expire=5min, cleanup_intent=5min)")


def _cleanup_stale_intents() -> None:
    """将超过 60s 的 intent 状态信号标记为 error。

    仅在 INGEST_LOCKLESS_EXECUTE 启用时机有意义（锁外执行时 intent 需要 GC），
    现作为 future-proofing 占位。
    """
    import os as _os
    if _os.getenv("INGEST_LOCKLESS_EXECUTE", "false").lower() not in (
        "1", "true", "yes", "on",
    ):
        return
    from binance.time_sync import now_utc
    from db import update_signal_status
    import sqlite3
    from db import DB_PATH
    cutoff = now_utc()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id FROM signals_log WHERE status='intent' AND received_at < ?",
            (cutoff,),
        ).fetchall()
        for r in rows:
            update_signal_status(r["id"], "error", "intent_timeout")
        if rows:
            logger.warning("cleanup_intents: marked %d stale intents as error", len(rows))
    except Exception:
        pass
    finally:
        conn.close()
