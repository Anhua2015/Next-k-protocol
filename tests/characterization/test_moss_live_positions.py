from __future__ import annotations

import pytest

pytestmark = pytest.mark.characterization


def test_position_filters_by_source_profile_and_status(seeded_config):
    import db

    db.insert_position(
        signal_log_id=1,
        symbol="BTCUSDT",
        side="LONG",
        entry_order_id="e1",
        sl_order_id="s1",
        tp_order_id="t1",
        entry_price=65000,
        sl_price=64000,
        tp_price=68000,
        quantity=0.01,
        notional_usdt=650,
        leverage=10,
        opened_at="2026-05-29T00:00:00Z",
        play="balanced",
        source="moss_quant",
        profile_id=12,
        client_ref="moss:12:open:1",
    )
    db.insert_position(
        signal_log_id=2,
        symbol="ETHUSDT",
        side="SHORT",
        entry_order_id="e2",
        sl_order_id="s2",
        tp_order_id="t2",
        entry_price=3000,
        sl_price=3100,
        tp_price=2800,
        quantity=0.1,
        notional_usdt=300,
        leverage=10,
        opened_at="2026-05-29T00:00:00Z",
        play="",
        source="momentum",
        profile_id=None,
        client_ref="",
    )

    rows = db.list_positions(status="open", source="moss_quant", profile_id=12, limit=50)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["profile_id"] == 12
    assert rows[0]["client_ref"] == "moss:12:open:1"
