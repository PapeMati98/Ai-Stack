import datetime,json,os,unittest
from unittest import mock
import knowledge as k
import test_knowledge_hygiene as h

class PermanentIntakeTests(unittest.TestCase):
 def setUp(self):
  self.f=h.KnowledgeHygieneTests();self.f.setUp();self.f.initialize_continuity_vault();page=self.f.vault/'wiki/projects/northwind-app.md';body=page.read_text(encoding='utf-8');page.write_text(body if 'project_slug:' in body else body.replace('type: project', 'type: project\nproject_slug: northwind-app',1),encoding='utf-8');self.f.enable_owner_checkpoint();self.root=self.f.root;self.vault=self.f.vault
 def tearDown(self):self.f.tearDown()
 def git(self,*a):return k.run_git_without_hooks(self.root,list(a))
 def seed(self,foreign=True):
  note=self.root/'owned-note.md';note.write_text('Synthetic source, no customer data.\n',encoding='utf-8')
  with mock.patch.object(k,'checkpoint_intake',return_value={'status':'BLOCKED_CHECKPOINT_LOCK','required':True}):r=k.record_handoff(self.vault,self.f.checkpoint_handoff_args(note))
  self.pid=r['proposal_id'];events=list((self.vault/'events').rglob('*.json'));self.events=events
  self.git('add','--',*[x.relative_to(self.root).as_posix() for x in events])
  if foreign:
   p=self.root/'foreign-publication.md';p.write_text('Other owned synthetic publication.\n');self.git('add','--','foreign-publication.md')
  self.git('commit','--no-verify','--no-gpg-sign','-m','Synthetic publication captured receipt events')
  return r
 def snapshot(self):return {str(p.relative_to(self.root)):p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}
 def test_partial_without_flag_stays_blocked(self):
  self.seed();b=self.snapshot();r=k.checkpoint_intake(self.vault,self.pid);self.assertIn(r['status'],['BLOCKED_PARTIAL_PROPOSAL','BLOCKED_PARTIAL_SOURCE']);self.assertEqual(self.snapshot(),b)
 def test_full_receipt_after_foreign_event_commit_and_later_head(self):
  self.seed();b=self.snapshot();r=k.checkpoint_intake(self.vault,self.pid,recover_partial_pair=True);self.assertEqual(r['status'],'CHECKPOINTED',r);self.assertEqual(r['path_count'],3);self.assertEqual(r['receipt_path_count'],5);self.assertTrue(r['partial_recovery']);self.assertEqual(self.snapshot(),b)
  p=self.root/'later-owned.md';p.write_text('Later unrelated owned change.\n');self.git('add','--','later-owned.md');self.git('commit','--no-verify','--no-gpg-sign','-m','Later unrelated owned change')
  retry=k.checkpoint_intake(self.vault,self.pid);self.assertEqual(retry['status'],'CURRENT',retry);self.assertEqual(retry['receipt_path_count'],5)
 def test_modified_committed_event_refused(self):
  self.seed();e=self.events[0];v=json.loads(e.read_text());v['summary']='altered original';e.write_text(json.dumps(v));self.git('add','--',e.relative_to(self.root).as_posix());self.git('commit','--no-verify','--no-gpg-sign','-m','Altered historical event');b=self.snapshot();r=k.checkpoint_intake(self.vault,self.pid,recover_partial_pair=True);self.assertTrue(r['status'].startswith('BLOCKED'),r);self.assertEqual(self.snapshot(),b)
 def test_event_producer_mismatch_refused(self):
  self.seed();e=self.events[0];v=json.loads(e.read_text());v['agent']='wrong-agent';e.write_text(json.dumps(v));self.git('add','--',e.relative_to(self.root).as_posix());self.git('commit','--no-verify','--no-gpg-sign','-m','Altered producer');b=self.snapshot();r=k.checkpoint_intake(self.vault,self.pid,recover_partial_pair=True);self.assertTrue(r['status'].startswith('BLOCKED'),r);self.assertEqual(self.snapshot(),b)
 def test_dirty_unrelated_index_preserved(self):
  self.seed();p=self.root/'unrelated-index.md';p.write_text('Owned staged unrelated.');self.git('add','--','unrelated-index.md');b=self.snapshot();index=self.git('diff','--cached','--raw').stdout;r=k.checkpoint_intake(self.vault,self.pid,recover_partial_pair=True);self.assertEqual(r['status'],'BLOCKED_DIRTY_INDEX');self.assertEqual(self.snapshot(),b);self.assertEqual(self.git('diff','--cached','--raw').stdout,index)
 def test_bad_source_hash_refused(self):
  self.seed();p=next((self.vault/'raw/sources').rglob('original.md'));p.write_text('Different bytes');b=self.snapshot();r=k.checkpoint_intake(self.vault,self.pid,recover_partial_pair=True);self.assertEqual(r['status'],'BLOCKED_SOURCE_INTEGRITY');self.assertEqual(self.snapshot(),b)
 def test_bad_proof_digest_refused_on_retry(self):
  self.seed();r=k.checkpoint_intake(self.vault,self.pid,recover_partial_pair=True);self.assertEqual(r['status'],'CHECKPOINTED');self.git('commit','--amend','--no-verify','--no-gpg-sign','-m','No valid receipt proof');b=self.snapshot();r=k.checkpoint_intake(self.vault,self.pid);self.assertEqual(r['status'],'BLOCKED_CURRENT_UNPROVEN',r);self.assertEqual(self.snapshot(),b)
 def test_zero_events_refused(self):
  self.seed();target=next(p for p in self.events if json.loads(p.read_text())['action']=='new-proposal');v=json.loads(target.read_text());v['proposal_ids']=[];target.write_text(json.dumps(v));b=self.snapshot();r=k.checkpoint_intake(self.vault,self.pid,recover_partial_pair=True);self.assertEqual(r['status'],'BLOCKED_PROPOSAL_EVENT_CARDINALITY');self.assertEqual(self.snapshot(),b)
 def test_normal_closeout_recovers_proven_dead_owner(self):
  lp=k.git_internal_path(self.root,k.CHECKPOINT_LOCK_NAME);created=k.now_utc()-datetime.timedelta(seconds=1800);lp.write_text(json.dumps({'lock_id':'ab'*16,'pid':876543,'machine':k.local_machine_name(),'created_at':k.iso_time(created)}));os.utime(lp,(created.timestamp(),created.timestamp()))
  note=self.root/'closeout.md';note.write_text('Normal synthetic completion.\n')
  with mock.patch.object(k,'process_is_running',return_value=False):r=k.record_handoff(self.vault,self.f.checkpoint_handoff_args(note))
  self.assertEqual(r['receipt_status'],'COMPLETE',r);self.assertTrue(r['checkpoint']['stale_lock_recovered'])
 def test_live_original_even_corrupt_start_preserved(self):
  lp=k.git_internal_path(self.root,k.CHECKPOINT_LOCK_NAME);created=k.now_utc()-datetime.timedelta(seconds=1800);lp.write_text(json.dumps({'lock_id':'ab'*16,'pid':876543,'machine':k.local_machine_name(),'created_at':k.iso_time(created),'owner_process_started_at':k.iso_time(created+datetime.timedelta(seconds=500))}));os.utime(lp,(created.timestamp(),created.timestamp()))
  b=lp.read_bytes()
  with mock.patch.object(k,'process_is_running',return_value=True),mock.patch.object(k,'windows_process_started_at',return_value=created-datetime.timedelta(seconds=120)):
   self.assertFalse(k.recover_stale_checkpoint_lock(lp,{'machine':k.local_machine_name()},requested=True))
  self.assertEqual(lp.read_bytes(),b)
 def test_admission_boundary_uses_same_math(self):
  for count in [45,46,48,50]:
   a={'policy':{'block_count':50,'block_age_hours':24},'changed_file_count':count,'oldest_age_hours':None,'state':'warning','write_blocked':False};r=k.backlog_admission(a,4);self.assertEqual(r['blocked'],count+4>=50,r)

if __name__=='__main__':unittest.main()
