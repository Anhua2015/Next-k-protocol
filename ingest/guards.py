"""信号摄入守卫链。

每个 guard 返回 GuardDecision(skip, reason, action, signal_log_id)。
任一 guard skip=True → 跳过本次信号。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("ingest.guards")

VALID_SOURCES = {"zct_vwap", "momentum", "jiezhen", "moss_quant"}


@dataclass
class GuardDecision:
    skip: bool
    reason: str = ""
    action: str = ""
    signal_log_id: Optional[int] = None


def guard_trading_disabled(sig: Any, _ctx: Any) -> GuardDecision:
    """交易总开关关闭时全部跳过（在 pipeline 层面处理，此 guard 保留占位）。"""
    return GuardDecision(skip=False)


def guard_invalid_source(sig: Any, _ctx: Any) -> GuardDecision:
    if sig.source not in VALID_SOURCES:
        return GuardDecision(skip=True, reason=f"invalid source={sig.source}",
                             action="skipped_invalid_source")
    return GuardDecision(skip=False)


def guard_source_disabled(sig: Any, ctx: Any) -> GuardDecision:
    if ctx.db.source_enabled(sig.source):
        return GuardDecision(skip=False)
    return GuardDecision(
        skip=True,
        reason=f"source disabled: {sig.source}",
        action="skipped_source_disabled",
    )


def _signal_payload(sig: Any) -> Dict[str, Any]:
    if hasattr(sig, "model_dump"):
        return sig.model_dump()
    if hasattr(sig, "dict"):
        return sig.dict()
    from dataclasses import asdict, is_dataclass

    if is_dataclass(sig):
        return asdict(sig)
    return dict(vars(sig))


def guard_dedup_insert(sig: Any, ctx: Any) -> GuardDecision:
    """去重：insert_signal 返回 None = 已存在。"""
    from binance.time_sync import now_utc
    payload = _signal_payload(sig)
    sid = ctx.db.insert_signal(
        source=sig.source, api_signal_id=sig.api_signal_id,
        symbol=sig.symbol, side=sig.side,
        entry_price=sig.entry_price, sl_price=sig.sl_price, tp_price=sig.tp_price,
        confidence=sig.confidence, regime=sig.regime,
        notional_usdt=None, received_at=now_utc(),
        play=sig.play or "",
        profile_id=getattr(sig, "profile_id", None),
        client_ref=getattr(sig, "client_ref", None) or "",
        action=getattr(sig, "action", None) or (
            "rolling" if "rolling" in (sig.play or "").lower() else "open"
        ),
        payload_json=json.dumps(payload, ensure_ascii=False, default=str),
    )
    if sid is None:
        return GuardDecision(skip=True, action="duplicate")
    return GuardDecision(skip=False, signal_log_id=sid)


def _signal_action(sig: Any) -> str:
    action = getattr(sig, "action", None) or ""
    if action:
        return str(action).lower()
    play = (getattr(sig, "play", None) or "").lower()
    if "rolling" in play:
        return "rolling"
    return "open"


def _symbol_has_live_position(symbol: str) -> bool:
    from trader import list_live_positions

    sym = str(symbol or "").upper()
    for pos in list_live_positions():
        if str(pos.get("symbol") or "").upper() == sym:
            amt = float(pos.get("positionAmt") or pos.get("quantity") or 0)
            if amt != 0:
                return True
    return False


def guard_position_exists(sig: Any, ctx: Any) -> GuardDecision:
    """开仓/加仓：同 symbol 已有持仓则跳过。"""
    action = _signal_action(sig)
    if action not in ("open", "rolling"):
        return GuardDecision(skip=False)
    if _symbol_has_live_position(sig.symbol):
        return GuardDecision(
            skip=True,
            reason="open position for symbol",
            action="skipped_position_exists",
        )
    return GuardDecision(skip=False)


def guard_close_requires_position(sig: Any, ctx: Any) -> GuardDecision:
    """平仓：无持仓则跳过。"""
    if _signal_action(sig) != "close":
        return GuardDecision(skip=False)
    if _symbol_has_live_position(sig.symbol):
        return GuardDecision(skip=False)
    return GuardDecision(
        skip=True,
        reason="no open position for symbol",
        action="skipped_no_position",
    )


def guard_max_positions(sig: Any, ctx: Any) -> GuardDecision:
    """仅开仓/加仓时按全局最大持仓数检查。"""
    if _signal_action(sig) not in ("open", "rolling"):
        return GuardDecision(skip=False)
    from trader import list_live_positions

    max_pos = ctx.max_pos
    if len(list_live_positions()) >= max_pos:
        return GuardDecision(
            skip=True,
            reason=f"global max={max_pos}",
            action="skipped_max_positions",
        )
    return GuardDecision(skip=False)


GUARDS = [
    guard_trading_disabled,
    guard_invalid_source,
    guard_dedup_insert,
    guard_source_disabled,
    guard_position_exists,
    guard_close_requires_position,
    guard_max_positions,
]
