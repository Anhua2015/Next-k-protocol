"""Free public macro feeds used by risk sizing / event quiet.

- Fear & Greed: https://api.alternative.me/fng/ (no key)
- Economic calendar: Forex Factory weekly JSON (primary) + FOMC dates fallback
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger("public_macros")

FNG_URL = "https://api.alternative.me/fng/"
FF_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FOMC_URL = "https://the-calendar.net/api/finance/fomc/2026.json"

_HEADERS = {
    "User-Agent": "NextK-QuantBot/1.0 (+https://github.com/edwarddddddd-77/Next-k-protocol)",
    "Accept": "application/json",
}

_ff_cache = {"ts": 0.0, "data": None}
_FF_TTL = 3600  # seconds


async def fear_greed(limit=30):
    async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as client:
        resp = await client.get(FNG_URL, params={"limit": str(max(1, int(limit)))})
        resp.raise_for_status()
        body = resp.json()
    rows = body.get("data") or []
    if not rows:
        return None
    vals = []
    for r in rows:
        try:
            vals.append(float(r.get("value")))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None
    series = list(reversed(vals))
    latest_row = rows[0]
    return {
        "latest": series[-1],
        "series": series[-30:],
        "label": latest_row.get("value_classification") or "",
        "source": "alternative.me",
    }


def _parse_ff_ts(raw):
    if not raw:
        return None
    s = str(raw).strip()
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        pass
    try:
        return int(parsedate_to_datetime(s).timestamp())
    except Exception:  # noqa: BLE001
        return None


def _parse_day_ts(date_str, hour_utc=18):
    """YYYY-MM-DD -> unix ts at ~14:00 ET (18:00 UTC, EDT approximation)."""
    try:
        y, m, d = [int(x) for x in str(date_str).split("-")[:3]]
        return int(datetime(y, m, d, hour_utc, 0, tzinfo=timezone.utc).timestamp())
    except Exception:  # noqa: BLE001
        return None


async def _fetch_ff_week(client):
    resp = await client.get(FF_WEEK_URL)
    if resp.status_code == 429:
        raise RuntimeError("FF calendar rate-limited (429)")
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list):
        return None
    events = []
    for r in rows:
        ts = _parse_ff_ts(r.get("date"))
        if not ts:
            continue
        impact = str(r.get("impact") or "").strip()
        country = str(r.get("country") or "").strip()
        title = str(r.get("title") or "").strip()
        if country:
            title = f"{country} {title}".strip()
        events.append({
            "ts": ts,
            "title": title[:80],
            "importance": impact,
            "country": country,
            "forecast": r.get("forecast") or "",
            "previous": r.get("previous") or "",
        })
    events.sort(key=lambda e: e["ts"])
    return {"events": events[:80], "source": "forexfactory"}


async def _fetch_fomc_fallback(client):
    year = datetime.now(timezone.utc).year
    url = f"https://the-calendar.net/api/finance/fomc/{year}.json"
    resp = await client.get(url)
    resp.raise_for_status()
    body = resp.json()
    events = []
    for m in body.get("meetings") or []:
        name = str(m.get("name") or "FOMC")
        note = str(m.get("note") or "")
        # Prefer rate-decision sessions (typically Day 2)
        if "Day 1" in name:
            continue
        if "Day 2" not in name and "rate decision" not in note.lower():
            continue
        ts = _parse_day_ts(m.get("date"))
        if not ts:
            continue
        events.append({
            "ts": ts,
            "title": f"USD {name}"[:80],
            "importance": "High",
            "country": "USD",
            "forecast": "",
            "previous": "",
        })
    events.sort(key=lambda e: e["ts"])
    return {"events": events[:40], "source": "fomc_calendar"}


async def econ_calendar():
    now = time.time()
    async with httpx.AsyncClient(timeout=20, headers=_HEADERS) as client:
        try:
            out = await _fetch_ff_week(client)
            if out and out.get("events"):
                _ff_cache["ts"] = now
                _ff_cache["data"] = out
                return out
        except Exception as exc:  # noqa: BLE001
            log.warning("FF calendar fetch failed: %s", exc)

        cached = _ff_cache.get("data")
        if cached and now - _ff_cache.get("ts", 0) < _FF_TTL * 6:
            log.info("using cached FF calendar (%d events)", len(cached.get("events") or []))
            return cached

        try:
            return await _fetch_fomc_fallback(client)
        except Exception as exc:  # noqa: BLE001
            log.warning("FOMC fallback failed: %s", exc)
            return cached  # may be None
