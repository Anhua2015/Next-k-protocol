"""Characterization: hedge/one-way mode affects positionSide param."""
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
        "source": "zct_vwap", "api_signal_id": "h-001",
        "symbol": "BTCUSDT", "side": "LONG",
        "entry_price": 67250.5, "sl_price": 66500.0, "tp_price": 68500.0,
        "play": "PLAY01",
    }]}


def test_hedge_mode_includes_position_side(seeded_config, mock_binance, httpx_mock):
    mock_binance.all(position_side="position_side_dual")
    client = _client(seeded_config)
    resp = client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    assert resp.json()["traded"] == 1
    order_calls = [
        r for r in httpx_mock.get_requests()
        if "/fapi/v1/order" in str(r.url)
    ]
    assert any("positionSide=LONG" in str(r.url) for r in order_calls)


def test_one_way_mode_omits_position_side(seeded_config, mock_binance, httpx_mock):
    mock_binance.all(position_side="position_side_single")
    client = _client(seeded_config)
    resp = client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    assert resp.json()["traded"] == 1
    order_calls = [
        r for r in httpx_mock.get_requests()
        if "/fapi/v1/order" in str(r.url)
    ]
    assert all("positionSide" not in str(r.url) for r in order_calls)
