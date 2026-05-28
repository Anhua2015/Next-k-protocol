"""Characterization: LIMIT entry + pending lifecycle."""
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


def _payload(**overrides):
    base = {
        "source": "zct_vwap", "api_signal_id": "l-001",
        "symbol": "BTCUSDT", "side": "LONG",
        "entry_price": 67000.0, "sl_price": 66500.0, "tp_price": 68500.0,
        "play": "PLAY01",
    }
    base.update(overrides)
    return {"signals": [base]}


def test_limit_entry_creates_pending(seeded_config, mock_binance):
    seeded_config.set_config("entry_type", "LIMIT")
    mock_binance.all(place_order="place_order_limit_ack")
    client = _client(seeded_config)
    resp = client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    assert resp.json()["traded"] == 1
    logs = client.get("/api/binance/signals?limit=10", headers=AUTH).json()
    assert logs[0]["status"] == "pending_entry"


def test_limit_entry_missing_entry_price_errors(seeded_config, mock_binance):
    seeded_config.set_config("entry_type", "LIMIT")
    mock_binance.all()
    client = _client(seeded_config)
    resp = client.post(
        "/api/binance/signals/ingest",
        json=_payload(entry_price=None, api_signal_id="l-002"),
        headers=AUTH,
    )
    assert resp.json()["errors"] == 1


def test_reconcile_promotes_filled_pending(seeded_config, mock_binance):
    seeded_config.set_config("entry_type", "LIMIT")
    mock_binance.all(place_order="place_order_limit_ack")
    client = _client(seeded_config)
    client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    mock_binance.all(get_order="get_order_filled")
    from trader import reconcile_pending_entries
    reconcile_pending_entries()
    pos = client.get("/api/binance/positions?status=open", headers=AUTH).json()
    assert len(pos) == 1


def test_reconcile_cancels_timeout(seeded_config, mock_binance):
    seeded_config.set_config("entry_type", "LIMIT")
    seeded_config.set_config("limit_entry_timeout_sec", "0")
    mock_binance.all(place_order="place_order_limit_ack")
    client = _client(seeded_config)
    client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    mock_binance.all(get_order="get_order_pending")
    from trader import reconcile_pending_entries
    reconcile_pending_entries()
    pos = client.get("/api/binance/positions?status=open", headers=AUTH).json()
    assert len(pos) == 0
