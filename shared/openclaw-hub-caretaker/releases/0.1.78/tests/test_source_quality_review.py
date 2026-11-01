"""Source-grounded knowledge quality: durable facts vs. metadata receipts.

A project page may carry the status ``reviewed`` when the batch contains
genuine source-grounded project facts and the existing hash-bound independent
review accepted the exact candidate. ``reviewed`` is never a live
verification: ``last_verified`` stays ``unverified`` and the verification
scope stays ``producer-reports``.

A pure directory/file metadata discovery receipt is not project understanding
and must never be promoted, and an unattested arbitrary draft stays refused.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.publication import (
    BOOTSTRAP_RECEIPT_PARAGRAPH,
    PublicationError,
    bind_review,
    build_candidate,
    fm,
    freeze,
    knowledge_basis,
    sha,
)


DURABLE_BODY = (
    "# Project state\n"
    "The local tool canary passed on 2026-09-19 and the deployment stays "
    "unverified (src-one).\n"
    "No stored token is present in the report (src-one)."
)
INVENTORY_BODY = (
    "# Discovery receipt\n"
    "Directory discovery listed 45807 files and 43208 open items in the "
    "scanned tree (src-one)."
)
REAL_SOURCE_DATA = (
    b"Dated report: local tool canary passed; deployment remains unverified."
)
#: The COMPLETE canonical metadata-only directory bootstrap receipt: fixed
#: heading, fixed receipt paragraph, Project slug, 64-hex directory identity.
BOOTSTRAP_RECEIPT = (
    "# Automatically discovered local project root\n\n"
    + BOOTSTRAP_RECEIPT_PARAGRAPH
    + "\n\nProject slug: proj-x\n"
    + "Directory identity SHA-256: " + "a" * 64 + "\n"
)


def _decision(body: str, disposition: str = "synthesized",
              knowledge_class: str | None = None,
              summary: str = "One dated report.") -> dict:
    row = {"disposition": disposition, "reason": "Dated and scope-limited report."}
    if knowledge_class is not None:
        row["knowledge_class"] = knowledge_class
    return {
        "body": body,
        "summary": summary,
        "dispositions": {"prop-one": row},
    }


def _write_source(vault: Path, sid: str, data: bytes) -> None:
    src = vault / f"raw/sources/2026/09/{sid}"
    src.mkdir(parents=True)
    (src / "original.md").write_bytes(data)
    meta = {
        "source_id": sid,
        "classification": "internal",
        "local_only": False,
        "original_file": "original.md",
        "sha256": sha(data),
        "byte_length": len(data),
    }
    (src / "source.json").write_text(json.dumps(meta), encoding="utf-8")


def _write_proposal(vault: Path, slug: str, source_ids=("src-one",)) -> None:
    ids_block = "".join(f"  - {sid}\n" for sid in source_ids)
    proposal = vault / "proposals/agent/one.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text(
        "---\nid: prop-one\nclassification: internal\nsource_ids:\n"
        + ids_block
        + f"target_pages:\n  - wiki/projects/{slug}.md\n---\nReport.\n",
        encoding="utf-8",
    )


def _write_source_and_proposal(vault: Path, slug: str) -> None:
    _write_source(vault, "src-one", REAL_SOURCE_DATA)
    _write_proposal(vault, slug)


def _attest_draft(vault: Path, state: Path, target: str, page: bytes) -> None:
    run = state / "curation" / "prior-run"
    candidate = run / "candidate"
    candidate.mkdir(parents=True)
    manifest = candidate / "publication.json"
    manifest.write_text(json.dumps({
        "outputs": [{"path": target, "sha256": sha(page)}]
    }), encoding="utf-8")
    manifest_sha = sha(manifest.read_bytes())
    event_id = "evt-prior-test"
    (run / "result.json").write_text(json.dumps({
        "status": "PUBLISHED", "manifest": str(manifest),
        "manifest_sha256": manifest_sha,
        "publication": {"event_id": event_id, "manifest_sha256": manifest_sha},
    }), encoding="utf-8")
    event = vault / "governance/curations" / (event_id + ".json")
    event.parent.mkdir(parents=True)
    event.write_text(json.dumps({
        "event_id": event_id, "manifest_sha256": manifest_sha,
        "review": {"verdict": "PASS", "maker": "openclaw-caretaker",
                   "manifest_sha256": manifest_sha,
                   "reviewer": {"id": "independent-reviewer"}},
    }), encoding="utf-8")


def _muse_envelope(candidate: Path, verdict: str = "PASS") -> dict:
    digest = sha((candidate / "publication.json").read_bytes())
    return {
        "ok": True,
        "status": "ok",
        "provider": "muse-code",
        "model": "muse-spark-1.3",
        "sessionId": "sess-source-quality",
        "final": json.dumps({
            "verdict": verdict,
            "manifest_sha256": digest,
            "findings": [],
        }),
    }


class SourceQualityReviewTests(unittest.TestCase):
    """Durable source-grounded content may be reviewed; metadata may not."""

    def _new_page_candidate(self, root: Path, decision: dict, slug: str = "proj-x"):
        vault = root / "vault"
        _write_source_and_proposal(vault, slug)
        frozen = freeze(vault, ["proposals/agent/one.md"],
                        f"wiki/projects/{slug}.md")
        candidate = root / "candidate"
        build_candidate(frozen, decision, candidate, "2026-09-20")
        page = (candidate / "payload" / "wiki" / "projects" / f"{slug}.md").read_text(
            encoding="utf-8")
        return vault, candidate, page

    def _custom_candidate(self, root: Path, decision: dict,
                          sources: dict, slug: str = "proj-x"):
        vault = root / "vault"
        for sid, data in sources.items():
            _write_source(vault, sid, data)
        _write_proposal(vault, slug, tuple(sources))
        frozen = freeze(vault, ["proposals/agent/one.md"],
                        f"wiki/projects/{slug}.md")
        candidate = root / "candidate"
        build_candidate(frozen, decision, candidate, "2026-09-20")
        page = (candidate / "payload" / "wiki" / "projects" / f"{slug}.md").read_text(
            encoding="utf-8")
        return vault, candidate, page

    # ------------------------------------------------------------------
    # Durable source-grounded content
    # ------------------------------------------------------------------

    def test_durable_source_backed_page_is_reviewed_not_verified(self):
        with tempfile.TemporaryDirectory() as d:
            _vault, _candidate, page = self._new_page_candidate(
                Path(d), _decision(DURABLE_BODY, knowledge_class="durable-fact"))
            self.assertEqual(fm(page, "status"), "reviewed")
            self.assertNotEqual(fm(page, "status"), "verified")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            self.assertEqual(fm(page, "verification_scope"), "producer-reports")
            self.assertEqual(fm(page, "project_slug"), "proj-x")
            self.assertIn("src-one", fm(page, "sources"))

    def test_reviewed_page_never_claims_live_verification(self):
        with tempfile.TemporaryDirectory() as d:
            _vault, _candidate, page = self._new_page_candidate(
                Path(d), _decision(DURABLE_BODY, knowledge_class="durable-fact"))
            self.assertNotEqual(fm(page, "status"), "verified")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            # A current verification date is never invented for a page whose
            # facts are producer reports.
            self.assertNotRegex(str(fm(page, "last_verified")),
                                r"^\d{4}-\d{2}-\d{2}")

    def test_legacy_decision_without_class_is_not_promoted_by_words_or_dates(self):
        """ASCII words and a date regex never prove a durable fact."""
        with tempfile.TemporaryDirectory() as d:
            _vault, candidate, page = self._new_page_candidate(
                Path(d), _decision(DURABLE_BODY))
            self.assertEqual(fm(page, "status"), "draft")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            self.assertIsNone(fm(page, "last_reviewed"))
            ledger = json.loads((candidate / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["knowledge_basis"]["status"], "unconfirmed")
            self.assertEqual(ledger["knowledge_basis"]["durable_fact_count"], 0)
            self.assertEqual(ledger["knowledge_basis"]["evidence"]["prop-one"],
                             "conservative_default")

    def test_legacy_undated_fact_stays_unconfirmed_and_draft(self):
        """Without a date or explicit evidence a fact is not promoted."""
        with tempfile.TemporaryDirectory() as d:
            undated = (
                "# Project note\n"
                "The canary tool reported a failure in the local run (src-one)."
            )
            _vault, candidate, page = self._new_page_candidate(
                Path(d), _decision(undated))
            self.assertEqual(fm(page, "status"), "draft")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            ledger = json.loads((candidate / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["knowledge_basis"]["status"], "unconfirmed")
            self.assertEqual(ledger["knowledge_basis"]["durable_fact_count"], 0)

    def test_legacy_inventory_receipt_without_class_stays_draft(self):
        with tempfile.TemporaryDirectory() as d:
            _vault, _candidate, page = self._new_page_candidate(
                Path(d), _decision(INVENTORY_BODY))
            self.assertEqual(fm(page, "status"), "draft")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            self.assertIsNone(fm(page, "last_reviewed"))

    # ------------------------------------------------------------------
    # Metadata inventory receipts are not project understanding
    # ------------------------------------------------------------------

    def test_metadata_inventory_receipt_stays_unaccepted_draft(self):
        with tempfile.TemporaryDirectory() as d:
            _vault, _candidate, page = self._new_page_candidate(
                Path(d), _decision(INVENTORY_BODY, knowledge_class="inventory-metadata"))
            self.assertEqual(fm(page, "status"), "draft")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            self.assertEqual(fm(page, "verification_scope"), "producer-reports")
            # No current review or verification date is invented for a receipt.
            self.assertIsNone(fm(page, "last_reviewed"))
            self.assertNotEqual(fm(page, "status"), "verified")

    def test_inventory_batch_never_invents_verification_dates(self):
        with tempfile.TemporaryDirectory() as d:
            vault, candidate, _page = self._new_page_candidate(
                Path(d), _decision(INVENTORY_BODY, knowledge_class="inventory-metadata"))
            ledger = json.loads((candidate / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["knowledge_basis"]["status"], "inventory")
            self.assertEqual(ledger["knowledge_basis"]["durable_fact_count"], 0)
            self.assertEqual(
                ledger["proposals"][0]["knowledge_class"], "inventory-metadata")
            self.assertFalse((vault / "wiki" / "projects").exists())

    def test_zero_durable_fact_batch_stays_explicit_and_unpromoted(self):
        with tempfile.TemporaryDirectory() as d:
            decision = {
                "body": "Earlier context only; the question stays open (src-one).",
                "summary": "Open question.",
                "dispositions": {
                    "prop-one": {
                        "disposition": "unresolved",
                        "reason": "Conflicting reports; not safely integrable.",
                        "knowledge_class": "unresolved-question",
                    }
                },
            }
            _vault, candidate, page = self._new_page_candidate(Path(d), decision)
            self.assertEqual(fm(page, "status"), "draft")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            ledger = json.loads((candidate / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["knowledge_basis"]["status"], "empty")
            self.assertEqual(ledger["knowledge_basis"]["durable_fact_count"], 0)

    # ------------------------------------------------------------------
    # Canonical bootstrap receipt closure is never durable
    # ------------------------------------------------------------------


    def test_actual_discovery_producer_protocol_is_not_project_knowledge(self):
        actual = ("# Automatically discovered local project root\n\n"
                  "Metadata-only discovery found a project directory on the owner's D: drive. "
                  "Its business purpose, file contents and current operating state have not yet "
                  "been verified. This receipt establishes an isolated project identity for later "
                  "source-grounded ingestion; it does not assert readiness or authorize action.\n\n"
                  "Project slug: proj-x\nDirectory identity SHA-256: " + "a" * 64 + "\n")
        with tempfile.TemporaryDirectory() as d:
            _vault, candidate, page = self._custom_candidate(
                Path(d), _decision(DURABLE_BODY, knowledge_class="durable-fact"),
                {"src-one": actual.encode("utf-8")})
            self.assertEqual(fm(page, "status"), "draft")
            ledger = json.loads((candidate / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["knowledge_basis"]["status"], "inventory")

    def test_bootstrap_closure_with_false_durable_label_stays_inventory(self):
        """A complete bootstrap-only closure cannot become durable by label."""
        with tempfile.TemporaryDirectory() as d:
            _vault, candidate, page = self._custom_candidate(
                Path(d),
                _decision(DURABLE_BODY, knowledge_class="durable-fact"),
                {"src-one": BOOTSTRAP_RECEIPT.encode("utf-8")})
            self.assertEqual(fm(page, "status"), "draft")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            self.assertIsNone(fm(page, "last_reviewed"))
            ledger = json.loads((candidate / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["knowledge_basis"]["status"], "inventory")
            self.assertEqual(ledger["knowledge_basis"]["durable_fact_count"], 0)
            self.assertEqual(ledger["knowledge_basis"]["evidence"]["prop-one"],
                             "bootstrap_receipt_closure")
            self.assertEqual(
                ledger["proposals"][0]["knowledge_class"], "inventory-metadata")

    def test_bootstrap_closure_with_multiple_receipts_stays_inventory(self):
        """Several complete receipts are still only a bootstrap closure."""
        with tempfile.TemporaryDirectory() as d:
            _vault, candidate, page = self._custom_candidate(
                Path(d),
                _decision(
                    "# Receipts\nTwo directory receipts were recorded "
                    "(src-one) (src-two).",
                    knowledge_class="durable-fact"),
                {"src-one": BOOTSTRAP_RECEIPT.encode("utf-8"),
                 "src-two": BOOTSTRAP_RECEIPT.encode("utf-8")})
            self.assertEqual(fm(page, "status"), "draft")
            ledger = json.loads((candidate / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["knowledge_basis"]["status"], "inventory")
            self.assertEqual(ledger["knowledge_basis"]["durable_fact_count"], 0)

    def test_mixed_bootstrap_and_real_source_is_reviewed_for_real_content(self):
        """A mixed batch is not a closure; its real durable content counts."""
        with tempfile.TemporaryDirectory() as d:
            _vault, candidate, page = self._custom_candidate(
                Path(d),
                _decision(
                    "# Project state\n"
                    "The local tool canary passed and the deployment stays "
                    "unverified (src-two). The directory receipt was also "
                    "recorded (src-one).",
                    knowledge_class="durable-fact"),
                {"src-one": BOOTSTRAP_RECEIPT.encode("utf-8"),
                 "src-two": REAL_SOURCE_DATA})
            self.assertEqual(fm(page, "status"), "reviewed")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            self.assertNotEqual(fm(page, "status"), "verified")
            ledger = json.loads((candidate / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["knowledge_basis"]["status"], "durable")
            self.assertEqual(ledger["knowledge_basis"]["evidence"]["prop-one"],
                             "explicit_decision_evidence")

    def test_extended_receipt_with_real_content_is_not_a_closure(self):
        """A keyword or fragment match never triggers the bootstrap clamp."""
        with tempfile.TemporaryDirectory() as d:
            extended = (
                BOOTSTRAP_RECEIPT
                + "\nProject content: the local canary gate passed in the "
                  "tool run and remains producer-reported.\n"
            )
            _vault, candidate, page = self._custom_candidate(
                Path(d),
                _decision(DURABLE_BODY, knowledge_class="durable-fact"),
                {"src-one": extended.encode("utf-8")})
            self.assertEqual(fm(page, "status"), "reviewed")
            ledger = json.loads((candidate / "ledger.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger["knowledge_basis"]["status"], "durable")
            self.assertEqual(ledger["knowledge_basis"]["evidence"]["prop-one"],
                             "explicit_decision_evidence")

    def test_genuine_polish_undated_content_with_explicit_class_is_reviewed(self):
        """Real technical Polish content without an event date can be reviewed.

        The explicit source-backed class plus the independent review supports
        reviewed CONTENT; it never asserts current live verification.
        """
        with tempfile.TemporaryDirectory() as d:
            body = (
                "# Stan projektu\n"
                "Lokalne narzedzie kanarykowe przechodzi kontrole, a wdrozenie "
                "pozostaje niezweryfikowane (src-one)."
            )
            _vault, _candidate, page = self._new_page_candidate(
                Path(d), _decision(body, knowledge_class="durable-fact"))
            self.assertEqual(fm(page, "status"), "reviewed")
            self.assertNotEqual(fm(page, "status"), "verified")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            self.assertEqual(fm(page, "verification_scope"), "producer-reports")

    # ------------------------------------------------------------------
    # The independent reviewer still gates everything
    # ------------------------------------------------------------------

    def test_accepted_independent_review_binds_to_the_exact_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            _vault, candidate, page = self._new_page_candidate(
                Path(d), _decision(DURABLE_BODY, knowledge_class="durable-fact"))
            self.assertEqual(fm(page, "status"), "reviewed")
            # No acceptance artifact exists before the real binding.
            self.assertFalse((candidate / "review.json").exists())
            self.assertFalse((candidate / "semantic-review.json").exists())
            bind_review(candidate, _muse_envelope(candidate))
            digest = sha((candidate / "publication.json").read_bytes())
            review = json.loads((candidate / "review.json").read_text(encoding="utf-8"))
            self.assertEqual(review["verdict"], "PASS")
            self.assertEqual(review["manifest_sha256"], digest)
            evidence = json.loads(
                (candidate / "semantic-review.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["reviewed_manifest_sha256"], digest)

    def test_prepared_candidate_is_not_published_before_acceptance(self):
        with tempfile.TemporaryDirectory() as d:
            vault, candidate, _page = self._new_page_candidate(
                Path(d), _decision(DURABLE_BODY, knowledge_class="durable-fact"))
            # Candidate preparation alone writes no wiki page and no review.
            self.assertFalse((vault / "wiki" / "projects").exists())
            self.assertFalse((candidate / "review.json").exists())
            self.assertFalse((candidate / "semantic-review.json").exists())

    def test_failed_review_does_not_accept_the_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            vault, candidate, _page = self._new_page_candidate(
                Path(d), _decision(DURABLE_BODY, knowledge_class="durable-fact"))
            with self.assertRaisesRegex(
                    PublicationError, "independent review did not accept exact publication"):
                bind_review(candidate, _muse_envelope(candidate, verdict="REPAIR"))
            self.assertFalse((candidate / "review.json").exists())
            self.assertFalse((candidate / "semantic-review.json").exists())
            self.assertFalse((vault / "wiki" / "projects").exists())

    def test_stale_or_tampered_manifest_is_rejected_by_binding(self):
        with tempfile.TemporaryDirectory() as d:
            _vault, candidate, _page = self._new_page_candidate(
                Path(d), _decision(DURABLE_BODY, knowledge_class="durable-fact"))
            envelope = _muse_envelope(candidate)
            # A grant issued for stale manifest bytes does not bind to the
            # current manifest.
            with self.assertRaisesRegex(
                    PublicationError, "independent review did not accept exact publication"):
                bind_review(candidate, envelope, reviewed_manifest_sha256="0" * 64)
            bind_review(candidate, envelope)
            # Tampering after binding invalidates a re-binding with the old
            # envelope: the recorded digest no longer matches the bytes.
            manifest = candidate / "publication.json"
            manifest.write_bytes(manifest.read_bytes() + b" ")
            with self.assertRaisesRegex(
                    PublicationError, "independent review did not accept exact publication"):
                bind_review(candidate, envelope)

    # ------------------------------------------------------------------
    # Refusals and preservation
    # ------------------------------------------------------------------

    def test_arbitrary_unattested_draft_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "proj-x")
            target = vault / "wiki/projects/proj-x.md"
            target.parent.mkdir(parents=True)
            target.write_text(
                '---\ntitle: "proj-x"\ntype: "project"\nstatus: "draft"\n'
                'summary: "Owner text."\nlast_verified: "unverified"\n'
                'verification_scope: "producer-reports"\nsources: ["src-one"]\n'
                'project_slug: "proj-x"\n---\n\n'
                '## Caretaker publication 2026-09-19\nUnattested claim (src-one).\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(PublicationError, "target is not reviewed"):
                freeze(vault, ["proposals/agent/one.md"], "wiki/projects/proj-x.md",
                       attestation_state_dir=root / "state")

    def test_attested_draft_grows_into_reviewed_without_live_claim(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "proj-x")
            target = vault / "wiki/projects/proj-x.md"
            target.parent.mkdir(parents=True)
            prior = (
                '---\ntitle: "proj-x"\ntype: "project"\nstatus: "draft"\n'
                'summary: "First source."\nlast_verified: "unverified"\n'
                'verification_scope: "producer-reports"\nsources: ["src-one"]\n'
                'project_slug: "proj-x"\n---\n\n'
                '## Caretaker publication 2026-09-19\nFirst source (src-one).\n'
            )
            target.write_text(prior, encoding="utf-8")
            state = root / "state"
            _attest_draft(vault, state, "wiki/projects/proj-x.md", target.read_bytes())
            frozen = freeze(vault, ["proposals/agent/one.md"],
                            "wiki/projects/proj-x.md", attestation_state_dir=state)
            candidate = root / "candidate"
            build_candidate(frozen, _decision(DURABLE_BODY, knowledge_class="durable-fact"),
                            candidate, "2026-09-20")
            page = (candidate / "payload" / "wiki" / "projects" / "proj-x.md").read_text(
                encoding="utf-8")
            self.assertEqual(fm(page, "status"), "reviewed")
            self.assertEqual(fm(page, "last_verified"), "unverified")
            self.assertIn("First source (src-one)", page)

    def test_past_claims_contradictions_and_source_ids_are_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "proj-x")
            target = vault / "wiki/projects/proj-x.md"
            target.parent.mkdir(parents=True)
            prior = (
                '---\ntitle: "proj-x"\ntype: "project"\nstatus: "reviewed"\n'
                'summary: "Earlier claims."\nlast_verified: "2026-01-01"\n'
                'verification_scope: "producer-reports"\nsources: ["src-one"]\n'
                'project_slug: "proj-x"\n---\n\n'
                '## Caretaker publication 2026-08-01\n'
                'Claim A says the token exists (src-one).\n'
                'Claim B says no token is stored (src-one).\n'
                'The contradiction is kept, not fused.\n'
            )
            target.write_text(prior, encoding="utf-8")
            frozen = freeze(vault, ["proposals/agent/one.md"], "wiki/projects/proj-x.md")
            candidate = root / "candidate"
            build_candidate(frozen, _decision(DURABLE_BODY, knowledge_class="durable-fact"),
                            candidate, "2026-09-20")
            page = (candidate / "payload" / "wiki" / "projects" / "proj-x.md").read_text(
                encoding="utf-8")
            self.assertIn("Claim A says the token exists (src-one).", page)
            self.assertIn("Claim B says no token is stored (src-one).", page)
            self.assertIn("The contradiction is kept, not fused.", page)
            self.assertIn("src-one", fm(page, "sources"))
            # The earlier verification date is kept; no current date invented.
            self.assertEqual(fm(page, "last_verified"), "2026-01-01")
            self.assertEqual(fm(page, "status"), "reviewed")

    def test_knowledge_basis_reports_inventory_and_durable_counts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "proj-x")
            frozen = freeze(vault, ["proposals/agent/one.md"],
                            "wiki/projects/proj-x.md")
            durable = knowledge_basis(
                frozen, DURABLE_BODY,
                {"prop-one": {"disposition": "synthesized",
                              "knowledge_class": "durable-fact"}})
            self.assertEqual(durable["status"], "durable")
            self.assertEqual(durable["durable_fact_count"], 1)
            self.assertEqual(durable["evidence"]["prop-one"],
                             "explicit_decision_evidence")
            inventory = knowledge_basis(
                frozen, INVENTORY_BODY,
                {"prop-one": {"disposition": "synthesized",
                              "knowledge_class": "inventory-metadata"}})
            self.assertEqual(inventory["status"], "inventory")
            self.assertEqual(inventory["durable_fact_count"], 0)

    def test_unknown_knowledge_class_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = root / "vault"
            _write_source_and_proposal(vault, "proj-x")
            frozen = freeze(vault, ["proposals/agent/one.md"],
                            "wiki/projects/proj-x.md")
            with self.assertRaisesRegex(PublicationError, "invalid disposition"):
                build_candidate(
                    frozen,
                    {"body": DURABLE_BODY, "summary": "s",
                     "dispositions": {"prop-one": {
                         "disposition": "synthesized", "reason": "r",
                         "knowledge_class": "made-up"}}},
                    root / "bad-class", "2026-09-20",
                )


if __name__ == "__main__":
    unittest.main()
