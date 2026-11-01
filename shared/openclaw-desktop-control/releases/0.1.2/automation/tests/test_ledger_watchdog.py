"""Race / crash / replay and recovery-gate tests for ledger + watchdog.

Run from automation/:  python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import tempfile
import unittest

from openclaw_ops import models
from openclaw_ops.adapters import (
    FakeNativeAdapter,
    ObserveOnlyAdapter,
    ProviderStatus,
    StaleFenceError,
)
from openclaw_ops.clock import FakeClock
from openclaw_ops.ledger import Ledger
from openclaw_ops.watchdog import checkpoint_digest, tick, tick_one

T0 = 1_700_000_000.0


class Script:
    """Serve scripted statuses in call order (then repeat the last)."""

    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    def __call__(self, task):
        self.calls += 1
        item = self.states[min(self.calls - 1, len(self.states) - 1)]
        return item(task) if callable(item) else item


def live(now, checkpoint="cp-live", writer=True):
    return ProviderStatus(
        state=models.STATE_RUNNING, observed_wall=now, provider_wall=now,
        revision="r1", checkpoint=checkpoint, writer_active=writer)


def failed(now, transient=True, revision="r1", checkpoint="cp1"):
    return ProviderStatus(
        state=models.STATE_TERMINAL_FAILED, observed_wall=now,
        provider_wall=now, revision=revision, checkpoint=checkpoint,
        writer_active=False, failure_cause="transport-dropped",
        cause_transient=transient)


def cancelled(now):
    return ProviderStatus(
        state=models.STATE_CANCELLED, observed_wall=now, provider_wall=now,
        revision="r2")


def completed(now):
    return ProviderStatus(
        state=models.STATE_TERMINAL_COMPLETED, observed_wall=now,
        provider_wall=now, revision="r1")


def waiting(now):
    return ProviderStatus(
        state=models.STATE_WAITING_USER, observed_wall=now,
        provider_wall=now, revision="r1")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "authority.db")
        self.clock = FakeClock(wall=T0)
        self.ledger = Ledger(self.db)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def grant(self, task_id="t1", provider="fake", **kw):
        args = dict(task_id=task_id, provider=provider,
                    host_session="h1", owner_grant_ref="grant-1",
                    grant_expires=T0 + 3600, now=self.clock.wall())
        args.update(kw)
        return self.ledger.register_task(**args)

    def resolve(self, task_id="t1", transient=True):
        self.ledger.note_cause(task_id, "transport-dropped", transient,
                               True, self.clock.wall())


class RaceAndLeaseTests(Case):
    def test_two_watchdogs_one_recovers(self):
        self.grant()
        self.resolve()
        adapter = FakeNativeAdapter(
            script=Script([lambda t: failed(self.clock.wall())]))
        reg = {"fake": adapter}
        other = Ledger(self.db)  # second watchdog, same authority file
        try:
            token_b = other.acquire_lease("t1", 1, "watchdog-B",
                                          self.clock.wall())
            self.assertIsNotNone(token_b)
            task = self.ledger.get_task("t1")
            dec = tick_one(self.ledger, reg, self.clock, task,
                           self.clock.wall(), owner="watchdog-A")
            self.assertEqual(dec["decision"], "lease-held")
            self.assertEqual(adapter.resume_calls, 0)
            self.assertIsNone(self.ledger.get_intent("t1:1:1"))
            other.release_lease("t1", "watchdog-B", self.clock.wall())
            task = self.ledger.get_task("t1")
            dec = tick_one(self.ledger, reg, self.clock, task,
                           self.clock.wall(), owner="watchdog-A")
            self.assertEqual(dec["decision"], "recovered")
            self.assertEqual(adapter.resume_calls, 1)
        finally:
            other.close()

    def test_lease_expiry_allows_takeover_but_old_token_is_stale(self):
        self.grant()
        t1 = self.ledger.acquire_lease("t1", 1, "watchdog-A",
                                       self.clock.wall())
        self.assertEqual(t1, 1)
        self.clock.advance(wall_delta=models.LEASE_TTL + 1)
        t2 = self.ledger.acquire_lease("t1", 1, "watchdog-B",
                                       self.clock.wall())
        self.assertEqual(t2, 2)
        adapter = FakeNativeAdapter()
        task = self.ledger.get_task("t1")
        adapter.resume(task, "t1:1:1", t2)  # current owner dispatches
        with self.assertRaises(StaleFenceError):
            adapter.resume(task, "t1:1:2", t1)  # superseded token rejected

    def test_renew_increases_fencing_token(self):
        self.grant()
        t1 = self.ledger.acquire_lease("t1", 1, "watchdog-A",
                                       self.clock.wall())
        t2 = self.ledger.renew_lease("t1", 1, "watchdog-A", t1,
                                     self.clock.wall())
        self.assertEqual(t2, t1 + 1)
        # Renew with the superseded token fails; expired lease fails.
        self.assertIsNone(self.ledger.renew_lease(
            "t1", 1, "watchdog-A", t1, self.clock.wall()))
        self.clock.advance(wall_delta=models.LEASE_TTL + 1)
        self.assertIsNone(self.ledger.renew_lease(
            "t1", 1, "watchdog-A", t2, self.clock.wall()))

    def test_generation_cas_blocks_old_host(self):
        self.grant()
        ok = self.ledger.transfer_authority(
            "t1", 1, "h2", "grant-2", T0 + 7200, self.clock.wall())
        self.assertTrue(ok)
        # Old host's expected generation no longer matches: CAS misses.
        self.assertFalse(self.ledger.transfer_authority(
            "t1", 1, "h1-evil", "grant-x", T0 + 7200, self.clock.wall()))
        task = self.ledger.get_task("t1")
        self.assertEqual(task["generation"], 2)
        self.assertIsNone(self.ledger.acquire_lease(
            "t1", 1, "watchdog-A", self.clock.wall()))

    def test_checkpoint_digest_stable_and_versioned(self):
        m = {"v": 1, "files": ["a.py"], "turn": "n1"}
        self.assertEqual(checkpoint_digest(m), checkpoint_digest(dict(m)))
        m2 = dict(m, turn="n2")
        self.assertNotEqual(checkpoint_digest(m), checkpoint_digest(m2))


class RecoveryGateTests(Case):
    def test_false_stall_never_dispatches(self):
        self.grant()
        self.resolve()
        seen = [f"cp-{i}" for i in range(5)]
        adapter = FakeNativeAdapter(script=Script(
            [lambda t, c=c: live(self.clock.wall(), checkpoint=c,
                                 writer=False) for c in seen]))
        reg = {"fake": adapter}
        for _ in range(5):
            task = self.ledger.get_task("t1")
            dec = tick_one(self.ledger, reg, self.clock, task,
                           self.clock.wall())
            self.assertIn(dec["decision"], ("monitor", "suspected-stall"))
            self.clock.advance(wall_delta=10)
        self.assertEqual(adapter.resume_calls, 0)
        self.assertEqual(self.ledger.get_task("t1")["attempts"], 0)

    def test_quiet_checkpoint_marks_suspect_but_no_dispatch(self):
        self.grant()
        self.resolve()
        adapter = FakeNativeAdapter(script=Script(
            [lambda t: live(self.clock.wall(), checkpoint="same",
                            writer=False)]))
        reg = {"fake": adapter}
        task = self.ledger.get_task("t1")
        self.assertEqual(
            tick_one(self.ledger, reg, self.clock, task,
                     self.clock.wall())["decision"], "monitor")
        self.clock.advance(wall_delta=models.CHECKPOINT_QUIET + 1)
        task = self.ledger.get_task("t1")
        dec = tick_one(self.ledger, reg, self.clock, task,
                       self.clock.wall())
        self.assertEqual(dec["decision"], "suspected-stall")
        self.assertEqual(adapter.resume_calls, 0)

    def test_cancellation_after_lease_aborts(self):
        self.grant()
        self.resolve()
        adapter = FakeNativeAdapter(script=Script([
            lambda t: failed(self.clock.wall(), revision="r1"),
            lambda t: cancelled(self.clock.wall()),
        ]))
        task = self.ledger.get_task("t1")
        dec = tick_one(self.ledger, {"fake": adapter}, self.clock, task,
                       self.clock.wall())
        self.assertEqual(dec["decision"], "aborted")
        self.assertEqual(adapter.resume_calls, 0)
        self.assertEqual(self.ledger.get_task("t1")["attempts"], 0)

    def test_completed_and_wait_states(self):
        self.grant(task_id="done")
        self.grant(task_id="wait")
        reg = {"fake": FakeNativeAdapter(script=Script([
            lambda t: completed(self.clock.wall())
            if t["task_id"] == "done"
            else waiting(self.clock.wall()),
        ]))}
        decs = {d["task_id"]: d["decision"]
                for d in tick(self.ledger, reg, self.clock)}
        self.assertEqual(decs["done"], "completed")
        self.assertEqual(decs["wait"], "needs-owner")
        self.assertEqual(self.ledger.get_task("done")["state"],
                         models.TASK_COMPLETED)

    def test_stale_status_means_unknown(self):
        self.grant()
        self.resolve()
        old = self.clock.wall() - models.STATUS_FRESHNESS - 5
        adapter = FakeNativeAdapter(script=Script([
            ProviderStatus(state=models.STATE_TERMINAL_FAILED,
                           observed_wall=old, provider_wall=old,
                           failure_cause="x", cause_transient=True),
        ]))
        task = self.ledger.get_task("t1")
        dec = tick_one(self.ledger, {"fake": adapter}, self.clock, task,
                       self.clock.wall())
        self.assertEqual(dec["decision"], "unknown")
        self.assertEqual(self.ledger.get_task("t1")["lease_owner"], "")

    def test_transient_only_and_cause_resolved(self):
        self.grant(task_id="perm")
        self.grant(task_id="unresolved")
        self.ledger.note_cause("perm", "business-decision", False, True,
                               self.clock.wall())
        self.ledger.note_cause("unresolved", "quota", True, False,
                               self.clock.wall())
        reg = {"fake": FakeNativeAdapter(script=Script([
            lambda t: failed(self.clock.wall(),
                             transient=(t["task_id"] == "unresolved")),
        ]))}
        decs = {d["task_id"]: d["decision"]
                for d in tick(self.ledger, reg, self.clock)}
        self.assertEqual(decs["perm"], "needs-owner")
        self.assertEqual(decs["unresolved"], "needs-owner")

    def test_backoff_and_exhaustion(self):
        self.grant()
        self.resolve()
        adapter = FakeNativeAdapter(script=Script(
            [lambda t: failed(self.clock.wall())]))
        reg = {"fake": adapter}
        task = self.ledger.get_task("t1")
        self.assertEqual(
            tick_one(self.ledger, reg, self.clock, task,
                     self.clock.wall())["decision"], "recovered")
        task = self.ledger.get_task("t1")
        self.assertEqual(
            tick_one(self.ledger, reg, self.clock, task,
                     self.clock.wall())["decision"], "backoff-wait")
        self.assertEqual(adapter.resume_calls, 1)
        self.clock.advance(wall_delta=models.BACKOFFS[0])
        self.ledger.note_attempt("t1", models.MAX_ATTEMPTS, 0,
                                 self.clock.wall())
        task = self.ledger.get_task("t1")
        self.assertEqual(
            tick_one(self.ledger, reg, self.clock, task,
                     self.clock.wall())["decision"], "exhausted")

    def test_grant_gates(self):
        self.grant(task_id="nog", owner_grant_ref="")
        self.grant(task_id="exp", grant_expires=T0 - 1)
        adapter = FakeNativeAdapter(script=Script(
            [lambda t: failed(self.clock.wall())]))
        reg = {"fake": adapter}
        decs = {d["task_id"]: d["decision"]
                for d in tick(self.ledger, reg, self.clock)}
        self.assertEqual(decs["nog"], "needs-owner")
        self.assertEqual(decs["exp"], "grant-expired")
        self.assertEqual(adapter.resume_calls, 0)


class CrashReplayTests(Case):
    def test_crash_after_intent_before_ack_adopts_no_resend(self):
        self.grant()
        self.resolve()
        adapter = FakeNativeAdapter(script=Script(
            [lambda t: failed(self.clock.wall())]))
        adapter.script_ack_unknown_once = True
        reg = {"fake": adapter}
        task = self.ledger.get_task("t1")
        first = tick_one(self.ledger, reg, self.clock, task,
                         self.clock.wall())
        self.assertEqual(first["decision"], "dispatched-uncertain")
        run_id = first["native_run_id"]
        # Simulate a restarted watchdog: fresh objects, same authority file.
        self.ledger.close()
        ledger2 = Ledger(self.db)
        try:
            task = ledger2.get_task("t1")
            second = tick_one(ledger2, reg, self.clock, task,
                              self.clock.wall())
            self.assertEqual(second["decision"], "adopted-no-resend")
            self.assertEqual(second["native_run_id"], run_id)
            self.assertEqual(adapter.resume_calls, 1)
        finally:
            ledger2.close()
            self.ledger = Ledger(self.db)

    def test_unsupported_adapter_stays_observe_only(self):
        self.grant(provider="mystery")
        reg = {"mystery": ObserveOnlyAdapter("mystery")}
        task = self.ledger.get_task("t1")
        dec = tick_one(self.ledger, reg, self.clock, task,
                       self.clock.wall())
        self.assertEqual(dec["decision"], "observe-only")
        self.assertIsNone(self.ledger.get_intent("t1:1:1"))

    def test_unconnected_provider_visible(self):
        self.grant(provider="ghost")
        task = self.ledger.get_task("t1")
        dec = tick_one(self.ledger, {}, self.clock, task,
                       self.clock.wall())
        self.assertEqual(dec["decision"], "unconnected")


if __name__ == "__main__":
    unittest.main()
