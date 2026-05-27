"""Characterization-suite fixtures.

`mock_binance` glues pytest-httpx to the recorded JSON fixtures and exposes a
helper to register exact-URL responses without repeating path strings.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

import pytest

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
    """Register the default-OK response for every Binance path.

    Tests may override individual paths by calling the returned helper.
    """
    defaults = {
        "server_time":          "server_time",
        "exchange_info":        "exchange_info_btcusdt",
        "premium_index":        "premium_index_btcusdt",
        "position_side":        "position_side_single",
        "place_order":          "place_order_market_filled",
        "get_order":            "get_order_filled",
        "cancel_order":         "cancel_order_success",
        "cancel_all_orders":    "cancel_all_orders_success",
        "place_algo":           "place_algo_order_success",
        "get_algo":             "place_algo_order_success",
        "cancel_algo":          "cancel_order_success",
        "open_algo_orders":     "get_open_algo_orders_sl_filled",
        "position_risk":        "position_risk_open",
        "set_leverage":         "place_algo_order_success",
        "set_margin_type":      "place_algo_order_success",
    }
    for key, fixture in defaults.items():
        method, path = PATH_MAP[key]
        httpx_mock.add_response(
            method=method,
            url=re.compile(rf"^{re.escape(BASE + path)}(\?.*)?$"),
            json=load_binance_fixture(fixture),
            is_reusable=True,
            is_optional=True,
        )

    def _set(key: str, fixture_name: str, *, status_code: int = 200):
        method, path = PATH_MAP[key]
        httpx_mock.add_response(
            method=method,
            url=re.compile(rf"^{re.escape(BASE + path)}(\?.*)?$"),
            json=load_binance_fixture(fixture_name),
            status_code=status_code,
            is_reusable=True,
        )

    return _set
