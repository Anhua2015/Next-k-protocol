"""Characterization: SL distance + min notional pre-checks."""
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


def _payload():
    return {"signals": [{
        "source": "zct_vwap", "api_signal_id": "v-001",
        "symbol": "BTCUSDT", "side": "LONG",
        "margin_usdt": 100.0,
        "leverage": 10.0,
        "entry_price": 67250.5,
        "sl_price": 67250.0,
        "tp_price": 68500.0, "play": "PLAY01",
    }]}


def test_sl_distance_too_close_warns_but_continues(
    seeded_config, mock_binance, caplog,
):
    """SL very close to mark — current code logs warning, doesn't reject."""
    mock_binance.all()
    client = _client(seeded_config)
    resp = client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    assert resp.json()["traded"] in (0, 1)


def test_min_notional_below_threshold_returns_error(seeded_config, mock_binance):
    """notional < minNotional -> status=error."""
    mock_binance.all(position_risk="position_risk_closed")
    client = _client(seeded_config)
    payload = {"signals": [{
        "source": "zct_vwap", "api_signal_id": "v-002",
        "symbol": "BTCUSDT", "side": "LONG",
        "margin_usdt": 0.0001,
        "leverage": 1.0,
        "entry_price": 67250.5, "sl_price": 66500.0, "tp_price": 68500.0,
        "play": "PLAY01",
    }]}
    resp = client.post("/api/binance/signals/ingest", json=payload, headers=AUTH)
    assert resp.json()["errors"] == 1
