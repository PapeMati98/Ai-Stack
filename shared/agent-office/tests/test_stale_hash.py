from __future__ import annotations

from support import CoordTestCase

from coordfs.hashing import hash_file


class StaleHashTests(CoordTestCase):
    def test_release_commits_intended_change_while_lease_is_fresh(self) -> None:
        target = self.cwd / "tracked.txt"
        target.write_text("one", encoding="utf-8")
        claimed = self.board.claim(str(target), "agent-a", ttl_s=30, kind="path")
        self.assertEqual(claimed["status"], "ok")
        target.write_text("two", encoding="utf-8")
        released = self.board.release(str(target), "agent-a", kind="path")
        self.assertEqual(released["status"], "ok")
        self.assertEqual(released["code"], "released")
        self.assertEqual(released["committed_hash"], hash_file(target))
        self.assertIsNone(self.board.who(str(target), kind="path")["holder_agent"])

    def test_renew_commits_change_and_refreshes_hash(self) -> None:
        target = self.cwd / "renewed.txt"
        target.write_text("before", encoding="utf-8")
        self.board.claim(str(target), "agent-a", ttl_s=30, kind="path")
        target.write_text("after", encoding="utf-8")
        renewed = self.board.renew(str(target), "agent-a", kind="path")
        self.assertEqual(renewed["status"], "ok")
        self.assertEqual(renewed["code"], "renewed")
        self.assertEqual(renewed["lease"]["file_hash"], hash_file(target))
        still = self.board.who(str(target), kind="path")
        self.assertEqual(still["holder_agent"], "agent-a")
        self.assertEqual(still["lease"]["file_hash"], hash_file(target))

    def test_change_after_expiry_is_a_conflict_not_a_commit(self) -> None:
        target = self.cwd / "expired.txt"
        target.write_text("before", encoding="utf-8")
        claimed = self.board.claim(str(target), "agent-a", ttl_s=1, kind="path")
        self.advance(1)
        target.write_text("after-expiry", encoding="utf-8")
        released = self.board.release(str(target), "agent-a", kind="path")
        self.assertEqual(released["status"], "conflict")
        self.assertEqual(released["code"], "stale_resource")
        self.assertEqual(released["expected_hash"], claimed["lease"]["file_hash"])
        self.assertEqual(released["observed_hash"], hash_file(target))
