"""价格/数量精度取整工具。从 binance.exchange_info 重导出。"""
from __future__ import annotations

from binance.exchange_info import round_price, round_quantity  # noqa: F401
