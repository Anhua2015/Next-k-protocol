from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.characterization


def test_insert_signal_persists_live_fields(seeded_config):
    import db

    sid = db.insert_signal(
        source="moss_quant",
        api_signal_id="moss-8-open-1",
        symbol="ETHUSDT",
        side="LONG",
        entry_price=3000,
        sl_price=2900,
        tp_price=3300,
        confidence=None,
        regime="TREND_UP",
        notional_usdt=400,
        received_at="2026-05-29T00:00:00Z",
        play="balanced",
        profile_id=8,
        client_ref="moss:8:open:1",
        action="open",
    )

    assert sid is not None
    rows = db.list_signals(source="moss_quant", action="open", profile_id=8, limit=10)
    assert len(rows) == 1
    assert rows[0]["profile_id"] == 8
    assert rows[0]["client_ref"] == "moss:8:open:1"
    assert rows[0]["action"] == "open"


def test_log_trade_event_records_non_open_actions(seeded_config):
    import db

    event_id = db.log_trade_event(
        source="moss_quant",
        action="update_sl",
        symbol="BTCUSDT",
        side="LONG",
        api_signal_id="moss-9-update-sl-1",
        status="traded",
        profile_id=9,
        position_id=77,
        client_ref="moss:9:update_sl:1",
        sl_price=65000,
        skip_reason="sl_adjusted",
        payload={"new_sl_price": 65000},
        result={"ok": True},
    )

    rows = db.list_signals(source="moss_quant", action="update_sl", profile_id=9, limit=10)
    assert rows[0]["id"] == event_id
    assert rows[0]["position_id"] == 77
    assert rows[0]["sl_price"] == 65000
    assert rows[0]["skip_reason"] == "sl_adjusted"
    assert json.loads(rows[0]["payload_json"])["new_sl_price"] == 65000
