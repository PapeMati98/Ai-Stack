"""Retained synthetic process tests; no provider, API or cleanup calls."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import fleet
import routing_v2
from process_supervision import ContextEventUnreadable
from test_fleet_context_budget import step_finish


class MiMoContextStopTests(unittest.TestCase):
    def test_only_verified_stream_adapters_can_enable_a_valid_limit(self):
        self.assertIsNone(fleet.profile_context_stop_tokens({"adapter": "mimo"}))
        for adapter in ("grok", "claude", "kimi", "codex"):
            with self.subTest(adapter=adapter), self.assertRaises(fleet.FleetError):
                fleet.profile_context_stop_tokens({"adapter": adapter, "context_stop_tokens": 200000})
        for invalid in (None, True, 63999, 4000001, 200000.0, "200000"):
            with self.subTest(value=invalid), self.assertRaises(fleet.FleetError):
                fleet.profile_context_stop_tokens({"adapter": "mimo", "context_stop_tokens": invalid})
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.assertEqual(fleet.profile_context_stop_tokens(profiles["mimo"]), 200000)
        self.assertEqual(fleet.profile_context_stop_tokens(profiles["opencode-mercury"]), 32768)
        self.assertEqual(fleet.profile_native_compaction_max_context(profiles["mimo"]), 180000)
        self.assertTrue(all("context_stop_tokens" not in p for name, p in profiles.items()
                            if name not in {"mimo", "opencode", "opencode-beam", "opencode-mercury"}))

    def test_per_step_cache_semantics_exclude_output_reasoning_and_total(self):
        event = step_finish("synthetic", fresh=4990, read=197569, write=0,
                            output=800000, reasoning=700000)
        self.assertEqual(fleet.mimo_stream_context_tokens(json.dumps(event)), 202559)
        self.assertIsNone(fleet.mimo_stream_context_tokens('{"type":"text","text":"999999"}'))
        self.assertIsNone(fleet.mimo_stream_context_tokens('{"type":"result","usage":{"input_tokens":999999}}'))

    def test_malformed_or_missing_step_counters_remain_unreadable(self):
        for line in ('{"type":"step_finish",', '{"type":"step_finish"}',
                     json.dumps(step_finish("bad", fresh=True, read=10)),
                     json.dumps(step_finish("bad", fresh=-1, read=10))):
            with self.subTest(line=line), self.assertRaises(ContextEventUnreadable):
                fleet.mimo_stream_context_tokens(line)

    def test_unknown_adapter_is_rejected_before_child_launch(self):
        with mock.patch.object(fleet.subprocess, "Popen") as spawn:
            with self.assertRaises(fleet.FleetError):
                fleet.run_bounded_child([sys.executable], cwd=Path.cwd(), env={},
                    stdin_text=None, timeout=1, adapter_id="grok",
                    max_model_tool_calls=None, max_context_tokens=200000)
            spawn.assert_not_called()

    def run_fixture(self, *, model="mimo-v2.6-pro", enabled=True, read_tokens=197569, malformed=False):
        root = Path(tempfile.mkdtemp(prefix="mimo-context-stop-retained-"))
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        profile = profiles["mimo"]
        if not enabled:
            profile.pop("context_stop_tokens")
        auth = root / "auth.json"
        auth.write_text(json.dumps({"xiaomi": {
            "type": "api", "key": "tp-test-only-not-a-real-key",
            "metadata": {"uid": "synthetic", "base_url": "https://token-plan-ams.xiaomimimo.com/v1"},
        }}), encoding="utf-8")
        profile["mimo"]["credential_store"]["path"] = str(auth)
        execution = routing_v2.resolve_execution_profile(
            "mimo", profile, complexity="low", mode="review", required_tools=[])
        identity = f"INFO 2026-09-28T00:00:00Z service=llm providerID=xiaomi modelID={model}"
        events = [step_finish("first", fresh=3183, read=4096),
                  step_finish("second", fresh=4990, read=read_tokens)]
        if malformed:
            events[-1]["part"]["tokens"].pop("input")
        code = (
            "import pathlib,json,sys,time\n"
            "pathlib.Path('retained-work.txt').write_text('synthetic work retained')\n"
            f"print({identity!r},file=sys.stderr,flush=True)\n"
            f"events={events!r}\n"
            "for event in events: print(json.dumps(event),flush=True); time.sleep(0.03)\n"
            "time.sleep(0.8)\n"
            "pathlib.Path('late.txt').write_text('child completed')\n"
        )
        original_child = fleet.run_provider_child
        launches = []
        def synthetic_child(command, **kwargs):
            launches.append(kwargs.get("max_context_tokens"))
            return original_child([sys.executable, "-u", "-c", code], **kwargs)
        env = dict(os.environ, AGENT_FLEET_DEPTH="0", AGENT_FLEET_OFFICE="0",
                   AGENT_FLEET_BILLING_MODE_PATH=str(root / "missing-billing.json"))
        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(fleet, "resolve_executable", return_value=str(root / "mimo.exe")),
            mock.patch.object(fleet.mimo_runtime, "require_mimo_native_executable"),
            mock.patch.object(fleet, "probe_command", return_value={"launchable": True, "exit_code": 0, "output": "ok"}),
            mock.patch.object(fleet, "profile_credential_status", return_value={"ready": True}),
            mock.patch.object(fleet, "merge_credential_readiness", return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "readiness_from_probes", return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "runtime_model_visible", return_value=(True, "visible")),
            mock.patch.object(routing_v2, "quota_hard_gates", return_value=[]),
            mock.patch.object(fleet, "current_quota_views", return_value=({"mimo": {"status": "fresh", "headroom": 1.0, "confidence": 1.0}}, {})),
            mock.patch.object(fleet, "run_provider_child", side_effect=synthetic_child),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            result = fleet.execute_single(
                "Synthetic numeric context fixture only.", profiles=profiles, agent_id="mimo",
                mode="review", kind="review", complexity="low", model_override=None,
                workspace=root, timeout=5, allow_write=False, native_subagent_limit=0,
                enable_web=False, inherit_api_keys=False, dry_run=False,
                output_root=root / "evidence", root=root,
                execution_profile=execution, knowledge_mode="none")
        record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
        return result, record, root, launches

    def test_actual_process_stop_propagates_as_partial_and_keeps_work(self):
        result, record, root, launches = self.run_fixture()
        self.assertEqual(launches, [200000], "No automatic retry")
        self.assertEqual(result["exit_code"], 127)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(record["run_status"], "PARTIAL")
        self.assertEqual(record["completion_reason"], "observed-context-limit-needs-parent-review")
        self.assertEqual(record["context_guard"]["observed_peak_tokens"], 202559)
        self.assertEqual(record["context_guard"]["status"], "exceeded")
        self.assertEqual(record["context_telemetry"]["max_step_input"], 202559)
        self.assertEqual(record["model_identity"]["status"], "verified")
        self.assertEqual((root / "retained-work.txt").read_text(), "synthetic work retained")
        self.assertFalse((root / "late.txt").exists())
        self.assertIn('"step_finish"', Path(result["stdout_path"]).read_text())
        self.assertTrue(result["parent_acceptance_required"])

    def test_wrong_model_cannot_be_upgraded_to_partial_by_context_stop(self):
        result, record, root, launches = self.run_fixture(model="foreign-model")
        self.assertEqual(record["model_identity"]["status"], "mismatch")
        self.assertNotEqual(result["status"], "PARTIAL")
        self.assertNotEqual(result["status"], "COMPLETED")
        self.assertTrue(record["context_guard"]["exceeded"])
        self.assertTrue((root / "retained-work.txt").exists())

    def test_absent_profile_field_does_not_apply_a_context_stop(self):
        result, record, root, launches = self.run_fixture(enabled=False)
        self.assertEqual(launches, [None])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(record["context_guard"]["status"], "not-configured")
        self.assertFalse(record["context_guard"]["applied"])
        self.assertTrue((root / "late.txt").exists())

    def test_below_peak_is_an_observation_not_an_acceptance(self):
        result, record, root, launches = self.run_fixture(read_tokens=10000)
        self.assertEqual(record["context_guard"]["status"], "observed-below")
        self.assertFalse(record["context_guard"]["exceeded"])
        self.assertEqual(record["context_guard"]["observed_peak_tokens"], 14990)
        self.assertNotEqual(result["status"], "COMPLETED", "No valid final handoff in this fixture")

    def test_missing_step_input_cannot_prove_a_below_limit_run(self):
        result, record, root, launches = self.run_fixture(malformed=True)
        self.assertEqual(record["context_guard"]["status"], "unknown")
        self.assertTrue(record["context_guard"]["scan_incomplete"])
        self.assertEqual(record["context_guard"]["observed_peak_tokens"], 7279)
        self.assertFalse(record["context_guard"]["exceeded"])


if __name__ == "__main__":
    unittest.main()
