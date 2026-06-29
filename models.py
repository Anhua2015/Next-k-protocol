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
        description="建议入场价（OR±2tick Stop-Limit 触发/限价）",
    )
    limit_price: Optional[float] = Field(
        None,
        description="Stop-Limit 限价上限（LONG=entry+chase, SHORT=entry-chase）",
    )
    allow_gap_market: Optional[bool] = Field(
        None,
        description="现价已穿越触发价时是否允许 MARKET 追价；preplace 模式应为 false",
    )
    oco_peer_api_id: Optional[str] = Field(
        None,
        description="OCO 对侧 api_signal_id；一侧成交后 Protocol 撤对侧 pending",
    )
    entry_type: Optional[str] = Field(
        "MARKET",
        description="入场方式：MARKET | STOP_LIMIT | stoplimit_gap（ORB 推荐）",
    )
    sl_price: Optional[float] = Field(
        None,
        description="止损价格；开仓时用于初始保护单",
    )
    tp_price: Optional[float] = Field(
        None,
        description="止盈价格，由 next-k-api 计算后推送。Protocol 不做二次计算",
    )
    or_high: Optional[float] = Field(
        None,
        description="Preplace OR 高点；or_range SL 锚定用",
    )
    or_low: Optional[float] = Field(
        None,
        description="Preplace OR 低点；or_range SL 锚定用",
    )
    sl_risk_dist: Optional[float] = Field(
        None,
        description="STOP 触发价与 SL 的距离；fill 后平移 SL（atr 模式）",
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
