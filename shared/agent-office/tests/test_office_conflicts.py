"""Lease conflict advice remains visible and warning-only for active work."""

from __future__ import annotations

from support import CoordTestCase

from coordfs.conflicts import ConflictAdvisor
from coordfs.work import WorkBoard


SHA = "b" * 40
LINEAGE = {"task_id": "fleet-task-004", "graph_id": "graph-r6", "run_id": "run-004"}


class ConflictAdviceTests(CoordTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.checkout = self.cwd / "checkout"
        self.checkout.mkdir()
        self.sentinel = self.checkout / "unrelated-wip.bin"
        self.sentinel_bytes = b"keep-this-wip\x00byte-for-byte\r\n"
        self.sentinel.write_bytes(self.sentinel_bytes)
        self.work = WorkBoard(home=self.home, now=lambda: self.clock[0])
        self.work.register_task(
            project="agent-office",
            task_id="fleet-task-004",
            checkout=str(self.checkout),
            scopes=["shared/agent-office/coordfs/"],
            base_sha=SHA,
            commit_intent="parent will decide whether to integrate",
            journal_refs=["journal://graph/node-office-active-work-004"],
        )
        self.advisor = ConflictAdvisor()

    def test_same_task_sessions_exchange_a_scoped_plan_without_serializing_fanout(self) -> None:
        held = self.board.claim(
            str(self.checkout),
            "interactive-holder",
            ttl_s=60,
            meta=LINEAGE,
            kind="path",
        )
        self.assertEqual(held["status"], "ok")
        warning = self.board.claim(
            str(self.checkout),
            "interactive-joiner",
            ttl_s=60,
            meta=LINEAGE,
            kind="path",
        )
        self.assertEqual(warning["status"], "warning")
        self.assertEqual(warning["code"], "held")

        advice = self.advisor.advise(
            warning,
            requester_agent="interactive-joiner",
            requester_lineage=LINEAGE,
        )
        self.assertEqual(advice["holder_agent"], "interactive-holder")
        self.assertEqual(advice["ttl_hint_s"], 60)
        self.assertEqual(advice["lineage_relation"], "same_task")
        self.assertTrue(advice["fanout_compatible"])
        self.assertFalse(advice["requires_serialization"])
        self.assertTrue(advice["advisory_only"])
        self.assertFalse(advice["automatic_retry"])

        # Fleet candidates can carry distinct run IDs while still working one
        # task/graph; this must stay a coordination warning, not a false deny.
        another_candidate = self.advisor.advise(
            warning,
            requester_agent="interactive-candidate-two",
            requester_lineage={**LINEAGE, "run_id": "candidate-two"},
        )
        self.assertEqual(another_candidate["lineage_relation"], "same_task")
        self.assertTrue(another_candidate["fanout_compatible"])
        self.assertFalse(another_candidate["requires_serialization"])

        proposed = self.advisor.propose_scoped_integration(
            self.work,
            project="agent-office",
            task_id="fleet-task-004",
            from_agent="interactive-joiner",
            to_agent=advice["holder_agent"],
            scopes=["shared/agent-office/coordfs/work.py"],
            summary="I will reconcile the lifecycle API after the holder's local edit.",
            journal_refs=["journal://integration/plan-1"],
        )
        plan = proposed["plan"]
        self.assertEqual(plan["status"], "proposed")
        acknowledged = self.work.acknowledge_integration_plan(
            project="agent-office",
            task_id="fleet-task-004",
            plan_id=plan["plan_id"],
            agent_id="interactive-holder",
            status="accepted",
            note="will publish the scoped handoff after local verification",
        )
        self.assertEqual(acknowledged["plan"]["status"], "accepted")
        self.assertEqual(self.work.list_integration_plans(project="agent-office", task_id="fleet-task-004")["count"], 1)
        self.assertEqual(self.board.who(str(self.checkout), kind="path")["holder_agent"], "interactive-holder")
        self.assertEqual(self.sentinel.read_bytes(), self.sentinel_bytes)

    def test_unrelated_lineage_stays_a_warning_not_a_deny(self) -> None:
        warning = self.board.claim(
            str(self.checkout),
            "holder",
            ttl_s=45,
            meta={"task_id": "other-task", "graph_id": "other-graph", "run_id": "other-run"},
            kind="path",
        )
        self.assertEqual(warning["status"], "ok")
        warning = self.board.claim(
            str(self.checkout),
            "requester",
            ttl_s=45,
            meta=LINEAGE,
            kind="path",
        )
        advice = self.advisor.advise(warning, requester_agent="requester", requester_lineage=LINEAGE)
        self.assertEqual(advice["lineage_relation"], "unrelated_task")
        self.assertFalse(advice["fanout_compatible"])
        self.assertFalse(advice["requires_serialization"])
        self.assertTrue(advice["advisory_only"])
        self.assertFalse(advice["automatic_retry"])

    def test_stale_unknown_and_unavailable_results_are_visible_without_escalation(self) -> None:
        stale = self.advisor.advise(
            {
                "status": "conflict",
                "code": "stale_resource",
                "holder_agent": "holder",
                "lease": {"meta": LINEAGE},
            },
            requester_agent="requester",
            requester_lineage=LINEAGE,
        )
        unknown = self.advisor.advise(None, requester_agent="requester", requester_lineage=LINEAGE)
        unavailable = self.advisor.advise(
            {"status": "degraded", "code": "board_unavailable", "message": "home is locked"},
            requester_agent="requester",
            requester_lineage=LINEAGE,
        )
        self.assertEqual(stale["visibility"], "stale")
        self.assertEqual(unknown["visibility"], "unknown")
        self.assertEqual(unavailable["visibility"], "unavailable")
        for advice in (stale, unknown, unavailable):
            with self.subTest(code=advice["code"]):
                self.assertTrue(advice["advisory_only"])
                self.assertFalse(advice["automatic_retry"])
                self.assertEqual(advice["authority"], "none")
