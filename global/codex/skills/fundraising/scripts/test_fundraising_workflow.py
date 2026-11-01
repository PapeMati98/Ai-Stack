from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.dont_write_bytecode = True

import fundraising_workflow as workflow


class FundraisingWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="fundraising workflow ")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    @staticmethod
    def write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def build_valid_campaign(self) -> None:
        evidence = self.root / "evidence" / "verified.txt"
        evidence.parent.mkdir()
        evidence.write_text("Synthetic company evidence only.\n", encoding="utf-8")
        digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
        decisions = {
            key: {"status": "approved", "value": f"approved-{key}"}
            for key in sorted(workflow.REQUIRED_DECISIONS)
        }
        campaign = {
            "schema_version": 1,
            "campaign_id": "campaign-synthetic",
            "company": {
                "name": "Synthetic Ventures",
                "project_slug": "synthetic-ventures",
                "continuity_status": "VERIFIED",
                "data_classification": "D1",
            },
            "mode": "create",
            "artifact_types": ["deck", "memorandum", "one-pager"],
            "owner_decisions": decisions,
            "provider_policy": {
                "allowed_providers": ["synthetic-local"],
                "selected_providers": ["synthetic-local"],
                "governance_contract_hash": None,
            },
            "ledgers": {
                "sources": "sources.json",
                "claims": "claims.json",
                "numbers": "numbers.json",
            },
            "content_lineage": {"maker": "maker-content", "reviewer": "reviewer-content"},
            "design_lineage": {"maker": "maker-design", "reviewer": "reviewer-design"},
            "repair_budget": {"max_rounds": 2, "used_rounds": 0},
            "distribution_status": "not_authorized",
        }
        sources = {
            "schema_version": 1,
            "sources": [
                {
                    "source_id": "source-evidence",
                    "title": "Synthetic verified evidence",
                    "source_type": "owner_record",
                    "owner": "synthetic-owner",
                    "observed_at": "2026-08-30",
                    "expires_on": "2099-12-31",
                    "classification": "internal",
                    "evidence_ref": "evidence/verified.txt",
                    "sha256": digest,
                    "verification_status": "verified",
                    "allowed_providers": ["synthetic-local"],
                }
            ],
        }
        claims = {
            "schema_version": 1,
            "claims": [
                {
                    "claim_id": "claim-proof",
                    "text": "Synthetic evidence supports this synthetic claim.",
                    "claim_kind": "fact",
                    "status": "verified",
                    "source_ids": ["source-evidence"],
                    "artifact_scopes": ["deck", "memorandum", "one-pager"],
                    "classification": "internal",
                    "owner": "synthetic-owner",
                }
            ],
        }
        rounding = {"places": 0, "mode": "HALF_EVEN"}
        numbers = {
            "schema_version": 1,
            "numbers": [
                {
                    "number_id": "number-revenue",
                    "kind": "input",
                    "status": "verified",
                    "value": "120000",
                    "unit": "currency",
                    "currency": "PLN",
                    "period": "synthetic-year",
                    "source_ids": ["source-evidence"],
                    "rounding": rounding,
                    "tolerance": "0",
                    "usages": [{"artifact": "deck", "display_value": "120000"}],
                },
                {
                    "number_id": "number-customers",
                    "kind": "input",
                    "status": "verified",
                    "value": "100",
                    "unit": "customers",
                    "period": "synthetic-year",
                    "source_ids": ["source-evidence"],
                    "rounding": rounding,
                    "tolerance": "0",
                    "usages": [{"artifact": "memorandum", "display_value": "100"}],
                },
                {
                    "number_id": "number-arpu",
                    "kind": "derived",
                    "status": "verified",
                    "value": "1200",
                    "unit": "currency-per-customer",
                    "currency": "PLN",
                    "period": "synthetic-year",
                    "formula": "revenue / customers",
                    "inputs": {"revenue": "number-revenue", "customers": "number-customers"},
                    "rounding": rounding,
                    "tolerance": "0",
                    "usages": [
                        {"artifact": "deck", "display_value": "1200"},
                        {"artifact": "one-pager", "display_value": "1200"},
                    ],
                },
            ],
        }
        self.write_json(self.root / "campaign.json", campaign)
        self.write_json(self.root / "sources.json", sources)
        self.write_json(self.root / "claims.json", claims)
        self.write_json(self.root / "numbers.json", numbers)

    def mutate_json(self, name: str, callback: object) -> None:
        path = self.root / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        callback(payload)
        self.write_json(path, payload)

    def test_valid_campaign_is_ready(self) -> None:
        self.build_valid_campaign()
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertEqual(report.status, "READY", report.output())
        self.assertEqual(report.state["number_count"], 3)

    def test_init_is_fail_closed_without_continuity_or_evidence(self) -> None:
        target = self.root / "new"
        args = argparse.Namespace(
            campaign_dir=str(target),
            campaign_id="campaign-globex",
            company_name="Globex",
            project_slug="globex",
            artifacts="deck,memorandum,one-pager",
            mode="create",
        )
        self.assertEqual(workflow.init_campaign(args), 0)
        report = workflow.validate_campaign(target, today=date(2026, 8, 30))
        self.assertEqual(report.status, "BLOCKED")
        self.assertIn("project continuity is MISSING", report.blockers)
        self.assertIn("no verified claims", report.blockers)

    def test_unsupported_verified_claim_is_rejected(self) -> None:
        self.build_valid_campaign()
        self.mutate_json("claims.json", lambda p: p["claims"][0].update(source_ids=[]))
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertEqual(report.status, "INVALID")
        self.assertTrue(any("lacks evidence" in item for item in report.errors))

    def test_stale_source_is_rejected(self) -> None:
        self.build_valid_campaign()
        self.mutate_json("sources.json", lambda p: p["sources"][0].update(expires_on="2020-01-01"))
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertTrue(any("is stale" in item for item in report.errors))

    def test_calculation_mismatch_is_rejected(self) -> None:
        self.build_valid_campaign()
        self.mutate_json("numbers.json", lambda p: p["numbers"][2].update(value="999"))
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertTrue(any("canonical calculation" in item for item in report.errors))

    def test_cross_artifact_number_drift_is_rejected(self) -> None:
        self.build_valid_campaign()
        self.mutate_json(
            "numbers.json",
            lambda p: p["numbers"][2]["usages"][1].update(display_value="1300"),
        )
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertTrue(any("drifts in artifact" in item for item in report.errors))

    def test_formula_cannot_execute_code(self) -> None:
        self.build_valid_campaign()
        self.mutate_json("numbers.json", lambda p: p["numbers"][2].update(formula="__import__('os')"))
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertTrue(any("forbidden node" in item for item in report.errors))

    def test_self_review_and_distribution_authority_are_rejected(self) -> None:
        self.build_valid_campaign()

        def corrupt(payload: dict[str, object]) -> None:
            payload["content_lineage"]["reviewer"] = payload["content_lineage"]["maker"]
            payload["distribution_status"] = "authorized"

        self.mutate_json("campaign.json", corrupt)
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertTrue(any("maker and reviewer" in item for item in report.errors))
        self.assertTrue(any("not_authorized" in item for item in report.errors))

    def test_sensitive_campaign_reuses_current_evidence_without_separate_contract(self) -> None:
        self.build_valid_campaign()
        for classification in ("D2", "D3"):
            with self.subTest(classification=classification):
                self.mutate_json("campaign.json", lambda p: p["company"].update(data_classification=classification))
                report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
                self.assertEqual(report.status, "READY", report.output())

    def test_sensitive_campaign_keeps_provider_and_source_boundaries(self) -> None:
        self.build_valid_campaign()
        self.mutate_json("campaign.json", lambda p: p["company"].update(data_classification="D2"))
        self.mutate_json("campaign.json", lambda p: p["provider_policy"].update(selected_providers=["unapproved-provider"]))
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertEqual(report.status, "INVALID", report.output())
        self.assertTrue(any("exceed" in x for x in report.errors))
        self.mutate_json("campaign.json", lambda p: p["provider_policy"].update(selected_providers=["synthetic-local"]))
        self.mutate_json("sources.json", lambda p: p["sources"][0].update(allowed_providers=["different-provider"]))
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertNotEqual(report.status, "READY", report.output())
        self.assertTrue(any("provider" in x for x in report.errors + report.blockers))

    def test_sensitive_campaign_keeps_classification_boundary(self) -> None:
        self.build_valid_campaign()
        self.mutate_json("campaign.json", lambda p: p["company"].update(data_classification="D2"))
        self.mutate_json("sources.json", lambda p: p["sources"][0].update(classification="restricted"))
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertEqual(report.status, "INVALID", report.output())
        self.assertTrue(any("classification exceeds" in x for x in report.errors))

    def test_legacy_governance_hash_never_waives_owner_decisions(self) -> None:
        self.build_valid_campaign()
        self.mutate_json("campaign.json", lambda p: p["company"].update(data_classification="D3"))
        self.mutate_json("campaign.json", lambda p: p["provider_policy"].update(governance_contract_hash="a" * 64))
        self.mutate_json("campaign.json", lambda p: p["owner_decisions"]["sensitive_disclosure"].update(status="pending"))
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertEqual(report.status, "BLOCKED", report.output())
        self.assertTrue(any("sensitive_disclosure" in x for x in report.blockers))

    def test_sensitive_existing_seal_binds_current_inputs(self) -> None:
        self.build_valid_campaign()
        self.mutate_json("campaign.json", lambda p: p["company"].update(data_classification="D2"))
        args = argparse.Namespace(campaign_dir=str(self.root), output="campaign-seal.json")
        self.assertEqual(workflow.command_seal(args), 0)
        seal = json.loads((self.root / "campaign-seal.json").read_text(encoding="utf-8"))
        self.assertEqual(set(seal["files"]), workflow.CANONICAL_CAMPAIGN_INPUTS)
        for name, digest in seal["files"].items():
            self.assertEqual(digest, workflow.sha256_file(self.root / name))
        self.mutate_json("campaign.json", lambda p: p["provider_policy"].update(selected_providers=["unapproved-provider"]))
        self.assertNotEqual(seal["files"]["campaign.json"], workflow.sha256_file(self.root / "campaign.json"))
        self.assertEqual(workflow.command_seal(args), 2)

    def test_provider_boundary_is_enforced(self) -> None:
        self.build_valid_campaign()
        self.mutate_json(
            "campaign.json",
            lambda p: p["provider_policy"].update(selected_providers=["unapproved-provider"]),
        )
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertTrue(any("exceed" in item for item in report.errors))

    def test_campaign_cannot_redirect_canonical_ledgers(self) -> None:
        self.build_valid_campaign()
        for name in ("sources", "claims", "numbers"):
            source = self.root / f"{name}.json"
            target = self.root / f"alternate-{name}.json"
            target.write_bytes(source.read_bytes())
        self.mutate_json(
            "campaign.json",
            lambda p: p.update(
                ledgers={
                    "sources": "alternate-sources.json",
                    "claims": "alternate-claims.json",
                    "numbers": "alternate-numbers.json",
                }
            ),
        )
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertEqual(report.status, "INVALID", report.output())
        self.assertTrue(any("canonical ledger paths" in item for item in report.errors))

    def test_boolean_ledger_schema_version_is_rejected(self) -> None:
        self.build_valid_campaign()
        self.mutate_json("sources.json", lambda p: p.update(schema_version=True))
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertEqual(report.status, "INVALID", report.output())
        self.assertTrue(any("sources.schema_version" in item for item in report.errors))

    def test_claim_classification_cannot_exceed_campaign(self) -> None:
        self.build_valid_campaign()
        self.mutate_json(
            "claims.json",
            lambda p: p["claims"][0].update(classification="restricted"),
        )
        report = workflow.validate_campaign(self.root, today=date(2026, 8, 30))
        self.assertEqual(report.status, "INVALID", report.output())
        self.assertTrue(any("claim-proof classification exceeds" in item for item in report.errors))

    def test_seal_output_cannot_overwrite_a_campaign_input(self) -> None:
        self.build_valid_campaign()
        before = (self.root / "sources.json").read_bytes()
        args = argparse.Namespace(campaign_dir=str(self.root), output="sources.json")
        with self.assertRaisesRegex(workflow.ContractError, "cannot overwrite campaign input"):
            workflow.command_seal(args)
        self.assertEqual((self.root / "sources.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
