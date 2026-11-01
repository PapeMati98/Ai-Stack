"""Synthetic, clock-controlled contract tests for the stateless pool brake."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import adaptive
import fleet
import quota_pace
import routing_v2


UTC = dt.timezone.utc
POOL = "xiaomi-mimo-token-plan-standard"
RESET = "2026-10-22T23:59:59Z"


def reading(when: str, remaining: float, *, reset: str = RESET) -> dict:
    return {
        "agent": "mimo", "quota_pool_id": POOL, "observed_at": when,
        "source": "provider-dashboard", "confidence": 1.0,
        "valid_for_hours": 48, "monthly_remaining_fraction": remaining,
        "monthly_reset_at": reset,
    }


def run(when: str, tokens: int, *, agent: str = "mimo") -> dict:
    return {"agent": agent, "completed_at": when, "token_usage": {"total_tokens": tokens}}


class PaceBrakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime(2026, 9, 24, 20, tzinfo=UTC)
        self.rows = [
            reading("2026-09-23T22:54:00Z", 0.751),
            reading("2026-09-24T08:20:00Z", 0.574),
            reading("2026-09-24T19:57:00Z", 0.37),
        ]
        self.runs = [
            run("2026-09-24T02:00:00Z", 6_000_000),
            run("2026-09-24T12:00:00Z", 6_000_000),
            run("2026-09-24T18:00:00Z", 6_000_000),
            run("2026-09-24T19:59:00Z", 500_000),
        ]
        self.config = {"enabled": True, "burst_factor": 1.5,
                       "window_hours": 24, "max_reading_age_hours": 48}
        self.profiles = {"mimo": {
            "adapter": "mimo", "quota_pool_id": POOL,
            "quota_collection": {"pace_brake": self.config},
        }}

    def brake(self, *, now: dt.datetime | None = None, rows=None, runs=None):
        return quota_pace.calculate_pool_brake(
            POOL, snapshots=self.rows if rows is None else rows,
            runs=self.runs if runs is None else runs,
            pool_ids={"mimo": POOL}, now=now or self.now,
            config=self.config,
        )

    def test_mimo_overburn_has_until_and_releases_itself(self) -> None:
        active = self.brake()
        self.assertTrue(active["active"])
        self.assertGreater(active["consumed_fraction_window"], active["allowed_fraction_window"])
        until = dt.datetime.fromisoformat(active["until"].replace("Z", "+00:00"))
        self.assertGreater(until, self.now)
        self.assertLessEqual(until, self.now + dt.timedelta(hours=24))
        cleared = self.brake(now=until + dt.timedelta(seconds=1))
        self.assertFalse(cleared["active"])
        self.assertIsNone(cleared["until"])

    def test_higher_new_reading_recomputes_without_stored_brake(self) -> None:
        self.assertTrue(self.brake()["active"])
        rows = [*self.rows, reading("2026-09-25T18:05:00Z", 0.95)]
        recent_runs = [*self.runs, run("2026-09-25T18:06:00Z", 100_000)]
        recalculated = self.brake(now=dt.datetime(2026, 9, 25, 18, 10, tzinfo=UTC),
                                  rows=rows, runs=recent_runs)
        self.assertFalse(recalculated["active"])
        self.assertIsNone(recalculated["until"])

    def test_fail_open_when_reading_stale_reset_passed_or_calibration_missing(self) -> None:
        cases = (
            (self.now + dt.timedelta(hours=49), self.rows, self.runs, "stale"),
            (dt.datetime(2026, 10, 23, tzinfo=UTC), self.rows, self.runs, "reset"),
            (self.now, self.rows[-1:], self.runs, "calibration"),
            (self.now, self.rows, [], "tokens"),
        )
        for now, rows, runs, reason in cases:
            with self.subTest(reason=reason):
                brake = self.brake(now=now, rows=rows, runs=runs)
                self.assertFalse(brake["active"])
                self.assertIsNone(brake["until"])
                self.assertIn(reason, brake["reason"])

    def test_current_views_export_brake_and_no_brake_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            root.mkdir()
            before = set(root.rglob("*"))
            with (
                mock.patch.object(adaptive, "latest_quotas", return_value={
                    "mimo": adaptive._quota_view(self.rows[-1], current=self.now),
                }),
                mock.patch.object(adaptive, "read_quota_records", return_value=self.rows),
                mock.patch.object(fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})),
                mock.patch.object(fleet, "read_usage", return_value=self.runs),
            ):
                views, diagnostics = fleet.current_quota_views(root, self.profiles, now=self.now)
            brake = views["mimo"]["pace_brake"]
            exported = json.loads((root / "pace" / "latest.json").read_text(encoding="utf-8"))
            after = {path.relative_to(root).as_posix() for path in root.rglob("*")}
            self.assertEqual(before, set())
            self.assertTrue(brake["active"])
            self.assertEqual(diagnostics[POOL]["pace_brake"], brake)
            self.assertEqual(exported["pools"][POOL]["brake"], brake)
            self.assertEqual(after, {"pace", "pace/latest.json", "pace/latest.lock"})

    def test_current_views_recompute_brake_in_memory(self) -> None:
        with (
            mock.patch.object(adaptive, "latest_quotas", return_value={
                "mimo": adaptive._quota_view(self.rows[-1], current=self.now),
            }),
            mock.patch.object(adaptive, "read_quota_records", return_value=self.rows),
            mock.patch.object(fleet.quota_collectors, "collect_profile_quotas", return_value=({}, {})),
            mock.patch.object(fleet, "read_usage", return_value=self.runs),
            mock.patch.object(fleet, "_write_pace_latest", return_value=True) as export,
        ):
            views, diagnostics = fleet.current_quota_views(
                Path("synthetic-state"), self.profiles, now=self.now)
            brake = views["mimo"]["pace_brake"]
            self.assertEqual(diagnostics[POOL]["pace_brake"], brake)
            until = dt.datetime.fromisoformat(
                brake["until"].replace("Z", "+00:00"))
            refreshed, _ = fleet.current_quota_views(
                Path("synthetic-state"), self.profiles,
                agent_ids=["mimo"], now=until + dt.timedelta(seconds=1))
        self.assertTrue(brake["active"])
        self.assertFalse(refreshed["mimo"]["pace_brake"]["active"])
        self.assertEqual(export.call_args_list[0].args[5][POOL], brake)
        self.assertEqual(export.call_args_list[1].args[5][POOL], refreshed["mimo"]["pace_brake"])

    def test_panel_export_contract_in_memory(self) -> None:
        payloads = []
        brake = self.brake()
        with (
            mock.patch.object(fleet, "is_filesystem_link", return_value=False),
            mock.patch.object(fleet, "ReleasableKernelLock"),
            mock.patch.object(Path, "read_text", side_effect=FileNotFoundError),
            mock.patch.object(adaptive, "atomic_write", side_effect=lambda path, value: payloads.append(value)),
        ):
            self.assertTrue(fleet._write_pace_latest(
                Path("synthetic-state"), {POOL: {"tempo": 10.3}}, {"mimo": POOL},
                self.now, {}, {POOL: brake}))
        self.assertEqual(payloads[0]["pools"][POOL]["brake"], brake)
        self.assertEqual(set(brake), {
            "active", "until", "reason", "allowed_fraction_window",
            "consumed_fraction_window", "window_hours", "evaluated_at",
        })

    def test_disabled_profile_preserves_routing_gates(self) -> None:
        profile = {"quota_collection": {}}
        view = {"status": "fresh", "headroom": 0.37, "confidence": 1.0,
                "pace_brake": {"active": True, "reason": "synthetic", "until": RESET}}
        gates = routing_v2.quota_hard_gates(profile, view)
        self.assertTrue(all(gate["passed"] for gate in gates))
        self.assertNotIn("pace-brake-clear", [gate["code"] for gate in gates])

    def test_active_gate_eliminates_pool_and_next_candidate_is_eligible(self) -> None:
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        profiles = {agent: profiles[agent] for agent in ("mimo", "codex")}
        mimo = profiles["mimo"]
        mimo["quota_collection"]["pace_brake"] = dict(self.config)
        codex = profiles["codex"]
        quota = {"status": "fresh", "headroom": 0.37, "confidence": 1.0,
                 "pace_brake": self.brake()}
        mimo_gates = routing_v2.quota_hard_gates(mimo, quota)
        codex_gates = routing_v2.quota_hard_gates(codex, quota)
        self.assertFalse(next(g for g in mimo_gates if g["code"] == "pace-brake-clear")["passed"])
        self.assertTrue(all(g["passed"] for g in codex_gates))
        assessment = adaptive.normalize_task_profile(adaptive.fallback_assessment(
            kind="implementation", complexity="low", profiles=profiles))
        ranked = fleet.rank_profiles(
            profiles, kind="implementation", complexity="low", counts={},
            availability={"mimo": True, "codex": True}, assessment=assessment,
            quotas={"mimo": quota, "codex": {"status": "fresh", "headroom": 0.8,
                                                "confidence": 1.0}},
        )
        self.assertFalse(next(row for row in ranked if row["agent"] == "mimo")["eligible"])
        self.assertEqual(next(row for row in ranked if row["eligible"])["agent"], "codex")

    def test_send_gate_rejects_before_prompt_materialization_and_override_passes(self) -> None:
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        profiles["mimo"]["quota_collection"]["pace_brake"] = dict(self.config)
        execution = routing_v2.resolve_execution_profile(
            "mimo", profiles["mimo"], complexity="low", mode="review", required_tools=[])
        view = {"status": "fresh", "headroom": 0.37, "confidence": 1.0,
                "pace_brake": self.brake()}
        kwargs = dict(task="PRIVATE TASK", profiles=profiles, agent_id="mimo",
                      mode="review", kind="review", complexity="low", model_override=None,
                      workspace=Path.cwd(), timeout=60, allow_write=False,
                      native_subagent_limit=0, enable_web=False, inherit_api_keys=False,
                      dry_run=False, output_root=Path.cwd(), root=Path.cwd(),
                      execution_profile=execution, knowledge_mode="none",
                      required_inputs_file="synthetic-inputs.json")
        with (
            mock.patch.object(fleet, "resolve_executable", return_value="mimo.exe"),
            mock.patch.object(fleet.mimo_runtime, "require_mimo_native_executable"),
            mock.patch.object(fleet, "version_probe_environment", return_value={}),
            mock.patch.object(fleet, "profile_probe_environment", return_value={}),
            mock.patch.object(fleet, "probe_command", return_value={"launchable": True, "exit_code": 0, "output": "ok"}),
            mock.patch.object(fleet, "profile_credential_status", return_value={"ready": True}),
            mock.patch.object(fleet, "merge_credential_readiness", return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "readiness_from_probes", return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "runtime_model_visible", return_value=(True, "visible")),
            mock.patch.object(fleet, "current_quota_views", return_value=({"mimo": view}, {})),
            mock.patch.dict(os.environ, {"AGENT_FLEET_DEPTH": "0"}),
            mock.patch.object(fleet, "create_run_dir") as create_run,
            mock.patch.object(fleet, "prepare_execution_workspace") as prepare_workspace,
            mock.patch.object(fleet, "load_required_inputs_manifest", side_effect=fleet.FleetError("AFTER_GATE")) as inputs,
        ):
            with self.assertRaisesRegex(fleet.FleetError, "pace-brake-clear.*until="):
                fleet.execute_single(**kwargs)
            inputs.assert_not_called()
            create_run.assert_not_called()
            prepare_workspace.assert_not_called()
            with self.assertRaisesRegex(fleet.FleetError, "AFTER_GATE"):
                fleet.execute_single(**kwargs, pace_brake_override="owner approved this run")
            inputs.assert_called_once()

    def test_explicit_override_needs_reason_and_only_bypasses_brake(self) -> None:
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        profiles["mimo"]["quota_collection"]["pace_brake"] = dict(self.config)
        row = {
            "agent": "mimo", "eligible": False, "launchable": True,
            "reasons": ["gate-failed=pace-brake-clear"],
            "suggested_model": "mimo-v2.6-pro", "gate_results": [
                {"code": "pace-brake-clear", "passed": False},
            ],
        }
        route = {
            "selected_agent": "mimo", "selected_model": "mimo-v2.6-pro",
            "selected_kind": "review", "rankings": [row],
            "quota_snapshots": {"mimo": {"pace_brake": self.brake()}},
        }
        with mock.patch.object(fleet, "route_task", return_value=route):
            with self.assertRaisesRegex(fleet.FleetError, "until="):
                fleet.choose_agent("mimo", "synthetic", profiles=profiles, kind="review",
                                   complexity="low", root=Path.cwd(), cwd=Path.cwd())
            selected, _, _ = fleet.choose_agent(
                "mimo", "synthetic", profiles=profiles, kind="review",
                complexity="low", root=Path.cwd(), cwd=Path.cwd(),
                override_pace_brake=True)
        self.assertEqual(selected, "mimo")
        args = fleet.build_parser().parse_args([
            "run", "--task", "synthetic", "--agent", "mimo",
            "--override-pace-brake", "owner approved",
        ])
        self.assertEqual(args.override_pace_brake, "owner approved")

    def test_pro_plan_pace_is_telemetry_only_but_exhaustion_still_blocks(self) -> None:
        profile = fleet.load_profiles(fleet.DEFAULT_PROFILES)["mimo"]
        self.assertEqual(profile["quota_collection"]["plan_capacity_credits"], 38_000_000_000)
        self.assertFalse(profile["quota_collection"]["pace_brake"]["enabled"])
        view = {"status": "fresh", "headroom": 0.5, "confidence": 1.0,
                "pace_brake": self.brake()}
        self.assertNotIn("pace-brake-clear", [gate["code"] for gate in
                         routing_v2.quota_hard_gates(profile, view)])
        exhausted = dict(view, headroom=0.0)
        self.assertFalse(all(gate["passed"] for gate in
                             routing_v2.quota_hard_gates(profile, exhausted)))


if __name__ == "__main__":
    unittest.main()
