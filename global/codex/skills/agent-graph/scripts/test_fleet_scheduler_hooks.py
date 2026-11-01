from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
from todo_fleet import resolve_fleet_root

# Was pinned to a "candidate" release channel that no longer exists, so this
# module failed to import at collection time. Resolve the same installed runtime
# production uses instead of hardcoding a version or channel - and skip rather
# than raise a collection error on a host that has no runtime installed at all.
try:
    FLEET_SCRIPTS = resolve_fleet_root() / "agent-fleet" / "scripts"
    sys.path.insert(0, str(FLEET_SCRIPTS))
    import fleet
except SystemExit as exc:  # no attested runtime on this host
    raise unittest.SkipTest(f"no installed Agent Fleet runtime: {exc}") from exc


class FleetSchedulerHookTests(unittest.TestCase):
    def test_optional_incident_api_is_exact_and_logger_failure_is_fail_open(self) -> None:
        calls = []
        module = types.SimpleNamespace(
            record_event=lambda state_root, event: calls.append((state_root, event))
            or {"status": "RECORDED"},
            report=lambda state_root: {"status": "READY", "state_root": str(state_root)},
        )
        root = Path("C:/bounded-state")
        event = {"stage": "provider-run", "code": "run-completed"}
        with mock.patch.object(fleet, "load_optional_incidents", return_value=module):
            self.assertEqual(
                fleet.record_incident_event(root, event)["status"], "RECORDED"
            )
            self.assertEqual(fleet.incident_report(root)["status"], "READY")
        self.assertEqual(calls, [(root, event)])

        stderr = io.StringIO()
        with (
            mock.patch.object(
                fleet,
                "load_optional_incidents",
                side_effect=RuntimeError("private exception prose"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            outcome = fleet.record_incident_event(root, event)
        self.assertEqual(outcome["status"], "WARNING")
        self.assertEqual(outcome["warning_code"], "INCIDENT_LOGGER_FAILED")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("private exception prose", stderr.getvalue())

    def test_parent_evaluation_records_sanitized_lifecycle_event(self) -> None:
        run_id = "run-20260912T120000Z-aaaaaaaaaa"
        record = {
            "schema_version": 5,
            "run_id": run_id,
            "root_task_id": "task-worker-0001",
            "attempt_id": "attempt-worker-0001",
            "agent": "qwen",
            "adapter": "qwen",
            "model": "qwen3.8-max",
            "execution_profile_fingerprint": "a" * 64,
            "runtime_attestation": {
                "payload_fingerprint": "b" * 64,
                "package_fingerprint": "c" * 64,
            },
            "quality_gate_required": False,
            "evaluation": None,
            "exit_code": 1,
            "run_status": "FAILED",
            "handoff_valid": False,
            "handoff_status": None,
            "transport_outcome": {"status": "failed"},
            "artifact_disposition": None,
            "completed_at": "2026-09-12T12:01:00Z",
            "duration_seconds": 1.25,
            "continuation_count": 0,
            "token_usage": {"status": "unknown"},
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fleet.persist_run_record(root, record)
            with mock.patch.object(
                fleet, "record_incident_event", return_value={"status": "RECORDED"}
            ) as recorder:
                result = fleet.evaluate_run(
                    root,
                    run_id=run_id,
                    quality_score=20,
                    criteria_pass=False,
                    note="free-form parent note must not enter incident data",
                )
        self.assertEqual(result["status"], "REJECTED")
        event = recorder.call_args.args[1]
        self.assertEqual(event["stage"], "acceptance")
        self.assertEqual(event["error_code"], "run-rejected")
        self.assertEqual(event["root_task_id"], "task-worker-0001")
        self.assertNotIn("note", event)
        self.assertNotIn("error", event)

    def test_qwen_docker_allocation_lock_releases_after_owned_container_appears(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "qwen_probe.py"
            helper.write_text(
                "import time\ntime.sleep(0.4)\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(fleet, "resolve_docker_executable", return_value="docker.exe"),
                mock.patch.object(
                    fleet,
                    "qwen_container_ids",
                    side_effect=[set(), {"container-a"}],
                ),
                mock.patch.object(
                    fleet, "qwen_container_owns_workspace", return_value=True
                ),
            ):
                result = fleet.run_provider_child(
                    [sys.executable, str(helper), "--sandbox"],
                    cwd=root,
                    env=dict(fleet.os.environ),
                    stdin_text=None,
                    timeout=5,
                    adapter_id="qwen",
                    max_model_tool_calls=None,
                    state_root=root / "state",
                )
        self.assertEqual(result["child"]["exit_code"], 0)
        self.assertEqual(result["launch_protection"]["status"], "confirmed")
        self.assertLess(
            result["launch_protection"]["confirmation_seconds"],
            result["execution_seconds"],
        )
        self.assertFalse(result["execution_serialized"])

    def test_qwen_launch_confirmation_timeout_stops_child_instead_of_holding_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / "qwen_slow.py"
            helper.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
            began = time.monotonic()
            with (
                mock.patch.object(fleet, "resolve_docker_executable", return_value="docker.exe"),
                mock.patch.object(fleet, "qwen_container_ids", return_value=set()),
                mock.patch.object(fleet, "QWEN_DOCKER_CONFIRM_TIMEOUT_SECONDS", 0.2),
            ):
                result = fleet.run_provider_child(
                        [sys.executable, str(helper), "--sandbox"],
                        cwd=root,
                        env=dict(fleet.os.environ),
                        stdin_text=None,
                        timeout=5,
                        adapter_id="qwen",
                        max_model_tool_calls=None,
                        state_root=root / "state",
                    )
                self.assertEqual(result['child']['exit_code'], 126)
                self.assertTrue(result['child']['supervision_failed'])
                self.assertIsNone(result['launch_protection'])
        self.assertLess(time.monotonic() - began, 3.0)


if __name__ == "__main__":
    unittest.main()
