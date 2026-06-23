"""表征测试统一使用的 FastAPI 客户端。"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.testclient import TestClient


def protocol_test_client(app: FastAPI) -> TestClient:
    """按当前测试环境构造客户端。

    GitHub Actions 会设置 ``PROTOCOL_MAINTENANCE_TOKEN=test-token``，用于模拟
    生产环境开启鉴权后的请求链路。测试客户端必须携带同一令牌，否则交易请求
    会在进入业务逻辑前被 401 拦截，无法覆盖原本要验证的下单、保护单和风控行为。

    本地未配置令牌时不添加请求头，继续覆盖兼容开放模式。
    """
    token = os.getenv("PROTOCOL_MAINTENANCE_TOKEN", "").strip()
    headers = {"X-Maintenance-Token": token} if token else None
    return TestClient(app, headers=headers)
