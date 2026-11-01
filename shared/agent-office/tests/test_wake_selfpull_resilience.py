"""Self-pull resilience: busy turn, STOP and lost/interrupted timers never double-handle a reply.

Combines paths the unit tests in test_wake_self_pull.py cover one by one. Every scenario ends
with the same invariant: one reserved reply, one `woken` ledger row, one consumed entry, and a
stale token for every older timer. Native Cron semantics (idle-only, one-shot, session-only)
are modelled by the caller: a timer "fires" when the test calls pull with its token.
"""

from __future__ import annotations

import unittest
from unittest import mock

from support import PACKAGE_ROOT  # noqa: F401  (puts the package on sys.path)

from coordfs.errors import CoordError
from coordfs.wake import Wake
from test_wake import AGENT_A, AGENT_B
import test_wake_self_pull as _pull

BINDING = "lead-pull"


_BASE = _pull.WakeSelfPullTests


class SelfPullResilienceTests(unittest.TestCase):
    # Fixtures only; the inherited scenarios already run in their own modules.
    setUp = _BASE.setUp
    tearDown = _BASE.tearDown
    wake = _BASE.wake
    office = _BASE.office
    enroll_pull = _BASE.enroll_pull
    request = _BASE.request
    reply = _BASE.reply
    pull = _BASE.pull

    def arm(self, wake: Wake, **kwargs) -> dict:
        return wake.pull_arm(binding_id=BINDING, caller=AGENT_A, **kwargs)

    def confirm(self, wake: Wake, token: str, job: str) -> str:
        return wake.pull_confirm(binding_id=BINDING, caller=AGENT_A, token=token, job_id=job)["action"]

    def woken_rows(self, wake: Wake, trigger_id: str) -> int:
        return sum(1 for row in wake.ledger()["entries"]
                   if row.get("binding_id") == BINDING and row.get("decision") == "woken"
                   and trigger_id in (row.get("trigger_ref"), row.get("trigger_message_id"),
                                      row.get("message_id")))

    def assert_single_delivery(self, wake: Wake, corr: str, trigger_id: str) -> None:
        correlation = wake.correlations.load(corr)
        self.assertEqual(list(correlation.get("reserved", {})), [trigger_id])
        self.assertEqual(correlation["wakes"], 1)
        self.assertEqual(self.woken_rows(wake, trigger_id), 1)
        wake.correlations.consume(corr, caller=AGENT_A, trigger_message_id=trigger_id)
        wake.correlations.consume(corr, caller=AGENT_A, trigger_message_id=trigger_id)  # replayed
        self.assertEqual(wake.correlations.load(corr)["consumed"], [trigger_id])
        # Once consumed, the reply is never returned by a later arm or pull.
        self.assertNotIn(trigger_id, [row["trigger_message_id"] for row in wake._pull_undelivered(BINDING)])

    # 1. busy -> held -> handled once after release -----------------------------------
    def test_resilience_busy_turn_holds_the_reply_and_it_is_handled_once_after_release(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        request_id = self.request(wake)
        armed = self.arm(wake)
        timer = armed["next"]
        self.assertEqual(self.confirm(wake, timer["token"], "job-1"), "keep")
        self.office(AGENT_A).presence(ttl_s=7200, status="busy")
        reply_id = self.reply(request_id)
        # Cron is idle-only: the long turn delays the timer. Re-arming meanwhile adds nothing.
        self.clock[0] += 3600
        again = self.arm(wake)
        self.assertEqual((again["armed"], again["next"], again["confirmed_job_id"]),
                         ("existing", None, "job-1"))
        self.office(AGENT_A).presence(ttl_s=7200, status="idle")  # the turn ended
        handled = self.pull(wake, timer)
        self.assertEqual(handled["action"], "handle")
        self.assertEqual([r["trigger_message_id"] for r in handled["replies"]], [reply_id])
        self.assertIsNone(handled["next"])
        # The same fired job cannot run a second handle.
        repeat = self.pull(wake, timer)
        self.assertEqual((repeat["action"], repeat["reason"], repeat["replies"]),
                         ("stop", "stale_pull_token", []))
        self.assert_single_delivery(wake, "c1", reply_id)

    def test_resilience_a_self_pull_does_not_depend_on_a_stale_busy_presence(self) -> None:
        # The pull runs inside the recipient's own idle turn; a busy left behind must not
        # hold the reply forever (contrast: CLI resume defers, test_wake_correlation:93).
        wake = self.wake()
        self.enroll_pull(wake)
        request_id = self.request(wake)
        timer = self.arm(wake)["next"]
        self.office(AGENT_A).presence(ttl_s=7200, status="busy")
        reply_id = self.reply(request_id)
        self.clock[0] += timer["delay_s"]
        handled = self.pull(wake, timer)
        self.assertEqual((handled["action"], [r["trigger_message_id"] for r in handled["replies"]]),
                         ("handle", [reply_id]))
        self.assertNotIn("deferred", [row.get("decision") for row in wake.ledger()["entries"]])
        self.assert_single_delivery(wake, "c1", reply_id)

    # 2. STOP: no next timer, reply kept, one redelivery after START -----------------
    def test_resilience_stop_leaves_no_timer_keeps_the_reply_and_redelivers_once(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        request_id = self.request(wake)
        self.request(wake, corr="c2")  # still awaited, so a handle schedules a next timer
        armed = self.arm(wake)
        reply_id = self.reply(request_id)
        self.clock[0] += armed["next"]["delay_s"]
        handled = self.pull(wake, armed["next"])
        self.assertEqual(handled["action"], "handle")
        self.assertIsNotNone(handled["next"])
        # Interrupted before consume; the operator sets STOP before the next timer fires.
        wake.stop()
        self.clock[0] += handled["next"]["delay_s"]
        stopped = self.pull(wake, handled["next"])
        self.assertEqual((stopped["action"], stopped["reason"], stopped["replies"], stopped["next"]),
                         ("stop", "global_stop", [], None))
        with self.assertRaises(CoordError) as caught:
            self.arm(wake)
        self.assertEqual(caught.exception.code, "wake_pull_global_stop")
        self.assertEqual(self.pull(wake, handled["next"])["reason"], "stale_pull_token")
        # After START the reserved reply is returned once more, still counted once.
        wake.start()
        rearmed = self.arm(wake)
        self.assertEqual(rearmed["armed"], "new")
        self.clock[0] += rearmed["next"]["delay_s"]
        again = self.pull(wake, rearmed["next"])
        self.assertEqual((again["action"], [r["trigger_message_id"] for r in again["replies"]]),
                         ("handle", [reply_id]))
        self.assert_single_delivery(wake, "c1", reply_id)

    def test_resilience_stop_set_during_the_pull_tick_schedules_no_timer_and_keeps_the_reply(self) -> None:
        # Independent review (Codex, 2026-09-28): STOP appearing while the tick reserves a
        # reply was ignored because a reply was present; the pull handed it out and
        # scheduled one more timer under STOP.
        wake = self.wake()
        self.enroll_pull(wake)
        request_id = self.request(wake)
        self.request(wake, corr="c2")  # still awaited, so a handle would schedule a next timer
        armed = self.arm(wake)
        reply_id = self.reply(request_id)
        self.clock[0] += armed["next"]["delay_s"]
        original_tick = wake._tick_binding

        def tick_then_stop(binding, *, dry_run):
            decisions = original_tick(binding, dry_run=dry_run)
            wake.stop()  # the operator's STOP lands between reservation and the final gate
            return decisions

        with mock.patch.object(wake, "_tick_binding", side_effect=tick_then_stop):
            result = self.pull(wake, armed["next"])
        self.assertEqual((result["action"], result.get("reason"), result["replies"], result["next"]),
                         ("stop", "global_stop", [], None))
        self.assertEqual(wake._pull_read(BINDING)["status"], "stopped")
        # The reservation made in that tick is kept and returned once after START.
        wake.start()
        rearmed = self.arm(wake)
        self.clock[0] += rearmed["next"]["delay_s"]
        again = self.pull(wake, rearmed["next"])
        self.assertEqual((again["action"], [r["trigger_message_id"] for r in again["replies"]]),
                         ("handle", [reply_id]))
        self.assert_single_delivery(wake, "c1", reply_id)

    def test_resilience_a_timer_created_after_stop_is_deleted_at_confirm(self) -> None:
        # Recheck (Codex, 2026-09-28): arm -> STOP -> CronCreate -> confirm kept the new job.
        # Every path that hands out `next` ends in confirm, so confirm is the one STOP gate
        # that also closes the arm and early-pull windows.
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        timer = self.arm(wake)["next"]
        wake.stop()
        confirm = wake.pull_confirm(binding_id=BINDING, caller=AGENT_A, token=timer["token"], job_id="job-1")
        self.assertEqual((confirm["action"], confirm.get("reason")), ("delete_job", "global_stop"))
        self.assertEqual(wake._pull_read(BINDING)["status"], "stopped")
        wake.start()
        self.assertEqual(self.pull(wake, timer)["reason"], "stale_pull_token")

    def test_resilience_stop_during_arm_or_an_early_pull_never_keeps_a_timer(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        original_read = wake._pull_read

        def read_then_stop(binding_id):
            state = original_read(binding_id)
            wake.stop()  # STOP lands after arm's own gate
            return state

        with mock.patch.object(wake, "_pull_read", side_effect=read_then_stop):
            timer = self.arm(wake)["next"]
        self.assertEqual(self.confirm(wake, timer["token"], "job-1"), "delete_job")
        wake.start()
        timer = self.arm(wake)["next"]  # a fresh chain; its timer fires early
        with mock.patch.object(wake, "_pull_read", side_effect=read_then_stop):
            early = self.pull(wake, timer)
        if early["next"] is not None:  # the early path re-issued before it saw STOP
            self.assertEqual(self.confirm(wake, early["next"]["token"], "job-2"), "delete_job")
        else:
            self.assertEqual((early["action"], early["reason"]), ("stop", "global_stop"))

    def test_resilience_stop_between_the_confirm_gate_and_the_job_write_deletes_the_job(self) -> None:
        # R1 (recheck 2, 2026-09-28): STOP landing after confirm's gate but before job_id
        # was recorded returned keep with the state still armed.
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        timer = self.arm(wake)["next"]
        original_write = wake._atomic_replace_json

        def stop_then_write(path, value):
            if value.get("job_id") == "job-1" and value.get("status") == "armed":
                wake.stop()  # exactly between the gate and the job_id write
            return original_write(path, value)

        with mock.patch.object(wake, "_atomic_replace_json", side_effect=stop_then_write):
            confirm = wake.pull_confirm(binding_id=BINDING, caller=AGENT_A, token=timer["token"], job_id="job-1")
        self.assertEqual((confirm["action"], confirm.get("reason")), ("delete_job", "global_stop"))
        self.assertEqual(wake._pull_read(BINDING)["status"], "stopped")
        wake.start()
        self.assertEqual(self.pull(wake, timer)["reason"], "stale_pull_token")

    def test_resilience_confirm_is_idempotent_and_stop_after_it_is_ordered_after(self) -> None:
        # The linearization point is confirm's final STOP read (after the job_id write).
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        timer = self.arm(wake)["next"]
        self.assertEqual(self.confirm(wake, timer["token"], "job-1"), "keep")
        self.assertEqual(self.confirm(wake, timer["token"], "job-1"), "keep")  # idempotent
        self.assertEqual(self.confirm(wake, timer["token"], "job-2"), "delete_job")  # duplicate
        wake.stop()
        # A re-confirm of the kept job now reports the STOP and stops the chain ...
        self.assertEqual(self.confirm(wake, timer["token"], "job-1"), "delete_job")
        # ... and a job that still fires does no work and schedules nothing.
        self.clock[0] += timer["delay_s"]
        fired = self.pull(wake, timer)
        self.assertEqual((fired["action"], fired["replies"], fired["next"]), ("stop", [], None))

    def test_resilience_concurrent_stop_and_confirm_never_deadlock_or_keep_after_stop(self) -> None:
        import threading

        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        outcomes: list[tuple[str, bool]] = []
        for round_no in range(25):
            wake.start()
            timer = self.arm(wake)["next"]
            barrier = threading.Barrier(2)
            result: dict[str, str] = {}

            def confirm_side() -> None:
                barrier.wait()
                result["action"] = self.confirm(wake, timer["token"], f"job-{round_no}")

            def stop_side() -> None:
                barrier.wait()
                wake.stop()

            threads = [threading.Thread(target=confirm_side), threading.Thread(target=stop_side)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
                self.assertFalse(thread.is_alive(), "deadlock: a thread did not finish")
            state = wake._pull_read(BINDING)
            # Invariant: a kept job means confirm linearized before STOP, so a re-confirm
            # (the next observer) must see STOP and stop; a delete means state is stopped.
            if result["action"] == "keep":
                self.assertEqual(self.confirm(wake, timer["token"], f"job-{round_no}"), "delete_job")
            self.assertEqual(wake._pull_read(BINDING)["status"], "stopped", state)
            outcomes.append((result["action"], True))
        self.assertEqual(len(outcomes), 25)

    # 3. timers lost or interrupted ----------------------------------------------------
    def test_resilience_next_timer_never_created_is_reissued_and_the_old_token_is_dead(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        request_id = self.request(wake)
        self.request(wake, corr="c2")
        armed = self.arm(wake)
        self.assertEqual(self.confirm(wake, armed["next"]["token"], "job-1"), "keep")
        reply_id = self.reply(request_id)
        self.clock[0] += armed["next"]["delay_s"]
        handled = self.pull(wake, armed["next"])
        lost_next = handled["next"]
        # The session died after handle, before CronCreate/confirm of `next`: nothing is
        # confirmed, so a plain arm (no `lost`) re-issues under a new token.
        reissued = self.arm(wake)
        self.assertEqual(reissued["armed"], "reissued")
        self.assertNotEqual(reissued["next"]["token"], lost_next["token"])
        self.assertEqual(self.confirm(wake, lost_next["token"], "job-late"), "delete_job")
        self.assertEqual(self.pull(wake, lost_next)["reason"], "stale_pull_token")
        self.clock[0] += reissued["next"]["delay_s"]
        again = self.pull(wake, reissued["next"])
        self.assertEqual([r["trigger_message_id"] for r in again["replies"]], [reply_id])
        self.assert_single_delivery(wake, "c1", reply_id)

    def test_resilience_lost_confirmed_timer_and_late_old_job_handle_only_once(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        request_id = self.request(wake)
        first = self.arm(wake)["next"]
        self.assertEqual(self.confirm(wake, first["token"], "job-1"), "keep")
        self.clock[0] += 3600  # CronList no longer shows job-1 (app restart)
        replacement = self.arm(wake, lost=True)
        self.assertEqual(replacement["armed"], "reissued")
        second = replacement["next"]
        self.assertEqual(self.confirm(wake, second["token"], "job-2"), "keep")
        self.assertEqual(self.confirm(wake, first["token"], "job-1"), "delete_job")
        reply_id = self.reply(request_id)
        self.clock[0] += second["delay_s"]
        results = [self.pull(wake, second), self.pull(wake, first), self.pull(wake, second)]
        self.assertEqual([r["action"] for r in results], ["handle", "stop", "stop"])
        self.assertEqual([r.get("reason") for r in results[1:]], ["stale_pull_token", "stale_pull_token"])
        self.assertEqual([r["trigger_message_id"] for r in results[0]["replies"]], [reply_id])
        self.assert_single_delivery(wake, "c1", reply_id)

    # 4. semantics of decision_reasons (UAT C observation) ------------------------------
    def test_resilience_decision_reasons_describe_scanned_messages_not_the_stop_reason(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        timer = self.arm(wake, max_pulls=1)["next"]
        # The counterpart sends something that answers no open request (e.g. an ACK of an
        # older, closed chain) while the current correlation is still open.
        self.office(AGENT_B).send(AGENT_A, "ack starszego lancucha", kind="note")
        self.clock[0] += timer["delay_s"]
        result = self.pull(wake, timer)
        self.assertEqual((result["action"], result["reason"], result["next"]),
                         ("stop", "pull_budget_exhausted", None))
        self.assertEqual(result["decision_reasons"], ["no_open_correlation"])
        self.assertEqual(wake.correlations.load("c1")["status"], "open")

    def test_resilience_decision_reasons_are_empty_without_other_messages(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        timer = self.arm(wake, max_pulls=1)["next"]
        self.clock[0] += timer["delay_s"]
        result = self.pull(wake, timer)
        self.assertEqual((result["action"], result["reason"], result["decision_reasons"]),
                         ("stop", "pull_budget_exhausted", []))


del _BASE  # keep the imported TestCase out of this module's collection

if __name__ == "__main__":
    unittest.main()
