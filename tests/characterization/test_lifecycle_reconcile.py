"""Characterization: reconcile_pending_entries promote / cancel / no-op."""
from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.characterization


def _pending_row(db, deadline="2099-01-01T00:00:00+00:00", **overrides):
    base = dict(
        signal_log_id=None, symbol="BTCUSDT", side="LONG",
        entry_order_id="22222222",
        entry_price=67000.0, sl_price=66500.0, tp_price=68500.0,
        quantity=0.012, notional_usdt=100.0, leverage=10,
        opened_at="2026-05-27T00:00:00+00:00", entry_deadline=deadline,
        play="PLAY01", source="zct_vwap",
    )
    base.update(overrides)
    return db.insert_pending_position(**base)


def test_reconcile_promotes_filled(seeded_config, mock_binance):
    _pending_row(seeded_config)
    mock_binance.all(get_order="get_order_filled")
    sys.modules.pop("trader", None)
    from trader import reconcile_pending_entries
    reconcile_pending_entries()
    assert len(seeded_config.get_open_positions()) == 1
    assert len(seeded_config.get_pending_entries()) == 0


def test_reconcile_keeps_pending_when_not_filled(seeded_config, mock_binance):
    _pending_row(seeded_config)
    mock_binance.all(get_order="get_order_pending")
    sys.modules.pop("trader", None)
    from trader import reconcile_pending_entries
    reconcile_pending_entries()
    assert len(seeded_config.get_pending_entries()) == 1


def test_reconcile_cancels_past_deadline(seeded_config, mock_binance):
    _pending_row(seeded_config, deadline="1999-01-01T00:00:00+00:00")
    mock_binance.all(get_order="get_order_pending")
    sys.modules.pop("trader", None)
    from trader import reconcile_pending_entries
    reconcile_pending_entries()
    assert len(seeded_config.get_pending_entries()) == 0
