"""后台任务注册。

当前协议层已移除本地 lifecycle/scheduler 管理，这里保留空实现以兼容旧导入。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def register_jobs(sch: Any) -> None:
    logger.info("scheduler.register_jobs() noop: lifecycle jobs removed")


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
