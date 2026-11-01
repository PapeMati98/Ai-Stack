"""Lifecycle records stay scoped coordination data, never execution authority."""

from __future__ import annotations

from support import CoordTestCase

from coordfs.errors import CoordError
from coordfs.work import WorkBoard


SHA = "a" * 40


class WorkLifecycleTests(CoordTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.checkout = self.cwd / "checkout"
        self.checkout.mkdir()
        self.work = WorkBoard(home=self.home, now=lambda: self.clock[0])

    def register(self, **overrides: object) -> dict:
        values: dict[str, object] = {
            "project": "agent-office",
            "task_id": "fleet-task-004",
            "checkout": str(self.checkout),
            "scopes": ["shared/agent-office/coordfs/"],
            "dependencies": ["fleet-task-003"],
            "base_sha": SHA,
            "commit_intent": "commit only after the parent integration review",
            "journal_refs": ["journal://graph/node-office-active-work-004"],
        }
        values.update(overrides)
        return self.work.register_task(**values)  # type: ignore[arg-type]

    def test_register_update_list_dependencies_and_journal_references(self) -> None:
        registered = self.register()
        self.assertEqual(registered["code"], "registered")
        task = registered["task"]
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["dependencies"], ["fleet-task-003"])
        self.assertEqual(task["journal_refs"], ["journal://graph/node-office-active-work-004"])
        self.assertEqual(task["base_sha"], SHA)

        updated = self.work.update_task(
            project="agent-office",
            task_id="fleet-task-004",
            dependencies=["fleet-task-003", "fleet-task-005"],
            journal_refs=["journal://graph/node-office-active-work-004", "journal://handoff/review"],
            status="ready",
        )
        self.assertEqual(updated["code"], "updated")
        self.assertEqual(updated["task"]["status"], "ready")
        self.assertEqual(updated["task"]["dependencies"], ["fleet-task-003", "fleet-task-005"])
        self.assertEqual(updated["task"]["journal_refs"][-1], "journal://handoff/review")

        listed = self.work.list_tasks(project="agent-office")
        self.assertEqual(listed["code"], "listed")
        self.assertEqual([item["task_id"] for item in listed["tasks"]], ["fleet-task-004"])

    def test_blocker_and_deferred_transitions_keep_reasons_explicit(self) -> None:
        self.register()
        blocked = self.work.add_blocker(
            project="agent-office",
            task_id="fleet-task-004",
            blocker_id="review-input",
            reason="waiting for the independent reviewer receipt",
            dependencies=["fleet-task-005"],
        )
        self.assertEqual(blocked["task"]["status"], "blocked")
        self.assertEqual(blocked["task"]["blockers"][0]["status"], "open")

        deferred = self.work.transition_blocker(
            project="agent-office",
            task_id="fleet-task-004",
            blocker_id="review-input",
            status="deferred",
            reason="reviewer is unavailable until the next scheduled window",
        )
        self.assertEqual(deferred["task"]["status"], "deferred")
        self.assertEqual(
            deferred["task"]["deferred_reason"],
            "reviewer is unavailable until the next scheduled window",
        )

        cleared = self.work.transition_blocker(
            project="agent-office",
            task_id="fleet-task-004",
            blocker_id="review-input",
            status="resolved",
            reason="reviewer receipt attached to the journal",
        )
        self.assertEqual(cleared["task"]["status"], "active")
        self.assertEqual(cleared["task"]["blockers"][0]["status"], "resolved")

    def test_unknown_fields_require_reasons_and_bindings_cannot_drift(self) -> None:
        with self.assertRaises(CoordError) as missing_reason:
            self.register(base_sha=None)
        self.assertEqual(missing_reason.exception.code, "missing_unknown_reason")

        unknown = self.register(
            base_sha=None,
            commit_intent=None,
            unknown_reasons={
                "base_sha": "parent has not published an integration baseline",
                "commit_intent": "commit ownership remains with the parent",
            },
        )
        self.assertIsNone(unknown["task"]["base_sha"])
        self.assertIn("base_sha", unknown["task"]["unknown_reasons"])

        other_checkout = self.cwd / "other-checkout"
        other_checkout.mkdir()
        with self.assertRaises(CoordError) as checkout_mismatch:
            self.work.update_task(
                project="agent-office",
                task_id="fleet-task-004",
                checkout=str(other_checkout),
            )
        self.assertEqual(checkout_mismatch.exception.code, "binding_mismatch")

        with self.assertRaises(CoordError) as scope_mismatch:
            self.work.update_task(
                project="agent-office",
                task_id="fleet-task-004",
                scopes=["shared/agent-office/tests/"],
            )
        self.assertEqual(scope_mismatch.exception.code, "binding_mismatch")

        with self.assertRaises(CoordError) as plan_scope_error:
            self.work.record_integration_plan(
                project="agent-office",
                task_id="fleet-task-004",
                from_agent="writer-a",
                to_agent="writer-b",
                scopes=["shared/agent-office/tests/"],
                summary="This must remain outside the registered write scope.",
            )
        self.assertEqual(plan_scope_error.exception.code, "invalid_scope_binding")

    def test_invalid_project_checkout_and_scope_are_rejected(self) -> None:
        with self.assertRaises(CoordError) as project_error:
            self.register(project="agent/office")
        self.assertEqual(project_error.exception.code, "invalid_project")

        with self.assertRaises(CoordError) as checkout_error:
            self.register(checkout="relative-checkout")
        self.assertEqual(checkout_error.exception.code, "invalid_checkout")

        with self.assertRaises(CoordError) as scope_error:
            self.register(scopes=["../outside/"])
        self.assertEqual(scope_error.exception.code, "invalid_scope")
