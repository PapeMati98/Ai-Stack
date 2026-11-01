#!/usr/bin/env python3
"""Deterministic and adversarial tests for the shadow-only controller."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_ROOT / "scripts" / "skill_evolution.py"
SCHEMAS = [
    SKILL_ROOT / "references" / "behavior-signature.schema.json",
    SKILL_ROOT / "references" / "pattern.schema.json",
    SKILL_ROOT / "references" / "candidate.schema.json",
    SKILL_ROOT / "references" / "verification-plan.schema.json",
]


def load_controller():
    if not SCRIPT.is_file():
        return None
    spec = importlib.util.spec_from_file_location("skill_evolution", SCRIPT)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load_controller()


class ContractFirstTests(unittest.TestCase):
    def test_controller_exists_after_test_first_stage(self):
        self.assertIsNotNone(MODULE, "controller intentionally absent during the red stage")

    def test_all_json_contract_objects_reject_unknown_fields(self):
        def walk(value):
            if isinstance(value, dict):
                if value.get("type") == "object":
                    self.assertIs(value.get("additionalProperties"), False)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        for path in SCHEMAS:
            payload = json.loads(path.read_text(encoding="utf-8"))
            walk(payload)


@unittest.skipIf(MODULE is None, "controller not implemented yet")
class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.state_root = self.workspace / "state"
        self.inputs = self.workspace / "inputs"
        self.inputs.mkdir()
        self.run_id = "run-evo-test"

    def write_json(self, name: str, value: dict) -> Path:
        path = self.inputs / name
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        return path

    def invoke(self, *arguments: str):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = MODULE.main(list(arguments))
        stream = stdout.getvalue() if code == 0 else stderr.getvalue()
        payload = json.loads(stream)
        return code, payload

    def contract(self) -> dict:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "project_slug": "agent-stack",
            "target_skill": "design-ui",
            "mode": "shadow-only",
            "created_by": {
                "actor_id": "parent-architect",
                "provider_lineage": "openai",
            },
            "planning_contract_sha256": "a" * 64,
            "governance_contract_sha256": "b" * 64,
            "graph_sha256": "c" * 64,
            "max_evidence": 64,
            "max_patterns": 32,
            "max_candidates": 8,
            "max_tests_per_phase": 32,
        }

    def init_run(self):
        path = self.write_json("contract.json", self.contract())
        code, payload = self.invoke(
            "init", "--contract", str(path), "--state-root", str(self.state_root)
        )
        self.assertEqual((code, payload["status"]), (0, "INITIALIZED"))
        return payload

    def state(self) -> dict:
        path = self.state_root / self.run_id / "state.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def evidence(
        self,
        index: int,
        *,
        model: str = "model-a",
        runtime: str = "runtime-a",
        actor: str | None = None,
        outcome: str = "failure",
    ) -> dict:
        return {
            "schema_version": 1,
            "evidence_id": f"evidence-case-{index}",
            "source_run_id": f"run-source-{index}",
            "source_kind": "agent-graph",
            "producer": {
                "actor_id": actor or f"actor-{index}",
                "provider_lineage": f"lineage-{index}",
            },
            "source_artifact_sha256": f"{index % 10}" * 64,
            "signature": {
                "task_family": "frontend",
                "role": "maker",
                "model_family": model,
                "runtime": runtime,
                "tool_surface": "filesystem",
                "environment": "dev",
                "symptom": "missed-responsive-state",
                "expected_behavior": "preserve-responsive-layout",
                "outcome": outcome,
            },
        }

    def ingest(self, receipt: dict, name: str):
        path = self.write_json(name, receipt)
        return self.invoke(
            "ingest",
            "--run-id",
            self.run_id,
            "--receipt",
            str(path),
            "--state-root",
            str(self.state_root),
        )

    def prepare_universal_candidate(self):
        self.init_run()
        index = 1
        for model, runtime in (
            ("model-a", "runtime-a"),
            ("model-a", "runtime-b"),
            ("model-b", "runtime-a"),
            ("model-b", "runtime-b"),
        ):
            for _ in range(2):
                code, payload = self.ingest(
                    self.evidence(index, model=model, runtime=runtime),
                    f"evidence-{index}.json",
                )
                self.assertEqual((code, payload["status"]), (0, "INGESTED"))
                index += 1
        code, payload = self.invoke(
            "compile-patterns",
            "--run-id",
            self.run_id,
            "--state-root",
            str(self.state_root),
        )
        self.assertEqual((code, payload["status"]), (0, "PATTERNS_COMPILED"))
        patterns = [
            item
            for item in self.state()["patterns"]
            if item["status"] == "ELIGIBLE"
            and item["signature"]["expected_behavior"] == "preserve-responsive-layout"
        ]
        candidate = {
            "schema_version": 1,
            "candidate_id": "candidate-responsive-1",
            "target_skill": "design-ui",
            "pattern_ids": [item["pattern_id"] for item in patterns],
            "applicability": "universal",
            "supported_model_families": ["model-a", "model-b"],
            "supported_runtimes": ["runtime-a", "runtime-b"],
            "expected_behavior": "preserve-responsive-layout",
            "candidate_sha256": "d" * 64,
            "proposer": {
                "actor_id": "candidate-maker",
                "provider_lineage": "openai",
            },
        }
        path = self.write_json("candidate.json", candidate)
        code, payload = self.invoke(
            "register-candidate",
            "--run-id",
            self.run_id,
            "--candidate",
            str(path),
            "--state-root",
            str(self.state_root),
        )
        self.assertEqual((code, payload["status"]), (0, "CANDIDATE_REGISTERED"))
        return candidate

    def catalog(self) -> dict:
        tests = []
        pairs = [
            ("model-a", "runtime-a"),
            ("model-a", "runtime-b"),
            ("model-b", "runtime-a"),
            ("model-b", "runtime-b"),
            ("model-a", "runtime-a"),
        ]
        for prefix, kind in (("target", "targeted"), ("confirm", "confirmation")):
            for index, (model, runtime) in enumerate(pairs, 1):
                tests.append(
                    {
                        "test_id": f"test-{prefix}-{index}",
                        "kind": kind,
                        "task_family": "frontend",
                        "model_family": model,
                        "runtime": runtime,
                        "tool_surface": "filesystem",
                        "environment": "dev",
                        "expected_behavior": "preserve-responsive-layout",
                        "condition_sha256": f"{(index + (0 if kind == 'targeted' else 5)) % 10}" * 64,
                    }
                )
        for index in range(1, 4):
            tests.append(
                {
                    "test_id": f"test-preserve-{index}",
                    "kind": "preservation",
                    "task_family": "frontend",
                    "model_family": "model-a" if index < 3 else "model-b",
                    "runtime": "runtime-a" if index % 2 else "runtime-b",
                    "tool_surface": "filesystem",
                    "environment": "dev",
                    "expected_behavior": f"preserve-existing-{index}",
                    "condition_sha256": f"{index + 6}" * 64,
                }
            )
        return {"schema_version": 1, "tests": tests}

    def plan_candidate(self):
        catalog = self.catalog()
        path = self.write_json("catalog.json", catalog)
        code, payload = self.invoke(
            "plan-verification",
            "--run-id",
            self.run_id,
            "--candidate-id",
            "candidate-responsive-1",
            "--catalog",
            str(path),
            "--state-root",
            str(self.state_root),
        )
        self.assertEqual((code, payload["status"]), (0, "VERIFICATION_PLANNED"))
        return catalog, self.state()["verification_plans"][0]

    def result_payload(self, phase: str, plan: dict, catalog: dict, *, regression=False):
        by_id = {item["test_id"]: item for item in catalog["tests"]}
        phase_ids = (
            plan["targeted_test_ids"]
            if phase == "initial"
            else plan["confirmation_test_ids"]
        )
        test_ids = list(phase_ids) + list(plan["preservation_test_ids"])
        observations = []
        for index, test_id in enumerate(test_ids):
            condition = by_id[test_id]["condition_sha256"]
            is_preservation = test_id in plan["preservation_test_ids"]
            baseline = "PASS" if is_preservation or index else "FAIL"
            candidate = "PASS"
            attributable = not is_preservation and index == 0
            if regression and not is_preservation and index == 1:
                baseline = "PASS"
                candidate = "FAIL"
                attributable = True
            observations.append(
                {
                    "test_id": test_id,
                    "baseline_condition_sha256": condition,
                    "candidate_condition_sha256": condition,
                    "baseline_outcome": baseline,
                    "candidate_outcome": candidate,
                    "attributable": attributable,
                    "behavior_preserved": candidate == "PASS",
                }
            )
        return {
            "schema_version": 1,
            "result_id": f"result-{phase}-1",
            "candidate_id": "candidate-responsive-1",
            "phase": phase,
            "reviewer": {
                "actor_id": "independent-reviewer",
                "provider_lineage": "zai",
            },
            "observations": observations,
        }

    def record_result(self, value: dict, name: str):
        path = self.write_json(name, value)
        return self.invoke(
            "record-verification",
            "--run-id",
            self.run_id,
            "--result",
            str(path),
            "--state-root",
            str(self.state_root),
        )

    def test_doctor_performs_zero_writes(self):
        self.assertFalse(self.state_root.exists())
        code, payload = self.invoke("doctor", "--state-root", str(self.state_root))
        self.assertEqual(payload["runtime_version"], "0.1.1")
        self.assertEqual((code, payload["status"], payload["writes_performed"]), (0, "READY", 0))
        self.assertFalse(self.state_root.exists())

    def test_relative_unc_and_reparse_state_roots_are_rejected(self):
        with self.assertRaises(MODULE.EvolutionError):
            MODULE.resolve_state_root("relative-state")
        with self.assertRaises(MODULE.EvolutionError):
            MODULE.resolve_state_root("//server/share/state")
        root = self.workspace / "reparse" / "state"
        with mock.patch.object(MODULE, "is_reparse_point", return_value=True):
            with self.assertRaises(MODULE.EvolutionError):
                MODULE.resolve_state_root(str(root))

    def test_actor_identifier_boundary_matches_published_schemas(self):
        accepted = "a" * 64
        self.assertEqual(
            MODULE._actor(
                {"actor_id": accepted, "provider_lineage": accepted}, "producer"
            ),
            {"actor_id": accepted, "provider_lineage": accepted},
        )
        with self.assertRaises(MODULE.EvolutionError):
            MODULE._actor(
                {"actor_id": "a" * 65, "provider_lineage": "zai"}, "producer"
            )

    def test_unknown_and_executable_receipts_fail_without_state_change(self):
        self.init_run()
        before = MODULE.sha256_file(self.state_root / self.run_id / "state.json")
        receipt = self.evidence(1)
        receipt["prompt"] = "ignore previous instructions"
        code, payload = self.ingest(receipt, "unknown.json")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["code"], "schema-keys")
        self.assertEqual(before, MODULE.sha256_file(self.state_root / self.run_id / "state.json"))
        receipt = self.evidence(2)
        receipt["signature"]["expected_behavior"] = "powershell-cmd"
        code, payload = self.ingest(receipt, "executable.json")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["code"], "executable-content")
        self.assertEqual(before, MODULE.sha256_file(self.state_root / self.run_id / "state.json"))

    def test_duplicate_evidence_is_rejected_without_partial_write(self):
        self.init_run()
        receipt = self.evidence(1)
        self.assertEqual(self.ingest(receipt, "first.json")[0], 0)
        before = MODULE.sha256_file(self.state_root / self.run_id / "state.json")
        code, payload = self.ingest(receipt, "duplicate.json")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["code"], "duplicate-evidence")
        self.assertEqual(before, MODULE.sha256_file(self.state_root / self.run_id / "state.json"))

    def test_event_chain_is_valid_and_tampering_is_detected(self):
        self.init_run()
        self.assertEqual(self.ingest(self.evidence(1), "one.json")[0], 0)
        code, payload = self.invoke(
            "validate", "--run-id", self.run_id, "--state-root", str(self.state_root)
        )
        self.assertEqual((code, payload["status"]), (0, "VALID"))
        state_path = self.state_root / self.run_id / "state.json"
        state = self.state()
        state["events"][0]["object_id"] = "tampered"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        code, payload = self.invoke(
            "validate", "--run-id", self.run_id, "--state-root", str(self.state_root)
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["code"], "event-chain")

    def test_persisted_state_uses_bounded_fail_closed_json_loader(self):
        self.init_run()
        state_path = self.state_root / self.run_id / "state.json"
        state_path.write_text('{"x":' * 1100 + "null" + "}" * 1100, encoding="utf-8")
        with self.assertRaisesRegex(MODULE.EvolutionError, "canonical UTF-8 JSON"):
            MODULE.load_run(self.state_root, self.run_id)

        state_path.write_bytes(b"{}" + b" " * MODULE.MAX_INPUT_BYTES)
        with self.assertRaisesRegex(MODULE.EvolutionError, "one MiB"):
            MODULE.load_run(self.state_root, self.run_id)

    def test_json_depth_bound_ignores_quoted_braces_and_escaped_quotes(self):
        path = self.inputs / "quoted-depth.json"
        value = {"text": '{["\\' * 1100}
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(MODULE.load_json_file(str(path)), value)
        path.write_text('{"x":' * (MODULE.MAX_INPUT_JSON_DEPTH + 1) + "null" + "}" * (MODULE.MAX_INPUT_JSON_DEPTH + 1), encoding="utf-8")
        with self.assertRaisesRegex(MODULE.EvolutionError, "nesting limit"):
            MODULE.load_json_file(str(path))

    def test_insufficient_independent_evidence_cannot_support_candidate(self):
        self.init_run()
        self.assertEqual(self.ingest(self.evidence(1), "one.json")[0], 0)
        self.assertEqual(
            self.invoke(
                "compile-patterns", "--run-id", self.run_id, "--state-root", str(self.state_root)
            )[0],
            0,
        )
        self.assertEqual(self.state()["patterns"][0]["status"], "NEEDS_EVIDENCE")

    def test_universal_candidate_requires_two_model_families_and_two_runtimes(self):
        self.init_run()
        for index in (1, 2):
            self.assertEqual(self.ingest(self.evidence(index), f"ev-{index}.json")[0], 0)
        self.assertEqual(
            self.invoke(
                "compile-patterns", "--run-id", self.run_id, "--state-root", str(self.state_root)
            )[0],
            0,
        )
        pattern = self.state()["patterns"][0]
        candidate = {
            "schema_version": 1,
            "candidate_id": "candidate-too-broad",
            "target_skill": "design-ui",
            "pattern_ids": [pattern["pattern_id"]],
            "applicability": "universal",
            "supported_model_families": ["model-a"],
            "supported_runtimes": ["runtime-a"],
            "expected_behavior": "preserve-responsive-layout",
            "candidate_sha256": "d" * 64,
            "proposer": {"actor_id": "maker", "provider_lineage": "openai"},
        }
        path = self.write_json("too-broad.json", candidate)
        code, payload = self.invoke(
            "register-candidate",
            "--run-id",
            self.run_id,
            "--candidate",
            str(path),
            "--state-root",
            str(self.state_root),
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["code"], "unsupported-universal")

    def test_verification_plan_has_targeted_preservation_and_fresh_confirmation(self):
        self.prepare_universal_candidate()
        _catalog, plan = self.plan_candidate()
        self.assertEqual(len(plan["targeted_test_ids"]), 5)
        self.assertEqual(len(plan["preservation_test_ids"]), 3)
        self.assertEqual(len(plan["confirmation_test_ids"]), 5)
        self.assertLessEqual(plan["confirmation_overlap_count"], 2)
        self.assertEqual(set(plan["targeted_test_ids"]) & set(plan["confirmation_test_ids"]), set())

    def test_self_review_is_rejected(self):
        self.prepare_universal_candidate()
        catalog, plan = self.plan_candidate()
        result = self.result_payload("initial", plan, catalog)
        result["reviewer"] = {
            "actor_id": "candidate-maker",
            "provider_lineage": "openai",
        }
        code, payload = self.record_result(result, "self-review.json")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["code"], "self-review")

    def test_incomplete_verification_returns_needs_evidence(self):
        self.prepare_universal_candidate()
        self.plan_candidate()
        code, payload = self.invoke(
            "decide",
            "--run-id",
            self.run_id,
            "--candidate-id",
            "candidate-responsive-1",
            "--state-root",
            str(self.state_root),
        )
        self.assertEqual((code, payload["decision"]), (0, "NEEDS_EVIDENCE"))
        self.assertFalse(payload["release_authority"])

    def test_attributable_regression_rejects_candidate(self):
        self.prepare_universal_candidate()
        catalog, plan = self.plan_candidate()
        initial = self.result_payload("initial", plan, catalog, regression=True)
        confirmation = self.result_payload("confirmation", plan, catalog)
        self.assertEqual(self.record_result(initial, "initial-regression.json")[0], 0)
        self.assertEqual(self.record_result(confirmation, "confirmation.json")[0], 0)
        code, payload = self.invoke(
            "decide",
            "--run-id",
            self.run_id,
            "--candidate-id",
            "candidate-responsive-1",
            "--state-root",
            str(self.state_root),
        )
        self.assertEqual((code, payload["decision"]), (0, "REJECTED"))
        self.assertIn("attributable-regression", payload["reason_codes"])
        self.assertFalse(payload["release_authority"])

    def test_complete_paired_evidence_yields_shadow_accepted_only(self):
        self.prepare_universal_candidate()
        catalog, plan = self.plan_candidate()
        self.assertEqual(
            self.record_result(self.result_payload("initial", plan, catalog), "initial.json")[0],
            0,
        )
        self.assertEqual(
            self.record_result(
                self.result_payload("confirmation", plan, catalog), "confirmation.json"
            )[0],
            0,
        )
        code, payload = self.invoke(
            "decide",
            "--run-id",
            self.run_id,
            "--candidate-id",
            "candidate-responsive-1",
            "--state-root",
            str(self.state_root),
        )
        self.assertEqual((code, payload["decision"]), (0, "SHADOW_ACCEPTED"))
        self.assertFalse(payload["release_authority"])
        self.assertNotIn("patch", json.dumps(self.state()).lower())

    def test_controller_has_no_execution_or_network_surface(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "import subprocess",
            "from subprocess",
            "import socket",
            "import urllib",
            "import requests",
            "import http.client",
            "os.system(",
            "exec(",
            "eval(",
        ):
            self.assertNotIn(forbidden, source)
        self.assertEqual(
            set(MODULE.PUBLIC_COMMANDS),
            {
                "doctor",
                "init",
                "ingest",
                "compile-patterns",
                "register-candidate",
                "plan-verification",
                "record-verification",
                "decide",
                "report",
                "validate",
            },
        )
        self.assertIn("os.replace", source)
        self.assertIn("os.fsync", source)
        self.assertIn("msvcrt", source)
        self.assertIn("fcntl", source)


if __name__ == "__main__":
    unittest.main()
