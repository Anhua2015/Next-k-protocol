# Moss Quant 实仓事实源改造设计

日期: 2026-05-29

## 目标

将 Moss Quant 从“本地纸面交易为主、旁路推送 protocol 实仓”改为“next-k-api 负责策略判断，Next-k-protocol 负责实仓事实”。`next-k-frontend/index.html` 的 Moss 区块不再展示纸面钱包、纸面持仓和纸面 PnL，而是通过 next-k-api 聚合后展示 protocol 中的真实余额、真实仓位和真实盈亏。

## 已确认口径

- Moss Quant 策略判断仍在 `next-k-api`：扫描 K 线、计算入场、退出、移动止损和滚仓。
- `Next-k-protocol` 是实仓事实源：账户余额、Moss 杠杆、仓位、平仓状态、PnL 都以 protocol/币安结果为准。
- Moss 每条 open、rolling、close、update SL 请求必须关联 `profile_id`。
- protocol 信号日志必须记录 next-k-api 对实仓的全部动作调用，包括开仓、平仓、止损/止盈触发、移动止损更新和滚仓。
- 实盘看板中的历史持仓和信号日志需要按策略来源分组展示，至少支持 `zct_vwap`、`momentum`、`jiezhen`、`moss_quant`。
- 如果币安 SL/TP 先触发，protocol 的 closed 状态优先；next-k-api 后续同步本地旧 open 记录，不再把它当持仓展示。
- 开仓大小按真实钱包余额和启用机器人数量平均分配。
- 杠杆倍数使用 protocol 的 `src_moss_quant_leverage`，Moss Profile 的 `base_leverage` 不再参与实仓杠杆判断。

## 架构

```
next-k-api                              Next-k-protocol
moss live scan ──GET /account/summary──→ Binance account summary + config
              ──GET /positions?...────→ real Moss positions
              ──POST /signals/ingest──→ guards → trader → Binance orders
              ──PUT /positions/:id/sl─→ replace real SL order
              ──POST /positions/close─→ market close real position

next-k-frontend/index.html
              ──GET /api/moss-quant/*─→ next-k-api aggregates protocol truth
```

Moss Quant 保持策略脑的职责。`moss_signals`、`moss_settlements`、`moss_wallet` 降级为策略事件、审计和历史兼容数据，不再作为主看板的交易事实。

## Protocol 接口与模型

### Account Summary

新增 `GET /api/binance/account/summary`，需要鉴权。

返回字段：

- `asset`: `"USDT"`
- `wallet_balance_usdt`: USDT 钱包余额
- `available_balance_usdt`: 可用余额
- `unrealized_pnl_usdt`: 当前账户未实现盈亏
- `moss_quant`: Moss 配置摘要，至少包含 `enabled`、`leverage`、`max_positions`、`entry_type`

余额读取失败返回 502。next-k-api 收到失败后本轮禁止新开仓。

### Signal And Position References

扩展 `SignalItem`：

- `profile_id: Optional[int]`
- `client_ref: Optional[str]`
- `action: Optional[str]`

Moss Quant 发送 open/rolling 信号时必须传 `profile_id`。protocol 将字段写入：

- `signals_log.profile_id`
- `signals_log.client_ref`
- `signals_log.action`
- `positions.profile_id`
- `positions.client_ref`

`client_ref` 建议格式：`moss:{profile_id}:{action}:{timestamp_ms}`，用于查询回填和排查重复动作。

`signals_log.action` 用于记录 next-k-api 发起的实仓动作，取值建议：

- `open`: 开仓请求
- `close`: 策略主动平仓请求
- `update_sl`: 移动止损更新
- `update_tp`: 止盈更新，如后续支持动态 TP
- `rolling`: 滚仓/加仓请求
- `exchange_sl`: 交易所止损触发后由 protocol 同步记录
- `exchange_tp`: 交易所止盈触发后由 protocol 同步记录
- `external_close`: 外部平仓或交易所状态同步导致的关闭

当前 `POST /api/binance/signals/ingest` 只覆盖开仓类信号，因此 close、update SL、交易所 SL/TP 同步也需要写入 `signals_log` 或新增等价的 `trade_events` 表。第一阶段推荐复用并扩展 `signals_log`，避免前端和 API 同时读两套事件源。

### Position Query

扩展 `GET /api/binance/positions` 查询参数：

- `source`
- `profile_id`
- `status`
- `limit`
- `offset`

next-k-api 聚合 Moss UI 时查询 `source=moss_quant`，按需分别取 `open` 和 `closed`。

实盘看板使用同一接口按策略展示历史持仓：

- `GET /api/binance/positions?source=zct_vwap&status=closed`
- `GET /api/binance/positions?source=momentum&status=closed`
- `GET /api/binance/positions?source=jiezhen&status=closed`
- `GET /api/binance/positions?source=moss_quant&status=closed`

不传 `source` 时保持现有全策略视图，便于总览。

### Signal Log Query

扩展 `GET /api/binance/signals` 查询参数：

- `source`
- `action`
- `status`
- `profile_id`
- `limit`
- `offset`

实盘看板按策略展示信号日志时使用 `source` 分组过滤。Moss Quant 详情页可进一步按 `profile_id` 查看单机器人事件。

信号日志必须覆盖 next-k-api 对 protocol 的所有实仓调用：

- open 信号进入 `signals/ingest` 时记录 `action=open` 或 `rolling`。
- Moss 主动平仓调用 `positions/close` 时记录 `action=close`，并关联 `position_id`。
- Moss 移动止损调用 `positions/{id}/sl` 时记录 `action=update_sl`，记录旧 SL、新 SL、结果状态。
- protocol scheduler 检测到交易所 SL/TP 成交后记录 `action=exchange_sl` 或 `exchange_tp`，关联真实 position。
- 任何调用失败都以 `status=error` 和 `skip_reason/error` 记录，不能只写应用日志。

如 `signals_log` 字段不足，新增字段：

- `position_id INTEGER`
- `action TEXT DEFAULT 'open'`
- `payload_json TEXT`
- `result_json TEXT`

### PnL

第一阶段不强制新增按 Profile 的 PnL 汇总接口。next-k-api 可从 protocol positions 汇总：

- 已实现 PnL：`status=closed AND source=moss_quant`
- 浮动 PnL：`status=open AND source=moss_quant`

如果 positions 返回性能不足，再新增 `GET /api/binance/pnl/by-profile?source=moss_quant`。

## next-k-api 设计

新增或改造 `moss_quant/protocol_client.py`：

- `get_account_summary()`
- `get_moss_positions(status: str | None = None)`
- `get_moss_leverage()`
- `send_open(...)`
- `send_close(...)`
- `send_update_sl(...)`
- `send_rolling(...)`

现有 `signal_sender.py` 可被保留并迁移到该 client，避免重复 HTTP 逻辑。

### 扫描流程

现有 `run_paper_scan` 可先保留函数名以减少调度影响，但语义调整为 live scan：

1. 读取启用 Moss Profile，得到 `enabled_profile_count = N`。
2. 调 protocol 获取账户余额和 Moss 杠杆。
3. 调 protocol 获取 `source=moss_quant` 的 open positions。
4. 建立 `profile_id -> open positions` 映射。
5. 对每个 Profile：
   - 有真实 open position：用真实仓位数据做 hold、exit、trailing 判断。
   - 无真实 open position：才允许计算新入场。
   - protocol 显示无 open，但本地还有旧 open signal：同步为 `external_closed` 或 `synced_from_protocol`。

### Sizing

开仓名义价值使用真实余额：

```
per_robot_equity = wallet_balance_usdt / enabled_profile_count
margin = min(
    per_robot_equity * risk_per_trade,
    per_robot_equity * max_position_pct,
)
notional = margin * protocol_moss_leverage
```

其中：

- `wallet_balance_usdt` 来自 protocol account summary。
- `enabled_profile_count` 是 `moss_profiles.enabled = 1` 的数量。
- `risk_per_trade` 和 `max_position_pct` 继续来自 Profile 策略参数。
- `protocol_moss_leverage` 来自 protocol `src_moss_quant_leverage`。
- 如果余额、机器人数、杠杆或计算结果无效，本轮跳过开仓并返回明确 skip reason。

### 实仓状态同步

- open signal 只有 protocol 成功执行并能查到 position 后，才算真实持仓。
- 如果 protocol 执行成功但响应里暂时没有 `position_id`，next-k-api 用 `source + profile_id + client_ref/api_signal_id` 查询回填。
- 回填仍失败时标记 `position_link_pending`，下一轮继续回填，禁止重复开仓。
- protocol 已 closed 的仓位优先于本地 Moss 状态；next-k-api 将旧 open 记录同步为外部平仓状态。

## 前端设计

`next-k-frontend/index.html` 的 Moss 区块继续调用 next-k-api 的 Moss API，不直接访问 protocol。

需要调整：

- 标题从 `Moss Quant (Paper)` 改为 `Moss Quant (Live)` 或中文“实仓”。
- “纸面钱包”改为“实仓钱包”。
- “纸面 Profile”改为“实仓 Profile”或“机器人配置”。
- “纸面信号”改为“实仓信号/策略事件”。
- `/api/moss-quant/summary` 展示 protocol 真实余额、真实持仓数、真实已结算数、真实 PnL。
- `/api/moss-quant/signals` 展示 protocol positions 和 Moss 策略事件的聚合结果。
- `/api/moss-quant/paper-scan/latest` 可保持路径兼容，但返回 live scan 摘要，并带 `mode: "live"`。

历史纸面数据保留在数据库中，但默认不进入主看板统计，避免用户误读为实仓。

`next-k-frontend/binance.html` 的实盘看板需要同步增强：

- 历史持仓按策略来源分组或提供策略 tabs/filter。
- 信号日志按策略来源分组或提供策略 tabs/filter。
- 信号日志展示动作类型：开仓、平仓、移动止损、交易所止盈、交易所止损、滚仓、外部平仓。
- Moss Quant 信号日志额外展示 `profile_id/client_ref/position_id`，方便从机器人追溯到真实仓位。

## 错误处理

- protocol 不可用：扫描可输出 WAIT/诊断，但禁止新开仓；已有仓位的平仓和 SL 更新失败要暴露 `protocol_error`。
- 余额接口失败：禁止新开仓。
- Moss 杠杆读取失败：禁止新开仓。
- 真实仓位多笔：按 Profile 聚合展示，exit/trailing/close 按 `position_id` 逐笔处理。
- 名义价值低于交易所最小值：跳过开仓，记录 `notional_below_min`.
- next-k-api 调 protocol 的任一实仓动作失败：写入信号日志 `status=error`，包含 `source/action/profile_id/position_id/client_ref` 和错误摘要。
- protocol scheduler 同步到交易所 SL/TP 平仓：写入信号日志事件，避免只有 positions closed 而没有策略事件轨迹。
- 清理 Moss 库：只清 next-k-api 的策略/审计数据，不影响 protocol 真实仓位；存在实仓时危险清理应拒绝或明确提示。

## 测试计划

### Protocol

- account summary 在 mock Binance 下返回 USDT wallet、available、unrealized 和 Moss 杠杆。
- Moss ingest 持久化 `profile_id/client_ref` 到 `signals_log` 和 `positions`。
- positions 支持 `source/profile_id/status` 过滤。
- signals 支持 `source/action/status/profile_id` 过滤。
- close、update SL、交易所 SL/TP 同步都会写入信号日志事件。
- close/update SL 按 `position_id` 精确处理 Moss 仓位。
- Moss 下单杠杆使用 `src_moss_quant_leverage`。

### next-k-api

- sizing 使用 protocol 余额、启用 Profile 数量、Profile 风险参数、protocol 杠杆。
- protocol 不可用时不创建新实仓信号。
- protocol 已有 open position 时不重复开仓。
- protocol 已 closed position 时本地旧 open 记录同步为外部平仓。
- Moss summary、signals、latest scan 返回 protocol 实仓聚合数据，并兼容前端现有字段。

### Frontend

- Moss 文案从纸面切到实仓。
- summary、机器人盈亏、扫描摘要、信号表展示 protocol 聚合数据。
- 实盘看板历史持仓按策略分组展示。
- 实盘看板信号日志按策略分组展示，并能看到 next-k-api 发起的 open/close/update SL/rolling 以及交易所 SL/TP 同步事件。
- protocol 不可用时显示错误或降级状态，不把旧纸面数据伪装成实仓。

## 非目标

- 不把 Moss 策略决策整体迁移到 protocol。
- 不删除历史纸面表和纸面历史数据。
- 不改变 ZCT、momentum、jiezhen 的交易流程。
- 不在前端直接拼接 protocol 和 next-k-api 两个服务的数据。
