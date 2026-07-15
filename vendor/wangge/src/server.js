// Bitget-only：共享账户 + 一标的一机器人
//   /api/overview, /api/symbols, /api/s/:SYM/*, /api/exchange/*
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { getConfig, ROOT, normalizeSymbol } from './config.js';
import { createExchange } from './exchange/bg/index.js';
import { createFleet, snapKey } from './fleet.js';
import { analyzeTrend } from './trend.js';
import { setupProxies, checkProxy } from './proxy.js';
import { loadSnapshot, saveSnapshot, deleteSnapshot, loadState } from './persist.js';
import { createAiService } from './ai/service.js';
import { createScout } from './scout.js';
import { getAutoLog, pushAutoLog } from './autolog.js';

const cfg = getConfig();

{
  const missing = [];
  if (cfg.bg.mode === 'live') {
    if (!cfg.bg.apiKey) missing.push(['Bitget', 'BITGET_API_KEY', 'Bitget → API 管理']);
    if (!cfg.bg.apiSecret) missing.push(['Bitget', 'BITGET_API_SECRET', 'Bitget → API 管理']);
    if (!cfg.bg.passphrase) missing.push(['Bitget', 'BITGET_PASSPHRASE', '创建 API 时设置的口令']);
  }
  if (missing.length) {
    console.error('\n[启动失败] Bitget 设为 live，但 .env 还缺凭据：\n');
    for (const [ex, key, where] of missing) {
      console.error(`  ${ex}  缺 ${key}`);
      console.error(`            获取方式：${where}`);
    }
    console.error('\n解决：补齐 .env，或把 BG_MODE 改回 paper\n');
    process.exit(1);
  }
}

const proxyResult = await setupProxies(cfg);
if (proxyResult.used) {
  console.log('[代理] 已启用: ' + proxyResult.used);
  const chk = await checkProxy();
  if (chk.ok) console.log('[代理检测] ✓ 出口 IP: ' + chk.ip);
  else {
    console.error('[代理检测] ✗ ' + chk.error);
    if (cfg.bg.mode === 'live') process.exit(1);
  }
} else {
  console.log('[代理] 未配置（直连）');
}

const exchange = createExchange(cfg.bg);
{
  // Migrate legacy single-slot snapshot `bg` → sym_<SYMBOL>
  const legacy = loadSnapshot('bg');
  if (legacy) {
    const fromCfg = normalizeSymbol(
      legacy.config?.displayName || legacy.config?.name || legacy.config?.symbol || 'BTCUSDT',
    ) || 'BTCUSDT';
    const target = snapKey(fromCfg);
    if (!loadSnapshot(target)) {
      saveSnapshot(target, legacy);
      console.log(`[迁移] 旧快照 bg → ${target}`);
      if (!cfg.bg.symbols.includes(fromCfg)) {
        cfg.bg.symbols = [...cfg.bg.symbols, fromCfg];
        console.log(`[迁移] 已把 ${fromCfg} 并入启动标的列表`);
      }
    }
    try { deleteSnapshot('bg'); } catch {}
  }
}

const fleet = createFleet(exchange, cfg.bg.symbols);

{
  const state = loadState();
  for (const key of Object.keys(state)) {
    if (!key.startsWith('sym_')) continue;
    const sym = key.slice(4);
    if (fleet.get(sym)) continue;
    if (state[key]?.running) {
      console.warn(`[警告] 快照 ${key} 仍标记 running，但不在 BG_SYMBOLS 中 — 交易所上可能仍有挂单，请手动处理`);
    }
  }
}

function fleetProxy(getter) {
  return new Proxy({}, {
    get(_t, key) {
      if (typeof key !== 'string') return undefined;
      return getter(normalizeSymbol(key) || key);
    },
    ownKeys() { return fleet.list(); },
    getOwnPropertyDescriptor(_t, key) {
      if (typeof key !== 'string') return undefined;
      const sym = normalizeSymbol(key) || key;
      if (!getter(sym)) return undefined;
      return { enumerable: true, configurable: true, value: getter(sym) };
    },
    has(_t, key) {
      return typeof key === 'string' && !!getter(normalizeSymbol(key) || key);
    },
  });
}

const aiService = createAiService({
  bots: fleetProxy((sym) => fleet.get(sym)),
  exchanges: fleetProxy((sym) => (fleet.get(sym) ? exchange : null)),
});
aiService.start();

const scout = createScout({
  exchange,
  fleet,
  persistSymbols: () => { try { persistSymbolsEnv(); } catch (e) { console.error(e?.message || e); } },
  getMode: () => cfg.bg.mode,
  log: (m) => { try { console.log(m); } catch {} },
});

exchange.on('error', (e) => {
  try { console.error('[Bitget] ' + (e?.message || e)); } catch {}
  for (const sym of fleet.list()) {
    const bot = fleet.get(sym);
    if (bot?.running) {
      try { bot._handleExError(e); } catch {}
    }
  }
});

const streamClients = new Map();
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css',
  '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon',
  '.json': 'application/json', '.woff2': 'font/woff2',
};

function jsonReplacer(_k, v) {
  if (typeof v === 'bigint') return v.toString();
  if (typeof v === 'number' && !Number.isFinite(v)) return null;
  return v;
}

function send(res, code, obj) {
  const body = JSON.stringify(obj, jsonReplacer);
  if (res.headersSent) { try { res.end(); } catch {} return; }
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(body);
}

function readBody(req, maxBytes = 1_000_000) {
  return new Promise((resolve) => {
    let b = '', n = 0, done = false;
    req.on('data', (c) => {
      if (done) return;
      n += c.length;
      if (n > maxBytes) { done = true; try { req.destroy(); } catch {} resolve({}); return; }
      b += c;
    });
    req.on('end', () => { if (done) return; done = true; try { resolve(b ? JSON.parse(b) : {}); } catch { resolve({}); } });
  });
}

function pick(s, mode) {
  return {
    running: s.running, mode, balance: s.balance, equity: s.equity,
    totalPnl: s.totalPnl, realizedPnl: s.realizedPnl, unrealizedPnl: s.unrealizedPnl,
    returnPct: s.returnPct, volume: s.volume,
    completedRungs: s.stats?.completedRungs ?? 0,
    openOrders: s.openOrders ?? 0, exchangeOpenOrders: s.exchangeOpenOrders ?? null,
    outOfRange: s.outOfRange ?? false, health: s.health ?? null,
    lastPrice: s.lastPrice, config: s.config,
    autopilot: s.autopilot ?? null,
  };
}

function buildOverview() {
  const symbols = {};
  for (const sym of fleet.list()) {
    const bot = fleet.get(sym);
    if (bot) symbols[sym] = pick(bot.getState(), cfg.bg.mode);
  }
  return {
    exchange: 'Bitget', mode: cfg.bg.mode, network: cfg.bg.network,
    dataSource: exchange.dataSource || null,
    sharedAccount: true,
    symbols,
    scout: scout.status(),
    autoLog: getAutoLog(50),
  };
}

function persistSymbolsEnv() {
  const list = fleet.list().join(',');
  const envFile = path.join(ROOT, '.env');
  let content = fs.existsSync(envFile) ? fs.readFileSync(envFile, 'utf8') : '';
  let wrote = false;
  for (const key of ['BG_SYMBOLS', 'BITGET_SYMBOLS']) {
    const regex = new RegExp(`^\\s*${key}\\s*=.*$`, 'm');
    if (regex.test(content)) {
      content = content.replace(regex, `${key}=${list}`);
      wrote = true;
    }
  }
  if (!wrote) content = content.trimEnd() + `\nBG_SYMBOLS=${list}\n`;
  fs.writeFileSync(envFile, content, 'utf8');
  process.env.BG_SYMBOLS = list;
}

async function handleSymbolApi(req, res, sym, subPath, url) {
  const bot = fleet.get(sym);
  if (!bot) return send(res, 404, { error: '标的不存在: ' + sym });

  if (subPath === '/markets') {
    const all = await exchange.getMarkets();
    const mine = all.filter((x) => fleet.marketMatches(x, sym));
    return send(res, 200, {
      exchange: 'Bitget', symbol: sym, mode: cfg.bg.mode,
      dataSource: exchange.dataSource || (cfg.bg.mode === 'live' ? 'real' : 'synthetic'),
      network: exchange.network || cfg.bg.network,
      apiUrl: exchange.apiUrl || cfg.bg.apiUrl,
      markets: mine, locked: true, sharedAccount: true,
    });
  }

  if (subPath === '/trend') {
    let marketId = Number(url.searchParams.get('marketId') || 0);
    if (!marketId) {
      const m = await fleet.resolveMarket(sym);
      if (m) marketId = m.marketId;
    }
    const intervalSec = Number(url.searchParams.get('intervalSec') || 3600);
    let candles = [];
    try { candles = await exchange.getCandles(marketId, intervalSec, 200); } catch {}
    let price = null;
    try { price = await exchange.getPrice(marketId); } catch {}
    const analysis = (candles && candles.length >= 20)
      ? analyzeTrend(candles)
      : { trend: 'range', recommended: 'neutral', strength: 0, atrPct: null, price,
        detail: '暂时拿不到足够K线，可手动设区间后启动。' };
    return send(res, 200, { analysis, candles: (candles || []).slice(-120), marketId });
  }

  if (subPath === '/state') return send(res, 200, bot.getState());

  if (subPath === '/start' && req.method === 'POST') {
    try {
      const body = await readBody(req);
      const m = await fleet.resolveMarket(sym);
      if (!m) throw new Error('找不到市场: ' + sym);
      body.marketId = m.marketId;
      return send(res, 200, await bot.start(body));
    } catch (e) { return send(res, 400, { error: e.message }); }
  }
  if (subPath === '/stop' && req.method === 'POST') {
    try { return send(res, 200, await bot.stop(await readBody(req))); }
    catch (e) { return send(res, 400, { error: e.message }); }
  }
  if (subPath === '/adjust' && req.method === 'POST') {
    try { return send(res, 200, await bot.adjustRange(await readBody(req))); }
    catch (e) { return send(res, 400, { error: e.message }); }
  }
  if (subPath === '/reset' && req.method === 'POST') {
    try { return send(res, 200, await bot.resetStats()); }
    catch (e) { return send(res, 400, { error: e.message }); }
  }
  if (subPath === '/cancel-orders' && req.method === 'POST') {
    try { return send(res, 200, await bot.cancelAllOrders()); }
    catch (e) { return send(res, 400, { error: e.message }); }
  }
  if (subPath === '/autopilot' && req.method === 'POST') {
    try { return send(res, 200, bot.setAutopilot(await readBody(req))); }
    catch (e) { return send(res, 400, { error: e.message }); }
  }
  if (subPath === '/autopilot-run' && req.method === 'POST') {
    try {
      const r = await bot.runAutopilotTick();
      return send(res, 200, { ok: true, decision: r, state: bot.getState() });
    } catch (e) { return send(res, 400, { error: e.message }); }
  }
  if (subPath === '/start-recovery' && req.method === 'POST') {
    try {
      const body = await readBody(req);
      const m = await fleet.resolveMarket(sym);
      if (m) body.marketId = m.marketId;
      return send(res, 200, await bot.startRecovery(body));
    } catch (e) { return send(res, 400, { error: e.message }); }
  }
  if (subPath === '/close-position' && req.method === 'POST') {
    try {
      const m = await fleet.resolveMarket(sym);
      return send(res, 200, await bot.closePositionNow(m?.marketId));
    } catch (e) { return send(res, 400, { error: e.message }); }
  }

  if (subPath === '/stream') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
    res.write(`data: ${JSON.stringify(bot.getState(), jsonReplacer)}\n\n`);
    if (!streamClients.has(sym)) streamClients.set(sym, new Set());
    streamClients.get(sym).add(res);
    req.on('close', () => streamClients.get(sym)?.delete(res));
    return;
  }

  send(res, 404, { error: 'not found: ' + subPath });
}

const server = http.createServer(async (request, res) => {
  const url = new URL(request.url, 'http://localhost');
  const p = url.pathname;
  try {
    if (p === '/api/overview') return send(res, 200, buildOverview());

    if (p === '/api/overview/stream') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
      res.write(`data: ${JSON.stringify(buildOverview(), jsonReplacer)}\n\n`);
      server._overviewClients.add(res);
      request.on('close', () => server._overviewClients.delete(res));
      return;
    }

    if (p === '/api/symbols') {
      if (request.method === 'GET') {
        return send(res, 200, { symbols: fleet.list(), mode: cfg.bg.mode, exchange: 'Bitget', sharedAccount: true });
      }
      if (request.method === 'POST') {
        const b = await readBody(request);
        const sym = normalizeSymbol(b.symbol);
        if (!sym) return send(res, 400, { error: '请提供 symbol，如 ETHUSDT' });
        if (fleet.list().length >= 5) return send(res, 400, { error: '最多 5 个标的（选币官自动维护）' });
        const ds = exchange.dataSource;
        if (ds == null || ds === 'connecting') {
          return send(res, 400, { error: 'Bitget 尚未就绪，请稍后重试或先点「重连」' });
        }
        const m = await fleet.resolveMarket(sym).catch(() => null);
        if (!m) return send(res, 400, { error: 'Bitget 上找不到: ' + sym });
        const r = fleet.add(sym);
        try { persistSymbolsEnv(); } catch (e) { console.error(e?.message || e); }
        return send(res, 200, r);
      }
      if (request.method === 'DELETE') {
        const b = await readBody(request);
        const r = await fleet.remove(b.symbol);
        if (!r.ok) return send(res, 400, r);
        try { persistSymbolsEnv(); } catch {}
        const gone = normalizeSymbol(b.symbol);
        const clients = streamClients.get(gone);
        if (clients) { for (const c of clients) { try { c.end(); } catch {} } streamClients.delete(gone); }
        return send(res, 200, r);
      }
    }

    if (p === '/api/exchange/markets') {
      return send(res, 200, {
        exchange: 'Bitget', mode: cfg.bg.mode,
        dataSource: exchange.dataSource || (cfg.bg.mode === 'live' ? 'real' : 'synthetic'),
        network: exchange.network || cfg.bg.network,
        apiUrl: exchange.apiUrl || cfg.bg.apiUrl,
        markets: await exchange.getMarkets(),
      });
    }

    if (p === '/api/exchange/reconnect' && request.method === 'POST') {
      try {
        if (typeof exchange.reconnect === 'function') await exchange.reconnect();
        else if (typeof exchange.init === 'function') await exchange.init();
        const resumed = [], errors = [];
        for (const sym of fleet.list()) {
          const bot = fleet.get(sym);
          if (!bot || bot.running) continue;
          const snap = loadSnapshot(snapKey(sym));
          if (!(snap?.running && snap?.config)) continue;
          try {
            const m = await fleet.resolveMarket(sym);
            if (m) snap.config.marketId = m.marketId;
            await bot.resume(snap);
            resumed.push(sym);
          } catch (e) { errors.push({ symbol: sym, error: e?.message || String(e) }); }
        }
        for (const sym of fleet.list()) {
          const bot = fleet.get(sym);
          if (bot?.running) await bot.reconcileOpenOrders().catch(() => {});
        }
        return send(res, 200, { ok: true, resumed, errors, overview: buildOverview() });
      } catch (e) { return send(res, 500, { error: e?.message || String(e) }); }
    }

    if (p === '/api/ai/status') return send(res, 200, aiService.status());
    if (p === '/api/scout') return send(res, 200, scout.status());
    if (p === '/api/scout/run' && request.method === 'POST') {
      try { return send(res, 200, await scout.tick()); }
      catch (e) { return send(res, 500, { error: e?.message || String(e) }); }
    }
    if (p === '/api/ai/test' && request.method === 'POST') {
      try { return send(res, 200, await aiService.test()); }
      catch (e) { return send(res, 200, { ok: false, error: e?.message || String(e) }); }
    }
    if (p === '/api/ai/sentinel-run' && request.method === 'POST') {
      try {
        const r = await aiService.runSentinel();
        return send(res, 200, r || { error: aiService.sentinelError || '巡检失败' });
      } catch (e) { return send(res, 500, { error: e?.message || String(e) }); }
    }
    if (p === '/api/ai/market-run' && request.method === 'POST') {
      try { return send(res, 200, await aiService.runMarketAnalysis()); }
      catch (e) { return send(res, 500, { error: e?.message || String(e) }); }
    }
    if (p === '/api/ai/report' && request.method === 'POST') {
      try { return send(res, 200, await aiService.makeReport()); }
      catch (e) { return send(res, 500, { error: e?.message || String(e) }); }
    }
    if (p === '/api/ai/analyze' && request.method === 'POST') {
      try {
        const b = await readBody(request);
        const key = normalizeSymbol(b.ex || b.symbol || fleet.list()[0] || 'BTCUSDT');
        return send(res, 200, await aiService.analyze(key));
      } catch (e) { return send(res, 500, { error: e?.message || String(e) }); }
    }
    if (p === '/api/ai/chat' && request.method === 'POST') {
      try {
        const b = await readBody(request);
        if (!b.message) return send(res, 400, { error: '消息为空' });
        return send(res, 200, await aiService.chatControl(b.message, Array.isArray(b.history) ? b.history : []));
      } catch (e) { return send(res, 500, { error: e?.message || String(e) }); }
    }

    if (p === '/api/proxy-check') return send(res, 200, await checkProxy());
    if (p === '/api/proxy-config') {
      return send(res, 200, { global: process.env.GLOBAL_PROXY || '', bg: process.env.BITGET_PROXY || '' });
    }

    if (p === '/api/env' && request.method === 'POST') {
      try {
        const { key, value } = await readBody(request);
        const PROXY_KEYS = ['GLOBAL_PROXY', 'BITGET_PROXY'];
        const AI_KEYS = ['AI_PROVIDER','AI_API_KEY','AI_BASE_URL','AI_MODEL','AI_MODEL_SMALL','AI_SENTINEL_MINUTES','AI_MARKET_MINUTES','AI_REPORT_HOUR','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID','NOTIFY_WEBHOOK'];
        const FLEET_KEYS = ['BG_SYMBOLS', 'BITGET_SYMBOLS'];
        if (![...PROXY_KEYS, ...AI_KEYS, ...FLEET_KEYS].includes(key)) {
          return send(res, 400, { error: '不允许修改: ' + key });
        }
        const val = value == null ? '' : String(value).trim();
        if (val) {
          if (/\s/.test(val) || [...val].some((c) => c.charCodeAt(0) < 32) || val.length > 500) {
            return send(res, 400, { error: '值非法或过长' });
          }
        }
        if (val) process.env[key] = val; else delete process.env[key];
        const envFile = path.join(ROOT, '.env');
        let content = fs.existsSync(envFile) ? fs.readFileSync(envFile, 'utf8') : '';
        const regex = new RegExp(`^\\s*${key}\\s*=.*$`, 'm');
        const line = val ? `${key}=${val}` : `# ${key}=`;
        if (regex.test(content)) content = content.replace(regex, line);
        else content = content.trimEnd() + '\n' + line + '\n';
        fs.writeFileSync(envFile, content, 'utf8');
        return send(res, 200, { ok: true });
      } catch (e) { return send(res, 500, { error: e.message }); }
    }

    const m = p.match(/^\/api\/s\/([A-Za-z0-9]+)(\/.*)?$/);
    if (m) return await handleSymbolApi(request, res, normalizeSymbol(m[1]), m[2] || '', url);

    if (p.startsWith('/api/bg/')) {
      const sym = fleet.get('BTCUSDT') ? 'BTCUSDT' : (fleet.list()[0] || 'BTCUSDT');
      return await handleSymbolApi(request, res, sym, p.slice('/api/bg'.length), url);
    }

    let file = p === '/' ? '/index.html' : p;
    const full = path.join(ROOT, 'public', path.normalize(file).replace(/^(\.\.[/\\])+/, ''));
    if (fs.existsSync(full) && fs.statSync(full).isFile()) {
      res.writeHead(200, { 'Content-Type': MIME[path.extname(full)] || 'application/octet-stream' });
      return fs.createReadStream(full).pipe(res);
    }
    send(res, 404, { error: 'not found' });
  } catch (e) {
    send(res, 500, { error: e.message });
  }
});

server._overviewClients = new Set();

setInterval(() => {
  const stringify = (obj) => JSON.stringify(obj, jsonReplacer);
  for (const [sym, clients] of streamClients) {
    if (!clients.size) continue;
    const bot = fleet.get(sym);
    if (!bot) continue;
    const data = `data: ${stringify(bot.getState())}\n\n`;
    for (const r of clients) { try { r.write(data); } catch { clients.delete(r); } }
  }
  if (server._overviewClients.size > 0) {
    const data = `data: ${stringify(buildOverview())}\n\n`;
    for (const r of server._overviewClients) { try { r.write(data); } catch { server._overviewClients.delete(r); } }
  }
}, 1000);

server.on('error', (e) => {
  if (e.code === 'EADDRINUSE') console.error(`\n[启动失败] 端口 ${cfg.port} 已被占用。\n`);
  else console.error('[服务器错误] ' + (e?.message || e));
  process.exit(1);
});

try {
  await exchange.init();
  console.log(`[Bitget] ✓ 连接成功 [${cfg.bg.mode.toUpperCase()}]`);
} catch (e) {
  console.error(`[Bitget] ✗ 初始化失败：${e?.message || e}`);
}

for (const sym of fleet.list()) {
  const bot = fleet.get(sym);
  const snap = loadSnapshot(snapKey(sym));
  if (!(snap?.running && snap?.config) || !bot) continue;
  if (exchange.dataSource == null || exchange.dataSource === 'connecting') {
    console.log(`[恢复] ${sym} 未连接，跳过续跑`);
    continue;
  }
  try {
    const m = await fleet.resolveMarket(sym);
    if (m) snap.config.marketId = m.marketId;
    await bot.resume(snap);
    console.log(`[恢复] ${sym} 已续跑`);
  } catch (e) {
    console.error(`[恢复] ${sym} 失败：${e?.message || e}`);
    await bot.recoverStrayOrders().catch(() => {});
  }
}

for (const sym of fleet.list()) {
  const bot = fleet.get(sym);
  if (!bot || exchange.dataSource == null) continue;
  try {
    const m = await fleet.resolveMarket(sym);
    if (m) {
      if (bot.config) bot.config.marketId = m.marketId;
      await exchange.getPrice(m.marketId).catch(() => {});
    }
  } catch {}
}

server.listen(cfg.port, cfg.host, () => {
  console.log(`\n${'═'.repeat(52)}`);
  console.log(`  Bitget 网格（多标的）已启动`);
  console.log(`  仪表盘: http://${cfg.host === '0.0.0.0' ? 'localhost' : cfg.host}:${cfg.port}`);
  console.log(`${'═'.repeat(52)}`);
  console.log(`  模式    ${cfg.bg.mode.toUpperCase()} · 共享账户余额`);
  console.log(`  标的    ${fleet.list().join(', ') || '（由选币官自动填充，最多5个）'}`);
  console.log('');
  pushAutoLog({ source: '系统', type: 'boot', message: `wangge 已启动 · ${cfg.bg.mode.toUpperCase()} · 选币官最多 ${5} 个机器人` });
  scout.start();
});
