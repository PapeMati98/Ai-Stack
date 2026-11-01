from __future__ import annotations

import concurrent.futures
import datetime as dt
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import adaptive  # noqa: E402
import fleet  # noqa: E402

UTC = dt.timezone.utc
POOL = "test-pool-monthly"


def monthly_row(observed: dt.datetime, reset: dt.datetime) -> dict:
    return {
        "schema_version": 1,
        "snapshot_id": "quota-synthetic",
        "agent": "mimo",
        "quota_pool_id": POOL,
        "observed_at": observed.isoformat(),
        "valid_for_hours": 6,
        "source": "provider-dashboard",
        "confidence": 1.0,
        "monthly_remaining_fraction": 0.7,
        "monthly_reset_at": reset.isoformat(),
    }


def parsed_z(value: str) -> dt.datetime:
    if not value.endswith("Z"):
        raise ValueError(f"Expected UTC Z timestamp: {value}")
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


class PaceLatestFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime.now(UTC)
        self.observed = self.now - dt.timedelta(minutes=2)
        self.reset = self.now + dt.timedelta(days=20)
        self.raw = monthly_row(self.observed, self.reset)
        self.profiles = {
            "mimo": {"adapter": "mimo", "quota_pool_id": POOL},
            "other": {"adapter": "other", "quota_pool_id": "pool-empty"},
        }

    def collect(
        self,
        root: Path,
        agent_ids: list[str] | None = None,
        profiles: dict[str, dict] | None = None,
    ):
        profiles = self.profiles if profiles is None else profiles
        with (
            mock.patch.object(
                adaptive,
                "latest_quotas",
                return_value={
                    "mimo": adaptive._quota_view(self.raw, current=self.now)
                },
            ),
            mock.patch.object(
                adaptive, "read_quota_records", return_value=[self.raw]
            ),
            mock.patch.object(
                fleet.quota_collectors,
                "collect_profile_quotas",
                return_value=({}, {}),
            ),
            mock.patch.object(fleet, "read_usage", return_value=[]),
        ):
            return fleet.current_quota_views(root, profiles, agent_ids=agent_ids)

    def test_latest_json_matches_contract_and_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            root.mkdir()
            views, diagnostics = self.collect(root)
            latest_path = root / "pace" / "latest.json"
            self.assertTrue(latest_path.is_file())
            payload = json.loads(latest_path.read_text(encoding="ascii"))
            leftovers = list((root / "pace").glob("*.tmp"))
            quota_dir_leak = (root / "quota" / "latest.json").exists()

        self.assertEqual(leftovers, [])
        self.assertFalse(quota_dir_leak)
        self.assertEqual(payload["schema_version"], 1)
        self.assertTrue(payload["generated_at"].endswith("Z"))
        dt.datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00"))
        self.assertEqual(payload["fleet_version"], fleet.VERSION)
        self.assertEqual(set(payload["pools"]), {POOL, "pool-empty"})

        entry = payload["pools"][POOL]
        pace = views["mimo"]["pace"]
        self.assertEqual(entry["agents"], ["mimo"])
        self.assertTrue(entry["computed_at"].endswith("Z"))
        dt.datetime.fromisoformat(entry["computed_at"].replace("Z", "+00:00"))
        for key in (
            "tempo",
            "confidence",
            "source",
            "reason",
            "window",
            "observed_at",
            "reset_at",
            "remaining_fraction",
            "elapsed_fraction",
        ):
            self.assertEqual(entry[key], pace[key], key)
        self.assertEqual(entry["window"], "monthly")
        self.assertGreater(entry["tempo"], 0)
        self.assertTrue(0 <= entry["confidence"] <= 1)

        empty = payload["pools"]["pool-empty"]
        self.assertEqual(empty["agents"], ["other"])
        self.assertTrue(empty["computed_at"].endswith("Z"))
        self.assertIsNone(empty["tempo"])
        self.assertEqual(empty["source"], "none")
        self.assertNotIn("window", empty)
        self.assertNotIn("reset_at", empty)

        self.assertEqual(
            diagnostics["pace_latest"],
            {"path": str(latest_path), "written": True},
        )

    def test_partial_profiles_merge_three_pools_with_per_pool_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            profiles = {
                **self.profiles,
                "third": {"adapter": "other", "quota_pool_id": "pool-third"},
            }
            self.collect(root, profiles=profiles)
            latest_path = root / "pace" / "latest.json"
            before = json.loads(latest_path.read_text(encoding="ascii"))
            self.assertEqual(set(before["pools"]), {POOL, "pool-empty", "pool-third"})

            self.collect(root, profiles={"mimo": profiles["mimo"]})
            after = json.loads(latest_path.read_text(encoding="ascii"))

        self.assertEqual(set(after["pools"]), set(before["pools"]))
        self.assertGreater(
            parsed_z(after["pools"][POOL]["computed_at"]),
            parsed_z(before["pools"][POOL]["computed_at"]),
        )
        for pool_id in ("pool-empty", "pool-third"):
            self.assertEqual(after["pools"][pool_id], before["pools"][pool_id])
        self.assertGreater(parsed_z(after["generated_at"]), parsed_z(before["generated_at"]))

    def test_agent_ids_view_refreshes_its_pool_and_keeps_other_pools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            root.mkdir()
            self.collect(root)
            latest_path = root / "pace" / "latest.json"
            before = json.loads(latest_path.read_text(encoding="ascii"))
            views, diagnostics = self.collect(root, agent_ids=["mimo"])
            after = json.loads(latest_path.read_text(encoding="ascii"))
            empty_root = Path(temporary) / "fleet-root-empty"
            self.collect(empty_root, agent_ids=["mimo"])
            created = json.loads((empty_root / "pace" / "latest.json").read_text(encoding="ascii"))
        self.assertIn("pace", views["mimo"])
        self.assertEqual(set(after["pools"]), {POOL, "pool-empty"})
        self.assertEqual(after["pools"]["pool-empty"], before["pools"]["pool-empty"])
        self.assertGreater(
            parsed_z(after["pools"][POOL]["computed_at"]),
            parsed_z(before["pools"][POOL]["computed_at"]),
        )
        self.assertIs(diagnostics["pace_latest"]["written"], True)
        self.assertEqual(set(created["pools"]), {POOL})

    def test_corrupt_or_other_schema_starts_with_computed_pools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            path = root / "pace" / "latest.json"
            path.parent.mkdir(parents=True)
            for old in ("{broken", json.dumps({"schema_version": 2, "pools": {"stale": {}}})):
                path.write_text(old, encoding="ascii")
                self.collect(root, profiles={"mimo": self.profiles["mimo"]})
                payload = json.loads(path.read_text(encoding="ascii"))
                self.assertEqual(payload["schema_version"], 1)
                self.assertEqual(set(payload["pools"]), {POOL})
                self.assertTrue(payload["pools"][POOL]["computed_at"].endswith("Z"))

    def test_legacy_pool_gets_old_file_generated_at(self) -> None:
        old_generated_at = "2026-09-24T16:02:00Z"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            path = root / "pace" / "latest.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "schema_version": 1,
                "generated_at": old_generated_at,
                "fleet_version": fleet.VERSION,
                "pools": {"legacy": {"agents": ["old"], "tempo": 9.25}},
            }), encoding="ascii")
            self.collect(root, profiles={"mimo": self.profiles["mimo"]})
            payload = json.loads(path.read_text(encoding="ascii"))
        self.assertEqual(payload["pools"]["legacy"]["computed_at"], old_generated_at)
        self.assertEqual(payload["pools"]["legacy"]["tempo"], 9.25)
        self.assertNotEqual(payload["pools"][POOL]["computed_at"], old_generated_at)

    def test_two_threads_merge_under_one_valid_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            barrier = threading.Barrier(2)

            def write(pool_id: str) -> bool:
                barrier.wait(timeout=5)
                return fleet._write_pace_latest(
                    root,
                    {pool_id: {"tempo": 1.0}},
                    {pool_id: pool_id},
                    dt.datetime.now(UTC),
                    {},
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(write, ("pool-a", "pool-b")))
            path = root / "pace" / "latest.json"
            payload = json.loads(path.read_text(encoding="ascii"))
        self.assertEqual(outcomes, [True, True])
        self.assertEqual(set(payload["pools"]), {"pool-a", "pool-b"})
        self.assertTrue(all(row["computed_at"].endswith("Z") for row in payload["pools"].values()))

    def test_unwritable_pace_directory_keeps_routing_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            root.mkdir()
            (root / "pace").write_text("a file blocks the pace directory")
            views, diagnostics = self.collect(root)
            self.assertFalse((root / "pace" / "latest.json").exists())
        pace = views["mimo"]["pace"]
        self.assertEqual(pace["window"], "monthly")
        self.assertGreater(pace["tempo"], 0)
        self.assertIs(diagnostics["pace_latest"]["written"], False)
        self.assertIn("error_type", diagnostics["pace_latest"])

    def test_quota_status_reports_writes_truthfully(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            root.mkdir()
            stdout = mock.Mock()
            stdout.buffer = io.BytesIO()
            stderr = mock.Mock()
            stderr.buffer = io.BytesIO()
            with (
                mock.patch.object(
                    fleet, "load_profiles", return_value=self.profiles
                ),
                mock.patch.object(
                    adaptive,
                    "latest_quotas",
                    return_value={
                        "mimo": adaptive._quota_view(self.raw, current=self.now)
                    },
                ),
                mock.patch.object(
                    adaptive, "read_quota_records", return_value=[self.raw]
                ),
                mock.patch.object(
                    fleet.quota_collectors,
                    "collect_profile_quotas",
                    return_value=({}, {}),
                ),
                mock.patch.object(fleet, "read_usage", return_value=[]),
                mock.patch.object(fleet.sys, "stdout", stdout),
                mock.patch.object(fleet.sys, "stderr", stderr),
            ):
                code = fleet.main(["--state-dir", str(root), "quota-status"])
            self.assertEqual(code, fleet.EXIT_SUCCESS)
            result = json.loads(stdout.buffer.getvalue())
            latest_path = root / "pace" / "latest.json"
            self.assertTrue(latest_path.is_file())

        # writes_performed is truthful: quota-status performs no quota-record
        # write, but the successful pace/latest.json export counts as one
        # performed write and is also reported in quota_collection.pace_latest.
        self.assertEqual(result["writes_performed"], 1)
        self.assertEqual(
            result["quota_collection"]["pace_latest"],
            {"path": str(latest_path), "written": True},
        )
        pace = result["quota_collection"][POOL]["pace"]
        self.assertEqual(pace["window"], "monthly")

    def test_missing_state_dir_is_created_for_the_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "never-initialized"
            views, diagnostics = self.collect(root)
            latest_path = root / "pace" / "latest.json"
            self.assertTrue(latest_path.is_file())
            payload = json.loads(latest_path.read_text(encoding="ascii"))
        # The contract requires <state_dir>/pace/latest.json whenever pace was
        # computed, so the export initializes the state directory instead of
        # being skipped.
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(set(payload["pools"]), {POOL, "pool-empty"})
        self.assertEqual(views["mimo"]["pace"]["window"], "monthly")
        self.assertEqual(
            diagnostics["pace_latest"],
            {"path": str(latest_path), "written": True},
        )

    def test_quota_status_reports_zero_when_pace_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            root.mkdir()
            (root / "pace").write_text("a file blocks the pace directory")
            stdout = mock.Mock()
            stdout.buffer = io.BytesIO()
            stderr = mock.Mock()
            stderr.buffer = io.BytesIO()
            with (
                mock.patch.object(
                    fleet, "load_profiles", return_value=self.profiles
                ),
                mock.patch.object(
                    adaptive,
                    "latest_quotas",
                    return_value={
                        "mimo": adaptive._quota_view(self.raw, current=self.now)
                    },
                ),
                mock.patch.object(
                    adaptive, "read_quota_records", return_value=[self.raw]
                ),
                mock.patch.object(
                    fleet.quota_collectors,
                    "collect_profile_quotas",
                    return_value=({}, {}),
                ),
                mock.patch.object(fleet, "read_usage", return_value=[]),
                mock.patch.object(fleet.sys, "stdout", stdout),
                mock.patch.object(fleet.sys, "stderr", stderr),
            ):
                code = fleet.main(["--state-dir", str(root), "quota-status"])
            self.assertEqual(code, fleet.EXIT_SUCCESS)
            result = json.loads(stdout.buffer.getvalue())

        self.assertEqual(result["writes_performed"], 0)
        self.assertIs(
            result["quota_collection"]["pace_latest"]["written"], False
        )


if __name__ == "__main__":
    unittest.main()
