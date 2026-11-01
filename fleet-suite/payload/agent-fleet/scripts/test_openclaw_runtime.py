"""OpenClaw helper tests; native schema validation runs without a model call."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import openclaw_runtime  # noqa: E402


class OpenClawContractTests(unittest.TestCase):
    def test_local_runtime_rejects_non_vllm_kind(self) -> None:
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "must be openclaw-vllm"
        ):
            openclaw_runtime.validate_local_runtime({"kind": "ollama-windows-external"})

    def test_local_runtime_rejects_remote_base_url(self) -> None:
        raw = {
            "kind": "openclaw-vllm",
            "state_id": "openclaw-qwen-11440",
            "base_url": "http://example.com/v1",
            "served_model_name": openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
            "provider": openclaw_runtime.OPENCLAW_LOCAL_PROVIDER,
            "context_window_tokens": 16384,
            "max_num_seqs": 1,
            "externally_managed": True,
            "provider_cost_pln": 0,
            "provider_cost_display": "0 zł",
            "runtime_not_ready": True,
        }
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "127.0.0.1"
        ):
            openclaw_runtime.validate_local_runtime(raw)

    def test_local_runtime_rejects_advertised_524288_context_when_disabled(self) -> None:
        raw = {
            "kind": "openclaw-vllm",
            "state_id": "openclaw-qwen-11440",
            "base_url": openclaw_runtime.OPENCLAW_LOCAL_BASE_URL,
            "served_model_name": openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
            "provider": openclaw_runtime.OPENCLAW_LOCAL_PROVIDER,
            "context_window_tokens": 524288,
            "max_num_seqs": 1,
            "externally_managed": True,
            "provider_cost_pln": 0,
            "provider_cost_display": "0 zł",
            "runtime_not_ready": True,
        }
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "16384 and 16384"
        ):
            openclaw_runtime.validate_local_runtime(raw)

    def test_local_runtime_rejects_local_provider_cost_nonzero(self) -> None:
        raw = {
            "kind": "openclaw-vllm",
            "state_id": "openclaw-qwen-11440",
            "base_url": openclaw_runtime.OPENCLAW_LOCAL_BASE_URL,
            "served_model_name": openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
            "provider": openclaw_runtime.OPENCLAW_LOCAL_PROVIDER,
            "context_window_tokens": 16384,
            "max_num_seqs": 1,
            "externally_managed": True,
            "provider_cost_pln": 0.01,
            "provider_cost_display": "0.01 zł",
            "runtime_not_ready": True,
        }
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "exactly 0"
        ):
            openclaw_runtime.validate_local_runtime(raw)

    def test_local_runtime_accepts_pinned_contract(self) -> None:
        raw = {
            "kind": "openclaw-vllm",
            "state_id": "openclaw-qwen-11440",
            "base_url": openclaw_runtime.OPENCLAW_LOCAL_BASE_URL,
            "served_model_name": openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
            "provider": openclaw_runtime.OPENCLAW_LOCAL_PROVIDER,
            "context_window_tokens": 16384,
            "max_num_seqs": 1,
            "externally_managed": True,
            "provider_cost_pln": 0,
            "provider_cost_display": "0 zł",
            "runtime_not_ready": True,
        }
        validated = openclaw_runtime.validate_local_runtime(raw)
        self.assertEqual(validated["port"], 11440)
        self.assertEqual(
            validated["runtime_provider_model"],
            openclaw_runtime.OPENCLAW_LOCAL_PROVIDER_MODEL,
        )
        self.assertTrue(validated["runtime_not_ready"])
        self.assertEqual(validated["context_window_tokens"], 16384)

    def test_local_runtime_accepts_explicit_activation_with_16384_context(self) -> None:
        raw = {
            "kind": "openclaw-vllm",
            "state_id": "openclaw-qwen-11440",
            "base_url": openclaw_runtime.OPENCLAW_LOCAL_BASE_URL,
            "served_model_name": openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
            "provider": openclaw_runtime.OPENCLAW_LOCAL_PROVIDER,
            "context_window_tokens": 16384,
            "max_num_seqs": 1,
            "externally_managed": True,
            "provider_cost_pln": 0,
            "provider_cost_display": "0 zł",
            "runtime_not_ready": False,
        }
        validated = openclaw_runtime.validate_local_runtime(raw)
        self.assertFalse(validated["runtime_not_ready"])
        self.assertEqual(validated["context_window_tokens"], 16384)

    def test_local_runtime_accepts_explicit_activation_with_bounded_window(self) -> None:
        raw = {
            "kind": "openclaw-vllm",
            "state_id": "openclaw-qwen-11440",
            "base_url": openclaw_runtime.OPENCLAW_LOCAL_BASE_URL,
            "served_model_name": openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
            "provider": openclaw_runtime.OPENCLAW_LOCAL_PROVIDER,
            "context_window_tokens": 524288,
            "max_num_seqs": 1,
            "externally_managed": True,
            "provider_cost_pln": 0,
            "provider_cost_display": "0 zł",
            "runtime_not_ready": False,
        }
        validated = openclaw_runtime.validate_local_runtime(raw)
        self.assertFalse(validated["runtime_not_ready"])
        self.assertEqual(validated["context_window_tokens"], 524288)

    def test_local_runtime_rejects_explicit_activation_window_below_minimum(self) -> None:
        raw = {
            "kind": "openclaw-vllm",
            "state_id": "openclaw-qwen-11440",
            "base_url": openclaw_runtime.OPENCLAW_LOCAL_BASE_URL,
            "served_model_name": openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
            "provider": openclaw_runtime.OPENCLAW_LOCAL_PROVIDER,
            "context_window_tokens": 8192,
            "max_num_seqs": 1,
            "externally_managed": True,
            "provider_cost_pln": 0,
            "provider_cost_display": "0 zł",
            "runtime_not_ready": False,
        }
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "between 16384 and 524288"
        ):
            openclaw_runtime.validate_local_runtime(raw)

    def test_local_runtime_rejects_explicit_activation_window_above_maximum(self) -> None:
        raw = {
            "kind": "openclaw-vllm",
            "state_id": "openclaw-qwen-11440",
            "base_url": openclaw_runtime.OPENCLAW_LOCAL_BASE_URL,
            "served_model_name": openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
            "provider": openclaw_runtime.OPENCLAW_LOCAL_PROVIDER,
            "context_window_tokens": 524289,
            "max_num_seqs": 1,
            "externally_managed": True,
            "provider_cost_pln": 0,
            "provider_cost_display": "0 zł",
            "runtime_not_ready": False,
        }
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "between 16384 and 524288"
        ):
            openclaw_runtime.validate_local_runtime(raw)

    def test_local_runtime_rejects_non_boolean_runtime_not_ready(self) -> None:
        raw = {
            "kind": "openclaw-vllm",
            "state_id": "openclaw-qwen-11440",
            "base_url": openclaw_runtime.OPENCLAW_LOCAL_BASE_URL,
            "served_model_name": openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
            "provider": openclaw_runtime.OPENCLAW_LOCAL_PROVIDER,
            "context_window_tokens": 16384,
            "max_num_seqs": 1,
            "externally_managed": True,
            "provider_cost_pln": 0,
            "provider_cost_display": "0 zł",
            "runtime_not_ready": "maybe",
        }
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "must be a boolean"
        ):
            openclaw_runtime.validate_local_runtime(raw)

    def test_muse_subscription_rejects_contributor_model(self) -> None:
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "Contributor is categorically"
        ):
            openclaw_runtime.validate_muse_subscription(
                {
                    "kind": "openclaw-muse-subscription",
                    "provider": "muse-code",
                    "served_model_name": "muse-spark-1.3-contributor",
                    "transport": "meta-subscription",
                    "subscription_verified": True,
                }
            )

    def test_muse_subscription_rejects_metered_transport(self) -> None:
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "subscription transport"
        ):
            openclaw_runtime.validate_muse_subscription(
                {
                    "kind": "openclaw-muse-subscription",
                    "provider": "muse-code",
                    "served_model_name": "muse-spark-1.3",
                    "transport": "metered-meta-api",
                    "subscription_verified": True,
                }
            )

    def test_muse_subscription_rejects_legacy_one_two(self) -> None:
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "pinned exactly to"
        ):
            openclaw_runtime.validate_muse_subscription(
                {
                    "kind": "openclaw-muse-subscription",
                    "provider": "muse-code",
                    "served_model_name": "muse-spark-1.2",
                    "transport": "meta-subscription",
                    "subscription_verified": True,
                }
            )

    def test_muse_subscription_requires_subscription_verified(self) -> None:
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "subscription_verified=true"
        ):
            openclaw_runtime.validate_muse_subscription(
                {
                    "kind": "openclaw-muse-subscription",
                    "provider": "muse-code",
                    "served_model_name": "muse-spark-1.3",
                    "transport": "meta-subscription",
                    "subscription_verified": False,
                }
            )

    def test_muse_subscription_accepts_pinned_contract(self) -> None:
        validated = openclaw_runtime.validate_muse_subscription(
            {
                "kind": "openclaw-muse-subscription",
                "provider": "muse-code",
                "served_model_name": "muse-spark-1.3",
                "transport": "meta-subscription",
                "subscription_verified": True,
            }
        )
        self.assertEqual(
            validated["runtime_provider_model"],
            openclaw_runtime.OPENCLAW_MUSE_SUBSCRIPTION_PROVIDER_MODEL,
        )
        self.assertEqual(validated["transport"], "meta-subscription")


class OpenClawRunConfigTests(unittest.TestCase):
    def test_run_config_review_mode_denies_edit_write_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = openclaw_runtime.build_run_config(
                mode="review", workspace=Path(directory)
            )
        tools = config["tools"]
        self.assertEqual(tools["allow"], ["read", "ls"])
        self.assertIn("edit", tools["deny"])
        self.assertIn("write", tools["deny"])
        self.assertIn("exec", tools["deny"])
        self.assertNotIn("edit", tools["allow"])
        self.assertNotIn("write", tools["allow"])
        self.assertNotIn("exec", tools["allow"])
        self.assertEqual(tools["fs"]["workspaceOnly"], True)
        self.assertIs(tools["swarm"], False)
        self.assertEqual(tools["exec"]["mode"], "deny")
        self.assertEqual(config["plugins"], {"enabled": False})
        self.assertEqual(config["mcp"], {"servers": {}})
        self.assertEqual(config["skills"]["allowBundled"], [])
        self.assertEqual(config["models"]["providers"]["vllm"]["baseUrl"],
                         openclaw_runtime.OPENCLAW_LOCAL_BASE_URL)

    def test_run_config_implement_allows_edit_and_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = openclaw_runtime.build_run_config(
                mode="implement", workspace=Path(directory)
            )
        tools = config["tools"]
        self.assertEqual(tools["allow"], ["read", "ls", "write", "edit", "apply_patch"])
        self.assertNotIn("edit", tools["deny"])
        self.assertNotIn("write", tools["deny"])
        self.assertNotIn("apply_patch", tools["deny"])
        self.assertIn("exec", tools["deny"])
        self.assertIs(tools["swarm"], False)
        self.assertEqual(tools["exec"]["mode"], "deny")

    def test_materialized_config_round_trip_validates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "worktree with spaces"
            workspace.mkdir()
            run_dir = Path(directory) / "run"
            config_path = openclaw_runtime.materialize_run_config(
                mode="review", workspace=workspace, run_dir=run_dir
            )
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            state_payload = json.loads(
                (run_dir / "openclaw-state" / "openclaw.json").read_text(
                    encoding="utf-8"
                )
            )
        validated = openclaw_runtime.validate_run_config_payload(payload)
        self.assertEqual(validated["agents"]["defaults"]["workspace"],
                         str(workspace.resolve()))
        self.assertEqual(state_payload, payload)

    def test_validate_rejects_former_invented_root_keys(self) -> None:
        payload = openclaw_runtime.build_run_config(
            mode="review", workspace=Path(tempfile.gettempdir())
        )
        payload["schema_version"] = 1
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "unsupported root keys"
        ):
            openclaw_runtime.validate_run_config_payload(payload)

    def test_validate_rejects_claude_tool_names(self) -> None:
        payload = openclaw_runtime.build_run_config(
            mode="review", workspace=Path(tempfile.gettempdir())
        )
        payload["tools"]["allow"] = ["Read"]
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "non-native tool names"
        ):
            openclaw_runtime.validate_run_config_payload(payload)

    def test_validate_rejects_overlapping_native_allow_and_deny(self) -> None:
        payload = openclaw_runtime.build_run_config(
            mode="review", workspace=Path(tempfile.gettempdir())
        )
        payload["tools"]["deny"].append("read")
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "same native tool"
        ):
            openclaw_runtime.validate_run_config_payload(payload)

    def test_run_config_propagates_context_window_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = openclaw_runtime.build_run_config(
                mode="review",
                workspace=workspace,
                context_window_tokens=32768,
            )
            materialized = openclaw_runtime.materialize_run_config(
                mode="review",
                workspace=workspace,
                run_dir=workspace / "run",
                context_window_tokens=32768,
            )
            payload = json.loads(materialized.read_text(encoding="utf-8"))
        model_def = config["models"]["providers"]["vllm"]["models"][0]
        self.assertEqual(model_def["contextWindow"], 32768)
        self.assertEqual(
            payload["models"]["providers"]["vllm"]["models"][0]["contextWindow"],
            32768,
        )

    def test_run_config_default_context_window_tokens_is_16384(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = openclaw_runtime.build_run_config(
                mode="review", workspace=workspace
            )
            materialized = openclaw_runtime.materialize_run_config(
                mode="review", workspace=workspace, run_dir=workspace / "run"
            )
            payload = json.loads(materialized.read_text(encoding="utf-8"))
        self.assertEqual(
            config["models"]["providers"]["vllm"]["models"][0]["contextWindow"],
            16384,
        )
        self.assertEqual(
            payload["models"]["providers"]["vllm"]["models"][0]["contextWindow"],
            16384,
        )

    def test_run_config_rejects_context_window_tokens_below_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with self.assertRaisesRegex(
                openclaw_runtime.OpenClawRuntimeError,
                "between 16384 and 524288",
            ):
                openclaw_runtime.build_run_config(
                    mode="review",
                    workspace=workspace,
                    context_window_tokens=8192,
                )

    def test_run_config_rejects_context_window_tokens_above_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with self.assertRaisesRegex(
                openclaw_runtime.OpenClawRuntimeError,
                "between 16384 and 524288",
            ):
                openclaw_runtime.build_run_config(
                    mode="review",
                    workspace=workspace,
                    context_window_tokens=524289,
                )

    def test_native_cli_validates_review_and_implement_payloads(self) -> None:
        """Use OpenClaw's real schema validator; never starts a model run."""
        executable = openclaw_runtime.PINNED_OPENCLAW_EXECUTABLE
        self.assertTrue(executable.is_file(), f"missing native CLI: {executable}")
        for mode in ("review", "implement"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / "workspace"
                workspace.mkdir()
                config = openclaw_runtime.build_run_config(
                    mode=mode,
                    workspace=workspace,
                    context_window_tokens=32768,
                )
                config_path = root / f"config-{mode}.json"
                config_path.write_text(
                    json.dumps(config, ensure_ascii=True),
                    encoding="utf-8",
                )
                state_dir = root / "state"
                state_dir.mkdir()
                child_env = openclaw_runtime.harden_openclaw_environment(
                    os.environ.copy()
                )
                child_env["OPENCLAW_CONFIG_PATH"] = str(config_path)
                child_env["OPENCLAW_STATE_DIR"] = str(state_dir)
                completed = subprocess.run(
                    [str(executable), "config", "validate", "--json"],
                    cwd=str(workspace),
                    env=child_env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )
                result = json.loads(completed.stdout)
                self.assertTrue(result.get("valid"), result)


class OpenClawLiveActivationTests(unittest.TestCase):
    """Tests for the read-only /v1/models probe used when the parent flips
    ``runtime_not_ready=false``."""

    def _ready_payload(self, *, max_model_len=16384) -> dict[str, object]:
        return {
            "object": "list",
            "data": [
                {
                    "id": openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
                    "max_model_len": max_model_len,
                }
            ],
        }

    def _disabled_config(self, *, context_window_tokens=16384):
        return {
            "kind": "openclaw-vllm",
            "state_id": "openclaw-qwen-11440",
            "base_url": openclaw_runtime.OPENCLAW_LOCAL_BASE_URL,
            "served_model_name": openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
            "provider": openclaw_runtime.OPENCLAW_LOCAL_PROVIDER,
            "context_window_tokens": context_window_tokens,
            "max_num_seqs": 1,
            "externally_managed": True,
            "provider_cost_pln": 0,
            "provider_cost_display": "0 zł",
            "runtime_not_ready": True,
        }

    def _enabled_config(self, *, context_window_tokens=16384):
        cfg = self._disabled_config(
            context_window_tokens=context_window_tokens
        )
        cfg["runtime_not_ready"] = False
        return cfg

    def test_runtime_status_disabled_default_reports_not_ready(self) -> None:
        with mock.patch.object(
            openclaw_runtime,
            "_probe_models_payload",
        ) as probe:
            status = openclaw_runtime.runtime_status(self._disabled_config())
        probe.assert_not_called()
        self.assertEqual(status["status"], "NOT_READY")
        self.assertFalse(status["routing_ready"])
        self.assertFalse(status["ready"])
        self.assertIn("NOT_READY", status["reason"])
        self.assertIsNone(status["probe"])

    def test_activation_rejects_other_local_port_without_probing(self) -> None:
        cfg = self._enabled_config()
        cfg["base_url"] = "http://127.0.0.1:11441/v1"
        with mock.patch.object(openclaw_runtime, "_probe_models_payload") as probe:
            with self.assertRaisesRegex(openclaw_runtime.OpenClawRuntimeError, "pinned"):
                openclaw_runtime.runtime_status(cfg)
        probe.assert_not_called()

    def test_probe_refuses_oversized_response(self) -> None:
        with mock.patch.object(openclaw_runtime.urllib.request, "build_opener") as factory:
            factory.return_value.open.return_value.__enter__.return_value.read.return_value = b" " * 2_000_001
            with self.assertRaisesRegex(openclaw_runtime.OpenClawRuntimeError, "size limit"):
                openclaw_runtime._probe_models_payload(openclaw_runtime.OPENCLAW_LOCAL_BASE_URL, timeout=1)

    def test_probe_disables_redirect_handler(self) -> None:
        with mock.patch.object(openclaw_runtime.urllib.request, "build_opener") as factory:
            factory.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"data": []}'
            openclaw_runtime._probe_models_payload(openclaw_runtime.OPENCLAW_LOCAL_BASE_URL, timeout=1)
            handlers = factory.call_args.args
        redirect = next(h for h in handlers if isinstance(h, openclaw_runtime.urllib.request.HTTPRedirectHandler))
        request = openclaw_runtime.urllib.request.Request(openclaw_runtime.OPENCLAW_LOCAL_BASE_URL)
        self.assertIsNone(redirect.redirect_request(request, None, 302, 'Found', {}, 'https://example.com/'))

    def test_ensure_runtime_disabled_default_raises(self) -> None:
        with mock.patch.object(
            openclaw_runtime,
            "_probe_models_payload",
        ) as probe:
            with self.assertRaisesRegex(
                openclaw_runtime.OpenClawRuntimeError,
                "fail-closed mode",
            ):
                openclaw_runtime.ensure_runtime(self._disabled_config())
        probe.assert_not_called()

    def test_runtime_status_explicit_ready_with_fake_live_model(self) -> None:
        cfg = self._enabled_config()
        with mock.patch.object(
            openclaw_runtime,
            "_probe_models_payload",
            return_value=self._ready_payload(max_model_len=16384),
        ) as probe:
            status = openclaw_runtime.runtime_status(cfg)
        probe.assert_called_once_with(
            cfg["base_url"],
            timeout=openclaw_runtime.OPENCLAW_LOCAL_PROBE_TIMEOUT_SECONDS,
        )
        self.assertEqual(status["status"], "READY")
        self.assertTrue(status["routing_ready"])
        self.assertTrue(status["ready"])
        self.assertEqual(
            status["probe"]["observed_model_id"],
            openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
        )
        self.assertEqual(status["probe"]["max_model_len"], 16384)
        self.assertTrue(status["probe"]["context_bound_satisfied"])

    def test_runtime_status_explicit_ready_with_bounded_context(self) -> None:
        cfg = self._enabled_config(context_window_tokens=32768)
        with mock.patch.object(
            openclaw_runtime,
            "_probe_models_payload",
            return_value=self._ready_payload(max_model_len=32768),
        ):
            status = openclaw_runtime.runtime_status(cfg)
        self.assertEqual(status["status"], "READY")
        self.assertEqual(status["context_window_tokens"], 32768)
        self.assertEqual(status["probe"]["max_model_len"], 32768)

    def test_runtime_status_explicit_ready_reports_wrong_identity(self) -> None:
        cfg = self._enabled_config()
        wrong_payload = {
            "object": "list",
            "data": [
                {"id": "some-other-model", "max_model_len": 16384},
            ],
        }
        with mock.patch.object(
            openclaw_runtime,
            "_probe_models_payload",
            return_value=wrong_payload,
        ):
            status = openclaw_runtime.runtime_status(cfg)
        self.assertEqual(status["status"], "NOT_READY")
        self.assertFalse(status["routing_ready"])
        self.assertIn("did not return the pinned served model", status["probe_error"])

    def test_runtime_status_explicit_ready_reports_context_shortfall(self) -> None:
        cfg = self._enabled_config(context_window_tokens=32768)
        with mock.patch.object(
            openclaw_runtime,
            "_probe_models_payload",
            return_value=self._ready_payload(max_model_len=16384),
        ):
            status = openclaw_runtime.runtime_status(cfg)
        self.assertEqual(status["status"], "NOT_READY")
        self.assertFalse(status["routing_ready"])
        self.assertIn("max_model_len=16384", status["probe_error"])

    def test_runtime_status_explicit_ready_reports_connection_outage(self) -> None:
        cfg = self._enabled_config()

        def outage(*args, **kwargs):
            raise openclaw_runtime.OpenClawRuntimeError(
                "OpenClaw local probe failed to reach http://127.0.0.1:11440/v1/models: "
                "[Errno 111] Connection refused"
            )

        with mock.patch.object(
            openclaw_runtime,
            "_probe_models_payload",
            side_effect=outage,
        ):
            status = openclaw_runtime.runtime_status(cfg)
        self.assertEqual(status["status"], "NOT_READY")
        self.assertIn("Connection refused", status["probe_error"])

    def test_runtime_status_explicit_ready_reports_malformed_json(self) -> None:
        cfg = self._enabled_config()

        def malformed(*args, **kwargs):
            raise openclaw_runtime.OpenClawRuntimeError(
                "OpenClaw local probe returned malformed JSON from "
                "http://127.0.0.1:11440/v1/models: Expecting value"
            )

        with mock.patch.object(
            openclaw_runtime,
            "_probe_models_payload",
            side_effect=malformed,
        ):
            status = openclaw_runtime.runtime_status(cfg)
        self.assertEqual(status["status"], "NOT_READY")
        self.assertIn("malformed JSON", status["probe_error"])

    def test_runtime_status_explicit_ready_reports_timeout(self) -> None:
        cfg = self._enabled_config()

        def timeout(*args, **kwargs):
            raise openclaw_runtime.OpenClawRuntimeError(
                "OpenClaw local probe failed to reach http://127.0.0.1:11440/v1/models: timed out"
            )

        with mock.patch.object(
            openclaw_runtime,
            "_probe_models_payload",
            side_effect=timeout,
        ):
            status = openclaw_runtime.runtime_status(cfg)
        self.assertEqual(status["status"], "NOT_READY")
        self.assertIn("timed out", status["probe_error"])

    def test_ensure_runtime_explicit_ready_succeeds_with_proof(self) -> None:
        cfg = self._enabled_config(context_window_tokens=32768)
        with mock.patch.object(
            openclaw_runtime,
            "_probe_models_payload",
            return_value=self._ready_payload(max_model_len=32768),
        ):
            ready = openclaw_runtime.ensure_runtime(cfg)
        self.assertFalse(ready["started_by_fleet"])
        self.assertEqual(ready["context_window_tokens"], 32768)
        self.assertEqual(
            ready["probe"]["observed_model_id"],
            openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
        )

    def test_ensure_runtime_explicit_ready_raises_on_outage(self) -> None:
        cfg = self._enabled_config()

        def outage(*args, **kwargs):
            raise openclaw_runtime.OpenClawRuntimeError(
                "OpenClaw local probe failed to reach http://127.0.0.1:11440/v1/models: "
                "connection refused"
            )

        with mock.patch.object(
            openclaw_runtime,
            "_probe_models_payload",
            side_effect=outage,
        ):
            with self.assertRaisesRegex(
                openclaw_runtime.OpenClawRuntimeError, "connection refused"
            ):
                openclaw_runtime.ensure_runtime(cfg)

    def test_ensure_runtime_explicit_ready_raises_on_identity_drift(self) -> None:
        cfg = self._enabled_config()
        wrong_payload = {
            "object": "list",
            "data": [
                {"id": "wrong-model", "max_model_len": 16384},
            ],
        }
        with mock.patch.object(
            openclaw_runtime,
            "_probe_models_payload",
            return_value=wrong_payload,
        ):
            with self.assertRaisesRegex(
                openclaw_runtime.OpenClawRuntimeError, "did not return the pinned"
            ):
                openclaw_runtime.ensure_runtime(cfg)

    def test_ensure_runtime_explicit_ready_raises_on_context_shortfall(self) -> None:
        cfg = self._enabled_config(context_window_tokens=32768)
        with mock.patch.object(
            openclaw_runtime,
            "_probe_models_payload",
            return_value=self._ready_payload(max_model_len=16384),
        ):
            with self.assertRaisesRegex(
                openclaw_runtime.OpenClawRuntimeError, "max_model_len=16384"
            ):
                openclaw_runtime.ensure_runtime(cfg)

    def test_probe_models_payload_bypasses_ambient_proxies(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            self._ready_payload(max_model_len=16384)
        ).encode("utf-8")
        opener = mock.Mock()
        opener.open.return_value = response
        proxy_handler = object()
        with (
            mock.patch.object(
                openclaw_runtime.urllib.request,
                "ProxyHandler",
                return_value=proxy_handler,
            ) as proxy_handler_factory,
            mock.patch.object(
                openclaw_runtime.urllib.request,
                "build_opener",
                return_value=opener,
            ) as build_opener,
        ):
            payload = openclaw_runtime._probe_models_payload(
                openclaw_runtime.OPENCLAW_LOCAL_BASE_URL, timeout=3.0
            )
        proxy_handler_factory.assert_called_once_with({})
        build_opener.assert_called_once_with(proxy_handler, mock.ANY)
        self.assertIsInstance(build_opener.call_args.args[1], openclaw_runtime._NoProbeRedirect)
        opener.open.assert_called_once_with(
            openclaw_runtime.OPENCLAW_LOCAL_BASE_URL.rstrip("/") + "/models",
            timeout=3.0,
        )
        self.assertEqual(
            payload["data"][0]["id"],
            openclaw_runtime.OPENCLAW_LOCAL_SERVED_MODEL,
        )


class OpenClawEnvelopeTests(unittest.TestCase):
    def test_envelope_to_result_parses_ok_with_usage(self) -> None:
        envelope = json.dumps(
            {
                "ok": True,
                "status": "ok",
                "final": "Hello world.",
                "payloads": [{"text": "Hello world."}],
                "usage": {"input_tokens": 12, "output_tokens": 4},
                "model": "Qwen3.8-27B",
                "provider": "vllm",
                "sessionId": "11111111-1111-4111-8111-111111111111",
            }
        )
        result = openclaw_runtime.envelope_to_result(envelope)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["final"], "Hello world.")
        self.assertEqual(result["provider"], "vllm")
        self.assertEqual(result["model"], "Qwen3.8-27B")
        self.assertEqual(result["sessionId"], "11111111-1111-4111-8111-111111111111")
        self.assertFalse(result["usage_unknown"])
        self.assertEqual(result["usage"], {"input_tokens": 12, "output_tokens": 4})

    def test_envelope_to_result_marks_usage_unknown_when_absent(self) -> None:
        envelope = json.dumps(
            {
                "ok": True,
                "status": "ok",
                "final": "Hello.",
                "payloads": [],
                "model": "Qwen3.8-27B",
                "provider": "vllm",
                "sessionId": "11111111-1111-4111-8111-111111111111",
            }
        )
        result = openclaw_runtime.envelope_to_result(envelope)
        self.assertTrue(result["ok"])
        self.assertTrue(result["usage_unknown"])
        self.assertIsNone(result["usage"])

    def test_envelope_to_result_parses_timeout(self) -> None:
        envelope = json.dumps(
            {
                "ok": False,
                "status": "timeout",
                "final": "",
                "payloads": [],
                "model": "Qwen3.8-27B",
                "provider": "vllm",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "error": {"message": "Agent run timed out", "kind": "timeout"},
            }
        )
        result = openclaw_runtime.envelope_to_result(envelope)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["error"]["kind"], "timeout")

    def test_envelope_to_result_parses_error(self) -> None:
        envelope = json.dumps(
            {
                "ok": False,
                "status": "error",
                "final": "",
                "payloads": [],
                "model": "Qwen3.8-27B",
                "provider": "vllm",
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "error": {
                    "message": "All model fallback candidates failed",
                    "kind": "fallback_exhausted",
                },
            }
        )
        result = openclaw_runtime.envelope_to_result(envelope)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["kind"], "fallback_exhausted")

    def test_envelope_to_result_handles_empty_stdout(self) -> None:
        result = openclaw_runtime.envelope_to_result("")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["kind"], "empty_envelope")
        self.assertTrue(result["usage_unknown"])

    def test_envelope_to_result_handles_invalid_json(self) -> None:
        result = openclaw_runtime.envelope_to_result("not json")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["kind"], "invalid_envelope")

    def test_identity_check_verified_with_usage_reported(self) -> None:
        envelope = openclaw_runtime.envelope_to_result(
            json.dumps(
                {
                    "ok": True,
                    "status": "ok",
                    "final": "ok",
                    "payloads": [],
                    "usage": {"input_tokens": 1},
                    "model": "Qwen3.8-27B",
                    "provider": "vllm",
                    "sessionId": "11111111-1111-4111-8111-111111111111",
                }
            )
        )
        result = openclaw_runtime.identity_check(
            expected_provider="vllm",
            expected_model="Qwen3.8-27B",
            envelope_result=envelope,
        )
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["usage_status"], "reported")

    def test_identity_check_verified_with_unknown_usage(self) -> None:
        envelope = openclaw_runtime.envelope_to_result(
            json.dumps(
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
        )
        result = openclaw_runtime.identity_check(
            expected_provider="vllm",
            expected_model="Qwen3.8-27B",
            envelope_result=envelope,
        )
        self.assertEqual(result["status"], "verified_unknown_usage")
        self.assertEqual(result["usage_status"], "unknown")

    def test_identity_check_detects_provider_mismatch(self) -> None:
        envelope = openclaw_runtime.envelope_to_result(
            json.dumps(
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
        )
        result = openclaw_runtime.identity_check(
            expected_provider="vllm",
            expected_model="Qwen3.8-27B",
            envelope_result=envelope,
        )
        self.assertEqual(result["status"], "mismatch")

    def test_identity_check_detects_model_mismatch(self) -> None:
        envelope = openclaw_runtime.envelope_to_result(
            json.dumps(
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
        )
        result = openclaw_runtime.identity_check(
            expected_provider="vllm",
            expected_model="Qwen3.8-27B",
            envelope_result=envelope,
        )
        self.assertEqual(result["status"], "mismatch")

    def test_identity_check_rejects_missing_provider_or_model(self) -> None:
        envelope = openclaw_runtime.envelope_to_result(
            json.dumps(
                {
                    "ok": True,
                    "status": "ok",
                    "final": "ok",
                    "payloads": [],
                    "sessionId": "11111111-1111-4111-8111-111111111111",
                }
            )
        )
        result = openclaw_runtime.identity_check(
            expected_provider="vllm",
            expected_model="Qwen3.8-27B",
            envelope_result=envelope,
        )
        self.assertEqual(result["status"], "mismatch")


class OpenClawCommandTests(unittest.TestCase):
    def test_build_command_emits_literal_argv_vector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = openclaw_runtime.PINNED_OPENCLAW_EXECUTABLE
            self.assertTrue(executable.is_file())
            state_dir = Path(directory) / "state"
            config_path = state_dir / "openclaw.runtime-config.review.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text("{}", encoding="utf-8")
            workspace = Path(directory) / "worktree with spaces"
            workspace.mkdir()
            command, _ = openclaw_runtime.build_command(
                executable=str(executable),
                workspace=workspace,
                state_dir=state_dir,
                run_config_path=config_path,
                timeout_seconds=600,
                provider_model=openclaw_runtime.OPENCLAW_LOCAL_PROVIDER_MODEL,
            )
        self.assertEqual(command[:2], [str(executable), "agent"])
        self.assertEqual(command[2], "exec")
        self.assertEqual(
            command[command.index("--message-file") + 1], "-"
        )
        self.assertEqual(command[command.index("--cwd") + 1], str(workspace.resolve()))
        self.assertEqual(
            command[command.index("--config") + 1], str(config_path.resolve())
        )
        self.assertEqual(
            command[command.index("--state-dir") + 1], str(state_dir.resolve())
        )
        self.assertEqual(
            command[command.index("--model") + 1],
            openclaw_runtime.OPENCLAW_LOCAL_PROVIDER_MODEL,
        )
        self.assertEqual(command[command.index("--timeout") + 1], "600")
        self.assertIn("--json", command)
        # These contract-banned flags must never appear.
        for forbidden in ("--isolated", "--auth-env-only", "--deliver"):
            self.assertNotIn(forbidden, command)

    def test_build_command_refuses_shell_bridge(self) -> None:
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError,
            "installed native Windows versioned executable",
        ):
            openclaw_runtime.build_command(
                executable="openclaw.cmd",
                workspace=Path("."),
                state_dir=Path("./state"),
                run_config_path=Path("./openclaw.runtime-config.review.json"),
                timeout_seconds=600,
                provider_model=openclaw_runtime.OPENCLAW_LOCAL_PROVIDER_MODEL,
            )

    def test_build_command_refuses_unsafe_provider_model_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = openclaw_runtime.PINNED_OPENCLAW_EXECUTABLE
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            config_path = state_dir / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(
                openclaw_runtime.OpenClawRuntimeError,
                "must be <provider>/<served-model>",
            ):
                openclaw_runtime.build_command(
                    executable=str(executable),
                    workspace=Path(directory),
                    state_dir=state_dir,
                    run_config_path=config_path,
                    timeout_seconds=600,
                    provider_model="Qwen3.8-27B",
                )

    def test_refuse_unsafe_argv_combination_blocks_config_and_isolated(self) -> None:
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError,
            "--config cannot be combined with --isolated",
        ):
            openclaw_runtime.refuse_unsafe_argv_combination(
                ["openclaw.cmd", "agent", "exec", "--config", "x.json", "--isolated"]
            )

    def test_refuse_unsafe_argv_combination_blocks_deliver(self) -> None:
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "forbidden; Fleet owns the receipt"
        ):
            openclaw_runtime.refuse_unsafe_argv_combination(
                ["openclaw.cmd", "agent", "exec", "--deliver", "out.bin"]
            )


class OpenClawEnvironmentTests(unittest.TestCase):
    def test_harden_environment_strips_ambient_overrides(self) -> None:
        environment = {
            "PATH": "C:/Windows",
            "OPENCLAW_CONFIG_PATH": "C:/Users/example/.openclaw/openclaw.json",
            "OPENCLAW_HOME": "C:/Users/example/.openclaw",
            "OPENCLAW_AGENT_DIR": "C:/Users/example/.openclaw/agents",
            "OPENCLAW_OAUTH_DIR": "C:/Users/example/.openclaw/oauth",
            "OPENCLAW_BUNDLED_PLUGINS_DIR": "C:/Users/example/plugins",
            "OPENCLAW_GATEWAY_URL": "ws://example.invalid:18789",
            "OPENCLAW_GATEWAY_TOKEN": "ambient-secret",
            "OPENCLAW_LOAD_SHELL_ENV": "1",
        }
        hardened = openclaw_runtime.harden_openclaw_environment(environment)
        self.assertEqual(hardened, {"PATH": "C:/Windows"})
        self.assertEqual(
            openclaw_runtime.assert_no_ambient_openclaw_environment(hardened),
            hardened,
        )

    def test_assert_no_ambient_openclaw_environment_rejects_blockers(self) -> None:
        with self.assertRaisesRegex(
            openclaw_runtime.OpenClawRuntimeError, "ambient override present"
        ):
            openclaw_runtime.assert_no_ambient_openclaw_environment(
                {
                    "OPENCLAW_GATEWAY_URL": "ws://127.0.0.1:18789",
                    "PATH": "C:/Windows",
                }
            )


class OpenClawStateDirTests(unittest.TestCase):
    def test_state_dir_is_run_owned_and_not_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            state_dir = openclaw_runtime.acquire_state_dir(run_dir, label="probe")
            openclaw_runtime.assert_run_owned_state_dir(state_dir)
            sentinel = state_dir / ".fleet-owned"
            self.assertTrue(sentinel.is_file())
            contents = sentinel.read_text(encoding="utf-8")
            self.assertIn("owner=fleet", contents)
            self.assertIn("label=probe", contents)

    def test_transcript_probe_counts_only_assistant_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = openclaw_runtime.acquire_state_dir(Path(directory), label="tool-budget")
            self.assertIsNone(openclaw_runtime.transcript_tool_call_count(state))
            db = state / "agents" / "main" / "agent" / "openclaw-agent.sqlite"
            db.parent.mkdir(parents=True)
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("CREATE TABLE schema_meta(role TEXT, schema_version INTEGER, app_version TEXT)")
                conn.execute("INSERT INTO schema_meta VALUES('agent',19,'2026.9.4')")
                conn.execute("CREATE TABLE transcript_events(event_json TEXT)")
                conn.execute("INSERT INTO transcript_events VALUES(?)", (json.dumps({
                    "type": "message", "message": {"role": "assistant", "content": [
                        {"type": "toolCall", "name": "read"},
                        {"type": "toolCall", "name": "read"},
                        {"type": "text", "text": "done"},
                    ]},
                }),))
                conn.execute("INSERT INTO transcript_events VALUES(?)", (json.dumps({
                    "type": "message", "message": {"role": "toolResult", "content": []},
                }),))
            self.assertEqual(openclaw_runtime.transcript_tool_call_count(state), 2)
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("UPDATE schema_meta SET app_version='future'")
            with self.assertRaisesRegex(openclaw_runtime.OpenClawRuntimeError, "schema/version changed"):
                openclaw_runtime.transcript_tool_call_count(state)


class OpenClawLegacyAliasTests(unittest.TestCase):
    def test_legacy_qwen_aliases_are_disabled_by_default(self) -> None:
        profiles = {
            "qwen-local": {
                "adapter": "qwen",
                "cli_provider_id": "local",
                "routing_enabled": False,
                "routing_blocker": "legacy-local-qwen-alias-disabled-pending-openclaw-11440-canary",
            },
            "qwen-local-5070ti": {
                "adapter": "qwen",
                "cli_provider_id": "local",
                "routing_enabled": False,
                "routing_blocker": "legacy-local-qwen-alias-disabled-pending-openclaw-11440-canary",
            },
            "openclaw-local-qwen": {
                "adapter": "openclaw",
                "cli_provider_id": "openclaw",
                "routing_enabled": False,
                "routing_blocker": "runtime_not_ready",
                "local_runtime": {
                    "kind": "openclaw-vllm",
                    "runtime_not_ready": True,
                },
            },
        }
        state = openclaw_runtime.legacy_qwen_alias_routing_status(profiles)
        self.assertIn("qwen-local", state["legacy_aliases_present"])
        self.assertIn("qwen-local-5070ti", state["legacy_aliases_present"])
        self.assertFalse(state["legacy_aliases_routing_enabled"])
        self.assertTrue(state["openclaw_local_qwen_present"])
        self.assertFalse(state["openclaw_local_qwen_routing_enabled"])
        self.assertTrue(state["routing_disabled_until_parent_canary"])

    def test_legacy_aliases_blocker_default_is_a_sentinel(self) -> None:
        profiles = {
            "qwen-local": {
                "adapter": "qwen",
                "cli_provider_id": "local",
                "routing_enabled": False,
            },
        }
        # The helper defaults a missing ``routing_blocker`` to a sentinel
        # string so audits always see *something*. The Fleet profile loader
        # is the layer that rejects the missing ``routing_blocker`` upstream.
        state = openclaw_runtime.legacy_qwen_alias_routing_status(profiles)
        self.assertIn(
            "qwen-local",
            state["legacy_aliases_blockers"],
        )
        self.assertTrue(
            state["legacy_aliases_blockers"]["qwen-local"].startswith(
                "legacy-local-qwen-alias-disabled-pending-openclaw-11440-canary"
            )
        )


class OpenClawProbeSkipTests(unittest.TestCase):
    def test_write_executable_probe_skip_marks_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            probe_path = openclaw_runtime.write_executable_probe_skip(
                run_dir,
                label="version",
                reason="parent-must-prove-11440-first",
            )
            contents = probe_path.read_text(encoding="utf-8")
        self.assertIn("status=SKIPPED", contents)
        self.assertIn("reason=parent-must-prove-11440-first", contents)
        self.assertIn(
            "pinned_version=" + openclaw_runtime.PINNED_OPENCLAW_VERSION,
            contents,
        )


if __name__ == "__main__":
    unittest.main()
