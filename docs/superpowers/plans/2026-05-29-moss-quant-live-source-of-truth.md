# Moss Quant Live Source Of Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Moss Quant use `Next-k-protocol` as the live trading source of truth while `next-k-api` remains the strategy decision layer and both dashboards show real positions, balances, and event logs grouped by strategy.

**Architecture:** `Next-k-protocol` owns real account balance, leverage, positions, lifecycle events, and strategy-filtered signal logs. `next-k-api` calls protocol before and during every Moss scan, sizes entries from live balance divided by enabled Moss profiles, sends action events with `profile_id/client_ref`, and exposes backward-compatible Moss APIs that aggregate protocol truth for `index.html`. `next-k-frontend/binance.html` and `index.html` display live data without mixing old paper data into main statistics.

**Tech Stack:** FastAPI, Pydantic, SQLite WAL, pytest, httpx, Binance Futures REST wrappers, static HTML/JavaScript.

---

## File Structure

Protocol files:

- `Next-k-protocol/repos/connection.py` owns SQLite DDL, migrations, and indexes for `signals_log` and `positions`.
- `Next-k-protocol/models.py` owns API request and response shapes for signals, positions, signal logs, and account summary.
- `Next-k-protocol/repos/signals_repo.py` owns signal/event insert, status update, and filtered signal-log queries.
- `Next-k-protocol/repos/positions_repo.py` owns position insert and filtered position queries.
- `Next-k-protocol/binance/account.py` owns signed account summary reads from Binance.
- `Next-k-protocol/trader.py` exposes account summary wrapper and keeps Moss leverage sourced from `src_moss_quant_leverage`.
- `Next-k-protocol/router.py` exposes account summary, filtered signal logs, filtered positions, close, and update SL endpoints.
- `Next-k-protocol/ingest/guards.py` persists `profile_id/client_ref/action` during ingest.
- `Next-k-protocol/ingest/dispatcher.py` passes Moss reference fields into execution.
- `Next-k-protocol/trading/market_entry.py` and `Next-k-protocol/trading/limit_entry.py` persist reference fields on positions.
- `Next-k-protocol/lifecycle/close.py`, `sync.py`, and `reconcile.py` write action events for close, exchange SL/TP, external close, pending cancellation, and promote failures.

next-k-api files:

- `next-k-api/moss_quant/protocol_client.py` is the single HTTP client for protocol account, positions, and Moss actions.
- `next-k-api/moss_quant/signal_sender.py` becomes a compatibility wrapper around `protocol_client.py`.
- `next-k-api/moss_quant/paper_scanner.py` keeps the existing scheduler entrypoint but reads protocol truth and live sizing.
- `next-k-api/moss_quant/db.py` adds helpers to mark old local open records as externally closed or link-pending.
- `next-k-api/routers/moss_quant.py` returns live summary, signals, and latest scan payloads while preserving existing response field names.
- `next-k-api/tests/test_moss_quant_live_protocol.py` covers sizing, failure behavior, real-position mapping, and API aggregation.

Frontend files:

- `next-k-frontend/index.html` renames Moss Paper UI to Live and consumes live-mode fields from existing Moss endpoints.
- `next-k-frontend/binance.html` adds strategy filters/tabs for historical positions and signal logs and renders action/profile/reference fields.

---

### Task 1: Protocol Schema And API Models

**Files:**
- Modify: `Next-k-protocol/repos/connection.py`
- Modify: `Next-k-protocol/models.py`
- Test: `Next-k-protocol/tests/characterization/test_moss_live_schema.py`

- [ ] **Step 1: Write the failing schema/model tests**

Create `Next-k-protocol/tests/characterization/test_moss_live_schema.py`:

```python
from __future__ import annotations

import pytest

pytestmark = pytest.mark.characterization


def test_live_reference_columns_exist(seeded_config):
    import db

    with db.get_db() as conn:
        signal_cols = {r["name"] for r in conn.execute("PRAGMA table_info(signals_log)").fetchall()}
        position_cols = {r["name"] for r in conn.execute("PRAGMA table_info(positions)").fetchall()}

    assert {"profile_id", "client_ref", "action", "position_id", "payload_json", "result_json"} <= signal_cols
    assert {"profile_id", "client_ref"} <= position_cols


def test_signal_model_accepts_moss_live_fields():
    from models import SignalItem

    item = SignalItem(
        source="moss_quant",
        api_signal_id="moss-7-open-1",
        symbol="BTCUSDT",
        side="LONG",
        sl_price=65000,
        tp_price=70000,
        notional_usdt=250,
        play="balanced",
        profile_id=7,
        client_ref="moss:7:open:1700000000000",
        action="open",
    )

    assert item.profile_id == 7
    assert item.client_ref == "moss:7:open:1700000000000"
    assert item.action == "open"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
cd Next-k-protocol
python -m pytest tests/characterization/test_moss_live_schema.py -v
```

Expected: FAIL because new columns and Pydantic fields do not exist.

- [ ] **Step 3: Add DDL columns and migrations**

In `Next-k-protocol/repos/connection.py`, extend `signals_log` and `positions` DDL:

```python
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

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_log_id   INTEGER,
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    entry_order_id  TEXT,
    sl_order_id     TEXT,
    tp_order_id     TEXT,
    entry_price     REAL,
    sl_price        REAL,
    tp_price        REAL,
    quantity        REAL,
    notional_usdt   REAL,
    leverage        INTEGER DEFAULT 1,
    opened_at       TEXT    NOT NULL,
    expire_at       TEXT,
    status          TEXT    NOT NULL DEFAULT 'open',
    close_reason    TEXT,
    close_price     REAL,
    closed_at       TEXT,
    pnl_usdt        REAL,
    pnl_pct         REAL,
    play            TEXT    DEFAULT '',
    source          TEXT    DEFAULT '',
    entry_deadline  TEXT,
    profile_id      INTEGER,
    client_ref      TEXT    DEFAULT ''
);
```

In `_PERF_INDEXES`, add:

```python
"CREATE INDEX IF NOT EXISTS idx_signals_log_source_action ON signals_log(source, action, status)",
"CREATE INDEX IF NOT EXISTS idx_signals_log_profile ON signals_log(source, profile_id)",
"CREATE INDEX IF NOT EXISTS idx_positions_source_profile_status ON positions(source, profile_id, status)",
```

In `init_db()`, add idempotent migrations:

```python
for table, column, ddl in [
    ("signals_log", "profile_id", "ALTER TABLE signals_log ADD COLUMN profile_id INTEGER"),
    ("signals_log", "client_ref", "ALTER TABLE signals_log ADD COLUMN client_ref TEXT DEFAULT ''"),
    ("signals_log", "action", "ALTER TABLE signals_log ADD COLUMN action TEXT DEFAULT 'open'"),
    ("signals_log", "position_id", "ALTER TABLE signals_log ADD COLUMN position_id INTEGER"),
    ("signals_log", "payload_json", "ALTER TABLE signals_log ADD COLUMN payload_json TEXT"),
    ("signals_log", "result_json", "ALTER TABLE signals_log ADD COLUMN result_json TEXT"),
    ("positions", "profile_id", "ALTER TABLE positions ADD COLUMN profile_id INTEGER"),
    ("positions", "client_ref", "ALTER TABLE positions ADD COLUMN client_ref TEXT DEFAULT ''"),
]:
    try:
        conn.execute(ddl)
        logger.info("migrated: %s.%s column added", table, column)
    except Exception:
        pass
```

- [ ] **Step 4: Add model fields**

In `Next-k-protocol/models.py`, extend `SignalItem`:

```python
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
```

Extend `ClosePositionRequest`:

```python
    profile_id: Optional[int] = Field(None, description="Moss Quant Profile ID")
    client_ref: Optional[str] = Field(None, description="调用方动作引用 ID")
```

Extend `UpdateSlRequest`:

```python
    profile_id: Optional[int] = Field(None, description="Moss Quant Profile ID")
    client_ref: Optional[str] = Field(None, description="调用方动作引用 ID")
```

Extend `PositionOut`:

```python
    profile_id: Optional[int] = Field(None, description="Moss Quant Profile ID")
    client_ref: Optional[str] = Field(None, description="调用方动作引用 ID")
```

Extend `SignalLogOut`:

```python
    play: Optional[str] = Field(None, description="策略子类型")
    profile_id: Optional[int] = Field(None, description="Moss Quant Profile ID")
    client_ref: Optional[str] = Field(None, description="调用方动作引用 ID")
    action: Optional[str] = Field(None, description="动作类型")
    position_id: Optional[int] = Field(None, description="关联持仓 ID")
    payload_json: Optional[str] = Field(None, description="动作请求快照 JSON")
    result_json: Optional[str] = Field(None, description="动作结果快照 JSON")
```

- [ ] **Step 5: Run tests and commit**

Run:

```bash
cd Next-k-protocol
python -m pytest tests/characterization/test_moss_live_schema.py -v
```

Expected: PASS.

Commit:

```bash
git add repos/connection.py models.py tests/characterization/test_moss_live_schema.py
git commit -m "feat: add Moss live reference schema"
```

---

### Task 2: Protocol Signal Event Logging And Filters

**Files:**
- Modify: `Next-k-protocol/repos/signals_repo.py`
- Modify: `Next-k-protocol/db.py`
- Modify: `Next-k-protocol/ingest/guards.py`
- Modify: `Next-k-protocol/router.py`
- Test: `Next-k-protocol/tests/characterization/test_moss_live_signal_events.py`

- [ ] **Step 1: Write failing event logging tests**

Create `Next-k-protocol/tests/characterization/test_moss_live_signal_events.py`:

```python
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.characterization


def test_insert_signal_persists_live_fields(seeded_config):
    import db

    sid = db.insert_signal(
        source="moss_quant",
        api_signal_id="moss-8-open-1",
        symbol="ETHUSDT",
        side="LONG",
        entry_price=3000,
        sl_price=2900,
        tp_price=3300,
        confidence=None,
        regime="TREND_UP",
        notional_usdt=400,
        received_at="2026-05-29T00:00:00Z",
        play="balanced",
        profile_id=8,
        client_ref="moss:8:open:1",
        action="open",
    )

    assert sid is not None
    rows = db.list_signals(source="moss_quant", action="open", profile_id=8, limit=10)
    assert len(rows) == 1
    assert rows[0]["profile_id"] == 8
    assert rows[0]["client_ref"] == "moss:8:open:1"
    assert rows[0]["action"] == "open"


def test_log_trade_event_records_non_open_actions(seeded_config):
    import db

    event_id = db.log_trade_event(
        source="moss_quant",
        action="update_sl",
        symbol="BTCUSDT",
        side="LONG",
        api_signal_id="moss-9-update-sl-1",
        status="traded",
        profile_id=9,
        position_id=77,
        client_ref="moss:9:update_sl:1",
        payload={"new_sl_price": 65000},
        result={"ok": True},
    )

    rows = db.list_signals(source="moss_quant", action="update_sl", profile_id=9, limit=10)
    assert rows[0]["id"] == event_id
    assert rows[0]["position_id"] == 77
    assert json.loads(rows[0]["payload_json"])["new_sl_price"] == 65000
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
cd Next-k-protocol
python -m pytest tests/characterization/test_moss_live_signal_events.py -v
```

Expected: FAIL because `insert_signal` lacks live args and `log_trade_event` does not exist.

- [ ] **Step 3: Extend signals repo**

In `Next-k-protocol/repos/signals_repo.py`, add imports:

```python
import json
from binance.time_sync import now_utc
```

Extend `insert_signal` signature:

```python
    profile_id: Optional[int] = None,
    client_ref: Optional[str] = None,
    action: Optional[str] = "open",
    position_id: Optional[int] = None,
    payload_json: Optional[str] = None,
    result_json: Optional[str] = None,
) -> Optional[int]:
```

Replace the insert SQL with:

```python
                """INSERT INTO signals_log
                   (source, api_signal_id, symbol, side, entry_price, sl_price,
                    tp_price, confidence, regime, notional_usdt, received_at,
                    status, skip_reason, play, profile_id, client_ref, action,
                    position_id, payload_json, result_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source, api_signal_id, symbol, side, entry_price, sl_price,
                    tp_price, confidence, regime, notional_usdt, received_at,
                    status, skip_reason, play, profile_id, client_ref or "",
                    action or "open", position_id, payload_json, result_json,
                ),
```

Replace `list_signals` with:

```python
def list_signals(
    limit: int = 100,
    offset: int = 0,
    source: Optional[str] = None,
    action: Optional[str] = None,
    status: Optional[str] = None,
    profile_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    vals: List[Any] = []
    if source:
        clauses.append("source=?")
        vals.append(source)
    if action:
        clauses.append("action=?")
        vals.append(action)
    if status:
        clauses.append("status=?")
        vals.append(status)
    if profile_id is not None:
        clauses.append("profile_id=?")
        vals.append(int(profile_id))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    vals.extend([limit, offset])
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM signals_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            vals,
        ).fetchall()
    return [dict(r) for r in rows]
```

Add `log_trade_event`:

```python
def log_trade_event(
    *,
    source: str,
    action: str,
    symbol: str,
    side: str,
    api_signal_id: str,
    status: str,
    profile_id: Optional[int] = None,
    position_id: Optional[int] = None,
    client_ref: Optional[str] = None,
    entry_price: Optional[float] = None,
    sl_price: Optional[float] = None,
    tp_price: Optional[float] = None,
    notional_usdt: Optional[float] = None,
    play: Optional[str] = None,
    skip_reason: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    return insert_signal(
        source=source,
        api_signal_id=api_signal_id,
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        confidence=None,
        regime=None,
        notional_usdt=notional_usdt,
        received_at=now_utc(),
        status=status,
        skip_reason=skip_reason,
        play=play,
        profile_id=profile_id,
        client_ref=client_ref,
        action=action,
        position_id=position_id,
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
        result_json=json.dumps(result or {}, ensure_ascii=False),
    )
```

- [ ] **Step 4: Export and wire filters**

In `Next-k-protocol/db.py`, export `log_trade_event`:

```python
from repos.signals_repo import (
    insert_signal,
    list_signals,
    log_trade_event,
    update_status as update_signal_status,
)
```

In `Next-k-protocol/ingest/guards.py`, pass live fields into `ctx.db.insert_signal`:

```python
        play=sig.play or "",
        profile_id=sig.profile_id,
        client_ref=sig.client_ref or "",
        action=sig.action or ("rolling" if "rolling" in (sig.play or "").lower() else "open"),
```

In `Next-k-protocol/router.py`, extend `list_signals` parameters and call:

```python
async def list_signals(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    source: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    profile_id: Optional[int] = Query(None),
):
    rows = _db.list_signals(
        limit=limit,
        offset=offset,
        source=source,
        action=action,
        status=status,
        profile_id=profile_id,
    )
    return rows
```

- [ ] **Step 5: Run tests and commit**

Run:

```bash
cd Next-k-protocol
python -m pytest tests/characterization/test_moss_live_signal_events.py tests/unit/test_guards.py -v
```

Expected: PASS.

Commit:

```bash
git add repos/signals_repo.py db.py ingest/guards.py router.py tests/characterization/test_moss_live_signal_events.py
git commit -m "feat: log strategy trade events"
```

---

### Task 3: Protocol Position References And Filters

**Files:**
- Modify: `Next-k-protocol/repos/positions_repo.py`
- Modify: `Next-k-protocol/db.py`
- Modify: `Next-k-protocol/ingest/dispatcher.py`
- Modify: `Next-k-protocol/trading/market_entry.py`
- Modify: `Next-k-protocol/trading/limit_entry.py`
- Modify: `Next-k-protocol/lifecycle/reconcile.py`
- Modify: `Next-k-protocol/router.py`
- Test: `Next-k-protocol/tests/characterization/test_moss_live_positions.py`

- [ ] **Step 1: Write failing position reference tests**

Create `Next-k-protocol/tests/characterization/test_moss_live_positions.py`:

```python
from __future__ import annotations

import pytest

pytestmark = pytest.mark.characterization


def test_position_filters_by_source_profile_and_status(seeded_config):
    import db

    db.insert_position(
        signal_log_id=1,
        symbol="BTCUSDT",
        side="LONG",
        entry_order_id="e1",
        sl_order_id="s1",
        tp_order_id="t1",
        entry_price=65000,
        sl_price=64000,
        tp_price=68000,
        quantity=0.01,
        notional_usdt=650,
        leverage=10,
        opened_at="2026-05-29T00:00:00Z",
        play="balanced",
        source="moss_quant",
        profile_id=12,
        client_ref="moss:12:open:1",
    )
    db.insert_position(
        signal_log_id=2,
        symbol="ETHUSDT",
        side="SHORT",
        entry_order_id="e2",
        sl_order_id="s2",
        tp_order_id="t2",
        entry_price=3000,
        sl_price=3100,
        tp_price=2800,
        quantity=0.1,
        notional_usdt=300,
        leverage=10,
        opened_at="2026-05-29T00:00:00Z",
        play="",
        source="momentum",
        profile_id=None,
        client_ref="",
    )

    rows = db.list_positions(status="open", source="moss_quant", profile_id=12, limit=50)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["profile_id"] == 12
    assert rows[0]["client_ref"] == "moss:12:open:1"
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
cd Next-k-protocol
python -m pytest tests/characterization/test_moss_live_positions.py -v
```

Expected: FAIL because `insert_position` and `list_positions` do not accept reference/filter args.

- [ ] **Step 3: Extend position repository**

In `Next-k-protocol/repos/positions_repo.py`, extend `insert_position` signature:

```python
    profile_id: Optional[int] = None,
    client_ref: str = "",
) -> int:
```

Change insert SQL:

```python
            """INSERT INTO positions
               (signal_log_id, symbol, side, entry_order_id, sl_order_id,
                tp_order_id, entry_price, sl_price, tp_price, quantity,
                notional_usdt, leverage, opened_at, expire_at, status, play,
                source, profile_id, client_ref)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?)""",
```

Change values:

```python
                signal_log_id, symbol, side, entry_order_id, sl_order_id,
                tp_order_id, entry_price, sl_price, tp_price, quantity,
                notional_usdt, leverage, opened_at, expire_at, play, source,
                profile_id, client_ref or "",
```

Extend `insert_pending_position` signature and SQL with `profile_id` and `client_ref` in the same pattern.

Replace `list_positions` with:

```python
def list_positions(
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    source: Optional[str] = None,
    profile_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    vals: List[Any] = []
    if status:
        clauses.append("status=?")
        vals.append(status)
    if source:
        clauses.append("source=?")
        vals.append(source)
    if profile_id is not None:
        clauses.append("profile_id=?")
        vals.append(int(profile_id))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    vals.extend([limit, offset])
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM positions{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            vals,
        ).fetchall()
    return [dict(r) for r in rows]
```

Update `get_open_positions()` to call `list_positions(status="open", limit=500)`.

- [ ] **Step 4: Export and pass reference fields**

In `Next-k-protocol/db.py`, the existing `list_positions`, `insert_position`, and `insert_pending_position` re-exports continue to work after signature changes.

In `Next-k-protocol/ingest/dispatcher.py`, add fields into `signal_dict`:

```python
            "profile_id": sig.profile_id,
            "client_ref": sig.client_ref or "",
            "action": sig.action or ("rolling" if "rolling" in (sig.play or "").lower() else "open"),
```

In `Next-k-protocol/trading/market_entry.py`, pass fields into `insert_position`:

```python
        opened_at=_now_utc(), play=play, source=source,
        profile_id=signal.get("profile_id"),
        client_ref=signal.get("client_ref") or "",
```

In `Next-k-protocol/trading/limit_entry.py`, pass the same fields into `insert_pending_position`.

In `Next-k-protocol/lifecycle/reconcile.py`, when promoting pending positions, keep `profile_id/client_ref` already present in the row; no extra function args are needed unless a new insert occurs.

In `Next-k-protocol/router.py`, extend positions route:

```python
async def list_positions(
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    source: Optional[str] = Query(None),
    profile_id: Optional[int] = Query(None),
):
    rows = _db.list_positions(
        status=status,
        limit=limit,
        offset=offset,
        source=source,
        profile_id=profile_id,
    )
    return rows
```

- [ ] **Step 5: Run tests and commit**

Run:

```bash
cd Next-k-protocol
python -m pytest tests/characterization/test_moss_live_positions.py tests/characterization/test_moss_quant.py -v
```

Expected: PASS.

Commit:

```bash
git add repos/positions_repo.py db.py ingest/dispatcher.py trading/market_entry.py trading/limit_entry.py lifecycle/reconcile.py router.py tests/characterization/test_moss_live_positions.py
git commit -m "feat: persist Moss profile references on positions"
```

---

### Task 4: Protocol Account Summary Endpoint

**Files:**
- Modify: `Next-k-protocol/binance/account.py`
- Modify: `Next-k-protocol/trader.py`
- Modify: `Next-k-protocol/models.py`
- Modify: `Next-k-protocol/router.py`
- Test: `Next-k-protocol/tests/characterization/test_account_summary.py`

- [ ] **Step 1: Write failing account summary tests**

Create `Next-k-protocol/tests/characterization/test_account_summary.py`:

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.characterization
AUTH = {"X-Maintenance-Token": "test-token"}


def test_account_summary_route_returns_usdt_and_moss_config(seeded_config, monkeypatch):
    import main
    import trader
    import db

    db.set_config("src_moss_quant_leverage", "7")
    db.set_config("src_moss_quant_max_positions", "4")
    db.set_config("src_moss_quant_entry_type", "MARKET")
    db.set_config("src_moss_quant_enabled", "true")

    monkeypatch.setattr(
        trader,
        "get_account_summary",
        lambda: {
            "asset": "USDT",
            "wallet_balance_usdt": 1000.5,
            "available_balance_usdt": 800.25,
            "unrealized_pnl_usdt": 12.5,
        },
    )

    client = TestClient(main.app)
    resp = client.get("/api/binance/account/summary", headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["wallet_balance_usdt"] == 1000.5
    assert body["available_balance_usdt"] == 800.25
    assert body["moss_quant"]["leverage"] == 7
    assert body["moss_quant"]["enabled"] is True
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
cd Next-k-protocol
python -m pytest tests/characterization/test_account_summary.py -v
```

Expected: FAIL because route and wrapper do not exist.

- [ ] **Step 3: Add Binance account reader**

In `Next-k-protocol/binance/account.py`, add:

```python
def get_account_summary(client: BinanceClient) -> Dict[str, Any]:
    data = client.request("GET", "/fapi/v2/account")
    assets = data.get("assets") or []
    usdt = None
    for row in assets:
        if str(row.get("asset") or "").upper() == "USDT":
            usdt = row
            break
    if usdt is None:
        raise RuntimeError("USDT asset not found in futures account")
    return {
        "asset": "USDT",
        "wallet_balance_usdt": float(usdt.get("walletBalance") or 0),
        "available_balance_usdt": float(data.get("availableBalance") or usdt.get("availableBalance") or 0),
        "unrealized_pnl_usdt": float(usdt.get("unrealizedProfit") or data.get("totalUnrealizedProfit") or 0),
    }
```

In `Next-k-protocol/trader.py`, import and wrap:

```python
from binance.account import (
    detect_hedge_mode as _hedge_fn,
    get_account_summary as _account_summary_fn,
    get_live_position as _live_pos_fn,
    get_order as _get_order_fn,
    set_leverage as _set_lev_fn,
    set_margin_type as _set_margin_fn,
)

def get_account_summary():      return _account_summary_fn(_resolve_client())
```

- [ ] **Step 4: Add model and route**

In `Next-k-protocol/models.py`, add:

```python
class MossQuantAccountConfig(BaseModel):
    enabled: bool = Field(..., description="Moss Quant source 是否启用")
    leverage: int = Field(..., description="Moss Quant protocol 杠杆")
    max_positions: int = Field(..., description="Moss Quant 最大持仓数")
    entry_type: str = Field(..., description="Moss Quant 入场类型")


class AccountSummaryOut(BaseModel):
    asset: str = Field("USDT", description="账户资产")
    wallet_balance_usdt: float = Field(..., description="USDT 钱包余额")
    available_balance_usdt: float = Field(..., description="USDT 可用余额")
    unrealized_pnl_usdt: float = Field(..., description="当前未实现盈亏")
    moss_quant: MossQuantAccountConfig = Field(..., description="Moss Quant protocol 配置摘要")
```

In `Next-k-protocol/router.py`, import `AccountSummaryOut` and add route after status/config routes:

```python
@router.get(
    "/account/summary",
    response_model=AccountSummaryOut,
    summary="读取币安合约账户摘要",
    dependencies=[Depends(require_auth)],
)
async def account_summary():
    from trader import get_account_summary

    try:
        raw = get_account_summary()
    except Exception as exc:
        logger.error("account summary failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"account_summary_failed: {exc}") from exc

    def _int_cfg(key: str, default: str) -> int:
        try:
            return int(_db.get_config(key, default))
        except ValueError:
            return int(default)

    return {
        **raw,
        "moss_quant": {
            "enabled": _db.get_config("src_moss_quant_enabled", "false").lower() == "true",
            "leverage": _int_cfg("src_moss_quant_leverage", "10"),
            "max_positions": _int_cfg("src_moss_quant_max_positions", "10"),
            "entry_type": _db.get_config("src_moss_quant_entry_type", "MARKET"),
        },
    }
```

- [ ] **Step 5: Run tests and commit**

Run:

```bash
cd Next-k-protocol
python -m pytest tests/characterization/test_account_summary.py -v
```

Expected: PASS.

Commit:

```bash
git add binance/account.py trader.py models.py router.py tests/characterization/test_account_summary.py
git commit -m "feat: expose futures account summary"
```

---

### Task 5: Protocol Event Writes For Close, Update SL, And Exchange Sync

**Files:**
- Modify: `Next-k-protocol/router.py`
- Modify: `Next-k-protocol/lifecycle/close.py`
- Modify: `Next-k-protocol/lifecycle/sync.py`
- Modify: `Next-k-protocol/lifecycle/reconcile.py`
- Test: `Next-k-protocol/tests/characterization/test_moss_live_lifecycle_events.py`

- [ ] **Step 1: Write failing lifecycle event tests**

Create `Next-k-protocol/tests/characterization/test_moss_live_lifecycle_events.py`:

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.characterization
AUTH = {"X-Maintenance-Token": "test-token"}


def _client(seeded_config):
    import main
    return TestClient(main.app)


def test_update_sl_writes_signal_event(seeded_config, mock_binance):
    import db

    pos_id = db.insert_position(
        signal_log_id=1,
        symbol="BTCUSDT",
        side="LONG",
        entry_order_id="entry",
        sl_order_id="old-sl",
        tp_order_id="tp",
        entry_price=65000,
        sl_price=64000,
        tp_price=69000,
        quantity=0.01,
        notional_usdt=650,
        leverage=10,
        opened_at="2026-05-29T00:00:00Z",
        play="balanced",
        source="moss_quant",
        profile_id=5,
        client_ref="moss:5:open:1",
    )
    mock_binance("cancel_algo", "cancel_order_success")
    mock_binance("place_algo", "place_algo_order_success")
    client = _client(seeded_config)

    resp = client.put(
        f"/api/binance/positions/{pos_id}/sl",
        json={"new_sl_price": 64500, "profile_id": 5, "client_ref": "moss:5:update_sl:1"},
        headers=AUTH,
    )

    assert resp.status_code == 200
    events = db.list_signals(source="moss_quant", action="update_sl", profile_id=5, limit=10)
    assert events
    assert events[0]["position_id"] == pos_id
    assert events[0]["status"] == "traded"
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
cd Next-k-protocol
python -m pytest tests/characterization/test_moss_live_lifecycle_events.py -v
```

Expected: FAIL because update SL does not log events.

- [ ] **Step 3: Log close request events**

In `Next-k-protocol/router.py`, inside `close_position`, after `do_close(...)` returns, write an event:

```python
        event_status = "traded" if ok else "error"
        _db.log_trade_event(
            source=body.source,
            action="close",
            symbol=body.symbol,
            side=body.side,
            api_signal_id=body.api_signal_id,
            status=event_status,
            profile_id=body.profile_id,
            position_id=body.position_id,
            client_ref=body.client_ref,
            skip_reason=None if ok else "no open position for symbol",
            payload=body.model_dump(),
            result={"ok": bool(ok), "action": "closed" if ok else "not_found"},
        )
```

When an exception occurs, add before raising 500:

```python
        _db.log_trade_event(
            source=body.source,
            action="close",
            symbol=body.symbol,
            side=body.side,
            api_signal_id=body.api_signal_id,
            status="error",
            profile_id=body.profile_id,
            position_id=body.position_id,
            client_ref=body.client_ref,
            skip_reason=str(exc),
            payload=body.model_dump(),
            result={"ok": False, "error": str(exc)},
        )
```

- [ ] **Step 4: Log update SL events**

In `Next-k-protocol/router.py`, inside `update_position_sl`, after DB update succeeds and before return:

```python
    _db.log_trade_event(
        source=pos.get("source") or "",
        action="update_sl",
        symbol=symbol,
        side=side,
        api_signal_id=body.client_ref or f"update_sl_{position_id}_{int(datetime.now(timezone.utc).timestamp() * 1000)}",
        status="traded",
        profile_id=body.profile_id if body.profile_id is not None else pos.get("profile_id"),
        position_id=position_id,
        client_ref=body.client_ref,
        sl_price=new_sl,
        payload={"old_sl_order_id": old_sl_id, "new_sl_price": body.new_sl_price},
        result={"ok": True, "new_sl_order_id": new_sl_id, "new_sl_price": new_sl},
    )
```

For cancel/place/DB update exceptions, log `status="error"` with the same `source/action/symbol/side/profile_id/position_id/client_ref` before raising.

- [ ] **Step 5: Log exchange close events**

In `Next-k-protocol/lifecycle/close.py`, after `_record_closed_position(...)` updates DB, add:

```python
    from db import log_trade_event
    action_by_reason = {
        "tp": "exchange_tp",
        "sl": "exchange_sl",
        "external": "external_close",
        "paper_close": "external_close",
        "manual": "external_close",
    }
    action = action_by_reason.get(close_reason, "external_close")
    log_trade_event(
        source=pos.get("source") or "",
        action=action,
        symbol=pos.get("symbol") or "",
        side=pos.get("side") or "",
        api_signal_id=f"position_{pos['id']}_{action}",
        status="closed",
        profile_id=pos.get("profile_id"),
        position_id=pos.get("id"),
        client_ref=pos.get("client_ref") or "",
        skip_reason=close_reason,
        payload={"close_reason": close_reason},
        result={"close_price": close_price, "pnl_usdt": round(pnl, 4), "pnl_pct": round(pnl_pct, 4)},
    )
```

When `_record_closed_position` exits early for incomplete data, log the same event with `pnl_usdt=0.0` and `pnl_pct=0.0`.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
cd Next-k-protocol
python -m pytest tests/characterization/test_moss_live_lifecycle_events.py tests/characterization/test_moss_quant.py -v
```

Expected: PASS.

Commit:

```bash
git add router.py lifecycle/close.py lifecycle/sync.py lifecycle/reconcile.py tests/characterization/test_moss_live_lifecycle_events.py
git commit -m "feat: record lifecycle events in signal log"
```

---

### Task 6: next-k-api Protocol Client And Live Sizing

**Files:**
- Create: `next-k-api/moss_quant/protocol_client.py`
- Modify: `next-k-api/moss_quant/signal_sender.py`
- Modify: `next-k-api/moss_quant/paper_scanner.py`
- Test: `next-k-api/tests/test_moss_quant_live_protocol.py`

- [ ] **Step 1: Write failing next-k-api client and sizing tests**

Create `next-k-api/tests/test_moss_quant_live_protocol.py`:

```python
from __future__ import annotations

import pytest


def test_live_notional_uses_protocol_balance_and_leverage():
    from moss_quant.paper_scanner import live_notional_from_account

    params = {"risk_per_trade": 0.10, "max_position_pct": 0.50}
    notional = live_notional_from_account(
        wallet_balance_usdt=1000,
        enabled_profile_count=5,
        protocol_leverage=8,
        params=params,
    )

    assert notional == 160.0


def test_live_notional_rejects_invalid_inputs():
    from moss_quant.paper_scanner import live_notional_from_account

    with pytest.raises(ValueError, match="enabled_profile_count"):
        live_notional_from_account(
            wallet_balance_usdt=1000,
            enabled_profile_count=0,
            protocol_leverage=8,
            params={"risk_per_trade": 0.1, "max_position_pct": 0.5},
        )


def test_protocol_client_builds_headers(monkeypatch):
    monkeypatch.setenv("PROTOCOL_API_URL", "http://protocol.test")
    monkeypatch.setenv("PROTOCOL_MAINTENANCE_TOKEN", "secret")

    from moss_quant.protocol_client import ProtocolClient

    c = ProtocolClient.from_env()
    assert c.base_url == "http://protocol.test"
    assert c.headers()["X-Maintenance-Token"] == "secret"
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
cd next-k-api
python -m pytest tests/test_moss_quant_live_protocol.py -v
```

Expected: FAIL because `protocol_client.py` and `live_notional_from_account` do not exist.

- [ ] **Step 3: Create protocol client**

Create `next-k-api/moss_quant/protocol_client.py`:

```python
"""Protocol client for Moss Quant live trading source of truth."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ProtocolClient:
    base_url: str
    token: str = ""
    timeout: float = 30.0

    @classmethod
    def from_env(cls) -> "ProtocolClient":
        return cls(
            base_url=os.getenv("PROTOCOL_API_URL", "").strip().rstrip("/"),
            token=os.getenv("PROTOCOL_MAINTENANCE_TOKEN", "").strip(),
        )

    def enabled(self) -> bool:
        return bool(self.base_url)

    def headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-Maintenance-Token"] = self.token
        return h

    def _get(self, path: str, **params: Any) -> Any:
        resp = httpx.get(
            f"{self.base_url}{path}",
            params={k: v for k, v in params.items() if v is not None},
            headers=self.headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = httpx.post(
            f"{self.base_url}{path}",
            json=body,
            headers=self.headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = httpx.put(
            f"{self.base_url}{path}",
            json=body,
            headers=self.headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_account_summary(self) -> Dict[str, Any]:
        return self._get("/api/binance/account/summary")

    def get_moss_positions(self, status: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        return self._get("/api/binance/positions", source="moss_quant", status=status, limit=limit)

    def get_moss_leverage(self) -> int:
        summary = self.get_account_summary()
        return int((summary.get("moss_quant") or {}).get("leverage") or 0)

    def send_open(
        self,
        *,
        symbol: str,
        side: str,
        entry_price: float,
        sl_price: float,
        tp_price: Optional[float],
        notional: float,
        profile_id: int,
        play: str,
        composite: float,
        regime: str,
        action: str = "open",
    ) -> Dict[str, Any]:
        client_ref = f"moss:{profile_id}:{action}:{int(time.time() * 1000)}"
        signal = {
            "source": "moss_quant",
            "api_signal_id": client_ref,
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "notional_usdt": round(notional, 2),
            "play": play,
            "regime": regime,
            "profile_id": profile_id,
            "client_ref": client_ref,
            "action": action,
        }
        return self._post("/api/binance/signals/ingest", {"signals": [signal]})

    def send_close(
        self,
        *,
        symbol: str,
        side: str,
        exit_rule: str,
        close_price: float,
        profile_id: int,
        position_id: int,
    ) -> Dict[str, Any]:
        client_ref = f"moss:{profile_id}:close:{int(time.time() * 1000)}"
        return self._post("/api/binance/positions/close", {
            "source": "moss_quant",
            "api_signal_id": client_ref,
            "symbol": symbol,
            "side": side,
            "exit_rule": exit_rule,
            "close_price": close_price,
            "position_id": position_id,
            "profile_id": profile_id,
            "client_ref": client_ref,
        })

    def send_update_sl(self, *, position_id: int, profile_id: int, new_sl_price: float) -> Dict[str, Any]:
        return self._put(f"/api/binance/positions/{position_id}/sl", {
            "new_sl_price": new_sl_price,
            "profile_id": profile_id,
            "client_ref": f"moss:{profile_id}:update_sl:{int(time.time() * 1000)}",
        })
```

- [ ] **Step 4: Add live sizing helper**

In `next-k-api/moss_quant/paper_scanner.py`, add near `_notional`:

```python
def live_notional_from_account(
    *,
    wallet_balance_usdt: float,
    enabled_profile_count: int,
    protocol_leverage: float,
    params: Dict[str, Any],
) -> float:
    if wallet_balance_usdt <= 0:
        raise ValueError("wallet_balance_usdt must be positive")
    if enabled_profile_count <= 0:
        raise ValueError("enabled_profile_count must be positive")
    if protocol_leverage <= 0:
        raise ValueError("protocol_leverage must be positive")
    per_robot_equity = float(wallet_balance_usdt) / int(enabled_profile_count)
    risk = float(params.get("risk_per_trade", 0.1))
    max_pct = float(params.get("max_position_pct", 0.5))
    margin = min(per_robot_equity * risk, per_robot_equity * max_pct)
    return round(max(margin * float(protocol_leverage), 0.0), 2)
```

In the existing `_notional`, keep old behavior for backtests/paper fallback. The live scan will call `live_notional_from_account`.

- [ ] **Step 5: Make signal_sender a wrapper**

In `next-k-api/moss_quant/signal_sender.py`, keep public functions but delegate to `ProtocolClient.from_env()`. For example:

```python
from moss_quant.protocol_client import ProtocolClient


def _client() -> ProtocolClient:
    return ProtocolClient.from_env()


def send_update_sl(*, position_id: int, new_sl_price: float, profile_id: int = 0) -> Dict[str, Any]:
    if not is_real_mode():
        logger.debug("[moss_quant] real mode disabled, skip send_update_sl")
        return {"ok": False, "error": "real_mode_disabled"}
    return _client().send_update_sl(
        position_id=position_id,
        profile_id=profile_id,
        new_sl_price=new_sl_price,
    )
```

Task 7 updates all existing call sites to pass `profile_id`.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
cd next-k-api
python -m pytest tests/test_moss_quant_live_protocol.py -v
```

Expected: PASS.

Commit:

```bash
git add moss_quant/protocol_client.py moss_quant/signal_sender.py moss_quant/paper_scanner.py tests/test_moss_quant_live_protocol.py
git commit -m "feat: add Moss protocol client and live sizing"
```

---

### Task 7: next-k-api Moss Live Scan Source Of Truth

**Files:**
- Modify: `next-k-api/moss_quant/paper_scanner.py`
- Modify: `next-k-api/moss_quant/db.py`
- Test: `next-k-api/tests/test_moss_quant_live_scan.py`

- [ ] **Step 1: Write failing live scan behavior tests**

Create `next-k-api/tests/test_moss_quant_live_scan.py`:

```python
from __future__ import annotations


def test_positions_map_prefers_protocol_profile_id():
    from moss_quant.paper_scanner import protocol_open_positions_by_profile

    rows = [
        {"id": 11, "profile_id": 3, "symbol": "BTCUSDT", "side": "LONG", "entry_price": 65000, "quantity": 0.01, "notional_usdt": 650, "leverage": 8},
        {"id": 12, "profile_id": 3, "symbol": "BTCUSDT", "side": "LONG", "entry_price": 66000, "quantity": 0.01, "notional_usdt": 660, "leverage": 8},
        {"id": 21, "profile_id": 4, "symbol": "ETHUSDT", "side": "SHORT", "entry_price": 3000, "quantity": 0.2, "notional_usdt": 600, "leverage": 8},
    ]

    by_profile = protocol_open_positions_by_profile(rows)
    assert [p["id"] for p in by_profile[3]] == [11, 12]
    assert by_profile[4][0]["symbol"] == "ETHUSDT"
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
cd next-k-api
python -m pytest tests/test_moss_quant_live_scan.py -v
```

Expected: FAIL because helper does not exist.

- [ ] **Step 3: Add protocol mapping helpers**

In `next-k-api/moss_quant/paper_scanner.py`, add:

```python
def protocol_open_positions_by_profile(
    positions: List[Dict[str, Any]],
) -> Dict[int, List[Dict[str, Any]]]:
    out: Dict[int, List[Dict[str, Any]]] = {}
    for pos in positions or []:
        pid = pos.get("profile_id")
        if pid is None:
            continue
        out.setdefault(int(pid), []).append(dict(pos))
    return out


def _protocol_position_notional(pos: Dict[str, Any]) -> float:
    n = pos.get("notional_usdt")
    if n is not None:
        return float(n or 0)
    qty = float(pos.get("quantity") or 0)
    entry = float(pos.get("entry_price") or 0)
    return abs(qty * entry)
```

- [ ] **Step 4: Add local sync helper**

In `next-k-api/moss_quant/db.py`, add:

```python
def mark_profile_open_signals_external_closed(
    conn: sqlite3.Connection,
    profile_id: int,
    *,
    exit_rule: str = "external_closed",
) -> int:
    now = _utc_now()
    cur = conn.execute(
        """UPDATE moss_signals
           SET outcome='external_closed',
               outcome_at_utc=?,
               exit_rule=?,
               updated_at_utc=?,
               unrealized_pnl_usdt=0
           WHERE profile_id=? AND outcome IS NULL AND side IN ('LONG','SHORT')""",
        (now, exit_rule, now, int(profile_id)),
    )
    return int(cur.rowcount or 0)
```

- [ ] **Step 5: Modify scan startup to load protocol truth**

In `run_paper_scan`, after loading `profiles`, add:

```python
    protocol_client = None
    account_summary: Dict[str, Any] = {}
    protocol_leverage = 0.0
    protocol_open_by_profile: Dict[int, List[Dict[str, Any]]] = {}
    sender = _get_sender()
    if sender:
        try:
            from moss_quant.protocol_client import ProtocolClient
            protocol_client = ProtocolClient.from_env()
            account_summary = protocol_client.get_account_summary()
            protocol_leverage = float((account_summary.get("moss_quant") or {}).get("leverage") or 0)
            protocol_open_by_profile = protocol_open_positions_by_profile(
                protocol_client.get_moss_positions(status="open")
            )
        except Exception as e:
            logger.error("[moss] protocol truth unavailable: %s", e)
            stats["protocol_error"] = str(e)
```

For each profile, before reading local `moss_signals`, compute:

```python
        real_positions = protocol_open_by_profile.get(pid, [])
```

When `real_positions` is empty and local `row` exists in real mode:

```python
            if sender and not real_positions:
                from moss_quant.db import mark_profile_open_signals_external_closed
                mark_profile_open_signals_external_closed(conn, pid)
                stats["details"].append(_scan_detail(label, profile, {
                    "symbol": symbol,
                    "action": "close",
                    "rule": "external_closed",
                    "pnl": 0.0,
                }))
                continue
```

When opening a new position, replace `_notional(profile, params_d, conn)` with:

```python
        if sender:
            if not account_summary or protocol_leverage <= 0:
                stats["details"].append(_scan_detail(label, profile, {
                    "symbol": symbol,
                    "action": "error",
                    "error": "protocol_account_or_leverage_unavailable",
                }))
                continue
            try:
                notional = live_notional_from_account(
                    wallet_balance_usdt=float(account_summary["wallet_balance_usdt"]),
                    enabled_profile_count=max(1, int(len([p for p in profiles if int(p.get("enabled") or 0)]))),
                    protocol_leverage=protocol_leverage,
                    params=params_d,
                )
                lev = protocol_leverage
            except ValueError as e:
                stats["details"].append(_scan_detail(label, profile, {
                    "symbol": symbol,
                    "action": "error",
                    "error": str(e),
                }))
                continue
        else:
            notional = _notional(profile, params_d, conn)
```

When calling `send_update_sl`, pass `profile_id=pid`. When calling `send_close`, pass the concrete `position_id` from protocol positions.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
cd next-k-api
python -m pytest tests/test_moss_quant_live_protocol.py tests/test_moss_quant_live_scan.py -v
```

Expected: PASS.

Commit:

```bash
git add moss_quant/paper_scanner.py moss_quant/db.py tests/test_moss_quant_live_scan.py
git commit -m "feat: scan Moss against live protocol positions"
```

---

### Task 8: next-k-api Moss API Aggregates Protocol Truth

**Files:**
- Modify: `next-k-api/routers/moss_quant.py`
- Modify: `next-k-api/moss_quant/paper_scanner.py`
- Test: `next-k-api/tests/test_moss_quant_live_api.py`

- [ ] **Step 1: Write failing API aggregation tests**

Create `next-k-api/tests/test_moss_quant_live_api.py`:

```python
from __future__ import annotations


def test_live_summary_aggregates_protocol_positions():
    from routers.moss_quant import _summarize_protocol_moss

    summary = _summarize_protocol_moss(
        account={"wallet_balance_usdt": 1000, "available_balance_usdt": 900, "unrealized_pnl_usdt": 25, "moss_quant": {"leverage": 8}},
        positions=[
            {"profile_id": 1, "symbol": "BTCUSDT", "status": "open", "pnl_usdt": None, "notional_usdt": 500},
            {"profile_id": 1, "symbol": "BTCUSDT", "status": "closed", "pnl_usdt": 12.5, "notional_usdt": 500},
            {"profile_id": 2, "symbol": "ETHUSDT", "status": "closed", "pnl_usdt": -3.0, "notional_usdt": 300},
        ],
        enabled_profiles=2,
    )

    assert summary["mode"] == "live"
    assert summary["wallet_balance_usdt"] == 1000
    assert summary["open_positions"] == 1
    assert summary["settled_count"] == 2
    assert summary["total_pnl_usdt"] == 9.5
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
cd next-k-api
python -m pytest tests/test_moss_quant_live_api.py -v
```

Expected: FAIL because `_summarize_protocol_moss` does not exist.

- [ ] **Step 3: Add summary helper**

In `next-k-api/routers/moss_quant.py`, add near helper functions:

```python
def _summarize_protocol_moss(
    *,
    account: Dict[str, Any],
    positions: List[Dict[str, Any]],
    enabled_profiles: int,
) -> Dict[str, Any]:
    open_rows = [p for p in positions if p.get("status") == "open"]
    closed_rows = [p for p in positions if p.get("status") == "closed"]
    total_pnl = round(sum(float(p.get("pnl_usdt") or 0) for p in closed_rows), 4)
    per_profile: Dict[int, Dict[str, Any]] = {}
    open_by_profile: Dict[int, Dict[str, Any]] = {}
    for p in closed_rows:
        pid = p.get("profile_id")
        if pid is None:
            continue
        row = per_profile.setdefault(int(pid), {
            "profile_id": int(pid),
            "symbol": str(p.get("symbol") or "").upper(),
            "settled_count": 0,
            "total_pnl_usdt": 0.0,
        })
        row["settled_count"] += 1
        row["total_pnl_usdt"] = round(row["total_pnl_usdt"] + float(p.get("pnl_usdt") or 0), 4)
    for p in open_rows:
        pid = p.get("profile_id")
        if pid is None:
            continue
        row = open_by_profile.setdefault(int(pid), {
            "profile_id": int(pid),
            "symbol": str(p.get("symbol") or "").upper(),
            "open_count": 0,
            "unrealized_pnl_usdt": 0.0,
        })
        row["open_count"] += 1
        row["unrealized_pnl_usdt"] = round(row["unrealized_pnl_usdt"] + float(p.get("pnl_usdt") or 0), 4)
    return {
        "ok": True,
        "mode": "live",
        "lane": "moss_quant",
        "open_positions": len(open_rows),
        "settled_count": len(closed_rows),
        "total_pnl_usdt": total_pnl,
        "wallet_initial_usdt": float(account.get("wallet_balance_usdt") or 0) - total_pnl,
        "wallet_balance_usdt": float(account.get("wallet_balance_usdt") or 0),
        "available_balance_usdt": float(account.get("available_balance_usdt") or 0),
        "enabled_profiles": enabled_profiles,
        "per_profile": list(per_profile.values()),
        "open_by_profile": list(open_by_profile.values()),
        "protocol_moss": account.get("moss_quant") or {},
    }
```

- [ ] **Step 4: Use protocol in summary/signals/latest scan**

In `get_summary()`, before local SQLite aggregation, try protocol:

```python
        try:
            from moss_quant.protocol_client import ProtocolClient
            pc = ProtocolClient.from_env()
            if pc.enabled():
                account = pc.get_account_summary()
                positions = pc.get_moss_positions(status=None, limit=1000)
                profiles = int(cur.execute("SELECT COUNT(*) FROM moss_profiles WHERE enabled=1").fetchone()[0] or 0)
                live = _summarize_protocol_moss(
                    account=account,
                    positions=positions,
                    enabled_profiles=profiles,
                )
                live.update({
                    "max_active_profiles": mq_cfg.MOSS_QUANT_MAX_ACTIVE_PROFILES,
                    "data_source": mq_cfg.MOSS_QUANT_DATA_SOURCE,
                    "data_source_label": mq_cfg.data_source_label(),
                    "kline_limit": mq_cfg.MOSS_QUANT_KLINE_LIMIT,
                })
                return live
        except Exception as e:
            logger.warning("[moss] protocol summary fallback: %s", e)
```

In `get_signals()`, if protocol client is enabled, return protocol positions converted to signal-like rows:

```python
        try:
            from moss_quant.protocol_client import ProtocolClient
            pc = ProtocolClient.from_env()
            if pc.enabled():
                positions = pc.get_moss_positions(status=None, limit=500)
                return {"mode": "live", "signals": [_position_to_moss_signal_row(p) for p in positions]}
        except Exception as e:
            logger.warning("[moss] protocol signals fallback: %s", e)
```

Add helper:

```python
def _position_to_moss_signal_row(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": p.get("id"),
        "profile_id": p.get("profile_id"),
        "recorded_at_utc": p.get("opened_at"),
        "side": p.get("side"),
        "symbol": p.get("symbol"),
        "entry_price": p.get("entry_price"),
        "virtual_notional_usdt": p.get("notional_usdt"),
        "mark_price": p.get("close_price") or p.get("entry_price"),
        "unrealized_pnl_usdt": p.get("pnl_usdt") if p.get("status") == "open" else 0,
        "outcome": None if p.get("status") == "open" else p.get("close_reason") or "closed",
        "outcome_at_utc": p.get("closed_at"),
        "exit_price": p.get("close_price"),
        "pnl_usdt": p.get("pnl_usdt"),
        "exit_rule": p.get("close_reason"),
        "leverage": p.get("leverage"),
        "client_ref": p.get("client_ref"),
        "position_id": p.get("id"),
        "source": p.get("source"),
    }
```

- [ ] **Step 5: Run tests and commit**

Run:

```bash
cd next-k-api
python -m pytest tests/test_moss_quant_live_api.py tests/test_moss_quant_live_protocol.py -v
```

Expected: PASS.

Commit:

```bash
git add routers/moss_quant.py moss_quant/paper_scanner.py tests/test_moss_quant_live_api.py
git commit -m "feat: aggregate Moss APIs from live protocol data"
```

---

### Task 9: Frontend Moss Live Labels

**Files:**
- Modify: `next-k-frontend/index.html`
- Test: manual browser check at `http://localhost:3000/index.html`

- [ ] **Step 1: Replace Moss paper copy with live copy**

In `next-k-frontend/index.html`, replace visible Moss labels:

```javascript
// Existing visible text replacements:
"Moss Quant (Paper)" -> "Moss Quant (Live)"
"全局纸面钱包" -> "实仓钱包"
"纸面 Profile（策略配置）" -> "实仓 Profile（机器人配置）"
"纸面信号" -> "实仓信号"
"加载 Moss 纸面…" -> "加载 Moss 实仓…"
"Moss 量化纸面扫描" -> "Moss 量化实仓扫描"
"Moss 纸面" -> "Moss 实仓"
"加入纸面" -> "加入实仓"
"已在纸面" -> "已启用"
```

Also update strings inside JavaScript render functions that mention `纸面`, including empty-state and confirm messages in the Moss section.

- [ ] **Step 2: Display live mode and protocol leverage**

In `renderMqSummary(sum)`, add `mode` and protocol leverage to the summary cards:

```javascript
const liveMode = String(sum.mode || '').toLowerCase() === 'live';
const protoLev = sum.protocol_moss && sum.protocol_moss.leverage != null
    ? Number(sum.protocol_moss.leverage)
    : null;
```

Change the wallet card title:

```javascript
<div class="text-text-muted text-[10px] uppercase">${liveMode ? '实仓钱包（Protocol）' : '纸面钱包（全账户）'}</div>
```

Change one summary card to show leverage:

```javascript
<div class="bg-surface-light/30 rounded-lg border border-border/70 px-3 py-2">
    <div class="text-text-muted text-[10px] uppercase">Moss 杠杆</div>
    <div class="text-lg font-semibold">${protoLev != null ? escHtml(String(protoLev) + 'x') : '—'}</div>
</div>
```

- [ ] **Step 3: Run static server and inspect**

Run:

```bash
cd next-k-frontend
./start.sh
```

Open `http://localhost:3000/index.html` in the in-app browser. Verify:

- Moss section title says `Moss Quant (Live)`.
- Wallet card says `实仓钱包`.
- No main Moss section label still says `纸面`.
- The section layout remains readable at desktop width.

- [ ] **Step 4: Commit**

Commit:

```bash
git add index.html
git commit -m "feat: show Moss Quant as live trading"
```

---

### Task 10: Frontend Strategy Filters For Real Trading Dashboard

**Files:**
- Modify: `next-k-frontend/binance.html`
- Test: manual browser check at `http://localhost:3000/binance.html`

- [ ] **Step 1: Add strategy filter controls**

In `next-k-frontend/binance.html`, add this shared filter UI above historical positions and signal log tables:

```html
<div class="flex flex-wrap gap-1.5 items-center" id="strategy-filter-bar">
    <button type="button" class="strategy-filter active px-2.5 py-1 text-[11px] rounded border border-accent/45 text-accent bg-accent/10" data-source="">全部</button>
    <button type="button" class="strategy-filter px-2.5 py-1 text-[11px] rounded border border-border/80 text-text-muted bg-surface-light/60" data-source="zct_vwap">ZCT</button>
    <button type="button" class="strategy-filter px-2.5 py-1 text-[11px] rounded border border-border/80 text-text-muted bg-surface-light/60" data-source="momentum">动量</button>
    <button type="button" class="strategy-filter px-2.5 py-1 text-[11px] rounded border border-border/80 text-text-muted bg-surface-light/60" data-source="jiezhen">接针</button>
    <button type="button" class="strategy-filter px-2.5 py-1 text-[11px] rounded border border-border/80 text-text-muted bg-surface-light/60" data-source="moss_quant">Moss</button>
</div>
```

Place one copy in the history section header and one copy in the signal log header. Use distinct IDs if both bars are present:

```html
id="position-strategy-filter-bar"
id="signal-strategy-filter-bar"
```

- [ ] **Step 2: Add filter state and URL builders**

In the script section:

```javascript
var _positionSourceFilter = '';
var _signalSourceFilter = '';

function sourceParam(src) {
    return src ? '&source=' + encodeURIComponent(src) : '';
}

function bindStrategyFilters(rootId, setter) {
    var root = document.getElementById(rootId);
    if (!root) return;
    root.querySelectorAll('.strategy-filter').forEach(function(btn) {
        btn.addEventListener('click', function() {
            var src = btn.getAttribute('data-source') || '';
            setter(src);
            root.querySelectorAll('.strategy-filter').forEach(function(b) {
                var on = b === btn;
                b.classList.toggle('active', on);
                b.classList.toggle('text-accent', on);
                b.classList.toggle('border-accent/45', on);
                b.classList.toggle('bg-accent/10', on);
            });
        });
    });
}
```

At startup:

```javascript
bindStrategyFilters('position-strategy-filter-bar', function(src) {
    _positionSourceFilter = src;
    loadClosedPositions();
});
bindStrategyFilters('signal-strategy-filter-bar', function(src) {
    _signalSourceFilter = src;
    loadSignals();
});
```

- [ ] **Step 3: Apply filters to API calls**

In `loadClosedPositions()`:

```javascript
var param = _currentTab === 'closed' ? '?status=closed&limit=100' : '?limit=100';
param += sourceParam(_positionSourceFilter);
var rows = await apiFetch('/api/binance/positions' + param);
```

In `loadSignals()`:

```javascript
var param = '?limit=100' + sourceParam(_signalSourceFilter);
var rows = await apiFetch('/api/binance/signals' + param);
```

- [ ] **Step 4: Render action and references**

In the signal table row renderer, add action/profile/position columns:

```javascript
+ '<td class="text-text-secondary">' + escHtml(s.action || 'open') + '</td>'
+ '<td class="text-text-muted font-mono">' + escHtml(s.profile_id != null ? String(s.profile_id) : '—') + '</td>'
+ '<td class="text-text-muted font-mono">' + escHtml(s.position_id != null ? String(s.position_id) : '—') + '</td>'
```

Update table header:

```javascript
+ '<th>来源</th><th>动作</th><th>Profile</th><th>仓位ID</th><th>标的</th><th>方向</th><th>入场价</th><th>止损</th><th>止盈</th><th>状态</th><th>原因</th><th>收到时间</th>'
```

In closed position rows, add source/profile columns:

```javascript
+ '<td class="text-text-secondary">' + escHtml(p.source || '—') + '</td>'
+ '<td class="text-text-muted font-mono">' + escHtml(p.profile_id != null ? String(p.profile_id) : '—') + '</td>'
```

Update history table header with `来源` and `Profile`.

- [ ] **Step 5: Run static server and inspect**

Run:

```bash
cd next-k-frontend
./start.sh
```

Open `http://localhost:3000/binance.html`. Verify:

- History strategy filters call `/api/binance/positions?...&source=moss_quant`.
- Signal filters call `/api/binance/signals?limit=100&source=moss_quant`.
- Table headers fit on desktop and remain horizontally scrollable.

- [ ] **Step 6: Commit**

Commit:

```bash
git add binance.html
git commit -m "feat: filter live dashboard by strategy"
```

---

### Task 11: Cross-Service Verification

**Files:**
- No source edits expected
- Verify: `Next-k-protocol`, `next-k-api`, `next-k-frontend`

- [ ] **Step 1: Run protocol tests**

Run:

```bash
cd Next-k-protocol
python -m pytest tests/characterization/test_moss_live_schema.py tests/characterization/test_moss_live_signal_events.py tests/characterization/test_moss_live_positions.py tests/characterization/test_account_summary.py tests/characterization/test_moss_live_lifecycle_events.py tests/characterization/test_moss_quant.py -v
```

Expected: all selected tests PASS.

- [ ] **Step 2: Run next-k-api tests**

Run:

```bash
cd next-k-api
python -m pytest tests/test_moss_quant_live_protocol.py tests/test_moss_quant_live_scan.py tests/test_moss_quant_live_api.py tests/test_moss_quant.py -v
```

Expected: all selected tests PASS.

- [ ] **Step 3: Start services for manual smoke**

Terminal 1:

```bash
cd Next-k-protocol
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

Terminal 2:

```bash
cd next-k-api
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Terminal 3:

```bash
cd next-k-frontend
./start.sh
```

- [ ] **Step 4: Verify protocol endpoints**

Run:

```bash
curl -s -H "X-Maintenance-Token: $NEXT_K_MAINTENANCE_TOKEN" \
  "http://localhost:8001/api/binance/positions?source=moss_quant&status=open&limit=5"
curl -s -H "X-Maintenance-Token: $NEXT_K_MAINTENANCE_TOKEN" \
  "http://localhost:8001/api/binance/signals?source=moss_quant&limit=5"
```

Expected:

- Both endpoints return JSON arrays.
- Requests do not return 500.
- Rows include `source`; Moss rows include `profile_id/client_ref/action` when present.

- [ ] **Step 5: Verify frontend pages**

Open:

- `http://localhost:3000/index.html`
- `http://localhost:3000/binance.html`

Expected:

- Moss main board says Live/实仓.
- Binance dashboard can filter history and signal log by strategy.
- No overlapping text or broken table layout in desktop browser.

- [ ] **Step 6: Commit verification notes if docs changed**

If verification required doc notes in this plan file, commit them:

```bash
git status --short
git add docs/superpowers/plans/2026-05-29-moss-quant-live-source-of-truth.md
git commit -m "docs: record Moss live verification notes"
```

If no files changed, do not create a commit.

---

## Rollout Notes

- Deploy `Next-k-protocol` before `next-k-api`, because next-k-api depends on new account/positions/signals fields.
- Deploy `next-k-api` before `next-k-frontend`, because the frontend expects live-mode Moss payloads.
- Keep `MOSS_QUANT_REAL_MODE=true` only after protocol account summary and positions filters are verified.
- Before live enablement, set protocol config:

```json
{
  "src_moss_quant_enabled": "true",
  "src_moss_quant_leverage": "10",
  "src_moss_quant_max_positions": "10",
  "src_moss_quant_entry_type": "MARKET"
}
```

## Self-Review

- Spec coverage: account summary, profile references, event logging, strategy-filtered dashboard, live sizing, protocol failure behavior, and frontend label changes are covered by Tasks 1-11.
- Placeholder scan: this plan contains concrete files, test commands, expected outcomes, and code snippets for each code-changing task.
- Type consistency: `profile_id`, `client_ref`, `action`, `position_id`, `payload_json`, and `result_json` are introduced in schema, models, repositories, API filters, and frontend renderers with consistent names.
