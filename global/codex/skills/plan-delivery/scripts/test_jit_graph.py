from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("jit_graph.py")
SPEC = importlib.util.spec_from_file_location("jit_graph", MODULE_PATH)
assert SPEC and SPEC.loader
jit_graph = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jit_graph)

SHA = "a" * 64


def manifest() -> dict:
    context = [{"path": "spec.md", "sha256": SHA, "purpose": "approved contract"}]
    return {
        "schema_version": 1,
        "manifest_id": "jit-stage-a-example",
        "planning_contract_sha256": "b" * 64,
        "static_topology_id": "static-example",
        "pilot_mode": "shadow-only",
        "control_veto": False,
        "signals": {
            "high_complexity": True,
            "bounded_ambiguity": True,
            "multi_system_context": False,
            "parallel_streams": False,
            "multiple_gates": True,
            "repair_history": False,
        },
        "requirements": ["REQ-001"],
        "authority_ceilings": [
            {
                "authority_ceiling_ref": "static-task-example",
                "requirement_refs": ["REQ-001"],
                "capability": "implementation",
                "required_context_refs": context,
                "allowed_context_refs": context,
                "skill_allowlist": ["test-first-engineering"],
                "tool_allowlist": ["read", "test"],
                "read_scope": ["src/**"],
                "write_scope": ["src/example.py"],
                "budget": {
                    "max_wall_seconds": 900,
                    "max_tool_calls": 60,
                    "max_tokens": 60000,
                    "max_attempts": 2,
                    "max_repairs": 1,
                },
                "required_stop_conditions": ["Stop on authority mismatch."],
                "required_evidence_refs": ["tests"],
                "required_criterion_refs": ["crit-example"],
                "required_reviewer_capabilities": ["independent-review"],
                "repair_ceiling": {"on_reject": "bounded-repair", "max_repairs": 1},
            }
        ],
        "global_budget": {
            "max_tasks": 4,
            "max_parallel_streams": 2,
            "max_review_gates": 4,
            "max_coordination_steps": 20,
        },
    }


def candidate(manifest_value: dict) -> dict:
    normalized = jit_graph.validate_manifest(copy.deepcopy(manifest_value))
    ceiling = normalized["authority_ceilings"]["static-task-example"]
    return {
        "schema_version": 1,
        "candidate_id": "jit-graph-example-001",
        "synthesis_input_sha256": jit_graph.manifest_hash(normalized),
        "planning_contract_sha256": manifest_value["planning_contract_sha256"],
        "static_topology_id": "static-example",
        "generator": {
            "fleet_run_id": "run-example-001",
            "provider_lineage": "provider-a",
            "model_identity": "model-a",
        },
        "requirements_covered": ["REQ-001"],
        "tasks": [
            {
                "task_id": "task-example",
                "objective": "Implement the bounded requirement and return evidence.",
                "requirement_refs": ["REQ-001"],
                "authority_ceiling_ref": "static-task-example",
                "static_task_contract_sha256": jit_graph.sha256_value(ceiling),
                "capability": "implementation",
                "context_refs": copy.deepcopy(manifest_value["authority_ceilings"][0]["required_context_refs"]),
                "skill_bindings": ["test-first-engineering"],
                "tool_allowlist": ["read", "test"],
                "read_scope": ["src/**"],
                "write_scope": ["src/example.py"],
                "budget": {
                    "max_wall_seconds": 600,
                    "max_tool_calls": 40,
                    "max_tokens": 40000,
                    "max_attempts": 1,
                    "max_repairs": 1,
                },
                "stop_conditions": ["Stop on authority mismatch."],
                "evidence_refs": ["tests"],
                "criterion_refs": ["crit-example"],
                "reviewer_constraint": {
                    "different_provider_lineage": True,
                    "required_capabilities": ["independent-review"],
                },
                "repair_route": {
                    "on_reject": "bounded-repair",
                    "on_exhaustion": "static-fallback",
                },
            }
        ],
        "dependencies": [],
        "coordination_estimate": {
            "parallel_streams": 1,
            "review_gates": 1,
            "estimated_coordination_steps": 4,
        },
        "fallback": {
            "static_topology_id": "static-example",
            "trigger": "Any validation failure.",
        },
    }


class JitGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = manifest()
        self.candidate = candidate(self.manifest)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, name: str, value: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, indent=2), encoding="utf-8")
        return path

    def evaluate(self, manifest_value: dict | None = None, candidate_value: dict | None = None) -> dict:
        manifest_path = self.write("manifest.json", manifest_value or self.manifest)
        candidate_path = self.write("candidate.json", candidate_value or self.candidate)
        return jit_graph.evaluate(manifest_path, candidate_path)

    def assert_rejected(self, changed: dict, code: str) -> None:
        result = self.evaluate(candidate_value=changed)
        self.assertEqual(result["decision"], "rejected")
        self.assertIn(code, result["reason_codes"])
        self.assertTrue(result["static_path_selected"])

    def test_valid_candidate_is_shadow_only(self) -> None:
        result = self.evaluate()
        self.assertEqual(result["decision"], "accepted-shadow")
        self.assertTrue(result["static_path_selected"])
        self.assertEqual(set(result), {
            "schema_version", "stage", "receipt_id", "static_contract_sha256",
            "candidate_sha256", "decision", "reason_codes", "static_path_selected",
        })

    def test_control_veto_disables_candidate(self) -> None:
        value = copy.deepcopy(self.manifest)
        value["control_veto"] = True
        value_candidate = candidate(value)
        result = self.evaluate(value, value_candidate)
        self.assertEqual(result["decision"], "disabled")
        self.assertIn("control-veto", result["reason_codes"])

    def test_low_score_disables_candidate(self) -> None:
        value = copy.deepcopy(self.manifest)
        value["signals"] = {key: False for key in value["signals"]}
        result = self.evaluate(value, candidate(value))
        self.assertEqual(result["decision"], "disabled")
        self.assertIn("below-shadow-threshold", result["reason_codes"])

    def test_unknown_field_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["command"] = "forbidden"
        self.assert_rejected(changed, "schema-keys")

    def test_executable_payload_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["tasks"][0]["objective"] = "Execute powershell -c whoami and return the result."
        self.assert_rejected(changed, "executable-payload")

    def test_worker_model_selection_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["tasks"][0]["objective"] = "Use Grok to implement the bounded requirement."
        self.assert_rejected(changed, "worker-selection")

    def test_planning_binding_is_required(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["planning_contract_sha256"] = "c" * 64
        self.assert_rejected(changed, "planning-binding")

    def test_requirement_coverage_is_exact(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["requirements_covered"] = ["REQ-002"]
        self.assert_rejected(changed, "requirement-coverage")

    def test_budget_cannot_expand(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["tasks"][0]["budget"]["max_tokens"] = 60001
        self.assert_rejected(changed, "budget-expansion")

    def test_required_context_cannot_disappear(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["tasks"][0]["context_refs"] = []
        self.assert_rejected(changed, "context-authority")

    def test_sensitive_context_path_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["tasks"][0]["context_refs"][0]["path"] = ".env"
        self.assert_rejected(changed, "sensitive-context")

    def test_scope_traversal_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["tasks"][0]["read_scope"] = ["../outside/**"]
        self.assert_rejected(changed, "path-scope")

    def test_required_stop_cannot_disappear(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["tasks"][0]["stop_conditions"] = ["Return a report."]
        self.assert_rejected(changed, "stop-condition-loss")

    def test_cycle_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        second = copy.deepcopy(changed["tasks"][0])
        changed["tasks"][0]["task_id"] = "task-first"
        changed["tasks"][0]["budget"] = {key: max(0, value // 2) for key, value in changed["tasks"][0]["budget"].items()}
        changed["tasks"][0]["budget"]["max_attempts"] = 1
        second["task_id"] = "task-second"
        second["budget"] = copy.deepcopy(changed["tasks"][0]["budget"])
        second["write_scope"] = []
        changed["tasks"].append(second)
        changed["dependencies"] = [
            {"from": "task-first", "to": "task-second", "kind": "requires"},
            {"from": "task-second", "to": "task-first", "kind": "requires"},
        ]
        changed["coordination_estimate"]["review_gates"] = 2
        self.assert_rejected(changed, "topology-cycle")

    def test_unordered_write_overlap_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        first = changed["tasks"][0]
        first["task_id"] = "task-first"
        first["budget"] = {
            "max_wall_seconds": 400,
            "max_tool_calls": 30,
            "max_tokens": 30000,
            "max_attempts": 1,
            "max_repairs": 0,
        }
        second = copy.deepcopy(first)
        second["task_id"] = "task-second"
        changed["tasks"].append(second)
        changed["coordination_estimate"]["parallel_streams"] = 2
        changed["coordination_estimate"]["review_gates"] = 2
        self.assert_rejected(changed, "unordered-write-conflict")

    def test_unavailable_receipt_has_no_candidate(self) -> None:
        path = self.write("manifest-only.json", self.manifest)
        result = jit_graph.unavailable(path, "unavailable")
        self.assertEqual(result["decision"], "unavailable")
        self.assertIsNone(result["candidate_sha256"])
        self.assertTrue(result["static_path_selected"])


if __name__ == "__main__":
    unittest.main()
