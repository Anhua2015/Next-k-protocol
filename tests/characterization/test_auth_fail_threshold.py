"""Characterization: 20x auth fail in sync auto-disables trading."""
from __future__ import annotations

import re
import sys

import pytest

pytestmark = pytest.mark.characterization


def _open_btc(db):
    return db.insert_position(
        signal_log_id=None, symbol="BTCUSDT", side="LONG",
        entry_order_id="11111111", sl_order_id="33333333", tp_order_id="33333334",
        entry_price=67250.5, sl_price=66500.0, tp_price=68500.0,
        quantity=0.012, notional_usdt=100.0, leverage=10,
        opened_at="2026-05-27T00:00:00+00:00", play="PLAY01", source="zct_vwap",
    )


POS_RISK = re.compile(r".*/fapi/v2/positionRisk.*")
SERVER_TIME = re.compile(r".*/fapi/v1/time.*")


def test_auth_fail_increments_and_eventually_disables(
    seeded_config, httpx_mock, load_binance_fixture,
):
    _open_btc(seeded_config)
    for _ in range(25):
        httpx_mock.add_response(
            method="GET", url=POS_RISK, status_code=401,
            json=load_binance_fixture("error_401_unauthorized"),
            is_optional=True,
        )
    httpx_mock.add_response(
        method="GET", url=SERVER_TIME, status_code=200,
        json=load_binance_fixture("server_time"), is_optional=True,
    )
    sys.modules.pop("trader", None)
    from trader import sync_open_positions, _reset_auth_fail_count
    _reset_auth_fail_count()
    for _ in range(25):
        try:
            sync_open_positions()
        except Exception:
            pass
    assert seeded_config.get_config("enabled", "true") == "false"


def test_auth_fail_reset_keeps_trading_enabled(
    seeded_config, httpx_mock, load_binance_fixture,
):
    _open_btc(seeded_config)
    for _ in range(5):
        httpx_mock.add_response(
            method="GET", url=POS_RISK, status_code=401,
            json=load_binance_fixture("error_401_unauthorized"),
            is_optional=True,
        )
    httpx_mock.add_response(
        method="GET", url=SERVER_TIME, status_code=200,
        json=load_binance_fixture("server_time"), is_optional=True,
    )
    sys.modules.pop("trader", None)
    from trader import sync_open_positions, _reset_auth_fail_count
    _reset_auth_fail_count()
    for _ in range(5):
        try:
            sync_open_positions()
        except Exception:
            pass
    assert seeded_config.get_config("enabled", "true") == "true"
