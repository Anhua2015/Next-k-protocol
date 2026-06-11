"""Characterization: ingest pipeline (dedup only)."""
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
        "source": "orb",
        "api_signal_id": "sig-001",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "margin_usdt": 100.0,
        "leverage": 10.0,
        "entry_price": 67250.5,
        "sl_price": 66500.0,
        "tp_price": 68500.0,
        "play": "PLAY01",
    }
    base.update(overrides)
    return {"signals": [base]}


def test_duplicate_signal_skipped(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_closed")
    client = _client(seeded_config)
    client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    resp = client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    body = resp.json()
    assert body["details"][0]["action"] == "duplicate"


def test_any_source_accepted(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_closed")
    client = _client(seeded_config)
    resp = client.post(
        "/api/binance/signals/ingest",
        json=_payload(source="legacy_foo", api_signal_id="sig-002"),
        headers=AUTH,
    )
    body = resp.json()
    assert body["details"][0]["action"] != "skipped_invalid_source"


def test_open_not_blocked_when_position_exists(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_open")
    client = _client(seeded_config)
    resp = client.post(
        "/api/binance/signals/ingest",
        json=_payload(api_signal_id="sig-004"),
        headers=AUTH,
    )
    body = resp.json()
    assert body["details"][0]["action"] != "skipped_position_exists"
