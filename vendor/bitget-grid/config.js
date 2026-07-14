// Minimal .env loader + Bitget config (forked from Extended grid config).
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));

export function loadEnv() {
  const file = path.join(root, '.env');
  if (fs.existsSync(file)) {
    for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)$/);
      if (m && process.env[m[1]] === undefined) {
        process.env[m[1]] = m[2].trim().replace(/^["']|["']$/g, '');
      }
    }
  }
  // Also inherit next-k-api/.env.oi if running under monorepo
  const oi = path.resolve(root, '../../.env.oi');
  if (fs.existsSync(oi)) {
    for (const line of fs.readFileSync(oi, 'utf8').split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)$/);
      if (!m) continue;
      const k = m[1];
      if (process.env[k] !== undefined) continue;
      let v = m[2].trim().replace(/^["']|["']$/g, '');
      v = v.split(' #')[0].split('\t#')[0].trim();
      process.env[k] = v;
    }
  }
}

export function getConfig() {
  loadEnv();
  return {
    network: 'mainnet',
    // Prefer BITGET_GRID_PORT so Railway public $PORT is not stolen by the Node worker.
    port: Number(process.env.BITGET_GRID_PORT || process.env.PORT || 8080),
    apiKey: process.env.BITGET_API_KEY || '',
    apiSecret: process.env.BITGET_API_SECRET || '',
    passphrase: process.env.BITGET_PASSPHRASE || process.env.BITGET_API_PASSPHRASE || '',
    apiUrl: (process.env.BITGET_API_URL || 'https://api.bitget.com').replace(/\/$/, ''),
    pollMs: Number(process.env.BITGET_GRID_POLL_MS || 2500),
    proxy: process.env.HTTPS_PROXY || process.env.HTTP_PROXY || '',
    authToken: process.env.GRID_AUTH_TOKEN || '',
  };
}

export const ROOT = root;
