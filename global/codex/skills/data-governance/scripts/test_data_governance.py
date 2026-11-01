from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("data_governance.py")
SPEC = importlib.util.spec_from_file_location("data_governance", MODULE_PATH)
assert SPEC and SPEC.loader
governance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(governance)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def evidence(path: Path, content: str) -> dict:
    payload = content.encode("utf-8")
    path.write_bytes(payload)
    return {
        "path": path.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "observed_at": governance.utc_now(),
    }


class DataGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.contract = {
            "schema_version": 1,
            "contract_schema_version": "1.0.0",
            "contract_id": "data-gov-customer-ledger",
            "status": "DRAFT",
            "project_slug": "fixture-app",
            "tier": "D3",
            "profile": "full",
            "change": {
                "summary": "Migrate the customer ledger without changing its approved purpose.",
                "materiality": "high-risk",
                "synthetic_only": False,
                "changed_surfaces": ["schema", "migration", "personal-data"],
            },
            "actors": {
                "business_owner": "owner:mateusz",
                "data_steward": "role:data-steward",
                "legal_dpo": {
                    "status": "AVAILABLE",
                    "identity": "role:legal-dpo",
                    "authority_source_ref": "source-gdpr",
                },
            },
            "planning_contract": {
                "contract_id": "plan-fixture-app",
                "contract_hash": "a" * 64,
                "requirement_refs": ["REQ-001", "REQ-002"],
            },
            "triggers": ["persistent-data", "personal-data", "migration"],
            "sources": [
                {
                    "source_id": "source-owner-policy",
                    "kind": "owner-decision",
                    "reference": "decision:fixture-retention-v1",
                    "version_or_commit": "v1",
                    "observed_at": "2026-08-28T00:00:00Z",
                },
                {
                    "source_id": "source-gdpr",
                    "kind": "primary-standard",
                    "reference": "https://eur-lex.europa.eu/eli/reg/2016/679/oj",
                    "version_or_commit": "consolidated-current-check",
                    "observed_at": "2026-08-28T00:00:00Z",
                },
            ],
            "assets": [
                {
                    "asset_id": "asset-customer-ledger",
                    "name": "customer_ledger",
                    "kind": "table",
                    "owner": "team:product",
                    "steward": "role:data-steward",
                    "system_of_record": True,
                    "classification": "restricted",
                    "personal_data": True,
                    "special_category_data": False,
                    "environments": ["DEV", "STAGING", "PROD"],
                    "consumers": ["service:billing-api"],
                }
            ],
            "purposes": [
                {
                    "purpose_id": "purpose-billing",
                    "asset_refs": ["asset-customer-ledger"],
                    "description": "Maintain the customer billing ledger.",
                    "allowed_uses": ["billing", "customer support"],
                    "prohibited_uses": ["advertising enrichment"],
                    "decision": {
                        "status": "DECIDED",
                        "owner": "owner:mateusz",
                        "source_ref": "source-owner-policy",
                        "verified_at": "2026-08-28T00:00:00Z",
                    },
                }
            ],
            "lineage": {
                "sources": ["api:customer-service"],
                "transformations": ["normalize billing identity"],
                "outputs": ["asset-customer-ledger", "report:billing-summary"],
            },
            "data_contract": {
                "version": "1.0.0",
                "compatibility": "backward",
                "fields": [
                    {
                        "asset_ref": "asset-customer-ledger",
                        "name": "customer_id",
                        "type": "uuid",
                        "nullable": False,
                        "description": "Stable internal customer identifier.",
                        "classification": "confidential",
                    },
                    {
                        "asset_ref": "asset-customer-ledger",
                        "name": "email_hash",
                        "type": "text",
                        "nullable": False,
                        "description": "Pseudonymous lookup key.",
                        "classification": "restricted",
                    },
                ],
            },
            "access_controls": [
                {
                    "control_id": "control-ledger-read",
                    "asset_refs": ["asset-customer-ledger"],
                    "principal": "role:billing-service",
                    "privileges": ["select", "insert", "update"],
                    "scope": "row-level",
                    "justification": "Billing service owns customer ledger writes.",
                    "reviewer": "role:security-reviewer",
                }
            ],
            "quality_checks": [
                {
                    "check_id": "quality-customer-id-unique",
                    "asset_refs": ["asset-customer-ledger"],
                    "dimension": "uniqueness",
                    "method": "count customer_id equals count distinct customer_id",
                    "threshold": "100 percent",
                    "severity": "blocking",
                    "owner": "role:data-steward",
                    "evidence_kind": "deterministic-check",
                },
                {
                    "check_id": "quality-ledger-reconcile",
                    "asset_refs": ["asset-customer-ledger"],
                    "dimension": "reconciliation",
                    "method": "source and target row counts and approved aggregates match",
                    "threshold": "zero unexplained variance",
                    "severity": "blocking",
                    "owner": "role:data-steward",
                    "evidence_kind": "deterministic-check",
                },
            ],
            "retention": {
                "status": "DECIDED",
                "policy": "Delete after the owner-approved business and legal period.",
                "decision_owner": "owner:mateusz",
                "source_ref": "source-owner-policy",
                "verified_at": "2026-08-28T00:00:00Z",
                "legal_hold_behavior": "Suspend deletion only for an approved hold.",
                "targets": [
                    {
                        "asset_ref": "asset-customer-ledger",
                        "stores": ["primary", "cache", "backup", "export"],
                        "deletion_verification": "tombstone plus restore-sample check",
                    }
                ],
            },
            "privacy": {
                "review_required": True,
                "lawful_basis": {
                    "status": "DECIDED",
                    "decision_owner": "role:legal-dpo",
                    "source_ref": "source-gdpr",
                    "verified_at": "2026-08-28T00:00:00Z",
                },
                "dpia": {
                    "status": "APPROVED",
                    "decision_owner": "role:legal-dpo",
                    "source_ref": "source-gdpr",
                    "verified_at": "2026-08-28T00:00:00Z",
                },
                "transfer": {
                    "status": "NOT_APPLICABLE",
                    "decision_owner": "role:legal-dpo",
                    "source_ref": "source-owner-policy",
                    "verified_at": "2026-08-28T00:00:00Z",
                    "rationale": "No transfer outside the approved processing region.",
                },
            },
            "migration": {
                "required": True,
                "strategy": "expand-migrate-contract",
                "destructive": False,
                "environments": ["DEV", "STAGING", "PROD"],
                "rehearsal": "Production-sized synthetic rehearsal in staging.",
                "checkpoint": "Immutable pre-cutover checkpoint identifier.",
                "backup": "Verified database backup before cutover.",
                "restore_test": "Restore into isolated staging and compare checksums.",
                "rollback": "Switch readers back before contract phase.",
                "roll_forward": "Resume idempotent backfill from checkpoint.",
                "validation_check_refs": [
                    "quality-customer-id-unique",
                    "quality-ledger-reconcile",
                ],
                "cutover_approval": {
                    "status": "APPROVED",
                    "owner": "owner:mateusz",
                    "scope": "production-cutover",
                },
            },
            "gates": [
                {
                    "gate_id": "gate-data-contract",
                    "kind": "data-contract",
                    "required_before": "mutation",
                    "mandatory": True,
                    "applicability": "applicable",
                    "reviewer_policy": "independent",
                    "max_age_seconds": 86400,
                    "invalidate_on": ["schema", "purpose", "owner", "source-state"],
                    "evidence_subjects": ["contract-snapshot", "schema-state"],
                },
                {
                    "gate_id": "gate-privacy",
                    "kind": "privacy",
                    "required_before": "publish",
                    "mandatory": True,
                    "applicability": "applicable",
                    "reviewer_policy": "legal-dpo",
                    "max_age_seconds": 86400,
                    "invalidate_on": ["purpose", "classification", "legal-source"],
                    "evidence_subjects": ["privacy-decision", "access-enforcement"],
                },
                {
                    "gate_id": "gate-quality",
                    "kind": "quality-reconciliation",
                    "required_before": "close",
                    "mandatory": True,
                    "applicability": "applicable",
                    "reviewer_policy": "independent",
                    "max_age_seconds": 3600,
                    "invalidate_on": ["schema", "transform", "source-state", "code-revision"],
                    "evidence_subjects": ["dataset-state", "consumer-contract"],
                },
                {
                    "gate_id": "gate-migration",
                    "kind": "migration-readiness",
                    "required_before": "cutover",
                    "mandatory": True,
                    "applicability": "applicable",
                    "reviewer_policy": "independent",
                    "max_age_seconds": 3600,
                    "invalidate_on": ["schema", "migration-plan", "source-state", "backup"],
                    "evidence_subjects": ["restore-run", "rehearsal-run", "cutover-plan"],
                },
            ],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seal(self, contract: dict | None = None) -> dict:
        return governance.seal_contract(copy.deepcopy(contract or self.contract), "2026-08-28T01:00:00Z")

    def test_doctor_reports_version(self) -> None:
        result = governance.doctor()
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["version"], "0.1.0")

    def test_valid_contract_seals_and_validates(self) -> None:
        ready = self.seal()
        governance.validate_contract(ready, "ready")
        self.assertEqual(ready["status"], "READY")
        self.assertRegex(ready["contract_hash"], r"^[a-f0-9]{64}$")

    def test_change_classifier_has_d0_d1_and_d3_paths(self) -> None:
        base = {
            "schema_version": 1,
            "project_slug": "fixture-app",
            "summary": "Bounded data change",
            "synthetic_only": True,
            "persistent_write": False,
            "schema_change": "none",
            "new_consumer": False,
            "published_metric_change": False,
            "personal_or_restricted": False,
            "cross_system": False,
            "access_change": False,
            "migration_or_backfill": False,
            "retention_or_delete": False,
            "production": False,
        }
        d0 = governance.classify_change(base)
        self.assertEqual((d0["tier"], d0["activation"]), ("D0", "SKIP"))
        d1_input = copy.deepcopy(base)
        d1_input.update({"synthetic_only": False, "persistent_write": True})
        d1 = governance.classify_change(d1_input)
        self.assertEqual((d1["tier"], d1["activation"]), ("D1", "FAST_PATH"))
        d3_input = copy.deepcopy(d1_input)
        d3_input["personal_or_restricted"] = True
        d3 = governance.classify_change(d3_input)
        self.assertEqual((d3["tier"], d3["activation"]), ("D3", "FULL"))

    def test_classifier_fails_closed_on_missing_signal(self) -> None:
        with self.assertRaisesRegex(governance.GovernanceError, "must be boolean"):
            governance.classify_change(
                {
                    "schema_version": 1,
                    "project_slug": "fixture-app",
                    "summary": "Incomplete assessment",
                }
            )

    def test_personal_data_requires_d3(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["tier"] = "D2"
        candidate["change"]["materiality"] = "material"
        with self.assertRaisesRegex(governance.GovernanceError, "D3"):
            governance.validate_contract(candidate, "draft")

    def test_contract_schema_version_and_profile_are_enforced(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["contract_schema_version"] = "2.0.0"
        with self.assertRaisesRegex(governance.GovernanceError, "contract_schema_version"):
            governance.validate_contract(candidate, "draft")
        candidate = copy.deepcopy(self.contract)
        candidate["profile"] = "compact"
        with self.assertRaisesRegex(governance.GovernanceError, "full"):
            governance.validate_contract(candidate, "draft")

    def test_d1_compact_omits_privacy_migration_and_detailed_access(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["contract_id"] = "data-gov-internal-cache-index"
        candidate["tier"] = "D1"
        candidate["profile"] = "compact"
        candidate["change"] = {
            "summary": "Add a compatible internal lookup index.",
            "materiality": "low",
            "synthetic_only": False,
            "changed_surfaces": ["storage"],
        }
        candidate["triggers"] = ["persistent-data"]
        candidate["assets"][0].update(
            {
                "classification": "internal",
                "personal_data": False,
                "special_category_data": False,
                "consumers": [],
            }
        )
        for field in candidate["data_contract"]["fields"]:
            field["classification"] = "internal"
        candidate["actors"]["legal_dpo"] = {"status": "NOT_APPLICABLE"}
        candidate["access_controls"] = []
        candidate.pop("privacy")
        candidate.pop("migration")
        candidate["gates"] = [candidate["gates"][0], candidate["gates"][2]]
        ready = self.seal(candidate)
        governance.validate_contract(ready, "ready")
        fragment = governance.compile_graph_fragment(ready)
        self.assertEqual(
            [node["node_key"] for node in fragment["nodes"]],
            ["data-contract", "quality-reconciliation", "data-acceptance"],
        )

    def test_asset_requires_owner_and_steward(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["assets"][0]["steward"] = ""
        with self.assertRaisesRegex(governance.GovernanceError, "steward"):
            governance.validate_contract(candidate, "draft")

    def test_migration_requires_restore_recovery_and_reconciliation(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["migration"]["restore_test"] = ""
        with self.assertRaisesRegex(governance.GovernanceError, "restore_test"):
            governance.validate_contract(candidate, "draft")
        candidate = copy.deepcopy(self.contract)
        candidate["migration"]["validation_check_refs"] = ["quality-customer-id-unique"]
        with self.assertRaisesRegex(governance.GovernanceError, "reconciliation"):
            governance.validate_contract(candidate, "draft")

    def test_sensitive_values_and_record_payloads_are_rejected(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["database_password"] = "not-a-real-secret"
        with self.assertRaisesRegex(governance.GovernanceError, "sensitive"):
            governance.validate_contract(candidate, "draft")
        candidate = copy.deepcopy(self.contract)
        candidate["sample_records"] = [{"email": "person@example.invalid"}]
        with self.assertRaisesRegex(governance.GovernanceError, "record payload"):
            governance.validate_contract(candidate, "draft")

    def test_compile_fragment_is_bound_to_contract_and_has_required_gates(self) -> None:
        ready = self.seal()
        fragment = governance.compile_graph_fragment(ready)
        self.assertEqual(fragment["contract_hash"], ready["contract_hash"])
        keys = {node["node_key"] for node in fragment["nodes"]}
        self.assertEqual(
            keys,
            {
                "data-contract",
                "privacy-access",
                "migration-readiness",
                "quality-reconciliation",
                "data-acceptance",
            },
        )
        governance.verify_graph_fragment(ready, fragment)

    def test_non_applicable_gate_requires_rationale(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["gates"][1]["applicability"] = "not-applicable"
        with self.assertRaisesRegex(governance.GovernanceError, "rationale"):
            governance.validate_contract(candidate, "draft")

    def test_closeout_requires_independent_review_and_exact_evidence(self) -> None:
        ready = self.seal()
        results = []
        for index, gate in enumerate(ready["gates"], start=1):
            item = evidence(self.root / f"gate-{index}.txt", f"PASS {gate['gate_id']}")
            item["subject_kind"] = gate["evidence_subjects"][0]
            item["subject_sha256"] = hashlib.sha256(
                f"subject:{gate['gate_id']}".encode("utf-8")
            ).hexdigest()
            results.append(
                {
                    "gate_id": gate["gate_id"],
                    "status": "PASS",
                    "reviewer": {
                        "id": f"reviewer-{index}",
                        "lineage": f"lab-{index}",
                    },
                    "evidence": [item],
                }
            )
        closeout = {
            "contract_id": ready["contract_id"],
            "contract_hash": ready["contract_hash"],
            "operation_id": "op-data-closeout-001",
            "maker": {"id": "maker-one", "lineage": "lab-maker"},
            "results": results,
        }
        receipt = governance.closeout(ready, closeout, self.root)
        self.assertEqual(receipt["status"], "ACCEPTED")
        self.assertEqual(receipt["assurance_scope"], "artifact-integrity-and-independent-review")
        governance.verify_receipt(ready, receipt)

        bad = copy.deepcopy(closeout)
        bad["results"][0]["reviewer"] = copy.deepcopy(bad["maker"])
        with self.assertRaisesRegex(governance.GovernanceError, "independent"):
            governance.closeout(ready, bad, self.root)

    def test_closeout_preserves_blocked_result(self) -> None:
        ready = self.seal()
        results = []
        for index, gate in enumerate(ready["gates"], start=1):
            item = evidence(self.root / f"blocked-{index}.txt", gate["gate_id"])
            item["subject_kind"] = gate["evidence_subjects"][0]
            item["subject_sha256"] = hashlib.sha256(
                f"subject:{gate['gate_id']}".encode("utf-8")
            ).hexdigest()
            results.append(
                {
                    "gate_id": gate["gate_id"],
                    "status": "PASS" if index > 1 else "BLOCKED",
                    "reviewer": {"id": f"reviewer-{index}", "lineage": f"lab-{index}"},
                    "evidence": [item],
                    "blocker": "Owner decision or remediation is still required" if index == 1 else None,
                }
            )
        receipt = governance.closeout(
            ready,
            {
                "contract_id": ready["contract_id"],
                "contract_hash": ready["contract_hash"],
                "operation_id": "op-data-closeout-002",
                "maker": {"id": "maker-one", "lineage": "lab-maker"},
                "results": results,
            },
            self.root,
        )
        self.assertEqual(receipt["status"], "NOT_ACCEPTED")
        self.assertEqual(receipt["blocked_gates"], ["gate-data-contract"])

    def test_pending_privacy_decision_cannot_close_as_pass(self) -> None:
        candidate = copy.deepcopy(self.contract)
        candidate["privacy"]["lawful_basis"]["status"] = "PENDING"
        ready = self.seal(candidate)
        results = []
        for index, gate in enumerate(ready["gates"], start=1):
            item = evidence(self.root / f"pending-{index}.txt", gate["gate_id"])
            item["subject_kind"] = gate["evidence_subjects"][0]
            item["subject_sha256"] = hashlib.sha256(gate["gate_id"].encode()).hexdigest()
            results.append(
                {
                    "gate_id": gate["gate_id"],
                    "status": "PASS",
                    "reviewer": {"id": f"reviewer-{index}", "lineage": f"lab-{index}"},
                    "evidence": [item],
                }
            )
        with self.assertRaisesRegex(governance.GovernanceError, "PENDING"):
            governance.closeout(
                ready,
                {
                    "contract_id": ready["contract_id"],
                    "contract_hash": ready["contract_hash"],
                    "operation_id": "op-data-closeout-003",
                    "maker": {"id": "maker-one", "lineage": "lab-maker"},
                    "results": results,
                },
                self.root,
            )


if __name__ == "__main__":
    unittest.main()
