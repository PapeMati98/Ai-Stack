from __future__ import annotations

import copy
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402
import quota_collectors  # noqa: E402
import quota_pace  # noqa: E402
import routing_v2  # noqa: E402

PROFILES_PATH = SCRIPT_DIR.parent / "references" / "profiles.json"
UTC = dt.timezone.utc
PROMPT_MARKER = "PROMPT-SECRET-MARKER-7f3a9c"
PATH_MARKER = "rollout-PATHMARKER-51bd.jsonl"


def token_count_event(
    observed: dt.datetime,
    *,
    primary: dict | None = None,
    secondary: dict | None = None,
    plan_type: str = "plus",
    nested: str = "info",
    resets_style: str = "epoch",
) -> dict:
    rate_limits: dict[str, object] = {"plan_type": plan_type}
    for key, spec in (("primary", primary), ("secondary", secondary)):
        if spec is None:
            rate_limits[key] = None
            continue
        window = {
            "used_percent": spec["used_percent"],
            "window_minutes": spec["window_minutes"],
        }
        if resets_style == "epoch":
            window["resets_at"] = spec["reset"].timestamp()
        else:
            window["resets_in_seconds"] = (spec["reset"] - observed).total_seconds()
        rate_limits[key] = window
    rate_limits["credits"] = {"balance": 987.65}
    rate_limits["limit_id"] = "LIMIT-MARKER-9e21"
    if nested == "info":
        payload: dict[str, object] = {
            "type": "token_count",
            "info": {"total_token_usage": {"total_tokens": 42}, "rate_limits": rate_limits},
        }
    else:
        payload = {"type": "token_count", "rate_limits": rate_limits}
    return {
        "timestamp": observed.isoformat().replace("+00:00", "Z"),
        "type": "event_msg",
        "payload": payload,
    }


def write_rollout(
    directory: Path,
    name: str,
    lines: list[str],
    *,
    mtime: float | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def collector_config(sessions_dir: Path, **overrides: object) -> dict:
    config: dict[str, object] = {
        "collector_mode": "codex-rollout-rate-limits",
        "sessions_dir": str(sessions_dir),
        "max_files": 8,
        "max_bytes_per_file": 262144,
        "max_sample_age_seconds": 3600,
        "confidence": 0.9,
    }
    config.update(overrides)
    return config


def weekly_spec(used_percent: float, observed: dt.datetime, elapsed: float) -> dict:
    return {
        "used_percent": used_percent,
        "window_minutes": 10080,
        "reset": observed + dt.timedelta(days=7 * (1.0 - elapsed)),
    }


class CodexRolloutRateLimitsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime.now(UTC)
        self.observed = self.now - dt.timedelta(minutes=3)

    def collect(self, sessions_dir: Path, **overrides: object) -> dict:
        return quota_collectors.collect_codex_rollout_rate_limits(
            collector_config(sessions_dir, **overrides), pool_id="codex"
        )

    def pace_for(self, row: dict) -> dict:
        return quota_pace.calculate_pool_pace(
            "codex",
            snapshots=[{**row, "agent": "codex"}],
            runs=[],
            pool_ids={"codex": "codex"},
            now=self.now,
        )

    def test_weekly_primary_only_gives_overburn_pace_and_routing_penalty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            event = token_count_event(
                self.observed, primary=weekly_spec(50, self.observed, 0.39)
            )
            write_rollout(
                sessions / "2026" / "09" / "24",
                "rollout-a.jsonl",
                [json.dumps(event)],
            )
            row = self.collect(sessions)
        self.assertEqual(row["status"], "fresh")
        self.assertEqual(row["weekly_remaining_fraction"], 0.5)
        self.assertIsNone(row["short_window_remaining_fraction"])
        self.assertIsNone(row["monthly_remaining_fraction"])
        pace = self.pace_for(row)
        self.assertEqual(pace["window"], "weekly")
        self.assertEqual(pace["source"], "codex-rollout-rate-limits")
        self.assertAlmostEqual(pace["tempo"], 0.5 / 0.39, delta=0.02)
        self.assertGreater(pace["tempo"], 1.25)
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        execution = routing_v2.resolve_execution_profile(
            "codex",
            profiles["codex"],
            complexity="medium",
            mode="implement",
            required_tools=["write"],
        )
        assessment = json.loads(
            (fleet.SKILL_ROOT / "references" / "task-profile-v2-example.json").read_text(
                encoding="utf-8"
            )
        )
        _, _, penalties, _ = routing_v2.assignment_score(
            capability_fit=90.0,
            execution=execution,
            assessment=assessment,
            quota={"status": "fresh", "headroom": 0.5},
            underuse_score=50.0,
            local={"sample_count": 0, "active": False},
            pace=pace,
        )
        self.assertIn("pace_overburn", penalties)
        self.assertGreater(penalties["pace_overburn"], 0.0)

    def test_primary_short_window_and_secondary_weekly(self) -> None:
        primary = {
            "used_percent": 60,
            "window_minutes": 300,
            "reset": self.observed + dt.timedelta(hours=1),
        }
        event = token_count_event(
            self.observed,
            primary=primary,
            secondary=weekly_spec(50, self.observed, 0.39),
        )
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            write_rollout(sessions, "rollout-a.jsonl", [json.dumps(event)])
            row = self.collect(sessions)
        self.assertEqual(row["status"], "fresh")
        self.assertEqual(row["short_window_remaining_fraction"], 0.4)
        self.assertEqual(row["short_window_hours"], 5.0)
        self.assertEqual(row["weekly_remaining_fraction"], 0.5)
        self.assertEqual(row["headroom"], 0.4)
        pace = self.pace_for(row)
        self.assertEqual(pace["window"], "weekly")  # max tempo wins

    def test_short_window_pace_uses_window_hours_from_snapshot_row(self) -> None:
        primary = {
            "used_percent": 60,
            "window_minutes": 300,
            "reset": self.observed + dt.timedelta(hours=1),
        }
        event = token_count_event(self.observed, primary=primary)
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            write_rollout(sessions, "rollout-a.jsonl", [json.dumps(event)])
            row = self.collect(sessions)
        self.assertIsNone(row["weekly_remaining_fraction"])
        # No profile-level short_window_hours fallback is passed: the pace must
        # come from the row's own short_window_hours (5 h window, 4 h elapsed).
        pace = self.pace_for(row)
        self.assertEqual(pace["window"], "short")
        self.assertAlmostEqual(pace["tempo"], 0.6 / 0.8, delta=0.02)

    def test_resets_in_seconds_variant_matches_epoch_variant(self) -> None:
        events = [
            token_count_event(
                self.observed,
                primary=weekly_spec(50, self.observed, 0.39),
                resets_style=style,
            )
            for style in ("epoch", "relative")
        ]
        rows = []
        with tempfile.TemporaryDirectory() as temporary:
            for index, event in enumerate(events):
                sessions = Path(temporary) / f"sessions-{index}"
                write_rollout(sessions, "rollout-a.jsonl", [json.dumps(event)])
                rows.append(self.collect(sessions))
        self.assertEqual(rows[0]["status"], "fresh")
        self.assertEqual(rows[1]["status"], "fresh")
        self.assertEqual(
            rows[0]["weekly_remaining_fraction"], rows[1]["weekly_remaining_fraction"]
        )
        self.assertEqual(rows[0]["weekly_reset_at"], rows[1]["weekly_reset_at"])

    def test_corrupted_resets_in_seconds_overflow_skips_window_not_routing(self) -> None:
        broken = token_count_event(
            self.now - dt.timedelta(minutes=1),
            secondary={
                "used_percent": 99,
                "window_minutes": 300,
                "reset": self.now + dt.timedelta(hours=1),
            },
        )
        # A finite but astronomical resets_in_seconds overflows timedelta.
        # The corrupted window must be skipped like any other invalid line,
        # never raised out of the collector into a routing decision.
        secondary = broken["payload"]["info"]["rate_limits"]["secondary"]
        del secondary["resets_at"]
        secondary["resets_in_seconds"] = 1e300
        good = token_count_event(
            self.observed, primary=weekly_spec(50, self.observed, 0.39)
        )
        with tempfile.TemporaryDirectory() as temporary:
            only_broken = Path(temporary) / "broken"
            write_rollout(only_broken, "rollout-a.jsonl", [json.dumps(broken)])
            row_broken = self.collect(only_broken)
            profiles = {
                "codex": {
                    "quota_pool_id": "codex",
                    "quota_collection": collector_config(only_broken),
                }
            }
            rows, diagnostics = quota_collectors.collect_profile_quotas(profiles)
            mixed = Path(temporary) / "mixed"
            write_rollout(
                mixed, "rollout-b.jsonl", [json.dumps(good), json.dumps(broken)]
            )
            row_mixed = self.collect(mixed)
        self.assertEqual(row_broken["status"], "fresh")
        self.assertEqual(row_broken["short_window_remaining_fraction"], 0.01)
        self.assertIsNone(row_broken["short_window_reset_at"])
        self.assertEqual(rows["codex"]["status"], "fresh")
        self.assertEqual(rows["codex"]["headroom"], 0.01)
        self.assertEqual(row_mixed["status"], "fresh")
        # The newer short-only sample is retained. An older weekly counter
        # must not be relabelled with the new sample's timestamp.
        self.assertIsNone(row_mixed["weekly_remaining_fraction"])
        self.assertEqual(row_mixed["short_window_remaining_fraction"], 0.01)
        self.assertIsNone(row_mixed["short_window_reset_at"])

    def test_payload_level_rate_limits_block_is_accepted(self) -> None:
        event = token_count_event(
            self.observed,
            primary=weekly_spec(50, self.observed, 0.39),
            nested="payload",
        )
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            write_rollout(sessions, "rollout-a.jsonl", [json.dumps(event)])
            row = self.collect(sessions)
        self.assertEqual(row["status"], "fresh")
        self.assertEqual(row["weekly_remaining_fraction"], 0.5)

    def test_sample_older_than_ceiling_is_stale(self) -> None:
        old_observed = self.now - dt.timedelta(hours=3)
        event = token_count_event(
            old_observed, primary=weekly_spec(50, old_observed, 0.39)
        )
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            write_rollout(sessions, "rollout-a.jsonl", [json.dumps(event)])
            row = self.collect(sessions)
        self.assertEqual(row["status"], "stale")
        self.assertIsNone(row["headroom"])
        self.assertEqual(row["reported_headroom"], 0.5)
        # A stale sample carries no window fields: pace and routing penalties
        # must never run on evidence the age ceiling already rejected.
        self.assertIsNone(row["weekly_remaining_fraction"])
        self.assertIsNone(row["weekly_reset_at"])
        self.assertGreaterEqual(row["age_hours"], 3.0)
        pace = self.pace_for(row)
        self.assertIsNone(pace["tempo"])
        self.assertEqual(pace["source"], "none")

    def test_used_percent_above_limit_clamps_remaining_to_zero(self) -> None:
        event = token_count_event(
            self.observed,
            primary={"used_percent": 105, "window_minutes": 10080,
                     "reset": self.observed + dt.timedelta(days=4)},
        )
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            write_rollout(sessions, "rollout-a.jsonl", [json.dumps(event)])
            row = self.collect(sessions)
        self.assertEqual(row["status"], "fresh")
        self.assertEqual(row["weekly_remaining_fraction"], 0.0)
        self.assertEqual(row["headroom"], 0.0)

    def test_corrupted_lines_and_foreign_events_are_ignored(self) -> None:
        good = token_count_event(
            self.observed, primary=weekly_spec(50, self.observed, 0.39)
        )
        broken_window = token_count_event(
            self.now - dt.timedelta(minutes=1),
            primary={"used_percent": "950", "window_minutes": 10080,
                     "reset": self.now + dt.timedelta(days=3)},
        )
        # A foreign response_item event with a valid-looking rate_limits
        # block and the newest timestamp is not a token_count quota sample.
        foreign = {
            "timestamp": self.now.isoformat().replace("+00:00", "Z"),
            "type": "response_item",
            "payload": {
                "type": "message",
                "rate_limits": {
                    "plan_type": "plus",
                    "primary": {
                        "used_percent": 99,
                        "window_minutes": 10080,
                        "resets_at": (self.now + dt.timedelta(days=3)).timestamp(),
                    },
                },
            },
        }
        lines = [
            '{"payload": {"rate_limits": broken json',
            json.dumps({"type": "response_item", "payload": {"message": PROMPT_MARKER}}),
            json.dumps({"timestamp": "not-a-timestamp", "payload": {"rate_limits": {}}}),
            json.dumps(broken_window),
            json.dumps(foreign),
            json.dumps(good),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            write_rollout(sessions, "rollout-a.jsonl", lines)
            row = self.collect(sessions)
        self.assertEqual(row["status"], "fresh")
        self.assertEqual(row["weekly_remaining_fraction"], 0.5)
        self.assertEqual(
            row["observed_at"], good["timestamp"]
        )

    def test_missing_directory_is_unknown_with_reason_and_no_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "no-such-dir"
            row = self.collect(missing)
            profiles = {
                "codex": {
                    "quota_pool_id": "codex",
                    "quota_collection": collector_config(missing),
                }
            }
            rows, diagnostics = quota_collectors.collect_profile_quotas(profiles)
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["reason"], "sessions-dir-unavailable")
        self.assertIsNone(row["headroom"])
        self.assertEqual(rows["codex"]["status"], "unknown")
        self.assertEqual(diagnostics["codex"]["status"], "UNKNOWN")
        self.assertIn("sessions-dir-unavailable", diagnostics["codex"]["blocker"])

    def test_empty_directory_and_directory_without_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            empty = Path(temporary) / "empty"
            empty.mkdir()
            row_empty = self.collect(empty)
            no_samples = Path(temporary) / "no-samples"
            write_rollout(
                no_samples,
                "rollout-a.jsonl",
                [json.dumps({"payload": {"message": "hi"}}), "junk"],
            )
            row_no_samples = self.collect(no_samples)
        self.assertEqual(row_empty["reason"], "no-rollout-files")
        self.assertEqual(row_no_samples["reason"], "no-rate-limits-samples")

    def test_max_files_reads_only_the_newest_files(self) -> None:
        newest = self.now.timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            for index in range(100):
                if index < 98:
                    # Older files carry a fresher-looking extreme sample; a
                    # collector that read them would report 0.01 remaining.
                    event = token_count_event(
                        self.observed,
                        primary=weekly_spec(99, self.observed, 0.39),
                    )
                    mtime = newest - (100 - index) * 60
                else:
                    event = token_count_event(
                        self.observed,
                        primary=weekly_spec(40, self.observed, 0.39),
                    )
                    mtime = newest - (100 - index) * 60
                write_rollout(
                    sessions,
                    f"rollout-{index:03d}.jsonl",
                    [json.dumps(event)],
                    mtime=mtime,
                )
            row = self.collect(sessions, max_files=2)
        self.assertEqual(row["status"], "fresh")
        self.assertEqual(row["weekly_remaining_fraction"], 0.6)

    def test_newest_rollout_files_keeps_bounded_heap_of_newest(self) -> None:
        newest = self.now.timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            expected: list[str] = []
            for index in range(100):
                path = write_rollout(
                    sessions / "2026" / "09" / f"{index % 28 + 1:02d}",
                    f"rollout-{index:03d}.jsonl",
                    ["{}"],
                    mtime=newest - (100 - index) * 60,
                )
                if index >= 97:
                    expected.append(str(path))
            found = quota_collectors._newest_rollout_files(
                sessions, max_files=3, max_entries=1000
            )
            capped = quota_collectors._newest_rollout_files(
                sessions, max_files=8, max_entries=10
            )
        self.assertEqual([path for _, path in found], sorted(expected, reverse=True))
        self.assertLessEqual(len(capped), 8)
        # The entry cap bounds the walk itself, so only a fraction of the
        # 100 files was visited and no error is raised.
        self.assertLess(len(capped), 8)

    def test_entry_cap_truncates_oldest_tail_not_newest_files(self) -> None:
        newest = self.now.timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            for index in range(30):
                # One rollout per ISO day partition; the newest partition
                # carries the live sample, the older ones a 99 % decoy.
                day = index + 1
                event = token_count_event(
                    self.observed,
                    primary=weekly_spec(
                        40 if day == 30 else 99, self.observed, 0.39
                    ),
                )
                write_rollout(
                    sessions / "2026" / "09" / f"{day:02d}",
                    f"rollout-2026-09-{day:02d}T00-00-00-aaaa.jsonl",
                    [json.dumps(event)],
                    mtime=newest - (30 - index) * 60,
                )
            # The budget covers the sessions/2026/09 listings (32 entries) and
            # descends newest-name-first: partition 30 (and 29) is scanned,
            # the oldest partitions are truncated away.
            row = self.collect(sessions, max_scan_entries=34, max_files=4)
        self.assertEqual(row["status"], "fresh")
        self.assertEqual(row["weekly_remaining_fraction"], 0.6)

    def test_entry_cap_explores_newest_partition_directories_first(self) -> None:
        newest = self.now.timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            old_event = token_count_event(
                self.observed, primary=weekly_spec(99, self.observed, 0.39)
            )
            for index in range(25):
                write_rollout(
                    sessions / "2026" / "09" / "23",
                    f"rollout-2026-09-23T{index:02d}-00-00-bbbb.jsonl",
                    [json.dumps(old_event)],
                    mtime=newest - 3600,
                )
            new_event = token_count_event(
                self.observed, primary=weekly_spec(40, self.observed, 0.39)
            )
            write_rollout(
                sessions / "2026" / "09" / "24",
                "rollout-2026-09-24T00-00-00-cccc.jsonl",
                [json.dumps(new_event)],
                mtime=newest,
            )
            # 4 entries reach the day partitions (2026, 09, 23, 24); the cap
            # at 6 leaves room for only one file, which must be the newest
            # partition's rollout, not a file from the older partition.
            row = self.collect(sessions, max_scan_entries=6, max_files=4)
        self.assertEqual(row["status"], "fresh")
        self.assertEqual(row["weekly_remaining_fraction"], 0.6)

    def test_newest_mtime_wins_over_name_order_within_directory(self) -> None:
        newest = self.now.timestamp()
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            live = token_count_event(
                self.observed, primary=weekly_spec(40, self.observed, 0.39)
            )
            stale_sample = token_count_event(
                self.observed, primary=weekly_spec(99, self.observed, 0.39)
            )
            # Name order disagrees with recency: the alphabetically first
            # file carries the newest mtime and the live sample. With the
            # entry budget covering the directory, the mtime heap decides
            # and the zzz-named decoys cannot displace the newest file.
            write_rollout(
                sessions,
                "rollout-aaa-00.jsonl",
                [json.dumps(live)],
                mtime=newest,
            )
            for index in range(20):
                write_rollout(
                    sessions,
                    f"rollout-zzz-{index:02d}.jsonl",
                    [json.dumps(stale_sample)],
                    mtime=newest - (index + 1) * 60,
                )
            row = self.collect(sessions, max_scan_entries=64, max_files=1)
        self.assertEqual(row["status"], "fresh")
        self.assertEqual(row["weekly_remaining_fraction"], 0.6)

    def test_entry_cap_bounds_entries_visited_inside_single_directory(self) -> None:
        newest = self.now.timestamp()
        real_scandir = os.scandir
        pulled = 0

        class CountingListing:
            def __init__(self, listing):
                self._listing = listing

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._listing.close()
                return False

            def __iter__(self):
                nonlocal pulled
                for entry in self._listing:
                    pulled += 1
                    yield entry

        def counting_scandir(path):
            return CountingListing(real_scandir(path))

        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            for index in range(40):
                event = token_count_event(
                    self.observed, primary=weekly_spec(40, self.observed, 0.39)
                )
                write_rollout(
                    sessions,
                    f"rollout-{index:02d}.jsonl",
                    [json.dumps(event)],
                    mtime=newest - index * 60,
                )
            with mock.patch.object(os, "scandir", counting_scandir):
                found = quota_collectors._newest_rollout_files(
                    sessions, max_files=4, max_entries=5
                )
        # The per-entry budget stops the walk inside one oversized flat
        # directory: 5 entries processed plus the one yielded at the break,
        # instead of all 40 files on every routing decision.
        self.assertLessEqual(pulled, 6)
        self.assertLessEqual(len(found), 4)

    def test_max_bytes_reads_only_the_file_tail(self) -> None:
        event = token_count_event(
            self.observed, primary=weekly_spec(50, self.observed, 0.39)
        )
        padding = ['{"padding": "' + "x" * 200 + '"}'] * 60
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            write_rollout(sessions, "rollout-a.jsonl", [json.dumps(event), *padding])
            row = self.collect(sessions, max_bytes_per_file=4096)
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["reason"], "no-rate-limits-samples")

    def test_result_never_contains_prompt_content_paths_or_credit_balances(self) -> None:
        event = token_count_event(
            self.observed, primary=weekly_spec(50, self.observed, 0.39)
        )
        prompt_line = json.dumps(
            {"type": "response_item", "payload": {"message": PROMPT_MARKER}}
        )
        with tempfile.TemporaryDirectory() as temporary:
            sessions = Path(temporary) / "sessions"
            write_rollout(sessions, PATH_MARKER, [prompt_line, json.dumps(event)])
            row = self.collect(sessions)
            profiles = {
                "codex": {
                    "quota_pool_id": "codex",
                    "quota_collection": collector_config(sessions),
                }
            }
            _, diagnostics = quota_collectors.collect_profile_quotas(profiles)
            for payload in (json.dumps(row), json.dumps(diagnostics)):
                self.assertNotIn(PROMPT_MARKER, payload)
                self.assertNotIn(PATH_MARKER, payload)
                self.assertNotIn(str(sessions), payload)
                self.assertNotIn("987.65", payload)
                self.assertNotIn("LIMIT-MARKER", payload)
        self.assertEqual(row["plan_type"], "plus")


class CodexProfileWiringTests(unittest.TestCase):
    def test_real_profile_uses_bounded_rollout_collector(self) -> None:
        profiles = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))["profiles"]
        config = profiles["codex"]["quota_collection"]
        self.assertEqual(config["collector_mode"], "codex-rollout-rate-limits")
        self.assertEqual(config["sessions_dir"], "%USERPROFILE%\\.codex\\sessions")
        self.assertLessEqual(int(config["max_files"]), 16)
        self.assertLessEqual(int(config["max_bytes_per_file"]), 1024 * 1024)
        self.assertLessEqual(int(config["max_sample_age_seconds"]), 3600)
        self.assertIn(
            "codex-rollout-rate-limits",
            quota_collectors.MACHINE_READABLE_COLLECTOR_MODES,
        )

    def test_current_quota_views_exposes_codex_pace_from_fixture(self) -> None:
        now = dt.datetime.now(UTC)
        observed = now - dt.timedelta(minutes=3)
        event = token_count_event(
            observed, primary=weekly_spec(50, observed, 0.39)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            sessions = Path(temporary) / "sessions"
            write_rollout(sessions, "rollout-a.jsonl", [json.dumps(event)])
            profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
            codex = copy.deepcopy(profiles["codex"])
            codex["quota_collection"]["sessions_dir"] = str(sessions)
            views, diagnostics = fleet.current_quota_views(root, {"codex": codex})
        view = views["codex"]
        self.assertEqual(view["status"], "fresh")
        self.assertIs(view["current"], True)
        pace = view["pace"]
        self.assertEqual(pace["window"], "weekly")
        self.assertAlmostEqual(pace["tempo"], 0.5 / 0.39, delta=0.02)
        self.assertEqual(diagnostics["codex"]["pace"], pace)

    def test_dashboard_view_does_not_hide_codex_window_from_pace(self) -> None:
        # Regression (0.9.25 acceptance): a fresh provider-dashboard record
        # with confidence 1.0 wins the status view over the 0.9-confidence
        # rollout collector row, but the displaced live row must still feed
        # quota pace - before the fix current_quota_views returned
        # tempo=None, source=none and routing lost the pace_overburn penalty.
        now = dt.datetime.now(UTC)
        observed = now - dt.timedelta(minutes=3)
        event = token_count_event(
            observed, primary=weekly_spec(50, observed, 0.39)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "fleet-root"
            sessions = Path(temporary) / "sessions"
            write_rollout(sessions, "rollout-a.jsonl", [json.dumps(event)])
            quota_dir = root / "quota"
            quota_dir.mkdir(parents=True)
            dashboard = {
                "schema_version": 1,
                "agent": "codex",
                "quota_pool_id": "codex",
                "observed_at": (now - dt.timedelta(minutes=1))
                .isoformat()
                .replace("+00:00", "Z"),
                "source": "provider-dashboard",
                "confidence": 1.0,
                "weekly_reset_at": None,
                "valid_for_hours": 24,
            }
            (quota_dir / "quota-2026-09-24.json").write_text(
                json.dumps(dashboard), encoding="ascii"
            )
            profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
            codex = copy.deepcopy(profiles["codex"])
            codex["quota_collection"]["sessions_dir"] = str(sessions)
            views, diagnostics = fleet.current_quota_views(root, {"codex": codex})
        view = views["codex"]
        self.assertEqual(view["source"], "provider-dashboard")
        self.assertEqual(view["status"], "fresh")
        self.assertEqual(
            diagnostics["codex"]["selected_source"], "provider-dashboard"
        )
        self.assertEqual(
            diagnostics["codex"]["pace_evidence_from"],
            "codex-rollout-rate-limits",
        )
        pace = view["pace"]
        self.assertEqual(pace["window"], "weekly")
        self.assertEqual(pace["source"], "codex-rollout-rate-limits")
        self.assertAlmostEqual(pace["tempo"], 0.5 / 0.39, delta=0.02)
        self.assertGreater(pace["tempo"], 1.25)
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        execution = routing_v2.resolve_execution_profile(
            "codex",
            profiles["codex"],
            complexity="medium",
            mode="implement",
            required_tools=["write"],
        )
        assessment = json.loads(
            (fleet.SKILL_ROOT / "references" / "task-profile-v2-example.json").read_text(
                encoding="utf-8"
            )
        )
        _, _, penalties, _ = routing_v2.assignment_score(
            capability_fit=90.0,
            execution=execution,
            assessment=assessment,
            quota={"status": "fresh", "headroom": 0.5},
            underuse_score=50.0,
            local={"sample_count": 0, "active": False},
            pace=pace,
        )
        self.assertIn("pace_overburn", penalties)
        self.assertGreater(penalties["pace_overburn"], 0.0)


if __name__ == "__main__":
    unittest.main()
