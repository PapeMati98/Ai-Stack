import copy
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from artifact_recovery import dirty_manifest, manifest, retained_candidate

class RecoveryTests(unittest.TestCase):
    def test_requires_verified_origin_model_workspace_scope_and_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'file.py').write_text('before')
            subprocess.run(['git','init','-q',str(root)],check=True)
            subprocess.run(['git','-C',str(root),'add','file.py'],check=True)
            subprocess.run(['git','-C',str(root),'-c','user.name=Test','-c','user.email=test@local.invalid',
                            'commit','-qm','Synthetic baseline'],check=True)
            base=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
            before_dirty=dirty_manifest(root)
            before=manifest(root,['file.py']); (root/'file.py').write_text('after')
            record={'run_id':'r1','agent':'muse','model':'muse-spark-1.3','model_provider_id':'meta',
                'runtime_attestation':{'status':'VERIFIED'}, 'model_identity':{'status':'verified'},
                'worktree_base_commit_sha':base,'worktree_base_provenance':'preserved',
                'exit_code':0,'handoff_parse_error':'invalid-json','provider_terminal_reason':'success',
                'execution_workspace':str(root)}
            result={'run_id':'r1','model':'muse-spark-1.3','artifact_disposition':{
                'status':'preserved','base_commit_sha':base,'base_provenance':'preserved','worktree':str(root)}}
            kwargs=dict(workspace=root,scope=['file.py'],base=base,expected_model='muse-spark-1.3',
                        expected_agent='muse',lineage='meta',before=before,before_dirty=before_dirty)
            proof=retained_candidate(record,result,**kwargs)
            self.assertEqual(proof['acceptance_status'],'PENDING_TESTS_AND_REVIEW')
            for field,value in [('run_id','other'),('model','wrong'),('agent','other'),
                ('worktree_base_commit_sha','wrong'),('worktree_base_provenance',None),
                ('exit_code',1),('provider_terminal_reason','error'),('execution_workspace',str(root/'absent')),
                ('handoff_parse_error',None),('model_identity',{'status':'mismatch'}),
                ('runtime_attestation',{'status':'FAILED'}),('model_provider_id','other')]:
                bad=copy.deepcopy(record); bad[field]=value
                with self.subTest(field=field): self.assertIsNone(retained_candidate(bad,result,**kwargs))
            self.assertIsNone(retained_candidate(record,{**result,'artifact_disposition':'quarantined'},**kwargs))
            self.assertIsNone(retained_candidate(record,{**result,'artifact_disposition':None},**kwargs))
            bad=copy.deepcopy(result); bad['artifact_disposition']['status']='quarantined'
            self.assertIsNone(retained_candidate(record,bad,**kwargs))
            self.assertIsNone(retained_candidate(record,result,**{**kwargs,'scope':['other.py']}))
            self.assertIsNone(retained_candidate(record,result,**{**kwargs,'before':None}))
            self.assertIsNone(retained_candidate(record,result,**{**kwargs,'before':manifest(root,['file.py'])}))
            self.assertIsNone(retained_candidate(record,result,**{**kwargs,'before_dirty':None}))
            (root/'outside.py').write_text('unscoped change')
            self.assertIsNone(retained_candidate(record,result,**kwargs))

    def test_dirty_manifest_tracks_outside_deletion_and_rename(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'outside.py').write_text('outside')
            subprocess.run(['git','init','-q',str(root)],check=True)
            subprocess.run(['git','-C',str(root),'add','outside.py'],check=True)
            subprocess.run(['git','-C',str(root),'-c','user.name=Test','-c','user.email=test@local.invalid',
                            'commit','-qm','Synthetic baseline'],check=True)
            self.assertEqual(dirty_manifest(root),{})
            (root/'outside.py').unlink()  # Self-created synthetic test file only.
            self.assertEqual(dirty_manifest(root)['outside.py'],'<missing>')
            (root/'outside.py').write_text('outside')
            (root/'outside.py').rename(root/'renamed.py')
            changed=dirty_manifest(root)
            self.assertEqual(changed['outside.py'],'<missing>')
            self.assertIn('renamed.py',changed)

if __name__=='__main__': unittest.main()
