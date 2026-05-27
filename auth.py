"""API 鉴权模块 — X-Maintenance-Token / Bearer token 校验。

所有 /api/binance/* 接口（除 /api/binance/health）都需要鉴权。
与 next-k-api 的 utils/maintenance_token.py 保持一致的校验逻辑。
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

_warned_open_mode = False


class AuthError(PermissionError):
    """鉴权失败：token 缺失或错误。"""


def _extract_token(
    x_maintenance_token: str | None,
    authorization: str | None,
) -> str:
    if x_maintenance_token and str(x_maintenance_token).strip():
        return str(x_maintenance_token).strip()
    if authorization:
        parts = str(authorization).split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    return ""


def verify_token(
    x_maintenance_token: str | None = None,
    authorization: str | None = None,
) -> None:
    """校验 PROTOCOL_MAINTENANCE_TOKEN。

    未配置 token 时输出警告并放行（开发模式）；生产环境必须配置。
    """
    global _warned_open_mode
    expected = os.getenv("PROTOCOL_MAINTENANCE_TOKEN", "").strip()
    if not expected:
        if not _warned_open_mode:
            logger.warning(
                "PROTOCOL_MAINTENANCE_TOKEN 未设置：所有接口对公网开放。"
                "生产环境请配置该变量"
            )
            _warned_open_mode = True
        return
    provided = _extract_token(x_maintenance_token, authorization)
    if not provided or not hmac.compare_digest(provided, expected):
        raise AuthError("maintenance_token_required")


async def require_auth(
    x_maintenance_token: str | None = Header(None, alias="X-Maintenance-Token"),
    authorization: str | None = Header(None),
) -> None:
    """FastAPI 依赖：校验请求鉴权。

    用法：
        @router.get("/status", dependencies=[Depends(require_auth)])
        async def get_status(): ...
    """
    try:
        verify_token(x_maintenance_token, authorization)
    except AuthError:
        raise HTTPException(
            status_code=401,
            detail="maintenance_token_required",
        ) from None
