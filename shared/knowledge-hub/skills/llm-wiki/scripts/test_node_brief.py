import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
GATEWAY = HERE / "knowledge.py"
sys.path.insert(0, str(HERE))
import knowledge  # noqa: E402

SESSION = "node-brief-parent-session"


def frontmatter(fields, body=""):
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {json.dumps(value)}")
    lines.extend(["---", body])
    return "\n".join(lines) + "\n"


class NodeBriefTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "vault"
        for part in ("wiki/projects", "wiki/infra", "proposals/codex", "events", "raw/sources", "governance"):
            (self.root / part).mkdir(parents=True)
        (self.root / "knowledge-vault.json").write_text(
            json.dumps({"curator_machine": "TEST", "curation_scope": "designated-machine-only"}), encoding="utf-8"
        )
        self.page("projects/alpha-project.md", "alpha-project", status="reviewed",
                  body="## Findings\n- Modul coordfs/wake.py ma termin zlecen relay\n- Frontend nie ma testow e2e\n"
                       "## Open questions\n- Czy relay dziala po restarcie?\n")
        self.page("projects/beta-project.md", "beta-project", status="reviewed",
                  body="## Findings\n- Tajny fakt innego projektu o wake.py\n")
        self.receipts = Path(self.temp.name) / "receipts"
        self.receipt = self.fresh_receipt()

    def fresh_receipt(self, project="alpha-project"):
        started = knowledge.begin_task(self.root, argparse.Namespace(
            agent="codex", session_id=SESSION, project_slug=project, require_context=False,
            receipt_root=str(self.receipts), limit=10))
        self.assertEqual(started["status"], "COMPLETE", started)
        return started["receipt_path"]

    def tearDown(self):
        self.temp.cleanup()

    def page(self, relative, project, status="reviewed", body="", **fields):
        metadata = {"title": relative, "type": "project", "status": status, "summary": f"Podsumowanie {project}",
                    "last_verified": "2026-09-20T00:00:00Z", "sources": [], "project_slug": project, **fields}
        path = self.root / "wiki" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frontmatter(metadata, body), encoding="utf-8")

    def run_brief(self, *extra, receipt=True, project="alpha-project", agent="codex", session=SESSION):
        args = [sys.executable, "-B", str(GATEWAY), "node-brief", "--vault", str(self.root),
                "--project-slug", project, "--node-id", "node-1", "--agent", agent, "--session-id", session, *extra]
        if receipt:
            args += ["--context-receipt", receipt if isinstance(receipt, str) else self.receipt]
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", check=False,
                                env={**os.environ, "KNOWLEDGE_READ_TELEMETRY": "0"})
        payload = json.loads(result.stdout) if result.stdout.strip().startswith("{") else {}
        return result, payload

    def test_packet_schema_and_bounded_size(self):
        result, packet = self.run_brief("--scope", "shared/agent-office/coordfs/wake.py", "--query", "relay termin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(packet), {"schema_version", "project_slug", "node_id", "query", "generated_at",
                                       "context_receipt", "continuity_status", "facts", "pending", "warnings",
                                       "truncated"})
        self.assertEqual((packet["schema_version"], packet["project_slug"], packet["node_id"]),
                         (1, "alpha-project", "node-1"))
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 4096)
        self.assertNotIn("\n  ", result.stdout)
        for fact in packet["facts"]:
            self.assertEqual(set(fact), {"text", "source_ref", "status", "observed_at"})
            self.assertIn(fact["status"], {"reviewed", "verified", "producer-attested"})

    def test_scope_selects_relevant_facts_and_never_crosses_projects(self):
        _, packet = self.run_brief("--scope", "shared/agent-office/coordfs/wake.py")
        texts = [fact["text"] for fact in packet["facts"]]
        self.assertTrue(any("wake.py" in text for text in texts))
        self.assertFalse(any("Tajny fakt" in text for text in texts))
        self.assertFalse(any("e2e" in text for text in texts))
        self.assertTrue(all(fact["source_ref"] == "wiki/projects/alpha-project.md" for fact in packet["facts"]))

    def test_unsafe_scope_and_missing_receipt_are_refused(self):
        for bad in ("C:/Windows/system32", "../etc/passwd", "shared/*.py", "\\\\server\\share"):
            result, _ = self.run_brief("--scope", bad)
            self.assertEqual(result.returncode, 2, bad)
        result, _ = self.run_brief(receipt=False)
        self.assertNotEqual(result.returncode, 0)

    def test_truncation_drops_whole_entries_and_says_so(self):
        for i in range(30):
            self.page(f"infra/alpha-{i:02d}.md", "alpha-project",
                      body=f"## Findings\n- Fakt numer {i} o wake.py " + "x" * 250 + "\n",
                      summary=f"Strona {i} o wake.py " + "y" * 250)
        result, packet = self.run_brief("--scope", "wake.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(packet["truncated"])
        self.assertIn("truncated_to_4096_bytes", packet["warnings"])
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 4096)
        self.assertGreater(len(packet["facts"]), 0)

    def test_pending_always_requires_live_verification(self):
        proposal = self.root / "proposals" / "codex" / "prop-20260925T000000000Z-0000abcd-x.md"
        proposal.write_text(frontmatter({
            "id": "prop-20260925T000000000Z-0000abcd", "type": "proposal", "status": "proposed",
            "title": "Relay po restarcie", "agent": "codex", "created_at": "2026-09-25T00:00:00Z",
            "classification": "internal", "source_ids": [], "target_pages": ["wiki/projects/alpha-project.md"],
            "state_summary": "Relay wake.py przeszedl test restartu",
        }, "tresc propozycji"), encoding="utf-8")
        _, packet = self.run_brief("--scope", "wake.py")
        self.assertTrue(packet["pending"])
        self.assertTrue(all(item["live_verification_required"] is True for item in packet["pending"]))
        self.assertNotIn("tresc propozycji", json.dumps(packet, ensure_ascii=False))

    def test_receipt_must_belong_to_the_caller_project_and_session(self):
        fake = Path(self.temp.name) / "fake.json"
        fake.write_text("{}", encoding="utf-8")
        self.assertEqual(self.run_brief(receipt=str(fake))[0].returncode, 2)
        self.assertEqual(self.run_brief(session="other-session")[0].returncode, 2)
        self.assertEqual(self.run_brief(agent="claude")[0].returncode, 2)
        beta = self.fresh_receipt("beta-project")
        self.assertEqual(self.run_brief(receipt=beta)[0].returncode, 2)

    def test_newest_pending_survives_truncation_and_facts_are_capped_per_source(self):
        for i in range(12):
            self.page(f"infra/alpha-{i:02d}.md", "alpha-project",
                      body="## Findings\n" + "".join(f"- Fakt {i}.{j} o wake.py " + "x" * 120 + "\n" for j in range(6)))
        for day in range(1, 7):
            proposal = self.root / "proposals" / "codex" / f"prop-202609{day:02d}T000000000Z-0000abc{day}-x.md"
            proposal.write_text(frontmatter({
                "id": f"prop-202609{day:02d}T000000000Z-0000abc{day}", "type": "proposal", "status": "proposed",
                "title": f"Stan {day}", "agent": "codex", "created_at": f"2026-09-{day:02d}T00:00:00Z",
                "classification": "internal", "source_ids": [], "target_pages": ["wiki/projects/alpha-project.md"],
                "state_summary": f"Stan wake.py dnia {day} " + "z" * 150,
            }, "tresc"), encoding="utf-8")
        result, packet = self.run_brief("--scope", "wake.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 4096)
        self.assertTrue(any(w.startswith("context_receipt_stale") for w in packet["warnings"]))
        self.assertTrue(packet["pending"])
        self.assertIn("prop-20260906T000000000Z-0000abc6", [p["source_ref"] for p in packet["pending"]])
        per_source = {}
        for fact in packet["facts"]:
            per_source[fact["source_ref"]] = per_source.get(fact["source_ref"], 0) + 1
        self.assertLessEqual(max(per_source.values()), 3)

    def test_unknown_project_is_explicitly_missing(self):
        result, packet = self.run_brief(project="gamma-project", receipt=self.fresh_receipt("gamma-project"))
        self.assertEqual(packet["continuity_status"], "MISSING")
        self.assertIn("no_matching_knowledge", packet["warnings"])
        self.assertEqual(packet["facts"], [])


if __name__ == "__main__":
    unittest.main()
