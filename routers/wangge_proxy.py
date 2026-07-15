"""Reverse-proxy Next K 网格 (vendor/wangge → Bitget multi-symbol).

Protocol keeps /api/binance/* /docs /redoc; everything else → grid UI on :8080
(dashboard + /api/s/:SYM + /api/overview + /api/symbols + /api/ai|…).
"""

from __future__ import annotations

import os
from typing import Iterable

import httpx
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse


def _upstream() -> str:
    return (os.getenv("WANGGE_INTERNAL_URL") or "http://127.0.0.1:8080").rstrip("/")


def _keep_on_protocol(path: str) -> bool:
    if path.startswith("/api/binance"):
        return True
    if path.startswith("/docs") or path.startswith("/redoc") or path.startswith("/openapi"):
        return True
    if path.startswith("/metrics") or path == "/health" or path.startswith("/api/health"):
        return True
    return False


class WanggeProxyMiddleware(BaseHTTPMiddleware):
    """Forward non-Protocol traffic to the Next K grid Node process."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path or "/"
        if _keep_on_protocol(path):
            return await call_next(request)

        url = f"{_upstream()}{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "connection", "transfer-encoding")
        }
        body = await request.body()

        # SSE / long streams
        accept = (request.headers.get("accept") or "").lower()
        is_stream = "text/event-stream" in accept or path.endswith("/stream")

        try:
            if is_stream:
                client = httpx.AsyncClient(timeout=None)

                async def gen() -> Iterable[bytes]:
                    try:
                        async with client.stream(
                            request.method,
                            url,
                            headers=headers,
                            content=body if body else None,
                        ) as resp:
                            async for chunk in resp.aiter_bytes():
                                if chunk:
                                    yield chunk
                    finally:
                        await client.aclose()

                return StreamingResponse(
                    gen(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )

            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.request(
                    request.method,
                    url,
                    headers=headers,
                    content=body if body else None,
                )
        except httpx.RequestError as exc:
            return Response(
                content=f'{{"error":"Next K grid unreachable at {_upstream()}: {exc}"}}',
                status_code=502,
                media_type="application/json",
            )

        out_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower()
            not in (
                "content-encoding",
                "transfer-encoding",
                "connection",
                "content-length",
            )
        }
        return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)


def wangge_enabled() -> bool:
    raw = os.getenv("WANGGE_ENABLED", "1")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")
