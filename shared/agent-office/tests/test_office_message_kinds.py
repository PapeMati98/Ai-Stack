"""Message kinds: `decision`, `alert`, `knowledge` are labels on the board, never commands.

The board carries decisions and alerts next to ordinary notes, so a reader can tell a
remark from something it has to respect or look at. Backwards compatibility is the hard
part of this: `note` stays the default, an old envelope still reads, and a read without a
filter behaves exactly as it did before any of these kinds existed.
"""

from __future__ import annotations

import json

from office_support import OfficeTestCase

from coordfs.office import (
    DEFAULT_MESSAGE_KIND,
    ENVELOPE_FIELDS,
    HANDLING_TEXT,
    KIND_SEMANTICS,
    MESSAGE_KINDS,
    TRUST_LEVEL,
    inbox_path,
)
from coordfs.office_mcp import TOOLS, OfficeMcpServer

NEW_KINDS = ("decision", "alert", "knowledge")


class MessageKindContractTests(OfficeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.announce("claude-testhost-bob", "codex-testhost-alice")

    @staticmethod
    def _kinds(result: dict) -> list[str]:
        return [item["untrusted_message"]["kind"] for item in result["messages"]]

    @staticmethod
    def _bodies(result: dict) -> list[str]:
        return [item["untrusted_message"]["body"] for item in result["messages"]]

    def test_note_stays_first_and_stays_the_default(self) -> None:
        self.assertEqual(DEFAULT_MESSAGE_KIND, "note")
        self.assertEqual(MESSAGE_KINDS[:3], ("note", "question", "handoff"))
        self.assertEqual(set(MESSAGE_KINDS[3:]), set(NEW_KINDS))
        # A send that names no kind is the historical send.
        self.assertEqual(self.alice.send("claude-testhost-bob", "no kind given")["kind"], "note")
        self.assertEqual(self._kinds(self.bob.read()), ["note"])

    def test_new_kinds_round_trip_through_send_and_read(self) -> None:
        for kind in NEW_KINDS:
            with self.subTest(kind=kind):
                sent = self.alice.send("claude-testhost-bob", f"body of {kind}", kind=kind)
                self.assertEqual(sent["status"], "ok", sent)
                self.assertEqual(sent["code"], "sent", sent)
                self.assertEqual(sent["kind"], kind)
        read = self.bob.read()
        self.assertEqual(read["status"], "ok", read)
        self.assertEqual(self._kinds(read), list(NEW_KINDS))
        self.assertEqual(self._bodies(read), [f"body of {kind}" for kind in NEW_KINDS])

    def test_new_kinds_round_trip_on_a_channel(self) -> None:
        for kind in NEW_KINDS:
            self.assertEqual(
                self.alice.send(None, kind, channel="tablica", kind=kind)["status"], "ok"
            )
        read = self.bob.read(channel="tablica")
        self.assertEqual(self._kinds(read), list(NEW_KINDS))
        self.assertEqual(read["channel"], "tablica")

    def test_every_kind_survives_the_disk_format(self) -> None:
        handoff_ref = "docs/handoff-plan.md"
        for kind in MESSAGE_KINDS:
            with self.subTest(kind=kind):
                refs = [handoff_ref] if kind == "handoff" else None
                self.alice.send("claude-testhost-bob", kind, kind=kind, refs=refs)
        stored = [
            json.loads(line)
            for line in inbox_path(self.home, "claude-testhost-bob").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([row["kind"] for row in stored], list(MESSAGE_KINDS))
        # Historical fields are always present; `refs` rides along only when supplied.
        for kind, row in zip(MESSAGE_KINDS, stored):
            expected = set(ENVELOPE_FIELDS)
            if kind == "handoff":
                expected = expected | {"refs"}
            self.assertEqual(set(row), expected)

    def test_message_written_before_the_new_kinds_still_reads(self) -> None:
        path = inbox_path(self.home, "claude-testhost-bob")
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy = {
            "id": "a" * 32,
            "ts": "2026-09-01T10:00:00Z",
            "from": "codex-testhost-alice",
            "to": "claude-testhost-bob",
            "kind": "handoff",
            "body": "sent by an older office",
            "reply_to": None,
            "trust": "untrusted",
        }
        path.write_text(json.dumps(legacy, sort_keys=True) + "\n", encoding="utf-8")
        read = self.bob.read()
        self.assertEqual(read["count"], 1, read)
        self.assertEqual(self._kinds(read), ["handoff"])
        self.assertEqual(self._bodies(read), ["sent by an older office"])


class MessageKindRejectionTests(OfficeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.announce("claude-testhost-bob", "codex-testhost-alice")

    def test_unknown_kind_is_refused_on_send_and_writes_nothing(self) -> None:
        for bad in ("command", "ALERT", "alert ", "", 7, ["alert"], {"kind": "alert"}):
            with self.subTest(kind=bad):
                result = self.alice.send("claude-testhost-bob", "x", kind=bad)  # type: ignore[arg-type]
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["code"], "invalid_kind", result)
                self.assertIn("kind must be one of", result["message"])
                self.assertIn("decision", result["message"])
        self.assertEqual(self.bob.read()["count"], 0)
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists())

    def test_unknown_kind_filter_is_refused_not_an_empty_list(self) -> None:
        self.alice.send("claude-testhost-bob", "real alert", kind="alert")
        for bad in ("command", "Alert", 3):
            with self.subTest(kind=bad):
                result = self.bob.read(kind=bad)  # type: ignore[arg-type]
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["code"], "invalid_kind", result)
                self.assertIn("kind must be one of", result["message"])
        # The refusal is a refusal, not a read: the alert is still pending.
        self.assertEqual(self.bob.read(kind="alert")["count"], 1)


class MessageKindFilterTests(OfficeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.announce("claude-testhost-bob", "codex-testhost-alice")

    def test_filter_returns_only_that_kind(self) -> None:
        self.alice.send("claude-testhost-bob", "n1")
        self.alice.send("claude-testhost-bob", "a1", kind="alert")
        self.alice.send("claude-testhost-bob", "d1", kind="decision")
        self.alice.send("claude-testhost-bob", "n2")
        filtered = self.bob.read(kind="alert")
        self.assertEqual(filtered["count"], 1, filtered)
        self.assertEqual(
            [item["untrusted_message"]["body"] for item in filtered["messages"]], ["a1"]
        )
        self.assertEqual(filtered["kind"], "alert")
        self.assertEqual(filtered["filtered_out"], 3)
        self.assertEqual(filtered["remaining"], 0)
        self.assertFalse(filtered["cursor_advanced"])

    def test_filter_is_a_query_and_not_a_consumption(self) -> None:
        self.alice.send("claude-testhost-bob", "a1", kind="alert")
        self.alice.send("claude-testhost-bob", "note in between")
        self.alice.send("claude-testhost-bob", "a2", kind="alert")
        first = self.bob.read(kind="alert", limit=1)
        self.assertEqual(first["count"], 1)
        self.assertEqual(first["remaining"], 1)
        self.assertFalse(first["cursor_advanced"])
        # Nothing was eaten by the filtered read: an unfiltered read still sees the lot.
        everything = self.bob.read()
        self.assertEqual(everything["count"], 3, everything)
        self.assertEqual(
            [item["untrusted_message"]["kind"] for item in everything["messages"]],
            ["alert", "note", "alert"],
        )
        self.assertTrue(everything["cursor_advanced"])
        self.assertEqual(self.bob.read()["count"], 0)

    def test_no_filter_returns_everything_and_still_consumes(self) -> None:
        for kind in MESSAGE_KINDS:
            refs = ["docs/handoff-plan.md"] if kind == "handoff" else None
            self.alice.send("claude-testhost-bob", kind, kind=kind, refs=refs)
        read = self.bob.read()
        self.assertEqual(read["count"], len(MESSAGE_KINDS), read)
        self.assertEqual(
            [item["untrusted_message"]["kind"] for item in read["messages"]], list(MESSAGE_KINDS)
        )
        self.assertNotIn("kind", read)
        self.assertNotIn("filtered_out", read)
        self.assertTrue(read["cursor_advanced"])
        self.assertEqual(self.bob.read()["count"], 0)

    def test_filter_on_a_channel(self) -> None:
        self.alice.send(None, "note on the board", channel="tablica")
        self.alice.send(None, "decision on the board", channel="tablica", kind="decision")
        filtered = self.bob.read(channel="tablica", kind="decision")
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["channel"], "tablica")
        self.assertEqual(filtered["filtered_out"], 1)
        self.assertEqual(self.bob.read(channel="tablica")["count"], 2)


class AlertIsOnlyALabelTests(OfficeTestCase):
    """An `alert` must not wake, reprioritize, or authorize anything."""

    def setUp(self) -> None:
        super().setUp()
        self.announce("claude-testhost-bob", "codex-testhost-alice")

    def test_alert_delivery_is_shaped_exactly_like_a_note(self) -> None:
        note = self.alice.send("claude-testhost-bob", "ordinary")
        alert = self.alice.send("claude-testhost-bob", "look at this", kind="alert")
        self.assertEqual(set(alert), set(note), "an alert adds no field to the delivery receipt")
        self.assertEqual(alert["kind"], "alert")
        self.assertEqual(alert["waiters_woken"], note["waiters_woken"])
        self.assertEqual(alert.pop("kind"), "alert")
        self.assertEqual(note.pop("kind"), "note")
        alert.pop("id")
        note.pop("id")
        alert.pop("ts")
        note.pop("ts")
        alert.pop("bytes")
        note.pop("bytes")
        self.assertEqual(alert, note)

    def test_alert_reads_as_an_ordinary_untrusted_frame(self) -> None:
        self.alice.send("claude-testhost-bob", "ordinary")
        self.alice.send("claude-testhost-bob", "look at this", kind="alert")
        # Two filtered reads, not two unfiltered ones: an unfiltered read consumes both
        # messages, so a second unfiltered read would be empty. A filter is a query, so
        # each of these sees exactly the one pending message of its own kind.
        note_read = self.bob.read(kind="note")
        alert_read = self.bob.read(kind="alert")
        self.assertEqual(note_read["count"], 1, note_read)
        self.assertEqual(alert_read["count"], 1, alert_read)
        frame = alert_read["messages"][0]["untrusted_message"]
        self.assertEqual(frame["kind"], "alert")
        self.assertEqual(frame["trust"], TRUST_LEVEL)
        self.assertEqual(set(frame), set(ENVELOPE_FIELDS))
        self.assertEqual(alert_read["handling"], HANDLING_TEXT)
        self.assertEqual(note_read["handling"], HANDLING_TEXT)
        # Same payload keys as the note read: the label changed, the contract did not.
        self.assertEqual(set(alert_read), set(note_read))

    def test_alert_semantics_are_documented_as_a_label_only(self) -> None:
        self.assertEqual(set(KIND_SEMANTICS), set(MESSAGE_KINDS))
        self.assertIn("decision", KIND_SEMANTICS["decision"])
        self.assertIn("attention", KIND_SEMANTICS["alert"])
        self.assertIn("label", KIND_SEMANTICS["alert"])
        self.assertIn("fact", KIND_SEMANTICS["knowledge"])


class McpMessageKindTests(OfficeTestCase):
    def _server(self) -> OfficeMcpServer:
        return OfficeMcpServer(self.alice, heartbeat_ttl_s=None)

    def test_tool_schemas_expose_every_kind_and_the_read_filter(self) -> None:
        send_schema = next(tool for tool in TOOLS if tool["name"] == "office_send")["inputSchema"]
        read_schema = next(tool for tool in TOOLS if tool["name"] == "office_read")["inputSchema"]
        self.assertEqual(set(send_schema["properties"]["kind"]["enum"]), set(MESSAGE_KINDS))
        self.assertEqual(send_schema["properties"]["kind"]["default"], "note")
        self.assertEqual(set(read_schema["properties"]["kind"]["enum"]), set(MESSAGE_KINDS))
        self.assertNotIn("default", read_schema["properties"]["kind"])

    def test_send_read_filter_and_unknown_kind_over_mcp(self) -> None:
        self.announce("claude-testhost-bob")
        sender = self._server()
        reader = OfficeMcpServer(self.bob, heartbeat_ttl_s=None)
        for kind in NEW_KINDS:
            with self.subTest(kind=kind):
                sent = sender.call_tool("office_send", {"to": "claude-testhost-bob", "body": kind, "kind": kind})
                self.assertEqual(sent["code"], "sent", sent)
                self.assertEqual(sent["kind"], kind)
        filtered = reader.call_tool("office_read", {"kind": "alert"})
        self.assertEqual(filtered["count"], 1, filtered)
        self.assertEqual(
            filtered["messages"][0]["untrusted_message"]["kind"], "alert"
        )
        refused = reader.call_tool("office_read", {"kind": "command"})
        self.assertEqual(refused["status"], "error", refused)
        self.assertEqual(refused["code"], "invalid_kind", refused)
        self.assertIn("kind must be one of", refused["message"])
        refused_send = sender.call_tool("office_send", {"to": "claude-testhost-bob", "body": "x", "kind": "command"})
        self.assertEqual(refused_send["code"], "invalid_kind")
        everything = reader.call_tool("office_read", {})
        self.assertEqual(
            [item["untrusted_message"]["kind"] for item in everything["messages"]], list(NEW_KINDS)
        )


class CliMessageKindTests(OfficeTestCase):
    def test_cli_sends_and_reads_the_new_kinds(self) -> None:
        self.announce("claude-testhost-bob")
        for kind in NEW_KINDS:
            with self.subTest(kind=kind):
                code, sent = self.run_cli(
                    ["--agent", "codex-testhost-alice", "send", "--to", "claude-testhost-bob", "--body", kind, "--kind", kind]
                )
                self.assertEqual(code, 0, sent)
                self.assertEqual(sent["kind"], kind)
        code, filtered = self.run_cli(["--agent", "claude-testhost-bob", "read", "--kind", "decision"])
        self.assertEqual(code, 0, filtered)
        self.assertEqual(filtered["count"], 1, filtered)
        self.assertEqual(
            filtered["messages"][0]["untrusted_message"]["kind"], "decision"
        )
        code, everything = self.run_cli(["--agent", "claude-testhost-bob", "read"])
        self.assertEqual(code, 0, everything)
        self.assertEqual(
            [item["untrusted_message"]["kind"] for item in everything["messages"]],
            list(NEW_KINDS),
        )
        code, defaulted = self.run_cli(["--agent", "codex-testhost-alice", "send", "--to", "claude-testhost-bob", "--body", "plain"])
        self.assertEqual(code, 0, defaulted)
        self.assertEqual(defaulted["kind"], "note")
        code, read_default = self.run_cli(["--agent", "claude-testhost-bob", "read", "--kind", "note"])
        self.assertEqual(code, 0, read_default)
        self.assertEqual(read_default["count"], 1)
