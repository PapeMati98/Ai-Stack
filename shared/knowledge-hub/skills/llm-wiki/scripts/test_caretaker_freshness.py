import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import knowledge


def make_synthetic_vault(root: Path) -> Path:
    vault = root / "vault"
    for part in ("wiki/projects", "wiki/infra", "proposals", "events", "raw/sources", "governance"):
        (vault / part).mkdir(parents=True, exist_ok=True)
    (vault / "knowledge-vault.json").write_text(
        json.dumps({"curator_machine": "TEST", "curation_scope": "designated-machine-only"}),
        encoding="utf-8",
    )
    return vault


class CaretakerFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp.name)
        self.fake_home = self.temp_path / "fake_home"
        self.fake_home.mkdir(parents=True, exist_ok=True)
        self.home_patcher = mock.patch.object(Path, "home", return_value=self.fake_home)
        self.home_patcher.start()
        self.vault = make_synthetic_vault(self.temp_path)

    def tearDown(self):
        self.home_patcher.stop()
        self.temp.cleanup()

    def test_fresh_with_10_min_old_observed_at(self):
        now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        observed = "2026-10-01T11:50:00Z"
        status_file = self.temp_path / "latest.json"
        status_file.write_text(
            json.dumps({"observed_at": observed, "status": "ok"}),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            result = knowledge.caretaker_freshness(self.vault, now=now)

        self.assertEqual(result["state"], "FRESH")
        self.assertEqual(result["age_minutes"], 10.0)
        self.assertEqual(result["observed_at"], observed)
        self.assertEqual(result["tick_status"], "ok")
        self.assertEqual(result["threshold_minutes"], 60)
        self.assertEqual(result["source_path"], str(status_file.resolve()))

    def test_fresh_with_plus_zero_timezone(self):
        now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        observed = "2026-10-01T11:50:00+00:00"
        status_file = self.temp_path / "latest.json"
        status_file.write_text(
            json.dumps({"observed_at": observed, "status": "healthy"}),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            result = knowledge.caretaker_freshness(self.vault, now=now)

        self.assertEqual(result["state"], "FRESH")
        self.assertEqual(result["age_minutes"], 10.0)
        self.assertEqual(result["observed_at"], observed)
        self.assertEqual(result["tick_status"], "healthy")

    def test_tick_status_truncation(self):
        now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        long_status = "a" * 100
        status_file = self.temp_path / "latest.json"
        status_file.write_text(
            json.dumps({"observed_at": "2026-10-01T11:55:00Z", "status": long_status}),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            result = knowledge.caretaker_freshness(self.vault, now=now)

        self.assertEqual(result["tick_status"], "a" * 40)

    def test_stale_with_2_hour_old_observed_at(self):
        now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        observed = "2026-10-01T10:00:00Z"
        status_file = self.temp_path / "latest.json"
        status_file.write_text(
            json.dumps({"observed_at": observed, "status": "lagging"}),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            result = knowledge.caretaker_freshness(self.vault, now=now)

        self.assertEqual(result["state"], "STALE")
        self.assertEqual(result["age_minutes"], 120.0)
        self.assertEqual(result["observed_at"], observed)
        self.assertEqual(result["tick_status"], "lagging")
        self.assertEqual(result["threshold_minutes"], 60)

    def test_boundary_60_min(self):
        now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        status_file = self.temp_path / "latest.json"

        # 60.0 min is FRESH
        observed_60_0 = "2026-10-01T11:00:00Z"
        status_file.write_text(
            json.dumps({"observed_at": observed_60_0, "status": "boundary_fresh"}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            res_60_0 = knowledge.caretaker_freshness(self.vault, now=now)
        self.assertEqual(res_60_0["state"], "FRESH")
        self.assertEqual(res_60_0["age_minutes"], 60.0)

        # 60.1 min is STALE (60 min 6 sec ago = 3606 seconds)
        observed_60_1 = "2026-10-01T10:59:54Z"
        status_file.write_text(
            json.dumps({"observed_at": observed_60_1, "status": "boundary_stale"}),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            res_60_1 = knowledge.caretaker_freshness(self.vault, now=now)
        self.assertEqual(res_60_1["state"], "STALE")
        self.assertEqual(res_60_1["age_minutes"], 60.1)

    def test_unknown_missing_file(self):
        missing = self.temp_path / "nonexistent.json"
        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(missing)}):
            result = knowledge.caretaker_freshness(self.vault)

        self.assertEqual(result["state"], "UNKNOWN")
        self.assertIsNone(result["age_minutes"])
        self.assertIsNone(result["observed_at"])
        self.assertIsNone(result["tick_status"])
        self.assertIsNone(result["source_path"])
        self.assertEqual(result["threshold_minutes"], 60)

    def test_unknown_empty_file(self):
        status_file = self.temp_path / "latest.json"
        status_file.write_bytes(b"")

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            result = knowledge.caretaker_freshness(self.vault)

        self.assertEqual(result["state"], "UNKNOWN")
        self.assertIsNone(result["age_minutes"])
        self.assertIsNone(result["observed_at"])
        self.assertIsNone(result["tick_status"])
        self.assertEqual(result["source_path"], str(status_file.resolve()))

    def test_unknown_malformed_json(self):
        status_file = self.temp_path / "latest.json"
        status_file.write_text("{\"not\": valid json", encoding="utf-8")

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            result = knowledge.caretaker_freshness(self.vault)

        self.assertEqual(result["state"], "UNKNOWN")
        self.assertIsNone(result["age_minutes"])
        self.assertIsNone(result["observed_at"])
        self.assertIsNone(result["tick_status"])
        self.assertEqual(result["source_path"], str(status_file.resolve()))

    def test_unknown_non_object_json(self):
        status_file = self.temp_path / "latest.json"
        for non_obj in (json.dumps(["item1", "item2"]), json.dumps("string"), json.dumps(12345)):
            status_file.write_text(non_obj, encoding="utf-8")
            with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
                result = knowledge.caretaker_freshness(self.vault)

            self.assertEqual(result["state"], "UNKNOWN")
            self.assertIsNone(result["age_minutes"])
            self.assertIsNone(result["observed_at"])
            self.assertIsNone(result["tick_status"])

    def test_unknown_naive_timestamp(self):
        now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        status_file = self.temp_path / "latest.json"
        # Naive timestamp lacks UTC offset / Z indicator
        status_file.write_text(
            json.dumps({"observed_at": "2026-10-01T11:50:00", "status": "ok"}),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            result = knowledge.caretaker_freshness(self.vault, now=now)

        self.assertEqual(result["state"], "UNKNOWN")
        self.assertIsNone(result["age_minutes"])
        self.assertIsNone(result["observed_at"])

    def test_unknown_future_timestamp_more_than_5_minutes(self):
        now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        status_file = self.temp_path / "latest.json"
        # 6 minutes in the future (> 5 min)
        observed = "2026-10-01T12:06:00Z"
        status_file.write_text(
            json.dumps({"observed_at": observed, "status": "from_future"}),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            result = knowledge.caretaker_freshness(self.vault, now=now)

        self.assertEqual(result["state"], "UNKNOWN")
        self.assertIsNone(result["age_minutes"])
        self.assertIsNone(result["observed_at"])

    def test_future_timestamp_within_5_minutes_is_fresh(self):
        now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        status_file = self.temp_path / "latest.json"
        # 1 minute in future (tolerated clock skew <= 5 min)
        observed = "2026-10-01T12:01:00Z"
        status_file.write_text(
            json.dumps({"observed_at": observed, "status": "skewed"}),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            result = knowledge.caretaker_freshness(self.vault, now=now)

        self.assertEqual(result["state"], "FRESH")
        self.assertEqual(result["age_minutes"], 0.0)
        self.assertEqual(result["observed_at"], observed)

    def test_unknown_oversized_file(self):
        status_file = self.temp_path / "latest.json"
        # > 256 KB
        padded_payload = {"observed_at": "2026-10-01T11:50:00Z", "padding": "x" * (260 * 1024)}
        status_file.write_text(json.dumps(padded_payload), encoding="utf-8")

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            result = knowledge.caretaker_freshness(self.vault)

        self.assertEqual(result["state"], "UNKNOWN")
        self.assertIsNone(result["age_minutes"])
        self.assertIsNone(result["observed_at"])

    def test_mtime_fallback_when_observed_at_is_absent(self):
        now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        status_file = self.temp_path / "latest.json"
        status_file.write_text(json.dumps({"status": "fallback_test"}), encoding="utf-8")

        # Set mtime to 10 minutes before now
        mtime_10 = now.timestamp() - 600
        os.utime(status_file, (mtime_10, mtime_10))

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            res_fresh = knowledge.caretaker_freshness(self.vault, now=now)

        self.assertEqual(res_fresh["state"], "FRESH")
        self.assertEqual(res_fresh["age_minutes"], 10.0)
        self.assertIsNotNone(res_fresh["observed_at"])
        self.assertEqual(res_fresh["tick_status"], "fallback_test")

        # Set mtime to 2 hours before now
        mtime_120 = now.timestamp() - 7200
        os.utime(status_file, (mtime_120, mtime_120))

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            res_stale = knowledge.caretaker_freshness(self.vault, now=now)

        self.assertEqual(res_stale["state"], "STALE")
        self.assertEqual(res_stale["age_minutes"], 120.0)

    def test_path_precedence_env_beats_config_beats_default(self):
        # Build vault hierarchy with >= 4 parents so default path can exist
        # default path: resolved_vault.parents[3] / "openclaw-data" / "hub-caretaker" / "latest.json"
        vault_root = self.temp_path / "sub1" / "sub2" / "sub3" / "sub4"
        vault = make_synthetic_vault(vault_root)

        claw_dir = self.temp_path / "sub1" / "openclaw-data" / "hub-caretaker"
        claw_dir.mkdir(parents=True, exist_ok=True)
        default_file = claw_dir / "latest.json"
        default_file.write_text(
            json.dumps({"observed_at": "2026-10-01T11:55:00Z", "status": "default_tick"}),
            encoding="utf-8",
        )

        config_dir = self.temp_path / "config_state"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "latest.json"
        config_file.write_text(
            json.dumps({"observed_at": "2026-10-01T11:50:00Z", "status": "config_tick"}),
            encoding="utf-8",
        )

        env_dir = self.temp_path / "env_state"
        env_dir.mkdir(parents=True, exist_ok=True)
        env_file = env_dir / "latest.json"
        env_file.write_text(
            json.dumps({"observed_at": "2026-10-01T11:45:00Z", "status": "env_tick"}),
            encoding="utf-8",
        )

        # Mock Path.home() so ~/.agents/knowledge-config.json points to tempdir
        fake_home = self.temp_path / "fake_home"
        agents_dir = fake_home / ".agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "knowledge-config.json").write_text(
            json.dumps({"caretaker_status_path": str(config_file)}),
            encoding="utf-8",
        )

        with mock.patch.object(Path, "home", return_value=fake_home):
            # 1. Env var beats config key and default
            with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(env_file)}):
                res1 = knowledge.caretaker_freshness(vault)
                self.assertEqual(res1["tick_status"], "env_tick")
                self.assertEqual(res1["source_path"], str(env_file.resolve()))

            # 2. Non-existent env var falls through to config key
            non_existent_env = self.temp_path / "non_existent_env.json"
            with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(non_existent_env)}):
                res_env_missing = knowledge.caretaker_freshness(vault)
                self.assertEqual(res_env_missing["tick_status"], "config_tick")
                self.assertEqual(res_env_missing["source_path"], str(config_file.resolve()))

            # 3. Config key beats default when env var is unset
            with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": ""}):
                res2 = knowledge.caretaker_freshness(vault)
                self.assertEqual(res2["tick_status"], "config_tick")
                self.assertEqual(res2["source_path"], str(config_file.resolve()))

            # 4. Non-existent env var and non-existent config key fall through to default
            (agents_dir / "knowledge-config.json").write_text(
                json.dumps({"caretaker_status_path": str(self.temp_path / "non_existent_config.json")}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(non_existent_env)}):
                res_both_missing = knowledge.caretaker_freshness(vault)
                self.assertEqual(res_both_missing["tick_status"], "default_tick")
                self.assertEqual(res_both_missing["source_path"], str(default_file.resolve()))

            # 5. Default wins when env var and config key are unset
            (agents_dir / "knowledge-config.json").write_text(
                json.dumps({}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": ""}):
                res3 = knowledge.caretaker_freshness(vault)
                self.assertEqual(res3["tick_status"], "default_tick")
                self.assertEqual(res3["source_path"], str(default_file.resolve()))

    def test_default_path_guarded_against_fewer_than_5_parents(self):
        # Vault with shallow path (fewer than 5 parents)
        shallow_dir = Path("C:/") / "shallow_test" if os.name == "nt" else Path("/shallow_test")
        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": ""}):
            # Ensure resolve_caretaker_status_path does not raise IndexError
            candidate = knowledge.resolve_caretaker_status_path(shallow_dir)
            self.assertIsNone(candidate)

    def test_status_includes_caretaker_freshness(self):
        status_file = self.temp_path / "latest.json"
        status_file.write_text(
            json.dumps({"observed_at": "2026-10-01T11:50:00Z", "status": "status_check"}),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            status_result = knowledge.status(self.vault)

        self.assertIn("caretaker_freshness", status_result)
        cf = status_result["caretaker_freshness"]
        self.assertIsInstance(cf, dict)
        self.assertEqual(cf["tick_status"], "status_check")

        # Verify all expected existing status keys remain present
        expected_keys = {
            "action", "gateway_mode", "vault_path", "sources", "proposals", "wiki_pages",
            "events", "curator_lock", "curator_lock_owner", "caretaker_freshness",
            "curator_machine", "curation_allowed", "machine", "git_root", "git_change_count",
            "git_changed_file_count", "dirty_tree_state", "dirty_tree_oldest_age_hours",
            "dirty_tree_age_unknown_count", "dirty_tree_write_blocked", "dirty_tree_reason",
            "dirty_tree_policy", "curation_health",
        }
        self.assertTrue(expected_keys.issubset(set(status_result.keys())))

    def test_lint_emits_one_warning_when_stale_and_none_when_fresh_or_unknown(self):
        now = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        status_file = self.temp_path / "latest.json"

        # 1. STALE: emits exactly one warning
        observed_stale = "2026-10-01T10:00:00Z"
        status_file.write_text(
            json.dumps({"observed_at": observed_stale, "status": "stale_tick"}),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            with mock.patch.object(knowledge, "now_utc", return_value=now):
                lint_stale = knowledge.lint(self.vault)

        self.assertEqual(lint_stale["error_count"], 0)
        caretaker_warnings = [w for w in lint_stale["warnings"] if "Caretaker is stale" in w]
        self.assertEqual(len(caretaker_warnings), 1)
        expected_warning = (
            f"Caretaker is stale: last tick {observed_stale} (120.0 min ago), "
            "expected a tick at least every 60 min"
        )
        self.assertEqual(caretaker_warnings[0], expected_warning)

        # 2. FRESH: emits no caretaker warning
        observed_fresh = "2026-10-01T11:50:00Z"
        status_file.write_text(
            json.dumps({"observed_at": observed_fresh, "status": "fresh_tick"}),
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(status_file)}):
            with mock.patch.object(knowledge, "now_utc", return_value=now):
                lint_fresh = knowledge.lint(self.vault)

        caretaker_warnings_fresh = [w for w in lint_fresh["warnings"] if "Caretaker is stale" in w]
        self.assertEqual(len(caretaker_warnings_fresh), 0)
        self.assertEqual(lint_fresh["error_count"], 0)

        # 3. UNKNOWN: emits no caretaker warning
        with mock.patch.dict(os.environ, {"KNOWLEDGE_CARETAKER_STATUS_PATH": str(self.temp_path / "nonexistent.json")}):
            lint_unknown = knowledge.lint(self.vault)

        caretaker_warnings_unknown = [w for w in lint_unknown["warnings"] if "Caretaker is stale" in w]
        self.assertEqual(len(caretaker_warnings_unknown), 0)
        self.assertEqual(lint_unknown["error_count"], 0)


if __name__ == "__main__":
    unittest.main()
