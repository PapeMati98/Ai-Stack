"""Contract tests for the Claude Desktop Office Wake relay driver."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from support import PACKAGE_ROOT

from coordfs.errors import CoordError
from coordfs.leases import format_ts
from coordfs.office import Office
from coordfs.leases import parse_ts
from coordfs.office_cli import main as office_main
from coordfs.wake import (
    CHILD_TERMINAL_PROMPT_TEMPLATE,
    SCHEDULE_PROMPT_TEMPLATE,
    WAKE_PROMPT_TEMPLATE,
    Wake,
)


TARGET = "claude-desktop-target"
SENDER = "codex-relay-sender"
RELAY = "claude-desktop-relay"
LOCAL_ID = "local_123e4567-e89b-12d3-a456-426614174000"


def writable_mkdtemp(prefix: str) -> str:
    """Keep mkdtemp usable with Python 3.13 Windows sandbox ACLs."""
    if os.name != "nt":
        return tempfile.mkdtemp(prefix=prefix)
    original_mkdir = os.mkdir

    def inherited_acl(path, _mode=0o777, *args, **kwargs):
        return original_mkdir(path, 0o777, *args, **kwargs)

    with mock.patch.object(tempfile._os, "mkdir", inherited_acl):
        return tempfile.mkdtemp(prefix=prefix)


class WakeRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(writable_mkdtemp(prefix="office-wake-relay-"))
        self.home = self.root / "coord"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True)
        self.clock = [datetime(2030, 1, 1, 17, 1).timestamp()]

    def wake(self, *, home: Path | None = None) -> Wake:
        return Wake(home=home or self.home, now=lambda: self.clock[0], driver_timeout_s=2.0)

    def office(self, agent: str, *, home: Path | None = None) -> Office:
        # Fixture participants joined Office earlier (sessions claim presence at task entry),
        # so they are registered recipients in every home a test uses. Genuine first contact
        # is covered by test_office_recipient_evidence (refused unless new_recipient).
        for participant in (TARGET, SENDER, RELAY):
            Office(agent_id=participant, home=home or self.home)._remember_canonical_agent()
        return Office(agent_id=agent, home=home or self.home, now=lambda: self.clock[0])

    def enroll(
        self,
        wake: Wake,
        *,
        binding_id: str = "desktop-binding",
        triggers: list[str] | None = None,
        native_thread_id: str = LOCAL_ID,
        budget: int = 6,
    ) -> None:
        wake.enroll(
            binding_id=binding_id,
            office_agent=TARGET,
            driver="claude-desktop-relay",
            native_thread_id=native_thread_id,
            workspace=str(self.workspace),
            enrolled_by="owner",
            allowed_wakers=[SENDER],
            allowed_triggers=triggers or ["schedule"],
            wake_kinds=["question", "handoff"],
            budget_per_day=budget,
            expires_at=format_ts(self.clock[0] + 30 * 86400),
            owner_confirm=True,
        )
        wake.verify(binding_id, owner_confirm=True)

    def add_due_schedule(
        self,
        wake: Wake,
        *,
        binding_id: str = "desktop-binding",
        schedule_id: str = "desktop-daily",
        task_text: str = "sprawdz syntetyczna kolejke i zglos wynik wlascicielowi",
    ) -> None:
        wake.add_schedule(
            schedule_id=schedule_id,
            binding_id=binding_id,
            time="17:00",
            days="daily",
            task_text=task_text,
            created_by="owner",
            owner_confirm=True,
        )

    def dispatch_files(self, *, home: Path | None = None) -> list[Path]:
        directory = (home or self.home) / "office" / "wake" / "desktop-dispatch"
        return sorted(directory.glob("*.json")) if directory.is_dir() else []

    def create_schedule_dispatch(self, wake: Wake) -> dict:
        self.enroll(wake)
        self.add_due_schedule(wake)
        with mock.patch("coordfs.wake.subprocess.run") as process:
            result = wake.tick()
        process.assert_not_called()
        self.assertEqual(result["counts"]["dispatched"], 1)
        path = self.dispatch_files()[0]
        return json.loads(path.read_text(encoding="utf-8"))

    def test_schedule_creates_one_atomic_dispatch_with_exact_prompt_and_no_process(self) -> None:
        wake = self.wake()
        task = "sprawdz syntetyczna kolejke i zglos wynik wlascicielowi"
        self.enroll(wake)
        self.add_due_schedule(wake, task_text=task)

        with mock.patch("coordfs.wake.subprocess.run") as process:
            first = wake.tick()
            second = wake.tick()

        process.assert_not_called()
        self.assertEqual(first["counts"]["dispatched"], 1)
        self.assertEqual(second["decisions"], [])
        files = self.dispatch_files()
        self.assertEqual(len(files), 1)
        dispatch = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(
            set(dispatch),
            {
                "dispatch_id",
                "binding_id",
                "binding_version",
                "target_local_id",
                "text",
                "trigger",
                "trigger_ref",
                "created_at",
                "status",
                "deadline_at",
            },
        )
        self.assertEqual(dispatch["target_local_id"], LOCAL_ID)
        self.assertEqual(dispatch["status"], "pending")
        self.assertEqual(dispatch["trigger"], "schedule")
        self.assertEqual(
            dispatch["text"],
            SCHEDULE_PROMPT_TEMPLATE.format(schedule_id="desktop-daily", task_text=task),
        )
        entry = wake.ledger(limit=10)["entries"][-1]
        self.assertEqual((entry["decision"], entry["reason"]), ("dispatched", "relay_pending"))
        self.assertEqual(entry["dispatch_id"], dispatch["dispatch_id"])

    def test_message_dispatch_uses_the_fixed_wake_template_and_counts_budget(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message", "schedule"], budget=1)
        sent = self.office(SENDER).send(TARGET, "UNTRUSTED BODY", kind="question")
        with mock.patch("coordfs.wake.subprocess.run") as process:
            first = wake.tick()
        process.assert_not_called()
        self.assertEqual(first["counts"]["dispatched"], 1)
        dispatch = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        expected = WAKE_PROMPT_TEMPLATE.format(sender=SENDER, message_id=sent["id"])
        self.assertEqual(dispatch["text"], expected)
        self.assertNotIn("UNTRUSTED BODY", dispatch["text"])

        # Queueing alone is free; the daily cap applies after real relay uptake.
        wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        self.add_due_schedule(wake)
        blocked = wake.tick()
        self.assertIn(
            "daily_budget_exhausted",
            [row["reason"] for row in blocked["decisions"]],
        )
        self.assertEqual(len(self.dispatch_files()), 1)

    def test_child_terminal_dispatch_uses_the_fixed_child_template_without_process(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["child-terminal"])
        result_path = self.root / "child-run" / "state.json"
        result_path.parent.mkdir()
        result_path.write_text(json.dumps({"status": "COMPLETED"}), encoding="utf-8")
        run_id = "synthetic-child-run"
        event_id = hashlib.sha256((run_id + "COMPLETED").encode("utf-8")).hexdigest()
        event = {
            "schema": "clawops.child-terminal/v1",
            "event_id": event_id,
            "run_id": run_id,
            "source": "agent-graph",
            "terminal_state": "COMPLETED",
            "result_ref": str(result_path.resolve()),
            "parent_agent": TARGET,
            "parent_session": None,
            "parent_harness": "claude-desktop",
            "workspace": str(self.workspace.resolve()),
            "started_at": format_ts(self.clock[0] - 60),
            "observed_at": format_ts(self.clock[0]),
        }
        body = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
        self.office(SENDER).send(None, body, channel="watch-events", kind="note")
        self.office(SENDER).send(TARGET, body, kind="note")

        with mock.patch("coordfs.wake.subprocess.run") as process:
            ticked = wake.tick()

        process.assert_not_called()
        self.assertEqual(ticked["counts"]["dispatched"], 1)
        dispatch = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.assertEqual(dispatch["trigger"], "child-terminal")
        self.assertEqual(dispatch["trigger_ref"], event_id)
        self.assertEqual(
            dispatch["text"],
            CHILD_TERMINAL_PROMPT_TEMPLATE.format(
                run_id=run_id,
                terminal_state="COMPLETED",
                result_ref=str(result_path.resolve()),
            ),
        )

    def test_pending_claim_and_delivered_receipt_are_fenced_and_ledgered(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        pending = wake.relay_pending()
        self.assertEqual([row["dispatch_id"] for row in pending["dispatches"]], [dispatch["dispatch_id"]])

        claimed = wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        self.assertEqual(claimed["dispatch"]["status"], "claimed")
        with self.assertRaises(CoordError) as second_claim:
            wake.relay_claim(dispatch["dispatch_id"], relay_agent="claude-desktop-other")
        self.assertEqual(second_claim.exception.code, "relay_dispatch_fenced")

        receipt = wake.relay_receipt(
            dispatch["dispatch_id"], status="delivered", message_id="message-001"
        )
        self.assertEqual(receipt["dispatch"]["status"], "delivered")
        decisions = [row["decision"] for row in wake.ledger(limit=20)["entries"]]
        self.assertEqual(decisions, ["dispatched", "claimed", "delivered"])

    def test_receipt_requires_claim_and_same_message_id_is_idempotent(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        with self.assertRaises(CoordError) as unclaimed:
            wake.relay_receipt(
                dispatch["dispatch_id"], status="delivered", message_id="message-002"
            )
        self.assertEqual(unclaimed.exception.code, "relay_dispatch_not_claimed")

        wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        first = wake.relay_receipt(
            dispatch["dispatch_id"], status="delivered", message_id="message-002"
        )
        repeated = wake.relay_receipt(
            dispatch["dispatch_id"], status="delivered", message_id="message-002"
        )
        self.assertFalse(first["idempotent"])
        self.assertTrue(repeated["idempotent"])
        delivered = [
            row for row in wake.ledger(limit=20)["entries"] if row["decision"] == "delivered"
        ]
        self.assertEqual(len(delivered), 1)

    def test_queued_receipt_can_be_observed_through_target_finish(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        wake.relay_receipt(
            dispatch["dispatch_id"],
            status="queued",
            message_id="message-queued",
            detail="target was busy",
        )
        started = wake.relay_observe(dispatch["dispatch_id"], stage="target_started")
        finished = wake.relay_observe(dispatch["dispatch_id"], stage="target_finished")
        self.assertEqual(started["dispatch"]["status"], "target_started")
        self.assertEqual(finished["dispatch"]["status"], "target_finished")
        decisions = [row["decision"] for row in wake.ledger(limit=20)["entries"]]
        self.assertEqual(
            decisions,
            ["dispatched", "claimed", "queued", "target_started", "target_finished"],
        )

    def test_stop_after_dispatch_cancels_pending_before_listing(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        wake.stop()
        pending = wake.relay_pending()
        self.assertEqual(pending["dispatches"], [])
        current = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.assertEqual((current["status"], current["cancel_reason"]), ("cancelled", "global_stop"))
        cancelled = wake.ledger(limit=20)["entries"][-1]
        self.assertEqual((cancelled["decision"], cancelled["dispatch_id"]), ("cancelled", dispatch["dispatch_id"]))

    def test_revoked_binding_cancels_pending_before_listing(self) -> None:
        wake = self.wake()
        self.create_schedule_dispatch(wake)
        wake.revoke("desktop-binding")
        self.assertEqual(wake.relay_pending()["dispatches"], [])
        current = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.assertEqual((current["status"], current["cancel_reason"]), ("cancelled", "binding_revoked"))

    def test_pending_older_than_three_hours_expires_and_is_not_redispatched(self) -> None:
        wake = self.wake()
        self.enroll(wake)
        wake.add_schedule(
            schedule_id="desktop-daily",
            binding_id="desktop-binding",
            time="17:00",
            days="daily",
            task_text="sprawdz syntetyczna kolejke i zglos wynik wlascicielowi",
            created_by="owner",
            grace_minutes=600,
            owner_confirm=True,
        )
        with mock.patch("coordfs.wake.subprocess.run"):
            self.assertEqual(wake.tick()["counts"]["dispatched"], 1)
        dispatch = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.clock[0] += 3 * 60 * 60
        self.assertEqual(len(wake.relay_pending()["dispatches"]), 1)

        self.clock[0] += 60
        self.assertEqual(wake.relay_pending()["dispatches"], [])
        current = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.assertEqual((current["status"], current["cancel_reason"]), ("cancelled", "dispatch_expired"))
        cancelled = wake.ledger(limit=20)["entries"][-1]
        self.assertEqual(
            (cancelled["decision"], cancelled["reason"], cancelled["dispatch_id"]),
            ("cancelled", "dispatch_expired", dispatch["dispatch_id"]),
        )
        with self.assertRaises(CoordError) as fenced:
            wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        self.assertEqual(fenced.exception.code, "relay_dispatch_fenced")
        with mock.patch("coordfs.wake.subprocess.run") as process:
            again = wake.tick()
        process.assert_not_called()
        self.assertEqual(again["counts"].get("dispatched", 0), 0)
        self.assertEqual(len(self.dispatch_files()), 1)

    def test_schedule_dispatch_expires_at_the_end_of_the_grace_window(self) -> None:
        wake = self.wake()
        self.enroll(wake)
        self.add_due_schedule(wake)
        self.clock[0] = datetime(2030, 1, 1, 19, 59).timestamp()
        with mock.patch("coordfs.wake.subprocess.run"):
            self.assertEqual(wake.tick()["counts"]["dispatched"], 1)
        dispatch = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.assertEqual(
            parse_ts(dispatch["deadline_at"]), datetime(2030, 1, 1, 20, 0).timestamp()
        )
        self.clock[0] = datetime(2030, 1, 1, 19, 59, 59).timestamp()
        self.assertEqual(len(wake.relay_pending()["dispatches"]), 1)

        self.clock[0] = datetime(2030, 1, 1, 20, 0, 1).timestamp()
        self.assertEqual(wake.relay_pending()["dispatches"], [])
        current = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.assertEqual((current["status"], current["cancel_reason"]), ("cancelled", "dispatch_expired"))
        with mock.patch("coordfs.wake.subprocess.run") as process:
            again = wake.tick()
        process.assert_not_called()
        self.assertEqual(again["counts"].get("dispatched", 0), 0)
        self.assertEqual(len(self.dispatch_files()), 1)

    def test_early_schedule_dispatch_keeps_the_generic_ttl_cap(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        self.assertEqual(
            parse_ts(dispatch["deadline_at"]), datetime(2030, 1, 1, 20, 0).timestamp()
        )

    def test_claim_of_expired_pending_is_refused_and_cancels_it(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        self.clock[0] += 3 * 60 * 60 + 1
        with self.assertRaises(CoordError):
            wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        current = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.assertEqual((current["status"], current["cancel_reason"]), ("cancelled", "dispatch_expired"))

    def test_claim_timeout_is_unknown_and_blocks_until_owner_reconcile(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        wake.relay_claim(dispatch["dispatch_id"], relay_agent=RELAY)
        self.clock[0] += 31 * 60
        self.assertEqual(wake.relay_pending()["dispatches"], [])
        current = json.loads(self.dispatch_files()[0].read_text(encoding="utf-8"))
        self.assertEqual(current["status"], "delivery_unknown")
        self.assertEqual(wake.ledger(limit=20)["entries"][-1]["decision"], "delivery_unknown")

        self.clock[0] = datetime(2030, 1, 2, 17, 1).timestamp()
        blocked = wake.tick()
        self.assertIn(
            "delivery_unknown_unreconciled",
            [row["reason"] for row in blocked["decisions"]],
        )
        self.assertEqual(len(self.dispatch_files()), 1)

        with self.assertRaises(CoordError) as missing_confirmation:
            wake.relay_reconcile(
                dispatch["dispatch_id"], outcome="not_delivered", owner_confirm=False
            )
        self.assertEqual(missing_confirmation.exception.code, "owner_confirmation_required")
        reconciled = wake.relay_reconcile(
            dispatch["dispatch_id"], outcome="not_delivered", owner_confirm=True
        )
        self.assertEqual(reconciled["dispatch"]["status"], "reconciled")
        self.assertEqual(wake.ledger(limit=20)["entries"][-1]["decision"], "reconciled")

        resumed = wake.tick()
        self.assertEqual(resumed["counts"]["dispatched"], 1)
        self.assertEqual(len(self.dispatch_files()), 2)

    def test_unknown_claim_hides_an_already_pending_sibling_until_reconcile(self) -> None:
        wake = self.wake()
        self.enroll(wake, triggers=["message"])
        self.office(SENDER).send(TARGET, "first untrusted body", kind="question")
        self.office(SENDER).send(TARGET, "second untrusted body", kind="question")
        with mock.patch("coordfs.wake.subprocess.run") as process:
            ticked = wake.tick()
        process.assert_not_called()
        self.assertEqual(ticked["counts"]["dispatched"], 2)
        pending = wake.relay_pending()["dispatches"]
        self.assertEqual(len(pending), 2)

        claimed_id = pending[0]["dispatch_id"]
        sibling_id = pending[1]["dispatch_id"]
        wake.relay_claim(claimed_id, relay_agent=RELAY)
        self.clock[0] += 31 * 60
        self.assertEqual(wake.relay_pending()["dispatches"], [])
        sibling = json.loads(
            (self.home / "office" / "wake" / "desktop-dispatch" / f"{sibling_id}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(sibling["status"], "pending")

        wake.relay_reconcile(claimed_id, outcome="not_delivered", owner_confirm=True)
        released = wake.relay_pending()["dispatches"]
        self.assertEqual([row["dispatch_id"] for row in released], [sibling_id])

    def test_relay_enroll_rejects_non_local_native_thread_id(self) -> None:
        wake = self.wake()
        with self.assertRaises(CoordError) as invalid:
            self.enroll(wake, native_thread_id="thread-not-local")
        self.assertEqual(invalid.exception.code, "invalid_native_thread_id")

    def test_relay_cli_commands_cover_pending_claim_receipt_and_observe(self) -> None:
        wake = self.wake()
        dispatch = self.create_schedule_dispatch(wake)
        base = ["--home", str(self.home), "--agent", RELAY]

        def run(*arguments: str) -> tuple[int, dict]:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = office_main(base + list(arguments))
            return code, json.loads(stdout.getvalue())

        code, pending = run("wake-relay-pending")
        self.assertEqual((code, pending["count"]), (0, 1))
        code, claimed = run(
            "wake-relay-claim",
            "--dispatch-id",
            dispatch["dispatch_id"],
            "--relay-agent",
            RELAY,
        )
        self.assertEqual((code, claimed["code"]), (0, "wake_relay_claimed"))
        code, receipt = run(
            "wake-relay-receipt",
            "--dispatch-id",
            dispatch["dispatch_id"],
            "--status",
            "delivered",
            "--message-id",
            "message-cli",
        )
        self.assertEqual((code, receipt["code"]), (0, "wake_relay_receipt"))
        code, observed = run(
            "wake-relay-observe",
            "--dispatch-id",
            dispatch["dispatch_id"],
            "--stage",
            "target_started",
        )
        self.assertEqual((code, observed["code"]), (0, "wake_relay_observed"))
        code, reconcile_without_owner = run(
            "wake-relay-reconcile",
            "--dispatch-id",
            dispatch["dispatch_id"],
            "--outcome",
            "delivered",
        )
        self.assertEqual(
            (code, reconcile_without_owner["code"]),
            (1, "owner_confirmation_required"),
        )


if __name__ == "__main__":
    unittest.main()
