"""Relay daily budget: queue-only dispatches cost zero, uptake counts once.

A relay dispatch queued by tick (ledger ``dispatched``/``relay_pending``) must not
consume the binding's daily budget until the relay actually takes it:
``relay_claim`` writes ``claimed``/``relay_claimed``, which charges the claim day
exactly once per dispatch. The claim enforces the cap after the
STOP/expiry/binding/unknown gates, and tick-time queueing stays fenced once
uptake has exhausted the budget.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from support import PACKAGE_ROOT  # noqa: F401  (puts the package on sys.path)

from coordfs.errors import CoordError
from coordfs.codex_ack import owned_codex_ack, owned_zcode_reply
from coordfs.codex_desktop_tool_host import (
    ACK_SCHEMA, OBSERVATION_SCHEMA, OBSERVATION_SOURCE, PROOF_SCHEMA,
    CodexDesktopTarget, CodexDesktopToolHost, TargetRegistry,
)
from coordfs.leases import format_ts, parse_ts
from coordfs.office import Office
from coordfs.wake import CODEX_RELAY_DRIVER, ZCODE_RELAY_DRIVER, Wake
import test_wake_relay as _relay
from test_wake_relay import RELAY, SENDER, TARGET


class RelayBudgetTests(unittest.TestCase):
    setUp = _relay.WakeRelayTests.setUp
    wake = _relay.WakeRelayTests.wake
    office = _relay.WakeRelayTests.office
    enroll = _relay.WakeRelayTests.enroll
    add_due_schedule = _relay.WakeRelayTests.add_due_schedule
    dispatch_files = _relay.WakeRelayTests.dispatch_files
    create_schedule_dispatch = _relay.WakeRelayTests.create_schedule_dispatch

    def attempts(self, wake: Wake, binding_id: str = "desktop-binding") -> int:
        binding = wake._load_binding(binding_id)
        return wake._daily_attempts(binding, wake._ledger_entries())

    def add_second_due_schedule(self, wake: Wake) -> None:
        wake.add_schedule(
            schedule_id="desktop-second",
            binding_id="desktop-binding",
            time="17:00",
            days="daily",
            task_text="drugi raport syntetyczny",
            created_by="owner",
            owner_confirm=True,
        )

    def two_queued_dispatch_ids(self, wake: Wake) -> list[str]:
        with mock.patch("coordfs.wake.subprocess.run"):
            result = wake.tick()
        self.assertEqual(result["counts"]["dispatched"], 2)
        return [row["dispatch_id"] for row in wake.relay_pending()["dispatches"]]

    def test_queue_only_dispatches_cost_zero(self) -> None:
        wake = self.wake()
        self.enroll(wake, budget=1)
        self.add_due_schedule(wake)
        self.add_second_due_schedule(wake)
        with mock.patch("coordfs.wake.subprocess.run"):
            result = wake.tick()
        self.assertEqual(result["counts"]["dispatched"], 2)
        self.assertEqual(self.attempts(wake), 0)

    def test_uptake_counts_once_per_dispatch(self) -> None:
        wake = self.wake()
        self.enroll(wake, budget=6)
        self.add_due_schedule(wake)
        self.add_second_due_schedule(wake)
        first_id, second_id = self.two_queued_dispatch_ids(wake)
        wake.relay_claim(first_id, relay_agent=RELAY)
        self.assertEqual(self.attempts(wake), 1)
        # Returning the claim to pending and taking it again is still one uptake.
        wake.relay_native_unsubmitted(first_id, relay_agent=RELAY)
        self.assertEqual(self.attempts(wake), 1)
        wake.relay_claim(first_id, relay_agent=RELAY)
        self.assertEqual(self.attempts(wake), 1)
        wake.relay_claim(second_id, relay_agent=RELAY)
        self.assertEqual(self.attempts(wake), 2)

    def test_claim_enforces_the_cap(self) -> None:
        wake = self.wake()
        self.enroll(wake, budget=1)
        self.add_due_schedule(wake)
        self.add_second_due_schedule(wake)
        first_id, second_id = self.two_queued_dispatch_ids(wake)
        wake.relay_claim(first_id, relay_agent=RELAY)
        with self.assertRaises(CoordError) as exhausted:
            wake.relay_claim(second_id, relay_agent=RELAY)
        self.assertEqual(exhausted.exception.code, "daily_budget_exhausted")
        # The refused uptake stays queued and costs nothing.
        self.assertEqual(self.attempts(wake), 1)
        pending = wake.relay_pending()["dispatches"]
        self.assertEqual(
            [(row["dispatch_id"], row["status"]) for row in pending],
            [(second_id, "pending")],
        )
        self.assertEqual(wake._load_dispatch(first_id)["status"], "claimed")

    def test_queueing_blocks_once_uptake_exhausts_the_budget(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message", "schedule"], budget=1)
        self.add_due_schedule(wake)
        with mock.patch("coordfs.wake.subprocess.run"):
            self.assertEqual(wake.tick()["counts"]["dispatched"], 1)
        dispatch = wake.relay_pending()["dispatches"][0]
        wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        self.office(SENDER).send(TARGET, "kolejne pytanie", kind="question")
        with mock.patch("coordfs.wake.subprocess.run"):
            result = wake.tick()
        self.assertIn(
            "daily_budget_exhausted",
            [row["reason"] for row in result["decisions"]],
        )
        self.assertEqual(len(self.dispatch_files()), 1)

    def test_stop_fencing_wins_over_an_exhausted_budget(self) -> None:
        wake = self.wake()
        self.enroll(wake, budget=1)
        self.add_due_schedule(wake)
        self.add_second_due_schedule(wake)
        first_id, second_id = self.two_queued_dispatch_ids(wake)
        wake.relay_claim(first_id, relay_agent=RELAY)
        wake.stop()
        with self.assertRaises(CoordError) as stopped:
            wake.relay_claim(second_id, relay_agent=RELAY)
        self.assertEqual(stopped.exception.code, "relay_dispatch_cancelled")
        self.assertEqual(stopped.exception.payload["reason"], "global_stop")

    def test_uptake_charges_the_claim_day(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        self.assertEqual(self.attempts(wake), 1)
        self.clock[0] += 3 * 86400
        self.assertEqual(self.attempts(wake), 0)

    def test_ten_historical_pending_queues_cost_zero_until_one_pickup(self) -> None:
        wake = self.wake()
        self.enroll(wake, budget=1)
        binding = wake._load_binding("desktop-binding")
        dispatches = []
        for index in range(10):
            dispatch = wake._create_relay_dispatch(
                binding, text="synthetic historical queue", trigger="schedule",
                trigger_ref=f"old-schedule:{index}",
            )
            wake._append_relay_stage(dispatch, decision="dispatched", reason="relay_pending")
            dispatches.append(dispatch)
        self.assertEqual(self.attempts(self.wake()), 0)
        self.assertEqual(len(wake.relay_pending()["dispatches"]), 10)
        wake.relay_claim(dispatches[0]["dispatch_id"], relay_agent=RELAY)
        restarted = self.wake()
        self.assertEqual(self.attempts(restarted), 1)
        with self.assertRaises(CoordError) as capped:
            restarted.relay_claim(dispatches[1]["dispatch_id"], relay_agent=RELAY)
        self.assertEqual(capped.exception.code, "daily_budget_exhausted")
        self.assertEqual(len(restarted.relay_pending()["dispatches"]), 9)

    def test_reclaim_at_the_cap_and_receipt_never_charge_twice(self) -> None:
        wake = self.wake()
        self.enroll(wake, budget=1)
        self.add_due_schedule(wake)
        wake.tick()
        dispatch_id = wake.relay_pending()["dispatches"][0]["dispatch_id"]
        wake.relay_claim(dispatch_id, relay_agent=RELAY)
        wake.relay_native_unsubmitted(dispatch_id, relay_agent=RELAY)
        self.wake().relay_claim(dispatch_id, relay_agent=RELAY)
        wake.relay_receipt(dispatch_id, status="delivered", message_id="synthetic-receipt")
        wake.relay_receipt(dispatch_id, status="delivered", message_id="synthetic-receipt")
        self.assertEqual(self.attempts(self.wake()), 1)

    def test_queue_day_is_free_and_first_claim_uses_utc_day(self) -> None:
        self.clock[0] = parse_ts("2030-01-01T23:59:58Z")
        wake = self.wake()
        self.enroll(wake, budget=1)
        dispatch = wake._create_relay_dispatch(
            wake._load_binding("desktop-binding"), text="synthetic UTC queue",
            trigger="schedule", trigger_ref="utc-rollover",
        )
        wake._append_relay_stage(dispatch, decision="dispatched", reason="relay_pending")
        self.assertEqual(self.attempts(wake), 0)
        self.clock[0] = parse_ts("2030-01-02T00:00:01Z")
        wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        self.assertEqual(self.attempts(self.wake()), 1)
        wake.relay_native_unsubmitted(dispatch["dispatch_id"], relay_agent=RELAY)
        # Crossing midnight with a pre-seam reclaim still belongs to the first claim day.
        self.clock[0] = parse_ts("2030-01-02T23:59:58Z")
        # The queue TTL remains independently enforced, so use a fresh synthetic
        # ledger to verify the date rule without bypassing delivery expiry.
        entries = wake._ledger_entries()
        repeated = dict(entries[-2])
        repeated.update(decision="claimed", reason="relay_claimed",
                        started_at="2030-01-03T00:00:01Z")
        self.clock[0] = parse_ts("2030-01-03T00:00:01Z")
        self.assertEqual(wake._daily_attempts(wake._load_binding("desktop-binding"), [*entries, repeated]), 0)

    def test_claim_timestamp_offset_is_normalized_and_legacy_attempts_survive(self) -> None:
        self.clock[0] = parse_ts("2030-01-01T23:30:00Z")
        wake = self.wake()
        self.enroll(wake)
        binding = wake._load_binding("desktop-binding")
        base = {"binding_id": binding["binding_id"], "started_at": format_ts(self.clock[0])}
        entries = [
            dict(base, decision="dispatched", reason="relay_pending"),
            dict(base, decision="woken", reason="driver_accepted"),
            dict(base, decision="woken", reason="delivery_unknown",
                 driver_result={"status": "delivery_unknown"}),
            dict(base, decision="dispatched", reason="host_pending"),
            dict(base, decision="claimed", reason="relay_claimed", dispatch_id="a" * 32,
                 started_at="2030-01-02T00:10:00+01:00"),
        ]
        self.assertEqual(wake._daily_attempts(binding, entries), 4)
        self.assertEqual(wake._trigger_attempts_ref(binding, "example", [
            dict(base, decision="dispatched", reason="relay_pending", trigger_ref="example")
        ]), 1)

    def test_unknown_after_native_begin_stays_fenced_and_never_refunds_uptake(self) -> None:
        wake = self.wake()
        self.enroll(wake, budget=1)
        self.add_due_schedule(wake)
        self.add_second_due_schedule(wake)
        first_id, second_id = self.two_queued_dispatch_ids(wake)
        wake.relay_claim(first_id, relay_agent=RELAY)
        wake.relay_native_begin(first_id, relay_agent=RELAY)
        self.assertEqual(self.attempts(self.wake()), 1)
        with self.assertRaises(CoordError) as unknown:
            self.wake().relay_claim(second_id, relay_agent=RELAY)
        self.assertEqual(unknown.exception.code, "delivery_unknown_unreconciled")
        wake.relay_reconcile(first_id, outcome="not_delivered", owner_confirm=True)
        self.assertEqual(self.attempts(wake), 1)
        with self.assertRaises(CoordError) as capped:
            wake.relay_claim(second_id, relay_agent=RELAY)
        self.assertEqual(capped.exception.code, "daily_budget_exhausted")

    def test_failed_claim_publication_cannot_release_budget_to_a_sibling(self) -> None:
        wake = self.wake()
        self.enroll(wake, budget=1)
        self.add_due_schedule(wake)
        self.add_second_due_schedule(wake)
        first_id, second_id = self.two_queued_dispatch_ids(wake)
        with mock.patch.object(wake, "_atomic_replace_json", side_effect=OSError("synthetic interrupted publish")):
            with self.assertRaises(OSError):
                wake.relay_claim(first_id, relay_agent=RELAY)
        self.assertEqual(wake._load_dispatch(first_id)["status"], "pending")
        self.assertEqual(self.attempts(self.wake()), 1)
        with self.assertRaises(CoordError) as capped:
            self.wake().relay_claim(second_id, relay_agent=RELAY)
        self.assertEqual(capped.exception.code, "daily_budget_exhausted")
        self.wake().relay_claim(first_id, relay_agent=RELAY)
        self.assertEqual(self.attempts(wake), 1)

    def test_two_processes_cannot_claim_distinct_dispatches_past_budget_one(self) -> None:
        wake = self.wake()
        self.enroll(wake, budget=1)
        self.add_due_schedule(wake)
        self.add_second_due_schedule(wake)
        dispatch_ids = self.two_queued_dispatch_ids(wake)
        release = self.root / "release-claims"
        code = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from coordfs.wake import Wake
from coordfs.errors import CoordError
wake = Wake(home=Path(sys.argv[2]), now=lambda: float(sys.argv[3]))
real_budget = wake._daily_attempts
def paced_budget(binding, entries):
    time.sleep(0.15)
    return real_budget(binding, entries)
wake._daily_attempts = paced_budget
Path(sys.argv[6]).write_text('ready', encoding='utf-8')
deadline = time.monotonic() + 10
while not Path(sys.argv[5]).is_file():
    if time.monotonic() > deadline:
        raise RuntimeError('synthetic race start timed out')
    time.sleep(0.01)
try:
    result = wake.relay_claim(sys.argv[4], relay_agent='claude-desktop-relay')
    print(json.dumps({'status': 'claimed', 'dispatch_id': result['dispatch']['dispatch_id']}))
except CoordError as exc:
    print(json.dumps({'status': 'refused', 'code': exc.code}))
"""
        processes = []
        for index, dispatch_id in enumerate(dispatch_ids):
            ready = self.root / f"ready-{index}"
            processes.append(subprocess.Popen(
                [sys.executable, "-B", "-X", "utf8", "-c", code, str(PACKAGE_ROOT),
                 str(self.home), str(self.clock[0]), dispatch_id, str(release), str(ready)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ))
        deadline = time.monotonic() + 10
        while not all((self.root / f"ready-{index}").is_file() for index in range(2)):
            if time.monotonic() > deadline:
                break
            time.sleep(0.01)
        release.write_text("start", encoding="utf-8")
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual(sorted(row["status"] for row in results), ["claimed", "refused"])
        self.assertEqual([row["code"] for row in results if row["status"] == "refused"], ["daily_budget_exhausted"])
        self.assertEqual(self.attempts(self.wake()), 1)
        self.assertEqual(sorted(wake._load_dispatch(value)["status"] for value in dispatch_ids), ["claimed", "pending"])

    def assert_unknown_publication_is_atomic_with_pickup(self, mode: str) -> None:
        wake = self.wake()
        self.enroll(wake, budget=6)
        self.add_due_schedule(wake)
        self.add_second_due_schedule(wake)
        first_id, sibling_id = self.two_queued_dispatch_ids(wake)
        wake.relay_claim(first_id, relay_agent=RELAY)
        self.clock[0] += 31 * 60 if mode == "timeout" else 1
        barrier = self.root / "unknown-race"
        barrier.mkdir()
        code = """
import json, sys, time
from contextlib import contextmanager
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import coordfs.wake as wake_module
from coordfs.wake import Wake
from coordfs.errors import CoordError
wake = Wake(home=Path(sys.argv[2]), now=lambda: float(sys.argv[3]))
first, sibling, role, mode, barrier = sys.argv[4:9]
barrier = Path(barrier)
order = []
real_mutex = wake_module.exclusive_mutex
@contextmanager
def traced_mutex(path, *args, **kwargs):
    if path == wake._attempt_lock('desktop-binding'):
        order.append('attempt')
        if role == 'unknown':
            (barrier / 'writer_waiting_attempt').write_text('ready', encoding='utf-8')
    elif path == wake._dispatch_lock(first):
        order.append('first_dispatch')
    elif path == wake._dispatch_lock(sibling):
        order.append('sibling_dispatch')
    with real_mutex(path, *args, **kwargs):
        yield
wake_module.exclusive_mutex = traced_mutex
if role == 'claim':
    eligibility = wake._relay_eligibility_reason
    def paused_eligibility(current):
        reason = eligibility(current)
        (barrier / 'claim_eligible').write_text(json.dumps({'reason': reason}), encoding='utf-8')
        deadline = time.monotonic() + 15
        while not (barrier / 'release_claim').is_file():
            if time.monotonic() > deadline:
                raise RuntimeError('synthetic claim barrier timed out')
            time.sleep(0.01)
        return reason
    wake._relay_eligibility_reason = paused_eligibility
else:
    append = wake._append_relay_stage
    def published(current, **kwargs):
        append(current, **kwargs)
        if current['dispatch_id'] == first and kwargs['decision'] == 'delivery_unknown':
            (barrier / 'unknown_published').write_text('durable', encoding='utf-8')
    wake._append_relay_stage = published
try:
    if role == 'claim':
        wake.relay_claim(sibling, relay_agent='claude-desktop-relay')
        status = 'claimed'
    elif mode == 'native':
        wake.relay_native_begin(first, relay_agent='claude-desktop-relay')
        status = 'unknown'
    else:
        wake._refresh_relay_timeouts('desktop-binding')
        status = 'unknown'
    print(json.dumps({'status': status, 'order': order}))
except CoordError as exc:
    print(json.dumps({'status': 'refused', 'code': exc.code, 'order': order}))
"""

        def start(role: str):
            return subprocess.Popen(
                [sys.executable, "-B", "-X", "utf8", "-c", code, str(PACKAGE_ROOT),
                 str(self.home), str(self.clock[0]), first_id, sibling_id, role, mode, str(barrier)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        def wait_for_any(*names: str) -> None:
            deadline = time.monotonic() + 10
            while not any((barrier / name).is_file() for name in names):
                if time.monotonic() > deadline:
                    self.fail(f"synthetic race did not reach {names}")
                time.sleep(0.01)

        claimant = start("claim")
        writer = None
        try:
            wait_for_any("claim_eligible")
            self.assertIsNone(json.loads((barrier / "claim_eligible").read_text(encoding="utf-8"))["reason"])
            writer = start("unknown")
            # On the old artifact UNKNOWN publishes while the claim is paused.
            # With a shared binding lock the writer waits instead: release the
            # claim so either legal linear order can finish without a deadlock.
            wait_for_any("unknown_published", "writer_waiting_attempt")
        finally:
            (barrier / "release_claim").write_text("go", encoding="utf-8")
            claim_stdout, claim_stderr = claimant.communicate(timeout=20)
            if writer is not None:
                writer_stdout, writer_stderr = writer.communicate(timeout=20)
        self.assertEqual(claimant.returncode, 0, claim_stderr)
        self.assertIsNotNone(writer)
        self.assertEqual(writer.returncode, 0, writer_stderr)
        claim_result, writer_result = json.loads(claim_stdout), json.loads(writer_stdout)
        self.assertEqual(writer_result["status"], "unknown", writer_result)
        entries = self.wake()._ledger_entries()
        unknown_index = next(index for index, row in enumerate(entries)
                             if row.get("dispatch_id") == first_id and row.get("decision") == "delivery_unknown")
        late_claims = [row for row in entries[unknown_index + 1:]
                       if row.get("dispatch_id") == sibling_id and row.get("reason") == "relay_claimed"]
        self.assertEqual(late_claims, [], "sibling pickup published after durable UNKNOWN")
        if claim_result["status"] == "refused":
            self.assertEqual(claim_result["code"], "delivery_unknown_unreconciled")
        else:
            self.assertEqual(claim_result["status"], "claimed")
        # All timeout/native publications use the same attempt-before-dispatch
        # order, not an acquisition of the binding mutex while holding dispatch.
        self.assertIn("attempt", writer_result["order"])
        self.assertLess(writer_result["order"].index("attempt"), writer_result["order"].index("first_dispatch"))
        self.assertEqual(wake._load_dispatch(first_id)["status"], "delivery_unknown")
        self.assertLess(self.attempts(wake), 6)  # This fence is independent of budget exhaustion.

    def test_two_process_unknown_native_begin_is_atomic_with_budget_six_pickup(self) -> None:
        self.assert_unknown_publication_is_atomic_with_pickup("native")

    def test_two_process_unknown_timeout_is_atomic_with_budget_six_pickup(self) -> None:
        self.assert_unknown_publication_is_atomic_with_pickup("timeout")


class NativeRelaySettlementTests(unittest.TestCase):
    """Run the existing native host proof/ACK contract against a synthetic Wake store."""

    setUp = _relay.WakeRelayTests.setUp
    wake = _relay.WakeRelayTests.wake
    attempts = RelayBudgetTests.attempts
    thread_id = "123e4567-e89b-12d3-a456-426614174000"
    prior_turn = "123e4567-e89b-12d3-a456-426614174001"
    new_turn = "123e4567-e89b-12d3-a456-426614174002"
    target_agent = "codex-test-" + thread_id
    relay_agent = "codex-test-123e4567-e89b-12d3-a456-426614174003"

    def office(self, agent: str) -> Office:
        for participant in (self.target_agent, self.relay_agent, SENDER):
            Office(agent_id=participant, home=self.home)._remember_canonical_agent()
        return Office(agent_id=agent, home=self.home, now=lambda: self.clock[0])

    def prepare_native(self) -> None:
        wake = self.wake()
        wake.enroll(
            binding_id="desktop-binding", office_agent=self.target_agent,
            driver=CODEX_RELAY_DRIVER, native_thread_id=self.thread_id,
            workspace=str(self.workspace), enrolled_by="owner", allowed_wakers=[SENDER],
            allowed_triggers=["message"], wake_kinds=["question"], budget_per_day=6,
            expires_at=format_ts(self.clock[0] + 30 * 86400), owner_confirm=True,
        )
        wake.verify("desktop-binding", owner_confirm=True)
        for text in ("synthetic native input", "synthetic sibling input"):
            self.assertEqual(self.office(SENDER).send(self.target_agent, text, kind="question")["status"], "ok")
        self.assertEqual(wake.tick()["counts"]["dispatched"], 2)
        pending = wake.relay_pending(driver=CODEX_RELAY_DRIVER)["dispatches"]
        self.requests = []

        def request(op, identity, args):
            self.requests.append((op, identity, dict(args)))
            current = self.wake()  # Every Office operation sees only durable state.
            if op == "wake-stop-status":
                return {"status": "ok", "stop_active": current._stop_is_present()}
            if op == "wake-relay-pending":
                return current.relay_pending(**args)
            if op == "wake-relay-claim":
                return current.relay_claim(**args)
            if op == "wake-relay-native-begin":
                return current.relay_native_begin(**args)
            if op == "wake-relay-receipt":
                return current.relay_receipt(**args, relay_agent=identity["agent_id"])
            if op == "wake-relay-observe":
                return current.relay_observe(**args, relay_agent=identity["agent_id"])
            if op == "delivery":
                # Inject the host's existing metadata response shape using an
                # actual durable synthetic inbox ACK. Office.delivery itself
                # accepts only channels; this is not an inbox API extension.
                ack = owned_codex_ack(self.home, self.first_id, identity["agent_id"])
                matched = (ack["acknowledged"] and args["id"] == ack["trigger_ref"]
                           and args["channel"] == "synthetic-native-proof")
                return {"status": "ok", "state": "acknowledged" if matched else "written",
                        "acknowledged_by": [self.target_agent] if matched else [],
                        "acks": [{"agent": self.target_agent, "ack_id": ack["ack_id"]}] if matched else []}
            self.fail(f"unexpected native host operation: {op}")

        target = CodexDesktopTarget(
            binding_id="desktop-binding", native_thread_id=self.thread_id,
            office_agent=self.target_agent, project_slug="synthetic-native-proof",
            workspace=str(self.workspace), relay_identity=self.relay_agent, authorized=True,
        )
        self.host = CodexDesktopToolHost(SimpleNamespace(request=request), TargetRegistry([target]),
                                         now=lambda: self.clock[0])
        self.assertEqual(self.host.accept_watch("desktop-binding", {
            "status": "ok", "code": "wake_relay_watch", "binding_ids": ["desktop-binding"],
            "result": "pending",
        })["code"], "watch_pending")
        ready = self.host.prepare("desktop-binding", {
            "schema": OBSERVATION_SCHEMA, "source": OBSERVATION_SOURCE,
            "native_thread_id": self.thread_id, "project_slug": target.project_slug,
            "workspace": str(self.workspace), "observed_at": self.clock[0],
            "thread_state": "idle", "latest_turn_id": self.prior_turn,
        })
        self.assertEqual(ready["code"], "submit_once", ready)
        self.first_id = ready["dispatch_id"]
        self.sibling_id = next(row["dispatch_id"] for row in pending if row["dispatch_id"] != self.first_id)
        self.trigger_ref = self.wake()._load_dispatch(self.first_id)["trigger_ref"]
        # No official tool is invoked: this is the actual ThreadId-only result
        # shape whose delivered promotion the production host performs later.
        self.assertEqual(self.host.settle(self.first_id, {"threadId": self.thread_id})["code"], "receipt_unknown")

    def target_ack(self) -> dict:
        ack = self.office(self.target_agent).ack(self.trigger_ref)
        self.assertEqual(ack["status"], "ok", ack)
        return {"schema": ACK_SCHEMA, "message_id": self.trigger_ref, "ack_id": ack["id"],
                "from_agent": self.target_agent, "channel": "synthetic-native-proof"}

    def target_proof(self, *, completed: bool = False) -> dict:
        return {"schema": PROOF_SCHEMA, "native_thread_id": self.thread_id,
                "message_id": None, "turn_id": self.new_turn, "observed_at": self.clock[0],
                "phase": "completed" if completed else "started", "thread_ended": completed}

    def assert_fenced(self) -> None:
        wake = self.wake()
        self.assertTrue(wake._has_unreconciled_unknown(wake._load_binding("desktop-binding"), wake._ledger_entries()))
        with self.assertRaises(CoordError) as refused:
            wake.relay_claim(self.sibling_id, relay_agent=self.relay_agent, driver=CODEX_RELAY_DRIVER)
        self.assertEqual(refused.exception.code, "delivery_unknown_unreconciled")
        self.assertEqual(self.attempts(self.wake()), 1)

    def reconciliation_rows(self) -> list[dict]:
        return [row for row in self.wake()._ledger_entries()
                if row.get("dispatch_id") == self.first_id and row.get("decision") == "reconciled"]

    def test_native_host_proven_delivery_releases_fence_and_retries_after_restart(self) -> None:
        self.prepare_native()
        ack = self.target_ack()
        self.assert_fenced()  # An ACK alone is not proof of a new native turn.
        result = self.host.reconcile(self.first_id, self.target_proof(), ack)
        self.assertEqual(result["code"], "proof_accepted", result)
        wake = self.wake()
        self.assertEqual(wake._load_dispatch(self.first_id)["status"], "target_started")
        self.assertFalse(wake._has_unreconciled_unknown(wake._load_binding("desktop-binding"), wake._ledger_entries()))
        receipt = next(args for op, _identity, args in self.requests if op == "wake-relay-receipt")
        self.assertTrue(wake.relay_receipt(**receipt, relay_agent=self.relay_agent)["idempotent"])
        self.assertTrue(self.wake().relay_observe(self.first_id, stage="target_started",
                                                driver=CODEX_RELAY_DRIVER, relay_agent=self.relay_agent)["idempotent"])
        self.assertEqual(self.host.reconcile(self.first_id, self.target_proof(completed=True), ack)["code"], "proof_accepted")
        self.assertTrue(self.wake().relay_observe(self.first_id, stage="target_finished",
                                                driver=CODEX_RELAY_DRIVER, relay_agent=self.relay_agent)["idempotent"])
        self.assertEqual(len(self.reconciliation_rows()), 1)
        self.assertEqual(self.attempts(self.wake()), 1)
        claimed = self.wake().relay_claim(self.sibling_id, relay_agent=self.relay_agent, driver=CODEX_RELAY_DRIVER)
        self.assertEqual(claimed["code"], "wake_relay_claimed")
        self.assertEqual(self.attempts(self.wake()), 2)

    def test_native_host_proven_delivery_completed_first_releases_fence(self) -> None:
        self.prepare_native()
        result = self.host.reconcile(self.first_id, self.target_proof(completed=True), self.target_ack())
        self.assertEqual(result["code"], "proof_accepted", result)
        wake = self.wake()
        self.assertFalse(wake._has_unreconciled_unknown(wake._load_binding("desktop-binding"), wake._ledger_entries()))
        self.assertEqual(len(self.reconciliation_rows()), 1)
        self.assertEqual(wake.relay_claim(self.sibling_id, relay_agent=self.relay_agent,
                                         driver=CODEX_RELAY_DRIVER)["code"], "wake_relay_claimed")

    def test_native_ack_without_valid_host_proof_never_settles(self) -> None:
        self.prepare_native()
        ack = self.target_ack()
        proof = self.target_proof()
        for changed in (
            {"turn_id": self.prior_turn}, {"turn_id": None}, {"turn_id": "short-id"},
            {"native_thread_id": self.new_turn}, {"message_id": "foreign-message"},
            {"observed_at": self.clock[0] - 61}, {"thread_ended": True}, {"phase": "queued"},
        ):
            with self.subTest(proof=changed):
                result = self.host.reconcile(self.first_id, dict(proof, **changed), ack)
                self.assertEqual(result["code"], "invalid_proof", result)
                self.assert_fenced()
        for changed in ({"from_agent": SENDER}, {"message_id": "f" * 32}, {"ack_id": "e" * 32}):
            with self.subTest(ack=changed):
                result = self.host.reconcile(self.first_id, proof, dict(ack, **changed))
                self.assertIn(result["code"], {"ack_correlation_mismatch", "ack_missing"}, result)
                self.assert_fenced()
        self.assertEqual(self.reconciliation_rows(), [])

    def test_native_receipt_without_observation_stays_fenced(self) -> None:
        self.prepare_native()
        self.target_ack()
        wake = self.wake()
        wake.relay_receipt(self.first_id, status="delivered", turn_id=self.new_turn,
                           driver=CODEX_RELAY_DRIVER, relay_agent=self.relay_agent)
        self.assert_fenced()
        self.assertEqual(self.reconciliation_rows(), [])

    def test_native_unconfirmed_with_ack_and_observation_stays_fenced(self) -> None:
        self.prepare_native()
        self.target_ack()
        wake = self.wake()
        wake.relay_receipt(self.first_id, status="unconfirmed", turn_id=self.new_turn,
                           driver=CODEX_RELAY_DRIVER, relay_agent=self.relay_agent)
        for stage in ("target_started", "target_finished"):
            self.wake().relay_observe(self.first_id, stage=stage, driver=CODEX_RELAY_DRIVER,
                                     relay_agent=self.relay_agent)
        self.assertEqual(self.wake()._load_dispatch(self.first_id)["status"], "unconfirmed")
        self.assert_fenced()
        self.assertEqual(self.reconciliation_rows(), [])

    def test_native_error_with_ack_stays_fenced(self) -> None:
        self.prepare_native()
        self.target_ack()
        self.wake().relay_receipt(self.first_id, status="error", turn_id=self.new_turn,
                                 driver=CODEX_RELAY_DRIVER, relay_agent=self.relay_agent)
        with self.assertRaises(CoordError) as observed:
            self.wake().relay_observe(self.first_id, stage="target_started", driver=CODEX_RELAY_DRIVER,
                                     relay_agent=self.relay_agent)
        self.assertEqual(observed.exception.code, "relay_observation_not_ready")
        self.assert_fenced()
        self.assertEqual(self.reconciliation_rows(), [])

    def test_native_receipt_rejects_queued_missing_ack_and_foreign_relay(self) -> None:
        self.prepare_native()
        wake = self.wake()
        for fields, code in (
            ({"status": "queued", "turn_id": self.new_turn}, "invalid_native_turn_id"),
            ({"status": "delivered", "turn_id": self.new_turn}, "relay_ack_missing"),
            ({"status": "delivered", "turn_id": "short-id"}, "invalid_native_turn_id"),
            ({"status": "delivered", "turn_id": self.new_turn, "relay_agent": SENDER}, "identity_mismatch"),
        ):
            with self.subTest(fields=fields):
                with self.assertRaises(CoordError) as refused:
                    wake.relay_receipt(self.first_id, driver=CODEX_RELAY_DRIVER,
                                       **dict({"relay_agent": self.relay_agent}, **fields))
                self.assertEqual(refused.exception.code, code)
                self.assert_fenced()
        # The exact recipient ACK of the other dispatch is not this dispatch's evidence.
        self.office(self.target_agent).ack(wake._load_dispatch(self.sibling_id)["trigger_ref"])
        with self.assertRaises(CoordError) as foreign_ack:
            wake.relay_receipt(self.first_id, status="delivered", turn_id=self.new_turn,
                               driver=CODEX_RELAY_DRIVER, relay_agent=self.relay_agent)
        self.assertEqual(foreign_ack.exception.code, "relay_ack_missing")
        self.target_ack()
        wake.relay_receipt(self.first_id, status="delivered", turn_id=self.new_turn,
                           driver=CODEX_RELAY_DRIVER, relay_agent=self.relay_agent)
        with self.assertRaises(CoordError) as foreign_observe:
            wake.relay_observe(self.first_id, stage="target_started", driver=CODEX_RELAY_DRIVER,
                               relay_agent=SENDER)
        self.assertEqual(foreign_observe.exception.code, "identity_mismatch")
        self.assert_fenced()
        self.assertEqual(self.reconciliation_rows(), [])

    def test_native_settlement_ledger_failure_is_retried_by_same_receipt_after_restart(self) -> None:
        self.prepare_native()
        ack = self.target_ack()
        append = Wake._append_relay_stage

        def interrupted(current, dispatch, **kwargs):
            if kwargs["decision"] == "reconciled":
                raise OSError("synthetic settlement append interruption")
            return append(current, dispatch, **kwargs)

        with mock.patch.object(Wake, "_append_relay_stage", interrupted):
            result = self.host.reconcile(self.first_id, self.target_proof(), ack)
        self.assertEqual(result["code"], "observe_rejected", result)
        self.assertEqual(self.wake()._load_dispatch(self.first_id)["status"], "target_started")
        self.assert_fenced()
        receipt = next(args for op, _identity, args in self.requests if op == "wake-relay-receipt")
        self.assertTrue(self.wake().relay_receipt(**receipt, relay_agent=self.relay_agent)["idempotent"])
        self.assertEqual(self.host.reconcile(self.first_id, self.target_proof(), ack)["code"], "proof_accepted")
        self.assertEqual(len(self.reconciliation_rows()), 1)
        self.assertEqual(self.attempts(self.wake()), 1)
        self.wake().relay_claim(self.sibling_id, relay_agent=self.relay_agent, driver=CODEX_RELAY_DRIVER)
        self.assertEqual(self.attempts(self.wake()), 2)

    def test_native_proven_settlement_does_not_bypass_stop(self) -> None:
        self.prepare_native()
        self.assertEqual(self.host.reconcile(self.first_id, self.target_proof(), self.target_ack())["code"], "proof_accepted")
        wake = self.wake()
        wake.stop()
        with self.assertRaises(CoordError) as stopped:
            wake.relay_claim(self.sibling_id, relay_agent=self.relay_agent, driver=CODEX_RELAY_DRIVER)
        self.assertEqual(stopped.exception.code, "relay_dispatch_cancelled")
        self.assertEqual(stopped.exception.payload["reason"], "global_stop")
        self.assertEqual(self.attempts(self.wake()), 1)

    def test_native_settlement_refuses_a_changed_binding_route(self) -> None:
        self.prepare_native()
        self.target_ack()
        wake = self.wake()
        wake.relay_receipt(self.first_id, status="delivered", turn_id=self.new_turn,
                           driver=CODEX_RELAY_DRIVER, relay_agent=self.relay_agent)
        binding = wake._load_binding("desktop-binding")
        # Synthetic endpoint replacement: the delivery belongs to the original
        # target UUID and may not authorize a new endpoint at the same binding.
        binding["native_thread_id"] = self.new_turn
        binding["office_agent"] = "codex-test-" + self.new_turn
        binding["version"] += 1
        wake._atomic_replace_json(wake._binding_path("desktop-binding"), binding)
        wake.relay_observe(self.first_id, stage="target_started", driver=CODEX_RELAY_DRIVER,
                           relay_agent=self.relay_agent)
        self.assertTrue(wake._has_unreconciled_unknown(wake._load_binding("desktop-binding"), wake._ledger_entries()))
        with self.assertRaises(CoordError) as changed:
            wake.relay_claim(self.sibling_id, relay_agent=self.relay_agent, driver=CODEX_RELAY_DRIVER)
        self.assertEqual(changed.exception.code, "relay_dispatch_cancelled")
        self.assertEqual(changed.exception.payload["reason"], "binding_version_changed")
        self.assertEqual(self.attempts(self.wake()), 1)
        self.assertEqual(self.reconciliation_rows(), [])

    def test_zcode_proven_reply_receipt_and_observation_settle_only_once(self) -> None:
        self.target_agent = "zcode-ailab-sess_" + self.thread_id
        self.relay_agent = "zcode-ailab-sess_123e4567-e89b-12d3-a456-426614174003"
        wake = self.wake()
        wake.enroll(
            binding_id="desktop-binding", office_agent=self.target_agent,
            driver=ZCODE_RELAY_DRIVER, native_thread_id="sess_" + self.thread_id,
            workspace=str(self.workspace), enrolled_by="owner", allowed_wakers=[SENDER],
            allowed_triggers=["message"], wake_kinds=["question"], budget_per_day=6,
            expires_at=format_ts(self.clock[0] + 30 * 86400), owner_confirm=True,
        )
        wake.verify("desktop-binding", owner_confirm=True)
        for text in ("synthetic external CU input", "synthetic external CU sibling"):
            self.office(SENDER).send(self.target_agent, text, kind="question")
        self.assertEqual(wake.tick()["counts"]["dispatched"], 2)
        first, sibling = wake.relay_pending(driver=ZCODE_RELAY_DRIVER)["dispatches"]
        self.first_id = first["dispatch_id"]
        wake.relay_claim(self.first_id, relay_agent=self.relay_agent, driver=ZCODE_RELAY_DRIVER)
        wake.relay_native_begin(self.first_id, relay_agent=self.relay_agent, driver=ZCODE_RELAY_DRIVER)
        reply = self.office(self.target_agent).send(SENDER, "synthetic target reply", reply_to=first["trigger_ref"])
        self.assertEqual(reply["status"], "ok", reply)
        proof = owned_zcode_reply(self.home, self.first_id, self.relay_agent)
        self.assertEqual(proof["ack_id"], reply["id"])
        with self.assertRaises(CoordError) as foreign:
            wake.relay_receipt(self.first_id, status="delivered", message_id="f" * 32,
                               driver=ZCODE_RELAY_DRIVER, relay_agent=self.relay_agent)
        self.assertEqual(foreign.exception.code, "relay_ack_missing")
        receipt_args = dict(status="delivered", message_id=reply["id"],
                            detail="verified_correlated_office_reply",
                            driver=ZCODE_RELAY_DRIVER, relay_agent=self.relay_agent)
        wake.relay_receipt(self.first_id, **receipt_args)
        self.assertTrue(wake._has_unreconciled_unknown(wake._load_binding("desktop-binding"), wake._ledger_entries()))
        for stage in ("target_started", "target_finished"):
            self.wake().relay_observe(self.first_id, stage=stage, driver=ZCODE_RELAY_DRIVER,
                                     relay_agent=self.relay_agent)
        self.assertTrue(self.wake().relay_receipt(self.first_id, **receipt_args)["idempotent"])
        self.assertEqual(len(self.reconciliation_rows()), 1)
        self.assertEqual(self.attempts(self.wake()), 1)
        self.assertFalse(wake._has_unreconciled_unknown(wake._load_binding("desktop-binding"), wake._ledger_entries()))
        self.wake().relay_claim(sibling["dispatch_id"], relay_agent=self.relay_agent, driver=ZCODE_RELAY_DRIVER)
        self.assertEqual(self.attempts(self.wake()), 2)


if __name__ == "__main__":
    unittest.main()
