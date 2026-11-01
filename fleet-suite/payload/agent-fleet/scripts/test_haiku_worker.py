"""Focused regression for the private Haiku 5.5 Worker candidate (CANARY, Claude adapter)."""
import copy
import datetime as dt
import json
import unittest
from pathlib import Path

import fleet

HAIKU = "claude-haiku-5-5"
CANARY_PROFILE = "claude-haiku-5-5-worker"
PROFILE_PATH = Path(__file__).resolve().parents[1] / "references/profiles.json"
NOW = dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc)


def _profiles():
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))["profiles"]


def _restricted_assessment(model, expired=False):
    return {
        "data_handling": {
            "classification": "restricted",
            "anthropic_zdr_authorization": {
                "status": "authorized",
                "scope": "account",
                "models": [model],
                "verified_at": "2026-08-01T00:00:00Z" if expired else "2026-09-01T00:00:00Z",
                "expires_at": "2026-08-08T00:00:00Z" if expired else "2026-09-08T00:00:00Z",
                "evidence_ref": "provider-authorization:haiku-5-5",
            },
        }
    }


class HaikuWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profiles = _profiles()
        cls.profile = cls.profiles["claude"]
        cls.execution = cls.profile["execution_profiles"][CANARY_PROFILE]

    def test_exact_supported_id_membership(self):
        self.assertIn(HAIKU, fleet.routing_v2.ANTHROPIC_EXACT_ID_MODELS)
        self.assertEqual(fleet.routing_v2.ANTHROPIC_HAIKU_5_5_MODEL_ID, HAIKU)

    def test_fable_opus_invariants_preserved(self):
        self.assertIn("claude-fable-5-1", fleet.routing_v2.ANTHROPIC_EXACT_ID_MODELS)
        self.assertIn("claude-opus-5-5", fleet.routing_v2.ANTHROPIC_EXACT_ID_MODELS)
        self.assertIn("claude-fable-5-1", self.profile["data_policy"]["applies_to_models"])
        self.assertIn("claude-opus-5-5", self.profile["data_policy"]["applies_to_models"])

    def test_policy_missing_denial(self):
        gate = fleet.routing_v2.anthropic_retention_gate(
            self.execution, profile=self.profile, assessment={}, now=NOW
        )
        self.assertFalse(gate["passed"])

    def test_unknown_model_denied_without_current_account_authorization(self):
        unknown = fleet.routing_v2.anthropic_retention_gate(
            self.execution,
            profile=self.profile,
            assessment={"data_handling": {"classification": "unknown"}},
            now=NOW,
        )
        self.assertFalse(unknown["passed"])
        self.assertEqual(unknown["evidence"], "classification-required")

    def test_restricted_without_authorization_denied(self):
        gate = fleet.routing_v2.anthropic_retention_gate(
            self.execution,
            profile=self.profile,
            assessment={
                "data_handling": {
                    "classification": "restricted",
                    "anthropic_zdr_authorization": None,
                }
            },
            now=NOW,
        )
        self.assertFalse(gate["passed"])

    def test_internal_existing_model_policy_pass(self):
        public = fleet.routing_v2.anthropic_retention_gate(
            self.execution,
            profile=self.profile,
            assessment={
                "data_handling": {
                    "classification": "public",
                    "anthropic_zdr_authorization": None,
                }
            },
            now=NOW,
        )
        authorized = fleet.routing_v2.anthropic_retention_gate(
            self.execution,
            profile=self.profile,
            assessment=_restricted_assessment(HAIKU),
            now=NOW,
        )
        expired = fleet.routing_v2.anthropic_retention_gate(
            self.execution,
            profile=self.profile,
            assessment=_restricted_assessment(HAIKU, expired=True),
            now=NOW,
        )
        self.assertTrue(public["passed"])
        self.assertTrue(authorized["passed"])
        self.assertFalse(expired["passed"])

    def test_wrong_anthropic_runtime_identity_rejected(self):
        enforcing = copy.deepcopy(self.profile)
        enforcing.setdefault("readiness", {})["enforce_model_inventory"] = True
        wrong = fleet.routing_v2.runtime_model_visible(
            self.execution,
            profile=enforcing,
            readiness={"deep_probe": {"output": '[{"id": "claude-haiku-5", "ready": true}]'}},
        )
        self.assertFalse(wrong[0])
        exact = fleet.routing_v2.runtime_model_visible(
            self.execution,
            profile=enforcing,
            readiness={"deep_probe": {"output": f'[{{"id": "{HAIKU}", "ready": true}}]'}},
        )
        self.assertTrue(exact[0])

    def test_canary_profile_schema_and_command(self):
        execution = self.execution
        self.assertEqual(execution["lifecycle"], "LIMITED")
        self.assertEqual(execution["model"], HAIKU)
        self.assertEqual(execution["runtime_model_id"], HAIKU)
        self.assertEqual(execution["model_provider_id"], "anthropic")
        self.assertEqual(execution["reasoning_level"], "medium")
        self.assertEqual(execution["max_risk"], "low")
        self.assertEqual(execution["supported_modes"], ["implement"])
        self.assertEqual(execution["context_window_tokens"], 1000000)
        self.assertEqual(execution["capabilities"], {})
        self.assertEqual(execution["efficiency_priors"], {})
        implement_tools = self.profile["execution_profiles"][
            "claude-opus-5-5-high"
        ]["tools"]
        self.assertEqual(execution["tools"], implement_tools)
        self.assertNotIn(CANARY_PROFILE, self.profile["execution_profile_map"].values())
        for mode_map in self.profile["execution_profile_map_by_mode"].values():
            self.assertNotIn(CANARY_PROFILE, mode_map.values())
        self.assertNotIn(CANARY_PROFILE, self.profile["models"].values())
        command, _, _ = fleet.build_command(
            "claude",
            "synthetic-claude.exe",
            prompt="synthetic",
            workspace=Path.cwd(),
            mode="implement",
            model=execution["model"],
            complexity="low",
            timeout=30,
            run_dir=Path.cwd(),
            native_subagent_limit=0,
            enable_web=False,
            materialize=False,
            reasoning_level=execution["reasoning_level"],
            profile=self.profile,
        )
        self.assertEqual(command[command.index("--model") + 1], HAIKU)
        self.assertEqual(command[command.index("--effort") + 1], "medium")


if __name__ == "__main__":
    unittest.main()
