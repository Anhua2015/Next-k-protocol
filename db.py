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
    apply_env_config_overrides,
    get_all_config,
    get_config,
    set_config,
    set_config_batch,
    source_enabled,
)

# -- Signals ----------------------------------------------------------
from repos.signals_repo import (
    insert_signal,
    list_signals,
    log_trade_event,
    update_execution as update_signal_execution,
    update_status as update_signal_status,
)
