"""告警：Webhook + 进程内去重。"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Dict, Optional

logger = logging.getLogger("observability.alerts")

_dedup: Dict[str, float] = {}
_DEDUP_COOLDOWN = int(os.getenv("PROTOCOL_ALERT_DEDUP_COOLDOWN_SEC", "300"))


def send_alert(
    level: str, event: str, body: str,
    dedup_key: Optional[str] = None,
) -> None:
    webhook_url = os.getenv("PROTOCOL_ALERT_WEBHOOK_URL", "").strip()
    if not webhook_url:
        logger.warning("alert_emit", level=level, event=event, body=body)
        return

    if dedup_key:
        now = time.time()
        last = _dedup.get(dedup_key, 0)
        if now - last < _DEDUP_COOLDOWN:
            return
        _dedup[dedup_key] = now

    template = os.getenv("PROTOCOL_ALERT_TEMPLATE", "raw_json")
    payload = _format(level, event, body, template)

    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:
        logger.error("alert_send_failed", webhook=webhook_url, error=str(exc))


def send_critical(event: str, details: str = "", dedup_key: str = "") -> None:
    send_alert("critical", event, details, dedup_key)


def _format(level: str, event: str, body: str, template: str) -> dict:
    if template == "telegram":
        return {
            "chat_id": os.getenv("PROTOCOL_ALERT_CHAT_ID", ""),
            "text": f"[{level.upper()}] {event}\n{body}",
        }
    elif template == "dingtalk":
        return {
            "msgtype": "text",
            "text": {"content": f"[{level.upper()}] {event}\n{body}"},
        }
    return {"level": level, "event": event, "body": body, "timestamp": time.time()}
