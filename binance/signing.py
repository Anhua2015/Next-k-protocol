"""HMAC 签名与 API 请求头。

纯函数，不依赖全局状态，可独立单测。
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from typing import Any, Dict


def sign(params: Dict[str, Any], secret: str) -> str:
    """对参数按 key 排序后 HMAC-SHA256 签名。"""
    qs = urllib.parse.urlencode(params)
    return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()


def make_headers(api_key: str) -> Dict[str, str]:
    """构建包含 X-MBX-APIKEY 的请求头。"""
    return {"X-MBX-APIKEY": api_key}
