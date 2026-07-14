"""Reverse-proxy Bitget grid Node worker under /grid-bot → http://127.0.0.1:8080.

next-k-api sets GRID_URL=https://<protocol-host>/grid-bot
so /api/grid/health → Protocol /grid-bot/api/health → Worker /api/health.
"""

from __future__ import annotations

import os
import httpx
from fastapi import APIRouter, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter(prefix="/grid-bot", tags=["grid-bot"])


def _upstream() -> str:
    return (os.getenv("BITGET_GRID_INTERNAL_URL") or "http://127.0.0.1:8080").rstrip("/")


def _timeout() -> float:
    try:
        return float(os.getenv("GRID_BOT_PROXY_TIMEOUT") or 180)
    except ValueError:
        return 180.0


def _enabled() -> bool:
    raw = os.getenv("GRID_WORKER_ENABLED", "1")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


@router.get("/_status")
async def grid_bot_status() -> dict:
    up = _upstream()
    health: dict = {"ok": False}
    if _enabled():
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{up}/api/health")
                health = {"ok": r.status_code < 400, "status": r.status_code, "body": r.json() if r.content else {}}
        except Exception as exc:
            health = {"ok": False, "error": str(exc)}
    return {
        "enabled": _enabled(),
        "upstream": up,
        "publicBase": "/grid-bot",
        "hint": "Set next-k-api GRID_URL to https://<this-host>/grid-bot",
        "health": health,
    }


@router.get("/api/stream")
async def grid_stream(request: Request) -> StreamingResponse:
    if not _enabled():
        raise HTTPException(503, "GRID_WORKER_ENABLED=0")
    url = f"{_upstream()}/api/stream"
    headers = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    params = dict(request.query_params)

    async def gen():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url, headers=headers, params=params) as resp:
                    if resp.status_code >= 400:
                        yield f'data: {{"error":"upstream {resp.status_code}"}}\n\n'.encode()
                        return
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            yield chunk
        except Exception as exc:
            yield f'data: {{"error":"{str(exc).replace(chr(34), chr(39))}"}}\n\n'.encode()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def grid_forward(request: Request, path: str = Path(...)) -> Response:
    if not _enabled():
        raise HTTPException(503, "GRID_WORKER_ENABLED=0")
    if path in ("_status",) or path.startswith("_status/"):
        raise HTTPException(404, "not found")

    upstream_path = "/" + path.lstrip("/")
    url = f"{_upstream()}{upstream_path}"
    headers = {"Accept": request.headers.get("Accept", "application/json")}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    ct = request.headers.get("content-type")
    body = await request.body()
    if ct:
        headers["Content-Type"] = ct

    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            resp = await client.request(
                request.method,
                url,
                headers=headers,
                content=body if body else None,
                params=dict(request.query_params) or None,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(504, f"grid worker timeout: {exc}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"grid worker unreachable at {_upstream()}: {exc}") from exc

    media = resp.headers.get("content-type", "application/json")
    if "json" in media or (resp.content[:1] in (b"{", b"[")):
        try:
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except Exception:
            pass
    return Response(content=resp.content, status_code=resp.status_code, media_type=media)
