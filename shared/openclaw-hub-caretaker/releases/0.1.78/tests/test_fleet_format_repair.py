"""0.1.16 format-repair loop for Fleet reviewers.

Measured 2026-09-23 on the caretaker's own review prompt: five of seven Fleet
reviewers judged a planted defect correctly and were still discarded, because
the prompt said "return only JSON" while Fleet demands its handoff envelope.
The answer's SHAPE is now repaired: the same reviewer gets the exact reason and
its own previous answer. Judgement is never relaxed - a prose PASS is still not
a verdict, and quota or gate failures still move on to the next candidate.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.publication import PublicationError, sha
from caretaker.transports import (
    PREVIOUS_OUTPUT_LIMIT, Envelope, TransportError, _parse_fleet_envelope)
from curate_once import FLEET_FORMAT_REPAIR_ATTEMPTS, produce_reviewed_candidate

from .test_fleet_verdict_extraction import (
    _candidate_spec, _fleet_handoff_with_summary, _load_cfg, _mkdir_tmp,
    _SequenceMaker, _valid_decision, _vault,
)

FLEET_CONFIG = {
    "maker_timeout_seconds": 400,
    "reviewer_timeout_seconds": 400,
    "reviewer": {"mode": "fleet", "candidates": [
        {"agent": "mimo", "model": "mimo-v2.6-pro"},
        {"agent": "codex", "model": "gpt-5.6-sol"},
    ]},
}


def _digest(run_root: Path) -> str:
    pubs = sorted(run_root.rglob("publication.json"), key=lambda p: p.stat().st_mtime)
    return sha(pubs[-1].read_bytes())


class _Scripted:
    """Reviewer whose answers are scripted per call; records every prompt."""

    def __init__(self, run_root, script, provider="mimo", model="mimo-v2.6-pro"):
        self.run_root, self.script = run_root, list(script)
        self.prompts, self.provider, self.model = [], provider, model

    def invoke(self, **kwargs):
        self.prompts.append(kwargs["prompt"])
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        text = step(_digest(self.run_root)) if callable(step) else step
        return _fleet_handoff_with_summary(text, provider=self.provider, model=self.model)


def _pass(digest):
    return json.dumps({"verdict": "PASS", "manifest_sha256": digest, "findings": []})


def _run(test, reviewers, rid, lineages=None):
    with _mkdir_tmp() as directory:
        root = Path(directory)
        vault, frozen = _vault(root)
        cfg, _ = _load_cfg(root, publication=FLEET_CONFIG)
        (root / "run").mkdir()
        spec = _candidate_spec()
        built = [factory(root / "run") for factory in reviewers]
        maker = _SequenceMaker([json.dumps(_valid_decision())], "vllm", "Qwen3.8-27B")
        named = lineages or [None] * len(built)
        try:
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run", observed="2026-09-23T00:00:00+00:00",
                cfg=cfg, rid=rid, maker=maker,
                fleet_reviewers=[{"transport": r, "spec": spec, **({"lineage": n} if n else {})}
                                 for r, n in zip(built, named)])
            error = None
        except PublicationError as exc:
            result, error = None, str(exc)
        files = {p.relative_to(root).as_posix(): p.read_bytes()
                 for p in (root / "run").rglob("*") if p.is_file()}
        return result, error, built, maker, files


class FormatRepairLoopTests(unittest.TestCase):

    def test_prose_then_json_is_accepted_after_one_repair(self):
        prose = "Review result: REPAIR-looking prose MARKER-7f3a, no JSON block."
        result, error, (rev,), maker, files = _run(
            self, [lambda run: _Scripted(run, [prose, _pass])], "rid-prose-then-json")
        self.assertIsNone(error)
        self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
        self.assertEqual(len(rev.prompts), 2)
        repair = rev.prompts[1]
        self.assertTrue(repair.startswith("FORMAT REPAIR 1/"))
        self.assertIn('no JSON object with a top-level "verdict" key', repair)
        self.assertIn("MARKER-7f3a", repair, "the reviewer gets its own answer back")
        # The format repair never re-runs the maker ...
        self.assertEqual(len(maker.calls), 1)
        # ... and the previous answer is never persisted anywhere in the run.
        for name, body in files.items():
            self.assertNotIn(b"MARKER-7f3a", body, name)

    def test_wrong_verdict_value_gets_specific_feedback(self):
        fail = lambda d: json.dumps({"verdict": "FAIL", "manifest_sha256": d, "findings": []})
        result, error, (rev,), _, _ = _run(
            self, [lambda run: _Scripted(run, [fail, _pass])], "rid-fail-value")
        self.assertIsNone(error)
        self.assertIn('"verdict": "FAIL"', rev.prompts[1])
        self.assertIn('must be exactly "PASS" or "REPAIR"', rev.prompts[1])

    def test_fleet_handoff_contract_error_is_repaired_with_fleet_detail(self):
        broken = TransportError("agent-fleet refused: contract", reason_code="handoff_contract")
        broken.detail = "HANDOFF CONTRACT MISMATCH: summary: expected a string, got dict"
        broken.previous_output = '{"summary": {"verdict": "PASS"}} MARKER-c0de'
        result, error, (rev,), _, _ = _run(
            self, [lambda run: _Scripted(run, [broken, _pass])], "rid-contract")
        self.assertIsNone(error)
        self.assertIn("Agent Fleet rejected your handoff envelope: HANDOFF CONTRACT MISMATCH",
                      rev.prompts[1])
        self.assertIn("MARKER-c0de", rev.prompts[1])

    def test_repair_too_long_for_the_command_line_is_not_sent_to_antigravity(self):
        # 0.1.17: the Antigravity adapter passes the task as an argument; a task the
        # Windows command line cannot carry fails before the model starts.
        from unittest.mock import patch
        with patch("curate_once.REPAIR_PROMPT_MAX_CHARS", 50):
            result, error, (agy, codex), _, _ = _run(self, [
                lambda run: _Scripted(run, ["prose, no JSON"], provider="antigravity", model="gemini-3.1-pro-high"),
                lambda run: _Scripted(run, [_pass], provider="codex", model="gpt-5.6-sol"),
            ], "rid-argv-cap", lineages=["antigravity", "codex"])
        self.assertIsNone(error)
        self.assertEqual(len(agy.prompts), 1, "no repair task is sent that the command line cannot carry")
        self.assertEqual(result["reviewer_model"], "gpt-5.6-sol")

    def test_quota_is_not_repaired_and_next_candidate_wins(self):
        quota = TransportError("agent-fleet refused: quota", reason_code="provider_quota")
        result, error, (first, second), _, _ = _run(self, [
            lambda run: _Scripted(run, [quota]),
            lambda run: _Scripted(run, [_pass], provider="codex", model="gpt-5.6-sol"),
        ], "rid-quota")
        self.assertIsNone(error)
        self.assertEqual(len(first.prompts), 1, "a quota failure must not burn format repairs")
        self.assertEqual(result["reviewer_model"], "gpt-5.6-sol")

    def test_repairs_are_bounded_then_next_candidate(self):
        prose = "still prose"
        result, error, (first, second), _, _ = _run(self, [
            lambda run: _Scripted(run, [prose] * (1 + FLEET_FORMAT_REPAIR_ATTEMPTS)),
            lambda run: _Scripted(run, [_pass], provider="codex", model="gpt-5.6-sol"),
        ], "rid-bounded")
        self.assertIsNone(error)
        self.assertEqual(len(first.prompts), 1 + FLEET_FORMAT_REPAIR_ATTEMPTS)
        self.assertEqual(result["reviewer_model"], "gpt-5.6-sol")

    def test_prose_pass_is_still_never_a_verdict(self):
        prose = "Verdict: PASS. Everything looks fine."
        result, error, (rev,), _, files = _run(
            self, [lambda run: _Scripted(run, [prose] * (1 + FLEET_FORMAT_REPAIR_ATTEMPTS))],
            "rid-prose-pass")
        self.assertIsNotNone(error)
        self.assertIn("independent_review_deferred", error)
        self.assertFalse(any(name.endswith("review.json") for name in files))

    def test_wrong_digest_is_never_repaired_even_if_the_next_answer_would_pass(self):
        """Independent review of 0.1.16: repairing a digest mismatch let a
        reviewer copy the right digest onto a judgement about another manifest."""
        wrong = lambda d: json.dumps({"verdict": "PASS", "manifest_sha256": "0" * 64,
                                      "findings": []})
        result, error, (rev,), _, files = _run(
            self, [lambda run: _Scripted(run, [wrong, _pass])], "rid-digest-once")
        self.assertIsNotNone(error)
        self.assertIn("fleet_verdict_manifest_mismatch", error)
        self.assertEqual(len(rev.prompts), 1)
        self.assertFalse(any(name.endswith("review.json") for name in files))

    def test_first_prompt_states_where_the_verdict_goes(self):
        result, error, (rev,), _, _ = _run(
            self, [lambda run: _Scripted(run, [_pass])], "rid-placement")
        self.assertIsNone(error)
        first = rev.prompts[0]
        self.assertIn("ANSWER PLACEMENT", first)
        self.assertIn("ENTIRE value of the envelope's `summary` string", first)


class FleetFailureClassificationTests(unittest.TestCase):

    def _raise(self, envelope):
        with self.assertRaises(TransportError) as cm:
            _parse_fleet_envelope("", json.dumps(envelope), 2, "")
        return cm.exception

    def test_handoff_contract_carries_fleets_own_diagnostic(self):
        exc = self._raise({"status": "FAILED", "handoff_parse_error":
                           "HANDOFF CONTRACT MISMATCH: status: expected a string, got dict",
                           "handoff": {"summary": "x"}})
        self.assertEqual(exc.reason_code, "handoff_contract")
        self.assertIn("HANDOFF CONTRACT MISMATCH", exc.detail)
        self.assertIn('"summary"', exc.previous_output)

    def test_quota_is_recognised_even_with_a_parse_error(self):
        exc = self._raise({"status": "FAILED", "handoff_parse_error": "NO HANDOFF CANDIDATE",
                           "error": "Individual quota reached. Resets in 2h7m11s."})
        self.assertEqual(exc.reason_code, "provider_quota")

    def test_refusal_and_unknown_failure(self):
        self.assertEqual(self._raise({"status": "REFUSED", "error": "gate"}).reason_code,
                         "fleet_refused")
        self.assertEqual(self._raise({"status": "FAILED"}).reason_code, "nonzero_exit")

    def test_contract_failure_whose_review_mentions_quota_stays_repairable(self):
        """Classification reads Fleet diagnostics, never review prose: a
        review that talks about a quota must not skip its format repair."""
        exc = self._raise({"status": "FAILED", "handoff_parse_error": "MALFORMED JSON at 1595",
                           "handoff": {"summary": "The quota of 429 retries and rate limit "
                                                  "policy were reviewed."}})
        self.assertEqual(exc.reason_code, "handoff_contract")

    def test_status_tokens_in_review_prose_do_not_skip_the_repair(self):
        """Round-2 review: RESOURCE_EXHAUSTED / code 429 written in review prose
        (under "text", not an "error" value) is not a provider quota."""
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "agent-fleet" / "evidence" / "run-x"
            run.mkdir(parents=True)
            out = run / "attempt-1.stdout.txt"
            out.write_text('{"type":"text","text":"The page mishandles RESOURCE_EXHAUSTED '
                           '(code 429) retries; verdict REPAIR"}', encoding="utf-8")
            exc = self._raise({"status": "FAILED", "handoff_parse_error": "NO HANDOFF CANDIDATE",
                               "stdout_path": str(out), "output_dir": str(run)})
        self.assertEqual(exc.reason_code, "handoff_contract")

    def test_structured_provider_quota_in_owned_output_is_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "agent-fleet" / "evidence" / "run-x"
            run.mkdir(parents=True)
            out = run / "attempt-1.stdout.txt"
            out.write_text('{"event":"result","result":{"status":"ERROR","error":'
                           '"Individual quota reached. Resets in 2h7m11s."}}', encoding="utf-8")
            exc = self._raise({"status": "FAILED", "handoff_parse_error": "NO HANDOFF CANDIDATE",
                               "stdout_path": str(out), "output_dir": str(run)})
        self.assertEqual(exc.reason_code, "provider_quota")

    def test_stdout_path_outside_the_attempts_own_evidence_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "agent-fleet" / "evidence" / "run-x"
            run.mkdir(parents=True)
            stranger = Path(tmp) / "unrelated-secret.txt"
            stranger.write_text("NOT-THE-REVIEWERS-OUTPUT", encoding="utf-8")
            exc = self._raise({"status": "FAILED", "handoff_parse_error": "x",
                               "stdout_path": str(stranger), "output_dir": str(run)})
        self.assertIsNone(exc.previous_output)

    def test_owned_output_read_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "agent-fleet" / "evidence" / "run-x"
            run.mkdir(parents=True)
            out = run / "attempt-1.stdout.txt"
            out.write_text("A" * 5_000_000 + "END-MARKER", encoding="utf-8")
            exc = self._raise({"status": "FAILED", "handoff_parse_error": "x",
                               "stdout_path": str(out), "output_dir": str(run)})
        self.assertLessEqual(len(exc.previous_output), PREVIOUS_OUTPUT_LIMIT)
        self.assertTrue(exc.previous_output.endswith("END-MARKER"))

    def test_parseable_nonzero_without_status_keeps_a_specific_code(self):
        with self.assertRaises(TransportError) as quota:
            _parse_fleet_envelope(json.dumps({"error": "Individual quota reached"}), "", 1, "")
        self.assertEqual(quota.exception.reason_code, "provider_quota")
        with self.assertRaises(TransportError) as contract:
            _parse_fleet_envelope(json.dumps({"handoff_parse_error": "NO HANDOFF CANDIDATE"}),
                                  "", 1, "")
        self.assertEqual(contract.exception.reason_code, "handoff_contract")


if __name__ == "__main__":
    unittest.main()
