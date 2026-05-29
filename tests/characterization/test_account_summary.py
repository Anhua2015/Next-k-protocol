from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.characterization
AUTH = {"X-Maintenance-Token": "test-token"}


def test_account_summary_route_returns_usdt_and_moss_config(seeded_config, monkeypatch):
    import main
    import trader
    import db

    db.set_config("src_moss_quant_leverage", "7")
    db.set_config("src_moss_quant_max_positions", "4")
    db.set_config("src_moss_quant_entry_type", "MARKET")
    db.set_config("src_moss_quant_enabled", "true")

    monkeypatch.setattr(
        trader,
        "get_account_summary",
        lambda: {
            "asset": "USDT",
            "wallet_balance_usdt": 1000.5,
            "available_balance_usdt": 800.25,
            "unrealized_pnl_usdt": 12.5,
        },
    )

    client = TestClient(main.app)
    resp = client.get("/api/binance/account/summary", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["wallet_balance_usdt"] == 1000.5
    assert body["available_balance_usdt"] == 800.25
    assert body["moss_quant"]["leverage"] == 7
    assert body["moss_quant"]["enabled"] is True
