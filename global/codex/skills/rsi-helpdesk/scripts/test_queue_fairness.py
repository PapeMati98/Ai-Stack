"""Fairness of next_open: older attempted work must not starve under new arrivals.

All tickets are in-memory via a patched all_tickets; no live stores are touched.
Covers failure/release/retry, claim extensions, owner priority, deferred and
malformed dates, and malformed history/count values.
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpdesk

OLD = "2026-01-01T00:00:00Z"
MID = "2026-02-01T00:00:00Z"
NEW = "2026-03-01T00:00:00Z"


def make_ticket(sig, *, count=1, first_seen=NEW, history=None,
                status="open", eligible_at=None):
    ticket = {"signature": sig, "status": status, "count": count,
              "first_seen": first_seen, "last_seen": first_seen,
              "history": list(history or [])}
    if eligible_at is not None:
        ticket["eligible_at"] = eligible_at
    return ticket


def taken(ts, note="claim taken for 30 min"):
    return {"ts": ts, "by": "run-a", "status": "claimed", "note": note}


def pick(tickets):
    with mock.patch.object(helpdesk, "all_tickets", return_value=tickets):
        with contextlib.redirect_stderr(io.StringIO()):
            return helpdesk.next_open()


class NextOpenFairness(unittest.TestCase):
    def test_retry_beats_new_arrivals(self):
        attempted = make_ticket("old-retry", first_seen=OLD, history=[
            taken(OLD),
            {"ts": MID, "by": "run-a", "status": "open",
             "note": "failed attempt released without result"},
        ])
        arrivals = [make_ticket(f"new-{i}", first_seen=NEW) for i in range(3)]
        self.assertEqual(pick([*arrivals, attempted])["signature"], "old-retry")

    def test_stream_of_arrivals_does_not_starve_retry(self):
        attempted = make_ticket("old-retry", first_seen=OLD,
                                history=[taken(OLD)])
        pool = [attempted]
        for day in range(2, 28):
            pool.append(make_ticket(f"arrival-{day:02d}",
                                    first_seen=f"2026-03-{day:02d}T00:00:00Z"))
            self.assertEqual(pick(list(pool))["signature"], "old-retry")

    def test_extension_does_not_refresh_service_time(self):
        extended = make_ticket("extended", first_seen=OLD, history=[
            taken(OLD),
            {"ts": NEW, "by": "run-a", "status": "claimed",
             "note": "claim extended for 30 min"},
        ])
        newer = make_ticket("newer", first_seen=MID)
        self.assertEqual(pick([newer, extended])["signature"], "extended")

    def test_last_take_sets_service_time(self):
        twice = make_ticket("taken-twice", first_seen=OLD,
                            history=[taken(OLD), taken(MID)])
        once = make_ticket("taken-once", first_seen=OLD,
                           history=[taken("2026-01-15T00:00:00Z")])
        self.assertEqual(pick([twice, once])["signature"], "taken-once")

    def test_owner_resumed_stays_first(self):
        resumed = make_ticket("owner-resumed", first_seen=NEW, history=[
            {"ts": MID, "by": "run-a", "status": "candidate",
             "note": "fix candidate"},
            {"ts": NEW, "by": "owner", "status": "open",
             "note": "reopened by owner [force]"},
        ])
        older = make_ticket("older-ordinary", first_seen=OLD,
                            history=[taken(OLD)])
        self.assertEqual(pick([older, resumed])["signature"], "owner-resumed")

    def test_deferred_and_malformed_eligible_excluded(self):
        deferred = make_ticket("deferred", first_seen=OLD,
                               eligible_at="2999-01-01T00:00:00Z")
        bad_str = make_ticket("bad-eligible", first_seen=OLD,
                              eligible_at="not-a-date")
        bad_type = make_ticket("bad-eligible-type", first_seen=OLD,
                               eligible_at=12345)
        healthy = make_ticket("healthy", first_seen=NEW)
        self.assertEqual(
            pick([deferred, bad_str, bad_type, healthy])["signature"],
            "healthy")

    def test_malformed_history_sorts_last_without_crash(self):
        healthy = make_ticket("healthy", first_seen=OLD)
        weird = make_ticket("weird", first_seen="not-a-date", count="many",
                            history=[None, "junk", {"no": "keys"},
                                     {"ts": "2026-01-01T00:00:00",
                                      "status": "claimed",
                                      "note": "claim taken naive ts"},
                                     {"ts": None, "status": "claimed",
                                      "note": "claim taken for 5 min"}])
        self.assertEqual(pick([weird, healthy])["signature"], "healthy")

    def test_frequency_then_signature_tiebreak(self):
        low = make_ticket("b-low", first_seen=MID, count=1)
        high = make_ticket("a-high", first_seen=MID, count=9)
        self.assertEqual(pick([low, high])["signature"], "a-high")
        tie_a = make_ticket("a-tie", first_seen=MID, count=2)
        tie_b = make_ticket("b-tie", first_seen=MID, count=2)
        self.assertEqual(pick([tie_b, tie_a])["signature"], "a-tie")


if __name__ == "__main__":
    unittest.main()
