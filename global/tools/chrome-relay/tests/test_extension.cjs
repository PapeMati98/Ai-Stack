'use strict';

// Behavioral tests for the real worker and its fixed DOM function. No npm
// dependency, owner profile, browser account or external network is used here.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto, createHash } = require('node:crypto');

const workerPath = path.join(__dirname, '..', 'extension', 'sw.js');
const workerSource = fs.readFileSync(workerPath, 'utf8');
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'extension', 'manifest.json'), 'utf8'));
const ORIGIN = 'https://app.example.test';
const clean = value => JSON.parse(JSON.stringify(value));
const actions = ['chrome_attachment', 'chrome_open', 'chrome_read', 'chrome_click', 'chrome_type', 'chrome_wait', 'chrome_close'];
const generationFor = config => createHash('sha256').update(JSON.stringify([config.profile_id, config.extension_token, Math.floor(typeof config.expires_at === 'number' ? config.expires_at : Date.parse(config.expires_at))])).digest('hex').slice(0, 32);
const managedGenerationFor = config => createHash('sha256').update(JSON.stringify(['managed/1', config.grant_id, config.profile_id, config.extension_token, config.allowed_origins.slice().sort(), config.allowed_actions.slice().sort(), config.synthetic, config.persistent ? null : Math.floor(config.expires_at)])).digest('hex').slice(0, 32);
const wireFor = (config, id) => (config.schema === 2 ? managedGenerationFor(config) : generationFor(config)) + ':' + createHash('sha256').update(id).digest('hex').slice(0, 32);

// Host permissions ignore ports and paths. Model only the exact-host and '*'
// patterns used by this manifest; browser-compat below checks actual Chromium.
function hostPermissionMatches(pattern, value) {
  const grant = new URL(pattern);
  const url = new URL(value);
  return grant.protocol === url.protocol && (grant.hostname === '*' || grant.hostname === url.hostname);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

class Element {
  constructor(tag, attrs = {}, text = '') {
    this.tagName = tag.toUpperCase();
    this.nodeType = 1;
    this.attrs = { ...attrs };
    this.childNodes = [];
    this.parentElement = null;
    this.ownerDocument = null;
    this.value = attrs.value || '';
    this.clicks = 0;
    this.events = [];
    this.disabled = false;
    this.hidden = false;
    if (text) this.childNodes.push({ nodeType: 3, nodeValue: text });
  }
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  get textContent() { return this.childNodes.map(node => node.nodeType === 3 ? node.nodeValue : node.textContent).join(''); }
  get innerText() { return this.textContent; }
  getAttribute(name) { return Object.hasOwn(this.attrs, name) ? String(this.attrs[name]) : null; }
  setAttribute(name, value) { this.attrs[name] = String(value); this.changed(); }
  append(...nodes) {
    for (const node of nodes) {
      node.parentElement = this;
      this.childNodes.push(node);
      if (node.nodeType === 1) node.adopt(this.ownerDocument);
    }
    this.changed();
    return this;
  }
  adopt(doc) {
    this.ownerDocument = doc;
    for (const node of this.children) node.adopt(doc);
  }
  changed() { if (this.ownerDocument) this.ownerDocument.changed(); }
  replace(old, replacement) {
    const index = this.childNodes.indexOf(old);
    assert.ok(index >= 0);
    replacement.parentElement = this;
    replacement.adopt(this.ownerDocument);
    this.childNodes[index] = replacement;
    old.parentElement = null;
    this.changed();
  }
  click() { this.clicks += 1; if (this.onclick) this.onclick(); }
  focus() { this.ownerDocument.activeElement = this; }
  dispatchEvent(event) { this.events.push(event.type); return true; }
}

function documentFixture(url = ORIGIN + '/home') {
  const doc = {
    location: { href: url },
    readyState: 'complete',
    observers: new Set(),
    activeElement: null,
    changed() { for (const observer of this.observers) observer.records.push({ type: 'mutation' }); }
  };
  const body = new Element('body');
  doc.body = body;
  body.adopt(doc);
  const note = new Element('textarea', { 'aria-label': 'Notes' }, 'FIELD_CANARY_NOT_FOR_READ');
  note.value = 'FIELD_CANARY_NOT_FOR_READ';
  const email = new Element('input', { type: 'email', 'aria-label': 'Contact email', value: 'EMAIL_CANARY_NOT_FOR_READ' });
  const send = new Element('button', { type: 'submit' }, 'Send');
  const submit = new Element('input', { type: 'submit' });
  const form = new Element('form', { action: '/notes' }).append(note, email, send, submit);
  const increment = new Element('button', {}, 'Increment');
  const approve = new Element('button', {}, 'Approve');
  const remove = new Element('button', {}, 'Delete');
  const pay = new Element('button', {}, 'Pay');
  const password = new Element('input', { type: 'password', value: 'PASSWORD_CANARY_NOT_FOR_READ' });
  const username = new Element('input', { 'aria-label': 'User name', autocomplete: 'username' });
  const credentialContinue = new Element('button', { type: 'submit' }, 'Continue');
  const login = new Element('form', { action: '/login' }).append(username, password, credentialContinue);
  const mfa = new Element('input', { 'aria-label': 'Code', autocomplete: 'one-time-code' });
  const oauth = new Element('a', { href: '/oauth/authorize?client_id=new-app' }, 'Continue');
  const oauthSubmit = new Element('button', { type: 'submit' }, 'Continue');
  const consentForm = new Element('form', { action: '/oauth2/authorize' }).append(oauthSubmit);
  const consent = new Element('button', {}, 'Allow access');
  const popup = new Element('button', {}, 'Popup');
  body.append(new Element('p', {}, 'Fixture ready token=SECRET_CANARY_NOT_FOR_READ'),
    form, increment, approve, remove, pay, login, mfa, oauth, consentForm, consent, popup);
  Object.assign(doc, { note, email, send, submit, form, increment, approve, remove, pay, password, username, credentialContinue, mfa, oauth, oauthSubmit, consent, popup });
  return doc;
}

function domRealm(doc, clock = Date) {
  class Observer {
    constructor(callback) { this.callback = callback; this.records = []; }
    observe(target) { this.document = target.ownerDocument; this.document.observers.add(this); }
    disconnect() { if (this.document) this.document.observers.delete(this); this.records = []; }
    takeRecords() { const records = this.records; this.records = []; return records; }
  }
  const getComputedStyle = function(node) {
    if (doc.defaultView && typeof doc.defaultView.getComputedStyle === 'function') {
      return doc.defaultView.getComputedStyle(node);
    }
    if (typeof node.getComputedStyle === 'function') {
      return node.getComputedStyle();
    }
    if (node && node.computedStyle && typeof node.computedStyle === 'object') {
      return node.computedStyle;
    }
    return null;
  };
  const context = vm.createContext({ document: doc, getComputedStyle, URL, Date: clock, crypto: webcrypto, MutationObserver: Observer, Event: class { constructor(type) { this.type = type; } } });
  return {
    call(func, command) {
      context.__command = command;
      return vm.runInContext('(' + func.toString() + ')(__command)', context);
    }
  };
}

const browserSessions = new WeakMap();
function harness(options = {}) {
  const storage = options.storage || {};
  const sessionStorage = options.sessionStorage || browserSessions.get(storage) || {};
  browserSessions.set(storage, sessionStorage);
  const tabs = options.tabs || new Map([
    [1, { id: 1, active: true, windowId: 1, url: ORIGIN + '/home' }],
    [2, { id: 2, active: false, windowId: 1, url: ORIGIN + '/manual-other' }]
  ]);
  const documents = new Map();
  const realms = new Map();
  const calls = { create: [], remove: [], update: [], scripts: [], permissionRequests: [], fetch: [], badges: [], events: [], results: [] };
  const permissions = new Set([...manifest.host_permissions, ...(options.permissions || [ORIGIN])]);
  const permitted = value => [...permissions].some(pattern => hostPermissionMatches(pattern, value));
  const env = { storage, tabs, documents, realms, calls, permissions, nextId: 10, pollResponse: null, failPoll: false, holdScript: null, holdCreate: null, onStorageSet: null, onStorageGet: null, failStorageGet: false, failStorageSet: false, userGesture: false };
  env.sessionStorage = sessionStorage;
  function event() { return { listeners: [], addListener(fn) { this.listeners.push(fn); }, removeListener(fn) { this.listeners = this.listeners.filter(item => item !== fn); } }; }
  let context;
  function inside(value) { return vm.runInContext('JSON.parse(' + JSON.stringify(JSON.stringify(value)) + ')', context); }
  const chrome = {
    storage: { local: {
      async get(key) {
        if (env.failStorageGet) throw new Error('synthetic_storage_get');
        if (env.onStorageGet) await env.onStorageGet(key);
        return inside({ [key]: storage[key] });
      },
      async set(value) {
        if (env.failStorageSet) throw new Error('synthetic_storage_set');
        if (env.onStorageSet) await env.onStorageSet(clean(value));
        const next = { ...storage, ...clean(value) };
        if (Buffer.byteLength(JSON.stringify(next)) > 10 * 1024 * 1024) throw new Error('synthetic_chrome_quota');
        Object.assign(storage, clean(value));
      }
    }, session: {
      async get(key) { return inside({ [key]: sessionStorage[key] }); },
      async set(value) { Object.assign(sessionStorage, clean(value)); }
    } },
    permissions: {
      async contains({ origins }) { return origins.every(permitted); },
      async request({ origins }) {
        calls.permissionRequests.push(clean(origins));
        // Conservatively expire the gesture as soon as the listener returns
        // its promise. An unconditional grant concealed MV3 request ordering.
        if (!env.userGesture) throw new Error('user_gesture_required');
        if (!origins.every(origin => [...manifest.host_permissions, ...manifest.optional_host_permissions].some(pattern => hostPermissionMatches(pattern, origin)))) throw new Error('undeclared_host_permission');
        if (options.onPermissionRequest) await options.onPermissionRequest();
        if (options.permissionDenied === true) return false;
        origins.forEach(origin => permissions.add(origin));
        return true;
      }
    },
    action: { onClicked: event(), async setBadgeText(arg) { calls.badges.push(arg.text); }, async setBadgeBackgroundColor() {} },
    tabs: {
      onCreated: event(), onUpdated: event(), onRemoved: event(),
      async get(id) { if (!tabs.has(id)) throw new Error('missing'); return inside(tabs.get(id)); },
      async query(query) { return inside([...tabs.values()].filter(tab => !query.active || tab.active)); },
      async create(args) {
        calls.create.push(clean(args));
        const tab = { id: env.nextId++, active: args.active, windowId: 1, url: args.url };
        if (Number.isFinite(options.pendingCreateDelayMs)) {
          tab.url = '';
          tab.pendingUrl = args.url;
          tab.status = 'loading';
        }
        tabs.set(tab.id, tab);
        if (Number.isFinite(options.pendingCreateDelayMs)) {
          setTimeout(() => {
            if (!tabs.has(tab.id)) return;
            const committedUrl = typeof options.commitCreateUrl === 'string' ? options.commitCreateUrl : args.url;
            Object.assign(tab, { url: committedUrl, pendingUrl: '', status: 'complete' });
            const update = { url: committedUrl, status: 'complete' };
            for (const listener of chrome.tabs.onUpdated.listeners.slice()) listener(tab.id, update, inside(tab));
          }, options.pendingCreateDelayMs);
        }
        if (env.holdCreate) { env.holdCreate.started.resolve(tab); await env.holdCreate.gate.promise; }
        return inside(tab);
      },
      async update(id, args) { calls.update.push({ id, ...clean(args) }); Object.assign(tabs.get(id), clean(args)); return inside(tabs.get(id)); },
      async remove(id) { calls.remove.push(id); tabs.delete(id); }
    },
    scripting: { async executeScript(spec) {
      assert.equal(spec.func.name, 'fixedDom');
      assert.deepEqual(Object.keys(spec.target), ['tabId']);
      assert.equal(Object.hasOwn(spec, 'code'), false);
      calls.scripts.push({ tab_id: spec.target.tabId, op: spec.args[0].op, origin: spec.args[0].origin });
      const id = spec.target.tabId;
      if (!documents.has(id)) documents.set(id, documentFixture(tabs.get(id).url));
      if (!realms.has(id)) realms.set(id, domRealm(documents.get(id)));
      const result = realms.get(id).call(spec.func, spec.args[0]);
      if (env.holdScript) { env.holdScript.started.resolve(result); await env.holdScript.gate.promise; }
      return inside([{ result }]);
    } },
    alarms: { onAlarm: event(), create() {} },
    runtime: { onStartup: event() }
  };
  const fetch = async (url, init) => {
    if (!permitted(url)) throw new Error('missing_host_permission');
    calls.fetch.push({ url, method: init.method, headers: clean(init.headers), body: init.body });
    if (url.endsWith('/extension/poll')) {
      if (env.failPoll) throw new Error('transport');
      return env.pollResponse || new Response(null, { status: 204 });
    }
    const body = JSON.parse(init.body);
    if (url.endsWith('/extension/event')) {
      calls.events.push(body);
      if (body.type === 'managed_browser_restart') return new Response(JSON.stringify({accepted:true}),
        {status:200,headers:{'Content-Type':'application/json'}});
    }
    if (url.endsWith('/extension/result')) calls.results.push(body);
    return new Response(null, { status: 204 });
  };
  context = vm.createContext({ chrome, fetch, URL, TextEncoder, TextDecoder, AbortController, Uint8Array, Uint32Array, crypto: webcrypto, setTimeout, clearTimeout, module: { exports: {} } });
  vm.runInContext(workerSource, context, { filename: workerPath });
  const api = context.module.exports;
  let sequence = 0;
  const config = {
    schema: 1, profile_id: 'profile-test', extension_token: 'PRIVATE_EXTENSION_CANARY_0123456789', broker_base: 'http://127.0.0.1:9333',
    allowed_origins: [ORIGIN], allowed_actions: actions, session_ids: ['agent-a', 'agent-b'], synthetic: false, expires_at: Date.now() + 120000,
    ...storage.chrome_relay_config,
    ...options.config
  };
  Object.assign(env, {
    api, config, chrome, wire(id) { return wireFor(config, id); },
    async configure() { storage.chrome_relay_config = clean(config); return api.recover(); },
    async attach(id = 1) {
      let pending;
      env.userGesture = true;
      try { pending = api.onToolbarClicked(inside(tabs.get(id))); }
      finally { env.userGesture = false; }
      return clean(await pending);
    },
    async operation(action, args = {}, tabId, overrides = {}) {
      const raw = { request_id: env.wire('req-' + ++sequence), session_id: 'agent-a', profile_id: config.profile_id, action, args, ...overrides };
      if (tabId !== undefined) raw.tab_id = tabId;
      return clean(await api.handleOperation(inside(raw)));
    },
    async raw(raw) { return clean(await api.handleOperation(inside(raw))); },
    async open(sessionId = 'agent-a', url = ORIGIN + '/work') {
      const result = await env.operation('chrome_open', { url }, undefined, { session_id: sessionId });
      assert.equal(result.status, 'ok');
      return result.tab_id;
    },
    async read(id, sid = 'agent-a') { return env.operation('chrome_read', {}, id, { session_id: sid }); },
    async ref(id, name) {
      const result = await env.read(id);
      assert.equal(result.status, 'ok');
      const entry = result.data.refs.find(ref => ref.name === name);
      assert.ok(entry, 'missing ref for ' + name);
      return entry.ref;
    },
    async ready() { await env.configure(); assert.equal((await env.attach()).reason, 'owner_attach'); return env; }
  });
  return env;
}

function emptyDocument(url, tag = 'body') {
  const doc = documentFixture(url);
  doc.body = new Element(tag);
  doc.body.adopt(doc);
  return doc;
}

function frameElement(tag, child) {
  const frame = new Element(tag, { src: child.location.href });
  frame.contentDocument = child;
  return frame;
}

test('chrome_open STOP aborts the URL-commit wait and releases its listener', async () => {
  const h = await harness({ pendingCreateDelayMs: 1000 }).ready();
  const baselineListeners = h.chrome.tabs.onUpdated.listeners.length;
  const pending = h.operation('chrome_open', { url: ORIGIN + '/work' });
  for (let i = 0; i < 100 && h.chrome.tabs.onUpdated.listeners.length <= baselineListeners; i++) {
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.equal(h.chrome.tabs.onUpdated.listeners.length, baselineListeners + 1, 'commit wait must be active before STOP');
  const childId = [...h.tabs.keys()].find(id => id >= 10);
  assert.ok(childId);
  assert.equal((await h.attach()).reason, 'owner_stop');
  const result = await pending;
  assert.equal(result.status, 'unknown');
  assert.equal(result.data.reason, 'owner_stop');
  assert.equal(h.calls.remove.includes(childId), true);
  assert.equal((h.storage.chrome_relay_latch.sessions['agent-a'] || []).includes(childId), false);
  assert.equal(h.chrome.tabs.onUpdated.listeners.length, baselineListeners, 'STOP cleanup must remove the temporary listener');
});
test('chrome_open URL-commit timeout is unknown, cleans up and cannot replay', async () => {
  const h = await harness({ pendingCreateDelayMs: 6500 }).ready();
  const raw = { request_id: h.wire('open-commit-timeout-once'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_open', args: { url: ORIGIN + '/work' } };
  const baselineListeners = h.chrome.tabs.onUpdated.listeners.length;
  const pending = h.raw(raw);
  for (let i = 0; i < 100 && h.tabs.size <= 2; i++) await new Promise(resolve => setTimeout(resolve, 5));
  const childId = [...h.tabs.keys()].find(id => id >= 10);
  assert.ok(childId);
  const first = await pending;
  assert.equal(first.status, 'unknown');
  assert.equal(first.data.reason, 'timeout');
  assert.equal(h.calls.remove.includes(childId), true);
  assert.equal((h.storage.chrome_relay_latch.sessions['agent-a'] || []).includes(childId), false);
  assert.equal(h.chrome.tabs.onUpdated.listeners.length, baselineListeners);
  const createCount = h.calls.create.length;
  const replay = await h.raw(raw);
  assert.equal(replay.status, 'unknown');
  assert.equal(replay.data.reason, 'timeout');
  assert.equal(h.calls.create.length, createCount);
});

test('chrome_open waits for a committed URL before returning a readable owned tab', async () => {
  const h = await harness({ pendingCreateDelayMs: 40 }).ready();
  const began = Date.now();
  const opened = await h.operation('chrome_open', { url: ORIGIN + '/work' });
  assert.equal(opened.status, 'ok', JSON.stringify(opened));
  assert.ok(Date.now() - began >= 30, 'open should wait for the URL commit');
  assert.equal(h.tabs.get(opened.tab_id).url, ORIGIN + '/work');
  assert.equal((await h.read(opened.tab_id)).status, 'ok');
});

test('chrome_open discards a tab that commits outside its approved origin', async () => {
  const h = await harness({ pendingCreateDelayMs: 10, commitCreateUrl: 'https://evil.example.test/work' }).ready();
  const opened = await h.operation('chrome_open', { url: ORIGIN + '/work' });
  assert.equal(opened.status, 'denied');
  assert.equal(opened.data.reason, 'origin');
  assert.equal(h.tabs.size, 2, 'both pre-existing owner tabs remain after discarding the redirected child');
  assert.equal(h.tabs.has(1) && h.tabs.has(2), true);
  assert.equal(h.calls.remove.length, 1);
});

test('same-origin frameset and iframe reads expose child text and a safe observed menu URL', async () => {
  for (const tag of ['frame', 'iframe']) {
    const h = await harness().ready();
    const id = await h.open();
    const root = emptyDocument(ORIGIN + '/work', tag === 'frame' ? 'frameset' : 'body');
    const child = emptyDocument(ORIGIN + '/legacy/menu');
    child.body.append(new Element('p', {}, 'Synthetic frame content'),
      new Element('a', { href: '../documents?view=drafts', target: 'main' }, 'Documents'));
    root.body.append(frameElement(tag, child));
    h.documents.set(id, root);
    const read = await h.read(id);
    assert.equal(read.status, 'ok');
    assert.match(read.data.excerpt, /Synthetic frame content/);
    assert.equal(read.data.refs.find(ref => ref.name === 'Documents').url, ORIGIN + '/documents?view=drafts');
    assert.deepEqual(read.data.diagnostics, {
      reader_status: 'ok', ready_state: 'complete', document_count: 2,
      same_origin_frame_count: 1, blocked_frame_count: 0,
      main_excerpt_chars: 0, child_excerpt_chars: 'Synthetic frame content Documents'.length
    });
    assert.ok(Buffer.byteLength(JSON.stringify(read.data)) <= 4096);
  }
});

test('reader diagnoses ordinary, empty and loading documents without implying authentication', async () => {
  const h = await harness().ready();
  const id = await h.open();
  const ordinary = await h.read(id);
  assert.equal(ordinary.data.diagnostics.reader_status, 'ok');
  assert.equal(ordinary.data.diagnostics.document_count, 1);
  assert.equal(ordinary.data.diagnostics.main_excerpt_chars, ordinary.data.excerpt.length);
  assert.equal(ordinary.data.diagnostics.child_excerpt_chars, 0);
  for (const readyState of ['complete', 'interactive', 'loading']) {
    const emptyId = await h.open();
    const root = emptyDocument(ORIGIN + '/work');
    root.readyState = readyState;
    h.documents.set(emptyId, root);
    const read = await h.read(emptyId);
    assert.equal(read.data.excerpt, '');
    assert.deepEqual(read.data.refs, []);
    assert.equal(read.data.diagnostics.reader_status, readyState === 'complete' ? 'empty' : 'loading');
    assert.equal(read.data.diagnostics.ready_state, readyState);
    root.body = null;
    assert.equal((await h.read(emptyId)).data.diagnostics.reader_status, 'loading');
  }
  const root = h.documents.get(id);
  const child = emptyDocument(ORIGIN + '/loading-child');
  child.readyState = 'loading';
  root.body.append(frameElement('iframe', child));
  const loading = await h.read(id);
  assert.equal(loading.data.diagnostics.ready_state, 'complete');
  assert.equal(loading.data.diagnostics.reader_status, 'loading');
  assert.equal(loading.data.diagnostics.document_count, 2);
});

test('nested same-origin frames contribute; foreign, inaccessible and auth document bodies are never inspected', async () => {
  const h = await harness().ready();
  const id = await h.open();
  const root = emptyDocument(ORIGIN + '/work');
  const child = emptyDocument(ORIGIN + '/child');
  const nested = emptyDocument(ORIGIN + '/nested');
  nested.body.append(new Element('p', {}, 'Nested text token=FRAME_SECRET_CANARY'),
    new Element('input', { value: 'FRAME_INPUT_CANARY', 'aria-label': 'Ordinary field' }),
    new Element('textarea', {}, 'FRAME_TEXTAREA_CANARY'),
    new Element('div', { contenteditable: 'true' }, 'FRAME_EDITABLE_CANARY'));
  child.body.append(new Element('p', {}, 'Child text'), frameElement('iframe', nested));
  root.body.append(new Element('p', {}, 'Root text'), frameElement('frame', child));
  const forbiddenUrls = ['https://foreign.example.test/private', ORIGIN + '/%6cogin', ORIGIN + '/home?CLIENT_ID=app',
    ORIGIN + '/login.php', ORIGIN + '/%4cOGIN', ORIGIN + '/home?%43LIENT_ID=app',
    ORIGIN + '/home#access_token=short', ORIGIN + '/home#/login', ORIGIN + '/home?view=login', 'about:blank', ''];
  for (const url of forbiddenUrls) {
    const forbidden = { location: { href: url } };
    Object.defineProperty(forbidden, 'body', { get() { assert.fail('forbidden body inspection'); } });
    root.body.append(frameElement('iframe', forbidden));
  }
  const inaccessible = new Element('iframe');
  Object.defineProperty(inaccessible, 'contentDocument', { get() { throw new Error('synthetic SOP denial'); } });
  root.body.append(inaccessible, new Element('iframe'));
  h.documents.set(id, root);
  const read = await h.read(id);
  assert.equal(read.status, 'ok');
  assert.match(read.data.excerpt, /Root text Child text Nested text \[redacted\]/);
  assert.equal(read.data.diagnostics.reader_status, 'partial');
  assert.equal(read.data.diagnostics.document_count, 3);
  assert.equal(read.data.diagnostics.same_origin_frame_count, 2);
  assert.equal(read.data.diagnostics.blocked_frame_count, forbiddenUrls.length + 2);
  assert.deepEqual(Object.keys(read.data.diagnostics), ['reader_status', 'ready_state', 'document_count',
    'same_origin_frame_count', 'blocked_frame_count', 'main_excerpt_chars', 'child_excerpt_chars']);
  for (const canary of ['FRAME_SECRET_CANARY', 'FRAME_INPUT_CANARY', 'FRAME_TEXTAREA_CANARY', 'FRAME_EDITABLE_CANARY', 'foreign.example.test']) {
    assert.equal(JSON.stringify(read.data).includes(canary), false);
  }
});

test('text clipping cannot expose a shortened credential-looking value in a frame', async () => {
  const h = await harness().ready();
  const id = await h.open();
  const root = emptyDocument(ORIGIN + '/work');
  const child = emptyDocument(ORIGIN + '/child');
  child.body.append(new Element('p', {}, 'T'.repeat(5969) + ' ' + 'a'.repeat(80)));
  root.body.append(frameElement('iframe', child));
  h.documents.set(id, root);
  const read = await h.read(id);
  assert.equal(read.status, 'ok');
  assert.equal(read.data.excerpt, '[redacted] [redacted]');
  assert.equal(read.data.truncated, true);
  assert.equal(read.data.diagnostics.reader_status, 'partial');
});

test('anchor URLs resolve against their document base; risky URLs are omitted whole and non-anchors never supply URLs', async () => {
  const h = await harness().ready();
  const id = await h.open();
  const root = emptyDocument(ORIGIN + '/work');
  const child = emptyDocument(ORIGIN + '/legacy/menu');
  child.baseURI = ORIGIN + '/base/';
  child.body.append(new Element('a', { href: 'documents?view=drafts', target: 'main' }, 'Safe menu'),
    new Element('a', { href: '#section' }, 'Safe section'),
    new Element('button', { href: '/documents' }, 'Button'));
  const unsafe = [
    '/login', '/oauth/authorize?client_id=app', '/consent', '/%6cogin', '/home?CLIENT_ID=app',
    '/docs?token=short', '/docs?accessToken=short', '/docs?%74oken=short', '/docs?%2574oken=short',
    '/docs?%252574oken=short', '/docs?apiKey=short', '/docs#password=short', '/docs#Bearer%20value',
    '/docs#eyJabcdefghijk.abcdefghijk.abcdefgh', '/docs?v=' + 'a'.repeat(64),
    '/docs?v=' + 'Q'.repeat(45), '/docs?v=sk-syntheticvalue', '/docs?view=%20drafts',
    '/docs?bad=%zz', 'https://user:pass@app.example.test/docs', 'https://foreign.example.test/docs',
    'javascript:alert(1)', 'data:text/plain,secret', '/docs#authorization=short'
  ];
  unsafe.forEach((href, index) => child.body.append(new Element('a', { href }, 'Risk ' + index)));
  root.body.append(frameElement('iframe', child));
  h.documents.set(id, root);
  const read = await h.read(id);
  assert.equal(read.status, 'ok');
  assert.equal(read.data.refs.find(ref => ref.name === 'Safe menu').url, ORIGIN + '/base/documents?view=drafts');
  assert.equal(read.data.refs.find(ref => ref.name === 'Safe section').url, ORIGIN + '/base/#section');
  assert.equal(read.data.refs.find(ref => ref.name === 'Button').url, undefined);
  unsafe.forEach((href, index) => assert.equal(read.data.refs.find(ref => ref.name === 'Risk ' + index)?.url, undefined, href));
  for (const ref of read.data.refs) assert.ok(Object.keys(ref).every(key => ['ref', 'role', 'name', 'url'].includes(key)));
  const observed = read.data.refs.find(ref => ref.name === 'Safe menu').url;
  const opened = await h.operation('chrome_open', { url: observed });
  assert.equal(opened.status, 'ok');
  assert.equal(h.calls.create.at(-1).url, observed);
  assert.equal(h.calls.update.length, 0);
});

test('frame references never mutate even on fresh, navigated, replaced, removed or changed documents', async () => {
  const changes = [
    () => {},
    (root, child) => { child.location.href = ORIGIN + '/new-location'; },
    (root, child) => { child.location.href = 'https://foreign.example.test/private'; },
    (root, child) => { child.location.href = ORIGIN + '/login'; },
    (root, child, frame) => { frame.contentDocument = emptyDocument(ORIGIN + '/replacement'); },
    (root, child, frame) => root.body.replace(frame, frameElement('iframe', emptyDocument(ORIGIN + '/replacement'))),
    (root, child, frame) => root.body.replace(frame, new Element('div')),
    (root, child) => child.body.append(new Element('p', {}, 'Changed child')),
    root => root.body.append(new Element('p', {}, 'Changed root'))
  ];
  for (const [index, change] of changes.entries()) for (const action of ['chrome_click', 'chrome_type']) {
    const h = await harness().ready();
    const id = await h.open();
    const root = emptyDocument(ORIGIN + '/work');
    const child = emptyDocument(ORIGIN + '/child');
    const mainButton = new Element('button', {}, 'Frame effect');
    const childButton = new Element('button', {}, 'Frame effect');
    const input = new Element('input', { 'aria-label': 'Frame field' });
    child.body.append(childButton, input);
    const frame = frameElement('iframe', child);
    root.body.append(mainButton, frame);
    h.documents.set(id, root);
    const read = await h.read(id);
    const ref = action === 'chrome_click'
      ? read.data.refs.filter(ref => ref.name === 'Frame effect').at(-1).ref
      : read.data.refs.find(ref => ref.name === 'Frame field').ref;
    change(root, child, frame);
    const args = action === 'chrome_type' ? { ref, text: 'Must not type' } : { ref };
    assert.equal((await h.operation(action, args, id)).status, 'denied', action + '/' + index);
    assert.equal(mainButton.clicks, 0, 'no colliding main-body path mutation');
    assert.equal(childButton.clicks, 0);
    assert.equal(input.value, '');
    assert.deepEqual(input.events, []);
  }
});

test('snapshot expiry, child mutations and repeat reads invalidate references without retaining observers', async () => {
  const h = harness();
  const root = emptyDocument(ORIGIN + '/work');
  const child = emptyDocument(ORIGIN + '/child');
  const button = new Element('button', {}, 'Main effect');
  root.body.append(button, frameElement('iframe', child));
  let now = Date.now();
  const realm = domRealm(root, class extends Date { static now() { return now; } });
  const snapshot = () => realm.call(h.api.fixedDom, { op: 'snapshot', origin: ORIGIN, deadline: now + 5000 });
  const click = (element, deadline = now + 5000) => realm.call(h.api.fixedDom, { ...element, op: 'click', origin: ORIGIN, deadline });
  const expired = snapshot().elements[0];
  now += 60001;
  assert.equal(click(expired).reason, 'stale');
  const changed = snapshot().elements[0];
  child.body.append(new Element('p', {}, 'Changed child'));
  assert.equal(click(changed).reason, 'stale');
  const old = snapshot().elements[0];
  const fresh = snapshot().elements[0];
  assert.equal(root.observers.size, 1);
  assert.equal(child.observers.size, 1);
  assert.equal(click(old).reason, 'stale');
  assert.equal(click(fresh, now - 1).reason, 'expired');
  assert.equal(button.clicks, 0);
});

test('document/frame/depth/node/UTF8 limits preserve bounded count-only diagnostics and redaction', async () => {
  const h = await harness().ready();
  for (const shape of ['frames', 'depth', 'nodes', 'bytes']) {
    const id = await h.open();
    const root = emptyDocument(ORIGIN + '/work');
    if (shape === 'frames') {
      for (let index = 0; index < 100; index++) {
        const child = emptyDocument(ORIGIN + '/child-' + index);
        child.body.append(new Element('p', {}, 'Child ' + index));
        root.body.append(frameElement('iframe', child));
      }
    } else if (shape === 'depth') {
      let parent = root;
      for (let index = 0; index < 20; index++) {
        const child = emptyDocument(ORIGIN + '/depth-' + index);
        child.body.append(new Element('p', {}, 'Depth ' + index));
        parent.body.append(frameElement('iframe', child));
        parent = child;
      }
    } else if (shape === 'nodes') {
      for (let index = 0; index < 3000; index++) root.body.append(new Element('div'));
      const lateFrame = new Element('iframe');
      Object.defineProperty(lateFrame, 'contentDocument', { get() { assert.fail('node discovery exceeded budget'); } });
      root.body.append(lateFrame);
    } else {
      const child = emptyDocument(ORIGIN + '/child');
      for (let index = 0; index < 40; index++) child.body.append(new Element('a', { href: '/safe-segment'.repeat(140) + '/menu-' + index }, 'Menu ' + index));
      child.body.append(new Element('p', {}, "public-member-fixture-1-Ω".repeat(5000)));
      root.body.append(new Element('p', {}, 'token=BOUNDED_SECRET_CANARY'), frameElement('iframe', child));
    }
    h.documents.set(id, root);
    const read = await h.read(id);
    assert.equal(read.status, 'ok', shape);
    assert.equal(read.data.diagnostics.reader_status, 'partial', shape);
    assert.equal(read.data.truncated, true, shape);
    assert.ok(Buffer.byteLength(JSON.stringify(read.data)) <= 4096, shape);
    assert.ok(read.data.refs.length <= 40);
    for (const key of ['document_count', 'same_origin_frame_count', 'blocked_frame_count']) {
      assert.ok(Number.isInteger(read.data.diagnostics[key]) && read.data.diagnostics[key] >= 0 && read.data.diagnostics[key] <= 64, key);
    }
    for (const key of ['main_excerpt_chars', 'child_excerpt_chars']) assert.ok(read.data.diagnostics[key] <= 6000, key);
    if (shape === 'frames') assert.equal(read.data.diagnostics.document_count, 64);
    if (shape === 'depth') assert.equal(read.data.diagnostics.document_count, 9);
    if (shape === 'bytes') assert.ok(read.data.refs.some(ref => ref.url), 'a retained safe URL participates in the byte budget');
    assert.equal(JSON.stringify(read.data).includes('BOUNDED_SECRET_CANARY'), false);
  }
});

test('frame reader retains STOP, ownership, origin, caller-JS and late-response guards', async () => {
  const h = await harness({ config: { allowed_actions: ['chrome_attachment', 'chrome_open', 'chrome_read', 'chrome_close'] } }).ready();
  const id = await h.open();
  const root = emptyDocument(ORIGIN + '/work');
  const child = emptyDocument(ORIGIN + '/child');
  child.body.append(new Element('a', { href: '/menu' }, 'Menu'));
  root.body.append(frameElement('iframe', child));
  h.documents.set(id, root);
  const read = await h.read(id);
  const ref = read.data.refs[0].ref;
  const calls = h.calls.scripts.length;
  for (const action of ['chrome_click', 'chrome_type']) assert.equal((await h.operation(action, action === 'chrome_type' ? { ref, text: 'denied' } : { ref }, id)).data.reason, 'action');
  assert.equal((await h.read(id, 'agent-b')).data.reason, 'ownership');
  assert.equal((await h.read(1)).data.reason, 'ownership');
  assert.equal((await h.operation('chrome_read', { code: 'caller JS' }, id)).data.reason, 'unknown_field');
  assert.equal(h.calls.scripts.length, calls);
  const held = { started: deferred(), gate: deferred() };
  h.holdScript = held;
  const pending = h.read(id);
  await held.started.promise;
  await h.attach();
  held.gate.resolve();
  const late = await pending;
  assert.equal(late.status, 'unknown');
  assert.equal(late.data.reason, 'stopped');
  assert.equal(late.data.excerpt, undefined);
  assert.equal((await h.read(id)).data.reason, 'stopped');
  h.holdScript = null;
  await h.attach();
  root.location.href = 'https://foreign.example.test/private';
  assert.equal((await h.read(id)).status, 'denied');
  root.location.href = ORIGIN + '/login';
  assert.equal((await h.read(id)).status, 'denied');
});

test('manifest keeps debugger required, HTTPS host scope optional and broad API permissions absent', () => {
  assert.equal(manifest.version, '1.2.0');
  assert.deepEqual(manifest.permissions, ['alarms', 'scripting', 'storage', 'tabs', 'debugger']);
  assert.deepEqual(manifest.host_permissions, ['http://127.0.0.1/*', 'http://localhost/*']);
  assert.deepEqual(manifest.optional_host_permissions, ['https://*/*']);
  assert.equal((manifest.optional_permissions || []).includes('debugger'), false);
  assert.equal(manifest.permissions.includes('identity'), false);
  assert.equal(manifest.permissions.includes('<all_urls>'), false);
  assert.equal(manifest.incognito, 'not_allowed');
});

test('declared loopback host permission covers explicit broker ports without an optional HTTP grant', async () => {
  for (const port of [9333, 49152, 65535]) {
    const base = 'http://127.0.0.1:' + port;
    const h = await harness({ config: { broker_base: base } }).ready();
    assert.equal(await h.chrome.permissions.contains({ origins: [base + '/*'] }), true);
    assert.equal(await h.chrome.permissions.contains({ origins: ['http://external.example.test:' + port + '/*'] }), false);
    assert.equal((await h.api.pollOnce()).polled, true);
    await h.open();
    for (const endpoint of ['poll', 'result', 'event']) {
      assert.ok(h.calls.fetch.some(call => call.url === base + '/extension/' + endpoint));
    }
    assert.ok(h.calls.permissionRequests.every(origins => origins.every(origin => origin.startsWith('https://'))));
  }
});

test('missing/invalid private policy stays STOP; production HTTP is not accepted', async () => {
  for (const config of [{ session_ids: undefined }, { profile_id: '' }, { synthetic: false, allowed_origins: ['http://127.0.0.1:9000'] }, { expires_at: 'not-a-date' }, { extra: true }]) {
    const h = harness({ config });
    assert.equal((await h.configure()).ok, false);
    assert.equal((await h.attach()).status, 'denied');
    assert.equal(h.storage.chrome_relay_latch.stop, true);
    assert.equal(h.calls.create.length, 0);
  }
});

test('only active owner attach requests an exact policy origin permission', async () => {
  const h = harness({ permissions: [] });
  await h.configure();
  assert.equal((await h.attach(2)).reason, 'ownership');
  assert.equal(h.calls.permissionRequests.length, 0);
  assert.equal((await h.attach()).reason, 'owner_attach');
  assert.deepEqual(h.calls.permissionRequests, [[ORIGIN + '/*']]);
  await h.open();
  assert.equal(h.calls.permissionRequests.length, 1, 'agent operations never request host grants');
  assert.equal(h.calls.update.length, 0);
  assert.equal((await h.attach()).reason, 'owner_stop');
  assert.equal(h.calls.permissionRequests.length, 1, 'STOP must never request a host grant');
});

test('permission refusal and policy-ineligible toolbar tabs stay STOP without granting access', async () => {
  const denied = harness({ permissions: [], permissionDenied: true });
  await denied.configure();
  assert.equal((await denied.attach()).reason, 'permission');
  assert.equal(denied.storage.chrome_relay_latch.stop, true);
  assert.equal(denied.calls.badges.at(-1), 'STOP');
  assert.equal((await denied.operation('chrome_open', { url: ORIGIN + '/work' })).status, 'denied');
  for (const url of ['https://foreign.example.test/', ORIGIN + ':444/other-port', ORIGIN + '/oauth/authorize', 'http://127.0.0.1:9000/']) {
    const h = harness({ permissions: [] });
    await h.configure();
    h.tabs.get(1).url = url;
    assert.equal((await h.attach()).status, 'denied');
    assert.equal(h.calls.permissionRequests.length, 0);
    assert.equal(h.storage.chrome_relay_latch.stop, true);
  }
});

test('toolbar revalidates the active tab origin after a delayed host permission grant', async () => {
  const grant = deferred();
  const h = harness({ permissions: [], onPermissionRequest: () => grant.promise });
  await h.configure();
  const pending = h.attach();
  assert.deepEqual(h.calls.permissionRequests, [[ORIGIN + '/*']], 'request must occur before any await');
  h.tabs.get(1).url = 'https://foreign.example.test/changed';
  grant.resolve();
  assert.equal((await pending).status, 'denied');
  assert.equal(h.storage.chrome_relay_latch.stop, true);
  assert.equal(h.calls.badges.at(-1), 'STOP');
  assert.equal(h.calls.events.some(event => event.type === 'attach'), false);
});

test('a delayed host grant cannot lift STOP after worker recovery or policy expiry', async () => {
  for (const invalidate of ['recover', 'expire', 'inactive', 'other_policy_origin']) {
    const grant = deferred();
    const h = harness({ permissions: [], onPermissionRequest: () => grant.promise,
      config: { allowed_origins: [ORIGIN, 'https://other.example.test'] } });
    await h.configure();
    const pending = h.attach();
    assert.equal(h.calls.permissionRequests.length, 1);
    if (invalidate === 'recover') await h.api.recover();
    if (invalidate === 'expire') {
      h.config.expires_at = Date.now() - 1;
      await h.configure();
    }
    if (invalidate === 'inactive') h.tabs.get(1).active = false;
    if (invalidate === 'other_policy_origin') h.tabs.get(1).url = 'https://other.example.test/';
    grant.resolve();
    assert.equal((await pending).status, 'denied', invalidate);
    assert.equal(h.storage.chrome_relay_latch.stop, true);
    assert.equal(h.calls.events.some(event => event.type === 'attach'), false);
  }
});

// Explicit focused compatibility check, not browser/user-journey UAT. No npm
// installation, owner profile, accounts, external network or profile deletion.
// All paths are supplied by the parent; keep the fresh synthetic profile as
// evidence under this task's ignored artifact directory.
const compatIndex = process.argv.indexOf('--browser-compat');
if (compatIndex !== -1) test('actual synthetic Chromium reads loopback responses on an explicit port without CORS', { timeout: 45000 }, async () => {
  const http = require('node:http');
  const { randomUUID } = require('node:crypto');
  const [playwrightPath, browserPath, artifactRoot] = process.argv.slice(compatIndex + 1);
  assert.ok(playwrightPath && browserPath && artifactRoot, 'provide local Playwright, Chromium and task artifact paths');
  const workspace = path.resolve(__dirname, '../../../..');
  const relative = path.relative(workspace, path.resolve(artifactRoot));
  assert.ok(relative && !relative.startsWith('..') && !path.isAbsolute(relative), 'artifacts must stay inside task worktree');
  const profile = path.join(artifactRoot, 'extension-compat-' + randomUUID());
  fs.mkdirSync(profile, { recursive: true });
  const server = http.createServer((request, response) => {
    if (!/^\/extension\/(poll|result|event)$/.test(request.url)) { response.writeHead(404).end(); return; }
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end('{"synthetic":true}'); // Deliberately no Access-Control-Allow-Origin.
  });
  let context;
  try {
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
    const base = 'http://127.0.0.1:' + server.address().port;
    // Use the same pinned persistent-context launcher as the integration UAT.
    // Playwright hides native children on Windows; this test is additionally
    // launched by the parent's private-desktop background helper.
    context = await require(playwrightPath).chromium.launchPersistentContext(profile, {
      executablePath: browserPath, headless: true, timeout: 25000,
      args: ['--disable-extensions-except=' + path.dirname(workerPath), '--load-extension=' + path.dirname(workerPath)]
    });
    const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker', { timeout: 15000 });
    const checked = await worker.evaluate(async base => {
      const granted = await chrome.permissions.contains({ origins: [base + '/*'] });
      const external = await chrome.permissions.contains({ origins: ['http://external.example.test/*'] });
      const responses = [];
      for (const name of ['poll', 'result', 'event']) {
        const response = await fetch(base + '/extension/' + name, {
          method: name === 'poll' ? 'GET' : 'POST', credentials: 'omit',
          headers: { 'Authorization': ['Synthetic', ['PUBLIC', 'COMPAT', 'CREDENTIAL', '0001', 'Z8Q4R6P2'].join('_')].join(' '), 'X-Profile-Id': 'synthetic-compat', 'Content-Type': 'application/json' },
          body: name === 'poll' ? undefined : '{}'
        });
        responses.push({ status: response.status, data: await response.json() });
      }
      return { granted, external, responses };
    }, base);
    assert.equal(checked.granted, true);
    assert.equal(checked.external, false);
    assert.deepEqual(checked.responses, Array.from({ length: 3 }, () => ({ status: 200, data: { synthetic: true } })));
    const quota = await worker.evaluate(async () => {
      await recover();
      const latch = defaultLatch();
      latch.generation = '1234567890abcdef1234567890abcdef';
      latch.stop = false;
      latch.authorized = true;
      for (let index = 0; index < 10000; index++) {
        const id = latch.generation + ':' + index.toString(16).padStart(32, '0');
        latch.completed_order.push(id);
        latch.completed[id] = { status: 'unknown', session_id: 's'.repeat(128), action: 'chrome_close', length: 1000, reason: 'a'.repeat(64), tab_id: 2147483647 };
      }
      await chrome.storage.local.set({ chrome_relay_latch: latch });
      await stopLatch('owner_stop');
      const persisted = (await chrome.storage.local.get('chrome_relay_latch')).chrome_relay_latch;
      return { stopped: persisted.stop, count: persisted.completed_order.length, bytes: await chrome.storage.local.getBytesInUse(null), quota: chrome.storage.local.QUOTA_BYTES };
    });
    assert.equal(quota.stopped, true);
    assert.equal(quota.count, 10000);
    assert.ok(quota.bytes < quota.quota - 2 * 1024 * 1024, 'real Chrome storage must leave STOP headroom');
  } finally {
    if (context) await context.close().catch(() => {});
    await new Promise(resolve => server.close(resolve));
  }
});

test('malformed envelopes, unknown fields, missing profile and unauthorized session never reach DOM or tabs', async () => {
  const h = await harness().ready();
  const base = { request_id: h.wire('bad-1'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_open', args: { url: ORIGIN + '/work' } };
  for (const changes of [{ profile_id: undefined }, { profile_id: 'other' }, { session_id: 'intruder' }, { action: 'Runtime.evaluate' }, { args: { url: ORIGIN + '/', code: 'SECRET_CANARY' } }, { extra: 'SECRET_CANARY' }, { toString: 'SECRET_CANARY' }, { valueOf: 'SECRET_CANARY' }, { args: [] }, { request_id: 42 }]) {
    assert.equal((await h.raw({ ...base, ...changes })).status, 'denied');
  }
  assert.equal((await h.raw(JSON.parse('{"request_id":"bad-2","session_id":"agent-a","profile_id":"profile-test","action":"chrome_open","args":{"url":"https://app.example.test/","__proto__":{}}}'))).status, 'denied');
  assert.equal((await h.raw({ ...base, args: { url: 'x'.repeat(65536) } })).status, 'denied');
  assert.equal(h.calls.create.length, 0);
  assert.equal(h.calls.scripts.length, 0);
});

test('each Fleet session owns inactive tabs; manual and other sessions remain inaccessible', async () => {
  const h = await harness().ready();
  const a = await h.open('agent-a');
  const b = await h.open('agent-b');
  assert.notEqual(a, b);
  for (const id of [1, 2, b]) {
    for (const [action, args] of [['chrome_read', {}], ['chrome_wait', { text: 'ready' }], ['chrome_click', { ref: 'r1_0' }], ['chrome_type', { ref: 'r1_0', text: 'x' }], ['chrome_close', {}]]) {
      const result = await h.operation(action, args, id);
      assert.equal(result.status, 'denied');
      assert.equal(result.data.reason, 'ownership');
    }
  }
  assert.equal((await h.read(a)).status, 'ok');
  assert.equal((await h.read(b, 'agent-b')).status, 'ok');
  assert.ok(h.calls.create.every(call => call.active === false));
  assert.equal(h.tabs.get(1).active, true);
  assert.equal(h.calls.update.length, 0);
  assert.equal(h.calls.remove.length, 0);
});

test('Send, Submit, Approve, Delete and Pay work autonomously in the approved ordinary scope', async () => {
  const h = await harness().ready();
  const id = await h.open();
  for (const name of ['Send', 'Submit', 'Approve', 'Delete', 'Pay']) {
    const ref = await h.ref(id, name);
    const result = await h.operation('chrome_click', { ref }, id);
    assert.equal(result.status, 'ok', name);
    assert.deepEqual(result.data, { clicked: true });
  }
  const ref = await h.ref(id, 'Contact email');
  const typed = await h.operation('chrome_type', { ref, text: 'ordinary@example.test' }, id);
  assert.equal(typed.status, 'ok');
  assert.equal(h.documents.get(id).email.value, 'ordinary@example.test');
  assert.deepEqual(h.documents.get(id).email.events, ['input', 'change']);
  assert.equal(h.calls.permissionRequests.length, 1, 'ordinary operations add no requests after owner attach');
  assert.equal(h.tabs.get(1).active, true);
});

test('password/MFA/login form and new OAuth links/forms/consent are blocked before any native effect', async () => {
  const h = await harness().ready();
  const id = await h.open();
  const read = await h.read(id);
  const names = read.data.refs.map(ref => ref.name);
  assert.equal(names.includes('User name'), false);
  assert.equal(names.includes('Code'), false);
  assert.equal(names.includes('Continue'), false);
  assert.equal(names.includes('Allow access'), false);
  for (const url of [ORIGIN + '/oauth/authorize?client_id=new', ORIGIN + '/mfa', 'https://accounts.google.com/']) {
    assert.equal((await h.operation('chrome_open', { url })).status, 'denied');
  }
  const doc = h.documents.get(id);
  for (const node of [doc.username, doc.password, doc.mfa, doc.credentialContinue, doc.oauth, doc.oauthSubmit, doc.consent]) {
    assert.equal(node.clicks, 0);
  }
  const safe = await h.ref(id, 'Send');
  doc.form.setAttribute('action', '/oauth2/authorize?client_id=new');
  assert.equal((await h.operation('chrome_click', { ref: safe }, id)).status, 'denied');
  assert.equal(doc.send.clicks, 0);
  const fresh = await h.read(id);
  assert.equal(fresh.data.refs.some(ref => ref.name === 'Send'), false);
});

test('fresh references reject changed DOM, same-label replacements and prior snapshots', async () => {
  const h = await harness().ready();
  const id = await h.open();
  const old = await h.ref(id, 'Increment');
  const current = await h.ref(id, 'Increment');
  assert.notEqual(old, current);
  assert.equal((await h.operation('chrome_click', { ref: old }, id)).data.reason, 'stale_ref');
  const doc = h.documents.get(id);
  const replacement = new Element('button', {}, 'Increment');
  doc.body.replace(doc.increment, replacement);
  assert.equal((await h.operation('chrome_click', { ref: current }, id)).status, 'denied');
  assert.equal(replacement.clicks, 0);
  const next = await h.ref(id, 'Increment');
  replacement.setAttribute('aria-label', 'Changed');
  assert.equal((await h.operation('chrome_click', { ref: next }, id)).status, 'denied');
  assert.equal(replacement.clicks, 0);
});

test('STOP cancels a dispatched operation; late completion and owner reattach cannot replay it', async () => {
  const h = await harness().ready();
  const id = await h.open();
  const ref = await h.ref(id, 'Increment');
  const held = { started: deferred(), gate: deferred() };
  h.holdScript = held;
  const raw = { request_id: h.wire('late-click'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_click', args: { ref }, tab_id: id };
  const pending = h.raw(raw);
  await held.started.promise;
  assert.equal(h.documents.get(id).increment.clicks, 1, 'effect was dispatched before STOP');
  assert.equal((await h.attach()).reason, 'owner_stop');
  held.gate.resolve();
  assert.equal((await pending).status, 'unknown');
  h.holdScript = null;
  assert.equal(h.storage.chrome_relay_latch.stop, true);
  assert.equal(h.storage.chrome_relay_latch.inflight.length, 0);
  await h.api.recover();
  assert.equal((await h.operation('chrome_attachment')).data.stopped, true);
  assert.equal(h.calls.badges.at(-1), 'STOP');
  assert.equal((await h.attach()).reason, 'owner_attach');
  assert.equal((await h.raw(raw)).status, 'unknown');
  assert.equal(h.documents.get(id).increment.clicks, 1);
  assert.equal((await h.raw({ ...raw, session_id: 'agent-b' })).status, 'denied');
});

test('late tab creation after STOP cannot adopt ownership or close manual/foreign tabs', async () => {
  const h = await harness().ready();
  const held = { started: deferred(), gate: deferred() };
  h.holdCreate = held;
  const pending = h.operation('chrome_open', { url: ORIGIN + '/slow-create' });
  const created = await held.started.promise;
  await h.attach();
  held.gate.resolve();
  assert.equal((await pending).status, 'unknown');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal((h.storage.chrome_relay_latch.sessions['agent-a'] || []).includes(created.id), false);
  assert.ok(h.calls.remove.includes(created.id));
  assert.equal(h.tabs.has(1), true);
  assert.equal(h.tabs.has(2), true);
  assert.equal(h.calls.update.length, 0);
});

test('STOP racing the dispatch journal prevents the native effect before it starts', async () => {
  const h = await harness().ready();
  const journaled = deferred();
  const release = deferred();
  h.onStorageSet = async value => {
    const latch = value.chrome_relay_latch;
    if (latch && latch.inflight.some(item => item.request_id === h.wire('before-effect') && item.dispatched)) {
      journaled.resolve();
      await release.promise;
    }
  };
  const pending = h.raw({ request_id: h.wire('before-effect'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_open', args: { url: ORIGIN + '/work' } });
  await journaled.promise;
  const stopping = h.attach();
  release.resolve();
  await stopping;
  assert.notEqual((await pending).status, 'ok');
  assert.equal(h.calls.create.length, 0);
  assert.equal(h.storage.chrome_relay_latch.stop, true);
});

test('expiry during an in-flight mutation persists STOP and retains truthful unknown', async () => {
  const h = await harness({ config: { expires_at: Date.now() + 250 } }).ready();
  const id = await h.open();
  const ref = await h.ref(id, 'Increment');
  const held = { started: deferred(), gate: deferred() };
  h.holdScript = held;
  const pending = h.raw({ request_id: h.wire('expiry-click'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_click', args: { ref }, tab_id: id });
  await held.started.promise;
  await new Promise(resolve => setTimeout(resolve, 300));
  held.gate.resolve();
  assert.equal((await pending).status, 'unknown');
  assert.equal(h.storage.chrome_relay_latch.stop, true);
  assert.equal(h.storage.chrome_relay_latch.completed[h.wire('expiry-click')].reason, 'expired');
  assert.equal(h.calls.badges.at(-1), 'STOP');
  assert.equal((await h.attach()).status, 'denied');
});

test('wait requires an observable condition and reports actual matches; loopback opens require synthetic policy', async () => {
  const h = await harness().ready();
  const id = await h.open();
  assert.equal((await h.operation('chrome_wait', {}, id)).status, 'denied');
  assert.equal((await h.operation('chrome_wait', { text: ' ' }, id)).status, 'denied');
  assert.equal((await h.operation('chrome_wait', { selector: '#any' }, id)).status, 'denied');
  assert.equal((await h.operation('chrome_wait', { role: 'button', name: 'Increment', text: 'Fixture ready' }, id)).data.matched, true);
  assert.deepEqual((await h.operation('chrome_wait', { text: 'absent' }, id)).data, { matched: false, reason: 'not_observed' });
  assert.equal((await h.operation('chrome_open', { url: 'http://127.0.0.1:9000/work' })).status, 'denied');
  const origin = 'http://127.0.0.1:9000';
  const synthetic = harness({ config: { synthetic: true, allowed_origins: [origin] }, permissions: [origin] });
  synthetic.tabs.get(1).url = origin + '/manual';
  await synthetic.ready();
  assert.equal((await synthetic.operation('chrome_open', { url: origin + '/work' })).status, 'ok');
});

test('native response timeout is unknown and stable request_id never repeats the mutation', async () => {
  const h = await harness().ready();
  const id = await h.open();
  const ref = await h.ref(id, 'Increment');
  const held = { started: deferred(), gate: deferred() };
  h.holdScript = held;
  const raw = { request_id: h.wire('timeout-click'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_click', args: { ref }, tab_id: id };
  const pending = h.raw(raw);
  await held.started.promise;
  assert.equal((await pending).status, 'unknown');
  held.gate.resolve();
  h.holdScript = null;
  assert.equal((await h.raw(raw)).status, 'unknown');
  assert.equal(h.documents.get(id).increment.clicks, 1);
  assert.equal(h.storage.chrome_relay_latch.inflight.length, 0);
});

test('more than 50 lifetime mutations remain autonomous without losing replay protection across STOP and restarts', async () => {
  const h = await harness().ready();
  const firstOpen = { request_id: h.wire('retained-open'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_open', args: { url: ORIGIN + '/work' } };
  const opened = await h.raw(firstOpen);
  assert.equal(opened.status, 'ok');
  const id = opened.tab_id;
  const firstClick = { request_id: h.wire('retained-click'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_click', tab_id: id, args: { ref: await h.ref(id, 'Increment') } };
  assert.equal((await h.raw(firstClick)).status, 'ok');
  const firstType = { request_id: h.wire('retained-type'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_type', tab_id: id, args: { ref: await h.ref(id, 'Notes'), text: 'retained' } };
  assert.equal((await h.raw(firstType)).status, 'ok');
  const unknownClick = { request_id: h.wire('retained-unknown'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_click', tab_id: id, args: { ref: await h.ref(id, 'Increment') } };
  const held = { started: deferred(), gate: deferred() };
  h.holdScript = held;
  const pending = h.raw(unknownClick);
  await held.started.promise;
  assert.equal((await h.attach()).reason, 'owner_stop');
  held.gate.resolve();
  assert.equal((await pending).status, 'unknown');
  h.holdScript = null;
  assert.equal((await h.attach()).reason, 'owner_attach');
  let firstClose;
  for (let index = 0; index < 60; index++) {
    const child = await h.open();
    assert.equal((await h.operation('chrome_click', { ref: await h.ref(child, 'Increment') }, child)).status, 'ok', 'click ' + index);
    assert.equal((await h.operation('chrome_type', { ref: await h.ref(child, 'Notes'), text: 'ordinary' }, child)).status, 'ok', 'type ' + index);
    const close = { request_id: h.wire('retained-close-' + index), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_close', tab_id: child, args: {} };
    assert.equal((await h.raw(close)).status, 'ok', 'close ' + index);
    if (!firstClose) firstClose = close;
  }
  assert.equal(h.storage.chrome_relay_latch.completed_order.length, 244);
  const retained = [firstOpen, firstClick, firstType, firstClose, unknownClick];
  async function replay(env) {
    const before = [env.calls.create.length, env.calls.scripts.length, env.calls.remove.length];
    for (const raw of retained) {
      const result = await env.raw(raw);
      assert.equal(result.status, raw === unknownClick ? 'unknown' : 'ok', raw.request_id);
      if (raw === firstOpen) assert.equal(result.tab_id, id);
      assert.equal((await env.raw({ ...raw, session_id: 'agent-b' })).status, 'denied');
    }
    assert.deepEqual([env.calls.create.length, env.calls.scripts.length, env.calls.remove.length], before, 'replays have no native effects');
  }
  await replay(h);
  assert.equal(h.documents.get(id).increment.clicks, 2);
  assert.equal(h.documents.get(id).note.value, 'retained');
  assert.equal((await h.attach()).reason, 'owner_stop');
  await replay(h);
  assert.equal((await h.attach()).reason, 'owner_attach');
  const restarted = harness({ storage: h.storage, tabs: h.tabs });
  restarted.nextId = h.nextId;
  await restarted.api.recover();
  await replay(restarted);
  assert.equal((await restarted.operation('chrome_attachment')).data.technical_block, false);
  const next = await restarted.operation('chrome_open', { url: ORIGIN + '/work' }, undefined, { session_id: 'agent-b', request_id: restarted.wire('after-worker-open') });
  assert.equal(next.status, 'ok');
  assert.equal((await restarted.operation('chrome_close', {}, next.tab_id, { session_id: 'agent-b', request_id: restarted.wire('after-worker-close') })).status, 'ok');
  await restarted.api.onBrowserStartup();
  await replay(restarted);
  assert.equal((await restarted.attach()).reason, 'owner_attach');
  const afterStartup = await restarted.operation('chrome_open', { url: ORIGIN + '/work' }, undefined, { session_id: 'agent-b', request_id: restarted.wire('after-browser-open') });
  assert.equal(afterStartup.status, 'ok');
  assert.equal((await restarted.operation('chrome_close', {}, afterStartup.tab_id, { session_id: 'agent-b', request_id: restarted.wire('after-browser-close') })).status, 'ok');
  assert.equal(restarted.tabs.get(1).active, true);
  assert.equal(restarted.calls.update.length, 0);
});

test('large completed journals normalize with linear lookup work while retaining every valid terminal record', () => {
  const context = vm.createContext({ URL, module: { exports: {} } });
  vm.runInContext(workerSource, context, { filename: workerPath });
  const measured = vm.runInContext(`(() => {
    const count = 10000;
    const generation = '1234567890abcdef1234567890abcdef';
    const completed = Object.create(null);
    const ids = [];
    for (let index = 0; index < count; index++) {
      const id = generation + ':' + index.toString(16).padStart(32, '0');
      ids.push(id);
      completed[id] = { status: ['ok', 'denied', 'unknown'][index % 3], session_id: 'agent-a', action: 'chrome_open', length: 0, reason: '', tab_id: 0 };
    }
    const order = ids;
    let lookupWork = 0;
    const includes = Array.prototype.includes;
    const indexOf = Array.prototype.indexOf;
    const has = Set.prototype.has;
    const add = Set.prototype.add;
    Array.prototype.includes = function (...args) { lookupWork += this.length; return includes.apply(this, args); };
    Array.prototype.indexOf = function (...args) { lookupWork += this.length; return indexOf.apply(this, args); };
    Set.prototype.has = function (...args) { lookupWork++; return has.apply(this, args); };
    Set.prototype.add = function (...args) { lookupWork++; return add.apply(this, args); };
    try {
      const normalized = normalizeLatch(Object.assign(defaultLatch(), { generation, completed, completed_order: order }));
      return {
        count: normalized.completed_order.length,
        records: Object.keys(normalized.completed).length,
        preserved: normalized.completed_order.every((id, index) => id === ids[index] && normalized.completed[id].status === completed[id].status),
        lookupWork,
        linearBudget: 8 * order.length
      };
    } finally {
      Array.prototype.includes = includes;
      Array.prototype.indexOf = indexOf;
      Set.prototype.has = has;
      Set.prototype.add = add;
    }
  })()`, context);
  assert.equal(measured.count, 10000);
  assert.equal(measured.records, 10000);
  assert.equal(measured.preserved, true);
  assert.ok(measured.lookupWork <= measured.linearBudget,
    'completed journal lookup work ' + measured.lookupWork + ' exceeds linear budget ' + measured.linearBudget);
});

test('worker SHA256 generation matches the broker golden vector and floors expiry milliseconds', async () => {
  for (const expires_at of [1760198400123.4, '2025-10-11T16:00:00.123400Z']) {
    const h = harness({ config: { profile_id: 'synthetic-profile', extension_token: 'E'.repeat(32), expires_at } });
    assert.equal((await h.configure()).ok, true);
    assert.equal(h.storage.chrome_relay_latch.generation, '1c77c827876b018249c11d9dd2a6030f');
    assert.equal(generationFor(h.config), '1c77c827876b018249c11d9dd2a6030f');
  }
  const h = await harness({ config: { expires_at: 1893456000123 } }).ready();
  assert.equal(h.storage.chrome_relay_latch.generation, '7922cae872b4a16c55932db154383c65');
  const generation = h.storage.chrome_relay_latch.generation;
  h.config.allowed_actions = ['chrome_attachment'];
  await h.configure();
  assert.equal(h.storage.chrome_relay_latch.generation, generation, 'scope does not enter the digest');
  assert.equal(h.storage.chrome_relay_latch.stop, true, 'scope changes still require owner attachment');
});

test('missing, malformed and old wire generations deny all actions before DOM, ownership or reconciliation', async () => {
  const h = await harness().ready();
  const id = await h.open();
  const ref = await h.ref(id, 'Increment');
  const generation = h.storage.chrome_relay_latch.generation;
  const malformed = ['bare-legacy-id', '0'.repeat(32) + ':' + '1'.repeat(32), generation.toUpperCase() + ':' + '1'.repeat(32), generation + ':short', generation + ':' + 'f'.repeat(33)];
  const before = [h.calls.create.length, h.calls.remove.length, h.calls.scripts.length];
  const journal = clean(h.storage.chrome_relay_latch);
  for (const request_id of malformed) for (const action of actions) {
    const raw = { request_id, session_id: 'agent-a', profile_id: h.config.profile_id, action, args: action === 'chrome_open' ? { url: ORIGIN + '/work' } : action === 'chrome_click' ? { ref } : action === 'chrome_type' ? { ref, text: 'denied' } : action === 'chrome_wait' ? { text: 'Fixture ready' } : {} };
    if (!['chrome_open', 'chrome_attachment'].includes(action)) raw.tab_id = id;
    assert.equal((await h.raw(raw)).status, 'denied', action + '/' + request_id);
  }
  assert.deepEqual([h.calls.create.length, h.calls.remove.length, h.calls.scripts.length], before);
  assert.deepEqual(h.storage.chrome_relay_latch, journal, 'stale wire cannot alter journal or ownership');
});

test('trusted new generation retires only old journal and ownership, never attaches, and denies old wire after restart', async () => {
  const h = await harness().ready();
  const raw = { request_id: h.wire('old-open'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_open', args: { url: ORIGIN + '/work' } };
  const opened = await h.raw(raw);
  assert.equal(opened.status, 'ok');
  const oldGeneration = h.storage.chrome_relay_latch.generation;
  h.config.expires_at += 60000;
  // Direct trusted storage assignment is detected even before a recovery event.
  h.storage.chrome_relay_config = clean(h.config);
  assert.equal((await h.raw(raw)).data.reason, 'generation');
  const status = await h.operation('chrome_attachment');
  assert.equal(status.data.stopped, true);
  assert.equal(status.data.authorized, false);
  assert.deepEqual(status.data.owned_tab_ids, []);
  assert.notEqual(h.storage.chrome_relay_latch.generation, oldGeneration);
  assert.deepEqual(h.storage.chrome_relay_latch.completed_order, []);
  assert.equal(h.tabs.has(opened.tab_id), true, 'generation change does not close old tabs');
  assert.equal((await h.operation('chrome_open', { url: ORIGIN + '/work' })).data.reason, 'stopped');
  const restarted = harness({ storage: h.storage, tabs: h.tabs });
  restarted.nextId = h.nextId;
  await restarted.api.recover();
  assert.equal((await restarted.raw(raw)).data.reason, 'generation');
  assert.equal((await restarted.attach()).reason, 'owner_attach');
  assert.equal((await restarted.read(opened.tab_id)).data.reason, 'ownership');
  const before = restarted.calls.create.length;
  assert.equal((await restarted.raw(raw)).data.reason, 'generation');
  assert.equal(restarted.calls.create.length, before);
  assert.equal((await restarted.operation('chrome_open', { url: ORIGIN + '/work' })).status, 'ok');
});

test('late old-generation tab creation cannot adopt new ownership or append to the new journal', async () => {
  const h = await harness().ready();
  const held = { started: deferred(), gate: deferred() };
  h.holdCreate = held;
  const raw = { request_id: h.wire('old-held-open'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_open', args: { url: ORIGIN + '/work' } };
  const pending = h.raw(raw);
  const created = await held.started.promise;
  h.config.extension_token = "SYNTHETICNEW" + "X".repeat(64);
  assert.equal((await h.configure()).ok, true);
  assert.equal(h.storage.chrome_relay_latch.stop, true);
  assert.equal((await h.attach()).reason, 'owner_attach');
  held.gate.resolve();
  assert.equal((await pending).status, 'unknown');
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(h.storage.chrome_relay_latch.sessions, {});
  assert.deepEqual(h.storage.chrome_relay_latch.completed_order, []);
  assert.equal(h.calls.remove.includes(created.id), true);
  assert.equal(h.tabs.has(1), true);
  assert.equal(h.tabs.has(2), true);
  assert.equal((await h.raw(raw)).data.reason, 'generation');
});

function seedJournal(h, count) {
  const latch = h.storage.chrome_relay_latch;
  latch.completed = {};
  latch.completed_order = [];
  for (let index = 0; index < count; index++) {
    const request_id = latch.generation + ':' + index.toString(16).padStart(32, '0');
    latch.completed_order.push(request_id);
    // Worst allowed persisted field widths; no URLs, page data or capabilities.
    latch.completed[request_id] = { status: 'unknown', session_id: 's'.repeat(128), action: 'chrome_close', length: 1000, reason: 'a'.repeat(64), tab_id: 2147483647 };
  }
}

test('10000 terminal plus reserved IDs bound effects without eviction; full-bound STOP leaves Chrome quota headroom', async () => {
  const h = await harness().ready();
  seedJournal(h, 9999);
  const first = h.storage.chrome_relay_latch.completed_order[0];
  const held = { started: deferred(), gate: deferred() };
  h.holdCreate = held;
  const raw = { request_id: h.wire('last-reservation'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_open', args: { url: ORIGIN + '/work' } };
  const pending = h.raw(raw);
  await held.started.promise;
  assert.equal(h.storage.chrome_relay_latch.completed_order.length + h.storage.chrome_relay_latch.inflight.length, 10000);
  assert.equal((await h.operation('chrome_open', { url: ORIGIN + '/must-not-open' })).data.reason, 'bound');
  assert.equal(h.calls.create.length, 1, 'reserved inflight consumes the last slot before completion');
  assert.equal((await h.attach()).reason, 'owner_stop');
  assert.equal(h.storage.chrome_relay_latch.stop, true);
  assert.equal(h.storage.chrome_relay_latch.completed_order.length, 10000);
  assert.equal(h.storage.chrome_relay_latch.inflight.length, 0);
  assert.equal(h.storage.chrome_relay_latch.completed[first].reason, 'a'.repeat(64), 'earliest active-generation terminal is never evicted');
  const storedBytes = Buffer.byteLength(JSON.stringify(h.storage));
  assert.ok(storedBytes < 8 * 1024 * 1024, 'worst metadata leaves >=2MiB below Chrome local 10MiB quota, bytes=' + storedBytes);
  held.gate.resolve();
  assert.equal((await pending).status, 'unknown');
  h.holdCreate = null;
  assert.equal((await h.attach()).reason, 'owner_attach');
  assert.equal((await h.operation('chrome_open', { url: ORIGIN + '/still-full' })).data.reason, 'bound');
  assert.equal(h.calls.create.length, 1);
  const restarted = harness({ storage: h.storage, tabs: h.tabs });
  await restarted.api.recover();
  assert.equal(restarted.storage.chrome_relay_latch.completed_order.length, 10000);
  assert.equal((await restarted.attach()).reason, 'owner_stop');
  assert.equal(restarted.storage.chrome_relay_latch.stop, true);
  assert.equal(restarted.storage.chrome_relay_latch.completed_order.length, 10000);
});

test('legacy, corrupt and oversized active journals fail closed without trimming or owner reset', async () => {
  const corruptions = [
    latch => { latch.schema = 1; delete latch.generation; },
    latch => { latch.completed[latch.completed_order[0]].status = 'invalid'; },
    latch => { latch.completed_order = []; },
    latch => { latch.completed[latch.completed_order[0]].page = 'PRIVATE_PAGE_CANARY'; },
    latch => { latch.inflight.push({ request_id: 'legacy-reservation' }); },
    latch => { const id = latch.generation + ':' + 'f'.repeat(32); latch.completed[id] = { ...latch.completed[latch.completed_order[0]] }; latch.completed_order.push(id); }
  ];
  for (const [index, corrupt] of corruptions.entries()) {
    const h = await harness().ready();
    seedJournal(h, index === 5 ? 10000 : 1);
    corrupt(h.storage.chrome_relay_latch);
    const saved = clean(h.storage.chrome_relay_latch);
    assert.equal((await h.operation('chrome_open', { url: ORIGIN + '/denied' })).data.reason, 'storage');
    assert.equal((await h.attach()).status, 'denied');
    assert.equal((await h.api.recover()).ok, false);
    assert.deepEqual(h.storage.chrome_relay_latch, saved, 'no journal data discarded, corruption ' + index);
    assert.equal(h.calls.create.length, 0);
    assert.equal(h.calls.badges.at(-1), 'STOP');
  }
});

test('storage reads/writes fail closed before effects and attachment needs a successful durable write', async () => {
  for (const failure of ['failStorageGet', 'failStorageSet']) {
    const h = await harness().ready();
    h[failure] = true;
    assert.notEqual((await h.operation('chrome_open', { url: ORIGIN + '/denied' })).status, 'ok');
    assert.equal((await h.attach()).status, 'denied');
    assert.equal(h.calls.create.length, 0);
    assert.equal(h.calls.badges.at(-1), 'STOP');
    h[failure] = false;
    assert.equal((await h.operation('chrome_open', { url: ORIGIN + '/still-stopped' })).data.reason, 'stopped');
    assert.equal(h.storage.chrome_relay_latch.stop, true);
    assert.equal((await h.attach()).reason, 'owner_attach');
    assert.equal((await h.operation('chrome_open', { url: ORIGIN + '/recovered' })).status, 'ok');
  }
  const h = harness();
  await h.configure();
  h.failStorageSet = true;
  assert.equal((await h.attach()).status, 'denied');
  assert.equal(h.storage.chrome_relay_latch.stop, true);
  assert.equal(h.calls.events.some(event => event.type === 'attach'), false);
  h.failStorageSet = false;
  assert.equal((await h.attach()).reason, 'owner_attach');
});

test('storage completion failure retains unknown outcome and never repeats an already dispatched mutation', async () => {
  const h = await harness().ready();
  const id = await h.open();
  const raw = { request_id: h.wire('completion-write-failure'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_click', tab_id: id, args: { ref: await h.ref(id, 'Increment') } };
  h.onStorageSet = value => {
    if (value.chrome_relay_latch.completed[raw.request_id]) throw new Error('synthetic_completion_storage_failure');
  };
  assert.equal((await h.raw(raw)).status, 'unknown');
  assert.equal(h.documents.get(id).increment.clicks, 1);
  assert.equal(h.calls.badges.at(-1), 'STOP');
  assert.equal((await h.attach()).status, 'denied');
  h.onStorageSet = null;
  const replay = await h.raw(raw);
  assert.equal(replay.status, 'unknown');
  assert.equal(h.storage.chrome_relay_latch.stop, true);
  assert.equal(h.storage.chrome_relay_latch.completed[raw.request_id].status, 'unknown');
  assert.equal((await h.attach()).reason, 'owner_attach');
  assert.equal((await h.raw(raw)).status, 'unknown');
  assert.equal(h.documents.get(id).increment.clicks, 1);
});

test('technical disconnect requires explicit authenticated attachment reconciliation; poll success cannot unlock STOP', async () => {
  const h = await harness().ready();
  const id = await h.open();
  h.failPoll = true;
  assert.equal((await h.api.pollOnce()).polled, false);
  h.failPoll = false;
  await h.api.pollOnce();
  assert.equal(h.storage.chrome_relay_latch.technical_block, true);
  assert.equal((await h.read(id)).data.reason, 'technical_block');
  const reconciled = await h.operation('chrome_attachment');
  assert.equal(reconciled.data.technical_block, false);
  assert.deepEqual(reconciled.data.owned_tab_ids, [id]);
  assert.equal((await h.read(id)).status, 'ok');
  await h.attach();
  await h.api.pollOnce();
  await h.operation('chrome_attachment');
  assert.equal(h.storage.chrome_relay_latch.stop, true);
  assert.equal((await h.read(id)).data.reason, 'stopped');
});

test('manual close and real browser startup persist STOP without closing unknown tabs', async () => {
  const h = await harness().ready();
  await h.open();
  h.tabs.delete(1);
  await h.api.onTabRemoved(1);
  assert.equal(h.storage.chrome_relay_latch.stop, true);
  assert.equal(h.storage.chrome_relay_latch.manual_tab_id, 0);
  await h.api.recover();
  assert.equal(h.storage.chrome_relay_latch.stop, true);
  await h.api.onBrowserStartup();
  assert.deepEqual(h.storage.chrome_relay_latch.sessions, {});
  assert.equal(h.tabs.has(2), true);
  assert.equal(h.calls.remove.length, 0);
});

test('attributable agent popups fail closed; manual or unproven windows remain untouched and focus is never changed', async () => {
  const h = await harness().ready();
  const id = await h.open();
  for (const [childId, opener] of [[20, id], [21, 1], [22, 2], [23, 999]]) h.tabs.set(childId, { id: childId, openerTabId: opener, active: false, url: ORIGIN + '/popup' });
  await h.api.onTabCreated({ id: 20, openerTabId: id });
  for (const childId of [21, 22, 23]) await h.api.onTabCreated(h.tabs.get(childId));
  h.tabs.set(24, { id: 24, openerTabId: 1, active: false, url: ORIGIN + '/' });
  await h.api.onTabCreated({ id: 24, openerTabId: id });
  assert.deepEqual(h.calls.remove, [20]);
  for (const keep of [1, 2, 21, 22, 23, 24]) assert.equal(h.tabs.has(keep), true);
  assert.equal(h.calls.update.length, 0);
  assert.equal(h.tabs.get(1).active, true);
});

test('read redacts all field values and token text; 4096-byte data and audit metadata stay bounded', async () => {
  const h = await harness().ready();
  const id = await h.open('agent-a', ORIGIN + '/PRIVATE_URL_PATH_CANARY');
  await h.read(id);
  const doc = h.documents.get(id);
  doc.body.append(new Element('p', {}, "public-member-fixture-2-Ω".repeat(10000)));
  const read = await h.read(id);
  const rendered = JSON.stringify(read.data);
  assert.ok(Buffer.byteLength(rendered, 'utf8') <= 4096);
  assert.equal(rendered.includes('FIELD_CANARY_NOT_FOR_READ'), false);
  assert.equal(rendered.includes('EMAIL_CANARY_NOT_FOR_READ'), false);
  assert.equal(rendered.includes('PASSWORD_CANARY_NOT_FOR_READ'), false);
  assert.equal(rendered.includes('SECRET_CANARY_NOT_FOR_READ'), false);
  const ref = await h.ref(id, 'Notes');
  await h.operation('chrome_type', { ref, text: 'TYPED_PRIVATE_CANARY' }, id);
  await h.raw({ request_id: h.wire('invalid-op'), session_id: 'agent-a', profile_id: h.config.profile_id, action: '/ACTION_PRIVATE_CANARY', args: {} });
  for (let index = 0; index < 60; index++) await h.operation('chrome_attachment');
  const audit = clean(h.api.getAudit());
  assert.ok(audit.length <= 50);
  const auditText = JSON.stringify(audit);
  for (const canary of ['PRIVATE_URL_PATH_CANARY', 'TYPED_PRIVATE_CANARY', 'ACTION_PRIVATE_CANARY', h.config.extension_token, 'FIELD_CANARY_NOT_FOR_READ']) assert.equal(auditText.includes(canary), false);
  for (const entry of audit) assert.deepEqual(Object.keys(entry), ['event', 'op', 'status', 'request_id', 'session_id', 'tab_id', 'count']);
});

test('poll validates JSON/UTF8/type/64KiB before dispatch and never puts auth material in query', async () => {
  const h = await harness().ready();
  const badResponses = [
    new Response('{', { headers: { 'content-type': 'application/json' } }),
    new Response(new Uint8Array([0xc3, 0x28]), { headers: { 'content-type': 'application/json' } }),
    new Response('{}', { headers: { 'content-type': 'text/html' } }),
    new Response('x'.repeat(65537), { headers: { 'content-type': 'application/json' } })
  ];
  for (const response of badResponses) { h.pollResponse = response; assert.equal((await h.api.pollOnce()).polled, false); }
  h.pollResponse = new Response(JSON.stringify({ request_id: h.wire('wire-status'), session_id: 'agent-a', profile_id: h.config.profile_id, action: 'chrome_attachment', args: {} }), { headers: { 'content-type': 'application/json' } });
  assert.equal((await h.api.pollOnce()).polled, true);
  assert.equal(h.calls.results.at(-1).request_id, h.wire('wire-status'));
  assert.equal(h.calls.create.length, 0);
  for (const call of h.calls.fetch) {
    assert.equal(new URL(call.url).search, '');
    assert.equal(call.url.includes(h.config.extension_token), false);
    assert.equal(call.headers.Authorization, 'Bearer ' + h.config.extension_token);
    assert.equal(call.headers['X-Profile-Id'], h.config.profile_id);
    if (call.body) assert.ok(Buffer.byteLength(call.body, 'utf8') <= 65536);
  }
});

test('large same-origin menu keeps useful Unicode frame text and safe navigation within 4096 bytes', async () => {
  const h = await harness().ready();
  const id = await h.open();
  const root = emptyDocument(ORIGIN + '/work', 'frameset');
  const child = emptyDocument(ORIGIN + '/legacy/menu');
  const marker = "public-variable-fixture-1-Ж";
  child.body.append(new Element('p', {}, marker.repeat(18)));
  for (let i = 0; i < 40; i += 1) child.body.append(new Element('a', {
    href: '/account/dashboard/ordinary/menu-' + i, target: 'main'
  }, 'Information menu ' + i));
  root.body.append(frameElement('frame', child));
  h.documents.set(id, root);
  const read = await h.read(id);
  assert.equal(read.status, 'ok');
  assert.ok(read.data.excerpt.startsWith(marker), 'menu must not erase useful frame text');
  assert.ok(Buffer.byteLength(JSON.stringify(read.data), 'utf8') <= 4096);
  assert.ok(read.data.refs.some(ref => ref.url === ORIGIN + '/account/dashboard/ordinary/menu-0'));
  assert.equal(read.data.diagnostics.reader_status, 'partial');
  assert.ok(!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(read.data.excerpt));
});

// Managed (schema 2) grants: controller-registered c-<hex> clients, explicit
// persistence and scope bound into the generation. Legacy tests above are unchanged.
const MANAGED_GOLDEN = '587dc3e21cfba84bad4e7ea386509508';  // test_managed.py GOLDEN_GENERATION.
const managedConfig = (changes = {}) => ({ schema: 2, mode: 'managed', grant_id: 'grant-test-1', session_ids: undefined, expires_at: undefined, persistent: true, ...changes });
const clientSid = hex => 'c-' + hex.repeat(32);
const SECOND_ORIGIN = 'https://second.example.test';
const twoOriginGrant = () => managedConfig({ allowed_origins: [ORIGIN, SECOND_ORIGIN] });

test('managed owner attach requests the whole approved HTTPS set once inside the gesture', async () => {
  const grant = deferred();
  const h = harness({ config: twoOriginGrant(), permissions: [], onPermissionRequest: () => grant.promise });
  await h.configure();
  assert.equal((await h.attach(2)).reason, 'ownership');
  assert.deepEqual(h.calls.permissionRequests, []);
  const pending = h.attach();
  assert.deepEqual(h.calls.permissionRequests, [[ORIGIN + '/*', SECOND_ORIGIN + '/*']], 'one synchronous request before any await');
  grant.resolve();
  assert.equal((await pending).reason, 'owner_attach');
  for (const origin of [ORIGIN, SECOND_ORIGIN]) {
    assert.equal(await h.chrome.permissions.contains({ origins: [origin + '/*'] }), true);
    assert.equal((await h.read(await h.open(clientSid('a'), origin + '/work'), clientSid('a'))).status, 'ok');
  }
  assert.equal((await h.attach()).reason, 'owner_stop');
  assert.equal(h.calls.permissionRequests.length, 1, 'ordinary calls and STOP request no grants');
});

test('managed operations use each freshly validated owned origin and preserve client isolation', async () => {
  const h = await harness({ config: twoOriginGrant(), permissions: [] }).ready();
  for (const [origin, sid, other] of [[ORIGIN, clientSid('a'), clientSid('b')], [SECOND_ORIGIN, clientSid('b'), clientSid('a')]]) {
    const id = await h.open(sid, origin + '/work');
    const read = await h.read(id, sid);
    assert.equal(read.status, 'ok');
    const doc = h.documents.get(id);
    doc.body.append(new Element('a', { href: '/documents' }, 'Documents'));
    let refs = (await h.read(id, sid)).data.refs;
    assert.equal(refs.find(ref => ref.name === 'Documents').url, origin + '/documents');
    assert.equal((await h.operation('chrome_wait', { text: 'Fixture ready' }, id, { session_id: sid })).data.matched, true);
    const click = () => h.operation('chrome_click', { ref: refs.find(ref => ref.name === 'Increment').ref }, id, { session_id: sid });
    assert.equal((await click()).status, 'ok');
    assert.equal(doc.increment.clicks, 1);
    refs = (await h.read(id, sid)).data.refs;
    assert.equal((await h.operation('chrome_type', { ref: refs.find(ref => ref.name === 'Notes').ref, text: 'synthetic note' }, id, { session_id: sid })).status, 'ok');
    assert.equal(doc.note.value, 'synthetic note');
    refs = (await h.read(id, sid)).data.refs;
    const scripts = h.calls.scripts.length;
    for (const action of ['chrome_read', 'chrome_wait', 'chrome_click', 'chrome_type', 'chrome_close']) {
      const args = action === 'chrome_wait' ? { text: 'Fixture ready' } : action === 'chrome_click' ? { ref: refs.find(ref => ref.name === 'Increment').ref } : action === 'chrome_type' ? { ref: refs.find(ref => ref.name === 'Notes').ref, text: 'wrong client' } : {};
      assert.equal((await h.operation(action, args, id, { session_id: other })).data.reason, 'ownership');
    }
    assert.equal(h.calls.scripts.length, scripts);
    assert.ok(h.calls.scripts.filter(call => call.tab_id === id).every(call => call.origin === origin));
    assert.equal((await h.operation('chrome_close', {}, id, { session_id: sid })).status, 'ok');
  }
  assert.equal(h.calls.permissionRequests.length, 1);
  assert.equal(h.calls.update.length, 0);
});

test('managed origin allowlist and per-origin permission remain mandatory without ordinary prompts', async () => {
  const h = await harness({ config: twoOriginGrant() }).ready();
  const id = await h.open(clientSid('a'), SECOND_ORIGIN + '/work');
  const refs = (await h.read(id, clientSid('a'))).data.refs;
  h.permissions.delete(SECOND_ORIGIN + '/*');
  const scripts = h.calls.scripts.length;
  const creates = h.calls.create.length;
  assert.equal((await h.operation('chrome_open', { url: SECOND_ORIGIN + '/again' }, undefined, { session_id: clientSid('a') })).data.reason, 'permission');
  for (const [action, args] of [['chrome_read', {}], ['chrome_wait', { text: 'Fixture ready' }], ['chrome_click', { ref: refs.find(ref => ref.name === 'Increment').ref }], ['chrome_type', { ref: refs.find(ref => ref.name === 'Notes').ref, text: 'blocked' }]]) {
    assert.equal((await h.operation(action, args, id, { session_id: clientSid('a') })).data.reason, 'permission');
  }
  for (const url of ['https://unapproved.example.test/work', SECOND_ORIGIN + ':444/work', SECOND_ORIGIN + '/login']) {
    assert.equal((await h.operation('chrome_open', { url }, undefined, { session_id: clientSid('a') })).status, 'denied');
  }
  assert.equal((await h.read(1, clientSid('a'))).data.reason, 'ownership');
  assert.equal(h.calls.scripts.length, scripts);
  assert.equal(h.calls.create.length, creates);
  assert.equal(h.calls.permissionRequests.length, 1);
  assert.equal((await h.read(await h.open(clientSid('b')), clientSid('b'))).status, 'ok');
});

test('managed owned navigation uses the actual tab origin and invalidates old references', async () => {
  const h = await harness({ config: twoOriginGrant() }).ready();
  const sid = clientSid('a');
  const id = await h.open(sid);
  const old = (await h.read(id, sid)).data.refs.find(ref => ref.name === 'Increment').ref;
  const moved = documentFixture(SECOND_ORIGIN + '/moved');
  h.tabs.get(id).url = moved.location.href;
  h.documents.set(id, moved);
  h.realms.set(id, domRealm(moved));
  h.api.onTabUpdated(id, { url: moved.location.href });
  assert.equal((await h.operation('chrome_click', { ref: old }, id, { session_id: sid })).data.reason, 'stale_ref');
  const ref = (await h.read(id, sid)).data.refs.find(ref => ref.name === 'Increment').ref;
  assert.equal((await h.operation('chrome_click', { ref }, id, { session_id: sid })).status, 'ok');
  assert.equal(moved.increment.clicks, 1);
  // A navigation racing the tabs.get/DOM boundary cannot read another origin.
  moved.location.href = ORIGIN + '/changed-before-injection';
  assert.equal((await h.read(id, sid)).status, 'denied');
  assert.equal(h.calls.permissionRequests.length, 1);
});

test('managed approved manual navigation and technical restart retain both origins and clients', async () => {
  const h = await harness({ config: twoOriginGrant() }).ready();
  const a = await h.open(clientSid('a'));
  const b = await h.open(clientSid('b'), SECOND_ORIGIN + '/work');
  h.tabs.get(1).url = SECOND_ORIGIN + '/manual';
  h.api.onTabUpdated(1, { url: h.tabs.get(1).url });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.storage.chrome_relay_latch.stop, false);
  h.failPoll = true;
  await h.api.pollOnce();
  assert.equal((await h.operation('chrome_attachment', {}, undefined, { session_id: clientSid('a') })).data.technical_block, false);
  assert.equal((await h.read(b, clientSid('b'))).status, 'ok');
  const restarted = harness({ storage: h.storage, tabs: h.tabs, permissions: [ORIGIN, SECOND_ORIGIN] });
  await restarted.api.recover();
  assert.equal((await restarted.operation('chrome_attachment', {}, undefined, { session_id: clientSid('a') })).data.technical_block, false);
  assert.deepEqual((await restarted.operation('chrome_attachment', {}, undefined, { session_id: clientSid('a') })).data.owned_tab_ids, [a]);
  assert.deepEqual((await restarted.operation('chrome_attachment', {}, undefined, { session_id: clientSid('b') })).data.owned_tab_ids, [b]);
  assert.equal((await restarted.read(a, clientSid('a'))).status, 'ok');
  assert.equal((await restarted.read(b, clientSid('b'))).status, 'ok');
  assert.equal((await restarted.read(b, clientSid('a'))).data.reason, 'ownership');
  assert.deepEqual(restarted.calls.permissionRequests, []);
  assert.equal(h.calls.permissionRequests.length, 1);
});

test('managed outside-origin manual navigation and owner STOP persist through restart with no replay', async () => {
  for (const cause of ['outside', 'STOP']) {
    const h = await harness({ config: twoOriginGrant() }).ready();
    const sid = clientSid('a');
    const id = await h.open(sid, SECOND_ORIGIN + '/work');
    const ref = (await h.read(id, sid)).data.refs.find(ref => ref.name === 'Increment').ref;
    const raw = { request_id: h.wire('managed-replay-' + cause), session_id: sid, profile_id: h.config.profile_id, action: 'chrome_click', tab_id: id, args: { ref } };
    assert.equal((await h.raw(raw)).status, 'ok');
    const doc = h.documents.get(id);
    if (cause === 'STOP') assert.equal((await h.attach()).reason, 'owner_stop');
    else {
      h.tabs.get(1).url = 'https://unapproved.example.test/outside';
      h.api.onTabUpdated(1, { url: h.tabs.get(1).url });
      await new Promise(resolve => setImmediate(resolve));
    }
    assert.equal(h.storage.chrome_relay_latch.stop, true);
    const restarted = harness({ storage: h.storage, tabs: h.tabs, permissions: [ORIGIN, SECOND_ORIGIN] });
    await restarted.api.recover();
    await restarted.api.pollOnce();
    assert.equal((await restarted.operation('chrome_attachment', {}, undefined, { session_id: sid })).data.stopped, true);
    assert.equal((await restarted.read(id, sid)).data.reason, 'stopped');
    assert.equal((await restarted.operation('chrome_open', { url: SECOND_ORIGIN + '/new' }, undefined, { session_id: sid })).data.reason, 'stopped');
    assert.equal((await restarted.raw(raw)).status, 'ok', 'cached result is returned without repeating the mutation');
    assert.equal(restarted.calls.scripts.length, 0);
    assert.equal(restarted.calls.create.length, 0);
    assert.deepEqual(restarted.calls.permissionRequests, []);
    h.tabs.get(1).url = SECOND_ORIGIN + '/manual';
    assert.equal((await restarted.attach()).reason, 'owner_attach');
    assert.equal((await restarted.raw(raw)).status, 'ok');
    assert.equal(restarted.calls.scripts.length, 0);
    assert.equal(doc.increment.clicks, 1);
  }
});

test('managed second-origin frame reading stays same-origin with safe URLs and read-only frame refs', async () => {
  const h = await harness({ config: twoOriginGrant() }).ready();
  const sid = clientSid('a');
  const id = await h.open(sid, SECOND_ORIGIN + '/work');
  const root = emptyDocument(SECOND_ORIGIN + '/work', 'frameset');
  const child = emptyDocument(SECOND_ORIGIN + '/menu');
  const button = new Element('button', {}, 'Child increment');
  child.body.append(new Element('p', {}, 'Synthetic second-origin frame'), new Element('a', { href: '/documents' }, 'Documents'), button);
  root.body.append(frameElement('frame', child));
  const foreign = { location: { href: ORIGIN + '/frame' }, get body() { throw new Error('other approved origin is still a foreign frame'); } };
  root.body.append(frameElement('iframe', foreign));
  h.documents.set(id, root);
  const read = await h.read(id, sid);
  assert.equal(read.status, 'ok');
  assert.match(read.data.excerpt, /Synthetic second-origin frame/);
  assert.equal(read.data.refs.find(ref => ref.name === 'Documents').url, SECOND_ORIGIN + '/documents');
  assert.equal(read.data.diagnostics.same_origin_frame_count, 1);
  assert.equal(read.data.diagnostics.blocked_frame_count, 1);
  assert.equal((await h.operation('chrome_wait', { text: 'Synthetic second-origin frame' }, id, { session_id: sid })).data.matched, false, 'wait retains its main-document-only semantics');
  assert.equal((await h.operation('chrome_click', { ref: read.data.refs.find(ref => ref.name === 'Child increment').ref }, id, { session_id: sid })).status, 'denied');
  assert.equal(button.clicks, 0);
  assert.equal(h.calls.permissionRequests.length, 1);
});

test('managed synthetic owner attach requests only the approved HTTPS subset and refuses a denied grant', async () => {
  const synthetic = 'http://127.0.0.1:8123';
  const h = harness({ config: managedConfig({ synthetic: true, allowed_origins: [synthetic, SECOND_ORIGIN] }), permissions: [] });
  h.tabs.get(1).url = synthetic + '/manual';
  await h.configure();
  assert.equal((await h.attach()).reason, 'owner_attach');
  assert.deepEqual(h.calls.permissionRequests, [[SECOND_ORIGIN + '/*']]);
  assert.equal((await h.read(await h.open(clientSid('a'), SECOND_ORIGIN + '/work'), clientSid('a'))).status, 'ok');
  const denied = harness({ config: twoOriginGrant(), permissions: [], permissionDenied: true });
  await denied.configure();
  assert.equal((await denied.attach()).reason, 'permission');
  assert.deepEqual(denied.calls.permissionRequests, [[ORIGIN + '/*', SECOND_ORIGIN + '/*']]);
  assert.equal(denied.storage.chrome_relay_latch.stop, true);
  assert.equal((await denied.operation('chrome_open', { url: SECOND_ORIGIN + '/work' }, undefined, { session_id: clientSid('a') })).data.reason, 'stopped');
  assert.equal(denied.calls.create.length, 0);
});

test('legacy remains bound to the owner-attached origin even when its policy lists two origins', async () => {
  const h = await harness({ config: { allowed_origins: [ORIGIN, SECOND_ORIGIN] }, permissions: [ORIGIN, SECOND_ORIGIN] }).ready();
  assert.deepEqual(h.calls.permissionRequests, [[ORIGIN + '/*']]);
  assert.equal((await h.operation('chrome_open', { url: SECOND_ORIGIN + '/work' })).data.reason, 'origin');
  const id = await h.open();
  h.tabs.get(id).url = SECOND_ORIGIN + '/changed';
  h.api.onTabUpdated(id, { url: h.tabs.get(id).url });
  assert.equal((await h.read(id)).data.reason, 'origin');
  assert.equal(h.calls.scripts.length, 0);
  h.tabs.get(1).url = SECOND_ORIGIN + '/manual';
  h.api.onTabUpdated(1, { url: h.tabs.get(1).url });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.storage.chrome_relay_latch.stop, true);
  await h.api.recover();
  assert.equal((await h.operation('chrome_attachment')).data.stopped, true);
  assert.equal(h.calls.permissionRequests.length, 1);
});

test('managed generation matches the Python controller vector and binds grant scope', async () => {
  const golden = managedConfig({ profile_id: 'synthetic-profile', extension_token: 'E'.repeat(32), broker_base: 'http://127.0.0.1:9444', allowed_origins: ['http://127.0.0.1:8123'], synthetic: true });
  const h = harness({ config: golden });
  assert.equal((await h.configure()).ok, true);
  assert.equal(h.storage.chrome_relay_latch.generation, MANAGED_GOLDEN);
  assert.equal(managedGenerationFor(h.config), MANAGED_GOLDEN);
  const seen = new Set([MANAGED_GOLDEN]);
  for (const change of [{ allowed_actions: ['chrome_attachment'] }, { allowed_origins: ['http://127.0.0.1:8124'] }, { grant_id: 'grant-test-2' }, { persistent: undefined, expires_at: 1893456000123 }]) {
    Object.assign(h.config, golden, change);
    assert.equal((await h.configure()).ok, true);
    const generation = h.storage.chrome_relay_latch.generation;
    assert.equal(generation, managedGenerationFor(clean(h.config)));
    assert.ok(!seen.has(generation), 'scope change must move the generation');
    seen.add(generation);
    assert.equal(h.storage.chrome_relay_latch.stop, true, 'a new grant still requires owner attachment');
  }
});

test('managed clients are broker-issued SIDs, isolated from each other; caller-chosen names are refused', async () => {
  const h = await harness({ config: managedConfig() }).ready();
  const a = await h.open(clientSid('a'));
  const b = await h.open(clientSid('b'));
  assert.equal((await h.read(a, clientSid('a'))).status, 'ok');
  assert.equal((await h.read(b, clientSid('a'))).data.reason, 'ownership');
  assert.equal((await h.read(a, clientSid('b'))).data.reason, 'ownership');
  const creates = h.calls.create.length;
  for (const sid of ['agent-a', 'managed', 'controller', 'c-' + 'A'.repeat(32), 'c-' + 'a'.repeat(31), clientSid('a') + 'a', ' ' + clientSid('a')]) {
    const result = await h.operation('chrome_open', { url: ORIGIN + '/work' }, undefined, { session_id: sid });
    assert.equal(result.status, 'denied', sid);
  }
  assert.equal(h.calls.create.length, creates);
});

test('persistent managed grant never expires; an explicit managed expiry still does', async () => {
  const h = await harness({ config: managedConfig() }).ready();
  assert.equal(h.storage.chrome_relay_config.persistent, true);
  assert.equal(Object.hasOwn(h.storage.chrome_relay_config, 'expires_at'), false);
  assert.equal((await h.operation('chrome_open', { url: ORIGIN + '/work' }, undefined, { session_id: clientSid('c') })).status, 'ok');
  const past = harness({ config: managedConfig({ persistent: undefined, expires_at: Date.now() - 1000 }) });
  assert.equal((await past.configure()).ok, true);
  assert.equal((await past.attach()).status, 'denied');
  assert.equal(past.storage.chrome_relay_latch.stop, true);
  assert.equal(past.calls.create.length, 0);
});

test('malformed managed grants stay STOP and never fall back to legacy sessions', async () => {
  const far = Date.now() + 120000;
  for (const change of [{ persistent: false }, { persistent: 'true' }, { persistent: 1 }, { expires_at: far }, { persistent: undefined }, { mode: 'legacy' }, { mode: undefined },
    { grant_id: 'bad id' }, { grant_id: undefined }, { session_ids: [clientSid('a')] }, { extra: true }, { schema: 3 }, { allowed_origins: ['http://127.0.0.1:9000'] }]) {
    const h = harness({ config: managedConfig(change) });
    assert.equal((await h.configure()).ok, false, JSON.stringify(change));
    assert.equal((await h.attach()).status, 'denied');
    assert.equal(h.storage.chrome_relay_latch.stop, true);
    assert.equal(h.calls.create.length, 0);
  }
});

test('full managed browser restart retains grant but never adopts old tabs or references', async () => {
  const h = await harness({config:managedConfig()}).ready(), sid = clientSid('a');
  const id = await h.open(sid), old = (await h.read(id,sid)).data.refs.find(ref => ref.name === 'Increment').ref;
  // New browser realm: local storage survives; session storage does not.
  const restarted = harness({storage:h.storage,tabs:h.tabs,sessionStorage:{}});
  assert.equal((await restarted.api.recover()).ok,true);
  await restarted.api.onBrowserStartup();
  const state = (await restarted.operation('chrome_attachment',{},undefined,{session_id:sid})).data;
  assert.equal(state.stopped,false);
  assert.equal(state.technical_block,false);
  assert.deepEqual(state.owned_tab_ids,[]);
  assert.equal(state.manual_tab_id,0);
  assert.equal((await restarted.operation('chrome_click',{ref:old},id,{session_id:sid})).status,'denied');
  assert.equal((await restarted.read(id,sid)).data.reason,'ownership');
  assert.deepEqual(restarted.calls.remove,[]);
  assert.deepEqual(restarted.calls.permissionRequests,[]);
  assert.equal(restarted.calls.events.filter(e=>e.type==='managed_browser_restart').length,1);
  restarted.nextId = 50;
  assert.equal(await restarted.open(sid),50);
  assert.equal((await restarted.read(50,sid)).status,'ok');
  // A late startup event cannot revoke tabs created in this new browser realm.
  await restarted.api.onBrowserStartup();
  await restarted.operation('chrome_attachment',{},undefined,{session_id:sid});
  assert.equal((await restarted.read(50,sid)).status,'ok');
});

test('managed restart cannot lift STOP, changed grant, corrupt state or missing permissions', async () => {
  for (const cause of ['stop','scope','journal','permission','session']) {
    const h = await harness({config:twoOriginGrant()}).ready(), sid=clientSid('b');
    await h.open(sid);
    if(cause==='stop') await h.attach();
    if(cause==='scope') h.storage.chrome_relay_config.allowed_origins=[ORIGIN];
    if(cause==='journal') h.storage.chrome_relay_latch.extra=true;
    const marker = cause==='session' ? {chrome_relay_browser_generation:'corrupt'} : {};
    const restarted=harness({storage:h.storage,tabs:h.tabs,sessionStorage:marker,permissions:[ORIGIN,SECOND_ORIGIN]});
    if(cause==='permission') restarted.permissions.delete(SECOND_ORIGIN);
    await restarted.api.recover();
    await restarted.api.onBrowserStartup();
    const state=await restarted.operation('chrome_attachment',{},undefined,{session_id:sid});
    assert.ok(state.status==='denied' || state.data.stopped || state.data.technical_block,cause);
    assert.notEqual((await restarted.operation('chrome_open',{url:ORIGIN+'/new'},undefined,{session_id:sid})).status,'ok',cause);
    assert.equal(restarted.calls.create.length,0);
    assert.equal(restarted.calls.permissionRequests.length,0);
  }
});

test('visible main form and safe public GUID remain readable and actionable despite >40 hidden portal shell controls', async () => {
  for (const mode of ['synthetic']) {
  const h = await harness().ready();
  const id = await h.open();
  const root = emptyDocument(ORIGIN + '/portal');

  // Portal shell container with >40 hidden elements using various hiding mechanisms
  // (hidden attribute, aria-hidden="true", inert attribute, style="display:none", style="visibility:hidden")
  // and missing DOM fixture APIs safely (Element in this harness lacks getComputedStyle/checkVisibility).
  const shell = new Element('header', { role: 'banner' });
  const hiddenNav = new Element('nav', { 'aria-hidden': 'true' });
  for (let i = 0; i < 15; i++) {
    hiddenNav.append(new Element('button', {}, 'Hidden nav button ' + i));
  }
  const hiddenSidebar = new Element('div', { style: 'display: none;' });
  for (let i = 0; i < 15; i++) {
    hiddenSidebar.append(new Element('a', { href: '/shell/item-' + i }, 'Hidden shell link ' + i));
  }
  const inertToolbar = new Element('div', { inert: '' });
  for (let i = 0; i < 10; i++) {
    inertToolbar.append(new Element('button', {}, 'Inert tool ' + i));
  }
  const collapsedTray = new Element('div', { hidden: '' });
  for (let i = 0; i < 10; i++) {
    collapsedTray.append(new Element('button', {}, 'Collapsed action ' + i));
  }
  shell.append(hiddenNav, hiddenSidebar, inertToolbar, collapsedTray);

  // Visible main section containing safe publicGUID and visible form controls
  const main = new Element('main', { role: 'main' });
  const publicGuid = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
  const publicGuidText = new Element('p', {}, 'Application (client) ID: ' + publicGuid + ' Tenant ID: 11111111-2222-4333-8444-555555555555');
  const appNameInput = new Element('input', { type: 'text', 'aria-label': 'App name', value: '' });
  const redirectInput = new Element('input', { type: 'url', 'aria-label': 'Redirect URI', value: '' });
  const submitButton = new Element('button', { type: 'submit' }, 'Save app');
  const mainForm = new Element('form', { action: '/apps' }).append(appNameInput, redirectInput, submitButton);
  main.append(publicGuidText, mainForm);

  root.body.append(shell, main);
  h.documents.set(id, root);

  const read = await h.read(id);
  assert.equal(read.status, 'ok');

  // Verify safe synthetic public GUID is in excerpt (not starved by shell text)
  assert.match(read.data.excerpt, new RegExp(publicGuid));

  // Verify visible main form controls are captured in refs (not starved by 50 hidden shell controls)
  const appNameRef = read.data.refs.find(ref => ref.name === 'App name');
  const submitRef = read.data.refs.find(ref => ref.name === 'Save app');
  assert.ok(appNameRef, 'visible App name input must be present in refs');
  assert.ok(submitRef, 'visible Save app submit button must be present in refs');

  // Verify none of the hidden shell controls leaked into refs
  assert.equal(read.data.refs.some(ref => ref.name.startsWith('Hidden nav button')), false);
  assert.equal(read.data.refs.some(ref => ref.name.startsWith('Hidden shell link')), false);
  assert.equal(read.data.refs.some(ref => ref.name.startsWith('Inert tool')), false);
  assert.equal(read.data.refs.some(ref => ref.name.startsWith('Collapsed action')), false);

  // Verify actionable: typing into App name and clicking Save app work
  const typed = await h.operation('chrome_type', { ref: appNameRef.ref, text: 'My Registered App' }, id);
  assert.equal(typed.status, 'ok');
  assert.equal(appNameInput.value, 'My Registered App');

  // Mutation correctly expired prior snapshot references (stale reference check)
  const priorSubmitStale = await h.operation('chrome_click', { ref: submitRef.ref }, id);
  assert.equal(priorSubmitStale.status, 'denied');
  assert.equal(priorSubmitStale.data.reason, 'stale_ref');

  // Immediate read + use works for next mutation
  const activeRead = await h.read(id);
  const activeSubmitRef = activeRead.data.refs.find(ref => ref.name === 'Save app');
  assert.ok(activeSubmitRef, 'visible Save app submit button must remain readable after typing');
  const clicked = await h.operation('chrome_click', { ref: activeSubmitRef.ref }, id);
  assert.equal(clicked.status, 'ok');
  assert.equal(submitButton.clicks, 1);

  // Verify invisible/stale reference denial:
  // If an element becomes hidden after snapshot, mutation is denied with stale_ref
  const freshRead = await h.read(id);
  const nextSubmitRef = freshRead.data.refs.find(ref => ref.name === 'Save app');
  assert.ok(nextSubmitRef);
  main.setAttribute('style', 'display: none;');
  const deniedClick = await h.operation('chrome_click', { ref: nextSubmitRef.ref }, id);
  assert.equal(deniedClick.status, 'denied');
  assert.equal(deniedClick.data.reason, 'stale_ref');
  assert.equal(submitButton.clicks, 1, 'invisible element must not receive click');

  // [P1 regression]: contenteditable ancestor excludes nested sections and text
  const docEditable = emptyDocument(ORIGIN + '/editable');
  const editableDiv = new Element('div', { contenteditable: 'true' });
  const editableMain = new Element('main', {}, 'EDITOR_VALUE_CANARY');
  editableDiv.append(editableMain);
  docEditable.body.append(editableDiv);
  h.realms.delete(id);
  h.documents.set(id, docEditable);
  const readEditable = await h.read(id);
  assert.equal(readEditable.status, 'ok');
  assert.equal(readEditable.data.excerpt.includes('EDITOR_VALUE_CANARY'), false, 'contenteditable ancestor must exclude nested main text');

  // [P1 regression]: global visit budget and deadline control during section discovery
  const docLarge = emptyDocument(ORIGIN + '/large');
  let sentinelReadsBeyondBudget = 0;
  for (let i = 0; i < 50; i++) {
    const parentEl = new Element('div');
    for (let j = 0; j < 100; j++) {
      const idx = i * 100 + j;
      const childEl = new Element('div');
      if (idx >= 2000) {
        const origGetAttr = childEl.getAttribute.bind(childEl);
        childEl.getAttribute = function(name) {
          sentinelReadsBeyondBudget++;
          return origGetAttr(name);
        };
      }
      parentEl.append(childEl);
    }
    docLarge.body.append(parentEl);
  }
  h.realms.delete(id);
  h.documents.set(id, docLarge);
  const readLarge = await h.read(id);
  assert.equal(readLarge.status, 'ok');
  assert.equal(sentinelReadsBeyondBudget, 0, 'sentinel attributes beyond visit budget must not be read');

  // [P2 regression]: preserve depth relative to body (cutoff after 30 levels)
  const docDeep = emptyDocument(ORIGIN + '/deep');
  let deepCurr = docDeep.body;
  for (let i = 0; i < 15; i++) {
    const d = new Element('div');
    deepCurr.append(d);
    deepCurr = d;
  }
  const deepMain = new Element('main', { role: 'main' });
  deepCurr.append(deepMain);
  deepCurr = deepMain;
  for (let i = 0; i < 27; i++) {
    const d = new Element('div');
    deepCurr.append(d);
    deepCurr = d;
  }
  const deepButton = new Element('button', {}, 'Deep button at depth 43');
  deepCurr.append(deepButton);
  h.realms.delete(id);
  h.documents.set(id, docDeep);
  const readDeep = await h.read(id);
  assert.equal(readDeep.status, 'ok');
  assert.equal(readDeep.data.refs.some(ref => ref.name === 'Deep button at depth 43'), false, 'controls beyond depth 30 relative to body must not be collected');

  // [P2 regression]: display: contents with checkVisibility() returning false on container must not cut off visible children
  const docContents = emptyDocument(ORIGIN + '/contents');
  const contentsContainer = new Element('div', { style: 'display: contents;' });
  contentsContainer.checkVisibility = function() { return false; };
  const contentsButton = new Element('button', {}, 'Visible contents button');
  contentsButton.checkVisibility = function() { return true; };
  contentsContainer.append(contentsButton);
  docContents.body.append(contentsContainer);
  h.realms.delete(id);
  h.documents.set(id, docContents);
  const readContents = await h.read(id);
  assert.equal(readContents.status, 'ok');
  assert.ok(readContents.data.refs.find(ref => ref.name === 'Visible contents button'), 'visible child inside display:contents must be captured in refs');
  assert.ok(readContents.data.excerpt.includes('Visible contents button'), 'visible text inside display:contents must be captured in excerpt');

  // [P1 regression 1]: visible <main> followed by 2100 empty divs in body
  const docAfter = emptyDocument(ORIGIN + '/after');
  const afterMain = new Element('main', { role: 'main' });
  const afterGuid = '11111111-after-4ccc-8ddd-222222222222';
  afterMain.append(new Element('p', {}, 'App ID: ' + afterGuid));
  const afterButton = new Element('button', {}, 'Save app after');
  afterMain.append(afterButton);
  docAfter.body.append(afterMain);
  let sentinelAfterBeyondBudget = 0;
  for (let i = 0; i < 2100; i++) {
    const d = new Element('div');
    if (i >= 2000) {
      const origGetAttr = d.getAttribute.bind(d);
      d.getAttribute = function(name) {
        sentinelAfterBeyondBudget++;
        return origGetAttr(name);
      };
    }
    docAfter.body.append(d);
  }
  h.realms.delete(id);
  h.documents.set(id, docAfter);
  const readAfter = await h.read(id);
  assert.equal(readAfter.status, 'ok');
  assert.match(readAfter.data.excerpt, new RegExp(afterGuid), 'visible main text must be captured before trailing 2100 divs');
  const afterRef = readAfter.data.refs.find(ref => ref.name === 'Save app after');
  assert.ok(afterRef, 'Save app after button must be in refs');
  const clickedAfter = await h.operation('chrome_click', { ref: afterRef.ref }, id);
  assert.equal(clickedAfter.status, 'ok');
  assert.equal(afterButton.clicks, 1);
  assert.equal(sentinelAfterBeyondBudget, 0, 'sentinels beyond 2000 in docAfter must not be visited');

  // [P1 regression 2]: visible controls at start of <main> followed by 2100 empty divs inside <main>
  const docInside = emptyDocument(ORIGIN + '/inside');
  const insideMain = new Element('main', { role: 'main' });
  const insideGuid = '33333333-inside-4ccc-8ddd-444444444444';
  insideMain.append(new Element('p', {}, 'App ID: ' + insideGuid));
  const insideButton = new Element('button', {}, 'Save app inside');
  insideMain.append(insideButton);
  let sentinelInsideBeyondBudget = 0;
  for (let i = 0; i < 2100; i++) {
    const d = new Element('div');
    if (i >= 2000) {
      const origGetAttr = d.getAttribute.bind(d);
      d.getAttribute = function(name) {
        sentinelInsideBeyondBudget++;
        return origGetAttr(name);
      };
    }
    insideMain.append(d);
  }
  docInside.body.append(insideMain);
  h.realms.delete(id);
  h.documents.set(id, docInside);
  const readInside = await h.read(id);
  assert.equal(readInside.status, 'ok');
  assert.match(readInside.data.excerpt, new RegExp(insideGuid), 'visible main text must be captured before interior 2100 divs');
  const insideRef = readInside.data.refs.find(ref => ref.name === 'Save app inside');
  assert.ok(insideRef, 'Save app inside button must be in refs');
  const clickedInside = await h.operation('chrome_click', { ref: insideRef.ref }, id);
  assert.equal(clickedInside.status, 'ok');
  assert.equal(insideButton.clicks, 1);
  assert.equal(sentinelInsideBeyondBudget, 0, 'sentinels beyond 2000 in docInside must not be visited');

  // [P2 regression]: visibility: hidden container with visibility: visible child (positive) and inherited hidden child (negative)
  const docVis = emptyDocument(ORIGIN + '/visibility');
  const hiddenContainer = new Element('div', { style: 'visibility: hidden;' });
  // Negative: inherited visibility: hidden child
  const hiddenText = new Element('p', {}, 'CANARY_HIDDEN_INHERITED');
  const hiddenButton = new Element('button', {}, 'Hidden inherited button');
  hiddenContainer.append(hiddenText, hiddenButton);
  // Positive: overridden visibility: visible child
  const visMain = new Element('main', { style: 'visibility: visible;' });
  const visGuid = '55555555-visible-4ccc-8ddd-666666666666';
  visMain.append(new Element('p', {}, 'App ID: ' + visGuid));
  const visButton = new Element('button', {}, 'Visible override button');
  visMain.append(visButton);
  hiddenContainer.append(visMain);
  docVis.body.append(hiddenContainer);
  h.realms.delete(id);
  h.documents.set(id, docVis);
  const readVis = await h.read(id);
  assert.equal(readVis.status, 'ok');
  // Verify negative: inherited hidden child text and button are excluded
  assert.equal(readVis.data.excerpt.includes('CANARY_HIDDEN_INHERITED'), false, 'inherited visibility:hidden text must not be in excerpt');
  assert.equal(readVis.data.refs.some(ref => ref.name === 'Hidden inherited button'), false, 'inherited visibility:hidden button must not be in refs');
  // Verify positive: visibility: visible child text and button are included and actionable
  assert.match(readVis.data.excerpt, new RegExp(visGuid), 'visibility:visible child in hidden container must be in excerpt');
  const visRef = readVis.data.refs.find(ref => ref.name === 'Visible override button');
  assert.ok(visRef, 'visibility:visible button must be present in refs');
  const clickedVis = await h.operation('chrome_click', { ref: visRef.ref }, id);
  assert.equal(clickedVis.status, 'ok');
  assert.equal(visButton.clicks, 1, 'visibility:visible button must be clickable');

  // [P2 regression]: CSS cascade overrides inline visibility (computed style precedence)
  const docCascade = emptyDocument(ORIGIN + '/cascade');
  const cascadeButton = new Element('button', { style: 'visibility: visible;' }, 'Cascade hidden button');
  cascadeButton.computedStyle = { visibility: 'visible' };
  docCascade.body.append(cascadeButton);
  h.realms.delete(id);
  h.documents.set(id, docCascade);
  const readCascadeInitial = await h.read(id);
  const cascadeRef = readCascadeInitial.data.refs.find(ref => ref.name === 'Cascade hidden button');
  assert.ok(cascadeRef, 'button initially visible in computed style must be present in refs');
  // CSS rule with !important overrides inline style to visibility: hidden
  cascadeButton.computedStyle = { visibility: 'hidden' };
  const clickCascade = await h.operation('chrome_click', { ref: cascadeRef.ref }, id);
  assert.equal(clickCascade.status, 'denied');
  assert.equal(clickCascade.data.reason, 'stale_ref');
  assert.equal(cascadeButton.clicks, 0, 'computed style hidden element must not receive click');
  const readCascadeHidden = await h.read(id);
  assert.equal(readCascadeHidden.data.excerpt.includes('Cascade hidden button'), false, 'text of element hidden by computed style cascade must not be in excerpt');
  assert.equal(readCascadeHidden.data.refs.some(ref => ref.name === 'Cascade hidden button'), false, 'element hidden by computed style cascade must not be in refs');

  // [P2 regression]: content-visibility: hidden subtree inside main must not starve visible metadata
  const docCv = emptyDocument(ORIGIN + '/content-visibility');
  const cvMain = new Element('main', { role: 'main' });
  const cvGuid = '77777777-cvtest-4ccc-8ddd-888888888888';
  const cvSubtree = new Element('div', { class: 'portal-hidden-section' });
  cvSubtree.computedStyle = { contentVisibility: 'hidden' };
  cvSubtree.checkVisibility = function() { return false; };
  cvSubtree.append(new Element('p', {}, 'HIDDEN_CV_CANARY_'.repeat(400)));
  const cvVisibleP = new Element('p', {}, 'Application ID: ' + cvGuid);
  const cvSaveButton = new Element('button', {}, 'Save app cv');
  cvMain.append(cvSubtree, cvVisibleP, cvSaveButton);
  docCv.body.append(cvMain);
  h.realms.delete(id);
  h.documents.set(id, docCv);
  const readCv = await h.read(id);
  assert.equal(readCv.status, 'ok');
  assert.equal(readCv.data.excerpt.includes('HIDDEN_CV_CANARY_'), false, 'content-visibility: hidden canary text must not be in excerpt');
  assert.match(readCv.data.excerpt, new RegExp(cvGuid), 'visible public GUID must remain present in excerpt');
  const cvSaveRef = readCv.data.refs.find(ref => ref.name === 'Save app cv');
  assert.ok(cvSaveRef, 'Save button must be captured in refs');
  const clickedCv = await h.operation('chrome_click', { ref: cvSaveRef.ref }, id);
  assert.equal(clickedCv.status, 'ok');
  assert.equal(cvSaveButton.clicks, 1);

  // [P2 regression]: dialog and main combined text must respect safeText 6000-char limit
  const docDialogMain = emptyDocument(ORIGIN + '/dialog-main');
  const bigDialog = new Element('dialog', { role: 'dialog' });
  bigDialog.append(new Element('p', {}, 'DIALOG_TEXT_'.repeat(450)));
  const bigMain = new Element('main', { role: 'main' });
  bigMain.append(new Element('p', {}, 'MAIN_TEXT_'.repeat(450)));
  docDialogMain.body.append(bigDialog, bigMain);
  h.realms.delete(id);
  h.documents.set(id, docDialogMain);
  const readDialogMain = await h.read(id);
  assert.equal(readDialogMain.status, 'ok');
  assert.ok(readDialogMain.data.diagnostics.main_excerpt_chars <= 6000, 'main_excerpt_chars must not exceed 6000 chars');
  assert.ok(readDialogMain.data.excerpt.length <= 5000, 'excerpt length must not exceed 5000 chars');
  assert.equal(readDialogMain.data.truncated, true, 'read must be flagged as truncated when combined text exceeds budget');
  assert.ok(readDialogMain.data.excerpt.startsWith('DIALOG_TEXT_'), 'dialog text must precede main text');

  // [P2 regression]: CSS cascade overrides inline display:none and content-visibility:hidden (computed style precedence)
  const docCascadeDisplay = emptyDocument(ORIGIN + '/cascade-display');
  // Main with style="display: none;" overridden by computedStyle = { display: 'block' }
  const cascadeMain = new Element('main', { role: 'main', style: 'display: none;' });
  cascadeMain.computedStyle = { display: 'block' };
  const cascadeGuid = '88888888-disp-4ccc-8ddd-999999999999';
  cascadeMain.append(new Element('p', {}, 'App ID: ' + cascadeGuid));
  const cascadeSaveBtn = new Element('button', {}, 'Save app cascade');
  cascadeMain.append(cascadeSaveBtn);
  docCascadeDisplay.body.append(cascadeMain);
  h.realms.delete(id);
  h.documents.set(id, docCascadeDisplay);
  const readCascadeDisp = await h.read(id);
  assert.equal(readCascadeDisp.status, 'ok');
  assert.match(readCascadeDisp.data.excerpt, new RegExp(cascadeGuid), 'display:none overridden by computed display:block must be readable in excerpt');
  const cascadeSaveRef = readCascadeDisp.data.refs.find(ref => ref.name === 'Save app cascade');
  assert.ok(cascadeSaveRef, 'Save app button with computed display:block must be present in refs');
  const clickCascadeDisp = await h.operation('chrome_click', { ref: cascadeSaveRef.ref }, id);
  assert.equal(clickCascadeDisp.status, 'ok');
  assert.equal(cascadeSaveBtn.clicks, 1, 'button with computed display:block must receive click');

  // Section with style="content-visibility: hidden;" overridden by computedStyle = { contentVisibility: 'visible' }
  const docCascadeCv = emptyDocument(ORIGIN + '/cascade-cv');
  const cvOverrideSection = new Element('section', { style: 'content-visibility: hidden;' });
  cvOverrideSection.computedStyle = { contentVisibility: 'visible' };
  const cvOverrideGuid = '99999999-cvvis-4ccc-8ddd-000000000000';
  cvOverrideSection.append(new Element('p', {}, 'App ID: ' + cvOverrideGuid));
  const cvOverrideBtn = new Element('button', {}, 'CV Override button');
  cvOverrideSection.append(cvOverrideBtn);
  docCascadeCv.body.append(cvOverrideSection);
  h.realms.delete(id);
  h.documents.set(id, docCascadeCv);
  const readCascadeCv = await h.read(id);
  assert.equal(readCascadeCv.status, 'ok');
  assert.match(readCascadeCv.data.excerpt, new RegExp(cvOverrideGuid), 'content-visibility:hidden overridden by computed visible must be readable in excerpt');
  const cvOverrideRef = readCascadeCv.data.refs.find(ref => ref.name === 'CV Override button');
  assert.ok(cvOverrideRef, 'button inside computed contentVisibility:visible section must be present in refs');
  const clickCascadeCv = await h.operation('chrome_click', { ref: cvOverrideRef.ref }, id);
  assert.equal(clickCascadeCv.status, 'ok');
  assert.equal(cvOverrideBtn.clicks, 1);

  // Negative check: style="display: none;" WITHOUT computed style override remains excluded
  const docNegativeDisp = emptyDocument(ORIGIN + '/negative-display');
  const negSection = new Element('div', { style: 'display: none;' });
  negSection.append(new Element('p', {}, 'CANARY_RAW_DISPLAY_NONE'));
  negSection.append(new Element('button', {}, 'Raw hidden button'));
  docNegativeDisp.body.append(negSection);
  h.realms.delete(id);
  h.documents.set(id, docNegativeDisp);
  const readNegDisp = await h.read(id);
  assert.equal(readNegDisp.status, 'ok');
  assert.equal(readNegDisp.data.excerpt.includes('CANARY_RAW_DISPLAY_NONE'), false, 'raw display:none without computed override must remain excluded from excerpt');
  assert.equal(readNegDisp.data.refs.some(ref => ref.name === 'Raw hidden button'), false, 'raw display:none without computed override must remain excluded from refs');

  // [P2 regression]: shared 500 candidate limit with dialog/main priority across 1200 controls
  const doc1200 = emptyDocument(ORIGIN + '/candidates-1200');
  // 400 controls in body
  for (let i = 0; i < 400; i++) {
    doc1200.body.append(new Element('button', {}, 'Body button ' + i));
  }
  // 400 controls in main
  const main1200 = new Element('main', { role: 'main' });
  for (let i = 0; i < 400; i++) {
    main1200.append(new Element('button', {}, 'Main button ' + i));
  }
  doc1200.body.append(main1200);
  // 400 controls in dialog
  const dialog1200 = new Element('dialog', { role: 'dialog' });
  for (let i = 0; i < 400; i++) {
    dialog1200.append(new Element('button', {}, 'Dialog button ' + i));
  }
  doc1200.body.append(dialog1200);
  h.realms.delete(id);
  h.documents.set(id, doc1200);

  const read1200 = await h.read(id);
  assert.equal(read1200.status, 'ok');
  assert.equal(read1200.data.truncated, true, 'read must be truncated due to 500 candidate pool cap and 40 element cap');
  assert.equal(read1200.data.refs.length, 40);
  assert.ok(read1200.data.refs.every(ref => ref.name.startsWith('Dialog button ')), 'top 40 refs must be from highest-priority dialog section');

  // Priority preservation: Body (1185) + Main (10) + Dialog (5) = 1200 controls total
  // Main and Dialog controls must not be starved by 1185 preceding body controls
  const docPriority = emptyDocument(ORIGIN + '/priority-1200');
  for (let i = 0; i < 1185; i++) {
    docPriority.body.append(new Element('button', {}, 'Bulk body button ' + i));
  }
  const prioMain = new Element('main', { role: 'main' });
  for (let i = 0; i < 10; i++) {
    prioMain.append(new Element('button', {}, 'Priority main button ' + i));
  }
  docPriority.body.append(prioMain);
  const prioDialog = new Element('dialog', { role: 'dialog' });
  for (let i = 0; i < 5; i++) {
    prioDialog.append(new Element('button', {}, 'Priority dialog button ' + i));
  }
  docPriority.body.append(prioDialog);
  h.realms.delete(id);
  h.documents.set(id, docPriority);

  const readPrio = await h.read(id);
  assert.equal(readPrio.status, 'ok');
  assert.equal(readPrio.data.refs.length, 40);
  for (let i = 0; i < 5; i++) {
    assert.ok(readPrio.data.refs.some(ref => ref.name === 'Priority dialog button ' + i), 'dialog button ' + i + ' must be in refs');
  }
  for (let i = 0; i < 10; i++) {
    assert.ok(readPrio.data.refs.some(ref => ref.name === 'Priority main button ' + i), 'main button ' + i + ' must be in refs');
  }
  const bodyCount = readPrio.data.refs.filter(ref => ref.name.startsWith('Bulk body button ')).length;
  assert.equal(bodyCount, 25, 'remaining 25 slots must be filled by body controls');
  }
});
