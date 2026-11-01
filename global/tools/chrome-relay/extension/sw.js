/*
Fleet Chrome Relay MV3 service worker.
Original implementation informed by agent-chrome-relay (aindeev, MIT, cf0f458901ea8aa0b505dfc4461246f47cc9c5dd).
No upstream source is copied. Caller JavaScript and CDP are not accepted.
The optional owner-bound saved login (login_gate.js) uses only the fixed
trusted Input.dispatchMouseEvent press/release on a freshly checked button.

Broker loopback, extension side only:
  GET  http://127.0.0.1:<port>/extension/poll
  POST http://127.0.0.1:<port>/extension/result
  POST http://127.0.0.1:<port>/extension/event
Authorization: Bearer <extension_token>
X-Profile-Id: <profile_id>
The token is never placed in the URL, audit, badge, or result body.
Result: {request_id, session_id, profile_id, status: ok|denied|unknown, tab_id?, data?}
*/

'use strict';

const STORAGE_CONFIG = 'chrome_relay_config';
const STORAGE_LATCH = 'chrome_relay_latch';
const STORAGE_BROWSER = 'chrome_relay_browser_generation';
const MAX_READ_BYTES = 4096;
const MAX_BODY_BYTES = 64 * 1024;
const MAX_TYPE_CHARS = 1000;
const MAX_REFS = 40;
const MAX_AUDIT = 50;
const MAX_INFLIGHT = 50;
const MAX_JOURNAL = 10000;
const MAX_TABS_PER_SESSION = 20;
const WAIT_ATTEMPTS = 3;
const WAIT_GAP_MS = 20;
const POLL_TIMEOUT_MS = 30000;
const DOM_TIMEOUT_MS = 5000;
const ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const GENERATION_RE = /^[a-f0-9]{32}$/;
const WIRE_RE = /^([a-f0-9]{32}):[a-f0-9]{32}$/;
const MUTATIONS = new Set(['chrome_open', 'chrome_click', 'chrome_type', 'chrome_close', 'chrome_saved_login']);
const ACTIONS = new Set([
  'chrome_attachment',
  'chrome_open',
  'chrome_read',
  'chrome_click',
  'chrome_type',
  'chrome_wait',
  'chrome_close',
  'chrome_saved_login'
]);
const LOGIN_ACTION = 'chrome_saved_login';
const LOGIN_PATH_RE = /^\/[A-Za-z0-9_./-]{0,200}$/;
const LOGIN_REASONS = new Set(['needs_owner', 'unsupported_login', 'login_route', 'form_changed']);
// Classic MV3 worker: load the fixed page function before any await.
if (typeof importScripts === 'function' && !globalThis.FleetLoginGate) {
  try { importScripts('login_gate.js'); } catch (err) { /* Saved login stays needs_owner. */ }
}
const BLOCK_WORDS = new Set([
  'authorize', 'authorise', 'consent', 'oauth', 'signin', 'login', 'password',
  'passkey', 'passcode', 'otp', 'mfa', 'totp', 'zaloguj', 'autoryzuj'
]);

let config = null;
let listenersInstalled = false;
let polling = false;
let pollBusy = false;
let toolbarBusy = false;
// A synchronous hint for the toolbar gesture only. The persisted latch is
// still authoritative and is checked again before attachment is committed.
let toolbarState = null;
let runtimeEpoch = 0;
let interrupted = true;
let storageFailed = false;
let policyChanged = false;
let latchSeen = false;
let browserResetPending = false;
const memory = {
  tabs: new Map(),
  poisoned: new Set(),
  generations: new Map(),
  authorizedOrigin: '',
  manualId: 0
};
const auditLog = [];

function browser() {
  const api = globalThis.chrome;
  if (!api) throw new Error('chrome_missing');
  return api;
}

function delay(ms) {
  return new Promise(function (resolve) {
    setTimeout(resolve, ms);
  });
}

function byteLength(text) {
  return new TextEncoder().encode(String(text)).length;
}

function foldLabel(value) {
  return String(value || '')
    .normalize('NFKC')
    .toLowerCase()
    .normalize('NFD')
    .replace(/\p{M}/gu, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

function labelBlocked(name) {
  const folded = foldLabel(name);
  if (!folded) return false;
  if (/\b(sign in|log in|grant access|allow access|approve permissions|zezwol na dostep)\b/.test(folded)) return true;
  return folded.split(' ').some(function (word) { return BLOCK_WORDS.has(word); });
}

function redactText(text) {
  return String(text || '')
    .replace(/\b(?:api[_-]?key|secret|token|password|passwd|pwd|authorization|credential)\b\s*[:=]\s*['"]?[^\s'"]+/gi, '[redacted]')
    .replace(/\bbearer\s+[a-z0-9\-._~+/]+=*/gi, '[redacted]')
    .replace(/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}/g, '[redacted]')
    .replace(/\b(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9+/]{40,}={0,2})\b/g, '[redacted]');
}

function safeId(value) {
  if (typeof value !== 'number' || !Number.isInteger(value)) return 0;
  if (value <= 0 || value > 2147483647) return 0;
  return value;
}

function plainObject(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

function dangerousKey(key) {
  return key === '__proto__' || key === 'prototype' || key === 'constructor';
}

function isLoopback(hostname) {
  return hostname === '127.0.0.1' || hostname === 'localhost';
}

function parseUrl(value) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 2048) return null;
  if (/\s/.test(value)) return null;
  let url;
  try {
    url = new URL(value);
  } catch (err) {
    return null;
  }
  if (url.username || url.password) return null;
  return url;
}

function isCredentialRoute(value) {
  const url = typeof value === 'string' ? parseUrl(value) : value;
  if (!url || !url.hostname) return false;
  const host = String(url.hostname).toLowerCase();
  const path = String(url.pathname || '').toLowerCase();
  if (
    host === 'accounts.google.com' ||
    host === 'login.microsoftonline.com' ||
    host === 'login.live.com' ||
    host === 'appleid.apple.com'
  ) {
    return true;
  }
  if (/(^|\.)auth0\.com$/.test(host) || /(^|\.)okta\.com$/.test(host)) return true;
  if (/\/(oauth2?|authorize|consent|signin|sign-in|login|log-in|saml|passkey|mfa|challenge)(\/|$)/.test(path)) {
    return true;
  }
  if (/[?&](client_id|redirect_uri|response_type)=/.test(url.search || '')) return true;
  return false;
}

// Omit the complete address on doubt: redacting it could create a different
// actionable route. This same fixed policy is used inside the isolated reader.
function safeNavigationUrl(value, origin, base) {
  if (typeof value !== 'string' || !value || value.length > 2048 || /\s/.test(value)) return '';
  try {
    const url = base ? new URL(value, base) : new URL(value);
    if (url.username || url.password || url.origin !== origin || !['https:', 'http:'].includes(url.protocol) || url.href.length > 2048) return '';
    let decoded = url.href;
    for (let i = 0; i < 3; i += 1) {
      const next = decodeURIComponent(decoded);
      if (next === decoded) break;
      decoded = next;
    }
    if (/%[a-f0-9]{2}|\s/i.test(decoded) || redactText(decoded) !== decoded) return '';
    if (/(?:^|[^a-z0-9])(?:api[_-]?key|secret|(?:access[_-]?|refresh[_-]?|id[_-]?)?token|password|passwd|pwd|authorization|credential|bearer|session(?:[_-]?id)?|auth|code|signature|client[_-]?id|redirect[_-]?uri|response[_-]?type|consent|oauth2?|signin|sign-in|login|log-in|saml|passkey|mfa|challenge)(?=$|[^a-z0-9])/i.test(decoded)) return '';
    if (/\b(?:sk-|gh[pousr]_|github_pat_|AKIA)[a-z0-9_-]{8,}/i.test(decoded)) return '';
    return url.href;
  } catch (err) { return ''; }
}

function normalizeOrigin(value) {
  if (typeof value !== 'string' || value.length > 200) return null;
  let url;
  try {
    url = new URL(value);
  } catch (err) {
    return null;
  }
  if (url.username || url.password || url.search || url.hash) return null;
  if (url.pathname !== '/' && url.pathname !== '') return null;
  if (url.protocol === 'https:') return url.origin;
  if (url.protocol === 'http:' && isLoopback(url.hostname)) return url.origin;
  return null;
}

function autocompleteBlocked(value) {
  const text = String(value || '').toLowerCase();
  return text.includes('password') || text.includes('one-time') || text.includes('webauthn');
}

function defaultLatch() {
  return {
    schema: 2,
    generation: '',
    stop: true,
    authorized: false,
    technical_block: true,
    profile_id: '',
    authorized_origin: '',
    manual_tab_id: 0,
    sessions: Object.create(null),
    inflight: [],
    completed: Object.create(null),
    completed_order: [],
    disconnect_notified: false
  };
}

function copySessions(value) {
  const out = Object.create(null);
  if (!value || typeof value !== 'object') return out;
  for (const key of Object.keys(value)) {
    if (Object.keys(out).length >= 100) break;
    if (!ID_RE.test(key)) continue;
    const ids = Array.isArray(value[key]) ? value[key] : [];
    const clean = [];
    for (const id of ids) {
      const parsed = safeId(id);
      if (parsed && clean.indexOf(parsed) === -1) clean.push(parsed);
      if (clean.length >= MAX_TABS_PER_SESSION) break;
    }
    if (clean.length) out[key] = clean;
  }
  return out;
}

function normalizeLatch(value) {
  const latch = defaultLatch();
  if (value === undefined) return latch;
  // Never repair a corrupt active journal by dropping IDs: that permits replay.
  function invalid() { throw new Error('journal'); }
  if (!plainObject(value) || value.schema !== 2) invalid();
  if (!argsExact(value, Object.keys(latch))) invalid();
  if (typeof value.stop !== 'boolean' || typeof value.authorized !== 'boolean' || typeof value.technical_block !== 'boolean' || typeof value.disconnect_notified !== 'boolean') invalid();
  const unbound = value.generation === '' && value.stop === true && value.authorized === false && value.manual_tab_id === 0 && plainObject(value.sessions) && Object.keys(value.sessions).length === 0;
  if (!GENERATION_RE.test(value.generation) && !unbound) invalid();
  latch.generation = value.generation;
  if (!Array.isArray(value.inflight) || !plainObject(value.completed) || !Array.isArray(value.completed_order)) invalid();
  const keys = Object.keys(value.completed);
  if (unbound && (keys.length || value.inflight.length || value.completed_order.length)) invalid();
  if (keys.length + value.inflight.length > MAX_JOURNAL || value.inflight.length > MAX_INFLIGHT || value.completed_order.length > MAX_JOURNAL) invalid();
  latch.stop = value.stop === true;
  latch.authorized = value.authorized === true;
  latch.technical_block = value.technical_block !== false;
  latch.profile_id = typeof value.profile_id === 'string' && ID_RE.test(value.profile_id) ? value.profile_id : '';
  latch.authorized_origin = normalizeOrigin(value.authorized_origin) || '';
  latch.manual_tab_id = safeId(value.manual_tab_id);
  latch.sessions = copySessions(value.sessions);
  latch.disconnect_notified = value.disconnect_notified === true;
  const reserved = new Set();
  for (const item of value.inflight) {
      if (!plainObject(item) || !wireMatches(item.request_id, latch.generation) || typeof item.session_id !== 'string' || !ID_RE.test(item.session_id) || dangerousKey(item.session_id)) invalid();
      if (!MUTATIONS.has(item.action) || reserved.has(item.request_id) || Object.hasOwn(value.completed, item.request_id)) invalid();
      if (item.tab_id !== 0 && !safeId(item.tab_id)) invalid();
      if (item.mutation !== true || typeof item.dispatched !== 'boolean' || (item.claimed !== undefined && typeof item.claimed !== 'boolean') || !Number.isSafeInteger(item.epoch)) invalid();
      reserved.add(item.request_id);
      latch.inflight.push({
        request_id: item.request_id,
        session_id: item.session_id,
        tab_id: safeId(item.tab_id),
        action: item.action,
        mutation: true,
        dispatched: item.dispatched === true,
        claimed: item.claimed === true,
        epoch: Number.isInteger(item.epoch) ? item.epoch : -1
      });
  }
  const completed = value.completed;
  const order = value.completed_order;
  const seen = new Set();
  for (const requestId of order) {
    if (!wireMatches(requestId, latch.generation) || !Object.hasOwn(completed, requestId)) invalid();
    if (seen.has(requestId)) continue;
    const rec = completed[requestId];
    if (!plainObject(rec) || !argsExact(rec, ['status', 'session_id', 'action', 'length', 'reason', 'tab_id'])) invalid();
    if (!['ok', 'denied', 'unknown'].includes(rec.status) || typeof rec.session_id !== 'string' || !ID_RE.test(rec.session_id) || dangerousKey(rec.session_id) || !MUTATIONS.has(rec.action)) invalid();
    if (!Number.isInteger(rec.length) || rec.length < 0 || rec.length > MAX_TYPE_CHARS || typeof rec.reason !== 'string' || !/^[a-z_]{0,64}$/.test(rec.reason) || (rec.tab_id !== 0 && !safeId(rec.tab_id))) invalid();
    latch.completed[requestId] = {
      status: rec.status,
      session_id: typeof rec.session_id === 'string' && ID_RE.test(rec.session_id) ? rec.session_id : '',
      action: ACTIONS.has(rec.action) ? rec.action : '',
      length: Number.isInteger(rec.length) && rec.length >= 0 && rec.length <= MAX_TYPE_CHARS ? rec.length : 0,
      reason: typeof rec.reason === 'string' ? rec.reason.slice(0, 64) : '',
      tab_id: safeId(rec.tab_id)
    };
    latch.completed_order.push(requestId);
    seen.add(requestId);
  }
  if (seen.size !== keys.length) invalid();
  return latch;
}

function wireMatches(requestId, generation) {
  const matched = typeof requestId === 'string' && WIRE_RE.exec(requestId);
  return !!matched && matched[1] === generation;
}

async function sha256Hex(text) {
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), function (byte) { return byte.toString(16).padStart(2, '0'); }).join('');
}

async function policyGeneration(value) {
  // Managed grants bind scope into the generation; legacy input is unchanged.
  const fields = value.managed === true
    ? ['managed/1', value.grant_id, value.profile_id, value.extension_token, value.allowed_origins.slice().sort(), value.allowed_actions.slice().sort(), value.synthetic, value.persistent ? null : Math.floor(value.expires_at)]
    : [value.profile_id, value.extension_token, Math.floor(value.expires_at)];
  // A saved-login binding joins the generation only when present.
  if (value.saved_login) fields.push(await sha256Hex(JSON.stringify(['saved_login/1', value.saved_login.origin, value.saved_login.pages.slice().sort(), value.saved_login.posts.slice().sort()])));
  return (await sha256Hex(JSON.stringify(fields))).slice(0, 32);
}

function validSavedLogin(value, origins) {
  // Exact owner binding: one HTTPS origin plus login-page and form-POST paths.
  if (!plainObject(value) || !argsExact(value, ['origin', 'pages', 'posts'])) return null;
  const origin = normalizeOrigin(value.origin);
  if (!origin || origin !== value.origin || new URL(origin).protocol !== 'https:' || origins.indexOf(origin) === -1 || isCredentialRoute(new URL(origin + '/'))) return null;
  const out = { origin: origin };
  for (const key of ['pages', 'posts']) {
    const items = value[key];
    if (!Array.isArray(items) || items.length < 1 || items.length > 8 || new Set(items).size !== items.length) return null;
    if (items.some(function (item) { return typeof item !== 'string' || !LOGIN_PATH_RE.test(item) || item.indexOf('..') !== -1 || item.indexOf('//') !== -1 || /\/(oauth2?|authorize|authorise|consent|saml|passkey|mfa|challenge)(\/|$)/i.test(item); })) return null;
    out[key] = items.slice().sort();
  }
  return out;
}

const MANAGED_SID_RE = /^c-[a-f0-9]{32}$/;

function sessionAllowed(sessionId) {
  // Managed clients are registered by the broker's controller; the extension
  // only accepts the broker-issued SID shape and never a caller-chosen list.
  if (!config || typeof sessionId !== 'string') return false;
  return config.managed === true ? MANAGED_SID_RE.test(sessionId) : config.session_ids.includes(sessionId);
}

function validateManagedConfig(value) {
  const required = ['schema', 'mode', 'grant_id', 'profile_id', 'extension_token', 'broker_base', 'allowed_origins', 'allowed_actions', 'synthetic'];
  const keys = Object.keys(value);
  const persistent = Object.hasOwn(value, 'persistent');
  if (persistent === Object.hasOwn(value, 'expires_at')) return null;
  if (keys.length !== required.length + 1 + (Object.hasOwn(value, 'saved_login') ? 1 : 0) || !required.every(function (key) { return Object.hasOwn(value, key); })) return null;
  if (value.mode !== 'managed' || typeof value.grant_id !== 'string' || !ID_RE.test(value.grant_id)) return null;
  if (persistent && value.persistent !== true) return null;
  const legacy = Object.assign({}, value, { schema: 1, session_ids: ['managed'], expires_at: persistent ? 1 : value.expires_at });
  delete legacy.mode;
  delete legacy.grant_id;
  delete legacy.persistent;
  const base = validateConfig(legacy);
  if (!base) return null;
  delete base.session_ids;
  return Object.assign(base, {
    schema: 2,
    managed: true,
    grant_id: value.grant_id,
    persistent: persistent,
    expires_at: persistent ? Infinity : base.expires_at
  });
}

function validateConfig(value) {
  if (!plainObject(value)) return null;
  if (value.schema === 2) return validateManagedConfig(value);
  const fields = ['schema', 'profile_id', 'extension_token', 'broker_base', 'allowed_origins', 'allowed_actions', 'session_ids', 'synthetic', 'expires_at'];
  if (Object.hasOwn(value, 'saved_login')) fields.push('saved_login');
  if (!argsExact(value, fields)) return null;
  if (value.schema !== 1) return null;
  if (typeof value.profile_id !== 'string' || !ID_RE.test(value.profile_id)) return null;
  if (typeof value.extension_token !== 'string') return null;
  if (value.extension_token.length < 16 || value.extension_token.length > 256) return null;
  if (!/^[\x21-\x7e]+$/.test(value.extension_token)) return null;
  if (typeof value.broker_base !== 'string' || !validBroker(value.broker_base)) return null;
  if (!Array.isArray(value.allowed_origins) || !Array.isArray(value.allowed_actions)) return null;
  if (!Array.isArray(value.session_ids) || value.session_ids.length < 1 || value.session_ids.length > 100) return null;
  if (value.session_ids.some(function (sid) { return typeof sid !== 'string' || !ID_RE.test(sid); })) return null;
  if (new Set(value.session_ids).size !== value.session_ids.length) return null;
  if (typeof value.synthetic !== 'boolean') return null;
  const expires = typeof value.expires_at === 'number' ? value.expires_at : Date.parse(value.expires_at);
  if (!Number.isSafeInteger(Math.floor(expires)) || expires <= 0) return null;
  const origins = [];
  for (const origin of value.allowed_origins) {
    const normalized = normalizeOrigin(origin);
    if (!normalized || origins.indexOf(normalized) !== -1) return null;
    if (!value.synthetic && new URL(normalized).protocol !== 'https:') return null;
    origins.push(normalized);
  }
  if (!origins.length || origins.length > 20) return null;
  const actions = [];
  for (const action of value.allowed_actions) {
    if (!ACTIONS.has(action) || actions.indexOf(action) !== -1) return null;
    actions.push(action);
  }
  if (!actions.length) return null;
  const hasLogin = Object.hasOwn(value, 'saved_login');
  const login = hasLogin ? validSavedLogin(value.saved_login, origins) : null;
  if ((hasLogin && !login) || hasLogin !== actions.includes(LOGIN_ACTION)) return null;
  const normalized = {
    schema: 1,
    profile_id: value.profile_id,
    extension_token: value.extension_token,
    broker_base: value.broker_base.replace(/\/$/, ''),
    allowed_origins: origins,
    allowed_actions: actions,
    session_ids: value.session_ids.slice(),
    synthetic: value.synthetic,
    expires_at: expires
  };
  if (login) normalized.saved_login = login;
  return normalized;
}

function validBroker(base) {
  let url;
  try {
    url = new URL(base);
  } catch (err) {
    return false;
  }
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port) return false;
  if (url.username || url.password || url.search || url.hash) return false;
  if (url.pathname !== '' && url.pathname !== '/') return false;
  return true;
}

function endpoint(pathname) {
  if (!config || !/^\/extension\/(poll|result|event)$/.test(pathname)) return '';
  const url = new URL(config.broker_base);
  if (!validBroker(url.origin)) return '';
  url.pathname = pathname;
  url.search = '';
  url.hash = '';
  return url.toString();
}

function originGate(value, savedLoginPage) {
  const url = typeof value === 'string' ? parseUrl(value) : value;
  if (!url) return { ok: false, reason: 'origin' };
  const approvedLogin = savedLoginPage === true && config && config.saved_login && loginPage(url, config.saved_login);
  if (isCredentialRoute(url) && !approvedLogin) return { ok: false, reason: 'oauth', url: url };
  if (!config) return { ok: false, reason: 'origin' };
  if (url.protocol === 'https:') {
    if (config.allowed_origins.indexOf(url.origin) === -1) return { ok: false, reason: 'origin', url: url };
    return { ok: true, url: url };
  }
  if (url.protocol === 'http:' && config.synthetic === true && isLoopback(url.hostname)) {
    if (config.allowed_origins.indexOf(url.origin) === -1) return { ok: false, reason: 'origin', url: url };
    return { ok: true, url: url };
  }
  return { ok: false, reason: 'origin', url: url };
}

function attachedOriginAllowed(origin, ownerOrigin) {
  return !!config && config.allowed_origins.includes(origin) &&
    (config.managed === true || origin === ownerOrigin);
}

function expired() {
  return !config || Date.now() >= config.expires_at;
}

function audit(entry) {
  const events = new Set(['badge', 'result', 'event', 'recover', 'operation', 'toolbar', 'popup', 'boot']);
  const statuses = new Set(['ok', 'denied', 'unknown', 'post_failed', 'owner_stop', 'owner_attach', 'technical_disconnect', 'manual_closed', 'expired']);
  const safe = {
    event: events.has(entry.event) ? entry.event : 'operation',
    op: ACTIONS.has(entry.op) || ['stop', 'attach', 'disconnect'].includes(entry.op) ? entry.op : '',
    status: statuses.has(entry.status) ? entry.status : 'denied',
    request_id: typeof entry.request_id === 'string' && ID_RE.test(entry.request_id) ? entry.request_id : '',
    session_id: typeof entry.session_id === 'string' && ID_RE.test(entry.session_id) ? entry.session_id : '',
    tab_id: safeId(entry.tab_id),
    count: Number.isInteger(entry.count) ? entry.count : 0
  };
  auditLog.push(safe);
  if (auditLog.length > MAX_AUDIT) auditLog.shift();
}

function getAudit() {
  return auditLog.map(function (entry) { return Object.assign({}, entry); });
}

async function storageGet(key) {
  try {
    const got = await browser().storage.local.get(key);
    if (!got || typeof got !== 'object') throw new Error('storage');
    return got[key];
  } catch (err) { storageFault(); throw err; }
}

async function storageSet(key, value) {
  try {
    const encoded = JSON.parse(JSON.stringify(value));
    await browser().storage.local.set({ [key]: encoded });
  } catch (err) { storageFault(); throw err; }
}

const nativeAborts = new Set();
function abortNative() {
  for (const abort of Array.from(nativeAborts)) abort();
  nativeAborts.clear();
}

// STOP aborts a pending native saved-login step instead of awaiting it.
function interruptibleNative(promise) {
  let abort;
  const stopped = new Promise(function (resolve, reject) { abort = function () { reject(new Error('stopped')); }; });
  nativeAborts.add(abort);
  if (interrupted) abort();
  return Promise.race([boundedNative(promise), stopped]).finally(function () { nativeAborts.delete(abort); });
}

function invalidateRuntime() {
  interrupted = true;
  runtimeEpoch += 1;
  abortNative();
  toolbarState = { stop: true, authorized: false };
  memory.tabs.clear();
  memory.generations.clear();
  memory.authorizedOrigin = '';
}

function storageFault() {
  storageFailed = true;
  invalidateRuntime();
  setBadge('STOP').catch(function () {});
}

let latchTail = Promise.resolve();
function exclusive(fn) {
  const run = latchTail.then(fn, fn);
  latchTail = run.then(function () {}, function () {});
  return run;
}

async function mutateLatch(mutator) {
  return exclusive(async function () {
    try {
      const saved = await storageGet(STORAGE_LATCH);
      if (saved === undefined && latchSeen) throw new Error('journal');
      const retired = config && saved && GENERATION_RE.test(saved.generation) && saved.generation !== config.generation;
      const latch = retired ? defaultLatch() : normalizeLatch(saved);
      if (retired || !latch.generation) {
        invalidateRuntime();
        latch.generation = config ? config.generation : '';
      }
      if (storageFailed || policyChanged) {
        latch.stop = true;
        latch.authorized = false;
        latch.technical_block = true;
        cancelInflight(latch, 'storage');
      }
      const returned = mutator(latch);
      await storageSet(STORAGE_LATCH, latch);
      latchSeen = true;
      storageFailed = false;
      policyChanged = false;
      toolbarState = { stop: latch.stop, authorized: latch.authorized };
      return returned;
    } catch (err) { storageFault(); throw err; }
  });
}

async function setBadge(text) {
  const label = text === 'ON' || text === 'STOP' ? text : '';
  try {
    await browser().action.setBadgeText({ text: label });
    await browser().action.setBadgeBackgroundColor({
      color: label === 'ON' ? '#0B7A3B' : '#9B1C1C'
    });
  } catch (err) {
    audit({ event: 'badge', status: 'unknown', count: 0 });
  }
}

async function badgeFromLatch() {
  const state = await mutateLatch(function (latch) {
    return { stop: latch.stop, authorized: latch.authorized, technical_block: latch.technical_block };
  });
  await setBadge(state.authorized && !state.stop && !state.technical_block && !expired() ? 'ON' : 'STOP');
}

function packResult(result) {
  let body = JSON.stringify(result);
  if (byteLength(body) <= MAX_BODY_BYTES) return body;
  const shrunk = {
    request_id: String(result.request_id || '').slice(0, 128),
    session_id: String(result.session_id || '').slice(0, 128),
    profile_id: String(result.profile_id || '').slice(0, 128),
    status: result.status === 'unknown' ? 'unknown' : 'denied',
    data: { reason: 'bound', truncated: true }
  };
  if (safeId(result.tab_id)) shrunk.tab_id = result.tab_id;
  body = JSON.stringify(shrunk);
  return body;
}

async function brokerFetch(pathname, method, body) {
  const url = endpoint(pathname);
  if (!url || !config) throw new Error('broker');
  const headers = {
    'Accept': 'application/json',
    'Authorization': 'Bearer ' + config.extension_token,
    'X-Profile-Id': config.profile_id
  };
  if (body != null) headers['Content-Type'] = 'application/json';
  const controller = new AbortController();
  const timer = setTimeout(function () { controller.abort(); }, POLL_TIMEOUT_MS);
  try {
    return await fetch(url, {
      method: method,
      headers: headers,
      body: body == null ? undefined : body,
      redirect: 'error',
      credentials: 'omit',
      cache: 'no-store',
      signal: controller.signal
    });
  } finally {
    clearTimeout(timer);
  }
}

async function readBounded(response) {
  if (response.headers && !/^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(response.headers.get('content-type') || '')) return { ok: false, reason: 'content_type' };
  const declared = response.headers && response.headers.get('content-length');
  if (declared && (!/^\d+$/.test(declared) || Number(declared) > MAX_BODY_BYTES)) return { ok: false, reason: 'bound' };
  let buffer;
  if (response.body && typeof response.body.getReader === 'function') {
    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        size += chunk.value.byteLength;
        if (size > MAX_BODY_BYTES) {
          await reader.cancel();
          return { ok: false, reason: 'bound' };
        }
        chunks.push(chunk.value);
      }
    } finally {
      reader.releaseLock();
    }
    buffer = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { buffer.set(chunk, offset); offset += chunk.byteLength; }
  } else {
    buffer = new Uint8Array(await response.arrayBuffer());
    if (buffer.byteLength > MAX_BODY_BYTES) return { ok: false, reason: 'bound' };
  }
  try {
    const text = new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(buffer));
    return { ok: true, text: text };
  } catch (err) {
    return { ok: false, reason: 'utf8' };
  }
}

async function postJson(pathname, payload) {
  const body = packResult(payload);
  if (byteLength(body) > MAX_BODY_BYTES) return false;
  const response = await brokerFetch(pathname, 'POST', body);
  return response.status === 200 || response.status === 204;
}

async function postResult(result) {
  const payload = {
    request_id: result.request_id,
    session_id: result.session_id,
    profile_id: config ? config.profile_id : '',
    status: result.status
  };
  if (safeId(result.tab_id)) payload.tab_id = result.tab_id;
  if (result.data && typeof result.data === 'object') payload.data = result.data;
  audit({
    event: 'result',
    op: result.op || '',
    status: result.status,
    request_id: result.request_id,
    session_id: result.session_id,
    tab_id: result.tab_id || 0,
    count: result.data && Array.isArray(result.data.refs) ? result.data.refs.length : 0
  });
  try {
    if (!(await postJson('/extension/result', payload))) await markDisconnect();
  } catch (err) {
    audit({ event: 'result', op: result.op || '', status: 'post_failed', request_id: result.request_id, count: 0 });
    await markDisconnect();
  }
  return payload;
}

async function postEvent(type, extra) {
  const payload = {
    type: type,
    profile_id: config ? config.profile_id : '',
    session_id: extra && extra.session_id ? extra.session_id : '',
    tab_id: extra && safeId(extra.tab_id) ? extra.tab_id : 0,
    reason: extra && extra.reason ? String(extra.reason).slice(0, 64) : ''
  };
  audit({ event: 'event', op: type, status: payload.reason, session_id: payload.session_id, tab_id: payload.tab_id, count: 1 });
  try {
    await postJson('/extension/event', payload);
  } catch (err) {
    audit({ event: 'event', op: type, status: 'post_failed', count: 0 });
  }
}

function rememberCompleted(latch, requestId, record) {
  if (!wireMatches(requestId, latch.generation)) return;
  if (latch.completed[requestId]) return;
  // Every mutation already reserved capacity. STOP converts reservations to
  // terminal records without growing the generation's journal or evicting IDs.
  if (!latch.completed[requestId]) latch.completed_order.push(requestId);
  latch.completed[requestId] = {
    status: record.status,
    session_id: record.session_id || '',
    action: record.action || '',
    length: Number.isInteger(record.length) ? record.length : 0,
    reason: record.reason || '',
    tab_id: safeId(record.tab_id)
  };
}

async function loadConfig() {
  return exclusive(async function () {
    const next = validateConfig(await storageGet(STORAGE_CONFIG));
    if (next) next.generation = await policyGeneration(next);
    if (config && JSON.stringify(config) !== JSON.stringify(next)) {
      invalidateRuntime();
      policyChanged = true;
    }
    if (!config || JSON.stringify(config) !== JSON.stringify(next)) config = next;
    return config;
  });
}

function cancelInflight(latch, reason) {
  const items = latch.inflight.slice();
  latch.inflight = [];
  for (const item of items) {
    rememberCompleted(latch, item.request_id, {
      status: item.dispatched ? 'unknown' : 'denied',
      reason: reason,
      session_id: item.session_id,
      action: item.action,
      tab_id: item.tab_id
    });
  }
  return items;
}

async function stopLatch(reason) {
  interrupted = true;
  runtimeEpoch += 1;
  abortNative();
  await mutateLatch(function (latch) {
    latch.stop = true;
    cancelInflight(latch, reason);
  });
  memory.tabs.clear();
  await setBadge('STOP');
}

async function recover() {
  try {
  interrupted = true;
  runtimeEpoch += 1;
  await loadConfig();
  if (!config) {
    await stopLatch('profile');
    audit({ event: 'recover', status: 'denied', count: 0 });
    return { ok: false };
  }
  // storage.session survives worker termination, but not browser restart.
  if (config.managed === true) {
    const session = browser().storage.session;
    if (!session || typeof session.get !== 'function' || typeof session.set !== 'function') throw new Error('storage');
    const marker = await boundedNative(session.get(STORAGE_BROWSER));
    if (!plainObject(marker)) throw new Error('storage');
    const generation = marker[STORAGE_BROWSER];
    if (generation !== undefined && (typeof generation !== 'string' || !GENERATION_RE.test(generation))) throw new Error('storage');
    browserResetPending = browserResetPending || generation !== config.generation;
  }
  const pending = await mutateLatch(function (latch) {
    const items = cancelInflight(latch, 'worker_restart');
    latch.technical_block = true;
    if (config.managed === true && browserResetPending) {
      latch.sessions = Object.create(null);
      latch.manual_tab_id = 0;
    }
    if (latch.profile_id !== config.profile_id || !config.allowed_origins.includes(latch.authorized_origin)) latch.stop = true;
    return items.filter(function (item) { return item.mutation; });
  });
  memory.tabs.clear();
  memory.poisoned.clear();
  memory.generations.clear();
  memory.authorizedOrigin = '';
  memory.manualId = await mutateLatch(function (latch) { return latch.manual_tab_id; });
  await badgeFromLatch();
  for (const item of pending) {
    const status = item.dispatched ? 'unknown' : 'denied';
    await postResult({
      request_id: item.request_id,
      session_id: item.session_id,
      status: status,
      op: item.action,
      tab_id: item.tab_id,
      data: { reason: item.dispatched ? 'worker_restart' : 'cancelled' }
    });
  }
  audit({ event: 'recover', status: 'ok', count: pending.length });
  return { ok: true };
  } catch (err) {
    storageFault();
    return { ok: false, reason: 'storage' };
  }
}

function sessionOwns(latch, sessionId, tabId) {
  if (!sessionAllowed(sessionId) || tabId === latch.manual_tab_id) return false;
  const list = latch.sessions[sessionId] || [];
  return list.indexOf(tabId) !== -1;
}

function boundedNative(promise) {
  let timer;
  const timeout = new Promise(function (resolve, reject) {
    timer = setTimeout(function () { reject(new Error('timeout')); }, DOM_TIMEOUT_MS);
  });
  return Promise.race([promise, timeout]).finally(function () { clearTimeout(timer); });
}

async function hasPermission(origin) {
  try {
    return await boundedNative(browser().permissions.contains({ origins: [origin + '/*'] })) === true;
  } catch (err) {
    return false;
  }
}

async function runDom(access, command) {
  const response = await boundedNative(browser().scripting.executeScript({
    target: { tabId: access.tabId },
    func: fixedDom,
    args: [Object.assign({}, command, { origin: access.url.origin, deadline: Math.min(config.expires_at, Date.now() + DOM_TIMEOUT_MS) })]
  }));
  if (!Array.isArray(response) || !response[0] || response[0].result == null) {
    return { ok: false, reason: 'unknown' };
  }
  if (byteLength(JSON.stringify(response[0].result)) > MAX_BODY_BYTES) return { ok: false, reason: 'bound' };
  return { ok: true, result: response[0].result };
}

function publishRefs(access, elements) {
  const tabId = access.tabId;
  const entropy = new Uint32Array(2);
  globalThis.crypto.getRandomValues(entropy);
  const generation = ((BigInt(entropy[0]) << 32n) | BigInt(entropy[1])).toString();
  memory.generations.set(tabId, generation);
  const stored = Object.create(null);
  const refs = [];
  let index = 0;
  for (const element of elements) {
    if (!element || typeof element !== 'object') continue;
    if (typeof element.path !== 'string' || element.path.length > 2048 || typeof element.tag !== 'string' || element.tag.length > 32 || typeof element.role !== 'string' || element.role.length > 32 || typeof element.snapshot !== 'string' || element.snapshot.length > 80 || typeof element.node_id !== 'string' || element.node_id.length > 10) continue;
    const name = redactText(element.name || '').slice(0, 120);
    const blocked = element.blocked === true || element.ambiguous === true || labelBlocked(name) || name !== String(element.name || '').slice(0, 120);
    if (blocked) continue;
    if (refs.length >= MAX_REFS) break;
    const ref = 'r' + generation + '_' + index;
    index += 1;
    stored[ref] = {
      path: typeof element.path === 'string' ? element.path : '',
      tag: String(element.tag || ''),
      role: String(element.role || ''),
      name: String(element.name || '').slice(0, 120),
      inputType: String(element.inputType || ''),
      autocomplete: String(element.autocomplete || ''),
      snapshot: typeof element.snapshot === 'string' ? element.snapshot : '',
      node_id: typeof element.node_id === 'string' ? element.node_id : ''
    };
    const item = { ref: ref, role: stored[ref].role, name: name };
    const url = element.tag === 'a' ? safeNavigationUrl(element.url, access.url.origin) : '';
    if (url) item.url = url;
    refs.push(item);
  }
  const fitted = fitRead('', refs, false);
  const published = new Set(fitted.refs.map(function (item) { return item.ref; }));
  for (const ref of Object.keys(stored)) if (!published.has(ref)) delete stored[ref];
  memory.tabs.set(tabId, { generation: generation, refs: stored });
  return fitted;
}

function fitRead(excerpt, refs, forceTruncated, diagnostics) {
  let text = redactText(excerpt);
  // Keep a useful prefix when menu URLs consume the result budget. Unicode
  // code points avoid splitting a surrogate pair while fitting UTF-8 bytes.
  let textPoints = Array.from(text);
  const minimumTextPoints = Math.min(textPoints.length, 256);
  let visible = refs.slice();
  const observed = diagnostics ? Object.assign({}, diagnostics) : null;
  if (observed && forceTruncated === true && observed.reader_status === 'ok') observed.reader_status = 'partial';
  let payload = { excerpt: text, refs: visible, truncated: forceTruncated === true };
  if (observed) payload.diagnostics = observed;
  while (byteLength(JSON.stringify(payload)) > MAX_READ_BYTES && (text.length > 0 || visible.length > 0)) {
    if (observed && observed.reader_status === 'ok') observed.reader_status = 'partial';
    if (textPoints.length > minimumTextPoints) {
      textPoints = textPoints.slice(0, Math.max(minimumTextPoints, Math.floor(textPoints.length * 0.75) - 1));
      text = textPoints.join('');
    } else if (visible.length > 0) visible = visible.slice(0, -1);
    else {
      textPoints = textPoints.slice(0, Math.max(0, textPoints.length - 1));
      text = textPoints.join('');
    }
    payload = { excerpt: text, refs: visible, truncated: true };
    if (observed) payload.diagnostics = observed;
  }
  return payload;
}

async function handleOperation(raw) {
  try {
    return await dispatchOperation(raw);
  } catch (err) {
    const requestId = raw && typeof raw.request_id === 'string' ? raw.request_id.slice(0, 128) : '';
    const sessionId = raw && typeof raw.session_id === 'string' ? raw.session_id.slice(0, 128) : '';
    if (storageFailed) {
      // Do not recurse through failed journal I/O, or report successful effects.
      return { request_id: requestId, session_id: sessionId, profile_id: config ? config.profile_id : '', status: MUTATIONS.has(raw && raw.action) ? 'unknown' : 'denied', data: { reason: 'storage' } };
    }
    if (ID_RE.test(requestId) && ID_RE.test(sessionId)) {
      if (raw && MUTATIONS.has(raw.action)) {
        const claimed = await claimMutation(requestId, null);
        return finishMutation(raw, claimed.claimed, 'unknown', 'unknown', safeId(raw.tab_id));
      }
      return postResult({
        request_id: requestId,
        session_id: sessionId,
        status: 'unknown',
        op: '',
        data: { reason: 'unknown' }
      });
    }
    audit({ event: 'operation', status: 'unknown', count: 0 });
    return { status: 'unknown' };
  }
}

function validateEnvelope(raw) {
  const identity = raw && typeof raw.request_id === 'string' && ID_RE.test(raw.request_id) && typeof raw.session_id === 'string' && ID_RE.test(raw.session_id)
    ? { request_id: raw.request_id, session_id: raw.session_id } : {};
  function invalid(reason) { return Object.assign({ ok: false, reason: reason }, identity); }
  if (!plainObject(raw)) return invalid('unknown_field');
  if (byteLength(JSON.stringify(raw)) > MAX_BODY_BYTES) return invalid('bound');
  const keys = Object.keys(raw);
  if (keys.length > 6) return invalid('unknown_field');
  for (const key of keys) {
    if (dangerousKey(key)) return invalid('unknown_field');
  }
  const allowed = { request_id: true, session_id: true, profile_id: true, tab_id: true, action: true, args: true };
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(allowed, key)) return invalid('unknown_field');
  }
  if (!identity.request_id) return invalid('unknown_field');
  if (!config || !wireMatches(raw.request_id, config.generation)) return invalid('generation');
  if (typeof raw.profile_id !== 'string' || raw.profile_id !== (config && config.profile_id)) return invalid('profile');
  if (!sessionAllowed(raw.session_id)) return invalid('session');
  if (!ACTIONS.has(raw.action)) return invalid('action');
  if (!plainObject(raw.args)) return invalid('unknown_field');
  if (raw.action === 'chrome_attachment' || raw.action === 'chrome_open') {
    if (Object.prototype.hasOwnProperty.call(raw, 'tab_id')) return invalid('ownership');
  } else if (safeId(raw.tab_id) === 0) return invalid('ownership');
  const argKeys = Object.keys(raw.args);
  if (argKeys.length > 3) return invalid('unknown_field');
  for (const key of argKeys) {
    if (dangerousKey(key)) return invalid('unknown_field');
  }
  return { ok: true };
}

function argsExact(args, names) {
  const keys = Object.keys(args);
  if (keys.length !== names.length) return false;
  return names.every(function (name) { return Object.prototype.hasOwnProperty.call(args, name); });
}

async function dispatchOperation(raw) {
  await loadConfig();
  const envelope = validateEnvelope(raw);
  if (!envelope.ok) {
    if (envelope.request_id && envelope.session_id) {
      return postResult({
        request_id: envelope.request_id,
        session_id: envelope.session_id,
        status: 'denied',
        op: raw && raw.action,
        data: { reason: envelope.reason }
      });
    }
    audit({ event: 'operation', status: 'denied', count: 0 });
    return { status: 'denied', data: { reason: envelope.reason } };
  }
  if (!config) {
    return postResult({
      request_id: raw.request_id,
      session_id: raw.session_id,
      status: 'denied',
      op: raw.action,
      data: { reason: 'profile' }
    });
  }
  if (config.allowed_actions.indexOf(raw.action) === -1) {
    return postResult({
      request_id: raw.request_id,
      session_id: raw.session_id,
      status: 'denied',
      op: raw.action,
      data: { reason: 'action' }
    });
  }
  const state = await mutateLatch(function (latch) {
    const prior = latch.completed[raw.request_id];
    if (prior && MUTATIONS.has(raw.action)) {
      if (prior.session_id !== raw.session_id || prior.action !== raw.action) return { kind: 'identity' };
      return { kind: 'done', rec: Object.assign({}, prior), stop: latch.stop, technical_block: latch.technical_block, authorized: latch.authorized, manual_tab_id: latch.manual_tab_id };
    }
    if (latch.inflight.some(function (item) { return item.request_id === raw.request_id; })) {
      return { kind: 'dup', stop: latch.stop };
    }
    return {
      kind: 'go',
      stop: latch.stop,
      technical_block: latch.technical_block,
      authorized: latch.authorized,
      manual_tab_id: latch.manual_tab_id,
      owned: sessionOwns(latch, raw.session_id, safeId(raw.tab_id))
    };
  });
  if (state.kind === 'identity') return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'duplicate' } });
  if (state.kind === 'done') {
    return postResult({
      request_id: raw.request_id,
      session_id: raw.session_id,
      status: state.rec.status,
      op: raw.action,
      tab_id: state.rec.tab_id,
      data: state.rec.status === 'ok' ? successData(raw.action, state.rec.length) : { reason: state.rec.reason || 'duplicate' }
    });
  }
  if (state.kind === 'dup') {
    audit({ event: 'operation', op: raw.action, status: 'denied', request_id: raw.request_id, session_id: raw.session_id, count: 0 });
    return { status: 'denied', data: { reason: 'duplicate' } };
  }
  if (expired()) {
    await stopLatch('expired');
    await postEvent('stop', { reason: 'expired' });
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'expired' } });
  }
  if (raw.action === 'chrome_attachment') return attachmentStatus(raw);
  if (!state.authorized || state.stop) {
    return postResult({
      request_id: raw.request_id,
      session_id: raw.session_id,
      status: 'denied',
      op: raw.action,
      data: { reason: 'stopped' }
    });
  }
  if (state.technical_block) {
    return postResult({
      request_id: raw.request_id,
      session_id: raw.session_id,
      status: 'denied',
      op: raw.action,
      data: { reason: 'technical_block' }
    });
  }
  if (raw.action === 'chrome_open') return openTab(raw);
  if (raw.action === 'chrome_close') return closeTab(raw, state);
  if (raw.action === 'chrome_read') return readTab(raw, state);
  if (raw.action === 'chrome_click') return clickTab(raw, state);
  if (raw.action === 'chrome_type') return typeTab(raw, state);
  if (raw.action === 'chrome_wait') return waitTab(raw, state);
  if (raw.action === LOGIN_ACTION) return loginTab(raw, state);
  return postResult({
    request_id: raw.request_id,
    session_id: raw.session_id,
    status: 'denied',
    op: raw.action,
    data: { reason: 'action' }
  });
}

async function attachmentStatus(raw) {
  if (!argsExact(raw.args, [])) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'unknown_field' } });
  }
  await reconcileAttachment();
  const state = await mutateLatch(function (latch) {
    return {
      stopped: latch.stop === true,
      authorized: latch.authorized === true,
      technical_block: latch.technical_block === true,
      manual_tab_id: latch.manual_tab_id || 0,
      owned_tab_ids: (latch.sessions[raw.session_id] || []).slice()
    };
  });
  return postResult({
    request_id: raw.request_id,
    session_id: raw.session_id,
    status: 'ok',
    op: raw.action,
    data: {
      stopped: state.stopped,
      authorized: state.authorized,
      technical_block: state.technical_block,
      owner_attached: state.manual_tab_id > 0 && state.authorized && !state.stopped,
      manual_tab_id: state.manual_tab_id,
      owned_tab_ids: state.owned_tab_ids,
      profile_id: config.profile_id
    }
  });
}

async function reconcileAttachment() {
  const epoch = runtimeEpoch;
  const saved = await mutateLatch(function (latch) {
    return { stop: latch.stop, authorized: latch.authorized, profile_id: latch.profile_id, origin: latch.authorized_origin,
      manual_tab_id: latch.manual_tab_id, sessions: copySessions(latch.sessions), technical_block: latch.technical_block };
  });
  if (saved.stop || !saved.authorized || saved.profile_id !== config.profile_id || expired()) return false;
  if (config.managed === true && (browserResetPending || !saved.manual_tab_id)) {
    const generation = config.generation;
    for (const origin of config.allowed_origins) if (!(await hasPermission(origin))) return false;
    if (browserResetPending) {
      const response = await brokerFetch('/extension/event', 'POST', JSON.stringify({
        type: 'managed_browser_restart', profile_id: config.profile_id, session_id: '', tab_id: 0,
        reason: 'browser_restart', generation: generation
      }));
      if (!response || response.status !== 200) return false;
      const decoded = await boundedNative(readBounded(response));
      if (!decoded.ok) return false;
      let accepted;
      try { accepted = JSON.parse(decoded.text); } catch (_) { return false; }
      if (!plainObject(accepted) || !argsExact(accepted, ['accepted']) || accepted.accepted !== true) return false;
      await boundedNative(browser().storage.session.set({ [STORAGE_BROWSER]: generation }));
      browserResetPending = false;
    }
    const reconciled = await mutateLatch(function (latch) {
      if (runtimeEpoch !== epoch || latch.stop || !latch.authorized || latch.generation !== generation || expired()) return false;
      latch.technical_block = false;
      latch.disconnect_notified = false;
      return true;
    });
    if (reconciled) {
      memory.authorizedOrigin = saved.origin;
      interrupted = false;
      await setBadge('ON');
    }
    return reconciled;
  }
  if (!saved.manual_tab_id) return false;
  let manual;
  try { manual = await boundedNative(browser().tabs.get(saved.manual_tab_id)); } catch (err) { return false; }
  const gate = originGate(manual && manual.url);
  if (!gate.ok || !attachedOriginAllowed(gate.url.origin, saved.origin) || !(await hasPermission(gate.url.origin))) return false;
  let existing;
  try { existing = await boundedNative(browser().tabs.query({})); } catch (err) { return false; }
  if (!Array.isArray(existing)) return false;
  const currentTabs = new Map(existing.map(function (tab) { return [safeId(tab.id), tab]; }));
  const sessions = Object.create(null);
  for (const sid of Object.keys(saved.sessions)) {
    if (!sessionAllowed(sid)) continue;
    const owned = [];
    for (const id of saved.sessions[sid]) {
      if (id === saved.manual_tab_id) continue;
      const tab = currentTabs.get(id);
      const ownedGate = originGate(tab && tab.url, true);
      if (ownedGate.ok && attachedOriginAllowed(ownedGate.url.origin, saved.origin) &&
          (config.managed !== true || await hasPermission(ownedGate.url.origin))) owned.push(id);
    }
    if (owned.length) sessions[sid] = owned;
  }
  const reconciled = await mutateLatch(function (latch) {
    if (runtimeEpoch !== epoch || latch.stop || !latch.authorized || latch.manual_tab_id !== saved.manual_tab_id || latch.authorized_origin !== saved.origin || expired()) return false;
    if (saved.technical_block && latch.technical_block) latch.sessions = sessions;
    latch.technical_block = false;
    latch.disconnect_notified = false;
    return true;
  });
  if (reconciled) {
    memory.authorizedOrigin = saved.origin;
    interrupted = false;
    await setBadge('ON');
  }
  return reconciled;
}

async function beginMutation(raw, tabId) {
  return mutateLatch(function (latch) {
    if (!wireMatches(raw.request_id, latch.generation) || latch.generation !== config.generation) return { ok: false, reason: 'generation' };
    if (latch.stop || latch.technical_block || !latch.authorized || interrupted) return { ok: false, reason: latch.stop ? 'stopped' : 'technical_block' };
    if (expired()) return { ok: false, reason: 'expired' };
    if (!sessionAllowed(raw.session_id)) return { ok: false, reason: 'session' };
    if (raw.action !== 'chrome_open' && !sessionOwns(latch, raw.session_id, safeId(tabId))) return { ok: false, reason: 'ownership' };
    if (latch.completed[raw.request_id]) return { ok: false, reason: 'duplicate', rec: Object.assign({}, latch.completed[raw.request_id]) };
    if (latch.inflight.some(function (item) { return item.request_id === raw.request_id; })) return { ok: false, reason: 'duplicate' };
    if (raw.action === LOGIN_ACTION && loginAttempted(latch, safeId(tabId))) return { ok: false, reason: 'login_attempted' };
    if (latch.inflight.length >= MAX_INFLIGHT || latch.completed_order.length + latch.inflight.length >= MAX_JOURNAL) return { ok: false, reason: 'bound' };
    latch.inflight.push({
      request_id: raw.request_id,
      session_id: raw.session_id,
      tab_id: safeId(tabId),
      action: raw.action,
      mutation: true,
      dispatched: false,
      epoch: runtimeEpoch
    });
    return { ok: true };
  });
}

async function markDispatched(requestId) {
  return mutateLatch(function (latch) {
    if (!config || !wireMatches(requestId, config.generation) || latch.generation !== config.generation) return false;
    if (latch.stop || latch.technical_block || !latch.authorized || interrupted || expired()) return false;
    const item = latch.inflight.find(function (entry) { return entry.request_id === requestId; });
    if (!item || item.epoch !== runtimeEpoch) return false;
    item.dispatched = true;
    return true;
  });
}

async function claimMutation(requestId, apply) {
  return mutateLatch(function (latch) {
    if (!config || !wireMatches(requestId, config.generation) || latch.generation !== config.generation) return { claimed: false, stop: true };
    if (expired()) { latch.stop = true; cancelInflight(latch, 'expired'); }
    const index = latch.inflight.findIndex(function (entry) { return entry.request_id === requestId; });
    if (index < 0) return { claimed: false, stop: latch.stop === true || interrupted || latch.technical_block || expired() };
    const item = latch.inflight[index];
    // Keep the journal entry until finishMutation commits its terminal result.
    // STOP between this claim and completion must still find and cancel it.
    if (item.claimed) return { claimed: false, stop: latch.stop === true || interrupted };
    item.claimed = true;
    if (latch.stop || interrupted || latch.technical_block || item.epoch !== runtimeEpoch || expired()) return { claimed: true, stop: true, item: item };
    if (apply) apply(latch, item);
    return { claimed: true, stop: false, item: item };
  });
}

async function finishMutation(raw, claimed, status, reason, tabId, data) {
  const terminal = await mutateLatch(function (latch) {
    if (!config || !wireMatches(raw.request_id, config.generation) || latch.generation !== config.generation) return { status: 'unknown', reason: 'generation' };
    if (expired()) { latch.stop = true; cancelInflight(latch, 'expired'); }
    const prior = latch.completed[raw.request_id];
    if (prior) return { status: prior.status, reason: prior.reason, tab_id: prior.tab_id, length: prior.length, prior: true };
    const index = latch.inflight.findIndex(function (entry) { return entry.request_id === raw.request_id; });
    if (index >= 0) latch.inflight.splice(index, 1);
    const blocked = latch.stop || latch.technical_block || interrupted || expired();
    const record = { status: blocked && status === 'ok' ? 'unknown' : status, reason: blocked && status === 'ok' ? 'stopped' : reason || '', tab_id: tabId || 0, session_id: raw.session_id, action: raw.action, length: data && data.length || 0 };
    if (claimed || index >= 0) rememberCompleted(latch, raw.request_id, record);
    return record;
  });
  if (expired()) { interrupted = true; await setBadge('STOP'); }
  return postResult({
    request_id: raw.request_id,
    session_id: raw.session_id,
    status: terminal.status,
    op: raw.action,
    tab_id: terminal.tab_id || 0,
    data: terminal.status === 'ok' ? successData(raw.action, terminal.length) : { reason: terminal.reason || '' }
  });
}

function successData(action, length) {
  if (action === 'chrome_open') return { owned: true };
  if (action === 'chrome_click') return { clicked: true };
  if (action === 'chrome_type') return { typed: true, length: length || 0 };
  if (action === 'chrome_close') return { closed: true };
  if (action === LOGIN_ACTION) return { submitted: true, authentication: 'unverified' };
  return {};
}

async function stillAllowed(raw, epoch) {
  return mutateLatch(function (latch) {
    return wireMatches(raw.request_id, latch.generation) && runtimeEpoch === epoch && !interrupted && !latch.stop && !latch.technical_block && latch.authorized && !expired() && sessionOwns(latch, raw.session_id, safeId(raw.tab_id));
  });
}

function waitForCommittedTab(tabId, expectedOrigin, deadline) {
  return new Promise(function (resolve) {
    let settled = false;
    let timer = null;
    let listener = null;
    const finish = function (result) {
      if (settled) return;
      settled = true;
      if (timer !== null) clearTimeout(timer);
      if (listener) {
        try { browser().tabs.onUpdated.removeListener(listener); } catch (err) { /* Native cleanup is best effort. */ }
      }
      nativeAborts.delete(abort);
      resolve(result);
    };
    const abort = function () { finish({ ok: false, reason: 'stopped' }); };
    const inspectUrl = function (value) {
      if (typeof value !== 'string' || value.length === 0) return false;
      const gate = originGate(value, true);
      if (!gate.ok) {
        finish({ ok: false, reason: gate.reason });
        return true;
      }
      if (gate.url.origin !== expectedOrigin || !attachedOriginAllowed(gate.url.origin, memory.authorizedOrigin) || memory.poisoned.has(tabId)) {
        finish({ ok: false, reason: 'origin' });
        return true;
      }
      finish({ ok: true });
      return true;
    };
    listener = function (changedId, changeInfo) {
      if (changedId === tabId && changeInfo && typeof changeInfo.url === 'string') inspectUrl(changeInfo.url);
    };
    nativeAborts.add(abort);
    if (interrupted) { abort(); return; }
    const remaining = deadline - Date.now();
    if (remaining <= 0) { finish({ ok: false, reason: 'timeout' }); return; }
    timer = setTimeout(function () { finish({ ok: false, reason: 'timeout' }); }, remaining);
    try { browser().tabs.onUpdated.addListener(listener); }
    catch (err) { finish({ ok: false, reason: 'ownership' }); return; }
    if (interrupted) { abort(); return; }
    browser().tabs.get(tabId).then(function (tab) {
      if (!tab || safeId(tab.id) !== tabId) { finish({ ok: false, reason: 'ownership' }); return; }
      // pendingUrl is navigation intent only; DOM access waits for a committed tab.url.
      if (tab.url) inspectUrl(tab.url);
    }, function () { finish({ ok: false, reason: 'ownership' }); });
  });
}

async function openTab(raw) {
  if (!argsExact(raw.args, ['url']) || typeof raw.args.url !== 'string') {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'unknown_field' } });
  }
  if (raw.tab_id != null) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'ownership' } });
  }
  const gate = originGate(raw.args.url, true);
  if (!gate.ok) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: gate.reason } });
  }
  if (!attachedOriginAllowed(gate.url.origin, memory.authorizedOrigin)) return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'origin' } });
  if (!(await hasPermission(gate.url.origin))) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'permission' } });
  }
  const room = await mutateLatch(function (latch) {
    return (latch.sessions[raw.session_id] || []).length < MAX_TABS_PER_SESSION;
  });
  if (!room) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'bound' } });
  }
  const started = await beginMutation(raw, 0);
  if (!started.ok) {
    return postResult({
      request_id: raw.request_id,
      session_id: raw.session_id,
      status: started.rec ? started.rec.status : 'denied',
      op: raw.action,
      tab_id: started.rec ? started.rec.tab_id : 0,
      data: { reason: started.reason }
    });
  }
  const armed = await markDispatched(raw.request_id);
  if (!armed || interrupted || expired()) return finishMutation(raw, true, 'denied', 'stopped', 0);
  let created = null;
  let timedOut = false;
  const epoch = runtimeEpoch;
  const openDeadline = Date.now() + DOM_TIMEOUT_MS;
  const creating = browser().tabs.create({ url: gate.url.toString(), active: false });
  creating.then(function (late) {
    if (timedOut || runtimeEpoch !== epoch) removeChildOnly(late && late.id, 0).catch(function () {});
  }, function () {});
  try {
    created = await boundedNative(creating);
  } catch (err) {
    timedOut = true;
    const claimed = await claimMutation(raw.request_id, null);
    return finishMutation(raw, claimed.claimed, 'unknown', 'unknown', 0);
  }
  const createdId = created && safeId(created.id);
  if (!createdId || created.active === true) {
    if (createdId) await removeChildOnly(createdId, 0).catch(function () {});
    const invalid = await claimMutation(raw.request_id, null);
    return finishMutation(raw, invalid.claimed, 'unknown', 'unknown', 0);
  }
  const committed = await waitForCommittedTab(createdId, gate.url.origin, openDeadline);
  if (!committed.ok) {
    const failed = await claimMutation(raw.request_id, null);
    const removed = await removeChildOnly(createdId, 0).catch(function () { return false; });
    const stopped = failed.stop || interrupted || committed.reason === 'stopped';
    const uncertain = !failed.claimed || stopped || !removed || committed.reason === 'timeout' || committed.reason === 'ownership';
    return finishMutation(raw, failed.claimed, uncertain ? 'unknown' : 'denied', stopped ? 'stopped' : uncertain ? 'timeout' : committed.reason, 0);
  }
  const claimed = await claimMutation(raw.request_id, function (latch) {
    if (!createdId) return;
    const list = (latch.sessions[raw.session_id] || []).slice();
    if (list.indexOf(createdId) === -1) list.push(createdId);
    latch.sessions[raw.session_id] = list;
  });
  if (!claimed.claimed || claimed.stop) {
    if (createdId) await removeChildOnly(createdId, 0).catch(function () {});
    return finishMutation(raw, claimed.claimed, 'unknown', claimed.stop ? 'stopped' : 'worker_restart', 0);
  }
  return finishMutation(raw, true, 'ok', '', createdId, { owned: true });
}

async function tabAccess(raw, state) {
  const tabId = safeId(raw.tab_id);
  if (!tabId) return { ok: false, reason: 'ownership' };
  const owned = await mutateLatch(function (latch) { return sessionOwns(latch, raw.session_id, tabId); });
  if (!owned || tabId === state.manual_tab_id) return { ok: false, reason: 'ownership' };
  if (memory.poisoned.has(tabId)) return { ok: false, reason: 'origin' };
  let tab;
  try {
    tab = await boundedNative(browser().tabs.get(tabId));
  } catch (err) {
    return { ok: false, reason: 'ownership' };
  }
  const gate = originGate(tab && tab.url);
  if (!gate.ok) return { ok: false, reason: gate.reason };
  if (!attachedOriginAllowed(gate.url.origin, memory.authorizedOrigin)) return { ok: false, reason: 'origin' };
  if (!(await hasPermission(gate.url.origin))) return { ok: false, reason: 'permission' };
  return { ok: true, tabId: tabId, url: gate.url };
}

async function readTab(raw, state) {
  if (!argsExact(raw.args, [])) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'unknown_field' } });
  }
  const access = await tabAccess(raw, state, false);
  if (!access.ok) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: safeId(raw.tab_id), data: { reason: access.reason } });
  }
  const epoch = runtimeEpoch;
  if (!(await stillAllowed(raw, epoch))) return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: access.tabId, data: { reason: 'stopped' } });
  let dom;
  try {
    dom = await runDom(access, { op: 'snapshot' });
  } catch (err) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: access.tabId, data: { reason: 'unknown' } });
  }
  if (!(await stillAllowed(raw, epoch))) return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'unknown', op: raw.action, tab_id: access.tabId, data: { reason: 'stopped' } });
  if (!dom.ok || !dom.result || !Array.isArray(dom.result.elements)) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: access.tabId, data: { reason: 'unknown' } });
  }
  const published = publishRefs(access, dom.result.elements);
  const fitted = fitRead(dom.result.excerpt || '', published.refs, published.truncated || dom.result.truncated, dom.result.diagnostics);
  const slot = memory.tabs.get(access.tabId);
  const visible = new Set(fitted.refs.map(function (item) { return item.ref; }));
  for (const ref of Object.keys(slot.refs)) if (!visible.has(ref)) delete slot.refs[ref];
  return postResult({
    request_id: raw.request_id,
    session_id: raw.session_id,
    status: 'ok',
    op: raw.action,
    tab_id: access.tabId,
    data: fitted
  });
}

async function clickTab(raw, state) {
  if (!argsExact(raw.args, ['ref']) || typeof raw.args.ref !== 'string' || raw.args.ref.length > 32) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'unknown_field' } });
  }
  const access = await tabAccess(raw, state, false);
  if (!access.ok) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: safeId(raw.tab_id), data: { reason: access.reason } });
  }
  const slot = memory.tabs.get(access.tabId);
  const meta = slot && slot.refs ? slot.refs[raw.args.ref] : null;
  if (!meta || raw.args.ref.indexOf('r' + slot.generation + '_') !== 0) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: access.tabId, data: { reason: 'stale_ref' } });
  }
  if (labelBlocked(meta.name) || autocompleteBlocked(meta.autocomplete) || meta.inputType === 'password' || meta.inputType === 'file' || meta.inputType === 'hidden') {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: access.tabId, data: { reason: 'credential' } });
  }
  const started = await beginMutation(raw, access.tabId);
  if (!started.ok) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: access.tabId, data: { reason: started.reason } });
  }
  const armed = await markDispatched(raw.request_id);
  if (!armed || interrupted || expired()) return finishMutation(raw, true, 'denied', 'stopped', access.tabId);
  memory.tabs.delete(access.tabId);
  let dom;
  try {
    dom = await runDom(access, {
      op: 'click',
      path: meta.path,
      tag: meta.tag,
      role: meta.role,
      name: meta.name,
      inputType: meta.inputType,
      autocomplete: meta.autocomplete,
      snapshot: meta.snapshot,
      node_id: meta.node_id
    });
  } catch (err) {
    const claimed = await claimMutation(raw.request_id, null);
    return finishMutation(raw, claimed.claimed, 'unknown', 'unknown', access.tabId);
  }
  const claimed = await claimMutation(raw.request_id, null);
  if (!claimed.claimed || claimed.stop) {
    return finishMutation(raw, claimed.claimed, 'unknown', claimed.stop ? 'stopped' : 'worker_restart', access.tabId);
  }
  if (!dom.ok || !dom.result || dom.result.performed !== true) {
    const reason = dom.result && ['credential', 'origin', 'popup', 'expired'].includes(dom.result.reason) ? dom.result.reason : 'stale_ref';
    return finishMutation(raw, true, 'denied', reason, access.tabId);
  }
  return finishMutation(raw, true, 'ok', '', access.tabId, { clicked: true });
}

async function typeTab(raw, state) {
  if (!argsExact(raw.args, ['ref', 'text']) || typeof raw.args.ref !== 'string' || typeof raw.args.text !== 'string') {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'unknown_field' } });
  }
  if (raw.args.ref.length > 32 || raw.args.text.length === 0 || raw.args.text.length > MAX_TYPE_CHARS) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'bound' } });
  }
  const access = await tabAccess(raw, state, false);
  if (!access.ok) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: safeId(raw.tab_id), data: { reason: access.reason } });
  }
  const slot = memory.tabs.get(access.tabId);
  const meta = slot && slot.refs ? slot.refs[raw.args.ref] : null;
  if (!meta || raw.args.ref.indexOf('r' + slot.generation + '_') !== 0) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: access.tabId, data: { reason: 'stale_ref' } });
  }
  if (labelBlocked(meta.name) || autocompleteBlocked(meta.autocomplete) || meta.inputType === 'password' || meta.inputType === 'file' || meta.inputType === 'hidden') {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: access.tabId, data: { reason: 'credential' } });
  }
  if ((meta.tag !== 'input' && meta.tag !== 'textarea') || (meta.tag === 'input' && !['text', 'search', 'url', 'email', 'tel', 'number'].includes(meta.inputType))) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: access.tabId, data: { reason: 'ambiguous' } });
  }
  const started = await beginMutation(raw, access.tabId);
  if (!started.ok) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: access.tabId, data: { reason: started.reason } });
  }
  const armed = await markDispatched(raw.request_id);
  if (!armed || interrupted || expired()) return finishMutation(raw, true, 'denied', 'stopped', access.tabId);
  memory.tabs.delete(access.tabId);
  let dom;
  try {
    dom = await runDom(access, {
      op: 'type',
      path: meta.path,
      tag: meta.tag,
      role: meta.role,
      name: meta.name,
      inputType: meta.inputType,
      autocomplete: meta.autocomplete,
      snapshot: meta.snapshot,
      node_id: meta.node_id,
      text: raw.args.text
    });
  } catch (err) {
    const claimed = await claimMutation(raw.request_id, null);
    return finishMutation(raw, claimed.claimed, 'unknown', 'unknown', access.tabId);
  }
  const claimed = await claimMutation(raw.request_id, null);
  if (!claimed.claimed || claimed.stop) {
    return finishMutation(raw, claimed.claimed, 'unknown', claimed.stop ? 'stopped' : 'worker_restart', access.tabId);
  }
  if (!dom.ok || !dom.result || dom.result.performed !== true) {
    return finishMutation(raw, true, 'denied', dom.result && ['credential', 'origin', 'popup', 'expired'].includes(dom.result.reason) ? dom.result.reason : 'stale_ref', access.tabId);
  }
  return finishMutation(raw, true, 'ok', '', access.tabId, { typed: true, length: raw.args.text.length });
}

async function waitTab(raw, state) {
  const names = Object.keys(raw.args);
  if (!names.length || !names.every(function (key) { return key === 'role' || key === 'name' || key === 'text'; })) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'unknown_field' } });
  }
  for (const key of names) {
    if (typeof raw.args[key] !== 'string' || raw.args[key].trim().length === 0 || raw.args[key].length > 200) {
      return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'bound' } });
    }
  }
  const access = await tabAccess(raw, state, false);
  if (!access.ok) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: safeId(raw.tab_id), data: { reason: access.reason } });
  }
  const epoch = runtimeEpoch;
  const command = { op: 'wait' };
  if (raw.args.role) command.role = raw.args.role;
  if (raw.args.name) command.name = raw.args.name;
  if (raw.args.text) command.text = raw.args.text;
  let dispatched = false;
  for (let attempt = 0; attempt < WAIT_ATTEMPTS; attempt += 1) {
    const stopped = !(await stillAllowed(raw, epoch));
    if (stopped) {
      return postResult({
        request_id: raw.request_id,
        session_id: raw.session_id,
        status: dispatched ? 'unknown' : 'denied',
        op: raw.action,
        tab_id: access.tabId,
        data: { reason: 'stopped' }
      });
    }
    dispatched = true;
    let dom;
    try {
      dom = await runDom(access, command);
    } catch (err) {
      return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'unknown', op: raw.action, tab_id: access.tabId, data: { reason: 'unknown' } });
    }
    if (!(await stillAllowed(raw, epoch))) return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'unknown', op: raw.action, tab_id: access.tabId, data: { reason: 'stopped' } });
    if (dom.ok && dom.result && dom.result.matched === true) {
      return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'ok', op: raw.action, tab_id: access.tabId, data: { matched: true } });
    }
    if (attempt + 1 < WAIT_ATTEMPTS) await delay(WAIT_GAP_MS);
  }
  return postResult({
    request_id: raw.request_id,
    session_id: raw.session_id,
    status: 'ok',
    op: raw.action,
    tab_id: access.tabId,
    data: { matched: false, reason: 'not_observed' }
  });
}

async function closeTab(raw, state) {
  if (!argsExact(raw.args, [])) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, data: { reason: 'unknown_field' } });
  }
  const tabId = safeId(raw.tab_id);
  if (!tabId || tabId === state.manual_tab_id || !state.owned) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: tabId, data: { reason: 'ownership' } });
  }
  const started = await beginMutation(raw, tabId);
  if (!started.ok) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: tabId, data: { reason: started.reason } });
  }
  const armed = await markDispatched(raw.request_id);
  if (!armed || interrupted || expired()) return finishMutation(raw, true, 'denied', 'stopped', tabId);
  try {
    await boundedNative(browser().tabs.remove(tabId));
  } catch (err) {
    const claimed = await claimMutation(raw.request_id, null);
    return finishMutation(raw, claimed.claimed, 'unknown', 'unknown', tabId);
  }
  const claimed = await claimMutation(raw.request_id, function (latch) {
    const list = latch.sessions[raw.session_id] || [];
    latch.sessions[raw.session_id] = list.filter(function (id) { return id !== tabId; });
  });
  if (!claimed.claimed || claimed.stop) {
    return finishMutation(raw, claimed.claimed, 'unknown', claimed.stop ? 'stopped' : 'worker_restart', tabId);
  }
  memory.tabs.delete(tabId);
  return finishMutation(raw, true, 'ok', '', tabId, { closed: true });
}

function loginAttempted(latch, tabId) {
  // Durable per-tab tombstone in the journal: an inflight attempt, or any
  // terminal one not denied before native input, blocks every request ID.
  if (latch.inflight.some(function (item) { return item.action === LOGIN_ACTION && item.tab_id === tabId; })) return true;
  return latch.completed_order.some(function (requestId) {
    const rec = latch.completed[requestId];
    return !!rec && rec.action === LOGIN_ACTION && rec.tab_id === tabId && rec.status !== 'denied';
  });
}

function loginPage(url, binding) {
  return !!url && url.origin === binding.origin && binding.pages.indexOf(url.pathname) !== -1 && !url.search && !url.hash;
}

async function loginAccess(raw, state, binding) {
  // The exact owner-bound page only; it replaces the credential-route gate.
  const tabId = safeId(raw.tab_id);
  if (!tabId) return { ok: false, reason: 'ownership' };
  const owned = await mutateLatch(function (latch) { return sessionOwns(latch, raw.session_id, tabId); });
  if (!owned || tabId === state.manual_tab_id) return { ok: false, reason: 'ownership' };
  let tab;
  try {
    tab = await boundedNative(browser().tabs.get(tabId));
  } catch (err) {
    return { ok: false, reason: 'ownership' };
  }
  const url = parseUrl(tab && tab.url);
  if (!loginPage(url, binding)) return { ok: false, reason: 'login_route' };
  if (!attachedOriginAllowed(url.origin, memory.authorizedOrigin)) return { ok: false, reason: 'origin' };
  if (!(await hasPermission(url.origin))) return { ok: false, reason: 'permission' };
  return { ok: true, tabId: tabId, url: url };
}

async function hasDebugger() {
  try {
    return await boundedNative(browser().permissions.contains({ permissions: ['debugger'] })) === true && !!browser().debugger;
  } catch (err) {
    return false;
  }
}

async function loginProbe(tabId, mode, binding, allowInline) {
  const wait = mode === 'disarm' ? boundedNative : interruptibleNative;
  const response = await wait(browser().scripting.executeScript({
    target: { tabId: tabId, frameIds: [0] },
    injectImmediately: true,
    func: globalThis.FleetLoginGate.loginFormProbe,
    args: [{ mode: mode, origin: binding.origin, pages: binding.pages.slice(), posts: binding.posts.slice(), deadline: Date.now() + DOM_TIMEOUT_MS, ...(allowInline === true ? { allowInline: true } : {}) }]
  }));
  if (!Array.isArray(response) || response.length !== 1 || !response[0] || response[0].frameId !== 0) return null;
  const result = response[0].result;
  return result && typeof result === 'object' && !Array.isArray(result) ? result : null;
}

function loginForm(result, binding, armed) {
  if (!result) return { ok: false, reason: 'unknown' };
  if (result.eligible !== true) return { ok: false, reason: LOGIN_REASONS.has(result.reason) ? result.reason : 'unsupported_login' };
  const names = ['eligible', 'origin', 'path', 'target_origin', 'target_path', 'saved', 'x', 'y', 'inline_submit', 'inline_click'];
  if (!argsExact(result, armed ? names.concat(['armed']) : names) || (armed && result.armed !== true)) return { ok: false, reason: 'unsupported_login' };
  if (result.saved !== true) return { ok: false, reason: 'needs_owner' };
  if (typeof result.inline_submit !== 'boolean' || typeof result.inline_click !== 'boolean') return { ok: false, reason: 'unsupported_login' };
  if (result.origin !== binding.origin || result.target_origin !== binding.origin || binding.pages.indexOf(result.path) === -1 || binding.posts.indexOf(result.target_path) === -1) return { ok: false, reason: 'login_route' };
  if (![result.x, result.y].every(function (n) { return typeof n === 'number' && Number.isFinite(n) && n >= 0 && n <= 32768; })) return { ok: false, reason: 'unsupported_login' };
  const form = {};
  for (const name of names) form[name] = result[name];
  return { ok: true, form: form, key: JSON.stringify(form) };
}

async function loginStillAllowed(raw, epoch, binding, tabId) {
  if (!(await stillAllowed(raw, epoch))) return false;
  try {
    return loginPage(parseUrl((await interruptibleNative(browser().tabs.get(tabId))).url), binding);
  } catch (err) {
    return false;
  }
}

async function detachAfter(attachCall, target) {
  // Detach after success, error or timeout; a late attach is detached too.
  try {
    await boundedNative(attachCall.then(async function () {
      try { await boundedNative(browser().debugger.sendCommand(target, 'Emulation.setScriptExecutionDisabled', { value: false })); }
      finally { await boundedNative(browser().debugger.detach(target)); }
    }, function () {}));
  } catch (err) { /* No raw error bodies. */ }
}

async function loginTab(raw, state) {
  const tabId = safeId(raw.tab_id);
  function deny(reason) {
    return postResult({ request_id: raw.request_id, session_id: raw.session_id, status: 'denied', op: raw.action, tab_id: tabId, data: { reason: reason } });
  }
  if (!argsExact(raw.args, [])) return deny('unknown_field');
  const binding = config.saved_login;
  if (!binding) return deny('action');
  const access = await loginAccess(raw, state, binding);
  if (!access.ok) return deny(access.reason);
  if (await mutateLatch(function (latch) { return loginAttempted(latch, access.tabId); })) return deny('login_attempted');
  const gate = globalThis.FleetLoginGate;
  if (!gate || typeof gate.loginFormProbe !== 'function' || !(await hasDebugger())) return deny('needs_owner');
  let first;
  try {
    first = loginForm(await loginProbe(access.tabId, 'inspect', binding), binding, false);
  } catch (err) {
    return deny(interrupted ? 'stopped' : 'unknown');
  }
  if (!first.ok) return deny(first.reason);
  const started = await beginMutation(raw, access.tabId);
  if (!started.ok) return deny(started.reason);
  const epoch = runtimeEpoch;
  // Durable before the first native command: a restart turns it into unknown.
  const armed = await markDispatched(raw.request_id);
  if (!armed || interrupted || expired()) return finishMutation(raw, true, 'denied', 'stopped', access.tabId);
  const target = { tabId: access.tabId };
  let attachCall = null;
  let sent = false;
  let outcome;
  try {
    attachCall = Promise.resolve(browser().debugger.attach(target, '1.3'));
    await interruptibleNative(attachCall);
    if (!(await loginStillAllowed(raw, epoch, binding, access.tabId))) {
      outcome = { status: 'denied', reason: 'stopped' };
    } else {
      // Temporarily suppress the page's own JavaScript in this owned tab.
      // This prevents an earlier main-world submit listener from redirecting
      // the credential POST. Fixed isolated extension probes still run.
      // No Network/Fetch, body, header, cookie or value access is introduced.
      await interruptibleNative(browser().debugger.sendCommand(target, 'Emulation.setScriptExecutionDisabled', { value: true }));
      // Fresh check after suppression; never reuse another document's state.
      const fresh = loginForm(await loginProbe(access.tabId, 'arm', binding, true), binding, true);
      if (!fresh.ok || fresh.key !== first.key) {
        outcome = { status: 'denied', reason: fresh.ok || fresh.reason === 'unknown' ? 'form_changed' : fresh.reason };
      } else if (!(await loginStillAllowed(raw, epoch, binding, access.tabId))) {
        outcome = { status: 'denied', reason: 'stopped' };
      } else {
        const point = { button: 'left', clickCount: 1, x: fresh.form.x, y: fresh.form.y };
        sent = true;
        await interruptibleNative(browser().debugger.sendCommand(target, 'Input.dispatchMouseEvent', Object.assign({ type: 'mousePressed' }, point)));
        if (!(await stillAllowed(raw, epoch))) {
          outcome = { status: 'unknown', reason: 'stopped' };
        } else {
          await interruptibleNative(browser().debugger.sendCommand(target, 'Input.dispatchMouseEvent', Object.assign({ type: 'mouseReleased' }, point)));
          let status = null;
          try { status = await loginProbe(access.tabId, 'status', binding); } catch (err) { status = null; }
          const seen = status && typeof status.outcome === 'string' ? status.outcome : '';
          outcome = seen === 'submitted' ? { status: 'ok', reason: '' }
            : { status: 'unknown', reason: seen === 'form_changed' ? 'form_changed' : seen === 'prevented' ? 'unsupported_login' : 'unknown' };
        }
      }
    }
  } catch (err) {
    outcome = sent ? { status: 'unknown', reason: interrupted ? 'stopped' : 'unknown' } : { status: 'denied', reason: interrupted ? 'stopped' : 'needs_owner' };
  } finally {
    // Cleanup is allowed after STOP; it performs no input/credential access.
    try { await loginProbe(access.tabId, 'disarm', binding); } catch (err) {}
    if (attachCall) await detachAfter(attachCall, target);
  }
  const claimed = await claimMutation(raw.request_id, null);
  if (outcome.status === 'ok' && (!claimed.claimed || claimed.stop)) {
    return finishMutation(raw, claimed.claimed, 'unknown', claimed.stop ? 'stopped' : 'worker_restart', access.tabId);
  }
  return finishMutation(raw, claimed.claimed, outcome.status, outcome.reason, access.tabId);
}

async function onToolbarClicked(tab) {
  if (toolbarBusy) return { status: 'denied', reason: 'duplicate' };
  toolbarBusy = true;
  interrupted = true;
  runtimeEpoch += 1;
  abortNative();
  const clickEpoch = runtimeEpoch;
  const clickConfig = config;
  const clickedGate = originGate(tab && tab.url || '');
  let permissionGrant = null;
  // Request optional HTTPS access while action.onClicked still owns the user
  // gesture, before any storage/tabs/permissions await. Managed attachment
  // asks once for the grant's whole HTTPS set; legacy keeps its clicked origin.
  // Already granted access resolves without another prompt. STOP never requests.
  if (config && toolbarState && !(toolbarState.authorized && !toolbarState.stop) &&
      !expired() && tab && safeId(tab.id) && tab.active === true && clickedGate.ok) {
    const permissionOrigins = (config.managed === true ? config.allowed_origins : [clickedGate.url.origin])
      .filter(function (origin) { return new URL(origin).protocol === 'https:'; })
      .map(function (origin) { return origin + '/*'; });
    // Optional host access is the only request made from this owner gesture.
    // The debugger API is required and separately checked by hasDebugger().
    const request = { origins: permissionOrigins };
    if (permissionOrigins.length) {
      try {
        permissionGrant = Promise.resolve(browser().permissions.request(request))
          .then(function (allowed) { return allowed === true; }, function () { return false; });
      } catch (err) { permissionGrant = Promise.resolve(false); }
    }
  }
  try {
    // Keep the host request above in the gesture, then re-read trusted policy
    // before any attach/STOP commit. A pending policy change cannot attach.
    await loadConfig();
    if (!config || !toolbarState || !tab || !safeId(tab.id)) {
      await setBadge('STOP');
      return { status: 'denied', reason: 'profile' };
    }
    const state = await mutateLatch(function (latch) {
      return { stop: latch.stop, authorized: latch.authorized };
    });
    if (state.authorized && !state.stop) {
      await stopLatch('owner_stop');
      await postEvent('stop', { tab_id: tab.id, reason: 'owner_stop' });
      audit({ event: 'toolbar', op: 'stop', status: 'ok', tab_id: tab.id, count: 1 });
      return { status: 'ok', reason: 'owner_stop' };
    }
    if (expired()) {
      await stopLatch('expired');
      return { status: 'denied', reason: 'expired' };
    }
    if (!clickedGate.ok) {
      await setBadge('STOP');
      return { status: 'denied', reason: clickedGate.reason };
    }
    if (permissionGrant && !(await permissionGrant)) {
      await setBadge('STOP');
      return { status: 'denied', reason: 'permission' };
    }
    let actual;
    try {
      actual = await boundedNative(browser().tabs.get(tab.id));
      const active = await boundedNative(browser().tabs.query({ active: true, currentWindow: true }));
      if (!actual.active || !active.some(function (item) { return item.id === tab.id; })) return { status: 'denied', reason: 'ownership' };
    } catch (err) { return { status: 'denied', reason: 'ownership' }; }
    const gate = originGate(actual.url || '');
    if (!gate.ok || gate.url.origin !== clickedGate.url.origin) {
      await setBadge('STOP');
      return { status: 'denied', reason: gate.ok ? 'origin' : gate.reason };
    }
    const allowed = await hasPermission(gate.url.origin);
    if (!allowed) {
      await setBadge('STOP');
      return { status: 'denied', reason: 'permission' };
    }
    if (expired() || config !== clickConfig || runtimeEpoch !== clickEpoch) {
      await setBadge('STOP');
      return { status: 'denied', reason: expired() ? 'expired' : 'stopped' };
    }
    if (config.managed === true) {
      await boundedNative(browser().storage.session.set({ [STORAGE_BROWSER]: config.generation }));
      browserResetPending = false;
    }
    await mutateLatch(function (latch) {
      latch.stop = false;
      latch.authorized = true;
      latch.technical_block = false;
      latch.disconnect_notified = false;
      latch.manual_tab_id = tab.id;
      latch.profile_id = config.profile_id;
      latch.authorized_origin = gate.url.origin;
      for (const sid of Object.keys(latch.sessions)) latch.sessions[sid] = latch.sessions[sid].filter(function (id) { return id !== tab.id; });
      return true;
    });
    memory.authorizedOrigin = gate.url.origin;
    memory.manualId = tab.id;
    interrupted = false;
    await setBadge('ON');
    await postEvent('attach', { tab_id: tab.id, reason: 'owner_attach' });
    audit({ event: 'toolbar', op: 'attach', status: 'ok', tab_id: tab.id, count: 1 });
    return { status: 'ok', reason: 'owner_attach' };
  } catch (err) {
    await setBadge('STOP');
    return { status: 'denied', reason: storageFailed ? 'storage' : 'stopped' };
  } finally {
    toolbarBusy = false;
  }
}

async function sessionForAgentTab(tabId) {
  return mutateLatch(function (latch) {
    if (!tabId || tabId === latch.manual_tab_id) return '';
    for (const sessionId of Object.keys(latch.sessions)) {
      if ((latch.sessions[sessionId] || []).indexOf(tabId) !== -1) return sessionId;
    }
    return '';
  });
}

async function removeChildOnly(childId, openerId) {
  const manualId = await mutateLatch(function (latch) { return latch.manual_tab_id || 0; });
  if (!safeId(childId) || childId === manualId || childId === openerId) return false;
  if (safeId(openerId)) {
    const child = await boundedNative(browser().tabs.get(childId));
    if (child.openerTabId !== openerId) return false;
  }
  await boundedNative(browser().tabs.remove(childId));
  return true;
}

async function onTabCreated(tab) {
  if (!tab || !safeId(tab.id) || !safeId(tab.openerTabId)) return;
  const openerId = tab.openerTabId;
  const sessionId = await sessionForAgentTab(openerId);
  if (!sessionId) return;
  // Native opener attribution is required; unknown/manual windows are untouched.
  // Closing attributable popups avoids adopting a focused or redirected child.
  try { await removeChildOnly(tab.id, openerId); } catch (err) { await markDisconnect(); }
  audit({ event: 'popup', op: 'chrome_close', status: 'denied', session_id: sessionId, tab_id: tab.id, count: 1 });
}

function onTabUpdated(tabId, changeInfo) {
  if (!changeInfo || typeof changeInfo.url !== 'string') return;
  const id = safeId(tabId);
  if (!id) return;
  memory.tabs.delete(id);
  memory.generations.delete(id);
  const gate = originGate(changeInfo.url, id !== memory.manualId);
  if (!gate.ok) memory.poisoned.add(id);
  else memory.poisoned.delete(id);
  if (id === memory.manualId && (!gate.ok || !attachedOriginAllowed(gate.url.origin, memory.authorizedOrigin))) stopLatch('origin').then(function () { return postEvent('stop', { tab_id: id, reason: 'origin' }); }).catch(function () {});
}

async function onTabRemoved(tabId) {
  const id = safeId(tabId);
  if (!id) return;
  memory.tabs.delete(id);
  memory.generations.delete(id);
  memory.poisoned.delete(id);
  if (id === memory.manualId) {
    interrupted = true;
    runtimeEpoch += 1;
    memory.manualId = 0;
  }
  const manualClosed = await mutateLatch(function (latch) {
    const manual = latch.manual_tab_id === id;
    if (manual) {
      latch.manual_tab_id = 0;
      latch.stop = true;
      cancelInflight(latch, 'manual_closed');
    }
    for (const sessionId of Object.keys(latch.sessions)) {
      latch.sessions[sessionId] = (latch.sessions[sessionId] || []).filter(function (item) { return item !== id; });
    }
    return manual;
  });
  if (manualClosed) {
    await setBadge('STOP');
    await postEvent('stop', { tab_id: id, reason: 'manual_closed' });
  }
}

async function markDisconnect() {
  interrupted = true;
  runtimeEpoch += 1;
  const notify = await mutateLatch(function (latch) {
    const edge = latch.technical_block !== true || latch.disconnect_notified !== true;
    latch.technical_block = true;
    cancelInflight(latch, 'technical_disconnect');
    const should = latch.disconnect_notified !== true;
    latch.disconnect_notified = true;
    return should && edge;
  });
  memory.tabs.clear();
  await setBadge('STOP');
  if (notify) await postEvent('disconnect', { reason: 'technical_disconnect' });
}

async function pollOnce() {
  if (pollBusy) return { polled: false, reason: 'busy' };
  pollBusy = true;
  try {
  if (!config) await loadConfig();
  if (!config) return { polled: false, reason: 'profile' };
  let response;
  try {
    response = await brokerFetch('/extension/poll', 'GET');
  } catch (err) {
    await markDisconnect();
    return { polled: false, reason: 'technical_block' };
  }
  if (!response || (response.status !== 200 && response.status !== 204)) {
    await markDisconnect();
    return { polled: false, reason: 'technical_block' };
  }
  if (response.status === 204) {
    return { polled: true, empty: true };
  }
  const decoded = await boundedNative(readBounded(response));
  if (!decoded.ok) {
    await markDisconnect();
    return { polled: false, reason: decoded.reason };
  }
  let message;
  try {
    message = JSON.parse(decoded.text);
  } catch (err) {
    await markDisconnect();
    return { polled: false, reason: 'utf8' };
  }
  await handleOperation(message);
  return { polled: true };
  } catch (err) {
    await markDisconnect();
    return { polled: false, reason: 'technical_block' };
  } finally { pollBusy = false; }
}

async function onBrowserStartup() {
  if (!config) await loadConfig();
  if (config && config.managed === true) {
    await recover(); // Recognize the browser realm even with late onStartup.
    return;
  }
  await stopLatch('browser_restart');
  await mutateLatch(function (latch) { latch.sessions = Object.create(null); latch.manual_tab_id = 0; });
  memory.manualId = 0;
  await postEvent('stop', { reason: 'browser_restart' });
}

function installListeners() {
  if (listenersInstalled) return;
  const api = browser();
  if (api.storage.onChanged) api.storage.onChanged.addListener(function (changes, area) {
    if (area === 'local' && Object.hasOwn(changes, STORAGE_CONFIG)) {
      invalidateRuntime();
      recover().catch(function () {});
    }
  });
  api.action.onClicked.addListener(function (tab) {
    onToolbarClicked(tab).catch(function () {});
  });
  api.tabs.onCreated.addListener(function (tab) {
    onTabCreated(tab).catch(function () {});
  });
  api.tabs.onUpdated.addListener(function (tabId, changeInfo) {
    try { onTabUpdated(tabId, changeInfo); } catch (err) { /* ignore page noise */ }
  });
  api.tabs.onRemoved.addListener(function (tabId) {
    onTabRemoved(tabId).catch(function () {});
  });
  if (api.runtime && api.runtime.onStartup) api.runtime.onStartup.addListener(function () { onBrowserStartup().catch(function () {}); });
  if (api.alarms && api.alarms.onAlarm && api.alarms.create) {
    api.alarms.onAlarm.addListener(function () {
      pollOnce().catch(function () {});
    });
    Promise.resolve(api.alarms.create('chrome-relay-poll', { periodInMinutes: 1 })).catch(function () {});
  }
  listenersInstalled = true;
}

async function boot(options) {
  try { installListeners(); } catch (err) { audit({ event: 'boot', status: 'unknown', count: 0 }); }
  await recover();
  if (options && options.poll === false) return;
  if (polling) return;
  polling = true;
  while (polling) {
    try { await pollOnce(); } catch (err) { await markDisconnect(); }
    await delay(250);
  }
}

function fixedDom(command) {
  function fold(value) {
    return String(value || '')
      .normalize('NFKC')
      .toLowerCase()
      .normalize('NFD')
      .replace(/\p{M}/gu, '')
      .replace(/[^a-z0-9]+/g, ' ')
      .trim();
  }
  function blockedName(name) {
    const folded = fold(name);
    if (!folded) return false;
    if (/\b(sign in|log in|grant access|allow access|approve permissions|zezwol na dostep)\b/.test(folded)) return true;
    const words = new Set([
      'authorize', 'authorise', 'consent', 'oauth', 'signin', 'login', 'password',
      'passkey', 'passcode', 'otp', 'mfa', 'totp', 'zaloguj', 'autoryzuj'
    ]);
    return folded.split(' ').some(function (word) { return words.has(word); });
  }
  function redact(text) {
    return String(text || '')
      .replace(/\b(?:api[_-]?key|secret|token|password|passwd|pwd|authorization|credential)\b\s*[:=]\s*['"]?[^\s'"]+/gi, '[redacted]')
      .replace(/\bbearer\s+[a-z0-9\-._~+/]+=*/gi, '[redacted]')
      .replace(/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}/g, '[redacted]')
      .replace(/\b(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9+/]{40,}={0,2})\b/g, '[redacted]');
  }
  function attr(node, key) {
    if (!node || typeof node.getAttribute !== 'function') return '';
    if (key === 'value') return '';
    const got = node.getAttribute(key);
    return got == null ? '' : String(got);
  }
  function tagOf(node) {
    return String(node && node.tagName || '').toLowerCase();
  }
  function kids(node) {
    if (!node || !node.children) return [];
    const out = [];
    for (let i = 0; i < node.children.length && i < 2000; i += 1) out.push(node.children[i]);
    return out;
  }
  function pathOf(node) {
    const parts = [];
    let current = node;
    while (current && current.parentElement) {
      const parent = current.parentElement;
      const tag = tagOf(current);
      let nth = 0;
      let index = -1;
      const children = kids(parent);
      for (let i = 0; i < children.length; i += 1) {
        if (tagOf(children[i]) === tag) {
          nth += 1;
          if (children[i] === current) index = nth;
        }
      }
      if (index < 1) return '';
      parts.push(tag + '[' + index + ']');
      if (parent === node.ownerDocument.body) break;
      current = parent;
    }
    return parts.reverse().join('/');
  }
  function resolve(doc, path) {
    if (!doc || !doc.body || typeof path !== 'string' || !path) return null;
    let node = doc.body;
    const parts = path.split('/');
    for (let p = 0; p < parts.length; p += 1) {
      const match = /^([a-z0-9]+)\[(\d+)\]$/.exec(parts[p]);
      if (!match) return null;
      const want = match[1];
      const index = Number(match[2]);
      let nth = 0;
      let found = null;
      const children = kids(node);
      for (let i = 0; i < children.length; i += 1) {
        if (tagOf(children[i]) === want) {
          nth += 1;
          if (nth === index) {
            found = children[i];
            break;
          }
        }
      }
      if (!found) return null;
      node = found;
    }
    return node;
  }
  function roleOf(node) {
    const explicit = attr(node, 'role');
    if (explicit) return explicit.toLowerCase();
    const tag = tagOf(node);
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'input') {
      const kind = (attr(node, 'type') || 'text').toLowerCase();
      if (kind === 'button' || kind === 'submit') return 'button';
      if (kind === 'checkbox') return 'checkbox';
      return 'textbox';
    }
    return tag;
  }
  function nameOf(node) {
    const aria = attr(node, 'aria-label');
    if (aria.trim()) return aria.trim().slice(0, 120);
    const tag = tagOf(node);
    if (node.labels && node.labels.length) return Array.from(node.labels).map(function (label) { return String(label.textContent || ''); }).join(' ').replace(/\s+/g, ' ').trim().slice(0, 120);
    if (tag === 'input' || tag === 'textarea') {
      return (attr(node, 'placeholder') || attr(node, 'title') || (inputType(node) === 'submit' ? 'Submit' : '')).trim().slice(0, 120);
    }
    return String(node.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  }
  function hasAttr(node, key) {
    if (!node) return false;
    if (typeof node.hasAttribute === 'function') {
      try { return node.hasAttribute(key); } catch (_) {}
    }
    if (typeof node.getAttribute === 'function') {
      try { return node.getAttribute(key) !== null; } catch (_) {}
    }
    return false;
  }
  function computedStyleOf(node) {
    if (!node || node.nodeType !== 1) return null;
    if (typeof globalThis.getComputedStyle === 'function') {
      try {
        const cs = globalThis.getComputedStyle(node);
        if (cs) return cs;
      } catch (_) {}
    }
    try {
      const view = (node.ownerDocument || globalThis.document || {}).defaultView;
      if (view && typeof view.getComputedStyle === 'function') {
        const cs = view.getComputedStyle(node);
        if (cs) return cs;
      }
    } catch (_) {}
    if (typeof node.getComputedStyle === 'function') {
      try {
        const cs = node.getComputedStyle();
        if (cs) return cs;
      } catch (_) {}
    }
    if (node.computedStyle && typeof node.computedStyle === 'object') {
      return node.computedStyle;
    }
    return null;
  }
  function isDisplayContents(node) {
    if (!node || node.nodeType !== 1) return false;
    const cs = computedStyleOf(node);
    let hasComputedDisplay = false;
    if (cs) {
      const disp = String(cs.display || '').toLowerCase();
      if (disp) {
        hasComputedDisplay = true;
        if (disp === 'contents') return true;
      }
    }
    if (!hasComputedDisplay) {
      const s = attr(node, 'style');
      if (s && /display\s*:\s*contents/i.test(s)) return true;
      if (node.style) {
        if (typeof node.style === 'object') {
          if (String(node.style.display || '').toLowerCase() === 'contents') return true;
        } else if (typeof node.style === 'string') {
          if (/display\s*:\s*contents/i.test(node.style)) return true;
        }
      }
    }
    return false;
  }
  function isExcludedField(node) {
    if (!node || node.nodeType !== 1) return false;
    const tag = tagOf(node);
    if (['input', 'textarea', 'select', 'script', 'style', 'noscript'].includes(tag)) return true;
    if (hasAttr(node, 'contenteditable')) {
      const ce = attr(node, 'contenteditable').toLowerCase();
      if (ce !== 'false') return true;
    }
    if (node.isContentEditable === true) return true;
    return false;
  }
  function hasExcludedAncestor(node, root) {
    let curr = node;
    let depth = 0;
    while (curr && curr.nodeType === 1 && depth < 30) {
      if (isExcludedField(curr)) return true;
      if (curr === root) break;
      curr = curr.parentElement;
      depth += 1;
    }
    return false;
  }
  function isSubtreeExcluded(node) {
    if (!node || node.nodeType !== 1) return false;
    if (node.hidden === true || (hasAttr(node, 'hidden') && attr(node, 'hidden').toLowerCase() !== 'false')) return true;
    if (node.inert === true || (hasAttr(node, 'inert') && attr(node, 'inert').toLowerCase() !== 'false')) return true;
    if (attr(node, 'aria-hidden').toLowerCase() === 'true') return true;
    const cs = computedStyleOf(node);
    let hasComputedDisplay = false;
    let hasComputedCv = false;
    if (cs) {
      const disp = String(cs.display || '').toLowerCase();
      if (disp) {
        hasComputedDisplay = true;
        if (disp === 'none') return true;
      }
      const cv = String(cs.contentVisibility || cs['content-visibility'] || '').toLowerCase();
      if (cv) {
        hasComputedCv = true;
        if (cv === 'hidden') return true;
      }
    }
    if (!hasComputedDisplay || !hasComputedCv) {
      const s = attr(node, 'style');
      if (s) {
        if (!hasComputedDisplay && /display\s*:\s*none/i.test(s)) return true;
        if (!hasComputedCv && /content-visibility\s*:\s*hidden/i.test(s)) return true;
      }
      if (node.style) {
        if (typeof node.style === 'object') {
          if (!hasComputedDisplay && String(node.style.display || '').toLowerCase() === 'none') return true;
          if (!hasComputedCv && String(node.style.contentVisibility || node.style['content-visibility'] || '').toLowerCase() === 'hidden') return true;
        } else if (typeof node.style === 'string') {
          if (!hasComputedDisplay && /display\s*:\s*none/i.test(node.style)) return true;
          if (!hasComputedCv && /content-visibility\s*:\s*hidden/i.test(node.style)) return true;
        }
      }
    }
    return false;
  }
  function getExplicitVisibility(node) {
    if (!node || node.nodeType !== 1) return '';
    const cs = computedStyleOf(node);
    if (cs) {
      const v = String(cs.visibility || '').toLowerCase();
      if (v === 'visible' || v === 'hidden' || v === 'collapse') return v;
    }
    const s = attr(node, 'style');
    if (s) {
      const m = s.match(/(?:^|;)\s*visibility\s*:\s*([a-z-]+)/i);
      if (m) {
        const v = m[1].toLowerCase();
        if (v === 'visible' || v === 'hidden' || v === 'collapse') return v;
      }
    }
    if (node.style) {
      if (typeof node.style === 'object') {
        const v = String(node.style.visibility || '').toLowerCase();
        if (v === 'visible' || v === 'hidden' || v === 'collapse') return v;
      } else if (typeof node.style === 'string') {
        const m = node.style.match(/(?:^|;)\s*visibility\s*:\s*([a-z-]+)/i);
        if (m) {
          const v = m[1].toLowerCase();
          if (v === 'visible' || v === 'hidden' || v === 'collapse') return v;
        }
      }
    }
    return '';
  }
  function isHiddenTree(node) {
    if (!node || node.nodeType !== 1) return false;
    let curr = node;
    const body = (node.ownerDocument || globalThis.document).body;
    let depth = 0;
    let explicitVis = '';
    while (curr && curr.nodeType === 1 && depth < 30) {
      if (isSubtreeExcluded(curr)) return true;
      if (!explicitVis) {
        const v = getExplicitVisibility(curr);
        if (v) explicitVis = v;
      }
      if (curr === body) break;
      curr = curr.parentElement;
      depth += 1;
    }
    if (explicitVis === 'hidden' || explicitVis === 'collapse') return true;
    if (typeof node.checkVisibility === 'function') {
      try {
        if (!node.checkVisibility() && !isDisplayContents(node)) return true;
      } catch (_) {}
    }
    return false;
  }
  function isDialogContainer(node) {
    if (!node || node.nodeType !== 1 || isExcludedField(node)) return false;
    const tag = tagOf(node);
    if (tag === 'dialog') return true;
    const role = attr(node, 'role').toLowerCase();
    return role === 'dialog' || role === 'alertdialog';
  }
  function isMainContainer(node) {
    if (!node || node.nodeType !== 1 || isExcludedField(node)) return false;
    const tag = tagOf(node);
    if (tag === 'main') return true;
    const role = attr(node, 'role').toLowerCase();
    return role === 'main';
  }
  function inputType(node) {
    if (tagOf(node) !== 'input') return '';
    return (attr(node, 'type') || 'text').toLowerCase();
  }
  function secretRoute(href, base) {
    let url;
    try { url = new URL(String(href || ''), base || globalThis.document.location.href); } catch (err) { return true; }
    const host = url.hostname.toLowerCase();
    let path = url.pathname.toLowerCase();
    let query = url.search.toLowerCase();
    let fragment = url.hash.toLowerCase();
    try {
      for (let i = 0; i < 3; i += 1) {
        path = decodeURIComponent(path);
        query = decodeURIComponent(query);
        fragment = decodeURIComponent(fragment);
      }
    } catch (err) { return true; }
    path = path.toLowerCase();
    query = query.toLowerCase();
    fragment = fragment.toLowerCase();
    if (host === 'accounts.google.com' || host === 'login.microsoftonline.com' || host === 'login.live.com' || host === 'appleid.apple.com') return true;
    if (/(^|\.)auth0\.com$/.test(host) || /(^|\.)okta\.com$/.test(host)) return true;
    const route = /\/(auth|authenticate|authentication|authorization|oauth2?|authorize|consent|signin|sign-in|login|log-in|saml|passkey|mfa|challenge)([/.;]|$)/;
    if (route.test(path) || route.test(fragment)) return true;
    if (/[?&#](client_id|redirect_uri|response_type|access_token|id_token|password|authorization|auth|consent|login|signin)(=|&|$)/.test(query + fragment)) return true;
    if (/[?&](action|page|route|mode|view)=(login|signin|sign-in|oauth2?|authorize|consent)(&|$)/.test(query)) return true;
    return false;
  }
  function safeNavigationUrl(value, origin, base) {
    if (typeof value !== 'string' || !value || value.length > 2048 || /\s/.test(value)) return '';
    try {
      const url = base ? new URL(value, base) : new URL(value);
      if (url.username || url.password || url.origin !== origin || !['https:', 'http:'].includes(url.protocol) || url.href.length > 2048) return '';
      let decoded = url.href;
      for (let i = 0; i < 3; i += 1) {
        const next = decodeURIComponent(decoded);
        if (next === decoded) break;
        decoded = next;
      }
      if (/%[a-f0-9]{2}|\s/i.test(decoded) || redact(decoded) !== decoded) return '';
      if (/(?:^|[^a-z0-9])(?:api[_-]?key|secret|(?:access[_-]?|refresh[_-]?|id[_-]?)?token|password|passwd|pwd|authorization|credential|bearer|session(?:[_-]?id)?|auth|code|signature|client[_-]?id|redirect[_-]?uri|response[_-]?type|consent|oauth2?|signin|sign-in|login|log-in|saml|passkey|mfa|challenge)(?=$|[^a-z0-9])/i.test(decoded)) return '';
      if (/\b(?:sk-|gh[pousr]_|github_pat_|AKIA)[a-z0-9_-]{8,}/i.test(decoded)) return '';
      return url.href;
    } catch (err) { return ''; }
  }
  function baseOf(node) {
    const owner = node && node.ownerDocument || globalThis.document;
    return owner.baseURI || owner.location.href;
  }
  function credentialControl(node) {
    const kind = inputType(node);
    const auto = attr(node, 'autocomplete');
    const ident = (attr(node, 'id') + ' ' + attr(node, 'name')).toLowerCase();
    if (kind === 'password') return true;
    if (/password|one-time|webauthn/.test(auto.toLowerCase())) return true;
    if (/password|passcode|passkey|otp|mfa|totp/.test(ident)) return true;
    return false;
  }
  function formOf(node) {
    if (node && node.form) return node.form;
    let parent = node;
    while (parent) {
      if (tagOf(parent) === 'form') return parent;
      parent = parent.parentElement;
    }
    return null;
  }
  function targetGate(node) {
    if (blockedName(nameOf(node)) || credentialControl(node)) return 'credential';
    const kind = inputType(node);
    if (kind === 'hidden' || kind === 'file') return 'credential';
    const form = formOf(node);
    if (form) {
      const controls = [];
      const budget = { visits: 0, bounded: false };
      walk(form, controls, 0, budget);
      if (budget.bounded || controls.some(credentialControl)) return 'credential';
      const action = attr(node, 'formaction') || attr(form, 'action');
      if (secretRoute(action || baseOf(node), baseOf(node))) return 'credential';
      if (action && !sameOrigin(action, baseOf(node))) return 'origin';
      const target = attr(node, 'formtarget') || attr(form, 'target');
      if (target && target !== '_self') return 'popup';
    }
    let link = node;
    while (link && tagOf(link) !== 'a') link = link.parentElement;
    if (link) {
      const target = attr(link, 'href');
      if (secretRoute(target, baseOf(link))) return 'credential';
      if (!sameOrigin(target, baseOf(link))) return 'origin';
      if (attr(link, 'target') && attr(link, 'target') !== '_self') return 'popup';
    }
    return '';
  }
  function sameOrigin(value, base) {
    try {
      const url = new URL(String(value || ''), base || globalThis.document.location.href);
      return !url.username && !url.password && url.origin === command.origin && (url.protocol === 'https:' || url.protocol === 'http:');
    } catch (err) { return false; }
  }
  function safeText(document, budget) {
    let dialogText = '';
    let mainText = '';
    let bodyText = '';
    const limit = budget || { visits: 0, bounded: false };
    function appendTo(targetText, value) {
      const source = String(value || '');
      const piece = redact(source) + ' ';
      if (source.length > 6000) limit.bounded = true;
      if (targetText.length + piece.length > 6000) limit.bounded = true;
      return targetText + piece.slice(0, Math.max(0, 6000 - targetText.length));
    }
    const docRoot = (document || globalThis.document).body;
    if (!docRoot) return '';
    function scanCollect(node, depth, currentSection, inheritedVis) {
      if (!node) return;
      if (depth > 30 || limit.visits >= 2000) { limit.bounded = true; return; }
      if (command && Number.isFinite(command.deadline) && Date.now() >= command.deadline) { limit.bounded = true; return; }
      limit.visits += 1;
      let nextVis = inheritedVis || 'visible';
      let nextSection = currentSection;
      if (node.nodeType === 3) {
        let isVis = (nextVis === 'visible');
        const parent = node.parentElement;
        if (isVis && parent && typeof parent.checkVisibility === 'function') {
          try {
            if (!parent.checkVisibility() && !isDisplayContents(parent)) isVis = false;
          } catch (_) {}
        }
        if (isVis) {
          if (nextSection === 'dialog') {
            if (dialogText.length < 6000) dialogText = appendTo(dialogText, node.nodeValue);
          } else if (nextSection === 'main') {
            if (mainText.length < 6000) mainText = appendTo(mainText, node.nodeValue);
          } else {
            if (bodyText.length < 6000) bodyText = appendTo(bodyText, node.nodeValue);
          }
        }
        return;
      }
      if (node.nodeType === 1) {
        if (isSubtreeExcluded(node)) return;
        const explicit = getExplicitVisibility(node);
        if (explicit) nextVis = explicit;
        if (typeof node.checkVisibility === 'function') {
          try {
            if (!node.checkVisibility() && !isDisplayContents(node)) {
              if (explicit !== 'hidden' && nextVis !== 'hidden') return;
            }
          } catch (_) {}
        }
        if (isExcludedField(node)) return;
        if (isDialogContainer(node) && !hasExcludedAncestor(node, docRoot)) {
          nextSection = 'dialog';
        } else if (isMainContainer(node) && currentSection !== 'dialog' && !hasExcludedAncestor(node, docRoot)) {
          nextSection = 'main';
        }
      }
      if (node.childNodes && node.childNodes.length) {
        for (let i = 0; i < node.childNodes.length; i += 1) {
          scanCollect(node.childNodes[i], depth + 1, nextSection, nextVis);
          if (limit.visits >= 2000) { limit.bounded = true; break; }
          if (command && Number.isFinite(command.deadline) && Date.now() >= command.deadline) { limit.bounded = true; break; }
        }
        if (limit.visits >= 2000 && node.childNodes.length > 0) limit.bounded = true;
      } else if (!kids(node).length && nextVis === 'visible') {
        let isVis = true;
        if (typeof node.checkVisibility === 'function') {
          try {
            if (!node.checkVisibility() && !isDisplayContents(node)) isVis = false;
          } catch (_) {}
        }
        if (isVis) {
          const txt = node.textContent;
          if (txt) {
            if (nextSection === 'dialog') {
              if (dialogText.length < 6000) dialogText = appendTo(dialogText, txt);
            } else if (nextSection === 'main') {
              if (mainText.length < 6000) mainText = appendTo(mainText, txt);
            } else {
              if (bodyText.length < 6000) bodyText = appendTo(bodyText, txt);
            }
          }
        }
      } else {
        const children = kids(node);
        if (node.children && node.children.length > children.length) limit.bounded = true;
        for (let i = 0; i < children.length; i += 1) {
          scanCollect(children[i], depth + 1, nextSection, nextVis);
          if (limit.visits >= 2000) { limit.bounded = true; break; }
          if (command && Number.isFinite(command.deadline) && Date.now() >= command.deadline) { limit.bounded = true; break; }
        }
      }
    }
    scanCollect(docRoot, 0, 'body', 'visible');
    let text = '';
    if (dialogText) {
      text = dialogText.slice(0, 6000);
      if (dialogText.length >= 6000) limit.bounded = true;
    }
    if (mainText) {
      if (text.length < 6000) {
        const sep = text ? ' ' : '';
        const budgetRemaining = 6000 - text.length - sep.length;
        if (budgetRemaining > 0) {
          if (mainText.length > budgetRemaining) limit.bounded = true;
          text = text + sep + mainText.slice(0, budgetRemaining);
        } else {
          limit.bounded = true;
        }
      } else {
        limit.bounded = true;
      }
    }
    if (bodyText) {
      if (text.length < 6000) {
        const sep = text ? ' ' : '';
        const budgetRemaining = 6000 - text.length - sep.length;
        if (budgetRemaining > 0) {
          if (bodyText.length > budgetRemaining) limit.bounded = true;
          text = text + sep + bodyText.slice(0, budgetRemaining);
        } else {
          limit.bounded = true;
        }
      } else {
        limit.bounded = true;
      }
    }
    text = redact(text.replace(/\s+/g, ' ').trim());
    if (text.length > 6000) {
      limit.bounded = true;
      text = text.slice(0, 6000);
    }
    return text;
  }
  function interactive(node) {
    const tag = tagOf(node);
    const role = attr(node, 'role').toLowerCase();
    if (tag === 'a' || tag === 'button' || tag === 'input' || tag === 'textarea' || tag === 'select') return true;
    return role === 'button' || role === 'link' || role === 'textbox';
  }
  function walk(node, out, depth, budget, skipHidden, excludedSet, inheritedVis) {
    const limit = budget || { visits: 0, bounded: false };
    if (!node) return;
    if (depth > 30 || out.length >= 500 || limit.visits >= 2000) { limit.bounded = true; return; }
    if (command && Number.isFinite(command.deadline) && Date.now() >= command.deadline) { limit.bounded = true; return; }
    limit.visits += 1;
    let nextVis = inheritedVis || 'visible';
    if (node.nodeType === 1) {
      if (skipHidden && isSubtreeExcluded(node)) return;
      const explicit = getExplicitVisibility(node);
      if (explicit) nextVis = explicit;
      if (excludedSet && excludedSet.has(node)) return;
      let isVisible = (nextVis === 'visible');
      if (isVisible && typeof node.checkVisibility === 'function') {
        try {
          if (!node.checkVisibility() && !isDisplayContents(node)) {
            isVisible = false;
            if (skipHidden && explicit !== 'hidden' && nextVis !== 'hidden') return;
          }
        } catch (_) {}
      }
      if (!skipHidden || isVisible) {
        if (interactive(node)) out.push(node);
      }
    }
    const children = kids(node);
    if (node.children && node.children.length > children.length) limit.bounded = true;
    for (let i = 0; i < children.length; i += 1) {
      walk(children[i], out, depth + 1, limit, skipHidden, excludedSet, nextVis);
      if (limit.visits >= 2000 || out.length >= 500) { limit.bounded = true; break; }
      if (command && Number.isFinite(command.deadline) && Date.now() >= command.deadline) { limit.bounded = true; break; }
    }
  }
  function readDocuments(root) {
    const documents = [{ document: root, depth: 0 }];
    const seen = new Set([root]);
    const budget = { visits: 0, bounded: false };
    let frameCount = 0;
    let blocked = 0;
    function discover(node, depth, entry) {
      if (!node) return;
      if (depth > 30 || budget.visits >= 2000 || frameCount >= 64) { budget.bounded = true; return; }
      budget.visits += 1;
      if (['frame', 'iframe'].includes(tagOf(node))) {
        frameCount += 1;
        if (entry.depth >= 8 || documents.length >= 64) { budget.bounded = true; return; }
        try {
          // contentDocument is SOP enforced by Chrome. Do not inject into a
          // foreign frame or inspect its body, title, attributes or controls.
          const child = node.contentDocument;
          const href = child && child.location && String(child.location.href || '');
          if (!child || !href || !/^https?:\/\//i.test(href) || !sameOrigin(href) || secretRoute(href)) { blocked += 1; return; }
          if (!seen.has(child)) {
            seen.add(child);
            documents.push({ document: child, depth: entry.depth + 1 });
          }
        } catch (err) { blocked += 1; }
        return;
      }
      const children = kids(node);
      if (node.children && node.children.length > children.length) budget.bounded = true;
      for (const child of children) {
        discover(child, depth + 1, entry);
        if (budget.visits >= 2000 || frameCount >= 64) { budget.bounded = true; break; }
      }
    }
    for (let i = 0; i < documents.length; i += 1) discover(documents[i].document.body, 0, documents[i]);
    return { documents: documents, blocked: blocked, bounded: budget.bounded };
  }
  const doc = globalThis.document;
  if (!doc || !command || typeof command !== 'object') return { performed: false, reason: 'rejected' };
  const href = doc.location ? String(doc.location.href || '') : '';
  if (typeof command.origin !== 'string' || !sameOrigin(href) || secretRoute(href)) return { performed: false, reason: 'credential' };
  if (!Number.isFinite(command.deadline) || Date.now() >= command.deadline) return { performed: false, reason: 'expired' };
  // This state lives only in the extension's isolated DOM world, never in the
  // page's world. Keeping actual nodes rejects a same-label replacement at a
  // former path; the observer also invalidates changed attributes or text.
  const stateKey = '__fleetChromeRelayDomSnapshot_v1';
  if (command.op === 'snapshot') {
    const old = globalThis[stateKey];
    if (old && old.observer) old.observer.disconnect();
    if (old && old.observers) for (const observer of old.observers) observer.disconnect();
    const snapshot = globalThis.crypto.randomUUID();
    const state = { document: doc, snapshot: snapshot, nodes: new Map(), readOnly: new Set(), invalid: false, observers: [], expires: Date.now() + 60000 };
    globalThis[stateKey] = state;
    const discovery = readDocuments(doc);
    const textBudget = { visits: 0, bounded: false };
    const nodeBudget = { visits: 0, bounded: false };
    let mainText = '';
    let childText = '';
    let loading = false;
    let urlChars = 0;
    const elements = [];
    for (const entry of discovery.documents) {
      const current = entry.document;
      const child = current !== doc;
      loading = loading || current.readyState !== 'complete' || !current.body;
      const text = safeText(current, textBudget);
      if (!child) mainText = text;
      else if (text) {
        const next = (childText ? childText + ' ' : '') + text;
        if (next.length > 6000) textBudget.bounded = true;
        childText = next.slice(0, 6000);
      }
      const dialogNodes = [];
      const mainNodes = [];
      const bodyNodes = [];
      const seenNodes = new Set();
      function scanWalk(node, depth, currentSection, inheritedVis) {
        if (!node) return;
        if (depth > 30 || nodeBudget.visits >= 2000 || dialogNodes.length >= 500) { nodeBudget.bounded = true; return; }
        if (command && Number.isFinite(command.deadline) && Date.now() >= command.deadline) { nodeBudget.bounded = true; return; }
        nodeBudget.visits += 1;
        let nextVis = inheritedVis || 'visible';
        let nextSection = currentSection;
        if (node.nodeType === 1) {
          if (isSubtreeExcluded(node)) return;
          const explicit = getExplicitVisibility(node);
          if (explicit) nextVis = explicit;
          let isVisible = (nextVis === 'visible');
          if (isVisible && typeof node.checkVisibility === 'function') {
            try {
              if (!node.checkVisibility() && !isDisplayContents(node)) {
                isVisible = false;
                if (explicit !== 'hidden' && nextVis !== 'hidden') return;
              }
            } catch (_) {}
          }
          if (isDialogContainer(node) && !hasExcludedAncestor(node, current.body)) {
            nextSection = 'dialog';
          } else if (isMainContainer(node) && currentSection !== 'dialog' && !hasExcludedAncestor(node, current.body)) {
            nextSection = 'main';
          }
          if (interactive(node) && isVisible && !seenNodes.has(node)) {
            seenNodes.add(node);
            const totalCandidates = dialogNodes.length + mainNodes.length + bodyNodes.length;
            if (nextSection === 'dialog') {
              if (totalCandidates < 500) {
                dialogNodes.push(node);
              } else if (bodyNodes.length > 0) {
                bodyNodes.pop();
                dialogNodes.push(node);
                nodeBudget.bounded = true;
              } else if (mainNodes.length > 0) {
                mainNodes.pop();
                dialogNodes.push(node);
                nodeBudget.bounded = true;
              } else {
                nodeBudget.bounded = true;
              }
            } else if (nextSection === 'main') {
              if (totalCandidates < 500) {
                mainNodes.push(node);
              } else if (bodyNodes.length > 0) {
                bodyNodes.pop();
                mainNodes.push(node);
                nodeBudget.bounded = true;
              } else {
                nodeBudget.bounded = true;
              }
            } else {
              if (totalCandidates < 500) {
                bodyNodes.push(node);
              } else {
                nodeBudget.bounded = true;
              }
            }
          }
          if (isExcludedField(node)) return;
        }
        const children = kids(node);
        if (node.children && node.children.length > children.length) nodeBudget.bounded = true;
        for (let i = 0; i < children.length; i += 1) {
          scanWalk(children[i], depth + 1, nextSection, nextVis);
          if (nodeBudget.visits >= 2000 || dialogNodes.length >= 500) { nodeBudget.bounded = true; break; }
          if (command && Number.isFinite(command.deadline) && Date.now() >= command.deadline) { nodeBudget.bounded = true; break; }
        }
      }
      if (current.body) scanWalk(current.body, 0, 'body', 'visible');
      const nodes = dialogNodes.concat(mainNodes).concat(bodyNodes);
      for (const node of nodes) {
        const kind = inputType(node);
        if (kind === 'password' || kind === 'hidden' || kind === 'file') continue;
        if (isHiddenTree(node)) continue;
        const name = nameOf(node);
        const url = tagOf(node) === 'a' ? safeNavigationUrl(attr(node, 'href'), command.origin, baseOf(node)) : '';
        const gate = targetGate(node);
        // A frameset menu may target a named sibling. Its observed URL is
        // usable through chrome_open; no frame click/type is authorized here.
        const blocked = Boolean(gate && !(child && gate === 'popup' && url));
        if (blocked || !fold(name) || redact(name) !== name) continue;
        if (elements.length >= 40) { nodeBudget.bounded = true; break; }
        const nodeId = String(elements.length);
        state.nodes.set(nodeId, node);
        if (child) state.readOnly.add(nodeId);
        const element = {
          path: pathOf(node), tag: tagOf(node), role: roleOf(node), name: name,
          inputType: kind, autocomplete: attr(node, 'autocomplete'),
          snapshot: snapshot, node_id: nodeId, blocked: false, ambiguous: false
        };
        if (url && urlChars + url.length <= 8192) {
          element.url = url;
          urlChars += url.length;
        } else if (url) nodeBudget.bounded = true;
        elements.push(element);
      }
      if (typeof globalThis.MutationObserver === 'function' && current.body) {
        const observer = new globalThis.MutationObserver(function () { state.invalid = true; });
        observer.observe(current.body, { childList: true, subtree: true, attributes: true, characterData: true });
        state.observers.push(observer);
      }
    }
    const excerpt = [mainText, childText].filter(Boolean).join(' ');
    const truncated = discovery.bounded || textBudget.bounded || nodeBudget.bounded || excerpt.length > 5000;
    const readyState = ['loading', 'interactive', 'complete'].includes(doc.readyState) ? doc.readyState : 'loading';
    const diagnostics = {
      reader_status: loading ? 'loading' : discovery.blocked || truncated ? 'partial' : excerpt || elements.length ? 'ok' : 'empty',
      ready_state: readyState, document_count: discovery.documents.length,
      same_origin_frame_count: discovery.documents.length - 1,
      blocked_frame_count: discovery.blocked,
      main_excerpt_chars: mainText.length, child_excerpt_chars: childText.length
    };
    return { excerpt: excerpt.slice(0, 5000), elements: elements, truncated: truncated, diagnostics: diagnostics };
  }
  if (secretRoute(href) && (command.op === 'click' || command.op === 'type')) {
    return { performed: false, reason: 'credential' };
  }
  if (command.op === 'click' || command.op === 'type') {
    const state = globalThis[stateKey];
    if (!state || state.document !== doc || state.snapshot !== command.snapshot || state.invalid || Date.now() >= state.expires || state.observers.some(function (observer) { return observer.takeRecords().length; })) return { performed: false, reason: 'stale' };
    if (state.readOnly.has(command.node_id)) return { performed: false, reason: 'rejected' };
    const node = resolve(doc, command.path);
    if (!node || state.nodes.get(command.node_id) !== node) return { performed: false, reason: 'stale' };
    if (tagOf(node) !== command.tag || roleOf(node) !== command.role || nameOf(node) !== command.name) {
      return { performed: false, reason: 'stale' };
    }
    if (inputType(node) !== String(command.inputType || '')) return { performed: false, reason: 'stale' };
    const denied = targetGate(node);
    if (denied) return { performed: false, reason: denied };
    if (node.disabled || attr(node, 'aria-disabled') === 'true' || isHiddenTree(node)) return { performed: false, reason: 'rejected' };
    state.invalid = true;
    if (command.op === 'click') {
      if (typeof node.click !== 'function') return { performed: false, reason: 'rejected' };
      node.click();
      return { performed: true };
    }
    if (tagOf(node) !== 'input' && tagOf(node) !== 'textarea') return { performed: false, reason: 'rejected' };
    if (typeof node.focus === 'function') node.focus();
    const constructor = tagOf(node) === 'textarea' ? globalThis.HTMLTextAreaElement : globalThis.HTMLInputElement;
    const setter = constructor && Object.getOwnPropertyDescriptor(constructor.prototype, 'value');
    if (setter && setter.set) setter.set.call(node, String(command.text || ''));
    else node.value = String(command.text || '');
    if (typeof node.dispatchEvent === 'function' && typeof globalThis.Event === 'function') {
      node.dispatchEvent(new globalThis.Event('input', { bubbles: true }));
      node.dispatchEvent(new globalThis.Event('change', { bubbles: true }));
    }
    return { performed: true, length: String(command.text || '').length };
  }
  if (command.op === 'wait') {
    const excerpt = safeText();
    if (command.text && excerpt.toLowerCase().indexOf(String(command.text).toLowerCase()) === -1) {
      return { matched: false };
    }
    if (command.role || command.name) {
      const nodes = [];
      walk(doc.body, nodes, 0, null, true);
      const found = nodes.some(function (node) {
        if (command.role && roleOf(node) !== String(command.role).toLowerCase()) return false;
        if (command.name && fold(nameOf(node)) !== fold(command.name)) return false;
        return true;
      });
      if (!found) return { matched: false };
    }
    return { matched: true };
  }
  return { performed: false, reason: 'rejected' };
}

const api = {
  boot: boot,
  recover: recover,
  installListeners: installListeners,
  pollOnce: pollOnce,
  handleOperation: handleOperation,
  onToolbarClicked: onToolbarClicked,
  onTabCreated: onTabCreated,
  onTabUpdated: onTabUpdated,
  onTabRemoved: onTabRemoved,
  onBrowserStartup: onBrowserStartup,
  fixedDom: fixedDom,
  getAudit: getAudit
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = api;
} else {
  boot({ poll: true }).catch(function () {});
}
