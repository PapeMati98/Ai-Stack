import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import planner_witness as witness
import plan_contract as plan


class PlannerWitnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prompt, self.plan = "a" * 64, "b" * 64
        self.marker = "PLAN_REVIEWED " + self.prompt + " " + self.plan

    def verify(self, data, kind="fleet"):
        source = self.root / "source.json"
        source.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        path = self.root / "witness.json"
        path.write_text(json.dumps({"kind": kind, "source": str(source),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}), encoding="utf-8")
        return witness.verify(path, self.prompt, self.plan)

    def record(self):
        return {"model": "qwen3.8-max", "run_id": "run-test", "run_status": "COMPLETED",
                "model_identity": {"status": "verified", "observed_models": ["qwen3.8-max-0902"]},
                "runtime_attestation": {"status": "VERIFIED"}, "handoff": {"summary": self.marker}}

    def codex_record(self):
        return {"adapter": "codex", "model": "gpt-6-astra", "run_id": "run-codex-test",
                "run_status": "COMPLETED", "execution_profile_id": "codex-gpt-6-astra",
                "provider_attempts": [{"command": ["codex.cmd", "exec", "--model", "gpt-6-astra"]}],
                "runtime_attestation": {"status": "VERIFIED"},
                "handoff": {"summary": self.marker}}

    def test_real_receipt_shape(self):
        self.assertEqual(self.verify(self.record())["status"], "VERIFIED")

    def test_codex_fleet_receipt_binds_provider_command(self):
        self.assertEqual(self.verify(self.codex_record())["model"], "gpt-6-astra")
        row = self.codex_record()
        row["provider_attempts"][0]["command"][-1] = "gpt-5.6-luna"
        with self.assertRaises(ValueError):
            self.verify(row)

    def test_bad_receipts(self):
        for key, value in (("run_status", "PARTIAL"), ("model", "gpt-5.6-luna"),
                           ("model_identity", {"status": "unknown"}), ("handoff", {"summary": "Nice plan"})):
            with self.subTest(key=key):
                row = self.record()
                row[key] = value
                with self.assertRaises(ValueError):
                    self.verify(row)

    def test_native_parent_and_wrong_role(self):
        rows = [{"type": "session_meta", "payload": {"id": "native-test"}},
                {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": self.marker}]}}]
        self.assertEqual(self.verify("\n".join(map(json.dumps, rows)), "codex-native")["model"], "gpt-6-astra")
        rows[2]["payload"]["role"] = "user"
        with self.assertRaises(ValueError):
            self.verify("\n".join(map(json.dumps, rows)), "codex-native")

    def test_cli_requires_full_planner_proof(self):
        args = plan.build_parser().parse_args(["approve", "--contract", "c.json", "--output", "a.json", "--approved-by", "parent-architect"])
        self.assertTrue(args.require_planner_witness)

    def test_claude_parent_requires_native_assistant_model(self):
        row = {"type": "assistant", "sessionId": "claude-native-test", "message": {
            "role": "assistant", "model": "claude-fable-5.1", "content": [{"type": "text", "text": self.marker}]}}
        self.assertEqual(self.verify(json.dumps(row), "claude-native")["model"], "claude-fable-5.1")
        row["message"]["model"] = "claude-sonnet-5"
        with self.assertRaises(ValueError):
            self.verify(json.dumps(row), "claude-native")

    def test_model_policy(self):
        for model in ("gpt-6-astra", "claude-fable-5.1", "qwen3.8-max"):
            self.assertTrue(witness.qualified(model))
        for model in ("gpt-5.6-luna", "qwen3.8-27b", "kimi-k2.7", "claude-haiku"):
            self.assertFalse(witness.qualified(model))

    def test_kimi_k31_pro_is_a_qualified_planner(self):
        # Owner decision 2026-10-24: Kimi K3.1 Pro replaces the deprecated K3.
        for model in ("k3.1-pro", "kimi-k3.1-pro", "kimi-code/k3.1-pro",
                      "moonshot/k3.1-pro", "moonshot/kimi-k3.1-pro"):
            with self.subTest(model=model):
                self.assertTrue(witness.qualified(model))

    def test_deprecated_k3_is_not_a_qualified_planner(self):
        for model in ("k3", "kimi-k3", "kimi-k3-max", "moonshot/kimi-k3"):
            with self.subTest(model=model):
                self.assertFalse(witness.qualified(model))

    def test_kimi_k31_pro_lineage_is_moonshot(self):
        for model in ("k3.1-pro", "kimi-k3.1-pro"):
            with self.subTest(model=model):
                self.assertEqual(witness.model_lineage(model), "moonshot")


if __name__ == "__main__":
    unittest.main()
