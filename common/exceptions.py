"""业务异常体系。"""
from __future__ import annotations


class ProtocolError(Exception):
    """所有业务异常基类。"""
    code: str = "protocol_error"
    retryable: bool = False


class BinanceHTTPError(ProtocolError):
    code = "binance_http"


class BinanceAuthError(BinanceHTTPError):
    code = "binance_auth"


class BinanceRateLimitError(BinanceHTTPError):
    code = "binance_rate_limit"
    retryable = True


class BinanceServerError(BinanceHTTPError):
    code = "binance_5xx"
    retryable = True


class BinanceTimeskewError(BinanceHTTPError):
    code = "binance_timeskew"


class BinanceBusinessError(BinanceHTTPError):
    code = "binance_business"

    def __init__(self, binance_code: int = 0, msg: str = ""):
        super().__init__(f"code={binance_code} msg={msg}")
        self.binance_code = binance_code


class ConfigError(ProtocolError):
    code = "bad_config"


class SignalValidationError(ProtocolError):
    code = "bad_signal"


class InsufficientNotionalError(ProtocolError):
    code = "min_notional"


class SLDistanceError(ProtocolError):
    code = "sl_too_close"


class EmergencyCloseFailedError(ProtocolError):
    code = "emergency_close_failed"

    def __init__(self, symbol: str = "", qty: float = 0.0):
        super().__init__(f"naked position {symbol} qty={qty}")
        self.symbol = symbol
        self.qty = qty
