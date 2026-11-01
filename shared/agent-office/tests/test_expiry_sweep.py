from __future__ import annotations

from support import CoordTestCase


class ExpirySweepTests(CoordTestCase):
    def test_lazy_sweep_reclaims_after_ttl(self) -> None:
        first = self.board.claim("data:ttl", "agent-a", ttl_s=1)
        self.assertEqual(first["status"], "ok")
        self.advance(1)
        second = self.board.claim("data:ttl", "agent-b", ttl_s=30)
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["code"], "reclaimed")
        self.assertEqual(second["holder_agent"], "agent-b")
        self.assertEqual(self.board.who("data:ttl")["holder_agent"], "agent-b")

    def test_explicit_sweep_removes_expired_without_manual_release(self) -> None:
        self.board.claim("data:sweep-1", "agent-a", ttl_s=1)
        self.board.claim("data:sweep-2", "agent-a", ttl_s=30)
        self.advance(1)
        swept = self.board.sweep()
        self.assertEqual(swept["count"], 1)
        self.assertEqual(swept["reclaimed"], ["data:sweep-1"])
        listed = self.board.ls()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["leases"][0]["resource_id"], "data:sweep-2")
