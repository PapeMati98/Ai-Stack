"""Guarded recovery of a crashed checkpoint's staged partial index.

Synthetic real-Git fixtures only: a checkpoint crash is simulated by staging
four of the five candidate files and leaving a stale same-host lock from a
dead owner.  No real source IDs, vault data or customer paths are used.
"""
import datetime
import json
import os
import unittest
from unittest import mock

import knowledge as k
import test_knowledge_hygiene as h


class IndexRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = h.KnowledgeHygieneTests()
        self.f.setUp()
        self.f.initialize_continuity_vault()
        page = self.f.vault / 'wiki/projects/northwind-app.md'
        body = page.read_text(encoding='utf-8')
        page.write_text(
            body if 'project_slug:' in body
            else body.replace('type: project', 'type: project\nproject_slug: northwind-app', 1),
            encoding='utf-8',
        )
        self.f.enable_owner_checkpoint()
        self.root = self.f.root
        self.vault = self.f.vault

    def tearDown(self):
        self.f.tearDown()

    def git(self, *a):
        return k.run_git_without_hooks(self.root, list(a))

    def seed(self, name='owned-note.md'):
        note = self.root / name
        note.write_text(f'Synthetic source {name}, no customer data.\n', encoding='utf-8')
        args = self.f.checkpoint_handoff_args(note)
        args.title = f'Owner checkpoint state {name}'
        args.state_summary = f'Northwind App owner checkpoint test state {name}.'
        with mock.patch.object(k, 'checkpoint_intake', return_value={'status': 'BLOCKED_CHECKPOINT_LOCK', 'required': True}):
            r = k.record_handoff(self.vault, args)
        return r['proposal_id']

    def untracked_vault_paths(self):
        out = self.git('ls-files', '--others', '--exclude-standard', '-z', '--', 'vault').stdout
        return sorted(p.replace('\\', '/') for p in out.split('\0') if p)

    def crash45(self):
        """Seed a pair and stage the first four of its five candidates (crash)."""
        self.pid = self.seed()
        candidates = self.untracked_vault_paths()
        self.assertEqual(len(candidates), 5, candidates)
        self.git('add', '--', *candidates[:4])
        self.candidates = candidates
        self.staged = candidates[:4]
        return candidates

    def write_lock(self, age=1800, pid=876543, machine=None):
        lp = k.git_internal_path(self.root, k.CHECKPOINT_LOCK_NAME)
        created = k.now_utc() - datetime.timedelta(seconds=age)
        lp.write_text(json.dumps({
            'lock_id': 'ab' * 16,
            'pid': pid,
            'machine': machine or k.local_machine_name(),
            'created_at': k.iso_time(created),
        }))
        os.utime(lp, (created.timestamp(), created.timestamp()))
        return lp

    def vault_bytes(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}

    def index_state(self):
        return self.git('ls-files', '--stage').stdout

    def head(self):
        return self.git('rev-parse', 'HEAD').stdout.strip()

    def test_default_dirty_index_still_blocked(self):
        self.crash45()
        lp = self.write_lock()
        b, index, head, lock = self.vault_bytes(), self.index_state(), self.head(), lp.read_bytes()
        r = k.checkpoint_intake(self.vault, self.pid)
        self.assertEqual(r['status'], 'BLOCKED_DIRTY_INDEX', r)
        self.assertEqual(self.vault_bytes(), b)
        self.assertEqual(self.index_state(), index)
        self.assertEqual(self.head(), head)
        self.assertEqual(lp.read_bytes(), lock)

    def test_explicit_recovery_completes_exact_five(self):
        candidates = self.crash45()
        lp = self.write_lock()
        b, head = self.vault_bytes(), self.head()
        with mock.patch.object(k, 'process_is_running', return_value=False):
            r = k.checkpoint_intake(self.vault, self.pid, recover_stale_lock=True)
        self.assertEqual(r['status'], 'CHECKPOINTED', r)
        self.assertEqual(r['path_count'], 5)
        self.assertTrue(r['stale_lock_recovered'], r)
        self.assertEqual(self.vault_bytes(), b)
        self.assertNotEqual(self.head(), head)
        committed = sorted(
            p.replace('\\', '/') for p in
            self.git('show', '--format=', '--name-status', 'HEAD').stdout.splitlines() if p
        )
        self.assertEqual(committed, sorted(f'A\t{p}' for p in candidates))
        self.assertEqual(self.git('diff', '--cached', '--name-only').stdout.strip(), '')
        retained = [p for p in lp.parent.glob(lp.name + '.release-*') if json.loads(p.read_text()).get('lock_id') == 'ab' * 16]
        self.assertTrue(retained, 'stale lock must be preserved as a release artifact')
        retry = k.checkpoint_intake(self.vault, self.pid)
        self.assertEqual(retry['status'], 'CURRENT', retry)
        self.assertEqual(retry['receipt_path_count'], 5)

    def test_explicit_recovery_preserves_unrelated_worktree(self):
        self.crash45()
        foreign = self.root / 'foreign-untracked.md'
        foreign.write_text('Foreign untracked work.\n', encoding='utf-8')
        self.write_lock()
        with mock.patch.object(k, 'process_is_running', return_value=False):
            r = k.checkpoint_intake(self.vault, self.pid, recover_stale_lock=True)
        self.assertEqual(r['status'], 'CHECKPOINTED', r)
        self.assertEqual(foreign.read_text(encoding='utf-8'), 'Foreign untracked work.\n')

    def test_automatic_recover_pending_intake(self):
        self.crash45()
        self.write_lock()
        with mock.patch.object(k, 'process_is_running', return_value=False):
            r = k.recover_pending_intake(self.vault)
        self.assertEqual(r['status'], 'COMPLETED', r)
        self.assertEqual(r['results'][0]['status'], 'CHECKPOINTED', r)
        self.assertEqual(r['results'][0]['path_count'], 5)

    def test_clean_intake_and_current_receipt_after_recovery(self):
        self.crash45()
        self.write_lock()
        with mock.patch.object(k, 'process_is_running', return_value=False):
            r = k.checkpoint_intake(self.vault, self.pid, recover_stale_lock=True)
        self.assertEqual(r['status'], 'CHECKPOINTED', r)
        retry = k.checkpoint_intake(self.vault, self.pid)
        self.assertEqual(retry['status'], 'CURRENT', retry)
        second = self.seed('second-note.md')
        clean = k.checkpoint_intake(self.vault, second)
        self.assertEqual(clean['status'], 'CHECKPOINTED', clean)
        self.assertEqual(clean['path_count'], 5)
        self.assertFalse(clean['stale_lock_recovered'], clean)
        again = k.checkpoint_intake(self.vault, second)
        self.assertEqual(again['status'], 'CURRENT', again)

    def assert_refusal_preserves(self, lp=None):
        b, index, head = self.vault_bytes(), self.index_state(), self.head()
        lock = lp.read_bytes() if lp is not None else None
        return b, index, head, lock

    def check_refusal_preserves(self, state, lp=None):
        b, index, head, lock = state
        self.assertEqual(self.vault_bytes(), b)
        self.assertEqual(self.index_state(), index)
        self.assertEqual(self.head(), head)
        if lp is not None:
            self.assertEqual(lp.read_bytes(), lock)

    def test_live_lock_refused(self):
        self.crash45()
        created = k.now_utc() - datetime.timedelta(seconds=1800)
        lp = self.write_lock(age=1800)
        state = self.assert_refusal_preserves(lp)
        with mock.patch.object(k, 'process_is_running', return_value=True), \
                mock.patch.object(k, 'windows_process_started_at', return_value=created - datetime.timedelta(seconds=120)):
            r = k.checkpoint_intake(self.vault, self.pid, recover_stale_lock=True)
        self.assertEqual(r['status'], 'BLOCKED_CHECKPOINT_LOCK', r)
        self.check_refusal_preserves(state, lp)

    def test_young_lock_refused(self):
        self.crash45()
        lp = self.write_lock(age=60)
        state = self.assert_refusal_preserves(lp)
        with mock.patch.object(k, 'process_is_running', return_value=False):
            r = k.checkpoint_intake(self.vault, self.pid, recover_stale_lock=True)
        self.assertEqual(r['status'], 'BLOCKED_CHECKPOINT_LOCK', r)
        self.check_refusal_preserves(state, lp)

    def test_wrong_host_lock_refused(self):
        self.crash45()
        lp = self.write_lock(machine='OTHER-HOST')
        state = self.assert_refusal_preserves(lp)
        with mock.patch.object(k, 'process_is_running', return_value=False):
            r = k.checkpoint_intake(self.vault, self.pid, recover_stale_lock=True)
        self.assertEqual(r['status'], 'BLOCKED_CHECKPOINT_LOCK', r)
        self.check_refusal_preserves(state, lp)

    def test_missing_lock_refused(self):
        self.crash45()
        lp = k.git_internal_path(self.root, k.CHECKPOINT_LOCK_NAME)
        self.assertFalse(lp.exists())
        state = self.assert_refusal_preserves()
        r = k.checkpoint_intake(self.vault, self.pid, recover_stale_lock=True)
        self.assertEqual(r['status'], 'BLOCKED_DIRTY_INDEX', r)
        self.assertIn('lock', r.get('reason', ''), r)
        self.check_refusal_preserves(state)
        self.assertFalse(lp.exists(), 'recovery attempt must release its own lock')

    def test_foreign_stage_refused(self):
        self.crash45()
        foreign = self.root / 'foreign-staged.md'
        foreign.write_text('Foreign staged work.\n', encoding='utf-8')
        self.git('add', '--', 'foreign-staged.md')
        lp = self.write_lock()
        state = self.assert_refusal_preserves(lp)
        with mock.patch.object(k, 'process_is_running', return_value=False):
            r = k.checkpoint_intake(self.vault, self.pid, recover_stale_lock=True)
        self.assertEqual(r['status'], 'BLOCKED_DIRTY_INDEX', r)
        self.assertIn('foreign', r.get('reason', ''), r)
        self.check_refusal_preserves(state, lp)

    def test_changed_staged_bytes_refused(self):
        self.crash45()
        # Tamper the staged proposal markdown (stays parseable, bytes differ).
        target = self.root / self.staged[2]
        self.assertTrue(self.staged[2].endswith('.md'), self.staged)
        target.write_bytes(target.read_bytes() + b'tampered\n')
        lp = self.write_lock()
        state = self.assert_refusal_preserves(lp)
        with mock.patch.object(k, 'process_is_running', return_value=False):
            r = k.checkpoint_intake(self.vault, self.pid, recover_stale_lock=True)
        self.assertEqual(r['status'], 'BLOCKED_DIRTY_INDEX', r)
        self.assertIn('differs', r.get('reason', ''), r)
        self.check_refusal_preserves(state, lp)

    def test_manual_block_refused(self):
        self.crash45()
        lp = self.write_lock()
        k.persist_checkpoint_manual_block(
            self.root,
            status='BLOCKED_TEST_MANUAL',
            proposal_id=self.pid,
            commit_sha=None,
            head_before=None,
            changed_paths=[],
            reason='synthetic manual block',
        )
        state = self.assert_refusal_preserves(lp)
        with mock.patch.object(k, 'process_is_running', return_value=False):
            r = k.checkpoint_intake(self.vault, self.pid, recover_stale_lock=True)
        self.assertEqual(r['status'], 'BLOCKED_MANUAL_RECOVERY', r)
        self.check_refusal_preserves(state, lp)

    def test_modified_stage_refused(self):
        self.crash45()
        # A tracked file modified in the index is not a pure addition.
        tracked = self.root / 'unrelated-tracked.md'
        tracked.write_text('baseline\n', encoding='utf-8')
        self.git('add', '--', 'unrelated-tracked.md')
        self.git('commit', '--no-verify', '--no-gpg-sign', '-m', 'tracked baseline')
        tracked.write_text('modified\n', encoding='utf-8')
        self.git('add', '--', 'unrelated-tracked.md')
        lp = self.write_lock()
        state = self.assert_refusal_preserves(lp)
        with mock.patch.object(k, 'process_is_running', return_value=False):
            r = k.checkpoint_intake(self.vault, self.pid, recover_stale_lock=True)
        self.assertEqual(r['status'], 'BLOCKED_DIRTY_INDEX', r)
        self.assertIn('pure addition', r.get('reason', ''), r)
        self.check_refusal_preserves(state, lp)

    def test_late_foreign_staging_refused_and_preserved(self):
        self.crash45()
        lp = self.write_lock()
        real = k._validate_partial_index_recovery
        calls = []

        def interleaved(root, paths):
            if len(calls) == 1:
                # A foreign actor stages its own file after our pre-lock
                # validation but before the in-lock revalidation reads the index.
                foreign = self.root / 'foreign-late.md'
                foreign.write_text('Late foreign staging.\n', encoding='utf-8')
                self.git('add', '--', 'foreign-late.md')
            calls.append(1)
            return real(root, paths)

        b, head, lock = self.vault_bytes(), self.head(), lp.read_bytes()
        with mock.patch.object(k, 'process_is_running', return_value=False), \
                mock.patch.object(k, '_validate_partial_index_recovery', side_effect=interleaved):
            r = k.checkpoint_intake(self.vault, self.pid, recover_stale_lock=True)
        self.assertEqual(r['status'], 'BLOCKED_CONCURRENT_GIT_STATE', r)
        self.assertEqual(self.vault_bytes(), b)
        self.assertEqual(self.head(), head)
        self.assertFalse(lp.exists(), 'own lock must be released after refusal')
        retained = [p for p in lp.parent.glob(lp.name + '.release-*') if p.read_bytes() == lock]
        self.assertTrue(retained, 'proven stale lock must be preserved as a release artifact')
        staged_now = self.git('diff', '--cached', '--name-only').stdout
        self.assertIn('foreign-late.md', staged_now)
        for candidate in self.staged:
            self.assertIn(candidate, staged_now)

    def test_automatic_ambiguous_pairs_stay_blocked(self):
        self.crash45()
        second = self.seed('second-note.md')
        second_proposal = k.repository_relative_path(self.root, k.proposal_path_for_id(self.vault, second))
        self.git('add', '--', second_proposal)
        self.write_lock()
        state = self.assert_refusal_preserves()
        with mock.patch.object(k, 'process_is_running', return_value=False):
            r = k.recover_pending_intake(self.vault)
        self.assertEqual(r['status'], 'BLOCKED_GIT_STATE', r)
        self.check_refusal_preserves(state)

    def test_automatic_foreign_only_stage_stays_blocked(self):
        self.crash45()
        self.git('restore', '--staged', '--', *self.staged)
        foreign = self.root / 'foreign-only.md'
        foreign.write_text('Foreign only.\n', encoding='utf-8')
        self.git('add', '--', 'foreign-only.md')
        state = self.assert_refusal_preserves()
        r = k.recover_pending_intake(self.vault)
        self.assertEqual(r['status'], 'BLOCKED_GIT_STATE', r)
        self.check_refusal_preserves(state)


if __name__ == '__main__':
    unittest.main()
