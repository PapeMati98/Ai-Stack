from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_graph_runner as base
import command_adapter


graph_runner = base.graph_runner
agent_graph = base.agent_graph


if os.name == "nt":
    import msvcrt
else:
    msvcrt = None  # type: ignore[assignment]


class NegativeRecoveryTests(unittest.TestCase):
    """Regression coverage for the append-only negative correction path."""

    def setUp(self) -> None:
        self._base = base.GraphRunnerTests()
        self._base.setUp()
        self.fixture = self._base.fixture
        self.run_id = self._base.run_id
        self.run_dir = self._base.run_dir
        self.addCleanup(self._base.tearDown)
        self.addCleanup(self._base.doCleanups)

    def _failure_fixture(self, **kwargs):
        return self._base._failure_fixture(**kwargs)

    def _state(self):
        return self._base._state()

    def _history_fingerprint(self, run_dir):
        return self._base._history_fingerprint(run_dir)

    def _negative_fixture(self, *, verdict: str = "blocked") -> tuple[dict, Path, dict]:
        attempt_id, evidence, dispatch_path = self._failure_fixture(
            verdict=verdict, record_result=False
        )
        folder = self.run_dir / "runner" / attempt_id
        journal_path = self.run_dir / "runner" / "journal.json"
        journal = graph_runner.read(journal_path)
        row = journal["attempts"][attempt_id]
        row.update(
            {
                "start_operation_id": "op-start-fail",
                "attempt_number": 1,
                "plan_hash": agent_graph.sha256_value(
                    agent_graph.load_current_plan(self.run_dir, self._state())
                ),
            }
        )
        graph_runner.write(journal_path, journal)

        evidence = copy.deepcopy(evidence)
        evidence["telemetry"]["quota_before"] = {"remaining": 19, "window": "5h"}
        evidence["telemetry"]["quota_after"] = {"remaining": 18, "window": "5h"}
        adapter_path = folder / "adapter-result.json"
        graph_runner.write(adapter_path, {"attempt_id": attempt_id, "evidence": evidence})
        graph_runner.write(
            folder / "finished.json",
            {
                "attempt_id": attempt_id,
                "status": "EXITED",
                "returncode": 0,
                "evidence_sha256": hashlib.sha256(adapter_path.read_bytes()).hexdigest(),
            },
        )
        job = graph_runner.read(folder / "job.json")
        start_result = agent_graph.load_state(self.run_dir)["operations"]["op-start-fail"]["result"]
        job.update(
            {
                "start_operation_id": "op-start-fail",
                "attempt": 1,
                "timeout": start_result["dispatch_bounds"]["timeout"],
                "dispatch": {
                    **job["dispatch"],
                    "workspace_root": str(self.fixture.project.resolve()),
                },
            }
        )
        graph_runner.write(folder / "job.json", job)
        plan = agent_graph.load_current_plan(self.run_dir, self._state())
        node = next(item for item in plan["nodes"] if item["node_id"] == "node-frontend")
        context = {
            "run_id": self.run_id,
            "node": node,
            "graph": plan,
            "dispatch_bounds": start_result["dispatch_bounds"],
        }
        graph_runner.write(folder / "context.json", context)
        return {"attempt_id": attempt_id, "evidence": evidence}, folder, graph_runner.read(
            dispatch_path
        )

    def _args(self, fixture: dict, evidence_path: Path | None = None, **changes):
        values = {
            "run_id": self.run_id,
            "node_id": "node-frontend",
            "attempt_id": fixture["attempt_id"],
            "state_root": str(self.fixture.state_root),
            "evidence": str(evidence_path or self.fixture.root / "corrected.json"),
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def _corrected(self, fixture: dict, **changes) -> dict:
        value = copy.deepcopy(fixture["evidence"])
        value["evidence_id"] = "evidence-frontend-corrected"
        value["summary"] = "Corrected metadata for the preserved negative result."
        value["telemetry"]["quota_before"] = json.dumps(
            {"window": "5h", "remaining": 19},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        value["telemetry"]["quota_after"] = json.dumps(
            {"window": "5h", "remaining": 18},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        value.update(changes)
        path = self.fixture.root / "corrected.json"
        base.write_json(path, value)
        return value

    def _run_correction(self, fixture: dict, value: dict | None = None) -> dict:
        value = value or self._corrected(fixture)
        path = self.fixture.root / "corrected.json"
        base.write_json(path, value)
        return graph_runner.correct_negative_evidence(self._args(fixture, path))

    def _run_correction_path(self, fixture: dict, path: Path) -> dict:
        return graph_runner.correct_negative_evidence(self._args(fixture, path))

    def test_positive_correction_preserves_negative_verdict_and_records_artifacts(self):
        fixture, folder, _ = self._negative_fixture()
        before = {
            name: (folder / name).read_bytes()
            for name in ("adapter-result.json", "finished.json", "job.json", "context.json")
        }
        result = self._run_correction(fixture)
        self.assertEqual(result["status"], "NEGATIVE_EVIDENCE_CORRECTED")
        self.assertEqual(result["verdict"], "blocked")
        self.assertFalse(result["idempotent"])
        self.assertNotEqual(result["verdict"], "accepted")
        self.assertTrue((folder / "negative-correction-intent.json").is_file())
        self.assertTrue((folder / "corrected-evidence.json").is_file())
        self.assertTrue((folder / "negative-correction-receipt.json").is_file())
        self.assertEqual(
            graph_runner.read(self.run_dir / "runner" / "journal.json")["attempts"][fixture["attempt_id"]]["phase"],
            "RECORDED",
        )
        self.assertEqual(self._state()["nodes"]["node-frontend"]["status"], "blocked")
        state = self._state()
        self.assertEqual(state["nodes"]["node-frontend"]["attempts"], 1)
        self.assertEqual(state["telemetry"]["total_attempts"], 1)
        self.assertEqual(
            len([key for key in state["operations"] if key.startswith("op-negative-correction-")]),
            1,
        )
        corrected = graph_runner.read(folder / "corrected-evidence.json")
        allowed = {
            "evidence_id",
            "summary",
            "telemetry.quota_before",
            "telemetry.quota_after",
        }
        for key in fixture["evidence"]:
            if key not in {"evidence_id", "summary", "telemetry"}:
                self.assertEqual(corrected[key], fixture["evidence"][key], key)
        for key in fixture["evidence"]["telemetry"]:
            if f"telemetry.{key}" not in allowed:
                self.assertEqual(corrected["telemetry"][key], fixture["evidence"]["telemetry"][key], key)
        for name, payload in before.items():
            self.assertEqual((folder / name).read_bytes(), payload, name)

    def test_quota_whitespace_and_key_order_are_semantically_equivalent(self):
        fixture, _, _ = self._negative_fixture()
        value = self._corrected(fixture)
        value["telemetry"]["quota_before"] = '{ "remaining": 19, "window": "5h" }'
        value["telemetry"]["quota_after"] = '{"window":"5h", "remaining":18}'
        result = self._run_correction(fixture, value)
        self.assertEqual(result["status"], "NEGATIVE_EVIDENCE_CORRECTED")

    def test_scalar_or_null_quota_and_nonquota_schema_invalid_are_refused(self):
        fixture, _, _ = self._negative_fixture()
        for replacement in (7, None):
            with self.subTest(replacement=replacement):
                value = self._corrected(fixture)
                value["telemetry"]["quota_before"] = replacement
                with self.assertRaises(agent_graph.GraphError):
                    self._run_correction(fixture, value)
        value = self._corrected(fixture)
        value["criteria_results"][0]["status"] = "PASS"
        with self.assertRaises(agent_graph.GraphError):
            self._run_correction(fixture, value)

    def test_disallowed_metadata_changes_and_duplicate_or_nonfinite_json_are_refused(self):
        fixture, _, _ = self._negative_fixture()
        changes = [
            {"verdict": "accepted"},
            {"metrics": {"new": 1}},
            {"external_refs": [{"kind": "fleet_run", "id": "changed"}]},
            {"telemetry": {"bad": float("nan")}},
        ]
        for change in changes:
            with self.subTest(change=change):
                value = self._corrected(fixture, **change)
                with self.assertRaises((agent_graph.GraphError, ValueError)):
                    self._run_correction(fixture, value)

    def test_duplicate_json_keys_are_rejected_by_the_real_correction_parser(self):
        fixture, _, _ = self._negative_fixture()
        path = self.fixture.root / "duplicate.json"
        path.write_text(
            '{"schema_version":1,"evidence_id":"evidence-frontend-corrected",'
            '"evidence_id":"evidence-frontend-other"}',
            encoding="utf-8",
        )
        with self.assertRaises(agent_graph.GraphError):
            self._run_correction_path(fixture, path)

    def test_running_finished_receipt_is_refused(self):
        fixture, folder, _ = self._negative_fixture()
        finished = graph_runner.read(folder / "finished.json")
        finished["status"] = "RUNNING"
        graph_runner.write(folder / "finished.json", finished)
        with self.assertRaises(agent_graph.GraphError):
            self._run_correction(fixture)

    def test_original_nonquota_schema_error_is_refused(self):
        fixture, folder, _ = self._negative_fixture()
        envelope = graph_runner.read(folder / "adapter-result.json")
        envelope["evidence"]["criteria_results"].append(
            copy.deepcopy(envelope["evidence"]["criteria_results"][0])
        )
        adapter = folder / "adapter-result.json"
        graph_runner.write(adapter, envelope)
        finished = graph_runner.read(folder / "finished.json")
        finished["evidence_sha256"] = hashlib.sha256(adapter.read_bytes()).hexdigest()
        graph_runner.write(folder / "finished.json", finished)
        with self.assertRaises(agent_graph.GraphError):
            self._run_correction(fixture)

    def test_later_original_start_is_refused(self):
        fixture, _, _ = self._negative_fixture()
        state_path = self.run_dir / "state.json"
        state = graph_runner.read(state_path)
        later = copy.deepcopy(state["operations"]["op-start-fail"]["result"])
        later["attempt"] = 2
        state["operations"]["op-later-start"] = {
            "action": "start",
            "payload_hash": "later",
            "recorded_at": "2026-09-13T19:01:00Z",
            "result": later,
        }
        graph_runner.write(state_path, state)
        with self.assertRaises(agent_graph.GraphError):
            self._run_correction(fixture)

    def test_ambiguous_original_start_is_refused(self):
        fixture, _, _ = self._negative_fixture()
        state_path = self.run_dir / "state.json"
        state = graph_runner.read(state_path)
        state["operations"]["op-ambiguous-start"] = {
            "action": "start",
            "payload_hash": "ambiguous",
            "recorded_at": "2026-09-13T19:01:00Z",
            "result": copy.deepcopy(state["operations"]["op-start-fail"]["result"]),
        }
        graph_runner.write(state_path, state)
        with self.assertRaises(agent_graph.GraphError):
            self._run_correction(fixture)

    def test_crash_after_public_result_before_journal_write_retries_idempotently(self):
        fixture, _, _ = self._negative_fixture()
        state_path = self.run_dir / "state.json"
        before = graph_runner.read(state_path)
        real_write = graph_runner.write

        def crash_before_journal(path, payload):
            if Path(path).name == "journal.json":
                raise RuntimeError("crash before journal write")
            return real_write(path, payload)

        with mock.patch.object(graph_runner, "write", side_effect=crash_before_journal):
            with self.assertRaisesRegex(RuntimeError, "crash before journal"):
                self._run_correction(fixture)
        after_crash = graph_runner.read(state_path)
        result_ops = [
            key for key in after_crash["operations"]
            if key.startswith("op-negative-correction-")
        ]
        self.assertEqual(len(result_ops), 1)
        self.assertEqual(after_crash["telemetry"]["total_attempts"], before["telemetry"]["total_attempts"])
        self.assertEqual(
            graph_runner.read(self.run_dir / "runner" / "journal.json")["attempts"][fixture["attempt_id"]]["phase"],
            "STARTED",
        )
        retry = self._run_correction(fixture)
        self.assertTrue(retry["idempotent"])
        final = graph_runner.read(state_path)
        self.assertEqual(len([key for key in final["operations"] if key.startswith("op-negative-correction-")]), 1)
        self.assertEqual(final["telemetry"]["total_attempts"], before["telemetry"]["total_attempts"])

    def test_wrong_foreign_later_ambiguous_and_running_attempts_refuse_without_mutation(self):
        fixture, _, _ = self._negative_fixture()
        baseline = self._history_fingerprint(self.run_dir)
        for changes in (
            {"node_id": "node-backend"},
            {"run_id": "run-graph-19990101T000000Z-ffffffffff"},
            {"attempt_id": "d" * 32},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(agent_graph.GraphError):
                    value = self._corrected(fixture)
                    path = self.fixture.root / "corrected.json"
                    graph_runner.correct_negative_evidence(
                        self._args(fixture, path, **changes)
                    )
                self.assertEqual(self._history_fingerprint(self.run_dir), baseline)
        journal = graph_runner.read(self.run_dir / "runner" / "journal.json")
        journal["attempts"][fixture["attempt_id"]]["phase"] = "RUNNING"
        graph_runner.write(self.run_dir / "runner" / "journal.json", journal)
        with self.assertRaises(agent_graph.GraphError):
            self._run_correction(fixture, self._corrected(fixture))

    def test_idempotent_and_conflicting_replay_are_distinct(self):
        fixture, _, _ = self._negative_fixture()
        first = self._run_correction(fixture)
        again = self._run_correction(fixture)
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["status"], first["status"])
        conflicting = self._corrected(fixture, summary="different correction")
        with self.assertRaises(agent_graph.GraphError):
            self._run_correction(fixture, conflicting)

    def test_settled_tamper_of_original_pins_and_corrected_file_is_refused(self):
        fixture, folder, _ = self._negative_fixture()
        self._run_correction(fixture)
        for name in ("adapter-result.json", "finished.json", "job.json", "context.json"):
            original = (folder / name).read_bytes()
            (folder / name).write_bytes(original + b"\n")
            with self.assertRaises(agent_graph.GraphError):
                self._run_correction(fixture)
            (folder / name).write_bytes(original)
        corrected = folder / "corrected-evidence.json"
        original = corrected.read_bytes()
        corrected.write_bytes(original + b"\n")
        with self.assertRaises(agent_graph.GraphError):
            self._run_correction(fixture)
        corrected.write_bytes(original)
        state_path = self.run_dir / "state.json"
        state_original = state_path.read_bytes()
        state = graph_runner.read(state_path)
        state["operations"]["op-start-fail"]["result"]["dispatch_bounds"]["timeout"] += 1
        graph_runner.write(state_path, state)
        with self.assertRaises(agent_graph.GraphError):
            self._run_correction(fixture)
        state_path.write_bytes(state_original)
        for name in (
            "negative-correction-intent.json",
            "corrected-evidence.json",
            "negative-correction-receipt.json",
        ):
            (folder / name).unlink(missing_ok=True)
        with self.assertRaises(agent_graph.GraphError):
            self._run_correction(fixture)

    def test_real_directory_junctions_are_refused_for_state_and_evidence_paths(self):
        fixture, _, _ = self._negative_fixture()
        self._corrected(fixture)
        junctions = []

        def junction(path: Path, target: Path) -> Path:
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(path), str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                self.skipTest(f"mklink /J unavailable: {completed.stderr or completed.stdout}")
            junctions.append(path)
            return path

        state_junction = junction(self.fixture.root / "state-junction", self.fixture.state_root)
        evidence_junction = junction(self.fixture.root / "evidence-junction", self.fixture.root)
        try:
            with self.assertRaises(agent_graph.GraphError):
                graph_runner.correct_negative_evidence(
                    self._args(
                        fixture,
                        evidence_junction / "corrected.json",
                        state_root=str(state_junction),
                    )
                )
            with self.assertRaises(agent_graph.GraphError):
                graph_runner.correct_negative_evidence(
                    self._args(fixture, evidence_junction / "corrected.json")
                )
        finally:
            for path in reversed(junctions):
                if path.exists():
                    path.rmdir()

    def test_active_clock_closes_one_original_interval_once_and_retry_is_unchanged(self):
        fixture, folder, _ = self._negative_fixture()
        plan_path = self.run_dir / "plans" / "revision-1.json"
        plan = graph_runner.read(plan_path)
        plan["global_budget"]["clock"] = "active"
        graph_runner.write(plan_path, plan)
        state_path = self.run_dir / "state.json"
        state = graph_runner.read(state_path)
        state["plan_hash"] = agent_graph.sha256_value(plan)
        state["telemetry"]["active_intervals"] = [
            {"node_id": "node-frontend", "attempt": 1, "start": "2026-09-13T19:00:00Z", "end": None}
        ]
        graph_runner.write(state_path, state)
        context = graph_runner.read(folder / "context.json")
        context["graph"] = plan
        context["dispatch_bounds"] = state["operations"]["op-start-fail"]["result"]["dispatch_bounds"]
        graph_runner.write(folder / "context.json", context)

        first = self._run_correction(fixture)
        self.assertFalse(first["idempotent"])
        after_first = graph_runner.read(state_path)
        intervals = after_first["telemetry"]["active_intervals"]
        self.assertEqual(len(intervals), 1)
        self.assertIsNotNone(intervals[0]["end"])
        closed_end = intervals[0]["end"]
        attempts = after_first["telemetry"]["total_attempts"]
        again = self._run_correction(fixture)
        self.assertTrue(again["idempotent"])
        after_retry = graph_runner.read(state_path)
        self.assertEqual(after_retry["telemetry"]["active_intervals"], intervals)
        self.assertEqual(after_retry["telemetry"]["active_intervals"][0]["end"], closed_end)
        self.assertEqual(after_retry["telemetry"]["total_attempts"], attempts)

    def test_lexical_state_root_symlink_is_refused(self):
        fixture, _, _ = self._negative_fixture()
        link = self.fixture.root / "state-link"
        try:
            link.symlink_to(self.fixture.state_root, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this Windows host")
        with self.assertRaises(agent_graph.GraphError):
            graph_runner.correct_negative_evidence(
                self._args(fixture, self.fixture.root / "corrected.json", state_root=str(link))
            )

    def test_reconcile_consumes_pending_correction_before_settled_skip(self):
        fixture, folder, _ = self._negative_fixture()
        value = self._corrected(fixture)
        real_write = graph_runner.write

        def crash_after_journal(path, payload):
            real_write(path, payload)
            if Path(path).name == "journal.json":
                raise RuntimeError("crash after journal persistence")

        with mock.patch.object(graph_runner, "write", side_effect=crash_after_journal):
            with self.assertRaisesRegex(RuntimeError, "crash after journal"):
                self._run_correction(fixture, value)
        self.assertTrue((folder / "negative-correction-intent.json").is_file())
        self.assertTrue((folder / "corrected-evidence.json").is_file())
        journal = graph_runner.read(self.run_dir / "runner" / "journal.json")
        errors = {}
        graph_runner.reconcile(
            self.run_dir,
            argparse.Namespace(state_root=str(self.fixture.state_root), run_id=self.run_id),
            journal,
            errors,
        )
        self.assertEqual(errors, {})
        state = graph_runner.read(self.run_dir / "state.json")
        self.assertTrue(state["operations"].get("op-negative-correction-" + fixture["attempt_id"]))
        self.assertEqual(state["telemetry"]["total_attempts"], 1)
        self.assertEqual(
            len([key for key in state["operations"] if key.startswith("op-negative-correction-")]),
            1,
        )
        self.assertEqual(state["telemetry"].get("active_intervals", []), [])
        self.assertEqual(
            graph_runner.read(self.run_dir / "runner" / "journal.json")["attempts"][fixture["attempt_id"]]["phase"],
            "RECORDED",
        )

    def test_public_parser_exposes_negative_correction(self):
        parsed = graph_runner.build_parser(
            [
                "correct-negative-evidence",
                "--state-root", "state",
                "--run-id", "run-graph-20260913T193242Z-4b2ae40d3f",
                "--node-id", "node-frontend",
                "--attempt-id", "b" * 32,
                "--evidence", "evidence.json",
            ]
        )
        self.assertEqual(parsed.action, "correct-negative-evidence")

    def test_canonical_quota_value_preserves_scalar_and_null(self):
        self.assertEqual(command_adapter.canonical_quota_value(3), 3)
        self.assertIsNone(command_adapter.canonical_quota_value(None))
        self.assertEqual(
            command_adapter.canonical_quota_value({"b": 2, "a": 1}),
            '{"a":1,"b":2}',
        )

    def test_windows_exclusive_initializes_after_lock_and_unlocks_in_finally(self):
        if os.name != "nt" or msvcrt is None:
            self.skipTest("Windows-exclusive msvcrt lock probe is Windows-only")
        events = []
        locked = {"value": False}

        class FakeHandle:
            def fileno(self):
                return 41

            def seek(self, offset, whence=0):
                events.append(("seek", offset, whence))

            def tell(self):
                return 0

            def write(self, value):
                self._assert_locked()
                events.append(("write", value))

            def flush(self):
                self._assert_locked()
                events.append(("flush",))

            def close(self):
                events.append(("close",))

            def _assert_locked(self):
                if not locked["value"]:
                    raise AssertionError("write/flush occurred before exclusive lock")

        def locking(fd, mode, size):
            self.assertEqual(fd, 41)
            self.assertEqual(size, 1)
            events.append(("lock", mode))
            locked["value"] = mode == msvcrt.LK_NBLCK

        handle = FakeHandle()
        with mock.patch.object(Path, "open", return_value=handle):
            with mock.patch.object(msvcrt, "locking", side_effect=locking):
                with self.assertRaisesRegex(RuntimeError, "body crash"):
                    with graph_runner.exclusive(self.fixture.root / "runner.lock"):
                        raise RuntimeError("body crash")
        self.assertEqual(events[0], ("seek", 0, 0))
        self.assertEqual(events[1], ("lock", msvcrt.LK_NBLCK))
        self.assertLess(
            next(i for i, item in enumerate(events) if item[0] == "lock"),
            next(i for i, item in enumerate(events) if item[0] == "write"),
        )
        self.assertLess(
            next(i for i, item in enumerate(events) if item[0] == "lock"),
            next(i for i, item in enumerate(events) if item[0] == "flush"),
        )
        self.assertEqual(events[-2], ("lock", msvcrt.LK_UNLCK))
        self.assertEqual(events[-1], ("close",))
        self.assertFalse(locked["value"])

    def test_native_journal_without_run_id_succeeds_for_correction(self):
        """The native runner writes only attempts, dispatch_sha256, waits. A
        journal that does not carry a top-level run_id must still accept the
        correction when state.run_id, context.run_id, the start result run_id,
        the lexical attempt path, the job and the original file/object hashes
        all bind correctly."""
        fixture, folder, _ = self._negative_fixture()
        journal_path = self.run_dir / "runner" / "journal.json"
        journal = graph_runner.read(journal_path)
        self.assertNotIn("run_id", journal)
        original_hashes = {
            name: (folder / name).read_bytes()
            for name in ("adapter-result.json", "finished.json", "job.json", "context.json")
        }
        result = self._run_correction(fixture)
        self.assertEqual(result["status"], "NEGATIVE_EVIDENCE_CORRECTED")
        for name, payload in original_hashes.items():
            self.assertEqual((folder / name).read_bytes(), payload, name)
        # The journal must remain free of a top-level run_id after settlement;
        # identity stays bound by state, context, start, and file/object hashes.
        post_journal = graph_runner.read(journal_path)
        self.assertNotIn("run_id", post_journal)

    def test_foreign_journal_run_id_is_rejected_when_present(self):
        """If any code path ever persists a top-level journal.run_id, a foreign
        value must be refused without mutating the original pins."""
        fixture, _, _ = self._negative_fixture()
        journal_path = self.run_dir / "runner" / "journal.json"
        journal = graph_runner.read(journal_path)
        journal["run_id"] = "run-graph-19990101T000000Z-ffffffffff"
        graph_runner.write(journal_path, journal)
        baseline = self._history_fingerprint(self.run_dir)
        with self.assertRaises(agent_graph.GraphError):
            self._run_correction(fixture)
        self.assertEqual(self._history_fingerprint(self.run_dir), baseline)
        # Native journal without a top-level run_id remains the only shape the
        # runner itself writes; foreign journal.run_id must be refused exactly
        # once and never persisted by the correction path.
        post_journal = graph_runner.read(journal_path)
        self.assertEqual(post_journal["run_id"], "run-graph-19990101T000000Z-ffffffffff")
        self.assertEqual(post_journal["attempts"][fixture["attempt_id"]]["phase"], "STARTED")

    def test_os_link_hardlinked_correction_file_is_refused(self):
        """Genuine os.link-based hardlinks must be refused by the immutable
        publication path even when the publisher is the same identity."""
        fixture, _, _ = self._negative_fixture()
        # Run a first correction so the canonical corrected evidence exists.
        self._run_correction(fixture)
        corrected = self.run_dir / "runner" / fixture["attempt_id"] / "corrected-evidence.json"
        self.assertTrue(corrected.is_file())
        # Build a fresh corrected evidence and a hardlink alias pointing at the
        # same inode; the immutable publisher must refuse the alias.
        value = self._corrected(fixture, summary="Hardlink probe for immutable publication")
        alias_target = self.fixture.root / "corrected-hardlink.json"
        base.write_json(alias_target, value)
        alias = self.fixture.root / "corrected-hardlink-link.json"
        try:
            os.link(alias_target, alias)
        except (OSError, AttributeError):
            self.skipTest("os.link is unavailable on this filesystem")
        # The hardened public command must treat the alias as a hardlink and
        # refuse without mutating any pinned original.
        baseline = self._history_fingerprint(self.run_dir)
        with self.assertRaises(agent_graph.GraphError):
            self._run_correction_path(fixture, alias)
        self.assertEqual(self._history_fingerprint(self.run_dir), baseline)
        alias.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
