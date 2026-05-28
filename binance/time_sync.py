"""币安服务器时间同步。

维护 server time offset（本地 ↔ 币安的时间差），由 BinanceClient 驱动。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("binance.time_sync")

RECV_WINDOW_MS = 5000
SERVER_TIME_RESYNC_SEC = 600

_offset_ms: int = 0
_offset_lock = threading.Lock()
_last_sync_ts: float = 0.0


def now_utc() -> str:
    """UTC ISO8601 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _local_ms() -> int:
    return int(time.time() * 1000)


def do_sync(base_url: str, http_client) -> None:
    """调用 /fapi/v1/time 更新 offset。由 BinanceClient._sync_server_time 调用。"""
    global _offset_ms, _last_sync_ts
    try:
        url = base_url + "/fapi/v1/time"
        resp = http_client.get(url)
        resp.raise_for_status()
        srv = int(resp.json()["serverTime"])
        with _offset_lock:
            _offset_ms = srv - _local_ms()
            _last_sync_ts = time.time()
        logger.debug("server time offset = %d ms", _offset_ms)
    except Exception as exc:
        logger.warning("server time sync failed: %s", exc)


def server_timestamp_ms() -> int:
    """返回 (本地毫秒 + offset)，即币安感知的当前时间戳。"""
    return _local_ms() + _offset_ms


def should_resync() -> bool:
    """是否需要重新同步（距上次同步超过阈值）。"""
    return (time.time() - _last_sync_ts) > SERVER_TIME_RESYNC_SEC
