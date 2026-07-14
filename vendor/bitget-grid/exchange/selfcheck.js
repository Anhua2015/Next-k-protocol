#!/usr/bin/env node
/**
 * Adapter self-check:
 * 1) Public contract meta (no key)
 * 2) Optional live auth checks if BITGET_API_* present (account/mode/positions, no orders)
 */
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { getConfig } from '../config.js';
import { BitgetExchange, selfCheckPublic, BITGET_FLEET_SYMBOLS, alignToStep } from './bitget.js';

const ROOT = path.dirname(fileURLToPath(import.meta.url));

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

async function main() {
  console.log('=== Bitget adapter self-check ===');
  const pub = await selfCheckPublic();
  console.log(JSON.stringify(pub, null, 2));
  assert(pub.ok, 'public meta failed');
  for (const sym of BITGET_FLEET_SYMBOLS) {
    const s = pub.symbols[sym];
    assert(s && s.stepSize > 0 && s.stepPrice > 0, `bad steps for ${sym}`);
    const q = alignToStep(1.23456789, s.stepSize, 'down');
    const p = alignToStep(pub.btcLast || 60000, s.stepPrice, 'nearest');
    assert(Number(q) > 0 && Number(p) > 0, `align failed ${sym}`);
  }
  console.log('[ok] public meta + alignToStep');

  // Absolute alignment: upstream names (BTC-USD) ↔ Bitget API (BTCUSDT)
  const mapEx = new BitgetExchange({});
  await mapEx._loadMarkets();
  for (const name of ['BTC-USD', 'ETH-USD', 'SOL-USD']) {
    const mid = mapEx.marketIdForName(name);
    assert(mid, `missing fleet name ${name}`);
    const m = mapEx.markets.get(mid);
    assert(m.name === name && m.displayName === name, `display name not upstream: ${name}`);
    assert(BITGET_FLEET_SYMBOLS.includes(m.symbol), `api symbol for ${name}: ${m.symbol}`);
    assert(mapEx.marketIdForName(m.symbol) === mid, `reverse lookup ${m.symbol}`);
  }
  console.log('[ok] BTC-USD ↔ BTCUSDT name alignment');

  // unit: track dedupe
  const dummy = new BitgetExchange({});
  dummy._putTracked({ orderId: '111', clientOid: 'nk1', marketId: 1, side: 'buy', price: 1, sizeBase: 1 });
  assert(dummy._uniqueTracked().length === 1, 'unique tracked broken');
  dummy._delTracked(dummy._uniqueTracked()[0]);
  assert(dummy._uniqueTracked().length === 0, 'del tracked broken');
  console.log('[ok] tracked map dedupe');

  const cfg = getConfig();
  if (!cfg.apiKey || !cfg.apiSecret || !cfg.passphrase) {
    console.log('[skip] live auth checks (no BITGET_API_*). Public checks passed.');
    console.log('RESULT: PASS (public)');
    return;
  }

  const ex = new BitgetExchange({
    apiKey: cfg.apiKey,
    apiSecret: cfg.apiSecret,
    passphrase: cfg.passphrase,
    apiUrl: cfg.apiUrl,
  });
  // do not start poll loop — call pieces
  await ex._ensureOneWayMode();
  console.log('[ok] one_way_mode', ex.posMode);
  await ex._loadMarkets();
  for (const name of ['BTC-USD', 'ETH-USD', 'SOL-USD']) {
    const mid = ex.marketIdForName(name);
    assert(mid, `missing market ${name}`);
    const m = ex.markets.get(mid);
    assert(m.name === name && BITGET_FLEET_SYMBOLS.includes(m.symbol), `name/symbol map ${name}`);
    assert(ex.marketIdForName(m.symbol) === mid, `reverse map ${m.symbol}`);
  }
  await ex._refreshAccount();
  console.log('[ok] account equity=', ex.equity, 'available=', ex.availableForTrade);
  await ex._refreshAllPositions();
  await ex._refreshAllOpenOrders();
  console.log('[ok] positions=', ex.getAllPositions().length, 'openOrders=', ex.getAllOpenOrders().length);
  const mid = ex.marketIdForName('BTC-USD');
  const px = await ex.getPrice(mid);
  assert(px > 0, 'btc price');
  const candles = await ex.getCandles(mid, 900, 30);
  assert(candles.length >= 10, 'candles');
  console.log('[ok] price', px, 'candles', candles.length);
  console.log('RESULT: PASS (public + live read)');
}

main().catch((e) => {
  console.error('RESULT: FAIL', e.message || e);
  process.exit(1);
});
