"""T6b scheduling-hardening tests: catch-up, revision digests, durable approvals.

Covers the delegated slice: missed-run/catch-up across reboot (bounded,
exactly-once, DST-aware), deterministic issue revision digests that change
even when the source timestamp does not, bounded collector retry with
exactly-once state-change alarms, and owner-decision repair gating with
durable replay state.

Run from automation/:  python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from openclaw_ops import repairs
from openclaw_ops.approvals import (
    ApprovalStore,
    DurableApprovalStore,
    TelegramMessage,
    canonical_text,
    decide,
)
from openclaw_ops import models
from openclaw_ops.helpdesk import (
    Finding,
    HelpdeskStore,
    claim_schedule,
    derive_revision,
    get_collector_state,
    is_due,
    latest_due_date,
    missed_catchup_date,
    process_catchup,
    process_day,
    read_with_retry,
    record_collector_outcome,
    release_schedule_claim,
    revision_digest,
    schedule_key,
)

T0 = 1_700_000_000.0
ALLOW = {("sender-1", "chat-1")}


def utc_ts(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute,
                    tzinfo=timezone.utc).timestamp()


class CatchupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "hd.db")
        self.store = HelpdeskStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def finding(self, stable="INC-1", rev="r1", scope="candidate-code",
                project="example-crm"):
        return Finding(project=project, stable_id=stable, revision=rev,
                       kind="internal",
                       summary="prod checkout latency p99 above SLO",
                       proposed_scope=scope)

    def run_keys(self):
        rows = self.store.conn.execute("SELECT key FROM runs").fetchall()
        return sorted(r[0] for r in rows)

    def test_reboot_catchup_is_single_bounded_day(self):
        day1 = utc_ts(2026, 7, 1, 15, 30)  # 17:30 Warsaw, due.
        first = process_day(self.store, "example-crm", 1,
                            [self.finding(rev="r1")], day1)
        self.assertEqual(first["action"], "proposed")
        # Outage 07-02..07-04: no runs at all (reboot before 17:00 daily).
        morning = utc_ts(2026, 7, 5, 6, 0)  # 08:00 Warsaw, not due.
        self.assertFalse(is_due(morning))
        out = process_catchup(self.store, "example-crm", 1,
                              [self.finding(rev="r2")], morning)
        self.assertTrue(out["catchup"])
        self.assertEqual(out["catchup_date"], "2026-07-04")
        self.assertEqual(out["action"], "proposed")
        self.assertEqual(out["new"], 1)
        # Bounded: only the latest missed day ran, never a historical storm.
        self.assertEqual(
            self.run_keys(),
            ["example-crm:2026-07-01:1", "example-crm:2026-07-04:1"])
        # Repeating the catch-up stays quiet.
        again = process_catchup(self.store, "example-crm", 1,
                                [self.finding(rev="r2")], morning + 60)
        self.assertEqual(again["action"], "nothing-missed")
        # The daily run still owns today and dedups the caught-up revision.
        day5 = utc_ts(2026, 7, 5, 15, 30)
        daily = process_day(self.store, "example-crm", 1,
                            [self.finding(rev="r2")], day5)
        self.assertEqual(daily["action"], "quiet-no-new-items")
        self.assertEqual(daily["repeated"], 1)

    def test_catchup_defers_to_daily_once_due(self):
        out = process_catchup(self.store, "example-crm", 1,
                              [self.finding()], utc_ts(2026, 7, 1, 15, 30))
        self.assertEqual(out["action"], "deferred-to-daily")
        self.assertFalse(out["catchup"])
        self.assertEqual(self.run_keys(), [])

    def test_catchup_unknown_project_rejected(self):
        with self.assertRaises(ValueError):
            process_catchup(self.store, "unknown-proj", 1, [],
                            utc_ts(2026, 7, 5, 6, 0))
        with self.assertRaises(ValueError):
            missed_catchup_date(self.store, "unknown-proj", 1,
                                utc_ts(2026, 7, 5, 6, 0))

    def test_catchup_dst_spring_forward_boundary(self):
        # 2026-03-29: clocks jump 02:00 -> 03:00 CET->CEST at 01:00 UTC.
        self.assertTrue(is_due(utc_ts(2026, 3, 29, 15, 0)))  # 17:00 CEST.
        morning = utc_ts(2026, 3, 29, 6, 0)  # 07:00 CEST, not due.
        self.assertFalse(is_due(morning))
        self.assertEqual(latest_due_date(morning), "2026-03-28")
        # Nothing ran yet: the missed day is 03-28, exactly one date.
        self.assertEqual(
            missed_catchup_date(self.store, "example-crm", 1, morning),
            "2026-03-28")
        # After the 03-28 run exists, nothing is missed.
        process_day(self.store, "example-crm", 1, [self.finding()],
                    utc_ts(2026, 3, 28, 16, 30))  # 17:30 CET, due.
        self.assertIsNone(
            missed_catchup_date(self.store, "example-crm", 1, morning))

    def test_concurrent_catchup_commits_one_run(self):
        path = os.path.join(self.tmp.name, "catchup-concurrent.db")
        now = utc_ts(2026, 7, 5, 6, 0)  # Not due; missed date is 07-04.
        entered = threading.Event()
        release = threading.Event()
        results = []
        errors = []

        def hold(point):
            if point == "after-sighting":
                entered.set()
                release.wait(2)

        def worker(hook=None):
            store = HelpdeskStore(path)
            try:
                results.append(process_catchup(
                    store, "example-crm", 1, [self.finding()], now,
                    failure_injector=hook))
            except BaseException as exc:  # surfaced below, never swallowed
                errors.append(exc)
            finally:
                store.close()

        first = threading.Thread(target=worker, args=(hold,))
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
        actions = [r["action"] for r in results]
        # Overlapping workers serialize inside the run transaction
        # (quiet-duplicate-run). A worker that observes the committed marker
        # before entering that transaction legitimately returns nothing-missed.
        # Either way exactly one run/proposal is committed.
        self.assertEqual(sum(1 for a in actions if a == "proposed"), 1)
        other = [a for a in actions if a != "proposed"]
        self.assertEqual(len(other), 1)
        self.assertIn(other[0], ("nothing-missed", "quiet-duplicate-run"))
        observer = HelpdeskStore(path)
        try:
            self.assertEqual(
                observer.conn.execute(
                    "SELECT COUNT(*) FROM sightings").fetchone()[0], 1)
            self.assertEqual(len(observer.list_requests("proposed")), 1)
            self.assertEqual(len(observer.outbox.pending()), 1)
            self.assertEqual(
                observer.conn.execute(
                    "SELECT COUNT(*) FROM runs").fetchone()[0], 1)
        finally:
            observer.close()


class RevisionDigestTests(unittest.TestCase):
    TS = "2026-09-16T12:00:00Z"

    def test_passthrough_without_content_fields(self):
        self.assertEqual(derive_revision(self.TS), self.TS)
        self.assertEqual(derive_revision(None), "")

    def test_deterministic_and_content_sensitive(self):
        first = revision_digest(title="Login fails", body="500 on submit",
                                status="NEW", severity="HIGH",
                                comments=("ack",))
        second = revision_digest(title="Login fails", body="500 on submit",
                                 status="NEW", severity="HIGH",
                                 comments=("ack",))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        changed = revision_digest(title="Login fails",
                                  body="500 on submit, retry helps",
                                  status="NEW", severity="HIGH",
                                  comments=("ack",))
        self.assertNotEqual(first, changed)

    def test_normalisation_irrelevant_differences(self):
        self.assertEqual(revision_digest(title="  padded  "),
                         revision_digest(title="padded"))
        self.assertEqual(revision_digest(title="caf\u00e9"),
                         revision_digest(title="cafe\u0301"))

    def test_new_revision_proposes_despite_same_updated_at(self):
        tmp = tempfile.TemporaryDirectory()
        store = HelpdeskStore(os.path.join(tmp.name, "hd.db"))
        try:
            day1 = utc_ts(2026, 7, 1, 15, 30)
            rev1 = derive_revision(self.TS, title="Login fails",
                                   body="500 on submit")
            process_day(store, "example-crm", 1,
                        [Finding(project="example-crm", stable_id="INC-9",
                                 revision=rev1, kind="internal",
                                 summary=json.dumps({"issue_id": "INC-9"}))],
                        day1)
            # Same source timestamp, materially changed body: new revision.
            rev2 = derive_revision(self.TS, title="Login fails",
                                   body="500 on submit, retry helps")
            self.assertNotEqual(rev1, rev2)
            day2 = utc_ts(2026, 7, 2, 15, 30)
            out = process_day(store, "example-crm", 2,
                              [Finding(project="example-crm",
                                       stable_id="INC-9", revision=rev2,
                                       kind="internal",
                                       summary=json.dumps(
                                           {"issue_id": "INC-9"}))],
                              day2)
            self.assertEqual(out["new"], 1)
        finally:
            store.close()
            tmp.cleanup()

    def test_unavailable_comments_not_hashed_as_complete(self):
        missing = revision_digest(title="Login fails", body="500",
                                  status="NEW", severity="HIGH")
        empty = revision_digest(title="Login fails", body="500",
                                status="NEW", severity="HIGH", comments=())
        present = revision_digest(title="Login fails", body="500",
                                  status="NEW", severity="HIGH",
                                  comments=("ack",))
        self.assertNotEqual(missing, empty)
        self.assertNotEqual(missing, present)
        self.assertNotEqual(empty, present)
        self.assertEqual(
            revision_digest(title="Login fails", body="500",
                            status="NEW", severity="HIGH"),
            missing)

    def test_raw_bodies_never_persisted(self):
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "hd.db")
        store = HelpdeskStore(path)
        try:
            secret = "SECRET-BODY-9f3k-customer-details"
            rev = derive_revision(self.TS, title="t", body=secret)
            store_path_rev = rev
            process_day(store, "example-crm", 1,
                        [Finding(project="example-crm", stable_id="INC-9",
                                 revision=store_path_rev, kind="internal",
                                 summary=json.dumps(
                                     {"issue_id": "INC-9",
                                      "revision": store_path_rev}))],
                        utc_ts(2026, 7, 1, 15, 30))
        finally:
            store.close()
        with open(path, "rb") as fh:
            blob = fh.read()
        self.assertNotIn(secret.encode("utf-8"), blob)
        tmp.cleanup()


class CollectorRetryAlarmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HelpdeskStore(os.path.join(self.tmp.name, "hd.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_retry_only_on_raising_reader(self):
        calls = []

        def blocked():
            calls.append(1)
            return {"status": "BLOCKED", "reason": "auth_or_source_unavailable"}

        self.assertEqual(read_with_retry(blocked)["status"], "BLOCKED")
        self.assertEqual(len(calls), 1)  # BLOCKED results never retried.

        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise TimeoutError("transient")
            return {"status": "OK"}

        self.assertEqual(read_with_retry(flaky)["status"], "OK")
        self.assertEqual(len(attempts), 3)

        doomed = []

        def down():
            doomed.append(1)
            raise ConnectionError("still down")

        with self.assertRaises(ConnectionError):
            read_with_retry(down)
        self.assertEqual(len(doomed), 3)  # Bounded: no retry storm.

    def test_blocked_never_marks_run_and_alarms_once(self):
        audits = []
        first = record_collector_outcome(
            self.store, "example-crm", ok=False,
            reason="auth_or_source_unavailable", now=T0,
            audit_hook=audits.append)
        self.assertIsNotNone(first["alarm"])
        self.assertEqual(len(audits), 1)
        alarm = audits[0]
        self.assertTrue(alarm)  # No empty audit payload, ever.
        self.assertEqual(alarm["kind"], "collector-alarm")
        self.assertEqual(alarm["project"], "example-crm")
        # No empty success: a failed collector leaves no run marker behind.
        self.assertEqual(
            self.store.conn.execute(
                "SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        # Repeated identical failure stays quiet.
        repeat = record_collector_outcome(
            self.store, "example-crm", ok=False,
            reason="auth_or_source_unavailable", now=T0 + 60,
            audit_hook=audits.append)
        self.assertIsNone(repeat["alarm"])
        self.assertEqual(len(audits), 1)
        state = get_collector_state(self.store, "example-crm")
        self.assertEqual(state["consecutive_failures"], 2)
        # A new reason alarms exactly once; steady OK is silent.
        changed = record_collector_outcome(
            self.store, "example-crm", ok=False, reason="ssh_read_failed",
            now=T0 + 120, audit_hook=audits.append)
        self.assertIsNotNone(changed["alarm"])
        self.assertEqual(len(audits), 2)
        healthy = record_collector_outcome(
            self.store, "example-crm", ok=True, now=T0 + 180,
            audit_hook=audits.append)
        self.assertIsNone(healthy["alarm"])
        self.assertEqual(len(audits), 2)
        self.assertEqual(
            get_collector_state(self.store, "example-crm")[
                "consecutive_failures"], 0)

    def test_alarm_survives_reboot_without_repeating(self):
        record_collector_outcome(
            self.store, "example-crm", ok=False,
            reason="auth_or_source_unavailable", now=T0)
        self.store.close()
        reopened = HelpdeskStore(self.path())
        try:
            quiet = record_collector_outcome(
                reopened, "example-crm", ok=False,
                reason="auth_or_source_unavailable", now=T0 + 3600)
            self.assertIsNone(quiet["alarm"])  # Same failure: still quiet.
            self.assertEqual(
                reopened.conn.execute(
                    "SELECT COUNT(*) FROM alarms").fetchone()[0], 1)
        finally:
            reopened.close()

    def path(self):
        return os.path.join(self.tmp.name, "hd.db")

    def test_unknown_project_rejected(self):
        with self.assertRaises(ValueError):
            record_collector_outcome(self.store, "unknown-proj", ok=True,
                                     now=T0)


class ScheduleClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = HelpdeskStore(os.path.join(self.tmp.name, "hd.db"))
        self.key = schedule_key("example-crm", "2026-07-04", 1)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_live_claim_blocks_duplicate_and_expired_claim_recovers(self):
        self.assertTrue(claim_schedule(self.store, self.key, T0))
        self.assertFalse(claim_schedule(self.store, self.key, T0 + 1))
        # Clock jumping backwards cannot steal a live claim.
        self.assertFalse(claim_schedule(
            self.store, self.key, T0 - models.CLOCK_SKEW_LIMIT - 1))
        # TTL expiry (authority-host wall clock) allows failure recovery.
        self.assertTrue(claim_schedule(
            self.store, self.key, T0 + models.LEASE_TTL))
        release_schedule_claim(self.store, self.key)
        self.assertTrue(claim_schedule(self.store, self.key, T0 + 10_000))
        self.store.conn.execute(
            "INSERT INTO runs(key, finished_at) VALUES(?,?)",
            (self.key, T0))
        release_schedule_claim(self.store, self.key)
        self.assertFalse(claim_schedule(self.store, self.key, T0 + 20_000))


class DurableApprovalRepairTests(unittest.TestCase):
    def setUp(self):
        self.req = repairs.bind_repair_request(
            request_id="RR-example-crm-INC-9-r7", project="example-crm",
            stable_id="INC-9", revision="r7", scope="candidate-code",
            expires_at=T0 + 600, created_at=T0)
        self.request_row = {
            "request_id": "RR-example-crm-INC-9-r7", "project": "example-crm",
            "stable_id": "INC-9", "revision": "r7", "scope": "candidate-code",
            "scope_hash": self.req.scope_hash, "status": "proposed"}

    def decide_valid(self, store=None):
        store = store if store is not None else ApprovalStore()
        store.add(self.req)
        text = canonical_text(self.req)
        msg = TelegramMessage(message_id="m-t6b-1", sender_id="sender-1",
                              chat_id="chat-1", text=text, sent_at=T0)
        return store, decide(store, ALLOW, msg, T0 + 10)

    def test_exact_decision_authorises_candidate_only(self):
        _, verdict = self.decide_valid()
        self.assertTrue(verdict["authorized"])
        dispatched = []
        audits = []
        out = repairs.authorize_repair(
            verdict=verdict, pending=self.req, request=self.request_row,
            now=T0 + 20, audit_hook=audits.append,
            graph_dispatch=dispatched.append)
        self.assertTrue(out["authorized"])
        self.assertTrue(out["dispatched"])
        self.assertTrue(out["candidate_only"])
        self.assertEqual(len(audits), 1)
        self.assertTrue(audits[0])  # No empty audit record.
        # The Graph hook receives references/hashes only.
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(set(dispatched[0]),
                         {"request_id", "project", "stable_id", "revision",
                          "scope", "scope_hash", "candidate_hash"})

    def test_repair_gating_without_live_dispatch(self):
        _, verdict = self.decide_valid()
        out = repairs.authorize_repair(
            verdict=verdict, pending=self.req, request=self.request_row,
            now=T0 + 20)
        self.assertTrue(out["authorized"])
        self.assertFalse(out["dispatched"])
        self.assertEqual(out["reason"], "graph-dispatch-unconnected")

    def test_replayed_message_never_authorises(self):
        store, first = self.decide_valid()
        self.assertTrue(first["authorized"])
        replay = decide(store, ALLOW,
                        TelegramMessage(
                            message_id="m-t6b-1", sender_id="sender-1",
                            chat_id="chat-1", text=canonical_text(self.req),
                            sent_at=T0 + 11),
                        T0 + 12)
        self.assertFalse(replay["authorized"])
        self.assertEqual(replay["reason"], "replayed-message")
        denied = repairs.authorize_repair(
            verdict=replay, pending=self.req, request=self.request_row,
            now=T0 + 20, graph_dispatch=lambda payload: self.fail(
                "must not dispatch without authorization"))
        self.assertFalse(denied["authorized"])

    def test_forged_candidate_binding_rejected(self):
        _, verdict = self.decide_valid()
        forged = dict(verdict)
        forged["candidate_hash"] = "deadbeef"
        out = repairs.authorize_repair(
            verdict=forged, pending=self.req, request=self.request_row,
            now=T0 + 20,
            graph_dispatch=lambda payload: self.fail("must not dispatch"))
        self.assertFalse(out["authorized"])
        self.assertEqual(out["reason"], "candidate-binding-mismatch")

    def test_tampered_revision_and_expiry_rejected(self):
        _, verdict = self.decide_valid()
        wrong_rev = dict(verdict)
        wrong_rev["revision"] = "r8"
        out = repairs.authorize_repair(
            verdict=wrong_rev, pending=self.req, request=self.request_row,
            now=T0 + 20)
        self.assertFalse(out["authorized"])
        self.assertEqual(out["reason"], "revision-mismatch")
        expired_verdict = {"authorized": True,
                           "request_id": self.req.request_id,
                           "revision": "r7", "scope": "candidate-code",
                           "scope_hash": self.req.scope_hash,
                           "action": "approve", "candidate_only": True,
                           "message_id": "m-t6b-1"}
        late = repairs.authorize_repair(
            verdict=expired_verdict, pending=self.req, request=self.request_row,
            now=T0 + 601)
        self.assertFalse(late["authorized"])
        self.assertEqual(late["reason"], "authorization-expired")

    def test_deploy_scope_never_unlocked(self):
        pending = repairs.bind_repair_request(
            request_id="RR-x", project="example-crm", stable_id="INC-9",
            revision="r7", scope="deploy", expires_at=T0 + 600,
            created_at=T0)
        forged_verdict = {"authorized": True, "request_id": "RR-x",
                          "revision": "r7", "scope": "deploy",
                          "scope_hash": pending.scope_hash,
                          "action": "approve", "message_id": "m-evil"}
        out = repairs.authorize_repair(
            verdict=forged_verdict, pending=pending,
            request={"request_id": "RR-x", "project": "example-crm",
                     "stable_id": "INC-9", "revision": "r7", "scope": "deploy",
                     "scope_hash": pending.scope_hash, "status": "proposed"},
            now=T0 + 20,
            graph_dispatch=lambda payload: self.fail("deploy must not run"))
        self.assertFalse(out["authorized"])
        self.assertEqual(
            out["reason"], "scope-requires-separate-explicit-authorization")

    def test_non_proposed_request_never_dispatches(self):
        _, verdict = self.decide_valid()
        row = dict(self.request_row)
        row["status"] = "forbidden-needs-explicit-scope"
        out = repairs.authorize_repair(
            verdict=verdict, pending=self.req, request=row, now=T0 + 20,
            graph_dispatch=lambda payload: self.fail("must not dispatch"))
        self.assertFalse(out["authorized"])
        self.assertEqual(out["reason"], "request-not-proposed")

    def test_durable_store_persists_binding_and_replay(self):
        tmp = tempfile.TemporaryDirectory()
        conn = sqlite3.connect(os.path.join(tmp.name, "approvals.db"))
        conn.isolation_level = None
        durable = DurableApprovalStore(conn)
        try:
            durable.save_pending(self.req)
            loaded = durable.load_pending(self.req.request_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.project, "example-crm")
            self.assertEqual(loaded.candidate_hash, self.req.candidate_hash)
            self.assertEqual(loaded.revision, "r7")
            self.assertIsNone(durable.load_pending("RR-missing"))
            self.assertTrue(durable.mark_seen("m-t6b-9", T0))
            self.assertTrue(durable.has_seen("m-t6b-9"))
            # Replayed message id is refused durably, across restarts.
            self.assertFalse(durable.mark_seen("m-t6b-9", T0 + 5))
            _, verdict = self.decide_valid()
            durable.record_decision(self.req, verdict, T0 + 20)
            stored = durable.get_decision(self.req.request_id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored["project"], "example-crm")
            self.assertEqual(stored["stable_id"], "INC-9")
            self.assertEqual(stored["revision"], "r7")
            self.assertEqual(stored["scope"], "candidate-code")
            self.assertEqual(stored["scope_hash"], self.req.scope_hash)
            self.assertEqual(stored["candidate_hash"],
                             self.req.candidate_hash)
            self.assertEqual(stored["authorized"], 1)
            self.assertIsNone(durable.get_decision("RR-missing"))
        finally:
            conn.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
