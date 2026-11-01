"""Wire-up tests for the production publication entry's fleet wiring.

Production entry ``_wire_fleet_publication`` must build the ``fleet_reviewers``
list from ``cfg.publication.reviewer.candidates`` whenever the configured
reviewer mode is ``"fleet"``, in the same order the configuration supplies
them. Before this slice, the production call site passed no
``fleet_reviewers`` and the inner ``use_fleet_mode = (reviewer_mode == 'fleet'
and fleet_reviewers is not None)`` guard was permanently False, so enabling
the fleet mode silently fell back to the legacy Muse path. These tests pin
the new wire-up so that misconfiguration surfaces to the operator instead of
being hidden.

The suite also pins that the legacy ``"muse"`` mode and the absence of a
reviewer section leave the existing behaviour bit-for-bit identical, and
that a fleet configuration whose candidate list cannot be built defers the
publication with a clear reason rather than silently reverting to Muse.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from caretaker.config import load_config
from caretaker.publication import PublicationError, freeze, sha
from caretaker.transports import AgentFleetArgvTransport, Envelope, TransportError
from curate_once import (
    _build_fleet_reviewers,
    _wire_fleet_publication,
)
from ._helpers import write_config


TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"


def _mkdir_tmp():
    TASK_TMP.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(TASK_TMP))


def _vault(root: Path):
    """Mirror the FormatRepairTests fixture: one proposal, one reviewed page."""
    vault = root / "vault"
    source = vault / "raw/sources/2026/09/src-one"
    source.mkdir(parents=True)
    data = b"Dated report: local tool canary passed; deployment remains unverified."
    (source / "original.md").write_bytes(data)
    meta = {
        "source_id": "src-one", "classification": "internal", "local_only": False,
        "original_file": "original.md", "sha256": sha(data), "byte_length": len(data),
    }
    (source / "source.json").write_text(json.dumps(meta), encoding="utf-8")
    proposal = vault / "proposals/agent/one.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text(
        "---\nid: prop-one\nclassification: internal\nsource_ids:\n  - src-one\n"
        "target_pages:\n  - wiki/projects/test.md\n---\nReport.\n",
        encoding="utf-8",
    )
    target = vault / "wiki/projects/test.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        '---\ntitle: Test\nstatus: reviewed\nlast_verified: "2026-01-01"\n'
        "sources: []\nproject_slug: test\n---\n\n# Test\nOld unresolved question.\n",
        encoding="utf-8",
    )
    return vault, freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")


def _valid_decision(pid="prop-one"):
    return {
        "body": "# Test\nOld unresolved question.\nDated tool canary report (src-one); deployment unverified.",
        "summary": "Tool canary report.",
        "dispositions": {pid: {"disposition": "synthesized",
                                "reason": "Dated and scope-limited report integrated."}},
    }


def _load_cfg(tmp: Path, publication=None) -> tuple:
    """Return (cfg, cfg_path) for a freshly written config.

    ``publication`` is merged into the example config so callers can exercise
    both ``mode`` and the absence of a publication section. ``None`` keeps the
    legacy layout (no publication section).
    """
    cfg_dir = tmp / "cfg"; cfg_dir.mkdir()
    cfg_path = write_config(cfg_dir)
    raw = json.loads(cfg_path.read_text("utf-8"))
    if publication is not None:
        raw["publication"] = publication
        cfg_path.write_text(json.dumps(raw, indent=2), "utf-8")
    elif "publication" in raw:
        raw.pop("publication")
        cfg_path.write_text(json.dumps(raw, indent=2), "utf-8")
    return load_config(cfg_path), cfg_path


class _SequenceMaker:
    """Records every (timeout, prompt) and returns the queued maker decision."""

    def __init__(self, finals, provider, model, session_id=None):
        self.finals = list(finals)
        self.calls = []
        self.provider = provider
        self.model = model
        self.session_id = session_id

    def invoke(self, *, spec, cwd, prompt, run_config_path, state_dir, timeout):
        self.calls.append({"timeout": timeout, "prompt": prompt, "cwd": cwd,
                           "run_config_path": run_config_path, "state_dir": state_dir})
        if not self.finals:
            raise AssertionError("unexpected extra maker call")
        final = self.finals.pop(0)
        return Envelope(
            ok=True, status="ok", final=final, model=self.model,
            provider=self.provider, session_id=self.session_id,
            raw={"ok": True, "status": "ok", "provider": self.provider,
                 "model": self.model, "sessionId": self.session_id, "final": final},
        )


class _MusePassReviewer:
    """Muse-shaped reviewer: PASS keyed to the manifest's actual SHA256."""

    def __init__(self, provider="muse-code", model="muse-spark-1.3",
                 session_id="sess-1"):
        self.calls = []
        self.provider = provider
        self.model = model
        self.session_id = session_id

    def invoke(self, *, spec, cwd, prompt, run_config_path, state_dir, timeout):
        self.calls.append({"timeout": timeout, "prompt": prompt, "cwd": cwd})
        pubs = sorted(Path(cwd).glob("**/publication.json"), key=lambda p: p.stat().st_mtime)
        digest = sha(pubs[-1].read_bytes())
        final = json.dumps({"verdict": "PASS", "manifest_sha256": digest,
                            "findings": []})
        return Envelope(
            ok=True, status="ok", final=final, model=self.model,
            provider=self.provider, session_id=self.session_id,
            raw={"ok": True, "status": "ok", "provider": self.provider,
                 "model": self.model, "sessionId": self.session_id, "final": final},
        )


class BuildFleetReviewersTests(unittest.TestCase):
    """``_build_fleet_reviewers`` constructs the list from configuration."""

    def test_fleet_mode_builds_candidates_in_config_order(self):
        """Every candidate in the configuration appears in the built list,
        in the exact same order. Each candidate carries a Fleet transport
        and a launch spec that mentions its agent and model."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            cfg, _ = _load_cfg(
                root,
                publication={"reviewer": {
                    "mode": "fleet",
                    "candidates": [
                        {"agent": "grok", "model": "grok-4.6"},
                        {"agent": "minimax", "model": "MiniMax-M3"},
                        {"agent": "muse", "model": "muse-spark-1.3"},
                    ],
                }},
            )
            fleet = _build_fleet_reviewers(cfg)
            self.assertIsNotNone(fleet)
            self.assertEqual(len(fleet), 3)
            # The configuration order is preserved bit-for-bit.
            self.assertEqual([c["lineage"] for c in fleet],
                             ["grok", "minimax", "muse"])
            argv_strings = [" ".join(c["spec"].argv) for c in fleet]
            argv_token_lists = [list(c["spec"].argv) for c in fleet]
            # Each candidate pins its ``--agent``/``--model`` flags. Fleet
            # accepts the model name as a separate flag (not a slashed
            # ``agent/model`` token) and we run through the Fleet-shaped
            # subcommand ``run`` with the prompt staged via ``--task-file``.
            self.assertIn("--agent grok", argv_strings[0])
            self.assertIn("--model grok-4.6", argv_strings[0])
            self.assertIn("--agent minimax", argv_strings[1])
            self.assertIn("--model MiniMax-M3", argv_strings[1])
            self.assertIn("--agent muse", argv_strings[2])
            self.assertIn("--model muse-spark-1.3", argv_strings[2])
            for line in argv_strings:
                self.assertIn("run", line)
                self.assertIn("--task-file", line)
            # The 2026-09-20 probe only validated --task-file + --agent +
            # --model as the minimum required for Fleet to return a
            # parseable JSON envelope; we deliberately do NOT pass the
            # unvalidated --assessment-file / --mode / --kind flags.
            # Compare exact argv TOKENS: '--mode' is a substring of
            # '--model', so substring matching false-positives here.
            for tokens in argv_token_lists:
                for absent in ("--assessment-file", "--mode", "--kind",
                               "--complexity", "--timeout", "--workspace"):
                    self.assertNotIn(absent, tokens,
                                     f"unvalidated flag in spec: {absent}")
            # Each entry is a real Fleet transport with the expected spec.
            for cand in fleet:
                self.assertIsInstance(cand["transport"], AgentFleetArgvTransport)
                self.assertEqual(cand["spec"].kind, "openclaw_argv")
                # The transport spawns ``python fleet.py run ...``; the
                # executable is the Python interpreter and the placeholder
                # ``${fleet_py}`` resolves to the real fleet.py path at
                # invoke time. The previous artefact ``agent-fleet.cmd``
                # never existed on disk; this assertion pins the fix.
                self.assertEqual(cand["spec"].executable, sys.executable)
                joined = " ".join(cand["spec"].argv)
                self.assertNotIn("agent-fleet.cmd", joined)

    def test_muse_mode_does_not_build_fleet_reviewers(self):
        """``mode == 'muse'`` is the legacy behaviour and must NOT touch the
        fleet code path."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            cfg, _ = _load_cfg(
                root,
                publication={"reviewer": {"mode": "muse"}},
            )
            self.assertIsNone(_build_fleet_reviewers(cfg))

    def test_no_publication_section_does_not_build_fleet_reviewers(self):
        """Existing production control.json omits the whole publication
        section; that must continue to behave like Muse (i.e. the helper
        returns None, no fleet candidates are constructed)."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            cfg, _ = _load_cfg(root, publication=None)
            self.assertIsNone(_build_fleet_reviewers(cfg))

    def test_publication_section_without_reviewer_does_not_build_fleet_reviewers(self):
        """A publication section present but without a reviewer subsection
        falls back to the legacy Muse path."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            cfg, _ = _load_cfg(root, publication={})
            self.assertIsNone(_build_fleet_reviewers(cfg))

    def test_empty_candidates_in_fleet_mode_raises_for_deferral(self):
        """Defensive guard: if the validator were ever bypassed and the
        fleet-mode configuration ended up with no candidates, the helper
        must surface that as an error so the wire-up defers with a clear
        reason instead of silently dropping into Muse."""
        cfg = SimpleNamespace(
            publication=SimpleNamespace(
                reviewer=SimpleNamespace(mode="fleet", candidates=[]),
            ),
        )
        with self.assertRaisesRegex(PublicationError,
                                    "fleet_reviewers_unavailable"):
            _build_fleet_reviewers(cfg)


class WireFleetPublicationTests(unittest.TestCase):
    """``_wire_fleet_publication`` is the production wire-up."""

    def test_fleet_candidates_reach_produce_reviewed_candidate(self):
        """Built candidates are actually passed to the reviewer loop and
        the Muse-shaped fallback is NOT used. ``use_fleet_mode`` becomes
        True and the first candidate's transport is invoked once."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(
                root,
                publication={"reviewer": {
                    "mode": "fleet",
                    "candidates": [
                        {"agent": "grok", "model": "grok-4.6"},
                        {"agent": "muse", "model": "muse-spark-1.3"},
                    ],
                }},
            )
            class _Recorder:
                def __init__(self, label, provider, model):
                    self.label = label
                    self.provider = provider
                    self.model = model
                    self.calls = []

                def invoke(self, **kwargs):
                    self.calls.append({
                        "timeout": kwargs["timeout"],
                        "spec_executable": kwargs["spec"].executable,
                    })
                    pubs = sorted(Path(kwargs["cwd"]).glob("**/publication.json"),
                                  key=lambda p: p.stat().st_mtime)
                    digest = sha(Path(pubs[-1]).read_bytes())
                    final = json.dumps({"verdict": "PASS",
                                        "manifest_sha256": digest,
                                        "findings": []})
                    return Envelope(
                        ok=True, status="ok", final=final,
                        model=self.model, provider=self.provider,
                        session_id="sess-" + self.label,
                        raw={"ok": True, "status": "ok",
                             "provider": self.provider,
                             "model": self.model,
                             "sessionId": "sess-" + self.label,
                             "final": final},
                    )

            grok_rec = _Recorder("grok", "grok", "grok-4.6")
            muse_rec = _Recorder("muse", "muse-code", "muse-spark-1.3")
            stubs = {
                ("grok", "grok-4.6"): grok_rec,
                ("muse", "muse-spark-1.3"): muse_rec,
            }

            def factory(agent, model):
                return stubs[(agent, model)]

            built = _build_fleet_reviewers(cfg, transport_factory=factory)
            self.assertIsNotNone(built)
            self.assertEqual(len(built), 2)
            self.assertIs(built[0]["transport"], grok_rec)
            self.assertIs(built[1]["transport"], muse_rec)

            (root / "run").mkdir()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            # NOTE: no muse reviewer is injected. If the wire-up fell back
            # to Muse, this call would either fail (the real openclaw.cmd
            # binary is unavailable in the test) or pass reviewer_model
            # "muse-spark-1.3" against a non-existent Muse transport;
            # both outcomes would fail the assertions below.
            result = _wire_fleet_publication(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid-wire-fleet",
                maker=maker, reviewer=None,
                transport_factory=factory,
            )
            # The first candidate was actually invoked (use_fleet_mode is
            # True; the loop in _invoke_fleet_review entered).
            self.assertEqual(len(grok_rec.calls), 1)
            # The first candidate's transport.spec was passed through. The
            # spec pins ``sys.executable`` so the Fleet transport can spawn
            # ``python <fleet.py> run ...`` against the real entry point.
            self.assertEqual(grok_rec.calls[0]["spec_executable"],
                             sys.executable)
            self.assertEqual(grok_rec.calls[0]["timeout"], cfg.publication.reviewer_timeout_seconds)
            # The first candidate returned PASS, so the loop ends before
            # reaching the second candidate.
            self.assertEqual(len(muse_rec.calls), 0)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            # The legacy Muse-shaped fallback was NOT used: the reviewer's
            # model is the first candidate's, not muse-spark-1.3.
            self.assertEqual(result["reviewer_model"], "grok-4.6")
            self.assertNotEqual(result["reviewer_model"], "muse-spark-1.3")

    def test_fleet_candidates_walk_in_config_order(self):
        """When the first two candidates transport-outage, only the third
        candidate can return PASS. The loop must walk the list in
        configuration order: grok tried first, then minimax, then muse.
        If the helper had reordered, muse would PASS first, the loop
        would exit, and grok + minimax would never be called."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(
                root,
                publication={"reviewer": {
                    "mode": "fleet",
                    "candidates": [
                        {"agent": "grok", "model": "grok-4.6"},
                        {"agent": "minimax", "model": "MiniMax-M3"},
                        {"agent": "muse", "model": "muse-spark-1.3"},
                    ],
                }},
            )
            class _Outage:
                def __init__(self, label):
                    self.label = label
                    self.calls = 0

                def invoke(self, **kwargs):
                    self.calls += 1
                    raise TransportError(self.label + " down",
                                         reason_code="process_timeout")

            class _Pass:
                def __init__(self, label):
                    self.label = label
                    self.calls = 0

                def invoke(self, **kwargs):
                    self.calls += 1
                    pubs = sorted(Path(kwargs["cwd"]).glob("**/publication.json"),
                                  key=lambda p: p.stat().st_mtime)
                    digest = sha(Path(pubs[-1]).read_bytes())
                    final = json.dumps({"verdict": "PASS",
                                        "manifest_sha256": digest,
                                        "findings": []})
                    return Envelope(
                        ok=True, status="ok", final=final,
                        model=self.label + "-model", provider=self.label,
                        session_id="sess-" + self.label,
                        raw={"ok": True, "status": "ok",
                             "provider": self.label,
                             "model": self.label + "-model",
                             "sessionId": "sess-" + self.label,
                             "final": final},
                    )

            grok_rec = _Outage("grok")
            minimax_rec = _Outage("minimax")
            muse_rec = _Pass("muse")
            stubs = {
                ("grok", "grok-4.6"): grok_rec,
                ("minimax", "MiniMax-M3"): minimax_rec,
                ("muse", "muse-spark-1.3"): muse_rec,
            }

            def factory(agent, model):
                return stubs[(agent, model)]

            (root / "run").mkdir()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            result = _wire_fleet_publication(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid-wire-fleet-order",
                maker=maker, reviewer=None,
                transport_factory=factory,
            )
            # All three candidates were tried; the loop walked the list
            # in configuration order (grok, minimax, muse). grok and
            # minimax outages caused ``continue``, muse PASS exited.
            self.assertEqual(grok_rec.calls, 1)
            self.assertEqual(minimax_rec.calls, 1)
            self.assertEqual(muse_rec.calls, 1)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["reviewer_model"], "muse-model")

    def test_muse_mode_in_production_entry_keeps_legacy_behavior(self):
        """``mode == 'muse'`` makes the wire-up a no-op: the helper returns
        None and the injected Muse reviewer runs the existing review path
        unchanged. This is the contract existing production configs rely on."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(
                root,
                publication={"reviewer": {"mode": "muse"}},
            )
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            muse = _MusePassReviewer()
            (root / "run").mkdir()
            result = _wire_fleet_publication(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid-wire-muse",
                maker=maker, reviewer=muse,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            # The injected Muse reviewer was used end-to-end.
            self.assertEqual(len(muse.calls), 1)
            self.assertEqual(result["reviewer_model"], "muse-spark-1.3")

    def test_no_reviewer_section_in_production_entry_keeps_legacy_behavior(self):
        """When the configuration omits the publication section entirely,
        the wire-up returns None fleet reviewers and the legacy Muse path
        runs as before. Production control.json files that have not opted
        into the new fields must continue to publish exactly as today."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(root, publication=None)
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            muse = _MusePassReviewer()
            (root / "run").mkdir()
            result = _wire_fleet_publication(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid-wire-nosection",
                maker=maker, reviewer=muse,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(len(muse.calls), 1)
            self.assertEqual(result["reviewer_model"], "muse-spark-1.3")

    def test_unbuildable_fleet_candidates_defers_without_muse(self):
        """When mode is ``"fleet"`` but the candidate list cannot be built,
        the wire-up MUST defer the publication with a clear reason. A
        silent fallback to Muse would hide the misconfiguration from the
        operator, so the result must NOT carry the legacy Muse identity."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            # Synthetic cfg that bypasses validation: mode='fleet' but no
            # candidates. The validator rejects this combination, so we
            # build a SimpleNamespace that satisfies the wire-up's
            # defensive guard instead. The wire-up must still surface this
            # as a PENDING defer, not a Muse fallback.
            cfg = SimpleNamespace(
                publication=SimpleNamespace(
                    reviewer=SimpleNamespace(mode="fleet", candidates=[]),
                ),
            )
            (root / "run").mkdir()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            # NO muse reviewer is injected. A silent Muse fallback would
            # either fail (real openclaw.cmd unavailable) or succeed
            # against a non-existent transport; in either case the
            # assertions below would catch the misconfiguration.
            result = _wire_fleet_publication(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid-wire-defer",
                maker=maker, reviewer=None,
            )
            # The wire-up defers; it does not silently publish.
            self.assertEqual(result["status"], "PENDING")
            # The reason points at the fleet configuration so the
            # operator can see what is broken.
            self.assertIn("fleet", result["reason"].lower())
            self.assertEqual(result.get("error_code"),
                             "fleet_reviewers_unavailable")
            # The legacy Muse identity was NOT used: reviewer_model is
            # explicitly None, never muse-spark-1.3.
            self.assertIsNone(result.get("reviewer_model"))
            self.assertNotEqual(result.get("reviewer_model"),
                                "muse-spark-1.3")
            # The result carries run_path / result_path so the caller
            # (``run``) persists the deferred outcome via diagnostics.json
            # + result.json, exactly like the legacy Muse deferral path.
            self.assertEqual(result["run_path"], str(root / "run"))
            self.assertEqual(result["result_path"],
                             str(root / "run" / "result.json"))

    def test_same_lineage_as_maker_is_not_used_as_reviewer(self):
        """A candidate whose envelope identity matches the maker is skipped;
        the next independent candidate reviews."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(
                root,
                publication={"reviewer": {
                    "mode": "fleet",
                    "candidates": [
                        {"agent": "vllm", "model": "Qwen3.8-27B"},
                        {"agent": "grok", "model": "grok-4.6"},
                    ],
                }},
            )

            class _Collision:
                def __init__(self):
                    self.calls = 0

                def invoke(self, **kwargs):
                    self.calls += 1
                    pubs = sorted(Path(kwargs["cwd"]).glob("**/publication.json"),
                                  key=lambda p: p.stat().st_mtime)
                    digest = sha(Path(pubs[-1]).read_bytes())
                    final = json.dumps({"verdict": "PASS",
                                        "manifest_sha256": digest,
                                        "findings": []})
                    return Envelope(
                        ok=True, status="ok", final=final,
                        model="Qwen3.8-27B", provider="vllm",
                        session_id="self-review",
                        raw={"ok": True, "status": "ok",
                             "provider": "vllm", "model": "Qwen3.8-27B",
                             "sessionId": "self-review", "final": final},
                    )

            class _Pass:
                def __init__(self):
                    self.calls = 0

                def invoke(self, **kwargs):
                    self.calls += 1
                    pubs = sorted(Path(kwargs["cwd"]).glob("**/publication.json"),
                                  key=lambda p: p.stat().st_mtime)
                    digest = sha(Path(pubs[-1]).read_bytes())
                    final = json.dumps({"verdict": "PASS",
                                        "manifest_sha256": digest,
                                        "findings": []})
                    return Envelope(
                        ok=True, status="ok", final=final,
                        model="grok-4.6", provider="grok",
                        session_id="sess-grok",
                        raw={"ok": True, "status": "ok",
                             "provider": "grok", "model": "grok-4.6",
                             "sessionId": "sess-grok", "final": final},
                    )

            collision = _Collision()
            grok_rec = _Pass()
            stubs = {
                ("vllm", "Qwen3.8-27B"): collision,
                ("grok", "grok-4.6"): grok_rec,
            }

            def factory(agent, model):
                return stubs[(agent, model)]

            (root / "run").mkdir()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            result = _wire_fleet_publication(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid-wire-lineage",
                maker=maker, reviewer=None,
                transport_factory=factory,
            )
            self.assertEqual(collision.calls, 1)
            self.assertEqual(grok_rec.calls, 1)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["reviewer_model"], "grok-4.6")
            self.assertNotEqual(result["reviewer_model"], "Qwen3.8-27B")

    def test_no_working_candidate_defers_without_muse(self):
        """Every candidate transport-fails: publication is deferred and
        the Muse fallback is not used."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(
                root,
                publication={"reviewer": {
                    "mode": "fleet",
                    "candidates": [
                        {"agent": "grok", "model": "grok-4.6"},
                        {"agent": "minimax", "model": "MiniMax-M3"},
                    ],
                }},
            )

            class _Outage:
                def __init__(self, label):
                    self.label = label
                    self.calls = 0

                def invoke(self, **kwargs):
                    self.calls += 1
                    raise TransportError(self.label + " down",
                                         reason_code="process_timeout")

            grok_rec = _Outage("grok")
            minimax_rec = _Outage("minimax")
            stubs = {
                ("grok", "grok-4.6"): grok_rec,
                ("minimax", "MiniMax-M3"): minimax_rec,
            }

            def factory(agent, model):
                return stubs[(agent, model)]

            (root / "run").mkdir()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            with self.assertRaisesRegex(PublicationError,
                                        "independent_review_deferred"):
                _wire_fleet_publication(
                    frozen=frozen, run_root=root / "run",
                    observed="2026-09-18T00:00:00+00:00",
                    cfg=cfg, rid="rid-wire-all-fail",
                    maker=maker, reviewer=None,
                    transport_factory=factory,
                )
            self.assertEqual(grok_rec.calls, 1)
            self.assertEqual(minimax_rec.calls, 1)
            self.assertEqual(list((root / "run").rglob("review.json")), [])


if __name__ == "__main__":
    unittest.main()