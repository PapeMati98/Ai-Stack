"""Regression: an explicit Graph DENY never reaches provider execution."""

from __future__ import annotations

import unittest
import json
from pathlib import Path
from unittest.mock import patch

import fleet


class FleetDenyPolicyTests(unittest.TestCase):
    def test_hard_muse_data_sharing_deny_precedes_catalog(self) -> None:
        profiles = json.loads(
            (Path(__file__).resolve().parents[1] / "references" / "profiles.json")
            .read_text(encoding="utf-8")
        )["profiles"]
        with patch.object(fleet.fleet_deny_guard, "require_catalog_allowed") as guard_call:
            with self.assertRaises(fleet.FleetError):
                fleet.execute_single(
                    "synthetic no-data task", profiles=profiles, agent_id="muse",
                    mode="review", kind="review", complexity="low",
                    model_override="muse-spark-contributor",
                    workspace=Path(__file__).resolve().parent, timeout=1,
                    allow_write=False, native_subagent_limit=0, enable_web=False,
                    inherit_api_keys=False, dry_run=True, output_root=None,
                    root=Path(__file__).resolve().parent,
                )
            guard_call.assert_not_called()

    def test_dedicated_legacy_security_review_profile_is_denied(self) -> None:
        profiles = json.loads(
            (Path(__file__).resolve().parents[1] / "references" / "profiles.json")
            .read_text(encoding="utf-8")
        )["profiles"]
        selected = profiles["codex"]["execution_profiles"]["codex-gpt-5-6-sol-security-review"]
        self.assertEqual(selected["model"], "gpt-5.6-sol")
        with (
            patch.object(fleet, "resolve_executable", return_value="fake-cli"),
            patch.object(fleet, "probe_command", return_value={"launchable": True}),
            patch.object(fleet, "profile_credential_status", return_value={"ready": True}),
            patch.object(fleet, "version_probe_environment", return_value={}),
            patch.object(fleet, "profile_probe_environment", return_value={}),
            patch.object(fleet.routing_v2, "readiness_from_probes", return_value={"ready": True}),
            patch.object(fleet, "merge_credential_readiness", return_value={"ready": True}),
        ):
            with self.assertRaisesRegex(fleet.FleetError, "DENY"):
                fleet.execute_single(
                    "synthetic no-data task", profiles=profiles, agent_id="codex",
                    mode="review", kind="review", complexity="high",
                    model_override=None, execution_profile=selected,
                    workspace=Path(__file__).resolve().parent, timeout=1,
                    allow_write=False, native_subagent_limit=0, enable_web=False,
                    inherit_api_keys=False, dry_run=True, output_root=None,
                    root=Path(__file__).resolve().parent,
                )

    def test_direct_model_override_refused_before_provider_launch(self) -> None:
        profiles = json.loads(
            (Path(__file__).resolve().parents[1] / "references" / "profiles.json")
            .read_text(encoding="utf-8")
        )["profiles"]
        with (
            patch.object(fleet, "resolve_executable", return_value="fake-cli"),
            patch.object(fleet, "probe_command", return_value={"launchable": True}),
            patch.object(fleet, "profile_credential_status", return_value={"ready": True}),
            patch.object(fleet, "version_probe_environment", return_value={}),
            patch.object(fleet, "profile_probe_environment", return_value={}),
            patch.object(fleet.routing_v2, "readiness_from_probes", return_value={"ready": True}),
            patch.object(fleet, "merge_credential_readiness", return_value={"ready": True}),
            patch.object(
                fleet, "run_provider_child", side_effect=AssertionError("provider launched")
            ) as provider_call,
            patch.object(
                fleet.fleet_deny_guard,
                "require_catalog_allowed",
                wraps=fleet.fleet_deny_guard.require_catalog_allowed,
            ) as guard_call,
        ):
            with self.assertRaisesRegex(fleet.FleetError, "DENY"):
                fleet.execute_single(
                    "synthetic no-data task",
                    profiles=profiles,
                    agent_id="codex",
                    mode="review",
                    kind="review",
                    complexity="low",
                    model_override="gpt-5.6-sol",
                    workspace=Path(__file__).resolve().parent,
                    timeout=1,
                    allow_write=False,
                    native_subagent_limit=0,
                    enable_web=False,
                    inherit_api_keys=False,
                    dry_run=False,
                    output_root=None,
                    root=Path(__file__).resolve().parent,
                )
            guard_call.assert_called_once_with("codex", "gpt-5.6-sol")
            provider_call.assert_not_called()

    def test_auto_selected_deny_refused_before_provider_run(self) -> None:
        profiles = json.loads(
            (Path(__file__).resolve().parents[1] / "references" / "profiles.json")
            .read_text(encoding="utf-8")
        )["profiles"]
        with (
            patch.object(fleet, "resolve_executable", return_value="fake-cli"),
            patch.object(fleet, "probe_command", return_value={"launchable": True}),
            patch.object(fleet, "profile_credential_status", return_value={"ready": True}),
            patch.object(fleet, "version_probe_environment", return_value={}),
            patch.object(fleet, "profile_probe_environment", return_value={}),
            patch.object(fleet.routing_v2, "readiness_from_probes", return_value={"ready": True}),
            patch.object(fleet, "merge_credential_readiness", return_value={"ready": True}),
            patch.object(
                fleet.routing_v2,
                "resolve_execution_profile",
                return_value=profiles["codex"]["execution_profiles"]["codex-gpt-5-6-sol"],
            ) as resolver,
            patch.object(
                fleet.fleet_deny_guard,
                "require_catalog_allowed",
                wraps=fleet.fleet_deny_guard.require_catalog_allowed,
            ) as guard_call,
        ):
            with self.assertRaisesRegex(fleet.FleetError, "DENY"):
                fleet.execute_single(
                    "synthetic no-data task",
                    profiles=profiles,
                    agent_id="codex",
                    mode="review",
                    kind="review",
                    complexity="low",
                    model_override=None,
                    workspace=Path(__file__).resolve().parent,
                    timeout=1,
                    allow_write=False,
                    native_subagent_limit=0,
                    enable_web=False,
                    inherit_api_keys=False,
                    dry_run=True,
                    output_root=None,
                    root=Path(__file__).resolve().parent,
                )
            guard_call.assert_called_once_with("codex", "gpt-5.6-sol")
            resolver.assert_called_once()


if __name__ == "__main__":
    unittest.main()
