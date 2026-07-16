// Autopilot: deterministic trend-driven adjust / guard (no LLM, no human confirm).
// Mirrors the console RISK + applyAutoRange formulas so paper/UI/server stay aligned.

export const RISK = {
  steady:     { rMul: 4, rMin: 0.02, rMax: 0.06, sMul: 0.5, sMin: 0.5, sMax: 1, lev: { hi: 2, mid: 2, lo: 3 }, budget: 0.70, skew: 1.2 },
  // Online default: aggressive range + fixed 30x
  aggressive: { rMul: 2.5, rMin: 0.015, rMax: 0.04, sMul: 0.25, sMin: 0.2, sMax: 0.5, lev: { hi: 30, mid: 30, lo: 30 }, budget: 0.90, skew: 1.4 },
};

export function defaultAutopilot(partial = {}) {
  return {
    enabled: partial.enabled !== false, // default ON — hands-off after start
    riskProfile: partial.riskProfile === 'steady' ? 'steady' : 'aggressive',
    intervalMs: Math.max(60_000, Number(partial.intervalMs) || 10 * 60_000),
    minStrength: clamp(Number(partial.minStrength ?? 0.55), 0.15, 0.9),
    coolDownMs: Math.max(60_000, Number(partial.coolDownMs) || 30 * 60_000),
    // Paper and live share the same policy: allow reverse flip on strong signals
    allowFlip: partial.allowFlip != null ? !!partial.allowFlip : true,
    edgePct: clamp(Number(partial.edgePct ?? 0.15), 0.05, 0.4), // adjust when price in outer 15% of range
    lastActionAt: Number(partial.lastActionAt) || 0,
    lastAction: partial.lastAction || null,
    lastTrend: partial.lastTrend || null,
  };
}

/**
 * Suggest grid bounds (+ optional gridCount/leverage) from analyzeTrend result.
 */
export function suggestFromTrend(analysis, { mode, riskProfile = 'aggressive', gridCount, leverage } = {}) {
  const r = RISK[riskProfile] || RISK.aggressive;
  const p = Number(analysis?.price);
  if (!(p > 0)) return null;
  const atrPct = Number(analysis?.atrPct) > 0 ? Number(analysis.atrPct) : 0.8;
  const halfPct = Math.min(r.rMax, Math.max(r.rMin, (atrPct / 100) * r.rMul));
  let lo = p * (1 - halfPct);
  let hi = p * (1 + halfPct);
  const m = mode || analysis.recommended || 'neutral';
  if (m === 'long') { hi = p * (1 + halfPct * r.skew); lo = p * (1 - halfPct * (2 - r.skew)); }
  if (m === 'short') { lo = p * (1 - halfPct * r.skew); hi = p * (1 + halfPct * (2 - r.skew)); }

  let n = gridCount;
  if (n == null) {
    const targetSpacingPct = Math.min(r.sMax, Math.max(r.sMin, atrPct * r.sMul));
    n = Math.round(((hi - lo) / p * 100) / targetSpacingPct);
    n = Math.min(50, Math.max(10, n));
  }
  let lev = leverage;
  if (lev == null) lev = atrPct > 3 ? r.lev.hi : atrPct > 1.5 ? r.lev.mid : r.lev.lo;

  return {
    mode: m,
    lower: round6(lo),
    upper: round6(hi),
    gridCount: n,
    leverage: lev,
    atrPct,
    strength: analysis.strength ?? 0,
    trend: analysis.trend,
    recommended: analysis.recommended,
  };
}

/**
 * Decide next autopilot action.
 * @returns {{ action:'none'|'adjust'|'guard_stop'|'guard_recover'|'flip', reason:string, params?:object }}
 */
export function decideAutopilot({ analysis, config, lastPrice, autopilot, now = Date.now() }) {
  const ap = autopilot || defaultAutopilot();
  if (!ap.enabled || !config || !analysis) {
    return { action: 'none', reason: 'disabled' };
  }
  if (now - (ap.lastActionAt || 0) < ap.coolDownMs) {
    return { action: 'none', reason: 'cooling' };
  }

  const mode = config.mode || 'neutral';
  const rec = analysis.recommended || 'neutral';
  const strength = Number(analysis.strength) || 0;
  const price = Number(analysis.price ?? lastPrice);
  const reversed =
    (mode === 'long' && rec === 'short') ||
    (mode === 'short' && rec === 'long');
  const mildConflict =
    (mode === 'neutral' && (rec === 'long' || rec === 'short') && strength >= ap.minStrength) ||
    ((mode === 'long' || mode === 'short') && rec === 'neutral' && strength >= ap.minStrength);

  // 1) Hard reverse → stop / recover / flip
  if (reversed && strength >= ap.minStrength) {
    if (ap.allowFlip) {
      const sug = suggestFromTrend(analysis, { mode: rec, riskProfile: ap.riskProfile, gridCount: config.gridCount });
      if (sug) {
        return {
          action: 'flip',
          reason: `趋势反转（${mode}→${rec}，强度${pct(strength)}），自动换向重开`,
          params: {
            ...sug,
            sizeBase: config.sizeBase,
            leverage: config.leverage || sug.leverage,
            outOfRangeAction: config.outOfRangeAction || 'close',
            marketId: config.marketId,
          },
        };
      }
    }
    const stopStyle = (config.outOfRangeAction === 'recover') ? 'guard_recover' : 'guard_stop';
    return {
      action: stopStyle,
      reason: `趋势反转（${mode}↛${rec}，强度${pct(strength)}），自动守卫停机`,
    };
  }

  // 2) Mild conflict: enter or leave a directional grid when signal is strong enough
  if (mildConflict && ap.allowFlip) {
    if (mode === 'neutral' && (rec === 'long' || rec === 'short')) {
      const sug = suggestFromTrend(analysis, { mode: rec, riskProfile: ap.riskProfile, gridCount: config.gridCount });
      if (sug) {
        return {
          action: 'flip',
          reason: `震荡转趋势（→${rec}，强度${pct(strength)}），自动切向`,
          params: {
            ...sug,
            sizeBase: config.sizeBase,
            leverage: config.leverage || sug.leverage,
            outOfRangeAction: config.outOfRangeAction || 'close',
            marketId: config.marketId,
          },
        };
      }
    }
    if ((mode === 'long' || mode === 'short') && rec === 'neutral') {
      const sug = suggestFromTrend(analysis, { mode: 'neutral', riskProfile: ap.riskProfile, gridCount: config.gridCount });
      if (sug) {
        return {
          action: 'flip',
          reason: `趋势转震荡（${mode}→中性，强度${pct(strength)}），自动切回中性网格`,
          params: {
            ...sug,
            sizeBase: config.sizeBase,
            leverage: config.leverage || sug.leverage,
            outOfRangeAction: config.outOfRangeAction || 'close',
            marketId: config.marketId,
          },
        };
      }
    }
  }

  // 3) Range recenter when price near edge or blatantly outside
  if (!(price > 0) || config.lower == null || config.upper == null) {
    return { action: 'none', reason: 'no-price' };
  }
  const lo = Number(config.lower);
  const hi = Number(config.upper);
  const span = hi - lo;
  if (!(span > 0)) return { action: 'none', reason: 'bad-range' };

  const edge = ap.edgePct * span;
  const nearEdge = price <= lo + edge || price >= hi - edge;
  const outside = price < lo || price > hi;
  if (!nearEdge && !outside) return { action: 'none', reason: 'in-band' };

  const sug = suggestFromTrend(analysis, { mode, riskProfile: ap.riskProfile, gridCount: config.gridCount });
  if (!sug) return { action: 'none', reason: 'no-suggest' };

  // Ignore tiny moves (< 20% of current span for both bounds)
  const dLo = Math.abs(sug.lower - lo);
  const dHi = Math.abs(sug.upper - hi);
  if (dLo < span * 0.08 && dHi < span * 0.08 && !outside) {
    return { action: 'none', reason: 'delta-small' };
  }

  return {
    action: 'adjust',
    reason: outside
      ? `价格已出区间（${round2(price)} ∉ [${lo},${hi}]），自动重设`
      : `价格贴近区间边缘，按 ATR 自动重设`,
    params: { lower: sug.lower, upper: sug.upper },
  };
}

function clamp(x, a, b) { return Math.min(b, Math.max(a, x)); }
function round2(x) { return Math.round(Number(x) * 100) / 100; }
function round6(x) { return Math.round(Number(x) * 1e6) / 1e6; }
function pct(s) { return `${Math.round(Number(s) * 100)}%`; }
