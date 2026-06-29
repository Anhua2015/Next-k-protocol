"""Unit: STOP entry via algo order helpers."""
from __future__ import annotations

from binance.orders import build_entry_stop_limit_algo_params, normalize_algo_entry_order


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
    assert "stopPrice" not in params


def test_normalize_algo_entry_order_new():
    out = normalize_algo_entry_order({"algoId": 99, "algoStatus": "NEW"})
    assert out["status"] == "NEW"
    assert out["executedQty"] == 0


def test_normalize_algo_entry_order_filled():
    out = normalize_algo_entry_order(
        {"algoId": 99, "algoStatus": "FINISHED", "actualQty": "2", "actualPrice": "148.5"}
    )
    assert out["status"] == "FILLED"
    assert out["executedQty"] == 2.0
    assert out["avgPrice"] == 148.5


def test_normalize_algo_entry_order_canceled():
    out = normalize_algo_entry_order({"algoId": 99, "algoStatus": "CANCELED"})
    assert out["status"] == "CANCELED"
