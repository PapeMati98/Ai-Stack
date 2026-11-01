"""Real SQLite/restart tests. All task-owned fixtures remain on disk."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import unittest
import uuid


WORKSPACE = Path(__file__).resolve().parents[3]
FARM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FARM))
from dots_farm.store import Store


def dot_config(**overrides):
    config = {"kind": "observer", "org": "org-a", "project": "pilot",
              "budget_micro_usd": 100, "allowed_actions": ["alert", "collect"],
              "allowed_peers": []}
    config.update(overrides)
    return config


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = Path(os.environ.get("DOTS_TEST_ROOT", str(FARM / ".test-runs")))
        if not base.is_absolute():
            base = WORKSPACE / base
        base = base.resolve()
        if not base.is_relative_to(WORKSPACE):
            raise ValueError("DOTS_TEST_ROOT must be within the task workspace")
        cls.fixture_root = base / ("store-" + uuid.uuid4().hex)
        cls.fixture_root.mkdir(parents=True, exist_ok=False)

    def setUp(self):
        self.fixture = self.fixture_root / (self._testMethodName + "-" + uuid.uuid4().hex)
        self.fixture.mkdir()
        self.path = self.fixture / "state.sqlite3"
        self.store = Store(self.path)
        self.store.configure_budget(100, period_start=1000, period_seconds=100)
        self.store.create_dot("observer", dot_config())
        self.store.create_dot("collector", dot_config(kind="collector"))

    def reserve(self, name, amount, **kwargs):
        kwargs.setdefault("now", 1001)
        return self.store.reserve("observer", name, amount, **kwargs)

    def intent(self, effect_id="effect-one", **kwargs):
        values = {"action": "alert", "target_ref": "synthetic-target",
                  "payload": {"org": "org-a", "project": "pilot", "content": "private-fixture"},
                  "grant_version": 1}
        values.update(kwargs)
        return self.store.intent("observer", effect_id, **values)

    def test_defaults_and_reopen_preserve_config_and_stop(self):
        config = self.store.get_dot("observer")["config"]
        self.assertEqual(config["model"], "synthetic")
        self.assertEqual(config["interval_seconds"], 900)
        self.assertEqual(config["grant_version"], 1)
        self.assertEqual(config["max_queue"], 100)
        self.assertEqual(config["max_payload_bytes"], 16384)
        self.store.pause("observer")
        reopened = Store(self.path)
        self.assertTrue(reopened.get_dot("observer")["paused"])
        self.assertEqual(reopened.get_dot("observer")["config"], config)
        self.assertEqual([dot["id"] for dot in reopened.list_dots()], ["collector", "observer"])
        self.assertEqual(reopened.budget_status()["cap"], 100)

    def test_initial_budget_zero_and_sqlite_durability_settings(self):
        path = self.fixture / "zero.sqlite3"
        store = Store(path)
        store.create_dot("zero", dot_config())
        self.assertEqual(store.budget_status()["cap"], 0)
        with self.assertRaisesRegex(ValueError, "budget_exhausted"):
            store.reserve("zero", "spend", 1)
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with store._connection() as conn:
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_duplicate_dot_never_replaces_memory_or_grants(self):
        self.store.remember("observer", "fact", "original observation", "fixture", 1000)
        self.store.pause("observer")
        with self.assertRaisesRegex(ValueError, "dot_exists"):
            self.store.create_dot("observer", dot_config(allowed_actions=["collect"]))
        self.assertTrue(self.store.get_dot("observer")["paused"])
        self.assertEqual(self.store.retrieve("observer", "original")[0]["revision"], 1)
        self.assertEqual(self.store.get_dot("observer")["config"]["allowed_actions"], ["alert", "collect"])

    def test_config_bounds_and_types_rejected_without_payload_in_errors(self):
        invalid = [
            {"org": "foreign/org"}, {"project": ""}, {"kind": "shell"},
            {"budget_micro_usd": True}, {"budget_micro_usd": -1},
            {"budget_micro_usd": 1.5}, {"budget_micro_usd": 1 << 63},
            {"interval_seconds": 0}, {"interval_seconds": float("inf")},
            {"interval_seconds": True}, {"grant_version": False},
            {"max_queue": 0}, {"max_queue": 10001}, {"max_payload_bytes": 0},
            {"max_payload_bytes": 1048577}, {"allowed_actions": "alert"},
            {"allowed_actions": ["alert", "alert"]}, {"allowed_peers": ["../peer"]},
            {"extra": float("nan")}, {"extra": {1: "synthetic-sensitive-marker"}},
            {"extra": ("tuple",)},
        ]
        for index, override in enumerate(invalid):
            with self.subTest(index=index):
                with self.assertRaises(ValueError) as error:
                    self.store.create_dot("invalid-" + str(index), dot_config(**override))
                self.assertNotIn("synthetic-sensitive-marker", str(error.exception))
        self.assertEqual(len(self.store.list_dots()), 2)

    def test_cross_org_peer_references_rejected_in_both_creation_orders(self):
        self.store.create_dot("foreign", dot_config(org="org-b"))
        with self.assertRaises(PermissionError):
            self.store.create_dot("bad-peer", dot_config(allowed_peers=["foreign"]))
        self.store.create_dot("future-sender", dot_config(allowed_peers=["future-peer"]))
        with self.assertRaises(PermissionError):
            self.store.create_dot("future-peer", dot_config(org="org-b"))
        self.store.create_dot("future-peer", dot_config())

    def test_coverage_failure_does_not_advance_success(self):
        self.store.update_coverage("observer", "ok", 1000, success=True)
        for index, status in enumerate(("failed", "inconclusive", "pending"), start=1):
            self.store.update_coverage("observer", status, 1000 + index)
            dot = Store(self.path).get_dot("observer")
            self.assertEqual(dot["last_success"], 1000)
            self.assertEqual(dot["last_attempt"], 1000 + index)
            self.assertEqual(dot["coverage_status"], status)
        self.store.update_coverage("observer", "empty", 1005, success=True)
        self.assertEqual(self.store.get_dot("observer")["last_success"], 1005)
        with self.assertRaises(ValueError):
            self.store.update_coverage("observer", "failed", 1006, success=True)
        with self.assertRaises(ValueError):
            self.store.update_coverage("observer", "ok", float("nan"))
        with self.assertRaises(ValueError):
            self.store.update_coverage("observer", "ok", 1004)

    def test_lease_fencing_expiry_restart_and_stale_release(self):
        token = self.store.claim("observer", "worker-a", 1000, 10)
        self.assertIsInstance(token, int)
        self.assertIsNone(self.store.claim("observer", "worker-b", 1009, 10))
        self.store.assert_lease("observer", "worker-a", token, 1009)
        with self.assertRaises(PermissionError):
            self.store.assert_lease("observer", "worker-a", token, 1010)
        reopened = Store(self.path)
        next_token = reopened.claim("observer", "worker-b", 1010, 10)
        self.assertGreater(next_token, token)
        self.assertFalse(reopened.release("observer", "worker-a", token))
        with self.assertRaises(PermissionError):
            self.store.assert_lease("observer", "worker-a", token, 1011)
        reopened.assert_lease("observer", "worker-b", next_token, 1011)
        self.assertTrue(reopened.release("observer", "worker-b", next_token))
        self.assertGreater(reopened.claim("observer", "worker-b", 1011, 10), next_token)

    def test_lease_renewal_is_fenced_and_never_shortens(self):
        token = self.store.claim("observer", "worker-a", 1000, 10)
        self.assertTrue(self.store.renew("observer", "worker-a", token, 1005, 10))
        # A backwards wall-clock sample cannot shorten the current live lease.
        self.assertTrue(self.store.renew("observer", "worker-a", token, 1001, 1))
        self.store.assert_lease("observer", "worker-a", token, 1014)
        with self.assertRaises(PermissionError):
            self.store.renew("observer", "worker-a", token, 1015, 10)
        replacement = self.store.claim("observer", "worker-b", 1015, 10)
        self.assertGreater(replacement, token)
        with self.assertRaises(PermissionError):
            self.store.renew("observer", "worker-a", token, 1016, 10)
        with self.assertRaises(PermissionError):
            self.store.renew("observer", "worker-b", token, 1016, 10)

    def test_pause_resume_never_revives_pre_stop_writer(self):
        token = self.store.claim("observer", "worker-a", 1000, 100)
        self.store.pause("observer")
        self.assertIsNone(self.store.claim("observer", "worker-b", 1001, 100))
        with self.assertRaises(PermissionError):
            self.store.assert_lease("observer", "worker-a", token, 1001)
        Store(self.path).pause("observer", False)
        with self.assertRaises(PermissionError):
            self.store.assert_lease("observer", "worker-a", token, 1002)
        self.assertGreater(self.store.claim("observer", "worker-b", 1002, 100), token)

    def test_concurrent_claims_have_exactly_one_fenced_writer(self):
        barrier = threading.Barrier(8)

        def compete(index):
            barrier.wait(timeout=10)
            return self.store.claim("observer", "worker-" + str(index), 1000, 60)

        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(compete, range(8)))
        self.assertEqual(sum(token is not None for token in tokens), 1)

    def test_invalid_lease_values_are_rejected(self):
        for value in (0, -1, True, float("nan"), float("inf")):
            with self.subTest(ttl=value), self.assertRaises(ValueError):
                self.store.claim("observer", "worker", 1000, value)
        with self.assertRaises(ValueError):
            self.store.claim("observer", "worker", 1e308, 1e308)
        with self.assertRaises(ValueError):
            self.store.assert_lease("observer", "worker", True, 1000)

    def test_atomic_concurrent_global_and_per_dot_caps(self):
        for global_cap, caps in ((110, (60, 60)), (200, (20, 30))):
            with self.subTest(global_cap=global_cap):
                store = Store(self.fixture / ("concurrent-" + str(global_cap) + ".sqlite3"))
                store.configure_budget(global_cap, period_start=1000)
                for index, cap in enumerate(caps):
                    store.create_dot("dot-" + str(index), dot_config(budget_micro_usd=cap))
                barrier = threading.Barrier(8)

                def spend(index):
                    barrier.wait(timeout=10)
                    accepted = 0
                    for attempt in range(8):
                        dot_id = "dot-" + str(index % 2)
                        try:
                            store.reserve(dot_id, "r-" + str(index) + "-" + str(attempt), 10,
                                          now=1001)
                            accepted += 1
                        except ValueError as error:
                            self.assertEqual(str(error), "budget_exhausted")
                        global_status = store.budget_status()
                        own_status = store.budget_status(dot_id)
                        self.assertLessEqual(global_status["spent"] + global_status["held"], global_cap)
                        self.assertLessEqual(own_status["spent"] + own_status["held"], own_status["cap"])
                    return accepted

                with ThreadPoolExecutor(max_workers=8) as pool:
                    accepted = sum(pool.map(spend, range(8)))
                self.assertEqual(accepted * 10, min(global_cap, sum(caps)))
                status = Store(store.path).budget_status()
                self.assertEqual(status["held"], accepted * 10)
                self.assertEqual(status["spent"], 0)
                with sqlite3.connect(store.path) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], accepted)
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_concurrent_duplicate_reservations_and_settlement_count_once(self):
        barrier = threading.Barrier(8)

        def duplicate(index):
            barrier.wait(timeout=10)
            return self.reserve("same-reservation", 10)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(duplicate, range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.store.budget_status()["held"], 10)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.store.settle("same-reservation", 3), range(8)))
        self.assertEqual(Store(self.path).budget_status()["spent"], 3)
        self.assertEqual(self.store.budget_status()["held"], 0)

    def test_emergency_partition_is_within_cap_and_not_double_withheld(self):
        self.store.configure_budget(100, emergency_reserve=20, period_seconds=100)
        self.reserve("ordinary", 80)
        with self.assertRaisesRegex(ValueError, "budget_exhausted"):
            self.store.reserve("collector", "ordinary-over", 1, now=1001)
        self.store.reserve("collector", "emergency", 20, emergency=True, now=1001)
        with self.assertRaisesRegex(ValueError, "budget_exhausted"):
            self.store.reserve("collector", "emergency-over", 1, emergency=True, now=1001)
        self.store.settle("ordinary", 60)
        self.store.settle("emergency", 20)
        self.reserve("ordinary-remaining", 20)
        status = Store(self.path).budget_status()
        self.assertEqual((status["spent"], status["held"], status["remaining"]), (80, 20, 0))
        self.assertEqual(status["emergency_reserve"], 20)

    def test_reconfiguration_and_restart_keep_spent_and_unknown_holds(self):
        self.reserve("known", 10)
        self.store.settle("known", 4)
        self.reserve("unknown", 20)
        self.store.settle("unknown")
        reopened = Store(self.path)
        reopened.configure_budget(80, emergency_reserve=5, period_seconds=100)
        status = reopened.budget_status()
        self.assertEqual((status["spent"], status["held"], status["remaining"]), (4, 20, 56))
        self.assertEqual(status["period_start"], 1000)
        reopened.settle("unknown")
        self.assertEqual(reopened.budget_status()["held"], 20)

    def test_unknown_can_be_reconciled_once_and_unused_hold_released(self):
        original = self.reserve("uncertain", 20)
        self.store.settle("uncertain")
        self.assertEqual(self.store.budget_status()["held"], 20)
        self.assertEqual(Store(self.path).settle("uncertain", 7)["actual"], 7)
        self.assertEqual(self.store.settle("uncertain", 7)["status"], "settled")
        self.assertEqual(self.store.budget_status()["spent"], 7)
        self.assertEqual(self.store.budget_status()["held"], 0)
        with self.assertRaisesRegex(ValueError, "settlement_conflict"):
            self.store.settle("uncertain", 8)
        with self.assertRaisesRegex(ValueError, "settlement_conflict"):
            self.store.settle("uncertain")
        self.assertEqual(self.reserve("uncertain", original["amount"])["status"], "settled")

    def test_reservation_parameter_conflict_does_not_change_hold(self):
        self.reserve("stable-id", 10)
        for values in (("observer", 11, False), ("observer", 10, True), ("collector", 10, False)):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "reservation_conflict"):
                self.store.reserve(values[0], "stable-id", values[1], emergency=values[2], now=1002)
        self.assertEqual(self.store.budget_status()["held"], 10)

    def test_actual_over_reservation_is_truthful_and_stops_overrun(self):
        self.store.configure_budget(10, period_seconds=100)
        token = self.store.claim("observer", "worker", 1000, 100)
        self.reserve("overrun", 5)
        self.store.settle("overrun", 12)
        status = Store(self.path).budget_status()
        self.assertEqual(status["spent"], 12)
        self.assertEqual(status["remaining"], -2)
        self.assertEqual(status["held"], 0)
        self.assertTrue(status["overrun"])
        self.assertTrue(self.store.get_dot("observer")["paused"])
        self.assertTrue(self.store.get_dot("collector")["paused"])
        with self.assertRaises(PermissionError):
            self.store.assert_lease("observer", "worker", token, 1002)

    def test_per_dot_actual_overrun_stops_only_that_dot(self):
        self.store.create_dot("limited", dot_config(budget_micro_usd=5))
        self.store.reserve("limited", "overrun", 3, now=1001)
        self.store.settle("overrun", 6)
        self.assertTrue(self.store.budget_status("limited")["overrun"])
        self.assertTrue(self.store.get_dot("limited")["paused"])
        self.assertFalse(self.store.get_dot("observer")["paused"])

    def test_rolling_window_does_not_reset_recent_spend_at_cycle_boundary(self):
        self.store.configure_budget(10, period_seconds=100)
        self.reserve("recent-cost", 8, now=1049)
        self.store.settle("recent-cost", 8)
        self.reserve("old-unknown", 2, now=1050)
        self.store.settle("old-unknown")
        with self.assertRaisesRegex(ValueError, "budget_exhausted"):
            self.reserve("boundary-cost", 1, now=1101)
        self.reserve("later-cost", 8, now=1150.01)
        status = Store(self.path).budget_status()
        self.assertEqual(status["spent"], 0)
        self.assertEqual(status["held"], 10)
        self.assertAlmostEqual(status["period_start"], 1050.01)
        # Late reconciliation retains the historical true cost without treating
        # it as a new inference or retaining its already-resolved old hold.
        self.store.settle("old-unknown", 2)
        self.assertEqual(self.store.budget_status()["held"], 8)

    def test_backdated_reservation_cannot_hide_cost_or_reset_budget(self):
        self.reserve("now-cost", 10, now=1040)
        self.store.settle("now-cost", 10)
        self.reserve("backdated-cost", 5, now=0)
        self.store.settle("backdated-cost", 5)
        self.assertEqual(self.store.budget_status()["spent"], 15)
        self.store.configure_budget(100, period_start=1050, period_seconds=100)
        self.assertEqual(self.store.budget_status()["spent"], 15)

    def test_explicit_new_period_keeps_recent_spend_and_all_unresolved_holds(self):
        self.reserve("old-known", 10, now=1001)
        self.store.settle("old-known", 10)
        self.reserve("recent-known", 5, now=1080)
        self.store.settle("recent-known", 5)
        self.reserve("old-unknown", 20, now=1081)
        self.store.settle("old-unknown")
        self.store.configure_budget(100, period_start=1102, period_seconds=100)
        status = Store(self.path).budget_status()
        self.assertEqual((status["spent"], status["held"]), (5, 20))
        self.assertEqual(status["period_start"], 1002)
        self.store.configure_budget(100, period_start=2000, period_seconds=100)
        status = Store(self.path).budget_status()
        self.assertEqual((status["spent"], status["held"]), (0, 20))
        self.assertEqual(status["period_start"], 1900)

    def test_first_configuration_does_not_hide_reported_zero_cap_overrun(self):
        store = Store(self.fixture / "initial-overrun.sqlite3")
        store.create_dot("initial", dot_config())
        store.reserve("initial", "zero-held", 0)
        store.settle("zero-held", 3)
        store.configure_budget(10)
        self.assertEqual(store.budget_status()["spent"], 3)

    def test_budget_invalid_values_and_finite_timestamps(self):
        for value in (-1, True, 1.5, float("nan"), float("inf"), 1 << 63):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.configure_budget(value)
                with self.assertRaises(ValueError):
                    self.reserve("invalid", value)
                with self.assertRaises(ValueError):
                    self.store.settle("missing", value)
        with self.assertRaises(ValueError):
            self.store.configure_budget(10, emergency_reserve=11)
        with self.assertRaises(ValueError):
            self.reserve("invalid", 1, now=True)
        with self.assertRaises(ValueError):
            self.reserve("invalid", 1, now=float("inf"))
        with self.assertRaises(ValueError):
            self.reserve("invalid", 1, emergency=1)
        with self.assertRaises(ValueError):
            self.store.configure_budget(10, period_seconds=0)

    def test_memory_is_private_relevant_bounded_and_survives_restart(self):
        self.store.remember("observer", "shared-id", "alpha threshold note", "fixture-one", 1000)
        self.store.remember("observer", "irrelevant", "unrelated delta note", "fixture-two", 1001)
        self.store.remember("observer", "related", "alpha newer note", "fixture-three", 1002)
        self.store.remember("collector", "shared-id", "alpha private collector", "fixture-other", 1003)
        reopened = Store(self.path)
        records = reopened.retrieve("observer", "ALPHA threshold", limit=1)
        self.assertEqual([record["id"] for record in records], ["shared-id"])
        self.assertEqual(records[0]["trust"], "untrusted")
        self.assertEqual(records[0]["source_ref"], "fixture-one")
        self.assertEqual(records[0]["observed_at"], 1000)
        self.assertEqual(records[0]["revision"], 1)
        self.assertEqual({record["dot_id"] for record in reopened.retrieve("observer", "alpha")}, {"observer"})
        self.assertEqual(reopened.retrieve("observer", "collector"), [])
        self.assertEqual(reopened.retrieve("observer", ""), [])
        small = reopened.retrieve("observer", "alpha", max_bytes=2)
        self.assertEqual(small, [])
        bounded = reopened.retrieve("observer", "alpha", max_bytes=220)
        encoded = json.dumps(bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertLessEqual(len(encoded.encode("utf-8")), 220)

    def test_owner_correction_invalidates_old_retrieval_and_keeps_grants(self):
        before = self.store.get_dot("observer")["config"]
        self.store.remember("observer", "rule", "obsolete authorization alpha", "untrusted-mail", 1000)
        correction = self.store.correct("observer", "rule", "corrected instruction beta")
        self.assertEqual(correction["revision"], 2)
        self.assertEqual(correction["source_ref"], "owner")
        reopened = Store(self.path)
        self.assertEqual(reopened.retrieve("observer", "obsolete alpha"), [])
        self.assertEqual(reopened.retrieve("observer", "corrected beta")[0]["revision"], 2)
        self.assertEqual(reopened.get_dot("observer")["config"], before)
        with self.assertRaises(ValueError):
            reopened.remember("observer", "rule", "obsolete authorization alpha", "untrusted-mail", 1000)

    def test_memory_cross_org_refs_and_other_dot_corrections_denied(self):
        self.store.create_dot("foreign", dot_config(org="org-b"))
        for source in ("foreign", "dot:foreign", "org:org-b"):
            with self.subTest(source=source), self.assertRaises(PermissionError):
                self.store.remember("observer", "record", "bounded text", source, 1000)
        self.store.remember("observer", "only-here", "alpha", "fixture", 1000)
        with self.assertRaises(ValueError):
            self.store.correct("collector", "only-here", "foreign correction")
        self.assertEqual(self.store.retrieve("observer", "alpha")[0]["revision"], 1)

    def test_memory_limits_and_json_safe_errors(self):
        self.store.create_dot("bounded", dot_config(max_queue=1, max_payload_bytes=16))
        self.store.remember("bounded", "one", "small", "fixture", 1000)
        with self.assertRaisesRegex(ValueError, "memory_limit"):
            self.store.remember("bounded", "two", "small", "fixture", 1000)
        with self.assertRaisesRegex(ValueError, "text_limit"):
            self.store.correct("bounded", "one", "x" * 17)
        with self.assertRaises(ValueError):
            self.store.remember("observer", "bad-time", "alpha", "fixture", float("nan"))
        with self.assertRaises(ValueError):
            self.store.retrieve("observer", "alpha", limit=True)
        with self.assertRaises(ValueError):
            self.store.retrieve("observer", "alpha", max_bytes=0)
        with self.assertRaises(ValueError):
            self.store.remember("observer", "bad-text", "\ud800", "fixture", 1000)
        with self.assertRaises(ValueError):
            self.store.remember("observer", "bad-reference", "alpha", "fixture\nprivate", 1000)

    def test_oversize_retrieval_record_is_skipped_without_unbounded_output(self):
        self.store.create_dot("large-memory", dot_config(max_payload_bytes=1048576))
        self.store.remember("large-memory", "huge", "alpha " + "x" * 1048570, "fixture", 1000)
        self.store.remember("large-memory", "small", "alpha useful note", "fixture", 999)
        records = self.store.retrieve("large-memory", "alpha", max_bytes=512)
        self.assertEqual([record["id"] for record in records], ["small"])
        self.assertEqual(self.store.retrieve("large-memory", "xxxxxxxx"), [])

    def test_over_reservation_within_caps_is_recorded_without_false_stop(self):
        self.reserve("larger-actual", 5)
        self.store.settle("larger-actual", 7)
        self.assertEqual(self.store.budget_status()["spent"], 7)
        self.assertEqual(self.store.budget_status()["held"], 0)
        self.assertFalse(self.store.get_dot("observer")["paused"])

    def test_seen_is_idempotent_and_dot_bound_after_reopen(self):
        self.assertFalse(self.store.seen("observer", "same-item"))
        self.store.mark_seen("observer", "same-item")
        self.store.mark_seen("observer", "same-item")
        reopened = Store(self.path)
        self.assertTrue(reopened.seen("observer", "same-item"))
        self.assertFalse(reopened.seen("collector", "same-item"))

    def test_intent_commits_before_independent_process_effect_boundary(self):
        # The child is a local read-only verifier, not a model, CLI orchestrator,
        # API or target. CREATE_NO_WINDOW applies to this process on Windows.
        self.intent()
        code = ("import json,sqlite3,sys; "
                "c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); "
                "r=c.execute('SELECT status FROM effects WHERE dot_id=? AND id=?',"
                "('observer','effect-one')).fetchone(); c.close(); "
                "print(json.dumps({'committed':r==('pending',)}))")
        result = subprocess.run([sys.executable, "-B", "-c", code, str(self.path)],
                                cwd=WORKSPACE, capture_output=True, text=True,
                                timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, "local verifier failed")
        self.assertEqual(json.loads(result.stdout), {"committed": True})
        self.assertEqual(Store(self.path).effects("observer")[0]["status"], "pending")

    def test_intent_idempotency_conflict_and_namespace_isolation(self):
        original = self.intent()
        self.assertEqual(self.intent(), original)
        self.assertEqual(len(self.store.effects("observer")), 1)
        self.assertEqual(self.store.effects("collector"), [])
        with self.assertRaisesRegex(ValueError, "intent_conflict"):
            self.intent(payload={"org": "org-a", "content": "different"})
        with self.assertRaises(ValueError):
            self.store.effect_status("collector", "effect-one", "confirmed")
        self.store.intent("collector", "effect-one", "collect", "synthetic-target", {}, 1)
        self.assertEqual(len(self.store.effects("collector")), 1)

    def test_effect_status_monotonic_receipt_private_and_retries_persist(self):
        self.intent()
        self.store.effect_status("observer", "effect-one", "unknown")
        reopened = Store(self.path)
        self.assertEqual(reopened.effects("observer", "unknown")[0]["status"], "unknown")
        with self.assertRaisesRegex(ValueError, "effect_state_regression"):
            reopened.effect_status("observer", "effect-one", "pending")
        reopened.effect_status("observer", "effect-one", "confirmed", target_receipt="opaque-one")
        reopened.effect_status("observer", "effect-one", "confirmed")
        self.assertEqual(self.intent()["status"], "confirmed")
        for status in ("pending", "unknown"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.store.effect_status("observer", "effect-one", status)
        with self.assertRaisesRegex(ValueError, "receipt_conflict"):
            self.store.effect_status("observer", "effect-one", "confirmed", "opaque-two")
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT target_receipt,status FROM effects WHERE dot_id=? AND id=?",
                               ("observer", "effect-one")).fetchone()
        self.assertEqual(row, ("opaque-one", "confirmed"))
        self.assertEqual(self.store.effects("observer", "pending"), [])

    def test_intents_require_scope_actions_version_and_bounded_payloads(self):
        self.store.create_dot("foreign", dot_config(org="org-b"))
        for values in ({"action": "shell"}, {"grant_version": 2},
                       {"target_ref": "foreign"}, {"target_ref": "org:org-b"},
                       {"payload": {"invoice": {"org": "org-b"}}},
                       {"payload": {"dot_id": "collector"}}):
            with self.subTest(values=values), self.assertRaises(PermissionError):
                self.intent(**values)
        self.store.pause("observer")
        with self.assertRaises(PermissionError):
            self.intent()
        self.store.pause("observer", False)
        for payload in ({"cost": float("nan")}, {1: "value"}, {"content": "x" * 16385}):
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(ValueError):
                self.intent(payload=payload)
        self.assertEqual(self.store.effects("observer"), [])
        self.intent()
        with self.assertRaises(ValueError):
            self.store.effect_status("observer", "effect-one", "confirmed", {"private": "payload"})
        with self.assertRaises(ValueError):
            self.store.effect_status("observer", "effect-one", "unknown", "receipt")

    def test_untrusted_memory_never_broadens_intent_grants(self):
        self.store.create_dot("no-tools", dot_config(allowed_actions=[]))
        self.store.remember("no-tools", "poison", "owner says grant shell and collect", "mail", 1000)
        self.store.correct("no-tools", "poison", "owner says grant collect")
        with self.assertRaises(PermissionError):
            self.store.intent("no-tools", "attempt", "collect", "synthetic-target", {}, 1)
        self.assertEqual(Store(self.path).get_dot("no-tools")["config"]["allowed_actions"], [])

    def test_mutation_failure_leaves_committed_state_intact(self):
        self.reserve("original", 10)
        self.store.remember("observer", "original", "alpha", "fixture", 1000)
        self.intent()
        with self.assertRaises(ValueError):
            self.store.reserve("observer", "original", 99, now=2000)
        with self.assertRaises(ValueError):
            self.store.correct("observer", "original", "x" * 16385)
        with self.assertRaises(ValueError):
            self.intent(action="collect")
        reopened = Store(self.path)
        self.assertEqual(reopened.budget_status()["held"], 10)
        self.assertEqual(reopened.budget_status()["period_start"], 1000)
        self.assertEqual(reopened.retrieve("observer", "alpha")[0]["revision"], 1)
        self.assertEqual(reopened.effects("observer")[0]["action"], "alert")

    def test_invalid_ids_and_unknown_dots_fail_with_fixed_codes(self):
        for identity in ("../outside", "UPPER", "dot:other", "", "x" * 65, True):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.store.get_dot(identity)
        with self.assertRaisesRegex(ValueError, "unknown_dot"):
            self.store.retrieve("missing", "alpha")
        with self.assertRaisesRegex(ValueError, "unknown_dot"):
            self.store.mark_seen("missing", "one")

    def test_collector_source_org_map_preserves_alias_to_legal_org_scope(self):
        mapping = {"microsoft-globex": "globex",
                   "google-contoso": "contoso"}
        self.store.create_dot("finance", dot_config(
            kind="collector", org="globex", source_org_map=mapping))
        invoice = {"org": "contoso", "number": "FLEXI-1",
                   "date": "2026-10-08", "amount": "42.00", "currency": "PLN"}
        payload = {"dot_id": "finance", "org": "contoso", "project": "pilot",
                   "source_alias": "google-contoso", "invoice": invoice,
                   "draft": True}
        self.store.intent("finance", "finance-effect", "collect", "approval-queue",
                          payload, 1)
        for alias, org in (("microsoft-globex", "contoso"),
                           ("google-contoso", "globex")):
            with self.subTest(alias=alias), self.assertRaises(PermissionError):
                bad_invoice = dict(invoice, org=org)
                bad = dict(payload, org=org, source_alias=alias, invoice=bad_invoice)
                self.store.intent("finance", "bad-" + alias, "collect",
                                  "approval-queue", bad, 1)
        with self.assertRaises(ValueError):
            self.store.create_dot("bad-map", dot_config(
                kind="collector", org="globex",
                source_org_map={"google-contoso": "globex"}))

    def test_calendar_week_budget_resets_on_monday_utc(self):
        monday = datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()
        store = Store(self.fixture / "monday-budget.sqlite3")
        store.configure_budget(100, period_start=monday + 60,
                               period_seconds=7 * 86400,
                               period_mode="calendar_week_monday_utc")
        store.create_dot("weekly", dot_config(org="weekly", budget_micro_usd=100))
        store.reserve("weekly", "prior-week", 80, now=monday + 3600)
        store.settle("prior-week", 80)
        with self.assertRaisesRegex(ValueError, "budget_exhausted"):
            store.reserve("weekly", "same-week", 21, now=monday + 6 * 86400)
        store.reserve("weekly", "next-week", 100, now=monday + 7 * 86400)
        status = store.budget_status("weekly")
        self.assertEqual(status["period_start"], monday + 7 * 86400)
        self.assertEqual(status["period_mode"], "calendar_week_monday_utc")
        self.assertEqual((status["spent"], status["held"]), (0, 100))

    def test_external_canary_spend_seed_is_idempotent_and_not_backdatable(self):
        self.store.create_dot("seeded", dot_config(org="seeded"))
        result = self.store.record_external_spend("seeded", "seed-canary", 21)
        self.assertEqual(result["actual"], 21)
        self.assertEqual(self.store.budget_status("seeded")["spent"], 21)
        self.assertEqual(self.store.record_external_spend(
            "seeded", "seed-canary", 21), result)
        with self.assertRaisesRegex(ValueError, "seed_spend_conflict"):
            self.store.record_external_spend("seeded", "seed-canary", 22)

    def test_external_unknown_request_hold_is_idempotent_and_reconcilable(self):
        store = Store(self.fixture / "unknown-hold.sqlite3")
        store.configure_budget(100)
        store.create_dot("held", dot_config(org="held", budget_micro_usd=100))
        first = store.record_external_hold("held", "canary-unknown-1", 50)
        second = store.record_external_hold("held", "canary-unknown-1", 50)
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(second["status"], "unknown")
        self.assertEqual(store.budget_status("held")["held"], 50)
        with self.assertRaisesRegex(ValueError, "seed_hold_conflict"):
            store.record_external_hold("held", "canary-unknown-1", 51)
        store.settle("canary-unknown-1", 20)
        self.assertEqual(store.record_external_hold("held", "canary-unknown-1", 50)["actual"], 20)
        self.assertEqual(store.budget_status("held")["spent"], 20)


if __name__ == "__main__":
    unittest.main()
