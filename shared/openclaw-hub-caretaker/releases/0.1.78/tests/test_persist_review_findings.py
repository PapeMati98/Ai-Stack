"""Persist sanitized reviewer charges in diagnostics.json, never in logs."""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from caretaker.config import load_config
from caretaker.publication import freeze, json_bytes, safe_diagnostics, sha
from caretaker.transports import Envelope
from curate_once import produce_reviewed_candidate
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
        final = json.dumps({
            "verdict": script["verdict"],
            "manifest_sha256": digest,
            "findings": findings,
        })
        return Envelope(
            ok=True, status="ok", final=final, model="muse-spark-1.3",
            provider="muse-code", session_id="sess-1",
            raw={
                "ok": True, "status": "ok", "provider": "muse-code",
                "model": "muse-spark-1.3", "sessionId": "sess-1", "final": final,
            },
        )


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class PersistReviewFindingsTests(unittest.TestCase):
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
        handler = LogCapture()
        root_log = logging.getLogger()
        root_log.addHandler(handler)
        try:
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=run_root, observed="2026-09-20T00:00:00+00:00",
                cfg=cfg, rid="rid-findings", maker=maker, reviewer=reviewer,
            )
        finally:
            root_log.removeHandler(handler)
        diag_path = run_root / "diagnostics.json"
        # Production ``curate_once.run`` rewrites diagnostics.json from the
        # returned result for every non-REVIEWED_CANDIDATE outcome.
        if result.get("status") != "REVIEWED_CANDIDATE":
            diag_path.write_bytes(json_bytes(safe_diagnostics(result)))
        self.assertTrue(diag_path.is_file())
        diag = json.loads(diag_path.read_text(encoding="utf-8"))
        return result, diag, "\n".join(handler.lines)

    def test_rejected_review_writes_concrete_findings(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            charge = "Missing citation for deployment claim"
            reviewer = ScriptedReviewer([
                {"verdict": "REPAIR", "findings": [charge]},
                {"verdict": "REPAIR", "findings": ["Still unsourced after repair"]},
            ])
            result, diag, logs = self._run(root, frozen, reviewer)
            self.assertEqual(result["status"], "PENDING")
            self.assertEqual(result.get("error_code"), "independent_review_not_accepted")
            self.assertIn("review_rounds", diag)
            blob = json.dumps(diag)
            self.assertIn(charge, blob)
            self.assertNotEqual(diag, {"error_code": "independent_review_not_accepted"})
            self.assertNotIn(charge, logs)

    def test_rounds_are_numbered(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            first = "Round-one charge: preserve contradiction"
            second = "Round-two charge: dispositions incomplete"
            reviewer = ScriptedReviewer([
                {"verdict": "REPAIR", "findings": [first]},
                {"verdict": "REPAIR", "findings": [second]},
            ])
            result, diag, logs = self._run(root, frozen, reviewer)
            rounds = diag["review_rounds"]
            self.assertEqual(len(rounds), 2)
            self.assertEqual(rounds[0]["round"], 1)
            self.assertEqual(rounds[0]["verdict"], "REPAIR")
            self.assertIn(first, rounds[0]["findings"])
            self.assertEqual(rounds[1]["round"], 2)
            self.assertEqual(rounds[1]["verdict"], "REPAIR")
            self.assertIn(second, rounds[1]["findings"])
            self.assertNotIn(first, logs)
            self.assertNotIn(second, logs)

    def test_secret_finding_is_not_written(self):
        secret = "sk-" + ("A" * 24)
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            safe = "Citation missing on canary claim"
            reviewer = ScriptedReviewer([
                {"verdict": "REPAIR", "findings": [safe, "password: hunter2-extra", secret]},
                {"verdict": "REPAIR" + " " + secret, "findings": [secret]},
            ])
            result, diag, logs = self._run(root, frozen, reviewer)
            blob = json.dumps(diag)
            self.assertIn(safe, blob)
            self.assertNotIn(secret, blob)
            self.assertNotIn("hunter2", blob)
            self.assertNotIn(secret, logs)
            self.assertNotEqual(diag["review_rounds"][1]["verdict"], "REPAIR " + secret)

    def test_pass_records_no_findings_explicitly(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = _vault(root)
            reviewer = ScriptedReviewer([{"verdict": "PASS", "findings": []}])
            result, diag, logs = self._run(root, frozen, reviewer, maker_count=1)
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertIn("review_rounds", diag)
            self.assertEqual(len(diag["review_rounds"]), 1)
            round1 = diag["review_rounds"][0]
            self.assertEqual(round1["round"], 1)
            self.assertEqual(round1["verdict"], "PASS")
            self.assertEqual(round1["findings"], [])
            self.assertTrue(round1["no_findings"])
            self.assertEqual(logs.find("findings"), -1)

    def test_safe_diagnostics_drops_unknown_keys(self):
        filtered = safe_diagnostics({
            "error_code": "independent_review_not_accepted",
            "review_rounds": [{"round": 1, "verdict": "REPAIR", "findings": ["x"], "no_findings": False}],
            "prompt": "must-not-pass",
            "candidate_body": "must-not-pass",
            "transcript": "must-not-pass",
            "raw_envelope": {"final": "must-not-pass"},
        })
        self.assertEqual(filtered["error_code"], "independent_review_not_accepted")
        self.assertIn("review_rounds", filtered)
        self.assertNotIn("prompt", filtered)
        self.assertNotIn("candidate_body", filtered)
        self.assertNotIn("transcript", filtered)
        self.assertNotIn("raw_envelope", filtered)


if __name__ == "__main__":
    unittest.main()
