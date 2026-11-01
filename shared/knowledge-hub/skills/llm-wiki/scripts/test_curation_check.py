"""Regression tests for curation_check.py.

Every case below is a defect that a real independent review round found in a
real candidate on 2026-09-21. A checker that does not reproduce these failures
would be decoration, so each test injects one historical defect and asserts the
exact check that must fire.

Each case uses a fresh, deterministic synthetic candidate.
"""
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from synthetic_curation_fixture import make_candidate

HERE = Path(__file__).resolve().parent
CHECKER = HERE / "curation_check.py"
class CurationCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=os.environ.get("TEAM_TEST_ROOT"))
        self.addCleanup(self.temp.cleanup)
        self.dir = make_candidate(Path(self.temp.name) / "candidate")

    # --- helpers -----------------------------------------------------------
    def page_path(self) -> Path:
        manifest = json.loads((self.dir / "publication.json").read_text(encoding="utf-8"))
        row = next(r for r in manifest["outputs"] if r["path"].startswith("wiki/history/"))
        return self.dir / row["payload_path"]

    def edit_page(self, old: str, new: str, count: int = 1):
        path = self.page_path()
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, "fixture drifted: pattern absent from the page")
        path.write_text(text.replace(old, new, count), encoding="utf-8")

    def edit_manifest(self, mutate):
        path = self.dir / "publication.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def edit_ledger(self, mutate):
        manifest = json.loads((self.dir / "publication.json").read_text(encoding="utf-8"))
        path = self.dir / manifest["ledger_path"]
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def run_checker(self) -> dict:
        result = subprocess.run(
            [sys.executable, "-B", str(CHECKER), "--candidate", str(self.dir)],
            capture_output=True, text=True, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=60)
        self.assertTrue(result.stdout.strip().startswith("{"), result.stderr[:400])
        return json.loads(result.stdout)

    def assert_catches(self, check_name: str):
        report = self.run_checker()
        self.assertEqual(report["status"], "FAIL", "checker accepted a known defect")
        self.assertIn(check_name, [f["check"] for f in report["findings"]],
                      f"expected {check_name}; got {[f['check'] for f in report['findings']]}")

    # --- baseline ----------------------------------------------------------
    def test_unmodified_candidate_passes(self):
        report = self.run_checker()
        self.assertEqual(report["status"], "PASS", report["findings"])
        self.assertGreaterEqual(report["checks_passed"], 20)

    # --- defects found by review round 1 -----------------------------------
    def test_catches_synthesized_without_a_project_page(self):
        """Round 1: 129 of 152 entries were called `synthesized` although the
        transaction never touched wiki/projects, where a synthesis would live."""
        self.edit_ledger(lambda d: d["proposals"][0].update({"disposition": "synthesized"}))
        self.assert_catches("ledger.synthesized_requires_project_page")

    def test_catches_false_no_duplicates_claim(self):
        """Round 1: the page asserted 'Byte-level duplicates: none' while three
        sources shared one sha256."""
        self.edit_page("Byte-identical notes: 1 group", "Byte-level duplicates: none")
        self.assert_catches("page.duplicate_claim")

    def test_catches_wrong_duplicate_group_count(self):
        self.edit_page("Byte-identical notes: 1 group", "Byte-identical notes: 4 group")
        self.assert_catches("page.duplicate_group_count")

    def test_catches_unflagged_open_blockers(self):
        """Round 1: entries saying INCIDENT OPEN / NOT FIXED were not unresolved."""
        def demote(data):
            for row in data["proposals"]:
                if row["disposition"] == "unresolved":
                    row["disposition"] = "historical"
        self.edit_ledger(demote)
        self.assert_catches("ledger.open_blockers_flagged")

    # --- defects found by review round 5 -----------------------------------
    def test_catches_restated_count_that_drifted(self):
        """Round 5: '23 automated wrappers' in one bullet, 'all 24 cases' three
        lines below, inside the same paragraph."""
        text = self.page_path().read_text(encoding="utf-8")
        current = int(re.search(r"In all (\d+) cases", text).group(1))
        self.edit_page(f"In all {current} cases", f"In all {current + 1} cases")
        self.assert_catches("page.wrapper_count_restated")

    def test_catches_wrong_content_span(self):
        """Round 5: the span claimed ...to 2026-09-21 while the last admitted
        entry was 2026-09-20; the two 09-21 entries were the removed ones."""
        text = self.page_path().read_text(encoding="utf-8")
        span = re.search(r"- \d+ entries, (\d{4}-\d\d-\d\d) to (\d{4}-\d\d-\d\d)\.", text)
        self.edit_page(f"to {span.group(2)}.", "to 2026-09-30.")
        self.assert_catches("page.span_composition")

    def test_catches_stale_count_in_manifest_summary(self):
        """Round 5: manifest summary said 'the 152 pending' at 150 proposals."""
        self.edit_manifest(lambda d: d.update(
            {"summary": "Account for the 999 pending agent-stack handoffs."}))
        self.assert_catches("manifest.summary_count")

    # --- defects found by review round 6 -----------------------------------
    def test_catches_run_date_regression(self):
        """Round 6: fixing the content span blindly also rewrote the curation run
        date, which must follow the ledger capture, not the span."""
        text = self.page_path().read_text(encoding="utf-8")
        run = re.search(r"(\d{4}-\d\d-\d\d) curation run", text).group(1)
        self.edit_page(f"{run} curation run", "2026-09-20 curation run"
                       if run != "2026-09-20" else "2026-09-19 curation run")
        self.assert_catches("page.run_date")

    def test_catches_live_count_without_measurement_time(self):
        """Round 6: a bare '3 proposals arrived after that cut' was already 4
        while the review was still running."""
        text = self.page_path().read_text(encoding="utf-8")
        measured = re.search(r"Measured at \S+?,\s*\n?\s*AT LEAST \d+", text)
        self.edit_page(measured.group(0), "AT LEAST 3")
        self.assert_catches("page.delta_is_timestamped")

    # --- structural defects ------------------------------------------------
    def test_catches_inventory_entry_count_mismatch(self):
        self.edit_manifest(lambda d: d["proposal_ids"].pop())
        report = self.run_checker()
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("page.entry_count", [f["check"] for f in report["findings"]])

    def test_catches_sources_frontmatter_drift(self):
        """The missing `sources:` field blocked curation.py validate only after
        four review rounds had already run."""
        text = self.page_path().read_text(encoding="utf-8")
        line = re.search(r"^sources: \[.*\]$", text, re.M).group(0)
        self.edit_page(line, 'sources: ["src-not-in-this-manifest"]')
        self.assert_catches("page.sources_match_manifest")

    def test_catches_disposition_sum_mismatch(self):
        self.edit_ledger(lambda d: d["proposals"].pop())
        report = self.run_checker()
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue({"ledger.counts_match", "ledger.dispositions_sum"}
                        & {f["check"] for f in report["findings"]})


    # --- defects found by the parallel wave on 2026-09-21 --------------------
    def test_catches_identifiers_from_another_project(self):
        """Hardcoded prose about one project's duplicate group leaked verbatim into
        three other projects' pages, asserting "0 group" over a description of
        proposals absent from those manifests. The count matched, so a count-only
        check passed it."""
        self.edit_page("## Composition",
                       "## Composition\n\n- Stray reference to `aabbccdd` and to "
                       "`prop-20190101T000000000Z-deadbeef` from another project.")
        self.assert_catches("page.no_foreign_ids")

    def test_catches_unresolved_without_a_verifiable_quote(self):
        """unresolved is keyword-detected, so the ledger must quote the phrase it
        matched and the quote has to exist in the frozen proposal."""
        def strip_quote(data):
            for row in data["proposals"]:
                if row["disposition"] == "unresolved":
                    row["reason"] = "Reports an open blocker."
                    return
            raise AssertionError("fixture has no unresolved row")
        self.edit_ledger(strip_quote)
        self.assert_catches("ledger.unresolved_quotes_verifiable")

    def test_catches_fabricated_unresolved_quote(self):
        def fake_quote(data):
            for row in data["proposals"]:
                if row["disposition"] == "unresolved":
                    row["reason"] = 'Open blocker - quoted: "THIS PHRASE IS NOT IN THE ENTRY".'
                    return
            raise AssertionError("fixture has no unresolved row")
        self.edit_ledger(fake_quote)
        self.assert_catches("ledger.unresolved_quotes_verifiable")

    def test_catches_missing_completeness_disclaimer(self):
        """Pattern detection may never be presented as proof that nothing is open."""
        self.edit_page("not a proof of", "a complete account of")
        self.edit_page('never as "none"', "and it is exhaustive")
        self.assert_catches("page.unresolved_completeness_disclaimed")


class OpenBlockerDetectorTests(unittest.TestCase):
    """The detector itself, independent of any live candidate.

    2026-09-22: a case-sensitive-only rule silently dropped real blockers written
    in lower case, including an entry whose summary begins "Incident unresolved."
    Case-insensitivity must come back for words that cannot be product names,
    while a bare `open` must stay case-sensitive (OpenClaw, OpenCode).
    """

    def setUp(self):
        sys.path.insert(0, str(HERE))
        from curation_check import open_blocker_match
        self.match = open_blocker_match

    def test_real_lower_case_blockers_are_detected(self):
        for text in ("Incident unresolved. Fixture 99 records preserved.",
                     "Fixture local backend corrected; UI acceptance blocked",
                     "TASK-FIXTURE full I/O and independent acceptance remain open",
                     "Status: DIAGNOSED, NOT REPAIRED.",
                     "Nowy incydent FIXTURE 13.09 - osobny, nadal otwarty",
                     "live six tickets four fixed two open"):
            with self.subTest(text=text):
                self.assertTrue(self.match(text), f"missed a real blocker: {text!r}")

    def test_blocker_stated_only_in_the_cited_note_is_seen(self):
        """A wrapper proposal carries boilerplate; the substance is in its note."""
        from curation_check import entry_detection_text
        sid = "src-20260913T000000001Z-fixture-cited-note-a0000001"
        other = "src-20260913T000000002Z-fixture-uncited-note-b0000002"
        wrapper = f"---\nid: prop-x\nsources:\n  - {sid}\n---\nCaretaker wrapper, see note."
        notes = {sid: "## Stan\nStatus: DIAGNOSED, NOT REPAIRED.",
                 other: "Incident unresolved."}
        self.assertFalse(self.match(wrapper), "fixture must not match on the wrapper alone")
        text = entry_detection_text(wrapper, notes)
        self.assertTrue(self.match(text), "blocker in the cited note was not seen")
        self.assertNotIn("Incident unresolved", text, "an uncited note leaked into the entry")

    def test_product_names_and_negations_are_not_blockers(self):
        for text in ("OpenClaw caretaker restarted cleanly",
                     "OpenCode and open source tooling updated",
                     "access was unblocked after the fix",
                     "a non-blocking advisory finding",
                     "firewall keeps port 443 open and 80 open"):
            with self.subTest(text=text):
                self.assertFalse(self.match(text), f"false positive: {text!r}")


if __name__ == "__main__":
    unittest.main()
