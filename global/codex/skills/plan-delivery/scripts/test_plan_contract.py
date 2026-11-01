from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("plan_contract.py")
SPEC = importlib.util.spec_from_file_location("plan_contract", MODULE_PATH)
assert SPEC and SPEC.loader
plan_contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plan_contract)

AGENT_GRAPH_PATH = (
    Path(__file__).resolve().parents[2]
    / "agent-graph"
    / "scripts"
    / "agent_graph.py"
)
GRAPH_SPEC = importlib.util.spec_from_file_location("agent_graph_bound", AGENT_GRAPH_PATH)
assert GRAPH_SPEC and GRAPH_SPEC.loader
agent_graph = importlib.util.module_from_spec(GRAPH_SPEC)
GRAPH_SPEC.loader.exec_module(agent_graph)


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def criterion(identifier: str, kind: str = "deterministic-check") -> dict:
    return {
        "criterion_id": identifier,
        "description": f"Evidence for {identifier} proves the declared requirement.",
        "evidence_kind": kind,
    }


def task(
    task_id: str,
    node_id: str,
    title: str,
    capability: str,
    executor: str,
    reviewer: str,
    requirement_refs: list[str],
    depends_on: list[str],
    evidence_from: list[str],
    write_scope: list[str],
    criterion_id: str,
    *,
    kind: str = "work",
    workspace_mode: str = "read-only",
    evidence_kind: str = "deterministic-check",
    wave: int = 1,
) -> dict:
    result = {
        "task_id": task_id,
        "graph_node_id": node_id,
        "title": title,
        "kind": kind,
        "requirement_refs": requirement_refs,
        "depends_on": depends_on,
        "evidence_from": evidence_from,
        "wave": wave,
        "capability": capability,
        "executor": executor,
        "reviewer": reviewer,
        "workspace_mode": workspace_mode,
        "read_scope": ["src/"],
        "write_scope": write_scope,
        "resource_locks": ["repo:source"] if write_scope else [],
        "acceptance_criteria": [criterion(criterion_id, evidence_kind)],
        "budget": {
            "max_attempts": 3,
            "max_wall_seconds": 3600,
            "max_tokens": 20000,
            "max_tool_calls": 100,
        },
        "repair_policy": {
            "on_reject": "amend-graph",
            "max_same_scope_repairs": 0,
            "max_amendments": 2,
        },
        "loop_policy": {
            "enabled": False,
            "max_rounds": 0,
            "stop_condition": "Acceptance criterion passes or the task blocks.",
        },
        "council_trigger": {
            "required": False,
            "reason": "No unresolved architectural conflict in this fixture.",
        },
        "status": "pending",
    }
    if executor == "fleet-run" and write_scope:
        result["maker_lineage"] = "provider-maker"
        result["reviewer_lineage"] = "provider-reviewer"
    return result


class PlanContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "src").mkdir(parents=True)
        self.prompt = self.root / "intake.md"
        self.prompt.write_text(
            "Implement the bounded feature. Never deploy it. Require quality and security acceptance.\n",
            encoding="utf-8",
        )
        self.fidelity_evidence = self.root / "fidelity.md"
        self.fidelity_evidence.write_text("PASS: every material prompt clause is represented.\n", encoding="utf-8")
        self.sufficiency_evidence = self.root / "sufficiency.md"
        self.sufficiency_evidence.write_text("PASS: the TODO is decision-complete and executable.\n", encoding="utf-8")
        self.draft = self.make_contract()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_contract(self) -> dict:
        requirements = [
            {
                "requirement_id": "REQ-001",
                "kind": "goal",
                "text": "Implement the bounded feature.",
                "source_ref": "intake.md:1",
                "clause_refs": ["CLAUSE-001"],
                "material": True,
                "task_refs": ["TASK-001", "TASK-002"],
                "status": "active",
            },
            {
                "requirement_id": "REQ-002",
                "kind": "negative",
                "text": "Never deploy the feature from this task.",
                "source_ref": "intake.md:1",
                "clause_refs": ["CLAUSE-002"],
                "material": True,
                "task_refs": ["TASK-005"],
                "status": "active",
            },
            {
                "requirement_id": "REQ-003",
                "kind": "acceptance",
                "text": "Independent quality and security gates must accept the integrated result.",
                "source_ref": "intake.md:1",
                "clause_refs": ["CLAUSE-003"],
                "material": True,
                "task_refs": ["TASK-003", "TASK-004", "TASK-005"],
                "status": "active",
            },
        ]
        clauses = [
            {
                "clause_id": "CLAUSE-001",
                "kind": "explicit",
                "text": "Implement the bounded feature.",
                "source_ref": "intake.md:1",
                "material": True,
                "requirement_refs": ["REQ-001"],
            },
            {
                "clause_id": "CLAUSE-002",
                "kind": "negative",
                "text": "Never deploy it.",
                "source_ref": "intake.md:1",
                "material": True,
                "requirement_refs": ["REQ-002"],
            },
            {
                "clause_id": "CLAUSE-003",
                "kind": "acceptance",
                "text": "Require quality and security acceptance.",
                "source_ref": "intake.md:1",
                "material": True,
                "requirement_refs": ["REQ-003"],
            },
        ]
        tasks = [
            task(
                "TASK-001",
                "node-implement",
                "Implement bounded feature",
                "implementation",
                "fleet-run",
                "quality-auditor",
                ["REQ-001"],
                [],
                [],
                ["src/feature/"],
                "crit-implementation",
                workspace_mode="detached-worktree",
                evidence_kind="artifact",
                wave=1,
            ),
            task(
                "TASK-002",
                "node-integrate",
                "Integrate accepted feature",
                "integration",
                "parent-local",
                "integration-reviewer",
                ["REQ-001"],
                ["TASK-001"],
                ["TASK-001"],
                ["src/"],
                "crit-integration",
                workspace_mode="integration-owner",
                evidence_kind="artifact",
                wave=2,
            ),
            task(
                "TASK-003",
                "node-quality",
                "Run independent quality gate",
                "independent-quality",
                "fleet-quality-gate",
                "parent-architect",
                ["REQ-003"],
                ["TASK-002"],
                ["TASK-001", "TASK-002"],
                [],
                "crit-quality",
                kind="gate",
                evidence_kind="quality-gate",
                wave=3,
            ),
            task(
                "TASK-004",
                "node-security",
                "Run incremental security gate",
                "incremental-security",
                "fleet-security-gate",
                "parent-architect",
                ["REQ-003"],
                ["TASK-002"],
                ["TASK-001", "TASK-002"],
                [],
                "crit-security",
                kind="gate",
                evidence_kind="security-gate",
                wave=3,
            ),
            task(
                "TASK-005",
                "node-final",
                "Perform final acceptance without deployment",
                "final-acceptance",
                "parent-local",
                "owner-review",
                ["REQ-002", "REQ-003"],
                ["TASK-003", "TASK-004"],
                [],
                [],
                "crit-final",
                kind="gate",
                evidence_kind="parent-acceptance",
                wave=4,
            ),
        ]
        contract = {
            "schema_version": 1,
            "contract_id": "plan-test-delivery",
            "revision": 1,
            "previous_contract_hash": None,
            "status": "draft",
            "prompt": {
                "source": "intake.md",
                "summary": "Implement and independently accept a bounded feature without deployment.",
                "sha256": plan_contract.prompt_hash(self.prompt),
                "context_sources": [],
            },
            "classification": {
                "tier": "full",
                "hard_risk_flags": [],
                "data_classification": "internal",
                "production_boundary": "Local implementation only; deployment is forbidden.",
            },
            "clauses": clauses,
            "requirements": requirements,
            "questions": [],
            "assumptions": [],
            "out_of_scope": ["Production deployment"],
            "decisions": [],
            "tasks": tasks,
            "gates": {
                "fidelity": {
                    "round": 0,
                    "status": "pending",
                    "auditor": None,
                    "reviewed_prompt_sha256": None,
                    "reviewed_plan_sha256": None,
                    "reviewed_clause_ids": [],
                    "reviewed_requirement_ids": [],
                    "reviewed_task_ids": [],
                    "attested_hard_risk_flags": [],
                    "evidence": None,
                },
                "sufficiency": {
                    "round": 0,
                    "status": "pending",
                    "auditor": None,
                    "reviewed_prompt_sha256": None,
                    "reviewed_plan_sha256": None,
                    "reviewed_clause_ids": [],
                    "reviewed_requirement_ids": [],
                    "reviewed_task_ids": [],
                    "attested_hard_risk_flags": [],
                    "evidence": None,
                },
            },
            "planning": {
                "repair_round": 0,
                "max_repair_rounds": 2,
                "planner_identity": "planner-main",
                "planner_lineage": "openai",
                "planner_session_id": "session-planner",
                "review_history": [],
            },
            "approval": {
                "approved_by": None,
                "approved_at": None,
                "contract_hash": None,
            },
            "owner_ceiling": plan_contract.budget_pools.example_owner_ceiling(
                planning_and_review=50000,
                execution=200000,
            ),
            "graph": {
                "graph_id": "graph-test-delivery",
                "project_root": str(self.project.resolve()),
                "integration_task_id": "TASK-002",
                "global_budget": {
                    "token_policy": "estimate",
                    "max_total_attempts": 30,
                    "max_total_tokens": 200000,
                    "max_wall_seconds": 7200,
                    "max_amendments": 3,
                },
                "previous_projection_sha256": None,
                "projection_sha256": None,
            },
            "amendment": None,
            "closeout": {"status": "pending", "receipt_sha256": None},
        }
        reviewed_plan = plan_contract.planning_payload_hash(contract)
        contract["gates"]["fidelity"] = {
            "round": 0,
            "status": "pass",
            "auditor": {
                "identity": "fidelity-auditor",
                "lineage": "anthropic",
                "session_id": "session-fidelity",
                "independent_from": ["planner-main"],
            },
            "reviewed_prompt_sha256": contract["prompt"]["sha256"],
            "reviewed_plan_sha256": reviewed_plan,
            "reviewed_clause_ids": [item["clause_id"] for item in clauses],
            "reviewed_requirement_ids": [item["requirement_id"] for item in requirements],
            "reviewed_task_ids": [],
            "attested_hard_risk_flags": contract["classification"]["hard_risk_flags"],
            "evidence": {
                "path": "fidelity.md",
                "sha256": plan_contract.file_hash(self.fidelity_evidence),
            },
        }
        contract["gates"]["sufficiency"] = {
            "round": 0,
            "status": "pass",
            "auditor": {
                "identity": "sufficiency-auditor",
                "lineage": "google",
                "session_id": "session-sufficiency",
                "independent_from": ["planner-main"],
            },
            "reviewed_prompt_sha256": contract["prompt"]["sha256"],
            "reviewed_plan_sha256": reviewed_plan,
            "reviewed_clause_ids": [item["clause_id"] for item in clauses],
            "reviewed_requirement_ids": [item["requirement_id"] for item in requirements],
            "reviewed_task_ids": [item["task_id"] for item in tasks],
            "attested_hard_risk_flags": [],
            "evidence": {
                "path": "sufficiency.md",
                "sha256": plan_contract.file_hash(self.sufficiency_evidence),
            },
        }
        contract["planning"]["review_history"] = [
            {
                "revision": contract["revision"],
                "gate": "fidelity",
                **copy.deepcopy(contract["gates"]["fidelity"]),
            },
            {
                "revision": contract["revision"],
                "gate": "sufficiency",
                **copy.deepcopy(contract["gates"]["sufficiency"]),
            },
        ]
        return contract

    def approve(
        self,
        contract: dict | None = None,
        filename: str = "approved.json",
        base_contract: Path | None = None,
    ) -> tuple[dict, Path]:
        draft_path = self.root / f"draft-{filename}"
        approved_path = self.root / filename
        write_json(draft_path, contract or self.draft)
        plan_contract.command_approve(
            argparse.Namespace(
                contract=str(draft_path),
                output=str(approved_path),
                approved_by="parent-architect",
                base_contract=str(base_contract) if base_contract else None,
            )
        )
        return plan_contract.load_json(approved_path, "approved"), approved_path

    def make_acceptance_contract(
        self,
        approved: dict,
        graph_value: dict,
        filename: str = "acceptance.json",
    ) -> Path:
        requirement_ids = [item["requirement_id"] for item in approved["requirements"]]
        independence = {
            "not_same_actor": True,
            "not_same_lineage": True,
            "allowed_roles": ["independent-reviewer"],
        }
        common = {
            "requirement_refs": requirement_ids,
            "mandatory": True,
            "applicability": "applicable",
            "max_age_seconds": 3600,
            "reviewer_independence": independence,
            "n_a_authority": None,
        }
        contract = {
            "schema_version": 1,
            "acceptance_id": "accept-plan-contract-tests",
            "project_slug": "plan-contract-tests",
            "status": "READY",
            "planning_contract": {
                "contract_id": approved["contract_id"],
                "contract_hash": approved["approval"]["contract_hash"],
            },
            "graph": {
                "graph_id": graph_value["graph_id"],
                "graph_hash": plan_contract.sha256_value(graph_value),
            },
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
                        "mechanism": "Fixture browser routing blocks external requests",
                        "evidence_ref": "fixture:browser-policy",
                    },
                    {
                        "scope": "application",
                        "mechanism": "Fixture adapters reject real destinations",
                        "evidence_ref": "fixture:application-policy",
                    },
                ],
                "owner_approval": None,
            },
            "maker": {"id": "planner-test-maker", "lineage": "openai", "role": "implementer"},
            "max_generations": 2,
            "criteria": [
                {
                    **copy.deepcopy(common),
                    "criterion_id": "accept-planning-business",
                    "title": "Planning business requirements are satisfied",
                    "domain": "business",
                    "oracle_kind": "business-oracle",
                    "oracle": {
                        "expected": "all material requirements have accepted implementation evidence",
                        "method": "compare implementation evidence with the approved requirement ledger",
                        "source_ref": "planning-contract:requirements",
                    },
                    "evidence_kinds": ["deterministic-check"],
                    "required_enforcement_scopes": [],
                },
                {
                    **copy.deepcopy(common),
                    "criterion_id": "accept-planning-journey",
                    "title": "The synthetic user journey completes",
                    "domain": "ui-functional",
                    "oracle_kind": "user-journey-evidence",
                    "oracle": {
                        "expected": "the synthetic user reaches final confirmation",
                        "method": "run a bounded browser journey against the fixture",
                        "source_ref": "planning-contract:user-journey",
                    },
                    "evidence_kinds": ["browser-e2e"],
                    "required_enforcement_scopes": [],
                },
                {
                    **copy.deepcopy(common),
                    "criterion_id": "accept-planning-side-effects",
                    "title": "DEV external effects stay blocked",
                    "domain": "side-effect",
                    "oracle_kind": "side-effect-safety",
                    "oracle": {
                        "expected": "no real external destination is reachable",
                        "method": "verify browser and application enforcement evidence",
                        "source_ref": "global-policy:dev-side-effects",
                    },
                    "evidence_kinds": ["deterministic-check"],
                    "required_enforcement_scopes": ["browser-context", "application"],
                },
            ],
        }
        all_domains = {
            "business",
            "backend",
            "data",
            "integration",
            "ui-functional",
            "ux",
            "responsive",
            "accessibility",
            "performance",
            "reliability",
            "security",
            "side-effect",
        }
        present_domains = {item["domain"] for item in contract["criteria"]}
        user_domains = {"ui-functional", "ux", "responsive", "accessibility"}
        for domain in sorted(all_domains - present_domains):
            user_facing = domain in user_domains
            contract["criteria"].append(
                {
                    "criterion_id": f"accept-na-{domain}",
                    "title": f"{domain} is explicitly not applicable",
                    "requirement_refs": [requirement_ids[0]],
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
                        "actor_id": "planner-test-discovery",
                        "actor_role": "discovery-gate",
                        "evidence_ref": f"discovery:fixture-{domain}",
                    },
                    "required_enforcement_scopes": [],
                }
            )
        path = self.root / filename
        write_json(path, contract)
        return path

    def test_full_contract_approves_compiles_and_initializes_graph(self) -> None:
        approved, approved_path = self.approve()
        graph_path = self.root / "graph.json"
        plan_contract.command_compile_graph(
            argparse.Namespace(contract=str(approved_path), output=str(graph_path))
        )
        graph_value = plan_contract.load_json(graph_path, "graph")
        self.assertEqual(
            plan_contract.sha256_value(graph_value),
            approved["graph"]["projection_sha256"],
        )
        acceptance_path = self.make_acceptance_contract(approved, graph_value)
        result = agent_graph.command_init(
            argparse.Namespace(
                graph=str(graph_path),
                planning_contract=str(approved_path),
                acceptance_contract=str(acceptance_path),
                state_root=str(self.root / "state"),
            )
        )
        self.assertEqual(result["planning_contract"]["contract_hash"], approved["approval"]["contract_hash"])
        self.assertEqual(result["acceptance_contract"]["acceptance_id"], "accept-plan-contract-tests")
        snapshot = Path(result["run_dir"]) / result["planning_contract"]["snapshot_path"]
        self.assertTrue(snapshot.is_file())

    def test_new_full_combined_policy_accepts_two_pass_projections_and_real_witness(self) -> None:
        contract = copy.deepcopy(self.draft)
        contract["planning"]["review_policy"] = "full-combined-v1"
        contract["planning"]["planner_lineage"] = "openai"
        contract["planning"]["planner_session_id"] = "planner-session"
        for gate in contract["gates"].values():
            gate.update({"status": "pending", "auditor": None, "reviewed_prompt_sha256": None,
                         "reviewed_plan_sha256": None, "reviewed_clause_ids": [],
                         "reviewed_requirement_ids": [], "reviewed_task_ids": [],
                         "attested_hard_risk_flags": [], "evidence": None})
        contract["planning"]["review_history"] = []
        reviewed_plan = plan_contract.planning_payload_hash(contract)
        combined_receipt = self.root / "full-combined-receipt.json"
        combined_receipt.write_text(json.dumps({
            "run_status": "COMPLETED",
            "agent": "opencode", "adapter": "opencode",
            "model": "glm-5.3-flash",
            "run_id": "combined-review-session",
            "execution_profile_id": "opencode-glm-5-3-flash-max",
            "model_identity": {"status": "verified",
                "expected_runtime_model": "zai-coding-plan/glm-5.3-flash",
                "observed_runtime_models": ["zai-coding-plan/glm-5.3-flash"]},
            "exit_code": 0, "timed_out": False,
            "handoff_valid": True, "handoff_status": "completed",
            "runtime_attestation": {"status": "VERIFIED"},
            "handoff": {"status": "completed", "files_changed": [],
                "verification": ["Complete synthetic test intake and task coverage reviewed"],
                "risks": [], "next_steps": [],
                "summary": "FIDELITY PASS\nSUFFICIENCY PASS\nFULL_COMBINED_REVIEW_PASS "
                    + contract["prompt"]["sha256"] + " " + reviewed_plan},
        }), encoding="utf-8")
        def projection(task_ids, flags):
            return {
                "round": 0, "status": "pass",
                "auditor": {"identity": "opencode", "model": "glm-5.3-flash", "lineage": "zai",
                             "session_id": "combined-review-session", "independent_from": ["planner-main"]},
                "reviewed_prompt_sha256": contract["prompt"]["sha256"],
                "reviewed_plan_sha256": reviewed_plan,
                "reviewed_clause_ids": [item["clause_id"] for item in contract["clauses"]],
                "reviewed_requirement_ids": [item["requirement_id"] for item in contract["requirements"]],
                "reviewed_task_ids": task_ids,
                "attested_hard_risk_flags": flags,
                "evidence": {"path": combined_receipt.name, "sha256": plan_contract.file_hash(combined_receipt)},
            }
        review = {"gate": "combined", "result": {
            "fidelity": projection([], []),
            "sufficiency": projection([item["task_id"] for item in contract["tasks"]], []),
        }}
        draft_path = self.root / "newpolicy-draft.json"
        reviewed_path = self.root / "newpolicy-reviewed.json"
        write_json(draft_path, contract)
        review_path = self.root / "newpolicy-review.json"
        write_json(review_path, review)
        plan_contract.command_record_review(argparse.Namespace(contract=str(draft_path), review=str(review_path), output=str(reviewed_path)))
        reviewed = plan_contract.load_json(reviewed_path, "reviewed")
        witness_source = self.root / "planner-receipt.json"
        witness_source.write_text(json.dumps({
            "model": "gpt-6-astra", "run_id": "planner-session", "run_status": "COMPLETED",
            "execution_profile_id": "codex-gpt-6-astra",
            "provider_attempts": [{"command": ["codex.cmd", "exec", "--model", "gpt-6-astra"]}],
            "runtime_attestation": {"status": "VERIFIED"},
            "handoff": {"summary": "PLAN_REVIEWED " + contract["prompt"]["sha256"] + " " + plan_contract.planning_payload_hash(reviewed)},
            "adapter": "codex",
        }), encoding="utf-8")
        witness_path = self.root / "planner-witness.json"
        witness_path.write_text(json.dumps({"kind": "fleet", "source": str(witness_source),
                                             "source_sha256": plan_contract.file_hash(witness_source)}), encoding="utf-8")
        approved_path = self.root / "newpolicy-approved.json"
        plan_contract.command_approve(argparse.Namespace(contract=str(reviewed_path), output=str(approved_path),
            approved_by="parent-architect", base_contract=None, planner_evidence=str(witness_path),
            require_planner_witness=True))
        self.assertEqual(plan_contract.load_json(approved_path, "approved")["planning"]["review_policy"], "full-combined-v1")

    def test_legacy_full_combined_is_rejected_but_legacy_two_lineages_remain_compatible(self) -> None:
        contract = copy.deepcopy(self.draft)
        review = {"gate": "combined", "result": copy.deepcopy(contract["gates"]["fidelity"])}
        review["result"]["reviewed_task_ids"] = [item["task_id"] for item in contract["tasks"]]
        review_path = self.root / "legacy-combined-review.json"
        draft_path = self.root / "legacy-combined-draft.json"
        write_json(draft_path, contract)
        write_json(review_path, review)
        with self.assertRaisesRegex(plan_contract.ContractError, "full-combined-v1"):
            plan_contract.command_record_review(argparse.Namespace(contract=str(draft_path), review=str(review_path), output=str(self.root / "x.json")))
        self.assertEqual(plan_contract.verify_gate_approval(contract), None)

    def test_fidelity_repair_blocks_approval_for_lost_negative(self) -> None:
        broken = copy.deepcopy(self.draft)
        broken["requirements"] = [item for item in broken["requirements"] if item["kind"] != "negative"]
        broken["tasks"][-1]["requirement_refs"] = ["REQ-003"]
        with self.assertRaisesRegex(plan_contract.ContractError, "missing requirements"):
            plan_contract.validate_contract(broken, "draft")

    def test_hard_risk_cannot_use_compact_tier(self) -> None:
        broken = copy.deepcopy(self.draft)
        broken["classification"]["tier"] = "compact"
        broken["classification"]["hard_risk_flags"] = ["instruction-conflict"]
        with self.assertRaisesRegex(plan_contract.ContractError, "requires tier=full"):
            plan_contract.validate_contract(broken, "draft")

    def test_pending_fidelity_allows_declared_hard_risks_before_review(self) -> None:
        pending = copy.deepcopy(self.draft)
        pending["classification"]["hard_risk_flags"] = ["production"]
        pending["planning"]["review_history"] = []
        for gate in pending["gates"].values():
            gate.update(
                {
                    "status": "pending",
                    "auditor": None,
                    "reviewed_prompt_sha256": None,
                    "reviewed_plan_sha256": None,
                    "reviewed_clause_ids": [],
                    "reviewed_requirement_ids": [],
                    "reviewed_task_ids": [],
                    "attested_hard_risk_flags": [],
                    "evidence": None,
                }
            )

        result = plan_contract.validate_contract(pending, "draft")

        self.assertEqual(result["status"], "draft")

    def test_fidelity_hard_risk_attestation_must_match_classification(self) -> None:
        broken = copy.deepcopy(self.draft)
        broken["gates"]["fidelity"]["attested_hard_risk_flags"] = ["secrets"]
        broken["planning"]["review_history"][0]["attested_hard_risk_flags"] = [
            "secrets"
        ]
        with self.assertRaisesRegex(plan_contract.ContractError, "hard-risk attestation"):
            plan_contract.validate_contract(broken, "draft")

    def test_prompt_hard_risk_scanner_blocks_undeclared_compact_contract(self) -> None:
        self.prompt.write_text(
            "Quick fix in production using an API secret.\n",
            encoding="utf-8",
        )
        broken = copy.deepcopy(self.draft)
        broken["prompt"]["summary"] = "Quick production fix using an API secret."
        broken["prompt"]["sha256"] = plan_contract.prompt_hash(self.prompt)
        broken["classification"]["tier"] = "compact"
        broken["classification"]["hard_risk_flags"] = []
        reviewed_plan = plan_contract.planning_payload_hash(broken)
        for gate in broken["gates"].values():
            gate["reviewed_prompt_sha256"] = broken["prompt"]["sha256"]
            gate["reviewed_plan_sha256"] = reviewed_plan
            gate["attested_hard_risk_flags"] = []
        for record in broken["planning"]["review_history"]:
            record["reviewed_prompt_sha256"] = broken["prompt"]["sha256"]
            record["reviewed_plan_sha256"] = reviewed_plan
            record["attested_hard_risk_flags"] = []
        draft_path = self.root / "undeclared-hard-risk.json"
        write_json(draft_path, broken)
        with self.assertRaisesRegex(
            plan_contract.ContractError,
            "undeclared hard-risk flags: production, secrets",
        ):
            plan_contract.command_approve(
                argparse.Namespace(
                    contract=str(draft_path),
                    output=str(self.root / "must-not-approve-hard-risk.json"),
                    approved_by="parent-architect",
                    base_contract=None,
                )
            )

    def test_low_risk_compact_contract_is_allowed(self) -> None:
        compact = copy.deepcopy(self.draft)
        compact["classification"]["tier"] = "compact"
        reviewed_plan = plan_contract.planning_payload_hash(compact)
        compact["gates"]["fidelity"]["reviewed_plan_sha256"] = reviewed_plan
        compact["gates"]["sufficiency"]["reviewed_plan_sha256"] = reviewed_plan
        for record in compact["planning"]["review_history"]:
            record["reviewed_plan_sha256"] = reviewed_plan
        approved, _ = self.approve(compact, "compact-approved.json")
        self.assertEqual(approved["classification"]["tier"], "compact")

    def test_repair_requires_monotonic_new_round_and_preserves_history(self) -> None:
        base = copy.deepcopy(self.draft)
        base["gates"]["fidelity"]["status"] = "repair"
        base["planning"]["review_history"][0]["status"] = "repair"
        base_path = self.root / "repair-base.json"
        write_json(base_path, base)
        plan_contract.validate_contract(base, "draft")

        same_round = copy.deepcopy(base)
        same_round["gates"]["fidelity"]["status"] = "pass"
        same_round["planning"]["review_history"].append(
            {
                "revision": same_round["revision"],
                "gate": "fidelity",
                **copy.deepcopy(same_round["gates"]["fidelity"]),
            }
        )
        with self.assertRaisesRegex(plan_contract.ContractError, "Duplicate review history"):
            plan_contract.validate_contract(same_round, "draft")

        candidate = copy.deepcopy(base)
        candidate["planning"]["repair_round"] = 1
        for gate in candidate["gates"].values():
            gate.update(
                {
                    "round": 1,
                    "status": "pending",
                    "auditor": None,
                    "reviewed_prompt_sha256": None,
                    "reviewed_plan_sha256": None,
                    "reviewed_clause_ids": [],
                    "reviewed_requirement_ids": [],
                    "reviewed_task_ids": [],
                    "evidence": None,
                }
            )
        candidate["tasks"][0]["title"] = "Implement bounded feature after fidelity repair"
        candidate["prompt"]["context_sources"].append(
            {
                "label": "late-audit-context",
                "kind": "url",
                "path": "https://example.invalid/pinned-source",
                "sha256": "a" * 64,
            }
        )
        candidate_path = self.root / "repair-candidate.json"
        output_path = self.root / "repair-round-1.json"
        write_json(candidate_path, candidate)
        result = plan_contract.command_advance_repair(
            argparse.Namespace(
                contract=str(base_path),
                candidate=str(candidate_path),
                output=str(output_path),
            )
        )
        self.assertEqual(result["to_round"], 1)
        repaired = plan_contract.load_json(output_path, "repaired")
        self.assertEqual(repaired["planning"]["review_history"], base["planning"]["review_history"])
        self.assertEqual(
            repaired["prompt"]["context_sources"],
            candidate["prompt"]["context_sources"],
        )

        rewritten = copy.deepcopy(candidate)
        rewritten["prompt"]["summary"] = "Rewrite the owner prompt"
        rewritten_path = self.root / "repair-rewritten-prompt.json"
        write_json(rewritten_path, rewritten)
        with self.assertRaisesRegex(
            plan_contract.ContractError,
            "cannot rewrite the bound prompt",
        ):
            plan_contract.command_advance_repair(
                argparse.Namespace(
                    contract=str(base_path),
                    candidate=str(rewritten_path),
                    output=str(self.root / "must-not-rewrite-prompt.json"),
                )
            )

    def test_graph_projection_keeps_reviewer_locks_loop_and_council_policy(self) -> None:
        approved, _ = self.approve()
        approved["graph"]["global_budget"]["token_policy"] = "estimate"
        approved["graph"]["global_budget"]["time_policy"] = "estimate"
        approved["tasks"][0]["budget"]["tool_policy"] = "estimate"
        projected = plan_contract.compile_graph_value(approved)
        node_value = projected["nodes"][0]
        self.assertEqual(node_value["reviewer"], "quality-auditor")
        self.assertEqual(node_value["resource_locks"], ["repo:source"])
        self.assertIn("loop_policy", node_value)
        self.assertIn("council_trigger", node_value)
        self.assertEqual(projected["global_budget"]["time_policy"], "estimate")
        self.assertEqual(node_value["budget"]["tool_policy"], "estimate")
        self.assertEqual(agent_graph.validate_graph(projected)["graph_id"], approved["graph"]["graph_id"])

    def test_new_draft_defaults_are_applied_before_review(self) -> None:
        draft = copy.deepcopy(self.draft)
        draft["planning"]["review_history"] = []
        for gate in draft["gates"].values():
            gate["status"] = "pending"
        approved = plan_contract.normalize_new_contract_defaults(draft)
        self.assertEqual(approved["graph"]["global_budget"]["token_policy"], "estimate")
        self.assertEqual(approved["graph"]["global_budget"]["time_policy"], "estimate")
        self.assertEqual(approved["owner_ceiling"]["token_policy"], "estimate")
        self.assertTrue(
            all(
                task["budget"]["token_policy"] == "estimate"
                and task["budget"]["tool_policy"] == "estimate"
                for task in approved["tasks"]
            )
        )
        self.assertEqual(approved["gates"], draft["gates"])
        self.assertEqual(approved["planning"]["review_history"], [])

    def test_defaults_cannot_rebind_existing_reviews(self) -> None:
        before = copy.deepcopy(self.draft)
        with self.assertRaisesRegex(plan_contract.ContractError, "unreviewed"):
            plan_contract.normalize_new_contract_defaults(self.draft)
        self.assertEqual(before, self.draft)

    def test_prepare_draft_command_writes_valid_unreviewed_snapshot(self) -> None:
        from test_task011_planning import declare_metadata

        draft = copy.deepcopy(self.draft)
        declare_metadata(draft)
        draft["planning"]["review_history"] = []
        for name in draft["gates"]:
            draft["gates"][name] = {
                "round": 0, "status": "pending", "auditor": None,
                "reviewed_prompt_sha256": None, "reviewed_plan_sha256": None,
                "reviewed_clause_ids": [], "reviewed_requirement_ids": [],
                "reviewed_task_ids": [], "attested_hard_risk_flags": [], "evidence": None,
            }
        source, output = self.root / "unreviewed.json", self.root / "prepared.json"
        write_json(source, draft)
        result = plan_contract.command_prepare_draft(argparse.Namespace(
            contract=str(source), output=str(output)))
        prepared = json.loads(output.read_text())
        plan_contract.validate_contract(prepared, "draft")
        self.assertEqual(result["status"], "DRAFT_PREPARED")
        self.assertEqual(prepared["graph"]["global_budget"]["time_policy"], "estimate")
        self.assertEqual(prepared["planning"]["review_history"], [])
        self.assertEqual(json.loads(source.read_text()), draft)

    def test_approval_preserves_reviewed_policy_and_hash(self) -> None:
        approved, _ = self.approve()
        self.assertEqual(approved["gates"], self.draft["gates"])
        self.assertEqual(approved["tasks"], self.draft["tasks"])
        self.assertEqual(approved["graph"]["global_budget"], self.draft["graph"]["global_budget"])

    def test_missing_reviewer_is_rejected(self) -> None:
        broken = copy.deepcopy(self.draft)
        del broken["tasks"][0]["reviewer"]
        with self.assertRaisesRegex(plan_contract.ContractError, "reviewer"):
            plan_contract.validate_contract(broken, "draft")

    def test_maker_and_reviewer_lineage_collision_is_rejected(self) -> None:
        broken = copy.deepcopy(self.draft)
        implementation = next(
            task for task in broken["tasks"] if task["executor"] == "fleet-run"
        )
        implementation["maker_lineage"] = "same-lineage"
        implementation["reviewer_lineage"] = "same-lineage"
        with self.assertRaisesRegex(
            plan_contract.ContractError, "maker_lineage cannot equal reviewer_lineage"
        ):
            plan_contract.validate_contract(broken, "draft")

    def test_unordered_write_or_resource_lock_conflict_is_rejected(self) -> None:
        broken = copy.deepcopy(self.draft)
        parallel = copy.deepcopy(broken["tasks"][0])
        parallel["task_id"] = "TASK-006"
        parallel["graph_node_id"] = "node-parallel"
        parallel["title"] = "Parallel conflicting implementation"
        parallel["acceptance_criteria"][0]["criterion_id"] = "crit-parallel"
        parallel["requirement_refs"] = ["REQ-001"]
        broken["requirements"][0]["task_refs"].append("TASK-006")
        broken["tasks"].append(parallel)
        with self.assertRaisesRegex(plan_contract.ContractError, "Unsafe unordered tasks"):
            plan_contract.validate_contract(broken, "draft")

    def test_contract_rejects_graph_incompatible_quality_coverage(self) -> None:
        broken = copy.deepcopy(self.draft)
        broken["tasks"][2]["evidence_from"] = ["TASK-002"]
        with self.assertRaisesRegex(
            plan_contract.ContractError,
            "quality gate does not explicitly cover",
        ):
            plan_contract.validate_contract(broken, "draft")

    def test_model_backed_task_can_leave_tokens_unknown(self) -> None:
        broken = copy.deepcopy(self.draft)
        broken["tasks"][0]["budget"]["max_tokens"] = None
        plan_contract.validate_contract(broken, "draft")

    def test_owner_ceiling_is_required_and_bound_to_execution_budget(self) -> None:
        missing = copy.deepcopy(self.draft)
        del missing["owner_ceiling"]
        with self.assertRaisesRegex(plan_contract.ContractError, "owner_ceiling"):
            plan_contract.validate_contract(missing, "draft")
        mismatched = copy.deepcopy(self.draft)
        mismatched["owner_ceiling"]["execution"] = 199999
        with self.assertRaisesRegex(plan_contract.ContractError, "must equal owner_ceiling.execution"):
            plan_contract.validate_contract(mismatched, "draft")

    def test_zero_planning_pool_still_compiles_approved_graph(self) -> None:
        contract = copy.deepcopy(self.draft)
        contract["owner_ceiling"]["planning_and_review"] = 0
        reviewed_plan = plan_contract.planning_payload_hash(contract)
        contract["gates"]["fidelity"]["reviewed_plan_sha256"] = reviewed_plan
        contract["gates"]["sufficiency"]["reviewed_plan_sha256"] = reviewed_plan
        for record in contract["planning"]["review_history"]:
            record["reviewed_plan_sha256"] = reviewed_plan
        approved, approved_path = self.approve(contract, "zero-planning.json")
        graph_value = plan_contract.compile_graph_value(approved)
        self.assertEqual(graph_value["global_budget"]["max_total_tokens"], 200000)
        self.assertEqual(approved["owner_ceiling"]["planning_and_review"], 0)
        self.assertEqual(approved_path.name, "zero-planning.json")

    def test_planner_rejects_graph_illegal_same_scope_repair_budget(self) -> None:
        broken = copy.deepcopy(self.draft)
        broken["tasks"][0]["repair_policy"]["max_same_scope_repairs"] = 6
        with self.assertRaisesRegex(plan_contract.ContractError, "max_same_scope_repairs"):
            plan_contract.validate_contract(broken, "draft")

    def test_persistent_contract_requires_material_requirement(self) -> None:
        broken = copy.deepcopy(self.draft)
        for requirement in broken["requirements"]:
            requirement["material"] = False
        for clause in broken["clauses"]:
            clause["material"] = False
        with self.assertRaisesRegex(plan_contract.ContractError, "material requirement"):
            plan_contract.validate_contract(broken, "draft")

    def test_material_requirement_requires_active_task_coverage(self) -> None:
        broken = copy.deepcopy(self.draft)
        broken["tasks"][0]["status"] = "blocked"
        broken["tasks"][1]["status"] = "blocked"
        with self.assertRaisesRegex(plan_contract.ContractError, "no active task coverage"):
            plan_contract.validate_contract(broken, "draft")

    def test_special_requirement_kinds_keep_reciprocal_coverage(self) -> None:
        for kind in ("output", "reporting", "conditional", "deferred", "owner_decision"):
            with self.subTest(kind=kind):
                value = copy.deepcopy(self.draft)
                value["requirements"][0]["kind"] = kind
                value["clauses"][0]["kind"] = kind
                if kind == "owner_decision":
                    value["decisions"] = [
                        {
                            "decision_id": "DECISION-001",
                            "text": value["clauses"][0]["text"],
                            "authority": "parent",
                            "evidence_ref": "CLAUSE-001",
                        }
                    ]
                reviewed_plan = plan_contract.planning_payload_hash(value)
                for gate in value["gates"].values():
                    gate["reviewed_plan_sha256"] = reviewed_plan
                for record in value["planning"]["review_history"]:
                    record["reviewed_plan_sha256"] = reviewed_plan
                plan_contract.validate_contract(value, "draft")

    def test_owner_decision_requires_exact_owner_clause(self) -> None:
        broken = copy.deepcopy(self.draft)
        broken["decisions"] = [
            {
                "decision_id": "DECISION-001",
                "text": "Parent approved a different decision.",
                "authority": "parent",
                "evidence_ref": "CLAUSE-001",
            }
        ]
        with self.assertRaisesRegex(plan_contract.ContractError, "differs from its clause"):
            plan_contract.validate_contract(broken, "draft")

    def test_gate_evidence_hash_mismatch_blocks_approval(self) -> None:
        broken = copy.deepcopy(self.draft)
        broken["gates"]["fidelity"]["evidence"]["sha256"] = "0" * 64
        broken["planning"]["review_history"][0]["evidence"]["sha256"] = "0" * 64
        draft_path = self.root / "bad-evidence.json"
        write_json(draft_path, broken)
        with self.assertRaisesRegex(plan_contract.ContractError, "Gate evidence hash mismatch"):
            plan_contract.command_approve(
                argparse.Namespace(
                    contract=str(draft_path),
                    output=str(self.root / "must-not-exist.json"),
                    approved_by="parent-architect",
                    base_contract=None,
                )
            )

    def test_immutable_approved_output_survives_repeat_attempt(self) -> None:
        approved, approved_path = self.approve()
        before = approved_path.read_bytes()
        with self.assertRaisesRegex(plan_contract.ContractError, "Refusing to overwrite"):
            plan_contract.command_approve(
                argparse.Namespace(
                    contract=str(self.root / "draft-approved.json"),
                    output=str(approved_path),
                    approved_by="parent-architect",
                    base_contract=None,
                )
            )
        self.assertEqual(before, approved_path.read_bytes())
        self.assertEqual(approved["approval"]["contract_hash"], plan_contract.load_json(approved_path, "approved")["approval"]["contract_hash"])

    def test_validate_approved_rechecks_bound_prompt(self) -> None:
        _, approved_path = self.approve()
        self.prompt.write_text("Prompt changed after immutable approval.\n", encoding="utf-8")
        with self.assertRaisesRegex(plan_contract.ContractError, "prompt.sha256"):
            plan_contract.command_validate(
                argparse.Namespace(contract=str(approved_path), phase="approved")
            )

    def test_graph_mutation_is_detected(self) -> None:
        _, approved_path = self.approve()
        graph_path = self.root / "graph.json"
        plan_contract.command_compile_graph(
            argparse.Namespace(contract=str(approved_path), output=str(graph_path))
        )
        graph_value = plan_contract.load_json(graph_path, "graph")
        graph_value["nodes"][0]["title"] = "Mutated projection"
        write_json(self.root / "mutated.json", graph_value)
        with self.assertRaisesRegex(plan_contract.ContractError, "differs from deterministic"):
            plan_contract.command_verify_graph(
                argparse.Namespace(contract=str(approved_path), graph=str(self.root / "mutated.json"))
            )

    def test_amendment_preserves_baseline_and_previous_projection(self) -> None:
        self.draft["prompt"]["context_sources"] = [
            {
                "label": "baseline-context",
                "kind": "url",
                "path": "https://example.invalid/baseline-source",
                "sha256": "b" * 64,
            }
        ]
        reviewed_plan = plan_contract.planning_payload_hash(self.draft)
        for gate in self.draft["gates"].values():
            gate["reviewed_plan_sha256"] = reviewed_plan
        for record in self.draft["planning"]["review_history"]:
            record["reviewed_plan_sha256"] = reviewed_plan
        approved, approved_path = self.approve()
        candidate = copy.deepcopy(approved)
        candidate["revision"] = 2
        candidate["previous_contract_hash"] = approved["approval"]["contract_hash"]
        candidate["status"] = "draft"
        candidate["approval"] = {
            "approved_by": None,
            "approved_at": None,
            "contract_hash": None,
        }
        candidate["graph"]["previous_projection_sha256"] = approved["graph"]["projection_sha256"]
        candidate["graph"]["projection_sha256"] = None
        candidate["planning"]["planner_session_id"] = "session-planner-revision-2"
        candidate["amendment"] = {
            "reason": "Replace the integration and acceptance tail after evidence.",
            "affected_task_ids": ["TASK-002", "TASK-003", "TASK-004", "TASK-005"],
            "introduced_task_ids": [],
            "authority": "parent",
            "approval_ref": "decision-local-001",
        }
        candidate["graph"]["integration_task_id"] = "TASK-006"
        tasks = {item["task_id"]: item for item in candidate["tasks"]}
        for task_id in candidate["amendment"]["affected_task_ids"]:
            tasks[task_id]["status"] = "blocked"
        replacements = []
        replacement_spec = [
            ("TASK-002", "TASK-006", "node-integrate-repair", ["TASK-001"], ["TASK-001"]),
            ("TASK-003", "TASK-007", "node-quality-repair", ["TASK-006"], ["TASK-001", "TASK-006"]),
            ("TASK-004", "TASK-008", "node-security-repair", ["TASK-006"], ["TASK-001", "TASK-006"]),
            ("TASK-005", "TASK-009", "node-final-repair", ["TASK-007", "TASK-008"], []),
        ]
        for source_id, task_id, node_id, dependencies, evidence_from in replacement_spec:
            replacement = copy.deepcopy(tasks[source_id])
            replacement["task_id"] = task_id
            replacement["graph_node_id"] = node_id
            replacement["title"] = f"Replacement for {tasks[source_id]['title']}"
            replacement["depends_on"] = dependencies
            replacement["evidence_from"] = evidence_from
            replacement["status"] = "pending"
            replacement["acceptance_criteria"][0]["criterion_id"] += f"-r{task_id[-3:]}"
            replacements.append(replacement)
            for requirement_id in replacement["requirement_refs"]:
                requirement = next(
                    item
                    for item in candidate["requirements"]
                    if item["requirement_id"] == requirement_id
                )
                requirement["task_refs"].append(task_id)
        candidate["tasks"].extend(replacements)
        candidate["amendment"]["introduced_task_ids"] = [
            replacement["task_id"] for replacement in replacements
        ]
        candidate["prompt"]["context_sources"].append(
            {
                "label": "amendment-context",
                "kind": "url",
                "path": "https://example.invalid/amendment-source",
                "sha256": "c" * 64,
            }
        )
        tasks["TASK-001"]["acceptance_criteria"].append(
            {
                "criterion_id": "crit-office-additive-002",
                "description": "Add one additive acceptance condition without changing the baseline criteria.",
                "evidence_kind": "deterministic-check",
            }
        )

        stale_reviews = copy.deepcopy(candidate)
        stale_hash = plan_contract.planning_payload_hash(stale_reviews)
        for gate in stale_reviews["gates"].values():
            gate["reviewed_plan_sha256"] = stale_hash
        for record in stale_reviews["planning"]["review_history"]:
            record["reviewed_plan_sha256"] = stale_hash
        with self.assertRaisesRegex(
            plan_contract.ContractError,
            "current-round history|current revision|review history",
        ):
            plan_contract.validate_amendment(approved, stale_reviews)

        pending_gate = {
            "round": 0,
            "status": "pending",
            "auditor": None,
            "reviewed_prompt_sha256": None,
            "reviewed_plan_sha256": None,
            "reviewed_clause_ids": [],
            "reviewed_requirement_ids": [],
            "reviewed_task_ids": [],
            "attested_hard_risk_flags": [],
            "evidence": None,
        }
        candidate["gates"] = {
            "fidelity": copy.deepcopy(pending_gate),
            "sufficiency": copy.deepcopy(pending_gate),
        }
        result = plan_contract.validate_amendment(approved, candidate)
        self.assertEqual(result["candidate_revision"], 2)

        mismatched_introduced = copy.deepcopy(candidate)
        mismatched_introduced["amendment"]["introduced_task_ids"] = ["TASK-006"]
        with self.assertRaisesRegex(
            plan_contract.ContractError,
            "introduced_task_ids must exactly match",
        ):
            plan_contract.validate_amendment(approved, mismatched_introduced)

        removed_baseline = copy.deepcopy(candidate)
        removed_baseline["prompt"]["context_sources"] = candidate["prompt"][
            "context_sources"
        ][1:]
        with self.assertRaisesRegex(
            plan_contract.ContractError,
            "may only append prompt.context_sources",
        ):
            plan_contract.validate_amendment(approved, removed_baseline)

        reordered = copy.deepcopy(candidate)
        reordered["prompt"]["context_sources"] = list(
            reversed(candidate["prompt"]["context_sources"])
        )
        with self.assertRaisesRegex(
            plan_contract.ContractError,
            "may only append prompt.context_sources",
        ):
            plan_contract.validate_amendment(approved, reordered)

        modified_baseline = copy.deepcopy(candidate)
        modified_baseline["prompt"]["context_sources"][0]["sha256"] = "d" * 64
        with self.assertRaisesRegex(
            plan_contract.ContractError,
            "may only append prompt.context_sources",
        ):
            plan_contract.validate_amendment(approved, modified_baseline)

        rewritten_prompt = copy.deepcopy(candidate)
        rewritten_prompt["prompt"]["summary"] = "Rewrite the owner prompt"
        with self.assertRaisesRegex(
            plan_contract.ContractError,
            "cannot rewrite the baseline prompt",
        ):
            plan_contract.validate_amendment(approved, rewritten_prompt)

        removed_task_criterion = copy.deepcopy(candidate)
        task_001 = next(
            task for task in removed_task_criterion["tasks"] if task["task_id"] == "TASK-001"
        )
        del task_001["acceptance_criteria"][0]
        with self.assertRaisesRegex(
            plan_contract.ContractError,
            "may only append acceptance_criteria",
        ):
            plan_contract.validate_amendment(approved, removed_task_criterion)

        reordered_task_criteria = copy.deepcopy(candidate)
        task_001 = next(
            task for task in reordered_task_criteria["tasks"] if task["task_id"] == "TASK-001"
        )
        criteria = task_001["acceptance_criteria"]
        criteria[0], criteria[1] = criteria[1], criteria[0]
        with self.assertRaisesRegex(
            plan_contract.ContractError,
            "may only append acceptance_criteria",
        ):
            plan_contract.validate_amendment(approved, reordered_task_criteria)

        modified_task_criterion = copy.deepcopy(candidate)
        task_001 = next(
            task for task in modified_task_criterion["tasks"] if task["task_id"] == "TASK-001"
        )
        task_001["acceptance_criteria"][0]["description"] = (
            "Rewrite the baseline acceptance condition."
        )
        with self.assertRaisesRegex(
            plan_contract.ContractError,
            "may only append acceptance_criteria",
        ):
            plan_contract.validate_amendment(approved, modified_task_criterion)

        fidelity_r2 = self.root / "fidelity-r2.md"
        sufficiency_r2 = self.root / "sufficiency-r2.md"
        fidelity_r2.write_text("PASS: revision 2 preserves prompt fidelity.\n", encoding="utf-8")
        sufficiency_r2.write_text("PASS: revision 2 TODO is sufficient.\n", encoding="utf-8")
        reviewed_plan = plan_contract.planning_payload_hash(candidate)
        candidate["gates"]["fidelity"] = {
            "round": 0,
            "status": "pass",
            "auditor": {
                "identity": "fidelity-auditor",
                "lineage": "anthropic",
                "session_id": "session-fidelity-r2",
                "independent_from": ["planner-main"],
            },
            "reviewed_prompt_sha256": candidate["prompt"]["sha256"],
            "reviewed_plan_sha256": reviewed_plan,
            "reviewed_clause_ids": [item["clause_id"] for item in candidate["clauses"]],
            "reviewed_requirement_ids": [
                item["requirement_id"] for item in candidate["requirements"]
            ],
            "reviewed_task_ids": [],
            "attested_hard_risk_flags": candidate["classification"]["hard_risk_flags"],
            "evidence": {
                "path": fidelity_r2.name,
                "sha256": plan_contract.file_hash(fidelity_r2),
            },
        }
        candidate["gates"]["sufficiency"] = {
            "round": 0,
            "status": "pass",
            "auditor": {
                "identity": "sufficiency-auditor",
                "lineage": "google",
                "session_id": "session-sufficiency-r2",
                "independent_from": ["planner-main"],
            },
            "reviewed_prompt_sha256": candidate["prompt"]["sha256"],
            "reviewed_plan_sha256": reviewed_plan,
            "reviewed_clause_ids": [item["clause_id"] for item in candidate["clauses"]],
            "reviewed_requirement_ids": [
                item["requirement_id"] for item in candidate["requirements"]
            ],
            "reviewed_task_ids": [item["task_id"] for item in candidate["tasks"]],
            "attested_hard_risk_flags": [],
            "evidence": {
                "path": sufficiency_r2.name,
                "sha256": plan_contract.file_hash(sufficiency_r2),
            },
        }
        candidate["planning"]["review_history"].extend(
            [
                {
                    "revision": 2,
                    "gate": gate_name,
                    **copy.deepcopy(candidate["gates"][gate_name]),
                }
                for gate_name in ("fidelity", "sufficiency")
            ]
        )
        amended, _ = self.approve(
            candidate,
            "amendment-approved.json",
            base_contract=approved_path,
        )
        self.assertEqual(amended["status"], "amended")

        reused_session = copy.deepcopy(candidate)
        reused_session["gates"]["fidelity"]["auditor"]["session_id"] = "session-fidelity"
        reused_session["planning"]["review_history"][-2]["auditor"][
            "session_id"
        ] = "session-fidelity"
        with self.assertRaisesRegex(plan_contract.ContractError, "fresh auditor sessions"):
            plan_contract.validate_amendment(approved, reused_session)

        broken = copy.deepcopy(candidate)
        broken["requirements"][0]["text"] = "Rewrite the baseline requirement"
        with self.assertRaisesRegex(plan_contract.ContractError, "baseline requirement"):
            plan_contract.validate_amendment(approved, broken)
        reclassified = copy.deepcopy(candidate)
        reclassified["classification"]["hard_risk_flags"] = ["production"]
        with self.assertRaisesRegex(
            plan_contract.ContractError, "hard-risk attestation|baseline classification"
        ):
            plan_contract.validate_amendment(approved, reclassified)
        rewritten = copy.deepcopy(candidate)
        rewritten["tasks"][1]["title"] = "Rewrite deactivated task in place"
        with self.assertRaisesRegex(plan_contract.ContractError, "without rewriting"):
            plan_contract.validate_amendment(approved, rewritten)

    def test_closeout_requires_every_material_requirement_and_hashed_evidence(self) -> None:
        approved, approved_path = self.approve()
        artifact = self.root / "result.txt"
        artifact.write_text("verified result\n", encoding="utf-8")
        records = []
        tasks = {item["task_id"]: item for item in approved["tasks"]}
        for requirement in approved["requirements"]:
            criterion_id = tasks[requirement["task_refs"][0]]["acceptance_criteria"][0]["criterion_id"]
            records.append(
                {
                    "requirement_id": requirement["requirement_id"],
                    "disposition": "satisfied",
                    "criterion_ids": [criterion_id],
                    "evidence": [
                        {
                            "criterion_id": criterion_id,
                            "path": "result.txt",
                            "sha256": plan_contract.file_hash(artifact),
                        }
                    ],
                    "blocker": None,
                }
            )
        closeout = {
            "schema_version": 1,
            "contract_id": approved["contract_id"],
            "contract_hash": approved["approval"]["contract_hash"],
            "requirements": records[:-1],
        }
        evidence_path = self.root / "closeout.json"
        write_json(evidence_path, closeout)
        with self.assertRaisesRegex(plan_contract.ContractError, "coverage mismatch"):
            plan_contract.command_closeout(
                argparse.Namespace(
                    contract=str(approved_path),
                    evidence=str(evidence_path),
                    output=str(self.root / "receipt.json"),
                )
            )
        closeout["requirements"] = records
        missing_mapping = copy.deepcopy(closeout)
        missing_mapping["requirements"][0]["evidence"][0]["criterion_id"] = (
            missing_mapping["requirements"][1]["criterion_ids"][0]
        )
        missing_mapping_path = self.root / "closeout-missing-criterion-map.json"
        write_json(missing_mapping_path, missing_mapping)
        with self.assertRaisesRegex(plan_contract.ContractError, "cover every cited criterion"):
            plan_contract.command_closeout(
                argparse.Namespace(
                    contract=str(approved_path),
                    evidence=str(missing_mapping_path),
                    output=str(self.root / "must-not-map-receipt.json"),
                )
            )

    def test_repair_budget_exhaustion_persists_blocked_snapshot(self) -> None:
        base = copy.deepcopy(self.draft)
        history = []
        for round_number in range(3):
            fidelity = copy.deepcopy(base["gates"]["fidelity"])
            sufficiency = copy.deepcopy(base["gates"]["sufficiency"])
            fidelity["round"] = round_number
            fidelity["status"] = "repair"
            sufficiency["round"] = round_number
            history.extend(
                [
                    {"revision": 1, "gate": "fidelity", **fidelity},
                    {"revision": 1, "gate": "sufficiency", **sufficiency},
                ]
            )
        base["planning"]["repair_round"] = 2
        base["planning"]["review_history"] = history
        base["gates"]["fidelity"] = {
            key: value
            for key, value in history[-2].items()
            if key not in {"revision", "gate"}
        }
        base["gates"]["sufficiency"] = {
            key: value
            for key, value in history[-1].items()
            if key not in {"revision", "gate"}
        }
        base_path = self.root / "repair-budget-base.json"
        candidate_path = self.root / "repair-budget-candidate.json"
        output_path = self.root / "repair-budget-blocked.json"
        write_json(base_path, base)
        write_json(candidate_path, base)
        result = plan_contract.command_advance_repair(
            argparse.Namespace(
                contract=str(base_path),
                candidate=str(candidate_path),
                output=str(output_path),
            )
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["_exit_code"], 3)
        self.assertEqual(
            plan_contract.load_json(output_path, "blocked")["status"], "blocked"
        )


if __name__ == "__main__":
    unittest.main()
