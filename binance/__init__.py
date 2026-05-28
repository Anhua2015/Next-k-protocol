"""币安合约 REST API 客户端层。

纯 HTTP 层，无业务逻辑，无 DB 依赖。通过依赖注入获取 key/secret/base_url。
"""
from __future__ import annotations

from binance.client import BinanceClient, client, init_client

__all__ = ["BinanceClient", "client", "init_client"]
