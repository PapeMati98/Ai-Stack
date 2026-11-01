from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

from pypdf import PdfWriter

import investment_material_acceptance as acceptance


class InvestmentMaterialAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="investment acceptance ")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    @staticmethod
    def write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def write_pdf(path: Path, pages: int) -> None:
        writer = PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=595, height=842)
        with path.open("wb") as handle:
            writer.write(handle)

    def build_valid_candidate(self) -> dict[str, object]:
        evidence = self.root / "evidence" / "verified.txt"
        evidence.parent.mkdir()
        evidence.write_text("Synthetic company evidence only.\n", encoding="utf-8")
        evidence_digest = self.digest(evidence)
        decisions = {
            key: {"status": "approved", "value": f"approved-{key}"}
            for key in (
                "audience",
                "final_ask",
                "funding_amount",
                "geography",
                "instrument",
                "sensitive_disclosure",
                "stage",
                "terms",
            )
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
        self.write_json(self.root / "campaign.json", campaign)
        self.write_json(
            self.root / "sources.json",
            {
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
                        "sha256": evidence_digest,
                        "verification_status": "verified",
                        "allowed_providers": ["synthetic-local"],
                    }
                ],
            },
        )
        self.write_json(
            self.root / "claims.json",
            {
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
            },
        )
        self.write_json(
            self.root / "numbers.json",
            {
                "schema_version": 1,
                "numbers": [
                    {
                        "number_id": "number-proof",
                        "kind": "input",
                        "status": "verified",
                        "value": "120000",
                        "unit": "currency",
                        "currency": "PLN",
                        "period": "synthetic-year",
                        "source_ids": ["source-evidence"],
                        "rounding": {"places": 0, "mode": "HALF_EVEN"},
                        "tolerance": "0",
                        "usages": [
                            {"artifact": "deck", "display_value": "120000"},
                            {"artifact": "memorandum", "display_value": "120000"},
                            {"artifact": "one-pager", "display_value": "120000"},
                        ],
                    }
                ],
            },
        )
        seal = {
            "schema_version": 1,
            "campaign_id": "campaign-synthetic",
            "status": "READY_FOR_DRAFTING",
            "files": {
                name: self.digest(self.root / name)
                for name in ("campaign.json", "sources.json", "claims.json", "numbers.json")
            },
        }
        self.write_json(self.root / "campaign-seal.json", seal)

        artifacts: list[dict[str, object]] = []
        artifact_by_id: dict[str, dict[str, object]] = {}
        for artifact_type, extension, pages in (
            ("deck", "pptx", 10),
            ("memorandum", "docx", 8),
            ("one-pager", "docx", 1),
        ):
            artifact_id = f"artifact-{artifact_type}"
            editable = self.root / "artifacts" / f"{artifact_type}.{extension}"
            pdf = self.root / "artifacts" / f"{artifact_type}.pdf"
            editable.parent.mkdir(exist_ok=True)
            editable.write_text(f"synthetic editable {artifact_type}\n", encoding="utf-8")
            self.write_pdf(pdf, pages)
            row = {
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "editable_path": editable.relative_to(self.root).as_posix(),
                "editable_sha256": self.digest(editable),
                "pdf_path": pdf.relative_to(self.root).as_posix(),
                "pdf_sha256": self.digest(pdf),
                "pdf_page_count": pages,
            }
            artifacts.append(row)
            artifact_by_id[artifact_id] = row

        reviews: list[dict[str, object]] = []
        for gate, kind in acceptance.GATE_BINDINGS.items():
            evidence = self.root / "reviews" / f"{gate}.md"
            evidence.parent.mkdir(exist_ok=True)
            evidence.write_text(f"Synthetic {gate} review: PASS.\n", encoding="utf-8")
            reviews.append(
                {
                    "gate": gate,
                    "reviewer": f"reviewer-{gate}",
                    "status": "PASS",
                    "binding_kind": kind,
                    "artifact_hashes": {
                        artifact_id: acceptance.binding_hash(artifact, kind)
                        for artifact_id, artifact in artifact_by_id.items()
                    },
                    "evidence_ref": evidence.relative_to(self.root).as_posix(),
                    "evidence_sha256": self.digest(evidence),
                }
            )

        candidate = {
            "schema_version": 1,
            "receipt_id": "receipt-synthetic",
            "campaign_id": "campaign-synthetic",
            "campaign_seal": {
                "path": "campaign-seal.json",
                "sha256": self.digest(self.root / "campaign-seal.json"),
            },
            "artifacts": artifacts,
            "content_lineage": {"maker": "maker-content"},
            "design_lineage": {"maker": "maker-design"},
            "reviews": reviews,
            "findings": [],
            "repair_budget": {"max_rounds": 2, "used_rounds": 1},
            "distribution_status": "not_authorized",
        }
        self.write_json(self.root / "candidate.json", candidate)
        return candidate

    def mutate_candidate(self, callback: object) -> None:
        path = self.root / "candidate.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        callback(payload)
        self.write_json(path, payload)

    def refresh_forged_seal(self) -> None:
        seal_path = self.root / "campaign-seal.json"
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        seal["files"] = {
            name: self.digest(self.root / name)
            for name in ("campaign.json", "sources.json", "claims.json", "numbers.json")
        }
        self.write_json(seal_path, seal)
        self.mutate_candidate(
            lambda payload: payload["campaign_seal"].update(sha256=self.digest(seal_path))
        )

    def test_complete_candidate_is_acceptable(self) -> None:
        self.build_valid_candidate()
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertEqual(report.status, "ACCEPTABLE", report.output())
        self.assertEqual(report.state["gate_count"], 6)

    def test_one_pager_must_be_exactly_one_page(self) -> None:
        self.build_valid_candidate()
        self.mutate_candidate(
            lambda p: next(a for a in p["artifacts"] if a["artifact_type"] == "one-pager").update(pdf_page_count=2)
        )
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertTrue(any("exactly one page" in item for item in report.errors))

    def test_one_pager_page_count_is_measured_from_pdf_bytes(self) -> None:
        self.build_valid_candidate()
        one_pager_pdf = self.root / "artifacts" / "one-pager.pdf"
        self.write_pdf(one_pager_pdf, 2)

        def update_hash(payload: dict[str, object]) -> None:
            artifact = next(
                item for item in payload["artifacts"] if item["artifact_type"] == "one-pager"
            )
            artifact["pdf_sha256"] = self.digest(one_pager_pdf)

        self.mutate_candidate(update_hash)
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertTrue(any("does not match the PDF bytes" in item for item in report.errors))

    def test_content_maker_cannot_review_own_work(self) -> None:
        self.build_valid_candidate()

        def corrupt(payload: dict[str, object]) -> None:
            next(review for review in payload["reviews"] if review["gate"] == "factual")["reviewer"] = "maker-content"

        self.mutate_candidate(corrupt)
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertTrue(any("cannot equal content maker" in item for item in report.errors))

    def test_receipt_makers_must_match_sealed_campaign(self) -> None:
        self.build_valid_candidate()
        self.mutate_candidate(
            lambda payload: (
                payload["content_lineage"].update(maker="relabelled-content"),
                payload["design_lineage"].update(maker="relabelled-design"),
            )
        )
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertTrue(any("sealed campaign content maker" in item for item in report.errors))
        self.assertTrue(any("sealed campaign design maker" in item for item in report.errors))

    def test_partial_campaign_seal_cannot_omit_ledgers(self) -> None:
        self.build_valid_candidate()
        seal_path = self.root / "campaign-seal.json"
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        seal["files"] = {"campaign.json": self.digest(self.root / "campaign.json")}
        self.write_json(seal_path, seal)
        self.mutate_candidate(
            lambda payload: payload["campaign_seal"].update(sha256=self.digest(seal_path))
        )
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertTrue(any("all three ledgers" in item for item in report.errors))

    def test_source_classification_cannot_exceed_campaign(self) -> None:
        self.build_valid_candidate()
        sources_path = self.root / "sources.json"
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
        sources["sources"][0]["classification"] = "restricted"
        self.write_json(sources_path, sources)
        self.refresh_forged_seal()
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertTrue(any("exceeds campaign data_classification" in item for item in report.errors))

    def test_missing_gate_is_not_accepted(self) -> None:
        self.build_valid_candidate()
        self.mutate_candidate(lambda p: p.update(reviews=p["reviews"][:-1]))
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertEqual(report.status, "NOT_ACCEPTED")
        self.assertTrue(any("missing review gates" in item for item in report.blockers))

    def test_stale_review_binding_is_rejected(self) -> None:
        self.build_valid_candidate()

        def corrupt(payload: dict[str, object]) -> None:
            payload["reviews"][0]["artifact_hashes"]["artifact-deck"] = "0" * 64

        self.mutate_candidate(corrupt)
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertTrue(any("stale binding" in item for item in report.errors))

    def test_open_p1_blocks_acceptance(self) -> None:
        self.build_valid_candidate()
        self.mutate_candidate(
            lambda p: p["findings"].append({"severity": "P1", "status": "open", "code": "UNSUPPORTED_CLAIM"})
        )
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertEqual(report.status, "NOT_ACCEPTED")
        self.assertTrue(any("open P1" in item for item in report.blockers))

    def test_changed_artifact_invalidates_candidate(self) -> None:
        self.build_valid_candidate()
        (self.root / "artifacts" / "deck.pdf").write_text("changed\n", encoding="utf-8")
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertTrue(any("pdf hash mismatch" in item for item in report.errors))

    def test_seal_and_verify_current_candidate(self) -> None:
        self.build_valid_candidate()
        seal_args = argparse.Namespace(
            campaign_dir=str(self.root),
            receipt="candidate.json",
            output="final-acceptance.json",
        )
        self.assertEqual(acceptance.command_seal(seal_args), 0)
        verify_args = argparse.Namespace(
            campaign_dir=str(self.root),
            acceptance="final-acceptance.json",
        )
        self.assertEqual(acceptance.command_verify(verify_args), 0)

    def test_distribution_authority_is_rejected(self) -> None:
        self.build_valid_candidate()
        self.mutate_candidate(lambda p: p.update(distribution_status="authorized"))
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertTrue(any("not_authorized" in item for item in report.errors))

    def test_forged_ready_seal_cannot_bypass_campaign_gate(self) -> None:
        self.build_valid_candidate()
        campaign_path = self.root / "campaign.json"
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        campaign["company"]["continuity_status"] = "MISSING"
        self.write_json(campaign_path, campaign)
        self.refresh_forged_seal()
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertEqual(report.status, "NOT_ACCEPTED", report.output())
        self.assertEqual(report.state["campaign_gate_status"], "BLOCKED")
        self.assertTrue(any("project continuity is MISSING" in item for item in report.blockers))

    def test_duplicate_candidate_key_is_rejected(self) -> None:
        self.build_valid_candidate()
        candidate_path = self.root / "candidate.json"
        raw = candidate_path.read_text(encoding="utf-8")
        candidate_path.write_text(
            raw.replace(
                '  "distribution_status": "not_authorized",',
                '  "distribution_status": "authorized",\n  "distribution_status": "not_authorized",',
                1,
            ),
            encoding="utf-8",
        )
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertEqual(report.status, "INVALID")
        self.assertTrue(any("duplicate JSON key" in item for item in report.errors))

    def test_alternate_ledgers_cannot_bypass_the_campaign_seal(self) -> None:
        self.build_valid_candidate()
        for name in ("sources", "claims", "numbers"):
            source = self.root / f"{name}.json"
            target = self.root / f"alternate-{name}.json"
            target.write_bytes(source.read_bytes())
        campaign_path = self.root / "campaign.json"
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        campaign["ledgers"] = {
            "sources": "alternate-sources.json",
            "claims": "alternate-claims.json",
            "numbers": "alternate-numbers.json",
        }
        self.write_json(campaign_path, campaign)
        self.refresh_forged_seal()
        alternate_claims = self.root / "alternate-claims.json"
        claims = json.loads(alternate_claims.read_text(encoding="utf-8"))
        claims["claims"][0]["text"] = "Changed after the canonical seal was refreshed."
        self.write_json(alternate_claims, claims)
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertEqual(report.status, "INVALID", report.output())
        self.assertTrue(any("canonical ledger paths" in item for item in report.errors))

    def test_canonical_ledger_mutation_after_seal_is_rejected(self) -> None:
        self.build_valid_candidate()
        claims_path = self.root / "claims.json"
        claims = json.loads(claims_path.read_text(encoding="utf-8"))
        claims["claims"][0]["text"] = "Changed after sealing."
        self.write_json(claims_path, claims)
        report = acceptance.validate_candidate(self.root, "candidate.json")
        self.assertEqual(report.status, "INVALID", report.output())
        self.assertTrue(any("campaign seal file changed: claims.json" in item for item in report.errors))

    def test_acceptance_seal_cannot_overwrite_canonical_campaign_inputs(self) -> None:
        self.build_valid_candidate()
        for name in ("campaign.json", "sources.json"):
            with self.subTest(name=name):
                target = self.root / name
                original = target.read_bytes()
                seal_args = argparse.Namespace(
                    campaign_dir=str(self.root),
                    receipt="candidate.json",
                    output=name,
                )
                self.assertEqual(acceptance.command_seal(seal_args), 2)
                self.assertEqual(target.read_bytes(), original)

    def test_final_receipt_boolean_schema_version_is_rejected(self) -> None:
        self.build_valid_candidate()
        seal_args = argparse.Namespace(
            campaign_dir=str(self.root),
            receipt="candidate.json",
            output="final-acceptance.json",
        )
        self.assertEqual(acceptance.command_seal(seal_args), 0)
        final_path = self.root / "final-acceptance.json"
        final = json.loads(final_path.read_text(encoding="utf-8"))
        final["schema_version"] = True
        self.write_json(final_path, final)
        verify_args = argparse.Namespace(
            campaign_dir=str(self.root),
            acceptance="final-acceptance.json",
        )
        self.assertEqual(acceptance.command_verify(verify_args), 2)

    def test_sibling_validator_digest_is_pinned(self) -> None:
        original = acceptance.FUNDRAISING_WORKFLOW_SHA256
        acceptance.load_fundraising_workflow.cache_clear()
        acceptance.FUNDRAISING_WORKFLOW_SHA256 = "0" * 64
        try:
            with self.assertRaisesRegex(acceptance.AcceptanceError, "digest mismatch"):
                acceptance.load_fundraising_workflow()
        finally:
            acceptance.FUNDRAISING_WORKFLOW_SHA256 = original
            acceptance.load_fundraising_workflow.cache_clear()


if __name__ == "__main__":
    unittest.main()
