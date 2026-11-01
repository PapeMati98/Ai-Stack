import json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import zcode_scope_mcp as m
import zcode_runtime as z
from types import SimpleNamespace

class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='zcode-read-');self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'fixture.txt').write_bytes(b'SYNTHETIC_DATA\n')
    def policy(self,cap=3): return m.build_policy(self.root,['fixture.txt'],cap)
    def call(self,name='fleet_read_file',args=None): return {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':{'path':'fixture.txt'} if args is None else args}}
    def test_read(self):
        r=m.Broker(self.policy()).handle(self.call());v=json.loads(r['result']['content'][0]['text']);self.assertEqual(v['content'],'SYNTHETIC_DATA\n')
    def test_path_and_fd_ctime_can_have_different_windows_semantics(self):
        policy=self.policy();real=os.stat(self.root/'fixture.txt')
        opened=SimpleNamespace(**{k:getattr(real,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_nlink')},st_ctime_ns=real.st_ctime_ns+1000000)
        with patch.object(m.os,'fstat',return_value=opened):
            self.assertIn('result',m.Broker(policy).handle(self.call()))
    def test_fd_change_time_mutation_during_read_is_refused(self):
        policy=self.policy();real=os.stat(self.root/'fixture.txt')
        common={k:getattr(real,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_nlink')}
        opened=SimpleNamespace(**common,st_ctime_ns=real.st_ctime_ns+1000000)
        changed=SimpleNamespace(**common,st_ctime_ns=opened.st_ctime_ns+1)
        with patch.object(m.os,'fstat',side_effect=[opened,changed]):
            self.assertIn('error',m.Broker(policy).handle(self.call()))
    def test_exact_list_does_not_scan(self):
        with patch.object(Path,'rglob',side_effect=AssertionError('No scans')):
            r=m.Broker(self.policy()).handle(self.call('fleet_list_files',{}));self.assertEqual(json.loads(r['result']['content'][0]['text']),{'files':['fixture.txt']})
    def test_byte_window(self):
        r=m.Broker(self.policy()).handle(self.call(args={'path':'fixture.txt','offset':2,'limit':3}));self.assertEqual(json.loads(r['result']['content'][0]['text'])['content'],'NTH')
    def test_utf8_windows_reconstruct_mixed_width_boundaries(self):
        two='Ł'.encode('utf-8');three='→'.encode('utf-8');four='😀'.encode('utf-8')
        raw=b'a'*(8192-1)+two+b'a'*(8191-len(two))+three+b'a'*(8191-len(three))+four+b'end'
        (self.root/'fixture.txt').write_bytes(raw);broker=m.Broker(self.policy(8))
        offset=0;parts=[];expected_hints=[2,3,4]
        while offset<len(raw):
            response=broker.handle(self.call(args={'path':'fixture.txt','offset':offset,'limit':8192}))
            self.assertIn('result',response)
            value=json.loads(response['result']['content'][0]['text']);encoded=value['content'].encode('utf-8')
            self.assertEqual(value['requested_offset'],offset);self.assertEqual(value['offset'],offset)
            self.assertEqual(value['bytes'],len(encoded));self.assertLessEqual(value['bytes'],8192)
            self.assertEqual(value['next_offset'],offset+value['bytes'])
            self.assertLessEqual(len(json.dumps(response,ensure_ascii=False).encode('utf-8')),m.MAX_OUTPUT)
            if len(parts)<len(expected_hints): self.assertEqual(value['next_min_limit'],expected_hints[len(parts)])
            self.assertGreater(value['next_offset'],offset)
            parts.append(encoded);offset=value['next_offset']
        self.assertEqual(b''.join(parts),raw)
    def test_mid_character_offset_reports_aligned_start(self):
        raw='AŁ→😀Z'.encode('utf-8');emoji='😀'.encode('utf-8');start=raw.index(emoji);requested=start+2
        (self.root/'fixture.txt').write_bytes(raw)
        response=m.Broker(self.policy()).handle(self.call(args={'path':'fixture.txt','offset':requested,'limit':16}))
        self.assertIn('result',response);value=json.loads(response['result']['content'][0]['text'])
        self.assertEqual(value['requested_offset'],requested);self.assertEqual(value['offset'],start)
        self.assertEqual(value['content'],'😀Z');self.assertEqual(value['next_offset'],len(raw))
        self.assertEqual(value['content'].encode('utf-8'),raw[start:])
    def test_small_limit_returns_retry_hint_without_dropping_codepoint(self):
        raw='😀tail'.encode('utf-8');(self.root/'fixture.txt').write_bytes(raw);broker=m.Broker(self.policy())
        first=broker.handle(self.call(args={'path':'fixture.txt','offset':0,'limit':1}))
        value=json.loads(first['result']['content'][0]['text'])
        self.assertEqual(value['content'],'');self.assertEqual(value['bytes'],0)
        self.assertEqual(value['next_offset'],0);self.assertEqual(value['next_min_limit'],4)
        second=broker.handle(self.call(args={'path':'fixture.txt','offset':value['next_offset'],'limit':value['next_min_limit']}))
        next_value=json.loads(second['result']['content'][0]['text'])
        self.assertEqual(next_value['content'],'😀');self.assertEqual(next_value['next_offset'],4)
        tail=broker.handle(self.call(args={'path':'fixture.txt','offset':next_value['next_offset'],'limit':4}))
        tail_value=json.loads(tail['result']['content'][0]['text'])
        self.assertEqual((value['content']+next_value['content']+tail_value['content']).encode('utf-8'),raw)
    def test_handshake(self):
        b=m.Broker(self.policy());r=b.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{}});self.assertEqual(r['result']['protocolVersion'],'2024-11-05');self.assertEqual(r['result']['capabilities'],{'tools':{}})
        self.assertIsNone(b.handle({'jsonrpc':'2.0','method':'notifications/initialized'}));self.assertEqual(len(b.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})['result']['tools']),2)
    def test_budget_mandatory(self):
        for cap in (None,True,0,-1,'2',1001):
            with self.subTest(cap=cap),self.assertRaises(ValueError): self.policy(cap)
    def test_denied_attempts_consume_budget(self):
        b=m.Broker(self.policy(1));self.assertIn('error',b.handle(self.call('unknown',{})));self.assertIn('error',b.handle(self.call()));self.assertEqual(b.calls,2)
    def test_unadmitted_sensitive_and_traversal(self):
        for path in ('other.txt','../fixture.txt','C:/x','//server/x','fixture.txt:ads','.zcode/config.json','.env','credentials.json','.git/config','CON'):
            with self.subTest(path=path): self.assertIn('error',m.Broker(self.policy()).handle(self.call(args={'path':path})))
    def test_invalid_bounds_and_extra_args(self):
        for extra in ({'offset':True},{'offset':-1},{'limit':True},{'limit':0},{'limit':8193},{'x':'SYNTHETIC_DATA'}):
            r=m.Broker(self.policy()).handle(self.call(args={'path':'fixture.txt',**extra}));self.assertIn('error',r);self.assertNotIn('SYNTHETIC_DATA',json.dumps(r))
    def test_directory_admission_refused(self):
        with self.assertRaises((ValueError,OSError)): m.build_policy(self.root,['.'],3)
    def test_case_alias_refused(self):
        with self.assertRaises(ValueError): m.build_policy(self.root,['fixture.txt','FIXTURE.TXT'],3)
    def test_hardlink_refused(self):
        os.link(self.root/'fixture.txt',self.root/'linked.txt')
        with self.assertRaises(ValueError): self.policy()
    def test_symlink_refused(self):
        try: (self.root/'linked.txt').symlink_to(self.root/'fixture.txt')
        except OSError:
            if os.name!='nt': self.skipTest('Host lacks symlink privilege')
            target=self.root/'real-dir';target.mkdir();(target/'fixture.txt').write_bytes(b'SYNTHETIC_DATA')
            linked=self.root/'linked-dir'
            # Both absolute targets are created by this test under its task temp.
            assert target.resolve().is_relative_to(self.root.resolve()) and linked.parent.resolve()==self.root.resolve()
            r=subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(linked),str(target)],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode: self.skipTest('Host lacks file-symlink and junction capability')
            with self.assertRaises(ValueError): m.build_policy(self.root,['linked-dir/fixture.txt'],3)
        else:
            with self.assertRaises(ValueError): m.build_policy(self.root,['linked.txt'],3)
    def test_changed_file_refused(self):
        b=m.Broker(self.policy());(self.root/'fixture.txt').write_text('CHANGED');self.assertIn('error',b.handle(self.call()))
    def test_policy_drift_refused(self):
        p=self.policy();p['fingerprints']['fixture.txt'][2]+=1
        with self.assertRaises(ValueError): m.Broker(p)
    def test_encoding_refused(self):
        for raw in (b'\xff\x00',b'valid\xe2\x82'):
            with self.subTest(raw=raw):
                (self.root/'fixture.txt').write_bytes(raw)
                self.assertIn('error',m.Broker(self.policy()).handle(self.call()))
    def test_batch_and_unknown_methods_refused(self):
        b=m.Broker(self.policy())
        for v in ([],None,{'jsonrpc':'2.0','id':1,'method':'resources/list'},{'jsonrpc':'2.0','id':1,'method':'tools/call','params':None}): self.assertIn('error',b.handle(v))
    def test_output_bound(self):
        self.assertLessEqual(len(m.bounded({'id':1,'huge':'x'*20000}).encode()),m.MAX_OUTPUT)
    def test_file_bound(self):
        (self.root/'fixture.txt').write_bytes(b'x'*(m.MAX_FILE+1))
        with self.assertRaises(ValueError): self.policy()
    def test_owner_home_binding_survives_native_isolation(self):
        owner=self.root/'owner';owner.mkdir()
        work=owner/'.agents/agent-fleet/evidence/run-20261011T000000Z-0123456789/worktree';work.mkdir(parents=True)
        (work/'fixture.txt').write_bytes(b'SYNTHETIC')
        policy=m.build_policy(work,['fixture.txt'],2,owner_home=str(owner))
        with patch.object(Path,'home',return_value=self.root/'isolated'):
            self.assertIn('result',m.Broker(policy).handle(self.call()))
    def test_owner_state_not_admitted_as_workspace(self):
        owner=self.root/'owner';owner.mkdir();store=owner/'.agents/agent-fleet/runs';store.mkdir(parents=True);(store/'fixture.txt').write_bytes(b'SYNTHETIC')
        with self.assertRaises(ValueError): m.build_policy(store,['fixture.txt'],2,owner_home=str(owner))

    def test_hidden_broker_roundtrips_unicode_with_cp1250_stdio_settings(self):
        text='ASCII Polski Łódź → 😀'
        (self.root/'fixture.txt').write_text(text,encoding='utf-8')
        policy=m.build_policy(self.root,['fixture.txt'],2)
        policy_path=self.root/'policy.json';policy_path.write_text(json.dumps(policy),encoding='utf-8')
        env={key:value for key,value in os.environ.items() if key.casefold() in z.ENV_KEYS}
        env.update(PYTHONIOENCODING='cp1250',PYTHONUTF8='0')
        messages=[
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'fleet_read_file','arguments':{'path':'fixture.txt'}}},
        ]
        request=b''.join((json.dumps(item,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8') for item in messages)
        child=subprocess.Popen([sys.executable,'-B',str(Path(m.__file__).resolve()),'--policy',str(policy_path)],
            cwd=str(self.root),env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        stdout,stderr=child.communicate(request,timeout=20)
        self.assertEqual(child.returncode,0,stderr.decode('ascii',errors='strict'))
        self.assertEqual(stderr,b'')
        self.assertIn('Łódź → 😀'.encode('utf-8'),stdout)
        responses=[json.loads(line.decode('utf-8')) for line in stdout.splitlines()]
        read=json.loads(responses[-1]['result']['content'][0]['text'])
        self.assertEqual(read['content'],text)

    def test_no_native_sdk_imports(self):
        self.assertNotIn('mistral_tools',Path(m.__file__).read_text(encoding='utf-8'))
        for text in ('import vibe','from vibe','import mistralai','from mistralai'):
            self.assertNotIn(text,Path(m.paths.__file__).read_text(encoding='utf-8'))

if __name__=='__main__': unittest.main()
