"""Verdict extraction contract for the fleet-mode reviewer loop.

The 2026-09-20 live Agent Fleet invocation returned envelopes where the
real reviewer verdict was embedded as a JSON block inside the handoff's
``summary`` string. The candidate loop's previous extraction
(``parsed.get("verdict")``) returned ``None`` on that shape, so the
publication always exited with ``independent_review_not_accepted`` after
the first review and never ran a repair round. This file pins the
contract that unblocks the fleet mode:

* Verdict embedded in the handoff's ``summary`` string is read together
  with the full ``findings`` list.
* Verdict in ``final`` and verdict at the top level of the handoff are
  also read (forward compatibility).
* ``manifest_sha256`` mismatch is a stable deferral reason; it is never
  silently treated as PASS.
* Missing verdict is a stable deferral reason; it is never silently
  treated as ``independent_review_not_accepted``.
* A REPAIR verdict triggers a repair round and a second review.
* Review content (findings, prose) does NOT leak into logs or
  diagnostics.
* The legacy Muse path keeps bit-for-bit identical behaviour.
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from caretaker.config import TransportSpec, load_config
from caretaker.publication import PublicationError, freeze, sha
from caretaker.transports import Envelope
from curate_once import (
    FLEET_FORMAT_REPAIR_ATTEMPTS,
    _FLEET_DEFER_REASON_VERDICT_MISSING,
    _FLEET_DEFER_REASON_VERDICT_MANIFEST_MISMATCH,
    _extract_fleet_verdict,
    _extract_verdict_block,
    _wrap_verdict,
    produce_reviewed_candidate,
)
from ._helpers import write_config


TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"


def _mkdir_tmp():
    TASK_TMP.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(TASK_TMP))


def _vault(root: Path):
    """Mirror the existing fleet-reviewer fixture: one proposal, one page."""
    vault = root / "vault"
    source = vault / "raw/sources/2026/09/src-one"
    source.mkdir(parents=True)
    data = (b"Dated report: local tool canary passed; deployment "
            b"remains unverified.")
    (source / "original.md").write_bytes(data)
    meta = {
        "source_id": "src-one", "classification": "internal",
        "local_only": False, "original_file": "original.md",
        "sha256": sha(data), "byte_length": len(data),
    }
    (source / "source.json").write_text(json.dumps(meta), encoding="utf-8")
    proposal = vault / "proposals/agent/one.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text(
        "---\nid: prop-one\nclassification: internal\nsource_ids:\n"
        "  - src-one\ntarget_pages:\n  - wiki/projects/test.md\n---\n"
        "Report.\n",
        encoding="utf-8",
    )
    target = vault / "wiki/projects/test.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        '---\ntitle: Test\nstatus: reviewed\nlast_verified: "2026-01-01"\n'
        "sources: []\nproject_slug: test\n---\n\n# Test\n"
        "Old unresolved question.\n",
        encoding="utf-8",
    )
    return vault, freeze(vault, ["proposals/agent/one.md"],
                         "wiki/projects/test.md")


def _valid_decision(pid="prop-one"):
    return {
        "body": ("# Test\nOld unresolved question.\nDated tool canary "
                 "report (src-one); deployment unverified."),
        "summary": "Tool canary report.",
        "dispositions": {pid: {"disposition": "synthesized",
                                "reason": "Dated and scope-limited "
                                          "report integrated."}},
    }


def _load_cfg(tmp: Path, publication=None):
    cfg_dir = tmp / "cfg"
    cfg_dir.mkdir()
    cfg_path = write_config(cfg_dir)
    raw = json.loads(cfg_path.read_text("utf-8"))
    if publication is not None:
        raw["publication"] = publication
        cfg_path.write_text(json.dumps(raw, indent=2), "utf-8")
    elif "publication" in raw:
        raw.pop("publication")
        cfg_path.write_text(json.dumps(raw, indent=2), "utf-8")
    return load_config(cfg_path), cfg_path


def _candidate_spec():
    return TransportSpec(kind="openclaw_argv", executable="openclaw.cmd",
                         argv=["agent", "exec"])


class _SequenceMaker:
    """Records every (timeout, prompt) and returns the queued maker."""

    def __init__(self, finals, provider, model, session_id=None):
        self.finals = list(finals)
        self.calls = []
        self.provider = provider
        self.model = model
        self.session_id = session_id

    def invoke(self, *, spec, cwd, prompt, run_config_path, state_dir,
               timeout):
        self.calls.append({"timeout": timeout, "prompt": prompt,
                           "cwd": cwd, "run_config_path": run_config_path,
                           "state_dir": state_dir})
        if not self.finals:
            raise AssertionError("unexpected extra maker call")
        final = self.finals.pop(0)
        return Envelope(
            ok=True, status="ok", final=final, model=self.model,
            provider=self.provider, session_id=self.session_id,
            raw={"ok": True, "status": "ok", "provider": self.provider,
                 "model": self.model, "sessionId": self.session_id,
                 "final": final},
        )


def _fleet_handoff_with_summary(summary_text, *,
                                provider="grok", model="grok-4.6",
                                session_id="sess-fleet-1"):
    """Build the Fleet-shaped envelope observed on 2026-09-20.

    The real envelope has no top-level ``verdict`` field; the reviewer's
    verdict lives inside the ``summary`` string as a JSON block mixed
    with prose. ``AgentFleetArgvTransport._parse_fleet_envelope``
    surfaces the whole envelope via ``Envelope.final`` so the candidate
    loop sees the handoff dict after ``json.loads(env.final)``.
    """
    handoff = {
        "files_changed": [],
        "next_steps": [""],
        "risks": [""],
        "status": "OK",
        "summary": summary_text,
        "verification": [],
    }
    return Envelope(
        ok=True, status="ok", final=json.dumps(handoff),
        model=model, provider=provider, session_id=session_id,
        raw={"ok": True, "status": "ok", "provider": provider,
             "model": model, "sessionId": session_id,
             "final": json.dumps(handoff)},
    )


def _repair_summary_json(digest, marker):
    """Return the JSON block the 2026-09-20 Fleet envelope embeds in
    ``summary`` for a REPAIR verdict. Hoisted to module level so the
    nested fake-reviewer classes can call it without depending on the
    enclosing test instance's ``self``.
    """
    return json.dumps(
        {"verdict": "REPAIR", "manifest_sha256": digest,
         "findings": [f"{marker}: missing citation for src-one",
                      f"{marker}: stale last_verified date"]})


def _pass_summary_json(digest):
    """Return the JSON block the 2026-09-20 Fleet envelope embeds in
    ``summary`` for a PASS verdict. Module level for the same reason
    as ``_repair_summary_json``.
    """
    return json.dumps(
        {"verdict": "PASS", "manifest_sha256": digest, "findings": []})


class VerdictBlockExtractionTests(unittest.TestCase):
    """``_extract_verdict_block`` finds the JSON block embedded in prose."""

    def test_extracts_first_json_object_with_verdict_key(self):
        block = _extract_verdict_block(
            'prose before {"verdict":"PASS","manifest_sha256":"abc",'
            '"findings":[]} prose after')
        self.assertEqual(block, {"verdict": "PASS",
                                 "manifest_sha256": "abc",
                                 "findings": []})

    def test_skips_non_verdict_objects(self):
        """The helper scans for the first object whose top-level
        ``verdict`` is a string; preceding unrelated objects are skipped
        rather than causing the whole call to fail."""
        text = ('prose {"unrelated":"object"} more prose '
                '{"verdict":"PASS","findings":[]}')
        out = _extract_verdict_block(text)
        self.assertIsNotNone(out)
        self.assertEqual(out.get("verdict"), "PASS")

        # A string with NO JSON object at all yields ``None``.
        self.assertIsNone(_extract_verdict_block("just plain prose"))

        # A string with a JSON object but no ``verdict`` key also
        # yields ``None``; the helper does not invent a verdict.
        self.assertIsNone(
            _extract_verdict_block('prose {"unrelated":"object"}'))

    def test_bare_pass_token_in_prose_is_not_accepted(self):
        """A literal ``PASS`` word in prose must NOT be treated as verdict."""
        self.assertIsNone(_extract_verdict_block(
            "The reviewer said PASS without a structured payload."))

    def test_returns_none_for_non_string_input(self):
        self.assertIsNone(_extract_verdict_block(None))
        self.assertIsNone(_extract_verdict_block(42))
        self.assertIsNone(_extract_verdict_block(["verdict"]))


class WrapVerdictTests(unittest.TestCase):
    """``_wrap_verdict`` produces the canonical verdict shape."""

    def test_accepts_pass_and_repair_tokens(self):
        out = _wrap_verdict({"verdict": "PASS", "manifest_sha256": "abc",
                             "findings": ["x"]})
        self.assertEqual(out, {"verdict": "PASS",
                               "manifest_sha256": "abc",
                               "findings": ["x"]})

    def test_rejects_unknown_verdict_tokens(self):
        self.assertIsNone(_wrap_verdict({"verdict": "MAYBE"}))
        self.assertIsNone(_wrap_verdict({"verdict": 1}))
        self.assertIsNone(_wrap_verdict({"verdict": None}))

    def test_normalises_missing_fields(self):
        out = _wrap_verdict({"verdict": "REPAIR"})
        self.assertEqual(out["manifest_sha256"], None)
        self.assertEqual(out["findings"], [])


class FleetVerdictExtractionTests(unittest.TestCase):
    """``_extract_fleet_verdict`` honours all three observed shapes."""

    def test_top_level_verdict_path(self):
        digest = "d" * 64
        parsed = {"verdict": "PASS", "manifest_sha256": digest,
                  "findings": []}
        status, verdict = _extract_fleet_verdict(parsed, digest)
        self.assertEqual(status, "ok")
        self.assertEqual(verdict["verdict"], "PASS")

    def test_verdict_embedded_in_summary_path(self):
        digest = "e" * 64
        summary = (
            "The reviewer finished. "
            '{"verdict":"REPAIR","manifest_sha256":"' + digest + '",'
            '"findings":["missing citation", "out of date"]}'
            " End of review."
        )
        parsed = {"files_changed": [], "next_steps": [""], "risks": [""],
                  "status": "OK", "summary": summary,
                  "verification": []}
        status, verdict = _extract_fleet_verdict(parsed, digest)
        self.assertEqual(status, "ok")
        self.assertEqual(verdict["verdict"], "REPAIR")
        self.assertEqual(verdict["findings"],
                         ["missing citation", "out of date"])

    def test_verdict_in_final_string_path(self):
        digest = "f" * 64
        parsed = {"final": json.dumps(
            {"verdict": "PASS", "manifest_sha256": digest,
             "findings": []})}
        status, verdict = _extract_fleet_verdict(parsed, digest)
        self.assertEqual(status, "ok")
        self.assertEqual(verdict["verdict"], "PASS")

    def test_manifest_sha256_mismatch_returns_manifest_mismatch(self):
        parsed = {"verdict": "PASS", "manifest_sha256": "0" * 64,
                  "findings": []}
        status, verdict = _extract_fleet_verdict(parsed, "f" * 64)
        self.assertEqual(status, "manifest_mismatch")
        self.assertEqual(verdict["verdict"], "PASS")

    def test_embedded_manifest_sha256_mismatch_returns_manifest_mismatch(self):
        digest = "a" * 64
        summary = (
            '{"verdict":"REPAIR","manifest_sha256":"' + ("b" * 64) + '",'
            '"findings":["x"]}')
        parsed = {"summary": summary}
        status, verdict = _extract_fleet_verdict(parsed, digest)
        self.assertEqual(status, "manifest_mismatch")
        self.assertEqual(verdict["manifest_sha256"], "b" * 64)

    def test_missing_verdict_returns_missing(self):
        parsed = {"files_changed": [], "next_steps": [], "risks": [],
                  "status": "OK", "summary": "no verdict here",
                  "verification": []}
        status, verdict = _extract_fleet_verdict(parsed, "d" * 64)
        self.assertEqual(status, "missing")
        self.assertIsNone(verdict)

    def test_bare_pass_in_prose_is_not_accepted(self):
        """Prose saying PASS must not be confused for a structured verdict."""
        parsed = {"summary": "Reviewer said PASS with no structure."}
        status, _ = _extract_fleet_verdict(parsed, "d" * 64)
        self.assertEqual(status, "missing")

    def test_nested_handoff_dict_is_inspected(self):
        """When a Fleet envelope wraps the handoff in an outer dict,
        the extraction inspects the nested ``handoff`` key as well as
        the root."""
        digest = "c" * 64
        # Nested handoff with verdict at top level.
        parsed = {
            "envelope_kind": "fleet",
            "handoff": {"verdict": "PASS", "manifest_sha256": digest,
                        "findings": []},
        }
        status, verdict = _extract_fleet_verdict(parsed, digest)
        self.assertEqual(status, "ok")
        self.assertEqual(verdict["verdict"], "PASS")
        # Nested handoff with verdict embedded in ``summary``.
        parsed = {
            "envelope_kind": "fleet",
            "handoff": {"summary": json.dumps(
                {"verdict": "REPAIR", "manifest_sha256": digest,
                 "findings": ["x"]})},
        }
        status, verdict = _extract_fleet_verdict(parsed, digest)
        self.assertEqual(status, "ok")
        self.assertEqual(verdict["verdict"], "REPAIR")
        self.assertEqual(verdict["findings"], ["x"])


class FleetVerdictIntegrationTests(unittest.TestCase):
    """End-to-end through ``produce_reviewed_candidate``."""

    def _fleet_config(self):
        return {
            "maker_timeout_seconds": 400,
            "reviewer_timeout_seconds": 400,
            "reviewer": {
                "mode": "fleet",
                "candidates": [
                    {"agent": "grok", "model": "grok-4.6"},
                ],
            },
        }

    def test_repair_verdict_in_summary_runs_repair_round_and_second_review(
            self):
        """A REPAIR verdict embedded in ``summary`` is read together with
        findings. The repair round runs, the second candidate returns
        PASS, and the publication completes."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(root, publication=self._fleet_config())
            spec = _candidate_spec()

            class _DualRound:
                """Returns REPAIR first, then PASS — keyed to manifest SHA."""
                def __init__(self):
                    self.calls = 0

                def invoke(self, **kwargs):
                    self.calls += 1
                    pubs = sorted(
                        Path(kwargs["cwd"]).glob("**/publication.json"),
                        key=lambda p: p.stat().st_mtime,
                    )
                    digest = sha(Path(pubs[-1]).read_bytes())
                    if self.calls == 1:
                        summary = (
                            "The reviewer noticed two issues. "
                            + _repair_summary_json(digest, "REPAIR-MARK")
                            + " Please fix.")
                    else:
                        summary = (
                            "All clear. " + _pass_summary_json(digest))
                    return _fleet_handoff_with_summary(summary)

            reviewer = _DualRound()
            maker = _SequenceMaker([json.dumps(_valid_decision()),
                                    json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            fleet_reviewers = [{"transport": reviewer, "spec": spec}]
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-20T00:00:00+00:00",
                cfg=cfg, rid="rid-fleet-repair",
                maker=maker, fleet_reviewers=fleet_reviewers,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            # Both the first review (REPAIR) and the second review (PASS)
            # were attempted.
            self.assertEqual(reviewer.calls, 2)
            # The maker was invoked twice: original + repair round.
            self.assertEqual(len(maker.calls), 2)
            # The repair prompt's body received the sanitized findings;
            # the original prompt and the repair prompt both target the
            # same project. The second maker call's prompt must mention
            # the review_REPAIR defect code that was wired in.
            self.assertIn("review_REPAIR", maker.calls[1]["prompt"])

    def test_pass_verdict_in_summary_accepts_publication(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(root, publication=self._fleet_config())
            spec = _candidate_spec()

            class _Pass:
                def __init__(self):
                    self.calls = 0
                def invoke(self, **kwargs):
                    self.calls += 1
                    pubs = sorted(
                        Path(kwargs["cwd"]).glob("**/publication.json"),
                        key=lambda p: p.stat().st_mtime,
                    )
                    digest = sha(Path(pubs[-1]).read_bytes())
                    summary = "All clear. " + _pass_summary_json(digest)
                    return _fleet_handoff_with_summary(summary)

            reviewer = _Pass()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            fleet_reviewers = [{"transport": reviewer, "spec": spec}]
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-20T00:00:00+00:00",
                cfg=cfg, rid="rid-fleet-pass",
                maker=maker, fleet_reviewers=fleet_reviewers,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(reviewer.calls, 1)
            self.assertEqual(result["reviewer_model"], "grok-4.6")
            # A fleet-shaped review.json + semantic-review.json were
            # written; no silent independent_review_not_accepted.
            run_root = root / "run"
            reviews = list(run_root.rglob("review.json"))
            self.assertEqual(len(reviews), 1)
            semantic = list(run_root.rglob("semantic-review.json"))
            self.assertEqual(len(semantic), 1)

    def test_verdict_in_final_field_is_accepted(self):
        """When the envelope's ``final`` is a verdict JSON directly (the
        pre-2026-09-20 shape some Fleet responses still use), the
        extraction honours it."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(root, publication=self._fleet_config())
            spec = _candidate_spec()

            class _Pass:
                def __init__(self):
                    self.calls = 0
                def invoke(self, **kwargs):
                    self.calls += 1
                    pubs = sorted(
                        Path(kwargs["cwd"]).glob("**/publication.json"),
                        key=lambda p: p.stat().st_mtime,
                    )
                    digest = sha(Path(pubs[-1]).read_bytes())
                    final = json.dumps(
                        {"verdict": "PASS", "manifest_sha256": digest,
                         "findings": []})
                    return Envelope(
                        ok=True, status="ok", final=final,
                        model="grok-4.6", provider="grok",
                        session_id="sess-fleet-final",
                        raw={"ok": True, "status": "ok",
                             "provider": "grok", "model": "grok-4.6",
                             "sessionId": "sess-fleet-final",
                             "final": final},
                    )

            reviewer = _Pass()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            fleet_reviewers = [{"transport": reviewer, "spec": spec}]
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-20T00:00:00+00:00",
                cfg=cfg, rid="rid-fleet-final-direct",
                maker=maker, fleet_reviewers=fleet_reviewers,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(reviewer.calls, 1)

    def test_verdict_directly_in_handoff_is_accepted(self):
        """When a future Fleet response lifts the verdict into the handoff
        root (no embedding inside ``summary``), the extraction honours it
        verbatim. The handoff root is what becomes ``Envelope.final``."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(root, publication=self._fleet_config())
            spec = _candidate_spec()

            class _Pass:
                def __init__(self):
                    self.calls = 0
                def invoke(self, **kwargs):
                    self.calls += 1
                    pubs = sorted(
                        Path(kwargs["cwd"]).glob("**/publication.json"),
                        key=lambda p: p.stat().st_mtime,
                    )
                    digest = sha(Path(pubs[-1]).read_bytes())
                    handoff = {
                        "files_changed": [], "next_steps": [""],
                        "risks": [""], "status": "OK",
                        "summary": "no verdict block here",
                        "verification": [],
                        "verdict": "PASS",
                        "manifest_sha256": digest,
                        "findings": [],
                    }
                    return Envelope(
                        ok=True, status="ok",
                        final=json.dumps(handoff),
                        model="grok-4.6", provider="grok",
                        session_id="sess-fleet-direct",
                        raw={"ok": True, "status": "ok",
                             "provider": "grok", "model": "grok-4.6",
                             "sessionId": "sess-fleet-direct",
                             "final": json.dumps(handoff)},
                    )

            reviewer = _Pass()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            fleet_reviewers = [{"transport": reviewer, "spec": spec}]
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-20T00:00:00+00:00",
                cfg=cfg, rid="rid-fleet-handoff-direct",
                maker=maker, fleet_reviewers=fleet_reviewers,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(reviewer.calls, 1)

    def test_manifest_sha256_mismatch_is_rejected_with_stable_reason(self):
        """A verdict whose ``manifest_sha256`` does not match the
        manifest under review must NOT be treated as PASS. It triggers
        a stable deferral reason (``fleet_verdict_manifest_mismatch``)
        that propagates via ``independent_review_deferred``."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(root, publication=self._fleet_config())
            spec = _candidate_spec()

            class _WrongDigest:
                def __init__(self):
                    self.calls = 0
                    self.prompts = []
                def invoke(self, **kwargs):
                    self.calls += 1
                    self.prompts.append(kwargs["prompt"])
                    summary = json.dumps(
                        {"verdict": "PASS",
                         "manifest_sha256": "0" * 64, "findings": []})
                    return _fleet_handoff_with_summary(summary)

            reviewer = _WrongDigest()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            fleet_reviewers = [{"transport": reviewer, "spec": spec}]
            (root / "run").mkdir()
            with self.assertRaises(PublicationError) as cm:
                produce_reviewed_candidate(
                    frozen=frozen, run_root=root / "run",
                    observed="2026-09-20T00:00:00+00:00",
                    cfg=cfg, rid="rid-fleet-mismatch",
                    maker=maker, fleet_reviewers=fleet_reviewers,
                )
            self.assertIn("independent_review_deferred", str(cm.exception))
            self.assertIn(
                _FLEET_DEFER_REASON_VERDICT_MANIFEST_MISMATCH,
                str(cm.exception),
            )
            # A digest mismatch is NOT a format defect and is never repaired:
            # re-prompting with the right digest would let the reviewer copy
            # it onto a judgement about another manifest (0.1.16 review).
            self.assertEqual(reviewer.calls, 1)
            # No silent PASS: no review.json was written.
            self.assertEqual(list((root / "run").rglob("review.json")), [])

    def test_missing_verdict_defers_with_clear_reason(self):
        """A Fleet envelope whose ``summary`` carries no structured verdict
        defers with a stable reason; it is never silently treated as
        ``independent_review_not_accepted``."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(root, publication=self._fleet_config())
            spec = _candidate_spec()

            class _NoVerdict:
                def __init__(self):
                    self.calls = 0
                    self.prompts = []
                def invoke(self, **kwargs):
                    self.calls += 1
                    self.prompts.append(kwargs["prompt"])
                    return _fleet_handoff_with_summary(
                        "Reviewer returned prose only; no JSON block.")

            reviewer = _NoVerdict()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            fleet_reviewers = [{"transport": reviewer, "spec": spec}]
            (root / "run").mkdir()
            with self.assertRaises(PublicationError) as cm:
                produce_reviewed_candidate(
                    frozen=frozen, run_root=root / "run",
                    observed="2026-09-20T00:00:00+00:00",
                    cfg=cfg, rid="rid-fleet-missing",
                    maker=maker, fleet_reviewers=fleet_reviewers,
                )
            message = str(cm.exception)
            self.assertIn("independent_review_deferred", message)
            self.assertIn(_FLEET_DEFER_REASON_VERDICT_MISSING, message)
            self.assertNotIn("independent_review_not_accepted", message)
            # Only one maker call: the missing verdict is caught before
            # any repair round is queued.
            self.assertEqual(len(maker.calls), 1)
            # 0.1.16: the prose-only reviewer is re-prompted with the exact
            # reason and its own answer before the candidate is given up.
            self.assertEqual(reviewer.calls, 1 + FLEET_FORMAT_REPAIR_ATTEMPTS)
            for repair in reviewer.prompts[1:]:
                self.assertTrue(repair.startswith("FORMAT REPAIR"))
                self.assertIn('no JSON object with a top-level "verdict" key', repair)
                self.assertIn("Reviewer returned prose only", repair)
            # No review.json was written.
            self.assertEqual(list((root / "run").rglob("review.json")), [])

    def test_review_content_does_not_leak_to_logs(self):
        """Findings and verdict prose must NOT appear in any log record.

        The deferred reason and any persisted diagnostics also stay free
        of the reviewer's review content; only the stable deferral code
        and the manifest_sha256 appear in the deferred outcome.
        """
        marker = "UNIQUE-FINDING-MARKER-DO-NOT-LOG-9f3a"
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(root, publication=self._fleet_config())
            spec = _candidate_spec()

            class _RepairWithMarker:
                def __init__(self):
                    self.calls = 0

                def invoke(self, **kwargs):
                    self.calls += 1
                    pubs = sorted(
                        Path(kwargs["cwd"]).glob("**/publication.json"),
                        key=lambda p: p.stat().st_mtime,
                    )
                    digest = sha(Path(pubs[-1]).read_bytes())
                    if self.calls == 1:
                        # First review: REPAIR with the marker in the
                        # findings. The repair round will be wired.
                        summary = json.dumps(
                            {"verdict": "REPAIR",
                             "manifest_sha256": digest,
                             "findings": [
                                 f"{marker}: missing citation",
                                 f"{marker}: stale date"]})
                        return _fleet_handoff_with_summary(summary)
                    # Second review: PASS.
                    summary = json.dumps(
                        {"verdict": "PASS", "manifest_sha256": digest,
                         "findings": []})
                    return _fleet_handoff_with_summary(summary)

            reviewer = _RepairWithMarker()
            maker = _SequenceMaker([json.dumps(_valid_decision()),
                                    json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            fleet_reviewers = [{"transport": reviewer, "spec": spec}]
            (root / "run").mkdir()

            records: list = []

            class _Capture(logging.Handler):
                def emit(self, record):
                    records.append(record.getMessage())
                    records.append(self.format(record))

            handler = _Capture()
            root_logger = logging.getLogger()
            previous_level = root_logger.level
            previous_handlers = list(root_logger.handlers)
            root_logger.addHandler(handler)
            root_logger.setLevel(logging.DEBUG)
            try:
                result = produce_reviewed_candidate(
                    frozen=frozen, run_root=root / "run",
                    observed="2026-09-20T00:00:00+00:00",
                    cfg=cfg, rid="rid-fleet-leak",
                    maker=maker, fleet_reviewers=fleet_reviewers,
                )
            finally:
                root_logger.removeHandler(handler)
                root_logger.setLevel(previous_level)
                for h in previous_handlers:
                    if h not in root_logger.handlers:
                        root_logger.addHandler(h)

            joined = "\n".join(records)
            self.assertNotIn(marker, joined,
                             "review findings leaked into log records")
            # The review body used a unique marker; also assert the
            # verdict text "PASS"/"REPAIR" did not leak via findings
            # into the maker prompt's prompt_bytes (already excluded by
            # the test above, but here the explicit findings MUST NOT
            # appear in any captured record).
            self.assertNotIn("missing citation", joined)
            # The repair prompt the maker received sanitized the
            # findings; the marker is filtered out of the maker's
            # input. The marker must not appear in the persisted
            # result either.
            diagnostics_path = root / "run" / "result.json"
            if diagnostics_path.exists():
                persisted = diagnostics_path.read_text(encoding="utf-8")
                self.assertNotIn(marker, persisted)
            # Publication succeeded via the second review.
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            # The first review returned REPAIR, so the second candidate
            # call ran.
            self.assertEqual(reviewer.calls, 2)

    def test_repair_then_missing_verdict_defers_with_stable_reason(self):
        """When the first review returns REPAIR but the second review
        returns no structured verdict, the publication defers with a
        clear reason (``fleet_verdict_missing``), not the silent
        ``independent_review_not_accepted``."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(root, publication=self._fleet_config())
            spec = _candidate_spec()

            class _RepairThenMissing:
                def __init__(self):
                    self.calls = 0
                def invoke(self, **kwargs):
                    self.calls += 1
                    pubs = sorted(
                        Path(kwargs["cwd"]).glob("**/publication.json"),
                        key=lambda p: p.stat().st_mtime,
                    )
                    digest = sha(Path(pubs[-1]).read_bytes())
                    if self.calls == 1:
                        summary = json.dumps(
                            {"verdict": "REPAIR",
                             "manifest_sha256": digest,
                             "findings": ["citation gap"]})
                        return _fleet_handoff_with_summary(summary)
                    return _fleet_handoff_with_summary(
                        "reviewer returned prose only")

            reviewer = _RepairThenMissing()
            maker = _SequenceMaker([json.dumps(_valid_decision()),
                                    json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            fleet_reviewers = [{"transport": reviewer, "spec": spec}]
            (root / "run").mkdir()
            with self.assertRaises(PublicationError) as cm:
                produce_reviewed_candidate(
                    frozen=frozen, run_root=root / "run",
                    observed="2026-09-20T00:00:00+00:00",
                    cfg=cfg, rid="rid-fleet-repair-missing",
                    maker=maker, fleet_reviewers=fleet_reviewers,
                )
            message = str(cm.exception)
            self.assertIn("independent_review_deferred", message)
            self.assertIn(_FLEET_DEFER_REASON_VERDICT_MISSING, message)
            self.assertNotIn("independent_review_not_accepted", message)
            # Both reviews were attempted (REPAIR → repair round → 2nd); the
            # 2nd review's missing verdict gets the 0.1.16 format repairs.
            self.assertEqual(reviewer.calls, 2 + FLEET_FORMAT_REPAIR_ATTEMPTS)
            # Maker ran twice (original + repair round); a FORMAT repair of
            # the reviewer never re-runs the maker.
            self.assertEqual(len(maker.calls), 2)


class LegacyMuseModeRegressionTests(unittest.TestCase):
    """The legacy Muse path is unchanged bit-for-bit."""

    def test_no_publication_section_uses_muse_path(self):
        """When no ``publication`` section is set, the legacy Muse path
        is used and the verdict is read directly from the Muse-shaped
        envelope (top-level verdict in ``final``)."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(root, publication=None)

            class _MuseReviewer:
                def __init__(self):
                    self.calls = 0
                def invoke(self, **kwargs):
                    self.calls += 1
                    pubs = sorted(
                        Path(kwargs["cwd"]).glob("**/publication.json"),
                        key=lambda p: p.stat().st_mtime,
                    )
                    digest = sha(Path(pubs[-1]).read_bytes())
                    final = json.dumps(
                        {"verdict": "PASS", "manifest_sha256": digest,
                         "findings": []})
                    return Envelope(
                        ok=True, status="ok", final=final,
                        model="muse-spark-1.3", provider="muse-code",
                        session_id="sess-muse",
                        raw={"ok": True, "status": "ok",
                             "provider": "muse-code",
                             "model": "muse-spark-1.3",
                             "sessionId": "sess-muse",
                             "final": final},
                    )

            muse = _MuseReviewer()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-20T00:00:00+00:00",
                cfg=cfg, rid="rid-legacy-muse",
                maker=maker, reviewer=muse,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["reviewer_model"], "muse-spark-1.3")
            self.assertEqual(muse.calls, 1)

    def test_explicit_muse_mode_keeps_legacy_path(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(
                root, publication={"reviewer": {"mode": "muse"}})

            class _MuseReviewer:
                def __init__(self):
                    self.calls = 0
                def invoke(self, **kwargs):
                    self.calls += 1
                    pubs = sorted(
                        Path(kwargs["cwd"]).glob("**/publication.json"),
                        key=lambda p: p.stat().st_mtime,
                    )
                    digest = sha(Path(pubs[-1]).read_bytes())
                    final = json.dumps(
                        {"verdict": "PASS", "manifest_sha256": digest,
                         "findings": []})
                    return Envelope(
                        ok=True, status="ok", final=final,
                        model="muse-spark-1.3", provider="muse-code",
                        session_id="sess-muse",
                        raw={"ok": True, "status": "ok",
                             "provider": "muse-code",
                             "model": "muse-spark-1.3",
                             "sessionId": "sess-muse",
                             "final": final},
                    )

            muse = _MuseReviewer()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-20T00:00:00+00:00",
                cfg=cfg, rid="rid-legacy-muse-explicit",
                maker=maker, reviewer=muse,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["reviewer_model"], "muse-spark-1.3")


if __name__ == "__main__":
    unittest.main()
