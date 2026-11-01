"""
Unit tests for Antigravity Windows adapter for agent-loop.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ADAPTER_DIR = Path(__file__).resolve().parent
if str(ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER_DIR))

from win_lock import (  # noqa: E402
    LockAcquisitionError,
    acquire_loop_lock,
    atomic_write_json,
)
from scheduler_trigger import (  # noqa: E402
    SchedulerContractError,
    build_scheduler_contract,
)
from discovery import discover_loops  # noqa: E402


class TestAntigravityAdapter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_atomic_write_json(self):
        target_file = self.root / "state.json"
        data = {"status": "running", "budget": 100}

        atomic_write_json(target_file, data)

        self.assertTrue(target_file.exists())
        with open(target_file, "r", encoding="utf-8") as f:
            read_data = json.load(f)
            self.assertEqual(read_data, data)

    def test_acquire_loop_lock_success(self):
        lock_file = self.root / "loop.lock"

        with acquire_loop_lock(lock_file):
            self.assertTrue(lock_file.exists())
            # We are holding the lock

        # Lock is released, file still exists
        self.assertTrue(lock_file.exists())

    def test_acquire_loop_lock_collision(self):
        lock_file = self.root / "loop.lock"

        # Helper to hold the lock in a background thread
        def hold_lock():
            with acquire_loop_lock(lock_file):
                time.sleep(1.0)

        t = threading.Thread(target=hold_lock)
        t.start()

        time.sleep(0.1)  # ensure thread acquires it

        with self.assertRaises(LockAcquisitionError):
            with acquire_loop_lock(lock_file, timeout_seconds=0.2):
                pass

        t.join()

    def test_build_scheduler_contract_success(self):
        contract = build_scheduler_contract(
            project_path="C:\\MyProject",
            loop_id="daily-health",
            cadence_minutes=60,
            dry_run=True,
        )

        self.assertEqual(contract["task_name"], "AgentLoop_daily-health")
        self.assertEqual(contract["interval_minutes"], 60)
        self.assertEqual(contract["writes_performed"], 0)
        self.assertEqual(contract["argv"][0], "schtasks.exe")
        self.assertIn("/create", contract["argv"])
        action = contract["argv"][contract["argv"].index("/tr") + 1]
        self.assertIn("--report-only", action)
        self.assertTrue(
            any("not reproducible" in warning for warning in contract["warnings"])
        )

    def test_scheduler_contract_pins_explicit_interpreter(self):
        """A scheduled task must not depend on whoever built the contract."""
        explicit = "C:\\Python311\\python.exe"
        contract = build_scheduler_contract(
            project_path="C:\\MyProject",
            loop_id="daily-health",
            cadence_minutes=60,
            dry_run=True,
            python_executable=explicit,
        )
        self.assertEqual(contract["interpreter_source"], "explicit")
        action = contract["argv"][contract["argv"].index("/tr") + 1]
        self.assertIn("python311", action.lower())
        self.assertNotIn(sys.executable.lower(), action.lower())

    def test_scheduler_contract_uses_env_override(self):
        os.environ["AGENT_LOOP_PYTHON"] = "C:\\Python311\\python.exe"
        try:
            contract = build_scheduler_contract(
                project_path="C:\\MyProject",
                loop_id="daily-health",
                cadence_minutes=60,
                dry_run=True,
            )
        finally:
            os.environ.pop("AGENT_LOOP_PYTHON", None)
        self.assertEqual(contract["interpreter_source"], "env:AGENT_LOOP_PYTHON")

    def test_scheduler_contract_warns_on_venv_interpreter(self):
        """Pinning a venv interpreter must be surfaced, not silently accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp) / "venv"
            (venv / "Scripts").mkdir(parents=True)
            (venv / "pyvenv.cfg").write_text("home = C:\\Python311\n", encoding="utf-8")
            fake_python = venv / "Scripts" / "python.exe"
            fake_python.write_text("", encoding="utf-8")
            contract = build_scheduler_contract(
                project_path="C:\\MyProject",
                loop_id="daily-health",
                cadence_minutes=60,
                dry_run=True,
                python_executable=str(fake_python),
            )
        self.assertTrue(
            any("virtual environment" in w for w in contract["warnings"]),
            contract["warnings"],
        )

    def test_build_scheduler_contract_rejects_mutation(self):
        with self.assertRaisesRegex(SchedulerContractError, "dry_run"):
            build_scheduler_contract(
                project_path="C:\\MyProject",
                loop_id="daily-health",
                cadence_minutes=60,
                dry_run=False,
            )

    def test_build_scheduler_contract_rejects_invalid_inputs(self):
        with self.assertRaises(SchedulerContractError):
            build_scheduler_contract(
                project_path="C:\\MyProject",
                loop_id="daily&health",
                cadence_minutes=60,
                dry_run=True,
            )

        with self.assertRaisesRegex(SchedulerContractError, "project path"):
            build_scheduler_contract(
                project_path="C:\\MyProject\r\n/ru SYSTEM",
                loop_id="daily-health",
                cadence_minutes=60,
                dry_run=True,
            )

        with self.assertRaisesRegex(SchedulerContractError, "controller path"):
            build_scheduler_contract(
                project_path="C:\\MyProject",
                loop_id="daily-health",
                cadence_minutes=60,
                dry_run=True,
                controller_path="C:\\agent-loop.py\r\n/ru SYSTEM",
            )

        with self.assertRaisesRegex(SchedulerContractError, "Cadence"):
            build_scheduler_contract(
                project_path="C:\\MyProject",
                loop_id="daily-health",
                cadence_minutes=True,
                dry_run=True,
            )

    def test_discover_loops(self):
        loops_dir = self.root / ".agents" / "loops"
        loops_dir.mkdir(parents=True)

        # Valid loop
        valid_loop_dir = loops_dir / "valid-loop"
        valid_loop_dir.mkdir()
        contract = {
            "schema_version": 1,
            "loop_id": "valid-loop",
            "goal": "Report project health.",
            "non_goals": ["No implementation."],
            "watched_scope": ["src/**"],
            "cadence": {"kind": "interval", "interval_seconds": 7200},
            "timezone": "UTC",
            "maturity": "L1",
            "delegation": {
                "via": "agent-fleet",
                "mode": "review",
                "agent": "auto",
                "kind": "review",
                "complexity": "low",
                "task_file": "task.md",
                "assessment_file": "assessment.json",
                "enable_web": False,
            },
            "limits": {
                "max_attempts_per_run": 1,
                "run_timeout_seconds": 30,
                "budget": {
                    "runs_per_day": 4,
                    "tokens_per_run": 1000,
                    "tokens_per_day": 4000,
                    "unknown_token_policy": "stop",
                },
            },
            "stop_conditions": {
                "no_progress_runs": 2,
                "repeated_failure_runs": 2,
                "budget_stop": True,
            },
            "human_gates": ["L2_promotion"],
            "kill_switch": {"file": "STOP", "env": "AGENT_LOOP_TEST_KILL"},
            "retention": {"runs_keep_days": 14, "reports_keep_days": 14},
            "write_policy": {
                "allowed_write_areas": [".agents/loops/valid-loop/**"],
                "path_claims": ["src/**"],
                "forbid_watched_writes": True,
            },
        }
        gate = {
            "schema_version": 1,
            "loop_id": "valid-loop",
            "verifier_default": "REJECT",
            "criteria": [
                {
                    "id": "parent-review",
                    "description": "Parent independently reviews the result.",
                    "check_kind": "parent-review",
                    "required": True,
                }
            ],
            "gates": [
                {
                    "id": "L2_promotion",
                    "kind": "human",
                    "status": "pending",
                    "approver": None,
                    "decided_at": None,
                    "note": None,
                }
            ],
        }
        (valid_loop_dir / "loop.json").write_text(
            json.dumps(contract, indent=2) + "\n",
            encoding="utf-8",
        )
        (valid_loop_dir / "gate.json").write_text(
            json.dumps(gate, indent=2) + "\n",
            encoding="utf-8",
        )
        (valid_loop_dir / "task.md").write_text("Review health.\n", encoding="utf-8")
        (valid_loop_dir / "assessment.json").write_text("{}\n", encoding="utf-8")

        # Invalid JSON loop
        invalid_loop_dir = loops_dir / "invalid-loop"
        invalid_loop_dir.mkdir()
        (invalid_loop_dir / "loop.json").write_text("{invalid json", encoding="utf-8")

        # Missing json loop
        empty_loop_dir = loops_dir / "empty-loop"
        empty_loop_dir.mkdir()

        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        discovered = discover_loops(self.root)
        after = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))

        self.assertEqual(before, after)
        self.assertIn("valid-loop", discovered)
        self.assertTrue(discovered["valid-loop"]["valid"])
        self.assertEqual(discovered["valid-loop"]["maturity"], "L1")
        self.assertEqual(
            discovered["valid-loop"]["cadence"],
            {"kind": "interval", "interval_seconds": 7200},
        )

        self.assertIn("invalid-loop", discovered)
        self.assertFalse(discovered["invalid-loop"]["valid"])
        self.assertIn("invalid UTF-8 JSON", discovered["invalid-loop"]["error"])

        self.assertNotIn("empty-loop", discovered)


if __name__ == "__main__":
    unittest.main()
