"""Unit: STOP entry via algo order helpers."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from binance.account import set_margin_type
from binance.client import BinanceClient
from binance.orders import (
    build_entry_stop_limit_algo_params,
    get_algo_order,
    normalize_algo_entry_order,
)


def test_build_entry_stop_limit_algo_params():
    params = build_entry_stop_limit_algo_params(
        symbol="COINUSDT",
        side="SELL",
        qty=1.5,
        trigger_price=148.98,
        limit_price=148.90,
        position_side="SHORT",
    )
    assert params["algoType"] == "CONDITIONAL"
    assert params["type"] == "STOP"
    assert params["triggerPrice"] == 148.98
    assert params["price"] == 148.90
    assert params["positionSide"] == "SHORT"
    assert params["workingType"] == "CONTRACT_PRICE"
    assert "stopPrice" not in params


def test_normalize_algo_entry_order_new():
    out = normalize_algo_entry_order({"algoId": 99, "algoStatus": "NEW"})
    assert out["status"] == "NEW"
    assert out["executedQty"] == 0


def test_normalize_algo_entry_order_working():
    out = normalize_algo_entry_order({"algoId": 99, "status": "WORKING"})
    assert out["status"] == "NEW"


def test_normalize_algo_entry_order_filled():
    out = normalize_algo_entry_order(
        {"algoId": 99, "algoStatus": "FINISHED", "actualQty": "2", "actualPrice": "148.5"}
    )
    assert out["status"] == "FILLED"
    assert out["executedQty"] == 2.0
    assert out["avgPrice"] == 148.5


def test_normalize_algo_entry_order_filled_qty_without_price_uses_trigger():
    out = normalize_algo_entry_order(
        {"algoId": 99, "actualQty": "1.07", "triggerPrice": "152.51"}
    )
    assert out["status"] == "FILLED"
    assert out["executedQty"] == 1.07
    assert out["avgPrice"] == 152.51


def test_normalize_algo_entry_order_canceled():
    out = normalize_algo_entry_order({"algoId": 99, "algoStatus": "CANCELED"})
    assert out["status"] == "CANCELED"


def test_normalize_algo_entry_order_finished_no_fill():
    out = normalize_algo_entry_order({"algoId": 99, "algoStatus": "FINISHED", "actualQty": "0"})
    assert out["status"] == "CANCELED"


def test_get_algo_order_retries_on_2013():
    client = MagicMock(spec=BinanceClient)
    resp = MagicMock()
    resp.status_code = 400
    resp.json.return_value = {"code": -2013, "msg": "Order does not exist."}
    not_found = httpx.HTTPStatusError(
        "400",
        request=MagicMock(),
        response=resp,
    )
    client.request.side_effect = [
        not_found,
        {"algoId": 123, "algoStatus": "NEW"},
    ]

    out = get_algo_order(client, "123", retries=2, retry_delay_sec=0)

    assert out["algoStatus"] == "NEW"
    assert client.request.call_count == 2


def test_set_margin_type_ignores_4046_and_4067():
    client = MagicMock(spec=BinanceClient)

    for code, msg in (
        (-4046, "No need to change margin type."),
        (-4067, "Position side cannot be changed if there exists open orders."),
    ):
        resp = MagicMock()
        resp.status_code = 400
        resp.text = msg
        resp.json.return_value = {"code": code, "msg": msg}
        client.request.side_effect = httpx.HTTPStatusError(
            "400", request=MagicMock(), response=resp
        )
        set_margin_type(client, "COINUSDT")
        client.request.reset_mock()


def test_set_margin_type_raises_other_errors():
    client = MagicMock(spec=BinanceClient)
    resp = MagicMock()
    resp.status_code = 400
    resp.text = "bad"
    resp.json.return_value = {"code": -9999, "msg": "unknown"}
    client.request.side_effect = httpx.HTTPStatusError(
        "400", request=MagicMock(), response=resp
    )
    with pytest.raises(httpx.HTTPStatusError):
        set_margin_type(client, "COINUSDT")
