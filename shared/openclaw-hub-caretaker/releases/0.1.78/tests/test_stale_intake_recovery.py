"""Stale intake-lock recovery through the trusted knowledge.py helpers only.

Covers the 2026-10-04 incident shape: a same-host dead-PID intake lock older
than 900s permanently blocked OpenClaw publication checkpoint retries because
caretaker acquisition never invoked guarded recovery. Live, young, foreign and
malformed locks must remain; a replacement/identity race must not release the
replacement; a pending retry must not start a new maker/reviewer/publication.
"""
import contextlib
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from caretaker.checkpoint import (
    INTAKE_LOCK_NAME,
    _acquire_intake_lock,
    _machine_name,
    _recover_stale_intake_lock,
    _trusted_gateway,
    checkpoint_publication,
)
from caretaker.publication import PublicationError

WORKTREE = Path(__file__).resolve().parents[2]
CANONICAL_KNOWLEDGE = WORKTREE / 'canonical_readonly' / 'knowledge.py'


def _git(root, *args):
    return subprocess.run(['git', '-c', 'core.autocrlf=false', '-C', str(root), *args],
                          check=True, capture_output=True)


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode('utf-8') if isinstance(data, str) else data)


def _commit(root, message, *paths):
    _git(root, 'add', *(paths or ('-A',)))
    _git(root, 'commit', '-m', message)


def _row(vault, name):
    return {'path': name, 'after': hashlib.sha256((vault / name).read_bytes()).hexdigest()}


def _publication(root, vault, rows, event='evt-test'):
    receipt = root / 'receipts'
    receipt.mkdir(exist_ok=True)
    journal = receipt / 'tx.json'
    journal.write_text(json.dumps({'status': 'committed', 'event_id': event,
                                   'vault': str(vault), 'files': rows}))
    return {'status': 'PUBLISHED', 'transaction': str(journal), 'event_id': event}, receipt


def _lock_path(root):
    return root / '.git' / INTAKE_LOCK_NAME


def _install_gateway(root):
    scripts = root / 'skills' / 'llm-wiki' / 'scripts'
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CANONICAL_KNOWLEDGE, scripts / 'knowledge.py')
    return scripts / 'knowledge.py'


def _dead_pid():
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    return proc.pid


def _iso_ago(seconds):
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
    return moment.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _write_lock(root, *, lock_id='stale-intake', pid=None, machine=None, age_seconds=None,
                agent='openclaw-caretaker', raw=None):
    lock = _lock_path(root)
    if raw is not None:
        lock.write_text(raw, encoding='utf-8')
        return lock
    payload = {'lock_id': lock_id, 'pid': pid, 'machine': machine,
               'created_at': _iso_ago(age_seconds), 'agent': agent}
    lock.write_text(json.dumps(payload) + '\n', encoding='utf-8')
    return lock


def _released_locks(root):
    out = []
    for path in (root / '.git').glob(INTAKE_LOCK_NAME + '.release-*'):
        try:
            out.append((json.loads(path.read_text(encoding='utf-8')), path))
        except (OSError, ValueError):
            out.append((None, path))
    return out


@contextlib.contextmanager
def _no_lock(*args, **kwargs):
    yield 'lock-test'


def _hub_repo(root, *, install_gateway=True, policy=None, commit_policy=True,
              pages=('wiki/projects/p.md',)):
    vault = root / 'vault'
    (vault / 'wiki' / 'projects').mkdir(parents=True)
    for args in [('init', '-b', 'main'), ('config', 'user.name', 'Synthetic Test'),
                 ('config', 'user.email', 'test@example.invalid')]:
        _git(root, *args)
    if install_gateway:
        _install_gateway(root)
    settings = {'auto_checkpoint_intake': True, 'checkpoint_mode': 'local-commit-only',
                'curator_machine': _machine_name()}
    if policy is not None:
        settings.update(policy)
    _write(vault / 'knowledge-vault.json', json.dumps(settings, indent=2))
    _write(vault / 'log.md', 'seed\n')
    to_commit = ['vault']
    if (root / 'skills').exists():
        to_commit.append('skills')
    _commit(root, 'seed', *to_commit)
    if not commit_policy:
        _git(root, 'rm', '--cached', '-q', 'vault/knowledge-vault.json')
        _git(root, 'commit', '-m', 'drop policy from HEAD')
    for page in pages:
        _write(vault / page, 'reviewed ' + page)
    publication, receipt = _publication(root, vault, [_row(vault, page) for page in pages])
    return vault, (publication, receipt)


class StaleIntakeRecoveryTests(unittest.TestCase):
    def test_stale_same_host_dead_lock_is_recovered_and_exact_checkpoint_proceeds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, (publication, receipt) = _hub_repo(root)
            dead = _dead_pid()
            lock = _write_lock(root, lock_id='stale-intake', pid=dead,
                               machine=_machine_name(), age_seconds=4 * 3600)
            original_bytes = lock.read_bytes()
            with patch('caretaker.checkpoint.LOCK_WAIT_SECONDS', 0.3):
                result = checkpoint_publication(publication, vault, receipt)
            self.assertEqual(result['status'], 'CHECKPOINTED')
            self.assertEqual(result['paths'], ['vault/wiki/projects/p.md'])
            self.assertFalse(lock.exists(), 'the stale active name was recovered')
            preserved = [path for payload, path in _released_locks(root)
                         if payload and payload.get('lock_id') == 'stale-intake']
            self.assertEqual(len(preserved), 1, 'prior lock bytes stay as an audit artifact')
            self.assertEqual(preserved[0].read_bytes(), original_bytes)
            self.assertEqual(json.loads(preserved[0].read_text())['pid'], dead)

    def test_alive_lock_is_never_broken(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, (publication, receipt) = _hub_repo(root)
            lock = _write_lock(root, lock_id='alive-intake', pid=os.getpid(),
                               machine=_machine_name(), age_seconds=4 * 3600)
            before = lock.read_bytes()
            with patch('caretaker.checkpoint.LOCK_WAIT_SECONDS', 0.3):
                with self.assertRaisesRegex(PublicationError, 'checkpoint_lock_busy'):
                    checkpoint_publication(publication, vault, receipt)
            self.assertEqual(lock.read_bytes(), before)

    def test_young_lock_is_never_broken(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, (publication, receipt) = _hub_repo(root)
            lock = _write_lock(root, lock_id='young-intake', pid=_dead_pid(),
                               machine=_machine_name(), age_seconds=60)
            before = lock.read_bytes()
            with patch('caretaker.checkpoint.LOCK_WAIT_SECONDS', 0.3):
                with self.assertRaisesRegex(PublicationError, 'checkpoint_lock_busy'):
                    checkpoint_publication(publication, vault, receipt)
            self.assertEqual(lock.read_bytes(), before)

    def test_foreign_machine_lock_is_never_broken(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, (publication, receipt) = _hub_repo(root)
            lock = _write_lock(root, lock_id='foreign-intake', pid=_dead_pid(),
                               machine='NOT-' + _machine_name(), age_seconds=4 * 3600)
            before = lock.read_bytes()
            with patch('caretaker.checkpoint.LOCK_WAIT_SECONDS', 0.3):
                with self.assertRaisesRegex(PublicationError, 'checkpoint_lock_busy'):
                    checkpoint_publication(publication, vault, receipt)
            self.assertEqual(lock.read_bytes(), before)

    def test_malformed_lock_is_never_broken(self):
        for label, raw in (('not-json', 'not a lock\n'),
                           ('missing-lock-id', json.dumps({'pid': _dead_pid()}) + '\n'),
                           ('empty', '')):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                vault, (publication, receipt) = _hub_repo(root)
                lock = _write_lock(root, raw=raw)
                before = lock.read_bytes()
                with patch('caretaker.checkpoint.LOCK_WAIT_SECONDS', 0.3):
                    with self.assertRaisesRegex(PublicationError, 'checkpoint_lock_busy'):
                        checkpoint_publication(publication, vault, receipt)
                self.assertEqual(lock.read_bytes(), before)

    def test_replacement_identity_race_leaves_the_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, (publication, receipt) = _hub_repo(root)
            lock = _write_lock(root, lock_id='stale-intake', pid=_dead_pid(),
                               machine=_machine_name(), age_seconds=4 * 3600)
            gateway = _trusted_gateway(vault)
            self.assertIsNotNone(gateway)
            original_snapshot = gateway.lock_snapshot
            replacement = json.dumps({'lock_id': 'replacement-live', 'pid': os.getpid(),
                                      'machine': _machine_name(), 'created_at': _iso_ago(5),
                                      'agent': 'other-writer'}) + '\n'

            def snapshot_then_replace(lock_path):
                result = original_snapshot(lock_path)
                if result is not None:
                    lock_path.write_text(replacement, encoding='utf-8')
                return result

            with patch.object(gateway, 'lock_snapshot', snapshot_then_replace):
                with patch('caretaker.checkpoint.LOCK_WAIT_SECONDS', 0.3):
                    with self.assertRaisesRegex(PublicationError, 'checkpoint_lock_busy'):
                        checkpoint_publication(publication, vault, receipt)
            self.assertTrue(lock.exists())
            self.assertEqual(json.loads(lock.read_text())['lock_id'], 'replacement-live')
            for payload, _path in _released_locks(root):
                self.assertNotEqual(payload and payload.get('lock_id'), 'replacement-live')

    def test_recovery_requires_trusted_gateway_and_owner_policy(self):
        cases = (
            ('no_gateway', {'install_gateway': False},
             {'auto_checkpoint_intake': True, 'checkpoint_mode': 'local-commit-only',
              'curator_machine': _machine_name()}),
            ('policy_disabled', {'install_gateway': True},
             {'auto_checkpoint_intake': False, 'checkpoint_mode': 'local-commit-only',
              'curator_machine': _machine_name()}),
            ('not_owner_machine', {'install_gateway': True},
             {'auto_checkpoint_intake': True, 'checkpoint_mode': 'local-commit-only',
              'curator_machine': 'NOT-' + _machine_name()}),
            ('wrong_mode', {'install_gateway': True},
             {'auto_checkpoint_intake': True, 'checkpoint_mode': 'remote-push',
              'curator_machine': _machine_name()}),
            ('untrusted_policy_source', {'install_gateway': True, 'commit_policy': False},
             {'auto_checkpoint_intake': True, 'checkpoint_mode': 'local-commit-only',
              'curator_machine': _machine_name()}),
        )
        for label, setup, policy in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                vault, (publication, receipt) = _hub_repo(root, policy=policy, **setup)
                lock = _write_lock(root, lock_id='stale-intake', pid=_dead_pid(),
                                   machine=_machine_name(), age_seconds=4 * 3600)
                before = lock.read_bytes()
                with patch('caretaker.checkpoint.LOCK_WAIT_SECONDS', 0.3):
                    with self.assertRaisesRegex(PublicationError, 'checkpoint_lock_busy'):
                        checkpoint_publication(publication, vault, receipt)
                self.assertEqual(lock.read_bytes(), before)
                self.assertFalse(_released_locks(root))

    def test_gateway_is_not_taken_from_an_untrusted_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, (publication, receipt) = _hub_repo(root, install_gateway=False)
            # A lookalike next to the vault must not become the recovery gateway.
            decoy = root / 'skills' / 'llm-wiki' / 'scripts'
            decoy.mkdir(parents=True, exist_ok=True)
            _write(decoy / 'curation.py', 'raise SystemExit("decoy")\n')
            _write(root / 'vault' / 'knowledge.py',
                   'CHECKPOINT_MODE = "local-commit-only"\n'
                   'def checkpoint_policy(vault):\n'
                   '    return {"enabled": True, "mode": "local-commit-only",'
                   ' "owner_machine": True, "policy_source": "git-head", "machine": "X"}\n'
                   'def recover_stale_checkpoint_lock(lock_path, policy, *, requested):\n'
                   '    lock_path.unlink()\n'
                   '    return True\n'
                   'lock_snapshot = remove_owned_checkpoint_lock = lambda *a, **k: None\n')
            self.assertIsNone(_trusted_gateway(vault))
            lock = _write_lock(root, lock_id='stale-intake', pid=_dead_pid(),
                               machine=_machine_name(), age_seconds=4 * 3600)
            before = lock.read_bytes()
            self.assertFalse(_recover_stale_intake_lock(lock, vault))
            self.assertEqual(lock.read_bytes(), before)

    def test_pending_retry_completes_checkpoint_without_maker_or_reviewer(self):
        import curate_once
        import maintenance
        from caretaker.pubstate import ensure_pub_tables
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, (publication, receipt) = _hub_repo(root)
            lock = _write_lock(root, lock_id='stale-intake', pid=_dead_pid(),
                               machine=_machine_name(), age_seconds=4 * 3600)
            db = sqlite3.connect(':memory:')
            ensure_pub_tables(db)
            pending = {'status': 'PUBLISHED', 'publication': publication,
                       'checkpoint': {'status': 'PENDING',
                                      'reason': 'checkpoint_lock_busy:intake',
                                      'receipt_dir': str(receipt)}}
            db.execute('INSERT INTO publications(batch,status,failures,result) VALUES(?,?,?,?)',
                       ('b1', 'PUBLISHED', 0, json.dumps(pending)))
            db.commit()
            canon = MagicMock()
            produce = MagicMock()
            maker = MagicMock()
            fleet = MagicMock()
            build = MagicMock()
            tick = MagicMock()
            with patch('curate_once.curator_lock', _no_lock), \
                    patch('curate_once.command', canon), \
                    patch('curate_once.produce_reviewed_candidate', produce), \
                    patch('curate_once.OpenClawArgvTransport', maker), \
                    patch('curate_once.AgentFleetArgvTransport', fleet), \
                    patch('curate_once.build_candidate', build), \
                    patch('maintenance.publication_tick', tick):
                remaining = maintenance.complete_pending_checkpoints(
                    db, {'hub': {'vault': str(vault), 'script': 'k.py'}})
            self.assertEqual(remaining, [])
            canon.assert_not_called()
            produce.assert_not_called()
            maker.assert_not_called()
            fleet.assert_not_called()
            build.assert_not_called()
            tick.assert_not_called()
            stored = json.loads(db.execute(
                "SELECT result FROM publications WHERE batch='b1'").fetchone()[0])
            self.assertEqual(stored['checkpoint']['status'], 'CHECKPOINTED')
            self.assertTrue(stored['checkpoint']['completed_after_pending'])
            self.assertFalse(lock.exists())

    def test_acquire_optional_vault_keeps_api_compatibility(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.git').mkdir()
            lock = _lock_path(root)
            lock_id = _acquire_intake_lock(lock)
            self.assertTrue(lock_id)
            self.assertTrue(lock.exists())
            payload = json.loads(lock.read_text())
            self.assertEqual(payload['lock_id'], lock_id)
            self.assertEqual(payload['agent'], 'openclaw-caretaker')


if __name__ == '__main__':
    unittest.main()
