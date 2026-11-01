'use strict';
// Synthetic verifier only. Agents interact through mcp.py, never this process.
// Only fixed synthetic configuration, owner gestures and worker termination.
// No CDP operation is exposed by the agent relay.
const readline = require('node:readline');
let context, worker, manualId, input, config, dialogs = new Set();
const lines = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
let commandQueue = Promise.resolve();
function emit(value) { process.stdout.write(JSON.stringify(value) + '\n'); }
async function launch() {
  const {chromium} = require(input.playwright_root);
  context = await chromium.launchPersistentContext(input.profile_dir, {
    executablePath: input.browser_exe, headless: true,
    args: ['--disable-extensions-except=' + input.extension_dir, '--load-extension=' + input.extension_dir],
    viewport: {width: 1000, height: 720}, timeout: 25000
  });
  context.on('page', bindPage);
  context.pages().forEach(bindPage);
  worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker', {timeout: 25000});
  const page = context.pages()[0] || await context.newPage();
  await page.goto(input.fixture_url + '/manual', {waitUntil: 'domcontentloaded'});
  return {extension_id: worker.url().split('/')[2], node_pid: process.pid};
}
function bindPage(page) {
  page.on('dialog', dialog => { dialogs.add(dialog); });
}
async function configure(value) {
  config = value;
  const result = await worker.evaluate(async ({cfg, url}) => {
    await chrome.storage.local.set({chrome_relay_config: cfg});
    await recover();
    const tab = (await chrome.tabs.query({})).find(t => t.url === url);
    if (!tab) throw new Error('manual_fixture_missing');
    return {...(await onToolbarClicked(tab)), manual_tab_id:tab.id};
  }, {cfg: config, url: input.fixture_url + '/manual'});
  if (result.status !== 'ok' || result.reason !== 'owner_attach') throw new Error('owner_attach_failed');
  manualId = result.manual_tab_id;
  return {configured: true, manual_tab_id: manualId};
}
async function ownerGesture() {
  const page = context.pages().find(p => p.url() === input.fixture_url + '/manual');
  if (!page) throw new Error('manual_fixture_missing');
  await page.bringToFront();
  const result = await worker.evaluate(async url => {
    const tab = (await chrome.tabs.query({})).find(t => t.url === url);
    if (!tab) throw new Error('manual_fixture_missing');
    return {...(await onToolbarClicked(tab)), manual_tab_id:tab.id};
  }, input.fixture_url + '/manual');
  manualId = result.manual_tab_id;
  return result;
}
async function restartWorker() {
  const extensionId = worker.url().split('/')[2];
  // Playwright retains the Worker object across an extension worker restart;
  // no second serviceworker event is emitted. Prove the execution context
  // changed using an in-memory sentinel in this synthetic fixture only.
  await worker.evaluate(() => { globalThis.__fleetSyntheticRestartProbe = 'before'; });
  // Google documents Target.closeTarget for actual worker termination. Reload
  // is a different extension lifecycle and may not reset this CFT context.
  // The target belongs to this isolated synthetic browser, never owner Chrome.
  const page = context.pages().find(p => p.url() === input.fixture_url + '/manual');
  if (!page) throw new Error('manual_fixture_missing');
  const cdp = await context.newCDPSession(page);
  try {
    const {targetInfos} = await cdp.send('Target.getTargets');
    const targets = targetInfos.filter(t => t.type === 'service_worker' && t.url === worker.url());
    if (targets.length !== 1) throw new Error('worker_identity_ambiguous');
    const closed = await cdp.send('Target.closeTarget', {targetId:targets[0].targetId});
    if (!closed.success) throw new Error('worker_termination_failed');
  } finally { await cdp.detach(); }
  // onCreated wakes the extension using its existing browser event listener.
  const wake = await context.newPage();
  await wake.close();
  const deadline = Date.now() + 25000;
  while (Date.now() < deadline) {
    const candidate = context.serviceWorkers().find(w => w.url().split('/')[2] === extensionId);
    if (candidate) {
      let timer;
      try {
        const restarted = await Promise.race([
          candidate.evaluate(() => ({changed:typeof globalThis.__fleetSyntheticRestartProbe === 'undefined', extension_id:chrome.runtime.id})),
          new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('worker_restart_timeout')), Math.max(1, deadline - Date.now())); })
        ]).finally(() => clearTimeout(timer));
        if (restarted.changed && restarted.extension_id === extensionId) {
          worker = candidate;
          await worker.evaluate(async () => { await recover(); return true; });
          return {worker_restarted:true, extension_id:extensionId};
        }
      } catch (_) { /* A destroyed old context may race the replacement. */ }
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('worker_restart_not_observed');
}
async function snapshot() {
  const pages = [];
  for (const page of context.pages()) {
    if (!page.url().startsWith(input.fixture_url)) continue;
    pages.push({manual: page.url().endsWith('/manual'),
      increments: await page.locator('#count').textContent().catch(() => null),
      sends: await page.locator('#sent').textContent().catch(() => null),
      delays: await page.locator('#delay').textContent().catch(() => null)});
  }
  const manual = context.pages().find(p => p.url() === input.fixture_url + '/manual');
  const tabs = await worker.evaluate(async id => (await chrome.tabs.query({})).map(tab => ({
    tab_id:tab.id, active:tab.active, window_id:tab.windowId, manual:tab.id === id
  })), manualId);
  if (manual && input.screenshot_path) await manual.screenshot({path:input.screenshot_path, animations:'disabled'});
  return {pages, tabs, held_dialogs: dialogs.size, manual_tab_id: manualId, fixture_screenshot_written:!!manual};
}
async function command(value) {
  if (!input) { input = value; emit({event:'ready', ...(await launch())}); return; }
  if (value.command === 'configure') { emit({id:value.id, ...(await configure(value.config))}); return; }
  if (value.command === 'owner-gesture') { emit({id:value.id, ...(await ownerGesture())}); return; }
  if (value.command === 'worker-restart') { emit({id:value.id, ...(await restartWorker())}); return; }
  if (value.command === 'snapshot') { emit({id:value.id, ...(await snapshot())}); return; }
  if (value.command === 'release-dialogs') {
    for (const dialog of dialogs) await dialog.dismiss().catch(() => {});
    dialogs.clear(); emit({id:value.id, released:true}); return;
  }
  if (value.command === 'restart') {
    await context.close(); dialogs.clear();
    emit({id:value.id, ...(await launch())}); return;
  }
  if (value.command === 'close') { await context.close(); emit({id:value.id, closed:true}); process.exitCode = 0; lines.close(); return; }
  throw new Error('unknown_harness_command');
}
lines.on('line', line => {
  commandQueue = commandQueue.then(() => command(JSON.parse(line))).catch(() => { emit({event:'error', reason:'browser_harness_failed'}); process.exitCode = 1; });
});
lines.on('close', () => { commandQueue.then(async () => { if (context) await context.close().catch(() => {}); }); });
