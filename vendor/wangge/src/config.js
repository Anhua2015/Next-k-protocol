// Bitget-only config: one shared account, N symbol bots.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

export function loadEnv() {
  const file = path.join(root, '.env');
  if (fs.existsSync(file)) {
    for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)$/);
      if (m && process.env[m[1]] === undefined) {
        let v = m[2].trim();
        const q = v.match(/^"([^"]*)"|^'([^']*)'/);
        if (q) v = q[1] ?? q[2];
        else v = v.replace(/\s+#.*$/, '').trim();
        process.env[m[1]] = v;
      }
    }
  }
}

/** Normalize: btc-usdt / BTC → BTCUSDT */
export function normalizeSymbol(raw) {
  const s = String(raw || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
  if (!s) return '';
  if (s.endsWith('USDT') || s.endsWith('USDC') || s.endsWith('USD')) return s;
  return s + 'USDT';
}

function parseSymbols(raw) {
  const list = String(raw || 'BTCUSDT')
    .split(/[,;\s]+/)
    .map(normalizeSymbol)
    .filter(Boolean);
  return [...new Set(list)].slice(0, 5);
}

export function getConfig() {
  loadEnv();
  const globalProxy =
    process.env.GLOBAL_PROXY ||
    process.env.HTTPS_PROXY ||
    process.env.HTTP_PROXY ||
    '';

  // Durable file wins when present (survives redeploy). Env is first-boot seed only —
  // Railway often has BG_SYMBOLS="" or a stale seed that would otherwise wipe the roster.
  let symbolsRaw = '';
  try {
    const dataDir = process.env.WANGGE_DATA_DIR || process.env.DATA_DIR || '';
    const file = path.join(dataDir ? path.resolve(dataDir) : root, 'wangge_symbols.txt');
    if (fs.existsSync(file)) {
      const raw = fs.readFileSync(file, 'utf8').trim();
      if (raw) symbolsRaw = raw;
    }
  } catch { /* ignore */ }
  if (!String(symbolsRaw || '').trim()) {
    symbolsRaw = process.env.BG_SYMBOLS || process.env.BITGET_SYMBOLS || '';
  }

  const bgNet = (process.env.BG_NETWORK || process.env.BITGET_NETWORK || 'mainnet').toLowerCase();
  const bg = {
    mode: (process.env.BG_MODE || process.env.BITGET_MODE || 'paper').toLowerCase() === 'live' ? 'live' : 'paper',
    network: bgNet,
    apiKey: process.env.BITGET_API_KEY || '',
    apiSecret: process.env.BITGET_API_SECRET || '',
    passphrase: process.env.BITGET_PASSPHRASE || '',
    apiUrl: (process.env.BITGET_API_URL || 'https://api.bitget.com').replace(/\/$/, ''),
    productType: (process.env.BITGET_PRODUCT_TYPE || 'USDT-FUTURES').toUpperCase(),
    marginMode: (process.env.BITGET_MARGIN_MODE || 'crossed').toLowerCase(),
    startBalance: Number(process.env.PAPER_BALANCE || 15000),
    proxy: process.env.BITGET_PROXY || globalProxy,
    // Empty = rely on scout / UI to populate (no seed BTCUSDT).
    symbols: parseSymbols(symbolsRaw),
  };

  return {
    port: Number(process.env.PORT || 8080),
    host: process.env.HOST || '127.0.0.1',
    globalProxy,
    bg,
  };
}

export const ROOT = root;
