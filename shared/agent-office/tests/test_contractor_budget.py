"""Contractor anti-snowball budget: message size cap, paged/truncated/budgeted reads, a
bounded number of waits and the limits stated in the tool descriptions. The full profile
and the CLI keep their previous behaviour."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from office_support import NO_WINDOW, OFFICE_PY, OfficeTestCase

from coordfs.office import (
    DEFAULT_ACK_BODY,
    DILEMMA_ANSWER_SCHEMA,
    DILEMMA_SCHEMA,
    MAX_BODY_BYTES,
    channel_path,
    frame_bytes,
)
from coordfs.office_cli import build_parser, contractor_limits_from_args
from coordfs.errors import CoordError
from coordfs.office_mcp import (
    CONTRACTOR_MAX_SEND_BYTES,
    CONTRACTOR_MAX_WAITS,
    CONTRACTOR_READ_BODY_BYTES,
    CONTRACTOR_READ_BUDGET_BYTES,
    CONTRACTOR_READ_PAGE,
    CONTRACTOR_TOOLS,
    TOOLS,
    ContractorLimits,
    OfficeMcpServer,
)

CHANNEL = "task-budget"
OPTIONS = {"A": "zostaw kontrakt v1", "B": "przejdz na kontrakt v2"}


class ContractorCase(OfficeTestCase):
    def contractor(self, office=None, limits: ContractorLimits | None = None, peers=("codex-testhost-alice",)) -> OfficeMcpServer:
        return OfficeMcpServer(
            office or self.bob, heartbeat_ttl_s=None, profile="contractor",
            channels=[CHANNEL], peers=list(peers), contractor_limits=limits,
        )

    def serve(self, server: OfficeMcpServer, lines: list[dict]) -> dict:
        raw = b"".join(json.dumps(line).encode("utf-8") + b"\n" for line in lines)
        stdout = io.BytesIO()
        server.serve(io.BytesIO(raw), stdout)
        replies = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line]
        return {reply["id"]: reply for reply in replies}

    def channel_ids(self, office) -> list[str]:
        return [m["untrusted_message"]["id"] for m in office.read(channel=CHANNEL, limit=500)["messages"]]

    @staticmethod
    def tool_payload(result: dict) -> dict:
        """The payload of a contractor tool result: sent once, as compact JSON text."""
        (block,) = result["content"]
        return json.loads(block["text"])


class DefaultsTests(unittest.TestCase):
    def test_defaults_are_the_agreed_numbers(self) -> None:
        self.assertEqual(
            (CONTRACTOR_MAX_SEND_BYTES, CONTRACTOR_READ_PAGE, CONTRACTOR_READ_BODY_BYTES,
             CONTRACTOR_READ_BUDGET_BYTES, CONTRACTOR_MAX_WAITS),
            (4096, 20, 2048, 65536, 6),
        )
        self.assertEqual(ContractorLimits(), ContractorLimits(4096, 20, 2048, 65536, 6))
        self.assertEqual(MAX_BODY_BYTES, 16 * 1024)

    def test_out_of_range_limits_are_refused(self) -> None:
        for bad in ({"max_send_bytes": 0}, {"max_send_bytes": MAX_BODY_BYTES + 1}, {"read_page": 0},
                    {"read_page": 501}, {"read_body_bytes": 0}, {"read_budget_bytes": 0},
                    {"max_waits": -1}, {"max_waits": True}, {"read_page": "20"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ContractorLimits(**bad)
        self.assertEqual(ContractorLimits(max_waits=0).max_waits, 0)


class SendCapTests(ContractorCase):
    def test_body_over_the_cap_is_refused_with_a_summary_hint_and_nothing_is_written(self) -> None:
        server = self.contractor()
        fits = server.call_tool("office_send", {"channel": CHANNEL, "body": "x" * 4096})
        self.assertEqual(fits["status"], "ok")
        refused = server.call_tool("office_send", {"channel": CHANNEL, "body": "x" * 4097})
        self.assertEqual((refused["status"], refused["code"]), ("error", "contractor_body_too_large"))
        self.assertEqual((refused["bytes"], refused["max_bytes"], refused["field"]), (4097, 4096, "body"))
        self.assertIn("short summary plus refs", refused["message"])
        self.assertIn("32-hex", refused["message"])
        self.assertEqual(self.channel_ids(self.alice), [fits["id"]])

    def test_the_cap_counts_utf8_bytes_not_characters(self) -> None:
        server = self.contractor()
        refused = server.call_tool("office_send", {"channel": CHANNEL, "body": "ż" * 2049})
        self.assertEqual((refused["code"], refused["bytes"]), ("contractor_body_too_large", 4098))
        self.assertEqual(server.call_tool("office_send", {"channel": CHANNEL, "body": "ż" * 2048})["status"], "ok")

    def test_ack_note_and_dilemma_texts_are_capped_too(self) -> None:
        server = self.contractor()
        post = self.alice.send(None, "kontrakt v2", kind="decision", channel=CHANNEL)
        self.assertEqual(server.call_tool("office_ack", {"message_id": post["id"], "note": "n" * 5000})["code"],
                         "contractor_body_too_large")
        big_open = server.call_tool("office_dilemma_open", {
            "channel": CHANNEL, "question": "q" * 3000, "options": {"A": "a" * 600, "B": "b" * 600}})
        self.assertEqual((big_open["code"], big_open["field"]), ("contractor_body_too_large", "question and options"))
        dilemma = self.alice.dilemma_open(CHANNEL, "pytanie", OPTIONS)
        big_answer = server.call_tool("office_dilemma_answer", {
            "message_id": dilemma["id"], "choice": "A", "rationale": "r" * 4097})
        self.assertEqual((big_answer["code"], big_answer["field"]), ("contractor_body_too_large", "rationale"))
        self.assertEqual(server.call_tool("office_ack", {"message_id": post["id"], "note": "ok"})["status"], "ok")

    def test_ack_without_a_note_is_capped_on_the_stored_default_text(self) -> None:
        # Office stores DEFAULT_ACK_BODY ("ACK", 3 bytes) when the note is absent or empty,
        # so the cap is checked on that stored text, not on the missing note.
        post = self.alice.send(None, "kontrakt v2", kind="decision", channel=CHANNEL)
        tight = self.contractor(limits=ContractorLimits(max_send_bytes=len(DEFAULT_ACK_BODY) - 1))
        for arguments in ({"message_id": post["id"]}, {"message_id": post["id"], "note": ""},
                          {"message_id": post["id"], "note": None}):
            refused = tight.call_tool("office_ack", arguments)
            self.assertEqual((refused["code"], refused["field"], refused["bytes"]),
                             ("contractor_body_too_large", "note", len(DEFAULT_ACK_BODY)), arguments)
        self.assertEqual(self.alice.read(channel=CHANNEL, limit=500)["count"], 1)
        fits = self.contractor(limits=ContractorLimits(max_send_bytes=len(DEFAULT_ACK_BODY)))
        self.assertEqual(fits.call_tool("office_ack", {"message_id": post["id"]})["status"], "ok")

    def test_readme_lists_every_contractor_tool(self) -> None:
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        start = readme.index("- `mcp --profile contractor` is for a delegated CLI without a shell")
        section = readme[start:readme.index("\n- ", start + 1)]
        for tool in sorted(CONTRACTOR_TOOLS):
            self.assertIn(f"`{tool}`", section, tool)

    def test_dilemma_cap_counts_the_stored_json_with_its_escapes(self) -> None:
        # Raw text under the cap, stored body far over it: every backslash, quote or control
        # character is escaped in the packet JSON.
        server = self.contractor()
        dilemma = self.alice.dilemma_open(CHANNEL, "pytanie", OPTIONS)
        for question in ("\\" * 3990, '"' * 3990, "\x01" * 1000):
            with self.subTest(question=repr(question[:1])):
                raw = len(question.encode("utf-8")) + 4  # plus the option keys and texts "A" "a" "B" "b"
                self.assertLessEqual(raw, CONTRACTOR_MAX_SEND_BYTES)
                refused = server.call_tool("office_dilemma_open", {
                    "channel": CHANNEL, "question": question, "options": {"A": "a", "B": "b"},
                    "correlation_id": "esc"})
                stored = json.dumps({"schema": DILEMMA_SCHEMA, "correlation_id": "esc", "question": question,
                                     "options": {"A": "a", "B": "b"}}, ensure_ascii=False, sort_keys=True)
                self.assertEqual((refused["status"], refused["code"]), ("error", "contractor_body_too_large"))
                self.assertEqual((refused["field"], refused["bytes"], refused["max_bytes"]),
                                 ("question and options", len(stored.encode("utf-8")), CONTRACTOR_MAX_SEND_BYTES))
                self.assertGreater(refused["bytes"], 6000)
                self.assertIn("escapes included", refused["message"])
        refused = server.call_tool("office_dilemma_answer", {
            "message_id": dilemma["id"], "choice": "A", "rationale": "\\" * 4000})
        self.assertEqual((refused["code"], refused["field"]), ("contractor_body_too_large", "rationale"))
        self.assertGreater(refused["bytes"], 8000)
        # Nothing was written: the channel holds only the dilemma itself.
        self.assertEqual(self.channel_ids(self.office("qwen-testhost-carol")), [dilemma["id"]])
        self.assertEqual(self.alice.dilemma_status(dilemma["id"])["state"], "open")

    def test_dilemma_cap_admits_a_stored_body_of_exactly_the_cap(self) -> None:
        question = 'cytat "x" i sciezka C:\\tmp\\a.txt'
        options = {"A": "zostaw", "B": "zmien"}
        stored = json.dumps({"schema": DILEMMA_SCHEMA, "correlation_id": "rowno", "question": question,
                             "options": options}, ensure_ascii=False, sort_keys=True)
        size = len(stored.encode("utf-8"))
        arguments = {"channel": CHANNEL, "question": question, "options": options, "correlation_id": "rowno"}
        tight = self.contractor(limits=ContractorLimits(max_send_bytes=size - 1))
        self.assertEqual(tight.call_tool("office_dilemma_open", arguments)["bytes"], size)
        opened = self.contractor(limits=ContractorLimits(max_send_bytes=size)).call_tool(
            "office_dilemma_open", arguments)
        self.assertEqual(opened["status"], "ok")
        body = self.alice.message(opened["id"])["message"]["untrusted_message"]["body"]
        self.assertEqual(body, stored)
        rationale = 'bo "tak" \\ koniec'
        answer_stored = json.dumps({"schema": DILEMMA_ANSWER_SCHEMA, "correlation_id": "rowno", "choice": "A",
                                    "rationale": rationale}, ensure_ascii=False, sort_keys=True)
        answer_size = len(answer_stored.encode("utf-8"))
        answer_args = {"message_id": opened["id"], "choice": "A", "rationale": rationale}
        architect = self.contractor(office=self.alice, limits=ContractorLimits(max_send_bytes=answer_size - 1))
        self.assertEqual(architect.call_tool("office_dilemma_answer", answer_args)["bytes"], answer_size)
        architect = self.contractor(office=self.alice, limits=ContractorLimits(max_send_bytes=answer_size))
        self.assertEqual(architect.call_tool("office_dilemma_answer", answer_args)["status"], "ok")

    def test_full_profile_and_core_dilemmas_keep_their_limits(self) -> None:
        full = OfficeMcpServer(self.bob, heartbeat_ttl_s=None)
        opened = full.call_tool("office_dilemma_open", {"channel": CHANNEL, "question": "\\" * 3990,
                                                        "options": OPTIONS})
        self.assertEqual(opened["status"], "ok")
        self.assertEqual(self.alice.dilemma_open(CHANNEL, "q", OPTIONS, max_body_bytes=0)["code"], "invalid_limit")

    def test_a_tuned_cap_applies(self) -> None:
        server = self.contractor(limits=ContractorLimits(max_send_bytes=100))
        self.assertEqual(server.call_tool("office_send", {"channel": CHANNEL, "body": "x" * 101})["max_bytes"], 100)
        self.assertEqual(server.call_tool("office_send", {"channel": CHANNEL, "body": "x" * 100})["status"], "ok")

    def test_full_profile_and_core_keep_the_16_kb_limit(self) -> None:
        full = OfficeMcpServer(self.bob, heartbeat_ttl_s=None)
        self.assertEqual(full.call_tool("office_send", {"channel": CHANNEL, "body": "x" * 10_000})["status"], "ok")
        self.assertEqual(full.call_tool("office_send", {"channel": CHANNEL, "body": "x" * (MAX_BODY_BYTES + 1)})["code"],
                         "too_large")
        self.assertEqual(self.alice.send(None, "y" * MAX_BODY_BYTES, channel=CHANNEL)["status"], "ok")

    def test_cli_send_keeps_the_16_kb_limit(self) -> None:
        code, payload = self.run_cli(["--agent", "qwen-testhost-carol", "send", "--channel", CHANNEL, "--body", "z" * 10_000])
        self.assertEqual((code, payload["status"]), (0, "ok"))


class ReadBudgetTests(ContractorCase):
    def post(self, count: int, body: str = "wiadomosc {i}") -> list[str]:
        return [self.alice.send(None, body.format(i=i), channel=CHANNEL)["id"] for i in range(count)]

    def test_reads_are_cursor_pages_of_at_most_twenty(self) -> None:
        ids = self.post(25)
        server = self.contractor()
        first = server.call_tool("office_read", {"channel": CHANNEL, "limit": 50})
        self.assertEqual((first["status"], first["count"], first["remaining"], first["page_limit"]), ("ok", 20, 5, 20))
        second = server.call_tool("office_read", {"channel": CHANNEL})
        self.assertEqual([m["untrusted_message"]["id"] for m in first["messages"] + second["messages"]], ids)
        self.assertEqual((second["count"], second["remaining"]), (5, 0))
        third = server.call_tool("office_read", {"channel": CHANNEL})
        self.assertEqual((third["count"], third["messages"]), (0, []))
        used = first["budget"]["charged_bytes"] + second["budget"]["charged_bytes"]
        self.assertEqual(third["budget"]["used_bytes"], used)
        self.assertEqual(used, sum(frame_bytes(m) for m in first["messages"] + second["messages"]))

    def test_peek_is_refused_so_nothing_is_returned_twice(self) -> None:
        self.post(1)
        server = self.contractor()
        self.assertEqual(server.call_tool("office_read", {"channel": CHANNEL, "peek": True})["code"], "outside_profile")
        self.assertEqual(server.call_tool("office_read", {"channel": CHANNEL, "peek": False})["count"], 1)

    def test_long_bodies_are_truncated_with_flag_and_id_and_fetchable_in_full(self) -> None:
        long_body = "ż" * 1500 + "KONIEC"  # 3006 bytes; the cut lands inside the multibyte run
        (long_id,) = self.post(1, long_body)
        short_id = self.alice.send(None, "krotko", channel=CHANNEL)["id"]
        server = self.contractor()
        read = server.call_tool("office_read", {"channel": CHANNEL})
        long_frame, short_frame = read["messages"]
        self.assertEqual(long_frame["untrusted_message"]["id"], long_id)
        self.assertTrue(long_frame["truncated"])
        self.assertEqual(long_frame["body_bytes"], len(long_body.encode("utf-8")))
        cut = long_frame["untrusted_message"]["body"]
        self.assertEqual(cut, "ż" * 1024)
        self.assertLessEqual(len(cut.encode("utf-8")), 2048)
        self.assertEqual((short_frame["truncated"], short_frame["untrusted_message"]["id"]), (False, short_id))
        before = read["budget"]["used_bytes"]
        fetched = server.call_tool("office_read", {"message_id": long_id})
        self.assertEqual((fetched["status"], fetched["code"]), ("ok", "fetched"))
        self.assertEqual(fetched["message"]["untrusted_message"]["body"], long_body)
        self.assertNotIn("truncated", fetched["message"])
        self.assertEqual(fetched["budget"]["used_bytes"], before + frame_bytes(fetched["message"]))
        # The stored envelope is untouched and the fetch did not move any cursor.
        self.assertEqual(self.alice.message(long_id)["message"]["untrusted_message"]["body"], long_body)
        self.assertEqual(server.call_tool("office_read", {"channel": CHANNEL})["count"], 0)

    def test_fetch_is_limited_to_task_channels_and_the_own_inbox(self) -> None:
        foreign = self.alice.send(None, "inny kanal", channel="tablica")
        server = self.contractor()
        self.assertEqual(server.call_tool("office_read", {"message_id": foreign["id"]})["code"], "outside_profile")
        self.assertEqual(server.call_tool("office_read", {"message_id": "0" * 32})["code"], "unknown_message")
        self.assertEqual(server.call_tool("office_read", {"message_id": "nie-id"})["code"], "invalid_message_id")
        self.announce("codex-testhost-alice", "claude-testhost-bob", "qwen-testhost-carol")
        other_inbox = self.alice.send("qwen-testhost-carol", "dla carol", kind="note")
        self.assertEqual(server.call_tool("office_read", {"message_id": other_inbox["id"]})["code"], "unknown_message")
        own_inbox = self.alice.send("claude-testhost-bob", "dla boba", kind="note")
        fetched = server.call_tool("office_read", {"message_id": own_inbox["id"]})
        self.assertEqual(fetched["message"]["untrusted_message"]["body"], "dla boba")
        full = OfficeMcpServer(self.bob, heartbeat_ttl_s=None)
        self.assertEqual(full.call_tool("office_read", {"message_id": own_inbox["id"]})["code"], "invalid_params")

    def test_budget_is_never_exceeded_and_unreturned_messages_stay_pending(self) -> None:
        ids = self.post(40, "{i:02d} " + "d" * 3000)
        server = self.contractor()
        returned: list[str] = []
        charged = 0
        for _ in range(10):
            result = server.call_tool("office_read", {"channel": CHANNEL})
            if result["status"] != "ok":
                break
            returned += [m["untrusted_message"]["id"] for m in result["messages"]]
            charged += sum(frame_bytes(m) for m in result["messages"])
            self.assertTrue(all(m["truncated"] for m in result["messages"]))
        self.assertEqual((result["status"], result["code"]), ("error", "budget_exhausted"))
        self.assertEqual((result["count"], result["messages"]), (0, []))
        self.assertIn("Finish the task with what you have", result["message"])
        self.assertIn("one short message", result["message"])
        self.assertLessEqual(charged, CONTRACTOR_READ_BUDGET_BYTES)
        self.assertEqual(result["budget"]["used_bytes"], charged)
        self.assertEqual(returned, ids[: len(returned)])
        self.assertLess(len(returned), len(ids))
        # Exhaustion is sticky: no further read returns bodies, and fetches are refused too.
        again = server.call_tool("office_read", {"channel": CHANNEL})
        self.assertEqual(again["code"], "budget_exhausted")
        self.assertEqual(server.call_tool("office_read", {"message_id": ids[0]})["code"], "budget_exhausted")
        # Nothing was consumed past the last returned message: the rest is still pending.
        rest = self.bob.read(channel=CHANNEL, limit=500)["messages"]
        self.assertEqual([m["untrusted_message"]["id"] for m in rest], ids[len(returned):])

    def flaky_origin(self, office, first):
        """The scope pre-check's lookup misses (as during a rotation) or fails once; later
        lookups, e.g. the one inside the Office operation, see the real channel."""
        real = office._channel_origin_of
        calls: list[str] = []

        def lookup(message_id: str):
            calls.append(message_id)
            if len(calls) == 1:
                if isinstance(first, BaseException):
                    raise first
                return first
            return real(message_id)

        return mock.patch.object(office, "_channel_origin_of", side_effect=lookup)

    def test_fetch_of_a_foreign_channel_message_is_refused_even_if_the_scope_lookup_misses(self) -> None:
        foreign = self.alice.send(None, "SEKRET-TARGET inny kanal", channel="tablica")
        for first in (None, OSError("rotacja w toku")):
            with self.subTest(first=repr(first)):
                server = self.contractor()
                with self.flaky_origin(self.bob, first):
                    refused = server.call_tool("office_read", {"message_id": foreign["id"]})
                self.assertEqual((refused["status"], refused["code"]), ("error", "outside_profile"))
                self.assertNotIn("SEKRET-TARGET", json.dumps(refused))
                self.assertEqual(server._read_bytes, 0)

    def test_ack_and_reply_never_land_on_a_foreign_channel_if_the_scope_lookup_misses(self) -> None:
        foreign = self.alice.send(None, "inny kanal", channel="tablica")
        for first in (None, OSError("rotacja w toku")):
            with self.subTest(first=repr(first)):
                server = self.contractor()
                with self.flaky_origin(self.bob, first):
                    ack = server.call_tool("office_ack", {"message_id": foreign["id"], "note": "wyciek"})
                self.assertEqual((ack["status"], ack["code"]), ("error", "outside_profile"))
                with self.flaky_origin(self.bob, first):
                    reply = server.call_tool("office_send", {"to": "codex-testhost-alice", "body": "wyciek",
                                                             "reply_to": foreign["id"]})
                self.assertEqual((reply["status"], reply["code"]), ("error", "outside_profile"))
        board = self.alice.read(channel="tablica", limit=500)["messages"]
        self.assertEqual([m["untrusted_message"]["id"] for m in board], [foreign["id"]])

    def test_a_fetch_larger_than_the_rest_of_the_budget_is_refused_without_charge(self) -> None:
        (big,) = self.post(1, "b" * 8000)
        server = self.contractor(limits=ContractorLimits(read_budget_bytes=5000))
        read = server.call_tool("office_read", {"channel": CHANNEL})
        self.assertEqual(read["count"], 1)
        used = read["budget"]["used_bytes"]
        refused = server.call_tool("office_read", {"message_id": big})
        self.assertEqual(refused["code"], "budget_exhausted")
        self.assertGreater(refused["needed_bytes"], refused["budget"]["left_bytes"])
        self.assertEqual(refused["budget"]["used_bytes"], used)

    def test_kind_query_is_refused_and_pages_reach_every_match_once(self) -> None:
        # 25 decisions (more than one page) between notes. A kind query never consumes, so
        # in this profile it would return the same first 20 on every call: it is refused
        # before any budget is spent, and plain pages return every message exactly once.
        ids: list[str] = []
        decisions: list[str] = []
        for i in range(25):
            ids.append(self.alice.send(None, f"notatka {i}", channel=CHANNEL)["id"])
            decisions.append(self.alice.send(None, f"decyzja {i}", kind="decision", channel=CHANNEL)["id"])
            ids.append(decisions[-1])
        server = self.contractor()
        for kind in ("decision", "note", ""):
            with self.subTest(kind=kind):
                refused = server.call_tool("office_read", {"channel": CHANNEL, "kind": kind})
                self.assertEqual((refused["status"], refused["code"]), ("error", "outside_profile"))
                self.assertIn("kind is off", refused["message"])
                self.assertNotIn("messages", refused)
        self.assertEqual(server._read_bytes, 0)
        seen: list[str] = []
        for _ in range(10):
            page = server.call_tool("office_read", {"channel": CHANNEL})
            self.assertEqual(page["status"], "ok")
            seen += [m["untrusted_message"]["id"] for m in page["messages"]]
            if page["remaining"] == 0:
                break
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(seen, ids)
        wanted = set(decisions)
        self.assertEqual([i for i in seen if i in wanted], decisions)
        self.assertEqual(server.call_tool("office_read", {"channel": CHANNEL})["count"], 0)

    def test_contractor_read_result_carries_the_payload_once_at_the_charged_size(self) -> None:
        self.post(20, "{i:02d} " + "p" * 1500 + ' "cytat" C:\\tmp')
        server = self.contractor()
        replies = self.serve(server, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "office_read", "arguments": {"channel": CHANNEL}}}])
        result = replies[1]["result"]
        self.assertEqual(set(result), {"content", "isError"})
        text = result["content"][0]["text"]
        payload = self.tool_payload(result)
        self.assertEqual((payload["status"], payload["count"]), ("ok", 20))
        charged = payload["budget"]["charged_bytes"]
        self.assertEqual(charged, sum(frame_bytes(m) for m in payload["messages"]))
        # Every frame sits in the text once; the rest is the small fixed envelope (status,
        # counters, budget, handling).
        envelope = json.dumps(dict(payload, messages=[]), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        frames_text = sum(len(json.dumps(m, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                          for m in payload["messages"])
        self.assertEqual(len(text.encode("utf-8")),
                         len(envelope.encode("utf-8")) + frames_text + payload["count"] - 1)
        self.assertLess(len(envelope.encode("utf-8")), 1024)
        # Each frame is charged at its size in the serialized line (the text escaped once
        # more), and the whole tool result on the wire is one copy of the payload, never two.
        self.assertGreater(charged, frames_text)
        wire = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self.assertGreaterEqual(wire, charged)
        self.assertLess(wire, charged + 2048)

    def raw_lines(self, server: OfficeMcpServer, lines: list[dict]) -> dict[int, bytes]:
        """The JSON-RPC response lines exactly as the server wrote them, by request id."""
        raw = b"".join(json.dumps(line).encode("utf-8") + b"\n" for line in lines)
        stdout = io.BytesIO()
        server.serve(io.BytesIO(raw), stdout)
        out = [line for line in stdout.getvalue().split(b"\n") if line]
        return {json.loads(line)["id"]: line for line in out}

    @staticmethod
    def wire_size(frame: dict) -> int:
        """Bytes one frame occupies in the serialized JSON-RPC line: compact JSON text of the
        tool result, which the response line then encodes again as a JSON string."""
        text = json.dumps(frame, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2

    def test_budget_counts_the_serialized_response_of_escape_heavy_bodies(self) -> None:
        # Review round 3: a quote or a backslash is escaped once in the tool result text and
        # once more when that text is a JSON string in the response line. Counting the frame
        # once let escape-heavy pages reach the contractor at about twice the budget.
        self.post(40, "{i:02d} " + '"\\' * 700 + ' "cytat" C:\\tmp\\x')
        server = self.contractor()
        requests = [{"jsonrpc": "2.0", "id": n, "method": "tools/call",
                     "params": {"name": "office_read", "arguments": {"channel": CHANNEL}}}
                    for n in range(1, 13)]
        lines = self.raw_lines(server, requests)
        delivered = 0
        wire_total = 0
        exhausted = False
        for n in range(1, 13):
            line = lines[n]
            wire_total += len(line)
            payload = self.tool_payload(json.loads(line)["result"])
            if payload["status"] == "error":
                self.assertEqual(payload["code"], "budget_exhausted")
                exhausted = True
                continue
            delivered += payload["count"]
            # Charged at exactly the bytes its frames occupy in this serialized line.
            self.assertEqual(payload["budget"]["charged_bytes"],
                             sum(self.wire_size(m) for m in payload["messages"]))
            self.assertLessEqual(sum(self.wire_size(m) for m in payload["messages"]), len(line))
        self.assertTrue(exhausted)
        self.assertGreater(delivered, 0)
        self.assertLess(delivered, 40)
        # All serialized responses together stay within the budget plus a small fixed
        # envelope per call (status, counters, budget, JSON-RPC framing).
        self.assertLessEqual(wire_total, CONTRACTOR_READ_BUDGET_BYTES + 1024 * len(lines))

    def test_message_id_fetch_is_charged_its_serialized_size(self) -> None:
        (message_id,) = self.post(1, '"\\' * 3000)
        server = self.contractor()
        lines = self.raw_lines(server, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": "office_read",
                                                    "arguments": {"message_id": message_id}}}])
        payload = self.tool_payload(json.loads(lines[1])["result"])
        self.assertEqual(payload["code"], "fetched")
        charged = payload["budget"]["charged_bytes"]
        self.assertEqual(charged, self.wire_size(payload["message"]))
        self.assertGreater(charged, 4 * 6000)
        self.assertLessEqual(len(lines[1]), charged + 1024)

    def test_full_profile_tool_result_still_has_structured_content(self) -> None:
        self.post(1)
        full = OfficeMcpServer(self.bob, heartbeat_ttl_s=None)
        replies = self.serve(full, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "office_read", "arguments": {"channel": CHANNEL}}}])
        result = replies[1]["result"]
        self.assertEqual(result["structuredContent"]["count"], 1)
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])

    def test_tuned_page_and_body_limits_apply(self) -> None:
        self.post(5, "{i} " + "t" * 300)
        server = self.contractor(limits=ContractorLimits(read_page=2, read_body_bytes=100))
        read = server.call_tool("office_read", {"channel": CHANNEL, "limit": 5})
        self.assertEqual((read["count"], read["remaining"], read["page_limit"]), (2, 3, 2))
        self.assertTrue(all(len(m["untrusted_message"]["body"].encode("utf-8")) == 100 for m in read["messages"]))

    def test_invalid_limit_is_still_refused(self) -> None:
        server = self.contractor()
        self.assertEqual(server.call_tool("office_read", {"channel": CHANNEL, "limit": 0})["code"], "invalid_limit")

    def test_full_profile_read_is_unchanged(self) -> None:
        body = "f" * 5000
        self.post(25, body)
        full = OfficeMcpServer(self.bob, heartbeat_ttl_s=None)
        read = full.call_tool("office_read", {"channel": CHANNEL})
        self.assertEqual(read["count"], 25)
        self.assertEqual(read["messages"][0]["untrusted_message"]["body"], body)
        for key in ("returned_bytes", "budget_stopped", "budget", "page_limit"):
            self.assertNotIn(key, read)
        self.assertNotIn("truncated", read["messages"][0])


class CoreCapTests(ContractorCase):
    def test_capped_core_read_stops_before_a_frame_that_does_not_fit(self) -> None:
        first = self.alice.send(None, "a" * 1000, channel=CHANNEL)["id"]
        self.alice.send(None, "b" * 1000, channel=CHANNEL)
        stopped = self.bob.read(channel=CHANNEL, max_total_bytes=100)
        self.assertEqual((stopped["status"], stopped["count"], stopped["budget_stopped"]), ("ok", 0, True))
        self.assertEqual((stopped["returned_bytes"], stopped["remaining"], stopped["cursor_advanced"]), (0, 2, False))
        one = self.bob.read(channel=CHANNEL, max_total_bytes=1500, max_body_bytes=4000)
        self.assertEqual([m["untrusted_message"]["id"] for m in one["messages"]], [first])
        self.assertEqual((one["remaining"], one["budget_stopped"]), (1, True))
        self.assertEqual(one["returned_bytes"], frame_bytes(one["messages"][0]))
        self.assertEqual(self.bob.read(channel=CHANNEL)["count"], 1)

    def test_budget_stop_on_a_gap_keeps_the_retained_generations_pending(self) -> None:
        # Small logs with one retained generation: twelve posts rotate four times, so the
        # oldest generations are evicted and a reader without a cursor meets a gap.
        sender = self.office("codex-testhost-alice", inbox_max_bytes=1024, inbox_retain=1)
        sent = [sender.send(None, f"m{i:02d} " + "r" * 150, channel=CHANNEL)["id"] for i in range(12)]
        retained = [m["untrusted_message"]["id"]
                    for m in self.office("qwen-testhost-carol").read(channel=CHANNEL, limit=500)["messages"]]
        live = [json.loads(line)["id"] for line in
                channel_path(self.home, CHANNEL).read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(retained, sent[-len(retained):])
        self.assertLess(len(retained), len(sent))
        # The retained rotated generation holds messages the live log does not.
        self.assertLess(len(live), len(retained))
        self.assertEqual(live, retained[-len(live):])
        stopped = self.bob.read(channel=CHANNEL, max_total_bytes=10)
        self.assertEqual((stopped["gap_detected"], stopped["budget_stopped"]), (True, True))
        self.assertEqual((stopped["count"], stopped["cursor_advanced"]), (0, False))
        # The next read still starts at the oldest retained generation: nothing left unread by
        # the budget stop is skipped by acknowledging the gap.
        full = self.bob.read(channel=CHANNEL, limit=500)
        self.assertEqual([m["untrusted_message"]["id"] for m in full["messages"]], retained)

    def test_invalid_caps_are_refused(self) -> None:
        for cap in (0, -1, True, "10"):
            with self.subTest(cap=cap):
                self.assertEqual(self.bob.read(channel=CHANNEL, max_body_bytes=cap)["code"], "invalid_limit")
                self.assertEqual(self.bob.read(channel=CHANNEL, max_total_bytes=cap)["code"], "invalid_limit")


class WaitCapTests(ContractorCase):
    def test_seventh_wait_is_refused(self) -> None:
        server = self.contractor()
        for _ in range(CONTRACTOR_MAX_WAITS):
            self.assertEqual(server.call_tool("office_wait", {"channel": CHANNEL, "timeout_s": 0})["status"], "ok")
        refused = server.call_tool("office_wait", {"channel": CHANNEL, "timeout_s": 0})
        self.assertEqual((refused["status"], refused["code"]), ("error", "contractor_wait_limit"))
        self.assertEqual((refused["waits_used"], refused["max_waits"]), (6, 6))
        self.assertIn("do not wait again", refused["message"])

    def test_refused_scope_does_not_spend_a_wait(self) -> None:
        server = self.contractor(limits=ContractorLimits(max_waits=1))
        self.assertEqual(server.call_tool("office_wait", {"channel": "tablica", "timeout_s": 0})["code"],
                         "outside_profile")
        self.assertEqual(server.call_tool("office_wait", {"channel": CHANNEL, "timeout_s": 0})["status"], "ok")
        self.assertEqual(server.call_tool("office_wait", {"timeout_s": 0})["code"], "contractor_wait_limit")

    def test_blocking_waits_through_serve_share_the_same_counter(self) -> None:
        server = self.contractor(limits=ContractorLimits(max_waits=2))
        replies = self.serve(server, [
            {"jsonrpc": "2.0", "id": i, "method": "tools/call",
             "params": {"name": "office_wait", "arguments": {"channel": CHANNEL, "timeout_s": 0}}}
            for i in (1, 2, 3)
        ])
        codes = [self.tool_payload(replies[i]["result"])["code"] for i in (1, 2, 3)]
        self.assertEqual(codes.count("contractor_wait_limit"), 1)
        # Admitted waits end as timeout, or as cancelled when stdin EOF closes them first.
        self.assertTrue(all(code in {"timeout", "cancelled", "contractor_wait_limit"} for code in codes), codes)
        # Blocking results go out through the same single-copy contractor result.
        self.assertTrue(all("structuredContent" not in replies[i]["result"] for i in (1, 2, 3)))
        refused = next(replies[i]["result"] for i in (1, 2, 3)
                       if self.tool_payload(replies[i]["result"])["code"] == "contractor_wait_limit")
        self.assertTrue(refused["isError"])

    def test_full_profile_waits_are_not_counted(self) -> None:
        full = OfficeMcpServer(self.bob, heartbeat_ttl_s=None)
        for _ in range(CONTRACTOR_MAX_WAITS + 2):
            self.assertEqual(full.call_tool("office_wait", {"channel": CHANNEL, "timeout_s": 0})["status"], "ok")


class DescriptionTests(ContractorCase):
    def tools(self, server: OfficeMcpServer) -> dict[str, dict]:
        replies = self.serve(server, [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        return {tool["name"]: tool for tool in replies[1]["result"]["tools"]}

    def test_contractor_descriptions_state_each_limit_in_one_line(self) -> None:
        tools = self.tools(self.contractor())
        self.assertEqual(set(tools), set(CONTRACTOR_TOOLS))
        send = tools["office_send"]["description"]
        self.assertIn("Contractor limit: body at most 4096 bytes", send)
        self.assertIn("summary plus refs", send)
        read = tools["office_read"]["description"]
        for fragment in ("at most 20 per call", "cut to 2048 bytes", "message_id",
                         "65536 bytes of returned messages in total per run", "budget_exhausted", "peek is off",
                         "so is the kind filter", "each returned once"):
            self.assertIn(fragment, read)
        self.assertNotIn("Optional `kind`", read)
        self.assertIn("`alert` is a label only", read)
        for name in ("office_dilemma_open", "office_dilemma_answer"):
            self.assertIn("escapes included", tools[name]["description"])
        self.assertIn("at most 6 office_wait calls per run", tools["office_wait"]["description"])
        for name in ("office_send", "office_read", "office_wait", "office_ack",
                     "office_dilemma_open", "office_dilemma_answer"):
            self.assertEqual(tools[name]["description"].count("Contractor limit"), 1, name)
            self.assertNotIn("\n", tools[name]["description"])
        schema = tools["office_read"]["inputSchema"]["properties"]
        self.assertNotIn("peek", schema)
        self.assertNotIn("kind", schema)
        self.assertEqual((schema["limit"]["maximum"], schema["limit"]["default"]), (20, 20))
        self.assertIn("message_id", schema)

    def test_limits_are_stated_as_a_soft_guardrail_of_this_mcp_process(self) -> None:
        # The budget lives in one MCP server process; a CLI with a shell could call the full
        # office.cmd. Descriptions and the initialize instructions say so plainly.
        server = self.contractor()
        replies = self.serve(server, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                                      {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        instructions = replies[1]["result"]["instructions"]
        for fragment in ("soft transport guardrail", "this MCP server process", "office.cmd",
                         "not a security boundary"):
            self.assertIn(fragment, instructions)
        tools = {tool["name"]: tool for tool in replies[2]["result"]["tools"]}
        # Every tool with a contractor limit says where it holds and how it is bypassed.
        for name in ("office_send", "office_read", "office_ack", "office_dilemma_open",
                     "office_dilemma_answer", "office_wait"):
            description = tools[name]["description"]
            self.assertIn("soft guardrail of this MCP process", description, name)
            self.assertIn("not a security boundary", description, name)
            self.assertIn("full office.cmd", description, name)
            self.assertEqual(description.count("soft guardrail"), 1, name)
        full = self.serve(OfficeMcpServer(self.bob, heartbeat_ttl_s=None),
                          [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}])
        self.assertNotIn("guardrail", full[1]["result"]["instructions"])

    def test_descriptions_say_which_results_the_read_budget_covers(self) -> None:
        tools = self.tools(self.contractor())
        self.assertIn("only office_read returns message bodies", tools["office_read"]["description"])
        for name in ("office_dilemma_status", "office_delivery"):
            description = tools[name]["description"]
            self.assertIn("metadata only", description, name)
            self.assertIn("not charged to the read budget", description, name)
            self.assertNotIn("\n", description)

    def test_tuned_limits_show_in_the_descriptions(self) -> None:
        tools = self.tools(self.contractor(limits=ContractorLimits(
            max_send_bytes=1000, read_page=5, read_body_bytes=512, read_budget_bytes=9999, max_waits=2)))
        self.assertIn("at most 1000 bytes", tools["office_send"]["description"])
        read = tools["office_read"]["description"]
        self.assertIn("at most 5 per call", read)
        self.assertIn("cut to 512 bytes", read)
        self.assertIn("9999 bytes of returned messages in total", read)
        self.assertIn("at most 2 office_wait calls", tools["office_wait"]["description"])

    def test_full_profile_descriptions_are_unchanged(self) -> None:
        tools = self.tools(OfficeMcpServer(self.bob, heartbeat_ttl_s=None))
        shared = {tool["name"]: tool for tool in TOOLS}
        self.assertEqual(tools, json.loads(json.dumps(shared)))
        self.assertNotIn("Contractor limit", json.dumps(tools))

    def test_limits_are_refused_outside_the_contractor_profile(self) -> None:
        with self.assertRaises(ValueError):
            OfficeMcpServer(self.bob, heartbeat_ttl_s=None, contractor_limits=ContractorLimits())


class BudgetCoverageTests(ContractorCase):
    """The read budget covers the results that carry message text: office_read pages and
    message_id fetches. Every other contractor result is fixed metadata (ids, agents,
    timestamps, an option key, a state), so it is not charged and works after exhaustion."""

    MARKERS = ("PYTANIE-QX", "OPCJA-OX", "UZASADNIENIE-RX", "NOTATKA-NX", "NOTATKA-DX")

    def test_status_and_delivery_never_return_message_text_and_are_not_charged(self) -> None:
        carol = self.office("qwen-testhost-carol")
        dilemma = self.alice.dilemma_open(CHANNEL, "PYTANIE-QX " + "q" * 3000,
                                          {"A": "OPCJA-OX " + "o" * 500, "B": "b"})
        answer = carol.dilemma_answer(dilemma["id"], "A", "UZASADNIENIE-RX " + "r" * 3000)
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(self.alice.ack(answer["id"], note="NOTATKA-NX " + "n" * 3000)["status"], "ok")
        self.assertEqual(carol.ack(dilemma["id"], note="NOTATKA-DX " + "d" * 3000)["status"], "ok")
        server = self.contractor(limits=ContractorLimits(read_budget_bytes=100))
        calls = [
            ("office_dilemma_status", {"message_id": dilemma["id"]}),
            ("office_delivery", {"message_id": dilemma["id"], "channel": CHANNEL}),
            ("office_delivery", {"message_id": answer["id"], "channel": CHANNEL}),
        ]
        for name, arguments in calls:
            with self.subTest(name=name, before="exhaustion"):
                result = server.call_tool(name, arguments)
                self.assertEqual(result["status"], "ok")
                text = json.dumps(result, ensure_ascii=False)
                for marker in self.MARKERS:
                    self.assertNotIn(marker, text)
                self.assertNotIn("budget", result)
                self.assertLess(len(text.encode("utf-8")), 1024)
        self.assertEqual(server._read_bytes, 0)
        self.assertEqual(server.call_tool("office_read", {"channel": CHANNEL})["code"], "budget_exhausted")
        for name, arguments in calls:
            with self.subTest(name=name, after="exhaustion"):
                self.assertEqual(server.call_tool(name, arguments)["status"], "ok")
        self.assertEqual(server.call_tool("office_dilemma_status", {"message_id": dilemma["id"]})["state"],
                         "acknowledged")

    def test_write_results_never_echo_the_written_text(self) -> None:
        server = self.contractor()
        dilemma = self.alice.dilemma_open(CHANNEL, "q", OPTIONS)
        results = [
            server.call_tool("office_send", {"channel": CHANNEL, "body": "PYTANIE-QX tresc"}),
            server.call_tool("office_ack", {"message_id": dilemma["id"], "note": "NOTATKA-NX tresc"}),
            server.call_tool("office_dilemma_answer", {"message_id": dilemma["id"], "choice": "A",
                                                       "rationale": "UZASADNIENIE-RX tresc"}),
        ]
        for result in results:
            self.assertEqual(result["status"], "ok")
            text = json.dumps(result, ensure_ascii=False)
            for marker in self.MARKERS:
                self.assertNotIn(marker, text)
        self.assertEqual(server._read_bytes, 0)


class CliFlagTests(ContractorCase):
    def parse(self, *argv: str):
        return build_parser().parse_args(["--agent", "claude-testhost-bob", "mcp", *argv])

    def test_absent_flags_keep_the_defaults(self) -> None:
        self.assertIsNone(contractor_limits_from_args(self.parse("--profile", "contractor", "--channel", CHANNEL)))
        self.assertIsNone(contractor_limits_from_args(self.parse()))

    def test_flags_build_the_limits(self) -> None:
        args = self.parse("--profile", "contractor", "--channel", CHANNEL, "--max-send-bytes", "1000",
                          "--read-page", "5", "--read-body-bytes", "512", "--max-read-bytes", "9999",
                          "--max-waits", "2")
        self.assertEqual(contractor_limits_from_args(args), ContractorLimits(1000, 5, 512, 9999, 2))
        partial = self.parse("--profile", "contractor", "--channel", CHANNEL, "--max-read-bytes", "1000")
        self.assertEqual(contractor_limits_from_args(partial), ContractorLimits(read_budget_bytes=1000))

    def test_bad_or_misplaced_flags_are_refused(self) -> None:
        with self.assertRaises(CoordError) as misplaced:
            contractor_limits_from_args(self.parse("--max-read-bytes", "1000"))
        self.assertEqual(misplaced.exception.code, "invalid_mcp_limits")
        with self.assertRaises(CoordError) as bad:
            contractor_limits_from_args(self.parse("--profile", "contractor", "--channel", CHANNEL,
                                                   "--read-page", "0"))
        self.assertEqual(bad.exception.code, "invalid_mcp_limits")

    def run_mcp(self, *argv: str, lines: list[dict]) -> subprocess.CompletedProcess:
        raw = b"".join(json.dumps(line).encode("utf-8") + b"\n" for line in lines)
        return subprocess.run(
            [sys.executable, "-I", str(OFFICE_PY), "--agent", "claude-testhost-bob", "mcp", "--no-heartbeat", *argv],
            input=raw, env=self.cli_env(), capture_output=True, timeout=60, check=False,
            creationflags=NO_WINDOW,
        )

    def test_mcp_process_uses_the_flags(self) -> None:
        completed = self.run_mcp(
            "--profile", "contractor", "--channel", CHANNEL, "--max-send-bytes", "10", "--max-waits", "1",
            lines=[
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "office_send", "arguments": {"channel": CHANNEL, "body": "x" * 11}}},
            ],
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        replies = {r["id"]: r for r in (json.loads(line) for line in completed.stdout.decode("utf-8").splitlines())}
        send = next(t for t in replies[1]["result"]["tools"] if t["name"] == "office_send")
        self.assertIn("at most 10 bytes", send["description"])
        self.assertEqual(self.tool_payload(replies[2]["result"])["code"], "contractor_body_too_large")

    def test_mcp_flags_with_full_profile_exit_with_a_json_error(self) -> None:
        completed = self.run_mcp("--max-waits", "2", lines=[])
        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual((payload["status"], payload["code"]), ("error", "invalid_mcp_limits"))


class OwnPostTests(ContractorCase):
    """A contractor's own posts (progress, dilemma, ACK) are already known to it: they neither
    end a wait nor come back from a read, so they spend no wait and no read budget."""

    def test_own_posts_do_not_end_a_wait_or_charge_the_read_budget(self) -> None:
        server = self.contractor(limits=ContractorLimits(max_waits=2))
        server.call_tool("office_send", {"channel": CHANNEL, "body": "postep: krok 1 gotowy"})
        server.call_tool("office_dilemma_open", {"channel": CHANNEL, "question": "ktory?", "options": OPTIONS})
        started = time.monotonic()
        waited = server.call_tool("office_wait", {"channel": CHANNEL, "timeout_s": 0.4})
        self.assertEqual((waited["code"], waited["new"]), ("timeout", False))
        self.assertGreaterEqual(time.monotonic() - started, 0.35)
        read = server.call_tool("office_read", {"channel": CHANNEL})
        self.assertEqual((read["count"], read["remaining"], read["own_skipped"]), (0, 0, 2))
        self.assertEqual(read["budget"]["used_bytes"], 0)
        # A post by anyone else still ends the wait at once and is the only thing read.
        other = self.alice.send(None, "odpowiedz", channel=CHANNEL)["id"]
        mine = server.call_tool("office_send", {"channel": CHANNEL, "body": "postep: krok 2"})["id"]
        self.assertEqual(server.call_tool("office_wait", {"channel": CHANNEL, "timeout_s": 5})["code"], "message")
        fresh = server.call_tool("office_read", {"channel": CHANNEL})
        self.assertEqual([m["untrusted_message"]["id"] for m in fresh["messages"]], [other])
        self.assertEqual((fresh["own_skipped"], fresh["remaining"]), (1, 0))
        self.assertEqual(fresh["budget"]["used_bytes"], frame_bytes(fresh["messages"][0]))
        self.assertNotIn(mine, json.dumps(fresh))

    def test_own_posts_keep_their_place_after_a_foreign_one_left_pending(self) -> None:
        server = self.contractor(limits=ContractorLimits(read_page=1))
        first = self.alice.send(None, "pierwsza", channel=CHANNEL)["id"]
        server.call_tool("office_send", {"channel": CHANNEL, "body": "moja"})
        second = self.alice.send(None, "druga", channel=CHANNEL)["id"]
        page = server.call_tool("office_read", {"channel": CHANNEL})
        self.assertEqual(([m["untrusted_message"]["id"] for m in page["messages"]], page["remaining"]), ([first], 1))
        page = server.call_tool("office_read", {"channel": CHANNEL})
        self.assertEqual([m["untrusted_message"]["id"] for m in page["messages"]], [second])
        self.assertEqual((page["own_skipped"], page["remaining"]), (1, 0))

    def test_full_profile_and_core_still_see_own_posts(self) -> None:
        full = OfficeMcpServer(self.bob, heartbeat_ttl_s=None)
        own = full.call_tool("office_send", {"channel": CHANNEL, "body": "moje"})["id"]
        self.assertEqual(full.call_tool("office_wait", {"channel": CHANNEL, "timeout_s": 0})["code"], "message")
        read = full.call_tool("office_read", {"channel": CHANNEL})
        self.assertEqual([m["untrusted_message"]["id"] for m in read["messages"]], [own])
        self.assertNotIn("own_skipped", read)


class WorkerArchitectWorkerTests(ContractorCase):
    """TODO 4 flow: a dependency goes worker -> architect -> worker and the worker takes the
    next step from the new messages only, without re-reading the whole board."""

    def test_dilemma_round_trip_reads_only_new_messages(self) -> None:
        history = [self.alice.send(None, f"historia {i}", channel=CHANNEL)["id"] for i in range(30)]
        # One wait is enough for one dilemma cycle, in the real order open -> wait -> answer,
        # even with a progress post in between.
        worker = self.contractor(office=self.bob, peers=("codex-testhost-arch",), limits=ContractorLimits(max_waits=1))
        seen: list[str] = []
        while True:
            page = worker.call_tool("office_read", {"channel": CHANNEL})
            seen += [m["untrusted_message"]["id"] for m in page["messages"]]
            if not page["remaining"]:
                break
        self.assertEqual(seen, history)
        opened = worker.call_tool("office_dilemma_open", {
            "channel": CHANNEL, "question": "ktory kontrakt?", "options": OPTIONS, "correlation_id": "k1"})
        self.assertEqual(opened["status"], "ok")
        progress = worker.call_tool("office_send", {"channel": CHANNEL, "body": "czekam na decyzje"})
        self.assertEqual(progress["status"], "ok")

        architect = self.contractor(office=self.office("codex-testhost-arch"), peers=("claude-testhost-bob",))
        answered: dict = {}

        def answer_later() -> None:
            answered.update(architect.call_tool("office_dilemma_answer", {
                "message_id": opened["id"], "choice": "B", "rationale": "v2 zgodny z consumer_b"}))

        timer = threading.Timer(0.3, answer_later)
        timer.start()
        try:
            woke = worker.call_tool("office_wait", {"channel": CHANNEL, "timeout_s": 10})
        finally:
            timer.join(timeout=10)
        self.assertEqual(answered["status"], "ok")
        self.assertEqual((woke["code"], woke["new"]), ("message", True))
        self.assertGreaterEqual(woke["waited_s"], 0.2)
        answer = answered
        fresh = worker.call_tool("office_read", {"channel": CHANNEL})
        fresh_ids = [m["untrusted_message"]["id"] for m in fresh["messages"]]
        self.assertEqual(fresh_ids, [answer["id"]])
        self.assertEqual(fresh["own_skipped"], 2)
        self.assertFalse(set(fresh_ids) & set(history))
        self.assertEqual(worker.call_tool("office_wait", {"channel": CHANNEL, "timeout_s": 0})["code"],
                         "contractor_wait_limit")
        status = worker.call_tool("office_dilemma_status", {"message_id": opened["id"]})
        self.assertEqual(status["state"], "answered")
        ack = worker.call_tool("office_ack", {"message_id": answer["id"], "note": "przechodze na v2"})
        self.assertEqual(ack["status"], "ok")
        self.assertEqual(worker.call_tool("office_dilemma_status", {"message_id": opened["id"]})["state"],
                         "acknowledged")
        self.assertLess(fresh["budget"]["used_bytes"], CONTRACTOR_READ_BUDGET_BYTES)
        self.assertEqual(fresh["budget"]["charged_bytes"], sum(frame_bytes(m) for m in fresh["messages"]))


if __name__ == "__main__":
    unittest.main()
