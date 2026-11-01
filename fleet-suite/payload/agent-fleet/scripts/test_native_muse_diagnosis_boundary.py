import json, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import fleet
ok={'status':'completed','summary':'Completed synthetic artifact review.','files_changed':[],'verification':['Synthetic product behavior checked.'],'risks':[],'next_steps':[]}
numbered='1| {\n2| "status": "FAIL",\n3| "message": "synthetic assertion"\n4| }'
run_stream='synthetic-diagnosis-run'
def stream(tool=None,answer=None,other=()):
 rows=[]
 if tool is not None:rows.append({'record_type':'event','payload_type':'tool.result','payload':{'tool_use_id':'synthetic-read','text':tool}})
 rows.extend(other)
 if answer is not None:rows.append({'record_type':'event','payload_type':'run.terminal.completed','payload':{'run_stream':run_stream,'terminal':'completed','text':answer}})
 return '\n'.join(json.dumps(x) for x in rows)
def error(text):
 p=fleet.project_provider_handoff('muse',text);d,raw=fleet.handoff_diagnosis_text(p,text)
 return p,fleet.handoff_semantic_error(p['handoff'],mode='implement',kind='implementation',source_text=d,raw_source=raw),raw
passed=[]
def check(name,predicate):
 assert predicate,name;passed.append(name)
p,e,raw=error(stream(numbered,'Finished synthetic work without a structured final handoff.'))
check('plain final rejected as no candidate, never numbered tool JSON',p['handoff'] is None and 'NO HANDOFF CANDIDATE' in str(e) and not raw and 'MALFORMED JSON' not in str(e))
for answer in (None,'','   '):
 p,e,raw=error(stream(numbered,answer))
 expected=fleet.HANDOFF_DIAGNOSIS_UNKNOWN if answer is None else 'NO HANDOFF CANDIDATE'
 check('empty or absent final remains rejected without raw fallback '+repr(answer),p['handoff'] is None and expected in str(e) and 'MALFORMED JSON' not in str(e) and not raw)
p,e,raw=error(stream(json.dumps(ok),None))
check('valid handoff inside tool result never accepted',p['handoff'] is None and not raw and e==fleet.HANDOFF_DIAGNOSIS_UNKNOWN and p['error']=='TERMINAL_ABSENT_OR_CONFLICTING')
p,e,raw=error(stream(numbered,'{"status":"completed",bad synthetic final}'))
check('genuine malformed final diagnosed from final only','MALFORMED JSON' in str(e) and 'bad synthetic final' in str(e) and '2|' not in str(e) and not raw)
p,e,raw=error(stream(numbered,json.dumps(ok)))
check('valid final after numbered tool remains valid',p['handoff']==ok and e is None)
p,e,raw=error(stream(numbered,other=[{'payload_type':'task.lifecycle.completed','payload':{'event':{'status':'completed','text':json.dumps(ok)}}}]))
check('nested task lifecycle never becomes handoff',p['handoff'] is None and not raw)
p=fleet.project_provider_handoff('muse',stream(numbered,'plain final'))
d,raw=fleet.handoff_diagnosis_text(p,'arbitrary raw tool transcript')
check('selected answer used for rejection',d=='plain final' and not raw)
d,raw=fleet.handoff_diagnosis_text({},'legacy raw source')
check('legacy projection without candidate field retains fallback',d=='legacy raw source' and raw)
provider='PROVIDER FAILURE synthetic timeout'
check('provider failure leads',fleet.handoff_semantic_error(None,mode='implement',kind='implementation',source_text='plain final',provider_failure=provider).startswith(provider))
task_event={'payload_type':'task.lifecycle.completed','payload':{'event':{'status':'completed'}}}
p,e,raw=error(stream(numbered,other=[task_event]))
check('task completion is not run completion',p['terminal_reason'] is None)
failed_event={'payload_type':'run.terminal.failed','payload':{'run_stream':run_stream,'terminal':'failed','text':'synthetic native step limit reached'}}
p,e,raw=error(stream(numbered,other=[task_event,failed_event]))
check('actual native failed terminal is reported failed',p['terminal_reason']=='failed' and p['handoff'] is None)
failed_no_status={'payload_type':'run.terminal.failed','payload':{'run_stream':run_stream,'text':'synthetic failure'}}
p,e,raw=error(stream(numbered,other=[failed_no_status]))
check('failed event suffix preserves failure when status omitted',p['terminal_reason']=='failed' and p['handoff'] is None)
bad_contract=dict(ok,status='done_partial',verification='synthetic verification string');bad_contract.pop('next_steps')
p,e,raw=error(stream(numbered,json.dumps(bad_contract)))
check('actual observed invalid handoff diagnosed as contract mismatch',p['handoff'] is None and str(e).startswith('HANDOFF CONTRACT MISMATCH') and '2|' not in str(e) and not raw)

# Stream identity rejection must remain separate from final-answer diagnosis.
for identity in (None,'other-synthetic-run'):
 payload={'terminal':'completed','text':json.dumps(ok)}
 if identity is not None:payload['run_stream']=identity
 events=[{'payload_type':'run.lifecycle.started','payload':{'run_stream':run_stream}},
         {'payload_type':'run.terminal.completed','payload':payload}]
 p,e,raw=error(stream(numbered,other=events))
 check('missing or mismatched terminal stream remains rejected '+repr(identity),p['handoff'] is None and p['error']=='TERMINAL_RUN_MISMATCH_OR_MISSING' and e is not None and not raw)

print(json.dumps({"status":"PASS","checks":len(passed),"names":passed}))
