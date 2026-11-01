'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const basePath = path.join(__dirname, 'test_extension.cjs');
let source = fs.readFileSync(basePath, 'utf8').split("test('same-origin frameset")[0];
source = source.replace('api, config, chrome, wire(id)', 'context, api, config, chrome, wire(id)');
source += '\nmodule.exports = { harness, ORIGIN };';
const fixture = new Module(basePath, module);
fixture.filename = basePath; fixture.paths = module.paths; fixture._compile(source, basePath);
const { harness, ORIGIN } = fixture.exports;
const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'extension', 'manifest.json'), 'utf8'));
const login = { origin: ORIGIN, pages: ['/login'], posts: ['/session'] };
const actions = ['chrome_attachment','chrome_open','chrome_read','chrome_type','chrome_click','chrome_wait','chrome_close','chrome_saved_login'];

test('debugger is required; the owner toolbar gesture requests only optional host origins', async () => {
  const h = harness({ config: { saved_login: login, allowed_actions: actions } });
  const requests = [];
  const chromeRequest = h.chrome.permissions.request.bind(h.chrome.permissions);
  h.chrome.permissions.request = spec => {
    const gestureAtCall = h.userGesture;
    requests.push({ spec: JSON.parse(JSON.stringify(spec)), gestureAtCall });
    assert.equal(gestureAtCall, true, 'optional host request stays in the toolbar gesture');
    // Chromium rejects debugger in permissions.request even if a buggy manifest lists it optional.
    if (spec.permissions && spec.permissions.includes('debugger')) throw new TypeError('debugger cannot be requested as optional');
    assert.deepEqual(Object.keys(spec).sort(), ['origins']);
    return chromeRequest(spec);
  };
  await h.configure();
  const result = await h.attach();
  assert.equal(result.reason, 'owner_attach', JSON.stringify({ result, requests }));
  assert.deepEqual(requests, [{ spec: { origins: [ORIGIN + '/*'] }, gestureAtCall: true }]);
  assert.ok(manifest.permissions.includes('debugger'));
  assert.ok(!(manifest.optional_permissions || []).includes('debugger'));
  assert.deepEqual(manifest.optional_host_permissions, ['https://*/*']);
});
