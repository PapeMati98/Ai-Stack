"""Synthetic fixtures only: append-only disposition-resolution gateway tests."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("curation", HERE / "curation.py")
curation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(curation)
knowledge = curation.knowledge

PID = "prop-20260911T000000000Z-bbbb0000"
PID2 = "prop-20260911T000000000Z-cccc0000"
EVT_A = "evt-20260911T000000000Z-aaaa0000"
EVT_B = "evt-20260911T000000000Z-bbbb1111"
RES_A = "res-20260911T000000000Z-dddd0000"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def staged_fixture_source(vault: Path, label: str, content: str) -> Path:
    raw = content.encode("utf-8")
    source_id = f"src-20260911T000000000Z-{label}-{sha(raw)[:8]}"
    package = vault / "raw" / "sources" / "2026" / "09" / source_id
    package.mkdir(parents=True, exist_ok=True)
    original = package / "original.txt"
    original.write_bytes(raw)
    write_json(package / "source.json", {
        "schema_version": 1, "source_id": source_id, "title": f"Synthetic {label}",
        "origin": "synthetic fixture", "captured_at": "2026-09-11T00:00:00Z",
        "captured_by": "codex", "classification": "internal", "local_only": False,
        "original_file": "original.txt", "byte_length": len(raw), "sha256": sha(raw),
        "trust": "untrusted", "risk_flags": [],
    })
    return original


def proposal_doc(proposal_id: str, title: str) -> str:
    return (
        f"---\nid: {proposal_id}\ntype: proposal\nstatus: proposed\ntitle: {title}\n"
        f"agent: codex\ncreated_at: 2026-09-11T00:00:00Z\nclassification: internal\n"
        f"source_ids: []\ntarget_pages: []\n---\n\n# {title}\n\nSynthetic curation fixture.\n"
    )


def curation_event(event_id: str, proposal_ids: list[str]) -> dict:
    return {
        "event_id": event_id,
        "occurred_at": "2026-09-11T00:00:00Z",
        "agent": "codex",
        "action": "record-curation",
        "proposal_ids": proposal_ids,
        "pages_changed": [],
        "source_ids": [],
        "summary": f"Synthetic curation {event_id}",
    }


def curation_ledger(event_id: str, rows: list[dict]) -> dict:
    return {"event_id": event_id, "proposals": rows, "sources": []}


class DispositionResolutionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        for directory in (
            self.vault / "proposals" / "codex",
            self.vault / "governance" / "curations",
            self.vault / "events" / "2026" / "09",
            self.vault / "wiki",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        write_json(
            self.vault / "knowledge-vault.json",
            {"curation_scope": "designated-machine-only", "curator_machine": "TEST-MACHINE"},
        )
        self.lock_id = "lock-res-1"
        self.set_lock(self.lock_id)
        self.proposal = self.vault / "proposals" / "codex" / f"{PID}-synthetic.md"
        self.proposal.write_text(proposal_doc(PID, "Synthetic duplicated"), encoding="utf-8")
        self.add_ledger_pair(EVT_A, PID, "synthesized")
        self.add_ledger_pair(EVT_B, PID, "unresolved")
        self.candidate = self.root / "candidate"
        self.candidate.mkdir()
        self.evidence = staged_fixture_source(self.vault, "evidence", "Synthetic independent review evidence.\n")
        self.review_evidence = staged_fixture_source(self.vault, "review", "Synthetic reviewer proof.\n")
        self.resolution_path = self.candidate / "resolution.json"
        self.review_path = self.candidate / "resolution-review.json"
        self.write_resolution()
        for patch in (
            mock.patch.object(curation, "DEFAULT_VAULT", self.vault),
            mock.patch.object(curation.knowledge, "local_machine_name", return_value="TEST-MACHINE"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.before = self.vault_bytes()

    def set_lock(self, ident: str) -> None:
        write_json(
            self.vault / "governance" / "curator.lock",
            {
                "lock_id": ident,
                "machine": "TEST-MACHINE",
                "agent": "codex",
                "acquired_at": knowledge.iso_time(knowledge.now_utc()),
            },
        )

    def add_ledger_pair(self, event_id: str, proposal_id: str, disposition: str, *, reason: str = "Synthetic.") -> None:
        write_json(
            self.vault / "events" / "2026" / "09" / f"{event_id}.json",
            curation_event(event_id, [proposal_id]),
        )
        write_json(
            self.vault / "governance" / "curations" / f"{event_id}.json",
            curation_ledger(event_id, [{"proposal_id": proposal_id, "disposition": disposition, "reason": reason}]),
        )

    def write_resolution(
        self,
        *,
        proposal_id: str = PID,
        supersedes: list[dict] | None = None,
        effective_disposition: str = "unresolved",
        resolution_id: str = RES_A,
        reason: str = "Independent review reconciles duplicated curations; matter remains open.",
    ) -> dict:
        if supersedes is None:
            supersedes = [
                {"event_id": EVT_A, "disposition": "synthesized"},
                {"event_id": EVT_B, "disposition": "unresolved"},
            ]
        document = {
            "schema_version": 1,
            "vault_path": str(self.vault),
            "resolution_id": resolution_id,
            "proposal_id": proposal_id,
            "supersedes": supersedes,
            "effective_disposition": effective_disposition,
            "reason": reason,
            "evidence": {"path": self.evidence.relative_to(self.vault).as_posix(), "sha256": sha(self.evidence.read_bytes())},
        }
        write_json(self.resolution_path, document)
        self.write_review()
        return document

    def write_review(
        self,
        *,
        maker: str = "codex-hub-owner",
        reviewer_id: str = "fixture-reviewer",
        include_reviewer: bool = True,
        include_evidence: bool = True,
    ) -> dict:
        document = json.loads(self.resolution_path.read_text(encoding="utf-8"))
        payload = {key: document[key] for key in knowledge.RESOLUTION_BINDING_FIELDS}
        review = {
            "schema_version": 1,
            "verdict": "PASS",
            "maker": maker,
            "reviewer": {"id": reviewer_id, "lineage": "fixture"} if include_reviewer else None,
            "resolution_sha256": knowledge.resolution_binding_sha256(payload),
            "evidence": (
                {"path": self.review_evidence.relative_to(self.vault).as_posix(), "sha256": sha(self.review_evidence.read_bytes())}
                if include_evidence
                else None
            ),
        }
        write_json(self.review_path, review)
        return review

    def vault_bytes(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.vault).as_posix(): path.read_bytes()
            for path in self.vault.rglob("*")
            if path.is_file()
        }

    def resolve(self):
        return curation.resolve_disposition(self.resolution_path, self.review_path, self.lock_id, "codex")

    def health(self):
        return knowledge.curation_health(self.vault)


class SuccessfulResolutionTests(DispositionResolutionFixture):
    def test_resolution_survives_vault_relocation(self) -> None:
        self.resolve()
        relocated = self.root / "relocated-vault"
        shutil.copytree(self.vault, relocated)
        health = knowledge.curation_health(relocated)
        self.assertEqual(health["resolved_disposition_proposals"], 1)
        self.assertEqual(health["effective_unresolved_conflict_proposals"], 0)
        self.assertEqual(knowledge.lint(relocated)["error_count"], 0)

    def test_two_ledger_conflict_resolves_only_after_reviewed_append(self) -> None:
        health = self.health()
        self.assertEqual(health["conflicting_disposition_proposals"], 1)
        self.assertEqual(health["raw_conflict_proposals"], 1)
        self.assertEqual(health["effective_unresolved_conflict_proposals"], 1)
        self.assertEqual(health["effective_disposition_counts"]["unresolved"], 1)
        self.assertEqual(health["resolved_disposition_proposals"], 0)

        result = self.resolve()
        self.assertEqual(result["status"], "RESOLVED")
        self.assertEqual(result["proposal_id"], PID)
        self.assertEqual(result["effective_disposition"], "unresolved")

        health = self.health()
        self.assertEqual(health["conflicting_disposition_proposals"], 1)
        self.assertEqual(health["raw_conflict_proposals"], 1)
        self.assertEqual(health["effective_unresolved_conflict_proposals"], 0)
        self.assertEqual(health["resolved_conflict_proposals"], 1)
        self.assertEqual(health["resolved_disposition_proposals"], 1)
        self.assertEqual(health["effective_disposition_counts"]["unresolved"], 1)
        self.assertEqual(health["resolve_disposition_events"], 1)

    def test_old_ledgers_are_not_overwritten_or_deleted(self) -> None:
        self.resolve()
        after = self.vault_bytes()
        for name in (f"governance/curations/{EVT_A}.json", f"governance/curations/{EVT_B}.json"):
            self.assertEqual(after[name], self.before[name])
        self.assertIn(f"governance/curations/{EVT_A}.json", after)
        self.assertIn(f"governance/curations/{EVT_B}.json", after)
        self.assertIn(f"governance/resolutions/{RES_A}.json", after)

    def test_resolution_record_embeds_independent_reviewer_proof(self) -> None:
        result = self.resolve()
        record = json.loads(Path(result["record_path"]).read_text(encoding="utf-8"))
        self.assertEqual(record["proposal_id"], PID)
        self.assertEqual(record["review"]["verdict"], "PASS")
        self.assertEqual(record["review"]["reviewer"]["id"], "fixture-reviewer")
        self.assertNotEqual(record["review"]["reviewer"]["id"], record["review"]["maker"])
        self.assertEqual(record["review"]["reviewer"]["id"] != record["agent"], True)
        self.assertEqual(len(record["supersedes"]), 2)
        self.assertEqual(record["effective_disposition"], "unresolved")

    def test_cli_entrypoint_is_usable(self) -> None:
        exit_code = curation.main(
            [
                "resolve-disposition",
                "--resolution", str(self.resolution_path),
                "--review", str(self.review_path),
                "--lock-id", self.lock_id,
                "--agent", "codex",
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue((self.vault / "governance" / "resolutions" / f"{RES_A}.json").is_file())

    def test_lint_accepts_valid_resolution_and_reports_counts(self) -> None:
        self.resolve()
        result = knowledge.lint(self.vault)
        self.assertEqual(result["error_count"], 0, result["errors"])
        self.assertEqual(result["raw_conflict_proposals"], 1)
        self.assertEqual(result["effective_unresolved_conflict_proposals"], 0)
        self.assertEqual(result["resolution_count"], 1)


class FailClosedResolutionTests(DispositionResolutionFixture):
    def assert_blocked_without_mutation(self, pattern: str) -> None:
        before = self.vault_bytes()
        with self.assertRaisesRegex(curation.CurationError, pattern):
            self.resolve()
        self.assertEqual(before, self.vault_bytes())

    def test_unknown_event_id_rejected(self) -> None:
        self.write_resolution(
            supersedes=[
                {"event_id": "evt-20260911T000000000Z-zzzz9999", "disposition": "synthesized"},
                {"event_id": EVT_B, "disposition": "unresolved"},
            ]
        )
        self.assert_blocked_without_mutation("partial|mismatched|unexpected|no accounted")

    def test_incorrect_disposition_rejected(self) -> None:
        self.write_resolution(
            supersedes=[
                {"event_id": EVT_A, "disposition": "historical"},
                {"event_id": EVT_B, "disposition": "unresolved"},
            ]
        )
        self.assert_blocked_without_mutation("partial|mismatched|unexpected")

    def test_partial_supersedes_rejected(self) -> None:
        self.write_resolution(supersedes=[{"event_id": EVT_A, "disposition": "synthesized"}])
        self.assert_blocked_without_mutation("partial")

    def test_duplicate_resolution_and_replay_rejected(self) -> None:
        self.resolve()
        before = self.vault_bytes()
        with self.assertRaisesRegex(curation.CurationError, "duplicate"):
            self.resolve()
        self.assertEqual(before, self.vault_bytes())

    def test_absent_reviewer_rejected(self) -> None:
        self.write_review(include_reviewer=False)
        self.assert_blocked_without_mutation("review|schema")

    def test_absent_evidence_rejected(self) -> None:
        self.write_review(include_evidence=False)
        self.assert_blocked_without_mutation("evidence|schema")

    def test_mutable_resolution_evidence_rejected(self) -> None:
        mutable = self.vault / "wiki" / "index.md"
        mutable.write_text("Mutable fixture.\n", encoding="utf-8")
        document = json.loads(self.resolution_path.read_text(encoding="utf-8"))
        document["evidence"] = {"path": "wiki/index.md", "sha256": sha(mutable.read_bytes())}
        write_json(self.resolution_path, document)
        self.write_review()
        self.before = self.vault_bytes()
        self.assert_blocked_without_mutation("Non-immutable")

    def test_mutable_reviewer_proof_rejected(self) -> None:
        mutable = self.vault / "wiki" / "index.md"
        mutable.write_text("Mutable fixture.\n", encoding="utf-8")
        review = json.loads(self.review_path.read_text(encoding="utf-8"))
        review["evidence"] = {"path": "wiki/index.md", "sha256": sha(mutable.read_bytes())}
        write_json(self.review_path, review)
        self.before = self.vault_bytes()
        self.assert_blocked_without_mutation("Non-immutable")

    def test_forged_review_identity_rejected(self) -> None:
        self.write_review(reviewer_id="codex")
        self.assert_blocked_without_mutation("distinct")

    def test_forged_review_binding_rejected(self) -> None:
        review = json.loads(self.review_path.read_text(encoding="utf-8"))
        review["resolution_sha256"] = "a" * 64
        write_json(self.review_path, review)
        self.assert_blocked_without_mutation("bound")

    def test_malformed_resolution_rejected(self) -> None:
        self.resolution_path.write_bytes(b"{not json")
        self.assert_blocked_without_mutation("JSON|invalid")

    def test_missing_reason_or_effective_disposition_rejected(self) -> None:
        document = json.loads(self.resolution_path.read_text(encoding="utf-8"))
        document["reason"] = ""
        write_json(self.resolution_path, document)
        self.write_review()
        self.assert_blocked_without_mutation("reason")
        self.write_resolution(effective_disposition="accepted")
        self.assert_blocked_without_mutation("effective disposition")

    def test_changed_evidence_hash_rejected(self) -> None:
        self.evidence.write_text("Changed evidence.\n", encoding="utf-8")
        self.assert_blocked_without_mutation("evidence")

    def test_wrong_lock_or_machine_rejected(self) -> None:
        before = self.vault_bytes()
        with self.assertRaisesRegex(curation.CurationError, "lock|machine"):
            curation.resolve_disposition(self.resolution_path, self.review_path, "wrong-lock", "codex")
        self.assertEqual(before, self.vault_bytes())
        with mock.patch.object(curation.knowledge, "local_machine_name", return_value="OTHER"):
            with self.assertRaisesRegex(curation.CurationError, "machine"):
                self.resolve()
        self.assertEqual(before, self.vault_bytes())

    def test_ambiguous_duplicate_supersedes_pair_rejected(self) -> None:
        self.write_resolution(
            supersedes=[
                {"event_id": EVT_A, "disposition": "synthesized"},
                {"event_id": EVT_A, "disposition": "synthesized"},
                {"event_id": EVT_B, "disposition": "unresolved"},
            ]
        )
        self.assert_blocked_without_mutation("ambiguous")

    def test_resolution_for_unaccounted_proposal_rejected(self) -> None:
        self.write_resolution(proposal_id=PID2)
        self.assert_blocked_without_mutation("no accounted|mismatched|unexpected|partial")

    def test_symlinked_resolution_record_rejected(self) -> None:
        result = self.resolve()
        target = Path(result["record_path"])
        external = self.candidate / "external-resolution.json"
        external.write_bytes(target.read_bytes())
        target.unlink()
        try:
            target.symlink_to(external)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        lint_result = knowledge.lint(self.vault)
        self.assertGreater(lint_result["error_count"], 0)
        self.assertTrue(any("regular vault path" in error for error in lint_result["errors"]))
        health = self.health()
        self.assertEqual(health["resolved_disposition_proposals"], 0)

    def test_forged_written_resolution_record_rejected_by_lint(self) -> None:
        result = self.resolve()
        record_path = Path(result["record_path"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["review"]["reviewer"]["id"] = record["review"]["maker"]
        write_json(record_path, record)
        lint_result = knowledge.lint(self.vault)
        self.assertGreater(lint_result["error_count"], 0)
        self.assertTrue(any("Forged" in error for error in lint_result["errors"]))
        health = self.health()
        self.assertEqual(health["resolved_disposition_proposals"], 0)
        self.assertEqual(health["effective_unresolved_conflict_proposals"], 1)

    def test_stale_mismatched_written_pairs_rejected_by_lint(self) -> None:
        result = self.resolve()
        record_path = Path(result["record_path"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["supersedes"] = [{"event_id": EVT_A, "disposition": "synthesized"}]
        write_json(record_path, record)
        lint_result = knowledge.lint(self.vault)
        self.assertGreater(lint_result["error_count"], 0)
        self.assertTrue(any("Partial" in error for error in lint_result["errors"]))
        health = self.health()
        self.assertEqual(health["resolved_disposition_proposals"], 0)

    def test_effective_disposition_never_silently_selects_synthesized(self) -> None:
        health = self.health()
        self.assertEqual(health["effective_disposition_counts"].get("synthesized", 0), 0)
        self.assertEqual(health["effective_disposition_counts"]["unresolved"], 1)

    def test_changed_effective_disposition_detected_by_lint(self) -> None:
        self.resolve()
        record_path = self.vault / "governance" / "resolutions" / f"{RES_A}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["effective_disposition"] = "synthesized"
        write_json(record_path, record)
        lint_result = knowledge.lint(self.vault)
        self.assertGreater(lint_result["error_count"], 0)
        self.assertTrue(any("Forged" in error or "content mismatch" in error for error in lint_result["errors"]))
        health = self.health()
        self.assertEqual(health["resolved_disposition_proposals"], 0)
        self.assertEqual(health["effective_unresolved_conflict_proposals"], 1)

    def test_symlinked_additional_record_invalidates_valid_resolution(self) -> None:
        self.resolve()
        bad_path = self.vault / "governance" / "resolutions" / "res-20260911T000000000Z-eeee0000.json"
        external = self.candidate / "external-bad.json"
        external.write_text("{}", encoding="utf-8")
        try:
            bad_path.symlink_to(external)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        lint_result = knowledge.lint(self.vault)
        self.assertGreater(lint_result["error_count"], 0)
        health = self.health()
        self.assertEqual(health["resolved_disposition_proposals"], 0)
        self.assertEqual(health["effective_unresolved_conflict_proposals"], 1)

    def test_list_in_effective_disposition_controlled_error(self) -> None:
        document = json.loads(self.resolution_path.read_text(encoding="utf-8"))
        document["effective_disposition"] = ["unresolved"]
        write_json(self.resolution_path, document)
        self.write_review()
        self.assert_blocked_without_mutation("effective disposition|invalid")

    def test_list_in_effective_disposition_detected_by_lint(self) -> None:
        self.resolve()
        record_path = self.vault / "governance" / "resolutions" / f"{RES_A}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["effective_disposition"] = ["unresolved"]
        write_json(record_path, record)
        lint_result = knowledge.lint(self.vault)
        self.assertGreater(lint_result["error_count"], 0)
        health = self.health()
        self.assertEqual(health["resolved_disposition_proposals"], 0)


class RecoveryJournalTests(DispositionResolutionFixture):
    def test_failed_event_write_archives_own_record_and_retry_succeeds(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "vault"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-q", "-m", "Synthetic baseline"], cwd=self.root, check=True)
        original = curation._create_resolution_output

        def fail_on_event(target, content, staging_dir, *, guard):
            if target.name.startswith("evt-"):
                raise OSError("synthetic event write failure")
            return original(target, content, staging_dir, guard=guard)

        with mock.patch.object(curation, "_create_resolution_output", side_effect=fail_on_event), \
             mock.patch.object(Path, "unlink", side_effect=AssertionError("vault deletion forbidden")):
            with self.assertRaisesRegex(curation.CurationError, "archived without deletion"):
                self.resolve()
        self.assertFalse((self.vault / "governance" / "resolutions" / f"{RES_A}.json").exists())
        archived = list((self.vault / "governance").glob(f"_do-usuniecia-*/{RES_A}/record.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(self.health()["effective_unresolved_conflict_proposals"], 1)
        self.assertEqual(knowledge.lint(self.vault)["error_count"], 0)
        self.assertEqual(self.resolve()["status"], "RESOLVED")

    def test_forged_journal_cannot_displace_complete_existing_resolution(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "vault"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-q", "-m", "Synthetic baseline"], cwd=self.root, check=True)
        first = self.resolve()
        record_path = Path(first["record_path"])
        event_path = Path(first["event_path"])
        before = self.vault_bytes()
        journal_path = self.candidate / f".resolution-tx-{RES_A}.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["status"] = "applying"
        write_json(journal_path, journal)
        with self.assertRaises(curation.CurationError):
            self.resolve()
        self.assertEqual(before, self.vault_bytes())
        self.assertTrue(record_path.exists())
        self.assertTrue(event_path.exists())
        self.assertEqual(self.health()["resolved_disposition_proposals"], 1)

    def test_forged_journal_with_missing_event_path_cannot_archive_live_pair(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "vault"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-q", "-m", "Synthetic baseline"], cwd=self.root, check=True)
        first = self.resolve()
        journal_path = self.candidate / f".resolution-tx-{RES_A}.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["status"] = "applying"
        journal["event_path"] = f"events/2025/09/{first['event_id']}.json"
        journal["event_sha256"] = "f" * 64
        write_json(journal_path, journal)
        before_files = self.vault_bytes()
        before_dirs = {p.relative_to(self.vault).as_posix() for p in self.vault.rglob("*") if p.is_dir()}
        with self.assertRaisesRegex(curation.CurationError, "event path does not match the record"):
            self.resolve()
        self.assertEqual(before_files, self.vault_bytes())
        self.assertEqual(before_dirs, {p.relative_to(self.vault).as_posix() for p in self.vault.rglob("*") if p.is_dir()})
        self.assertTrue(Path(first["record_path"]).exists())
        self.assertTrue(Path(first["event_path"]).exists())

    def test_failed_first_write_does_not_create_empty_archive(self) -> None:
        before_dirs = {p.relative_to(self.vault).as_posix() for p in self.vault.rglob("*") if p.is_dir()}
        with mock.patch.object(curation, "_create_resolution_output", side_effect=OSError("synthetic first write failure")):
            with self.assertRaisesRegex(curation.CurationError, "archived without deletion"):
                self.resolve()
        self.assertEqual(before_dirs, {p.relative_to(self.vault).as_posix() for p in self.vault.rglob("*") if p.is_dir()})
        self.assertFalse(list((self.vault / "governance").glob("_do-usuniecia-*/" + RES_A)))

    def test_forged_journal_cannot_archive_git_tracked_resolution(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "vault"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-q", "-m", "Synthetic baseline"], cwd=self.root, check=True)
        first = self.resolve()
        subprocess.run(["git", "add", "vault"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "-q", "-m", "Published resolution"], cwd=self.root, check=True)
        journal_path = self.candidate / f".resolution-tx-{RES_A}.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["status"] = "applying"
        write_json(journal_path, journal)
        before = self.vault_bytes()
        with self.assertRaisesRegex(curation.CurationError, "tracked resolution output"):
            self.resolve()
        self.assertEqual(before, self.vault_bytes())
        self.assertTrue(Path(first["record_path"]).exists())

    def test_partial_journal_without_git_keeps_output_in_place(self) -> None:
        journal_path = self.candidate / f".resolution-tx-{RES_A}.json"
        partial = self.vault / "governance" / "resolutions" / f"{RES_A}.json"
        write_json(partial, {"resolution_id": RES_A, "event_id": EVT_A})
        write_json(journal_path, {
            "schema_version": 1, "resolution_id": RES_A, "status": "applying",
            "vault": str(self.vault), "record_path": f"governance/resolutions/{RES_A}.json",
            "event_path": "events/2026/09/evt-20260924T000000000Z-ffffffff.json",
            "record_sha256": sha(partial.read_bytes()), "event_sha256": "f" * 64,
            "archive_dir": f"governance/_do-usuniecia-20260924/{RES_A}",
        })
        before = partial.read_bytes()
        with mock.patch.object(knowledge, "find_git_root", return_value=None):
            with self.assertRaisesRegex(curation.CurationError, "without Git"):
                self.resolve()
        self.assertEqual(partial.read_bytes(), before)

    def test_forged_journal_cannot_delete_vault_config(self) -> None:
        config_path = self.vault / "knowledge-vault.json"
        config_bytes = config_path.read_bytes()
        journal_path = self.candidate / f".resolution-tx-{RES_A}.json"
        forged_journal = {
            "schema_version": 1,
            "resolution_id": RES_A,
            "status": "planned",
            "vault": str(self.vault),
            "record_path": "knowledge-vault.json",
            "event_path": "events/2026/09/evt-20260911T000000000Z-aaaa0000.json",
            "record_sha256": sha(config_bytes),
            "event_sha256": "b" * 64,
        }
        write_json(journal_path, forged_journal)
        before = self.vault_bytes()
        with self.assertRaises(curation.CurationError):
            self.resolve()
        self.assertEqual(before, self.vault_bytes())
        self.assertTrue(config_path.is_file())

    def test_forged_journal_cannot_delete_outside_resolutions_or_events(self) -> None:
        proposal_bytes = self.proposal.read_bytes()
        journal_path = self.candidate / f".resolution-tx-{RES_A}.json"
        forged_journal = {
            "schema_version": 1,
            "resolution_id": RES_A,
            "status": "planned",
            "vault": str(self.vault),
            "record_path": f"governance/curations/{EVT_A}.json",
            "event_path": "events/2026/09/evt-20260911T000000000Z-aaaa0000.json",
            "record_sha256": sha(proposal_bytes),
            "event_sha256": "b" * 64,
        }
        write_json(journal_path, forged_journal)
        before = self.vault_bytes()
        with self.assertRaises(curation.CurationError):
            self.resolve()
        self.assertEqual(before, self.vault_bytes())


if __name__ == "__main__":
    unittest.main()
