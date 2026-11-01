"""Regression tests for fail-closed owner-repair approval defaults."""

import unittest

from dots_farm import engine as engine_module


class StorageBoundaryReached(Exception):
    """Sentinel proving the test reached only its mock storage boundary."""


class StoreProbe:
    def __init__(self):
        self.calls = []

    def get_dot(self, dot_id):
        self.calls.append("get_dot")
        raise StorageBoundaryReached

    def claim(self, *args, **kwargs):
        self.calls.append("claim")
        raise StorageBoundaryReached


class OwnerRepairDefaultTests(unittest.TestCase):
    def setUp(self):
        self.original_reference = engine_module.OWNER_REPAIR_APPROVAL_REFERENCE
        self.assertIsNone(
            self.original_reference,
            "owner-repair approval reference must default to None",
        )
        engine_module.OWNER_REPAIR_APPROVAL_REFERENCE = None
        self.store = StoreProbe()
        self.engine = object.__new__(engine_module.Engine)
        self.engine.store = self.store

    def tearDown(self):
        engine_module.OWNER_REPAIR_APPROVAL_REFERENCE = self.original_reference

    def test_default_none_rejects_missing_reference_before_store(self):
        self.assertIsNone(engine_module.OWNER_REPAIR_APPROVAL_REFERENCE)
        with self.assertRaises(PermissionError):
            self.engine.resume_classification(
                "dot-test-1", "repair-test-1", approval_reference=None
            )
        self.assertEqual(self.store.calls, [])

    def test_default_and_mismatch_reject_before_store(self):
        self.assertIsNone(engine_module.OWNER_REPAIR_APPROVAL_REFERENCE)
        for reference in (
            "",
            "not-a-grant",
            "EXAMPLE-OWNER-APPROVAL-NOT-A-GRANT",
        ):
            with self.subTest(reference_class="empty" if not reference else "nonempty"):
                with self.assertRaises(PermissionError):
                    self.engine.resume_classification(
                        "dot-test-1", "repair-test-1", approval_reference=reference
                    )
                self.assertEqual(self.store.calls, [])

        expected = "synthetic-test-only-expected-grant"
        engine_module.OWNER_REPAIR_APPROVAL_REFERENCE = expected
        with self.assertRaises(PermissionError):
            self.engine.resume_classification(
                "dot-test-1",
                "repair-test-1",
                approval_reference="synthetic-test-only-mismatched-grant",
            )
        self.assertEqual(self.store.calls, [])

    def test_only_explicit_test_grant_reaches_mock_store(self):
        self.assertIsNone(engine_module.OWNER_REPAIR_APPROVAL_REFERENCE)
        synthetic_grant = "synthetic-test-only-owner-grant"
        engine_module.OWNER_REPAIR_APPROVAL_REFERENCE = synthetic_grant
        with self.assertRaises(StorageBoundaryReached):
            self.engine.resume_classification(
                "dot-test-1", "repair-test-1", approval_reference=synthetic_grant
            )
        self.assertEqual(self.store.calls, ["get_dot"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
