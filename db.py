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
    _compute_expire_at,
    _resolve_expire_hours,
    count_open_by_play,
    count_open_by_source,
    count_open_total,
    get_all_config,
    get_config,
    get_source_config,
    set_config,
    set_config_batch,
    source_enabled,
)

# -- Signals ----------------------------------------------------------
from repos.signals_repo import (
    insert_signal,
    list_signals,
    update_status as update_signal_status,
)

# -- Positions --------------------------------------------------------
from repos.positions_repo import (
    cancel_pending_position,
    compute_expire_at,
    compute_pending_deadline,
    get_open_expired_positions,
    get_open_position_for_symbol,
    get_open_positions,
    get_pending_entries,
    get_position_by_id,
    insert_pending_position,
    insert_position,
    list_positions,
    promote_pending_to_open,
    resolve_expire_hours,
    update_position_closed,
)

# -- PnL --------------------------------------------------------------
from repos.pnl_repo import pnl_summary
