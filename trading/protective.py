"""SL/TP 条件单下单 + 紧急平仓。

所有函数通过 client 参数注入 BinanceClient。
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable, Dict, Optional, Tuple

from binance.client import BinanceClient
from binance.orders import cancel_all_orders, place_algo_order, place_order
from common.exceptions import EmergencyCloseFailedError

logger = logging.getLogger("trading.protective")


def build_protective_params(
    symbol: str, close_side: str, stop_price: float, qty: float,
    position_side: Optional[str], kind: str,
) -> Dict[str, Any]:
    """构建 SL/TP 条件单参数。"""
    order_type = "STOP_MARKET" if kind == "SL" else "TAKE_PROFIT_MARKET"
    params: Dict[str, Any] = {
        "algoType": "CONDITIONAL", "symbol": symbol, "side": close_side,
        "type": order_type, "triggerPrice": stop_price,
        "workingType": "MARK_PRICE", "quantity": qty, "priceProtect": "false",
    }
    if position_side:
        params["positionSide"] = position_side
    else:
        params["reduceOnly"] = "true"
    return params


def place_sl(
    client: BinanceClient, symbol: str, close_side: str,
    stop_price: float, qty: float, position_side: Optional[str],
) -> Dict[str, Any]:
    params = build_protective_params(symbol, close_side, stop_price, qty, position_side, "SL")
    return place_algo_order(client, params)


def place_tp(
    client: BinanceClient, symbol: str, close_side: str,
    stop_price: float, qty: float, position_side: Optional[str],
) -> Dict[str, Any]:
    params = build_protective_params(symbol, close_side, stop_price, qty, position_side, "TP")
    return place_algo_order(client, params)


def place_sl_tp(
    client: BinanceClient, symbol: str, close_side: str,
    sl_price: Optional[float], tp_price: Optional[float],
    qty: float, position_side: Optional[str],
) -> tuple:
    """下单 SL 和/或 TP 条件单。返回 (sl_order_id, tp_order_id)。"""
    sl_id = ""
    tp_id = ""
    if sl_price is not None:
        r = place_sl(client, symbol, close_side, sl_price, qty, position_side)
        sl_id = str(r.get("algoId", "") or r.get("orderId", ""))
        logger.info("SL placed: %s %s sl=%.6f algoId=%s", "protect", symbol, sl_price, sl_id)
    if tp_price is not None:
        r = place_tp(client, symbol, close_side, tp_price, qty, position_side)
        tp_id = str(r.get("algoId", "") or r.get("orderId", ""))
        logger.info("TP placed: %s %s tp=%.6f algoId=%s", "protect", symbol, tp_price, tp_id)
    return sl_id, tp_id


def emergency_close(
    client: BinanceClient, symbol: str, side: str,
    qty: float, position_side: Optional[str],
) -> Optional[str]:
    """紧急 MARKET 平仓（SL/TP 下单失败时调用）。"""
    close_side = "SELL" if side == "LONG" else "BUY"
    params: Dict[str, Any] = {
        "symbol": symbol, "side": close_side, "type": "MARKET",
        "quantity": qty, "reduceOnly": "true",
    }
    if position_side:
        params["positionSide"] = position_side
        params.pop("reduceOnly", None)
    try:
        resp = place_order(client, params)
        oid = str(resp.get("orderId", ""))
        logger.error("EMERGENCY close %s %s qty=%s order=%s", side, symbol, qty, oid)
        return oid
    except Exception as exc:
        logger.critical(
            "EMERGENCY close FAILED %s %s qty=%s — POSITION IS NAKED: %s",
            side, symbol, qty, exc,
        )
        return None


def emergency_close_strict(
    client: BinanceClient, symbol: str, side: str,
    qty: float, position_side: Optional[str],
) -> str:
    """紧急 MARKET 平仓；失败则抛 EmergencyCloseFailedError（裸仓告警）。"""
    oid = emergency_close(client, symbol, side, qty, position_side)
    if not oid:
        raise EmergencyCloseFailedError(symbol=symbol, qty=qty)
    return oid


def _parse_tick(tick: str) -> float:
    try:
        return float(tick)
    except (TypeError, ValueError):
        return 0.0


def mark_distance_margin(mark_px: float, tick: str) -> float:
    """Binance mark 价缓冲：max(2×tick, 0.05%×mark)。"""
    tick_f = _parse_tick(tick)
    return max(tick_f * 2.0, mark_px * 0.0005)


def sl_too_close_to_mark(side: str, sl_price: float, mark_px: float, tick: str) -> bool:
    margin = mark_distance_margin(mark_px, tick)
    if side == "LONG":
        return sl_price >= mark_px - margin
    return sl_price <= mark_px + margin


def widen_sl_for_mark(side: str, sl_price: float, mark_px: float, tick: str) -> float:
    """按 mark 放宽 SL（LONG 下移 / SHORT 上移），对齐 tick 后满足 validate_sl_distance。"""
    tick_f = _parse_tick(tick)
    margin = mark_distance_margin(mark_px, tick)
    if side == "LONG":
        cap = mark_px - margin
        if tick_f > 0:
            widened = math.floor((cap - 1e-12) / tick_f) * tick_f
        else:
            widened = cap * (1.0 - 1e-9)
    else:
        floor_px = mark_px + margin
        if tick_f > 0:
            widened = math.ceil((floor_px + 1e-12) / tick_f) * tick_f
        else:
            widened = floor_px * (1.0 + 1e-9)
    if sl_too_close_to_mark(side, widened, mark_px, tick) and tick_f > 0:
        widened = widened - tick_f if side == "LONG" else widened + tick_f
    return widened


def widen_sl_one_tick(side: str, sl_price: float, tick: str) -> float:
    tick_f = _parse_tick(tick)
    if tick_f <= 0:
        return sl_price
    return sl_price - tick_f if side == "LONG" else sl_price + tick_f


def protective_placement_retryable(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "-2021" in msg or "immediately trigger" in msg or "too close" in msg


def _ensure_sl_clear_of_mark(
    side: str, sl: float, mark_px: float, tick: str, *, original_sl: float,
) -> Tuple[float, bool]:
    """Widen until validate passes; raise ValueError if still too close."""
    widened = False
    for _ in range(8):
        if not sl_too_close_to_mark(side, sl, mark_px, tick):
            validate_sl_distance(side, sl, mark_px, tick)
            return sl, widened
        prev = sl
        sl = widen_sl_for_mark(side, sl, mark_px, tick)
        if sl == prev:
            sl = widen_sl_one_tick(side, sl, tick)
        widened = True
    raise ValueError(
        f"SL {original_sl} unfixable vs mark {mark_px} after widen (tick={tick})")


def place_sl_strict(
    *,
    place_fn: Callable[[float], Dict[str, Any]],
    side: str,
    sl_price: float,
    mark_px: float,
    tick: str,
    max_attempts: int = 2,
    mark_px_fn: Optional[Callable[[], float]] = None,
) -> Tuple[float, Dict[str, Any]]:
    """严格按信号 SL 下单；不放宽。无效或失败则抛异常（由调用方 emergency close）。"""

    def _current_mark() -> float:
        if mark_px_fn is not None:
            try:
                return float(mark_px_fn())
            except Exception as exc:
                logger.warning("mark refresh failed, using cached mark: %s", exc)
        return mark_px

    last_exc: Optional[BaseException] = None
    for attempt in range(max_attempts):
        current_mark = _current_mark()
        validate_sl_distance(side, sl_price, current_mark, tick)
        try:
            resp = place_fn(sl_price)
            return sl_price, resp
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1 and protective_placement_retryable(exc):
                logger.warning(
                    "SL strict retry %s/%s %s @ %.6f mark=%.6f: %s",
                    attempt + 1, max_attempts, side, sl_price, current_mark, exc,
                )
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("SL placement exhausted without response")


def place_sl_with_mark_retries(
    *,
    place_fn: Callable[[float], Dict[str, Any]],
    side: str,
    sl_price: float,
    mark_px: float,
    tick: str,
    max_attempts: int = 3,
) -> Tuple[float, bool, Dict[str, Any]]:
    """按 mark 放宽 SL 后下单；交易所拒单则再放宽 1 tick 重试。最终仍失败则抛异常。"""
    sl = sl_price
    widened = False
    last_exc: Optional[BaseException] = None

    for attempt in range(max_attempts):
        sl, step_widened = _ensure_sl_clear_of_mark(
            side, sl, mark_px, tick, original_sl=sl_price,
        )
        widened = widened or step_widened
        if step_widened and sl != sl_price:
            logger.warning(
                "SL widened for mark distance %s: %.6f -> %.6f mark=%.6f",
                side, sl_price if attempt == 0 else sl, sl, mark_px,
            )

        try:
            resp = place_fn(sl)
            return sl, widened, resp
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1 and protective_placement_retryable(exc):
                prev = sl
                sl = widen_sl_one_tick(side, sl, tick)
                widened = True
                logger.warning(
                    "SL place retry %s/%s %s: %.6f -> %.6f (%s)",
                    attempt + 1, max_attempts, side, prev, sl, exc,
                )
                continue
            raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("SL placement exhausted without response")


def resolve_sl_price_for_mark(
    side: str, sl_price: float, mark_px: float, tick: str,
) -> Tuple[float, bool]:
    """过近则按 mark 放宽；返回 (最终 SL, 是否放宽)。"""
    if not sl_too_close_to_mark(side, sl_price, mark_px, tick):
        return sl_price, False
    return widen_sl_for_mark(side, sl_price, mark_px, tick), True


def validate_sl_distance(
    side: str, sl_price: float, mark_px: float, tick: str,
) -> None:
    """验证 SL 距离 mark price 不能过近。"""
    margin = mark_distance_margin(mark_px, tick)
    if side == "LONG" and sl_price >= mark_px - margin:
        raise ValueError(
            f"SL {sl_price} too close to mark {mark_px} (need <= {mark_px - margin:.6f})")
    if side == "SHORT" and sl_price <= mark_px + margin:
        raise ValueError(
            f"SL {sl_price} too close to mark {mark_px} (need >= {mark_px + margin:.6f})")


def validate_tp_distance(
    side: str, tp_price: float, mark_px: float, tick: str,
) -> None:
    """验证 TP 距离 mark price 不能过近。"""
    try:
        tick_f = float(tick)
    except ValueError:
        tick_f = 0.0
    margin = max(tick_f * 2.0, mark_px * 0.0005)
    if side == "LONG" and tp_price <= mark_px + margin:
        raise ValueError(
            f"TP {tp_price} too close to mark {mark_px} (need >= {mark_px + margin:.6f})")
    if side == "SHORT" and tp_price >= mark_px - margin:
        raise ValueError(
            f"TP {tp_price} too close to mark {mark_px} (need <= {mark_px - margin:.6f})")
