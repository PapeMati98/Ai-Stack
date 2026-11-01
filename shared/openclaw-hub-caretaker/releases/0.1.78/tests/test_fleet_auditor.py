import json,tempfile,unittest,subprocess
from pathlib import Path
from types import SimpleNamespace
from caretaker.fleet_auditor import review,FleetReviewError

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.c=self.root/'candidate';self.c.mkdir()
        (self.c/'payload').mkdir();(self.c/'payload/page.md').write_text('# Dated producer report')
        (self.c/'publication.json').write_text(json.dumps({'outputs':[{'payload_path':'payload/page.md'}]}))
        for n in ['ledger.json','source-packet.json']:(self.c/n).write_text('{}')
    def tearDown(self):self.tmp.cleanup()
    def runner(self,patch=None,mutate=False):
        def run(argv,**kwargs):
            self.assertNotIn('shell',kwargs)
            self.assertEqual(kwargs['env']['AGENT_FLEET_SUITE_ROOT'],str(self.root/'suite'))
            req=json.loads(Path(argv[argv.index('--request')+1]).read_text())
            self.assertEqual(req['role'],'reviewer');self.assertEqual(req['maker']['lineage'],'qwen')
            self.assertEqual(req['task']['reviewer'][0]['model'],'muse-spark-1.3')
            result={'status':'REVIEWED','agent':'muse','model':'muse-spark-1.3','lineage':'meta','run_id':'run-test',
                    'verdict':'ACCEPT','findings':[],'checked':req['task']['done_when']}
            result.update(patch or {})
            Path(argv[argv.index('--output')+1]).write_text(json.dumps(result))
            if mutate:(self.c/'payload/page.md').write_text('changed after review')
            return SimpleNamespace(returncode=0)
        return run
    def call(self,runner):return review(self.c,python='python',bridge=self.root/'bridge.py',suite=self.root/'suite',runner=runner)
    def test_accept_binds_exact_bytes_and_actual_fleet_identity(self):
        path,result=self.call(self.runner());d=json.loads(path.read_text())
        self.assertEqual(d['verdict'],'PASS');self.assertEqual(d['reviewer']['lineage'],'meta')
        e=json.loads((self.c/'semantic-review.json').read_text());self.assertEqual(e['transport'],'agent-cli/todo_fleet')
        self.assertEqual(e['manifest_sha256'],d['manifest_sha256'])
    def test_same_family_rejected(self):
        with self.assertRaisesRegex(FleetReviewError,'identity'):self.call(self.runner({'lineage':'qwen'}))
    def test_contributor_rejected(self):
        with self.assertRaisesRegex(FleetReviewError,'identity'):self.call(self.runner({'model':'muse-spark-contributor'}))
    def test_changed_payload_rejected(self):
        with self.assertRaisesRegex(FleetReviewError,'changed'):self.call(self.runner(mutate=True))
    def test_incomplete_checklist_rejected(self):
        with self.assertRaisesRegex(FleetReviewError,'incomplete'):self.call(self.runner({'checked':[]}))
    def test_findings_never_pass(self):
        with self.assertRaisesRegex(FleetReviewError,'requires_repair') as cm:self.call(self.runner({'verdict':'REPAIR','findings':['Retain dated unresolved claim']}))
        self.assertEqual(cm.exception.findings,['Retain dated unresolved claim'])
        self.assertFalse((self.c/'review.json').exists())
    def test_unavailable_defers_without_changing_candidate(self):
        before=(self.c/'payload/page.md').read_bytes()
        def fail(*a,**k):raise subprocess.TimeoutExpired('test',420)
        with self.assertRaisesRegex(FleetReviewError,'unavailable'):self.call(fail)
        self.assertEqual(before,(self.c/'payload/page.md').read_bytes());self.assertFalse((self.c/'review.json').exists())
    def test_review_attempt_cannot_be_overwritten(self):
        self.call(self.runner())
        with self.assertRaisesRegex(FleetReviewError,'already_exists'):self.call(self.runner())
    def test_path_escape_never_calls_model(self):
        (self.c/'publication.json').write_text(json.dumps({'outputs':[{'payload_path':'../outside'}]}))
        with self.assertRaisesRegex(FleetReviewError,'unsafe'):self.call(lambda *a,**k:self.fail('model should not run'))

if __name__=='__main__':unittest.main()
