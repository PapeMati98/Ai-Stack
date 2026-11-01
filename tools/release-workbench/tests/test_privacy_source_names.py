"""Parent-owned synthetic regression canary, not a production credential read."""
import builtins,io,json,os,sys,unittest,uuid
from pathlib import Path
from unittest.mock import patch
R=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(R))
from release_workbench.privacy import scan_tree
FIXTURES=Path(os.environ.get('TEAM_TEST_ROOT',str(R/'test-temp')))/'privacy-source-regression-fixtures';FIXTURES.mkdir(parents=True,exist_ok=True)
class SourceNameRegression(unittest.TestCase):
 def fixture(self):
  p=FIXTURES/uuid.uuid4().hex;p.mkdir();return p
 def test_legitimate_source_is_scanned(self):
  p=self.fixture();(p/'secret_run.py').write_text('def safe_operation():\n    return None\n',encoding='utf-8')
  result=scan_tree(p);self.assertEqual(result['status'],'PASS');self.assertEqual(result['files_scanned'],1)
 def test_sensitive_content_in_recognized_source_is_still_detected(self):
  p=self.fixture();fake='gh'+'p_'+'A'*36;(p/'secret_run.py').write_text('api_key = '+repr(fake)+'\n',encoding='utf-8')
  result=scan_tree(p);self.assertEqual(result['status'],'FAIL');self.assertTrue(any(x['rule'].startswith('privacy.') for x in result['findings']))
 def test_sensitive_stores_refused_before_read(self):
  for name in ['credentials.json','credentials.py','secret.json','.envrc','secret_run.py.json']:
   with self.subTest(name=name):
    p=self.fixture();target=p/name;target.write_text('SYNTHETIC_OPAQUE_STORE_DATA',encoding='utf-8');reads=[]
    def guard(original):
     def call(file,*args,**kwargs):
      if isinstance(file,(str,bytes,os.PathLike)) and Path(os.fsdecode(file)).name==name:
       reads.append(name);raise AssertionError('Protected synthetic store must not be opened')
      return original(file,*args,**kwargs)
     return call
    with patch('builtins.open',guard(builtins.open)),patch('io.open',guard(io.open)),patch('os.open',guard(os.open)):
     result=scan_tree(p)
    self.assertEqual(reads,[]);self.assertEqual(result['files_scanned'],0);self.assertEqual(result['status'],'FAIL');self.assertTrue(any(x['rule']=='scan.refused_sensitive_path' for x in result['findings']))
 def test_protected_directory_does_not_gain_source_exception(self):
  p=self.fixture();(p/'secrets').mkdir();(p/'secrets/secret_run.py').write_text('def safe_operation():\n    return None\n',encoding='utf-8');result=scan_tree(p);self.assertEqual(result['status'],'FAIL');self.assertEqual(result['files_scanned'],0)
if __name__=='__main__':unittest.main()
