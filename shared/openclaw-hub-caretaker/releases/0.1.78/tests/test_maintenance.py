import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import maintenance
from maintenance import event_id, publication_tick, route, source_alias_only
from caretaker.publication import PublicationError


TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"


class SourceAliasTests(unittest.TestCase):
 def test_copied_log_location_is_not_a_changed_fact(self):
  original={'tool':'claude','native_session_id':'n','cursor':'c','excerpt':'fact','source_ref':'log-a:10','classification':'internal','observed_at':'2026-09-17','cwd':'project'}
  self.assertTrue(source_alias_only(original,original|{'source_ref':'log-b:22'}))
  self.assertFalse(source_alias_only(original,original))
  for key,value in [('excerpt','changed fact'),('classification','restricted'),('observed_at','2026-09-18'),('cwd','other'),('cursor','other')]:
   self.assertFalse(source_alias_only(original,original|{'source_ref':'log-b:22',key:value}))


class _Event:
 def __init__(self, *, project="one"):
  self.project = project

 def to_dict(self):
  return {
   "schema_version": 1,
   "event_type": "completed_turn.v1",
   "tool": "codex",
   "native_session_id": "native-1",
   "cursor": "cursor-1",
   "observed_at": "2026-09-17T12:00:00Z",
   "title": "Synthetic completed turn",
   "excerpt": "A bounded synthetic maintenance event.",
   "classification": "internal",
   "project": self.project,
  }


def _maintenance_fixture(root: Path):
 source = root / "source.jsonl"
 source.write_text("synthetic source\n", encoding="utf-8")
 base_config = root / "base-config.json"
 base_config.write_text(json.dumps({
  "project_slug": "base",
  "agent": "hub-caretaker",
  "session_id": "base-session",
  "state_dir": str(root / "base-state"),
  "hub": {"vault": str(root / "vault")},
  "allowlist": {"tools": ["codex"]},
  "primary": {},
  "office": {},
  "limits": {},
 }), encoding="utf-8")
 control = {
  "state_dir": str(root / "control-state"),
  "base_config": str(base_config),
  "python": "python",
  "sources": [{"glob": str(source), "tool": "codex", "kind": "jsonl"}],
  "known_projects": ["one"],
  "session_projects": {"native-1": "one"},
  "max_sources_per_tick": 4,
  "max_projects_per_tick": 4,
  "publication": {"enabled": False},
  "retry_epoch": "epoch-1",
 }
 control_path = root / "control.json"
 control_path.write_text(json.dumps(control), encoding="utf-8")
 return control, control_path, source

class RoutingTests(unittest.TestCase):
 def test_scoped_mapping_not_path_prefix_guess(self):
  control={'workspace_projects':{r'D:\Projects\one':'one',r'D:\Projects\one\sub':'two'},'session_projects':{'native':'three'}}
  self.assertEqual(route({'cwd':r'D:\Projects\one\sub\task'},control),'two')
  self.assertIsNone(route({'cwd':r'D:\Projects\one-other'},control))
  self.assertEqual(route({'native_session_id':'native'},control),'three')
  self.assertIsNone(route({},control))
 def test_project_escape_rejected(self):
  self.assertIsNone(route({'native_session_id':'x'},{'session_projects':{'x':'../../outside'}}))
 def test_explicit_project_requires_known_owner_allowlist(self):
  self.assertEqual(route({'project':'one'},{'known_projects':['one']}),'one')
  self.assertIsNone(route({'project':'unknown'},{'known_projects':['one']}))
 def test_identity_is_provenance_not_text(self):
  a={'tool':'codex','native_session_id':'native','cursor':'turn-1','excerpt':'a'}
  self.assertEqual(event_id(a),event_id(a|{'excerpt':'b'}))
  self.assertNotEqual(event_id(a),event_id(a|{'cursor':'turn-2'}))

 def test_two_publication_failures_stop_further_model_calls(self):
  TASK_TMP.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
   vault=Path(directory)/'vault'
   folder=vault/'proposals'/'codex';folder.mkdir(parents=True)
   (folder/'prop-1.md').write_text('---\nid: "prop-1"\nclassification: "internal"\nagent: "codex"\nproject_slug: "one"\ntarget_pages: ["wiki/projects/one.md"]\nsource_ids: ["src-1"]\n---\nbody\n',encoding='utf-8')
   db=sqlite3.connect(':memory:')
   cfg={'project_slug':'one','hub':{'vault':str(vault)},'agent':'hub-caretaker','state_dir':str(Path(directory)/'state')}
   control={'publication':{'enabled':True,'include_existing_hub_backlog':True,'min_ready_groups':1,'max_wait_hours':24,'max_batch':8}}
   blocked={'status':'BLOCKED','reason':'independent_review_not_accepted'}
   with patch('maintenance._invoke_curate',return_value=blocked) as run:
    self.assertEqual(publication_tick('config',cfg,control,db)['status'],'BLOCKED')
    self.assertEqual(publication_tick('config',cfg,control,db)['status'],'QUARANTINED')
    third=publication_tick('config',cfg,control,db)
    self.assertIn(third['status'],('QUARANTINED','SKIPPED_NO_DUE_BATCH','IDLE'))
    self.assertEqual(run.call_count,2)
   db.close()


class PipelineCircuitTests(unittest.TestCase):
 def test_project_outage_retries_when_backoff_due_without_epoch_reset(self):
  TASK_TMP.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
   root=Path(directory);control,_,_= _maintenance_fixture(root)
   state=Path(control['state_dir'])
   state.mkdir(parents=True, exist_ok=True)
   failure=json.dumps({'status':'BLOCKED','incidents':['local_outage'],
                       'notes':'primary_failed'})
   success=json.dumps({'status':'OK','incidents':[],'notes':''})
   calls=[]
   def run_process(*args, **kwargs):
    calls.append({'args':args[0],'env':kwargs.get('env',{})})
    code=0 if len(calls)==3 else 1
    return subprocess.CompletedProcess(args[0], code,
                                       success if code==0 else failure, '')

   event=_Event()
   with patch('maintenance.collect_sources',
              return_value=SimpleNamespace(events=(event,), issues=())):
    with patch('maintenance.subprocess.run', side_effect=run_process):
     first=maintenance._locked_tick(control,state)
     second=maintenance._locked_tick(control,state)
     blocked=maintenance._locked_tick(control,state)
     self.assertEqual(len(calls),1)
     self.assertEqual(blocked['projects'][0]['pipeline']['status'],
                      'BACKOFF_DEFERRED')

     def elapsed():
      db=sqlite3.connect(state/'intake.sqlite')
      db.execute("UPDATE pipeline_failures SET next_due='2000-01-01T00:00:00+00:00'")
      db.commit();db.close()
     elapsed()
     maintenance._locked_tick(control,state)
     self.assertEqual(len(calls),2)
     elapsed()
     retried=maintenance._locked_tick(control,state)
     self.assertEqual(len(calls),3)
     self.assertEqual(retried['projects'][0]['pipeline']['exit_code'],0)

     # A subsequent identical failure is allowed after the successful run;
     # the success reset the same-epoch circuit count to zero.
     failing_again=maintenance._locked_tick(control,state)
     self.assertEqual(len(calls),4)
     self.assertEqual(failing_again['projects'][0]['pipeline']['exit_code'],1)

   db=sqlite3.connect(state/'intake.sqlite')
   row=db.execute('SELECT count,retry_epoch FROM pipeline_failures '
                  'WHERE project=?',('one',)).fetchone()
   self.assertEqual(row,(1,control.get('retry_epoch','initial')))
   db.close()

 def test_sqlite_wal_fingerprint_change_rescans_source(self):
  TASK_TMP.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
   root=Path(directory);control,_,source=_maintenance_fixture(root)
   Path(control['state_dir']).mkdir(parents=True, exist_ok=True)
   control['sources'][0]['kind']='sqlite'
   source=source.with_suffix('.sqlite')
   source.write_bytes(b'main database bytes')
   control['sources'][0]['glob']=str(source)
   calls=[]
   empty=SimpleNamespace(events=(), issues=())
   with patch('maintenance.collect_sources',
              side_effect=lambda specs: calls.append(specs[0].path) or empty):
    first=maintenance._locked_tick(control,Path(control['state_dir']))
    self.assertEqual(first['new_events'],0)
    self.assertEqual(len(calls),1)
    # The main SQLite file is untouched.  A WAL-only update must invalidate
    # the stored scanner fingerprint and trigger a second read.
    wal=Path(str(source)+'-wal')
    wal.write_bytes(b'wal frame 1')
    second=maintenance._locked_tick(control,Path(control['state_dir']))
   self.assertEqual(second['new_events'],0)
   self.assertEqual(calls,[str(source),str(source)])

 def test_enabling_user_messages_rescans_completed_source(self):
  TASK_TMP.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
   root=Path(directory);control,_,_= _maintenance_fixture(root)
   state=Path(control['state_dir']);state.mkdir(parents=True, exist_ok=True)
   empty=SimpleNamespace(events=(), issues=())
   calls=[]
   def collect(specs):
    calls.append(specs[0].include_user_messages)
    return empty
   with patch('maintenance.collect_sources',side_effect=collect):
    first=maintenance._locked_tick(control,state)
    self.assertEqual(first['new_events'],0)
    control['sources'][0]['include_user_messages']=True
    second=maintenance._locked_tick(control,state)
   self.assertEqual(second['new_events'],0)
   self.assertEqual(calls,[False,True])

 def test_connector_scan_schema_change_rescans_existing_evidence(self):
  TASK_TMP.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
   root=Path(directory);control,_,source=_maintenance_fixture(root)
   state=Path(control['state_dir']);state.mkdir(parents=True, exist_ok=True)
   empty=SimpleNamespace(events=(), issues=())
   calls=[]
   with patch('maintenance.collect_sources',side_effect=lambda specs: calls.append(specs[0].path) or empty):
    maintenance._locked_tick(control,state)
    database=sqlite3.connect(state/'intake.sqlite')
    stored=database.execute('SELECT fingerprint FROM scans WHERE path=?',(str(source),)).fetchone()[0]
    self.assertIn(f'scan_schema={maintenance.CONNECTOR_SCAN_SCHEMA_VERSION}',stored)
    stat=source.stat()
    database.execute('UPDATE scans SET fingerprint=? WHERE path=?',(
     f'{stat.st_mtime_ns}:{stat.st_size}:include_user_messages=0',str(source)))
    database.commit();database.close()
    maintenance._locked_tick(control,state)
   self.assertEqual(calls,[str(source),str(source)])

 def test_each_source_keeps_its_user_message_opt_in(self):
  TASK_TMP.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
   root=Path(directory);control,_,source=_maintenance_fixture(root)
   second_source=root/'second.jsonl';second_source.write_text('synthetic source 2\n',encoding='utf-8')
   control['sources']=[
    {'glob':str(source),'tool':'codex','kind':'jsonl'},
    {'glob':str(second_source),'tool':'codex','kind':'jsonl','include_user_messages':True},
   ]
   state=Path(control['state_dir']);state.mkdir(parents=True, exist_ok=True)
   empty=SimpleNamespace(events=(), issues=())
   calls={}
   def collect(specs):
    calls[specs[0].path]=specs[0].include_user_messages
    return empty
   with patch('maintenance.collect_sources',side_effect=collect):
    report=maintenance._locked_tick(control,state)
   self.assertEqual(report['new_events'],0)
   self.assertEqual(calls,{str(source):False,str(second_source):True})


class SourceFairnessTests(unittest.TestCase):
 def test_source_families_each_get_a_slot_under_fleet_fanout(self):
  TASK_TMP.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
   root=Path(directory);control,_,_= _maintenance_fixture(root)
   state=Path(control['state_dir']);state.mkdir(parents=True, exist_ok=True)
   fleet=root/'fleet';fleet.mkdir()
   for index in range(40):
    (fleet/f'{index:03d}.jsonl').write_text(f'fleet {index}\n',encoding='utf-8')
   minimax=root/'minimax.jsonl';minimax.write_text('minimax 1\n',encoding='utf-8')
   codex=root/'codex.jsonl';codex.write_text('codex 1\n',encoding='utf-8')
   control['sources']=[
    {'glob':str(fleet/'*.jsonl'),'tool':'fleet','kind':'jsonl'},
    {'glob':str(minimax),'tool':'minimax','kind':'jsonl'},
    {'glob':str(codex),'tool':'codex','kind':'jsonl'},
   ]
   control['max_sources_per_tick']=3
   calls=[];empty=SimpleNamespace(events=(),issues=())
   def collect(specs):
    calls.append(specs[0].tool)
    return empty
   with patch('maintenance.collect_sources',side_effect=collect):
    first=maintenance._locked_tick(control,state)
    minimax.write_text('minimax 2\n',encoding='utf-8')
    codex.write_text('codex 2\n',encoding='utf-8')
    second=maintenance._locked_tick(control,state)
   self.assertEqual(first['new_events'],0)
   self.assertEqual(second['new_events'],0)
   self.assertEqual(len(calls),6)
   self.assertEqual(set(calls[:3]),{'fleet','minimax','codex'})
   self.assertEqual(set(calls[3:]),{'fleet','minimax','codex'})

if __name__=='__main__':unittest.main()
