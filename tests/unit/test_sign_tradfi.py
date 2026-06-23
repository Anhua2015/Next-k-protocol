"""TradFi-Perps 协议签署。"""
from __future__ import annotations

from binance.tradfi import TRADFI_AGREEMENT_PATH, sign_tradfi_perps_agreement


class _FakeClient:
    def __init__(self):
        self.calls = []

    def request(self, method, path, params, as_text=False):
        self.calls.append((method, path, params, as_text))
        return "SUCCESS"


def test_sign_tradfi_perps_agreement():
    client = _FakeClient()
    assert sign_tradfi_perps_agreement(client) == "SUCCESS"
    assert client.calls == [("POST", TRADFI_AGREEMENT_PATH, {}, True)]
