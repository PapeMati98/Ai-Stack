"""Compact catalog, cache, and folded-scalar tests for skill_catalog.py."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import skill_catalog  # noqa: E402


def write_skill(root: Path, folder: str, frontmatter: str, body: str = "body\n") -> Path:
    path = root / folder / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter + body, encoding="utf-8")
    return path


class FoldedScalarTests(unittest.TestCase):
    def test_folded_strip_joins_with_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "a", "---\nname: a\ndescription: >-\n  pierwsza linia\n  druga linia\n---\n")
            record = skill_catalog.build_inventory([root])["skills"][0]
            self.assertEqual(record["description"], "pierwsza linia druga linia")

    def test_literal_strip_keeps_newlines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "a", "---\nname: a\ndescription: |-\n  pierwsza linia\n  druga linia\n---\n")
            record = skill_catalog.build_inventory([root])["skills"][0]
            self.assertEqual(record["description"], "pierwsza linia\ndruga linia")

    def test_folded_without_chomping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "a", "---\nname: a\ndescription: >\n  linia jeden\n  linia dwa\n---\n")
            record = skill_catalog.build_inventory([root])["skills"][0]
            self.assertEqual(record["description"], "linia jeden linia dwa")
            self.assertNotIn(">-", record["description"])
            self.assertNotIn(">", record["description"][:1])

    def test_multiline_double_quoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "a", '---\nname: a\ndescription: "pierwsza\n  druga"\n---\n')
            record = skill_catalog.build_inventory([root])["skills"][0]
            self.assertEqual(record["description"], "pierwsza druga")

    def test_comment_lines_and_inline_comment_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fm = "---\nname: a\n# komentarz pelnej linii\ndescription: opis # uwaga\n---\n"
            write_skill(root, "a", fm)
            record = skill_catalog.build_inventory([root])["skills"][0]
            self.assertEqual(record["description"], "opis")

    def test_bom_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "a" / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xef\xbb\xbf---\nname: a\ndescription: >-\n  linia jeden\n  linia dwa\n---\nbody\n")
            record = skill_catalog.build_inventory([root])["skills"][0]
            self.assertEqual(record["description"], "linia jeden linia dwa")


class CompactOutputTests(unittest.TestCase):
    def _many_skills(self, root: Path, count: int = 12) -> None:
        for index in range(count):
            write_skill(root, f"s{index:02d}",
                        f"---\nname: skill-{index:02d}\ndescription: unikalny opis domenowy numer {index}\n---\n")

    def test_compact_keeps_full_inventory_beyond_top8(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._many_skills(root)
            inventory = skill_catalog.build_inventory([root], "wspolny opis domenowy")
            self.assertGreater(len(inventory["skills"]), 8)
            compact = skill_catalog.compact_full_result(inventory)
            self.assertEqual(compact["skill_count"], len(inventory["skills"]))
            self.assertEqual(len(compact["skills"]), len(inventory["skills"]))
            for item in compact["skills"]:
                self.assertIn("name", item)
                self.assertIn("description", item)
                self.assertIn("path", item)
            self.assertLessEqual(len(compact["shortlist"]), 8)
            self.assertTrue(compact["semantic_selection_required"])
            # Full details retained, not reduced to the lexical shortlist.
            self.assertIn("conflicts", compact)
            self.assertIn("errors", compact)
            short_names = {item["name"] for item in compact["shortlist"]}
            all_names = {item["name"] for item in compact["skills"]}
            self.assertTrue(short_names <= all_names)
            self.assertGreater(len(all_names), len(short_names))

    def test_compact_keeps_conflict_and_error_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "a", "---\nname: alpha\ndescription: one\n---\n")
            write_skill(root, "b", "---\nname: alpha\ndescription: two\n---\n")
            missing = Path(tmp) / "missing"
            inventory = skill_catalog.build_inventory([root, missing], "one")
            compact = skill_catalog.compact_full_result(inventory)
            self.assertEqual(compact["conflicted_names"], ["alpha"])
            self.assertTrue(any("paths" in item for item in compact["conflicts"]))
            self.assertTrue(any(item["error"] == "root_not_found" for item in compact["errors"]))

    def test_shortlist_only_still_backwards_compatible(self):
        inventory = {
            "schema_version": 1,
            "shortlist": [{"name": "alpha"}],
            "conflicts": [{"name": "beta", "paths": ["very-long-path"]}],
            "errors": [],
        }
        compact = skill_catalog.compact_result(inventory)
        self.assertTrue(compact["semantic_selection_required"])
        self.assertNotIn("very-long-path", str(compact))

    def test_cli_compact_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._many_skills(root, 10)
            code = skill_catalog.main(["--root", str(root), "--task", "opis domenowy", "--compact"])
            self.assertEqual(code, 0)


class CacheTests(unittest.TestCase):
    def _skills(self, root: Path) -> None:
        write_skill(root, "a", "---\nname: alfa\ndescription: faktury raport\n---\n")
        write_skill(root, "b", "---\nname: beta\ndescription: grafika ekran\n---\n")

    def test_cache_reuses_unchanged_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            self._skills(root)
            cache = Path(tmp) / "cache.json"
            first = skill_catalog.build_inventory([root], "faktury raport xyzzyq", cache_file=cache)
            self.assertTrue(cache.is_file())
            self.assertTrue(first["shortlist"])
            payload = json.loads(cache.read_text(encoding="utf-8"))
            self.assertNotIn("xyzzyq", json.dumps(payload, ensure_ascii=False))
            reads = {"count": 0}
            original = skill_catalog.read_frontmatter

            def counting(path, *args, **kwargs):
                reads["count"] += 1
                return original(path, *args, **kwargs)

            skill_catalog.read_frontmatter = counting  # type: ignore[method-assign]
            try:
                second = skill_catalog.build_inventory([root], "grafika ekran", cache_file=cache)
            finally:
                skill_catalog.read_frontmatter = original  # type: ignore[method-assign]
            self.assertEqual(reads["count"], 2)
            self.assertEqual(first["skills"], second["skills"])
            # Each new task still recomputes ranking.
            self.assertNotEqual(first["shortlist"], second["shortlist"])

    def test_valid_cache_with_forged_frontmatter_is_refreshed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            self._skills(root)
            # Keep a real duplicate-name conflict so a forged cached name cannot hide it.
            write_skill(root, "b", "---\nname: alfa\ndescription: grafika ekran\n---\n")
            cache = Path(tmp) / "cache.json"
            skill_catalog.build_inventory([root], cache_file=cache)

            payload = json.loads(cache.read_text(encoding="utf-8"))
            forged_key = str((root / "a" / "SKILL.md").resolve())
            payload["entries"][forged_key]["frontmatter"].update(
                name="ukryty-skill", description="podrobiony opis")
            cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            inventory = skill_catalog.build_inventory([root], cache_file=cache)
            self.assertEqual({skill["name"] for skill in inventory["skills"]}, {"alfa"})
            self.assertEqual(
                {skill["description"] for skill in inventory["skills"]},
                {"faktury raport", "grafika ekran"},
            )
            self.assertEqual(inventory["conflicts"][0]["name"], "alfa")
            refreshed = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(refreshed["entries"][forged_key]["frontmatter"]["name"], "alfa")
            self.assertEqual(refreshed["entries"][forged_key]["frontmatter"]["description"], "faktury raport")

    def test_same_size_edit_with_restored_timestamp_refreshes_metadata(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'skills'; self._skills(root); cache=Path(tmp)/'cache.json'
            skill_catalog.build_inventory([root], cache_file=cache)
            path=root/'a'/'SKILL.md'; before=path.stat()
            text=path.read_text().replace('faktury','zmienny')
            path.write_text(text); os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
            result=skill_catalog.build_inventory([root],cache_file=cache)
            self.assertIn('zmienny raport',str(result['skills']))

    def test_cache_invalidates_on_modification_addition_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            self._skills(root)
            cache = Path(tmp) / "cache.json"
            skill_catalog.build_inventory([root], cache_file=cache)
            # Modify one skill.
            write_skill(root, "a", "---\nname: alfa\ndescription: calkiem nowy opis platnosci\n---\n")
            modified = skill_catalog.build_inventory([root], cache_file=cache)
            self.assertIn("calkiem nowy opis platnosci", str(modified["skills"]))
            # Add one skill.
            write_skill(root, "c", "---\nname: gamma\ndescription: nowy skill\n---\n")
            added = skill_catalog.build_inventory([root], cache_file=cache)
            self.assertIn("gamma", {s["name"] for s in added["skills"]})
            # Remove one skill.
            (root / "c" / "SKILL.md").unlink()
            removed = skill_catalog.build_inventory([root], cache_file=cache)
            self.assertNotIn("gamma", {s["name"] for s in removed["skills"]})

    def test_corrupt_cache_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            self._skills(root)
            cache = Path(tmp) / "cache.json"
            cache.write_text("to nie jest poprawny json {{{", encoding="utf-8")
            inventory = skill_catalog.build_inventory([root], "faktury", cache_file=cache)
            self.assertEqual(len(inventory["skills"]), 2)
            self.assertTrue(inventory["shortlist"])

    def test_root_change_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_root = Path(tmp) / "first"
            second_root = Path(tmp) / "second"
            self._skills(first_root)
            self._skills(second_root)
            cache = Path(tmp) / "cache.json"
            skill_catalog.build_inventory([first_root], cache_file=cache)
            payload_before = cache.read_text(encoding="utf-8")
            inventory = skill_catalog.build_inventory([second_root], cache_file=cache)
            self.assertTrue(all(str(second_root) in str(s["path"]) for s in inventory["skills"]))
            payload_after = cache.read_text(encoding="utf-8")
            self.assertNotEqual(payload_before, payload_after)

    def test_all_runtimes_binding_is_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            self._skills(root)
            cache = Path(tmp) / "cache.json"
            skill_catalog.build_inventory([root], cache_file=cache, all_runtimes=False)
            self.assertIsNone(skill_catalog.load_cache_file(cache, [root], True))
            self.assertIsNotNone(skill_catalog.load_cache_file(cache, [root], False))

    def test_cache_stores_no_task_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            self._skills(root)
            cache = Path(tmp) / "cache.json"
            secret_task = "sekretne zadanie hasełko alfa beta gamma 12345"
            skill_catalog.build_inventory([root], secret_task, cache_file=cache)
            raw = cache.read_text(encoding="utf-8")
            self.assertNotIn(secret_task, raw)
            self.assertNotIn("hasełko", raw)


if __name__ == "__main__":
    unittest.main()
