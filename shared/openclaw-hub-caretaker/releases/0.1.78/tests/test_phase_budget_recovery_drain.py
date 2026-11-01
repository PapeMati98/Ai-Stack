import json
import unittest
from unittest.mock import patch
import test_phase_resume_retry as fixtures
import curate_once as curator
import maintenance
from caretaker.config import load_config


class ProvenBudgetDrainTests(unittest.TestCase):
    def test_normal_drain_recovers_quarantined_interruption_once_without_new_maker(self):
        f=fixtures.PhaseResumeRetryTests();f.setUp();self.addCleanup(f.doCleanups)
        record=json.loads(f.phase_pointer.read_text(encoding='utf-8'))
        record.update(phase='REPAIR_STARTED',code_version='6d74ee300e9032e35c27ced6fd2f274e',
                      candidate=None,manifest=None,manifest_sha256=None,pins={})
        record['progress'].update(phase='REPAIR_STARTED',review_calls=1,
                                  assessment={'verdict':'REPAIR','findings':[]},repair_attempt=1)
        record['cache_sha256']=curator._phase_digest(record)
        f.phase_pointer.write_text(json.dumps(record),encoding='utf-8')
        raw=json.loads(f.db.execute('SELECT result FROM publications WHERE batch=?',(f.old_key,)).fetchone()[0])
        raw.update(status='QUARANTINED',reason='phase_resume_terminal_no_replay',original_reason='phase_resume_terminal_no_replay')
        f.db.execute('UPDATE publications SET status=?,failures=4,attempt=4,result=? WHERE batch=?',('QUARANTINED',json.dumps(raw),f.old_key))
        f.db.execute("UPDATE audit_members SET status='quarantined' WHERE cycle_id='cycle-old-phase'")
        f.db.commit()
        def invoke(config_path,paths):return f._run_curator(load_config(config_path),paths)
        patches=f._curator_patches()
        for p in patches:p.start()
        try:
            with patch.object(maintenance,'_invoke_curate',side_effect=invoke):
                first=maintenance.publication_drain(f.control,f.base,f.db,f.state,
                    now=fixtures.NOW,lane='legacy',recovery={'status':'NO_LOCK'})
                self.assertEqual(first['batches'][0]['status'],'PUBLISHED',first)
                self.assertEqual(f.maker.calls,1)
                self.assertEqual(f.reviewer.calls,1)
                self.assertEqual(f.db.execute('SELECT COUNT(*) FROM repaired_retry_tokens').fetchone()[0],1)
                maintenance.publication_drain(f.control,f.base,f.db,f.state,
                    now=fixtures.NOW,lane='legacy',recovery={'status':'NO_LOCK'})
                self.assertEqual(f.maker.calls,1)
                self.assertEqual(f.reviewer.calls,1)
        finally:
            for p in reversed(patches):p.stop()


if __name__=='__main__':unittest.main()
