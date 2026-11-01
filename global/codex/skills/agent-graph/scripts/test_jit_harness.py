from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("jit_harness.py")
SPEC = importlib.util.spec_from_file_location("jit_harness", MODULE_PATH)
assert SPEC and SPEC.loader
jit_harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jit_harness)

SHA = "a" * 64


def static_contract() -> dict:
    context = [{"path": "spec.md", "sha256": SHA, "purpose": "approved contract"}]
    return {
        "schema_version": 1,
        "contract_id": "static-node-example",
        "planning_contract_sha256": "b" * 64,
        "graph_sha256": "c" * 64,
        "node_id": "node-example",
        "static_template_id": "static-template-example",
        "pilot_mode": "shadow-only",
        "control_veto": False,
        "objective": "Implement the bounded requirement and return evidence.",
        "capability": "implementation",
        "required_context_refs": context,
        "allowed_context_refs": context,
        "skill_allowlist": ["test-first-engineering"],
        "tool_allowlist": ["read", "test"],
        "read_scope": ["src/**"],
        "write_scope": ["src/example.py"],
        "budget": {
            "max_wall_seconds": 600,
            "max_tool_calls": 40,
            "max_tokens": 40000,
            "max_attempts": 2,
        },
        "stop_conditions": ["Stop on authority mismatch."],
        "criterion_refs": ["crit-example"],
        "repair_policy": {"on_reject": "bounded-repair", "max_repairs": 1},
        "reviewer_capabilities": ["independent-review"],
        "maker_provider_lineage": "provider-maker",
        "reviewer_provider_lineage": "provider-reviewer",
    }


def candidate(static_value: dict) -> dict:
    normalized = jit_harness.validate_static(copy.deepcopy(static_value))
    return {
        "schema_version": 1,
        "harness_id": "harness-example-001",
        "node_id": static_value["node_id"],
        "planning_contract_sha256": static_value["planning_contract_sha256"],
        "graph_sha256": static_value["graph_sha256"],
        "static_node_contract_sha256": jit_harness.sha256_value(normalized),
        "generator": {
            "fleet_run_id": "run-example-001",
            "provider_lineage": "provider-generator",
            "model_identity": "model-generator",
        },
        "reviewer_constraint": {
            "different_provider_lineage": True,
            "required_capabilities": ["independent-review"],
        },
        "objective": static_value["objective"],
        "capability": static_value["capability"],
        "context_refs": copy.deepcopy(static_value["required_context_refs"]),
        "skill_bindings": ["test-first-engineering"],
        "tool_allowlist": ["read", "test"],
        "read_scope": ["src/**"],
        "write_scope": ["src/example.py"],
        "budget": {
            "max_wall_seconds": 300,
            "max_tool_calls": 20,
            "max_tokens": 20000,
            "max_attempts": 1,
        },
        "stop_conditions": ["Stop on authority mismatch."],
        "criterion_refs": ["crit-example"],
        "repair_policy": {"on_reject": "static-fallback", "max_repairs": 0},
        "fallback": {
            "static_template_id": static_value["static_template_id"],
            "trigger": "Any validation failure.",
        },
    }


class JitHarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.static = static_contract()
        self.candidate = candidate(self.static)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, name: str, value: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value, indent=2), encoding="utf-8")
        return path

    def evaluate(self, static_value: dict | None = None, candidate_value: dict | None = None) -> dict:
        static_path = self.write("static.json", static_value or self.static)
        candidate_path = self.write("candidate.json", candidate_value or self.candidate)
        return jit_harness.evaluate(static_path, candidate_path)

    def assert_rejected(self, changed: dict, code: str) -> None:
        result = self.evaluate(candidate_value=changed)
        self.assertEqual(result["decision"], "rejected")
        self.assertIn(code, result["reason_codes"])
        self.assertTrue(result["static_path_selected"])

    def test_valid_candidate_is_shadow_only(self) -> None:
        result = self.evaluate()
        self.assertEqual(result["decision"], "accepted-shadow")
        self.assertTrue(result["static_path_selected"])
        self.assertIn("active-path-not-implemented", result["reason_codes"])

    def test_control_veto_disables_candidate(self) -> None:
        value = copy.deepcopy(self.static)
        value["control_veto"] = True
        result = self.evaluate(value, candidate(value))
        self.assertEqual(result["decision"], "disabled")
        self.assertIn("control-veto", result["reason_codes"])

    def test_unknown_field_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["activation_score"] = 9
        self.assert_rejected(changed, "schema-keys")

    def test_executable_payload_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["fallback"]["trigger"] = "Run bash -c whoami after failure."
        self.assert_rejected(changed, "executable-payload")

    def test_worker_selection_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["fallback"]["trigger"] = "Route to Claude after failure."
        self.assert_rejected(changed, "worker-selection")

    def test_static_binding_is_required(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["static_node_contract_sha256"] = "d" * 64
        self.assert_rejected(changed, "static-binding")

    def test_objective_cannot_change(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["objective"] = "Implement a broader behavior and return evidence."
        self.assert_rejected(changed, "objective-expansion")

    def test_extra_tool_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["tool_allowlist"].append("shell")
        self.assert_rejected(changed, "tool_allowlist-expansion")

    def test_extra_write_scope_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["write_scope"].append("src/other.py")
        self.assert_rejected(changed, "write_scope-expansion")

    def test_budget_cannot_expand(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["budget"]["max_tokens"] = 40001
        self.assert_rejected(changed, "budget-expansion")

    def test_required_context_cannot_disappear(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["context_refs"] = []
        self.assert_rejected(changed, "context-loss")

    def test_unapproved_context_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["context_refs"].append({"path": "extra.md", "sha256": "e" * 64, "purpose": "extra context"})
        self.assert_rejected(changed, "context-expansion")

    def test_sensitive_context_path_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["context_refs"][0]["path"] = "secrets.json"
        self.assert_rejected(changed, "sensitive-context")

    def test_scope_traversal_is_rejected(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["read_scope"] = ["../outside/**"]
        self.assert_rejected(changed, "path-scope")

    def test_required_stop_cannot_disappear(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["stop_conditions"] = ["Return a report."]
        self.assert_rejected(changed, "stop-condition-loss")

    def test_required_criterion_cannot_disappear(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["criterion_refs"] = ["crit-other"]
        self.assert_rejected(changed, "criterion-loss")

    def test_repair_policy_cannot_expand(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["repair_policy"] = {"on_reject": "bounded-repair", "max_repairs": 2}
        self.assert_rejected(changed, "repair-expansion")

    def test_generator_lineage_must_be_independent(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["generator"]["provider_lineage"] = "provider-maker"
        self.assert_rejected(changed, "lineage-collision")

    def test_fallback_must_bind_static_template(self) -> None:
        changed = copy.deepcopy(self.candidate)
        changed["fallback"]["static_template_id"] = "other-template"
        self.assert_rejected(changed, "fallback-binding")

    def test_unavailable_receipt_has_no_candidate(self) -> None:
        static_path = self.write("static-only.json", self.static)
        result = jit_harness.unavailable(static_path, "late")
        self.assertEqual(result["decision"], "late")
        self.assertIsNone(result["candidate_sha256"])
        self.assertTrue(result["static_path_selected"])


if __name__ == "__main__":
    unittest.main()
