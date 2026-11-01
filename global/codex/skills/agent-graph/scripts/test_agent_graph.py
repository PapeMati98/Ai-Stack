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
from unittest import mock


MODULE_PATH = Path(__file__).with_name("agent_graph.py")
SPEC = importlib.util.spec_from_file_location("agent_graph", MODULE_PATH)
assert SPEC and SPEC.loader
agent_graph = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent_graph)

PLANNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "plan-delivery"
    / "scripts"
    / "plan_contract.py"
)
PLANNER_SPEC = importlib.util.spec_from_file_location("plan_contract_for_graph_tests", PLANNER_PATH)
assert PLANNER_SPEC and PLANNER_SPEC.loader
plan_contract = importlib.util.module_from_spec(PLANNER_SPEC)
PLANNER_SPEC.loader.exec_module(plan_contract)

ACCEPTANCE_PATH = (
    Path(__file__).resolve().parents[2]
    / "accept-system"
    / "scripts"
    / "acceptance.py"
)
ACCEPTANCE_SPEC = importlib.util.spec_from_file_location(
    "acceptance_for_graph_tests", ACCEPTANCE_PATH
)
assert ACCEPTANCE_SPEC and ACCEPTANCE_SPEC.loader
acceptance = importlib.util.module_from_spec(ACCEPTANCE_SPEC)
ACCEPTANCE_SPEC.loader.exec_module(acceptance)


def node(
    node_id: str,
    kind: str,
    capability: str,
    executor: str,
    depends_on: list[str],
    write_scope: list[str],
    workspace_mode: str,
    evidence_from: list[str] | None = None,
    on_reject: str = "amend-graph",
) -> dict:
    suffix = node_id.removeprefix("node-")
    evidence_kind = {
        "fleet-quality-gate": "quality-gate",
        "fleet-security-gate": "security-gate",
        "human": "human-approval",
    }.get(executor, "artifact" if write_scope else "deterministic-check")
    if capability == "final-acceptance":
        evidence_kind = "parent-acceptance"
    return {
        "node_id": node_id,
        "active": True,
        "kind": kind,
        "capability": capability,
        "title": f"Test node {suffix}",
        "depends_on": depends_on,
        "evidence_from": evidence_from or [],
        "executor": executor,
        "reviewer": "independent-reviewer",
        "workspace_mode": workspace_mode,
        "read_scope": ["frontend/", "backend/"],
        "write_scope": write_scope,
        "resource_locks": [f"repo:{suffix}"] if write_scope else [],
        "acceptance_criteria": [
            {
                "criterion_id": f"crit-{suffix}",
                "description": f"Acceptance evidence for test node {suffix} is complete.",
                "evidence_kind": evidence_kind,
            }
        ],
        "budget": {
            "token_policy": "hard",
            "tool_policy": "hard",
            "max_attempts": 3,
            "max_wall_seconds": 3600,
            "max_tool_calls": 100,
            "max_tokens": 10000,
        },
        "repair_policy": {
            "on_reject": on_reject,
            "max_same_scope_repairs": 2 if on_reject == "retry-same-scope" else 0,
            "max_amendments": 3,
        },
        "loop_policy": {
            "enabled": False,
            "max_rounds": 0,
            "stop_condition": "Acceptance criteria pass or the node blocks.",
        },
        "council_trigger": {
            "required": False,
            "reason": "No unresolved instruction conflict in this fixture.",
        },
    }


def graph(project_root: Path) -> dict:
    return {
        "schema_version": 1,
        "graph_id": "graph-test-fullstack",
        "revision": 1,
        "previous_revision_hash": None,
        "intent": "Exercise parallel frontend and backend delivery with independent security acceptance.",
        "project_root": str(project_root.resolve()),
        "data_classification": "internal",
        "integration_node_id": "node-integrate",
        "global_budget": {
            "max_total_attempts": 30,
            "max_total_tokens": 100000,
            "max_wall_seconds": 7200,
            "max_amendments": 4,
            "token_policy": "hard",
            "time_policy": "hard",
        },
        "nodes": [
            node(
                "node-frontend",
                "work",
                "implementation",
                "fleet-run",
                [],
                ["frontend/"],
                "detached-worktree",
                on_reject="retry-same-scope",
            ),
            node(
                "node-backend",
                "work",
                "implementation",
                "fleet-run",
                [],
                ["backend/"],
                "detached-worktree",
                on_reject="retry-same-scope",
            ),
            node(
                "node-integrate",
                "work",
                "integration",
                "parent-local",
                ["node-frontend", "node-backend"],
                ["frontend/", "backend/"],
                "integration-owner",
            ),
            node(
                "node-quality",
                "gate",
                "independent-quality",
                "fleet-quality-gate",
                ["node-integrate"],
                [],
                "read-only",
                evidence_from=["node-frontend", "node-backend", "node-integrate"],
            ),
            node(
                "node-security",
                "gate",
                "incremental-security",
                "fleet-security-gate",
                ["node-integrate"],
                [],
                "read-only",
                evidence_from=["node-frontend", "node-backend", "node-integrate"],
            ),
            node(
                "node-final",
                "gate",
                "final-acceptance",
                "parent-local",
                ["node-quality", "node-security"],
                [],
                "read-only",
            ),
        ],
    }


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def evidence_for(
    graph_value: dict,
    node_id: str,
    workspace: Path,
    verdict: str = "accepted",
    changed_path: str | None = None,
    sequence: str = "one",
) -> dict:
    graph_node = next(item for item in graph_value["nodes"] if item["node_id"] == node_id)
    artifacts = []
    changed_paths = []
    if changed_path:
        artifact_path = workspace / changed_path
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(f"artifact-{node_id}-{sequence}\n", encoding="utf-8")
        payload = artifact_path.read_bytes()
        artifacts.append(
            {
                "path": changed_path,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        )
        changed_paths.append(changed_path)
    criterion_status = "PASS" if verdict == "accepted" else "FAIL"
    external_refs = []
    expected = agent_graph.EXPECTED_EXTERNAL_REF.get(graph_node["executor"])
    if expected:
        external_refs.append({"kind": expected, "id": f"external-{node_id}-{sequence}"})
    if graph_node["capability"] == "final-acceptance":
        external_refs.append({"kind": "parent_acceptance", "id": f"parent-{sequence}"})
    return {
        "schema_version": 1,
        "evidence_id": f"evidence-{node_id.removeprefix('node-')}-{sequence}",
        "node_id": node_id,
        "verdict": verdict,
        "summary": f"Evidence for {node_id} verdict {verdict}.",
        "workspace_root": str(workspace.resolve()),
        "changed_paths": changed_paths,
        "artifacts": artifacts,
        "criteria_results": [
            {
                "criterion_id": criterion["criterion_id"],
                "status": criterion_status,
                "evidence_ref": f"test:{node_id}:{sequence}",
            }
            for criterion in graph_node["acceptance_criteria"]
        ],
        "checks": [
            {
                "name": "test-check",
                "status": "PASS" if verdict == "accepted" else "FAIL",
                "evidence_ref": f"test:{node_id}:{sequence}",
            }
        ],
        "external_refs": external_refs,
        "findings": []
        if verdict == "accepted"
        else [
            {
                "severity": "medium",
                "code": "TEST-DEFECT",
                "summary": "A test defect requires bounded repair.",
                "evidence_ref": f"test:{node_id}:{sequence}",
            }
        ],
        "telemetry": {
            "model": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": 10,
            "tool_calls": 1,
            "cost_usd": None,
            "quota_before": None,
            "quota_after": None,
            "duration_seconds": 1.0,
        },
    }


class AgentGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "frontend").mkdir()
        (self.project / "backend").mkdir()
        self.state_root = self.root / "state"
        self.graph = graph(self.project)
        self.graph_path = self.root / "graph.json"
        write_json(self.graph_path, self.graph)
        self.prompt_path = self.root / "intake.md"
        self.prompt_path.write_text(self.graph["intent"] + "\n", encoding="utf-8")
        self.fidelity_path = self.root / "fidelity.md"
        self.fidelity_path.write_text("PASS fidelity\n", encoding="utf-8")
        self.sufficiency_path = self.root / "sufficiency.md"
        self.sufficiency_path.write_text("PASS sufficiency\n", encoding="utf-8")
        self.contract_path = self.make_approved_contract()
        self.acceptance_path = self.make_acceptance_contract(
            self.graph, self.contract_path, "acceptance"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_approved_contract(
        self, planning_tokens: int = 50000, stem: str = "contract"
    ) -> Path:
        node_to_task = {
            node_value["node_id"]: f"TASK-{index:03d}"
            for index, node_value in enumerate(self.graph["nodes"], 1)
        }
        clauses = []
        requirements = []
        tasks = []
        for index, node_value in enumerate(self.graph["nodes"], 1):
            clause_id = f"CLAUSE-{index:03d}"
            requirement_id = f"REQ-{index:03d}"
            task_id = f"TASK-{index:03d}"
            clauses.append(
                {
                    "clause_id": clause_id,
                    "kind": "derived",
                    "text": f"Execute and verify {node_value['title']}.",
                    "source_ref": "intake.md:1",
                    "material": True,
                    "requirement_refs": [requirement_id],
                }
            )
            requirements.append(
                {
                    "requirement_id": requirement_id,
                    "kind": "acceptance",
                    "text": f"{node_value['title']} must satisfy its acceptance criteria.",
                    "source_ref": "intake.md:1",
                    "clause_refs": [clause_id],
                    "material": True,
                    "task_refs": [task_id],
                    "status": "active",
                }
            )
            tasks.append(
                {
                    "task_id": task_id,
                    "graph_node_id": node_value["node_id"],
                    "title": node_value["title"],
                    "kind": node_value["kind"],
                    "requirement_refs": [requirement_id],
                    "depends_on": [node_to_task[item] for item in node_value["depends_on"]],
                    "evidence_from": [node_to_task[item] for item in node_value["evidence_from"]],
                    "wave": index,
                    "capability": node_value["capability"],
                    "executor": node_value["executor"],
                    "reviewer": node_value["reviewer"],
                    "workspace_mode": node_value["workspace_mode"],
                    "read_scope": node_value["read_scope"],
                    "write_scope": node_value["write_scope"],
                    "resource_locks": node_value["resource_locks"],
                    "acceptance_criteria": node_value["acceptance_criteria"],
                    "budget": node_value["budget"],
                    "repair_policy": node_value["repair_policy"],
                    "loop_policy": node_value["loop_policy"],
                    "council_trigger": node_value["council_trigger"],
                    "status": "pending",
                }
            )
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
        contract = {
            "schema_version": 1,
            "contract_id": "plan-agent-graph-tests",
            "revision": 1,
            "previous_contract_hash": None,
            "status": "draft",
            "prompt": {
                "source": "intake.md",
                "summary": self.graph["intent"],
                "sha256": plan_contract.prompt_hash(self.prompt_path),
                "context_sources": [],
            },
            "classification": {
                "tier": "full",
                "hard_risk_flags": [],
                "data_classification": "internal",
                "production_boundary": "Temporary test workspace only.",
            },
            "clauses": clauses,
            "requirements": requirements,
            "questions": [],
            "assumptions": [],
            "out_of_scope": ["Production deployment"],
            "decisions": [],
            "tasks": tasks,
            "gates": {
                "fidelity": copy.deepcopy(pending_gate),
                "sufficiency": copy.deepcopy(pending_gate),
            },
            "planning": {
                "repair_round": 0,
                "max_repair_rounds": 2,
                "planner_identity": "planner-test",
                "planner_lineage": "openai",
                "planner_session_id": "planner-session-test",
                "review_history": [],
            },
            "approval": {"approved_by": None, "approved_at": None, "contract_hash": None},
            "owner_ceiling": plan_contract.budget_pools.example_owner_ceiling(
                planning_and_review=planning_tokens,
                execution=self.graph["global_budget"]["max_total_tokens"],
            ),
            "graph": {
                "graph_id": self.graph["graph_id"],
                "project_root": self.graph["project_root"],
                "integration_task_id": node_to_task[self.graph["integration_node_id"]],
                "global_budget": self.graph["global_budget"],
                "previous_projection_sha256": None,
                "projection_sha256": None,
            },
            "amendment": None,
            "closeout": {"status": "pending", "receipt_sha256": None},
        }
        if "token_policy" in self.graph["global_budget"]:
            contract["owner_ceiling"]["token_policy"] = self.graph["global_budget"]["token_policy"]
        return self.approve_contract_value(contract, stem)

    def approve_contract_value(
        self, contract: dict, stem: str, base_contract: Path | None = None
    ) -> Path:
        plan_hash = plan_contract.planning_payload_hash(contract)
        clauses = contract["clauses"]
        requirements = contract["requirements"]
        tasks = contract["tasks"]
        revision = contract["revision"]
        fidelity_path = self.root / ("fidelity.md" if revision == 1 else f"fidelity-r{revision}.md")
        sufficiency_path = self.root / (
            "sufficiency.md" if revision == 1 else f"sufficiency-r{revision}.md"
        )
        if revision > 1:
            fidelity_path.write_text(
                f"PASS fidelity revision {revision}\n", encoding="utf-8"
            )
            sufficiency_path.write_text(
                f"PASS sufficiency revision {revision}\n", encoding="utf-8"
            )
        contract["gates"]["fidelity"] = {
            "round": contract["planning"]["repair_round"],
            "status": "pass",
            "auditor": {
                "identity": "fidelity-test",
                "lineage": "anthropic",
                "session_id": f"fidelity-session-test-r{revision}",
                "independent_from": ["planner-test"],
            },
            "reviewed_prompt_sha256": contract["prompt"]["sha256"],
            "reviewed_plan_sha256": plan_hash,
            "reviewed_clause_ids": [item["clause_id"] for item in clauses],
            "reviewed_requirement_ids": [item["requirement_id"] for item in requirements],
            "reviewed_task_ids": [],
            "attested_hard_risk_flags": contract["classification"]["hard_risk_flags"],
            "evidence": {
                "path": fidelity_path.name,
                "sha256": plan_contract.file_hash(fidelity_path),
            },
        }
        contract["gates"]["sufficiency"] = {
            "round": contract["planning"]["repair_round"],
            "status": "pass",
            "auditor": {
                "identity": "sufficiency-test",
                "lineage": "google",
                "session_id": f"sufficiency-session-test-r{revision}",
                "independent_from": ["planner-test"],
            },
            "reviewed_prompt_sha256": contract["prompt"]["sha256"],
            "reviewed_plan_sha256": plan_hash,
            "reviewed_clause_ids": [item["clause_id"] for item in clauses],
            "reviewed_requirement_ids": [item["requirement_id"] for item in requirements],
            "reviewed_task_ids": [item["task_id"] for item in tasks],
            "attested_hard_risk_flags": [],
            "evidence": {
                "path": sufficiency_path.name,
                "sha256": plan_contract.file_hash(sufficiency_path),
            },
        }
        contract["planning"]["review_history"].extend(
            [
                {
                    "revision": revision,
                    "gate": "fidelity",
                    **copy.deepcopy(contract["gates"]["fidelity"]),
                },
                {
                    "revision": revision,
                    "gate": "sufficiency",
                    **copy.deepcopy(contract["gates"]["sufficiency"]),
                },
            ]
        )
        draft_path = self.root / f"{stem}-draft.json"
        approved_path = self.root / f"{stem}-approved.json"
        write_json(draft_path, contract)
        plan_contract.command_approve(
            argparse.Namespace(
                contract=str(draft_path),
                output=str(approved_path),
                approved_by="parent-architect",
                base_contract=str(base_contract) if base_contract else None,
            )
        )
        return approved_path

    def make_acceptance_contract(
        self,
        graph_value: dict,
        planning_path: Path,
        stem: str,
    ) -> Path:
        planning = plan_contract.load_json(planning_path, "approved contract")
        requirement_ids = [item["requirement_id"] for item in planning["requirements"]]
        groups = [requirement_ids[0::3], requirement_ids[1::3], requirement_ids[2::3]]
        independence = {
            "not_same_actor": True,
            "not_same_lineage": True,
            "allowed_roles": ["independent-reviewer"],
        }
        contract = {
            "schema_version": 1,
            "acceptance_id": "accept-agent-graph-tests",
            "project_slug": "agent-graph-tests",
            "status": "READY",
            "planning_contract": {
                "contract_id": planning["contract_id"],
                "contract_hash": planning["approval"]["contract_hash"],
            },
            "graph": {
                "graph_id": graph_value["graph_id"],
                "graph_hash": agent_graph.sha256_value(graph_value),
            },
            "environment": {
                "name": "DEV",
                "target": "http://fixture.invalid",
                "synthetic_data": True,
                "recipient_policy": "synthetic-only",
                "external_side_effects": "blocked",
                "allowed_destinations": [],
                "denied_effects": [
                    "email",
                    "payment",
                    "calendar",
                    "crm",
                    "public-integration",
                ],
                "enforcement_scope": [
                    {
                        "scope": "browser-context",
                        "mechanism": "Playwright fixture routing blocks external requests",
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
            "maker": {
                "id": "graph-test-maker",
                "lineage": "openai",
                "role": "implementer",
            },
            "max_generations": 2,
            "criteria": [
                {
                    "criterion_id": "accept-graph-business",
                    "title": "Graph business contract is satisfied",
                    "requirement_refs": groups[0],
                    "mandatory": True,
                    "applicability": "applicable",
                    "domain": "business",
                    "oracle_kind": "business-oracle",
                    "oracle": {
                        "expected": "all mapped graph requirements have accepted evidence",
                        "method": "compare graph node evidence with the planning ledger",
                        "source_ref": "planning-contract:requirements",
                    },
                    "evidence_kinds": ["deterministic-check"],
                    "max_age_seconds": 3600,
                    "reviewer_independence": copy.deepcopy(independence),
                    "n_a_authority": None,
                    "required_enforcement_scopes": [],
                },
                {
                    "criterion_id": "accept-graph-journey",
                    "title": "Synthetic user journey is visible and complete",
                    "requirement_refs": groups[1],
                    "mandatory": True,
                    "applicability": "applicable",
                    "domain": "ui-functional",
                    "oracle_kind": "user-journey-evidence",
                    "oracle": {
                        "expected": "synthetic user reaches the final confirmation",
                        "method": "run a bounded browser journey against the fixture",
                        "source_ref": "planning-contract:user-journey",
                    },
                    "evidence_kinds": ["browser-e2e"],
                    "max_age_seconds": 3600,
                    "reviewer_independence": copy.deepcopy(independence),
                    "n_a_authority": None,
                    "required_enforcement_scopes": [],
                },
                {
                    "criterion_id": "accept-graph-side-effects",
                    "title": "DEV external effects stay blocked",
                    "requirement_refs": groups[2],
                    "mandatory": True,
                    "applicability": "applicable",
                    "domain": "side-effect",
                    "oracle_kind": "side-effect-safety",
                    "oracle": {
                        "expected": "no real external destination is reachable",
                        "method": "verify browser and application enforcement evidence",
                        "source_ref": "global-policy:dev-side-effects",
                    },
                    "evidence_kinds": ["deterministic-check"],
                    "max_age_seconds": 3600,
                    "reviewer_independence": copy.deepcopy(independence),
                    "n_a_authority": None,
                    "required_enforcement_scopes": [
                        "browser-context",
                        "application",
                    ],
                },
            ],
        }
        present_domains = {item["domain"] for item in contract["criteria"]}
        user_domains = {"ui-functional", "ux", "responsive", "accessibility"}
        for domain in sorted(acceptance.DOMAINS - present_domains):
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
                        "actor_id": "graph-test-discovery",
                        "actor_role": "discovery-gate",
                        "evidence_ref": f"discovery:fixture-{domain}",
                    },
                    "required_enforcement_scopes": [],
                }
            )
        path = self.root / f"{stem}.json"
        write_json(path, contract)
        return path

    def init_run(self) -> tuple[str, Path]:
        result = agent_graph.command_init(
            argparse.Namespace(
                graph=str(self.graph_path),
                state_root=str(self.state_root),
                planning_contract=str(self.contract_path),
                acceptance_contract=str(self.acceptance_path),
            )
        )
        return result["run_id"], Path(result["run_dir"])

    def test_init_refuses_uncontracted_new_run(self) -> None:
        with self.assertRaisesRegex(agent_graph.GraphError, "require --planning-contract"):
            agent_graph.command_init(
                argparse.Namespace(
                    graph=str(self.graph_path),
                    state_root=str(self.state_root),
                    planning_contract=None,
                )
            )

    def test_init_requires_acceptance_contract_for_final_acceptance_graph(self) -> None:
        with self.assertRaisesRegex(agent_graph.GraphError, "requires --acceptance-contract"):
            agent_graph.command_init(
                argparse.Namespace(
                    graph=str(self.graph_path),
                    state_root=str(self.state_root),
                    planning_contract=str(self.contract_path),
                    acceptance_contract=None,
                )
            )

    def test_init_failure_leaves_no_partial_run(self) -> None:
        real_write = agent_graph.atomic_write_json

        def fail_state(path: Path, value: dict) -> None:
            if path.name == "state.json":
                raise OSError("injected init crash")
            real_write(path, value)

        with mock.patch.object(agent_graph, "atomic_write_json", side_effect=fail_state):
            with self.assertRaisesRegex(OSError, "injected init crash"):
                agent_graph.command_init(
                    argparse.Namespace(
                        graph=str(self.graph_path),
                        state_root=str(self.state_root),
                        planning_contract=str(self.contract_path),
                        acceptance_contract=str(self.acceptance_path),
                    )
                )
        self.assertEqual(list(self.state_root.glob("run-graph-*")), [])
        self.assertEqual(list(self.state_root.glob(".init-*")), [])

    def test_status_exposes_planning_contract_linkage(self) -> None:
        run_id, _ = self.init_run()
        status = agent_graph.command_status(
            argparse.Namespace(run_id=run_id, state_root=str(self.state_root))
        )
        self.assertEqual(status["planning_contract"]["mode"], "approved-contract")

    def test_stored_contract_snapshot_is_revalidated_before_use(self) -> None:
        run_id, run_dir = self.init_run()
        state = agent_graph.load_state(run_dir)
        plan = agent_graph.load_current_plan(run_dir, state)
        snapshot = run_dir / state["planning_contract"]["snapshot_path"]
        contract = agent_graph.load_json(snapshot, "stored planning contract")
        contract["prompt"]["summary"] = "Tampered after initialization."
        agent_graph.atomic_write_json(snapshot, contract)
        with self.assertRaisesRegex(
            agent_graph.GraphError,
            "Stored planning contract validation failed",
        ):
            agent_graph.load_linked_planning_contract(run_dir, state, plan)

    def test_doctor_exposes_planner_validator_pin(self) -> None:
        doctor = agent_graph.command_doctor(argparse.Namespace())
        self.assertIn("planner_validator_pinned", doctor["checks"])
        self.assertTrue(doctor["checks"]["planner_validator_pinned"])

    def test_init_preserves_approved_contract_revision(self) -> None:
        base = plan_contract.load_json(self.contract_path, "approved contract")
        candidate = copy.deepcopy(base)
        candidate["revision"] = 2
        candidate["previous_contract_hash"] = base["approval"]["contract_hash"]
        candidate["status"] = "draft"
        candidate["approval"] = {
            "approved_by": None,
            "approved_at": None,
            "contract_hash": None,
        }
        candidate["graph"]["previous_projection_sha256"] = base["graph"][
            "projection_sha256"
        ]
        candidate["graph"]["projection_sha256"] = None
        candidate["amendment"] = {
            "reason": "Start a pre-approved revision two execution snapshot.",
            "affected_task_ids": ["TASK-006"],
            "authority": "parent",
            "approval_ref": "parent-revision-two-test",
        }
        candidate["planning"]["planner_session_id"] = "planner-session-test-r2"
        final_task = next(
            item for item in candidate["tasks"] if item["task_id"] == "TASK-006"
        )
        final_task["status"] = "blocked"
        replacement = copy.deepcopy(final_task)
        replacement["task_id"] = "TASK-007"
        replacement["graph_node_id"] = "node-final-r2"
        replacement["title"] = "Replacement final acceptance for revision two"
        replacement["status"] = "pending"
        replacement["acceptance_criteria"][0]["criterion_id"] = "crit-final-r2"
        candidate["tasks"].append(replacement)
        for requirement in candidate["requirements"]:
            if "TASK-006" in requirement["task_refs"]:
                requirement["task_refs"].append("TASK-007")
        amendment_path = self.approve_contract_value(
            candidate, "revision-two-init", self.contract_path
        )
        graph_path = self.root / "revision-two-graph.json"
        plan_contract.command_compile_graph(
            argparse.Namespace(contract=str(amendment_path), output=str(graph_path))
        )
        revision_two_graph = agent_graph.load_json(graph_path, "revision two graph")
        revision_two_acceptance = self.make_acceptance_contract(
            revision_two_graph, amendment_path, "acceptance-r2-init"
        )
        result = agent_graph.command_init(
            argparse.Namespace(
                graph=str(graph_path),
                state_root=str(self.state_root),
                planning_contract=str(amendment_path),
                acceptance_contract=str(revision_two_acceptance),
            )
        )
        run_dir = Path(result["run_dir"])
        state = agent_graph.load_state(run_dir)
        self.assertEqual(state["revision"], 2)
        self.assertTrue((run_dir / "plans" / "revision-2.json").is_file())
        self.assertFalse((run_dir / "plans" / "revision-1.json").exists())

    def test_status_supports_state_created_before_contract_linkage(self) -> None:
        run_id, run_dir = self.init_run()
        state_path = run_dir / "state.json"
        state = agent_graph.load_json(state_path, "state")
        del state["planning_contract"]
        plan_path = run_dir / "plans" / "revision-1.json"
        historical_plan = agent_graph.load_json(plan_path, "historical plan")
        for historical_node in historical_plan["nodes"]:
            for field in ("reviewer", "resource_locks", "loop_policy", "council_trigger"):
                del historical_node[field]
        agent_graph.atomic_write_json(plan_path, historical_plan)
        state["plan_hash"] = agent_graph.sha256_value(historical_plan)
        agent_graph.atomic_write_json(state_path, state)
        status = agent_graph.command_status(
            argparse.Namespace(run_id=run_id, state_root=str(self.state_root))
        )
        self.assertEqual(status["planning_contract"]["mode"], "legacy-pre-0.2")

    def start(self, run_id: str, node_id: str, operation_id: str) -> dict:
        return agent_graph.command_start(
            argparse.Namespace(
                run_id=run_id,
                node_id=node_id,
                operation_id=operation_id,
                workspace_root=str(self.project),
                external_ref=None,
                state_root=str(self.state_root),
            )
        )

    def result(
        self,
        run_id: str,
        node_id: str,
        operation_id: str,
        evidence: dict,
    ) -> dict:
        evidence_path = self.root / f"{operation_id}.json"
        write_json(evidence_path, evidence)
        return agent_graph.command_result(
            argparse.Namespace(
                run_id=run_id,
                node_id=node_id,
                operation_id=operation_id,
                evidence=str(evidence_path),
                state_root=str(self.state_root),
            )
        )

    def complete(
        self,
        run_id: str,
        node_id: str,
        sequence: str,
        changed_path: str | None = None,
        verdict: str = "accepted",
        graph_value: dict | None = None,
    ) -> None:
        self.start(run_id, node_id, f"op-start-{sequence}")
        evidence = evidence_for(
            graph_value or self.graph,
            node_id,
            self.project,
            verdict=verdict,
            changed_path=changed_path,
            sequence=sequence,
        )
        active_graph = graph_value or self.graph
        graph_node = next(item for item in active_graph["nodes"] if item["node_id"] == node_id)
        run_dir = agent_graph.resolve_run_dir(self.state_root.resolve(), run_id)
        state = agent_graph.load_state(run_dir)
        for source in graph_node["evidence_from"]:
            for reference in state["nodes"][source]["external_refs"]:
                if reference not in evidence["external_refs"]:
                    evidence["external_refs"].append(reference)
        self.result(run_id, node_id, f"op-result-{sequence}", evidence)

    def planning_closeout_receipt(self, filename: str = "planning-closeout-receipt.json") -> Path:
        contract = plan_contract.load_json(self.contract_path, "approved contract")
        proof = self.root / "planning-closeout-proof.txt"
        proof.write_text("All graph requirements have accepted node evidence.\n", encoding="utf-8")
        task_map = {item["task_id"]: item for item in contract["tasks"]}
        records = []
        for requirement in contract["requirements"]:
            task_value = task_map[requirement["task_refs"][0]]
            criterion_id = task_value["acceptance_criteria"][0]["criterion_id"]
            records.append(
                {
                    "requirement_id": requirement["requirement_id"],
                    "disposition": "satisfied",
                    "criterion_ids": [criterion_id],
                    "evidence": [
                        {
                            "criterion_id": criterion_id,
                            "path": proof.name,
                            "sha256": plan_contract.file_hash(proof),
                        }
                    ],
                    "blocker": None,
                }
            )
        closeout_path = self.root / "planning-closeout-input.json"
        receipt_path = self.root / filename
        write_json(
            closeout_path,
            {
                "schema_version": 1,
                "contract_id": contract["contract_id"],
                "contract_hash": contract["approval"]["contract_hash"],
                "requirements": records,
            },
        )
        plan_contract.command_closeout(
            argparse.Namespace(
                contract=str(self.contract_path),
                evidence=str(closeout_path),
                output=str(receipt_path),
            )
        )
        return receipt_path

    def acceptance_receipt(
        self,
        graph_run_id: str,
        graph_value: dict | None = None,
        planning_path: Path | None = None,
        acceptance_path: Path | None = None,
    ) -> Path:
        active_graph = graph_value or self.graph
        active_planning = planning_path or self.contract_path
        active_acceptance = acceptance_path or self.acceptance_path
        acceptance_state_root = self.root / "acceptance-state"
        active_graph_path = self.root / (
            "acceptance-graph-" + agent_graph.sha256_value(active_graph)[:12] + ".json"
        )
        if not active_graph_path.exists():
            write_json(active_graph_path, active_graph)
        initialized = acceptance.command_init(
            argparse.Namespace(
                contract=str(active_acceptance),
                planning_contract=str(active_planning),
                graph=str(active_graph_path),
                graph_run_id=graph_run_id,
                state_root=str(acceptance_state_root),
            )
        )
        run_id = initialized["run_id"]
        binding = {
            "schema_version": 1,
            "binding_id": "binding-graph-test",
            "code": {
                "revision": "fixture-current",
                "sha256": agent_graph.sha256_value(active_graph),
                "evidence_ref": "fixture:graph-bytes",
            },
            "environment": {
                "name": "DEV",
                "target": "http://fixture.invalid",
                "policy_sha256": "d" * 64,
            },
            "maker": {
                "id": "graph-test-maker",
                "lineage": "openai",
                "role": "implementer",
            },
        }
        binding_path = self.root / f"{run_id}-binding.json"
        write_json(binding_path, binding)
        started = acceptance.command_start(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-start-acceptance",
                binding=str(binding_path),
                state_root=str(acceptance_state_root),
            )
        )
        contract = acceptance.load_json(active_acceptance, "acceptance fixture")
        env_by_scope = {
            item["scope"]: item for item in contract["environment"]["enforcement_scope"]
        }
        runtime = acceptance.runtime_identity()
        for index, criterion in enumerate(contract["criteria"], 1):
            if criterion["applicability"] == "not-applicable":
                continue
            evidence_root = self.root / f"{run_id}-evidence-{index}"
            evidence_root.mkdir()
            proof = evidence_root / "proof.txt"
            proof.write_text(
                f"accepted evidence for {criterion['criterion_id']}\n",
                encoding="utf-8",
            )
            proof_bytes = proof.read_bytes()
            journey = None
            enforcement = None
            if criterion["oracle_kind"] == "user-journey-evidence":
                journey = {
                    "role": "synthetic-user",
                    "route": "/fixture",
                    "viewport": {"width": 1280, "height": 800},
                    "actions": ["open fixture", "complete bounded flow", "inspect confirmation"],
                    "expected": criterion["oracle"]["expected"],
                    "actual": "synthetic user reaches the final confirmation",
                }
            if criterion["oracle_kind"] == "side-effect-safety":
                enforcement = [
                    {
                        "scope": scope,
                        "mechanism": env_by_scope[scope]["mechanism"],
                        "evidence_ref": "artifact:proof.txt",
                    }
                    for scope in criterion["required_enforcement_scopes"]
                ]
            evidence = {
                "schema_version": 1,
                "evidence_id": f"evidence-graph-acceptance-{index}",
                "criterion_id": criterion["criterion_id"],
                "generation": 1,
                "binding_sha256": started["binding_sha256"],
                "status": "PASS",
                "reviewer": {
                    "id": "graph-test-independent-reviewer",
                    "lineage": "xai",
                    "role": "independent-reviewer",
                },
                "observed_at": acceptance.utc_now(),
                "summary": "Independent fixture acceptance evidence.",
                "oracle_result": {
                    "expected": criterion["oracle"]["expected"],
                    "actual": "fixture produced the required result",
                    "method": criterion["oracle"]["method"],
                },
                "evidence_kind": criterion["evidence_kinds"][0],
                "artifacts": [
                    {
                        "path": proof.name,
                        "sha256": hashlib.sha256(proof_bytes).hexdigest(),
                        "size_bytes": len(proof_bytes),
                    }
                ],
                "journey": journey,
                "enforcement_scope": enforcement,
                "runtime": {
                    "helper_sha256": runtime["helper_sha256"],
                    "skill_manifest_sha256": runtime["skill_manifest_sha256"],
                },
                "findings": [],
            }
            evidence_path = evidence_root / "evidence.json"
            write_json(evidence_path, evidence)
            acceptance.command_result(
                argparse.Namespace(
                    run_id=run_id,
                    operation_id=f"op-result-acceptance-{index}",
                    criterion_id=criterion["criterion_id"],
                    evidence=str(evidence_path),
                    state_root=str(acceptance_state_root),
                )
            )
        closed = acceptance.command_close(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-close-acceptance",
                stop_reason=None,
                output=None,
                state_root=str(acceptance_state_root),
            )
        )
        self.assertEqual(closed["status"], "ACCEPTED")
        return Path(closed["receipt_path"])

    def test_valid_parallel_graph_and_complete_lifecycle(self) -> None:
        summary = agent_graph.validate_graph(self.graph)
        self.assertEqual(summary["parallel_mutation_pairs"], [["node-frontend", "node-backend"]])
        run_id, _ = self.init_run()
        ready = agent_graph.command_ready(
            argparse.Namespace(run_id=run_id, state_root=str(self.state_root))
        )
        self.assertEqual(
            {item["node_id"] for item in ready["ready"]},
            {"node-frontend", "node-backend"},
        )
        self.complete(run_id, "node-frontend", "frontend", "frontend/app.txt")
        self.complete(run_id, "node-backend", "backend", "backend/api.txt")
        self.complete(run_id, "node-integrate", "integrate", "frontend/integrated.txt")
        self.complete(run_id, "node-quality", "quality")
        self.complete(run_id, "node-security", "security")
        self.complete(run_id, "node-final", "final")
        receipt_path = self.planning_closeout_receipt()
        acceptance_receipt_path = self.acceptance_receipt(run_id)
        closed = agent_graph.command_close(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-close-graph",
                planning_closeout_receipt=str(receipt_path),
                acceptance_receipt=str(acceptance_receipt_path),
                state_root=str(self.state_root),
            )
        )
        self.assertEqual(closed["status"], "CLOSED")

    def test_acceptance_receipt_cannot_be_replayed_across_graph_runs(self) -> None:
        first_run_id, _ = self.init_run()
        receipt_for_first_run = self.acceptance_receipt(first_run_id)
        second_run_id, _ = self.init_run()
        self.complete(second_run_id, "node-frontend", "replay-fe", "frontend/app.txt")
        self.complete(second_run_id, "node-backend", "replay-be", "backend/api.txt")
        self.complete(
            second_run_id, "node-integrate", "replay-integrate", "frontend/integrated.txt"
        )
        self.complete(second_run_id, "node-quality", "replay-quality")
        self.complete(second_run_id, "node-security", "replay-security")
        self.complete(second_run_id, "node-final", "replay-final")
        planning_receipt = self.planning_closeout_receipt("planning-replay-receipt.json")
        with self.assertRaisesRegex(agent_graph.GraphError, "Agent Graph run_id mismatch"):
            agent_graph.command_close(
                argparse.Namespace(
                    run_id=second_run_id,
                    operation_id="op-close-replayed-acceptance",
                    planning_closeout_receipt=str(planning_receipt),
                    acceptance_receipt=str(receipt_for_first_run),
                    state_root=str(self.state_root),
                )
            )

    def test_rejects_cycle(self) -> None:
        broken = copy.deepcopy(self.graph)
        broken["nodes"][0]["depends_on"] = ["node-final"]
        with self.assertRaisesRegex(agent_graph.GraphError, "cycle"):
            agent_graph.validate_graph(broken)

    def test_rejects_equal_maker_and_reviewer_lineage(self) -> None:
        broken = copy.deepcopy(self.graph)
        broken["nodes"][0]["maker_lineage"] = "same-lineage"
        broken["nodes"][0]["reviewer_lineage"] = "same-lineage"
        with self.assertRaisesRegex(
            agent_graph.GraphError, "maker_lineage cannot equal reviewer_lineage"
        ):
            agent_graph.validate_graph(broken)

    def test_rejects_unordered_overlapping_writes(self) -> None:
        broken = copy.deepcopy(self.graph)
        broken["nodes"][1]["write_scope"] = ["frontend/"]
        with self.assertRaisesRegex(agent_graph.GraphError, "overlap"):
            agent_graph.validate_graph(broken)

    def test_requires_explicit_independent_quality_coverage(self) -> None:
        broken = copy.deepcopy(self.graph)
        quality = next(item for item in broken["nodes"] if item["node_id"] == "node-quality")
        quality["evidence_from"].remove("node-backend")
        with self.assertRaisesRegex(agent_graph.GraphError, "quality gate does not explicitly cover"):
            agent_graph.validate_graph(broken)

    def test_ordered_gate_dependency_warns_without_rejecting_legacy_graph(self) -> None:
        broken = copy.deepcopy(self.graph)
        quality = next(item for item in broken["nodes"] if item["node_id"] == "node-quality")
        quality["depends_on"] = ["node-security"]
        result = agent_graph.validate_graph(broken)
        self.assertIn("review-of-review", result["scheduling_warnings"][0])

    def test_operation_id_is_idempotent_and_payload_bound(self) -> None:
        run_id, _ = self.init_run()
        first = self.start(run_id, "node-frontend", "op-idempotent-start")
        second = self.start(run_id, "node-frontend", "op-idempotent-start")
        self.assertEqual(first, second)
        with self.assertRaisesRegex(agent_graph.GraphError, "another payload"):
            agent_graph.command_start(
                argparse.Namespace(
                    run_id=run_id,
                    node_id="node-backend",
                    operation_id="op-idempotent-start",
                    workspace_root=str(self.project),
                    external_ref=None,
                    state_root=str(self.state_root),
                )
            )

    def test_artifact_hash_mismatch_is_rejected(self) -> None:
        run_id, _ = self.init_run()
        self.start(run_id, "node-frontend", "op-start-bad-hash")
        evidence = evidence_for(
            self.graph,
            "node-frontend",
            self.project,
            changed_path="frontend/bad.txt",
            sequence="bad-hash",
        )
        evidence["artifacts"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(agent_graph.GraphError, "hash or size mismatch"):
            self.result(run_id, "node-frontend", "op-result-bad-hash", evidence)

    def test_evidence_workspace_must_match_start_binding(self) -> None:
        run_id, _ = self.init_run()
        self.start(run_id, "node-frontend", "op-start-workspace-binding")
        other_workspace = self.root / "other-workspace"
        other_workspace.mkdir()
        evidence = evidence_for(
            self.graph,
            "node-frontend",
            other_workspace,
            changed_path="frontend/app.txt",
            sequence="workspace-mismatch",
        )
        with self.assertRaisesRegex(agent_graph.GraphError, "workspace_root does not match"):
            self.result(run_id, "node-frontend", "op-result-workspace-mismatch", evidence)

    def test_gate_evidence_must_include_covered_executor_refs(self) -> None:
        run_id, _ = self.init_run()
        self.complete(run_id, "node-frontend", "gate-ref-fe", "frontend/app.txt")
        self.complete(run_id, "node-backend", "gate-ref-be", "backend/api.txt")
        self.complete(run_id, "node-integrate", "gate-ref-integrate", "frontend/integrated.txt")
        self.start(run_id, "node-quality", "op-start-gate-ref")
        evidence = evidence_for(
            self.graph,
            "node-quality",
            self.project,
            sequence="missing-source-refs",
        )
        with self.assertRaisesRegex(agent_graph.GraphError, "omits covered executor references"):
            self.result(run_id, "node-quality", "op-result-missing-source-refs", evidence)

    def test_exhausted_global_token_budget_blocks_start(self) -> None:
        run_id, run_dir = self.init_run()
        with agent_graph.run_lock(run_dir):
            state = agent_graph.load_state(run_dir)
            state["telemetry"]["known_total_tokens"] = self.graph["global_budget"][
                "max_total_tokens"
            ]
            agent_graph.atomic_write_json(run_dir / "state.json", state)
        with self.assertRaisesRegex(agent_graph.GraphError, "already exhausted"):
            self.start(run_id, "node-frontend", "op-start-token-exhausted")

    def prepare_estimate_graph(self):
        self.graph['global_budget'].update(token_policy='estimate', clock='active')
        for graph_node in self.graph['nodes']:
            graph_node['budget']['token_policy'] = 'estimate'
        self.contract_path = self.make_approved_contract(stem='estimate')
        self.acceptance_path = self.make_acceptance_contract(self.graph, self.contract_path, 'accept-estimate')
        write_json(self.graph_path, self.graph)
        return self.init_run()

    def test_soft_estimate_overrun_records_warning_not_hard_failure(self):
        run_id, run_dir = self.prepare_estimate_graph()
        self.start(run_id, 'node-frontend', 'op-start-soft')
        evidence = evidence_for(self.graph, 'node-frontend', self.project, changed_path='frontend/App.tsx')
        evidence['telemetry']['total_tokens'] = self.graph['global_budget']['max_total_tokens'] + 1
        result = self.result(run_id, 'node-frontend', 'op-result-soft', evidence)
        self.assertFalse(result['budget_exceeded'])
        self.assertIn('global-token-estimate-exceeded', result['estimate_warnings'])
        state = agent_graph.load_state(run_dir)
        self.assertFalse(state['telemetry']['budget_exceeded'])
        self.assertIsNotNone(state['telemetry']['active_intervals'][0]['end'])

    def prepare_time_graph(self, policy: str, clock: str = "elapsed"):
        self.graph["global_budget"].update(
            time_policy=policy,
            clock=clock,
            max_wall_seconds=60,
        )
        self.contract_path = self.make_approved_contract(stem=f"time-{policy}-{clock}")
        self.acceptance_path = self.make_acceptance_contract(
            self.graph, self.contract_path, f"accept-time-{policy}-{clock}"
        )
        write_json(self.graph_path, self.graph)
        return self.init_run()

    def make_old(self, run_dir):
        state = agent_graph.load_state(run_dir)
        state["created_at"] = "2020-01-01T00:00:00Z"
        agent_graph.atomic_write_json(run_dir / "state.json", state)

    def test_time_estimate_overrun_allows_start_and_accepted_result(self):
        run_id, run_dir = self.prepare_time_graph("estimate")
        self.make_old(run_dir)
        started = self.start(run_id, "node-frontend", "op-start-time-estimate")
        self.assertEqual(started["status"], "STARTED")
        self.assertEqual(started["time_policy"], "estimate")
        evidence = evidence_for(
            self.graph, "node-frontend", self.project, changed_path="frontend/time.txt"
        )
        recorded = self.result(run_id, "node-frontend", "op-result-time-estimate", evidence)
        self.assertEqual(recorded["verdict"], "accepted")
        self.assertIn("global-time-estimate-exceeded", recorded["estimate_warnings"])
        self.assertFalse(recorded["budget_exceeded"])

    def test_time_hard_overrun_blocks_start(self):
        run_id, run_dir = self.prepare_time_graph("hard")
        self.make_old(run_dir)
        with self.assertRaisesRegex(agent_graph.GraphError, "wall-time budget exhausted"):
            self.start(run_id, "node-frontend", "op-start-time-hard")

    def test_time_estimate_overrun_does_not_block_close(self):
        run_id, run_dir = self.prepare_time_graph("estimate")
        self.complete(run_id, "node-frontend", "time-close-fe", "frontend/app.txt")
        self.complete(run_id, "node-backend", "time-close-be", "backend/api.txt")
        self.complete(run_id, "node-integrate", "time-close-integrate", "frontend/integrated.txt")
        self.complete(run_id, "node-quality", "time-close-quality")
        self.complete(run_id, "node-security", "time-close-security")
        self.complete(run_id, "node-final", "time-close-final")
        receipt_path = self.planning_closeout_receipt("time-close-planning.json")
        acceptance_receipt_path = self.acceptance_receipt(run_id)
        self.make_old(run_dir)
        closed = agent_graph.command_close(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-close-time-estimate",
                planning_closeout_receipt=str(receipt_path),
                acceptance_receipt=str(acceptance_receipt_path),
                state_root=str(self.state_root),
            )
        )
        self.assertEqual(closed["status"], "CLOSED")

    def test_known_hard_overrun_latches_even_if_attested_counter_diverges(self):
        run_id, run_dir = self.init_run()
        self.start(run_id, 'node-frontend', 'op-start-known-cap')
        state = agent_graph.load_state(run_dir)
        state['telemetry']['known_total_tokens'] = self.graph['global_budget']['max_total_tokens'] + 1
        state['telemetry']['attested_total_tokens'] = 0
        agent_graph.atomic_write_json(run_dir / 'state.json', state)
        evidence = evidence_for(self.graph, 'node-frontend', self.project, changed_path='frontend/App.tsx')
        result = self.result(run_id, 'node-frontend', 'op-result-known-cap', evidence)
        self.assertTrue(result['budget_exceeded'])
        self.assertTrue(agent_graph.load_state(run_dir)['telemetry']['budget_exceeded'])

    def test_active_clock_recovery_cannot_restart_unreconciled_interval(self):
        run_id, run_dir = self.prepare_estimate_graph()
        self.start(run_id, 'node-frontend', 'op-start-active-live')
        state = agent_graph.load_state(run_dir)
        state['nodes']['node-frontend']['status'] = 'recovery-required'
        agent_graph.atomic_write_json(run_dir / 'state.json', state)
        before = (run_dir / 'state.json').read_bytes()
        with self.assertRaisesRegex(agent_graph.GraphError, 'prior live attempt'):
            self.start(run_id, 'node-frontend', 'op-start-active-recovery')
        self.assertEqual((run_dir / 'state.json').read_bytes(), before)

    def test_explicit_hard_reserves_parallel_attempts(self):
        self.graph['global_budget']['token_policy'] = 'hard'
        cap = self.graph['global_budget']['max_total_tokens']
        self.graph['nodes'][0]['budget']['max_tokens'] = cap // 2 + 1
        self.graph['nodes'][1]['budget']['max_tokens'] = cap // 2 + 1
        self.contract_path = self.make_approved_contract(stem='parallel-hard')
        self.acceptance_path = self.make_acceptance_contract(self.graph, self.contract_path, 'accept-parallel-hard')
        write_json(self.graph_path, self.graph)
        run_id, run_dir = self.init_run()
        self.start(run_id, 'node-frontend', 'op-start-reserved-first')
        with self.assertRaisesRegex(agent_graph.GraphError, 'reserved global hard'):
            self.start(run_id, 'node-backend', 'op-start-reserved-second')

    def test_estimate_start_does_not_request_owner_approval(self):
        self.graph["nodes"][0]["budget"]["tool_policy"] = "estimate"
        run_id, run_dir = self.prepare_estimate_graph()
        state = agent_graph.load_state(run_dir)
        state['telemetry']['attested_total_tokens'] = self.graph['global_budget']['max_total_tokens'] + 1
        agent_graph.atomic_write_json(run_dir / 'state.json', state)
        result = self.start(run_id, 'node-frontend', 'op-start-over-estimate')
        self.assertEqual(result['status'], 'STARTED')
        self.assertTrue(result['estimate_warning'])
        self.assertFalse(result.get('owner_approval_required', False))
        self.assertNotIn('max_model_tool_calls', result['dispatch_bounds'])

    def test_explicit_hard_tool_bound_is_dispatched(self):
        run_id, _ = self.init_run()
        result = self.start(run_id, 'node-frontend', 'op-start-hard-tools')
        self.assertEqual(result['tool_policy'], 'hard')
        self.assertEqual(result['dispatch_bounds']['max_model_tool_calls'], 100)

    def test_legacy_missing_policies_remain_hard_on_recovery(self):
        legacy_plan = {"global_budget": {}}
        legacy_node = {"budget": {}}
        self.assertEqual(agent_graph.token_policy(legacy_plan), "hard")
        self.assertEqual(agent_graph.time_policy(legacy_plan), "hard")
        self.assertEqual(agent_graph.tool_policy(legacy_node), "hard")

    def test_node_estimate_cannot_raise_global_owner_hard_cap(self):
        self.graph['global_budget']['token_policy'] = 'hard'
        self.graph['nodes'][0]['budget']['token_policy'] = 'estimate'
        self.contract_path = self.make_approved_contract(stem='owner-hard')
        self.acceptance_path = self.make_acceptance_contract(self.graph, self.contract_path, 'accept-hard')
        write_json(self.graph_path, self.graph)
        run_id, run_dir = self.init_run()
        state = agent_graph.load_state(run_dir)
        state['telemetry']['attested_total_tokens'] = self.graph['global_budget']['max_total_tokens']
        agent_graph.atomic_write_json(run_dir / 'state.json', state)
        with self.assertRaisesRegex(agent_graph.GraphError, 'already exhausted'):
            self.start(run_id, 'node-frontend', 'op-start-hard-owner')

    def test_active_clock_unions_parallel_work_and_excludes_idle(self):
        plan = {'global_budget': {'clock':'active'}}
        state = {'created_at':'2026-01-01T00:00:00Z', 'telemetry': {'active_intervals':[
            {'start':'2026-01-01T01:00:00Z','end':'2026-01-01T01:10:00Z'},
            {'start':'2026-01-01T01:05:00Z','end':'2026-01-01T01:15:00Z'},
            {'start':'2026-01-01T02:00:00Z','end':None},
        ]}}
        now = agent_graph.parse_utc('2026-01-01T02:05:00Z')
        self.assertEqual(agent_graph.budget_elapsed_seconds(plan,state,now), 1200)
        self.assertEqual(agent_graph.budget_elapsed_seconds({'global_budget':{}},state,now), 7500)

    def test_invalid_controller_evidence_does_not_charge_an_attempt(self):
        run_id, run_dir = self.init_run()
        self.start(run_id, 'node-frontend', 'op-start-invalid-controller')
        before = (run_dir / 'state.json').read_bytes()
        evidence = evidence_for(self.graph,'node-frontend',self.project)
        evidence['unknown_controller_field'] = True
        with self.assertRaises(agent_graph.GraphError):
            self.result(run_id, 'node-frontend', 'op-result-invalid-controller', evidence)
        self.assertEqual((run_dir/'state.json').read_bytes(), before)

    def test_exhausted_planning_pool_does_not_block_graph_start(self) -> None:
        self.contract_path = self.make_approved_contract(
            planning_tokens=0, stem="zero-planning"
        )
        self.acceptance_path = self.make_acceptance_contract(
            self.graph, self.contract_path, "acceptance-zero-planning"
        )
        run_id, _ = self.init_run()
        result = self.start(run_id, "node-frontend", "op-start-zero-planning")
        self.assertEqual(result["status"], "STARTED")

    def test_unknown_token_telemetry_stays_unknown_without_fake_charge(self) -> None:
        run_id, run_dir = self.init_run()
        self.start(run_id, "node-frontend", "op-start-unknown-tokens")
        evidence = evidence_for(
            self.graph,
            "node-frontend",
            self.project,
            changed_path="frontend/app.txt",
            sequence="unknown-tokens",
        )
        evidence["telemetry"]["total_tokens"] = None
        recorded = self.result(run_id, "node-frontend", "op-result-unknown-tokens", evidence)
        self.assertEqual(recorded["status"], "RECORDED")
        state = agent_graph.load_state(run_dir)
        self.assertEqual(state["telemetry"]["unknown_token_results"], 1)
        self.assertEqual(state["telemetry"]["known_total_tokens"], 0)
        self.assertEqual(state["telemetry"]["estimated_unknown_tokens"], 0)
        self.assertEqual(state["telemetry"]["attested_total_tokens"], 0)

    def test_start_rejects_wrong_external_reference_kind(self) -> None:
        run_id, _ = self.init_run()
        with self.assertRaisesRegex(agent_graph.GraphError, "requires external-ref kind fleet_run"):
            agent_graph.command_start(
                argparse.Namespace(
                    run_id=run_id,
                    node_id="node-frontend",
                    operation_id="op-start-wrong-ref",
                    workspace_root=str(self.project),
                    external_ref="fleet_quality_gate:qgate-wrong-kind",
                    state_root=str(self.state_root),
                )
            )

    def test_close_rejects_incomplete_graph(self) -> None:
        run_id, _ = self.init_run()
        with self.assertRaisesRegex(agent_graph.GraphError, "Active nodes are not accepted"):
            agent_graph.command_close(
                argparse.Namespace(
                    run_id=run_id,
                    operation_id="op-close-incomplete",
                    planning_closeout_receipt=None,
                    state_root=str(self.state_root),
                )
            )

    def test_contracted_close_requires_planning_receipt(self) -> None:
        run_id, _ = self.init_run()
        self.complete(run_id, "node-frontend", "receipt-fe", "frontend/app.txt")
        self.complete(run_id, "node-backend", "receipt-be", "backend/api.txt")
        self.complete(run_id, "node-integrate", "receipt-integrate", "frontend/integrated.txt")
        self.complete(run_id, "node-quality", "receipt-quality")
        self.complete(run_id, "node-security", "receipt-security")
        self.complete(run_id, "node-final", "receipt-final")
        with self.assertRaisesRegex(
            agent_graph.GraphError,
            "requires --planning-closeout-receipt",
        ):
            agent_graph.command_close(
                argparse.Namespace(
                    run_id=run_id,
                    operation_id="op-close-without-planning-receipt",
                    planning_closeout_receipt=None,
                    state_root=str(self.state_root),
                )
            )

    def test_contracted_close_rejects_forged_empty_complete_receipt(self) -> None:
        run_id, _ = self.init_run()
        self.complete(run_id, "node-frontend", "forged-fe", "frontend/app.txt")
        self.complete(run_id, "node-backend", "forged-be", "backend/api.txt")
        self.complete(run_id, "node-integrate", "forged-integrate", "frontend/integrated.txt")
        self.complete(run_id, "node-quality", "forged-quality")
        self.complete(run_id, "node-security", "forged-security")
        self.complete(run_id, "node-final", "forged-final")
        contract = plan_contract.load_json(self.contract_path, "approved contract")
        receipt = {
            "schema_version": 1,
            "contract_id": contract["contract_id"],
            "contract_hash": contract["approval"]["contract_hash"],
            "status": "COMPLETE",
            "closed_at": plan_contract.utc_now(),
            "requirements": [],
            "receipt_hash": None,
        }
        receipt["receipt_hash"] = plan_contract.sha256_value(
            {**receipt, "receipt_hash": None}
        )
        receipt_path = self.root / "forged-empty-receipt.json"
        write_json(receipt_path, receipt)
        with self.assertRaisesRegex(agent_graph.GraphError, "coverage mismatch"):
            agent_graph.command_close(
                argparse.Namespace(
                    run_id=run_id,
                    operation_id="op-close-forged-empty-receipt",
                    planning_closeout_receipt=str(receipt_path),
                    state_root=str(self.state_root),
                )
            )

    def test_approved_contract_amendment_is_append_only(self) -> None:
        run_id, run_dir = self.init_run()
        self.complete(run_id, "node-backend", "contract-amend-trigger", "backend/api.txt")
        base = plan_contract.load_json(self.contract_path, "approved base contract")
        candidate = copy.deepcopy(base)
        candidate["revision"] = 2
        candidate["previous_contract_hash"] = base["approval"]["contract_hash"]
        candidate["status"] = "draft"
        candidate["approval"] = {
            "approved_by": None,
            "approved_at": None,
            "contract_hash": None,
        }
        candidate["graph"]["previous_projection_sha256"] = base["graph"]["projection_sha256"]
        candidate["graph"]["projection_sha256"] = None
        candidate["graph"]["integration_task_id"] = "TASK-008"
        candidate["planning"]["planner_session_id"] = "planner-session-amendment"
        candidate["gates"] = {
            gate: {
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
            for gate in ("fidelity", "sufficiency")
        }
        candidate["amendment"] = {
            "reason": "Replace the backend-dependent subgraph after terminal evidence.",
            "affected_task_ids": [
                "TASK-002",
                "TASK-003",
                "TASK-004",
                "TASK-005",
                "TASK-006",
            ],
            "authority": "parent",
            "approval_ref": "parent-test-contract-amendment",
        }
        candidate["closeout"] = {"status": "pending", "receipt_sha256": None}

        tasks = {task["task_id"]: task for task in candidate["tasks"]}
        tasks["TASK-001"]["acceptance_criteria"].append(
            {
                "criterion_id": "crit-preflight-amendment",
                "description": "Approved amendment keeps the preflight evidence bound",
                "evidence_kind": "deterministic-check",
            }
        )
        for task_id in candidate["amendment"]["affected_task_ids"]:
            tasks[task_id]["status"] = "blocked"
        replacements = []
        replacement_spec = [
            ("TASK-002", "TASK-007", "node-backend-repair", [], []),
            ("TASK-003", "TASK-008", "node-integrate-repair", ["TASK-001", "TASK-007"], []),
            (
                "TASK-004",
                "TASK-009",
                "node-quality-recheck",
                ["TASK-008"],
                ["TASK-001", "TASK-007", "TASK-008"],
            ),
            (
                "TASK-005",
                "TASK-010",
                "node-security-recheck",
                ["TASK-008"],
                ["TASK-001", "TASK-007", "TASK-008"],
            ),
            ("TASK-006", "TASK-011", "node-final-recheck", ["TASK-009", "TASK-010"], []),
        ]
        for source_id, task_id, node_id, dependencies, evidence_from in replacement_spec:
            replacement = copy.deepcopy(tasks[source_id])
            replacement["task_id"] = task_id
            replacement["graph_node_id"] = node_id
            replacement["title"] = f"Replacement for {tasks[source_id]['title']}"
            replacement["depends_on"] = dependencies
            replacement["evidence_from"] = evidence_from
            replacement["status"] = "pending"
            for criterion in replacement["acceptance_criteria"]:
                criterion["criterion_id"] = (
                    criterion["criterion_id"] + f"-r{task_id.removeprefix('TASK-')}"
                )
            replacements.append(replacement)
            requirement_id = replacement["requirement_refs"][0]
            requirement = next(
                item for item in candidate["requirements"]
                if item["requirement_id"] == requirement_id
            )
            requirement["task_refs"].append(task_id)
        candidate["tasks"].extend(replacements)
        amendment_path = self.approve_contract_value(
            candidate, "contract-amendment", self.contract_path
        )
        amendment_contract = plan_contract.load_json(
            amendment_path, "approved amendment"
        )
        amendment_graph = plan_contract.compile_graph_value(amendment_contract)
        amendment_acceptance_path = self.make_acceptance_contract(
            amendment_graph, amendment_path, "acceptance-contract-amendment"
        )

        result = agent_graph.command_amend(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-contract-amend",
                amendment=None,
                planning_contract_amendment=str(amendment_path),
                acceptance_contract_amendment=str(amendment_acceptance_path),
                triggered_by_node="node-backend",
                approval_ref=None,
                state_root=str(self.state_root),
            )
        )
        self.assertEqual(result["revision"], 2)
        self.assertEqual(
            result["deactivated_nodes"],
            ["node-backend", "node-final", "node-integrate", "node-quality", "node-security"],
        )
        state = agent_graph.load_state(run_dir)
        self.assertEqual(state["planning_contract"]["revision"], 2)
        self.assertEqual(state["nodes"]["node-backend"]["status"], "inactive")
        ready = agent_graph.command_ready(
            argparse.Namespace(run_id=run_id, state_root=str(self.state_root))
        )
        self.assertEqual(
            {item["node_id"] for item in ready["ready"]},
            {"node-frontend", "node-backend-repair"},
        )

    def test_amendment_cannot_invalidate_unrelated_sibling(self) -> None:
        run_id, run_dir = self.init_run()
        state = agent_graph.load_state(run_dir)
        del state["planning_contract"]
        agent_graph.atomic_write_json(run_dir / "state.json", state)
        self.complete(run_id, "node-frontend", "unrelated-trigger", "frontend/app.txt")
        replacement = node(
            "node-backend-unrelated-replacement",
            "work",
            "implementation",
            "fleet-run",
            [],
            ["backend/"],
            "detached-worktree",
        )
        amendment = {
            "schema_version": 1,
            "amendment_id": "amend-unrelated-sibling",
            "base_revision": 1,
            "reason": "Attempt to invalidate a sibling unrelated to the terminal trigger evidence.",
            "triggered_by_node": "node-frontend",
            "invalidate_nodes": ["node-backend"],
            "add_nodes": [replacement],
            "new_integration_node_id": None,
            "approval_scope": "technical-parent",
        }
        amendment_path = self.root / "unrelated-amendment.json"
        write_json(amendment_path, amendment)
        with self.assertRaisesRegex(agent_graph.GraphError, "upstream provenance"):
            agent_graph.command_amend(
                argparse.Namespace(
                    run_id=run_id,
                    operation_id="op-amend-unrelated",
                    amendment=str(amendment_path),
                    planning_contract_amendment=None,
                    triggered_by_node=None,
                    approval_ref=None,
                    state_root=str(self.state_root),
                )
            )

    def test_recover_marks_stale_running_node_without_retry(self) -> None:
        run_id, run_dir = self.init_run()
        self.start(run_id, "node-frontend", "op-start-recover")
        with agent_graph.run_lock(run_dir):
            state = agent_graph.load_state(run_dir)
            state["nodes"]["node-frontend"]["started_at"] = (
                datetime.now(timezone.utc) - timedelta(minutes=10)
            ).isoformat().replace("+00:00", "Z")
            agent_graph.atomic_write_json(run_dir / "state.json", state)
        recovered = agent_graph.command_recover(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-recover-stale",
                stale_after_seconds=30,
                state_root=str(self.state_root),
            )
        )
        self.assertEqual(recovered["recovery_required"], ["node-frontend"])
        state = agent_graph.load_state(run_dir)
        self.assertEqual(state["nodes"]["node-frontend"]["status"], "recovery-required")
        self.assertEqual(state["nodes"]["node-frontend"]["attempts"], 1)

    def test_append_only_amendment_replans_affected_subgraph(self) -> None:
        run_id, run_dir = self.init_run()
        state = agent_graph.load_state(run_dir)
        del state["planning_contract"]
        agent_graph.atomic_write_json(run_dir / "state.json", state)
        self.complete(run_id, "node-frontend", "amend-fe", "frontend/app.txt")
        self.complete(run_id, "node-backend", "amend-be", "backend/api.txt")
        self.complete(run_id, "node-integrate", "amend-integration", "frontend/integrated.txt")
        self.complete(run_id, "node-quality", "amend-quality")
        self.complete(run_id, "node-security", "amend-security", verdict="rejected")

        repair = node(
            "node-backend-repair",
            "work",
            "implementation",
            "fleet-run",
            [],
            ["backend/"],
            "detached-worktree",
            evidence_from=["node-backend", "node-security"],
            on_reject="retry-same-scope",
        )
        integrate = node(
            "node-integrate-repair",
            "work",
            "integration",
            "parent-local",
            ["node-frontend", "node-backend-repair"],
            ["frontend/", "backend/"],
            "integration-owner",
        )
        security = node(
            "node-security-recheck",
            "gate",
            "incremental-security",
            "fleet-security-gate",
            ["node-integrate-repair"],
            [],
            "read-only",
            evidence_from=["node-frontend", "node-backend-repair", "node-integrate-repair"],
        )
        quality = node(
            "node-quality-recheck",
            "gate",
            "independent-quality",
            "fleet-quality-gate",
            ["node-integrate-repair"],
            [],
            "read-only",
            evidence_from=["node-frontend", "node-backend-repair", "node-integrate-repair"],
        )
        final = node(
            "node-final-recheck",
            "gate",
            "final-acceptance",
            "parent-local",
            ["node-quality-recheck", "node-security-recheck"],
            [],
            "read-only",
        )
        amendment = {
            "schema_version": 1,
            "amendment_id": "amend-backend-security-defect",
            "base_revision": 1,
            "reason": "Incremental security evidence identifies a backend defect that invalidates the integrated candidate.",
            "triggered_by_node": "node-security",
            "invalidate_nodes": ["node-backend"],
            "add_nodes": [repair, integrate, quality, security, final],
            "new_integration_node_id": "node-integrate-repair",
            "approval_scope": "technical-parent",
        }
        amendment_path = self.root / "amendment.json"
        write_json(amendment_path, amendment)
        result = agent_graph.command_amend(
            argparse.Namespace(
                run_id=run_id,
                operation_id="op-amend-backend",
                amendment=str(amendment_path),
                planning_contract_amendment=None,
                triggered_by_node=None,
                approval_ref=None,
                state_root=str(self.state_root),
            )
        )
        self.assertEqual(result["revision"], 2)
        self.assertEqual(
            set(result["deactivated_nodes"]),
            {
                "node-backend",
                "node-integrate",
                "node-quality",
                "node-security",
                "node-final",
            },
        )
        state = agent_graph.load_state(run_dir)
        self.assertEqual(state["nodes"]["node-frontend"]["status"], "accepted")
        self.assertEqual(state["nodes"]["node-backend"]["status"], "inactive")
        ready = agent_graph.command_ready(
            argparse.Namespace(run_id=run_id, state_root=str(self.state_root))
        )
        self.assertEqual([item["node_id"] for item in ready["ready"]], ["node-backend-repair"])

    def test_reference_schemas_are_valid_json(self) -> None:
        references = MODULE_PATH.parents[1] / "references"
        for name in ("graph.schema.json", "evidence.schema.json", "amendment.schema.json"):
            value = json.loads((references / name).read_text(encoding="utf-8"))
            self.assertEqual(value["$schema"], "https://json-schema.org/draft/2020-12/schema")
        graph_schema = json.loads((references / "graph.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(graph_schema["$defs"]["globalBudget"]["properties"]["time_policy"]["default"], "estimate")
        self.assertEqual(graph_schema["$defs"]["nodeBudget"]["properties"]["tool_policy"]["default"], "estimate")


if __name__ == "__main__":
    unittest.main()
