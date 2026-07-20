"""Global risk layer — every entry signal passes through here.

Rules (from STRATEGIES.md): event quiet window, fear&greed sizing, per-coin
cap, gross leverage cap, daily loss halt, max concurrent coins, no hedged
same-coin positions.
"""
import logging
import time

from . import db, factors

log = logging.getLogger("risk")

QUIET_EXEMPT = {"S09_EVENT_BREAKOUT"}

# USD macro titles that should silence new entries (STRATEGIES.md: CPI/FOMC/NFP).
_MACRO_KEYS = (
    "cpi", "fomc", "nfp", "non-farm", "nonfarm", "non farm",
    "非农", "利率", "interest rate", "fed ", "federal reserve",
    "pce", "payroll", "employment", "jobless",
)


def _day_start():
    return int(time.time()) // 86400 * 86400


def daily_realized_pnl():
    return sum(p["pnl"] or 0 for p in db.closed_positions_today(_day_start()))


def halted():
    until = float(db.get_meta("halt_until", "0") or 0)
    return time.time() < until


def check_daily_halt(equity, cfg_global):
    pnl = daily_realized_pnl()
    if equity > 0 and pnl < 0 and abs(pnl) / equity * 100 >= cfg_global["daily_loss_halt_pct"]:
        tomorrow = _day_start() + 86400
        db.set_meta("halt_until", tomorrow)
        db.log("warn", f"日内熔断触发:已实现亏损 {pnl:.2f} ({abs(pnl)/equity*100:.1f}%),停止开仓至次日 UTC0")
        return True
    return False


def _is_high_importance(imp):
    s = str(imp or "").strip().lower()
    return any(x in s for x in ("3", "高", "high"))


def _is_usd_macro(event):
    """True for high-impact USD macro releases that should quiet the book."""
    if not _is_high_importance(event.get("importance")):
        return False
    title = str(event.get("title") or "").lower()
    country = str(event.get("country") or "").upper()
    if any(k in title for k in _MACRO_KEYS):
        return True
    # FOMC fallback rows are tagged USD + High without always matching keys
    if country in ("USD", "US", "USA") and "fomc" in title:
        return True
    return False


def event_quiet(cfg_global):
    """True while inside the pre-event quiet window of a high-importance USD macro."""
    cal = (db.get_factor("econ_cal") or {}).get("events", [])
    now = time.time()
    window = float(cfg_global.get("event_quiet_minutes") or 60) * 60
    for e in cal:
        ts = e.get("ts")
        if not ts:
            continue
        if not _is_usd_macro(e):
            continue
        if 0 < float(ts) - now <= window:
            return e.get("title", "event")
    return ""


def size_multiplier(side, cfg_global):
    """Fear & Greed extreme haircut for new entries (STRATEGIES.md)."""
    fg = db.get_factor("fear_greed") or {}
    v = fg.get("latest")
    ext = cfg_global.get("fear_greed_extreme") or {}
    greed = float(ext.get("greed", 85))
    fear = float(ext.get("fear", 15))
    mult = float(ext.get("size_mult", 0.6))
    if v is None:
        return 1.0
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 1.0
    if side == "long" and v >= greed:
        return mult
    if side == "short" and v <= fear:
        return mult
    return 1.0


def _strategy_capacity(sig, scfg, equity, cfg_global):
    """(capital_base, leverage, max_position_usd, used_notional) for a strategy."""
    risk = scfg.get("risk") or {}
    capital = float(risk.get("capital_usd") or 0) or equity
    leverage = int(risk.get("leverage") or cfg_global.get("max_gross_leverage", 3))
    max_pos = float(risk.get("max_position_usd") or 0) or capital * leverage
    used = sum(abs(p["qty"] * factors.mark_price_of(p["symbol"]))
               for p in db.open_positions(strategy=sig.strategy))
    return capital, leverage, max_pos, used


def allow_entry(sig, scfg, equity, cfg_global):
    """Returns (allowed: bool, reason: str, size_mult: float)."""
    if halted():
        return False, "日内熔断中", 0
    if sig.strategy not in QUIET_EXEMPT:
        ev = event_quiet(cfg_global)
        if ev:
            return False, f"事件静默({ev[:30]})", 0
    open_pos = db.open_positions()
    coins = {p["symbol"] for p in open_pos}
    if sig.symbol in coins:
        return False, "同币已有仓位(禁止对锁/加仓)", 0
    if len(coins) >= cfg_global["max_concurrent_coins"]:
        return False, "并发币数已达上限", 0
    gross = sum(abs(p["qty"] * factors.mark_price_of(p["symbol"])) for p in open_pos)
    if equity > 0 and gross / equity >= cfg_global["max_gross_leverage"]:
        return False, "总敞口已达杠杆上限", 0
    capital, leverage, _max_pos, used = _strategy_capacity(sig, scfg, equity, cfg_global)
    if used >= capital * leverage:
        return False, "策略分配资金已用满", 0
    return True, "", size_multiplier(sig.side, cfg_global)


def position_qty(sig, scfg, equity, cfg_global, price):
    """Per-strategy risk-based sizing:
    qty = (capital_base × risk_per_trade%) / stop_distance,
    then capped by the strategy's max_position_usd, its remaining
    capital×leverage capacity, and the global per-coin exposure limit."""
    if price <= 0:
        return 0
    stop_dist = abs(price - sig.stop_price) if sig.stop_price else price * 0.01
    if stop_dist <= 0:
        return 0
    risk = scfg.get("risk") or {}
    capital, leverage, max_pos, used = _strategy_capacity(sig, scfg, equity, cfg_global)
    rpt = float(risk.get("risk_per_trade_pct") or 0) or cfg_global["risk_per_trade_pct"]
    risk_usd = capital * rpt / 100 * sig.size_mult
    qty = risk_usd / stop_dist
    notional = qty * price
    notional = min(notional, max_pos)                       # per-position cap
    notional = min(notional, max(capital * leverage - used, 0))  # strategy capacity left
    notional = min(notional, equity * cfg_global["max_position_pct_per_coin"] / 100
                   * leverage)                              # global per-coin cap (×lev)
    return notional / price


def _strategy_leverage(pos, cfg):
    scfg = (cfg.get("strategies") or {}).get(pos["strategy"]) or {}
    risk = scfg.get("risk") or {}
    return int(risk.get("leverage") or (cfg.get("global") or {}).get("max_gross_leverage", 3))


async def sync_event_quiet_leverage(cfg):
    """During USD macro quiet: halve live leverage on open positions; restore after.

    Paper mode only logs state transitions (no exchange leverage to change).
    """
    from . import executor

    cfg_global = cfg.get("global") or {}
    ev = event_quiet(cfg_global)
    prev = db.get_meta("event_quiet_active", "") or ""

    if ev and not prev:
        db.set_meta("event_quiet_active", ev)
        db.log("warn", f"事件静默开始 — 禁止开仓,存量仓杠杆减半: {ev[:60]}")
        if executor.mode() == "live":
            await _set_open_leverage(cfg, half=True)
        return ev

    if not ev and prev:
        db.set_meta("event_quiet_active", "")
        db.log("info", f"事件静默结束 — 恢复杠杆: {prev[:60]}")
        if executor.mode() == "live":
            await _set_open_leverage(cfg, half=False)
        return ""

    return ev or ""


async def _set_open_leverage(cfg, half=True):
    from . import executor, exchanges, binance

    venue = executor.exchange()
    for pos in db.open_positions():
        if pos["strategy"] in QUIET_EXEMPT:
            continue
        lev = _strategy_leverage(pos, cfg)
        target = max(1, lev // 2) if half else lev
        sym = pos["symbol"]
        try:
            if venue == "binance":
                await binance.set_leverage(sym, target)
            else:
                await exchanges.set_leverage(venue, sym, target)
            log.info("quiet leverage %s %s -> %dx", sym, "half" if half else "restore", target)
        except Exception as exc:  # noqa: BLE001
            log.warning("quiet leverage %s failed: %s", sym, exc)
