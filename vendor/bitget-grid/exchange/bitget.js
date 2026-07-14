/**
 * Bitget USDT-M adapter — same IExchange surface as ExtendedExchange.
 * Strategy / fleet / bot unchanged; only venue wiring.
 *
 * Hard requirements for parity:
 * - one_way_mode (enforced on init)
 * - post_only limit grids + reduceOnly with clientOid tracking
 * - fill detection without requiring "seen on book" (instant fills)
 * - precise price/size steps from contract meta
 */
import { EventEmitter } from 'node:events';
import crypto from 'node:crypto';
import { httpJson } from './http.js';

const PRODUCT = 'USDT-FUTURES';
const MARGIN = 'USDT';
const USER_AGENT = 'NextK-BitgetGrid/1.0';

export const BITGET_FLEET_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT'];

/** Upstream fleet uses Extended market names; Bitget API uses USDT symbols. */
export const EXTENDED_NAME_TO_SYMBOL = {
  'BTC-USD': 'BTCUSDT',
  'ETH-USD': 'ETHUSDT',
  'SOL-USD': 'SOLUSDT',
};
export const SYMBOL_TO_EXTENDED_NAME = {
  BTCUSDT: 'BTC-USD',
  ETHUSDT: 'ETH-USD',
  SOLUSDT: 'SOL-USD',
};

export function alignToStep(value, step, mode = 'nearest') {
  const s = Number(step) || 0.01;
  const v = Number(value);
  if (!(s > 0) || !Number.isFinite(v)) return String(value);
  const n = v / s;
  const snapped = mode === 'down' ? Math.floor(n) : mode === 'up' ? Math.ceil(n) : Math.round(n);
  const out = snapped * s;
  const stepStr = String(s);
  const decimals = stepStr.includes('e') || stepStr.includes('E')
    ? Math.min(8, Math.max(0, -Math.floor(Math.log10(s))))
    : Math.min(8, (stepStr.split('.')[1] || '').length);
  return out.toFixed(decimals);
}

function stepDecimals(step) {
  const s = Number(step) || 0.01;
  const stepStr = String(s);
  if (stepStr.includes('e') || stepStr.includes('E')) {
    return Math.min(8, Math.max(0, -Math.floor(Math.log10(s))));
  }
  return Math.min(8, (stepStr.split('.')[1] || '').length);
}

export class BitgetExchange extends EventEmitter {
  constructor(opts = {}) {
    super();
    this.mode = 'live';
    this.network = 'mainnet';
    this.dataSource = 'real';
    this.apiKey = opts.apiKey || '';
    this.apiSecret = opts.apiSecret || '';
    this.passphrase = opts.passphrase || '';
    this.apiUrl = (opts.apiUrl || 'https://api.bitget.com').replace(/\/$/, '');
    this.pollMs = opts.pollMs ?? 2000;
    this.marginMode = (opts.marginMode || 'crossed').toLowerCase() === 'isolated' ? 'isolated' : 'crossed';

    this.markets = new Map();
    this.balance = null;
    this.equity = null;
    this.unrealisedPnl = null;
    this.realizedPnl = null;
    this.availableForTrade = null;
    this.availableForWithdrawal = null;
    this.initialMargin = null;
    this.spotEquity = null;
    this._balanceRefreshError = null;
    this._balanceRefreshAt = null;
    this._statsCache = null;
    this.pnlSinceDate = null;
    this.statsMarketNames = Object.keys(EXTENDED_NAME_TO_SYMBOL);
    this.posMode = null;

    /** @type {Map<string, object>} primary key = trackKey (orderId or clientOid) */
    this._tracked = new Map();
    this._watch = new Set();
    this._pos = new Map();
    this._allPositions = [];
    this._allOpenOrders = [];
    this._prices = new Map();
    this._timer = null;
    this._busy = false;
    this._fleetRunningIds = [];
    this._emittedFills = new Set();
    this._fillCursor = {};
  }

  async init() {
    if (!this.apiKey || !this.apiSecret || !this.passphrase) {
      throw new Error('需要 BITGET_API_KEY / BITGET_API_SECRET / BITGET_PASSPHRASE');
    }
    await this._ensureOneWayMode();
    await this._loadMarkets();
    // seed last prices for fleet symbols
    for (const m of this.markets.values()) {
      if (!BITGET_FLEET_SYMBOLS.includes(m.symbol)) continue;
      try {
        await this.getPrice(m.marketId);
      } catch { /* ok */ }
    }
    await this._refreshAccount();
    if (!(this.equity > 0) && !(this.balance > 0)) {
      console.warn('[Bitget] 账户权益/可用为 0，请确认 API 权限与合约账户有资金');
    }
    await this._refreshAllPositions().catch(() => {});
    await this._refreshAllOpenOrders().catch(() => {});
    this.start();
    return true;
  }

  async _ensureOneWayMode() {
    try {
      await this._post('/api/v2/mix/account/set-position-mode', {
        productType: PRODUCT,
        posMode: 'one_way_mode',
      });
      this.posMode = 'one_way_mode';
    } catch (e) {
      const msg = String(e.message || e);
      if (/no change|not modified|same|already|identical/i.test(msg)) {
        this.posMode = 'one_way_mode';
        return;
      }
      // 读取当前模式（若接口可用）
      try {
        const acc = await this._get('/api/v2/mix/account/account', {
          symbol: 'BTCUSDT',
          productType: PRODUCT,
          marginCoin: MARGIN,
        });
        const mode = String(acc?.posMode || acc?.holdMode || '').toLowerCase();
        if (mode.includes('one') || mode.includes('single') || mode === 'one_way_mode') {
          this.posMode = 'one_way_mode';
          return;
        }
        if (mode.includes('hedge') || mode.includes('double')) {
          throw new Error(`账户为双向持仓(${mode})，网格必须单向。请手动改为 one_way_mode 后再启动。`);
        }
      } catch (e2) {
        if (/双向|hedge|one_way/i.test(String(e2.message))) throw e2;
      }
      throw new Error(
        `无法确认/切换 Bitget one_way_mode（网格需要单向持仓）。原始错误: ${msg}`
      );
    }
  }

  async _loadMarkets() {
    const rows = await this._publicGet('/api/v2/mix/market/contracts', { productType: PRODUCT });
    const list = Array.isArray(rows) ? rows : [];
    const wantedApi = BITGET_FLEET_SYMBOLS;
    let id = 1;
    const bySym = new Map(list.map((m) => [String(m.symbol || '').toUpperCase(), m]));
    const ordered = [
      ...wantedApi.map((s) => bySym.get(s)).filter(Boolean),
      ...list.filter((m) => !wantedApi.includes(String(m.symbol || '').toUpperCase())).slice(0, 20),
    ];
    for (const m of ordered) {
      const symbol = String(m.symbol || '').toUpperCase();
      if (!symbol || this.marketIdForName(symbol) || this.marketIdForName(SYMBOL_TO_EXTENDED_NAME[symbol] || '')) continue;
      const pricePlace = Number(m.pricePlace ?? 2);
      const stepPrice = Number.isFinite(pricePlace) && pricePlace >= 0
        ? Math.pow(10, -pricePlace)
        : 0.01;
      const stepSize = Number(m.sizeMultiplier || m.minTradeNum || 0.001);
      // Keep upstream fleet names (BTC-USD) so fleet-plan/scanner need zero edits.
      const extendedName = SYMBOL_TO_EXTENDED_NAME[symbol] || symbol;
      this.markets.set(id, {
        marketId: id,
        name: extendedName,
        displayName: extendedName,
        symbol,
        lastPrice: Number(m.lastPr || m.markPrice || 0),
        stepSize,
        stepPrice,
        maxLeverage: Number(m.maxLever || m.maxLeverage || 50),
        minOrderSize: Number(m.minTradeNum || stepSize),
        qtyStep: String(stepSize),
        priceStep: stepPrice.toFixed(stepDecimals(stepPrice)),
        pricePlace,
        volumePlace: Number(m.volumePlace ?? stepDecimals(stepSize)),
      });
      id++;
    }
    for (const [extName, apiSym] of Object.entries(EXTENDED_NAME_TO_SYMBOL)) {
      if (!this.marketIdForName(extName)) {
        throw new Error(`Bitget 合约列表缺少舰队标的 ${apiSym}（映射 ${extName}）`);
      }
    }
  }

  // ---------- HTTP ----------
  _sign(ts, method, requestPath, body = '') {
    const prehash = `${ts}${method.toUpperCase()}${requestPath}${body}`;
    return crypto.createHmac('sha256', this.apiSecret).update(prehash).digest('base64');
  }

  _authHeaders(ts, sign) {
    return {
      'Content-Type': 'application/json',
      'User-Agent': USER_AGENT,
      'ACCESS-KEY': this.apiKey,
      'ACCESS-SIGN': sign,
      'ACCESS-TIMESTAMP': ts,
      'ACCESS-PASSPHRASE': this.passphrase,
      locale: 'en-US',
    };
  }

  async _publicGet(path, params = {}) {
    const qs = new URLSearchParams(params).toString();
    const url = `${this.apiUrl}${path}${qs ? `?${qs}` : ''}`;
    const { json: j } = await httpJson('GET', url, {
      headers: { 'User-Agent': USER_AGENT, Accept: 'application/json' },
    });
    if (String(j?.code) !== '00000') throw new Error(`Bitget ${path}: ${j?.code} ${j?.msg}`);
    return j.data;
  }

  async _req(method, path, bodyObj, params) {
    let requestPath = path;
    let body = '';
    if (method === 'GET' && params && Object.keys(params).length) {
      const qs = new URLSearchParams(params).toString();
      requestPath = `${path}?${qs}`;
    } else if (method !== 'GET') {
      body = JSON.stringify(bodyObj || {});
    }
    const ts = String(Date.now());
    const sign = this._sign(ts, method, requestPath, body);
    const url = `${this.apiUrl}${requestPath}`;
    const headers = this._authHeaders(ts, sign);
    const { json: j, status } = await httpJson(method, url, {
      headers,
      bodyRaw: method === 'GET' ? null : body,
    });
    if (String(j?.code) !== '00000') {
      throw new Error(`Bitget ${path}: ${j?.code || status} ${j?.msg || JSON.stringify(j)}`);
    }
    return j.data;
  }

  _get(path, params) {
    return this._req('GET', path, null, params);
  }

  _post(path, body) {
    return this._req('POST', path, body);
  }

  marketIdForName(name) {
    const s = String(name || '');
    const extended = EXTENDED_NAME_TO_SYMBOL[s]
      ? s
      : (SYMBOL_TO_EXTENDED_NAME[s.toUpperCase()] || s);
    const api = EXTENDED_NAME_TO_SYMBOL[extended] || s.toUpperCase();
    for (const [id, m] of this.markets) {
      if (m.name === extended || m.displayName === extended || m.symbol === api || m.symbol === s.toUpperCase() || m.name === s) {
        return id;
      }
    }
    return null;
  }

  _apiSymbol(marketOrName) {
    if (marketOrName && typeof marketOrName === 'object' && marketOrName.symbol) return marketOrName.symbol;
    const id = this.marketIdForName(marketOrName);
    return id ? this.markets.get(id).symbol : String(marketOrName || '').toUpperCase();
  }

  _trackKey(t) {
    return String(t.orderId || t.clientOid);
  }

  _putTracked(t) {
    const key = this._trackKey(t);
    this._tracked.set(key, t);
    if (t.orderId && t.clientOid && t.orderId !== t.clientOid) {
      // lookup alias only — iterate via unique keys set
      this._tracked.set(`alias:${t.clientOid}`, t);
      this._tracked.set(`alias:${t.orderId}`, t);
    }
  }

  _delTracked(t) {
    if (!t) return;
    this._tracked.delete(this._trackKey(t));
    if (t.orderId) {
      this._tracked.delete(t.orderId);
      this._tracked.delete(`alias:${t.orderId}`);
    }
    if (t.clientOid) {
      this._tracked.delete(t.clientOid);
      this._tracked.delete(`alias:${t.clientOid}`);
    }
  }

  _uniqueTracked() {
    const seen = new Set();
    const out = [];
    for (const [k, t] of this._tracked) {
      if (String(k).startsWith('alias:')) continue;
      const id = this._trackKey(t);
      if (seen.has(id)) continue;
      seen.add(id);
      out.push(t);
    }
    return out;
  }

  _findTracked(orderId, clientOid) {
    if (orderId && this._tracked.has(String(orderId))) return this._tracked.get(String(orderId));
    if (clientOid && this._tracked.has(String(clientOid))) return this._tracked.get(String(clientOid));
    if (orderId && this._tracked.has(`alias:${orderId}`)) return this._tracked.get(`alias:${orderId}`);
    if (clientOid && this._tracked.has(`alias:${clientOid}`)) return this._tracked.get(`alias:${clientOid}`);
    return null;
  }

  // ---------- market data ----------
  async getMarkets() {
    return [...this.markets.values()];
  }

  _market(marketId) {
    const m = this.markets.get(Number(marketId));
    if (!m) throw new Error('未知市场 marketId=' + marketId);
    return m;
  }

  async getCandles(marketId, intervalSec = 900, n = 200) {
    const m = this._market(marketId);
    const map = { 60: '1m', 300: '5m', 900: '15m', 1800: '30m', 3600: '1H', 14400: '4H', 86400: '1D' };
    const granularity = map[intervalSec] || '15m';
    const data = await this._publicGet('/api/v2/mix/market/candles', {
      symbol: m.symbol,
      productType: PRODUCT,
      granularity,
      limit: String(Math.min(n, 200)),
    });
    const rows = Array.isArray(data) ? data : [];
    return rows
      .map((c) => {
        const ts = Number(c[0]);
        return {
          time: ts < 1e12 ? ts * 1000 : ts,
          open: +c[1],
          high: +c[2],
          low: +c[3],
          close: +c[4],
          volume: +(c[5] ?? 0),
        };
      })
      .filter((c) => Number.isFinite(c.close))
      .sort((a, b) => a.time - b.time);
  }

  async getPrice(marketId) {
    const mId = Number(marketId);
    this._watch.add(mId);
    const m = this._market(mId);
    try {
      const data = await this._publicGet('/api/v2/mix/market/ticker', {
        symbol: m.symbol,
        productType: PRODUCT,
      });
      const row = Array.isArray(data) ? data[0] : data;
      const px = Number(row?.lastPr || row?.markPrice || 0);
      if (px > 0) {
        this._prices.set(mId, px);
        m.lastPrice = px;
        return px;
      }
    } catch { /* fall back */ }
    return this._prices.get(mId) ?? m.lastPrice;
  }

  async getFundingRate(marketId) {
    const m = this._market(marketId);
    try {
      const data = await this._publicGet('/api/v2/mix/market/current-fund-rate', {
        symbol: m.symbol,
        productType: PRODUCT,
      });
      const row = Array.isArray(data) ? data[0] : data;
      return Number(row?.fundingRate ?? 0) || 0;
    } catch {
      try {
        const data = await this._publicGet('/api/v2/mix/market/ticker', {
          symbol: m.symbol,
          productType: PRODUCT,
        });
        const row = Array.isArray(data) ? data[0] : data;
        return Number(row?.fundingRate || 0) || 0;
      } catch {
        return 0;
      }
    }
  }

  // ---------- trading ----------
  async setLeverage(marketId, x) {
    const m = this._market(marketId);
    const lev = String(Math.max(1, Number(x) || 1));
    try {
      await this._post('/api/v2/mix/account/set-leverage', {
        symbol: m.symbol,
        productType: PRODUCT,
        marginCoin: MARGIN,
        leverage: lev,
      });
      return true;
    } catch (e) {
      const msg = String(e.message || e);
      if (/no change|not modified/i.test(msg)) return true;
      // 部分账户要求分别设 long/short
      try {
        await this._post('/api/v2/mix/account/set-leverage', {
          symbol: m.symbol,
          productType: PRODUCT,
          marginCoin: MARGIN,
          leverage: lev,
          holdSide: 'long',
        });
        await this._post('/api/v2/mix/account/set-leverage', {
          symbol: m.symbol,
          productType: PRODUCT,
          marginCoin: MARGIN,
          leverage: lev,
          holdSide: 'short',
        });
        return true;
      } catch (e2) {
        this.emit('error', e2);
        return false;
      }
    }
  }

  async placeLimitOrder(o) {
    const m = this._market(o.marketId);
    const qtyStr = alignToStep(o.sizeBase, m.qtyStep, 'down');
    const priceStr = alignToStep(o.price, m.priceStep, 'nearest');
    if (!(Number(qtyStr) > 0)) throw new Error('数量过小，低于市场最小下单单位。');
    if (!(Number(priceStr) > 0)) throw new Error('价格无效');

    const clientOid = `nk${Date.now()}${Math.floor(Math.random() * 1e6)}`;
    const data = await this._post('/api/v2/mix/order/place-order', {
      symbol: m.symbol,
      productType: PRODUCT,
      marginMode: this.marginMode,
      marginCoin: MARGIN,
      size: qtyStr,
      price: priceStr,
      side: o.side === 'sell' ? 'sell' : 'buy',
      orderType: 'limit',
      force: o.postOnly === false ? 'gtc' : 'post_only',
      clientOid,
      reduceOnly: o.reduceOnly ? 'YES' : 'NO',
    });

    // Bitget RO 可能不返回 orderId
    const orderId = String(data?.orderId || '').trim() || null;
    const trackId = orderId || clientOid;
    this._watch.add(m.marketId);
    const tracked = {
      orderId: orderId || trackId,
      clientOid,
      marketId: m.marketId,
      symbol: m.symbol,
      levelIndex: o.levelIndex,
      side: o.side,
      price: Number(priceStr),
      sizeBase: Number(qtyStr),
      seen: false,
      reduceOnly: !!o.reduceOnly,
      placedAt: Date.now(),
    };
    this._putTracked(tracked);
    return { orderId: trackId, clientOid };
  }

  async cancelOrder(marketId, orderId) {
    const m = this._market(marketId);
    const key = String(orderId);
    const t = this._findTracked(key, key);
    const body = { symbol: m.symbol, productType: PRODUCT };
    if (t?.orderId && /^\d{6,}$/.test(String(t.orderId))) body.orderId = String(t.orderId);
    else if (t?.clientOid) body.clientOid = t.clientOid;
    else if (/^\d{6,}$/.test(key)) body.orderId = key;
    else body.clientOid = key;
    const r = await this._post('/api/v2/mix/order/cancel-order', body);
    this._delTracked(t || { orderId: key, clientOid: key });
    return r;
  }

  async cancelAll(marketId) {
    const m = this._market(marketId);
    try {
      const r = await this._post('/api/v2/mix/order/cancel-all-orders', {
        symbol: m.symbol,
        productType: PRODUCT,
        marginCoin: MARGIN,
      });
      for (const t of this._uniqueTracked()) {
        if (t.marketId === m.marketId) this._delTracked(t);
      }
      return r;
    } catch (e) {
      this.emit('error', e);
      return false;
    }
  }

  getOpenOrders(marketId) {
    return this.getOpenOrdersForMarket(marketId);
  }

  getPosition(marketId) {
    const p = this._pos.get(Number(marketId));
    return p && p.sizeBase !== 0 ? p : null;
  }

  async _refreshAllPositions() {
    try {
      const data = await this._get('/api/v2/mix/position/all-position', {
        productType: PRODUCT,
        marginCoin: MARGIN,
      });
      const list = Array.isArray(data) ? data : [];
      const parsed = [];
      const touched = new Set();
      for (const p of list) {
        const symbol = String(p.symbol || '').toUpperCase();
        const marketId = this.marketIdForName(symbol);
        const m = marketId ? this.markets.get(marketId) : null;
        const rawTotal = Number(p.total ?? p.available ?? 0);
        const qty = Math.abs(rawTotal);
        if (!(qty > 0)) continue;
        const side = String(p.holdSide || p.posSide || '').toLowerCase();
        // one_way: holdSide is long/short (preferred); net/empty fall back to signed size
        let sizeBase;
        if (side === 'short' || side === 'sell') sizeBase = -qty;
        else if (side === 'long' || side === 'buy') sizeBase = qty;
        else if (rawTotal < 0 || Number(p.available) < 0) sizeBase = -qty;
        else sizeBase = qty;

        const entry = Number(p.openPriceAvg || p.averageOpenPrice || 0);
        const mark = Number(p.markPrice || this._prices.get(marketId) || entry);
        const upnl = Number(p.unrealizedPL || p.unrealizedPnl || 0);
        const row = {
          market: m?.name || SYMBOL_TO_EXTENDED_NAME[symbol] || symbol,
          marketId,
          side: sizeBase < 0 ? 'short' : 'long',
          size: qty,
          sizeBase,
          entryPrice: entry,
          markPrice: mark,
          valueUsd: Math.round(qty * mark * 100) / 100,
          unrealizedPnl: upnl,
          realizedPnl: Number(p.achievedProfits || 0),
          unrealizedPct: 0,
          leverage: p.leverage != null ? Number(p.leverage) : null,
          margin: p.marginSize != null ? Number(p.marginSize) : null,
          liquidationPrice: p.liquidationPrice != null ? Number(p.liquidationPrice) : null,
        };
        parsed.push(row);
        if (marketId) {
          touched.add(marketId);
          this._pos.set(marketId, {
            sizeBase,
            entryPrice: entry,
            unrealizedPnl: upnl,
            leverage: row.leverage,
          });
        }
      }
      for (const id of [...this._pos.keys()]) {
        if (!touched.has(id)) this._pos.delete(id);
      }
      parsed.sort((a, b) => Math.abs(b.valueUsd) - Math.abs(a.valueUsd));
      this._allPositions = parsed;
    } catch { /* keep */ }
  }

  getAllPositions() {
    return (this._allPositions || []).map((p) => ({ ...p }));
  }

  async _refreshAllOpenOrders() {
    try {
      const open = [];
      const symbols = this.statsMarketNames?.length
        ? this.statsMarketNames
        : Object.keys(EXTENDED_NAME_TO_SYMBOL);
      for (const name of symbols) {
        const mId = this.marketIdForName(name);
        if (!mId) continue;
        const m = this.markets.get(mId);
        let data;
        try {
          data = await this._get('/api/v2/mix/order/orders-pending', {
            symbol: m.symbol,
            productType: PRODUCT,
          });
        } catch {
          continue;
        }
        const rows = Array.isArray(data) ? data : data?.entrustedList || [];
        for (const o of rows) {
          const id = String(o.orderId || '');
          const cid = String(o.clientOid || '');
          const sideRaw = String(o.side || '').toLowerCase();
          const side = sideRaw.startsWith('sell') ? 'sell' : 'buy';
          const tracked = this._findTracked(id, cid);
          if (tracked) tracked.seen = true;
          open.push({
            orderId: id || cid,
            market: m.name,
            marketId: mId,
            side,
            price: Number(o.price || 0),
            sizeBase: Number(o.size || o.baseVolume || 0),
            reduceOnly: String(o.reduceOnly || '').toUpperCase() === 'YES' || !!tracked?.reduceOnly,
            type: 'LIMIT',
            status: String(o.status || o.state || 'live').toUpperCase(),
            levelIndex: tracked?.levelIndex ?? null,
          });
        }
      }
      open.sort((a, b) => b.price - a.price);
      this._allOpenOrders = open;
    } catch { /* keep */ }
  }

  getAllOpenOrders() {
    return (this._allOpenOrders || []).map((o) => ({ ...o }));
  }

  getCachedOpenOrders(marketId) {
    return this.getOpenOrdersForMarket(marketId);
  }

  getOpenOrdersForMarket(marketId) {
    const id = Number(marketId);
    const out = this.getAllOpenOrders().filter((o) => o.marketId === id);
    const seen = new Set(out.map((o) => o.orderId));
    for (const t of this._uniqueTracked()) {
      if (t.marketId !== id) continue;
      const oid = t.orderId || t.clientOid;
      if (seen.has(oid)) continue;
      out.push({
        orderId: oid,
        marketId: id,
        side: t.side,
        price: t.price,
        sizeBase: t.sizeBase,
        levelIndex: t.levelIndex ?? null,
        reduceOnly: !!t.reduceOnly,
        type: 'LIMIT',
      });
      seen.add(oid);
    }
    out.sort((a, b) => b.price - a.price);
    return out;
  }

  async closePosition(marketId) {
    const m = this._market(marketId);
    await this._refreshAllPositions().catch(() => {});
    const p = this._pos.get(m.marketId);
    if (!p || !p.sizeBase) return true;

    // Prefer dedicated close-positions endpoint when available
    try {
      await this._post('/api/v2/mix/order/close-positions', {
        symbol: m.symbol,
        productType: PRODUCT,
      });
      return true;
    } catch {
      /* fallback market RO */
    }

    const side = p.sizeBase < 0 ? 'buy' : 'sell';
    const qtyStr = alignToStep(Math.abs(p.sizeBase), m.qtyStep, 'down');
    return this._post('/api/v2/mix/order/place-order', {
      symbol: m.symbol,
      productType: PRODUCT,
      marginMode: this.marginMode,
      marginCoin: MARGIN,
      size: qtyStr,
      side,
      orderType: 'market',
      force: 'ioc',
      reduceOnly: 'YES',
      clientOid: `nkc${Date.now()}`,
    });
  }

  // ---------- account / stats ----------
  async _refreshAccount() {
    this._balanceRefreshError = null;
    try {
      const data = await this._get('/api/v2/mix/account/accounts', { productType: PRODUCT });
      const rows = Array.isArray(data) ? data : [];
      const row = rows.find((r) => String(r.marginCoin || '').toUpperCase() === MARGIN) || rows[0];
      if (row) {
        this.equity = Number(row.accountEquity || row.usdtEquity || 0);
        this.balance = Number(row.crossedMaxAvailable || row.available || this.equity);
        this.availableForTrade = Number(row.crossedMaxAvailable || row.available || 0);
        this.unrealisedPnl = Number(row.unrealizedPL || 0);
        this.availableForWithdrawal = this.availableForTrade;
        this.initialMargin = Number(row.locked || row.isolatedMaxAvailable || 0);
      }
      this._balanceRefreshAt = Date.now();
    } catch (e) {
      this._balanceRefreshError = e.message;
    }
  }

  async _refreshOfficialStats() {
    let volume = 0;
    let fees = 0;
    let realized = 0;
    const byMarket = {};
    try {
      for (const extName of Object.keys(EXTENDED_NAME_TO_SYMBOL)) {
        const apiSym = EXTENDED_NAME_TO_SYMBOL[extName];
        byMarket[extName] = { realizedPnl: 0, fees: 0, volume: 0 };
        const data = await this._get('/api/v2/mix/order/fills', {
          symbol: apiSym,
          productType: PRODUCT,
          limit: '100',
        });
        const rows = Array.isArray(data) ? data : data?.fillList || [];
        for (const f of rows) {
          const qty = Number(f.baseVolume || f.sizeQty || f.size || 0);
          const px = Number(f.price || f.fillPrice || 0);
          const fee = Math.abs(Number(f.fee || f.feeDetail?.[0]?.totalFee || 0));
          const pnl = Number(f.profit || 0);
          volume += Math.abs(qty * px);
          fees += fee;
          realized += pnl;
          byMarket[extName].volume += Math.abs(qty * px);
          byMarket[extName].fees += fee;
          byMarket[extName].realizedPnl += pnl;
        }
      }
    } catch { /* optional */ }

    let recentClosed = [];
    let allClosed = [];
    try {
      const hist = await this._get('/api/v2/mix/position/history-position', {
        productType: PRODUCT,
        limit: '50',
      });
      const list = Array.isArray(hist) ? hist : hist?.list || [];
      allClosed = list.map((h) => {
        const symbol = String(h.symbol || '').toUpperCase();
        const market = SYMBOL_TO_EXTENDED_NAME[symbol] || symbol;
        const openFee = Math.abs(Number(h.openFee || 0));
        const closeFee = Math.abs(Number(h.closeFee || 0));
        return {
          market,
          side: String(h.holdSide || 'long').toLowerCase(),
          size: Number(h.closeTotalPos || h.openTotalPos || 0),
          openPrice: Number(h.openAvgPrice || 0),
          exitPrice: Number(h.closeAvgPrice || 0),
          realizedPnl: Number(h.pnl ?? h.netProfit ?? 0),
          fees: openFee + closeFee,
          closedTime: Number(h.utime || h.uTime || h.ctime || h.cTime || Date.now()),
        };
      });
      recentClosed = allClosed.slice(0, 30);
      // Prefer history-position realized if fills sum looks empty
      if (!(Math.abs(realized) > 0) && allClosed.length) {
        realized = allClosed.reduce((s, p) => s + (p.realizedPnl || 0), 0);
      }
    } catch { /* optional */ }

    this.realizedPnl = realized || this.realizedPnl;
    this._statsCache = {
      realizedPnl: this.realizedPnl,
      unrealizedPnl: this.unrealisedPnl,
      totalPnl: (this.realizedPnl || 0) + (this.unrealisedPnl || 0),
      volume: volume || null,
      feesPaid: fees || null,
      statsWindow: 'bitget-fills+history',
      pnlSource: 'portfolio',
      recentClosed,
      allClosed,
      byMarket,
      updatedAt: Date.now(),
    };
  }

  getOfficialStats() {
    return this._statsCache;
  }

  async fetchAllTrades(marketNames) {
    const names = marketNames?.length ? marketNames : Object.keys(EXTENDED_NAME_TO_SYMBOL);
    const out = [];
    for (const name of names) {
      const apiSym = this._apiSymbol(name);
      const extName = SYMBOL_TO_EXTENDED_NAME[apiSym] || name;
      try {
        const data = await this._get('/api/v2/mix/order/fills', {
          symbol: apiSym,
          productType: PRODUCT,
          limit: '100',
        });
        const rows = Array.isArray(data) ? data : data?.fillList || [];
        for (const f of rows) {
          // Shape aligned with upstream journal-backfill (Extended trade fields)
          out.push({
            id: String(f.tradeId || f.fillId || `${f.orderId}-${f.cTime}`),
            market: extName,
            side: String(f.side || '').toLowerCase().startsWith('sell') ? 'SELL' : 'BUY',
            price: Number(f.price || f.fillPrice || 0),
            qty: Number(f.baseVolume || f.size || 0),
            quantity: Number(f.baseVolume || f.size || 0),
            orderId: String(f.orderId || ''),
            createdTime: Number(f.cTime || f.createdTime || Date.now()),
            timestamp: Number(f.cTime || f.createdTime || Date.now()),
          });
        }
      } catch { /* skip */ }
    }
    return out;
  }

  // ---------- polling ----------
  start() {
    if (!this._timer) {
      this._timer = setInterval(() => this._poll(), this.pollMs);
      this._timer.unref?.();
    }
  }

  stop() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  async _poll() {
    if (this._busy) return;
    this._busy = true;
    try {
      for (const mId of this._watch) {
        const m = this.markets.get(mId);
        if (!m) continue;
        this.getPrice(mId)
          .then((px) => {
            if (px) this.emit('price', { marketId: mId, price: px });
          })
          .catch(() => {});

        let pending = null;
        try {
          const data = await this._get('/api/v2/mix/order/orders-pending', {
            symbol: m.symbol,
            productType: PRODUCT,
          });
          pending = Array.isArray(data) ? data : data?.entrustedList || [];
        } catch {
          // Never treat poll failure as “all orders gone” → fake fills.
          await this._pollFillsBackup(m).catch(() => {});
          continue;
        }

        const liveIds = new Set();
        for (const o of pending) {
          const oid = String(o.orderId || '');
          const cid = String(o.clientOid || '');
          if (oid) liveIds.add(oid);
          if (cid) liveIds.add(cid);
          const t = this._findTracked(oid, cid);
          if (t) {
            t.seen = true;
            if (oid && !t.orderId) t.orderId = oid;
          }
        }

        for (const t of this._uniqueTracked()) {
          if (t.marketId !== mId) continue;
          const oid = String(t.orderId || '');
          const cid = String(t.clientOid || '');
          if (liveIds.has(oid) || liveIds.has(cid)) continue;
          if (Date.now() - (t.placedAt || 0) < Math.min(800, this.pollMs * 0.4)) continue;
          this._delTracked(t);
          await this._resolveGone(t, m.symbol);
        }

        // backup: recent fills feed
        await this._pollFillsBackup(m).catch(() => {});
      }
      await this._refreshAccount().catch(() => {});
      await this._refreshAllPositions().catch(() => {});
      await this._refreshAllOpenOrders().catch(() => {});
      this._refreshOfficialStats().catch(() => {});
    } catch (e) {
      this.emit('error', e);
    } finally {
      this._busy = false;
    }
  }

  async _pollFillsBackup(m) {
    const data = await this._get('/api/v2/mix/order/fills', {
      symbol: m.symbol,
      productType: PRODUCT,
      limit: '50',
    });
    const rows = Array.isArray(data) ? data : data?.fillList || [];
    for (const f of rows) {
      const tradeId = String(f.tradeId || f.fillId || `${f.orderId}-${f.cTime}`);
      if (!tradeId || this._emittedFills.has(tradeId)) continue;
      const oid = String(f.orderId || '');
      const cid = String(f.clientOid || '');
      const t = this._findTracked(oid, cid);
      if (!t) continue;
      this._emittedFills.add(tradeId);
      this._delTracked(t);
      this._emitFill(t, {
        price: Number(f.price || f.fillPrice || t.price),
        sizeBase: Number(f.baseVolume || f.size || t.sizeBase),
      });
    }
  }

  async _resolveGone(t, symbol) {
    let filled = null; // null = unknown — do not invent fills
    let price = t.price;
    let sizeBase = t.sizeBase;
    try {
      const params = { symbol, productType: PRODUCT };
      if (t.clientOid) params.clientOid = t.clientOid;
      else params.orderId = t.orderId;
      const data = await this._get('/api/v2/mix/order/detail', params);
      const st = String(data?.status || data?.state || '').toLowerCase();
      const filledQty = Number(data?.baseVolume || data?.filledQty || 0);
      if (Number(data?.priceAvg || 0) > 0) price = Number(data.priceAvg);
      if (filledQty > 0) sizeBase = filledQty;

      // Still on book — pending poll was a miss
      if (st === 'live' || st === 'new' || st === 'partial_fill_live') {
        t.seen = true;
        this._putTracked(t);
        return;
      }
      if ((st.includes('cancel') || st === 'canceled') && !(filledQty > 0)) {
        filled = false;
      } else if (filledQty > 0 || st === 'filled' || st.includes('fill')) {
        filled = true;
        if (!(filledQty > 0) && (st.includes('cancel') || st === 'canceled')) filled = false;
      }
    } catch {
      // Detail failed — re-track; fills backup will confirm later
      this._putTracked(t);
      return;
    }

    if (filled == null) {
      this._putTracked(t);
      return;
    }
    if (filled) this._emitFill(t, { price, sizeBase });
    else this.emit('cancel', { orderId: t.orderId || t.clientOid, marketId: t.marketId });
  }

  _emitFill(t, { price, sizeBase }) {
    const dedupe = `${t.orderId || t.clientOid}:${sizeBase}:${price}`;
    if (this._emittedFills.has(dedupe)) return;
    this._emittedFills.add(dedupe);
    this.emit('fill', {
      orderId: t.orderId || t.clientOid,
      marketId: t.marketId,
      side: t.side,
      price,
      sizeBase,
      levelIndex: t.levelIndex,
    });
  }
}

/** Non-secret static checks for CI / local smoke (no API key required). */
export async function selfCheckPublic() {
  const ex = new BitgetExchange({});
  const rows = await ex._publicGet('/api/v2/mix/market/contracts', { productType: PRODUCT });
  const list = Array.isArray(rows) ? rows : [];
  const report = { ok: true, symbols: {} };
  for (const sym of BITGET_FLEET_SYMBOLS) {
    const m = list.find((x) => String(x.symbol).toUpperCase() === sym);
    if (!m) {
      report.ok = false;
      report.symbols[sym] = { error: 'missing' };
      continue;
    }
    const pricePlace = Number(m.pricePlace);
    const stepPrice = Math.pow(10, -pricePlace);
    const stepSize = Number(m.sizeMultiplier || m.minTradeNum);
    report.symbols[sym] = {
      pricePlace,
      stepPrice,
      stepSize,
      minTradeNum: m.minTradeNum,
      maxLever: m.maxLever,
    };
  }
  const ticker = await ex._publicGet('/api/v2/mix/market/ticker', {
    symbol: 'BTCUSDT',
    productType: PRODUCT,
  });
  const row = Array.isArray(ticker) ? ticker[0] : ticker;
  report.btcLast = Number(row?.lastPr || 0);
  report.ok = report.ok && report.btcLast > 0;
  return report;
}
