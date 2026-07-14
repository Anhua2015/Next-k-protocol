/**
 * Agent 闭环：周期性用 4H+funding 重判 BTC/ETH/SOL，模式变化则撤单/重挂（flat=空仓）。
 * 对齐 bitget-fleet-grid-agent，执行层为本仓库实盘 fleet。
 */
import { analyzeAgentTrend, agentTrendEnabled } from './agent-trend.js';
import { CANDIDATE_NAMES, FLEET_DEFAULTS } from './fleet-plan.js';
import { restartFleet } from './fleet-plan.js';
import { pauseFleet, isFleetPaused } from './fleet-control.js';

let lastDecisions = [];
let lastFingerprint = '';
let timer = null;

export function getLastAgentDecisions() {
  return lastDecisions;
}

export async function decideAll(exchange) {
  const out = [];
  for (const name of CANDIDATE_NAMES) {
    const mid = exchange.marketIdForName(name);
    if (!mid) continue;
    let funding = 0;
    try {
      funding = await exchange.getFundingRate?.(mid) ?? 0;
    } catch { /* optional */ }
    const candles = await exchange.getCandles(mid, 14400, 120);
    const analysis = analyzeAgentTrend(candles, funding);
    out.push({
      name,
      marketId: mid,
      ...analysis,
      decidedAt: new Date().toISOString(),
    });
  }
  lastDecisions = out;
  return out;
}

function fingerprint(decisions) {
  return decisions.map((d) => `${d.name}:${d.gridMode}`).sort().join('|');
}

/**
 * @returns {{ changed: boolean, decisions: Array, action?: string }}
 */
export async function syncAgentModes(fleet, exchange, { forceRestart = false } = {}) {
  if (!agentTrendEnabled()) {
    return { changed: false, decisions: [], skipped: true };
  }
  FLEET_DEFAULTS.TREND_LINKED_MODE = true;

  const decisions = await decideAll(exchange);
  const fp = fingerprint(decisions);
  const allFlat = decisions.length > 0 && decisions.every((d) => d.gridMode === 'flat');

  if (!forceRestart && fp === lastFingerprint && !allFlat) {
    return { changed: false, decisions, action: 'noop' };
  }

  const prev = lastFingerprint;
  lastFingerprint = fp;

  if (allFlat) {
    if (!isFleetPaused() && fleet.getState().botCount > 0) {
      await pauseFleet(fleet);
      console.log('[Agent] 全标的 UNCLEAR/flat → 暂停网格空仓');
      return { changed: true, decisions, action: 'pause_flat', prev };
    }
    return { changed: fp !== prev, decisions, action: 'already_flat' };
  }

  // 有可交易模式 → 按新 mode 重配三标
  console.log('[Agent] 模式变更 → restartFleet', fp);
  const { preview, started } = await restartFleet(fleet, exchange, { closeFirst: false });
  return {
    changed: true,
    decisions,
    action: 'restart',
    prev,
    preview: {
      plans: preview.plans?.map((p) => ({ name: p.name, mode: p.mode })) || [],
    },
    started,
  };
}

export function startAgentLoop(fleet, exchange) {
  if (!agentTrendEnabled()) {
    console.log('[Agent] GRID_AGENT_TREND=0，不启动趋势闭环');
    return;
  }
  FLEET_DEFAULTS.TREND_LINKED_MODE = true;
  const ms = Number(process.env.GRID_AGENT_INTERVAL_MS || 4 * 60 * 60 * 1000);

  const tick = async () => {
    try {
      const r = await syncAgentModes(fleet, exchange);
      if (r.changed) console.log('[Agent] sync', r.action, fingerprint(r.decisions));
    } catch (e) {
      console.warn('[Agent] loop error:', e.message);
    }
  };

  // 启动后稍等再首次决策（等行情/账户就绪）
  setTimeout(() => tick(), 15_000);
  timer = setInterval(tick, ms);
  timer.unref?.();
  console.log(`[Agent] 趋势闭环已启动，间隔 ${Math.round(ms / 60000)} 分钟（4H ADX/RSI/funding）`);
}

export function stopAgentLoop() {
  if (timer) clearInterval(timer);
  timer = null;
}
