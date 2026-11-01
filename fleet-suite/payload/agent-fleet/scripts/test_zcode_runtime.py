"""Offline synthetic transport/identity/profile checks; no credentials or inference."""
import copy,json,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import fleet
import zcode_runtime as z

def profile(): return json.loads(fleet.DEFAULT_PROFILES.read_text(encoding='utf-8'))['profiles']['zcode']

class NativeBoundaryTests(unittest.TestCase):
    def test_profile_load_and_shared_pool(self):
        p=fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.assertEqual(p['zcode']['quota_pool_id'],p['opencode']['quota_pool_id'])
        self.assertFalse(p['opencode']['routing_enabled'])
        self.assertEqual(p['zcode']['supported_modes'],['advisory','review'])
        self.assertIn('zcode',fleet.DEFAULT_DENY_NATIVE_SUBAGENT_AGENTS)

    def test_pinned_profile_rejects_drift(self):
        for field,value in [('api_fallback',True),('coding_plan_only',False),('cli_sha256','0'*64),('native_version','0.17.0'),('tools','all')]:
            with self.subTest(field=field):
                p=profile();p['zcode'][field]=value
                with self.assertRaises(z.ZCodeRuntimeError): z.validate_zcode_profile(p)

    def test_no_generic_credential_or_alias(self):
        for key,value in [('credential',{}),('local_runtime',{}),('candidates',['zcode'])]:
            p=profile();p[key]=value
            with self.assertRaises(z.ZCodeRuntimeError): z.validate_zcode_profile(p)

    def test_activation_requires_qualification(self):
        p=profile();p['routing_enabled']=True;p['qualification']={'status':'pending'}
        with self.assertRaises(z.ZCodeRuntimeError): z.validate_zcode_profile(p)

    def test_exact_model_gate(self):
        for model in ['glm-5.3','GLM-5.3-Flash','auto','glm5.3',None]:
            with self.subTest(model=model),self.assertRaises(z.ZCodeRuntimeError): z.require_model(model)

    def test_missing_tool_limits_fail_closed(self):
        for cap in [None,0,-1,True,'3',1001]:
            with self.subTest(cap=cap),self.assertRaises(z.ZCodeRuntimeError): z.bind_tool_policy('synthetic',workspace=Path('.'),mode='review',max_tool_calls=cap)

    def test_implementation_not_claimed(self):
        with self.assertRaises(z.ZCodeRuntimeError): z.bind_tool_policy('synthetic',workspace=Path('.'),mode='implement',max_tool_calls=3)

    def test_prompt_json_does_not_create_dispatch(self):
        text=json.dumps({'scope':[],'checks':[]})*2
        self.assertIsNone(z.bind_tool_policy(text,workspace=Path('.'),mode='advisory',max_tool_calls=3))

    def test_prompt_only_has_no_tools(self):
        self.assertIsNone(z.bind_tool_policy('synthetic',workspace=Path('.'),mode='advisory',max_tool_calls=3))

    def test_no_parent_credentials_in_commandline(self):
        command=z.build_command(z.NODE,model=z.MODEL,mode='review',run_dir=Path('synthetic'),timeout=60)
        self.assertNotIn('--prompt',command)
        self.assertNotIn('secret',' '.join(command))

    def test_inference_budget_tracks_admitted_tool_cap(self):
        with TemporaryDirectory() as temp:
            base=Path(temp)
            for cap in (1,16,32,1000):
                run=base/str(cap)
                run.mkdir()
                z.prepare_run(run,{'files':['synthetic.txt']},cap)
                boundary=json.loads((run/'zcode-boundary.json').read_text(encoding='utf-8'))
                self.assertEqual(boundary['max_requests'],cap+1)
                self.assertEqual(boundary['max_tool_calls'],cap)
                self.assertEqual(boundary['transport_schema_version'],z.TRANSPORT_SCHEMA_VERSION)
            free=base/'tool-free';free.mkdir()
            z.prepare_run(free,None,32)
            self.assertEqual(json.loads((free/'zcode-boundary.json').read_text(encoding='utf-8'))['max_requests'],1)

    def test_configured_or_echoed_model_is_not_identity(self):
        for e in [{},{'model':'GLM-5.3'},{'observed_models':['GLM-5.3']},{'boundary_verified':True,'observed_models':['GLM-5.3-Flash']},{'boundary_verified':True,'observed_models':[]}]:
            self.assertEqual(z.model_identity(e)['status'],'unverified')

    def test_served_identity(self):
        self.assertEqual(z.model_identity({'boundary_verified':True,'observed_models':['GLM-5.3']})['status'],'verified')

    def test_usage_unknown_not_zero(self):
        for e in [{},{'usage':{}},{'usage':{'source':'provider','inputTokens':False,'outputTokens':0,'totalTokens':0}}]:
            self.assertEqual(z.token_usage(e)['status'],'unknown')

    def test_provider_usage_is_subscription_telemetry(self):
        e={'usage':{'source':'provider','inputTokens':100,'outputTokens':2,'totalTokens':102}}
        self.assertEqual(z.token_usage(e)['total_tokens'],102)
        self.assertIsNone(z.token_usage(e)['cost_usd'])

    def test_native_terminal_handoff_projection(self):
        handoff={'status':'completed','summary':'Synthetic','files_changed':[],'verification':['synthetic'],'risks':[],'next_steps':[]}
        text=json.dumps([{'type':'message','content':[{'type':'text','text':json.dumps(handoff)}]}])
        result=fleet.project_provider_handoff('zcode',text)
        self.assertEqual(result['handoff']['status'],'completed')
        self.assertEqual(result['source'],'zcode.native-terminal')

    def test_version_probe_only(self):
        p=profile();self.assertEqual(p['version_args'],[z.CLI,'--version']);self.assertEqual(p['deep_probe_args'],[z.CLI,'--version'])

if __name__=='__main__': unittest.main()
