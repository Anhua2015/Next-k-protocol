// Lightweight crash-safe state persistence.
//
// A grid bot holds non-trivial in-memory state: its config, cumulative stats
// (volume / completed rungs / theoretical profit) and the starting balance used
// for return%. If the process restarts, all of that is lost while the REAL
// resting orders remain on the exchange — a dangerous "half-known grid".
//
// This module persists a small snapshot per exchange to a JSON file. On boot the
// server restores the snapshot (so the dashboard keeps showing cumulative stats)
// and, for any bot that was running, cancels stray orders for that market.
//
// It deliberately stores NO secrets — only public config + counters.
//
// On Railway / container hosts the app directory is ephemeral: every deploy wipes
// vendor/wangge/.state.json. Prefer DATA_DIR (or WANGGE_DATA_DIR) on a volume,
// same as binance.db — default mount is /data.
import fs from 'node:fs';
import path from 'node:path';
import { ROOT } from './config.js';

function resolveDataDir() {
  const raw = process.env.WANGGE_DATA_DIR || process.env.DATA_DIR || '';
  if (raw) return path.resolve(raw);
  return ROOT;
}

export function getDataDir() {
  return resolveDataDir();
}

function ensureDir(dir) {
  try { fs.mkdirSync(dir, { recursive: true }); } catch { /* best effort */ }
}

const DATA_DIR = resolveDataDir();
ensureDir(DATA_DIR);

const STATE_FILE = path.join(DATA_DIR, 'wangge.state.json');
const LEGACY_STATE = path.join(ROOT, '.state.json');
export const SYMBOLS_FILE = path.join(DATA_DIR, 'wangge_symbols.txt');
export const PAPER_FILE = path.join(DATA_DIR, 'wangge_paper.json');

let cache = null;
let saveTimer = null;
let paperTimer = null;

function migrateLegacyIfNeeded() {
  if (fs.existsSync(STATE_FILE)) return;
  if (!fs.existsSync(LEGACY_STATE)) return;
  try {
    fs.copyFileSync(LEGACY_STATE, STATE_FILE);
    console.log(`[持久化] 已从 ${LEGACY_STATE} 迁移到 ${STATE_FILE}`);
  } catch (e) {
    console.warn(`[持久化] 迁移旧快照失败: ${e?.message || e}`);
  }
}

/** Read the whole state file once (cached). Returns {} on any problem. */
export function loadState() {
  if (cache) return cache;
  migrateLegacyIfNeeded();
  try {
    cache = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')) || {};
  } catch {
    cache = {};
  }
  return cache;
}

/** Get a single bot's snapshot (e.g. key 'de'). */
export function loadSnapshot(key) {
  return loadState()[key] || null;
}

function flushSoon() {
  if (saveTimer) return;
  saveTimer = setTimeout(() => {
    saveTimer = null;
    try {
      ensureDir(DATA_DIR);
      const tmp = STATE_FILE + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify(cache, null, 2), 'utf8');
      fs.renameSync(tmp, STATE_FILE); // atomic replace
    } catch { /* persistence must never crash trading */ }
  }, 500);
  saveTimer.unref?.();
}

/** Persist one bot's snapshot under `key`, debounced to avoid thrashing disk. */
export function saveSnapshot(key, snapshot) {
  const state = loadState();
  state[key] = snapshot;
  cache = state;
  flushSoon();
}

/** Remove a snapshot key (e.g. after deleting a fleet symbol). */
export function deleteSnapshot(key) {
  const state = loadState();
  if (!(key in state)) return;
  delete state[key];
  cache = state;
  flushSoon();
}

/** Durable symbol list (survives redeploy when DATA_DIR is on a volume). */
export function loadPersistedSymbols() {
  try {
    const raw = fs.readFileSync(SYMBOLS_FILE, 'utf8').trim();
    if (!raw) return [];
    return raw.split(/[,;\s]+/).map((s) => s.trim()).filter(Boolean);
  } catch {
    return [];
  }
}

export function savePersistedSymbols(list) {
  try {
    ensureDir(DATA_DIR);
    const joined = [...new Set((list || []).map(String).filter(Boolean))].join(',');
    fs.writeFileSync(SYMBOLS_FILE, joined + (joined ? '\n' : ''), 'utf8');
  } catch { /* never break trading */ }
}

/** Paper book (balance / positions / resting orders). Live mode ignores this. */
export function loadPaperBook() {
  try {
    return JSON.parse(fs.readFileSync(PAPER_FILE, 'utf8')) || null;
  } catch {
    return null;
  }
}

export function savePaperBook(snapshot) {
  if (!snapshot) return;
  if (paperTimer) clearTimeout(paperTimer);
  paperTimer = setTimeout(() => {
    paperTimer = null;
    try {
      ensureDir(DATA_DIR);
      const tmp = PAPER_FILE + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify(snapshot), 'utf8');
      fs.renameSync(tmp, PAPER_FILE);
    } catch { /* never break trading */ }
  }, 400);
  paperTimer.unref?.();
}

export function deletePaperBook() {
  try { if (fs.existsSync(PAPER_FILE)) fs.unlinkSync(PAPER_FILE); } catch { /* ignore */ }
}
