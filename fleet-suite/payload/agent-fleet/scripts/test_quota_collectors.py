from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import adaptive  # noqa: E402
import fleet  # noqa: E402
import quota_collectors  # noqa: E402

PROFILES_PATH = SCRIPT_DIR.parent / "references" / "profiles.json"
EXPECTED_CLAUDE_USAGE_PATH = "%APPDATA%\\Claude\\plan-usage-history.json"


def load_real_profiles() -> dict[str, dict[str, object]]:
    payload = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    return payload["profiles"]


class QuotaCollectorTests(unittest.TestCase):
    def config(self, directory: Path) -> dict[str, object]:
        return {
            "collector_mode": "local-token-ledger",
            "ledger_dir": str(directory),
            "ledger_glob": "token-usage-*.jsonl",
            "window_capacity_tokens": 1000,
            "window_seconds": 604800,
            "window_reset_anchor": "2026-08-12T18:49:00Z",
            "capacity_source": "test-fixture",
            "confidence": 0.7,
            "require_fresh_snapshot": True,
            "minimum_confidence": 0.6,
        }

    def test_local_ledger_rolls_window_and_deduplicates_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            rows = [
                {
                    "id": "old",
                    "timestamp": "2026-08-12T18:48:59Z",
                    "totalTokens": 900,
                },
                {
                    "id": "first",
                    "timestamp": "2026-08-12T18:49:00Z",
                    "totalTokens": 100,
                },
                {
                    "id": "second",
                    "timestamp": "2026-08-17T10:00:00Z",
                    "totalTokens": 200,
                },
                {
                    "id": "second",
                    "timestamp": "2026-08-17T10:00:00Z",
                    "totalTokens": 200,
                },
            ]
            content = "\n".join(json.dumps(row) for row in rows) + "\nnot-json\n"
            (directory / "token-usage-test.jsonl").write_text(
                content, encoding="utf-8"
            )
            result = quota_collectors.collect_local_token_ledger(
                self.config(directory),
                pool_id="bailian",
                now=dt.datetime(2026, 8, 17, 12, tzinfo=dt.timezone.utc),
            )
        self.assertEqual(result["used_tokens"], 300)
        self.assertIsNone(result["remaining_tokens"])
        self.assertIsNone(result["headroom"])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["estimated_remaining_tokens"], 700)
        self.assertEqual(result["estimated_weekly_remaining_fraction"], 0.7)
        self.assertEqual(result["duplicate_records"], 1)
        self.assertEqual(result["invalid_records"], 1)
        self.assertEqual(result["window_start_at"], "2026-08-12T18:49:00Z")
        self.assertIsNone(result["weekly_reset_at"])
        self.assertEqual(result["estimated_weekly_reset_at"], "2026-08-19T18:49:00Z")

    def test_one_collector_supplies_every_profile_in_shared_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "token-usage-test.jsonl").write_text(
                json.dumps(
                    {
                        "id": "one",
                        "timestamp": "2026-08-17T10:00:00Z",
                        "totalTokens": 250,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            profiles = {
                "qwen": {
                    "quota_pool_id": "bailian",
                    "quota_collection": self.config(directory),
                },
                "qwen-review": {"quota_pool_id": "bailian"},
            }
            rows, diagnostics = quota_collectors.collect_profile_quotas(
                profiles,
                now=dt.datetime(2026, 8, 17, 12, tzinfo=dt.timezone.utc),
            )
        self.assertEqual(set(rows), {"qwen", "qwen-review"})
        self.assertEqual(rows["qwen"]["snapshot_id"], rows["qwen-review"]["snapshot_id"])
        self.assertEqual(rows["qwen-review"]["effective_agent"], "qwen-review")
        self.assertEqual(diagnostics["bailian"]["status"], "UNKNOWN")
        for row in rows.values():
            self.assertIsNone(row["headroom"])
            self.assertEqual(row["estimated_remaining_tokens"], 750)

    def test_unavailable_ledger_is_reported_without_crashing_other_routes(self) -> None:
        profiles = {
            "qwen": {
                "quota_pool_id": "bailian",
                "quota_collection": self.config(Path(tempfile.mkdtemp(prefix='missing-ledger-fixture-')) / 'absent'),
            }
        }
        rows, diagnostics = quota_collectors.collect_profile_quotas(profiles)
        self.assertEqual(rows, {})
        self.assertEqual(diagnostics["bailian"]["status"], "ERROR")
        self.assertIn("unavailable", diagnostics["bailian"]["error"])

    def test_local_unmetered_profile_is_fresh_and_zero_provider_cost(self) -> None:
        profiles = {
            "qwen-local": {
                "quota_pool_id": "local-qwen-zero-provider-cost",
                "quota_collection": {
                    "collector_mode": "local-unmetered",
                    "confidence": 1.0,
                },
            }
        }
        rows, diagnostics = quota_collectors.collect_profile_quotas(
            profiles,
            now=dt.datetime(2026, 8, 19, 12, tzinfo=dt.timezone.utc),
        )
        row = rows["qwen-local"]
        self.assertEqual(row["status"], "fresh")
        self.assertEqual(row["headroom"], 1.0)
        self.assertEqual(row["provider_cost_pln"], 0.0)
        self.assertEqual(row["provider_cost_display"], "0 zł")
        self.assertEqual(
            diagnostics["local-qwen-zero-provider-cost"]["status"], "FRESH"
        )


class ClaudeNativeQuotaRegressionTests(unittest.TestCase):
    """Regression for the 0.8.43 Claude native quota activation.

    Loads the real profiles.json and drives the live collector plus the
    persisted-snapshot merge with a synthetic usage-history cache, so no
    host Claude state is ever read.
    """

    def profiles_with_temp_history(
        self, history_path: Path
    ) -> dict[str, dict[str, object]]:
        profiles = load_real_profiles()
        claude = copy.deepcopy(profiles["claude"])
        claude["quota_collection"]["usage_history_path"] = str(history_path)
        profiles["claude"] = claude
        # The Codex rollout collector must never read the host's real
        # ~/.codex sessions from a test; point it at a missing fixture path.
        codex = copy.deepcopy(profiles["codex"])
        codex["quota_collection"]["sessions_dir"] = str(
            history_path.parent / "missing-codex-sessions"
        )
        profiles["codex"] = codex
        return profiles

    def write_history(self, path: Path, samples: list[dict[str, object]]) -> None:
        path.write_text(json.dumps({"samples": samples}), encoding="utf-8")

    def write_stale_persisted_snapshot(self, root: Path) -> None:
        observed = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
        adaptive.record_quota(
            root,
            agent="claude",
            short_remaining_fraction=0.4,
            weekly_remaining_fraction=0.4,
            credits_remaining=None,
            short_reset_at=None,
            weekly_reset_at=None,
            observed_at=observed.isoformat().replace("+00:00", "Z"),
            source="provider-dashboard",
            confidence=0.95,
            note="stale manual quota record",
            valid_for_hours=1,
        )

    def test_real_profile_selects_claude_native_with_bounded_settings(self) -> None:
        profiles = load_real_profiles()
        config = profiles["claude"]["quota_collection"]
        self.assertEqual(config["collector_mode"], "claude-native-usage")
        self.assertEqual(config["usage_history_path"], EXPECTED_CLAUDE_USAGE_PATH)
        self.assertEqual(config["max_bytes"], 8_388_608)
        self.assertLessEqual(int(config["max_bytes"]), 8 * 1024 * 1024)
        self.assertEqual(config["max_sample_age_seconds"], 3600)
        self.assertLessEqual(int(config["max_sample_age_seconds"]), 3600)
        self.assertIn(
            "claude-native-usage",
            quota_collectors.MACHINE_READABLE_COLLECTOR_MODES,
        )
        self.assertIsNone(
            quota_collectors.collector_mode_blocker("claude-native-usage")
        )

    def test_kimi_and_codex_do_not_use_claude_native_collector(self) -> None:
        profiles = load_real_profiles()
        for agent in ("kimi", "codex"):
            config = profiles[agent]["quota_collection"]
            self.assertNotEqual(config["collector_mode"], "claude-native-usage")
            self.assertNotIn("usage_history_path", config)
        kimi_blocker = quota_collectors.collector_mode_blocker(
            str(profiles["kimi"]["quota_collection"]["collector_mode"])
        )
        self.assertIsNotNone(kimi_blocker)
        self.assertTrue(str(kimi_blocker).startswith("tui-only:"))
        codex_config = profiles["codex"]["quota_collection"]
        self.assertEqual(
            codex_config["collector_mode"], "codex-rollout-rate-limits"
        )
        self.assertIsNone(
            quota_collectors.collector_mode_blocker("codex-rollout-rate-limits")
        )
        sandboxed = load_real_profiles()
        codex = copy.deepcopy(sandboxed["codex"])
        codex["quota_collection"]["sessions_dir"] = str(Path(tempfile.mkdtemp(prefix='missing-codex-fixture-')) / 'absent')
        sandboxed["codex"] = codex
        rows, diagnostics = quota_collectors.collect_profile_quotas(
            sandboxed, agent_ids=["kimi", "codex"]
        )
        self.assertNotIn("kimi", rows)
        self.assertEqual(diagnostics["kimi"]["status"], "NOT_MACHINE_READABLE")
        self.assertIn("tui-only", diagnostics["kimi"]["blocker"])
        self.assertEqual(rows["codex"]["status"], "unknown")
        self.assertEqual(rows["codex"]["reason"], "sessions-dir-unavailable")
        self.assertIsNone(rows["codex"]["headroom"])
        self.assertEqual(diagnostics["codex"]["status"], "UNKNOWN")
        self.assertIn("sessions-dir-unavailable", diagnostics["codex"]["blocker"])

    def test_fresh_native_sample_overrides_stale_persisted_snapshot(self) -> None:
        sample_time = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=120)
        sample_ms = int(sample_time.timestamp() * 1000)
        expected_observed = quota_collectors._utc_timestamp(
            dt.datetime.fromtimestamp(sample_ms / 1000.0, tz=dt.timezone.utc)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            history = Path(temporary) / "plan-usage-history.json"
            self.write_history(
                history,
                [
                    {"t": sample_ms - 600_000, "u": {"fh": 80.0, "sd": 90.0}},
                    {"t": sample_ms, "u": {"fh": 12.5, "sd": 30.0}},
                    {"t": -1, "u": {"fh": 1.0, "sd": 1.0}},
                ],
            )
            self.write_stale_persisted_snapshot(root)
            views, diagnostics = fleet.current_quota_views(
                root,
                self.profiles_with_temp_history(history),
                agent_ids=["claude"],
            )
        view = views["claude"]
        self.assertEqual(view["status"], "fresh")
        self.assertEqual(view["observed_at"], expected_observed)
        self.assertAlmostEqual(view["age_hours"], 120 / 3600.0, delta=0.01)
        self.assertLessEqual(view["age_hours"], 3600 / 3600.0)
        self.assertIsNotNone(view["headroom"])
        self.assertEqual(view["headroom"], 0.7)
        self.assertIs(view["cache_read"], True)
        self.assertIs(view["current"], True)
        self.assertIsNone(view["blocker"])
        self.assertEqual(diagnostics["claude"]["status"], "FRESH")
        self.assertEqual(diagnostics["claude"]["selected_source"], "native-json")

    def test_sample_older_than_ceiling_stays_stale_with_precise_blocker(self) -> None:
        sample_time = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=7200)
        sample_ms = int(sample_time.timestamp() * 1000)
        expected_observed = quota_collectors._utc_timestamp(
            dt.datetime.fromtimestamp(sample_ms / 1000.0, tz=dt.timezone.utc)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            history = Path(temporary) / "plan-usage-history.json"
            self.write_history(
                history,
                [{"t": sample_ms, "u": {"fh": 12.5, "sd": 30.0}}],
            )
            self.write_stale_persisted_snapshot(root)
            views, diagnostics = fleet.current_quota_views(
                root,
                self.profiles_with_temp_history(history),
                agent_ids=["claude"],
            )
        view = views["claude"]
        self.assertEqual(view["status"], "stale")
        self.assertEqual(view["observed_at"], expected_observed)
        self.assertIsNone(view["headroom"])
        self.assertEqual(view["reported_headroom"], 0.7)
        self.assertGreaterEqual(view["age_hours"], 2.0)
        self.assertIs(view["cache_read"], True)
        self.assertIs(view["current"], False)
        self.assertTrue(
            str(view["blocker"]).startswith(
                f"stale: latest native-json snapshot observed_at={expected_observed}"
            )
        )
        self.assertIn("exceeded its validity", str(view["blocker"]))
        pool = diagnostics["claude"]
        self.assertEqual(pool["status"], "STALE")
        self.assertTrue(
            str(pool["blocker"]).startswith(
                "stale-cache: newest cached sample "
                f"observed_at={expected_observed}"
            )
        )
        self.assertIn("age_hours=", str(pool["blocker"]))
        self.assertIn("max_sample_age_seconds=3600", str(pool["blocker"]))

    def test_claude_cache_never_projects_tokens_onto_codex_or_budget(self) -> None:
        sample_time = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)
        sample_ms = int(sample_time.timestamp() * 1000)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            history = Path(temporary) / "plan-usage-history.json"
            self.write_history(
                history,
                [{"t": sample_ms, "u": {"fh": 90.0, "sd": 95.0}}],
            )
            views, diagnostics = fleet.current_quota_views(
                root,
                self.profiles_with_temp_history(history),
                agent_ids=["claude", "codex"],
            )
        claude = views["claude"]
        self.assertEqual(claude["status"], "fresh")
        self.assertEqual(claude["headroom"], 0.05)
        for key in (
            "used_tokens",
            "remaining_tokens",
            "capacity_tokens",
            "total_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            self.assertIsNone(claude.get(key), key)
        self.assertIsNone(diagnostics["claude"]["used_tokens"])
        self.assertIsNone(diagnostics["claude"]["remaining_tokens"])
        codex = views["codex"]
        self.assertEqual(codex["status"], "unknown")
        self.assertIsNone(codex["headroom"])
        self.assertIsNot(codex.get("cache_read"), True)
        self.assertIs(codex["current"], False)
        self.assertIn("unknown", str(codex["blocker"]))
        # Claude's cached window fractions never surface as Codex headroom.
        self.assertNotEqual(
            codex.get("reported_headroom"), claude["reported_headroom"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
