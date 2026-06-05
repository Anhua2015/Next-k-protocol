"""Moss 实盘槽（与 next-k-api 一致：默认 moss2/币安 EN，改 lane 请改本文件）。"""

from __future__ import annotations

from typing import Literal

MossLane = Literal["moss_quant", "moss2"]
MOSS_LANES: tuple[MossLane, ...] = ("moss_quant", "moss2")
MOSS_SOURCES = frozenset(MOSS_LANES)
DEFAULT_MOSS_ACTIVE_LANE: MossLane = "moss2"


def active_moss_lane() -> MossLane:
    return DEFAULT_MOSS_ACTIVE_LANE


def is_moss_source(source: str) -> bool:
    return (source or "").strip().lower() in MOSS_SOURCES
