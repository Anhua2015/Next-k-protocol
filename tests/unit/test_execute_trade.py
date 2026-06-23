"""Unit: execute_trade execution pause gate."""
from __future__ import annotations

import sys

import pytest


def test_execute_trade_rejects_when_execution_paused(seeded_config):
    seeded_config.insert_signal(
        source="orb",
        api_signal_id="sig-paused",
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
    import trader

    trader._pause_execution()
    try:
        ok = trader.execute_trade(
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
    finally:
        trader.notify_binance_auth_success()

    assert ok is False
    row = seeded_config.list_signals(limit=1)[0]
    assert row["status"] == "error"
    assert row["skip_reason"] == "execution_paused"
