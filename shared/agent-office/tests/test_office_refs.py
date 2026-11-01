"""Envelope refs: a handoff carries references, never a bare "continue what I was doing".

`refs` is an optional envelope field (up to 16 entries), each a repo-relative path or a
32-hex id. It is a hint for the reader, never a capability: the office never opens or
reads the file one names. Validation runs at send time only, so an envelope stored
without the field -- an old handoff included -- still reads without error.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from office_support import OfficeTestCase

from coordfs.office import ENVELOPE_FIELDS, detect_secret, inbox_path
from coordfs.office_mcp import TOOLS, TOOL_ARGS, OfficeMcpServer


class HandoffRefsRequiredTests(OfficeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.announce("claude-testhost-bob", "codex-testhost-alice")

    def test_handoff_without_refs_is_refused_with_refs_required(self) -> None:
        result = self.alice.send("claude-testhost-bob", "continue what I was doing", kind="handoff")
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["code"], "refs_required", result)
        self.assertIn("refs", result["message"].lower())
        self.assertIn("handoff", result["message"].lower())
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists(), "a refusal writes nothing")

    def test_handoff_with_empty_refs_list_is_refused(self) -> None:
        result = self.alice.send("claude-testhost-bob", "handoff body", kind="handoff", refs=[])
        self.assertEqual(result["code"], "refs_required", result)
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists())

    def test_handoff_with_refs_is_delivered_and_read_with_refs(self) -> None:
        sent = self.alice.send(
            "claude-testhost-bob",
            "handoff body",
            kind="handoff",
            refs=["shared/agent-office/coordfs/office.py", "0123456789abcdef0123456789abcdef"],
        )
        self.assertEqual(sent["status"], "ok", sent)
        self.assertEqual(sent["code"], "sent", sent)
        read = self.bob.read()
        self.assertEqual(read["status"], "ok", read)
        self.assertEqual(read["count"], 1, read)
        frame = read["messages"][0]["untrusted_message"]
        self.assertEqual(frame["kind"], "handoff")
        self.assertEqual(frame["body"], "handoff body")
        self.assertEqual(
            frame["refs"],
            ["shared/agent-office/coordfs/office.py", "0123456789abcdef0123456789abcdef"],
        )
        self.assertEqual(set(frame), set(ENVELOPE_FIELDS) | {"refs"})

    def test_hex_id_ref_is_stored_in_canonical_lowercase(self) -> None:
        sent = self.alice.send(
            "claude-testhost-bob", "handoff body", kind="handoff", refs=["ABCDEF0123456789ABCDEF0123456789"]
        )
        self.assertEqual(sent["status"], "ok", sent)
        frame = self.bob.read()["messages"][0]["untrusted_message"]
        self.assertEqual(frame["refs"], ["abcdef0123456789abcdef0123456789"])


class RefsOptionalForOtherKindsTests(OfficeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.announce("claude-testhost-bob", "codex-testhost-alice")

    def test_note_without_refs_works_as_today(self) -> None:
        sent = self.alice.send("claude-testhost-bob", "plain note")
        self.assertEqual(sent["status"], "ok", sent)
        read = self.bob.read()
        self.assertEqual(read["status"], "ok", read)
        self.assertEqual(read["count"], 1, read)
        frame = read["messages"][0]["untrusted_message"]
        self.assertEqual(frame["kind"], "note")
        self.assertEqual(frame["body"], "plain note")
        self.assertEqual(set(frame), set(ENVELOPE_FIELDS))
        self.assertNotIn("refs", frame)

    def test_note_may_carry_refs_without_becoming_a_handoff(self) -> None:
        sent = self.alice.send("claude-testhost-bob", "see also", kind="note", refs=["docs/aside.md"])
        self.assertEqual(sent["status"], "ok", sent)
        self.assertEqual(sent["kind"], "note")
        frame = self.bob.read()["messages"][0]["untrusted_message"]
        self.assertEqual(frame["kind"], "note")
        self.assertEqual(frame["refs"], ["docs/aside.md"])


class RefPathSyntaxTests(OfficeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.announce("claude-testhost-bob", "codex-testhost-alice")

    def test_absolute_parent_and_drive_letter_refs_are_refused(self) -> None:
        rejected = (
            "/etc/passwd",
            "\\Windows\\System32",
            "//server/share/file.md",
            "C:\\repo\\file.md",
            "c:/repo/file.md",
            "D:file.md",
            "../outside.md",
            "docs/../../outside.md",
            "docs\\..\\..\\outside.md",
            "..",
        )
        for ref in rejected:
            with self.subTest(ref=ref):
                result = self.alice.send("claude-testhost-bob", "x", kind="handoff", refs=[ref])
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["code"], "invalid_ref", result)
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists(), "no rejected ref reaches disk")

    def test_repo_relative_paths_in_both_separator_flavours_are_accepted(self) -> None:
        sent = self.alice.send(
            "claude-testhost-bob",
            "x",
            kind="handoff",
            refs=["shared/agent-office/coordfs/office.py", "shared\\docs\\plan.md"],
        )
        self.assertEqual(sent["status"], "ok", sent)
        frame = self.bob.read()["messages"][0]["untrusted_message"]
        self.assertEqual(
            frame["refs"],
            ["shared/agent-office/coordfs/office.py", "shared\\docs\\plan.md"],
        )

    def test_non_string_and_empty_refs_are_refused(self) -> None:
        for bad in ([7], [None], [""], ["   "], [["docs/x.md"]], {"docs/x.md": 1}, "docs/x.md"):
            with self.subTest(ref=bad):
                result = self.alice.send("claude-testhost-bob", "x", kind="handoff", refs=bad)  # type: ignore[arg-type]
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["code"], "invalid_ref", result)
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists())


class RefLimitTests(OfficeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.announce("claude-testhost-bob", "codex-testhost-alice")

    def test_seventeen_refs_are_refused(self) -> None:
        refs = [f"docs/file-{index}.md" for index in range(17)]
        result = self.alice.send("claude-testhost-bob", "x", kind="handoff", refs=refs)
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["code"], "too_many_refs", result)
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists())

    def test_sixteen_refs_are_accepted(self) -> None:
        sent = self.alice.send(
            "claude-testhost-bob", "x", kind="handoff", refs=[f"docs/file-{index}.md" for index in range(16)]
        )
        self.assertEqual(sent["status"], "ok", sent)
        frame = self.bob.read()["messages"][0]["untrusted_message"]
        self.assertEqual(len(frame["refs"]), 16)
        self.assertEqual(frame["refs"][0], "docs/file-0.md")
        self.assertEqual(frame["refs"][-1], "docs/file-15.md")


class LegacyEnvelopeTests(OfficeTestCase):
    def test_old_handoff_without_refs_field_still_reads(self) -> None:
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
        self.assertEqual(read["status"], "ok", read)
        self.assertEqual(read["count"], 1, read)
        frame = read["messages"][0]["untrusted_message"]
        self.assertEqual(frame["kind"], "handoff")
        self.assertEqual(frame["body"], "sent by an older office")
        self.assertEqual(set(frame), set(ENVELOPE_FIELDS))
        self.assertNotIn("refs", frame)


class RefsSecretPreflightTests(OfficeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.announce("claude-testhost-bob", "codex-testhost-alice")

    def test_synthetic_secret_in_ref_is_refused_before_write(self) -> None:
        # Assembled at run time on purpose: the private backup refuses files that carry
        # literal secret patterns (incident BLOCKED_SECRET 2026-09-21).
        label = "".join(("api", "_key"))
        filler = "V" * 24
        secret_ref = "docs/" + label + " = " + filler
        self.assertIsNotNone(detect_secret(secret_ref), "the preflight must see this shape")
        result = self.alice.send("claude-testhost-bob", "x", kind="handoff", refs=[secret_ref])
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["code"], "secret_detected", result)
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists(), "a secret ref never lands")

    def test_secret_in_one_ref_refuses_the_whole_envelope(self) -> None:
        prefix = "".join(("sk-", "ant-api03-"))
        secret_ref = "docs/" + prefix + "w" * 32
        self.assertIsNotNone(detect_secret(secret_ref))
        result = self.alice.send("claude-testhost-bob", "x", kind="handoff", refs=["docs/clean.md", secret_ref])
        self.assertEqual(result["code"], "secret_detected", result)
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists())


class RefsAreHintsNotCapabilitiesTests(OfficeTestCase):
    def test_office_never_opens_the_file_a_ref_names(self) -> None:
        self.announce("claude-testhost-bob")
        target = Path(self.tmp.name) / "referenced-artifact.md"
        target.write_text("artifact bytes", encoding="utf-8")
        opened: list[str] = []
        real_open = Path.open

        def tracking_open(path_self: Path, *args: object, **kwargs: object):
            opened.append(str(path_self))
            return real_open(path_self, *args, **kwargs)  # type: ignore[arg-type]

        with mock.patch.object(Path, "open", tracking_open):
            sent = self.alice.send(
                "claude-testhost-bob", "handoff body", kind="handoff", refs=["referenced-artifact.md"]
            )
            read = self.bob.read()
        self.assertEqual(sent["status"], "ok", sent)
        self.assertEqual(read["count"], 1, read)
        self.assertEqual(
            read["messages"][0]["untrusted_message"]["refs"], ["referenced-artifact.md"]
        )
        self.assertFalse(
            any("referenced-artifact.md" in name for name in opened),
            f"a ref is a hint, never a capability; opened={opened!r}",
        )


class CliRefsTests(OfficeTestCase):
    def test_send_ref_is_repeatable_and_read_returns_refs(self) -> None:
        self.announce("claude-testhost-bob")
        code, sent = self.run_cli(
            [
                "--agent",
                "codex-testhost-alice",
                "send",
                "--to",
                "claude-testhost-bob",
                "--body",
                "via cli",
                "--kind",
                "handoff",
                "--ref",
                "docs/plan.md",
                "--ref",
                "cdef0123456789abcdef0123456789ab",
            ]
        )
        self.assertEqual(code, 0, sent)
        read = self.bob.read()
        frame = read["messages"][0]["untrusted_message"]
        self.assertEqual(
            frame["refs"], ["docs/plan.md", "cdef0123456789abcdef0123456789ab"]
        )

    def test_cli_handoff_without_ref_is_refused(self) -> None:
        self.announce("claude-testhost-bob")
        code, payload = self.run_cli(
            ["--agent", "codex-testhost-alice", "send", "--to", "claude-testhost-bob", "--body", "no refs", "--kind", "handoff"]
        )
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload["code"], "refs_required", payload)
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists())


class McpRefsTests(OfficeTestCase):
    def test_office_send_schema_exposes_refs(self) -> None:
        send_schema = next(tool for tool in TOOLS if tool["name"] == "office_send")["inputSchema"]
        self.assertIn("refs", send_schema["properties"])
        self.assertIn("refs", TOOL_ARGS["office_send"])

    def test_mcp_send_with_refs_round_trips(self) -> None:
        self.announce("claude-testhost-bob")
        sender = OfficeMcpServer(self.alice, heartbeat_ttl_s=None)
        reader = OfficeMcpServer(self.bob, heartbeat_ttl_s=None)
        sent = sender.call_tool(
            "office_send",
            {
                "to": "claude-testhost-bob",
                "body": "via mcp",
                "kind": "handoff",
                "refs": ["docs/mcp-plan.md"],
            },
        )
        self.assertEqual(sent["code"], "sent", sent)
        read = reader.call_tool("office_read", {})
        frame = read["messages"][0]["untrusted_message"]
        self.assertEqual(frame["refs"], ["docs/mcp-plan.md"])

    def test_mcp_handoff_without_refs_is_refused(self) -> None:
        self.announce("claude-testhost-bob")
        sender = OfficeMcpServer(self.alice, heartbeat_ttl_s=None)
        result = sender.call_tool(
            "office_send", {"to": "claude-testhost-bob", "body": "no refs", "kind": "handoff"}
        )
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["code"], "refs_required", result)
        self.assertFalse(inbox_path(self.home, "claude-testhost-bob").exists())
