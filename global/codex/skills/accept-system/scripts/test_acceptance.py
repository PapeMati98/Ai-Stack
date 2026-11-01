from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("acceptance.py")
SPEC = importlib.util.spec_from_file_location("acceptance", MODULE_PATH)
assert SPEC and SPEC.loader
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def artifact(path: Path, content: str) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content.encode("utf-8")
    path.write_bytes(payload)
    return {
        "path": path.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


class AcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_root = self.root / "state"
        self.input_root = self.root / "inputs"
        self.input_root.mkdir()
        self.graph_run_id = "run-graph-20260827T000000Z-0123456789"
        self.graph = {
            "schema_version": 1,
            "graph_id": "graph-acceptance-fixture",
            "revision": 1,
            "nodes": [
                {
                    "node_id": "node-final",
                    "active": True,
                    "capability": "final-acceptance",
                }
            ],
        }
        graph_hash = acceptance.sha256_value(self.graph)
        self.planning = {
            "schema_version": 1,
            "status": "approved",
            "contract_id": "plan-acceptance-fixture",
            "contract_hash": "a" * 64,
            "graph": {"projection_sha256": graph_hash},
            "requirements": [
                {"requirement_id": "REQ-001", "material": True, "status": "active"},
                {"requirement_id": "REQ-002", "material": True, "status": "active"},
                {"requirement_id": "REQ-003", "material": True, "status": "active"},
            ],
        }
        independence = {
            "not_same_actor": True,
            "not_same_lineage": True,
            "allowed_roles": ["independent-reviewer"],
        }
        self.contract = {
            "schema_version": 1,
            "acceptance_id": "accept-fixture-system",
            "project_slug": "fixture",
            "status": "READY",
            "planning_contract": {
                "contract_id": self.planning["contract_id"],
                "contract_hash": self.planning["contract_hash"],
            },
            "graph": {"graph_id": self.graph["graph_id"], "graph_hash": graph_hash},
            "environment": {
                "name": "DEV",
                "target": "http://fixture.invalid",
                "synthetic_data": True,
                "recipient_policy": "synthetic-only",
                "external_side_effects": "blocked",
                "allowed_destinations": [],
                "denied_effects": ["email", "payment", "calendar", "crm", "public-integration"],
                "enforcement_scope": [
                    {
                        "scope": "browser-context",
                        "mechanism": "Playwright request routing blocks non-fixture traffic",
                        "evidence_ref": "policy:browser",
                    },
                    {
                        "scope": "application",
                        "mechanism": "Application fake adapters reject real recipients",
                        "evidence_ref": "policy:application",
                    },
                ],
                "owner_approval": None,
            },
            "maker": {"id": "maker-one", "lineage": "openai", "role": "implementer"},
            "max_generations": 3,
            "criteria": [
                {
                    "criterion_id": "accept-business-formula",
                    "title": "Business formula matches the owner rule",
                    "requirement_refs": ["REQ-001"],
                    "mandatory": True,
                    "applicability": "applicable",
                    "domain": "business",
                    "oracle_kind": "business-oracle",
                    "oracle": {
                        "expected": "total equals quantity multiplied by approved unit price",
                        "method": "compare deterministic fixture output with owner-approved formula",
                        "source_ref": "intake:business-rule-1",
                    },
                    "evidence_kinds": ["deterministic-check"],
                    "max_age_seconds": 3600,
                    "reviewer_independence": copy.deepcopy(independence),
                    "n_a_authority": None,
                    "required_enforcement_scopes": [],
                },
                {
                    "criterion_id": "accept-user-journey",
                    "title": "User can complete the visible journey",
                    "requirement_refs": ["REQ-002"],
                    "mandatory": True,
                    "applicability": "applicable",
                    "domain": "ui-functional",
                    "oracle_kind": "user-journey-evidence",
                    "oracle": {
                        "expected": "user sees saved synthetic order and confirmation",
                        "method": "role-based browser journey on isolated fixture",
                        "source_ref": "intake:user-journey-1",
                    },
                    "evidence_kinds": ["browser-e2e", "visual-evidence"],
                    "max_age_seconds": 3600,
                    "reviewer_independence": copy.deepcopy(independence),
                    "n_a_authority": None,
                    "required_enforcement_scopes": [],
                },
                {
                    "criterion_id": "accept-side-effect-safety",
                    "title": "External effects stay blocked in DEV",
                    "requirement_refs": ["REQ-003"],
                    "mandatory": True,
                    "applicability": "applicable",
                    "domain": "side-effect",
                    "oracle_kind": "side-effect-safety",
                    "oracle": {
                        "expected": "no request can reach a real external destination",
                        "method": "verify browser and application enforcement logs",
                        "source_ref": "global-policy:dev-side-effects",
                    },
                    "evidence_kinds": ["deterministic-check"],
                    "max_age_seconds": 3600,
                    "reviewer_independence": copy.deepcopy(independence),
                    "n_a_authority": None,
                    "required_enforcement_scopes": ["browser-context", "application"],
                },
            ],
        }
        present_domains = {item["domain"] for item in self.contract["criteria"]}
        user_domains = {"ui-functional", "ux", "responsive", "accessibility"}
        for domain in sorted(acceptance.DOMAINS - present_domains):
            user_facing = domain in user_domains
            self.contract["criteria"].append(
                {
                    "criterion_id": f"accept-na-{domain}",
                    "title": f"{domain} is explicitly not applicable",
                    "requirement_refs": ["REQ-001"],
                    "mandatory": True,
                    "applicability": "not-applicable",
                    "domain": domain,
                    "oracle_kind": (
                        "user-journey-evidence" if user_facing else "business-oracle"
                    ),
                    "oracle": {
                        "expected": f"{domain} boundary is absent from this fixture",
                        "method": "verify the trusted discovery record before execution",
                        "source_ref": f"discovery:fixture-{domain}",
                    },
                    "evidence_kinds": (
                        ["browser-e2e"] if user_facing else ["deterministic-check"]
                    ),
                    "max_age_seconds": 3600,
                    "reviewer_independence": copy.deepcopy(independence),
                    "n_a_authority": {
                        "kind": "trusted-discovery",
                        "actor_id": "discovery-one",
                        "actor_role": "discovery-gate",
                        "evidence_ref": f"discovery:fixture-{domain}",
                    },
                    "required_enforcement_scopes": [],
                }
            )
        self.contract_path = self.input_root / "acceptance.json"
        self.planning_path = self.input_root / "planning.json"
        self.graph_path = self.input_root / "graph.json"
        write_json(self.contract_path, self.contract)
        write_json(self.planning_path, self.planning)
        write_json(self.graph_path, self.graph)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def init_run(self) -> str:
        result = acceptance.command_init(
            argparse.Namespace(
                contract=str(self.contract_path),
                planning_contract=str(self.planning_path),
                graph=str(self.graph_path),
                graph_run_id=self.graph_run_id,
                state_root=str(self.state_root),
            )
        )
        return result["run_id"]

    def binding(self, revision: str = "one", digest: str = "b") -> dict:
        return {
            "schema_version": 1,
            "binding_id": f"binding-{revision}",
            "code": {
                "revision": revision,
                "sha256": digest * 64,
                "evidence_ref": f"fixture:{revision}",
            },
            "environment": {
                "name": "DEV",
                "target": "http://fixture.invalid",
                "policy_sha256": "c" * 64,
            },
            "maker": copy.deepcopy(self.contract["maker"]),
        }

    def start_run(self, run_id: str, binding: dict | None = None, operation: str = "op-start-one") -> dict:
        path = self.input_root / f"{operation}-binding.json"
        write_json(path, binding or self.binding())
        return acceptance.command_start(
            argparse.Namespace(
                run_id=run_id,
                operation_id=operation,
                binding=str(path),
                state_root=str(self.state_root),
            )
        )

    def evidence(
        self,
        criterion_id: str,
        generation: int,
        binding_sha256: str,
        evidence_id: str,
        status: str = "PASS",
        observed_at: str | None = None,
        reviewer: dict | None = None,
    ) -> tuple[dict, Path]:
        criterion = next(item for item in self.contract["criteria"] if item["criterion_id"] == criterion_id)
        evidence_root = self.input_root / evidence_id
        evidence_root.mkdir(parents=True, exist_ok=True)
        if criterion.get("surface", "graphical") == "cli":
            item_artifacts = [
                artifact(evidence_root / "stdout.txt", f"stdout-{evidence_id}-{status}"),
                artifact(evidence_root / "stderr.txt", f"stderr-{evidence_id}-{status}"),
            ]
        else:
            item_artifacts = [artifact(evidence_root / f"{evidence_id}.txt", f"proof-{evidence_id}-{status}")]
        journey = None
        enforcement = None
        if criterion["oracle_kind"] == "user-journey-evidence" and criterion.get("surface", "graphical") == "cli":
            journey = {
                "role": "synthetic-cli-user",
                "command": criterion["cli"]["argv"],
                "actions": ["invoke command", "capture stdout", "capture stderr", "inspect exit code"],
                "expected": criterion["oracle"]["expected"],
                "actual": "command produced the frozen expected result",
                "exit_code": criterion["cli"]["expected_exit_code"],
                "expected_exit_code": criterion["cli"]["expected_exit_code"],
            }
        elif criterion["oracle_kind"] == "user-journey-evidence":
            journey = {
                "role": "synthetic-user",
                "route": "/orders/new",
                "viewport": {"width": 1280, "height": 800},
                "actions": ["open form", "enter synthetic data", "submit", "inspect confirmation"],
                "expected": criterion["oracle"]["expected"],
                "actual": "user sees saved synthetic order and confirmation",
            }
        if criterion["oracle_kind"] == "side-effect-safety":
            env_by_scope = {item["scope"]: item for item in self.contract["environment"]["enforcement_scope"]}
            enforcement = [
                {
                    "scope": scope,
                    "mechanism": env_by_scope[scope]["mechanism"],
                    "evidence_ref": f"artifact:{item_artifacts[0]['path']}",
                }
                for scope in criterion["required_enforcement_scopes"]
            ]
        value = {
            "schema_version": 1,
            "evidence_id": evidence_id,
            "criterion_id": criterion_id,
            "generation": generation,
            "binding_sha256": binding_sha256,
            "status": status,
            "reviewer": reviewer
            or {"id": "reviewer-one", "lineage": "xai", "role": "independent-reviewer"},
            "observed_at": observed_at or acceptance.utc_now(),
            "summary": f"Independent {status} for {criterion_id}",
            "oracle_result": {
                "expected": criterion["oracle"]["expected"],
                "actual": "observed fixture result",
                "method": criterion["oracle"]["method"],
            },
            "evidence_kind": criterion["evidence_kinds"][0],
            "artifacts": item_artifacts,
            "journey": journey,
            "enforcement_scope": enforcement,
            "runtime": {
                "helper_sha256": acceptance.runtime_identity()["helper_sha256"],
                "skill_manifest_sha256": acceptance.runtime_identity()["skill_manifest_sha256"],
            },
            "findings": []
            if status == "PASS"
            else [
                {
                    "finding_id": f"finding-{evidence_id}",
                    "severity": "high",
                    "summary": "Synthetic defect requires repair",
                }
            ],
        }
        path = evidence_root / "evidence.json"
        write_json(path, value)
        return value, path

    def make_cli_contract(self, expected_exit_code: int = 0) -> None:
        criterion = next(item for item in self.contract["criteria"] if item["criterion_id"] == "accept-user-journey")
        criterion["surface"] = "cli"
        criterion["cli"] = {
            "argv": ["python", "-c", "print('fixture')"],
            "expected_exit_code": expected_exit_code,
        }
        criterion["evidence_kinds"] = ["deterministic-check"]
        write_json(self.contract_path, self.contract)

    def record(self, run_id: str, criterion_id: str, evidence_path: Path, operation: str) -> dict:
        return acceptance.command_result(
            argparse.Namespace(
                run_id=run_id,
                operation_id=operation,
                criterion_id=criterion_id,
                evidence=str(evidence_path),
                state_root=str(self.state_root),
            )
        )

    def prepare_evidence_only_rebind(self) -> tuple[str, dict, dict[str, Path]]:
        run_id = self.init_run()
        started = self.start_run(run_id)
        binding_hash = started["binding_sha256"]
        business, business_path = self.evidence(
            "accept-business-formula", 1, binding_hash, "evidence-rebind-business-fail", "FAIL"
        )
        _, journey_path = self.evidence(
            "accept-user-journey", 1, binding_hash, "evidence-rebind-journey-pass"
        )
        _, safety_path = self.evidence(
            "accept-side-effect-safety", 1, binding_hash, "evidence-rebind-safety-pass"
        )
        self.record(run_id, business["criterion_id"], business_path, "op-rebind-business-fail")
        self.record(run_id, "accept-user-journey", journey_path, "op-rebind-journey-pass")
        self.record(run_id, "accept-side-effect-safety", safety_path, "op-rebind-safety-pass")
        acceptance.command_close(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-rebind-close-first",
                stop_reason=None,
                output=None,
                state_root=str(self.state_root),
            )
        )
        return run_id, started, {
            "business": business_path,
            "journey": journey_path,
            "safety": safety_path,
        }

    def evidence_only_rebind(
        self,
        run_id: str,
        binding: dict,
        preserve_ids: list[str],
        changed_paths: list[str] | None = None,
        operation: str = "op-evidence-only-rebind",
    ) -> dict:
        binding_path = self.input_root / f"{operation}-binding.json"
        write_json(binding_path, binding)
        repair_root = self.input_root / operation
        repair_artifact = artifact(repair_root / "packet.txt", "transported formal packet")
        resolution = {
            "schema_version": 1,
            "reason": "Transported the unchanged formal acceptance packet.",
            "resolved_evidence_ids": ["evidence-rebind-business-fail"],
            "preserve_pass_evidence_ids": preserve_ids,
            "changed_paths": changed_paths or [],
            "packet_paths": ["transport/formal-packet.json"],
            "artifacts": [repair_artifact],
        }
        resolution_path = repair_root / "resolution.json"
        write_json(resolution_path, resolution)
        return acceptance.command_evidence_only_rebind(
            argparse.Namespace(
                run_id=run_id,
                operation_id=operation,
                binding=str(binding_path),
                resolution=str(resolution_path),
                state_root=str(self.state_root),
            )
        )

    def test_doctor_is_local_and_deterministic(self) -> None:
        result = acceptance.command_doctor(argparse.Namespace())
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["model_calls"], 0)

    def test_user_perception_gates_are_normative_and_referenced(self) -> None:
        skill_root = MODULE_PATH.parents[1]
        skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        contract = (skill_root / "references" / "acceptance-contract.md").read_text(
            encoding="utf-8"
        )
        evidence = (
            skill_root / "references" / "user-perception-and-feedback.md"
        ).read_text(encoding="utf-8")
        self.assertIn("user-perception-and-feedback.md", skill)
        for gate in (
            "surface-reachability",
            "action-feedback",
            "visible-failure-recovery",
        ):
            self.assertIn(gate, contract)
            self.assertIn(gate, evidence)
        for phrase in (
            "obowiazkowymi kryteriami PASS/FAIL",
            "initial_screen",
            "injected_condition",
            "system_correlation",
            "Maker nie wystawia finalnego werdyktu",
        ):
            self.assertIn(phrase, evidence)

    def test_contract_binding_validates_complete_requirement_coverage(self) -> None:
        result = acceptance.validate_contract_bindings(self.contract, self.planning, self.graph)
        self.assertEqual(result["criterion_count"], len(acceptance.DOMAINS))
        self.assertEqual(result["material_requirement_count"], 3)

    def test_missing_domain_disposition_fails_closed(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["criteria"] = [
            item for item in candidate["criteria"] if item["domain"] != "reliability"
        ]
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "missing explicit domain applicability"
        ):
            acceptance.validate_contract(candidate)

    def test_missing_material_requirement_fails_closed(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["criteria"][2]["requirement_refs"] = ["REQ-002"]
        with self.assertRaisesRegex(acceptance.AcceptanceError, "misses material requirements"):
            acceptance.validate_contract_bindings(candidate, self.planning, self.graph)

    def test_material_requirement_cannot_be_covered_only_by_na(self) -> None:
        candidate = copy.deepcopy(self.contract)
        criterion = candidate["criteria"][1]
        criterion["applicability"] = "not-applicable"
        criterion["n_a_authority"] = {
            "kind": "trusted-discovery",
            "actor_id": "discovery-one",
            "actor_role": "discovery-gate",
            "evidence_ref": "discovery:ui-boundary",
        }
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "lacks applicable mandatory acceptance"
        ):
            acceptance.validate_contract_bindings(candidate, self.planning, self.graph)

    def test_dev_real_destination_fails_closed(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["environment"]["allowed_destinations"] = ["real@example.com"]
        with self.assertRaisesRegex(acceptance.AcceptanceError, "must not contain allowed real"):
            acceptance.validate_contract(candidate)

    def test_business_placeholder_oracle_fails_closed(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["criteria"][0]["oracle"]["source_ref"] = "TODO"
        with self.assertRaisesRegex(acceptance.AcceptanceError, "placeholder"):
            acceptance.validate_contract(candidate)

    def test_maker_authored_na_fails_closed(self) -> None:
        candidate = copy.deepcopy(self.contract)
        criterion = candidate["criteria"][1]
        criterion["applicability"] = "not-applicable"
        criterion["n_a_authority"] = {
            "kind": "trusted-discovery",
            "actor_id": "maker-one",
            "actor_role": "discovery-gate",
            "evidence_ref": "discovery:test",
        }
        with self.assertRaisesRegex(acceptance.AcceptanceError, "maker cannot author N/A"):
            acceptance.validate_contract(candidate)

    def test_lifecycle_rejects_repairs_and_requires_full_retest(self) -> None:
        run_id = self.init_run()
        started = self.start_run(run_id)
        binding_hash = started["binding_sha256"]
        business, business_path = self.evidence(
            "accept-business-formula", 1, binding_hash, "evidence-business-fail", "FAIL"
        )
        _, journey_path = self.evidence(
            "accept-user-journey", 1, binding_hash, "evidence-journey-pass"
        )
        _, safety_path = self.evidence(
            "accept-side-effect-safety", 1, binding_hash, "evidence-safety-pass"
        )
        self.record(run_id, business["criterion_id"], business_path, "op-result-business-fail")
        self.record(run_id, "accept-user-journey", journey_path, "op-result-journey-pass")
        self.record(run_id, "accept-side-effect-safety", safety_path, "op-result-safety-pass")
        first = acceptance.command_close(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-close-first",
                stop_reason=None,
                output=None,
                state_root=str(self.state_root),
            )
        )
        self.assertEqual(first["status"], "NOT_ACCEPTED")
        self.assertFalse(first["final"])

        new_binding = self.binding("two", "d")
        binding_path = self.input_root / "binding-two.json"
        write_json(binding_path, new_binding)
        repair_root = self.input_root / "repair-two"
        repair_artifact = artifact(repair_root / "repair.txt", "fixed formula")
        resolution = {
            "schema_version": 1,
            "reason": "Corrected the business formula and added a regression test.",
            "resolved_evidence_ids": ["evidence-business-fail"],
            "changed_paths": ["src/formula.py", "tests/test_formula.py"],
            "artifacts": [repair_artifact],
        }
        resolution_path = repair_root / "resolution.json"
        write_json(resolution_path, resolution)
        repaired = acceptance.command_repair(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-repair-two",
                binding=str(binding_path),
                resolution=str(resolution_path),
                state_root=str(self.state_root),
            )
        )
        self.assertTrue(repaired["all_prior_passes_invalidated"])
        state = acceptance._load_state(self.state_root / run_id)
        criteria_state = state["generations"][-1]["criteria"]
        applicable = {
            item["criterion_id"]
            for item in self.contract["criteria"]
            if item["applicability"] == "applicable"
        }
        self.assertEqual(
            {criterion_id for criterion_id, item in criteria_state.items() if item["status"] == "PENDING"},
            applicable,
        )
        self.assertEqual(
            {
                criterion_id
                for criterion_id, item in criteria_state.items()
                if item["status"] == "N/A"
            },
            set(criteria_state) - applicable,
        )

        new_hash = repaired["binding_sha256"]
        _, business_pass_path = self.evidence(
            "accept-business-formula", 2, new_hash, "evidence-business-pass-g2"
        )
        self.record(run_id, "accept-business-formula", business_pass_path, "op-result-business-pass-g2")
        incomplete = acceptance.command_close(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-close-incomplete-g2",
                stop_reason=None,
                output=None,
                state_root=str(self.state_root),
            )
        )
        self.assertEqual({item["criterion_id"] for item in incomplete["blocking"]}, {"accept-user-journey", "accept-side-effect-safety"})
        _, journey_g2 = self.evidence("accept-user-journey", 2, new_hash, "evidence-journey-pass-g2")
        _, safety_g2 = self.evidence("accept-side-effect-safety", 2, new_hash, "evidence-safety-pass-g2")
        self.record(run_id, "accept-user-journey", journey_g2, "op-result-journey-pass-g2")
        self.record(run_id, "accept-side-effect-safety", safety_g2, "op-result-safety-pass-g2")
        accepted = acceptance.command_close(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-close-accepted",
                stop_reason=None,
                output=None,
                state_root=str(self.state_root),
            )
        )
        self.assertEqual(accepted["status"], "ACCEPTED")
        verified = acceptance.verify_receipt_document(Path(accepted["receipt_path"]), require_accepted=True)
        self.assertEqual(verified["status"], "VERIFIED")
        self.assertEqual(verified["graph_run_id"], self.graph_run_id)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "Agent Graph run_id mismatch"):
            acceptance.verify_receipt_document(
                Path(accepted["receipt_path"]),
                expected_graph_run_id="run-graph-20260827T000001Z-abcdef0123",
                require_accepted=True,
            )

    def test_evidence_only_rebind_preserves_explicit_unchanged_passes(self) -> None:
        run_id, started, _ = self.prepare_evidence_only_rebind()
        rebound = self.evidence_only_rebind(
            run_id,
            self.binding(),
            ["evidence-rebind-journey-pass", "evidence-rebind-safety-pass"],
        )
        self.assertEqual(rebound["status"], "EVIDENCE_ONLY_REBIND_STARTED")
        self.assertFalse(rebound["all_prior_passes_invalidated"])
        state = acceptance._load_state(self.state_root / run_id)
        criteria = state["generations"][-1]["criteria"]
        self.assertEqual(criteria["accept-user-journey"]["status"], "PASS")
        self.assertEqual(criteria["accept-side-effect-safety"]["status"], "PASS")
        self.assertEqual(criteria["accept-business-formula"]["status"], "PENDING")
        self.assertEqual(state["generations"][-1]["binding_sha256"], started["binding_sha256"])

    def test_evidence_only_rebind_rejects_product_or_binding_change(self) -> None:
        run_id, _, _ = self.prepare_evidence_only_rebind()
        with self.assertRaisesRegex(acceptance.AcceptanceError, "unchanged product binding"):
            self.evidence_only_rebind(
                run_id,
                self.binding("changed-product", "d"),
                ["evidence-rebind-journey-pass"],
                operation="op-evidence-only-product-change",
            )
        with self.assertRaisesRegex(acceptance.AcceptanceError, "product paths changed"):
            self.evidence_only_rebind(
                run_id,
                self.binding(),
                ["evidence-rebind-journey-pass"],
                changed_paths=["src/product.py"],
                operation="op-evidence-only-path-change",
            )
        state = acceptance._load_state(self.state_root / run_id)
        self.assertEqual(state["current_generation"], 1)

    def test_evidence_only_rebind_can_close_without_retesting_unchanged_passes(self) -> None:
        run_id, _, _ = self.prepare_evidence_only_rebind()
        rebound = self.evidence_only_rebind(
            run_id, self.binding(),
            ["evidence-rebind-journey-pass", "evidence-rebind-safety-pass"],
        )
        _, corrected = self.evidence(
            "accept-business-formula", 2, rebound["binding_sha256"],
            "evidence-corrected-packet-g2",
        )
        self.record(run_id, "accept-business-formula", corrected, "op-corrected-packet")
        closed = acceptance.command_close(argparse.Namespace(
            run_id=run_id, operation_id="op-close-evidence-only", stop_reason=None,
            output=None, state_root=str(self.state_root),
        ))
        self.assertEqual(closed["status"], "ACCEPTED")
        verified = acceptance.verify_receipt_document(
            Path(closed["receipt_path"]), require_accepted=True,
        )
        self.assertEqual(verified["status"], "VERIFIED")

    def test_evidence_only_rebind_rejects_criteria_drift(self) -> None:
        run_id, _, _ = self.prepare_evidence_only_rebind()
        snapshot = self.state_root / run_id / "contracts" / "acceptance.json"
        contract = json.loads(snapshot.read_text(encoding="utf-8"))
        contract["criteria"][0]["title"] = "changed after init"
        write_json(snapshot, contract)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "Acceptance contract changed"):
            self.evidence_only_rebind(
                run_id,
                self.binding(),
                ["evidence-rebind-journey-pass"],
                operation="op-evidence-only-criteria-drift",
            )

    def test_evidence_only_rebind_rejects_tampered_manifest(self) -> None:
        run_id, _, _ = self.prepare_evidence_only_rebind()
        state = acceptance._load_state(self.state_root / run_id)
        record = state["generations"][0]["criteria"]["accept-user-journey"]["evidence"]
        manifest = self.state_root / run_id / record["manifest_path"]
        evidence_value = json.loads(manifest.read_text(encoding="utf-8"))
        evidence_value["summary"] = "tampered evidence"
        write_json(manifest, evidence_value)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "Evidence manifest drift"):
            self.evidence_only_rebind(
                run_id,
                self.binding(),
                ["evidence-rebind-journey-pass"],
                operation="op-evidence-only-tamper",
            )

    def test_evidence_only_rebind_rejects_non_independent_reviewer(self) -> None:
        run_id, _, _ = self.prepare_evidence_only_rebind()
        state = acceptance._load_state(self.state_root / run_id)
        record = state["generations"][0]["criteria"]["accept-user-journey"]["evidence"]
        manifest = self.state_root / run_id / record["manifest_path"]
        evidence_value = json.loads(manifest.read_text(encoding="utf-8"))
        evidence_value["reviewer"] = {
            "id": "other-maker-lineage",
            "lineage": "openai",
            "role": "independent-reviewer",
        }
        write_json(manifest, evidence_value)
        record["manifest_sha256"] = acceptance.sha256_file(manifest)
        write_json(self.state_root / run_id / "state.json", state)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "reviewer lineage"):
            self.evidence_only_rebind(
                run_id,
                self.binding(),
                ["evidence-rebind-journey-pass"],
                operation="op-evidence-only-independence",
            )

    def test_result_operation_is_idempotent(self) -> None:
        run_id = self.init_run()
        started = self.start_run(run_id)
        _, evidence_path = self.evidence(
            "accept-business-formula", 1, started["binding_sha256"], "evidence-idempotent"
        )
        first = self.record(run_id, "accept-business-formula", evidence_path, "op-result-idempotent")
        second = self.record(run_id, "accept-business-formula", evidence_path, "op-result-idempotent")
        self.assertEqual(first, second)

    def test_same_criterion_cannot_be_rerun_in_generation(self) -> None:
        run_id = self.init_run()
        started = self.start_run(run_id)
        _, first_path = self.evidence(
            "accept-business-formula", 1, started["binding_sha256"], "evidence-first-result"
        )
        self.record(run_id, "accept-business-formula", first_path, "op-result-first")
        _, second_path = self.evidence(
            "accept-business-formula", 1, started["binding_sha256"], "evidence-second-result"
        )
        with self.assertRaisesRegex(acceptance.AcceptanceError, "already has a result"):
            self.record(run_id, "accept-business-formula", second_path, "op-result-second")

    def test_self_review_fails_closed(self) -> None:
        run_id = self.init_run()
        started = self.start_run(run_id)
        _, path = self.evidence(
            "accept-business-formula",
            1,
            started["binding_sha256"],
            "evidence-self-review",
            reviewer={"id": "maker-one", "lineage": "openai", "role": "independent-reviewer"},
        )
        with self.assertRaisesRegex(acceptance.AcceptanceError, "Maker cannot review"):
            self.record(run_id, "accept-business-formula", path, "op-result-self-review")

    def test_stale_evidence_fails_closed(self) -> None:
        run_id = self.init_run()
        started = self.start_run(run_id)
        stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        _, path = self.evidence(
            "accept-business-formula", 1, started["binding_sha256"], "evidence-stale", observed_at=stale
        )
        with self.assertRaisesRegex(acceptance.AcceptanceError, "stale"):
            self.record(run_id, "accept-business-formula", path, "op-result-stale")

    def test_missing_user_journey_fails_closed(self) -> None:
        run_id = self.init_run()
        started = self.start_run(run_id)
        evidence, path = self.evidence(
            "accept-user-journey", 1, started["binding_sha256"], "evidence-no-journey"
        )
        evidence["journey"] = None
        write_json(path, evidence)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "requires journey evidence"):
            self.record(run_id, "accept-user-journey", path, "op-result-no-journey")

    def test_cli_journey_accepts_matching_zero_exit_and_output_artifacts(self) -> None:
        self.make_cli_contract()
        run_id = self.init_run()
        started = self.start_run(run_id)
        _, path = self.evidence("accept-user-journey", 1, started["binding_sha256"], "evidence-cli-pass")
        result = self.record(run_id, "accept-user-journey", path, "op-result-cli-pass")
        self.assertEqual(result["verdict"], "PASS")

    def test_cli_argv_preserves_repeated_and_empty_arguments(self) -> None:
        argv = ["python", "tool.py", "--include", "a", "--include", "a", ""]
        self.assertEqual(acceptance.require_argv(argv, "argv"), argv)
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.require_argv(["python", "bad\x00arg"], "argv")

    def test_cli_journey_accepts_legitimate_nonzero_expected_exit(self) -> None:
        self.make_cli_contract(expected_exit_code=2)
        run_id = self.init_run()
        started = self.start_run(run_id)
        _, path = self.evidence("accept-user-journey", 1, started["binding_sha256"], "evidence-cli-negative")
        result = self.record(run_id, "accept-user-journey", path, "op-result-cli-negative")
        self.assertEqual(result["verdict"], "PASS")

    def test_cli_journey_rejects_missing_stdout_or_stderr_artifact_without_state_change(self) -> None:
        self.make_cli_contract()
        run_id = self.init_run()
        started = self.start_run(run_id)
        evidence, path = self.evidence("accept-user-journey", 1, started["binding_sha256"], "evidence-cli-missing-artifact")
        evidence["artifacts"] = evidence["artifacts"][:1]
        write_json(path, evidence)
        run_dir = self.state_root / run_id
        before_state = acceptance._load_state(run_dir)
        before_files = sorted(item.relative_to(run_dir).as_posix() for item in run_dir.rglob("*") if item.is_file())
        with self.assertRaisesRegex(acceptance.AcceptanceError, "stdout and stderr artifacts"):
            self.record(run_id, "accept-user-journey", path, "op-result-cli-missing-artifact")
        self.assertEqual(before_state, acceptance._load_state(run_dir))
        self.assertEqual(before_files, sorted(item.relative_to(run_dir).as_posix() for item in run_dir.rglob("*") if item.is_file()))

    def test_cli_pass_rejects_wrong_exit_code(self) -> None:
        self.make_cli_contract()
        run_id = self.init_run()
        started = self.start_run(run_id)
        evidence, path = self.evidence("accept-user-journey", 1, started["binding_sha256"], "evidence-cli-wrong-exit")
        evidence["journey"]["exit_code"] = 1
        write_json(path, evidence)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "CLI PASS requires"):
            self.record(run_id, "accept-user-journey", path, "op-result-cli-wrong-exit")

    def test_graphical_journey_rejects_cli_evidence_payload(self) -> None:
        run_id = self.init_run()
        started = self.start_run(run_id)
        evidence, path = self.evidence("accept-user-journey", 1, started["binding_sha256"], "evidence-gui-cli-bypass")
        evidence["journey"] = {
            "role": "synthetic-cli-user",
            "command": ["python", "-c", "print('fixture')"],
            "actions": ["invoke command"],
            "expected": self.contract["criteria"][1]["oracle"]["expected"],
            "actual": "command output",
            "exit_code": 0,
            "expected_exit_code": 0,
        }
        write_json(path, evidence)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "missing keys|unsupported keys"):
            self.record(run_id, "accept-user-journey", path, "op-result-gui-cli-bypass")

    def test_evidence_runtime_drift_fails_closed(self) -> None:
        run_id = self.init_run()
        started = self.start_run(run_id)
        evidence, path = self.evidence(
            "accept-business-formula", 1, started["binding_sha256"], "evidence-runtime-drift"
        )
        evidence["runtime"]["helper_sha256"] = "0" * 64
        write_json(path, evidence)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "helper hash differs"):
            self.record(run_id, "accept-business-formula", path, "op-result-runtime-drift")

    def test_score_key_fails_closed(self) -> None:
        run_id = self.init_run()
        started = self.start_run(run_id)
        evidence, path = self.evidence(
            "accept-business-formula", 1, started["binding_sha256"], "evidence-score"
        )
        evidence["score"] = 99
        write_json(path, evidence)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "score key"):
            self.record(run_id, "accept-business-formula", path, "op-result-score")

    def test_sensitive_artifact_name_fails_closed(self) -> None:
        run_id = self.init_run()
        started = self.start_run(run_id)
        evidence, path = self.evidence(
            "accept-business-formula", 1, started["binding_sha256"], "evidence-sensitive"
        )
        root = path.parent
        sensitive = artifact(root / "cookies.json", "not-real")
        evidence["artifacts"] = [sensitive]
        write_json(path, evidence)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "sensitive browser/auth material"):
            self.record(run_id, "accept-business-formula", path, "op-result-sensitive")

    def test_storage_state_key_fails_closed(self) -> None:
        run_id = self.init_run()
        started = self.start_run(run_id)
        evidence, path = self.evidence(
            "accept-business-formula", 1, started["binding_sha256"], "evidence-storage-state"
        )
        evidence["storageState"] = {"cookies": []}
        write_json(path, evidence)
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "forbidden sensitive key"
        ):
            self.record(run_id, "accept-business-formula", path, "op-result-storage-state")

    def test_browser_mechanism_cannot_claim_host_scope(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["environment"]["enforcement_scope"].append(
            {
                "scope": "host",
                "mechanism": "Playwright browser route abort",
                "evidence_ref": "policy:false-host",
            }
        )
        candidate["criteria"][2]["required_enforcement_scopes"].append("host")
        self.contract = candidate
        write_json(self.contract_path, candidate)
        run_id = self.init_run()
        started = self.start_run(run_id)
        _, path = self.evidence(
            "accept-side-effect-safety", 1, started["binding_sha256"], "evidence-false-host"
        )
        with self.assertRaisesRegex(acceptance.AcceptanceError, "cannot prove host"):
            self.record(run_id, "accept-side-effect-safety", path, "op-result-false-host")

    def test_browser_mechanism_cannot_claim_external_service_scope(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["environment"]["enforcement_scope"].append(
            {
                "scope": "external-service",
                "mechanism": "Playwright browser route abort",
                "evidence_ref": "policy:false-external-service",
            }
        )
        candidate["criteria"][2]["required_enforcement_scopes"].append(
            "external-service"
        )
        self.contract = candidate
        write_json(self.contract_path, candidate)
        run_id = self.init_run()
        started = self.start_run(run_id)
        _, path = self.evidence(
            "accept-side-effect-safety",
            1,
            started["binding_sha256"],
            "evidence-false-external-service",
        )
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "cannot prove external-service"
        ):
            self.record(
                run_id,
                "accept-side-effect-safety",
                path,
                "op-result-false-external-service",
            )

    def test_repair_requires_changed_binding(self) -> None:
        run_id = self.init_run()
        started = self.start_run(run_id)
        _, path = self.evidence(
            "accept-business-formula", 1, started["binding_sha256"], "evidence-unchanged-fail", "FAIL"
        )
        self.record(run_id, "accept-business-formula", path, "op-result-unchanged-fail")
        binding_path = self.input_root / "same-binding.json"
        write_json(binding_path, self.binding())
        repair_root = self.input_root / "repair-same"
        repair_artifact = artifact(repair_root / "repair.txt", "attempted")
        resolution = {
            "schema_version": 1,
            "reason": "Attempted repair without changing the binding.",
            "resolved_evidence_ids": ["evidence-unchanged-fail"],
            "changed_paths": ["src/formula.py"],
            "artifacts": [repair_artifact],
        }
        resolution_path = repair_root / "resolution.json"
        write_json(resolution_path, resolution)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "changed execution binding"):
            acceptance.command_repair(
                argparse.Namespace(
                    run_id=run_id,
                    operation_id="op-repair-unchanged",
                    binding=str(binding_path),
                    resolution=str(resolution_path),
                    state_root=str(self.state_root),
                )
            )

    def test_final_not_accepted_after_generation_budget(self) -> None:
        self.contract["max_generations"] = 1
        write_json(self.contract_path, self.contract)
        run_id = self.init_run()
        started = self.start_run(run_id)
        _, path = self.evidence(
            "accept-business-formula", 1, started["binding_sha256"], "evidence-budget-fail", "FAIL"
        )
        self.record(run_id, "accept-business-formula", path, "op-result-budget-fail")
        closed = acceptance.command_close(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-close-budget",
                stop_reason=None,
                output=None,
                state_root=str(self.state_root),
            )
        )
        self.assertTrue(closed["final"])
        self.assertEqual(closed["status"], "NOT_ACCEPTED")
        with self.assertRaisesRegex(acceptance.AcceptanceError, "requires an ACCEPTED"):
            acceptance.verify_receipt_document(Path(closed["receipt_path"]), require_accepted=True)

    def test_receipt_tamper_is_rejected(self) -> None:
        run_id = self.init_run()
        started = self.start_run(run_id)
        for index, criterion_id in enumerate(
            ["accept-business-formula", "accept-user-journey", "accept-side-effect-safety"], 1
        ):
            _, path = self.evidence(
                criterion_id, 1, started["binding_sha256"], f"evidence-tamper-{index}"
            )
            self.record(run_id, criterion_id, path, f"op-result-tamper-{index}")
        closed = acceptance.command_close(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-close-tamper",
                stop_reason=None,
                output=None,
                state_root=str(self.state_root),
            )
        )
        receipt_path = Path(closed["receipt_path"])
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["stop_reason"] = "tampered"
        write_json(receipt_path, receipt)
        with self.assertRaisesRegex(acceptance.AcceptanceError, "hash mismatch"):
            acceptance.verify_receipt_document(receipt_path)

    def test_recover_reports_consistent_state(self) -> None:
        run_id = self.init_run()
        (self.state_root / run_id / ".lock").write_text("pid=stale\n", encoding="ascii")
        result = acceptance.command_recover(
            argparse.Namespace(run_id=run_id, state_root=str(self.state_root))
        )
        self.assertEqual(result["status"], "CONSISTENT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
