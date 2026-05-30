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
        description='配置键值对，如 {"enabled": "true", "entry_type": "MARKET"}。'
        "币安 API Key/Secret 不允许通过该接口修改。",
        examples=[{"enabled": "true", "entry_type": "MARKET"}],
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
    margin_usdt: Optional[float] = Field(
        None,
        gt=0,
        description="本次交易保证金（USDT）；开仓/滚仓必填，平仓/更新止损可为空",
    )
    leverage: Optional[float] = Field(
        None,
        gt=0,
        description="本次交易杠杆；开仓/滚仓必填，平仓/更新止损可为空",
    )
    entry_price: Optional[float] = Field(
        None,
        description="建议入场价（信号触发时的 VWAP 价格）",
    )
    sl_price: Optional[float] = Field(
        None,
        description="止损价格；开仓/滚仓时用于初始保护单，update_sl 时表示新的止损价格",
    )
    tp_price: Optional[float] = Field(
        None,
        description="止盈价格，由 next-k-api 计算后推送。Protocol 不做二次计算",
    )
    close_price: Optional[float] = Field(
        None,
        description="建议平仓价，仅用于日志记录；实盘按 MARKET 成交",
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
        description="策略子类型标记。ZCT VWAP: PLAY01/PLAY02/PLAY03；动量/接针: 可空",
    )
    profile_id: Optional[int] = Field(
        None,
        description="Moss Quant Profile ID，用于将实仓归属到单个机器人",
    )
    client_ref: Optional[str] = Field(
        None,
        description="调用方生成的动作引用 ID，用于回填 position 和排查重复调用",
    )
    action: Optional[str] = Field(
        "open",
        description="动作类型：open / rolling / close / update_sl / exchange_sl / exchange_tp / external_close",
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


class UpdateSlRequest(BaseModel):
    """动态修改止损价请求，由 Moss Quant 移动止损触发。"""

    new_sl_price: float = Field(
        ...,
        description="新的止损触发价格",
    )
    profile_id: Optional[int] = Field(None, description="Moss Quant Profile ID")
    client_ref: Optional[str] = Field(None, description="调用方动作引用 ID")


class ClosePositionRequest(BaseModel):
    """平仓请求，由 next-k-api 在 trail 触发退出后推送。"""

    source: str = Field(
        ...,
        description="信号来源：momentum / jiezhen",
    )
    api_signal_id: str = Field(
        ...,
        description="原始纸面信号 ID，用于关联 signals_log",
    )
    symbol: str = Field(
        ...,
        description="交易对符号",
    )
    side: str = Field(
        ...,
        description="持仓方向：LONG / SHORT",
        pattern="^(LONG|SHORT)$",
    )
    exit_rule: str = Field(
        ...,
        description="退出原因：trail_stop / trail_low / trail_tier1 / trail_tier2 / expired",
    )
    close_price: Optional[float] = Field(
        None,
        description="建议平仓价（纸面记录的退出价），实盘按 MARKET 成交",
    )
    position_id: Optional[int] = Field(
        None,
        description="指定平仓的持仓 ID。Moss Quant 同 symbol 多仓时需精确指定",
    )
    profile_id: Optional[int] = Field(None, description="Moss Quant Profile ID")
    client_ref: Optional[str] = Field(None, description="调用方动作引用 ID")


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
    play: Optional[str] = Field(None, description="策略子类型")
    profile_id: Optional[int] = Field(None, description="Moss Quant Profile ID")
    client_ref: Optional[str] = Field(None, description="调用方动作引用 ID")
    action: Optional[str] = Field(None, description="动作类型")
    position_id: Optional[int] = Field(None, description="关联持仓 ID")
    payload_json: Optional[str] = Field(None, description="动作请求快照 JSON")
    result_json: Optional[str] = Field(None, description="动作结果快照 JSON")


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


class AccountSummaryOut(BaseModel):
    asset: str = Field("USDT", description="账户资产")
    wallet_balance_usdt: float = Field(..., description="USDT 钱包余额")
    available_balance_usdt: float = Field(..., description="USDT 可用余额")
    unrealized_pnl_usdt: float = Field(..., description="当前未实现盈亏")


class StatusOut(BaseModel):
    """服务状态概览。"""

    enabled: str = Field(..., description="交易是否启用：'true' | 'false'")
    testnet: str = Field(..., description="是否使用测试网：'true' | 'false'")
    open_positions: int = Field(..., description="当前持仓数")
    max_positions: str = Field(..., description="全局最大持仓数")
    api_key_set: bool = Field(..., description="币安 API Key 是否已配置")
    db_path: str = Field(..., description="SQLite 数据库文件路径")
    strategy_positions: dict = Field(
        default_factory=dict,
        description="各策略持仓数，如 {'zct_vwap':3, 'momentum':1, 'jiezhen':2}",
    )


class HealthOut(BaseModel):
    """健康检查响应。"""

    status: str = Field("ok", description="服务状态：'ok' 表示正常运行")
    module: str = Field("next-k-protocol", description="服务名称")
    version: str = Field("1.0.0", description="服务版本号")
