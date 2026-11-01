"""Approval-gate and notification-state tests (Telegram decision contract).

Run from automation/:  python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest

from openclaw_ops import models
from openclaw_ops.approvals import (
    ApprovalStore,
    PendingRequest,
    TelegramMessage,
    canonical_text,
    decide,
)
from openclaw_ops.helpdesk import scope_hash
from openclaw_ops.notify import Outbox

T0 = 1_700_000_000.0
ALLOW = {("sender-1", "chat-1")}


def pending(scope="candidate-code", action="approve",
            request_id="RR-example-crm-INC-1-r3"):
    return PendingRequest(
        request_id=request_id, stable_id="INC-1", revision="r3",
        scope=scope, scope_hash=scope_hash(scope), action=action,
        expires_at=T0 + 600, created_at=T0)


def msg(text, message_id="m1", sender="sender-1", chat="chat-1",
        sent_at=T0, forwarded=False):
    return TelegramMessage(message_id=message_id, sender_id=sender,
                           chat_id=chat, text=text, sent_at=sent_at,
                           is_forwarded=forwarded)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.store = ApprovalStore()
        self.req = pending()
        self.store.add(self.req)

    def valid_text(self):
        return canonical_text(self.req)

    def test_valid_approval_authorises_candidate_only(self):
        v = decide(self.store, ALLOW, msg(self.valid_text()), T0 + 10)
        self.assertTrue(v["authorized"])
        self.assertEqual(v["scope"], "candidate-code")
        self.assertTrue(v["candidate_only"])
        # Exact candidate scope is preserved on the verdict.
        self.assertEqual(v["revision"], "r3")

    def test_expired_request_rejected(self):
        v = decide(self.store, ALLOW, msg(self.valid_text()), T0 + 601)
        self.assertFalse(v["authorized"])
        self.assertEqual(v["reason"], "request-expired")

    def test_forged_hash_rejected(self):
        bad = f"APPROVE {self.req.request_id} r3 deadbeef approve"
        v = decide(self.store, ALLOW, msg(bad), T0 + 10)
        self.assertFalse(v["authorized"])
        self.assertEqual(v["reason"],
                         "revision-scope-hash-action-mismatch")

    def test_replayed_message_rejected(self):
        first = decide(self.store, ALLOW, msg(self.valid_text()), T0 + 10)
        self.assertTrue(first["authorized"])
        second = decide(self.store, ALLOW, msg(self.valid_text(),
                                               sent_at=T0 + 11), T0 + 12)
        # Same message_id reused: replay, even with a fresh timestamp.
        self.assertFalse(second["authorized"])
        self.assertEqual(second["reason"], "replayed-message")

    def test_replay_survives_serialise_restore(self):
        decide(self.store, ALLOW, msg(self.valid_text()), T0 + 10)
        data = self.store.serialise()
        restored = ApprovalStore.restore(data)
        v = decide(restored, ALLOW, msg(self.valid_text(), sent_at=T0 + 11),
                   T0 + 12)
        self.assertFalse(v["authorized"])
        self.assertEqual(v["reason"], "replayed-message")

    def test_wrong_sender_group_forwarded_old_plain_rejected(self):
        cases = [
            (msg(self.valid_text(), sender="attacker"), "untrusted"),
            (msg(self.valid_text(), chat="group-9"), "untrusted"),
            (msg(self.valid_text(), forwarded=True), "forwarded"),
            (msg(self.valid_text(), sent_at=T0 - 400), "stale"),
            (msg("please approve this, thanks"), "not-a-decision"),
            (msg("APPROVE something else entirely now"), "unknown"),
        ]
        for i, (m, _) in enumerate(cases):
            m.message_id = f"mx-{i}"
            v = decide(self.store, ALLOW, m, T0 + 10)
            self.assertFalse(v["authorized"], m)
        reasons = set()
        store2 = ApprovalStore()
        store2.add(pending())
        for i, (m, _) in enumerate(cases):
            m.message_id = f"my-{i}"
            reasons.add(decide(store2, ALLOW, m, T0 + 10)["reason"])
        self.assertIn("untrusted-sender-or-chat", reasons)
        self.assertIn("forwarded-message", reasons)
        self.assertIn("stale-message", reasons)
        self.assertIn("not-a-decision", reasons)
        self.assertIn("unknown-request", reasons)

    def test_deploy_scope_never_unlocked_by_approval(self):
        store = ApprovalStore()
        req = pending(scope="deploy", request_id="RR-x")
        store.add(req)
        v = decide(store, ALLOW, msg(canonical_text(req)), T0 + 10)
        self.assertFalse(v["authorized"])
        self.assertEqual(
            v["reason"], "scope-requires-separate-explicit-authorization")

    def test_inputs_are_data_never_shell(self):
        evil = ("APPROVE RR-example-crm-INC-1-r3 "
                "$(rm -rf /) approve; `id`")
        v = decide(self.store, ALLOW, msg(evil), T0 + 10)
        self.assertFalse(v["authorized"])


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(os.path.join(self.tmp.name, "n.db"))
        self.conn.isolation_level = None
        self.outbox = Outbox(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_sent_confirmed(self):
        self.outbox.enqueue("k1", "owner", "RR-1", "hash1")
        res = self.outbox.send_all(lambda r, h: "sent", T0)
        self.assertEqual(res, [{"key": "k1", "state": "sent"}])
        self.assertEqual(self.outbox.get("k1")["state"],
                         models.NOTIFY_SENT)

    def test_uncertain_ack_never_blind_resends(self):
        calls = []
        self.outbox.enqueue("k1", "owner", "RR-1", "hash1")

        def flaky(ref, digest):
            calls.append(ref)
            return "unknown"

        res = self.outbox.send_all(flaky, T0)
        self.assertEqual(res[0]["state"], "unknown")
        # Second pass: UNKNOWN entries are not pending -> no resend.
        res2 = self.outbox.send_all(flaky, T0 + 60)
        self.assertEqual(res2, [])
        self.assertEqual(calls, ["RR-1"])
        # Explicit owner reconcile requeues exactly once.
        rec = self.outbox.reconcile("k1", False, T0 + 120)
        self.assertEqual(rec["state"], models.NOTIFY_PENDING)
        self.outbox.send_all(lambda r, h: "sent", T0 + 180)
        self.assertEqual(self.outbox.get("k1")["state"],
                         models.NOTIFY_SENT)
        with self.assertRaises(ValueError):
            self.outbox.reconcile("k1", True, T0 + 200)

    def test_transport_exception_becomes_unknown(self):
        self.outbox.enqueue("k1", "owner", "RR-1", "hash1")

        def boom(ref, digest):
            raise TimeoutError("no ack")

        res = self.outbox.send_all(boom, T0)
        self.assertEqual(res[0]["state"], "unknown")

    def test_two_workers_atomically_get_one_claim(self):
        """A live claim is not stolen by a concurrent worker."""
        path = os.path.join(self.tmp.name, "workers.db")
        conn1 = sqlite3.connect(path, check_same_thread=False)
        conn1.isolation_level = None
        conn2 = sqlite3.connect(path, check_same_thread=False)
        conn2.isolation_level = None
        outbox1 = Outbox(conn1)
        outbox2 = Outbox(conn2)
        outbox1.enqueue("k1", "owner", "RR-1", "hash1")
        claimed = threading.Event()
        release = threading.Event()
        calls = []
        worker_result = []
        worker_errors = []

        def hold_claim(entry):
            claimed.set()
            release.wait(2)

        def transport(ref, digest):
            calls.append(ref)
            return "sent"

        def worker():
            try:
                worker_result.extend(outbox1.send_all(
                    transport, T0, after_claim=hold_claim,
                    worker_id="worker-a"))
            except BaseException as exc:  # surfaced below, never swallowed
                worker_errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(claimed.wait(2))
        try:
            second = outbox2.send_all(
                lambda ref, digest: calls.append("duplicate") or "sent",
                T0, worker_id="worker-b")
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(worker_errors, [])
        self.assertEqual(second, [])
        self.assertEqual(calls, ["RR-1"])
        self.assertEqual(outbox2.get("k1")["state"], models.NOTIFY_SENT)
        conn1.close()
        conn2.close()

    def test_crash_after_claim_is_uncertain_and_never_resent(self):
        self.outbox.enqueue("k1", "owner", "RR-1", "hash1")

        def crash_after_claim(entry):
            raise RuntimeError("simulated worker crash")

        with self.assertRaises(RuntimeError):
            self.outbox.send_all(
                lambda ref, digest: self.fail("transport must not run"),
                T0, after_claim=crash_after_claim, worker_id="worker-a")
        self.assertEqual(self.outbox.get("k1")["state"],
                         models.NOTIFY_CLAIMED)

        # A restarted worker sees the durable claim but cannot resend it.
        path = os.path.join(self.tmp.name, "n.db")
        conn2 = sqlite3.connect(path)
        conn2.isolation_level = None
        outbox2 = Outbox(conn2)
        calls = []
        self.assertEqual(
            outbox2.send_all(
                lambda ref, digest: calls.append(ref) or "sent", T0), [])
        self.assertEqual(calls, [])
        self.assertEqual(outbox2.get("k1")["state"],
                         models.NOTIFY_CLAIMED)

        # Once the bounded claim window is over, it becomes UNKNOWN.  It is
        # still not requeued: only explicit reconcile(False) may do that.
        self.assertEqual(
            outbox2.send_all(
                lambda ref, digest: calls.append(ref) or "sent",
                T0 + models.NOTIFY_CLAIM_TTL + 1), [])
        self.assertEqual(calls, [])
        self.assertEqual(outbox2.get("k1")["state"],
                         models.NOTIFY_UNKNOWN)
        conn2.close()


if __name__ == "__main__":
    unittest.main()
