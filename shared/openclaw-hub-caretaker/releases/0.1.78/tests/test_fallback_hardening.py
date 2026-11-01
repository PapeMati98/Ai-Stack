"""Regressions for empty Muse fallback config and ambient startup overrides."""
import copy,json,os,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from caretaker.config import load_config,ConfigError,TransportSpec,_muse_plugin_root
from caretaker.transports import build_run_config_inline,TransportError,_default_run
ROOT=Path(__file__).resolve().parents[1]
class FallbackHardeningTests(unittest.TestCase):
    def config(self):
        raw=json.loads((ROOT/'config.example.json').read_text(encoding='utf-8'))
        raw['fallback']['run_config_inline']['plugins']['load']={'paths':[_muse_plugin_root().as_posix()]}
        return raw
    def test_missing_config_rejected_before_execution(self):
        for inline in (None,{}):
            spec=TransportSpec(kind='openclaw_argv',executable='openclaw.cmd',argv=['--model','muse-code/muse-spark-1.3'],subscription_only=True,run_config_inline=inline)
            with self.assertRaises(TransportError):build_run_config_inline(spec,'STATE','CWD')
    def test_unsafe_fallbacks_rejected_by_loader(self):
        mutations=[lambda r:r['fallback'].pop('run_config_inline'),
                   lambda r:r['fallback']['run_config_inline']['tools']['exec'].update(mode='full'),
                   lambda r:r['fallback']['run_config_inline']['tools'].update(allow=['exec']),
                   lambda r:r['fallback']['run_config_inline']['models']['providers'].update(meta={'apiKey':'synthetic'}),
                   lambda r:r['fallback']['run_config_inline']['agents'].update(entries={'other':{}})]
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as d:
                raw=self.config();mutate(raw);p=Path(d)/'c.json';p.write_text(json.dumps(raw),encoding='utf-8')
                with self.assertRaises(ConfigError):load_config(p)
    @unittest.skipUnless(os.environ.get('HUB_CARETAKER_TEST_NATIVE_SCHEMA') == '1',
                         'native schema validation requires explicit local runtime bindings')
    def test_explicit_fallback_native_schema_validates(self):
        with tempfile.TemporaryDirectory() as d:
            native=[Path(os.environ[name]) for name in ('HUB_CARETAKER_TEST_NODE', 'HUB_CARETAKER_TEST_OPENCLAW')]
            self.assertTrue(all(p.is_absolute() and p.is_file() for p in native))
            config_path=Path(d)/'caretaker.json';config_path.write_text(json.dumps(self.config()),encoding='utf-8')
            cfg=load_config(config_path)
            root=Path(d);rendered=build_run_config_inline(cfg.fallback,str(root),str(root));p=root/'openclaw.json';p.write_text(json.dumps(rendered),encoding='utf-8')
            env={k:v for k,v in os.environ.items() if not k.startswith('OPENCLAW_')};env.update(OPENCLAW_STATE_DIR=str(root),OPENCLAW_CONFIG_PATH=str(p))
            result=subprocess.run([*(str(p) for p in native),'config','validate','--json'],capture_output=True,env=env,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'))
            self.assertEqual(rendered['tools']['deny'],['*'])
            self.assertEqual(rendered['agents']['defaults']['model']['fallbacks'],[])
    def test_child_config_and_state_ignore_ambient_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'config.json';p.write_text('{"bounded":true}',encoding='utf-8')
            result=SimpleNamespace(returncode=0,stdout=b'{}',stderr=b'')
            with patch.dict(os.environ,{'HUB_CARETAKER_ALLOW_EXEC':'1','OPENCLAW_CONFIG_PATH':'untrusted','OPENCLAW_STATE_DIR':'untrusted','OPENCLAW_HOME':'untrusted','NODE_OPTIONS':'untrusted'}), patch('caretaker.transports.subprocess.run',return_value=result) as spawn:
                _default_run(executable='openclaw.cmd',argv=['agent','exec','--config',str(p),'--state-dir',str(root)],cwd=str(root),prompt='test',timeout=1)
            kwargs=spawn.call_args.kwargs;env=kwargs['env'];argv=spawn.call_args.args[0]
            self.assertNotIn('OPENCLAW_HOME',env);self.assertNotIn('NODE_OPTIONS',env)
            self.assertEqual(argv[argv.index('--state-dir')+1],env['OPENCLAW_STATE_DIR'])
            self.assertEqual(Path(env['OPENCLAW_CONFIG_PATH']).read_text(encoding='utf-8'),'{"bounded":true}')
            self.assertTrue(Path(env['OPENCLAW_STATE_DIR']).is_relative_to(root))
    def test_only_reviewed_plugin_files_are_staged(self):
        from caretaker.transports import _stage_subscription_plugin
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);_stage_subscription_plugin(root,self.config()['fallback']['run_config_inline'])
            manifest=json.loads((ROOT/'caretaker/muse-plugin-manifest.json').read_text(encoding='utf-8'))
            actual={p.relative_to(root/'extensions/muse-code').as_posix() for p in (root/'extensions/muse-code').rglob('*') if p.is_file()}
            self.assertEqual(actual,set(manifest['file_hashes']))
            self.assertFalse(any('auth' in x or 'credential' in x for x in actual))
    def test_tampered_plugin_is_rejected_before_child_launch(self):
        from caretaker.transports import _stage_subscription_plugin
        original=Path.read_bytes
        def read(path):return b'tampered' if path.name=='backend.js' else original(path)
        with tempfile.TemporaryDirectory() as d,patch.object(Path,'read_bytes',read):
            with self.assertRaisesRegex(TransportError,'hash mismatch'):_stage_subscription_plugin(Path(d),self.config()['fallback']['run_config_inline'])
if __name__=='__main__':unittest.main()
