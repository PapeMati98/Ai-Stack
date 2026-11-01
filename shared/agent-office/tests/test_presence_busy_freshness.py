"""A live connection must not keep an old `busy` fresh (heartbeat liveness vs explicit status).

Live 2026-09-27: a native Desktop canary said `busy` once; its Office MCP heartbeat renewed the
presence lease every ttl/2, `acquired_at` moved forward and Wake skipped the binding with
`recipient_busy` long after the turn had ended. Freshness now comes from `status_at`, set only
by an explicit status.
"""

from __future__ import annotations

import json
import unittest

from support import PACKAGE_ROOT  # noqa: F401  (puts the package on sys.path)

from coordfs.leases import CoordBoard, format_ts
from coordfs.office import Office
from coordfs.wake import BUSY_FRESH_S, Wake
import test_wake_relay as _relay

TARGET = "claude-host-canary"
OTHER = "claude-host-other"


class BusyFreshnessTests(unittest.TestCase):
    setUp = _relay.WakeRelayTests.setUp

    def office(self, agent: str) -> Office:
        return Office(agent_id=agent, home=self.home, now=lambda: self.clock[0])

    def wake(self) -> Wake:
        return Wake(home=self.home, now=lambda: self.clock[0])

    def reason(self, agent: str = TARGET) -> str | None:
        return self.wake()._recipient_busy_reason({"office_agent": agent})

    def heartbeat_for(self, seconds: int, *, agent: str = TARGET, step: int = 30) -> None:
        for _ in range(seconds // step):
            self.clock[0] += step
            self.assertEqual(self.office(agent).presence(ttl_s=60)["status"], "ok")  # no status

    def test_heartbeat_does_not_keep_an_old_busy_fresh(self) -> None:
        self.assertEqual(self.office(TARGET).presence(ttl_s=60, status="busy")["status"], "ok")
        self.assertEqual(self.reason(), "recipient_busy")
        self.heartbeat_for(BUSY_FRESH_S + 60)
        lease = self.lease(TARGET)
        self.assertEqual(lease["meta"]["status"], "busy")  # still shown, liveness kept
        self.assertIsNone(self.reason())  # but no longer a fresh busy for Wake

    def test_an_explicit_busy_re_announcement_stays_fresh(self) -> None:
        office = self.office(TARGET)
        office.presence(ttl_s=60, status="busy")
        for _ in range(4):
            self.clock[0] += BUSY_FRESH_S // 2
            office.presence(ttl_s=BUSY_FRESH_S, status="busy")
        self.assertEqual(self.reason(), "recipient_busy")

    def test_idle_is_never_busy_and_a_new_status_redates(self) -> None:
        office = self.office(TARGET)
        office.presence(ttl_s=60, status="busy")
        office.presence(ttl_s=60, status="idle")
        self.assertIsNone(self.reason())
        self.clock[0] += 5
        office.presence(ttl_s=60, status="busy")
        self.assertEqual(self.lease(TARGET)["meta"]["status_at"], format_ts(self.clock[0]))

    def test_a_legacy_lease_without_status_at_keeps_the_cautious_rule_and_says_so(self) -> None:
        board = CoordBoard(home=self.home, now=lambda: self.clock[0])
        board.claim("agent:" + TARGET, TARGET, ttl_s=3 * BUSY_FRESH_S,
                    meta={"kind": "presence", "status": "busy"}, kind="agent")
        self.assertEqual(self.reason(), "recipient_busy_legacy")
        self.clock[0] += BUSY_FRESH_S + 1
        board.renew("agent:" + TARGET, TARGET, ttl_s=3 * BUSY_FRESH_S, kind="agent")
        self.assertEqual(self.reason(), "recipient_busy_legacy")  # renewal re-dates legacy only

    def test_an_unreadable_status_time_counts_as_busy(self) -> None:
        board = CoordBoard(home=self.home, now=lambda: self.clock[0])
        board.claim("agent:" + TARGET, TARGET, ttl_s=60,
                    meta={"kind": "presence", "status": "busy", "status_at": "not-a-time"}, kind="agent")
        self.assertEqual(self.reason(), "recipient_busy_unverified")

    def test_another_agents_lease_is_untouched(self) -> None:
        self.office(OTHER).presence(ttl_s=600, status="busy")
        before = self.lease(OTHER)
        self.office(TARGET).presence(ttl_s=60, status="busy")
        self.heartbeat_for(120)
        self.assertEqual(self.lease(OTHER), before)
        self.assertEqual(self.reason(OTHER), "recipient_busy")

    def test_wake_skips_with_the_precise_reason(self) -> None:
        wake = self.wake()
        wake.enroll(binding_id="canary", office_agent=TARGET, driver="claude-desktop-relay",
                    native_thread_id=_relay.LOCAL_ID, workspace=str(self.workspace), enrolled_by="owner",
                    allowed_wakers=["codex-host-sender"], allowed_triggers=["message"],
                    wake_kinds=["question"], budget_per_day=6,
                    expires_at=format_ts(self.clock[0] + 86400), owner_confirm=True)
        wake.verify("canary", owner_confirm=True)
        Office(agent_id=TARGET, home=self.home)._remember_canonical_agent()
        self.office(TARGET).presence(ttl_s=60, status="busy")
        self.office("codex-host-sender").send(TARGET, "pytanie", kind="question")
        reasons = [d.get("reason") for d in wake.tick()["decisions"]]
        self.assertIn("recipient_busy", reasons)
        self.heartbeat_for(BUSY_FRESH_S + 60)
        self.office("codex-host-sender").send(TARGET, "drugie", kind="question")
        decisions = [d.get("decision") for d in wake.tick()["decisions"]]
        self.assertIn("dispatched", decisions)

    def lease(self, agent: str) -> dict:
        board = CoordBoard(home=self.home, now=lambda: self.clock[0])
        for lease in board.ls()["leases"]:
            if lease.get("resource_id") == "agent:" + agent:
                snapshot = json.loads(json.dumps(lease))
                snapshot.pop("ttl_remaining_s", None)  # changes with the clock by itself
                return snapshot
        self.fail(f"no lease for {agent}")


if __name__ == "__main__":
    unittest.main()
