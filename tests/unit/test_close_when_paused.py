"""Unit: close signals bypass execution pause."""
from __future__ import annotations

import re
import sys

BASE = "https://testnet.binancefuture.com"


def test_close_allowed_when_execution_paused(seeded_config, httpx_mock, load_binance_fixture):
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(BASE)}/fapi/v1/time(\?.*)?$"),
        json=load_binance_fixture("server_time"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(BASE)}/fapi/v2/positionRisk(\?.*)?$"),
        json=load_binance_fixture("position_risk_hood_short"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(BASE)}/fapi/v1/positionSide/dual(\?.*)?$"),
        json=load_binance_fixture("position_side_single"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=re.compile(rf"^{re.escape(BASE)}/fapi/v1/allOpenOrders(\?.*)?$"),
        json={"code": 200, "msg": "success"},
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(BASE)}/fapi/v1/openAlgoOrders(\?.*)?$"),
        json=[],
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="POST",
        url=re.compile(rf"^{re.escape(BASE)}/fapi/v1/order(\?.*)?$"),
        json=load_binance_fixture("place_order_market_filled"),
        is_reusable=True,
        is_optional=True,
    )
    seeded_config.insert_signal(
        source="orb",
        api_signal_id="orb:close:HOODUSDT:manual:1",
        symbol="HOODUSDT",
        side="SHORT",
        entry_price=None,
        sl_price=None,
        tp_price=None,
        confidence="high",
        regime=None,
        notional_usdt=None,
        received_at="2026-06-22T20:00:00+00:00",
        status="received",
        action="close",
    )
    signal_log_id = seeded_config.list_signals(limit=1)[0]["id"]

    sys.modules.pop("trader", None)
    import trader

    trader._pause_execution()
    try:
        ok = trader.execute_trade(
            {
                "signal_log_id": signal_log_id,
                "symbol": "HOODUSDT",
                "side": "SHORT",
                "source": "orb",
                "action": "close",
            }
        )
    finally:
        trader.notify_binance_auth_success()

    assert ok is True
    row = seeded_config.list_signals(limit=1)[0]
    assert row["status"] == "traded"
