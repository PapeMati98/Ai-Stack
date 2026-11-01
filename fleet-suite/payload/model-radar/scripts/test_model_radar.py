from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import model_radar


def audit_text(timestamp: str, *, valid_for_days: int = 7) -> str:
    sources = "\n".join(f"- https://example.com/source-{index}" for index in range(6))
    return f"""---
schema_version: 1
last_verified_at: {timestamp}
valid_for_days: {valid_for_days}
status: reviewed
---

# Model Market Audit

## Current routing snapshot

Snapshot.

## Evidence and uncertainty

Evidence.

## Primary sources

{sources}

## Update log

- Updated.
"""


class ModelRadarTests(unittest.TestCase):
    def inspect(self, text: str, now: str) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audit.md"
            path.write_text(text, encoding="utf-8")
            return model_radar.inspect_audit(
                path,
                now=model_radar.parse_timestamp(now),
            )

    def test_fresh_audit_is_accepted_at_exactly_seven_days(self) -> None:
        result = self.inspect(
            audit_text("2026-07-14T12:00:00Z"),
            "2026-07-21T12:00:00Z",
        )
        self.assertEqual(result["status"], "FRESH")
        self.assertTrue(result["fresh"])

    def test_audit_older_than_seven_days_is_stale(self) -> None:
        result = self.inspect(
            audit_text("2026-07-14T11:59:59Z"),
            "2026-07-21T12:00:00Z",
        )
        self.assertEqual(result["status"], "STALE")
        self.assertTrue(result["refresh_required"])

    def test_declared_shorter_validity_wins(self) -> None:
        result = self.inspect(
            audit_text("2026-07-18T12:00:00Z", valid_for_days=2),
            "2026-07-21T12:00:00Z",
        )
        self.assertEqual(result["status"], "STALE")

    def test_future_timestamp_is_invalid(self) -> None:
        result = self.inspect(
            audit_text("2026-07-22T12:00:00Z"),
            "2026-07-21T12:00:00Z",
        )
        self.assertEqual(result["status"], "INVALID")

    def test_missing_file_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = model_radar.inspect_audit(Path(temporary) / "missing.md")
        self.assertEqual(result["status"], "MISSING")

    def benchmark_document(self, timestamp: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "generated_at": timestamp,
            "valid_for_days": 7,
            "source": {
                "dataset": "lmarena-ai/leaderboard-dataset",
                "dataset_url": "https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset",
                "rows_api": "https://datasets-server.huggingface.co/rows",
                "split": "latest",
            },
            "configs": {"agent": {"status": "fetched", "row_count": 2}},
            "models": {
                "grok@grok-4.5": {
                    "profile": "grok",
                    "model": "grok-4.5",
                    "benchmarks": {
                        "agent": {"status": "matched", "percentile": 90.0}
                    },
                }
            },
            "fetch_warnings": [],
        }

    def inspect_benchmark(self, timestamp: str, now: str) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"
            path.write_text(
                json.dumps(self.benchmark_document(timestamp)), encoding="ascii"
            )
            return model_radar.inspect_benchmark_snapshot(
                path, now=model_radar.parse_timestamp(now)
            )

    def test_fresh_benchmark_is_accepted(self) -> None:
        result = self.inspect_benchmark(
            "2026-07-20T12:00:00Z", "2026-07-21T12:00:00Z"
        )
        self.assertEqual(result["status"], "FRESH")
        self.assertEqual(result["matched_metric_count"], 1)

    def test_stale_benchmark_does_not_change_audit_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audit = root / "audit.md"
            benchmark = root / "snapshot.json"
            audit.write_text(audit_text("2026-07-20T12:00:00Z"), encoding="utf-8")
            benchmark.write_text(
                json.dumps(self.benchmark_document("2026-07-01T12:00:00Z")),
                encoding="ascii",
            )
            result = model_radar.combined_check(
                audit,
                benchmark,
                max_age_days=7,
                now=model_radar.parse_timestamp("2026-07-21T12:00:00Z"),
            )
        self.assertEqual(result["status"], "FRESH")
        self.assertEqual(result["benchmark"]["status"], "STALE")
        self.assertTrue(result["routing_allowed"])

    def test_execution_profile_registry_is_validated(self) -> None:
        profiles = (
            Path(__file__).resolve().parents[1]
            / "references"
            / "profiles-does-not-exist.json"
        )
        missing = model_radar.inspect_execution_profiles(profiles)
        self.assertEqual(missing["status"], "MISSING")
        actual = (
            Path(__file__).resolve().parents[2]
            / "agent-fleet"
            / "references"
            / "profiles.json"
        )
        valid = model_radar.inspect_execution_profiles(actual)
        self.assertEqual(valid["status"], "VALID")
        self.assertGreaterEqual(valid["execution_profile_count"], 6)
        self.assertGreater(valid["lifecycle_counts"]["ACTIVE"], 0)

    def test_local_benchmark_suite_is_machine_validated(self) -> None:
        actual = (
            Path(__file__).resolve().parents[1]
            / "references"
            / "local-benchmark-suite.json"
        )
        result = model_radar.inspect_local_benchmark_suite(actual)
        self.assertEqual(result["status"], "VALID")
        self.assertGreaterEqual(result["case_count"], 6)
        self.assertIn("implementation", result["covered_kinds"])

    def test_state_initialization_is_external_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_root = root / "mutable-state"
            audit_source = root / "audit-source.md"
            benchmark_source = root / "benchmark-source.json"
            audit_source.write_text(
                audit_text("2026-07-20T12:00:00Z"), encoding="utf-8"
            )
            benchmark_source.write_text(
                json.dumps(self.benchmark_document("2026-07-20T12:00:00Z")),
                encoding="ascii",
            )

            initialized = model_radar.initialize_state(
                state_root,
                audit_source=audit_source,
                benchmark_source=benchmark_source,
            )
            audit_target, benchmark_target = model_radar.state_paths(state_root)
            self.assertEqual(initialized["status"], "INITIALIZED")
            self.assertEqual(initialized["writes_performed"], 2)
            self.assertTrue(audit_target.is_file())
            self.assertTrue(benchmark_target.is_file())
            self.assertFalse(audit_target.is_relative_to(model_radar.SKILL_ROOT))

            refreshed = audit_target.read_text(encoding="utf-8") + "\nLocal refresh.\n"
            audit_target.write_text(refreshed, encoding="utf-8")
            current = model_radar.initialize_state(
                state_root,
                audit_source=audit_source,
                benchmark_source=benchmark_source,
            )
            self.assertEqual(current["status"], "CURRENT")
            self.assertEqual(current["writes_performed"], 0)
            self.assertEqual(audit_target.read_text(encoding="utf-8"), refreshed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
