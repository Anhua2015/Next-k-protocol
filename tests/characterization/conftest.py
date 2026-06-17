"""Characterization-suite fixtures.

`mock_binance` returns a helper to register Binance HTTP responses lazily.
Tests call `mock_binance("key", "fixture_name")` for each endpoint they need.
Pre-registration of defaults is avoided so overrides always win.
"""
from __future__ import annotations

import re
from typing import Any, Callable

import pytest

from binance.client import client as binance_client

BASE = "https://testnet.binancefuture.com"

PATH_MAP = {
    "server_time":            ("GET",    "/fapi/v1/time"),
    "exchange_info":          ("GET",    "/fapi/v1/exchangeInfo"),
    "premium_index":          ("GET",    "/fapi/v1/premiumIndex"),
    "position_side":          ("GET",    "/fapi/v1/positionSide/dual"),
    "set_leverage":           ("POST",   "/fapi/v1/leverage"),
    "set_margin_type":        ("POST",   "/fapi/v1/marginType"),
    "place_order":            ("POST",   "/fapi/v1/order"),
    "get_order":              ("GET",    "/fapi/v1/order"),
    "cancel_order":           ("DELETE", "/fapi/v1/order"),
    "cancel_all_orders":      ("DELETE", "/fapi/v1/allOpenOrders"),
    "place_algo":             ("POST",   "/fapi/v1/algoOrder"),
    "get_algo":               ("GET",    "/fapi/v1/algoOrder"),
    "cancel_algo":            ("DELETE", "/fapi/v1/algoOrder"),
    "open_algo_orders":       ("GET",    "/fapi/v1/openAlgoOrders"),
    "position_risk":          ("GET",    "/fapi/v2/positionRisk"),
}


@pytest.fixture
def mock_binance(httpx_mock, load_binance_fixture):
    """Return a helper that registers Binance HTTP responses on demand.

    Usage:
        mock_binance("place_order", "place_order_market_filled")
        mock_binance("position_risk", "position_risk_closed")
        mock_binance("position_risk", "error_401_unauthorized", status_code=401)

    All registered mocks use is_reusable=True + is_optional=True so skipped
    mock assertions don't fail teardown.
    """
    def _set(key: str, fixture_name: str, *, status_code: int = 200):
        method, path = PATH_MAP[key]
        httpx_mock.add_response(
            method=method,
            url=re.compile(rf"^{re.escape(BASE + path)}(\?.*)?$"),
            json=load_binance_fixture(fixture_name),
            status_code=status_code,
            is_reusable=True,
            is_optional=True,
        )

    DEFAULTS = {
        "server_time":       "server_time",
        "exchange_info":     "exchange_info_btcusdt",
        "premium_index":     "premium_index_btcusdt",
        "position_side":     "position_side_single",
        "set_leverage":      "place_algo_order_success",
        "set_margin_type":   "place_algo_order_success",
        "place_order":       "place_order_market_filled",
        "get_order":         "get_order_filled",
        "cancel_order":      "cancel_order_success",
        "cancel_all_orders": "cancel_all_orders_success",
        "place_algo":        "place_algo_order_success",
        "get_algo":          "place_algo_order_success",
        "cancel_algo":       "cancel_order_success",
        "open_algo_orders":  "get_open_algo_orders_sl_filled",
        "position_risk":     "position_risk_open",
    }

    def _set_all(status_code_overrides=None, **overrides):
        """Reset + re-register all defaults with overrides applied.

        Usage:
            mock_binance.all()  # all happy-path defaults
            mock_binance.all(place_order="place_order_limit_ack")
            mock_binance.all(position_risk="error_401_unauthorized",
                             status_code_overrides={"position_risk": 401})
        """
        httpx_mock.reset()
        merged = {**DEFAULTS, **overrides}
        sc_overrides = status_code_overrides or {}
        for key, fixture_name in merged.items():
            _set(key, fixture_name, status_code=sc_overrides.get(key, 200))
        return _set

    _set.all = _set_all
    return _set


@pytest.fixture(autouse=True)
def _close_binance_client_after_test():
    """Ensure the global BinanceClient is closed after each characterization test.

    Prevents ``ResourceWarning: unclosed <ssl.SSLSocket>`` from httpx connection
    pools that are created during lifespan startup (time sync / exchange info).
    """
    yield
    c = binance_client
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
