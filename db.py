"""数据库访问层 — Phase 6 repo facade。

所有实现已提取到 repos/ 模块。db.py 保留纯名字重新导出以保持向后兼容。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("db")

# -- Connection + init ------------------------------------------------
from repos.connection import (
    DB_PATH,
    _db_write_lock,
    get_db,
    init_db,
)

# -- Config -----------------------------------------------------------
from repos.config_repo import (
    get_all_config,
    get_config,
    set_config,
    set_config_batch,
)

# -- Signals ----------------------------------------------------------
from repos.signals_repo import (
    delete_signals_older_than,
    insert_signal,
    list_signals,
    update_execution as update_signal_execution,
    update_status as update_signal_status,
)

# -- Income / PnL -----------------------------------------------------
from repos.income_repo import (
    aggregate_pnl,
    clear_income_cache,
    get_income_sync_state,
    list_income_events,
    pnl_totals,
    upsert_income_events,
)
