"""Criterion 9: 100 processes writing to one inbox, zero corruption. Full load is run_office_stress.py."""

from __future__ import annotations

import os

from office_support import OfficeTestCase
from office_stress_harness import run_inbox_stress

STRESS_SENDERS = max(2, int(os.environ.get("OFFICE_STRESS_SENDERS", "100")))
STRESS_PER_SENDER = max(1, int(os.environ.get("OFFICE_STRESS_PER_SENDER", "3")))


class InboxStressTests(OfficeTestCase):
    def test_many_processes_append_to_one_inbox_without_corruption(self) -> None:
        receipt = run_inbox_stress(self.home, senders=STRESS_SENDERS, per_sender=STRESS_PER_SENDER)
        self.assertTrue(receipt["passed"], receipt)
        self.assertEqual(receipt["unique_pids"], STRESS_SENDERS)
        self.assertEqual(receipt["inbox"]["parsed"], STRESS_SENDERS * STRESS_PER_SENDER)
        self.assertEqual(receipt["inbox"]["malformed"], 0)
        self.assertEqual(receipt["inbox"]["trailing_partial_bytes"], 0)
