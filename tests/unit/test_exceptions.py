"""Unit: common/exceptions.py — exception hierarchy."""
from __future__ import annotations

import pytest

from common.exceptions import (
    BinanceAuthError,
    BinanceBusinessError,
    BinanceRateLimitError,
    BinanceServerError,
    BinanceTimeskewError,
    ConfigError,
    EmergencyCloseFailedError,
    InsufficientNotionalError,
    ProtocolError,
    SLDistanceError,
    SignalValidationError,
)


class TestExceptionHierarchy:
    def test_protocol_error_is_base(self):
        assert issubclass(ConfigError, ProtocolError)
        assert issubclass(SignalValidationError, ProtocolError)
        assert issubclass(EmergencyCloseFailedError, ProtocolError)

    def test_binance_errors_inherit_http(self):
        assert issubclass(BinanceAuthError, ProtocolError)
        assert issubclass(BinanceRateLimitError, ProtocolError)
        assert issubclass(BinanceServerError, ProtocolError)

    def test_retryable_property(self):
        assert BinanceRateLimitError.retryable is True
        assert BinanceServerError.retryable is True
        assert BinanceAuthError.retryable is False
        assert ProtocolError.retryable is False

    def test_code_property(self):
        assert BinanceAuthError.code == "binance_auth"
        assert BinanceRateLimitError.code == "binance_rate_limit"
        assert ConfigError.code == "bad_config"
        assert EmergencyCloseFailedError.code == "emergency_close_failed"

    def test_business_error_stores_code(self):
        e = BinanceBusinessError(binance_code=-2019, msg="Insufficient margin")
        assert e.binance_code == -2019
        assert "Insufficient margin" in str(e)

    def test_emergency_close_error_stores_context(self):
        e = EmergencyCloseFailedError(symbol="BTCUSDT", qty=0.012)
        assert e.symbol == "BTCUSDT"
        assert e.qty == 0.012
        assert "BTCUSDT" in str(e)

    def test_exceptions_are_catchable_as_base(self):
        try:
            raise ConfigError("bad")
        except ProtocolError:
            pass

        try:
            raise BinanceAuthError("auth")
        except ProtocolError:
            pass


class TestRetryable:
    def test_rate_limit_retryable(self):
        e = BinanceRateLimitError()
        assert e.retryable

    def test_default_not_retryable(self):
        assert not SignalValidationError().retryable
        assert not InsufficientNotionalError().retryable
        assert not SLDistanceError().retryable
