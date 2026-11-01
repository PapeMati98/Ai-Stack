from __future__ import annotations
def _redacted_fixture(kind, ordinal, binary=False):
    """Return a public fixture derived only from category and ordinal."""
    seed = (len(kind) * 131 + ordinal * 7919 + 17) & 0x7fffffff
    chars = []
    for _ in range(40):
        seed = (seed * 1103515245 + 12345) & 0x7fffffff
        n = seed % 36
        chars.append(chr(97 + n) if n < 26 else chr(48 + n - 26))
    body = ''.join(chars)
    if kind == 'vendor-token':
        body = chr(103) + chr(104) + chr(112) + chr(95) + body
    elif kind == 'aws-access-key-id':
        body = chr(65) + chr(75) + chr(73) + chr(65) + body[:16].upper()
    elif kind == 'private-key':
        marker = (chr(80) + chr(82) + chr(73) + chr(86) + chr(65)
                  + chr(84) + chr(69) + chr(32) + chr(75) + chr(69) + chr(89))
        dash = chr(45) * 5
        body = (dash + chr(66) + chr(69) + chr(71) + chr(73) + chr(78)
                + chr(32) + marker + dash + chr(10) + body + chr(10)
                + dash + chr(69) + chr(78) + chr(68) + chr(32) + marker + dash)
    return body.encode('ascii') if binary else body


import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "security_audit.py"


class SecurityAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.project = root / "sample-project"
        self.audit_root = root / "audits"
        self.project.mkdir()
        (self.project / "app.py").write_text(
            "DEBUG = True\n"
            "def run(value):\n"
            "    return eval(value)\n",
            encoding="utf-8",
        )
        (self.project / ".env").write_text(
            ('API_KEY=' + _redacted_fixture('assigned-secret', 1) + '\n'),
            encoding="utf-8",
        )
        self.env = os.environ.copy()
        self.env["SECURITY_AUDIT_TEST_MODE"] = "1"
        self.env["SECURITY_AUDIT_ROOT"] = str(self.audit_root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
            encoding="utf-8",
        )

    def test_gate_run_redaction_and_freshness(self) -> None:
        initial = self.run_cli("status", "--project", str(self.project))
        self.assertEqual(initial.returncode, 3, initial.stderr)
        self.assertEqual(json.loads(initial.stdout)["state"], "AUDIT_REQUIRED")

        run = self.run_cli(
            "run-local",
            "--project",
            str(self.project),
            "--agent",
            "test",
            "--classification",
            "confidential",
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        report = Path(result["report"])
        self.assertTrue(report.exists())
        combined = report.read_text(encoding="utf-8")
        combined += (report.parent / "baseline.json").read_text(encoding="utf-8")
        self.assertNotIn((_redacted_fixture('assigned-secret', 1)), combined)
        self.assertGreaterEqual(result["finding_counts"]["medium"], 1)

        fresh = self.run_cli("status", "--project", str(self.project))
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(json.loads(fresh.stdout)["state"], "AUDIT_FRESH")

    def test_stale_audit_requires_new_run(self) -> None:
        run = self.run_cli("run-local", "--project", str(self.project))
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        project_dir = Path(result["audit_dir"]).parents[1]
        latest_path = project_dir / "latest.json"
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=15)
        latest["completed_at"] = stale.isoformat().replace("+00:00", "Z")
        latest_path.write_text(json.dumps(latest), encoding="utf-8")

        status = self.run_cli("status", "--project", str(self.project))
        self.assertEqual(status.returncode, 3, status.stderr)
        self.assertEqual(json.loads(status.stdout)["state"], "AUDIT_REQUIRED")

    def test_external_review_attachment_is_traced(self) -> None:
        run = self.run_cli("run-local", "--project", str(self.project))
        result = json.loads(run.stdout)
        review = Path(self.tmp.name) / "review.md"
        review.write_text("# Review\n\nREVISE\n", encoding="utf-8")
        attach = self.run_cli(
            "attach-review",
            "--project",
            str(self.project),
            "--audit-id",
            result["audit_id"],
            "--agent",
            "claude",
            "--source",
            str(review),
        )
        self.assertEqual(attach.returncode, 0, attach.stderr)
        target = Path(json.loads(attach.stdout)["target"])
        self.assertTrue(target.exists())
        report_text = Path(result["report"]).read_text(encoding="utf-8")
        self.assertIn("Niezalezny review", report_text)

    def test_generated_virtualenv_variants_are_excluded(self) -> None:
        for relative in (
            ".clawops-venv/Lib/site-packages/vendor.py",
            ".venv-run/Lib/site-packages/vendor.py",
            "tools/venv-build/Lib/site-packages/vendor.py",
        ):
            target = self.project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("result = eval(user_input)\n", encoding="utf-8")

        run = self.run_cli("run-local", "--project", str(self.project))
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        baseline = json.loads(
            (Path(result["audit_dir"]) / "baseline.json").read_text(encoding="utf-8")
        )
        finding_paths = {finding["path"] for finding in baseline["findings"]}
        self.assertFalse(
            any("site-packages" in path or "venv" in path for path in finding_paths)
        )


if __name__ == "__main__":
    unittest.main()
