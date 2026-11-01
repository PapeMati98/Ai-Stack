"""Synthetic fixtures only, with real gateway lint."""
import hashlib
import importlib.util
import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("curation", HERE / "curation.py")
curation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(curation)
SID = "src-20260911T000000000Z-evidence-aaaa0000"
PID = "prop-20260911T000000000Z-bbbb0000"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def page(title, kind="system", producer=False):
    fields = {"title": title, "type": kind, "status": "reviewed" if producer else "verified",
              "summary": "Dated evidence", "last_verified": "2026-09-11", "sources": [SID] if producer else []}
    if kind == "project":
        fields["project_slug"] = "test"
    if producer:
        fields.update(verification_scope="producer-reports", last_reviewed="2026-09-11")
    return ("---\n" + "\n".join(f"{k}: {json.dumps(v)}" for k, v in fields.items())
            + "\n---\n\n# " + title + "\n").encode()


class CurationFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.source_dir = self.vault / "raw/sources/2026/09" / SID
        for d in (self.source_dir, self.vault / "proposals/codex", self.vault / "wiki",
                  self.vault / "governance", self.vault / "events/2026/09"):
            d.mkdir(parents=True, exist_ok=True)
        self.original = self.source_dir / "original.md"
        self.original.write_bytes(b"owner evidence\r\n")
        self.meta = self.source_dir / "source.json"
        self.metadata = {"schema_version": 1, "source_id": SID, "title": "Evidence", "origin": "fixture",
                         "captured_at": "2026-09-11T00:00:00Z", "captured_by": "codex", "classification": "internal",
                         "local_only": False, "original_file": "original.md", "byte_length": self.original.stat().st_size,
                         "sha256": sha(self.original.read_bytes()), "trust": "untrusted", "risk_flags": []}
        write_json(self.meta, self.metadata)
        self.proposal = self.vault / "proposals/codex" / (PID + "-test.md")
        self.proposal.write_text(
            f"---\nid: {PID}\ntype: proposal\nstatus: proposed\ntitle: Evidence\nagent: codex\n"
            f"created_at: 2026-09-11\nclassification: internal\nsource_ids: [{SID}]\ntarget_pages: []\n---\n\nEvidence.\n",
            encoding="utf-8")
        write_json(self.vault / "knowledge-vault.json", {
            "curation_scope": "designated-machine-only", "curator_machine": "TEST-MACHINE"})
        self.lock_id = "lock-1"
        self.set_lock(self.lock_id)
        for i in range(6):
            (self.vault / f"wiki/existing-{i}.md").write_bytes(page(f"Existing {i}"))
        (self.vault / "wiki/index.md").write_bytes(b"# Old index\r\n")
        (self.vault / "log.md").write_bytes(b"# Old log\r\n")
        write_json(self.vault / "events/2026/09/evt-existing.json", {
            "event_id": "evt-existing", "occurred_at": "2026-09-11T00:00:00Z", "agent": "codex",
            "action": "stage-source", "summary": "Earlier evidence", "source_ids": [SID],
            "proposal_ids": [], "pages_changed": []})
        # The cooperative writer fence file is persistent infrastructure that
        # survives any rollback, so the exact-bytes fixtures include it.
        (self.vault / '.local').mkdir()
        (self.vault / '.local/event-write-fence.lock').write_bytes(b"0")
        self.candidate = self.root / "candidate"
        self.candidate.mkdir()
        self.manifest_path = self.candidate / "publication.json"
        self.review_path = self.candidate / "review.json"
        self.evidence = self.candidate / "review-evidence.txt"
        self.evidence.write_text("Synthetic completed review fixture.\n", encoding="utf-8")
        self.manifest = {"schema_version": 1, "vault_path": str(self.vault), "snapshot_sha256": "a" * 64,
                         "inputs": [], "outputs": [], "source_ids": [SID], "proposal_ids": [PID],
                         "ledger_path": "ledger.json", "ledger_sha256": "", "summary": "Dated source synthesis."}
        self.freeze_inputs()
        self.add_output("wiki/test.md", page("Test project", "project", True))
        self.add_output("wiki/history/test.md", page("Test history", "evidence-ledger", True))
        self.ledger = {"schema_version": 1, "snapshot_sha256": "a" * 64, "captured_at": "2026-09-11T00:00:00Z",
                       "source_count": 1, "proposal_count": 1, "review_scope": "Producer reports only.",
                       "sources": [{"source_id": SID, "disposition": "synthesized", "reason": "Original retained.",
                                    "sha256": self.metadata["sha256"], "path": self.original.relative_to(self.vault).as_posix()}],
                       "proposals": [{"proposal_id": PID, "disposition": "synthesized", "reason": "Dated claim represented.",
                                      "sha256": sha(self.proposal.read_bytes()), "source_ids": [SID],
                                      "history_path": "wiki/history/test.md"}]}
        self.save_candidate()
        for p in (mock.patch.object(curation, "DEFAULT_VAULT", self.vault),
                  mock.patch.object(curation.knowledge, "local_machine_name", return_value="TEST-MACHINE")):
            p.start()
            self.addCleanup(p.stop)
        self.before = self.vault_bytes()

    def set_lock(self, ident):
        write_json(self.vault / "governance/curator.lock", {"lock_id": ident, "machine": "TEST-MACHINE",
                   "agent": "codex", "acquired_at": curation.knowledge.iso_time(curation.knowledge.now_utc())})

    def freeze_inputs(self):
        self.manifest["inputs"] = [{"path": p.relative_to(self.vault).as_posix(), "sha256": sha(p.read_bytes()),
                                    "bytes": p.stat().st_size} for p in (self.original, self.meta, self.proposal)]

    def add_output(self, name, data):
        target = self.vault / name
        payload = self.candidate / "payload" / name
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(data)
        self.manifest["outputs"].append({"path": name, "payload_path": payload.relative_to(self.candidate).as_posix(),
                                         "sha256": sha(data), "preimage_sha256": sha(target.read_bytes()) if target.exists() else None})

    def save_candidate(self):
        write_json(self.candidate / "ledger.json", self.ledger)
        self.manifest["ledger_sha256"] = sha((self.candidate / "ledger.json").read_bytes())
        write_json(self.manifest_path, self.manifest)
        write_json(self.review_path, {"schema_version": 1, "verdict": "PASS", "maker": "codex-hub-owner",
                   "reviewer": {"id": "fixture-reviewer", "lineage": "fixture"},
                   "manifest_sha256": sha(self.manifest_path.read_bytes()),
                   "evidence": {"path": self.evidence.name, "sha256": sha(self.evidence.read_bytes())}})

    def vault_bytes(self):
        return {p.relative_to(self.vault).as_posix(): p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}

    def validate(self):
        return curation.validate(self.manifest_path, self.review_path, self.lock_id, "codex")

    def publish(self):
        return curation.publish(self.manifest_path, self.review_path, self.lock_id, "codex")

    def test_real_lint_publish_rollback_exact_bytes(self):
        self.assertEqual(curation.knowledge.lint(self.vault)["error_count"], 0)
        result = self.publish()
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(curation.knowledge.lint(self.vault)["error_count"], 0)
        curation.rollback(Path(result["transaction"]), self.lock_id, "codex")
        self.assertEqual(self.before, self.vault_bytes())
        self.assertEqual(curation.knowledge.lint(self.vault)["error_count"], 0)

    def test_preflight_lint_before_any_write(self):
        (self.vault / "wiki/existing-0.md").write_bytes(b"missing frontmatter")
        before = self.vault_bytes()
        with self.assertRaisesRegex(curation.CurationError, "lint"):
            self.publish()
        self.assertEqual(before, self.vault_bytes())
        self.assertFalse((self.candidate / ".curation-transactions").exists())

    def test_rejects_proposal_already_accounted_by_prior_publication(self):
        first = self.publish()
        self.assertEqual(first["status"], "PUBLISHED")
        for output in self.manifest["outputs"]:
            output["preimage_sha256"] = sha((self.vault / output["path"]).read_bytes())
        self.save_candidate()
        before = self.vault_bytes()
        with self.assertRaisesRegex(curation.CurationError, "already accounted"):
            self.validate()
        self.assertEqual(before, self.vault_bytes())

    def test_source_only_correction_allows_previously_accounted_source(self):
        first = self.publish()
        self.assertEqual(first["status"], "PUBLISHED")
        self.manifest["proposal_ids"] = []
        self.manifest["inputs"] = [
            row for row in self.manifest["inputs"]
            if row["path"] != self.proposal.relative_to(self.vault).as_posix()
        ]
        self.ledger["proposal_count"] = 0
        self.ledger["proposals"] = []
        for output in self.manifest["outputs"]:
            output["preimage_sha256"] = sha((self.vault / output["path"]).read_bytes())
        self.save_candidate()
        self.validate()

    def test_complete_staged_overlay_lint_before_write(self):
        payload = self.candidate / self.manifest["outputs"][0]["payload_path"]
        payload.write_bytes(b"missing frontmatter")
        self.manifest["outputs"][0]["sha256"] = sha(payload.read_bytes())
        self.save_candidate()
        with self.assertRaisesRegex(curation.CurationError, "lint"):
            self.publish()
        self.assertEqual(self.before, self.vault_bytes())
        self.assertFalse((self.candidate / ".curation-transactions").exists())

    def test_overlay_links_unchanged_source_and_proposal_inputs(self):
        ctx = self.validate()
        checked = {}
        original = curation.knowledge.lint

        def inspect_overlay(vault, **kwargs):
            checked["external_parent"] = Path(vault).parent == ctx["manifest_path"].parent
            checked["source"] = os.path.samefile(vault / self.original.relative_to(self.vault), self.original)
            checked["proposal"] = os.path.samefile(vault / self.proposal.relative_to(self.vault), self.proposal)
            return original(vault, **kwargs)

        with mock.patch.object(curation.knowledge, "lint", side_effect=inspect_overlay):
            curation._lint_overlay(ctx)
        self.assertTrue(checked["external_parent"])
        if not checked["source"] and not checked["proposal"]:
            self.skipTest("same-volume hardlinks are unavailable")
        self.assertEqual(checked["source"], checked["proposal"])
        self.assertTrue(checked["source"])

    def test_overlay_copy_fallback_when_hardlinks_are_unavailable(self):
        ctx = self.validate()
        checked = {}
        original = curation.knowledge.lint

        def inspect_overlay(vault, **kwargs):
            checked["source"] = os.path.samefile(vault / self.original.relative_to(self.vault), self.original)
            checked["proposal"] = os.path.samefile(vault / self.proposal.relative_to(self.vault), self.proposal)
            return original(vault, **kwargs)

        with mock.patch.object(curation.os, "link", side_effect=OSError(errno.EXDEV, "cross-device")), \
                mock.patch.object(curation.knowledge, "lint", side_effect=inspect_overlay):
            curation._lint_overlay(ctx)
        self.assertEqual(checked, {"source": False, "proposal": False})

    def test_overlay_refuses_source_changed_since_snapshot(self):
        ctx = self.validate()
        original = self.original.read_bytes()
        try:
            self.original.write_bytes(original + b"changed")
            with self.assertRaisesRegex(curation.CurationError, "changed after snapshot"):
                curation._lint_overlay(ctx)
        finally:
            self.original.write_bytes(original)

    def test_metadata_and_proposal_must_be_frozen(self):
        for path in (self.meta, self.proposal):
            with self.subTest(path=path.name):
                saved = list(self.manifest["inputs"])
                self.manifest["inputs"] = [r for r in saved if r["path"] != path.relative_to(self.vault).as_posix()]
                self.save_candidate()
                with self.assertRaisesRegex(curation.CurationError, "frozen"):
                    self.validate()
                self.manifest["inputs"] = saved

    def test_changed_source_metadata_or_proposal(self):
        for path in (self.original, self.meta, self.proposal):
            with self.subTest(path=path.name):
                saved = path.read_bytes()
                path.write_bytes(saved + b"\n")
                with self.assertRaisesRegex(curation.CurationError, "input"):
                    self.validate()
                path.write_bytes(saved)

    def test_original_matches_metadata_even_with_rebound_inputs(self):
        self.metadata["sha256"] = "d" * 64
        write_json(self.meta, self.metadata)
        self.freeze_inputs()
        self.save_candidate()
        with self.assertRaisesRegex(curation.CurationError, "source original"):
            self.validate()

    def test_wrong_machine_lock_and_environment_spoof(self):
        with mock.patch.dict("os.environ", {"COMPUTERNAME": "TEST-MACHINE"}), mock.patch.object(
                curation.knowledge, "local_machine_name", return_value="OTHER"):
            with self.assertRaisesRegex(curation.CurationError, "machine"):
                self.validate()
        with self.assertRaisesRegex(curation.CurationError, "lock"):
            curation.validate(self.manifest_path, self.review_path, "wrong", "codex")

    def test_review_raw_hash_evidence_and_identity(self):
        self.manifest_path.write_bytes(self.manifest_path.read_bytes() + b"\n")
        with self.assertRaisesRegex(curation.CurationError, "manifest"):
            self.validate()
        self.save_candidate()
        self.evidence.write_bytes(b"changed")
        with self.assertRaisesRegex(curation.CurationError, "evidence"):
            self.validate()
        self.save_candidate()
        review = json.loads(self.review_path.read_text())
        review["reviewer"]["id"] = review["maker"].upper()
        write_json(self.review_path, review)
        with self.assertRaisesRegex(curation.CurationError, "distinct"):
            self.validate()

    def test_reserved_or_ambiguous_output_paths(self):
        for name in ("wiki/index.md", "wiki/INDEX.md", "wiki/test.md.", "wiki/dir /test.md", "wiki/AUX.md",
                     "wiki/a:b.md", "wiki/a?.md", "wiki/../test.md", "wiki/a\\b.md", "raw/test.md"):
            with self.subTest(name=name):
                self.manifest["outputs"][0]["path"] = name
                self.save_candidate()
                with self.assertRaises(curation.CurationError):
                    self.validate()

    def test_casefold_duplicate_outputs(self):
        self.manifest["outputs"].append(dict(self.manifest["outputs"][0], path="wiki/TEST.md"))
        self.save_candidate()
        with self.assertRaisesRegex(curation.CurationError, "duplicate|case"):
            self.validate()

    def test_symlink_payload(self):
        link = self.candidate / "linked.md"
        try:
            link.symlink_to(self.candidate / self.manifest["outputs"][0]["payload_path"])
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        self.manifest["outputs"][0]["payload_path"] = link.name
        self.save_candidate()
        with self.assertRaisesRegex(curation.CurationError, "symlink|reparse"):
            self.validate()

    def test_ledger_disposition_reason_history_snapshot(self):
        for field, value in (("disposition", "accepted"), ("reason", ""), ("history_path", "raw/original.md"),
                             ("history_path", "wiki/history/missing.md")):
            with self.subTest(field=field):
                saved = self.ledger["proposals"][0][field]
                self.ledger["proposals"][0][field] = value
                self.save_candidate()
                with self.assertRaisesRegex(curation.CurationError, "ledger|history"):
                    self.validate()
                self.ledger["proposals"][0][field] = saved
        self.ledger["snapshot_sha256"] = "b" * 64
        self.save_candidate()
        with self.assertRaisesRegex(curation.CurationError, "snapshot"):
            self.validate()

    def test_index_and_complete_governance_record(self):
        self.add_output("wiki/existing-0.md", page("Updated system"))
        self.save_candidate()
        result = self.publish()
        index = (self.vault / "wiki/index.md").read_text()
        for i in range(6):
            self.assertEqual(index.count(f"](existing-{i}.md)"), 1)
        self.assertIn("[Updated system](existing-0.md) | system | verified", index)
        history = next(line for line in index.splitlines() if "](history/test.md)" in line)
        self.assertIn("evidence-ledger", history)
        self.assertIn("producer-reports", history)
        self.assertIn("not independently verified", history)
        self.assertIn("Earlier evidence", (self.vault / "log.md").read_text())
        ledger = json.loads((self.vault / "governance/curations" / (result["event_id"] + ".json")).read_text())
        for key in ("sources", "proposals", "snapshot_sha256"):
            self.assertEqual(ledger[key], self.ledger[key])
        self.assertEqual(ledger["review"], json.loads(self.review_path.read_text()))
        self.assertEqual(ledger["counts"]["proposals"]["synthesized"], 1)

    def test_preimage_change_blocked_and_exact_restore(self):
        self.add_output("wiki/existing-0.md", page("Replacement"))
        self.save_candidate()
        target = self.vault / "wiki/existing-0.md"
        old = target.read_bytes()
        target.write_bytes(page("Concurrent"))
        with self.assertRaisesRegex(curation.CurationError, "preimage"):
            self.validate()
        target.write_bytes(old)
        result = self.publish()
        curation.rollback(Path(result["transaction"]), self.lock_id, "codex")
        self.assertEqual(self.before, self.vault_bytes())

    def test_absent_target_race_at_atomic_replace(self):
        original = curation._atomic_replace
        target = self.vault / "wiki/test.md"
        def race(path, content, **kwargs):
            if path == target and not path.exists():
                path.write_bytes(b"foreign arrival")
            return original(path, content, **kwargs)
        with mock.patch.object(curation, "_atomic_replace", side_effect=race):
            with self.assertRaises(curation.CurationError):
                self.publish()
        self.assertEqual(target.read_bytes(), b"foreign arrival")

    def test_payload_race_at_atomic_replace(self):
        original = curation._atomic_replace
        payload = self.candidate / self.manifest["outputs"][0]["payload_path"]
        def race(path, content, **kwargs):
            if path == self.vault / "wiki/test.md":
                payload.write_bytes(b"altered payload")
            return original(path, content, **kwargs)
        with mock.patch.object(curation, "_atomic_replace", side_effect=race):
            with self.assertRaisesRegex(curation.CurationError, "payload"):
                self.publish()
        self.assertEqual(self.before, self.vault_bytes())

    def test_mid_publish_failure_rolls_back(self):
        original = curation._atomic_replace
        def fail(path, content, **kwargs):
            if path == self.vault / "wiki/history/test.md":
                raise OSError("injected publish failure")
            return original(path, content, **kwargs)
        with mock.patch.object(curation, "_atomic_replace", side_effect=fail):
            with self.assertRaisesRegex(curation.CurationError, "rolled back"):
                self.publish()
        self.assertEqual(self.before, self.vault_bytes())

    def test_unrelated_append_only_source_during_staging_does_not_block_publish(self):
        """A new source does not invalidate the curator's frozen read set."""
        source_id = "src-20260911T000000000Z-evidence-cccc0000"
        source_dir = self.vault / "raw/sources/2026/09" / source_id
        original = curation._write_journal

        def append_after_staging(path, journal):
            original(path, journal)
            if journal["status"] == "planned" and not source_dir.exists():
                source_dir.mkdir(parents=True)
                payload = source_dir / "original.md"
                payload.write_bytes(b"other evidence\n")
                metadata = dict(self.metadata, source_id=source_id,
                                byte_length=payload.stat().st_size,
                                sha256=sha(payload.read_bytes()))
                write_json(source_dir / "source.json", metadata)

        with mock.patch.object(curation, "_write_journal", side_effect=append_after_staging):
            result = self.publish()
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertTrue((source_dir / "source.json").is_file())
        self.assertEqual(curation.knowledge.lint(self.vault)["error_count"], 0)

    def test_new_event_during_staging_is_included_in_published_log(self):
        event = self.vault / "events/2026/09/evt-unrelated.json"
        source_id = "src-20260911T000000000Z-evidence-dddd0000"
        proposal_id = "prop-20260911T000000000Z-dddd0000"
        source_dir = self.vault / "raw/sources/2026/09" / source_id
        proposal = self.vault / "proposals/codex" / (proposal_id + "-other.md")
        original = curation._write_journal

        def append_after_staging(path, journal):
            original(path, journal)
            if journal["status"] == "planned" and not event.exists():
                source_dir.mkdir(parents=True)
                payload = source_dir / "original.md"
                payload.write_bytes(b"other evidence\n")
                metadata = dict(self.metadata, source_id=source_id,
                                byte_length=payload.stat().st_size,
                                sha256=sha(payload.read_bytes()))
                write_json(source_dir / "source.json", metadata)
                proposal.write_text(
                    f"---\nid: {proposal_id}\ntype: proposal\nstatus: proposed\ntitle: Other evidence\n"
                    f"agent: codex\ncreated_at: 2026-09-11\nclassification: internal\n"
                    f"source_ids: [{source_id}]\ntarget_pages: []\n---\n\nOther evidence.\n",
                    encoding="utf-8")
                write_json(event, {
                    "event_id": "evt-unrelated", "occurred_at": "2026-09-11T00:00:00Z",
                    "agent": "other", "action": "stage-source", "summary": "Other evidence",
                    "source_ids": [source_id], "proposal_ids": [proposal_id], "pages_changed": [],
                })

        with mock.patch.object(curation, "_write_journal", side_effect=append_after_staging):
            result = self.publish()
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertTrue(event.is_file())
        self.assertTrue(proposal.is_file())
        self.assertIn("Other evidence", (self.vault / "log.md").read_text(encoding="utf-8"))
        self.assertEqual(curation.knowledge.lint(self.vault)["error_count"], 0)

    def test_event_arriving_after_log_rebase_is_tolerated_and_included_once(self):
        """Valid append-only intake during apply no longer rolls back the publication.

        The late event is merged into the generated log exactly once by the
        fenced post-apply rebase, and the vault passes the final lint.
        """
        event = self.vault / "events/2026/09/evt-late.json"
        original = curation._write_journal

        def append_during_apply(path, journal):
            original(path, journal)
            if journal["status"] == "applying" and not event.exists():
                write_json(event, {
                    "event_id": "evt-late", "occurred_at": "2026-09-11T00:00:00Z",
                    "agent": "other", "action": "stage-source", "summary": "Late evidence",
                    "source_ids": [SID], "proposal_ids": [], "pages_changed": [],
                })

        with mock.patch.object(curation, "_write_journal", side_effect=append_during_apply):
            result = self.publish()
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertTrue(event.is_file())
        self.assertTrue((self.vault / "wiki/test.md").exists())
        log = (self.vault / "log.md").read_text(encoding="utf-8")
        self.assertEqual(log.count("Late evidence"), 1)
        self.assertEqual(curation.knowledge.lint(self.vault)["error_count"], 0)

    def test_existing_event_change_during_staging_still_blocks_publish(self):
        event = self.vault / "events/2026/09/evt-existing.json"
        original = curation._write_journal

        def change_after_staging(path, journal):
            original(path, journal)
            if journal["status"] == "planned":
                data = json.loads(event.read_text(encoding="utf-8"))
                data["summary"] = "Changed during publication"
                write_json(event, data)

        with mock.patch.object(curation, "_write_journal", side_effect=change_after_staging):
            with self.assertRaisesRegex(curation.CurationError, "baseline changed"):
                self.publish()
        self.assertFalse((self.vault / "wiki/test.md").exists())

    def test_new_wiki_page_during_staging_still_blocks_publish(self):
        foreign = self.vault / "wiki/foreign.md"
        original = curation._write_journal

        def add_foreign_page(path, journal):
            original(path, journal)
            if journal["status"] == "planned" and not foreign.exists():
                foreign.write_bytes(page("Foreign"))

        with mock.patch.object(curation, "_write_journal", side_effect=add_foreign_page):
            with self.assertRaisesRegex(curation.CurationError, "baseline changed"):
                self.publish()
        self.assertEqual(foreign.read_bytes(), page("Foreign"))
        self.assertFalse((self.vault / "wiki/test.md").exists())

    def test_crash_after_replace_before_journal_update(self):
        class Crash(BaseException):
            pass
        original = curation._write_journal
        def crash(path, journal):
            if any(r["state"] == "replaced" for r in journal["files"]):
                raise Crash()
            return original(path, journal)
        with mock.patch.object(curation, "_write_journal", side_effect=crash):
            with self.assertRaises(Crash):
                self.publish()
        transactions = list((self.candidate / ".curation-transactions").glob("tx-*.json"))
        transaction = next(p for p in transactions if not p.name.endswith(".manifest.json"))
        self.assertTrue((self.vault / "wiki/test.md").exists())
        curation.rollback(transaction, self.lock_id, "codex")
        self.assertEqual(self.before, self.vault_bytes())

    def test_lock_change_blocks_publish_and_automatic_rollback(self):
        original = curation._atomic_replace
        def race(path, content, **kwargs):
            if path == self.vault / "wiki/history/test.md":
                self.set_lock("foreign-lock")
            return original(path, content, **kwargs)
        with mock.patch.object(curation, "_atomic_replace", side_effect=race):
            with self.assertRaisesRegex(curation.CurationError, "rollback.*block|rollback.*fail"):
                self.publish()
        self.assertTrue((self.vault / "wiki/test.md").is_file())
        self.assertFalse((self.vault / "wiki/history/test.md").exists())

    def test_postwrite_inputs_rehashed_and_foreign_input_preserved(self):
        original = curation._atomic_replace
        altered = self.proposal.read_bytes() + b"\n"
        def race(path, content, **kwargs):
            result = original(path, content, **kwargs)
            if path == self.vault / "wiki/history/test.md":
                self.proposal.write_bytes(altered)
            return result
        with mock.patch.object(curation, "_atomic_replace", side_effect=race):
            with self.assertRaisesRegex(curation.CurationError, "input"):
                self.publish()
        expected = dict(self.before)
        expected[self.proposal.relative_to(self.vault).as_posix()] = altered
        self.assertEqual(expected, self.vault_bytes())

    def test_final_lint_failure_rolls_back(self):
        original = curation.knowledge.lint
        def lint(path):
            if path == self.vault and (path / "wiki/test.md").exists():
                return {"error_count": 1, "errors": ["injected final lint"]}
            return original(path)
        with mock.patch.object(curation.knowledge, "lint", side_effect=lint):
            with self.assertRaisesRegex(curation.CurationError, "lint"):
                self.publish()
        self.assertEqual(self.before, self.vault_bytes())

    def test_rollback_changed_output_not_clobbered(self):
        result = self.publish()
        (self.vault / "wiki/test.md").write_bytes(b"foreign edit")
        before = self.vault_bytes()
        with self.assertRaisesRegex(curation.CurationError, "foreign|concurrent"):
            curation.rollback(Path(result["transaction"]), self.lock_id, "codex")
        self.assertEqual(before, self.vault_bytes())

    def test_tampered_preimage_rejected_before_any_rollback_mutation(self):
        result = self.publish()
        transaction = Path(result["transaction"])
        journal = json.loads(transaction.read_text())
        row = next(r for r in journal["files"] if r["before"] is not None)
        (transaction.parent / row["preimage"]).write_bytes(b"tampered preimage")
        before = self.vault_bytes()
        with self.assertRaisesRegex(curation.CurationError, "preimage"):
            curation.rollback(transaction, self.lock_id, "codex")
        self.assertEqual(before, self.vault_bytes())

    def test_rollback_foreign_paths_hashes_and_duplicate_targets(self):
        result = self.publish()
        transaction = Path(result["transaction"])
        saved = transaction.read_bytes()
        before = self.vault_bytes()
        for mutate in (
                lambda j: j["files"][0].update(path=self.original.relative_to(self.vault).as_posix()),
                lambda j: j["files"][0].update(preimage=str(self.original)),
                lambda j: j.update(vault=str(self.root)),
                lambda j: j["files"].append(dict(j["files"][0], path=j["files"][0]["path"].upper())),
                lambda j: j["files"][0].update(after="invalid")):
            journal = json.loads(saved)
            mutate(journal)
            write_json(transaction, journal)
            with self.assertRaises(curation.CurationError):
                curation.rollback(transaction, self.lock_id, "codex")
            self.assertEqual(before, self.vault_bytes())

    def test_new_owned_lock_rollback_is_idempotent(self):
        result = self.publish()
        self.set_lock("new-owned")
        expected = dict(self.before)
        expected["governance/curator.lock"] = (self.vault / "governance/curator.lock").read_bytes()
        for _ in range(2):
            curation.rollback(Path(result["transaction"]), "new-owned", "codex")
            self.assertEqual(expected, self.vault_bytes())

    def test_resolution_operation_rejects_single_ledger_pair(self):
        result = self.publish()
        self.assertEqual(result["status"], "PUBLISHED")
        resolution = self.candidate / "single-resolution.json"
        source_id = "src-20260911T000000000Z-single-evidence-aabbccdd"
        package = self.vault / "raw" / "sources" / "2026" / "09" / source_id
        evidence = package / "original.txt"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("Synthetic single-pair evidence.\n", encoding="utf-8")
        write_json(package / "source.json", {
            "schema_version": 1, "source_id": source_id, "title": "Synthetic single-pair evidence",
            "origin": "synthetic fixture", "captured_at": "2026-09-11T00:00:00Z",
            "captured_by": "codex", "classification": "internal", "local_only": False,
            "original_file": "original.txt", "byte_length": len(evidence.read_bytes()),
            "sha256": sha(evidence.read_bytes()), "trust": "untrusted", "risk_flags": [],
        })
        document = {
            "schema_version": 1,
            "vault_path": str(self.vault),
            "resolution_id": "res-20260911T000000000Z-dddd0001",
            "proposal_id": PID,
            "supersedes": [{"event_id": result["event_id"], "disposition": "synthesized"}],
            "effective_disposition": "synthesized",
            "reason": "Synthetic single-pair attempt.",
            "evidence": {"path": evidence.relative_to(self.vault).as_posix(), "sha256": sha(evidence.read_bytes())},
        }
        write_json(resolution, document)
        review = self.candidate / "single-review.json"
        write_json(review, {
            "schema_version": 1, "verdict": "PASS", "maker": "codex-hub-owner",
            "reviewer": {"id": "fixture-reviewer", "lineage": "fixture"},
            "resolution_sha256": curation.knowledge.resolution_binding_sha256(
                {key: document[key] for key in curation.knowledge.RESOLUTION_BINDING_FIELDS}),
            "evidence": {"path": evidence.relative_to(self.vault).as_posix(), "sha256": sha(evidence.read_bytes())},
        })
        before = self.vault_bytes()
        with self.assertRaisesRegex(curation.CurationError, "at least two"):
            curation.resolve_disposition(resolution, review, self.lock_id, "codex")
        self.assertEqual(before, self.vault_bytes())


if __name__ == "__main__":
    unittest.main()
