// Trend detection — default uses hackathon Agent rules (ADX/RSI/funding → modes incl. flat).
import { analyzeAgentTrend, agentTrendEnabled } from './agent-trend.js';
import { ema, atr, normalizedSlope } from './indicators.js';

/**
 * Analyse candles and recommend a grid strategy.
 * When GRID_AGENT_TREND=1 (default): Agent ADX/RSI/funding rules.
 * When off: legacy EMA/slope only (no flat).
 */
export function analyzeTrend(candles, fundingOrOpts = 0, opts = {}) {
  if (agentTrendEnabled()) {
    const funding = typeof fundingOrOpts === 'number' ? fundingOrOpts : (fundingOrOpts?.funding8h ?? 0);
    return analyzeAgentTrend(candles, funding, opts.cfg);
  }
  return analyzeTrendLegacy(candles, typeof fundingOrOpts === 'object' ? fundingOrOpts : opts);
}

function analyzeTrendLegacy(candles, opts = {}) {
  const fast = opts.fast ?? 20;
  const slow = opts.slow ?? 50;
  const slopeBars = opts.slopeBars ?? 20;
  const slopeThreshold = opts.slopeThreshold ?? 0.0015;

  const closes = candles.map((c) => c.close).filter((v) => Number.isFinite(v));
  const price = closes[closes.length - 1];

  if (closes.length < slow + 1) {
    return {
      trend: 'range', recommended: 'neutral', strength: 0,
      atrPct: null, price,
      detail: `K线样本不足（需要至少 ${slow + 1} 根，当前 ${closes.length} 根），默认中性网格。`,
    };
  }

  const emaFast = ema(closes, fast);
  const emaSlow = ema(closes, slow);
  const slope = normalizedSlope(closes, slopeBars);
  const a = atr(candles, 14);
  const atrPct = a && price ? (a / price) * 100 : null;

  const up = emaFast > emaSlow && slope > slopeThreshold;
  const down = emaFast < emaSlow && slope < -slopeThreshold;

  const strength = Math.min(
    1,
    (Math.abs(slope) / (slopeThreshold * 4)) * 0.6 +
      (Math.abs(emaFast - emaSlow) / emaSlow / 0.02) * 0.4
  );

  let trend, recommended, detail;
  if (up) {
    trend = 'up'; recommended = 'long';
    detail = 'EMA 多头 + 斜率为正，倾向做多网格。';
  } else if (down) {
    trend = 'down'; recommended = 'short';
    detail = 'EMA 空头 + 斜率为负，倾向做空网格。';
  } else {
    trend = 'range'; recommended = 'neutral';
    detail = '震荡市，中性网格。';
  }

  return { trend, recommended, strength, atrPct, price, detail };
}
