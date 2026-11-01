"""0.1.17: a failed local checkpoint after a canonical publication is PENDING, never a failed batch."""
import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import curate_once
import maintenance
from caretaker.publication import PublicationError
from caretaker.pubstate import ensure_pub_tables


@contextlib.contextmanager
def _no_lock(*args, **kwargs):
    yield 'lock-test'


def _db_with_pending(receipt_dir):
    db = sqlite3.connect(':memory:')
    ensure_pub_tables(db)
    result = {'status': 'PUBLISHED', 'publication': {'status': 'PUBLISHED', 'event_id': 'evt-1'},
              'checkpoint': {'status': 'PENDING', 'reason': 'foreign_staged_changes_keep_pending',
                             'receipt_dir': str(receipt_dir)}}
    db.execute("INSERT INTO publications(batch,status,failures,result) VALUES(?,?,?,?)",
               ('b1', 'PUBLISHED', 0, json.dumps(result)))
    db.commit()
    return db


class CheckpointOrPendingTests(unittest.TestCase):
    def test_any_checkpoint_failure_becomes_pending(self):
        for exc in (PublicationError('foreign_staged_changes_keep_pending'), OSError('disk'), TimeoutError()):
            with self.subTest(exc=type(exc).__name__), tempfile.TemporaryDirectory() as temp:
                with patch('curate_once.checkpoint_publication', side_effect=exc):
                    out = curate_once.checkpoint_or_pending({'event_id': 'evt-x'}, Path(temp), Path(temp))
                self.assertEqual(out['status'], 'PENDING')
                self.assertEqual(out['receipt_dir'], str(Path(temp)))
        with tempfile.TemporaryDirectory() as temp:
            with patch('curate_once.checkpoint_publication',
                       side_effect=PublicationError('foreign_staged_changes_keep_pending')):
                out = curate_once.checkpoint_or_pending({'event_id': 'evt-x'}, Path(temp), Path(temp))
            self.assertEqual(out['reason'], 'foreign_staged_changes_keep_pending')

    def test_success_passes_through(self):
        done = {'status': 'CHECKPOINTED', 'head': 'abc'}
        with tempfile.TemporaryDirectory() as temp, patch('curate_once.checkpoint_publication', return_value=done):
            self.assertEqual(curate_once.checkpoint_or_pending({'event_id': 'e'}, Path(temp), Path(temp)), done)


class PendingCheckpointDrainTests(unittest.TestCase):
    def test_pending_checkpoint_is_completed_and_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            db = _db_with_pending(Path(temp))
            done = {'status': 'CHECKPOINTED', 'event_id': 'evt-1', 'head': 'abc'}
            with patch('curate_once.curator_lock', _no_lock), \
                    patch('caretaker.checkpoint.checkpoint_publication', return_value=done) as cp:
                remaining = maintenance.complete_pending_checkpoints(db, {'hub': {'vault': temp, 'script': 'k.py'}})
            self.assertEqual(remaining, [])
            cp.assert_called_once()
            stored = json.loads(db.execute("SELECT result FROM publications WHERE batch='b1'").fetchone()[0])
            self.assertEqual(stored['checkpoint']['status'], 'CHECKPOINTED')
            self.assertTrue(stored['checkpoint']['completed_after_pending'])
            self.assertEqual(maintenance._pending_checkpoint_rows(db), [])

    def test_still_pending_blocks_every_new_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            db = _db_with_pending(Path(temp))
            with patch('curate_once.curator_lock', _no_lock), \
                    patch('caretaker.checkpoint.checkpoint_publication',
                          side_effect=PublicationError('foreign_staged_changes_keep_pending')), \
                    patch('caretaker.audit.summarize', return_value={}), \
                    patch('maintenance.publication_tick') as tick:
                out = maintenance.publication_drain({'publication': {'enabled': True}},
                                                    {'hub': {'vault': temp, 'script': 'k.py'}}, db, Path(temp))
            self.assertEqual(out['status'], 'CHECKPOINT_PENDING')
            self.assertEqual(out['model_calls'], 0)
            self.assertEqual(out['pending_checkpoints'][0]['reason'], 'foreign_staged_changes_keep_pending')
            tick.assert_not_called()
            self.assertEqual(len(maintenance._pending_checkpoint_rows(db)), 1)

    def test_unexpected_error_and_missing_receipt_stay_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            db = _db_with_pending(Path(temp))
            with patch('curate_once.curator_lock', _no_lock), \
                    patch('caretaker.checkpoint.checkpoint_publication', side_effect=OSError('locked file')):
                remaining = maintenance.complete_pending_checkpoints(db, {'hub': {'vault': temp, 'script': 'k.py'}})
            self.assertEqual(remaining[0]['reason'], 'OSError')
        db = _db_with_pending(Path(__file__).resolve().parents[1] / '.test-tmp' /
                              'missing-pending-checkpoint-receipt')
        with patch('curate_once.curator_lock', _no_lock):
            remaining = maintenance.complete_pending_checkpoints(db, {'hub': {'vault': '.', 'script': 'k.py'}})
        self.assertEqual(remaining[0]['reason'], 'pending_checkpoint_receipt_missing')


class PostPublicationStateTests(unittest.TestCase):
    """Independent review of 0.1.17: a published batch must never look like a failed one."""

    def test_reconciled_interrupted_run_leaves_checkpoint_pending(self):
        from caretaker.pubstate import reconcile_running, record_start
        db = sqlite3.connect(':memory:')
        ensure_pub_tables(db)
        record_start(db, 'b9', '2026-09-23T07:00:00+00:00', 'fp', {'proposal_ids': ['p']})
        journal = Path('D:/state/curation/run9/candidate/.curation-transactions/tx-9.json')
        out = reconcile_running(db, 'b9', 'committed', {'journal_path': str(journal), 'event_id': 'evt-9'})
        self.assertEqual(out['status'], 'PUBLISHED')
        [(batch, value)] = maintenance._pending_checkpoint_rows(db)
        self.assertEqual(batch, 'b9')
        self.assertEqual(value['publication'], {'status': 'PUBLISHED', 'transaction': str(journal), 'event_id': 'evt-9'})
        self.assertEqual(value['checkpoint']['receipt_dir'], str(Path('D:/state/curation/run9')))

    def test_lock_release_failure_after_publication_keeps_the_batch_published(self):
        @contextlib.contextmanager
        def lock_that_fails_on_release(*args, **kwargs):
            yield 'lock-x'
            raise PublicationError('curator_lock_changed_do_not_release')

        cfg = type('Cfg', (), {'hub': type('Hub', (), {'script': 'k.py', 'vault': 'V', 'python_executable': 'py'})()})()
        with tempfile.TemporaryDirectory() as temp, \
                patch('curate_once.curator_lock', lock_that_fails_on_release), \
                patch('curate_once.command', return_value={'status': 'PUBLISHED', 'event_id': 'evt-x'}), \
                patch('curate_once.checkpoint_or_pending', return_value={'status': 'CHECKPOINTED'}):
            result = curate_once._publish_under_lock(cfg, Path(temp) / 'm.json', Path(temp) / 'r.json', Path(temp), {})
        self.assertEqual(result['status'], 'PUBLISHED')
        self.assertEqual(result['reason'], 'curator_lock_release_failed')
        self.assertEqual(result['lock_release']['reason'], 'curator_lock_changed_do_not_release')

    def test_failure_before_publication_still_raises(self):
        cfg = type('Cfg', (), {'hub': type('Hub', (), {'script': 'k.py', 'vault': 'V', 'python_executable': 'py'})()})()
        with tempfile.TemporaryDirectory() as temp, patch('curate_once.curator_lock', _no_lock), \
                patch('curate_once.command', return_value={'status': 'REFUSED'}):
            with self.assertRaisesRegex(PublicationError, 'canonical_publish_not_complete'):
                curate_once._publish_under_lock(cfg, Path(temp) / 'm.json', Path(temp) / 'r.json', Path(temp), {})

    def test_pending_checkpoint_marks_the_published_batch_for_attention(self):
        cfg = type('Cfg', (), {'hub': type('Hub', (), {'script': 'k.py', 'vault': 'V', 'python_executable': 'py'})()})()
        with tempfile.TemporaryDirectory() as temp, patch('curate_once.curator_lock', _no_lock), \
                patch('curate_once.command', return_value={'status': 'PUBLISHED', 'event_id': 'evt-x'}), \
                patch('curate_once.checkpoint_publication', side_effect=PublicationError('foreign_staged_changes_keep_pending')):
            result = curate_once._publish_under_lock(cfg, Path(temp) / 'm.json', Path(temp) / 'r.json', Path(temp), {})
        self.assertEqual(result['status'], 'PUBLISHED')
        self.assertEqual(result['reason'], 'checkpoint_pending')
        self.assertIn('checkpoint_pending', maintenance.POST_PUBLICATION_ATTENTION)


class RepairPromptBoundTests(unittest.TestCase):
    def test_repair_prompt_fits_a_windows_command_line(self):
        prompt = curate_once._format_repair_prompt('x' * 24000, 'd' * 64, 'reason', 'p' * 6000, 1)
        self.assertLessEqual(len(prompt), curate_once.REPAIR_PROMPT_MAX_CHARS)
        self.assertIn('ppp', prompt)
        small = curate_once._format_repair_prompt('task', 'd' * 64, 'reason', 'previous answer', 1)
        self.assertIn('previous answer', small)


if __name__ == '__main__':
    unittest.main()
