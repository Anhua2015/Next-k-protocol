"""信号摄入流水线：守卫链 → 分发执行。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict

from models import SignalIngestResult

from ingest.guards import GUARDS, guard_dedup_insert

logger = logging.getLogger("ingest.pipeline")


@dataclass
class IngestContext:
    db: Any
    max_pos: int = 8
    play_max: Dict[str, int] = field(default_factory=dict)
    source_max: Dict[str, int] = field(default_factory=dict)


def process_signal_batch(
    signals, db_module, max_pos: int,
    play_max: Dict[str, int], source_max: Dict[str, int],
) -> SignalIngestResult:
    """处理一批信号，返回 SignalIngestResult。"""
    ctx = IngestContext(db=db_module, max_pos=max_pos,
                        play_max=play_max, source_max=source_max)

    result = {"scanned": 0, "traded": 0, "skipped": 0, "errors": 0, "details": []}

    for sig in signals:
        result["scanned"] += 1
        detail = _process_one(sig, ctx)
        action = detail.get("action", "error")
        if action == "traded":
            result["traded"] += 1
        elif action == "error":
            result["errors"] += 1
        else:
            result["skipped"] += 1
        result["details"].append(detail)

    if result["scanned"]:
        logger.info("ingest complete: scanned=%d traded=%d skipped=%d errors=%d",
                    result["scanned"], result["traded"], result["skipped"], result["errors"])
    return SignalIngestResult(**result)


def _process_one(sig, ctx: IngestContext) -> Dict[str, Any]:
    detail = {
        "api_signal_id": sig.api_signal_id,
        "symbol": sig.symbol, "side": sig.side, "source": sig.source,
    }

    logger.info("ingest signal: source=%s id=%s symbol=%s side=%s play=%s",
                sig.source, sig.api_signal_id, sig.symbol, sig.side, sig.play)

    # 交易总开关
    if ctx.db.get_config("enabled", "false").lower() != "true":
        detail["action"] = "skipped_disabled"
        return detail

    # 守卫链
    dedup_signal_log_id = None
    for guard in GUARDS:
        decision = guard(sig, ctx)
        if guard is guard_dedup_insert:
            dedup_signal_log_id = decision.signal_log_id
        if decision.skip:
            signal_log_id = decision.signal_log_id
            action = decision.action
            reason = decision.reason
            if signal_log_id and action:
                ctx.db.update_signal_status(signal_log_id, action, reason)
            detail["action"] = action
            return detail

    # 所有 guard 通过 → 执行交易
    signal_log_id = dedup_signal_log_id
    from ingest.dispatcher import dispatch
    return dispatch(sig, signal_log_id)
