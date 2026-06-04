"""Unit: ingest/guards.py — guard functions."""
from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from ingest.guards import (
    GUARDS,
    guard_close_requires_position,
    guard_dedup_insert,
    guard_invalid_source,
    guard_max_positions,
    guard_position_exists,
    guard_source_disabled,
)


@dataclass
class FakeSignal:
    source: str = "zct_vwap"
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
    max_pos: int = 8
    play_max: dict = field(default_factory=lambda: {"play01": 5, "play02": 5, "play03": 5})
    source_max: dict = field(default_factory=dict)


class TestGuardInvalidSource:
    def test_valid_source_passes(self):
        sig = FakeSignal(source="zct_vwap")
        d = guard_invalid_source(sig, None)
        assert not d.skip

    def test_invalid_source_skips(self):
        sig = FakeSignal(source="foo")
        d = guard_invalid_source(sig, None)
        assert d.skip
        assert d.action == "skipped_invalid_source"

    def test_momentum_valid(self):
        assert not guard_invalid_source(FakeSignal(source="momentum"), None).skip

    def test_jiezhen_valid(self):
        assert not guard_invalid_source(FakeSignal(source="jiezhen"), None).skip

    def test_moss_quant_valid(self):
        assert not guard_invalid_source(FakeSignal(source="moss_quant"), None).skip

    def test_moss2_valid(self):
        assert not guard_invalid_source(FakeSignal(source="moss2"), None).skip


class TestGuardSourceDisabled:
    def test_enabled_passes(self):
        ctx = FakeCtx()
        ctx.db.source_enabled.return_value = True
        d = guard_source_disabled(FakeSignal(), ctx)
        assert not d.skip

    def test_disabled_skips(self):
        ctx = FakeCtx()
        ctx.db.source_enabled.return_value = False
        d = guard_source_disabled(FakeSignal(), ctx)
        assert d.skip
        assert d.action == "skipped_source_disabled"


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


class TestGuardPositionExists:
    @patch("trader.list_live_positions", return_value=[])
    def test_no_position_passes(self, _mock):
        d = guard_position_exists(FakeSignal(action="open"), FakeCtx())
        assert not d.skip

    @patch(
        "trader.list_live_positions",
        return_value=[{"symbol": "BTCUSDT", "positionAmt": "0.01"}],
    )
    def test_position_exists_skips_open(self, _mock):
        d = guard_position_exists(FakeSignal(action="open"), FakeCtx())
        assert d.skip
        assert d.action == "skipped_position_exists"

    @patch(
        "trader.list_live_positions",
        return_value=[{"symbol": "BTCUSDT", "positionAmt": "0.01"}],
    )
    def test_close_passes_when_position_exists(self, _mock):
        d = guard_position_exists(FakeSignal(action="close"), FakeCtx())
        assert not d.skip


class TestGuardCloseRequiresPosition:
    @patch("trader.list_live_positions", return_value=[])
    def test_close_without_position_skips(self, _mock):
        d = guard_close_requires_position(FakeSignal(action="close"), FakeCtx())
        assert d.skip
        assert d.action == "skipped_no_position"

    @patch(
        "trader.list_live_positions",
        return_value=[{"symbol": "BTCUSDT", "positionAmt": "0.01"}],
    )
    def test_close_with_position_passes(self, _mock):
        d = guard_close_requires_position(FakeSignal(action="close"), FakeCtx())
        assert not d.skip


class TestGuardMaxPositions:
    @patch("trader.list_live_positions", return_value=[{"symbol": "ETHUSDT", "positionAmt": "1"}])
    def test_under_limit_passes(self, _mock):
        ctx = FakeCtx(max_pos=8)
        d = guard_max_positions(FakeSignal(action="open"), ctx)
        assert not d.skip

    @patch("trader.list_live_positions", return_value=[{"symbol": "X"}] * 8)
    def test_global_at_limit_skips_open(self, _mock):
        ctx = FakeCtx(max_pos=8)
        d = guard_max_positions(FakeSignal(action="open"), ctx)
        assert d.skip

    @patch("trader.list_live_positions", return_value=[{"symbol": "X"}] * 8)
    def test_close_not_blocked_by_max_positions(self, _mock):
        ctx = FakeCtx(max_pos=8)
        d = guard_max_positions(FakeSignal(action="close"), ctx)
        assert not d.skip


class TestGuardChain:
    def test_all_guards_registered(self):
        assert len(GUARDS) >= 7
