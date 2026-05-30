# Protocol 当前持仓改为币安实时事实源设计

## 背景

当前 `Next-k-protocol` 同时承担了三类职责：

1. 执行层：接收信号并调用币安下单
2. 本地持仓账本：在 `positions` 表中保存 `open / pending_entry / closed`
3. 生命周期管理器：维护 LIMIT pending、SL/TP 同步、过期强平、历史 PnL

这导致两个问题：

- “当前持仓”接口的事实源是本地 `positions` 表，而不是币安实时持仓
- `signals_log`、`positions`、scheduler、PnL 统计彼此耦合，系统边界偏重

本次改造目标是把 `protocol` 收敛成轻量执行网关：

- 当前持仓直接读取币安实时数据
- 不再维护本地持仓历史账本
- `signals_log` 只作为执行日志
- 配置只保留全局配置，不再区分策略

## 目标

### 必须达成

1. `GET /api/binance/positions` 返回币安当前实时持仓，不再读本地 `positions` 表
2. `protocol` 不再保存本地持仓历史，不再依赖 `positions` 作为事实源
3. `signals_log` 只记录请求与执行结果，不再承担持仓、历史、策略归属等职责
4. 交易配置只保留全局配置，不再区分 `src_*`、`play*`、`moss_quant_*`
5. 删除本地生命周期管理：
   - LIMIT pending 对账
   - LIMIT 超时撤单
   - 自动补 SL/TP 生命周期
   - 过期强平
   - 本地历史 PnL 汇总
6. 删除全局 `margin_usdt` 配置，改为调用方每次请求显式传 `margin_usdt`

### 明确不做

1. 不为当前持仓匹配策略元数据
2. 不保留历史持仓查询能力
3. 不保留策略级配置回退逻辑
4. 不让 `protocol` 负责仓位 sizing 决策

## 设计结论

### 职责边界

改造后的 `protocol` 只承担四类职责：

1. 全局配置管理
2. 执行日志记录
3. 币安账户摘要读取
4. 币安当前持仓查询与统一下单入口

它不再是“本地持仓账本”，而是“执行网关 + 实时查询代理”。

### 数据库最终状态

本地 SQLite 最终只保留两张表：

1. `config`
2. `signals_log`

`positions` 表删除。

迁移时采用两步：

1. 代码先停止读写 `positions`
2. 所有调用方完成切换后，再执行删表迁移

这样能避免结构先删导致老路径直接崩溃。

### 配置模型

保留的全局配置：

- `enabled`
- `testnet`
- `binance_api_key`
- `binance_api_secret`
- `leverage`
- `entry_type`
- `max_positions`

删除的配置：

- `margin_usdt`
- `limit_entry_timeout_sec`
- 所有 `src_*`
- 所有 `play*`
- 所有 expire 配置
- 所有 Moss / Momentum / Jiezhen / ZCT 独立配置

### 请求模型

`POST /api/binance/signals/ingest` 的单条请求统一为执行请求，不再带策略归属语义。

建议保留字段：

- `api_signal_id`
- `symbol`
- `side`
- `margin_usdt`
- `entry_price`
- `sl_price`
- `tp_price`
- `client_ref`

字段规则：

- `margin_usdt` 必填，且必须大于 0
- `entry_price`
  - `MARKET` 时可为空
  - `LIMIT` 时必填
- `sl_price / tp_price` 暂时保留为可选字段，用于当前执行入口兼容

删除字段：

- `source`
- `play`
- `profile_id`
- `notional_usdt`
- 其他所有策略特有字段

### 仓位计算口径

调用方负责决定保证金。

`protocol` 只读取全局 `leverage` 并做执行换算：

- `notional_usdt = margin_usdt * leverage`
- `quantity = notional_usdt / price`

这样 `protocol` 不再承担 sizing 决策职责。

### LIMIT 语义

全局 `entry_type=LIMIT` 时，`protocol` 只负责向币安提交限价单，不再管理其生命周期。

明确删除的能力：

- pending_entry 状态
- 限价超时撤单
- 限价成交后自动补 SL/TP
- 限价挂单对账

因此 `limit_entry_timeout_sec` 配置不再有意义，应删除。

### signals_log 语义

`signals_log` 只保留审计职责。

建议状态收敛为：

- `received`
- `submitted`：LIMIT 挂单提交成功
- `traded`：MARKET 成交成功
- `error`
- `skipped_disabled`
- `skipped_max_positions`
- `duplicate`

`payload_json` 记录原始请求。

`result_json` 记录执行结果，重点保留：

- `orderId`
- `type`
- `status`
- `avgPrice`
- `executedQty`
- 错误码
- 错误消息

### 当前持仓接口

`GET /api/binance/positions` 改成直接读取币安 `/fapi/v2/positionRisk`。

返回时过滤：

- `positionAmt != 0`

统一映射出的字段建议包含：

- `symbol`
- `side`
- `quantity`
- `entry_price`
- `mark_price`
- `unrealized_pnl_usdt`
- `leverage`
- `liquidation_price`
- `margin_type`

明确不再返回：

- `source`
- `profile_id`
- `play`
- `client_ref`
- 本地 `status`
- 本地 `position_id`

`status` 参数不再承担历史筛选语义。

建议兼容策略：

- `status=open`：正常返回实时持仓
- 不传 `status`：等同于 `open`
- `status=closed/pending_entry/cancelled_pending`：返回 410 或空列表并给出明确错误说明

推荐返回 410，清楚表达“该能力已下线”。

### 账户摘要接口

`GET /api/binance/account/summary` 继续保留，直接返回币安账户摘要：

- `wallet_balance_usdt`
- `available_balance_usdt`
- `unrealized_pnl_usdt`

不再附带策略级配置摘要。

### 删除的接口

建议删除或下线：

1. `GET /api/binance/positions/{position_id}`
2. `PUT /api/binance/positions/{id}/sl`
3. 所有 `/api/binance/pnl/*`

`POST /api/binance/positions/close` 有两种路径：

- 如果希望 `protocol` 继续支持手动平仓，则重定义为基于 `symbol + side` 的即时平仓动作
- 如果希望 `protocol` 进一步纯化，则直接删除

本次设计默认建议删除，避免继续维持“持仓生命周期服务”的心智模型。

## 内部模块改造

### 删除的模块职责

以下逻辑整体删除：

- `positions_repo` 及其 facade
- `lifecycle.sync`
- `lifecycle.reconcile`
- `lifecycle.expire`
- 基于 `positions` 的 PnL 汇总
- 基于本地 `position_id` 的 SL 更新

### scheduler

删除以下任务：

- `sync_open_positions`
- `reconcile_pending_entries`
- `expire_open_positions`

`scheduler.py` 可以删除，或保留一个空壳并在启动时不注册任何交易生命周期任务。

推荐直接删除调度器注册，降低误解。

### 执行路径

`signals/ingest` 处理流程收敛为：

1. 鉴权
2. 校验请求字段
3. 去重
4. 检查全局交易开关
5. 实时查询币安当前持仓数量，校验 `max_positions`
6. 读取全局 `leverage`
7. 计算 `notional_usdt`
8. 按全局 `entry_type` 执行 MARKET 或 LIMIT
9. 写入 `signals_log`

MARKET：

- 成功则写 `traded`

LIMIT：

- 成功提交挂单则写 `submitted`
- 不再跟踪后续是否成交

## 兼容性与调用方影响

### 对 next-k-frontend 的影响

必须同步调整：

1. 历史持仓区块下线
2. PnL 汇总区块下线或标记“不再由 protocol 提供”
3. 当前持仓列表改成适配币安实时持仓字段
4. 所有依赖本地 `position_id / source / profile_id` 的 UI 删除

### 对 next-k-api 的影响

必须同步调整：

1. 不再依赖 protocol 历史持仓
2. 不再依赖持仓中的策略元数据
3. 调用下单接口时必须显式传 `margin_usdt`
4. 如果仍需要策略级历史表现，必须从 `next-k-api` 自己的数据源承担，而不是从 protocol 读取

## 迁移顺序

推荐按以下顺序实施：

1. `protocol` 增加新的实时 `positions` 返回逻辑
2. `protocol` 调整 `signals/ingest`，改为必传 `margin_usdt`
3. `next-k-api` 改造调用方，显式传 `margin_usdt`
4. `next-k-frontend` 下线历史持仓与 protocol PnL 依赖
5. `protocol` 删除 position-id 相关接口与 `/pnl/*`
6. `protocol` 停止读写 `positions`
7. 删除 scheduler 生命周期逻辑
8. 最后执行 `positions` 删表迁移与无用配置清理

## 风险与约束

### 主要风险

1. 前端或 `next-k-api` 仍假设 protocol 提供历史持仓
2. 现有手动平仓、SL 更新能力依赖本地 `position_id`
3. LIMIT 语义变化后，调用方可能误以为 protocol 还会自动管理挂单

### 约束结论

因此本次改造必须同时满足两点：

1. API 文档与前端文案同步修改
2. 对 `closed/pending_entry` 等下线能力返回明确错误，而不是静默伪造旧数据

## 测试策略

### Protocol

新增或更新测试应覆盖：

1. `GET /positions` 直接使用币安实时持仓
2. `GET /positions?status=closed` 返回下线错误
3. `signals/ingest` 缺少 `margin_usdt` 时报错
4. `MARKET` 请求成功写入 `signals_log.status=traded`
5. `LIMIT` 请求成功写入 `signals_log.status=submitted`
6. `max_positions` 依据币安当前持仓数量判断

### 联调

联调时应验证：

1. 前端当前持仓与币安页面一致
2. 前端不会再展示 protocol 历史持仓
3. `next-k-api` 所有调用都显式传入 `margin_usdt`

## 推荐实现范围

这次改造建议视为单独架构收敛任务，范围仅限：

- `Next-k-protocol`
- `next-k-frontend` 中依赖 protocol 持仓/PnL 的页面
- `next-k-api` 中调用 protocol 的下单入口

不包含策略本身的 alpha 逻辑调整。
