// BitgetExchange: LIVE adapter for Bitget USDT-M futures (API v2).
// Zero external deps beyond Node crypto + fetch. Fills detected by polling
// pending orders.
import { EventEmitter } from 'node:events';
import crypto from 'node:crypto';

const PRODUCT = 'USDT-FUTURES';
const GRAN = { 60: '1m', 300: '5m', 900: '15m', 1800: '30m', 3600: '1H', 14400: '4H', 86400: '1D' };

/** Align qty/price to exchange step without float/scientific-notation traps. */
function align(n, step, mode = 'nearest') {
  const x = Number(n);
  const s = Number(step);
  if (!Number.isFinite(x) || !Number.isFinite(s) || s <= 0) return String(n);
  const k = mode === 'down' ? Math.floor(x / s + 1e-12)
    : mode === 'up' ? Math.ceil(x / s - 1e-12)
    : Math.round(x / s);
  const v = k * s;
  // Count decimals from step magnitude (handles 1e-8 etc.)
  let dec = 0;
  const t = s.toFixed(12).replace(/0+$/, '');
  const dot = t.indexOf('.');
  if (dot >= 0) dec = t.length - dot - 1;
  return v.toFixed(Math.min(dec, 12));
}

function stepFromPlace(place, fallback = 0.001) {
  const p = Number(place);
  if (!Number.isFinite(p) || p < 0 || p > 12) return fallback;
  return Number((10 ** -p).toFixed(p));
}

export class BitgetExchange extends EventEmitter {
  constructor(opts = {}) {
    super();
    this.mode = 'live';
    this.apiKey = opts.apiKey;
    this.apiSecret = opts.apiSecret;
    this.passphrase = opts.passphrase;
    this.apiUrl = (opts.apiUrl || 'https://api.bitget.com').replace(/\/$/, '');
    this.productType = opts.productType || PRODUCT;
    this.marginMode = (opts.marginMode || 'crossed').toLowerCase() === 'isolated' ? 'isolated' : 'crossed';
    this.network = 'mainnet';
    // Bitget USDT-M perp base tier: maker 0.02% / taker 0.06%
    this.makerFeeRate = 0.0002;
    this.takerFeeRate = 0.0006;
    this.feeRate = this.makerFeeRate; // grids are post-only/limit → maker
    this.posMode = 'one_way_mode'; // refreshed on init
    this.pollMs = opts.pollMs ?? 2500;
    this._graceMs = this.pollMs * 2;
    this.lastOkAt = 0;
    this.lastError = null;
    this.markets = new Map();
    this.balance = null;
    this.equity = null;
    this.realizedPnl = null;
    this._tracked = new Map();
    this._watch = new Set();
    this._watchTouch = new Map();
    this._pos = new Map();
    this._prices = new Map();
    this._timer = null;
    this._busy = false;
  }

  async init() {
    if (!this.apiKey || !this.apiSecret || !this.passphrase) {
      throw new Error('LIVE 模式需要 BITGET_API_KEY / BITGET_API_SECRET / BITGET_PASSPHRASE（Bitget → API 管理）。');
    }
    const contracts = await this._public('/api/v2/mix/market/contracts', { productType: this.productType });
    const tickers = await this._public('/api/v2/mix/market/tickers', { productType: this.productType }).catch(() => []);
    const tick = new Map();
    for (const t of tickers || []) tick.set(String(t.symbol), Number(t.lastPr || t.markPrice || 0));

    const prefer = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'];
    const rows = (contracts || [])
      .filter((m) => {
        const st = String(m.symbolStatus || m.status || 'normal').toLowerCase();
        return !st || st === 'normal' || st === 'listed';
      })
      .filter((m) => String(m.quoteCoin || '').toUpperCase() === 'USDT' || String(m.symbol || '').endsWith('USDT'))
      .sort((a, b) => {
        const ia = prefer.indexOf(String(a.symbol));
        const ib = prefer.indexOf(String(b.symbol));
        if (ia >= 0 || ib >= 0) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
        return String(a.symbol).localeCompare(String(b.symbol));
      });

    let id = 1;
    for (const m of rows) {
      const sym = String(m.symbol);
      const stepPrice = stepFromPlace(m.pricePlace, 0.1);
      const stepSize = stepFromPlace(m.volumePlace, 0.001);
      const minSz = Number(m.minTradeNum || stepSize);
      const px = tick.get(sym) || 0;
      this.markets.set(id, {
        marketId: id, name: sym, displayName: sym,
        symbol: String(m.baseCoin || sym.replace(/USDT$/i, '')),
        lastPrice: px,
        stepSize, stepPrice,
        maxLeverage: Number(m.maxLever || 50),
        minOrderSize: minSz > 0 ? minSz : stepSize,
      });
      if (px) this._prices.set(id, px);
      id++;
    }
    if (!this.markets.size) throw new Error('Bitget 未返回可交易永续合约。');

    await this._detectPosMode();
    this.dataSource = 'real';
    this.lastOkAt = Date.now();
    await this._refreshAccount();
    this.start();
    return true;
  }

  async _detectPosMode() {
    const m = [...this.markets.values()].find((x) => x.name === 'BTCUSDT') || [...this.markets.values()][0];
    try {
      const acc = await this._priv('GET', '/api/v2/mix/account/account', {
        symbol: m.name, productType: this.productType, marginCoin: 'USDT',
      });
      const pm = String(acc?.posMode || '').toLowerCase();
      if (pm === 'hedge_mode' || pm === 'one_way_mode') this.posMode = pm;
      if (acc?.marginMode) {
        const mm = String(acc.marginMode).toLowerCase();
        if (mm === 'isolated' || mm === 'crossed') this.marginMode = mm;
      }
    } catch {
      this.posMode = 'one_way_mode';
    }
  }

  async reconnect() {
    this.stop();
    this._busy = false;
    this.lastError = null;
    if (!this.markets.size) return this.init();
    await this._detectPosMode().catch(() => {});
    await this._refreshAccount();
    this.lastOkAt = Date.now();
    this.start();
    return true;
  }

  // ---------- HTTP ----------
  _sign(timestamp, method, pathWithQuery, body = '') {
    const prehash = `${timestamp}${method.toUpperCase()}${pathWithQuery}${body}`;
    return crypto.createHmac('sha256', this.apiSecret).update(prehash).digest('base64');
  }

  _authHeaders(method, pathWithQuery, body = '') {
    const timestamp = Date.now().toString();
    return {
      'ACCESS-KEY': this.apiKey,
      'ACCESS-SIGN': this._sign(timestamp, method, pathWithQuery, body),
      'ACCESS-TIMESTAMP': timestamp,
      'ACCESS-PASSPHRASE': this.passphrase,
      'Content-Type': 'application/json',
      locale: 'zh-CN',
    };
  }

  async _public(path, query = {}) {
    const qs = new URLSearchParams(query).toString();
    const full = qs ? `${path}?${qs}` : path;
    const res = await fetch(this.apiUrl + full, { signal: AbortSignal.timeout(15000) });
    const j = await res.json().catch(() => ({}));
    if (j.code !== '00000') throw new Error(`Bitget 公共接口错误 ${j.code || res.status}: ${j.msg || path}`);
    return j.data;
  }

  async _priv(method, path, query = {}, bodyObj) {
    const qs = new URLSearchParams(query).toString();
    const pathWithQuery = qs ? `${path}?${qs}` : path;
    const body = bodyObj !== undefined ? JSON.stringify(bodyObj) : '';
    const res = await fetch(this.apiUrl + pathWithQuery, {
      method,
      headers: this._authHeaders(method, pathWithQuery, body),
      body: body || undefined,
      signal: AbortSignal.timeout(15000),
    });
    let j = null;
    try { j = await res.json(); } catch { /* empty */ }
    if (!j) throw new Error(`Bitget HTTP ${res.status}: ${path}`);
    if (j.code === '40014' || j.code === '40001' || j.code === '40009' || res.status === 401) {
      throw new Error(`Bitget API Key/签名错误 (${j.code || res.status}): ${j.msg || ''}`);
    }
    if (j.code !== '00000') throw new Error(`Bitget 接口错误 ${j.code}: ${j.msg || path}`);
    return j.data;
  }

  // ---------- markets ----------
  async getMarkets() { return [...this.markets.values()]; }

  /** Ensure a USDT-perp is present (refresh contracts if the local map is stale). */
  async ensureSymbol(symbol) {
    const want = String(symbol || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (!want) return null;
    const hit = (list) => list.find((m) => {
      const n = String(m.name || m.displayName || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
      return n === want;
    }) || null;
    let found = hit([...this.markets.values()]);
    if (found) return found;

    const contracts = await this._public('/api/v2/mix/market/contracts', { productType: this.productType });
    const tickers = await this._public('/api/v2/mix/market/tickers', { productType: this.productType }).catch(() => []);
    const tick = new Map();
    for (const t of tickers || []) tick.set(String(t.symbol), Number(t.lastPr || t.markPrice || 0));

    const byName = new Map();
    for (const m of this.markets.values()) byName.set(String(m.name).toUpperCase(), m.marketId);
    let nextId = Math.max(0, ...this.markets.keys(), 0) + 1;

    for (const raw of contracts || []) {
      const st = String(raw.symbolStatus || raw.status || 'normal').toLowerCase();
      if (st && st !== 'normal' && st !== 'listed') continue;
      const sym = String(raw.symbol || '');
      if (!sym.endsWith('USDT') && String(raw.quoteCoin || '').toUpperCase() !== 'USDT') continue;
      if (byName.has(sym.toUpperCase())) continue;
      const stepPrice = stepFromPlace(raw.pricePlace, 0.1);
      const stepSize = stepFromPlace(raw.volumePlace, 0.001);
      const minSz = Number(raw.minTradeNum || stepSize);
      const px = tick.get(sym) || 0;
      const id = nextId++;
      this.markets.set(id, {
        marketId: id, name: sym, displayName: sym,
        symbol: String(raw.baseCoin || sym.replace(/USDT$/i, '')),
        lastPrice: px,
        stepSize, stepPrice,
        maxLeverage: Number(raw.maxLever || 50),
        minOrderSize: minSz > 0 ? minSz : stepSize,
      });
      if (px) this._prices.set(id, px);
      byName.set(sym.toUpperCase(), id);
    }
    return hit([...this.markets.values()]);
  }

  _market(marketId) {
    const m = this.markets.get(Number(marketId));
    if (!m) throw new Error('未知市场 marketId=' + marketId);
    return m;
  }

  async getCandles(marketId, intervalSec = 3600, n = 200) {
    const m = this._market(marketId);
    const g = GRAN[intervalSec] || '1H';
    const data = await this._public('/api/v2/mix/market/candles', {
      symbol: m.name, granularity: g, limit: String(Math.min(n, 200)), productType: this.productType,
    });
    return (data || [])
      .map((d) => ({ time: Number(d[0]), open: +d[1], high: +d[2], low: +d[3], close: +d[4], volume: +d[5] }))
      .filter((c) => Number.isFinite(c.close))
      .sort((a, b) => a.time - b.time);
  }

  async getPrice(marketId, opts = {}) {
    const mId = Number(marketId);
    this._watch.add(mId);
    if (opts.touch !== false) this._watchTouch.set(mId, Date.now());
    const m = this._market(mId);
    try {
      const rows = await this._public('/api/v2/mix/market/ticker', { symbol: m.name, productType: this.productType });
      const t = Array.isArray(rows) ? rows[0] : rows;
      const px = Number(t?.lastPr || t?.markPrice || 0);
      if (px) { this._prices.set(mId, px); return px; }
    } catch { /* fall back */ }
    return this._prices.get(mId) ?? m.lastPrice;
  }

  async setLeverage(marketId, x) {
    const m = this._market(marketId);
    const lev = String(Math.max(1, Math.min(m.maxLeverage || 125, Number(x) || 1)));
    const base = {
      symbol: m.name, productType: this.productType, marginCoin: 'USDT', leverage: lev,
    };
    try {
      if (this.posMode === 'hedge_mode') {
        await this._priv('POST', '/api/v2/mix/account/set-leverage', {}, { ...base, holdSide: 'long' });
        await this._priv('POST', '/api/v2/mix/account/set-leverage', {}, { ...base, holdSide: 'short' });
      } else {
        await this._priv('POST', '/api/v2/mix/account/set-leverage', {}, base);
      }
      return true;
    } catch (e) { this.emit('error', e); return false; }
  }

  /**
   * Bitget hedge_mode uses (side, tradeSide) as position direction + open/close.
   * one_way_mode uses buy/sell + reduceOnly (tradeSide must be omitted).
   */
  _orderDirection(side, reduceOnly) {
    const s = side === 'buy' ? 'buy' : 'sell';
    if (this.posMode === 'hedge_mode') {
      if (reduceOnly) {
        // close long → side buy + close; close short → side sell + close
        return s === 'sell'
          ? { side: 'buy', tradeSide: 'close' }
          : { side: 'sell', tradeSide: 'close' };
      }
      return { side: s, tradeSide: 'open' };
    }
    return { side: s, reduceOnly: reduceOnly ? 'YES' : 'NO' };
  }

  async placeLimitOrder(o) {
    const m = this._market(o.marketId);
    const size = align(o.sizeBase, m.stepSize, 'down');
    const price = align(o.price, m.stepPrice, 'nearest');
    if (!(Number(size) > 0)) throw new Error('数量过小，低于市场最小下单单位。');
    const clientOid = String(o.clientOrderId || `wg${Date.now().toString(36)}${Math.floor(Math.random() * 1e5).toString(36)}`).slice(0, 32);
    const dir = this._orderDirection(o.side, !!o.reduceOnly);
    const payload = {
      symbol: m.name,
      productType: this.productType,
      marginMode: this.marginMode,
      marginCoin: 'USDT',
      size,
      price,
      orderType: 'limit',
      force: 'gtc',
      clientOid,
      ...dir,
    };
    const data = await this._priv('POST', '/api/v2/mix/order/place-order', {}, payload);
    const orderId = String(data?.orderId || data?.order_id || '');
    if (!orderId) throw new Error('Bitget 下单未返回 orderId');
    this._watch.add(m.marketId);
    this._tracked.set(orderId, {
      orderId, marketId: m.marketId, levelIndex: o.levelIndex, side: o.side,
      price: Number(price), sizeBase: Number(size), seen: false,
      clientOid, placedAt: Date.now(), goneAttempts: 0, resolving: false,
    });
    return { orderId };
  }

  async cancelOrder(marketId, orderId) {
    const m = this._market(marketId);
    this._tracked.delete(String(orderId));
    return this._priv('POST', '/api/v2/mix/order/cancel-order', {}, {
      symbol: m.name, productType: this.productType, orderId: String(orderId),
    });
  }

  async cancelAll(marketId) {
    const m = this._market(marketId);
    for (const [id, o] of this._tracked) if (o.marketId === m.marketId) this._tracked.delete(id);
    try {
      return await this._priv('POST', '/api/v2/mix/order/cancel-all-orders', {}, {
        symbol: m.name, productType: this.productType, marginCoin: 'USDT',
      });
    } catch (e) { this.emit('error', e); return false; }
  }

  getOpenOrders(marketId) {
    return [...this._tracked.values()]
      .filter((o) => o.marketId === Number(marketId))
      .map((o) => ({ ...o, orderId: String(o.orderId) }));
  }

  _pendingRows(data) {
    if (Array.isArray(data)) return data;
    if (data && Array.isArray(data.entrustedList)) return data.entrustedList;
    if (data && Array.isArray(data.orderList)) return data.orderList;
    return null;
  }

  async fetchOpenOrders(marketId) {
    const m = this._market(marketId);
    try {
      const data = await this._priv('GET', '/api/v2/mix/order/orders-pending', {
        productType: this.productType, symbol: m.name,
      });
      const rows = this._pendingRows(data);
      if (!rows) return null;
      return rows.map((o) => {
        const apiSide = String(o.side || '').toLowerCase() === 'buy' ? 'buy' : 'sell';
        const ts = String(o.tradeSide || '').toLowerCase();
        // Hedge close: API side=buy means close-long (aggressor sell); side=sell → close-short (buy)
        let side = apiSide;
        if (this.posMode === 'hedge_mode' && ts === 'close') {
          side = apiSide === 'buy' ? 'sell' : 'buy';
        }
        return { orderId: String(o.orderId), price: Number(o.price), side };
      });
    } catch { return null; }
  }

  adoptOrder({ orderId, marketId, levelIndex, side, price, sizeBase }) {
    const mId = Number(marketId);
    const oid = String(orderId);
    this._watch.add(mId);
    this._tracked.set(oid, {
      orderId: oid, marketId: mId, levelIndex, side, price: Number(price), sizeBase: Number(sizeBase),
      seen: false, placedAt: Date.now(), goneAttempts: 0, resolving: false,
    });
  }

  getPosition(marketId) {
    const p = this._pos.get(Number(marketId));
    return p && p.sizeBase !== 0 ? p : null;
  }

  async closePosition(marketId) {
    const m = this._market(marketId);
    const p = this._pos.get(m.marketId);
    if (!p || !p.sizeBase) return true;
    const side = p.sizeBase > 0 ? 'sell' : 'buy';
    const size = align(Math.abs(p.sizeBase), m.stepSize, 'down');
    if (!(Number(size) > 0)) return true;
    const dir = this._orderDirection(side, true);
    await this._priv('POST', '/api/v2/mix/order/place-order', {}, {
      symbol: m.name,
      productType: this.productType,
      marginMode: this.marginMode,
      marginCoin: 'USDT',
      size,
      orderType: 'market',
      force: 'ioc',
      clientOid: `wgc${Date.now().toString(36)}`.slice(0, 32),
      ...dir,
    });
    return true;
  }

  start() { if (!this._timer) { this._timer = setInterval(() => this._poll(), this.pollMs); this._timer.unref?.(); } }
  stop() { if (this._timer) { clearInterval(this._timer); this._timer = null; } }

  async _poll() {
    if (this._busy) {
      if (Date.now() - (this._busySince || 0) < 90_000) return;
      console.log('[Bitget] ⚠ 上一轮轮询卡住超过 90 秒，强制解锁继续轮询。');
    }
    this._busy = true; this._busySince = Date.now();
    try {
      const nowT = Date.now();
      for (const mId of [...this._watch]) {
        const hasOrders = [...this._tracked.values()].some((t) => t.marketId === mId);
        if (!hasOrders && !this._pos.has(mId) && nowT - (this._watchTouch.get(mId) || 0) > 600_000) {
          this._watch.delete(mId); this._watchTouch.delete(mId);
        }
      }
      for (const mId of this._watch) {
        const m = this.markets.get(mId);
        if (!m) continue;
        this.getPrice(mId, { touch: false }).then((px) => {
          if (px) this.emit('price', { marketId: mId, price: px });
        }).catch(() => {});

        let open = null;
        try {
          const data = await this._priv('GET', '/api/v2/mix/order/orders-pending', {
            productType: this.productType, symbol: m.name,
          });
          open = this._pendingRows(data);
        } catch { /* keep */ }
        if (Array.isArray(open)) {
          const liveIds = new Set(open.map((o) => String(o.orderId)));
          for (const o of open) {
            const t = this._tracked.get(String(o.orderId));
            if (t) { t.seen = true; t.goneAttempts = 0; }
          }
          const now = Date.now();
          for (const [id, t] of [...this._tracked]) {
            if (t.marketId !== mId || liveIds.has(id) || t.resolving) continue;
            if (!t.seen && now - (t.placedAt || 0) < this._graceMs) continue;
            t.resolving = true;
            this._resolveGone(id, t).finally(() => { t.resolving = false; });
          }
        }

        await this._refreshPosition(mId, m.name).catch(() => {});
      }
      await this._refreshAccount().catch(() => {});
      this.lastOkAt = Date.now();
    } catch (e) {
      this.lastError = e?.message || String(e);
      this.emit('error', e);
    } finally { this._busy = false; }
  }

  async _refreshPosition(mId, symbol) {
    let rows = null;
    try {
      const ps = await this._priv('GET', '/api/v2/mix/position/single-position', {
        productType: this.productType, symbol, marginCoin: 'USDT',
      });
      rows = Array.isArray(ps) ? ps : (ps ? [ps] : []);
    } catch {
      try {
        const all = await this._priv('GET', '/api/v2/mix/position/all-position', {
          productType: this.productType, marginCoin: 'USDT',
        });
        const list = Array.isArray(all) ? all : [];
        rows = list.filter((p) => String(p.symbol || '').toUpperCase() === String(symbol).toUpperCase());
      } catch { return; }
    }
    let size = 0, entry = 0, upnl = 0;
    for (const p of rows || []) {
      const abs = Math.abs(Number(p.total || p.size || p.available || 0));
      if (!abs) continue;
      const hold = String(p.holdSide || p.posSide || p.side || '').toLowerCase();
      const signed = (hold === 'short' || hold === 'sell') ? -abs : abs;
      if (size === 0 || Math.sign(size) === Math.sign(signed)) {
        const ep = Number(p.openPriceAvg || p.averageOpenPrice || p.openPrice || 0);
        entry = size === 0 ? ep : (Math.abs(size) * entry + abs * ep) / (Math.abs(size) + abs);
        size += signed;
      } else {
        size += signed;
        entry = Number(p.openPriceAvg || p.averageOpenPrice || entry);
      }
      upnl += Number(p.unrealizedPL ?? p.unrealizedPnl ?? 0);
    }
    if (size) this._pos.set(mId, { sizeBase: size, entryPrice: entry, unrealizedPnl: upnl });
    else this._pos.delete(mId);
  }

  async _resolveGone(id, t) {
    let verdict = 'unknown';
    let fillPrice = t.price, fillSize = t.sizeBase;
    try {
      const data = await this._priv('GET', '/api/v2/mix/order/detail', {
        productType: this.productType, orderId: id,
      });
      if (data) {
        const st = String(data.state || data.status || '').toLowerCase();
        const fq = Number(data.baseVolume || data.filledQty || data.accBaseVolume || data.fillsTotal || 0);
        // Bitget: filled / canceled / live / partially_filled
        if (fq > 0 || st === 'filled' || st === 'full_fill') {
          verdict = 'filled';
          if (fq > 0) fillSize = fq;
          const avg = Number(data.priceAvg || data.averagePrice || 0);
          if (avg > 0) fillPrice = avg;
        } else if (/cancel|rejected|expired/.test(st)) {
          verdict = 'cancelled';
        } else if (/live|new|partial|init/.test(st)) {
          t.goneAttempts = 0;
          t.seen = true;
          return;
        }
      }
    } catch { /* keep unknown */ }

    if (verdict === 'unknown') {
      t.goneAttempts = (t.goneAttempts || 0) + 1;
      if (t.goneAttempts < 12) return;
      verdict = 'cancelled';
    }
    this._tracked.delete(id);
    if (verdict === 'filled') {
      this.emit('fill', {
        orderId: id, marketId: t.marketId, side: t.side, price: fillPrice, sizeBase: fillSize, levelIndex: t.levelIndex,
      });
    } else {
      this.emit('error', new Error(`订单 ${id}（${t.side} @ ${t.price}）未确认成交，已停止跟踪（不补单）。`));
    }
  }

  async _refreshAccount() {
    // Prefer aggregate accounts list; fall back to single-account on first market.
    try {
      const data = await this._priv('GET', '/api/v2/mix/account/accounts', { productType: this.productType });
      const rows = Array.isArray(data) ? data : [];
      const usdt = rows.find((a) => String(a.marginCoin || a.coin || '').toUpperCase() === 'USDT') || rows[0];
      if (usdt) {
        const bal = Number(usdt.available ?? usdt.crossedMaxAvailable ?? usdt.availableEquity ?? 0);
        const eq = Number(usdt.accountEquity ?? usdt.usdtEquity ?? usdt.equity ?? bal);
        if (Number.isFinite(bal)) this.balance = bal;
        if (Number.isFinite(eq)) this.equity = eq;
        const rp = usdt.realizedPL != null ? Number(usdt.realizedPL) : NaN;
        if (Number.isFinite(rp)) this.realizedPnl = rp;
        this.lastOkAt = Date.now();
        return;
      }
    } catch { /* fall through */ }
    const m = [...this.markets.values()][0];
    if (!m) return;
    const acc = await this._priv('GET', '/api/v2/mix/account/account', {
      symbol: m.name, productType: this.productType, marginCoin: 'USDT',
    });
    if (acc) {
      this.balance = Number(acc.available ?? acc.crossedMaxAvailable ?? 0);
      this.equity = Number(acc.accountEquity ?? acc.usdtEquity ?? this.balance);
      const pm = String(acc.posMode || '').toLowerCase();
      if (pm === 'hedge_mode' || pm === 'one_way_mode') this.posMode = pm;
      this.lastOkAt = Date.now();
    }
  }
}
