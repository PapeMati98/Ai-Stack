"""Regression tests for caretaker 0.1.40:
- Claude duplicate reconciliation (dry-run and apply) for 0.1.39 path-scoped ids.
- Canonical event identity for Claude plus backwards-compatible deserialization.
- Files connector re-observation of unchanged content is not a payload conflict.
- Canonical curation command budget of 1800 s.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import curate_once
import maintenance
from caretaker.connectors import (
    FILE_EVENT_TYPE,
    FILE_PROVENANCE,
    FILE_SCHEMA_VERSION,
    FILE_SPEAKER,
    NormalizedEvent,
    SourceSpec,
    USER_MESSAGE_EVENT_TYPE,
    USER_SCHEMA_VERSION,
    _file_native_session_id,
    _serialized_event_id_matches,
    collect_sources,
    event_to_dict,
    export_normalized_events,
)


TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"
DOCUMENT_DIGEST = "a" * 64


def setUpModule():
    TASK_TMP.mkdir(parents=True, exist_ok=True)


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def _claude_source(directory: Path) -> Path:
    return _write_jsonl(Path(directory) / "claude.jsonl", [
        {"type": "user", "session_id": "claude-s1", "uuid": "u-1",
         "timestamp": "2026-09-17T13:00:00Z", "content": "safe"},
        {"type": "assistant", "session_id": "claude-s1", "uuid": "a-1",
         "timestamp": "2026-09-17T13:00:01Z", "status": "completed",
         "content": "Stable completed answer."},
    ])


def _only_event(collected) -> NormalizedEvent:
    events = tuple(collected.events)
    if len(events) != 1:
        raise AssertionError(f"expected one collected event, got {events}")
    return events[0]


def _serialized_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _legacy_id(event) -> str:
    """Pre-0.1.39 queue identity: tool, session, cursor."""
    return _serialized_id(event.tool, event.native_session_id, event.cursor)


def _path_scoped_id(event) -> str:
    """0.1.39 queue identity: tool, session, cursor, source_ref.path_or_store."""
    return _serialized_id(event.tool, event.native_session_id, event.cursor,
                          str(event.source_ref.get("path_or_store") or ""))


def _path_scoped_id_from_payload(payload) -> str:
    reference = payload.get("source_ref") or {}
    return _serialized_id(payload["tool"], payload["native_session_id"], payload["cursor"],
                          str(reference.get("path_or_store") or ""))


def intake_db(path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE queue(id TEXT PRIMARY KEY,project TEXT,"
               "payload TEXT NOT NULL,forwarded INTEGER NOT NULL DEFAULT 0)")
    db.execute("CREATE TABLE conflicts(id TEXT, payload_hash TEXT, payload TEXT,"
               " PRIMARY KEY(id,payload_hash))")
    return db


def insert_queue(db, ident, payload, forwarded=0):
    db.execute("INSERT INTO queue(id,project,payload,forwarded) VALUES(?,?,?,?)",
               (ident, "przyklad", json.dumps(payload, ensure_ascii=False), forwarded))
    db.commit()
    return ident


def file_event(observed, mtime, *, digest=DOCUMENT_DIGEST, title="owner.md",
               excerpt="Synthetic owner document evidence"):
    return NormalizedEvent(
        tool="files",
        native_session_id=_file_native_session_id("owner-notes", "notes/owner.md"),
        cursor=digest,
        observed_at=observed,
        completed_at=mtime,
        completion_kind="owner_document",
        title=title,
        excerpt=excerpt,
        source_ref={
            "path_or_store": "notes/owner.md",
            "table_or_stream": "files",
            "relative_path": "notes/owner.md",
            "sha256": digest,
            "mtime": mtime,
            "source_id": "owner-notes",
        },
        linked_user_cursor="",
        project="przyklad",
        schema_version=FILE_SCHEMA_VERSION,
        event_type=FILE_EVENT_TYPE,
        speaker=FILE_SPEAKER,
        provenance=FILE_PROVENANCE,
        parent_cursor=digest,
    )


def turn_event(excerpt="Synthetic bounded evidence"):
    return {
        "schema_version": 1, "event_type": "completed_turn.v1", "tool": "claude",
        "native_session_id": "claude-s1", "cursor": "turn-1",
        "observed_at": "2026-09-17T13:00:01Z", "completed_at": "2026-09-17T13:00:01Z",
        "completion_kind": "stop_reason:end_turn", "title": "claude completed turn",
        "excerpt": excerpt, "linked_user_cursor": "u-1", "classification": "internal",
        "source_ref": {"path_or_store": "C:/logs/claude.jsonl", "table_or_stream": "jsonl"},
    }


def enqueue(db, events):
    report = {"new_events": 0, "source_issues": []}
    maintenance._enqueue_source_events(
        db, events, report, "owner-notes",
        {"known_projects": ["przyklad"], "workspace_projects": {}, "session_projects": {}},
    )
    return report


@contextlib.contextmanager
def _unlocked(*args, **kwargs):
    yield True


@contextlib.contextmanager
def _curator_lock(*args, **kwargs):
    yield "lock-0140"


class CanonicalCurationTimeout0140Tests(unittest.TestCase):
    def test_canonical_publish_command_is_bounded_to_1800_seconds(self):
        cfg = type("Cfg", (), {"hub": type("Hub", (), {
            "script": "k.py", "vault": "V", "python_executable": "py"})()})()
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory, \
                patch("curate_once.curator_lock", _curator_lock), \
                patch("curate_once.command",
                      return_value={"status": "PUBLISHED", "event_id": "evt-x"}) as run_command, \
                patch("curate_once.checkpoint_or_pending",
                      return_value={"status": "CHECKPOINTED"}):
            result = curate_once._publish_under_lock(
                cfg, Path(directory) / "m.json", Path(directory) / "r.json",
                Path(directory), {})
        self.assertEqual(result["status"], "PUBLISHED")
        publish = [call for call in run_command.call_args_list if "publish" in call.args[0]]
        self.assertEqual(len(publish), 1)
        self.assertEqual(publish[0].kwargs["timeout"], 1800)


class ClaudeDuplicateReconciliation0140Tests(unittest.TestCase):
    def _duplicate_queue(self, root, *, forwarded=0, canonical=True):
        event = _only_event(collect_sources(
            [SourceSpec("claude", _claude_source(root / "logs"))]))
        payload = event_to_dict(event)
        db = intake_db(root / "intake.sqlite")
        canonical_id = maintenance.event_id(payload)
        if canonical_id != _legacy_id(event):
            raise AssertionError("queue identity must stay the pre-0.1.39 formula")
        duplicate_id = _path_scoped_id(event)
        self.assertNotEqual(duplicate_id, canonical_id)
        if canonical:
            insert_queue(db, canonical_id, payload)
        insert_queue(db, duplicate_id, payload, forwarded=forwarded)
        return db, canonical_id, duplicate_id

    def test_dry_run_reports_without_writing(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            db, _, duplicate_id = self._duplicate_queue(Path(directory))
            maintenance._ensure_new_epoch_tables(db)
            db.execute("INSERT INTO new_epoch_events(event_id,status,reason,forwarded) "
                       "VALUES(?,'admitted','',0)", (duplicate_id,))
            db.commit()
            report = maintenance.reconcile_claude_duplicates(db)
            self.assertEqual(report, {"checked": 2, "duplicates_found": 1, "applied": False,
                                      "updated_queue": 0, "updated_new_epoch": 0})
            self.assertEqual(db.execute("SELECT forwarded FROM queue ORDER BY id").fetchall(),
                             [(0,), (0,)])
            self.assertEqual(db.execute("SELECT status,reason,forwarded FROM new_epoch_events "
                                        "WHERE event_id=?", (duplicate_id,)).fetchone(),
                             ("admitted", "", 0))
            self.assertEqual(db.execute("SELECT count(*) FROM queue").fetchone()[0], 2)
            db.close()

    def test_apply_marks_duplicate_superseded_without_deleting(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            db, canonical_id, duplicate_id = self._duplicate_queue(Path(directory))
            maintenance._ensure_new_epoch_tables(db)
            db.executemany(
                "INSERT INTO new_epoch_events(event_id,status,reason,forwarded) "
                "VALUES(?,'admitted','',0)", [(canonical_id,), (duplicate_id,)])
            db.commit()
            report = maintenance.reconcile_claude_duplicates(db, apply=True)
            self.assertEqual(report, {"checked": 2, "duplicates_found": 1, "applied": True,
                                      "updated_queue": 1, "updated_new_epoch": 1})
            self.assertEqual(db.execute("SELECT forwarded FROM queue WHERE id=?",
                                        (duplicate_id,)).fetchone()[0], -2)
            self.assertEqual(db.execute("SELECT forwarded FROM queue WHERE id=?",
                                        (canonical_id,)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status,reason,forwarded FROM new_epoch_events "
                                        "WHERE event_id=?", (duplicate_id,)).fetchone(),
                             ("duplicate", "superseded_by_canonical_event_id", -2))
            self.assertEqual(db.execute("SELECT status,reason,forwarded FROM new_epoch_events "
                                        "WHERE event_id=?", (canonical_id,)).fetchone(),
                             ("admitted", "", 0))
            self.assertEqual(db.execute("SELECT count(*) FROM queue").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM new_epoch_events").fetchone()[0], 2)
            again = maintenance.reconcile_claude_duplicates(db, apply=True)
            self.assertEqual(again["duplicates_found"], 1)
            self.assertEqual(again["updated_queue"], 0)
            self.assertEqual(again["updated_new_epoch"], 0)
            db.close()

    def test_only_claude_rows_with_a_canonical_twin_are_duplicates(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            db, _, _ = self._duplicate_queue(root)
            orphan = intake_db(root / "orphan.sqlite")
            lonely = event_to_dict(_only_event(collect_sources(
                [SourceSpec("claude", _claude_source(root / "other-logs"))])))
            insert_queue(orphan, _path_scoped_id_from_payload(lonely), lonely)
            self.assertEqual(maintenance.reconcile_claude_duplicates(orphan, apply=True),
                             {"checked": 1, "duplicates_found": 0, "applied": True,
                              "updated_queue": 0, "updated_new_epoch": 0})
            other = turn_event() | {"tool": "codex"}
            other_id = _serialized_id("codex", other["native_session_id"], other["cursor"],
                                      other["source_ref"]["path_or_store"])
            insert_queue(db, maintenance.event_id(other), other)
            insert_queue(db, other_id, other)
            report = maintenance.reconcile_claude_duplicates(db, apply=True)
            self.assertEqual(report["checked"], 4)
            self.assertEqual(report["duplicates_found"], 1)
            self.assertEqual(report["updated_queue"], 1)
            self.assertEqual(db.execute("SELECT forwarded FROM queue WHERE id=?",
                                        (other_id,)).fetchone()[0], 0)
            orphan.close()
            db.close()

    def test_already_forwarded_duplicates_are_left_alone(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            db, _, duplicate_id = self._duplicate_queue(Path(directory), forwarded=1)
            report = maintenance.reconcile_claude_duplicates(db, apply=True)
            self.assertEqual(report["duplicates_found"], 1)
            self.assertEqual(report["updated_queue"], 0)
            self.assertEqual(db.execute("SELECT forwarded FROM queue WHERE id=?",
                                        (duplicate_id,)).fetchone()[0], 1)
            db.close()

    def test_missing_tables_report_zero_instead_of_failing(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            empty = sqlite3.connect(str(root / "empty.sqlite"))
            self.assertEqual(maintenance.reconcile_claude_duplicates(empty, apply=True),
                             {"checked": 0, "duplicates_found": 0, "applied": True,
                              "updated_queue": 0, "updated_new_epoch": 0})
            queue_only = intake_db(root / "queue-only.sqlite")
            self.assertEqual(maintenance.reconcile_claude_duplicates(queue_only, apply=True),
                             {"checked": 0, "duplicates_found": 0, "applied": True,
                              "updated_queue": 0, "updated_new_epoch": 0})
            empty.close()
            queue_only.close()


class FilesContentDeduplication0140Tests(unittest.TestCase):
    def test_same_content_event_ignores_only_volatile_timestamps(self):
        first = event_to_dict(file_event("2026-09-23T12:00:00.000Z", "2026-09-20T09:00:00.000Z"))
        second = event_to_dict(file_event("2026-09-24T06:30:00.000Z", "2026-09-23T18:15:00.000Z"))
        self.assertNotEqual(first, second)
        self.assertTrue(maintenance._same_file_content_event(first, second))
        self.assertTrue(maintenance._same_file_content_event(second, first))
        self.assertFalse(maintenance._same_file_content_event(first, first | {"title": "renamed.md"}))
        self.assertFalse(maintenance._same_file_content_event(first, first | {"excerpt": "other"}))
        self.assertFalse(maintenance._same_file_content_event(
            first, event_to_dict(file_event("2026-09-24T06:30:00.000Z",
                                            "2026-09-23T18:15:00.000Z", digest="b" * 64))))
        self.assertFalse(maintenance._same_file_content_event(first, first | {"sha256": ""}))
        self.assertFalse(maintenance._same_file_content_event(first, turn_event()))
        self.assertFalse(maintenance._same_file_content_event(first, None))
        self.assertFalse(maintenance._same_file_content_event(
            first, first | {"event_type": "completed_turn.v1"}))
        self.assertFalse(maintenance._same_file_content_event(first, first | {"tool": "claude"}))

    def test_re_observed_unchanged_document_is_not_a_conflict(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            db = intake_db(Path(directory) / "intake.sqlite")
            first = event_to_dict(file_event("2026-09-23T12:00:00.000Z",
                                             "2026-09-20T09:00:00.000Z"))
            report = enqueue(db, [first])
            self.assertEqual(report["new_events"], 1)
            self.assertEqual(report["source_issues"], [])
            again = enqueue(db, [event_to_dict(file_event("2026-09-24T06:30:00.000Z",
                                                          "2026-09-23T18:15:00.000Z"))])
            self.assertEqual(again["new_events"], 0)
            self.assertEqual(again["source_issues"], [])
            self.assertEqual(db.execute("SELECT count(*) FROM queue").fetchone()[0], 1)
            self.assertEqual(json.loads(db.execute("SELECT payload FROM queue").fetchone()[0]),
                             first)
            db.close()

    def test_changed_payloads_still_report_a_conflict(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            db = intake_db(Path(directory) / "intake.sqlite")
            conflict = [{"source": "owner-notes", "status": "conflict",
                         "reason": "same_event_identity_changed_payload"}]
            enqueue(db, [event_to_dict(file_event("2026-09-23T12:00:00.000Z",
                                                  "2026-09-20T09:00:00.000Z"))])
            renamed = enqueue(db, [event_to_dict(file_event(
                "2026-09-23T12:00:00.000Z", "2026-09-20T09:00:00.000Z", title="renamed.md"))])
            self.assertEqual(renamed["source_issues"], conflict)
            enqueue(db, [turn_event()])
            edited = enqueue(db, [turn_event(excerpt="A different bounded answer.")])
            self.assertEqual(edited["source_issues"], conflict)
            db.close()


class ReconcileCommandLine0140Tests(unittest.TestCase):
    def test_parser_defaults_to_a_tick_and_a_dry_run(self):
        args = maintenance.build_parser().parse_args(["--control", "control.json"])
        self.assertEqual(args.control, "control.json")
        self.assertFalse(args.reconcile_claude_duplicates)
        self.assertFalse(args.apply)
        flagged = maintenance.build_parser().parse_args(
            ["--control", "control.json", "--reconcile-claude-duplicates", "--apply"])
        self.assertTrue(flagged.reconcile_claude_duplicates)
        self.assertTrue(flagged.apply)

    def test_main_runs_reconciliation_and_never_a_tick(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            state = root / "control-state"
            state.mkdir()
            payload = event_to_dict(_only_event(collect_sources(
                [SourceSpec("claude", _claude_source(root))])))
            db = intake_db(state / "intake.sqlite")
            duplicate_id = _path_scoped_id_from_payload(payload)
            insert_queue(db, maintenance.event_id(payload), payload)
            insert_queue(db, duplicate_id, payload)
            db.close()
            control = root / "control.json"
            control.write_text(json.dumps({"state_dir": str(state)}), encoding="utf-8")
            with patch("maintenance.caretaker_maintenance_lock", _unlocked), \
                    patch("maintenance.tick") as tick:
                dry_run = io.StringIO()
                with contextlib.redirect_stdout(dry_run):
                    self.assertEqual(maintenance.main(
                        ["--control", str(control), "--reconcile-claude-duplicates"]), 0)
                tick.assert_not_called()
                dry = json.loads(dry_run.getvalue())
                self.assertEqual(dry["duplicates_found"], 1)
                self.assertFalse(dry["applied"])
                self.assertEqual(dry["updated_queue"], 0)
                applied_out = io.StringIO()
                with contextlib.redirect_stdout(applied_out):
                    self.assertEqual(maintenance.main(
                        ["--control", str(control), "--reconcile-claude-duplicates",
                         "--apply"]), 0)
                applied = json.loads(applied_out.getvalue())
                self.assertTrue(applied["applied"])
                self.assertEqual(applied["updated_queue"], 1)
            db = sqlite3.connect(str(state / "intake.sqlite"))
            self.assertEqual(db.execute("SELECT forwarded FROM queue WHERE id=?",
                                        (duplicate_id,)).fetchone()[0], -2)
            self.assertEqual(db.execute("SELECT count(*) FROM queue").fetchone()[0], 2)
            db.close()

    def test_main_without_the_flag_still_runs_a_tick(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            control = Path(directory) / "control.json"
            control.write_text(json.dumps({"state_dir": directory}), encoding="utf-8")
            with patch("maintenance.caretaker_maintenance_lock", _unlocked), \
                    patch("maintenance.tick", return_value={"status": "OK"}) as tick:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(maintenance.main(["--control", str(control)]), 0)
            tick.assert_called_once_with(str(control))
            self.assertEqual(json.loads(out.getvalue()), {"status": "OK"})

    def test_reconciliation_reports_a_running_tick_without_writing(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            control = root / "control.json"
            control.write_text(json.dumps({"state_dir": str(root / "state")}), encoding="utf-8")

            @contextlib.contextmanager
            def _busy(*args, **kwargs):
                yield False

            with patch("maintenance.caretaker_maintenance_lock", _busy):
                self.assertEqual(
                    maintenance.reconcile_claude_duplicates_command(str(control), apply=True),
                    {"status": "ALREADY_RUNNING"})
            self.assertFalse((root / "state" / "intake.sqlite").exists())


class StableEventIdentity0140Tests(unittest.TestCase):
    def test_claude_event_id_is_the_canonical_four_tuple(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            event = _only_event(collect_sources(
                [SourceSpec("claude", _claude_source(Path(directory)))]))
        self.assertEqual(event.event_id, _serialized_id(
            "completed_turn.v1", event.tool, event.native_session_id, event.cursor))
        self.assertNotEqual(event.event_id, _legacy_id(event))
        self.assertNotEqual(event.event_id, _path_scoped_id(event))
        self.assertEqual(event_to_dict(event)["event_id"], event.event_id)

    def test_serialized_ids_from_older_releases_still_match(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            event = _only_event(collect_sources(
                [SourceSpec("claude", _claude_source(Path(directory)))]))
        for stale in (event.event_id, _legacy_id(event), _path_scoped_id(event)):
            with self.subTest(stale=stale[:12]):
                self.assertTrue(_serialized_event_id_matches(event, stale))
        for rejected in ("f" * 64, "", None, 7, "completed_turn.v1"):
            with self.subTest(rejected=str(rejected)):
                self.assertFalse(_serialized_event_id_matches(event, rejected))

    def test_deserialization_accepts_historical_ids_for_every_event_type(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            turn = event_to_dict(_only_event(collect_sources(
                [SourceSpec("claude", _claude_source(Path(directory)))])))
        user = dict(turn, event_type=USER_MESSAGE_EVENT_TYPE, schema_version=USER_SCHEMA_VERSION,
                    speaker="user", provenance="user_statement", linked_user_cursor="",
                    linked_assistant_cursor=turn["cursor"], parent_cursor=turn["cursor"],
                    chunk_index=0, chunk_count=1)
        document = event_to_dict(file_event("2026-09-23T12:00:00.000Z",
                                            "2026-09-20T09:00:00.000Z"))
        for payload in (turn, user, document):
            anonymous = {key: value for key, value in payload.items() if key != "event_id"}
            restored = NormalizedEvent.from_mapping(anonymous)
            self.assertIsNotNone(restored, payload["event_type"])
            for stale in (restored.event_id, _legacy_id(restored), _path_scoped_id(restored)):
                with self.subTest(event_type=payload["event_type"], stale=stale[:12]):
                    accepted = NormalizedEvent.from_mapping(dict(anonymous, event_id=stale))
                    self.assertIsNotNone(accepted)
                    self.assertEqual(accepted.event_id, restored.event_id)
            self.assertIsNone(NormalizedEvent.from_mapping(dict(anonymous, event_id="0" * 64)))

    def test_export_merges_a_record_written_with_a_stale_id(self):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            root = Path(directory)
            source = _claude_source(root)
            output = root / "normalized_events.jsonl"
            first = export_normalized_events(
                (SourceSpec("claude", source) for _ in [0]), output)
            self.assertEqual(first.new_records, 1)
            stored = json.loads(output.read_text("utf-8"))
            event = NormalizedEvent.from_mapping(stored)
            self.assertIsNotNone(event)
            output.write_text(
                json.dumps(dict(stored, event_id=_path_scoped_id(event)),
                           ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8")
            second = export_normalized_events(
                (SourceSpec("claude", source) for _ in [0]), output)
            self.assertEqual(second.total_records, 1)
            self.assertEqual(second.new_records, 0)
            self.assertEqual(json.loads(output.read_text("utf-8"))["event_id"], event.event_id)


if __name__ == "__main__":
    unittest.main()
