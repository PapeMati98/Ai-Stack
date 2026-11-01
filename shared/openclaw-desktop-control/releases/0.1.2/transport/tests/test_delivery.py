"""Delivery lifecycle: claims, ACK crash, recovery, alarm, STOP, cancel."""

import os
import tempfile
import threading
import unittest

from openclaw_control import delivery, models
from openclaw_control.adapters import DisabledAdapter, FakeControlAdapter
from openclaw_control.store import (
    ClaimMismatchError,
    ControlStore,
    ReconcileError,
    StoppedError,
)


def _mkstore(tmp: str, name: str = "authority.db") -> ControlStore:
    return ControlStore(os.path.join(tmp, name))


def _bound(store: ControlStore, i: int, now: float) -> dict:
    return delivery.enqueue_prompt(
        store,
        task_id="task-%d" % i,
        generation=1,
        provider="p",
        native_thread="n",
        raw_text="tekst %d" % i,
        entities=[],
        now=now,
    )


class DeliveryTest(unittest.TestCase):
    def test_concurrent_claim_single_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "authority.db")
            s0 = ControlStore(path)
            try:
                _bound(s0, 0, 1.0)
            finally:
                s0.close()
            barrier = threading.Barrier(2)
            got: list = []

            def worker(name: str) -> None:
                store = ControlStore(path)
                try:
                    barrier.wait(timeout=10)
                    got.append(store.claim_one(name, 2.0))
                finally:
                    store.close()

            threads = [
                threading.Thread(target=worker, args=("a",)),
                threading.Thread(target=worker, args=("b",)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            winners = [g for g in got if g is not None]
            self.assertEqual(len(winners), 1)
            self.assertEqual(len(got), 2)

    def test_ack_crash_unknown_no_resend_single_alarm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                adapter = FakeControlAdapter()
                adapter.script_ack_unknown_once = True
                row = _bound(store, 0, 100.0)
                claimed = store.claim_one("w", 100.0)
                self.assertIsNotNone(claimed)
                out = delivery.deliver(
                    store, adapter, claimed["dispatch_id"], 100.0
                )
                self.assertEqual(out["state"], models.DELIVERY_UNKNOWN)
                # The native run DID happen: lookup proves it, no resend.
                self.assertEqual(
                    adapter.lookup(row["dispatch_id"]),
                    "%s:run1" % row["dispatch_id"],
                )
                # Restart recovery finds nothing dispatching (no resend).
                self.assertEqual(store.recover_crashed(101.0), [])
                self.assertIsNone(store.claim_one("w2", 101.0))
                # Alarm only after 120s, exactly once.
                self.assertIsNone(store.pop_unknown_alarm(219.0))
                first = store.pop_unknown_alarm(221.0)
                self.assertIsNotNone(first)
                self.assertEqual(
                    first["dispatch_id"], row["dispatch_id"]
                )
                self.assertIsNone(store.pop_unknown_alarm(500.0))
                # Explicit owner reconciliation confirms delivery.
                rec = store.reconcile(
                    row["dispatch_id"], True, 500.0
                )
                self.assertEqual(rec["state"], models.DELIVERED)
                store.record_start(row['dispatch_id'], 500.0)
                store.record_result(row['dispatch_id'], models.COMPLETED, 500.0)
                # ...or requeue exactly once by owner decision.
                store2row = _bound(store, 1, 500.0)
                c2 = store.claim_one("w", 500.0)
                store.mark_unknown(c2["dispatch_id"], 500.0)
                rq = store.reconcile(
                    c2["dispatch_id"], False, 500.0
                )
                self.assertEqual(rq["state"], models.QUEUED)
                _ = store2row
            finally:
                store.close()

    def test_crash_before_ack_recovery_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "authority.db")
            store = ControlStore(path)
            row = _bound(store, 0, 10.0)
            claimed = store.claim_one("crashed-worker", 10.0)
            self.assertIsNotNone(claimed)
            store.close()  # crash: claim orphaned, process gone
            store2 = ControlStore(path)
            try:
                recovered = store2.recover_crashed(11.0)
                self.assertEqual(len(recovered), 1)
                self.assertEqual(
                    recovered[0]["dispatch_id"], row["dispatch_id"]
                )
                now = store2.require_dispatch(row["dispatch_id"])
                self.assertEqual(now["state"], models.DELIVERY_UNKNOWN)
            finally:
                store2.close()

    def test_ack_start_result_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                adapter = FakeControlAdapter()
                row = _bound(store, 0, 1.0)
                claimed = store.claim_one("w", 1.0)
                out = delivery.deliver(
                    store, adapter, claimed["dispatch_id"], 1.0
                )
                self.assertEqual(out["ack"], models.ACKED)
                self.assertEqual(out["state"], models.DELIVERED)
                # Result before start is illegal.
                with self.assertRaises(ClaimMismatchError):
                    store.record_result(
                        row["dispatch_id"], models.COMPLETED, 2.0
                    )
                started = delivery.start_turn(
                    store, adapter, row["dispatch_id"], 7, 2.0
                )
                self.assertEqual(started["state"], models.RUNNING)
                done = delivery.finish(
                    store, row["dispatch_id"], models.COMPLETED, 3.0
                )
                self.assertEqual(done["state"], models.COMPLETED)
                # Double ACK after delivery is illegal (no replay).
                with self.assertRaises(ClaimMismatchError):
                    store.record_ack(
                        row["dispatch_id"], models.ACKED, 4.0
                    )
            finally:
                store.close()

    def test_stop_blocks_new_dispatch_and_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                store.set_stop(models.STOP_SCOPE_ALL, 1.0, "owner")
                with self.assertRaises(StoppedError):
                    _bound(store, 0, 2.0)
                store.clear_stop(models.STOP_SCOPE_ALL)
                row = _bound(store, 0, 3.0)
                claimed = store.claim_one("w", 3.0)
                delivery.deliver(
                    store, FakeControlAdapter(),
                    claimed["dispatch_id"], 3.0,
                )
                store.set_stop("task:task-0", 4.0, "owner")
                # STOP after send must not erase an observed native start.
                started = store.record_start(row["dispatch_id"], 5.0)
                self.assertEqual(started["state"], models.RUNNING)
                # New sends for the stopped task stay blocked.
                with self.assertRaises(StoppedError):
                    delivery.enqueue_prompt(
                        store, "task-0", 1, "p", "n", "kolejna", [], 6.0
                    )
                self.assertIsNone(store.claim_one("w", 6.0))
                # STOP survives: still listed.
                self.assertTrue(store.is_stopped("task-0"))
                self.assertFalse(store.is_stopped("other"))
            finally:
                store.close()

    def test_cancel_pending_vs_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                # No confirmation -> stays pending, never "stopped".
                row = _bound(store, 0, 1.0)
                c = store.claim_one("w", 1.0)
                delivery.deliver(
                    store, FakeControlAdapter(), c["dispatch_id"], 1.0
                )
                delivery.start_turn(
                    store, FakeControlAdapter(),
                    row["dispatch_id"], 1, 2.0,
                )
                pending = store.request_cancel(row["dispatch_id"], 3.0)
                self.assertEqual(
                    pending["state"], models.CANCEL_PENDING
                )
                still = store.confirm_cancel(
                    row["dispatch_id"], False, 4.0
                )
                self.assertEqual(still["state"], models.CANCEL_PENDING)
                done = store.confirm_cancel(
                    row["dispatch_id"], True, 5.0
                )
                self.assertEqual(done["state"], models.CANCELLED)
                # Full path via adapter that refuses confirmation.
                row2 = _bound(store, 1, 6.0)
                c2 = store.claim_one("w", 6.0)
                delivery.deliver(
                    store, FakeControlAdapter(), c2["dispatch_id"], 6.0
                )
                adapter = FakeControlAdapter()
                adapter.refuse_stop_once = True
                out = delivery.cancel(store, adapter,
                                      row2["dispatch_id"], 7.0)
                self.assertEqual(out["state"], models.CANCEL_PENDING)
                # Terminal states cannot be cancelled.
                with self.assertRaises(ReconcileError):
                    store.request_cancel(row["dispatch_id"], 8.0)
            finally:
                store.close()

    def test_disabled_adapter_never_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                row = _bound(store, 0, 1.0)
                claimed = store.claim_one("w", 1.0)
                out = delivery.deliver(
                    store, DisabledAdapter("desktop-code"),
                    claimed["dispatch_id"], 1.0,
                )
                # Transport raised -> uncertain, never blind-retried.
                self.assertEqual(out["state"], models.DELIVERY_UNKNOWN)
                _ = row
            finally:
                store.close()

    def test_unbound_dispatch_refused_no_send(self) -> None:
        # Regression: a prompt with no explicit target (empty provider
        # AND empty thread) must never reach an adapter; deliver fails
        # closed and the envelope is preserved for later binding.
        from openclaw_control.delivery import UnboundTargetError

        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                row = delivery.enqueue_prompt(
                    store, "", 0, "", "", "tekst bez celu", [], 1.0
                )
                self.assertEqual(row["target_binding"], "")
                claimed = store.claim_one("w", 1.0)
                self.assertIsNotNone(claimed)
                adapter = FakeControlAdapter()
                with self.assertRaises(UnboundTargetError):
                    delivery.deliver(
                        store, adapter, claimed["dispatch_id"], 1.0
                    )
                self.assertEqual(adapter.deliver_calls, 0)
                self.assertEqual(
                    store.require_dispatch(
                        row["dispatch_id"])["raw_text"],
                    "tekst bez celu",
                )
            finally:
                store.close()

    def test_takeover_and_fresh_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                delivery.takeover(store, "t1", "host-B", 50.0, "owner")
                got = store.get_takeover("t1")
                self.assertIsNotNone(got)
                self.assertEqual(got["host_session"], "host-B")
                delivery.require_fresh_status(49.0, 50.0)
                with self.assertRaises(delivery.StaleStatusError):
                    delivery.require_fresh_status(10.0, 50.0)
                with self.assertRaises(delivery.StaleStatusError):
                    delivery.require_fresh_status(110.0, 50.0)
            finally:
                store.close()

    def test_diagnostics_carry_no_raw_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _mkstore(tmp)
            try:
                row = _bound(store, 0, 1.0)
                summary = delivery.redacted_summary(row)
                self.assertNotIn("raw_text", summary)
                self.assertIn("text_sha256", summary)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
