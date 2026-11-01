"""One focused synthetic global-entry regression; never a live model/API call."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import maintenance,curate_once
from caretaker import tick_budget
from caretaker.pubstate import read_row
from caretaker.transports import Envelope
from tests import test_prespawn206_recovery as fixture
from tests.test_curate_fleet_reviewer import _HashPassReviewer


class BudgetContinuationTests(fixture.PreSpawn206Tests):
    def _exercise_budget(self, *, after_pass=False, reject=None):
        calls=[];reviewer=_HashPassReviewer(provider='grok',model='grok-4')
        original_timeout=curate_once._phase_timeout
        defer=[True]
        class Maker:
            def invoke(inner,**kwargs):
                calls.append(kwargs['cwd'])
                decision={'body':'# Current source note\nCited synthetic evidence (src-one).\n',
                          'summary':'Current synthetic source evidence.',
                          'dispositions':{self.identity[0]:{'disposition':'synthesized','reason':'Cited src-one.'}}}
                return Envelope(ok=True,status='ok',provider='kimi',model='k3',session_id='maker-budget',final=json.dumps(decision),raw={})
        def timeout(*args,**kwargs):
            phase=json.loads(self.pointer.read_text()).get('phase')
            if defer[0] and not after_pass and phase=='REPAIR_PREPARED':
                defer[0]=False
                raise tick_budget.TickBudgetExceeded('tick_time_budget_exhausted')
            return original_timeout(*args,**kwargs)
        def publish(cfg,paths,root,result):
            self.assertEqual(root,self.generation)
            self.assertEqual(result['status'],'REVIEWED_CANDIDATE')
            self.assertEqual(json.loads(Path(result['review']).read_text())['verdict'],'PASS')
            if defer[0] and after_pass:
                defer[0]=False
                raise tick_budget.TickBudgetExceeded('tick_time_budget_exhausted')
            result.update(status='PUBLISHED',publication={'status':'PUBLISHED','event_id':'synthetic-budget'},checkpoint={'status':'CHECKPOINTED'})
        policy=dict(self.cfg.raw['publication'],enabled=True,projects=['test'],include_existing_hub_backlog=True,min_ready_groups=1,max_batches_per_tick=1)
        control={'publication':policy,'known_projects':[],'max_projects_per_tick':1}
        base=fixture.fixture._base(self.root,self.vault);base['project_slug']='unrelated-base'
        state=Path(self.cfg.raw['maintenance_state_dir'])
        preflight={'result':{'status':'PASS','health_status':'pass','lint_status':'pass'}}
        with patch.object(curate_once,'CanonicalKnowledgeGateway') as gateway, \
             patch.object(curate_once,'_default_fleet_transport',side_effect=lambda agent,model: Maker() if agent=='kimi' else reviewer), \
             patch.object(curate_once,'_publish_retained',side_effect=publish), \
             patch.object(curate_once,'_phase_timeout',side_effect=timeout):
            gateway.return_value.health.return_value=SimpleNamespace(status='pass')
            gateway.return_value.lint.return_value=SimpleNamespace(status='pass')
            first=maintenance.publication_drain(control,base,self.db,state,lane='legacy',recovery={'status':'NO_LOCK'},_hub_preflight_state=preflight)
            self.assertEqual(first['batches'][0]['status'],'DEFERRED_PREPARED',first)
            self.assertEqual(json.loads(self.pointer.read_text())['phase'],'REVIEW_ACCEPTED' if after_pass else 'REPAIR_PREPARED')
            self.assertEqual(len(calls),1);self.assertEqual(len(reviewer.calls),1 if after_pass else 0)
            self.assertEqual(read_row(self.db,self.batch)['status'],'UNKNOWN_REQUIRES_RECONCILIATION')
            intent=(self.generation/'transport-recovery206.json').read_bytes()
            first_result=(self.generation/'transport-recovery206-result.json').read_bytes()
            if reject in ('true_unknown','native_timeout'):
                capsule=json.loads(first_result)
                capsule.update(status='UNKNOWN_REQUIRES_RECONCILIATION',reason='native_timeout' if reject=='native_timeout' else 'runtime_outcome_unknown')
                (self.generation/'transport-recovery206-result.json').write_text(json.dumps(capsule))
            elif reject=='changed_source':
                source=self.vault/self.paths[0];source.write_bytes(source.read_bytes()+b'changed')
            elif reject=='changed_phase':
                doc=json.loads(self.pointer.read_text());doc['phase']='REPAIR_STARTED';doc['progress']['phase']='REPAIR_STARTED'
                doc['cache_sha256']=curate_once._phase_digest(doc);self.pointer.write_text(json.dumps(doc))
            elif reject=='effects':
                doc=json.loads(self.pointer.read_text());candidate=self.phase_state/doc['candidate']
                (candidate/'.curation-transactions').mkdir()
            elif reject=='active_process':
                self.active.return_value=True
            second=maintenance.publication_drain(control,base,self.db,state,lane='legacy',recovery={'status':'NO_LOCK'},_hub_preflight_state=preflight)
            if reject:
                self.assertFalse(any(x['status']=='PUBLISHED' for x in second.get('batches',[])),second)
                self.assertEqual(len(calls),1);self.assertEqual(len(reviewer.calls),1 if after_pass else 0)
                self.assertEqual(read_row(self.db,self.batch),self.row)
                self.assertEqual(list(self.generation.glob('transport-budget-*.intent.json')),[])
                return
            self.assertTrue(second.get('batches') and second['batches'][0]['status']=='PUBLISHED',second)
            third=maintenance.publication_drain(control,base,self.db,state,lane='legacy',recovery={'status':'NO_LOCK'},_hub_preflight_state=preflight)
        self.assertEqual(calls,[str(self.generation)])
        self.assertEqual(len(reviewer.calls),1)
        self.assertEqual((self.generation/'transport-recovery206.json').read_bytes(),intent)
        self.assertEqual((self.generation/'transport-recovery206-result.json').read_bytes(),first_result)
        self.assertEqual(read_row(self.db,self.batch)['status'],'PUBLISHED')

    def test_budget_defer_then_normal_drain_reviews_without_second_maker(self):
        self._exercise_budget()

    def test_budget_after_fresh_pass_continues_publish_without_second_maker(self):
        self._exercise_budget(after_pass=True)

    def test_budget_rejects_true_unknown(self):
        self._exercise_budget(reject='true_unknown')

    def test_budget_rejects_native_timeout(self):
        self._exercise_budget(reject='native_timeout')

    def test_budget_rejects_changed_source(self):
        self._exercise_budget(reject='changed_source')

    def test_budget_rejects_changed_phase(self):
        self._exercise_budget(reject='changed_phase')

    def test_budget_rejects_effects(self):
        self._exercise_budget(reject='effects')

    def test_budget_rejects_active_process(self):
        self._exercise_budget(reject='active_process')


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(BudgetContinuationTests(name) for name in BudgetContinuationTests.__dict__ if name.startswith('test_budget_'))
