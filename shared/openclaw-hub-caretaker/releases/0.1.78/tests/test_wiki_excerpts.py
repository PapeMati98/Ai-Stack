"""Tests for the bounded read-only wiki excerpt retrieval.

The gateway must:
- refuse paths that escape the vault (no symlink escape, no ``..``),
- filter pages by frontmatter ``status`` (reviewed/verified) and
  ``classification`` (internal/public),
- redact bodies that carry secrets / reasoning / raw tool dumps,
- cap excerpts at the caller-supplied byte budget.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from caretaker.hub import CanonicalKnowledgeGateway, WikiExcerpt


TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"


class CanonicalGatewayWikiExcerptTests(unittest.TestCase):

    def _vault(self, files):
        root = tempfile.mkdtemp(dir=str(TASK_TMP))
        root_path = Path(root)
        for relative, content in files.items():
            target = root_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, "utf-8")
        return root_path

    def test_only_reviewed_or_verified_internal_or_public_returned(self):
        root = self._vault({
            "wiki/projects/p.md": (
                "---\nstatus: reviewed\nclassification: internal\n"
                "title: Project\n---\nA stable capability."
            ),
            "wiki/projects/draft.md": (
                "---\nstatus: draft\nclassification: internal\n"
                "title: Draft\n---\nNot yet ready."
            ),
            "wiki/projects/verified.md": (
                "---\nstatus: verified\nclassification: public\n"
                "title: Verified\n---\nPublic canonical fact."
            ),
            "wiki/projects/restricted.md": (
                "---\nstatus: verified\nclassification: restricted\n"
                "title: Restricted\n---\nNever leaked."
            ),
        })
        gateway = CanonicalKnowledgeGateway(
            python_executable="python", script="knowledge.py",
            vault=str(root),
        )
        excerpts = gateway.read_wiki_excerpts(
            pages=["wiki/projects/p.md", "wiki/projects/draft.md",
                   "wiki/projects/verified.md",
                   "wiki/projects/restricted.md"],
            max_bytes=512, project_slug="p")
        pages = sorted(e.page for e in excerpts)
        self.assertEqual(pages, ["wiki/projects/p.md",
                                  "wiki/projects/verified.md"])
        for excerpt in excerpts:
            self.assertIn(excerpt.status, ("reviewed", "verified"))
            self.assertIn(excerpt.classification, ("internal", "public"))

    def test_path_escape_via_dotdot_rejected(self):
        root = self._vault({
            "wiki/projects/p.md": (
                "---\nstatus: reviewed\nclassification: internal\n"
                "title: P\n---\nSafe body."
            ),
            "wiki/projects/../escaped.md": (
                "---\nstatus: reviewed\nclassification: internal\n"
                "title: P\n---\nShould not be read."
            ),
        })
        gateway = CanonicalKnowledgeGateway(
            python_executable="python", script="knowledge.py",
            vault=str(root))
        excerpts = gateway.read_wiki_excerpts(
            pages=["wiki/projects/../escaped.md"],
            max_bytes=256, project_slug="p")
        self.assertEqual(excerpts, ())

    def test_path_escape_via_absolute_prefix_rejected(self):
        root = self._vault({
            "wiki/projects/p.md": "ignored",
        })
        gateway = CanonicalKnowledgeGateway(
            python_executable="python", script="knowledge.py",
            vault=str(root))
        for bad in ("/etc/passwd", "\\Windows\\System32",
                    "wiki/../outside.md"):
            excerpts = gateway.read_wiki_excerpts(
                pages=[bad], max_bytes=256, project_slug="p")
            self.assertEqual(excerpts, (),
                             f"page {bad!r} should have been refused")

    def test_symlink_escape_rejected(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlink not supported on this platform")
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            root = Path(d) / "vault"
            root.mkdir()
            outside = Path(d) / "outside.md"
            outside.write_text(
                "---\nstatus: reviewed\nclassification: internal\n"
                "title: Outside\n---\nSECRET",
                "utf-8")
            try:
                (root / "inside.md").symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink creation unsupported: {exc}")
            gateway = CanonicalKnowledgeGateway(
                python_executable="python", script="knowledge.py",
                vault=str(root))
            excerpts = gateway.read_wiki_excerpts(
                pages=["inside.md"], max_bytes=256, project_slug="p")
            self.assertEqual(excerpts, ())

    def test_secret_bearing_body_redacted(self):
        root = self._vault({
            "wiki/projects/leak.md": (
                "---\nstatus: verified\nclassification: internal\n"
                "title: Synthetic redaction canary\n---\n" + 'sk' + '-' + 'SYNTHETIC' + 'x' * 32
            ),
        })
        gateway = CanonicalKnowledgeGateway(
            python_executable="python", script="knowledge.py",
            vault=str(root))
        excerpts = gateway.read_wiki_excerpts(
            pages=["wiki/projects/leak.md"], max_bytes=512,
            project_slug="p")
        self.assertEqual(excerpts, ())

    def test_excerpt_capped_at_max_bytes(self):
        body = "x" * 4096
        root = self._vault({
            "wiki/projects/big.md": (
                "---\nstatus: verified\nclassification: internal\n"
                "title: Big\n---\n" + body),
        })
        gateway = CanonicalKnowledgeGateway(
            python_executable="python", script="knowledge.py",
            vault=str(root))
        excerpts = gateway.read_wiki_excerpts(
            pages=["wiki/projects/big.md"], max_bytes=128,
            project_slug="p")
        self.assertEqual(len(excerpts), 1)
        self.assertLessEqual(len(excerpts[0].excerpt.encode("utf-8")), 128)

    def test_invalid_max_bytes_returns_empty(self):
        root = self._vault({})
        gateway = CanonicalKnowledgeGateway(
            python_executable="python", script="knowledge.py",
            vault=str(root))
        for bad in (0, -1):
            with self.subTest(max_bytes=bad):
                self.assertEqual(gateway.read_wiki_excerpts(
                    pages=["wiki/projects/p.md"], max_bytes=bad,
                    project_slug="p"), ())


class PipelineWikiExcerptEvidenceTests(unittest.TestCase):

    def test_duplicate_without_named_evidence_is_rejected(self):
        from caretaker.office import (OfficeConnector, OfficeDedupStore,
                                       OfficeIdentity, RateLimiter)
        from caretaker.office import (FakeOfficeTransport,
                                       OfficeSendResult)
        from caretaker.pipeline import PipelineDeps, run_pipeline
        from caretaker.store import Store
        from caretaker.transports import Envelope, FakeTransport
        from caretaker.hub import (FakeKnowledgeGateway,
                                    InventoryItem, WikiExcerpt)
        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name)
            from ._helpers import write_config
            cfg_path = write_config(root)
            # Keep this regression focused on one synthetic record so the
            # source-ref validation does not mask the evidence diagnostic.
            input_path = root / "input.jsonl"
            input_path.write_text(json.dumps({
                "native_session_id": "s",
                "cursor": "c",
                "observed_at": "2026-09-17T10:00:00Z",
                "title": "t",
                "excerpt": "candidate fact",
                "classification": "internal",
            }) + "\n", "utf-8")
            raw_config = json.loads(cfg_path.read_text("utf-8"))
            raw_config["inputs"] = [{
                "path": str(input_path),
                "kind": "normalized_events",
                "tool": "codex",
            }]
            cfg_path.write_text(json.dumps(raw_config), "utf-8")
            cfg = __import__("caretaker.config",
                             fromlist=["load_config"]).load_config(cfg_path)
            store = Store(str(root / "state.sqlite"))
            try:
                gateway = FakeKnowledgeGateway()
                # Seed the inventory with a wiki page; this is the only
                # wiki excerpt the model could cite.
                gateway.inventory_items = [
                    InventoryItem(page="wiki/projects/p.md", title="P",
                                   summary="x", classification="internal"),
                ]
                gateway.wiki_pages["wiki/projects/p.md"] = WikiExcerpt(
                    page="wiki/projects/p.md", title="P",
                    excerpt="Stable capability.",
                    status="verified", classification="internal")

                primary = FakeTransport(name="primary", responses=[
                    Envelope(
                        ok=True, status="ok",
                        final=json.dumps({
                            "decision": "duplicate",
                            "synthesis": "",
                            "source_refs": ["codex:s:c"],
                            "rationale": "unsupported",
                            # No evidence_pages — must be rejected.
                        }),
                        model="Qwen3.8-27B", provider="vllm",
                    ),
                ], expected_identity=("vllm", "Qwen3.8-27B"))
                office = OfficeConnector(
                    identity=OfficeIdentity(harness="h", host="h",
                                             session_id="sid"),
                    transport=FakeOfficeTransport(responses=[
                        OfficeSendResult(status="sent", message_id="m1"),
                    ]),
                    dedup=OfficeDedupStore(),
                    rate=RateLimiter(max_per_minute=60),
                )
                deps = PipelineDeps(primary=primary, fallback=None,
                                     gateway=gateway, office=office,
                                     worktree_root="")
                result = run_pipeline(cfg, deps, store=store)
                self.assertIn("duplicate_without_evidence", result.incidents)
                self.assertEqual(result.proposals_contradiction, 1)
            finally:
                store.close()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
