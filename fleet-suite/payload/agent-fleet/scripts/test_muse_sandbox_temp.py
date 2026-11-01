"""Muse sandbox TEMP/TMP/TMPDIR regressions, with no provider/network execution."""
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, os.environ.get('FLEET_TEST_ROOT', str(Path(__file__).parent)))
import fleet


def _git(workspace, *args):
    return fleet.subprocess.run(
        ['git', *args],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo_with_commit(workspace):
    _git(workspace, 'init')
    (workspace / 'seed.txt').write_text('seed', encoding='utf-8')
    _git(workspace, 'add', 'seed.txt')
    _git(
        workspace,
        '-c', 'user.email=fleet-test@example.invalid',
        '-c', 'user.name=fleet-test',
        'commit', '-m', 'seed',
    )


class MuseSandboxTempTests(unittest.TestCase):
    def test_implement_child_temp_env_points_inside_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'workspace with spaces'
            workspace.mkdir()
            env = {'TEMP': 'C:\\outside', 'TMP': 'C:\\outside', 'TMPDIR': '/outside', 'PATH': 'test'}
            actual = fleet.muse_sandbox_temp_environment(env, workspace, mode='implement')
            expected = str((workspace / '.muse-tmp').resolve())
            self.assertEqual(actual['TEMP'], expected)
            self.assertEqual(actual['TMP'], expected)
            self.assertEqual(actual['TMPDIR'], expected)
            self.assertEqual(actual['PATH'], 'test')
            self.assertTrue((workspace / '.muse-tmp').is_dir())
            self.assertEqual(env['TEMP'], 'C:\\outside')

    def test_git_exclude_appended_once_across_two_runs(self):
        if shutil.which('git') is None:
            self.skipTest('git executable unavailable')
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'repo'
            workspace.mkdir()
            _git(workspace, 'init')
            fleet.prepare_muse_sandbox_temp(workspace)
            fleet.prepare_muse_sandbox_temp(workspace)
            git_dir = fleet.muse_workspace_git_common_dir(workspace)
            self.assertIsNotNone(git_dir)
            exclude = (git_dir / 'info' / 'exclude').read_text(encoding='utf-8')
            entries = [line.strip() for line in exclude.splitlines() if line.strip() == '.muse-tmp/']
            self.assertEqual(len(entries), 1)

    def test_workspace_without_git_works_without_exclude(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'plain'
            workspace.mkdir()
            temp_dir = fleet.prepare_muse_sandbox_temp(workspace)
            self.assertTrue(temp_dir.is_dir())
            self.assertFalse((workspace / '.git').exists())
            self.assertIsNone(fleet.muse_workspace_git_common_dir(workspace))

    def test_non_implement_modes_leave_environment_and_tree_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'review-ws'
            workspace.mkdir()
            env = {'TEMP': 'C:\\keep', 'TMPDIR': '/keep'}
            for mode in ('review', 'advisory'):
                actual = fleet.muse_sandbox_temp_environment(env, workspace, mode=mode)
                self.assertIs(actual, env)
                self.assertEqual(actual['TEMP'], 'C:\\keep')
            self.assertFalse((workspace / '.muse-tmp').exists())

    def test_common_dir_resolution_for_plain_repo(self):
        if shutil.which('git') is None:
            self.skipTest('git executable unavailable')
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'repo'
            workspace.mkdir()
            _git(workspace, 'init')
            git_dir = fleet.muse_workspace_git_common_dir(workspace)
            self.assertEqual(git_dir, (workspace / '.git').resolve())

    def test_common_dir_resolution_for_linked_worktree(self):
        if shutil.which('git') is None:
            self.skipTest('git executable unavailable')
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / 'main'
            main.mkdir()
            _init_repo_with_commit(main)
            worktree = Path(directory) / 'linked'
            _git(main, 'worktree', 'add', '--detach', str(worktree))
            git_dir = fleet.muse_workspace_git_common_dir(worktree)
            self.assertEqual(git_dir, (main / '.git').resolve())

    def test_plain_repo_muse_tmp_hidden_from_git_status(self):
        if shutil.which('git') is None:
            self.skipTest('git executable unavailable')
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'repo'
            workspace.mkdir()
            _init_repo_with_commit(workspace)
            fleet.prepare_muse_sandbox_temp(workspace)
            (workspace / '.muse-tmp' / 'artifact.tmp').write_text('x', encoding='utf-8')
            status = _git(workspace, 'status', '--porcelain', '--untracked-files=all')
            self.assertEqual(status.stdout.strip(), '')

    def test_linked_worktree_muse_tmp_hidden_from_git_status(self):
        """Git reads info/exclude only from the common dir, not the worktree git-dir."""
        if shutil.which('git') is None:
            self.skipTest('git executable unavailable')
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / 'main'
            main.mkdir()
            _init_repo_with_commit(main)
            worktree = Path(directory) / 'linked'
            _git(main, 'worktree', 'add', '--detach', str(worktree))
            fleet.prepare_muse_sandbox_temp(worktree)
            (worktree / '.muse-tmp' / 'artifact.tmp').write_text('x', encoding='utf-8')
            status = _git(worktree, 'status', '--porcelain', '--untracked-files=all')
            self.assertEqual(status.stdout.strip(), '')

    def test_linked_worktree_exclude_appended_once_across_two_runs(self):
        if shutil.which('git') is None:
            self.skipTest('git executable unavailable')
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / 'main'
            main.mkdir()
            _init_repo_with_commit(main)
            worktree = Path(directory) / 'linked'
            _git(main, 'worktree', 'add', '--detach', str(worktree))
            fleet.prepare_muse_sandbox_temp(worktree)
            fleet.prepare_muse_sandbox_temp(worktree)
            exclude = (main / '.git' / 'info' / 'exclude').read_text(encoding='utf-8')
            entries = [line.strip() for line in exclude.splitlines() if line.strip() == '.muse-tmp/']
            self.assertEqual(len(entries), 1)


if __name__ == '__main__':
    unittest.main()
