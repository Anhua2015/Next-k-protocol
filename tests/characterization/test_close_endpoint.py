"""Characterization: POST /positions/close paper-close path."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.characterization

AUTH = {"X-Maintenance-Token": "test-token"}


def _client(seeded_config):
    import importlib
    import sys
    for mod in ("main", "router", "trader"):
        sys.modules.pop(mod, None)
    import main
    importlib.reload(main)
    return TestClient(main.app)


def _open_btc(db):
    return db.insert_position(
        signal_log_id=None, symbol="BTCUSDT", side="LONG",
        entry_order_id="11111111", sl_order_id="33333333", tp_order_id="33333334",
        entry_price=67250.5, sl_price=66500.0, tp_price=68500.0,
        quantity=0.012, notional_usdt=100.0, leverage=10,
        opened_at="2026-05-27T00:00:00+00:00", play="PLAY01", source="zct_vwap",
    )


def test_close_endpoint_marks_closed(seeded_config, mock_binance):
    _open_btc(seeded_config)
    mock_binance.all()
    client = _client(seeded_config)
    resp = client.post(
        "/api/binance/positions/close",
        json={
            "source": "zct_vwap", "api_signal_id": "x-1",
            "symbol": "BTCUSDT", "side": "LONG",
            "exit_rule": "trail_stop", "close_price": 67500.0,
        },
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert len(seeded_config.get_open_positions()) == 0


def test_close_endpoint_missing_position_returns_error(
    seeded_config, mock_binance,
):
    client = _client(seeded_config)
    resp = client.post(
        "/api/binance/positions/close",
        json={
            "source": "zct_vwap", "api_signal_id": "x-2",
            "symbol": "NONEXIST", "side": "LONG",
            "exit_rule": "trail_stop", "close_price": 1.0,
        },
        headers=AUTH,
    )
    assert resp.status_code in (200, 404)
    assert "status" in resp.json() or "detail" in resp.json()
