"""Frozen publication members cannot duplicate another curation ledger."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import curate_once
import maintenance
from caretaker.audit import active_cycle, maybe_start_cycle, policy_from
from caretaker.publication import PublicationError
from caretaker.pubstate import MAX_TRANSIENT_ATTEMPTS, read_row, record_failure
from .test_autonomous_loop import memory_db


def _control():
    return {"publication": {
        "enabled": True, "include_existing_hub_backlog": True,
        "min_ready_groups": 1, "max_wait_hours": 24, "max_batch": 8,
    }}


def _summary(*proposal_ids):
    eligible = [{
        "proposal_id": pid, "path": f"proposals/codex/{pid}.md",
        "sha256": "hash", "project": "one", "producer": "codex",
        "episode": "", "source_ids": [f"src-{pid}"],
    } for pid in proposal_ids]
    return {
        "trigger": "threshold", "eligible": eligible,
        "raw_eligible": len(eligible), "group_count": len(eligible),
        "ready_group_count": len(eligible), "oldest_age_hours": 24,
        "deferred": 0, "unsupported": 0,
        "policy": policy_from(_control()["publication"]),
    }


def _cfg():
    return {"hub": {"vault": "synthetic-vault"}, "project_slug": "one",
            "state_dir": "synthetic-state"}


@contextmanager
def _lock(*args, **kwargs):
    yield "test-lock"


class AccountedElsewhereTests(unittest.TestCase):
    def test_accounted_pending_member_is_not_batched_and_cycle_drains(self):
        db = memory_db()
        summary = _summary("prop-a")
        cycle = maybe_start_cycle(db, summary)
        self.assertEqual(cycle["members"][0]["status"], "pending")

        with patch("maintenance._audit_summary", return_value=summary), \
                patch("caretaker.audit.accounted_proposal_ids", return_value={"prop-a"}), \
                patch("maintenance._invoke_curate") as invoke:
            result = maintenance.publication_tick("config", _cfg(), _control(), db)

        invoke.assert_not_called()
        self.assertEqual(result["status"], "NO_PENDING")
        self.assertEqual(result["model_calls"], 0)
        self.assertIsNone(active_cycle(db))
        self.assertEqual(db.execute(
            "SELECT status, reason FROM audit_members WHERE proposal_id='prop-a'"
        ).fetchone(), ("accounted_elsewhere", "accounted_by_another_ledger_after_cycle_froze"))
        db.close()

    def test_accounted_inside_lock_blocks_publish_and_preserves_other_member(self):
        db = memory_db()
        summary = _summary("prop-a", "prop-b")
        maybe_start_cycle(db, summary)
        cfg = SimpleNamespace(hub=SimpleNamespace(
            script="knowledge.py", vault="synthetic-vault", python_executable="python"))
        ledger_ids = Mock(side_effect=[set(), {"prop-a"}, {"prop-a"}])

        def curate_run(config_path, paths, publish, *, maintenance_lock_held=False):
            self.assertTrue(publish)
            self.assertTrue(maintenance_lock_held)
            self.assertEqual(set(paths), {
                "proposals/codex/prop-a.md", "proposals/codex/prop-b.md"})
            return curate_once._publish_under_lock(
                cfg, Path("manifest.json"), Path("review.json"), Path("synthetic-state"),
                {"proposal_ids": ["prop-a", "prop-b"]})

        with patch("maintenance._audit_summary", return_value=summary), \
                patch("caretaker.audit.accounted_proposal_ids", ledger_ids), \
                patch("curate_once.accounted_proposal_ids", ledger_ids), \
                patch("maintenance._live_member_hash", return_value="hash"), \
                patch("caretaker.publication.safe_file", return_value=Path("missing_page")), \
                patch("curate_once.run", side_effect=curate_run), \
                patch("curate_once.curator_lock", _lock), \
                patch("curate_once.command") as publish_command, \
                patch("curate_once.checkpoint_or_pending") as checkpoint:
            result = maintenance.publication_tick("config", _cfg(), _control(), db)

        self.assertEqual(ledger_ids.call_count, 3)
        publish_command.assert_not_called()
        checkpoint.assert_not_called()
        self.assertEqual(result["reason"], "proposals_accounted_elsewhere")
        self.assertEqual(result["status"], "BACKOFF_DEFERRED")
        self.assertEqual(dict(db.execute("SELECT proposal_id, status FROM audit_members")),
                         {"prop-a": "accounted_elsewhere", "prop-b": "pending"})
        row = read_row(db, result["batch"])
        self.assertEqual(row["status"], "BACKOFF_DEFERRED")
        self.assertEqual(row["category"], "transient")
        db.close()

    def test_unreadable_ledger_after_summary_blocks_batch(self):
        db = memory_db()
        summary = _summary("prop-a")
        maybe_start_cycle(db, summary)
        with patch("maintenance._audit_summary", return_value=summary), \
                patch("caretaker.audit.accounted_proposal_ids",
                      side_effect=PublicationError("unreadable_curation_ledger")), \
                patch("maintenance._invoke_curate") as invoke:
            result = maintenance.publication_tick("config", _cfg(), _control(), db)

        invoke.assert_not_called()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "unreadable_curation_ledger")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(active_cycle(db)["members"][0]["status"], "pending")
        db.close()

    def test_accounting_race_never_quarantines_even_after_retry_limit(self):
        db = memory_db()
        for attempt in range(MAX_TRANSIENT_ATTEMPTS + 1):
            result = record_failure(
                db, "batch", "proposals_accounted_elsewhere", {"status": "BLOCKED"},
                "fingerprint")
            self.assertEqual(result["status"], "BACKOFF_DEFERRED", attempt)
        self.assertEqual(read_row(db, "batch")["category"], "transient")
        db.close()


if __name__ == "__main__":
    unittest.main()
