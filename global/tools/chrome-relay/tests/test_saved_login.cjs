'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const vm = require('node:vm');
const { createHash } = require('node:crypto');

// Reuse the behavioral browser harness, without running its separate tests.
const basePath = path.join(__dirname, 'test_extension.cjs');
let source = fs.readFileSync(basePath, 'utf8').split("test('same-origin frameset")[0];
source = source.replace('api, config, chrome, wire(id)', 'context, api, config, chrome, wire(id)');
source += '\nmodule.exports = { harness, ORIGIN, deferred };';
const fixture = new Module(basePath, module);
fixture.filename = basePath; fixture.paths = module.paths;
fixture._compile(source, basePath);
const { harness, ORIGIN, deferred } = fixture.exports;
const sha = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const ACTION = 'chrome_saved_login';
const binding = { origin: ORIGIN, pages: ['/login'], posts: ['/session'] };

function loginHarness(options = {}) {
  const managed = options.managed === true;
  const config = { allowed_actions: ['chrome_attachment','chrome_open','chrome_read','chrome_type','chrome_click','chrome_wait','chrome_close', ACTION], saved_login: binding, ...options.config };
  if (managed) Object.assign(config, { schema: 2, mode: 'managed', grant_id: 'g-test', persistent: true, session_ids: undefined });
  const h = harness({ storage: options.storage, tabs: options.tabs, sessionStorage: options.sessionStorage, config });
  if (managed) { delete h.config.session_ids; delete h.config.expires_at; }
  h.wire = id => {
    const c = h.config;
    const fields = c.schema === 2 ? ['managed/1', c.grant_id, c.profile_id, c.extension_token, c.allowed_origins.slice().sort(), c.allowed_actions.slice().sort(), c.synthetic, c.persistent ? null : Math.floor(c.expires_at)] : [c.profile_id, c.extension_token, Math.floor(c.expires_at)];
    if (c.saved_login) fields.push(sha(['saved_login/1', c.saved_login.origin, c.saved_login.pages.slice().sort(), c.saved_login.posts.slice().sort()]));
    return sha(fields).slice(0,32) + ':' + sha(id).slice(0,32);
  };
  h.native = []; h.probes = []; h.scriptDisabled = false; h.hook = null; h.probeHook = null;
  const oldContains = h.chrome.permissions.contains;
  h.chrome.permissions.contains = spec => spec.permissions ? Promise.resolve(options.debugger !== false) : oldContains(spec);
  h.chrome.permissions.request = async () => true; // Only harness owner gesture, not production permission code.
  h.chrome.debugger = {
    async attach(target, version) { h.native.push(['attach', target.tabId, version]); if (h.hook) await h.hook('attach'); },
    async sendCommand(target, method, params) {
      h.native.push([method, target.tabId, {...params}]);
      if (method === 'Emulation.setScriptExecutionDisabled' && params.value === true && options.failScriptDisable === true) throw new Error('synthetic_script_freeze_failure');
      if (method === 'Emulation.setScriptExecutionDisabled') h.scriptDisabled = params.value;
      if (h.hook) await h.hook(params.type || (params.value ? 'disable' : 'restore'));
    },
    async detach(target) { h.native.push(['detach', target.tabId]); }
  };
  vm.runInContext('globalThis.FleetLoginGate = Object.freeze({ loginFormProbe: function loginFormProbe() {} });', h.context);
  h.chrome.scripting.executeScript = async spec => {
    assert.equal(spec.func.name, 'loginFormProbe');
    assert.deepEqual(Object.keys(spec.target).sort(), ['frameIds','tabId']);
    assert.deepEqual(Array.from(spec.target.frameIds), [0]);
    const command = spec.args[0]; h.probes.push(command.mode);
    assert.equal(command.origin, binding.origin);
    if (command.mode === 'arm') { assert.equal(h.scriptDisabled, true); assert.equal(command.allowInline, true); }
    else assert.equal(command.allowInline, undefined);
    if (h.probeHook) await h.probeHook(command.mode);
    let result = { eligible: true, origin: ORIGIN, path:'/login', target_origin:ORIGIN,target_path:'/session', saved:true,x:20,y:30,inline_submit:false,inline_click:false };
    if (command.mode === 'arm') result.armed = true;
    if (['status','disarm'].includes(command.mode)) result = { outcome: 'submitted' };
    if (options.probeResult) result = options.probeResult(command.mode, result);
    return vm.runInContext('JSON.parse(' + JSON.stringify(JSON.stringify([{ frameId:0, result }])) + ')', h.context);
  };
  h.login = (tab, id='login', sid=managed ? 'c-'+'a'.repeat(32) : 'agent-a', args={}) => h.raw({ request_id:h.wire(id),session_id:sid,profile_id:h.config.profile_id,action:ACTION,tab_id:tab,args });
  h.start = async () => { await h.configure(); assert.equal((await h.attach()).reason,'owner_attach'); h.loginTab = await h.open(managed ? 'c-'+'a'.repeat(32) : 'agent-a', ORIGIN+'/login'); return h; };
  return h;
}
const presses = h => h.native.filter(call => call[0] === 'Input.dispatchMouseEvent' && call[2].type === 'mousePressed');

test('saved login uses only fixed native input, restores page scripting, disarms and detaches', async () => {
  const h = await loginHarness().start();
  const result = await h.login(h.loginTab);
  assert.equal(result.status,'ok'); assert.equal(result.data.authentication,'unverified');
  assert.equal(presses(h).length,1); assert.equal(h.scriptDisabled,false);
  assert.deepEqual(h.probes,['inspect','arm','status','disarm']);
  assert.equal(h.native.at(-1)[0],'detach');
  assert.deepEqual([...new Set(h.native.map(c=>c[0]))].sort(), ['Emulation.setScriptExecutionDisabled','Input.dispatchMouseEvent','attach','detach'].sort());
});

test('saved-login probes request immediate injection while a document is still loading', async () => {
  const h = await loginHarness().start();
  const nativeExecute = h.chrome.scripting.executeScript;
  h.probeImmediate = [];
  h.chrome.scripting.executeScript = async spec => {
    h.probeImmediate.push(spec.injectImmediately === true);
    return nativeExecute(spec);
  };
  await h.login(h.loginTab);
  assert.deepEqual(h.probeImmediate, [true, true, true, true]);
});

test('managed configuration validates login binding and action coupling before attachment', async () => {
  const good = await loginHarness({managed:true}).start();
  assert.equal((await good.login(good.loginTab)).status,'ok');
  for (const extra of [{saved_login:{...binding,password:'not-allowed'}},{saved_login:{...binding,pages:['/oauth/authorize']}},{saved_login:{...binding,posts:['/saml/consume']}},{allowed_actions:['chrome_attachment','chrome_open']}]) {
    const h = loginHarness({managed:true,config:extra}); await h.configure();
    assert.notEqual((await h.attach()).reason,'owner_attach'); assert.equal(h.native.length,0);
  }
});

test('unknown args, foreign tab and absent owner permission cannot cause input', async () => {
  const h = await loginHarness().start();
  for (const args of [{password:'x'},{selector:'button'},{x:20},{method:'Input.dispatchMouseEvent'}]) assert.equal((await h.login(h.loginTab,'bad-'+JSON.stringify(args),'agent-a',args)).status,'denied');
  assert.equal((await h.login(1,'manual')).status,'denied');
  assert.equal((await h.login(h.loginTab,'foreign','agent-b')).status,'denied');
  const no = await loginHarness({debugger:false}).start(); assert.equal((await no.login(no.loginTab)).data.reason,'needs_owner');
  assert.equal(presses(h).length,0); assert.equal(no.native.length,0);
});

test('ordinary credential read and typing remain denied on an explicitly allowed login page', async () => {
  const h = await loginHarness().start();
  assert.equal((await h.read(h.loginTab)).status,'denied');
  assert.equal((await h.operation('chrome_type',{ref:'r1_1',text:'secret'},h.loginTab)).status,'denied');
  assert.equal(h.native.length,0);
});

test('missing autofill and changed form are denied before native mouse input', async () => {
  const none = await loginHarness({probeResult:()=>({eligible:false,reason:'needs_owner'})}).start();
  assert.equal((await none.login(none.loginTab)).data.reason,'needs_owner'); assert.equal(none.native.length,0);
  const changed = await loginHarness({probeResult:(mode,r)=>mode==='arm'?{...r,x:90}:r}).start();
  assert.equal((await changed.login(changed.loginTab)).data.reason,'form_changed');
  assert.equal(presses(changed).length,0); assert.equal(changed.scriptDisabled,false); assert.equal(changed.native.at(-1)[0],'detach');
});

test('inline submit and click attributes are accepted only after the script freeze and are compared fresh', async () => {
  const h = await loginHarness({probeResult:(mode,r)=>['inspect','arm'].includes(mode)?{...r,inline_submit:true,inline_click:true}:r}).start();
  assert.equal((await h.login(h.loginTab)).status,'ok');
  assert.deepEqual(h.probes,['inspect','arm','status','disarm']);
  assert.equal(h.native.find(call=>call[0]==='Emulation.setScriptExecutionDisabled'&&call[2].value===true)[0],'Emulation.setScriptExecutionDisabled');
  const changed = await loginHarness({probeResult:(mode,r)=>mode==='inspect'?{...r,inline_submit:true}:r}).start();
  assert.equal((await changed.login(changed.loginTab)).data.reason,'form_changed');
  assert.equal(presses(changed).length,0); assert.equal(changed.scriptDisabled,false);
});

test('failure to freeze page scripts never dispatches the saved-login click', async () => {
  const h = await loginHarness({failScriptDisable:true}).start();
  const result = await h.login(h.loginTab);
  assert.equal(result.data.reason,'needs_owner');
  assert.deepEqual(h.probes,['inspect','disarm']);
  assert.equal(h.native.filter(call=>call[0]==='attach').length,1);
  assert.equal(h.native.filter(call=>call[0]==='Emulation.setScriptExecutionDisabled'&&call[2].value===true).length,1);
  assert.equal(presses(h).length,0); assert.equal(h.scriptDisabled,false); assert.equal(h.native.at(-1)[0],'detach');
});

test('wrong host, path or query cannot reach the saved-login form probe', async () => {
  for (const url of ['https://evil.example.test/login',ORIGIN+'/other',ORIGIN+'/login?next=/session']) {
    const h = await loginHarness().start(); h.tabs.get(h.loginTab).url = url;
    const result = await h.login(h.loginTab);
    assert.equal(result.data.reason,'login_route'); assert.equal(h.native.length,0); assert.deepEqual(h.probes,[]);
  }
});

test('unsafe submit overrides and script-only cancellation never report a successful submit', async () => {
  const unsafe = await loginHarness({probeResult:mode=>mode==='inspect'?{eligible:false,reason:'unsupported_login'}:undefined}).start();
  assert.equal((await unsafe.login(unsafe.loginTab)).data.reason,'unsupported_login'); assert.equal(presses(unsafe).length,0);
  const jsOnly = await loginHarness({probeResult:(mode,r)=>mode==='status'?{outcome:'prevented'}:r}).start();
  const result = await jsOnly.login(jsOnly.loginTab);
  assert.equal(result.status,'unknown'); assert.equal(result.data.reason,'unsupported_login'); assert.equal(presses(jsOnly).length,1);
});

test('two request IDs racing for one tab submit once and restart preserves the prohibition', async () => {
  const h = await loginHarness().start(); const wait = deferred(); const began = deferred();
  h.hook = async phase => { if (phase === 'disable') { began.resolve(); await wait.promise; } };
  const first = h.login(h.loginTab,'first'); await began.promise;
  const second = await h.login(h.loginTab,'second'); assert.equal(second.data.reason,'login_attempted');
  wait.resolve(); assert.equal((await first).status,'ok'); assert.equal(presses(h).length,1);
  const restart = loginHarness({storage:h.storage,tabs:h.tabs,sessionStorage:h.sessionStorage,config:h.config}); await restart.configure();
  assert.equal((await restart.operation('chrome_attachment')).status,'ok');
  assert.equal((await restart.login(h.loginTab,'third')).data.reason,'login_attempted'); assert.equal(restart.native.length,0);
});

for (const point of ['attach','disable','mousePressed']) test('owner STOP during '+point+' prevents subsequent submit and still restores/detaches', async () => {
  const h = await loginHarness().start();
  h.hook = async phase => { if (phase === point) { h.hook=null; assert.equal((await h.attach()).reason,'owner_stop'); } };
  const result = await h.login(h.loginTab);
  assert.notEqual(result.status,'ok'); assert.equal(h.scriptDisabled,false); assert.equal(h.native.at(-1)[0],'detach');
  assert.equal(h.native.filter(c=>c[0]==='Input.dispatchMouseEvent'&&c[2].type==='mouseReleased').length,0);
  assert.equal((await h.login(h.loginTab,'after-stop')).status,'denied');
});

test('native error after first press is unknown and never replayed with a new request', async () => {
  const h=await loginHarness().start(); h.hook=async phase=>{if(phase==='mousePressed')throw new Error('synthetic_native_error');};
  const result=await h.login(h.loginTab); assert.equal(result.status,'unknown');
  assert.equal(h.scriptDisabled,false); assert.equal(h.native.at(-1)[0],'detach');
  assert.equal((await h.login(h.loginTab,'retry')).data.reason,'login_attempted'); assert.equal(presses(h).length,1);
});

test('an old completed login cannot be evicted by observation cache pressure', async () => {
  const h=await loginHarness().start(); assert.equal((await h.login(h.loginTab)).status,'ok');
  // Observations do not enter the mutation journal; a new login ID remains forbidden.
  for(let i=0;i<80;i++) await h.operation('chrome_attachment');
  assert.equal((await h.login(h.loginTab,'later')).data.reason,'login_attempted'); assert.equal(presses(h).length,1);
});

test('late native attach after the deadline is restored and detached without input', async () => {
  const h=await loginHarness().start(); const delayed=deferred();
  h.hook=async phase=>{if(phase==='attach')await delayed.promise;};
  const running=h.login(h.loginTab); setTimeout(()=>delayed.resolve(),5200);
  assert.notEqual((await running).status,'ok');
  await new Promise(resolve=>setTimeout(resolve,100));
  assert.equal(presses(h).length,0); assert.equal(h.scriptDisabled,false); assert.equal(h.native.at(-1)[0],'detach');
});

test('a stalled script restore still attempts detach rather than holding the debugger forever', async () => {
  const h=await loginHarness().start();
  h.hook=async phase=>{if(phase==='restore')await new Promise(()=>{});};
  const result=await h.login(h.loginTab); assert.equal(result.status,'ok');
  await new Promise(resolve=>setTimeout(resolve,30));
  assert.equal(h.native.at(-1)[0],'detach'); assert.equal(presses(h).length,1);
});

test('a full valid mutation journal preserves saved-login denial without evicting its tombstone', async () => {
  const h=await loginHarness().start(); assert.equal((await h.login(h.loginTab)).status,'ok');
  const journal=h.storage.chrome_relay_latch;
  for(let i=journal.completed_order.length;i<10000;i++) {
    const id=h.wire('full-'+i); journal.completed_order.push(id);
    journal.completed[id]={status:'ok',reason:'',tab_id:h.loginTab,session_id:'agent-a',action:'chrome_type',length:1};
  }
  const restart=loginHarness({storage:h.storage,tabs:h.tabs,sessionStorage:h.sessionStorage,config:h.config});
  await restart.configure(); assert.equal((await restart.operation('chrome_attachment')).status,'ok');
  assert.equal((await restart.login(h.loginTab,'after-capacity')).data.reason,'login_attempted');
  assert.equal(restart.storage.chrome_relay_latch.completed_order.length,10000);
  assert.equal(restart.native.length,0);
});
