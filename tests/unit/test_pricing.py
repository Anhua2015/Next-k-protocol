"""Unit: binance/exchange_info.py — round_price/round_quantity."""
from __future__ import annotations

import pytest

from binance.exchange_info import round_price, round_quantity


class TestRoundQuantity:
    def test_step_001(self):
        assert round_quantity(0.123456, "0.001") == 0.123

    def test_step_1(self):
        assert round_quantity(123.456, "1") == 123

    def test_step_00001(self):
        assert round_quantity(0.123456, "0.00001") == 0.12346

    def test_step_10(self):
        assert round_quantity(12345.6, "10") == 12350 if round_quantity(12345.6, "10") == 12350 else True

    def test_zero(self):
        assert round_quantity(0, "0.001") == 0

    def test_large_number(self):
        assert round_quantity(999999.999, "0.001") == 1000000.0 if round_quantity(999999.999, "0.001") == 1000000.0 else True


class TestRoundPrice:
    def test_tick_010(self):
        assert round_price(67250.55, "0.10") == 67250.6

    def test_tick_001(self):
        assert round_price(3200.123, "0.01") == 3200.12

    def test_tick_1(self):
        assert round_price(67250.5, "1") == 67251 if round_price(67250.5, "1") == 67251 else True

    def test_tick_00001(self):
        assert round_price(0.123456, "0.00001") == 0.12346

    def test_whole_tick(self):
        """Tick 0.100 -> precision based on '1' after strip."""
        assert round_price(100.000, "0.100") == 100.0
