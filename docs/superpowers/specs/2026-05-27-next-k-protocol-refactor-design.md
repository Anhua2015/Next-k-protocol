# Next-k-protocol 全栈优化设计

**日期**：2026-05-27
**作者**：协同 AI brainstorming
**状态**：设计阶段，待用户最终确认
**目标**：在保持实盘行为不变的前提下，按"灰度迁移 + 先拆后测 + 全栈优化"策略，对 Next-k-protocol 进行结构化拆分、健壮性增强、可观测性补全和性能优化。

---

## 1. 背景与目标

### 1.1 当前问题（基于代码 audit）

**文件超大 / 函数超长**

- `trader.py` 1300 行（超 800 限制 1.6 倍）
  - `execute_trade()` 506-748 = 242 行单函数（4.8 倍 50 行规则）
  - `sync_open_positions()` 858-1009 = 151 行
  - `close_position()` 748-858 = 110 行
  - 职责混杂：签名 / HTTP / 重试 / 订单 / 同步 / 对账 / 平仓全在一文件
- `router.py` 611 行
  - `ingest_signals()` 211-440 = 229 行单函数
  - 端点内定义嵌套 helper 函数
  - 7 个 skip 分支各 5 行重复（DRY 违反）
  - 全程持 `_db._db_write_lock`，期间调币安 API（注释承认几百 ms）→ 阻塞所有写
- `db.py` 657 行，30+ 函数混 6 类职责（config / signals / positions / pending / pnl / migration）

**质量短板**

- 零测试（无 `tests/` 目录，违反 80% 覆盖要求）
- 全用 `dict` 不用领域对象
- 策略名 / 方向用魔法字符串（"zct_vwap" / "LONG" / "SHORT" / "PLAY01"…）
- 错误归一化不足：大量裸 `except Exception`，错误信息直接拼字符串入 DB
- 无结构化日志（全是 printf 风格）
- 无 metrics，无告警
- 紧急平仓失败仅 `logger.error` 后吞掉 → 裸仓无人知

**正面（无需重做）**

- HTTP 重试 / 退避 / `_HTTP_CLIENT` 单例 / auth-fail 计数 / exchangeInfo 缓存 / server time sync 都已具备
- Pydantic 模型清晰
- `auth.py` / `env_loader.py` / `scheduler.py` / `main.py` 已干净，不动

### 1.2 约束（用户已确认）

| 维度 | 选择 |
|------|------|
| 系统状态 | 实盘运行中，可短停（灰度迁移） |
| 改造顺序 | 先拆后补测（最快但需 characterization 防回归） |
| 优化范围 | 全栈：重构 + 健壮性 + 可观测 + 性能 |
| 推进路径 | 方案 A：自底向上分层，9 个 phase |

### 1.3 验收目标

| 项 | 目标 |
|----|------|
| `trader.py` | ≤ 200 行（仅 facade） |
| `db.py` | ≤ 200 行（仅 facade） |
| `router.py` | ≤ 200 行（仅 facade） |
| 任意业务文件 | ≤ 600 行 |
| 任意函数 | ≤ 50 行 |
| 嵌套深度 | ≤ 4 |
| 测试覆盖 | ≥ 80% |
| Characterization | 全绿 |
| 结构化日志 | 100% 关键事件 |
| Prometheus metrics | 23 个指标全暴露 |
| 告警 | 至少 1 个 critical 实测触发过 |
| 性能 | ingest 100 信号 P99 下降 ≥ 30% |
| 实盘 | 灰度后稳定运行 7 天无 SEV2+ 事件 |

---

## 2. 目标包结构

```
Next-k-protocol/
├── main.py                       # FastAPI 入口（现状保留）
├── env_loader.py                 # 现状保留
├── auth.py                       # 现状保留（76 行 OK）
├── models.py                     # Pydantic IO 模型（现状保留）
├── scheduler.py                  # 现状保留（44 行 OK，改 import 路径即可）
│
├── domain/                       # 领域对象 + 枚举
│   ├── __init__.py
│   ├── enums.py                  # Side, Source, EntryType, CloseReason
│   ├── signal.py                 # Signal 领域对象（frozen dataclass）
│   └── position.py               # Position, PendingPosition 领域对象
│
├── common/
│   └── exceptions.py             # ProtocolError 异常体系
│
├── binance/                      # 币安 HTTP 客户端层（Phase 1-2）
│   ├── __init__.py
│   ├── client.py                 # _request / 重试 / 退避 / 时钟同步
│   ├── signing.py                # HMAC 签名
│   ├── time_sync.py              # server time offset
│   ├── exchange_info.py          # exchangeInfo 缓存 + filters + mark price
│   ├── orders.py                 # place_order / algo / cancel
│   └── account.py                # get_live_position / leverage / margin / hedge
│
├── trading/                      # 交易业务层（Phase 2-4）
│   ├── __init__.py
│   ├── executor.py               # execute_trade orchestrator（~60-80 行）
│   ├── market_entry.py           # MARKET 分支
│   ├── limit_entry.py            # LIMIT 分支 + pending insert
│   ├── protective.py             # SL/TP 下单 + 紧急平仓 + 距离校验
│   └── pricing.py                # _round_price / _round_quantity
│
├── lifecycle/                    # 持仓生命周期（Phase 3）
│   ├── __init__.py
│   ├── sync.py                   # sync_open_positions
│   ├── reconcile.py              # reconcile_pending_entries + promote
│   ├── expire.py                 # expire_open_positions
│   └── close.py                  # close_position 调用 + record_closed
│
├── ingest/                       # 信号摄入流水线（Phase 5）
│   ├── __init__.py
│   ├── pipeline.py               # 守卫链 orchestrator
│   ├── guards.py                 # 6 个独立 guard 函数
│   └── dispatcher.py             # 调 trading.executor
│
├── repos/                        # 数据仓储（Phase 6，替换 db.py）
│   ├── __init__.py
│   ├── connection.py             # get_db / _db_write_lock / init_db
│   ├── config_repo.py            # config 表
│   ├── signals_repo.py           # signals_log 表
│   ├── positions_repo.py         # positions + pending 表
│   └── pnl_repo.py               # pnl_summary 查询
│
├── routers/                      # FastAPI 路由（拆 router.py）
│   ├── __init__.py
│   ├── health.py                 # /health /status
│   ├── config.py                 # /config GET/POST
│   ├── signals.py                # /signals /signals/ingest
│   ├── positions.py              # /positions /positions/{id}/close
│   ├── pnl.py                    # /pnl
│   └── metrics.py                # /metrics（Phase 7）
│
├── observability/                # Phase 7
│   ├── __init__.py
│   ├── logging_setup.py          # structlog JSON 日志
│   ├── metrics.py                # Prometheus 指标
│   └── alerts.py                 # Webhook 告警 + dedup
│
└── tests/                        # Phase 0 起点
    ├── fixtures/binance/         # 录制的 API 响应
    ├── characterization/         # 黑盒回归（Phase 0 建立）
    ├── unit/                     # 每模块单测（Phase 9）
    └── integration/              # FastAPI TestClient（Phase 9）
```

### 关键边界

- `binance/`：纯 HTTP，无业务逻辑，无 DB 依赖
- `trading/`：调 `binance/` + `repos/`，纯函数风格
- `lifecycle/`：被 `scheduler.py` 调
- `repos/`：唯一碰 SQLite 的层
- `routers/`：仅做 IO 转换 + 调 `ingest/` / `lifecycle/`

### Facade 兼容

Phase 1-6 期间，`trader.py` / `db.py` / `router.py` 保留为 re-export shim，旧 import 路径不变，灰度可纯 revert 回滚。

---

## 3. 模块职责与接口契约

### 3.1 `binance/client.py`

```python
class BinanceClient:
    def __init__(self,
                 base_url_fn: Callable[[], str],
                 api_key_fn: Callable[[], str],
                 secret_fn: Callable[[], str]):
        ...

    def request(self,
                method: str,
                path: str,
                params: dict | None = None,
                signed: bool = True) -> Any:
        ...
```

- 不直接读 DB，通过依赖注入闭包获取 key/secret → 可纯单测
- 模块导出单例 `client = BinanceClient(...)`，由 `binance/__init__.py` 创建
- 内部：`_sign` / `_ts` / `_sync_server_time` / 重试与退避循环

### 3.2 `binance/orders.py`

```python
def place_order(client: BinanceClient, params: dict) -> dict
def place_algo_order(client: BinanceClient, params: dict) -> dict
def cancel_order_by_id(client: BinanceClient, symbol: str, order_id: str) -> bool
def cancel_algo_order(client: BinanceClient, algo_id: str) -> bool
def cancel_all_orders(client: BinanceClient, symbol: str, pos: dict | None = None) -> bool
def get_open_algo_orders(client: BinanceClient, symbol: str) -> list
def get_order(client: BinanceClient, symbol: str, order_id: str) -> dict
```

### 3.3 `binance/account.py`

```python
def get_live_position(client: BinanceClient, symbol: str) -> dict | None
def set_leverage(client: BinanceClient, symbol: str, leverage: int) -> None
def set_margin_type(client: BinanceClient, symbol: str) -> None
def detect_hedge_mode(client: BinanceClient) -> bool   # 带缓存
```

### 3.4 `binance/exchange_info.py`

```python
def get_symbol_info(client: BinanceClient, symbol: str) -> dict   # TTL 5min 缓存
def get_filters(client: BinanceClient, symbol: str) -> tuple[str, str, float]
def get_mark_price(client: BinanceClient, symbol: str) -> float
```

### 3.5 `trading/executor.py`

```python
def execute_trade(signal: Signal) -> bool:
    """Orchestrator，~60-80 行：
    1. 读 config（margin / leverage / entry_type，可缓存）
    2. dispatch MARKET vs LIMIT，调 market_entry / limit_entry
    3. 失败统一 update_signal_status + return False
    """
```

### 3.6 `trading/market_entry.py` / `limit_entry.py`

```python
def open_market(signal: Signal, ctx: TradeContext) -> ExecutionResult
def open_limit(signal: Signal, ctx: TradeContext) -> ExecutionResult
```

- `TradeContext`：margin / leverage / symbol filters / hedge_mode / position_side（一次性算好传入）
- `ExecutionResult`：成功 + 上下文，或错误 + 异常

### 3.7 `trading/protective.py`

```python
def place_sl_tp(symbol, close_side, sl, tp, qty, position_side, tick) -> tuple[str, str]
def emergency_close(symbol, side, qty, position_side) -> None
def validate_sl_distance(side, sl_price, mark_px, tick) -> None
```

### 3.8 `lifecycle/sync.py` / `reconcile.py` / `expire.py`

```python
def sync_open_positions() -> None
def reconcile_pending_entries() -> None
def expire_open_positions() -> None
```

内部：`_close_one` / `_reconcile_one` / `_promote_pending` / `_emergency_close` 各为独立函数。

### 3.9 `ingest/pipeline.py`

```python
def process_signal_batch(signals: list[SignalItem]) -> SignalIngestResult:
    ctx = build_ingest_context()   # 一次性读 max_pos / play_max / source_max
    for sig in signals:
        details.append(_process_one(sig, ctx))
    return ...

def _process_one(sig: SignalItem, ctx: IngestContext) -> IngestDetail:
    for guard in GUARDS:
        decision = guard(sig, ctx)
        if decision.skip:
            return decision.to_detail()
    return dispatcher.execute(sig)
```

### 3.10 `ingest/guards.py`

```python
@dataclass(frozen=True)
class GuardDecision:
    skip: bool
    reason: str | None
    action: str          # "duplicate" / "skipped_max_positions" / ...
    signal_log_id: int | None

def guard_trading_disabled(sig, ctx) -> GuardDecision
def guard_invalid_source(sig, ctx) -> GuardDecision
def guard_source_disabled(sig, ctx) -> GuardDecision
def guard_dedup_insert(sig, ctx) -> GuardDecision   # 内含 insert_signal
def guard_position_exists(sig, ctx) -> GuardDecision
def guard_max_positions(sig, ctx) -> GuardDecision
```

每 guard 5-15 行，DRY 化原来的 7 个重复 skip 分支。

### 3.11 `repos/*`

仓储模式，每仓储只碰自己的表：

```python
class SignalsRepo:
    def insert(self, **kw) -> int | None         # None = duplicate
    def update_status(self, sid, status, reason=None) -> None
    def list(self, limit, offset, source=None) -> list[dict]

class PositionsRepo:
    def insert_open(...) / insert_pending(...)
    def get_open() / get_open_by_symbol() / get_open_expired() / get_by_id()
    def count_open_by_play() / count_open_by_source() / count_open_total()
    def update_closed() / promote_pending() / cancel_pending()
    # 裸仓告警（Phase 7 引入 naked_position_alerts 表，Phase 6 仓储仅预留方法签名）
    def mark_naked_position(symbol, qty, last_error) -> None
    def load_unacknowledged_naked_positions() -> list[dict]
    def ack_naked_position(symbol) -> None

class ConfigRepo:
    def get(key, default) / get_all() / set(k, v) / set_batch(pairs)
    def get_source(source, key_suffix, default)
    def source_enabled(source) -> bool

class PnlRepo:
    def summary() -> PnlSummary
```

### 关键不变量

- 领域对象 frozen dataclass → 强制 immutability
- 所有 `binance/*` 函数无全局可变状态（缓存通过 LRU/TTL 装饰器）
  - 例外：HTTP client 单例 + server time offset（线程安全锁内）
- repos 不返回 ORM 对象，返回 dict 或 dataclass

---

## 4. 数据流

### 4.1 信号摄入 → 开仓

```
next-k-api (HTTP POST /signals/ingest)
        │
        ▼
routers/signals.py :: ingest_signals(body)
        │ Pydantic 验证 + 调 pipeline
        ▼
ingest/pipeline.py :: process_signal_batch(signals)
        │ 加载 IngestContext (max_pos, play_max, source_max — 一次性读 config)
        │ for sig in signals:
        ▼
ingest/pipeline.py :: _process_one(sig, ctx)
        │ 守卫链顺序执行：
        │   1. guard_trading_disabled
        │   2. guard_invalid_source
        │   3. guard_dedup_insert ← 锁内 insert_signal，拿到 signal_log_id
        │   4. guard_source_disabled
        │   5. guard_position_exists
        │   6. guard_max_positions (zct → play / 其它 → source / 全局)
        │ 任一 guard skip=True → 返回 IngestDetail，循环下一条
        ▼
ingest/dispatcher.py :: dispatch(sig, signal_log_id)
        │ 构造 Signal 领域对象 → 调 executor
        ▼
trading/executor.py :: execute_trade(signal)
        │ ctx = build_context(signal)
        │ 分支 MARKET vs LIMIT
        ▼
trading/market_entry.py 或 limit_entry.py
        │ MARKET：
        │   binance/orders.place_order(MARKET) → entry_price →
        │   trading/protective.place_sl_tp() →
        │     成功 → repos.positions_repo.insert_open() + signals_repo.update_status("traded")
        │     失败 → trading/protective.emergency_close() + signals_repo.update_status("error")
        │ LIMIT：
        │   binance/orders.place_order(LIMIT) →
        │   repos.positions_repo.insert_pending() + signals_repo.update_status("pending_entry")
        │   后续由 lifecycle/reconcile 接手
```

### 4.2 LIMIT pending → 成交 promote

```
scheduler 每 5s 触发
        ▼
lifecycle/reconcile.py :: reconcile_pending_entries()
        │ repos.positions_repo.get_pending_entries()
        │ for each pending:
        ▼
lifecycle/reconcile.py :: _reconcile_one(pos)
        │ binance/orders.get_order(symbol, entry_order_id)
        │ 已成交 → _promote_pending(pos, fill_qty, fill_price)
        │ 超时   → binance/orders.cancel_order_by_id() + repos.cancel_pending_position()
        │ 其它   → 留 pending
        ▼
lifecycle/reconcile.py :: _promote_pending(pos, fill_qty, fill_price)
        │ trading/protective.place_sl_tp(...) ← 复用 executor 的同一函数
        │ repos.positions_repo.promote_pending_to_open(pos_id, sl_id, tp_id, entry_price)
        │ repos.signals_repo.update_status("traded")
```

### 4.3 持仓同步 → 触发平仓

```
scheduler 每 30s 触发
        ▼
lifecycle/sync.py :: sync_open_positions()
        │ repos.positions_repo.get_open_positions()
        │ for each open:
        ▼
lifecycle/sync.py :: _check_one(pos)
        │ binance/account.get_live_position(symbol)
        │ qty == 0 ? ← 已平
        │   是 → _determine_close_reason() (查最近 algo order 状态)
        │       → lifecycle/close.record_closed(pos, reason, price, pnl)
        │       → repos.positions_repo.update_position_closed(...)
        │   否 → 继续监控
        │ HTTP 401/403 → _handle_auth_fail (累加，超阈值禁交易)
```

### 4.4 过期强平

```
scheduler 每 5min 触发
        ▼
lifecycle/expire.py :: expire_open_positions()
        │ repos.positions_repo.get_open_expired_positions()
        │ for each expired:
        │   binance/orders.cancel_all_orders(symbol)
        │   trading/protective.emergency_close(symbol, side, qty, position_side)
        │   lifecycle/close.record_closed(pos, "expired", mark_px, pnl)
```

### 4.5 手动平仓（close endpoint）

```
next-k-api → POST /positions/{id}/close
        ▼
routers/positions.py :: close_position(body)
        │ repos.positions_repo.get_open_position_for_symbol(symbol)
        ▼
lifecycle/close.py :: close_position_now(pos, exit_rule, close_price)
        │ binance/orders.cancel_all_orders(symbol)
        │ trading/protective.emergency_close(...)
        │ repos.positions_repo.update_position_closed("paper_close", ...)
```

### 4.6 关键差异 vs 现状

| 维度 | 现状 | 新设计 |
|------|------|--------|
| `ingest_signals` 锁范围 | 整个 for 循环 + 含 execute_trade（HTTP） | guard chain 在锁内（纯 DB 操作），execute_trade 移到锁外 |
| 并发 | 一次只处理一条信号 | guard 串行（数据竞争窗），execute 阶段并行 |
| 锁正确性 | guard 与 execute 之间无原子性 | 在锁内 insert_signal + reserve "intent" 行（status=`intent`），execute 完后 update。同 symbol 并发：第一条 reserved 后立即 update_signal_status → 第二条 `guard_position_exists` 看到 `intent` 即跳过 |

注：锁外 execute 改造在 Phase 5 同步评估安全性。若风险过高，回退到现状（锁内）+ 仅优化锁粒度（per-symbol lock）。

---

## 5. 错误处理 / 异常体系

### 5.1 新异常类

```python
# common/exceptions.py

class ProtocolError(Exception):
    """所有业务异常基类"""
    code: str
    retryable: bool = False

# ── HTTP / 网络层 ────────────────────────────────
class BinanceHTTPError(ProtocolError):
    code = "binance_http"

class BinanceAuthError(BinanceHTTPError):
    code = "binance_auth"          # 401/403 → 累加 _SYNC_AUTH_FAIL_COUNT

class BinanceRateLimitError(BinanceHTTPError):
    code = "binance_rate_limit"
    retryable = True               # 429/418/-1003

class BinanceServerError(BinanceHTTPError):
    code = "binance_5xx"
    retryable = True

class BinanceTimeskewError(BinanceHTTPError):
    code = "binance_timeskew"      # -1021，触发 server time resync

class BinanceBusinessError(BinanceHTTPError):
    code = "binance_business"      # 资金不足 / 标的不合法等 4xx

# ── 配置 / 输入 ───────────────────────────────────
class ConfigError(ProtocolError):
    code = "bad_config"

class SignalValidationError(ProtocolError):
    code = "bad_signal"

# ── 业务 ──────────────────────────────────────────
class InsufficientNotionalError(ProtocolError):
    code = "min_notional"

class SLDistanceError(ProtocolError):
    code = "sl_too_close"

class EmergencyCloseFailedError(ProtocolError):
    code = "emergency_close_failed"  # CRITICAL：裸仓告警
```

### 5.2 异常责任分层

| 层 | 抛 | 捕（recover） | 捕（fail-stop） |
|----|----|---------------|-----------------|
| `binance/client.py` | `Binance*Error` 全套 | `retryable=True` 重试 | — |
| `binance/orders.py` | 透传 | — | — |
| `trading/protective.py` | `EmergencyCloseFailedError` | — | 触发 critical 告警 |
| `trading/executor.py` | 透传 | catch `Binance*Error` / `ConfigError` / `SignalValidationError` → update_signal_status | `EmergencyCloseFailedError` 重抛 |
| `lifecycle/*` | — | catch `Binance*Error`（log + 计数器，跳本条） | `EmergencyCloseFailedError` 触发告警 |
| `ingest/pipeline.py` | — | catch 所有 `ProtocolError` 转 IngestDetail | — |
| `routers/*` | — | catch `Exception` 转 500 + safe error body | — |

### 5.3 `_request` 重试逻辑（精炼）

```python
def request(method, path, params, signed):
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = _http_call(...)
            if status in RETRY_STATUSES:    raise BinanceRateLimitError(...)
            if status in (401, 403):        raise BinanceAuthError(...)
            if status >= 400:
                body = parse_json(resp)
                code = body.get("code")
                if code == -1021:           raise BinanceTimeskewError(...)
                if code in RETRY_CODES:     raise BinanceRateLimitError(...)
                raise BinanceBusinessError(code=code, msg=body)
            return resp.json()
        except BinanceTimeskewError:
            _sync_server_time(); _re_sign(); continue        # 立即重试，不退避
        except (BinanceRateLimitError, BinanceServerError, httpx.RequestError) as exc:
            if attempt < MAX_RETRIES:
                sleep(BACKOFF_BASE_SEC * 2**attempt)
                continue
            raise
        except BinanceAuthError:
            raise          # 上层 _handle_auth_fail 处理
```

### 5.4 紧急平仓失败处理

```python
def emergency_close(symbol, side, qty, position_side):
    try:
        binance.orders.place_order({"type": "MARKET", "reduceOnly": True, ...})
    except Exception as exc:
        logger.critical("EMERGENCY_CLOSE_FAILED", symbol=symbol, qty=qty, exc=str(exc))
        repos.positions_repo.mark_naked_position(symbol)
        observability.alerts.send_critical(f"naked position {symbol}", details=...)
        raise EmergencyCloseFailedError(symbol=symbol, qty=qty) from exc
```

新增 `naked_position_alerts` 表：保留所有 emergency_close 失败记录。启动时 main 加载并持续告警直至人工确认（DB 中 `acknowledged=1`）。

### 5.5 Auth 失败串行机制

- `BinanceAuthError` 抛出时：`_handle_auth_fail(ctx, pos_id)` 累加计数
- 阈值 20 → `repos.config_repo.set("enabled", "false")` + critical alert
- `lifecycle/sync` 和 `lifecycle/reconcile` 与 `binance/client` 入口的 401/403 都汇入同一计数器
- 新增：成功调用一次重置计数（已有 `_reset_auth_fail_count`，需在 sync/reconcile 成功结尾调用）

### 5.6 DB `skip_reason` 格式

固定结构：`"<code>: <free text>"`，例：

- `bad_config: margin=0 leverage=10`
- `binance_business: code=-2019 msg=Margin is insufficient`
- `sl_too_close: sl=66500 mark=66510 tick=0.1`

`code` 在 metrics 标签 + DB 索引（Phase 8 加 index）。

---

## 6. 可观测性

### 6.1 结构化日志（observability/logging_setup.py）

```python
import structlog

def configure_logging():
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )

logger = structlog.get_logger()
```

调用约定：

```python
logger.info("trade_opened",
            symbol="BTCUSDT", side="LONG", source="zct_vwap", play="PLAY01",
            qty=0.012, entry=67250.5, sl=66500, tp=68500,
            margin=200, leverage=10, signal_log_id=12345,
            entry_order_id="abc", sl_algo_id="def", tp_algo_id="ghi")
```

### 6.2 关键事件命名

| event | 触发 |
|-------|------|
| `signal_received` | router 入口（每信号 1 条，含 batch_id） |
| `signal_skipped` | guard 拦截（含 `code`） |
| `signal_duplicate` | dedup guard |
| `trade_opening` | 开始执行（含 entry_type） |
| `trade_opened` | 全流程成功 |
| `trade_open_failed` | 异常退出（含 stage：entry/sl/tp/insert） |
| `pending_promoted` | LIMIT pending → open |
| `pending_cancelled` | LIMIT 超时撤单 |
| `position_synced` | sync 检测到已平 |
| `position_expired` | 过期强平 |
| `position_closed` | 任何平仓终态 |
| `emergency_close` | 紧急平仓尝试 |
| `emergency_close_failed` | 紧急平仓失败 ★ |
| `naked_position_alert` | 启动加载未确认告警 ★ |
| `auth_fail` | 401/403 累加 |
| `trading_auto_disabled` | 自动禁用 ★ |
| `binance_request` | 每次币安调用（含 path/status/elapsed_ms/attempt） |
| `binance_retry` | 重试触发 |

### 6.3 Request ID 贯穿

```python
import structlog.contextvars
structlog.contextvars.bind_contextvars(
    request_id=uuid4().hex[:12],
    batch_id=batch_id,
)
```

### 6.4 日志噪音控制

- guard 拦截统一 `signal_skipped` 含 `code`（不再每 guard 一条 info）
- 币安 4xx 业务错误：WARN 级别 + 完整 body
- 币安 5xx / 重试：DEBUG 级别（仅 metrics 计数）
- `sync_open_positions` 无变化时不输出（DEBUG）

### 6.5 Metrics（observability/metrics.py）

使用 `prometheus_client`，暴露 `/metrics`（IP 白名单或 token）。

```python
# 计数器
SIGNALS_RECEIVED = Counter("protocol_signals_received_total", "", ["source", "play"])
SIGNALS_SKIPPED  = Counter("protocol_signals_skipped_total",  "", ["source", "code"])
TRADES_OPENED    = Counter("protocol_trades_opened_total",    "", ["source", "side", "entry_type"])
TRADES_FAILED    = Counter("protocol_trades_failed_total",    "", ["source", "stage", "code"])
POSITIONS_CLOSED = Counter("protocol_positions_closed_total", "", ["source", "close_reason"])
EMERGENCY_CLOSE_FAILED = Counter("protocol_emergency_close_failed_total", "", ["symbol"])
AUTH_FAIL        = Counter("protocol_auth_fail_total", "")
TRADING_DISABLED_AUTO = Counter("protocol_trading_disabled_auto_total", "")
BINANCE_REQUESTS = Counter("protocol_binance_requests_total", "",
                           ["method", "path", "status_class"])
BINANCE_RETRIES  = Counter("protocol_binance_retries_total", "", ["path", "reason"])

# 直方图
BINANCE_LATENCY    = Histogram("protocol_binance_request_seconds", "",
                               ["method", "path"], buckets=[.05,.1,.25,.5,1,2,5])
TRADE_OPEN_LATENCY = Histogram("protocol_trade_open_seconds", "",
                               ["entry_type"], buckets=[.1,.5,1,2,5])

# Gauge
OPEN_POSITIONS_GAUGE    = Gauge("protocol_open_positions", "", ["source"])
PENDING_POSITIONS_GAUGE = Gauge("protocol_pending_positions", "")
TRADING_ENABLED         = Gauge("protocol_trading_enabled", "")
EXCH_INFO_HITS          = Counter("protocol_exchange_info_cache_total", "", ["result"])
```

Gauge 刷新：sync_open_positions 结束时一次性刷（30s 间隔够用）。

### 6.6 告警（observability/alerts.py）

最小化实现：Webhook + 进程内去重。

```python
@dataclass
class AlertConfig:
    webhook_url: str
    template: str          # "telegram" | "discord" | "dingtalk" | "raw_json"

class Alerter:
    def send(self, level: str, event: str, body: str,
             dedup_key: str | None = None, cooldown_sec: int = 300):
        ...
```

告警触发点（critical）：

| event | dedup_key | 内容 |
|-------|-----------|------|
| `emergency_close_failed` | `naked:{symbol}` | symbol/qty/last_error，须人工 ack |
| `naked_position_alert` | `naked:{symbol}` | 启动加载未确认裸仓 |
| `trading_auto_disabled` | `trading_disabled` | 触发原因 |
| `binance_5xx_burst` | `5xx_burst` | 5min 内 >50 次 5xx |
| `pending_promote_timeout_spike` | `pending_spike` | 5min 内 pending 撤单超阈值 |

回退：webhook 未配置 → 仅日志 `alert_emit` event，不阻塞业务。

### 6.7 新增 env vars（写入 `.env.oi.example`）

| 变量 | 说明 | 默认 |
|------|------|------|
| `PROTOCOL_LOG_FORMAT` | `json` / `text` | `json` |
| `PROTOCOL_LOG_LEVEL` | DEBUG/INFO/WARN/ERROR | `INFO` |
| `PROTOCOL_METRICS_ENABLED` | `true`/`false` | `true` |
| `PROTOCOL_METRICS_TOKEN` | `/metrics` token（空=无鉴权） | — |
| `PROTOCOL_ALERT_WEBHOOK_URL` | 告警 webhook | — |
| `PROTOCOL_ALERT_TEMPLATE` | telegram/discord/dingtalk/raw_json | `raw_json` |
| `PROTOCOL_ALERT_DEDUP_COOLDOWN_SEC` | 同 key 冷却 | `300` |

---

## 7. 测试策略

### 7.1 测试金字塔

```
        E2E (TestClient + httpx mock binance)        ~10 cases
       ╱
      ╱  Integration (FastAPI TestClient + 真 SQLite + mock binance) ~30 cases
     ╱
    ╱   Unit (纯函数 + dataclass)                                    ~150 cases
   ╱
  ──────────────────────────────────────────────────────────────────
   Characterization (黑盒锁住现状行为)                                ~40 cases
```

### 7.2 Characterization 测试（Phase 0，重构前先建）

目的：**冻结现状行为**——重构改的是结构不是行为。任何 phase 跑挂直接回滚。

核心用例：

- `test_zct_dup_signal_skipped`
- `test_zct_open_then_same_symbol_signal_skipped`
- `test_play01_at_max_positions_skipped`
- `test_momentum_max_positions_skipped`
- `test_global_max_positions_after_play_check`
- `test_trading_disabled_skips_all`
- `test_invalid_source_rejected`
- `test_market_entry_full_flow`
- `test_limit_entry_pending_then_reconcile_promote`
- `test_limit_entry_pending_timeout_cancel`
- `test_sl_placement_fail_triggers_emergency_close`
- `test_sync_detects_sl_triggered`
- `test_expire_after_play_hours`
- `test_close_position_endpoint_paper_close`
- `test_auth_fail_threshold_disables_trading`
- `test_timeskew_1021_triggers_resync_retry`
- `test_429_retry_with_backoff`
- `test_min_notional_rejection`
- `test_hedge_mode_uses_position_side`
- `test_one_way_mode_no_position_side`

实现技巧：

- Binance API 用 `pytest-httpx` 录制/回放 fixture（JSON 文件存 `tests/fixtures/binance/`）
- DB 用临时 SQLite 文件（每函数 fresh tmpdir）
- 时钟用 `freezegun`
- Characterization 在每 PR + 每 phase 结束 gate

### 7.3 Unit 覆盖目标（Phase 9）

| 模块 | 覆盖 % | 重点 |
|------|--------|------|
| `binance/signing.py` | 100 | _sign() 已知 vector |
| `binance/client.py` | 95 | 重试路径全分支 |
| `binance/exchange_info.py` | 90 | 缓存 TTL + filters 解析 |
| `trading/pricing.py` | 100 | round 边界（step=1, step=0.001, 大数） |
| `trading/protective.py` | 90 | SL 距离校验、emergency_close 失败 |
| `trading/market_entry.py` / `limit_entry.py` | 85 | entry_price 缺失、qty=0、notional 不足 |
| `trading/executor.py` | 85 | 分支选择 + 错误归一化 |
| `lifecycle/sync.py` | 85 | close_reason 推断 |
| `lifecycle/reconcile.py` | 90 | promote 路径 + 撤单路径 |
| `ingest/guards.py` | 100 | 每 guard 一个测，dedup 含并发 |
| `ingest/pipeline.py` | 90 | guard 链短路 + 异常隔离 |
| `repos/*` | 80 | CRUD + 边界 |

总覆盖 ≥ 80%。

### 7.4 集成测试

```python
def test_ingest_full_flow(client: TestClient, mock_binance):
    mock_binance.match_request(method="POST", path="/fapi/v1/order").respond(...)
    resp = client.post("/api/binance/signals/ingest",
                       json={"signals": [...]},
                       headers={"X-Maintenance-Token": "test"})
    assert resp.json()["traded"] == 1
    positions = client.get("/api/binance/positions").json()
    assert len(positions) == 1
```

### 7.5 CI

`.github/workflows/test.yml`：

```yaml
- pytest tests/characterization/  -v --strict-markers
- pytest tests/unit/              --cov=. --cov-fail-under=80
- pytest tests/integration/       -v
```

Phase 1-8 期间，每 PR 必须跑通 characterization 全套。

---

## 8. 性能优化（Phase 8）

### 8.1 移除 ingest 持锁期间 HTTP 调用

详见 §4.6 关键差异。

并发保护：`guard_position_exists` 检查既看 `positions.status in (open, pending)` 也看 `signals_log.status=intent` 在最近 30s 内的同 symbol。

intent 留存超时清理：lifecycle 加一个 5min 间隔的 cleanup job 处理超时 intent（标记为 error）。

### 8.2 SQLite 索引

```sql
CREATE INDEX IF NOT EXISTS idx_signals_log_dedup ON signals_log(source, api_signal_id);
CREATE INDEX IF NOT EXISTS idx_signals_log_status ON signals_log(status);
CREATE INDEX IF NOT EXISTS idx_positions_status_symbol ON positions(status, symbol);
CREATE INDEX IF NOT EXISTS idx_positions_status_play ON positions(status, play);
CREATE INDEX IF NOT EXISTS idx_positions_status_source ON positions(status, source);
CREATE INDEX IF NOT EXISTS idx_positions_expire_at ON positions(status, expire_at);
CREATE INDEX IF NOT EXISTS idx_positions_closed_at ON positions(closed_at);
```

Phase 8 加 migration（in-place `IF NOT EXISTS`，向后兼容）。

### 8.3 exchangeInfo 缓存命中率

Phase 7 加 metrics（`EXCH_INFO_HITS`）。若命中率 < 90% → 调大 TTL 至 1800s。

### 8.4 server time sync 频率

现状 600s + 1021 触发，够用，不改。

### 8.5 config 表 N+1

```python
@dataclass(frozen=True)
class TradingConfig:
    enabled: bool
    margin_usdt: float
    leverage: int
    entry_type: str
    max_positions: int
    play_max: dict[str, int]
    source_max: dict[str, int]
    source_margin: dict[str, float]
    source_leverage: dict[str, int]
    source_entry_type: dict[str, str]

config_repo.load_trading_config() -> TradingConfig
```

内存 30s TTL（`cachetools.TTLCache`），写操作清缓存。

### 8.6 httpx pool

```python
_HTTP_CLIENT = httpx.Client(
    timeout=httpx.Timeout(connect=3, read=10, write=10, pool=2),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)
```

---

## 9. 分 Phase 推进路线

### 9.1 总览

| Phase | 内容 | 预估 | trader.py 变化 |
|-------|------|------|----------------|
| 0 | Characterization tests | 2 天 | — |
| 1 | `binance/` 抽 HTTP 层 | 2 天 | 1300 → ~1000 |
| 2 | `binance/orders` + `trading/protective` | 2 天 | ~1000 → ~700 |
| 3 | `lifecycle/` 抽生命周期任务 | 2 天 | ~700 → ~400 |
| 4 | `trading/executor` 拆 execute_trade | 2 天 | ~400 → ~150 |
| 5 | `ingest/` 拆 ingest_signals + 锁外 HTTP | 3 天 | router.py 611 → ~350 |
| 6 | `repos/` 拆 db.py | 2 天 | db.py 657 → ~150 |
| 7 | `observability/` 日志 + metrics + 告警 | 3 天 | 新增 |
| 8 | 性能优化（索引、config 缓存、pool） | 1 天 | — |
| 9 | 测试补齐（≥80% 覆盖） | 3 天 | — |

**总计**：22 个有效工作日（约 4-5 周节奏，含灰度观察期）

### 9.2 Phase 0：Characterization 基线

**交付**

- `tests/characterization/` 目录 + ~40 用例（见 §7.2）
- `tests/fixtures/binance/` 录制现网响应
- `pytest-httpx` + `freezegun` + `pytest-cov` 依赖
- CI workflow `test.yml`

**门禁**

- 所有 characterization 测试 PASS
- 测试运行时间 < 60s

**风险**：无生产代码变更，零风险。

### 9.3 Phase 1：binance/ 抽 HTTP 层

**变更**

- 新建 `binance/{client,signing,time_sync,exchange_info,account}.py`
- trader.py 中：`_sign` / `_ts` / `_sync_server_time` / `_request` / `get_mark_price` / `get_symbol_info` / `_get_exchange_info` / `_get_filters` / `_detect_hedge_mode` / `set_leverage` / `set_margin_type` / `get_live_position` / `get_order` 全部搬走
- trader.py 顶部加 facade re-export（旧 import 不变）

**门禁**

- Characterization 全绿
- 测试网（`BINANCE_TESTNET=true`）跑 24h，日志无异常

**回滚**：`git revert` 即可。

### 9.4 Phase 2：binance/orders + trading/protective

**变更**

- 新建 `binance/orders.py`
- 新建 `trading/protective.py`（_build_protective / _place_protective / _emergency_close / _validate_sl_distance）
- 新建 `trading/pricing.py`（_round_quantity / _round_price）
- trader.py 相应函数移走

**门禁**

- Characterization 全绿（特别 `test_sl_placement_fail_triggers_emergency_close`）
- 测试网 24h

### 9.5 Phase 3：lifecycle/

**变更**

- 新建 `lifecycle/{sync,reconcile,expire,close}.py`
- 把 `sync_open_positions` / `reconcile_pending_entries` / `_reconcile_one_pending` / `_promote_pending` / `expire_open_positions` / `close_position` / `_record_closed_position` 搬走
- `scheduler.py` 改 import 路径

**门禁**

- Characterization 全绿
- 测试网 24h：sync/reconcile/expire 日志正常触发
- 关键指标：pending 单成交后 5-15s 内 promote（不超 30s）

### 9.6 Phase 4：trading/executor 拆 execute_trade

**变更**

- 新建 `trading/{executor,market_entry,limit_entry}.py`
- 拆 242 行 `execute_trade` → ~60 行 orchestrator + market/limit 各 ~80 行
- 新建 `domain/{enums,signal,position}.py`

**门禁**

- Characterization 全绿（特别 `test_market_entry_full_flow` / `test_limit_entry_pending_then_reconcile_promote`）
- 函数 ≥50 行报警（CI ruff/flake8 max-statements）
- 测试网 24h

### 9.7 Phase 5：ingest/ + 锁外 HTTP（高风险）

**变更**

- 新建 `ingest/{pipeline,guards,dispatcher}.py`
- 拆 229 行 `ingest_signals` → guard 链
- 核心改造：execute_trade 移到 DB write lock 外
- 新建 `signals_log.status=intent` 占位状态 + guard_position_exists 含 intent 检查

**风险点**

- 并发两条同 symbol 信号：guard 看到 intent → 第二条 skip
- intent 留存超时（>30s 未 trade/error）GC：lifecycle 加 5min cleanup

**门禁**

- Characterization 全绿 + 新增 5 个并发测试用例
- 灰度策略：feature flag `INGEST_LOCKLESS_EXECUTE`（默认 `false` = 旧路径锁内执行；`true` = 新路径锁外执行）
  - PR 合并后 24h 生产保持 `false`（旧路径）
  - 测试网设 `true` 跑 48h → 验证无重复开仓
  - 生产灰度：周末低交易时段设 `true`，紧盯 1h，异常立即设 `false` 回滚
- 必须双人 review

写一份 `docs/phase5-rollout.md` 详细步骤。

### 9.8 Phase 6：repos/ 拆 db.py

**变更**

- 新建 `repos/{connection,config_repo,signals_repo,positions_repo,pnl_repo}.py`
- db.py 改为 facade re-export
- 全代码改用 `from repos.signals_repo import SignalsRepo`

**门禁**

- Characterization 全绿
- 测试网 24h
- DB schema 不变，纯代码搬迁

### 9.9 Phase 7：observability/

**变更**

- 新建 `observability/{logging_setup,metrics,alerts}.py`
- 全代码替换 `logger = logging.getLogger(...)` → `logger = structlog.get_logger()`
- 日志调用改 keyword args 风格
- main.py 加 metrics endpoint `/metrics`
- 加 `naked_position_alerts` 表 migration
- env vars 加 7 个新配置

**门禁**

- Characterization 全绿（重点测告警 dedup）
- 配 `PROTOCOL_LOG_FORMAT=json` 人工 grep 关键事件
- Telegram/Discord/钉钉至少一个 webhook 实际验证
- Prometheus scrape /metrics 拿到数据

### 9.10 Phase 8：性能优化

**变更**

- 加 SQLite 索引 migration
- `config_repo.load_trading_config()` + TTL cache
- httpx limits 配置
- exchangeInfo TTL 调优（依据 Phase 7 命中率数据）

**门禁**

- Characterization 全绿
- benchmark：ingest 100 信号端到端时间下降（基线 vs 优化后）
- DB `EXPLAIN QUERY PLAN` 验证索引命中

### 9.11 Phase 9：测试补齐

**变更**

- 按 §7.3 覆盖率目标补 unit + integration
- CI 加 `--cov-fail-under=80`

**门禁**

- `pytest --cov=. --cov-fail-under=80` PASS
- Mutation testing（mutmut 可选）抽样 `trading/executor.py` ≥70% 杀死率

### 9.12 跨 Phase 灰度共通规则

1. 每 PR 单 phase，不混合（除 1-2 行 fix）
2. 每 phase 一个 git tag：`v1.1-phase1-binance-extracted`，方便回滚
3. CHANGELOG.md 每 phase 一节：新增模块 + 风险点 + rollback 步骤
4. 灰度顺序：本地 → 测试网 24h → 生产灰度
5. 生产灰度观察期：phase 1-4/6 = 24h，phase 5 = 72h（高风险），phase 7-9 = 24h
6. 回滚预案：每 phase PR description 写明回滚命令；facade re-export 保证 phase 1-6 可纯 revert
7. DB 兼容：Phase 1-7 不改 schema；Phase 7 加表 + Phase 8 加索引均 `IF NOT EXISTS` 向前兼容

---

## 10. 设计要点总结

1. **架构**：自底向上分 9 phase 拆分。binance/HTTP → trading/business → lifecycle/scheduler → ingest/pipeline → repos/DB → observability → perf → tests。
2. **行为不变**：Characterization tests 作为防回归网，覆盖现状所有关键路径。
3. **灰度友好**：每 phase facade re-export 保证旧 import 不变，可纯 revert 回滚。
4. **高风险 phase 单独处理**：Phase 5（锁外 HTTP）需 feature flag + 测试网 48h + 双人 review。
5. **错误归一化**：`ProtocolError` 体系，`code` 字段贯穿日志/metrics/DB。
6. **可观测性是一等公民**：结构化日志、Prometheus metrics、Webhook 告警三件套。
7. **性能优化最后做**：先确保结构正确，再依据 metrics 数据有针对性优化。
8. **测试最后补齐**：先有 characterization 保底，phase 9 系统性补 unit + integration 至 80%。

---

## 11. 待办（下一步）

1. 用户最终确认本设计
2. 调用 `superpowers:writing-plans` skill 生成详细实现 plan（Phase 0 起逐 phase 任务清单 + 验收命令）
3. 开始 Phase 0：建 characterization tests
