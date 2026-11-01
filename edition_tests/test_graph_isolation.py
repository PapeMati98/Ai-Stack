"""Synthetic offline binding tests; not native Fleet/Office/Hub acceptance."""
from contextlib import contextmanager, ExitStack, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
import uuid

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1] / "global/codex/skills/agent-graph/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location("isolation_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


office = load("office_bridge")
context = load("knowledge_context")
journal = load("native_session_journal")
scout = load("history_scout")


@contextmanager
def replace(obj, name, value):
    missing = object()
    original = vars(obj).get(name, missing)
    setattr(obj, name, value)
    try:
        yield
    finally:
        if original is missing:
            delattr(obj, name)
        else:
            setattr(obj, name, original)


@contextmanager
def bindings(**values):
    keys = {"AGENT_OFFICE_RUNTIME_ROOT", "AGENT_KNOWLEDGE_CONFIG", "COORDFS_HOME"} | values.keys()
    original = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ.pop(key, None)
    os.environ.update({key: str(value) for key, value in values.items() if value is not None})
    try:
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def forbidden(*args, **kwargs):
    raise AssertionError("Host fallback or unintended process launch")


class IsolationTests(unittest.TestCase):
    def setUp(self):
        # Python 3.13's Windows mkdtemp mode 0700 excludes sandbox identities.
        # A unique task-owned directory with inherited ACLs works in both the
        # contractor sandbox and normal Windows/Linux test environments.
        self.root = (Path(__file__).parent / ("_graph_isolation_" + uuid.uuid4().hex)).absolute()
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.guard = ExitStack()
        self.addCleanup(self.guard.close)
        self.guard.enter_context(bindings())
        self.guard.enter_context(replace(Path, "home", classmethod(forbidden)))
        self.guard.enter_context(replace(subprocess, "run", forbidden))

    def config(self, name="selected.json"):
        skill = self.root / (name + "-skill")
        (skill / "scripts").mkdir(parents=True)
        (skill / "scripts/knowledge.py").write_text("# synthetic gateway path fixture\n", encoding="utf-8")
        vault = self.root / (name + "-vault")
        vault.mkdir()
        config = self.root / name
        config.write_text(json.dumps({"canonical_skill_path": str(skill), "vault_path": str(vault)}), encoding="utf-8")
        return config, skill / "scripts/knowledge.py"

    def runtime(self, remote=False):
        root = self.root / "runtime"
        root.mkdir()
        (root / "office.py").write_text("# synthetic runtime source fixture\n", encoding="utf-8")
        marker = {
            "managed_by": "agent-office-installer", "package_id": "agent-office",
            "schema_version": 1, "package_version": "1.2.3", "tool": "runtime",
            "package_fingerprint": "a" * 64,
            "python_executable": sys.executable,
            "source_fingerprint": office._runtime_source_fingerprint(root),
        }
        if remote:
            config = root / office.REMOTE_CONFIG_NAME
            config.write_text('{"synthetic":true}', encoding="utf-8")
            marker["remote_config_sha256"] = hashlib.sha256(config.read_bytes()).hexdigest()
        (root / ".agent-office-runtime.json").write_text(json.dumps(marker), encoding="utf-8")
        return root

    def journal_args(self):
        return dict(
            request={}, task={"id": "synthetic-node", "project_slug": "synthetic-project"},
            role="maker", outdir=self.root / "journal-output",
            result={"run_id": "synthetic-run", "model": "synthetic-model", "status": "COMPLETED", "exit_code": 0},
            record={"run_id": "synthetic-run", "model": "synthetic-model", "agent": "synthetic-agent"},
            attested=True, identity_ok=True, identity={}, candidate={"model": "synthetic-model"},
            agent_id="synthetic-agent",
        )

    def test_office_selects_explicit_verified_runtime_and_local_store(self):
        root = self.runtime()
        calls = []

        def run(command, args, location, timeout):
            calls.append((command, args, location))
            return {"status": "ok"}

        store = self.root / "store"
        with bindings(AGENT_OFFICE_RUNTIME_ROOT=root, COORDFS_HOME=store), replace(office, "_run", run):
            result = office.publish("synthetic-run", "synthetic-node", "completed")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[0][-1] == str(root / "office.py") for call in calls))
        self.assertTrue(all(call[2] == ["--home", str(store)] for call in calls))

    def test_office_invalid_binding_blocks_before_any_runtime_or_store_read(self):
        not_directory = self.root / "file"
        not_directory.write_text("fixture", encoding="utf-8")
        values = ["", " \t", ".", "relative-runtime", str(self.root / "missing"), str(not_directory)]
        for value in values:
            with self.subTest(value=value), bindings(AGENT_OFFICE_RUNTIME_ROOT=value):
                with replace(Path, "read_text", forbidden), replace(office, "_run", forbidden):
                    result = office.publish("synthetic-run", "synthetic-node", "completed")
                self.assertEqual(result["code"], "office_runtime_unavailable")

    def test_office_marker_and_source_tampering_block(self):
        root = self.runtime()
        marker = root / ".agent-office-runtime.json"
        original = marker.read_text(encoding="utf-8")
        with bindings(AGENT_OFFICE_RUNTIME_ROOT=root):
            for value in ("[]", "{", "[" * 2000 + "]" * 2000):
                with self.subTest(marker=value[:20]):
                    marker.write_text(value, encoding="utf-8")
                    self.assertEqual(office.publish("r", "n", "dispatch")["status"], "blocked")
            marker.write_text(original, encoding="utf-8")
            (root / "office.py").write_text("# changed source\n", encoding="utf-8")
            self.assertEqual(office.publish("r", "n", "dispatch")["code"], "office_runtime_unavailable")

    def test_office_remote_pin_and_store_precedence_and_frozen_protection(self):
        root = self.runtime(remote=True)
        store = self.root / "store"
        explicit_store = self.root / "explicit-store"
        with bindings(AGENT_OFFICE_RUNTIME_ROOT=root):
            selector = (["--remote-config", str(root / office.REMOTE_CONFIG_NAME)], True)
            self.assertEqual(office._office_location(None), selector)
            (root / office.REMOTE_CONFIG_NAME).write_text("{}", encoding="utf-8")
            self.assertIsNone(office._office_location(None))
        with bindings(AGENT_OFFICE_RUNTIME_ROOT=root, COORDFS_HOME=store):
            self.assertEqual(office._office_location(explicit_store), (["--home", str(explicit_store)], False))
            (store / "office").mkdir(parents=True)
            (store / "office/FROZEN").write_text("frozen", encoding="utf-8")
            self.assertIsNone(office._office_location(None))
            self.assertEqual(office.publish("r", "n", "dispatch")["code"], "office_store_unavailable")

    def test_office_failed_transport_is_fail_open_and_windowless(self):
        root = self.runtime()
        calls = []

        def run(argv, **kwargs):
            calls.append(kwargs)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        with bindings(AGENT_OFFICE_RUNTIME_ROOT=root, COORDFS_HOME=self.root / "store"), replace(subprocess, "run", run):
            self.assertEqual(office.publish("r", "n", "dispatch")["status"], "degraded")
        self.assertFalse(calls[0]["shell"])
        if os.name == "nt":
            self.assertEqual(calls[0]["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_knowledge_override_and_explicit_arg_precedence(self):
        selected, gateway = self.config()
        for module in (context, scout, journal):
            with self.subTest(module=module.__name__), bindings(AGENT_KNOWLEDGE_CONFIG=selected):
                self.assertEqual(module._config_path(), selected)
                self.assertEqual(module._config_path(self.root / "explicit.json"), self.root / "explicit.json")
        with bindings(AGENT_KNOWLEDGE_CONFIG=selected):
            self.assertEqual(context._read_config()["canonical_skill_path"], str(gateway.parents[1]))
            self.assertEqual(scout._read_config()["canonical_skill_path"], str(gateway.parents[1]))
            self.assertEqual(journal.resolve_gateway(), (gateway, None))
        with bindings(AGENT_KNOWLEDGE_CONFIG=""):
            self.assertEqual(journal.resolve_gateway(selected), (gateway, None))

    def test_invalid_knowledge_binding_never_reads_or_launches_host(self):
        for value in ("", " \t", ".", "relative.json", str(self.root / "missing.json"), str(self.root)):
            with self.subTest(value=value), bindings(AGENT_KNOWLEDGE_CONFIG=value), replace(Path, "read_text", forbidden):
                with self.assertRaises(context.KnowledgeConfigError):
                    context._read_config()
                with self.assertRaises(scout.ScoutError):
                    scout._discover_gateway(None, ["synthetic"], limit=2, offset=0)
                result = journal.record_native_session(**self.journal_args())
                self.assertEqual(result["cause"], "gateway-resolution-error")
                self.assertFalse(result["hub_written"])
                self.assertFalse((self.root / "journal-output").exists())

    def test_malformed_deep_and_invalid_field_configs_never_fall_back(self):
        selected, gateway = self.config()
        valid = json.loads(selected.read_text(encoding="utf-8"))
        bad = ["{", "[]", "null", "[" * 2000 + "]" * 2000]
        for field in (None, 12, [], {}, False, "", " \t"):
            bad.append(json.dumps(dict(valid, canonical_skill_path=field)))
        for text in bad:
            with self.subTest(config=text[:80]), bindings(AGENT_KNOWLEDGE_CONFIG=selected):
                selected.write_text(text, encoding="utf-8")
                with self.assertRaises(context.KnowledgeConfigError):
                    context._read_config()
                with self.assertRaises(scout.ScoutError):
                    scout._discover_gateway(None, ["synthetic"], limit=2, offset=0)
                result = journal.record_native_session(**self.journal_args())
                self.assertEqual(result["cause"], "gateway-resolution-error")
                self.assertFalse(result["hub_written"])
        for field in (None, 12, [], {}, False, "", " \t"):
            selected.write_text(json.dumps(dict(valid, vault_path=field)), encoding="utf-8")
            with bindings(AGENT_KNOWLEDGE_CONFIG=selected), self.assertRaises(scout.ScoutError):
                scout._discover_gateway(None, ["synthetic"], limit=2, offset=0)

    def test_installed_defaults_unchanged_when_bindings_absent(self):
        with replace(Path, "home", classmethod(lambda cls: self.root)):
            self.assertEqual(office._runtime_root(), self.root / ".agents/runtime/agent-office")
            for module in (context, scout, journal):
                self.assertEqual(module._config_path(), self.root / ".agents/knowledge-config.json")

    def test_missing_explicit_argument_cannot_fall_back_to_valid_environment(self):
        selected, gateway = self.config()
        missing = self.root / "missing-explicit.json"
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            self.assertEqual(path, missing, "Only the selected explicit config may be read")
            return original_read(path, *args, **kwargs)

        with bindings(AGENT_KNOWLEDGE_CONFIG=selected), replace(Path, "read_text", read):
            with self.assertRaises(context.KnowledgeConfigError):
                context._read_config(missing)
            with self.assertRaises(scout.ScoutError):
                scout._discover_gateway(None, ["synthetic"], limit=2, offset=0, config_path=missing)
            self.assertIsNone(journal.resolve_gateway(missing)[0])

    def test_scout_explicit_config_controls_gateway_child_config(self):
        selected, gateway = self.config()
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stdout=b'{"entries":[]}')

        with bindings(AGENT_KNOWLEDGE_CONFIG=""), replace(subprocess, "run", run):
            result = scout.discover(gateway=gateway, topics=["synthetic"], config_path=selected)
        self.assertEqual(result, [])
        self.assertEqual(calls[0][0][2], str(gateway))
        self.assertEqual(calls[0][1]["env"]["AGENT_KNOWLEDGE_CONFIG"], str(selected))
        if os.name == "nt":
            self.assertEqual(calls[0][1]["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_context_explicit_cli_config_controls_child_config(self):
        selected, gateway = self.config()
        output = self.root / "context.json"
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(stdout='{"status":"COMPLETE","health":{"lint":"PASS"}}')

        args = ["knowledge_context.py", "--project-slug", "synthetic", "--agent", "synthetic",
                "--session-id", "synthetic", "--output", str(output), "--config", str(selected)]
        with bindings(AGENT_KNOWLEDGE_CONFIG=""), replace(sys, "argv", args), replace(subprocess, "run", run), redirect_stdout(io.StringIO()):
            self.assertEqual(context.main(), 0)
        self.assertTrue(output.is_file())
        self.assertEqual(calls[0][0][1], str(gateway))
        self.assertEqual(calls[0][1]["env"]["AGENT_KNOWLEDGE_CONFIG"], str(selected))
        if os.name == "nt":
            self.assertEqual(calls[0][1]["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_journal_explicit_config_controls_child_without_claiming_confirmation(self):
        selected, gateway = self.config()
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        with bindings(AGENT_KNOWLEDGE_CONFIG=""), replace(subprocess, "run", run):
            result = journal.record_native_session(config_path=selected, **self.journal_args())
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertIsNone(result["hub_written"])
        self.assertEqual(calls[0][0][2], str(gateway))
        self.assertEqual(calls[0][1]["env"]["AGENT_KNOWLEDGE_CONFIG"], str(selected))
        if os.name == "nt":
            self.assertEqual(calls[0][1]["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_linked_config_runtime_and_parent_directory_are_refused(self):
        selected, gateway = self.config()
        root = self.runtime()
        link = self.root / "linked-config.json"
        try:
            link.symlink_to(selected)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink capability unavailable; synthetic link guard tested separately")
        runtime_link = self.root / "linked-runtime"
        runtime_link.symlink_to(root, target_is_directory=True)
        parent_link = self.root / "linked-parent"
        parent_link.symlink_to(self.root / (selected.name + "-skill"), target_is_directory=True)
        config_in_parent = gateway.parent / "config.json"
        config_in_parent.write_bytes(selected.read_bytes())
        for config in (link, parent_link / "scripts/config.json"):
            with bindings(AGENT_KNOWLEDGE_CONFIG=config):
                with self.assertRaises(context.KnowledgeConfigError):
                    context._read_config()
                with self.assertRaises(scout.ScoutError):
                    scout._read_config()
                self.assertIsNone(journal.resolve_gateway()[0])
        with bindings(AGENT_OFFICE_RUNTIME_ROOT=runtime_link):
            self.assertEqual(office.publish("r", "n", "dispatch")["code"], "office_runtime_unavailable")

    def test_windows_junction_guard_without_requiring_link_privileges(self):
        selected, gateway = self.config()
        root = self.runtime()
        parents = {root, selected.parent}
        with replace(Path, "is_junction", lambda path: path in parents):
            with bindings(AGENT_OFFICE_RUNTIME_ROOT=root):
                self.assertIsNone(office._runtime_command())
            with bindings(AGENT_KNOWLEDGE_CONFIG=selected):
                for module in (context, scout, journal):
                    with self.subTest(module=module.__name__), self.assertRaises((ValueError, RuntimeError)):
                        module._config_path()


if __name__ == "__main__":
    unittest.main()
