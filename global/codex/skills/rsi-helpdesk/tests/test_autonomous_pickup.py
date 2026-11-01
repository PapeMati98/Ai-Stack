"""Isolated end-to-end coverage for ordinary RSI queue pickup and launch."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

WORKSPACE = Path(__file__).resolve().parents[5]
FIXTURE_ROOT = WORKSPACE / "task-inputs" / "office-wake-rsi"
SCRIPTS = WORKSPACE / "global" / "codex" / "skills" / "rsi-helpdesk" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import helpdesk  # noqa: E402
import resolve_cycle  # noqa: E402


class AutonomousPickupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = FIXTURE_ROOT / f".rsi-pickup-{os.getpid()}-{time.time_ns()}"
        self.root.mkdir()
        self.home = self.root / "helpdesk"
        self.runs = self.root / "fleet-runs"
        self.runs.mkdir()
        self.processes: list[subprocess.Popen] = []
        self.env = mock.patch.dict(os.environ, {
            "RSI_HELPDESK_HOME": str(self.home),
            "RSI_HELPDESK_FLEET_RUNS": str(self.runs),
            "RSI_HELPDESK_STDERR_ROOTS": str(self.root / "out"),
            "RSI_HELPDESK_NO_OFFICE": "1",
            "RSI_HELPDESK_NO_TRIGGER": "1",
        })
        self.env.start()
        self.configure_synthetic_resolver()

    def tearDown(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        self.env.stop()
        shutil.rmtree(self.root)

    def configure_synthetic_resolver(self) -> None:
        # It consumes the normal resolver prompt and stays alive long enough for the
        # cycle to persist and inspect its real process identity. No model is invoked.
        argv = [sys.executable, "-c", "import sys,time; sys.stdin.read(); time.sleep(30)"]
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({
            "enabled": True,
            "workspace": str(self.root),
            "max_parallel": 1,
            "deploy_mode": "candidate",
            "claim_minutes": 240,
            "scan": {"lookback_hours": 24, "threshold": 3, "min_workspaces": 2},
            "resolvers": [{"name": "synthetic", "argv": argv}],
            # Missing usage checker means unknown/credit mode; the only resolver is
            # synthetic, so this cannot route to a paid model.
            "usage_check": str(self.root / "no-usage-check.py"),
        }), encoding="utf-8")

    def minimax_ticket(self) -> dict:
        ticket, action = helpdesk.file_ticket(
            component="fleet-adapter:minimax",
            model="MiniMax-M3",
            symptom="Native MiniMax returns JSON Schema instead of handoff instance; "
                    "handoff-unparsed with preserved artifact",
            evidence=["run-20261014T195030Z-b70bda82ed"],
            impact="Repeated candidate handoff failure delays native delivery",
            workaround="Preserve artifact and run checks plus independent review",
            reporter="synthetic-test",
        )
        self.assertEqual(action, "new")
        self.assertEqual(ticket["signature"], "e47130924af7")
        return ticket

    def start_synthetic_process(self) -> subprocess.Popen:
        flags = helpdesk.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        self.processes.append(process)
        return process

    def test_normal_cycle_claims_and_starts_open_minimax_ticket(self) -> None:
        ticket = self.minimax_ticket()
        started: list[subprocess.Popen] = []
        real_popen = subprocess.Popen

        def capture_resolver(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            started.append(process)
            self.processes.append(process)
            return process

        with mock.patch.object(resolve_cycle.subprocess, "Popen", side_effect=capture_resolver):
            report = resolve_cycle.cycle()

        self.assertEqual(report["result"], "launched")
        self.assertEqual(len(report["launched"]), 1)
        record = report["launched"][0]
        self.assertEqual(record["ticket"], ticket["signature"])
        self.assertTrue(record["pid"])
        self.assertEqual(resolve_cycle.identity_state(record), "ours")
        self.assertEqual(len(started), 1)
        claimed = helpdesk.load(ticket["signature"])
        self.assertEqual(claimed["status"], "claimed")
        self.assertEqual(claimed["claim"]["by"], record["run_id"])
        self.assertEqual(resolve_cycle.load_state()["active"][0]["run_id"], record["run_id"])
        prompt = (self.home / "runs" / record["run_id"] / "prompt.md").read_text(encoding="utf-8")
        self.assertIn(ticket["id"], prompt)

    def test_stop_file_prevents_claim_and_start(self) -> None:
        ticket = self.minimax_ticket()
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "STOP").write_text("", encoding="utf-8")

        report = resolve_cycle.cycle()

        self.assertEqual(report["result"], "stopped")
        self.assertEqual(report.get("launched", []), [])
        unchanged = helpdesk.load(ticket["signature"])
        self.assertEqual((unchanged["status"], unchanged["claim"]), ("open", None))
        self.assertEqual(resolve_cycle.load_state()["active"], [])

    def test_live_slot_and_live_ticket_claim_are_respected(self) -> None:
        ticket = self.minimax_ticket()
        occupant = self.start_synthetic_process()
        record = {
            "run_id": "synthetic-existing-run",
            "resolver": "synthetic",
            "ticket": None,
            "started_at": helpdesk.iso(helpdesk.now()),
            "pid": occupant.pid,
            "created": resolve_cycle.process_identity(occupant.pid),
        }
        resolve_cycle.save_state({"active": [record], "rotation": 0, "history": []})

        report = resolve_cycle.cycle()

        self.assertEqual(report["launched"], [])
        self.assertEqual(report["active"], [record["run_id"]])
        unchanged = helpdesk.load(ticket["signature"])
        self.assertEqual((unchanged["status"], unchanged["claim"]), ("open", None))

        occupant.terminate()
        occupant.wait(timeout=5)
        resolve_cycle.save_state({"active": [], "rotation": 0, "history": []})
        helpdesk.claim(ticket["signature"], "another-live-run", 60)
        report = resolve_cycle.cycle()
        self.assertEqual(report["launched"], [])
        claimed = helpdesk.load(ticket["signature"])
        self.assertEqual((claimed["status"], claimed["claim"]["by"]), ("claimed", "another-live-run"))

    def test_corrupt_resolver_state_fails_closed_without_changing_ticket(self) -> None:
        ticket = self.minimax_ticket()
        payload = '{"active":"untrusted"}'
        self.home.mkdir(parents=True, exist_ok=True)
        resolve_cycle.state_path().write_text(payload, encoding="utf-8")

        report = resolve_cycle.cycle()

        self.assertEqual(report["result"], "state-error")
        self.assertEqual(resolve_cycle.state_path().read_text(encoding="utf-8"), payload)
        unchanged = helpdesk.load(ticket["signature"])
        self.assertEqual((unchanged["status"], unchanged["claim"]), ("open", None))
        self.assertFalse((self.home / "runs").exists())


if __name__ == "__main__":
    unittest.main()
