"""0.1.29: only a fully verified dead-owner transaction rolls forward."""
import hashlib
import datetime as dt
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintenance
from caretaker.audit import ensure_tables as ensure_audit_tables
from caretaker.pubstate import batch_key, read_row, record_start
from caretaker.roll_forward import _process_alive, recover_stale_curator_lock


LOCK_ID = 'a' * 32
EVENT_ID = 'evt-20260926T194052643Z-12345678'
TX_ID = 'tx-' + 'b' * 32


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding='utf-8')


def _git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class Fixture:
    def __init__(self, root, *, journal=True):
        self.root = root
        self.vault = root / 'vault'
        # Keep the private Git index path below Windows MAX_PATH in this long checkout.
        self.state = root / 's'
        self.project_state = self.state / 'p'
        self.candidate = self.project_state / 'curation/r/candidate'
        self.candidate.mkdir(parents=True)
        self.lock = self.vault / 'governance/curator.lock'
        self.input = self.vault / 'proposals/openclaw/prop-a.md'
        self.output = self.vault / 'wiki/projects/example.md'
        self.event = self.vault / 'events/2026/09' / (EVENT_ID + '.json')
        _write(self.input, 'frozen proposal\n')
        _write(self.vault / 'seed', 'seed\n')
        for args in (('init', '-b', 'main'), ('config', 'core.autocrlf', 'false'),
                     ('config', 'user.name', 'Synthetic Test'),
                     ('config', 'user.email', 'test@example.invalid')):
            _git(root, *args)
        _git(root, 'add', '--', 'vault/seed', 'vault/proposals/openclaw/prop-a.md')
        _git(root, 'commit', '-m', 'seed')
        self.before = _git(root, 'rev-parse', 'HEAD')
        self.write_lock()
        self.db = sqlite3.connect(':memory:')
        self.batch = batch_key('example', ['proposals/openclaw/prop-a.md'])
        self.base = {'hub': {'vault': str(self.vault), 'script': str(root / 'knowledge.py'),
                             'python_executable': 'python'}}
        if journal:
            _write(self.output, 'reviewed publication\n')
            _write(self.event, '{"event_id":"' + EVENT_ID + '"}\n')
            self.manifest = self.candidate / 'publication.json'
            self.manifest.write_text(json.dumps({
                'vault_path': str(self.vault),
                'proposal_ids': ['prop-a'],
                'inputs': [{'path': 'proposals/openclaw/prop-a.md', 'sha256': _sha(self.input)}],
                'outputs': [{'path': 'wiki/projects/example.md', 'sha256': _sha(self.output)}],
            }), encoding='utf-8')
            _write(self.candidate / 'semantic-review.json', '{"reviewed":true}')
            (self.candidate / 'review.json').write_text(json.dumps({
                'verdict': 'PASS', 'maker': 'openclaw-caretaker',
                'manifest_sha256': _sha(self.manifest),
                'evidence': {'path': 'semantic-review.json',
                             'sha256': _sha(self.candidate / 'semantic-review.json')},
            }), encoding='utf-8')
            self.journal = self.candidate / '.curation-transactions' / (TX_ID + '.json')
            self.journal.parent.mkdir()
            self.journal.write_text(json.dumps({
                'transaction': TX_ID, 'status': 'applying', 'vault': str(self.vault),
                'manifest': str(self.manifest), 'manifest_sha256': _sha(self.manifest),
                'lock_id': LOCK_ID, 'agent': 'openclaw-caretaker', 'event_id': EVENT_ID,
                'files': [{'path': 'wiki/projects/example.md', 'after': _sha(self.output),
                           'state': 'replaced'},
                          {'path': 'events/2026/09/' + EVENT_ID + '.json',
                           'after': _sha(self.event), 'state': 'replaced'}],
            }), encoding='utf-8')
            ensure_audit_tables(self.db)
            self.db.execute("INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)",
                            ('cycle-1', 'test', '2026-09-26T19:40:00+00:00', 'ACTIVE', '{}', 'new'))
            self.db.execute("INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status) VALUES(?,?,?,?,?,?,?)",
                            ('cycle-1', 'prop-a', 'proposals/openclaw/prop-a.md',
                             _sha(self.input), 'example', 'group', 'pending'))
            self.db.commit()
            record_start(self.db, self.batch, '2026-09-26T19:40:00+00:00', 'fp', {
                'proposal_ids': ['prop-a'], 'paths': ['proposals/openclaw/prop-a.md'],
                'hashes': [_sha(self.input)], 'project': 'example',
                'state_dir': str(self.project_state), 'cycle_id': 'cycle-1',
            })

    def write_lock(self, *, agent='openclaw-caretaker', machine='TEST', pid=999999,
                   acquired_at='old'):
        if acquired_at == 'old':
            acquired_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        lock = {'lock_id': LOCK_ID, 'agent': agent, 'machine': machine,
                'process_id': pid}
        if acquired_at is not None:
            lock['acquired_at'] = acquired_at
        _write(self.lock, json.dumps(lock))

    def change_journal(self, **changes):
        doc = json.loads(self.journal.read_text(encoding='utf-8'))
        doc.update(changes)
        self.journal.write_text(json.dumps(doc), encoding='utf-8')

    def release(self, args, **kwargs):
        self.last_gateway_args = args
        os.rename(self.lock, self.state / 'released-curator-lock.json')
        return {'status': 'released'}

    def recover(self, lint='pass', release=None, alive=False, control=None, now=None):
        with patch('caretaker.roll_forward._machine_name', return_value='TEST'), \
                patch('caretaker.roll_forward._process_alive', return_value=alive), \
                patch('caretaker.hub.CanonicalKnowledgeGateway') as gateway, \
                patch('curate_once.command', side_effect=release or self.release) as command:
            gateway.return_value.lint.return_value.status = lint
            result = recover_stale_curator_lock(self.db, self.base, self.state,
                                                control=control, now=now)
            return result, command, gateway


class AutoRollForwardTests(unittest.TestCase):
    def test_fresh_foreign_agent_or_machine_lock_is_normal_block(self):
        now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
        for owner in ('agent', 'machine'):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as temp:
                f = Fixture(Path(temp))
                stamp = (now - dt.timedelta(minutes=10)).isoformat()
                if owner == 'agent':
                    f.write_lock(agent='another-agent', acquired_at=stamp)
                else:
                    f.write_lock(machine='ELSEWHERE', acquired_at=stamp)
                before = f.lock.read_bytes()
                result, command, gateway = f.recover(
                    now=now, control={'publication': {'stale_curator_lock_minutes': 15}})
                self.assertEqual(result, {'status': 'LOCK_FRESH'})
                drain = maintenance.publication_drain(
                    {'publication': {'enabled': True}}, f.base, f.db, f.state,
                    recovery=result)
                self.assertEqual((drain['status'], drain['reason']),
                                 ('BLOCKED', 'curator_lock_held'))
                self.assertEqual(f.lock.read_bytes(), before)
                command.assert_not_called()
                gateway.assert_not_called()

    def test_stale_unparsable_and_future_foreign_lock_require_review(self):
        now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
        for stamp in ((now - dt.timedelta(minutes=31)).isoformat(),
                      'not-a-date', None,
                      (now + dt.timedelta(minutes=1)).isoformat()):
            with self.subTest(stamp=stamp), tempfile.TemporaryDirectory() as temp:
                f = Fixture(Path(temp))
                f.write_lock(agent='another-agent', acquired_at=stamp)
                before = f.lock.read_bytes()
                result, command, gateway = f.recover(now=now)
                self.assertEqual(result['status'], 'REQUIRES_REVIEW')
                self.assertEqual(result['reason'], 'stale_curator_lock_requires_review')
                self.assertEqual(f.lock.read_bytes(), before)
                command.assert_not_called()
                gateway.assert_not_called()

    def test_real_process_probe_keeps_current_owner(self):
        self.assertTrue(_process_alive(os.getpid()))
        self.assertTrue(_process_alive(0))

    def test_fresh_dead_pid_skips_publication_without_degrading_or_mutating(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp))
            now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
            f.write_lock(acquired_at=(now - dt.timedelta(seconds=40)).isoformat())
            original_lock = f.lock.read_bytes()
            result, command, gateway = f.recover(now=now)
            self.assertEqual(result['status'], 'LOCK_FRESH')
            drain = maintenance.publication_drain({'publication': {'enabled': True}},
                                                   f.base, f.db, f.state, recovery=result)
            self.assertEqual(drain['status'], 'BLOCKED')
            self.assertEqual(drain['reason'], 'curator_lock_held')
            self.assertNotIn('stale_curator_lock_requires_review', json.dumps(drain))
            self.assertEqual(f.lock.read_bytes(), original_lock)
            self.assertEqual(_git(f.root, 'rev-parse', 'HEAD'), f.before)
            self.assertEqual(read_row(f.db, f.batch)['status'], 'RUNNING')
            command.assert_not_called()
            gateway.assert_not_called()

    def test_configured_threshold_controls_when_old_dead_pid_is_recovered(self):
        now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
        for minutes, expected in ((15, 'LOCK_FRESH'), (5, 'RECOVERED')):
            with self.subTest(minutes=minutes), tempfile.TemporaryDirectory() as temp:
                f = Fixture(Path(temp))
                f.write_lock(acquired_at=(now - dt.timedelta(minutes=10)).isoformat())
                result, command, _ = f.recover(
                    control={'publication': {'stale_curator_lock_minutes': minutes}}, now=now)
                self.assertEqual(result['status'], expected)
                if expected == 'LOCK_FRESH':
                    self.assertEqual(_git(f.root, 'rev-parse', 'HEAD'), f.before)
                    command.assert_not_called()
                else:
                    self.assertFalse(f.lock.exists())
                    command.assert_called_once()

    def test_missing_unparsable_or_future_acquisition_time_blocks_without_review(self):
        now = dt.datetime(2026, 9, 27, 12, tzinfo=dt.timezone.utc)
        for stamp in (None, 'not-a-date', (now + dt.timedelta(seconds=1)).isoformat()):
            with self.subTest(stamp=stamp), tempfile.TemporaryDirectory() as temp:
                f = Fixture(Path(temp))
                f.write_lock(acquired_at=stamp)
                result, command, gateway = f.recover(now=now)
                self.assertEqual(result['status'], 'LOCK_TIME_UNVERIFIED')
                drain = maintenance.publication_drain({'publication': {'enabled': True}},
                                                       f.base, f.db, f.state, recovery=result)
                self.assertEqual(drain['status'], 'BLOCKED')
                self.assertEqual(drain['reason'], 'curator_lock_held')
                self.assertEqual(_git(f.root, 'rev-parse', 'HEAD'), f.before)
                self.assertTrue(f.lock.exists())
                self.assertEqual(read_row(f.db, f.batch)['status'], 'RUNNING')
                command.assert_not_called()
                gateway.assert_not_called()

    def test_complete_replacements_checkpoint_exact_paths_release_and_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp))
            result, command, gateway = f.recover()
            self.assertEqual(result['status'], 'RECOVERED')
            self.assertEqual(result['event_id'], EVENT_ID)
            self.assertEqual(result['transaction'], TX_ID)
            self.assertTrue(result['released'])
            self.assertEqual(result['commit'], _git(f.root, 'rev-parse', 'HEAD'))
            self.assertEqual(set(_git(f.root, 'show', '--format=', '--name-only', 'HEAD').splitlines()),
                             {'vault/wiki/projects/example.md',
                              'vault/events/2026/09/' + EVENT_ID + '.json'})
            self.assertEqual(_git(f.root, 'show', 'HEAD:vault/wiki/projects/example.md'),
                             f.output.read_text(encoding='utf-8').strip())
            row = read_row(f.db, f.batch)
            self.assertEqual(row['status'], 'PUBLISHED')
            self.assertEqual(f.db.execute("SELECT status FROM audit_members").fetchone()[0],
                             'published')
            stored = json.loads(row['result'])
            self.assertEqual(stored['checkpoint']['status'], 'CHECKPOINTED')
            self.assertEqual(stored['recovery']['commit'], result['commit'])
            self.assertFalse(f.lock.exists())
            self.assertEqual(gateway.return_value.lint.call_count, 1)
            command.assert_called_once()
            self.assertIn('release-lock', f.last_gateway_args)
            self.assertNotIn('reviewed publication', json.dumps(result))

    def test_second_tick_does_not_commit_or_release_again(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp))
            first, _, _ = f.recover()
            second, command, _ = f.recover()
            self.assertEqual(first['status'], 'RECOVERED')
            self.assertEqual(second, {'status': 'NO_LOCK'})
            self.assertEqual(_git(f.root, 'rev-list', '--count', 'HEAD'), '2')
            command.assert_not_called()

    def test_gateway_release_without_json_is_verified_by_lock_absence(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp), journal=False)
            def release_without_json(args, **kwargs):
                f.release(args, **kwargs)
                raise json.JSONDecodeError('no JSON', '', 0)
            result, _, _ = f.recover(release=release_without_json)
            self.assertEqual(result['status'], 'RELEASED_NO_TRANSACTION')
            self.assertFalse(f.lock.exists())

    def test_retry_after_gateway_release_failure_keeps_one_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp))
            def fail(*args, **kwargs):
                raise RuntimeError('gateway unavailable')
            first, _, _ = f.recover(release=fail)
            self.assertEqual(first['status'], 'REQUIRES_REVIEW')
            self.assertEqual(read_row(f.db, f.batch)['status'], 'PUBLISHED')
            self.assertTrue(f.lock.exists())
            second, _, _ = f.recover()
            self.assertEqual(second['status'], 'RECOVERED')
            self.assertTrue(second['released'])
            self.assertEqual(_git(f.root, 'rev-list', '--count', 'HEAD'), '2')

    def test_live_foreign_and_other_machine_locks_are_untouched(self):
        details = {'live': 'live_or_unverified_owner',
                   'foreign': 'other_agent_lock',
                   'machine': 'other_machine_lock'}
        for kind, detail in details.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                f = Fixture(Path(temp))
                if kind == 'foreign':
                    f.write_lock(agent='another-agent')
                elif kind == 'machine':
                    f.write_lock(machine='ELSEWHERE')
                result, command, gateway = f.recover(alive=kind == 'live')
                self.assertEqual(result, {'status': 'REQUIRES_REVIEW',
                                          'reason': 'stale_curator_lock_requires_review',
                                          'detail': detail})
                drain = maintenance.publication_drain({'publication': {'enabled': True}},
                                                      f.base, f.db, f.state, recovery=result)
                self.assertEqual(drain['status'], 'DEGRADED')
                self.assertEqual(drain['reason'], 'stale_curator_lock_requires_review')
                self.assertTrue(f.lock.exists())
                self.assertEqual(_git(f.root, 'rev-parse', 'HEAD'), f.before)
                self.assertEqual(read_row(f.db, f.batch)['status'], 'RUNNING')
                command.assert_not_called()
                gateway.assert_not_called()

    def test_planned_and_mixed_journals_require_review(self):
        for kind in ('planned', 'mixed'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                f = Fixture(Path(temp))
                if kind == 'planned':
                    f.change_journal(status='planned')
                else:
                    doc = json.loads(f.journal.read_text(encoding='utf-8'))
                    doc['files'][0]['state'] = 'staged'
                    f.change_journal(files=doc['files'])
                self.assert_refused(f)

    def test_hash_and_review_mismatches_require_review(self):
        for kind in ('output', 'input', 'manifest', 'review'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                f = Fixture(Path(temp))
                if kind == 'output':
                    _write(f.output, 'changed output\n')
                elif kind == 'input':
                    _write(f.input, 'changed input\n')
                elif kind == 'manifest':
                    f.manifest.write_text(f.manifest.read_text(encoding='utf-8') + ' ', encoding='utf-8')
                else:
                    _write(f.candidate / 'semantic-review.json', 'changed evidence')
                self.assert_refused(f)

    def test_lint_errors_and_multiple_matching_journals_require_review(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assert_refused(Fixture(Path(temp)), lint='fail')
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp))
            another = f.journal.with_name('tx-' + 'c' * 32 + '.json')
            another.write_bytes(f.journal.read_bytes())
            self.assert_refused(f)

    def test_foreign_staged_change_refuses_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp))
            _write(f.root / 'foreign.txt', 'unrelated\n')
            _git(f.root, 'add', 'foreign.txt')
            self.assert_refused(f)
            self.assertEqual(_git(f.root, 'diff', '--cached', '--name-only'), 'foreign.txt')

    def test_no_journal_releases_only_when_vault_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp), journal=False)
            result, command, _ = f.recover()
            self.assertEqual(result, {'status': 'RELEASED_NO_TRANSACTION', 'released': True})
            self.assertFalse(f.lock.exists())
            self.assertEqual(_git(f.root, 'rev-parse', 'HEAD'), f.before)
            command.assert_called_once()
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp), journal=False)
            old = f.candidate / '.curation-transactions' / ('tx-' + 'd' * 32 + '.json')
            old.parent.mkdir()
            old.write_text(json.dumps({'status': 'committed', 'lock_id': 'old',
                                       'files': [{'path': 'seed', 'state': 'replaced'}]}),
                           encoding='utf-8')
            self.assertEqual(f.recover()[0]['status'], 'RELEASED_NO_TRANSACTION')
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp), journal=False)
            _write(f.vault / 'wiki/projects/unknown.md', 'possibly published\n')
            self.assert_refused(f, has_batch=False)
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp), journal=False)
            path = f.candidate / '.curation-transactions' / ('tx-' + 'c' * 32 + '.json')
            path.parent.mkdir()
            path.write_text(json.dumps({'status': 'applying', 'lock_id': 'other',
                                        'files': []}), encoding='utf-8')
            self.assert_refused(f, has_batch=False)

    def test_degraded_drain_precedes_running_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp))
            f.change_journal(status='planned')
            with patch('caretaker.roll_forward._machine_name', return_value='TEST'), \
                    patch('caretaker.roll_forward._process_alive', return_value=False), \
                    patch('curate_once.command') as command:
                out = maintenance.publication_drain({'publication': {'enabled': True}},
                                                    f.base, f.db, f.state)
            self.assertEqual(out['status'], 'DEGRADED')
            self.assertEqual(out['reason'], 'stale_curator_lock_requires_review')
            self.assertEqual(read_row(f.db, f.batch)['status'], 'RUNNING')
            command.assert_not_called()

    def test_tick_report_exposes_review_reason_without_file_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 's'
            state.mkdir()
            vault = root / 'vault'
            vault.mkdir()
            base = root / 'base.json'
            base.write_text(json.dumps({'hub': {'vault': str(vault),
                                                'script': str(root / 'knowledge.py')}}), encoding='utf-8')
            control = {'state_dir': str(state), 'base_config': str(base),
                       'sources': [], 'known_projects': [], 'publication': {'enabled': False}}
            recovery = {'status': 'REQUIRES_REVIEW',
                        'reason': 'stale_curator_lock_requires_review',
                        'detail': 'recovery_output_hash_mismatch'}
            with patch('caretaker.roll_forward.recover_stale_curator_lock',
                       return_value=recovery):
                report = maintenance._locked_tick(control, state)
            self.assertEqual(report['status'], 'DEGRADED')
            self.assertEqual(report['curator_lock_recovery'], recovery)
            self.assertEqual(json.loads((state / 'latest.json').read_text(encoding='utf-8'))
                             ['curator_lock_recovery'], recovery)
            self.assertNotIn('reviewed publication', json.dumps(report))

    def test_tick_keeps_fresh_lock_as_normal_blocked_publication(self):
        for new_lane in (False, True):
          with self.subTest(new_lane=new_lane), tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / 's'
            state.mkdir()
            vault = root / 'vault'
            vault.mkdir()
            base = root / 'base.json'
            base.write_text(json.dumps({'hub': {'vault': str(vault),
                                                'script': str(root / 'knowledge.py')}}), encoding='utf-8')
            control = {'state_dir': str(state), 'base_config': str(base),
                       'sources': [], 'known_projects': [], 'publication': {'enabled': True}}
            if new_lane:
                control['new_event_epoch'] = {'enabled': True, 'activate': True}
            recovery = {'status': 'LOCK_FRESH'}
            with patch('caretaker.roll_forward.recover_stale_curator_lock',
                       return_value=recovery):
                report = maintenance._locked_tick(control, state)
            self.assertEqual(report['status'], 'OK')
            publication = report['new_lane']['publication'] if new_lane else report['publication']
            self.assertEqual(publication['status'], 'BLOCKED')
            self.assertEqual(publication['reason'], 'curator_lock_held')
            if new_lane:
                self.assertEqual(report['new_lane']['status'], 'LOCK_HELD')
            self.assertEqual(report['curator_lock_recovery'], recovery)

    def test_fleet_append_maker_with_independent_reviewer_rolls_forward_and_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp))
            diag = {
                'maker_route': 'fleet_append',
                'maker_agent': 'claude',
                'maker_lineage': 'claude',
                'primary_model': 'sonnet',
            }
            (f.candidate.parent / 'diagnostics.json').write_text(json.dumps(diag), encoding='utf-8')
            review = {
                'verdict': 'PASS',
                'maker': 'claude',
                'reviewer': {'id': 'codex-attempt-1', 'lineage': 'codex'},
                'manifest_sha256': _sha(f.manifest),
                'evidence': {'path': 'semantic-review.json',
                             'sha256': _sha(f.candidate / 'semantic-review.json')},
            }
            (f.candidate / 'review.json').write_text(json.dumps(review), encoding='utf-8')
            result, command, gateway = f.recover()
            self.assertEqual(result['status'], 'RECOVERED')
            self.assertTrue(result['released'])
            self.assertFalse(f.lock.exists())
            self.assertEqual(read_row(f.db, f.batch)['status'], 'PUBLISHED')
            command.assert_called_once()
            gateway.return_value.lint.assert_called_once()

    def test_fleet_append_same_lineage_refuses_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp))
            diag = {
                'maker_route': 'fleet_append',
                'maker_agent': 'claude',
                'maker_lineage': 'claude',
            }
            (f.candidate.parent / 'diagnostics.json').write_text(json.dumps(diag), encoding='utf-8')
            review = {
                'verdict': 'PASS',
                'maker': 'claude',
                'reviewer': {'id': 'claude-3-opus', 'lineage': 'claude'},
                'manifest_sha256': _sha(f.manifest),
                'evidence': {'path': 'semantic-review.json',
                             'sha256': _sha(f.candidate / 'semantic-review.json')},
            }
            (f.candidate / 'review.json').write_text(json.dumps(review), encoding='utf-8')
            result, command, gateway = f.recover()
            self.assertEqual(result['status'], 'REQUIRES_REVIEW')
            self.assertEqual(result['detail'], 'recovery_review_same_lineage')
            self.assertTrue(f.lock.exists())

    def test_fleet_append_manifest_hash_mismatch_refuses_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(Path(temp))
            diag = {
                'maker_route': 'fleet_append',
                'maker_agent': 'claude',
                'maker_lineage': 'claude',
            }
            (f.candidate.parent / 'diagnostics.json').write_text(json.dumps(diag), encoding='utf-8')
            review = {
                'verdict': 'PASS',
                'maker': 'claude',
                'reviewer': {'id': 'codex-attempt-1', 'lineage': 'codex'},
                'manifest_sha256': '0' * 64,
                'evidence': {'path': 'semantic-review.json',
                             'sha256': _sha(f.candidate / 'semantic-review.json')},
            }
            (f.candidate / 'review.json').write_text(json.dumps(review), encoding='utf-8')
            result, command, gateway = f.recover()
            self.assertEqual(result['status'], 'REQUIRES_REVIEW')
            self.assertEqual(result['detail'], 'recovery_review_not_bound')
            self.assertTrue(f.lock.exists())

    def assert_refused(self, f, *, lint='pass', has_batch=True):
        result, command, _ = f.recover(lint=lint)
        self.assertEqual(result['status'], 'REQUIRES_REVIEW', result)
        self.assertEqual(result['reason'], 'stale_curator_lock_requires_review')
        self.assertTrue(f.lock.exists())
        self.assertEqual(_git(f.root, 'rev-parse', 'HEAD'), f.before)
        command.assert_not_called()
        if has_batch:
            self.assertEqual(read_row(f.db, f.batch)['status'], 'RUNNING')


if __name__ == '__main__':
    unittest.main()
