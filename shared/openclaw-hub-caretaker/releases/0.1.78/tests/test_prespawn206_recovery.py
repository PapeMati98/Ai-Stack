"""Synthetic normal tick -> real phase/maker/independent review -> publish seam."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import curate_once
import maintenance
from caretaker import prespawn206
from caretaker.audit import ensure_tables
from caretaker.config import load_config
from caretaker.pubstate import ensure_pub_tables, read_row, code_version
from caretaker.publication import sha
from caretaker.repaired_retry import ensure_retry_table, retry_token, REPAIRED_PUBLICATION_SHA256, TERMINAL_GENERATION_RETRY_REASON
from caretaker.terminal_generation import helper_sha256
from caretaker.transports import Envelope
from caretaker.transports import AgentFleetArgvTransport, TransportError
from caretaker.config import TransportSpec
from tests import test_terminal_generation_invoke as invoke_fixture
from tests.test_curate_fleet_reviewer import _HashPassReviewer
from tests import test_publication_state_owner as fixture

TEMP=Path(os.environ.get('TEAM_TEST_ROOT') or tempfile.gettempdir())


class PreSpawn206Tests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(exist_ok=True, parents=True)
        patched=patch.object(fixture, 'TEST_TMP', TEMP);patched.start();self.addCleanup(patched.stop)
        case=invoke_fixture.TerminalGenerationInvokeTests()
        self.config,self.paths,self.cfg,self.pointer,record,plan=case._case(self, fleet=True)
        self.root=self.config.parents[3]
        self.vault=Path(self.cfg.hub.vault)
        (self.vault/'wiki/registry.md').write_text('```json\n'+json.dumps({'schema_version':1,'projects':{'test':{'main_page':fixture.MAIN}},'aliases':{}})+'\n```\n')
        self.frozen=plan['frozen'];self.phase_state=self.pointer.parent
        self.initial=self.phase_state/'initial-generation'
        self.candidate=self.initial/'candidate'
        self.manifest=self.candidate/'publication.json'
        self.progress={k:v for k,v in record['progress'].items() if not k.startswith('terminal_generation_')}
        self.progress.update(phase='REPAIR_STARTED',repair_attempt=1,maker_format_repairs=0,reviewer_format_repairs={})
        curate_once._save_phase_progress(self.cfg,self.paths,self.frozen,self.initial,record['observed'],'REPAIR_STARTED',None,None,self.progress)
        archive=self.initial/('terminal-generation-archive-'+sha(self.manifest.read_bytes())[:16]);archive.mkdir()
        (archive/'phase.json').write_bytes(self.pointer.read_bytes())
        (archive/'candidate-pins.json').write_text(json.dumps(curate_once._phase_pins(self.phase_state,self.candidate)))
        self.generation=self.phase_state/'terminal-generation-retry-0123456789abcdef';self.generation.mkdir()
        configs=self.generation/'run-configs';configs.mkdir()
        (configs/'rid-maker-candidate-review-repair-append0.json').write_text('{}')
        curate_once._save_phase_progress(self.cfg,self.paths,self.frozen,self.generation,record['observed'],'REPAIR_STARTED',None,None,self.progress)
        self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
        ensure_tables(self.db);ensure_pub_tables(self.db);ensure_retry_table(self.db)
        item=self.frozen['proposals'][0];self.identity=(item['id'],item['path'],item['sha256'])
        self.old_batch='a'*64;self.batch='repaired-'+'e'*64;self.cycle='audit-reserved206'
        token=retry_token(REPAIRED_PUBLICATION_SHA256,'test',*self.identity,reason=TERMINAL_GENERATION_RETRY_REASON,helper_sha256=helper_sha256())
        selected=dict(proposal_id=self.identity[0],path=self.identity[1],sha256=self.identity[2],token=token,
                      reason=TERMINAL_GENERATION_RETRY_REASON,project='test',previous_batch=self.old_batch,
                      helper_sha256=helper_sha256(),publication_sha256=REPAIRED_PUBLICATION_SHA256,code_version=code_version())
        self.db.execute('INSERT INTO audit_cycles(id,lane,status,counts,trigger,created_at) VALUES(?,?,?,?,?,?)',(self.cycle,'legacy','DRAINED',json.dumps({'repaired_retry':[selected]}),'threshold','2026-10-24T00:00:00Z'))
        self.db.execute('INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status) VALUES(?,?,?,?,?,?,?)',(self.cycle,*self.identity,'test','test','unknown'))
        self.db.execute('INSERT INTO repaired_retry_tokens(token,cycle_id,publication_sha256,code_version,proposal_id,path,sha256,previous_batch) VALUES(?,?,?,?,?,?,?,?)',(token,self.cycle,REPAIRED_PUBLICATION_SHA256,code_version(),*self.identity,self.old_batch))
        source=dict(status='QUARANTINED',reason='phase_resume_terminal_no_replay',project='test',paths=self.paths,proposal_ids=[self.identity[0]],hashes=[self.identity[2]])
        old=dict(source,status='UNKNOWN_REQUIRES_RECONCILIATION',reason='terminal_generation_maker_outcome_unknown',cycle_id=self.cycle)
        for batch,status,value in [(self.old_batch,'QUARANTINED',source),(self.batch,old['status'],old)]:
            self.db.execute('INSERT INTO publications(batch,status,failures,attempt,fingerprint,result) VALUES(?,?,?,?,?,?)',(batch,status,3,4,'fp',json.dumps(value)))
        self.db.commit()
        raw=json.loads(self.config.read_text());raw['maintenance_terminal_generation_retry'].update(
            helper_sha256=helper_sha256(),publication_sha256=REPAIRED_PUBLICATION_SHA256,code_version=code_version(),cycle_id=self.cycle,previous_batch=self.old_batch,previous_fingerprint='fp',members=[selected])
        self.config.write_text(json.dumps(raw));self.cfg=load_config(self.config)
        self.evidence=self.generation.parents[3]/'synthetic-fleet-evidence';self.evidence.mkdir()
        self.run=self.evidence/'run-synthetic206';self.run.mkdir()
        self.error=b'Traceback (most recent call last):\r\n  File "process_supervision.py", line 231, in _child\r\n    subprocess.run(argv)\r\n  File "subprocess.py", line 1554, in _execute_child\r\nFileNotFoundError: [WinError 206] filename too long\r\n'
        self.write_witness()
        self.row=read_row(self.db,self.batch)
        self.enterContext(patch.object(prespawn206,'EVIDENCE_ROOT',self.evidence))
        self.active=self.enterContext(patch.object(prespawn206,'owned_process_active',return_value=False))
        from caretaker.publication import existing_page_body
        self.enterContext(patch('caretaker.draft_summary._prior_inventory_body_proof',
                               return_value={'body':existing_page_body(self.frozen['before'])}))

    def write_witness(self, **updates):
        err=self.run/'attempt-1.stderr.txt';err.write_bytes(self.error)
        out=self.run/'attempt-1.stdout.txt';out.write_bytes(b'')
        checkpoint=dict(schema='agent-fleet.process-checkpoint.v1',status='process_failed',exit_code=1,
                        pid=99999999,started_at='2026-10-25T01:10:25Z',finished_at='2026-10-25T01:10:26Z',
                        stdout_bytes=0,stdout_sha256=sha(b''),stderr_bytes=len(self.error),stderr_sha256=sha(self.error))
        (self.run/'attempt-1.process-checkpoint.json').write_text(json.dumps(checkpoint))
        attempt=dict(attempt=1,exit_code=1,timed_out=False,command=['kimi.exe','-p','<redacted>'],stderr_path=str(err),stdout_path=str(out))
        result=dict(run_id=self.run.name,agent='kimi',status='FAILED',exit_code=1,timed_out=False,
                    continuation_count=0,workspace=str(self.generation),execution_workspace=str(self.generation),provider_attempts=[attempt])
        result.update(updates);(self.run/'result.json').write_text(json.dumps(result))

    def qualify(self):
        return prespawn206.legacy206_plan(self.config,self.db,read_row(self.db,self.batch))

    def test_normal_tick_resumes_same_generation_reviews_and_publishes_once(self):
        self.assertIsNotNone(self.qualify())
        calls=[];reviewer=_HashPassReviewer(provider='grok',model='grok-4')
        original_source=read_row(self.db,self.old_batch)
        tokens=list(self.db.execute('SELECT * FROM repaired_retry_tokens'))
        class Maker:
            def invoke(inner,**kwargs):
                calls.append(kwargs['cwd'])
                decision={'body':'# Current source note\nCited synthetic evidence (src-one).\n',
                          'summary':'Current synthetic source evidence.',
                          'dispositions':{self.identity[0]:{'disposition':'synthesized','reason':'Cited src-one.'}}}
                return Envelope(ok=True,status='ok',provider='kimi',model='k3',session_id='maker-206',final=json.dumps(decision),raw={})
        def publish(cfg,paths,root,result):
            self.assertEqual(root,self.generation)
            self.assertEqual(result['status'],'REVIEWED_CANDIDATE')
            self.assertNotEqual(result['maker_lineage'], 'grok')
            review=json.loads(Path(result['review']).read_text())
            self.assertEqual(review['verdict'],'PASS')
            result.update(status='PUBLISHED',publication={'status':'PUBLISHED','event_id':'synthetic-event'},checkpoint={'status':'CHECKPOINTED'})
        with patch.object(curate_once,'CanonicalKnowledgeGateway') as gateway, \
             patch.object(curate_once,'_default_fleet_transport',side_effect=lambda agent,model: Maker() if agent=='kimi' else reviewer), \
             patch.object(curate_once,'_publish_retained',side_effect=publish):
            gateway.return_value.health.return_value=SimpleNamespace(status='pass')
            gateway.return_value.lint.return_value=SimpleNamespace(status='pass')
            policy=dict(self.cfg.raw['publication'],enabled=True,projects=['test'],include_existing_hub_backlog=True,min_ready_groups=1,max_batches_per_tick=1)
            control={'publication':policy,'known_projects':[],'max_projects_per_tick':1}
            base=fixture._base(self.root,self.vault);base['project_slug']='unrelated-base'
            runtime_state=Path(self.cfg.raw['maintenance_state_dir'])
            preflight={'result':{'status':'PASS','health_status':'pass','lint_status':'pass'}}
            result=maintenance.publication_drain(control,base,self.db,runtime_state,lane='legacy',recovery={'status':'NO_LOCK'},_hub_preflight_state=preflight)
            self.assertTrue(result.get('batches') and result['batches'][0]['status']=='PUBLISHED',result)
            second=maintenance.publication_drain(control,base,self.db,runtime_state,lane='legacy',recovery={'status':'NO_LOCK'},_hub_preflight_state=preflight)
        self.assertEqual(calls,[str(self.generation)])
        self.assertEqual(len(reviewer.calls),1)
        self.assertEqual(read_row(self.db,self.old_batch),original_source)
        self.assertEqual(list(self.db.execute('SELECT * FROM repaired_retry_tokens')),tokens)
        final=read_row(self.db,self.batch)
        self.assertEqual((final['failures'],final['attempt']),(3,5))
        receipt=json.loads((self.generation/'transport-recovery206.json').read_text())
        self.assertEqual(receipt['previous_row'],self.row)
        self.assertEqual(receipt['previous_phase']['progress']['repair_attempt'],1)
        self.assertIsNone(self.qualify())

    def test_native_started_timeout_and_other_error_reject_without_effects(self):
        for update in ({'native_started':True},{'timed_out':True},{'provider_session_id':'native-session'}):
            with self.subTest(update=update):
                self.write_witness(**update);self.assertIsNone(self.qualify())
        self.error=self.error.replace(b'WinError 206',b'WinError 216');self.write_witness()
        self.assertIsNone(self.qualify())
        self.assertEqual(read_row(self.db,self.batch),self.row)
        self.assertFalse((self.generation/'transport-recovery206.json').exists())

    def test_active_pid_source_drift_and_effects_reject(self):
        self.active.return_value=True;self.assertIsNone(self.qualify());self.active.return_value=False
        source=self.vault/self.paths[0];before=source.read_bytes();source.write_bytes(before+b'drift')
        self.assertIsNone(self.qualify());source.write_bytes(before)
        (self.generation/'candidate').mkdir();self.assertIsNone(self.qualify())
        self.assertEqual(read_row(self.db,self.batch),self.row)

    def test_wrong_workspace_and_ambiguous_results_reject(self):
        self.write_witness(execution_workspace=str(self.initial));self.assertIsNone(self.qualify())
        self.write_witness();second=self.evidence/'run-second206';shutil.copytree(self.run,second)
        result=json.loads((second/'result.json').read_text());result['run_id']=second.name
        for entry in result['provider_attempts']:
            for key in ('stdout_path','stderr_path'):entry[key]=str(second/Path(entry[key]).name)
        (second/'result.json').write_text(json.dumps(result));self.assertIsNone(self.qualify())

    def test_counter_or_format_retry_proof_is_not_silently_ignored(self):
        original=self.pointer.read_bytes();doc=json.loads(original)
        doc['progress']['maker_calls']+=1;doc['cache_sha256']=curate_once._phase_digest(doc)
        self.pointer.write_text(json.dumps(doc));self.assertIsNone(self.qualify())
        self.pointer.write_bytes(original)
        (self.generation/'run-configs/rid-maker-candidate-review-repair-repair-append0.json').write_text('{}')
        self.assertIsNone(self.qualify())

    def test_lossy_utf8_supervisor_hash_requires_exact_raw_roundtrip(self):
        raw=self.error.replace(b'filename too long',b'\xbf\xf3')
        error=raw.decode('utf-8',errors='replace').replace('\r\n','\n').replace('\n','\r\n').encode('utf-8')
        checkpoint={'stderr_bytes':len(raw),'stderr_sha256':sha(raw)}
        self.assertTrue(prespawn206._checkpoint_stderr_matches(error,checkpoint))
        checkpoint['stderr_sha256']='0'*64
        self.assertFalse(prespawn206._checkpoint_stderr_matches(error,checkpoint))

    def test_future_transport_classifies_only_source_backed_prespawn206(self):
        self.write_witness(output_dir=str(self.run))
        def runner(**kwargs):
            return 1,(self.run/'result.json').read_text(),''
        spec=TransportSpec(kind='openclaw_argv',executable='python.exe',
            argv=['fleet.py','run','--agent','kimi','--model','k3'],fleet_role='maker',data_classification='internal')
        with patch('caretaker.transports.resolve_fleet_py',return_value=self.run/'fleet.py'), \
             patch('caretaker.transports._fleet_selected_profiles',return_value={}), \
             patch('caretaker.transports._fleet_task_assessment',return_value={}):
            for expected in ('native_prespawn_win206','nonzero_exit'):
                with self.assertRaises(TransportError) as error:
                    AgentFleetArgvTransport(run_callable=runner).invoke(spec=spec,cwd=str(self.generation),
                        state_dir=str(self.generation),prompt='synthetic',run_config_path='unused',timeout=90)
                self.assertEqual(error.exception.reason_code,expected)
                self.error=self.error.replace(b'WinError 206',b'WinError 216')
                self.write_witness(output_dir=str(self.run))

    def test_future_precall_uses_existing_defer_restore_mechanism(self):
        result,calls,details,pointer,record=invoke_fixture.TerminalGenerationInvokeTests()._invoke(
            self,fleet=True,transport_error='native_prespawn_win206')
        self.assertEqual(result['status'],'DEFERRED_PREPARED')
        self.assertEqual(result['model_calls'],0)
        self.assertEqual(calls['fleet'],[('kimi','k3')])
        self.assertEqual(json.loads(pointer.read_text()),record)


if __name__=='__main__':unittest.main()
