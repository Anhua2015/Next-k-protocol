"""Bitget USDT-FUTURES liquidations — fallback when OKX is unavailable.

Binance REST allForceOrders is retired. Prefer okx_market; this module is the
secondary public source (~3 days history, often higher lag).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

import httpx

log = logging.getLogger("bitget_market")

BASE = "https://api.bitget.com"
CATEGORY = "USDT-FUTURES"


async def _get(client, path, params=None):
    resp = await client.get(BASE + path, params=params or {})
    resp.raise_for_status()
    body = resp.json()
    if str(body.get("code")) != "00000":
        raise RuntimeError(f"Bitget {path}: {body.get('code')} {body.get('msg')}")
    return body.get("data")


async def liquidations(symbol, limit=100, pages=5):
    """Recent liquidation prints (newest first; cursor pages older)."""
    rows = []
    cursor = None
    async with httpx.AsyncClient(timeout=15) as client:
        for _ in range(max(1, pages)):
            params = {"category": CATEGORY, "symbol": symbol, "limit": str(min(int(limit), 100))}
            if cursor:
                params["cursor"] = cursor
            data = await _get(client, "/api/v3/market/liquidations", params)
            if not isinstance(data, dict):
                break
            batch = data.get("list") or []
            rows.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not batch:
                break
    return rows


def _liq_row(row):
    px = float(row.get("price") or 0)
    qty = float(row.get("amount") or row.get("qty") or 0)
    ts = float(row.get("ts") or 0)
    side = str(row.get("side") or "").lower()
    return px, qty, ts, side


def _anchor_ms(rows_ts, now_ms=None):
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    if not rows_ts:
        return now_ms
    newest = max(rows_ts)
    return min(now_ms, newest) if newest > 0 else now_ms


async def liq_agg(symbol):
    """Hourly long/short liquidation USD + spike multiples.

    Bitget side: buy = long liq, sell = short liq.
    Windows are anchored to the newest print to tolerate API lag.
    """
    try:
        rows = await liquidations(symbol, limit=100, pages=8)
    except Exception as exc:  # noqa: BLE001
        log.warning("bitget liq_agg %s: %s", symbol, exc)
        return None

    parsed = []
    for r in rows or []:
        px, qty, ts, side = _liq_row(r)
        if px <= 0 or qty <= 0 or ts <= 0 or side not in ("buy", "sell"):
            continue
        parsed.append((px * qty, ts, "long" if side == "buy" else "short"))

    anchor = _anchor_ms([t for _, t, _ in parsed])
    long_h = defaultdict(float)
    short_h = defaultdict(float)
    long_1h = short_1h = 0.0
    for usd, ts, side in parsed:
        if anchor - 3_600_000 < ts <= anchor:
            if side == "long":
                long_1h += usd
            else:
                short_1h += usd
        hour = int(ts // 3_600_000) * 3600
        if side == "long":
            long_h[hour] += usd
        else:
            short_h[hour] += usd

    cur_hour = int(anchor // 3_600_000) * 3600
    hours = [cur_hour - 3600 * i for i in range(47, -1, -1)]
    prior_l = [long_h[h] for h in hours[:-1] if long_h[h] > 0]
    prior_s = [short_h[h] for h in hours[:-1] if short_h[h] > 0]
    avg_l = sum(prior_l) / len(prior_l) if prior_l else 0.0
    avg_s = sum(prior_s) / len(prior_s) if prior_s else 0.0
    return {
        "long_1h": long_1h,
        "short_1h": short_1h,
        "long_mult": long_1h / avg_l if avg_l else 0.0,
        "short_mult": short_1h / avg_s if avg_s else 0.0,
        "source": "bitget",
        "hours": max(len(prior_l), len(prior_s)),
        "events": len(parsed),
        "lag_sec": max(0.0, (time.time() * 1000 - anchor) / 1000.0),
    }


async def liq_orders(symbol, min_usd=100_000.0):
    """Large liquidation count in 10m / prior 20m windows (anchor = newest print)."""
    try:
        rows = await liquidations(symbol, limit=100, pages=3)
    except Exception as exc:  # noqa: BLE001
        log.warning("bitget liq_orders %s: %s", symbol, exc)
        return None

    parsed = []
    for r in rows or []:
        px, qty, ts, _ = _liq_row(r)
        if px <= 0 or qty <= 0 or ts <= 0:
            continue
        parsed.append((px * qty, ts))

    anchor = _anchor_ms([t for _, t in parsed])
    recent = older = 0
    for usd, ts in parsed:
        if usd < min_usd:
            continue
        age = anchor - ts
        if 0 <= age < 600_000:
            recent += 1
        elif 600_000 <= age < 1_800_000:
            older += 1
    return {
        "n_10m": recent,
        "n_prev_20m": older,
        "source": "bitget",
        "lag_sec": max(0.0, (time.time() * 1000 - anchor) / 1000.0),
    }
