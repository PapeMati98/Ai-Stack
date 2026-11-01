"""Publication budgets from control.json must reach curate_once.

base-config.json is not the source of truth for the publication section.
_project_runtime_cfg copies that section from control into the project
runtime config so load_config / curate_once see the owner budgets.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.config import (
    DEFAULT_PUBLICATION_TIMEOUT_SECONDS,
    load_config,
)
from caretaker.publication import freeze, sha
from caretaker.transports import Envelope
from curate_once import _build_fleet_reviewers, produce_reviewed_candidate
from maintenance import (
    _project_runtime_cfg,
    _runtime_base_with_control_publication,
)
from ._helpers import write_config


TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"
CONTROL_PUBLICATION = {
    "enabled": True,
    "maker_timeout_seconds": 900,
    "reviewer_timeout_seconds": 600,
    "reviewer": {
        "mode": "fleet",
        "candidates": [
            {"agent": "grok", "model": "grok-4.6"},
            {"agent": "codex", "model": "gpt-5.4"},
        ],
    },
}


def _mkdir_tmp():
    TASK_TMP.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(TASK_TMP))


def _base_without_publication(tmp: Path) -> dict:
    base_dir = tmp / "base"
    base_dir.mkdir(parents=True, exist_ok=True)
    path = write_config(base_dir)
    raw = json.loads(path.read_text("utf-8"))
    raw.pop("publication", None)
    path.write_text(json.dumps(raw), "utf-8")
    return raw


def _vault(root: Path):
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


class _TimeoutMaker:
    def __init__(self, finals, provider="vllm", model="Qwen3.8-27B"):
        self.finals = list(finals)
        self.calls = []
        self.provider = provider
        self.model = model

    def invoke(self, *, spec, cwd, prompt, run_config_path, state_dir, timeout):
        self.calls.append({"timeout": timeout})
        final = self.finals.pop(0)
        return Envelope(
            ok=True, status="ok", final=final, model=self.model,
            provider=self.provider, session_id="maker-sess",
            raw={"ok": True, "status": "ok", "provider": self.provider,
                 "model": self.model, "sessionId": "maker-sess", "final": final},
        )


class _HashPassReviewer:
    def __init__(self):
        self.calls = []
        self.provider = "muse-code"
        self.model = "muse-spark-1.3"

    def invoke(self, *, spec, cwd, prompt, run_config_path, state_dir, timeout):
        self.calls.append({"timeout": timeout})
        pubs = sorted(Path(cwd).glob("**/publication.json"), key=lambda p: p.stat().st_mtime)
        digest = sha(pubs[-1].read_bytes())
        final = json.dumps({"verdict": "PASS", "manifest_sha256": digest,
                            "findings": []})
        return Envelope(
            ok=True, status="ok", final=final, model=self.model,
            provider=self.provider, session_id="muse-sess",
            raw={"ok": True, "status": "ok", "provider": self.provider,
                 "model": self.model, "sessionId": "muse-sess", "final": final},
        )


class BudgetPlumbingTests(unittest.TestCase):

    def test_control_publication_reaches_project_runtime_and_curate_once(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            base = _base_without_publication(root)
            self.assertNotIn("publication", base)
            control = {"publication": CONTROL_PUBLICATION}
            prepared = _runtime_base_with_control_publication(base, control)
            config_path, runtime = _project_runtime_cfg(
                prepared, "test", root / "state")
            self.assertNotIn("publication", base)
            self.assertEqual(runtime["publication"]["maker_timeout_seconds"], 900)
            self.assertEqual(runtime["publication"]["reviewer"]["mode"], "fleet")
            self.assertEqual(
                runtime["publication"]["reviewer"]["candidates"][0]["agent"],
                "grok",
            )
            written = json.loads(Path(config_path).read_text("utf-8"))
            self.assertEqual(written["publication"]["maker_timeout_seconds"], 900)
            cfg = load_config(config_path)
            self.assertEqual(cfg.publication.maker_timeout_seconds, 900)
            self.assertEqual(cfg.publication.reviewer_timeout_seconds, 600)
            self.assertNotEqual(cfg.publication.maker_timeout_seconds, 360)
            self.assertEqual(cfg.publication.reviewer.mode, "fleet")
            self.assertEqual(
                [(c.agent, c.model) for c in cfg.publication.reviewer.candidates],
                [("grok", "grok-4.6"), ("codex", "gpt-5.4")],
            )
            reviewers = _build_fleet_reviewers(cfg)
            self.assertEqual(len(reviewers), 2)
            self.assertEqual(reviewers[0]["lineage"], "grok")
            vault, frozen = _vault(root)
            maker = _TimeoutMaker([json.dumps(_valid_decision())])
            reviewer = _HashPassReviewer()
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-20T00:00:00+00:00",
                cfg=cfg, rid="rid-budget", maker=maker, reviewer=reviewer,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(maker.calls[0]["timeout"], 900)
            self.assertNotEqual(maker.calls[0]["timeout"], 360)

    def test_missing_control_publication_keeps_legacy_360_and_muse(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            base = _base_without_publication(root)
            self.assertNotIn("publication", base)
            prepared = _runtime_base_with_control_publication(
                base, {"known_projects": ["test"]})
            config_path, runtime = _project_runtime_cfg(
                prepared, "test", root / "state")
            self.assertNotIn("publication", runtime)
            written = json.loads(Path(config_path).read_text("utf-8"))
            self.assertNotIn("publication", written)
            cfg = load_config(config_path)
            self.assertEqual(cfg.publication.maker_timeout_seconds, 360)
            self.assertEqual(
                cfg.publication.maker_timeout_seconds,
                DEFAULT_PUBLICATION_TIMEOUT_SECONDS,
            )
            self.assertEqual(cfg.publication.reviewer.mode, "muse")
            self.assertEqual(cfg.publication.reviewer.candidates, [])
            self.assertIsNone(_build_fleet_reviewers(cfg))
            vault, frozen = _vault(root)
            maker = _TimeoutMaker([json.dumps(_valid_decision())])
            reviewer = _HashPassReviewer()
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run",
                observed="2026-09-20T00:00:00+00:00",
                cfg=cfg, rid="rid-legacy", maker=maker, reviewer=reviewer,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(maker.calls[0]["timeout"], 360)
            self.assertEqual(reviewer.calls[0]["timeout"], 360)

    def test_base_publication_is_not_used_when_control_omits_section(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            base = _base_without_publication(root)
            base["publication"] = {
                "maker_timeout_seconds": 900,
                "reviewer_timeout_seconds": 600,
            }
            prepared = _runtime_base_with_control_publication(base, {})
            config_path, runtime = _project_runtime_cfg(
                prepared, "test", root / "state")
            self.assertNotIn("publication", runtime)
            cfg = load_config(config_path)
            self.assertEqual(cfg.publication.maker_timeout_seconds, 360)
            self.assertEqual(cfg.publication.reviewer.mode, "muse")


if __name__ == "__main__":
    unittest.main()
