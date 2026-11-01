import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from helpdesk_tick import run_tick, HelpdeskStore


def stamp(hour):
    return datetime(2026, 9, 18, hour, tzinfo=timezone.utc).timestamp()


def valid():
    return {'status': 'OK', 'project': 'example-workspace', 'environment': 'prod', 'read_only': True,
            'items': [{'issue_id': '56', 'revision': '2026-09-16T12:00:00Z',
                       'status': 'reported', 'priority': 'unspecified',
                       'url': 'https://workspace.example.invalid/helpdesk',
                       'body': 'synthetic excluded body'}]}


class TickIntegrationTests(unittest.TestCase):
    def test_no_work_before_due(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'state.db'
            result = run_tick(db, now=stamp(14), readers={})
            self.assertEqual(result['status'], 'NOT_DUE')
            self.assertFalse(db.exists())

    def test_blocked_source_remains_retryable_success_deduplicates(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'state.db'
            counts = {'s': 0, 'o': 0}
            def s():
                counts['s'] += 1
                return valid()
            def o():
                counts['o'] += 1
                return {'status': 'BLOCKED'}
            readers = {'example-workspace': s, 'example-crm': o}
            first = run_tick(db, now=stamp(15), readers=readers)
            again = run_tick(db, now=stamp(16), readers=readers)
            self.assertEqual(first['pending_requests'], 1)
            self.assertEqual(again['pending_requests'], 1)
            self.assertEqual(counts, {'s': 1, 'o': 2})
            self.assertEqual(first['telegram'], 'UNCONNECTED')
            store = HelpdeskStore(str(db))
            try:
                self.assertEqual(len(store.outbox.pending()), 1)
                self.assertEqual(store.conn.execute('SELECT count(*) FROM runs').fetchone()[0], 1)
            finally:
                store.close()
            self.assertNotIn(b'synthetic excluded body', db.read_bytes())

    def test_reboot_catchup_before_due_reads_once_and_stays_quiet(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'state.db'
            HelpdeskStore(str(db)).close()
            counts = {'s': 0, 'o': 0}
            def s():
                counts['s'] += 1
                return valid()
            def o():
                counts['o'] += 1
                return {'status': 'BLOCKED'}
            readers = {'example-workspace': s, 'example-crm': o}
            first = run_tick(db, now=stamp(6), readers=readers)
            again = run_tick(db, now=stamp(7), readers=readers)
            self.assertEqual(first['status'], 'BLOCKED')  # crm still blocked
            self.assertEqual(first['projects']['example-workspace']['status'], 'CATCHUP_COLLECTED')
            self.assertEqual(again['projects']['example-workspace']['status'], 'QUIET_NO_CATCHUP')
            self.assertEqual(counts['s'], 1)
            self.assertEqual(counts['o'], 2)  # blocked source stays retryable
            store = HelpdeskStore(str(db))
            try:
                self.assertEqual(store.conn.execute('SELECT count(*) FROM runs').fetchone()[0], 1)
                self.assertEqual(len(store.list_requests('proposed')), 1)
            finally:
                store.close()
            self.assertNotIn(b'synthetic excluded body', db.read_bytes())

    def test_concurrent_catchup_ticks_do_not_duplicate_source_reads(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'state.db'
            HelpdeskStore(str(db)).close()
            counts = {'s': 0}
            lock = threading.Lock()
            entered = threading.Event()
            release = threading.Event()

            def s():
                with lock:
                    counts['s'] += 1
                entered.set()
                release.wait(2)
                return valid()

            def o():
                return {'status': 'BLOCKED'}

            readers = {'example-workspace': s, 'example-crm': o}
            results = []
            errors = []

            def worker():
                try:
                    results.append(run_tick(db, now=stamp(6), readers=readers))
                except BaseException as exc:
                    errors.append(exc)

            first = threading.Thread(target=worker)
            second = threading.Thread(target=worker)
            first.start()
            self.assertTrue(entered.wait(2))
            second.start()
            try:
                release.set()
                first.join(5)
                second.join(5)
            finally:
                release.set()
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(counts['s'], 1)
            statuses = {r['projects']['example-workspace']['status'] for r in results}
            self.assertIn('CATCHUP_COLLECTED', statuses)
            self.assertTrue(statuses <= {'CATCHUP_COLLECTED', 'QUIET_DUPLICATE_RUN', 'QUIET_NO_CATCHUP'})
            store = HelpdeskStore(str(db))
            try:
                self.assertEqual(store.conn.execute('SELECT count(*) FROM runs').fetchone()[0], 1)
                self.assertEqual(len(store.list_requests('proposed')), 1)
            finally:
                store.close()

    def test_tick_content_revision_despite_unchanged_updated_at(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'state.db'
            def reader(body):
                item = dict(valid()['items'][0])
                item['body'] = body
                payload = dict(valid())
                payload['items'] = [item]
                return payload
            day1 = datetime(2026, 9, 18, 15, tzinfo=timezone.utc).timestamp()
            day2 = datetime(2026, 9, 19, 15, tzinfo=timezone.utc).timestamp()
            first = run_tick(db, now=day1, readers={
                'example-workspace': lambda: reader('500 on submit'),
                'example-crm': lambda: {'status': 'BLOCKED'}})
            second = run_tick(db, now=day2, readers={
                'example-workspace': lambda: reader('500 on submit, retry helps'),
                'example-crm': lambda: {'status': 'BLOCKED'}})
            self.assertEqual(first['pending_requests'], 1)
            self.assertEqual(second['pending_requests'], 2)
            self.assertNotIn(b'500 on submit', db.read_bytes())

    def test_wrong_environment_does_not_create_success_marker(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / 'state.db'
            bad = valid()
            bad['environment'] = 'dev'
            result = run_tick(db, now=stamp(15), readers={
                'example-workspace': lambda: bad, 'example-crm': lambda: {'status': 'BLOCKED'}})
            self.assertEqual(result['pending_requests'], 0)
            store = HelpdeskStore(str(db))
            try:
                self.assertEqual(store.conn.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
