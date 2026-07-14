import { BitgetExchange } from './bitget.js';

/** Factory: Bitget USDT-M live adapter (strategy code unchanged). */
export function createExchange(cfg) {
  if (!cfg.apiKey || !cfg.apiSecret || !cfg.passphrase) {
    throw new Error('需要 BITGET_API_KEY / BITGET_API_SECRET / BITGET_PASSPHRASE（写入 vendor/bitget-grid/.env）');
  }
  return new BitgetExchange({
    apiKey: cfg.apiKey,
    apiSecret: cfg.apiSecret,
    passphrase: cfg.passphrase,
    apiUrl: cfg.apiUrl,
    pollMs: cfg.pollMs,
  });
}
