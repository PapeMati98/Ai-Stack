"""Real-Git synthetic recovery after normal lock retention, without data loss."""
import datetime
import json
import os
import subprocess
import unittest
from unittest import mock
import knowledge as k
import test_intake_index_recovery as base


class RetainedIndexTests(unittest.TestCase):
    def setUp(self):
        self.b = base.IndexRecoveryTests()
        self.b.setUp()

    def tearDown(self):
        self.b.tearDown()

    def stage_prefix(self, count):
        self.b.pid = self.b.seed()
        paths = self.b.untracked_vault_paths()
        self.assertEqual(len(paths), 5)
        self.b.git('add', '--', *paths[:count])
        return paths

    def retain(self, intent=True):
        lp = self.b.write_lock()
        if intent:
            payload = json.loads(lp.read_text())
            payload['intake_proposal_id'] = self.b.pid
            payload['intake_receipt_sha256'] = k.receipt_content_digest(
                k.capture_candidate_bytes(self.b.root, self.b.untracked_vault_paths() + [
                    p for p in self.b.git('diff', '--cached', '--name-only').stdout.splitlines() if p
                ]))
            lp.write_text(json.dumps(payload))
        with mock.patch.object(k, 'process_is_running', return_value=False):
            self.assertTrue(k.recover_stale_checkpoint_lock(lp, {'machine': k.local_machine_name()}, requested=True))
        archives = list(lp.parent.glob(lp.name + '.release-*'))
        self.assertEqual(len(archives), 1)
        self.assertFalse(lp.exists())
        return archives[0]

    def assert_exact(self, paths, before, head, receipt):
        self.assertEqual(receipt['status'], 'CHECKPOINTED', receipt)
        self.assertEqual(receipt['path_count'], 5)
        self.assertEqual(self.b.vault_bytes(), before)
        self.assertNotEqual(self.b.head(), head)
        actual = sorted(self.b.git('show', '--format=', '--name-only', 'HEAD').stdout.splitlines())
        self.assertEqual(actual, paths)
        self.assertEqual(self.b.git('diff', '--cached', '--name-only').stdout.strip(), '')

    def test_stale_active_each_crash_prefix_one_through_five(self):
        # A separate fixture per prefix prevents one successful commit from
        # making the following test accidentally use a CURRENT receipt.
        for count in range(1, 6):
            with self.subTest(staged_count=count):
                if count > 1:
                    self.b.tearDown(); self.b.setUp()
                paths = self.stage_prefix(count)
                self.b.write_lock()
                before, head = self.b.vault_bytes(), self.b.head()
                with mock.patch.object(k, 'process_is_running', return_value=False):
                    result = k.checkpoint_intake(self.b.vault, self.b.pid, recover_stale_lock=True)
                self.assert_exact(paths, before, head, result)

    def test_legacy_retained_requires_explicit_exact_proof(self):
        paths = self.stage_prefix(4); archive = self.retain(intent=False)
        archive_bytes = archive.read_bytes(); state = self.b.assert_refusal_preserves()
        result = k.checkpoint_intake(self.b.vault, self.b.pid, recover_stale_lock=True)
        self.assertEqual(result['status'], 'BLOCKED_DIRTY_INDEX', result)
        self.b.check_refusal_preserves(state)
        with mock.patch.object(k, 'process_is_running', return_value=False):
            result = k.checkpoint_intake(self.b.vault, self.b.pid, recover_stale_lock=True,
                                         retained_checkpoint_lock=archive)
        self.assert_exact(paths, state[0], state[2], result)
        self.assertEqual(archive.read_bytes(), archive_bytes)
        self.assertTrue(result['staged_index_recovered'])
        retry = k.checkpoint_intake(self.b.vault, self.b.pid)
        self.assertEqual(retry['status'], 'CURRENT', retry)
        self.assertEqual(retry['receipt_path_count'], 5)

    def test_automatic_retained_intent_completes_exact_pair(self):
        paths = self.stage_prefix(4); archive = self.retain()
        state = self.b.assert_refusal_preserves(); old = archive.read_bytes()
        with mock.patch.object(k, 'process_is_running', return_value=False):
            result = k.recover_pending_intake(self.b.vault)
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assert_exact(paths, state[0], state[2], result['results'][0])
        self.assertEqual(archive.read_bytes(), old)

    def test_retained_wrong_pair_digest_refuses(self):
        self.stage_prefix(4); archive = self.retain()
        payload = json.loads(archive.read_text()); payload['intake_receipt_sha256'] = '0' * 64
        archive.write_text(json.dumps(payload)); state = self.b.assert_refusal_preserves()
        with mock.patch.object(k, 'process_is_running', return_value=False):
            result = k.checkpoint_intake(self.b.vault, self.b.pid, recover_stale_lock=True,
                                         retained_checkpoint_lock=archive)
        self.assertEqual(result['status'], 'BLOCKED_DIRTY_INDEX', result)
        self.b.check_refusal_preserves(state)

    def test_retained_untrusted_path_refuses(self):
        self.stage_prefix(4); archive = self.retain(intent=False)
        outside = self.b.root / archive.name; outside.write_bytes(archive.read_bytes())
        state = self.b.assert_refusal_preserves()
        with mock.patch.object(k, 'process_is_running', return_value=False):
            result = k.checkpoint_intake(self.b.vault, self.b.pid, recover_stale_lock=True,
                                         retained_checkpoint_lock=outside)
        self.assertEqual(result['status'], 'BLOCKED_DIRTY_INDEX', result)
        self.b.check_refusal_preserves(state)

    def test_retained_age_host_live_and_corrupt_refuse(self):
        self.stage_prefix(4); archive = self.retain(intent=False)
        original = json.loads(archive.read_text())
        cases = [dict(original, machine='FOREIGN'), dict(original, machine=''),
                 dict(original, created_at=k.iso_time(k.now_utc())), {'lock_id': 'x'},
                 dict(original)]
        for index, payload in enumerate(cases):
            with self.subTest(case=index):
                archive.write_text(json.dumps(payload)); state = self.b.assert_refusal_preserves()
                with mock.patch.object(k, 'process_is_running', return_value=index == 4), \
                     mock.patch.object(k, 'windows_process_started_at', return_value=None):
                    result = k.checkpoint_intake(self.b.vault, self.b.pid, recover_stale_lock=True,
                                                 retained_checkpoint_lock=archive)
                self.assertEqual(result['status'], 'BLOCKED_DIRTY_INDEX', result)
                self.b.check_refusal_preserves(state)

    def test_live_active_lock_still_blocks_retained_proof(self):
        self.stage_prefix(4); archive = self.retain(intent=False)
        active = self.b.write_lock(age=60, pid=os.getpid())
        state = self.b.assert_refusal_preserves(active)
        with mock.patch.object(k, 'process_is_running', side_effect=lambda p: p == os.getpid()):
            result = k.checkpoint_intake(self.b.vault, self.b.pid, recover_stale_lock=True,
                                         retained_checkpoint_lock=archive)
        self.assertEqual(result['status'], 'BLOCKED_CHECKPOINT_LOCK', result)
        self.b.check_refusal_preserves(state, active)

    def test_retained_proof_swap_after_lock_acquire_refuses(self):
        self.stage_prefix(4); archive = self.retain(intent=False)
        state = self.b.assert_refusal_preserves()
        real = k.acquire_checkpoint_lock
        def acquire(*args, **kwargs):
            got = real(*args, **kwargs)
            archive.write_bytes(archive.read_bytes() + b' ')
            return got
        with mock.patch.object(k, 'process_is_running', return_value=False), \
             mock.patch.object(k, 'acquire_checkpoint_lock', side_effect=acquire):
            result = k.checkpoint_intake(self.b.vault, self.b.pid, recover_stale_lock=True,
                                         retained_checkpoint_lock=archive)
        self.assertEqual(result['status'], 'BLOCKED_CONCURRENT_GIT_STATE', result)
        self.b.check_refusal_preserves(state)

    def test_regular_checkpoint_retains_bound_intent(self):
        self.b.pid = self.b.seed(); paths = self.b.untracked_vault_paths()
        result = k.checkpoint_intake(self.b.vault, self.b.pid)
        self.assertEqual(result['status'], 'CHECKPOINTED', result)
        lock = k.git_internal_path(self.b.root, k.CHECKPOINT_LOCK_NAME)
        archives = list(lock.parent.glob(k._checkpoint_pair_archive_prefix(lock, self.b.pid) + '*'))
        self.assertEqual(len(archives), 1)
        payload = json.loads(archives[0].read_text())
        self.assertEqual(payload['intake_proposal_id'], self.b.pid)
        self.assertEqual(payload['intake_receipt_sha256'], k.receipt_content_digest(
            k.capture_candidate_bytes(self.b.root, paths)))

    def test_blob_failure_before_staging_keeps_global_index_clean(self):
        self.b.pid = self.b.seed()
        state = self.b.assert_refusal_preserves()
        real = k.write_git_blob
        count = 0
        def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise k.GatewayError('Synthetic failure between pair blob writes')
            return real(*args, **kwargs)
        with mock.patch.object(k, 'write_git_blob', side_effect=fail_second):
            result = k.checkpoint_intake(self.b.vault, self.b.pid)
        self.assertTrue(result['status'].startswith('BLOCKED'), result)
        self.b.check_refusal_preserves(state)
        self.assertTrue(k.index_is_clean(self.b.root))
        self.assertIsNone(k.checkpoint_manual_block(self.b.root))
        retry = k.checkpoint_intake(self.b.vault, self.b.pid)
        self.assertEqual(retry['status'], 'CHECKPOINTED', retry)

    def test_full_staged_commit_failure_is_retryable_without_manual_block(self):
        paths = self.stage_prefix(5); archive = self.retain()
        state = self.b.assert_refusal_preserves()
        real = k.run_git_without_hooks
        def fail_commit(root, args, **kwargs):
            if args[0] == 'commit':
                raise k.GatewayError('Synthetic commit failure after all-five staging')
            return real(root, args, **kwargs)
        with mock.patch.object(k, 'process_is_running', return_value=False), \
             mock.patch.object(k, 'run_git_without_hooks', side_effect=fail_commit):
            result = k.checkpoint_intake(self.b.vault, self.b.pid,
                recover_stale_lock=True, retained_checkpoint_lock=archive)
        self.assertEqual(result['status'], 'BLOCKED_COMMIT_FAILURE', result)
        self.assertIsNone(k.checkpoint_manual_block(self.b.root))
        self.b.check_refusal_preserves(state)
        with mock.patch.object(k, 'process_is_running', return_value=False):
            retry = k.checkpoint_intake(self.b.vault, self.b.pid, recover_stale_lock=True)
        self.assert_exact(paths, state[0], state[2], retry)

    def test_unstage_empty_and_absent_paths_are_successful_noops(self):
        self.b.pid = self.b.seed(); paths = self.b.untracked_vault_paths()
        state = self.b.assert_refusal_preserves()
        self.assertTrue(k.unstage_checkpoint_paths(self.b.root, []))
        self.assertTrue(k.unstage_checkpoint_paths(self.b.root, paths))
        self.b.check_refusal_preserves(state)

    def test_automatic_staged_deleted_proposal_refuses_without_exception(self):
        self.b.pid = self.b.seed(); paths = self.b.untracked_vault_paths()
        self.b.git('add', '--', *paths)
        self.b.git('commit', '--no-verify', '--no-gpg-sign', '-m', 'synthetic baseline')
        proposal = k.repository_relative_path(self.b.root, k.proposal_path_for_id(self.b.vault, self.b.pid))
        self.b.git('rm', '--cached', '--', proposal)
        state = self.b.assert_refusal_preserves()
        result = k.recover_pending_intake(self.b.vault)
        self.assertEqual(result['status'], 'BLOCKED_GIT_STATE', result)
        self.b.check_refusal_preserves(state)

    def test_automatic_unresolved_staged_id_refuses_without_exception(self):
        self.stage_prefix(4)
        proposal = k.proposal_path_for_id(self.b.vault, self.b.pid)
        other_id = self.b.pid[:-8] + '00000000'
        proposal.write_text(proposal.read_text().replace(self.b.pid, other_id))
        self.b.git('add', '--', k.repository_relative_path(self.b.root, proposal))
        state = self.b.assert_refusal_preserves()
        result = k.recover_pending_intake(self.b.vault)
        self.assertEqual(result['status'], 'BLOCKED_GIT_STATE', result)
        self.b.check_refusal_preserves(state)

    def test_automatic_listing_error_is_bounded_refusal(self):
        self.stage_prefix(4); state = self.b.assert_refusal_preserves()
        real = k.run_git
        def fail_listing(root, args, **kwargs):
            if args == ['diff', '--cached', '--name-only', '-z']:
                raise subprocess.CalledProcessError(1, 'git synthetic listing')
            return real(root, args, **kwargs)
        with mock.patch.object(k, 'run_git', side_effect=fail_listing):
            result = k.recover_pending_intake(self.b.vault)
        self.assertEqual(result['status'], 'BLOCKED_GIT_STATE', result)
        self.b.check_refusal_preserves(state)


if __name__ == '__main__':
    unittest.main()
