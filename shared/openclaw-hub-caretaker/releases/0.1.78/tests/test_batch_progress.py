"""Per-member isolation of a poisoned batch before the first model call.

A batch that mixed one unsafe member with independent valid members was
rejected as a whole by the publication freeze, so the independent proposals
never advanced and every member of the batch was recorded as one failure.

The repair validates the dependency closure of every member before the first
model call, isolates exactly the bad members (and every member that shares
their bad source) with an explicit hash-specific outcome, and lets the
independent members advance in one reviewed subset transaction.

The fixtures are synthetic: a temporary vault with generated proposals and
sources, a stub curator and an in-memory database. No Hub, model, service,
network or real source body is used.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import maintenance
from caretaker.audit import ensure_tables, policy_from
from caretaker.pubstate import ensure_pub_tables

PROJECT = "northwind-app"
NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)
TASK_TMP = ROOT / ".test-tmp"


def _workspace():
    """Task-owned scratch directory inside the release tree."""
    TASK_TMP.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(TASK_TMP))


#: A source body that carries no secret, no reasoning marker and no raw tool
#: dump, so it passes every publication privacy check.
CLEAN_BODY = "# Session note\n\nA plain synthetic note about the project.\n"

#: A body that the publication filters refuse for memory publication.
SECRET_BODY = 'sk' + '-' + 'SYNTHETIC' + 'x' * 32


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _policy():
    return {
        "enabled": True,
        "include_existing_hub_backlog": True,
        "projects": [PROJECT],
        "held_projects": [],
        "min_ready_groups": 10,
        "max_wait_hours": 24,
        "max_batch": 8,
    }


def _summary(eligible):
    return {
        "trigger": "threshold",
        "eligible": list(eligible),
        "raw_eligible": len(eligible),
        "group_count": len(eligible),
        "ready_group_count": len(eligible),
        "oldest_age_hours": 48,
        "deferred": 0,
        "unsupported": 0,
        "policy": policy_from(_policy()),
    }


def _write_source(vault, sid, body=CLEAN_BODY, *, classification="internal",
                  local_only=False, sha_override=None, byte_length=None):
    """Write one synthetic source and return its frozen metadata."""
    folder = vault / "raw" / "sources" / "2026" / "10" / sid
    folder.mkdir(parents=True, exist_ok=True)
    data = body.encode("utf-8")
    meta = {
        "source_id": sid,
        "classification": classification,
        "local_only": local_only,
        "original_file": "original.md",
        "sha256": sha_override if sha_override is not None else _sha(data),
        "byte_length": byte_length if byte_length is not None else len(data),
    }
    (folder / "source.json").write_bytes(json.dumps(meta).encode("utf-8"))
    (folder / "original.md").write_bytes(data)
    return meta


def _write_proposal(vault, pid, source_ids, *, project=PROJECT,
                    classification="internal"):
    """Write one synthetic proposal and return its frozen member metadata."""
    path = vault / "proposals" / "codex" / (pid + ".md")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        f"id: {pid}\n"
        f"classification: {classification}\n"
        f"project_slug: {project}\n"
        "source_ids: " + json.dumps(list(source_ids)) + "\n"
        "---\n\n"
        f"# {pid}\n\nA synthetic proposal body.\n"
    )
    path.write_bytes(text.encode("utf-8"))
    return {
        "proposal_id": pid,
        "path": "proposals/codex/" + pid + ".md",
        "sha256": _sha(text.encode("utf-8")),
        "project": project,
        "producer": "codex",
        "episode": "",
        "source_ids": sorted(source_ids),
    }


def _db():
    conn = sqlite3.connect(":memory:")
    ensure_tables(conn)
    ensure_pub_tables(conn)
    return conn


def _member_status(conn, proposal_id):
    row = conn.execute(
        "SELECT status, reason, sha256 FROM audit_members WHERE proposal_id=?",
        (proposal_id,),
    ).fetchone()
    return row


def _build_mixed_batch(vault, count=8, bad_indexes=(3,), *, bad_kind="local_only",
                       bad_sid=None):
    """Create a batch of proposals where only some members are poisoned."""
    members = []
    bad_sid = bad_sid or "src-bad"
    if bad_kind == "local_only":
        _write_source(vault, bad_sid, local_only=True)
    elif bad_kind == "secret":
        _write_source(vault, bad_sid, SECRET_BODY)
    elif bad_kind == "hash":
        _write_source(vault, bad_sid, sha_override=_sha(b"different bytes"))
    elif bad_kind == "missing":
        pass
    for index in range(count):
        pid = f"prop-{index:02d}"
        sid = bad_sid if index in bad_indexes else f"src-{index:02d}"
        if sid != bad_sid:
            _write_source(vault, sid)
        members.append(_write_proposal(vault, pid, [sid]))
    return members


class BatchIsolationTests(unittest.TestCase):
    """A poisoned member must not reject the independent members of a batch."""

    def _run_tick(self, vault, members):
        """Run one publication tick with a stub curator; return (result, calls)."""
        db = _db()
        self.addCleanup(db.close)
        calls = []

        def invoke(config_path, paths):
            calls.append(list(paths))
            return {"status": "PUBLISHED"}

        cfg = {
            "hub": {"vault": str(vault)},
            "project_slug": PROJECT,
            "state_dir": str(vault.parent / "state"),
        }
        Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)
        with patch("maintenance._audit_summary", return_value=_summary(members)), \
                patch("maintenance._invoke_curate", side_effect=invoke):
            result = maintenance.publication_tick(
                "config", cfg, {"publication": _policy()}, db, now=NOW,
            )
        return result, calls, db

    def test_one_poisoned_member_isolated_and_the_rest_advance(self):
        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            members = _build_mixed_batch(vault, bad_indexes=(3,))
            good = [m for m in members if m["proposal_id"] != "prop-03"]
            result, calls, db = self._run_tick(vault, members)

            # Exactly one transaction, carrying only the independent members.
            self.assertEqual(len(calls), 1)
            self.assertEqual(sorted(calls[0]), sorted(m["path"] for m in good))
            self.assertNotIn("proposals/codex/prop-03.md", calls[0])
            self.assertEqual(result["status"], "PUBLISHED")
            self.assertEqual(result["model_calls"], 1)

            # The poisoned member keeps an explicit, hash-specific outcome.
            status, reason, digest = _member_status(db, "prop-03")
            self.assertEqual(status, "quarantined")
            self.assertTrue(reason.startswith("source_not_publishable"), reason)
            self.assertIn(members[3]["sha256"], reason)
            self.assertIn("src-bad", reason)
            # The rejected member keeps its frozen identity and hash.
            self.assertEqual(digest, members[3]["sha256"])
            # It is not relabelled to shrink the backlog.
            self.assertNotIn(status, {"historical", "synthesized", "accounted_elsewhere"})

            # The independent members advanced unchanged.
            for member in good:
                status, _, digest = _member_status(db, member["proposal_id"])
                self.assertEqual(status, "published")
                self.assertEqual(digest, member["sha256"])

            # The rejected member is reported, not silently dropped.
            self.assertEqual(
                [q["proposal_ids"] for q in result["quarantined"]], [["prop-03"]],
            )

    def test_shared_bad_source_isolates_every_member_that_depends_on_it(self):
        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            members = _build_mixed_batch(vault, bad_indexes=(0, 5))
            good = [m for m in members if m["proposal_id"] not in {"prop-00", "prop-05"}]
            result, calls, db = self._run_tick(vault, members)

            self.assertEqual(len(calls), 1)
            self.assertEqual(sorted(calls[0]), sorted(m["path"] for m in good))
            for pid in ("prop-00", "prop-05"):
                status, reason, _ = _member_status(db, pid)
                self.assertEqual(status, "quarantined")
                self.assertTrue(reason.startswith("source_not_publishable"), reason)
            self.assertEqual(result["status"], "PUBLISHED")

    def test_all_members_invalid_makes_no_model_call(self):
        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            members = []
            for index in range(8):
                pid = f"prop-{index:02d}"
                sid = f"src-bad-{index:02d}"
                _write_source(vault, sid, local_only=True)
                members.append(_write_proposal(vault, pid, [sid]))
            result, calls, db = self._run_tick(vault, members)

            self.assertEqual(calls, [])
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(result["status"], "QUARANTINED_MEMBERS_ONLY")
            for member in members:
                status, reason, _ = _member_status(db, member["proposal_id"])
                self.assertEqual(status, "quarantined")
                self.assertIn(member["sha256"], reason)
            # No transaction was started for an empty batch.
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0,
            )

    def test_wholly_valid_batch_keeps_the_single_batch_path(self):
        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            members = []
            for index in range(8):
                pid = f"prop-{index:02d}"
                sid = f"src-{index:02d}"
                _write_source(vault, sid)
                members.append(_write_proposal(vault, pid, [sid]))
            result, calls, db = self._run_tick(vault, members)

            self.assertEqual(len(calls), 1)
            self.assertEqual(sorted(calls[0]), sorted(m["path"] for m in members))
            self.assertEqual(result["status"], "PUBLISHED")
            self.assertEqual(result["model_calls"], 1)
            self.assertEqual(result.get("quarantined", []), [])
            for member in members:
                status, _, _ = _member_status(db, member["proposal_id"])
                self.assertEqual(status, "published")

    def test_hash_broken_source_isolated_with_both_hashes(self):
        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            members = _build_mixed_batch(vault, bad_indexes=(2,), bad_kind="hash")
            good = [m for m in members if m["proposal_id"] != "prop-02"]
            result, calls, db = self._run_tick(vault, members)

            self.assertEqual(len(calls), 1)
            self.assertEqual(sorted(calls[0]), sorted(m["path"] for m in good))
            status, reason, _ = _member_status(db, "prop-02")
            self.assertEqual(status, "quarantined")
            self.assertTrue(reason.startswith("source_integrity_mismatch"), reason)
            self.assertIn(members[2]["sha256"], reason)

    def test_duplicate_source_id_with_identical_metadata_is_isolated(self):
        from caretaker.audit import partition_batch_members, source_metadata_index
        from caretaker.publication import PublicationError, freeze

        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            duplicate_meta = _write_source(vault, "src-duplicate")
            duplicate_folder = (
                vault / "raw" / "sources" / "2026" / "11" / "src-duplicate-copy"
            )
            duplicate_folder.mkdir(parents=True)
            duplicate_meta_path = duplicate_folder / "source.json"
            duplicate_meta_path.write_bytes(json.dumps(duplicate_meta).encode("utf-8"))
            (duplicate_folder / "original.md").write_bytes(b"hash-bad duplicate bytes")
            _write_source(vault, "src-good")

            duplicate_paths = [
                path
                for path in (vault / "raw" / "sources").glob("*/*/*/source.json")
                if json.loads(path.read_text(encoding="utf-8"))["source_id"] == "src-duplicate"
            ]
            self.assertEqual(len(duplicate_paths), 2)
            self.assertNotEqual(sorted(duplicate_paths)[0], duplicate_paths[-1])
            self.assertEqual(
                json.loads(duplicate_paths[0].read_text(encoding="utf-8")),
                json.loads(duplicate_paths[1].read_text(encoding="utf-8")),
            )

            duplicate_members = [
                _write_proposal(vault, "prop-duplicate-a", ["src-duplicate"]),
                _write_proposal(vault, "prop-duplicate-b", ["src-duplicate"]),
            ]
            independent = _write_proposal(vault, "prop-independent", ["src-good"])

            # The unchanged freeze path is last-wins and sees the hash-bad copy.
            with self.assertRaisesRegex(PublicationError, "source integrity mismatch"):
                freeze(
                    vault,
                    [member["path"] for member in duplicate_members] + [independent["path"]],
                    f"wiki/projects/{PROJECT}.md",
                )

            eligible, isolated = partition_batch_members(
                vault, duplicate_members + [independent], PROJECT, None,
            )
            self.assertEqual(
                [member["proposal_id"] for member in eligible], ["prop-independent"],
            )
            self.assertEqual(
                [member["proposal_id"] for member in isolated],
                ["prop-duplicate-a", "prop-duplicate-b"],
            )
            self.assertTrue(all(
                member["code"] == "source_metadata_duplicate" for member in isolated
            ))

            result, calls, db = self._run_tick(
                vault, duplicate_members + [independent],
            )
            self.assertEqual(calls, [[independent["path"]]])
            self.assertEqual(result["status"], "PUBLISHED")
            self.assertEqual(result["model_calls"], 1)
            for member in duplicate_members:
                status, reason, _ = _member_status(db, member["proposal_id"])
                self.assertEqual(status, "quarantined")
                self.assertTrue(reason.startswith("source_metadata_duplicate"), reason)
            self.assertEqual(_member_status(db, "prop-independent")[0], "published")

    def test_missing_source_isolated_without_touching_the_others(self):
        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            members = _build_mixed_batch(vault, bad_indexes=(4,), bad_kind="missing")
            good = [m for m in members if m["proposal_id"] != "prop-04"]
            result, calls, db = self._run_tick(vault, members)

            self.assertEqual(len(calls), 1)
            self.assertEqual(sorted(calls[0]), sorted(m["path"] for m in good))
            status, reason, _ = _member_status(db, "prop-04")
            self.assertEqual(status, "quarantined")
            self.assertTrue(reason.startswith("source_missing"), reason)

    def test_changed_proposal_hash_isolates_only_that_member(self):
        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            members = _build_mixed_batch(vault, bad_indexes=())
            # The proposal changed after the cycle froze it: that member needs
            # a fresh review, the other members stay reviewable.
            members[6] = dict(members[6], sha256="b" * 64)
            good = [m for m in members if m["proposal_id"] != "prop-06"]
            result, calls, db = self._run_tick(vault, members)

            self.assertEqual(len(calls), 1)
            self.assertEqual(sorted(calls[0]), sorted(m["path"] for m in good))
            status, reason, digest = _member_status(db, "prop-06")
            self.assertEqual(status, "stale")
            self.assertEqual(reason, "frozen_hash_changed_needs_fresh_review")
            self.assertEqual(digest, "b" * 64)
            for member in good:
                status, _, _ = _member_status(db, member["proposal_id"])
                self.assertEqual(status, "published")
            self.assertEqual(result["status"], "PUBLISHED")


class MemberClosureTests(unittest.TestCase):
    """The dependency closure of one member decides its own eligibility."""

    def _member(self, vault, pid, sid, **kwargs):
        return _write_proposal(vault, pid, [sid], **kwargs)

    def test_valid_member_is_eligible_and_bad_members_are_not(self):
        from caretaker.audit import member_eligibility, source_metadata_index

        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            _write_source(vault, "src-good")
            _write_source(vault, "src-local", local_only=True)
            _write_source(vault, "src-secret", SECRET_BODY)
            _write_source(vault, "src-hash", sha_override=_sha(b"other"))
            index = source_metadata_index(vault)

            good = self._member(vault, "prop-good", "src-good")
            outcome = member_eligibility(vault, good, PROJECT, None, index)
            self.assertTrue(outcome["eligible"], outcome)

            for pid, sid, code in (
                ("prop-local", "src-local", "source_not_publishable"),
                ("prop-secret", "src-secret", "source_content_not_eligible"),
                ("prop-hash", "src-hash", "source_integrity_mismatch"),
            ):
                member = self._member(vault, pid, sid)
                outcome = member_eligibility(vault, member, PROJECT, None, index)
                self.assertFalse(outcome["eligible"])
                self.assertEqual(outcome["code"], code)
                self.assertIn(member["sha256"], outcome["reason"])

            missing = self._member(vault, "prop-missing", "src-absent")
            outcome = member_eligibility(vault, missing, PROJECT, None, index)
            self.assertFalse(outcome["eligible"])
            self.assertEqual(outcome["code"], "source_missing")

    def test_foreign_project_member_is_isolated(self):
        from caretaker.audit import member_eligibility, source_metadata_index

        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            _write_source(vault, "src-good")
            index = source_metadata_index(vault)
            member = _write_proposal(vault, "prop-other", ["src-good"],
                                     project="another-project")
            outcome = member_eligibility(vault, member, PROJECT, None, index)
            self.assertFalse(outcome["eligible"])
            self.assertEqual(outcome["code"], "proposal_targets_another_project")

    def test_malformed_source_metadata_fails_closed(self):
        from caretaker.audit import member_eligibility, source_metadata_index

        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            folder = vault / "raw" / "sources" / "2026" / "10" / "src-broken"
            folder.mkdir(parents=True)
            (folder / "source.json").write_bytes(b"{not json")
            (folder / "original.md").write_bytes(CLEAN_BODY.encode("utf-8"))
            index = source_metadata_index(vault)
            member = _write_proposal(vault, "prop-broken", ["src-broken"])
            outcome = member_eligibility(vault, member, PROJECT, None, index)
            self.assertFalse(outcome["eligible"])
            self.assertEqual(outcome["code"], "source_metadata_malformed")

    def test_partition_keeps_every_member_exactly_once(self):
        from caretaker.audit import partition_batch_members

        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            _write_source(vault, "src-good")
            _write_source(vault, "src-local", local_only=True)
            members = [
                _write_proposal(vault, "prop-a", ["src-good"]),
                _write_proposal(vault, "prop-b", ["src-local"]),
                _write_proposal(vault, "prop-c", ["src-good"]),
            ]
            eligible, isolated = partition_batch_members(
                vault, members, PROJECT, None,
            )
            self.assertEqual(
                sorted(m["proposal_id"] for m in eligible), ["prop-a", "prop-c"],
            )
            self.assertEqual(
                [m["proposal_id"] for m in isolated], ["prop-b"],
            )
            self.assertEqual(
                sorted(m["proposal_id"] for m in eligible + isolated),
                sorted(m["proposal_id"] for m in members),
            )

    def test_reason_vocabulary_is_stable_and_content_free(self):
        from caretaker.audit import (
            MEMBER_INELIGIBLE_CODES, member_eligibility, source_metadata_index,
        )

        with _workspace() as root:
            vault = Path(root) / "vault"
            vault.mkdir()
            _write_source(vault, "src-secret", SECRET_BODY)
            index = source_metadata_index(vault)
            member = _write_proposal(vault, "prop-secret", ["src-secret"])
            outcome = member_eligibility(vault, member, PROJECT, None, index)
            self.assertFalse(outcome["eligible"])
            code = outcome["reason"].split(" ", 1)[0]
            self.assertIn(code, MEMBER_INELIGIBLE_CODES)
            # The stored outcome carries no source text, key material or path.
            self.assertNotIn("sk-live", outcome["reason"])
            self.assertNotIn("api_key", outcome["reason"])
            self.assertNotIn("session note", outcome["reason"].lower())
            self.assertNotIn(vault.name, outcome["reason"])


if __name__ == "__main__":
    unittest.main()
