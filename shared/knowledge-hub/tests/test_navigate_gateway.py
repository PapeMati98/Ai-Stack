"""Synthetic tests for the canonical knowledge.py navigate dispatcher (I3).

All fixtures are temporary synthetic vaults. Nothing here reads the canonical
hub vault, real registry seeds, or real topic feeds.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "llm-wiki" / "scripts"
GATEWAY = SCRIPTS / "knowledge.py"
NAVIGATION = SCRIPTS / "navigation.py"

_spec = importlib.util.spec_from_file_location("knowledge", GATEWAY)
knowledge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(knowledge)


DEFAULT_REGISTRY = {
    "version": "synthetic-test",
    "projects": {
        "alpha-project": {"aliases": ["alpha"], "org_links": []},
        "beta-project": {"aliases": [], "org_links": []},
    },
    "organizations": [{"id": "acme"}],
    "topics": [{"id": "ksef", "name": "KSeF", "aliases": ["ksef"]}],
}


def make_vault(root: Path, *, with_registry=True, pages=(), registry=None) -> Path:
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "registry").mkdir(parents=True, exist_ok=True)
    (root / "feeds").mkdir(parents=True, exist_ok=True)
    (root / "knowledge-vault.json").write_text("{}", encoding="utf-8")
    if with_registry:
        payload = registry if registry is not None else DEFAULT_REGISTRY
        (root / "registry" / "registry-v2.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    for page in pages:
        path = root / "wiki" / page["rel"]
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(
            [
                "---",
                f'title: "{page["title"]}"',
                f'project_slug: {page["project_slug"]}',
                f'status: {page.get("status", "reviewed")}',
                f'summary: "{page.get("summary", "")}"',
                f'last_verified: {page.get("last_verified", "2026-10-05")}',
                "---",
                page.get("body", "tresc"),
                "",
            ]
        )
        path.write_text(body, encoding="utf-8")
    return root


def nav_namespace(**overrides) -> argparse.Namespace:
    values = {
        "action": "navigate",
        "vault": None,
        "topic": None,
        "org": None,
        "project": None,
        "person": None,
        "limit": None,
        "offset": None,
        "json": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class RecordingNav:
    """Stub I3 module: records argv and path bindings, emits caller payload."""

    def __init__(self, *, exit_code=0, payload=None, raise_exc=None, hub_root=None):
        self.exit_code = exit_code
        self.payload = payload if payload is not None else {
            "status": "OK",
            "result": "EMPTY",
            "total": 0,
            "query": {},
            "records": [],
            "warnings": [],
        }
        self.raise_exc = raise_exc
        if hub_root is not None:
            self.HUB_ROOT = Path(hub_root)
            self.VAULT = self.HUB_ROOT / "vault"
            self.REGISTRY_LIVE = self.HUB_ROOT / "registry" / "registry-v2.json"
            self.REGISTRY_SEED = self.HUB_ROOT / "registry" / "I1-registry-v2.frozen-v0.json"
            self.TOPIC_FEEDS = self.HUB_ROOT / "feeds" / "KSEF-SCOUT-CONTRACT.json"
        self.seen_argv = None
        self.seen_paths = None

    def main(self):
        self.seen_argv = list(sys.argv)
        self.seen_paths = {
            name: getattr(self, name, None)
            for name in ("VAULT", "REGISTRY_LIVE", "REGISTRY_SEED", "TOPIC_FEEDS")
        }
        if self.raise_exc is not None:
            raise self.raise_exc
        print(json.dumps(self.payload, ensure_ascii=False))
        return self.exit_code


class ParserFlagTests(unittest.TestCase):
    def test_navigate_accepts_all_i3_flags(self):
        args = knowledge.parser().parse_args([
            "navigate",
            "--topic", "ksef",
            "--org", "acme",
            "--project", "alpha-project",
            "--person", "jan-kowalski",
            "--limit", "5",
            "--offset", "2",
            "--json",
            "--vault", r"C:\synthetic\vault",
        ])
        self.assertEqual(args.action, "navigate")
        self.assertEqual(args.topic, "ksef")
        self.assertEqual(args.org, "acme")
        self.assertEqual(args.project, "alpha-project")
        self.assertEqual(args.person, "jan-kowalski")
        self.assertEqual(args.limit, 5)
        self.assertEqual(args.offset, 2)
        self.assertTrue(args.json)
        self.assertEqual(args.vault, r"C:\synthetic\vault")

    def test_limit_offset_default_to_unset_so_module_defaults_apply(self):
        args = knowledge.parser().parse_args(["navigate", "--topic", "ksef"])
        self.assertIsNone(args.limit)
        self.assertIsNone(args.offset)
        self.assertFalse(args.json)

    def test_person_is_accepted_and_not_dropped(self):
        args = knowledge.parser().parse_args(["navigate", "--person", "jan-kowalski"])
        self.assertEqual(args.person, "jan-kowalski")

    def test_json_defaults_false(self):
        args = knowledge.parser().parse_args(["navigate", "--topic", "x"])
        self.assertIs(args.json, False)


class ForwardingTests(unittest.TestCase):
    def test_forwards_every_supplied_facet_faithfully(self):
        nav = RecordingNav()
        args = nav_namespace(
            topic="ksef", org="acme", project="alpha-project",
            person="jan-kowalski", limit=5, offset=2, json=True,
        )
        out, err = io.StringIO(), io.StringIO()
        with patch.object(knowledge, "_load_navigation_module", return_value=nav):
            with redirect_stdout(out), redirect_stderr(err):
                code = knowledge.navigate(Path("C:/synthetic/vault"), args)
        self.assertEqual(code, 0)
        argv = nav.seen_argv
        self.assertEqual(argv[0], "knowledge.py navigate")
        for flag, value in (
            ("--topic", "ksef"),
            ("--org", "acme"),
            ("--project", "alpha-project"),
            ("--person", "jan-kowalski"),
            ("--limit", "5"),
            ("--offset", "2"),
        ):
            self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--json", argv)

    def test_unset_limit_offset_json_are_not_forwarded(self):
        nav = RecordingNav()
        args = nav_namespace(topic="ksef")
        with patch.object(knowledge, "_load_navigation_module", return_value=nav):
            with redirect_stdout(io.StringIO()):
                knowledge.navigate(Path("C:/synthetic/vault"), args)
        argv = nav.seen_argv
        self.assertEqual(argv, ["knowledge.py navigate", "--topic", "ksef"])
        self.assertNotIn("--limit", argv)
        self.assertNotIn("--offset", argv)
        self.assertNotIn("--json", argv)

    def test_person_is_never_silently_discarded(self):
        nav = RecordingNav(payload={
            "status": "PARTIAL",
            "result": "EMPTY",
            "total": 0,
            "query": {"person": "jan-kowalski"},
            "availability": {"unavailable": ["axis:people_not_in_registry_v0"]},
            "records": [],
            "warnings": [],
        })
        args = nav_namespace(person="jan-kowalski")
        out = io.StringIO()
        with patch.object(knowledge, "_load_navigation_module", return_value=nav):
            with redirect_stdout(out):
                code = knowledge.navigate(Path("C:/synthetic/vault"), args)
        self.assertEqual(code, 0)
        self.assertIn("--person", nav.seen_argv)
        self.assertEqual(nav.seen_argv[nav.seen_argv.index("--person") + 1], "jan-kowalski")
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["query"].get("person"), "jan-kowalski")
        self.assertIn("axis:people_not_in_registry_v0", payload["availability"]["unavailable"])

    def test_sys_argv_is_restored_after_dispatch(self):
        nav = RecordingNav()
        before = list(sys.argv)
        with patch.object(knowledge, "_load_navigation_module", return_value=nav):
            with redirect_stdout(io.StringIO()):
                knowledge.navigate(Path("C:/synthetic/vault"), nav_namespace(topic="ksef"))
        self.assertEqual(sys.argv, before)


class EmptyUnavailableAndUtf8Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_real(self, vault: Path, **flags):
        args = nav_namespace(**flags)
        out = io.StringIO()
        with patch.object(knowledge, "_load_navigation_module", side_effect=self._load_isolated_navigation):
            with redirect_stdout(out):
                code = knowledge.navigate(vault, args)
        return code, out.getvalue()

    def _load_isolated_navigation(self):
        spec = importlib.util.spec_from_file_location("knowledge_navigation_i3_under_test", NAVIGATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_utf8_result_from_synthetic_wiki_page(self):
        vault = make_vault(
            self.root / "vault",
            pages=[{
                "rel": "projekty/zolc.md",
                "title": "Zażółć gęślą jaźń",
                "project_slug": "alpha-project",
                "summary": "Strona z polskimi znakami: ąćęłńóśźż",
            }],
        )
        code, raw = self.run_real(vault, project="alpha-project")
        self.assertEqual(code, 0, raw)
        self.assertIn("Zażółć gęślą jaźń", raw)
        payload = json.loads(raw)
        self.assertEqual(payload["result"], "FOUND")
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["records"][0]["page_ref"], "wiki/projekty/zolc.md")
        self.assertIn("jaźń", payload["records"][0]["title"])

    def test_empty_result_when_registry_knows_project_but_wiki_is_empty(self):
        vault = make_vault(self.root / "empty-vault", pages=[])
        code, raw = self.run_real(vault, project="beta-project")
        self.assertEqual(code, 0, raw)
        payload = json.loads(raw)
        self.assertEqual(payload["result"], "EMPTY")
        self.assertEqual(payload["total"], 0)
        self.assertEqual(payload["records"], [])
        self.assertEqual(payload["availability"]["unavailable"], [])

    def test_unavailable_when_registry_missing(self):
        vault = make_vault(self.root / "no-registry", with_registry=False, pages=[])
        code, raw = self.run_real(vault, project="alpha-project")
        self.assertEqual(code, 0, raw)
        payload = json.loads(raw)
        self.assertEqual(payload["result"], "UNAVAILABLE")
        self.assertIn("registry:registry_unavailable", payload["availability"]["unavailable"])

    def test_person_axis_is_unavailable_not_fake_supported(self):
        vault = make_vault(
            self.root / "person-vault",
            pages=[{
                "rel": "alpha.md",
                "title": "Alpha",
                "project_slug": "alpha-project",
                "summary": "podsumowanie",
            }],
        )
        code, raw = self.run_real(vault, person="jan-kowalski")
        self.assertEqual(code, 0, raw)
        payload = json.loads(raw)
        self.assertEqual(payload["query"].get("person"), "jan-kowalski")
        self.assertIn("axis:people_not_in_registry_v0", payload["availability"]["unavailable"])

    def test_no_facet_error_propagates_as_module_exit(self):
        vault = make_vault(self.root / "facetless")
        code, raw = self.run_real(vault)
        self.assertEqual(code, 1, raw)
        payload = json.loads(raw)
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["code"], "no_facet")

    def test_unknown_project_is_a_hard_module_error(self):
        vault = make_vault(self.root / "unknown")
        code, raw = self.run_real(vault, project="nie-ma-takiego")
        self.assertEqual(code, 1, raw)
        payload = json.loads(raw)
        self.assertEqual(payload["code"], "unknown_facet_value")


class ModuleErrorAndExitTests(unittest.TestCase):
    def test_missing_module_is_structured_unavailable(self):
        out = io.StringIO()
        with patch.object(knowledge, "_load_navigation_module", return_value=None):
            with redirect_stdout(out):
                code = knowledge.navigate(Path("C:/synthetic/vault"), nav_namespace(topic="ksef"))
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["code"], "navigation_module_unavailable")
        self.assertEqual(payload["result"], "UNAVAILABLE")
        self.assertIn("module:navigation", payload["availability"]["unavailable"])
        self.assertIn("navigation.py", payload["module_path"])

    def test_module_import_failure_is_structured_error(self):
        out = io.StringIO()
        with patch.object(
            knowledge, "_load_navigation_module", side_effect=ImportError("broken navigation")
        ):
            with redirect_stdout(out):
                code = knowledge.navigate(Path("C:/synthetic/vault"), nav_namespace(topic="ksef"))
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "ERROR")
        self.assertEqual(payload["code"], "navigation_module_error")
        self.assertEqual(payload["error_type"], "ImportError")
        self.assertIn("broken navigation", payload["error"])

    def test_module_integer_exit_code_is_propagated(self):
        for exit_code in (0, 1, 2, 3, 7):
            nav = RecordingNav(exit_code=exit_code)
            with patch.object(knowledge, "_load_navigation_module", return_value=nav):
                with redirect_stdout(io.StringIO()):
                    code = knowledge.navigate(Path("C:/synthetic/vault"), nav_namespace(topic="ksef"))
            self.assertEqual(code, exit_code)

    def test_module_system_exit_is_propagated(self):
        nav = RecordingNav(raise_exc=SystemExit(5))
        with patch.object(knowledge, "_load_navigation_module", return_value=nav):
            with redirect_stdout(io.StringIO()):
                code = knowledge.navigate(Path("C:/synthetic/vault"), nav_namespace(topic="ksef"))
        self.assertEqual(code, 5)

    def test_module_runtime_error_is_structured_error(self):
        nav = RecordingNav(raise_exc=RuntimeError("topic-feed intersection defect"))
        out = io.StringIO()
        with patch.object(knowledge, "_load_navigation_module", return_value=nav):
            with redirect_stdout(out):
                code = knowledge.navigate(Path("C:/synthetic/vault"), nav_namespace(topic="ksef"))
        self.assertEqual(code, 2)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["code"], "navigation_module_error")
        self.assertEqual(payload["error_type"], "RuntimeError")
        self.assertIn("topic-feed intersection defect", payload["error"])

    def test_string_system_exit_becomes_exit_1(self):
        nav = RecordingNav(raise_exc=SystemExit("signal-string"))
        err = io.StringIO()
        with patch.object(knowledge, "_load_navigation_module", return_value=nav):
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = knowledge.navigate(Path("C:/synthetic/vault"), nav_namespace(topic="ksef"))
        self.assertEqual(code, 1)
        self.assertIn("signal-string", err.getvalue())


class VaultIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.canonical_hub = self.root / "canonical-hub"
        self.canonical_vault = self.canonical_hub / "vault"
        self.canonical_vault.mkdir(parents=True)
        self.selected = self.root / "selected-vault"

    def test_alternate_vault_rebinds_every_data_path_inside_itself(self):
        mod = RecordingNav(hub_root=self.canonical_hub)
        knowledge._bind_navigation_to_vault(mod, self.selected)
        self.assertEqual(Path(mod.VAULT), self.selected)
        for name in ("REGISTRY_LIVE", "REGISTRY_SEED", "TOPIC_FEEDS"):
            path = Path(getattr(mod, name))
            self.assertTrue(
                path.is_relative_to(self.selected),
                f"{name} escaped the selected vault: {path}",
            )
            self.assertFalse(
                str(path).startswith(str(self.canonical_hub)),
                f"{name} still points at the canonical hub: {path}",
            )

    def test_selected_vault_never_uses_real_registry_or_feed_fallbacks(self):
        make_vault(
            self.selected,
            pages=[{
                "rel": "wybrane.md",
                "title": "Wybrana strona",
                "project_slug": "alpha-project",
                "summary": "tylko syntetyczna",
            }],
        )
        canary_registry = self.root / "elsewhere" / "registry-v2.json"
        canary_registry.parent.mkdir(parents=True)
        canary_registry.write_text(json.dumps({
            "version": "canary",
            "projects": {"canary-project": {"aliases": [], "org_links": []}},
            "organizations": [],
            "topics": [],
        }), encoding="utf-8")
        nav = RecordingNav(hub_root=self.canonical_hub)
        with patch.object(knowledge, "_load_navigation_module", return_value=nav):
            with redirect_stdout(io.StringIO()):
                knowledge.navigate(self.selected, nav_namespace(project="alpha-project"))
        paths = {name: Path(path) for name, path in nav.seen_paths.items() if path}
        for name, path in paths.items():
            self.assertTrue(path.is_relative_to(self.selected), f"{name} -> {path}")
            self.assertNotEqual(path, canary_registry)
        self.assertEqual(paths["REGISTRY_LIVE"], self.selected / "registry" / "registry-v2.json")
        self.assertEqual(paths["TOPIC_FEEDS"], self.selected / "feeds" / "KSEF-SCOUT-CONTRACT.json")

    def test_real_module_reads_only_the_selected_synthetic_vault(self):
        make_vault(
            self.canonical_vault,
            registry={
                "version": "canary-hub",
                "projects": {"canary-project": {"aliases": [], "org_links": []}},
                "organizations": [],
                "topics": [],
            },
            pages=[{
                "rel": "canary.md",
                "title": "CANARY-DO-NOT-READ",
                "project_slug": "canary-project",
                "summary": "canary",
            }],
        )
        (self.canonical_hub / "registry").mkdir(parents=True, exist_ok=True)
        (self.canonical_hub / "registry" / "registry-v2.json").write_text(
            json.dumps({
                "version": "canary-hub-registry",
                "projects": {"canary-project": {"aliases": [], "org_links": []}},
                "organizations": [],
                "topics": [],
            }),
            encoding="utf-8",
        )
        make_vault(
            self.selected,
            pages=[{
                "rel": "wybrane.md",
                "title": "Wybrana strona żółć",
                "project_slug": "alpha-project",
                "summary": "wyłącznie syntetyczna",
            }],
        )

        def loader_with_canary_hub():
            module = self._load_real_navigation()
            module.HUB_ROOT = self.canonical_hub
            module.VAULT = self.canonical_vault
            module.REGISTRY_LIVE = self.canonical_hub / "registry" / "registry-v2.json"
            module.REGISTRY_SEED = self.canonical_hub / "registry" / "I1-registry-v2.frozen-v0.json"
            module.TOPIC_FEEDS = self.canonical_hub / "feeds" / "KSEF-SCOUT-CONTRACT.json"
            return module

        out = io.StringIO()
        with patch.object(knowledge, "_load_navigation_module", side_effect=loader_with_canary_hub):
            with redirect_stdout(out):
                code = knowledge.navigate(self.selected, nav_namespace(project="alpha-project"))
        self.assertEqual(code, 0, out.getvalue())
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["result"], "FOUND")
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["records"][0]["title"], "Wybrana strona żółć")
        self.assertNotIn("CANARY-DO-NOT-READ", out.getvalue())
        self.assertNotIn("canary-project", out.getvalue())

    def _load_real_navigation(self):
        spec = importlib.util.spec_from_file_location("knowledge_navigation_i3_paths", NAVIGATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_canonical_vault_keeps_module_registry_and_feed_defaults(self):
        mod = RecordingNav(hub_root=self.canonical_hub)
        expected_registry = self.canonical_hub / "registry" / "registry-v2.json"
        expected_seed = self.canonical_hub / "registry" / "I1-registry-v2.frozen-v0.json"
        expected_feed = self.canonical_hub / "feeds" / "KSEF-SCOUT-CONTRACT.json"
        knowledge._bind_navigation_to_vault(mod, self.canonical_vault)
        self.assertEqual(Path(mod.VAULT), self.canonical_vault)
        self.assertEqual(Path(mod.REGISTRY_LIVE), expected_registry)
        self.assertEqual(Path(mod.REGISTRY_SEED), expected_seed)
        self.assertEqual(Path(mod.TOPIC_FEEDS), expected_feed)

    def test_navigation_module_file_is_not_modified(self):
        before = NAVIGATION.read_bytes()
        make_vault(self.selected, pages=[{
            "rel": "a.md", "title": "A", "project_slug": "alpha-project", "summary": "a",
        }])
        nav = RecordingNav()
        with patch.object(knowledge, "_load_navigation_module", return_value=nav):
            with redirect_stdout(io.StringIO()):
                knowledge.navigate(self.selected, nav_namespace(project="alpha-project"))
        self.assertEqual(NAVIGATION.read_bytes(), before)

    def test_navigate_is_read_only_on_the_vault(self):
        vault = make_vault(
            self.selected,
            pages=[{
                "rel": "a.md", "title": "A", "project_slug": "alpha-project", "summary": "a",
            }],
        )
        before = {
            path.relative_to(vault).as_posix(): path.read_bytes()
            for path in sorted(vault.rglob("*"))
            if path.is_file()
        }
        spec = importlib.util.spec_from_file_location("knowledge_navigation_i3_ro", NAVIGATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(knowledge, "_load_navigation_module", return_value=module):
            with redirect_stdout(io.StringIO()):
                knowledge.navigate(vault, nav_namespace(project="alpha-project"))
        after = {
            path.relative_to(vault).as_posix(): path.read_bytes()
            for path in sorted(vault.rglob("*"))
            if path.is_file()
        }
        self.assertEqual(before, after)


class OldParserRegressionTests(unittest.TestCase):
    def test_existing_commands_still_parse(self):
        parser = knowledge.parser()
        cases = [
            ["status", "--vault", "v"],
            ["lint", "--vault", "v", "--registry-stage", "F2"],
            ["stage-source", "--vault", "v", "--path", "p", "--title", "t", "--agent", "a"],
            ["new-proposal", "--vault", "v", "--title", "t", "--agent", "a"],
            ["checkpoint-intake", "--vault", "v", "--proposal-id", "prop-20260101T000000000Z-00000000"],
            ["record-handoff", "--vault", "v", "--path", "p", "--title", "t", "--agent", "a",
             "--project-slug", "alpha-project", "--state-summary", "s"],
            ["record-session", "--vault", "v", "--agent", "a", "--session-id", "s",
             "--checkpoint-id", "c", "--outcome", "COMPLETED"],
            ["project-brief", "--vault", "v", "--project-slug", "alpha-project"],
            ["node-brief", "--vault", "v", "--project-slug", "alpha-project", "--node-id", "n1",
             "--context-receipt", "r", "--agent", "a", "--session-id", "s"],
            ["inventory", "--vault", "v", "--query", "q"],
            ["begin-task", "--vault", "v", "--agent", "a", "--session-id", "s",
             "--project-slug", "alpha-project"],
            ["verify-context", "--vault", "v", "--context-receipt", "r", "--agent", "a",
             "--session-id", "s", "--project-slug", "alpha-project"],
        ]
        for argv in cases:
            args = parser.parse_args(argv)
            self.assertEqual(args.action, argv[0], argv)

    def test_action_is_still_required(self):
        with self.assertRaises(SystemExit):
            knowledge.parser().parse_args([])

    def test_navigate_does_not_steal_other_command_flags(self):
        args = knowledge.parser().parse_args(["inventory", "--query", "topic"])
        self.assertEqual(args.action, "inventory")
        self.assertEqual(getattr(args, "topic", "missing"), "missing")
        self.assertFalse(hasattr(args, "person") and args.action != "navigate")

    def test_main_dispatches_navigate(self):
        nav = RecordingNav(payload={
            "status": "OK",
            "result": "FOUND",
            "total": 1,
            "query": {"project": "alpha-project"},
            "records": [],
            "warnings": [],
        })
        with tempfile.TemporaryDirectory() as temp:
            vault = make_vault(Path(temp) / "vault", pages=[])
            out = io.StringIO()
            with patch.object(knowledge, "_load_navigation_module", return_value=nav):
                with patch.object(
                    sys, "argv",
                    ["knowledge.py", "navigate", "--project", "alpha-project", "--vault", str(vault)],
                ):
                    with redirect_stdout(out):
                        code = knowledge.main()
            self.assertEqual(code, 0, out.getvalue())
            self.assertIn("--project", nav.seen_argv)
            self.assertIn("alpha-project", nav.seen_argv)



class FeedFacetIntersectionTests(unittest.TestCase):
    def test_project_and_org_intersection_filters_feed_before_pagination(self):
        from types import SimpleNamespace
        for selected in ({"alpha"}, set(), None):
            with self.subTest(selected=selected):
                mod = SimpleNamespace(
                    registry_facets=lambda *a, **kw: ({"projects_filter": selected}, []),
                    topic_feed_records=lambda *a, **kw: ([{"project_slug":"alpha"}, {"project_slug":"beta"}], "same-note"))
                knowledge._filter_navigation_feed(mod)
                mod.registry_facets({}, project="alpha")
                records, note=mod.topic_feed_records(["ksef"])
                expected=2 if selected is None else len(selected)
                self.assertEqual(len(records), expected)
                self.assertEqual(note, "same-note")
                if selected is not None:
                    self.assertTrue(all(r["project_slug"] in selected for r in records))

if __name__ == "__main__":
    unittest.main()
