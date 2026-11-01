import json, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from caretaker.transports import AgentFleetArgvTransport, TransportError, _fleet_selected_profiles

class SelectedProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.fleet=self.root/'scripts/fleet.py'
        self.fleet.parent.mkdir(); self.fleet.write_text('# synthetic')
        refs=self.root/'references'; refs.mkdir()
        self.profiles_path=refs/'profiles.json'
        self.raw={'schema_version':5,'routing_policy':{'deny':['contributor'],'data_use':'unchanged'},
                  'profiles':{'muse':{'model':'muse-spark-1.3','auth':{'native':True},'quota':{'pool':'meta'},'policy':{'training':False}},
                              'other':{'model':'other','never_probe':True},
                              'qwen-local':{'routing_enabled':False,'runtime':{'capacity':1}},
                              'qwen-local-5070ti':{'routing_enabled':False,'runtime':{'capacity':1}}}}
        self.profiles_path.write_text(json.dumps(self.raw),encoding='utf-8')
    def invoke(self,role,classification='internal',agent='muse',actual_model='muse-spark-1.3'):
        seen={}
        def runner(**kw):
            argv=kw['argv']; seen['argv']=argv
            seen['timeout']=kw['timeout']
            if '--profiles' in argv:
                seen['profiles']=json.loads(Path(argv[argv.index('--profiles')+1]).read_text())
            if '--assessment-file' in argv:
                seen['assessment']=json.loads(Path(argv[argv.index('--assessment-file')+1]).read_text())
            self.assertEqual(Path(argv[argv.index('--task-file')+1]).read_text(),'synthetic publication')
            return 0,json.dumps({'ok':True,'status':'ok','final':'{}','provider':'muse','model':actual_model}),''
        spec=SimpleNamespace(argv=['--agent',agent,'--model','muse-spark-1.3'],
                             executable='python',fleet_role=role,data_classification=classification)
        tr=AgentFleetArgvTransport(run_callable=runner,expected_identity=('muse','muse-spark-1.3'))
        with patch('caretaker.transports.resolve_fleet_py',return_value=self.fleet):
            tr.invoke(spec=spec,cwd=str(self.root),prompt='synthetic publication',
                      run_config_path='r',state_dir='s',timeout=900)
        return seen
    def test_both_publication_roles_keep_exact_profile_outer_policy_and_timeout(self):
        original=self.profiles_path.read_bytes()
        for role in ('maker','reviewer'):
            with self.subTest(role=role):
                s=self.invoke(role); argv=s['argv']
                self.assertLess(argv.index('--profiles'),argv.index('run'))
                self.assertTrue(argv[0].endswith('fleet_publication_runner.py'))
                self.assertEqual(argv[argv.index('--fleet-script')+1],str(self.fleet))
                expected={k:v for k,v in self.raw['profiles'].items() if k!='other'}
                self.assertEqual(s['profiles'],{**self.raw,'profiles':expected})
                self.assertEqual(set(s['assessment']['model_scores']),set(expected))
                self.assertEqual(s['assessment']['data_handling']['classification'],'internal')
                self.assertEqual(argv[argv.index('--model')+1],'muse-spark-1.3')
                self.assertEqual(argv.count('--timeout'),1)
                self.assertEqual(argv[argv.index('--timeout')+1],'870')
                self.assertEqual(s['timeout'],900)
        self.assertEqual(self.profiles_path.read_bytes(),original)
    def test_reviewer_without_classification_still_selects_profile(self):
        s=self.invoke('reviewer',None)
        self.assertEqual(set(s['profiles']['profiles']),{'muse','qwen-local','qwen-local-5070ti'})
        self.assertNotIn('--assessment-file',s['argv'])
    def test_legacy_nonpublication_argv_and_assessment_unchanged(self):
        s=self.invoke(None)
        self.assertNotIn('--profiles',s['argv'])
        self.assertEqual(set(s['assessment']['model_scores']),set(self.raw['profiles']))
    def test_missing_selected_profile_fails_before_runner(self):
        with self.assertRaises(TransportError) as cm:self.invoke('maker',agent='missing')
        self.assertEqual(cm.exception.reason_code,'fleet_profiles_unavailable')
    def test_invalid_profiles_fail_closed(self):
        for raw in ({}, {'profiles':[]}, {'profiles':{'muse':None}}, {'profiles':{'muse':{}}}):
            if raw=={'profiles':{'muse':{}}}:continue
            self.profiles_path.write_text(json.dumps(raw))
            with self.subTest(raw=raw),self.assertRaises(TransportError):_fleet_selected_profiles(self.fleet,'muse')
    def test_model_identity_mismatch_still_refused(self):
        with self.assertRaises(TransportError) as cm:self.invoke('maker',actual_model='wrong')
        self.assertEqual(cm.exception.reason_code,'identity_mismatch')
    def test_missing_or_enabled_local_alias_fails_closed(self):
        for enabled in (None,True):
            raw=json.loads(json.dumps(self.raw))
            if enabled is None:raw['profiles'].pop('qwen-local')
            else:raw['profiles']['qwen-local']['routing_enabled']=enabled
            self.profiles_path.write_text(json.dumps(raw))
            with self.subTest(enabled=enabled),self.assertRaises(TransportError):
                _fleet_selected_profiles(self.fleet,'muse')
    def test_invalid_data_classification_still_refused(self):
        with self.assertRaises(TransportError) as cm:self.invoke('maker',classification='restricted')
        self.assertEqual(cm.exception.reason_code,'invalid_envelope')

if __name__=='__main__':unittest.main()
