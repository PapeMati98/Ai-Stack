"""Offline Graph qualification using the installed canonical Fleet resolver."""
import copy
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import todo_fleet


class MuseProfileResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fleet = todo_fleet.load_fleet()
        cls.profiles = cls.fleet.load_profiles(cls.fleet.DEFAULT_PROFILES)
        path = os.environ.get("RSI_TEST_BRIDGE_PATH")
        cls.bridge = todo_fleet
        if path:
            spec = importlib.util.spec_from_file_location("muse_baseline_bridge", path)
            cls.bridge = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = cls.bridge
            spec.loader.exec_module(cls.bridge)

    def exercise(self, *, role, complexity, model, profiles=None, quota=None):
        # Fixtures are retained for inspection; no cleanup touches shared paths.
        root = Path(tempfile.mkdtemp(prefix="muse-profile-", dir=os.environ.get("TEAM_TEST_ROOT")))
        (root / "answer.txt").write_text("42\n", encoding="utf-8")
        candidate = {"agent": "muse", "model": model, "score": 100, "reason": "Exact-model fixture"}
        task = {
            "id": "muse-profile", "text": "Read the synthetic answer.",
            "mode": "advisory", "scope": ["answer.txt"], "done_when": ["answer is 42"],
            "maker": [candidate], "reviewer": [candidate],
            "complexity": complexity, "review_complexity": complexity,
        }
        request = {"goal": "Exact Muse profile", "role": role, "workspace": str(root), "task": task}
        out = root / "out"
        out.mkdir()
        quota = quota or {"status": "unknown", "headroom": None, "hard_blocking": False}
        selected = profiles or self.profiles
        f = self.fleet
        with patch.dict(os.environ, {"AGENT_GRAPH_TIER_ROUTING": "0", "AGENT_GRAPH_WATCH_EVENTS": "0"}), \
             patch.object(f, "load_profiles", return_value=selected), \
             patch.object(f, "state_root", return_value=root / "state"), \
             patch.object(f, "live_readiness", side_effect=lambda p, **kw: ({k: {"ready": True} for k in p}, {})), \
             patch.object(f, "current_quota_views", side_effect=lambda r, p: ({k: quota for k in p}, {})), \
             patch.object(f, "execute_single", return_value={"status": "COMPLETED", "model": model, "run_id": "synthetic"}) as native:
            result = self.bridge.execute(request, out, fleet=f)
        return native.call_args_list, result

    def test_full_review_and_advisory_preserve_exact_model_mode_and_effort(self):
        for role, mode in (("maker", "advisory"), ("reviewer", "review")):
            for complexity, effort in (("low", "low"), ("medium", "medium"), ("high", "high")):
                with self.subTest(role=role, complexity=complexity):
                    calls, _ = self.exercise(role=role, complexity=complexity, model="muse-wonder-full-1.0")
                    self.assertEqual(len(calls), 1, "Exact Full model must reach native dispatch without substitution")
                    args = calls[0].kwargs
                    execution = args["execution_profile"]
                    self.assertEqual(args["agent_id"], "muse")
                    self.assertEqual(args["model_override"], "muse-wonder-full-1.0")
                    self.assertEqual(execution["model"], "muse-wonder-full-1.0")
                    self.assertEqual(execution["reasoning_level"], effort)
                    self.assertIn(mode, execution["supported_modes"])
                    self.assertNotIn("filesystem-write", execution["tools"])

    def test_default_spark_and_wonder_models_remain_in_their_read_lanes(self):
        for model in ("muse-spark-1.3", "muse-wonder-1.0"):
            with self.subTest(model=model):
                calls, _ = self.exercise(role="reviewer", complexity="high", model=model)
                self.assertEqual(len(calls), 1)
                execution = calls[0].kwargs["execution_profile"]
                self.assertEqual(execution["model"], model)
                self.assertEqual(execution["reasoning_level"], "high")
                self.assertIn("review", execution["supported_modes"])

    def test_missing_compatible_lane_refuses_before_native_dispatch(self):
        profiles = copy.deepcopy(self.profiles)
        muse = profiles["muse"]
        muse["execution_profiles"] = {
            k: v for k, v in muse["execution_profiles"].items()
            if v["model"] != "muse-wonder-full-1.0" or "review" not in v["supported_modes"]
        }
        calls, result = self.exercise(role="reviewer", complexity="high", model="muse-wonder-full-1.0", profiles=profiles)
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "BLOCKED")

    def test_contributor_override_is_refused_before_native_dispatch(self):
        profiles = copy.deepcopy(self.profiles)
        muse = profiles["muse"]
        row = copy.deepcopy(muse["execution_profiles"]["muse-wonder-full-1-0-read-high"])
        row["model"] = "muse-wonder-full-1.0-contributor"
        muse["execution_profiles"]["synthetic-contributor"] = row
        calls, result = self.exercise(role="reviewer", complexity="high", model=row["model"], profiles=profiles)
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "BLOCKED")

    def test_exhausted_quota_is_still_a_hard_gate(self):
        calls, result = self.exercise(role="reviewer", complexity="high", model="muse-wonder-full-1.0",
                                      quota={"status": "fresh", "headroom": 0.0, "hard_blocking": True})
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
