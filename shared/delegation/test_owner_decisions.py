"""NEW PUBLIC TEST SUITE. No equivalence to private original coverage."""
import json,os,unittest,uuid
from pathlib import Path
import owner_decisions as decisions

class PublicDecisionContract(unittest.TestCase):
 def setUp(self):
  self.root=Path(os.environ['PUBLIC_TEST_STATE'])/('decision-'+uuid.uuid4().hex);self.root.mkdir();self.store=self.root/'records.jsonl'
 def record(self,**kw):
  data={'project':'public-demo','question':'Choose a schedule','choice':'weekly','category':'technical','source':'chat','now':'2026-01-02T03:04:05Z','path':self.store};data.update(kw);return decisions.record_decision(**data)
 def test_chain_append_only_and_supersedes(self):
  a=self.record();first=self.store.read_bytes();b=self.record(choice='monthly',supersedes=a['id']);self.assertTrue(self.store.read_bytes().startswith(first));self.assertNotEqual(a['id'],b['id']);self.assertEqual(b['prev'],a['sha256']);self.assertEqual(decisions.read_entries(self.store),[a,b]);self.assertTrue(decisions.verify_chain([a,b])['ok']);self.assertEqual(decisions.filter_entries([a,b],superseded_only=True),[b])
 def test_tamper_or_reorder_fails_integrity(self):
  a=self.record();b=self.record(choice='monthly');changed=dict(a,choice='daily');self.assertFalse(decisions.verify_chain([changed,b])['ok']);self.assertFalse(decisions.verify_chain([b,a])['ok']);self.assertTrue(decisions.verify_chain([a,b])['ok'])
 def test_invalid_inputs_and_decider_fail_closed(self):
  for change in ({'project':'  '},{'question':''},{'choice':''},{'category':'other'},{'source':'unknown'},{'decided_by':'worker'},{'decided_by':'director:'}):
   with self.subTest(change=change),self.assertRaises(decisions.DecisionError):self.record(**change)
  self.assertFalse(self.store.exists())
 def test_secret_like_input_rejected_without_writing(self):
  value=''.join(('pass','word','=','SYNTHETIC_ONLY'))
  with self.assertRaises(decisions.DecisionError) as caught:self.record(question=value)
  self.assertEqual(caught.exception.code,'SECRET_LIKE_CONTENT');self.assertFalse(self.store.exists())
 def test_unknown_supersedes_does_not_modify_log(self):
  self.record();before=self.store.read_bytes()
  with self.assertRaises(decisions.DecisionError) as caught:self.record(supersedes='unknown-public-id')
  self.assertEqual(caught.exception.code,'UNKNOWN_SUPERSEDES');self.assertEqual(self.store.read_bytes(),before)
 def test_invalid_store_target_cannot_write(self):
  with self.assertRaises(OSError):self.record(path=self.root)
  self.assertEqual(list(self.root.iterdir()),[])
 def test_filter_and_bad_timestamp(self):
  a=self.record();b=self.record(project='different',choice='monthly');self.assertEqual(decisions.filter_entries([a,b],project='public-demo'),[a])
  with self.assertRaises(decisions.DecisionError) as caught:decisions.filter_entries([a],since='not-a-date')
  self.assertEqual(caught.exception.code,'BAD_TIMESTAMP')
if __name__=='__main__':unittest.main()
