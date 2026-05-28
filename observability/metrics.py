"""Prometheus 指标。"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# -- Counters ---------------------------------------------------------------
SIGNALS_RECEIVED = Counter(
    "protocol_signals_received_total", "Signals received", ["source", "play"])
SIGNALS_SKIPPED = Counter(
    "protocol_signals_skipped_total", "Signals skipped", ["source", "code"])
SIGNALS_DUPLICATE = Counter(
    "protocol_signals_duplicate_total", "Duplicate signals", ["source"])
TRADES_OPENED = Counter(
    "protocol_trades_opened_total", "Trades opened", ["source", "side", "entry_type"])
TRADES_FAILED = Counter(
    "protocol_trades_failed_total", "Trades failed", ["source", "stage", "code"])
POSITIONS_CLOSED = Counter(
    "protocol_positions_closed_total", "Positions closed",
    ["source", "close_reason"])
EMERGENCY_CLOSE_FAILED = Counter(
    "protocol_emergency_close_failed_total", "Emergency close failures", ["symbol"])
AUTH_FAIL = Counter(
    "protocol_auth_fail_total", "Binance auth failures")
TRADING_DISABLED_AUTO = Counter(
    "protocol_trading_disabled_auto_total", "Auto-disabled events")
BINANCE_REQUESTS = Counter(
    "protocol_binance_requests_total", "Binance API calls",
    ["method", "path", "status_class"])
BINANCE_RETRIES = Counter(
    "protocol_binance_retries_total", "Binance retries", ["path", "reason"])
EXCH_INFO_CACHE = Counter(
    "protocol_exchange_info_cache_total", "Exchange info cache", ["result"])

# -- Histograms -------------------------------------------------------------
BINANCE_LATENCY = Histogram(
    "protocol_binance_request_seconds", "Binance request latency",
    ["method", "path"], buckets=[.05, .1, .25, .5, 1, 2, 5])
TRADE_OPEN_LATENCY = Histogram(
    "protocol_trade_open_seconds", "Trade open latency",
    ["entry_type"], buckets=[.1, .5, 1, 2, 5])

# -- Gauges -----------------------------------------------------------------
OPEN_POSITIONS_GAUGE = Gauge(
    "protocol_open_positions", "Open positions count", ["source"])
PENDING_POSITIONS_GAUGE = Gauge(
    "protocol_pending_positions", "Pending positions count")
TRADING_ENABLED = Gauge(
    "protocol_trading_enabled", "Trading enabled (1/0)")
