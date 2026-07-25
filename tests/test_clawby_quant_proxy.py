"""clawby-quant reverse-proxy path mapping."""

from __future__ import annotations

from routers.clawby_quant import map_api_upstream_path, map_ui_upstream_path
from utils.clawby_quant_runtime import _apply_binance_secret_alias


def test_map_api_no_double_api_prefix():
    assert map_api_upstream_path("api/status") == "/api/status"
    assert map_api_upstream_path("api/positions/1/close") == "/api/positions/1/close"
    assert map_api_upstream_path("/api/factors") == "/api/factors"
    assert map_api_upstream_path("") == "/"
    assert map_api_upstream_path("health") == "/health"


def test_map_ui_assets():
    assert map_ui_upstream_path("assets/index.js") == "/assets/index.js"
    assert map_ui_upstream_path("") == "/"
    assert map_ui_upstream_path("/") == "/"


def test_binance_secret_alias():
    env = {"BINANCE_API_SECRET": "sec-from-protocol"}
    _apply_binance_secret_alias(env)
    assert env["BINANCE_SECRET_KEY"] == "sec-from-protocol"

    env2 = {"BINANCE_SECRET_KEY": "already", "BINANCE_API_SECRET": "other"}
    _apply_binance_secret_alias(env2)
    assert env2["BINANCE_SECRET_KEY"] == "already"
