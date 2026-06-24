"""Binance income 流水缓存与 PnL 聚合。

净盈亏以 Binance /fapi/v1/income 为权威来源。本地只做缓存、去重和周期聚合，不根据
订单自行推导手续费或资金费率。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from repos.connection import get_db

PNL_INCOME_TYPES = {
    "REALIZED_PNL",
    "FUNDING_FEE",
    "COMMISSION",
    "COMMISSION_REBATE",
    "FEE_RETURN",
    "API_REBATE",
    "REFERRAL_KICKBACK",
}

REBATE_TYPES = {"COMMISSION_REBATE", "FEE_RETURN", "API_REBATE", "REFERRAL_KICKBACK"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_income_events(rows: list[dict[str, Any]]) -> int:
    """插入 income 流水，返回新增条数。"""
    if not rows:
        return 0
    synced_at = _now_iso()
    inserted = 0
    with get_db(write=True) as conn:
        for row in rows:
            income_type = str(row.get("incomeType") or "").upper()
            if income_type not in PNL_INCOME_TYPES:
                continue
            asset = str(row.get("asset") or "").upper()
            if asset and asset != "USDT":
                continue
            tran_id = str(row.get("tranId") or "")
            if not tran_id:
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO income_events
                   (symbol, income_type, income, asset, time_ms, tran_id, trade_id,
                    info, raw_json, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(row.get("symbol") or ""),
                    income_type,
                    float(row.get("income") or 0),
                    asset or "USDT",
                    int(row.get("time") or 0),
                    tran_id,
                    str(row.get("tradeId") or ""),
                    str(row.get("info") or ""),
                    json.dumps(row, ensure_ascii=False, default=str),
                    synced_at,
                ),
            )
            inserted += int(cur.rowcount or 0)
        conn.execute(
            """INSERT INTO income_sync_state(key, value, updated_at)
               VALUES ('last_sync_at', ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (synced_at, synced_at),
        )
    return inserted


def get_income_sync_state() -> dict[str, str]:
    with get_db() as conn:
        rows = conn.execute("SELECT key, value, updated_at FROM income_sync_state").fetchall()
    return {str(r["key"]): str(r["value"]) for r in rows}


def clear_income_cache() -> dict[str, int]:
    """清空本地 income/PnL 缓存，不影响 Binance 账户真实流水。"""
    with get_db(write=True) as conn:
        ev = conn.execute("DELETE FROM income_events")
        st = conn.execute("DELETE FROM income_sync_state")
    return {
        "deleted_events": int(ev.rowcount or 0),
        "deleted_sync_state": int(st.rowcount or 0),
    }


def list_income_events(
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where = ["asset='USDT'"]
    params: list[Any] = []
    if start_ms is not None:
        where.append("time_ms >= ?")
        params.append(int(start_ms))
    if end_ms is not None:
        where.append("time_ms <= ?")
        params.append(int(end_ms))
    params.extend([int(limit), int(offset)])
    sql = "SELECT * FROM income_events WHERE " + " AND ".join(where) + " ORDER BY time_ms DESC LIMIT ? OFFSET ?"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _period_key(dt: datetime, period: str) -> tuple[str, int]:
    if period == "weekly":
        y, w, _ = dt.isocalendar()
        return f"{y}-W{w:02d}", y * 100 + w
    if period == "monthly":
        return f"{dt.year:04d}-{dt.month:02d}", dt.year * 100 + dt.month
    return dt.date().isoformat(), int(dt.strftime("%Y%m%d"))


def aggregate_pnl(
    *,
    period: str = "daily",
    days: int = 90,
    tz_name: str = "Asia/Shanghai",
) -> list[dict[str, Any]]:
    period = period if period in {"daily", "weekly", "monthly"} else "daily"
    days = max(1, min(int(days), 365))
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86_400_000
    tz = ZoneInfo(tz_name)
    events = list_income_events(start_ms=start_ms, end_ms=end_ms, limit=100_000, offset=0)

    buckets: dict[str, dict[str, Any]] = {}
    order: dict[str, int] = {}
    for ev in events:
        dt = datetime.fromtimestamp(int(ev["time_ms"]) / 1000, timezone.utc).astimezone(tz)
        label, sort_key = _period_key(dt, period)
        if label not in buckets:
            buckets[label] = {
                "period": label,
                "net_pnl_usdt": 0.0,
                "realized_pnl_usdt": 0.0,
                "commission_usdt": 0.0,
                "funding_fee_usdt": 0.0,
                "rebate_usdt": 0.0,
                "event_count": 0,
            }
            order[label] = sort_key
        b = buckets[label]
        t = str(ev["income_type"]).upper()
        v = float(ev["income"] or 0)
        b["net_pnl_usdt"] += v
        b["event_count"] += 1
        if t == "REALIZED_PNL":
            b["realized_pnl_usdt"] += v
        elif t == "COMMISSION":
            b["commission_usdt"] += v
        elif t == "FUNDING_FEE":
            b["funding_fee_usdt"] += v
        elif t in REBATE_TYPES:
            b["rebate_usdt"] += v

    rows = [buckets[k] for k in sorted(buckets, key=lambda x: order[x], reverse=True)]
    for row in rows:
        for key, val in list(row.items()):
            if key.endswith("_usdt"):
                row[key] = round(float(val), 8)
    return rows


def pnl_totals(*, days: int = 90, tz_name: str = "Asia/Shanghai") -> dict[str, Any]:
    rows = aggregate_pnl(period="daily", days=days, tz_name=tz_name)
    sums: defaultdict[str, float] = defaultdict(float)
    events = 0
    for row in rows:
        events += int(row.get("event_count") or 0)
        for key in ("net_pnl_usdt", "realized_pnl_usdt", "commission_usdt", "funding_fee_usdt", "rebate_usdt"):
            sums[key] += float(row.get(key) or 0)
    return {
        "days": days,
        "net_pnl_usdt": round(sums["net_pnl_usdt"], 8),
        "realized_pnl_usdt": round(sums["realized_pnl_usdt"], 8),
        "commission_usdt": round(sums["commission_usdt"], 8),
        "funding_fee_usdt": round(sums["funding_fee_usdt"], 8),
        "rebate_usdt": round(sums["rebate_usdt"], 8),
        "event_count": events,
    }
