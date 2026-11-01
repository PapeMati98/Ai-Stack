"""Regression tests for routing existing-page syntheses through Fleet."""
import json
import unittest
from pathlib import Path

from caretaker.publication import safe_diagnostics, sha
from caretaker.transports import Envelope, TransportError
from curate_once import produce_reviewed_candidate, _build_fleet_append_makers

from .test_curate_fleet_reviewer import (
    _mkdir_tmp, _vault, _load_cfg, _valid_decision, _SequenceMaker,
    _HashPassReviewer, _candidate_spec,
)


APPEND = {"mode": "fleet", "candidates": [
    {"agent": "claude", "model": "sonnet"},
    {"agent": "minimax", "model": "MiniMax-M3"},
]}
REVIEW = {"mode": "fleet", "candidates": [
    {"agent": "claude", "model": "claude-opus-5-5"},
    {"agent": "muse", "model": "muse-spark-1.3"},
]}


class FleetMaker:
    def __init__(self, model="sonnet", provider="claude", unavailable=False):
        self.calls = []
        self.model, self.provider, self.unavailable = model, provider, unavailable

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.unavailable:
            raise TransportError("unavailable", reason_code="native_timeout")
        final = json.dumps({"status": "completed",
                            "summary": json.dumps(_valid_decision())})
        return Envelope(ok=True, status="ok", final=final, model=self.model,
                        provider=self.provider, session_id="maker-1",
                        raw={"ok": True, "status": "ok", "final": final,
                             "provider": self.provider, "model": self.model,
                             "sessionId": "maker-1"})


class AppendMakerTests(unittest.TestCase):
    def _run(self, root, frozen, cfg, makers, reviewers, local=None):
        run_root = root / "run"
        run_root.mkdir()
        return produce_reviewed_candidate(
            frozen=frozen, run_root=run_root,
            observed="2026-09-27T00:00:00+00:00", cfg=cfg, rid="0134",
            maker=local or _SequenceMaker([], "vllm", "Qwen3.8-27B"),
            fleet_append_makers=[{"transport": m, "spec": _candidate_spec()}
                                 for m in makers] if makers is not None else None,
            fleet_reviewers=[{"transport": r, "spec": _candidate_spec(),
                              "lineage": r.provider} for r in reviewers],
        )

    def test_existing_page_uses_fleet_and_records_actual_identity(self):
        with _mkdir_tmp() as d:
            root = Path(d)
            _, frozen = _vault(root)
            cfg, _ = _load_cfg(root, {"append_maker": APPEND, "reviewer": REVIEW})
            maker = FleetMaker()
            result = self._run(root, frozen, cfg, [maker], [_HashPassReviewer()])
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["maker_route"], "fleet_append")
            self.assertEqual(result["primary_model"], "sonnet")
            self.assertEqual(result["maker_agent"], "claude")
            self.assertEqual(result["maker_lineage"], "claude")
            self.assertEqual(safe_diagnostics(result)["maker_route"], "fleet_append")
            self.assertIn("ANSWER PLACEMENT", maker.calls[0]["prompt"])

    def test_new_page_keeps_local_maker(self):
        with _mkdir_tmp() as d:
            root = Path(d)
            _, frozen = _vault(root)
            frozen["preimage_sha256"] = None
            cfg, _ = _load_cfg(root, {"append_maker": APPEND, "reviewer": REVIEW})
            fleet = FleetMaker()
            local = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            result = self._run(root, frozen, cfg, [fleet],
                               [_HashPassReviewer()], local)
            self.assertEqual(result["maker_route"], "local_new_page")
            self.assertEqual(len(fleet.calls), 0)
            self.assertEqual(len(local.calls), 1)

    def test_same_lineage_reviewer_skipped_and_none_leaves_pending(self):
        with _mkdir_tmp() as d:
            root = Path(d)
            _, frozen = _vault(root)
            cfg, _ = _load_cfg(root, {"append_maker": APPEND, "reviewer": REVIEW})
            same = _HashPassReviewer(provider="claude", model="claude-opus-5-5")
            other = _HashPassReviewer()
            result = self._run(root, frozen, cfg, [FleetMaker()], [same, other])
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["reviewer_model"], "muse-spark-1.3")
        with _mkdir_tmp() as d:
            root = Path(d)
            _, frozen = _vault(root)
            cfg, _ = _load_cfg(root, {"append_maker": APPEND, "reviewer": REVIEW})
            result = self._run(root, frozen, cfg, [FleetMaker()],
                               [_HashPassReviewer(provider="claude",
                                                  model="claude-opus-5-5")])
            self.assertEqual(result["status"], "PENDING")
            self.assertEqual(result["reason"], "no_independent_reviewer")
            self.assertEqual(result["maker_route"], "fleet_append")

    def test_all_unavailable_falls_back_to_local(self):
        with _mkdir_tmp() as d:
            root = Path(d)
            _, frozen = _vault(root)
            cfg, _ = _load_cfg(root, {"append_maker": APPEND, "reviewer": REVIEW})
            local = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            makers = [FleetMaker(unavailable=True), FleetMaker(unavailable=True)]
            result = self._run(root, frozen, cfg, makers,
                               [_HashPassReviewer()], local)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["maker_route"], "local_fallback")
            self.assertEqual(result["primary_model"], "Qwen3.8-27B")
            self.assertEqual([len(m.calls) for m in makers], [1, 1])

    def test_first_available_fleet_candidate_wins(self):
        with _mkdir_tmp() as d:
            root = Path(d)
            _, frozen = _vault(root)
            cfg, _ = _load_cfg(root, {"append_maker": APPEND, "reviewer": REVIEW})
            first = FleetMaker(unavailable=True)
            second = FleetMaker(model="MiniMax-M3", provider="minimax")
            result = self._run(root, frozen, cfg, [first, second],
                               [_HashPassReviewer()])
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["primary_model"], "MiniMax-M3")
            self.assertEqual(result["maker_route"], "fleet_append")
            self.assertEqual([len(first.calls), len(second.calls)], [1, 1])

    def test_repair_uses_actual_second_maker_for_independence(self):
        class FirstRoundMaker(FleetMaker):
            def invoke(self, **kwargs):
                if self.calls:
                    self.calls.append(kwargs)
                    raise TransportError("unavailable", reason_code="native_timeout")
                return super().invoke(**kwargs)

        class RepairThenOutage(_HashPassReviewer):
            def invoke(self, **kwargs):
                if self.calls:
                    self.calls.append(kwargs)
                    raise TransportError("unavailable", reason_code="native_timeout")
                self.calls.append(kwargs)
                pubs = sorted(Path(kwargs["cwd"]).glob("**/publication.json"),
                              key=lambda p: p.stat().st_mtime)
                final = json.dumps({
                    "verdict": "REPAIR", "manifest_sha256": sha(pubs[-1].read_bytes()),
                    "findings": [{"text": "Clarify source attribution",
                                  "weight": "blocking",
                                  "class": "source_misattribution"}],
                })
                return Envelope(ok=True, status="ok", final=final,
                                model=self.model, provider=self.provider,
                                session_id=self.session_id,
                                raw={"ok": True, "status": "ok", "final": final,
                                     "provider": self.provider, "model": self.model,
                                     "sessionId": self.session_id})

        with _mkdir_tmp() as d:
            root = Path(d)
            _, frozen = _vault(root)
            cfg, _ = _load_cfg(root, {"append_maker": APPEND, "reviewer": REVIEW})
            first = FirstRoundMaker()
            second = FleetMaker(model="MiniMax-M3", provider="minimax")
            muse = RepairThenOutage()
            claude = _HashPassReviewer(provider="claude", model="claude-opus-5-5")
            result = self._run(root, frozen, cfg, [first, second], [muse, claude])
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["primary_model"], "MiniMax-M3")
            self.assertEqual(result["reviewer_model"], "claude-opus-5-5")
            self.assertEqual(len(result["review_rounds"]), 2)

    def test_absent_config_has_no_append_candidates(self):
        with _mkdir_tmp() as d:
            root = Path(d)
            _, frozen = _vault(root)
            cfg, _ = _load_cfg(root, {"reviewer": REVIEW})
            self.assertIsNone(_build_fleet_append_makers(cfg))
            local = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            result = self._run(root, frozen, cfg, None,
                               [_HashPassReviewer()], local)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["maker_route"], "local_fallback")
            self.assertEqual(len(local.calls), 1)
