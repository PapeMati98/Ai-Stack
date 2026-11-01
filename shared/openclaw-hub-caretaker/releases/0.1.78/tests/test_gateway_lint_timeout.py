"""Action-specific canonical lint timeout stays inside caller/tick budgets."""

from __future__ import annotations

import json
import unittest

from caretaker.hub import CanonicalKnowledgeGateway
from caretaker.tick_budget import TickBudget, TickBudgetExceeded


def gateway_with_runner(calls, *, timeout=CanonicalKnowledgeGateway.DEFAULT_TIMEOUT,
                        full_lint_timeout=None):
    def runner(*, argv, cwd, timeout):
        calls.append({"argv": list(argv), "timeout": timeout})
        action = next(item for item in argv if item in {
            "status", "lint", "verify-context", "record-handoff",
        })
        payload = (
            {"action": "status", "dirty_tree_state": "clean",
             "dirty_tree_write_blocked": False}
            if action == "status" else
            {"action": "lint", "error_count": 0, "warning_count": 0}
            if action == "lint" else {"action": action}
        )
        return 0, json.dumps(payload), ""

    return CanonicalKnowledgeGateway(
        python_executable="python", script="knowledge.py", vault="synthetic",
        timeout=timeout, full_lint_timeout=full_lint_timeout,
        run_callable=runner,
    )


class CanonicalLintTimeoutTests(unittest.TestCase):

    def test_default_lint_gets_600_seconds_while_status_stays_at_120(self):
        calls = []
        gateway = gateway_with_runner(calls)

        self.assertEqual(gateway.health().status, "pass")
        self.assertEqual(gateway.lint().status, "pass")
        self.assertEqual(gateway._call("record-handoff")["action"], "record-handoff")
        self.assertEqual(gateway._call("verify-context")["action"], "verify-context")
        by_action = {
            next(item for item in call["argv"] if item in {
                "status", "lint", "verify-context", "record-handoff",
            }): call
            for call in calls
        }
        self.assertEqual(by_action["status"]["timeout"], 120)
        self.assertEqual(by_action["lint"]["timeout"], 600)
        self.assertEqual(by_action["record-handoff"]["timeout"], 120)
        self.assertEqual(by_action["verify-context"]["timeout"], 120)

    def test_explicit_short_timeout_is_kept_unless_full_lint_override_is_given(self):
        calls = []
        short = gateway_with_runner(calls, timeout=42)
        self.assertEqual(short.lint().status, "pass")
        self.assertEqual(calls[-1]["timeout"], 42)

        overridden = gateway_with_runner(calls, timeout=42, full_lint_timeout=75)
        self.assertEqual(overridden.lint().status, "pass")
        self.assertEqual(calls[-1]["timeout"], 75)

    def test_tick_budget_caps_lint_and_expired_budget_skips_runner(self):
        calls = []
        gateway = gateway_with_runner(calls)
        now = [100.0]
        budget = TickBudget(seconds=30, clock=lambda: now[0])
        with budget.activate():
            self.assertEqual(gateway.lint().status, "pass")
        self.assertEqual(calls[-1]["timeout"], 25)

        expired_calls = []
        expired_gateway = gateway_with_runner(expired_calls)
        expired_now = [200.0]
        expired_budget = TickBudget(seconds=30, clock=lambda: expired_now[0])
        expired_now[0] = expired_budget.deadline
        with expired_budget.activate():
            with self.assertRaises(TickBudgetExceeded):
                expired_gateway.lint()
        self.assertEqual(expired_calls, [])


if __name__ == "__main__":
    unittest.main()
