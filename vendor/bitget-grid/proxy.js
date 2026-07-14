// Process-local proxy for Bitget HTTP (http.js / http_py.py). Undici setGlobalDispatcher is unused by http.js.
import net from 'node:net';

async function proxyReachable(proxyUrl) {
  try {
    const u = new URL(proxyUrl);
    const port = Number(u.port) || (u.protocol === 'https:' ? 443 : 80);
    return await new Promise((resolve) => {
      const s = net.connect({ host: u.hostname, port, timeout: 2500 });
      const done = (ok) => {
        try { s.destroy(); } catch { /* ignore */ }
        resolve(ok);
      };
      s.on('connect', () => done(true));
      s.on('error', () => done(false));
      s.setTimeout(2500, () => done(false));
    });
  } catch {
    return false;
  }
}

export async function setupProxy() {
  const proxy = (process.env.BITGET_PROXY || process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.EXTENDED_PROXY || '').trim();
  if (!proxy) return null;
  let reachable = false;
  for (let i = 0; i < 8; i++) {
    if (await proxyReachable(proxy)) {
      reachable = true;
      break;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  if (!reachable) {
    throw new Error('[代理] ' + proxy + ' 不可达。请检查 BITGET_PROXY / HTTPS_PROXY。');
  }
  // Ensure http.js / http_py see the same URL (Python ProxyHandler).
  process.env.BITGET_PROXY = proxy;
  process.env.HTTPS_PROXY = process.env.HTTPS_PROXY || proxy;
  console.log('[代理] Bitget HTTP 将经 Python urllib 走代理: ' + proxy);
  return proxy;
}

/** 运行中代理失效时切回直连 */
export async function disableProxyToDirect() {
  delete process.env.BITGET_PROXY;
  // leave user HTTPS_PROXY alone if they set it at OS level
  return true;
}
