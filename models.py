"""Pydantic 模型 — Binance 实盘交易 API 的请求/响应数据结构。

所有模型均带有详细的 Field description，在 Swagger /docs 中自动展示。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SignalItem(BaseModel):
    """单条 ORB 信号，由 next-k-api 在扫描完成后推送。"""

    source: str = Field(
        "orb",
        description="信号来源标识，如 'orb'",
    )
    api_signal_id: str = Field(
        ...,
        description="调用方生成的唯一 ID，用于去重",
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
    margin_usdt: Optional[float] = Field(
        None,
        gt=0,
        description="本次交易保证金（USDT）；开仓/滚仓必填，平仓可为空",
    )
    leverage: Optional[float] = Field(
        None,
        gt=0,
        description="本次交易杠杆；开仓/滚仓必填，平仓可为空",
    )
    entry_price: Optional[float] = Field(
        None,
        description="建议入场价（信号触发时的 VWAP 价格）",
    )
    sl_price: Optional[float] = Field(
        None,
        description="止损价格；开仓时用于初始保护单",
    )
    tp_price: Optional[float] = Field(
        None,
        description="止盈价格，由 next-k-api 计算后推送。Protocol 不做二次计算",
    )
    close_price: Optional[float] = Field(
        None,
        description="建议平仓价；有值且非收盘平仓时 LIMIT 减仓，否则 MARKET",
    )
    confidence: Optional[str] = Field(
        None,
        description="信号置信度标签，如 'high'、'medium'、'low'",
    )
    regime: Optional[str] = Field(
        None,
        description="市场状态标记，如 'TREND_UP'、'RANGE'",
    )
    play: Optional[str] = Field(
        None,
        description="策略子类型标记，ORB 可空",
    )
    profile_id: Optional[int] = Field(
        None,
        description="可选的调用方 profile 标识",
    )
    client_ref: Optional[str] = Field(
        None,
        description="调用方生成的动作引用 ID，用于回填 position 和排查重复调用",
    )
    action: Optional[str] = Field(
        "open",
        description="动作类型：open / rolling / close",
    )


class SignalIngestRequest(BaseModel):
    """信号批量推送请求体。"""

    signals: List[SignalItem] = Field(
        ...,
        description="待处理的信号列表",
        min_length=0,
        max_length=100,
    )


class SignalIngestResult(BaseModel):
    """信号推送处理的汇总结果。"""

    scanned: int = Field(0, description="本次推送的信号总数")
    traded: int = Field(0, description="成功开仓的信号数")
    skipped: int = Field(0, description="跳过的信号数（通常为重复 api_signal_id）")
    errors: int = Field(0, description="处理失败的信号数")
    details: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="每条信号的处理详情",
    )


class LivePositionOut(BaseModel):
    """币安实时持仓视图。"""

    symbol: str = Field(..., description="交易对符号")
    side: str = Field(..., description="持仓方向：LONG / SHORT")
    quantity: float = Field(..., description="持仓数量（绝对值）")
    entry_price: Optional[float] = Field(None, description="开仓均价")
    mark_price: Optional[float] = Field(None, description="当前标记价格")
    unrealized_pnl_usdt: float = Field(..., description="当前未实现盈亏")
    leverage: Optional[int] = Field(None, description="当前仓位杠杆")
    liquidation_price: Optional[float] = Field(None, description="预估强平价")
    margin_type: Optional[str] = Field(None, description="保证金模式，如 ISOLATED / CROSSED")


class SignalLogOut(BaseModel):
    """信号日志记录（API 返回）。"""

    id: int = Field(..., description="信号日志主键 ID")
    source: str = Field(..., description="信号来源标识，如 'orb'")
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
    play: Optional[str] = Field(None, description="策略子类型")
    profile_id: Optional[int] = Field(None, description="调用方 profile 标识")
    client_ref: Optional[str] = Field(None, description="调用方动作引用 ID")
    action: Optional[str] = Field(None, description="动作类型")
    position_id: Optional[int] = Field(None, description="关联持仓 ID")
    payload_json: Optional[str] = Field(None, description="动作请求快照 JSON")
    result_json: Optional[str] = Field(None, description="动作结果快照 JSON")


class AccountSummaryOut(BaseModel):
    asset: str = Field("USDT", description="账户资产")
    wallet_balance_usdt: float = Field(..., description="USDT 钱包余额")
    available_balance_usdt: float = Field(..., description="USDT 可用余额")
    unrealized_pnl_usdt: float = Field(..., description="当前未实现盈亏")


class PnlSyncOut(BaseModel):
    """盈亏流水同步结果。"""

    ok: bool = Field(..., description="是否成功")
    days: int = Field(..., description="同步最近 N 天")
    fetched: int = Field(..., description="从 Binance 拉取到的流水数量")
    inserted: int = Field(..., description="新增写入本地缓存的流水数量")


class PnlClearOut(BaseModel):
    """盈亏缓存清理结果。"""

    ok: bool = Field(..., description="是否成功")
    deleted_events: int = Field(..., description="删除的 income_events 数量")
    deleted_sync_state: int = Field(..., description="删除的同步状态数量")


class PnlSummaryRow(BaseModel):
    """单个日/周/月周期的净盈亏聚合。"""

    period: str = Field(..., description="周期标签：YYYY-MM-DD / YYYY-Www / YYYY-MM")
    net_pnl_usdt: float = Field(..., description="净盈亏 = 已实现盈亏 + 手续费 + 资金费率 + 返佣")
    realized_pnl_usdt: float = Field(..., description="已实现盈亏")
    commission_usdt: float = Field(..., description="手续费，通常为负数")
    funding_fee_usdt: float = Field(..., description="资金费率，可正可负")
    rebate_usdt: float = Field(..., description="返佣/手续费返还")
    event_count: int = Field(..., description="该周期内 income 流水数量")


class PnlSummaryOut(BaseModel):
    """盈亏摘要返回。"""

    period: str = Field(..., description="聚合粒度：daily / weekly / monthly")
    days: int = Field(..., description="统计最近 N 天")
    start_date: Optional[str] = Field(None, description="只统计该日期及之后，格式 YYYY-MM-DD")
    timezone: str = Field(..., description="周期边界使用的时区")
    totals: Dict[str, Any] = Field(..., description="总计")
    sync_state: Dict[str, str] = Field(default_factory=dict, description="同步状态")
    rows: List[PnlSummaryRow] = Field(default_factory=list, description="周期聚合结果")


class IncomeEventOut(BaseModel):
    """本地缓存的 Binance income 流水。"""

    id: int
    symbol: Optional[str] = None
    income_type: str
    income: float
    asset: str
    time_ms: int
    tran_id: str
    trade_id: Optional[str] = None
    info: Optional[str] = None
    raw_json: Optional[str] = None
    synced_at: str


class TradFiSignOut(BaseModel):
    """TradFi-Perps 协议签署结果。"""

    ok: bool = Field(..., description="是否成功")
    result: str = Field(..., description="币安返回内容，通常为 SUCCESS")


class StatusOut(BaseModel):
    """服务状态概览。"""

    testnet: bool = Field(..., description="是否连接币安测试网（来自 BINANCE_TESTNET 环境变量）")
    open_positions: int = Field(..., description="当前持仓数")
    api_key_set: bool = Field(..., description="币安 API Key 是否已配置")
    execution_paused: bool = Field(..., description="因连续鉴权失败等原因暂停执行")
    db_path: str = Field(..., description="SQLite 数据库文件路径")
