/**
 * Bitget USDT-perp grid backtest: compare neutral / long / short under steady configs.
 * Usage: node scripts/backtest-grid.mjs
 * Public candles only — no API keys.
 */
import { buildGrid, seedOrders, replacementFor } from '../src/grid.js';
import { analyzeTrend } from '../src/trend.js';
import { RISK, suggestFromTrend } from '../src/autopilot.js';

const PRODUCT = 'USDT-FUTURES';
const FEE = 0.0002; // Bitget futures maker per side
const HOURS = 24 * 45; // ~45 days hourly
const EQUITY = 15000;
const SLOTS = 5;
const RISK_P = RISK.steady;

const SYMBOLS = [
  'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'LTCUSDT',
  'XRPUSDT', 'DOGEUSDT', 'ADAUSDT', 'LINKUSDT', 'AVAXUSDT',
];

async function fetchCandles(symbol, limit = 500) {
  const url =
    `https://api.bitget.com/api/v2/mix/market/candles` +
    `?symbol=${symbol}&productType=${PRODUCT}&granularity=1H&limit=${Math.min(limit, 1000)}`;
  const res = await fetch(url, { signal: AbortSignal.timeout(20000) });
  if (!res.ok) throw new Error(`${symbol} HTTP ${res.status}`);
  const j = await res.json();
  const rows = j?.data || [];
  // Bitget: [ts, open, high, low, close, volBase, volQuote]
  return rows
    .map((r) => ({
      time: Number(r[0]),
      open: Number(r[1]),
      high: Number(r[2]),
      low: Number(r[3]),
      close: Number(r[4]),
      volume: Number(r[5]),
    }))
    .filter((c) => c.close > 0)
    .sort((a, b) => a.time - b.time);
}

function sizeFor(price, leverage = 3) {
  const budgetNotional = (EQUITY * RISK_P.budget * leverage) / SLOTS;
  const gridCount = 20;
  let size = budgetNotional / gridCount / price;
  if (!(size > 0)) size = 0;
  return size;
}

/**
 * Path simulation: walk candle by candle; fill resting limits when mid crosses.
 * Inventory MTM marked at last close. Fees on each fill.
 */
function simulateForced(candles, mode, { rebalanceHrs = 24 } = {}) {
  if (candles.length < 80) return null;
  const warmup = 60;
  let i = warmup;
  let equityCash = 0; // realized after fees relative to 0
  let pos = 0;
  let entry = 0;
  let realized = 0;
  let fees = 0;
  let rungs = 0;
  let peak = 0;
  let maxDd = 0;
  let orders = []; // {levelIndex, price, side}
  let levels = [];
  let spacing = 0;
  let sizeBase = 0;
  let lastReb = -Infinity;

  function mark(price) {
    const upnl = pos === 0 ? 0 : pos * (price - entry);
    return realized + upnl - fees;
  }

  function rebuild(price, analysis) {
    const sug = suggestFromTrend(analysis, {
      mode,
      riskProfile: 'steady',
    });
    if (!sug) return false;
    const g = buildGrid({ lower: sug.lower, upper: sug.upper, gridCount: sug.gridCount });
    levels = g.levels;
    spacing = g.spacing;
    sizeBase = sizeFor(price, sug.leverage || 3);
    if (!(sizeBase > 0)) return false;
    orders = seedOrders({
      levels,
      price,
      mode,
      spacing,
    }).map((o) => ({ levelIndex: o.levelIndex, price: o.price, side: o.side }));
    lastReb = i;
    return true;
  }

  function fill(order, px) {
    const qty = sizeBase;
    const fee = px * qty * FEE;
    fees += fee;
    const signed = order.side === 'buy' ? qty : -qty;
    if (pos === 0 || Math.sign(pos) === Math.sign(signed)) {
      const newPos = pos + signed;
      entry = newPos === 0 ? 0 : (Math.abs(pos) * entry + Math.abs(signed) * px) / Math.abs(newPos);
      pos = newPos;
    } else {
      const closeQty = Math.min(Math.abs(pos), Math.abs(signed));
      const pnl = pos > 0 ? closeQty * (px - entry) : closeQty * (entry - px);
      realized += pnl;
      const remaining = pos + signed;
      if (Math.sign(remaining) === Math.sign(pos) || remaining === 0) {
        pos = remaining;
        if (pos === 0) entry = 0;
      } else {
        pos = remaining;
        entry = px;
      }
      if (Math.abs(remaining) < Math.abs(pos + signed) || closeQty > 0) {
        // count a completed rung when we closed inventory via the opposite side
        if ((order.side === 'sell' && signed < 0 && pnl !== 0) || (order.side === 'buy' && signed > 0 && pnl !== 0)) {
          if (Math.abs(pnl) > 0) rungs += 1;
        }
      }
    }
    const repl = replacementFor(order, levels, mode);
    orders = orders.filter((o) => !(o.levelIndex === order.levelIndex && o.side === order.side));
    if (repl) orders.push({ levelIndex: repl.levelIndex, price: repl.price, side: repl.side });
  }

  // init
  {
    const slice = candles.slice(0, i + 1);
    const analysis = analyzeTrend(slice);
    if (!rebuild(candles[i].close, analysis)) return null;
  }

  for (; i < candles.length; i++) {
    const c = candles[i];
    if (i - lastReb >= rebalanceHrs) {
      const analysis = analyzeTrend(candles.slice(0, i + 1));
      // keep inventory; only rebuild ladder around price
      rebuild(c.close, analysis);
    }

    // Process fills using candle path: check lows then highs for buys/sells
    let safety = 0;
    while (safety++ < 40) {
      let hit = null;
      for (const o of orders) {
        if (o.side === 'buy' && c.low <= o.price) { hit = o; break; }
        if (o.side === 'sell' && c.high >= o.price) { hit = o; break; }
      }
      if (!hit) break;
      fill(hit, hit.price);
    }

    const eq = mark(c.close);
    peak = Math.max(peak, eq);
    maxDd = Math.min(maxDd, eq - peak);
  }

  // flatten inventory at last
  const last = candles[candles.length - 1].close;
  if (pos !== 0) {
    realized += pos * (last - entry);
    fees += Math.abs(pos) * last * FEE;
    pos = 0;
  }
  const net = realized - fees;
  const notionalBudget = (EQUITY * RISK_P.budget) / SLOTS;
  const retPct = (net / notionalBudget) * 100;
  return {
    mode,
    net: round2(net),
    fees: round2(fees),
    rungs,
    retPct: round2(retPct),
    maxDd: round2(maxDd),
    bars: candles.length - warmup,
  };
}

/** Same as forced, but mode follows analyzeTrend.recommended each rebalance. */
function simulateAdaptive(candles, { rebalanceHrs = 24 } = {}) {
  if (candles.length < 80) return null;
  const warmup = 60;
  let i = warmup;
  let realized = 0;
  let fees = 0;
  let pos = 0;
  let entry = 0;
  let rungs = 0;
  let peak = 0;
  let maxDd = 0;
  let orders = [];
  let levels = [];
  let spacing = 0;
  let sizeBase = 0;
  let mode = 'neutral';
  let lastReb = -Infinity;
  const modeHours = { neutral: 0, long: 0, short: 0 };

  function mark(price) {
    const upnl = pos === 0 ? 0 : pos * (price - entry);
    return realized + upnl - fees;
  }

  function rebuild(price, analysis) {
    mode = analysis.recommended || 'neutral';
    const sug = suggestFromTrend(analysis, { mode, riskProfile: 'steady' });
    if (!sug) return false;
    const g = buildGrid({ lower: sug.lower, upper: sug.upper, gridCount: sug.gridCount });
    levels = g.levels;
    spacing = g.spacing;
    sizeBase = sizeFor(price, sug.leverage || 3);
    orders = seedOrders({ levels, price, mode, spacing }).map((o) => ({
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
      entry = newPos === 0 ? 0 : (Math.abs(pos) * entry + Math.abs(signed) * px) / Math.abs(newPos);
      pos = newPos;
    } else {
      const closeQty = Math.min(Math.abs(pos), Math.abs(signed));
      const pnl = pos > 0 ? closeQty * (px - entry) : closeQty * (entry - px);
      realized += pnl;
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
    const analysis = analyzeTrend(candles.slice(0, i + 1));
    if (!rebuild(candles[i].close, analysis)) return null;
  }

  for (; i < candles.length; i++) {
    const c = candles[i];
    modeHours[mode] = (modeHours[mode] || 0) + 1;
    if (i - lastReb >= rebalanceHrs) {
      // flatten before mode change to avoid inventory mismatch
      if (pos !== 0) {
        realized += pos * (c.close - entry);
        fees += Math.abs(pos) * c.close * FEE;
        pos = 0;
        entry = 0;
      }
      const analysis = analyzeTrend(candles.slice(0, i + 1));
      rebuild(c.close, analysis);
    }
    let safety = 0;
    while (safety++ < 40) {
      let hit = null;
      for (const o of orders) {
        if (o.side === 'buy' && c.low <= o.price) { hit = o; break; }
        if (o.side === 'sell' && c.high >= o.price) { hit = o; break; }
      }
      if (!hit) break;
      fill(hit, hit.price);
    }
    const eq = mark(c.close);
    peak = Math.max(peak, eq);
    maxDd = Math.min(maxDd, eq - peak);
  }
  const last = candles[candles.length - 1].close;
  if (pos !== 0) {
    realized += pos * (last - entry);
    fees += Math.abs(pos) * last * FEE;
  }
  const net = realized - fees;
  const notionalBudget = (EQUITY * RISK_P.budget) / SLOTS;
  return {
    mode: 'adaptive',
    net: round2(net),
    fees: round2(fees),
    rungs,
    retPct: round2((net / notionalBudget) * 100),
    maxDd: round2(maxDd),
    modeHours,
    bars: candles.length - warmup,
  };
}

function round2(x) { return Math.round(Number(x) * 100) / 100; }

function summarize(rows, key) {
  const xs = rows.map((r) => r[key]).filter((n) => Number.isFinite(n));
  if (!xs.length) return null;
  const avg = xs.reduce((a, b) => a + b, 0) / xs.length;
  const win = xs.filter((x) => x > 0).length / xs.length;
  return { avg: round2(avg), win: round2(win * 100), n: xs.length, median: round2(xs.sort((a, b) => a - b)[Math.floor(xs.length / 2)]) };
}

async function main() {
  const perSym = [];
  for (const sym of SYMBOLS) {
    process.stderr.write(`fetch ${sym}…\n`);
    let candles;
    try {
      candles = await fetchCandles(sym, Math.min(HOURS + 80, 1000));
    } catch (e) {
      process.stderr.write(`  skip ${sym}: ${e.message}\n`);
      continue;
    }
    if (candles.length < 100) {
      process.stderr.write(`  skip ${sym}: only ${candles.length} bars\n`);
      continue;
    }

    // Regime share over window
    let rangeN = 0, upN = 0, downN = 0;
    for (let t = 60; t < candles.length; t += 12) {
      const a = analyzeTrend(candles.slice(0, t + 1));
      if (a.trend === 'up') upN++;
      else if (a.trend === 'down') downN++;
      else rangeN++;
    }
    const tot = rangeN + upN + downN || 1;

    const row = {
      symbol: sym,
      bars: candles.length,
      regime: {
        range: round2((rangeN / tot) * 100),
        up: round2((upN / tot) * 100),
        down: round2((downN / tot) * 100),
      },
      neutral: simulateForced(candles, 'neutral'),
      long: simulateForced(candles, 'long'),
      short: simulateForced(candles, 'short'),
      adaptive: simulateAdaptive(candles),
    };
    // Best forced
    const forced = [row.neutral, row.long, row.short].filter(Boolean);
    row.bestForced = forced.sort((a, b) => b.retPct - a.retPct)[0]?.mode || null;
    perSym.push(row);
    await new Promise((r) => setTimeout(r, 200));
  }

  const modes = ['neutral', 'long', 'short', 'adaptive'];
  const summary = {};
  for (const m of modes) {
    const rows = perSym.map((r) => r[m]).filter(Boolean);
    summary[m] = {
      ret: summarize(rows, 'retPct'),
      net: summarize(rows, 'net'),
      rungs: summarize(rows, 'rungs'),
      dd: summarize(rows, 'maxDd'),
    };
  }

  // Match analysis: when adaptive spent most hours in a mode, did forced that mode win?
  let matchWins = 0, mismatchPenalty = 0, nMatch = 0;
  for (const r of perSym) {
    if (!r.adaptive?.modeHours) continue;
    const mh = r.adaptive.modeHours;
    const top = Object.entries(mh).sort((a, b) => b[1] - a[1])[0][0];
    const forcedRet = { neutral: r.neutral?.retPct, long: r.long?.retPct, short: r.short?.retPct };
    const best = Object.entries(forcedRet).sort((a, b) => (b[1] ?? -999) - (a[1] ?? -999))[0][0];
    nMatch++;
    if (top === best) matchWins++;
    mismatchPenalty += (forcedRet[best] ?? 0) - (forcedRet[top] ?? 0);
  }

  const out = {
    meta: {
      equity: EQUITY,
      slots: SLOTS,
      feeMaker: FEE,
      horizonHours: HOURS,
      symbols: perSym.map((r) => r.symbol),
      note: 'Hourly Bitget candles; steady RISK; daily range rebuild; maker fee 0.02%/side; MTM flatten at end.',
    },
    summary,
    matchRate: nMatch ? round2((matchWins / nMatch) * 100) : null,
    avgMismatchGapPct: nMatch ? round2(mismatchPenalty / nMatch) : null,
    perSym,
  };
  console.log(JSON.stringify(out, null, 2));
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
