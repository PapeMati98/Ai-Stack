"""Synthetic proof and continuation tests for terminal generation recovery."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from caretaker.terminal_generation import (
    _terminal_report, canonical_proposal_identities,
    prepare_terminal_generation_resume,
)


TEST_TMP = ROOT / ".test-tmp"


class TerminalGenerationTests(unittest.TestCase):
    def setUp(self):
        TEST_TMP.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="terminal-generation-", dir=TEST_TMP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_unsorted_paths_canonicalize_aligned_identity_triples(self):
        paths = ["zeta.md", "alpha.md"]
        proposal_ids = ["proposal-z", "proposal-a"]
        hashes = ["hash-z", "hash-a"]
        expected = [
            ("proposal-a", "alpha.md", "hash-a"),
            ("proposal-z", "zeta.md", "hash-z"),
        ]
        self.assertEqual(canonical_proposal_identities(paths, proposal_ids, hashes), expected)
        self.assertEqual(canonical_proposal_identities(
            list(reversed(paths)), list(reversed(proposal_ids)), list(reversed(hashes)),
        ), expected)
        self.assertNotEqual(canonical_proposal_identities(
            paths, list(reversed(proposal_ids)), hashes,
        ), expected)
        self.assertIsNone(canonical_proposal_identities(
            ["same.md", "same.md"], ["proposal-1", "proposal-2"],
            ["hash-1", "hash-2"],
        ))

    def test_old_consumer_report_requires_exact_terminal_pre_effect_batch(self):
        state = self.root / "state"
        runs = state / "runs"
        runs.mkdir(parents=True)
        batch = "a" * 64
        proposal_ids = ["prop-z", "prop-a"]
        report = {
            "status": "DEFERRED_BUDGET", "running": False,
            "started_at": "2026-10-24T21:01:22Z",
            "completed_at": "2026-10-24T21:21:22Z",
            "completed_batches": [{
                "batch": batch, "project": "dokumenty-89680e43",
                "status": "BACKOFF_DEFERRED", "reason": "process_timeout",
                "lane": "legacy", "proposal_ids": proposal_ids,
                "publication": None, "checkpoint": None,
            }],
        }
        path = runs / "terminal.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        phase_time = 1792876200.0

        accepted = _terminal_report(
            state, batch=batch, project="dokumenty-89680e43",
            proposal_ids=proposal_ids, phase_mtime=phase_time,
        )
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted["path"], path)
        self.assertIsNotNone(_terminal_report(
            state, batch=batch, project="dokumenty-89680e43",
            proposal_ids=list(reversed(proposal_ids)), phase_mtime=phase_time,
        ))

        report["running"] = True
        path.write_text(json.dumps(report), encoding="utf-8")
        self.assertIsNone(_terminal_report(
            state, batch=batch, project="dokumenty-89680e43",
            proposal_ids=proposal_ids, phase_mtime=phase_time,
        ))
        report["running"] = False
        report["completed_batches"][0]["proposal_ids"] = ["prop-other"]
        path.write_text(json.dumps(report), encoding="utf-8")
        self.assertIsNone(_terminal_report(
            state, batch=batch, project="dokumenty-89680e43",
            proposal_ids=proposal_ids, phase_mtime=phase_time,
        ))

    def test_resume_archives_old_generation_and_preserves_call_history(self):
        phase_state = self.root / "curation"
        old_run = phase_state / "run-old"
        candidate = old_run / "candidate"
        candidate.mkdir(parents=True)
        manifest = candidate / "publication.json"
        manifest.write_text("synthetic manifest", encoding="utf-8")
        pointer = phase_state / "phase-pointer.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer_bytes = b"immutable prior phase record\n"
        pointer.write_bytes(pointer_bytes)
        old_result = old_run / "result.json"
        old_result.write_bytes(b"pre-effect prior result\n")
        report = self.root / "consumer-report.json"
        report.write_bytes(b"terminal consumer report\n")
        assessment = {
            "verdict": "REPAIR", "manifest_sha256": "b" * 64,
            "findings": [{"class": "source_misattribution", "weight": "blocking"}],
        }
        old_progress = {
            "maker_calls": 2, "review_calls": 1, "repair_attempt": 1,
            "assessment": assessment,
            "review_rounds": [{"round": 1, "verdict": "REPAIR"}],
            "maker_format_repairs": 0, "reviewer_format_repairs": {},
            "maker_response": {"provider": "must-not-be-reused"},
            "review_response": {"provider": "old-review"},
        }
        plan = {
            "record": {"observed": "2026-10-24T21:10:00Z"},
            "phase_state": phase_state, "run_root": old_run,
            "pointer": pointer, "old_result_path": old_result,
            "pins": {"candidate/publication.json": hashlib.sha256(
                manifest.read_bytes()).hexdigest()},
            "terminal_report_path": report,
            "progress": old_progress, "assessment": assessment,
            "manifest_sha256": "b" * 64,
            "terminal_report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            "batch": "c" * 64, "candidate": candidate, "manifest": manifest,
            "phase_mtime": 1792876200.0,
        }
        saved = []

        def save_phase(cfg, paths, frozen, run_root, observed, phase,
                       saved_candidate, saved_manifest, progress):
            saved.append({
                "run_root": run_root, "phase": phase, "candidate": saved_candidate,
                "manifest": saved_manifest, "progress": progress,
            })
            return True

        result = prepare_terminal_generation_resume(
            plan, SimpleNamespace(), ["proposals/agent/one.md"], {}, save_phase,
        )

        self.assertIsNotNone(result)
        self.assertEqual(pointer.read_bytes(), pointer_bytes)
        self.assertEqual(old_result.read_bytes(), b"pre-effect prior result\n")
        self.assertEqual(saved[0]["phase"], "REPAIR_REQUIRED")
        self.assertEqual(saved[0]["run_root"], result["run_root"])
        progress = saved[0]["progress"]
        self.assertEqual((progress["maker_calls"], progress["review_calls"]), (2, 1))
        self.assertEqual(progress["repair_attempt"], 0)
        self.assertTrue(progress["terminal_generation_retry"])
        self.assertEqual(progress["terminal_generation_source_batch"], "c" * 64)
        self.assertNotIn("maker_response", progress)
        self.assertNotIn("review_response", progress)
        archive = result["archive"]
        self.assertEqual((archive / "phase.json").read_bytes(), pointer_bytes)
        self.assertEqual((archive / "result.json").read_bytes(), b"pre-effect prior result\n")
        self.assertEqual((archive / "consumer-report.json").read_bytes(), report.read_bytes())


if __name__ == "__main__":
    unittest.main()
