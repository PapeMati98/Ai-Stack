"""0.1.33: advisory reviews bind the exact manifest and keep retry evidence."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from caretaker.publication import PublicationError, WEIGHT_ADVISORY, sha
from caretaker.pubstate import code_version, due_to_run, fingerprint
from caretaker.transports import Envelope
from curate_once import produce_reviewed_candidate
from .test_curator_amendment import _mkdir_tmp, _vault, _valid_decision, SequenceTransport
from .test_fleet_verdict_extraction import _candidate_spec, _load_cfg


class Reviewer:
    def __init__(self, findings):
        self.findings = findings
        self.digests = []

    def invoke(self, *, cwd, **_kwargs):
        manifest = next(Path(cwd).rglob("publication.json"))
        digest = sha(manifest.read_bytes())
        self.digests.append(digest)
        final = json.dumps({"verdict": "PASS", "manifest_sha256": digest,
                            "findings": self.findings})
        return Envelope(ok=True, status="ok", final=final, model="grok-4.6",
                        provider="grok", session_id="fleet-1",
                        raw={"ok": True, "status": "ok", "final": final,
                             "provider": "grok", "model": "grok-4.6",
                             "sessionId": "fleet-1"})


class Caretaker0133Tests(unittest.TestCase):
    def _run_fleet(self, root, frozen, findings):
        cfg, _ = _load_cfg(root, publication={"reviewer": {"mode": "fleet",
                            "candidates": [{"agent": "grok", "model": "grok-4.6"}]}})
        maker = SequenceTransport([json.dumps(_valid_decision())], "vllm", "Qwen3.8-27B")
        reviewer = Reviewer(findings)
        run = root / "run"
        run.mkdir()
        result = produce_reviewed_candidate(
            frozen=frozen, run_root=run, observed="2026-09-27T00:00:00+00:00",
            cfg=cfg, rid="rid-0133", maker=maker,
            fleet_reviewers=[{"transport": reviewer, "spec": _candidate_spec()}])
        return result, run, reviewer

    def test_pass_advisory_on_existing_continuation_preserves_manifest(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root, "wiki/projects/test-cont-4.md")
            finding = {"text": "Omitted canary detail", "weight": WEIGHT_ADVISORY,
                       "class": "incomplete_coverage", "fix": {
                           "text": "Local tool canary passed (src-one).", "source_id": "src-one"}}
            result, run, reviewer = self._run_fleet(root, frozen, [finding])
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["manifest_sha256"], reviewer.digests[0])
            self.assertEqual(sha(Path(result["manifest"]).read_bytes()), reviewer.digests[0])
            self.assertEqual(json.loads(Path(result["review"]).read_text())["manifest_sha256"], reviewer.digests[0])
            self.assertEqual(result["advisory_count"], 1)
            self.assertEqual(result["advisory_classes"], ["incomplete_coverage"])
            page = (Path(result["candidate_path"]) / "payload" / frozen["target"]).read_text()
            self.assertNotIn("Local tool canary passed (src-one).", page)
            self.assertNotIn("curator_amendment", json.loads((Path(result["candidate_path"]) / "ledger.json").read_text()))
            diagnostics = json.loads((run / "diagnostics.json").read_text())
            self.assertEqual(diagnostics["advisory_count"], 1)
            self.assertNotIn("Omitted canary detail", json.dumps(diagnostics))

    def test_foreign_source_fix_is_not_applied(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            finding = {"text": "Foreign source suggestion", "weight": WEIGHT_ADVISORY,
                       "class": "incomplete_coverage", "fix": {
                           "text": "Unsupported claim (src-foreign).", "source_id": "src-foreign"}}
            result, _, reviewer = self._run_fleet(root, frozen, [finding])
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(result["manifest_sha256"], reviewer.digests[0])
            self.assertNotIn("src-foreign", Path(result["manifest"]).read_text())

    def test_post_review_failure_has_own_code_and_exception_class(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            cfg, _ = _load_cfg(root)
            maker = SequenceTransport([json.dumps(_valid_decision())], "vllm", "Qwen3.8-27B")
            class PassReviewer:
                def invoke(self, *, cwd, **_kwargs):
                    digest = sha(next(Path(cwd).rglob("publication.json")).read_bytes())
                    final = json.dumps({"verdict": "PASS", "manifest_sha256": digest, "findings": []})
                    return Envelope(ok=True, status="ok", final=final, model="muse-spark-1.3",
                                    provider="muse-code", session_id="sess-1",
                                    raw={"ok": True, "status": "ok", "final": final,
                                         "provider": "muse-code", "model": "muse-spark-1.3",
                                         "sessionId": "sess-1"})
            run = root / "run"
            run.mkdir()
            with patch("curate_once.bind_review", side_effect=PublicationError("post_review_gate_failed")):
                result = produce_reviewed_candidate(
                    frozen=frozen, run_root=run, observed="2026-09-27T00:00:00+00:00",
                    cfg=cfg, rid="rid-post", maker=maker, reviewer=PassReviewer())
            self.assertEqual(result["status"], "PENDING")
            self.assertEqual(result["error_code"], "post_review_step_failed:post_review_gate_failed")
            diagnostics = json.loads((run / "diagnostics.json").read_text())
            self.assertEqual(diagnostics["post_review_exception"], "PublicationError")
            self.assertEqual(diagnostics["post_review_error_code"], "post_review_gate_failed")

    def test_new_code_version_retries_quarantined_batch_once(self):
        previous = "9207fe2110c06a622e6717c621a23cac"
        current = code_version()
        self.assertNotEqual(current, previous)
        old_fp = fingerprint("test", ["proposal.md"], ["abc"], {}, previous)
        new_fp = fingerprint("test", ["proposal.md"], ["abc"], {}, current)
        self.assertNotEqual(new_fp, old_fp)
        row = {"status": "QUARANTINED", "fingerprint": old_fp}
        self.assertEqual(due_to_run(row, "2026-09-27T00:00:00+00:00", new_fp),
                         (True, "fingerprint_changed"))
        row["fingerprint"] = new_fp
        self.assertEqual(due_to_run(row, "2026-09-27T00:00:00+00:00", new_fp),
                         (False, "quarantined_same_fingerprint"))


if __name__ == "__main__":
    unittest.main()
