import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("resolve_roster.py")
SPEC = importlib.util.spec_from_file_location("resolve_roster", MODULE_PATH)
ROSTER = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ROSTER)

POLICY_PATH = Path(__file__).parents[1] / "references" / "roster-policy.json"


def model(model_id, created, name=None):
    return {
        "id": model_id,
        "name": name or model_id,
        "canonical_slug": f"{model_id}-snapshot",
        "created": created,
        "expiration_date": None,
        "supported_parameters": ["max_tokens", "reasoning_effort"],
    }


def current_catalog():
    return {
        "data": [
            model("anthropic/claude-fable-5", 1781007515),
            model("anthropic/claude-opus-4.8", 1779905091),
            model("anthropic/claude-opus-5", 1784912544),
            model("anthropic/claude-opus-5-fast", 1784912546),
            model("anthropic/claude-opus-5:batch", 1784912544),
            model("qwen/qwen3.7-max", 1779376861),
            model("qwen/qwen3.8-max", 1785731612),
            model("moonshotai/kimi-k2.6", 1776699402),
            model("moonshotai/kimi-k2.7-code", 1781266361),
            model("moonshotai/kimi-k3", 1784215858),
            model("~moonshotai/kimi-latest", 1777318428),
            model("tencent/hy3-preview", 1782000000),
            model("tencent/hy4-preview", 1787897375),
            model("z-ai/glm-5.2", 1781631930),
            model("deepseek/deepseek-v4-pro", 1777000679),
            model("deepseek/deepseek-v4-flash", 1777000666),
            model("deepseek/deepseek-v4-flash-0731", 1785478908),
            model("meta/muse-spark-1.1", 1784215741),
            model("meta/muse-spark-1.2", 1785959287),
            model("x-ai/grok-4.3", 1777591821),
            model("x-ai/grok-4.5", 1783523154),
            model("stealth/ox-alpha", 1787256295, "Ox Alpha"),
            model("nvidia/nemotron-3-ultra-550b-a55b:free", 1780551208),
            model("mistralai/mistral-medium-3-5", 1777570439),
            model("google/gemini-3.5-flash", 1779193800),
            model("google/gemini-3.6-flash", 1784646733),
            model("sakana/fugu-ultra", 1782276303),
        ]
    }


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog_path = self.root / "catalog.json"
        self.catalog_path.write_text(json.dumps(current_catalog()), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def args(self, **overrides):
        values = {
            "policy": str(POLICY_PATH),
            "state_root": str(self.root / "state"),
            "mode": "default",
            "custom_model": [],
            "include_role": [],
            "exclude_role": [],
            "refresh_catalog": False,
            "skip_endpoint_probe": True,
            "catalog_file": str(self.catalog_path),
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_default_roster_uses_current_models(self):
        result = ROSTER.resolve(self.args())
        self.assertEqual(len(result["panel"]), 9)
        selected = {row["role"]: row["model"] for row in result["panel"]}
        self.assertEqual(selected["principal"], "anthropic/claude-opus-5")
        self.assertEqual(selected["execution"], "qwen/qwen3.8-max")
        self.assertEqual(selected["long_context"], "moonshotai/kimi-k3")
        self.assertEqual(selected["systems"], "tencent/hy4-preview")
        self.assertEqual(selected["correctness"], "deepseek/deepseek-v4-flash-0731")
        self.assertEqual(selected["independent"], "meta/muse-spark-1.2")
        self.assertEqual(selected["contrarian"], "x-ai/grok-4.5")
        self.assertNotIn("stealth/ox-alpha", selected.values())
        self.assertNotIn("deepseek/deepseek-v4-pro", selected.values())
        self.assertNotIn("tencent/hy3-preview", selected.values())

    def test_newer_family_member_beats_current_preference(self):
        catalog = current_catalog()
        catalog["data"].append(model("qwen/qwen4-max", 1800000000))
        catalog["data"].append(model("deepseek/deepseek-v5-flash", 1800000001))
        catalog["data"].append(model("x-ai/grok-5", 1800000002))
        catalog["data"].append(model("anthropic/claude-opus-6", 1800000003))
        catalog["data"].append(model("moonshotai/kimi-k4-max", 1800000004))
        catalog["data"].append(model("tencent/hy5", 1800000005))
        self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        result = ROSTER.resolve(self.args())
        selected = {row["role"]: row["model"] for row in result["panel"]}
        self.assertEqual(selected["execution"], "qwen/qwen4-max")
        self.assertEqual(selected["correctness"], "deepseek/deepseek-v5-flash")
        self.assertEqual(selected["contrarian"], "x-ai/grok-5")
        self.assertEqual(selected["principal"], "anthropic/claude-opus-6")
        self.assertEqual(selected["long_context"], "moonshotai/kimi-k4-max")
        self.assertEqual(selected["systems"], "tencent/hy5")

    def test_old_generation_is_below_policy_floor(self):
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        rows, _ = ROSTER.selector_candidates(
            [model("qwen/qwen3.7-max", 1779376861)],
            policy["roles"]["execution"]["selector"],
            {"models": {}},
            set(),
        )
        self.assertEqual(rows, [])

        hy_rows, _ = ROSTER.selector_candidates(
            [model("tencent/hy3-preview", 1782000000)],
            policy["roles"]["systems"]["selector"],
            {"models": {}},
            set(),
        )
        self.assertEqual(hy_rows, [])

    def test_opus_and_kimi_variants_are_not_silent_aliases(self):
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        opus_rows, _ = ROSTER.selector_candidates(
            [
                model("anthropic/claude-opus-5-fast", 1784912546),
                model("anthropic/claude-opus-5:batch", 1784912544),
                model("~anthropic/claude-opus-latest", 1776795361),
            ],
            policy["roles"]["principal"]["selector"],
            {"models": {}},
            set(),
        )
        kimi_rows, _ = ROSTER.selector_candidates(
            [
                model("moonshotai/kimi-k2.7-code", 1781266361),
                model("~moonshotai/kimi-latest", 1777318428),
            ],
            policy["roles"]["long_context"]["selector"],
            {"models": {}},
            set(),
        )
        self.assertEqual(opus_rows, [])
        self.assertEqual(kimi_rows, [])

    def test_quick_stays_compact_while_default_has_opus_and_kimi(self):
        quick = ROSTER.resolve(self.args(mode="quick"))
        default = ROSTER.resolve(self.args(mode="default"))
        quick_roles = {row["role"] for row in quick["panel"]}
        default_roles = {row["role"] for row in default["panel"]}
        self.assertNotIn("principal", quick_roles)
        self.assertNotIn("long_context", quick_roles)
        self.assertIn("principal", default_roles)
        self.assertIn("long_context", default_roles)

    def test_full_roster_upgrades_gemini_flash(self):
        result = ROSTER.resolve(self.args(mode="full"))
        self.assertEqual(len(result["panel"]), 12)
        selected = {row["role"]: row["model"] for row in result["panel"]}
        self.assertEqual(selected["business"], "google/gemini-3.6-flash")

    def test_retired_ox_alpha_is_rejected_in_custom_panel(self):
        with self.assertRaisesRegex(ROSTER.RosterError, "Custom model is retired"):
            ROSTER.resolve(self.args(custom_model=["stealth/ox-alpha"]))

    def test_unknown_custom_model_fails_before_inference(self):
        with self.assertRaises(ROSTER.RosterError):
            ROSTER.resolve(self.args(custom_model=["missing/model"]))

    def test_two_failures_quarantine_model(self):
        record_args = argparse.Namespace(
            policy=str(POLICY_PATH),
            state_root=str(self.root / "state"),
            model="qwen/qwen3.8-max",
            success=False,
            failure=True,
            failure_kind="hard",
        )
        ROSTER.record_health(record_args)
        result = ROSTER.record_health(record_args)
        self.assertIsNotNone(result["quarantined_until"])
        health = ROSTER.load_health(self.root / "state")
        quarantined, _ = ROSTER.is_quarantined(
            "qwen/qwen3.8-max", health, ROSTER.utc_now()
        )
        self.assertTrue(quarantined)

    def test_concurrent_failures_do_not_lose_updates(self):
        from concurrent.futures import ThreadPoolExecutor

        record_args = argparse.Namespace(
            policy=str(POLICY_PATH),
            state_root=str(self.root / "concurrent-state"),
            model="meta/muse-spark-1.2",
            success=False,
            failure=True,
            failure_kind="transient",
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: ROSTER.record_health(record_args), range(8)))
        health = ROSTER.load_health(self.root / "concurrent-state")
        row = health["models"]["meta/muse-spark-1.2"]
        self.assertEqual(row["consecutive_failures"], 8)


if __name__ == "__main__":
    unittest.main()
