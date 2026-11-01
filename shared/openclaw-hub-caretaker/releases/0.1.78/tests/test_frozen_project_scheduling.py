"""Real global audit/project tick/model boundary on small synthetic frozen cycles."""
import json
import copy
import hashlib
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import maintenance
from caretaker.audit import _open_cycle, policy_from, summarize
from caretaker.pubstate import batch_key
from tests import test_publication_entry_recovery as entry
from tests import test_repaired_batch_retry as retry
from tests import test_safe_partial_checkpoint_recovery as partial


class FrozenProjectSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.case=entry.PublicationEntryRoutingTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.db,self.vault,self.state=self.case.db,self.case.vault,self.case.state
        self.base,self.control=self.case.base,self.case.control

    def add_project(self,project):
        path=self.vault/'proposals/agent'/f'{project}.md'
        path.write_text(f'---\nid: prop-{project}\nclassification: internal\nproject_slug: {project}\n'
                        f'source_ids: ["src-one"]\ntarget_pages: ["wiki/projects/{project}.md"]\n---\nSynthetic source receipt.\n',encoding='utf-8')
        (self.vault/'wiki/projects'/f'{project}.md').write_text(f'---\nstatus: reviewed\nproject_slug: {project}\n---\n# Synthetic page\n',encoding='utf-8')

    def seed(self,frozen,extras=(),*,rotation=0,cap=3,batches=3,lane='legacy',blocked=()):
        for project in [*frozen,*extras]:self.add_project(project)
        self.control['publication'].update(projects=[*frozen,*extras],max_batches_per_tick=batches)
        self.control['max_projects_per_tick']=cap
        summary=summarize(self.vault,policy_from(self.control['publication']))
        members=[m for m in summary['eligible'] if m['project'] in frozen]
        self.assertEqual({m['project'] for m in members},set(frozen))
        cycle=_open_cycle(self.db,summary,members,retry.NOW.isoformat(),lane,8)
        table='new_publication_rotation' if lane=='new' else 'publication_rotation'
        self.db.execute(f'CREATE TABLE IF NOT EXISTS {table}(id INTEGER PRIMARY KEY,value INTEGER NOT NULL)')
        self.db.execute(f'INSERT OR REPLACE INTO {table} VALUES(1,?)',(rotation,))
        for member in members:
            if member['project'] in blocked:
                key=batch_key(member['project'],[member['path']])
                self.db.execute('INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint) VALUES(?,?,?,?,?,?,?,?)',
                                (key,'BLOCKED',0,'{}',0,'2099-01-01T00:00:00+00:00','transient','fixture'))
        self.db.commit();return cycle

    def drain(self,lane='legacy'):
        with patch.object(maintenance,'_new_epoch_proposal_filter',return_value=lambda m:True):
            return maintenance.publication_drain(self.control,self.base,self.db,self.state,now=retry.NOW,lane=lane,
                recovery={'status':'NO_LOCK'},_hub_preflight_state=self.case.preflight)

    def test_no_pending_extras_cannot_fill_all_slots_before_actionable_frozen_work(self):
        for lane in ('legacy','new'):
            if lane=='new':
                self.case.doCleanups();self.setUp()
            with self.subTest(lane=lane):
                self.seed(['frozen-a','frozen-b'],[f'future-{n:02d}' for n in range(12)],
                          rotation=1331,cap=3,batches=3,lane=lane,blocked=['frozen-b'])
                reached=[]
                def invoke(config,paths):
                    from caretaker.config import load_config
                    reached.append(load_config(config).project_slug)
                    return {'status':'PUBLISHED','model_calls':1}
                with patch.object(maintenance,'publication_tick',wraps=maintenance.publication_tick) as tick, \
                     patch.object(maintenance,'_invoke_curate',side_effect=invoke):
                    result=self.drain(lane)
                self.assertEqual(reached,['frozen-a'],result)
                visited=[call.args[1]['project_slug'] for call in tick.call_args_list]
                self.assertEqual(set(visited[:2]),{'frozen-a','frozen-b'})
                self.assertLessEqual(sum(p.startswith('future-') for p in visited),1)
                self.assertLessEqual(len(visited),3)
                self.assertEqual(result['model_calls'],1)

    def test_ordinary_frozen_rotation_is_retained_without_speculative_projects(self):
        projects=['frozen-a','frozen-b','frozen-c']
        self.seed(projects,cap=1,batches=1,blocked=projects)
        visited=[]
        with patch.object(maintenance,'publication_tick',wraps=maintenance.publication_tick) as tick, \
             patch.object(maintenance,'_invoke_curate') as model:
            for _ in range(3):self.drain()
            visited=[call.args[1]['project_slug'] for call in tick.call_args_list]
        self.assertEqual(visited,projects);model.assert_not_called()

    def test_single_slot_alternates_frozen_and_closed_repair_discovery(self):
        old=json.loads(self.db.execute('SELECT result FROM publications WHERE batch=?',(self.case.old_key,)).fetchone()[0])
        old.update(reason='citation_outside_publication',original_reason='citation_outside_publication',
                   error_code='citation_outside_publication',review_repair_exhausted=True,
                   format_repair_exhausted=False,candidate_path=None,maker_calls=2,review_calls=1)
        self.db.execute('UPDATE publications SET result=? WHERE batch=?',(json.dumps(old),self.case.old_key))
        self.seed(['alpha'],cap=1,batches=1,blocked=['alpha'])
        self.control['publication']['projects']=['alpha','test']
        source_row=self.db.execute('SELECT * FROM publications WHERE batch=?',(self.case.old_key,)).fetchone()
        reached=[]
        def invoke(config,paths):
            from caretaker.config import load_config
            self.assertEqual(load_config(config).project_slug,'test')
            self.assertEqual(self.db.execute('SELECT count(*) FROM repaired_retry_tokens').fetchone()[0],1)
            reached.append(list(paths));return {'status':'PUBLISHED','model_calls':1}
        with patch('caretaker.draft_summary.inventory_history_repair_plan',return_value={}), \
             patch.object(maintenance,'publication_tick',wraps=maintenance.publication_tick) as tick, \
             patch.object(maintenance,'_invoke_curate',side_effect=invoke):
            first=self.drain();second=self.drain()
            visited=[call.args[1]['project_slug'] for call in tick.call_args_list]
        self.assertEqual(visited,['alpha','test'])
        self.assertEqual(reached,[old['paths']])
        self.assertEqual(first['model_calls']+second['model_calls'],1)
        self.assertEqual(self.db.execute('SELECT * FROM publications WHERE batch=?',(self.case.old_key,)).fetchone(),source_row)

    def test_qualified_prepared_priority_stays_before_both_rotated_pools(self):
        self.seed(['frozen-a','frozen-b'],['future-one'],rotation=1,cap=1,batches=1,blocked=['frozen-a','frozen-b'])
        priority='PREPARED|'+'a'*64+'|1|0'
        with patch.object(maintenance,'_prepared_priority_candidate',return_value=('frozen-b',priority)) as prepared, \
             patch.object(maintenance,'publication_tick',wraps=maintenance.publication_tick) as tick, \
             patch.object(maintenance,'_invoke_curate') as model:
            self.drain()
        self.assertEqual(tick.call_args_list[0].args[1]['project_slug'],'frozen-b')
        self.assertEqual(tick.call_args_list[0].kwargs['prepare_priority_seen_key'],priority)
        self.assertEqual(set(prepared.call_args.args[2]),{'frozen-a','frozen-b','future-one'})
        model.assert_not_called()

    def test_single_slot_even_sized_pools_each_advance_on_their_own_turn(self):
        frozen=['frozen-a','frozen-b'];extras=[f'future-{n}' for n in range(4)]
        self.seed(frozen,extras,cap=2,batches=1,blocked=frozen)
        with patch.object(maintenance,'publication_tick',wraps=maintenance.publication_tick) as tick, \
             patch.object(maintenance,'_invoke_curate') as model:
            for _ in range(8):
                before=len(tick.call_args_list);self.drain()
                self.assertEqual(len(tick.call_args_list)-before,1)
            visited=[call.args[1]['project_slug'] for call in tick.call_args_list]
        self.assertEqual(visited,['frozen-a','future-0','frozen-b','future-1','frozen-a','future-2','frozen-b','future-3'])
        model.assert_not_called()


class ConcurrentPathChangePartialTests(unittest.TestCase):
    def setUp(self):
        self.case=partial.SafePartialCheckpointRecoveryTests();self.case.setUp();self.addCleanup(self.case.doCleanups)

    def recovery(self):
        value=partial.partial(only_busy=True)
        value['results'][0]['status']='BLOCKED_CONCURRENT_PATH_CHANGE'
        return value

    def test_committed_unrelated_inputs_reach_model_boundary_with_real_head_proof(self):
        case=self.case;recovery=self.recovery()
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        cfg=dict(case.base,project_slug='fixture-project')
        with case.batch_environment(db),patch('caretaker.pubstate.record_start') as start, \
             patch.object(maintenance,'_invoke_curate',side_effect=partial.StopBeforeModel) as invoke:
            def drain(*args,**kwargs):
                self.assertTrue(kwargs['_require_committed_inputs'])
                return maintenance.publication_tick('fixture-config',cfg,case.control,db,
                    _require_committed_inputs=True,_hub_preflight_state={'result':case.passed})
            with case.tick_environment(recovery),patch.object(maintenance,'publication_drain',side_effect=drain):
                with self.assertRaises(partial.StopBeforeModel):
                    maintenance._locked_tick(copy.deepcopy(case.control),case.state)
            start.assert_called_once();invoke.assert_called_once()
        self.assertEqual(recovery,self.recovery(),'blocked pair remains unchanged for engine-owned retry')

    def test_changed_selected_proposal_or_dependency_stops_before_start_or_model(self):
        case=self.case
        for index in (0,2):
            with self.subTest(input_index=index):
                db=sqlite3.connect(':memory:');self.addCleanup(db.close)
                cfg=dict(case.base,project_slug='fixture-project')
                with case.batch_environment(db) as frozen,patch('caretaker.pubstate.record_start') as start, \
                     patch.object(maintenance,'_invoke_curate') as invoke:
                    original=dict(frozen['inputs'][index]);path=case.vault/original['path'];before=path.read_bytes()
                    changed=b'uncommitted selected synthetic input';path.write_bytes(changed)
                    frozen['inputs'][index]=dict(path=original['path'],bytes=len(changed),sha256=hashlib.sha256(changed).hexdigest())
                    def drain(*args,**kwargs):
                        self.assertTrue(kwargs['_require_committed_inputs'])
                        return maintenance.publication_tick('fixture-config',cfg,case.control,db,
                            _require_committed_inputs=True,_hub_preflight_state={'result':case.passed})
                    with case.tick_environment(self.recovery()),patch.object(maintenance,'publication_drain',side_effect=drain):
                        result=maintenance._locked_tick(copy.deepcopy(case.control),case.state)
                    self.assertEqual(result['publication']['reason'],'selected_inputs_not_in_head')
                    start.assert_not_called();invoke.assert_not_called()
                    path.write_bytes(before);frozen['inputs'][index]=original

    def test_foreign_role_wrong_policy_and_nonsafe_shapes_do_not_pass_recovery(self):
        mutations=[('curator_machine','OTHER'),('policy_source','working-tree'),('status','BLOCKED_ROLE'),
                   ('status','UNKNOWN'),('status','BLOCKED_OTHER'),('dirty_tree_state','dirty'),
                   ('lint_error_count',1),('commit','a'*40)]
        for key,value in mutations:
            with self.subTest(key=key,value=value):
                recovery=self.recovery();recovery['results'][0][key]=value
                with self.case.tick_environment(recovery),patch.object(maintenance,'publication_drain') as drain:
                    report=maintenance._locked_tick(copy.deepcopy(self.case.control),self.case.state)
                self.assertEqual(report['status'],'DEFERRED_INTAKE_RECOVERY');drain.assert_not_called()
        recovery=self.recovery();recovery['new_sources']=1
        self.assertFalse(maintenance._safe_partial_checkpoint_recovery(recovery))
        recovery=self.recovery();recovery['results'].append({'status':'BLOCKED_CONCURRENT_PATH_CHANGE'})
        self.assertFalse(maintenance._safe_partial_checkpoint_recovery(recovery))

    def test_concurrent_pair_never_enables_source_collection_or_bypasses_hub_gate(self):
        case=self.case
        with case.tick_environment(self.recovery()),patch.object(maintenance,'_global_hub_preflight',return_value=case.passed), \
             patch.object(maintenance,'publication_drain',return_value={'status':'IDLE','model_calls':0}) as drain, \
             patch('caretaker.backlog.pending_projects',side_effect=AssertionError('ingest touched')), \
             patch.object(maintenance,'collect_sources',side_effect=AssertionError('source collection touched')):
            report=maintenance._locked_tick(copy.deepcopy(case.control),case.state)
        self.assertEqual(report['projects'],[]);self.assertEqual(report['new_events'],0)
        self.assertTrue(drain.call_args.kwargs['_require_committed_inputs'])
        for reason in ('hub_lint_failed','hub_health_failed','hub_role_blocked'):
            with case.tick_environment(self.recovery()),patch.object(maintenance,'_global_hub_preflight',
                    return_value={'status':'DEFERRED_HUB_PREFLIGHT','reason':reason}), \
                 patch.object(maintenance,'_invoke_curate') as model:
                report=maintenance._locked_tick(copy.deepcopy(case.control),case.state)
            self.assertEqual(report['status'],'DEFERRED_HUB_PREFLIGHT');model.assert_not_called()


if __name__=='__main__':unittest.main()
