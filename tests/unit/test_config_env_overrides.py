"""apply_env_config_overrides 应覆盖 Volume 里陈旧的 enabled/testnet。"""
from __future__ import annotations


def test_apply_env_overrides_testnet_on_stale_volume(fresh_db, monkeypatch):
    fresh_db.set_config("testnet", "true")
    fresh_db.set_config("enabled", "false")

    monkeypatch.setenv("BINANCE_TESTNET", "false")
    monkeypatch.setenv("BINANCE_ENABLED", "true")

    fresh_db.apply_env_config_overrides()

    assert fresh_db.get_config("testnet") == "false"
    assert fresh_db.get_config("enabled") == "true"


def test_apply_env_overrides_skips_unset_env(fresh_db, monkeypatch):
    fresh_db.set_config("testnet", "true")
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)

    fresh_db.apply_env_config_overrides()

    assert fresh_db.get_config("testnet") == "true"
