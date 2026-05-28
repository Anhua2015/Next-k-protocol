"""币安 REST API 客户端。

封装签名、时间同步、重试/退避。通过依赖注入获取 key/secret/base_url，
无 DB 依赖，可独立单测。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

import httpx

from binance.signing import make_headers, sign
from binance.time_sync import (
    RECV_WINDOW_MS,
    do_sync,
    server_timestamp_ms,
    should_resync,
)

logger = logging.getLogger("binance.client")

LIVE_BASE = "https://fapi.binance.com"
TEST_BASE = "https://testnet.binancefuture.com"

RETRY_STATUSES = {429, 418, 500, 502, 503, 504}
RETRY_CODES = {-1003, -1004}
MAX_RETRIES = 3
BACKOFF_BASE_SEC = 0.5


class BinanceClient:
    """币安合约 REST 客户端（同步，线程安全）。

    通过闭包注入 key/secret/base_url，避免直接 import db。
    """

    def __init__(
        self,
        base_url_fn: Callable[[], str],
        api_key_fn: Callable[[], str],
        secret_fn: Callable[[], str],
        timeout: float = 10.0,
    ):
        self._base_url_fn = base_url_fn
        self._api_key_fn = api_key_fn
        self._secret_fn = secret_fn
        self._http = httpx.Client(
            timeout=httpx.Timeout(connect=3, read=timeout, write=10, pool=2),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._http_sync = httpx.Client(timeout=5.0)

    # -- Low-level helpers ------------------------------------------------

    def _base_url(self) -> str:
        return self._base_url_fn()

    def _api_key(self) -> str:
        return self._api_key_fn()

    def _secret(self) -> str:
        return self._secret_fn()

    def _headers(self) -> Dict[str, str]:
        return make_headers(self._api_key())

    def _sign(self, params: Dict[str, Any]) -> str:
        return sign(params, self._secret())

    def _ts(self) -> int:
        if should_resync():
            self._sync_server_time()
        return server_timestamp_ms()

    def _sync_server_time(self) -> None:
        do_sync(self._base_url(), self._http_sync)

    # -- Core request -----------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = True,
    ) -> Any:
        params = dict(params or {})
        if signed:
            params["timestamp"] = self._ts()
            params["recvWindow"] = RECV_WINDOW_MS
            params["signature"] = self._sign(params)

        url = self._base_url() + path
        hdrs = self._headers()

        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                if method == "GET":
                    resp = self._http.get(url, params=params, headers=hdrs)
                elif method == "POST":
                    resp = self._http.post(url, params=params, headers=hdrs)
                elif method == "DELETE":
                    resp = self._http.delete(url, params=params, headers=hdrs)
                else:
                    raise ValueError(f"Unsupported method: {method}")

                if resp.status_code in RETRY_STATUSES:
                    last_exc = httpx.HTTPStatusError(
                        f"{resp.status_code} retryable",
                        request=resp.request,
                        response=resp,
                    )
                    if attempt < MAX_RETRIES:
                        delay = BACKOFF_BASE_SEC * (2 ** attempt)
                        logger.warning(
                            "retry %d/%d after %.2fs",
                            attempt + 1, MAX_RETRIES, delay,
                        )
                        time.sleep(delay)
                        continue
                    raise last_exc

                if resp.status_code >= 400:
                    logger.error(
                        "Binance %s %s -> %s body=%s",
                        method, path, resp.status_code, resp.text,
                    )
                    try:
                        body = resp.json()
                    except Exception:
                        body = None
                    if isinstance(body, dict) and body.get("code") in RETRY_CODES:
                        last_exc = httpx.HTTPStatusError(
                            f"{resp.status_code} code={body.get('code')}",
                            request=resp.request,
                            response=resp,
                        )
                        if attempt < MAX_RETRIES:
                            delay = BACKOFF_BASE_SEC * (2 ** attempt)
                            logger.warning(
                                "retry %d/%d after %.2fs",
                                attempt + 1, MAX_RETRIES, delay,
                            )
                            time.sleep(delay)
                            continue
                        raise last_exc
                    if isinstance(body, dict) and body.get("code") == -1021 and attempt == 0:
                        self._sync_server_time()
                        inner = {k: v for k, v in params.items() if k != "signature"}
                        inner["timestamp"] = self._ts()
                        inner["signature"] = self._sign(inner)
                        params = inner
                        continue
                    resp.raise_for_status()

                return resp.json()
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    delay = BACKOFF_BASE_SEC * (2 ** attempt)
                    logger.warning(
                        "retry %d/%d after %.2fs: %s",
                        attempt + 1, MAX_RETRIES, delay, exc,
                    )
                    time.sleep(delay)
                    continue
                raise
        if last_exc:
            raise last_exc

    # -- Convenience methods (used by higher-level modules) --------------

    def close(self) -> None:
        self._http.close()
        self._http_sync.close()


# Module-level singleton — constructed in main.py lifespan before first use.
client: Optional[BinanceClient] = None


def init_client(
    base_url_fn: Callable[[], str],
    api_key_fn: Callable[[], str],
    secret_fn: Callable[[], str],
) -> BinanceClient:
    global client
    c = BinanceClient(base_url_fn, api_key_fn, secret_fn)
    client = c
    return c
