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

    # Force reload so DB_PATH picks up new DATA_DIR
    if "db" in sys.modules:
        del sys.modules["db"]
    import db as db_module
    importlib.reload(db_module)
    db_module.init_db()
    yield db_module


@pytest.fixture
def seeded_config(fresh_db):
    """Apply a sane default trading config for tests that need it."""
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
    return fresh_db


@pytest.fixture
def load_binance_fixture() -> Callable[[str], Any]:
    """Return a callable that loads a JSON fixture by name (without .json)."""
    def _load(name: str) -> Any:
        path = FIXTURES_DIR / f"{name}.json"
        return json.loads(path.read_text())
    return _load
