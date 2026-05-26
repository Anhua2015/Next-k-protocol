"""Binance Futures 执行层 — HMAC 签名 REST 交易。

核心行为：
- SL/TP 通过 /fapi/v1/algoOrder 下条件单（2025-12-09 迁移后的新接口）。
- HEDGE 模式感知：dual-side position 时使用 positionSide。
- SL 距离预检：距 mark price 过近时拒绝下单。
- SL/TP 下单失败 → 紧急 MARKET 平仓，避免裸仓。
- exchangeInfo 缓存 5min；server time 每 10min 同步，-1021 时自动重试。
- 429 / -1003 / 5xx 可重试，指数退避最大 3 次。
- pnl_pct = 杠杆收益率 × 100%。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from db import (
    cancel_pending_position,
    compute_expire_at,
    compute_pending_deadline,
    get_config,
    get_open_expired_positions,
    get_open_position_for_symbol,
    get_open_positions,
    get_pending_entries,
    get_source_config,
    insert_pending_position,
    insert_position,
    promote_pending_to_open,
    resolve_expire_hours,
    set_config,
    update_position_closed,
    update_signal_status,
)

logger = logging.getLogger("trader")

LIVE_BASE = "https://fapi.binance.com"
TEST_BASE = "https://testnet.binancefuture.com"

RECV_WINDOW_MS = 5000
EXCHANGE_INFO_TTL_SEC = 300
SERVER_TIME_RESYNC_SEC = 600

RETRY_STATUSES = {429, 418, 500, 502, 503, 504}
RETRY_CODES = {-1003, -1004}
MAX_RETRIES = 3
BACKOFF_BASE_SEC = 0.5

_ts_offset_ms: int = 0
_ts_offset_lock = threading.Lock()
_last_sync_ts: float = 0.0

_exch_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_exch_cache_lock = threading.Lock()

_hedge_mode_cache: Optional[bool] = None
_hedge_mode_lock = threading.Lock()

_SYNC_AUTH_FAIL_COUNT = 0
_SYNC_AUTH_FAIL_THRESHOLD = 20


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _base_url() -> str:
    return TEST_BASE if get_config("testnet", "false").lower() == "true" else LIVE_BASE


def _sign(params: Dict[str, Any], secret: str) -> str:
    qs = urllib.parse.urlencode(params)
    return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()


def _headers() -> Dict[str, str]:
    return {"X-MBX-APIKEY": get_config("binance_api_key", "")}


def _local_ms() -> int:
    return int(time.time() * 1000)


def _sync_server_time() -> None:
    global _ts_offset_ms, _last_sync_ts
    try:
        url = _base_url() + "/fapi/v1/time"
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            srv = int(resp.json()["serverTime"])
        with _ts_offset_lock:
            _ts_offset_ms = srv - _local_ms()
            _last_sync_ts = time.time()
        logger.debug("server time offset = %d ms", _ts_offset_ms)
    except Exception as exc:
        logger.warning("server time sync failed: %s", exc)


def _ts() -> int:
    if time.time() - _last_sync_ts > SERVER_TIME_RESYNC_SEC:
        _sync_server_time()
    return _local_ms() + _ts_offset_ms


def _request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    signed: bool = True,
) -> Any:
    params = dict(params or {})
    if signed:
        params["timestamp"] = _ts()
        params["recvWindow"] = RECV_WINDOW_MS
        params["signature"] = _sign(params, get_config("binance_api_secret", ""))
    url = _base_url() + path

    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=10.0) as client:
                if method == "GET":
                    resp = client.get(url, params=params, headers=_headers())
                elif method == "POST":
                    resp = client.post(url, params=params, headers=_headers())
                elif method == "DELETE":
                    resp = client.delete(url, params=params, headers=_headers())
                else:
                    raise ValueError(f"Unsupported method: {method}")

            if resp.status_code in RETRY_STATUSES:
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code} retryable", request=resp.request, response=resp,
                )
                if attempt < MAX_RETRIES:
                    delay = BACKOFF_BASE_SEC * (2 ** attempt)
                    logger.warning("retry %d/%d after %.2fs", attempt + 1, MAX_RETRIES, delay)
                    time.sleep(delay)
                    continue
                raise last_exc

            if resp.status_code >= 400:
                logger.error("Binance %s %s -> %s body=%s", method, path, resp.status_code, resp.text)
                try:
                    body = resp.json()
                except Exception:
                    body = None
                if isinstance(body, dict) and body.get("code") in RETRY_CODES:
                    last_exc = httpx.HTTPStatusError(
                        f"{resp.status_code} code={body.get('code')}", request=resp.request, response=resp,
                    )
                    if attempt < MAX_RETRIES:
                        delay = BACKOFF_BASE_SEC * (2 ** attempt)
                        logger.warning("retry %d/%d after %.2fs", attempt + 1, MAX_RETRIES, delay)
                        time.sleep(delay)
                        continue
                    raise last_exc
                if isinstance(body, dict) and body.get("code") == -1021 and attempt == 0:
                    _sync_server_time()
                    inner = {k: v for k, v in params.items() if k != "signature"}
                    inner["timestamp"] = _ts()
                    inner["signature"] = _sign(inner, get_config("binance_api_secret", ""))
                    params = inner
                    last_exc = httpx.HTTPStatusError("-1021 skew, retry", request=resp.request, response=resp)
                    continue
                resp.raise_for_status()

            return resp.json()
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                delay = BACKOFF_BASE_SEC * (2 ** attempt)
                logger.warning("retry %d/%d after %.2fs: %s", attempt + 1, MAX_RETRIES, delay, exc)
                time.sleep(delay)
                continue
            raise
    if last_exc:
        raise last_exc


def get_mark_price(symbol: str) -> float:
    data = _request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, signed=False)
    return float(data["markPrice"])


def _get_exchange_info() -> Dict[str, Any]:
    base = _base_url()
    with _exch_cache_lock:
        entry = _exch_cache.get(base)
        if entry and (time.time() - entry[0]) < EXCHANGE_INFO_TTL_SEC:
            return entry[1]
    data = _request("GET", "/fapi/v1/exchangeInfo", signed=False)
    with _exch_cache_lock:
        _exch_cache[base] = (time.time(), data)
    return data


def get_symbol_info(symbol: str) -> Dict[str, Any]:
    data = _get_exchange_info()
    for s in data.get("symbols", []):
        if s["symbol"] == symbol:
            return s
    raise ValueError(f"Symbol {symbol} not found in exchangeInfo")


def _round_quantity(qty: float, step_size: str) -> float:
    precision = len(step_size.rstrip("0").split(".")[-1]) if "." in step_size else 0
    return round(qty, precision)


def _round_price(price: float, tick_size: str) -> float:
    precision = len(tick_size.rstrip("0").split(".")[-1]) if "." in tick_size else 0
    return round(price, precision)


def _get_filters(symbol: str) -> Tuple[str, str, float]:
    info = get_symbol_info(symbol)
    step_size: Optional[str] = None
    tick_size: Optional[str] = None
    min_notional: Optional[float] = None
    for f in info.get("filters", []):
        if f["filterType"] == "LOT_SIZE":
            step_size = f["stepSize"]
        elif f["filterType"] == "PRICE_FILTER":
            tick_size = f["tickSize"]
        elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
            v = f.get("notional") or f.get("minNotional")
            if v:
                try:
                    min_notional = float(v)
                except ValueError:
                    pass
    if step_size is None or tick_size is None:
        raise ValueError(f"exchangeInfo for {symbol} missing LOT_SIZE/PRICE_FILTER")
    if min_notional is None:
        min_notional = 5.0
    return step_size, tick_size, min_notional


def _detect_hedge_mode() -> bool:
    global _hedge_mode_cache
    with _hedge_mode_lock:
        if _hedge_mode_cache is not None:
            return _hedge_mode_cache
    try:
        data = _request("GET", "/fapi/v1/positionSide/dual")
        with _hedge_mode_lock:
            _hedge_mode_cache = bool(data.get("dualSidePosition"))
            return _hedge_mode_cache
    except Exception as exc:
        logger.warning("hedge-mode detect failed (assume one-way): %s", exc)
        with _hedge_mode_lock:
            _hedge_mode_cache = False
            return False


def set_leverage(symbol: str, leverage: int) -> None:
    try:
        _request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
    except httpx.HTTPStatusError as e:
        try:
            if e.response.json().get("code") == -4028:
                return
        except Exception:
            pass
        raise


def set_margin_type(symbol: str) -> None:
    try:
        _request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
    except httpx.HTTPStatusError as e:
        if "No need to change margin type" in (e.response.text or ""):
            return
        raise


def cancel_all_orders(symbol: str, pos: Optional[Dict[str, Any]] = None) -> bool:
    """取消 symbol 的所有挂单（普通单 + algo 条件单）。

    优先用 pos 里记录的 sl_order_id / tp_order_id 精确取消 SL/TP，
    再用 openAlgoOrders 列表查询兜底（覆盖 DB 漏记 / 手动加挂的边角情况）。

    返回 algo 段是否完全成功；普通单失败仅记 warning。
    """
    try:
        _request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    except Exception as exc:
        logger.warning("cancel_all_orders (regular) %s: %s", symbol, exc)

    algo_ok = True
    handled: set = set()

    if pos:
        for key in ("sl_order_id", "tp_order_id"):
            aid = pos.get(key)
            if not aid:
                continue
            aid = str(aid)
            if aid in handled:
                continue
            handled.add(aid)
            ok = cancel_algo_order(aid)
            logger.info("cancel_all_orders %s %s=%s ok=%s", symbol, key, aid, ok)
            if not ok:
                algo_ok = False

    try:
        for o in get_open_algo_orders(symbol):
            aid = o.get("algoId") or o.get("clientAlgoId")
            if not aid:
                continue
            aid = str(aid)
            if aid in handled:
                continue
            handled.add(aid)
            ok = cancel_algo_order(aid)
            logger.info("cancel_all_orders %s fallback algoId=%s ok=%s", symbol, aid, ok)
            if not ok:
                algo_ok = False
    except Exception as exc:
        logger.error("cancel_all_orders (algo list) %s: %s", symbol, exc)
        algo_ok = False

    return algo_ok


def place_algo_order(params: Dict[str, Any]) -> Dict[str, Any]:
    return _request("POST", "/fapi/v1/algoOrder", params)


def get_algo_order(algo_id: str) -> Dict[str, Any]:
    return _request("GET", "/fapi/v1/algoOrder", {"algoId": algo_id})


def cancel_algo_order(algo_id: str) -> bool:
    try:
        _request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
        return True
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if 400 <= code < 500:
            logger.info("cancel_algo_order %s: already gone (%s)", algo_id, code)
            return True
        logger.error("cancel_algo_order %s: http %s", algo_id, code)
        return False
    except Exception as exc:
        logger.error("cancel_algo_order %s: %s", algo_id, exc)
        return False


def cancel_order_by_id(symbol: str, order_id: str) -> bool:
    """按 orderId 取消单个普通订单（LIMIT / STOP / 等）。

    4xx 视作 already gone（已成交 / 已取消 / 不存在）返回 True；5xx / 网络错误返回 False。
    """
    try:
        _request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
        return True
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if 400 <= code < 500:
            logger.info("cancel_order_by_id %s %s: already gone (%s)", symbol, order_id, code)
            return True
        logger.error("cancel_order_by_id %s %s: http %s", symbol, order_id, code)
        return False
    except Exception as exc:
        logger.error("cancel_order_by_id %s %s: %s", symbol, order_id, exc)
        return False


def get_open_algo_orders(symbol: str) -> List[Any]:
    try:
        data = _request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})
        if isinstance(data, list):
            return data
        return data.get("orders", []) if isinstance(data, dict) else []
    except Exception as exc:
        logger.warning("get_open_algo_orders %s: %s", symbol, exc)
        return []


def place_order(params: Dict[str, Any]) -> Dict[str, Any]:
    return _request("POST", "/fapi/v1/order", params)


def get_live_position(symbol: str) -> Optional[Dict[str, Any]]:
    rows = _request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    for r in rows:
        if r["symbol"] == symbol and float(r["positionAmt"]) != 0:
            return r
    return None


def get_order(symbol: str, order_id: str) -> Dict[str, Any]:
    return _request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})


def _emergency_close(
    symbol: str, side: str, qty: float, position_side: Optional[str]
) -> Optional[str]:
    close_side = "SELL" if side == "LONG" else "BUY"
    params: Dict[str, Any] = {
        "symbol": symbol,
        "side": close_side,
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true",
    }
    if position_side:
        params["positionSide"] = position_side
        params.pop("reduceOnly", None)
    try:
        resp = place_order(params)
        oid = str(resp.get("orderId", ""))
        logger.error("EMERGENCY close %s %s qty=%s order=%s", side, symbol, qty, oid)
        return oid
    except Exception as exc:
        logger.critical(
            "EMERGENCY close FAILED %s %s qty=%s — POSITION IS NAKED: %s",
            side, symbol, qty, exc,
        )
        return None


def _build_protective(
    symbol: str, close_side: str, stop_price: float, qty: float,
    position_side: Optional[str], kind: str,
) -> Dict[str, Any]:
    order_type = "STOP_MARKET" if kind == "SL" else "TAKE_PROFIT_MARKET"
    params: Dict[str, Any] = {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": close_side,
        "type": order_type,
        "triggerPrice": stop_price,
        "workingType": "MARK_PRICE",
        "quantity": qty,
        "priceProtect": "false",
    }
    if position_side:
        params["positionSide"] = position_side
    else:
        params["reduceOnly"] = "true"
    return params


def _place_protective(
    symbol: str, close_side: str, stop_price: float, qty: float,
    position_side: Optional[str], tick_size: str, kind: str,
) -> Dict[str, Any]:
    params = _build_protective(symbol, close_side, stop_price, qty, position_side, kind)
    return place_algo_order(params)


def _validate_sl_distance(side: str, sl_price: float, mark_px: float, tick: str) -> None:
    try:
        tick_f = float(tick)
    except ValueError:
        tick_f = 0.0
    margin = max(tick_f * 2.0, mark_px * 0.0005)
    if side == "LONG" and sl_price >= mark_px - margin:
        raise ValueError(f"SL {sl_price} too close to mark {mark_px} (need <= {mark_px - margin:.6f})")
    if side == "SHORT" and sl_price <= mark_px + margin:
        raise ValueError(f"SL {sl_price} too close to mark {mark_px} (need >= {mark_px + margin:.6f})")


def execute_trade(signal: Dict[str, Any]) -> bool:
    signal_log_id = signal["signal_log_id"]
    symbol = signal["symbol"]
    side = signal["side"]
    source = signal.get("source", "") or ""
    play = signal.get("play", "") or ""

    logger.info(
        "execute_trade: source=%s symbol=%s side=%s play=%s signal_log_id=%s",
        source, symbol, side, play, signal_log_id,
    )

    # 读策略保证金/杠杆，动量/接针用各自的，ZCT 用全局
    if source in ("momentum", "jiezhen"):
        margin = float(get_source_config(source, "margin_usdt", "100"))
        leverage = int(get_source_config(source, "leverage", "10"))
    else:
        try:
            margin = float(get_config("margin_usdt", "100"))
            leverage = int(get_config("leverage", "10"))
        except (TypeError, ValueError) as exc:
            logger.error("config parse failed %s: %s", symbol, exc)
            update_signal_status(signal_log_id, "error", f"bad config: {exc}")
            return False

    logger.info(
        "execute_trade %s: source=%s margin=%.0f leverage=%d",
        symbol, source, margin, leverage,
    )

    # SL/TP 统一从信号读取，不由 Protocol 计算
    try:
        sl_price = float(signal["sl_price"]) if signal.get("sl_price") is not None else None
        tp_price = float(signal["tp_price"]) if signal.get("tp_price") is not None else None
    except (TypeError, ValueError) as exc:
        logger.error("signal SL/TP parse failed %s: %s", symbol, exc)
        update_signal_status(signal_log_id, "error", f"bad signal SL/TP: {exc}")
        return False

    if margin <= 0 or leverage <= 0:
        update_signal_status(signal_log_id, "error", f"invalid margin={margin} leverage={leverage}")
        return False

    if get_config("enabled", "false").lower() != "true":
        update_signal_status(signal_log_id, "skipped_disabled", "trading disabled")
        return False

    entry_type = get_source_config(
        source, "entry_type",
        get_config("entry_type", "MARKET"),
    ).upper()

    qty: float = 0.0
    actual_entry: float = 0.0
    position_side: Optional[str] = None
    entry_order_id = ""

    try:
        step_size, tick_size, min_notional = _get_filters(symbol)
        set_margin_type(symbol)
        set_leverage(symbol, leverage)

        hedge = _detect_hedge_mode()
        if hedge:
            position_side = "LONG" if side == "LONG" else "SHORT"

        mark_px = get_mark_price(symbol)

        order_side = "BUY" if side == "LONG" else "SELL"
        close_side = "SELL" if side == "LONG" else "BUY"

        if entry_type == "LIMIT":
            signal_entry = signal.get("entry_price")
            if signal_entry is None or float(signal_entry) <= 0:
                logger.error("LIMIT entry %s %s: signal missing entry_price", side, symbol)
                update_signal_status(signal_log_id, "error", "limit needs entry_price")
                return False
            limit_price_raw = float(signal_entry)
            limit_price = _round_price(limit_price_raw, tick_size)
            raw_qty = margin * leverage / limit_price
            qty = _round_quantity(raw_qty, step_size)
            if qty <= 0:
                raise ValueError(f"computed qty={qty} (margin={margin}, limit={limit_price})")
            if qty * limit_price < min_notional:
                raise ValueError(f"notional {qty * limit_price:.2f} < exchange min {min_notional}")

            entry_params: Dict[str, Any] = {
                "symbol": symbol,
                "side": order_side,
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": qty,
                "price": limit_price,
                "newOrderRespType": "ACK",
            }
            if position_side:
                entry_params["positionSide"] = position_side
            entry_resp = place_order(entry_params)
            entry_order_id = str(entry_resp.get("orderId", ""))
            if not entry_order_id:
                raise ValueError(f"LIMIT response missing orderId: {entry_resp}")

            timeout_sec = float(get_source_config(
                source, "limit_entry_timeout_sec",
                get_config("limit_entry_timeout_sec", "30"),
            ))
            deadline = compute_pending_deadline(timeout_sec)

            insert_pending_position(
                signal_log_id=signal_log_id,
                symbol=symbol,
                side=side,
                entry_order_id=entry_order_id,
                entry_price=limit_price,
                sl_price=sl_price,
                tp_price=tp_price,
                quantity=qty,
                notional_usdt=margin,
                leverage=leverage,
                opened_at=_now_utc(),
                entry_deadline=deadline,
                play=play,
                source=source,
            )
            update_signal_status(signal_log_id, "pending_entry")
            logger.info(
                "LIMIT placed: %s %s qty=%s price=%.6f order=%s deadline=%s",
                side, symbol, qty, limit_price, entry_order_id, deadline,
            )
            return True

        # MARKET 分支（原行为）
        raw_qty = margin * leverage / mark_px
        qty = _round_quantity(raw_qty, step_size)
        if qty <= 0:
            raise ValueError(f"computed qty={qty} (margin={margin}, mark={mark_px})")
        if qty * mark_px < min_notional:
            raise ValueError(f"notional {qty * mark_px:.2f} < exchange min {min_notional}")

        entry_params = {
            "symbol": symbol,
            "side": order_side,
            "type": "MARKET",
            "quantity": qty,
            "newOrderRespType": "RESULT",
        }
        if position_side:
            entry_params["positionSide"] = position_side
        entry_resp = place_order(entry_params)
        entry_order_id = str(entry_resp.get("orderId", ""))
        actual_entry = float(entry_resp.get("avgPrice") or 0)
        if actual_entry <= 0 and entry_order_id:
            try:
                detail = get_order(symbol, entry_order_id)
                actual_entry = float(detail.get("avgPrice") or 0)
            except Exception as exc:
                logger.warning("get_order after entry %s: %s", symbol, exc)
        if actual_entry <= 0:
            actual_entry = mark_px
            logger.warning("entry avgPrice missing for %s order=%s; using mark=%.6f", symbol, entry_order_id, mark_px)

        logger.info(
            "entry filled: %s %s qty=%s entry=%.6f order=%s",
            side, symbol, qty, actual_entry, entry_order_id,
        )
    except Exception as exc:
        logger.error("entry %s %s failed: %s", side, symbol, exc)
        update_signal_status(signal_log_id, "error", f"entry: {exc}")
        return False

    # 统一使用信号 SL/TP（所有策略均由 next-k-api 计算）
    final_sl_p: Optional[float] = None
    final_tp_p: Optional[float] = None

    if sl_price is not None:
        final_sl_p = _round_price(sl_price, tick_size)
    if tp_price is not None:
        final_tp_p = _round_price(tp_price, tick_size)

    logger.info(
        "signal SL/TP: %s %s source=%s entry=%.6f sl=%s tp=%s",
        side, symbol, source, actual_entry, final_sl_p, final_tp_p,
    )

    # 验证 SL 距离
    if final_sl_p is not None:
        try:
            _validate_sl_distance(side, final_sl_p, mark_px, tick_size)
        except ValueError as exc:
            logger.warning("SL validation failed %s: %s", symbol, exc)

    sl_order_id = ""
    tp_order_id = ""
    try:
        if final_sl_p is not None:
            sl_resp = _place_protective(symbol, close_side, final_sl_p, qty, position_side, tick_size, "SL")
            sl_order_id = str(sl_resp.get("algoId", "") or sl_resp.get("orderId", ""))
            logger.info("SL placed: %s %s sl=%.6f algoId=%s", side, symbol, final_sl_p, sl_order_id)
        else:
            logger.info("SL skipped: %s %s (no SL price)", side, symbol)

        if final_tp_p is not None:
            tp_resp = _place_protective(symbol, close_side, final_tp_p, qty, position_side, tick_size, "TP")
            tp_order_id = str(tp_resp.get("algoId", "") or tp_resp.get("orderId", ""))
            logger.info("TP placed: %s %s tp=%.6f algoId=%s", side, symbol, final_tp_p, tp_order_id)
        else:
            logger.info("TP skipped: %s %s (no TP price)", side, symbol)
    except Exception as exc:
        logger.error("SL/TP placement failed %s %s: %s", side, symbol, exc)
        try:
            cancel_all_orders(symbol)
        except Exception:
            pass
        _emergency_close(symbol, side, qty, position_side)
        update_signal_status(signal_log_id, "error", f"SL/TP failed, position closed: {exc}")
        return False

    insert_position(
        signal_log_id=signal_log_id,
        symbol=symbol,
        side=side,
        entry_order_id=entry_order_id,
        sl_order_id=sl_order_id,
        tp_order_id=tp_order_id,
        entry_price=actual_entry,
        sl_price=final_sl_p,
        tp_price=final_tp_p,
        quantity=qty,
        notional_usdt=margin,
        leverage=leverage,
        opened_at=_now_utc(),
        play=play,
        source=source,
    )
    update_signal_status(signal_log_id, "traded")
    logger.info(
        "Opened %s %s source=%s qty=%s entry=%.6f sl=%.6f tp=%.6f margin=%.0f lev=%d",
        side, symbol, source, qty, actual_entry, final_sl_p, final_tp_p, margin, leverage,
    )
    return True


def close_position(
    source: str,
    symbol: str,
    side: str,
    exit_rule: str,
    close_price: Optional[float] = None,
) -> bool:
    """平仓：取消 SL/TP 条件单 + MARKET 平仓 + 记录 PnL。

    由 next-k-api trail 退出后通过 POST /api/binance/positions/close 调用。
    """
    logger.info(
        "close_position: source=%s symbol=%s side=%s exit_rule=%s close_price=%s",
        source, symbol, side, exit_rule, close_price,
    )

    pos = get_open_position_for_symbol(symbol)
    if pos is None:
        logger.warning("close_position %s %s: no open position found", side, symbol)
        return False

    # pending_entry：限价单还没成交，撤掉单子标记 cancelled_pending，不走 MARKET（没仓位可平）
    if pos.get("status") == "pending_entry":
        entry_oid = pos.get("entry_order_id")
        logger.info(
            "close_position %s %s: pending_entry, cancelling limit order=%s rule=%s",
            side, symbol, entry_oid, exit_rule,
        )
        if entry_oid:
            cancel_order_by_id(symbol, str(entry_oid))
        cancel_pending_position(pos["id"], reason=exit_rule)
        sl_id = pos.get("signal_log_id")
        if sl_id:
            update_signal_status(int(sl_id), "closed", "paper_close_pending")
        return True

    # side 一致性校验
    if side.upper() != pos["side"].upper():
        logger.warning(
            "close_position side mismatch: req=%s db=%s, using db side",
            side, pos["side"],
        )

    qty = pos.get("quantity")
    if not qty:
        logger.warning("close_position pos=%s has no quantity", pos["id"])
        return False

    hedge = _detect_hedge_mode()
    position_side = (pos["side"] if hedge else None)
    close_side = "SELL" if pos["side"] == "LONG" else "BUY"

    # MARKET 平仓（先平仓，成功后再清理 SL/TP，避免裸仓）
    actual_close: Optional[float] = None
    market_ok = False
    try:
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": qty,
            "reduceOnly": "true",
        }
        if position_side:
            params["positionSide"] = position_side
            params.pop("reduceOnly", None)
        resp = place_order(params)
        avg = resp.get("avgPrice")
        if avg and float(avg) > 0:
            actual_close = float(avg)
            market_ok = True
        logger.info(
            "close_position %s %s: MARKET filled qty=%s price=%s",
            side, symbol, qty, actual_close,
        )
    except Exception as exc:
        logger.error("close_position MARKET failed %s %s: %s", side, symbol, exc)

    if not market_ok:
        logger.critical(
            "close_position ABORTED %s %s: MARKET order failed, SL/TP retained",
            side, symbol,
        )
        return False

    # 平仓成功，清理 SL/TP 条件单
    try:
        ok = cancel_all_orders(symbol, pos)
        if ok:
            logger.info("close_position %s %s: orders cancelled", side, symbol)
        else:
            logger.error(
                "close_position %s %s: SL/TP cancel FAILED, manual cleanup needed "
                "sl=%s tp=%s",
                side, symbol, pos.get("sl_order_id"), pos.get("tp_order_id"),
            )
    except Exception as exc:
        logger.error(
            "close_position cancel_all_orders %s %s: %s sl=%s tp=%s",
            side, symbol, exc, pos.get("sl_order_id"), pos.get("tp_order_id"),
        )

    _record_closed_position(pos, exit_rule, actual_close)
    logger.info(
        "close_position done: %s %s source=%s rule=%s close=%.6f",
        side, symbol, source, exit_rule, actual_close or 0,
    )
    return True


def sync_open_positions() -> None:
    global _SYNC_AUTH_FAIL_COUNT
    if not get_config("binance_api_key", ""):
        return
    if get_config("enabled", "false").lower() != "true":
        return

    for pos in get_open_positions():
        try:
            if get_live_position(pos["symbol"]) is not None:
                _SYNC_AUTH_FAIL_COUNT = 0
                continue

            close_reason = "unknown"
            close_price: Optional[float] = None
            saw_pending_algo = False

            for order_id, reason in [
                (pos["tp_order_id"], "tp"),
                (pos["sl_order_id"], "sl"),
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
                        logger.info("sync pos=%s matched %s algo status=%s", pos["id"], reason, algo_status)
                        break
                    if algo_status in ("WORKING", "NEW", "PENDING"):
                        saw_pending_algo = True
                        continue
                    logger.info("sync pos=%s %s algoStatus=%s", pos["id"], reason, algo_status)
                except Exception as exc:
                    logger.debug("get_algo_order(%s) failed: %s", order_id, exc)
                if algo_seen:
                    continue
                try:
                    o = get_order(pos["symbol"], order_id)
                    status = o.get("status", "")
                    if status == "FILLED":
                        close_reason = reason
                        avg = o.get("avgPrice")
                        stop = o.get("stopPrice")
                        if avg and float(avg) > 0:
                            close_price = float(avg)
                        elif stop:
                            close_price = float(stop)
                        break
                    if status in ("NEW", "PARTIALLY_FILLED"):
                        saw_pending_algo = True
                    else:
                        logger.info("sync pos=%s %s legacy status=%s", pos["id"], reason, status)
                except Exception as exc:
                    logger.debug("get_order(%s,%s) failed: %s", pos["symbol"], order_id, exc)

            if close_reason == "unknown" and saw_pending_algo:
                close_reason = "manual"

            # 动量/接针只有 SL 单(无 TP)，若 SL 被 paper close 提前取消，
            # sync 无法溯源，推断为 paper_close
            if close_reason == "unknown" and not pos.get("tp_order_id"):
                if pos.get("sl_order_id"):
                    try:
                        sl_info = get_algo_order(pos["sl_order_id"])
                        sl_status = (sl_info.get("algoStatus") or "").upper()
                        if sl_status in ("CANCELLED", "EXPIRED", "CANCELED"):
                            close_reason = "paper_close"
                            logger.info(
                                "sync pos=%s %s: SL cancelled, infer paper_close",
                                pos["id"], pos.get("symbol"),
                            )
                    except Exception:
                        close_reason = "paper_close"
                        logger.info(
                            "sync pos=%s %s: SL not found, infer paper_close",
                            pos["id"], pos.get("symbol"),
                        )
                else:
                    close_reason = "paper_close"
                    logger.info(
                        "sync pos=%s %s: no SL/TP, infer paper_close",
                        pos["id"], pos.get("symbol"),
                    )

            # ZCT VWAP(或任何同时有 TP+SL 的仓位): 两张条件单都处于
            # CANCELLED/EXPIRED 等终态但均未触发, 仓位在币安已消失,
            # 推断为外部平仓(交易所强制平仓/系统取消等)
            if close_reason == "unknown":
                all_cancelled = True
                for oid in (pos.get("tp_order_id"), pos.get("sl_order_id")):
                    if not oid:
                        all_cancelled = False
                        break
                    try:
                        info = get_algo_order(oid)
                        st = (info.get("algoStatus") or "").upper()
                        if st not in ("CANCELLED", "EXPIRED", "CANCELED"):
                            all_cancelled = False
                            break
                    except Exception:
                        pass
                if all_cancelled:
                    close_reason = "paper_close"
                else:
                    close_reason = "external"
                logger.info(
                    "sync pos=%s %s: fallback close_reason=%s (all_cancelled=%s)",
                    pos["id"], pos.get("symbol"), close_reason, all_cancelled,
                )

            if close_price is None:
                try:
                    close_price = get_mark_price(pos["symbol"])
                except Exception:
                    close_price = None

            # 清理币安上残留的条件单(SL/TP)
            try:
                cancel_all_orders(pos["symbol"], pos)
            except Exception as exc:
                logger.warning("sync cancel_all_orders %s: %s", pos["symbol"], exc)

            _record_closed_position(pos, close_reason, close_price)
            _SYNC_AUTH_FAIL_COUNT = 0

        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in (401, 403):
                _SYNC_AUTH_FAIL_COUNT += 1
                if _SYNC_AUTH_FAIL_COUNT >= _SYNC_AUTH_FAIL_THRESHOLD:
                    set_config("enabled", "false")
                    logger.critical(
                        "Binance auth failed %d times; DISABLED trading (rotate API key then re-enable)",
                        _SYNC_AUTH_FAIL_COUNT,
                    )
                else:
                    logger.warning("sync pos=%s auth-fail %d/%d", pos["id"], _SYNC_AUTH_FAIL_COUNT, _SYNC_AUTH_FAIL_THRESHOLD)
            else:
                logger.warning("sync pos=%s: %s", pos["id"], exc)
        except Exception as exc:
            logger.warning("sync pos=%s: %s", pos["id"], exc)


def reconcile_pending_entries() -> None:
    """轮询 status='pending_entry' 的持仓，按 entry 限价单状态分发：

    - FILLED          -> 用真实成交价/数量补 SL/TP，promote 到 open
    - PARTIALLY_FILLED 未到 deadline -> 跳过
    - PARTIALLY_FILLED 到 deadline   -> 用已成交部分 promote，剩余撤掉
    - NEW             未到 deadline -> 跳过
    - NEW             到 deadline   -> cancel + cancelled_pending(timeout)
    - CANCELED/REJECTED/EXPIRED     -> cancelled_pending(rejected)
    """
    global _SYNC_AUTH_FAIL_COUNT
    if not get_config("binance_api_key", ""):
        return
    if get_config("enabled", "false").lower() != "true":
        return

    pending = get_pending_entries()
    if not pending:
        return

    for pos in pending:
        try:
            _reconcile_one_pending(pos)
            _SYNC_AUTH_FAIL_COUNT = 0
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in (401, 403):
                _SYNC_AUTH_FAIL_COUNT += 1
                if _SYNC_AUTH_FAIL_COUNT >= _SYNC_AUTH_FAIL_THRESHOLD:
                    set_config("enabled", "false")
                    logger.critical(
                        "Binance auth failed %d times; DISABLED trading (rotate API key then re-enable)",
                        _SYNC_AUTH_FAIL_COUNT,
                    )
                else:
                    logger.warning("reconcile pos=%s auth-fail %d/%d",
                                   pos["id"], _SYNC_AUTH_FAIL_COUNT, _SYNC_AUTH_FAIL_THRESHOLD)
            else:
                logger.warning("reconcile pos=%s: %s", pos["id"], exc)
        except Exception as exc:
            logger.warning("reconcile pos=%s: %s", pos["id"], exc)


def _reconcile_one_pending(pos: Dict[str, Any]) -> None:
    pos_id = pos["id"]
    symbol = pos["symbol"]
    side = pos["side"]
    entry_order_id = pos.get("entry_order_id")
    deadline = pos.get("entry_deadline")
    signal_log_id = pos.get("signal_log_id")

    if not entry_order_id:
        logger.error("reconcile pos=%s %s: missing entry_order_id, marking error", pos_id, symbol)
        cancel_pending_position(pos_id, reason="error_no_orderid")
        if signal_log_id:
            update_signal_status(int(signal_log_id), "error", "pending without entry_order_id")
        return

    now_iso = _now_utc()
    past_deadline = bool(deadline and now_iso >= deadline)

    info = get_order(symbol, str(entry_order_id))
    status = (info.get("status") or "").upper()
    executed_qty = float(info.get("executedQty") or 0)
    avg_price = float(info.get("avgPrice") or 0)

    if status == "FILLED":
        _promote_pending(pos, fill_qty=executed_qty, fill_price=avg_price)
        return

    if status == "PARTIALLY_FILLED":
        if not past_deadline:
            logger.info("reconcile pos=%s %s: PARTIALLY_FILLED, waiting", pos_id, symbol)
            return
        if executed_qty > 0 and avg_price > 0:
            cancel_order_by_id(symbol, str(entry_order_id))
            _promote_pending(pos, fill_qty=executed_qty, fill_price=avg_price)
            return
        cancel_order_by_id(symbol, str(entry_order_id))
        cancel_pending_position(pos_id, reason="timeout_no_fill")
        if signal_log_id:
            update_signal_status(int(signal_log_id), "cancelled_pending", "limit timeout (partial=0)")
        logger.info("reconcile pos=%s %s: timeout no real fill", pos_id, symbol)
        return

    if status in ("CANCELED", "EXPIRED", "REJECTED"):
        cancel_pending_position(pos_id, reason="rejected")
        if signal_log_id:
            update_signal_status(int(signal_log_id), "cancelled_pending", f"order {status}")
        logger.info("reconcile pos=%s %s: order %s, marked cancelled_pending", pos_id, symbol, status)
        return

    # NEW / 其它
    if not past_deadline:
        logger.debug("reconcile pos=%s %s: status=%s, waiting", pos_id, symbol, status)
        return

    cancel_order_by_id(symbol, str(entry_order_id))
    cancel_pending_position(pos_id, reason="timeout")
    if signal_log_id:
        update_signal_status(int(signal_log_id), "cancelled_pending", "limit timeout")
    logger.info("reconcile pos=%s %s: timeout (status=%s), cancelled", pos_id, symbol, status)


def _promote_pending(pos: Dict[str, Any], *, fill_qty: float, fill_price: float) -> None:
    """entry 已成交（全部或部分到截止）：下 SL/TP，promote 到 open。"""
    pos_id = pos["id"]
    symbol = pos["symbol"]
    side = pos["side"]
    sl_price = pos.get("sl_price")
    tp_price = pos.get("tp_price")
    play = pos.get("play")
    source = pos.get("source") or ""
    signal_log_id = pos.get("signal_log_id")

    if fill_qty <= 0 or fill_price <= 0:
        logger.error("promote pos=%s %s: invalid fill qty=%s price=%s", pos_id, symbol, fill_qty, fill_price)
        cancel_pending_position(pos_id, reason="invalid_fill")
        if signal_log_id:
            update_signal_status(int(signal_log_id), "error", "invalid fill in promote")
        return

    try:
        _, tick_size, _ = _get_filters(symbol)
    except Exception as exc:
        logger.error("promote pos=%s %s: get filters failed: %s", pos_id, symbol, exc)
        return

    hedge = _detect_hedge_mode()
    position_side = side if hedge else None
    close_side = "SELL" if side == "LONG" else "BUY"

    final_sl_p = _round_price(float(sl_price), tick_size) if sl_price is not None else None
    final_tp_p = _round_price(float(tp_price), tick_size) if tp_price is not None else None

    if final_sl_p is not None:
        try:
            mark_px = get_mark_price(symbol)
            _validate_sl_distance(side, final_sl_p, mark_px, tick_size)
        except ValueError as exc:
            logger.warning("promote SL validation failed %s: %s", symbol, exc)
        except Exception as exc:
            logger.warning("promote mark_price fetch failed %s: %s", symbol, exc)

    sl_order_id = ""
    tp_order_id = ""
    try:
        if final_sl_p is not None:
            sl_resp = _place_protective(symbol, close_side, final_sl_p, fill_qty, position_side, tick_size, "SL")
            sl_order_id = str(sl_resp.get("algoId", "") or sl_resp.get("orderId", ""))
            logger.info("promote SL placed: pos=%s %s sl=%.6f algoId=%s", pos_id, symbol, final_sl_p, sl_order_id)
        if final_tp_p is not None:
            tp_resp = _place_protective(symbol, close_side, final_tp_p, fill_qty, position_side, tick_size, "TP")
            tp_order_id = str(tp_resp.get("algoId", "") or tp_resp.get("orderId", ""))
            logger.info("promote TP placed: pos=%s %s tp=%.6f algoId=%s", pos_id, symbol, final_tp_p, tp_order_id)
    except Exception as exc:
        logger.error("promote SL/TP failed pos=%s %s: %s — emergency close", pos_id, symbol, exc)
        try:
            cancel_all_orders(symbol)
        except Exception:
            pass
        _emergency_close(symbol, side, fill_qty, position_side)
        cancel_pending_position(pos_id, reason="sltp_failed")
        if signal_log_id:
            update_signal_status(int(signal_log_id), "error", f"SL/TP failed in promote: {exc}")
        return

    expire_hours = resolve_expire_hours(play, source=source)
    expire_at = compute_expire_at(expire_hours)

    promote_pending_to_open(
        pos_id,
        entry_price=fill_price,
        quantity=fill_qty,
        sl_order_id=sl_order_id,
        tp_order_id=tp_order_id,
        expire_at=expire_at,
    )
    if signal_log_id:
        update_signal_status(int(signal_log_id), "traded")
    logger.info(
        "promote pos=%s %s %s qty=%.6f entry=%.6f sl=%s tp=%s expire=%s",
        pos_id, side, symbol, fill_qty, fill_price, final_sl_p, final_tp_p, expire_at,
    )


def expire_open_positions() -> None:
    if not get_config("binance_api_key", ""):
        return
    if get_config("enabled", "false").lower() != "true":
        return

    expired = get_open_expired_positions()
    if not expired:
        return

    for pos in expired:
        symbol = pos["symbol"]
        side = pos["side"]
        qty = pos.get("quantity")
        if not qty:
            logger.warning("expire pos=%s has no quantity; skipping", pos["id"])
            continue

        logger.info("Expiring pos id=%s %s %s (expire_at=%s)", pos["id"], side, symbol, pos.get("expire_at"))

        hedge = _detect_hedge_mode()
        position_side = (side if hedge else None)
        close_side = "SELL" if side == "LONG" else "BUY"
        close_price: Optional[float] = None

        try:
            cancel_all_orders(symbol, pos)
        except Exception as exc:
            logger.warning("expire cancel_all_orders %s: %s", symbol, exc)
        try:
            params: Dict[str, Any] = {
                "symbol": symbol,
                "side": close_side,
                "type": "MARKET",
                "quantity": qty,
                "reduceOnly": "true",
            }
            if position_side:
                params["positionSide"] = position_side
                params.pop("reduceOnly", None)
            resp = place_order(params)
            avg = resp.get("avgPrice")
            if avg and float(avg) > 0:
                close_price = float(avg)
        except Exception as exc:
            logger.error("expire close FAILED pos=%s %s: %s", pos["id"], symbol, exc)

        if close_price is None:
            try:
                close_price = get_mark_price(symbol)
            except Exception:
                close_price = 0.0

        _record_closed_position(pos, "expired", close_price)
        logger.info("Expired pos=%s %s %s close=%.6f", pos["id"], side, symbol, close_price or 0)


def _record_closed_position(
    pos: Dict[str, Any],
    close_reason: str,
    close_price: Optional[float],
) -> None:
    entry = pos.get("entry_price")
    qty = pos.get("quantity")
    lev = pos.get("leverage") or 1
    side = pos.get("side")

    if entry is None or qty is None or close_price is None or entry <= 0:
        logger.warning(
            "record_closed pos=%s incomplete data (entry=%s qty=%s close=%s); pnl=0",
            pos["id"], entry, qty, close_price,
        )
        update_position_closed(
            position_id=pos["id"],
            close_reason=close_reason,
            close_price=close_price or 0.0,
            closed_at=_now_utc(),
            pnl_usdt=0.0,
            pnl_pct=0.0,
        )
        return

    if side == "LONG":
        pnl = qty * (close_price - entry)
        ret = close_price / entry - 1.0
    else:
        pnl = qty * (entry - close_price)
        ret = (entry - close_price) / entry if entry > 0 else 0.0

    pnl_pct = ret * lev * 100.0
    update_position_closed(
        position_id=pos["id"],
        close_reason=close_reason,
        close_price=close_price,
        closed_at=_now_utc(),
        pnl_usdt=round(pnl, 4),
        pnl_pct=round(pnl_pct, 4),
    )
    # 同步更新 signals_log
    signal_log_id = pos.get("signal_log_id")
    if signal_log_id:
        update_signal_status(signal_log_id, "closed", close_reason)
    logger.info(
        "Closed %s %s reason=%s close=%.6f pnl=%.4f pct=%.2f%%",
        side, pos.get("symbol"), close_reason, close_price, pnl, pnl_pct,
    )
