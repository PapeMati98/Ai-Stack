"""Knowledge messages cross into the Knowledge Hub through one bounded gateway call.

The suite never resolves the real canonical gateway.  Every CLI invocation gets an
explicit ``OFFICE_KNOWLEDGE_GATEWAY`` pointing at a small local recorder, and every
``COORDFS_HOME`` is rooted in a directory created by ``mkdtemp``.  The directories are
intentionally not cleaned up: the Agent Office safety contract forbids tests from
introducing filesystem deletion as cleanup.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.office import (  # noqa: E402
    Office,
    channel_path,
    channel_source,
    cursor_path,
)


OFFICE_PY = PACKAGE_ROOT / "office.py"


class KnowledgeBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        # Python 3.13 gives mode 0700 special ACL semantics on Windows.  The
        # restricted test runner uses a sandbox token that cannot reopen such a
        # directory even in the creating process, so keep mkdtemp's collision-safe
        # naming while asking mkdir for an inherited ACL.  No directory is removed.
        real_mkdir = os.mkdir
        mkdir = lambda path, _mode=0o777: real_mkdir(path, 0o777)  # noqa: E731
        with mock.patch.object(tempfile._os, "mkdir", mkdir):
            self.root = Path(tempfile.mkdtemp(prefix="office-knowledge-bridge-"))
        self.home = self.root / "coord-home"
        self.calls_path = self.root / "gateway-calls.jsonl"
        self.gateway = self.root / "fake-knowledge-gateway.py"
        self.gateway.write_text(
            """from __future__ import annotations
import json
import os
import sys
from pathlib import Path

call_path = Path(os.environ["FAKE_GATEWAY_CALLS"])
with call_path.open("a", encoding="utf-8", newline="\\n") as handle:
    handle.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
if os.environ.get("FAKE_GATEWAY_MODE") == "error":
    print(json.dumps({"receipt_status": "FAILED", "reason": "synthetic"}))
    raise SystemExit(9)
print(json.dumps({"receipt_status": "COMPLETE", "source_id": "fake-source"}))
""",
            encoding="utf-8",
            newline="\n",
        )
        self.sender = Office(agent_id="alice", home=self.home)

    def cli_env(self, *, mode: str = "complete") -> dict[str, str]:
        env = os.environ.copy()
        env["COORDFS_HOME"] = str(self.home)
        env["OFFICE_KNOWLEDGE_GATEWAY"] = str(self.gateway)
        env["FAKE_GATEWAY_CALLS"] = str(self.calls_path)
        env["FAKE_GATEWAY_MODE"] = mode
        return env

    def run_sync(
        self,
        *,
        project: str = "agent-office",
        channels: tuple[str, ...] = (),
        dry_run: bool = False,
        mode: str = "complete",
    ) -> tuple[int, dict[str, Any], str]:
        args = [
            sys.executable,
            "-I",
            str(OFFICE_PY),
            "knowledge-sync",
            "--project-slug",
            project,
        ]
        for channel in channels:
            args.extend(("--channel", channel))
        if dry_run:
            args.append("--dry-run")
        completed = subprocess.run(
            args,
            cwd=str(self.root),
            env=self.cli_env(mode=mode),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        return completed.returncode, json.loads(completed.stdout), completed.stdout

    def calls(self) -> list[list[str]]:
        if not self.calls_path.exists():
            return []
        return [json.loads(line) for line in self.calls_path.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def option(argv: list[str], name: str) -> str:
        position = argv.index(name)
        return argv[position + 1]

    def bridge_cursor_files(self) -> list[Path]:
        root = self.home / "office" / "knowledge-bridge" / "cursors"
        return sorted(root.rglob("*.json")) if root.is_dir() else []

    def outbox_files(self) -> list[Path]:
        root = self.home / "office" / "knowledge-bridge" / "outbox"
        return sorted(root.glob("*.md")) if root.is_dir() else []

    def send_knowledge(
        self,
        body: str = "A durable local fact",
        *,
        channel: str = "tablica",
        refs: list[str] | None = None,
    ) -> dict[str, Any]:
        result = self.sender.send(None, body, channel=channel, kind="knowledge", refs=refs)
        self.assertEqual(result["status"], "ok", result)
        return result

    def test_noop_without_new_knowledge_never_calls_gateway_or_writes_outbox(self) -> None:
        code, result, _stdout = self.run_sync()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["code"], "no-op", result)
        self.assertEqual(result["message_count"], 0, result)
        self.assertFalse(result["cursor_advanced"], result)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.outbox_files(), [])

    def test_one_knowledge_message_calls_record_session_once_and_keeps_digest(self) -> None:
        sent = self.send_knowledge("Knowledge bridge fact", refs=["docs/decision.md"])
        code, result, _stdout = self.run_sync()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["code"], "synced", result)
        self.assertEqual(result["message_ids"], [sent["id"]], result)
        self.assertTrue(result["cursor_advanced"], result)

        calls = self.calls()
        self.assertEqual(len(calls), 1, calls)
        argv = calls[0]
        self.assertEqual(argv[0], "record-session", argv)
        self.assertEqual(self.option(argv, "--agent"), "office-knowledge-bridge")
        checkpoint = self.option(argv, "--checkpoint-id")
        self.assertEqual(checkpoint, sha256(sent["id"].encode("utf-8")).hexdigest())
        self.assertEqual(checkpoint, result["checkpoint_id"])
        self.assertEqual(
            self.option(argv, "--session-id"), f"office-kb-agent-office-{checkpoint}"
        )
        self.assertEqual(self.option(argv, "--project-slug"), "agent-office")
        self.assertEqual(self.option(argv, "--outcome"), "COMPLETED")
        self.assertIn("1 message(s)", self.option(argv, "--summary"))
        self.assertEqual(self.option(argv, "--classification"), "internal")

        digest = Path(self.option(argv, "--path"))
        self.assertTrue(digest.is_file(), digest)
        self.assertEqual(self.outbox_files(), [digest])
        text = digest.read_text(encoding="utf-8")
        self.assertIn(sent["id"], text)
        self.assertIn("alice", text)
        self.assertIn(sent["ts"], text)
        self.assertIn("Knowledge bridge fact", text)
        self.assertIn("docs/decision.md", text)

    def test_record_session_summary_contains_first_nonempty_fact_lines(self) -> None:
        self.send_knowledge("\n  first   fact\twith spaces\nsecond line is not included")
        self.send_knowledge("\n\tsecond fact\nmore detail")

        code, result, _stdout = self.run_sync()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["code"], "synced", result)

        summary = self.option(self.calls()[0], "--summary")
        self.assertIn("first fact with spaces", summary)
        self.assertIn("second fact", summary)
        self.assertIn("; ", summary)
        self.assertNotIn("second line is not included", summary)
        self.assertNotIn("\n", summary)

    def test_record_session_summary_is_one_line_and_bounded_for_many_long_messages(self) -> None:
        for index in range(30):
            self.send_knowledge(f"Fact {index}: " + ("long detail " * 40))

        code, result, _stdout = self.run_sync()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["code"], "synced", result)

        summary = self.option(self.calls()[0], "--summary")
        self.assertNotIn("\n", summary)
        self.assertLessEqual(len(summary), 500)
        self.assertRegex(summary, r"\[omitted: \d+\]")

    def test_secret_message_is_not_in_record_session_summary(self) -> None:
        self.send_knowledge("safe visible fact")
        secret = "gh" + "p_" + ("S" * 24)
        message_id = "e" * 32
        envelope = {
            "id": message_id,
            "ts": "2026-09-24T10:00:00Z",
            "from": "legacy-agent",
            "to": "#tablica",
            "kind": "knowledge",
            "body": "do-not-index " + secret,
            "reply_to": None,
            "trust": "untrusted",
        }
        path = channel_path(self.home, "tablica")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(envelope, sort_keys=True) + "\n")

        code, result, _stdout = self.run_sync()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["message_count"], 1, result)
        summary = self.option(self.calls()[0], "--summary")
        self.assertIn("safe visible fact", summary)
        self.assertNotIn("do-not-index", summary)

    def test_non_knowledge_messages_are_ignored(self) -> None:
        self.sender.send(None, "ordinary note", channel="tablica", kind="note")
        self.sender.send(None, "local decision", channel="tablica", kind="decision")
        code, result, _stdout = self.run_sync()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["code"], "no-op", result)
        self.assertEqual(result["message_count"], 0, result)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.bridge_cursor_files(), [])

    def test_successful_cursor_makes_the_next_sync_a_noop(self) -> None:
        sent = self.send_knowledge()
        first_code, first, _stdout = self.run_sync()
        second_code, second, _stdout = self.run_sync()
        self.assertEqual(first_code, 0, first)
        self.assertEqual(first["code"], "synced", first)
        self.assertEqual(second_code, 0, second)
        self.assertEqual(second["code"], "no-op", second)
        self.assertEqual(second["message_count"], 0, second)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(first["message_ids"], [sent["id"]])
        self.assertEqual(len(self.outbox_files()), 1, "successful no-op must retain its digest")

    def test_gateway_error_keeps_cursor_and_retry_reuses_checkpoint(self) -> None:
        sent = self.send_knowledge("retryable fact")
        first_code, first, _stdout = self.run_sync(mode="error")
        self.assertEqual(first_code, 0, first)
        self.assertEqual(first["status"], "degraded", first)
        self.assertEqual(first["code"], "gateway_failed", first)
        self.assertFalse(first["cursor_advanced"], first)
        self.assertEqual(self.bridge_cursor_files(), [])
        self.assertEqual(first["message_ids"], [sent["id"]])
        self.assertEqual(len(self.outbox_files()), 1, "failed gateway retries retain the outbox digest")

        second_code, second, _stdout = self.run_sync(mode="complete")
        self.assertEqual(second_code, 0, second)
        self.assertEqual(second["code"], "synced", second)
        calls = self.calls()
        self.assertEqual(len(calls), 2, calls)
        first_checkpoint = self.option(calls[0], "--checkpoint-id")
        second_checkpoint = self.option(calls[1], "--checkpoint-id")
        self.assertEqual(first_checkpoint, second_checkpoint)
        self.assertEqual(self.option(calls[0], "--path"), self.option(calls[1], "--path"))
        self.assertEqual(second["checkpoint_id"], first["checkpoint_id"])
        self.assertEqual(len(self.bridge_cursor_files()), 1)

    def test_secret_body_is_skipped_and_only_its_id_is_reported(self) -> None:
        secret = "gh" + "p_" + ("Z" * 24)
        message_id = "f" * 32
        envelope = {
            "id": message_id,
            "ts": "2026-09-24T10:00:00Z",
            "from": "legacy-agent",
            "to": "#tablica",
            "kind": "knowledge",
            "body": "credential copied by mistake: " + secret,
            "reply_to": None,
            "trust": "untrusted",
        }
        path = channel_path(self.home, "tablica")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(envelope, sort_keys=True) + "\n")

        code, result, stdout = self.run_sync()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["code"], "no-op", result)
        self.assertEqual(result["skipped_secret_ids"], [message_id], result)
        self.assertNotIn(secret, stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.outbox_files(), [])
        self.assertEqual(self.bridge_cursor_files(), [])

    def test_dry_run_shows_digest_without_gateway_state_or_cursor_writes(self) -> None:
        sent = self.send_knowledge("dry run fact")
        code, result, _stdout = self.run_sync(dry_run=True)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["code"], "dry-run", result)
        self.assertEqual(result["message_ids"], [sent["id"]], result)
        self.assertIn("dry run fact", result["digest_markdown"])
        self.assertFalse(result["cursor_advanced"], result)
        self.assertFalse(result["gateway_invoked"], result)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.outbox_files(), [])
        self.assertEqual(self.bridge_cursor_files(), [])

    def test_bridge_cursor_is_separate_from_an_agent_reader_cursor(self) -> None:
        reader_cursor = cursor_path(self.home, "reader", channel_source("tablica"))
        reader_cursor.parent.mkdir(parents=True, exist_ok=True)
        sentinel = b'{"offset":7,"head":"reader-owned"}\n'
        reader_cursor.write_bytes(sentinel)
        self.send_knowledge("reader cursor stays unchanged")

        code, result, _stdout = self.run_sync()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["code"], "synced", result)
        self.assertEqual(reader_cursor.read_bytes(), sentinel)
        bridge_cursors = self.bridge_cursor_files()
        self.assertEqual(len(bridge_cursors), 1)
        self.assertNotEqual(bridge_cursors[0], reader_cursor)

    def test_repeated_channel_options_are_aggregated_into_one_session(self) -> None:
        first = self.send_knowledge("board fact", channel="tablica")
        second = self.send_knowledge("release fact", channel="release")
        code, result, _stdout = self.run_sync(channels=("release", "tablica"))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["code"], "synced", result)
        self.assertEqual(set(result["message_ids"]), {first["id"], second["id"]})
        self.assertEqual(result["channels"], ["release", "tablica"])
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(len(self.bridge_cursor_files()), 1)


if __name__ == "__main__":
    unittest.main()
