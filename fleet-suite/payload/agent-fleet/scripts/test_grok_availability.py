from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import grok_acp
import grok_availability


def make_envelope(
    *,
    session_id: str = "session-123",
    stop_reason: str = "end_turn",
    text: str = "OK",
    usage: dict[str, Any] | None = None,
    tool_calls: Any = 0,
    permission_requests_approved: Any = 0,
    ambient_mcp_servers: Any = 0,
    has_fleet: bool = True,
) -> str:
    envelope: dict[str, Any] = {
        "sessionId": session_id,
        "stopReason": stop_reason,
        "text": text,
    }
    if usage is not None:
        envelope["usage"] = usage
    if has_fleet:
        envelope["_fleet"] = {
            "tool_calls": tool_calls,
            "permission_requests_approved": permission_requests_approved,
            "ambient_mcp_servers": ambient_mcp_servers,
        }
    return json.dumps(envelope)


def make_result(
    *,
    stdout: str | None = None,
    exit_code: Any = 0,
    prompt_count: Any = 1,
    model_tool_calls: Any = 0,
    timed_out: bool = False,
    transport: str = grok_acp.TRANSPORT_ID,
    session_id: str = "session-123",
    quota_exhausted: bool = False,
) -> dict[str, Any]:
    if stdout is None:
        stdout = make_envelope(session_id=session_id)
    return {
        "exit_code": exit_code,
        "prompt_count": prompt_count,
        "model_tool_calls": model_tool_calls,
        "timed_out": timed_out,
        "transport": transport,
        "session_id": session_id,
        "quota_exhausted": quota_exhausted,
        "stdout": stdout,
        "stderr": "",
    }


class GrokAvailabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profile: dict[str, Any] = {
            "adapter": "grok",
            "models": {"low": "grok-4.7"},
        }
        self.fleet = mock.MagicMock()
        self.fleet.resolve_executable.return_value = "grok.exe"
        self.fleet.profile_runtime_environment.return_value = {"CLEAN_ENV": "1"}
        self.fleet.grok_model_identity.return_value = {
            "status": "verified",
            "model_id": "grok-4.7",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_successful_probe_proves_contract_and_retention(self) -> None:
        seen_build_args: dict[str, Any] = {}
        seen_run_args: dict[str, Any] = {}

        def fake_build_command(executable: str, **kwargs: Any) -> list[str]:
            seen_build_args.update(kwargs)
            return ["grok.exe", "stdio"]

        def fake_run_session(command: list[str], **kwargs: Any) -> dict[str, Any]:
            seen_run_args.update(kwargs)
            # Prove workspace is an empty directory
            workspace = kwargs["workspace"]
            self.assertTrue(workspace.is_dir())
            self.assertEqual(list(workspace.iterdir()), [])
            return make_result(
                stdout=make_envelope(usage={"totalTokens": 42})
            )

        with (
            mock.patch.object(grok_acp, "build_command", side_effect=fake_build_command),
            mock.patch.object(grok_acp, "run_session", side_effect=fake_run_session),
        ):
            row = grok_availability.native_probe(self.fleet, self.profile, self.root)

        self.assertEqual(row["status"], "available")
        self.assertEqual(row["source"], "native-cli-check")
        self.assertEqual(row["covered_scopes"], ["credits"])
        self.assertEqual(row["probe_tokens"], 42)

        # Verify environment call: no API env
        self.fleet.profile_runtime_environment.assert_called_once_with(
            self.profile, inherit_api_keys=False, native_subagent_limit=0
        )

        # Verify build_command contract: zero tools advertised, empty schema
        self.assertEqual(seen_build_args["mode"], "availability")
        self.assertEqual(seen_build_args["model"], "grok-4.7")
        self.assertEqual(seen_build_args["handoff_schema"], {})
        self.assertEqual(seen_build_args["max_model_tool_calls"], 1)

        # Verify run_session contract: fixed prompt, availability mode, retention
        self.assertEqual(seen_run_args["mode"], "availability")
        self.assertEqual(seen_run_args["prompt"], "Reply OK only. Do not use tools.")
        self.assertEqual(seen_run_args["timeout"], 25)
        self.assertEqual(seen_run_args["max_model_tool_calls"], 1)
        self.assertEqual(seen_run_args["max_additional_prompts"], 0)
        self.assertTrue(seen_run_args["retain_runtime"])

        # Verify retention: attempt directory under availability-probes was not deleted
        probe_dirs = list((self.root / "availability-probes").iterdir())
        self.assertEqual(len(probe_dirs), 1)
        attempt_dir = probe_dirs[0]
        self.assertTrue(attempt_dir.is_dir())
        self.assertTrue((attempt_dir / "run").is_dir())
        self.assertTrue((attempt_dir / "empty-workspace").is_dir())
        self.assertTrue((attempt_dir / "proxy").is_dir())

        # Verify metadata written by helper: allowlisted fields only
        meta_file = attempt_dir / "probe-metadata.json"
        self.assertTrue(meta_file.is_file())
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "available")
        self.assertEqual(meta["model"], "grok-4.7")
        self.assertEqual(meta["probe_tokens"], 42)
        for forbidden in ("prompt", "auth", "stdout", "stderr", "thought"):
            self.assertNotIn(forbidden, meta)

    def test_retention_on_failure(self) -> None:
        def fake_run_session(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return make_result(exit_code=1)

        with (
            mock.patch.object(grok_acp, "run_session", side_effect=fake_run_session),
        ):
            with self.assertRaisesRegex(ValueError, "exited non-zero"):
                grok_availability.native_probe(self.fleet, self.profile, self.root)

        # Attempt tree must still be retained on failure
        probe_dirs = list((self.root / "availability-probes").iterdir())
        self.assertEqual(len(probe_dirs), 1)
        attempt_dir = probe_dirs[0]
        self.assertTrue(attempt_dir.is_dir())

        meta_file = attempt_dir / "probe-metadata.json"
        self.assertTrue(meta_file.is_file())
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["error_type"], "ValueError")

    def test_quota_exhausted_outcome(self) -> None:
        def fake_run_session(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return make_result(quota_exhausted=True)

        with mock.patch.object(grok_acp, "run_session", side_effect=fake_run_session):
            row = grok_availability.native_probe(self.fleet, self.profile, self.root)

        self.assertEqual(
            row,
            {
                "status": "exhausted",
                "source": "native-cli-check",
                "scope": "credits",
                "reset_at": None,
                "clear_requires": "native-cli-check",
            },
        )
        probe_dirs = list((self.root / "availability-probes").iterdir())
        self.assertEqual(len(probe_dirs), 1)
        meta_file = probe_dirs[0] / "probe-metadata.json"
        self.assertTrue(meta_file.is_file())
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "exhausted")

    def test_unknown_and_nonpositive_usage(self) -> None:
        cases = [
            None,
            {},
            {"totalTokens": 0},
            {"totalTokens": -5},
            {"totalTokens": float("nan")},
            {"totalTokens": float("inf")},
            {"totalTokens": True},
            {"totalTokens": "50"},
        ]
        for usage in cases:
            def fake_run_session(*_args: Any, u=usage, **_kwargs: Any) -> dict[str, Any]:
                return make_result(stdout=make_envelope(usage=u))

            with mock.patch.object(grok_acp, "run_session", side_effect=fake_run_session):
                row = grok_availability.native_probe(self.fleet, self.profile, self.root)
            self.assertEqual(row["status"], "available")
            self.assertNotIn("probe_tokens", row)

    def test_positive_finite_token_usage_recorded(self) -> None:
        for total in (1, 105, 12.5):
            def fake_run_session(*_args: Any, t=total, **_kwargs: Any) -> dict[str, Any]:
                return make_result(stdout=make_envelope(usage={"totalTokens": t}))

            with mock.patch.object(grok_acp, "run_session", side_effect=fake_run_session):
                row = grok_availability.native_probe(self.fleet, self.profile, self.root)
            self.assertEqual(row["probe_tokens"], total)

    def _assert_probe_fails(self, result: dict[str, Any], regex: str) -> None:
        with mock.patch.object(grok_acp, "run_session", return_value=result):
            with self.assertRaisesRegex(ValueError, regex):
                grok_availability.native_probe(self.fleet, self.profile, self.root)

    def test_failure_exit_code_nonzero(self) -> None:
        self._assert_probe_fails(make_result(exit_code=1), "exited non-zero")
        self._assert_probe_fails(make_result(exit_code=70), "exited non-zero")

    def test_failure_exit_code_boolean(self) -> None:
        self._assert_probe_fails(make_result(exit_code=False), "exited non-zero")

    def test_failure_prompt_count_not_one(self) -> None:
        self._assert_probe_fails(make_result(prompt_count=0), "exactly one prompt")
        self._assert_probe_fails(make_result(prompt_count=2), "exactly one prompt")

    def test_failure_prompt_count_boolean(self) -> None:
        self._assert_probe_fails(make_result(prompt_count=True), "exactly one prompt")

    def test_failure_model_tool_calls_nonzero(self) -> None:
        self._assert_probe_fails(make_result(model_tool_calls=1), "unexpected model tool call")

    def test_failure_model_tool_calls_boolean(self) -> None:
        self._assert_probe_fails(make_result(model_tool_calls=False), "unexpected model tool call")

    def test_failure_fleet_tool_calls_nonzero(self) -> None:
        stdout = make_envelope(tool_calls=1)
        self._assert_probe_fails(make_result(stdout=stdout), "unexpected tool call")
        stdout_bool = make_envelope(tool_calls=False)
        self._assert_probe_fails(make_result(stdout=stdout_bool), "unexpected tool call")

    def test_failure_permission_approved_nonzero(self) -> None:
        stdout = make_envelope(permission_requests_approved=1)
        self._assert_probe_fails(make_result(stdout=stdout), "approved a tool permission")
        stdout_bool = make_envelope(permission_requests_approved=False)
        self._assert_probe_fails(make_result(stdout=stdout_bool), "approved a tool permission")

    def test_failure_ambient_mcp_servers_nonzero(self) -> None:
        stdout = make_envelope(ambient_mcp_servers=1)
        self._assert_probe_fails(make_result(stdout=stdout), "ambient MCP servers")
        stdout_bool = make_envelope(ambient_mcp_servers=False)
        self._assert_probe_fails(make_result(stdout=stdout_bool), "ambient MCP servers")

    def test_failure_missing_fleet_metadata(self) -> None:
        stdout = make_envelope(has_fleet=False)
        self._assert_probe_fails(make_result(stdout=stdout), "missing _fleet metadata")

    def test_failure_invalid_json_envelope(self) -> None:
        self._assert_probe_fails(make_result(stdout="not-json"), "envelope is invalid")

    def test_failure_non_dict_envelope(self) -> None:
        self._assert_probe_fails(make_result(stdout=json.dumps([1, 2, 3])), "envelope is not an object")

    def test_failure_session_id_missing(self) -> None:
        self._assert_probe_fails(make_result(session_id=""), "no session ID")

    def test_failure_session_id_mismatch(self) -> None:
        stdout = make_envelope(session_id="session-different")
        self._assert_probe_fails(make_result(session_id="session-123", stdout=stdout), "session ID mismatch")

    def test_failure_stop_reason_not_end_turn(self) -> None:
        for bad_stop in ("max_tokens", "error", "tool_use"):
            stdout = make_envelope(stop_reason=bad_stop)
            self._assert_probe_fails(make_result(stdout=stdout), "did not report end_turn")

    def test_failure_model_identity_unverified(self) -> None:
        self.fleet.grok_model_identity.return_value = {
            "status": "unverified",
            "error": "wrong model id",
        }
        self._assert_probe_fails(make_result(), "model identity unverified")

    def test_failure_assistant_text_not_ok(self) -> None:
        for bad_text in ("DONE", "OK ", "OK\n", "", "Here is OK", "{\"status\":\"ok\"}"):
            stdout = make_envelope(text=bad_text)
            self._assert_probe_fails(make_result(stdout=stdout), "exactly 'OK'")

    def test_failure_timed_out(self) -> None:
        self._assert_probe_fails(make_result(timed_out=True), "timed out")

    def test_failure_unexpected_transport(self) -> None:
        self._assert_probe_fails(make_result(transport="native-transport"), "unexpected transport")

    def test_failure_missing_executable(self) -> None:
        self.fleet.resolve_executable.return_value = None
        with self.assertRaisesRegex(ValueError, "executable not found"):
            grok_availability.native_probe(self.fleet, self.profile, self.root)

    def test_failure_environment_error(self) -> None:
        with mock.patch.object(
            grok_acp, "build_environment", side_effect=grok_acp.GrokAcpError("env bomb")
        ):
            with self.assertRaisesRegex(ValueError, "environment failed: env bomb"):
                grok_availability.native_probe(self.fleet, self.profile, self.root)


if __name__ == "__main__":
    unittest.main()
