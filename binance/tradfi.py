"""TradFi 永续（股票类合约）账户协议。"""
from __future__ import annotations

import logging

from binance.client import BinanceClient

logger = logging.getLogger("binance.tradfi")

TRADFI_AGREEMENT_PATH = "/fapi/v1/stock/contract"


def sign_tradfi_perps_agreement(client: BinanceClient) -> str:
    """签署 TradFi-Perps 协议（账户级，一次性）。"""
    result = client.request("POST", TRADFI_AGREEMENT_PATH, {}, as_text=True)
    logger.info("TradFi-Perps agreement signed: %s", result)
    return result
