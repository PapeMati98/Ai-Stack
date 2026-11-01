"""Office Wake v2: task-scoped correlations for automatic continuation."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from support import PACKAGE_ROOT  # noqa: F401  (puts the package on sys.path)

from coordfs.errors import CoordError
from coordfs.leases import format_ts
from coordfs.wake import Wake
import test_wake as _p1
from test_wake import AGENT_A, AGENT_B, AGENT_C

LOCAL_ID = "local_123e4567-e89b-12d3-a456-426614174000"


class WakeCorrelationTests(_p1.WakeTests):
    def enroll_v2(self, wake: Wake, *, binding_id: str = "lead-a", driver: str = "codex-cli",
                  allowed: list[str] | None = None, budget: int = 20) -> None:
        wake.enroll(
            binding_id=binding_id,
            office_agent=AGENT_A,
            driver=driver,
            native_thread_id=LOCAL_ID if driver == "claude-desktop-relay" else f"thread-{binding_id}",
            workspace=str(self.workspace),
            enrolled_by="owner",
            allowed_wakers=allowed or [AGENT_B, AGENT_C],
            allowed_triggers=["message"],
            wake_kinds=["question", "handoff"],
            budget_per_day=budget,
            max_attempts_per_trigger=1,
            expires_at=format_ts(self.clock[0] + 86400),
            owner_confirm=True,
            correlation_required=True,
        )
        wake.verify(binding_id, owner_confirm=True)

    def request(self, wake: Wake, *, task: str = "t1", corr: str = "c1", max_wakes: int = 6,
                counterpart: str = AGENT_B, binding_id: str = "lead-a") -> str:
        sent = self.office(AGENT_A).send(counterpart, "zlecenie wezla", kind="question")
        self.assertEqual(sent["status"], "ok")
        wake.expect(binding_id=binding_id, caller=AGENT_A, task_id=task, correlation_id=corr,
                    counterpart=counterpart, request_message_id=sent["id"], max_wakes=max_wakes)
        return sent["id"]

    def reply(self, request_id: str, *, sender: str = AGENT_B, body: str = "wynik wezla") -> str:
        sent = self.office(sender).send(AGENT_A, body, kind="note", reply_to=request_id)
        self.assertEqual(sent["status"], "ok")
        return sent["id"]

    def test_v2_reply_to_open_request_wakes_with_correlation_prompt_and_nothing_else_does(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        request_id = self.request(wake)
        reply_id = self.reply(request_id)
        first = wake.tick()
        self.assertEqual(first["counts"]["woken"], 1)
        # v2.1: the only request is answered, so nothing awaits a fast tick any more.
        self.assertIsNone(first["recommended_next_tick_s"])
        prompt = self.driver_calls()[-1][-1]
        self.assertIn("korelacja c1", prompt)
        self.assertIn(f"--trigger-message-id {reply_id}", prompt)
        # An ordinary message, even a question from an allowed waker, does not wake.
        self.office(AGENT_B).send(AGENT_A, "luzna uwaga", kind="question")
        second = wake.tick()
        self.assertIn("no_open_correlation", self.reasons(second))
        # The same reply is never processed twice (duplicate tick).
        self.assertEqual(wake.tick()["counts"]["woken"], 0)
        self.assertEqual(len(self.driver_calls()), 1)

    def test_v2_foreign_sender_old_epoch_and_limit_do_not_wake(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        request_id = self.request(wake, max_wakes=1)
        self.reply(request_id, sender=AGENT_C)  # allowed waker but not the counterpart
        self.assertIn("no_open_correlation", self.reasons(wake.tick()))
        self.reply(request_id)
        self.assertEqual(wake.tick()["counts"]["woken"], 1)
        self.reply(request_id, body="druga odpowiedz")
        limited = wake.tick()
        self.assertIn("correlation_limit_reached", self.reasons(limited))
        self.assertEqual(wake.correlations.load("c1")["status"], "limit_reached")
        # Epoch bump invalidates a fresh expectation of the same task.
        request_two = self.request(wake, corr="c2")
        wake.correlations.cancel_task("t1", caller=AGENT_A)
        self.reply(request_two)
        self.assertIn("correlation_cancelled", self.reasons(wake.tick()))
        self.assertEqual(len(self.driver_calls()), 1)

    def test_v2_busy_cli_session_defers_then_replays_once_after_restart(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        request_id = self.request(wake)
        self.office(AGENT_A).presence(status="busy")
        reply_id = self.reply(request_id)
        deferred = wake.tick()
        self.assertEqual([d["decision"] for d in deferred["decisions"]], ["deferred"])
        self.assertEqual(self.driver_calls(), [])
        self.assertEqual(wake.tick()["decisions"], [])  # still busy: no ledger row per tick
        self.clock[0] += 11 * 60  # busy lease is stale now
        restarted = self.wake()  # new process state, same files
        replay = restarted.tick()
        self.assertEqual(replay["counts"]["woken"], 1)
        self.assertEqual(restarted.correlations.load("c1")["woken"], [reply_id])
        self.assertEqual(restarted.tick()["counts"]["woken"], 0)
        self.assertEqual(len(self.driver_calls()), 1)

    def test_v2_busy_desktop_relay_queues_behind_the_turn_without_process(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake, driver="claude-desktop-relay")
        request_id = self.request(wake)
        self.office(AGENT_A).presence(status="busy")
        self.reply(request_id)
        with mock.patch("coordfs.wake.subprocess.run") as process:
            result = wake.tick()
        process.assert_not_called()
        self.assertEqual(result["counts"].get("dispatched"), 1)
        states = [row["state"] for row in wake.correlations.receipts("c1")]
        self.assertEqual(states, ["expected", "reserved", "dispatched"])

    def test_v2_consume_is_requester_only_and_ack_does_not_imply_it(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        request_id = self.request(wake)
        reply_id = self.reply(request_id)
        wake.tick()
        self.office(AGENT_A).ack(reply_id)
        self.assertEqual(wake.correlations.load("c1")["consumed"], [])
        with self.assertRaises(CoordError):
            wake.correlations.consume("c1", caller=AGENT_B, trigger_message_id=reply_id)
        with self.assertRaises(CoordError):
            wake.correlations.consume("c1", caller=AGENT_A, trigger_message_id=request_id)
        wake.correlations.consume("c1", caller=AGENT_A, trigger_message_id=reply_id)
        self.assertEqual(wake.correlations.load("c1")["consumed"], [reply_id])
        wake.correlations.close("c1", caller=AGENT_A)
        self.assertIsNone(wake.tick()["recommended_next_tick_s"])

    def test_v2_expect_is_bound_session_only_and_needs_v2_binding(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        sent = self.office(AGENT_A).send(AGENT_B, "x", kind="question")
        with self.assertRaises(CoordError):
            wake.expect(binding_id="lead-a", caller=AGENT_B, task_id="t1", correlation_id="c9",
                        counterpart=AGENT_A, request_message_id=sent["id"])
        self.enroll(wake, "legacy-b", AGENT_B, [AGENT_A])
        with self.assertRaises(CoordError):
            wake.expect(binding_id="legacy-b", caller=AGENT_B, task_id="t1", correlation_id="c8",
                        counterpart=AGENT_A, request_message_id=sent["id"])
        # A correlation id cannot be re-bound to another counterpart.
        self.request(wake, corr="c7")
        with self.assertRaises(CoordError):
            self.request(wake, corr="c7", counterpart=AGENT_C)

    def test_v2_cli_commands_round_trip(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        sent = self.office(AGENT_A).send(AGENT_B, "zlecenie", kind="question")
        payload = self.run_cli(["--agent", AGENT_A, "wake-expect", "--binding-id", "lead-a",
                                "--task-id", "t1", "--correlation-id", "c1", "--counterpart", AGENT_B,
                                "--request-message-id", sent["id"]])
        self.assertEqual(payload["code"], "wake_expected")
        listed = self.run_cli(["--agent", AGENT_A, "wake-correlations"])
        self.assertEqual(listed["open"], 1)
        cancelled = self.run_cli(["--agent", AGENT_A, "wake-task-cancel", "--task-id", "t1"])
        self.assertEqual(cancelled["cancelled_correlations"], ["c1"])

    # --- regressions for the Luna review of 660a1b8 ---------------------------------
    def test_v2_expect_requires_a_real_request_from_caller_to_counterpart(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        with self.assertRaises(CoordError) as missing:
            wake.expect(binding_id="lead-a", caller=AGENT_A, task_id="t1", correlation_id="c1",
                        counterpart=AGENT_B, request_message_id="f" * 32)
        self.assertEqual(missing.exception.code, "wake_request_not_found")
        to_c = self.office(AGENT_A).send(AGENT_C, "inny adresat", kind="question")
        with self.assertRaises(CoordError):
            wake.expect(binding_id="lead-a", caller=AGENT_A, task_id="t1", correlation_id="c1",
                        counterpart=AGENT_B, request_message_id=to_c["id"])
        self.reply("f" * 32)
        self.assertEqual(self.driver_calls(), [])

    def test_v2_cancellation_that_wins_the_lock_prevents_the_wake(self) -> None:
        from coordfs.wake_correlation import Correlations
        wake = self.wake()
        self.enroll_v2(wake)
        self.reply(self.request(wake))
        original = Correlations.reserve

        def cancel_first(corr, *args, **kwargs):
            corr.cancel_task("t1", caller=AGENT_A)
            return original(corr, *args, **kwargs)

        with mock.patch.object(Correlations, "reserve", cancel_first):
            result = wake.tick()
        self.assertEqual(result["counts"]["woken"], 0)
        self.assertEqual(self.driver_calls(), [])
        self.assertEqual(wake.correlations.load("c1")["woken"], [])

    def test_v2_interrupted_relay_wake_reuses_its_dispatch_after_restart(self) -> None:
        from coordfs.wake_correlation import Correlations
        wake = self.wake()
        self.enroll_v2(wake, driver="claude-desktop-relay")
        self.reply(self.request(wake))
        dispatch_dir = self.home / "office" / "wake" / "desktop-dispatch"
        with mock.patch.object(Correlations, "record_wake", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                wake.tick()
        self.assertEqual(len(list(dispatch_dir.glob("*.json"))), 1)
        restarted = self.wake()
        restarted.tick()
        restarted.tick()
        self.assertEqual(len(list(dispatch_dir.glob("*.json"))), 1)

    def test_v2_interrupted_cli_wake_is_never_rerun(self) -> None:
        from coordfs.wake_correlation import Correlations
        wake = self.wake()
        self.enroll_v2(wake)
        self.reply(self.request(wake))
        with mock.patch.object(Correlations, "record_wake", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                wake.tick()
        self.assertEqual(len(self.driver_calls()), 1)
        # The binding cursor never advanced past the trigger, so the same reply is seen again.
        again = self.wake().tick()
        self.assertIn("delivery_unknown_reserved", self.reasons(again))
        self.assertEqual(len(self.driver_calls()), 1)

    def test_v2_idempotent_retry_persists_a_lower_limit(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        sent = self.office(AGENT_A).send(AGENT_B, "zlecenie", kind="question")
        for limit in (10, 1):
            wake.expect(binding_id="lead-a", caller=AGENT_A, task_id="t1", correlation_id="c1",
                        counterpart=AGENT_B, request_message_id=sent["id"], max_wakes=limit)
        self.assertEqual(self.wake().correlations.load("c1")["max_wakes"], 1)

    def test_v2_only_the_task_owner_may_cancel(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        self.request(wake)
        with self.assertRaises(CoordError):
            wake.correlations.cancel_task("t1", caller=AGENT_C)
        self.assertEqual(wake.correlations.load("c1")["status"], "open")
        wake.correlations.cancel_task("t1", caller=AGENT_A)
        self.assertEqual(wake.correlations.load("c1")["status"], "cancelled")

    # --- regressions for the Luna recheck of 49c5b2d (interleavings) --------------
    def test_v2_cancel_never_touches_a_correlation_of_the_new_epoch(self) -> None:
        import threading
        from coordfs.wake_correlation import Correlations
        wake = self.wake()
        self.enroll_v2(wake)
        self.request(wake, corr="old")
        new_request = self.office(AGENT_A).send(AGENT_B, "nowe zlecenie", kind="question")
        opened: dict = {}
        original = Correlations._set_terminal

        def open_new_during_scan(corr, *args, **kwargs):
            if "thread" not in opened:
                def register():
                    opened["result"] = self.wake().expect(
                        binding_id="lead-a", caller=AGENT_A, task_id="t1", correlation_id="new",
                        counterpart=AGENT_B, request_message_id=new_request["id"])
                opened["thread"] = threading.Thread(target=register)
                opened["thread"].start()
                opened["thread"].join(0.3)  # must be blocked on the task lock
                opened["blocked_during_scan"] = opened["thread"].is_alive()
            return original(corr, *args, **kwargs)

        with mock.patch.object(Correlations, "_set_terminal", open_new_during_scan):
            cancelled = wake.correlations.cancel_task("t1", caller=AGENT_A)
        opened["thread"].join(10)
        self.assertTrue(opened["blocked_during_scan"])
        self.assertEqual(cancelled["cancelled_correlations"], ["old"])
        new = wake.correlations.load("new")
        self.assertEqual((new["status"], new["epoch"]), ("open", 2))

    def test_v2_reservation_and_cancellation_are_linearized(self) -> None:
        import threading
        from coordfs.wake_correlation import Correlations
        wake = self.wake()
        self.enroll_v2(wake)
        self.reply(self.request(wake))
        state: dict = {}
        original_epoch = Correlations.task_epoch

        def cancel_while_reserving(corr, task_id):
            value = original_epoch(corr, task_id)
            if state.get("reserving") and "thread" not in state:
                def cancel():
                    state["cancel"] = self.wake().correlations.cancel_task("t1", caller=AGENT_A)
                state["thread"] = threading.Thread(target=cancel)
                state["thread"].start()
                state["thread"].join(0.3)
                state["cancel_blocked"] = state["thread"].is_alive()
            return value

        original_reserve = Correlations.reserve

        def reserve(corr, *args, **kwargs):
            state["reserving"] = True
            try:
                return original_reserve(corr, *args, **kwargs)
            finally:
                state["reserving"] = False

        with mock.patch.object(Correlations, "task_epoch", cancel_while_reserving),                 mock.patch.object(Correlations, "reserve", reserve):
            result = wake.tick()
        state["thread"].join(10)
        self.assertTrue(state["cancel_blocked"])
        # The reservation won the lock: exactly one wake, and the cancellation followed it.
        self.assertEqual(result["counts"]["woken"], 1)
        final = wake.correlations.load("c1")
        self.assertEqual(final["status"], "cancelled")
        # Nothing further wakes after the cancellation.
        self.reply(final["requests"][0], body="po anulowaniu")
        self.assertEqual(wake.tick()["counts"]["woken"], 0)
        self.assertEqual(len(self.driver_calls()), 1)

    # --- Wake v2.1: regressions for the multi-lens review of b55f94b ------------------
    def enroll_agent_v2(self, wake: Wake, binding_id: str, agent: str, allowed: list[str]) -> None:
        wake.enroll(
            binding_id=binding_id, office_agent=agent, driver="codex-cli",
            native_thread_id=f"thread-{binding_id}", workspace=str(self.workspace),
            enrolled_by="owner", allowed_wakers=allowed, allowed_triggers=["message"],
            wake_kinds=["question", "handoff"], budget_per_day=20, max_attempts_per_trigger=1,
            expires_at=format_ts(self.clock[0] + 86400), owner_confirm=True, correlation_required=True,
        )
        wake.verify(binding_id, owner_confirm=True)

    def ledger_rows(self, wake: Wake, trigger_ref: str) -> list[dict]:
        return [row for row in wake.ledger(limit=1000)["entries"] if row.get("trigger_ref") == trigger_ref]

    def test_v21_counterpart_is_normalized_to_a_canonical_office_address(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        sent = self.office(AGENT_A).send(AGENT_B, "zlecenie", kind="question")
        result = wake.expect(binding_id="lead-a", caller=AGENT_A, task_id="t1", correlation_id="c1",
                             counterpart="Agent:CLAUDE-HOST-B", request_message_id=sent["id"])
        self.assertEqual(result["correlation"]["counterpart"], AGENT_B)
        self.reply(sent["id"])
        self.assertEqual(wake.tick()["counts"]["woken"], 1)
        # An alias of the caller itself is not "another Office address".
        with self.assertRaises(CoordError) as alias:
            wake.correlations.expect(binding=wake._load_binding("lead-a"), caller=AGENT_A, task_id="t1",
                                     correlation_id="c2", counterpart="AGENT:codex-host-a",
                                     request_message_id=sent["id"])
        self.assertEqual(alias.exception.code, "invalid_wake_correlation")

    def test_v21_match_prefers_an_open_correlation_over_a_terminal_one(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        request_id = self.request(wake, corr="a1")
        # Legacy state written before request ids were unique across correlations.
        folder = self.home / "office" / "wake" / "correlations"
        old = json.loads((folder / "a1.json").read_text(encoding="utf-8"))
        (folder / "b2.json").write_text(json.dumps({**old, "correlation_id": "b2"}), encoding="utf-8")
        (folder / "a1.json").write_text(json.dumps({**old, "status": "expired"}), encoding="utf-8")
        self.reply(request_id)
        self.assertEqual(wake.tick()["counts"]["woken"], 1)
        self.assertEqual(len(wake.correlations.load("b2")["woken"]), 1)

    def test_v21_repeated_expect_of_an_open_request_extends_the_ttl(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        sent = self.office(AGENT_A).send(AGENT_B, "dlugie zadanie", kind="question")
        fields = dict(binding_id="lead-a", caller=AGENT_A, task_id="t1", correlation_id="c1",
                      counterpart=AGENT_B, request_message_id=sent["id"], ttl_s=600)
        wake.expect(**fields)
        self.clock[0] += 500
        self.assertEqual(wake.expect(**fields)["code"], "wake_expect_exists")
        self.clock[0] += 200
        self.reply(sent["id"])
        self.assertEqual(wake.tick()["counts"]["woken"], 1)

    def test_v21_task_ids_do_not_cross_the_requester_boundary(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        self.enroll_agent_v2(wake, "lead-c", AGENT_C, [AGENT_B])
        self.request(wake, task="t1", corr="c-a")
        sent = self.office(AGENT_C).send(AGENT_B, "zlecenie C", kind="question")
        with self.assertRaises(CoordError) as foreign:
            wake.expect(binding_id="lead-c", caller=AGENT_C, task_id="t1", correlation_id="c-c",
                        counterpart=AGENT_B, request_message_id=sent["id"])
        self.assertEqual(foreign.exception.code, "wake_task_foreign")
        # A legacy correlation of another requester in the same task is never cancelled.
        wake.expect(binding_id="lead-c", caller=AGENT_C, task_id="t9", correlation_id="c-c",
                    counterpart=AGENT_B, request_message_id=sent["id"])
        path = self.home / "office" / "wake" / "correlations" / "c-c.json"
        legacy = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**legacy, "task_id": "t1"}), encoding="utf-8")
        cancelled = wake.correlations.cancel_task("t1", caller=AGENT_A)
        self.assertEqual(cancelled["cancelled_correlations"], ["c-a"])
        self.assertEqual(wake.correlations.load("c-c")["status"], "open")

    def test_v21_a_request_id_belongs_to_one_correlation_and_one_epoch(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        request_id = self.request(wake, corr="c1", max_wakes=1)
        self.reply(request_id)
        self.assertEqual(wake.tick()["counts"]["woken"], 1)
        self.reply(request_id, body="druga")
        wake.tick()
        self.assertEqual(wake.correlations.load("c1")["status"], "limit_reached")
        with self.assertRaises(CoordError) as reused:
            wake.expect(binding_id="lead-a", caller=AGENT_A, task_id="t1", correlation_id="a0",
                        counterpart=AGENT_B, request_message_id=request_id, max_wakes=50)
        self.assertEqual(reused.exception.code, "wake_request_already_correlated")
        # A request sent before a cancellation cannot join the new epoch.
        old = self.office(AGENT_A).send(AGENT_B, "stare zlecenie", kind="question")
        self.clock[0] += 5
        wake.correlations.cancel_task("t1", caller=AGENT_A)
        with self.assertRaises(CoordError) as stale:
            wake.expect(binding_id="lead-a", caller=AGENT_A, task_id="t1", correlation_id="c3",
                        counterpart=AGENT_B, request_message_id=old["id"])
        self.assertEqual(stale.exception.code, "wake_correlation_stale")
        self.assertEqual(len(self.driver_calls()), 1)

    def test_v21_cli_woken_turn_can_consume_its_reply(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        reply_id = self.reply(self.request(wake))
        outcome: dict = {}

        def turn(_self, binding, metadata, correlation=None):
            # The resumed session runs `office wake-consume` inside the same turn.
            outcome["consume"] = self.wake().correlations.consume(
                correlation["correlation_id"], caller=AGENT_A, trigger_message_id=metadata["id"])
            return {"status": "accepted", "exit_code": 0, "duration_ms": 1}

        with mock.patch.object(Wake, "_run_driver", turn):
            self.assertEqual(wake.tick()["counts"]["woken"], 1)
        self.assertEqual(outcome["consume"]["code"], "wake_reply_consumed")
        final = wake.correlations.load("c1")
        self.assertEqual((final["woken"], final["consumed"]), ([reply_id], [reply_id]))

    def test_v21_interrupted_cli_wake_blocks_the_binding_until_reconciled(self) -> None:
        from coordfs.wake_correlation import Correlations
        wake = self.wake()
        self.enroll_v2(wake)
        request_id = self.request(wake)
        self.reply(request_id)
        with mock.patch.object(Correlations, "record_wake", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                wake.tick()
        self.assertIn("delivery_unknown_reserved", self.reasons(self.wake().tick()))
        self.reply(request_id, body="druga odpowiedz")
        self.assertEqual(self.wake().tick()["counts"]["woken"], 0)
        self.assertEqual(len(self.driver_calls()), 1)

    def test_v21_interrupted_cli_wake_stays_unknown_after_its_task_is_cancelled(self) -> None:
        from coordfs.wake_correlation import Correlations
        wake = self.wake()
        self.enroll_v2(wake)
        self.reply(self.request(wake))
        with mock.patch.object(Correlations, "record_wake", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                wake.tick()
        wake.correlations.cancel_task("t1", caller=AGENT_A)
        self.assertIn("delivery_unknown_reserved", self.reasons(self.wake().tick()))
        self.reply(self.request(wake, corr="c2"))
        self.assertEqual(self.wake().tick()["counts"]["woken"], 0)
        self.assertEqual(len(self.driver_calls()), 1)

    def test_v21_deferred_reply_interrupted_after_reserve_is_not_lost(self) -> None:
        from coordfs.wake_correlation import Correlations
        wake = self.wake()
        self.enroll_v2(wake)
        self.office(AGENT_A).presence(status="busy")
        reply_id = self.reply(self.request(wake))
        self.assertEqual(self.reasons(wake.tick()), ["recipient_busy"])
        self.clock[0] += 11 * 60
        with mock.patch.object(Correlations, "record_wake", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.wake().tick()
        self.assertEqual(len(self.driver_calls()), 1)
        again = self.wake().tick()
        self.assertIn("delivery_unknown_reserved", self.reasons(again))
        self.assertEqual(len(self.driver_calls()), 1)
        # The turn may have run: the requester can still record consumption.
        self.wake().correlations.consume("c1", caller=AGENT_A, trigger_message_id=reply_id)
        self.assertEqual(self.wake().correlations.load("c1")["pending"], [])

    def test_v21_v2_binding_cannot_coexist_with_a_v1_binding_of_the_same_session(self) -> None:
        wake = self.wake()
        self.enroll(wake, "legacy-a", AGENT_A, [AGENT_B])
        with self.assertRaises(CoordError) as conflict:
            self.enroll_v2(wake)
        self.assertEqual(conflict.exception.code, "wake_binding_conflict")
        other = self.wake()
        self.enroll_agent_v2(other, "lead-c", AGENT_C, [AGENT_B])
        with self.assertRaises(CoordError):
            self.enroll(other, "legacy-c", AGENT_C, [AGENT_B])

    def test_v21_existing_v1_binding_is_gated_while_a_v2_binding_of_the_session_is_active(self) -> None:
        wake = self.wake()
        self.enroll(wake, "legacy-a", AGENT_A, [AGENT_B], expires_in=86400)
        with mock.patch.object(Wake, "_binding_conflict", return_value=None):
            self.enroll_v2(wake)  # state written before v2.1 refused the pair
        self.reply(self.request(wake))
        self.office(AGENT_B).send(AGENT_A, "luzne pytanie", kind="question")
        result = wake.tick()
        self.assertIn("superseded_by_correlation_binding", self.reasons(result))
        self.assertEqual(len(self.driver_calls()), 1)

    def test_v21_tick_hint_only_while_a_reply_is_awaited(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        request_id = self.request(wake)
        self.assertEqual(wake.tick()["recommended_next_tick_s"], 60)
        reply_id = self.reply(request_id)
        self.assertIsNone(wake.tick()["recommended_next_tick_s"])  # answered
        wake.correlations.consume("c1", caller=AGENT_A, trigger_message_id=reply_id)
        self.assertIsNone(wake.tick()["recommended_next_tick_s"])
        self.request(wake, corr="c2")
        self.assertEqual(wake.tick()["recommended_next_tick_s"], 60)
        wake.revoke("lead-a")
        self.assertIsNone(wake.tick()["recommended_next_tick_s"])

    def test_relay_tick_checks_unconsumed_delivery_without_resending(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake, driver="claude-desktop-relay")
        reply_id = self.reply(self.request(wake))

        first = wake.tick()
        self.assertEqual(first["counts"]["dispatched"], 1)
        self.assertEqual(first["recommended_next_tick_s"], 300)
        second = wake.tick()
        self.assertEqual(second["counts"].get("dispatched", 0), 0)
        self.assertEqual(second["recommended_next_tick_s"], 300)

        dispatch_id = wake.correlations.load("c1")["reserved"][reply_id]
        wake.relay_claim(dispatch_id, relay_agent=AGENT_C)
        wake.relay_receipt(dispatch_id, status="queued", message_id="native-message-1")
        wake.relay_observe(dispatch_id, stage="target_started")
        self.assertEqual(wake.tick()["recommended_next_tick_s"], 300)
        wake.relay_observe(dispatch_id, stage="target_finished")
        self.assertIsNone(wake.tick()["recommended_next_tick_s"])

        wake.correlations.consume("c1", caller=AGENT_A, trigger_message_id=reply_id)
        self.assertIsNone(wake.tick()["recommended_next_tick_s"])

    def test_v21_dry_run_does_not_change_correlation_state(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        request_id = self.request(wake, max_wakes=1)
        self.reply(request_id)
        wake.tick()
        self.reply(request_id, body="druga")
        before = wake.correlations.receipts("c1")
        dry = wake.tick(dry_run=True)
        self.assertIn("correlation_limit_reached", self.reasons(dry))
        self.assertEqual(wake.correlations.load("c1")["status"], "open")
        self.assertEqual(wake.correlations.receipts("c1"), before)

    def test_v21_transient_block_keeps_a_queued_reply_without_ledger_rows(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        self.office(AGENT_A).presence(status="busy")
        reply_id = self.reply(self.request(wake))
        wake.tick()  # deferred behind the running turn
        self.clock[0] += 11 * 60
        wake.stop()
        for _ in range(3):
            wake.tick()
        self.assertEqual([row["decision"] for row in self.ledger_rows(wake, reply_id)], ["deferred"])
        wake.start()
        self.assertEqual(wake.tick()["counts"]["woken"], 1)
        self.assertEqual(len(self.driver_calls()), 1)

    def test_v21_replay_blocked_in_recheck_does_not_write_a_row_per_tick(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        self.office(AGENT_A).presence(status="busy")
        reply_id = self.reply(self.request(wake))
        wake.tick()
        self.clock[0] += 11 * 60
        with mock.patch.object(Wake, "_has_unreconciled_unknown", return_value=True):
            for _ in range(4):
                wake.tick()
        self.assertEqual(len(self.ledger_rows(wake, reply_id)), 1)
        self.assertEqual(wake.tick()["counts"]["woken"], 1)

    def test_v21_reply_blocked_by_daily_budget_is_replayed_next_day(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake, budget=1)
        sent = [self.office(AGENT_A).send(AGENT_B, f"zlecenie {n}", kind="question") for n in (1, 2)]
        for index, item in enumerate(sent):
            wake.expect(binding_id="lead-a", caller=AGENT_A, task_id="t1", correlation_id=f"c{index}",
                        counterpart=AGENT_B, request_message_id=item["id"], ttl_s=24 * 3600)
        self.reply(sent[0]["id"])
        self.reply(sent[1]["id"])
        first = wake.tick()
        self.assertEqual(first["counts"]["woken"], 1)
        self.assertEqual(first["counts"].get("deferred"), 1)
        self.clock[0] += 17 * 3600  # next UTC day
        self.assertEqual(wake.tick()["counts"]["woken"], 1)
        self.assertEqual(len(self.driver_calls()), 2)

    def test_v21_recheck_logs_deferred_only_when_actually_queued(self) -> None:
        from coordfs.wake_correlation import Correlations
        wake = self.wake()
        self.enroll_v2(wake)
        reply_id = self.reply(self.request(wake))
        with mock.patch.object(Wake, "_recipient_busy_reason", side_effect=[None, "recipient_busy"]), \
                mock.patch.object(Correlations, "defer", return_value=False):
            result = wake.tick()
        self.assertEqual(self.reasons(result), ["deferred_not_queued"])
        self.assertEqual(self.ledger_rows(wake, reply_id)[-1]["decision"], "skipped")

    def test_v21_transient_correlation_read_error_does_not_drop_the_reply(self) -> None:
        from coordfs.wake_correlation import Correlations
        wake = self.wake()
        self.enroll_v2(wake)
        self.reply(self.request(wake))
        original = Correlations._read
        calls = {"n": 0}

        def flaky(corr, path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise CoordError("sharing violation", code="wake_state_busy")
            return original(corr, path)

        with mock.patch.object(Correlations, "_read", flaky):
            first = wake.tick()
        self.assertEqual(first["counts"]["woken"], 0)
        self.assertIn("wake_state_busy", self.reasons(first))
        self.assertEqual(wake.tick()["counts"]["woken"], 1)

    def test_v21_broken_v2_state_does_not_stop_other_bindings(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        self.enroll(wake, "other-b", AGENT_B, [AGENT_C])
        self.reply(self.request(wake))
        (self.home / "office" / "wake" / "tasks" / "t1.json").write_text("{broken", encoding="utf-8")
        self.office(AGENT_C).send(AGENT_B, "pytanie do B", kind="question")
        result = wake.tick()
        self.assertEqual(result["counts"]["woken"], 1)
        self.assertIn("invalid_wake_correlation", self.reasons(result))

    def test_v21_cancel_revokes_an_unclaimed_relay_dispatch(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake, driver="claude-desktop-relay")
        self.reply(self.request(wake))
        self.assertEqual(wake.tick()["counts"]["dispatched"], 1)
        wake.correlations.cancel_task("t1", caller=AGENT_A)
        self.assertEqual(wake.relay_pending()["count"], 0)

    def test_v21_identifiers_cannot_collide_with_lock_names(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        sent = self.office(AGENT_A).send(AGENT_B, "zlecenie", kind="question")
        with self.assertRaises(CoordError) as colon:
            wake.expect(binding_id="lead-a", caller=AGENT_A, task_id="todo:12", correlation_id="c1",
                        counterpart=AGENT_B, request_message_id=sent["id"])
        self.assertEqual(colon.exception.code, "invalid_wake_correlation")
        for task, corr in (("t1", "receipts"), ("t2", "task-t2")):
            item = self.office(AGENT_A).send(AGENT_B, f"zlecenie {corr}", kind="question")
            wake.expect(binding_id="lead-a", caller=AGENT_A, task_id=task, correlation_id=corr,
                        counterpart=AGENT_B, request_message_id=item["id"])
            self.assertEqual(wake.correlations.close(corr, caller=AGENT_A)["correlation"]["status"], "closed")

    def run_cli(self, argv: list[str]) -> dict:
        import io
        from contextlib import redirect_stdout
        from coordfs.office_cli import main as office_main
        out = io.StringIO()
        with redirect_stdout(out):
            office_main(["--home", str(self.home), *argv])
        return json.loads(out.getvalue())


# Reuse the P1 fixture only; the inherited P1 tests run once, in test_wake.
for _name in dir(_p1.WakeTests):
    if _name.startswith("test_") and not _name.startswith("test_v2_"):
        setattr(WakeCorrelationTests, _name, None)
