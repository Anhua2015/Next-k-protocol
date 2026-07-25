"""Builtin factors: Binance default + OKX/Bitget liquidations."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

from backend import factors, okx_market


def test_cvd_from_klines():
    ks = [
        {"volume": 10.0, "taker_buy_base": 7.0},
        {"volume": 10.0, "taker_buy_base": 3.0},
        {"volume": 8.0, "taker_buy_base": 6.0},
    ]

    async def _run():
        with patch.object(factors.binance, "klines", AsyncMock(return_value=ks)):
            return await factors.f_cvd("BTCUSDT")

    out = asyncio.run(_run())
    assert out["source"] == "binance"
    assert out["series"] == [4.0, 0.0, 4.0]


def test_ob_wall_detects_large_levels():
    depth = {
        "bids": [["100", "1"], ["99", "1000"]],
        "asks": [["101", "1"], ["102", "2000"]],
    }

    async def _run():
        with patch.object(factors.binance, "depth", AsyncMock(return_value=depth)):
            return await factors.f_ob_wall("BTCUSDT")

    out = asyncio.run(_run())
    assert out["source"] == "binance"
    assert any(o["notional"] >= 100_000 for o in out["orders"])


def test_okx_uly_mapping():
    assert okx_market.to_uly("BTCUSDT") == "BTC-USDT"
    assert okx_market.to_uly("1000PEPEUSDT") == "1000PEPE-USDT"


def test_liq_agg_okx_anchor_window():
    now = time.time() * 1000
    # Newest print 20m ago (API lag). Baseline must sit outside the rolling 1h of that anchor.
    rows = [
        {"side": "long", "usd": 5000.0, "ts": now - 5_400_000},   # ~90m before now / ~70m before anchor
        {"side": "long", "usd": 1000.0, "ts": now - 1_500_000},
        {"side": "short", "usd": 2000.0, "ts": now - 1_500_000},
        {"side": "long", "usd": 100.0, "ts": now - 1_200_000},    # newest (=anchor)
    ]

    async def _run():
        with patch.object(okx_market, "liquidations", AsyncMock(return_value=rows)):
            return await okx_market.liq_agg("BTCUSDT")

    out = asyncio.run(_run())
    assert out["source"] == "okx"
    assert out["long_1h"] == 1100.0
    assert out["short_1h"] == 2000.0
    assert out["long_mult"] == 1100.0 / 5000.0
    assert out["lag_sec"] >= 1000  # ~20 minutes


def test_liq_orders_okx_anchor_window():
    now = time.time() * 1000
    rows = [
        {"side": "long", "usd": 200_000.0, "ts": now - 1_200_000},           # newest
        {"side": "long", "usd": 200_000.0, "ts": now - 1_200_000 - 900_000}, # 15m before newest
        {"side": "long", "usd": 10_000.0, "ts": now - 1_200_000},            # below min
    ]

    async def _run():
        with patch.object(okx_market, "liquidations", AsyncMock(return_value=rows)):
            return await okx_market.liq_orders("ETHUSDT")

    out = asyncio.run(_run())
    assert out["source"] == "okx"
    assert out["n_10m"] == 1
    assert out["n_prev_20m"] == 1


def test_liq_agg_falls_back_to_bitget():
    bitget_out = {
        "long_1h": 1.0, "short_1h": 2.0, "long_mult": 0.5, "short_mult": 0.5,
        "source": "bitget", "hours": 1, "events": 1, "lag_sec": 0,
    }

    async def _run():
        with patch.object(factors.okx_market, "liq_agg", AsyncMock(return_value=None)):
            with patch.object(factors.bitget_market, "liq_agg", AsyncMock(return_value=bitget_out)):
                return await factors.f_liq_agg("BTCUSDT")

    out = asyncio.run(_run())
    assert out["source"] == "bitget"
