"""领域枚举。"""
from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Source(StrEnum):
    ZCT_VWAP = "zct_vwap"
    MOMENTUM = "momentum"
    JIEZHEN = "jiezhen"


class EntryType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
