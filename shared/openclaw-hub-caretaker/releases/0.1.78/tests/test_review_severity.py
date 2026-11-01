"""Reviewer charges carry weight: blocking integrity vs advisory coverage."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.config import load_config
from caretaker.publication import (
    BLOCKING_FINDING_CLASSES,
    WEIGHT_ADVISORY,
    WEIGHT_BLOCKING,
    bind_review,
    freeze,
    normalize_review_finding,
    review_allows_publication,
    review_has_blocking_findings,
    sha,
)
from caretaker.transports import Envelope
from curate_once import (
    _extract_verdict_block,
    _finding_detail_records,
    produce_reviewed_candidate,
)
from ._helpers import write_config


TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"


def _mkdir_tmp():
    TASK_TMP.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(TASK_TMP))


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
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = []

    def invoke(self, *, spec, cwd, prompt, run_config_path, state_dir, timeout):
        self.calls.append(prompt)
        pubs = sorted(Path(cwd).glob("**/publication.json"), key=lambda p: p.stat().st_mtime)
        digest = sha(pubs[-1].read_bytes())
        script = self.scripts.pop(0)
        findings = list(script.get("findings", []))
        payload = {
            "verdict": script["verdict"],
            "manifest_sha256": digest,
            "findings": findings,
        }
        final = json.dumps(payload)
        return Envelope(
            ok=True, status="ok", final=final, model="muse-spark-1.3",
            provider="muse-code", session_id="sess-1",
            raw={
                "ok": True, "status": "ok", "provider": "muse-code",
                "model": "muse-spark-1.3", "sessionId": "sess-1", "final": final,
            },
        )


class ReviewSeverityUnitTests(unittest.TestCase):
    def test_every_blocking_class_is_recognized(self):
        self.assertGreaterEqual(len(BLOCKING_FINDING_CLASSES), 8)
        for cls in BLOCKING_FINDING_CLASSES:
            with self.subTest(cls=cls):
                finding = normalize_review_finding({
                    "text": "integrity defect",
                    "class": cls,
                    "weight": WEIGHT_ADVISORY,
                })
                self.assertEqual(finding["weight"], WEIGHT_BLOCKING)
                self.assertEqual(finding["class"], cls)
                self.assertTrue(review_has_blocking_findings([{
                    "text": "integrity defect",
                    "class": cls,
                }]))

    def test_missing_weight_is_blocking(self):
        self.assertEqual(
            normalize_review_finding("bare string charge")["weight"],
            WEIGHT_BLOCKING,
        )
        self.assertEqual(
            normalize_review_finding({"text": "no weight field"})["weight"],
            WEIGHT_BLOCKING,
        )
        self.assertTrue(review_has_blocking_findings(["legacy string"]))
        self.assertFalse(review_allows_publication({
            "verdict": "PASS",
            "findings": [{"text": "no weight"}],
        }))

    def test_advisory_finding_does_not_block(self):
        finding = {
            "text": "source fact not copied",
            "weight": WEIGHT_ADVISORY,
            "class": "incomplete_coverage",
            "fix": {"text": "Canary remains unverified (src-one).", "source_id": "src-one"},
        }
        self.assertEqual(normalize_review_finding(finding)["weight"], WEIGHT_ADVISORY)
        self.assertFalse(review_has_blocking_findings([finding]))
        self.assertTrue(review_allows_publication({
            "verdict": "REPAIR",
            "findings": [finding],
        }))

    def test_fix_is_read_with_source_id(self):
        finding = normalize_review_finding({
            "text": "coverage gap",
            "weight": WEIGHT_ADVISORY,
            "fix": {
                "text": "Dated canary passed locally (src-one).",
                "source_id": "src-one",
            },
        })
        self.assertEqual(finding["fix"]["text"], "Dated canary passed locally (src-one).")
        self.assertEqual(finding["fix"]["source_id"], "src-one")
        rows = _finding_detail_records([{
            "text": "coverage gap",
            "weight": WEIGHT_ADVISORY,
            "fix": {
                "text": "Dated canary passed locally (src-one).",
                "source_id": "src-one",
            },
        }])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["fix_supplied"])
        self.assertEqual(rows[0]["source_id"], "src-one")
        self.assertEqual(rows[0]["weight"], WEIGHT_ADVISORY)

    def test_finding_class_is_secret_filtered_in_diagnostics(self):
        secret = "sk-" + ("A" * 24)
        rows = _finding_detail_records([{
            "text": "integrity defect",
            "weight": WEIGHT_BLOCKING,
            "class": secret,
        }])
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["class"], secret)
        self.assertNotIn(secret, json.dumps(rows))

    def test_bare_pass_in_prose_is_rejected(self):
        self.assertIsNone(_extract_verdict_block(
            "The reviewer said PASS without a structured payload."))

    def test_manifest_sha256_still_required_for_bind(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "publication.json").write_text("{}", encoding="utf-8")
            digest = sha(b"{}")
            good = {
                "ok": True, "status": "ok", "provider": "muse-code",
                "model": "muse-spark-1.3", "sessionId": "native-session",
                "final": json.dumps({
                    "verdict": "PASS", "findings": [],
                    "manifest_sha256": digest,
                }),
            }
            self.assertTrue(bind_review(root, good).exists())
            bad = dict(good)
            bad["final"] = json.dumps({
                "verdict": "PASS", "findings": [],
                "manifest_sha256": "0" * 64,
            })
            with self.assertRaises(Exception):
                bind_review(root, bad)


class ReviewSeverityPipelineTests(unittest.TestCase):
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
            cfg=cfg, rid="rid-severity", maker=maker, reviewer=reviewer,
        )
        diag = json.loads((run_root / "diagnostics.json").read_text(encoding="utf-8"))
        return result, diag

    def test_blocking_finding_stops_publication(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            charge = {
                "text": "Claim is not anchored in supplied sources",
                "weight": WEIGHT_BLOCKING,
                "class": "unanchored_content",
            }
            reviewer = ScriptedReviewer([
                {"verdict": "REPAIR", "findings": [charge]},
                {"verdict": "REPAIR", "findings": [charge]},
            ])
            result, diag = self._run(root, frozen, reviewer)
            self.assertEqual(result["status"], "PENDING")
            self.assertEqual(result.get("error_code"), "independent_review_not_accepted")
            details = diag["review_rounds"][0]["finding_details"]
            self.assertEqual(details[0]["weight"], WEIGHT_BLOCKING)
            self.assertFalse(details[0]["fix_supplied"])

    def test_advisory_finding_does_not_stop_publication(self):
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
            result, diag = self._run(root, frozen, reviewer, maker_count=1)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(diag["review_rounds"][0]["advisory_count"], 1)
            self.assertEqual(diag["review_rounds"][0]["advisory_classes"], ["incomplete_coverage"])
            self.assertNotIn("finding_details", diag["review_rounds"][0])

    def test_unweighted_finding_stops_publication(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            reviewer = ScriptedReviewer([
                {"verdict": "REPAIR", "findings": ["legacy unweighted charge"]},
                {"verdict": "REPAIR", "findings": ["still unweighted"]},
            ])
            result, _diag = self._run(root, frozen, reviewer)
            self.assertEqual(result["status"], "PENDING")
            self.assertEqual(result.get("error_code"), "independent_review_not_accepted")


if __name__ == "__main__":
    unittest.main()
