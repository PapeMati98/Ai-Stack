"""Office Core CLI, MCP and SSH gate parity on an isolated home."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from coordfs.office import Office
from coordfs.office_cli import build_parser, dispatch
from coordfs.office_mcp import OfficeMcpServer
from coordfs.office_ssh_gate import _dispatch as gate_dispatch
from coordfs.project_registry_view import IMPORT_PRINCIPALS


SEED = Path(__file__).parent / "fixtures" / "system_projects" / "I1-registry-v2.frozen-v0.json"
IMPORTER = sorted(IMPORT_PRINCIPALS)[0]


class ProjectProgramParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="office-program-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "coord"
        self.office = Office(agent_id=IMPORTER, home=self.home)
        self.mcp = OfficeMcpServer(self.office, heartbeat_ttl_s=None)

    def cli(self, command: str, *options: str) -> dict:
        identity = ["--harness", "claude", "--session-id", IMPORTER.removeprefix("claude-ailab-")] if command == "checkpoint" else ["--agent", IMPORTER]
        if command == "checkpoint":
            options = ("--channel", "synthetic-program", "--workspace", str(self.home), *options)
        args = build_parser().parse_args(["--home", str(self.home), *identity, command, *options])
        return dispatch(args)

    def gate(self, name: str, arguments: dict) -> dict:
        return gate_dispatch("tool-call", {"name": name, "arguments": arguments}, self.office)

    def test_import_thread_goal_banner_and_surface_parity(self) -> None:
        seed = json.loads(SEED.read_text(encoding="utf-8"))
        imported = self.cli("project-view-import", "--record-file", str(SEED))
        self.assertEqual(imported["code"], "imported", imported)
        self.assertEqual(self.mcp.call_tool("office_project_view_get", {"project_slug": "agent-stack"})["status"], "ok")
        self.assertEqual(self.gate("office_project_view_list", {})["count"], len(seed["projects"]))
        record = {"office_agent": IMPORTER, "tool": "claude", "surface": "cli",
                  "title": "Project integration", "project_slug": "agent-stack"}
        registered = self.cli("thread-register", "--record-json", json.dumps(record))
        self.assertEqual(registered["status"], "ok", registered)
        self.assertIsNone(registered["project_banner"])
        self.assertEqual(self.mcp.call_tool("office_thread_resolve", {"office_agent": IMPORTER})["record"]["project_slug"], "agent-stack")
        self.assertEqual(self.gate("office_thread_list", {"project_slug": "agent-stack"})["count"], 1)
        goal = {"goal_id": "g1", "project_slug": "agent-stack", "title": "Review import",
                "status": "blocked", "blocked_reason": "Synthetic review pending", "unblock_by": "reviewer"}
        self.assertEqual(self.mcp.call_tool("office_goal_add", {"record": goal})["status"], "ok")
        for checkpoint in (self.cli("checkpoint"), self.mcp.call_tool("office_checkpoint"),
                           self.gate("office_checkpoint", {})):
            self.assertEqual(checkpoint["project_banner"]["blocked_goals"][0]["goal_id"], "g1")
        self.assertEqual(self.cli("goal-update", "--goal-id", "g1", "--record-json",
                                  json.dumps({"status": "in_progress"}))["status"], "ok")
        self.assertIsNone(self.cli("checkpoint")["project_banner"])
        self.assertEqual(self.cli("goal-get", "--goal-id", "g1")["goal"]["status"], "in_progress")

    def test_unknown_slug_and_claimed_sender_refused_before_write(self) -> None:
        record = {"office_agent": "claude-ailab-22222222-2222-4222-8222-222222222222",
                  "tool": "claude", "surface": "cli", "title": "Claim", "project_slug": "agent-stack"}
        self.assertEqual(self.mcp.call_tool("office_thread_register", {"record": record})["code"], "identity_conflict")
        record["office_agent"] = IMPORTER
        self.assertEqual(self.office.thread_register(record)["code"], "unknown_project")
        self.assertFalse((self.home / "office" / "thread-registry.json").exists())

    def test_bad_recipient_rejected_even_with_legacy_marker_and_first_contact(self) -> None:
        bad = "zcode-ailab-zcode-ailab-process-99-abc"
        marker = self.home / "office" / "known-agents" / bad
        marker.parent.mkdir(parents=True)
        marker.touch()
        for new in (False, True):
            result = self.office.send(bad, "synthetic", new_recipient=new)
            self.assertEqual(result["code"], "double_prefix", result)
        self.assertFalse((self.home / "office" / "inbox" / (bad + ".jsonl")).exists())

    def test_title_spelling_cannot_select_registered_inbox_or_dilemma_copy(self) -> None:
        # Legacy normalization would turn this label into a real, known inbox.
        peer = Office(agent_id="synthetic-office-thread", home=self.home)
        self.assertEqual(peer.presence(ttl_s=60)["status"], "ok")
        before = {str(p.relative_to(self.home)): p.read_bytes()
                  for p in self.home.rglob("*") if p.is_file()}
        for result in (
            self.office.send("Synthetic Office Thread", "synthetic"),
            self.office.dilemma_open("synthetic-channel", "Choose", {"A": "a", "B": "b"},
                                     to="Synthetic Office Thread"),
        ):
            self.assertEqual(result["code"], "not_an_address", result)
        after = {str(p.relative_to(self.home)): p.read_bytes()
                 for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
