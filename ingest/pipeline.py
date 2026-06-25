"""信号摄入流水线：守卫链 → 分发执行。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict

from models import SignalIngestResult

from ingest.guards import GUARDS, guard_dedup_insert

logger = logging.getLogger("ingest.pipeline")


@dataclass
class IngestContext:
    db: Any


def process_signal_batch(signals, db_module) -> SignalIngestResult:
    """处理一批信号，返回 SignalIngestResult。"""
    ctx = IngestContext(db=db_module)

    from observability.metrics import SIGNALS_RECEIVED
    result = {"scanned": 0, "traded": 0, "skipped": 0, "errors": 0, "details": []}

    for sig in signals:
        result["scanned"] += 1
        SIGNALS_RECEIVED.labels(source=sig.source, play=sig.play or "").inc()
        detail = _process_one(sig, ctx)
        action = detail.get("action", "error")
        if action == "traded":
            result["traded"] += 1
        elif action == "submitted":
            result["traded"] += 0
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

    # 守卫链（仅去重）
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
            from observability.metrics import SIGNALS_SKIPPED
            SIGNALS_SKIPPED.labels(source=sig.source, code=action).inc()
            detail["action"] = action
            return detail

    # 所有 guard 通过 → 执行交易
    signal_log_id = dedup_signal_log_id
    from ingest.dispatcher import dispatch
    return dispatch(sig, signal_log_id)
