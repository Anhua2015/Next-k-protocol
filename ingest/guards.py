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
    return GuardDecision(skip=False)


def guard_dedup_insert(sig: Any, ctx: Any) -> GuardDecision:
    """去重：insert_signal 返回 None = 已存在。"""
    from binance.time_sync import now_utc
    payload = sig.model_dump() if hasattr(sig, "model_dump") else sig.dict()
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


def guard_position_exists(sig: Any, ctx: Any) -> GuardDecision:
    # moss_quant 允许同 symbol 加仓（滚仓场景）
    if sig.source == "moss_quant":
        return GuardDecision(skip=False)
    if ctx.db.get_open_position_for_symbol(sig.symbol) is not None:
        return GuardDecision(skip=True, reason="open position for symbol",
                             action="skipped_position_exists")
    return GuardDecision(skip=False)


def guard_max_positions(sig: Any, ctx: Any) -> GuardDecision:
    """按 source 分支检查仓位上限。"""
    source = sig.source
    play = sig.play or ""
    max_pos = ctx.max_pos

    if source == "zct_vwap":
        pu = play.strip().upper()
        for k in ("PLAY01", "PLAY02", "PLAY03"):
            if pu.startswith(k):
                play_max = ctx.play_max.get(k.lower(), 5)
                if ctx.db.count_open_by_play(play) >= play_max:
                    return GuardDecision(
                        skip=True, reason=f"play={play} max={play_max}",
                        action="skipped_max_positions")
                break

    if source != "zct_vwap":
        src_max = ctx.source_max.get(source, 5)
        if ctx.db.count_open_by_source(source) >= src_max:
            return GuardDecision(
                skip=True, reason=f"source={source} max={src_max}",
                action="skipped_max_positions")

    if ctx.db.count_open_total() >= max_pos:
        return GuardDecision(skip=True, reason=f"global max={max_pos}",
                             action="skipped_max_positions")
    return GuardDecision(skip=False)


GUARDS = [
    guard_trading_disabled,
    guard_invalid_source,
    guard_dedup_insert,
    guard_source_disabled,
    guard_position_exists,
    guard_max_positions,
]
