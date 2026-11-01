"""Fleet harness integration tests for the OpenClaw native Windows adapter.

These tests exercise the seams that the parent has to honour: the literal
argv vector built from the verified source contract, the per-run JSON config
that disables ambient inheritance, the post-run identity check that fails
closed on mismatch, the legacy routing-blocker enforcement, and the
fleet-level profile loader expectations.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402
import openclaw_runtime  # noqa: E402


class OpenClawAdapterBuildCommandTests(unittest.TestCase):
    def command(self, *, executable, workspace, run_dir, mode, model):
        return fleet.build_command(
            "openclaw",
            executable,
            prompt="Inspect the bounded change.",
            workspace=workspace,
            mode=mode,
            model=model,
            complexity="medium",
            timeout=600,
            run_dir=run_dir,
            native_subagent_limit=0,
            enable_web=False,
            materialize=True,
            reasoning_level="medium",
        )

    def _install(self, root: Path) -> Path:
        del root
        # The adapter must use the installed, version-pinned native CLI.  A
        # fake executable under a test temp directory would bypass that guard.
        return openclaw_runtime.PINNED_OPENCLAW_EXECUTABLE

    def test_build_command_emits_literal_argv_and_workspace_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self._install(root)
            workspace = root / "worktree with spaces"
            workspace.mkdir()
            run_dir = root / "run"
            command, stdin_text, _ = self.command(
                executable=executable,
                workspace=workspace,
                run_dir=run_dir,
                mode="review",
                model=openclaw_runtime.OPENCLAW_LOCAL_PROVIDER_MODEL,
            )
            self.assertEqual(command[:3], [str(executable), "agent", "exec"])
            self.assertEqual(command[command.index("--message-file") + 1], "-")
            self.assertEqual(command[command.index("--cwd") + 1], str(workspace.resolve()))
            self.assertEqual(command[command.index("--model") + 1],
                             openclaw_runtime.OPENCLAW_LOCAL_PROVIDER_MODEL)
            self.assertEqual(command[command.index("--timeout") + 1], "600")
            self.assertIn("--json", command)
            for forbidden in ("--isolated", "--auth-env-only", "--deliver"):
                self.assertNotIn(forbidden, command)
            # Prompt travels over stdin to keep the task out of argv.
            self.assertEqual(stdin_text, "Inspect the bounded change.\n")
            # The per-run JSON config and state-dir are run-owned and never deleted.
            config_path = Path(command[command.index("--config") + 1])
            state_dir = Path(command[command.index("--state-dir") + 1])
            self.assertTrue(config_path.exists())
            self.assertTrue((state_dir / ".fleet-owned").is_file())
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["tools"]["fs"]["workspaceOnly"], True)
            self.assertIs(payload["tools"]["swarm"], False)
            self.assertEqual(payload["tools"]["exec"]["mode"], "deny")
            self.assertEqual(payload["plugins"], {"enabled": False})
            self.assertEqual(payload["mcp"], {"servers": {}})
            self.assertEqual(
                payload["models"]["providers"]["vllm"]["models"][0]["id"],
                openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
            )

    def test_review_mode_denies_edit_write_shell_in_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self._install(root)
            workspace = root / "worktree"
            workspace.mkdir()
            run_dir = root / "run"
            command, _, _ = self.command(
                executable=executable,
                workspace=workspace,
                run_dir=run_dir,
                mode="review",
                model=openclaw_runtime.OPENCLAW_LOCAL_PROVIDER_MODEL,
            )
            config_path = Path(command[command.index("--config") + 1])
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        tools = payload["tools"]
        self.assertIn("edit", tools["deny"])
        self.assertIn("write", tools["deny"])
        self.assertIn("exec", tools["deny"])
        self.assertNotIn("edit", tools["allow"])
        self.assertNotIn("write", tools["allow"])
        self.assertNotIn("exec", tools["allow"])
        self.assertNotIn("review_only_blockers", payload)

    def test_implement_mode_allows_edit_and_write_in_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self._install(root)
            workspace = root / "worktree"
            workspace.mkdir()
            run_dir = root / "run"
            command, _, _ = self.command(
                executable=executable,
                workspace=workspace,
                run_dir=run_dir,
                mode="implement",
                model=openclaw_runtime.OPENCLAW_LOCAL_PROVIDER_MODEL,
            )
            config_path = Path(command[command.index("--config") + 1])
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        tools = payload["tools"]
        self.assertIn("edit", tools["allow"])
        self.assertIn("write", tools["allow"])
        self.assertIn("apply_patch", tools["allow"])
        self.assertNotIn("edit", tools["deny"])
        self.assertNotIn("write", tools["deny"])
        self.assertEqual(payload["tools"]["exec"]["mode"], "deny")

    def test_build_command_refuses_unknown_provider_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self._install(root)
            workspace = root / "worktree"
            workspace.mkdir()
            run_dir = root / "run"
            with self.assertRaisesRegex(
                fleet.FleetError, "pinned"
            ):
                self.command(
                    executable=executable,
                    workspace=workspace,
                    run_dir=run_dir,
                    mode="review",
                    model="Qwen3.8-27B-Tampered",
                )

    def test_build_command_refuses_muse_subscription_without_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self._install(root)
            workspace = root / "worktree"
            workspace.mkdir()
            run_dir = root / "run"
            with self.assertRaisesRegex(
                fleet.FleetError,
                "subscription_verified=true",
            ):
                self.command(
                    executable=executable,
                    workspace=workspace,
                    run_dir=run_dir,
                    mode="review",
                    model=openclaw_runtime.OPENCLAW_MUSE_SUBSCRIPTION_PROVIDER_MODEL,
                )

    def test_build_command_propagates_explicit_context_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self._install(root)
            workspace = root / "worktree"
            workspace.mkdir()
            run_dir = root / "run"
            local_runtime = {
                "kind": "openclaw-vllm",
                "state_id": "openclaw-qwen-11440",
                "base_url": openclaw_runtime.OPENCLAW_LOCAL_BASE_URL,
                "served_model_name": openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
                "provider": openclaw_runtime.OPENCLAW_LOCAL_PROVIDER,
                "context_window_tokens": 32768,
                "max_num_seqs": 1,
                "externally_managed": True,
                "provider_cost_pln": 0,
                "provider_cost_display": "0 zł",
                "runtime_not_ready": False,
            }
            command, _, _ = fleet.build_command(
                "openclaw",
                executable,
                prompt="Inspect the bounded change.",
                workspace=workspace,
                mode="review",
                model=openclaw_runtime.OPENCLAW_LOCAL_PROVIDER_MODEL,
                complexity="medium",
                timeout=600,
                run_dir=run_dir,
                native_subagent_limit=0,
                enable_web=False,
                materialize=True,
                reasoning_level="medium",
                local_runtime=local_runtime,
            )
            config_path = Path(command[command.index("--config") + 1])
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["models"]["providers"]["vllm"]["models"][0][
                    "contextWindow"
                ],
                32768,
            )

    def test_build_command_uses_default_16384_when_local_runtime_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = self._install(root)
            workspace = root / "worktree"
            workspace.mkdir()
            run_dir = root / "run"
            command, _, _ = fleet.build_command(
                "openclaw",
                executable,
                prompt="Inspect the bounded change.",
                workspace=workspace,
                mode="review",
                model=openclaw_runtime.OPENCLAW_LOCAL_PROVIDER_MODEL,
                complexity="medium",
                timeout=600,
                run_dir=run_dir,
                native_subagent_limit=0,
                enable_web=False,
                materialize=True,
                reasoning_level="medium",
                local_runtime=None,
            )
            config_path = Path(command[command.index("--config") + 1])
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["models"]["providers"]["vllm"]["models"][0][
                    "contextWindow"
                ],
                16384,
            )


class OpenClawAdapterIdentityTests(unittest.TestCase):
    def test_identity_verified_with_unknown_usage_passes(self) -> None:
        stream = json.dumps(
            {
                "ok": True,
                "status": "ok",
                "final": "ok",
                "payloads": [],
                "model": "Qwen3.8-27B",
                "provider": "vllm",
                "sessionId": "11111111-1111-4111-8111-111111111111",
            }
        )
        result = fleet.openclaw_model_identity(
            stream,
            expected_runtime_model="vllm/Qwen3.8-27B",
        )
        self.assertEqual(result["status"], "verified_unknown_usage")
        self.assertEqual(result["usage_status"], "unknown")
        self.assertEqual(result["observed_models"], ["vllm/Qwen3.8-27B"])
        self.assertEqual(result["observed_providers"], ["vllm"])

    def test_identity_verified_with_usage_reported_passes(self) -> None:
        stream = json.dumps(
            {
                "ok": True,
                "status": "ok",
                "final": "ok",
                "payloads": [],
                "usage": {"input_tokens": 12, "output_tokens": 7},
                "model": "Qwen3.8-27B",
                "provider": "vllm",
                "sessionId": "11111111-1111-4111-8111-111111111111",
            }
        )
        result = fleet.openclaw_model_identity(
            stream,
            expected_runtime_model="vllm/Qwen3.8-27B",
        )
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["usage_status"], "reported")

    def test_identity_mismatch_fails_closed(self) -> None:
        stream = json.dumps(
            {
                "ok": True,
                "status": "ok",
                "final": "ok",
                "payloads": [],
                "model": "Qwen3.8-27B",
                "provider": "openai",
                "sessionId": "11111111-1111-4111-8111-111111111111",
            }
        )
        result = fleet.openclaw_model_identity(
            stream,
            expected_runtime_model="vllm/Qwen3.8-27B",
        )
        self.assertEqual(result["status"], "mismatch")
        self.assertIn("provider", result["error"])

    def test_identity_mismatch_on_model_drift(self) -> None:
        stream = json.dumps(
            {
                "ok": True,
                "status": "ok",
                "final": "ok",
                "payloads": [],
                "model": "Qwen3.8-27B-Tampered",
                "provider": "vllm",
                "sessionId": "11111111-1111-4111-8111-111111111111",
            }
        )
        result = fleet.openclaw_model_identity(
            stream,
            expected_runtime_model="vllm/Qwen3.8-27B",
        )
        self.assertEqual(result["status"], "mismatch")

    def test_identity_unknown_when_envelope_absent(self) -> None:
        stream = json.dumps(
            {
                "ok": True,
                "status": "ok",
                "final": "ok",
                "payloads": [],
                "sessionId": "11111111-1111-4111-8111-111111111111",
            }
        )
        result = fleet.openclaw_model_identity(
            stream,
            expected_runtime_model="vllm/Qwen3.8-27B",
        )
        self.assertEqual(result["status"], "mismatch")
        self.assertTrue(result["usage_unknown"])

    def test_identity_reports_timeout_status(self) -> None:
        stream = json.dumps(
            {
                "ok": False,
                "status": "timeout",
                "final": "",
                "payloads": [],
                "model": "Qwen3.8-27B",
                "provider": "vllm",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "error": {"message": "timeout", "kind": "timeout"},
            }
        )
        result = fleet.openclaw_model_identity(
            stream,
            expected_runtime_model="vllm/Qwen3.8-27B",
        )
        self.assertEqual(result["status"], "mismatch")


class OpenClawProfileLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._billing_patch = mock.patch.dict(
            os.environ,
            {"AGENT_FLEET_BILLING_MODE_PATH": ""},
            clear=False,
        )
        self._billing_patch.start()
        self.addCleanup(self._billing_patch.stop)

    def test_openclaw_profile_loader_rejects_unsafe_provider_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            payload = json.loads(Path(fleet.DEFAULT_PROFILES).read_text(encoding="utf-8"))
            payload["profiles"]["openclaw-local-qwen"]["models"]["medium"] = (
                "vllm/Qwen3.8-Tampered"
            )
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                fleet.FleetError, "must be pinned exactly to"
            ):
                fleet.load_profiles(path)

    def test_openclaw_profile_loader_rejects_enabled_unready_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            payload = json.loads(Path(fleet.DEFAULT_PROFILES).read_text(encoding="utf-8"))
            payload["profiles"]["openclaw-local-qwen"]["routing_enabled"] = True
            payload["profiles"]["openclaw-local-qwen"]["local_runtime"]["runtime_not_ready"] = True
            payload["profiles"]["openclaw-local-qwen"]["routing_blocker"] = (
                "runtime_not_ready"
            )
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                fleet.FleetError, "requires runtime_not_ready=false"
            ):
                fleet.load_profiles(path)

    def test_openclaw_profile_loader_accepts_explicit_local_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            payload = json.loads(Path(fleet.DEFAULT_PROFILES).read_text(encoding="utf-8"))
            profile = payload["profiles"]["openclaw-local-qwen"]
            profile["routing_enabled"] = True
            profile["routing_blocker"] = ""
            profile["local_runtime"]["runtime_not_ready"] = False
            profile["local_runtime"]["context_window_tokens"] = 524288
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertTrue(fleet.load_profiles(path))

    def test_openclaw_muse_profile_cannot_activate_with_only_a_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            payload = json.loads(Path(fleet.DEFAULT_PROFILES).read_text(encoding="utf-8"))
            payload["profiles"]["openclaw-muse-subscription"]["routing_enabled"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(fleet.FleetError, "must declare routing_enabled=false"):
                fleet.load_profiles(path)

    def test_legacy_qwen_local_aliases_cannot_be_re_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            payload = json.loads(Path(fleet.DEFAULT_PROFILES).read_text(encoding="utf-8"))
            payload["profiles"]["qwen-local"]["routing_enabled"] = True
            payload["profiles"]["qwen-local"]["routing_blocker"] = (
                "legacy-local-qwen-alias-disabled-pending-openclaw-11440-canary"
            )
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                fleet.FleetError, "must stay disabled"
            ):
                fleet.load_profiles(path)

    def test_openclaw_profile_loader_rejects_missing_routing_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            payload = json.loads(Path(fleet.DEFAULT_PROFILES).read_text(encoding="utf-8"))
            del payload["profiles"]["openclaw-muse-subscription"]["routing_blocker"]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                fleet.FleetError, "needs a routing_blocker"
            ):
                fleet.load_profiles(path)


class OpenClawEnvironmentHardeningTests(unittest.TestCase):
    def test_default_environment_strips_ambient_openclaw_keys(self) -> None:
        env = fleet.profile_runtime_environment(
            {
                "adapter": "openclaw",
                "cli_provider_id": "openclaw",
                "routing_enabled": False,
                "routing_blocker": "runtime_not_ready",
            },
            inherit_api_keys=False,
            native_subagent_limit=0,
        )
        for key in openclaw_runtime.AMBIENT_OPENCLAW_ENV_KEYS:
            self.assertNotIn(key, env)
        self.assertNotIn("OPENCLAW_CONFIG_PATH", env)
        self.assertFalse(any(key.upper().startswith("OPENCLAW_") for key in env))

    def test_probe_environment_strips_openclaw_keys(self) -> None:
        env = fleet.profile_probe_environment(
            {
                "adapter": "openclaw",
                "cli_provider_id": "openclaw",
                "routing_enabled": False,
                "routing_blocker": "runtime_not_ready",
            }
        )
        for key in openclaw_runtime.AMBIENT_OPENCLAW_ENV_KEYS:
            self.assertNotIn(key, env)
        self.assertNotIn("OPENCLAW_CONFIG_PATH", env)
        self.assertFalse(any(key.upper().startswith("OPENCLAW_") for key in env))


class OpenClawAdapterSupportTests(unittest.TestCase):
    def test_adapter_ids_allow_openclaw(self) -> None:
        self.assertIn("openclaw", fleet.SUPPORTED_ADAPTERS)

    def test_openclaw_never_inherits_ambient_api_keys(self) -> None:
        self.assertFalse(
            fleet.adapter_inherits_ambient_api_keys("openclaw", True)
        )

    def test_openclaw_agents_are_native_subagent_denied(self) -> None:
        for agent_id in (
            "openclaw",
            "openclaw-local-qwen",
            "openclaw-muse-subscription",
        ):
            self.assertIn(agent_id, fleet.DEFAULT_DENY_NATIVE_SUBAGENT_AGENTS)


class OpenClawInvocationModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._billing_patch = mock.patch.dict(
            os.environ,
            {"AGENT_FLEET_BILLING_MODE_PATH": ""},
            clear=False,
        )
        self._billing_patch.start()
        self.addCleanup(self._billing_patch.stop)
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)

    def test_openclaw_local_qwen_invocation_mode(self) -> None:
        profile = self.profiles["openclaw-local-qwen"]
        # The helper only returns a non-None mode after running preflight; here
        # we assert that the adapter ID is registered and the local runtime kind
        # is recognised by the matching branch in provider_invocation_mode logic
        # (Fleet uses raw conditionals, so we verify by inspecting the profile).
        self.assertEqual(profile["adapter"], "openclaw")
        self.assertEqual(profile["local_runtime"]["kind"], "openclaw-vllm")
        self.assertTrue(profile["routing_enabled"])
        self.assertFalse(profile["local_runtime"]["runtime_not_ready"])
        self.assertEqual(profile["local_runtime"]["context_window_tokens"], 524288)
        self.assertNotIn("routing_blocker", profile)
        self.assertTrue(self.profiles["muse"]["routing_enabled"])
        self.assertTrue(self.profiles["opencode"]["routing_enabled"])
        self.assertFalse(self.profiles["qwen-local"]["routing_enabled"])
        self.assertFalse(self.profiles["qwen-local-5070ti"]["routing_enabled"])

    def test_openclaw_muse_subscription_invocation_mode(self) -> None:
        profile = self.profiles["openclaw-muse-subscription"]
        self.assertEqual(profile["adapter"], "openclaw")
        self.assertEqual(
            profile["openclaw_subscription"]["kind"],
            "openclaw-muse-subscription",
        )
        self.assertFalse(profile["routing_enabled"])


class OpenClawLiveInventoryTests(unittest.TestCase):
    def test_inventory_uses_observed_backend_not_cli_version(self):
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        profile = profiles["openclaw-local-qwen"]
        execution = next(iter(profile["execution_profiles"].values()))
        state = {"status": "READY", "routing_ready": True,
                 "probe": {"observed_model_id": "Qwen3.8-27B", "context_bound_satisfied": True}}
        readiness = {"deep_probe": {"output": "OpenClaw 2026.9.4"}, "local_runtime": state}
        self.assertTrue(fleet.routing_v2.runtime_model_visible(execution, profile=profile, readiness=readiness)[0])
        state["probe"]["observed_model_id"] = "other-model"
        self.assertFalse(fleet.routing_v2.runtime_model_visible(execution, profile=profile, readiness=readiness)[0])
        state["probe"]["observed_model_id"] = "Qwen3.8-27B"
        state["status"] = "NOT_READY"
        self.assertFalse(fleet.routing_v2.runtime_model_visible(execution, profile=profile, readiness=readiness)[0])
        self.assertFalse(fleet.routing_v2.runtime_model_visible(execution, profile=profile, readiness={})[0])


class OpenClawWorkerPromptTests(unittest.TestCase):
    def test_openclaw_review_worker_prompt_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "worktree"
            prompt = fleet.build_worker_prompt(
                "Inspect",
                agent_id="openclaw-local-qwen",
                adapter_id="openclaw",
                mode="review",
                workspace=workspace,
                kind="review",
                complexity="medium",
                native_subagent_limit=0,
                enable_web=False,
                knowledge_snapshot=None,
                schema={
                    "type": "object",
                    "required": ["status", "summary", "files_changed",
                                 "verification", "risks", "next_steps"],
                    "additionalProperties": False,
                    "properties": {},
                },
            )
        self.assertIn("OpenClaw native Windows headless rules", prompt)
        self.assertIn("vllm/Qwen3.8-27B", prompt)
        self.assertIn("muse-code/muse-spark-1.3", prompt)
        self.assertIn("hard read-only", prompt.lower())
        self.assertIn("native read and ls tools only", prompt)
        self.assertNotIn("native Read, Grep", prompt)


if __name__ == "__main__":
    unittest.main()
