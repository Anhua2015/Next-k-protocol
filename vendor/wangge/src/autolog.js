// Shared ring buffer for automation events (scout + autopilot), shown on overview.
const MAX = 80;
const buffer = [];

/**
 * @param {{ source?: string, symbol?: string, type?: string, message: string, t?: number }} entry
 */
export function pushAutoLog(entry) {
  if (!entry || !entry.message) return;
  buffer.unshift({
    t: entry.t || Date.now(),
    source: entry.source || 'system',
    symbol: entry.symbol || '',
    type: entry.type || 'info',
    message: String(entry.message).slice(0, 500),
  });
  if (buffer.length > MAX) buffer.length = MAX;
}

export function getAutoLog(limit = 40) {
  return buffer.slice(0, Math.max(1, Math.min(MAX, limit)));
}

export function clearAutoLog() {
  buffer.length = 0;
}
