"""Direct addressing: a `to` names somebody who can actually read the message.

Measured on the live home (2026-09-20): 32 of 46 messages were addressed to `root`, which
is neither an agent nor a channel. Every one of them was accepted, created `inbox/root.jsonl`
and reported success, so the sender believed the handoff landed while it went nowhere. A
direct send is therefore an addressed delivery now and refuses an address this office cannot
vouch for; a channel is a notice board and its first post is still allowed to create it.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from office_support import OfficeTestCase

from coordfs.office import Office, inbox_path


class UnknownRecipientTests(OfficeTestCase):
    def test_canonical_shaped_typo_is_refused_without_creating_a_void_inbox(self) -> None:
        real = self.office("codex-ailab-12345678-1234-4234-8234-123456789abc")
        self.assertEqual(real.presence(ttl_s=5)["status"], "ok")
        self.advance(6)
        typo = "codex-ailab-12345678-1234-4234-8234-123456789abd"
        result = self.alice.send(typo, "handoff for a nonexistent session")
        self.assertEqual(result["code"], "unknown_recipient", result)
        self.assertFalse(inbox_path(self.home, typo).exists())
        self.assertEqual(self.alice.send(real.agent_id, "offline handoff")["status"], "ok")

    def test_send_to_an_unknown_id_is_refused_and_creates_no_inbox(self) -> None:
        result = self.alice.send(self.ROOT_ID, "handoff: the parser is yours")
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["code"], "unknown_recipient")
        self.assertIn(self.ROOT_ID, result["message"])
        self.assertIn("office who", result["message"], "the refusal must point at `office who`")
        self.assertFalse(inbox_path(self.home, self.ROOT_ID).exists(), "a refused send leaves no void inbox")
        self.assertEqual(self.bob.read()["count"], 0)

    def test_cli_refuses_an_unknown_recipient_with_a_non_zero_exit(self) -> None:
        code, payload = self.run_cli(["--agent", self.ALICE_ID, "send", "--to", self.ROOT_ID, "--body", "vanish"])
        self.assertEqual(code, 1, "a refusal is the caller's mistake, not a transport failure")
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "unknown_recipient")
        self.assertFalse(inbox_path(self.home, self.ROOT_ID).exists())

    def test_send_to_a_live_agent_is_unchanged(self) -> None:
        self.announce("claude-testhost-bob")
        sent = self.alice.send("claude-testhost-bob", "still works")
        self.assertEqual(sent["status"], "ok", sent)
        self.assertEqual(sent["to"], "claude-testhost-bob")
        messages = self.bob.read()["messages"]
        self.assertEqual([item["untrusted_message"]["body"] for item in messages], ["still works"])

    def test_a_canonical_agent_id_outlives_its_presence_lease(self) -> None:
        """Offline delivery is vouched by a marker written after successful presence.

        The old rule vouched for any non-empty `inbox/<name>.jsonl`. Measured on a copy of
        the real coord home (2026-09-20): `inbox/root.jsonl` held 32 stale messages, so
        `send --to root` returned ok and appended a 33rd -- one historical mistake had
        become a permanent address. A durable presence marker keeps a genuinely offline
        agent reachable without trusting a leftover inbox; see `StaleInboxTests`.
        """
        offline = self.office("grok-host-s1")
        self.assertEqual(offline.presence(ttl_s=5)["status"], "ok")
        self.assertEqual(self.alice.send(offline.agent_id, "first")["status"], "ok")
        self.advance(6)
        self.assertEqual(offline.who()["count"], 0, "the lease is gone")
        self.assertEqual(
            self.alice.send(offline.agent_id, "second")["status"],
            "ok",
            "a real agent id stays addressable while it is offline",
        )
        self.assertEqual(offline.read()["count"], 2)

    def test_registered_offline_agent_receives_mail_without_a_readable_board(self) -> None:
        offline = self.office("codex-ailab-12345678-1234-4234-8234-123456789abc")
        self.assertEqual(offline.presence(ttl_s=5)["status"], "ok")
        self.advance(6)
        with mock.patch.object(self.alice, "_board_or_raise", side_effect=OSError("board unavailable")):
            sent = self.alice.send(offline.agent_id, "board-free handoff")
        self.assertEqual(sent["status"], "ok", sent)
        self.assertEqual(offline.read()["count"], 1)

    def test_unknown_canonical_id_with_unreadable_board_degrades(self) -> None:
        typo = "codex-ailab-12345678-1234-4234-8234-123456789abd"
        with mock.patch.object(self.alice, "_board_or_raise", side_effect=OSError("board unavailable")):
            result = self.alice.send(typo, "no basis to decide")
        self.assertEqual(result["status"], "degraded", result)
        self.assertFalse(inbox_path(self.home, typo).exists())

    def test_an_unreadable_board_degrades_instead_of_refusing(self) -> None:
        """An unreadable board cannot establish whether an unknown full ID is present."""
        with mock.patch.object(self.alice, "_board_or_raise", side_effect=OSError("home is gone")):
            result = self.alice.send(self.BOB_ID, "hello")
        self.assertEqual(result["status"], "degraded", result)
        self.assertEqual(result["code"], "office_unavailable")
        self.assertNotEqual(result["code"], "unknown_recipient")
        self.assertFalse(inbox_path(self.home, self.BOB_ID).exists())

    def test_a_foreign_harness_name_is_still_unknown(self) -> None:
        self.announce("claude-testhost-bob")
        result = self.alice.send("kimi", "anybody there?")
        self.assertEqual(result["code"], "not_an_address", result)
        self.assertFalse(inbox_path(self.home, "kimi").exists())


class ProjectStreamRecipientTests(OfficeTestCase):
    """Registered project inboxes are addressable independently of live presence.

    The registry provides the project, stream and runtime binding for an opaque `pa-`
    address; its prefix and digest alone cannot vouch for an inbox.
    """

    def setUp(self) -> None:
        super().setUp()
        from coordfs.projects import ProjectRegistry, _scoped_inbox_address

        checkout = Path(self.tmp.name) / "checkout"
        checkout.mkdir(parents=True, exist_ok=True)
        registry = ProjectRegistry(home=self.home)
        registry.register("test-project", checkout=checkout)
        stream_rec = registry.ensure_stream(
            "test-project", {"task_id": "t1", "thread_id": "th1", "run_id": "r1"}
        )
        identity = {
            "harness": "claude",
            "host": "ailab",
            "session": "s1",
            "agent_id": "claude-ailab-s1",
            "strength": "strong",
            "source": "explicit-session-id",
        }
        registry.bind_runtime("test-project", identity)
        self.ADDRESS = _scoped_inbox_address(
            "test-project", stream_rec["stream"]["stream_id"], identity
        )

    def test_a_project_stream_is_delivered_without_any_presence_lease(self) -> None:
        sent = self.alice.send(self.ADDRESS, "project payload")
        self.assertEqual(sent["status"], "ok", sent)
        self.assertEqual(sent["to"], self.ADDRESS)
        self.assertIn("project payload", inbox_path(self.home, self.ADDRESS).read_text(encoding="utf-8"))
        self.assertEqual(self.alice.who()["count"], 0, "no agent ever announced itself")

    def test_a_project_stream_is_delivered_when_the_presence_board_is_unreadable(self) -> None:
        with mock.patch.object(self.alice, "_board_or_raise", side_effect=OSError("home is gone")):
            sent = self.alice.send(self.ADDRESS, "project payload")
        self.assertEqual(sent["status"], "ok", sent)
        self.assertEqual(sent["to"], self.ADDRESS)
        self.assertIn("project payload", inbox_path(self.home, self.ADDRESS).read_text(encoding="utf-8"))

    def test_a_bare_prefix_is_not_a_stream_and_is_refused(self) -> None:
        """Measured 2026-09-20: `--to pa-root`, `--to PA-ROOT`, `--to pp-cokolwiek` and
        `--to pc-x` all returned ok and created junk inboxes. A derived name is the prefix
        plus the digest; the prefix alone is an ordinary address and must earn delivery.
        """
        for address in ("pa-root", "PA-ROOT", "pp-cokolwiek", "pc-x", "pa-nothex000000000000000000"):
            with self.subTest(address=address):
                result = self.alice.send(address, "junk")
                self.assertEqual(result["status"], "error", result)
                self.assertEqual(result["code"], "not_an_address", result)
                self.assertFalse(
                    inbox_path(self.home, address.lower()).exists(),
                    "a refused send leaves no void inbox",
                )

    def test_cli_refuses_a_bare_prefix_with_a_non_zero_exit(self) -> None:
        code, payload = self.run_cli(["--agent", "codex-testhost-alice", "send", "--to", "pa-root", "--body", "vanish"])
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload["code"], "not_an_address")
        self.assertFalse(inbox_path(self.home, "pa-root").exists())


class StaleInboxTests(OfficeTestCase):
    """A leftover `inbox/<name>.jsonl` is never evidence that `<name>` is an agent.

    Measured 2026-09-20 on a copy of the real coord home: `inbox/root.jsonl` held 32 stale
    messages and `send --to root` returned ok and appended a 33rd, so the fix that only
    added a presence check did not fix the machine where the problem happened. The same
    rule let `--to codex` land in a leftover `inbox/codex.jsonl` instead of the one live
    session of that harness.
    """

    def _seed_inbox(self, name: str, lines: int) -> Path:
        """A non-empty inbox exactly like the one the live home already had."""
        path = inbox_path(self.home, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(
                json.dumps(
                    {
                        "id": f"{index:032x}",
                        "ts": "2026-09-20T00:00:00Z",
                        "from": "somebody",
                        "to": name,
                        "kind": "note",
                        "body": f"stale {index}",
                        "reply_to": None,
                        "trust": "untrusted",
                    },
                    sort_keys=True,
                )
                + "\n"
                for index in range(lines)
            ),
            encoding="utf-8",
        )
        return path

    def test_a_stale_inbox_does_not_make_root_a_recipient(self) -> None:
        path = self._seed_inbox("root", 32)
        before = path.read_text(encoding="utf-8")
        result = self.alice.send("root", "handoff: the parser is yours")
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["code"], "not_an_address")
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            before,
            "the refusal appends nothing -- and deletes nothing, this office never removes files",
        )

    def test_a_stale_inbox_does_not_capture_a_harness_alias(self) -> None:
        stale = self._seed_inbox("codex", 1)
        before = stale.read_text(encoding="utf-8")
        session = Office(harness="codex", session_id="s1", home=self.home)
        self.assertEqual(session.presence(ttl_s=600)["status"], "ok")
        sent = self.alice.send("codex", "for the only live codex session")
        self.assertEqual((sent["status"], sent["code"]), ("error", "not_an_address"), sent)
        self.assertEqual(session.read()["count"], 0, "a harness alias never receives a send")
        self.assertEqual(stale.read_text(encoding="utf-8"), before, "the legacy alias inbox is untouched")


class HarnessAddressTests(OfficeTestCase):
    def _session(self, harness: str, session_id: str, **overrides: object) -> Office:
        options: dict[str, object] = {
            "harness": harness,
            "session_id": session_id,
            "home": self.home,
            "now": lambda: self.clock[0],
        }
        options.update(overrides)
        return Office(**options)  # type: ignore[arg-type]

    def test_harness_name_is_refused_even_with_one_live_session(self) -> None:
        codex = self._session("codex", "s1")
        self.assertEqual(codex.presence(ttl_s=600)["status"], "ok")
        sent = self.alice.send("codex", "ping the only codex session")
        self.assertEqual((sent["status"], sent["code"]), ("error", "not_an_address"), sent)
        self.assertEqual(codex.read()["count"], 0)
        self.assertFalse(inbox_path(self.home, "codex").exists())

    def test_two_live_sessions_of_one_harness_are_refused_with_candidates(self) -> None:
        first = self._session("codex", "s1")
        second = self._session("codex", "s2")
        self.assertEqual(first.presence(ttl_s=600)["status"], "ok")
        self.assertEqual(second.presence(ttl_s=600)["status"], "ok")
        result = self.alice.send("codex", "guess who")
        self.assertEqual((result["status"], result["code"]), ("error", "not_an_address"), result)
        self.assertFalse(inbox_path(self.home, "codex").exists())
        self.assertEqual(first.read()["count"], 0)
        self.assertEqual(second.read()["count"], 0)
        # Full ids are unaffected by the alias: each session is still directly addressable.
        for session in (first, second):
            self.assertEqual(self.alice.send(session.agent_id, "by id")["status"], "ok")
        self.assertEqual(first.read()["count"], 1)
        self.assertEqual(second.read()["count"], 1)

    def test_cli_refuses_an_ambiguous_harness_name_with_a_non_zero_exit(self) -> None:
        # Wall-clock leases: the CLI subprocess runs on real time, like a real harness.
        first = Office(harness="codex", session_id="s1", home=self.home)
        second = Office(harness="codex", session_id="s2", home=self.home)
        self.assertEqual(first.presence(ttl_s=600)["status"], "ok")
        self.assertEqual(second.presence(ttl_s=600)["status"], "ok")
        code, payload = self.run_cli(["--agent", "codex-testhost-alice", "send", "--to", "codex", "--body", "guess who"])
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload["code"], "not_an_address")
        self.assertEqual(first.read()["count"], 0)
        self.assertEqual(second.read()["count"], 0)


class ChannelNeedsNoRecipientTests(OfficeTestCase):
    def test_channel_send_works_without_any_known_recipient(self) -> None:
        sent = self.alice.send(None, "board is open", channel="fresh")
        self.assertEqual(sent["status"], "ok", sent)
        self.assertEqual(sent["to"], "#fresh")
        self.assertEqual(sent["channel"], "fresh")
        self.assertEqual(self.bob.read(channel="fresh")["count"], 1)

    def test_cli_channel_send_works_without_any_known_recipient(self) -> None:
        code, payload = self.run_cli(["--agent", "codex-testhost-alice", "send", "--channel", "board", "--body", "notice"])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["code"], "sent")
        self.assertEqual(payload["to"], "#board")
        self.assertEqual(self.bob.who()["count"], 0, "no agent ever announced itself")
