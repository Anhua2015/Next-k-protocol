from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.characterization
AUTH = {"X-Maintenance-Token": "test-token"}


def _client(seeded_config):
    import main
    return TestClient(main.app)


def test_update_sl_writes_signal_event(seeded_config, mock_binance):
    import db

    pos_id = db.insert_position(
        signal_log_id=1,
        symbol="BTCUSDT",
        side="LONG",
        entry_order_id="entry",
        sl_order_id="old-sl",
        tp_order_id="tp",
        entry_price=65000,
        sl_price=64000,
        tp_price=69000,
        quantity=0.01,
        notional_usdt=650,
        leverage=10,
        opened_at="2026-05-29T00:00:00Z",
        play="balanced",
        source="moss_quant",
        profile_id=5,
        client_ref="moss:5:open:1",
    )
    mock_binance("server_time", "server_time")
    mock_binance("exchange_info", "exchange_info_btcusdt")
    mock_binance("cancel_algo", "cancel_order_success")
    mock_binance("place_algo", "place_algo_order_success")
    client = _client(seeded_config)

    resp = client.put(
        f"/api/binance/positions/{pos_id}/sl",
        json={"new_sl_price": 64500, "profile_id": 5, "client_ref": "moss:5:update_sl:1"},
        headers=AUTH,
    )

    assert resp.status_code == 200
    events = db.list_signals(source="moss_quant", action="update_sl", profile_id=5, limit=10)
    assert events
    assert events[0]["position_id"] == pos_id
    assert events[0]["status"] == "traded"
    assert events[0]["sl_price"] == 64500
