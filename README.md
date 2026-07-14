# Next K Protocol

币安合约实盘交易 API 服务。从 next-k-api 独立出来的微服务，通过 HTTP 接口接收交易请求并执行币安合约交易。

## 1. 项目概述

### 服务定位

Next K Protocol 是 Next K 交易系统的**执行层服务**，负责：

- 接收来自 next-k-api 的开仓请求
- 在币安 Futures 上执行 MARKET / LIMIT 入场
- 自动下止损/止盈条件单（STOP_MARKET / TAKE_PROFIT_MARKET）
- 直接代理币安实时账户摘要与当前持仓列表
- 记录执行日志与请求结果

### 与 next-k-api / 前端的关系

```
next-k-api ──/api/binance/*──► Next K Protocol（币安执行）
next-k-frontend/wangge.html ─► Protocol / （3xx-wangge 原样总控台+AI）
```

`vendor/wangge` = `3xx-wangge-main` 原壳 + Bitget（`bg`）第四所。详见 `docs/WANGGE.md`。

### 架构图

```
Request Flow:
  Client (binance.html / curl)
       |
       v
  FastAPI (main.py)
       |
       +-- /api/binance/health       (public)
       +-- /api/binance/status       (auth)
       +-- /api/binance/config       (auth)
       +-- /api/binance/signals/*    (auth)
       +-- /api/binance/positions    (auth)
              |
              v
         router.py ------> auth.py (token verification)
              |
       +------+------+
       v      v      v
     db.py  trader.py  (signal ingest)
       |      |
       |      v
       |  Binance Futures REST API
       |  (fapi.binance.com)
       v
   binance.db (SQLite)
```

## 2. 核心功能

### 信号接收与执行

1. next-k-api 完成 ZCT VWAP 扫描后，通过 `POST /api/binance/signals/ingest` 推送信号
2. 服务对每条信号进行多道闸门检查（去重、开关、信号源、持仓冲突、仓位上限）
3. 通过检查的信号调用 `trader.execute_trade()` 执行：
   - MARKET 市价单入场
   - LIMIT 限价挂单提交
   - STOP_MARKET 条件单止损
   - TAKE_PROFIT_MARKET 条件单止盈
4. SL/TP 下单失败 -> 紧急 MARKET 平仓，避免裸仓

### 运行特性

- **当前持仓事实源**：`/api/binance/positions` 直接读取币安 `positionRisk`
- **LIMIT 语义**：提交挂单后不做本地 pending 生命周期托管
- **执行日志**：`signals_log` 仅记录请求与执行结果
- **熔断保护**：连续 API 鉴权失败仍会自动禁用交易

### 配置管理

- 通过 `GET/POST /api/binance/config` 读写非敏感交易配置
- `BINANCE_API_KEY` / `BINANCE_API_SECRET` 仅通过 `.env.oi`、系统环境变量或 Railway 配置
- `GET /api/binance/config` 不返回凭证，`POST /api/binance/config` 也不接受凭证更新

## 3. 目录结构

```
Next-k-protocol/
  main.py           FastAPI entry, lifespan, CORS, route registration
  router.py         API route definitions (/api/binance/*)
  models.py         Pydantic request/response models
  db.py             SQLite database layer (config, signals_log)
  trader.py         Binance Futures REST execution layer
  scheduler.py      No-op compatibility shim
  auth.py           API auth module (X-Maintenance-Token / Bearer)
  env_loader.py     .env.oi environment variable loader
  railway.json      Railway deployment config
  requirements.txt  Python dependencies
  .env.oi.example   Environment variable reference
  start.sh          Local start script
  stop.sh           Stop script
  README.md         This document
  BINANCE.md        Binance trading module detailed docs
```

## 4. 配置参考

All env vars can be set via `.env.oi` file or system environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | 8001 | Service port |
| `PROTOCOL_MAINTENANCE_TOKEN` | (empty) | API auth token. Required in production |
| `BINANCE_API_KEY` | (empty) | Binance API Key（仅环境变量 / Railway） |
| `BINANCE_API_SECRET` | (empty) | Binance API Secret（仅环境变量 / Railway） |
| `BINANCE_TESTNET` | false | Use testnet |
| `BINANCE_MARGIN_USDT` | 100 | Margin per trade (USDT) |
| `BINANCE_LEVERAGE` | 10 | Leverage multiplier |
| `BINANCE_MAX_POSITIONS` | 8 | Global max positions |
| `BINANCE_MAX_POSITIONS_PLAY01` | 5 | PLAY01 max positions |
| `BINANCE_MAX_POSITIONS_PLAY02` | 5 | PLAY02 max positions |
| `BINANCE_MAX_POSITIONS_PLAY03` | 5 | PLAY03 max positions |
| `BINANCE_EXPIRE_HOURS_PLAY01` | 5 | PLAY01 expiry (hours) |
| `BINANCE_EXPIRE_HOURS_PLAY02` | 4 | PLAY02 expiry (hours) |
| `BINANCE_EXPIRE_HOURS_PLAY03` | 3 | PLAY03 expiry (hours) |
| `BINANCE_SRC_MOMENTUM_ENABLED` | false | Momentum strategy switch |
| `BINANCE_SRC_MOMENTUM_MARGIN_USDT` | 100 | Momentum margin per trade |
| `BINANCE_SRC_MOMENTUM_LEVERAGE` | 10 | Momentum leverage |
| `BINANCE_SRC_MOMENTUM_MAX_POSITIONS` | 2 | Momentum max positions |
| `BINANCE_SRC_MOMENTUM_EXPIRE_HOURS` | 4 | Momentum expiry (hours) |
| `BINANCE_SRC_MOMENTUM_HARD_SL_PCT` | 2.0 | Momentum hard stop-loss % |
| `BINANCE_SRC_MOMENTUM_TP_PCT` | 4.0 | Momentum take-profit % |
| `BINANCE_SRC_JIEZHEN_ENABLED` | false | Jiezhen strategy switch |
| `BINANCE_SRC_JIEZHEN_MARGIN_USDT` | 100 | Jiezhen margin per trade |
| `BINANCE_SRC_JIEZHEN_LEVERAGE` | 10 | Jiezhen leverage |
| `BINANCE_SRC_JIEZHEN_MAX_POSITIONS` | 3 | Jiezhen max positions |
| `BINANCE_SRC_JIEZHEN_EXPIRE_HOURS` | 4 | Jiezhen expiry (hours) |
| `BINANCE_SRC_JIEZHEN_HARD_SL_PCT` | 2.0 | Jiezhen hard stop-loss % |
| `BINANCE_SRC_JIEZHEN_TP_PCT` | 4.0 | Jiezhen take-profit % |
| `DATA_DIR` | (current dir) | Data directory (binance.db location) |

## 5. API 接口文档

Full interactive docs available at: `http://localhost:8001/docs`

### 5.1 Health Check

**GET /api/binance/health** (no auth)

```bash
curl http://localhost:8001/api/binance/health
```

Response:
```json
{
  "status": "ok",
  "module": "next-k-protocol",
  "version": "1.0.0"
}
```

### 5.2 Service Status

**GET /api/binance/status** (auth required)

```bash
curl -H "X-Maintenance-Token: your-token" \
  http://localhost:8001/api/binance/status
```

### 5.3 Read Config

**GET /api/binance/config** (auth required)

```bash
curl -H "X-Maintenance-Token: your-token" \
  http://localhost:8001/api/binance/config
```

### 5.4 Update Config

**POST /api/binance/config** (auth required)

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Maintenance-Token: your-token" \
  -d '{"pairs": {"enabled": "true", "margin_usdt": "200"}}' \
  http://localhost:8001/api/binance/config
```

### 5.5 Ingest Signals

**POST /api/binance/signals/ingest** (auth required)

Called by next-k-api to push ZCT signals.

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Maintenance-Token: your-token" \
  -d '{
    "signals": [
      {
        "source": "zct_vwap",
        "api_signal_id": "12345",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry_price": 67250.5,
        "sl_price": 66500.0,
        "tp_price": 68500.0,
        "confidence": 0.85,
        "regime": "TREND_UP",
        "play": "PLAY01"
      }
    ]
  }' \
  http://localhost:8001/api/binance/signals/ingest
```

### 5.6 Signal Log

**GET /api/binance/signals** (auth required)

```bash
curl -H "X-Maintenance-Token: your-token" \
  "http://localhost:8001/api/binance/signals?limit=50&offset=0"
```

### 5.7 List Positions

**GET /api/binance/positions** (auth required)

```bash
# Open positions
curl -H "X-Maintenance-Token: your-token" \
  "http://localhost:8001/api/binance/positions?status=open"

# Closed positions
curl -H "X-Maintenance-Token: your-token" \
  "http://localhost:8001/api/binance/positions?status=closed"
```

### 5.8 PnL Summary

**GET /api/binance/pnl/summary** (auth required)

```bash
curl -H "X-Maintenance-Token: your-token" \
  http://localhost:8001/api/binance/pnl/summary
```

## 6. 数据库设计

Database file: `$DATA_DIR/binance.db` (SQLite, WAL mode)

### config table

Key-value config storage. Seeded from env vars on first init, then managed via API.

| Column | Type | Description |
|--------|------|-------------|
| key | TEXT PK | Config key name |
| value | TEXT | Config value |

### signals_log table

Signal processing log. `(source, api_signal_id)` unique index ensures dedup.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Primary key |
| source | TEXT | Signal source (e.g. zct_vwap) |
| api_signal_id | TEXT | Original signal ID |
| symbol | TEXT | Trading pair |
| side | TEXT | LONG/SHORT |
| entry_price | REAL | Signal entry price |
| sl_price | REAL | Stop loss price |
| tp_price | REAL | Take profit price |
| confidence | REAL | Confidence score |
| regime | TEXT | Market regime |
| notional_usdt | REAL | Notional value |
| received_at | TEXT | Received time (UTC ISO8601) |
| status | TEXT | Processing status |
| skip_reason | TEXT | Skip/failure reason |
| play | TEXT | Strategy type |

### positions table

Position records. status='open' = active, status='closed' = settled.

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Primary key |
| signal_log_id | INTEGER FK | Linked signal |
| symbol | TEXT | Trading pair |
| side | TEXT | LONG/SHORT |
| entry_order_id | TEXT | Entry order ID |
| sl_order_id | TEXT | SL algo order ID |
| tp_order_id | TEXT | TP algo order ID |
| entry_price | REAL | Entry fill price |
| sl_price | REAL | SL trigger price |
| tp_price | REAL | TP trigger price |
| quantity | REAL | Position quantity |
| notional_usdt | REAL | Notional value |
| leverage | INTEGER | Leverage |
| opened_at | TEXT | Open time (UTC ISO8601) |
| expire_at | TEXT | Expiry time (UTC ISO8601) |
| status | TEXT | open/closed |
| close_reason | TEXT | tp/sl/expired/manual/unknown |
| close_price | REAL | Close price |
| closed_at | TEXT | Close time (UTC ISO8601) |
| pnl_usdt | REAL | P&L in USDT |
| pnl_pct | REAL | Leveraged return % |
| play | TEXT | Strategy type |

## 7. 交易流程

```
Signal arrives (POST /signals/ingest)
  |
  +-- 1. Dedup check: source+api_signal_id exists?
  |     +-- Yes -> skipped (return)
  |
  +-- 2. Trading enabled?
  |     +-- No -> skipped_disabled (return)
  |
  +-- 3. Source filter: source in enabled_sources?
  |     +-- No -> skipped_source_disabled (return)
  |
  +-- 4. Position conflict: open position for symbol?
  |     +-- Yes -> skipped_position_exists (return)
  |
  +-- 5. Position limits: per-play / global max reached?
  |     +-- Yes -> skipped_max_positions (return)
  |
  +-- 6. Execute trade (execute_trade)
       +-- Get symbol filters (LOT_SIZE, PRICE_FILTER, MIN_NOTIONAL)
       +-- Set margin mode (ISOLATED)
       +-- Set leverage
       +-- Detect HEDGE mode
       +-- Validate SL distance (>= 2 tick or 0.05% from mark)
       +-- Calculate qty = margin * leverage / mark_price
       +-- MARKET entry order
       |     +-- Fails -> error (return)
       +-- SL algo order (STOP_MARKET)
       |     +-- Fails -> emergency close + error (return)
       +-- TP algo order (TAKE_PROFIT_MARKET)
       |     +-- Fails -> emergency close + error (return)
       +-- Write positions table + update signal status -> traded

Background jobs:
  sync_open_positions (every 30s):
    For each open position:
      +-- Query Binance current position
      +-- If no longer open -> check SL/TP algo order status
      |     +-- TRIGGERED/FILLED -> record close by tp/sl
      |     +-- WORKING -> still pending (skip)
      |     +-- Unknown -> record close at mark price
      +-- Calculate and persist PnL

  expire_open_positions (every 5min):
    For each expired position:
      +-- Cancel all open orders
      +-- MARKET close order
      +-- Record PnL (close_reason='expired')
```

## 8. 鉴权机制

- All `/api/binance/*` endpoints except `/api/binance/health` require auth
- Auth methods: `X-Maintenance-Token` header or `Authorization: Bearer <token>`
- Token configured via `PROTOCOL_MAINTENANCE_TOKEN` env var
- Unset token = warning logged + all requests allowed (dev mode)
- 401 response: `{"detail": "maintenance_token_required"}`

## 9. 本地开发

### Setup

```bash
cd Next-k-protocol
cp .env.oi.example .env.oi
# Edit .env.oi, set:
#   PROTOCOL_MAINTENANCE_TOKEN=dev-token
#   BINANCE_TESTNET=true
./start.sh
```

### Manual Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

### API Docs

Open `http://localhost:8001/docs` in browser.

### Logs

```bash
tail -f logs/api.log
```

## 10. 部署说明

### Railway

1. Create new Railway project pointing to Next-k-protocol directory
2. `railway.json` already configured with NIXPACKS builder
3. Set environment variables:
   - `PROTOCOL_MAINTENANCE_TOKEN`
   - `BINANCE_API_KEY`
   - `BINANCE_API_SECRET`
   - `BINANCE_TESTNET=false`
4. For persistent data, mount Volume at `/data` and set `DATA_DIR=/data`

### Railway Env Vars

```
PORT=8001
PROTOCOL_MAINTENANCE_TOKEN=<your-secret-token>
BINANCE_API_KEY=<your-api-key>
BINANCE_API_SECRET=<your-api-secret>
BINANCE_TESTNET=false
DATA_DIR=/data
EMBED_SCHEDULER=1
```

## 11. 与 next-k-api 对接

next-k-api must set these env vars:

```
PROTOCOL_API_URL=http://localhost:8001
PROTOCOL_MAINTENANCE_TOKEN=<same-token>
```

next-k-api's `worker_tasks.py` will call `POST /api/binance/signals/ingest` after each ZCT scan.

### Signal Data Format

next-k-api reads `accumulation.db` -> `zct_vwap_signals` table, filters:
- `outcome IS NULL`
- `sl_price IS NOT NULL AND tp_price IS NOT NULL`
- `side IN ('LONG', 'SHORT')`

Converts matching rows to `SignalItem` list, POSTs to Next-k-protocol. 
