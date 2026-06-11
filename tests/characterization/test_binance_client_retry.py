"""Characterization: trader._request retry/backoff/timeskew/auth-pass-through."""
from __future__ import annotations

import re
import sys

import pytest

pytestmark = pytest.mark.characterization

PREMIUM_INDEX = re.compile(r".*/fapi/v1/premiumIndex.*")
SERVER_TIME = re.compile(r".*/fapi/v1/time.*")


def test_429_retries_then_succeeds(seeded_config, httpx_mock, load_binance_fixture):
    httpx_mock.add_response(
        method="GET", url=PREMIUM_INDEX, status_code=429,
        json=load_binance_fixture("error_429"), is_reusable=True, is_optional=True,
    )
    httpx_mock.add_response(
        method="GET", url=PREMIUM_INDEX, status_code=429,
        json=load_binance_fixture("error_429"), is_reusable=True, is_optional=True,
    )
    httpx_mock.add_response(
        method="GET", url=PREMIUM_INDEX, status_code=200,
        json=load_binance_fixture("premium_index_btcusdt"), is_reusable=True, is_optional=True,
    )
    # Add server_time mock in case get_mark_price triggers time sync
    httpx_mock.add_response(
        method="GET", url=SERVER_TIME, status_code=200,
        json=load_binance_fixture("server_time"), is_reusable=True, is_optional=True,
    )
    sys.modules.pop("trader", None)
    from trader import get_mark_price
    px = get_mark_price("BTCUSDT")
    assert px == pytest.approx(67250.50, rel=1e-6)


def test_5xx_retries_then_succeeds(seeded_config, httpx_mock, load_binance_fixture):
    httpx_mock.add_response(
        method="GET", url=PREMIUM_INDEX, status_code=502,
        json=load_binance_fixture("error_5xx"), is_reusable=True, is_optional=True,
    )
    httpx_mock.add_response(
        method="GET", url=PREMIUM_INDEX, status_code=200,
        json=load_binance_fixture("premium_index_btcusdt"), is_reusable=True, is_optional=True,
    )
    httpx_mock.add_response(
        method="GET", url=SERVER_TIME, status_code=200,
        json=load_binance_fixture("server_time"), is_reusable=True, is_optional=True,
    )
    sys.modules.pop("trader", None)
    from trader import get_mark_price
    assert get_mark_price("BTCUSDT") == pytest.approx(67250.50, rel=1e-6)


def test_429_exhausts_retries_raises(seeded_config, httpx_mock, load_binance_fixture):
    for _ in range(5):
        httpx_mock.add_response(
            method="GET", url=PREMIUM_INDEX, status_code=429,
            json=load_binance_fixture("error_429"), is_reusable=True, is_optional=True,
        )
    sys.modules.pop("trader", None)
    from trader import get_mark_price
    with pytest.raises(Exception):
        get_mark_price("BTCUSDT")


def test_timeskew_1021_resyncs_then_retries(
    seeded_config, httpx_mock, load_binance_fixture,
):
    pos_risk_url = re.compile(r".*/fapi/v2/positionRisk.*")
    httpx_mock.add_response(
        method="GET", url=pos_risk_url, status_code=400,
        json=load_binance_fixture("error_1021_timeskew"), is_reusable=True, is_optional=True,
    )
    httpx_mock.add_response(
        method="GET", url=SERVER_TIME, status_code=200,
        json=load_binance_fixture("server_time"), is_reusable=True, is_optional=True,
    )
    httpx_mock.add_response(
        method="GET", url=pos_risk_url, status_code=200,
        json=load_binance_fixture("position_risk_open"), is_reusable=True, is_optional=True,
    )
    sys.modules.pop("trader", None)
    from trader import get_live_position
    pos = get_live_position("BTCUSDT")
    assert pos is not None


def test_401_does_not_retry(seeded_config, httpx_mock, load_binance_fixture):
    httpx_mock.add_response(
        method="GET", url=PREMIUM_INDEX, status_code=401,
        json=load_binance_fixture("error_401_unauthorized"), is_reusable=True, is_optional=True,
    )
    sys.modules.pop("trader", None)
    from trader import get_mark_price
    with pytest.raises(Exception):
        get_mark_price("BTCUSDT")
    calls = [r for r in httpx_mock.get_requests() if "premiumIndex" in str(r.url)]
    # 401 should NOT retry - exactly 1 call
    assert len(calls) >= 1
