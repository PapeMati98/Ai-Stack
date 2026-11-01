"""Static contract checks for the capability ledger; not a live runtime test."""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path
from typing import Any


OFFICE_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = OFFICE_ROOT / "capability-matrix.json"
README_PATH = OFFICE_ROOT / "README.md"

EXPECTED_CAPABILITIES = {
    "discovery",
    "send_read",
    "pending_wait",
    "progress_cancel",
    "provider_session_resume",
    "new_turn",
    "action_event_capture",
    "ended_turn_wake",
}
ALLOWED_STATUSES = {"verified", "unverified", "unsupported", "unknown", "deferred"}
EXPECTED_PARTICIPANTS = {
    "codex": "runtime",
    "claude-code": "runtime",
    "kimi-code": "runtime",
    "opencode": "runtime",
    "zcode": "runtime",
    "minimax-code": "runtime",
    "antigravity": "runtime",
    "grok-cli": "runtime",
    "qwen-code": "runtime",
    "human-mateusz": "human",
}
EXPECTED_DEFERRED_BOUNDARIES = {
    "remote-hermes",
    "scheduler",
    "voice",
    "phone",
    "real-mail",
    "customer-data",
    "network-exposure",
}


class CapabilityMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix: dict[str, Any] = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
        cls.readme = README_PATH.read_text(encoding="utf-8")

    def test_schema_shape_and_unique_participant_ids(self) -> None:
        matrix = self.matrix
        self.assertEqual(matrix["schema_version"], 1)
        self.assertRegex(matrix["matrix_version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(set(matrix["status_values"]), ALLOWED_STATUSES)
        self.assertEqual(set(matrix["capability_categories"]), EXPECTED_CAPABILITIES)
        self.assertEqual(len(matrix["capability_categories"]), len(EXPECTED_CAPABILITIES))

        participants = matrix["participants"]
        self.assertIsInstance(participants, list)
        participant_ids = [participant["id"] for participant in participants]
        self.assertEqual(len(participant_ids), len(set(participant_ids)), "participant ids must be unique")
        self.assertEqual(set(participant_ids), set(EXPECTED_PARTICIPANTS))
        self.assertEqual(
            {participant["id"]: participant["kind"] for participant in participants},
            EXPECTED_PARTICIPANTS,
        )

        office = matrix["office"]
        self.assertEqual(office["shared_home_template"], r"%USERPROFILE%\.agents\coord\office")
        self.assertEqual(
            office["installed_cli_path_template"],
            r"%USERPROFILE%\.agents\runtime\agent-office\office.cmd",
        )
        self.assertIn(" mcp", office["stdio_mcp_entrypoint"])

    def test_every_capability_is_separate_and_has_explicit_evidence_state(self) -> None:
        matrix = self.matrix
        evidence_catalog = matrix["evidence_catalog"]
        self.assertIsInstance(evidence_catalog, dict)

        capability_sets = [matrix["transport_capabilities"]]
        capability_sets.extend(participant["capabilities"] for participant in matrix["participants"])
        for capabilities in capability_sets:
            self.assertEqual(set(capabilities), EXPECTED_CAPABILITIES)
            for name in EXPECTED_CAPABILITIES:
                state = capabilities[name]
                self.assertEqual(set(state), {"status", "evidence", "basis"})
                self.assertIn(state["status"], ALLOWED_STATUSES)
                self.assertIsInstance(state["evidence"], list)
                self.assertTrue(state["evidence"], name)
                self.assertTrue(all(reference in evidence_catalog for reference in state["evidence"]))
                self.assertIsInstance(state["basis"], str)
                self.assertTrue(state["basis"].strip())

        # Resolve source-test evidence to real classes/methods so the matrix
        # cannot silently point at a renamed or removed test.
        tests_root = (OFFICE_ROOT / "tests").resolve()
        for evidence_id, evidence in evidence_catalog.items():
            if evidence.get("kind") != "source_test":
                continue
            with self.subTest(source_test=evidence_id):
                target = (OFFICE_ROOT / evidence["path"]).resolve()
                self.assertTrue(target.is_relative_to(tests_root))
                self.assertTrue(target.is_file())
                tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
                case_parts = evidence["case"].split(".")
                self.assertIn(len(case_parts), (1, 2))
                class_name = case_parts[0]
                classes = {
                    node.name: node
                    for node in tree.body
                    if isinstance(node, ast.ClassDef)
                }
                self.assertIn(class_name, classes)
                if len(case_parts) == 2:
                    method_names = {
                        node.name
                        for node in classes[class_name].body
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    }
                    self.assertIn(case_parts[1], method_names)

    def test_verified_values_have_source_test_evidence_not_copied_skill_proof(self) -> None:
        matrix = self.matrix
        evidence_catalog = matrix["evidence_catalog"]
        rules = matrix["proof_rules"]
        self.assertTrue(rules["copied_skill_is_not_native_discovery_evidence"])
        self.assertTrue(rules["transport_delivery_is_not_provider_session_resume_evidence"])
        self.assertTrue(rules["source_test_is_not_a_live_provider_client_receipt"])
        self.assertTrue(rules["unverified_must_not_be_reported_as_available"])

        verified_states = [
            state
            for capability_set in [matrix["transport_capabilities"]]
            + [participant["capabilities"] for participant in matrix["participants"]]
            for state in capability_set.values()
            if state["status"] == "verified"
        ]
        self.assertTrue(verified_states, "the local transport needs explicit source-test evidence")
        for state in verified_states:
            kinds = {evidence_catalog[reference]["kind"] for reference in state["evidence"]}
            self.assertIn("source_test", kinds)
            self.assertNotIn("copied_skill", kinds)
            self.assertNotIn("copied skill", state["basis"].lower())

        for participant in matrix["participants"]:
            discovery = participant["capabilities"]["discovery"]
            discovery_kinds = {evidence_catalog[reference]["kind"] for reference in discovery["evidence"]}
            if "distribution_metadata" in discovery_kinds:
                self.assertNotEqual(
                    discovery["status"],
                    "verified",
                    f"{participant['id']} must not use a copied skill as native-discovery proof",
                )
            resume = participant["capabilities"]["provider_session_resume"]
            self.assertNotEqual(resume["status"], "verified")

    def test_deferred_boundaries_are_not_activated_or_claimed(self) -> None:
        evidence_catalog = self.matrix["evidence_catalog"]
        boundaries = self.matrix["deferred_boundaries"]
        self.assertEqual({boundary["id"] for boundary in boundaries}, EXPECTED_DEFERRED_BOUNDARIES)
        for boundary in boundaries:
            self.assertEqual(boundary["status"], "deferred")
            self.assertIs(boundary["activated"], False)
            self.assertIs(boundary["claimed_available"], False)
            self.assertTrue(boundary["evidence"])
            self.assertTrue(all(reference in evidence_catalog for reference in boundary["evidence"]))

    def test_readme_declares_local_paths_identity_assumption_and_negative_boundary(self) -> None:
        normalized_readme = " ".join(self.readme.split())
        for phrase in (
            "`capability-matrix.json`",
            "%USERPROFILE%\\.agents\\coord\\office",
            "%USERPROFILE%\\.agents\\runtime\\agent-office\\office.cmd",
            "Stdio MCP entrypoint",
            "warning-only",
            "no local authentication of agent identity",
            "Headless Qwen Code may use",
            "Remote Hermes remains uncontacted",
            "No scheduler or automatic new-turn launcher is activated.",
            "No voice or phone capability is activated.",
            "No real-mail integration is activated.",
            "No customer data is read, stored, or sent.",
            "No network listener, tunnel, port, or public exposure is activated.",
        ):
            self.assertIn(" ".join(phrase.split()), normalized_readme)

        self.assertIsNone(
            re.search(r"remote Hermes[^\n]{0,80}\b(?:is|are)\s+activated\b", self.readme, re.IGNORECASE),
            "README must not claim remote Hermes activation",
        )


if __name__ == "__main__":
    unittest.main()
