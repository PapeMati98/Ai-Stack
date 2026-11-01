'use strict';
// Installed official CLI owns authentication; this boundary admits only the
// account-plan route and declared tools. No headers, prompts or answers logged.
const fs=require('node:fs');
const policy=JSON.parse(fs.readFileSync(process.env.FLEET_ZCODE_BOUNDARY,'utf8'));
const originalFetch=globalThis.fetch;
const TRANSPORT_SCHEMA_VERSION=1,MAX_FRAME=65536,MAX_TRANSPORT_ERRORS=16;
if(!Array.isArray(policy.tools)||!Number.isInteger(policy.transport_schema_version)||policy.transport_schema_version!==TRANSPORT_SCHEMA_VERSION)throw new Error('Invalid Fleet ZCode boundary');
const allowedTools=new Set(policy.tools);
if(allowedTools.size!==policy.tools.length||!Number.isInteger(policy.max_requests)||policy.max_requests<1||policy.max_requests>1001||!Number.isInteger(policy.max_tool_calls)||policy.max_tool_calls<1||policy.max_tool_calls>1000||typeof policy.live!=='boolean')throw new Error('Invalid Fleet ZCode boundary');
const emit=event=>fs.appendFileSync(policy.witness,JSON.stringify(event)+'\n','utf8');
// Native stdio descendants stay hidden and receive no account auth context.
const cp=require('node:child_process'),spawn=cp.spawn;let children=0,toolCalls=0;
cp.spawn=function(command,args=[],options={}){
  if(!Array.isArray(policy.child_argv)||command!==policy.child_argv[0]||JSON.stringify(args)!==JSON.stringify(policy.child_argv.slice(1))||++children>4)throw new Error('Undeclared native subprocess');
  const safe={};for(const [key,value] of Object.entries(options.env??process.env))if(['systemroot','windir','comspec','path','pathext','temp','tmp','home','userprofile','homedrive','homepath','localappdata','appdata','programdata','programfiles','processor_architecture','number_of_processors'].includes(key.toLowerCase()))safe[key]=value;
  emit({kind:'broker_spawn',number:children,windowsHide:true});
  const child=spawn(command,args,{...options,env:safe,windowsHide:true,shell:false});
  const write=child.stdin.write.bind(child.stdin),end=child.stdin.end.bind(child.stdin);
  const pendingCalls=new Map(),completedCalls=new Map(),seenToolIds=new Set();
  let inputBuffer=Buffer.alloc(0),outputBuffer=Buffer.alloc(0),transportErrors=0;
  const idKey=id=>typeof id==='string'&&Buffer.byteLength(id,'utf8')<=128?'s:'+id:Number.isSafeInteger(id)?'n:'+id:null;
  const pendingSummary=()=>Array.from(pendingCalls.values()).sort((a,b)=>a.number-b.number).slice(0,1000).map(call=>({number:call.number,name:call.name}));
  function transportError(code,extra={}){
    if(transportErrors++>=MAX_TRANSPORT_ERRORS)return;
    const event={kind:'transport_error',code};
    if(Array.isArray(extra.pending_tool_calls))event.pending_tool_calls=extra.pending_tool_calls.slice(0,1000).map(call=>({number:call.number,name:call.name}));
    emit(event);
  }
  function decodeJson(frame,direction){
    let text;
    try{text=new TextDecoder('utf-8',{fatal:true}).decode(frame);}
    catch{transportError(direction==='request'?'invalid_tool_request_utf8':'invalid_broker_response_utf8');throw new Error('Invalid UTF-8 broker frame');}
    try{return JSON.parse(text);}
    catch{transportError(direction==='request'?'invalid_tool_request_frame':'invalid_broker_response');throw new Error('Invalid JSON broker frame');}
  }
  function requestFrame(frame){
    const request=decodeJson(frame,'request');
    if(!request||Array.isArray(request)||typeof request!=='object'||request.jsonrpc!=='2.0'||!['initialize','notifications/initialized','tools/list','tools/call','server/discover','ping'].includes(request.method)){
      transportError('invalid_tool_request_frame');throw new Error('Uninspected broker frame');
    }
    if(request.method==='tools/call'){
      const name=request.params?.name;
      if(!['fleet_read_file','fleet_list_files','fleet_chrome_attachment','fleet_chrome_open','fleet_chrome_read','fleet_chrome_wait','fleet_chrome_close'].includes(name)){
        transportError('invalid_tool_request_frame');throw new Error('Unknown broker tool');
      }
      const key=idKey(request.id);
      if(key===null){transportError('invalid_tool_call_id');throw new Error('Invalid broker call id');}
      toolCalls++;const number=toolCalls;emit({kind:'tool_call',name,number});
      if(seenToolIds.has(key)){transportError('tool_call_id_collision');throw new Error('Broker call id collision');}
      seenToolIds.add(key);
      if(toolCalls>policy.max_tool_calls){emit({kind:'tool_budget_exceeded'});throw new Error('Cumulative native tool budget');}
      pendingCalls.set(key,{name,number});
    }
  }
  function responseFrame(frame){
    const result=decodeJson(frame,'response');
    if(!result||Array.isArray(result)||typeof result!=='object'||result.jsonrpc!=='2.0'||!Object.hasOwn(result,'id')){
      transportError('invalid_broker_response');throw new Error('Invalid broker response');
    }
    const key=idKey(result.id);
    if(key===null){transportError('invalid_broker_response');throw new Error('Invalid broker response id');}
    const hasResult=Object.hasOwn(result,'result'),hasError=Object.hasOwn(result,'error');
    if(hasResult===hasError||('method' in result)){
      if(pendingCalls.has(key)){transportError('invalid_broker_response');throw new Error('Invalid broker tool result');}
      return;
    }
    const call=pendingCalls.get(key);
    if(call){
      if(hasError&&(!result.error||Array.isArray(result.error)||typeof result.error!=='object'||!Number.isInteger(result.error.code)||typeof result.error.message!=='string'||Buffer.byteLength(result.error.message,'utf8')>1024)){
        transportError('invalid_broker_response');throw new Error('Invalid broker error response');
      }
      emit({kind:'tool_result',number:call.number,name:call.name,refused:Boolean(hasError||result.result?.isError)});
      pendingCalls.delete(key);completedCalls.set(key,call);return;
    }
    if(completedCalls.has(key)){transportError('duplicate_tool_result');return;}
    if(seenToolIds.has(key)){transportError('tool_result_without_call');return;}
    // initialize/tools/list responses are outside the scoped file-call evidence.
  }
  function appendFrames(buffer,chunk,limit,handler,direction){
    const bytes=Buffer.from(chunk);
    const combined=Buffer.concat([buffer,bytes]);let start=0,at;
    while((at=combined.indexOf(0x0a,start))>=0){
      if(at-start+1>limit){transportError('broker_frame_too_large');throw new Error('Broker frame bound');}
      handler(combined.subarray(start,at));start=at+1;
    }
    const leftover=Buffer.from(combined.subarray(start));
    if(leftover.length>limit){transportError('broker_frame_too_large');throw new Error('Broker frame bound');}
    return leftover;
  }
  if(child.stdout){
    child.stdout.on('data',chunk=>{try{outputBuffer=appendFrames(outputBuffer,chunk,MAX_FRAME,responseFrame,'response');}catch{child.kill?.();}});
    child.stdout.on('end',()=>{if(outputBuffer.length){outputBuffer=Buffer.alloc(0);transportError('broker_closed_with_partial_frame');}});
    child.stdout.on('error',()=>{transportError('broker_stdout_error');child.kill?.();});
  }
  child.on?.('error',()=>transportError('broker_spawn_error'));
  child.on?.('close',(code,signal)=>{
    const boundedCode=Number.isInteger(code)&&code>=0&&code<=255?code:null;
    const boundedSignal=typeof signal==='string'&&/^[A-Z0-9_]{1,32}$/.test(signal)?signal:null;
    emit({kind:'broker_exit',code:boundedCode,signal:boundedSignal});
    if(inputBuffer.length){inputBuffer=Buffer.alloc(0);transportError('broker_closed_with_partial_frame');}
    if(outputBuffer.length){outputBuffer=Buffer.alloc(0);transportError('broker_closed_with_partial_frame');}
    if(pendingCalls.size)transportError('broker_closed_with_pending_calls',{pending_tool_calls:pendingSummary()});
  });
  child.stdin.write=function(chunk,...rest){
    if(typeof chunk!=='string'&&!(chunk instanceof Uint8Array)){transportError('invalid_tool_request_frame');throw new Error('Uninspected broker request');}
    const bytes=typeof chunk==='string'?Buffer.from(chunk,typeof rest[0]==='string'?rest[0]:'utf8'):Buffer.from(chunk);
    inputBuffer=appendFrames(inputBuffer,bytes,MAX_FRAME,requestFrame,'request');
    return write(chunk,...rest);
  };
  child.stdin.end=function(...args){
    let endArgs=args;
    if(typeof args[0]==='string'||args[0] instanceof Uint8Array){
      const encoding=typeof args[1]==='string'?args[1]:undefined;
      child.stdin.write(args[0],...(encoding?[encoding]:[]));
      endArgs=args.slice(encoding?2:1).filter(arg=>typeof arg!=='string');
    }
    if(inputBuffer.length){transportError('invalid_tool_request_frame');throw new Error('Incomplete broker request frame');}
    return end(...endArgs);
  };
  return child;
};
for(const name of ['exec','execSync','execFile','execFileSync','spawnSync','fork'])cp[name]=()=>{throw new Error('Undeclared native command');};
require('node:module').syncBuiltinESMExports();
if(process.env.FLEET_ZCODE_PROMPT_FILE){
  const at=process.argv.indexOf('__FLEET_PRIVATE_PROMPT__');
  if(at<0||process.argv.lastIndexOf('__FLEET_PRIVATE_PROMPT__')!==at)throw new Error('Missing private prompt binding');
  const bytes=fs.readFileSync(process.env.FLEET_ZCODE_PROMPT_FILE);
  if(bytes.length>131072)throw new Error('Native prompt bound exceeded');
  process.argv[at]=bytes.toString('utf8');
}
let calls=0;
globalThis.fetch=async function(input,options={}){
  const url=new URL(typeof input==='string'?input:input.url??String(input));
  const inference=url.hostname==='api.z.ai'&&url.pathname==='/api/anthropic/v1/messages';
  const control=(url.hostname==='zcode.z.ai'&&url.pathname==='/api/v1/agent/configs')||(url.hostname==='api.z.ai'&&url.pathname==='/api/paas/c1f3a7e2/v2/client');
  if(url.protocol!=='https:'||url.port||url.username||url.password||(!inference&&!control))throw new Error('Native endpoint refused');
  if(!inference)return originalFetch(input,{...options,redirect:'error'});
  let text;
  if(typeof options.body==='string')text=options.body;
  else if(options.body instanceof Uint8Array)text=Buffer.from(options.body).toString('utf8');
  else if(typeof input?.clone==='function')text=await input.clone().text();
  if(typeof text!=='string'||Buffer.byteLength(text)>2097152)throw new Error('Uninspected native inference');
  let body;try{body=JSON.parse(text);}catch{throw new Error('Uninspected native inference');}
  if(typeof body.model!=='string'||body.model.toLowerCase()!=='glm-5.3')throw new Error('Native model refused');
  if((body.tools!==undefined&&!Array.isArray(body.tools))||Object.hasOwn(body,'functions')||Object.hasOwn(body,'function_call'))throw new Error('Ambiguous native tools');
  const names=(body.tools??[]).map(t=>t?.name);
  if(names.some(name=>typeof name!=='string'||!allowedTools.has(name))||new Set(names).size!==names.length)throw new Error('Undeclared native tool');
  if(allowedTools.size!==names.length)throw new Error('Incomplete native tool projection');
  if(body.tool_choice!==undefined&&body.tool_choice!=='none'&&!(body.tool_choice?.type==='none'&&Object.keys(body.tool_choice).length===1)&&!(names.length&&body.tool_choice?.type==='auto'&&Object.keys(body.tool_choice).length===1))throw new Error('Native tool choice refused');
  calls++;emit({kind:'model_request',model:body.model,tools:names,number:calls});
  if(!policy.live)throw new Error('Native route disabled by boundary');
  if(calls>policy.max_requests){emit({kind:'request_budget_exceeded',number:calls,max_requests:policy.max_requests});throw new Error('Native model request budget exceeded');}
  const response=await originalFetch(input,{...options,redirect:'error'});
  emit({kind:'provider_status',status:response.status,request:calls});
  // Resolve witness before returning response. Native caller receives the same
  // bounded bytes, so process exit cannot outrun an asynchronous clone reader.
  const chunks=[];let size=0;
  for await(const chunk of response.body){size+=chunk.length;if(size>2097152)throw new Error('Native response bound exceeded');chunks.push(Buffer.from(chunk));}
  const bytes=Buffer.concat(chunks), models=new Set();
  for(let line of bytes.toString('utf8').split(/\r?\n/)){
    if(line.startsWith('data:'))line=line.slice(5).trim();
    try{const event=JSON.parse(line),model=event.model??event.message?.model;
      if(model)models.add(model);
    }catch{}
  }
  for(const model of models)emit({kind:'served_model',model,request:calls});
  if(response.ok&&(models.size!==1||[...models][0].toLowerCase()!=='glm-5.3'))throw new Error('Missing or changed native served model');
  return new Response(bytes,{status:response.status,statusText:response.statusText,headers:response.headers});
};
emit({kind:'boundary_ready',live:policy.live,transport_schema_version:TRANSPORT_SCHEMA_VERSION});
