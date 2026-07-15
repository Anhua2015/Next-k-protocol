// Fleet: one shared Bitget adapter + one GridBot per symbol (shared real balance).
import { GridBot } from './bot.js';
import { loadSnapshot, saveSnapshot, deleteSnapshot } from './persist.js';
import { normalizeSymbol } from './config.js';

export function snapKey(symbol) {
  return 'sym_' + normalizeSymbol(symbol);
}

export function marketMatches(m, sym) {
  if (!m) return false;
  const name = normalizeSymbol(m.name || m.displayName || '');
  const disp = normalizeSymbol(m.displayName || '');
  const base = String(m.symbol || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
  return name === sym || disp === sym || (base && (base + 'USDT') === sym) || base === sym;
}

/**
 * @param {object} exchange shared Bitget adapter
 * @param {string[]} initialSymbols
 */
export function createFleet(exchange, initialSymbols = ['BTCUSDT']) {
  const bots = new Map();

  function ensure(symbol) {
    const sym = normalizeSymbol(symbol);
    if (!sym) throw new Error('标的无效');
    if (bots.has(sym)) return bots.get(sym);
    // bindError:false — shared adapter; server owns the process-level error listener
    const bot = new GridBot(exchange, {
      onChange: (s) => saveSnapshot(snapKey(sym), s),
      bindError: false,
    });
    const snap = loadSnapshot(snapKey(sym));
    if (snap) bot.restore(snap);
    bots.set(sym, bot);
    return bot;
  }

  for (const s of initialSymbols) {
    try { ensure(s); } catch { /* skip */ }
  }
  if (!bots.size) ensure('BTCUSDT');

  return {
    exchange,
    list() { return [...bots.keys()]; },
    get(symbol) {
      const sym = normalizeSymbol(symbol);
      return bots.get(sym) || null;
    },
    ensure,
    add(symbol) {
      const sym = normalizeSymbol(symbol);
      if (bots.has(sym)) return { ok: true, created: false, symbol: sym };
      ensure(sym);
      return { ok: true, created: true, symbol: sym };
    },
    async remove(symbol) {
      const sym = normalizeSymbol(symbol);
      const bot = bots.get(sym);
      if (!bot) return { ok: false, error: '标的不存在' };
      if (bot.running) return { ok: false, error: '请先停止该标的网格再删除' };
      try { bot.dispose?.(); } catch {}
      bots.delete(sym);
      try { deleteSnapshot(snapKey(sym)); } catch {}
      return { ok: true, symbol: sym };
    },
    async resolveMarket(symbol) {
      const sym = normalizeSymbol(symbol);
      const markets = await exchange.getMarkets();
      return markets.find((x) => marketMatches(x, sym)) || null;
    },
    marketMatches,
  };
}
