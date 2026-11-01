"""Focused synthetic acceptance tests for the durable Office work journal bridge."""

from __future__ import annotations

import errno
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from office_support import PACKAGE_ROOT

from coordfs.journal_bridge import JournalBridge, make_work_record, validate_work_record


SCHEMA_PATH = PACKAGE_ROOT / "docs" / "office-work.v1.schema.json"


def _artifact(identifier: str = "patch-artifact") -> list[dict[str, object]]:
    return [
        {
            "id": identifier,
            "kind": "patch",
            "sha256": {"value": "c" * 64, "null_reason": "not_null"},
        }
    ]


def _evidence(identifier: str = "test-evidence") -> list[dict[str, str]]:
    return [{"id": identifier, "kind": "synthetic", "sha256": "d" * 64}]


def _record(
    *,
    origin_id: str = "tool-a",
    epoch: str = "epoch-1",
    sequence: int = 1,
    project_id: str = "project-synthetic",
    task_id: str = "task-synthetic",
    checkpoint_id: str | None = None,
    checkpoint_sequence: int | None = None,
    predecessor_hash: str | None = None,
    effect_state: str = "NONE",
    patch_hash: str = "a" * 64,
    artifact_id: str = "patch-artifact",
) -> dict[str, object]:
    return make_work_record(
        origin_id=origin_id,
        origin_epoch=epoch,
        sequence=sequence,
        project_id=project_id,
        task_id=task_id,
        thread_id="thread-synthetic",
        run_id="run-synthetic",
        actor_id=origin_id,
        actor_strength="strong",
        provider_name="provider-synthetic",
        provider_session_id="session-synthetic",
        patch_hash=patch_hash,
        git_commit="b" * 40,
        git_state="dirty",
        artifacts=_artifact(artifact_id),
        checkpoint_id=checkpoint_id,
        checkpoint_sequence=checkpoint_sequence,
        predecessor_hash=predecessor_hash,
        effect_state=effect_state,
        input_tokens=0,
        output_tokens=17,
        duration_ms=8,
        cost_micros=0,
        evidence=_evidence(),
    )


def _read_checkpoint_worker(home: str, queue: Any) -> None:
    result = JournalBridge(Path(home)).read_checkpoint("project-transfer", "task-transfer")
    queue.put(result)


def _commit_checkpoint_worker(home: str, origin_id: str, checkpoint_id: str, queue: Any) -> None:
    record = _record(
        origin_id=origin_id,
        project_id="project-concurrent",
        task_id="task-concurrent",
        checkpoint_id=checkpoint_id,
        checkpoint_sequence=1,
        effect_state="CONFIRMED",
    )
    queue.put(JournalBridge(Path(home)).commit_checkpoint(record))


class JournalBridgeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="office-work-journal-")
        self.home = Path(self.tmp.name) / "coord-home"
        self.bridge = JournalBridge(self.home, mutex_timeout_s=3.0)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def checkpoint(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "origin_id": "tool-checkpoint",
            "epoch": "epoch-checkpoint",
            "sequence": 1,
            "project_id": "project-checkpoint",
            "task_id": "task-checkpoint",
            "checkpoint_id": "checkpoint-1",
            "checkpoint_sequence": 1,
            "predecessor_hash": None,
            "effect_state": "CONFIRMED",
        }
        values.update(overrides)
        return _record(**values)  # type: ignore[arg-type]


class SchemaAndSafetyTests(JournalBridgeTestCase):
    def test_schema_models_required_lineage_and_nullable_reasons(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["schema"]["const"], "office-work.v1")
        self.assertTrue(
            {"origin", "lineage", "actor", "provider", "artifact", "checkpoint", "usage", "blocker", "evidence"}
            .issubset(set(schema["required"]))
        )
        self.assertEqual(schema["$defs"]["nullableHash"]["required"], ["value", "null_reason"])
        self.assertIn("computed_on_commit", schema["$defs"]["nullReason"]["enum"])
        self.assertIn("UNKNOWN", schema["$defs"]["checkpoint"]["properties"]["effect_state"]["enum"])

    def test_sensitive_content_is_rejected_before_persistence(self) -> None:
        record = _record()
        record["raw_transcript"] = "do not persist"
        result = self.bridge.export(record)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "sensitive_content")
        self.assertFalse(self.bridge.journal_path.exists())

    def test_nullable_zero_is_not_confused_with_unknown(self) -> None:
        record = _record()
        checked = validate_work_record(record)
        self.assertEqual(checked["usage"]["input_tokens"], {"value": 0, "null_reason": "not_null"})
        self.assertEqual(checked["usage"]["cost_micros"], {"value": 0, "null_reason": "not_null"})


class ExportIdempotencyTests(JournalBridgeTestCase):
    def test_fresh_coordinating_home_uses_coorfs_home_and_duplicate_is_accepted(self) -> None:
        inherited = os.environ.get("COORDFS_HOME")
        try:
            os.environ["COORDFS_HOME"] = str(self.home)
            bridge = JournalBridge()
            first = bridge.export(_record())
            duplicate = bridge.export(_record())
        finally:
            if inherited is None:
                os.environ.pop("COORDFS_HOME", None)
            else:
                os.environ["COORDFS_HOME"] = inherited
        self.assertEqual(first["status"], "ok", first)
        self.assertEqual(first["code"], "exported")
        self.assertEqual(duplicate["status"], "ok", duplicate)
        self.assertEqual(duplicate["code"], "duplicate")
        self.assertTrue(duplicate["duplicate"])
        self.assertTrue((self.home / "office-work" / "journal.jsonl").is_file())

    def test_conflicting_payload_and_cursor_gap_are_visible(self) -> None:
        self.assertEqual(self.bridge.export(_record())["status"], "ok")
        conflict = self.bridge.export(_record(patch_hash="e" * 64))
        self.assertEqual(conflict["status"], "conflict", conflict)
        self.assertEqual(conflict["code"], "origin_payload_conflict")

        gap = self.bridge.export(_record(sequence=3, patch_hash="f" * 64))
        self.assertEqual(gap["status"], "warning", gap)
        self.assertEqual(gap["code"], "cursor_gap")
        self.assertEqual(gap["cursor_gap"], {"expected": 2, "received": 3})

        reconciled = self.bridge.reconcile()
        self.assertEqual(reconciled["status"], "unknown", reconciled)
        self.assertIn("cursor_gap", [issue["code"] for issue in reconciled["issues"]])
        self.assertEqual(reconciled["reconciliation"]["usage"]["input_tokens"], {"known_total": 0, "unknown_count": 0})


class CheckpointTransferTests(JournalBridgeTestCase):
    def test_successor_is_hash_bound_and_conflicting_successor_is_rejected(self) -> None:
        first = self.bridge.commit_checkpoint(self.checkpoint())
        self.assertEqual(first["status"], "ok", first)
        first_hash = first["checkpoint_hash"]

        wrong = self.bridge.commit_checkpoint(
            self.checkpoint(
                sequence=2,
                checkpoint_id="checkpoint-2",
                checkpoint_sequence=2,
                predecessor_hash="f" * 64,
            )
        )
        self.assertEqual(wrong["status"], "conflict", wrong)
        self.assertEqual(wrong["code"], "checkpoint_successor_conflict")

        second = self.bridge.commit_checkpoint(
            self.checkpoint(
                sequence=2,
                checkpoint_id="checkpoint-2",
                checkpoint_sequence=2,
                predecessor_hash=first_hash,
            )
        )
        self.assertEqual(second["status"], "ok", second)
        self.assertNotEqual(first_hash, second["checkpoint_hash"])

        tampered = self.checkpoint(
            sequence=3,
            checkpoint_id="checkpoint-3",
            checkpoint_sequence=3,
            predecessor_hash=second["checkpoint_hash"],
        )
        tampered["checkpoint"]["hash"] = {"value": "0" * 64, "null_reason": "not_null"}
        hash_mismatch = self.bridge.commit_checkpoint(tampered)
        self.assertEqual(hash_mismatch["status"], "error", hash_mismatch)
        self.assertEqual(hash_mismatch["code"], "checkpoint_hash_mismatch")

    def test_separate_process_reads_transfer_without_reservation_or_repeated_substep(self) -> None:
        created = self.bridge.commit_checkpoint(
            _record(
                origin_id="tool-a",
                epoch="epoch-a",
                project_id="project-transfer",
                task_id="task-transfer",
                checkpoint_id="checkpoint-transfer",
                checkpoint_sequence=1,
                effect_state="CONFIRMED",
                patch_hash="e" * 64,
            )
        )
        self.assertEqual(created["status"], "ok", created)

        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        process = context.Process(target=_read_checkpoint_worker, args=(str(self.home), queue))
        process.start()
        result = queue.get(timeout=15)
        process.join(timeout=15)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["code"], "checkpoint_read")
        self.assertEqual(result["receipt"]["checkpoint_hash"], created["checkpoint_hash"])
        self.assertEqual(result["receipt"]["patch_hash"], {"value": "e" * 64, "null_reason": "not_null"})
        self.assertEqual(result["receipt"]["git_state"], {"value": "dirty", "null_reason": "not_null"})
        self.assertEqual(result["receipt"]["lineage"]["project_id"], "project-transfer")
        self.assertEqual(result["transfer"]["decomposition"], "not_performed")
        self.assertFalse(result["transfer"]["repeated_substep"])
        self.assertFalse(result["reservation_created"])

    def test_concurrent_checkpoint_commits_leave_one_visible_successor_conflict(self) -> None:
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        left = context.Process(target=_commit_checkpoint_worker, args=(str(self.home), "tool-left", "checkpoint-left", queue))
        right = context.Process(target=_commit_checkpoint_worker, args=(str(self.home), "tool-right", "checkpoint-right", queue))
        left.start()
        right.start()
        results = [queue.get(timeout=15), queue.get(timeout=15)]
        left.join(timeout=15)
        right.join(timeout=15)
        self.assertEqual(left.exitcode, 0)
        self.assertEqual(right.exitcode, 0)
        self.assertEqual(sorted(result["status"] for result in results), ["conflict", "ok"])
        loser = next(result for result in results if result["status"] == "conflict")
        self.assertEqual(loser["code"], "checkpoint_successor_conflict")
        reconciled = self.bridge.reconcile()
        self.assertEqual(reconciled["status"], "ok", reconciled)
        self.assertEqual(reconciled["reconciliation"]["ids"], 1)


class RecoveryAndWriteOnceTests(JournalBridgeTestCase):
    def test_corrupt_cursor_and_log_are_unknown_and_never_auto_retried(self) -> None:
        self.assertEqual(self.bridge.export(_record(origin_id="tool-recovery"))["status"], "ok")
        self.bridge.cursor_path("tool-recovery", "epoch-1").write_text("{corrupt", encoding="utf-8")
        cursor_state = self.bridge.reconcile()
        self.assertEqual(cursor_state["status"], "unknown", cursor_state)
        self.assertIn("cursor_corrupt", [issue["code"] for issue in cursor_state["issues"]])
        retry = self.bridge.export(_record(origin_id="tool-recovery", sequence=2))
        self.assertEqual(retry["status"], "unknown", retry)
        self.assertEqual(retry["code"], "cursor_corrupt")
        self.assertFalse(retry["retryable"])

        corrupt_home = Path(self.tmp.name) / "corrupt-log-home"
        corrupt = JournalBridge(corrupt_home)
        corrupt.root.mkdir(parents=True)
        corrupt.journal_path.write_bytes(b"{not-json\n")
        log_state = corrupt.reconcile()
        self.assertEqual(log_state["status"], "unknown", log_state)
        self.assertEqual(log_state["code"], "journal_corrupt")
        blocked = corrupt.export(_record(origin_id="tool-corrupt"))
        self.assertEqual(blocked["status"], "unknown", blocked)
        self.assertFalse(blocked["retryable"])

    def test_disk_full_after_checkpoint_file_is_unknown_and_reconciliation_finds_orphan(self) -> None:
        record = self.checkpoint(origin_id="tool-disk", epoch="epoch-disk")
        with mock.patch("coordfs.journal_bridge._append_json_line", side_effect=OSError(errno.ENOSPC, "disk full")):
            result = self.bridge.commit_checkpoint(record)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(result["code"], "persistence_unknown")
        self.assertFalse(result["retryable"])
        recovered = self.bridge.reconcile()
        self.assertEqual(recovered["status"], "unknown", recovered)
        self.assertIn("orphan_checkpoint", [issue["code"] for issue in recovered["issues"]])

    def test_unconfirmed_effect_never_enters_an_automatic_commit_path(self) -> None:
        unconfirmed = self.checkpoint(effect_state="UNKNOWN")
        result = self.bridge.commit_checkpoint(unconfirmed)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(result["code"], "unconfirmed_effect")
        self.assertFalse(result["retryable"])
        self.assertFalse(self.bridge.journal_path.exists())

    def test_write_once_rejects_overwrite_partial_rewrite_hash_update_and_delete_with_audit(self) -> None:
        created = self.bridge.commit_checkpoint(self.checkpoint())
        self.assertEqual(created["status"], "ok", created)
        checkpoint_hash = created["checkpoint_hash"]
        checkpoint_path = Path(created["checkpoint_path"])
        original = checkpoint_path.read_bytes()

        attempts = [
            self.bridge.overwrite_checkpoint(checkpoint_hash, {"replacement": "ignored"}),
            self.bridge.partial_rewrite_checkpoint(checkpoint_hash, {"field": "ignored"}),
            self.bridge.update_checkpoint_hash(checkpoint_hash, "e" * 64),
            self.bridge.delete_checkpoint(checkpoint_hash),
        ]
        self.assertTrue(all(result["status"] == "conflict" for result in attempts), attempts)
        self.assertTrue(all(result["code"] == "checkpoint_write_once" for result in attempts), attempts)
        self.assertEqual(checkpoint_path.read_bytes(), original)

        audit = self.bridge.audit_events()
        self.assertEqual(audit["status"], "ok", audit)
        actions = [event["action"] for event in audit["events"] if event["event"] == "checkpoint_mutation_rejected"]
        self.assertEqual(actions, ["overwrite", "partial_rewrite", "hash_only_update", "delete"])
