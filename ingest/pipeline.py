"""信号摄入流水线：逐条审计、幂等守卫、执行分发和批量汇总。

每条信号独立产生 detail，因此同一批中一条失败不会阻止其余信号执行。路由层在进入
本模块前持有全局写锁，保证批量内的去重插入和状态更新不会与另一个 ingest 请求交错。
"""
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
    """顺序处理一批信号，并把每条 action 汇总为 traded/skipped/errors。"""
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
    """处理单条信号。

    守卫执行时会先插入 ``signals_log``。插入成功得到的主键必须一路传给执行层，
    后续开仓、保护单或平仓结果都更新同一审计记录。
    """
    detail = {
        "api_signal_id": sig.api_signal_id,
        "symbol": sig.symbol, "side": sig.side, "source": sig.source,
    }

    logger.info("ingest signal: source=%s id=%s symbol=%s side=%s play=%s",
                sig.source, sig.api_signal_id, sig.symbol, sig.side, sig.play)

    # 当前守卫链只保留去重。保留 list 结构是为了未来增加纯执行安全守卫时不改编排器。
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
