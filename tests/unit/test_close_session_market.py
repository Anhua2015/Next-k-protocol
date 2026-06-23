"""Unit: session close uses MARKET even if close_price is present."""
from __future__ import annotations

import re
import sys

import pytest

BASE = "https://testnet.binancefuture.com"


def test_session_close_uses_market_order(seeded_config, httpx_mock, load_binance_fixture):
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
        api_signal_id="orb:close:HOODUSDT:session_close:1782158411621",
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
        payload_json='{"close_price": 105.79}',
    )
    signal_log_id = seeded_config.list_signals(limit=1)[0]["id"]

    sys.modules.pop("trader", None)
    from trader import execute_trade

    ok = execute_trade(
        {
            "signal_log_id": signal_log_id,
            "symbol": "HOODUSDT",
            "side": "SHORT",
            "source": "orb",
            "action": "close",
            "close_price": 105.79,
            "api_signal_id": "orb:close:HOODUSDT:session_close:1782158411621",
        }
    )

    assert ok is True
    market_calls = [
        req
        for req in httpx_mock.get_requests()
        if req.method == "POST" and "/fapi/v1/order" in str(req.url)
    ]
    assert len(market_calls) == 1
    assert "type=MARKET" in str(market_calls[0].url) or "type=MARKET" in (market_calls[0].content or b"").decode(
        "utf-8", errors="ignore"
    )
