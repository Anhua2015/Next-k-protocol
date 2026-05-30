"""Characterization: LIMIT entry submit-only semantics."""
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
        "source": "zct_vwap",
        "api_signal_id": "l-001",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "margin_usdt": 100.0,
        "entry_price": 67000.0,
        "sl_price": 66500.0,
        "tp_price": 68500.0,
        "play": "PLAY01",
    }
    base.update(overrides)
    return {"signals": [base]}


def test_limit_entry_records_submitted_status(seeded_config, mock_binance):
    seeded_config.set_config("entry_type", "LIMIT")
    mock_binance.all(
        place_order="place_order_limit_ack",
        position_risk="position_risk_closed",
    )
    client = _client(seeded_config)

    resp = client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["traded"] == 1
    logs = client.get("/api/binance/signals?limit=10", headers=AUTH).json()
    assert logs[0]["status"] == "submitted"


def test_limit_entry_missing_entry_price_errors(seeded_config, mock_binance):
    seeded_config.set_config("entry_type", "LIMIT")
    mock_binance.all(position_risk="position_risk_closed")
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_payload(entry_price=None, api_signal_id="l-002"),
        headers=AUTH,
    )

    assert resp.json()["errors"] == 1
