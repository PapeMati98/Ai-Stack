"""Owner-retained Explore plan and canonical successor Worker."""
import json
import unittest
from pathlib import Path


class CatalogMiniMaxM31Tests(unittest.TestCase):
    def test_successor_worker_and_legacy_history(self):
        p = Path(__file__).parent.parent / "references/model-catalog.json"
        d = json.loads(p.read_text())
        rows = {m["model_id"]: m for m in d["models"]}
        self.assertEqual(d["policy"]["plan_weights"]["minimax"], 55)
        for name in ["MiniMax-M3", "MiniMax-M3.1-Flash"]:
            self.assertNotIn("retire_after", rows[name])
            self.assertEqual(rows[name]["tier"], "W")
        self.assertEqual(rows["MiniMax-M3.1-Flash"]["lifecycle"], "active")
        self.assertEqual(rows["MiniMax-M3"]["lifecycle"], "deprecated")
        self.assertNotIn("TEMPORARY squeeze", d["policy"]["plan_weights_unit"])
        self.assertIn("Monthly M Plan Explore", rows["MiniMax-M3.1-Flash"]["notes"])


if __name__ == "__main__":
    unittest.main()
