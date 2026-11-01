"""Curator editorial amendments: same gates as maker, append-only, provenance."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.config import load_config
from caretaker.publication import (
    WEIGHT_ADVISORY,
    WEIGHT_BLOCKING,
    existing_page_body,
    freeze,
    sha,
)
from caretaker.transports import Envelope
from curate_once import produce_reviewed_candidate
from ._helpers import write_config


TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"


def _mkdir_tmp():
    TASK_TMP.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(TASK_TMP))


def _vault(root: Path, target_rel: str = "wiki/projects/test.md"):
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
        f"target_pages:\n  - {target_rel}\n---\nReport.\n",
        encoding="utf-8",
    )
    target = vault / target_rel
    target.parent.mkdir(parents=True)
    prior = (
        '---\ntitle: Test\nstatus: reviewed\nlast_verified: "2026-01-01"\n'
        "sources: []\nproject_slug: test\n---\n\n# Test\nOld unresolved question.\n"
    )
    target.write_text(prior, encoding="utf-8")
    return vault, freeze(vault, ["proposals/agent/one.md"], target_rel)


def _valid_decision(pid="prop-one"):
    return {
        "body": "# Test\nOld unresolved question.\nDated tool canary report (src-one); deployment unverified.",
        "summary": "Tool canary report.",
        "dispositions": {pid: {"disposition": "synthesized",
                                "reason": "Dated and scope-limited report integrated."}},
    }


class SequenceTransport:
    def __init__(self, finals, provider, model, session_id=None):
        self.finals = list(finals)
        self.calls = []
        self.provider = provider
        self.model = model
        self.session_id = session_id

    def invoke(self, *, spec, cwd, prompt, run_config_path, state_dir, timeout):
        self.calls.append(prompt)
        if not self.finals:
            raise AssertionError("unexpected extra model call")
        final = self.finals.pop(0)
        return Envelope(
            ok=True, status="ok", final=final, model=self.model,
            provider=self.provider, session_id=self.session_id,
            raw={
                "ok": True, "status": "ok", "provider": self.provider,
                "model": self.model, "sessionId": self.session_id, "final": final,
            },
        )


class ScriptedReviewer:
    def __init__(self, scripts, model="muse-spark-1.3", provider="muse-code"):
        self.scripts = list(scripts)
        self.calls = []
        self.model = model
        self.provider = provider

    def invoke(self, *, spec, cwd, prompt, run_config_path, state_dir, timeout):
        self.calls.append(prompt)
        pubs = sorted(Path(cwd).glob("**/publication.json"), key=lambda p: p.stat().st_mtime)
        digest = sha(pubs[-1].read_bytes())
        script = self.scripts.pop(0)
        payload = {
            "verdict": script["verdict"],
            "manifest_sha256": digest,
            "findings": list(script.get("findings", [])),
        }
        final = json.dumps(payload)
        return Envelope(
            ok=True, status="ok", final=final, model=self.model,
            provider=self.provider, session_id="sess-1",
            raw={
                "ok": True, "status": "ok", "provider": self.provider,
                "model": self.model, "sessionId": "sess-1", "final": final,
            },
        )


def _payload_page(run_root: Path, frozen: dict) -> str:
    cands = sorted(run_root.glob("**/payload/" + frozen["target"]), key=lambda p: p.stat().st_mtime)
    return cands[-1].read_text(encoding="utf-8")


def _ledger(run_root: Path) -> dict:
    ledgers = sorted(run_root.glob("**/ledger.json"), key=lambda p: p.stat().st_mtime)
    return json.loads(ledgers[-1].read_text(encoding="utf-8"))


class CuratorAmendmentTests(unittest.TestCase):
    def _run(self, root, frozen, reviewer, maker_count=2):
        cfg_dir = root / "cfg"
        cfg_dir.mkdir()
        cfg = load_config(write_config(cfg_dir))
        maker = SequenceTransport(
            [json.dumps(_valid_decision())] * maker_count,
            "vllm", "Qwen3.8-27B",
        )
        run_root = root / "run"
        run_root.mkdir()
        result = produce_reviewed_candidate(
            frozen=frozen, run_root=run_root, observed="2026-09-20T00:00:00+00:00",
            cfg=cfg, rid="rid-amend", maker=maker, reviewer=reviewer,
        )
        return result, run_root

    def test_advisory_with_fix_keeps_reviewed_manifest(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            charge = {
                "text": "Local canary detail present in source was not copied",
                "weight": WEIGHT_ADVISORY,
                "class": "incomplete_coverage",
                "fix": {
                    "text": "Local tool canary passed; deployment unverified (src-one).",
                    "source_id": "src-one",
                },
            }
            reviewer = ScriptedReviewer([
                {"verdict": "REPAIR", "findings": [charge]},
            ])
            result, run_root = self._run(root, frozen, reviewer, maker_count=1)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            page = _payload_page(run_root, frozen)
            self.assertNotIn("Local tool canary passed; deployment unverified (src-one).", page)
            self.assertIn("# Test\nOld unresolved question.\n", page)
            self.assertEqual(result["advisory_count"], 1)
            self.assertEqual(result["advisory_classes"], ["incomplete_coverage"])
            self.assertEqual(result["manifest_sha256"], sha(Path(result["manifest"]).read_bytes()))

    def test_foreign_source_fix_is_ignored(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            charge = {
                "text": "Need a fact from another packet",
                "weight": WEIGHT_ADVISORY,
                "class": "incomplete_coverage",
                "fix": {
                    "text": "Claim from another file (src-foreign).",
                    "source_id": "src-foreign",
                },
            }
            reviewer = ScriptedReviewer([
                {"verdict": "REPAIR", "findings": [charge]},
            ])
            result, run_root = self._run(root, frozen, reviewer, maker_count=1)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            page = _payload_page(run_root, frozen)
            self.assertNotIn("src-foreign", page)

    def test_secret_or_frontmatter_fix_is_ignored(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            secret_charge = {
                "text": "coverage gap",
                "weight": WEIGHT_ADVISORY,
                "class": "incomplete_coverage",
                "fix": {
                    "text": "Keep canary (src-one) sk-" + ("A" * 24),
                    "source_id": "src-one",
                },
            }
            reviewer = ScriptedReviewer([
                {"verdict": "REPAIR", "findings": [secret_charge]},
            ])
            result, _run_root = self._run(root, frozen, reviewer, maker_count=1)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")

        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            fm_charge = {
                "text": "coverage gap",
                "weight": WEIGHT_ADVISORY,
                "class": "incomplete_coverage",
                "fix": {
                    "text": "---\ntitle: injected\n---\nCanary passed (src-one).",
                    "source_id": "src-one",
                },
            }
            reviewer = ScriptedReviewer([
                {"verdict": "REPAIR", "findings": [fm_charge]},
            ])
            result, _run_root = self._run(root, frozen, reviewer, maker_count=1)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")

    def test_frozen_prior_bytes_unchanged(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            prior = existing_page_body(frozen["before"])
            charge = {
                "text": "gap",
                "weight": WEIGHT_ADVISORY,
                "class": "incomplete_coverage",
                "fix": {
                    "text": "Local tool canary passed; deployment unverified (src-one).",
                    "source_id": "src-one",
                },
            }
            reviewer = ScriptedReviewer([
                {"verdict": "REPAIR", "findings": [charge]},
            ])
            result, run_root = self._run(root, frozen, reviewer, maker_count=1)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            page = _payload_page(run_root, frozen)
            self.assertIn("# Test\nOld unresolved question.\n", page)
            self.assertEqual(page.count("# Test\nOld unresolved question."), 1)
            self.assertTrue(prior.strip() in page)

    def test_ledger_has_no_unreviewed_curator_amendment(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            charge = {
                "text": "gap",
                "weight": WEIGHT_ADVISORY,
                "class": "incomplete_coverage",
                "fix": {
                    "text": "Local tool canary passed; deployment unverified (src-one).",
                    "source_id": "src-one",
                },
            }
            reviewer = ScriptedReviewer([
                {"verdict": "REPAIR", "findings": [charge]},
            ])
            result, run_root = self._run(root, frozen, reviewer, maker_count=1)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            ledger = _ledger(run_root)
            self.assertNotIn("curator_amendment", ledger)
            self.assertEqual(result["advisory_classes"], ["incomplete_coverage"])

    def test_unfixed_advisory_is_only_in_diagnostics(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            charge = {
                "text": "Source mentions local canary but page omits it",
                "weight": WEIGHT_ADVISORY,
                "class": "incomplete_coverage",
            }
            reviewer = ScriptedReviewer([
                {"verdict": "REPAIR", "findings": [charge]},
            ])
            result, run_root = self._run(root, frozen, reviewer, maker_count=1)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            page = _payload_page(run_root, frozen)
            self.assertNotIn("Known incomplete coverage", page)
            self.assertNotIn("Source mentions local canary but page omits it", page)
            self.assertEqual(result["advisory_count"], 1)

    def test_blocking_finding_still_stops_despite_fix(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            charge = {
                "text": "Claim is not anchored in supplied sources",
                "weight": WEIGHT_BLOCKING,
                "class": "unanchored_content",
                "fix": {
                    "text": "Local tool canary passed; deployment unverified (src-one).",
                    "source_id": "src-one",
                },
            }
            reviewer = ScriptedReviewer([
                {"verdict": "REPAIR", "findings": [charge]},
                {"verdict": "REPAIR", "findings": [charge]},
            ])
            result, run_root = self._run(root, frozen, reviewer)
            self.assertEqual(result["status"], "PENDING")
            self.assertEqual(result.get("error_code"), "independent_review_not_accepted")
            page = _payload_page(run_root, frozen)
            self.assertNotIn("Local tool canary passed; deployment unverified (src-one).", page)


if __name__ == "__main__":
    unittest.main()
