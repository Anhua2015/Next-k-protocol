"""Unit tests: fear_greed sizing + econ_cal event quiet gate entries."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

from backend import risk


CFG = {
    "event_quiet_minutes": 60,
    "fear_greed_extreme": {"greed": 85, "fear": 15, "size_mult": 0.6},
    "max_concurrent_coins": 6,
    "max_gross_leverage": 3,
    "max_position_pct_per_coin": 10,
    "risk_per_trade_pct": 0.5,
    "daily_loss_halt_pct": 3,
}


def _sig(side="long", strategy="S02_LIQ_REBOUND", symbol="BTCUSDT"):
    return SimpleNamespace(side=side, strategy=strategy, symbol=symbol, size_mult=1.0)


def test_fg_long_haircut_at_extreme_greed():
    with patch.object(risk.db, "get_factor", return_value={"latest": 90}):
        assert risk.size_multiplier("long", CFG) == 0.6
        assert risk.size_multiplier("short", CFG) == 1.0


def test_fg_short_haircut_at_extreme_fear():
    with patch.object(risk.db, "get_factor", return_value={"latest": 10}):
        assert risk.size_multiplier("short", CFG) == 0.6
        assert risk.size_multiplier("long", CFG) == 1.0


def test_fg_neutral_no_haircut():
    with patch.object(risk.db, "get_factor", return_value={"latest": 50}):
        assert risk.size_multiplier("long", CFG) == 1.0
        assert risk.size_multiplier("short", CFG) == 1.0


def test_event_quiet_blocks_usd_macro():
    now = time.time()
    cal = {
        "events": [
            {"ts": now + 1800, "title": "USD CPI m/m", "importance": "High", "country": "USD"},
        ]
    }
    with patch.object(risk.db, "get_factor", return_value=cal):
        assert "CPI" in risk.event_quiet(CFG)


def test_event_quiet_ignores_non_macro_high():
    now = time.time()
    cal = {
        "events": [
            {"ts": now + 1800, "title": "EUR Retail Sales", "importance": "High", "country": "EUR"},
        ]
    }
    with patch.object(risk.db, "get_factor", return_value=cal):
        assert risk.event_quiet(CFG) == ""


def test_allow_entry_blocked_by_quiet():
    now = time.time()
    cal = {
        "events": [
            {"ts": now + 900, "title": "USD FOMC Rate Decision", "importance": "High", "country": "USD"},
        ]
    }

    def _gf(name, *a, **k):
        if name == "econ_cal":
            return cal
        if name == "fear_greed":
            return {"latest": 50}
        return None

    with patch.object(risk.db, "get_factor", side_effect=_gf), \
         patch.object(risk.db, "get_meta", return_value="0"), \
         patch.object(risk.db, "open_positions", return_value=[]), \
         patch.object(risk, "halted", return_value=False):
        ok, why, mult = risk.allow_entry(_sig(), {}, 10_000, CFG)
        assert ok is False
        assert "事件静默" in why
        assert mult == 0


def test_allow_entry_applies_fg_mult():
    def _gf(name, *a, **k):
        if name == "econ_cal":
            return {"events": []}
        if name == "fear_greed":
            return {"latest": 90}
        return None

    with patch.object(risk.db, "get_factor", side_effect=_gf), \
         patch.object(risk.db, "get_meta", return_value="0"), \
         patch.object(risk.db, "open_positions", return_value=[]), \
         patch.object(risk.factors, "mark_price_of", return_value=100.0), \
         patch.object(risk, "halted", return_value=False):
        ok, why, mult = risk.allow_entry(_sig("long"), {"risk": {"leverage": 3}}, 10_000, CFG)
        assert ok is True
        assert mult == 0.6
