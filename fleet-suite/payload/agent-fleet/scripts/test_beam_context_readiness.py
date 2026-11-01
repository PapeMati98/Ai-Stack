"""Default-interface Beam readiness regression tests."""

from __future__ import annotations

import builtins
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import fleet
from test_opencode_beam import OpenCodeBeamTests


class BeamDefaultReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.beam = fleet.load_profiles(fleet.DEFAULT_PROFILES)["opencode-beam"]

    def _fixture(self, workspace_root: Path) -> dict:
        return OpenCodeBeamTests.beam_profile_for_root(self, workspace_root)

    def _temporary_root(self):
        return OpenCodeBeamTests.temporary_root(self)

    @staticmethod
    def _probe_result(output: str) -> dict:
        return {
            "launchable": True,
            "exit_code": 0,
            "output": output,
            "error": None,
            "duration_seconds": 0.0,
        }

    def _ready_probe_mocks(self):
        status = {
            "required": True,
            "ready": True,
            "reason": "test-fixture",
            "path": "test-only-placeholder",
            "env_var": "REFLECTION_API_KEY",
        }
        return (
            mock.patch.object(fleet, "resolve_executable", return_value="opencode.exe"),
            mock.patch.object(
                fleet,
                "probe_command",
                side_effect=[
                    self._probe_result("1.18.21"),
                    self._probe_result("reflection/Beam-1.0-501B-A23B"),
                ],
            ),
            mock.patch.object(fleet, "profile_credential_status", return_value=status),
            mock.patch.object(
                fleet, "decrypt_windows_dpapi_securestring", return_value="test-only-key"
            ),
        )

    def test_default_interface_admits_healthy_qualified_workspace(self) -> None:
        with self._temporary_root() as temp:
            base = Path(temp)
            allowed = base / "beam-synthetic"
            workspace = allowed / "project"
            workspace.mkdir(parents=True)
            profile = self._fixture(allowed)
            resolve, probe, credential_status, decrypt = self._ready_probe_mocks()
            with (
                mock.patch.object(fleet, "SYNTHETIC_ONLY_WORKSPACE_ROOT", str(allowed)),
                resolve as resolve_mock,
                probe as probe_mock,
                credential_status,
                decrypt as decrypt_mock,
            ):
                readiness, executables = fleet.live_readiness(
                    {"opencode-beam": profile}, cwd=workspace
                )

            row = readiness["opencode-beam"]
            self.assertTrue(row["ready"], row["reasons"])
            self.assertTrue(row["synthetic_admitted"])
            self.assertEqual(executables["opencode-beam"], "opencode.exe")
            resolve_mock.assert_called_once_with(profile)
            self.assertEqual(probe_mock.call_count, 2)
            self.assertEqual(
                probe_mock.call_args_list[0].kwargs["cwd"], workspace.resolve()
            )
            self.assertEqual(
                probe_mock.call_args_list[1].kwargs["cwd"], workspace.resolve()
            )
            decrypt_mock.assert_called_once()

    def test_legacy_optional_arguments_remain_accepted(self) -> None:
        with self._temporary_root() as temp:
            base = Path(temp)
            allowed = base / "beam-synthetic"
            workspace = allowed / "project"
            workspace.mkdir(parents=True)
            profile = self._fixture(allowed)
            resolve, probe, credential_status, decrypt = self._ready_probe_mocks()
            with (
                mock.patch.object(fleet, "SYNTHETIC_ONLY_WORKSPACE_ROOT", str(allowed)),
                resolve,
                probe,
                credential_status,
                decrypt,
            ):
                readiness, _executables = fleet.live_readiness(
                    {"opencode-beam": profile},
                    cwd=workspace,
                    mode="implement",
                    allow_synthetic_worker=True,
                    requested_agent="opencode-beam",
                )
            self.assertTrue(readiness["opencode-beam"]["synthetic_admitted"])

    def test_outside_workspace_is_rejected_before_probes_or_credentials(self) -> None:
        with self._temporary_root() as temp:
            base = Path(temp)
            allowed = base / "beam-synthetic"
            outside = base / "other-workspace"
            allowed.mkdir()
            outside.mkdir()
            profile = self._fixture(allowed)
            with (
                mock.patch.object(fleet, "SYNTHETIC_ONLY_WORKSPACE_ROOT", str(allowed)),
                mock.patch.object(fleet, "resolve_executable") as resolve,
                mock.patch.object(fleet, "probe_command") as probe,
                mock.patch.object(fleet, "profile_credential_status") as credential_status,
                mock.patch.object(fleet, "decrypt_windows_dpapi_securestring") as decrypt,
            ):
                readiness, executables = fleet.live_readiness(
                    {"opencode-beam": profile}, cwd=outside
                )
            self.assertFalse(readiness["opencode-beam"]["ready"])
            self.assertIn(
                "synthetic-workspace-or-trial-not-admitted",
                readiness["opencode-beam"]["reasons"],
            )
            self.assertIsNone(executables["opencode-beam"])
            resolve.assert_not_called()
            probe.assert_not_called()
            credential_status.assert_not_called()
            decrypt.assert_not_called()

    def test_expired_trial_is_rejected_before_probes_or_credentials(self) -> None:
        with self._temporary_root() as temp:
            base = Path(temp)
            allowed = base / "beam-synthetic"
            workspace = allowed / "project"
            workspace.mkdir(parents=True)
            profile = self._fixture(allowed)
            real_datetime = fleet.dt.datetime
            expiry = real_datetime.fromisoformat(fleet.SYNTHETIC_ONLY_TRIAL_EXPIRES_AT)

            class FutureDateTime(real_datetime):
                @classmethod
                def now(cls, tz=None):
                    current = expiry + timedelta(seconds=1)
                    return current.astimezone(tz) if tz is not None else current

            with (
                mock.patch.object(fleet, "SYNTHETIC_ONLY_WORKSPACE_ROOT", str(allowed)),
                mock.patch.object(fleet.dt, "datetime", FutureDateTime),
                mock.patch.object(fleet, "resolve_executable") as resolve,
                mock.patch.object(fleet, "probe_command") as probe,
                mock.patch.object(fleet, "profile_credential_status") as credential_status,
                mock.patch.object(fleet, "decrypt_windows_dpapi_securestring") as decrypt,
            ):
                readiness, executables = fleet.live_readiness(
                    {"opencode-beam": profile}, cwd=workspace
                )
            self.assertFalse(readiness["opencode-beam"]["ready"])
            self.assertIn(
                "synthetic-workspace-or-trial-not-admitted",
                readiness["opencode-beam"]["reasons"],
            )
            self.assertIsNone(executables["opencode-beam"])
            resolve.assert_not_called()
            probe.assert_not_called()
            credential_status.assert_not_called()
            decrypt.assert_not_called()

    def test_execute_single_rejects_managed_chrome_before_credentials_probes_or_binding(self) -> None:
        with self._temporary_root() as temp:
            base = Path(temp)
            allowed = base / "beam-synthetic"
            workspace = allowed / "project"
            workspace.mkdir(parents=True)
            profile = self._fixture(allowed)
            real_import = builtins.__import__
            binding_imports = []

            def track_binding_import(name, *args, **kwargs):
                if name == "fleet_chrome_integration":
                    binding_imports.append(name)
                    raise AssertionError("managed Chrome binding module must not be imported")
                return real_import(name, *args, **kwargs)

            with (
                mock.patch.object(fleet, "SYNTHETIC_ONLY_WORKSPACE_ROOT", str(allowed)),
                mock.patch.dict(fleet.os.environ, {"AGENT_FLEET_DEPTH": "0"}),
                mock.patch("builtins.__import__", side_effect=track_binding_import),
                mock.patch.object(fleet, "resolve_executable") as resolve,
                mock.patch.object(fleet, "probe_command") as probe,
                mock.patch.object(fleet, "profile_credential_status") as credential_status,
                mock.patch.object(fleet, "decrypt_windows_dpapi_securestring") as decrypt,
            ):
                with self.assertRaisesRegex(fleet.FleetError, "Beam cannot receive managed Chrome"):
                    fleet.execute_single(
                        "Implement a synthetic fixture.",
                        profiles={"opencode-beam": profile},
                        agent_id="opencode-beam",
                        mode="implement",
                        kind="implementation",
                        complexity="low",
                        model_override="Beam-1.0-501B-A23B",
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
                        managed_chrome=True,
                    )
            self.assertEqual(binding_imports, [])
            resolve.assert_not_called()
            probe.assert_not_called()
            credential_status.assert_not_called()
            decrypt.assert_not_called()
