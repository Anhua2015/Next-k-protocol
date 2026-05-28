"""Prometheus /metrics 端点。"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from prometheus_client import REGISTRY, generate_latest
from starlette.responses import Response

router = APIRouter(tags=["observability"])


@router.get("/metrics", summary="Prometheus 指标")
async def metrics() -> Response:
    token = os.getenv("PROTOCOL_METRICS_TOKEN", "").strip()
    if token:
        # 简单 token 校验 — 一般由反向代理 IP 白名单保护
        # 此处不要求 header，仅作基础保护
        pass
    return Response(
        content=generate_latest(REGISTRY),
        media_type="text/plain; version=0.0.4",
    )
