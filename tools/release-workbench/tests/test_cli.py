"""CLI paths use actual libraries and task-owned synthetic committed blobs."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import os

from release_workbench.__main__ import main
from release_workbench.integration import load_json, offline_demo


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workbench-cli-test-", dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        fixture = self.base / "fixture.md"
        fixture.write_text("# Synthetic CLI fixture\nhttps://example.invalid\n", encoding="utf-8")
        self.root = self.base / "offline"
        offline_demo(self.root, fixture)

    def invoke(self, *argv):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main([str(v) for v in argv])
        return code, json.loads(stream.getvalue())

    def test_all_spec_commands_and_actual_statuses(self):
        receipt = self.root / "receipt-c.json"
        candidate = self.root / "candidate-c"
        code, exported = self.invoke("export", "--manifest", self.root / "manifest.json", "--destination", candidate,
                                     "--output-parent", self.root, "--receipt", receipt)
        self.assertEqual(code, 0)
        self.assertEqual(exported, load_json(receipt))
        self.assertEqual(self.invoke("verify", "--candidate", candidate, "--receipt", receipt)[0], 0)
        code, scan = self.invoke("scan", "--candidate", candidate, "--report", self.root / "scan-cli.json")
        self.assertEqual((code, scan["status"]), (0, "PASS"))
        code, config = self.invoke("config-check", "--config", self.root / "config.json", "--owned-root", self.root)
        self.assertEqual((code, config["status"]), (0, "PASS"))
        code, rights = self.invoke("rights-check", "--manifest", self.root / "manifest.json",
                                  "--components", self.root / "components.json", "--report", self.root / "rights-cli.json")
        self.assertEqual((code, rights["status"]), (2, "OPEN"))
        code, bundle = self.invoke("bundle-check", "--candidate", candidate, "--receipt", receipt,
                                  "--report", self.root / "bundle-cli.json")
        self.assertEqual((code, bundle["status"]), (2, "PENDING"))
        self.assertFalse(bundle["public_ready"])

    def test_report_collision_preserves_bytes_and_revision_recovers(self):
        existing = self.root / "scan.json"
        before = existing.read_bytes()
        code, result = self.invoke("scan", "--candidate", self.root / "candidate-a", "--report", existing)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(existing.read_bytes(), before)
        code, _ = self.invoke("scan", "--candidate", self.root / "candidate-a", "--report", self.root / "scan-v2.json")
        self.assertEqual(code, 0)

    def test_existing_receipt_prevents_export_side_effect(self):
        destination = self.root / "must-not-exist"
        code, _ = self.invoke("export", "--manifest", self.root / "manifest.json", "--destination", destination,
                              "--output-parent", self.root, "--receipt", self.root / "receipt-a.json")
        self.assertEqual(code, 1)
        self.assertFalse(destination.exists())

    def test_report_inside_candidate_is_rejected(self):
        candidate = self.root / "candidate-a"
        code, _ = self.invoke("scan", "--candidate", candidate, "--report", candidate / "report.json")
        self.assertEqual(code, 1)
        self.assertFalse((candidate / "report.json").exists())

    def test_malformed_json_is_nonzero_without_echoing_values(self):
        bad = self.base / "bad.json"
        bad.write_text('{"synthetic-sensitive-value": invalid}', encoding="utf-8")
        code, result = self.invoke("config-check", "--config", bad, "--owned-root", self.root)
        self.assertEqual(code, 1)
        self.assertNotIn("synthetic-sensitive-value", json.dumps(result))

    def test_cli_demo_is_actual_offline_journey(self):
        code, result = self.invoke("demo", "--source", self.base / "fixture.md", "--workspace", self.base / "second-demo",
                                  "--report", self.base / "demo-report.json")
        self.assertEqual(code, 0)
        self.assertEqual(result["kind"], "offline-synthetic-only")
        self.assertEqual(result["native_uat"], "NOT_RUN")
        self.assertTrue((self.base / "second-demo" / "bundle.json").is_file())

    def test_native_cli_retains_exact_blocked_stage(self):
        (self.base / "plan.json").write_text("{}", encoding="utf-8")
        workspace = self.base / "not-git"
        workspace.mkdir()
        code, report = self.invoke("native-uat", "--candidate", self.root / "candidate-a", "--receipt", self.root / "receipt-a.json",
                                  "--config", self.root / "config.json", "--workspace", workspace,
                                  "--plan", self.base / "plan.json", "--report", self.base / "native-report.json")
        self.assertEqual(code, 1)
        self.assertEqual(report["stage"], "workspace")
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(load_json(self.base / "native-report.json"), report)


class CLIHelpTests(unittest.TestCase):
    def test_module_entrypoint_help_is_executable(self):
        proc = subprocess.run([sys.executable, "-B", "-m", "release_workbench", "--help"],
                              cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30, shell=False,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"native-uat", proc.stdout)


if __name__ == "__main__":
    unittest.main()
