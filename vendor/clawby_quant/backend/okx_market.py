"""OKX public liquidation history — primary source for liq_agg / liq_orders.

Binance REST allForceOrders is retired. Bitget liquidations lag ~1h+;
OKX /api/v5/public/liquidation-orders is fresher (~20-30m) and needs no key.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

import httpx

log = logging.getLogger("okx_market")

BASE = "https://www.okx.com"

_ctval: dict[str, float] = {}  # uly -> contract value in base coin


def to_uly(symbol: str) -> str:
    """BTCUSDT -> BTC-USDT (OKX underlying)."""
    s = (symbol or "").upper().replace("-", "").replace("_", "")
    if s.endswith("USDT"):
        return f"{s[:-4]}-USDT"
    if s.endswith("USD"):
        return f"{s[:-3]}-USD"
    return symbol


async def _get(path, params=None, timeout=15):
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(BASE + path, params=params or {})
        resp.raise_for_status()
        body = resp.json()
    if str(body.get("code")) != "0":
        raise RuntimeError(f"OKX {path}: {body.get('code')} {body.get('msg')}")
    return body.get("data")


async def _ct_val(uly: str) -> float:
    if uly in _ctval:
        return _ctval[uly]
    rows = await _get("/api/v5/public/instruments", {"instType": "SWAP", "uly": uly})
    for r in rows or []:
        u = r.get("uly") or uly
        try:
            _ctval[u] = float(r.get("ctVal") or 0)
        except (TypeError, ValueError):
            _ctval[u] = 0.0
    if uly not in _ctval:
        _ctval[uly] = 0.01 if uly.startswith("BTC") else 0.1
        log.warning("okx ctVal missing for %s — fallback %s", uly, _ctval[uly])
    return _ctval[uly]


async def liquidations(symbol, limit=100):
    """Filled liquidation prints for USDT-margined swap underlying."""
    uly = to_uly(symbol)
    data = await _get(
        "/api/v5/public/liquidation-orders",
        {"instType": "SWAP", "uly": uly, "state": "filled", "limit": str(min(int(limit), 100))},
    )
    if not data:
        return []
    # Response is a list of buckets; each has details[]
    out = []
    ct = await _ct_val(uly)
    for bucket in data if isinstance(data, list) else []:
        for row in bucket.get("details") or []:
            try:
                px = float(row.get("bkPx") or row.get("px") or 0)
                sz = float(row.get("sz") or 0)
                ts = float(row.get("ts") or row.get("time") or 0)
            except (TypeError, ValueError):
                continue
            pos = str(row.get("posSide") or "").lower()
            side = str(row.get("side") or "").lower()
            # Prefer posSide: long/short position liquidated.
            if pos in ("long", "short"):
                liq_side = pos
            elif side == "sell":
                liq_side = "long"
            elif side == "buy":
                liq_side = "short"
            else:
                continue
            if px <= 0 or sz <= 0 or ts <= 0:
                continue
            usd = sz * ct * px
            out.append({"side": liq_side, "price": px, "qty": sz * ct, "usd": usd, "ts": ts})
    out.sort(key=lambda r: r["ts"])
    return out


def _anchor_ms(rows, now_ms=None):
    """Use newest print as clock so API lag does not zero the 1h / 10m windows."""
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    if not rows:
        return now_ms
    newest = max(float(r["ts"]) for r in rows)
    return min(now_ms, newest) if newest > 0 else now_ms


async def liq_agg(symbol):
    try:
        rows = await liquidations(symbol, limit=100)
    except Exception as exc:  # noqa: BLE001
        log.warning("okx liq_agg %s: %s", symbol, exc)
        return None

    anchor = _anchor_ms(rows)
    long_h = defaultdict(float)
    short_h = defaultdict(float)
    long_1h = short_1h = 0.0
    for r in rows:
        ts = float(r["ts"])
        usd = float(r["usd"])
        side = r["side"]
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
    # exclude current (possibly partial / lag-skewed) hour from baseline
    prior_l = [long_h[h] for h in hours[:-1] if long_h[h] > 0]
    prior_s = [short_h[h] for h in hours[:-1] if short_h[h] > 0]
    avg_l = sum(prior_l) / len(prior_l) if prior_l else 0.0
    avg_s = sum(prior_s) / len(prior_s) if prior_s else 0.0
    return {
        "long_1h": long_1h,
        "short_1h": short_1h,
        "long_mult": long_1h / avg_l if avg_l else 0.0,
        "short_mult": short_1h / avg_s if avg_s else 0.0,
        "source": "okx",
        "hours": max(len(prior_l), len(prior_s)),
        "events": len(rows),
        "lag_sec": max(0.0, (time.time() * 1000 - anchor) / 1000.0),
    }


async def liq_orders(symbol, min_usd=100_000.0):
    try:
        rows = await liquidations(symbol, limit=100)
    except Exception as exc:  # noqa: BLE001
        log.warning("okx liq_orders %s: %s", symbol, exc)
        return None

    anchor = _anchor_ms(rows)
    recent = older = 0
    for r in rows:
        usd = float(r["usd"])
        ts = float(r["ts"])
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
        "source": "okx",
        "lag_sec": max(0.0, (time.time() * 1000 - anchor) / 1000.0),
    }
