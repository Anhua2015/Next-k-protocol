"""Protocol 敏感写接口的可选令牌鉴权。"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)
_warned_open_mode = False


def protocol_token_configured() -> bool:
    return bool(os.getenv("PROTOCOL_MAINTENANCE_TOKEN", "").strip())


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


async def require_auth(
    x_maintenance_token: str | None = Header(None, alias="X-Maintenance-Token"),
    authorization: str | None = Header(None),
) -> None:
    global _warned_open_mode
    expected = os.getenv("PROTOCOL_MAINTENANCE_TOKEN", "").strip()
    if not expected:
        if not _warned_open_mode:
            logger.warning(
                "PROTOCOL_MAINTENANCE_TOKEN 未设置：交易写接口处于兼容开放模式；"
                "生产环境请配置令牌"
            )
            _warned_open_mode = True
        return

    provided = _extract_token(x_maintenance_token, authorization)
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="protocol_token_required",
        )
