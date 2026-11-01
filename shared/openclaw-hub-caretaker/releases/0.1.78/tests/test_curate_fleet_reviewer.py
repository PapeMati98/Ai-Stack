"""Fleet-mode reviewer and configurable budgets for the curated publication step.

These tests pin down the new behaviour introduced alongside the
``publication`` config section: budgets above the legacy 360 s ceiling come
from configuration (with a floor default larger than 360), and
``publication.reviewer.mode == 'fleet'`` walks an injected candidate list,
skipping candidates that match the maker's lineage and deferring with
``independent_review_deferred`` only when no candidate is available.

The previous Muse subscription path is preserved bit-for-bit when no
``publication`` section is present (or when ``mode`` is ``"muse"``).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.config import (
    DEFAULT_PUBLICATION_FLOOR_TIMEOUT_SECONDS,
    DEFAULT_PUBLICATION_REVIEWER_TIMEOUT_SECONDS,
    DEFAULT_PUBLICATION_TIMEOUT_SECONDS,
    OUTER_PUBLICATION_BUDGET_SECONDS,
    TransportSpec,
    load_config,
)
from caretaker.publication import (
    PublicationError, freeze, sha,
)
from caretaker.transports import Envelope, TransportError
from curate_once import produce_reviewed_candidate
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
    both ``mode`` and explicit budgets. ``None`` keeps the legacy layout
    (no publication section).
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


class _HashPassReviewer:
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


def _candidate_spec():
    return TransportSpec(kind="openclaw_argv", executable="openclaw.cmd",
                         argv=["agent", "exec"])


class LegacyBehaviorTests(unittest.TestCase):
    """No ``publication`` section → identical to today (Muse, 360 s)."""

    def test_no_publication_config_uses_muse_with_360_budget(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(root, publication=None)
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            reviewer = _HashPassReviewer()
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid-legacy", maker=maker, reviewer=reviewer,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            # Maker used the legacy 360 s ceiling (no publication section).
            self.assertEqual(maker.calls[0]["timeout"],
                             DEFAULT_PUBLICATION_TIMEOUT_SECONDS)
            self.assertEqual(maker.calls[0]["timeout"], 360)
            # The injected Muse reviewer was used end-to-end.
            self.assertEqual(len(reviewer.calls), 1)
            self.assertEqual(reviewer.calls[0]["timeout"], 360)
            # Publication still records the muse-spark identity.
            self.assertEqual(result["reviewer_model"], "muse-spark-1.3")

    def test_explicit_muse_mode_keeps_360_budget(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(root, publication={"reviewer": {"mode": "muse"}})
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            reviewer = _HashPassReviewer()
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid-muse", maker=maker, reviewer=reviewer,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            # Explicit mode='muse' preserves the legacy 360 s ceiling.
            self.assertEqual(maker.calls[0]["timeout"], 360)
            self.assertEqual(reviewer.calls[0]["timeout"], 360)


class ConfigurableBudgetTests(unittest.TestCase):
    """With a publication section present, budgets come from config."""

    def test_fleet_publication_default_floor_exceeds_360(self):
        """Fleet mode is the entry point for configurable budgets, so its
        defaults must exceed the legacy 360 s ceiling without exceeding the
        canonical publish step's outer 1800 s ceiling."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            cfg, _ = _load_cfg(
                root,
                publication={"reviewer": {
                    "mode": "fleet",
                    "candidates": [{"agent": "grok", "model": "grok-4.6"}],
                }},
            )
            self.assertGreater(cfg.publication.maker_timeout_seconds, 360)
            self.assertGreater(cfg.publication.reviewer_timeout_seconds, 360)
            self.assertEqual(cfg.publication.maker_timeout_seconds,
                             DEFAULT_PUBLICATION_FLOOR_TIMEOUT_SECONDS)
            self.assertEqual(cfg.publication.reviewer_timeout_seconds,
                             DEFAULT_PUBLICATION_REVIEWER_TIMEOUT_SECONDS)
            # The configurable budget floor must still fit strictly below
            # the canonical publish step's outer 1800 s ceiling.
            self.assertLess(
                cfg.publication.maker_timeout_seconds
                + cfg.publication.reviewer_timeout_seconds,
                OUTER_PUBLICATION_BUDGET_SECONDS,
            )

    def test_explicit_timeouts_propagate_to_transport(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(
                root,
                publication={
                    "maker_timeout_seconds": 410,
                    "reviewer_timeout_seconds": 420,
                    "reviewer": {"mode": "muse"},
                },
            )
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")
            reviewer = _HashPassReviewer()
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid-budgets", maker=maker, reviewer=reviewer,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(maker.calls[0]["timeout"], 410)
            self.assertEqual(reviewer.calls[0]["timeout"], 420)


class FleetReviewerTests(unittest.TestCase):
    """``mode == 'fleet'`` walks the injected candidate list."""

    def _pass_envelope(self, provider, model, manifest_path):
        digest = sha(Path(manifest_path).read_bytes())
        final = json.dumps({"verdict": "PASS", "manifest_sha256": digest,
                            "findings": []})
        return Envelope(
            ok=True, status="ok", final=final, model=model,
            provider=provider, session_id="fleet-sess",
            raw={"ok": True, "status": "ok", "provider": provider,
                 "model": model, "sessionId": "fleet-sess", "final": final},
        )

    def test_first_unavailable_candidate_falls_through_to_next(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(
                root,
                publication={
                    "maker_timeout_seconds": 400,
                    "reviewer_timeout_seconds": 400,
                    "reviewer": {
                        "mode": "fleet",
                        "candidates": [
                            {"agent": "grok", "model": "grok-4.6"},
                            {"agent": "muse", "model": "muse-spark-1.3"},
                        ],
                    },
                },
            )
            spec = _candidate_spec()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")

            # First candidate: transport outage; second candidate: PASS.
            class _Outage:
                def __init__(self):
                    self.calls = []
                def invoke(self, **kwargs):
                    self.calls.append(kwargs["timeout"])
                    raise TransportError("fleet outage", reason_code="process_timeout")

            first = _Outage()
            # The second candidate uses the muse-shaped PASS reviewer so its
            # real envelope identity differs from the maker's. ``_HashPassReviewer``
            # records every invocation in ``calls``, letting the assertion below
            # prove the second candidate was actually invoked.
            second = _HashPassReviewer()

            fleet_reviewers = [
                {"transport": first, "spec": spec},
                {"transport": second, "spec": spec},
            ]
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid-fleet-fallthrough",
                maker=maker, fleet_reviewers=fleet_reviewers,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            # The first candidate was invoked exactly once and raised.
            self.assertEqual(first.calls, [400])
            # The second candidate won the review.
            self.assertEqual(result["reviewer_model"], "muse-spark-1.3")
            self.assertEqual(len(second.calls), 1)
            # The maker still ran with the configured budget.
            self.assertEqual(maker.calls[0]["timeout"], 400)

    def test_no_candidate_available_defers_without_publishing(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(
                root,
                publication={
                    "reviewer": {
                        "mode": "fleet",
                        "candidates": [
                            {"agent": "grok", "model": "grok-4.6"},
                            {"agent": "muse", "model": "muse-spark-1.3"},
                        ],
                    },
                },
            )
            spec = _candidate_spec()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")

            class _Outage:
                def __init__(self, label):
                    self.label = label
                    self.calls = []
                def invoke(self, **kwargs):
                    self.calls.append(self.label)
                    raise TransportError(self.label + " down",
                                         reason_code="native_timeout")

            first = _Outage("grok")
            second = _Outage("muse")
            fleet_reviewers = [
                {"transport": first, "spec": spec},
                {"transport": second, "spec": spec},
            ]
            (root / "run").mkdir()
            run_root = root / "run"
            with self.assertRaisesRegex(PublicationError,
                                        "independent_review_deferred"):
                produce_reviewed_candidate(
                    frozen=frozen, run_root=run_root,
                    observed="2026-09-18T00:00:00+00:00",
                    cfg=cfg, rid="rid-fleet-defer",
                    maker=maker, fleet_reviewers=fleet_reviewers,
                )
            # Both candidates were attempted before the defer.
            self.assertEqual(first.calls, ["grok"])
            self.assertEqual(second.calls, ["muse"])
            # No candidate wrote an independent review.json. The maker's
            # publication.json from the candidate synthesis is still there
            # but that is *not* a published wiki page — the deferred code
            # path returns before ``bind_review`` runs.
            reviews = list(run_root.rglob("review.json"))
            self.assertEqual(reviews, [])
            # Maker still ran once (the candidate synthesis succeeded).
            self.assertEqual(len(maker.calls), 1)

    def test_candidate_with_same_lineage_as_maker_is_skipped(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(
                root,
                publication={
                    "reviewer": {
                        "mode": "fleet",
                        "candidates": [
                            {"agent": "vllm", "model": "Qwen3.8-27B"},
                            {"agent": "muse", "model": "muse-spark-1.3"},
                        ],
                    },
                },
            )
            spec = _candidate_spec()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")

            # First candidate claims the maker's own identity; it must be
            # skipped and the second candidate (muse) must win.
            class _Collision:
                def __init__(self):
                    self.calls = []
                def invoke(self, *, spec, cwd, prompt, run_config_path,
                           state_dir, timeout):
                    self.calls.append(timeout)
                    pubs = sorted(Path(cwd).glob("**/publication.json"),
                                  key=lambda p: p.stat().st_mtime)
                    # Returns the maker's identity on purpose.
                    return self._owner_response(pubs[-1])

                @staticmethod
                def _owner_response(manifest_path):
                    digest = sha(Path(manifest_path).read_bytes())
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

            collision = _Collision()
            # Use the muse-shaped PASS reviewer so the candidate's real envelope
            # identity differs from the maker's. ``_HashPassReviewer`` records
            # every invocation in ``calls`` so the assertion below can prove the
            # second candidate was actually invoked.
            muse = _HashPassReviewer()

            fleet_reviewers = [
                {"transport": collision, "spec": spec},
                {"transport": muse, "spec": spec},
            ]
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid-fleet-self",
                maker=maker, fleet_reviewers=fleet_reviewers,
            )
            # Collision candidate was attempted and rejected on lineage.
            self.assertEqual(len(collision.calls), 1)
            # Second candidate won; review was not silently self-approved.
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["reviewer_model"], "muse-spark-1.3")
            self.assertEqual(len(muse.calls), 1)

    def test_when_every_candidate_matches_maker_publication_is_deferred(self):
        """All candidates claim the maker's identity → no review possible."""
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = _vault(root)
            cfg, _ = _load_cfg(
                root,
                publication={
                    "reviewer": {
                        "mode": "fleet",
                        # The validator rejects duplicate (agent, model) pairs,
                        # so we declare two unique candidates here. Both
                        # ``_Collision`` transports below still return envelopes
                        # that claim the maker's own identity, which is what
                        # actually exercises the ``maker cannot review itself``
                        # rule at the transport layer.
                        "candidates": [
                            {"agent": "vllm", "model": "Qwen3.8-27B"},
                            {"agent": "vllm-mirror", "model": "Qwen3.8-27B"},
                        ],
                    },
                },
            )
            spec = _candidate_spec()
            maker = _SequenceMaker([json.dumps(_valid_decision())],
                                   "vllm", "Qwen3.8-27B")

            def make_self_review():
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
                            session_id="self",
                            raw={"ok": True, "status": "ok",
                                 "provider": "vllm", "model": "Qwen3.8-27B",
                                 "sessionId": "self", "final": final},
                        )
                return _Collision()

            collisions = [make_self_review(), make_self_review()]
            fleet_reviewers = [{"transport": c, "spec": spec} for c in collisions]
            (root / "run").mkdir()
            run_root = root / "run"
            with self.assertRaisesRegex(PublicationError,
                                        "independent_review_deferred"):
                produce_reviewed_candidate(
                    frozen=frozen, run_root=run_root,
                    observed="2026-09-18T00:00:00+00:00",
                    cfg=cfg, rid="rid-fleet-allself",
                    maker=maker, fleet_reviewers=fleet_reviewers,
                )
            for c in collisions:
                self.assertEqual(c.calls, 1)
            # No candidate could serve as a real reviewer; no review.json
            # was written, so the wiki page must NOT be published.
            self.assertEqual(list(run_root.rglob("review.json")), [])


if __name__ == "__main__":
    unittest.main()
