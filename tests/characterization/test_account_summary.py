from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.characterization

def test_open_signal_without_margin_is_rejected_at_execution(seeded_config, mock_binance):
    import main

    mock_binance.all(position_risk="position_risk_closed")
    client = TestClient(main.app)
    resp = client.post(
        "/api/binance/signals/ingest",
        json={
            "signals": [
                {
                    "api_signal_id": "sig-1",
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "sl_price": 65000.0,
                }
            ]
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["errors"] == 1
    assert body["details"][0]["action"] == "error"


def test_global_config_no_longer_contains_margin_or_strategy_keys(seeded_config):
    import db

    cfg = db.get_all_config()
    assert "margin_usdt" not in cfg
    assert "leverage" not in cfg
    assert "src_momentum_enabled" not in cfg
    assert "src_moss_quant_leverage" not in cfg


def test_config_endpoint_omits_binance_credentials(seeded_config):
    import main

    client = TestClient(main.app)
    resp = client.get("/api/binance/config")

    assert resp.status_code == 200
    body = resp.json()
    assert "binance_api_key" not in body
    assert "binance_api_secret" not in body


def test_config_endpoint_rejects_binance_credentials_updates(seeded_config):
    import main

    client = TestClient(main.app)
    resp = client.post(
        "/api/binance/config",
        json={"pairs": {"binance_api_key": "new-key"}},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "binance_credentials_env_only"
