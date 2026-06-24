from __future__ import annotations

import importlib
import sys

import httpx
import pytest

from tests.characterization.client import protocol_test_client

pytestmark = pytest.mark.characterization


def _client():
    for mod in ("main", "router", "trader"):
        sys.modules.pop(mod, None)
    import main

    importlib.reload(main)
    return protocol_test_client(main.app)


def test_positions_open_reads_binance_position_risk(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_open")
    client = _client()

    resp = client.get("/api/binance/positions?status=open")

    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["symbol"] == "BTCUSDT"
    assert body[0]["side"] == "LONG"
    assert "profile_id" not in body[0]


def test_positions_closed_is_gone(seeded_config):
    client = _client()

    resp = client.get("/api/binance/positions?status=closed")

    assert resp.status_code == 410


def test_positions_upstream_failure_returns_502(seeded_config, monkeypatch):
    import trader

    def boom():
        req = httpx.Request("GET", "https://fapi.binance.com/fapi/v2/positionRisk")
        resp = httpx.Response(401, request=req)
        raise httpx.HTTPStatusError("boom", request=req, response=resp)

    monkeypatch.setattr(trader, "list_live_positions", boom)
    client = _client()

    resp = client.get("/api/binance/positions?status=open")

    assert resp.status_code == 502
    assert resp.json()["detail"] == "positions_failed"


def test_pnl_summary_endpoint_returns_empty_cache(seeded_config):
    client = _client()

    resp = client.get("/api/binance/pnl/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["period"] == "daily"
    assert body["rows"] == []
    assert body["totals"]["net_pnl_usdt"] == 0
