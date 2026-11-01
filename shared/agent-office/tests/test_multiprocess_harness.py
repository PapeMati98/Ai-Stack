"""Fast process-level checks for the full coordfs acceptance harness."""

from __future__ import annotations

from multiprocess_harness import (
    run_atomic_file_stress,
    run_claim_stress,
    run_crash_ttl_recovery,
)
from support import CoordTestCase


class MultiprocessHarnessTests(CoordTestCase):
    def test_distinct_processes_have_exactly_one_claim_winner(self) -> None:
        result = run_claim_stress(self.home, self.cwd, claimers=12, timeout_s=90)

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["winner_count"], 1)
        self.assertEqual(result["warning_count"], 11)
        self.assertEqual(result["unique_pids"], 12)

    def test_two_process_atomic_file_harness_has_no_unexpected_errors(self) -> None:
        result = run_atomic_file_stress(
            self.cwd / "atomic-smoke",
            process_count=2,
            file_count=5,
            iterations=20,
            timeout_s=90,
        )

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["attempts"], 200)
        self.assertEqual(result["unique_pids"], 2)
        self.assertEqual(result["unexpected_error_count"], 0)

    def test_crashed_holder_expires_and_is_reclaimed(self) -> None:
        result = run_crash_ttl_recovery(self.home, self.cwd)

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["reclaim_code"], "reclaimed")
