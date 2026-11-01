const test = require('node:test');
const assert = require('node:assert/strict');
const {codexNativeSubmit, codexOfficeCycle, codexOfficeReconcile} = require('./codex_app_transport.js');
const threadId = '01a10b42-94e2-78e1-a908-c6549d6b9668';
const prior = '01a10b42-975d-7ac2-ad22-3dfa672534bf';
const next = '01a10b49-9772-75d1-ba44-08dd78e1adbf';
const office = 'codex-ailab-01a0ac55-48ac-7742-9b18-8fa624150d4b';
const operator = {relayAgent:office,targets:{[threadId]:{threadId,hostId:'local',authorized:true,
 projectSlug:'agent-stack',workspace:'C:/synthetic/office'}}};
const intent = {target:threadId,driver:'codex-desktop-relay',relayAgent:office,
 projectSlug:'agent-stack',workspace:'C:/synthetic/office',dispatchId:'dispatch_1',prompt:'Read approved Office task'};

test('one existing Office queue cycle reaches the real app contract and correlated DM ACK', async()=>{
 const f=fixture(); const calls=[]; const dispatch='a'.repeat(32), trigger='b'.repeat(32);
 const registry={...operator,targets:{[threadId]:{...operator.targets[threadId],
   bindingId:'codex-canary',officeAgent:`codex-ailab-${threadId}`}}};
 const remote=async(op,args)=>{
  calls.push({op,args});
  if(op==='wake-relay-watch')return{status:'ok',code:'wake_relay_watch',result:'pending',binding_ids:['codex-canary']};
  if(op==='wake-relay-pending')return{status:'ok',code:'wake_relay_pending',dispatches:[{
   binding_id:'codex-canary',status:'pending',dispatch_id:dispatch,trigger_ref:trigger,
   target_local_id:threadId,text:'Read the single approved Office task',created_at:'2026-10-05'}]};
  if(op==='wake-relay-claim')return{status:'ok',code:'wake_relay_claimed',dispatch:{dispatch_id:dispatch,status:'claimed'}};
  if(op==='wake-relay-native-begin')return{status:'ok',code:'wake_native_submission_fenced',dispatch:{dispatch_id:dispatch,status:'delivery_unknown'}};
  if(op==='wake-relay-ack')return{status:'ok',code:'wake_relay_ack',dispatch_id:dispatch,
   target:`codex-ailab-${threadId}`,trigger_ref:trigger,ack_id:'c'.repeat(32),acknowledged:true};
  if(op==='wake-relay-receipt')return{status:'ok',code:'wake_relay_receipt'};
  if(op==='wake-relay-observe')return{status:'ok',code:'wake_relay_observed'};
  throw Error(op);
 };
 const r=await codexOfficeCycle(f.tools,remote,registry,threadId);
 assert.equal(r.status,'DELIVERED');assert.equal(r.taskReceived,true);
 assert.equal(f.counts().sends,1);
 const receipt=calls.find(x=>x.op==='wake-relay-receipt').args;
 assert.equal(receipt.turn_id,next);assert.equal(receipt.message_id,undefined);
 assert.equal(calls.filter(x=>x.op==='wake-relay-native-begin').length,1);
});
const result = x => ({content:[{type:'text',text:JSON.stringify(x)}]});
test('late correlated ACK settles the retained native outcome without another send', async()=>{
 const registry={...operator,targets:{[threadId]:{...operator.targets[threadId],
   bindingId:'codex-canary',officeAgent:`codex-ailab-${threadId}`}}};
 const previous={status:'DELIVERY_UNKNOWN',sendCalls:1,threadId,dispatchId:'a'.repeat(32),
  bindingId:'codex-canary',officeTrigger:'b'.repeat(32),beforeTurnId:prior,
  actualToolPayload:{threadId},observedNewTurn:{turnId:next,status:'inProgress'}};
 const calls=[];
 const tools={mcp__codex_app__wait_threads:async()=>snapshot('idle',next,'completed'),
  mcp__codex_app__send_message_to_thread:async()=>{throw Error('No resend permitted');}};
 const remote=async(op,args)=>{
  calls.push(op);
  if(op==='wake-relay-ack')return{status:'ok',code:'wake_relay_ack',dispatch_id:previous.dispatchId,
   target:registry.targets[threadId].officeAgent,trigger_ref:previous.officeTrigger,
   acknowledged:true,ack_id:'c'.repeat(32)};
  if(op==='wake-relay-receipt')return{status:'ok',code:'wake_relay_receipt'};
  if(op==='wake-relay-observe'){
   assert.equal(args.stage,'target_finished');return{status:'ok',code:'wake_relay_observed'};
  }
  throw Error('Unexpected operation '+op);
 };
 const settled=await codexOfficeReconcile(tools,remote,registry,previous);
 assert.equal(settled.status,'DELIVERED');assert.equal(settled.taskReceived,true);
 assert.deepEqual(calls,['wake-relay-ack','wake-relay-receipt','wake-relay-observe']);
 const wrong=await codexOfficeReconcile(tools,remote,registry,{...previous,bindingId:'other'});
 assert.equal(wrong.status,'REFUSED');assert.equal(calls.length,3);
 const changed={...tools,mcp__codex_app__wait_threads:async()=>snapshot('idle',prior,'completed')};
 const stale=await codexOfficeReconcile(changed,remote,registry,previous);
 assert.equal(stale.status,'DELIVERY_UNKNOWN');assert.equal(calls.length,3);
});
function snapshot(activity,turnId=prior,status='completed') {
 return result({polls:[{thread:{id:threadId,hostId:'local',status:{type:activity}},
  latestTurn:{id:turnId,status,startedAt:1791190734}}]});
}
function fixture({busy=false,throwSend=false,wrong=false,throwAfter=false,deny=false,exhausted=false}={}) {
 let sends=0,observes=0,begins=0;
 const tools={mcp__codex_app__get_usage_limits:async()=>result({ordinaryUsageAllowed:!exhausted}),
  mcp__codex_app__wait_threads:async()=>{
  observes++;if(observes>1&&throwAfter)throw Error('unknown');
  return observes===1?snapshot(busy?'active':'idle'):snapshot('active',next,'inProgress');},
  mcp__codex_app__send_message_to_thread:async()=>{sends++;if(throwSend)throw Error('unknown');
   return result({threadId:wrong?prior:threadId});}};
 const begin=async i=>{begins++;return {allowed:!deny,dispatchId:i.dispatchId,
  driver:i.driver,target:i.target,relayAgent:i.relayAgent};};
 return {tools,begin,counts:()=>({sends,begins})};
}
test('actual thread-only receipt stays unknown; fresh turn is separate evidence',async()=>{
 const f=fixture();const r=await codexNativeSubmit(f.tools,intent,operator,f.begin);
 assert.equal(r.status,'DELIVERY_UNKNOWN');assert.equal(r.taskReceived,false);
 assert.deepEqual(r.actualToolPayload,{threadId});assert.equal(r.observedNewTurn.turnId,next);
 assert.deepEqual(f.counts(),{sends:1,begins:1});assert.equal(r.message_id,undefined);
});
test('busy defers before native fence/send',async()=>{
 const f=fixture({busy:true});const r=await codexNativeSubmit(f.tools,intent,operator,f.begin);
 assert.equal(r.status,'DEFERRED');assert.deepEqual(f.counts(),{sends:0,begins:0});
});
test('STOP or revoked claim denies submission',async()=>{
 const f=fixture({deny:true});const r=await codexNativeSubmit(f.tools,intent,operator,f.begin);
 assert.equal(r.status,'REFUSED');assert.deepEqual(f.counts(),{sends:0,begins:1});
});
test('confirmed quota exhaustion defers without fence/send',async()=>{
 const f=fixture({exhausted:true});const r=await codexNativeSubmit(f.tools,intent,operator,f.begin);
 assert.equal(r.status,'DEFERRED');assert.deepEqual(f.counts(),{sends:0,begins:0});
});
for(const option of ['throwSend','wrong','throwAfter']) test(option+' never retries',async()=>{
 const f=fixture({[option]:true});const r=await codexNativeSubmit(f.tools,intent,operator,f.begin);
 assert.equal(r.status,'DELIVERY_UNKNOWN');assert.equal(f.counts().sends,1);
});
for(const patch of [{target:'Office title'},{driver:'claude-desktop-relay'},
 {relayAgent:'codex-ailab-process-1'},{projectSlug:'other-project'},{workspace:'C:/other'}]) {
 test('untrusted intent '+JSON.stringify(patch),async()=>{
  const f=fixture();const r=await codexNativeSubmit(f.tools,{...intent,...patch},operator,f.begin);
  assert.equal(r.status,'REFUSED');assert.deepEqual(f.counts(),{sends:0,begins:0});
 });
}
