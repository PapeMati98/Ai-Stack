import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import incidents


def metadata_event(event_id=None, **changes):
    value = {
        "source": "synthetic-test",
        "event_type": "lifecycle",
        "run_id": "run-synthetic-1",
        "stage": "quality-gate",
        "outcome": "failure",
        "classification": "harness_transport",
        "error_code": "MISSING_TERMINAL_HANDOFF",
        "provider": "zai",
        "model": "glm-5.3-flash",
        "profile": "opencode-glm-5-3-flash-high",
        "runtime": "opencode",
        "release": "candidate-r1",
        "occurred_at": "2026-09-12T18:00:00Z",
    }
    if event_id is not None:
        value["event_id"] = event_id
    value.update(changes)
    return value


class IncidentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_allowlist_rejects_prompt_and_exception_fields(self):
        for field in ("prompt", "exception", "command", "source_code"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    incidents.record_event(self.root, metadata_event(**{field: "synthetic"}))

    def test_record_is_idempotent_and_report_keeps_unknown_denominator(self):
        event = metadata_event(event_id="event-idempotent")
        first = incidents.record_event(self.root, event)
        second = incidents.record_event(self.root, event)
        self.assertTrue(first["stored"])
        self.assertTrue(second["duplicate"])
        self.assertFalse(first["index_updated"])
        self.assertFalse(second["index_updated"])
        result = incidents.report(self.root)
        self.assertEqual(result["events"]["total"], 1)
        self.assertEqual(len(result["incidents"]), 1)
        group = result["incidents"][0]
        self.assertIsNone(group["denominator"])
        self.assertIsNone(group["frequency"])
        self.assertEqual(group["occurrences"], 1)
        self.assertEqual(result["coverage"]["omitted_events"], 0)

    def test_record_writepath_does_not_scan_or_take_shared_lock(self):
        with mock.patch.object(incidents, "_StateLock", side_effect=AssertionError("lock")), mock.patch.object(
            incidents, "_load_events", side_effect=AssertionError("scan")
        ), mock.patch.object(incidents, "_rebuild_index", side_effect=AssertionError("index")):
            result = incidents.record_event(self.root, metadata_event(event_id="fast-event"))
        self.assertTrue(result["stored"])
        self.assertFalse(result["index_updated"])

    def test_record_with_large_history_does_not_read_existing_events(self):
        events_dir = self.root / "incidents" / "events"
        events_dir.mkdir(parents=True)
        for index in range(1000):
            (events_dir / f"history-{index}.json").write_text("{}\n", encoding="utf-8")
        with mock.patch.object(incidents, "_load_events", side_effect=AssertionError("scan")):
            result = incidents.record_event(self.root, metadata_event(event_id="new-event"))
        self.assertTrue(result["stored"])
        self.assertTrue((events_dir / "new-event.json").exists())

    def test_io_failure_is_visible_and_does_not_raise(self):
        with mock.patch.object(incidents, "_atomic_create_json", side_effect=OSError("hidden detail")):
            result = incidents.record_event(self.root, metadata_event(event_id="io-failure"))
        self.assertFalse(result["stored"])
        self.assertFalse(result["duplicate"])
        self.assertFalse(result["index_updated"])

    def test_report_rebuilds_index_with_events_written_after_previous_report(self):
        first = incidents.record_event(self.root, metadata_event(event_id="before-report"))
        self.assertTrue(first["stored"])
        first_report = incidents.report(self.root)
        self.assertEqual(first_report["events"]["total"], 1)
        second = incidents.record_event(self.root, metadata_event(event_id="after-report", stage="new-stage"))
        self.assertTrue(second["stored"])
        refreshed = incidents.report(self.root)
        self.assertEqual(refreshed["events"]["total"], 2)
        self.assertEqual(refreshed["coverage"]["total_events"], 2)

    def test_waiting_is_observation_not_failure(self):
        incidents.record_event(
            self.root,
            metadata_event(
                event_id="event-waiting",
                stage="scheduler",
                outcome="waiting",
                classification=None,
                error_code=None,
            ),
        )
        result = incidents.report(self.root)
        self.assertEqual(result["events"]["observations"], 1)
        self.assertEqual(result["events"]["problems"], 0)
        self.assertEqual(result["incidents"], [])

    def test_same_worker_set_correlates_retries_not_owner_tasks(self):
        for index in range(3):
            incidents.record_event(
                self.root,
                metadata_event(
                    event_id=f"audit-event-{index}",
                    root_id=f"auditor-root-{index}",
                    gate_id=f"gate-{index}",
                    attempt_index=index,
                    worker_ids=["worker-shared-1", "worker-shared-2"],
                    occurred_at=f"2026-09-12T18:00:0{index}Z",
                ),
            )
        result = incidents.report(self.root)
        group = result["incidents"][0]
        self.assertEqual(group["occurrences"], 3)
        self.assertEqual(group["affected_tasks"], 1)
        self.assertEqual(group["distinct_affected_tasks"], 1)
        self.assertTrue(group["owner_task_unknown"])
        self.assertEqual(group["retry_count"], 2)

    def test_issue_fix_regression_and_postdeploy_lifecycle(self):
        incidents.record_event(
            self.root,
            metadata_event(
                event_id="issue-before-fix",
                occurred_at="2026-09-12T18:00:01Z",
                issue_id="issue-handoff",
                evidence_ref="gate-evidence-1",
                fix_release="release-fixed-1",
                regression_test_id="test-handoff-1",
                postdeploy_status="pass",
            ),
        )
        incidents.record_event(
            self.root,
            metadata_event(
                event_id="issue-after-fix",
                occurred_at="2026-09-12T18:00:02Z",
                issue_id="issue-handoff",
            ),
        )
        result = incidents.report(self.root)
        group = result["incidents"][0]
        self.assertEqual(group["issue"]["issue_id"], "issue-handoff")
        self.assertEqual(group["issue"]["fix_releases"], ["release-fixed-1"])
        self.assertEqual(group["issue"]["regression_tests"], ["test-handoff-1"])
        self.assertEqual(group["issue"]["postdeploy_statuses"], ["pass"])
        self.assertTrue(group["possible_regression"])

    def test_bounded_index_marks_coverage_without_deleting_authoritative_events(self):
        with mock.patch.object(incidents, "MAX_INDEX_ENTRIES", 2), mock.patch.object(
            incidents, "INDEX_MAX_ENTRIES", 2
        ):
            for index in range(5):
                incidents.record_event(
                    self.root,
                    metadata_event(
                        event_id=f"overflow-{index}",
                        stage=f"stage-{index}",
                        error_code=f"SYNTHETIC_{index}",
                    ),
                )
            result = incidents.report(self.root)
        self.assertTrue(result["coverage"]["overflow"])
        self.assertTrue(result["coverage"]["truncated"])
        self.assertEqual(result["coverage"]["total_signatures"], 5)
        self.assertEqual(result["coverage"]["indexed_signatures"], 2)
        self.assertEqual(result["events"]["total"], 5)
        event_files = list((self.root / "incidents" / "events").glob("*.json"))
        self.assertEqual(len(event_files), 5)
        self.assertLessEqual(
            (self.root / "incidents" / "index.json").stat().st_size,
            incidents._index_limits()[0] + 1,
        )

    def test_concurrent_subprocesses_store_one_metadata_event(self):
        event = metadata_event(event_id="concurrent-event")
        code = (
            "import json, sys; "
            f"sys.path.insert(0, {str(SCRIPT_DIR)!r}); "
            "import incidents; "
            "incidents.record_event(sys.argv[1], json.loads(sys.argv[2]))"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-B", "-c", code, str(self.root), json.dumps(event)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        results = [process.communicate(timeout=20) for process in processes]
        self.assertTrue(all(process.returncode == 0 for process in processes), results)
        result = incidents.report(self.root)
        self.assertEqual(result["events"]["total"], 1)
        self.assertEqual(result["incidents"][0]["occurrences"], 1)

    def test_offline_cli_import_and_report(self):
        input_path = self.root / "metadata.json"
        input_path.write_text(
            json.dumps({"events": [metadata_event(event_id="cli-event")]}), encoding="utf-8"
        )
        import_result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT_DIR / "incidents.py"), "import", str(self.root), str(input_path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(import_result.returncode, 0, import_result.stderr)
        self.assertEqual(json.loads(import_result.stdout)["imported"], 1)
        report_result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT_DIR / "incidents.py"), "report", str(self.root)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(report_result.returncode, 0, report_result.stderr)
        self.assertEqual(json.loads(report_result.stdout)["events"]["total"], 1)


if __name__ == "__main__":
    unittest.main()
