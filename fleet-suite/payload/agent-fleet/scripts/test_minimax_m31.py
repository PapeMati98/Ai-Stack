"""Successor routing preserves exact managed identity and legacy commands."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fleet
from test_minimax_terminal_failures import limit_exceeded, structured_failure, success_line

ALIAS = "minimax/MiniMax-M3.1-Flash-Preview"


class MiniMaxM31Tests(unittest.TestCase):
    def command(self, folder, model=ALIAS, effort="medium"):
        return fleet.build_command("minimax", "mcode.cmd", prompt="Synthetic check",
            workspace=Path(folder), mode="implement", model=model, complexity="low",
            timeout=60, run_dir=Path(folder), native_subagent_limit=0,
            enable_web=False, materialize=True, reasoning_level=effort)[0]

    def test_successor_command_config_effort_and_tool_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            cmd = self.command(folder)
            config = json.loads(Path(cmd[cmd.index("--config") + 1]).read_text())
            self.assertEqual(cmd[cmd.index("--model") + 1], ALIAS)
            self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")
            self.assertEqual(config["defaultModel"], ALIAS)
            self.assertEqual(config["agents"]["default"]["tools"], ["read", "grep", "glob", "write", "edit"])
            self.assertFalse(config["skills"]["external"]["enabled"])
            self.assertFalse(config["memory"]["enabled"])

    def test_legacy_is_explicit_and_flag_free(self):
        with tempfile.TemporaryDirectory() as folder:
            cmd = self.command(folder, "minimax/MiniMax-M3")
            self.assertEqual(cmd[cmd.index("--model") + 1], "minimax/MiniMax-M3")
            self.assertNotIn("--effort", cmd)

    def test_unsupported_model_and_effort_refuse(self):
        with tempfile.TemporaryDirectory() as folder:
            for model, effort in [("other/MiniMax-M3.1-Flash", "medium"), (ALIAS, "off")]:
                with self.subTest(model=model, effort=effort), self.assertRaises(fleet.FleetError):
                    self.command(folder, model, effort)

    def test_successor_envelope_and_exact_final_identity(self):
        body = json.loads(success_line())
        body["model"]["modelId"] = ALIAS.split("/")[1]
        text = json.dumps(body)
        self.assertTrue(fleet.minimax_terminal_envelope_completion(text))
        self.assertEqual(fleet.minimax_model_identity(text, expected_runtime_model=ALIAS)["status"], "verified")
        body["model"]["modelId"] = "MiniMax-M3"
        self.assertEqual(fleet.minimax_model_identity(json.dumps(body), expected_runtime_model=ALIAS)["status"], "mismatch")

    def test_failure_causes_preserved_for_successor(self):
        for factory, code in [(limit_exceeded, 7), (structured_failure, 4)]:
            body = factory()
            body["model"]["modelId"] = ALIAS.split("/")[1]
            result = fleet.minimax_terminal_envelope_completion(json.dumps(body))
            self.assertEqual(result.exit_code, code)
            body["model"]["providerKind"] = "custom"
            self.assertFalse(fleet.minimax_terminal_envelope_completion(json.dumps(body)))

    def test_profiles_select_successor_by_default_and_keep_old_profile(self):
        p = Path(__file__).parent.parent / "references/profiles.json"
        profile = json.loads(p.read_text())["profiles"]["minimax"]
        self.assertEqual(profile["readiness"]["minimum_version"], "0.6.12")
        self.assertIn("versions\\0.6.12\\mcode.cmd", profile["candidates"][0])
        for level in ("low", "medium", "high"):
            selected = profile["execution_profiles"][profile["execution_profile_map"][level]]
            self.assertEqual(selected["model"], "MiniMax-M3.1-Flash")
            self.assertEqual(selected["runtime_model_id"], ALIAS)
            self.assertEqual(selected["reasoning_level"], level)
        self.assertEqual(profile["execution_profiles"]["minimax-m3-thinking"]["model"], "MiniMax-M3")


if __name__ == "__main__":
    unittest.main()
