"""Characterization: MARKET entry full happy path + key failure branches."""
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
        "api_signal_id": "m-001",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry_price": 67250.5,
        "sl_price": 66500.0,
        "tp_price": 68500.0,
        "play": "PLAY01",
    }
    base.update(overrides)
    return {"signals": [base]}


def test_market_entry_full_flow(seeded_config, mock_binance):
    client = _client(seeded_config)
    resp = client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    body = resp.json()
    assert body["traded"] == 1
    pos_list = client.get("/api/binance/positions?status=open", headers=AUTH).json()
    assert len(pos_list) == 1
    assert pos_list[0]["symbol"] == "BTCUSDT"
    assert pos_list[0]["side"] == "LONG"
    assert pos_list[0]["entry_price"] == pytest.approx(67250.50, rel=1e-6)
    assert pos_list[0]["sl_price"] == pytest.approx(66500.0, rel=1e-6)
    assert pos_list[0]["tp_price"] == pytest.approx(68500.0, rel=1e-6)


def test_market_entry_short_uses_sell_side(seeded_config, mock_binance):
    client = _client(seeded_config)
    resp = client.post(
        "/api/binance/signals/ingest",
        json=_payload(
            side="SHORT", api_signal_id="m-002",
            sl_price=68000.0, tp_price=66000.0,
        ),
        headers=AUTH,
    )
    assert resp.json()["traded"] == 1


def test_market_entry_min_notional_rejection(seeded_config, mock_binance):
    """margin*leverage / mark_px * mark_px < minNotional -> status=error."""
    seeded_config.set_config("margin_usdt", "0.01")
    seeded_config.set_config("leverage", "1")
    client = _client(seeded_config)
    resp = client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    body = resp.json()
    assert body["errors"] == 1
    assert body["traded"] == 0


def test_market_entry_invalid_margin_returns_error(seeded_config, mock_binance):
    seeded_config.set_config("margin_usdt", "0")
    client = _client(seeded_config)
    resp = client.post("/api/binance/signals/ingest", json=_payload(), headers=AUTH)
    assert resp.json()["errors"] == 1
