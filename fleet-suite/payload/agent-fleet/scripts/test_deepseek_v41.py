"""DeepSeek V4.1 Flash replaces V4 Flash 0731 as the Qwen low lane; synthetic only."""
import json
from pathlib import Path
import unittest

import fleet
import routing_v2


NEW = "deepseek-v4.1-flash"
OLD = "deepseek-v4-flash-0731"


class DeepSeekV41Tests(unittest.TestCase):
    def setUp(self):
        self.profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        self.qwen = self.profiles["qwen"]

    def test_low_lane_uses_v41_in_every_mode(self):
        self.assertEqual(self.qwen["execution_profile_map"]["low"], "qwen-deepseek-v4-1-flash")
        for mode, lanes in self.qwen["execution_profile_map_by_mode"].items():
            with self.subTest(mode=mode):
                self.assertEqual(lanes["low"], "qwen-deepseek-v4-1-flash")
        self.assertEqual(self.qwen["models"]["low"], NEW)

    def test_v41_profile_identity_and_lifecycle(self):
        row = self.qwen["execution_profiles"]["qwen-deepseek-v4-1-flash"]
        self.assertEqual(row["model"], NEW)
        self.assertEqual(row["runtime_model_id"], NEW)
        self.assertEqual(row["model_provider_id"], "deepseek")
        self.assertEqual(row["lifecycle"], "ACTIVE")
        self.assertTrue(routing_v2.lifecycle_allows_explicit_run(row["lifecycle"]))
        self.assertEqual(set(row["supported_modes"]), {"advisory", "review", "implement"})
        self.assertTrue(fleet.is_qwen_deepseek_v4_model(NEW))

    def test_resolver_selects_v41_for_low_complexity(self):
        for mode in ("advisory", "review", "implement"):
            with self.subTest(mode=mode):
                execution = fleet.resolve_execution_profile(
                    "qwen", self.qwen, complexity="low", mode=mode, required_tools=[],
                )
                self.assertEqual(execution["model"], NEW)

    def test_predecessor_stays_explicitly_runnable_during_retirement(self):
        row = self.qwen["execution_profiles"]["qwen-deepseek-v4-flash-0731"]
        self.assertEqual(row["model"], OLD)
        self.assertTrue(routing_v2.lifecycle_allows_explicit_run(row["lifecycle"]))
        execution = fleet.resolve_execution_profile(
            "qwen", self.qwen, complexity="low", mode="implement", required_tools=[],
            model_override=OLD,
        )
        self.assertEqual(execution["model"], OLD)

    def test_catalog_promotes_v41_and_deprecates_0731_with_retirement(self):
        catalog = json.loads((fleet.DEFAULT_PROFILES.parent / "model-catalog.json").read_text(encoding="utf-8"))
        rows = {row["model_id"]: row for row in catalog["models"]}
        self.assertEqual(rows[NEW]["lifecycle"], "active")
        self.assertEqual(rows[NEW]["tier"], "W")
        self.assertEqual(rows[OLD]["lifecycle"], "deprecated")
        self.assertEqual(rows[OLD]["replacement_id"], NEW)
        self.assertEqual(rows[OLD]["retire_after"], "2026-10-27")


if __name__ == "__main__":
    unittest.main()
