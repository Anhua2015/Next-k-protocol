/**
 * Real-candle grid backtest via Binance public data-api (no key).
 * Compares neutral / long / short / adaptive under RISK.steady.
 * Usage: node scripts/backtest-grid-binance.mjs
 */
import { writeFileSync } from 'fs';
import { buildGrid, seedOrders, replacementFor } from '../src/grid.js';
import { analyzeTrend } from '../src/trend.js';
import { RISK, suggestFromTrend } from '../src/autopilot.js';

const FEE = 0.0002;
const EQUITY = 15000;
const SLOTS = 5;
const RISK_P = RISK.steady;
const LIMIT = 1000; // ~41 days of 1h
const SYMBOLS = [
  'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'LTCUSDT',
  'XRPUSDT', 'DOGEUSDT', 'ADAUSDT', 'LINKUSDT', 'AVAXUSDT',
];

async function fetchCandles(symbol) {
  const url = `https://data-api.binance.vision/api/v3/klines?symbol=${symbol}&interval=1h&limit=${LIMIT}`;
  const res = await fetch(url, { signal: AbortSignal.timeout(20000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const rows = await res.json();
  return rows.map((r) => ({
    time: Number(r[0]),
    open: Number(r[1]),
    high: Number(r[2]),
    low: Number(r[3]),
    close: Number(r[4]),
    volume: Number(r[5]),
  }));
}

function sizeFor(price, leverage = 3) {
  return (EQUITY * RISK_P.budget * leverage) / SLOTS / 20 / price;
}

function simulate(candles, modePicker, { rebalanceHrs = 24 } = {}) {
  const warmup = 60;
  let i = warmup;
  let realized = 0, fees = 0, pos = 0, entry = 0, rungs = 0;
  let peak = 0, maxDd = 0;
  let orders = [], levels = [], sizeBase = 0, mode = 'neutral', lastReb = -1e9;

  const mark = (px) => realized + (pos === 0 ? 0 : pos * (px - entry)) - fees;

  function rebuild(price, analysis) {
    mode = typeof modePicker === 'function' ? modePicker(analysis) : modePicker;
    const sug = suggestFromTrend(analysis, { mode, riskProfile: 'steady' });
    if (!sug) return false;
    const g = buildGrid({ lower: sug.lower, upper: sug.upper, gridCount: sug.gridCount });
    levels = g.levels;
    sizeBase = sizeFor(price, sug.leverage || 3);
    orders = seedOrders({ levels, price, mode, spacing: g.spacing }).map((o) => ({
      levelIndex: o.levelIndex, price: o.price, side: o.side,
    }));
    lastReb = i;
    return true;
  }

  function fill(order, px) {
    const qty = sizeBase;
    fees += px * qty * FEE;
    const signed = order.side === 'buy' ? qty : -qty;
    if (pos === 0 || Math.sign(pos) === Math.sign(signed)) {
      const newPos = pos + signed;
      entry = newPos === 0 ? 0 : (Math.abs(pos) * entry + qty * px) / Math.abs(newPos);
      pos = newPos;
    } else {
      const closeQty = Math.min(Math.abs(pos), qty);
      realized += pos > 0 ? closeQty * (px - entry) : closeQty * (entry - px);
      if (closeQty > 0) rungs += 1;
      const remaining = pos + signed;
      if (Math.sign(remaining) === Math.sign(pos) || remaining === 0) {
        pos = remaining;
        if (pos === 0) entry = 0;
      } else {
        pos = remaining;
        entry = px;
      }
    }
    const repl = replacementFor(order, levels, mode);
    orders = orders.filter((o) => !(o.levelIndex === order.levelIndex && o.side === order.side));
    if (repl) orders.push({ levelIndex: repl.levelIndex, price: repl.price, side: repl.side });
  }

  {
    if (!rebuild(candles[i].close, analyzeTrend(candles.slice(0, i + 1)))) return null;
  }

  for (; i < candles.length; i++) {
    const c = candles[i];
    if (i - lastReb >= rebalanceHrs) {
      if (pos !== 0) {
        realized += pos * (c.close - entry);
        fees += Math.abs(pos) * c.close * FEE;
        pos = 0; entry = 0;
      }
      rebuild(c.close, analyzeTrend(candles.slice(0, i + 1)));
    }
    const filledSides = new Set();
    for (let s = 0; s < 8; s++) {
      let hit = null;
      for (const o of orders) {
        if (filledSides.has(o.side)) continue;
        if (o.side === 'buy' && c.low <= o.price) { hit = o; break; }
        if (o.side === 'sell' && c.high >= o.price) { hit = o; break; }
      }
      if (!hit) break;
      filledSides.add(hit.side);
      fill(hit, hit.price);
    }
    const eq = mark(c.close);
    peak = Math.max(peak, eq);
    maxDd = Math.min(maxDd, eq - peak);
  }
  const last = candles.at(-1).close;
  if (pos !== 0) {
    realized += pos * (last - entry);
    fees += Math.abs(pos) * last * FEE;
  }
  const budget = (EQUITY * RISK_P.budget) / SLOTS;
  const net = realized - fees;
  return {
    net: +net.toFixed(2),
    retPct: +((net / budget) * 100).toFixed(2),
    fees: +fees.toFixed(2),
    rungs,
    maxDd: +maxDd.toFixed(2),
  };
}

function stats(xs) {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const avg = xs.reduce((a, b) => a + b, 0) / xs.length;
  return {
    avg: +avg.toFixed(2),
    med: +s[Math.floor(s.length / 2)].toFixed(2),
    win: +((xs.filter((x) => x > 0).length / xs.length) * 100).toFixed(1),
    n: xs.length,
  };
}

async function main() {
  const perSym = [];
  for (const sym of SYMBOLS) {
    process.stderr.write(`fetch ${sym}\n`);
    try {
      const candles = await fetchCandles(sym);
      if (candles.length < 120) continue;
      const row = {
        symbol: sym,
        bars: candles.length,
        from: new Date(candles[0].time).toISOString().slice(0, 10),
        to: new Date(candles.at(-1).time).toISOString().slice(0, 10),
        neutral: simulate(candles, 'neutral'),
        long: simulate(candles, 'long'),
        short: simulate(candles, 'short'),
        adaptive: simulate(candles, (a) => a.recommended || 'neutral'),
      };
      const forced = [row.neutral, row.long, row.short].filter(Boolean);
      row.best = forced.sort((a, b) => b.retPct - a.retPct)[0]
        ? ['neutral', 'long', 'short'].map((m) => ({ m, r: row[m]?.retPct ?? -1e9 })).sort((a, b) => b.r - a.r)[0].m
        : null;
      perSym.push(row);
    } catch (e) {
      process.stderr.write(`  skip ${sym}: ${e.message}\n`);
    }
    await new Promise((r) => setTimeout(r, 120));
  }

  const summary = {};
  for (const m of ['neutral', 'long', 'short', 'adaptive']) {
    summary[m] = {
      ret: stats(perSym.map((r) => r[m]?.retPct).filter((x) => x != null)),
      rungs: stats(perSym.map((r) => r[m]?.rungs).filter((x) => x != null)),
      dd: stats(perSym.map((r) => r[m]?.maxDd).filter((x) => x != null)),
    };
  }
  const bestCount = { neutral: 0, long: 0, short: 0 };
  for (const r of perSym) if (r.best) bestCount[r.best]++;

  const out = {
    meta: {
      source: 'Binance public spot 1H via data-api.binance.vision',
      feeMaker: FEE,
      equity: EQUITY,
      bars: LIMIT,
      symbols: perSym.map((s) => s.symbol),
      note: 'Same grid/trend code as Next K; spot candles proxy for mode comparison (not Bitget funding).',
    },
    summary,
    bestCount,
    perSym: perSym.map((r) => ({
      symbol: r.symbol,
      from: r.from,
      to: r.to,
      best: r.best,
      neutral: r.neutral?.retPct,
      long: r.long?.retPct,
      short: r.short?.retPct,
      adaptive: r.adaptive?.retPct,
      rungsN: r.neutral?.rungs,
      rungsL: r.long?.rungs,
      rungsS: r.short?.rungs,
    })),
  };
  writeFileSync(new URL('./backtest-binance-out.json', import.meta.url), JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out, null, 2));
}

main().catch((e) => { console.error(e); process.exit(1); });
