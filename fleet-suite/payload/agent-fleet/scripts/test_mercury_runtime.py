"""Offline Mercury runtime + actual Fleet adapter wiring. Never read a real key."""
from __future__ import annotations
import copy
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import shutil
import uuid
from unittest import mock

import mercury_runtime as mr

# Everything created by these tests stays in a new task-owned test-temp.
TEST_TEMP = Path(__file__).resolve().parents[3] / 'test-temp'

class MercuryFixtures(unittest.TestCase):
    def setUp(self):
        TEST_TEMP.mkdir(exist_ok=True)
        # Default mkdir ACL: Python 3.13 TemporaryDirectory's 0700 ACL excludes
        # the Windows sandbox's restricted SID. Never change machine ACLs.
        self.root = TEST_TEMP / ('mercury-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.workspace = self.root / 'synthetic'
        self.workspace.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.ledger = self.state / 'ledger.json'
        self.patch = mock.patch.multiple(mr, WORKSPACE_ROOT=str(self.workspace), STATE_ROOT=self.state)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        profiles = Path(__file__).resolve().parents[1] / 'references' / 'profiles.json'
        self.profile = json.loads(profiles.read_text(encoding='utf-8'))['profiles']['opencode-mercury']
        self.profile['data_boundary']['workspace_root'] = str(self.workspace)
        self.attestation = self.state / 'attestation.json'
        self.write(self.attestation, self.fresh())
        self.secret = 'synthetic-credential-DO-NOT-USE'
        self.native_reader = mr._read_native_credential
        self.native = mock.patch.object(mr, '_read_native_credential', return_value=self.secret)
        self.reader = self.native.start()
        self.addCleanup(self.native.stop)

    def fresh(self, **overrides):
        return dict(account_opt_out=True, billing_free_only=True,
                    remaining_free_tokens=100_000_000, checked_at=time.time(), **overrides)

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')


class MercuryRuntimeTests(MercuryFixtures):
    def test_exact_contract_and_no_credential_read_in_admission(self):
        self.assertEqual(mr.gate_native_run(self.profile, self.workspace)[0], self.workspace)
        self.reader.assert_not_called()
        self.assertTrue(self.profile['routing_enabled'])
        self.assertEqual(self.profile['quota_pool_id'], 'inception-mercury-free')

    def test_pin_drift_refused_before_credentials(self):
        changes = [('enabled_providers',['reflection']), ('quota_pool_id','zai-coding-plan'),
                   ('candidates',['opencode.cmd']), ('paid_fallback',True), ('paid_fallback',None),
                   ('routing_enabled',False), ('office_access','mcp'), ('local_runtime',{}),
                   ('version_args',['run','--model','evil/model']), ('paid_fallback',0),
                   ('credential',{'type':'windows-credential-manager','target':'other','env_var':'INCEPTION_API_KEY'})]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                p=copy.deepcopy(self.profile); p[key]=value
                with self.assertRaises(mr.MercuryGateError): mr.gate_native_run(p,self.workspace)
        for field, value in [('baseURL','http://api.inceptionlabs.ai/v1'), ('baseURL','https://evil.invalid/v1'),
                             ('apiKey','ambient')]:
            p=copy.deepcopy(self.profile); p['provider']['inception']['options'][field]=value
            with self.assertRaises(mr.MercuryGateError): mr.gate_native_run(p,self.workspace)
        p=copy.deepcopy(self.profile)
        next(iter(p['execution_profiles'].values()))['runtime_model_id']='inception/mercury-2'
        with self.assertRaises(mr.MercuryGateError): mr.gate_native_run(p,self.workspace)
        self.reader.assert_not_called()

    def test_attestation_invalid_types_expiry_and_false(self):
        invalid = {'account_opt_out':[False,1,'true',None], 'billing_free_only':[False,1,'true',None],
                   'remaining_free_tokens':[False,-1,float('nan'),100_000_001,'100000000'],
                   'checked_at':[True,None,'1',float('nan'),float('inf'),-1,time.time()-90000,time.time()+100]}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    data=self.fresh(); data[key]=value; self.write(self.attestation,data)
                    with self.assertRaises(mr.MercuryGateError): mr.gate_native_run(self.profile,self.workspace)
        self.reader.assert_not_called()

    def test_missing_malformed_and_low_snapshot(self):
        with self.assertRaises(mr.MercuryGateError): mr.read_attestation(self.state/'missing.json')
        for value in ('broken','[]','null','{}','{"account_opt_out":false,"account_opt_out":true}'):
            self.attestation.write_text(value,encoding='utf-8')
            with self.assertRaises(mr.MercuryGateError): mr.gate_native_run(self.profile,self.workspace)
        data=self.fresh(); data['remaining_free_tokens']=mr.RUN_RESERVATION-1
        self.write(self.attestation,data)
        with self.assertRaises(mr.MercuryGateError): mr.gate_native_run(self.profile,self.workspace)

    def test_cross_root_sibling_and_hardlink(self):
        outside=self.root/'synthetic-evil'; outside.mkdir()
        with self.assertRaises(mr.MercuryGateError): mr.gate_native_run(self.profile,outside)
        source=self.root/'outside.txt'; source.write_text('fixture')
        os.link(source,self.workspace/'hardlink.txt')
        with self.assertRaises(mr.MercuryGateError): mr.gate_native_run(self.profile,self.workspace)
        self.reader.assert_not_called()

    def test_symlink_workspace_and_state(self):
        outside=self.root/'outside'; outside.mkdir()
        link=self.workspace/'linked'
        try: link.symlink_to(outside,target_is_directory=True)
        except OSError: self.skipTest('symlink privilege unavailable; hardlink boundary tested separately')
        with self.assertRaises(mr.MercuryGateError): mr.gate_native_run(self.profile,self.workspace)
        with self.assertRaises(mr.MercuryGateError): mr.gate_native_run(self.profile,link)
        state_link=self.root/'state-link'; state_link.symlink_to(self.state,target_is_directory=True)
        with self.assertRaises(mr.MercuryGateError): mr.read_attestation(state_link/'attestation.json')

    def test_linked_attestation_ledger_and_lock(self):
        original=self.state/'original.json'; self.write(original,self.fresh())
        linked=self.state/'linked.json'; os.link(original,linked)
        with self.assertRaises(mr.MercuryGateError): mr.read_attestation(linked)
        os.link(original,self.ledger)
        with self.assertRaises(mr.MercuryGateError): mr.reserve(self.ledger,'run',100)
        other=self.state/'other-ledger.json'; os.link(original,Path(str(other)+'.lock'))
        with self.assertRaises(mr.MercuryGateError): mr.reserve(other,'run',100)

    def test_native_executable_pin_and_hash(self):
        with self.assertRaises(mr.MercuryGateError): mr.require_native('opencode.cmd')
        exe=self.root/'native.exe'; exe.write_bytes(b'offline test executable fixture')
        with mock.patch.object(mr,'NATIVE_EXE',str(exe)):
            with self.assertRaises(mr.MercuryGateError): mr.require_native(str(exe))
            with mock.patch.object(mr,'NATIVE_SHA256',hashlib.sha256(exe.read_bytes()).hexdigest()):
                self.assertEqual(mr.require_native(str(exe)),str(exe))

    def test_native_credential_status_injection_and_scrub(self):
        status=mr.credential_status(self.profile,self.workspace)
        self.assertTrue(status.available)
        self.assertEqual(status.fingerprint,hashlib.sha256(self.secret.encode()).hexdigest()[:16])
        self.assertNotIn(self.secret,repr(status))
        env=mr.inject_credential({},self.profile,self.workspace)
        self.assertEqual(env,{'INCEPTION_API_KEY':self.secret})
        out=mr.scrub_output(self.secret+' '+mr.CRED_TARGET,(self.secret,))
        self.assertNotIn(self.secret,out); self.assertNotIn(mr.CRED_TARGET,out)
        with self.assertRaises(mr.MercuryGateError): self.native_reader('other')

    def test_credential_blob_encodings_and_malicious_values(self):
        for encoding in ('utf-8','utf-16-le'):
            self.assertEqual(mr._decode_credential(self.secret.encode(encoding)),self.secret)
        for value in (b'',b'short',b'x'*8193,b'Bearer fake value',b'api-key\nattack',b'\xff\xfe\xff'):
            with self.subTest(value=value), self.assertRaises(mr.MercuryGateError): mr._decode_credential(value)

    def test_ledger_full_overage_idempotency_no_cross_run_release(self):
        mr.reserve(self.ledger,'one',100)
        mr.reserve(self.ledger,'two',200)
        mr.reserve(self.ledger,'one',100)
        result=mr.reconcile(self.ledger,'one',350)
        self.assertEqual((result['reserved'],result['spent_known']),(200,350))
        self.assertEqual(mr.reconcile(self.ledger,'one',350),result)
        with self.assertRaises(mr.MercuryGateError): mr.reconcile(self.ledger,'one',349)
        with self.assertRaises(mr.MercuryGateError): mr.reconcile(self.ledger,'missing',0)
        with self.assertRaises(mr.MercuryGateError): mr.reserve(self.ledger,'one',100)
        with self.assertRaises(mr.MercuryGateError): mr.reserve(self.ledger,'two',201)

    def test_unknown_usage_holds_reservation(self):
        mr.reserve(self.ledger,'one',100)
        self.assertEqual(mr.reconcile(self.ledger,'one',None)['reserved'],100)
        self.assertEqual(mr.reconcile(self.ledger,'one',None)['spent_known'],0)
        self.assertEqual(mr.reconcile(self.ledger,'one',10)['spent_known'],10)

    def test_concurrent_reconciliation_counts_one_run_once(self):
        mr.reserve(self.ledger,'one',100); mr.reserve(self.ledger,'two',200)
        errors=[]
        def finish():
            try: mr.reconcile(self.ledger,'one',125)
            except Exception as error: errors.append(type(error).__name__)
        threads=[threading.Thread(target=finish) for _ in range(12)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(errors)
        data=mr._read_ledger(self.ledger)
        self.assertEqual((data['reserved'],data['spent_known']),(200,125))

    @unittest.skipUnless(os.name=='nt','Windows junction regression')
    def test_junction_ancestor_descendant_and_state(self):
        outside=self.root/'outside'; outside.mkdir()
        link=self.workspace/'junction'
        result=subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(link),str(outside)],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        for workspace in (self.workspace,link):
            with self.assertRaises(mr.MercuryGateError): mr.gate_native_run(self.profile,workspace)
        self.write(outside/'attestation.json',self.fresh())
        with self.assertRaises(mr.MercuryGateError): mr.read_attestation(link/'attestation.json')

    def test_invalid_counts_and_over_cap(self):
        for value in [True,False,-1,float('nan'),1.5,'100',None,100_000_001,0]:
            with self.subTest(value=value), self.assertRaises(mr.MercuryGateError):
                mr.reserve(self.ledger,'run',value)
        mr.reserve(self.ledger,'one',100)
        for value in [True,-1,float('nan'),1.5,'1']:
            with self.subTest(value=value), self.assertRaises(mr.MercuryGateError):
                mr.reconcile(self.ledger,'one',value)
        with self.assertRaises(mr.MercuryGateError): mr.reserve(self.ledger,'two',100,remaining_snapshot=199)

    def test_invalid_stale_and_inconsistent_ledger(self):
        good=mr.reserve(self.ledger,'one',100)
        for key,value in [('reserved',False),('spent_known',-1),('allowance',100_000_001),
                          ('updated_at',float('nan')),('updated_at',time.time()-90000),('runs',[]),('reserved',99)]:
            with self.subTest(key=key,value=value):
                data=copy.deepcopy(good); data[key]=value; self.write(self.ledger,data)
                with self.assertRaises(mr.MercuryGateError): mr.reserve(self.ledger,'new',100)

    def test_concurrent_reservations_at_cap(self):
        errors=[]
        def run(i):
            try: mr.reserve(self.ledger,f'run-{i}',20_000_000)
            except mr.MercuryGateError: errors.append(i)
        threads=[threading.Thread(target=run,args=(i,)) for i in range(12)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        data=mr._read_ledger(self.ledger)
        self.assertEqual(data['reserved'],100_000_000)
        self.assertEqual(len(data['runs']),5)
        self.assertEqual(len(errors),7)

    def test_cross_process_reservations(self):
        # Native children are Python only, hidden; no credentials or model calls.
        code=('import mercury_runtime as m,sys; '
              'm.reserve(sys.argv[1],sys.argv[2],1000)')
        processes=[subprocess.Popen([sys.executable,'-B','-c',code,str(self.ledger),f'child-{i}'],
                   cwd=Path(__file__).parent,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)) for i in range(4)]
        for process in processes:
            stdout,stderr=process.communicate(timeout=20)
            self.assertEqual(process.returncode,0,stderr.decode(errors='replace'))
        self.assertEqual(mr._read_ledger(self.ledger)['reserved'],4000)

    def test_reasoning_counted_once_and_auxiliary_unknown(self):
        event={'type':'step_finish','part':{'id':'step1','tokens':{
            'total':1907,'input':1863,'output':12,'reasoning':32,'cache':{'read':0,'write':0}}}}
        line=json.dumps(event)
        usage=mr.native_usage(line+'\n'+line)
        self.assertEqual(usage['observed_main_total_tokens'],1907)
        self.assertIsNone(usage['total_tokens'])
        self.assertFalse(usage['auxiliary_usage_complete'])
        event['part']['tokens']['total']=True
        self.assertIsNone(mr.native_usage(json.dumps(event))['observed_main_total_tokens'])

    def test_refusal_only_error_event_or_stderr(self):
        for status in (401,402,403):
            result=mr.native_rejection(json.dumps({'type':'error','error':{'statusCode':status}}),'')
            self.assertEqual(result['status'],'recheck-required')
        self.assertIsNotNone(mr.native_rejection('','insufficient credits'))
        self.assertIsNone(mr.native_rejection(json.dumps({'type':'text','part':{'text':'HTTP 402'}}),''))


class MercuryRejectionTelemetryTests(unittest.TestCase):
    def test_numeric_runtime_telemetry_is_not_an_http_refusal(self):
        for value in (401, 402, 403):
            self.assertIsNone(mr.native_rejection('', f'INFO duration={value} service=stream'))

    def test_explicit_http_refusal_remains_a_recheck_signal(self):
        for value in (401, 402, 403):
            self.assertEqual('recheck-required', mr.native_rejection('', f'HTTP {value} request rejected')['status'])


class MercuryAdapterTests(MercuryFixtures):
    def setUp(self):
        super().setUp()
        # Exercise the in-process adapter below its outer recursion guard;
        # every native launcher is mocked in execute_single tests.
        depth = mock.patch.dict(os.environ, {'AGENT_FLEET_DEPTH': '0'})
        depth.start(); self.addCleanup(depth.stop)
        import fleet
        self.fleet=fleet
        self.zai=fleet.load_profiles(fleet.DEFAULT_PROFILES)['opencode']
        self.beam=fleet.load_profiles(fleet.DEFAULT_PROFILES)['opencode-beam']

    def test_beam_identity_and_ordinary_import_have_no_mercury_io(self):
        f=self.fleet
        self.assertFalse(f.is_beam_profile(self.profile)); self.assertTrue(f.is_beam_profile(self.beam))
        self.assertFalse(f.is_beam_profile({'data_boundary':{'synthetic_only':True}}))
        with mock.patch.object(f,'mercury_runtime_module',side_effect=AssertionError('ordinary session touched Mercury')):
            f.opencode_runtime_config(profile=self.zai)
            f.opencode_runtime_config(profile=self.beam)
            rows,_=f.live_readiness({'opencode-mercury':self.profile},cwd=self.root)
            self.assertFalse(rows['opencode-mercury']['ready'])

    def test_admitted_pilot_readiness_uses_bound_probes(self):
        f=self.fleet
        with mock.patch.object(f,'resolve_executable',return_value=mr.NATIVE_EXE), \
             mock.patch.object(mr,'require_native'), \
             mock.patch.object(f,'profile_probe_environment',return_value={}), \
             mock.patch.object(f,'probe_command',return_value={'launchable':True,'output':'1.18.21\ninception/mercury-3'}) as probe, \
             mock.patch.object(f.routing_v2,'readiness_from_probes',return_value={'ready':True,'reasons':[]}):
            rows,_=f.live_readiness({'opencode-mercury':self.profile},cwd=self.workspace)
        self.assertTrue(rows['opencode-mercury']['ready'])
        self.assertTrue(rows['opencode-mercury']['synthetic_admitted'])
        self.assertEqual(probe.call_count,2)
        for call in probe.call_args_list:
            self.assertEqual(call.kwargs['profile'],self.profile)
            self.assertEqual(call.kwargs['cwd'],self.workspace)

    def test_provider_contract_modes_and_permissions(self):
        f=self.fleet
        self.assertEqual(f.opencode_profile_provider_contract(self.profile)['provider_id'],'inception')
        for mode in ('advisory','review','implement'):
            execution=f.resolve_execution_profile('opencode-mercury',self.profile,complexity='medium',mode=mode,
                                                  required_tools=['write'] if mode=='implement' else [])
            self.assertEqual(execution['runtime_model_id'],'inception/mercury-3')
            self.assertEqual(execution['reasoning_level'],'medium')
            config=json.loads(f.opencode_runtime_config(profile=self.profile,mode=mode))
            self.assertEqual(config['agent']['build']['steps'],16)
            self.assertEqual(config['agent']['build']['temperature'],0.6)
            self.assertTrue(config['agent']['title']['disable'])
            self.assertEqual(config['permission']['edit'],f.beam_edit_permission() if mode=='implement' else 'deny')
            for name in ('bash','task','skill','webfetch','websearch','external_directory'):
                self.assertEqual(config['permission'][name],'deny')
            self.assertEqual(config['mcp'],{})
            self.assertEqual(config['provider']['inception']['models']['mercury-3']['limit']['output'],16384)
        with self.assertRaises(f.FleetError): f.opencode_runtime_config(profile=self.profile,office_mcp={})
        with self.assertRaises(f.FleetError): f.resolve_execution_profile('opencode-mercury',self.profile,complexity='low',
            mode='review',required_tools=[],execution_profile_override='opencode-mercury-3-implement')

    def test_command_pin_reasoning_and_input_envelope(self):
        f=self.fleet
        kwargs=dict(prompt='synthetic fixture',workspace=self.workspace,mode='implement',model='inception/mercury-3',
                    complexity='medium',timeout=30,run_dir=self.root,native_subagent_limit=0,enable_web=False,
                    materialize=False,reasoning_level='medium',profile=self.profile)
        command,_,_=f.build_command('opencode',mr.NATIVE_EXE,**kwargs)
        self.assertEqual(command[command.index('--variant')+1],'medium')
        for changes in ({'enable_web':True},{'native_subagent_limit':1},{'model':'inception/mercury-2'},
                        {'reasoning_level':'high'},{'prompt':'x'*24577}):
            with self.subTest(changes=changes), self.assertRaises(f.FleetError):
                f.build_command('opencode',mr.NATIVE_EXE,**dict(kwargs,**changes))

    def test_credential_wiring_failure_order_and_scrub_json(self):
        f=self.fleet
        data=self.fresh(); data['account_opt_out']=False; self.write(self.attestation,data)
        with self.assertRaises(f.FleetError): f.apply_profile_credential({},self.profile,workspace=self.workspace)
        self.reader.assert_not_called()
        self.write(self.attestation,self.fresh())
        env={}; f.apply_profile_credential(env,self.profile,workspace=self.workspace)
        self.assertEqual(env['INCEPTION_API_KEY'],self.secret)
        self.assertTrue(f.profile_credential_status(self.profile,workspace=self.workspace)['ready'])
        text=json.dumps({'part':{'text':self.secret+' '+mr.CRED_TARGET}})
        redacted=f.redact_sensitive_jsonl(text,exact_secret=self.secret)
        self.assertIsInstance(json.loads(redacted),dict)
        self.assertNotIn(self.secret,redacted); self.assertNotIn(mr.CRED_TARGET,redacted)
        self.reader.side_effect=mr.MercuryGateError('fixture-unavailable')
        self.assertFalse(f.profile_credential_status(self.profile,workspace=self.workspace)['ready'])
        with self.assertRaises(f.FleetError): f.apply_profile_credential({},self.profile,workspace=self.workspace)

    def test_probe_requires_admission_before_native_and_isolates_env(self):
        f=self.fleet
        outside=self.root/'outside'; outside.mkdir()
        with mock.patch.object(f.subprocess,'run') as native:
            with self.assertRaises(f.FleetError): f.profile_probe_environment(self.profile,workspace=outside)
            with self.assertRaises(f.FleetError): f.probe_command(mr.NATIVE_EXE,['--version'],profile=self.profile)
            native.assert_not_called(); self.reader.assert_not_called()
        with mock.patch.object(mr,'require_native'), mock.patch.object(f.tempfile,'mkdtemp',return_value=str(self.root/'probe')):
            env=f.profile_probe_environment(self.profile,workspace=self.workspace)
        self.assertNotIn('INCEPTION_API_KEY',env)
        for key in ('SYSTEMROOT','WINDIR','PATH'):
            if key in {name.upper() for name in os.environ}:
                self.assertIn(key, {name.upper() for name in env})
        self.assertNotIn('OPENAI_BASE_URL',mr.harden_environment({'OPENAI_BASE_URL':'https://evil','INCEPTION_API_KEY':'bad'}))
        self.assertEqual(Path(env['HOME']),self.root/'probe'/'opencode-home')
        for key in ('OPENCODE_DISABLE_PROJECT_CONFIG','OPENCODE_DISABLE_MODELS_FETCH'):
            self.assertEqual(env[key],'true')

    def test_resolved_config_drift_before_network_and_exact_permissions(self):
        f=self.fleet
        config=json.loads(f.opencode_runtime_config(profile=self.profile,mode='implement'))
        with mock.patch.object(mr,'require_native'), mock.patch.object(f.subprocess,'run') as native:
            native.return_value=subprocess.CompletedProcess([],0,json.dumps(config),'')
            self.assertEqual(f.validate_opencode_resolved_config(mr.NATIVE_EXE,self.workspace,{},profile=self.profile,mode='implement')['status'],'PASS')
            for agent in config['agent'].values():
                agent.setdefault('options',{});agent.setdefault('permission',{})
            native.return_value=subprocess.CompletedProcess([],0,json.dumps(config),'')
            self.assertEqual(f.validate_opencode_resolved_config(mr.NATIVE_EXE,self.workspace,{},profile=self.profile,mode='implement')['status'],'PASS')
            config['agent']['build']['permission']={'bash':'allow'}
            native.return_value=subprocess.CompletedProcess([],0,json.dumps(config),'')
            with self.assertRaises(f.FleetError):
                f.validate_opencode_resolved_config(mr.NATIVE_EXE,self.workspace,{},profile=self.profile,mode='implement')
            config['agent']['build']['permission']={}
            config['provider']['inception']['options']['baseURL']='https://evil.invalid/v1'
            native.return_value=subprocess.CompletedProcess([],0,json.dumps(config),'')
            with self.assertRaises(f.FleetError):
                f.validate_opencode_resolved_config(mr.NATIVE_EXE,self.workspace,{},profile=self.profile,mode='implement')
            native.reset_mock()
            data=self.fresh(); data['billing_free_only']=False; self.write(self.attestation,data)
            with self.assertRaises(f.FleetError):
                f.validate_opencode_resolved_config(mr.NATIVE_EXE,self.workspace,{},profile=self.profile)
            native.assert_not_called()

    def test_execute_rejects_outside_before_prompt_credentials_or_native(self):
        f=self.fleet
        outside=self.root/'outside'; outside.mkdir()
        kwargs=dict(agent_id='opencode-mercury',profiles={'opencode-mercury':self.profile},task='synthetic',
                    workspace=outside,root=self.root,output_root=None,mode='review',kind='review',complexity='low',
                    timeout=30,allow_write=False,inherit_api_keys=False,native_subagent_limit=0,enable_web=False,
                    dry_run=False,model_override=None)
        with mock.patch.object(f,'resolve_executable') as exe, mock.patch.object(f,'probe_command') as probe:
            with self.assertRaisesRegex(f.FleetError,'Mercury admission refused'): f.execute_single(**kwargs)
            with self.assertRaisesRegex(f.FleetError,'model override refused'):
                f.execute_single(**dict(kwargs,workspace=self.workspace,model_override='other/model'))
            exe.assert_not_called(); probe.assert_not_called(); self.reader.assert_not_called()

    def test_old_beam_zai_config_parity(self):
        f=self.fleet
        baseline=Path(__file__).resolve().parents[4]/'0.9.114'/'payload'/'agent-fleet'/'scripts'/'fleet.py'
        spec=importlib.util.spec_from_file_location('fleet_mercury_baseline',baseline)
        old=importlib.util.module_from_spec(spec); sys.modules[spec.name]=old
        self.addCleanup(sys.modules.pop,spec.name,None)
        spec.loader.exec_module(old)
        for profile in (self.zai,self.beam):
            for mode in ('advisory','review','implement'):
                self.assertEqual(f.opencode_runtime_config(profile=profile,mode=mode),old.opencode_runtime_config(profile=profile,mode=mode))

    def test_prepared_workspace_gate_and_real_injection_before_mock_child(self):
        f=self.fleet
        outside=self.root/'outside'; outside.mkdir()
        run_dir=self.root/'run'; run_dir.mkdir()
        kwargs=dict(agent_id='opencode-mercury',profiles={'opencode-mercury':self.profile},task='synthetic',
                    workspace=self.workspace,root=self.root,output_root=self.root,mode='implement',kind='implementation',
                    complexity='low',timeout=30,allow_write=True,inherit_api_keys=False,native_subagent_limit=0,
                    enable_web=False,dry_run=False,model_override=None,knowledge_mode='none')
        for prepared in (outside,self.workspace):
            with self.subTest(prepared=prepared), ExitStack() as stack:
                patches=[(f,'resolve_executable',mr.NATIVE_EXE),
                         (f,'probe_command',{'launchable':True,'output':'inception/mercury-3'}),
                         (f,'profile_probe_environment',{}),
                         (f,'profile_credential_status',{'required':True,'ready':True,'reason':'test-fixture'}),
                         (f.routing_v2,'readiness_from_probes',{'ready':True,'reasons':[]}),
                         (f.routing_v2,'runtime_model_visible',(True,'offline fixture')),
                         (f.routing_v2,'quota_hard_gates',[]),
                         (f.fleet_deny_guard,'require_catalog_allowed',{'status':'allowed'}),
                         (f,'current_quota_views',({},{})),
                         (f,'fleet_runtime_attestation',{'status':'VERIFIED'}),
                         (f,'create_run_dir',run_dir),
                         (f,'prepare_execution_workspace',(prepared,prepared,'a'*40)),
                         (f,'validate_opencode_resolved_config',{'status':'PASS'})]
                for target,name,value in patches:
                    stack.enter_context(mock.patch.object(target,name,return_value=value))
                stack.enter_context(mock.patch.object(mr,'require_native'))
                child=stack.enter_context(mock.patch.object(f,'run_provider_child',side_effect=RuntimeError('offline-stop-before-native')))
                self.reader.reset_mock()
                if prepared==outside:
                    with self.assertRaisesRegex(f.FleetError,'Mercury admission refused'):
                        f.execute_single(**kwargs)
                    child.assert_not_called(); self.reader.assert_not_called()
                else:
                    with self.assertRaisesRegex(RuntimeError,'offline-stop-before-native'):
                        f.execute_single(**kwargs)
                    child.assert_called_once()
                    self.reader.assert_called_once()
                    env=child.call_args.kwargs['env']
                    self.assertEqual(env['INCEPTION_API_KEY'],self.secret)
                    self.assertEqual(json.loads(env['OPENCODE_CONFIG_CONTENT'])['agent']['build']['steps'],16)
                    self.assertEqual(mr._read_ledger(self.state/'allowance.json')['reserved'],mr.RUN_RESERVATION)

if __name__ == '__main__':
    unittest.main()
