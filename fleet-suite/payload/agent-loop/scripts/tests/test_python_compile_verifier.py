import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


VERIFIER = Path(__file__).resolve().parents[1] / "trusted_verifiers" / "python_compile_v1.py"


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


class PythonCompileVerifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        run(["git", "init", "-q"], self.repo)
        run(["git", "config", "user.name", "Verifier Test"], self.repo)
        run(["git", "config", "user.email", "verifier@example.invalid"], self.repo)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def verify(self) -> tuple[subprocess.CompletedProcess[str], dict]:
        completed = run([sys.executable, str(VERIFIER), str(self.repo)], self.repo)
        return completed, json.loads(completed.stdout)

    def test_valid_tracked_python_passes(self) -> None:
        (self.repo / "valid.py").write_text("VALUE = 1\n", encoding="utf-8")
        run(["git", "add", "valid.py"], self.repo)
        completed, payload = self.verify()
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "PASS")

    def test_invalid_tracked_python_fails(self) -> None:
        (self.repo / "invalid.py").write_text("def broken(:\n", encoding="utf-8")
        run(["git", "add", "invalid.py"], self.repo)
        completed, payload = self.verify()
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["checks"][0]["failures"][0]["path"], "invalid.py")


if __name__ == "__main__":
    unittest.main()
