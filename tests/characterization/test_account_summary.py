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


def test_global_config_no_longer_contains_trading_keys(seeded_config):
    import db

    cfg = db.get_all_config()
    assert "margin_usdt" not in cfg
    assert "leverage" not in cfg
    assert "enabled" not in cfg
    assert "entry_type" not in cfg
    assert "max_positions" not in cfg


