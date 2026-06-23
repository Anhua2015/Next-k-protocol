"""Unit: close when exchange position already flat."""
from __future__ import annotations

import re
import sys

import pytest

BASE = "https://testnet.binancefuture.com"


def test_close_succeeds_when_position_already_flat(
    seeded_config, httpx_mock, load_binance_fixture,
):
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
        json=load_binance_fixture("position_risk_usar_closed"),
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
    seeded_config.insert_signal(
        source="orb",
        api_signal_id="orb:close:USARUSDT:loss:1",
        symbol="USARUSDT",
        side="SHORT",
        entry_price=None,
        sl_price=None,
        tp_price=None,
        confidence="high",
        regime=None,
        notional_usdt=None,
        received_at="2026-06-23T00:50:00+00:00",
        status="received",
        action="close",
    )
    signal_log_id = seeded_config.list_signals(limit=1)[0]["id"]

    sys.modules.pop("trader", None)
    from trader import execute_trade

    ok = execute_trade(
        {
            "signal_log_id": signal_log_id,
            "symbol": "USARUSDT",
            "side": "SHORT",
            "source": "orb",
            "action": "close",
            "close_price": 24.65,
        }
    )

    assert ok is True
    row = seeded_config.list_signals(limit=1)[0]
    assert row["status"] == "traded"
    assert row["skip_reason"] == "already_flat"
