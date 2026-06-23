from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from auth import protocol_token_configured, require_auth


def test_protocol_auth_open_mode_without_token(monkeypatch):
    monkeypatch.delenv("PROTOCOL_MAINTENANCE_TOKEN", raising=False)

    assert protocol_token_configured() is False


@pytest.mark.asyncio
async def test_protocol_auth_accepts_header_token(monkeypatch):
    monkeypatch.setenv("PROTOCOL_MAINTENANCE_TOKEN", "test-token")

    assert protocol_token_configured() is True
    await require_auth("test-token", None)


@pytest.mark.asyncio
async def test_protocol_auth_accepts_bearer_token(monkeypatch):
    monkeypatch.setenv("PROTOCOL_MAINTENANCE_TOKEN", "test-token")

    await require_auth(None, "Bearer test-token")


@pytest.mark.asyncio
async def test_protocol_auth_rejects_missing_or_wrong_token(monkeypatch):
    monkeypatch.setenv("PROTOCOL_MAINTENANCE_TOKEN", "test-token")

    with pytest.raises(HTTPException) as missing:
        await require_auth(None, None)
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as wrong:
        await require_auth("wrong", None)
    assert wrong.value.status_code == 401


def test_ingest_endpoint_requires_token_when_configured(monkeypatch):
    monkeypatch.setenv("PROTOCOL_MAINTENANCE_TOKEN", "test-token")
    import main

    client = TestClient(main.app)
    missing = client.post("/api/binance/signals/ingest", json={"signals": []})
    accepted = client.post(
        "/api/binance/signals/ingest",
        json={"signals": []},
        headers={"X-Maintenance-Token": "test-token"},
    )

    assert missing.status_code == 401
    assert accepted.status_code == 200
