/**
 * Robust HTTPS client for Bitget.
 * 1) Prefer Node https with retries (direct)
 * 2) Fall back to Python urllib (this host: Node TLS to api.bitget.com often ECONNRESET)
 * 3) When BITGET_PROXY / HTTPS_PROXY is set → Python bridge with ProxyHandler (Node undici dispatcher is unused here)
 */
import https from 'node:https';
import http from 'node:http';
import { URL } from 'node:url';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const PY_BRIDGE = path.join(path.dirname(fileURLToPath(import.meta.url)), 'http_py.py');

export function activeProxy() {
  return (process.env.BITGET_PROXY || process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.EXTENDED_PROXY || '').trim();
}

function requestOnce(method, urlStr, { headers = {}, body = null, timeoutMs = 20000 } = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(urlStr);
    const lib = u.protocol === 'http:' ? http : https;
    const req = lib.request(
      {
        protocol: u.protocol,
        hostname: u.hostname,
        port: u.port || (u.protocol === 'https:' ? 443 : 80),
        path: u.pathname + u.search,
        method,
        headers,
        timeout: timeoutMs,
        servername: u.hostname,
      },
      (res) => {
        const chunks = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          resolve({ status: res.statusCode || 0, headers: res.headers, text: Buffer.concat(chunks).toString('utf8') });
        });
      }
    );
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

function requestViaPython(method, urlStr, { headers = {}, body = null, timeoutMs = 20000, proxy = '' } = {}) {
  const payload = JSON.stringify({
    method,
    url: urlStr,
    headers,
    body,
    timeoutSec: Math.ceil(timeoutMs / 1000),
    proxy: proxy || activeProxy() || null,
  });
  const r = spawnSync('python', [PY_BRIDGE], {
    input: payload,
    encoding: 'utf8',
    maxBuffer: 8 * 1024 * 1024,
    windowsHide: true,
  });
  if (r.error) throw r.error;
  if (r.status !== 0) throw new Error(r.stderr || `python bridge exit ${r.status}`);
  const out = JSON.parse(r.stdout || '{}');
  if (out.error && !out.text) throw new Error(out.error);
  return { status: out.status || 0, text: out.text || '' };
}

let preferPython = false;

export async function httpJson(method, urlStr, { headers = {}, bodyObj = null, bodyRaw = null, timeoutMs = 20000 } = {}) {
  const body = bodyRaw != null ? bodyRaw : (bodyObj != null ? JSON.stringify(bodyObj) : null);
  const hdrs = { ...headers };
  if (body != null && !hdrs['Content-Type'] && !hdrs['content-type']) {
    hdrs['Content-Type'] = 'application/json';
  }
  if (body != null) hdrs['Content-Length'] = String(Buffer.byteLength(body));

  const proxy = activeProxy();
  // Proxy: Node requestOnce has no CONNECT agent — use Python urllib ProxyHandler.
  const forcePy = !!proxy || preferPython;

  const run = async (viaPy) => {
    const res = viaPy
      ? requestViaPython(method, urlStr, { headers: hdrs, body, timeoutMs, proxy })
      : await requestOnce(method, urlStr, { headers: hdrs, body, timeoutMs });
    let json = null;
    try {
      json = res.text ? JSON.parse(res.text) : null;
    } catch {
      throw new Error(`invalid json HTTP ${res.status}: ${String(res.text).slice(0, 200)}`);
    }
    return { status: res.status, json, text: res.text };
  };

  if (forcePy) {
    preferPython = true;
    return run(true);
  }

  let lastErr;
  for (let i = 0; i < 2; i++) {
    try {
      return await run(false);
    } catch (e) {
      lastErr = e;
      await new Promise((r) => setTimeout(r, 200 * (i + 1)));
    }
  }

  try {
    const out = await run(true);
    preferPython = true;
    if (!process.env.BITGET_HTTP_SILENT) {
      console.warn('[Bitget HTTP] Node TLS failed; using Python urllib bridge for api.bitget.com');
    }
    return out;
  } catch (e2) {
    throw lastErr || e2;
  }
}
