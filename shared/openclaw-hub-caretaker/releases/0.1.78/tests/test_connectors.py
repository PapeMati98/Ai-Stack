"""Synthetic and metadata-only tests for cross-tool connectors."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from caretaker.connectors import (
    CodexConnector,
    SourceSpec,
    collect_sources,
    discover_source,
    export_normalized_events,
    support_matrix,
)
from caretaker.ingest import read_inputs


HERE = Path(__file__).resolve().parent
TASK_TMP = HERE.parent / ".test-tmp"


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        "utf-8",
    )
    return path


def _codex_rows(*, session_id: str = "codex-s1", turn_id: str = "turn-1") -> list[dict]:
    return [
        {
            "type": "session_meta",
            "timestamp": "2026-09-17T12:00:00Z",
            "payload": {
                "id": "meta-1",
                "session_id": session_id,
                "cwd": "C:/synthetic/project",
                "model_provider": "vllm",
                "model": "Qwen3.8-27B",
                "title": "synthetic codex turn",
            },
        },
        {
            "type": "turn_context",
            "timestamp": "2026-09-17T12:00:01Z",
            "payload": {
                "turn_id": turn_id,
                "cwd": "C:/synthetic/project",
                "model": "Qwen3.8-27B",
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-17T12:00:02Z",
            "payload": {
                "id": "user-1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Please inspect the fixture."}],
            },
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-17T12:00:03Z",
            "payload": {
                "id": "assistant-1",
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [
                    {"type": "output_text", "text": "The fixture is bounded and safe."},
                    {"type": "reasoning", "text": "private reasoning must not escape"},
                    {"type": "tool_use", "name": "read", "input": {"path": "secret"}},
                ],
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-09-17T12:00:04Z",
            "payload": {
                "type": "task_complete",
                "turn_id": turn_id,
                "completed_at": "2026-09-17T12:00:04Z",
                "last_agent_message": "The fixture is bounded and safe.",
            },
        },
    ]


class ConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        TASK_TMP.mkdir(parents=True, exist_ok=True)

    def test_codex_connector_still_drops_bare_tool_field_word(self) -> None:
        rows = _codex_rows()
        rows[3]["payload"]["content"][0]["text"] = "Windows arguments were repaired."
        rows[4]["payload"]["last_agent_message"] = "Windows arguments were repaired."
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            path = _write_jsonl(Path(directory) / "codex.jsonl", rows)
            result = collect_sources([SourceSpec("codex", path)])
        self.assertEqual(result.events, ())

    def test_codex_nested_completed_turn_keeps_identity_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            path = _write_jsonl(Path(directory) / "codex.jsonl", _codex_rows())
            result = collect_sources([SourceSpec("codex", path)])
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.tool, "codex")
        self.assertEqual(event.native_session_id, "codex-s1")
        self.assertEqual(event.cursor, "turn-1")
        self.assertEqual(event.linked_user_cursor, "user-1")
        self.assertEqual(event.excerpt, "The fixture is bounded and safe.")
        self.assertEqual(event.cwd, "C:/synthetic/project")
        self.assertEqual(event.model, "Qwen3.8-27B")
        self.assertNotIn("reasoning", json.dumps(event.to_dict()).lower())
        self.assertNotIn("tool_use", json.dumps(event.to_dict()).lower())

    def test_codex_active_turn_and_missing_link_are_not_admitted(self) -> None:
        active = _codex_rows(turn_id="turn-active")
        active[-1]["payload"]["type"] = "task_started"
        active[-1]["payload"].pop("completed_at")
        active[-1]["payload"]["last_agent_message"] = "still streaming"
        no_link = _codex_rows(turn_id="turn-no-user")
        no_link = [row for row in no_link if not (
            row.get("type") == "response_item" and row.get("payload", {}).get("role") == "user"
        )]
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            active_path = _write_jsonl(Path(directory) / "active.jsonl", active)
            no_link_path = _write_jsonl(Path(directory) / "no-link.jsonl", no_link)
            result = collect_sources([
                SourceSpec("codex", active_path),
                SourceSpec("codex", no_link_path),
            ])
        self.assertEqual(result.events, ())
        self.assertTrue(any(issue.reason == "missing_linked_user" for issue in result.issues))

    def test_claude_qwen_and_grok_require_terminal_assistant_and_link(self) -> None:
        rows_by_tool = {
            "claude": [
                {
                    "type": "user",
                    "sessionId": "claude-s1",
                    "uuid": "cu-1",
                    "timestamp": "2026-09-17T12:10:00Z",
                    "message": {"role": "user", "content": "Review this fixture."},
                },
                {
                    "type": "assistant",
                    "sessionId": "claude-s1",
                    "uuid": "ca-1",
                    "timestamp": "2026-09-17T12:10:01Z",
                    "message": {
                        "id": "cm-1",
                        "role": "assistant",
                        "stop_reason": "end_turn",
                        "content": [
                            {"type": "text", "text": "Claude completed safely."},
                            {"type": "thinking", "thinking": "private"},
                            {"type": "tool_use", "input": {"cmd": "never"}},
                        ],
                    },
                },
            ],
            "qwen": [
                {"type": "user", "sessionId": "qwen-s1", "uuid": "qu-1", "timestamp": "2026-09-17T12:11:00Z", "message": "Review."},
                {"type": "assistant", "sessionId": "qwen-s1", "uuid": "qa-1", "timestamp": "2026-09-17T12:11:01Z", "status": "completed", "model": "qwen3.8-max", "cwd": "C:/synthetic/qwen", "message": {"role": "assistant", "content": "Qwen completed safely."}},
            ],
            "grok": [
                {"type": "user", "session_id": "grok-s1", "uuid": "gu-1", "timestamp": "2026-09-17T12:12:00Z", "content": "Review."},
                {"type": "assistant", "session_id": "grok-s1", "uuid": "ga-1", "timestamp": "2026-09-17T12:12:01Z", "status": "completed", "content": [{"type": "text", "text": "Grok completed safely."}, {"type": "tool_result", "text": "hidden"}]},
            ],
        }
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            specs = []
            for tool, rows in rows_by_tool.items():
                path = _write_jsonl(Path(directory) / f"{tool}.jsonl", rows)
                specs.append(SourceSpec(tool, path))
            result = collect_sources(specs)
        self.assertEqual({event.tool for event in result.events}, {"claude", "qwen", "grok"})
        self.assertEqual({event.excerpt for event in result.events}, {
            "Claude completed safely.", "Qwen completed safely.", "Grok completed safely."
        })
        self.assertTrue(all(event.linked_user_cursor for event in result.events))

    def test_generic_connector_drops_secret_and_partial_terminal(self) -> None:
        synthetic_secret = 'sk' + '-' + 'SYNTHETIC' + 'x' * 32
        rows = [
            {"type": "user", "session_id": "q-s", "uuid": "u", "content": "safe request", "timestamp": "2026-09-17T12:20:00Z"},
            {"type": "assistant", "session_id": "q-s", "uuid": "a-active", "status": "active", "content": "not done", "timestamp": "2026-09-17T12:20:01Z"},
            {"type": "assistant", "session_id": "q-s", "uuid": "a-secret", "status": "completed",
             "content": synthetic_secret, "timestamp": "2026-09-17T12:20:02Z"},
        ]
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            path = _write_jsonl(Path(directory) / "qwen.jsonl", rows)
            result = collect_sources([SourceSpec("qwen", path)])
        self.assertEqual(result.events, ())
        self.assertTrue(any(issue.reason == "active" for issue in result.issues))
        self.assertTrue(any(issue.reason == "unsafe_or_invalid_record" for issue in result.issues))
        self.assertNotIn("sk-abcdef", " ".join(json.dumps(issue.to_dict()) for issue in result.issues))
        self.assertNotIn(synthetic_secret, " ".join(json.dumps(issue.to_dict()) for issue in result.issues))

        for status in ("max_tokens", "terminated", "eos"):
            rows = [
                {"type": "user", "session_id": "q-status", "uuid": "u", "content": "safe request", "timestamp": "2026-09-17T12:20:00Z"},
                {"type": "assistant", "session_id": "q-status", "uuid": "a", "status": status, "content": "not a trusted completed answer", "timestamp": "2026-09-17T12:20:01Z"},
            ]
            with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
                path = _write_jsonl(Path(directory) / f"{status}.jsonl", rows)
                status_result = collect_sources([SourceSpec("qwen", path)])
            self.assertEqual(status_result.events, ())
            self.assertTrue(any(issue.reason == "missing_terminal_marker" for issue in status_result.issues))

    def test_jsonl_bound_counts_terminal_turns_and_prefers_bounded_tail(self) -> None:
        # A long stream of deltas must not stop immediately before the final
        # marker merely because max_records is one terminal turn.
        rows = [
            {"type": "user", "session_id": "q-bound", "uuid": "u", "content": "safe request", "timestamp": "2026-09-17T12:21:00Z"},
        ]
        rows.extend(
            {
                "type": "assistant",
                "session_id": "q-bound",
                "uuid": f"delta-{index}",
                "status": "active",
                "content": "streaming delta",
                "timestamp": "2026-09-17T12:21:01Z",
            }
            for index in range(200)
        )
        rows.append(
            {
                "type": "assistant",
                "session_id": "q-bound",
                "uuid": "a-final",
                "status": "completed",
                "content": "The bounded terminal answer is complete.",
                "timestamp": "2026-09-17T12:21:02Z",
            }
        )
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            path = _write_jsonl(Path(directory) / "bounded.jsonl", rows)
            result = collect_sources([SourceSpec("qwen", path, max_records=1)])
        self.assertEqual([event.cursor for event in result.events], ["a-final"])

        # When a growing source exceeds one byte window, the newest window is
        # inspected while the manifest-facing result exposes the limitation.
        tail_rows = [
            {"type": "event", "session_id": "old", "content": "x" * 80}
            for _ in range(30)
        ] + [
            {"type": "user", "session_id": "q-tail", "uuid": "tu", "content": "safe tail request", "timestamp": "2026-09-17T12:22:00Z"},
            {"type": "assistant", "session_id": "q-tail", "uuid": "ta", "status": "completed", "content": "The newest bounded answer is complete.", "timestamp": "2026-09-17T12:22:01Z"},
        ]
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            path = _write_jsonl(Path(directory) / "tail.jsonl", tail_rows)
            result = collect_sources([SourceSpec("qwen", path, max_records=1, max_bytes=1024)])
        self.assertEqual([event.cursor for event in result.events], ["ta"])
        self.assertTrue(any(issue.reason == "coverage_limited" for issue in result.issues))

    def _create_opencode_db(self, path: Path, *, event_only: bool = False) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY, title TEXT, model TEXT, project_id TEXT,
                directory TEXT, time_created INTEGER, time_updated INTEGER
            );
            CREATE TABLE session_message (
                id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER,
                time_created INTEGER, time_updated INTEGER, data TEXT
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
                time_created INTEGER, time_updated INTEGER, data TEXT
            );
            CREATE TABLE event (
                id TEXT PRIMARY KEY, aggregate_id TEXT, seq INTEGER,
                type TEXT, data TEXT
            );
            CREATE TABLE credential (token TEXT);
            """
        )
        connection.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("op-s1", "Synthetic OpenCode", "glm-5.3-flash", "project-1", "C:/synthetic/opencode", 1789647000000, 1789647005000),
        )
        connection.execute(
            "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ou-1", "op-s1", "user", 1, 1789647001000, 1789647001000, json.dumps({"role": "user", "content": "Review OpenCode fixture."})),
        )
        if event_only:
            connection.execute(
                "INSERT INTO event VALUES (?, ?, ?, ?, ?)",
                ("oe-1", "op-s1", 2, "message.completed", json.dumps({"role": "assistant", "id": "om-event", "status": "completed", "content": "OpenCode event completed safely."})),
            )
        else:
            connection.execute(
                "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("oa-1", "op-s1", "assistant", 2, 1789647002000, 1789647003000, json.dumps({"role": "assistant", "content": "OpenCode completed safely.", "time": {"completed": 1789647003}})),
            )
            connection.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                ("part-tool", "oa-1", "op-s1", 1789647002500, 1789647002500, json.dumps({"type": "tool", "text": "must stay out"})),
            )
        connection.commit()
        connection.close()

    def _create_minimax_db(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE local_runtime_sessions (
                session_id TEXT PRIMARY KEY, record_json TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL, agent_name TEXT,
                runtime TEXT, session_type TEXT, status TEXT,
                workspace_dir TEXT, project_workspace_dir TEXT, title TEXT,
                created_at_ms INTEGER, project_id INTEGER
            );
            CREATE TABLE local_runtime_message_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                msg_id TEXT NOT NULL, role TEXT, turn_id TEXT,
                created_at_ms INTEGER NOT NULL, data_json TEXT NOT NULL,
                source TEXT, UNIQUE(session_id, msg_id)
            );
            CREATE TABLE local_runtime_session_agent_state (
                session_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL,
                turn_sequence INTEGER NOT NULL, runtime_seq INTEGER,
                event_id TEXT NOT NULL, terminal_outcome TEXT,
                updated_at_ms INTEGER NOT NULL
            );
            CREATE TABLE local_runtime_turn_ingress (
                turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                status TEXT NOT NULL, accepted_at_ms INTEGER NOT NULL,
                accepted_sequence INTEGER, completed_at_ms INTEGER,
                input_digest TEXT
            );
            CREATE TABLE secret_store (token TEXT);
            """
        )
        connection.execute(
            "INSERT INTO local_runtime_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("mm-s1", "{}", 1789647205000, "main", "minimax", "conversation", "idle", "C:/synthetic/minimax", "C:/synthetic/minimax", "Synthetic MiniMax", 1789647200000, 7),
        )
        connection.execute(
            "INSERT INTO local_runtime_message_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "mm-s1", "mm-u1", "user", "mm-t1", 1789647201000, json.dumps({"role": "user", "msg_id": "mm-u1", "turn_id": "mm-t1", "msg_content": "Review MiniMax fixture."}), "api"),
        )
        connection.execute(
            "INSERT INTO local_runtime_message_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (2, "mm-s1", "mm-a1", "assistant", "mm-t1", 1789647204000, json.dumps({"role": "assistant", "msg_id": "mm-a1", "turn_id": "mm-t1", "msg_content": "MiniMax completed safely.", "finish_reason": "stop", "thinking_content": "private", "tool_calls": [{"name": "never"}]}), "api"),
        )
        connection.execute(
            "INSERT INTO local_runtime_session_agent_state VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("mm-s1", "mm-t1", 1, 2, "mm-e1", "completed", 1789647205000),
        )
        connection.execute(
            "INSERT INTO local_runtime_turn_ingress VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("mm-t1", "mm-s1", "completed", 1789647200500, 1, 1789647205000, "digest"),
        )
        connection.commit()
        connection.close()

    def test_minimax_sqlite_uses_native_completed_turn_and_safe_msg_content(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            path = Path(directory) / "minimax.db"
            self._create_minimax_db(path)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            result = collect_sources([SourceSpec("minimax", path)])
            after = hashlib.sha256(path.read_bytes()).hexdigest()
            discovery = discover_source(SourceSpec("minimax", path))
        self.assertEqual(before, after)
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.native_session_id, "mm-s1")
        self.assertEqual(event.cursor, "mm-t1")
        self.assertEqual(event.linked_user_cursor, "mm-u1")
        self.assertEqual(event.excerpt, "MiniMax completed safely.")
        self.assertEqual(event.cwd, "C:/synthetic/minimax")
        encoded = json.dumps(event.to_dict()).lower()
        self.assertNotIn("thinking_content", encoded)
        self.assertNotIn("tool_calls", encoded)
        self.assertNotIn("private", encoded)

        self.assertEqual(discovery.status, "supported")
        self.assertIn("local_runtime_message_rows.data_json", discovery.schema_keys)
        self.assertNotIn("secret_store", discovery.tables)

    def test_opencode_sqlite_read_only_allowlist_and_terminal_message(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            path = Path(directory) / "opencode.db"
            self._create_opencode_db(path)
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            result = collect_sources([SourceSpec("opencode", path)])
            after = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(len(result.events), 1)
        event = result.events[0]
        self.assertEqual(event.excerpt, "OpenCode completed safely.")
        self.assertEqual(event.project, "project-1")
        self.assertEqual(event.cwd, "C:/synthetic/opencode")
        discovery = discover_source(SourceSpec("opencode", path))
        self.assertEqual(discovery.status, "unavailable")  # TemporaryDirectory has ended.

    def test_opencode_event_table_is_terminal_fallback(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            path = Path(directory) / "opencode-events.db"
            self._create_opencode_db(path, event_only=True)
            result = collect_sources([SourceSpec("opencode", path)])
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].excerpt, "OpenCode event completed safely.")
        self.assertEqual(result.events[0].cursor, "2")

    def test_hermes_sqlite_requires_ended_session_and_terminal_message(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            path = Path(directory) / "hermes.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, source TEXT, session_key TEXT,
                    display_name TEXT, model TEXT, title TEXT, started_at REAL,
                    ended_at REAL, end_reason TEXT, last_activity_at REAL,
                    cwd TEXT, git_repo_root TEXT, billing_provider TEXT
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                    content TEXT, timestamp REAL, finish_reason TEXT,
                    active INTEGER, compacted INTEGER, display_kind TEXT,
                    display_order INTEGER, reasoning TEXT, tool_calls TEXT
                );
                """
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("he-s1", "hermes", "key-1", "Synthetic Hermes", "qwen3.8-max", "Hermes title", 1789647100, 1789647104, "completed", 1789647104, "C:/synthetic/hermes", "project-hermes", "local"),
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("he-active", "hermes", "key-2", "Active", "qwen3.8-max", "Active", 1789647100, None, "active", 1789647101, "C:/synthetic/active", "project-active", "local"),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (1, "he-s1", "user", "Summarize this fixture.", 1789647101, None, 0, 0, "text", 1, "never read", "never read"),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (2, "he-s1", "assistant", "Hermes completed safely.", 1789647103, "stop", 0, 0, "text", 2, "private reasoning", "private tool calls"),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (3, "he-active", "assistant", "Open active answer.", 1789647102, "stop", 1, 0, "text", 2, "private", "private"),
            )
            connection.commit()
            connection.close()
            result = collect_sources([SourceSpec("hermes", path)])
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].excerpt, "Hermes completed safely.")
        self.assertNotIn("reasoning", json.dumps(result.events[0].to_dict()).lower())

    def test_unverified_source_is_explicitly_skipped(self) -> None:
        result = collect_sources([SourceSpec("kimi", "C:/does/not/exist/session.jsonl")])
        self.assertEqual(result.events, ())
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].status, "unsupported")
        self.assertEqual(result.issues[0].reason, "unverified_source")
        matrix = {entry["tool"]: entry for entry in support_matrix()}
        self.assertEqual(matrix["openclaw"]["status"], "unsupported")
        self.assertEqual(matrix["openclaw"]["note"], "use existing caretaker.ingest.py")

    def test_metadata_discovery_never_returns_payload_values_or_auth_columns(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            jsonl = _write_jsonl(Path(directory) / "qwen.jsonl", [
                {"type": "assistant", "sessionId": "s", "uuid": "a", "status": "completed", "content": "PRIVATE FIXTURE TEXT"},
            ])
            discovery = discover_source(SourceSpec("qwen", jsonl))
            db = Path(directory) / "hermes.db"
            connection = sqlite3.connect(db)
            connection.executescript(
                "CREATE TABLE sessions (id TEXT, ended_at REAL, end_reason TEXT); CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT, finish_reason TEXT); CREATE TABLE credential (token TEXT);"
            )
            connection.commit()
            connection.close()
            sqlite_discovery = discover_source(SourceSpec("hermes", db))
        self.assertEqual(discovery.status, "supported")
        self.assertIn("content", discovery.schema_keys)
        self.assertNotIn("PRIVATE FIXTURE TEXT", discovery.schema_keys)
        self.assertEqual(sqlite_discovery.status, "supported")
        self.assertNotIn("credential", sqlite_discovery.tables)
        self.assertFalse(any(key.startswith("credential.") for key in sqlite_discovery.schema_keys))

    def test_export_is_idempotent_and_compatible_with_normalized_ingest(self) -> None:
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            source = _write_jsonl(Path(directory) / "claude.jsonl", [
                {"type": "user", "session_id": "s", "uuid": "u", "timestamp": "2026-09-17T13:00:00Z", "content": "safe"},
                {"type": "assistant", "session_id": "s", "uuid": "a", "timestamp": "2026-09-17T13:00:01Z", "status": "completed", "content": "Stable completed answer."},
            ])
            output = Path(directory) / "normalized_events.jsonl"
            first = export_normalized_events((SourceSpec("claude", source) for _ in [0]), output)
            bytes_first = output.read_bytes()
            manifest_first = Path(first.manifest_path).read_bytes()
            cursor_first = Path(first.cursor_path).read_bytes()
            second = export_normalized_events((SourceSpec("claude", source) for _ in [0]), output)
            self.assertEqual(bytes_first, output.read_bytes())
            self.assertEqual(manifest_first, Path(second.manifest_path).read_bytes())
            self.assertEqual(cursor_first, Path(second.cursor_path).read_bytes())
            self.assertEqual(first.new_records, 1)
            self.assertEqual(second.new_records, 0)
            self.assertEqual(second.total_records, 1)
            payload = json.loads(output.read_text("utf-8"))
            self.assertEqual(payload["event_type"], "completed_turn.v1")
            self.assertIn("native_session_id", payload)
            ingested, unsupported, stats = read_inputs([(str(output), "normalized_events", "claude")])
        self.assertEqual(unsupported, [])
        self.assertEqual(stats.lines_admitted, 1)
        self.assertEqual(len(ingested), 1)
        self.assertEqual(ingested[0].body, "Stable completed answer.")

    def test_explicit_exclusions_keep_root_and_caretaker_sessions_out(self) -> None:
        rows = _codex_rows(session_id="root-current") + _codex_rows(session_id="caretaker-run-once", turn_id="turn-caretaker")
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            path = _write_jsonl(Path(directory) / "excluded.jsonl", rows)
            result = collect_sources([SourceSpec("codex", path, exclude_native_session_ids=("root-current",))])
        self.assertEqual(result.events, ())
        self.assertGreaterEqual(sum(issue.reason == "excluded_session" for issue in result.issues), 2)


if __name__ == "__main__":
    unittest.main()
