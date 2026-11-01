import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from command_adapter import expand_worker_run_ids


class CommandAdapterTests(unittest.TestCase):
    def test_worker_run_marker_repeats_run_id_flag(self):
        refs = [
            {"kind": "fleet_run", "id": "run-b"},
            {"kind": "fleet_run", "id": "run-a"},
            {"kind": "agent_loop", "id": "loop-a"},
        ]
        self.assertEqual(
            expand_worker_run_ids(["fleet.py", "--run-id", "{worker_run_ids}"], refs),
            ["fleet.py", "--run-id", "run-a", "--run-id", "run-b"],
        )

    def run_case(self, oracle_code=0, *, missing=False, fleet=False, run_status=None,
                 exit_code=1, disposition=None, agent="qwen", model="qwen3.8-max",
                 identity_status="verified", identity_expected=None, identity_observed=None,
                 provider_id=None, attested=True, source_ref_sets=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_ref_sets = source_ref_sets or []
            source_nodes = {
                f"source-{index}": {"external_refs": refs}
                for index, refs in enumerate(source_ref_sets)
            }
            context = {"node": {"node_id": "node-test", "executor": "fleet-run" if fleet else "parent-local",
                        "evidence_from": list(source_nodes), "write_scope": [],
                        "acceptance_criteria": [{"criterion_id": "crit-test"}]},
                       "state": {"nodes": source_nodes}, "dispatch_bounds": {"timeout": 10}}
            context_path = root / "context.json"
            context_path.write_text(json.dumps(context), encoding="utf-8")
            recipe = {"argv": [sys.executable, "-c", "print('{}')"],
                      "pinned_files": {sys.executable: hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()},
                      "oracles": {} if missing else {"crit-test": [sys.executable, "-c", f"raise SystemExit({oracle_code})"]},
                      "artifact_paths": []}
            if run_status is not None:
                record = root / "fleet-record.json"
                expected = identity_expected if identity_expected is not None else model
                record.write_text(json.dumps({"run_id": "run-test", "run_status": run_status,
                    "agent": agent, "model": model, "model_provider_id": provider_id,
                    "model_identity": {"status": identity_status, "expected_model": expected,
                                       "observed_models": identity_observed or []},
                    "runtime_attestation": {"status": "VERIFIED" if attested else "UNVERIFIED"},
                    "execution_workspace": str(root), "artifact_disposition": disposition}), encoding="utf-8")
                reply = json.dumps({"run_id": "run-test", "usage_record": str(record)})
                recipe["argv"] = [sys.executable, "-c", f"print({reply!r}); raise SystemExit({exit_code})"]
            recipe_path = root / "recipe.json"
            recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
            output = root / "result.json"
            env = dict(os.environ, AGENT_GRAPH_CONTEXT_PATH=str(context_path),
                       AGENT_GRAPH_ATTEMPT_ID="test-attempt", AGENT_GRAPH_EVIDENCE_PATH=str(output))
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            proc = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("command_adapter.py")), str(recipe_path)],
                                  cwd=root, env=env, capture_output=True, text=True, timeout=15, **options)
            return proc.returncode, json.loads(output.read_text()) if output.exists() else None

    def test_real_command_and_oracle_pass(self):
        code, output = self.run_case()
        self.assertEqual(code, 0)
        self.assertEqual(output["evidence"]["verdict"], "accepted")

    def test_inherited_external_refs_are_deduplicated_in_recipe_evidence(self):
        code, output = self.run_case(
            source_ref_sets=[
                [
                    {"kind": "fleet_run", "id": "run-shared"},
                    {"kind": "fleet_run", "id": "run-a"},
                ],
                [
                    {"kind": "fleet_run", "id": "run-shared"},
                    {"kind": "fleet_run", "id": "run-b"},
                ],
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            output["evidence"]["external_refs"],
            [
                {"kind": "fleet_run", "id": "run-shared"},
                {"kind": "fleet_run", "id": "run-a"},
                {"kind": "fleet_run", "id": "run-b"},
            ],
        )

    def test_failed_oracle_rejects_successful_command(self):
        code, output = self.run_case(oracle_code=1)
        self.assertEqual(code, 0)
        self.assertEqual(output["evidence"]["verdict"], "rejected")

    def test_missing_oracle_is_not_pass(self):
        code, output = self.run_case(missing=True)
        self.assertNotEqual(code, 0)
        self.assertIsNone(output)

    def test_fleet_exit_zero_without_receipt_is_not_pass(self):
        code, output = self.run_case(fleet=True)
        self.assertNotEqual(code, 0)
        self.assertIsNone(output)

    def test_partial_candidate_with_passing_oracle_can_reach_reviewer(self):
        code, output = self.run_case(fleet=True, run_status="PARTIAL")
        self.assertEqual(code, 0)
        self.assertEqual(output["evidence"]["verdict"], "accepted")
        self.assertIn("PARTIAL", output["evidence"]["summary"])

    def test_partial_candidate_with_failing_oracle_is_rejected(self):
        code, output = self.run_case(fleet=True, run_status="PARTIAL", oracle_code=1)
        self.assertEqual(code, 0)
        self.assertEqual(output["evidence"]["verdict"], "rejected")

    def test_partial_does_not_mask_unexpected_process_crash(self):
        code, output = self.run_case(fleet=True, run_status="PARTIAL", exit_code=137)
        self.assertEqual(code, 0)
        self.assertEqual(output["evidence"]["verdict"], "rejected")

    def test_failed_or_quarantined_fleet_record_is_not_pass(self):
        for status, disposition in [("FAILED", None), ("PARTIAL", {"status": "quarantined"})]:
            with self.subTest(status=status, disposition=disposition):
                code, output = self.run_case(fleet=True, run_status=status, disposition=disposition)
                self.assertNotEqual(code, 0)
                self.assertIsNone(output)

    def test_codex_first_party_identity_exception_requires_model_attestation(self):
        code, output = self.run_case(fleet=True, run_status="COMPLETED", exit_code=0,
                                     agent="codex", model="gpt-5.6-luna",
                                     identity_status="not-applicable")
        self.assertEqual(code, 0)
        self.assertEqual(output["evidence"]["verdict"], "accepted")

        code, output = self.run_case(fleet=True, run_status="COMPLETED", agent="codex",
                                     model="gpt-5.6-luna", identity_status="not-applicable",
                                     identity_expected="gpt-6-astra")
        self.assertNotEqual(code, 0)
        self.assertIsNone(output)

    def test_external_unverified_identity_stays_blocked(self):
        code, output = self.run_case(fleet=True, run_status="COMPLETED", agent="qwen",
                                     model="qwen3.8-max", identity_status="not-applicable",
                                     provider_id="moonshot")
        self.assertNotEqual(code, 0)
        self.assertIsNone(output)

    def test_kimi_config_attested_identity_is_accepted(self):
        code, output = self.run_case(fleet=True, run_status="COMPLETED", exit_code=0,
                                     agent="kimi", model="k3", provider_id="moonshot",
                                     identity_status="not-applicable")
        self.assertEqual(code, 0)
        self.assertEqual(output["evidence"]["verdict"], "accepted")

    def test_kimi_config_attestation_rejects_missing_or_conflicting_evidence(self):
        cases = [
            ("wrong expected model", {"identity_expected": "other"}),
            ("missing runtime attestation", {"attested": False}),
            ("wrong provider", {"provider_id": "other"}),
            ("mismatch status", {"identity_status": "mismatch"}),
            ("conflicting observed model", {"identity_observed": ["other"]}),
            ("empty expected model", {"identity_expected": ""}),
        ]
        for name, kwargs in cases:
            with self.subTest(name=name):
                options = dict(fleet=True, run_status="COMPLETED", exit_code=0,
                               agent="kimi", model="k3", provider_id="moonshot",
                               identity_status="not-applicable")
                options.update(kwargs)
                code, output = self.run_case(**options)
                self.assertNotEqual(code, 0)
                self.assertIsNone(output)


if __name__ == "__main__":
    unittest.main()
