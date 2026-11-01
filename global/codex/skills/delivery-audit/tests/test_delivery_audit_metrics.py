"""Unit tests for delivery_audit_metrics.py."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure delivery_audit_metrics can be imported regardless of execution working directory
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import delivery_audit_metrics
from delivery_audit_metrics import (
    BENCHMARK_PROFILES,
    CEREMONY_ALERT_THRESHOLD,
    DeliveryMetrics,
    compute_ceremony_ratio,
    compute_delivery_metrics,
    format_benchmark_table,
    is_high_ceremony,
    load_and_compute_metrics,
    load_state,
    load_telemetry,
    main,
    run_benchmark,
)


class TestDeliveryAuditMetricsFormulas(unittest.TestCase):
    """Test core math and threshold predicates."""

    def test_compute_ceremony_ratio_standard(self) -> None:
        self.assertEqual(compute_ceremony_ratio(10.0, 100.0), 0.1000)
        self.assertEqual(compute_ceremony_ratio(25.0, 100.0), 0.2500)
        self.assertEqual(compute_ceremony_ratio(1.0, 3.0), 0.3333)

    def test_compute_ceremony_ratio_zero_and_sub_second_clamp(self) -> None:
        # denominator clamped to max(lead_time_s, 1.0)
        self.assertEqual(compute_ceremony_ratio(0.5, 0.2), 0.5000)
        self.assertEqual(compute_ceremony_ratio(0.0, 0.0), 0.0000)
        self.assertEqual(compute_ceremony_ratio(0.2, 0.0), 0.2000)

    def test_is_high_ceremony_threshold(self) -> None:
        self.assertFalse(is_high_ceremony(0.25))
        self.assertFalse(is_high_ceremony(0.2499))
        self.assertFalse(is_high_ceremony(0.0))
        self.assertTrue(is_high_ceremony(0.2501))
        self.assertTrue(is_high_ceremony(0.40))


class TestComputeDeliveryMetricsFromState(unittest.TestCase):
    """Test passive parsing and computation from state.json structures."""

    def test_direct_metric_keys(self) -> None:
        state = {
            "lead_time_s": 120.0,
            "model_exec_s": 70.0,
            "review_s": 20.0,
            "check_s": 10.0,
            "ceremony_s": 20.0,
        }
        metrics = compute_delivery_metrics(state=state, profile="test-direct")
        self.assertEqual(metrics.lead_time_s, 120.0)
        self.assertEqual(metrics.model_exec_s, 70.0)
        self.assertEqual(metrics.review_s, 20.0)
        self.assertEqual(metrics.check_s, 10.0)
        self.assertEqual(metrics.ceremony_s, 20.0)
        self.assertEqual(metrics.ceremony_ratio, round(20.0 / 120.0, 4))
        self.assertFalse(metrics.high_ceremony_alert)
        self.assertEqual(metrics.profile, "test-direct")

    def test_tasks_list_aggregation(self) -> None:
        state = {
            "wall_seconds": 100.0,
            "tasks": [
                {
                    "id": "node-1",
                    "maker": {"duration_s": 30.0},
                    "reviewer": {"duration_s": 10.0},
                    "checks": [{"duration_s": 5.0}],
                },
                {
                    "id": "node-2",
                    "maker": {"duration_s": 20.0},
                    "reviewer": {"duration_s": 10.0},
                    "checks": [{"duration_s": 5.0}],
                },
            ],
        }
        metrics = compute_delivery_metrics(state=state)
        self.assertEqual(metrics.lead_time_s, 100.0)
        self.assertEqual(metrics.model_exec_s, 50.0)
        self.assertEqual(metrics.review_s, 20.0)
        self.assertEqual(metrics.check_s, 10.0)
        # unallocated time: 100 - (50 + 20 + 10) = 20.0
        self.assertEqual(metrics.ceremony_s, 20.0)
        self.assertEqual(metrics.ceremony_ratio, 0.2000)
        self.assertFalse(metrics.high_ceremony_alert)

    def test_tasks_dict_aggregation(self) -> None:
        state = {
            "wall_seconds": 80.0,
            "tasks": {
                "t1": {
                    "maker": {"duration_s": 30.0},
                    "reviewer": {"duration_s": 15.0},
                    "checks": [{"duration_s": 5.0}],
                }
            },
        }
        metrics = compute_delivery_metrics(state=state)
        self.assertEqual(metrics.lead_time_s, 80.0)
        self.assertEqual(metrics.model_exec_s, 30.0)
        self.assertEqual(metrics.review_s, 15.0)
        self.assertEqual(metrics.check_s, 5.0)
        # ceremony: 80 - 50 = 30 -> ratio 30/80 = 0.375
        self.assertEqual(metrics.ceremony_s, 30.0)
        self.assertEqual(metrics.ceremony_ratio, 0.3750)
        self.assertTrue(metrics.high_ceremony_alert)

    def test_state_timestamps(self) -> None:
        state = {
            "started_at": 1000.0,
            "completed_at": 1100.0,
            "model_exec_s": 60.0,
            "review_s": 20.0,
            "check_s": 5.0,
        }
        metrics = compute_delivery_metrics(state=state)
        self.assertEqual(metrics.lead_time_s, 100.0)
        self.assertEqual(metrics.ceremony_s, 15.0)
        self.assertEqual(metrics.ceremony_ratio, 0.1500)
        self.assertFalse(metrics.high_ceremony_alert)


class TestComputeDeliveryMetricsFromTelemetry(unittest.TestCase):
    """Test passive parsing from telemetry event streams."""

    def test_telemetry_event_stream(self) -> None:
        telemetry = [
            {"event": "task_start", "timestamp": 100.0},
            {"role": "maker", "duration_s": 40.0, "timestamp": 110.0},
            {"role": "reviewer", "duration_s": 15.0, "timestamp": 150.0},
            {"event": "test", "duration_s": 5.0, "timestamp": 165.0},
            {"category": "ceremony", "duration_s": 10.0, "timestamp": 170.0},
            {"event": "task_end", "timestamp": 200.0},
        ]
        metrics = compute_delivery_metrics(telemetry=telemetry)
        # lead time is max_ts - min_ts = 200 - 100 = 100.0
        self.assertEqual(metrics.lead_time_s, 100.0)
        self.assertEqual(metrics.model_exec_s, 40.0)
        self.assertEqual(metrics.review_s, 15.0)
        self.assertEqual(metrics.check_s, 5.0)
        self.assertEqual(metrics.ceremony_s, 10.0)
        self.assertEqual(metrics.ceremony_ratio, 0.1000)
        self.assertFalse(metrics.high_ceremony_alert)

    def test_telemetry_high_ceremony_detection(self) -> None:
        telemetry = [
            {"role": "maker", "duration_s": 20.0},
            {"role": "reviewer", "duration_s": 10.0},
            {"event": "check", "duration_s": 5.0},
            {"category": "overhead", "duration_s": 35.0},
        ]
        metrics = compute_delivery_metrics(telemetry=telemetry, override_lead_time_s=70.0)
        self.assertEqual(metrics.lead_time_s, 70.0)
        self.assertEqual(metrics.ceremony_s, 35.0)
        self.assertEqual(metrics.ceremony_ratio, 0.5000)
        self.assertTrue(metrics.high_ceremony_alert)


class TestLoadAndComputeMetricsPassive(unittest.TestCase):
    """Verify loading from files without creating any new files."""

    def test_passive_read_and_no_new_files_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            state_file = tmp_path / "state.json"
            telemetry_file = tmp_path / "telemetry.jsonl"

            state_file.write_text(
                json.dumps({
                    "lead_time_s": 50.0,
                    "model_exec_s": 35.0,
                    "review_s": 10.0,
                    "check_s": 2.0,
                    "ceremony_s": 3.0,
                }),
                encoding="utf-8",
            )
            telemetry_file.write_text(
                json.dumps({"phase": "maker", "duration_s": 35.0}) + "\n" +
                json.dumps({"phase": "review", "duration_s": 10.0}) + "\n" +
                json.dumps({"phase": "check", "duration_s": 2.0}) + "\n",
                encoding="utf-8",
            )

            files_before = set(tmp_path.iterdir())

            metrics = load_and_compute_metrics(run_dir=tmp_path, profile="run-test")
            self.assertEqual(metrics.lead_time_s, 50.0)
            self.assertEqual(metrics.ceremony_ratio, round(3.0 / 50.0, 4))
            self.assertFalse(metrics.high_ceremony_alert)

            files_after = set(tmp_path.iterdir())
            # ZERO new files must be created
            self.assertEqual(files_before, files_after)

    def test_load_state_missing(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_state("non_existent_state_file_xyz.json")

    def test_load_telemetry_missing(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_telemetry("non_existent_telemetry_file_xyz.jsonl")


class TestBenchmarkSubcommand(unittest.TestCase):
    """Verify S, M, L trace profiles evaluation."""

    def test_benchmark_profiles_defined(self) -> None:
        self.assertIn("S", BENCHMARK_PROFILES)
        self.assertIn("M", BENCHMARK_PROFILES)
        self.assertIn("L", BENCHMARK_PROFILES)

    def test_run_benchmark_all(self) -> None:
        results = run_benchmark(["S", "M", "L"])
        self.assertEqual(set(results.keys()), {"S", "M", "L"})

        s_m = results["S"]
        self.assertEqual(s_m.lead_time_s, 15.0)
        self.assertEqual(s_m.ceremony_s, 0.5)
        self.assertEqual(s_m.ceremony_ratio, 0.0333)
        self.assertFalse(s_m.high_ceremony_alert)

        m_m = results["M"]
        self.assertEqual(m_m.lead_time_s, 85.0)
        self.assertEqual(m_m.ceremony_s, 8.0)
        self.assertEqual(m_m.ceremony_ratio, 0.0941)
        self.assertFalse(m_m.high_ceremony_alert)

        l_m = results["L"]
        self.assertEqual(l_m.lead_time_s, 240.0)
        self.assertEqual(l_m.ceremony_s, 20.0)
        self.assertEqual(l_m.ceremony_ratio, 0.0833)
        self.assertFalse(l_m.high_ceremony_alert)

    def test_run_benchmark_subset_and_invalid(self) -> None:
        single = run_benchmark(["M"])
        self.assertIn("M", single)
        self.assertEqual(len(single), 1)

        with self.assertRaises(KeyError):
            run_benchmark(["NON_EXISTENT"])

    def test_format_benchmark_table(self) -> None:
        results = run_benchmark(["S", "M", "L"])
        table = format_benchmark_table(results)
        self.assertIn("Profile", table)
        self.assertIn("Lead Time (s)", table)
        self.assertIn("Ceremony Ratio", table)
        self.assertIn("PASS", table)

    def test_format_benchmark_table_with_alert(self) -> None:
        custom_metrics = {
            "HIGH_CEREMONY": DeliveryMetrics(
                lead_time_s=100.0,
                model_exec_s=40.0,
                review_s=10.0,
                check_s=5.0,
                ceremony_s=45.0,
                ceremony_ratio=0.4500,
                high_ceremony_alert=True,
                profile="HIGH_CEREMONY",
            )
        }
        table = format_benchmark_table(custom_metrics)
        self.assertIn("ALERT (>0.25)", table)


class TestDeliveryMetricsDataClass(unittest.TestCase):
    """Test DeliveryMetrics serialization."""

    def test_delivery_metrics_to_dict(self) -> None:
        m = DeliveryMetrics(
            lead_time_s=100.0,
            model_exec_s=60.0,
            review_s=20.0,
            check_s=5.0,
            ceremony_s=15.0,
            ceremony_ratio=0.1500,
            high_ceremony_alert=False,
            profile="test",
            details={"sample": 123},
        )
        d = m.to_dict()
        self.assertEqual(d["lead_time_s"], 100.0)
        self.assertEqual(d["model_exec_s"], 60.0)
        self.assertEqual(d["review_s"], 20.0)
        self.assertEqual(d["check_s"], 5.0)
        self.assertEqual(d["ceremony_s"], 15.0)
        self.assertEqual(d["ceremony_ratio"], 0.1500)
        self.assertFalse(d["high_ceremony_alert"])
        self.assertEqual(d["profile"], "test")
        self.assertEqual(d["details"], {"sample": 123})


class TestCLIExecution(unittest.TestCase):
    """Test CLI subcommands."""

    def test_benchmark_cli_json(self) -> None:
        f = io.StringIO()
        with patch("sys.stdout", f):
            code = main(["benchmark", "--json"])
        self.assertEqual(code, 0)
        output = json.loads(f.getvalue())
        self.assertIn("S", output)
        self.assertIn("M", output)
        self.assertIn("L", output)
        self.assertFalse(output["S"]["high_ceremony_alert"])

    def test_benchmark_cli_table(self) -> None:
        f = io.StringIO()
        with patch("sys.stdout", f):
            code = main(["benchmark"])
        self.assertEqual(code, 0)
        output = f.getvalue()
        self.assertIn("Profile", output)
        self.assertIn("S", output)
        self.assertIn("M", output)
        self.assertIn("L", output)
        self.assertIn("PASS", output)

    def test_benchmark_cli_single_profile(self) -> None:
        f = io.StringIO()
        with patch("sys.stdout", f):
            code = main(["benchmark", "--profile", "S", "--json"])
        self.assertEqual(code, 0)
        output = json.loads(f.getvalue())
        self.assertIn("S", output)
        self.assertEqual(len(output), 1)

    def test_compute_cli_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(
                json.dumps({
                    "lead_time_s": 60.0,
                    "model_exec_s": 40.0,
                    "review_s": 10.0,
                    "check_s": 5.0,
                    "ceremony_s": 5.0,
                }),
                encoding="utf-8",
            )
            f = io.StringIO()
            with patch("sys.stdout", f):
                code = main(["compute", "--state", str(state_file), "--json"])
            self.assertEqual(code, 0)
            output = json.loads(f.getvalue())
            self.assertEqual(output["lead_time_s"], 60.0)
            self.assertEqual(output["ceremony_s"], 5.0)
            self.assertEqual(output["ceremony_ratio"], round(5.0 / 60.0, 4))
            self.assertFalse(output["high_ceremony_alert"])

    def test_compute_cli_human_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            state_file.write_text(
                json.dumps({
                    "lead_time_s": 50.0,
                    "model_exec_s": 30.0,
                    "review_s": 10.0,
                    "check_s": 5.0,
                    "ceremony_s": 5.0,
                }),
                encoding="utf-8",
            )
            f = io.StringIO()
            with patch("sys.stdout", f):
                code = main(["compute", "--state", str(state_file)])
            self.assertEqual(code, 0)
            output = f.getvalue()
            self.assertIn("Lead Time (s):   50.00", output)
            self.assertIn("Ceremony Ratio:  0.1000", output)
            self.assertIn("Status:          PASS", output)

    def test_main_no_subcommand(self) -> None:
        f = io.StringIO()
        with patch("sys.stdout", f):
            code = main([])
        self.assertEqual(code, 0)

    def test_benchmark_cli_positional_profile(self) -> None:
        f = io.StringIO()
        with patch("sys.stdout", f):
            code = main(["benchmark", "S", "--json"])
        self.assertEqual(code, 0)
        output = json.loads(f.getvalue())
        self.assertIn("S", output)
        self.assertEqual(len(output), 1)

    def test_benchmark_top_level_flag(self) -> None:
        f = io.StringIO()
        with patch("sys.stdout", f):
            code = main(["--benchmark", "--json"])
        self.assertEqual(code, 0)
        output = json.loads(f.getvalue())
        self.assertIn("S", output)
        self.assertIn("M", output)
        self.assertIn("L", output)

    def test_benchmark_top_level_single_profile(self) -> None:
        f = io.StringIO()
        with patch("sys.stdout", f):
            code = main(["--benchmark", "--profile", "S", "--json"])
        self.assertEqual(code, 0)
        output = json.loads(f.getvalue())
        self.assertIn("S", output)
        self.assertEqual(len(output), 1)

    def test_state_with_metrics_block(self) -> None:
        state = {
            "metrics": {
                "wall_seconds": 90.0,
                "model_exec_s": 50.0,
                "review_s": 20.0,
                "check_s": 5.0,
                "ceremony_s": 15.0,
            }
        }
        metrics = compute_delivery_metrics(state=state)
        self.assertEqual(metrics.lead_time_s, 90.0)
        self.assertEqual(metrics.model_exec_s, 50.0)
        self.assertEqual(metrics.review_s, 20.0)
        self.assertEqual(metrics.check_s, 5.0)
        self.assertEqual(metrics.ceremony_s, 15.0)
        self.assertEqual(metrics.ceremony_ratio, round(15.0 / 90.0, 4))
        self.assertFalse(metrics.high_ceremony_alert)

    def test_telemetry_word_boundary_classification(self) -> None:
        telemetry = [
            {"event": "maker_execution", "duration_s": 25.0},
            {"event": "peer_review", "duration_s": 10.0},
            {"event": "run_checks", "duration_s": 5.0},
            {"event": "checkpoint", "duration_s": 5.0},
        ]
        metrics = compute_delivery_metrics(telemetry=telemetry, override_lead_time_s=50.0)
        self.assertEqual(metrics.model_exec_s, 25.0)
        self.assertEqual(metrics.review_s, 10.0)
        self.assertEqual(metrics.check_s, 5.0)
        self.assertEqual(metrics.ceremony_s, 5.0)
        self.assertEqual(metrics.ceremony_ratio, 0.1000)
        self.assertFalse(metrics.high_ceremony_alert)

    def test_load_and_compute_metrics_separate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sf = tmp_path / "custom_state.json"
            tf = tmp_path / "custom_telemetry.jsonl"
            sf.write_text(json.dumps({"lead_time_s": 40.0}), encoding="utf-8")
            tf.write_text(
                json.dumps({"phase": "maker", "duration_s": 20.0}) + "\n" +
                json.dumps({"phase": "review", "duration_s": 10.0}) + "\n" +
                json.dumps({"phase": "check", "duration_s": 5.0}) + "\n",
                encoding="utf-8",
            )
            metrics = load_and_compute_metrics(state_path=sf, telemetry_path=tf)
            self.assertEqual(metrics.lead_time_s, 40.0)
            self.assertEqual(metrics.model_exec_s, 20.0)
            self.assertEqual(metrics.review_s, 10.0)
            self.assertEqual(metrics.check_s, 5.0)
            self.assertEqual(metrics.ceremony_s, 5.0)
            self.assertEqual(metrics.ceremony_ratio, 0.1250)
            self.assertFalse(metrics.high_ceremony_alert)


if __name__ == "__main__":
    unittest.main()
