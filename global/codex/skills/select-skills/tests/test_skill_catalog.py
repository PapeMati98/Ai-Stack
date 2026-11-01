import builtins
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import skill_catalog  # noqa: E402


def write_skill(root: Path, folder: str, frontmatter: str, body: str = "sekret body\n") -> Path:
    path = root / folder / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter + body, encoding="utf-8")
    return path


class SkillCatalogTests(unittest.TestCase):
    def test_dedupe_identical_name_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fm = "---\nname: alpha\ndescription: raport\n---\n"
            first = root / "a"
            second = root / "b"
            write_skill(first, "alpha", fm)
            write_skill(second, "alpha", fm)
            inventory = skill_catalog.build_inventory([first, second])
            self.assertEqual(len(inventory["skills"]), 1)
            self.assertEqual(inventory["conflicts"], [])

    def test_conflict_same_name_different_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "a", "---\nname: alpha\ndescription: one\n---\n")
            write_skill(root, "b", "---\nname: alpha\ndescription: two\n---\n")
            inventory = skill_catalog.build_inventory([root])
            self.assertEqual(len(inventory["skills"]), 2)
            self.assertEqual(inventory["conflicts"][0]["name"], "alpha")
            self.assertEqual(skill_catalog.shortlist(inventory["skills"], "one", inventory["conflicts"]), [])

    def test_frontmatter_multiline_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "alpha", "---\nname: alpha\ndescription: opis\naliases:\n  - faktury\n  - korekty\n---\n")
            record = skill_catalog.build_inventory([root])["skills"][0]
            self.assertEqual(record["metadata"]["aliases"], ["faktury", "korekty"])

    def test_frontmatter_multiline_block_description(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "alpha", "---\nname: alpha\ndescription: |\n  pierwsza linia\n  druga linia\n---\n")
            record = skill_catalog.build_inventory([root])["skills"][0]
            self.assertEqual(record["description"], "pierwsza linia\ndruga linia")

    def test_shortlist_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "z", "---\nname: zeta\ndescription: faktury raport\n---\n")
            write_skill(root, "a", "---\nname: alfa\ndescription: faktury raport\n---\n")
            first = skill_catalog.build_inventory([root], "raport faktury")["shortlist"]
            second = skill_catalog.build_inventory([root], "raport faktury")["shortlist"]
            self.assertEqual(first, second)
            self.assertEqual([item["name"] for item in first], ["alfa", "zeta"])

    def test_shortlist_normalizes_diacritics_and_inflection_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "pl-tax-compliance", "---\nname: pl-tax-compliance\ndescription: faktury i korekty podatkowe\n---\n")
            result = skill_catalog.build_inventory([root], "Sprawdz fakture korygujaca podatnika")["shortlist"]
            self.assertEqual(result[0]["name"], "pl-tax-compliance")

    def test_does_not_read_body_after_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_skill(root, "alpha", "---\nname: alpha\ndescription: ok\n---\n", "BODY_MUST_NOT_BE_READ\n")
            original_open = builtins.open
            class Guard:
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    self._stream.close()
                    return False
                def readline(self):
                    line = self._stream.readline()
                    if line == b"BODY_MUST_NOT_BE_READ\n":
                        raise AssertionError("body was read")
                    return line
                def __getattr__(self, name):
                    return getattr(self._stream, name)
            def guarded_open(file, mode="r", *args, **kwargs):
                stream = original_open(file, mode, *args, **kwargs)
                if Path(file).resolve() == path.resolve():
                    guard = Guard()
                    guard._stream = stream
                    return guard
                return stream
            with patch("pathlib.Path.open", lambda self, mode="r", *a, **kw: guarded_open(self, mode, *a, **kw)):
                skill_catalog.build_inventory([root])

    def test_bad_path_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            inventory = skill_catalog.build_inventory([missing])
            self.assertEqual(inventory["skills"], [])
            self.assertEqual(inventory["errors"][0]["error"], "root_not_found")

    def test_shortlist_only_is_compact(self):
        inventory = {
            "schema_version": 1,
            "shortlist": [{"name": "alpha"}],
            "conflicts": [{"name": "beta", "paths": ["very-long-path"]}],
            "errors": [],
        }
        compact = skill_catalog.compact_result(inventory)
        self.assertEqual(compact["conflicted_names"], ["beta"])
        self.assertTrue(compact["semantic_selection_required"])
        self.assertEqual(compact["max_final_skills"], 4)
        self.assertNotIn("very-long-path", str(compact))

    def test_candidate_routes_ai_infrastructure_audit(self):
        skills_root = Path(__file__).resolve().parents[2]
        result = skill_catalog.build_inventory(
            [skills_root], "Oceń bezpieczeństwo nowego MCP z pamięcią RAG"
        )["shortlist"]
        self.assertIn("ai-infrastructure-audit", {item["name"] for item in result})

    def test_candidate_routes_market_research(self):
        skills_root = Path(__file__).resolve().parents[2]
        result = skill_catalog.build_inventory(
            [skills_root], "Zbadaj strategię rynkową i backtest bez handlu"
        )["shortlist"]
        self.assertIn("market-strategy-research", {item["name"] for item in result})

    def test_candidate_pool_keeps_wordpress_and_security_audit(self):
        skills_root = Path(__file__).resolve().parents[2]
        result = skill_catalog.build_inventory(
            [skills_root],
            "Zbuduj bezpieczny plugin WordPress z REST API i przygotuj audyt przed wdrozeniem",
        )["shortlist"]
        names = {item["name"] for item in result}
        self.assertLessEqual(len(result), 8)
        self.assertIn("wordpress-engineering", names)
        self.assertIn("security-audit", names)


class RoutingCorpusTests(unittest.TestCase):
    """Run the original routing corpus against the real discovered catalog.

    Skills that live in managed releases (see skills-ownership.json) and are
    absent from the scanned origin tree are loaded from their real release
    payload resolved through the ownership manifest, so a changed release
    description changes what the corpus exercises instead of drifting away
    from a hand-written copy. `expect_all`, `expect_any`, `forbid` and
    `expect_empty` always apply.
    """

    @classmethod
    def setUpClass(cls):
        cls.skill_dir = Path(__file__).resolve().parents[1]
        cls.skills_root = Path(__file__).resolve().parents[2]
        cls.inventory = skill_catalog.build_inventory([cls.skills_root])
        cls.catalog_names = {str(skill["name"]) for skill in cls.inventory["skills"]}
        cls.corpus_path = cls.skill_dir / "references" / "routing-corpus.json"
        cls.corpus = json.loads(cls.corpus_path.read_text(encoding="utf-8"))
        cls.managed_records = cls._load_managed_release_skills()
        cls.managed_names = set(cls.managed_records)
        extras = [record for name, record in cls.managed_records.items() if name not in cls.catalog_names]
        cls.merged_skills = [*cls.inventory["skills"], *extras]
        cls.merged_names = cls.catalog_names | cls.managed_names

    @classmethod
    def _load_managed_release_skills(cls) -> dict[str, dict[str, object]]:
        """Resolve declared managed skills to their real release frontmatter.

        The ownership manifest pins each managed skill to an installer
        contract with a `release_root`, so a version bump changes which file
        the corpus exercises without any test edit. When several copies of a
        SKILL.md exist in one release (payload, adapters, platform), the
        payload wins over adapters and both win over anything else; ties
        break lexicographically, keeping the choice deterministic.
        """
        repo_root = Path(__file__).resolve().parents[5]
        ownership_path = repo_root / "global" / "codex" / "skills-ownership.json"
        if not ownership_path.is_file():
            # Runtime copies can run this suite from the source repository.
            # Keep the same real ownership manifest and release frontmatter.
            repo_root = Path.cwd()
            ownership_path = repo_root / "global" / "codex" / "skills-ownership.json"
        records: dict[str, dict[str, object]] = {}
        if not ownership_path.is_file():
            return records
        ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
        managers = {str(entry["name"]): str(entry.get("managed_by", "")) for entry in ownership.get("external_managed", [])}
        contracts = ownership.get("manager_contracts", {})
        for name in cls.corpus.get("managed_release_skills", []):
            contract = contracts.get(managers.get(name, ""), {})
            release_root = str(contract.get("release_root", ""))
            if not release_root:
                continue
            relative = release_root.replace("%REPO_ROOT%", "").replace("\\", "/").strip("/")
            root = repo_root / relative
            if not root.is_dir():
                continue
            candidates = sorted(
                root.glob(f"**/{name}/SKILL.md"),
                key=lambda p: (
                    0 if "payload" in p.parts else 1 if "adapters" in p.parts else 2,
                    p.as_posix().casefold(),
                ),
            )
            if not candidates:
                continue
            frontmatter, digest = skill_catalog.read_frontmatter(candidates[0])
            records[name] = {
                "name": name,
                "description": str(frontmatter.get("description", "")),
                "metadata": {key: frontmatter[key] for key in skill_catalog.FRONTMATTER_FIELDS if key in frontmatter},
                "path": str(candidates[0].resolve()),
                "frontmatter_hash": digest,
            }
        return records

    def _run_case(self, case: dict) -> list[str]:
        result = skill_catalog.shortlist(
            self.merged_skills, case["task"], self.inventory["conflicts"]
        )
        self.assertLessEqual(len(result), skill_catalog.CANDIDATE_LIMIT)
        return [str(item["name"]) for item in result]

    def test_corpus_has_at_least_six_cases(self):
        cases = self.corpus["cases"]
        self.assertGreaterEqual(len(cases), 6)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))

    def test_corpus_references_only_known_skills(self):
        known = self.merged_names
        referenced = set(self.corpus.get("orchestration_skills", []))
        for case in self.corpus["cases"]:
            referenced.update(case.get("expect_all", []))
            referenced.update(case.get("expect_any", []))
            referenced.update(case.get("forbid", []))
        unknown = referenced - known
        self.assertFalse(unknown, f"corpus references unknown skills: {sorted(unknown)}")
        missing_managed = set(self.corpus.get("managed_release_skills", [])) - self.managed_names
        self.assertFalse(
            missing_managed,
            f"declared managed skills not resolvable from skills-ownership.json releases: {sorted(missing_managed)}",
        )

    def test_managed_skills_come_from_real_releases(self):
        for name in self.corpus.get("managed_release_skills", []):
            with self.subTest(skill=name):
                record = self.managed_records.get(name)
                if record is None:
                    self.fail(f"{name} declared as managed but no release record was resolved")
                path = Path(str(record["path"]))
                self.assertNotIn("routing-fixtures", path.parts)
                self.assertTrue(
                    "releases" in path.parts or "shared" in path.parts,
                    f"{name} not loaded from a managed release tree: {path}",
                )

    def test_managed_skills_absent_from_origin_are_still_exercised(self):
        orchestration = set(self.corpus.get("orchestration_skills", []))
        for name in self.corpus.get("managed_release_skills", []):
            with self.subTest(skill=name):
                if name in self.catalog_names:
                    self.skipTest(f"{name} present in the scanned catalog, managed release not needed")
                exercised = name in orchestration or any(
                    name in case.get("expect_all", [])
                    or name in case.get("expect_any", [])
                    or name in case.get("forbid", [])
                    for case in self.corpus["cases"]
                )
                self.assertTrue(exercised, f"managed skill {name} is never exercised by any case")

    def test_corpus_cases_route_expected_skills(self):
        exercised = 0
        for case in self.corpus["cases"]:
            with self.subTest(case=case["id"]):
                names = self._run_case(case)
                if case.get("expect_empty"):
                    self.assertEqual(names, [])
                for name in case.get("expect_all", []):
                    self.assertIn(name, names)
                expected_any = case.get("expect_any", [])
                if expected_any:
                    self.assertTrue(
                        set(expected_any) & set(names),
                        f"none of {expected_any} in pool {names}",
                    )
                for name in case.get("forbid", []):
                    self.assertNotIn(name, names)
                if case.get("expect_all") or expected_any or case.get("expect_empty"):
                    exercised += 1
        self.assertGreaterEqual(exercised, 6)

    def test_simple_prompts_do_not_surface_orchestration(self):
        orchestration = set(self.corpus.get("orchestration_skills", []))
        for task in (
            "Pokaż mi status zadania",
            "Pokaż status projektu",
            "What is the current task status?",
            "What is the project status?",
        ):
            with self.subTest(task=task):
                names = set(self._run_case({"task": task}))
                self.assertFalse(names & orchestration, names & orchestration)

    def test_real_catalog_shortlist_is_deterministic(self):
        task = "Oceń wpływ zmiany schematu danych na pozostałe moduły systemu"
        first = skill_catalog.build_inventory([self.skills_root], task)["shortlist"]
        second = skill_catalog.build_inventory([self.skills_root], task)["shortlist"]
        self.assertEqual(first, second)

    def test_synonyms_score_below_exact_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_skill(root, "a", "---\nname: alfa\ndescription: obsluga mockup ekranu\n---\n")
            write_skill(root, "b", "---\nname: beta\ndescription: obsluga frontend dashboard\n---\n")
            result = skill_catalog.build_inventory([root], "popraw mockup ekranu")["shortlist"]
            self.assertEqual(result[0]["name"], "alfa")
            self.assertEqual({item["name"] for item in result}, {"alfa", "beta"})


if __name__ == "__main__":
    unittest.main()
