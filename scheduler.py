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
    logger.info("Binance jobs registered (sync=30s, reconcile_pending=5s, expire=5min)")
