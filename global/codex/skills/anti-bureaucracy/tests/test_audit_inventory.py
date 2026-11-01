import contextlib
import io
import json
import os
import sys
import uuid
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import audit_inventory


class AuditInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = os.environ.get("ANTY_AUDIT_FIXTURE_ROOT")
        if not root or not Path(root).is_absolute():
            raise RuntimeError("Set ANTY_AUDIT_FIXTURE_ROOT to an absolute task-owned fixture directory.")
        cls.fixture_root = Path(root)

    def setUp(self):
        self.base = self.fixture_root / uuid.uuid4().hex / self._testMethodName
        self.base.mkdir(parents=True, exist_ok=False)

    def make_skill(self, name, text="", references=True):
        root = self.base / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "SKILL.md").write_text(text, encoding="utf-8")
        if references:
            (root / "references").mkdir(exist_ok=True)
        return root

    def write_catalog(self, entries):
        catalog = self.base / "catalog.json"
        catalog.write_text(json.dumps({"skills": entries}), encoding="utf-8")
        return catalog

    @staticmethod
    def entry(name, root):
        return {"name": name, "path": str(root)}

    def test_flags_negation_and_legacy_as_hints_without_treating_fidelity_alone_as_contract(self):
        root = self.make_skill(
            "flags",
            "Every new request must include a formal receipt and record-session checkpoint.\n"
            "Do not require every new TODO to create a formal receipt.\n"
            "This legacy flow names planner-witness and fidelity/sufficiency for historical reference.\n"
            "The word fidelity appears by itself here.\n",
        )
        report = audit_inventory.build_report(self.write_catalog([self.entry("flags", root)]))
        records = [item for item in report["files"] if item["role"] == "skill"]
        flags = records[0]["excerpt_flags"]

        universal = [flag for flag in flags if flag["type"] == "universal_trigger_candidate"]
        receipts = [flag for flag in flags if flag["type"] == "formal_receipt_candidate"]
        contracts = [flag for flag in flags if flag["type"] == "old_contract_candidate"]
        self.assertEqual([flag["line"] for flag in universal], [1, 2])
        self.assertFalse(universal[0]["negation_hint"])
        self.assertTrue(universal[1]["negation_hint"])
        self.assertTrue(any(flag["legacy_hint"] for flag in contracts))
        self.assertEqual([flag["line"] for flag in contracts], [3])
        self.assertEqual([flag["line"] for flag in receipts], [1, 2])

    def test_duplicate_obligations_are_cross_skill_only_and_short_boilerplate_is_ignored(self):
        obligation = "Every task must record a complete audit trail before any workflow can be considered finished."
        first = self.make_skill("first", f"{obligation}\n{obligation}\n" + "You must follow the instructions.\n")
        (first / "references" / "extra.md").write_text(f"- {obligation}\n", encoding="utf-8")
        second = self.make_skill("second", f"- {obligation}\n")
        report = audit_inventory.build_report(self.write_catalog([
            self.entry("first", first), self.entry("second", second), self.entry("second", second),
        ]))

        self.assertEqual(len(report["duplicate_obligations"]), 1)
        duplicate = report["duplicate_obligations"][0]
        self.assertEqual(len({location["skill_path"] for location in duplicate["locations"]}), 2)
        self.assertEqual(len(duplicate["locations"]), 4)
        self.assertNotIn("normalized_line", duplicate)
        self.assertEqual(report["summary"]["read_files"], 3)

    def test_conflicting_catalog_names_remain_candidate_sets(self):
        source = self.make_skill("source", "See beta-skill for a related procedure.\n")
        beta_one = self.make_skill("beta-one", "A separate skill.\n")
        beta_two = self.make_skill("beta-two", "Another separate skill.\n")
        report = audit_inventory.build_report(self.write_catalog([
            self.entry("source", source), self.entry("beta-skill", beta_one),
            self.entry("beta-skill", beta_two),
        ]))

        self.assertEqual(len(report["catalog_name_conflicts"]), 1)
        conflict = report["catalog_name_conflicts"][0]
        self.assertEqual(conflict["name"], "beta-skill")
        self.assertEqual(len(conflict["candidate_paths"]), 2)
        relations = [edge for edge in report["mentioned_skill_candidates"]
                     if edge["mentioned_name"] == "beta-skill"]
        self.assertEqual(len(relations), 1)
        self.assertEqual(len(relations[0]["candidate_paths"]), 2)
        self.assertEqual(relations[0]["locations"][0]["line"], 1)
        self.assertEqual(relations[0]["relationship"], "textual_mention_candidate_only")

    def test_utf8_bom_and_metadata(self):
        root = self.make_skill("bom")
        source = root / "SKILL.md"
        source.write_bytes(b"\xef\xbb\xbfFirst line.\nSecond line.\n")
        report = audit_inventory.build_report(self.write_catalog([self.entry("bom", root)]))
        record = next(item for item in report["files"] if item["role"] == "skill")
        self.assertEqual(record["status"], "read")
        self.assertEqual(record["lines"], 2)
        self.assertEqual(record["bytes"], source.stat().st_size)
        self.assertEqual(len(record["sha256"]), 64)

    def test_missing_and_oversize_inputs_produce_explicit_partial_report(self):
        missing = self.base / "missing-skill"
        root = self.make_skill("large")
        (root / "references" / "large.md").write_bytes(b"x" * (audit_inventory.MAX_FILE_BYTES + 1))
        missing_rule = self.base / "missing-rules.md"
        report = audit_inventory.build_report(
            self.write_catalog([self.entry("missing", missing), self.entry("large", root)]),
            [missing_rule],
        )
        self.assertTrue(report["summary"]["partial"])
        self.assertEqual(report["summary"]["detected_files"], 4)
        self.assertEqual(report["summary"]["read_files"], 1)
        self.assertEqual(report["summary"]["failed_files"], 3)
        self.assertEqual(report["summary"]["detected_files"],
                         report["summary"]["read_files"] + report["summary"]["failed_files"])
        self.assertEqual(report["summary"]["detected_files"], len(report["files"]))
        reasons = {item.get("failure") for item in report["files"]}
        self.assertIn("missing", reasons)
        self.assertIn("oversize", reasons)

    def test_reference_symlink_outside_skill_is_rejected(self):
        root = self.make_skill("linked", references=False)
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("private outside text", encoding="utf-8")
        try:
            (root / "references").symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks unavailable: {error.__class__.__name__}")
        report = audit_inventory.build_report(self.write_catalog([self.entry("linked", root)]))
        self.assertTrue(report["summary"]["partial"])
        self.assertFalse(any("secret.md" in item.get("path", "") for item in report["files"]))
        self.assertIn("reparse_point_rejected", {issue["reason"] for issue in report["issues"]})

    def test_source_files_are_unchanged_when_writing_explicit_report(self):
        root = self.make_skill("read-only", "Review each requested file carefully.\n")
        catalog = self.write_catalog([self.entry("read-only", root)])
        rule = self.base / "team-rules.md"
        rule.write_text("A rule document with a formal closeout receipt.\n", encoding="utf-8")
        sources = [catalog, root / "SKILL.md", rule]
        before = {path: path.read_bytes() for path in sources}
        output_dir = self.base / "reports"
        output_dir.mkdir()
        output = output_dir / "inventory.json"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = audit_inventory.main([
                "--catalog", str(catalog), "--rules", str(rule), "--output", str(output),
            ])
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertTrue(output.is_file())
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["summary"]["read_files"], 2)
        self.assertEqual(before, {path: path.read_bytes() for path in sources})
        self.assertEqual(stdout.getvalue(), "")

    def test_catalog_duplicate_path_is_read_once(self):
        root = self.make_skill("dedupe", "A single source document.\n")
        report = audit_inventory.build_report(self.write_catalog([
            self.entry("dedupe", root), self.entry("dedupe", root),
        ]))
        self.assertEqual(report["summary"]["detected_files"], 1)
        self.assertEqual(report["summary"]["read_files"], 1)

    def test_obsidian_authoring_path_is_not_mistaken_for_auth_secret(self):
        root = self.make_skill("obsidian-authoring", "A normal skill document.\n")
        report = audit_inventory.build_report(self.write_catalog([self.entry("obsidian-authoring", root)]))
        self.assertEqual(report["summary"]["read_files"], 1)
        self.assertEqual(report["summary"]["excluded_items"], 0)

    def test_relative_catalog_paths_resolve_from_catalog_and_output_cannot_overwrite_rule(self):
        root = self.make_skill("relative-skill", "A skill document.\n")
        catalog = self.write_catalog([{"name": "relative", "path": "relative-skill"}])
        rule = self.base / "rules.md"
        rule.write_text("Keep this explicit rule unchanged.\n", encoding="utf-8")
        before = rule.read_bytes()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = audit_inventory.main([
                "--catalog", str(catalog), "--rules", "rules.md", "--output", str(rule),
            ])
        self.assertEqual(result, 2)
        self.assertIn("output_overlaps_input", stderr.getvalue())
        self.assertEqual(rule.read_bytes(), before)
        self.assertEqual(root.joinpath("SKILL.md").read_text(encoding="utf-8"), "A skill document.\n")

    def test_eager_discovery_overhead_measures_frontmatter_only_and_tracks_incomplete_skills(self):
        folded = self.make_skill(
            "folded-skill",
            "\ufeff---\r\n"
            "name: folded-skill # komentarz przy nazwie\r\n"
            "description: >- # komentarz przy znaczniku bloku\r\n"
            "  Pierwsza linia opisu z polskimi znakami ąęć.\r\n"
            "  Druga linia opisu.\r\n"
            "---\r\n"
            "# Duzy korpus SKILL.md\n" + ("Treść korpusu poza frontmatterem.\n" * 50),
        )
        (folded / "references" / "heavy.md").write_text("Referencja " * 200, encoding="utf-8")

        quoted = self.make_skill(
            "quoted-skill",
            '---\nname: "quoted-skill"\ndescription: "Krótki opis skilla." # komentarz po cudzyslowie\n---\nBody text.\n',
        )
        incomplete = self.make_skill(
            "incomplete-skill",
            "---\nname: incomplete-skill\ndescription: # sam komentarz to brak wartosci\n---\nBrak pola description.\n",
        )
        no_fm = self.make_skill("no-fm", "Zwykły plik bez frontmattera YAML.\n")

        report = audit_inventory.build_report(self.write_catalog([
            self.entry("folded-skill", folded),
            self.entry("folded-skill-dup", folded),
            self.entry("quoted-skill", quoted),
            self.entry("incomplete-skill", incomplete),
            self.entry("no-fm", no_fm),
        ]))
        ego = report["summary"]["eager_discovery_overhead"]
        self.assertEqual(ego["measured_skills"], 2)
        self.assertEqual(ego["incomplete_frontmatter_skills"], 2)
        expected_desc_1 = "Pierwsza linia opisu z polskimi znakami ąęć. Druga linia opisu."
        expected_bytes = (
            len("folded-skill".encode("utf-8"))
            + len(expected_desc_1.encode("utf-8"))
            + len("quoted-skill".encode("utf-8"))
            + len("Krótki opis skilla.".encode("utf-8"))
        )
        self.assertEqual(ego["name_description_bytes"], expected_bytes)
        self.assertEqual(ego["estimated_tokens"], round(expected_bytes / 4))
        self.assertEqual(ego["estimation_formula"], "utf8_bytes_div_4_approx")
        self.assertEqual(ego["scope"], "supplied_catalog_deduplicated_skill_roots")
        self.assertEqual(ego["policy"], "passive_telemetry_not_a_gate")
        self.assertGreater(ego["frontmatter_bytes"], ego["name_description_bytes"])


if __name__ == "__main__":
    unittest.main()
