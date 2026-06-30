"""Unit: trading/protective.py — SL mark-distance widen."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from trading.protective import (
    place_sl_with_mark_retries,
    protective_placement_retryable,
    resolve_sl_price_for_mark,
    sl_too_close_to_mark,
    validate_sl_distance,
    widen_sl_for_mark,
)


class TestSlMarkDistance:
    def test_usar_long_too_close(self):
        """USARUSDT: SL 21.3 vs mark 21.2971 — widen below mark margin."""
        assert sl_too_close_to_mark("LONG", 21.3, 21.2971, "0.01")
        widened = widen_sl_for_mark("LONG", 21.3, 21.2971, "0.01")
        assert widened < 21.2971 - 0.02
        validate_sl_distance("LONG", widened, 21.2971, "0.01")

    def test_resolve_usar(self):
        sl, widened = resolve_sl_price_for_mark("LONG", 21.3, 21.2971, "0.01")
        assert widened is True
        assert sl == 21.27
        validate_sl_distance("LONG", sl, 21.2971, "0.01")

    def test_no_change_when_valid(self):
        sl, widened = resolve_sl_price_for_mark("LONG", 66500.0, 67250.0, "0.10")
        assert widened is False
        assert sl == 66500.0

    def test_short_widen_up(self):
        mark = 100.0
        tick = "0.01"
        margin = max(0.02, mark * 0.0005)
        bad_sl = mark + margin - 0.005
        assert sl_too_close_to_mark("SHORT", bad_sl, mark, tick)
        widened = widen_sl_for_mark("SHORT", bad_sl, mark, tick)
        validate_sl_distance("SHORT", widened, mark, tick)
        assert widened > bad_sl


class TestSlPlaceRetries:
    def test_retry_on_immediate_trigger(self):
        calls: list[float] = []

        def place_fn(sl: float) -> dict:
            calls.append(sl)
            if len(calls) == 1:
                raise ValueError("Order would immediately trigger. (-2021)")
            return {"algoId": "sl-1"}

        sl, widened, resp = place_sl_with_mark_retries(
            place_fn=place_fn,
            side="LONG",
            sl_price=21.3,
            mark_px=21.2971,
            tick="0.01",
        )
        assert widened is True
        assert sl == pytest.approx(21.26)
        assert resp["algoId"] == "sl-1"
        assert len(calls) == 2

    def test_unfixable_raises(self):
        def place_fn(sl: float) -> dict:
            return {"algoId": "x"}

        with patch("trading.protective.sl_too_close_to_mark", return_value=True):
            with pytest.raises(ValueError, match="unfixable"):
                place_sl_with_mark_retries(
                    place_fn=place_fn,
                    side="LONG",
                    sl_price=21.29,
                    mark_px=21.29,
                    tick="0.01",
                )

    def test_non_retryable_raises_immediately(self):
        def place_fn(sl: float) -> dict:
            raise ValueError("insufficient margin")

        with pytest.raises(ValueError, match="insufficient"):
            place_sl_with_mark_retries(
                place_fn=place_fn,
                side="LONG",
                sl_price=21.3,
                mark_px=21.2971,
                tick="0.01",
                max_attempts=3,
            )

    def test_retryable_detection(self):
        assert protective_placement_retryable(ValueError("immediately trigger"))
        assert not protective_placement_retryable(ValueError("insufficient margin"))
