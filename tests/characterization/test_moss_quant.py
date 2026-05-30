"""Characterization: Moss Quant integration with Next-k-protocol.

Verifies:
- moss_quant source accepted as valid
- moss_quant exempt from position_exists guard
- PU T /sl endpoint modifies SL
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.characterization

AUTH = {"X-Maintenance-Token": "test-token"}


def _client(seeded_config):
    import importlib
    import sys
    for mod in ("main", "router", "trader"):
        sys.modules.pop(mod, None)
    import main
    importlib.reload(main)
    return TestClient(main.app)


def _mq_payload(**overrides):
    base = {
        "source": "moss_quant",
        "api_signal_id": "moss_sig-001",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry_price": 67250.5,
        "sl_price": 66500.0,
        "tp_price": 68500.0,
        "notional_usdt": 500.0,
        "play": "balanced",
    }
    base.update(overrides)
    return {"signals": [base]}


def _open_moss_position(seeded_config, mock_binance, client=None):
    """Helper: open a moss_quant position via ingest. Returns client."""
    mock_binance.all()
    cl = client or _client(seeded_config)
    cl.post("/api/binance/signals/ingest", json=_mq_payload(), headers=AUTH)
    return cl


# -- source validation --------------------------------------------------------

def test_moss_quant_source_accepted(seeded_config, mock_binance):
    """moss_quant is a valid source, not rejected as invalid_source."""
    mock_binance.all()
    client = _client(seeded_config)
    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(api_signal_id="mq-test-001"),
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["traded"] == 1
    assert body["details"][0]["action"] == "traded"


def test_moss_quant_missing_notional_rejected(seeded_config, mock_binance):
    """moss_quant without notional_usdt returns error."""
    mock_binance.all()
    client = _client(seeded_config)
    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(api_signal_id="mq-no-not", notional_usdt=None),
        headers=AUTH,
    )
    body = resp.json()
    assert body["errors"] == 1


# -- position_exists exemption ------------------------------------------------

def test_moss_quant_bypasses_position_exists(seeded_config, mock_binance):
    """moss_quant can open same symbol multiple times (rolling)."""
    mock_binance.all()
    client = _client(seeded_config)

    # First open
    r1 = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(api_signal_id="mq-dup-1"),
        headers=AUTH,
    )
    b1 = r1.json()
    assert b1["traded"] == 1

    # Second open on same symbol - should also trade (not skipped)
    r2 = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(api_signal_id="mq-dup-2"),
        headers=AUTH,
    )
    b2 = r2.json()
    assert b2["traded"] == 1, f"expected traded, got {b2['details'][0]}"


def test_moss_quant_rolling_forces_market_when_source_entry_type_is_limit(seeded_config, mock_binance):
    """rolling 信号在 LIMIT 配置下仍应按 MARKET 成交，而不是 pending/报缺少 entry_price。"""
    seeded_config.set_config("src_moss_quant_entry_type", "LIMIT")
    mock_binance.all(place_order="place_order_market_filled")
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(
            api_signal_id="mq-roll-limit-1",
            entry_price=None,
            action="rolling",
            play="balanced_rolling",
        ),
        headers=AUTH,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["traded"] == 1, body
    assert body["details"][0]["action"] == "traded"

    logs = client.get(
        "/api/binance/signals?source=moss_quant&action=rolling&limit=10",
        headers=AUTH,
    ).json()
    assert logs[0]["status"] == "traded"


def test_non_moss_quant_still_blocked_by_position_exists(seeded_config, mock_binance):
    """zct_vwap is still blocked by position_exists (no regression)."""
    mock_binance.all()
    client = _client(seeded_config)

    # Open with zct_vwap
    r1 = client.post(
        "/api/binance/signals/ingest",
        json={
            "signals": [{
                "source": "zct_vwap", "api_signal_id": "zct-001",
                "symbol": "ETHUSDT", "side": "LONG",
                "sl_price": 2500.0, "play": "PLAY01",
            }]
        },
        headers=AUTH,
    )
    assert r1.json()["traded"] == 1

    # Second zct_vwap on same symbol should be skipped
    r2 = client.post(
        "/api/binance/signals/ingest",
        json={
            "signals": [{
                "source": "zct_vwap", "api_signal_id": "zct-002",
                "symbol": "ETHUSDT", "side": "LONG",
                "sl_price": 2550.0, "play": "PLAY01",
            }]
        },
        headers=AUTH,
    )
    body = r2.json()
    assert body["details"][0]["action"] == "skipped_position_exists"


# -- config initialization ----------------------------------------------------

def test_moss_quant_default_config_present(seeded_config):
    """Moss Quant config keys initialized with defaults."""
    import db
    assert db.get_config("src_moss_quant_enabled") == "true"
    assert db.get_config("src_moss_quant_leverage") == "10"
    assert db.get_config("src_moss_quant_max_positions") == "10"
    assert db.get_config("src_moss_quant_expire_hours") == "24"
    assert db.get_config("src_moss_quant_entry_type") == "MARKET"


# -- slack update endpoint ----------------------------------------------------

def test_update_sl_success(seeded_config, mock_binance):
    """PUT /positions/{id}/sl updates SL order successfully."""
    mock_binance.all()
    client = _client(seeded_config)

    # Open a moss position first
    _open_moss_position(seeded_config, mock_binance, client=client)

    # Get position ID
    import db
    positions = db.get_open_positions()
    assert len(positions) == 1
    pos_id = positions[0]["id"]

    # Update SL
    mock_binance("cancel_algo", "cancel_order_success")
    mock_binance("place_algo", "place_algo_order_success")
    new_sl = 66000.0
    resp = client.put(
        f"/api/binance/positions/{pos_id}/sl",
        json={"new_sl_price": new_sl},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["position_id"] == pos_id
    assert body["new_sl_price"] == new_sl


def test_update_sl_nonexistent_position(seeded_config, mock_binance):
    """PUT /sl on non-existent position returns 404."""
    client = _client(seeded_config)
    resp = client.put(
        "/api/binance/positions/99999/sl",
        json={"new_sl_price": 66000.0},
        headers=AUTH,
    )
    assert resp.status_code == 404


def test_update_sl_invalid_price(seeded_config, mock_binance):
    """PUT /sl with sl_price <= 0 returns 400."""
    mock_binance.all()
    client = _client(seeded_config)
    _open_moss_position(seeded_config, mock_binance, client=client)

    import db
    positions = db.get_open_positions()
    pos_id = positions[0]["id"]

    resp = client.put(
        f"/api/binance/positions/{pos_id}/sl",
        json={"new_sl_price": 0},
        headers=AUTH,
    )
    assert resp.status_code == 400
