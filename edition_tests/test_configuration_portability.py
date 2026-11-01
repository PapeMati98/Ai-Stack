"""Offline portability regressions; all fixture state stays under this checkout."""
import ast
import copy
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "shared" / "routing-ledger" / "routing_ledger.py"


def load_ledger():
    return load_source(SOURCE)


def load_source(path):
    name = "portable_test_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[name]
    return module


@contextmanager
def binding(name, value):
    previous = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = str(value)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


class RoutingLedgerPortabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ("synthetic-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.ledger = load_ledger()

    def test_defaults_follow_installation_and_native_home(self):
        home = self.root / "synthetic-user"
        stack = self.root / "relocated-install"
        expected = [home / ".agents", stack / "docs", stack / "outputs",
                    stack.parent / "research-runs"]
        for directory in expected:
            directory.mkdir(parents=True)

        class InstalledPath(type(Path())):
            @classmethod
            def home(cls):
                return cls(home)

        self.ledger.Path = InstalledPath
        self.ledger.__file__ = str(stack / "shared" / "routing-ledger" / "routing_ledger.py")
        self.assertEqual(self.ledger.default_roots(), expected)
        (stack / "outputs").rmdir()
        self.assertEqual(self.ledger.default_roots(), [p for p in expected if p.name != "outputs"])

    def test_relocated_build_keeps_source_and_refuses_overwrite(self):
        source = self.root / "synthetic-runs"
        source.mkdir()
        sentinel = source / "unrelated.txt"
        sentinel.write_bytes(b"synthetic source stays unchanged")
        out = self.root / "ledger-output"
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ledger, summary, result = self.ledger.build([source], source, out, now=moment)
        self.assertEqual(sentinel.read_bytes(), b"synthetic source stays unchanged")
        self.assertEqual(ledger.read_bytes(), b"")
        self.assertEqual(json.loads(summary.read_text(encoding="utf-8")), result)
        self.assertEqual(result["source_roots"], [self.ledger.short_hash(source.resolve())])
        with self.assertRaises(FileExistsError):
            self.ledger.build([source], source, out, now=moment)

    def test_output_inside_or_above_runs_is_still_refused(self):
        runs = self.root / "synthetic-runs"
        for out in (runs, runs / "output", self.root):
            with self.subTest(out=out), self.assertRaisesRegex(ValueError, "OVERLAPS_FLEET_RUNS"):
                self.ledger.build([runs], runs, out)
        self.assertFalse(runs.exists())


class ExplicitBindingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ("synthetic-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)

    def test_topic_feed_override_is_read_and_invalid_selection_never_falls_back(self):
        source = ROOT / "shared/knowledge-hub/skills/llm-wiki/scripts/navigation.py"
        feed = self.root / "feed.json"
        feed.write_text(json.dumps({"topic": "synthetic-topic", "curated_records": [
            {"source_ref": "wiki/projects/synthetic.md", "project_slug": "synthetic",
             "observed_at": "2026-01-01", "title": "Synthetic record"}]}), encoding="utf-8")
        with binding("KNOWLEDGE_TOPIC_FEEDS", feed):
            navigation = load_source(source)
            self.assertEqual(navigation.TOPIC_FEEDS, feed.resolve())
            records, warning = navigation.topic_feed_records(["synthetic-topic"])
            self.assertIsNone(warning)
            self.assertEqual(records[0]["title"], "Synthetic record")
        for invalid in ("", "relative.json", self.root / "missing.json", self.root):
            with self.subTest(invalid=invalid), binding("KNOWLEDGE_TOPIC_FEEDS", invalid):
                with self.assertRaisesRegex(ValueError, "KNOWLEDGE_TOPIC_FEEDS"):
                    load_source(source)

    def test_muse_binding_keeps_release_and_all_privacy_gates(self):
        config = load_source(ROOT / "shared/openclaw-hub-caretaker/releases/0.1.78/caretaker/config.py")
        plugin = self.root / "muse-code" / "0.1.0"
        plugin.mkdir(parents=True)
        inline = {
            "agents": {"defaults": {"model": {"primary": "muse-code/muse-spark-1.3", "fallbacks": []},
                                    "skipBootstrap": True, "heartbeat": {"every": "0m"}}},
            "telemetry": {"enabled": False}, "update": {"checkOnStart": False},
            "tools": {"profile": "full", "allow": [], "deny": ["*"], "exec": {"mode": "deny"},
                      "fs": {"workspaceOnly": True}, "swarm": False,
                      "web": {"search": {"enabled": False}, "fetch": {"enabled": False}}},
            "plugins": {"enabled": True, "allow": ["muse-code"], "entries": {"muse-code": {"enabled": True}},
                        "load": {"paths": [plugin.as_posix()]}},
            "models": {"providers": {}}, "mcp": {"servers": {}},
            "skills": {"allowBundled": [], "load": {"watch": False, "extraDirs": []}},
        }
        with binding("HUB_CARETAKER_MUSE_PLUGIN_ROOT", plugin):
            config.validate_muse_inline(inline)
            mutations = [
                ("telemetry", {"enabled": True}),
                ("models", {"providers": {"synthetic": {}}}),
                ("mcp", {"servers": {"synthetic": {}}}),
                ("skills", {"allowBundled": ["synthetic"]}),
            ]
            for key, value in mutations:
                candidate = copy.deepcopy(inline)
                candidate[key] = value
                with self.subTest(key=key), self.assertRaises(config.ConfigError):
                    config.validate_muse_inline(candidate)
            for key, value in (("allow", ["exec"]), ("deny", []), ("swarm", True)):
                candidate = copy.deepcopy(inline)
                candidate["tools"][key] = value
                with self.subTest(tool=key), self.assertRaises(config.ConfigError):
                    config.validate_muse_inline(candidate)
            candidate = copy.deepcopy(inline)
            candidate["plugins"]["load"]["paths"] = [str(self.root / "unreviewed-plugin")]
            with self.assertRaises(config.ConfigError):
                config.validate_muse_inline(candidate)
        for invalid in ("", "relative", self.root / "missing", self.root):
            with self.subTest(invalid=invalid), binding("HUB_CARETAKER_MUSE_PLUGIN_ROOT", invalid):
                with self.assertRaises(config.ConfigError):
                    config._muse_plugin_root()

    def test_helpdesk_explicit_roots_do_not_fall_back(self):
        helpdesk = load_source(ROOT / "global/codex/skills/rsi-helpdesk/scripts/helpdesk.py")
        with binding("RSI_HELPDESK_STDERR_ROOTS", self.root):
            self.assertEqual(helpdesk.stderr_roots(), [self.root.resolve()])
        with binding("RSI_HELPDESK_STDERR_ROOTS", "relative"):
            with self.assertRaises(ValueError):
                helpdesk.stderr_roots()
        with binding("RSI_HELPDESK_STDERR_ROOTS", ""):
            self.assertEqual(helpdesk.stderr_roots(), [])
        for invalid in ("", "relative.py", self.root / "missing.py"):
            with self.subTest(invalid=invalid), binding("RSI_HELPDESK_OFFICE", invalid):
                self.assertIsNone(helpdesk.office_script())
        office = self.root / "synthetic-office.py"
        office.write_text("# synthetic binding; never executed\n", encoding="utf-8")
        with binding("RSI_HELPDESK_OFFICE", office):
            self.assertEqual(helpdesk.office_script(), office)

    def test_remote_apply_refuses_missing_bindings_before_loading_dependencies(self):
        source = ROOT / "shared/mcp/initech-m365/workspace-remote.py"
        for task_root in ("relative", str(self.root)):
            with self.subTest(task_root=task_root):
                result = subprocess.run([sys.executable, "-I", "-B", str(source), "--apply", "--task-root", task_root],
                                        capture_output=True, text=True, timeout=10,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("error:", result.stderr)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_embedded_remote_programs_compile_with_synthetic_bindings(self):
        source = ROOT / "shared/mcp/initech-m365/workspace-remote.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        programs = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
                    and isinstance(node.value, str) and "base=pathlib.Path(BASE_LITERAL)" in node.value]
        self.assertEqual(len(programs), 2)
        for program in programs:
            for marker, value in (("BASE_LITERAL", repr("/synthetic/release/0.150.2")),
                                  ("ROOT_LITERAL", repr("/synthetic/profile/m365")),
                                  ("REMOTE_HOME_LITERAL", repr("/synthetic/home")), ("HASHES", "{}")):
                program = program.replace(marker, value)
            compile(program, "synthetic-remote-program", "exec")
        source = ROOT / "shared/openclaw-hub-caretaker/releases/0.1.78/caretaker/sync.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        program = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "REMOTE_SCRIPT" for target in node.targets))
        program = program.replace("BINDINGS_JSON", repr(json.dumps({
            "hub": "/synthetic/hub", "origin": "/synthetic/origin.git"})))
        compile(program, "synthetic-sync-program", "exec")


class NativeProfilePortabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "edition_tests" / ("synthetic-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        source = ROOT / "fleet-suite/payload/agent-fleet/scripts/fleet.py"
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        # Execute the real pure path functions without loading native SDKs,
        # user32, account stores, or the Fleet CLI in this offline check.
        names = {"expand_candidate", "bind_native_profile_paths", "normalized_workspace_path",
                 "path_is_within", "is_filesystem_link", "validate_profile_workspace_boundary"}
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in names]
        self.assertEqual({node.name for node in functions}, names)
        namespace = {"Any": object, "Path": Path, "os": os, "stat": stat, "FleetError": ValueError}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
        self.bind_paths = namespace["bind_native_profile_paths"]
        self.validate_workspace = namespace["validate_profile_workspace_boundary"]
        self.profiles = json.loads((ROOT / "fleet-suite/payload/agent-fleet/references/profiles.json")
                                   .read_text(encoding="utf-8-sig"))["profiles"]

    def test_profiles_bind_to_synthetic_native_home_before_adapter_validation(self):
        home = self.root / "synthetic-user"
        for agent_id, relative in (
            ("opencode-mercury", ".local/opencode-cli-1.18.21/node_modules/opencode-ai/bin/opencode.exe"),
            ("mistral", ".agents/runtime/mistral-vibe/2.26.1/Scripts/vibe.exe"),
            ("mimo", ".mimocode/data/auth.json"),
        ):
            profile = copy.deepcopy(self.profiles[agent_id])
            with self.subTest(agent_id=agent_id), binding("USERPROFILE", home), binding("HOME", home):
                self.bind_paths(agent_id, profile)
                actual = (profile["mimo"]["credential_store"]["path"] if agent_id == "mimo"
                          else profile["candidates"][0])
                self.assertEqual(Path(actual), home / relative)
                if agent_id == "opencode-mercury":
                    self.assertEqual(Path(profile["data_boundary"]["workspace_root"]),
                                     home / ".agents/task-work/mercury-pilot-20261020/synthetic")
                self.assertFalse(home.exists(), "Binding must not read/create a native installation or auth store")

    def test_relative_or_unresolved_native_binding_is_refused(self):
        for agent_id in ("mimo", "mistral", "opencode-mercury"):
            for invalid in ("", "relative/tool.exe", "$PORTABILITY_MISSING/tool.exe", None):
                profile = copy.deepcopy(self.profiles[agent_id])
                if agent_id == "mimo":
                    profile["mimo"]["credential_store"]["path"] = invalid
                else:
                    profile["candidates"] = [invalid]
                with self.subTest(agent_id=agent_id, invalid=invalid), binding("PORTABILITY_MISSING", None):
                    with self.assertRaises(ValueError):
                        self.bind_paths(agent_id, profile)

    def test_path_binding_keeps_roles_models_and_provider_gates(self):
        for agent_id in ("mimo", "mistral", "opencode-mercury"):
            before = copy.deepcopy(self.profiles[agent_id])
            after = copy.deepcopy(before)
            with binding("USERPROFILE", self.root), binding("HOME", self.root):
                self.bind_paths(agent_id, after)
            if agent_id == "mimo":
                after["mimo"]["credential_store"]["path"] = before["mimo"]["credential_store"]["path"]
            else:
                after["candidates"] = before["candidates"]
            if agent_id == "opencode-mercury":
                after["data_boundary"]["workspace_root"] = before["data_boundary"]["workspace_root"]
            self.assertEqual(after, before)
        self.assertIs(self.profiles["mimo"]["mimo"]["token_plan_only"], True)
        self.assertIs(self.profiles["mistral"]["mistral"]["pro_only"], True)
        self.assertIs(self.profiles["mistral"]["mistral"]["payg_fallback"], False)
        mercury = self.profiles["opencode-mercury"]
        self.assertIs(mercury["paid_fallback"], False)
        self.assertIs(mercury["data_boundary"]["synthetic_only"], True)
        self.assertEqual(mercury["routing_blocker"], "pending-parent-canary-and-independent-review")

    def test_unrelated_profile_contracts_are_unchanged(self):
        for agent_id in ("opencode-beam", "codex", "openclaw-muse-subscription"):
            before = copy.deepcopy(self.profiles[agent_id])
            after = copy.deepcopy(before)
            self.bind_paths(agent_id, after)
            self.assertEqual(after, before)

    def test_mercury_workspace_binding_preserves_synthetic_boundary(self):
        home = self.root / "synthetic-user"
        allowed = home / ".agents/task-work/mercury-pilot-20261020/synthetic"
        admitted = allowed / "admitted-task"
        admitted.mkdir(parents=True)
        outside = self.root / "foreign-workspace"
        outside.mkdir()
        profile = copy.deepcopy(self.profiles["opencode-mercury"])
        with binding("USERPROFILE", home), binding("HOME", home):
            self.bind_paths("opencode-mercury", profile)
            self.assertEqual(self.validate_workspace(profile, admitted), admitted.resolve())
            with self.assertRaisesRegex(ValueError, "restricted to workspace root"):
                self.validate_workspace(profile, outside)
            profile["data_boundary"]["workspace_root"] = str(self.root / "missing-synthetic-root")
            with self.assertRaisesRegex(ValueError, "could not be resolved"):
                self.validate_workspace(profile, admitted)

    def test_explicit_absolute_native_paths_are_retained(self):
        for agent_id in ("opencode-mercury", "mistral", "mimo"):
            path = str(self.root / "explicit-install" / "native-file")
            profile = copy.deepcopy(self.profiles[agent_id])
            if agent_id == "mimo":
                profile["mimo"]["credential_store"]["path"] = path
            else:
                profile["candidates"] = [path]
            if agent_id == "opencode-mercury":
                profile["data_boundary"]["workspace_root"] = str(self.root / "explicit-synthetic-root")
            before = copy.deepcopy(profile)
            self.bind_paths(agent_id, profile)
            self.assertEqual(profile, before)

    def test_invalid_mercury_workspace_binding_is_refused_before_admission(self):
        for invalid in ("", "relative-workspace", "$PORTABILITY_MISSING/workspace", None):
            profile = copy.deepcopy(self.profiles["opencode-mercury"])
            profile["data_boundary"]["workspace_root"] = invalid
            with self.subTest(invalid=invalid), binding("PORTABILITY_MISSING", None):
                with self.assertRaises(ValueError):
                    self.bind_paths("opencode-mercury", profile)


if __name__ == "__main__":
    unittest.main()
