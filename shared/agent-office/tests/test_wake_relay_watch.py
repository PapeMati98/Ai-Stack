"""Event-driven relay: a scoped tick and a watcher that waits for one binding without model turns.

Live UAT 2026-09-27: a Claude Desktop session in Auto mode (which refuses a self-scheduled
CronCreate) was woken after its turn ended by a relay whose background watcher ran only the
scoped tick of that binding; the relay session was resumed only when a dispatch existed.
"""

from __future__ import annotations

import hashlib
import io
import json
import unittest
from unittest import mock
from contextlib import redirect_stdout

from support import PACKAGE_ROOT  # noqa: F401  (puts the package on sys.path)

from coordfs.errors import CoordError
from coordfs.leases import format_ts
from coordfs.office import Office
from coordfs.office_cli import main as office_main
from coordfs.office_mcp import OfficeMcpServer
from coordfs.wake import (
    CORRELATED_WAKE_PROMPT_TEMPLATE,
    RELAY_BUSY_DEFER_SCHEDULE_S,
    WAKE_PROMPT_TEMPLATE,
    Wake,
)
import test_wake_relay as _relay
from test_wake_relay import SENDER, TARGET

OTHER = "claude-desktop-other"
OTHER_LOCAL = "local_223e4567-e89b-12d3-a456-426614174000"


class RelayWatchTests(unittest.TestCase):
    setUp = _relay.WakeRelayTests.setUp
    wake = _relay.WakeRelayTests.wake
    office = _relay.WakeRelayTests.office
    enroll = _relay.WakeRelayTests.enroll
    add_due_schedule = _relay.WakeRelayTests.add_due_schedule
    dispatch_files = _relay.WakeRelayTests.dispatch_files
    create_schedule_dispatch = _relay.WakeRelayTests.create_schedule_dispatch

    def enroll_other(self, wake: Wake, *, triggers: list[str]) -> None:
        wake.enroll(
            binding_id="other-binding", office_agent=OTHER, driver="claude-desktop-relay",
            native_thread_id=OTHER_LOCAL, workspace=str(self.workspace), enrolled_by="owner",
            allowed_wakers=[SENDER], allowed_triggers=triggers, wake_kinds=["question", "handoff"],
            budget_per_day=6, expires_at=format_ts(self.clock[0] + 30 * 86400), owner_confirm=True,
        )
        wake.verify("other-binding", owner_confirm=True)
        Office(agent_id=OTHER, home=self.home)._remember_canonical_agent()

    def test_scoped_tick_touches_only_its_binding_and_no_schedule(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.enroll_other(wake, triggers=["message", "schedule"])
        self.add_due_schedule(wake, binding_id="other-binding")
        self.office(SENDER).send(OTHER, "do innej sesji", kind="question")
        scoped = wake.tick(binding_id="desktop-binding")
        self.assertEqual(scoped["decisions"], [])
        self.assertEqual(self.dispatch_files(), [])
        self.office(SENDER).send(TARGET, "do celu", kind="question")
        scoped = wake.tick(binding_id="desktop-binding")
        self.assertEqual([d["binding_id"] for d in scoped["decisions"]], ["desktop-binding"])
        self.assertEqual(len(self.dispatch_files()), 1)
        with self.assertRaises(CoordError) as caught:
            wake.tick(binding_id="missing-binding")
        self.assertEqual(caught.exception.code, "wake_binding_not_found")

    def test_scoped_pending_lists_only_its_binding(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.enroll_other(wake, triggers=["message"])
        self.office(SENDER).send(TARGET, "do celu", kind="question")
        self.office(SENDER).send(OTHER, "do innej", kind="question")
        wake.tick()
        self.assertEqual(wake.relay_pending()["count"], 2)
        scoped = wake.relay_pending(binding_id="other-binding")
        self.assertEqual([d["binding_id"] for d in scoped["dispatches"]], ["other-binding"])

    def test_watch_returns_pending_times_out_and_honours_stop(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        slept: list[float] = []

        def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            self.clock[0] += seconds

        idle = wake.relay_watch(binding_id="desktop-binding", interval_s=20, timeout_s=60, sleep=fake_sleep, monotonic=lambda: self.clock[0])
        self.assertEqual((idle["result"], idle["ticks"]), ("timeout", 4))  # ticks at 0, 20, 40, 60 s
        self.assertEqual(slept, [20, 20, 20])
        self.office(SENDER).send(TARGET, "pytanie", kind="question")
        found = wake.relay_watch(binding_id="desktop-binding", interval_s=20, timeout_s=60, sleep=fake_sleep, monotonic=lambda: self.clock[0])
        self.assertEqual((found["result"], found["ticks"]), ("pending", 1))
        self.assertEqual(found["dispatches"][0]["target_local_id"], _relay.LOCAL_ID)
        wake.stop()
        stopped = wake.relay_watch(binding_id="desktop-binding", interval_s=20, timeout_s=60, sleep=fake_sleep, monotonic=lambda: self.clock[0])
        self.assertEqual(stopped["result"], "stopped")

    def test_watch_stop_wins_before_a_delay(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        slept: list[float] = []
        wake.stop()
        early = wake.relay_watch(binding_id="desktop-binding", delay_s=3600, timeout_s=60,
                                 sleep=slept.append, monotonic=lambda: self.clock[0])
        self.assertEqual((early["result"], slept), ("stopped", []))

    def test_watch_timeout_counts_tick_time(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        original = wake.tick

        def slow_tick(**kwargs):
            self.clock[0] += 50  # a slow tick consumes the budget too
            return original(**kwargs)

        wake.tick = slow_tick
        result = wake.relay_watch(binding_id="desktop-binding", interval_s=20, timeout_s=60,
                                  sleep=lambda _s: None, monotonic=lambda: self.clock[0])
        self.assertEqual((result["result"], result["ticks"]), ("timeout", 1))

    def test_watch_stop_set_during_the_delay_ends_it_after_one_step(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        slept: list[float] = []

        def sleep_then_stop(seconds: float) -> None:
            slept.append(seconds)
            self.clock[0] += seconds
            wake.stop()

        result = wake.relay_watch(binding_id="desktop-binding", interval_s=20, delay_s=3600,
                                  timeout_s=7200, sleep=sleep_then_stop, monotonic=lambda: self.clock[0])
        self.assertEqual((result["result"], slept), ("stopped", [20]))

    def test_a_dispatch_found_by_the_last_tick_wins_over_the_budget(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.office(SENDER).send(TARGET, "pytanie", kind="question")
        original = wake.tick

        def slow_tick(**kwargs):
            self.clock[0] += 500  # past the whole budget, but it produced the dispatch
            return original(**kwargs)

        wake.tick = slow_tick
        result = wake.relay_watch(binding_id="desktop-binding", interval_s=20, timeout_s=60,
                                  sleep=lambda _s: None, monotonic=lambda: self.clock[0])
        self.assertEqual((result["result"], result["ticks"]), ("pending", 1))

    def test_scoped_calls_never_lock_another_bindings_dispatch(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.enroll_other(wake, triggers=["message"])
        self.office(SENDER).send(OTHER, "do innej", kind="question")
        wake.tick()
        other_id = wake.relay_pending(binding_id="other-binding")["dispatches"][0]["dispatch_id"]
        locked: list[str] = []
        original = wake._dispatch_lock

        def spy(dispatch_id: str):
            locked.append(dispatch_id)
            return original(dispatch_id)

        wake._dispatch_lock = spy
        wake.tick(binding_id="desktop-binding")
        wake.relay_pending(binding_id="desktop-binding")
        self.assertNotIn(other_id, locked)

    def test_an_unreadable_dispatch_fails_closed_on_the_scoped_path(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.office(SENDER).send(TARGET, "pytanie", kind="question")
        wake.tick()
        self.dispatch_files()[0].write_text("{broken", encoding="utf-8")
        with self.assertRaises(CoordError):
            wake.relay_pending(binding_id="desktop-binding")

    def test_global_pending_skips_a_binding_served_by_a_watcher(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.office(SENDER).send(TARGET, "pytanie", kind="question")
        found = wake.relay_watch(binding_id="desktop-binding", sleep=lambda _s: None,
                                 monotonic=lambda: self.clock[0])
        self.assertEqual(found["result"], "pending")
        # The /loop relay's global call leaves it to the watcher; the watcher still sees it.
        self.assertEqual(wake.relay_pending()["count"], 0)
        self.assertEqual(wake.relay_pending(binding_id="desktop-binding")["count"], 1)
        # A watcher that stops pulsing hands the binding back after the grace period.
        self.clock[0] += 601
        self.assertEqual(wake.relay_pending()["count"], 1)

    def test_pulse_is_best_effort_and_bound_to_its_file_name(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.enroll_other(wake, triggers=["message"])
        self.office(SENDER).send(TARGET, "pytanie", kind="question")
        with mock.patch.object(Wake, "_atomic_replace_json", side_effect=CoordError("busy", code="wake_state_busy")):
            wake._watch_pulse("desktop-binding", 60)  # never raises
        found = wake.relay_watch(binding_id="desktop-binding", sleep=lambda _s: None,
                                 monotonic=lambda: self.clock[0])
        self.assertEqual(found["result"], "pending")
        # A pulse file named after one binding cannot hide another one.
        forged = wake.relay_watch_dir / "desktop-binding.json"
        forged.write_text(json.dumps({"binding_id": "other-binding",
                                      "fresh_until": format_ts(self.clock[0] + 600)}), encoding="utf-8")
        self.assertEqual(wake._watched_bindings(), frozenset())
        # A far-future pulse (damaged or forged) is not trusted either.
        forged.write_text(json.dumps({"binding_id": "desktop-binding",
                                      "fresh_until": format_ts(self.clock[0] + 7200)}), encoding="utf-8")
        self.assertEqual(wake._watched_bindings(), frozenset())
        forged.write_text(json.dumps({"binding_id": "desktop-binding",
                                      "fresh_until": format_ts(self.clock[0] + 600)}), encoding="utf-8")
        self.assertEqual(wake._watched_bindings(), frozenset({"desktop-binding"}))

    def test_stop_releases_the_pulse_at_once(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.office(SENDER).send(TARGET, "pytanie", kind="question")
        wake.tick()
        wake.stop()
        stopped = wake.relay_watch(binding_id="desktop-binding", sleep=lambda _s: None,
                                   monotonic=lambda: self.clock[0])
        self.assertEqual(stopped["result"], "stopped")
        self.assertEqual(wake._watched_bindings(), frozenset())

    def test_watch_needs_a_relay_binding(self) -> None:
        wake = self.wake()
        wake.enroll(
            binding_id="cli-binding", office_agent=OTHER, driver="codex-cli",
            native_thread_id="thread-cli", workspace=str(self.workspace), enrolled_by="owner",
            allowed_wakers=[SENDER], allowed_triggers=["message"], wake_kinds=["question"],
            budget_per_day=6, expires_at=format_ts(self.clock[0] + 86400), owner_confirm=True,
        )
        with self.assertRaises(CoordError) as caught:
            wake.relay_watch(binding_id="cli-binding", sleep=lambda _s: None)
        self.assertEqual(caught.exception.code, "wake_relay_watch_invalid")

    def test_scoped_tick_never_consumes_another_bindings_child_event(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.enroll_other(wake, triggers=["child-terminal"])
        result_path = self.root / "child-run" / "state.json"
        result_path.parent.mkdir()
        result_path.write_text(json.dumps({"status": "COMPLETED"}), encoding="utf-8")
        run_id = "synthetic-child-run"
        event = {
            "schema": "clawops.child-terminal/v1",
            "event_id": hashlib.sha256((run_id + "COMPLETED").encode("utf-8")).hexdigest(),
            "run_id": run_id, "source": "agent-graph", "terminal_state": "COMPLETED",
            "result_ref": str(result_path.resolve()), "parent_agent": OTHER, "parent_session": None,
            "parent_harness": "claude-desktop", "workspace": str(self.workspace.resolve()),
            "started_at": format_ts(self.clock[0] - 60), "observed_at": format_ts(self.clock[0]),
        }
        body = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
        self.office(SENDER).send(None, body, channel="watch-events", kind="note")
        self.office(SENDER).send(OTHER, body, kind="note")
        self.assertEqual(wake.tick(binding_id="desktop-binding")["decisions"], [])
        full = wake.tick()
        dispatched = [d for d in full["decisions"] if d.get("decision") == "dispatched"]
        self.assertEqual([d["binding_id"] for d in dispatched], ["other-binding"])
        self.assertEqual(len(self.dispatch_files()), 1)

    def test_scoped_refresh_never_times_out_another_bindings_claim(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.enroll_other(wake, triggers=["message"])
        self.office(SENDER).send(OTHER, "do innej", kind="question")
        wake.tick()
        other = wake.relay_pending(binding_id="other-binding")["dispatches"][0]
        wake.relay_claim(other["dispatch_id"], relay_agent="claude-desktop-relay")
        self.clock[0] += 31 * 60
        wake.relay_pending(binding_id="desktop-binding")
        wake.tick(binding_id="desktop-binding")
        path = self.dispatch_files()[0]
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "claimed")
        wake.relay_pending()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "delivery_unknown")

    def test_watch_delay_waits_before_an_already_pending_dispatch(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.office(SENDER).send(TARGET, "pytanie", kind="question")
        slept: list[float] = []

        def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            self.clock[0] += seconds

        found = wake.relay_watch(binding_id="desktop-binding", delay_s=120, sleep=fake_sleep, monotonic=lambda: self.clock[0])
        self.assertEqual((found["result"], found["ticks"], slept), ("pending", 1, [20] * 6))
        short = wake.relay_watch(binding_id="desktop-binding", delay_s=120, timeout_s=60, sleep=fake_sleep, monotonic=lambda: self.clock[0])
        self.assertEqual((short["result"], short["ticks"]), ("timeout", 0))

    def test_wake_prompts_name_the_mcp_twins(self) -> None:
        for twin in ("office_read", "office_wake_consume", "office_wake_close"):
            self.assertIn(twin, CORRELATED_WAKE_PROMPT_TEMPLATE)
        for twin in ("office_read", "office_send"):
            self.assertIn(twin, WAKE_PROMPT_TEMPLATE)

    def test_mcp_close_is_limited_to_the_own_correlation(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        wake.revoke("desktop-binding")
        wake.enroll(
            binding_id="v2-binding", office_agent=TARGET, driver="claude-desktop-relay",
            native_thread_id=_relay.LOCAL_ID, workspace=str(self.workspace), enrolled_by="owner",
            allowed_wakers=[SENDER], allowed_triggers=["message"], wake_kinds=["question", "handoff"],
            budget_per_day=6, expires_at=format_ts(self.clock[0] + 86400), owner_confirm=True,
            correlation_required=True,
        )
        wake.verify("v2-binding", owner_confirm=True)
        target = OfficeMcpServer(self.office(TARGET), heartbeat_ttl_s=None)
        sent = target.call_tool("office_send", {"to": SENDER, "body": "zlecenie", "kind": "question"})
        expected = target.call_tool("office_wake_expect", {
            "binding_id": "v2-binding", "task_id": "t1", "correlation_id": "c1",
            "counterpart": SENDER, "request_message_id": sent["id"]})
        self.assertEqual(expected["status"], "ok", expected)
        foreign = OfficeMcpServer(self.office(SENDER), heartbeat_ttl_s=None)
        refused = foreign.call_tool("office_wake_close", {"correlation_id": "c1"})
        self.assertEqual(refused["status"], "error")
        closed = target.call_tool("office_wake_close", {"correlation_id": "c1"})
        self.assertEqual(closed["status"], "ok", closed)
        self.assertEqual(wake.correlations.load("c1")["status"], "closed")


class RelayBusyDeferTests(unittest.TestCase):
    """Bounded, durable target-busy deferral: `wake-relay-defer-busy`."""

    setUp = _relay.WakeRelayTests.setUp
    wake = _relay.WakeRelayTests.wake
    office = _relay.WakeRelayTests.office
    enroll = _relay.WakeRelayTests.enroll
    add_due_schedule = _relay.WakeRelayTests.add_due_schedule
    dispatch_files = _relay.WakeRelayTests.dispatch_files
    create_schedule_dispatch = _relay.WakeRelayTests.create_schedule_dispatch
    enroll_other = RelayWatchTests.enroll_other

    def test_first_defer_backs_off_and_holds_pending_and_claim_until_eligible(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)

        deferred = wake.relay_defer_busy(dispatch["dispatch_id"])
        self.assertEqual(deferred["code"], "wake_relay_deferred_busy")
        self.assertEqual(deferred["backoff_s"], RELAY_BUSY_DEFER_SCHEDULE_S[0])
        self.assertEqual(deferred["dispatch"]["busy_defer_count"], 1)

        self.assertEqual(wake.relay_pending()["dispatches"], [])
        with self.assertRaises(CoordError) as early_claim:
            wake.relay_claim(dispatch["dispatch_id"], relay_agent=_relay.RELAY)
        self.assertEqual(early_claim.exception.code, "target_busy_backoff")

        self.clock[0] += RELAY_BUSY_DEFER_SCHEDULE_S[0]
        self.assertEqual(
            [row["dispatch_id"] for row in wake.relay_pending()["dispatches"]],
            [dispatch["dispatch_id"]],
        )
        claimed = wake.relay_claim(dispatch["dispatch_id"], relay_agent=_relay.RELAY)
        self.assertEqual(claimed["dispatch"]["status"], "claimed")

        decisions = [row["decision"] for row in wake.ledger(limit=20)["entries"]]
        self.assertEqual(decisions, ["dispatched", "deferred", "claimed"])

    def test_a_fractional_second_clock_still_holds_for_the_full_backoff(self) -> None:
        # format_ts truncates to whole seconds; if the eligibility deadline were computed
        # as format_ts(now + backoff_s) directly, a busy report at a sub-second clock value
        # would round the stored deadline down, letting relay_claim succeed up to ~1s before
        # the full backoff actually elapsed. The deadline must be ceiled, never truncated.
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        self.clock[0] += 0.9

        deferred = wake.relay_defer_busy(dispatch["dispatch_id"])
        backoff_s = RELAY_BUSY_DEFER_SCHEDULE_S[0]
        self.assertEqual(deferred["backoff_s"], backoff_s)

        # Advance by exactly the declared backoff: the full backoff has not elapsed yet
        # (deferred at x.9, so eligibility is truly at x.9 + backoff_s), and must still hold.
        self.clock[0] += backoff_s - 0.1
        with self.assertRaises(CoordError) as still_busy:
            wake.relay_claim(dispatch["dispatch_id"], relay_agent=_relay.RELAY)
        self.assertEqual(still_busy.exception.code, "target_busy_backoff")

        # Once the full backoff has actually elapsed, the claim succeeds.
        self.clock[0] += 1.0
        claimed = wake.relay_claim(dispatch["dispatch_id"], relay_agent=_relay.RELAY)
        self.assertEqual(claimed["dispatch"]["status"], "claimed")

    def test_repeated_busy_follows_the_schedule_and_the_sixth_call_is_terminal(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        dispatch_id = dispatch["dispatch_id"]

        for expected_count, backoff_s in enumerate(RELAY_BUSY_DEFER_SCHEDULE_S, start=1):
            deferred = wake.relay_defer_busy(dispatch_id)
            self.assertEqual(deferred["code"], "wake_relay_deferred_busy")
            self.assertEqual(deferred["backoff_s"], backoff_s)
            self.assertEqual(deferred["dispatch"]["busy_defer_count"], expected_count)
            self.clock[0] += backoff_s

        terminal = wake.relay_defer_busy(dispatch_id)
        self.assertEqual(terminal["code"], "wake_relay_busy_limit")
        self.assertEqual(terminal["dispatch"]["status"], "cancelled")
        self.assertEqual(terminal["dispatch"]["cancel_reason"], "target_busy_limit")
        self.assertEqual(terminal["dispatch"]["busy_defer_count"], len(RELAY_BUSY_DEFER_SCHEDULE_S) + 1)

        self.assertEqual(wake.relay_pending()["dispatches"], [])
        with self.assertRaises(CoordError) as fenced:
            wake.relay_claim(dispatch_id, relay_agent=_relay.RELAY)
        self.assertEqual(fenced.exception.code, "relay_dispatch_fenced")
        with self.assertRaises(CoordError) as no_resend:
            wake.relay_defer_busy(dispatch_id)
        self.assertEqual(no_resend.exception.code, "relay_dispatch_fenced")

        cancelled_entry = wake.ledger(limit=20)["entries"][-1]
        self.assertEqual(
            (cancelled_entry["decision"], cancelled_entry["reason"]),
            ("cancelled", "target_busy_limit"),
        )

    def test_defer_busy_after_claim_is_fenced(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        wake.relay_claim(dispatch["dispatch_id"], relay_agent=_relay.RELAY)
        with self.assertRaises(CoordError) as fenced:
            wake.relay_defer_busy(dispatch["dispatch_id"])
        self.assertEqual(fenced.exception.code, "relay_dispatch_fenced")

    def test_defer_busy_on_an_already_cancelled_dispatch_is_fenced(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        self.clock[0] += 3 * 60 * 60 + 1  # past DEFAULT_RELAY_PENDING_TTL_S
        self.assertEqual(wake.relay_pending()["dispatches"], [])  # cancels it: dispatch_expired
        current = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.assertEqual(current["status"], "cancelled")
        with self.assertRaises(CoordError) as fenced:
            wake.relay_defer_busy(dispatch["dispatch_id"])
        self.assertEqual(fenced.exception.code, "relay_dispatch_fenced")

    def test_defer_busy_during_stop_cancels_with_global_stop_not_busy_limit(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        wake.stop()
        with self.assertRaises(CoordError) as stopped:
            wake.relay_defer_busy(dispatch["dispatch_id"])
        self.assertEqual(stopped.exception.code, "relay_dispatch_cancelled")
        self.assertEqual(stopped.exception.payload["reason"], "global_stop")
        current = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.assertEqual((current["status"], current["cancel_reason"]), ("cancelled", "global_stop"))
        self.assertNotIn("busy_defer_count", current)

    def test_defer_busy_is_scoped_to_its_own_binding(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.enroll_other(wake, triggers=["message"])
        self.office(SENDER).send(TARGET, "do celu", kind="question")
        self.office(SENDER).send(OTHER, "do innej", kind="question")
        wake.tick()
        own = wake.relay_pending(binding_id="desktop-binding")["dispatches"][0]
        other = wake.relay_pending(binding_id="other-binding")["dispatches"][0]

        wake.relay_defer_busy(own["dispatch_id"])

        self.assertEqual(
            [row["dispatch_id"] for row in wake.relay_pending(binding_id="other-binding")["dispatches"]],
            [other["dispatch_id"]],
        )
        self.assertEqual(wake.relay_pending(binding_id="desktop-binding")["dispatches"], [])

    def test_defer_busy_rejects_a_malformed_dispatch_id(self) -> None:
        wake = self.wake()
        with self.assertRaises(CoordError) as invalid:
            wake.relay_defer_busy("not-a-hex-dispatch-id")
        self.assertEqual(invalid.exception.code, "invalid_dispatch_id")

    def test_dispatch_schema_is_unchanged_until_a_busy_report_is_recorded(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        self.assertNotIn("busy_defer_count", dispatch)
        self.assertNotIn("busy_next_eligible_at", dispatch)
        # An older dispatch file with no busy fields still loads and claims normally.
        claimed = wake.relay_claim(dispatch["dispatch_id"], relay_agent=_relay.RELAY)
        self.assertEqual(claimed["dispatch"]["status"], "claimed")

    def test_relay_defer_busy_cli_and_mcp_twins(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        base = ["--home", str(self.home), "--agent", _relay.RELAY]

        def run(*arguments: str) -> tuple[int, dict]:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = office_main(base + list(arguments))
            return code, json.loads(stdout.getvalue())

        code, deferred = run("wake-relay-defer-busy", "--dispatch-id", dispatch["dispatch_id"])
        self.assertEqual((code, deferred["code"]), (0, "wake_relay_deferred_busy"))
        self.assertEqual(deferred["dispatch"]["busy_defer_count"], 1)

        self.clock[0] += RELAY_BUSY_DEFER_SCHEDULE_S[0]
        # RELAY is the runbook's dedicated Office Wake Relay session: a distinct Office
        # identity from the binding's own `office_agent` (TARGET, the addressee it wakes).
        # Its MCP connection is what actually calls this tool -- not the addressee itself.
        server = OfficeMcpServer(self.office(_relay.RELAY), heartbeat_ttl_s=None)
        mcp_result = server.call_tool(
            "office_wake_relay_defer_busy", {"dispatch_id": dispatch["dispatch_id"]}
        )
        self.assertEqual(mcp_result["status"], "ok")
        self.assertEqual(mcp_result["code"], "wake_relay_deferred_busy")
        self.assertEqual(mcp_result["dispatch"]["busy_defer_count"], 2)

    def test_relay_defer_busy_mcp_works_for_the_dedicated_relay_session_not_the_addressee(
        self,
    ) -> None:
        """The real caller (runbook: a dedicated Office Wake Relay session) is never the
        binding's own `office_agent` (TARGET), the addressee it wakes -- and no relay identity
        is recorded anywhere before claim. So, like the CLI form and the rest of
        `wake-relay-*` (`relay_claim`/`relay_receipt`/`relay_observe`/`relay_reconcile`, none
        of which check a caller either), MCP authorizes solely via possession of the
        unguessable `dispatch_id`, not by matching the caller to the addressee.
        """
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        server = OfficeMcpServer(self.office(_relay.RELAY), heartbeat_ttl_s=None)
        result = server.call_tool(
            "office_wake_relay_defer_busy", {"dispatch_id": dispatch["dispatch_id"]}
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["code"], "wake_relay_deferred_busy")
        self.assertEqual(result["dispatch"]["busy_defer_count"], 1)

        current = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.assertEqual(current["status"], "pending")
        self.assertEqual(current["busy_defer_count"], 1)

    def test_binding_paused_during_busy_backoff_cancels_with_the_real_reason_not_a_hold(
        self,
    ) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        deferred = wake.relay_defer_busy(dispatch["dispatch_id"])
        self.assertEqual(deferred["code"], "wake_relay_deferred_busy")

        wake.pause("desktop-binding")
        # Still inside the busy backoff window: a revoked/paused binding must cancel the
        # dispatch immediately with its own reason, not hold it as target_busy_backoff until
        # the backoff expires.
        self.assertEqual(wake.relay_pending()["dispatches"], [])
        current = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.assertEqual(current["status"], "cancelled")
        self.assertEqual(current["cancel_reason"], "binding_paused")

        with self.assertRaises(CoordError) as claimed:
            wake.relay_claim(dispatch["dispatch_id"], relay_agent=_relay.RELAY)
        self.assertEqual(claimed.exception.code, "relay_dispatch_fenced")
        with self.assertRaises(CoordError) as deferred_again:
            wake.relay_defer_busy(dispatch["dispatch_id"])
        self.assertEqual(deferred_again.exception.code, "relay_dispatch_fenced")

    def test_defer_busy_never_echoes_the_free_text_prompt_payload(self) -> None:
        """Fixed structured fields only: the relay already has the prompt from claim/pending,
        so a busy-backoff response must not re-send the (up to 8192-char) dispatch `text` on
        every deferral or on the terminal sixth call.
        """
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        self.assertIn("text", dispatch)
        dispatch_id = dispatch["dispatch_id"]

        for backoff_s in RELAY_BUSY_DEFER_SCHEDULE_S:
            deferred = wake.relay_defer_busy(dispatch_id)
            self.assertNotIn("text", deferred["dispatch"])
            self.clock[0] += backoff_s

        terminal = wake.relay_defer_busy(dispatch_id)
        self.assertEqual(terminal["code"], "wake_relay_busy_limit")
        self.assertNotIn("text", terminal["dispatch"])

    def test_defer_busy_cancelled_by_gate_never_echoes_the_free_text_prompt_payload(
        self,
    ) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        self.assertIn("text", dispatch)
        wake.stop()
        with self.assertRaises(CoordError) as stopped:
            wake.relay_defer_busy(dispatch["dispatch_id"])
        self.assertEqual(stopped.exception.code, "relay_dispatch_cancelled")
        self.assertNotIn("text", stopped.exception.payload["dispatch"])


if __name__ == "__main__":
    unittest.main()
