"""Normal curator entry checks for terminal generation retries."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import curate_once
import maintenance
from caretaker.config import load_config
from caretaker.publication import build_candidate, freeze, sha
from caretaker.transports import TransportError
from tests.test_publication_state_owner import (
    PROJECT, MAIN, _attest_draft, _base, _workspace, _write_page, _write_proposal,
)


class TerminalGenerationInvokeTests(unittest.TestCase):
    def _case(self, test, *, fleet=False):
        root, vault = _workspace(test)
        state = root / "state"
        state.mkdir()
        page = _write_page(vault, draft=True)
        owner_state = state / "projects" / PROJECT / "state"
        _attest_draft(vault, owner_state, page)
        proposal = _write_proposal(vault)
        paths = [proposal.relative_to(vault).as_posix()]
        frozen = freeze(vault, paths, MAIN, attestation_state_dir=owner_state)

        base = _base(root, vault)
        if fleet:
            base["publication"] = {
                "reviewer": {
                    "mode": "fleet",
                    "candidates": [{"agent": "grok", "model": "grok-4"}],
                    "data_classification": "internal",
                },
                "append_maker": {
                    "mode": "fleet",
                    "candidates": [
                        {"agent": "kimi", "model": "k3"},
                        {"agent": "codex", "model": "gpt-6.1"},
                    ],
                    "data_classification": "internal",
                },
            }
        config_path, _ = maintenance._project_runtime_cfg(
            base, PROJECT, state, lane="legacy", database=None,
        )
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["maintenance_terminal_generation_retry"] = {
            "schema": 1, "reason": "terminal_generation_source_retry",
            "project": PROJECT, "paths": paths, "lane": "legacy",
        }
        config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        cfg = load_config(config_path)

        phase_state = owner_state / "curation"
        old_run = phase_state / "initial-generation"
        candidate = old_run / "candidate"
        manifest = build_candidate(
            frozen,
            {
                "body": "# Current source note\nCited synthetic evidence (src-one).\n",
                "summary": "Synthetic source summary.",
                "dispositions": {proposal.stem: {
                    "disposition": "synthesized", "reason": "Cited src-one.",
                }},
            },
            candidate, "2026-10-24T21:00:00Z",
        )
        manifest_digest = sha(manifest.read_bytes())
        assessment = {
            "verdict": "REPAIR", "manifest_sha256": manifest_digest,
            "findings": [{
                "class": "source_misattribution", "weight": "blocking",
                "text": "Synthetic finding bound to the initial manifest.",
            }],
        }
        progress = {
            "phase": "REPAIR_REQUIRED", "maker_calls": 2, "review_calls": 1,
            "maker_provider": "kimi", "maker_model": "k3",
            "maker_route": "fleet_append", "assessment": assessment,
            "review_rounds": [{"round": 1, "verdict": "REPAIR"}],
            "repair_attempt": 0, "terminal_generation_retry": True,
            "terminal_generation_source_batch": "a" * 64,
            "terminal_generation_report_sha256": "b" * 64,
            "terminal_generation_prior_manifest_sha256": manifest_digest,
            "terminal_generation_initial_maker_calls": 2,
            "terminal_generation_initial_review_calls": 1,
            "terminal_generation_archive_path": "initial-generation/terminal-generation-archive-x",
            "terminal_generation_old_pointer_sha256": "c" * 64,
            "terminal_generation_phase_mtime": 1792876200.0,
        }
        curate_once._save_phase_progress(
            cfg, paths, frozen, old_run, "2026-10-24T21:00:00Z",
            "REPAIR_REQUIRED", candidate, manifest, progress,
        )
        pointer = curate_once._phase_pointer(phase_state, PROJECT, paths)
        record = json.loads(pointer.read_text(encoding="utf-8"))
        plan = {
            "continuation": True, "current_record": record,
            "current_pointer": pointer, "prior_inventory_body": "Old note retained verbatim.",
            "frozen": frozen,
        }
        return config_path, paths, cfg, pointer, record, plan

    def _invoke(self, test, *, fleet=False, transport_error=None):
        config_path, paths, _cfg, pointer, record, plan = self._case(test, fleet=fleet)
        calls = {"fleet": [], "local": []}

        class FakeFleet:
            def __init__(self, agent, model):
                self.agent, self.model = agent, model

            def invoke(self, **_kwargs):
                calls["fleet"].append((self.agent, self.model))
                if transport_error:
                    raise TransportError("synthetic transport result",
                                         reason_code=transport_error)
                return SimpleNamespace(
                    provider=self.agent, model=self.model, ok=True, status="ok",
                    final="{}", session_id="synthetic-fleet",
                )

        validated = {"old_result": {"status": "QUARANTINED"}, "plan": plan}
        run_result = {}

        def run_stub(path, selected, publish, **_kwargs):
            self.assertTrue(publish)
            built = curate_once._build_fleet_append_makers(load_config(path))
            run_result["candidate_count"] = len(built or [])
            if built:
                try:
                    built[0]["transport"].invoke()
                except TransportError:
                    calls["local"].append("primary")
                    raise AssertionError("terminal retry must not fall back locally")
            return {"status": "PENDING", "reason": "synthetic_complete"}

        with patch.object(maintenance, "_validated_terminal_generation_marker",
                          return_value=validated), \
             patch.object(curate_once, "_default_fleet_transport",
                          side_effect=lambda agent, model: FakeFleet(agent, model)), \
             patch.object(curate_once, "run", side_effect=run_stub):
            result = maintenance._invoke_curate(config_path, paths)
        return result, calls, run_result, pointer, record

    def test_missing_fleet_maker_defers_without_starting_curator(self):
        result, calls, run_result, pointer, record = self._invoke(self, fleet=False)
        self.assertEqual(result["status"], "DEFERRED_PREPARED")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["phase"], "REPAIR_REQUIRED")
        self.assertEqual(calls["fleet"], [])
        self.assertEqual(run_result, {})
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), record)

    def test_terminal_retry_uses_one_fleet_maker_and_defers_on_pre_call_unavailable(self):
        result, calls, run_result, pointer, record = self._invoke(
            self, fleet=True, transport_error="fleet_profiles_unavailable",
        )
        self.assertEqual(result["status"], "DEFERRED_PREPARED")
        self.assertEqual(result["reason"], "fleet_profiles_unavailable")
        self.assertEqual(calls["fleet"], [("kimi", "k3")])
        self.assertEqual(calls["local"], [])
        self.assertEqual(run_result["candidate_count"], 1)
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), record)

    def test_terminal_retry_keeps_uncertain_fleet_result_unknown(self):
        result, calls, run_result, _pointer, _record = self._invoke(
            self, fleet=True, transport_error="process_timeout",
        )
        self.assertEqual(result["status"], "UNKNOWN_REQUIRES_RECONCILIATION")
        self.assertEqual(result["reason"], "terminal_generation_maker_outcome_unknown")
        self.assertEqual(calls["fleet"], [("kimi", "k3")])
        self.assertEqual(calls["local"], [])
        self.assertEqual(run_result["candidate_count"], 1)


if __name__ == "__main__":
    unittest.main()
