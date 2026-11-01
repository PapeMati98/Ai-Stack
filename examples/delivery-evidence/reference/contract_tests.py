"""Original synthetic product checks; a limited Python guard, never an OS-sandbox claim."""
import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
import uuid

sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parent
TOKEN_KEYS=('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens','reasoning_tokens','total_tokens')
STATUSES=('COMPLETED','FAILED','PARTIAL','CANCELLED','UNKNOWN')
SAFE_ENV_KEYS={'SYSTEMROOT','WINDIR','SYSTEMDRIVE','PATH','PATHEXT','COMSPEC','TEMP','TMP','PYTHONIOENCODING','PYTHONDONTWRITEBYTECODE'}
# Generated application code has no use for inherited account/provider variables.
safe={k:v for k,v in os.environ.items() if k.upper() in SAFE_ENV_KEYS}
os.environ.clear();os.environ.update(safe)
spawn_allowed=[False]
def audit(event,args):
    if event.startswith('socket.') or event in {'os.system','os.exec','os.posix_spawn','ctypes.dlopen'}:
        raise PermissionError('Synthetic product test forbids network or external execution')
    if event=='subprocess.Popen' and not spawn_allowed[0]:
        raise PermissionError('Application must not launch subprocesses')
    if event=='open' and args and isinstance(args[0],(str,bytes,os.PathLike)):
        path=Path(os.fsdecode(args[0])).resolve()
        roots=(ROOT,Path(sys.base_prefix).resolve(),Path(sys.prefix).resolve())
        if not any(path.is_relative_to(p) for p in roots):raise PermissionError('Product reads outside synthetic workspace forbidden')
        mode=args[1] if len(args)>1 else None;flags=args[2] if len(args)>2 else 0
        write=(isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
        if write and not path.is_relative_to(ROOT):raise PermissionError('Product writes outside synthetic workspace forbidden')
sys.addaudithook(audit)

if len(sys.argv)>1 and sys.argv[1]=='_cli':
    target=ROOT/'delivery_summary.py';sys.argv=[str(target),*sys.argv[2:]]
    sys.path.insert(0,str(ROOT))
    runpy.run_path(str(target),run_name='__main__');raise SystemExit(0)

def load_library():
    spec=importlib.util.spec_from_file_location('synthetic_delivery_product',ROOT/'delivery_evidence.py')
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module);return module

def records():
    return [
        {'run_id':'synthetic-a','status':'COMPLETED','quality':'ACCEPT','tokens':dict(zip(TOKEN_KEYS,(120,30,100,0,20,150))),'duration_seconds':1.25},
        {'run_id':'synthetic-b','status':'FAILED','quality':'unknown','tokens':dict(zip(TOKEN_KEYS,(0,0,0,0,0,0))),'duration_seconds':0},
        {'run_id':'synthetic-c','status':'PARTIAL','quality':'REPAIR','tokens':{'input_tokens':4},'duration_seconds':None},
    ]

class LibraryContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.m=load_library()
    def test_normalizes_whitelist_and_preserves_provider_total(self):
        row=records()[0];row['details']='SYNTHETIC-DETAILS-EXCLUDE';value=self.m.normalize_run(row)
        self.assertEqual(set(value),{'run_id','status','quality','tokens','duration_seconds'})
        self.assertEqual(value['tokens'],row['tokens']);self.assertNotIn('SYNTHETIC-DETAILS-EXCLUDE',json.dumps(value))
    def test_never_adds_cache_or_reasoning_again_to_provider_total(self):
        value=self.m.summarize_runs(records())
        self.assertEqual(value['tokens']['total_tokens'],{'known_sum':150,'unknown_count':1})
        self.assertEqual(value['tokens']['cache_read_tokens'],{'known_sum':100,'unknown_count':1})
        self.assertEqual(value['tokens']['reasoning_tokens'],{'known_sum':20,'unknown_count':1})
    def test_preserves_failed_partial_and_independent_quality(self):
        value=self.m.summarize_runs(records());self.assertEqual(value['run_count'],3)
        self.assertEqual(value['status_counts'],dict(zip(STATUSES,(1,1,1,0,0))))
        self.assertEqual(value['quality_counts'],{'ACCEPT':1,'REPAIR':1,'unknown':1})
    def test_never_derives_a_missing_total_from_input_output(self):
        value=self.m.summarize_runs([{'run_id':'x','tokens':{'input_tokens':8,'output_tokens':3}}])
        self.assertEqual(value['tokens']['total_tokens'],{'known_sum':None,'unknown_count':1})
    def test_missing_is_distinct_from_explicit_zero(self):
        missing=self.m.summarize_runs([{'run_id':'x'}]);zero=self.m.summarize_runs([records()[1]])
        self.assertEqual(missing['tokens']['total_tokens'],{'known_sum':None,'unknown_count':1})
        self.assertEqual(zero['tokens']['total_tokens'],{'known_sum':0,'unknown_count':0})
    def test_empty_population_is_explicit(self):
        value=self.m.summarize_runs([]);self.assertEqual(value['run_count'],0)
        self.assertEqual(value['status_counts'],{k:0 for k in STATUSES})
        self.assertEqual(value['quality_counts'],{'ACCEPT':0,'REPAIR':0,'unknown':0})
        for k in TOKEN_KEYS:self.assertEqual(value['tokens'][k],{'known_sum':None,'unknown_count':0})
    def test_durations_keep_known_sums_and_unknown_denominator(self):
        self.assertEqual(self.m.summarize_runs(records())['duration_seconds'],{'known_sum':1.25,'unknown_count':1})
    def test_invalid_token_numbers_are_rejected(self):
        for key in TOKEN_KEYS:
            for bad in (-1,True,1.5,float('nan'),float('inf'),'12'):
                with self.subTest(key=key,bad=repr(bad)),self.assertRaises(ValueError):self.m.normalize_run({'run_id':'x','tokens':{key:bad}})
    def test_invalid_durations_are_rejected(self):
        for bad in (-1,True,float('nan'),float('inf'),'2'):
            with self.subTest(bad=repr(bad)),self.assertRaises(ValueError):self.m.normalize_run({'run_id':'x','duration_seconds':bad})
    def test_missing_ids_duplicate_ids_and_bad_enums_are_rejected(self):
        for row in ({},{'run_id':''},{'run_id':' '},{'run_id':9},{'run_id':'x','status':'BOGUS'},{'run_id':'x','quality':'yes'},{'run_id':'x','tokens':'unknown'}):
            with self.subTest(row=row),self.assertRaises(ValueError):self.m.normalize_run(row)
        with self.assertRaises(ValueError):self.m.summarize_runs([{'run_id':'same'},{'run_id':'same'}])
    def test_unknown_defaults_and_cancelled_are_not_invented_success(self):
        value=self.m.normalize_run({'run_id':'x'});self.assertEqual(value['status'],'UNKNOWN');self.assertEqual(value['quality'],'unknown')
        value=self.m.normalize_run({'run_id':'x','status':'COMPLETED'});self.assertEqual(value['quality'],'unknown')
        self.assertEqual(self.m.summarize_runs([{'run_id':'x','status':'CANCELLED'}])['status_counts']['CANCELLED'],1)
    def test_input_records_are_not_mutated(self):
        rows=records();before=copy.deepcopy(rows);self.m.summarize_runs(rows);self.assertEqual(rows,before)

class CliContract(unittest.TestCase):
    def run_cli(self,args):
        spawn_allowed[0]=True
        try:return subprocess.run([sys.executable,'-I','-B',str(Path(__file__).resolve()),'_cli',*args],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),timeout=20)
        finally:spawn_allowed[0]=False
    def fixture(self,text):
        folder=ROOT/'.test-inputs';folder.mkdir(exist_ok=True);path=folder/(uuid.uuid4().hex+'.json');path.write_text(text,encoding='utf-8');return str(path)
    def test_actual_cli_reports_known_and_unknown_without_double_counting(self):
        result=self.run_cli([self.fixture(json.dumps(records()))]);self.assertEqual(result.returncode,0,result.stderr.decode())
        value=json.loads(result.stdout);self.assertEqual(value['run_count'],3);self.assertEqual(value['tokens']['total_tokens'],{'known_sum':150,'unknown_count':1});self.assertEqual(value['status_counts']['FAILED'],1)
    def test_output_is_byte_deterministic(self):
        path=self.fixture(json.dumps(records()));a=self.run_cli([path]);b=self.run_cli([path]);self.assertEqual(a.returncode,0);self.assertEqual(b.returncode,0);self.assertEqual(a.stdout,b.stdout)
    def test_invalid_json_is_a_controlled_error(self):
        result=self.run_cli([self.fixture('{bad')]);self.assertEqual(result.returncode,2);self.assertTrue(result.stderr);self.assertNotIn(b'Traceback',result.stderr)
    def test_invalid_record_never_echoes_extra_details(self):
        row={'run_id':'x','tokens':{'total_tokens':-1},'details':'SYNTHETIC-DETAILS-DO-NOT-ECHO'}
        result=self.run_cli([self.fixture(json.dumps([row]))]);self.assertEqual(result.returncode,2);self.assertNotIn(b'SYNTHETIC-DETAILS-DO-NOT-ECHO',result.stderr+result.stdout);self.assertNotIn(b'Traceback',result.stderr)
    def test_help_is_available(self):
        result=self.run_cli(['--help']);self.assertEqual(result.returncode,0);self.assertIn(b'usage',result.stdout.lower())
    def test_non_array_top_level_is_rejected(self):
        result=self.run_cli([self.fixture('{}')]);self.assertEqual(result.returncode,2);self.assertNotIn(b'Traceback',result.stderr)

if __name__=='__main__':
    lane=sys.argv[1] if len(sys.argv)==2 else '';case={'library':LibraryContract,'cli':CliContract}.get(lane)
    if case is None:raise SystemExit('Expected library or cli contract lane')
    outcome=unittest.TextTestRunner(stream=sys.stdout,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    print(json.dumps({'status':'PASS' if outcome.wasSuccessful() else 'FAIL','actual_tests':outcome.testsRun,'lane':lane,'guard':'limited Python audit and scrubbed environment, not OS sandbox'}),flush=True)
    raise SystemExit(0 if outcome.wasSuccessful() else 1)
