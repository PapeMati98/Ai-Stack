"""Invocation-local read bounds for project identity and context fingerprints."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import knowledge


MARKER = "IRRELEVANT-BODY-MARKER"


def frontmatter(fields, body=""):
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(value)}]")
        else:
            lines.append(f"{key}: {json.dumps(value) if isinstance(value, str) else value}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


class FingerprintIoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        deep = Path(self.temp.name)
        for index in range(6):
            deep = deep / f"level{index}"
        self.vault = deep / "vault"
        for relative in (
            "wiki/projects",
            "wiki/deep/l1/l2/l3/l4/l5",
            "wiki/infra",
            "proposals/codex",
            "raw/sources/2026/10",
            "events",
            "governance",
        ):
            (self.vault / relative).mkdir(parents=True)
        (self.vault / "knowledge-vault.json").write_text(
            json.dumps({"curator_machine": "TEST", "curation_scope": "designated-machine-only"}),
            encoding="utf-8",
        )
        self.page(
            "projects/alpha-project.md",
            "alpha-project",
            "Alpha",
            body="[[wiki/infra/alpha.md]]\n",
        )
        self.page("infra/alpha.md", "alpha-project", "Infra", page_type="infrastructure", body="Local infra.\n")
        self.write_registry()

    def page(self, relative, project, title, *, body="", status="reviewed", page_type="project", sources=None):
        path = self.vault / "wiki" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frontmatter({
            "title": title,
            "type": page_type,
            "status": status,
            "summary": title,
            "last_verified": "2026-10-01T00:00:00Z",
            "sources": sources or [],
            "project_slug": project,
        }, body), encoding="utf-8")
        return path

    def write_registry(self, aliases=None):
        alias_map = aliases or {"alpha-alias": "alpha-project"}
        registry = {
            "schema_version": 1,
            "navigation_page": "wiki/projects/alpha-project.md",
            "families": [{"id": "alpha", "title": "Alpha", "projects": ["alpha-project"]}],
            "projects": {
                "alpha-project": {
                    "title": "Alpha",
                    "family": "alpha",
                    "main_page": "wiki/projects/alpha-project.md",
                    "latest_page": "wiki/projects/alpha-project.md",
                    "aliases": sorted(alias_map),
                    "related": [],
                    "folders": [],
                    "current_state_source": None,
                }
            },
            "aliases": alias_map,
        }
        (self.vault / "wiki" / "registry.md").write_text(frontmatter({
            "title": "Project registry",
            "type": "system",
            "status": "reviewed",
            "summary": "Navigation",
            "last_verified": "2026-10-01T00:00:00Z",
            "sources": [],
        }, "```json\n" + json.dumps(registry) + "\n```\n"), encoding="utf-8")
        return registry

    def add_proposal(self, name, project_slug, *, body="Synthetic pending.\n", targets=None):
        path = self.vault / "proposals" / "codex" / name
        path.write_text(frontmatter({
            "id": "prop-20261001T000000000Z-0000abcd",
            "type": "proposal",
            "status": "proposed",
            "title": "Synthetic",
            "agent": "codex",
            "created_at": "2026-10-01T00:00:00Z",
            "classification": "internal",
            "source_ids": [],
            "target_pages": targets or [f"wiki/projects/{project_slug}.md"],
            "project_slug": project_slug,
            "knowledge_kind": "general",
        }, body), encoding="utf-8")
        return path

    def add_source(self, source_id, project_slug, *, body=b"source-body"):
        package = self.vault / "raw" / "sources" / "2026" / "10" / source_id
        package.mkdir(parents=True, exist_ok=True)
        original = package / "original.md"
        original.write_bytes(body)
        metadata = {
            "schema_version": 1,
            "source_id": source_id,
            "project_slug": project_slug,
            "original_file": "original.md",
            "sha256": "unused-by-fingerprint-selection",
        }
        (package / "source.json").write_text(json.dumps(metadata), encoding="utf-8")
        return original

    def test_registered_resolution_reads_irrelevant_bodies_once_per_invocation(self):
        irrelevant = []
        for index in range(24):
            body = ("pad\n" * 4000) + MARKER + f"\n{index}\n"
            irrelevant.append(self.add_proposal(f"noise-{index}.md", "other-project", body=body))
            self.page(f"noise/noise-{index}.md", "other-project", f"Noise {index}", body=MARKER + "\n")
            self.add_source(f"src-20261001T00000000{index:02d}Z-noise-aaaaaaaa", "other-project", body=MARKER.encode() + b"\n")
        for index in range(18):
            self.page(
                f"deep/l1/l2/l3/l4/l5/alpha-{index}.md",
                "alpha-project",
                f"Deep {index}",
                body="Relevant deep page.\n",
            )
        collision = self.add_proposal("compact.md", "alphaalias", body="Compact collision from a proposal.\n")
        self.write_registry()
        reads = []
        original_read = knowledge.read_text

        def tracking(path):
            reads.append(Path(path))
            return original_read(path)

        with mock.patch.object(knowledge, "read_text", side_effect=tracking):
            with knowledge.invocation_read_scope(self.vault):
                first = knowledge.resolve_brief_identity(self.vault, "alpha-alias")
                knowledge.resolve_brief_identity(self.vault, "alpha-alias")
                fingerprint = knowledge.project_context_fingerprint(self.vault, "alpha-project", "exact")
                paths = knowledge.project_context_paths(self.vault, "alpha-project", "exact")
                report = knowledge.invocation_read_report()

        self.assertEqual(first[1], "alpha-project")
        self.assertEqual(first[2], "registry-alias")
        self.assertIn("alphaalias", first[3])
        self.assertNotIn(collision, reads)
        for path in irrelevant:
            self.assertNotIn(path, reads)
        self.assertEqual(report["known_slug_builds"], 1)
        self.assertEqual(report["proposal_prefix_reads"], 25)
        self.assertLess(report["reparse_walks"], len(paths))
        self.assertGreaterEqual(len(paths), 18)
        self.assertTrue(fingerprint)

    def test_next_invocation_sees_content_alias_and_reparse_changes(self):
        page = self.vault / "wiki" / "projects" / "alpha-project.md"
        before = knowledge.project_context_fingerprint(self.vault, "alpha-alias", "registry-alias")
        page.write_text(page.read_text(encoding="utf-8") + "Fresh invocation text.\n", encoding="utf-8")
        after = knowledge.project_context_fingerprint(self.vault, "alpha-project", "exact")
        self.assertNotEqual(before, after)
        alias = knowledge.resolve_brief_identity(self.vault, "Alpha Alias")
        self.assertEqual(alias[1:], ("alpha-project", "registry-alias", ["alpha-project"]))

        link = self.vault / "wiki" / "projects" / "alpha-link.md"
        try:
            link.symlink_to(page)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        # The symlinked page is selected only when its slug is read through the link.
        # Reject it when the canonical page path itself is a reparse point.
        replacement = self.vault / "wiki" / "projects" / "alpha-real.md"
        replacement.write_text(page.read_text(encoding="utf-8"), encoding="utf-8")
        page.unlink()
        try:
            page.symlink_to(replacement)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        with self.assertRaisesRegex(knowledge.GatewayError, "reparse|changed"):
            knowledge.project_context_fingerprint(self.vault, "alpha-project", "exact")

    def test_ancestor_late_swap_fails_closed_and_same_scope_does_not_reuse_bytes(self):
        page = self.vault / "wiki" / "projects" / "alpha-project.md"
        with knowledge.invocation_read_scope(self.vault):
            self.assertFalse(knowledge.path_has_reparse_component(self.vault, page))
            walks = knowledge.invocation_read_report()["reparse_walks"]
            self.assertFalse(knowledge.path_has_reparse_component(self.vault, page))
            self.assertEqual(knowledge.invocation_read_report()["reparse_walks"], walks)
            first = knowledge.project_context_fingerprint(self.vault, "alpha-project", "exact")
            page.write_text(page.read_text(encoding="utf-8") + "Same scope bytes.\n", encoding="utf-8")
            second = knowledge.project_context_fingerprint(self.vault, "alpha-project", "exact")
            self.assertNotEqual(first, second)
            original_stat = knowledge._path_stat_for_recheck

            def swapped(path):
                st = original_stat(path)
                if path.name == "projects":
                    class FakeStat:
                        st_dev = st.st_dev
                        st_ino = st.st_ino
                        st_mode = st.st_mode
                        st_file_attributes = int(getattr(st, "st_file_attributes", 0) or 0) | 0x400
                        st_reparse_tag = int(getattr(st, "st_reparse_tag", 0) or 0)
                        st_ctime_ns = int(getattr(st, "st_ctime_ns", 0) or 0)
                    return FakeStat()
                return st

            with mock.patch.object(knowledge, "_path_stat_for_recheck", side_effect=swapped):
                with self.assertRaisesRegex(knowledge.GatewayError, "reparse|changed"):
                    knowledge._recheck_invocation_ancestors()

    def test_unregistered_alias_and_ambiguous_semantics_match_slug_scan(self):
        (self.vault / "wiki" / "registry.md").unlink()
        self.page("projects/example-crm.md", "example-crm", "Example")
        self.add_proposal("example.md", "example-crm", body="Pending only.\n")
        exact = knowledge.resolve_brief_identity(self.vault, "example-crm")
        alias = knowledge.resolve_brief_identity(self.vault, "Example CRM")
        self.assertEqual(exact[1:3], ("example-crm", "exact"))
        self.assertEqual(alias[1:3], ("example-crm", "unique-normalized-alias"))
        self.page("projects/foo-bar.md", "foo-bar", "First")
        self.page("projects/foobar.md", "foobar", "Second")
        ambiguous = knowledge.resolve_brief_identity(self.vault, "foo bar")
        self.assertEqual(ambiguous[2], "ambiguous")
        self.assertEqual(ambiguous[3], ["foo-bar", "foobar"])

    def test_irrelevant_mutation_does_not_change_fingerprint_and_relevant_source_does(self):
        source_id = "src-20261001T010101010Z-alpha-bbbbbbbb"
        original = self.add_source(source_id, "alpha-project", body=b"relevant-source")
        self.page(
            "projects/alpha-project.md",
            "alpha-project",
            "Alpha",
            body="[[wiki/infra/alpha.md]]\n",
            sources=[source_id],
        )
        noise = self.add_proposal("noise.md", "other-project", body="noise-before\n")
        other = self.add_source("src-20261001T010101011Z-other-cccccccc", "other-project", body=b"other-source")
        baseline = knowledge.project_context_fingerprint(self.vault, "alpha-project", "exact")
        noise.write_text(noise.read_text(encoding="utf-8") + "noise-after\n", encoding="utf-8")
        other.write_bytes(b"other-source-changed")
        self.assertEqual(baseline, knowledge.project_context_fingerprint(self.vault, "alpha-project", "exact"))
        original.write_bytes(b"relevant-source-changed")
        self.assertNotEqual(baseline, knowledge.project_context_fingerprint(self.vault, "alpha-project", "exact"))
        paths = knowledge.project_context_paths(self.vault, "alpha-project", "exact")
        self.assertIn(original, paths)
        self.assertNotIn(noise, paths)
        self.assertIn(self.vault / "wiki" / "infra" / "alpha.md", paths)

    def test_large_irrelevant_bodies_are_never_read(self):
        body = "irrelevant padding\n" * 40000
        proposal = self.add_proposal("huge.md", "other-project", body=body)
        page = self.page("noise/huge.md", "other-project", "Huge", body=body)
        per_path: dict[str, int] = {}
        original_open = Path.open

        def counting_open(self, *args, **kwargs):
            handle = original_open(self, *args, **kwargs)
            path_key = str(self)
            mode = args[0] if args else kwargs.get("mode", "r")
            if "r" not in mode or any(flag in mode for flag in ("w", "a", "x", "+")):
                return handle

            class Counting:
                def read(self, size=-1):
                    data = handle.read(size)
                    per_path[path_key] = per_path.get(path_key, 0) + len(data)
                    return data

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return handle.__exit__(*exc)

                def __getattr__(self, name):
                    return getattr(handle, name)

            return Counting()

        with mock.patch.object(Path, "open", counting_open):
            with knowledge.invocation_read_scope(self.vault):
                resolved = knowledge.resolve_brief_identity(self.vault, "alpha-alias")
                fingerprint = knowledge.project_context_fingerprint(self.vault, "alpha-project", "exact")
                report = knowledge.invocation_read_report()

        self.assertEqual(resolved[1], "alpha-project")
        self.assertTrue(fingerprint)
        self.assertGreater(per_path.get(str(proposal), 0), 0)
        self.assertGreater(per_path.get(str(page), 0), 0)
        self.assertLess(per_path.get(str(proposal), 0), len(body))
        self.assertLess(per_path.get(str(page), 0), len(body))
        self.assertEqual(report["proposal_prefix_reads"], 1)
        self.assertEqual(report["proposal_full_reads"], 0)
        self.assertEqual(report["known_slug_builds"], 1)

    def test_no_index_survives_across_invocations(self):
        self.add_proposal("kept.md", "other-project", body="Kept proposal.\n")
        with knowledge.invocation_read_scope(self.vault):
            knowledge.resolve_brief_identity(self.vault, "alpha-alias")
            knowledge.resolve_brief_identity(self.vault, "alpha-alias")
            first = knowledge.invocation_read_report()
        self.assertEqual(first["known_slug_builds"], 1)
        self.assertEqual(first["proposal_prefix_reads"], 1)
        with knowledge.invocation_read_scope(self.vault):
            knowledge.resolve_brief_identity(self.vault, "alpha-alias")
            second = knowledge.invocation_read_report()
        self.assertEqual(second["known_slug_builds"], 1)
        self.assertEqual(second["proposal_prefix_reads"], 1)
        self.assertEqual(second["wiki_prefix_reads"], first["wiki_prefix_reads"])


if __name__ == "__main__":
    unittest.main()
