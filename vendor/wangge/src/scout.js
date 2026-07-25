// Fleet scout: maintain a scored USDT-perp candidate pool and auto manage bots (max N).
import { analyzeTrend } from './trend.js';
import { suggestFromTrend, RISK } from './autopilot.js';
import { normalizeSymbol } from './config.js';
import { pushAutoLog } from './autolog.js';

/** Liquid majors — default auto-eligible at scoreIn. */
export const SCOUT_CORE = Object.freeze([
  'BTCUSDT', 'ETHUSDT', 'SOLUSDT',
]);

/**
 * Second-tier large caps — eligible only with higher score + clear range/weak trend.
 * Empty by default (only BTC/ETH/SOL). Override via SCOUT_ALLOWLIST_SECONDARY.
 */
export const SCOUT_SECONDARY = Object.freeze([]);

const DEFAULTS = {
  enabled: true,
  maxBots: 3,
  intervalMs: 2 * 60 * 60_000, // 2h — slow cadence, less churn
  poolSize: 30,          // volume-ranked screen before ATR scoring (within allowlist)
  scoreIn: 0.68,         // core entry floor
  scoreInSecondary: 0.76, // secondary needs clearer grid setup
  scoreOut: 0.22,        // eviction only when clearly broken
  strikesToEvict: 4,     // ~4× interval of weakness before remove
  atrMin: 0.45,
  atrMax: 2.2,
  replaceGap: 0.28,      // replace only if challenger is clearly better
  minHoldMs: 6 * 60 * 60_000, // do not evict/replace a bot younger than 6h
  confirmEntries: 2,     // need N consecutive high scores before opening
};

function parseSymList(raw, fallback) {
  if (raw == null || String(raw).trim() === '') return [...fallback];
  return String(raw).split(/[,;\s]+/)
    .map((s) => normalizeSymbol(s))
    .filter(Boolean);
}

export function createScout(ctx) {
  const {
    exchange,
    fleet,
    persistSymbols,
    getMode = () => 'paper',
    log = console.log,
  } = ctx;

  const state = {
    ...DEFAULTS,
    core: new Set(SCOUT_CORE),
    secondary: new Set(SCOUT_SECONDARY),
    pool: [],
    lastRunAt: 0,
    lastError: null,
    lastActions: [],
    strikes: Object.create(null),
    openedAt: Object.create(null),
    entryStreak: Object.create(null), // symbol -> consecutive in-threshold scans
    busy: false,
    timer: null,
  };

  function tierOf(sym) {
    if (state.core.has(sym)) return 'core';
    if (state.secondary.has(sym)) return 'secondary';
    return null;
  }

  function isAllowlisted(sym) {
    return tierOf(sym) != null;
  }

  /** Secondary only enters on range / weak trend; core uses scoreIn alone. */
  function passesEntryGate(row) {
    const tier = row.tier || tierOf(row.symbol);
    if (!tier) return false;
    if (tier === 'core') return row.score >= state.scoreIn;
    if (row.score < state.scoreInSecondary) return false;
    const strength = Number(row.strength) || 0;
    return row.trend === 'range' || strength < 0.45;
  }

  function configure(partial = {}) {
    if (partial.enabled != null) state.enabled = !!partial.enabled;
    if (partial.maxBots != null) state.maxBots = Math.min(5, Math.max(1, Number(partial.maxBots) || 5));
    if (partial.intervalMs != null) state.intervalMs = Math.max(30 * 60_000, Number(partial.intervalMs) || DEFAULTS.intervalMs);
    if (partial.scoreIn != null) state.scoreIn = Number(partial.scoreIn);
    if (partial.scoreInSecondary != null) state.scoreInSecondary = Number(partial.scoreInSecondary);
    if (partial.scoreOut != null) state.scoreOut = Number(partial.scoreOut);
    if (partial.minHoldMs != null) state.minHoldMs = Math.max(60_000, Number(partial.minHoldMs) || DEFAULTS.minHoldMs);
    if (partial.confirmEntries != null) state.confirmEntries = Math.max(1, Number(partial.confirmEntries) || DEFAULTS.confirmEntries);
    if (partial.replaceGap != null) state.replaceGap = Number(partial.replaceGap);
    if (partial.core != null) {
      state.core = new Set(Array.isArray(partial.core) ? partial.core.map(normalizeSymbol) : parseSymList(partial.core, SCOUT_CORE));
    }
    if (partial.secondary != null) {
      state.secondary = new Set(Array.isArray(partial.secondary) ? partial.secondary.map(normalizeSymbol) : parseSymList(partial.secondary, SCOUT_SECONDARY));
    }
  }

  function pushAction(a) {
    state.lastActions.unshift({ t: Date.now(), ...a });
    if (state.lastActions.length > 30) state.lastActions.pop();
    const label = ({
      open: '开仓托管',
      'open-fail': '开仓失败',
      evict: '淘汰下架',
      'replace-out': '替换出局',
      trim: '裁剪超额',
    })[a.type] || a.type;
    pushAutoLog({
      source: '选币官',
      symbol: a.symbol || '',
      type: a.type || 'info',
      message: `${label}${a.symbol ? ' ' + a.symbol : ''}${a.score != null ? `（分 ${a.score}）` : ''}${a.reason ? ' · ' + a.reason : ''}`,
    });
  }

  async function loadVolumeMap() {
    const apiUrl = exchange.apiUrl || 'https://api.bitget.com';
    const productType = exchange.productType || 'USDT-FUTURES';
    try {
      const res = await fetch(
        `${apiUrl}/api/v2/mix/market/tickers?productType=${encodeURIComponent(productType)}`,
        { signal: AbortSignal.timeout(12000) },
      );
      if (!res.ok) return new Map();
      const j = await res.json();
      if (j.code !== '00000') return new Map();
      const map = new Map();
      for (const t of j.data || []) {
        const sym = String(t.symbol || '').toUpperCase();
        if (!sym.endsWith('USDT')) continue;
        const vol = Number(t.usdtVolume || t.quoteVolume || t.baseVolume || 0);
        const last = Number(t.lastPr || t.markPrice || 0);
        map.set(sym, { vol: Number.isFinite(vol) ? vol : 0, last: Number.isFinite(last) ? last : 0 });
      }
      return map;
    } catch {
      return new Map();
    }
  }

  function hardFilter(m, volInfo) {
    const name = normalizeSymbol(m.name || m.displayName || '');
    if (!name || !name.endsWith('USDT')) return false;
    if (!isAllowlisted(name)) return false;
    // Skip leveraged token style names
    if (/^(1000|10000)/.test(name)) return false;
    if ((m.maxLeverage || 50) < 3) return false;
    const px = Number(m.lastPrice || volInfo?.last || 0);
    const minSz = Number(m.minOrderSize || m.stepSize || 0);
    if (!(px > 0) || !(minSz > 0)) return false;
    // One step notional shouldn't be absurd vs a small account
    if (minSz * px > 250) return false;
    return true;
  }

  function scoreAnalysis(analysis, volRank, poolN, tier) {
    if (!analysis) return { score: 0, why: '无K线' };
    const atr = Number(analysis.atrPct);
    if (!(atr > 0)) return { score: 0, why: '无ATR' };
    let score = 0;
    const why = [];

    // Prefer a mid ATR band; outside band is heavily penalized (quality over quantity)
    if (atr >= state.atrMin && atr <= state.atrMax) {
      const mid = (state.atrMin + state.atrMax) / 2;
      const closeness = 1 - Math.min(1, Math.abs(atr - mid) / mid);
      score += 0.38 * closeness;
      why.push(`ATR${atr.toFixed(2)}%合适`);
    } else if (atr < state.atrMin) {
      score += 0.02;
      why.push(`ATR过低${atr.toFixed(2)}%`);
    } else {
      score += 0.03;
      why.push(`ATR过高${atr.toFixed(2)}%`);
    }

    // Regime: pure range best (backtest: neutral >> directional in range AND mild trends).
    const strength = Number(analysis.strength) || 0;
    if (analysis.trend === 'range') {
      score += 0.45;
      why.push('震荡适合中性');
    } else if (strength < 0.45) {
      score += 0.18;
      why.push(`弱${analysis.trend}·宜中性`);
    } else if (strength < 0.70) {
      score += 0.08;
      why.push(`中等${analysis.trend}·慎单边`);
    } else {
      score -= 0.05;
      why.push(`强趋势回避`);
    }

    // Liquidity: only reward top of the pool (stick to thick books)
    const volScore = poolN > 1 ? (1 - (volRank / (poolN - 1))) : 1;
    const volWeight = volRank < Math.max(8, Math.floor(poolN * 0.35)) ? 0.22 : 0.08;
    score += volWeight * volScore;
    why.push(`量排名${volRank + 1}/${poolN}`);

    if (tier === 'core') {
      score += 0.05;
      why.push('核心白名单');
    } else if (tier === 'secondary') {
      why.push('次核心·门槛更高');
    }

    return { score: Math.max(0, Math.min(1, Number(score.toFixed(3)))), why: why.join(' · ') };
  }

  async function buildPool() {
    if (typeof exchange.ensureSymbol === 'function') {
      // refresh full catalog when possible
      try { await exchange.ensureSymbol('BTCUSDT'); } catch { /* */ }
    }
    const markets = await exchange.getMarkets();
    const volMap = await loadVolumeMap();

    const candidates = [];
    for (const m of markets) {
      const name = normalizeSymbol(m.name || m.displayName || '');
      const vi = volMap.get(name);
      if (!hardFilter(m, vi)) continue;
      candidates.push({
        symbol: name,
        marketId: m.marketId,
        displayName: m.displayName || name,
        lastPrice: Number(m.lastPrice || vi?.last || 0),
        stepSize: m.stepSize,
        minOrderSize: m.minOrderSize || m.stepSize,
        maxLeverage: m.maxLeverage,
        vol: vi?.vol || 0,
      });
    }
    candidates.sort((a, b) => b.vol - a.vol);
    const top = candidates.slice(0, state.poolSize);

    const scored = [];
    for (let i = 0; i < top.length; i++) {
      const c = top[i];
      const tier = tierOf(c.symbol);
      let analysis = null;
      try {
        const candles = await exchange.getCandles(c.marketId, 3600, 120);
        analysis = analyzeTrend(candles || []);
      } catch { /* */ }
      const { score, why } = scoreAnalysis(analysis, i, top.length, tier);
      scored.push({
        ...c,
        tier,
        score,
        why,
        trend: analysis?.trend || null,
        recommended: analysis?.recommended || null,
        strength: analysis?.strength ?? null,
        atrPct: analysis?.atrPct ?? null,
        price: analysis?.price || c.lastPrice,
      });
      // gentle pacing to avoid hammering public API
      if (i > 0 && i % 8 === 0) await sleep(200);
    }
    scored.sort((a, b) => b.score - a.score);
    state.pool = scored;
    return scored;
  }

  function sizeBaseFor(market, price, leverage, equity, slots) {
    const r = RISK.aggressive;
    const bal = Number(equity) > 0 ? Number(equity) : 15000;
    const n = Math.max(1, slots);
    const budgetNotional = (bal * (r.budget || 0.7) * leverage) / n;
    const step = Number(market.stepSize) || 0.0001;
    const minSz = Number(market.minOrderSize || step);
    const gridCount = 20;
    let size = Math.floor((budgetNotional / gridCount / price) / step) * step;
    if (!(size > 0) || size < minSz) size = minSz;
    // round to step decimals
    const decimals = String(step).includes('.') ? String(step).split('.')[1].length : 0;
    return Number(size.toFixed(Math.min(8, decimals || 4)));
  }

  async function accountEquity() {
    if (typeof exchange.equity === 'number' && Number.isFinite(exchange.equity)) return exchange.equity;
    if (typeof exchange.balance === 'number' && Number.isFinite(exchange.balance)) return exchange.balance;
    return Number(process.env.PAPER_BALANCE || 15000);
  }

  async function openBot(row) {
    const sym = row.symbol;
    await fleet.resolveMarket(sym);
    fleet.add(sym);
    const bot = fleet.get(sym);
    if (!bot) throw new Error('无法创建 bot ' + sym);
    if (bot.running) return { ok: true, skipped: true };

    const market = await fleet.resolveMarket(sym);
    if (!market) throw new Error('找不到市场 ' + sym);
    const analysis = {
      trend: row.trend, recommended: row.recommended || 'neutral',
      strength: row.strength || 0, atrPct: row.atrPct, price: row.price || market.lastPrice,
    };
    // Backtest: forced neutral beat long/short across range/up/down synthetics.
    // Only use directional when signal is very strong (≥0.75).
    const strength = Number(analysis.strength) || 0;
    let mode = 'neutral';
    if ((analysis.recommended === 'long' || analysis.recommended === 'short') && strength >= 0.75) {
      mode = analysis.recommended;
    }
    const sug = suggestFromTrend(analysis, { mode, riskProfile: 'aggressive' });
    if (!sug) throw new Error('无法生成网格参数 ' + sym);
    const equity = await accountEquity();
    const lev = Math.min(sug.leverage || 3, market.maxLeverage || 50);
    const sizeBase = sizeBaseFor(market, analysis.price || market.lastPrice, lev, equity, state.maxBots);

    try {
      await bot.start({
        marketId: market.marketId,
        mode: sug.mode,
        lower: sug.lower,
        upper: sug.upper,
        gridCount: sug.gridCount,
        sizeBase,
        leverage: lev,
        outOfRangeAction: 'recover',
        autopilot: { enabled: true, riskProfile: 'aggressive', allowFlip: true },
      });
    } catch (e) {
      try { await fleet.remove(sym); } catch { /* */ }
      throw e;
    }
    state.strikes[sym] = 0;
    state.openedAt[sym] = Date.now();
    delete state.entryStreak[sym];
    try { persistSymbols?.(); } catch { /* */ }
    return { ok: true };
  }

  function heldLongEnough(sym) {
    const t0 = state.openedAt[sym];
    if (!t0) {
      state.openedAt[sym] = Date.now();
      return false;
    }
    return Date.now() - t0 >= state.minHoldMs;
  }

  async function closeBot(sym, { remove = true } = {}) {
    const bot = fleet.get(sym);
    if (!bot) {
      delete state.strikes[sym];
      delete state.openedAt[sym];
      delete state.entryStreak[sym];
      return { ok: true, closeOk: true };
    }
    let closeOk = true;
    try {
      const st = await bot.stop({ closePosition: true });
      closeOk = st?.closeOk !== false;
    } catch (e) {
      log('[选币] 停止 ' + sym + ' 失败: ' + (e?.message || e));
      closeOk = false;
    }
    if (!closeOk) {
      log('[选币] ' + sym + ' 平仓未确认，保留名额且不删除');
      return { ok: false, closeOk: false };
    }
    let removed = !remove;
    if (remove) {
      try {
        const r = await fleet.remove(sym);
        removed = !!r?.ok || !fleet.get(sym);
      } catch (e) {
        log('[选币] 移除 ' + sym + ' 失败: ' + (e?.message || e));
        removed = !fleet.get(sym);
      }
      try { persistSymbols?.(); } catch { /* */ }
    }
    if (removed || !remove) {
      delete state.strikes[sym];
      delete state.openedAt[sym];
      delete state.entryStreak[sym];
    }
    return { ok: removed || !remove, closeOk: true };
  }

  function seedOpenedAt() {
    for (const sym of fleet.list()) {
      if (fleet.get(sym)?.running && !state.openedAt[sym]) state.openedAt[sym] = Date.now();
    }
  }

  async function tick() {
    if (!state.enabled) return { ok: false, reason: 'disabled' };
    if (state.busy) return { ok: false, reason: 'busy' };
    state.busy = true;
    state.lastError = null;
    try {
      seedOpenedAt();
      const pool = await buildPool();
      const bySym = new Map(pool.map((p) => [p.symbol, p]));
      const held = fleet.list();

      const inPoolTop = new Set(pool.filter((p) => passesEntryGate(p)).map((p) => p.symbol));
      for (const sym of Object.keys(state.entryStreak)) {
        if (!inPoolTop.has(sym)) delete state.entryStreak[sym];
      }
      for (const row of pool) {
        if (passesEntryGate(row)) {
          state.entryStreak[row.symbol] = (state.entryStreak[row.symbol] || 0) + 1;
        }
      }

      for (const sym of [...held]) {
        const row = bySym.get(sym);
        const score = row?.score ?? 0;
        // Off-allowlist leftovers accumulate strikes so they get cleaned out.
        if (!row || !isAllowlisted(sym) || score < state.scoreOut) {
          state.strikes[sym] = (state.strikes[sym] || 0) + 1;
        } else {
          state.strikes[sym] = 0;
        }
        if ((state.strikes[sym] || 0) >= state.strikesToEvict && heldLongEnough(sym)) {
          const closed = await closeBot(sym, { remove: true });
          if (closed.ok) {
            pushAction({ type: 'evict', symbol: sym, score, reason: row?.why || '连续评分过低' });
            log('[选币] 淘汰 ' + sym + '（分 ' + score + '）');
          }
        }
      }

      let active = fleet.list().filter((s) => fleet.get(s)?.running);
      // Keep slots occupied when a bot stopped but close failed (openedAt retained).
      const occupied = fleet.list().filter((s) => fleet.get(s)?.running || state.openedAt[s]);
      let slots = state.maxBots - occupied.length;

      if (slots <= 0 && pool.length) {
        const activeRows = active.map((s) => ({
          symbol: s,
          score: bySym.get(s)?.score ?? 0,
        })).sort((a, b) => a.score - b.score);
        const weakest = activeRows[0];
        const bestOut = pool.find((p) => !active.includes(p.symbol));
        if (
          weakest && bestOut
          && heldLongEnough(weakest.symbol)
          && bestOut.score - weakest.score >= state.replaceGap
          && passesEntryGate(bestOut)
          && (state.entryStreak[bestOut.symbol] || 0) >= state.confirmEntries
        ) {
          const closed = await closeBot(weakest.symbol, { remove: true });
          if (closed.ok) {
            pushAction({ type: 'replace-out', symbol: weakest.symbol, score: weakest.score, reason: '让位给 ' + bestOut.symbol });
            log('[选币] 替换出局 ' + weakest.symbol + ' → 候选 ' + bestOut.symbol);
            slots = 1;
            active = fleet.list().filter((s) => fleet.get(s)?.running);
          }
        }
      }

      // Cold bootstrap: empty fleet → one scan fills up to maxBots among scoreIn.
      // Once any bot is live, new entries need confirmEntries consecutive passes.
      const bootstrap = active.length === 0 && occupied.length === 0;
      const needConfirm = bootstrap ? 1 : state.confirmEntries;
      for (const row of pool) {
        if (slots <= 0) break;
        if (!passesEntryGate(row)) continue;
        if ((state.entryStreak[row.symbol] || 0) < needConfirm) continue;
        if (fleet.get(row.symbol)?.running) continue;
        try {
          if (!fleet.get(row.symbol)) {
            if (fleet.list().length >= state.maxBots) {
              const idle = fleet.list().find((s) => !fleet.get(s)?.running && !state.openedAt[s]);
              if (idle) await closeBot(idle, { remove: true });
              if (fleet.list().length >= state.maxBots) continue;
            }
          }
          await openBot(row);
          pushAction({
            type: 'open',
            symbol: row.symbol,
            score: row.score,
            reason: (row.why || '') + (bootstrap ? ' · 冷启动' : ' · 确认×' + needConfirm),
          });
          log('[选币] 开仓托管 ' + row.symbol + '（分 ' + row.score + ' · ' + row.why + '）');
          slots -= 1;
        } catch (e) {
          pushAction({ type: 'open-fail', symbol: row.symbol, reason: e?.message || String(e) });
          log('[选币] 打开 ' + row.symbol + ' 失败: ' + (e?.message || e));
        }
      }

      while (fleet.list().length > state.maxBots) {
        const idle = fleet.list().find((s) => !fleet.get(s)?.running) || fleet.list()[fleet.list().length - 1];
        await closeBot(idle, { remove: true });
        pushAction({ type: 'trim', symbol: idle, reason: '超过最多 5 个名额' });
      }

      state.lastRunAt = Date.now();
      pushAutoLog({
        source: '选币官',
        type: 'scan',
        message: '巡检完成 · 候选 ' + pool.length + ' · 运行 ' + fleet.list().filter((s) => fleet.get(s)?.running).length + '/' + state.maxBots
          + (pool[0] ? ' · 头名 ' + pool[0].symbol + '（分 ' + pool[0].score + '）' : ''),
      });
      return { ok: true, pool: state.pool.slice(0, 15), actions: state.lastActions.slice(0, 5) };
    } catch (e) {
      state.lastError = e?.message || String(e);
      log('[选币] 巡检失败: ' + state.lastError);
      return { ok: false, error: state.lastError };
    } finally {
      state.busy = false;
    }
  }

  function status() {
    return {
      enabled: state.enabled,
      maxBots: state.maxBots,
      intervalMs: state.intervalMs,
      lastRunAt: state.lastRunAt,
      lastError: state.lastError,
      busy: state.busy,
      pool: state.pool.slice(0, 15),
      strikes: { ...state.strikes },
      entryStreak: { ...state.entryStreak },
      minHoldMs: state.minHoldMs,
      confirmEntries: state.confirmEntries,
      scoreIn: state.scoreIn,
      scoreInSecondary: state.scoreInSecondary,
      scoreOut: state.scoreOut,
      core: [...state.core],
      secondary: [...state.secondary],
      actions: state.lastActions.slice(0, 12),
      running: fleet.list().filter((s) => fleet.get(s)?.running),
      symbols: fleet.list(),
      mode: getMode(),
    };
  }

  function start() {
    configure({
      enabled: process.env.SCOUT_ENABLED !== '0' && process.env.SCOUT_ENABLED !== 'false',
      maxBots: Number(process.env.SCOUT_MAX_BOTS || DEFAULTS.maxBots),
      intervalMs: Number(process.env.SCOUT_INTERVAL_MS || DEFAULTS.intervalMs),
      minHoldMs: Number(process.env.SCOUT_MIN_HOLD_MS || DEFAULTS.minHoldMs),
      scoreInSecondary: process.env.SCOUT_SCORE_IN_SECONDARY != null
        ? Number(process.env.SCOUT_SCORE_IN_SECONDARY)
        : DEFAULTS.scoreInSecondary,
      core: parseSymList(process.env.SCOUT_ALLOWLIST, SCOUT_CORE),
      secondary: parseSymList(process.env.SCOUT_ALLOWLIST_SECONDARY, SCOUT_SECONDARY),
    });
    stop();
    if (!state.enabled) {
      log('[选币] 已关闭（SCOUT_ENABLED=0）');
      return;
    }
    // First run after markets warm up
    setTimeout(() => { tick().catch(() => {}); }, 20_000).unref?.();
    state.timer = setInterval(() => { tick().catch(() => {}); }, state.intervalMs);
    state.timer.unref?.();
    log(`[选币] 已启动：最多 ${state.maxBots} 个机器人，白名单核心 ${state.core.size} + 次核心 ${state.secondary.size}，约每 ${Math.round(state.intervalMs / 60000)} 分钟巡检`);
    pushAutoLog({
      source: '选币官',
      type: 'boot',
      message: `选币官已启动 · 最多 ${state.maxBots} 个 · 白名单 ${state.core.size}+${state.secondary.size} · 每 ${Math.round(state.intervalMs / 60000)} 分钟巡检 · 入场需确认 · 最短持有 ${Math.round(state.minHoldMs / 3600000)}h`,
    });
  }

  function stop() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  }

  return { start, stop, tick, status, configure };
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }
