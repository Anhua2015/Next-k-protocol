"""信号摄入守卫链。

Protocol 仅作跳板：记录信号并转发币安，不做策略侧限制。
仅保留 api_signal_id 去重，避免 HTTP 重试重复下单。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger("ingest.guards")


@dataclass
class GuardDecision:
    skip: bool
    reason: str = ""
    action: str = ""
    signal_log_id: Optional[int] = None


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
    """去重：insert_signal 返回 None = 已存在；error/cancelled 允许重试。"""
    from binance.time_sync import now_utc
    payload = _signal_payload(sig)
    action = (getattr(sig, "action", None) or "").strip().lower()
    if not action and "rolling" in (sig.play or "").lower():
        action = "rolling"
    if not action:
        action = "open"
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    sid = ctx.db.insert_signal(
        source=sig.source, api_signal_id=sig.api_signal_id,
        symbol=sig.symbol, side=sig.side,
        entry_price=sig.entry_price, sl_price=sig.sl_price, tp_price=sig.tp_price,
        confidence=sig.confidence, regime=sig.regime,
        notional_usdt=None, received_at=now_utc(),
        play=sig.play or "",
        profile_id=getattr(sig, "profile_id", None),
        client_ref=getattr(sig, "client_ref", None) or "",
        action=action,
        payload_json=payload_json,
    )
    if sid is None:
        existing = ctx.db.get_signal_by_api_id(sig.source, sig.api_signal_id)
        prev_status = str((existing or {}).get("status") or "").lower()
        if existing and prev_status in ("error", "cancelled"):
            ctx.db.reset_signal_for_retry(
                int(existing["id"]),
                received_at=now_utc(),
                payload_json=payload_json,
            )
            logger.info(
                "ingest retry: source=%s id=%s prev_status=%s",
                sig.source,
                sig.api_signal_id,
                prev_status,
            )
            return GuardDecision(skip=False, signal_log_id=int(existing["id"]))
        return GuardDecision(skip=True, action="duplicate")
    return GuardDecision(skip=False, signal_log_id=sid)


GUARDS = [guard_dedup_insert]
