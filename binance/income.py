"""Binance Futures income history 客户端。"""
from __future__ import annotations

import time
from typing import Any

from binance.client import BinanceClient

MAX_DAYS = 90
CHUNK_MS = 7 * 24 * 60 * 60 * 1000
PNL_LIMIT = 1000


def get_income_history(
    client: BinanceClient,
    *,
    start_ms: int,
    end_ms: int,
    page: int = 1,
    limit: int = PNL_LIMIT,
) -> list[dict[str, Any]]:
    data = client.request(
        "GET",
        "/fapi/v1/income",
        {
            "startTime": int(start_ms),
            "endTime": int(end_ms),
            "page": int(page),
            "limit": min(int(limit), PNL_LIMIT),
        },
    )
    return data if isinstance(data, list) else []


def sync_recent_income(client: BinanceClient, *, days: int = MAX_DAYS) -> dict[str, Any]:
    """按 7 天窗口拉取最近 income 流水。

    普通 income 接口只保留近期历史；本地缓存负责长期留存。
    """
    from repos.income_repo import upsert_income_events

    days = max(1, min(int(days), MAX_DAYS))
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    fetched = 0
    inserted = 0
    cur = start_ms
    while cur <= end_ms:
        chunk_end = min(cur + CHUNK_MS - 1, end_ms)
        page = 1
        while True:
            rows = get_income_history(client, start_ms=cur, end_ms=chunk_end, page=page, limit=PNL_LIMIT)
            fetched += len(rows)
            inserted += upsert_income_events(rows)
            if len(rows) < PNL_LIMIT:
                break
            page += 1
        cur = chunk_end + 1
    return {"days": days, "fetched": fetched, "inserted": inserted}
