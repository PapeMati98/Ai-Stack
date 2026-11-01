"""Recovery must consume persisted records, not transient CLI response fields."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fleet


class PersistedRepairTests(unittest.TestCase):
    def dispatch(self, root, record, **options):
        gate_id = "qgate-20260907T020000Z-aaaaaaaaaa"
        gate = {
            "gate_id": gate_id, "verdict": "REPAIR", "attempt_index": 0,
            "contract": {"max_repair_rounds": 1}, "parent_acceptance": None,
            "workers": [{"run_id": record["run_id"], "model": "fixture",
                         "root_task_id": "task-repair"}],
            "result": {"items": [{"run_id": record["run_id"],
                "root_task_id": "task-repair", "verdict": "REPAIR",
                "criteria_failed": ["Complete the terminal handoff"], "findings": []}]},
        }
        with mock.patch.object(fleet, "load_quality_gate_record", return_value=(gate, root / "gate.json")), \
             mock.patch.object(fleet, "load_run_record", return_value=(record, root / "run.json")), \
             mock.patch.object(fleet.routing_v2, "resolve_execution_profile", return_value={}), \
             mock.patch.object(fleet, "execute_single", return_value={"run_id": "fixture-repair", "status": "DRY_RUN"}) as execute, \
             mock.patch.object(fleet, "persist_run_record") as persist_run, \
             mock.patch.object(fleet, "persist_quality_gate_record") as persist_gate:
            try:
                fleet.dispatch_quality_repairs(root, profiles={"qwen": {}}, gate_id=gate_id,
                    timeout=300, dry_run=True, output_root=None, **options)
            except fleet.FleetError:
                execute.assert_not_called()
                raise
            finally:
                persist_run.assert_not_called()
                persist_gate.assert_not_called()
            return execute.call_args.kwargs

    def record(self, worktree):
        return {"run_id": "run-20260907T020000Z-bbbbbbbbbb", "agent": "qwen",
            "mode": "implement", "kind": "implementation", "model": "fixture",
            "execution_profile_id": "fixture", "complexity": "low",
            "git_evidence": {"worktree": str(worktree),
                "base_commit_sha": "a" * 40, "base_provenance": "recorded"}}

    def test_legacy_persisted_record_uses_guarded_worktree_and_same_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree = root / "evidence" / "real-run" / "worktree"
            worktree.mkdir(parents=True)
            kwargs = self.dispatch(root, self.record(worktree))
            self.assertEqual(kwargs["workspace"], worktree)
            self.assertEqual(kwargs["existing_execution_workspace"], worktree)
            self.assertEqual(kwargs["preserved_base_commit_sha"], "a" * 40)
            self.assertEqual(kwargs["preserved_base_provenance"], "preserved")
            self.assertEqual(kwargs["root_task_id"], "task-repair")

    def test_missing_paths_never_fall_back_to_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            record = self.record(root)
            record["git_evidence"] = {}
            with self.assertRaises(fleet.FleetError):
                self.dispatch(root, record)

    def test_outside_evidence_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(fleet.FleetError, "escapes Fleet evidence"):
                self.dispatch(root, self.record(root))

    def test_explicit_execution_must_belong_to_same_worktree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree = root / "evidence" / "real-run" / "worktree"
            other = root / "evidence" / "other-run" / "worktree"
            worktree.mkdir(parents=True)
            other.mkdir(parents=True)
            record = self.record(worktree)
            record["execution_workspace"] = str(other)
            with self.assertRaisesRegex(fleet.FleetError, "same.*worktree|recorded.*worktree"):
                self.dispatch(root, record)

    def test_explicit_nested_project_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree = root / "evidence" / "real-run" / "worktree"
            nested = worktree / "packages" / "app"
            nested.mkdir(parents=True)
            record = self.record(worktree)
            record.update(workspace=str(root), execution_workspace=str(nested))
            kwargs = self.dispatch(root, record)
            self.assertEqual(kwargs["workspace"], root)
            self.assertEqual(kwargs["existing_execution_workspace"], nested)

    def test_legacy_read_only_missing_workspace_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            record = self.record(root)
            record["mode"] = "review"
            with self.assertRaisesRegex(fleet.FleetError, "workspace"):
                self.dispatch(root, record)

    def test_explicit_empty_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree = root / "evidence" / "real-run" / "worktree"
            worktree.mkdir(parents=True)
            for field in ("workspace", "execution_workspace"):
                for value in ("", " ", None):
                    with self.subTest(field=field, value=value):
                        record = self.record(worktree)
                        record[field] = value
                        with self.assertRaisesRegex(fleet.FleetError, "empty or invalid"):
                            self.dispatch(root, record)

    def test_explicit_tool_bound_is_forwarded_and_no_option_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            worktree = root / "evidence" / "real-run" / "worktree"
            worktree.mkdir(parents=True)
            record = self.record(worktree)
            self.assertIsNone(self.dispatch(root, record).get("max_model_tool_calls"))
            for bound in (1, 128, 256):
                with self.subTest(bound=bound):
                    kwargs = self.dispatch(root, record, max_model_tool_calls=bound)
                    self.assertEqual(kwargs["max_model_tool_calls"], bound)

    def test_tool_bound_refuses_invalid_values_before_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for bound in (0, -1, 257, True, "128", 1.5):
                with self.subTest(bound=bound), mock.patch.object(fleet, "execute_single") as execute:
                    with self.assertRaisesRegex(fleet.FleetError, "1.*256"):
                        self.dispatch(root, self.record(root), max_model_tool_calls=bound)
                    execute.assert_not_called()

    def test_public_parser_accepts_typed_repair_tool_bound(self):
        args = fleet.build_parser().parse_args([
            "quality-repair", "--gate-id", "qgate-20260907T020000Z-aaaaaaaaaa",
            "--max-model-tool-calls", "128"])
        self.assertEqual(args.max_model_tool_calls, 128)

    def test_qwen_explicit_tool_bound_leaves_room_for_terminal_turn(self):
        options = dict(model="qwen3.8-flash", reasoning_level="medium",
                       complexity="medium", timeout=600)
        default = fleet.qwen_control_policy(**options, max_model_tool_calls=None)
        explicit = fleet.qwen_control_policy(**options, max_model_tool_calls=128)
        self.assertEqual(default["turn_limit"], fleet.qwen_turn_limit("medium"))
        self.assertEqual(explicit["tool_limit"], 128)
        self.assertGreaterEqual(explicit["turn_limit"], 130)


if __name__ == "__main__":
    unittest.main()
