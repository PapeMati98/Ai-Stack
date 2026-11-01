"""The public CLI owns read-scope lifetime; writes remain outside it."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import knowledge


def frontmatter(fields, body=""):
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(value)}]")
        else:
            lines.append(f"{key}: {json.dumps(value) if isinstance(value, str) else value}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


class CliInvocationReadScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        for relative in (
            "wiki/projects", "proposals/codex", "raw/sources", "events", "governance",
        ):
            (self.vault / relative).mkdir(parents=True)
        (self.vault / "knowledge-vault.json").write_text(
            json.dumps({"curator_machine": "TEST", "curation_scope": "designated-machine-only"}),
            encoding="utf-8",
        )
        self.page = self.vault / "wiki" / "projects" / "alpha-project.md"
        self.write_page("Alpha stable summary", "Stable body.\n")
        self.write_registry()
        (self.vault / "proposals" / "codex" / "pending.md").write_text(frontmatter({
            "id": "prop-20261001T000000000Z-0000abcd",
            "type": "proposal",
            "status": "proposed",
            "title": "Pending synthetic item",
            "agent": "codex",
            "created_at": "2026-10-01T00:00:00Z",
            "classification": "internal",
            "source_ids": [],
            "target_pages": ["wiki/projects/alpha-project.md"],
            "project_slug": "alpha-project",
            "knowledge_kind": "general",
        }, "Synthetic pending body.\n"), encoding="utf-8")
        self.receipts = self.root / "receipts"
        self.receipts.mkdir()

    def write_page(self, summary, body):
        self.page.write_text(frontmatter({
            "title": "Alpha",
            "type": "project",
            "status": "reviewed",
            "summary": summary,
            "last_verified": "2026-10-01T00:00:00Z",
            "sources": [],
            "project_slug": "alpha-project",
        }, body), encoding="utf-8")

    def write_registry(self):
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
                    "aliases": ["alpha-alias"],
                    "related": [],
                    "folders": [],
                    "current_state_source": None,
                }
            },
            "aliases": {"alpha-alias": "alpha-project"},
        }
        (self.vault / "wiki" / "registry.md").write_text(frontmatter({
            "title": "Project registry",
            "type": "system",
            "status": "reviewed",
            "summary": "Navigation",
            "last_verified": "2026-10-01T00:00:00Z",
            "sources": [],
        }, "```json\n" + json.dumps(registry) + "\n```\n"), encoding="utf-8")

    def invoke(self, argv):
        output = io.StringIO()
        with mock.patch.object(knowledge.sys, "argv", ["knowledge.py", *argv]):
            with contextlib.redirect_stdout(output):
                code = knowledge.main()
        return code, output.getvalue()

    def begin_task(self, session_id):
        return self.invoke([
            "begin-task", "--vault", str(self.vault), "--agent", "codex",
            "--session-id", session_id, "--project-slug", "alpha-alias",
            "--receipt-root", str(self.receipts), "--lock-wait-seconds", "0",
            "--require-context",
        ])

    def test_main_owns_one_scope_per_read_and_releases_it_before_telemetry(self):
        observed_telemetry = []

        def scoped_status(vault):
            self.assertIsNotNone(knowledge._active_scope(vault))
            return {
                "curator_lock": False,
                "dirty_tree_state": "clean",
                "dirty_tree_write_blocked": False,
                "curation_health": {"status": "PASS"},
            }

        def scoped_lint(vault, *, registry_stage="F1"):
            self.assertIsNotNone(knowledge._active_scope(vault))
            return {"error_count": 0, "warning_count": 0}

        def telemetry(vault, action, args, result):
            self.assertIsNone(knowledge._active_scope(vault))
            observed_telemetry.append((action, result["context_fingerprint"]))

        with mock.patch.object(knowledge, "status", side_effect=scoped_status), \
             mock.patch.object(knowledge, "lint", side_effect=scoped_lint), \
             mock.patch.object(knowledge, "record_read_telemetry", side_effect=telemetry):
            code, output = self.begin_task("cli-scope-first")
            first = json.loads(output)
            first_report = knowledge.invocation_read_report()
            self.assertEqual(code, 0)
            self.assertEqual(first["status"], "COMPLETE")
            self.assertEqual(first_report["known_slug_builds"], 1)
            self.assertEqual(first_report["wiki_prefix_reads"], 2)
            self.assertEqual(first_report["proposal_prefix_reads"], 1)
            self.assertIsNone(knowledge._active_scope(self.vault))

            self.write_page("Fresh summary", "Stable body changed between CLI calls.\n")
            code, output = self.begin_task("cli-scope-second")
            second = json.loads(output)
            second_report = knowledge.invocation_read_report()

        self.assertEqual(code, 0)
        self.assertEqual(second["status"], "COMPLETE")
        self.assertNotEqual(first["context_fingerprint"], second["context_fingerprint"])
        self.assertEqual(second["context"]["stable_state"][0]["summary"], "Fresh summary")
        self.assertEqual(second_report["known_slug_builds"], 1)
        self.assertEqual(second_report["wiki_prefix_reads"], 2)
        self.assertEqual(second_report["proposal_prefix_reads"], 1)
        self.assertIsNone(knowledge._active_scope(self.vault))
        self.assertEqual([item[0] for item in observed_telemetry], ["begin-task", "begin-task"])

    def test_new_proposal_dispatch_has_no_read_scope_and_sees_new_source(self):
        # Build the source index through a real read-only CLI invocation first.
        with mock.patch.object(knowledge, "status", return_value={
            "curator_lock": False,
            "dirty_tree_state": "clean",
            "dirty_tree_write_blocked": False,
            "curation_health": {"status": "PASS"},
        }), mock.patch.object(knowledge, "lint", return_value={"error_count": 0, "warning_count": 0}), \
             mock.patch.object(knowledge, "record_read_telemetry"):
            code, _ = self.begin_task("cli-scope-before-write")
        self.assertEqual(code, 0)
        self.assertEqual(knowledge.invocation_read_report()["source_metadata_reads"], 0)
        self.assertIsNone(knowledge._active_scope(self.vault))

        source_id = "src-20261022T010101010Z-alpha-bbbbbbbb"
        package = self.vault / "raw" / "sources" / "2026" / "10" / source_id
        package.mkdir(parents=True)
        original = b"synthetic source bytes\n"
        (package / "original.md").write_bytes(original)
        (package / "source.json").write_text(json.dumps({
            "schema_version": 1,
            "source_id": source_id,
            "project_slug": "alpha-project",
            "original_file": "original.md",
            "sha256": hashlib.sha256(original).hexdigest(),
        }), encoding="utf-8")

        original_source_records = knowledge.source_records
        source_scope_states = []

        def fresh_source_records(vault):
            source_scope_states.append(knowledge._active_scope(vault))
            return original_source_records(vault)

        with mock.patch.object(knowledge, "source_records", side_effect=fresh_source_records), \
             mock.patch.object(knowledge, "assert_git_backlog_writable", return_value={}), \
             mock.patch.object(knowledge, "lint", return_value={"error_count": 0, "warning_count": 0}), \
             mock.patch.object(knowledge, "checkpoint_intake", return_value={"required": False, "status": "CURRENT"}):
            code, output = self.invoke([
                "new-proposal", "--vault", str(self.vault), "--title", "Fresh source proposal",
                "--agent", "codex", "--source-id", source_id,
            ])

        result = json.loads(output)
        self.assertEqual(code, 0)
        self.assertTrue(source_scope_states)
        self.assertTrue(all(scope is None for scope in source_scope_states))
        proposal_path = Path(result["proposal_path"])
        self.assertEqual(knowledge.fm_list(proposal_path.read_text(encoding="utf-8"), "source_ids"), [source_id])
        self.assertIsNone(knowledge._active_scope(self.vault))


if __name__ == "__main__":
    unittest.main()
