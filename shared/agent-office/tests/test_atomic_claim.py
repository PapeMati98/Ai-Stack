from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from unittest.mock import patch

from coordfs.atomic import exclusive_create_flags
from coordfs.errors import CoordError

from support import CoordTestCase


class AtomicClaimTests(CoordTestCase):
    def test_claim_uses_creat_excl(self) -> None:
        flags_seen: list[int] = []
        real_open = os.open

        def spy(path, flags, mode=0o777):
            flags_seen.append(flags)
            return real_open(path, flags, mode)

        with patch("os.open", spy):
            result = self.board.claim("data:atomic-1", "agent-a", ttl_s=30)
        self.assertEqual(result["status"], "ok")
        expected = exclusive_create_flags()
        matching = [flags for flags in flags_seen if flags & os.O_EXCL and flags & os.O_CREAT]
        self.assertTrue(matching)
        self.assertTrue(all(flags & os.O_WRONLY for flags in matching))
        self.assertEqual(matching[0], expected)

    def test_contended_claim_has_one_winner(self) -> None:
        barrier = threading.Barrier(2)
        results: list[dict] = []

        def worker(agent: str) -> None:
            barrier.wait()
            results.append(self.board.claim("data:race", agent, ttl_s=30))

        threads = [
            threading.Thread(target=worker, args=("agent-a",)),
            threading.Thread(target=worker, args=("agent-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        statuses = sorted(item["status"] for item in results)
        self.assertEqual(statuses, ["ok", "warning"])
        winner = next(item for item in results if item["status"] == "ok")
        loser = next(item for item in results if item["status"] == "warning")
        self.assertEqual(loser["code"], "held")
        self.assertEqual(loser["holder_agent"], winner["holder_agent"])

    def test_live_foreign_lease_returns_warning_without_waiting_for_mutex(self) -> None:
        first = self.board.claim("data:fast-path", "agent-a", ttl_s=30)
        self.assertEqual(first["status"], "ok")

        with patch(
            "coordfs.leases.exclusive_mutex",
            side_effect=AssertionError("live foreign lease must bypass the mutex"),
        ):
            second = self.board.claim("data:fast-path", "agent-b", ttl_s=30)

        self.assertEqual(second["status"], "warning")
        self.assertEqual(second["code"], "held")
        self.assertEqual(second["holder_agent"], "agent-a")

    def test_mutex_busy_falls_back_to_live_foreign_lease(self) -> None:
        first = self.board.claim("data:busy-fallback", "agent-a", ttl_s=30)
        lease = first["lease"]

        @contextmanager
        def busy_mutex(*_args, **_kwargs):
            raise CoordError("coordination mutex is busy", code="busy")
            yield

        with (
            patch.object(self.board, "_read_lock", side_effect=[None, lease]),
            patch("coordfs.leases.exclusive_mutex", busy_mutex),
        ):
            second = self.board.claim("data:busy-fallback", "agent-b", ttl_s=30)

        self.assertEqual(second["status"], "warning")
        self.assertEqual(second["code"], "held")
        self.assertEqual(second["holder_agent"], "agent-a")

    def test_mutex_busy_does_not_hide_expired_lease(self) -> None:
        first = self.board.claim("data:expired-busy", "agent-a", ttl_s=1)
        self.advance(2)

        @contextmanager
        def busy_mutex(*_args, **_kwargs):
            raise CoordError("coordination mutex is busy", code="busy")
            yield

        with patch("coordfs.leases.exclusive_mutex", busy_mutex):
            with self.assertRaises(CoordError) as caught:
                self.board.claim("data:expired-busy", "agent-b", ttl_s=30)

        self.assertEqual(caught.exception.code, "busy")
        self.assertEqual(first["holder_agent"], "agent-a")
