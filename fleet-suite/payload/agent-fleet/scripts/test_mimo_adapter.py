"""Fleet harness regression tests for the native MiMo Code adapter.

These tests prove the seams the parent has to honour for the Xiaomi Token Plan
route: the exact native ``mimo-v2.6-pro`` identity (no ``mimo-auto``, no Flash,
no PAYG, no custom endpoint), the literal and safe argv vector, the hardened
child environment and mode-scoped permission payload, validated (not
presence-only) Token Plan credential binding in an isolated per-run home,
observation-based identity verification and accounting that stays UNKNOWN on
empty, error and timed-out output. No test launches the MiMo CLI, reads a real
credential store, or performs network access.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import uuid


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))


@contextmanager
def retained_test_directory():
    root = Path(os.environ.get("TEAM_TEST_ROOT", str(SCRIPT_DIR / ".test-artifacts")))
    path = root / ("mimo-" + uuid.uuid4().hex)
    path.mkdir(parents=True)
    yield str(path)


tempfile = SimpleNamespace(TemporaryDirectory=retained_test_directory)


def _make_probe_dir(root: Path) -> Path:
    path = root / ("probe-" + uuid.uuid4().hex)
    path.mkdir()
    return path

import adaptive  # noqa: E402
import fleet  # noqa: E402
import mimo_runtime  # noqa: E402


# Synthetic absolute path: argv construction never launches or stats this file.
MIMO_NATIVE_EXECUTABLE = str(SCRIPT_DIR / ".test-artifacts" / "mimo.exe")

# Test-only fixture: it matches the verified Token Plan record shape
# (type api, tp- prefix, AMS base_url) and is never a real credential.
MIMO_TEST_AUTH_KEY = "tp-test-only-not-a-real-key"
MIMO_TEST_FOREIGN_KEY = "foreign-test-key"
MIMO_TEST_AMS_ENDPOINT = "https://token-plan-ams.xiaomimimo.com/v1"


def valid_auth_store_payload() -> dict:
    return {
        "xiaomi": {
            "type": "api",
            "key": MIMO_TEST_AUTH_KEY,
            "metadata": {"uid": "test-account", "base_url": MIMO_TEST_AMS_ENDPOINT},
        }
    }


def profile_with_auth_store(directory: Path, payload) -> tuple[dict, Path]:
    """Point a copy of the shipped MiMo profile at a fixture auth store."""
    profile = json.loads(json.dumps(default_mimo_profile()))
    store = directory / "auth.json"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    store.write_text(text, encoding="utf-8")
    profile["mimo"]["credential_store"]["path"] = str(store)
    return profile, store


def default_mimo_profile() -> dict:
    payload = json.loads(Path(fleet.DEFAULT_PROFILES).read_text(encoding="utf-8"))
    payload["profiles"] = {name: payload["profiles"][name] for name in ("mimo", "qwen-local", "qwen-local-5070ti")}
    with mock.patch.object(fleet, "load_json", return_value=payload):
        return load_mimo_profiles(fleet.DEFAULT_PROFILES)["mimo"]


def load_mimo_profiles(path: Path) -> dict:
    # The mandatory Qwen aliases are unrelated to the MiMo contract and their
    # Ollama executable is deliberately absent from this isolated test profile.
    with mock.patch.object(fleet, "validate_local_runtime", side_effect=lambda raw: dict(raw)), \
            mock.patch.object(fleet.ollama_local_runtime, "validate_pinned_qwen_ollama_contract", side_effect=lambda raw: dict(raw)), \
            mock.patch.object(fleet.ollama_local_runtime, "capacity_lock_identity", return_value="synthetic-qwen"):
        return fleet.load_profiles(path)


def write_profiles(directory: Path, mutate) -> Path:
    """Materialise a mutated copy of the shipped profiles in a temp file."""
    payload = json.loads(Path(fleet.DEFAULT_PROFILES).read_text(encoding="utf-8"))
    payload["profiles"] = {name: payload["profiles"][name] for name in ("mimo", "qwen-local", "qwen-local-5070ti")}
    mutate(payload["profiles"]["mimo"])
    path = directory / "profiles.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class MiMoIdentityPinningTests(unittest.TestCase):
    def test_native_pro_identity_is_accepted(self) -> None:
        self.assertEqual(
            mimo_runtime.require_token_plan_model("mimo-v2.6-pro"),
            mimo_runtime.MIMO_RUNTIME_PROVIDER_MODEL,
        )
        self.assertEqual(
            mimo_runtime.require_token_plan_model("xiaomi/mimo-v2.6-pro"),
            "xiaomi/mimo-v2.6-pro",
        )
        self.assertEqual(
            mimo_runtime.require_token_plan_model(
                "xiaomi-token-plan-ams/mimo-v2.6-pro"
            ),
            "xiaomi-token-plan-ams/mimo-v2.6-pro",
        )

    def test_auto_flash_and_foreign_models_are_refused(self) -> None:
        for refused in (
            "mimo-auto",
            "xiaomi/mimo-auto",
            "mimo-v2-6-flash",
            "xiaomi/mimo-v2.6-flash",
            "xiaomi/mimo-v2.6-mini",
            "openai/gpt-5.6-terra",
            "xiaomi/mimo-v2.6-pro-max",
        ):
            with self.subTest(refused=refused):
                with self.assertRaises(mimo_runtime.MiMoRuntimeError):
                    mimo_runtime.require_token_plan_model(refused)

    def test_payg_and_metered_markers_are_refused(self) -> None:
        for refused in (
            "xiaomi-payg/mimo-v2.6-pro",
            "xiaomi/mimo-v2.6-pro-payg",
            "xiaomi/mimo-v2.6-pro-metered",
            "xiaomi/mimo-v2.6-pro-on-demand",
        ):
            with self.subTest(refused=refused):
                with self.assertRaisesRegex(
                    mimo_runtime.MiMoRuntimeError, "PAYG"
                ):
                    mimo_runtime.require_token_plan_model(refused)

    def test_custom_endpoints_are_refused_and_token_plan_hosts_accepted(self) -> None:
        for host in mimo_runtime.MIMO_TOKEN_PLAN_ENDPOINTS.values():
            with self.subTest(host=host):
                self.assertEqual(
                    mimo_runtime.require_token_plan_endpoint(host), host
                )
        self.assertIsNone(mimo_runtime.require_token_plan_endpoint(None))
        for refused in (
            "https://api.example.com/v1",
            "https://token-plan-ams.xiaomimimo.example.com/v1",
            "https://payg.xiaomimimo.com/v1",
        ):
            with self.subTest(refused=refused):
                with self.assertRaises(mimo_runtime.MiMoRuntimeError):
                    mimo_runtime.require_token_plan_endpoint(refused)

    def test_region_binding_reports_its_strength(self) -> None:
        ams = mimo_runtime.MIMO_TOKEN_PLAN_ENDPOINTS["ams"]
        self.assertEqual(
            mimo_runtime.bind_token_plan_region(
                "xiaomi-token-plan-ams/mimo-v2.6-pro", ams
            ),
            ("ams", "provider-id"),
        )
        # The built-in catalog id carries no region, so the binding is weaker
        # and must be reported as such instead of being claimed as proven.
        self.assertEqual(
            mimo_runtime.bind_token_plan_region("xiaomi/mimo-v2.6-pro", ams),
            ("ams", "declared-endpoint-only"),
        )
        self.assertEqual(
            mimo_runtime.bind_token_plan_region("xiaomi/mimo-v2.6-pro", None),
            (None, "unbound"),
        )
        with self.assertRaisesRegex(mimo_runtime.MiMoRuntimeError, "region"):
            mimo_runtime.bind_token_plan_region(
                "xiaomi-token-plan-cn/mimo-v2.6-pro", ams
            )

    def test_identity_uses_native_observations_only(self) -> None:
        log_line = (
            "INFO  2026-09-22T20:15:00Z service=llm providerID=xiaomi "
            "modelID=mimo-v2.6-pro"
        )
        identity = mimo_runtime.mimo_model_identity(log_line)
        self.assertEqual(identity["status"], "verified")
        self.assertEqual(identity["observed_models"], ["mimo-v2.6-pro"])

        structured = json.dumps(
            {
                "type": "session",
                "providerID": "xiaomi-token-plan-ams",
                "modelID": "mimo-v2.6-pro",
            }
        )
        identity = mimo_runtime.mimo_model_identity(structured)
        self.assertEqual(identity["status"], "mismatch")

        for text in (
            "",
            json.dumps({"type": "assistant", "providerID": "xiaomi",
                        "modelID": "mimo-auto"}),
            json.dumps({"type": "assistant", "providerID": "openai",
                        "modelID": "mimo-v2.6-pro"}),
            # Model self-report prose is never evidence.
            json.dumps({"type": "assistant",
                        "text": "I am mimo-v2.6-pro on the Xiaomi Token Plan"}),
        ):
            with self.subTest(text=text[:48]):
                self.assertNotEqual(
                    mimo_runtime.mimo_model_identity(text)["status"], "verified"
                )


class MiMoProfileContractTests(unittest.TestCase):
    def test_shipped_profile_is_token_plan_only_and_pinned(self) -> None:
        profile = default_mimo_profile()
        self.assertEqual(profile["adapter"], "mimo")
        self.assertEqual(profile["cli_provider_id"], "xiaomi")
        self.assertNotIn("credential", profile)
        self.assertNotIn("local_runtime", profile)
        block = profile["mimo"]
        self.assertTrue(block["token_plan_only"])
        self.assertEqual(block["provider_model"], "xiaomi/mimo-v2.6-pro")
        self.assertEqual(
            block["endpoint"], "https://token-plan-ams.xiaomimimo.com/v1"
        )
        self.assertEqual(block["region"], "ams")
        self.assertEqual(block["endpoint_binding"], "declared-endpoint-only")
        self.assertEqual(set(profile["models"].values()), {"mimo-v2.6-pro"})
        for execution in profile["execution_profiles"].values():
            self.assertEqual(execution["model"], "mimo-v2.6-pro")
            self.assertEqual(execution["runtime_model_id"], "mimo-v2.6-pro")
            self.assertEqual(execution["lifecycle"], "LIMITED")
            self.assertNotIn("shell", set(execution["tools"]))
        # No machine-readable usage source exists, so quota stays UNKNOWN.
        self.assertEqual(
            profile["quota_collection"]["collector_mode"],
            "provider-dashboard-manual",
        )

    def test_loader_refuses_unpinned_model(self) -> None:
        def mutate(profile: dict) -> None:
            profile["models"]["medium"] = "mimo-auto"

        def mutate_execution(profile: dict) -> None:
            profile["execution_profiles"]["mimo-v2-6-pro-read"][
                "runtime_model_id"
            ] = "xiaomi/mimo-v2-6-flash"

        for name, mutation in (
            ("models", mutate),
            ("execution-profile", mutate_execution),
        ):
            with self.subTest(field=name), tempfile.TemporaryDirectory() as temporary:
                path = write_profiles(Path(temporary), mutation)
                with self.assertRaisesRegex(fleet.FleetError, "mimo-v2.6-pro"):
                    load_mimo_profiles(path)

    def test_loader_refuses_payg_fallback_flag(self) -> None:
        def mutate_flag(profile: dict) -> None:
            profile["mimo"]["token_plan_only"] = False

        def mutate_provider(profile: dict) -> None:
            profile["mimo"]["provider_model"] = "xiaomi-payg/mimo-v2.6-pro"

        for name, mutation in (
            ("token_plan_only", mutate_flag),
            ("provider_model", mutate_provider),
        ):
            with self.subTest(field=name), tempfile.TemporaryDirectory() as temporary:
                path = write_profiles(Path(temporary), mutation)
                with self.assertRaisesRegex(fleet.FleetError, "contract invalid"):
                    load_mimo_profiles(path)

    def test_loader_refuses_custom_endpoint(self) -> None:
        def mutate(profile: dict) -> None:
            profile["mimo"]["endpoint"] = "https://api.example.com/v1"

        with tempfile.TemporaryDirectory() as temporary:
            path = write_profiles(Path(temporary), mutate)
            with self.assertRaisesRegex(fleet.FleetError, "custom endpoints"):
                load_mimo_profiles(path)

    def test_loader_refuses_shell_tool(self) -> None:
        def mutate(profile: dict) -> None:
            profile["execution_profiles"]["mimo-v2-6-pro-implement"]["tools"] = [
                "filesystem-read",
                "filesystem-write",
                "shell",
            ]

        with tempfile.TemporaryDirectory() as temporary:
            path = write_profiles(Path(temporary), mutate)
            with self.assertRaisesRegex(fleet.FleetError, "shell"):
                load_mimo_profiles(path)

    def test_loader_refuses_fleet_managed_credential(self) -> None:
        def mutate(profile: dict) -> None:
            profile["credential"] = {"path": "C:/secrets/mimo.json",
                                     "env_var": "MIMO_API_KEY"}

        with tempfile.TemporaryDirectory() as temporary:
            path = write_profiles(Path(temporary), mutate)
            with self.assertRaisesRegex(fleet.FleetError, "designated native store"):
                load_mimo_profiles(path)

    def test_loader_refuses_foreign_profile_identity(self) -> None:
        def mutate(profile: dict) -> None:
            profile["cli_provider_id"] = "xiaomi-payg"

        with tempfile.TemporaryDirectory() as temporary:
            path = write_profiles(Path(temporary), mutate)
            with self.assertRaisesRegex(fleet.FleetError, "pinned mimo/xiaomi"):
                load_mimo_profiles(path)


class MiMoCommandTests(unittest.TestCase):
    def command(self, *, mode: str, model: str, profile, prompt: str, run_dir: Path):
        return fleet.build_command(
            "mimo",
            MIMO_NATIVE_EXECUTABLE,
            prompt=prompt,
            workspace=run_dir / "worktree",
            mode=mode,
            model=model,
            complexity="medium",
            timeout=600,
            run_dir=run_dir,
            native_subagent_limit=0,
            enable_web=False,
            materialize=False,
            profile=profile,
        )

    def test_build_command_emits_safe_literal_argv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            command, stdin_text, _ = self.command(
                mode="review",
                model=mimo_runtime.MIMO_NATIVE_MODEL_ID,
                profile=default_mimo_profile(),
                prompt="Inspect the bounded change.",
                run_dir=run_dir,
            )
            self.assertEqual(
                command[:3], [MIMO_NATIVE_EXECUTABLE, "run", "--print-logs"]
            )
            self.assertEqual(
                command[command.index("--log-level") + 1], "INFO"
            )
            self.assertEqual(
                command[command.index("--model") + 1], "xiaomi/mimo-v2.6-pro"
            )
            self.assertEqual(command[command.index("--agent") + 1], "build")
            self.assertEqual(command[command.index("--format") + 1], "json")
            self.assertTrue(
                command[command.index("--title") + 1].startswith(
                    "Agent Fleet bounded MiMo"
                )
            )
            for forbidden in mimo_runtime.MIMO_UNSAFE_FLAGS:
                self.assertNotIn(forbidden, command)
            self.assertNotIn("--permission", command)
            self.assertNotIn("Inspect the bounded change.", command)
            self.assertEqual(stdin_text, "Inspect the bounded change.\n")

    def test_prompt_travels_over_stdin_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            prompt = "Implement the bounded slice.\n"
            _, stdin_text, _ = self.command(
                mode="implement",
                model=mimo_runtime.MIMO_NATIVE_MODEL_ID,
                profile=default_mimo_profile(),
                prompt=prompt,
                run_dir=run_dir,
            )
            self.assertEqual(stdin_text, prompt)

    def test_wrong_model_is_refused_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            for model in ("mimo-auto", "mimo-v2-6-flash", "xiaomi/mimo-v2.6-mini"):
                with self.subTest(model=model):
                    with self.assertRaisesRegex(
                        fleet.FleetError, "refused model"
                    ):
                        self.command(
                            mode="review",
                            model=model,
                            profile=default_mimo_profile(),
                            prompt="Inspect.",
                            run_dir=run_dir,
                        )

    def test_missing_contract_block_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            with self.assertRaisesRegex(
                fleet.FleetError, "requires the profile contract block"
            ):
                self.command(
                    mode="review",
                    model=mimo_runtime.MIMO_NATIVE_MODEL_ID,
                    profile=None,
                    prompt="Inspect.",
                    run_dir=run_dir,
                )
            broken = json.loads(json.dumps(default_mimo_profile()))
            broken["mimo"]["provider_model"] = "xiaomi/mimo-auto"
            with self.assertRaisesRegex(fleet.FleetError, "provider model"):
                self.command(
                    mode="review",
                    model=mimo_runtime.MIMO_NATIVE_MODEL_ID,
                    profile=broken,
                    prompt="Inspect.",
                    run_dir=run_dir,
                )


class MiMoPermissionAndEnvironmentTests(unittest.TestCase):
    def test_review_permissions_deny_shell_network_and_subagents(self) -> None:
        payload = mimo_runtime.build_permission_payload("review")
        self.assertEqual(payload["*"], "deny")
        for tool in ("bash", "shell", "exec", "task", "agent", "actor",
                     "subagent", "webfetch", "websearch", "mcp"):
            self.assertEqual(payload[tool], "deny", tool)
        self.assertEqual(payload["read"], "allow")
        self.assertNotIn("write", payload)
        self.assertNotIn("edit", payload)

    def test_implement_permissions_add_workspace_writes_only(self) -> None:
        payload = mimo_runtime.build_permission_payload("implement")
        self.assertEqual(payload["*"], "deny")
        self.assertEqual(payload["edit"], "allow")
        self.assertEqual(payload["write"], "allow")
        self.assertEqual(payload["bash"], "deny")
        self.assertEqual(payload["shell"], "deny")
        self.assertEqual(payload["task"], "deny")
        self.assertEqual(payload["actor"], "deny")
        self.assertEqual(payload["webfetch"], "deny")

    def test_child_environment_strips_ambient_overrides(self) -> None:
        ambient = {
            "PATH": "C:/bin",
            "MIMOCODE_AUTH_CONTENT": "inline-auth",
            "MIMOCODE_BASE_URL": "https://evil.example/v1",
            "MIMOCODE_MODEL": "mimo-auto",
            "MIMOCODE_PROVIDER": "openai",
            "MIMOCODE_API_KEY": "sk-ambient",
            "MIMOCODE_HOME": "C:/elsewhere",
            "MIMOCODE_BIN_PATH": "C:/elsewhere/mimo.exe",
            "MIMO_PLATFORM_URL": "https://evil.example/platform",
        }
        hardened = mimo_runtime.harden_mimo_environment(
            dict(ambient), mode="review"
        )
        # Every ambient override is gone; only the keys Fleet pins itself
        # remain, and they carry Fleet-pinned values.
        managed = {item.upper() for item in mimo_runtime.MIMO_MANAGED_ENV}
        for key in mimo_runtime.MIMO_STRIP_ENV:
            if key.upper() in managed:
                continue
            self.assertNotIn(key, hardened)
        self.assertNotIn("MIMO_PLATFORM_URL", hardened)
        # No run home and no auth record were supplied, so neither is set.
        self.assertNotIn("MIMOCODE_HOME", hardened)
        self.assertNotIn("MIMOCODE_AUTH_CONTENT", hardened)
        self.assertEqual(hardened["PATH"], "C:/bin")
        self.assertEqual(hardened["MIMOCODE_PURE"], "true")
        self.assertEqual(hardened["MIMOCODE_ENABLE_ANALYSIS"], "false")
        self.assertEqual(hardened["MIMOCODE_AUTO_SHARE"], "false")
        self.assertEqual(hardened["MIMOCODE_DISABLE_AUTOUPDATE"], "true")
        self.assertEqual(hardened["MIMOCODE_DISABLE_PROJECT_CONFIG"], "true")
        self.assertEqual(
            hardened["MIMOCODE_DANGEROUSLY_SKIP_PERMISSIONS"], "false"
        )
        self.assertEqual(hardened["OTEL_SDK_DISABLED"], "true")
        self.assertEqual(
            hardened["MIMOCODE_PERMISSION"], mimo_runtime.permission_json("review")
        )
        # The pinned native config is always present and carries no endpoint
        # other than the official Token Plan host.
        binding = json.loads(hardened["MIMOCODE_CONFIG_CONTENT"])
        self.assertEqual(binding["provider"]["xiaomi"]["options"]["baseURL"],
                         mimo_runtime.MIMO_TOKEN_PLAN_ENDPOINTS["ams"])

    def test_native_agent_steps_are_bounded_by_mode(self) -> None:
        expected = {"advisory": 8, "review": 16, "implement": 24}
        for mode, steps in expected.items():
            with self.subTest(mode=mode):
                env = mimo_runtime.harden_mimo_environment(
                    {"MIMOCODE_CONFIG_CONTENT": '{"agent":{"build":{"steps":999}}}'},
                    mode=mode,
                )
                config = json.loads(env["MIMOCODE_CONFIG_CONTENT"])
                self.assertEqual(config["agent"]["build"]["steps"], steps)

    def test_environment_attestation_never_carries_a_secret(self) -> None:
        redacted = mimo_runtime.redact_mimo_environment(
            {"MIMOCODE_AUTH_CONTENT": "inline-auth", "MIMOCODE_PURE": "true"}
        )
        self.assertEqual(redacted["MIMOCODE_AUTH_CONTENT"], "<redacted>")
        self.assertEqual(redacted["MIMOCODE_PURE"], "true")

        hardened = mimo_runtime.harden_mimo_environment({}, mode="implement")
        self.assertEqual(
            mimo_runtime.mimo_environment_attestation(
                hardened, mode="implement"
            )["status"],
            "VERIFIED",
        )
        # Simulate an override that survived hardening.
        leaked = dict(hardened)
        leaked["MIMOCODE_TOKEN"] = "leaked-token-value"
        attestation = mimo_runtime.mimo_environment_attestation(
            leaked, mode="implement"
        )
        self.assertEqual(attestation["status"], "UNVERIFIED")
        self.assertEqual(
            attestation["stripped_overrides_present"], ["MIMOCODE_TOKEN"]
        )
        blob = json.dumps(attestation)
        self.assertIn("<redacted>", blob)
        self.assertNotIn("leaked-token-value", blob)
        for key in attestation["environment"]:
            self.assertTrue(
                key.upper().startswith("MIMOCODE_") or key.upper().startswith("OTEL_"),
                key,
            )

    def test_attestation_reports_auth_and_home_binding(self) -> None:
        binding = mimo_runtime.harden_mimo_environment(
            {}, mode="implement", run_home="C:/runs/mimo-home"
        )
        attestation = mimo_runtime.mimo_environment_attestation(
            binding, mode="implement"
        )
        self.assertEqual(attestation["status"], "VERIFIED")
        self.assertEqual(attestation["auth_injection"], "absent")
        self.assertTrue(attestation["run_home_isolated"])

        # An injected credential without an isolated home could land in the
        # shared native home, so the attestation must fail closed.
        leaked_home = dict(binding)
        leaked_home.pop("MIMOCODE_HOME", None)
        leaked_home["MIMOCODE_AUTH_CONTENT"] = json.dumps(
            valid_auth_store_payload()
        )
        unsafe = mimo_runtime.mimo_environment_attestation(
            leaked_home, mode="implement"
        )
        self.assertEqual(unsafe["status"], "UNVERIFIED")
        self.assertEqual(unsafe["auth_injection"], "validated-inline-auth-content")
        self.assertFalse(unsafe["run_home_isolated"])

    def test_probe_environment_is_hardened_and_read_only(self) -> None:
        profile = default_mimo_profile()
        # A cleared ambient environment keeps the probe assertion independent of
        # whatever MiMo variables the operator happens to have exported.
        with retained_test_directory() as directory, \
                mock.patch.object(fleet.tempfile, "mkdtemp", side_effect=lambda **kw: str(_make_probe_dir(Path(directory)))), \
                mock.patch.dict(os.environ, {}, clear=True):
            env = fleet.profile_probe_environment(profile)
            self.assertEqual(
                env["MIMOCODE_PERMISSION"],
                mimo_runtime.permission_json("review"),
            )
            managed = {item.upper() for item in mimo_runtime.MIMO_MANAGED_ENV}
            for key in mimo_runtime.MIMO_STRIP_ENV:
                if key.upper() in managed:
                    continue
                self.assertNotIn(key, env)
            self.assertEqual(env["OTEL_SDK_DISABLED"], "true")
            second_probe = fleet.version_probe_environment(profile)
            self.assertNotEqual(second_probe["MIMOCODE_HOME"], env["MIMOCODE_HOME"])
            self.assertEqual(second_probe["MIMOCODE_PERMISSION"], env["MIMOCODE_PERMISSION"])
            # A --version probe needs no credential and must not inherit one.
            self.assertNotIn("MIMOCODE_AUTH_CONTENT", env)
            self.assertEqual((Path(env["MIMOCODE_HOME"])/"cache/version").read_text(), "21")
            self.assertEqual(
                json.loads(env["MIMOCODE_CONFIG_CONTENT"])["model"],
                "xiaomi/mimo-v2.6-pro",
            )


class MiMoCredentialBindingTests(unittest.TestCase):
    """The designated Token Plan record is validated, bound and never logged."""

    def test_missing_login_store_blocks_before_paid_inference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = json.loads(json.dumps(default_mimo_profile()))
            profile["mimo"]["credential_store"]["path"] = str(
                Path(directory) / "missing-auth.json"
            )
            status = fleet.profile_credential_status(profile)
            self.assertTrue(status["required"])
            self.assertFalse(status["ready"])
            self.assertEqual(
                status["reason"], "mimo-native-login-store-missing"
            )
            with self.assertRaisesRegex(
                mimo_runtime.MiMoRuntimeError, "native Token Plan login"
            ):
                mimo_runtime.require_native_login(profile)

    def test_valid_token_plan_record_is_bound_and_never_logged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile, store = profile_with_auth_store(
                Path(directory), valid_auth_store_payload()
            )
            original = store.read_text(encoding="utf-8")
            status = fleet.profile_credential_status(profile)
            self.assertTrue(status["ready"])
            self.assertEqual(
                status["reason"], "mimo-native-token-plan-auth-valid"
            )
            self.assertTrue(status["contents_inspected"])
            self.assertTrue(status["auth_schema_known"])
            self.assertTrue(status["auth_record_validated"])
            self.assertEqual(
                status["credential_source"], "designated-native-home"
            )
            self.assertEqual(
                status["injection_mode"], "validated-inline-auth-content"
            )
            self.assertEqual(status["native_auth_window_seconds"], 300)
            evidence = status["auth_evidence"]
            self.assertEqual(evidence["provider"], "xiaomi")
            self.assertEqual(evidence["type"], "api")
            self.assertEqual(evidence["key_prefix"], "tp-")
            self.assertTrue(evidence["uid_present"])
            self.assertEqual(evidence["base_url"], MIMO_TEST_AMS_ENDPOINT)
            self.assertNotIn("env_var", status)
            blob = json.dumps(status)
            self.assertNotIn(MIMO_TEST_AUTH_KEY, blob)
            # The account uid is reported as a boolean presence flag only.
            self.assertNotIn("test-account", blob)

            binding = mimo_runtime.require_native_login(profile)
            injected = json.loads(binding["auth_content"])
            self.assertEqual(set(injected), {"xiaomi"})
            record = injected["xiaomi"]
            self.assertEqual(record["type"], "api")
            self.assertEqual(record["key"], MIMO_TEST_AUTH_KEY)
            self.assertEqual(
                record["metadata"]["base_url"], MIMO_TEST_AMS_ENDPOINT
            )
            # The designated store is validated, never rewritten or copied.
            self.assertEqual(store.read_text(encoding="utf-8"), original)

    def test_invalid_auth_records_are_refused(self) -> None:
        def with_record(**overrides):
            record = {
                "type": "api",
                "key": MIMO_TEST_AUTH_KEY,
                "metadata": {"uid": "test-account",
                             "base_url": MIMO_TEST_AMS_ENDPOINT},
            }
            record.update(overrides)
            return {"xiaomi": record}

        cases = {
            "wrong-type": with_record(type="oauth"),
            "foreign-key": with_record(key=MIMO_TEST_FOREIGN_KEY),
            "empty-key": with_record(key="   "),
            "payg-key": with_record(key="tp-payg-test-only"),
            "custom-endpoint": with_record(
                metadata={"uid": "test-account",
                          "base_url": "https://api.example.com/v1"}
            ),
            "other-region": with_record(
                metadata={"uid": "test-account",
                          "base_url": "https://token-plan-cn.xiaomimimo.com/v1"}
            ),
            "missing-metadata": with_record(metadata=None),
            "no-xiaomi-record": {"openai": {"type": "api", "key": "sk-x"}},
            "not-json": "{not json",
            "empty": "",
        }
        for name, payload in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                profile, _ = profile_with_auth_store(Path(temporary), payload)
                status = fleet.profile_credential_status(profile)
                self.assertFalse(status["ready"], name)
                self.assertIn(
                    status["reason"],
                    mimo_runtime.MIMO_AUTH_UNAVAILABLE_REASONS,
                    name,
                )
                blob = json.dumps(status)
                self.assertNotIn(MIMO_TEST_AUTH_KEY, blob)
                with self.assertRaises(mimo_runtime.MiMoRuntimeError):
                    mimo_runtime.require_native_login(profile)

    def test_foreign_store_entries_are_never_injected(self) -> None:
        payload = valid_auth_store_payload()
        payload["openai"] = {"type": "api", "key": MIMO_TEST_FOREIGN_KEY}
        with tempfile.TemporaryDirectory() as directory:
            profile, _ = profile_with_auth_store(Path(directory), payload)
            binding = mimo_runtime.require_native_login(profile)
            injected = json.loads(binding["auth_content"])
            self.assertEqual(set(injected), {"xiaomi"})
            status = binding["status"]
            self.assertEqual(
                status["auth_evidence"]["ignored_store_providers"], ["openai"]
            )
            blob = json.dumps(status) + binding["auth_content"]
            self.assertNotIn(MIMO_TEST_FOREIGN_KEY, blob)


class MiMoRunIsolationTests(unittest.TestCase):
    """Per-run home, pinned config and injected credential for a real run."""

    def bound_environment(self, directory: Path) -> tuple[dict, Path, dict]:
        profile, _store = profile_with_auth_store(
            Path(directory), valid_auth_store_payload()
        )
        run_dir = Path(directory) / "run"
        run_dir.mkdir()
        binding = mimo_runtime.require_native_login(profile)
        home = mimo_runtime.prepare_mimo_run_home(run_dir)
        env = mimo_runtime.harden_mimo_environment(
            {
                "PATH": "C:/bin",
                "MIMO_PLATFORM_URL": "https://evil.example/platform",
                "MIMOCODE_BASE_URL": "https://evil.example/v1",
                "MIMOCODE_MODEL": "mimo-auto",
            },
            mode="review",
            run_home=str(home),
            auth_content=binding["auth_content"],
            provider_model="xiaomi/mimo-v2.6-pro",
            endpoint=MIMO_TEST_AMS_ENDPOINT,
        )
        return env, home, json.loads(binding["auth_content"])

    def test_run_home_is_isolated_and_preseeds_cache_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env, home, injected = self.bound_environment(Path(directory))
            self.assertEqual(home, Path(directory) / "run" / "mimo-home")
            self.assertEqual(
                (home / "cache" / "version").read_text(encoding="utf-8"), "21"
            )
            self.assertEqual(env["MIMOCODE_HOME"], str(home))
            self.assertEqual(
                json.loads(env["MIMOCODE_AUTH_CONTENT"]), injected
            )
            self.assertNotIn("MIMO_PLATFORM_URL", env)
            self.assertNotIn("MIMOCODE_BASE_URL", env)
            self.assertNotIn("MIMOCODE_MODEL", env)
            self.assertEqual(env["PATH"], "C:/bin")

    def test_pinned_config_has_one_provider_one_model_and_token_plan_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env, _home, _injected = self.bound_environment(Path(directory))
            config = json.loads(env["MIMOCODE_CONFIG_CONTENT"])
            self.assertEqual(config["enabled_providers"], ["xiaomi"])
            self.assertEqual(config["model"], "xiaomi/mimo-v2.6-pro")
            self.assertEqual(config["small_model"], "xiaomi/mimo-v2.6-pro")
            self.assertEqual(
                config["provider"]["xiaomi"]["options"]["baseURL"],
                MIMO_TEST_AMS_ENDPOINT,
            )
            self.assertEqual(config["plugin"], [])
            self.assertEqual(config["mcp"], {})
            self.assertFalse(config["snapshot"])
            self.assertEqual(config["share"], "disabled")
            self.assertEqual(config["instructions"], [])
            self.assertEqual(
                config["permission"],
                mimo_runtime.build_permission_payload("review"),
            )
            for marker in mimo_runtime.MIMO_PAYG_MARKERS:
                self.assertNotIn(marker, env["MIMOCODE_CONFIG_CONTENT"].casefold())

    def test_bound_run_attestation_is_verified_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env, _home, _injected = self.bound_environment(Path(directory))
            attestation = mimo_runtime.mimo_environment_attestation(
                env, mode="review"
            )
            self.assertEqual(attestation["status"], "VERIFIED")
            self.assertEqual(
                attestation["auth_injection"], "validated-inline-auth-content"
            )
            self.assertTrue(attestation["run_home_isolated"])
            self.assertEqual(
                attestation["config_binding"]["base_url"], MIMO_TEST_AMS_ENDPOINT
            )
            self.assertEqual(
                attestation["config_binding"]["model"], "xiaomi/mimo-v2.6-pro"
            )
            blob = json.dumps(attestation)
            self.assertIn("<redacted>", blob)
            self.assertNotIn(MIMO_TEST_AUTH_KEY, blob)

    def test_custom_endpoint_cannot_enter_the_run_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(mimo_runtime.MiMoRuntimeError):
                mimo_runtime.harden_mimo_environment(
                    {},
                    mode="review",
                    endpoint="https://api.example.com/v1",
                )
            with self.assertRaises(mimo_runtime.MiMoRuntimeError):
                mimo_runtime.harden_mimo_environment(
                    {}, mode="review", provider_model="xiaomi/mimo-auto"
                )


class MiMoOutputAccountingTests(unittest.TestCase):
    def test_error_output_is_failed_and_never_a_handoff(self) -> None:
        text = json.dumps(
            {
                "type": "error",
                "error": {"name": "ProviderError",
                          "message": "insufficient credits"},
            }
        )
        projected = fleet.project_provider_handoff("mimo", text)
        self.assertIsNone(projected["handoff"])
        self.assertEqual(projected["terminal_reason"], "failed")
        self.assertEqual(projected["error"], "insufficient credits")

    def test_tool_output_is_never_a_handoff(self) -> None:
        text = json.dumps(
            {
                "type": "tool_result",
                "part": {"role": "tool",
                         "output": json.dumps({"status": "COMPLETED"})},
            }
        )
        projected = fleet.project_provider_handoff("mimo", text)
        self.assertIsNone(projected["handoff"])
        self.assertIn("no valid terminal handoff", str(projected["error"]))

    def test_empty_output_has_no_handoff(self) -> None:
        projected = fleet.project_provider_handoff("mimo", "")
        self.assertIsNone(projected["handoff"])
        self.assertIsNone(projected["terminal_reason"])

    def test_empty_error_and_timeout_usage_stays_unknown(self) -> None:
        for text in (
            "",
            "   \n",
            "MiMo run timed out after 600s",
            json.dumps({"type": "error",
                        "error": {"message": "insufficient credits"}}),
        ):
            with self.subTest(text=text[:32]):
                usage = adaptive.extract_token_usage(text, adapter="mimo")
                self.assertEqual(usage["status"], "unknown")
                self.assertIsNone(usage["input_tokens"])
                self.assertIsNone(usage["output_tokens"])
                self.assertIsNone(usage["total_tokens"])
                self.assertIsNone(usage["cost_usd"])
                self.assertIsNone(usage["credits"])
                self.assertEqual(
                    usage["cost_semantics"], mimo_runtime.MIMO_COST_SEMANTICS
                )
        aggregated = fleet.aggregate_token_usage(
            ["", "MiMo run timed out after 600s"], adapter="mimo"
        )
        self.assertEqual(aggregated["status"], "unknown")
        self.assertIsNone(aggregated["cost_usd"])
        self.assertEqual(
            aggregated["cost_semantics"], mimo_runtime.MIMO_COST_SEMANTICS
        )

    def test_credits_are_reported_without_usd_or_tokens(self) -> None:
        text = json.dumps(
            {"type": "step_finish", "usage": {"credits": 12.5}}
        )
        usage = adaptive.extract_token_usage(text, adapter="mimo")
        self.assertEqual(usage["status"], "known")
        self.assertEqual(usage["credits"], 12.5)
        self.assertIsNone(usage["cost_usd"])
        self.assertIsNone(usage["total_tokens"])
        self.assertIsNone(usage["input_tokens"])

    def test_token_counts_are_summed_without_cost(self) -> None:
        text = json.dumps(
            {
                "type": "step_finish",
                "tokens": {"inputTokens": 100, "outputTokens": 50},
            }
        )
        usage = adaptive.extract_token_usage(text, adapter="mimo")
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 50)
        self.assertEqual(usage["total_tokens"], 150)
        self.assertIsNone(usage["cost_usd"])

        aggregated = fleet.aggregate_token_usage(
            [text, text], adapter="mimo"
        )
        self.assertEqual(aggregated["status"], "known")
        self.assertEqual(aggregated["total_tokens"], 300)
        self.assertIsNone(aggregated["cost_usd"])
        self.assertEqual(
            aggregated["cost_semantics"], mimo_runtime.MIMO_COST_SEMANTICS
        )


class MiMoFleetBranchTests(unittest.TestCase):
    def test_reaching_native_step_cap_requires_parent_review(self) -> None:
        def step(identity: str) -> str:
            return json.dumps({
                "type": "step_finish",
                "part": {"type": "step-finish", "id": identity,
                         "tokens": {"total": 100, "input": 90, "output": 10,
                                    "cache": {"read": 0, "write": 0}}},
            })

        at_cap = fleet.native_step_limit_evidence(
            [step("one") + "\n" + step("two")], adapter="mimo", cap=2,
            warn_tokens=150_000,
        )
        self.assertEqual((at_cap["status"], at_cap["observed_max_steps"]),
                         ("reached", 2))
        self.assertTrue(fleet.native_step_cap_requires_review("COMPLETED", at_cap))
        below = fleet.native_step_limit_evidence(
            [step("one")], adapter="mimo", cap=2, warn_tokens=150_000,
        )
        self.assertEqual(below["status"], "below")
        self.assertFalse(fleet.native_step_cap_requires_review("COMPLETED", below))
        unknown = fleet.native_step_limit_evidence(
            [""], adapter="mimo", cap=2, warn_tokens=150_000,
        )
        self.assertEqual(unknown["status"], "unknown")

    def test_adapter_is_registered_and_subagent_denied(self) -> None:
        self.assertIn("mimo", fleet.SUPPORTED_ADAPTERS)
        self.assertIn("mimo", fleet.DEFAULT_DENY_NATIVE_SUBAGENT_AGENTS)
        self.assertEqual(fleet.MIMO_PROVIDER_ID, "xiaomi")
        self.assertEqual(
            fleet.MIMO_NATIVE_MODEL_ID, mimo_runtime.MIMO_NATIVE_MODEL_ID
        )

    def test_mimo_never_inherits_ambient_api_keys(self) -> None:
        self.assertFalse(fleet.adapter_inherits_ambient_api_keys("mimo", True))

    def test_profile_endpoint_pair_is_pinned_to_the_token_plan(self) -> None:
        self.assertEqual(
            fleet.mimo_profile_endpoint_pair(default_mimo_profile()),
            (
                "xiaomi/mimo-v2.6-pro",
                "https://token-plan-ams.xiaomimimo.com/v1",
            ),
        )
        self.assertEqual(
            fleet.mimo_profile_endpoint_pair({}),
            (
                mimo_runtime.MIMO_RUNTIME_PROVIDER_MODEL,
                mimo_runtime.MIMO_DESIGNATED_AUTH_ENDPOINT,
            ),
        )

    def test_worker_prompt_pins_the_token_plan_route(self) -> None:
        schema = fleet.load_json(fleet.HANDOFF_SCHEMA_PATH, "handoff schema")
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            review = fleet.build_worker_prompt(
                "Review the bounded change.",
                agent_id="mimo",
                adapter_id="mimo",
                mode="review",
                workspace=workspace,
                kind="review",
                complexity="medium",
                native_subagent_limit=0,
                enable_web=False,
                knowledge_snapshot=None,
                schema=schema,
            )
            self.assertIn("MiMo Code headless rules", review)
            self.assertIn("mimo-v2.6-pro", review)
            self.assertIn("mimo-auto", review)
            self.assertIn("PAYG", review)
            self.assertIn("hard read-only", review)
            self.assertNotIn("skip-permissions", review.casefold())
            implement = fleet.build_worker_prompt(
                "Implement the bounded change.",
                agent_id="mimo",
                adapter_id="mimo",
                mode="implement",
                workspace=workspace,
                kind="implementation",
                complexity="medium",
                native_subagent_limit=0,
                enable_web=False,
                knowledge_snapshot=None,
                schema=schema,
            )
            self.assertIn("native edit and write tools", implement)


class NativeRegressionTests(unittest.TestCase):
    def test_schema_type_annotation_is_removed_without_relaxing_validation(self):
        handoff={"type":"object","status":"completed","summary":"Read-only check complete.","files_changed":[],"verification":["Read requested file."],"risks":[],"next_steps":[]}
        event={"type":"text","part":{"text":json.dumps(handoff)}}
        projected=fleet.project_provider_handoff("mimo",json.dumps(event))
        self.assertIsNotNone(projected["handoff"])
        self.assertNotIn("type",projected["handoff"])
        handoff["unexpected_authority"]="accept everything"
        event["part"]["text"]=json.dumps(handoff)
        self.assertIsNone(fleet.project_provider_handoff("mimo",json.dumps(event))["handoff"])
        event["type"]="tool_use"
        self.assertEqual(mimo_runtime.mimo_terminal_text(json.dumps(event)),"")

    def test_real_step_finish_shapes_are_summed_and_deduplicated(self):
        def step(identity, tokens):
            return {"type":"step_finish", "part":{"type":"step-finish", "id":identity, "tokens":tokens,"cost":99}}
        first=step("one", {"total":5360,"input":5299,"output":40,"reasoning":21,"cache":{"write":0,"read":0}})
        second=step("two", {"total":16149,"input":10797,"output":64,"reasoning":40,"cache":{"write":0,"read":5248}})
        usage=mimo_runtime.extract_mimo_token_usage(json.dumps([first,second,first]))
        self.assertEqual(usage["total_tokens"],21509)
        self.assertEqual(usage["input_tokens"],16096)
        self.assertEqual(usage["output_tokens"],104)
        self.assertEqual(usage["cache_read_tokens"],5248)
        self.assertEqual(usage["step_count"],2)
        self.assertIsNone(usage["credits"])
        self.assertIsNone(usage["cost_usd"])

    def test_unanchored_model_claim_cannot_fake_identity(self):
        claim="providerID=xiaomi modelID=mimo-v2.6-pro "
        self.assertEqual(mimo_runtime.mimo_model_identity(claim)["status"],"unavailable")
        self.assertEqual(mimo_runtime.mimo_model_identity(json.dumps({"type":"text","part":{"text":claim}}))["status"],"unavailable")

    def test_profile_normalization_round_trip_and_mismatch_refusal(self):
        block=default_mimo_profile()["mimo"]
        self.assertEqual(mimo_runtime.validate_mimo_profile(block),block)
        changed=dict(block,region="cn")
        with self.assertRaises(mimo_runtime.MiMoRuntimeError):mimo_runtime.validate_mimo_profile(changed)

    def test_injected_native_key_is_redacted_without_generic_credential(self):
        key="tp-"+"synthetic"*5
        env={"MIMOCODE_AUTH_CONTENT":json.dumps({"xiaomi":{"type":"api","key":key}})}
        secret=mimo_runtime.injected_output_secret(env)
        for text in ["stdout="+key,"stderr="+key]:
            self.assertNotIn(key,fleet.redact_sensitive_output(text,exact_secret=secret))
        with self.assertRaises(mimo_runtime.MiMoRuntimeError):mimo_runtime.injected_output_secret({})


if __name__ == "__main__":
    unittest.main()
