"""Unit: execute_trade trading gate."""
from __future__ import annotations

import sys

import pytest


@pytest.mark.parametrize("enabled", ["false", "0", ""])
def test_execute_trade_rejects_when_trading_disabled(seeded_config, enabled):
    seeded_config.set_config("enabled", enabled)
    seeded_config.insert_signal(
        source="orb",
        api_signal_id="sig-disabled",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=67250.0,
        sl_price=66500.0,
        tp_price=None,
        confidence="high",
        regime=None,
        notional_usdt=None,
        received_at="2026-06-07T00:00:00+00:00",
        status="received",
    )
    rows = seeded_config.list_signals(limit=1)
    signal_log_id = rows[0]["id"]

    sys.modules.pop("trader", None)
    from trader import execute_trade

    ok = execute_trade(
        {
            "signal_log_id": signal_log_id,
            "symbol": "BTCUSDT",
            "side": "LONG",
            "source": "orb",
            "margin_usdt": 100.0,
            "leverage": 5,
            "sl_price": 66500.0,
            "action": "open",
        }
    )

    assert ok is False
    row = seeded_config.list_signals(limit=1)[0]
    assert row["status"] == "error"
    assert row["skip_reason"] == "trading_disabled"
