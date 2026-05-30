from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.characterization

AUTH = {"X-Maintenance-Token": "test-token"}


def _client():
    for mod in ("main", "router", "trader"):
        sys.modules.pop(mod, None)
    import main

    importlib.reload(main)
    return TestClient(main.app)


def test_positions_open_reads_binance_position_risk(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_open")
    client = _client()

    resp = client.get("/api/binance/positions?status=open", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["symbol"] == "BTCUSDT"
    assert body[0]["side"] == "LONG"
    assert "profile_id" not in body[0]


def test_positions_closed_is_gone(seeded_config):
    client = _client()

    resp = client.get("/api/binance/positions?status=closed", headers=AUTH)

    assert resp.status_code == 410


def test_pnl_summary_endpoint_removed(seeded_config):
    client = _client()

    resp = client.get("/api/binance/pnl/summary", headers=AUTH)

    assert resp.status_code in (404, 410)
