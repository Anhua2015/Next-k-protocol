# Binance Live Trading Bridge

Binance Futures live trading module. Supports ZCT VWAP / Momentum / Jiezhen three strategies. Originally embedded in next-k-api, now standalone as Next-k-protocol service.

## Key Behaviours

- SL/TP placed via `/fapi/v1/algoOrder` (Binance migration 2025-12-09).
- HEDGE-mode aware: uses `positionSide` when account is dual-side.
- SL distance pre-validated against mark price.
- On SL/TP placement failure -> emergency MARKET close to avoid naked position.
- `exchangeInfo` cached 5 min; server time synced every 10 min and on -1021.
- Retry with exponential backoff on 429 / -1003 / 5xx.
- Current positions are read directly from Binance `positionRisk`.
- LIMIT orders are submit-only; no local pending lifecycle management.

## Configuration

Trading config is stored in `binance.db` -> `config` table and managed via `GET/POST /api/binance/config`.
Binance credentials are **not** stored or edited through that API; `BINANCE_API_KEY` / `BINANCE_API_SECRET`
must come from `.env.oi`, process environment variables, or Railway environment configuration.

See `.env.oi.example` for the full list.

## Database

SQLite WAL mode. Tables: `config`, `signals_log`. See `db.py` for DDL and `README.md` for field descriptions.

## Signal Processing

Signals arrive via `POST /api/binance/signals/ingest` (pushed by next-k-api). Processing gates:

1. Duplicate check (source + api_signal_id UNIQUE)
2. Trading enabled
3. No open position for same symbol (based on live Binance positions)
4. Global max positions not reached

## Safety

- Circuit breaker: auto-disables trading after 20 consecutive auth failures
- Emergency close on SL/TP placement failure
- SL distance pre-validation
- Write lock serialisation for signal processing
