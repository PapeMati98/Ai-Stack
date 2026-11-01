"""Scope overlap detection warns when two holders' scopes touch the same file.

The board treats each ``scope:`` lease as a separate lock keyed by the
canonical scope id, so two leases whose scopes contain each other (for
example ``scope:scripts/`` and ``scope:scripts/sub/``) used to coexist
silently. This suite pins the warning contract that closes that gap.
"""

from __future__ import annotations

from time import perf_counter

from support import CoordTestCase

from coordfs.leases import encode_lease
from coordfs.office import Office
from coordfs.store import lock_path


class ScopeOverlapDetectionTests(CoordTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.agent_alpha = "alpha-harness-alpha-host-alpha-session"
        self.agent_bravo = "bravo-harness-bravo-host-bravo-session"
        self.agent_charlie = "charlie-harness-charlie-host-charlie-session"

    def _claim_scope(self, scope: str, agent: str, ttl_s: int = 60) -> dict:
        return self.board.claim(scope, agent, ttl_s=ttl_s, kind="scope")

    def _replace_holder(self, claimed: dict, holder_agent: str) -> dict:
        """Model an overlap left by a race/older writer without bypassing lease parsing."""
        lease = dict(claimed["lease"])
        lease["holder_agent"] = holder_agent
        lock_path(self.home, lease["resource_id"]).write_bytes(encode_lease(lease))
        return lease

    def test_same_agent_taking_child_after_parent_is_not_a_conflict(self) -> None:
        parent = self._claim_scope("scope:scripts/", self.agent_alpha, ttl_s=60)
        self.assertEqual(parent["status"], "ok")
        child = self._claim_scope("scope:scripts/sub/", self.agent_alpha, ttl_s=60)
        self.assertEqual(child["status"], "ok", child)
        self.assertEqual(child["code"], "claimed")
        self.assertEqual(child["holder_agent"], self.agent_alpha)
        self.assertEqual(self.board.ls()["scope_overlaps"], [])
        self.assertEqual(self.board.who("scope:scripts/", kind="scope")["scope_overlaps"], [])

    def test_scope_a_and_scope_ab_are_disjoint(self) -> None:
        base = self._claim_scope("scope:scripts/a", self.agent_alpha, ttl_s=60)
        self.assertEqual(base["status"], "ok")
        sibling = self._claim_scope("scope:scripts/ab", self.agent_bravo, ttl_s=60)
        self.assertEqual(sibling["status"], "ok", sibling)
        self.assertEqual(sibling["code"], "claimed")
        self.assertEqual(sibling["holder_agent"], self.agent_bravo)
        self.assertEqual(self.board.ls()["scope_overlaps"], [])
        self.assertEqual(self.board.who("scope:scripts/a", kind="scope")["scope_overlaps"], [])

    def test_case_difference_is_treated_as_the_same_scope(self) -> None:
        lower = self._claim_scope("scope:scripts/", self.agent_alpha, ttl_s=60)
        self.assertEqual(lower["status"], "ok")
        upper = self._claim_scope("scope:Scripts/", self.agent_bravo, ttl_s=60)
        self.assertEqual(upper["status"], "warning", upper)
        self.assertEqual(upper["code"], "held")
        self.assertEqual(upper["holder_agent"], self.agent_alpha)
        overlap = upper["overlap"]
        self.assertEqual(overlap["relation"], "equal")
        self.assertEqual(overlap["other_resource_id"], lower["resource_id"])
        self.assertEqual(overlap["other_holder_agent"], self.agent_alpha)

    def test_expired_parent_scope_does_not_block_a_new_child_claim(self) -> None:
        parent = self._claim_scope("scope:scripts/", self.agent_alpha, ttl_s=1)
        self.assertEqual(parent["status"], "ok")
        self.advance(120)
        child = self._claim_scope("scope:scripts/sub/", self.agent_bravo, ttl_s=60)
        self.assertEqual(child["status"], "ok", child)
        self.assertEqual(child["code"], "claimed")
        self.assertEqual(child["holder_agent"], self.agent_bravo)
        self.assertEqual(self.board.ls()["scope_overlaps"], [])

    def test_disjoint_scopes_belong_to_two_holders_independently(self) -> None:
        docs = self._claim_scope("scope:docs/", self.agent_alpha, ttl_s=60)
        scripts = self._claim_scope("scope:scripts/", self.agent_bravo, ttl_s=60)
        self.assertEqual(docs["status"], "ok")
        self.assertEqual(scripts["status"], "ok", scripts)
        self.assertEqual(scripts["holder_agent"], self.agent_bravo)

    def test_overlap_warning_is_emitted_in_both_directions(self) -> None:
        narrow_first = self._claim_scope("scope:scripts/sub/", self.agent_alpha, ttl_s=60)
        self.assertEqual(narrow_first["status"], "ok")

        broad_second = self._claim_scope("scope:scripts/", self.agent_bravo, ttl_s=60)
        self.assertEqual(broad_second["status"], "warning", broad_second)
        self.assertEqual(broad_second["code"], "held")
        self.assertEqual(broad_second["overlap"]["relation"], "contains")
        self.assertEqual(broad_second["overlap"]["other_holder_agent"], self.agent_alpha)
        self.assertEqual(broad_second["overlap"]["other_resource_id"], narrow_first["resource_id"])

        narrow_third = self._claim_scope("scope:scripts/sub/inner/", self.agent_charlie, ttl_s=60)
        self.assertEqual(narrow_third["status"], "warning", narrow_third)
        self.assertEqual(narrow_third["code"], "held")
        self.assertEqual(narrow_third["overlap"]["relation"], "contained")
        self.assertEqual(narrow_third["overlap"]["other_holder_agent"], self.agent_alpha)
        self.assertEqual(narrow_third["overlap"]["other_resource_id"], narrow_first["resource_id"])

    def test_overlap_scan_runs_when_kind_is_auto_detected(self) -> None:
        """Auto-detected scope (kind=None) must trigger the overlap scan.

        The CLI forwards ``args.kind=None`` by default, and
        ``CoordBoard.claim("scope:scripts/", ...)`` is the documented direct-call
        form used by ``test_lease_schema.py``. Both rely on
        ``identity.detect_kind`` to recognise the scope; if the overlap guard
        inside ``claim`` only fires on the explicit ``kind == "scope"`` branch,
        two scope claims with ``kind=None`` will silently coexist even though
        their scopes overlap, contradicting the unconditional contract in
        ``SKILL.md`` (lines 194-203).
        """
        parent = self.board.claim("scope:scripts/", self.agent_alpha, ttl_s=60)
        self.assertEqual(parent["status"], "ok")

        child = self.board.claim("scope:scripts/sub/", self.agent_bravo, ttl_s=60)
        self.assertEqual(child["status"], "warning", child)
        self.assertEqual(child["code"], "held")
        self.assertEqual(child["holder_agent"], self.agent_alpha)
        overlap = child["overlap"]
        self.assertEqual(overlap["relation"], "contained")
        self.assertEqual(overlap["other_resource_id"], parent["resource_id"])
        self.assertEqual(overlap["other_holder_agent"], self.agent_alpha)

        casefold = self.board.claim("scope:Scripts/", self.agent_charlie, ttl_s=60)
        self.assertEqual(casefold["status"], "warning", casefold)
        self.assertEqual(casefold["code"], "held")
        self.assertEqual(casefold["overlap"]["relation"], "equal")
        self.assertEqual(casefold["overlap"]["other_holder_agent"], self.agent_alpha)

    def test_overlap_scan_does_not_fire_for_path_or_data_kinds(self) -> None:
        """Path and data claims must keep their original semantics even when a scope lease covers the same area.

        The detector is allowed to recognise ``scope:`` and trailing-slash
        resources, but ``path`` and ``data`` claims have their own lock family
        and must not be scanned for scope overlap even when the raw string
        would parse as a scope. A scope lease taken by another agent on a
        parent resource must not affect a path or data claim on a string that
        would, if claimed as scope, conflict with that lease. The previous
        test claimed ``data:overlap-guard`` on an empty board, so the absence
        of ``overlap`` proved nothing - removing the overlap guard entirely
        would still leave the field absent. With a sibling scope lease in
        place first, the path and data claims must still succeed normally and
        carry no ``overlap`` warning.
        """
        # Another agent takes a scope lease on a parent resource. If the next
        # claim were made as scope with the same raw string, it would either
        # be rejected (canonical conflict) or carry an overlap warning.
        scope_lease = self._claim_scope("scope:overlap-guard/", self.agent_alpha, ttl_s=60)
        self.assertEqual(scope_lease["status"], "ok")

        # The same string claimed as data: must succeed normally and carry
        # no overlap warning. The resource_id is the data-prefixed form, so
        # the overlap guard would have nothing to scan for anyway - but the
        # sibling scope lease proves the boundary against an empty-board
        # smoke test.
        data_lease = self.board.claim("data:overlap-guard", self.agent_bravo, ttl_s=60)
        self.assertEqual(data_lease["status"], "ok", data_lease)
        self.assertEqual(data_lease["code"], "claimed")
        self.assertNotIn("overlap", data_lease)
        self.assertNotEqual(data_lease["resource_id"], scope_lease["resource_id"])

        # The same string claimed as path: must also succeed normally and
        # carry no overlap warning, even though ``detect_kind`` would parse
        # the raw string as a scope if kind were left to auto-detect.
        path_lease = self.board.claim(
            "overlap-guard", self.agent_charlie, ttl_s=60, kind="path"
        )
        self.assertEqual(path_lease["status"], "ok", path_lease)
        self.assertEqual(path_lease["code"], "claimed")
        self.assertNotIn("overlap", path_lease)
        self.assertNotEqual(path_lease["resource_id"], scope_lease["resource_id"])

    def test_who_ls_and_office_who_report_one_foreign_holder_pair(self) -> None:
        parent = self._claim_scope("scope:scripts/", self.agent_alpha, ttl_s=60)
        child = self._claim_scope("scope:scripts/sub/", self.agent_alpha, ttl_s=60)
        self._replace_holder(child, self.agent_bravo)
        paths = [
            lock_path(self.home, parent["resource_id"]),
            lock_path(self.home, child["resource_id"]),
        ]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]

        expected_pair = {
            "relation": "contains",
            "resource_id": parent["resource_id"],
            "holder_agent": self.agent_alpha,
            "other_resource_id": child["resource_id"],
            "other_holder_agent": self.agent_bravo,
        }
        listed = self.board.ls()
        self.assertEqual(listed["scope_overlaps"], [expected_pair])
        self.assertEqual(listed["count"], 2, "the existing ls count keeps counting leases")

        parent_who = self.board.who("scope:scripts/", kind="scope")
        self.assertEqual(parent_who["scope_overlaps"], [expected_pair])
        self.assertEqual(parent_who["holder_agent"], self.agent_alpha)

        child_who = self.board.who("scope:scripts/sub/", kind="scope")
        self.assertEqual(
            child_who["scope_overlaps"],
            [
                {
                    "relation": "contained",
                    "resource_id": child["resource_id"],
                    "holder_agent": self.agent_bravo,
                    "other_resource_id": parent["resource_id"],
                    "other_holder_agent": self.agent_alpha,
                }
            ],
        )

        office_who = Office(
            agent_id="scope-overlap-observer",
            home=self.home,
            now=lambda: self.clock[0],
        ).who()
        self.assertEqual(office_who["scope_overlaps"], [expected_pair])
        self.assertIn("online", office_who, "the existing Office.who payload stays intact")
        after = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        self.assertEqual(after, before, "who/ls must not renew or rewrite live leases")

    def test_who_reports_a_planned_scope_overlap_before_claim(self) -> None:
        parent = self._claim_scope("scope:scripts/", self.agent_alpha, ttl_s=60)

        planned = self.board.who("scope:scripts/sub/", kind="scope")

        self.assertEqual(planned["code"], "free")
        self.assertIsNone(planned["holder_agent"])
        self.assertEqual(
            planned["scope_overlaps"],
            [
                {
                    "relation": "contained",
                    "resource_id": "scripts/sub/",
                    "holder_agent": None,
                    "other_resource_id": parent["resource_id"],
                    "other_holder_agent": self.agent_alpha,
                }
            ],
        )

    def test_ls_scans_200_live_disjoint_scopes_in_reasonable_time(self) -> None:
        for index in range(200):
            claimed = self._claim_scope(
                f"scope:benchmark/item-{index:03d}/",
                f"benchmark-holder-{index:03d}",
                ttl_s=60,
            )
            self.assertEqual(claimed["status"], "ok", claimed)

        started = perf_counter()
        listed = self.board.ls()
        elapsed_s = perf_counter() - started

        self.assertEqual(listed["count"], 200)
        self.assertEqual(listed["scope_overlaps"], [])
        self.assertLess(elapsed_s, 2.0, f"200-lease overlap snapshot took {elapsed_s:.3f}s")
