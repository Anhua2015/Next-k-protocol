from __future__ import annotations

import pytest

pytestmark = pytest.mark.characterization


def test_live_reference_columns_exist(seeded_config):
    import db

    with db.get_db() as conn:
        signal_cols = {r["name"] for r in conn.execute("PRAGMA table_info(signals_log)").fetchall()}
        position_cols = {r["name"] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}

    assert {"profile_id", "client_ref", "action", "position_id", "payload_json", "result_json"} <= signal_cols
    assert {"profile_id", "client_ref"} <= position_cols


def test_signal_model_accepts_moss_live_fields():
    from models import SignalItem

    item = SignalItem(
        source="moss_quant",
        api_signal_id="moss-7-open-1",
        symbol="BTCUSDT",
        side="LONG",
        sl_price=65000,
        tp_price=70000,
        notional_usdt=250,
        play="balanced",
        profile_id=7,
        client_ref="moss:7:open:1700000000000",
        action="open",
    )

    assert item.profile_id == 7
    assert item.client_ref == "moss:7:open:1700000000000"
    assert item.action == "open"
