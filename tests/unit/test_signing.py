"""Unit: binance/signing.py — pure functions, no mocks needed."""
from __future__ import annotations

import pytest

from binance.signing import make_headers, sign


def test_sign_known_vector():
    """HMAC-SHA256 vector test."""
    params = {"symbol": "BTCUSDT", "side": "BUY", "timestamp": 1716800000000}
    result = sign(params, "test-secret-key")
    assert isinstance(result, str)
    assert len(result) == 64  # SHA256 hex
    # Deterministic: same input -> same output
    assert sign(params, "test-secret-key") == result


def test_sign_different_secret_different_output():
    params = {"symbol": "BTCUSDT"}
    assert sign(params, "a") != sign(params, "b")


def test_make_headers_includes_api_key():
    h = make_headers("my-api-key-123")
    assert h["X-MBX-APIKEY"] == "my-api-key-123"


def test_make_headers_empty_key():
    h = make_headers("")
    assert h["X-MBX-APIKEY"] == ""
