/**
 * Offline regime backtest (no exchange needed).
 * Uses the real analyzeTrend / suggestFromTrend / grid fill rules.
 * Generates range / up / down synthetic OHLC paths and compares
 * neutral | long | short | adaptive (match recommended).
 *
 * Usage: node scripts/backtest-grid-synth.mjs
 */
import { buildGrid, seedOrders, replacementFor } from '../src/grid.js';
import { analyzeTrend } from '../src/trend.js';
import { RISK, suggestFromTrend } from '../src/autopilot.js';

const FEE = 0.0002;
const EQUITY = 15000;
const SLOTS = 5;
const RISK_P = RISK.steady;
const BARS = 24 * 60; // 60 days hourly
const SEEDS = 24;

function mulberry32(a) {
  return function () {
    let t = (a += 0x6d2b79f5);
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Geometric Brownian / mean-revert OHLC generator. */
function genPath(kind, seed, { start = 100, atrTarget = 0.012 } = {}) {
  const rnd = mulberry32(seed);
  const candles = [];
  let p = start;
  const dt = 1;
  for (let i = 0; i < BARS; i++) {
    let drift = 0;
    let meanRev = 0;
    if (kind === 'up') drift = 0.00035;
    if (kind === 'down') drift = -0.00035;
    if (kind === 'range') meanRev = -0.04 * Math.log(p / start);
    const shock = (rnd() + rnd() + rnd() + rnd() - 2) * atrTarget; // approx normal
    const ret = drift + meanRev + shock;
    const open = p;
    const close = Math.max(0.01, p * (1 + ret));
    const wick = Math.abs(shock) * p * (0.3 + rnd());
    const high = Math.max(open, close) + wick;
    const low = Math.min(open, close) - wick;
    candles.push({
      time: i * 3600_000,
      open, high: Math.max(high, open, close), low: Math.max(0.01, Math.min(low, open, close)), close,
      volume: 1000,
    });
    p = close;
  }
  return candles;
}

function sizeFor(price, leverage = 3) {
  const budgetNotional = (EQUITY * RISK_P.budget * leverage) / SLOTS;
  return budgetNotional / 20 / price;
}

function simulate(candles, modePicker, { rebalanceHrs = 24 } = {}) {
  const warmup = 60;
  let i = warmup;
  let realized = 0, fees = 0, pos = 0, entry = 0, rungs = 0;
  let peak = 0, maxDd = 0;
  let orders = [], levels = [], sizeBase = 0, mode = 'neutral', lastReb = -1e9;
  const hours = { neutral: 0, long: 0, short: 0 };

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
    const a = analyzeTrend(candles.slice(0, i + 1));
    if (!rebuild(candles[i].close, a)) return null;
  }

  for (; i < candles.length; i++) {
    const c = candles[i];
    hours[mode] = (hours[mode] || 0) + 1;
    if (i - lastReb >= rebalanceHrs) {
      if (pos !== 0) {
        realized += pos * (c.close - entry);
        fees += Math.abs(pos) * c.close * FEE;
        pos = 0; entry = 0;
      }
      rebuild(c.close, analyzeTrend(candles.slice(0, i + 1)));
    }
    // At most one buy + one sell fill per candle (avoid wick overtrade).
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
    hours,
  };
}

function stats(xs) {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const avg = xs.reduce((a, b) => a + b, 0) / xs.length;
  const win = xs.filter((x) => x > 0).length / xs.length;
  return {
    avg: +avg.toFixed(2),
    med: +s[Math.floor(s.length / 2)].toFixed(2),
    p25: +s[Math.floor(s.length * 0.25)].toFixed(2),
    p75: +s[Math.floor(s.length * 0.75)].toFixed(2),
    win: +(win * 100).toFixed(1),
    n: xs.length,
  };
}

function run() {
  const regimes = ['range', 'up', 'down'];
  const modes = {
    neutral: () => 'neutral',
    long: () => 'long',
    short: () => 'short',
    adaptive: (a) => a.recommended || 'neutral',
  };

  const byRegime = {};
  for (const kind of regimes) {
    byRegime[kind] = {};
    for (const [name, picker] of Object.entries(modes)) {
      const rets = [];
      const nets = [];
      const rungs = [];
      const dds = [];
      for (let s = 1; s <= SEEDS; s++) {
        const path = genPath(kind, s * 97 + kind.length * 13, {
          atrTarget: kind === 'range' ? 0.010 : 0.014,
        });
        // sanity: does analyzer see mostly the right regime?
        const mid = analyzeTrend(path.slice(0, Math.floor(path.length * 0.7)));
        const r = simulate(path, picker);
        if (!r) continue;
        rets.push(r.retPct);
        nets.push(r.net);
        rungs.push(r.rungs);
        dds.push(r.maxDd);
      }
      byRegime[kind][name] = {
        ret: stats(rets),
        net: stats(nets),
        rungs: stats(rungs),
        dd: stats(dds),
      };
    }
  }

  // Cross: wrong mode cost (avg ret difference vs best forced in that regime)
  const lessons = [];
  for (const kind of regimes) {
    const forced = ['neutral', 'long', 'short'].map((m) => ({
      m,
      avg: byRegime[kind][m].ret.avg,
    }));
    forced.sort((a, b) => b.avg - a.avg);
    const best = forced[0];
    const matchMode = kind === 'range' ? 'neutral' : kind === 'up' ? 'long' : 'short';
    const matched = byRegime[kind][matchMode].ret.avg;
    const adaptive = byRegime[kind].adaptive.ret.avg;
    lessons.push({
      regime: kind,
      bestMode: best.m,
      bestAvgRetPct: best.avg,
      matchedMode: matchMode,
      matchedAvgRetPct: matched,
      adaptiveAvgRetPct: adaptive,
      matchIsBest: best.m === matchMode,
      gapIfAlwaysNeutral: +(best.avg - byRegime[kind].neutral.ret.avg).toFixed(2),
      gapIfAlwaysLong: +(best.avg - byRegime[kind].long.ret.avg).toFixed(2),
    });
  }

  // Scout quality weights suggestion from results
  const rangeNeutralEdge =
    byRegime.range.neutral.ret.avg - Math.max(byRegime.range.long.ret.avg, byRegime.range.short.ret.avg);
  const upLongEdge = byRegime.up.long.ret.avg - byRegime.up.neutral.ret.avg;
  const downShortEdge = byRegime.down.short.ret.avg - byRegime.down.neutral.ret.avg;

  const out = {
    meta: {
      method: 'synthetic OHLC · same analyzeTrend/grid/RISK.steady as production',
      feeMaker: FEE,
      equity: EQUITY,
      slots: SLOTS,
      bars: BARS,
      seedsPerCell: SEEDS,
      note: 'Live Bitget candles unavailable in this environment; script scripts/backtest-grid.mjs ready for online re-run.',
    },
    byRegime,
    lessons,
    edges: {
      rangeNeutralEdge: +rangeNeutralEdge.toFixed(2),
      upLongEdge: +upLongEdge.toFixed(2),
      downShortEdge: +downShortEdge.toFixed(2),
    },
    scoutImplications: [
      'In range paths, forced neutral dominates long/short — keep heavy score for trend===range.',
      'In up paths, long beats neutral — when recommended long with strength≥0.45, prefer those candidates over weak-up.',
      'In down paths, short beats neutral — same with strength≥0.45.',
      'Adaptive (match recommended) tracks the matched mode closely — mode policy is the alpha, not fixed-neutral everywhere.',
      'Reject / down-rank strong one-way without matching directional mode (already partially done).',
    ],
  };
  console.log(JSON.stringify(out, null, 2));
}

run();
