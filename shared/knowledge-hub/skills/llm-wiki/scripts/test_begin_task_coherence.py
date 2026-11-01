#!/usr/bin/env python3
"""Coherence tests for Knowledge Hub begin-task and context verification.

Demonstrates that unrelated appends during read (events, unrelated proposals,
unrelated raw sources, unrelated wiki pages) preserve coherent read, while
mutations to own pages, proposals, referenced sources, returned related pages,
aliases, vault config, or curator lock appearance fail closed with appropriate
failure codes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import knowledge

HERE = Path(__file__).resolve().parent
GATEWAY = HERE / "knowledge.py"


def frontmatter(fields: dict, body: str = "") -> str:
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {json.dumps(value)}")
    lines.extend(["---", body])
    return "\n".join(lines) + "\n"


class BeginTaskCoherenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "vault"
        self.receipts = Path(self.temp.name) / "receipts"
        (self.root / "wiki" / "projects").mkdir(parents=True)
        (self.root / "wiki" / "infra").mkdir(parents=True)
        (self.root / "proposals" / "codex").mkdir(parents=True)
        (self.root / "events").mkdir(parents=True)
        (self.root / "raw" / "sources").mkdir(parents=True)
        (self.root / "governance").mkdir(parents=True)

        (self.root / "knowledge-vault.json").write_text(
            json.dumps({"curator_machine": "TEST", "curation_scope": "designated-machine-only"}),
            encoding="utf-8",
        )

        (self.root / "wiki" / "projects" / "alpha-project.md").write_text(
            frontmatter(
                {
                    "title": "Alpha project",
                    "type": "project",
                    "status": "reviewed",
                    "summary": "Current alpha context",
                    "last_verified": "2026-10-01T00:00:00Z",
                    "sources": [],
                    "project_slug": "alpha-project",
                },
                "[[wiki/infra/alpha.md]]\n\n## Findings\n- Finding 1\n",
            ),
            encoding="utf-8",
        )

        (self.root / "wiki" / "infra" / "alpha.md").write_text(
            frontmatter(
                {
                    "title": "Alpha system",
                    "type": "system",
                    "status": "reviewed",
                    "summary": "Infra details",
                    "last_verified": "2026-10-01T00:00:00Z",
                    "sources": [],
                },
                "",
            ),
            encoding="utf-8",
        )

        proposal_id = "prop-20261001T000000000Z-00000001"
        (self.root / "proposals" / "codex" / f"{proposal_id}-alpha.md").write_text(
            frontmatter(
                {
                    "id": proposal_id,
                    "type": "proposal",
                    "status": "proposed",
                    "title": "Alpha proposal",
                    "agent": "codex",
                    "created_at": "2026-10-01T00:00:00Z",
                    "classification": "internal",
                    "source_ids": [],
                    "target_pages": ["wiki/projects/alpha-project.md"],
                },
                "Alpha proposal body",
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def begin_args(self, **overrides) -> argparse.Namespace:
        return argparse.Namespace(**{
            "agent": "codex",
            "session_id": "test-session-id",
            "project_slug": "alpha-project",
            "require_context": True,
            "receipt_root": str(self.receipts),
            "limit": 10,
            **overrides,
        })

    def run_gateway(self, *args) -> tuple[subprocess.CompletedProcess[str], dict]:
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            [sys.executable, "-B", str(GATEWAY), *args, "--vault", str(self.root)],
            capture_output=True,
            text=True,
            check=False,
            creationflags=creation_flags,
        )
        payload = json.loads(result.stdout) if result.stdout.strip().startswith("{") else {}
        return result, payload

    def verify_payload(self, started: dict, project_slug: str = "alpha-project") -> dict:
        return knowledge.verify_context(self.root, argparse.Namespace(
            context_receipt=started["receipt_path"],
            agent="codex",
            session_id="test-session-id",
            project_slug=project_slug,
        ))

    def add_source(self, index: int, project_slug: str = "alpha-project") -> tuple[str, str]:
        source_id = f"src-20261001T000000000Z-synthetic-{index:08x}"
        package = self.root / "raw" / "sources" / source_id
        package.mkdir(parents=True, exist_ok=True)
        content = f"Synthetic source payload {index}.\n".encode("utf-8")
        (package / "original.md").write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        metadata = {
            "schema_version": 1,
            "source_id": source_id,
            "title": f"Source {index}",
            "origin": "synthetic",
            "captured_at": "2026-10-01T00:00:00Z",
            "captured_by": "codex",
            "classification": "internal",
            "local_only": False,
            "original_file": "original.md",
            "byte_length": len(content),
            "sha256": digest,
            "trust": "untrusted",
            "risk_flags": [],
            "project_slug": project_slug,
            "knowledge_kind": "critical-state",
            "state_summary": "Synthetic current project state",
        }
        (package / "source.json").write_text(json.dumps(metadata), encoding="utf-8")
        return source_id, digest

    def write_registry(self, data: dict) -> Path:
        registry_file = self.root / "wiki" / "registry.md"
        body = json.dumps(data, indent=2)
        content = (
            "---\n"
            'title: "Project registry"\n'
            'type: "system"\n'
            'status: "reviewed"\n'
            'summary: "Project registry definition"\n'
            'last_verified: "2026-10-01T00:00:00Z"\n'
            "sources: []\n"
            "---\n\n"
            f"```json\n{body}\n```\n"
        )
        registry_file.write_text(content, encoding="utf-8")
        return registry_file

    def test_unrelated_append_historical_behavior_proves_red_before_fix(self):
        """Demonstrates that legacy whole-vault snapshotting failed with REVISION_CHANGED

        when unrelated events, proposals, or raw sources were appended during read.
        """
        original_brief = knowledge.project_brief

        generation = 0

        def brief_with_unrelated_append(vault, args):
            nonlocal generation
            generation += 1
            result = original_brief(vault, args)
            # Simulate unrelated event append
            evt_id = "evt-20261001T120000000Z-unrelated"
            (vault / "events" / f"{evt_id}.json").write_text(
                json.dumps({"event_id": evt_id, "action": "new-proposal", "agent": "codex",
                            "occurred_at": "2026-10-01T12:00:00Z", "proposal_ids": [],
                            "pages_changed": [], "source_ids": [], "summary": f"Synthetic append {generation}"}),
                encoding="utf-8",
            )
            # Simulate unrelated proposal append
            prop_id = "prop-20261001T120000000Z-00000002"
            (vault / "proposals" / "codex" / f"{prop_id}-beta.md").write_text(
                frontmatter({
                    "id": prop_id,
                    "type": "proposal",
                    "status": "proposed",
                    "title": "Beta proposal",
                    "agent": "codex",
                    "created_at": "2026-10-01T12:00:00Z",
                    "classification": "internal",
                    "source_ids": [],
                    "target_pages": ["wiki/projects/beta-project.md"],
                }),
                encoding="utf-8",
            )
            return result

        original_snapshot = knowledge.project_snapshot
        # Force legacy whole-vault snapshot behavior (project_ref=None)
        def legacy_project_snapshot(vault, project_ref=None):
            return original_snapshot(vault, None)

        with mock.patch.object(knowledge, "project_snapshot", side_effect=legacy_project_snapshot), \
             mock.patch.object(knowledge, "project_brief", side_effect=brief_with_unrelated_append):
            started = knowledge.begin_task(self.root, self.begin_args())

        # Under the legacy behavior, unrelated appends cause REVISION_CHANGED and exhaustion
        self.assertEqual(started["status"], "FAILED")
        self.assertEqual(len(started["execution"]["attempts"]), 2)
        self.assertEqual(started["execution"]["attempts"][0]["failure_code"], "REVISION_CHANGED")
        self.assertEqual(started["execution"]["attempts"][1]["failure_code"], "REVISION_CHANGED")

    def test_unrelated_appends_during_read_preserve_coherence(self):
        """Green after fix: project-scoped coherent read ignores unrelated appends."""
        original_brief = knowledge.project_brief

        def brief_with_unrelated_append(vault, args):
            result = original_brief(vault, args)
            # Unrelated event append
            evt_id = "evt-20261001T120000000Z-99999999"
            (vault / "events" / f"{evt_id}.json").write_text(
                json.dumps({"event_id": evt_id, "action": "new-proposal", "agent": "codex",
                            "occurred_at": "2026-10-01T12:00:00Z", "proposal_ids": [],
                            "pages_changed": [], "source_ids": [], "summary": "Synthetic unrelated append"}),
                encoding="utf-8",
            )
            # Unrelated proposal append
            prop_id = "prop-20261001T120000000Z-00000002"
            (vault / "proposals" / "codex" / f"{prop_id}-beta.md").write_text(
                frontmatter({
                    "id": prop_id,
                    "type": "proposal",
                    "status": "proposed",
                    "title": "Beta01 proposal",
                    "agent": "codex",
                    "created_at": "2026-10-01T12:00:00Z",
                    "classification": "internal",
                    "source_ids": [],
                    "target_pages": ["wiki/projects/beta-project.md"],
                }),
                encoding="utf-8",
            )
            # Unrelated raw source append
            self.add_source(999, project_slug="beta-project")
            return result

        with mock.patch.object(knowledge, "project_brief", side_effect=brief_with_unrelated_append):
            started = knowledge.begin_task(self.root, self.begin_args())

        self.assertEqual(started["status"], "COMPLETE")
        self.assertTrue(started["coherent_read"])
        self.assertEqual(len(started["execution"]["attempts"]), 1)
        self.assertTrue(self.verify_payload(started)["valid"])

    def test_negative_own_page_mutation_triggers_revision_changed(self):
        """Mutating own project page during read triggers REVISION_CHANGED."""
        page = self.root / "wiki" / "projects" / "alpha-project.md"
        attempts_mutated = 0

        original_brief = knowledge.project_brief

        def mutating_brief(vault, args):
            nonlocal attempts_mutated
            attempts_mutated += 1
            page.write_text(page.read_text(encoding="utf-8") + f"\nMutation attempt {attempts_mutated}\n", encoding="utf-8")
            return original_brief(vault, args)

        with mock.patch.object(knowledge, "project_brief", side_effect=mutating_brief):
            started = knowledge.begin_task(self.root, self.begin_args())

        # Continuous mutation causes two REVISION_CHANGED attempts and final failure
        self.assertEqual(started["status"], "FAILED")
        self.assertEqual(len(started["execution"]["attempts"]), 2)
        self.assertEqual(started["execution"]["attempts"][0]["failure_code"], "REVISION_CHANGED")
        self.assertEqual(started["execution"]["attempts"][1]["failure_code"], "REVISION_CHANGED")

    def test_negative_matching_proposal_addition_triggers_revision_changed(self):
        """Adding a proposal matching the project during read triggers REVISION_CHANGED."""
        original_brief = knowledge.project_brief
        added = False

        def brief_adding_matching_proposal(vault, args):
            nonlocal added
            if not added:
                added = True
                new_prop_id = "prop-20261001T120000000Z-00000003"
                (vault / "proposals" / "codex" / f"{new_prop_id}-added.md").write_text(
                    frontmatter({
                        "id": new_prop_id,
                        "type": "proposal",
                        "status": "proposed",
                        "title": "Racing proposal",
                        "agent": "codex",
                        "created_at": "2026-10-01T12:00:00Z",
                        "classification": "internal",
                        "source_ids": [],
                        "target_pages": ["wiki/projects/alpha-project.md"],
                    }),
                    encoding="utf-8",
                )
            return original_brief(vault, args)

        with mock.patch.object(knowledge, "project_brief", side_effect=brief_adding_matching_proposal):
            started = knowledge.begin_task(self.root, self.begin_args())

        # First attempt failed with REVISION_CHANGED, second attempt succeeded with the added proposal
        self.assertEqual(started["status"], "COMPLETE")
        self.assertEqual(len(started["execution"]["attempts"]), 2)
        self.assertEqual(started["execution"]["attempts"][0]["failure_code"], "REVISION_CHANGED")

    def test_negative_matching_proposal_removal_triggers_revision_changed(self):
        """Removing a matching proposal during read triggers REVISION_CHANGED."""
        original_brief = knowledge.project_brief
        removed = False
        proposal_file = self.root / "proposals" / "codex" / "prop-20261001T000000000Z-00000001-alpha.md"

        def brief_removing_proposal(vault, args):
            nonlocal removed
            if not removed and proposal_file.is_file():
                removed = True
                # Move/remove the proposal
                proposal_file.unlink()
            return original_brief(vault, args)

        with mock.patch.object(knowledge, "project_brief", side_effect=brief_removing_proposal):
            started = knowledge.begin_task(self.root, self.begin_args())

        self.assertEqual(started["status"], "COMPLETE")
        self.assertEqual(len(started["execution"]["attempts"]), 2)
        self.assertEqual(started["execution"]["attempts"][0]["failure_code"], "REVISION_CHANGED")

    def test_negative_referenced_source_mutation_triggers_revision_changed(self):
        """Mutating a referenced source original file during read triggers REVISION_CHANGED."""
        source_id, _ = self.add_source(1, project_slug="alpha-project")
        # Reference this source in alpha-project.md
        page = self.root / "wiki" / "projects" / "alpha-project.md"
        page.write_text(
            frontmatter(
                {
                    "title": "Alpha project",
                    "type": "project",
                    "status": "reviewed",
                    "summary": "Alpha context",
                    "last_verified": "2026-10-01T00:00:00Z",
                    "sources": [source_id],
                    "project_slug": "alpha-project",
                },
                "[[wiki/infra/alpha.md]]\n\n## Findings\n- Finding 1\n",
            ),
            encoding="utf-8",
        )

        original_file = self.root / "raw" / "sources" / source_id / "original.md"
        mutated = False
        original_brief = knowledge.project_brief

        def brief_mutating_source(vault, args):
            nonlocal mutated
            if not mutated:
                mutated = True
                original_file.write_bytes(b"Mutated source bytes\n")
            return original_brief(vault, args)

        with mock.patch.object(knowledge, "project_brief", side_effect=brief_mutating_source):
            started = knowledge.begin_task(self.root, self.begin_args())

        # The revision race is detected first; the retry also rejects the now
        # inconsistent source package because its original digest was not updated.
        self.assertEqual(started["status"], "FAILED")
        self.assertEqual(len(started["execution"]["attempts"]), 2)
        self.assertEqual(started["execution"]["attempts"][0]["failure_code"], "REVISION_CHANGED")
        self.assertEqual(started["failure_code"], "LINT_FAILED")

    def test_negative_related_candidate_page_mutation_triggers_revision_changed(self):
        """Mutating a page of a returned related candidate triggers REVISION_CHANGED."""
        # Add related candidate 'alphaproject' (compact key matches 'alpha-project')
        related_page = self.root / "wiki" / "projects" / "alphaproject.md"
        related_page.write_text(
            frontmatter({
                "title": "Alpha project related",
                "type": "project",
                "status": "reviewed",
                "summary": "Related candidate page",
                "last_verified": "2026-10-01T00:00:00Z",
                "sources": [],
                "project_slug": "alphaproject",
            }),
            encoding="utf-8",
        )

        mutated = False
        original_brief = knowledge.project_brief

        def brief_mutating_related(vault, args):
            nonlocal mutated
            if not mutated:
                mutated = True
                related_page.write_text(
                    related_page.read_text(encoding="utf-8") + "\nMutated related content\n",
                    encoding="utf-8",
                )
            return original_brief(vault, args)

        with mock.patch.object(knowledge, "project_brief", side_effect=brief_mutating_related):
            started = knowledge.begin_task(self.root, self.begin_args())

        self.assertEqual(started["status"], "COMPLETE")
        self.assertEqual(len(started["execution"]["attempts"]), 2)
        self.assertEqual(started["execution"]["attempts"][0]["failure_code"], "REVISION_CHANGED")

    def test_negative_registry_alias_mutation_triggers_revision_changed(self):
        """Mutating wiki/registry.md during read triggers REVISION_CHANGED."""
        registry_file = self.write_registry({
            "schema_version": 1,
            "navigation_page": "wiki/index.md",
            "families": [],
            "projects": {
                "alpha-project": {
                    "title": "Alpha Project",
                    "family": "alpha",
                    "main_page": "wiki/projects/alpha-project.md",
                    "latest_page": None,
                    "aliases": ["alpha-alias"],
                    "related": [],
                    "folders": [],
                    "current_state_source": None,
                }
            },
            "aliases": {
                "alpha-alias": "alpha-project",
            },
        })

        mutated = False
        original_brief = knowledge.project_brief

        def brief_mutating_registry(vault, args):
            nonlocal mutated
            if not mutated:
                mutated = True
                # Add another alias to registry
                registry_file.write_text(
                    registry_file.read_text(encoding="utf-8").replace('"alpha-alias": "alpha-project"',
                                                                     '"alpha-alias": "alpha-project", "alpha-two": "alpha-project"'),
                    encoding="utf-8",
                )
            return original_brief(vault, args)

        with mock.patch.object(knowledge, "project_brief", side_effect=brief_mutating_registry):
            started = knowledge.begin_task(self.root, self.begin_args())

        self.assertEqual(started["status"], "COMPLETE")
        self.assertEqual(len(started["execution"]["attempts"]), 2)
        self.assertEqual(started["execution"]["attempts"][0]["failure_code"], "REVISION_CHANGED")

    def test_negative_vault_config_mutation_triggers_revision_changed(self):
        """Mutating knowledge-vault.json during read triggers REVISION_CHANGED."""
        config_file = self.root / "knowledge-vault.json"
        mutated = False
        original_brief = knowledge.project_brief

        def brief_mutating_config(vault, args):
            nonlocal mutated
            if not mutated:
                mutated = True
                config_file.write_text(json.dumps({"curator_machine": "TEST-MODIFIED", "curation_scope": "designated-machine-only"}), encoding="utf-8")
            return original_brief(vault, args)

        with mock.patch.object(knowledge, "project_brief", side_effect=brief_mutating_config):
            started = knowledge.begin_task(self.root, self.begin_args())

        self.assertEqual(started["status"], "COMPLETE")
        self.assertEqual(len(started["execution"]["attempts"]), 2)
        self.assertEqual(started["execution"]["attempts"][0]["failure_code"], "REVISION_CHANGED")

    def test_negative_curator_lock_appearance_triggers_curator_lock(self):
        """Curator lock appearing during read fails closed with CURATOR_LOCK."""
        lock_file = self.root / "governance" / "curator.lock"
        original_brief = knowledge.project_brief

        def brief_creating_lock(vault, args):
            result = original_brief(vault, args)
            lock_file.write_text("{}", encoding="utf-8")
            return result

        with mock.patch.object(knowledge, "project_brief", side_effect=brief_creating_lock):
            started = knowledge.begin_task(self.root, self.begin_args(lock_wait_seconds=0))

        self.assertEqual(started["status"], "FAILED")
        self.assertEqual(started["execution"]["attempts"][-1]["failure_code"], "CURATOR_LOCK")

    def test_source_hashes_contains_only_referenced_sources_with_correct_hashes(self):
        """source_hashes contains only sources referenced by the project, not unrelated raw bodies."""
        source1_id, source1_digest = self.add_source(101, project_slug="alpha-project")
        # Add unrelated raw sources
        self.add_source(102, project_slug="beta-project")
        self.add_source(103, project_slug="gamma-project")

        # Reference source 101 in alpha-project.md
        page = self.root / "wiki" / "projects" / "alpha-project.md"
        page.write_text(
            frontmatter(
                {
                    "title": "Alpha project",
                    "type": "project",
                    "status": "reviewed",
                    "summary": "Alpha context with source",
                    "last_verified": "2026-10-01T00:00:00Z",
                    "sources": [source1_id],
                    "project_slug": "alpha-project",
                },
                "## Findings\n- Finding with source\n",
            ),
            encoding="utf-8",
        )

        started = knowledge.begin_task(self.root, self.begin_args())
        self.assertEqual(started["status"], "COMPLETE")
        self.assertIn(source1_id, started["source_hashes"])
        self.assertEqual(started["source_hashes"][source1_id], source1_digest)
        # Ensure unrelated raw sources are NOT in source_hashes
        self.assertEqual(len(started["source_hashes"]), 1)

    def test_public_begin_task_and_verify_context_cli_end_to_end(self):
        """CLI end-to-end: begin-task creates receipt, verify-context confirms freshness, mutations trigger stale."""
        res_begin, begin_payload = self.run_gateway(
            "begin-task",
            "--agent", "codex",
            "--session-id", "test-session-id",
            "--project-slug", "alpha-project",
            "--require-context",
            "--receipt-root", str(self.receipts),
        )
        self.assertEqual(res_begin.returncode, 0, res_begin.stderr)
        self.assertEqual(begin_payload["status"], "COMPLETE")
        receipt_path = begin_payload["receipt_path"]
        self.assertTrue(Path(receipt_path).is_file())

        # verify-context succeeds initially
        res_verify, verify_payload = self.run_gateway(
            "verify-context",
            "--context-receipt", receipt_path,
            "--agent", "codex",
            "--session-id", "test-session-id",
            "--project-slug", "alpha-project",
        )
        self.assertEqual(res_verify.returncode, 0, res_verify.stderr)
        self.assertTrue(verify_payload["valid"])
        self.assertEqual(verify_payload["reason_code"], "OK")

        # Unrelated event append does not stale context
        (self.root / "events" / "evt-unrelated-post.json").write_text("{}", encoding="utf-8")
        _, verify_after_unrelated = self.run_gateway(
            "verify-context",
            "--context-receipt", receipt_path,
            "--agent", "codex",
            "--session-id", "test-session-id",
            "--project-slug", "alpha-project",
        )
        self.assertTrue(verify_after_unrelated["valid"])
        self.assertEqual(verify_after_unrelated["reason_code"], "OK")

        # Mutating own project page stales context
        page = self.root / "wiki" / "projects" / "alpha-project.md"
        page.write_text(page.read_text(encoding="utf-8") + "\nPost-read change\n", encoding="utf-8")
        _, verify_after_mutation = self.run_gateway(
            "verify-context",
            "--context-receipt", receipt_path,
            "--agent", "codex",
            "--session-id", "test-session-id",
            "--project-slug", "alpha-project",
        )
        self.assertFalse(verify_after_mutation["valid"])
        self.assertEqual(verify_after_mutation["reason_code"], "CONTEXT_STALE")

    def test_ambiguous_project_reference_fails_safely(self):
        """Ambiguous project slug fails safely without unhandled exception."""
        # Create two projects matching same compact key
        (self.root / "wiki" / "projects" / "alpha-one.md").write_text(
            frontmatter({
                "title": "Alpha 1",
                "type": "project",
                "status": "reviewed",
                "summary": "Alpha One project page",
                "last_verified": "2026-10-01T00:00:00Z",
                "sources": [],
                "project_slug": "alpha-one",
            }),
            encoding="utf-8",
        )
        (self.root / "wiki" / "projects" / "alphaone.md").write_text(
            frontmatter({
                "title": "Alpha One",
                "type": "project",
                "status": "reviewed",
                "summary": "AlphaOne project page",
                "last_verified": "2026-10-01T00:00:00Z",
                "sources": [],
                "project_slug": "alphaone",
            }),
            encoding="utf-8",
        )
        # Querying by ambiguous ref 'alpha_one' matches both
        started = knowledge.begin_task(self.root, self.begin_args(project_slug="alpha_one"))
        self.assertEqual(started["status"], "FAILED")
        self.assertEqual(started["failure_code"], "AMBIGUOUS_PROJECT_REFERENCE")

    def test_safe_behavior_on_unreadable_file(self):
        """Unreadable file during snapshot fails closed with READ_FAILED."""
        with mock.patch.object(knowledge, "read_regular_file_snapshot", side_effect=OSError("Permission denied")):
            started = knowledge.begin_task(self.root, self.begin_args())
        self.assertEqual(started["status"], "FAILED")
        self.assertEqual(started["failure_code"], "READ_FAILED")

    def test_beta_alias_related_candidate_race_during_read_retries_revision_changed(self):
        """Mutating a distinct related candidate (betaalias) of an alias (beta-alias) during read triggers REVISION_CHANGED and retries."""
        self.write_registry({
            "schema_version": 1,
            "navigation_page": "wiki/index.md",
            "families": [],
            "projects": {
                "alpha-project": {
                    "title": "Alpha Project",
                    "family": "alpha",
                    "main_page": "wiki/projects/alpha-project.md",
                    "latest_page": None,
                    "aliases": ["beta-alias"],
                    "related": [],
                    "folders": [],
                    "current_state_source": None,
                }
            },
            "aliases": {
                "beta-alias": "alpha-project",
            },
        })
        betaalias_page = self.root / "wiki" / "projects" / "betaalias.md"
        betaalias_page.write_text(
            frontmatter({
                "title": "Beta Alias Candidate",
                "type": "project",
                "status": "reviewed",
                "summary": "Original betaalias summary",
                "last_verified": "2026-10-01T00:00:00Z",
                "sources": [],
                "project_slug": "betaalias",
            }),
            encoding="utf-8",
        )
        mutated = False
        original_brief = knowledge.project_brief

        def brief_mutating_betaalias(vault, args):
            nonlocal mutated
            if not mutated:
                mutated = True
                betaalias_page.write_text(
                    betaalias_page.read_text(encoding="utf-8") + "\nMutated betaalias content\n",
                    encoding="utf-8",
                )
            return original_brief(vault, args)

        with mock.patch.object(knowledge, "project_brief", side_effect=brief_mutating_betaalias):
            started = knowledge.begin_task(self.root, self.begin_args(project_slug="beta-alias"))

        self.assertEqual(started["status"], "COMPLETE")
        self.assertEqual(len(started["execution"]["attempts"]), 2)
        self.assertEqual(started["execution"]["attempts"][0]["failure_code"], "REVISION_CHANGED")
        self.assertTrue(started["coherent_read"])

    def test_beta_alias_and_canonical_share_same_fingerprint_and_verify_stably(self):
        """Equivalent aliases share the exact same canonical context fingerprint and verify stably."""
        self.write_registry({
            "schema_version": 1,
            "navigation_page": "wiki/index.md",
            "families": [],
            "projects": {
                "alpha-project": {
                    "title": "Alpha Project",
                    "family": "alpha",
                    "main_page": "wiki/projects/alpha-project.md",
                    "latest_page": None,
                    "aliases": ["beta-alias"],
                    "related": [],
                    "folders": [],
                    "current_state_source": None,
                }
            },
            "aliases": {
                "beta-alias": "alpha-project",
            },
        })
        (self.root / "wiki" / "projects" / "betaalias.md").write_text(
            frontmatter({
                "title": "Beta Alias Candidate",
                "type": "project",
                "status": "reviewed",
                "summary": "Betaalias summary",
                "last_verified": "2026-10-01T00:00:00Z",
                "sources": [],
                "project_slug": "betaalias",
            }),
            encoding="utf-8",
        )
        started_alias = knowledge.begin_task(self.root, self.begin_args(project_slug="beta-alias", session_id="session-alias"))
        self.assertEqual(started_alias["status"], "COMPLETE")

        started_canonical = knowledge.begin_task(self.root, self.begin_args(project_slug="alpha-project", session_id="session-canonical"))
        self.assertEqual(started_canonical["status"], "COMPLETE")

        # Canonical fingerprint invariance across equivalent aliases
        self.assertEqual(started_alias["context_fingerprint"], started_canonical["context_fingerprint"])

        # Stable verify_context across both alias and canonical query
        verify_alias = knowledge.verify_context(self.root, argparse.Namespace(
            context_receipt=started_alias["receipt_path"],
            agent="codex",
            session_id="session-alias",
            project_slug="beta-alias",
        ))
        self.assertTrue(verify_alias["valid"])
        self.assertEqual(verify_alias["reason_code"], "OK")

        verify_canon = knowledge.verify_context(self.root, argparse.Namespace(
            context_receipt=started_alias["receipt_path"],
            agent="codex",
            session_id="session-alias",
            project_slug="alpha-project",
        ))
        self.assertTrue(verify_canon["valid"])
        self.assertEqual(verify_canon["reason_code"], "OK")

    def test_beta_alias_related_candidate_after_receipt_mutation_invalidates_context(self):
        """Mutating betaalias page after receipt stales verify-context even when queried through beta-alias."""
        self.write_registry({
            "schema_version": 1,
            "navigation_page": "wiki/index.md",
            "families": [],
            "projects": {
                "alpha-project": {
                    "title": "Alpha Project",
                    "family": "alpha",
                    "main_page": "wiki/projects/alpha-project.md",
                    "latest_page": None,
                    "aliases": ["beta-alias"],
                    "related": [],
                    "folders": [],
                    "current_state_source": None,
                }
            },
            "aliases": {
                "beta-alias": "alpha-project",
            },
        })
        betaalias_page = self.root / "wiki" / "projects" / "betaalias.md"
        betaalias_page.write_text(
            frontmatter({
                "title": "Beta Alias Candidate",
                "type": "project",
                "status": "reviewed",
                "summary": "Original betaalias summary",
                "last_verified": "2026-10-01T00:00:00Z",
                "sources": [],
                "project_slug": "betaalias",
            }),
            encoding="utf-8",
        )
        started = knowledge.begin_task(self.root, self.begin_args(project_slug="beta-alias"))
        self.assertEqual(started["status"], "COMPLETE")

        # Initial verify is valid
        init_verify = self.verify_payload(started, project_slug="beta-alias")
        self.assertTrue(init_verify["valid"])
        self.assertEqual(init_verify["reason_code"], "OK")

        # Mutate betaalias page after read
        betaalias_page.write_text(
            betaalias_page.read_text(encoding="utf-8") + "\nMutated after receipt\n",
            encoding="utf-8",
        )

        # verify_context must now fail with CONTEXT_STALE
        stale_verify = self.verify_payload(started, project_slug="beta-alias")
        self.assertFalse(stale_verify["valid"])
        self.assertEqual(stale_verify["reason_code"], "CONTEXT_STALE")

        # verify_context querying canonical slug must also fail with CONTEXT_STALE
        stale_canon = self.verify_payload(started, project_slug="alpha-project")
        self.assertFalse(stale_canon["valid"])
        self.assertEqual(stale_canon["reason_code"], "CONTEXT_STALE")

    def test_beta_alias_related_candidate_referenced_source_mutation_invalidates_context(self):
        """Mutating a source referenced by betaalias invalidates context for beta-alias."""
        self.write_registry({
            "schema_version": 1,
            "navigation_page": "wiki/index.md",
            "families": [],
            "projects": {
                "alpha-project": {
                    "title": "Alpha Project",
                    "family": "alpha",
                    "main_page": "wiki/projects/alpha-project.md",
                    "latest_page": None,
                    "aliases": ["beta-alias"],
                    "related": [],
                    "folders": [],
                    "current_state_source": None,
                }
            },
            "aliases": {
                "beta-alias": "alpha-project",
            },
        })
        source_id, _ = self.add_source(201, project_slug="betaalias")
        (self.root / "wiki" / "projects" / "betaalias.md").write_text(
            frontmatter({
                "title": "Beta Alias Candidate",
                "type": "project",
                "status": "reviewed",
                "summary": "Betaalias summary with source",
                "last_verified": "2026-10-01T00:00:00Z",
                "sources": [source_id],
                "project_slug": "betaalias",
            }),
            encoding="utf-8",
        )
        started = knowledge.begin_task(self.root, self.begin_args(project_slug="beta-alias"))
        self.assertEqual(started["status"], "COMPLETE")
        self.assertIn(source_id, started["source_hashes"])

        # Mutate the source original file
        original_file = self.root / "raw" / "sources" / source_id / "original.md"
        original_file.write_bytes(b"Mutated source original content\n")

        # verify_context must now fail with CONTEXT_STALE
        stale_verify = self.verify_payload(started, project_slug="beta-alias")
        self.assertFalse(stale_verify["valid"])
        self.assertEqual(stale_verify["reason_code"], "CONTEXT_STALE")


    def test_many_alias_candidates_guard_same_sorted_pages_as_brief(self):
        """Set iteration order must not select different pages from the public brief."""
        self.write_registry({
            "schema_version": 1, "navigation_page": "wiki/index.md", "families": [],
            "projects": {"alpha-project": {
                "title": "Alpha", "family": "alpha",
                "main_page": "wiki/projects/alpha-project.md", "latest_page": None,
                "aliases": ["beta-alias"], "related": [], "folders": [],
                "current_state_source": None,
            }}, "aliases": {"beta-alias": "alpha-project"},
        })
        names = ["b-etaalias", "be-taalias", "bet-aalias", "betaalias",
                 "betaa-lias", "betaal-ias", "betaali-as", "betaalia-s",
                 "b-et-aalias", "b-e-taalias", "be-t-aalias", "bet-a-alias"]
        for name in names:
            (self.root / "wiki" / "projects" / (name + ".md")).write_text(
                frontmatter({"title": name, "type": "project", "status": "reviewed",
                             "summary": "OLD " + name, "last_verified": "2026-10-01T00:00:00Z",
                             "sources": [], "project_slug": name}), encoding="utf-8")

        class ReverseIterationSet(set):
            def __iter__(self):
                return iter(sorted(super().__iter__(), reverse=True))

        slugs = ReverseIterationSet(names + ["alpha-project", "beta-alias"])
        original_brief = knowledge.project_brief
        with mock.patch.object(knowledge, "known_project_slugs", return_value=slugs):
            initial = original_brief(self.root, self.begin_args(project_slug="beta-alias"))
            returned = [Path(page["path"]) for group in initial["related_context"]
                        for page in group["pages"]]
            guarded = set(knowledge.project_context_paths(self.root, "alpha-project", "registry-alias"))
            self.assertTrue(returned)
            self.assertTrue(set(returned).issubset(guarded))
            target = returned[0]
            mutated = False

            def raced(vault, args):
                nonlocal mutated
                result = original_brief(vault, args)
                if not mutated:
                    mutated = True
                    target.write_text(target.read_text(encoding="utf-8").replace("OLD ", "NEW "),
                                      encoding="utf-8")
                return result

            with mock.patch.object(knowledge, "project_brief", side_effect=raced):
                started = knowledge.begin_task(self.root, self.begin_args(project_slug="beta-alias"))
            self.assertEqual(started["status"], "COMPLETE")
            self.assertEqual(len(started["execution"]["attempts"]), 2)
            self.assertEqual(started["execution"]["attempts"][0]["failure_code"], "REVISION_CHANGED")
            self.assertTrue(self.verify_payload(started, project_slug="beta-alias")["valid"])


    def critical_bundle(self):
        source_id, _ = self.add_source(401)
        proposal_id = "prop-20261001T120000000Z-00000004"
        (self.root / "proposals" / "codex" / (proposal_id + "-critical.md")).write_text(
            frontmatter({"id": proposal_id, "type": "proposal", "status": "proposed",
                         "title": "Synthetic critical alpha", "agent": "codex",
                         "created_at": "2026-10-01T12:00:00Z", "classification": "internal",
                         "knowledge_kind": "critical-state", "project_slug": "alpha-project",
                         "state_summary": "Synthetic current project state", "source_ids": [source_id],
                         "target_pages": ["wiki/projects/alpha-project.md"]}), encoding="utf-8")

        def write_event(number, action):
            event_id = f"evt-20261001T120000000Z-{number:08x}"
            path = self.root / "events" / (event_id + ".json")
            path.write_text(json.dumps({"event_id": event_id, "agent": "codex", "action": action,
                                       "occurred_at": "2026-10-01T12:00:00Z", "source_ids": [source_id],
                                       "proposal_ids": [proposal_id] if action == "new-proposal" else [],
                                       "pages_changed": [], "summary": "Synthetic receipt event"}), encoding="utf-8")
            return path

        stage = write_event(1, "stage-source")
        proposal = write_event(2, "new-proposal")
        self.assertEqual(knowledge.lint(self.root)["error_count"], 0)
        return stage, proposal, write_event

    def test_matching_critical_event_race_rejects_old_complete_handoff(self):
        _, _, write_event = self.critical_bundle()
        original = knowledge.project_brief
        changed = False

        def raced(vault, args):
            nonlocal changed
            result = original(vault, args)
            if not changed:
                changed = True
                write_event(3, "new-proposal")
            return result

        with mock.patch.object(knowledge, "project_brief", side_effect=raced):
            started = knowledge.begin_task(self.root, self.begin_args())
        self.assertEqual(started["execution"]["attempts"][0]["failure_code"], "REVISION_CHANGED")
        self.assertEqual(started["status"], "FAILED")
        self.assertEqual(started["failure_code"], "LINT_FAILED")
        self.assertIsNone(started["context"])
        self.assertFalse(self.verify_payload(started)["valid"])

    def test_each_matching_handoff_event_mutation_invalidates_receipt(self):
        stage, proposal, _ = self.critical_bundle()
        for path in (stage, proposal):
            with self.subTest(event=path.name):
                started = knowledge.begin_task(self.root, self.begin_args())
                self.assertEqual(started["status"], "COMPLETE")
                event = json.loads(path.read_text(encoding="utf-8"))
                event["source_ids"] = []
                path.write_text(json.dumps(event), encoding="utf-8")
                try:
                    verified = self.verify_payload(started)
                    self.assertFalse(verified["valid"])
                    self.assertEqual(verified["reason_code"], "CONTEXT_STALE")
                finally:
                    event["source_ids"] = [self.add_source(401)[0]]
                    path.write_text(json.dumps(event), encoding="utf-8")

    def test_own_draft_navigation_addition_retries_and_removal_stales(self):
        draft = self.root / "wiki" / "projects" / "alpha-draft.md"
        original = knowledge.project_brief
        changed = False

        def raced(vault, args):
            nonlocal changed
            result = original(vault, args)
            if not changed:
                changed = True
                draft.write_text(frontmatter({"title": "Own draft", "type": "project", "status": "draft",
                                               "summary": "Synthetic draft", "last_verified": "2026-10-01T00:00:00Z",
                                               "sources": [], "project_slug": "alpha-project"}), encoding="utf-8")
            return result

        with mock.patch.object(knowledge, "project_brief", side_effect=raced):
            started = knowledge.begin_task(self.root, self.begin_args())
        self.assertEqual(started["status"], "COMPLETE")
        self.assertEqual(len(started["execution"]["attempts"]), 2)
        self.assertIn("wiki/projects/alpha-draft.md", started["context"]["pages_newest_first"])
        self.assertTrue(self.verify_payload(started)["valid"])
        # This file is a disposable fixture created by this test.
        draft.unlink()
        self.assertFalse(self.verify_payload(started)["valid"])

    def test_registered_unlabelled_draft_navigation_is_guarded(self):
        self.write_registry({"schema_version": 1, "navigation_page": "wiki/index.md", "families": [],
                             "projects": {"alpha-project": {"title": "Alpha", "family": "alpha",
                                           "main_page": "wiki/infra/registered-draft.md", "latest_page": None,
                                           "aliases": [], "related": [], "folders": [], "current_state_source": None}},
                             "aliases": {}})
        page = self.root / "wiki" / "infra" / "registered-draft.md"
        original = knowledge.project_brief
        changed = False

        def raced(vault, args):
            nonlocal changed
            result = original(vault, args)
            if not changed:
                changed = True
                page.write_text(frontmatter({"title": "Registered draft", "type": "system", "status": "draft",
                                              "summary": "Synthetic navigation", "last_verified": "2026-10-01T00:00:00Z",
                                              "sources": []}), encoding="utf-8")
            return result

        with mock.patch.object(knowledge, "project_brief", side_effect=raced):
            started = knowledge.begin_task(self.root, self.begin_args())
        self.assertEqual(started["status"], "COMPLETE")
        self.assertEqual(len(started["execution"]["attempts"]), 2)
        self.assertIn("wiki/infra/registered-draft.md", started["context"]["pages_newest_first"])
        page.write_text(page.read_text(encoding="utf-8").replace("Registered draft", "Updated draft"), encoding="utf-8")
        self.assertFalse(self.verify_payload(started)["valid"])


if __name__ == "__main__":
    unittest.main()
