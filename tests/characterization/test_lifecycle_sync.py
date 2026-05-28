"""Characterization: sync_open_positions detects external/SL/TP closes."""
from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.characterization


def _open_position_row(db, **overrides):
    base = dict(
        signal_log_id=None, symbol="BTCUSDT", side="LONG",
        entry_order_id="11111111", sl_order_id="33333333", tp_order_id="33333334",
        entry_price=67250.5, sl_price=66500.0, tp_price=68500.0,
        quantity=0.012, notional_usdt=100.0, leverage=10,
        opened_at="2026-05-27T00:00:00+00:00", play="PLAY01", source="zct_vwap",
    )
    base.update(overrides)
    return db.insert_position(**base)


def test_sync_detects_position_closed(seeded_config, mock_binance):
    _open_position_row(seeded_config)
    mock_binance.all(position_risk="position_risk_closed")
    # Reload trader so it picks up the reloaded db module
    sys.modules.pop("trader", None)
    from trader import sync_open_positions
    sync_open_positions()
    assert len(seeded_config.get_open_positions()) == 0


def test_sync_keeps_position_when_still_open(seeded_config, mock_binance):
    _open_position_row(seeded_config)
    mock_binance.all(position_risk="position_risk_open")
    sys.modules.pop("trader", None)
    from trader import sync_open_positions
    sync_open_positions()
    assert len(seeded_config.get_open_positions()) == 1
