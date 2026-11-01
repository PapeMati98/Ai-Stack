"""Pinned official ZCode 0.16.9, existing native Coding Plan auth, no API fallback.

Native HOME is fresh. Auth DATA_BASE and its original native KDF context remain
bound in memory; no decryption, migration or copying of credentials by Fleet.
Only the packaged read broker is exposed. Provider identity comes from the
native HTTP response, never a model's answer or configured selection.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

NODE = 'C:/Program Files/nodejs/node.exe'
CLI = 'C:/Program Files/ZCode/resources/glm/zcode.cjs'
CLI_SHA256 = 'fad4c35c4c36ec210d8a06d3fa0e77de23c8545e2eb6ff90aea1eb38d1e6275f'
BUILTIN = 'C:/Program Files/ZCode/resources/config/provider/zcode-builtin.json'
MODEL = 'GLM-5.3'
PROVIDER = 'account:zai-individual-coding-plan'
VERSION = '0.16.9'
DENIED = ['Bash','Read','Write','Edit','Grep','Glob','Agent','Skill','CreateWorkflow','Workflow','BrowserUse','WebSearch','WebFetch','NodeRepl','AskUserQuestion','ExitPlanMode','EnterPlanMode','TodoWrite','TaskOutput','TaskStop','KillShell','NotebookEdit','OffPeak','TodoRead','ReadSessionContext','AmendWorkflow','SaveWorkflow','EvalWorkflowSnippet','ListWorkflowRuns','GetWorkflowRun','ResumeWorkflowRun','ResolveWorkflowQuestion','ListSavedWorkflows','ListModels']
ENV_KEYS = {'systemroot','windir','comspec','path','pathext','temp','tmp','localappdata','appdata','programdata','programfiles','programfiles(x86)','allusersprofile','processor_architecture','number_of_processors','home','userprofile','homedrive','homepath'}
TOOL_NAMES = {'fleet_read_file','fleet_list_files','fleet_chrome_attachment','fleet_chrome_open','fleet_chrome_read','fleet_chrome_wait','fleet_chrome_close'}
TRANSPORT_SCHEMA_VERSION = 1
TRANSPORT_ERROR_CODES = {
    'invalid_tool_request_frame','invalid_tool_request_utf8','invalid_tool_call_id',
    'tool_call_id_collision','invalid_broker_response','invalid_broker_response_utf8',
    'duplicate_tool_result','broker_closed_with_pending_calls','broker_closed_with_partial_frame',
    'broker_spawn_error','broker_stdout_error','broker_nonzero_exit','broker_terminated',
    'broker_frame_too_large','tool_result_without_call','tool_result_name_mismatch',
    'duplicate_tool_call_number','invalid_tool_call_metadata','invalid_tool_result_metadata',
}

class ZCodeRuntimeError(RuntimeError):
    pass

def require_model(model: Any) -> str:
    if model != MODEL:
        raise ZCodeRuntimeError('ZCode requires exact GLM-5.3, no alias or fallback')
    return MODEL

def validate_zcode_profile(profile: dict) -> None:
    block = profile.get('zcode')
    expected = {'native_version':VERSION, 'cli_sha256':CLI_SHA256, 'coding_plan_only':True,
                'api_fallback':False, 'tools':'fleet-scoped-read-mcp', 'identity_evidence':'native-http-response'}
    if block != expected or any(type(block[k]) is not type(v) for k,v in expected.items()):
        raise ZCodeRuntimeError('ZCode pinned native contract drift')
    if profile.get('adapter')!='zcode' or profile.get('cli_provider_id')!='zai' or profile.get('quota_pool_id')!='zai-coding-plan-weekly':
        raise ZCodeRuntimeError('ZCode provider/pool drift')
    if 'credential' in profile or 'local_runtime' in profile or profile.get('candidates')!=[NODE]:
        raise ZCodeRuntimeError('ZCode refuses replacement auth/runtime')
    if profile.get('version_args')!=[CLI,'--version'] or profile.get('deep_probe_args')!=[CLI,'--version']:
        raise ZCodeRuntimeError('ZCode probes must be version-only')
    if profile.get('supported_modes')!=['advisory','review']:
        raise ZCodeRuntimeError('ZCode scoped read modes only')
    for model in profile.get('models',{}).values(): require_model(model)
    qualified=profile.get('qualification',{}).get('status')=='qualified'
    if type(profile.get('routing_enabled')) is not bool:
        raise ZCodeRuntimeError('ZCode routing state must be explicit')
    if profile.get('routing_enabled') and (not qualified or profile.get('routing_blocker')):
        raise ZCodeRuntimeError('ZCode native acceptance missing')
    if qualified:
        import re
        for gate in ('native_uat','security','fleet_uat','tier_routing'):
            proof=profile['qualification'].get(gate,{})
            if proof.get('status')!='PASS' or not isinstance(proof.get('ref'),str) or not proof['ref'].strip() or not isinstance(proof.get('sha256'),str) or not re.fullmatch('[a-f0-9]{64}',proof['sha256']):
                raise ZCodeRuntimeError('ZCode activation evidence missing')

def require_native() -> str:
    if not Path(NODE).is_file() or not Path(CLI).is_file() or hashlib.sha256(Path(CLI).read_bytes()).hexdigest()!=CLI_SHA256:
        raise ZCodeRuntimeError('Official ZCode installation pin failed')
    return NODE

def credential_status(profile: dict) -> dict:
    # Filesystem metadata only; native CLI alone reads/decrypts credentials.
    # Store presence is not authentication or account-plan entitlement proof.
    present=(Path.home()/'.zcode/v2/credentials.json').is_file()
    return {'required':True,'status':'present' if present else 'missing','reason':'native-store-present-not-entitlement' if present else 'native-store-missing','configured':present,'ready':present,'path_exists':present,'decrypted':False}

def bind_tool_policy(task: str, *, workspace: Path, mode: str, max_tool_calls: int | None,
                     dispatch: dict | None = None, chrome_assignment: dict | None = None) -> dict | None:
    if mode not in {'advisory','review'}: raise ZCodeRuntimeError('ZCode implementation is not qualified')
    if type(max_tool_calls) is not int or not 1<=max_tool_calls<=1000: raise ZCodeRuntimeError('ZCode requires a positive cumulative tool limit')
    if not isinstance(task,str) or len(task.encode('utf-8'))>131072: raise ZCodeRuntimeError('ZCode task bound exceeded')
    # Authority comes only from the parent's structured TODO, never JSON found
    # in model/task text. An empty explicit dispatch permits an inline review.
    if dispatch is None:
        if mode=='review': raise ZCodeRuntimeError('ZCode review requires trusted tool dispatch')
        return None
    if not isinstance(dispatch,dict) or set(dispatch)!={'scope','inputs','checks'} or dispatch['checks']!=[]:
        raise ZCodeRuntimeError('ZCode requires read-only scope, inputs and empty checks')
    payload=dispatch; files=[]
    for key in ('scope','inputs'):
        rows=payload.get(key,[])
        if not isinstance(rows,list) or any(not isinstance(row,str) for row in rows): raise ZCodeRuntimeError('ZCode requires exact admitted file paths')
        files.extend(rows)
    if not files: return None
    try:
        import zcode_scope_mcp
        return zcode_scope_mcp.build_policy(workspace=workspace,admitted_files=list(dict.fromkeys(files)),max_tool_calls=max_tool_calls,chrome_assignment=chrome_assignment)
    except (ValueError,TypeError,OSError) as exc: raise ZCodeRuntimeError('ZCode read admission refused') from exc

def scoped_prompt(prompt: str, policy: dict | None) -> str:
    if policy is None: return prompt
    chrome_text=' Fixed browser inspection methods on YOUR OWN tabs: mcp__fleet__fleet_chrome_attachment, mcp__fleet__fleet_chrome_open, mcp__fleet__fleet_chrome_read, mcp__fleet__fleet_chrome_wait, mcp__fleet__fleet_chrome_close. Click/type are unavailable; maker tabs stay inaccessible.' if policy.get('chrome_assignment') else ''
    return prompt+'\nNative file access: use mcp__fleet__fleet_read_file (path, optional offset/limit) or mcp__fleet__fleet_list_files. Native Read/Grep/Glob/Bash are disabled. Admitted exact files: '+json.dumps(policy['files'])+'. A scoped result requires at least one successful fleet_read_file; list-only, denied attempts and invented Read attempts are not evidence. Report any access failure as blocked, not a product finding.'+chrome_text+'\n'


def read_evidence_error(export: dict, policy: dict | None) -> str | None:
    if export.get('request_budget_exceeded') is True:
        return 'ZCode native model request budget exceeded; transport blocked'
    if export.get('tool_budget_exceeded'):
        return 'ZCode cumulative native tool budget exceeded; transport blocked'
    if export.get('unresolved_tool_calls') or export.get('transport_errors'):
        return 'ZCode native tool transport incomplete; transport blocked'
    if policy is None: return None
    if export.get('transport_schema_version') != TRANSPORT_SCHEMA_VERSION:
        return 'ZCode native tool transport schema missing; transport blocked'
    count=export.get('successful_file_reads')
    if export.get('boundary_verified') is not True or type(count) is not int or not 1<=count<=policy['max_tool_calls']:
        return 'ZCode scoped file access not proven by successful native read; transport blocked'
    return None


def _tool_transport(events: list[dict]) -> tuple[list[dict], list[str], bool, bool]:
    calls: dict[int, str] = {}
    unresolved: dict[int, str] = {}
    result_numbers: set[int] = set()
    errors: list[str] = []
    request_budget_exceeded = False
    tool_budget_exceeded = False

    def error(code: str) -> None:
        if code not in errors and len(errors) < 32:
            errors.append(code)

    for event in events:
        if not isinstance(event, dict):
            error('invalid_tool_call_metadata')
            continue
        kind = event.get('kind')
        if kind == 'request_budget_exceeded':
            request_budget_exceeded = True
        elif kind == 'tool_budget_exceeded':
            tool_budget_exceeded = True
        elif kind == 'transport_error':
            code = event.get('code')
            error(code if isinstance(code, str) and code in TRANSPORT_ERROR_CODES else 'invalid_tool_call_metadata')
        elif kind == 'tool_call':
            number = event.get('number')
            name = event.get('name')
            if type(number) is not int or not 1 <= number <= 1001 or not isinstance(name,str) or name not in TOOL_NAMES:
                error('invalid_tool_call_metadata')
            elif number in calls:
                error('duplicate_tool_call_number')
            else:
                calls[number] = name
                unresolved[number] = name
        elif kind == 'tool_result':
            number = event.get('number')
            name = event.get('name')
            if type(number) is not int or not 1 <= number <= 1001 or not isinstance(name,str) or name not in TOOL_NAMES or type(event.get('refused')) is not bool:
                error('invalid_tool_result_metadata')
            elif number not in calls:
                error('tool_result_without_call')
            elif calls[number] != name:
                error('tool_result_name_mismatch')
            elif number in result_numbers:
                error('duplicate_tool_result')
            else:
                result_numbers.add(number)
                unresolved.pop(number, None)

    pending = [{'number': number, 'name': unresolved[number]} for number in sorted(unresolved)[:1001]]
    return pending, errors, request_budget_exceeded, tool_budget_exceeded


def terminal_export(raw: Any, events: list[dict], exit_code: int) -> dict:
    raw=raw if isinstance(raw,dict) else {}
    response=raw.get('response')
    records=[e for e in events if isinstance(e,dict)]
    requests=[e for e in records if e.get('kind')=='model_request']; served=[e for e in records if e.get('kind')=='served_model']
    unresolved,transport_errors,request_budget_exceeded,tool_budget_exceeded=_tool_transport(events)
    verified=exit_code==0 and isinstance(response,str) and len(requests)>0 and len(served)==len(requests) and all(isinstance(e.get('model'),str) and e['model'].lower()=='glm-5.3' for e in served) and not unresolved and not transport_errors and not request_budget_exceeded and not tool_budget_exceeded
    return {'schema_version':1,'transport_schema_version':TRANSPORT_SCHEMA_VERSION,'exit_code':exit_code,'session_id':raw.get('sessionId'),'response':response if isinstance(response,str) else None,'usage':raw.get('usage'),'observed_models':[e.get('model') for e in served],'boundary_verified':verified,'model_request_count':len(requests),'model_tool_calls':sum(e.get('kind')=='tool_call' for e in records),'successful_file_reads':sum(e.get('kind')=='tool_result' and e.get('name')=='fleet_read_file' and e.get('refused') is False for e in records),'tool_budget_exceeded':tool_budget_exceeded,'request_budget_exceeded':request_budget_exceeded,'unresolved_tool_calls':unresolved,'transport_errors':transport_errors,'native_output_status':'response-present' if isinstance(response,str) else 'missing-response'}


def prepare_run(run_dir: Path, tool_policy: dict | None, cap: int) -> Path:
    if type(cap) is not int or not 1<=cap<=1000: raise ZCodeRuntimeError('ZCode tool limit missing')
    home=run_dir/'zcode-home'; config=home/'.zcode/cli/config.json'; config.parent.mkdir(parents=True,exist_ok=True)
    scratch=run_dir/'zcode-scratch'; scratch.mkdir(exist_ok=True)
    servers={}; tool_names=[]
    if tool_policy is not None:
        policy_path=run_dir/'zcode-tools.json'; policy_path.write_text(json.dumps(tool_policy),encoding='utf-8')
        tool_names=['mcp__fleet__fleet_read_file','mcp__fleet__fleet_list_files']
        if tool_policy.get('chrome_assignment'):
            import fleet_chrome_integration as chrome
            tool_names.extend('mcp__fleet__'+name for name in chrome.NATIVE_READ_ONLY_TOOLS)
        servers={'fleet':{'type':'stdio','command':sys.executable,'args':['-B',str(Path(__file__).with_name('zcode_scope_mcp.py')),'--policy',str(policy_path)],'cwd':str(scratch)}}
    controls={'permission':{'mode':'plan','allowedTools':tool_names if servers else ['__fleet_no_native_tools__'],'disallowedTools':DENIED,'autoApproveHighRisk':False,'allowMediumRiskInAuto':False},'features':{'compact':False,'rewind':False,'subagent':False,'memory':False,'skill':False,'mcp':bool(servers)},'memory':{'use':False},'plugins':{'enabled':False,'dirs':[],'enabledPlugins':{},'options':{},'suppressedBuiltins':[]},'skills':{'enabled':False,'includeInstructions':False,'roots':[]},'hooks':{'enabled':False,'events':{}},'mcp':{'servers':servers},'storage':{'dir':str(home/'.zcode'),'sessionDbPath':str(home/'.zcode/cli/db/db.sqlite')},'logging':{'level':'error','format':'json'}}
    config.write_text(json.dumps(controls),encoding='utf-8')
    (run_dir/'zcode-provider.json').write_text(json.dumps({'schemaVersion':1,'config':{'defaultModelSelection':{'providerId':PROVIDER,'modelId':MODEL}}}),encoding='utf-8')
    child_argv=[sys.executable,*servers['fleet']['args']] if servers else None
    boundary={'live':True,'transport_schema_version':TRANSPORT_SCHEMA_VERSION,'max_requests':cap+1 if servers else 1,'max_tool_calls':cap,'tools':tool_names if servers else [],'witness':str(run_dir/'zcode-witness.jsonl'),'child_argv':child_argv}
    (run_dir/'zcode-boundary.json').write_text(json.dumps(boundary),encoding='utf-8')
    return home

def child_environment(env: dict, run_dir: Path) -> dict:
    original={k:v for k,v in env.items() if k.casefold() in ENV_KEYS}
    native=subprocess.run([NODE,'-p','JSON.stringify({platform:require("node:os").platform(),home:require("node:os").homedir(),user:require("node:os").userInfo().username})'],env=original,capture_output=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    context=json.loads(native.stdout)
    secret=os.environ.get('ZCODE_CREDENTIAL_SECRET') or 'zcode-credential-fallback:'+context['platform']+':'+context['home']+':'+context['user']
    home=run_dir/'zcode-home'; result={k:v for k,v in original.items() if k.casefold() not in {'home','userprofile','homedrive','homepath'}}
    result.update(HOME=str(home),USERPROFILE=str(home),HOMEDRIVE=home.drive,HOMEPATH=str(home)[len(home.drive):],ZCODE_CREDENTIAL_SECRET=secret,ZCODE_DATA_BASE_DIR=context['home'],ZCODE_STORAGE_DIR=str(home/'.zcode'),ZCODE_MODEL_TELEMETRY_ENABLED='0',DO_NOT_TRACK='1',ZCODE_LOG_DIR=str(run_dir/'zcode-native-logs'),ZCODE_DYNAMIC_WORKFLOW_MODE='off',ZCODE_BUILTIN_PROVIDER_CONFIG_FILE=BUILTIN,ZCODE_PERSONAL_PROVIDER_CONFIG_FILE=str(run_dir/'zcode-provider.json'),FLEET_ZCODE_BOUNDARY=str(run_dir/'zcode-boundary.json'),FLEET_ZCODE_PROMPT_FILE=str(run_dir/'zcode-prompt.txt'))
    return result

def build_command(executable: str, *, model: str, mode: str, run_dir: Path, timeout: int) -> list[str]:
    require_model(model)
    if mode not in {'advisory','review'} or Path(executable).resolve()!=Path(NODE).resolve(): raise ZCodeRuntimeError('ZCode route refused')
    return [sys.executable,'-B',str(Path(__file__)), '--run-dir',str(run_dir),'--timeout',str(timeout)]

def read_export(run_dir: Path) -> dict:
    try:
        path=run_dir/'zcode-export.json'
        if path.stat().st_size>2097152: return {}
        value=json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError): return {}

def model_identity(export: dict) -> dict:
    models=export.get('observed_models',[])
    valid=export.get('boundary_verified') is True and bool(models) and all(isinstance(m,str) and m.lower()=='glm-5.3' for m in models)
    return {'status':'verified' if valid else 'unverified','expected_model':MODEL,'observed_models':models,'source':'native-http-response','error':None if valid else 'ZCode native response identity unavailable'}

def token_usage(export: dict) -> dict:
    usage=export.get('usage',{}); keys=('inputTokens','outputTokens','totalTokens')
    if not isinstance(usage,dict) or usage.get('source')!='provider' or any(type(usage.get(k)) is not int or not 0<=usage[k]<=10**12 for k in keys) or usage['totalTokens']!=usage['inputTokens']+usage['outputTokens']: return {'status':'unknown','adapter':'zcode','source':'native-provider-export'}
    return {'status':'known','adapter':'zcode','source':'native-provider-export','confidence':1.0,'input_tokens':usage['inputTokens'],'output_tokens':usage['outputTokens'],'total_tokens':usage['totalTokens'],'cache_read_tokens':usage.get('cacheReadTokens',0),'cache_write_tokens':usage.get('cacheWriteTokens',0),'cost_usd':None,'cost_semantics':'coding-plan-allowance-not-api-estimate'}

def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument('--run-dir',type=Path,required=True); parser.add_argument('--timeout',type=int,required=True); args=parser.parse_args()
    run_dir=args.run_dir.resolve(); require_native()
    env=child_environment(dict(os.environ),run_dir)
    argv=[NODE,'--require',str(Path(__file__).with_name('zcode_native_witness.cjs')),CLI,'--prompt','__FLEET_PRIVATE_PROMPT__','--mode','plan','--json','--no-color','--surface','terminal','--disallowed-tools',*DENIED]
    # Fleet supervises this entire private child tree and enforces wall time.
    child=subprocess.run(argv,env=env,cwd=run_dir/'zcode-scratch',capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        if len(child.stdout)>2097152: raise ValueError('Native output bound')
        try: raw=json.loads(child.stdout)
        except (ValueError,UnicodeError): raw=None
        witness_path=run_dir/'zcode-witness.jsonl'
        witness_bytes=witness_path.read_bytes()
        if len(witness_bytes)>1048576: events=[{'kind':'transport_error','code':'invalid_tool_call_metadata'}]
        else:
            try: events=[json.loads(line) for line in witness_bytes.decode('utf-8').splitlines()]
            except (ValueError,UnicodeError): events=[{'kind':'transport_error','code':'invalid_tool_call_metadata'}]
        value=terminal_export(raw,events,child.returncode)
        (run_dir/'zcode-export.json').write_text(json.dumps(value),encoding='utf-8')
        response=value['response']
        if response is None: raise ValueError('Native response missing')
        print(json.dumps([{'type':'message','role':'assistant','generation_status':'completed','content':[{'type':'text','text':response}]}]))
        return child.returncode
    except (ValueError,TypeError,OSError,KeyError):
        print('Native ZCode output or identity boundary failed',file=sys.stderr); return child.returncode or 126

if __name__=='__main__':
    try: raise SystemExit(main())
    except (ZCodeRuntimeError,OSError,subprocess.SubprocessError,ValueError):
        print('Native ZCode preflight failed',file=sys.stderr); raise SystemExit(126)
