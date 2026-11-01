import json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import curate_once as curator
import maintenance
from caretaker.publication import PublicationError,sha

class ResumeTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'.test-tmp'; root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=root); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.state=self.root/'state'; self.vault=self.root/'vault'
        self.state.mkdir(); self.vault.mkdir(); self.curation=self.state/'curation'; self.curation.mkdir()
        self.run=self.curation/'run-a'; self.run.mkdir(); self.candidate=self.run/'candidate'; self.candidate.mkdir()
        def put(name,value):
            p=self.candidate/name; p.write_text(json.dumps(value)); return p
        self.frozen={'project_slug':'p','target':'wiki/a.md','preimage_sha256':None,
            'snapshot_sha256':'snapshot-a','inputs':[],
            'proposals':[{'id':'prop-a','path':'proposals/a.md','sha256':'a'*64,'source_ids':[]}]}
        self.manifest=put('publication.json',{'proposal_ids':['prop-a'],'inputs':[],
            'snapshot_sha256':'snapshot-a','outputs':[{'path':'wiki/a.md','preimage_sha256':None,'payload_path':'page.md'}],'ledger_path':'ledger.json'})
        put('ledger.json',{}); put('page.md',{}); evidence=put('evidence.json',{'provider':'synthetic'})
        self.review=put('review.json',{'verdict':'PASS','manifest_sha256':sha(self.manifest.read_bytes()),
            'reviewer':{'lineage':'muse'},'evidence':{'path':'evidence.json','sha256':sha(evidence.read_bytes())}})
        self.cfg=SimpleNamespace(state_dir=self.state,project_slug='p',
            hub=SimpleNamespace(vault=self.vault,python_executable='python',script=self.root/'hub.py'))
        self.result={'status':'REVIEWED_CANDIDATE','manifest':str(self.manifest),
            'manifest_sha256':sha(self.manifest.read_bytes()),'review':str(self.review),
            'maker_lineage':'qwen'}
        curator._save_reviewed_candidate(self.cfg,['proposals/a.md'],self.run,self.result)
        self.accounted=mock.patch.object(curator,'accounted_proposal_ids',return_value=set())
        self.accounted.start(); self.addCleanup(self.accounted.stop)

    def test_transient_retry_reuses_exact_review_without_models(self):
        gateway=SimpleNamespace(health=lambda:SimpleNamespace(status='pass'),lint=lambda:SimpleNamespace(status='pass'))
        with mock.patch.object(curator,'CanonicalKnowledgeGateway',return_value=gateway), \
             mock.patch('caretaker.registry.read_registry',return_value=(None,None)), \
             mock.patch('caretaker.registry.main_page',return_value='wiki/a.md'), \
             mock.patch.object(curator,'freeze',return_value=self.frozen), \
             mock.patch.object(curator,'_wire_fleet_publication',side_effect=AssertionError('must not recall models')), \
             mock.patch.object(curator,'_publish_under_lock',side_effect=PublicationError('curator_lock_held')):
            first=curator._run_with_config(self.cfg,['proposals/a.md'],True,None,None)
        self.assertTrue(first['retained_for_retry']); self.assertEqual(first['model_calls'],0)
        def publish(cfg,manifest,review,run,result):
            self.assertEqual(manifest,self.manifest); self.assertEqual(review,self.review)
            result.update(status='PUBLISHED',checkpoint={'status':'CHECKPOINTED'})
        with mock.patch.object(curator,'CanonicalKnowledgeGateway',return_value=gateway), \
             mock.patch('caretaker.registry.read_registry',return_value=(None,None)), \
             mock.patch('caretaker.registry.main_page',return_value='wiki/a.md'), \
             mock.patch.object(curator,'freeze',return_value=self.frozen), \
             mock.patch.object(curator,'_wire_fleet_publication',side_effect=AssertionError('must not recall models')), \
             mock.patch.object(curator,'_publish_under_lock',side_effect=publish):
            second=curator._run_with_config(self.cfg,['proposals/a.md'],True,None,None)
        self.assertEqual(second['status'],'PUBLISHED'); self.assertEqual(second['model_calls'],0)

    def test_changed_evidence_cannot_resume(self):
        (self.candidate/'evidence.json').write_text('{}')
        with self.assertRaisesRegex(PublicationError,'artifact_changed'):
            curator._load_reviewed_candidate(self.cfg,['proposals/a.md'],self.curation)

    def test_changed_preimage_cannot_resume(self):
        (self.vault/'wiki').mkdir(); (self.vault/'wiki/a.md').write_text('foreign change')
        with self.assertRaisesRegex(PublicationError,'stale_requires_fresh_review'):
            curator._load_reviewed_candidate(self.cfg,['proposals/a.md'],self.curation)

    def test_applying_journal_requires_existing_reconciliation(self):
        tx=self.candidate/'.curation-transactions'; tx.mkdir()
        (tx/'tx-a.json').write_text(json.dumps({'status':'applying'}))
        with self.assertRaisesRegex(PublicationError,'journal_requires_reconciliation'):
            curator._load_reviewed_candidate(self.cfg,['proposals/a.md'],self.curation)

    def test_already_accounted_cannot_republish(self):
        with mock.patch.object(curator,'accounted_proposal_ids',return_value={'prop-a'}):
            with self.assertRaisesRegex(PublicationError,'accounted_elsewhere'):
                curator._load_reviewed_candidate(self.cfg,['proposals/a.md'],self.curation)

class DrainTests(unittest.TestCase):
    def drain(self,results,held=None,projects=None):
        import caretaker.audit as audit
        db=sqlite3.connect(':memory:'); self.addCleanup(db.close)
        cycle={'id':'cycle','members':[]}; projects=projects or ['p']
        control={'max_projects_per_tick':2,'publication':{'enabled':True,'max_batches_per_tick':3,
                  'include_existing_hub_backlog':True,'held_projects':held or []}}
        calls=[]
        def tick(config,cfg,*args,**kwargs):
            calls.append(cfg['project_slug']); return results[min(len(calls)-1,len(results)-1)]
        with mock.patch.object(maintenance,'_audit_summary',return_value={}), \
             mock.patch.object(maintenance,'_audit_fields',return_value={}), \
             mock.patch.object(maintenance,'complete_pending_checkpoints',return_value=[]), \
             mock.patch.object(audit,'maybe_start_cycle',return_value=cycle), \
             mock.patch.object(audit,'cycle_projects',return_value=projects), \
             mock.patch.object(maintenance,'_project_runtime_cfg',side_effect=lambda b,p,s:('config',{'project_slug':p})), \
             mock.patch.object(maintenance,'publication_tick',side_effect=tick), \
             mock.patch('caretaker.pubstate.reconcile_all_running'):
            result=maintenance.publication_drain(control,{'hub':{'vault':'synthetic'}},db,Path('synthetic'),recovery={'status':'NO_LOCK'})
        return calls,result

    def test_three_batches_same_project_and_held_scope(self):
        calls,result=self.drain([{'status':'PUBLISHED','model_calls':1}],['z-owned'],['z-owned','p'])
        self.assertEqual(calls,['p']*3); self.assertEqual(result['model_calls'],3)

    def test_no_model_resume_still_consumes_bounded_budget(self):
        calls,result=self.drain([{'status':'PUBLISHED','model_calls':0}])
        self.assertEqual(len(calls),3); self.assertEqual(result['model_calls'],0)

    def test_transient_does_not_spin_same_project(self):
        calls,result=self.drain([{'status':'REVIEWED_CANDIDATE','model_calls':0}])
        self.assertEqual(calls,['p'])

if __name__=='__main__':unittest.main()
