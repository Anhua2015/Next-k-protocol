"""Unit: Binance 401/403 hooks auth circuit breaker."""
from __future__ import annotations

import re
import sys

import pytest

from common.exceptions import BinanceAuthError

BASE = "https://testnet.binancefuture.com"


def test_signed_401_triggers_auth_fail_and_raises(seeded_config, httpx_mock, load_binance_fixture):
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
        status_code=401,
        json={"code": -2015, "msg": "Invalid API-key, IP, or permissions for action"},
        is_reusable=True,
        is_optional=True,
    )

    sys.modules.pop("trader", None)
    import trader

    trader.notify_binance_auth_success()
    try:
        with pytest.raises(BinanceAuthError):
            trader.list_live_positions()
        assert trader.is_execution_paused() is False
    finally:
        trader.notify_binance_auth_success()


def test_auth_fail_threshold_pauses_execution(seeded_config):
    sys.modules.pop("trader", None)
    import trader

    trader.notify_binance_auth_success()
    try:
        for i in range(trader._SYNC_AUTH_FAIL_THRESHOLD):
            trader.notify_binance_auth_fail(f"test-{i}")
        assert trader.is_execution_paused() is True
    finally:
        trader.notify_binance_auth_success()


def test_auth_success_clears_pause(seeded_config):
    sys.modules.pop("trader", None)
    import trader

    trader.notify_binance_auth_success()
    try:
        for _ in range(trader._SYNC_AUTH_FAIL_THRESHOLD):
            trader.notify_binance_auth_fail("test")
        assert trader.is_execution_paused() is True
        trader.notify_binance_auth_success()
        assert trader.is_execution_paused() is False
    finally:
        trader.notify_binance_auth_success()
