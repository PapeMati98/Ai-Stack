"""Numeric synthetic Fleet integration, retained artifacts, no provider launches."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import fleet
import routing_v2
import antigravity_context
from process_supervision import ContextEventUnreadable

CANDIDATE=Path(__file__).resolve().parents[3]
FIXTURES=CANDIDATE/'integration-fixtures'
FIXTURES.mkdir(exist_ok=True)

def step(index,fresh,cache):
    return {'event':'step_update','step_update':{'conversation_id':'synthetic',
        'step_index':index,'state':'DONE','step_type':'agent_response',
        'usage':{'input_tokens':fresh,'cache_read_tokens':cache,'output_tokens':17}}}

class AntigravityContextIntegrationTests(unittest.TestCase):
    def test_unreadable_envelope_cannot_disappear_from_completeness(self):
        for row in ({'event':'step_update'}, {'event':'step_update','step_update':None},
                    {'event':'step_update','step_update':{'state':'DONE','usage':{'input_tokens':1,'cache_read_tokens':1}}}):
            with self.subTest(row=row):
                measured=antigravity_context.measure_steps([step(1,1,0),row])
                self.assertTrue(measured['incomplete'])
                self.assertEqual(measured['steps'],[1])
                with self.assertRaises(ContextEventUnreadable):
                    antigravity_context.stream_context_tokens(json.dumps(row))

    def test_deep_json_and_blank_identity_are_unreadable(self):
        with self.assertRaises(ContextEventUnreadable):
            antigravity_context.stream_context_tokens('{"event":"step_update","step_update":'+'['*2000+'0'+']'*2000+'}')
        row=step(1,1,0)
        row['step_update']['conversation_id']='  '
        self.assertTrue(antigravity_context.measure_steps([row])['incomplete'])

    def test_numeric_dispatch_and_aggregate_exclusion(self):
        rows=[step(2,30117,0),step(263,6197,208634),
          {'event':'result','result':{'usage':{'input_tokens':1449789,'cache_read_tokens':8930668}}}]
        result=fleet.context_telemetry(['\n'.join(json.dumps(r) for r in rows)],adapter='antigravity')
        self.assertEqual(result['steps'],2)
        self.assertEqual(result['first_step_input'],30117)
        self.assertEqual(result['last_step_input'],214831)
        self.assertEqual(result['max_step_input'],214831)
        self.assertEqual(result['growth_ratio'],7.133)
        self.assertFalse(result['incomplete'])
        self.assertTrue(fleet.context_snowball_warning(result))

    def run_fixture(self,*,enabled=True,model='gemini-3.8-flash-high',cache=208634,malformed=False):
        root=Path(tempfile.mkdtemp(prefix='agy-',dir=FIXTURES))
        profiles=fleet.load_profiles(fleet.DEFAULT_PROFILES)
        if enabled: profiles['antigravity']['context_stop_tokens']=200000
        execution=routing_v2.resolve_execution_profile('antigravity',profiles['antigravity'],
            complexity='low',mode='review',required_tools=[],model_override='gemini-3.8-flash-high')
        rows=[{'event':'init','init':{'model':model}},step(2,30117,0),step(263,6197,cache)]
        if malformed: rows[-1]['step_update']['usage'].pop('input_tokens')
        code=("import pathlib,json,time\npathlib.Path('work.txt').write_text('retained')\n"
            f"rows={rows!r}\n"
            "for row in rows: print(json.dumps(row),flush=True); time.sleep(0.03)\n"
            "time.sleep(0.7)\npathlib.Path('late.txt').write_text('late')\n")
        original=fleet.run_provider_child
        launches=[]
        def child(command,**kwargs):
            launches.append(kwargs.get('max_context_tokens'))
            return original([sys.executable,'-u','-c',code],**kwargs)
        env=dict(os.environ,AGENT_FLEET_DEPTH='0',AGENT_FLEET_OFFICE='0',
                 AGENT_FLEET_BILLING_MODE_PATH=str(root/'missing.json'))
        patches=[mock.patch.dict(os.environ,env,clear=True),
           mock.patch.object(fleet,'resolve_executable',return_value=str(root/'agy.exe')),
           mock.patch.object(fleet,'probe_command',return_value={'launchable':True,'exit_code':0,'output':'ok'}),
           mock.patch.object(fleet,'profile_credential_status',return_value={'ready':True}),
           mock.patch.object(fleet,'merge_credential_readiness',return_value={'ready':True,'reasons':[]}),
           mock.patch.object(routing_v2,'readiness_from_probes',return_value={'ready':True,'reasons':[]}),
           mock.patch.object(routing_v2,'runtime_model_visible',return_value=(True,'visible')),
           mock.patch.object(routing_v2,'quota_hard_gates',return_value=[]),
           mock.patch.object(fleet,'current_quota_views',return_value=({'antigravity':{'status':'fresh','headroom':1.0,'confidence':1.0}},{})),
           mock.patch.object(fleet,'run_provider_child',side_effect=child)]
        with contextlib.ExitStack() as stack:
            for patch in patches: stack.enter_context(patch)
            result=fleet.execute_single('Synthetic numeric fixture only.',profiles=profiles,agent_id='antigravity',
                mode='review',kind='review',complexity='low',model_override='gemini-3.8-flash-high',workspace=root,
                timeout=5,allow_write=False,native_subagent_limit=0,enable_web=False,inherit_api_keys=False,
                dry_run=False,output_root=root/'evidence',root=root,execution_profile=execution,knowledge_mode='none')
        record=json.loads(Path(result['usage_record']).read_text(encoding='utf-8'))
        return result,record,root,launches

    def test_actual_private_stop_requires_parent_and_retains_work(self):
        result,record,root,launches=self.run_fixture()
        self.assertEqual(launches,[200000],'Never automatically retry observed context stop')
        self.assertEqual(result['exit_code'],127)
        self.assertEqual(result['status'],'PARTIAL')
        self.assertEqual(record['context_guard']['status'],'exceeded')
        self.assertEqual(record['context_guard']['observed_peak_tokens'],214831)
        self.assertEqual(record['completion_reason'],'observed-context-limit-needs-parent-review')
        self.assertEqual(record['model_identity']['status'],'verified')
        self.assertTrue(result['parent_acceptance_required'])
        self.assertEqual((root/'work.txt').read_text(),'retained')
        self.assertFalse((root/'late.txt').exists())
        self.assertIn('step_update',Path(result['stdout_path']).read_text())

    def test_foreign_model_does_not_become_partial(self):
        result,record,root,_=self.run_fixture(model='foreign-model')
        self.assertEqual(record['model_identity']['status'],'mismatch')
        self.assertNotIn(result['status'],['COMPLETED','PARTIAL'])
        self.assertTrue(record['context_guard']['exceeded'])
        self.assertTrue((root/'work.txt').exists())

    def test_unchanged_default_has_no_guard(self):
        result,record,root,launches=self.run_fixture(enabled=False)
        self.assertEqual(launches,[None])
        self.assertEqual(record['context_guard']['status'],'not-configured')
        self.assertTrue((root/'late.txt').exists())

    def test_missing_counter_cannot_prove_below_limit(self):
        _,record,root,_=self.run_fixture(malformed=True)
        self.assertEqual(record['context_guard']['status'],'unknown')
        self.assertTrue(record['context_guard']['scan_incomplete'])
        self.assertFalse(record['context_guard']['exceeded'])
        self.assertTrue((root/'work.txt').exists())

if __name__=='__main__': unittest.main()
