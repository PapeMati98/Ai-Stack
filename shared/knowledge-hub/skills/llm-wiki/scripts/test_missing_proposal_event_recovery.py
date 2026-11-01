from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import unittest
from pathlib import Path
from unittest import mock

import knowledge as k
import test_knowledge_hygiene as hygiene


class MissingProposalEventRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = hygiene.KnowledgeHygieneTests()
        self.fixture.setUp()
        self.fixture.initialize_continuity_vault()
        self.fixture.enable_owner_checkpoint()
        self.root = self.fixture.root
        self.vault = self.fixture.vault
        self.original_write_event = k.write_event

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def git(self, *args: str) -> None:
        k.run_git_without_hooks(self.root, list(args))

    def seed_orphan(self) -> dict:
        note = self.root / "synthetic-handoff.md"
        note.write_text("Synthetic critical handoff evidence; no customer data.\n", encoding="utf-8")

        def fail_only_proposal_event(vault, agent, action, summary, source_ids=None, proposal_ids=None):
            if action == "new-proposal":
                raise OSError("synthetic interrupted event append")
            return self.original_write_event(vault, agent, action, summary, source_ids, proposal_ids)

        with mock.patch.object(k, "write_event", side_effect=fail_only_proposal_event):
            receipt = k.record_handoff(self.vault, self.fixture.checkpoint_handoff_args(note))
        self.assertEqual(receipt["receipt_status"], "INCOMPLETE", receipt)
        self.assertEqual(receipt["error"]["code"], "PROPOSAL_WRITE_INCOMPLETE")
        self.assertTrue(receipt["proposal_id"])
        self.assertTrue(receipt["source_id"])
        self.proposal_id = receipt["proposal_id"]
        self.proposal_path = Path(receipt["proposal_path"])
        self.source_id = receipt["source_id"]
        self.proposal_before = self.proposal_path.read_bytes()

        source_metadata = Path(receipt["source_metadata_path"])
        source_original = Path(receipt["source_original_path"])
        source_event = Path(receipt["source_event_path"])
        self.source_metadata_path = source_metadata
        self.source_original_path = source_original
        self.source_event_path = source_event
        self.source_before = source_original.read_bytes()
        self.source_metadata_before = source_metadata.read_bytes()
        self.stage_event_before = source_event.read_bytes()
        self.git(
            "add",
            "--",
            source_metadata.relative_to(self.root).as_posix(),
            source_original.relative_to(self.root).as_posix(),
            source_event.relative_to(self.root).as_posix(),
        )
        self.git("commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", "Synthetic completed source half")
        self.assertTrue(k.index_is_clean(self.root))
        self.assertFalse(any(
            self.proposal_id in (event.get("proposal_ids") or [])
            for event, _ in k.readable_event_records(self.vault)
        ))
        return receipt

    def recovery_events(self) -> list[tuple[dict, Path]]:
        return [
            (event, path)
            for event, path in k.readable_event_records(self.vault)
            if event.get("action") == "new-proposal" and self.proposal_id in (event.get("proposal_ids") or [])
        ]

    def snapshot(self) -> dict[str, bytes]:
        return {path.relative_to(self.vault).as_posix(): path.read_bytes() for path in self.vault.rglob("*") if path.is_file()}

    def test_normal_pending_intake_recovers_orphan_once_with_explicit_provenance(self) -> None:
        self.seed_orphan()

        result = k.recover_pending_intake(self.vault)

        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["candidate_count"], 1, result)
        self.assertEqual(len(result["results"]), 1)
        checkpoint = result["results"][0]
        self.assertEqual(checkpoint["status"], "CHECKPOINTED", checkpoint)
        self.assertTrue(checkpoint["partial_recovery"])
        self.assertEqual(checkpoint["receipt_path_count"], 5)
        self.assertEqual(self.proposal_path.read_bytes(), self.proposal_before)
        self.assertEqual(self.source_original_path.read_bytes(), self.source_before)
        self.assertEqual(self.source_metadata_path.read_bytes(), self.source_metadata_before)
        self.assertEqual(self.source_event_path.read_bytes(), self.stage_event_before)

        events = self.recovery_events()
        self.assertEqual(len(events), 1)
        event, event_path = events[0]
        self.assertEqual(event["agent"], "claude")
        self.assertEqual(event["source_ids"], [self.source_id])
        self.assertEqual(event["proposal_ids"], [self.proposal_id])
        provenance = event["recovery_provenance"]
        self.assertEqual(provenance["kind"], "missing-new-proposal-event-recovery")
        self.assertEqual(provenance["producer_agent"], "claude")
        self.assertEqual(provenance["repair_actor"], "llm-wiki.checkpoint_intake")
        self.assertEqual(provenance["recovered_by"], k.local_machine_name())
        self.assertEqual(provenance["component"], "checkpoint_intake")
        event_time = dt.datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
        recovered_time = dt.datetime.fromisoformat(provenance["recovered_at"].replace("Z", "+00:00"))
        self.assertEqual(event_time, recovered_time)
        self.assertTrue(event_time.tzinfo is not None)
        self.assertEqual(event_path.stem, event["event_id"])
        self.assertEqual(k.lint(self.vault)["error_count"], 0)

        second = k.recover_pending_intake(self.vault)
        self.assertEqual(second["status"], "COMPLETED", second)
        self.assertEqual(second["candidate_count"], 0, second)
        self.assertEqual(len(self.recovery_events()), 1)

    def test_normal_complete_pair_keeps_original_event_behavior(self) -> None:
        note = self.root / "normal-handoff.md"
        note.write_text("Normal synthetic handoff evidence.\n", encoding="utf-8")
        receipt = k.record_handoff(self.vault, self.fixture.checkpoint_handoff_args(note))
        self.assertEqual(receipt["receipt_status"], "COMPLETE", receipt)
        events = [
            (event, path)
            for event, path in k.readable_event_records(self.vault)
            if event.get("action") == "new-proposal" and receipt["proposal_id"] in (event.get("proposal_ids") or [])
        ]
        self.assertEqual(len(events), 1)
        self.assertNotIn("recovery_provenance", events[0][0])
        self.assertEqual(k.lint(self.vault)["error_count"], 0)

    def test_changed_source_bytes_refuse_recovery_without_event(self) -> None:
        self.seed_orphan()
        original = next(
            path for path in (self.vault / "raw" / "sources").rglob("original.md")
            if self.source_id in str(path)
        )
        original.write_bytes(self.source_before + b"tamper")
        before_events = len(list((self.vault / "events").rglob("*.json")))

        result = k.recover_pending_intake(self.vault)

        self.assertEqual(result["status"], "PARTIAL", result)
        self.assertTrue(result["results"][0]["status"].startswith("BLOCKED"), result)
        self.assertEqual(len(self.recovery_events()), 0)
        self.assertEqual(len(list((self.vault / "events").rglob("*.json"))), before_events)

    def test_source_metadata_producer_mismatch_refuses_recovery(self) -> None:
        self.seed_orphan()
        metadata = json.loads(self.source_metadata_path.read_text(encoding="utf-8"))
        metadata["captured_by"] = "different-producer"
        self.source_metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        before = self.snapshot()

        result = k.recover_pending_intake(self.vault)

        self.assertEqual(result["status"], "PARTIAL", result)
        self.assertTrue(result["results"][0]["status"].startswith("BLOCKED"), result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.recovery_events()), 0)

    def test_existing_conflicting_event_blocks_recovery(self) -> None:
        self.seed_orphan()
        self.original_write_event(
            self.vault,
            "claude",
            "new-proposal",
            "Synthetic conflicting reference.",
            source_ids=["src-20261023T000000000Z-aaaaaaaa"],
            proposal_ids=[self.proposal_id],
        )
        before = self.snapshot()

        result = k.recover_pending_intake(self.vault)

        self.assertEqual(result["status"], "PARTIAL", result)
        self.assertEqual(result["results"][0]["status"], "BLOCKED_PROPOSAL_EVENT_CARDINALITY", result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.recovery_events()), 1)  # only the deliberate conflicting event
        self.assertNotIn("recovery_provenance", self.recovery_events()[0][0])

    def test_same_source_event_without_proposal_id_is_not_mistaken_for_an_orphan(self) -> None:
        self.seed_orphan()
        self.original_write_event(
            self.vault,
            "claude",
            "new-proposal",
            "Synthetic event with a missing proposal ID.",
            source_ids=[self.source_id],
            proposal_ids=[],
        )
        before = self.snapshot()

        result = k.recover_pending_intake(self.vault)

        self.assertEqual(result["status"], "PARTIAL", result)
        self.assertEqual(result["results"][0]["status"], "BLOCKED_PROPOSAL_EVENT_CARDINALITY", result)
        self.assertEqual(self.snapshot(), before)
        same_source_events = [
            (event, path)
            for event, path in k.readable_event_records(self.vault)
            if event.get("action") == "new-proposal" and event.get("source_ids") == [self.source_id]
        ]
        self.assertEqual(len(same_source_events), 1)
        self.assertEqual(same_source_events[0][0]["proposal_ids"], [])
        self.assertNotIn("recovery_provenance", same_source_events[0][0])

    def test_duplicate_proposal_events_are_preserved_and_never_repaired(self) -> None:
        self.seed_orphan()
        for index in range(2):
            self.original_write_event(
                self.vault,
                "claude",
                "new-proposal",
                f"Synthetic duplicate proposal event {index}.",
                source_ids=[self.source_id],
                proposal_ids=[self.proposal_id],
            )
        before = self.snapshot()

        first = k.recover_pending_intake(self.vault)
        second = k.recover_pending_intake(self.vault)

        self.assertEqual(first["status"], "PARTIAL", first)
        self.assertEqual(first["results"][0]["status"], "BLOCKED_PROPOSAL_EVENT_CARDINALITY", first)
        self.assertEqual(second["status"], "PARTIAL", second)
        self.assertEqual(second["results"][0]["status"], "BLOCKED_PROPOSAL_EVENT_CARDINALITY", second)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.recovery_events()), 2)
        self.assertTrue(all("recovery_provenance" not in event for event, _ in self.recovery_events()))

    def test_malformed_event_file_refuses_automatic_recovery(self) -> None:
        self.seed_orphan()
        malformed = self.vault / "events" / "2026" / "10" / "evt-20261023T000000000Z-bad00000.json"
        malformed.parent.mkdir(parents=True, exist_ok=True)
        malformed.write_text("{not json", encoding="utf-8")
        before = self.snapshot()

        result = k.recover_pending_intake(self.vault)

        self.assertEqual(result["status"], "PARTIAL", result)
        self.assertEqual(result["results"][0]["status"], "BLOCKED_INVALID_PAIR", result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.recovery_events()), 0)

    def test_reparse_candidate_refuses_recovery(self) -> None:
        self.seed_orphan()
        source_event = next(
            path for path in (self.vault / "events").rglob("*.json")
            if json.loads(path.read_text(encoding="utf-8"))["action"] == "stage-source"
        )
        before = self.snapshot()
        original_guard = k.path_has_reparse_component

        def flagged(root: Path, path: Path) -> bool:
            return Path(path) == source_event or original_guard(root, path)

        with mock.patch.object(k, "path_has_reparse_component", side_effect=flagged):
            result = k.recover_pending_intake(self.vault)

        self.assertEqual(result["status"], "PARTIAL", result)
        self.assertEqual(result["results"][0]["status"], "BLOCKED_REPARSE_PATH", result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.recovery_events()), 0)

    def test_disabled_checkpoint_policy_refuses_recovery(self) -> None:
        self.seed_orphan()
        config_path = self.vault / "knowledge-vault.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["auto_checkpoint_intake"] = False
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        self.git(
            "add", "--", config_path.relative_to(self.root).as_posix(),
        )
        self.git("commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", "Synthetic checkpoint policy disabled")
        before = self.snapshot()

        result = k.recover_pending_intake(self.vault)

        self.assertEqual(result["status"], "NOT_OWNER", result)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.recovery_events()), 0)

    def test_candidate_change_after_fence_acquisition_refuses_event_write(self) -> None:
        self.seed_orphan()
        before_event_count = len(list((self.vault / "events").rglob("*.json")))
        original_fence = k._event_write_fence

        @contextlib.contextmanager
        def mutate_inside_fence(vault: Path):
            with original_fence(vault):
                self.proposal_path.write_bytes(self.proposal_before + b"\n")
                yield

        with mock.patch.object(k, "_event_write_fence", side_effect=mutate_inside_fence):
            result = k.recover_pending_intake(self.vault)

        self.assertEqual(result["status"], "PARTIAL", result)
        self.assertEqual(result["results"][0]["status"], "BLOCKED_CONCURRENT_CONTENT_CHANGE", result)
        self.assertEqual(len(self.recovery_events()), 0)
        self.assertEqual(len(list((self.vault / "events").rglob("*.json"))), before_event_count)


if __name__ == "__main__":
    unittest.main()
