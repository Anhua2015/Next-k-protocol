"""STOP（Stop-Limit）入场 — OR 突破 @ entry_price。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("trading.stop_limit_entry")


@dataclass
class StopLimitEntryResult:
    ok: bool
    submitted: bool = False
    filled_immediately: bool = False
    entry_order_id: str = ""
    entry_price: float = 0.0
    qty: float = 0.0
    position_side: Optional[str] = None
    error: str = ""


def _immediate_trigger_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "-2021" in msg or "immediately trigger" in msg


def _effective_sl_for_fill(
    signal: Dict[str, Any],
    *,
    side: str,
    actual_entry: float,
) -> Optional[float]:
    """fill 价与 STOP 触发价不一致时平移 SL（or_range 保持 OR 锚定 SL）。"""
    sl_raw = signal.get("sl_price")
    if sl_raw is None:
        return None
    sl_f = float(sl_raw)
    sig_entry = signal.get("entry_price")
    if sig_entry is None or float(sig_entry) <= 0:
        return sl_f
    sig_e = float(sig_entry)
    if abs(actual_entry - sig_e) < 1e-12:
        return sl_f
    or_h = signal.get("or_high")
    or_l = signal.get("or_low")
    if or_h is not None and or_l is not None and float(or_h) > float(or_l):
        return sl_f
    dist = signal.get("sl_risk_dist")
    if dist is not None and float(dist) > 0:
        d = float(dist)
        return actual_entry - d if side == "LONG" else actual_entry + d
    return sl_f + (actual_entry - sig_e)


def _attach_protective(
    signal: Dict[str, Any],
    *,
    symbol: str,
    side: str,
    qty: float,
    actual_entry: float,
    tick_size: str,
    mark_px: float,
    position_side: Optional[str],
    source: str,
    play: str,
    entry_type: str = "STOP_LIMIT",
) -> tuple[bool, str]:
    from binance.exchange_info import round_price as _round_price
    from db import update_signal_execution, update_signal_status
    from trader import _place_protective, cancel_all_orders, get_mark_price
    from trading.protective import place_sl_strict
    from common.exceptions import EmergencyCloseFailedError

    def _abort_unprotected(reason: str) -> tuple[bool, str]:
        try:
            cancel_all_orders(symbol)
        except Exception:
            pass
        try:
            from trader import emergency_close_position
            emergency_close_position(symbol, side, qty, position_side)
        except EmergencyCloseFailedError:
            update_signal_status(signal_log_id, "error", f"{reason}; emergency_close_failed")
            logger.critical("NAKED POSITION %s %s qty=%s — %s", side, symbol, qty, reason)
            return False, f"{reason}; emergency_close_failed"
        update_signal_status(signal_log_id, "error", reason)
        return False, reason

    signal_log_id = signal["signal_log_id"]
    sl_price = None
    tp_price = None
    try:
        sl_price = _effective_sl_for_fill(signal, side=side, actual_entry=float(actual_entry))
        tp_price = float(signal["tp_price"]) if signal.get("tp_price") is not None else None
    except (TypeError, ValueError):
        pass

    close_side = "SELL" if side == "LONG" else "BUY"
    final_sl_p = _round_price(sl_price, tick_size) if sl_price else None
    final_tp_p = _round_price(tp_price, tick_size) if tp_price else None

    if final_sl_p is None:
        return _abort_unprotected("missing_sl")

    try:
        mark_px = get_mark_price(symbol)
    except Exception as exc:
        logger.warning("mark refresh before SL %s: %s", symbol, exc)

    sl_order_id = ""
    tp_order_id = ""
    try:
        final_sl_p, sl_resp = place_sl_strict(
            place_fn=lambda sl_px: _place_protective(
                symbol, close_side, sl_px, qty, position_side, tick_size, "SL",
            ),
            side=side,
            sl_price=final_sl_p,
            mark_px=mark_px,
            tick=tick_size,
            mark_px_fn=lambda: get_mark_price(symbol),
        )
        sl_order_id = str(sl_resp.get("algoId", "") or sl_resp.get("orderId", ""))
        if final_tp_p is not None:
            tp_resp = _place_protective(symbol, close_side, final_tp_p, qty, position_side, tick_size, "TP")
            tp_order_id = str(tp_resp.get("algoId", "") or tp_resp.get("orderId", ""))
    except ValueError as exc:
        logger.error("SL invalid %s %s — emergency close: %s", side, symbol, exc)
        return _abort_unprotected(f"sl_validation: {exc}")
    except Exception as exc:
        logger.error("SL/TP placement failed %s %s: %s", side, symbol, exc)
        return _abort_unprotected(f"SL/TP failed: {exc}")

    margin = float(signal.get("margin_usdt") or 0)
    leverage = int(float(signal.get("leverage") or 0))
    update_signal_execution(
        signal_log_id,
        status="traded",
        result={
            "ok": True,
            "entry_order_id": signal.get("_entry_order_id") or "",
            "quantity": qty,
            "entry_price": actual_entry,
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
            "notional_usdt": margin * leverage,
            "entry_type": entry_type,
            "entry_is_algo": entry_type == "STOP_LIMIT",
        },
    )
    from observability.metrics import TRADES_OPENED

    TRADES_OPENED.labels(source=source, side=side, entry_type=entry_type).inc()
    sl_log = f"{final_sl_p:.6f}" if final_sl_p is not None else "-"
    tp_log = f"{final_tp_p:.6f}" if final_tp_p is not None else "-"
    logger.info(
        "Opened %s %s source=%s qty=%s entry=%.6f sl=%s tp=%s (STOP_LIMIT)",
        side, symbol, source, qty, actual_entry, sl_log, tp_log,
    )
    return True, ""


def open_stop_limit(
    signal: Dict[str, Any],
    symbol: str,
    side: str,
    margin: float,
    leverage: int,
    step_size: str,
    tick_size: str,
    min_notional: float,
    hedge: bool,
    mark_px: float,
    source: str,
    play: str,
) -> StopLimitEntryResult:
    """挂 OR Stop-Limit；已穿越 entry 时 fallback MARKET（gap 穿越）。"""
    from binance.exchange_info import round_price as _round_price, round_quantity as _round_quantity
    from db import update_signal_execution, update_signal_status
    from binance.orders import build_entry_stop_limit_algo_params, normalize_algo_entry_order
    from trader import get_algo_order, place_algo_order

    signal_log_id = signal["signal_log_id"]
    order_side = "BUY" if side == "LONG" else "SELL"
    position_side = side if hedge else None

    signal_entry = signal.get("entry_price")
    if signal_entry is None or float(signal_entry) <= 0:
        update_signal_status(signal_log_id, "error", "stop_limit needs entry_price")
        return StopLimitEntryResult(ok=False, error="missing entry_price")

    stop_px = _round_price(float(signal_entry), tick_size)
    limit_raw = signal.get("limit_price")
    if limit_raw is not None and float(limit_raw) > 0:
        limit_px = _round_price(float(limit_raw), tick_size)
    else:
        limit_px = stop_px

    allow_gap_raw = signal.get("allow_gap_market")
    allow_gap = True if allow_gap_raw is None else bool(allow_gap_raw)

    # preplace：现价未穿越时应正常挂 STOP；已穿越且禁止 gap 则拒单
    gap_crossed = (side == "LONG" and mark_px >= stop_px) or (side == "SHORT" and mark_px <= stop_px)
    if gap_crossed and not allow_gap:
        msg = f"gap_exceeds_limit mark={mark_px:.6f} stop={stop_px:.6f} limit={limit_px:.6f}"
        logger.info("stop_limit reject gap %s %s: %s", side, symbol, msg)
        update_signal_status(signal_log_id, "error", msg)
        return StopLimitEntryResult(ok=False, error=msg)
    if gap_crossed and allow_gap:
        logger.info(
            "stop_limit gap fallback MARKET %s %s mark=%.6f entry=%.6f",
            side, symbol, mark_px, stop_px,
        )
        from trading.market_entry import open_market

        m = open_market(
            signal, symbol, side, margin, leverage,
            step_size, tick_size, min_notional, hedge, mark_px, source, play,
        )
        if not m.ok:
            return StopLimitEntryResult(ok=False, error=m.error or "market_gap_fallback_failed")
        return StopLimitEntryResult(
            ok=True,
            filled_immediately=True,
            entry_order_id=m.entry_order_id,
            entry_price=m.entry_price,
            qty=m.qty,
            position_side=m.position_side,
        )

    try:
        raw_qty = margin * leverage / max(stop_px, 1e-12)
        qty = _round_quantity(raw_qty, step_size)
        if qty <= 0:
            raise ValueError(f"computed qty={qty}")
        if qty * stop_px < min_notional:
            raise ValueError(f"notional {qty * stop_px:.2f} < min {min_notional}")

        entry_params = build_entry_stop_limit_algo_params(
            symbol=symbol,
            side=order_side,
            qty=qty,
            trigger_price=stop_px,
            limit_price=limit_px,
            position_side=position_side,
        )
        entry_resp = place_algo_order(entry_params)
        entry_order_id = str(entry_resp.get("algoId") or entry_resp.get("orderId") or "")
        normalized = normalize_algo_entry_order(entry_resp)
        status = str(normalized.get("status") or "").upper()
        actual_entry = float(normalized.get("avgPrice") or 0)
        exec_qty = float(normalized.get("executedQty") or 0)

        if status == "FILLED" or (exec_qty > 0 and actual_entry > 0):
            if (actual_entry <= 0 or exec_qty <= 0) and entry_order_id:
                detail = normalize_algo_entry_order(get_algo_order(entry_order_id))
                actual_entry = float(detail.get("avgPrice") or stop_px)
                exec_qty = float(detail.get("executedQty") or qty)
            signal["_entry_order_id"] = entry_order_id
            ok, err = _attach_protective(
                signal,
                symbol=symbol,
                side=side,
                qty=exec_qty or qty,
                actual_entry=actual_entry or stop_px,
                tick_size=tick_size,
                mark_px=mark_px,
                position_side=position_side,
                source=source,
                play=play,
            )
            if not ok:
                return StopLimitEntryResult(ok=False, error=err)
            return StopLimitEntryResult(
                ok=True,
                filled_immediately=True,
                entry_order_id=entry_order_id,
                entry_price=actual_entry or stop_px,
                qty=exec_qty or qty,
                position_side=position_side,
            )

        update_signal_execution(
            signal_log_id,
            status="submitted",
            result={
                "ok": True,
                "entry_order_id": entry_order_id,
                "quantity": qty,
                "entry_price": stop_px,
                "limit_price": limit_px,
                "entry_type": "STOP_LIMIT",
                "entry_is_algo": True,
                "stop_price": stop_px,
                "notional_usdt": margin * leverage,
                "margin_usdt": margin,
                "leverage": leverage,
                "side": side,
                "sl_price": signal.get("sl_price"),
                "tp_price": signal.get("tp_price"),
                "or_high": signal.get("or_high"),
                "or_low": signal.get("or_low"),
                "sl_risk_dist": signal.get("sl_risk_dist"),
                "oco_peer_api_id": signal.get("oco_peer_api_id") or "",
            },
        )
        logger.info(
            "STOP_LIMIT submitted: %s %s qty=%s stop=%.6f order=%s",
            side, symbol, qty, stop_px, entry_order_id,
        )
        return StopLimitEntryResult(
            ok=True,
            submitted=True,
            entry_order_id=entry_order_id,
            entry_price=stop_px,
            qty=qty,
            position_side=position_side,
        )
    except Exception as exc:
        if _immediate_trigger_error(exc):
            allow_gap_raw = signal.get("allow_gap_market")
            allow_gap = True if allow_gap_raw is None else bool(allow_gap_raw)
            if not allow_gap:
                logger.info("STOP immediate trigger rejected %s %s: %s", side, symbol, exc)
                update_signal_status(signal_log_id, "error", f"immediate_trigger: {exc}")
                return StopLimitEntryResult(ok=False, error=str(exc))
            logger.info("STOP immediate trigger → MARKET gap fallback %s %s: %s", side, symbol, exc)
            from trading.market_entry import open_market

            m = open_market(
                signal, symbol, side, margin, leverage,
                step_size, tick_size, min_notional, hedge, mark_px, source, play,
            )
            if m.ok:
                return StopLimitEntryResult(
                    ok=True,
                    filled_immediately=True,
                    entry_order_id=m.entry_order_id,
                    entry_price=m.entry_price,
                    qty=m.qty,
                    position_side=m.position_side,
                )
        logger.error("STOP_LIMIT entry %s %s failed: %s", side, symbol, exc)
        update_signal_status(signal_log_id, "error", f"STOP_LIMIT entry: {exc}")
        return StopLimitEntryResult(ok=False, error=str(exc))


def finalize_filled_entry(
    signal_row: Dict[str, Any],
    *,
    order: Dict[str, Any],
    tick_size: str,
    mark_px: float,
    source: str,
) -> bool:
    """pending STOP 成交后补 SL/TP。"""
    import json

    result = json.loads(signal_row.get("result_json") or "{}")
    signal_log_id = int(signal_row["id"])
    symbol = str(signal_row["symbol"])
    side = str(result.get("side") or signal_row.get("side") or "")
    qty = float(order.get("executedQty") or result.get("quantity") or 0)
    actual_entry = float(order.get("avgPrice") or result.get("entry_price") or 0)
    if qty <= 0 or actual_entry <= 0:
        return False

    from trader import _detect_hedge_mode

    hedge = _detect_hedge_mode()
    position_side = side if hedge else None
    signal = {
        "signal_log_id": signal_log_id,
        "entry_price": result.get("entry_price") or signal_row.get("entry_price"),
        "sl_price": result.get("sl_price") or signal_row.get("sl_price"),
        "tp_price": result.get("tp_price") or signal_row.get("tp_price"),
        "or_high": result.get("or_high"),
        "or_low": result.get("or_low"),
        "sl_risk_dist": result.get("sl_risk_dist"),
        "margin_usdt": float(result.get("margin_usdt") or 0),
        "leverage": int(float(result.get("leverage") or 1)),
        "_entry_order_id": str(order.get("orderId") or result.get("entry_order_id") or ""),
    }
    ok, _ = _attach_protective(
        signal,
        symbol=symbol,
        side=side,
        qty=qty,
        actual_entry=actual_entry,
        tick_size=tick_size,
        mark_px=mark_px,
        position_side=position_side,
        source=source,
        play=str(signal_row.get("play") or ""),
        entry_type=str(result.get("entry_type") or "STOP_LIMIT").upper(),
    )
    return ok
