"""Tests for Mistral native progress stall detection and diagnosis (HD-20261013-906853)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

# Ensure candidate directory is on sys.path for direct imports
CANDIDATE_DIR = Path(__file__).resolve().parent
if str(CANDIDATE_DIR) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_DIR))

import fleet
import mistral_runtime
import process_supervision


class SupervisionProgressStallTests(unittest.TestCase):
    def _create_retained_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="mistral_stall_retained_"))

    def test_real_child_process_stalled_stop(self):
        work_dir = self._create_retained_dir()
        checkpoint_path = work_dir / "checkpoint.json"
        t0 = time.monotonic()
        probe = lambda: time.monotonic() - t0
        command = [sys.executable, "-c", "import time; time.sleep(20)"]
        res = process_supervision.run(
            command,
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=15.0,
            checkpoint_path=checkpoint_path,
            progress_probe=probe,
            progress_stall_seconds=1.0,
        )
        self.assertEqual(res["exit_code"], 124)
        self.assertTrue(res["timed_out"])
        self.assertTrue(res["progress_stalled"])
        self.assertEqual(res["progress_stall_seconds"], 1.0)
        self.assertIsNotNone(res["progress_last_observed_seconds"])
        self.assertGreater(res["progress_last_observed_seconds"], 1.0)
        self.assertFalse(res["progress_probe_failed"])
        self.assertLess(res["execution_seconds"], 10.0)
        self.assertIn("without native progress", res["stderr"])
        self.assertIn("Agent Fleet stopped the provider after", res["stderr"])
        self.assertTrue(checkpoint_path.exists())
        checkpoint_data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint_data.get("status"), "stalled")
        self.assertEqual(checkpoint_data.get("exit_code"), 124)

    def test_control_child_process_probe_zero_no_stall(self):
        work_dir = self._create_retained_dir()
        checkpoint_path = work_dir / "checkpoint.json"
        probe = lambda: 0.0
        command = [sys.executable, "-c", "import time; time.sleep(0.1)"]
        res = process_supervision.run(
            command,
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=10.0,
            checkpoint_path=checkpoint_path,
            progress_probe=probe,
            progress_stall_seconds=1.0,
        )
        self.assertEqual(res["exit_code"], 0)
        self.assertFalse(res["timed_out"])
        self.assertFalse(res["progress_stalled"])
        self.assertFalse(res["progress_probe_failed"])
        self.assertNotIn("without native progress", res["stderr"])
        self.assertTrue(checkpoint_path.exists())
        checkpoint_data = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint_data.get("status"), "completed")

    def test_probe_exception_does_not_fail_supervision(self):
        work_dir = self._create_retained_dir()

        def faulty_probe():
            raise RuntimeError("simulated probe failure")

        command = [sys.executable, "-c", "import time; time.sleep(0.1); print('child-done')"]
        res = process_supervision.run(
            command,
            cwd=str(work_dir),
            env=dict(os.environ),
            stdin_text=None,
            timeout=10.0,
            progress_probe=faulty_probe,
            progress_stall_seconds=1.0,
        )
        self.assertEqual(res["exit_code"], 0)
        self.assertFalse(res["timed_out"])
        self.assertFalse(res["progress_stalled"])
        self.assertTrue(res["progress_probe_failed"])
        self.assertFalse(res["supervision_failed"])
        self.assertIn("Agent Fleet native progress probe failed; stall detection was unavailable.", res["stderr"])
        self.assertIn("child-done", res["stdout"])

    def test_supervision_parameter_validation(self):
        work_dir = self._create_retained_dir()
        command = [sys.executable, "-c", "pass"]
        # One parameter without the other -> ValueError
        with self.assertRaises(ValueError):
            process_supervision.run(
                command,
                cwd=str(work_dir),
                env=dict(os.environ),
                stdin_text=None,
                timeout=5.0,
                progress_probe=lambda: 0.0,
                progress_stall_seconds=None,
            )
        with self.assertRaises(ValueError):
            process_supervision.run(
                command,
                cwd=str(work_dir),
                env=dict(os.environ),
                stdin_text=None,
                timeout=5.0,
                progress_probe=None,
                progress_stall_seconds=10.0,
            )
        # Non-callable probe -> TypeError
        with self.assertRaises(TypeError):
            process_supervision.run(
                command,
                cwd=str(work_dir),
                env=dict(os.environ),
                stdin_text=None,
                timeout=5.0,
                progress_probe="not_a_callable",
                progress_stall_seconds=10.0,
            )
        # Zero, negative, bool, or non-numeric limit -> ValueError
        for invalid_limit in (0, -1, -5.0, True, False, "10", [10]):
            with self.subTest(invalid_limit=invalid_limit):
                with self.assertRaises(ValueError):
                    process_supervision.run(
                        command,
                        cwd=str(work_dir),
                        env=dict(os.environ),
                        stdin_text=None,
                        timeout=5.0,
                        progress_probe=lambda: 0.0,
                        progress_stall_seconds=invalid_limit,
                    )


class NativeStallSecondsTests(unittest.TestCase):
    def test_default_value(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(mistral_runtime.native_stall_seconds(3600), 1200)
        self.assertEqual(mistral_runtime.MISTRAL_STALL_SECONDS_DEFAULT, 1200)
        self.assertEqual(mistral_runtime.MISTRAL_STALL_SECONDS_ENV, "FLEET_MISTRAL_STALL_SECONDS")

    def test_env_zero_disables(self):
        with mock.patch.dict(os.environ, {mistral_runtime.MISTRAL_STALL_SECONDS_ENV: "0"}):
            self.assertIsNone(mistral_runtime.native_stall_seconds(3600))

    def test_env_custom_limit(self):
        with mock.patch.dict(os.environ, {mistral_runtime.MISTRAL_STALL_SECONDS_ENV: "900"}):
            self.assertEqual(mistral_runtime.native_stall_seconds(3600), 900)

    def test_timeout_less_than_or_equal_to_limit(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(mistral_runtime.native_stall_seconds(1200))
            self.assertIsNone(mistral_runtime.native_stall_seconds(1000))
            self.assertEqual(mistral_runtime.native_stall_seconds(1201), 1200)
        with mock.patch.dict(os.environ, {mistral_runtime.MISTRAL_STALL_SECONDS_ENV: "900"}):
            self.assertIsNone(mistral_runtime.native_stall_seconds(900))
            self.assertIsNone(mistral_runtime.native_stall_seconds(500))
            self.assertEqual(mistral_runtime.native_stall_seconds(901), 900)

    def test_garbage_env_falls_back_to_default(self):
        for val in ("garbage", "-50", "   ", "12a", "none"):
            with self.subTest(val=val):
                with mock.patch.dict(os.environ, {mistral_runtime.MISTRAL_STALL_SECONDS_ENV: val}):
                    self.assertEqual(mistral_runtime.native_stall_seconds(3600), 1200)


class NativeProgressProbeTests(unittest.TestCase):
    def _create_retained_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="mistral_stall_retained_"))

    def test_empty_run_dir_returns_elapsed_since_start(self):
        work_dir = self._create_retained_dir()
        t0 = time.time() - 15.0
        probe = mistral_runtime.native_progress_probe(work_dir, started_at=t0)
        delta = probe()
        self.assertIsNotNone(delta)
        self.assertGreaterEqual(delta, 15.0)

        probe_now = mistral_runtime.native_progress_probe(work_dir)
        delta_now = probe_now()
        self.assertIsNotNone(delta_now)
        self.assertGreaterEqual(delta_now, 0.0)
        self.assertLess(delta_now, 2.0)

    def test_touching_telemetry_file_resets_probe(self):
        work_dir = self._create_retained_dir()
        probe = mistral_runtime.native_progress_probe(work_dir, started_at=time.time() - 80.0)
        self.assertGreaterEqual(probe(), 80.0)

        telem = work_dir / "mistral-tool-telemetry.jsonl"
        telem.write_text('{"event": "call"}\n', encoding="utf-8")
        delta = probe()
        self.assertIsNotNone(delta)
        self.assertLess(delta, 2.0)

    def test_newer_messages_jsonl_in_session_resets_probe(self):
        work_dir = self._create_retained_dir()
        telem = work_dir / "mistral-tool-telemetry.jsonl"
        telem.write_text('{"old": 1}\n', encoding="utf-8")
        old_time = time.time() - 60.0
        os.utime(str(telem), (old_time, old_time))

        session_dir = work_dir / "mistral-workspace" / ".vibe" / "logs" / "session" / "sess-1"
        session_dir.mkdir(parents=True, exist_ok=True)
        msg_file = session_dir / "messages.jsonl"
        msg_file.write_text('{"msg": 1}\n', encoding="utf-8")

        probe = mistral_runtime.native_progress_probe(work_dir, started_at=old_time)
        delta = probe()
        self.assertIsNotNone(delta)
        self.assertLess(delta, 2.0)

    def test_newer_vibe_log_resets_probe(self):
        work_dir = self._create_retained_dir()
        vibe_log = work_dir / "mistral-workspace" / ".vibe" / "logs" / "vibe.log"
        vibe_log.parent.mkdir(parents=True, exist_ok=True)
        vibe_log.write_text("vibe logger line\n", encoding="utf-8")

        probe = mistral_runtime.native_progress_probe(work_dir, started_at=time.time() - 120.0)
        delta = probe()
        self.assertIsNotNone(delta)
        self.assertLess(delta, 2.0)

    def test_ignored_directory_and_symlink(self):
        work_dir = self._create_retained_dir()
        # Directory with matching name is ignored
        dir_telem = work_dir / "mistral-tool-telemetry.jsonl"
        dir_telem.mkdir()
        t0 = time.time() - 30.0
        probe = mistral_runtime.native_progress_probe(work_dir, started_at=t0)
        self.assertGreaterEqual(probe(), 30.0)

        # Symlink is ignored if supported on platform
        real_file = work_dir / "real.log"
        real_file.write_text("content\n", encoding="utf-8")
        link_target = work_dir / "mistral-workspace" / ".vibe" / "logs" / "vibe.log"
        link_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(str(real_file), str(link_target))
            symlink_created = True
        except (OSError, NotImplementedError):
            symlink_created = False
        if symlink_created:
            probe_sym = mistral_runtime.native_progress_probe(work_dir, started_at=t0)
            self.assertGreaterEqual(probe_sym(), 30.0)


class ReadNativeSessionStatsTests(unittest.TestCase):
    def _create_retained_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="mistral_stall_retained_"))

    def test_full_synthetic_meta_copied_and_marker_absent(self):
        work_dir = self._create_retained_dir()
        session_dir = work_dir / "mistral-workspace" / ".vibe" / "logs" / "session" / "sess-1"
        session_dir.mkdir(parents=True, exist_ok=True)
        meta_content = {
            "schema_version": 1,
            "system_prompt": "SYNTHETIC-PRIVATE-MARKER-prompt-secret",
            "title": "SYNTHETIC-PRIVATE-MARKER-title-secret",
            "path": "C:\\secret\\SYNTHETIC-PRIVATE-MARKER",
            "model": "mistral-large-4",
            "start_time": "2026-10-20T05:00:00Z",
            "end_time": "2026-10-20T05:31:29Z",
            "stats": {
                "steps": 11,
                "session_prompt_tokens": 5000,
                "session_completion_tokens": 86459,
                "last_turn_prompt_tokens": 400,
                "last_turn_completion_tokens": 86459,
                "last_turn_duration": 1889.3,
                "tokens_per_second": 45.768,
                "tool_calls_failed": 8,
                "tool_calls_succeeded": 10,
                "context_tokens": 120000,
            },
        }
        (session_dir / "meta.json").write_text(json.dumps(meta_content), encoding="utf-8")
        msg_file = session_dir / "messages.jsonl"
        msg_file.write_text('{"turn": 1}\n', encoding="utf-8")

        stats = mistral_runtime.read_native_session_stats(work_dir)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["steps"], 11)
        self.assertEqual(stats["session_prompt_tokens"], 5000)
        self.assertEqual(stats["session_completion_tokens"], 86459)
        self.assertEqual(stats["last_turn_prompt_tokens"], 400)
        self.assertEqual(stats["last_turn_completion_tokens"], 86459)
        self.assertEqual(stats["last_turn_seconds"], 1889)
        self.assertEqual(stats["tokens_per_second"], 45.8)
        self.assertEqual(stats["tool_calls_failed"], 8)
        self.assertEqual(stats["tool_calls_succeeded"], 10)
        self.assertEqual(stats["context_tokens"], 120000)
        self.assertEqual(stats["start_time"], "2026-10-20T05:00:00Z")
        self.assertEqual(stats["end_time"], "2026-10-20T05:31:29Z")
        self.assertIn("last_progress_at", stats)

        serialized = json.dumps(stats)
        self.assertNotIn("SYNTHETIC-PRIVATE-MARKER", serialized)
        self.assertNotIn("system_prompt", stats)
        self.assertNotIn("title", stats)
        self.assertNotIn("path", stats)
        self.assertNotIn("model", stats)

    def test_live_vibe_layout_helper_dirs_are_ignored(self):
        # Vibe 2.26.1 writes `.last_session/ppid-<n>` and `active/.registry` beside `session_<id>/`.
        work_dir = self._create_retained_dir()
        root = work_dir / "mistral-workspace" / ".vibe" / "logs" / "session"
        (root / ".last_session").mkdir(parents=True, exist_ok=True)
        (root / ".last_session" / "ppid-81328").write_text("x", encoding="utf-8")
        (root / "active").mkdir(parents=True, exist_ok=True)
        (root / "active" / ".registry").write_text("", encoding="utf-8")
        session = root / "session_20261020_060938_c5747a2b"
        session.mkdir(parents=True, exist_ok=True)
        (session / "meta.json").write_text(json.dumps({"stats": {"steps": 3, "last_turn_duration": 2.78}}),
                                           encoding="utf-8")
        (session / "messages.jsonl").write_text('{"turn": 1}\n', encoding="utf-8")

        stats = mistral_runtime.read_native_session_stats(work_dir)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["steps"], 3)
        self.assertEqual(stats["last_turn_seconds"], 3)

    def test_multiple_sessions_newest_meta_wins(self):
        work_dir = self._create_retained_dir()
        root = work_dir / "mistral-workspace" / ".vibe" / "logs" / "session"
        s1 = root / "sess-1"
        s2 = root / "sess-2"
        s1.mkdir(parents=True, exist_ok=True)
        s2.mkdir(parents=True, exist_ok=True)
        (s1 / "meta.json").write_text(json.dumps({"stats": {"steps": 1}}), encoding="utf-8")
        (s2 / "meta.json").write_text(json.dumps({"stats": {"steps": 2}}), encoding="utf-8")
        now = time.time()
        os.utime(s1 / "meta.json", (now - 600, now - 600))
        os.utime(s2 / "meta.json", (now - 60, now - 60))
        self.assertEqual(mistral_runtime.read_native_session_stats(work_dir)["steps"], 2)
        os.utime(s1 / "meta.json", (now, now))
        self.assertEqual(mistral_runtime.read_native_session_stats(work_dir)["steps"], 1)

    def test_no_directory_with_meta_returns_none(self):
        work_dir = self._create_retained_dir()
        root = work_dir / "mistral-workspace" / ".vibe" / "logs" / "session"
        (root / "active").mkdir(parents=True, exist_ok=True)
        (root / "active" / ".registry").write_text("", encoding="utf-8")
        self.assertIsNone(mistral_runtime.read_native_session_stats(work_dir))

    def test_missing_stats_dict_returns_none(self):
        work_dir = self._create_retained_dir()
        s1 = work_dir / "mistral-workspace" / ".vibe" / "logs" / "session" / "sess-1"
        s1.mkdir(parents=True, exist_ok=True)
        (s1 / "meta.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

        self.assertIsNone(mistral_runtime.read_native_session_stats(work_dir))

    def test_booleans_and_strings_in_numeric_fields_dropped(self):
        work_dir = self._create_retained_dir()
        s1 = work_dir / "mistral-workspace" / ".vibe" / "logs" / "session" / "sess-1"
        s1.mkdir(parents=True, exist_ok=True)
        meta = {
            "stats": {
                "steps": "11",
                "session_completion_tokens": 500,
                "tool_calls_failed": True,
                "tool_calls_succeeded": False,
            }
        }
        (s1 / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        stats = mistral_runtime.read_native_session_stats(work_dir)
        self.assertIsNotNone(stats)
        self.assertNotIn("steps", stats)
        self.assertNotIn("tool_calls_failed", stats)
        self.assertNotIn("tool_calls_succeeded", stats)
        self.assertEqual(stats["session_completion_tokens"], 500)

        # When no valid numeric field survives, returns None
        meta_all_invalid = {
            "stats": {
                "steps": True,
                "session_completion_tokens": "500",
            }
        }
        (s1 / "meta.json").write_text(json.dumps(meta_all_invalid), encoding="utf-8")
        self.assertIsNone(mistral_runtime.read_native_session_stats(work_dir))

    def test_timestamps_with_unexpected_characters_dropped(self):
        work_dir = self._create_retained_dir()
        s1 = work_dir / "mistral-workspace" / ".vibe" / "logs" / "session" / "sess-1"
        s1.mkdir(parents=True, exist_ok=True)
        meta = {
            "stats": {
                "steps": 5,
                "start_time": "2026-10-20T12:00:00PM",
                "end_time": "2026-10-20T12:00:00Z",
            }
        }
        (s1 / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

        stats = mistral_runtime.read_native_session_stats(work_dir)
        self.assertIsNotNone(stats)
        self.assertNotIn("start_time", stats)
        self.assertEqual(stats["end_time"], "2026-10-20T12:00:00Z")


class DiagnosisTests(unittest.TestCase):
    def test_mistral_stalled_diagnosis_contains_expected_details(self):
        stats = {
            "steps": 11,
            "session_completion_tokens": 86459,
            "last_turn_seconds": 1889,
            "last_turn_completion_tokens": 86459,
            "tool_calls_succeeded": 10,
            "tool_calls_failed": 8,
        }
        reason = fleet.provider_failure_reason(
            adapter_id="mistral",
            timed_out=True,
            exit_code=124,
            stdout="",
            progress_stalled=True,
            progress_stall_seconds=1200,
            native_session=stats,
            idle_seconds=702,
        )
        self.assertIsNotNone(reason)
        self.assertIn("no native progress for more than 1200 seconds", reason)
        self.assertIn("11 model steps", reason)
        self.assertIn("86459 completion tokens", reason)
        self.assertIn("702 s", reason)

    def test_mistral_stalled_diagnosis_with_alternate_key_names(self):
        stats = {
            "steps": 11,
            "completion_tokens": 86459,
            "last_step_duration_seconds": 1889,
        }
        reason = fleet.provider_failure_reason(
            adapter_id="mistral",
            timed_out=True,
            exit_code=124,
            stdout="",
            progress_stalled=True,
            progress_stall_seconds=1200,
            native_session=stats,
            idle_seconds=702,
        )
        self.assertIsNotNone(reason)
        self.assertIn("no native progress for more than 1200 seconds", reason)
        self.assertIn("11 model steps", reason)
        self.assertIn("86459 completion tokens", reason)
        self.assertIn("702 s", reason)

    def test_mistral_timed_out_without_stall_keeps_parent_timeout_wording(self):
        stats = {
            "steps": 11,
            "session_completion_tokens": 86459,
            "last_turn_seconds": 1889,
            "last_turn_completion_tokens": 86459,
        }
        reason = fleet.provider_failure_reason(
            adapter_id="mistral",
            timed_out=True,
            exit_code=124,
            stdout="",
            progress_stalled=False,
            native_session=stats,
            idle_seconds=702,
        )
        self.assertIsNotNone(reason)
        self.assertIn("hit the parent timeout", reason)
        self.assertNotIn("no native progress for more than", reason)
        self.assertIn("11 model steps", reason)

    def test_exit_code_without_export_appends_session_summary(self):
        stats = {
            "steps": 11,
            "session_completion_tokens": 86459,
            "last_turn_seconds": 1889,
            "last_turn_completion_tokens": 86459,
        }
        reason = fleet.provider_failure_reason(
            adapter_id="mistral",
            timed_out=False,
            exit_code=4294967295,
            stdout="",
            native_export=None,
            native_session=stats,
            idle_seconds=702,
        )
        self.assertIsNotNone(reason)
        self.assertIn("export (mistral-output/export.json) is missing or unreadable", reason)
        self.assertIn("11 model steps", reason)
        self.assertIn("702 s", reason)

    def test_foreign_adapters_are_byte_identical_with_and_without_new_kwargs(self):
        stats = {
            "steps": 11,
            "session_completion_tokens": 86459,
        }
        for adapter in ("codex", "qwen", "grok", "minimax"):
            with self.subTest(adapter=adapter):
                r1 = fleet.provider_failure_reason(
                    adapter_id=adapter, timed_out=True, exit_code=124, stdout=""
                )
                r2 = fleet.provider_failure_reason(
                    adapter_id=adapter,
                    timed_out=True,
                    exit_code=124,
                    stdout="",
                    progress_stalled=True,
                    progress_stall_seconds=1200,
                    native_session=stats,
                    idle_seconds=702,
                )
                self.assertEqual(r1, r2)

                r3 = fleet.provider_failure_reason(
                    adapter_id=adapter, timed_out=False, exit_code=2, stdout=""
                )
                r4 = fleet.provider_failure_reason(
                    adapter_id=adapter,
                    timed_out=False,
                    exit_code=2,
                    stdout="",
                    progress_stalled=True,
                    progress_stall_seconds=1200,
                    native_session=stats,
                    idle_seconds=702,
                )
                self.assertEqual(r3, r4)


class TransportOutcomeAndStatusTests(unittest.TestCase):
    def test_provider_transport_outcome_mistral_stalled(self):
        outcome = fleet.provider_transport_outcome(
            "mistral",
            exit_code=124,
            terminal_reason=None,
            timed_out=True,
            progress_stalled=True,
        )
        self.assertEqual(
            outcome,
            {"status": "partial", "reason": "mistral-native-progress-stall"},
        )

    def test_completed_run_status_mistral_stalled(self):
        status = fleet.completed_run_status(
            124, None, adapter_id="mistral", timed_out=True, progress_stalled=True
        )
        self.assertEqual(status, "PARTIAL")

    def test_control_without_progress_stalled(self):
        outcome = fleet.provider_transport_outcome(
            "mistral",
            exit_code=124,
            terminal_reason=None,
            timed_out=True,
            progress_stalled=False,
        )
        self.assertEqual(
            outcome,
            {"status": "partial", "reason": "parent-timeout"},
        )
        status = fleet.completed_run_status(
            124, None, adapter_id="mistral", timed_out=True, progress_stalled=False
        )
        self.assertEqual(status, "PARTIAL")

    def test_foreign_adapters_transport_outcome(self):
        outcome_codex = fleet.provider_transport_outcome(
            "codex",
            exit_code=124,
            terminal_reason=None,
            timed_out=True,
            progress_stalled=True,
        )
        self.assertEqual(outcome_codex, {"status": "partial", "reason": "parent-timeout"})


if __name__ == "__main__":
    unittest.main()
