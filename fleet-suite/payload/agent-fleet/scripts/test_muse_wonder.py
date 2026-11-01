"""Exact Muse model selection and opt-out; all provider calls are synthetic."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fleet
import native_availability


MODELS = {"muse-spark-1.3", "muse-wonder-1.0", "muse-wonder-full-1.0"}
FORBIDDEN = (
    "muse-wonder", "muse-wonder-full", "muse-wonder-1",
    "muse-wonder-1.0-contributor", "muse-wonder-full-1.0-contributor",
    "muse-spark-1.3-contributor", "muse-spark-1.2", "",
)


class MuseWonderTests(unittest.TestCase):
    def setUp(self):
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.profile = self.profiles["muse"]

    def test_each_exact_model_and_mode_reaches_argv_with_optout(self):
        for model in MODELS:
            for mode in ("advisory", "review", "implement"):
                with self.subTest(model=model, mode=mode):
                    fleet.require_safe_muse_model(model)
                    command, _, _ = fleet.build_command(
                        "muse", "muse-bin-1.4.9-R6297.1.exe",
                        prompt="Synthetic fixture.", workspace=Path.cwd(),
                        mode=mode, model=model, complexity="medium", timeout=30,
                        run_dir=Path.cwd() / "not-materialized", materialize=False,
                        native_subagent_limit=0, enable_web=False,
                    )
                    self.assertEqual(command.count("--model"), 1)
                    self.assertEqual(command[command.index("--model") + 1], model)
                    self.assertEqual(command.count("--contributor-mode"), 1)
                    self.assertEqual(command[command.index("--contributor-mode") + 1], "off")
                    self.assertEqual(command[command.index("--provider") + 1], "meta")
                    if mode != "implement":
                        self.assertIn("--disable-write", command)
                        self.assertIn("--disable-shell", command)

    def test_model_override_preserves_mode_and_effort(self):
        for model in MODELS:
            for mode in ("advisory", "review", "implement"):
                for complexity, effort in (("low", "low"), ("medium", "medium"), ("high", "high")):
                    with self.subTest(model=model, mode=mode, complexity=complexity):
                        resolved = fleet.resolve_execution_profile(
                            "muse", self.profile, complexity=complexity, mode=mode,
                            required_tools=["write"] if mode == "implement" else [],
                            model_override=model,
                        )
                        self.assertEqual(resolved["model"], model)
                        self.assertEqual(resolved["runtime_model_id"], model)
                        self.assertEqual(resolved["reasoning_level"], effort)
                        self.assertIn(mode, resolved["supported_modes"])
                        self.assertEqual("write" in resolved["tools"], mode == "implement")
                        self.assertEqual(resolved["lifecycle"], "LIMITED")

    def test_default_maps_stay_spark_and_quota_is_shared(self):
        self.assertEqual(set(self.profile["models"].values()), {"muse-spark-1.3"})
        self.assertEqual(self.profile["quota_pool_id"], "meta-muse-code-plan")
        for mode in ("advisory", "review", "implement"):
            for complexity in ("low", "medium", "high"):
                resolved = fleet.routing_v2.resolve_execution_profile(
                    "muse", self.profile, complexity=complexity, mode=mode, required_tools=[],
                )
                self.assertEqual(resolved["model"], "muse-spark-1.3")
        self.assertEqual(fleet.routing_v2.configured_models(self.profile), MODELS)

    def test_aliases_legacy_contributor_and_empty_rejected_before_process(self):
        for model in FORBIDDEN:
            with self.subTest(model=model), patch.object(fleet, "resolve_executable") as resolve, patch.object(fleet, "run_provider_child") as child:
                with self.assertRaises(fleet.FleetError):
                    self.execute(Path.cwd(), model, None)
                resolve.assert_not_called()
                child.assert_not_called()

    def test_profile_loader_rejects_forbidden_ids_and_substitution(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TEAM_TEST_ROOT")) as tmp:
            path = Path(tmp) / "synthetic-profiles.json"
            raw = json.loads(fleet.DEFAULT_PROFILES.read_text(encoding="utf-8"))
            for field in ("model", "runtime_model_id"):
                for model in FORBIDDEN:
                    with self.subTest(field=field, model=model):
                        data = copy.deepcopy(raw)
                        row = data["profiles"]["muse"]["execution_profiles"]["muse-spark-1-3-read-low"]
                        row[field] = model
                        path.write_text(json.dumps(data), encoding="utf-8")
                        with self.assertRaises(fleet.FleetError):
                            fleet.load_profiles(path)
            data = copy.deepcopy(raw)
            data["profiles"]["muse"]["execution_profiles"]["muse-spark-1-3-read-low"]["runtime_model_id"] = "muse-wonder-1.0"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(fleet.FleetError):
                fleet.load_profiles(path)

    def test_cli_minimum_and_synthetic_readiness(self):
        self.assertEqual(self.profile["readiness"]["minimum_version"], "1.4.9")
        self.assertEqual(self.profile["readiness"]["tested_through_version"], "1.4.9")
        self.assertEqual(self.profile["candidates"], [r"%LOCALAPPDATA%\Programs\muse\muse-bin-1.4.9-R6297.1.exe"])
        for version, expected in (("1.4.0", False), ("1.4.9", True)):
            with self.subTest(version=version):
                result = fleet.routing_v2.readiness_from_probes(
                    self.profile, executable="synthetic.exe",
                    version_probe={"launchable": True, "exit_code": 0, "output": "Muse Code " + version},
                    deep_probe={"launchable": True, "output": "\n".join(self.profile["readiness"]["required_deep_probe_lines"])},
                )
                with patch.object(fleet, "profile_credential_status", return_value={"required": False, "ready": True}):
                    result = fleet.merge_credential_readiness(result, self.profile)
                self.assertEqual(result["ready"], expected)
                if not expected:
                    self.assertIn("minimum-version-not-met", result["reasons"])
                    self.assertIn("contributor-mode-off-requires-muse-1.4.6", result["reasons"])

    def test_old_cli_refuses_execution_before_model_launch(self):
        probe = {"launchable": True, "exit_code": 0, "output": "Muse Code 1.4.0\n" + "\n".join(self.profile["readiness"]["required_deep_probe_lines"])}
        with patch.object(fleet, "resolve_executable", return_value="synthetic.exe"), patch.object(fleet, "require_muse_native_executable"), patch.object(fleet, "profile_credential_status", return_value={"required": False, "ready": True}), patch.object(fleet, "probe_command", return_value=probe), patch.object(fleet, "run_provider_child") as child:
            with self.assertRaisesRegex(fleet.FleetError, "contributor-mode"):
                self.execute(Path.cwd(), "muse-wonder-1.0", None)
            child.assert_not_called()

    def test_explicit_profile_cannot_substitute_a_different_model(self):
        with self.assertRaisesRegex(fleet.FleetError, "does not use model"):
            fleet.resolve_execution_profile(
                "muse", self.profile, complexity="high", mode="review", required_tools=[],
                model_override="muse-wonder-1.0", execution_profile_override="muse-spark-1-3-read-high",
            )

    def test_native_availability_model_probe_also_forces_contributor_off(self):
        for model in MODELS:
            with self.subTest(model=model), tempfile.TemporaryDirectory(dir=os.environ.get("TEAM_TEST_ROOT")) as tmp:
                profile = copy.deepcopy(self.profile)
                profile["models"]["low"] = model
                stdout = "\n".join(json.dumps(row) for row in [
                    {"payload_type": "run.model.configured", "payload": {"model_id": model, "provider_id": "meta"}},
                    {"payload_type": "run.terminal.completed", "payload": {"run_stream": "synthetic-probe", "terminal": "completed", "text": "OK"}},
                ])
                result = {"exit_code": 0, "stdout": stdout, "stderr": "", "model_tool_calls": 0}
                with patch.object(fleet, "resolve_executable", return_value="synthetic.exe"), patch.object(fleet, "require_muse_native_executable"), patch.object(fleet, "profile_runtime_environment", return_value={}), patch.object(native_availability.process_supervision, "run", return_value=result) as run:
                    observed = native_availability.native_probe(fleet, profile, Path(tmp))
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("--model") + 1], model)
                self.assertEqual(command.count("--contributor-mode"), 1)
                self.assertEqual(command[command.index("--contributor-mode") + 1], "off")
                self.assertEqual(observed["status"], "available")

    def test_native_identity_rejects_mixed_foreign_and_inexact_ids(self):
        for requested in MODELS:
            for observed, provider in ((requested, "meta"), (requested + "-contributor", "meta"), ("foreign", "meta"), (None, "meta"), (requested, "foreign")):
                with self.subTest(requested=requested, observed=observed, provider=provider):
                    text = json.dumps({"payload_type": "run.model.configured", "payload": {"model_id": observed, "provider_id": provider}})
                    identity = fleet.muse_model_identity(text, expected_model=requested)
                    self.assertEqual(identity["status"] == "verified", observed == requested and provider == "meta")
            configured = json.dumps({"payload_type": "run.model.configured", "payload": {"model_id": requested, "provider_id": "meta"}})
            other = next(model for model in MODELS if model != requested)
            self.assertEqual(fleet.muse_model_identity(configured + "\n" + json.dumps({"model_id": other}), expected_model=requested)["status"], "mismatch")
            padded = json.dumps({"payload_type": "run.model.configured", "payload": {"model_id": " " + requested, "provider_id": "meta"}})
            self.assertEqual(fleet.muse_model_identity(padded, expected_model=requested)["status"], "mismatch")
            self.assertEqual(fleet.muse_model_identity("{}", expected_model=requested)["status"], "unavailable")

    def test_native_identity_never_normalizes_model_or_provider_ids(self):
        for requested in MODELS:
            configured = {"payload_type": "run.model.configured", "payload": {"model_id": requested, "provider_id": "meta"}}
            for model, provider in ((" " + requested, "meta"), (requested + " ", "meta"), (requested.upper(), "meta"), (requested, " meta "), (requested, "Meta")):
                for location in ("configured", "additional-event"):
                    with self.subTest(requested=requested, model=model, provider=provider, location=location):
                        row = {"payload_type": "run.model.configured" if location == "configured" else "synthetic.model.usage", "payload": {"model_id": model, "provider_id": provider}}
                        rows = [row] if location == "configured" else [configured, row]
                        identity = fleet.muse_model_identity("\n".join(json.dumps(event) for event in rows), expected_model=requested)
                        self.assertEqual(identity["status"], "mismatch")

    def execute(self, root, model, execution_profile, mode="review"):
        # Only the mocked execution is tested, including when run by a Fleet child.
        with patch.dict(os.environ, {"AGENT_FLEET_DEPTH": "0"}):
            return fleet.execute_single(
                "Review a synthetic fixture.", profiles=self.profiles, agent_id="muse",
                mode=mode, kind="implementation" if mode == "implement" else "review", complexity="medium", model_override=model,
                workspace=root, timeout=30, allow_write=mode == "implement", native_subagent_limit=0,
                enable_web=False, inherit_api_keys=False, dry_run=False,
                output_root=root / "evidence", root=root, execution_profile=execution_profile,
                knowledge_mode="none",
            )

    def test_native_configured_identity_controls_actual_handoff_acceptance(self):
        handoff = {"status": "completed", "summary": "Synthetic review complete.", "files_changed": [], "verification": ["Synthetic check."], "risks": [], "next_steps": []}
        for observed in ("muse-wonder-1.0", "muse-wonder-1.0-contributor", "muse-spark-1.3", None):
            with self.subTest(observed=observed), tempfile.TemporaryDirectory(dir=os.environ.get("TEAM_TEST_ROOT")) as tmp:
                root = Path(tmp)
                rows = [{"payload_type": "run.terminal.completed", "payload": {"run_stream": "synthetic-wonder-run", "terminal": "completed", "text": json.dumps(handoff)}}]
                if observed is not None:
                    rows.insert(0, {"payload_type": "run.model.configured", "payload": {"provider_id": "meta", "model_id": observed}})
                child = {"exit_code": 0, "stdout": "\n".join(json.dumps(row) for row in rows), "stderr": "", "timed_out": False, "model_tool_calls": None, "tool_budget_exceeded": False}
                probe = {"launchable": True, "exit_code": 0, "output": "Muse Code 1.4.9\n" + "\n".join(self.profile["readiness"]["required_deep_probe_lines"])}
                with patch.object(fleet, "resolve_executable", return_value="synthetic.exe"), patch.object(fleet, "require_muse_native_executable"), patch.object(fleet, "profile_credential_status", return_value={"required": False, "ready": True}), patch.object(fleet, "probe_command", return_value=probe), patch.object(fleet.adaptive, "latest_quotas", return_value={}), patch.object(fleet, "run_provider_child", return_value={"child": child, "queue_wait_seconds": 0.0, "execution_serialized": False}) as run:
                    result = self.execute(root, "muse-wonder-1.0", None)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(result["model"], "muse-wonder-1.0")
                if observed == "muse-wonder-1.0":
                    self.assertEqual(result["status"], "COMPLETED", result.get("handoff_parse_error"))
                    self.assertEqual(result["handoff"], handoff)
                else:
                    self.assertEqual(result["status"], "FAILED")
                    self.assertIsNone(result["handoff"])
                    self.assertIn("exact-model gate failed", result["handoff_parse_error"])

    def test_catalog_tiers_roles_and_deny_patterns(self):
        catalog = json.loads((fleet.DEFAULT_PROFILES.parent / "model-catalog.json").read_text(encoding="utf-8"))
        models = {row["model_id"]: row for row in catalog["models"]}
        for model, tier in (("muse-wonder-full-1.0", "D"), ("muse-wonder-1.0", "M"), ("muse-spark-1.3", "K")):
            with self.subTest(model=model):
                self.assertEqual(models[model]["tier"], tier)
                self.assertEqual(models[model]["lineage"], "meta")
                self.assertIn({"agent": "muse", "model": model}, models[model]["profiles"])
                if model != "muse-spark-1.3":
                    self.assertEqual(set(models[model]["roles"]), {"plan", "review", "execute"})
        self.assertFalse(any("contributor" in model for model in models))
        self.assertIn("contributor", [row["pattern"] for row in catalog["policy"]["deny_model_patterns"]])


if __name__ == "__main__":
    unittest.main()
