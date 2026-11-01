from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import grok_acp

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


class GrokHomeIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_id = "run-20261025T000000Z-1234567890"
        self.run_dir = self.root / self.run_id
        self.workspace = self.root / "workspace"
        self.proxy_base = self.root / "proxy"
        self.auth_path = self.root / "auth.json"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.isolated_home = grok_acp.safe_home_path(self.run_dir)
        (self.workspace / "fixture.txt").write_text("bounded", encoding="utf-8")
        self.auth_path.write_text('{"token":"synthetic-fixture-only"}', encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_stale_home_inputs_pinned_to_isolated_home(self) -> None:
        base_env = {
            "HOME": "C:\\stale\\home",
            "USERPROFILE": "C:\\stale\\userprofile",
        }
        env = grok_acp.build_environment(
            base_env, run_dir=self.run_dir, auth_path=self.auth_path
        )
        self.assertEqual(env["HOME"], str(self.isolated_home))
        self.assertEqual(env["USERPROFILE"], str(self.isolated_home))
        self.assertEqual(env["GROK_HOME"], str(self.isolated_home))

    def test_absent_home_inputs_pinned_to_isolated_home(self) -> None:
        base_env: dict[str, str] = {}
        env = grok_acp.build_environment(
            base_env, run_dir=self.run_dir, auth_path=self.auth_path
        )
        self.assertEqual(env["HOME"], str(self.isolated_home))
        self.assertEqual(env["USERPROFILE"], str(self.isolated_home))
        self.assertEqual(env["GROK_HOME"], str(self.isolated_home))

    def test_mixed_case_windows_home_inputs_stripped_and_pinned(self) -> None:
        base_env = {
            "home": "C:\\stale\\lower",
            "UserProfile": "C:\\stale\\mixed",
            "Home": "C:\\stale\\title",
            "userprofile": "C:\\stale\\lowerprof",
        }
        env = grok_acp.build_environment(
            base_env, run_dir=self.run_dir, auth_path=self.auth_path
        )
        self.assertEqual(env["HOME"], str(self.isolated_home))
        self.assertEqual(env["USERPROFILE"], str(self.isolated_home))
        for variant in ("home", "UserProfile", "Home", "userprofile"):
            self.assertNotIn(variant, env)

    def test_caller_input_dict_preserved_unmutated(self) -> None:
        base_env = {
            "HOME": "C:\\original\\home",
            "USERPROFILE": "C:\\original\\profile",
            "home": "C:\\original\\lower",
            "GROK_API_KEY": "must-strip-from-output",
            "CUSTOM_VAR": "keep",
        }
        snapshot = dict(base_env)
        env = grok_acp.build_environment(
            base_env, run_dir=self.run_dir, auth_path=self.auth_path
        )
        self.assertEqual(base_env, snapshot)
        self.assertEqual(env["CUSTOM_VAR"], "keep")

    def test_explicit_subscription_reference_preserved_as_original_path(self) -> None:
        env = grok_acp.build_environment(
            {}, run_dir=self.run_dir, auth_path=self.auth_path
        )
        self.assertEqual(env["GROK_AUTH_PATH"], str(self.auth_path.resolve()))
        self.assertNotEqual(Path(env["GROK_AUTH_PATH"]).parent, self.isolated_home)

    def test_env_subscription_reference_preserved_including_mixed_case(self) -> None:
        env = grok_acp.build_environment(
            {"GROK_AUTH_PATH": str(self.auth_path)}, run_dir=self.run_dir
        )
        self.assertEqual(env["GROK_AUTH_PATH"], str(self.auth_path.resolve()))

        mixed_env = grok_acp.build_environment(
            {"grok_auth_path": str(self.auth_path)}, run_dir=self.run_dir
        )
        self.assertEqual(mixed_env["GROK_AUTH_PATH"], str(self.auth_path.resolve()))
        self.assertNotIn("grok_auth_path", mixed_env)

    def test_child_subprocess_path_home_resolves_to_isolated_home(self) -> None:
        base_env = {
            "home": "C:\\hostile\\home",
            "UserProfile": "C:\\hostile\\profile",
            "HOME": "C:\\hostile\\home2",
        }
        env = grok_acp.build_environment(
            base_env, run_dir=self.run_dir, auth_path=self.auth_path
        )
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import pathlib; print(pathlib.Path.home().resolve())",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=creationflags,
        )
        self.assertEqual(completed.returncode, 0)
        output_home = Path(completed.stdout.strip()).resolve()
        self.assertEqual(output_home, self.isolated_home.resolve())

    def test_hostile_home_inputs_do_not_escape_isolation(self) -> None:
        base_env = {
            "HOME": str(self.root),
            "USERPROFILE": str(self.root / "escape"),
        }
        env = grok_acp.build_environment(
            base_env, run_dir=self.run_dir, auth_path=self.auth_path
        )
        self.assertEqual(Path(env["HOME"]), self.isolated_home)
        self.assertEqual(Path(env["USERPROFILE"]), self.isolated_home)
        self.assertNotEqual(Path(env["HOME"]), self.root)

    def test_api_key_stripping_and_controlled_env_preserved(self) -> None:
        base_env = {
            "GROK_API_KEY": "secret1",
            "grok_api_key": "secret2",
            "XAI_API_KEY": "secret3",
            "xai_api_key": "secret4",
            "GROK_AGENT": "agent",
            "GROK_CONFIG": "config",
            "GROK_CONFIG_PATH": "path",
        }
        env = grok_acp.build_environment(
            base_env, run_dir=self.run_dir, auth_path=self.auth_path
        )
        for key in (
            "GROK_API_KEY",
            "grok_api_key",
            "XAI_API_KEY",
            "xai_api_key",
            "GROK_AGENT",
            "GROK_CONFIG",
            "GROK_CONFIG_PATH",
        ):
            self.assertNotIn(key, env)
        for k in grok_acp.COMPATIBILITY_ENV_KEYS:
            self.assertEqual(env[k], "false")
        self.assertEqual(env["NO_COLOR"], "1")

    @unittest.skipUnless(os.name == "nt", "Windows job command assertion")
    def test_windows_job_command_pins_child_home(self) -> None:
        command, child_env, gate, token = grok_acp._windows_job_command(
            ["dummy"],
            home=self.isolated_home,
            env={"home": "C:\\stale", "USERPROFILE": "C:\\stale"},
        )
        self.assertEqual(child_env["HOME"], str(self.isolated_home))
        self.assertEqual(child_env["USERPROFILE"], str(self.isolated_home))
        self.assertNotIn("home", child_env)
        self.assertTrue(str(gate).startswith(str(self.isolated_home)))

    def test_run_session_with_fake_server_pins_home(self) -> None:
        command = grok_acp.build_command(
            sys.executable,
            run_dir=self.run_dir,
            run_id=self.run_id,
            mode="review",
            model="grok-4.7",
            reasoning_effort="medium",
            complexity="medium",
            handoff_schema=SCHEMA,
            max_model_tool_calls=8,
            proxy_base=self.proxy_base,
            executable_args=(str(FAKE_SERVER),),
        )
        base = dict(os.environ)
        base["FAKE_GROK_ACP_SCENARIO"] = "complete"
        base["home"] = "C:\\stale\\home"
        base["UserProfile"] = "C:\\stale\\profile"
        env = grok_acp.build_environment(
            base, run_dir=self.run_dir, auth_path=self.auth_path
        )
        result = grok_acp.run_session(
            command=command,
            run_dir=self.run_dir,
            run_id=self.run_id,
            workspace=self.workspace,
            mode="review",
            prompt="Inspect project/fixture.txt and return the handoff.",
            timeout=30,
            max_model_tool_calls=8,
            max_additional_prompts=0,
            env=env,
            continuation_decider=lambda _v: None,
            proxy_base=self.proxy_base,
        )
        self.assertEqual(result["exit_code"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
