import json
import math
import queue
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import run_telemetry as telemetry


class JournalTests(unittest.TestCase):
    def test_parallel_producers_and_report(self):
        with tempfile.TemporaryDirectory() as root:
            journal = telemetry.Journal(root, capacity=4096)
            def produce(n):
                for i in range(100):
                    journal.emit("node_terminal", node=f"n{n}-{i}", status="ACCEPTED")
            workers = [threading.Thread(target=produce, args=(n,)) for n in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertTrue(journal.close()["complete"])
            report = telemetry.report(root)
            self.assertEqual(report["events"], 800)
            self.assertEqual(report["coverage"], "complete")
            self.assertEqual(report["node_attempt_outcomes"]["ACCEPTED"], 800)
            rows = [json.loads(s) for s in journal.path.read_text().splitlines()][:-1]
            self.assertEqual(len({r["sequence"] for r in rows}), 800)

    def test_metadata_allowlist_no_prompts_paths_or_messages(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            j.emit("stage_error", code="FileNotFoundError", stage="tests", prompt="SECRET",
                   reason="Bearer SECRET", path="C:/private/SECRET", argv=["SECRET"], model="not a model SECRET")
            j.close()
            self.assertNotIn("SECRET", j.path.read_text())

    def test_io_failure_does_not_raise_or_wait_for_index(self):
        with tempfile.TemporaryDirectory() as root:
            file = Path(root) / "not-a-directory"
            file.write_text("x")
            j = telemetry.Journal(file)
            j.emit("run_start")
            result = j.close()
            self.assertFalse(result["complete"])
            self.assertGreater(result["errors"], 0)

    def test_slow_disk_queue_is_bounded_and_close_is_bounded(self):
        release = threading.Event()
        with tempfile.TemporaryDirectory() as root:
            with patch.object(telemetry.Journal, "_writer", lambda _: release.wait(2)):
                j = telemetry.Journal(root, capacity=2)
                start = time.monotonic()
                for _ in range(100):
                    j.emit("run_start")
                result = j.close()
                self.assertLess(time.monotonic() - start, 0.8)
                self.assertEqual(result["dropped"], 98)
                self.assertFalse(result["complete"])
                release.set()
                j.thread.join()

    def test_size_cap_and_truncated_tail_are_visible(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root, max_bytes=500)
            for _ in range(20):
                j.emit("run_start")
            self.assertFalse(j.close()["complete"])
            self.assertGreater(telemetry.report(root)["dropped"], 0)
            with j.path.open("ab") as stream:
                stream.write(b'{"schema":1')
            self.assertEqual(telemetry.report(root)["coverage"], "incomplete")

    def test_unknown_usage_and_recovery_do_not_double_count(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            result = {"agent": "fake", "model": "test", "status": "CANDIDATE", "run_id": "run-1", "tokens": {"total_tokens": 10}}
            telemetry.provider_result(j, "a", "maker", result, 1)
            telemetry.provider_result(j, "a", "maker", result, 2)
            telemetry.provider_result(j, "a", "reviewer", {"agent": "fake", "model": "test"}, 2)
            j.close()
            stats = telemetry.report(root)["providers"]["fake/test"]
            # Legacy dedup + total_tokens accounting is preserved.
            self.assertEqual(stats["results"], 2)
            self.assertEqual(stats["unknown_usage"], 1)
            self.assertEqual(stats["reported_total_tokens"], 10)
            # No cache components were reported for either unique row, so each
            # component records the attempt as missing (not a silent zero).
            for comp_key in telemetry.CACHE_COMPONENTS:
                self.assertEqual(stats["components"][comp_key], {"sum": None, "reported": 0, "unknown": 2})
            self.assertIsNone(stats["cache_ratio"])
            self.assertIsNone(stats["cache_input_source"])
            self.assertFalse(stats["cache_components_complete"])

    def test_all_unknown_usage_is_null_not_zero(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            telemetry.provider_result(j, "a", "maker", {"agent": "fake", "model": "test"}, 1)
            j.close()
            self.assertIsNone(telemetry.report(root)["providers"]["fake/test"]["reported_total_tokens"])

    def test_stage_exception_records_class_only_and_preserves_exception(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            with self.assertRaises(FileNotFoundError):
                with telemetry.stage(j, "a", "tests"):
                    raise FileNotFoundError("secret-path")
            j.close()
            self.assertNotIn("secret-path", j.path.read_text())
            self.assertEqual(telemetry.report(root)["errors"], {"tests/FileNotFoundError": 1})

    def test_disabled_has_no_files(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root, enabled=False)
            j.emit("run_start")
            self.assertFalse(j.close()["enabled"])
            self.assertFalse(j.path.exists())

    def test_abrupt_process_exit_is_not_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as root:
            code = "import os,sys,time; from run_telemetry import Journal; j=Journal(sys.argv[1]); j.emit('run_start'); time.sleep(0.2); os._exit(0)"
            subprocess.run([sys.executable, "-B", "-c", code, root], cwd=Path(__file__).parent, check=True)
            result = telemetry.report(root)
            self.assertEqual(result["incomplete_journals"], 1)
            self.assertEqual(result["coverage"], "incomplete")

    def test_relative_run_dir_is_refused_by_root_for(self):
        with self.assertRaises(ValueError) as ctx:
            telemetry.root_for("relative-evidence")
        self.assertIn("absolute", str(ctx.exception).lower())

    def test_absolute_run_dir_hash_is_stable(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            self.assertEqual(telemetry.root_for(path), telemetry.root_for(str(path)))


class CacheAccountingTests(unittest.TestCase):
    """Passive native cache metadata; offline aggregation, no inference, no warmers."""

    @staticmethod
    def _emit(journal, agent, model, *, tokens=None, run_id="r-1", round_number=1, source=None, status="CANDIDATE"):
        usage = dict(tokens or {})
        if source is not None:
            usage["cache_input_source"] = source
        result = {"agent": agent, "model": model, "status": status, "run_id": run_id, "tokens": usage}
        telemetry.provider_result(journal, "a", "maker", result, round_number)

    def test_aggregate_components_and_ratio_for_complete_provider(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            tokens = {"fresh_input": 70, "cache_read": 30, "cache_write": 20,
                      "output_tokens": 50, "cache_ttl_seconds": 300}
            self._emit(j, "codex", "test", tokens=tokens, run_id="r-1", source="codex_inclusive")
            j.close()
            stats = telemetry.report(root)["providers"]["codex/test"]
            self.assertEqual(stats["components"]["fresh_input"], {"sum": 70, "reported": 1, "unknown": 0})
            self.assertEqual(stats["components"]["cache_read"], {"sum": 30, "reported": 1, "unknown": 0})
            self.assertEqual(stats["components"]["cache_write"], {"sum": 20, "reported": 1, "unknown": 0})
            self.assertEqual(stats["components"]["output_tokens"], {"sum": 50, "reported": 1, "unknown": 0})
            self.assertEqual(stats["components"]["cache_ttl_seconds"], {"sum": 300, "reported": 1, "unknown": 0})
            self.assertEqual(stats["cache_input_source"], "codex_inclusive")
            self.assertAlmostEqual(stats["cache_ratio"], 30 / (70 + 30 + 20), places=6)
            self.assertTrue(stats["cache_components_complete"])

    def test_ratio_skipped_when_any_cache_input_component_missing(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            self._emit(j, "codex", "test",
                       tokens={"fresh_input": 70, "cache_read": 30, "cache_write": 20},
                       run_id="r-1", source="codex_inclusive")
            # Second attempt omits fresh_input: per-component unknown counter
            # records the gap; ratio inference is rejected.
            self._emit(j, "codex", "test",
                       tokens={"cache_read": 30, "cache_write": 20},
                       run_id="r-2", source="codex_inclusive")
            j.close()
            stats = telemetry.report(root)["providers"]["codex/test"]
            self.assertEqual(stats["components"]["fresh_input"], {"sum": 70, "reported": 1, "unknown": 1})
            self.assertEqual(stats["components"]["cache_read"], {"sum": 60, "reported": 2, "unknown": 0})
            self.assertIsNone(stats["cache_ratio"])
            self.assertFalse(stats["cache_components_complete"])

    def test_ratio_skipped_when_denominator_is_zero(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            # True zeros are reported as reported=1; only the ratio is suppressed.
            self._emit(j, "codex", "test",
                       tokens={"fresh_input": 0, "cache_read": 0, "cache_write": 0},
                       run_id="r-1", source="codex_inclusive")
            j.close()
            stats = telemetry.report(root)["providers"]["codex/test"]
            self.assertEqual(stats["components"]["fresh_input"]["reported"], 1)
            self.assertEqual(stats["components"]["cache_read"]["reported"], 1)
            self.assertEqual(stats["components"]["cache_write"]["reported"], 1)
            self.assertIsNone(stats["cache_ratio"])
            self.assertFalse(stats["cache_components_complete"])

    def test_ratio_skipped_when_input_source_inconsistent(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            self._emit(j, "codex", "test",
                       tokens={"fresh_input": 70, "cache_read": 30, "cache_write": 20},
                       run_id="r-1", source="codex_inclusive")
            self._emit(j, "codex", "test",
                       tokens={"fresh_input": 70, "cache_read": 30, "cache_write": 20},
                       run_id="r-2", source="claude_exclusive")
            j.close()
            stats = telemetry.report(root)["providers"]["codex/test"]
            self.assertEqual(stats["cache_input_source"], "inconsistent")
            self.assertIsNone(stats["cache_ratio"])
            self.assertFalse(stats["cache_components_complete"])

    def test_legacy_cached_input_alias_maps_to_cache_read(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            self._emit(j, "codex", "test",
                       tokens={"fresh_input": 30, "cached_input": 50, "cache_write": 10},
                       run_id="r-1", source="codex_inclusive")
            j.close()
            stats = telemetry.report(root)["providers"]["codex/test"]
            self.assertEqual(stats["components"]["cache_read"], {"sum": 50, "reported": 1, "unknown": 0})
            self.assertEqual(stats["components"]["fresh_input"], {"sum": 30, "reported": 1, "unknown": 0})
            # cached_input must NOT appear as its own component.
            self.assertNotIn("cached_input", stats["components"])

    def test_duplicate_provider_run_ignored_separate_attempts_counted(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            tokens = {"fresh_input": 70, "cache_read": 30, "cache_write": 20}
            self._emit(j, "codex", "test", tokens=tokens, run_id="r-1", source="codex_inclusive")
            # Same provider_run: alias/retry of the same attempt, ignored.
            self._emit(j, "codex", "test", tokens=tokens, run_id="r-1", source="codex_inclusive")
            # Different provider_run: actually executed retry, counted.
            self._emit(j, "codex", "test", tokens=tokens, run_id="r-2", source="codex_inclusive")
            j.close()
            stats = telemetry.report(root)["providers"]["codex/test"]
            self.assertEqual(stats["results"], 2)
            self.assertEqual(stats["components"]["fresh_input"]["sum"], 140)
            self.assertEqual(stats["components"]["cache_read"]["sum"], 60)

    def test_invalid_cache_values_are_discarded_as_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            tokens = {
                "fresh_input": 100,                     # valid
                "cache_read": -10,                      # negative: invalid
                "cache_write": True,                    # bool: invalid
                "cache_ttl_seconds": float("inf"),      # inf: invalid
                "output_tokens": float("nan"),          # nan: invalid
            }
            self._emit(j, "codex", "test", tokens=tokens, run_id="r-1", source="codex_inclusive")
            j.close()
            stats = telemetry.report(root)["providers"]["codex/test"]
            self.assertEqual(stats["components"]["fresh_input"], {"sum": 100, "reported": 1, "unknown": 0})
            self.assertEqual(stats["components"]["cache_read"], {"sum": None, "reported": 0, "unknown": 1})
            self.assertEqual(stats["components"]["cache_write"], {"sum": None, "reported": 0, "unknown": 1})
            self.assertEqual(stats["components"]["cache_ttl_seconds"], {"sum": None, "reported": 0, "unknown": 1})
            self.assertEqual(stats["components"]["output_tokens"], {"sum": None, "reported": 0, "unknown": 1})
            self.assertNotIn("True", j.path.read_text())

    def test_missing_ttl_does_not_block_other_components(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            self._emit(j, "codex", "test",
                       tokens={"fresh_input": 70, "cache_read": 30, "cache_write": 20},
                       run_id="r-1", source="codex_inclusive")
            j.close()
            stats = telemetry.report(root)["providers"]["codex/test"]
            self.assertEqual(stats["components"]["cache_ttl_seconds"], {"sum": None, "reported": 0, "unknown": 1})
            self.assertEqual(stats["components"]["fresh_input"]["reported"], 1)
            self.assertIsNotNone(stats["cache_ratio"])
            self.assertTrue(stats["cache_components_complete"])

    def test_uneven_missing_component_coverage_per_provider(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            # Provider A: complete (Codex).
            self._emit(j, "codex", "test-a",
                       tokens={"fresh_input": 100, "cache_read": 50, "cache_write": 25},
                       run_id="r-1", source="codex_inclusive")
            # Provider B: cache_read/cache_write present, fresh_input missing.
            self._emit(j, "codex", "test-b",
                       tokens={"cache_read": 50, "cache_write": 25},
                       run_id="r-2", source="codex_inclusive")
            # Provider C: legacy total_tokens only, no cache components.
            self._emit(j, "codex", "test-c",
                       tokens={"total_tokens": 1000},
                       run_id="r-3", source="codex_inclusive")
            j.close()
            providers = telemetry.report(root)["providers"]
            self.assertIsNotNone(providers["codex/test-a"]["cache_ratio"])
            self.assertIsNone(providers["codex/test-b"]["cache_ratio"])
            self.assertIsNone(providers["codex/test-c"]["cache_ratio"])
            self.assertEqual(providers["codex/test-b"]["components"]["fresh_input"],
                             {"sum": None, "reported": 0, "unknown": 1})
            self.assertEqual(providers["codex/test-c"]["components"]["fresh_input"],
                             {"sum": None, "reported": 0, "unknown": 1})

    def test_cache_ratio_aggregate_only_across_complete_population(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            # Complete provider (Codex).
            self._emit(j, "codex", "test-a",
                       tokens={"fresh_input": 100, "cache_read": 50, "cache_write": 25},
                       run_id="r-1", source="codex_inclusive")
            # Complete provider (Claude) — should also count.
            self._emit(j, "claude", "test-b",
                       tokens={"fresh_input": 100, "cache_read": 50, "cache_write": 25},
                       run_id="r-2", source="claude_exclusive")
            # Incomplete provider: excluded from aggregate.
            self._emit(j, "codex", "test-c",
                       tokens={"cache_read": 999},
                       run_id="r-3", source="codex_inclusive")
            j.close()
            aggregate = telemetry.report(root)["cache_ratio_aggregate"]
            self.assertEqual(aggregate["providers_in_population"], 2)
            self.assertEqual(aggregate["fresh_input_sum"], 200)
            self.assertEqual(aggregate["cache_read_sum"], 100)
            self.assertEqual(aggregate["cache_write_sum"], 50)
            self.assertAlmostEqual(aggregate["cache_ratio"], 100 / (200 + 100 + 50), places=6)
            self.assertIn("cache_ratio_definition", telemetry.report(root))

    def test_invalid_cache_input_source_dropped_and_source_defaults_survive(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            # First row: known Codex source.
            self._emit(j, "codex", "test",
                       tokens={"fresh_input": 70, "cache_read": 30, "cache_write": 20},
                       run_id="r-1", source="codex_inclusive")
            # Second row: free-form text source; must NOT be logged, must NOT
            # flip the per-provider semantic to 'inconsistent'.
            self._emit(j, "codex", "test",
                       tokens={"fresh_input": 70, "cache_read": 30, "cache_write": 20,
                               "cache_input_source": "secret_or_unknown"},
                       run_id="r-2")
            j.close()
            path_text = j.path.read_text()
            self.assertNotIn("secret_or_unknown", path_text)
            self.assertNotIn("SECRET", path_text)
            stats = telemetry.report(root)["providers"]["codex/test"]
            self.assertEqual(stats["cache_input_source"], "codex_inclusive")
            # Both rows had complete components; per-provider ratio is computed.
            self.assertIsNotNone(stats["cache_ratio"])

    def test_cache_ratio_definition_is_documented_and_neutral(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            self._emit(j, "codex", "test",
                       tokens={"fresh_input": 70, "cache_read": 30, "cache_write": 20},
                       run_id="r-1", source="codex_inclusive")
            j.close()
            result = telemetry.report(root)
            definition = result["cache_ratio_definition"]
            self.assertIn("cache_read", definition)
            self.assertIn("fresh_input", definition)
            self.assertIn("cache_write", definition)
            self.assertIn("codex_inclusive", definition)
            self.assertIn("claude_exclusive", definition)
            # Anti-bureaucracy: no price estimate, no active warming.
            self.assertIn("no price estimate", definition)
            self.assertIn("no active warming", definition)
            # TTL contract.
            self.assertIn("cache_ttl_seconds", definition)
            self.assertIn("unknown", definition.lower())


class NativeCarrierRegressionTests(unittest.TestCase):
    def test_actual_normalized_fields_and_inconsistent_input(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            for rid, input_count in (("good", 300), ("bad", 301)):
                telemetry.provider_result(j, "node", "maker", {
                    "agent": "codex", "model": rid, "run_id": rid,
                    "tokens": {"input_tokens": input_count, "fresh_input_tokens": 100,
                               "cache_read_tokens": 200, "cache_write_tokens": 0,
                               "output_tokens": 10, "adapter": "codex"}}, 1)
            j.close()
            providers = telemetry.report(root)["providers"]
            self.assertEqual(providers["codex/good"]["components"]["cache_read"]["sum"], 200)
            self.assertEqual(providers["codex/good"]["cache_ratio"], round(2 / 3, 6))
            self.assertIsNone(providers["codex/bad"]["cache_ratio"])

    def test_native_alias_never_overrides_invalid_primary(self):
        with tempfile.TemporaryDirectory() as root:
            j = telemetry.Journal(root)
            for rid, tokens in (("alias", {"cached_input_tokens": 17}),
                                ("invalid", {"cache_read_tokens": -1, "cached_input_tokens": 17})):
                telemetry.provider_result(j, "node", "maker", {
                    "agent": "legacy", "model": rid, "run_id": rid, "tokens": tokens}, 1)
            j.close()
            providers = telemetry.report(root)["providers"]
            self.assertEqual(providers["legacy/alias"]["components"]["cache_read"]["sum"], 17)
            self.assertEqual(providers["legacy/invalid"]["components"]["cache_read"]["reported"], 0)


if __name__ == "__main__":
    unittest.main()
