"""PnL income 后台同步任务。

同步数据以 Binance income history 为权威源，本任务只负责周期性拉取近期流水并写入本地
SQLite 缓存。所有异常都被吞掉并写日志，避免后台同步影响交易执行 API。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger("pnl_auto_sync")


def _truthy_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        logger.warning("bad %s=%r; fallback to %s", name, raw, default)
        return default
    return max(min_value, min(max_value, value))


def pnl_auto_sync_enabled() -> bool:
    return _truthy_env("PROTOCOL_PNL_AUTO_SYNC_ENABLED", True)


def pnl_auto_sync_config() -> dict[str, Any]:
    return {
        "startup_days": _int_env("PROTOCOL_PNL_STARTUP_SYNC_DAYS", 7, min_value=1, max_value=90),
        "incremental_days": _int_env("PROTOCOL_PNL_INCREMENTAL_SYNC_DAYS", 3, min_value=1, max_value=90),
        "incremental_interval_sec": _int_env(
            "PROTOCOL_PNL_INCREMENTAL_SYNC_INTERVAL_SEC",
            1800,
            min_value=60,
            max_value=86_400,
        ),
        "full_days": _int_env("PROTOCOL_PNL_FULL_SYNC_DAYS", 90, min_value=1, max_value=90),
        "full_interval_sec": _int_env(
            "PROTOCOL_PNL_FULL_SYNC_INTERVAL_SEC",
            86_400,
            min_value=3600,
            max_value=7 * 86_400,
        ),
    }


def _has_binance_credentials() -> bool:
    return bool(os.getenv("BINANCE_API_KEY", "").strip() and os.getenv("BINANCE_API_SECRET", "").strip())


async def _run_sync(days: int, *, reason: str) -> None:
    if not _has_binance_credentials():
        logger.info("PnL auto sync skipped: Binance credentials missing")
        return
    try:
        from trader import sync_recent_income

        result = await asyncio.to_thread(sync_recent_income, days=days)
        logger.info(
            "PnL auto sync done reason=%s days=%s fetched=%s inserted=%s",
            reason,
            result.get("days"),
            result.get("fetched"),
            result.get("inserted"),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning(
            "PnL auto sync failed reason=%s days=%s type=%s upstream_status=%s error=%s",
            reason,
            days,
            type(exc).__name__,
            status_code,
            exc,
        )


async def pnl_auto_sync_loop() -> None:
    """后台循环：启动同步 7 天；每 30 分钟同步 3 天；每天同步 90 天。"""
    if not pnl_auto_sync_enabled():
        logger.info("PnL auto sync disabled by PROTOCOL_PNL_AUTO_SYNC_ENABLED")
        return

    cfg = pnl_auto_sync_config()
    logger.info("PnL auto sync enabled: %s", cfg)
    await _run_sync(int(cfg["startup_days"]), reason="startup")

    last_full = time.monotonic()
    while True:
        await asyncio.sleep(int(cfg["incremental_interval_sec"]))
        await _run_sync(int(cfg["incremental_days"]), reason="incremental")

        now = time.monotonic()
        if now - last_full >= int(cfg["full_interval_sec"]):
            await _run_sync(int(cfg["full_days"]), reason="full")
            last_full = now
