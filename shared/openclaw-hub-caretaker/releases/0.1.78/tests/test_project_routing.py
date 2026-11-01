"""Project routing: equivalent slugs match, foreign stay rejected, missing stay unassigned."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


from caretaker.publication import (
    PublicationError,
    build_candidate,
    canonical_project_slug,
    freeze,
    proposal_project_identity,
    proposal_targets_project,
    sha,
)
from caretaker.registry import read_registry
from caretaker.audit import eligible_proposals


def _vault(root: Path, *, proposal_front: str, target_name: str = "wiki/projects/system-nominacji-energii.md"):
    vault = root / "vault"
    src = vault / "raw/sources/2026/09/src-one"
    src.mkdir(parents=True)
    data = b"Dated report: local tool canary passed; deployment remains unverified."
    (src / "original.md").write_bytes(data)
    meta = {
        "source_id": "src-one",
        "classification": "internal",
        "local_only": False,
        "original_file": "original.md",
        "sha256": sha(data),
        "byte_length": len(data),
    }
    (src / "source.json").write_text(json.dumps(meta))
    prop = vault / "proposals/agent/one.md"
    prop.parent.mkdir(parents=True)
    prop.write_text(proposal_front + "Report.")
    target = vault / target_name
    target.parent.mkdir(parents=True)
    slug = canonical_project_slug(target_name) or "system-nominacji-energii"
    target.write_text(
        "---\ntitle: Test\nstatus: reviewed\nlast_verified: \"2026-01-01\"\n"
        f"sources: []\nproject_slug: {slug}\n---\n\n# Test\nOld unresolved question.\n"
    )
    return vault


def _registry(vault: Path, *, aliases=None, invalid=False):
    page = vault / 'wiki/registry.md'
    page.parent.mkdir(parents=True, exist_ok=True)
    value = {'schema_version': 1, 'projects': {
        'system-nominacji-energii': {
            'main_page': 'wiki/projects/system-nominacji-energii.md',
            'related': ['related-product']},
        'related-product': {'main_page': 'wiki/projects/related-product.md',
                            'related': ['system-nominacji-energii']}},
        'aliases': aliases if aliases is not None else {'old-nominations': 'system-nominacji-energii'}}
    if invalid:
        value['aliases']['old-nominations'] = 'missing-project'
    page.write_text('# Registry\n\n```json\n' + json.dumps(value) + '\n```\n',
                    encoding='utf-8')


class CanonicalSlugTests(unittest.TestCase):
    def test_path_and_separator_aliases_are_the_same_project(self):
        expected = "system-nominacji-energii"
        variants = [
            "system-nominacji-energii",
            "System_Nominacji_Energii",
            "wiki/projects/system-nominacji-energii.md",
            "projects/system-nominacji-energii.md",
            "wiki/projects/system-nominacji-energii-cont-2.md",
            "projects/system_nominacji_energii",
        ]
        for value in variants:
            with self.subTest(value=value):
                self.assertEqual(canonical_project_slug(value), expected)

    def test_missing_or_unusable_input_stays_unassigned(self):
        for value in (None, "", "   ", "../escape", "???"):
            with self.subTest(value=value):
                self.assertIsNone(canonical_project_slug(value))


class ProposalIdentityTests(unittest.TestCase):
    def test_equivalent_target_variant_matches_batch_project(self):
        text = (
            "---\nid: prop-one\nclassification: internal\n"
            "project_slug: System_Nominacji_Energii\n"
            "target_pages:\n  - projects/system-nominacji-energii.md\n"
            "source_ids:\n  - src-one\n---\n"
        )
        batch = "wiki/projects/system-nominacji-energii.md"
        self.assertTrue(proposal_targets_project(text, batch))
        self.assertEqual(
            proposal_project_identity(text), "system-nominacji-energii"
        )

    def test_foreign_project_is_not_the_batch_project(self):
        text = (
            "---\nid: prop-one\nclassification: internal\n"
            "project_slug: other-project\n"
            "target_pages:\n  - wiki/projects/other-project.md\n"
            "source_ids:\n  - src-one\n---\n"
        )
        self.assertFalse(
            proposal_targets_project(text, "wiki/projects/system-nominacji-energii.md")
        )
        self.assertEqual(proposal_project_identity(text), "other-project")

    def test_missing_project_data_is_not_guessed(self):
        text = (
            "---\nid: prop-one\nclassification: internal\n"
            "source_ids:\n  - src-one\n---\n"
        )
        self.assertIsNone(proposal_project_identity(text))
        self.assertFalse(
            proposal_targets_project(text, "wiki/projects/system-nominacji-energii.md")
        )

    def test_conflicting_slug_and_targets_stay_unassigned(self):
        text = (
            "---\nid: prop-one\nclassification: internal\n"
            "project_slug: alpha\n"
            "target_pages:\n  - wiki/projects/beta.md\n"
            "source_ids:\n  - src-one\n---\n"
        )
        self.assertIsNone(proposal_project_identity(text))
        self.assertFalse(proposal_targets_project(text, "wiki/projects/alpha.md"))


class FreezeRoutingTests(unittest.TestCase):
    def test_registry_alias_routes_and_publishes_to_canonical_main_page(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = _vault(root, proposal_front=(
                '---\nid: prop-one\nclassification: internal\n'
                'project_slug: old-nominations\n'
                'target_pages: ["wiki/projects/old-nominations.md"]\n'
                'source_ids: ["src-one"]\n---\n'))
            _registry(vault)
            registry, warning = read_registry(vault)
            self.assertIsNone(warning)
            text = (vault / 'proposals/agent/one.md').read_text(encoding='utf-8')
            self.assertEqual(proposal_project_identity(text, registry),
                             'system-nominacji-energii')
            found, unsupported = eligible_proposals(vault)
            self.assertEqual(unsupported, 0)
            self.assertEqual(found[0]['project'], 'system-nominacji-energii')
            frozen = freeze(vault, ['proposals/agent/one.md'],
                            'wiki/projects/old-nominations.md')
            self.assertEqual(frozen['target'],
                             'wiki/projects/system-nominacji-energii.md')
            decision = {'body': 'src-one records a dated local test and no deploy.',
                        'summary': 'Dated local test.',
                        'dispositions': {'prop-one': {'disposition': 'synthesized',
                                                       'reason': 'Dated evidence.'}}}
            build_candidate(frozen, decision, root / 'candidate', '2026-09-27')
            self.assertTrue((root / 'candidate/payload/wiki/projects/system-nominacji-energii.md').is_file())
            self.assertFalse((root / 'candidate/payload/wiki/projects/old-nominations.md').exists())

    def test_registry_main_page_can_differ_from_canonical_slug(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = 'wiki/projects/nominations-overview.md'
            vault = _vault(root, proposal_front=(
                '---\nid: prop-one\nclassification: internal\n'
                'project_slug: old-nominations\n'
                'target_pages: ["wiki/projects/old-nominations.md"]\n'
                'source_ids: ["src-one"]\n---\n'), target_name=target)
            page = vault / target
            page.write_text(page.read_text(encoding='utf-8').replace(
                'project_slug: nominations-overview',
                'project_slug: system-nominacji-energii'), encoding='utf-8')
            _registry(vault)
            registry_path = vault / 'wiki/registry.md'
            registry_path.write_text(registry_path.read_text(encoding='utf-8').replace(
                'wiki/projects/system-nominacji-energii.md', target), encoding='utf-8')
            frozen = freeze(vault, ['proposals/agent/one.md'],
                            'wiki/projects/old-nominations.md')
            self.assertEqual(frozen['target'], target)
            self.assertEqual(frozen['project_slug'], 'system-nominacji-energii')

    def test_related_project_is_not_an_alias_and_invalid_registry_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            vault = _vault(Path(d), proposal_front=(
                '---\nid: prop-one\nclassification: internal\n'
                'project_slug: related-product\n'
                'target_pages: ["wiki/projects/related-product.md"]\n'
                'source_ids: ["src-one"]\n---\n'))
            _registry(vault)
            registry, _ = read_registry(vault)
            text = (vault / 'proposals/agent/one.md').read_text(encoding='utf-8')
            self.assertEqual(proposal_project_identity(text, registry), 'related-product')
            with self.assertRaisesRegex(PublicationError, 'proposal targets another project'):
                freeze(vault, ['proposals/agent/one.md'],
                       'wiki/projects/system-nominacji-energii.md')
            _registry(vault, invalid=True)
            registry, warning = read_registry(vault)
            self.assertIsNone(registry)
            self.assertEqual(warning, 'registry_invalid')

    def test_invalid_registry_alias_proposal_keeps_old_unassigned_result(self):
        with tempfile.TemporaryDirectory() as d:
            vault = _vault(Path(d), proposal_front=(
                '---\nid: prop-one\nclassification: internal\n'
                'project_slug: old-nominations\n'
                'target_pages: ["wiki/projects/system-nominacji-energii.md"]\n'
                'source_ids: ["src-one"]\n---\n'))
            self.assertEqual(read_registry(vault)[1], 'registry_missing')
            _registry(vault, invalid=True)
            registry, warning = read_registry(vault)
            self.assertEqual(warning, 'registry_invalid')
            text = (vault / 'proposals/agent/one.md').read_text(encoding='utf-8')
            self.assertIsNone(proposal_project_identity(text, registry))
            _registry(vault, aliases={'related-product': 'system-nominacji-energii'})
            self.assertEqual(read_registry(vault)[1], 'registry_invalid')

    def test_equivalent_slug_variant_is_not_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            vault = _vault(
                Path(d),
                proposal_front=(
                    "---\nid: prop-one\nclassification: internal\n"
                    "project_slug: System_Nominacji_Energii\n"
                    "target_pages:\n  - projects/system-nominacji-energii.md\n"
                    "source_ids:\n  - src-one\n---\n"
                ),
            )
            frozen = freeze(
                vault,
                ["proposals/agent/one.md"],
                "wiki/projects/system-nominacji-energii.md",
            )
            self.assertEqual(
                frozen["target"], "wiki/projects/system-nominacji-energii.md"
            )

    def test_foreign_project_still_raises_same_error(self):
        with tempfile.TemporaryDirectory() as d:
            vault = _vault(
                Path(d),
                proposal_front=(
                    "---\nid: prop-one\nclassification: internal\n"
                    "project_slug: other-project\n"
                    "target_pages:\n  - wiki/projects/other-project.md\n"
                    "source_ids:\n  - src-one\n---\n"
                ),
            )
            with self.assertRaises(PublicationError) as ctx:
                freeze(
                    vault,
                    ["proposals/agent/one.md"],
                    "wiki/projects/system-nominacji-energii.md",
                )
            self.assertEqual(str(ctx.exception), "proposal targets another project")

    def test_missing_project_data_is_not_assigned_to_the_batch(self):
        with tempfile.TemporaryDirectory() as d:
            vault = _vault(
                Path(d),
                proposal_front=(
                    "---\nid: prop-one\nclassification: internal\n"
                    "target_pages: []\n"
                    "source_ids:\n  - src-one\n---\n"
                ),
            )
            with self.assertRaises(PublicationError) as ctx:
                freeze(
                    vault,
                    ["proposals/agent/one.md"],
                    "wiki/projects/system-nominacji-energii.md",
                )
            self.assertEqual(str(ctx.exception), "proposal targets another project")
            self.assertIsNone(
                proposal_project_identity(
                    (vault / "proposals/agent/one.md").read_text(encoding="utf-8")
                )
            )


if __name__ == "__main__":
    unittest.main()
