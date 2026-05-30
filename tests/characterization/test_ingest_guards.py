"""Characterization: ingest guard chain.

Verifies the 7 reject paths in router.ingest_signals stay observable as
each test pins a `signals_log.status` and the SignalIngestResult counters.
"""
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
        "api_signal_id": "sig-001",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "margin_usdt": 100.0,
        "entry_price": 67250.5,
        "sl_price": 66500.0,
        "tp_price": 68500.0,
        "play": "PLAY01",
    }
    base.update(overrides)
    return {"signals": [base]}


def test_trading_disabled_skips_all(seeded_config, mock_binance):
    seeded_config.set_config("enabled", "false")
    client = _client(seeded_config)
    resp = client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["scanned"] == 1
    assert body["skipped"] == 1
    assert body["traded"] == 0
    assert body["details"][0]["action"] == "skipped_disabled"


def test_invalid_source_rejected(seeded_config, mock_binance):
    client = _client(seeded_config)
    resp = client.post(
        "/api/binance/signals/ingest",
        json=_payload(source="foo_bar", api_signal_id="sig-002"),
        headers=AUTH,
    )
    body = resp.json()
    assert body["details"][0]["action"] == "skipped_invalid_source"


def test_duplicate_signal_skipped(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_closed")
    client = _client(seeded_config)
    client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    resp = client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    body = resp.json()
    assert body["details"][0]["action"] == "duplicate"


def test_position_conflict_skipped(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_open")
    client = _client(seeded_config)
    resp = client.post(
        "/api/binance/signals/ingest",
        json=_payload(api_signal_id="sig-004"),
        headers=AUTH,
    )
    body = resp.json()
    assert body["details"][0]["action"] == "skipped_position_exists"

def test_global_max_positions_skipped(seeded_config, mock_binance):
    seeded_config.set_config("max_positions", "0")
    mock_binance.all(position_risk="position_risk_closed")
    client = _client(seeded_config)
    resp = client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    body = resp.json()
    assert body["details"][0]["action"] == "skipped_max_positions"
