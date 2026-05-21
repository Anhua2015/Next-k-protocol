"""Pydantic 模型 — Binance 实盘交易 API 的请求/响应数据结构。

所有模型均带有详细的 Field description，在 Swagger /docs 中自动展示。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ConfigUpdate(BaseModel):
    """批量更新交易配置的键值对。"""

    pairs: Dict[str, str] = Field(
        ...,
        description='配置键值对，如 {"enabled": "true", "margin_usdt": "200"}。'
        "敏感字段（binance_api_key/binance_api_secret）的值会在日志中脱敏。",
        examples=[{"enabled": "true", "margin_usdt": "200"}],
    )


class SignalItem(BaseModel):
    """单条 ZCT 信号，由 next-k-api 在扫描完成后推送。"""

    source: str = Field(
        "zct_vwap",
        description="信号来源标识，如 'zct_vwap'",
    )
    api_signal_id: str = Field(
        ...,
        description="accumulation.db 中 zct_vwap_signals 表的主键 ID，用于去重",
    )
    symbol: str = Field(
        ...,
        description="交易对符号，如 'BTCUSDT'、'ETHUSDT'",
    )
    side: str = Field(
        ...,
        description="方向：'LONG'（做多）或 'SHORT'（做空）",
        pattern="^(LONG|SHORT)$",
    )
    entry_price: Optional[float] = Field(
        None,
        description="建议入场价（信号触发时的 VWAP 价格）",
    )
    sl_price: float = Field(
        ...,
        description="止损价格（必填）",
    )
    tp_price: float = Field(
        ...,
        description="止盈价格（必填）",
    )
    confidence: Optional[float] = Field(
        None,
        description="信号置信度，范围 0.0-1.0",
    )
    regime: Optional[str] = Field(
        None,
        description="市场状态标记，如 'TREND_UP'、'RANGE'",
    )
    notional_usdt: Optional[float] = Field(
        None,
        description="名义价值（USDT），如果不传则使用默认保证金×杠杆计算",
    )
    play: Optional[str] = Field(
        None,
        description="策略类型标记，如 'PLAY01'、'PLAY02'、'PLAY03'。"
        "用于区分不同策略的持仓上限和过期时限",
    )


class SignalIngestRequest(BaseModel):
    """信号批量推送请求体。"""

    signals: List[SignalItem] = Field(
        ...,
        description="待处理的信号列表，通常来自一次 ZCT 扫描的全部新信号",
        min_length=0,
        max_length=100,
    )


class SignalIngestResult(BaseModel):
    """信号推送处理的汇总结果。"""

    scanned: int = Field(0, description="本次推送的信号总数")
    traded: int = Field(0, description="成功开仓的信号数")
    skipped: int = Field(0, description="跳过的信号数（重复/持仓冲突/仓位已满）")
    errors: int = Field(0, description="处理失败的信号数")
    details: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="每条信号的处理详情",
    )


class PositionOut(BaseModel):
    """持仓记录（API 返回）。"""

    id: int = Field(..., description="持仓主键 ID")
    signal_log_id: Optional[int] = Field(None, description="关联的信号日志 ID")
    symbol: str = Field(..., description="交易对符号")
    side: str = Field(..., description="持仓方向：LONG / SHORT")
    entry_order_id: Optional[str] = Field(None, description="入场订单 ID（币安 orderId）")
    sl_order_id: Optional[str] = Field(None, description="止损条件单 ID（币安 algoId）")
    tp_order_id: Optional[str] = Field(None, description="止盈条件单 ID（币安 algoId）")
    entry_price: Optional[float] = Field(None, description="入场成交均价（USDT）")
    sl_price: Optional[float] = Field(None, description="止损触发价格")
    tp_price: Optional[float] = Field(None, description="止盈触发价格")
    quantity: Optional[float] = Field(None, description="持仓数量（合约张数）")
    notional_usdt: Optional[float] = Field(None, description="名义价值（USDT）")
    leverage: Optional[int] = Field(None, description="杠杆倍数")
    opened_at: str = Field(..., description="开仓时间（UTC ISO8601）")
    expire_at: Optional[str] = Field(None, description="持仓过期时间（UTC ISO8601），到期自动平仓")
    status: str = Field(..., description="持仓状态：'open'（持仓中）| 'closed'（已平仓）")
    close_reason: Optional[str] = Field(
        None,
        description="平仓原因：'tp'(止盈) | 'sl'(止损) | 'expired'(到期) | 'manual'(手动) | 'unknown'(未知)",
    )
    close_price: Optional[float] = Field(None, description="平仓成交均价（USDT）")
    closed_at: Optional[str] = Field(None, description="平仓时间（UTC ISO8601）")
    pnl_usdt: Optional[float] = Field(None, description="已实现盈亏（USDT），正数为盈利")
    pnl_pct: Optional[float] = Field(None, description="杠杆收益率百分比，公式：(ret × leverage × 100)%")


class SignalLogOut(BaseModel):
    """信号日志记录（API 返回）。"""

    id: int = Field(..., description="信号日志主键 ID")
    source: str = Field(..., description="信号来源标识，如 'zct_vwap'")
    api_signal_id: str = Field(..., description="原始信号 ID（用于去重）")
    symbol: str = Field(..., description="交易对符号")
    side: str = Field(..., description="信号方向：LONG / SHORT")
    entry_price: Optional[float] = Field(None, description="信号入场价")
    sl_price: Optional[float] = Field(None, description="信号止损价")
    tp_price: Optional[float] = Field(None, description="信号止盈价")
    confidence: Optional[str] = Field(None, description="信号置信度")
    regime: Optional[str] = Field(None, description="市场状态")
    notional_usdt: Optional[float] = Field(None, description="名义价值（USDT）")
    received_at: str = Field(..., description="信号接收时间（UTC ISO8601）")
    status: str = Field(
        ...,
        description="处理状态：'traded'(已开仓) | 'received'(已收到) | 'error'(错误) | 'skipped_*'(跳过)",
    )
    skip_reason: Optional[str] = Field(None, description="跳过/失败原因")


class DailyPnl(BaseModel):
    """每日 PnL 汇总。"""

    day: str = Field(..., description="日期（YYYY-MM-DD，UTC）")
    pnl: float = Field(..., description="当日总盈亏（USDT）")


class PnlSummaryOut(BaseModel):
    """聚合 P&L 汇总。"""

    total: int = Field(..., description="总交易笔数（已平仓）")
    wins: int = Field(..., description="盈利笔数")
    losses: int = Field(..., description="亏损笔数")
    total_pnl: float = Field(..., description="累计总盈亏（USDT）")
    avg_pnl: float = Field(..., description="平均单笔盈亏（USDT）")
    daily: List[DailyPnl] = Field(default_factory=list, description="近 30 日每日 PnL 明细")


class StatusOut(BaseModel):
    """服务状态概览。"""

    enabled: str = Field(..., description="交易是否启用：'true' | 'false'")
    testnet: str = Field(..., description="是否使用测试网：'true' | 'false'")
    open_positions: int = Field(..., description="当前持仓数")
    max_positions: str = Field(..., description="全局最大持仓数")
    position_expire_hours: str = Field(..., description="全局持仓过期时限（小时）")
    api_key_set: bool = Field(..., description="币安 API Key 是否已配置")
    db_path: str = Field(..., description="SQLite 数据库文件路径")


class HealthOut(BaseModel):
    """健康检查响应。"""

    status: str = Field("ok", description="服务状态：'ok' 表示正常运行")
    module: str = Field("next-k-protocol", description="服务名称")
    version: str = Field("1.0.0", description="服务版本号")
