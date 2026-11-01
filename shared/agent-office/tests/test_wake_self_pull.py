"""Office Wake self-pull (variant C): the bound session pulls its own correlated replies."""

from __future__ import annotations

import unittest
from unittest import mock

from support import PACKAGE_ROOT  # noqa: F401  (puts the package on sys.path)

from coordfs.errors import CoordError
from coordfs.leases import format_ts
from coordfs.wake import PULL_DRIVER, PULL_SCHEDULE_S, Wake
import test_wake as _p1
from test_wake import AGENT_A, AGENT_B, AGENT_C

LOCAL_ID = "local_123e4567-e89b-12d3-a456-426614174000"


class WakeSelfPullTests(_p1.WakeTests):
    def enroll_pull(self, wake: Wake, *, binding_id: str = "lead-pull", budget: int = 20) -> None:
        wake.enroll(
            binding_id=binding_id,
            office_agent=AGENT_A,
            driver=PULL_DRIVER,
            native_thread_id=LOCAL_ID,
            workspace=str(self.workspace),
            enrolled_by="owner",
            allowed_wakers=[AGENT_B],
            allowed_triggers=["message"],
            wake_kinds=["question", "handoff"],
            budget_per_day=budget,
            max_attempts_per_trigger=1,
            expires_at=format_ts(self.clock[0] + 86400),
            owner_confirm=True,
            correlation_required=True,
        )
        wake.verify(binding_id, owner_confirm=True)

    def request(self, wake: Wake, *, corr: str = "c1", binding_id: str = "lead-pull") -> str:
        sent = self.office(AGENT_A).send(AGENT_B, "zlecenie wezla", kind="question")
        wake.expect(binding_id=binding_id, caller=AGENT_A, task_id="t1", correlation_id=corr,
                    counterpart=AGENT_B, request_message_id=sent["id"], max_wakes=3)
        return sent["id"]

    def reply(self, request_id: str, *, sender: str = AGENT_B) -> str:
        return self.office(sender).send(AGENT_A, "wynik wezla", kind="note", reply_to=request_id)["id"]

    def pull(self, wake: Wake, timer: dict) -> dict:
        return wake.pull(binding_id="lead-pull", caller=AGENT_A, token=timer["token"])

    def test_enrollment_requires_correlation_and_message_only(self) -> None:
        wake = self.wake()
        for triggers, correlated in ((["message", "schedule"], True), (["message"], False)):
            with self.assertRaises(CoordError) as caught:
                wake.enroll(
                    binding_id="bad-pull", office_agent=AGENT_A, driver=PULL_DRIVER,
                    native_thread_id=LOCAL_ID, workspace=str(self.workspace), enrolled_by="owner",
                    allowed_wakers=[AGENT_B], allowed_triggers=triggers,
                    expires_at=format_ts(self.clock[0] + 86400), owner_confirm=True,
                    correlation_required=correlated,
                )
            self.assertEqual(caught.exception.code, "invalid_wake_binding")

    def test_relay_tick_never_dispatches_or_paces_a_self_pull_binding(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.reply(self.request(wake))
        with mock.patch("coordfs.wake.subprocess.run") as process:
            result = wake.tick()
        process.assert_not_called()
        self.assertEqual(result["decisions"], [])
        self.assertIsNone(result["recommended_next_tick_s"])
        self.assertEqual(wake.correlations.load("c1").get("reserved", {}), {})

    def test_full_chain_arm_confirm_dedupe_rotate_handle_consume_stop(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        request_id = self.request(wake)
        armed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        self.assertEqual(armed["armed"], "new")
        first = armed["next"]
        self.assertEqual(first["delay_s"], PULL_SCHEDULE_S[0])
        self.assertIn(first["token"], first["prompt"])
        # Unconfirmed timer + second arm: re-issued under a NEW token, the old one is stale.
        reissued = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        self.assertEqual(reissued["armed"], "reissued")
        self.assertNotEqual(reissued["next"]["token"], first["token"])
        timer = reissued["next"]
        confirm = lambda job, token=None: wake.pull_confirm(  # noqa: E731
            binding_id="lead-pull", caller=AGENT_A, token=token or timer["token"], job_id=job)
        self.assertEqual(confirm("job-1")["action"], "keep")
        self.assertEqual(confirm("job-1")["action"], "keep")  # idempotent
        self.assertEqual(confirm("job-2")["action"], "delete_job")  # a second timer
        self.assertEqual(confirm("job-0", token=first["token"])["action"], "delete_job")  # stale
        # A confirmed timer: another arm creates nothing.
        existing = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        self.assertEqual((existing["armed"], existing["next"]), ("existing", None))
        # The stale first timer (if it still fires) stops without counting.
        self.assertEqual(self.pull(wake, first)["reason"], "stale_pull_token")
        # The current timer firing early is re-issued (new token), counting nothing.
        early = self.pull(wake, timer)
        self.assertEqual((early["action"], early["reason"], early["pulls"]), ("wait", "not_due", 0))
        timer = early["next"]
        self.clock[0] += first["delay_s"]
        waiting = self.pull(wake, timer)
        self.assertEqual((waiting["action"], waiting["pulls"]), ("wait", 1))
        second = waiting["next"]
        self.assertEqual(second["delay_s"], PULL_SCHEDULE_S[1])
        self.assertEqual(self.pull(wake, timer)["reason"], "stale_pull_token")  # delayed duplicate
        reply_id = self.reply(request_id)
        self.clock[0] += second["delay_s"]
        handled = self.pull(wake, second)
        self.assertEqual(handled["action"], "handle")
        self.assertEqual([r["trigger_message_id"] for r in handled["replies"]], [reply_id])
        self.assertIsNone(handled["next"])  # nothing else awaited: no extra turn
        self.assertEqual(wake.correlations.load("c1")["woken"], [])  # reserved, not woken
        wake.correlations.consume("c1", caller=AGENT_A, trigger_message_id=reply_id)
        self.assertEqual(wake.correlations.load("c1")["consumed"], [reply_id])
        with self.assertRaises(CoordError) as caught:
            wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        self.assertEqual(caught.exception.code, "no_open_correlation")
        ledger = [row for row in wake.ledger()["entries"] if row.get("binding_id") == "lead-pull"]
        self.assertEqual([(row["decision"], row["reason"]) for row in ledger], [("woken", "self_pull")])

    def test_timer_never_lands_on_minute_zero_or_thirty(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        for offset in range(0, 3600, 60):
            state = {"token": "a" * 32, "next_due_at": format_ts(self.clock[0] + offset)}
            minute = int(wake._pull_timer("lead-pull", state)["cron"].split()[0])
            self.assertNotIn(minute, (0, 30))

    def test_stop_is_checked_before_an_early_timer_is_reissued(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        armed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        wake.stop()
        early = self.pull(wake, armed["next"])  # not due yet, but STOP wins
        self.assertEqual((early["action"], early["reason"], early["next"]), ("stop", "global_stop", None))

    def test_reserved_reply_is_redelivered_even_after_the_daily_budget(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake, budget=1)
        request_id = self.request(wake)
        armed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        reply_id = self.reply(request_id)
        self.clock[0] += armed["next"]["delay_s"]
        handled = self.pull(wake, armed["next"])
        self.assertEqual(handled["action"], "handle")  # budget now used: 1/1
        # Interrupted before consume: re-arm and re-pull still return the paid reply.
        rearmed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        self.clock[0] += rearmed["next"]["delay_s"]
        again = self.pull(wake, rearmed["next"])
        self.assertEqual((again["action"], [r["trigger_message_id"] for r in again["replies"]]),
                         ("handle", [reply_id]))

    def test_unconsumed_reply_is_returned_again_after_an_interruption(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        request_id = self.request(wake)
        armed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        reply_id = self.reply(request_id)
        self.clock[0] += armed["next"]["delay_s"]
        handled = self.pull(wake, armed["next"])
        self.assertEqual([r["trigger_message_id"] for r in handled["replies"]], [reply_id])
        # The session died before consuming: a later arm finds the reserved reply.
        rearmed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        self.clock[0] += rearmed["next"]["delay_s"]
        again = self.pull(wake, rearmed["next"])
        self.assertEqual(again["action"], "handle")
        self.assertEqual([r["trigger_message_id"] for r in again["replies"]], [reply_id])
        self.assertEqual(wake.correlations.load("c1")["wakes"], 1)  # counted once

    def test_paid_reply_is_rearmable_while_another_correlation_waits(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake, budget=1)
        request_one = self.request(wake)
        self.request(wake, corr="c2")  # still awaiting after c1 is answered
        armed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        reply_id = self.reply(request_one)
        self.clock[0] += armed["next"]["delay_s"]
        handled = self.pull(wake, armed["next"])
        self.assertEqual(handled["action"], "handle")  # daily budget now 1/1
        # Interrupted before consume: re-arm must still succeed for the paid reply.
        rearmed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        self.assertIn(rearmed["armed"], ("new", "reissued"))
        self.clock[0] += rearmed["next"]["delay_s"]
        again = self.pull(wake, rearmed["next"])
        self.assertEqual((again["action"], [r["trigger_message_id"] for r in again["replies"]]),
                         ("handle", [reply_id]))

    def test_expired_correlation_reply_is_not_redelivered(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        request_id = self.request(wake)
        armed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        self.reply(request_id)
        self.clock[0] += armed["next"]["delay_s"]
        self.assertEqual(self.pull(wake, armed["next"])["action"], "handle")
        self.clock[0] += 3 * 3600 + 60  # past the correlation TTL, reply never consumed
        self.assertEqual(wake._pull_undelivered("lead-pull"), [])
        with self.assertRaises(CoordError) as caught:
            wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        self.assertEqual(caught.exception.code, "no_open_correlation")

    def test_timer_carries_a_cleanup_marker_for_older_jobs(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        timer = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)["next"]
        self.assertEqual(timer["cleanup_marker"], "wake-pull --binding-id lead-pull --token")
        self.assertIn(timer["cleanup_marker"], timer["prompt"])
        self.assertIn("CronDelete", timer["prompt"])

    def test_confirmed_delayed_timer_is_only_replaced_on_explicit_lost(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        armed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        self.assertIn("CronDelete", armed["protocol"])
        timer = armed["next"]
        wake.pull_confirm(binding_id="lead-pull", caller=AGENT_A, token=timer["token"], job_id="job-1")
        self.clock[0] += 3600  # a long active turn delayed the idle-only timer
        kept = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        self.assertEqual((kept["armed"], kept["next"], kept["confirmed_job_id"]), ("existing", None, "job-1"))
        lost = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A, lost=True)
        self.assertEqual(lost["armed"], "reissued")
        self.assertNotEqual(lost["next"]["token"], timer["token"])
        self.assertIn("CronDelete", lost["protocol"])
        # The delayed old job, if it still fires, is stale and counts nothing.
        self.assertEqual(self.pull(wake, timer)["reason"], "stale_pull_token")

    def test_default_chain_is_short_because_each_pull_is_a_full_turn(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        armed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        self.assertEqual(armed["max_pulls"], 4)
        timer, actions = armed["next"], []
        for _ in range(6):
            self.clock[0] += timer["delay_s"]
            result = self.pull(wake, timer)
            actions.append(result["action"])
            if result["next"] is None:
                break
            timer = result["next"]
        self.assertEqual(actions, ["wait", "wait", "wait", "stop"])

    def test_pull_budget_stops_the_chain(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        armed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A, max_pulls=2)
        self.clock[0] += armed["next"]["delay_s"]
        first = self.pull(wake, armed["next"])
        self.assertEqual(first["action"], "wait")
        self.clock[0] += first["next"]["delay_s"]
        second = self.pull(wake, first["next"])
        self.assertEqual((second["action"], second["reason"]), ("stop", "pull_budget_exhausted"))

    def test_global_stop_ends_the_chain_even_with_an_empty_inbox(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        armed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        wake.stop()
        self.clock[0] += armed["next"]["delay_s"]
        result = self.pull(wake, armed["next"])
        self.assertEqual((result["action"], result["reason"], result["next"]), ("stop", "global_stop", None))
        with self.assertRaises(CoordError):
            wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)

    def test_daily_budget_stops_the_chain_even_with_an_empty_inbox(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake, budget=1)
        request_id = self.request(wake)
        self.request(wake, corr="c2")
        armed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        reply_id = self.reply(request_id)
        self.clock[0] += armed["next"]["delay_s"]
        handled = self.pull(wake, armed["next"])
        self.assertEqual(handled["action"], "handle")
        wake.correlations.consume("c1", caller=AGENT_A, trigger_message_id=reply_id)
        self.clock[0] += handled["next"]["delay_s"]
        stopped = self.pull(wake, handled["next"])
        self.assertEqual((stopped["action"], stopped["reason"]), ("stop", "daily_budget_exhausted"))

    def test_only_the_bound_session_may_arm_pull_or_disarm(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        for call in (lambda: wake.pull_arm(binding_id="lead-pull", caller=AGENT_C),
                     lambda: wake.pull(binding_id="lead-pull", caller=AGENT_C, token="0" * 32),
                     lambda: wake.pull_disarm(binding_id="lead-pull", caller=AGENT_C)):
            with self.assertRaises(CoordError) as caught:
                call()
            self.assertEqual(caught.exception.code, "wake_pull_forbidden")

    def test_invalid_state_fails_closed_until_disarm(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        armed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        wake._pull_path("lead-pull").write_text('{"status": "armed", "token": 5}', encoding="utf-8")
        for call in (lambda: wake.pull_arm(binding_id="lead-pull", caller=AGENT_A),
                     lambda: self.pull(wake, armed["next"])):
            with self.assertRaises(CoordError) as caught:
                call()
            self.assertEqual(caught.exception.code, "wake_pull_state_invalid")
        self.assertTrue(wake.pull_disarm(binding_id="lead-pull", caller=AGENT_A)["changed"])
        self.assertEqual(wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)["armed"], "new")

    def test_disarm_stops_future_timers(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        self.request(wake)
        armed = wake.pull_arm(binding_id="lead-pull", caller=AGENT_A)
        self.assertTrue(wake.pull_disarm(binding_id="lead-pull", caller=AGENT_A)["changed"])
        self.clock[0] += armed["next"]["delay_s"]
        result = self.pull(wake, armed["next"])
        self.assertEqual((result["action"], result["reason"]), ("stop", "stale_pull_token"))


if __name__ == "__main__":
    unittest.main()
