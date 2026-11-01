"""Regression of real maker prompt/check diagnostics. Retains all fixtures."""
import argparse, importlib.util, json, os, sys, unittest, uuid
from pathlib import Path

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

parser=argparse.ArgumentParser()
parser.add_argument('--artifact-root',required=True)
parser.add_argument('--scripts-root',default=str(Path(__file__).resolve().parent))
args,remaining=parser.parse_known_args()
ARTIFACT=Path(args.artifact_root).resolve()
SCRIPTS=Path(args.scripts_root).resolve()
ARTIFACT.mkdir(parents=True,exist_ok=True)
sys.path.insert(0,str(SCRIPTS))
spec=importlib.util.spec_from_file_location('diagnostic_bridge_under_test',Path(__file__).with_name('todo_fleet.py'))
bridge=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=bridge
spec.loader.exec_module(bridge)
fake_spec=importlib.util.spec_from_file_location('existing_bridge_test_fakes',SCRIPTS/'test_todo_fleet.py')
fakes=importlib.util.module_from_spec(fake_spec)
sys.modules[fake_spec.name]=fakes
fake_spec.loader.exec_module(fakes)
os.environ['AGENT_GRAPH_TIER_ROUTING']='0'
os.environ['AGENT_GRAPH_WATCH_EVENTS']='0'

class MakerDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.root=ARTIFACT/('case-'+uuid.uuid4().hex)
        self.out=self.root/'task/3/maker'
        self.out.mkdir(parents=True)
        self.check=self.root/'task/2/check-0'
        self.check.mkdir(parents=True)
        (self.check/'stdout.txt').write_text('',encoding='utf-8')
        (self.check/'stderr.txt').write_text('AssertionError: actual synthetic failure api_key=secret-value\n',encoding='utf-8')
        self.fleet=fakes.FakeFleet()
        candidate={'agent':'maker','model':'m-model','score':90,'reason':'synthetic test'}
        self.task={'id':'task','text':'synthetic diagnostic test','mode':'advisory','scope':['src'],
                   'done_when':['actual error transmitted'],'maker':[candidate],'reviewer':[candidate],
                   'checks':[['python','check.py','--api-key','secret-value']]}
        self.request={'goal':'synthetic only','role':'maker','workspace':str(self.root),'task':self.task,
                      'feedback':{'findings':[],'failed_tests':[{'exit_code':1,'stdout':str(self.check/'stdout.txt')}]}}
    def test_actual_empty_stdout_stderr_is_inline_redacted(self):
        got=bridge.maker_feedback(self.request,self.out,self.fleet)
        row=got['failed_tests_evidence'][0]
        self.assertIn('AssertionError: actual synthetic failure',row['stderr_evidence']['content'])
        self.assertNotIn('secret-value',json.dumps(got))
        self.assertNotIn('stdout',row)
        self.assertEqual(row['command']['argv'][-1],'[REDACTED]')
    def test_real_execute_delivers_declared_argv_and_inline_error(self):
        bridge.execute(self.request,self.out,fleet=self.fleet)
        prompt=self.fleet.execute_args[0][0]
        payload=json.loads(prompt.split('\n',1)[1])
        self.assertEqual(payload['checks'][0]['argv'][:2],['python','check.py'])
        self.assertNotIn('secret-value',json.dumps(payload))
        self.assertIn('AssertionError',payload['feedback']['failed_tests_evidence'][0]['stderr_evidence']['content'])
        self.assertNotIn('parent_verification_notes',payload)
    def test_outside_task_pointer_cannot_read_bytes(self):
        outside=self.root/'outside/stdout.txt'; outside.parent.mkdir(); outside.write_text('DO_NOT_READ')
        self.request['feedback']['failed_tests'][0]['stdout']=str(outside)
        got=bridge.maker_feedback(self.request,self.out,self.fleet)
        self.assertEqual(got['failed_tests_evidence'][0]['status'],'out-of-task-check-pointer')
        self.assertNotIn('DO_NOT_READ',json.dumps(got))
    def test_current_future_and_non_numeric_rounds_refused(self):
        for round_name in ['3','4','not-a-round']:
            self.request['feedback']['failed_tests'][0]['stdout']=str(self.root/'task'/round_name/'check-0/stdout.txt')
            got=bridge.maker_feedback(self.request,self.out,self.fleet)
            self.assertEqual(got['failed_tests_evidence'][0]['status'],'invalid-check-pointer')
    def test_check_index_outside_declared_commands_refused(self):
        self.request['feedback']['failed_tests'][0]['stdout']=str(self.root/'task/2/check-99/stdout.txt')
        got=bridge.maker_feedback(self.request,self.out,self.fleet)
        self.assertEqual(got['failed_tests_evidence'][0]['status'],'invalid-check-index')
    def test_nested_and_relative_paths_refused(self):
        for raw in ['task/2/check-0/stdout.txt',str(self.check/'nested/stdout.txt'),str(self.check/'stderr.txt')]:
            self.request['feedback']['failed_tests'][0]['stdout']=raw
            got=bridge.maker_feedback(self.request,self.out,self.fleet)
            self.assertEqual(got['failed_tests_evidence'][0]['status'],'invalid-check-pointer')
    def test_missing_valid_pointer_is_honestly_unavailable(self):
        self.request['feedback']['failed_tests'][0]['stdout']=str(self.root/'task/1/check-0/stdout.txt')
        got=bridge.maker_feedback(self.request,self.out,self.fleet)
        self.assertEqual(got['failed_tests_evidence'][0]['stdout_evidence']['status'],'unavailable')
        self.assertEqual(got['failed_tests_evidence'][0]['stderr_evidence']['content'],'')
    def test_large_stderr_is_bounded_without_removing_fixture(self):
        path=self.check/'stderr.txt'; raw=('bounded synthetic error\n'*100000)
        path.write_text(raw,encoding='utf-8')
        got=bridge.maker_feedback(self.request,self.out,self.fleet)
        self.assertEqual(got['failed_tests_evidence'][0]['stderr_evidence']['status'],'truncated')
        self.assertLess(len(json.dumps(got).encode()),bridge.MAX_REVIEW_EVIDENCE_TEXT_BYTES+2048)
        self.assertEqual(path.read_text(),raw)

if __name__=='__main__':
    unittest.main(argv=[sys.argv[0],*remaining],verbosity=2)
