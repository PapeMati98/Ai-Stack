'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs');
const {EventEmitter}=require('node:events'),{PassThrough}=require('node:stream');
const file=process.argv[2];let policy,entries=[],forwarded=0,served='GLM-5.3',spawned;
const cp=require('node:child_process');const originals={};for(const name of ['spawn','exec','execSync','execFile','execFileSync','spawnSync','fork'])originals[name]=cp[name];
const read=fs.readFileSync;
fs.readFileSync=(path,...args)=>path==='synthetic'?JSON.stringify(policy):read(path,...args);fs.appendFileSync=(_,value)=>entries.push(JSON.parse(value));
const url='https://api.z.ai/api/anthropic/v1/messages';
function setup(extra={}){
  policy={tools:[],live:true,transport_schema_version:1,max_requests:1,max_tool_calls:2,witness:'synthetic',...extra};entries=[];forwarded=0;served='GLM-5.3';
  process.env.FLEET_ZCODE_BOUNDARY='synthetic';delete process.env.FLEET_ZCODE_PROMPT_FILE;
  Object.assign(cp,originals);
  cp.spawn=(command,args,options)=>{
    const child=new EventEmitter();child.stdout=new PassThrough();child.killed=false;child.kill=()=>{child.killed=true;return true;};
    child.stdin={write:chunk=>{spawned.frames.push(Buffer.from(chunk));return true;},end:()=>{}};
    spawned={command,args,options,frames:[],child};return child;
  };
  globalThis.fetch=async()=>{forwarded++;return new Response('data: '+JSON.stringify({message:{model:served}})+'\n\n',{status:200});};
  delete require.cache[require.resolve(file)];require(file);
}
const data=extra=>JSON.stringify({model:'GLM-5.3',tools:[],messages:[{role:'user',content:'PRIVATE_SYNTHETIC'}],...extra});
const toolRequest=(id,name='fleet_read_file')=>JSON.stringify({jsonrpc:'2.0',id,method:'tools/call',params:{name,arguments:{path:'fixture.txt'}}})+'\n';
const toolResponse=(id,body)=>Buffer.from(JSON.stringify({jsonrpc:'2.0',id,...body})+'\n','utf8');
async function main(){
  for(const extra of [{model:'GLM-5.3-Flash'},{tools:{}},{tools:[{name:'Bash'}]},{functions:[]},{function_call:'none'},{tool_choice:'auto'},{tool_choice:{type:'tool',name:'Bash'}}]){
    setup();await assert.rejects(fetch(url,{method:'POST',body:data(extra)}));assert.equal(forwarded,0);
  }
  for(const body of [undefined,'bad','{}',new Uint8Array(Buffer.from('{bad'))]){
    setup();await assert.rejects(fetch(url,{method:'POST',body}));assert.equal(forwarded,0);
  }
  setup();await assert.rejects(fetch(url,{method:'POST',body:'x'.repeat(2097153)}));assert.equal(forwarded,0);
  for(const endpoint of ['http://api.z.ai/api/anthropic/v1/messages','https://example.org/messages','https://api.z.ai/api/v4/chat/completions','https://secret@api.z.ai/api/anthropic/v1/messages']){
    setup();await assert.rejects(fetch(endpoint,{body:data()}));assert.equal(forwarded,0);
  }
  setup({live:false});await assert.rejects(fetch(url,{body:data()}));assert.equal(forwarded,0);
  setup();await fetch(url,{body:new Uint8Array(Buffer.from(data()))});assert.equal(forwarded,1);assert(entries.some(e=>e.kind==='served_model'));
  await assert.rejects(fetch(url,{body:data()}));assert.equal(forwarded,1);
  assert(!JSON.stringify(entries).includes('PRIVATE_SYNTHETIC'));
  setup();served='GLM-5.3-Flash';await assert.rejects(fetch(url,{body:data()}));
  setup();await fetch(new Request(url,{method:'POST',body:data()}));assert.equal(forwarded,1);
  setup({tools:['mcp__fleet__fleet_read_file']});await assert.rejects(fetch(url,{body:data()}));assert.equal(forwarded,0);
  await fetch(url,{body:data({tools:[{name:'mcp__fleet__fleet_read_file'}],tool_choice:{type:'auto'}})});assert.equal(forwarded,1);
  setup({child_argv:['python',['unused']]});await assert.rejects(async()=>cp.fork('other'));await assert.rejects(async()=>cp.exec('other'));
  setup({child_argv:['python','broker.py']});const child=cp.spawn('python',['broker.py'],{env:{PATH:'synthetic',ZCODE_CREDENTIAL_SECRET:'PRIVATE_SYNTHETIC',OPENAI_API_KEY:'PRIVATE_SYNTHETIC'},shell:true});
  assert.equal(spawned.options.windowsHide,true);assert.equal(spawned.options.shell,false);assert.deepEqual(spawned.options.env,{PATH:'synthetic'});
  child.stdin.write(toolRequest('one'));
  child.stdin.write(toolRequest('two'));
  assert.throws(()=>child.stdin.write(toolRequest('three')));
  assert.equal(spawned.frames.length,2);assert(entries.some(e=>e.kind==='tool_budget_exceeded'));
  for(const frame of ['[]\n','null\n','{"method":"unknown"}\n','{bad}\n']){
    setup({child_argv:['python','broker.py']});const c=cp.spawn('python',['broker.py']);assert.throws(()=>c.stdin.write(frame));assert.equal(spawned.frames.length,0);
  }
  setup({child_argv:['python','broker.py']});assert.throws(()=>cp.spawn('python',['other.py']));
  const incomplete=cp.spawn('python',['broker.py']);incomplete.stdin.write('{');assert.throws(()=>incomplete.stdin.end());
  setup({child_argv:['python','broker.py']});
  for(let i=0;i<4;i++){const c=cp.spawn('python',['broker.py']);c.stdout.end();c.emit('close',0,null);}
  assert.throws(()=>cp.spawn('python',['broker.py']));
  assert.equal(entries.filter(e=>e.kind==='broker_spawn').length,4);
  setup({child_argv:['python','broker.py']});const tooLarge=cp.spawn('python',['broker.py']);
  assert.throws(()=>tooLarge.stdin.write('x'.repeat(65536)+'\n'));
  assert.equal(spawned.frames.length,0);
  assert(entries.some(e=>e.kind==='transport_error'&&e.code==='broker_frame_too_large'));
  setup({child_argv:['python','broker.py'],tools:['mcp__fleet__fleet_read_file'],max_tool_calls:3});
  const emptyHelper=cp.spawn('python',['broker.py']);
  emptyHelper.stdin.write(JSON.stringify({jsonrpc:'2.0',id:'initialize',method:'initialize',params:{protocolVersion:'2025-03-26'}})+'\n');
  emptyHelper.stdout.write(JSON.stringify({jsonrpc:'2.0',id:'initialize',result:{protocolVersion:'2025-03-26',capabilities:{}}})+'\n');
  emptyHelper.stdin.write(JSON.stringify({jsonrpc:'2.0',method:'notifications/initialized'})+'\n');
  emptyHelper.stdin.end();emptyHelper.stdout.end();emptyHelper.emit('close',null,'SIGTERM');
  assert(entries.some(e=>e.kind==='broker_exit'&&e.code===null&&e.signal==='SIGTERM'));
  assert(!entries.some(e=>e.kind==='transport_error'));

  setup({child_argv:['python','broker.py'],tools:['mcp__fleet__fleet_read_file'],max_tool_calls:3});
  const nonzeroHelper=cp.spawn('python',['broker.py']);
  nonzeroHelper.stdin.write(JSON.stringify({jsonrpc:'2.0',id:'initialize',method:'initialize',params:{protocolVersion:'2025-03-26'}})+'\n');
  nonzeroHelper.stdout.write(JSON.stringify({jsonrpc:'2.0',id:'initialize',result:{protocolVersion:'2025-03-26',capabilities:{}}})+'\n');
  nonzeroHelper.stdin.write(JSON.stringify({jsonrpc:'2.0',method:'notifications/initialized'})+'\n');
  nonzeroHelper.stdin.end();nonzeroHelper.stdout.end();nonzeroHelper.emit('close',23,null);
  assert(entries.some(e=>e.kind==='broker_exit'&&e.code===23&&e.signal===null));
  assert(!entries.some(e=>e.kind==='transport_error'));
  setup({max_requests:33});
  for(let i=0;i<33;i++)await fetch(url,{body:data()});
  assert.equal(forwarded,33);assert(!entries.some(e=>e.kind==='request_budget_exceeded'));
  await assert.rejects(fetch(url,{body:data()}));assert.equal(forwarded,33);
  assert(entries.some(e=>e.kind==='request_budget_exceeded'&&e.number===34&&e.max_requests===33));
  setup({child_argv:['python','broker.py'],max_tool_calls:32});const thirtyTwo=cp.spawn('python',['broker.py']);
  for(let i=0;i<32;i++)thirtyTwo.stdin.write(toolRequest('cap-'+i));
  assert.throws(()=>thirtyTwo.stdin.write(toolRequest('cap-32')));assert.equal(spawned.frames.length,32);
  assert(entries.some(e=>e.kind==='tool_budget_exceeded'));

  setup({child_argv:['python','broker.py'],tools:['mcp__fleet__fleet_read_file'],max_tool_calls:3});
  const unicodeChild=cp.spawn('python',['broker.py']);unicodeChild.stdin.write(toolRequest('unicode-call'));
  const unicodeText='ASCII Polski Łódź → 😀';
  const unicodeFrame=toolResponse('unicode-call',{result:{content:[{type:'text',text:unicodeText}]}});
  const arrow=unicodeFrame.indexOf(Buffer.from('→'));
  unicodeChild.stdout.write(unicodeFrame.subarray(0,arrow+1));unicodeChild.stdout.write(unicodeFrame.subarray(arrow+1));
  unicodeChild.stdout.end();unicodeChild.emit('close',null,'SIGTERM');
  assert(entries.some(e=>e.kind==='tool_result'&&e.number===1&&e.name==='fleet_read_file'&&!e.refused));
  assert(entries.some(e=>e.kind==='broker_exit'&&e.code===null&&e.signal==='SIGTERM'));
  assert(!entries.some(e=>e.kind==='transport_error'));
  assert(!JSON.stringify(entries).includes(unicodeText));

  setup({child_argv:['python','broker.py'],tools:['mcp__fleet__fleet_read_file'],max_tool_calls:3});
  const pendingChild=cp.spawn('python',['broker.py']);pendingChild.stdin.write(toolRequest('read-1'));
  pendingChild.stdout.write(toolResponse('read-1',{result:{content:[{type:'text',text:'SYNTHETIC'}]}}));
  pendingChild.stdin.write(toolRequest('private-pending-id','fleet_list_files'));
  pendingChild.stdout.end();pendingChild.emit('close',null,'SIGTERM');
  const closed=entries.find(e=>e.kind==='transport_error'&&e.code==='broker_closed_with_pending_calls');
  assert.deepEqual(closed.pending_tool_calls,[{number:2,name:'fleet_list_files'}]);
  assert(!JSON.stringify(entries).includes('private-pending-id'));

  setup({child_argv:['python','broker.py'],tools:['mcp__fleet__fleet_read_file'],max_tool_calls:3});
  const partialOutputChild=cp.spawn('python',['broker.py']);partialOutputChild.stdin.write(toolRequest('partial-output-read'));
  partialOutputChild.stdout.write(toolResponse('partial-output-read',{result:{content:[{type:'text',text:'SYNTHETIC'}]}}));
  partialOutputChild.stdout.write(Buffer.from('{"jsonrpc":"2.0"','utf8'));
  partialOutputChild.stdout.end();partialOutputChild.emit('close',null,'SIGTERM');
  assert(entries.some(e=>e.kind==='tool_result'&&e.number===1&&!e.refused));
  assert(entries.some(e=>e.kind==='transport_error'&&e.code==='broker_closed_with_partial_frame'));

  setup({child_argv:['python','broker.py'],tools:['mcp__fleet__fleet_read_file'],max_tool_calls:3});
  const partialInputChild=cp.spawn('python',['broker.py']);partialInputChild.stdin.write(toolRequest('partial-input-read'));
  partialInputChild.stdout.write(toolResponse('partial-input-read',{result:{content:[{type:'text',text:'SYNTHETIC'}]}}));
  partialInputChild.stdin.write('{"jsonrpc":"2.0"');
  partialInputChild.stdout.end();partialInputChild.emit('close',null,'SIGTERM');
  assert(entries.some(e=>e.kind==='tool_result'&&e.number===1&&!e.refused));
  assert(entries.some(e=>e.kind==='transport_error'&&e.code==='broker_closed_with_partial_frame'));

  setup({child_argv:['python','broker.py'],tools:['mcp__fleet__fleet_read_file'],max_tool_calls:3});
  const duplicateChild=cp.spawn('python',['broker.py']);duplicateChild.stdin.write(toolRequest('duplicate-id'));
  const goodResult=toolResponse('duplicate-id',{result:{content:[{type:'text',text:'SYNTHETIC'}]}});
  duplicateChild.stdout.write(goodResult);duplicateChild.stdout.write(goodResult);
  duplicateChild.stdin.write(toolRequest('missing-result','fleet_list_files'));
  duplicateChild.stdout.end();duplicateChild.emit('close',0,null);
  assert(entries.some(e=>e.kind==='transport_error'&&e.code==='duplicate_tool_result'));
  assert(entries.some(e=>e.kind==='transport_error'&&e.code==='broker_closed_with_pending_calls'&&e.pending_tool_calls[0].number===2));

  setup({child_argv:['python','broker.py'],tools:['mcp__fleet__fleet_read_file'],max_tool_calls:3});
  const invalidChild=cp.spawn('python',['broker.py']);invalidChild.stdin.write(toolRequest('bad-utf8'));
  const prefix=Buffer.from('{"jsonrpc":"2.0","id":"bad-utf8","result":{"content":[{"type":"text","text":"');
  const suffix=Buffer.from('"}]}}\n');invalidChild.stdout.write(Buffer.concat([prefix,Buffer.from([0xff]),suffix]));
  invalidChild.stdout.end();invalidChild.emit('close',0,null);
  assert(entries.some(e=>e.kind==='transport_error'&&e.code==='invalid_broker_response_utf8'));

  setup({child_argv:['python','broker.py'],tools:['mcp__fleet__fleet_read_file'],max_tool_calls:3});
  const refusedChild=cp.spawn('python',['broker.py']);refusedChild.stdin.write(toolRequest('refused-id'));
  refusedChild.stdout.write(toolResponse('refused-id',{error:{code:-32000,message:'Fleet read request refused'}}));
  refusedChild.stdout.end();refusedChild.emit('close',0,null);
  assert(entries.some(e=>e.kind==='tool_result'&&e.refused));
  assert(!entries.some(e=>e.kind==='transport_error'));
  console.log('PASS: native request/tool caps, split UTF-8, paired outcomes and transport failures; offline');
}
main().catch(e=>{console.error(e.message);process.exitCode=1;});
