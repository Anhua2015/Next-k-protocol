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
    """先写审计日志，同时利用唯一键实现跨请求幂等。

    ``UNIQUE(source, api_signal_id)`` 比进程内 set 更可靠：服务重启、多个请求线程或
    API 因网络超时重发同一个信号时，都不会产生第二笔真实订单。
    """
    from binance.time_sync import now_utc
    payload = _signal_payload(sig)
    action = (getattr(sig, "action", None) or "").strip().lower()
    if not action and "rolling" in (sig.play or "").lower():
        action = "rolling"
    if not action:
        action = "open"
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
        payload_json=json.dumps(payload, ensure_ascii=False, default=str),
    )
    if sid is None:
        return GuardDecision(skip=True, action="duplicate")
    return GuardDecision(skip=False, signal_log_id=sid)


GUARDS = [guard_dedup_insert]
