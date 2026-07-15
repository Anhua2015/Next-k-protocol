import { PaperExchange } from './paper.js';
import { BitgetExchange } from './bitget.js';

/** Factory: choose adapter by mode. */
export function createExchange(cfg) {
  if (cfg.mode === 'live') {
    if (!cfg.apiKey || !cfg.apiSecret || !cfg.passphrase) {
      throw new Error('LIVE 模式需要 BITGET_API_KEY / BITGET_API_SECRET / BITGET_PASSPHRASE 环境变量。');
    }
    return new BitgetExchange({
      apiKey: cfg.apiKey,
      apiSecret: cfg.apiSecret,
      passphrase: cfg.passphrase,
      apiUrl: cfg.apiUrl,
      productType: cfg.productType,
      marginMode: cfg.marginMode,
    });
  }
  return new PaperExchange({ apiUrl: cfg.apiUrl, startBalance: cfg.startBalance, productType: cfg.productType });
}
