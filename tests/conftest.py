"""Shared test fixtures.

Goals:
- Each test gets a fresh SQLite file under tmp dir — no shared state.
- Env vars set to a deterministic baseline before db.init_db() runs.
- A reusable `load_binance_fixture` helper for JSON response replay.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "binance"


@pytest.fixture(autouse=True)
def _env_baseline(tmp_path, monkeypatch):
    """Set deterministic env vars + isolate DB to tmpdir for every test."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PROTOCOL_MAINTENANCE_TOKEN", "test-token")
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("EMBED_SCHEDULER", "0")
    monkeypatch.delenv("PROTOCOL_CORS_ORIGINS", raising=False)
    # Unset system proxy vars so httpx.Client() doesn't try SOCKS
    for _proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(_proxy_var, raising=False)
    yield


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Initialize a fresh binance.db under tmpdir and yield the module."""
    import importlib
    import sys

    # Clear all modules that cache DB_PATH or db function references
    for _mod in list(sys.modules):
        if _mod.startswith("repos.") or _mod == "db":
            del sys.modules[_mod]

    import db as db_module
    importlib.reload(db_module)
    db_module.init_db()
    yield db_module


@pytest.fixture
def seeded_config(fresh_db):
    """Apply a sane default trading config + init binance client for tests."""
    fresh_db.set_config_batch({
        "enabled": "true",
        "testnet": "true",
        "margin_usdt": "100",
        "leverage": "10",
        "entry_type": "MARKET",
        "max_positions": "8",
        "max_positions_play01": "5",
        "max_positions_play02": "5",
        "max_positions_play03": "5",
        "src_zct_vwap_enabled": "true",
        "src_momentum_enabled": "true",
        "src_jiezhen_enabled": "true",
        "src_momentum_max_positions": "3",
        "src_jiezhen_max_positions": "3",
        "binance_api_key": "test-key",
        "binance_api_secret": "test-secret",
    })
    # Init/reset the binance HTTP client (Phase 1 lazy singleton)
    import sys
    import binance.account as _bacct
    # Clear modules that cache db references across test reloads.
    for _mod in ("ingest.pipeline", "ingest.guards", "ingest.dispatcher",
                 "trading.market_entry", "trading.limit_entry",
                 "lifecycle.close", "lifecycle.sync", "lifecycle.reconcile",
                 "lifecycle.expire",
                 "repos.connection", "repos.config_repo", "repos.signals_repo",
                 "repos.positions_repo", "repos.pnl_repo"):
        sys.modules.pop(_mod, None)
    # Also clear db facade so it re-imports from repos
    sys.modules.pop("db", None)
    from binance.client import init_client
    from db import get_config as _cfg
    init_client(
        base_url_fn=lambda: (
            "https://testnet.binancefuture.com"
            if _cfg("testnet", "false").lower() == "true"
            else "https://fapi.binance.com"
        ),
        api_key_fn=lambda: _cfg("binance_api_key", ""),
        secret_fn=lambda: _cfg("binance_api_secret", ""),
    )
    # Clear caches that persist across test DB reloads
    _bacct._hedge_mode_cache = None
    import binance.exchange_info as _exch
    _exch._exch_cache.clear()
    return fresh_db


@pytest.fixture
def load_binance_fixture() -> Callable[[str], Any]:
    """Return a callable that loads a JSON fixture by name (without .json)."""
    def _load(name: str) -> Any:
        path = FIXTURES_DIR / f"{name}.json"
        return json.loads(path.read_text())
    return _load
