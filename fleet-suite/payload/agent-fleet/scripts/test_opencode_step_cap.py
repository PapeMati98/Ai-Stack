"""OpenCode run-scoped step ceiling prevents unbounded context growth."""

import json
import unittest

import fleet


class OpenCodeStepCapTests(unittest.TestCase):
    def test_advisory_and_review_have_bounded_agent_steps(self):
        advisory = json.loads(fleet.opencode_runtime_config(mode="advisory"))
        review = json.loads(fleet.opencode_runtime_config(mode="review"))
        self.assertEqual(advisory["agent"]["build"]["steps"], 8)
        self.assertEqual(review["agent"]["build"]["steps"], 16)

    def test_implementation_has_larger_but_finite_ceiling(self):
        config = json.loads(fleet.opencode_runtime_config(mode="implement"))
        self.assertEqual(config["agent"]["build"]["steps"], 32)

    def test_invalid_mode_is_refused_instead_of_running_without_cap(self):
        with self.assertRaises(fleet.FleetError):
            fleet.opencode_runtime_config(mode="unexpected")


if __name__ == "__main__":
    unittest.main()
