"""Synthetic-only Reflection Beam profile contracts; no credentials or network."""

from __future__ import annotations

import importlib.util
import json
import os
import contextlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid
import unittest
from datetime import timedelta
from unittest import mock

import fleet


class OpenCodeBeamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        cls.zai = cls.profiles["opencode"]
        cls.beam = cls.profiles["opencode-beam"]

    @contextlib.contextmanager
    def temporary_root(self):
        base = Path(os.environ.get("TEAM_TEST_ROOT", tempfile.gettempdir()))
        root = base / f"fleet-opencode-beam-{uuid.uuid4().hex}"
        root.mkdir(parents=True)
        try:
            yield str(root)
        finally:
            shutil.rmtree(root)

    def beam_profile_for_root(self, root: Path) -> dict:
        profile = json.loads(json.dumps(self.beam))
        profile["data_boundary"]["workspace_root"] = str(root)
        return profile

    def test_zai_runtime_config_matches_0985_byte_for_byte(self) -> None:
        baseline = Path(__file__).resolve().parents[3].parent / "0.9.85" / "payload" / "agent-fleet" / "scripts" / "fleet.py"
        self.assertTrue(baseline.is_file(), "0.9.85 source fixture is required for release parity")
        spec = importlib.util.spec_from_file_location("fleet_0985_baseline", baseline)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        baseline_fleet = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = baseline_fleet
        baseline_scripts = str(baseline.parent)
        sys.path.insert(0, baseline_scripts)
        try:
            spec.loader.exec_module(baseline_fleet)
            self.assertEqual(
                fleet.opencode_runtime_config(profile=self.zai),
                baseline_fleet.opencode_runtime_config(),
            )
        finally:
            sys.path.remove(baseline_scripts)
            sys.modules.pop(spec.name, None)

    def test_beam_runtime_config_has_only_reflection_and_no_office_tools(self) -> None:
        for mode in ("advisory", "review"):
            config = json.loads(fleet.opencode_runtime_config(mode=mode, profile=self.beam))
            self.assertEqual(config["permission"]["edit"], "deny")
        config = json.loads(
            fleet.opencode_runtime_config(mode="implement", profile=self.beam)
        )
        self.assertEqual(config["enabled_providers"], ["reflection"])
        self.assertEqual(set(config["provider"]), {"reflection"})
        self.assertEqual(config["provider"], self.beam["provider"])
        self.assertEqual(config["mcp"], {})
        self.assertFalse(
            any(name.startswith(f"{fleet.OFFICE_MCP_SERVER_NAME}_") for name in config["permission"])
        )
        self.assertEqual(config["permission"]["edit"], fleet.beam_edit_permission())
        self.assertEqual(config["permission"]["read"]["**/.git"], "deny")
        for name in ("bash", "task", "skill", "webfetch", "websearch", "external_directory"):
            self.assertEqual(config["permission"][name], "deny")
        for path in fleet.BEAM_EDIT_DENY_PATTERNS:
            self.assertEqual(config["permission"]["edit"][path], "deny")
        with self.assertRaisesRegex(fleet.FleetError, "cannot mount Office"):
            fleet.opencode_runtime_config(
                office_mcp={"command": "office", "args": []},
                mode="implement",
                profile=self.beam,
            )

    def test_opencode_command_uses_declared_execution_profile_variant(self) -> None:
        beam_execution = fleet.resolve_execution_profile(
            "opencode-beam",
            self.beam,
            complexity="high",
            mode="review",
            required_tools=[],
        )
        beam_command, _, _ = fleet.build_command(
            "opencode",
            "opencode.exe",
            prompt="Review a synthetic fixture.",
            workspace=Path.cwd(),
            mode="review",
            model=beam_execution["runtime_model_id"],
            complexity="high",
            timeout=60,
            run_dir=Path.cwd(),
            native_subagent_limit=0,
            enable_web=False,
            materialize=False,
            reasoning_level=beam_execution["reasoning_level"],
            profile=self.beam,
        )
        self.assertEqual(beam_command[beam_command.index("--model") + 1], "reflection/Beam-1.0-501B-A23B")
        self.assertEqual(beam_command[beam_command.index("--variant") + 1], "high")
        self.assertIn("Reflection Beam", beam_command[beam_command.index("--title") + 1])

        zai_execution = fleet.resolve_execution_profile(
            "opencode",
            self.zai,
            complexity="high",
            mode="review",
            required_tools=[],
        )
        zai_command, _, _ = fleet.build_command(
            "opencode",
            "opencode.exe",
            prompt="Review a fixture.",
            workspace=Path.cwd(),
            mode="review",
            model=zai_execution["runtime_model_id"],
            complexity="high",
            timeout=60,
            run_dir=Path.cwd(),
            native_subagent_limit=0,
            enable_web=False,
            materialize=False,
            reasoning_level=zai_execution["reasoning_level"],
            profile=self.zai,
        )
        self.assertEqual(zai_command[zai_command.index("--variant") + 1], "max")
        self.assertEqual(
            zai_command[zai_command.index("--title") + 1],
            "Agent Fleet bounded GLM-5.3-Flash review",
        )

        with self.assertRaisesRegex(fleet.FleetError, "reasoning variant"):
            fleet.build_command(
                "opencode",
                "opencode.exe",
                prompt="Review a synthetic fixture.",
                workspace=Path.cwd(),
                mode="review",
                model=beam_execution["runtime_model_id"],
                complexity="high",
                timeout=60,
                run_dir=Path.cwd(),
                native_subagent_limit=0,
                enable_web=False,
                materialize=False,
                reasoning_level="max",
                profile=self.beam,
            )

    def test_workspace_boundary_accepts_inside_and_refuses_outside_and_sibling(self) -> None:
        with self.temporary_root() as temp:
            base = Path(temp)
            allowed = base / "beam-synthetic"
            inside = allowed / "project"
            outside = base / "other-workspace"
            sibling = base / "beam-synthetic-evil"
            for directory in (allowed, inside, outside, sibling):
                directory.mkdir(parents=True, exist_ok=True)
            profile = {
                "data_boundary": {
                    "synthetic_only": True,
                    "workspace_root": str(allowed),
                }
            }
            self.assertEqual(fleet.validate_profile_workspace_boundary(profile, inside), inside.resolve())
            for rejected in (outside, sibling):
                with self.subTest(workspace=rejected), self.assertRaises(fleet.FleetError):
                    fleet.validate_profile_workspace_boundary(profile, rejected)

    @unittest.skipUnless(os.name == "nt", "Windows junctions are only available on Windows")
    def test_workspace_boundary_refuses_junction_escape(self) -> None:
        with self.temporary_root() as temp:
            base = Path(temp)
            allowed = base / "beam-synthetic"
            outside = base / "outside"
            junction = allowed / "junction-escape"
            allowed.mkdir()
            outside.mkdir()
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"mklink /J failed: {completed.stdout}\n{completed.stderr}",
            )
            self.assertTrue(
                junction.exists(),
                f"mklink /J reported success but created no junction: {completed.stdout}\n{completed.stderr}",
            )
            profile = {
                "data_boundary": {
                    "synthetic_only": True,
                    "workspace_root": str(allowed),
                }
            }
            try:
                with self.assertRaises(fleet.FleetError):
                    fleet.validate_profile_workspace_boundary(profile, junction)
            finally:
                os.rmdir(junction)

    def test_trial_qualification_is_pinned_and_expiry_fails_closed(self) -> None:
        expiry = fleet.dt.datetime.fromisoformat(
            fleet.SYNTHETIC_ONLY_TRIAL_EXPIRES_AT
        )
        fleet.validate_beam_trial(
            self.beam, now=expiry - timedelta(microseconds=1)
        )
        fleet.validate_beam_trial(self.beam, now=expiry)
        with self.assertRaisesRegex(fleet.FleetError, "expired"):
            fleet.validate_beam_trial(self.beam, now=expiry + timedelta(microseconds=1))
        malformed = json.loads(json.dumps(self.beam))
        malformed["trial_expires_at"] = "not-an-expiry"
        with self.assertRaisesRegex(fleet.FleetError, "expiry"):
            fleet.validate_beam_trial(malformed)

    def test_qualifier_accepts_only_in_root_and_rejects_linked_workspace_escape(self) -> None:
        with self.temporary_root() as temp:
            base = Path(temp)
            allowed = base / "beam-synthetic"
            workspace = allowed / "project"
            outside = base / "outside"
            sibling = base / "beam-synthetic-evil"
            for directory in (workspace, outside, sibling):
                directory.mkdir(parents=True)
            profile = self.beam_profile_for_root(allowed)
            with mock.patch.object(fleet, "SYNTHETIC_ONLY_WORKSPACE_ROOT", str(allowed)):
                self.assertEqual(
                    fleet.qualify_beam_workspace("opencode-beam", profile, workspace),
                    workspace.resolve(),
                )
                for rejected in (outside, sibling):
                    with self.subTest(workspace=rejected), self.assertRaises(
                        fleet.FleetError
                    ):
                        fleet.qualify_beam_workspace(
                            "opencode-beam", profile, rejected
                        )

    @unittest.skipUnless(os.name == "nt", "Windows junctions are only available on Windows")
    def test_qualifier_rejects_nested_junction_escape(self) -> None:
        with self.temporary_root() as temp:
            base = Path(temp)
            allowed = base / "beam-synthetic"
            workspace = allowed / "project"
            outside = base / "outside"
            junction = workspace / "escape"
            workspace.mkdir(parents=True)
            outside.mkdir()
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            profile = self.beam_profile_for_root(allowed)
            try:
                with mock.patch.object(
                    fleet, "SYNTHETIC_ONLY_WORKSPACE_ROOT", str(allowed)
                ), self.assertRaisesRegex(fleet.FleetError, "link escaping"):
                    fleet.qualify_beam_workspace("opencode-beam", profile, workspace)
            finally:
                os.rmdir(junction)

    def test_expired_trial_blocks_probe_and_credential_decryption(self) -> None:
        with self.temporary_root() as temp:
            base = Path(temp)
            allowed = base / "beam-synthetic"
            workspace = allowed / "project"
            workspace.mkdir(parents=True)
            profile = self.beam_profile_for_root(allowed)
            real_datetime = fleet.dt.datetime
            expiry = real_datetime.fromisoformat(
                fleet.SYNTHETIC_ONLY_TRIAL_EXPIRES_AT
            )

            class FutureDateTime(real_datetime):
                @classmethod
                def now(cls, tz=None):
                    current = expiry + timedelta(seconds=1)
                    return current.astimezone(tz) if tz is not None else current

            with (
                mock.patch.object(fleet, "SYNTHETIC_ONLY_WORKSPACE_ROOT", str(allowed)),
                mock.patch.object(fleet.dt, "datetime", FutureDateTime),
                mock.patch.object(fleet, "resolve_executable") as resolve,
                mock.patch.object(fleet, "decrypt_windows_dpapi_securestring") as decrypt,
            ):
                readiness, _executables = fleet.live_readiness(
                    {"opencode-beam": profile},
                    cwd=workspace,
                    mode="implement",
                    allow_synthetic_worker=True,
                )
                self.assertFalse(readiness["opencode-beam"]["ready"])
                self.assertIn(
                    "synthetic-workspace-or-trial-not-admitted",
                    readiness["opencode-beam"]["reasons"],
                )
                resolve.assert_not_called()
                with self.assertRaisesRegex(fleet.FleetError, "expired"):
                    fleet.apply_profile_credential({}, profile)
                with mock.patch.dict(fleet.os.environ, {"AGENT_FLEET_DEPTH": "0"}):
                    with self.assertRaisesRegex(fleet.FleetError, "expired"):
                        fleet.execute_single(
                            "Implement a synthetic fixture.",
                            profiles={"opencode-beam": profile},
                            agent_id="opencode-beam",
                            mode="implement",
                            kind="implementation",
                            complexity="low",
                            model_override=None,
                            workspace=workspace,
                            timeout=60,
                            allow_write=True,
                            native_subagent_limit=0,
                            enable_web=False,
                            inherit_api_keys=False,
                            dry_run=True,
                            output_root=base / "output",
                            root=base,
                            knowledge_mode="none",
                        )
                resolve.assert_not_called()
                decrypt.assert_not_called()

    def test_opencode_credential_env_var_uses_closed_provider_allowlist(self) -> None:
        invalid_provider = json.loads(json.dumps(self.beam))
        unapproved_id = "unapproved-reflection-provider"
        invalid_provider["enabled_providers"] = [unapproved_id]
        invalid_provider["provider"] = {
            unapproved_id: invalid_provider["provider"]["reflection"]
        }
        invalid_provider["deep_probe_args"][-1] = unapproved_id
        for execution in invalid_provider["execution_profiles"].values():
            execution["model_provider_id"] = unapproved_id
            execution["runtime_model_id"] = f"{unapproved_id}/Beam-1.0-501B-A23B"
        with self.assertRaisesRegex(fleet.FleetError, "closed allowlist"):
            fleet.opencode_profile_provider_contract(invalid_provider)

        with self.temporary_root() as temp:
            raw_profiles = json.loads(fleet.DEFAULT_PROFILES.read_text(encoding="utf-8"))
            raw_profiles["profiles"]["opencode-beam"] = invalid_provider
            profiles_path = Path(temp) / "profiles.json"
            profiles_path.write_text(json.dumps(raw_profiles), encoding="utf-8")
            with self.assertRaisesRegex(fleet.FleetError, "closed allowlist"):
                fleet.load_profiles(profiles_path)

        invalid_credential = json.loads(json.dumps(self.beam))
        invalid_credential["credential"]["env_var"] = "UNAPPROVED_API_KEY"
        decrypt = mock.patch.object(fleet, "decrypt_windows_dpapi_securestring")
        with decrypt as load_secret, self.assertRaises(fleet.FleetError):
            fleet.apply_profile_credential({}, invalid_credential)
        load_secret.assert_not_called()

    def test_reflection_credential_is_in_shared_secret_redaction_set(self) -> None:
        fake_key = "fake-reflection-beam-key-for-unit-test"
        self.assertIn("REFLECTION_API_KEY", fleet.SENSITIVE_ENV_KEYS)
        hardened = fleet.harden_muse_environment(
            {"REFLECTION_API_KEY": fake_key, "ZHIPU_API_KEY": "fake-zai-key"}
        )
        self.assertNotIn("REFLECTION_API_KEY", hardened)
        self.assertNotIn(fake_key, repr(hardened))
        self.assertNotIn(fake_key, fleet.redact_sensitive_output(fake_key, exact_secret=fake_key))

    def test_reflection_child_output_and_persisted_record_are_redacted(self) -> None:
        fake_key = "fake-reflection-beam-key-persistence-test"
        with self.temporary_root() as temp, mock.patch.dict(
            os.environ, {"REFLECTION_API_KEY": fake_key}
        ):
            exact_secret = os.environ["REFLECTION_API_KEY"]
            raw_stdout = json.dumps({"type": "text", "text": exact_secret})
            raw_stderr = json.dumps({"error": "provider echoed " + exact_secret})
            stdout = fleet.redact_sensitive_jsonl(
                raw_stdout, exact_secret=exact_secret
            )
            stderr = fleet.redact_sensitive_jsonl(
                raw_stderr, exact_secret=exact_secret
            )
            root = Path(temp)
            stdout_path = root / "attempt-1.stdout.txt"
            stderr_path = root / "attempt-1.stderr.txt"
            stdout_path.write_text(stdout, encoding="utf-8")
            stderr_path.write_text(stderr, encoding="utf-8")
            record = fleet.redact_sensitive_value(
                {
                    "run_id": "run-20261015T000000Z-a1b2c3d4e5",
                    "stdout": raw_stdout,
                    "stderr": raw_stderr,
                    "provider_attempts": [
                        {"stdout_path": str(stdout_path), "stderr_path": str(stderr_path)}
                    ],
                    "credential_echo": exact_secret,
                },
                exact_secret=exact_secret,
            )
            record_path = fleet.persist_run_record(root, record)
            persisted_record = record_path.read_text(encoding="utf-8")
            self.assertNotIn(exact_secret, stdout)
            self.assertNotIn(exact_secret, stderr)
            self.assertNotIn(exact_secret, stdout_path.read_text(encoding="utf-8"))
            self.assertNotIn(exact_secret, stderr_path.read_text(encoding="utf-8"))
            self.assertNotIn(exact_secret, persisted_record)

    def test_workspace_boundary_precedes_credential_decryption(self) -> None:
        with self.temporary_root() as temp:
            base = Path(temp)
            allowed = base / "beam-synthetic"
            outside = base / "outside"
            case_changed_sibling = base / "Beam-Synthetic-Evil"
            for directory in (allowed, outside, case_changed_sibling):
                directory.mkdir()
            profile = self.beam_profile_for_root(allowed)
            profiles = {"opencode-beam": profile}
            workspaces = (
                outside,
                Path("."),
                case_changed_sibling,
            )
            with mock.patch.object(fleet, "SYNTHETIC_ONLY_WORKSPACE_ROOT", str(allowed)):
                for workspace in workspaces:
                    with self.subTest(workspace=str(workspace)):
                        with mock.patch.object(
                            fleet, "decrypt_windows_dpapi_securestring"
                        ) as decrypt:
                            with self.assertRaises(fleet.FleetError):
                                fleet.execute_single(
                                    "Review a synthetic fixture.",
                                    profiles=profiles,
                                    agent_id="opencode-beam",
                                    mode="review",
                                    kind="review",
                                    complexity="low",
                                    model_override=None,
                                    workspace=workspace,
                                    timeout=60,
                                    allow_write=False,
                                    native_subagent_limit=0,
                                    enable_web=False,
                                    inherit_api_keys=False,
                                    dry_run=True,
                                    output_root=base / "output",
                                    root=base,
                                    knowledge_mode="none",
                                )
                            decrypt.assert_not_called()

    def test_profile_schema_validation_accepts_beam_contract(self) -> None:
        profile = self.beam
        self.assertEqual(profile["adapter"], "opencode")
        self.assertTrue(profile["routing_enabled"])
        self.assertEqual(profile["office_access"], "none")
        self.assertEqual(profile["trial_expires_at"], fleet.SYNTHETIC_ONLY_TRIAL_EXPIRES_AT)
        self.assertEqual(profile["supported_modes"], ["advisory", "review", "implement"])
        self.assertEqual(profile["data_boundary"]["synthetic_only"], True)
        self.assertEqual(
            profile["data_boundary"]["workspace_root"],
            fleet.SYNTHETIC_ONLY_WORKSPACE_ROOT,
        )
        self.assertEqual(set(profile["execution_profiles"]), {
            "opencode-beam-1-0-low",
            "opencode-beam-1-0-high",
            "opencode-beam-1-0-implement",
        })
        expected_readiness = json.loads(json.dumps(self.zai["readiness"]))
        actual_readiness = json.loads(json.dumps(profile["readiness"]))
        expected_deny_patterns = set(expected_readiness.pop("deny_patterns"))
        actual_deny_patterns = set(actual_readiness.pop("deny_patterns"))
        self.assertEqual(actual_readiness, expected_readiness)
        self.assertEqual(
            actual_deny_patterns,
            expected_deny_patterns | {"incorrect api key provided"},
        )
        self.assertEqual(profile["credential"]["env_var"], "REFLECTION_API_KEY")
        self.assertEqual(
            profile["credential"]["path"],
            fleet.REFLECTION_BEAM_CREDENTIAL_PATH,
        )
        for execution in profile["execution_profiles"].values():
            self.assertEqual(execution["lifecycle"], "LIMITED")
            self.assertFalse(execution["premium"])
        implementation = profile["execution_profiles"]["opencode-beam-1-0-implement"]
        self.assertIn(implementation["lifecycle"], fleet.routing_v2.ACTIVE_LIFECYCLES)
        self.assertEqual(implementation["max_risk"], "medium")
        self.assertEqual(implementation["supported_modes"], ["implement"])
        self.assertIn("filesystem-write", implementation["tools"])
        self.assertGreaterEqual(profile["fit"]["implementation"], 60)

    def test_automatic_ranking_requires_admitted_implementation_workspace(self) -> None:
        mocked_rows = [{"agent": "opencode-beam", "eligible": True, "launchable": True}]
        with mock.patch.object(fleet.routing_v2, "rank_execution_profiles", return_value=mocked_rows):
            refused = fleet.rank_profiles(
                {"opencode-beam": self.beam},
                kind="docs",
                complexity="low",
                counts={},
                availability={},
                routing_mode="adaptive",
                mode="implement",
            )
            admitted = fleet.rank_profiles(
                {"opencode-beam": self.beam},
                kind="docs",
                complexity="low",
                counts={},
                availability={},
                routing_mode="adaptive",
                mode="implement",
                admitted_synthetic_workers={"opencode-beam"},
            )
            read_only = fleet.rank_profiles(
                {"opencode-beam": self.beam},
                kind="docs",
                complexity="low",
                counts={},
                availability={},
                routing_mode="adaptive",
                mode="review",
                admitted_synthetic_workers={"opencode-beam"},
            )
        self.assertFalse(refused[0]["eligible"])
        self.assertFalse(refused[0]["launchable"])
        self.assertIn("synthetic-only-admitted-workspace-required", refused[0]["reasons"])
        self.assertTrue(admitted[0]["eligible"])
        self.assertTrue(admitted[0]["launchable"])
        self.assertFalse(read_only[0]["eligible"])

    def test_automatic_route_and_choose_agent_select_beam_as_worker(self) -> None:
        implementation = self.beam["execution_profiles"]["opencode-beam-1-0-implement"]
        beam_row = {
            "agent": "opencode-beam",
            "eligible": True,
            "launchable": True,
            "suggested_model": "Beam-1.0-501B-A23B",
            "selected_execution_profile": implementation,
            "execution_profile_id": "opencode-beam-1-0-implement",
            "assignment_score": 100.0,
            "score_version": "test",
            "gate_results": [],
            "routing_mode": "adaptive",
            "reasons": [],
        }
        with self.temporary_root() as temp:
            root = Path(temp)
            allowed = root / "beam-synthetic"
            workspace = allowed / "project"
            workspace.mkdir(parents=True)
            profile = self.beam_profile_for_root(allowed)
            profiles = {"opencode-beam": profile}
            with (
                mock.patch.object(fleet, "SYNTHETIC_ONLY_WORKSPACE_ROOT", str(allowed)),
                mock.patch.object(
                    fleet, "require_fresh_model_audit", return_value={"routing_status": "READY"}
                ),
                mock.patch.object(
                    fleet,
                    "live_readiness",
                    return_value=(
                        {"opencode-beam": {"ready": True, "synthetic_admitted": True}},
                        {},
                    ),
                ),
                mock.patch.object(fleet, "persist_readiness_exhaustion"),
                mock.patch.object(fleet, "read_usage", return_value=[]),
                mock.patch.object(fleet, "current_quota_views", return_value=({}, {})),
                mock.patch.object(fleet, "block_unavailable_readiness"),
                mock.patch.object(fleet, "load_fresh_model_benchmark", return_value={}),
                mock.patch.object(fleet, "read_claude_billing_policy", return_value={}),
                mock.patch.object(
                    fleet, "apply_billing_last_resort_order", side_effect=lambda rows, _policy: rows
                ),
                mock.patch.object(fleet, "rank_profiles", return_value=[beam_row]),
            ):
                routed = fleet.route_task(
                    "Implement a synthetic fixture.",
                    profiles=profiles,
                    kind="docs",
                    complexity="low",
                    root=root,
                    cwd=workspace,
                    mode="implement",
                    force_delegation=True,
                    allow_synthetic_worker=True,
                )
                chosen_agent, _kind, _route = fleet.choose_agent(
                    "auto",
                    "Implement a synthetic fixture.",
                    profiles=profiles,
                    kind="docs",
                    complexity="low",
                    root=root,
                    cwd=workspace,
                    mode="implement",
                    force_delegation=True,
                )
            self.assertEqual(routed["selected_agent"], "opencode-beam")
            self.assertEqual(
                routed["selected_execution_profile_id"], "opencode-beam-1-0-implement"
            )
            self.assertEqual(chosen_agent, "opencode-beam")

    def test_automatic_readiness_refuses_outside_root_before_resolving_or_probing(self) -> None:
        with self.temporary_root() as temp:
            base = Path(temp)
            allowed = base / "beam-synthetic"
            outside = base / "other-workspace"
            allowed.mkdir()
            outside.mkdir()
            profile = self.beam_profile_for_root(allowed)
            with (
                mock.patch.object(fleet, "SYNTHETIC_ONLY_WORKSPACE_ROOT", str(allowed)),
                mock.patch.object(fleet, "resolve_executable") as resolve,
                mock.patch.object(fleet, "probe_command") as probe,
                mock.patch.object(fleet, "decrypt_windows_dpapi_securestring") as decrypt,
            ):
                readiness, executables = fleet.live_readiness(
                    {"opencode-beam": profile},
                    cwd=outside,
                    mode="implement",
                    allow_synthetic_worker=True,
                )
                self.assertFalse(readiness["opencode-beam"]["ready"])
                self.assertIsNone(executables["opencode-beam"])
                resolve.assert_not_called()
                probe.assert_not_called()
                decrypt.assert_not_called()

    def test_council_selection_excludes_beam_and_rejects_explicit_beam(self) -> None:
        regular_ids = ("grok", "claude")
        selected_profiles = {
            "opencode-beam": self.beam,
            **{profile_id: self.profiles[profile_id] for profile_id in regular_ids},
        }
        rows = []
        for profile_id, profile in selected_profiles.items():
            execution_id = profile.get("execution_profile_map_by_mode", {}).get(
                "review", {}
            ).get("low", profile["execution_profile_map"]["low"])
            execution = profile["execution_profiles"][execution_id]
            rows.append(
                {
                    "agent": profile_id,
                    "eligible": True,
                    "launchable": True,
                    "adapter": profile["adapter"],
                    "fit": 50,
                    "quality_floor": 25,
                    "suggested_model": execution["model"],
                    "gate_results": [],
                }
            )
        route = {"rankings": rows}
        with mock.patch.object(
            fleet.fleet_deny_guard,
            "catalog_decision",
            return_value={"status": "allowed"},
        ):
            selected = fleet.select_council_members(
                route,
                selected_profiles,
                mode="review",
                requested="auto",
                count=2,
                all_models=False,
            )
            self.assertEqual({member["agent"] for member in selected}, set(regular_ids))
            beam_model = "Beam-1.0-501B-A23B"
            grok_model = rows[1]["suggested_model"]
            with self.assertRaisesRegex(fleet.FleetError, "Synthetic-only"):
                fleet.select_council_members(
                    route,
                    selected_profiles,
                    mode="review",
                    requested=f"opencode-beam@{beam_model},grok@{grok_model}",
                    count=2,
                    all_models=False,
                )

    def test_quality_auditor_selection_excludes_beam(self) -> None:
        selected_profiles = {
            "opencode-beam": self.beam,
            "grok": self.profiles["grok"],
        }
        route = {
            "rankings": [
                {
                    "agent": "opencode-beam",
                    "eligible": True,
                    "execution_profile_id": "opencode-beam-1-0-low",
                    "assignment_score": 100,
                },
                {
                    "agent": "grok",
                    "eligible": True,
                    "execution_profile_id": "grok-4-7-low",
                    "assignment_score": 50,
                },
            ],
            "task_profile": {"required_tools": []},
            "complexity": "low",
            "readiness": {"opencode-beam": {}, "grok": {}},
            "quota_snapshots": {"opencode-beam": {}, "grok": {}},
        }

        def execution_for(agent_id, profile, **_kwargs):
            configured_execution = next(iter(profile["execution_profiles"].values()))
            return {
                "execution_profile_id": f"{agent_id}-review",
                "model": configured_execution["model"],
                "model_provider_id": configured_execution.get(
                    "model_provider_id", profile["cli_provider_id"]
                ),
                "capabilities": {"independent-review": 100},
            }

        with (
            mock.patch.object(fleet, "resolve_execution_profile", side_effect=execution_for),
            mock.patch.object(fleet.routing_v2, "hard_gates", return_value=[]),
            mock.patch.object(
                fleet,
                "owner_authorized_muse_security_auditor_candidate",
                return_value=(None, None),
            ),
        ):
            selected, _diagnostics = fleet.quality_auditor_candidates(
                route,
                selected_profiles,
                [],
                contract={"risk": "low"},
                requested_agent="auto",
                requested_model=None,
            )
            self.assertEqual(selected["agent"], "grok")
            with self.assertRaisesRegex(fleet.FleetError, "Synthetic-only"):
                fleet.quality_auditor_candidates(
                    route,
                    selected_profiles,
                    [],
                    contract={"risk": "low"},
                    requested_agent="opencode-beam",
                    requested_model=None,
                )

    def test_execution_profile_allows_only_the_scoped_implementation_lane(self) -> None:
        implementation = fleet.resolve_execution_profile(
            "opencode-beam",
            self.beam,
            complexity="low",
            mode="implement",
            required_tools=["write", "filesystem-write"],
        )
        self.assertEqual(
            implementation["execution_profile_id"], "opencode-beam-1-0-implement"
        )
        with self.assertRaisesRegex(fleet.FleetError, "does not support review"):
            fleet.resolve_execution_profile(
                "opencode-beam",
                self.beam,
                complexity="low",
                mode="review",
                required_tools=["filesystem-write"],
                execution_profile_override="opencode-beam-1-0-implement",
            )

        wrong_model = json.loads(json.dumps(self.beam))
        wrong_model["execution_profiles"]["opencode-beam-1-0-implement"][
            "runtime_model_id"
        ] = "zai-coding-plan/GLM-5.3-Flash"
        with self.assertRaisesRegex(fleet.FleetError, "inventory"):
            fleet.opencode_profile_provider_contract(wrong_model)
        with self.assertRaises(fleet.FleetError):
            fleet.resolve_execution_profile(
                "opencode-beam",
                self.beam,
                complexity="low",
                mode="implement",
                required_tools=["filesystem-write"],
                model_override="zai-coding-plan/GLM-5.3-Flash",
            )

    def test_default_medium_risk_fallback_is_refused_but_low_is_accepted(self) -> None:
        execution = self.beam["execution_profiles"]["opencode-beam-1-0-low"]
        fallback = fleet.adaptive.fallback_assessment(
            kind="review", complexity="low", profiles={"opencode-beam": self.beam}
        )

        def risk_ceiling(assessment):
            gates = fleet.routing_v2.hard_gates(
                execution,
                profile=self.beam,
                assessment=assessment,
                readiness={"ready": True, "synthetic": True},
                quota={},
                mode="review",
            )
            return next(gate for gate in gates if gate["code"] == "risk-ceiling")

        self.assertEqual(fallback["risk"], "medium")
        self.assertFalse(risk_ceiling(fallback)["passed"])
        accepted = {**fallback, "risk": "low"}
        self.assertTrue(risk_ceiling(accepted)["passed"])
        implementation = self.beam["execution_profiles"]["opencode-beam-1-0-implement"]
        medium_risk = {**fallback, "risk": "medium", "required_tools": ["filesystem-write"]}
        implementation_gates = fleet.routing_v2.hard_gates(
            implementation,
            profile=self.beam,
            assessment=medium_risk,
            readiness={"ready": True, "synthetic": True},
            quota={},
            mode="implement",
        )
        self.assertTrue(
            next(gate for gate in implementation_gates if gate["code"] == "risk-ceiling")[
                "passed"
            ]
        )


if __name__ == "__main__":
    unittest.main()
