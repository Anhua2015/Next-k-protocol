"""Unit: ingest/guards.py ? guard functions."""
from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

from ingest.guards import GUARDS, guard_dedup_insert


@dataclass
class FakeSignal:
    source: str = "orb"
    api_signal_id: str = "sig-1"
    symbol: str = "BTCUSDT"
    side: str = "LONG"
    entry_price: float = 67250.5
    sl_price: float = 66500.0
    tp_price: float = 68500.0
    confidence: str = "high"
    regime: str = "TREND_UP"
    notional_usdt: float = 100.0
    play: str = "PLAY01"
    action: str = "open"


@dataclass
class FakeCtx:
    db: MagicMock = field(default_factory=MagicMock)


class TestGuardDedupInsert:
    def test_new_signal_returns_signal_log_id(self):
        ctx = FakeCtx()
        ctx.db.insert_signal.return_value = 42
        d = guard_dedup_insert(FakeSignal(), ctx)
        assert not d.skip
        assert d.signal_log_id == 42

    def test_duplicate_skips(self):
        ctx = FakeCtx()
        ctx.db.insert_signal.return_value = None
        d = guard_dedup_insert(FakeSignal(), ctx)
        assert d.skip
        assert d.action == "duplicate"


class TestGuardChain:
    def test_only_dedup_guard_registered(self):
        assert GUARDS == [guard_dedup_insert]
