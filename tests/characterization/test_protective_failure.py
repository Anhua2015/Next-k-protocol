"""Characterization: SL/TP placement failure triggers emergency_close."""
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


def _payload():
    return {"signals": [{
        "source": "orb", "api_signal_id": "p-001",
        "symbol": "BTCUSDT", "side": "LONG",
        "margin_usdt": 100.0,
        "leverage": 10.0,
        "entry_price": 67250.5, "sl_price": 66500.0, "tp_price": 68500.0,
        "play": "PLAY01",
    }]}


def test_sl_placement_fail_triggers_emergency_close(seeded_config, mock_binance):
    """SL/TP placement fails -> cancel_all_orders + emergency MARKET close."""
    mock_binance.all(
        place_algo="error_2019_insufficient_margin",
        position_risk="position_risk_closed",
    )
    mock_binance("place_algo", "error_2019_insufficient_margin", status_code=400)
    client = _client(seeded_config)
    resp = client.post("/api/binance/signals/ingest", json=_payload())
    body = resp.json()
    assert body["errors"] == 1


def test_tp_skipped_when_no_tp_price(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_closed")
    payload = {"signals": [{
        "source": "orb", "api_signal_id": "p-002",
        "symbol": "BTCUSDT", "side": "LONG",
        "margin_usdt": 100.0,
        "leverage": 10.0,
        "entry_price": 67250.5, "sl_price": 66500.0, "tp_price": None,
        "play": "PLAY01",
    }]}
    client = _client(seeded_config)
    resp = client.post("/api/binance/signals/ingest", json=payload)
    assert resp.json()["traded"] == 1
