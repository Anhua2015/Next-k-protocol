"""Characterization: Moss Quant ingest on the simplified protocol."""
from __future__ import annotations

import re

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
        "margin_usdt": 50.0,
        "leverage": 8,
        "entry_price": 67250.5,
        "sl_price": 66500.0,
        "tp_price": 68500.0,
        "play": "balanced",
    }
    base.update(overrides)
    return {"signals": [base]}


def test_moss_quant_source_accepted(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_closed")
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(api_signal_id="mq-test-001"),
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert resp.json()["traded"] == 1
    assert resp.json()["details"][0]["action"] == "traded"


def test_moss_quant_open_requires_explicit_leverage(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_closed")
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(api_signal_id="mq-test-no-lev", leverage=None),
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert resp.json()["errors"] == 1
    assert resp.json()["details"][0]["action"] == "error"


def test_moss_quant_rolling_forces_market_when_global_entry_type_is_limit(
    seeded_config, mock_binance
):
    seeded_config.set_config("entry_type", "LIMIT")
    mock_binance.all(
        place_order="place_order_market_filled",
        position_risk="position_risk_closed",
    )
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
    assert resp.json()["traded"] == 1
    logs = client.get(
        "/api/binance/signals?source=moss_quant&action=rolling&limit=10",
        headers=AUTH,
    ).json()
    assert logs[0]["status"] == "traded"


def test_moss_quant_close_bypasses_open_position_guard(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_open")
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(
            api_signal_id="mq-close-live-1",
            margin_usdt=None,
            leverage=None,
            entry_price=None,
            sl_price=None,
            tp_price=None,
            close_price=67280.0,
            action="close",
            play="take_profit",
        ),
        headers=AUTH,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["traded"] == 1
    assert body["details"][0]["action"] == "traded"


def test_moss_quant_update_sl_bypasses_open_position_guard(seeded_config, mock_binance):
    mock_binance.all(position_risk="position_risk_open")
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(
            api_signal_id="mq-update-sl-live-1",
            margin_usdt=None,
            leverage=None,
            entry_price=None,
            tp_price=None,
            sl_price=66800.0,
            action="update_sl",
            play="trailing_stop",
        ),
        headers=AUTH,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["traded"] == 1
    assert body["details"][0]["action"] == "traded"


def test_moss_quant_update_sl_logs_cancel_and_place(
    seeded_config, httpx_mock, load_binance_fixture, caplog
):
    base = "https://testnet.binancefuture.com"
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/time')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("server_time"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/exchangeInfo')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("exchange_info_btcusdt"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v2/positionRisk')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("position_risk_open"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/positionSide/dual')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("position_side_single"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/premiumIndex')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("premium_index_btcusdt"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("get_open_algo_orders_sl_working"),
        is_optional=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("cancel_order_success"),
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=[],
        is_optional=True,
    )
    httpx_mock.add_response(
        method="POST",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("place_algo_order_success"),
        is_optional=True,
    )

    caplog.set_level("INFO")
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(
            api_signal_id="mq-update-sl-log-1",
            margin_usdt=None,
            leverage=None,
            entry_price=None,
            tp_price=None,
            sl_price=66800.0,
            action="update_sl",
            play="trailing_stop",
        ),
        headers=AUTH,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["traded"] == 1
    log_text = caplog.text
    assert "update_sl context symbol=BTCUSDT" in log_text
    assert "live_pos={symbol=BTCUSDT position_amt=0.012 position_side=BOTH" in log_text
    assert "cancel protective orders symbol=BTCUSDT kind=SL" in log_text
    assert "cancel protective order symbol=BTCUSDT kind=SL algo_id=33333333" in log_text
    assert "cancel protective order result symbol=BTCUSDT kind=SL algo_id=33333333 ok=True" in log_text
    assert "place protective order symbol=BTCUSDT kind=SL trigger=66800.0 qty=0.012" in log_text
    assert "placed protective order symbol=BTCUSDT kind=SL" in log_text


def test_moss_quant_update_sl_logs_raw_open_orders_and_skip_reason(
    seeded_config, httpx_mock, load_binance_fixture, caplog
):
    base = "https://testnet.binancefuture.com"
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/time')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("server_time"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/exchangeInfo')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("exchange_info_btcusdt"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v2/positionRisk')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("position_risk_open"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/positionSide/dual')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("position_side_single"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/premiumIndex')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("premium_index_btcusdt"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=[
            {
                "algoId": 44444444,
                "symbol": "BTCUSDT",
                "side": "BUY",
                "positionSide": "BOTH",
                "type": "STOP_MARKET",
                "status": "WORKING",
            }
        ],
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=[],
        is_optional=True,
    )
    httpx_mock.add_response(
        method="POST",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("place_algo_order_success"),
        is_optional=True,
    )

    caplog.set_level("INFO")
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(
            api_signal_id="mq-update-sl-raw-log-1",
            margin_usdt=None,
            leverage=None,
            entry_price=None,
            tp_price=None,
            sl_price=66800.0,
            action="update_sl",
            play="trailing_stop",
        ),
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert resp.json()["traded"] == 1
    log_text = caplog.text
    assert "openAlgoOrders raw symbol=BTCUSDT count=1" in log_text
    assert "algo_id=44444444" in log_text
    assert "protective order skipped symbol=BTCUSDT kind=SL" in log_text
    assert "reason=side_mismatch" in log_text


def test_moss_quant_update_sl_resolves_conditional_orders_via_algo_detail(
    seeded_config, httpx_mock, load_binance_fixture, caplog
):
    base = "https://testnet.binancefuture.com"
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/time')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("server_time"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/exchangeInfo')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("exchange_info_btcusdt"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v2/positionRisk')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("position_risk_open"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/positionSide/dual')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("position_side_single"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/premiumIndex')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("premium_index_btcusdt"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=[
            {
                "algoId": 11111111,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "BOTH",
                "algoType": "CONDITIONAL",
                "status": "NEW",
            },
            {
                "algoId": 22222222,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "BOTH",
                "algoType": "CONDITIONAL",
                "status": "NEW",
            },
        ],
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=[
            {
                "algoId": 22222222,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "BOTH",
                "algoType": "CONDITIONAL",
                "status": "NEW",
            },
        ],
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*algoId=11111111.*)?$"),
        status_code=200,
        json={
            "algoId": 11111111,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "BOTH",
            "type": "STOP_MARKET",
            "origType": "STOP_MARKET",
            "algoType": "CONDITIONAL",
            "status": "NEW",
        },
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*algoId=22222222.*)?$"),
        status_code=200,
        json={
            "algoId": 22222222,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "BOTH",
            "type": "TAKE_PROFIT_MARKET",
            "origType": "TAKE_PROFIT_MARKET",
            "algoType": "CONDITIONAL",
            "status": "NEW",
        },
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*algoId=11111111.*)?$"),
        status_code=200,
        json=load_binance_fixture("cancel_order_success"),
        is_optional=True,
    )
    httpx_mock.add_response(
        method="POST",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("place_algo_order_success"),
        is_optional=True,
    )

    caplog.set_level("INFO")
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(
            api_signal_id="mq-update-sl-detail-1",
            margin_usdt=None,
            leverage=None,
            entry_price=None,
            tp_price=None,
            sl_price=66800.0,
            action="update_sl",
            play="trailing_stop",
        ),
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert resp.json()["traded"] == 1
    log_text = caplog.text
    assert "algoOrder detail algo_id=11111111" in log_text
    assert "algoOrder detail algo_id=22222222" in log_text
    assert "cancel protective orders symbol=BTCUSDT kind=SL" in log_text
    assert "count=1 algo_ids=11111111" in log_text
    delete_calls = [
        r for r in httpx_mock.get_requests() if "/fapi/v1/algoOrder" in str(r.url) and r.method == "DELETE"
    ]
    assert len(delete_calls) == 1
    assert "algoId=11111111" in str(delete_calls[0].url)


def test_moss_quant_update_tp_resolves_conditional_orders_via_trigger_price(
    seeded_config, httpx_mock, load_binance_fixture, caplog
):
    base = "https://testnet.binancefuture.com"
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/time')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("server_time"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/exchangeInfo')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("exchange_info_btcusdt"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v2/positionRisk')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("position_risk_open"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/positionSide/dual')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("position_side_single"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/premiumIndex')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("premium_index_btcusdt"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=[
            {
                "algoId": 11111111,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "BOTH",
                "algoType": "CONDITIONAL",
                "status": "NEW",
                "triggerPrice": "66800.0",
            },
            {
                "algoId": 22222222,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "BOTH",
                "algoType": "CONDITIONAL",
                "status": "NEW",
                "triggerPrice": "68500.0",
            },
        ],
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=[
            {
                "algoId": 11111111,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "BOTH",
                "algoType": "CONDITIONAL",
                "status": "NEW",
                "triggerPrice": "66800.0",
            },
        ],
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*algoId=11111111.*)?$"),
        status_code=200,
        json={
            "algoId": 11111111,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "BOTH",
            "algoType": "CONDITIONAL",
            "status": "NEW",
            "triggerPrice": "66800.0",
        },
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*algoId=22222222.*)?$"),
        status_code=200,
        json={
            "algoId": 22222222,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "BOTH",
            "algoType": "CONDITIONAL",
            "status": "NEW",
            "triggerPrice": "68500.0",
        },
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*algoId=22222222.*)?$"),
        status_code=200,
        json=load_binance_fixture("cancel_order_success"),
        is_optional=True,
    )
    httpx_mock.add_response(
        method="POST",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("place_algo_order_success"),
        is_optional=True,
    )

    caplog.set_level("INFO")
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(
            api_signal_id="mq-update-tp-trigger-1",
            margin_usdt=None,
            leverage=None,
            entry_price=None,
            sl_price=None,
            tp_price=68500.0,
            action="update_tp",
            play="trailing_stop",
        ),
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert resp.json()["traded"] == 1
    log_text = caplog.text
    assert "algoOrder detail algo_id=11111111" in log_text
    assert "algoOrder detail algo_id=22222222" in log_text
    assert "cancel protective orders symbol=BTCUSDT kind=TP" in log_text
    assert "count=1 algo_ids=22222222" in log_text
    delete_calls = [
        r for r in httpx_mock.get_requests() if "/fapi/v1/algoOrder" in str(r.url) and r.method == "DELETE"
    ]
    assert len(delete_calls) == 1
    assert "algoId=22222222" in str(delete_calls[0].url)


def test_moss_quant_update_sl_aborts_when_old_sl_still_open(
    seeded_config, httpx_mock, load_binance_fixture, monkeypatch
):
    base = "https://testnet.binancefuture.com"
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/time')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("server_time"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/exchangeInfo')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("exchange_info_btcusdt"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v2/positionRisk')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("position_risk_open"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/positionSide/dual')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("position_side_single"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/premiumIndex')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("premium_index_btcusdt"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("get_open_algo_orders_sl_working"),
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("get_open_algo_orders_sl_working"),
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("get_open_algo_orders_sl_working"),
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("get_open_algo_orders_sl_working"),
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("get_open_algo_orders_sl_working"),
        is_optional=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("cancel_order_success"),
        is_optional=True,
    )
    httpx_mock.add_response(
        method="POST",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("place_algo_order_success"),
        is_reusable=True,
        is_optional=True,
    )

    client = _client(seeded_config)
    import trader

    monkeypatch.setattr(trader, "_PROTECTIVE_CANCEL_RETRY_DELAY_SEC", 0.0)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(
            api_signal_id="mq-update-sl-stale-1",
            margin_usdt=None,
            leverage=None,
            entry_price=None,
            tp_price=None,
            sl_price=66800.0,
            action="update_sl",
            play="trailing_stop",
        ),
        headers=AUTH,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["errors"] == 1
    assert body["details"][0]["action"] == "error"

    place_calls = [
        r for r in httpx_mock.get_requests() if "/fapi/v1/algoOrder" in str(r.url) and r.method == "POST"
    ]
    assert place_calls == []


def test_moss_quant_update_sl_retries_when_old_sl_clears_after_delay(
    seeded_config, httpx_mock, load_binance_fixture, caplog, monkeypatch
):
    base = "https://testnet.binancefuture.com"
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/time')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("server_time"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/exchangeInfo')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("exchange_info_btcusdt"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v2/positionRisk')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("position_risk_open"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/positionSide/dual')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("position_side_single"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/premiumIndex')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("premium_index_btcusdt"),
        is_reusable=True,
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("get_open_algo_orders_sl_working"),
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("get_open_algo_orders_sl_working"),
        is_optional=True,
    )
    httpx_mock.add_response(
        method="GET",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/openAlgoOrders')}(\?.*)?$"),
        status_code=200,
        json=[],
        is_optional=True,
    )
    httpx_mock.add_response(
        method="DELETE",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("cancel_order_success"),
        is_optional=True,
    )
    httpx_mock.add_response(
        method="POST",
        url=re.compile(rf"^{re.escape(base + '/fapi/v1/algoOrder')}(\?.*)?$"),
        status_code=200,
        json=load_binance_fixture("place_algo_order_success"),
        is_optional=True,
    )

    caplog.set_level("INFO")
    client = _client(seeded_config)
    import trader

    monkeypatch.setattr(trader, "_PROTECTIVE_CANCEL_RETRY_DELAY_SEC", 0.0)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(
            api_signal_id="mq-update-sl-retry-1",
            margin_usdt=None,
            leverage=None,
            entry_price=None,
            tp_price=None,
            sl_price=66800.0,
            action="update_sl",
            play="trailing_stop",
        ),
        headers=AUTH,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["traded"] == 1
    log_text = caplog.text
    assert "cancel protective orders pending retry symbol=BTCUSDT kind=SL attempt=1/3" in log_text
    assert "cancel protective orders cleared symbol=BTCUSDT kind=SL" in log_text
    assert "place protective order symbol=BTCUSDT kind=SL trigger=66800.0 qty=0.012" in log_text


def test_moss_quant_rolling_bypasses_position_and_max_position_guards(
    seeded_config, mock_binance
):
    seeded_config.set_config("entry_type", "LIMIT")
    seeded_config.set_config("max_positions", "0")
    mock_binance.all(
        place_order="place_order_market_filled",
        position_risk="position_risk_open",
    )
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/signals/ingest",
        json=_mq_payload(
            api_signal_id="mq-roll-live-guard-1",
            entry_price=None,
            action="rolling",
            play="balanced_rolling",
        ),
        headers=AUTH,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["traded"] == 1
    assert body["details"][0]["action"] == "traded"


def test_update_sl_endpoint_removed(seeded_config):
    client = _client(seeded_config)

    resp = client.put(
        "/api/binance/positions/99999/sl",
        json={"new_sl_price": 66000.0},
        headers=AUTH,
    )

    assert resp.status_code == 410


def test_close_endpoint_removed(seeded_config):
    client = _client(seeded_config)

    resp = client.post(
        "/api/binance/positions/close",
        json={
            "source": "moss_quant",
            "api_signal_id": "mq-close-1",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "exit_rule": "manual",
        },
        headers=AUTH,
    )

    assert resp.status_code == 410
