"""Prometheus /metrics 端点。"""
from __future__ import annotations

from fastapi import APIRouter
from prometheus_client import REGISTRY, generate_latest
from starlette.responses import Response

router = APIRouter(tags=["observability"])


@router.get("/metrics", summary="Prometheus 指标")
async def metrics() -> Response:
    from observability.metrics import TRADING_ENABLED
    from trader import is_execution_paused

    TRADING_ENABLED.set(0 if is_execution_paused() else 1)
    return Response(
        content=generate_latest(REGISTRY),
        media_type="text/plain; version=0.0.4",
    )
