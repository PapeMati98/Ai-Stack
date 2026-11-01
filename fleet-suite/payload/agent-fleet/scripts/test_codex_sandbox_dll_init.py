"""Offline regressions for HD-20261013-82360b: Codex Windows sandbox user32 DLL init.

A Codex sandbox launched from a scheduled-task session cannot start children
whose DLL initialisation needs user32 (python _ctypes/_ssl, PowerShell, whoami:
STATUS_DLL_INIT_FAILED 0xC0000142). Fleet names the limit in the Codex preamble,
counts the affected check commands per run and appends the note to the
transport cause. Nothing here changes run status, transport or handoff gates.
"""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import fleet

CTYPES_TRACEBACK = (
    "Traceback (most recent call last):\r\n"
    '  File "test_x.py", line 3, in <module>\r\n    import fleet\r\n'
    '  File "fleet.py", line 6, in <module>\r\n    import ctypes\r\n'
    "    from _ctypes import Union, Structure, Array\r\n"
    "ImportError: DLL load failed while importing _ctypes: Wyst\u0105pi\u0142 b\u0142\u0105d wewn\u0119trzny.\r\n"
    "\r\nRan 1 test in 0.000s\r\n\r\nFAILED (errors=1)\r\n"
)


def event(kind: str, **item) -> str:
    return json.dumps({"type": "item.completed", "item": {"type": kind, **item}})


def command(output: str, exit_code=0, status="completed") -> str:
    return event(
        "command_execution",
        id="item_1",
        command='"C:\\Windows\\system32\\cmd.exe" /c "python -m unittest test_x"',
        aggregated_output=output,
        exit_code=exit_code,
        status=status,
    )


def codex_stdout(*lines: str, handoff=None) -> str:
    out = [json.dumps({"type": "thread.started", "thread_id": "synthetic"}),
           json.dumps({"type": "turn.started"}), *lines]
    if handoff is not None:
        out.append(event("agent_message", text=json.dumps(handoff)))
        out.append(json.dumps({"type": "turn.completed"}))
    return "\n".join(out)


class DetectorTests(unittest.TestCase):
    def test_counts_marker_and_status_dll_init_failed_exit_codes(self):
        stdout = codex_stdout(
            command(CTYPES_TRACEBACK, exit_code=0),
            command("", exit_code=3221225794),
            command("", exit_code=-1073741502),
            command("ok\r\n", exit_code=0),
            command("ImportError: No module named foo", exit_code=1),
        )
        self.assertEqual(fleet.codex_sandbox_dll_init_failures("codex", stdout), 3)

    def test_marker_is_case_insensitive_and_covers_ssl(self):
        stdout = codex_stdout(command(
            "ImportError: DLL LOAD FAILED while importing _ssl: internal error", exit_code=1,
        ))
        self.assertEqual(fleet.codex_sandbox_dll_init_failures("codex", stdout), 1)

    def test_zero_for_other_adapters_prose_partial_lines_and_other_items(self):
        stdout = codex_stdout(command(CTYPES_TRACEBACK))
        for adapter in ("grok", "claude", "minimax", None, ""):
            with self.subTest(adapter=adapter):
                self.assertEqual(fleet.codex_sandbox_dll_init_failures(adapter, stdout), 0)
        for text in (None, "", "DLL load failed while importing _ctypes",
                     "{not json", codex_stdout(command(CTYPES_TRACEBACK))[:-40],
                     json.dumps({"type": "item.started", "item": {
                         "type": "command_execution", "aggregated_output": CTYPES_TRACEBACK}}),
                     event("agent_message", text=CTYPES_TRACEBACK),
                     json.dumps({"type": "item.completed", "item": "command_execution"}),
                     command("", exit_code="3221225794"), command("", exit_code=True)):
            with self.subTest(text=str(text)[:40]):
                self.assertEqual(fleet.codex_sandbox_dll_init_failures("codex", text), 0)

    def test_note_only_for_positive_counts(self):
        for count in (0, -1, None, "2", 2.0, True):
            with self.subTest(count=count):
                self.assertIsNone(fleet.codex_sandbox_dll_init_note(count))
        note = fleet.codex_sandbox_dll_init_note(2)
        self.assertIn("CODEX SANDBOX DLL INIT: 2 maker command(s)", note)
        self.assertIn("0xC0000142", note)
        self.assertIn("outside the sandbox", note)


class PreambleTests(unittest.TestCase):
    schema = {"type": "object"}

    def prompt(self, adapter_id, mode="implement", **extra):
        return fleet.build_worker_prompt(
            "Fix one file.", agent_id=adapter_id, adapter_id=adapter_id, mode=mode,
            workspace=Path("C:/work/project"), kind="implementation", complexity="low",
            native_subagent_limit=0, enable_web=False, knowledge_snapshot=None,
            schema=self.schema, **extra,
        )

    def test_codex_prompt_names_the_sandbox_limit_in_every_mode(self):
        for mode in ("implement", "review", "advisory"):
            with self.subTest(mode=mode):
                prompt = self.prompt("codex", mode=mode)
                self.assertIn("Codex Windows sandbox rules:", prompt)
                self.assertIn("STATUS_DLL_INIT_FAILED (0xC0000142)", prompt)
                self.assertIn("`DLL load failed while importing _ctypes`/`_ssl`", prompt)
                self.assertIn("After the first such failure stop troubleshooting", prompt)
                self.assertIn("not runnable in sandbox: user32 DLL init", prompt)
                self.assertIn("the parent runs the declared checks outside the sandbox", prompt)
                self.assertEqual(prompt.count("Codex Windows sandbox rules:"), 1)

    def test_thin_profile_keeps_both_rule_blocks(self):
        prompt = self.prompt("codex", runtime_profile="l2-thin", max_model_tool_calls=4)
        self.assertIn("Codex Windows sandbox rules:", prompt)
        self.assertIn("Thin executor rules:", prompt)
        self.assertLess(prompt.index("Codex Windows sandbox rules:"), prompt.index("Thin executor rules:"))

    def test_other_adapters_keep_their_own_rules(self):
        for adapter in ("grok", "claude", "antigravity", "minimax", "qwen", "mistral", "test"):
            with self.subTest(adapter=adapter):
                prompt = self.prompt(adapter)
                self.assertNotIn("Codex Windows sandbox rules:", prompt)
                self.assertNotIn("STATUS_DLL_INIT_FAILED", prompt)
        self.assertIn("Antigravity headless rules:", self.prompt("antigravity"))
        self.assertIn("Qwen headless rules:", self.prompt("qwen"))


class FailureReasonTests(unittest.TestCase):
    def test_note_is_appended_to_the_transport_cause_only_for_codex(self):
        stdout = codex_stdout(command(CTYPES_TRACEBACK), command("", exit_code=3221225794))
        budget = fleet.provider_failure_reason(
            adapter_id="codex", stdout=stdout, exit_code=125, tool_budget_exceeded=True,
        )
        self.assertTrue(budget.startswith("CODEX TOOL BUDGET EXHAUSTED"))
        self.assertIn(" CODEX SANDBOX DLL INIT: 2 maker command(s)", budget)
        plain = fleet.provider_failure_reason(adapter_id="codex", stdout=stdout, exit_code=1)
        self.assertTrue(plain.startswith("PROVIDER FAILURE (not a handoff problem): "))
        self.assertTrue(plain.endswith("instead of re-running the maker."))
        timeout = fleet.provider_failure_reason(
            adapter_id="codex", stdout=stdout, exit_code=124, timed_out=True,
        )
        self.assertIn("parent timeout", timeout)
        self.assertIn("CODEX SANDBOX DLL INIT", timeout)
        for adapter in ("grok", "claude"):
            with self.subTest(adapter=adapter):
                reason = fleet.provider_failure_reason(adapter_id=adapter, stdout=stdout, exit_code=1)
                self.assertIn("PROVIDER FAILURE", reason)
                self.assertNotIn("CODEX SANDBOX DLL INIT", reason)

    def test_note_never_creates_a_failure_or_changes_transport_and_status(self):
        stdout = codex_stdout(command(CTYPES_TRACEBACK))
        self.assertIsNone(fleet.provider_failure_reason(adapter_id="codex", stdout=stdout, exit_code=0))
        self.assertEqual(fleet.provider_transport_outcome(
            "codex", stdout=stdout, exit_code=0, timed_out=False, terminal_reason=None,
            handoff_valid=True,
        )["status"], "complete")
        self.assertEqual(fleet.provider_transport_outcome(
            "codex", stdout=stdout, exit_code=125, timed_out=False, terminal_reason=None,
            tool_budget_exceeded=True,
        ), {"status": "failed", "reason": "codex-tool-budget-exhausted"})
        self.assertEqual(fleet.completed_run_status(
            0, {"status": "completed"}, adapter_id="codex", stdout=stdout,
        ), "COMPLETED")
        self.assertIn("identity rejected", fleet.identity_gate_error(
            fleet.provider_failure_reason(adapter_id="codex", stdout=stdout, exit_code=1),
            "identity rejected",
        ))


class ExecuteSingleTests(unittest.TestCase):
    def test_run_record_counts_sandbox_dll_init_failures(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        valid = {"status": "completed", "summary": "Synthetic Codex result.",
                 "files_changed": [], "verification": ["not runnable in sandbox: user32 DLL init"],
                 "risks": [], "next_steps": []}
        failures = (command(CTYPES_TRACEBACK), command("", exit_code=3221225794))
        cases = (
            # handoff, exit, budget stop, failing commands, expected status, expected count
            (valid, 0, False, failures, "COMPLETED", 2),
            (None, 125, True, failures, "FAILED", 2),
            (valid, 0, False, (command("ok\r\n"),), "COMPLETED", 0),
            (None, 1, False, (), "FAILED", 0),
        )
        for handoff, code, budget, commands, expected_status, expected_count in cases:
            with self.subTest(handoff=handoff is not None, code=code, count=expected_count), \
                    tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                child = {"exit_code": code, "timed_out": False,
                         "stdout": codex_stdout(*commands, handoff=handoff),
                         "stderr": "Synthetic supervisor stop." if budget else "",
                         "model_tool_calls": 49 if budget else 3, "tool_budget_exceeded": budget}
                patches = [mock.patch.dict(os.environ, {
                    "AGENT_FLEET_DEPTH": "0", "AGENT_FLEET_OFFICE": "0",
                    "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing-billing.json"),
                })]
                patches.extend(mock.patch.object(obj, name, return_value=value)
                    for obj, name, value in (
                        (fleet, "resolve_executable", "synthetic-codex.exe"),
                        (fleet, "probe_command", {"launchable": True, "exit_code": 0, "output": "ok"}),
                        (fleet, "codex_mcp_servers", {"status": "ok", "servers": [], "enabled": []}),
                        (fleet, "profile_runtime_environment", {}),
                        (fleet, "profile_credential_status", {"ready": True}),
                        (fleet, "merge_credential_readiness", {"ready": True, "reasons": []}),
                        (fleet.routing_v2, "readiness_from_probes", {"ready": True, "reasons": []}),
                        (fleet.routing_v2, "runtime_model_visible", (True, "visible")),
                        (fleet, "fleet_runtime_attestation", {"status": "VERIFIED"}),
                        (fleet, "read_usage", []),
                        (fleet.adaptive, "latest_quotas", {}),
                        (fleet.quota_collectors, "collect_profile_quotas", ({}, {})),
                        (fleet, "build_command", (["synthetic-codex"], "", None)),
                    ))
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    provider = stack.enter_context(mock.patch.object(
                        fleet, "run_provider_child", return_value={"child": child}))
                    result = fleet.execute_single(
                        "Synthetic Codex sandbox fixture.", profiles=profiles,
                        agent_id="codex", mode="advisory", kind="implementation",
                        complexity="low", model_override="gpt-6-luna", workspace=root,
                        timeout=5, allow_write=False, native_subagent_limit=0,
                        enable_web=False, inherit_api_keys=False, dry_run=False,
                        output_root=root / "evidence", root=root, knowledge_mode="none",
                        max_model_tool_calls=48,
                    )
                self.assertEqual(provider.call_count, 1)
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["codex_sandbox_dll_init_failures"], expected_count)
                run_files = list((root / "evidence").rglob("run-*.json")) + list(
                    (root / "evidence").rglob("*.json"))
                stored = [json.loads(p.read_text(encoding="utf-8")) for p in run_files
                          if p.name.startswith("run-")]
                if stored:
                    self.assertEqual(stored[0].get("codex_sandbox_dll_init_failures"), expected_count)
                if expected_status == "FAILED":
                    self.assertIsNone(result["handoff"])
                    if expected_count:
                        self.assertIn("CODEX SANDBOX DLL INIT: 2", result["handoff_parse_error"])
                    else:
                        self.assertNotIn("CODEX SANDBOX DLL INIT", result["handoff_parse_error"] or "")
                else:
                    self.assertEqual(result["handoff"]["status"], "completed")
                    self.assertFalse(result.get("handoff_parse_error"))
                if budget:
                    self.assertEqual(result["transport_outcome"]["reason"], "codex-tool-budget-exhausted")


if __name__ == "__main__":
    unittest.main()
