from __future__ import annotations

import threading

from support import CoordTestCase

from coordfs.leases import CoordBoard, lease_digest, lease_payload


class ReleaseRaceTests(CoordTestCase):
    def test_release_never_deletes_successor_lease(self) -> None:
        target = self.cwd / "release-race.txt"
        target.write_text("stable", encoding="utf-8")
        first = self.board.claim(str(target), "agent-a", ttl_s=1, kind="path")
        self.assertEqual(first["status"], "ok")

        checked = threading.Event()
        resume = threading.Event()
        original_require = self.board._require_own_fresh

        # Forward any keyword the production call adds (e.g. caller_strength):
        # the pause must wrap the real owner check, not fail on its signature.
        def pause_after_owner_check(resource: str, agent_id: str, kind: str | None, **kwargs):
            result = original_require(resource, agent_id, kind, **kwargs)
            checked.set()
            self.assertTrue(resume.wait(timeout=5))
            return result

        self.board._require_own_fresh = pause_after_owner_check  # type: ignore[method-assign]
        release_result: list[dict] = []
        release_thread = threading.Thread(
            target=lambda: release_result.append(
                self.board.release(str(target), "agent-a", kind="path")
            )
        )
        release_thread.start()
        self.assertTrue(checked.wait(timeout=5))

        self.advance(2)
        successor_board = CoordBoard(
            home=self.home,
            cwd=self.cwd,
            now=lambda: self.clock[0],
            events_max_bytes_value=4096,
            events_retain_value=3,
        )
        successor = successor_board.claim(
            str(target), "agent-b", ttl_s=30, kind="path"
        )
        self.assertEqual(successor["status"], "ok")
        self.assertEqual(successor["holder_agent"], "agent-b")

        resume.set()
        release_thread.join(timeout=5)
        self.assertFalse(release_thread.is_alive())

        current = successor_board.who(str(target), kind="path")
        self.assertEqual(current["holder_agent"], "agent-b")
        self.assertNotEqual(release_result[0].get("code"), "released")

    def test_release_cas_mismatch_never_unlinks_current_lease(self) -> None:
        target = self.cwd / "cas-foreign.txt"
        target.write_text("stable", encoding="utf-8")
        first = self.board.claim(
            str(target),
            "agent-a",
            ttl_s=30,
            kind="path",
            meta={"binding_id": "bind-1", "actor": "agent-a"},
        )
        self.assertEqual(first["status"], "ok")
        current_digest = lease_digest(first["lease"])
        self.assertEqual(first["lease_digest"], current_digest)

        foreign = self.board.release(
            str(target),
            "agent-a",
            kind="path",
            expected_lease_digest="0" * 64,
        )
        self.assertEqual(foreign["status"], "conflict")
        self.assertEqual(foreign["code"], "lease_digest_mismatch")
        held = self.board.who(str(target), kind="path")
        self.assertEqual(held["holder_agent"], "agent-a")

        invalid = self.board.release(
            str(target),
            "agent-a",
            kind="path",
            expected_lease_digest="not-a-digest",
        )
        self.assertEqual(invalid["status"], "conflict")
        self.assertEqual(invalid["code"], "lease_digest_invalid")
        self.assertEqual(self.board.who(str(target), kind="path")["holder_agent"], "agent-a")

        invalid_hex = self.board.release(
            str(target),
            "agent-a",
            kind="path",
            expected_lease_digest="g" * 64,
        )
        self.assertEqual(invalid_hex["status"], "conflict")
        self.assertEqual(invalid_hex["code"], "lease_digest_invalid")
        self.assertEqual(self.board.who(str(target), kind="path")["holder_agent"], "agent-a")

        released = self.board.release(
            str(target),
            "agent-a",
            kind="path",
            expected_lease_digest=current_digest,
        )
        self.assertEqual(released["status"], "ok")
        self.assertEqual(released["code"], "released")
        self.assertEqual(self.board.who(str(target), kind="path")["holder_agent"], None)

    def test_release_expected_meta_fences_same_actor_to_binding(self) -> None:
        target = self.cwd / "binding-scope.txt"
        target.write_text("stable", encoding="utf-8")
        claim = self.board.claim(
            str(target),
            "codex-ailab-01a0ac55-48ac-7742-9b18-8fa624150d4b",
            ttl_s=30,
            kind="path",
            meta={"binding_id": "binding-a", "actor": "codex-ailab-01a0ac55-48ac-7742-9b18-8fa624150d4b"},
        )
        self.assertEqual(claim["status"], "ok")
        digest = lease_digest(claim["lease"])
        actor = "codex-ailab-01a0ac55-48ac-7742-9b18-8fa624150d4b"

        wrong_binding = self.board.release(
            str(target), actor, kind="path", expected_lease_digest=digest,
            expected_meta={"binding_id": "binding-b", "actor": actor},
        )
        self.assertEqual((wrong_binding["status"], wrong_binding["code"]),
                         ("conflict", "lease_meta_mismatch"))
        self.assertEqual(self.board.who(str(target), kind="path")["holder_agent"], actor)

        wrong_actor_meta = self.board.release(
            str(target), actor, kind="path", expected_lease_digest=digest,
            expected_meta={"binding_id": "binding-a", "actor": "codex-ailab-ffffffff-ffff-4fff-8fff-ffffffffffff"},
        )
        self.assertEqual((wrong_actor_meta["status"], wrong_actor_meta["code"]),
                         ("conflict", "lease_meta_mismatch"))
        invalid = self.board.release(
            str(target), actor, kind="path", expected_lease_digest=digest,
            expected_meta=["binding-a"],  # type: ignore[arg-type]
        )
        self.assertEqual((invalid["status"], invalid["code"]),
                         ("conflict", "lease_meta_invalid"))
        self.assertEqual(self.board.who(str(target), kind="path")["holder_agent"], actor)

        released = self.board.release(
            str(target), actor, kind="path", expected_lease_digest=digest,
            expected_meta={"binding_id": "binding-a", "actor": actor},
        )
        self.assertEqual((released["status"], released["code"]), ("ok", "released"))
        self.assertIsNone(self.board.who(str(target), kind="path")["holder_agent"])

    def test_release_cas_race_with_successor_digest_refused(self) -> None:
        target = self.cwd / "cas-race.txt"
        target.write_text("stable", encoding="utf-8")
        first = self.board.claim(str(target), "agent-a", ttl_s=1, kind="path")
        self.assertEqual(first["status"], "ok")
        stale_digest = lease_digest(first["lease"])

        self.advance(2)
        successor_board = CoordBoard(
            home=self.home,
            cwd=self.cwd,
            now=lambda: self.clock[0],
            events_max_bytes_value=4096,
            events_retain_value=3,
        )
        successor = successor_board.claim(
            str(target),
            "agent-b",
            ttl_s=30,
            kind="path",
            meta={"binding_id": "bind-2", "actor": "agent-b"},
        )
        self.assertEqual(successor["status"], "ok")
        successor_digest = lease_digest(successor["lease"])
        self.assertNotEqual(stale_digest, successor_digest)

        raced = successor_board.release(
            str(target),
            "agent-b",
            kind="path",
            expected_lease_digest=stale_digest,
        )
        self.assertEqual(raced["status"], "conflict")
        self.assertEqual(raced["code"], "lease_digest_mismatch")
        self.assertEqual(successor_board.who(str(target), kind="path")["holder_agent"], "agent-b")

        released = successor_board.release(
            str(target),
            "agent-b",
            kind="path",
            expected_lease_digest=successor_digest,
        )
        self.assertEqual(released["status"], "ok")
        self.assertEqual(successor_board.who(str(target), kind="path")["holder_agent"], None)

    def test_release_cas_on_expired_lease_is_not_released(self) -> None:
        target = self.cwd / "cas-expired.txt"
        target.write_text("stable", encoding="utf-8")
        first = self.board.claim(str(target), "agent-a", ttl_s=1, kind="path")
        self.assertEqual(first["status"], "ok")
        digest = lease_digest(first["lease"])
        self.advance(5)
        expired = self.board.release(
            str(target),
            "agent-a",
            kind="path",
            expected_lease_digest=digest,
        )
        self.assertNotEqual(expired.get("code"), "released")
        self.assertIn(expired["status"], {"error", "conflict", "warning"})

    def test_lease_digest_includes_meta(self) -> None:
        base = lease_payload(
            "data:asset-1",
            "agent-a",
            "2030-01-01T00:00:00Z",
            30,
            "",
            {"binding_id": "bind-a", "actor": "agent-a"},
        )
        other_meta = lease_payload(
            "data:asset-1",
            "agent-a",
            "2030-01-01T00:00:00Z",
            30,
            "",
            {"binding_id": "bind-b", "actor": "agent-a"},
        )
        self.assertNotEqual(lease_digest(base), lease_digest(other_meta))
        self.assertEqual(lease_digest(base), lease_digest(dict(base)))
        # An extra/missing meta key changes the full canonical lease digest.
        sparse = lease_payload("data:asset-1", "agent-a", "2030-01-01T00:00:00Z", 30, "", {})
        self.assertNotEqual(lease_digest(base), lease_digest(sparse))
