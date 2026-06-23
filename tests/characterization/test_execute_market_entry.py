"""Characterization: MARKET entry full happy path + key failure branches."""
from __future__ import annotations

import pytest

from tests.characterization.client import protocol_test_client

pytestmark = pytest.mark.characterization


def _client(seeded_config):
    import importlib
    import sys
    for mod in ("main", "router", "trader"):
        sys.modules.pop(mod, None)
    import main
    importlib.reload(main)
    return protocol_test_client(main.app)


def _payload(**overrides):
    base = {
        "source": "orb",
        "api_signal_id": "m-001",
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


def test_market_entry_full_flow(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_closed")
    client = _client(seeded_config)
    resp = client.post("/api/binance/signals/ingest", json=_payload())
    body = resp.json()
    assert body["traded"] == 1
    mock_binance.all(position_risk="position_risk_open")
    pos_list = client.get("/api/binance/positions?status=open").json()
    assert len(pos_list) == 1
    assert pos_list[0]["symbol"] == "BTCUSDT"
    assert pos_list[0]["side"] == "LONG"
    assert pos_list[0]["entry_price"] == pytest.approx(67250.50, rel=1e-6)
    assert pos_list[0]["quantity"] == pytest.approx(0.012, rel=1e-6)
    assert pos_list[0]["unrealized_pnl_usdt"] == pytest.approx(1.20, rel=1e-6)


def test_market_entry_short_uses_sell_side(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_closed")
    client = _client(seeded_config)
    resp = client.post(
        "/api/binance/signals/ingest",
        json=_payload(
            side="SHORT", api_signal_id="m-002",
            sl_price=68000.0, tp_price=66000.0,
        ),
    )
    assert resp.json()["traded"] == 1


def test_market_entry_min_notional_rejection(seeded_config, mock_binance):
    """margin*leverage / mark_px * mark_px < minNotional -> status=error."""
    mock_binance.all(position_risk="position_risk_closed")
    client = _client(seeded_config)
    resp = client.post(
        "/api/binance/signals/ingest",
        json=_payload(margin_usdt=0.01, leverage=1.0),
    )
    body = resp.json()
    assert body["errors"] == 1
    assert body["traded"] == 0


def test_market_entry_invalid_margin_returns_error(seeded_config, mock_binance):
    mock_binance.all()
    client = _client(seeded_config)
    resp = client.post(
        "/api/binance/signals/ingest",
        json=_payload(margin_usdt=0),
    )
    assert resp.status_code == 422
