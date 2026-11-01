from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import grok_acp  # noqa: E402


FAKE_SERVER = SCRIPT_DIR / "fake_grok_acp_server.py"
SCHEMA = {
    "type": "object",
    "required": [
        "status",
        "summary",
        "files_changed",
        "verification",
        "risks",
        "next_steps",
    ],
    "properties": {
        "status": {"type": "string"},
        "summary": {"type": "string"},
        "files_changed": {"type": "array"},
        "verification": {"type": "array"},
        "risks": {"type": "array"},
        "next_steps": {"type": "array"},
    },
}


class GrokAcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_id = "run-20260820T000000Z-abcdef1234"
        self.run_dir = self.root / self.run_id
        self.workspace = self.root / "workspace"
        self.proxy_base = self.root / "proxy"
        self.auth_path = self.root / "auth.json"
        self.run_dir.mkdir()
        self.workspace.mkdir()
        (self.workspace / "fixture.txt").write_text("bounded", encoding="utf-8")
        self.auth_path.write_text('{"token":"fixture-only"}', encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self, *, mode: str = "review", tool_limit: int = 8) -> list[str]:
        return grok_acp.build_command(
            sys.executable,
            run_dir=self.run_dir,
            run_id=self.run_id,
            mode=mode,
            model="grok-4.7",
            reasoning_effort="medium",
            complexity="medium",
            handoff_schema=SCHEMA,
            max_model_tool_calls=tool_limit,
            proxy_base=self.proxy_base,
            executable_args=(str(FAKE_SERVER),),
        )

    def environment(self, scenario: str) -> dict[str, str]:
        base = dict(os.environ)
        base["GROK_API_KEY"] = "must-be-removed"
        base["XAI_API_KEY"] = "must-also-be-removed"
        base["FAKE_GROK_ACP_SCENARIO"] = scenario
        return grok_acp.build_environment(
            base, run_dir=self.run_dir, auth_path=self.auth_path
        )

    def execute_scenario(
        self, scenario: str, **overrides: object
    ) -> dict[str, object]:
        tool_limit = int(overrides.pop("max_model_tool_calls", 8))
        options: dict[str, object] = {
            "command": self.command(tool_limit=tool_limit),
            "run_dir": self.run_dir,
            "run_id": self.run_id,
            "workspace": self.workspace,
            "mode": "review",
            "prompt": "Inspect project/fixture.txt and return the handoff.",
            "timeout": int(overrides.pop("timeout", 30)),
            "max_model_tool_calls": tool_limit,
            "max_additional_prompts": int(
                overrides.pop("max_additional_prompts", 0)
            ),
            "env": self.environment(scenario),
            "continuation_decider": overrides.pop(
                "continuation_decider", lambda _value: None
            ),
            "knowledge_snapshot": None,
            "proxy_base": self.proxy_base,
        }
        options.update(overrides)
        return grok_acp.run_session(**options)

    def test_command_is_native_acp_and_bounded(self) -> None:
        command = self.command(mode="implement")
        self.assertEqual(command[-1], "stdio")
        self.assertIn("agent", command)
        self.assertIn("--agent-profile", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(command[command.index("--sandbox") + 1], "strict")
        self.assertIn("search_replace", command[command.index("--tools") + 1])
        self.assertIn("Edit(project/**)", command)
        for forbidden in ("--single", "--resume", "--always-approve"):
            self.assertNotIn(forbidden, command)

    def test_system_contract_exposes_bounded_shell_consistently(self) -> None:
        command = self.command(mode="implement")
        prompt = command[command.index("--system-prompt-override") + 1]
        profile_path = grok_acp.materialize_profile(self.run_dir, mode="implement")
        profile = profile_path.read_text(encoding="utf-8")
        for required in (
            "relative path project/",
            "read_file",
            "grep_search",
            "list_dir",
            "bounded Bash tool",
            "strict sandbox",
            "Do not commit, push, deploy, or alter remotes",
        ):
            self.assertIn(required, prompt)
            self.assertIn(required, profile)
        tools = command[command.index("--tools") + 1].split(",")
        disallowed = command[command.index("--disallowed-tools") + 1].split(",")
        self.assertIn("bash", tools)
        self.assertIn("search_replace", tools)
        self.assertNotIn("bash", disallowed)
        allow_rules = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--allow"
        ]
        deny_rules = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--deny"
        ]
        self.assertIn("Bash(Get-ChildItem*)", allow_rules)
        self.assertIn("Bash(Get-Content*)", allow_rules)
        self.assertIn(
            "Bash(pwsh -NoLogo -NoProfile -File project/.fleet/*)", allow_rules
        )
        self.assertIn(
            "Bash(pwsh -NoLogo -NoProfile -File project/*)", allow_rules
        )
        self.assertIn("Bash(python -I project/*)", allow_rules)
        self.assertIn("Bash(python project/*)", allow_rules)
        self.assertNotIn(
            "Bash(pwsh -NoLogo -NoProfile -File .fleet/*)", allow_rules
        )
        self.assertIn("Bash(git push*)", deny_rules)
        self.assertNotIn("Bash", deny_rules)

    def test_bounded_shell_permission_policy_is_fail_closed(self) -> None:
        self.assertTrue(
            grok_acp._bounded_shell_permission_allowed(
                "pwsh -NoLogo -NoProfile -File project/.fleet/verify.ps1"
            )
        )
        self.assertTrue(
            grok_acp._bounded_shell_permission_allowed(
                "Get-ChildItem -Force | Select-Object Name, Mode, Length"
            )
        )
        self.assertTrue(
            grok_acp._bounded_shell_permission_allowed(
                "python -I project/skills/example/scripts/test_example.py ExampleTests.test_case -v"
            )
        )
        self.assertTrue(
            grok_acp._bounded_shell_permission_allowed(
                "python project/skills/example/scripts/validate.py lint --vault project/vault"
            )
        )
        for command in (
            "pwsh -NoLogo -NoProfile -File .fleet/verify.ps1",
            "git push origin main",
            "npm test && rm -rf project",
            "python -m pytest; Get-Content ../secret.txt",
            "python -c \"import os; print(os.environ)\"",
            "python -I project/../outside.py",
            "Get-ChildItem | Remove-Item -Recurse",
            "Get-ChildItem || Remove-Item -Recurse",
            "",
        ):
            self.assertFalse(grok_acp._bounded_shell_permission_allowed(command))

    def test_environment_uses_existing_auth_without_api_keys(self) -> None:
        env = self.environment("complete")
        self.assertEqual(env["GROK_AUTH_PATH"], str(self.auth_path.resolve()))
        self.assertEqual(env["GROK_HOME"], str((self.run_dir / "grok-acp-home")))
        self.assertNotIn("GROK_API_KEY", env)
        self.assertNotIn("XAI_API_KEY", env)
        for key in grok_acp.COMPATIBILITY_ENV_KEYS:
            self.assertEqual(env[key], "false")
        self.assertEqual(env["GROK_MANAGED_MCPS_ENABLED"], "false")

    def test_auth_path_with_junction_component_is_rejected(self) -> None:
        target = self.root / "auth-target"
        link = self.root / "auth-link"
        target.mkdir()
        (target / "auth.json").write_text("{}", encoding="ascii")
        grok_acp._create_directory_link(target, link)
        try:
            with self.assertRaisesRegex(grok_acp.GrokAcpError, "unsafe"):
                grok_acp.build_environment(
                    dict(os.environ),
                    run_dir=self.run_dir,
                    auth_path=link / "auth.json",
                )
        finally:
            grok_acp._remove_directory_link(link)

    def test_absolute_tool_and_repair_prompt_ceilings_fail_closed(self) -> None:
        with self.assertRaisesRegex(grok_acp.GrokAcpError, "1 to 512"):
            self.command(tool_limit=513)
        with self.assertRaisesRegex(grok_acp.GrokAcpError, "0 to 6"):
            grok_acp.run_session(
                [],
                run_dir=self.run_dir,
                run_id=self.run_id,
                workspace=self.workspace,
                mode="review",
                prompt="bounded",
                timeout=10,
                max_model_tool_calls=8,
                max_additional_prompts=7,
                env=self.environment("complete"),
                continuation_decider=lambda _value: None,
                proxy_base=self.proxy_base,
            )

    def test_complete_session_discards_thought_and_cleans_runtime(self) -> None:
        result = self.execute_scenario("complete")
        envelope = json.loads(str(result["stdout"]))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["prompt_count"], 1)
        self.assertEqual(envelope["modelId"], "grok-4.7")
        self.assertEqual(envelope["usage"]["totalTokens"], 50)
        self.assertEqual(envelope["_fleet"]["thought_chunks_discarded"], 1)
        self.assertNotIn("PRIVATE_THOUGHT_MUST_NOT_PERSIST", result["stdout"])
        self.assertEqual(envelope["_fleet"]["ambient_mcp_servers"], 0)
        self.assertFalse((self.run_dir / "grok-acp-home").exists())
        self.assertFalse((self.proxy_base / self.run_id).exists())

    def test_partial_handoff_is_repaired_in_the_same_session(self) -> None:
        def decide(value: str) -> tuple[str, str] | None:
            envelope = json.loads(value)
            structured = envelope.get("structuredOutput")
            if isinstance(structured, dict) and structured.get("status") == "partial":
                return "partial-handoff", "Finish the same task and return final JSON."
            return None

        result = self.execute_scenario(
            "repair", max_additional_prompts=1, continuation_decider=decide
        )
        envelope = json.loads(str(result["stdout"]))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["prompt_count"], 2)
        self.assertEqual(result["continuation_triggers"], ["partial-handoff"])
        self.assertEqual(envelope["structuredOutput"]["status"], "completed")
        self.assertEqual(
            [item["kind"] for item in result["attempts"]],
            ["initial", "same-session-repair"],
        )

    def test_permission_request_is_denied_without_blocking_safe_result(self) -> None:
        result = self.execute_scenario("permission")
        envelope = json.loads(str(result["stdout"]))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(envelope["_fleet"]["permission_requests_denied"], 1)

    def test_allowlisted_shell_permission_is_approved_once(self) -> None:
        result = self.execute_scenario("permission-allow")
        envelope = json.loads(str(result["stdout"]))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(envelope["_fleet"]["permission_requests_approved"], 1)
        self.assertEqual(envelope["_fleet"]["permission_requests_denied"], 0)

    def test_project_local_search_replace_is_approved_only_in_implement_mode(self) -> None:
        result = self.execute_scenario(
            "permission-write-allow",
            command=self.command(mode="implement"),
            mode="implement",
        )
        envelope = json.loads(str(result["stdout"]))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(envelope["_fleet"]["permission_requests_approved"], 1)
        self.assertEqual(envelope["_fleet"]["permission_requests_denied"], 0)

        write_message = {
            "params": {
                "toolCall": {
                    "rawInput": {
                        "variant": "SearchReplace",
                        "file_path": "project/.fleet/evidence/review.md",
                    }
                },
                "options": [{"optionId": "allow-once", "kind": "allow_once"}],
            }
        }
        self.assertIsNone(
            grok_acp._permission_allow_once_option(
                write_message,
                allow_writes=False,
                workspace_root=self.workspace,
            )
        )

    def test_project_local_search_replace_rejects_traversal(self) -> None:
        message = {
            "params": {
                "toolCall": {
                    "rawInput": {
                        "variant": "SearchReplace",
                        "file_path": "project/../outside.md",
                    }
                },
                "options": [{"optionId": "allow-once", "kind": "allow_once"}],
            }
        }
        self.assertIsNone(
            grok_acp._permission_allow_once_option(
                message,
                allow_writes=True,
                workspace_root=self.workspace,
            )
        )

    def test_unlisted_shell_permission_is_denied(self) -> None:
        result = self.execute_scenario("permission-deny")
        envelope = json.loads(str(result["stdout"]))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(envelope["_fleet"]["permission_requests_approved"], 0)
        self.assertEqual(envelope["_fleet"]["permission_requests_denied"], 1)

    def test_unknown_reverse_request_is_denied(self) -> None:
        result = self.execute_scenario("reverse-request")
        envelope = json.loads(str(result["stdout"]))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(envelope["_fleet"]["reverse_requests_denied"], 1)

    def test_availability_command_advertises_zero_tools(self) -> None:
        command = self.command(mode="availability")
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--max-turns") + 1], "1")
        disallowed = command[command.index("--disallowed-tools") + 1].split(",")
        for builtin in (
            "read_file",
            "grep_search",
            "list_dir",
            "bash",
            "search_replace",
            "web_search",
            "web_fetch",
            "todo_write",
            "task",
            "kill_task",
            "get_task_output",
            "memory_search",
            "memory_get",
            "search_tool",
            "use_tool",
            "lsp",
        ):
            self.assertIn(builtin, disallowed)
        allow_rules = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--allow"
        ]
        self.assertEqual(allow_rules, [])
        profile = grok_acp._agent_profile("availability")
        self.assertIn("tools:\n", profile)
        tools_section = profile.split("tools:\n", 1)[1].split("disallowedTools:\n", 1)[0]
        self.assertEqual(tools_section.strip(), "")
        disallowed_section = profile.split("disallowedTools:\n", 1)[1].split("---", 1)[0]
        for builtin in (
            "read_file",
            "grep_search",
            "list_dir",
            "bash",
            "search_replace",
            "web_search",
            "web_fetch",
            "todo_write",
            "task",
            "kill_task",
            "get_task_output",
            "memory_search",
            "memory_get",
            "search_tool",
            "use_tool",
            "lsp",
        ):
            self.assertIn(f"  - {builtin}", disallowed_section)
        self.assertIn(grok_acp.AVAILABILITY_SYSTEM_PROMPT, profile)
        self.assertNotIn("You are an isolated Agent Fleet coding contractor.", profile)

    def test_availability_mode_cancels_a_normally_approved_shell_permission(self) -> None:
        result = self.execute_scenario(
            "permission-allow",
            mode="availability",
            command=self.command(mode="availability"),
        )
        envelope = json.loads(str(result["stdout"]))
        self.assertNotEqual(result["exit_code"], 0)
        self.assertEqual(envelope["_fleet"]["permission_requests_approved"], 0)
        self.assertEqual(envelope["_fleet"]["permission_requests_denied"], 1)

    def test_office_shell_broker_permission_policy(self) -> None:
        fake_paths = ((os.path.normcase("C:/bin/office.cmd"), grok_acp.OFFICE_SHELL_SUBCOMMANDS),)
        self.assertTrue(grok_acp._coordination_shell_allowed("C:/bin/office.cmd who", fake_paths))
        self.assertTrue(grok_acp._coordination_shell_allowed("C:/bin/office.cmd --harness codex who", fake_paths))
        self.assertFalse(grok_acp._coordination_shell_allowed("C:/bin/office.cmd wait", fake_paths))
        self.assertFalse(grok_acp._coordination_shell_allowed("C:/bin/office.cmd mcp", fake_paths))
        self.assertFalse(grok_acp._coordination_shell_allowed("C:/bin/office.cmd project-read", fake_paths))
        self.assertFalse(grok_acp._coordination_shell_allowed("C:/bin/office.cmd journal-add", fake_paths))
        self.assertFalse(grok_acp._coordination_shell_allowed("C:/bin/office.cmd work-update", fake_paths))
        self.assertFalse(grok_acp._coordination_shell_allowed("C:/bin/other.cmd who", fake_paths))

    def test_availability_mode_denies_office_shell_broker_permission(self) -> None:
        office_message = {
            "id": 999,
            "method": "session/request_permission",
            "params": {
                "sessionId": "test-session",
                "toolCall": {
                    "toolCallId": "office-call",
                    "rawInput": {
                        "variant": "Bash",
                        "command": "office.cmd who",
                    },
                },
                "options": [{"optionId": "allow-once", "kind": "allow_once"}],
            },
        }
        conn = grok_acp._AcpConnection(
            mock.MagicMock(),
            deadline=time.monotonic() + 10,
            state=grok_acp._AcpState(max_tool_calls=1),
            allow_writes=False,
            workspace_root=self.workspace,
            deny_all_tools=True,
        )
        with mock.patch.object(conn, "_send") as send_mock:
            conn._handle_reverse_request(office_message)
            send_mock.assert_called_once_with({
                "jsonrpc": "2.0",
                "id": 999,
                "result": {"outcome": {"outcome": "cancelled"}},
            })
            self.assertEqual(conn.state.permission_requests_denied, 1)
            self.assertEqual(conn.state.permission_requests_approved, 0)

    def test_absent_retain_runtime_cleans_evidence_after_failure(self) -> None:
        result = self.execute_scenario(
            "permission-allow",
            mode="availability",
            command=self.command(mode="availability"),
            retain_runtime=False,
        )
        self.assertNotEqual(result["exit_code"], 0)
        self.assertFalse((self.run_dir / "grok-acp-home").exists())
        self.assertFalse((self.proxy_base / self.run_id).exists())

    def test_retain_runtime_keeps_evidence_on_success(self) -> None:
        result = self.execute_scenario(
            "complete",
            mode="availability",
            command=self.command(mode="availability"),
            retain_runtime=True,
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue((self.run_dir / "grok-acp-home").exists())
        self.assertTrue((self.proxy_base / self.run_id).exists())

    def test_retain_runtime_keeps_evidence_after_partial_failure(self) -> None:
        result = self.execute_scenario(
            "permission-allow",
            mode="availability",
            command=self.command(mode="availability"),
            retain_runtime=True,
        )
        self.assertNotEqual(result["exit_code"], 0)
        self.assertTrue((self.run_dir / "grok-acp-home").exists())
        self.assertTrue((self.proxy_base / self.run_id).exists())

    def test_provider_stderr_content_is_suppressed(self) -> None:
        result = self.execute_scenario("stderr-thought")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("provider stderr suppressed", result["stderr"])
        self.assertNotIn("PRIVATE_STDERR_THOUGHT_MUST_NOT_PERSIST", result["stderr"])

    def test_model_history_preserves_mid_session_switch(self) -> None:
        def decide(value: str) -> tuple[str, str] | None:
            envelope = json.loads(value)
            structured = envelope.get("structuredOutput")
            if isinstance(structured, dict) and structured.get("status") == "partial":
                return "partial-handoff", "Finish the same task."
            return None

        result = self.execute_scenario(
            "model-switch", max_additional_prompts=1, continuation_decider=decide
        )
        envelope = json.loads(str(result["stdout"]))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(envelope["modelId"], "grok-4.7")
        self.assertEqual(envelope["modelIds"], ["grok-4.5", "grok-4.7"])

    def test_tool_budget_is_a_hard_stop(self) -> None:
        result = self.execute_scenario("tool-budget", max_model_tool_calls=1)
        self.assertEqual(result["exit_code"], 125)
        self.assertTrue(result["tool_budget_exceeded"])
        self.assertIn("tool-call budget exceeded", result["stderr"])

    def test_ambient_mcp_discovery_is_a_hard_stop(self) -> None:
        result = self.execute_scenario("ambient-mcp")
        self.assertEqual(result["exit_code"], 77)
        self.assertIn("MCP server discovery was not isolated", result["stderr"])

    def test_parent_timeout_kills_the_process(self) -> None:
        result = self.execute_scenario("timeout", timeout=1)
        self.assertEqual(result["exit_code"], 124)
        self.assertTrue(result["timed_out"])
        self.assertIn("parent timeout", result["stderr"])

    @unittest.skipUnless(os.name == "nt", "Windows process-tree assertion")
    def test_parent_timeout_kills_descendant_process(self) -> None:
        pid_path = self.root / "child.pid"
        env = self.environment("timeout-child")
        env["FAKE_GROK_CHILD_PID_PATH"] = str(pid_path)
        result = self.execute_scenario("timeout-child", timeout=2, env=env)
        self.assertEqual(result["exit_code"], 124)
        self.assertTrue(pid_path.is_file())
        child_pid = int(pid_path.read_text(encoding="ascii"))
        tasklist = subprocess.run(
            ["tasklist.exe", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        self.assertNotIn(f'"{child_pid}"', tasklist.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows job-object assertion")
    def test_job_close_kills_orphan_after_parent_exits_first(self) -> None:
        pid_path = self.root / "orphan.pid"
        env = self.environment("orphan-child")
        env["FAKE_GROK_CHILD_PID_PATH"] = str(pid_path)
        result = self.execute_scenario("orphan-child", timeout=10, env=env)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(pid_path.is_file())
        child_pid = int(pid_path.read_text(encoding="ascii"))
        tasklist = subprocess.run(
            ["tasklist.exe", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        self.assertNotIn(f'"{child_pid}"', tasklist.stdout)

    def test_runtime_cleanup_retries_transient_windows_sharing_violation(self) -> None:
        transient = OSError("fixture sharing violation")
        transient.winerror = 32
        target = self.root / "transient-cleanup"
        target.mkdir()
        with (
            mock.patch.object(grok_acp.os, "name", "nt"),
            mock.patch.object(
                grok_acp.shutil,
                "rmtree",
                side_effect=[transient, None],
            ) as remove_tree,
            mock.patch.object(grok_acp.time, "sleep") as sleep,
        ):
            grok_acp._remove_tree_with_retry(target)
        self.assertEqual(remove_tree.call_count, 2)
        sleep.assert_called_once_with(0.05)

    def test_partial_setup_failure_cleans_proxy_and_home(self) -> None:
        snapshot = self.root / "knowledge"
        target = self.root / "linked-target"
        snapshot.mkdir()
        target.mkdir()
        (target / "unsafe.md").write_text("unsafe", encoding="ascii")
        linked = snapshot / "linked"
        grok_acp._create_directory_link(target, linked)
        try:
            with self.assertRaisesRegex(grok_acp.GrokAcpError, "Linked file refused"):
                grok_acp._prepare_runtime(
                    run_dir=self.run_dir,
                    run_id=self.run_id,
                    workspace=self.workspace,
                    mode="review",
                    knowledge_snapshot=snapshot,
                    proxy_base=self.proxy_base,
                )
            self.assertFalse((self.proxy_base / self.run_id).exists())
            self.assertFalse((self.run_dir / "grok-acp-home").exists())
        finally:
            grok_acp._remove_directory_link(linked)


QUOTA_FAKE_SECRET = "xai-" + "fake-secret-" + "9f8e7d6c5b4a3f2e1d0c"


class _QuotaStubConnection:
    """Minimal _AcpConnection stand-in replaying one quota scenario."""

    def __init__(
        self,
        process: object,
        *,
        state: object,
        prompt_failure: BaseException | None = None,
        stderr_lines: tuple[str, ...] = (),
        **_ignored: object,
    ) -> None:
        self.process = process
        self.state = state
        self.prompt_failure = prompt_failure
        self.stderr_lines = list(stderr_lines)
        self.prompt_calls = 0
        self.stdout_thread = mock.MagicMock()
        self.stderr_thread = mock.MagicMock()

    def request(self, method: str, params: dict[str, object]) -> object:
        if method == "initialize":
            return {"authMethods": [{"id": "cached_token"}]}
        if method == "authenticate":
            return {}
        if method == "session/new":
            return {"sessionId": "quota-fixture-session"}
        if method == "session/prompt":
            self.prompt_calls += 1
            assert self.prompt_failure is not None
            raise self.prompt_failure
        raise AssertionError(f"unexpected ACP method: {method}")

    def close(self) -> tuple[int, str]:
        return (0, "")


class GrokQuotaExhaustionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_id = "run-20260820T000000Z-abcdef1234"
        self.run_dir = self.root / self.run_id
        self.workspace = self.root / "workspace"
        self.proxy_base = self.root / "proxy"
        self.auth_path = self.root / "auth.json"
        self.run_dir.mkdir()
        self.workspace.mkdir()
        (self.workspace / "fixture.txt").write_text("bounded", encoding="utf-8")
        self.auth_path.write_text('{"token":"fixture-only"}', encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def environment(self) -> dict[str, str]:
        base = dict(os.environ)
        base["FAKE_GROK_ACP_SCENARIO"] = "complete"
        return grok_acp.build_environment(
            base, run_dir=self.run_dir, auth_path=self.auth_path
        )

    def execute_quota(
        self,
        *,
        stderr_lines: tuple[str, ...] = (),
        prompt_failure: BaseException | None = None,
    ) -> tuple[dict[str, object], list[_QuotaStubConnection], list[str]]:
        connections: list[_QuotaStubConnection] = []

        def factory(process: object, **kwargs: object) -> _QuotaStubConnection:
            connection = _QuotaStubConnection(
                process,
                prompt_failure=prompt_failure,
                stderr_lines=stderr_lines,
                **kwargs,
            )
            connections.append(connection)
            return connection

        fake_process = mock.MagicMock()
        fake_process.poll.return_value = 0
        fake_process.returncode = 1
        decider_calls: list[str] = []

        def decider(value: str) -> tuple[str, str] | None:
            decider_calls.append(value)
            return None

        # Only the ACP server spawn may be faked. Helpers such as the
        # Windows junction fallback (cmd.exe / mklink via subprocess.run)
        # and taskkill/tasklist must reach the real Popen; a blanket
        # Popen mock breaks subprocess.run's communicate() unpacking on
        # native Windows ("not enough values to unpack").
        real_popen = grok_acp.subprocess.Popen

        def _quota_popen(args: object, **kwargs: object) -> object:
            argv = list(args) if isinstance(args, (list, tuple)) else []
            if any("grok-stub" in str(part) for part in argv):
                return fake_process
            return real_popen(args, **kwargs)

        class _QuotaDummyJob:
            """No-op Windows job containment for quota-path tests."""

            def assign(self, _process: object) -> None:
                return None

            def close(self) -> None:
                return None

        with (
            mock.patch.object(grok_acp, "_AcpConnection", factory),
            mock.patch.object(grok_acp, "_WindowsJob", _QuotaDummyJob),
            mock.patch.object(
                grok_acp.subprocess, "Popen", side_effect=_quota_popen
            ),
        ):
            result = grok_acp.run_session(
                ["grok-stub"],
                run_dir=self.run_dir,
                run_id=self.run_id,
                workspace=self.workspace,
                mode="review",
                prompt="Inspect project/fixture.txt and return the handoff.",
                timeout=30,
                max_model_tool_calls=8,
                max_additional_prompts=1,
                env=self.environment(),
                continuation_decider=decider,
                proxy_base=self.proxy_base,
            )
        return result, connections, decider_calls

    def assert_quota_result(
        self,
        result: dict[str, object],
        *,
        prompt_calls: int,
        decider_calls: list[str],
        connections: list[_QuotaStubConnection],
    ) -> None:
        self.assertEqual(result["exit_code"], grok_acp.QUOTA_EXHAUSTED_EXIT_CODE)
        self.assertTrue(result["quota_exhausted"])
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["tool_budget_exceeded"])
        self.assertEqual(result["prompt_count"], 0)
        self.assertEqual(result["attempts"], [])
        self.assertEqual(result["continuation_triggers"], [])
        self.assertEqual(len(connections), 1)
        self.assertEqual(connections[0].prompt_calls, prompt_calls)
        self.assertEqual(decider_calls, [])
        envelope = json.loads(str(result["stdout"]))
        self.assertIsNone(envelope["usage"])
        self.assertEqual(envelope["_fleet"]["prompt_count"], 0)
        self.assertEqual(envelope["_fleet"]["attempts"], [])
        self.assertEqual(envelope["_fleet"]["continuation_triggers"], [])
        self.assertNotIn("totalTokens", str(result["stdout"]))
        self.assertIn("quota exhausted", str(result["stderr"]).casefold())
        self.assertLessEqual(len(str(result["stderr"])), 2048)
        self.assertNotIn(
            QUOTA_FAKE_SECRET, json.dumps(result, default=str)
        )

    def test_startup_402_is_quota_before_any_prompt(self) -> None:
        result, connections, decider_calls = self.execute_quota(
            stderr_lines=(
                "ERROR HTTP 402 Payment Required: Grok Build usage balance "
                f"exhausted ({QUOTA_FAKE_SECRET})\n",
            ),
            prompt_failure=AssertionError("startup quota must not prompt"),
        )
        self.assert_quota_result(
            result,
            prompt_calls=0,
            decider_calls=decider_calls,
            connections=connections,
        )

    def test_prompt_402_has_no_retry_and_zero_tokens(self) -> None:
        failure = grok_acp.GrokAcpError(
            "Grok ACP session/prompt failed (402): usage balance exhausted; "
            f"request-id abc123 ({QUOTA_FAKE_SECRET})"
        )
        result, connections, decider_calls = self.execute_quota(
            prompt_failure=failure,
        )
        self.assert_quota_result(
            result,
            prompt_calls=1,
            decider_calls=decider_calls,
            connections=connections,
        )

    def test_provider_detail_never_reaches_quota_diagnostic(self) -> None:
        self.assertTrue(grok_acp.quota_exhaustion_detected("HTTP 402"))
        self.assertTrue(
            grok_acp.quota_exhaustion_detected("Grok Build usage balance exhausted")
        )
        self.assertTrue(grok_acp.quota_exhaustion_detected("402 Payment Required"))
        self.assertFalse(grok_acp.quota_exhaustion_detected("HTTP 500 internal"))
        message = grok_acp.quota_exhaustion_error(QUOTA_FAKE_SECRET)
        self.assertIn("quota exhausted", message.casefold())
        self.assertNotIn(QUOTA_FAKE_SECRET, message)
        self.assertEqual(message, grok_acp.quota_exhaustion_error())

    def test_non_quota_transport_error_is_preserved(self) -> None:
        failure = grok_acp.GrokAcpError(
            "Grok ACP session/prompt failed (500): internal provider boom"
        )
        result, connections, _decider_calls = self.execute_quota(
            prompt_failure=failure,
        )
        self.assertEqual(result["exit_code"], 70)
        self.assertFalse(result["quota_exhausted"])
        self.assertIn("internal provider boom", str(result["stderr"]))
        self.assertEqual(connections[0].prompt_calls, 1)


class GrokNotificationEnvelopeTests(unittest.TestCase):
    def test_only_the_known_legacy_envelope_attests_inventory(self) -> None:
        for outer, expected in (("_x.ai/notification", True), ("_spoof", False),
                                ("_x.ai/not-a-notification", False)):
            state = grok_acp._AcpState(max_tool_calls=12, expected_mcp_servers=("fleet-chrome-relay",))
            state.observe({"method": outer, "params": {
                "method": "x.ai/mcp/servers_updated",
                "params": {"servers": [{"name": "fleet-chrome-relay"}]},
            }})
            self.assertEqual(state.observed_mcp_servers == state.expected_mcp_servers, expected)

    def test_unknown_envelope_cannot_attest_the_native_session(self) -> None:
        for outer in ("_spoof", "_x.ai/not-a-notification"):
            state = grok_acp._AcpState(max_tool_calls=12, expected_mcp_servers=("fleet-chrome-relay",))
            state.expected_session_id = "assigned"
            state.expected_mcp_tool_count = 8
            for method, fields in (("x.ai/mcp_initialized", {"mcpToolCount": 8}),
                                   ("x.ai/mcp/server_status", {"name": "fleet-chrome-relay", "status": "ready"})):
                state.observe({"method": outer, "params": {
                    "method": method, "params": {"sessionId": "assigned", **fields},
                }})
            self.assertFalse(state.session_mcp_attested)


if __name__ == "__main__":
    unittest.main(verbosity=2)
