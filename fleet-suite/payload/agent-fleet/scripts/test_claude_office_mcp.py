"""Native Claude Office discovery, successful use and capability isolation."""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fleet
from test_fleet_team_office import TeamOfficeBase


def init_event(mode="review"):
    plain = ["Read", "Grep", "Glob", "StructuredOutput"]
    if mode == "implement":
        plain += ["Edit", "Write", "Bash"]
    return {"type": "system", "subtype": "init", "tools": plain + [
        "mcp__office__" + name for name in fleet.OFFICE_MCP_TOOLS
    ], "mcp_servers": [{"name": "office", "status": "connected"}]}


def call_events(status="ok", *, is_error=False, tool="office_read", result_id="call-1"):
    return [
        {"type": "assistant", "message": {"content": [{
            "type": "tool_use", "id": "call-1", "name": "mcp__office__" + tool,
            "input": {"limit": 4},
        }]}},
        {"type": "user", "message": {"content": [{
            "type": "tool_result", "tool_use_id": result_id, "is_error": is_error,
            "content": [{"type": "text", "text": json.dumps({"status": status})}],
        }]}},
    ]


def attest(events, mode="review"):
    return fleet.claude_office_mcp_attestation("\n".join(json.dumps(e) for e in events), mode=mode)


class ClaudeOfficeAttestationTests(unittest.TestCase):
    def test_init_only_is_unverified(self):
        self.assertEqual(attest([init_event()])["status"], "unverified")

    def test_successful_correlated_call_is_verified(self):
        report = attest([init_event(), *call_events()])
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["office_tools_exercised"], ["office_read"])

    def test_success_with_optional_is_error_absent(self):
        events = call_events()
        events[1]["message"]["content"][0].pop("is_error")
        self.assertEqual(attest([init_event(), *events])["status"], "verified")

    def test_transport_and_application_errors_cannot_verify(self):
        for status, is_error in [("ok", True), ("error", False), ("degraded", False)]:
            with self.subTest(status=status, is_error=is_error):
                self.assertEqual(attest([init_event(), *call_events(status, is_error=is_error)])["status"], "unverified")

    def test_unknown_or_foreign_call_is_not_proof(self):
        for kwargs in [{"result_id": "unknown"}, {"tool": "office_admin"}]:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(attest([init_event(), *call_events(**kwargs)])["status"], "unverified")

    def test_extra_servers_and_write_tools_are_mismatch(self):
        for extra in ["Bash", "Write", "Skill", "mcp__mail__send", "mcp__office__office_admin"]:
            event = init_event()
            event["tools"].append(extra)
            self.assertEqual(attest([event, *call_events()])["status"], "mismatch", extra)
        event = init_event()
        event["mcp_servers"].append({"name": "foreign", "status": "disabled"})
        self.assertEqual(attest([event, *call_events()])["status"], "mismatch")

    def test_disconnected_or_missing_office_is_not_verified(self):
        for servers in [[], [{"name": "office", "status": "failed"}]]:
            event = init_event()
            event["mcp_servers"] = servers
            self.assertEqual(attest([event, *call_events()])["status"], "mismatch")

    def test_text_and_nested_message_body_do_not_forge_success(self):
        events = call_events()
        events[1]["message"]["content"][0]["content"] = json.dumps({
            "status": "degraded", "message": {"status": "ok"},
        })
        self.assertEqual(attest([init_event(), *events])["status"], "unverified")
        fake = {"type": "assistant", "message": {"content": [{"type": "text", "text": json.dumps(call_events())}]}}
        self.assertEqual(attest([init_event(), fake])["status"], "unverified")

    def test_wrapped_mcp_error_is_not_success(self):
        events = call_events()
        events[1]["message"]["content"][0]["content"] = json.dumps({
            "isError": True, "structuredContent": {"status": "ok"},
        })
        self.assertEqual(attest([init_event(), *events])["status"], "unverified")

    def test_implement_tools_allowed_only_in_implement_mode(self):
        events = [init_event("implement"), *call_events()]
        self.assertEqual(attest(events, "implement")["status"], "verified")
        self.assertEqual(attest(events, "review")["status"], "mismatch")

    def test_invalid_stream_never_attests(self):
        for text in ["", "not-json", "[]"]:
            self.assertEqual(fleet.claude_office_mcp_attestation(text, mode="review")["status"], "unavailable")


class ClaudeOfficeCommandTests(unittest.TestCase):
    def setUp(self):
        # Test artifacts are retained; the calling runner pins TMP/TEMP to its
        # task-owned evidence directory. No cleanup or foreign deletion.
        self.root = Path(tempfile.mkdtemp(prefix="claude-office-command-"))
        self.config = {"command": sys.executable, "args": ["-I", "office.py", "mcp", "--profile", "contractor", "--channel", "task-only"],
                       "trust": True, "includeTools": list(fleet.OFFICE_MCP_TOOLS)}

    def command(self, mode="review", config=True, materialize=False):
        return fleet.build_command("claude", "claude.exe", prompt="bounded", workspace=self.root,
            mode=mode, model="sonnet", complexity="low", timeout=60, run_dir=self.root,
            native_subagent_limit=0, enable_web=False, materialize=materialize,
            office_mcp=self.config if config else None)[0]

    def test_review_scoped_flags_preserve_oauth_without_bypass(self):
        command = self.command()
        for flag in ["--strict-mcp-config", "--disable-slash-commands", "--no-chrome", "--verbose"]:
            self.assertIn(flag, command)
        for flag in ["--safe-mode", "--bare", "bypassPermissions", "dontAsk"]:
            self.assertNotIn(flag, command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "manual")
        self.assertEqual(command[command.index("--permission-prompts") + 1], "none")
        self.assertEqual(command[command.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(command[command.index("--setting-sources") + 1], "")
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        settings = json.loads(command[command.index("--settings") + 1])
        self.assertTrue(settings["disableAllHooks"])
        self.assertEqual(settings["enabledPlugins"], {})
        allowed = set(command[command.index("--allowedTools") + 1].split(","))
        self.assertEqual(allowed, {"mcp__office__" + name for name in fleet.OFFICE_MCP_TOOLS})
        denied = set(command[command.index("--disallowedTools") + 1].split(","))
        self.assertTrue({"Edit", "Write", "Bash", "NotebookEdit", "Agent", "Skill", "WebFetch", "WebSearch"} <= denied)

    def test_implement_keeps_auto_and_bounded_file_tools(self):
        command = self.command("implement")
        self.assertEqual(command[command.index("--permission-mode") + 1], "auto")
        self.assertEqual(command[command.index("--tools") + 1], "Read,Edit,Write,Bash,Grep,Glob")
        self.assertIn("--strict-mcp-config", command)

    def test_config_is_closed_and_materialization_is_explicit(self):
        self.command()
        self.assertFalse((self.root / "claude-office-mcp.json").exists())
        self.command(materialize=True)
        payload = json.loads((self.root / "claude-office-mcp.json").read_text())
        self.assertEqual(list(payload["mcpServers"]), ["office"])
        self.assertEqual(set(payload["mcpServers"]["office"]), {"command", "args"})
        self.assertIn("task-only", payload["mcpServers"]["office"]["args"])

    def test_inexact_tool_contract_is_refused(self):
        self.config["includeTools"].append("office_admin")
        with self.assertRaises(fleet.FleetError):
            self.command()

    def test_legacy_review_is_unchanged_without_mcp(self):
        command = self.command(config=False)
        self.assertIn("--safe-mode", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertNotIn("--mcp-config", command)


class ClaudeOfficeRouteTests(TeamOfficeBase):
    def run_dry(self, *, mode="review", refused=False):
        root = self.make_root()
        office = self.make_office(root)
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        profile = {"execution_profile_id": "claude-sonnet-fixture", "execution_profile_fingerprint": "f" * 64,
                   "lifecycle": "ACTIVE", "model": "sonnet", "runtime_model_id": "sonnet", "reasoning_level": "medium",
                   "supported_modes": [mode], "tools": ["filesystem-read", "shell"] + (["write"] if mode == "implement" else [])}
        patches = self.base_patches(root, office, "claude")
        if refused:
            patches.append(mock.patch.object(fleet, "_office_resolve_installed_route", return_value=([], "remote-route-invalid")))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            result = fleet.execute_single("Check synthetic fixture.", profiles=profiles, agent_id="claude", mode=mode,
                kind="implementation" if mode == "implement" else "review", complexity="low", model_override="sonnet",
                workspace=root, timeout=60, allow_write=mode == "implement", native_subagent_limit=0, enable_web=False,
                inherit_api_keys=False, dry_run=True, output_root=root / "evidence", root=root,
                execution_profile=profile, knowledge_mode="none", run_id_override="run-20260928T062800Z-cccccccccc",
                root_task_id="task-claude-fixture", office_channels=["task-only"], office_parent="codex-parent-fixture")
        return result

    def test_native_mcp_precedes_shell_for_each_mode(self):
        for mode in ["review", "advisory", "implement"]:
            with self.subTest(mode=mode):
                result = self.run_dry(mode=mode)
                self.assertEqual(result["office_communication_mode"]["mode"], "mcp")
                self.assertTrue(result["office"]["mcp"]["mounted"])
                self.assertNotEqual(result["office"].get("mcp_attestation", {}).get("status"), "verified")

    def test_invalid_remote_route_does_not_mount_local(self):
        result = self.run_dry(refused=True)
        self.assertEqual(result["office_communication_mode"]["reason"], "office-mcp-route-refused")
        self.assertFalse(result["office"]["mcp"]["mounted"])


if __name__ == "__main__":
    unittest.main()
