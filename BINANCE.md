# Binance Live Trading Bridge

Binance Futures live trading module. Originally embedded in next-k-api, now standalone as Next-k-protocol service.

## Key Behaviours

- SL/TP placed via `/fapi/v1/algoOrder` (Binance migration 2025-12-09).
- HEDGE-mode aware: uses `positionSide` when account is dual-side.
- SL distance pre-validated against mark price.
- On SL/TP placement failure -> emergency MARKET close to avoid naked position.
- `exchangeInfo` cached 5 min; server time synced every 10 min and on -1021.
- Retry with exponential backoff on 429 / -1003 / 5xx.
- `pnl_pct` = leveraged return on margin.
- `expire_open_positions()`: force-closes positions older than `position_expire_hours`.

## Configuration

All config stored in `binance.db` -> `config` table. Initial values seeded from environment variables on first start, then managed via `GET/POST /api/binance/config`.

See `.env.oi.example` for the full list.

## Database

SQLite WAL mode. Tables: `config`, `signals_log`, `positions`. See `db.py` for DDL and `README.md` for field descriptions.

## Signal Processing

Signals arrive via `POST /api/binance/signals/ingest` (pushed by next-k-api after ZCT scan). Processing gates:

1. Duplicate check (source + api_signal_id UNIQUE)
2. Trading enabled
3. Source in enabled_sources
4. No open position for same symbol
5. Per-play max positions not reached
6. Global max positions not reached

## Safety

- Circuit breaker: auto-disables trading after 20 consecutive auth failures
- Emergency close on SL/TP placement failure
- SL distance pre-validation
- Position expiry hard limit
- Write lock serialisation for signal processing
