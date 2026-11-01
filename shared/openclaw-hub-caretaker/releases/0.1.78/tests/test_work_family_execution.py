"""Real tick/lease/rotation/locked-tick orchestration, synthetic terminal seams."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import maintenance
from caretaker import tick_budget

TEMP=Path(os.environ.get('TEAM_TEST_ROOT') or tempfile.gettempdir())/'caretaker-tests'


class WorkFamilyExecutionTests(unittest.TestCase):
    def setUp(self):
        TEMP.mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(prefix='execution-',dir=TEMP)
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.state=self.root/'state';self.state.mkdir()
        self.vault=self.root/'vault';self.vault.mkdir()
        self.base=json.loads((Path(maintenance.__file__).parent/'config.example.json').read_text(encoding='utf-8'))
        self.base['hub']['vault']=str(self.vault)
        self.base_path=self.root/'base.json';self.base_path.write_text(json.dumps(self.base))
        native=self.root/'synthetic-session.jsonl';native.write_text('{}\n')
        self.control={'state_dir':str(self.state),'base_config':str(self.base_path),
            'python':sys.executable,'max_tick_seconds':1200,'max_projects_per_tick':1,
            'sources':[{'id':'session-fixture','tool':'openclaw','kind':'normalized_events','glob':str(native)},
                       {'id':'file-fixture','tool':'files','kind':'files','scheduled':True}],
            'new_event_epoch':{'enabled':True,'activate':False},
            'publication':{'enabled':True,'include_existing_hub_backlog':True},
            'known_projects':['synthetic-project']}
        self.control_path=self.root/'control.json';self.control_path.write_text(json.dumps(self.control))
        event={'tool':'openclaw','native_session_id':'synthetic-session','cursor':'1',
               'completed_at':'2026-10-25T07:00:00Z','body':'Synthetic fixture only.'}
        self.event_id=maintenance.event_id(event)
        with sqlite3.connect(self.state/'intake.sqlite') as db:
            db.execute('CREATE TABLE queue(id TEXT PRIMARY KEY,project TEXT,payload TEXT NOT NULL,forwarded INTEGER NOT NULL DEFAULT 0)')
            maintenance._ensure_new_epoch_tables(db)
            db.execute('INSERT INTO new_event_epoch VALUES(1,?)',('2026-10-24T00:00:00Z',))
            db.execute('INSERT INTO queue VALUES(?,?,?,0)',(self.event_id,'synthetic-project',json.dumps(event)))
            db.execute("INSERT INTO new_epoch_events(event_id,status,reason,forwarded) VALUES(?,'admitted','',0)",(self.event_id,))
        db.close()
        self.trace=[];self.drain_kwargs=[]
        self.gate_result={'status':'PASS','health_status':'pass','lint_status':'pass'}
        self.recovery={'status':'COMPLETED','new_sources':0}
        self.publication_result={'status':'IDLE','model_calls':0,'batches':[]}
        self.require_lease=True

    def assert_lease_held(self):
        if not self.require_lease:return
        import msvcrt
        with open(self.state/'maintenance.lock','r+b') as probe:
            probe.seek(0)
            try:msvcrt.locking(probe.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:return
            probe.seek(0);msvcrt.locking(probe.fileno(),msvcrt.LK_UNLCK,1)
        self.fail('selected family work lost the real maintenance lease')

    @contextlib.contextmanager
    def seams(self):
        def gate(_base):
            self.trace.append('hub-gate');return dict(self.gate_result)
        def recover(_base):
            self.trace.append('intake-recovery');return dict(self.recovery)
        def drain(control,base,database,state,*,lane='legacy',**kwargs):
            self.assert_lease_held()
            self.trace.append('publication-call:'+lane);self.drain_kwargs.append(kwargs)
            if control['publication'].get('enabled') is not True:
                return {'status':'DISABLED','model_calls':0,'batches':[]}
            cache=kwargs['_hub_preflight_state']
            if 'result' not in cache:cache['result']=maintenance._global_hub_preflight(base)
            if not maintenance._valid_hub_preflight(cache['result']):
                return maintenance._deferred_hub_preflight(cache['result'])
            self.trace.append('publication-work:'+lane)
            return dict(self.publication_result)
        def pipeline(_argv):
            self.assert_lease_held()
            self.trace.append('forward');print(json.dumps({'status':'OK'}));return 0
        class Store:
            def __init__(self,*args):pass
            def cursor_seen(self,**kwargs):return True
            def close(self):pass
        def sessions(_sources):
            self.trace.append('collect-session');return SimpleNamespace(events=[],issues=[])
        def files(*args):
            self.trace.append('collect-files');return SimpleNamespace(events=[],issues=[])
        with contextlib.ExitStack() as stack:
            for patcher in (
                patch('caretaker.transports.resolve_fleet_runtime',return_value={'version_text':'synthetic','path':'synthetic','pinned':True}),
                patch('caretaker.registry.read_registry',return_value=(None,None)),
                patch('caretaker.roll_forward.recover_stale_curator_lock',return_value={'status':'NO_LOCK'}),
                patch.object(maintenance,'drain_intake_checkpoints',side_effect=recover),
                patch.object(maintenance,'_global_hub_preflight',side_effect=gate),
                patch.object(maintenance,'publication_drain',side_effect=drain),
                patch.object(maintenance,'collect_sources',side_effect=sessions),
                patch.object(maintenance,'_collect_files_source',side_effect=files),
                patch.object(maintenance,'_admit_new_epoch_events',side_effect=lambda *a,**k:self.trace.append('epoch-admit')),
                patch.object(maintenance,'Store',Store),patch('run.main',side_effect=pipeline),
            ):stack.enter_context(patcher)
            yield

    def tick(self):
        with self.seams():return maintenance.tick(self.control_path)

    def test_three_real_ticks_execute_only_the_selected_family(self):
        original=self.control_path.read_bytes()
        expected=[('legacy_publication',['publication-call:legacy','publication-work:legacy']),
                  ('new_publication',['publication-call:new','publication-work:new']),
                  ('source_intake',['collect-session','collect-files','epoch-admit','forward'])]
        for family,work in expected:
            self.trace.clear();report=self.tick()
            actual=[item for item in self.trace if item not in ('intake-recovery','hub-gate')]
            self.assertEqual(actual,work,(family,self.trace))
            self.assertEqual(report['work_stage'],'publication_family:'+family)
            self.assertEqual(self.trace[:2],['intake-recovery','hub-gate'])
            self.assertEqual(self.trace.count('hub-gate'),1)
        self.assertEqual(self.control_path.read_bytes(),original)
        self.assertEqual(json.loads((self.state/'publication_family_rotation.json').read_text())['next_slot'],0)

    def test_no_runtime_family_keeps_both_drains_and_source_pipeline(self):
        self.require_lease=False
        budget=tick_budget.TickBudget(1200,self.state)
        with self.seams(),budget.activate():
            report=maintenance._locked_tick(json.loads(json.dumps(self.control)),self.state)
        work=[x for x in self.trace if x not in ('intake-recovery','hub-gate')]
        self.assertEqual(work,['publication-call:legacy','publication-work:legacy','publication-call:new','publication-work:new',
                               'collect-session','collect-files','epoch-admit','forward'])
        self.assertNotIn('runtime_deferred',report)

    def test_each_selected_family_stops_on_incomplete_intake_before_other_work(self):
        self.recovery={'status':'UNAVAILABLE','new_sources':0}
        for slot in range(3):
            (self.state/'publication_family_rotation.json').write_text(json.dumps({'schema':1,'next_slot':slot}))
            self.trace.clear();report=self.tick()
            self.assertEqual(report['status'],'DEFERRED_INTAKE_RECOVERY')
            self.assertEqual(self.trace,['intake-recovery'])

    def test_global_hub_defer_prevents_all_selected_family_work(self):
        self.gate_result={'status':'DEFERRED_HUB_PREFLIGHT','reason':'hub_lint_failed'}
        for slot in range(3):
            (self.state/'publication_family_rotation.json').write_text(json.dumps({'schema':1,'next_slot':slot}))
            self.trace.clear();report=self.tick()
            self.assertEqual(report['status'],'DEFERRED_HUB_PREFLIGHT')
            self.assertEqual(self.trace,['intake-recovery','hub-gate'])

    def test_partial_checkpoint_recovery_still_drains_selected_lane_with_head_gate(self):
        self.recovery={'status':'PARTIAL','new_sources':0,'candidate_count':1,'results':[
            {'action':'checkpoint-intake','status':'BLOCKED_CHECKPOINT_LOCK','required':True,
             'mode':'local-commit-only','machine':'AILAB','curator_machine':'AILAB','policy_source':'git-head','proposal_id':'prop-synthetic'}]}
        with patch.object(maintenance,'_safe_partial_checkpoint_recovery',wraps=maintenance._safe_partial_checkpoint_recovery):
            report=self.tick()
        self.assertEqual([x for x in self.trace if x.startswith('publication-call:')],['publication-call:legacy'])
        self.assertTrue(self.drain_kwargs[0]['_require_committed_inputs'])
        self.assertNotIn('forward',self.trace)
        control=json.loads(self.control_path.read_text());control['new_event_epoch']['activate']=True
        self.control_path.write_text(json.dumps(control))
        (self.state/'publication_family_rotation.json').write_text(json.dumps({'schema':1,'next_slot':2}))
        self.trace.clear()
        with patch.object(maintenance,'activate_new_event_epoch') as activate:
            self.tick()
        activate.assert_not_called()
        self.assertEqual(self.trace,['intake-recovery','hub-gate'])

    def test_selected_legacy_budget_defer_never_spends_the_remainder_on_another_family(self):
        self.publication_result={'status':'DEFERRED_PREPARED','reason':'tick_time_budget_exhausted',
                                 'model_calls':2,'batches':[{'status':'DEFERRED_PREPARED'}]}
        report=self.tick()
        self.assertEqual(report['publication']['status'],'DEFERRED_PREPARED')
        self.assertEqual(report['publication']['model_calls'],2)
        self.assertEqual(report['new_lane']['publication']['status'],'DEFERRED_SCHEDULED')
        self.assertEqual([x for x in self.trace if x.startswith('publication-work:')],['publication-work:legacy'])
        self.assertNotIn('collect-files',self.trace);self.assertNotIn('forward',self.trace)
        with sqlite3.connect(self.state/'intake.sqlite') as check:
            self.assertEqual(check.execute('SELECT count(*) FROM scans').fetchone()[0],0)
            self.assertEqual(check.execute('SELECT count(*) FROM project_runs').fetchone()[0],0)
            self.assertEqual(check.execute('SELECT forwarded FROM new_epoch_events WHERE event_id=?',(self.event_id,)).fetchone()[0],0)
        check.close()

    def test_epoch_activation_is_source_only_and_after_shared_recovery_and_hub_gate(self):
        control=json.loads(self.control_path.read_text());control['new_event_epoch']['activate']=True
        self.control_path.write_text(json.dumps(control))
        with patch.object(maintenance,'activate_new_event_epoch',
                          side_effect=lambda *a:self.trace.append('activate')) as activate:
            for slot in range(3):
                self.trace.clear();self.tick()
                if slot<2:activate.assert_not_called()
                else:
                    activate.assert_called_once()
                    self.assertEqual(self.trace[:3],['intake-recovery','hub-gate','activate'])

    def test_unknown_family_rejected_before_opening_database(self):
        with patch.object(maintenance.sqlite3,'connect') as connect:
            with self.assertRaisesRegex(ValueError,'invalid_runtime_work_family'):
                maintenance._locked_tick(dict(self.control,_runtime_work_family='unsupported'),self.state)
        connect.assert_not_called()


if __name__=='__main__':unittest.main()
