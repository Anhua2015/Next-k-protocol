# Protocol Live Positions Without Local Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert `Next-k-protocol` into a lightweight execution gateway that serves current positions directly from Binance, drops the local `positions` ledger and lifecycle manager, and requires callers to send `margin_usdt` on every entry request.

**Architecture:** `Next-k-protocol` keeps only global config and execution logs in SQLite. Current positions and account summary come straight from Binance REST. `next-k-api` adapts its callers to send `margin_usdt`, and `next-k-frontend` removes history/PnL assumptions that depended on protocol’s local positions ledger.

**Tech Stack:** FastAPI, Pydantic, SQLite, httpx, Binance USD-M Futures REST, static HTML/JS frontend, pytest

---

## File Map

### `Next-k-protocol`

- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/models.py`
  - Simplify request/response models around global config, live positions, and execution logs.
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/repos/connection.py`
  - Remove `margin_usdt`, `src_*`, `play*`, expire, and other strategy-specific defaults.
  - Stop creating the `positions` table in new DBs.
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/repos/config_repo.py`
  - Remove fallback helpers that exist only for strategy-specific config.
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/router.py`
  - Rework `/positions`, `/signals`, `/signals/ingest`, `/config`, `/account/summary`.
  - Remove/downline position-id and `/pnl/*` endpoints.
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/trader.py`
  - Read `margin_usdt` from the request body, not config.
  - Use global `entry_type` only.
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/ingest/guards.py`
  - Remove strategy-specific guards and source-specific config assumptions.
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/ingest/dispatcher.py`
  - Pass `margin_usdt` through to execution and simplify result handling.
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/main.py`
  - Stop registering scheduler lifecycle tasks.
- Delete: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/repos/positions_repo.py`
- Delete: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/lifecycle/sync.py`
- Delete: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/lifecycle/reconcile.py`
- Delete: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/lifecycle/expire.py`
- Delete: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/lifecycle/close.py`
- Delete: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/repos/pnl_repo.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/db.py`
  - Remove re-exports for deleted repos/functions.
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/README.md`
  - Update service description, config docs, and API docs.

### `Next-k-api`

- Modify: `/Users/liuketao/MyDisk/project/OImode_f/next-k-api/moss_quant/protocol_client.py`
  - Send `margin_usdt` instead of `notional_usdt` for open/rolling.
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/next-k-api/moss_quant/signal_sender.py`
  - Keep the public helper API aligned with the new protocol client semantics.
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/next-k-api/momentum_scanner.py`
  - Send `margin_usdt` explicitly for momentum entries.
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/next-k-api/jiezhen_signals.py` or the actual protocol call site discovered during implementation
  - Send `margin_usdt` explicitly for jiezhen entries.
- Modify: any remaining call sites found by `rg "notional_usdt.*signals/ingest|signals/ingest"` to pass `margin_usdt`.

### `next-k-frontend`

- Modify: `/Users/liuketao/MyDisk/project/OImode_f/next-k-frontend/binance.html`
  - Remove history/PnL panels and strategy-specific config fields.
  - Adapt current positions panel to the new live-only response shape.
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/next-k-frontend/index.html`
  - Remove any protocol-history assumptions in Moss-related admin views that read `binance.html` APIs.

### Tests

- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/conftest.py`
  - Update seeded config to drop `margin_usdt` defaults and obsolete strategy config.
- Modify or replace:
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/characterization/test_execute_market_entry.py`
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/characterization/test_execute_limit_entry.py`
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/characterization/test_ingest_guards.py`
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/characterization/test_moss_quant.py`
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/characterization/test_account_summary.py`
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/characterization/test_moss_live_positions.py`
- Delete tests that only cover removed lifecycle/history features:
  - `test_lifecycle_sync.py`
  - `test_lifecycle_reconcile.py`
  - `test_lifecycle_expire.py`
  - `test_close_endpoint.py`
  - `test_moss_live_lifecycle_events.py`
  - any `/pnl/*` characterization files found during implementation
- Modify:
  - `/Users/liuketao/MyDisk/project/OImode_f/next-k-api/tests/test_moss_quant_live_protocol.py`
  - other protocol-client tests that assert `notional_usdt`

---

### Task 1: Simplify Protocol Data Model and Config Surface

**Files:**
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/models.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/repos/connection.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/repos/config_repo.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/conftest.py`
- Test: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/characterization/test_account_summary.py`

- [ ] **Step 1: Write the failing config/model test**

```python
def test_signal_item_requires_margin_usdt(client):
    resp = client.post(
        "/api/binance/signals/ingest",
        json={"signals": [{
            "api_signal_id": "sig-1",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "sl_price": 65000.0,
        }]},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "margin_usdt" in resp.text


def test_global_config_no_longer_contains_margin_or_strategy_keys(seeded_config):
    import db
    cfg = db.get_all_config()
    assert "margin_usdt" not in cfg
    assert "src_momentum_enabled" not in cfg
    assert "src_moss_quant_leverage" not in cfg
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol
./.venv/bin/python -m pytest tests/characterization/test_account_summary.py -v
```

Expected: FAIL because `SignalItem` still allows `notional_usdt`/missing `margin_usdt`, and config still seeds strategy keys.

- [ ] **Step 3: Write minimal model/config implementation**

Update `SignalItem` in `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/models.py` to remove strategy fields and require `margin_usdt`:

```python
class SignalItem(BaseModel):
    api_signal_id: str = Field(..., description="调用方请求唯一 ID")
    symbol: str = Field(..., description="交易对，例如 BTCUSDT")
    side: str = Field(..., pattern="^(LONG|SHORT)$")
    margin_usdt: float = Field(..., gt=0, description="本次交易保证金（USDT）")
    entry_price: Optional[float] = Field(None, description="LIMIT 模式下的挂单价格")
    sl_price: Optional[float] = Field(None, description="可选止损价格")
    tp_price: Optional[float] = Field(None, description="可选止盈价格")
    client_ref: Optional[str] = Field(None, description="调用方请求引用")
```

Shrink `DEFAULT_CONFIG` in `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/repos/connection.py` to:

```python
DEFAULT_CONFIG: Dict[str, str] = {
    "enabled": "false",
    "testnet": "false",
    "leverage": "10",
    "entry_type": "MARKET",
    "max_positions": "8",
}
```

Drop `margin_usdt` fallback from `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/repos/config_repo.py`:

```python
def get_config(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] else default
```

Update seeded config in `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/conftest.py`:

```python
fresh_db.set_config_batch({
    "enabled": "true",
    "testnet": "true",
    "leverage": "10",
    "entry_type": "MARKET",
    "max_positions": "8",
    "binance_api_key": "test-key",
    "binance_api_secret": "test-secret",
})
```

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol
./.venv/bin/python -m pytest tests/characterization/test_account_summary.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol
git add models.py repos/connection.py repos/config_repo.py tests/conftest.py tests/characterization/test_account_summary.py
git commit -m "refactor: require margin_usdt and trim protocol config"
```

### Task 2: Make `/positions` a Binance Live View and Remove Position/PnL Endpoints

**Files:**
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/binance/account.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/router.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/models.py`
- Delete: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/repos/positions_repo.py`
- Delete: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/repos/pnl_repo.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/db.py`
- Test: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/characterization/test_execute_market_entry.py`
- Test: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/characterization/test_moss_live_positions.py`

- [ ] **Step 1: Write the failing live-positions tests**

```python
def test_positions_open_reads_binance_position_risk(client, mock_binance):
    mock_binance.all(position_risk="position_risk_open")
    resp = client.get("/api/binance/positions?status=open", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["symbol"] == "BTCUSDT"
    assert "profile_id" not in body[0]


def test_positions_closed_is_gone(client):
    resp = client.get("/api/binance/positions?status=closed", headers=AUTH)
    assert resp.status_code == 410


def test_pnl_summary_endpoint_removed(client):
    resp = client.get("/api/binance/pnl/summary", headers=AUTH)
    assert resp.status_code in (404, 410)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol
./.venv/bin/python -m pytest tests/characterization/test_execute_market_entry.py tests/characterization/test_moss_live_positions.py -v
```

Expected: FAIL because `/positions` still reads SQLite and closed/PnL endpoints still exist.

- [ ] **Step 3: Write minimal live-position implementation**

Add list helper in `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/binance/account.py`:

```python
def list_live_positions(client: BinanceClient) -> list[dict]:
    rows = client.request("GET", "/fapi/v2/positionRisk")
    out = []
    for row in rows:
        amt = float(row.get("positionAmt") or 0)
        if amt == 0:
            continue
        side = "LONG" if amt > 0 else "SHORT"
        out.append({
            "symbol": row["symbol"],
            "side": side,
            "quantity": abs(amt),
            "entry_price": float(row.get("entryPrice") or 0),
            "mark_price": float(row.get("markPrice") or 0),
            "unrealized_pnl_usdt": float(row.get("unRealizedProfit") or 0),
            "leverage": int(float(row.get("leverage") or 0)),
            "liquidation_price": float(row.get("liquidationPrice") or 0),
            "margin_type": str(row.get("marginType") or "").upper(),
        })
    return out
```

Update `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/router.py`:

```python
@router.get("/positions", response_model=List[LivePositionOut], dependencies=[Depends(require_auth)])
async def list_positions(status: Optional[str] = Query(None), limit: int = Query(100, ge=1, le=1000)):
    if status in ("closed", "pending_entry", "cancelled_pending"):
        raise HTTPException(status_code=410, detail="position_history_removed")
    rows = list_live_positions()
    return rows[:limit]
```

Remove `/positions/{position_id}` and `/pnl/*` route definitions entirely. Delete re-exports in `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/db.py` that only exist for `positions_repo` / `pnl_repo`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol
./.venv/bin/python -m pytest tests/characterization/test_execute_market_entry.py tests/characterization/test_moss_live_positions.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol
git add binance/account.py router.py models.py db.py tests/characterization/test_execute_market_entry.py tests/characterization/test_moss_live_positions.py
git rm repos/positions_repo.py repos/pnl_repo.py
git commit -m "refactor: serve current positions from binance only"
```

### Task 3: Remove Lifecycle Manager and Make Ingest a Fire-and-Forget Executor

**Files:**
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/trader.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/ingest/guards.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/ingest/dispatcher.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/main.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/router.py`
- Delete:
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/lifecycle/sync.py`
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/lifecycle/reconcile.py`
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/lifecycle/expire.py`
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/lifecycle/close.py`
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/scheduler.py`
- Test:
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/characterization/test_execute_market_entry.py`
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/characterization/test_execute_limit_entry.py`
  - `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/tests/characterization/test_ingest_guards.py`

- [ ] **Step 1: Write the failing ingest/lifecycle tests**

```python
def test_market_signal_uses_request_margin(client, mock_binance):
    mock_binance.all()
    resp = client.post("/api/binance/signals/ingest", json={
        "signals": [{
            "api_signal_id": "m-001",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "margin_usdt": 125.0,
            "sl_price": 65000.0,
        }]
    }, headers=AUTH)
    assert resp.json()["traded"] == 1


def test_limit_signal_is_logged_as_submitted(client, mock_binance, seeded_config):
    seeded_config.set_config("entry_type", "LIMIT")
    mock_binance.all(place_order="place_order_limit_ack")
    resp = client.post("/api/binance/signals/ingest", json={
        "signals": [{
            "api_signal_id": "l-001",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "margin_usdt": 125.0,
            "entry_price": 67000.0,
        }]
    }, headers=AUTH)
    assert resp.json()["details"][0]["action"] == "submitted"
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol
./.venv/bin/python -m pytest tests/characterization/test_execute_market_entry.py tests/characterization/test_execute_limit_entry.py tests/characterization/test_ingest_guards.py -v
```

Expected: FAIL because execution still reads config `margin_usdt`, still writes position rows, and LIMIT still produces pending semantics.

- [ ] **Step 3: Write minimal execution implementation**

In `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/trader.py`, replace source-specific sizing with request `margin_usdt`:

```python
margin = float(signal.get("margin_usdt") or 0)
leverage = int(get_config("leverage", "10"))
if margin <= 0 or leverage <= 0:
    update_signal_status(signal_log_id, "error", f"invalid margin={margin} leverage={leverage}")
    return False
```

For LIMIT requests, record `submitted` in the dispatcher result instead of `pending_entry`, and do not write any local position row. In `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/ingest/dispatcher.py`:

```python
detail = {
    "api_signal_id": sig.api_signal_id,
    "symbol": sig.symbol,
    "side": sig.side,
    "action": "traded" if entry_type == "MARKET" else "submitted",
}
```

Delete scheduler startup in `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/main.py`:

```python
# remove register_jobs(scheduler)
```

Simplify `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/ingest/guards.py` by removing source-specific and position-ledger guards; keep only:

```python
GUARDS = [
    guard_trading_disabled,
    guard_dedup_insert,
    guard_max_positions,
]
```

Implement `guard_max_positions` against Binance live count by calling the live positions helper instead of `count_open_total()`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol
./.venv/bin/python -m pytest tests/characterization/test_execute_market_entry.py tests/characterization/test_execute_limit_entry.py tests/characterization/test_ingest_guards.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol
git add trader.py ingest/guards.py ingest/dispatcher.py main.py router.py tests/characterization/test_execute_market_entry.py tests/characterization/test_execute_limit_entry.py tests/characterization/test_ingest_guards.py
git rm lifecycle/sync.py lifecycle/reconcile.py lifecycle/expire.py lifecycle/close.py scheduler.py
git commit -m "refactor: remove local position lifecycle manager"
```

### Task 4: Adapt `next-k-api` to Send `margin_usdt`

**Files:**
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/next-k-api/moss_quant/protocol_client.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/next-k-api/moss_quant/signal_sender.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/next-k-api/momentum_scanner.py`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/next-k-api/jiezhen_signals.py` (or discovered protocol call site)
- Modify: any remaining sender in `next-k-api` found during implementation
- Test: `/Users/liuketao/MyDisk/project/OImode_f/next-k-api/tests/test_moss_quant_live_protocol.py`

- [ ] **Step 1: Write the failing client test**

```python
def test_protocol_client_send_open_posts_margin_usdt(monkeypatch):
    from moss_quant.protocol_client import ProtocolClient
    captured = {}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"ok": True}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return FakeResp()

    monkeypatch.setattr("httpx.post", fake_post)
    client = ProtocolClient("http://localhost:8001", "tok")
    client.send_open(symbol="BTCUSDT", side="LONG", entry_price=65000, sl_price=64000, tp_price=68000, margin_usdt=100, client_ref="c1")
    signal = captured["json"]["signals"][0]
    assert signal["margin_usdt"] == 100
    assert "notional_usdt" not in signal
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/next-k-api
./.venv/bin/python -m pytest tests/test_moss_quant_live_protocol.py -v
```

Expected: FAIL because protocol client still emits `notional_usdt`.

- [ ] **Step 3: Write minimal client/call-site implementation**

In `/Users/liuketao/MyDisk/project/OImode_f/next-k-api/moss_quant/protocol_client.py`, change method signatures and payload:

```python
def send_open(..., margin_usdt: float, client_ref: str = "", action: str = "open") -> Dict[str, Any]:
    signal = {
        "api_signal_id": client_ref or f"signal:{int(time.time() * 1000)}",
        "symbol": symbol,
        "side": side,
        "margin_usdt": round(margin_usdt, 2),
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "client_ref": client_ref,
    }
```

At each sender call site, convert notional to margin before calling:

```python
margin_usdt = round(notional / protocol_leverage, 2)
open_resp = sender.send_open(
    symbol=symbol,
    side=side,
    entry_price=mark,
    sl_price=round(sl_price, 6),
    tp_price=round(tp_price, 6),
    margin_usdt=margin_usdt,
    client_ref=f"moss:{pid}:open:{int(time.time() * 1000)}",
)
```

Do the same for rolling:

```python
margin_usdt = round(add_notional / lev, 2)
sender.send_rolling(..., margin_usdt=margin_usdt, ...)
```

Update momentum/jiezhen call sites to send their existing sizing as `margin_usdt`.

- [ ] **Step 4: Run test to verify it passes**

Run:

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/next-k-api
./.venv/bin/python -m pytest tests/test_moss_quant_live_protocol.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/next-k-api
git add moss_quant/protocol_client.py moss_quant/signal_sender.py momentum_scanner.py jiezhen_signals.py tests/test_moss_quant_live_protocol.py
git commit -m "refactor: send margin_usdt to protocol"
```

### Task 5: Remove Protocol History/PnL Assumptions from `next-k-frontend`

**Files:**
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/next-k-frontend/binance.html`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/next-k-frontend/index.html`

- [ ] **Step 1: Write the failing browser/manual expectations**

Document the behavior to verify after implementation:

```text
1. binance.html no longer renders history positions or pnl summary sections backed by /api/binance/pnl/*
2. current positions table loads from /api/binance/positions without closed/history filters
3. config editor no longer exposes strategy-specific protocol keys
4. no code path fetches /api/binance/pnl/summary or /api/binance/positions?status=closed
```

- [ ] **Step 2: Run quick search to verify old assumptions still exist**

Run:

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/next-k-frontend
rg -n "/api/binance/pnl/summary|status=closed|src_momentum_|src_jiezhen_|src_moss_" binance.html index.html
```

Expected: matches found

- [ ] **Step 3: Write minimal frontend implementation**

In `/Users/liuketao/MyDisk/project/OImode_f/next-k-frontend/binance.html`, remove strategy-specific config inputs and history/PnL fetches. Keep only global config inputs like:

```javascript
var EDITABLE_KEYS = {
  global: [
    { key: 'enabled', label: '启用交易', hint: 'true / false' },
    { key: 'testnet', label: '测试网', hint: 'true / false' },
    { key: 'leverage', label: '杠杆', hint: '10' },
    { key: 'entry_type', label: '入场单类型', hint: 'MARKET', type: 'select', options: ['MARKET', 'LIMIT'] },
    { key: 'max_positions', label: '最大持仓', hint: '8' },
  ],
};
```

Change current positions loading to:

```javascript
var rows = await apiFetch('/api/binance/positions?limit=50');
```

Delete any renderer that expects `closed_at`, `pnl_usdt`, `profile_id`, `source`, or `/api/binance/pnl/summary`.

- [ ] **Step 4: Run syntax checks and verify searches are clean**

Run:

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/next-k-frontend
perl -0ne 'while(/<script(?:\\s[^>]*)?>(.*?)<\\/script>/sg){print $1,"\\n"}' binance.html | node --check /dev/stdin
perl -0ne 'while(/<script(?:\\s[^>]*)?>(.*?)<\\/script>/sg){print $1,"\\n"}' index.html | node --check /dev/stdin
rg -n "/api/binance/pnl/summary|status=closed|src_momentum_|src_jiezhen_|src_moss_" binance.html index.html
```

Expected: syntax check passes; `rg` returns no matches tied to removed protocol features.

- [ ] **Step 5: Commit**

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/next-k-frontend
git add binance.html index.html
git commit -m "refactor: align frontend with live-only protocol"
```

### Task 6: Clean Up Protocol Docs and Delete Obsolete Tests

**Files:**
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/README.md`
- Modify: `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/BINANCE.md`
- Delete obsolete characterization tests discovered during Task 2/3

- [ ] **Step 1: Write the failing doc consistency checklist**

```text
README/BINANCE must stop claiming:
- protocol stores position history
- /api/binance/pnl/* exists
- strategy-specific config keys exist
- margin_usdt is a global config
- LIMIT orders are lifecycle-managed by protocol
```

- [ ] **Step 2: Run search to verify outdated docs still exist**

Run:

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol
rg -n "margin_usdt|/api/binance/pnl|positions/\\{|pending_entry|src_momentum_|src_moss_|expire_open_positions|reconcile_pending" README.md BINANCE.md tests
```

Expected: matches found

- [ ] **Step 3: Update docs and delete obsolete tests**

Revise README/BINANCE to describe:

```text
- protocol keeps only global config and signals_log
- current positions come directly from Binance
- callers must send margin_usdt per request
- LIMIT orders are submit-only; protocol does not manage their lifecycle
```

Delete characterization files that only assert removed behavior:

```bash
git rm tests/characterization/test_lifecycle_sync.py
git rm tests/characterization/test_lifecycle_reconcile.py
git rm tests/characterization/test_lifecycle_expire.py
git rm tests/characterization/test_close_endpoint.py
git rm tests/characterization/test_moss_live_lifecycle_events.py
```

- [ ] **Step 4: Run targeted regression suite**

Run:

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol
./.venv/bin/python -m pytest tests/characterization/test_account_summary.py tests/characterization/test_execute_market_entry.py tests/characterization/test_execute_limit_entry.py tests/characterization/test_ingest_guards.py tests/characterization/test_moss_quant.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol
git add README.md BINANCE.md
git commit -m "docs: document live-only protocol behavior"
```

## Self-Review

- Spec coverage:
  - Live Binance-backed current positions: Task 2
  - Remove local positions ledger/history/PnL/lifecycle: Tasks 2, 3, 6
  - Global config only: Task 1, Task 5
  - Caller-supplied `margin_usdt`: Tasks 1, 3, 4
  - Frontend/API compatibility changes: Tasks 4, 5
- Placeholder scan:
  - No `TODO` / `TBD`
  - Each task includes concrete files, commands, and code snippets
- Type consistency:
  - Request body uses `margin_usdt` consistently
  - Live positions response uses `LivePositionOut`-style fields consistently
  - `signals_log.status` uses `received/submitted/traded/error/skipped_*`

## Execution Handoff

Plan complete and saved to `/Users/liuketao/MyDisk/project/OImode_f/Next-k-protocol/docs/superpowers/plans/2026-05-30-protocol-live-positions-without-local-ledger.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
