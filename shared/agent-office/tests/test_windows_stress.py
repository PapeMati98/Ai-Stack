"""Bounded thread-level smoke coverage; full acceptance is run_full_stress.py."""

from __future__ import annotations

import os
import threading

from support import CoordTestCase

STRESS_THREADS = min(8, max(2, int(os.environ.get("COORDFS_STRESS_THREADS", "8"))))
STRESS_CLAIMS = min(24, max(4, int(os.environ.get("COORDFS_STRESS_CLAIMS", "16"))))


class WindowsStressTests(CoordTestCase):
    def test_bounded_concurrent_claims_and_case_aliases(self) -> None:
        contended = "data:stress-contended"
        unique_ok = 0
        unique_lock = threading.Lock()
        contended_results: list[dict] = []
        contended_lock = threading.Lock()
        errors: list[str] = []

        def worker(index: int) -> None:
            nonlocal unique_ok
            try:
                unique = self.board.claim(f"data:stress-{index}", f"agent-{index}", ttl_s=30)
                if unique["status"] != "ok":
                    errors.append(f"unique claim failed: {unique}")
                    return
                with unique_lock:
                    unique_ok += 1
                path = self.cwd / f"CaseFile-{index}.TXT"
                path.write_text(str(index), encoding="utf-8")
                claimed = self.board.claim(str(path), f"path-agent-{index}", ttl_s=30, kind="path")
                if os.name == "nt":
                    # Windows-only: the filesystem and normcase collapse case
                    # aliases onto the one lease. POSIX normcase is identity,
                    # so the alias check cannot apply there.
                    alias = self.board.who(str(path).swapcase(), kind="path")
                    if claimed["status"] != "ok" or alias["holder_agent"] != claimed["holder_agent"]:
                        errors.append(f"path alias failed for {index}: {claimed} {alias}")
                elif claimed["status"] != "ok":
                    errors.append(f"path claim failed for {index}: {claimed}")
                result = self.board.claim(contended, f"contender-{index}", ttl_s=60)
                with contended_lock:
                    contended_results.append(result)
            except Exception as exc:  # pragma: no cover - surfaced as test failure
                errors.append(f"worker {index}: {exc}")

        threads = [
            threading.Thread(target=worker, args=(index,))
            for index in range(STRESS_THREADS)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())

        extra = 0
        for index in range(STRESS_THREADS, STRESS_CLAIMS):
            result = self.board.claim(f"data:stress-extra-{index}", "agent-extra", ttl_s=30)
            self.assertEqual(result["status"], "ok")
            extra += 1

        self.assertEqual(errors, [])
        self.assertEqual(unique_ok, STRESS_THREADS)
        winners = [item for item in contended_results if item["status"] == "ok"]
        warnings = [item for item in contended_results if item["status"] == "warning"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(warnings), STRESS_THREADS - 1)
        self.assertTrue(all(item["holder_agent"] == winners[0]["holder_agent"] for item in warnings))
        listed = self.board.ls()
        self.assertGreaterEqual(listed["count"], STRESS_THREADS + extra + 1)
