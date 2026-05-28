"""同步持仓：检测 SL/TP 触发 + 链上平仓。"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("lifecycle.sync")


def sync_open_positions() -> None:
    from db import get_config, get_open_positions
    from lifecycle.close import _record_closed_position
    from trader import (
        _handle_auth_fail,
        _reset_auth_fail_count,
        cancel_all_orders,
        get_algo_order,
        get_live_position,
        get_mark_price,
        get_order,
    )

    if not get_config("binance_api_key", ""):
        return
    if get_config("enabled", "false").lower() != "true":
        return

    for pos in get_open_positions():
        try:
            if get_live_position(pos["symbol"]) is not None:
                _reset_auth_fail_count()
                continue

            close_reason = "unknown"
            close_price: Optional[float] = None
            saw_pending_algo = False

            for order_id, reason in [
                (pos["tp_order_id"], "tp"), (pos["sl_order_id"], "sl"),
            ]:
                if not order_id:
                    continue
                algo_seen = False
                try:
                    a = get_algo_order(order_id)
                    algo_seen = True
                    algo_status = (a.get("algoStatus") or "").upper()
                    actual_px = a.get("actualPrice")
                    actual_oid = a.get("actualOrderId")
                    triggered = bool(
                        (actual_px and float(actual_px) > 0)
                        or (actual_oid and str(actual_oid) not in ("", "0"))
                        or algo_status in ("TRIGGERED", "FILLED", "EXECUTED")
                    )
                    if triggered:
                        close_reason = reason
                        if actual_px and float(actual_px) > 0:
                            close_price = float(actual_px)
                        else:
                            trig_px = a.get("triggerPrice")
                            if trig_px:
                                close_price = float(trig_px)
                        break
                    if algo_status in ("WORKING", "NEW", "PENDING"):
                        saw_pending_algo = True
                        continue
                except Exception as exc:
                    logger.debug("get_algo_order(%s) failed: %s", order_id, exc)
                if algo_seen:
                    continue
                try:
                    o = get_order(pos["symbol"], order_id)
                    if o.get("status") == "FILLED":
                        close_reason = reason
                        avg = o.get("avgPrice")
                        if avg and float(avg) > 0:
                            close_price = float(avg)
                        elif o.get("stopPrice"):
                            close_price = float(o["stopPrice"])
                        break
                    if o.get("status") in ("NEW", "PARTIALLY_FILLED"):
                        saw_pending_algo = True
                except Exception as exc:
                    logger.debug("get_order(%s,%s) failed: %s", pos["symbol"], order_id, exc)

            if close_reason == "unknown" and saw_pending_algo:
                close_reason = "manual"
            if close_reason == "unknown" and not pos.get("tp_order_id"):
                if pos.get("sl_order_id"):
                    try:
                        sl_info = get_algo_order(pos["sl_order_id"])
                        s = (sl_info.get("algoStatus") or "").upper()
                        if s in ("CANCELLED", "EXPIRED", "CANCELED"):
                            close_reason = "paper_close"
                    except Exception:
                        close_reason = "paper_close"
                else:
                    close_reason = "paper_close"
            if close_reason == "unknown":
                all_cancelled = True
                for oid in (pos.get("tp_order_id"), pos.get("sl_order_id")):
                    if not oid:
                        all_cancelled = False
                        break
                    try:
                        info = get_algo_order(oid)
                        if (info.get("algoStatus") or "").upper() not in (
                            "CANCELLED", "EXPIRED", "CANCELED"):
                            all_cancelled = False
                            break
                    except Exception:
                        pass
                close_reason = "paper_close" if all_cancelled else "external"

            if close_price is None:
                try:
                    close_price = get_mark_price(pos["symbol"])
                except Exception:
                    close_price = None

            try:
                cancel_all_orders(pos["symbol"], pos)
            except Exception as exc:
                logger.warning("sync cancel_all_orders %s: %s", pos["symbol"], exc)

            _record_closed_position(pos, close_reason, close_price)
            _reset_auth_fail_count()

        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in (401, 403):
                _handle_auth_fail("sync", pos["id"])
            else:
                logger.warning("sync pos=%s: %s", pos["id"], exc)
        except Exception as exc:
            logger.warning("sync pos=%s: %s", pos["id"], exc)
