from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - older interpreter
    tomllib = None


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402


RUN_ID = "run-20260920T120000Z-aaaaaaaaaa"
TASK_ID = "task-office-fixture"
# Sciezka z ta sama wlasciwoscia co realna: zawiera spacje i jest bezwzgledna.
WORKSPACE_FIXTURE = "Q:" + chr(92) + "Synthetic Workspaces" + chr(92) + "repo-fixture"
OFFICE_STUB_SOURCE = """from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

root = Path(__file__).resolve().parent
mode_path = root / "office-mode.txt"
sleep_path = root / "office-sleep.txt"
log_path = root / "office-calls.jsonl"
mode = mode_path.read_text(encoding="utf-8").strip() if mode_path.is_file() else "ok"
record = {"argv": sys.argv[1:]}
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, ensure_ascii=True) + "\\n")
    handle.flush()
if mode == "timeout":
    delay = 1.0
    if sleep_path.is_file():
        delay = float(sleep_path.read_text(encoding="utf-8").strip() or "1")
    time.sleep(delay)
    raise SystemExit(0)
if mode == "fail":
    sys.stderr.write("office stub failed\\n")
    raise SystemExit(3)
if mode == "unknown":
    sys.stderr.write("unknown option: --workspace\\n")
    raise SystemExit(2)
raise SystemExit(0)
"""


def sample_handoff() -> dict[str, object]:
    return {
        "status": "completed",
        "summary": "Reviewed the bounded task.",
        "files_changed": [],
        "verification": ["Inspected the requested files."],
        "risks": [],
        "next_steps": [],
    }


def grok_session_result() -> dict[str, object]:
    envelope = {
        "sessionId": "11111111-1111-4111-8111-111111111111",
        "stopReason": "end_turn",
        "text": json.dumps(sample_handoff()),
        "modelId": "grok-4.7",
        "_fleet": {
            "transport": "native-cli-acp-stdio",
            "prompt_count": 1,
            "tool_calls": 0,
            "tool_events": [],
            "thought_chunks_discarded": 0,
            "permission_requests_approved": 0,
            "permission_requests_denied": 0,
            "reverse_requests_denied": 0,
            "ambient_mcp_servers": 0,
            "mcp_catalog_observed": False,
            "windows_os_sandbox_enforced": False,
        },
    }
    return {
        "exit_code": 0,
        "stdout": json.dumps(envelope),
        "stderr": "",
        "timed_out": False,
        "model_tool_calls": 0,
        "tool_budget_exceeded": False,
        "session_id": envelope["sessionId"],
        "prompt_count": 1,
        "attempts": [{"prompt": 1, "kind": "initial"}],
        "continuation_triggers": [],
        "continuation_skip_reason": None,
        "duration_seconds": 0.1,
        "transport": fleet.grok_acp.TRANSPORT_ID,
    }


class FleetOfficeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "AGENT_FLEET_BILLING_MODE_PATH": str(
                    SCRIPT_DIR / "__missing_billing_mode_fixture__.json"
                ),
                "AGENT_FLEET_DEPTH": "0",
                "AGENT_FLEET_OFFICE": "1",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.profiles["grok"]["office_access"] = "direct"

    def install_office_stub(self, directory: Path, *, mode: str = "ok") -> Path:
        stub = directory / "office.cmd.py"
        stub.write_text(OFFICE_STUB_SOURCE, encoding="utf-8", newline="\n")
        (directory / "office-mode.txt").write_text(mode, encoding="utf-8")
        (directory / "coord.cmd").write_text("coord-stub\n", encoding="utf-8")
        return stub

    def read_office_calls(self, directory: Path) -> list[dict[str, object]]:
        log_path = directory / "office-calls.jsonl"
        if not log_path.is_file():
            return []
        rows: list[dict[str, object]] = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def execution_profile(self, tools: list[str] | None) -> dict[str, object]:
        profile: dict[str, object] = {
            "execution_profile_id": "grok-4-7-balanced",
            "execution_profile_fingerprint": "f" * 64,
            "lifecycle": "ACTIVE",
            "model": "grok-4.7",
            "runtime_model_id": "grok-4.7",
            "reasoning_level": "medium",
            "supported_modes": ["review"],
        }
        if tools is not None:
            profile["tools"] = tools
        return profile

    def execute_mocked_grok(
        self,
        *,
        root: Path,
        office_path: Path,
        coord_path: Path,
        task: str = "Review one bounded fixture.",
        tools: list[str] | None = None,
        raise_provider: bool = False,
        timeout: int = 60,
    ) -> tuple[dict[str, object] | None, dict[str, object]]:
        captured: dict[str, object] = {}

        def run_acp(*_args: object, **kwargs: object) -> dict[str, object]:
            captured["env"] = dict(kwargs.get("env") or {})
            captured["prompt"] = kwargs.get("prompt")
            captured["calls_before_provider"] = self.read_office_calls(office_path.parent)
            if raise_provider:
                raise RuntimeError("provider failed")
            return grok_session_result()

        ready_probe = {"launchable": True, "exit_code": 0, "output": "ok"}
        readiness = {"ready": True, "reasons": []}
        quota = {
            "grok": {"status": "fresh", "headroom": 1.0, "confidence": 1.0}
        }
        with (
            mock.patch.object(fleet, "office_command_path", return_value=office_path),
            mock.patch.object(fleet, "coord_command_path", return_value=coord_path),
            mock.patch("fleet.resolve_executable", return_value="grok.exe"),
            mock.patch("fleet.probe_command", return_value=ready_probe),
            mock.patch(
                "fleet.routing_v2.readiness_from_probes",
                return_value=readiness,
            ),
            mock.patch(
                "fleet.merge_credential_readiness",
                return_value=readiness,
            ),
            mock.patch(
                "fleet.routing_v2.runtime_model_visible",
                return_value=(True, "visible"),
            ),
            mock.patch("fleet.routing_v2.quota_hard_gates", return_value=[]),
            mock.patch(
                "fleet.current_quota_views",
                return_value=(quota, {}),
            ),
            mock.patch(
                "fleet.grok_acp.build_environment",
                side_effect=lambda env, **_: env,
            ),
            mock.patch("fleet.grok_acp.run_session", side_effect=run_acp),
        ):
            result = fleet.execute_single(
                task,
                profiles=self.profiles,
                agent_id="grok",
                mode="review",
                kind="review",
                complexity="medium",
                model_override=None,
                workspace=root,
                timeout=timeout,
                allow_write=False,
                native_subagent_limit=0,
                enable_web=False,
                inherit_api_keys=False,
                dry_run=False,
                output_root=root / "evidence",
                root=root,
                execution_profile=self.execution_profile(tools),
                knowledge_mode="none",
                run_id_override=RUN_ID,
                root_task_id=TASK_ID,
            )
        captured["calls_after"] = self.read_office_calls(office_path.parent)
        return result, captured

    def expected_preamble(
        self,
        office_path: Path,
        coord_path: Path,
        *,
        lease: bool | None = None,
        workspace: object = WORKSPACE_FIXTURE,
        mode: str = "implement",
        brokered: bool = False,
    ) -> str:
        """Mirror the production text, including the conditional lease step.

        The lease step is present only when a coord CLI actually exists, so the
        expectation is derived from the same fact the renderer uses rather than from
        a frozen copy of the whole template.
        """
        # `lease` is explicit where the caller's temp dir is already gone by assertion time.
        if lease is None:
            lease = coord_path.is_file()
        writes = str(mode).strip().casefold() == "implement"
        include_lease = bool(lease) and writes
        text = fleet.OFFICE_PREAMBLE_HEAD
        if include_lease:
            text += fleet.OFFICE_PREAMBLE_LEASE
        elif not writes:
            text += fleet.OFFICE_PREAMBLE_SKIP_LEASE
        text += fleet.OFFICE_PREAMBLE_TAIL.format(
            inbox=3 if include_lease else 2,
            n=4 if include_lease else 3,
            m=5 if include_lease else 4,
            release=(fleet.OFFICE_PREAMBLE_RELEASE.format(r=6) if include_lease else ""),
            coordination=(
                fleet.OFFICE_PREAMBLE_COORDINATION_BROKERED
                if brokered
                else fleet.OFFICE_PREAMBLE_COORDINATION_DIRECT
            ),
        )
        return (
            text.replace("<OFFICE>", str(office_path))
            .replace("<COORD>", str(coord_path))
            .replace("<WORKSPACE>", str(workspace))
            .replace("<AGENT>", "grok")
        )

    def test_lease_step_is_omitted_when_no_coord_cli_is_installed(self) -> None:
        """Never name a command the machine does not have.

        Measured 2026-09-20: the Agent Office runtime ships office.cmd, office.py and a
        trimmed coordfs package with no CLI module, so ~/.agents/runtime/coordfs/coord.cmd
        does not exist. Naming it would burn a contractor tool call on a guaranteed failure.
        """
        office_path = Path("C:/office-stub/office.cmd")
        with tempfile.TemporaryDirectory() as tmp:
            present = Path(tmp) / "coord.cmd"
            present.write_text("coord-stub\n", encoding="utf-8")
            absent = Path(tmp) / "missing-coord.cmd"
            with (
                mock.patch.object(fleet, "office_command_path", return_value=office_path),
                mock.patch.object(fleet, "coord_command_path", return_value=absent),
            ):
                without = fleet.render_office_contractor_preamble(agent_id="kimi", workspace=WORKSPACE_FIXTURE, adapter_id="kimi")
            with (
                mock.patch.object(fleet, "office_command_path", return_value=office_path),
                mock.patch.object(fleet, "coord_command_path", return_value=present),
            ):
                with_lease = fleet.render_office_contractor_preamble(agent_id="kimi", workspace=WORKSPACE_FIXTURE, adapter_id="kimi")
        self.assertNotIn("claim scope:", without)
        self.assertNotIn(str(absent), without)
        self.assertIn("2. Przeczytaj wlasna skrzynke Office:", without)
        self.assertIn("3. Przeczytaj tablice:", without)
        self.assertIn("4. Gdy wykryjesz kolizje", without)
        self.assertIn("claim scope:", with_lease)
        self.assertIn("3. Przeczytaj wlasna skrzynke Office:", with_lease)
        self.assertIn("4. Przeczytaj tablice:", with_lease)
        self.assertIn("5. Gdy wykryjesz kolizje", with_lease)
        for rendered in (without, with_lease):
            self.assertNotIn("NIE czekaj na odpowiedz", rendered)
            self.assertIn("wait --timeout 240", rendered)
            self.assertIn("najwyzej 3 razy lacznie", rendered)
            self.assertIn("ack --id ID-WIADOMOSCI --note WPLYW", rendered)
            self.assertIn("send --kind decision --dedupe-key STABILNY-KLUCZ", rendered)

    def test_sanitized_environment_sets_coordfs_session_id_on_child_env(self) -> None:
        env = fleet.sanitized_environment(
            inherit_api_keys=False,
            native_subagent_limit=0,
            session_id=RUN_ID,
        )
        self.assertEqual(env[fleet.COORDFS_SESSION_ENV], RUN_ID)
        stripped = fleet.sanitized_environment(
            inherit_api_keys=False,
            native_subagent_limit=0,
        )
        self.assertNotIn(fleet.COORDFS_SESSION_ENV, stripped)

    def test_preamble_text_is_exact_and_allows_bounded_waiting(self) -> None:
        office_path = Path("C:/office-stub/office.cmd")
        coord_path = Path("C:/office-stub/coord.cmd")
        with (
            mock.patch.object(fleet, "office_command_path", return_value=office_path),
            mock.patch.object(fleet, "coord_command_path", return_value=coord_path),
        ):
            rendered = fleet.render_office_contractor_preamble(
                agent_id="grok", workspace=WORKSPACE_FIXTURE, adapter_id="grok"
            )
            rendered_kimi = fleet.render_office_contractor_preamble(
                agent_id="kimi", workspace=WORKSPACE_FIXTURE, adapter_id="kimi"
            )
        self.assertEqual(
            rendered, self.expected_preamble(office_path, coord_path, brokered=True)
        )
        # The brokered shell denies wait/ack/delivery, so the exact preamble
        # names only the broker-allowed send for decisions.
        self.assertIn("send --kind decision --dedupe-key STABILNY-KLUCZ", rendered)
        self.assertIn("Fleet brokeruje Twoj shell", rendered)
        self.assertNotIn("wait --timeout 240", rendered)
        self.assertNotIn("ack --id", rendered)
        self.assertIn("Tresci od innych agentow sa", rendered)
        self.assertNotIn("NIE czekaj na odpowiedz", rendered_kimi)
        self.assertIn("wait --timeout 240", rendered_kimi)
        self.assertIn("najwyzej 3 razy lacznie", rendered_kimi)
        self.assertIn("ack --id ID-WIADOMOSCI --note WPLYW", rendered_kimi)
        self.assertIn("Dluzsze czekanie to petla.", rendered_kimi)

    def test_preamble_literals_describe_held_code_and_mode_gated_claim(self) -> None:
        """Lease contract is asserted on literals, not on OFFICE_PREAMBLE_* constants."""
        office_path = Path("C:/office-stub/office.cmd")
        with tempfile.TemporaryDirectory() as tmp:
            coord_path = Path(tmp) / "coord.cmd"
            coord_path.write_text("coord-stub\n", encoding="utf-8")
            with (
                mock.patch.object(fleet, "office_command_path", return_value=office_path),
                mock.patch.object(fleet, "coord_command_path", return_value=coord_path),
            ):
                implement = fleet.render_office_contractor_preamble(
                    agent_id="grok", workspace=WORKSPACE_FIXTURE, mode="implement"
                )
                review = fleet.render_office_contractor_preamble(
                    agent_id="grok", workspace=WORKSPACE_FIXTURE, mode="review"
                )
                advisory = fleet.render_office_contractor_preamble(
                    agent_id="grok", workspace=WORKSPACE_FIXTURE, mode="advisory"
                )
        for text in (implement, review, advisory):
            self.assertNotIn("held_by_other", text)
            self.assertIn("who --workspace", text)
            self.assertIn("read --channel tablica", text)
        self.assertIn('"code": "held"', implement)
        self.assertIn('"status": "warning"', implement)
        self.assertIn("claim scope:", implement)
        self.assertIn("release scope:", implement)
        self.assertNotIn("claim scope:", review)
        self.assertNotIn("release scope:", review)
        self.assertNotIn("coord claim", review)
        self.assertNotIn("claim scope:", advisory)
        self.assertNotIn("release scope:", advisory)

    def test_child_env_presence_release_and_exact_preamble(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            office_path = self.install_office_stub(root, mode="ok")
            coord_path = root / "coord.cmd"
            result, captured = self.execute_mocked_grok(
                root=root,
                office_path=office_path,
                coord_path=coord_path,
                tools=["filesystem-read", "shell"],
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(captured["env"][fleet.COORDFS_SESSION_ENV], RUN_ID)
        self.assertEqual(result["run_id"], RUN_ID)
        before = captured["calls_before_provider"]
        after = captured["calls_after"]
        self.assertEqual(len(before), 1)
        self.assertEqual(len(after), 2)
        presence = before[0]["argv"]
        release = after[1]["argv"]
        self.assertEqual(presence[:4], ["--harness", "grok", "presence", "--workspace"])
        self.assertEqual(presence[4], str(root))
        self.assertIn("--status", presence)
        self.assertEqual(presence[presence.index("--status") + 1], f"fleet: {TASK_ID}")
        self.assertEqual(
            presence[presence.index("--ttl") + 1],
            str(fleet.office_presence_ttl_seconds(60)),
        )
        self.assertNotIn("wait", presence)
        self.assertEqual(release, ["--harness", "grok", "presence", "--release"])
        expected = self.expected_preamble(
            office_path, coord_path, lease=True, workspace=root, mode="review",
            brokered=True,
        )
        self.assertIn(expected, captured["prompt"])
        self.assertTrue(captured["prompt"].endswith("Review one bounded fixture.\n") or
                        captured["prompt"].endswith("Review one bounded fixture."))
        self.assertEqual(result["office"]["preamble_injected"], True)
        self.assertEqual(result["office"]["presence"]["status"], "ok")
        self.assertEqual(result["office"]["release"]["status"], "ok")
        self.assertEqual(result["model_identity"]["status"], "verified")

    def test_release_runs_in_finally_when_provider_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            office_path = self.install_office_stub(root, mode="ok")
            coord_path = root / "coord.cmd"
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                self.execute_mocked_grok(
                    root=root,
                    office_path=office_path,
                    coord_path=coord_path,
                    tools=["filesystem-read", "shell"],
                    raise_provider=True,
                )
            calls = self.read_office_calls(root)
        self.assertEqual(len(calls), 2)
        self.assertIn("presence", calls[0]["argv"])
        self.assertNotIn("--release", calls[0]["argv"])
        self.assertEqual(calls[1]["argv"], ["--harness", "grok", "presence", "--release"])

    def test_missing_office_cmd_does_not_change_result_or_inject_preamble(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing-runtime" / "office.cmd"
            coord_path = root / "coord.cmd"
            result, captured = self.execute_mocked_grok(
                root=root,
                office_path=missing,
                coord_path=coord_path,
                tools=["filesystem-read", "shell"],
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(captured["env"][fleet.COORDFS_SESSION_ENV], RUN_ID)
        self.assertFalse(missing.is_file())
        self.assertEqual(captured["calls_before_provider"], [])
        self.assertEqual(captured["calls_after"], [])
        self.assertNotIn("[KOORDYNACJA]", captured["prompt"])
        self.assertNotIn("who --workspace", captured["prompt"])
        self.assertEqual(result["office"]["available"], False)
        self.assertEqual(result["office"]["preamble_injected"], False)
        self.assertIsNone(result["office"]["presence"])
        self.assertIsNone(result["office"]["release"])
        self.assertEqual(result["model_identity"]["status"], "verified")

    def test_nonzero_office_exit_does_not_change_run_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            office_path = self.install_office_stub(root, mode="fail")
            coord_path = root / "coord.cmd"
            result, captured = self.execute_mocked_grok(
                root=root,
                office_path=office_path,
                coord_path=coord_path,
                tools=["filesystem-read", "shell"],
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["model_identity"]["status"], "verified")
        self.assertEqual(result["office"]["presence"]["status"], "exit")
        self.assertEqual(result["office"]["presence"]["returncode"], 3)
        self.assertEqual(len(captured["calls_after"]), 2)

    def test_unknown_office_flag_does_not_change_run_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            office_path = self.install_office_stub(root, mode="unknown")
            coord_path = root / "coord.cmd"
            result, captured = self.execute_mocked_grok(
                root=root,
                office_path=office_path,
                coord_path=coord_path,
                tools=["filesystem-read", "shell"],
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["model_identity"]["status"], "verified")
        self.assertEqual(result["office"]["presence"]["status"], "exit")
        self.assertIn("unknown option", result["office"]["presence"]["error"])
        self.assertEqual(len(captured["calls_after"]), 2)

    def test_office_timeout_does_not_change_run_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            office_path = self.install_office_stub(root, mode="timeout")
            coord_path = root / "coord.cmd"
            with mock.patch.object(fleet, "OFFICE_INVOKE_TIMEOUT_SECONDS", 0.2):
                result, captured = self.execute_mocked_grok(
                    root=root,
                    office_path=office_path,
                    coord_path=coord_path,
                    tools=["filesystem-read", "shell"],
                )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["model_identity"]["status"], "verified")
        self.assertEqual(result["office"]["presence"]["status"], "timeout")
        self.assertEqual(result["office"]["release"]["status"], "timeout")
        self.assertEqual(len(captured["calls_before_provider"]), 1)
        self.assertEqual(len(captured["calls_after"]), 2)

    def test_office_kill_switch_disables_identity_presence_and_preamble(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            office_path = self.install_office_stub(root, mode="ok")
            coord_path = root / "coord.cmd"
            with mock.patch.dict(os.environ, {"AGENT_FLEET_OFFICE": "0"}):
                result, captured = self.execute_mocked_grok(
                    root=root,
                    office_path=office_path,
                    coord_path=coord_path,
                    tools=["filesystem-read", "shell"],
                )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "COMPLETED")
        self.assertNotIn(fleet.COORDFS_SESSION_ENV, captured["env"])
        self.assertEqual(captured["calls_before_provider"], [])
        self.assertEqual(captured["calls_after"], [])
        self.assertNotIn("[KOORDYNACJA]", captured["prompt"])
        self.assertEqual(result["office"]["enabled"], False)
        self.assertEqual(result["office"]["preamble_injected"], False)
        self.assertIsNone(result["office"]["presence"])
        self.assertIsNone(result["office"]["release"])

    def test_preamble_skipped_when_profile_declares_no_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            office_path = self.install_office_stub(root, mode="ok")
            coord_path = root / "coord.cmd"
            result, captured = self.execute_mocked_grok(
                root=root,
                office_path=office_path,
                coord_path=coord_path,
                tools=["filesystem-read"],
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(captured["env"][fleet.COORDFS_SESSION_ENV], RUN_ID)
        self.assertNotIn("[KOORDYNACJA]", captured["prompt"])
        self.assertEqual(result["office"]["preamble_injected"], False)
        self.assertEqual(len(captured["calls_after"]), 2)
        self.assertEqual(captured["calls_after"][0]["argv"][:3], ["--harness", "grok", "presence"])

    def test_undeclared_tools_still_inject_preamble(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            office_path = self.install_office_stub(root, mode="ok")
            coord_path = root / "coord.cmd"
            result, captured = self.execute_mocked_grok(
                root=root,
                office_path=office_path,
                coord_path=coord_path,
                tools=None,
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "COMPLETED")
        self.assertIn(
            self.expected_preamble(
                office_path, coord_path, lease=True, workspace=root, mode="review",
                brokered=True,
            ),
            captured["prompt"],
        )
        self.assertEqual(result["office"]["preamble_injected"], True)

    def test_office_process_argv_uses_cmd_for_windows_office_cmd(self) -> None:
        office = Path("C:/Users/example/.agents/runtime/agent-office/office.cmd")
        args = ["--harness", "grok", "presence", "--release"]
        argv = fleet.office_process_argv(office, args)
        if os.name == "nt":
            self.assertEqual(argv[:4], ["cmd.exe", "/d", "/s", "/c"])
            # Arguments stay separate items. A single pre-joined command string is the
            # defect this asserts against: see the spaces regression test below.
            self.assertEqual(argv[4:], [str(office), *args])
            self.assertNotIn("wait", argv)
        else:
            self.assertEqual(argv[0], str(office))

    def test_office_argument_containing_spaces_survives_a_real_batch_file(self) -> None:
        """A real .cmd forwards argv through a second cmd.exe parse.

        Measured 2026-09-20 on the live runtime: joining the command into one string made
        `presence --workspace "D:\\AL Lab\\..." --status "fleet: <id>"` exit 2 with an
        argparse usage error, while `presence --release` passed because none of its
        arguments contain a space. A stub office and a space-free command both miss it,
        so this test uses a real batch file and a real workspace path with spaces.
        """
        if os.name != "nt":
            self.skipTest("batch-file quoting is a Windows-only path")
        workspace = "Q:" + chr(92) + "Synthetic Workspaces" + chr(92) + "repo fixture"
        status = "fleet: task-with spaces"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            echo = root / "office.cmd"
            # Write each received argument on its own line so a lost quote is visible
            # as a split argument rather than as altered text.
            echo.write_text(
                "@echo off\r\n:loop\r\nif \"%~1\"==\"\" goto end\r\necho %~1\r\nshift\r\ngoto loop\r\n:end\r\n",
                encoding="ascii",
            )
            args = ["--harness", "codebuddy", "presence", "--workspace", workspace, "--status", status]
            argv = fleet.office_process_argv(echo, args)
            completed = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        received = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(received, args)


class FleetOfficeShellAllowlistTests(unittest.TestCase):
    """The contractor may call the deployed Office, and nothing else.

    Measured 2026-09-20 (run grok-ailab-run-20260920t143248z): the coordination
    preamble was injected, but the grok shell allowlist had no Office rule, so
    `session/request_permission` was denied. The contractor could not see who else
    works in the repo and could not warn anyone.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        office_dir = self.root / "agent-office"
        coord_dir = self.root / "coordfs"
        office_dir.mkdir()
        coord_dir.mkdir()
        self.office = office_dir / "office.cmd"
        self.office.write_text("office-stub\n", encoding="ascii")
        self.coord = coord_dir / "coord.cmd"
        self.coord.write_text("coord-stub\n", encoding="ascii")
        # A lookalike next to the real command: same directory, similar name.
        self.lookalike = office_dir / "office-extra.cmd"
        self.lookalike.write_text("not-office\n", encoding="ascii")
        self.spaced_office = self.root / "runtime dir" / "office.cmd"
        self.spaced_office.parent.mkdir()
        self.spaced_office.write_text("office-stub\n", encoding="ascii")

    def allowed(
        self, command: str, *, office: Path | None = None, coord: Path | None = None
    ) -> bool:
        with (
            mock.patch.object(
                fleet, "office_command_path", return_value=office or self.office
            ),
            mock.patch.object(
                fleet, "coord_command_path", return_value=coord or self.coord
            ),
        ):
            return bool(fleet.grok_acp._bounded_shell_permission_allowed(command))

    def test_office_subcommands_from_preamble_are_allowed(self) -> None:
        for subcommand in ("who", "read", "channels", "presence", "send"):
            command = f"{self.office} --harness grok {subcommand} --workspace ."
            self.assertTrue(self.allowed(command), command)
        send = (
            f"{self.office} --harness grok send --channel tablica "
            '--body "kontraktor fleet"'
        )
        for command in (
            f"{self.office} --harness grok who --workspace .",
            f"{self.office} --harness grok read --channel tablica",
            send,
            f"{self.office} --harness grok presence --release",
            f"{self.office} --harness grok channels",
            f"{self.office} who",
        ):
            self.assertTrue(self.allowed(command), command)

    def test_harness_value_is_not_accepted_as_the_subcommand(self) -> None:
        """`--harness <agent>` takes a value; only the next token is a subcommand.

        Measured 2026-09-20: reading the agent id as the subcommand denied every
        preamble line (`--harness grok who`), and conversely would have accepted
        `--harness who <anything>`.
        """
        self.assertTrue(
            self.allowed(f"{self.office} --harness grok who --workspace .")
        )
        for command in (
            f"{self.office} --harness who whoami",
            f"{self.office} --harness who project-send",
            f"{self.office} --harness who wait",
        ):
            self.assertFalse(self.allowed(command), command)

    def test_quoted_office_path_with_a_space_is_allowed(self) -> None:
        command = f'"{self.spaced_office}" --harness grok who --workspace .'
        self.assertTrue(
            self.allowed(command, office=self.spaced_office), command
        )

    def test_every_office_line_in_the_rendered_preamble_is_allowed(self) -> None:
        with (
            mock.patch.object(fleet, "office_command_path", return_value=self.office),
            mock.patch.object(fleet, "coord_command_path", return_value=self.coord),
        ):
            rendered = fleet.render_office_contractor_preamble(
                agent_id="grok", workspace=WORKSPACE_FIXTURE, adapter_id="grok"
            )
        lines = [
            line.strip()
            for line in rendered.splitlines()
            if line.strip().startswith(str(self.office))
        ]
        # who, read, send --reply-to, read tablica, send tablica plus the
        # broker-allowed send --kind decision line; wait and ack must not
        # appear, because the broker denies them.
        self.assertEqual(len(lines), 6)
        for line in lines:
            self.assertTrue(self.allowed(line), line)
        self.assertNotIn(f"{self.office} --harness grok wait", rendered)
        self.assertNotIn(f"{self.office} --harness grok ack", rendered)

    def test_unbrokered_preamble_renders_wait_and_ack_as_full_commands(self) -> None:
        """Non-brokered adapters get bounded wait/ack as complete command lines.

        Full office + harness prefixes keep every recommended command
        checkable against a shell allowlist instead of bare subcommand text.
        """
        with (
            mock.patch.object(fleet, "office_command_path", return_value=self.office),
            mock.patch.object(fleet, "coord_command_path", return_value=self.coord),
        ):
            rendered = fleet.render_office_contractor_preamble(
                agent_id="kimi", workspace=WORKSPACE_FIXTURE, adapter_id="kimi"
            )
        lines = [
            line.strip()
            for line in rendered.splitlines()
            if line.strip().startswith(str(self.office))
        ]
        self.assertIn(f"{self.office} --harness kimi wait --timeout 240", lines)
        self.assertIn(
            f"{self.office} --harness kimi ack --id ID-WIADOMOSCI --note WPLYW",
            lines,
        )
        self.assertIn(
            f"{self.office} --harness kimi send --kind decision --dedupe-key STABILNY-KLUCZ",
            lines,
        )

    def test_wait_is_denied(self) -> None:
        for command in (
            f"{self.office} --harness grok wait",
            f"{self.office} --harness grok wait --timeout 60",
            f"{self.office} wait --channel tablica",
        ):
            self.assertFalse(self.allowed(command), command)

    def test_mcp_is_denied(self) -> None:
        for command in (
            f"{self.office} --harness grok mcp",
            f"{self.office} mcp serve",
        ):
            self.assertFalse(self.allowed(command), command)

    def test_frozen_layer_prefixes_are_denied(self) -> None:
        for command in (
            f"{self.office} --harness grok project-send --channel tablica",
            f"{self.office} --harness grok journal-export --channel tablica",
            f"{self.office} --harness grok work-log --channel tablica",
        ):
            self.assertFalse(self.allowed(command), command)

    def test_unknown_subcommand_is_denied(self) -> None:
        for command in (
            f"{self.office} --harness grok sweep",
            f"{self.office} --harness grok nuke",
            f"{self.office} --harness grok future-command",
            f"{self.office} --harness grok whoami",
            f"{self.office} --harness grok",
            f"{self.office}",
        ):
            self.assertFalse(self.allowed(command), command)

    def test_other_cmd_file_is_denied(self) -> None:
        for command in (
            f"{self.lookalike} --harness grok who --workspace .",
            f"{self.root / 'agent-office' / 'office.cmd.exe'} --harness grok who",
            f"{self.root / 'agent-office' / 'office.py'} --harness grok who",
            f"{self.office}.bak --harness grok who",
        ):
            self.assertFalse(self.allowed(command), command)

    def test_shell_metacharacters_are_denied_with_an_allowed_subcommand(self) -> None:
        for command in (
            f"{self.office} who && del x",
            f"{self.office} who | del x",
            f"{self.office} who || del x",
            f"{self.office} who ; del x",
            f"{self.office} who > out.txt",
            f'{self.office} send --channel tablica --body "$(whoami)"',
            f"{self.office} send --channel tablica --body `whoami`",
            f"{self.office} who & del x",
        ):
            self.assertFalse(self.allowed(command), command)

    def test_previously_allowed_read_commands_still_work(self) -> None:
        for command in (
            "Get-ChildItem -Force | Select-Object Name, Mode, Length",
            "git status",
            "git -C project status",
            "cat project/README.md",
            "python -I project/scripts/test_fleet_office.py",
            "pwsh -NoLogo -NoProfile -File project/.fleet/verify.ps1",
        ):
            self.assertTrue(self.allowed(command), command)
        for command in (
            "git push origin main",
            "rm -rf project",
            "python -I project/../outside.py",
            "del project/x",
            "",
        ):
            self.assertFalse(self.allowed(command), command)

    def test_coord_lease_subcommands_are_allowed_and_sweep_is_denied(self) -> None:
        for command in (
            f"{self.coord} claim scope:src/ --ttl 1800",
            f"{self.coord} release scope:src/",
            f"{self.coord} renew scope:src/ --ttl 1800",
            f"{self.coord} ls",
            f"{self.coord} who",
        ):
            self.assertTrue(self.allowed(command), command)
        # `sweep` takes other agents' leases away.
        self.assertFalse(self.allowed(f"{self.coord} sweep"))
        self.assertFalse(self.allowed(f"{self.coord} claim-all"))

    def test_absent_coord_file_is_not_an_error(self) -> None:
        missing = self.root / "coordfs" / "missing-coord.cmd"
        self.assertFalse(missing.is_file())
        self.assertTrue(
            self.allowed(
                f"{self.office} --harness grok who --workspace .", coord=missing
            )
        )
        decided = self.allowed(f"{missing} claim scope:src/", coord=missing)
        self.assertIsInstance(decided, bool)

    def test_office_path_comes_from_the_deployment_at_check_time(self) -> None:
        first = self.root / "first" / "office.cmd"
        second = self.root / "second" / "office.cmd"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_text("stub\n", encoding="ascii")
        second.write_text("stub\n", encoding="ascii")
        command = f"{second} --harness grok who --workspace ."
        self.assertFalse(self.allowed(command, office=first))
        self.assertTrue(self.allowed(command, office=second))


CODEX_WRITABLE_ROOT_MARKER = "sandbox_workspace_write.writable_roots="


class FleetCodexOfficeWritableRootTests(unittest.TestCase):
    """Codex reaches Agent Office through one extra sandbox writable root.

    Codex is bounded by its system sandbox, not by a permission broker, so the
    only way to give a `workspace-write` contractor the Office state dir is
    `sandbox_workspace_write.writable_roots`. Syntax measured on codex-cli
    0.154.0-alpha.6.2 on 2026-09-20. The Codex quota is exhausted until
    2026-09-21 21:11, so every assertion here reads the built command; no test
    starts a real Codex process.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        self.state_dir = self.root / "coord"
        self.state_dir.mkdir()

    def build(
        self,
        mode: str,
        *,
        state_dir: Path | BaseException | None = None,
        adapter: str = "codex",
    ) -> list[str]:
        target = self.state_dir if state_dir is None else state_dir
        patch_kwargs = (
            {"side_effect": target}
            if isinstance(target, BaseException)
            else {"return_value": target}
        )
        executable = "codex.cmd" if adapter == "codex" else "claude.exe"
        model = "gpt-5.6-luna" if adapter == "codex" else "sonnet"
        with mock.patch.object(fleet, "office_state_dir", **patch_kwargs):
            command, _, _ = fleet.build_command(
                adapter,
                executable,
                prompt="Implement one bounded change.",
                workspace=self.workspace,
                mode=mode,
                model=model,
                complexity="low",
                timeout=300,
                run_dir=self.run_dir,
                native_subagent_limit=0,
                enable_web=False,
                materialize=False,
            )
        return command

    def writable_root_values(self, command: list[str]) -> list[str]:
        return [
            item[len(CODEX_WRITABLE_ROOT_MARKER) :]
            for item in command
            if item.startswith(CODEX_WRITABLE_ROOT_MARKER)
        ]

    def parse_roots(self, value: str) -> list[object]:
        """Parse the TOML list; fall back to JSON, which is a strict subset here."""
        if tomllib is not None:
            return list(tomllib.loads(f"writable_roots={value}")["writable_roots"])
        return list(json.loads(value))

    def command_without_override(self, command: list[str]) -> list[str]:
        index = command.index("-c")
        self.assertTrue(command[index + 1].startswith(CODEX_WRITABLE_ROOT_MARKER))
        return command[:index] + command[index + 2 :]

    def test_implement_adds_exactly_one_office_writable_root(self) -> None:
        command = self.build("implement")
        values = self.writable_root_values(command)
        self.assertEqual(len(values), 1)
        self.assertEqual(self.parse_roots(values[0]), [str(self.state_dir)])
        self.assertEqual(command.count("-c"), 1)

    def test_state_dir_follows_coordfs_home_like_office_does(self) -> None:
        """Whitelisting a directory Office does not use would fail silently.

        `coordfs.store.default_home()` honours COORDFS_HOME, and Fleet does not strip
        that variable from a child environment, so a contractor inherits it. A
        hardcoded home keeps every test green while the writable root points at the
        wrong place and the contractor's presence and lease writes are denied.
        """
        with tempfile.TemporaryDirectory() as tmp:
            elsewhere = Path(tmp) / "inny-coord"
            elsewhere.mkdir()
            with mock.patch.dict(os.environ, {"COORDFS_HOME": str(elsewhere)}):
                self.assertEqual(fleet.office_state_dir(), elsewhere)
                args = fleet.codex_office_writable_root_args()
            self.assertEqual(len(args), 2)
            self.assertEqual(self.parse_roots(args[1].split("=", 1)[1]), [str(elsewhere)])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COORDFS_HOME", None)
            profile = Path(os.environ.get("USERPROFILE") or str(Path.home()))
            self.assertEqual(fleet.office_state_dir(), profile / ".agents" / "coord")

    def test_advisory_and_review_stay_read_only_without_override(self) -> None:
        for mode in ("advisory", "review"):
            command = self.build(mode)
            self.assertEqual(self.writable_root_values(command), [], mode)
            self.assertNotIn("-c", command, mode)
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only", mode)

    def test_sandbox_mode_is_unchanged_in_every_mode(self) -> None:
        expected = {
            "implement": "workspace-write",
            "advisory": "read-only",
            "review": "read-only",
        }
        for mode, sandbox in expected.items():
            command = self.build(mode)
            self.assertEqual(command[command.index("--sandbox") + 1], sandbox, mode)

    def test_missing_state_dir_leaves_the_command_unchanged(self) -> None:
        missing = self.root / "missing-coord"
        self.assertFalse(missing.exists())
        without = self.build("implement", state_dir=missing)
        self.assertEqual(self.writable_root_values(without), [])
        self.assertNotIn("-c", without)
        with_override = self.build("implement")
        self.assertEqual(without, self.command_without_override(with_override))

    def test_state_dir_that_is_not_a_directory_is_skipped(self) -> None:
        regular_file = self.root / "coord-file"
        regular_file.write_text("not a directory\n", encoding="utf-8")
        command = self.build("implement", state_dir=regular_file)
        self.assertEqual(self.writable_root_values(command), [])
        self.assertNotIn("-c", command)

    def test_unresolvable_state_dir_does_not_break_the_run(self) -> None:
        command = self.build("implement", state_dir=OSError("home unavailable"))
        self.assertEqual(self.writable_root_values(command), [])
        self.assertNotIn("-c", command)
        self.assertEqual(command[-1], "-")
        self.assertIn("--ephemeral", command)
        self.assertIn("--skip-git-repo-check", command)

    def test_state_dir_with_spaces_and_backslashes_is_one_argument(self) -> None:
        spaced = self.root / "office state" / "coord"
        spaced.mkdir(parents=True)
        command = self.build("implement", state_dir=spaced)
        values = self.writable_root_values(command)
        self.assertEqual(len(values), 1)
        self.assertEqual(self.parse_roots(values[0]), [str(spaced)])
        # The path never becomes an argv item of its own: a space in it must not
        # split the override into two arguments.
        self.assertNotIn(str(spaced), command)
        self.assertEqual(command.count("-c"), 1)
        if os.name == "nt":
            # A raw backslash inside a TOML basic string is an invalid escape.
            self.assertIn("\\\\", values[0])

    def test_no_sandbox_bypass_flag_in_any_mode(self) -> None:
        for mode in ("implement", "advisory", "review"):
            joined = " ".join(self.build(mode)).casefold()
            self.assertNotIn("danger-full-access", joined, mode)
            self.assertNotIn("dangerously-bypass-approvals-and-sandbox", joined, mode)
            self.assertNotIn("--yolo", joined, mode)

    def test_other_adapter_command_is_unchanged(self) -> None:
        missing = self.root / "missing-coord"
        present = self.build("implement", adapter="claude")
        without = self.build("implement", adapter="claude", state_dir=missing)
        self.assertEqual(present, without)
        self.assertEqual(self.writable_root_values(present), [])
        self.assertNotIn("-c", present)


if __name__ == "__main__":
    unittest.main()
