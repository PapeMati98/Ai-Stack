from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import adaptive
import fleet


class MonthlyQuotaRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "fleet-state"
        self.reset_at = "2026-10-22T23:59:59Z"
        profiles = {
            "mimo": {"adapter": "mimo", "quota_pool_id": "xiaomi-mimo-token-plan-standard"},
            "legacy": {"adapter": "synthetic", "quota_pool_id": "legacy"},
        }
        self.enterContext(mock.patch.object(fleet, "load_profiles", return_value=profiles))
        self.enterContext(
            mock.patch.object(
                fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})
            )
        )
        self.enterContext(
            mock.patch.object(
                fleet.quota_availability,
                "check",
                side_effect=AssertionError("Schema tests must not probe providers"),
            )
        )

    def run_cli(self, *arguments: str) -> tuple[int, dict]:
        stdout = mock.Mock()
        stdout.buffer = io.BytesIO()
        stderr = mock.Mock()
        stderr.buffer = io.BytesIO()
        with (
            mock.patch.object(fleet.sys, "stdout", stdout),
            mock.patch.object(fleet.sys, "stderr", stderr),
        ):
            try:
                code = fleet.main(["--state-dir", str(self.root), *arguments])
            except SystemExit as exc:
                self.fail(f"Quota CLI parser rejected {arguments!r} (exit {exc.code})")
        output = stdout.buffer.getvalue() or stderr.buffer.getvalue()
        return code, json.loads(output)

    def record(self, *arguments: str) -> tuple[int, dict]:
        return self.run_cli(
            "quota-record", "--agent", "mimo", "--source", "provider-dashboard", *arguments
        )

    def test_quota_record_persists_monthly_fields(self) -> None:
        for percentage in (75.10545985454546, 0.0, 100.0):
            code, result = self.record(
                "--monthly-remaining-pct", str(percentage),
                "--monthly-reset-at", self.reset_at,
            )
            self.assertEqual(code, fleet.EXIT_SUCCESS, result)
            self.assertEqual(result["status"], "RECORDED")
            path = Path(result["record_path"])
            self.assertEqual(path.parent, self.root / "quota")
            stored = json.loads(path.read_text(encoding="ascii"))
            self.assertEqual(stored, result["snapshot"])
            self.assertEqual(stored["quota_pool_id"], "xiaomi-mimo-token-plan-standard")
            self.assertAlmostEqual(stored["monthly_remaining_fraction"], percentage / 100.0)
            self.assertEqual(stored["monthly_reset_at"], self.reset_at)
            self.assertIsNone(stored["short_window_remaining_fraction"])
            self.assertIsNone(stored["weekly_remaining_fraction"])
            self.assertIsNone(stored["credits_remaining"])

        # A credits-based monthly plan can carry its reset without a percentage.
        code, result = self.record(
            "--credits-remaining", "8261600584", "--monthly-reset-at", self.reset_at
        )
        self.assertEqual(code, fleet.EXIT_SUCCESS, result)
        stored = json.loads(Path(result["record_path"]).read_text(encoding="ascii"))
        self.assertIsNone(stored["monthly_remaining_fraction"])
        self.assertEqual(stored["monthly_reset_at"], self.reset_at)
        self.assertEqual(stored["credits_remaining"], 8261600584)

    def test_quota_status_shows_monthly_fields(self) -> None:
        code, recorded = self.record(
            "--monthly-remaining-pct", "75.1",
            "--monthly-reset-at", self.reset_at,
            "--credits-remaining", "8261600584",
        )
        self.assertEqual(code, fleet.EXIT_SUCCESS, recorded)
        code, status = self.run_cli("quota-status")
        self.assertEqual(code, fleet.EXIT_SUCCESS, status)
        view = status["quota_snapshots"]["mimo"]
        self.assertEqual(view["snapshot_id"], recorded["snapshot"]["snapshot_id"])
        self.assertAlmostEqual(view["monthly_remaining_fraction"], 0.751)
        self.assertEqual(view["monthly_reset_at"], self.reset_at)
        self.assertEqual(view["credits_remaining"], 8261600584)
        # Monthly remaining capacity now contributes to routing headroom.
        self.assertAlmostEqual(view["headroom"], 0.751)
        self.assertFalse(view["hard_blocking"])
        # quota-status writes no quota record, but its pace/latest.json
        # export under the existing state dir is one performed write.
        self.assertEqual(status["writes_performed"], 1)
        self.assertEqual(
            status["quota_collection"]["pace_latest"]["written"], True
        )

    def test_legacy_snapshot_remains_readable_alongside_monthly_snapshot(self) -> None:
        legacy = {
            "schema_version": 1,
            "snapshot_id": "quota-legacy",
            "agent": "legacy",
            "observed_at": adaptive.utc_now(),
            "valid_for_hours": 24,
            "source": "provider-dashboard",
            "confidence": 0.8,
            "short_window_remaining_fraction": 0.8,
            "short_window_reset_at": None,
            "weekly_remaining_fraction": 0.6,
            "weekly_reset_at": self.reset_at,
            "credits_remaining": None,
            "note": "Snapshot written before monthly fields existed",
        }
        path = self.root / "quota" / "quota-legacy.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(legacy), encoding="ascii")
        original_bytes = path.read_bytes()
        self.assertEqual(adaptive.read_quota_records(self.root), [legacy])

        code, before = self.run_cli("quota-status")
        self.assertEqual(code, fleet.EXIT_SUCCESS, before)
        code, recorded = self.record("--monthly-remaining-pct", "50")
        self.assertEqual(code, fleet.EXIT_SUCCESS, recorded)
        code, after = self.run_cli("quota-status")
        self.assertEqual(code, fleet.EXIT_SUCCESS, after)
        view = after["quota_snapshots"]["legacy"]
        for key, value in legacy.items():
            self.assertEqual(view[key], value, key)
        self.assertNotIn("monthly_remaining_fraction", view)
        self.assertNotIn("monthly_reset_at", view)
        for key in ("status", "headroom", "hard_blocking"):
            self.assertEqual(view[key], before["quota_snapshots"]["legacy"][key])
        self.assertEqual(view["headroom"], 0.6)
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(len(adaptive.read_quota_records(self.root)), 2)

    def test_invalid_monthly_values_are_rejected_like_weekly_values(self) -> None:
        cases = [
            ("remaining-pct", "-0.1", "Quota remaining fractions must be between 0 and 1"),
            ("remaining-pct", "100.1", "Quota remaining fractions must be between 0 and 1"),
            ("remaining-pct", "nan", "Quota remaining fractions must be between 0 and 1"),
            ("remaining-pct", "inf", "Quota remaining fractions must be between 0 and 1"),
            ("reset-at", "not-a-timestamp", "Invalid timestamp"),
            ("reset-at", "2026-02-30T00:00:00Z", "Invalid timestamp"),
            ("reset-at", "2026-10-22T23:59:59", "Timestamp must include a timezone"),
        ]
        for suffix, value, expected_error in cases:
            errors = []
            for window in ("weekly", "monthly"):
                code, result = self.record(
                    "--credits-remaining", "1", f"--{window}-{suffix}", value
                )
                self.assertEqual(code, fleet.EXIT_REFUSED, result)
                self.assertEqual(result["status"], "REFUSED")
                self.assertIn(expected_error, result["error"])
                self.assertEqual(result["writes_performed"], 0)
                errors.append(result["error"])
            self.assertEqual(errors[0], errors[1])
        self.assertEqual(adaptive.read_quota_records(self.root), [])
        self.assertFalse((self.root / "quota").exists())


class QuotaRecordProfilePoolTests(unittest.TestCase):
    def test_cli_records_declared_profile_pool_without_filesystem_write(self) -> None:
        stdout = mock.Mock()
        stdout.buffer = io.BytesIO()
        stderr = mock.Mock()
        stderr.buffer = io.BytesIO()
        with (
            mock.patch.object(fleet, "load_profiles", return_value={
                "mimo": {"adapter": "mimo", "quota_pool_id": "xiaomi-mimo-token-plan-standard"}
            }),
            mock.patch.object(adaptive, "atomic_write") as write,
            mock.patch.object(fleet.sys, "stdout", stdout),
            mock.patch.object(fleet.sys, "stderr", stderr),
        ):
            code = fleet.main([
                "--state-dir", "unused-state", "quota-record", "--agent", "mimo",
                "--source", "provider-dashboard", "--credits-remaining", "100",
            ])
        self.assertEqual(code, fleet.EXIT_SUCCESS)
        result = json.loads(stdout.buffer.getvalue())
        self.assertEqual(result["snapshot"]["quota_pool_id"], "xiaomi-mimo-token-plan-standard")
        write.assert_called_once()
        self.assertEqual(write.call_args.args[1], result["snapshot"])


if __name__ == "__main__":
    unittest.main()
