"""Inbox cursor semantics: per-agent cursor, peek, limit, and rotation via events.rotate_if_needed."""

from __future__ import annotations

import json

from office_support import OfficeTestCase

from coordfs.office import FIRST_READ_GAP_TEXT, GAP_TEXT, _first_line_id, cursor_path, inbox_path


class CursorTests(OfficeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.announce("codex-testhost-alice", "claude-testhost-bob")

    def _bodies(self, result: dict) -> list[str]:
        return [item["untrusted_message"]["body"] for item in result["messages"]]

    def test_reading_never_deletes_and_cursor_is_per_agent(self) -> None:
        for index in range(3):
            self.alice.send("claude-testhost-bob", f"m{index}")
        self.assertEqual(self._bodies(self.bob.read()), ["m0", "m1", "m2"])
        self.assertEqual(self.bob.read()["count"], 0)
        self.assertEqual(len(inbox_path(self.home, "claude-testhost-bob").read_text(encoding="utf-8").splitlines()), 3)
        cursor = json.loads(cursor_path(self.home, "claude-testhost-bob", "inbox").read_text(encoding="utf-8"))
        self.assertEqual(cursor["offset"], inbox_path(self.home, "claude-testhost-bob").stat().st_size)
        self.assertFalse(cursor_path(self.home, "codex-testhost-alice", "inbox").exists())

    def test_peek_and_limit(self) -> None:
        for index in range(5):
            self.alice.send("claude-testhost-bob", f"m{index}")
        peeked = self.bob.read(limit=2, peek=True)
        self.assertEqual(self._bodies(peeked), ["m0", "m1"])
        self.assertEqual(peeked["remaining"], 3)
        self.assertTrue(peeked["peek"])
        first = self.bob.read(limit=2)
        self.assertEqual(self._bodies(first), ["m0", "m1"])
        self.assertEqual(first["remaining"], 3)
        rest = self.bob.read(limit=10)
        self.assertEqual(self._bodies(rest), ["m2", "m3", "m4"])
        self.assertEqual(rest["remaining"], 0)
        self.assertEqual(self.bob.read(limit=0)["code"], "invalid_limit")
        self.assertEqual(self.bob.read(limit=True)["code"], "invalid_limit")  # type: ignore[arg-type]

    def test_rotation_keeps_unread_messages_across_one_rotation(self) -> None:
        alice = self.office("codex-testhost-alice", inbox_max_bytes=1024, inbox_retain=2)
        bob = self.office("claude-testhost-bob", inbox_max_bytes=1024, inbox_retain=2)
        inbox = inbox_path(self.home, "claude-testhost-bob")
        rotated = inbox.with_name(inbox.name + ".1")
        pad = "x" * 120

        def body(index: int) -> str:
            return f"m{index}-{pad}"

        sent_bytes = alice.send("claude-testhost-bob", body(0))["bytes"]
        self.assertGreater(sent_bytes * 4, 1024, "four messages must exceed the 1 KiB inbox cap")
        alice.send("claude-testhost-bob", body(1))
        alice.send("claude-testhost-bob", body(2))
        self.assertFalse(rotated.exists())
        self.assertEqual(self._bodies(bob.read(limit=1)), [body(0)])
        alice.send("claude-testhost-bob", body(3))
        self.assertTrue(rotated.is_file(), "fourth message must rotate the inbox")
        alice.send("claude-testhost-bob", body(4))
        self.assertEqual(self._bodies(bob.read()), [body(1), body(2), body(3), body(4)])
        alice.send("claude-testhost-bob", body(5))
        self.assertEqual(self._bodies(bob.read()), [body(5)])
        alice.send("claude-testhost-bob", body(6))
        self.assertTrue(inbox.with_name(inbox.name + ".2").is_file())
        self.assertEqual(self._bodies(bob.read()), [body(6)])
        self.assertEqual(bob.read()["count"], 0)

    def test_rotation_past_retention_is_signalled_not_silent(self) -> None:
        alice = self.office("codex-testhost-alice", inbox_max_bytes=1024, inbox_retain=2)
        bob = self.office("claude-testhost-bob", inbox_max_bytes=1024, inbox_retain=2)
        inbox = inbox_path(self.home, "claude-testhost-bob")
        pad = "x" * 180
        alice.send("claude-testhost-bob", f"m000-{pad}")
        first = bob.read()
        self.assertFalse(first["gap_detected"])
        self.assertNotIn("gap_message", first)
        cursor_head = json.loads(cursor_path(self.home, "claude-testhost-bob", "inbox").read_text(encoding="utf-8"))["head"]
        self.assertTrue(cursor_head)

        def retained_heads() -> list[str]:
            return [_first_line_id(inbox.with_name(f"{inbox.name}.{index}")) for index in (1, 2)]

        # Rotate until the generation holding the cursor is gone (retain=2 -> three rotations).
        index = 1
        while cursor_head in retained_heads() or not inbox.with_name(inbox.name + ".2").is_file():
            alice.send("claude-testhost-bob", f"m{index:03d}-{pad}")
            index += 1
            self.assertLess(index, 60, "rotation never evicted the cursor generation")
        self.assertNotIn(cursor_head, retained_heads())

        peeked = bob.read(peek=True)
        self.assertTrue(peeked["gap_detected"], "peek must report the gap too")
        result = bob.read()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["gap_detected"])
        self.assertEqual(result["gap_message"], GAP_TEXT)
        self.assertNotIn("lost_count", result, "an unmeasured loss must not be reported as a number")
        bodies = self._bodies(result)
        self.assertGreater(len(bodies), 0, "everything still retained is returned")
        self.assertEqual(bodies[-1], f"m{index - 1:03d}-{pad}")
        self.assertNotIn(f"m000-{pad}", bodies)
        # Retained generations are drained oldest-first without duplicates.
        numbers = [int(body[1:4]) for body in bodies]
        self.assertEqual(numbers, list(range(numbers[0], numbers[-1] + 1)))
        self.assertGreater(numbers[0], 1, "the evicted generation is really gone")
        # The gap is acknowledged once; later reads are clean again.
        again = bob.read()
        self.assertFalse(again["gap_detected"])
        self.assertEqual(again["count"], 0)
        alice.send("claude-testhost-bob", f"m{index:03d}-{pad}")
        after = bob.read()
        self.assertFalse(after["gap_detected"])
        self.assertEqual(self._bodies(after), [f"m{index:03d}-{pad}"])

    def test_gap_with_nothing_readable_is_still_reported_once(self) -> None:
        alice = self.office("codex-testhost-alice", inbox_max_bytes=1024, inbox_retain=1)
        bob = self.office("claude-testhost-bob", inbox_max_bytes=1024, inbox_retain=1)
        alice.send("claude-testhost-bob", "m0-" + "x" * 180)
        bob.read()
        # Simulate rotation that evicted the cursor generation and left an empty current file.
        inbox = inbox_path(self.home, "claude-testhost-bob")
        inbox.rename(inbox.with_name(inbox.name + ".1"))
        inbox.with_name(inbox.name + ".1").write_bytes(b'{"id":"other","ts":"t","from":"a","to":"claude-testhost-bob","kind":"note","body":"kept"}\n')
        inbox.write_bytes(b"")
        result = bob.read()
        self.assertTrue(result["gap_detected"])
        self.assertEqual(self._bodies(result), ["kept"])
        self.assertFalse(bob.read()["gap_detected"])

    def test_unread_messages_survive_up_to_retain_rotations(self) -> None:
        alice = self.office("codex-testhost-alice", inbox_max_bytes=1024, inbox_retain=2)
        bob = self.office("claude-testhost-bob", inbox_max_bytes=1024, inbox_retain=2)
        inbox = inbox_path(self.home, "claude-testhost-bob")
        pad = "x" * 180
        for index in range(3):
            alice.send("claude-testhost-bob", f"m{index:02d}-{pad}")
        self.assertEqual(self._bodies(bob.read(limit=1)), [f"m00-{pad}"])
        cursor_head = json.loads(cursor_path(self.home, "claude-testhost-bob", "inbox").read_text(encoding="utf-8"))["head"]
        index = 3
        while _first_line_id(inbox.with_name(inbox.name + ".2")) != cursor_head:
            alice.send("claude-testhost-bob", f"m{index:02d}-{pad}")
            index += 1
            self.assertLess(index, 40)
        result = bob.read()
        self.assertFalse(result["gap_detected"], "cursor generation still retained in .2: no loss")
        self.assertEqual(self._bodies(result), [f"m{number:02d}-{pad}" for number in range(1, index)])
        self.assertEqual(bob.read()["count"], 0)

    def test_rotation_is_bounded_by_retain(self) -> None:
        alice = self.office("codex-testhost-alice", inbox_max_bytes=1024, inbox_retain=2)
        for index in range(40):
            alice.send("claude-testhost-bob", f"m{index:02d}")
        inbox = inbox_path(self.home, "claude-testhost-bob")
        rotated = sorted(path.name for path in inbox.parent.glob("claude-testhost-bob.jsonl.*") if not path.name.endswith(".lock"))
        self.assertEqual(rotated, ["claude-testhost-bob.jsonl.1", "claude-testhost-bob.jsonl.2"])

    def test_reply_to_round_trip(self) -> None:
        sent = self.alice.send("claude-testhost-bob", "question?", kind="question")
        self.bob.send("codex-testhost-alice", "answer.", kind="note", reply_to=sent["id"])
        result = self.alice.read()
        self.assertEqual(result["messages"][0]["untrusted_message"]["reply_to"], sent["id"])


class FirstReadRotationTests(OfficeTestCase):
    """Follow-up audit FAIL 2: rotation before the reader's first read must not be silent.

    Decision: with no cursor the reader drains every retained generation oldest-first AND
    flags `gap_detected` when the rotation ledger records a deleted generation (loss real,
    uncountable). Until rotation number retain+1 nothing has been deleted, so everything is
    returned without a gap (see test_office_rotation_ledger for the exact boundaries).
    """

    PAD = "x" * 180

    def setUp(self) -> None:
        super().setUp()
        self.announce("codex-testhost-alice", "claude-testhost-bob")

    def _bodies(self, result: dict) -> list[str]:
        return [item["untrusted_message"]["body"] for item in result["messages"]]

    def _body(self, index: int) -> str:
        return f"m{index:03d}-{self.PAD}"

    def test_full_window_before_first_read_flags_gap_and_returns_oldest_retained(self) -> None:
        alice = self.office("codex-testhost-alice", inbox_max_bytes=1024, inbox_retain=2)
        bob = self.office("claude-testhost-bob", inbox_max_bytes=1024, inbox_retain=2)
        inbox = inbox_path(self.home, "claude-testhost-bob")
        oldest = inbox.with_name(inbox.name + ".2")
        index = 0
        while not oldest.is_file():
            alice.send("claude-testhost-bob", self._body(index))
            index += 1
            self.assertLess(index, 60, "retention window never filled")
        evicted_head = _first_line_id(oldest)
        # One more eviction so that m000 is provably gone, all before bob ever reads.
        while _first_line_id(oldest) == evicted_head:
            alice.send("claude-testhost-bob", self._body(index))
            index += 1
            self.assertLess(index, 60, "rotation never evicted the oldest generation")
        self.assertFalse(cursor_path(self.home, "claude-testhost-bob", "inbox").exists(), "bob has never read")

        peeked = bob.read(peek=True)
        self.assertTrue(peeked["gap_detected"], "peek must report the gap too")
        result = bob.read()
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["gap_detected"])
        self.assertEqual(result["gap_message"], FIRST_READ_GAP_TEXT)
        self.assertNotIn("lost_count", result, "an unmeasured loss must not be reported as a number")
        bodies = self._bodies(result)
        expected_first = json.loads(oldest.read_bytes().splitlines()[0])["body"]
        self.assertEqual(bodies[0], expected_first, "the oldest retained generation is returned first")
        numbers = [int(body[1:4]) for body in bodies]
        self.assertGreater(numbers[0], 0, "m000 was evicted and is really gone")
        self.assertEqual(numbers, list(range(numbers[0], index)), "oldest-first, consecutive, up to the last sent")
        # Acknowledged once; later reads are clean and continue from the current generation.
        again = bob.read()
        self.assertFalse(again["gap_detected"])
        self.assertEqual(again["count"], 0)
        alice.send("claude-testhost-bob", self._body(index))
        after = bob.read()
        self.assertFalse(after["gap_detected"])
        self.assertEqual(self._bodies(after), [self._body(index)])

    def test_partial_window_before_first_read_returns_everything_without_gap(self) -> None:
        alice = self.office("codex-testhost-alice", inbox_max_bytes=1024, inbox_retain=2)
        bob = self.office("claude-testhost-bob", inbox_max_bytes=1024, inbox_retain=2)
        inbox = inbox_path(self.home, "claude-testhost-bob")
        index = 0
        while not inbox.with_name(inbox.name + ".1").is_file():
            alice.send("claude-testhost-bob", self._body(index))
            index += 1
            self.assertLess(index, 60)
        alice.send("claude-testhost-bob", self._body(index))
        index += 1
        self.assertFalse(inbox.with_name(inbox.name + ".2").exists(), "only one rotation: nothing deleted yet")
        result = bob.read()
        self.assertFalse(result["gap_detected"])
        self.assertNotIn("gap_message", result)
        self.assertEqual(self._bodies(result), [self._body(number) for number in range(index)])
        self.assertEqual(bob.read()["count"], 0)

    def test_wait_sees_an_unread_retained_generation_before_first_read(self) -> None:
        self.alice.send("claude-testhost-bob", "m0")
        inbox = inbox_path(self.home, "claude-testhost-bob")
        # Rotation happened and nothing has landed in the new current generation yet.
        inbox.rename(inbox.with_name(inbox.name + ".1"))
        self.assertEqual(self.bob.wait(timeout_s=0.1)["code"], "message")
        self.assertEqual(self._bodies(self.bob.read()), ["m0"])
        self.assertEqual(self.bob.wait(timeout_s=0.1)["code"], "timeout")
        self.alice.send("claude-testhost-bob", "m1")
        self.assertEqual(self.bob.wait(timeout_s=0.1)["code"], "message")
        self.assertEqual(self._bodies(self.bob.read()), ["m1"])
