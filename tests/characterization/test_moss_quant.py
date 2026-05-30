"""Characterization: Moss Quant ingest on the simplified protocol."""
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


def _mq_payload(**overrides):
    base = {
        "source": "moss_quant",
        "api_signal_id": "moss_sig-001",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "margin_usdt": 50.0,
        "entry_price": 67250.5,
        "sl_price": 66500.0,
        "tp_price": 68500.0,
        "play": "balanced",
    }
    base.update(overrides)
    return {"signals": [base]}


def test_moss_quant_source_accepted(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_closed")
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(api_signal_id="mq-test-001"),
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert resp.json()["traded"] == 1
    assert resp.json()["details"][0]["action"] == "traded"


def test_moss_quant_rolling_forces_market_when_global_entry_type_is_limit(
    seeded_config, mock_binance
):
    seeded_config.set_config("entry_type", "LIMIT")
    mock_binance.all(
        place_order="place_order_market_filled",
        position_risk="position_risk_closed",
    )
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(
            api_signal_id="mq-roll-limit-1",
            entry_price=None,
            action="rolling",
            play="balanced_rolling",
        ),
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert resp.json()["traded"] == 1
    logs = client.get(
        "/api/binance/signals?source=moss_quant&action=rolling&limit=10",
        headers=AUTH,
    ).json()
    assert logs[0]["status"] == "traded"


def test_update_sl_endpoint_removed(seeded_config):
    client = _client(seeded_config)

    resp = client.put(
        "/api/binance/positions/99999/sl",
        json={"new_sl_price": 66000.0},
        headers=AUTH,
    )

    assert resp.status_code == 410


def test_close_endpoint_removed(seeded_config):
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/positions/close",
        json={
            "source": "moss_quant",
            "api_signal_id": "mq-close-1",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "exit_rule": "manual",
        },
        headers=AUTH,
    )

    assert resp.status_code == 410
