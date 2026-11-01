'use strict';
// Owner-approved agent-browser profile launch; scope comes from the owner setup file.
// Never grants permissions, never simulates attach, never reads credentials.
const fs=require('node:fs'), path=require('node:path'), {isDeepStrictEqual}=require('node:util');
const setup=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const root=path.dirname(process.argv[2]);
const {chromium}=require(setup.playwright_root);
if(setup.required_debugger_consent!==true)throw Error('owner_required_debugger_consent_missing');
const EXPECTED=JSON.stringify([...setup.expected_origins].sort());
const BUILD=setup.build||'owner-start-1';
let context;
async function main(){
 context=await chromium.launchPersistentContext(setup.profile_path,{executablePath:setup.browser_exe,headless:false,viewport:null,args:['--disable-extensions-except='+setup.extension_path,'--load-extension='+setup.extension_path],timeout:25000});
 const worker=context.serviceWorkers().find(w=>w.url().startsWith('chrome-extension://'))||await context.waitForEvent('serviceworker',{timeout:25000});
 const id=worker.url().split('/')[2];
 if(id!==setup.expected_extension_id){
  fs.writeFileSync(path.join(root,'owner-browser-error.json'),JSON.stringify({status:'FAILED',error_type:'identity'}));
  await context.close().catch(()=>{});
  context=null;
  process.exitCode=1;
  return;
 }
 if(!/^[a-p]{32}$/.test(id))throw Error('identity');
 fs.writeFileSync(path.join(root,'owner-extension-ready.json'),JSON.stringify({extension_id:id,node_pid:process.pid,build:BUILD}));
 const deadline=Date.now()+60000;
 while(!fs.existsSync(setup.extension_config_path)&&Date.now()<deadline)await new Promise(r=>setTimeout(r,100));
 const config=JSON.parse(fs.readFileSync(setup.extension_config_path,'utf8'));
 if(config.synthetic!==false||config.profile_id!==setup.profile_id||JSON.stringify([...config.allowed_origins].sort())!==EXPECTED||!isDeepStrictEqual(config.saved_login,setup.saved_login))throw Error('scope');
 await worker.evaluate(async value=>{await chrome.storage.local.set({chrome_relay_config:value});},config);
 const page=context.pages()[0]||await context.newPage();
 await page.goto(setup.start_url,{waitUntil:'domcontentloaded',timeout:25000}).catch(()=>{});
 fs.writeFileSync(path.join(root,'owner-browser-ready.json'),JSON.stringify({status:'OWNER_ATTACH_REQUIRED',build:BUILD,origins:setup.expected_origins.length,automatic_optional_host_grants:false,automatic_attach:false,credentials_read:false}));
 await new Promise(r=>context.once('close',r));
 fs.writeFileSync(path.join(root,'owner-browser-stopped.json'),JSON.stringify({at:new Date().toISOString()}));
}
main().catch(async error=>{fs.writeFileSync(path.join(root,'owner-browser-error.json'),JSON.stringify({status:'FAILED',error_type:error.constructor.name}));if(context)await context.close().catch(()=>{});process.exitCode=1;});
