"""New history pages carry one canonical project proved by the original proposals."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.publication import PublicationError, build_candidate, fm, freeze, sha


ROOT = Path(__file__).resolve().parents[2]
SLUG = "knowledge-hub"
SOURCE = b"Dated report: local tool canary passed; deployment remains unverified."


def _snapshot(root: Path) -> dict[str, bytes]:
    files = {}
    if not root.exists():
        return files
    for path in root.rglob("*"):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _source(vault: Path, source_id: str, payload: bytes = SOURCE) -> None:
    folder = vault / "raw/sources/2026/09" / source_id
    folder.mkdir(parents=True)
    (folder / "original.md").write_bytes(payload)
    meta = {
        "source_id": source_id,
        "classification": "internal",
        "local_only": False,
        "original_file": "original.md",
        "sha256": sha(payload),
        "byte_length": len(payload),
    }
    (folder / "source.json").write_text(json.dumps(meta), encoding="utf-8")


def _page(vault: Path, slug: str = SLUG) -> None:
    path = vault / "wiki/projects" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntitle: Test\nstatus: reviewed\nlast_verified: \"2026-01-01\"\n"
        f"sources: []\nproject_slug: {slug}\n---\n\n# Test\nOld unresolved question.\n",
        encoding="utf-8",
    )


def _closeout(project: str) -> bytes:
    return (
        "# Agent session closeout\n\n"
        "Agent: codex\n"
        f"Project: {project}\n"
        f"Session SHA-256: {'a' * 64}\n\n"
        "Dated report: local tool canary passed; deployment remains unverified.\n"
    ).encode()


def _proposal(vault: Path, name: str, source_id: str, *, slug: str | None = None,
              targets: list[str] | None = None, title: str | None = None,
              sources: list[str] | None = None) -> None:
    path = vault / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"id: {path.stem}",
        "classification: internal",
        "source_ids:",
    ]
    for sid in sources or [source_id]:
        lines.append(f"  - {sid}")
    if slug is not None:
        lines.append(f"project_slug: {slug}")
    if title is not None:
        lines.append(f"title: {json.dumps(title)}")
    if targets is not None:
        lines.append("target_pages: " + json.dumps(targets))
    lines.append("---")
    lines.append("Report.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _registry(vault: Path) -> None:
    value = {
        "schema_version": 1,
        "projects": {SLUG: {"main_page": f"wiki/projects/{SLUG}.md"}},
        "aliases": {"legacy-hub": SLUG},
    }
    page = vault / "wiki/registry.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# Registry\n\n```json\n" + json.dumps(value) + "\n```\n", encoding="utf-8")


def _decision(disposition: str = "synthesized", reason: str = "Dated evidence.",
              body: str | None = None, proposals: tuple[str, ...] = ("prop-one",)) -> dict:
    text = body or "Dated tool canary report (src-one); deployment unverified."
    return {
        "body": text,
        "summary": "Dated local test.",
        "dispositions": {pid: {"disposition": disposition, "reason": reason} for pid in proposals},
    }


def _history_slug(root: Path) -> str:
    pages = list((root / "payload/wiki/history").glob("openclaw-*.md"))
    if not pages:
        raise AssertionError("no generated history page")
    slugs = {fm(page.read_text(encoding="utf-8"), "project_slug") for page in pages}
    if slugs != {SLUG}:
        raise AssertionError(slugs)
    for page in pages:
        text = page.read_text(encoding="utf-8")
        if fm(text, "continues") is not None:
            raise AssertionError("history inherited continues")
        if SLUG in page.name:
            raise AssertionError("history filename used as project proof")
    return SLUG


class HistoryProjectSlugTests(unittest.TestCase):
    def _vault(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        tmp = tempfile.TemporaryDirectory()
        vault = Path(tmp.name) / "vault"
        _page(vault)
        old = vault / "wiki/history/openclaw-already-published.md"
        old.parent.mkdir(parents=True)
        old.write_text("---\ntitle: prior\ntype: evidence-ledger\nstatus: reviewed\nsummary: prior\n---\n\nold\n", encoding="utf-8")
        return tmp, vault

    def _refuse(self, vault: Path, names: list[str], target: str, message: str) -> None:
        before = _snapshot(vault)
        out = vault.parent / "candidate"
        with self.assertRaises(PublicationError) as caught:
            frozen = freeze(vault, names, target)
            build_candidate(frozen, _decision(), out, "2026-10-05")
        self.assertEqual(str(caught.exception), message)
        self.assertEqual(_snapshot(vault), before)
        self.assertFalse(out.exists())

    def test_explicit_slug_is_on_every_new_history_page(self):
        tmp, vault = self._vault()
        try:
            _source(vault, "src-one")
            _proposal(vault, "proposals/agent/prop-one.md", "src-one", slug=SLUG,
                      targets=[f"wiki/projects/{SLUG}.md"])
            before = _snapshot(vault)
            frozen = freeze(vault, ["proposals/agent/prop-one.md"], f"wiki/projects/{SLUG}.md")
            self.assertEqual(frozen["proposals"][0]["project_proof"]["canonical"], SLUG)
            self.assertEqual(frozen["proposals"][0]["project_proof"]["declared"], SLUG)
            out = Path(tmp.name) / "candidate"
            build_candidate(frozen, _decision(), out, "2026-10-05")
            self.assertEqual(_history_slug(out), SLUG)
            self.assertEqual(_snapshot(vault), before)
        finally:
            tmp.cleanup()

    def test_alias_canonicalizes_deterministically(self):
        tmp, vault = self._vault()
        try:
            _source(vault, "src-one")
            _registry(vault)
            _proposal(vault, "proposals/agent/prop-one.md", "src-one", slug="legacy-hub",
                      targets=["wiki/projects/legacy-hub.md"])
            proofs = []
            slugs = []
            for folder in ("one", "two"):
                frozen = freeze(vault, ["proposals/agent/prop-one.md"], "wiki/projects/legacy-hub.md")
                proofs.append(frozen["proposals"][0]["project_proof"]["canonical"])
                out = Path(tmp.name) / folder
                build_candidate(frozen, _decision(), out, "2026-10-05")
                slugs.append(_history_slug(out))
                page = out / "payload/wiki/projects" / f"{SLUG}.md"
                self.assertTrue(page.is_file())
                self.assertFalse((out / "payload/wiki/projects/legacy-hub.md").exists())
            self.assertEqual(proofs, [SLUG, SLUG])
            self.assertEqual(slugs, [SLUG, SLUG])
        finally:
            tmp.cleanup()

    def test_target_metadata_and_source_closeout_resolve_without_declared_slug(self):
        tmp, vault = self._vault()
        try:
            _source(vault, "src-one")
            _proposal(vault, "proposals/agent/prop-one.md", "src-one",
                      targets=[f"wiki/projects/{SLUG}.md"])
            frozen = freeze(vault, ["proposals/agent/prop-one.md"], f"wiki/projects/{SLUG}.md")
            self.assertIsNone(frozen["proposals"][0]["project_proof"]["declared"])
            self.assertEqual(frozen["proposals"][0]["project_proof"]["targets"], [SLUG])
            build_candidate(frozen, _decision(), Path(tmp.name) / "from-target", "2026-10-05")
            self.assertEqual(_history_slug(Path(tmp.name) / "from-target"), SLUG)

            _source(vault, "src-closeout", _closeout(SLUG))
            _proposal(vault, "proposals/agent/prop-closeout.md", "src-closeout")
            frozen = freeze(vault, ["proposals/agent/prop-closeout.md"], f"wiki/projects/{SLUG}.md")
            proof = frozen["proposals"][0]["project_proof"]
            self.assertEqual(proof["source"], SLUG)
            self.assertIsNone(proof["declared"])
            self.assertIsNone(proof["session_title"])
            self.assertEqual(proof["targets"], [])
            build_candidate(frozen, _decision(proposals=("prop-closeout",),
                                               body="Dated tool canary report (src-closeout); deployment unverified."),
                            Path(tmp.name) / "from-source", "2026-10-05")
            self.assertEqual(_history_slug(Path(tmp.name) / "from-source"), SLUG)
        finally:
            tmp.cleanup()

    def test_accepted_no_durable_session_keeps_its_project_and_historical_disposition(self):
        tmp, vault = self._vault()
        try:
            _source(vault, "src-one")
            _proposal(vault, "proposals/agent/prop-one.md", "src-one",
                      title=f"Session {SLUG} NO_DURABLE_CHANGE abc123")
            frozen = freeze(vault, ["proposals/agent/prop-one.md"], f"wiki/projects/{SLUG}.md")
            proof = frozen["proposals"][0]["project_proof"]
            self.assertEqual(proof["session_title"], SLUG)
            self.assertIsNone(proof["declared"])
            out = Path(tmp.name) / "candidate"
            build_candidate(frozen, _decision("historical", "No durable change recorded."), out, "2026-10-05")
            self.assertEqual(_history_slug(out), SLUG)
            text = next((out / "payload/wiki/history").glob("openclaw-*.md")).read_text(encoding="utf-8")
            self.assertIn("prop-one — historical: No durable change recorded.", text)
        finally:
            tmp.cleanup()

    def test_missing_conflict_mismatch_and_invalid_slug_write_nothing(self):
        tmp, vault = self._vault()
        try:
            _source(vault, "src-one")
            _source(vault, "src-hub", _closeout(SLUG))
            _source(vault, "src-other", _closeout("other-project"))
            _proposal(vault, "proposals/agent/prop-missing.md", "src-one")
            self._refuse(vault, ["proposals/agent/prop-missing.md"], f"wiki/projects/{SLUG}.md",
                         "proposal targets another project")

            _proposal(vault, "proposals/agent/prop-mixed.md", "src-one", slug=SLUG,
                      targets=["wiki/projects/other-project.md"])
            self._refuse(vault, ["proposals/agent/prop-mixed.md"], f"wiki/projects/{SLUG}.md",
                         "history_project_conflict")

            _proposal(vault, "proposals/agent/prop-foreign.md", "src-one", slug="other-project",
                      targets=["wiki/projects/other-project.md"])
            self._refuse(vault, ["proposals/agent/prop-foreign.md"], f"wiki/projects/{SLUG}.md",
                         "proposal targets another project")

            _proposal(vault, "proposals/agent/prop-bad.md", "src-one", slug="???",
                      targets=[f"wiki/projects/{SLUG}.md"])
            self._refuse(vault, ["proposals/agent/prop-bad.md"], f"wiki/projects/{SLUG}.md",
                         "invalid project slug")

            _proposal(vault, "proposals/agent/prop-sources.md", "src-hub",
                      sources=["src-hub", "src-other"])
            self._refuse(vault, ["proposals/agent/prop-sources.md"], f"wiki/projects/{SLUG}.md",
                         "history_project_conflict")
        finally:
            tmp.cleanup()

    def test_target_and_missing_proof_are_not_accepted_as_identity(self):
        tmp, vault = self._vault()
        try:
            _source(vault, "src-one")
            _proposal(vault, "proposals/agent/prop-one.md", "src-one", slug=SLUG,
                      targets=[f"wiki/projects/{SLUG}.md"])
            frozen = freeze(vault, ["proposals/agent/prop-one.md"], f"wiki/projects/{SLUG}.md")
            before = _snapshot(vault)
            frozen["proposals"][0].pop("project_proof")
            out = Path(tmp.name) / "missing-proof"
            with self.assertRaises(PublicationError) as missing:
                build_candidate(frozen, _decision(), out, "2026-10-05")
            self.assertEqual(str(missing.exception), "history_project_missing")
            self.assertFalse(out.exists())
            self.assertEqual(_snapshot(vault), before)

            frozen = freeze(vault, ["proposals/agent/prop-one.md"], f"wiki/projects/{SLUG}.md")
            frozen["target"] = "wiki/projects/other-project.md"
            frozen["project_slug"] = "other-project"
            out = Path(tmp.name) / "target-mismatch"
            with self.assertRaises(PublicationError) as mismatch:
                build_candidate(frozen, _decision(), out, "2026-10-05")
            self.assertEqual(str(mismatch.exception), "proposal targets another project")
            self.assertFalse(out.exists())
            self.assertEqual(_snapshot(vault), before)
        finally:
            tmp.cleanup()

    def test_batch_cap_and_uncited_synthesis_still_refuse_before_writes(self):
        tmp, vault = self._vault()
        try:
            with self.assertRaises(PublicationError) as capped:
                freeze(vault, [f"proposals/agent/prop-{i}.md" for i in range(9)],
                       f"wiki/projects/{SLUG}.md")
            self.assertEqual(str(capped.exception), "proposal batch outside bound")
            self.assertFalse((vault.parent / "candidate").exists())

            _source(vault, "src-one")
            _proposal(vault, "proposals/agent/prop-one.md", "src-one", slug=SLUG,
                      targets=[f"wiki/projects/{SLUG}.md"])
            frozen = freeze(vault, ["proposals/agent/prop-one.md"], f"wiki/projects/{SLUG}.md")
            before = _snapshot(vault)
            out = Path(tmp.name) / "uncited"
            with self.assertRaises(PublicationError) as uncited:
                build_candidate(frozen, _decision(body="Uncited claim with no source ID at all."),
                                out, "2026-10-05")
            self.assertEqual(str(uncited.exception), "synthesized proposal lacks citation")
            self.assertFalse(out.exists())
            self.assertEqual(_snapshot(vault), before)
        finally:
            tmp.cleanup()

    def test_reviewed_control_has_no_deny_or_human_route_and_preserves_other_fields(self):
        before = json.loads((ROOT / "control-before.json").read_text(encoding="utf-8"))
        reviewed = json.loads((ROOT / "control-reviewed.json").read_text(encoding="utf-8"))
        self.assertEqual(before.get("owner"), "parent")
        self.assertEqual(reviewed.get("owner"), "parent")
        self.assertEqual(before.get("hub_backlog"), reviewed.get("hub_backlog"))
        self.assertEqual(before.get("session_admission"), reviewed.get("session_admission"))
        self.assertNotIn("human", json.dumps(reviewed.get("hub_backlog")).casefold())
        self.assertNotIn("human", json.dumps(reviewed.get("session_admission")).casefold())

        def candidates(doc, lane):
            rows = doc["publication"][lane]["candidates"]
            self.assertIsInstance(rows, list)
            self.assertTrue(rows)
            for row in rows:
                agent = str(row.get("agent", ""))
                model = str(row.get("model", ""))
                blob = f"{agent} {model}".casefold()
                self.assertNotIn("deny", blob)
                self.assertNotIn("glm", blob)
                self.assertNotIn("antigravity", blob)
            return rows

        for lane in ("reviewer", "append_maker"):
            candidates(reviewed, lane)
        changed = _changed_paths(before, reviewed)
        self.assertEqual(changed, [
            "publication.append_maker.candidates",
            "publication.reviewer.candidates",
        ])


def _changed_paths(before, reviewed, prefix=""):
    paths = []
    if type(before) is not type(reviewed):
        return [prefix or "<root>"]
    if isinstance(before, dict):
        for key in sorted(set(before) | set(reviewed)):
            child = f"{prefix}.{key}" if prefix else key
            if key not in before or key not in reviewed:
                paths.append(child)
            else:
                paths.extend(_changed_paths(before[key], reviewed[key], child))
        return paths
    if before != reviewed:
        paths.append(prefix or "<root>")
    return paths


if __name__ == "__main__":
    unittest.main()
