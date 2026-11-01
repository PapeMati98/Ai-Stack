import importlib.util
import argparse
import copy
import json
import unittest
from pathlib import Path

import test_plan_contract as fixtures

plan = fixtures.plan_contract


def declare_metadata(contract):
    """Caller-authored declarations for the five-task delivery fixture."""
    edges = [
        {},
        {"TASK-001": {"kind": "write-conflict", "reason": "Integration modifies the feature files written by the maker", "input": "src/feature/"}},
        {"TASK-002": {"kind": "data/interface", "reason": "Quality review consumes the integrated feature artifact", "input": "crit-integration artifact"}},
        {"TASK-002": {"kind": "data/interface", "reason": "Security review consumes the integrated feature artifact", "input": "crit-integration artifact"}},
        {dep: {"kind": "gate", "reason": "Final acceptance requires independent review acceptance", "input": receipt}
         for dep, receipt in [("TASK-003", "crit-quality receipt"), ("TASK-004", "crit-security receipt")]},
    ]
    for task, declarations in zip(contract["tasks"], edges):
        task["dependency_justifications"] = declarations
    contract["graph"]["capacity_basis"] = {
        "kind": "owner-hard-cap", "value": 1,
        "reason": "Owner requests one concurrent worker for this fixture",
        "evidence": "intake.md: owner cap1 for all providers",
    }
    contract["planning"]["adapter_declarations"] = {
        task["task_id"]: ({"mode": "manual"} if task["capability"] == "final-acceptance"
                          else {"mode": "dispatch", "adapter": "fixture-command-adapter"})
        for task in contract["tasks"]
    }
    return contract


MODULE = Path(__file__).with_name("planning_checks.py")
SPEC = importlib.util.spec_from_file_location("planning_checks", MODULE)
assert SPEC and SPEC.loader
checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checks)


class Task011PlanningChecksTests(unittest.TestCase):
    def test_dependency_reason_requires_kind_reason_and_input(self):
        tasks = {"TASK-001": {"depends_on": ["TASK-000"], "dependency_justifications": {"TASK-000": {"kind": "gate", "reason": "gate result is required", "input": "quality receipt"}}}}
        checks.validate_dependency_justifications(tasks)
        tasks["TASK-001"]["dependency_justifications"]["TASK-000"].pop("input")
        with self.assertRaises(ValueError):
            checks.validate_dependency_justifications(tasks)

    def test_capacity_distinguishes_owner_cap_from_host_estimate(self):
        checks.validate_capacity_basis({"kind": "owner-hard-cap", "value": 1, "reason": "owner explicitly limited concurrency", "evidence": "intake.md:7"})
        checks.validate_capacity_basis({"kind": "host-capacity-estimate", "value": 4, "reason": "four CLI supervisors fit measured RAM headroom", "evidence": "host-observation-20260912"})
        checks.validate_capacity_basis({"kind": "owner-hard-cap", "value": 1, "reason": "owner explicitly limited concurrency", "evidence": "owner cap1 across all providers"})
        with self.assertRaises(ValueError):
            checks.validate_capacity_basis({"kind": "provider-count", "value": 2, "reason": "two providers configured", "evidence": "provider count"})

    def test_dispatch_requires_adapters_but_keeps_authority_manual(self):
        tasks = {
            "TASK-001": {"status": "pending", "kind": "work", "capability": "implementation", "executor": "fleet-run"},
            "TASK-002": {"status": "pending", "kind": "gate", "capability": "final-acceptance", "executor": "parent-local"},
        }
        result = checks.validate_adapter_declarations(tasks, {"TASK-001": {"mode": "dispatch", "adapter": "codex"}, "TASK-002": {"mode": "manual"}})
        self.assertEqual(result["TASK-002"]["mode"], "manual")
        with self.assertRaises(ValueError):
            checks.validate_adapter_declarations(tasks, {"TASK-001": {"mode": "dispatch", "adapter": "codex"}})

    def test_parallelism_report_is_observational(self):
        tasks = {
            "TASK-001": {"depends_on": [], "write_scope": ["src/a"]},
            "TASK-002": {"depends_on": ["TASK-001"], "write_scope": ["src/b"]},
        }
        before = list(tasks["TASK-002"]["depends_on"])
        report = checks.parallelism_report(tasks)
        self.assertEqual(report["status"], "REPORT_ONLY")
        self.assertFalse(report["dependencies_changed"])
        self.assertEqual(tasks["TASK-002"]["depends_on"], before)
        self.assertEqual(report["suspicious_serial_edges"][0]["from"], "TASK-001")

    def test_parent_local_dispatch_and_reasoned_manual_override(self):
        tasks = {"TASK-001": {"status": "pending", "kind": "work", "capability": "implementation", "executor": "parent-local"}}
        for declaration in ({"mode": "dispatch", "adapter": "command-adapter"},
                            {"mode": "manual", "reason": "Operator must inspect a local hardware indicator"}):
            self.assertEqual(checks.validate_adapter_declarations(tasks, {"TASK-001": declaration})["TASK-001"], declaration)
        for declaration in ({"mode": "manual"}, {"mode": "manual", "reason": "        "}, {"mode": "dispatch", "adapter": ""}):
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                checks.validate_adapter_declarations(tasks, {"TASK-001": declaration})

    def test_human_and_authority_cannot_dispatch(self):
        for kind, executor, capability in [("human", "human", "contract-definition"),
                                           ("gate", "parent-local", "final-acceptance"),
                                           ("gate", "parent-local", "production-approval"),
                                           ("gate", "parent-local", "deep-security-approval")]:
            tasks = {"TASK-001": dict(status="pending", kind=kind, executor=executor, capability=capability)}
            with self.subTest(capability=capability), self.assertRaises(ValueError):
                checks.validate_adapter_declarations(tasks, {"TASK-001": {"mode": "dispatch", "adapter": "command"}})
            checks.validate_adapter_declarations(tasks, {"TASK-001": {"mode": "manual"}})

    def test_report_accepts_all_justified_edges_and_flags_only_redundancy(self):
        tasks = {"TASK-001": {"depends_on": []}, "TASK-002": {"depends_on": ["TASK-001"]},
                 "TASK-003": {"depends_on": ["TASK-001", "TASK-002"]}}
        for kind in ("data/interface", "write-conflict", "gate"):
            for task in tasks.values():
                task["dependency_justifications"] = {dep: {"kind": kind, "reason": "Consumes a required artifact", "input": "interface-v1.json"} for dep in task["depends_on"]}
            before = copy.deepcopy(tasks)
            warnings = checks.parallelism_report(tasks)["suspicious_serial_edges"]
            self.assertEqual([(row["from"], row["to"]) for row in warnings], [("TASK-001", "TASK-003")])
            self.assertIn("transitive", warnings[0]["reason"])
            self.assertEqual(before, tasks)

    def test_report_invalid_justifications_even_for_read_only_tasks(self):
        for justification in (None, [], {}, {"kind": "data/interface", "reason": "        ", "input": "schema"}):
            tasks = {"TASK-001": {"depends_on": []}, "TASK-002": {"depends_on": ["TASK-001"], "dependency_justifications": {"TASK-001": justification}}}
            before = copy.deepcopy(tasks)
            self.assertEqual(len(checks.parallelism_report(tasks)["suspicious_serial_edges"]), 1)
            self.assertEqual(tasks, before)

    def test_report_rejects_unknown_refs_cycles_and_oversized_dags(self):
        for tasks in ({"TASK-001": {"depends_on": ["missing"]}},
                      {"TASK-001": {"depends_on": ["TASK-002"]}, "TASK-002": {"depends_on": ["TASK-001"]}},
                      {str(i): {"depends_on": []} for i in range(129)}):
            with self.assertRaises(ValueError):
                checks.parallelism_report(tasks)


class PreparedPlanningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PlanContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.draft = copy.deepcopy(self.fixture.draft)
        self.draft["planning"]["review_history"] = []
        for gate in self.draft["gates"].values():
            gate.update(status="pending", auditor=None, reviewed_prompt_sha256=None,
                        reviewed_plan_sha256=None, reviewed_clause_ids=[], reviewed_requirement_ids=[],
                        reviewed_task_ids=[], attested_hard_risk_flags=[], evidence=None)

    def prepare(self, draft):
        source, output = self.root / "new.json", self.root / "prepared.json"
        fixtures.write_json(source, draft)
        result = plan.command_prepare_draft(argparse.Namespace(contract=str(source), output=str(output)))
        self.assertEqual(result["status"], "DRAFT_PREPARED")
        self.assertEqual(json.loads(source.read_text()), draft)
        return json.loads(output.read_text())

    def test_prepare_activates_policy_preserves_caller_intent_and_cap1(self):
        declare_metadata(self.draft)
        self.draft["tasks"][0]["title"] = "Interface ready, implementation still pending"
        prepared = self.prepare(self.draft)
        self.assertEqual(prepared["planning"]["preparation_policy"], "parallel-planning-v1")
        self.assertEqual(prepared["graph"]["capacity_basis"], self.draft["graph"]["capacity_basis"])
        self.assertEqual(prepared["planning"]["adapter_declarations"], self.draft["planning"]["adapter_declarations"])
        for before, after in zip(self.draft["tasks"], prepared["tasks"]):
            self.assertEqual(before["depends_on"], after["depends_on"])
            self.assertEqual(before["dependency_justifications"], after["dependency_justifications"])
        plan.validate_contract(prepared, "draft")

    def test_prepare_requires_caller_metadata_and_writes_nothing_on_failure(self):
        for missing in ("dependency_justifications", "capacity_basis", "adapter_declarations"):
            draft = declare_metadata(copy.deepcopy(self.draft))
            target = draft["tasks"][1] if missing == "dependency_justifications" else draft["graph"] if missing == "capacity_basis" else draft["planning"]
            del target[missing]
            with self.subTest(missing=missing), self.assertRaises(plan.ContractError):
                self.prepare(draft)
            self.assertFalse((self.root / "prepared.json").exists())

    def test_prepare_rejects_invalid_supplied_metadata_without_fabrication(self):
        mutations = [lambda c: c["tasks"][1]["dependency_justifications"]["TASK-001"].update(reason="        "),
                     lambda c: c["tasks"][1]["dependency_justifications"]["TASK-001"].pop("input"),
                     lambda c: c["graph"]["capacity_basis"].update(evidence=""),
                     lambda c: c["graph"].update(capacity_basis=None),
                     lambda c: c["planning"].update(preparation_policy="unknown-v9"),
                     lambda c: c["planning"]["adapter_declarations"].update({"TASK-001": {"mode": "manual"}})]
        for mutate in mutations:
            draft = declare_metadata(copy.deepcopy(self.draft))
            mutate(draft)
            before = copy.deepcopy(draft)
            with self.assertRaises(plan.ContractError):
                self.prepare(draft)
            self.assertFalse((self.root / "prepared.json").exists())
            self.assertEqual(draft, before)

    def test_opted_in_validation_requires_metadata_before_approval(self):
        draft = declare_metadata(copy.deepcopy(self.draft))
        draft["planning"]["preparation_policy"] = "parallel-planning-v1"
        for field in ("dependency_justifications", "capacity_basis", "adapter_declarations"):
            broken = copy.deepcopy(draft)
            target = broken["tasks"][0] if field == "dependency_justifications" else broken["graph"] if field == "capacity_basis" else broken["planning"]
            target[field] = None
            with self.subTest(field=field), self.assertRaises(plan.ContractError):
                plan.validate_contract(broken, "draft")

    def test_prepare_does_not_migrate_reviewed_draft_or_revision2(self):
        for draft in (self.fixture.draft, {**self.draft, "revision": 2}):
            with self.assertRaisesRegex(plan.ContractError, "unreviewed"):
                self.prepare(draft)
            self.assertFalse((self.root / "prepared.json").exists())

    def test_legacy_hash_and_review_history_are_unchanged(self):
        contract = self.fixture.draft
        before = copy.deepcopy(contract)
        expected = {field: contract[field] for field in ("prompt", "classification", "clauses", "requirements", "questions", "assumptions", "out_of_scope", "decisions", "tasks", "owner_ceiling", "graph")}
        self.assertEqual(plan.planning_payload_hash(contract), plan.sha256_value(expected))
        plan.validate_contract(contract, "draft")
        plan.verify_gate_approval(contract)
        self.assertEqual(before, contract)

    def test_optional_declarations_are_bound_even_without_preparation_policy(self):
        contract = copy.deepcopy(self.fixture.draft)
        contract["planning"]["adapter_declarations"] = declare_metadata(copy.deepcopy(contract))["planning"]["adapter_declarations"]
        before = plan.planning_payload_hash(contract)
        contract["planning"]["adapter_declarations"]["TASK-001"]["adapter"] = "changed-adapter"
        self.assertNotEqual(before, plan.planning_payload_hash(contract))

    def test_every_new_metadata_change_changes_hash_and_rejects_old_review(self):
        contract = declare_metadata(copy.deepcopy(self.fixture.draft))
        contract["planning"]["preparation_policy"] = "parallel-planning-v1"
        contract["tasks"][0]["interface_contract"] = {"status": "ready", "evidence": "schema-v1.json"}
        reviewed = plan.planning_payload_hash(contract)
        for gate in contract["gates"].values():
            gate["reviewed_plan_sha256"] = reviewed
        mutations = [lambda c: c["planning"]["adapter_declarations"]["TASK-001"].update(adapter="another-adapter"),
                     lambda c: c["planning"].pop("preparation_policy"),
                     lambda c: c["graph"]["capacity_basis"].update(value=2),
                     lambda c: c["tasks"][1]["dependency_justifications"]["TASK-001"].update(input="src/new/"),
                     lambda c: c["tasks"][0]["interface_contract"].update(status="implemented")]
        for mutate in mutations:
            changed = copy.deepcopy(contract)
            mutate(changed)
            self.assertNotEqual(reviewed, plan.planning_payload_hash(changed))
            with self.assertRaisesRegex(plan.ContractError, "different planning payload"):
                plan.verify_gate_approval(changed)

    def test_dispatch_preparation_rejects_changed_bound_declarations(self):
        contract = declare_metadata(copy.deepcopy(self.draft))
        declarations = contract["planning"]["adapter_declarations"]
        self.assertFalse(plan.prepare_dispatch(contract, declarations)["executable"])
        changed = copy.deepcopy(declarations)
        changed["TASK-001"]["adapter"] = "unreviewed-adapter"
        with self.assertRaisesRegex(plan.ContractError, "declarations differ"):
            plan.prepare_dispatch(contract, changed)

    def test_report_command_can_inspect_invalid_justifications(self):
        draft = declare_metadata(copy.deepcopy(self.draft))
        draft["tasks"][1]["dependency_justifications"]["TASK-001"]["input"] = ""
        path = self.root / "inspect.json"
        fixtures.write_json(path, draft)
        before = path.read_bytes()
        report = plan.command_report_parallelism(argparse.Namespace(contract=str(path)))
        self.assertEqual(report["status"], "REPORT_ONLY")
        self.assertTrue(any(row["to"] == "TASK-002" for row in report["suspicious_serial_edges"]))
        self.assertEqual(before, path.read_bytes())

    def test_prepared_full_combined_approval_and_tamper_rejection(self):
        import test_full_combined_policy as combined

        fixture = combined.FullCombinedPolicyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.draft["planning"]["review_policy"] = "full-combined-v1"
        declare_metadata(self.draft)
        self.draft["planning"]["adapter_declarations"]["TASK-002"] = {
            "mode": "manual", "reason": "Operator must inspect integration on local hardware"}
        fixture.contract = self.prepare(self.draft)
        for gate in fixture.contract["gates"].values():
            gate["status"] = "pass"
        fixture.bind_review()
        fixture.contract = fixture.ingest({"gate": "combined", "result": fixture.contract["gates"]})
        approved = fixture.approve()
        plan.validate_approved_contract(approved)
        before = copy.deepcopy(approved)
        requirements = plan.dispatch_requirements(approved)
        self.assertEqual(requirements["status"], "DECLARED")
        self.assertEqual(requirements["contract_hash"], approved["approval"]["contract_hash"])
        self.assertEqual(requirements["graph_sha256"], approved["graph"]["projection_sha256"])
        self.assertEqual(requirements["required_dispatch_node_ids"], ["node-implement", "node-quality", "node-security"])
        self.assertEqual(list(requirements["manual_declarations"]), ["node-integrate", "node-final"])
        self.assertEqual(requirements["manual_declarations"]["node-integrate"]["reason"],
                         self.draft["planning"]["adapter_declarations"]["TASK-002"]["reason"])
        self.assertEqual(requirements["dispatch_declarations"]["node-implement"],
                         approved["planning"]["adapter_declarations"]["TASK-001"])
        self.assertEqual(requirements["capacity_basis"], approved["graph"]["capacity_basis"])
        self.assertEqual(requirements["undeclared_node_ids"], [])
        self.assertEqual(requirements, plan.dispatch_requirements(approved))
        requirements["capacity_basis"]["value"] = 16
        requirements["manual_declarations"]["node-integrate"]["reason"] = "changed"
        self.assertEqual(before, approved)
        projection = plan.compile_graph_value(approved)
        fixtures.agent_graph.validate_graph(projection)
        self.assertNotIn("capacity_basis", projection)
        self.assertNotIn("adapter_declarations", projection)
        for field in ("adapter_declarations", "preparation_policy"):
            broken = copy.deepcopy(approved)
            if field == "adapter_declarations":
                broken["planning"][field]["TASK-001"]["adapter"] = "replacement"
            else:
                del broken["planning"][field]
            with self.assertRaises(plan.ContractError):
                plan.validate_approved_contract(broken)

    def test_dispatch_requirements_legacy_is_explicitly_undeclared(self):
        approved, _ = self.fixture.approve()
        before = copy.deepcopy(approved)
        result = plan.dispatch_requirements(approved)
        self.assertEqual(result["status"], "LEGACY_UNDECLARED")
        self.assertIsNone(result["capacity_basis"])
        self.assertEqual(result["required_dispatch_node_ids"], [])
        self.assertEqual(result["manual_declarations"], {})
        self.assertEqual(result["undeclared_node_ids"], [t["graph_node_id"] for t in approved["tasks"]])
        self.assertEqual(approved, before)
        with self.assertRaises(plan.ContractError):
            plan.dispatch_requirements(self.draft)


if __name__ == "__main__":
    unittest.main()
