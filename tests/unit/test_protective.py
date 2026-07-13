"""Unit: trading/protective.py — SL mark-distance widen."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from trading.protective import (
    emergency_close_strict,
    place_sl_strict,
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


class TestSlStrict:
    def test_strict_rejects_widen(self):
        with pytest.raises(ValueError, match="too close"):
            place_sl_strict(
                place_fn=lambda sl: {"algoId": "1"},
                side="LONG",
                sl_price=21.3,
                mark_px=21.2971,
                tick="0.01",
            )

    def test_strict_places_valid_sl(self):
        sl, resp = place_sl_strict(
            place_fn=lambda sl: {"algoId": "1"},
            side="LONG",
            sl_price=21.27,
            mark_px=21.2971,
            tick="0.01",
        )
        assert sl == 21.27
        assert resp["algoId"] == "1"

    def test_strict_retries_same_price(self):
        calls: list[float] = []

        def place_fn(sl: float) -> dict:
            calls.append(sl)
            if len(calls) == 1:
                raise ValueError("Order would immediately trigger. (-2021)")
            return {"algoId": "sl-1"}

        sl, resp = place_sl_strict(
            place_fn=place_fn,
            side="LONG",
            sl_price=21.27,
            mark_px=21.2971,
            tick="0.01",
        )
        assert sl == 21.27
        assert len(calls) == 2
        assert resp["algoId"] == "sl-1"

    def test_strict_refreshes_mark_on_retry(self):
        mark_calls = [0]
        calls: list[float] = []

        def mark_fn() -> float:
            mark_calls[0] += 1
            return 21.2971

        def place_fn(sl: float) -> dict:
            calls.append(sl)
            if len(calls) == 1:
                raise ValueError("Order would immediately trigger. (-2021)")
            return {"algoId": "sl-1"}

        sl, resp = place_sl_strict(
            place_fn=place_fn,
            side="LONG",
            sl_price=21.27,
            mark_px=21.2971,
            tick="0.01",
            mark_px_fn=mark_fn,
        )
        assert sl == 21.27
        assert len(calls) == 2
        assert mark_calls[0] >= 2
        assert resp["algoId"] == "sl-1"

    def test_strict_fails_if_mark_moves_against_sl(self):
        marks = [21.2971, 21.2800]

        def mark_fn() -> float:
            idx = min(len(marks) - 1, 1)
            return marks[idx if len(marks) > 1 else 0]

        call_count = [0]

        def place_fn(sl: float) -> dict:
            call_count[0] += 1
            if call_count[0] == 1:
                raise ValueError("Order would immediately trigger. (-2021)")
            return {"algoId": "sl-1"}

        with pytest.raises(ValueError, match="too close"):
            place_sl_strict(
                place_fn=place_fn,
                side="LONG",
                sl_price=21.27,
                mark_px=21.2971,
                tick="0.01",
                mark_px_fn=lambda: marks[1] if call_count[0] >= 1 else marks[0],
            )


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


class TestEmergencyCloseStrict:
    def test_raises_when_close_fails(self):
        from common.exceptions import EmergencyCloseFailedError

        class FakeClient:
            pass

        with patch("trading.protective.emergency_close", return_value=None):
            with pytest.raises(EmergencyCloseFailedError):
                emergency_close_strict(FakeClient(), "BTCUSDT", "LONG", 0.01, None)
