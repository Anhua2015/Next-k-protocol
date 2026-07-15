// PaperExchange: simulated trading on Bitget USDT-M public prices.
import { EventEmitter } from 'node:events';

const PRODUCT = 'USDT-FUTURES';
const FALLBACK_MARKETS = [
  { marketId: 1, displayName: 'BTCUSDT', symbol: 'BTC', name: 'BTCUSDT', lastPrice: 61000, stepSize: 0.001, stepPrice: 0.1, maxLeverage: 50, minOrderSize: 0.001 },
  { marketId: 2, displayName: 'ETHUSDT', symbol: 'ETH', name: 'ETHUSDT', lastPrice: 3000, stepSize: 0.01, stepPrice: 0.01, maxLeverage: 50, minOrderSize: 0.01 },
];
const GRAN = { 60: '1m', 300: '5m', 900: '15m', 1800: '30m', 3600: '1H', 14400: '4H', 86400: '1D' };

export class PaperExchange extends EventEmitter {
  constructor(opts = {}) {
    super();
    this.mode = 'paper';
    this.balance = opts.startBalance ?? 10000;
    this.productType = opts.productType || PRODUCT;
    this.candidates = [...new Set((opts.apiUrl ? [opts.apiUrl] : []).concat([
      'https://api.bitget.com',
      'https://api.bitget.site',
    ]))];
    this.apiUrl = this.candidates[0];
    this.dataSource = 'connecting';
    this.network = 'mainnet';
    this.tickMs = opts.tickMs ?? 1000;
    this.pollMs = opts.pollMs ?? 5000;
    this.volPerTick = opts.volPerTick ?? 0.0015;
    this.feeRate = Number(opts.feeRate) || 0.0006;
    this.markets = new Map();
    this.orders = new Map();
    this.positions = new Map();
    this.realizedPnl = 0;
    this.lastOkAt = Date.now();
    this.lastError = null;
    this.prices = new Map();
    this.realTarget = new Map();
    this._seq = 1;
    this._tickTimer = null;
    this._pollTimer = null;
  }

  /** Shared-account equity = cash + all legs' unrealized. */
  get equity() {
    let upnl = 0;
    for (const [id, p] of this.positions) {
      if (!p || !p.sizeBase) continue;
      const last = this.prices.get(id);
      if (!Number.isFinite(last)) continue;
      upnl += p.sizeBase * (last - p.entryPrice);
    }
    return this.balance + upnl;
  }

  async init() {
    let chosen = null;
    for (const url of this.candidates) {
      const list = await this._fetchMarkets(url);
      if (list && list.length) { chosen = url; this._setMarkets(list); break; }
    }
    if (chosen) {
      this.apiUrl = chosen;
      this.dataSource = 'real';
    } else {
      this.dataSource = 'synthetic';
      this._setMarkets(FALLBACK_MARKETS.map((m) => ({ ...m })));
    }
    for (const [id, m] of this.markets) {
      this.prices.set(id, m.lastPrice || 100);
      this.realTarget.set(id, m.lastPrice || 100);
    }
    this._startLoops();
    return true;
  }

  async reconnect() {
    try {
      for (const url of this.candidates) {
        const list = await this._fetchMarkets(url);
        if (list && list.length) {
          this.apiUrl = url;
          if (this.dataSource !== 'real') {
            this.dataSource = 'real';
            this._setMarkets(list, { preserveIds: true });
            for (const [id, m] of this.markets) {
              if (!this.prices.has(id)) this.prices.set(id, m.lastPrice || 100);
              this.realTarget.set(id, m.lastPrice || this.prices.get(id) || 100);
            }
          }
          break;
        }
      }
    } catch { /* keep */ }
    this._startLoops();
    this.lastOkAt = Date.now();
    return true;
  }

  async _fetchMarkets(url) {
    try {
      const [cRes, tRes] = await Promise.all([
        fetch(`${url}/api/v2/mix/market/contracts?productType=${this.productType}`, { signal: AbortSignal.timeout(8000) }),
        fetch(`${url}/api/v2/mix/market/tickers?productType=${this.productType}`, { signal: AbortSignal.timeout(8000) }),
      ]);
      if (!cRes.ok || !tRes.ok) return null;
      const cj = await cRes.json();
      const tj = await tRes.json();
      if (cj.code !== '00000' || tj.code !== '00000') return null;
      const tick = new Map();
      for (const t of tj.data || []) tick.set(String(t.symbol), Number(t.lastPr || t.markPrice || 0));
      const out = [];
      let id = 1;
      const rows = (cj.data || [])
        .filter((m) => {
          const st = String(m.symbolStatus || m.status || 'normal').toLowerCase();
          return !st || st === 'normal' || st === 'listed';
        })
        .filter((m) => String(m.quoteCoin || '').toUpperCase() === 'USDT' || String(m.symbol || '').endsWith('USDT'));
      const prefer = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'];
      rows.sort((a, b) => {
        const ia = prefer.indexOf(String(a.symbol));
        const ib = prefer.indexOf(String(b.symbol));
        if (ia >= 0 || ib >= 0) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
        return String(a.symbol).localeCompare(String(b.symbol));
      });
      const stepFromPlace = (place, fb) => {
        const p = Number(place);
        if (!Number.isFinite(p) || p < 0 || p > 12) return fb;
        return Number((10 ** -p).toFixed(p));
      };
      for (const m of rows) {
        const sym = String(m.symbol);
        const price = tick.get(sym) || Number(m.lastPr || 0) || 0;
        const stepPrice = stepFromPlace(m.pricePlace, 0.1);
        const stepSize = stepFromPlace(m.volumePlace, 0.001);
        const minSz = Number(m.minTradeNum || m.minOrderQty || stepSize);
        out.push({
          marketId: id++,
          name: sym,
          displayName: sym,
          symbol: String(m.baseCoin || sym.replace(/USDT$/i, '')),
          lastPrice: price || 100,
          stepSize, stepPrice,
          maxLeverage: Number(m.maxLever || m.maxLeverage || 50),
          minOrderSize: minSz > 0 ? minSz : stepSize,
        });
      }
      return out.length ? out : null;
    } catch { return null; }
  }

  _setMarkets(list, { preserveIds = false } = {}) {
    if (!preserveIds || !this.markets.size) {
      this.markets.clear();
      for (const m of list) this.markets.set(m.marketId, m);
      return;
    }
    // Keep existing marketId per symbol so running bots / orders stay valid.
    const byName = new Map();
    for (const m of this.markets.values()) {
      const n = String(m.name || m.displayName || '').toUpperCase();
      if (n) byName.set(n, m.marketId);
    }
    let nextId = Math.max(0, ...this.markets.keys(), 0) + 1;
    const next = new Map();
    for (const m of list) {
      const n = String(m.name || m.displayName || '').toUpperCase();
      const id = byName.has(n) ? byName.get(n) : nextId++;
      next.set(id, { ...m, marketId: id });
    }
    this.markets = next;
  }

  async getMarkets() { return [...this.markets.values()]; }

  /**
   * Look up / inject a single USDT-perp by symbol (e.g. HYPEUSDT).
   * Refreshes the contract list if missing from the local cache.
   */
  async ensureSymbol(symbol) {
    const want = String(symbol || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (!want) return null;
    const hit = (list) => list.find((m) => {
      const n = String(m.name || m.displayName || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
      return n === want;
    }) || null;
    let found = hit([...this.markets.values()]);
    if (found) return found;
    if (this.dataSource === 'synthetic') return null;
    const list = await this._fetchMarkets(this.apiUrl);
    if (!list?.length) return null;
    this._setMarkets(list, { preserveIds: true });
    for (const [id, m] of this.markets) {
      if (!this.prices.has(id)) this.prices.set(id, m.lastPrice || 100);
      if (!this.realTarget.has(id)) this.realTarget.set(id, m.lastPrice || this.prices.get(id) || 100);
    }
    this.dataSource = 'real';
    return hit([...this.markets.values()]);
  }

  async getCandles(marketId, intervalSec = 3600, n = 200) {
    const m = this.markets.get(Number(marketId));
    if (this.dataSource === 'real' && m) {
      try {
        const g = GRAN[intervalSec] || '1H';
        const url = `${this.apiUrl}/api/v2/mix/market/candles?symbol=${encodeURIComponent(m.name)}&granularity=${g}&limit=${Math.min(n, 200)}&productType=${this.productType}`;
        const res = await fetch(url, { signal: AbortSignal.timeout(8000) });
        if (res.ok) {
          const j = await res.json();
          if (j.code === '00000' && Array.isArray(j.data)) {
            const data = j.data.map((d) => ({
              time: Number(d[0]), open: +d[1], high: +d[2], low: +d[3], close: +d[4], volume: +d[5],
            })).filter((c) => Number.isFinite(c.close)).sort((a, b) => a.time - b.time);
            if (data.length >= 20) return data;
          }
        }
      } catch { /* fall through */ }
    }
    return synthCandles(this.prices.get(Number(marketId)) || 100, n);
  }

  async getPrice(marketId) { return this.prices.get(Number(marketId)); }
  async setLeverage() { return true; }

  async placeLimitOrder(o) {
    const id = `paper-${this._seq++}`;
    this.orders.set(id, { orderId: id, ...o });
    return { orderId: id };
  }
  async cancelOrder(_m, orderId) { this.orders.delete(orderId); return true; }
  async cancelAll(marketId) {
    for (const [id, o] of this.orders) if (Number(o.marketId) === Number(marketId)) this.orders.delete(id);
    return true;
  }
  getOpenOrders(marketId) { return [...this.orders.values()].filter((o) => Number(o.marketId) === Number(marketId)); }
  async fetchOpenOrders(marketId) {
    return [...this.orders.values()]
      .filter((o) => Number(o.marketId) === Number(marketId))
      .map((o) => ({ orderId: String(o.orderId), price: Number(o.price), side: o.side }));
  }

  adoptOrder({ orderId, marketId, levelIndex, side, price, sizeBase }) {
    this.orders.set(String(orderId), {
      orderId: String(orderId), marketId: Number(marketId), levelIndex, side,
      price: Number(price), sizeBase: Number(sizeBase), reduceOnly: false,
    });
  }

  getPosition(marketId) {
    const p = this.positions.get(Number(marketId));
    if (!p || p.sizeBase === 0) return null;
    const last = this.prices.get(Number(marketId));
    return { sizeBase: p.sizeBase, entryPrice: p.entryPrice, unrealizedPnl: p.sizeBase * (last - p.entryPrice) };
  }

  async closePosition(marketId) {
    const id = Number(marketId);
    const p = this.positions.get(id);
    if (!p || !p.sizeBase) return null;
    const price = this.prices.get(id);
    this._applyFill(id, p.sizeBase > 0 ? 'sell' : 'buy', price, Math.abs(p.sizeBase));
    return true;
  }

  start() { this._startLoops(); }
  stop() { /* keep price feed */ }

  _startLoops() {
    if (!this._tickTimer) { this._tickTimer = setInterval(() => this._tick(), this.tickMs); this._tickTimer.unref?.(); }
    if (this.dataSource === 'real' && !this._pollTimer) {
      this._pollTimer = setInterval(() => this._pollReal(), this.pollMs); this._pollTimer.unref?.();
    }
  }

  async _pollReal() {
    try {
      const res = await fetch(`${this.apiUrl}/api/v2/mix/market/tickers?productType=${this.productType}`, { signal: AbortSignal.timeout(6000) });
      if (!res.ok) return;
      const j = await res.json();
      if (j.code !== '00000') return;
      const byName = new Map([...this.markets.values()].map((m) => [m.name, m.marketId]));
      for (const t of j.data || []) {
        const id = byName.get(String(t.symbol));
        const price = Number(t.lastPr || t.markPrice || 0);
        if (id != null && price) this.realTarget.set(id, price);
      }
    } catch { /* transient */ }
  }

  _tick() {
    this.lastOkAt = Date.now();
    for (const [id, price] of this.prices) {
      let next;
      if (this.dataSource === 'real') {
        const target = this.realTarget.get(id) ?? price;
        next = price + (target - price) * 0.25;
        if (Math.abs(next - target) / target < 1e-5) next = target;
      } else {
        const seed = this.markets.get(id)?.lastPrice || price;
        const drift = (seed - price) / seed * 0.02;
        const shock = (Math.random() * 2 - 1) * this.volPerTick;
        next = Math.max(0.0001, price * (1 + drift + shock));
      }
      this.prices.set(id, next);
      this.emit('price', { marketId: id, price: next });
      this._matchFills(id, price, next);
    }
  }

  _matchFills(marketId, prev, cur) {
    for (const o of [...this.orders.values()]) {
      if (Number(o.marketId) !== Number(marketId)) continue;
      const crossedBuy = o.side === 'buy' && cur <= o.price;
      const crossedSell = o.side === 'sell' && cur >= o.price;
      if (!crossedBuy && !crossedSell) continue;
      if (o.reduceOnly && !this._reduces(marketId, o.side)) { this.orders.delete(o.orderId); continue; }
      this.orders.delete(o.orderId);
      this._applyFill(marketId, o.side, o.price, o.sizeBase);
      this.emit('fill', {
        orderId: o.orderId, marketId, side: o.side, price: o.price, sizeBase: o.sizeBase,
        levelIndex: o.levelIndex, clientOrderId: o.clientOrderId,
      });
    }
  }

  _reduces(marketId, side) {
    const p = this.positions.get(Number(marketId));
    if (!p || p.sizeBase === 0) return false;
    return side === 'sell' ? p.sizeBase > 0 : p.sizeBase < 0;
  }

  _applyFill(marketId, side, price, qty) {
    const fee = price * qty * this.feeRate;
    this.balance -= fee;
    this.realizedPnl -= fee;
    const p = this.positions.get(marketId) || { sizeBase: 0, entryPrice: 0 };
    const signed = side === 'buy' ? qty : -qty;
    if (p.sizeBase === 0 || Math.sign(p.sizeBase) === Math.sign(signed)) {
      const newSize = p.sizeBase + signed;
      p.entryPrice = (Math.abs(p.sizeBase) * p.entryPrice + Math.abs(signed) * price) / Math.abs(newSize);
      p.sizeBase = newSize;
    } else {
      const closeQty = Math.min(Math.abs(p.sizeBase), Math.abs(signed));
      const pnl = p.sizeBase > 0 ? closeQty * (price - p.entryPrice) : closeQty * (p.entryPrice - price);
      this.realizedPnl += pnl; this.balance += pnl;
      const remaining = p.sizeBase + signed;
      if (Math.sign(remaining) === Math.sign(p.sizeBase) || remaining === 0) {
        p.sizeBase = remaining; if (remaining === 0) p.entryPrice = 0;
      } else { p.sizeBase = remaining; p.entryPrice = price; }
    }
    this.positions.set(marketId, p);
  }
}

function synthCandles(start, n) {
  const out = []; let price = start; let t = Date.now() - n * 3600_000;
  const regime = Math.random() < 0.34 ? 0.0012 : Math.random() < 0.5 ? -0.0012 : 0;
  for (let i = 0; i < n; i++) {
    const open = price, close = price * (1 + regime + (Math.random() * 2 - 1) * 0.006);
    out.push({ time: t, open, high: Math.max(open, close) * 1.001, low: Math.min(open, close) * 0.999, close, volume: 100 });
    price = close; t += 3600_000;
  }
  return out;
}
