import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fleet_bound import validate_worktree


class FleetBoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "synthetic fixture")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        self.worktree = self.root / "worktree"
        self.git("worktree", "add", "--detach", str(self.worktree), "HEAD")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True, timeout=15)

    def test_detached_root_and_base(self):
        self.assertEqual(validate_worktree(self.worktree, self.base), self.worktree.resolve())
        with self.assertRaises(ValueError):
            validate_worktree(self.repo, self.base)
        with self.assertRaises(ValueError):
            validate_worktree(self.worktree, "a" * 40)

    def test_adapter_preserves_normal_fleet_cli_and_reuses_workspace(self):
        fake = self.root / "fleet.py"
        fake.write_text('''import json
from pathlib import Path
class FleetError(Exception): pass
def execute_single(task, **kwargs):
    print(json.dumps({key:str(value) for key,value in kwargs.items()}))
    return 0
def main(argv):
    return execute_single("synthetic", mode="implement", allow_write=True, workspace=Path(argv[-1]))
''', encoding="utf-8")
        env = dict(os.environ, AGENT_GRAPH_ATTEMPT_ID="synthetic")
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("fleet_bound.py")),
                                 "--fleet", str(fake), "--base-commit", self.base, "--", "run", str(self.repo)],
                                 cwd=self.worktree, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["existing_execution_workspace"], str(self.worktree))
        self.assertEqual(value["preserved_base_commit_sha"], self.base)
        self.assertEqual(value["preserved_base_provenance"], "preserved")


if __name__ == "__main__":
    unittest.main()
