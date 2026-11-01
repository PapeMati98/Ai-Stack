"""Office Wake v2.2: an ``undelivered``/no-start native result with a message id.

Live UAT 2026-09-27: Claude Desktop ``send_message`` returned ``undelivered`` with
a message id and "no sign of starting within 20s ... can still pick it up later"
while the message WAS shown in the target session.  v2.1 stored a terminal
``error`` and offered no way to confirm or settle the delivery later.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from support import PACKAGE_ROOT  # noqa: F401  (puts the package on sys.path)

from coordfs.errors import CoordError
from coordfs.leases import format_ts, parse_ts
from coordfs.office import Office
from coordfs.office_cli import main as office_main
from coordfs.wake import Wake
from test_wake_relay import writable_mkdtemp

TARGET = "claude-desktop-target"
SENDER = "codex-relay-sender"
RELAY = "claude-desktop-relay"
LOCAL_ID = "local_123e4567-e89b-12d3-a456-426614174000"
LIVE_MESSAGE_ID = "f9778bd6-00a0-40f6-a228-616f44267b6e"
LIVE_DETAIL = "undelivered: no start within 20s (may be awaiting approval); may pick up later"
# The exact pre-v2.2 dispatch recorded by the live relay (dispatch ee9abc25...).
LEGACY_ERROR_DISPATCH = {
    "binding_id": "lead-claude-88900f5b",
    "binding_version": 2,
    "claimed_at": "2026-09-27T00:44:30Z",
    "created_at": "2026-09-26T23:37:19Z",
    "dispatch_id": "ee9abc2542b0b5d5016814e77d45fad5",
    "receipt_at": "2026-09-27T00:45:02Z",
    "receipt_detail": LIVE_DETAIL,
    "receipt_message_id": LIVE_MESSAGE_ID,
    "receipt_status": "error",
    "relay_agent": "claude-code-ailab-a2435e02-a13f-41e4-89d9-aa7133428de6",
    "status": "error",
    "target_local_id": "local_88900f5b-3914-40b8-bf9b-65be18db6e8c",
    "text": "[Office Wake] synthetic copy of the live prompt (office-wake-uat, wake-uat-1)",
    "trigger": "message",
    "trigger_ref": "d57adf102eb84ce693acc34aab27f0ed",
}


class WakeRelayUnconfirmedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(writable_mkdtemp(prefix="office-wake-unconfirmed-"))
        self.home = self.root / "coord"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True)
        self.clock = [datetime(2030, 1, 1, 17, 1).timestamp()]

    # -- helpers -----------------------------------------------------------
    def wake(self) -> Wake:
        return Wake(home=self.home, now=lambda: self.clock[0], driver_timeout_s=2.0)

    def office(self, agent: str) -> Office:
        # Fixture participants joined Office earlier (sessions claim presence at task entry),
        # so they are registered recipients. First contact: test_office_recipient_evidence.
        for participant in (TARGET, SENDER, RELAY):
            Office(agent_id=participant, home=self.home)._remember_canonical_agent()
        return Office(agent_id=agent, home=self.home, now=lambda: self.clock[0])

    @property
    def dispatch_dir(self) -> Path:
        return self.home / "office" / "wake" / "desktop-dispatch"

    def dispatch_files(self) -> list[Path]:
        return sorted(self.dispatch_dir.glob("*.json")) if self.dispatch_dir.is_dir() else []

    def read_dispatch(self, dispatch_id: str) -> dict:
        return json.loads((self.dispatch_dir / f"{dispatch_id}.json").read_text(encoding="utf-8"))

    def enroll_v2(self, wake: Wake) -> None:
        wake.enroll(
            binding_id="lead-desktop",
            office_agent=TARGET,
            driver="claude-desktop-relay",
            native_thread_id=LOCAL_ID,
            workspace=str(self.workspace),
            enrolled_by="owner",
            allowed_wakers=[SENDER],
            allowed_triggers=["message"],
            wake_kinds=["question", "handoff"],
            budget_per_day=20,
            max_attempts_per_trigger=1,
            expires_at=format_ts(self.clock[0] + 86400),
            owner_confirm=True,
            correlation_required=True,
        )
        wake.verify("lead-desktop", owner_confirm=True)

    def request(self, wake: Wake, corr: str) -> str:
        sent = self.office(TARGET).send(SENDER, "zlecenie wezla", kind="question")
        wake.expect(
            binding_id="lead-desktop",
            caller=TARGET,
            task_id="office-wake-uat",
            correlation_id=corr,
            counterpart=SENDER,
            request_message_id=sent["id"],
        )
        return sent["id"]

    def reply(self, request_id: str) -> str:
        return self.office(SENDER).send(TARGET, "wynik wezla", kind="note", reply_to=request_id)["id"]

    def unconfirmed_dispatch(self, wake: Wake) -> tuple[str, str]:
        """Reproduce the live run up to the native undelivered/no-start result."""
        self.enroll_v2(wake)
        reply_id = self.reply(self.request(wake, "wake-uat-1"))
        with mock.patch("coordfs.wake.subprocess.run") as process:
            self.assertEqual(wake.tick()["counts"]["dispatched"], 1)
        process.assert_not_called()
        [pending] = wake.relay_pending()["dispatches"]
        dispatch_id = pending["dispatch_id"]
        wake.relay_claim(dispatch_id, relay_agent=RELAY)
        self.clock[0] += 32  # the native tool waited 20 s before answering
        receipt = wake.relay_receipt(
            dispatch_id, status="unconfirmed", message_id=LIVE_MESSAGE_ID, detail=LIVE_DETAIL
        )
        self.assertEqual(receipt["dispatch"]["status"], "unconfirmed")
        return dispatch_id, reply_id

    def decisions(self, wake: Wake) -> list[tuple[str, str]]:
        return [(row["decision"], row["reason"]) for row in wake.ledger(limit=200)["entries"]]

    def cli(self, *arguments: str) -> tuple[int, dict]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = office_main(["--home", str(self.home), "--agent", RELAY, *arguments])
        return code, json.loads(stdout.getvalue())

    # -- 1. unconfirmed is neither an error nor resent ----------------------
    def test_live_undelivered_no_start_is_unconfirmed_and_never_resent(self) -> None:
        wake = self.wake()
        dispatch_id, _reply_id = self.unconfirmed_dispatch(wake)
        current = self.read_dispatch(dispatch_id)
        self.assertEqual(
            (current["status"], current["receipt_status"], current["receipt_message_id"]),
            ("unconfirmed", "unconfirmed", LIVE_MESSAGE_ID),
        )
        self.assertIn(("unconfirmed", "relay_unconfirmed"), self.decisions(wake))
        self.assertNotIn("error", [decision for decision, _ in self.decisions(wake)])

        # Later relay ticks: nothing pending, no second dispatch, no re-claim.
        for _ in range(3):
            self.clock[0] += 60
            with mock.patch("coordfs.wake.subprocess.run") as process:
                wake.tick()
            process.assert_not_called()
            self.assertEqual(wake.relay_pending()["count"], 0)
        self.assertEqual(len(self.dispatch_files()), 1)
        with self.assertRaises(CoordError) as fenced:
            wake.relay_claim(dispatch_id, relay_agent=RELAY)
        self.assertEqual(fenced.exception.code, "relay_dispatch_fenced")
        # The same receipt is idempotent; a different one is refused.
        again = wake.relay_receipt(
            dispatch_id, status="unconfirmed", message_id=LIVE_MESSAGE_ID, detail=LIVE_DETAIL
        )
        self.assertTrue(again["idempotent"])
        with self.assertRaises(CoordError) as conflict:
            wake.relay_receipt(dispatch_id, status="delivered", message_id=LIVE_MESSAGE_ID)
        self.assertEqual(conflict.exception.code, "relay_receipt_conflict")

    def test_running_observation_cannot_turn_unconfirmed_into_delivery(self) -> None:
        # Claude Desktop may report a warm target process as running while it
        # rejects the new message. This was observed in live Wake UAT-3.
        wake = self.wake()
        dispatch_id, _reply_id = self.unconfirmed_dispatch(wake)
        observed = wake.relay_observe(dispatch_id, stage="target_started")
        self.assertEqual(observed["dispatch"]["status"], "unconfirmed")
        self.assertEqual(observed["dispatch"]["observed_stages"], ["target_started"])
        self.assertNotIn("settled", observed)
        self.assertNotIn("correlation_receipt", observed)
        self.assertNotIn("delivered", [decision for decision, _ in self.decisions(wake)])
        self.assertNotIn("relay_delivered", [row["state"] for row in wake.correlations.receipts("wake-uat-1")])
        self.assertTrue(wake.relay_observe(dispatch_id, stage="target_started")["idempotent"])

    def test_unconfirmed_blocks_new_wakes_until_owner_reconciles_it(self) -> None:
        wake = self.wake()
        dispatch_id, reply_id = self.unconfirmed_dispatch(wake)
        second_reply = self.reply(self.request(wake, "wake-uat-2"))
        with mock.patch("coordfs.wake.subprocess.run"):
            blocked = wake.tick()
        self.assertIn("deferred", [row["decision"] for row in blocked["decisions"]])
        self.assertEqual(len(self.dispatch_files()), 1)  # fail-closed: no second send

        observed = wake.relay_observe(dispatch_id, stage="target_started")
        self.assertEqual(observed["dispatch"]["status"], "unconfirmed")
        self.assertNotIn("settled", observed)
        finished = wake.relay_observe(dispatch_id, stage="target_finished")
        self.assertEqual(finished["dispatch"]["status"], "unconfirmed")
        self.assertNotIn("settled", finished)
        self.assertTrue(wake.relay_observe(dispatch_id, stage="target_started")["idempotent"])
        with mock.patch("coordfs.wake.subprocess.run"):
            still_blocked = wake.tick()
        self.assertEqual(still_blocked["counts"].get("dispatched", 0), 0)
        self.assertEqual(len(self.dispatch_files()), 1)

        # A human owner verifies the actual transcript before resolving the
        # uncertain native result. Observing a running process alone cannot.
        reconciled = wake.relay_reconcile(dispatch_id, outcome="delivered", owner_confirm=True)
        self.assertEqual(reconciled["correlation_receipt"], "recorded")

        # Released: the deferred second reply is dispatched exactly once, the first never again.
        with mock.patch("coordfs.wake.subprocess.run"):
            released = wake.tick()
        self.assertEqual(released["counts"].get("dispatched"), 1)
        files = self.dispatch_files()
        self.assertEqual(len(files), 2)
        triggers = sorted(json.loads(path.read_text(encoding="utf-8"))["trigger_ref"] for path in files)
        self.assertEqual(triggers, sorted([reply_id, second_reply]))

        # Correlation: the reserved wake counted once, consume still works.
        correlation = wake.correlations.load("wake-uat-1")
        self.assertEqual((correlation["wakes"], correlation["woken"]), (1, [reply_id]))
        states = [row["state"] for row in wake.correlations.receipts("wake-uat-1")]
        self.assertEqual(states, ["expected", "reserved", "dispatched", "relay_delivered"])
        consumed = wake.correlations.consume(
            "wake-uat-1", caller=TARGET, trigger_message_id=reply_id
        )
        self.assertEqual(consumed["correlation"]["consumed"], [reply_id])
        self.assertEqual(wake.correlations.load("wake-uat-1")["wakes"], 1)

    def test_target_finished_first_does_not_settle_unconfirmed(self) -> None:
        wake = self.wake()
        dispatch_id, _ = self.unconfirmed_dispatch(wake)
        observed = wake.relay_observe(dispatch_id, stage="target_finished")
        self.assertEqual(observed["dispatch"]["status"], "unconfirmed")
        self.assertNotIn("settled", observed)
        self.assertNotIn("delivered", [decision for decision, _ in self.decisions(wake)])

    def test_legacy_unconfirmed_observation_stays_fenced_until_reconciled(self) -> None:
        wake = self.wake()
        dispatch_id, _reply_id = self.unconfirmed_dispatch(wake)
        legacy = self.read_dispatch(dispatch_id)
        legacy.update({"status": "target_started", "observed_stages": ["target_started"],
                       "target_started_at": format_ts(self.clock[0])})
        wake._dispatch_path(dispatch_id).write_text(json.dumps(legacy), encoding="utf-8")

        second_reply = self.reply(self.request(wake, "wake-uat-2"))
        with mock.patch("coordfs.wake.subprocess.run"):
            blocked = wake.tick()
        self.assertEqual(blocked["counts"].get("dispatched", 0), 0)
        self.assertEqual(len(self.dispatch_files()), 1)

        reconciled = wake.relay_reconcile(dispatch_id, outcome="not_delivered", owner_confirm=True)
        self.assertEqual(reconciled["reconciled_from"], "target_started")
        with mock.patch("coordfs.wake.subprocess.run"):
            released = wake.tick()
        self.assertEqual(released["counts"].get("dispatched"), 1)
        self.assertIn(second_reply, [self.read_dispatch(path.stem)["trigger_ref"]
                                     for path in self.dispatch_files()])

    def test_unconfirmed_without_observation_times_out_to_delivery_unknown(self) -> None:
        wake = self.wake()
        dispatch_id, reply_id = self.unconfirmed_dispatch(wake)
        self.clock[0] += 30 * 60 - 60
        wake.relay_pending()
        self.assertEqual(self.read_dispatch(dispatch_id)["status"], "unconfirmed")
        self.clock[0] += 2 * 60
        self.assertEqual(wake.relay_pending()["count"], 0)
        current = self.read_dispatch(dispatch_id)
        self.assertEqual(
            (current["status"], current["unknown_reason"], current["receipt_status"]),
            ("delivery_unknown", "unconfirmed_observation_timeout", "unconfirmed"),
        )
        self.assertIn(("delivery_unknown", "unconfirmed_observation_timeout"), self.decisions(wake))
        self.assertNotIn("error", [decision for decision, _ in self.decisions(wake)])
        # One-way: a late observation no longer settles it; only the owner does.
        with self.assertRaises(CoordError) as late:
            wake.relay_observe(dispatch_id, stage="target_started")
        self.assertEqual(late.exception.code, "relay_observation_not_ready")
        blocked = wake.tick()
        self.assertEqual(blocked["counts"].get("dispatched", 0), 0)
        reconciled = wake.relay_reconcile(dispatch_id, outcome="delivered", owner_confirm=True)
        self.assertEqual(
            (reconciled["dispatch"]["status"], reconciled["reconciled_from"]),
            ("reconciled", "delivery_unknown"),
        )
        self.assertEqual(len(self.dispatch_files()), 1)
        self.assertEqual(wake.correlations.load("wake-uat-1")["wakes"], 1)
        wake.correlations.consume("wake-uat-1", caller=TARGET, trigger_message_id=reply_id)

    def test_unconfirmed_requires_the_native_message_id(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        self.reply(self.request(wake, "wake-uat-1"))
        with mock.patch("coordfs.wake.subprocess.run"):
            wake.tick()
        [pending] = wake.relay_pending()["dispatches"]
        wake.relay_claim(pending["dispatch_id"], relay_agent=RELAY)
        with self.assertRaises(CoordError) as missing:
            wake.relay_receipt(pending["dispatch_id"], status="unconfirmed", message_id="none")
        self.assertEqual(missing.exception.code, "relay_unconfirmed_requires_message_id")
        self.assertEqual(self.read_dispatch(pending["dispatch_id"])["status"], "claimed")

    # -- 3. owner reconcile of unconfirmed and legacy error records ---------
    def test_owner_reconciles_unconfirmed_not_delivered_without_resend(self) -> None:
        wake = self.wake()
        dispatch_id, reply_id = self.unconfirmed_dispatch(wake)
        with self.assertRaises(CoordError) as unconfirmed_owner:
            wake.relay_reconcile(dispatch_id, outcome="not_delivered", owner_confirm=False)
        self.assertEqual(unconfirmed_owner.exception.code, "owner_confirmation_required")
        reconciled = wake.relay_reconcile(dispatch_id, outcome="not_delivered", owner_confirm=True)
        self.assertEqual(
            (reconciled["dispatch"]["status"], reconciled["reconciled_from"], reconciled["correlation_receipt"]),
            ("reconciled", "unconfirmed", "recorded"),
        )
        with mock.patch("coordfs.wake.subprocess.run"):
            wake.tick()
        self.assertEqual(wake.relay_pending()["count"], 0)
        self.assertEqual(len(self.dispatch_files()), 1)
        correlation = wake.correlations.load("wake-uat-1")
        self.assertEqual((correlation["wakes"], correlation["woken"]), (1, [reply_id]))
        self.assertEqual(
            wake.correlations.receipts("wake-uat-1")[-1]["state"], "relay_not_delivered"
        )

    def test_owner_reconciles_the_live_legacy_error_record_without_resend(self) -> None:
        self.clock[0] = parse_ts("2026-09-27T01:30:00Z")
        wake = self.wake()
        self.dispatch_dir.mkdir(parents=True)
        path = self.dispatch_dir / f"{LEGACY_ERROR_DISPATCH['dispatch_id']}.json"
        path.write_text(json.dumps(LEGACY_ERROR_DISPATCH), encoding="utf-8")
        dispatch_id = LEGACY_ERROR_DISPATCH["dispatch_id"]

        with self.assertRaises(CoordError) as legacy_observe:
            wake.relay_observe(dispatch_id, stage="target_started")
        self.assertEqual(legacy_observe.exception.code, "relay_observation_not_ready")
        with self.assertRaises(CoordError) as no_owner:
            wake.relay_reconcile(dispatch_id, outcome="delivered")
        self.assertEqual(no_owner.exception.code, "owner_confirmation_required")

        reconciled = wake.relay_reconcile(dispatch_id, outcome="delivered", owner_confirm=True)
        current = self.read_dispatch(dispatch_id)
        self.assertEqual(
            (current["status"], current["reconciliation_outcome"], current["unknown_reason"]),
            ("reconciled", "delivered", "receipt_error"),
        )
        self.assertEqual((current["receipt_status"], current["receipt_message_id"]), ("error", LIVE_MESSAGE_ID))
        self.assertEqual((reconciled["reconciled_from"], reconciled["correlation_receipt"]), ("error", "none"))
        row = wake.ledger(limit=10)["entries"][-1]
        self.assertEqual(
            (row["decision"], row["reason"], row["reconciled_from"], row["message_id"]),
            ("reconciled", "delivered", "error", LIVE_MESSAGE_ID),
        )
        # Idempotent repeat, conflicting outcome refused, and nothing was ever resent.
        self.assertTrue(
            wake.relay_reconcile(dispatch_id, outcome="delivered", owner_confirm=True)["idempotent"]
        )
        with self.assertRaises(CoordError) as conflict:
            wake.relay_reconcile(dispatch_id, outcome="not_delivered", owner_confirm=True)
        self.assertEqual(conflict.exception.code, "relay_reconciliation_conflict")
        self.assertEqual(wake.relay_pending()["count"], 0)
        self.assertEqual(self.dispatch_files(), [path])
        self.assertEqual(
            [entry["decision"] for entry in wake.ledger(limit=10)["entries"]], ["reconciled"]
        )

    def test_error_without_native_message_id_stays_as_recorded(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        self.reply(self.request(wake, "wake-uat-1"))
        with mock.patch("coordfs.wake.subprocess.run"):
            wake.tick()
        [pending] = wake.relay_pending()["dispatches"]
        wake.relay_claim(pending["dispatch_id"], relay_agent=RELAY)
        wake.relay_receipt(pending["dispatch_id"], status="error", message_id="none", detail="tool error")
        with self.assertRaises(CoordError) as refused:
            wake.relay_reconcile(pending["dispatch_id"], outcome="delivered", owner_confirm=True)
        self.assertEqual(refused.exception.code, "relay_dispatch_not_unknown")
        self.assertEqual(self.read_dispatch(pending["dispatch_id"])["status"], "error")

    # -- CLI -------------------------------------------------------------
    def test_cli_records_unconfirmed_receipt_and_owner_reconcile(self) -> None:
        wake = self.wake()
        self.enroll_v2(wake)
        self.reply(self.request(wake, "wake-uat-1"))
        with mock.patch("coordfs.wake.subprocess.run"):
            wake.tick()
        [pending] = wake.relay_pending()["dispatches"]
        dispatch_id = pending["dispatch_id"]
        self.assertEqual(self.cli("wake-relay-claim", "--dispatch-id", dispatch_id, "--relay-agent", RELAY)[0], 0)
        code, receipt = self.cli(
            "wake-relay-receipt", "--dispatch-id", dispatch_id, "--status", "unconfirmed",
            "--message-id", LIVE_MESSAGE_ID, "--detail", LIVE_DETAIL,
        )
        self.assertEqual((code, receipt["dispatch"]["status"]), (0, "unconfirmed"))
        code, reconciled = self.cli(
            "wake-relay-reconcile", "--dispatch-id", dispatch_id, "--outcome", "delivered", "--owner-confirm"
        )
        self.assertEqual((code, reconciled["reconciled_from"]), (0, "unconfirmed"))

    # -- 4. a transient correlation write failure is retried, never resent ---
    def assert_settled_once(self, wake: Wake, dispatch_id: str, reply_id: str, state: str) -> None:
        correlation = wake.correlations.load("wake-uat-1")
        self.assertEqual((correlation["wakes"], correlation["woken"]), (1, [reply_id]))
        settled = [
            row
            for row in wake.correlations.receipts("wake-uat-1")
            if row["state"] == state and row.get("dispatch_id") == dispatch_id
        ]
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["trigger_message_id"], reply_id)
        self.assertEqual(len(self.dispatch_files()), 1)
        self.assertEqual(wake.relay_pending()["count"], 0)

    def failing_receipt_once(self):
        from coordfs.wake_correlation import Correlations

        real_append = Correlations._append_receipt
        calls = {"failed": 0}

        def append(correlations, correlation, state, **fields):
            if state.startswith("relay_") and not calls["failed"]:
                calls["failed"] += 1
                raise CoordError("synthetic receipt write failure", code="synthetic_io")
            return real_append(correlations, correlation, state, **fields)

        return mock.patch.object(Correlations, "_append_receipt", append), calls

    def test_observation_never_attempts_a_delivery_receipt(self) -> None:
        wake = self.wake()
        dispatch_id, reply_id = self.unconfirmed_dispatch(wake)
        patch, calls = self.failing_receipt_once()
        with patch:
            observed = wake.relay_observe(dispatch_id, stage="target_started")
        self.assertEqual(calls["failed"], 0)
        self.assertEqual(observed["dispatch"]["status"], "unconfirmed")
        self.assertNotIn("correlation_receipt", observed)
        states = [row["state"] for row in wake.correlations.receipts("wake-uat-1")]
        self.assertNotIn("relay_delivered", states)

        with mock.patch("coordfs.wake.subprocess.run") as process:
            retried = wake.relay_observe(dispatch_id, stage="target_started")
        process.assert_not_called()
        self.assertTrue(retried["idempotent"])
        self.assertNotIn("correlation_receipt", retried)

        finished = wake.relay_observe(dispatch_id, stage="target_finished")
        self.assertNotIn("correlation_receipt", finished)
        delivered = [
            row
            for row in wake.ledger(limit=200)["entries"]
            if row["decision"] == "delivered" and row.get("dispatch_id") == dispatch_id
        ]
        self.assertEqual(delivered, [])
        self.assertEqual(wake.correlations.load("wake-uat-1")["woken"], [reply_id])

    def test_reconcile_retries_a_failed_correlation_receipt_exactly_once(self) -> None:
        wake = self.wake()
        dispatch_id, reply_id = self.unconfirmed_dispatch(wake)
        patch, calls = self.failing_receipt_once()
        with patch:
            reconciled = wake.relay_reconcile(dispatch_id, outcome="not_delivered", owner_confirm=True)
        self.assertEqual(calls["failed"], 1)
        self.assertEqual(
            (reconciled["dispatch"]["status"], reconciled["correlation_receipt"]),
            ("reconciled", "unavailable"),
        )
        with mock.patch("coordfs.wake.subprocess.run") as process:
            retried = wake.relay_reconcile(dispatch_id, outcome="not_delivered", owner_confirm=True)
            wake.tick()
        process.assert_not_called()
        self.assertEqual((retried["idempotent"], retried["correlation_receipt"]), (True, "recorded"))
        self.assert_settled_once(wake, dispatch_id, reply_id, "relay_not_delivered")
        again = wake.relay_reconcile(dispatch_id, outcome="not_delivered", owner_confirm=True)
        self.assertEqual((again["idempotent"], again["correlation_receipt"]), (True, "already_recorded"))
        self.assert_settled_once(wake, dispatch_id, reply_id, "relay_not_delivered")
        reconciled_rows = [
            row for row in wake.ledger(limit=200)["entries"] if row["decision"] == "reconciled"
        ]
        self.assertEqual(len(reconciled_rows), 1)

    def test_observation_does_not_write_correlation_state(self) -> None:
        from coordfs.wake_correlation import Correlations

        wake = self.wake()
        dispatch_id, _reply_id = self.unconfirmed_dispatch(wake)
        with mock.patch.object(
            Correlations, "_write", side_effect=CoordError("synthetic", code="synthetic_io")
        ):
            observed = wake.relay_observe(dispatch_id, stage="target_finished")
        self.assertNotIn("correlation_receipt", observed)
        retried = wake.relay_observe(dispatch_id, stage="target_finished")
        self.assertTrue(retried["idempotent"])
        self.assertNotIn("correlation_receipt", retried)
        self.assertNotIn("relay_delivered", [row["state"] for row in wake.correlations.receipts("wake-uat-1")])

    # -- 5. read-only STOP status ------------------------------------------
    def test_wake_stop_status_is_read_only_false_true_and_error(self) -> None:
        code, inactive = self.cli("wake-stop-status")
        self.assertEqual(
            (code, inactive),
            (0, {"status": "ok", "code": "wake_stop_status", "stop_active": False, "since": None}),
        )
        self.assertFalse((self.home / "office" / "wake").exists())  # no writes at all

        stopped_at = format_ts(self.clock[0])
        self.wake().stop()
        wake_root = self.home / "office" / "wake"
        before = sorted(path.name for path in wake_root.rglob("*"))
        code, active = self.cli("wake-stop-status")
        self.assertEqual(
            (code, active),
            (0, {"status": "ok", "code": "wake_stop_status", "stop_active": True, "since": stopped_at}),
        )
        self.assertEqual(sorted(path.name for path in wake_root.rglob("*")), before)

        stop_path = wake_root / "STOP"
        real_stat = Path.stat

        def failing_stat(path: Path, *args, **kwargs):
            if Path(path) == stop_path:
                raise PermissionError("synthetic denied")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", failing_stat):
            code, unavailable = self.cli("wake-stop-status")
        self.assertEqual(
            (code, unavailable),
            (1, {"status": "error", "code": "wake_stop_unavailable", "stop_active": None}),
        )

    def test_wake_stop_status_stat_ok_read_denied_is_unavailable(self) -> None:
        self.wake().stop()
        stop_path = self.home / "office" / "wake" / "STOP"
        real_open = Path.open

        def failing_open(path: Path, *args, **kwargs):
            if Path(path) == stop_path:
                raise PermissionError("synthetic denied")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", failing_open):
            code, unavailable = self.cli("wake-stop-status")
        self.assertEqual(
            (code, unavailable),
            (1, {"status": "error", "code": "wake_stop_unavailable", "stop_active": None}),
        )

    def test_wake_stop_status_non_regular_stop_is_unavailable_without_reading(self) -> None:
        stop_path = self.home / "office" / "wake" / "STOP"
        stop_path.mkdir(parents=True)
        code, result = self.cli("wake-stop-status")
        self.assertEqual(
            (code, result),
            (1, {"status": "error", "code": "wake_stop_unavailable", "stop_active": None}),
        )

    @unittest.skipUnless(hasattr(__import__("os"), "mkfifo"), "FIFO needs POSIX")
    def test_wake_stop_status_fifo_does_not_block(self) -> None:
        import os
        import threading
        stop_path = self.home / "office" / "wake" / "STOP"
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(stop_path)
        box = {}
        worker = threading.Thread(target=lambda: box.setdefault("r", self.wake().stop_status()), daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), "stop_status blocked on a FIFO")
        self.assertEqual(box["r"]["code"], "wake_stop_unavailable")

    def test_wake_stop_status_malformed_content_falls_back_to_mtime(self) -> None:
        self.wake().stop()
        stop_path = self.home / "office" / "wake" / "STOP"
        stop_path.write_text("not json", encoding="utf-8")
        code, active = self.cli("wake-stop-status")
        self.assertEqual((code, active["status"], active["stop_active"]), (0, "ok", True))
        self.assertIsInstance(active["since"], str)


if __name__ == "__main__":
    unittest.main()
