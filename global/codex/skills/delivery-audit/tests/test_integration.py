"""Synthetic command-level acceptance. No native model or real sink calls.

Retain every test directory under TEAM_TEST_ROOT; no cleanup or user data.
Successful no-launch enqueue is deliberately not native audit acceptance.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / "scripts" / "audit.py"


def packet(harness="antigravity"):
    return {
        "schema_version": 1, "task_id": "integration-delivery",
        "session_id": "integration-parent", "project_slug": "agent-stack",
        "harness": harness, "task_kind": "delivery", "task_family": "skill",
        "outcome": "COMPLETED", "workspace_id": "a" * 64,
        "requirements": [{"id": "r1", "status": "pass", "evidence_refs": ["fixture-e1"]}],
        "runs": [],
        "metrics": {"wall_seconds": 1, "repair_rounds": 0, "duplicate_work": False,
                    "uat_required": False, "uat_status": "not-applicable",
                    "evidence_complete": True},
        "delegation": {"parent_tier": "D", "eligible": True, "exception": "none",
                       "planned_tasks": 1},
        "observations": [], "evidence_refs": ["fixture-e1"],
    }


class CommandAcceptance(unittest.TestCase):
    def setUp(self):
        parent = Path(os.environ["TEAM_TEST_ROOT"])
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="audit-command-uat-", dir=parent))
        self.state = self.root / "state"

    def write_packet(self, payload):
        path = self.root / "packet.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def configure(self):
        self.state.mkdir(exist_ok=True)
        config = {"enabled": True, "state": "RUNNING", "test_only": True,
                  "office_enabled": False, "rsi_enabled": False}
        (self.state / "audit-config.json").write_text(json.dumps(config), encoding="utf-8")

    def command(self, *args):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.run([sys.executable, "-B", str(CLI), *map(str, args)],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=30, creationflags=flags, check=False)

    def test_no_launch_replay_and_export_never_claim_native_completion(self):
        for harness in ("antigravity", "zcode"):
            with self.subTest(harness=harness):
                self.configure()
                path = self.write_packet(packet(harness))
                first = self.command("enqueue", "--packet", path, "--state-dir", self.state,
                                     "--no-launch")
                self.assertEqual(first.returncode, 0, first.stderr.decode(errors="replace"))
                second = self.command("enqueue", "--packet", path, "--state-dir", self.state,
                                      "--no-launch")
                self.assertEqual(second.returncode, 0)
                self.assertIn("QUEUED", first.stdout.decode())
                self.assertIn("DUPLICATE", second.stdout.decode())
                export = self.root / (harness + "-feed.json")
                result = self.command("export-confirmed", "--state-dir", self.state,
                                      "--output", export)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertNotIn("CONFIRMED", export.read_text(encoding="utf-8"))
                queued = list((self.state / "state" / "records").glob("*.json"))
                self.assertTrue(queued)
                self.assertTrue(all(json.loads(p.read_text())["status"] == "QUEUED" for p in queued))

    def test_internal_and_noncompleted_episodes_do_not_enter_queue(self):
        self.configure()
        cases = [(kind, "COMPLETED") for kind in
                 ("audit", "audit-review", "subtask", "status", "metadata-only")]
        cases += [("delivery", outcome) for outcome in
                  ("BLOCKED", "INTERRUPTED", "NO_DURABLE_CHANGE")]
        for kind, outcome in cases:
            with self.subTest(kind=kind, outcome=outcome):
                payload = packet()
                payload.update(task_kind=kind, outcome=outcome)
                result = self.command("enqueue", "--packet", self.write_packet(payload),
                                      "--state-dir", self.state, "--no-launch")
                self.assertEqual(result.returncode, 0)
                self.assertIn("SKIPPED", result.stdout.decode())
        self.assertFalse((self.state / "state" / "records").exists())

    def test_unknown_private_fields_refused_without_echo_or_state(self):
        payload = packet()
        secret = "synthetic-restricted-value-never-route"
        payload["raw_prompt"] = secret
        result = self.command("enqueue", "--packet", self.write_packet(payload),
                              "--state-dir", self.state, "--no-launch")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(secret, (result.stdout + result.stderr).decode(errors="replace"))
        self.assertFalse(self.state.exists())

    def test_absent_or_unreadable_config_prevents_execution(self):
        path = self.write_packet(packet())
        self.state.mkdir()
        result = self.command("enqueue", "--packet", path, "--state-dir", self.state)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("STOP", str(json.loads(result.stdout).get("code")))
        self.assertFalse((self.state / "state" / "records").exists())
        self.state.mkdir(exist_ok=True)
        (self.state / "audit-config.json").write_text("{invalid", encoding="utf-8")
        result = self.command("run-one", "--state-dir", self.state)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.state / "state" / "records").exists())

    def test_stop_keeps_queue_and_blocks_run(self):
        self.configure()
        result = self.command("enqueue", "--packet", self.write_packet(packet()),
                              "--state-dir", self.state, "--no-launch")
        self.assertEqual(result.returncode, 0)
        before = {p.name: p.read_bytes() for p in (self.state / "state" / "records").glob("*.json")}
        self.assertTrue(before)
        (self.state / "STOP").write_text("synthetic stop", encoding="utf-8")
        result = self.command("run-one", "--state-dir", self.state)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("STOP", str(json.loads(result.stdout).get("code")))
        after = {p.name: p.read_bytes() for p in (self.state / "state" / "records").glob("*.json")}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
