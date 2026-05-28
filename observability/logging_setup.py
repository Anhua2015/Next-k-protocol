"""结构化日志配置 (structlog JSON)。"""
from __future__ import annotations

import logging
import os
import sys

import structlog


def configure_logging() -> None:
    fmt = os.getenv("PROTOCOL_LOG_FORMAT", "text")
    level = os.getenv("PROTOCOL_LOG_LEVEL", "INFO").upper()

    if fmt == "json":
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.dict_tracebacks,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )
    else:
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.dev.ConsoleRenderer(),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
