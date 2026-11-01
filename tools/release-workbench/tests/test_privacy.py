"""NEW PUBLIC TEST SUITE. No equivalence to private original coverage."""
import hashlib,os,unittest,uuid
from pathlib import Path
from release_workbench import privacy

class PublicPrivacyContract(unittest.TestCase):
 def setUp(self):
  self.root=Path(os.environ['PUBLIC_TEST_STATE'])/('privacy-'+uuid.uuid4().hex);self.root.mkdir()
 def make(self,name,text):
  p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf8');return p
 def scan(self,policy=None):return privacy.scan_tree(self.root,policy=policy)
 def assignment(self):return ''.join(('api','_key',' = ','SYNTHETIC','DATA','1234567890'))
 def entry(self,rule,path,body):return {'rule':rule,'path':path,'content_sha256':hashlib.sha256(body.encode()).hexdigest()}
 def test_clean_placeholder_and_real_finding(self):
  self.make('clean.txt',''.join(('api','_key',' = ','<PLACEHOLDER>')));r=self.scan();self.assertEqual(r['status'],'PASS',r)
  self.make('bad.txt',self.assignment());r=self.scan();self.assertEqual(r['status'],'FAIL');self.assertIn(('privacy.credential_assignment','bad.txt'),[(x['rule'],x['path']) for x in r['findings']])
 def test_exact_rule_path_and_whole_hash(self):
  body=self.assignment();p=self.make('a.txt',body);rule='privacy.credential_assignment';policy={'version':1,'allowlist':[self.entry(rule,'a.txt',body)]};r=self.scan(policy);self.assertEqual(r['status'],'PASS');self.assertEqual(r['allowlisted_findings'],1)
  self.make('b.txt',body);r=self.scan(policy);self.assertEqual(r['status'],'FAIL');self.assertEqual([x['path'] for x in r['findings']],['b.txt'])
  p.write_text(body+' changed',encoding='utf8');r=self.scan(policy);self.assertEqual(r['status'],'FAIL');self.assertIn('a.txt',[x['path'] for x in r['findings']])
 def test_other_rule_is_not_suppressed(self):
  body=self.assignment()+'\\n'+'PUBLIC'+'MARKER';self.make('a.txt',body);policy={'version':1,'forbidden_literals':['PUBLIC'+'MARKER'],'allowlist':[self.entry('privacy.credential_assignment','a.txt',body)]};r=self.scan(policy);self.assertEqual(r['status'],'FAIL');self.assertEqual([x['rule'] for x in r['findings']],['privacy.forbidden_literal']);self.assertEqual(r['allowlisted_findings'],1)
 def test_invalid_allowlist_fails_closed(self):
  digest='0'*64
  for rule,path,h in [('privacy.credential_assignment','../bad',digest),('privacy.unknown','a.txt',digest),('privacy.credential_assignment','a.txt','F'*64)]:
   with self.subTest(path=path),self.assertRaises(ValueError):privacy.validate_policy({'version':1,'allowlist':[{'rule':rule,'path':path,'content_sha256':h}]})
 def test_wildcard_is_literal_not_a_pattern(self):
  body=self.assignment();self.make('a.txt',body);policy={'version':1,'allowlist':[self.entry('privacy.credential_assignment','*.txt',body)]};r=self.scan(policy);self.assertEqual(r['status'],'FAIL');self.assertEqual(r['allowlisted_findings'],0);self.assertEqual(r['findings'][0]['path'],'a.txt')
 def test_sensitive_path_refused_before_read(self):
  self.make('.env.synthetic','PUBLIC');r=self.scan();self.assertEqual(r['status'],'FAIL');self.assertEqual(r['files_refused'],1);self.assertEqual(r['findings'][0]['rule'],'scan.refused_sensitive_path')
 def test_binary_file_and_unknown_encoding_fail_closed(self):
  (self.root/'binary.bin').write_bytes(bytes((0,1,2)));(self.root/'unknown.txt').write_bytes(bytes((255,)));r=self.scan();self.assertEqual(r['status'],'FAIL');self.assertEqual({x['rule'] for x in r['findings']},{'scan.binary_file','scan.unknown_encoding'});self.assertEqual(r['files_refused'],2)
 def test_policy_duplicate_and_unknown_fields_rejected(self):
  for policy in ({'version':1,'extra':True},{'version':True},{'version':1,'forbidden_literals':['same','SAME']}):
   with self.assertRaises(ValueError):privacy.validate_policy(policy)
if __name__=='__main__':unittest.main()
