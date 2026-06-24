from __future__ import annotations


def test_income_events_aggregate_net_pnl(fresh_db):
    from datetime import datetime, timezone

    import db

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows = [
        {
            "symbol": "BTCUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "10.5",
            "asset": "USDT",
            "time": now_ms,
            "tranId": "pnl-1",
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "COMMISSION",
            "income": "-0.5",
            "asset": "USDT",
            "time": now_ms,
            "tranId": "fee-1",
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": "-0.25",
            "asset": "USDT",
            "time": now_ms,
            "tranId": "funding-1",
        },
        {
            "symbol": "BTCUSDT",
            "incomeType": "TRANSFER",
            "income": "1000",
            "asset": "USDT",
            "time": now_ms,
            "tranId": "transfer-1",
        },
    ]

    assert db.upsert_income_events(rows) == 3
    assert db.upsert_income_events(rows) == 0

    summary = db.aggregate_pnl(period="daily", days=365, tz_name="UTC")

    assert len(summary) == 1
    assert summary[0]["net_pnl_usdt"] == 9.75
    assert summary[0]["realized_pnl_usdt"] == 10.5
    assert summary[0]["commission_usdt"] == -0.5
    assert summary[0]["funding_fee_usdt"] == -0.25
    assert summary[0]["event_count"] == 3

    tomorrow = datetime.fromtimestamp(now_ms / 1000, timezone.utc).date()
    summary_from_today = db.aggregate_pnl(
        period="daily",
        days=365,
        tz_name="UTC",
        start_date=tomorrow.isoformat(),
    )
    assert len(summary_from_today) == 1

    cleared = db.clear_income_cache()
    assert cleared["deleted_events"] == 3
    assert cleared["deleted_sync_state"] == 1
    assert db.aggregate_pnl(period="daily", days=365, tz_name="UTC") == []
