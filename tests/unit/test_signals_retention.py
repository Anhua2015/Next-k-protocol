"""Unit: signals_log retention cleanup."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _iso_hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_delete_signals_older_than_keeps_recent(fresh_db):
    fresh_db.insert_signal(
        source="orb",
        api_signal_id="recent-1",
        symbol="BTCUSDT",
        side="LONG",
        entry_price=1.0,
        sl_price=0.9,
        tp_price=None,
        confidence="high",
        regime=None,
        notional_usdt=None,
        received_at=_iso_hours_ago(1),
        status="traded",
    )
    fresh_db.insert_signal(
        source="orb",
        api_signal_id="old-1",
        symbol="ETHUSDT",
        side="SHORT",
        entry_price=1.0,
        sl_price=1.1,
        tp_price=None,
        confidence="high",
        regime=None,
        notional_usdt=None,
        received_at=_iso_hours_ago(30),
        status="traded",
    )

    deleted = fresh_db.delete_signals_older_than(keep_hours=24.0)
    rows = fresh_db.list_signals(limit=10)

    assert deleted == 1
    assert len(rows) == 1
    assert rows[0]["api_signal_id"] == "recent-1"

    assert fresh_db.clear_signals() == 1
    assert fresh_db.list_signals(limit=10) == []
