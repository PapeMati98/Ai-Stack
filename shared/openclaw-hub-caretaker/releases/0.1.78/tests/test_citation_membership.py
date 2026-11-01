"""Citation membership: new-section source_id tokens must belong to this packet."""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from caretaker.publication import PublicationError, build_candidate, freeze, sha


PACKET_SOURCES = (
    "src-20260912T080000000Z-session-alpha-111111111111-aaaaaaaa",
    "src-20260912T090000000Z-session-beta-222222222222-bbbbbbbb",
    "src-20260912T100000000Z-session-gamma-333333333333-cccccccc",
)
FOREIGN_SOURCE = (
    "src-20260916T120000000Z-session-fixture-foreign-444444444444-dddddddd"
)
HOST_MARKER = "example.invalid"
ADDR_MARKER = "203.0.113.10"
PRIOR_SOURCE = "src-20260901T000000000Z-session-old-page-deadbeefdead"


def _write_source(vault: Path, sid: str, body: bytes) -> None:
    src = vault / "raw/sources/2026/09" / sid
    src.mkdir(parents=True, exist_ok=True)
    (src / "original.md").write_bytes(body)
    meta = {
        "source_id": sid,
        "classification": "internal",
        "local_only": False,
        "original_file": "original.md",
        "sha256": sha(body),
        "byte_length": len(body),
    }
    (src / "source.json").write_text(json.dumps(meta), encoding="utf-8")


def _vault(root: Path) -> Path:
    vault = root / "vault"
    for sid in PACKET_SOURCES:
        _write_source(
            vault,
            sid,
            b"Dated 2026-09-12 producer report; deployment remains unverified.",
        )
    proposal = vault / "proposals/agent/one.md"
    proposal.parent.mkdir(parents=True)
    listed = "\n".join(f"  - {sid}" for sid in PACKET_SOURCES)
    proposal.write_text(
        "---\nid: prop-one\nclassification: internal\nsource_ids:\n"
        + listed
        + "\ntarget_pages:\n  - wiki/projects/agent-stack-cont-3.md\n---\nReport.\n",
        encoding="utf-8",
    )
    target = vault / "wiki/projects/agent-stack-cont-3.md"
    target.parent.mkdir(parents=True)
    prior = (
        "---\ntitle: Agent stack\nstatus: reviewed\nlast_verified: \"2026-09-12\"\n"
        "sources: []\nproject_slug: agent-stack-cont-3\n---\n\n"
        "# Continuation stub\n\n"
        f"Earlier page cites {PRIOR_SOURCE} from a previous publication.\n"
    )
    target.write_text(prior, encoding="utf-8")
    return vault


def _decision(body: str, disposition: str = "synthesized") -> dict:
    return {
        "body": body,
        "summary": "Section from this publication packet.",
        "dispositions": {
            "prop-one": {
                "disposition": disposition,
                "reason": "Packet-scoped report integrated.",
            }
        },
    }


class CitationMembershipTests(unittest.TestCase):
    def test_foreign_source_id_in_new_section_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = _vault(root)
            frozen = freeze(
                vault,
                ["proposals/agent/one.md"],
                "wiki/projects/agent-stack-cont-3.md",
            )
            self.assertEqual(sorted(frozen["sources"]), sorted(PACKET_SOURCES))
            body = (
                "## Recovery (Example A & Example B)\n\n"
                f"Packet note ({PACKET_SOURCES[0]}). "
                f"Host {HOST_MARKER} at {ADDR_MARKER} "
                f"({FOREIGN_SOURCE}). Producer-reported only."
            )
            records: list[str] = []

            class _Capture(logging.Handler):
                def emit(self, record):
                    records.append(record.getMessage())
                    records.append(self.format(record))

            handler = _Capture()
            logger = logging.getLogger()
            previous = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            try:
                with self.assertRaises(PublicationError) as ctx:
                    build_candidate(
                        frozen,
                        _decision(body),
                        root / "bad-foreign",
                        "2026-09-20",
                    )
            finally:
                logger.removeHandler(handler)
                logger.setLevel(previous)
            self.assertEqual(str(ctx.exception), "citation_outside_publication")
            joined = "\n".join(records) + str(ctx.exception)
            self.assertNotIn(HOST_MARKER, joined)
            self.assertNotIn(ADDR_MARKER, joined)
            self.assertNotIn(body, joined)
            self.assertFalse((root / "bad-foreign").exists())

    def test_prior_page_citations_do_not_reject(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = _vault(root)
            frozen = freeze(
                vault,
                ["proposals/agent/one.md"],
                "wiki/projects/agent-stack-cont-3.md",
            )
            self.assertIn(PRIOR_SOURCE, frozen["before"])
            body = (
                "Dated 2026-09-12 packet report "
                f"({PACKET_SOURCES[0]}); deployment unverified. "
                "Producer-reported only."
            )
            self.assertNotIn(PRIOR_SOURCE, body)
            manifest = build_candidate(
                frozen, _decision(body), root / "good-prior", "2026-09-20"
            )
            value = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(sorted(value["source_ids"]), sorted(PACKET_SOURCES))
            published = (
                root / "good-prior" / "payload" / "wiki" / "projects"
                / "agent-stack-cont-3.md"
            ).read_text(encoding="utf-8")
            self.assertIn(PRIOR_SOURCE, published)
            self.assertIn(PACKET_SOURCES[0], published)

    def test_in_packet_citation_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = _vault(root)
            frozen = freeze(
                vault,
                ["proposals/agent/one.md"],
                "wiki/projects/agent-stack-cont-3.md",
            )
            body = (
                "Three packet sources cited: "
                f"{PACKET_SOURCES[0]}, {PACKET_SOURCES[1]}, {PACKET_SOURCES[2]}. "
                "Producer-reported only."
            )
            manifest = build_candidate(
                frozen, _decision(body), root / "good-packet", "2026-09-20"
            )
            value = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(len(value["source_ids"]), 3)
            self.assertNotIn(FOREIGN_SOURCE, value["source_ids"])

    def test_synthesized_still_requires_a_citation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            vault = _vault(root)
            frozen = freeze(
                vault,
                ["proposals/agent/one.md"],
                "wiki/projects/agent-stack-cont-3.md",
            )
            with self.assertRaises(PublicationError) as ctx:
                build_candidate(
                    frozen,
                    _decision("Uncited claim with no source ID at all here."),
                    root / "bad-cite",
                    "2026-09-20",
                )
            self.assertEqual(str(ctx.exception), "synthesized proposal lacks citation")


if __name__ == "__main__":
    unittest.main()
