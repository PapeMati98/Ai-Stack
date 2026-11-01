"""Schedule (DST Warsaw), dedup and scope tests for the helpdesk workflow.

Run from automation/:  python -m unittest discover -s tests
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from openclaw_ops import helpdesk as helpdesk_module, sanitize
from openclaw_ops.helpdesk import (
    Finding,
    HelpdeskStore,
    is_due,
    process_day,
    schedule_key,
    warsaw_local,
)


def utc_ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute,
                    tzinfo=timezone.utc).timestamp()


class ScheduleTests(unittest.TestCase):
    def test_warsaw_dst_offsets(self):
        # CEST (+2) in summer, CET (+1) in winter, via either time source.
        from openclaw_ops.helpdesk import warsaw_offset_seconds
        self.assertEqual(
            warsaw_offset_seconds(utc_ts(2026, 7, 1, 12)), 7200)
        self.assertEqual(
            warsaw_offset_seconds(utc_ts(2026, 1, 15, 12)), 3600)

    def test_due_at_17_warsaw_across_dst(self):
        # Summer (CEST): 17:00 local == 15:00Z.
        self.assertFalse(is_due(utc_ts(2026, 7, 1, 14, 59)))
        self.assertTrue(is_due(utc_ts(2026, 7, 1, 15, 0)))
        # Winter (CET): 17:00 local == 16:00Z.
        self.assertFalse(is_due(utc_ts(2026, 1, 15, 15, 59)))
        self.assertTrue(is_due(utc_ts(2026, 1, 15, 16, 0)))
        # DST transition Sundays still resolve to a local date.
        self.assertEqual(warsaw_local(utc_ts(2026, 3, 29, 12)).date()
                         .isoformat(), "2026-03-29")
        self.assertEqual(warsaw_local(utc_ts(2026, 10, 25, 12)).date()
                         .isoformat(), "2026-10-25")

    def test_schedule_key_unique_per_day_and_generation(self):
        self.assertNotEqual(
            schedule_key("example-crm", "2026-09-18", 1),
            schedule_key("example-crm", "2026-09-18", 2))
        self.assertNotEqual(
            schedule_key("example-crm", "2026-09-18", 1),
            schedule_key("example-workspace", "2026-09-18", 1))


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HelpdeskStore(os.path.join(self.tmp.name, "hd.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def finding(self, stable="INC-1", rev="r3", scope="candidate-code",
                project="example-crm"):
        return Finding(project=project, stable_id=stable, revision=rev,
                       kind="internal",
                       summary="prod checkout latency p99 above SLO",
                       proposed_scope=scope)

    def test_daily_run_dedup_is_quiet(self):
        now = utc_ts(2026, 7, 1, 15, 30)
        first = process_day(self.store, "example-crm", 1,
                            [self.finding()], now)
        self.assertEqual(first["action"], "proposed")
        self.assertEqual(first["new"], 1)
        second = process_day(self.store, "example-crm", 1,
                             [self.finding()], now + 3600)
        self.assertEqual(second["action"], "quiet-duplicate-run")

    def test_repeated_findings_stay_quiet_new_revision_proposes(self):
        day1 = utc_ts(2026, 7, 1, 15, 30)
        day2 = utc_ts(2026, 7, 2, 15, 30)
        process_day(self.store, "example-crm", 1, [self.finding()], day1)
        again = process_day(self.store, "example-crm", 1,
                            [self.finding()], day2)
        self.assertEqual(again["action"], "quiet-no-new-items")
        self.assertEqual(again["repeated"], 1)
        updated = process_day(self.store, "example-crm", 2,
                              [self.finding(rev="r4")], day2 + 3600)
        self.assertEqual(updated["new"], 1)
        reqs = self.store.list_requests(status="proposed")
        self.assertEqual(len(reqs), 2)

    def test_forbidden_scope_recorded_never_dispatched(self):
        now = utc_ts(2026, 7, 1, 15, 30)
        out = process_day(self.store, "example-workspace", 1,
                          [self.finding(project="example-workspace",
                                        scope="deploy")], now)
        self.assertEqual(out["forbidden"], 1)
        self.assertEqual(out["new"], 0)
        bad = self.store.list_requests(
            status="forbidden-needs-explicit-scope")
        self.assertEqual(len(bad), 1)
        self.assertEqual(self.store.outbox.pending(), [])

    def test_new_proposal_enqueues_owner_notification(self):
        now = utc_ts(2026, 7, 1, 15, 30)
        process_day(self.store, "example-crm", 1, [self.finding()], now)
        pend = self.store.outbox.pending()
        self.assertEqual(len(pend), 1)
        self.assertTrue(pend[0]["key"].startswith("helpdesk:RR-"))

    def test_unknown_project_rejected(self):
        with self.assertRaises(ValueError):
            process_day(self.store, "unknown-proj", 1, [], utc_ts(
                2026, 7, 1, 15, 30))

    def test_quiet_when_no_findings(self):
        out = process_day(self.store, "example-crm", 1, [],
                          utc_ts(2026, 7, 1, 15, 30))
        self.assertEqual(out["action"], "quiet-no-new-items")

    def test_not_due_before_17(self):
        out = process_day(self.store, "example-crm", 1, [self.finding()],
                          utc_ts(2026, 7, 1, 14, 0))
        self.assertEqual(out["action"], "not-due")
        self.assertFalse(self.store.has_run(out["key"]))

    def test_failed_run_rolls_back_sighting_request_outbox_and_marker(self):
        now = utc_ts(2026, 7, 1, 15, 30)

        def fail_after_outbox(point):
            if point == "after-outbox":
                raise RuntimeError("simulated helpdesk crash")

        with self.assertRaises(RuntimeError):
            process_day(self.store, "example-crm", 1, [self.finding()], now,
                        failure_injector=fail_after_outbox)
        key = schedule_key("example-crm", "2026-07-01", 1)
        self.assertFalse(self.store.has_run(key))
        self.assertEqual(
            self.store.conn.execute("SELECT COUNT(*) FROM sightings")
            .fetchone()[0], 0)
        self.assertEqual(self.store.list_requests(), [])
        self.assertEqual(self.store.outbox.pending(), [])

        recovered = process_day(self.store, "example-crm", 1,
                                [self.finding()], now)
        self.assertEqual(recovered["new"], 1)
        self.assertTrue(self.store.has_run(key))
        self.assertEqual(len(self.store.list_requests("proposed")), 1)
        self.assertEqual(len(self.store.outbox.pending()), 1)

    def test_two_workers_commit_one_daily_run(self):
        """BEGIN IMMEDIATE serialises the run and prevents duplicate requests."""
        path = os.path.join(self.tmp.name, "concurrent.db")
        now = utc_ts(2026, 7, 1, 15, 30)
        entered = threading.Event()
        release = threading.Event()
        results = []
        errors = []

        def hold_transaction(point):
            if point == "after-sighting":
                entered.set()
                release.wait(2)

        def worker(hook=None):
            # sqlite connections stay in the thread that owns them on
            # purpose; the database file is the shared authority boundary.
            store = HelpdeskStore(path)
            try:
                results.append(process_day(
                    store, "example-crm", 1, [self.finding()], now,
                    failure_injector=hook))
            except BaseException as exc:  # surfaced below, never swallowed
                errors.append(exc)
            finally:
                store.close()

        first = threading.Thread(target=worker, args=(hold_transaction,))
        second = threading.Thread(target=worker)
        first.start()
        self.assertTrue(entered.wait(2))
        second.start()
        try:
            release.set()
            first.join(2)
            second.join(2)
        finally:
            release.set()
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(
            sorted(r["action"] for r in results),
            ["proposed", "quiet-duplicate-run"])
        observer = HelpdeskStore(path)
        self.assertEqual(
            observer.conn.execute("SELECT COUNT(*) FROM sightings")
            .fetchone()[0], 1)
        self.assertEqual(len(observer.list_requests("proposed")), 1)
        self.assertEqual(len(observer.outbox.pending()), 1)
        observer.close()


class SanitizeTests(unittest.TestCase):
    def test_secrets_redacted_before_admission(self):
        raw = "latency high; api_key=SECRET123 token ok"
        admitted = sanitize.admit_summary(raw, "internal")
        self.assertNotIn("SECRET123", admitted)
        self.assertIn(sanitize.REDACTED, admitted)

    def test_oversize_claim_rejected_not_truncated(self):
        with self.assertRaises(sanitize.TruncationRejected):
            sanitize.admit_summary("x" * 2000, "public")
        with self.assertRaises(sanitize.TruncationRejected):
            sanitize.bound_synthesis("y" * 7000)

    def test_non_terminal_kind_rejected(self):
        with self.assertRaises(ValueError):
            sanitize.admit_summary("draft reasoning trace", "draft")

    def test_warsaw_fallback_keeps_dst_offset(self):
        original = helpdesk_module._WARSAW
        helpdesk_module._WARSAW = None
        try:
            summer = warsaw_local(utc_ts(2026, 7, 1, 12))
            winter = warsaw_local(utc_ts(2026, 1, 15, 12))
            self.assertEqual(summer.utcoffset().total_seconds(), 7200)
            self.assertEqual(winter.utcoffset().total_seconds(), 3600)
        finally:
            helpdesk_module._WARSAW = original


if __name__ == "__main__":
    unittest.main()
