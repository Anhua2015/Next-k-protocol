# Next K Protocol

币安合约实盘交易执行层。基于 FastAPI 构建，接收来自 next-k-api 的交易信号并通过币安 REST API 执行开仓/平仓/保护单操作。Protocol 不做任何策略逻辑 —— 纯受信执行层。

**端口**: 8001 | **语言**: Python 3.11+ | **数据库**: SQLite (binance.db) | **部署**: Railway / Docker

---

## 目录

- [架构概览](#架构概览)
- [启动流程](#启动流程)
- [环境变量配置](#环境变量配置)
- [API 路由参考](#api-路由参考)
- [数据模型 Pydantic](#数据模型-pydantic)
- [信号摄入流水线](#信号摄入流水线)
- [交易执行引擎](#交易执行引擎)
- [数据库设计](#数据库设计)
- [币安客户端](#币安客户端)
- [风控机制](#风控机制)
- [可观测性](#可观测性)
- [异常体系](#异常体系)
- [测试体系](#测试体系)
- [信号源配置](#信号源配置)
- [部署](#部署)
- [依赖清单](#依赖清单)
- [开发命令速查](#开发命令速查)

---

## 架构概览

```
next-k-api (扫描/信号) → HTTP POST /api/binance/signals/ingest → Protocol (执行层)
                                                                      │
                                                                      ▼
                                                               Binance Futures REST
```

### 模块职责

| 模块 | 职责 |
|------|------|
| `main.py` | FastAPI 应用入口，lifespan 管理生命周期 |
| `router.py` | `/api/binance/*` 路由定义，健康检查/状态/配置/信号摄入/信号日志/持仓查询 |
| `models.py` | Pydantic 请求/响应模型，Swagger 自动文档 |
| `ingest/pipeline.py` | 信号批量摄入编排，循环处理每条信号 |
| `ingest/guards.py` | 守卫链：api_signal_id 去重 |
| `ingest/dispatcher.py` | 信号分发：将守卫通过的信号转交 trader 执行 |
| `trader.py` | 交易编排层：配置检查、杠杆/保证金设置、分派 MARKET/LIMIT/平仓 |
| `trading/market_entry.py` | MARKET 市价单入场 + SL/TP 保护单 |
| `trading/limit_entry.py` | LIMIT 限价单入场 |
| `trading/protective.py` | SL/TP 条件单构建 + 紧急平仓 |
| `trading/pricing.py` | 价格/数量精度取整工具（重导出） |
| `binance/client.py` | 币安 HTTP 客户端：签名、时间同步、重试/退避 |
| `binance/signing.py` | HMAC-SHA256 签名 |
| `binance/time_sync.py` | 服务器时间偏移维护 |
| `binance/account.py` | 账户操作：hedge 模式检测、杠杆/保证金设置、持仓查询 |
| `binance/orders.py` | 订单操作：下单、条件单、撤单、查询 |
| `binance/exchange_info.py` | exchangeInfo 缓存（300s TTL），mark price、filters、精度取整 |
| `repos/connection.py` | SQLite 连接管理、DDL 建表、WAL 模式、RLock 写锁 |
| `repos/config_repo.py` | config 表 KV 读写 |
| `repos/signals_repo.py` | signals_log 表 CRUD |
| `observability/logging_setup.py` | structlog 结构化日志配置 |
| `observability/metrics.py` | Prometheus 指标定义 |
| `observability/alerts.py` | Webhook 告警（支持 telegram/dingtalk/raw_json） |
| `routers/metrics.py` | `/metrics` Prometheus 端点 |
| `common/exceptions.py` | 业务异常层次体系 |
| `env_loader.py` | `.env.oi` 加载（setdefault 不覆盖已有环境变量） |

---

## 启动流程

### main.py 生命周期

```
1. configure_logging()         → 配置 structlog（根据 PROTOCOL_LOG_FORMAT 选择 text/json 格式）
2. load_env_oi()               → 加载 .env.oi 环境变量（setdefault，不覆盖已设置的环境变量）
3. FastAPI lifespan startup:
   a. db.init_db()             → 创建/迁移 SQLite 表结构
   b. db.apply_env_config_overrides() → 环境变量覆盖 DB config（BINANCE_ENABLED）
   c. init_client()            → 初始化 BinanceClient 单例
4. CORS 中间件                  → allow_origins=["*"]
5. 注册路由                     → router（/api/binance/*）+ metrics_router（/metrics）
6. lifespan shutdown:
   → BinanceClient.close()     → 关闭 httpx 连接池
```

### 启动命令

```bash
# 直接启动
uvicorn main:app --host 0.0.0.0 --port 8001 --reload

# 使用启动脚本（含虚拟环境管理）
./start.sh

# 停止
./stop.sh
```

### start.sh 启动脚本流程

1. 检测 Python 3.11+
2. 创建/复用虚拟环境 `.venv`
3. 安装 `requirements.txt` 依赖
4. 从 `.env.oi` 加载环境变量（setdefault）
5. `nohup` 后台启动 uvicorn，PID 写入 `.pid/api.pid`
6. 轮询 `/api/binance/health` 最多 30s 等待就绪

### stop.sh 停止脚本流程

1. 读取 PID 文件，发送 SIGTERM
2. 等待最多 15s 优雅退出
3. 超时则发送 SIGKILL
4. 清理 PID 文件

---

## 环境变量配置

所有环境变量通过 `.env.oi` 文件或系统环境变量设置。

### 核心配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `8001` | 服务端口 |
| `BINANCE_API_KEY` | （空） | 币安 API Key（必填） |
| `BINANCE_API_SECRET` | （空） | 币安 API Secret（必填） |
| `BINANCE_TESTNET` | `false` | 测试网开关。`true` = testnet.binancefuture.com, `false` = fapi.binance.com |
| `BINANCE_ENABLED` | `false` | 全局交易开关。启动时写入 DB config 表 |
| `DATA_DIR` | `.`(当前目录) | SQLite 数据库文件目录，Railway 挂载 Volume 时通常为 `/data` |

### CORS 配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROTOCOL_CORS_ORIGINS` | `localhost` 系列 | CORS 白名单，逗号分隔。留空则默认允许 localhost:8000/8001/5173/5500。生产环境必须显式配置 |

### 日志配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROTOCOL_LOG_FORMAT` | `text` | 日志格式。`text` = ConsoleRenderer（开发友好），`json` = JSONRenderer（生产） |
| `PROTOCOL_LOG_LEVEL` | `INFO` | 日志级别。可选：DEBUG, INFO, WARNING, ERROR, CRITICAL |

### 指标与告警配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROTOCOL_METRICS_TOKEN` | （空） | Prometheus 指标端点鉴权 token |
| `PROTOCOL_ALERT_WEBHOOK_URL` | （空） | 告警 Webhook URL。不设置则仅记录日志，不发送告警 |
| `PROTOCOL_ALERT_TEMPLATE` | `raw_json` | 告警消息模板。可选：`raw_json`, `telegram`, `dingtalk` |
| `PROTOCOL_ALERT_DEDUP_COOLDOWN_SEC` | `300` | 告警去重冷却时间（秒）。相同 dedup_key 在此时间内不重复发送 |

### 启动时环境变量覆盖 DB 配置

`apply_env_config_overrides()` 在 startup 时执行，允许通过环境变量强制覆盖数据库中已有的配置值：

| 环境变量 | 对应 DB key | 说明 |
|----------|------------|------|
| `BINANCE_ENABLED` | `enabled` | 覆盖交易开关（支持 1/0/true/false/yes/no/on/off） |

---

## API 路由参考

所有接口前缀：`/api/binance`

### 1. 健康检查

```
GET /api/binance/health
```

**说明**：公开端点，无需鉴权。Railway 部署健康检查 / 负载均衡存活探针。

**响应**：
```json
{
  "status": "ok",
  "module": "next-k-protocol",
  "version": "1.0.0",
  "db": "ok"
}
```

status 可能值：`ok`（正常），`degraded`（DB 连接失败）

---

### 2. 服务状态

```
GET /api/binance/status
```

**响应模型**：`StatusOut`

**响应**：
```json
{
  "enabled": "true",
  "testnet": "false",
  "open_positions": 3,
  "max_positions": "8",
  "api_key_set": true,
  "db_path": "/data/binance.db"
}
```

---

### 3. 读取配置

```
GET /api/binance/config
```

**说明**：返回所有可通过 API 管理的配置。币安 API Key/Secret 不会出现在返回中。

**响应**：`Dict[str, str]` 键值对

---

### 4. 更新配置

```
POST /api/binance/config
```

**请求模型**：`ConfigUpdate`

**说明**：批量更新一个或多个配置项。`binance_api_key` 和 `binance_api_secret` 被阻止，仅支持通过环境变量配置。

**请求示例**：
```json
{
  "pairs": {
    "enabled": "true",
    "entry_type": "MARKET",
    "max_positions": "8"
  }
}
```

**响应**：
```json
{
  "ok": true,
  "updated": ["enabled", "entry_type", "max_positions"]
}
```

---

### 5. 账户摘要

```
GET /api/binance/account/summary
```

**响应模型**：`AccountSummaryOut`

**说明**：直接查询币安合约账户 USDT 余额。失败返回 502。

**响应**：
```json
{
  "asset": "USDT",
  "wallet_balance_usdt": 10500.50,
  "available_balance_usdt": 8200.30,
  "unrealized_pnl_usdt": -25.75
}
```

---

### 6. 信号摄入（核心）

```
POST /api/binance/signals/ingest
```

**请求模型**：`SignalIngestRequest`
**响应模型**：`SignalIngestResult`

**说明**：由 next-k-api 调用，批量推送交易信号。Protocol 仅做去重 + 转发币安，不做策略判断。SL/TP、仓位数量均由 next-k-api 在信号中算好。

**处理流程**：
1. 获取 `_db_write_lock`（RLock）串行化写入
2. 逐条信号：去重检查（UNIQUE(source, api_signal_id)） -> 分发执行
3. 聚合结果返回

**请求示例**：
```json
{
  "signals": [
    {
      "source": "orb",
      "api_signal_id": "orb-BTCUSDT-20260617-1",
      "symbol": "BTCUSDT",
      "side": "LONG",
      "margin_usdt": 1000.0,
      "leverage": 5.0,
      "entry_price": 67250.5,
      "sl_price": 66500.0,
      "tp_price": 68500.0,
      "confidence": "high",
      "regime": "TREND_UP",
      "profile_id": 1,
      "action": "open"
    }
  ]
}
```

**响应**：
```json
{
  "scanned": 1,
  "traded": 1,
  "skipped": 0,
  "errors": 0,
  "details": [
    {
      "api_signal_id": "orb-BTCUSDT-20260617-1",
      "symbol": "BTCUSDT",
      "side": "LONG",
      "source": "orb",
      "action": "traded"
    }
  ]
}
```

**action 可能值**：`traded`（已执行），`duplicate`（重复），`error`（失败）

**限制**：signals 数组长度 0-100

---

### 7. 信号日志查询

```
GET /api/binance/signals?limit=100&offset=0&source=&action=&status=&profile_id=
```

**响应模型**：`List[SignalLogOut]`

**查询参数**：

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `limit` | int | 100 | 每页条数（1-1000） |
| `offset` | int | 0 | 分页偏移量 |
| `source` | str | 无 | 按信号来源过滤，如 `orb` |
| `action` | str | 无 | 按动作过滤：`open` / `rolling` / `close` |
| `status` | str | 无 | 按状态过滤：`traded` / `received` / `error` / `duplicate` |
| `profile_id` | int | 无 | 按策略 profile 过滤 |

**响应**：按 `id DESC` 倒序排列的信号日志数组，每条记录包含 20 个字段的完整审计信息。

---

### 8. 持仓查询

```
GET /api/binance/positions?status=open&limit=100&offset=0
```

**响应模型**：`List[LivePositionOut]`

**说明**：直接查询币安当前非零持仓。`status` 参数仅支持 `open`（历史持仓已下线，传其他值返回 410）。

---

### 9. Prometheus 指标

```
GET /metrics
```

**说明**：Prometheus 标准格式指标端点。通过 `PROTOCOL_METRICS_TOKEN` 可配置基础鉴权。返回包括 counters、histograms、gauges 三类指标（详见[可观测性](#可观测性)）。

---

## 数据模型 Pydantic

### SignalItem -- 单条交易信号

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `source` | str | 否 | 信号来源标识，默认 `"orb"` |
| `api_signal_id` | str | 是 | 调用方生成的唯一 ID，用于去重 |
| `symbol` | str | 是 | 交易对符号，如 `"BTCUSDT"` |
| `side` | str | 是 | 方向，仅允许 `"LONG"` 或 `"SHORT"` |
| `margin_usdt` | float | 否 | 保证金（USDT），开仓必填 |
| `leverage` | float | 否 | 杠杆倍数，开仓必填 |
| `entry_price` | float | 否 | 建议入场价（信号触发价） |
| `sl_price` | float | 否 | 止损价 |
| `tp_price` | float | 否 | 止盈价（由 next-k-api 计算） |
| `close_price` | float | 否 | 建议平仓价；有值时 LIMIT 减仓，否则 MARKET |
| `confidence` | str | 否 | 置信度标签 `"high"` / `"medium"` / `"low"` |
| `regime` | str | 否 | 市场状态标记 `"TREND_UP"` / `"RANGE"` 等 |
| `play` | str | 否 | 策略子类型标记 |
| `profile_id` | int | 否 | 调用方 profile 标识 |
| `client_ref` | str | 否 | 调用方动作引用 ID，用于回填排查 |
| `action` | str | 否 | 动作类型：`"open"`（默认） / `"rolling"`（滚仓） / `"close"`（平仓） |

### SignalIngestRequest

| 字段 | 类型 | 说明 |
|------|------|------|
| `signals` | `List[SignalItem]` | 待处理信号列表，长度 0-100 |

### SignalIngestResult

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `scanned` | int | 0 | 本次推送的信号总数 |
| `traded` | int | 0 | 成功执行的信号数 |
| `skipped` | int | 0 | 跳过的信号数（重复） |
| `errors` | int | 0 | 处理失败的信号数 |
| `details` | `List[Dict]` | [] | 每条信号的处理详情 |

### LivePositionOut

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbol` | str | 交易对 |
| `side` | str | 方向：LONG / SHORT |
| `quantity` | float | 持仓数量（绝对值） |
| `entry_price` | float | 开仓均价 |
| `mark_price` | float | 当前标记价 |
| `unrealized_pnl_usdt` | float | 未实现盈亏 |
| `leverage` | int | 杠杆倍数 |
| `liquidation_price` | float | 预估强平价 |
| `margin_type` | str | 保证金模式（ISOLATED / CROSSED） |

### SignalLogOut

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 主键 |
| `source` | str | 信号来源 |
| `api_signal_id` | str | 去重 ID |
| `symbol` | str | 交易对 |
| `side` | str | 方向 |
| `entry_price` | float | 入场价 |
| `sl_price` | float | 止损价 |
| `tp_price` | float | 止盈价 |
| `confidence` | str | 置信度 |
| `regime` | str | 市场状态 |
| `notional_usdt` | float | 名义价值 |
| `received_at` | str | 接收时间（UTC ISO8601） |
| `status` | str | 状态：`traded` / `received` / `error` / `duplicate` 等 |
| `skip_reason` | str | 跳过/失败原因 |
| `play` | str | 策略子类型 |
| `profile_id` | int | profile 标识 |
| `client_ref` | str | 动作引用 ID |
| `action` | str | 动作类型 |
| `position_id` | int | 关联持仓 ID |
| `payload_json` | str | 信号请求快照 JSON |
| `result_json` | str | 执行结果快照 JSON |

### AccountSummaryOut

| 字段 | 类型 | 说明 |
|------|------|------|
| `asset` | str | 账户资产，固定 "USDT" |
| `wallet_balance_usdt` | float | 钱包余额 |
| `available_balance_usdt` | float | 可用余额 |
| `unrealized_pnl_usdt` | float | 未实现盈亏 |

### StatusOut

| 字段 | 类型 | 说明 |
|------|------|------|
| `enabled` | str | 交易开关 `"true"` / `"false"` |
| `testnet` | str | 测试网 `"true"` / `"false"` |
| `open_positions` | int | 当前持仓数 |
| `max_positions` | str | 最大持仓数配置 |
| `api_key_set` | bool | API Key 是否配置 |
| `db_path` | str | 数据库路径 |

### ConfigUpdate

| 字段 | 类型 | 说明 |
|------|------|------|
| `pairs` | `Dict[str, str]` | 配置键值对。`binance_api_key` / `binance_api_secret` 不可通过此接口修改 |

---

## 信号摄入流水线

### 整体流程

```
POST /api/binance/signals/ingest
    │
    ▼
router.ingest_signals()
    │  获取 _db_write_lock（RLock 串行化）
    ▼
pipeline.process_signal_batch(signals, db)
    │  逐条遍历 signal
    ├── SIGNALS_RECEIVED counter +1
    ▼
guards.guard_dedup_insert()
    │  INSERT INTO signals_log（UNIQUE(source, api_signal_id)）
    │  返回 None -> duplicate，跳过
    │  返回 id   -> 通过，记录 signal_log_id
    ▼
dispatcher.dispatch(sig, signal_log_id)
    │  将 Pydantic 模型转为 dict
    │  调用 trader.execute_trade(signal_dict)
    ▼
聚合 SignalIngestResult
    │  scanned / traded / skipped / errors + details
```

### 守卫链

```
GUARDS = [guard_dedup_insert]
```

当前仅保留 `guard_dedup_insert` -- 唯一去重守卫。Protocol 不做策略侧限制（不做持仓数限制、置信度过滤等，这些由 next-k-api 负责）。

**去重机制**：signals_log 表 `UNIQUE(source, api_signal_id)` 约束。insert 时若违反唯一约束返回 None -> 标记为 `duplicate` 跳过。

### 写入锁

信号摄入在 `_db_write_lock`（threading.RLock）保护下执行。整个批次共享同一把锁，避免并发信号导致重复插入。

---

## 交易执行引擎

### execute_trade() 编排

```
trader.execute_trade(signal_dict)
    │
    ├── 检查 enabled 配置 -> false 则返回 error "trading_disabled"
    │
    ├── action == "close" -> _close_live_position()
    │
    ├── 解析 margin_usdt / leverage
    ├── 验证 SL/TP 可解析
    ├── 确定 entry_type（MARKET/LIMIT），rolling 强制 MARKET
    │
    ├── 交易前设置：
    │   ├── get_filters(symbol) -> step_size, tick_size, min_notional
    │   ├── set_margin_type(symbol) -> ISOLATED
    │   ├── set_leverage(symbol, leverage)
    │   ├── detect_hedge_mode()
    │   └── get_mark_price(symbol)
    │
    └── dispatch：
        ├── MARKET -> market_entry.open_market()
        └── LIMIT  -> limit_entry.open_limit()
```

### MARKET 市价入场（market_entry.py）

```
open_market()
    │
    ├── 1. 计算数量：qty = margin * leverage / mark_price -> 精度取整
    ├── 2. 验证最小名义价值：qty * mark_price >= min_notional
    ├── 3. 下单 MARKET 市价单
    ├── 4. 获取实际成交价（avgPrice / fallback mark_px）
    │
    ├── 5. 下 SL 保护单：STOP_MARKET, workingType=MARK_PRICE
    ├── 6. 下 TP 保护单：TAKE_PROFIT_MARKET, workingType=MARK_PRICE
    │
    └── SL/TP 下单失败 -> 紧急 MARKET 平仓
         ├── cancel_all_orders(symbol)
         └── _emergency_close() -> MARKET reduceOnly=true
```

**Hedge 模式处理**：若检测到 hedge 模式，入场单和 SL/TP 单都带 `positionSide`（LONG/SHORT）且不使用 `reduceOnly`。

### LIMIT 限价入场（limit_entry.py）

```
open_limit()
    │
    ├── 1. 验证 entry_price 存在 > 0
    ├── 2. 计算数量：qty = margin * leverage / limit_price -> 精度取整
    ├── 3. 验证最小名义价值
    ├── 4. 下单 GTC LIMIT 限价单（newOrderRespType="ACK"）
    │
    └── 结果 status = "submitted"
         注意：LIMIT 入场不下 SL/TP，需后续监控成交后补保护单
```

### 平仓（_close_live_position）

```
_close_live_position()
    │
    ├── 1. get_live_position(symbol) -> 查找非零持仓
    ├── 2. 检查 direction 匹配（signal side vs actual side）
    ├── 3. detect_hedge_mode() -> position_side
    ├── 4. cancel_all_orders(symbol) -> 先清理挂单
    │
    ├── 5. 平仓：
    │   ├── 有 close_price -> LIMIT GTC 限价平仓
    │   └── 无 close_price -> MARKET reduceOnly 市价平仓
    │
    └── update_signal_status("traded")
```

### 保护单（protective.py）

**SL 条件单**：`STOP_MARKET`，triggerPrice 由信号指定，`workingType=MARK_PRICE`

**TP 条件单**：`TAKE_PROFIT_MARKET`，triggerPrice 由信号指定，`workingType=MARK_PRICE`

**参数构建**（`build_protective_params`）：

| 参数 | SL 值 | TP 值 |
|------|-------|-------|
| `algoType` | `"CONDITIONAL"` | `"CONDITIONAL"` |
| `type` | `"STOP_MARKET"` | `"TAKE_PROFIT_MARKET"` |
| `workingType` | `"MARK_PRICE"` | `"MARK_PRICE"` |
| `priceProtect` | `"false"` | `"false"` |

Hedge 模式：使用 `positionSide`（LONG/SHORT）；One-way 模式：使用 `reduceOnly="true"`。

**SL 距离验证**（`validate_sl_distance`）：
- 计算安全边距：`max(tick_size * 2, mark_price * 0.0005)`
- LONG：`sl_price < mark_price - margin`
- SHORT：`sl_price > mark_price + margin`
- 仅警告，不拒绝交易

**TP 距离验证**（`validate_tp_distance`）：
- LONG：`tp_price > mark_price + margin`
- SHORT：`tp_price < mark_price - margin`
- 仅警告，不拒绝交易

### 紧急平仓

触发条件：SL 或 TP 保护单下单失败时立即执行。

```
emergency_close(symbol, side, qty, position_side)
    │
    ├── MARKET 市价单，reduceOnly=true
    ├── 成功：记录 ERROR 级别日志
    └── 失败：记录 CRITICAL 级别日志 -> 仓位裸露（naked）
              -> EMERGENCY_CLOSE_FAILED counter +1
```

**保护单取消失败处理**：重试最多 3 次，间隔 0.2s。仍有残留则抛出 RuntimeError。

### 鉴权失败自动禁用

由 trader.py 维护 `_SYNC_AUTH_FAIL_COUNT`，受 `threading.Lock` 保护。连续 20 次鉴权失败 -> 自动调用 `set_config("enabled", "false")` -> 记录 CRITICAL 日志 + `TRADING_DISABLED_AUTO` counter。

---

## 数据库设计

### 概述

- **文件**：`binance.db`（路径由 `DATA_DIR` 环境变量决定）
- **模式**：SQLite WAL（Write-Ahead Logging）
- **并发**：写操作受 `threading.RLock` 保护
- **迁移**：在 `init_db()` 中通过 try-except ALTER TABLE 实现向前兼容

### config 表

```sql
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
```

**默认条目**：

| key | 默认值 | 说明 |
|-----|--------|------|
| `enabled` | `"false"` | 交易开关 |
| `testnet` | `"false"` | 测试网开关 |
| `entry_type` | `"MARKET"` | 入场方式 |
| `max_positions` | `"8"` | 最大持仓数（历史配置项，ingest 不再据此拦截） |

### signals_log 表

```sql
CREATE TABLE IF NOT EXISTS signals_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT    NOT NULL DEFAULT '',
    api_signal_id  TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    side           TEXT    NOT NULL,
    entry_price    REAL,
    sl_price       REAL,
    tp_price       REAL,
    confidence     TEXT,
    regime         TEXT,
    notional_usdt  REAL,
    received_at    TEXT    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'received',
    skip_reason    TEXT,
    play           TEXT    DEFAULT '',
    profile_id     INTEGER,
    client_ref     TEXT    DEFAULT '',
    action         TEXT    DEFAULT 'open',
    position_id    INTEGER,
    payload_json   TEXT,
    result_json    TEXT,
    UNIQUE(source, api_signal_id)
);
```

**索引**：

| 索引 | 用途 |
|------|------|
| `idx_signals_log_dedup`（source, api_signal_id） | 去重查询加速 |
| `idx_signals_log_status`（status） | 按状态过滤 |
| `idx_signals_log_source_action`（source, action, status） | 按来源+动作+状态组合查询 |
| `idx_signals_log_profile`（source, profile_id） | 按 profile 追踪 |

### Repository 模式

遵循 Repository 模式，业务逻辑通过抽象接口操作数据：

| 接口 | 方法 | 说明 |
|------|------|------|
| `repos/config_repo` | `get_config(key, default)` | 读取单个配置 |
| | `get_all_config()` | 读取全部配置 |
| | `set_config(key, value)` | 设置单个配置（UPSERT） |
| | `set_config_batch(pairs)` | 批量设置配置 |
| | `apply_env_config_overrides()` | 环境变量覆盖 |
| `repos/signals_repo` | `insert_signal(...)` | 插入信号记录（返回 ID 或 None=重复） |
| | `update_status(signal_id, status, reason)` | 更新状态 |
| | `update_execution(signal_id, ...)` | 更新执行结果（含 payload/result JSON） |
| | `list_signals(limit, offset, filters)` | 分页查询信号日志 |

db.py 作为 facade 重导出 repos/ 的所有函数，保持向后兼容。

---

## 币安客户端

### BinanceClient

定制的币安合约 REST 客户端，通过依赖注入获取 key/secret/base_url，不直接依赖 DB。

**核心特性**：

| 特性 | 实现 |
|------|------|
| HTTP 库 | `httpx.Client` + `httpx.Timeout` |
| 签名 | HMAC-SHA256（binance/signing.py） |
| 时间同步 | 600s 自动重同步，-1021 timeskew 即时修正 |
| 重试机制 | 最多 3 次，指数退避（0.5s * 2^attempt） |
| 连接池 | max_connections=20, keepalive=10 |

**重试触发条件**：
- HTTP 状态码：429, 418, 500, 502, 503, 504
- 币安错误码：-1003, -1004
- 网络错误：`httpx.RequestError`
- 时间偏移：-1021（先重同步时间再重试，不计入 retry 次数）

**类结构**：

```
BinanceClient
    ├── _base_url_fn()   -> testnet 或 mainnet URL（通过闭包从 DB 读取 testnet 配置）
    ├── _api_key_fn()    -> API Key（从环境变量读取）
    ├── _secret_fn()     -> API Secret（从环境变量读取）
    ├── _http            -> httpx.Client（主要请求，timeout=10s）
    ├── _http_sync       -> httpx.Client（时间同步专用，timeout=5s）
    │
    ├── _ts()            -> 获取 server_timestamp_ms()（必要时自动同步）
    ├── _sign(params)    -> 对参数签名
    ├── _headers()       -> 构建含 X-MBX-APIKEY 的请求头
    │
    └── request(method, path, params, signed) -> 核心请求方法（含重试逻辑）
```

### 签名流程（signing.py）

```python
def sign(params: Dict[str, Any], secret: str) -> str:
    qs = urllib.parse.urlencode(params)       # key 排序
    return hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
```

### 时间同步（time_sync.py）

- 维护全局 `_offset_ms`（本地与币安服务器时间差）
- 每 600s 自动重新同步（`SERVER_TIME_RESYNC_SEC`）
- 签名请求时自动注入 `timestamp` + `recvWindow=5000ms`
- 收到 -1021 timeskew 错误 -> 立即重同步并重试（不占用 retry 额度）

### 账户模块（account.py）

| 函数 | 说明 |
|------|------|
| `detect_hedge_mode()` | 检测双向持仓模式（缓存结果） |
| `set_leverage(symbol, leverage)` | 设置杠杆（-4028 视为已设置） |
| `set_margin_type(symbol)` | 设为 ISOLATED 逐仓 |
| `get_live_position(symbol)` | 获取单个非零持仓 |
| `list_live_positions()` | 列出所有非零持仓 |
| `get_account_summary()` | USDT 账户摘要 |

### 订单模块（orders.py）

| 函数 | 说明 |
|------|------|
| `place_order(params)` | 下普通订单（MARKET/LIMIT） |
| `place_algo_order(params)` | 下条件单（SL/TP） |
| `get_algo_order(algo_id)` | 查询条件单详情 |
| `cancel_algo_order(algo_id)` | 撤销条件单（4xx 宽容：视为已撤销） |
| `cancel_order_by_id(symbol, order_id)` | 按 ID 撤销普通订单 |
| `get_open_algo_orders(symbol)` | 查询活跃条件单列表 |
| `cancel_all_orders(symbol, pos)` | 撤销所有订单（普通 + 条件单） |

### 交易对信息模块（exchange_info.py）

| 函数 | 说明 |
|------|------|
| `get_symbol_info(symbol)` | 获取交易对信息（缓存 TTL 300s） |
| `get_mark_price(symbol)` | 获取当前标记价格 |
| `get_filters(symbol)` | 返回（stepSize, tickSize, minNotional） |
| `round_quantity(qty, step_size)` | 数量精度取整 |
| `round_price(price, tick_size)` | 价格精度取整 |

---

## 风控机制

Protocol 内置 8 层风控：

### 1. 全局交易开关

- `enabled` config key 控制
- `"false"` 时所有开仓请求被拒绝（status="trading_disabled"）
- 支持运行时通过 `POST /api/binance/config` 切换
- 启动时 `BINANCE_ENABLED` 环境变量覆盖

### 2. 信号去重

- `signals_log` 表 `UNIQUE(source, api_signal_id)` 约束
- 数据库层面防止 HTTP 重试导致重复下单
- insert 返回 None 时标记为 `duplicate` 跳过

### 3. 最小名义价值校验

- MARKET 入场：`qty * mark_price >= min_notional`
- LIMIT 入场：`qty * limit_price >= min_notional`
- 不满足则抛出 `ValueError`，标记 status="error"

### 4. SL 距离验证

- SL 距 mark price 至少 `max(tick_size * 2, mark_price * 0.0005)`
- 验证失败仅 `WARNING` 日志，不拒绝交易（因 mark price 可能波动）

### 5. SL/TP 下单失败 -> 紧急平仓

- MARKET 入场后 SL 或 TP 条件单下单失败 -> 立即 MARKET 紧急平仓
- 防止产生"裸露仓位"（无保护单的持仓）
- 紧急平仓失败 -> CRITICAL 日志 + `EMERGENCY_CLOSE_FAILED` counter

### 6. 鉴权失败自动禁用

- 连续 20 次鉴权失败（HTTP 401 等） -> 自动设置 `enabled=false`
- `AUTH_FAIL` counter 记录每次鉴权失败
- 禁用时 `TRADING_DISABLED_AUTO` counter +1 + CRITICAL 日志
- 需手动通过 `POST /api/binance/config` 重新启用

### 7. 保证金模式强制 ISOLATED

- 每次开仓前 `set_margin_type(symbol)` 确保逐仓模式
- "No need to change margin type" 错误静默忽略

### 8. 写入串行化

- `threading.RLock` 保护信号摄入写入路径
- 整个信号批次共享同一把写锁
- 避免并发信号导致竞态条件

---

## 可观测性

### 结构化日志（structlog）

配置在 `observability/logging_setup.py`：

**text 格式**（PROTOCOL_LOG_FORMAT=text）：
- ConsoleRenderer 彩色输出
- ISO 时间戳，UTC

**json 格式**（PROTOCOL_LOG_FORMAT=json）：
- JSONRenderer 结构化输出
- ISO 时间戳，UTC
- 包含 traceback dict 处理

**日志级别**：通过 `PROTOCOL_LOG_LEVEL` 环境变量设置（默认 INFO）

### Prometheus 指标

端点：`GET /metrics`

#### Counters（12 个）

| 指标名 | 标签 | 说明 |
|--------|------|------|
| `protocol_signals_received_total` | source, play | 信号接收总数 |
| `protocol_signals_skipped_total` | source, code | 信号跳过总数 |
| `protocol_signals_duplicate_total` | source | 重复信号总数 |
| `protocol_trades_opened_total` | source, side, entry_type | 成功开仓数 |
| `protocol_trades_failed_total` | source, stage, code | 交易失败数 |
| `protocol_positions_closed_total` | source, close_reason | 平仓总数 |
| `protocol_emergency_close_failed_total` | symbol | 紧急平仓失败数 |
| `protocol_auth_fail_total` | （无） | 鉴权失败次数 |
| `protocol_trading_disabled_auto_total` | （无） | 自动禁用事件数 |
| `protocol_binance_requests_total` | method, path, status_class | 币安 API 调用数 |
| `protocol_binance_retries_total` | path, reason | 币安请求重试数 |
| `protocol_exchange_info_cache_total` | result | exchangeInfo 缓存命中/未命中 |

#### Histograms（2 个）

| 指标名 | 标签 | buckets | 说明 |
|--------|------|---------|------|
| `protocol_binance_request_seconds` | method, path | .05, .1, .25, .5, 1, 2, 5 | 币安请求延迟 |
| `protocol_trade_open_seconds` | entry_type | .1, .5, 1, 2, 5 | 开仓耗时 |

#### Gauges（3 个）

| 指标名 | 标签 | 说明 |
|--------|------|------|
| `protocol_open_positions` | source | 当前持仓数 |
| `protocol_pending_positions` | （无） | 待成交挂单数 |
| `protocol_trading_enabled` | （无） | 交易启用状态（1/0） |

### Webhook 告警

配置在 `observability/alerts.py`：

**触发方式**：通过 `send_alert(level, event, body, dedup_key)` 调用

**告警模板**（PROTOCOL_ALERT_TEMPLATE）：

| 模板 | 输出格式 |
|------|---------|
| `raw_json` | `{"level", "event", "body", "timestamp"}` |
| `telegram` | `{"chat_id", "text": "[LEVEL] event\nbody"}` |
| `dingtalk` | `{"msgtype": "text", "text": {"content": "..."}}` |

**去重机制**：进程内 dict 缓存，相同 `dedup_key` 在 `PROTOCOL_ALERT_DEDUP_COOLDOWN_SEC`（默认 300s）内不重复发送。

---

## 异常体系

定义在 `common/exceptions.py`：

```
ProtocolError（基类，code="protocol_error"）
├── BinanceHTTPError（code="binance_http"）
│   ├── BinanceAuthError（code="binance_auth"）
│   ├── BinanceRateLimitError（code="binance_rate_limit"，retryable=True）
│   ├── BinanceServerError（code="binance_5xx"，retryable=True）
│   ├── BinanceTimeskewError（code="binance_timeskew"）
│   └── BinanceBusinessError（code="binance_business"，binance_code, msg）
├── ConfigError（code="bad_config"）
├── SignalValidationError（code="bad_signal"）
├── InsufficientNotionalError（code="min_notional"）
├── SLDistanceError（code="sl_too_close"）
└── EmergencyCloseFailedError（code="emergency_close_failed"）
```

所有异常继承自 `ProtocolError`，统一携带 `code` 和 `retryable` 属性。

---

## 测试体系

### 测试框架

| 工具 | 用途 |
|------|------|
| pytest 8.0+ | 测试框架 |
| pytest-cov | 覆盖率 |
| pytest-asyncio | 异步测试 |
| pytest-httpx | HTTP mock（httpx_mock） |
| freezegun | 时间冻结 |
| pyyaml | 测试数据加载 |

### pytest 标记

配置在 `pytest.ini`：

| 标记 | 说明 |
|------|------|
| `characterization` | 黑盒行为锁定测试（Phase 0 baseline） |
| `unit` | 单元测试（Phase 9） |
| `integration` | 集成测试（FastAPI TestClient） |
| `slow` | 耗时 >1s 的测试 |

### 测试目录结构

```
tests/
├── conftest.py                    # 全局 fixtures
├── unit/                          # 单元测试（5 个文件）
│   ├── test_exceptions.py
│   ├── test_execute_trade.py
│   ├── test_guards.py
│   ├── test_pricing.py
│   └── test_signing.py
├── characterization/              # 表征测试（9 个文件）
│   ├── conftest.py
│   ├── test_account_summary.py
│   ├── test_binance_client_retry.py
│   ├── test_execute_limit_entry.py
│   ├── test_execute_market_entry.py
│   ├── test_hedge_mode.py
│   ├── test_ingest_guards.py
│   ├── test_live_positions.py
│   ├── test_protective_failure.py
│   └── test_validation.py
└── fixtures/binance/              # 币安 API 响应 fixture（22 个 JSON 文件）
    ├── cancel_all_orders_success.json
    ├── cancel_order_success.json
    ├── error_1021_timeskew.json
    ├── error_2019_insufficient_margin.json
    ├── error_401_unauthorized.json
    ├── error_429.json
    ├── error_5xx.json
    ├── exchange_info_btcusdt.json
    ├── get_open_algo_orders_sl_filled.json
    ├── get_open_algo_orders_sl_working.json
    ├── get_order_canceled.json
    ├── get_order_filled.json
    ├── get_order_pending.json
    ├── place_algo_order_success.json
    ├── place_order_limit_ack.json
    ├── place_order_market_filled.json
    ├── position_risk_closed.json
    ├── position_risk_open.json
    ├── position_side_dual.json
    ├── position_side_single.json
    ├── premium_index_btcusdt.json
    └── server_time.json
```

### 核心 Fixtures

| fixture | scope | 说明 |
|---------|-------|------|
| `_env_baseline` | auto-use（每个测试） | 隔离 DB 到 tmpdir，设置 testnet=true, test-key/test-secret，清除代理环境变量 |
| `fresh_db` | function | 全新 binance.db 初始化（清除模块缓存） |
| `seeded_config` | function | 预置 trading config + 初始化 BinanceClient，清除缓存 |
| `load_binance_fixture` | function | 加载 binance fixture JSON 返回 dict |

### 运行测试

```bash
# 全部测试
pytest tests/ -v

# 单元测试
pytest tests/unit/ -v

# 表征测试
pytest tests/characterization/ -v

# 按名称筛选
pytest tests/ -v -k "test_signal_ingest"

# 含覆盖率
pytest tests/ --cov=. --cov-report=term
```

### 代码检查

```bash
pip install ruff
ruff check .
```

配置在 `ruff.toml`：E/F/I/N/W/UP/B/SIM 规则，忽略 E501（行长度）/ B904（raise from），排除 tests 目录。

---

## 信号源配置

`signal_sources/` 目录下的策略参数文件供 next-k-api 使用，Protocol 运行时不需要：

> 注意：历史策略（ZCT VWAP / Momentum / Jiezhen / Moss Quant / Moss2）已废弃，对应的 source `.py` 文件已删除，仅 `__pycache__/` 目录残留。

---

## 部署

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY . .
EXPOSE 8001
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001}"]
```

```bash
# 构建
docker build -t next-k-protocol .

# 运行
docker run -p 8001:8001 \
  -e BINANCE_API_KEY=xxx \
  -e BINANCE_API_SECRET=xxx \
  -e BINANCE_ENABLED=true \
  -v /data:/data \
  -e DATA_DIR=/data \
  next-k-protocol
```

### Railway

`railway.json`：

```json
{
  "build": { "builder": "DOCKERFILE", "dockerfilePath": "Dockerfile" },
  "deploy": { "restartPolicyType": "ON_FAILURE", "restartPolicyMaxRetries": 10 }
}
```

### 本地开发

```bash
cd Next-k-protocol
cp .env.oi.example .env.oi
# 编辑 .env.oi 配置 BINANCE_API_KEY / BINANCE_API_SECRET
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

---

## 依赖清单

### 生产依赖（requirements.txt）

| 包 | 版本 | 用途 |
|----|------|------|
| fastapi | >=0.109.0 | Web 框架 |
| uvicorn[standard] | >=0.27.0 | ASGI 服务器 |
| pydantic | >=2.5.0 | 数据验证/序列化 |
| httpx | >=0.27.0 | HTTP 客户端（币安 API） |
| structlog | >=24.0.0 | 结构化日志 |
| prometheus_client | >=0.20.0 | Prometheus 指标 |

### 开发依赖（requirements-dev.txt）

| 包 | 版本 | 用途 |
|----|------|------|
| pytest | >=8.0.0 | 测试框架 |
| pytest-cov | >=4.1.0 | 覆盖率 |
| pytest-asyncio | >=0.23.0 | 异步测试 |
| httpx2 | >=0.27.0 | Starlette TestClient 依赖 |
| pytest-httpx | >=0.30.0 | HTTP mock |
| freezegun | >=1.4.0 | 时间冻结 |
| pyyaml | >=6.0.1 | YAML 加载 |

---

## 开发命令速查

```bash
# == 安装 ==
cd Next-k-protocol
pip install -r requirements.txt
pip install -r requirements-dev.txt   # 含测试工具

# == 运行 ==
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
# 或
./start.sh   # 含虚拟环境管理
./stop.sh    # 优雅停止

# == 测试 ==
pytest tests/ -v                          # 全部
pytest tests/unit/ -v                     # 单元
pytest tests/characterization/ -v         # 表征
pytest tests/ -v -k "test_signal_ingest"  # 筛选
pytest tests/ --cov=. --cov-report=term   # 含覆盖率

# == Lint ==
pip install ruff
ruff check .

# == 文档 ==
# Swagger:  http://localhost:8001/docs
# ReDoc:    http://localhost:8001/redoc
# 健康检查: http://localhost:8001/api/binance/health
```
