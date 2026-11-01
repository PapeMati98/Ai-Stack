"""Self-pull Wake over MCP: the whole chain runs through the session's own Office MCP server.

A session whose shell is unavailable (for example refused by a permission classifier) drives
its owner-enrolled self-pull binding with the same five operations the CLI offers. The caller
is always the server's own identity, so no argument can act for another session.
"""

from __future__ import annotations

import unittest
from unittest import mock

from support import PACKAGE_ROOT  # noqa: F401  (puts the package on sys.path)

from coordfs.office_mcp import OfficeMcpServer
from coordfs.wake import PULL_SCHEDULE_S
import test_wake as _p1
import test_wake_self_pull as _sp
from test_wake import AGENT_A, AGENT_B

WAKE_TOOLS = ("office_wake_expect", "office_wake_pull_arm", "office_wake_pull",
              "office_wake_pull_confirm", "office_wake_consume")


class OfficeMcpWakeTests(unittest.TestCase):
    # Borrow the Wake fixtures without re-running the parent suites' tests.
    setUp = _p1.WakeTests.setUp
    tearDown = _p1.WakeTests.tearDown
    wake = _p1.WakeTests.wake
    office = _p1.WakeTests.office
    enroll_pull = _sp.WakeSelfPullTests.enroll_pull

    def server(self, agent: str, profile: str = "full") -> OfficeMcpServer:
        extra = {"channels": ["task-w"], "peers": [AGENT_B]} if profile == "contractor" else {}
        return OfficeMcpServer(self.office(agent), heartbeat_ttl_s=None, profile=profile, **extra)

    def test_full_chain_runs_through_mcp_only(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        lead = self.server(AGENT_A)
        sent = lead.call_tool("office_send", {"to": AGENT_B, "body": "zlecenie", "kind": "question"})
        self.assertEqual(sent["status"], "ok")
        expected = lead.call_tool("office_wake_expect", {
            "binding_id": "lead-pull", "task_id": "t1", "correlation_id": "c1",
            "counterpart": AGENT_B, "request_message_id": sent["id"], "max_wakes": 3})
        self.assertEqual(expected["status"], "ok", expected)
        armed = lead.call_tool("office_wake_pull_arm", {"binding_id": "lead-pull"})
        self.assertEqual((armed["status"], armed["armed"]), ("ok", "new"), armed)
        timer = armed["next"]
        self.assertIn("office_wake_pull", timer["prompt"])
        self.assertIn(timer["cleanup_marker"], timer["prompt"])
        confirmed = lead.call_tool("office_wake_pull_confirm", {
            "binding_id": "lead-pull", "token": timer["token"], "job_id": "job-1"})
        self.assertEqual(confirmed["action"], "keep")
        reply_id = self.office(AGENT_B).send(AGENT_A, "wynik", kind="note", reply_to=sent["id"])["id"]
        self.clock[0] += PULL_SCHEDULE_S[0]
        pulled = lead.call_tool("office_wake_pull", {"binding_id": "lead-pull", "token": timer["token"]})
        self.assertEqual(pulled["action"], "handle", pulled)
        self.assertEqual([r["trigger_message_id"] for r in pulled["replies"]], [reply_id])
        consumed = lead.call_tool("office_wake_consume", {"correlation_id": "c1", "trigger_message_id": reply_id})
        self.assertEqual(consumed["status"], "ok", consumed)
        self.assertEqual(wake.correlations.load("c1")["consumed"], [reply_id])

    def test_the_caller_is_always_the_server_identity(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        other = self.server(AGENT_B)
        refused = other.call_tool("office_wake_pull_arm", {"binding_id": "lead-pull"})
        self.assertEqual((refused["status"], refused["code"]), ("error", "wake_pull_forbidden"))
        self.assertEqual(refused["agent"], AGENT_B)
        impersonation = other.call_tool("office_wake_pull_arm", {"binding_id": "lead-pull", "caller": AGENT_A})
        self.assertEqual(impersonation["code"], "invalid_params")

    def test_wake_errors_come_back_as_results(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        lead = self.server(AGENT_A)
        empty = lead.call_tool("office_wake_pull_arm", {"binding_id": "lead-pull"})
        self.assertEqual((empty["status"], empty["code"]), ("error", "no_open_correlation"))
        bad = lead.call_tool("office_wake_pull_arm", {"binding_id": "lead-pull", "lost": "yes"})
        self.assertEqual(bad["code"], "invalid_params")
        stale = lead.call_tool("office_wake_pull", {"binding_id": "lead-pull", "token": "0" * 32})
        self.assertEqual((stale["action"], stale["reason"]), ("stop", "stale_pull_token"))

    def test_an_io_failure_is_degraded_never_raised(self) -> None:
        wake = self.wake()
        self.enroll_pull(wake)
        lead = self.server(AGENT_A)
        with mock.patch("coordfs.office_mcp.Wake.pull_arm", side_effect=OSError("disk")):
            result = lead.call_tool("office_wake_pull_arm", {"binding_id": "lead-pull"})
        self.assertEqual((result["status"], result["code"]), ("degraded", "office_unavailable"))
        bad_type = lead.call_tool("office_wake_pull_arm", {"binding_id": "lead-pull", "max_pulls": True})
        self.assertEqual(bad_type["status"], "error")
        token = lead.call_tool("office_wake_pull", {"binding_id": "lead-pull", "token": 7})
        self.assertEqual((token["status"], token["code"]), ("error", "invalid_params"))
        with mock.patch("coordfs.office_mcp.Wake", side_effect=OSError("home")):
            built = lead.call_tool("office_wake_pull", {"binding_id": "lead-pull", "token": "a" * 32})
        self.assertEqual(built["status"], "degraded")

    def test_contractor_profile_has_no_wake_tools(self) -> None:
        contractor = self.server(AGENT_A, profile="contractor")
        listed = {tool["name"] for tool in contractor._dispatch("tools/list", {})["tools"]}
        self.assertFalse(listed & set(WAKE_TOOLS))
        refused = contractor.call_tool("office_wake_pull_arm", {"binding_id": "lead-pull"})
        self.assertEqual(refused["code"], "outside_profile")
        full = {tool["name"] for tool in self.server(AGENT_A)._dispatch("tools/list", {})["tools"]}
        self.assertTrue(set(WAKE_TOOLS) <= full)


if __name__ == "__main__":
    unittest.main()
