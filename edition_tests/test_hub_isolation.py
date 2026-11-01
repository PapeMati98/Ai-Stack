"""Isolated Hub gateway isolation tests (synthetic offline fixtures only)."""
import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "shared" / "knowledge-hub" / "skills" / "llm-wiki" / "scripts" / "knowledge.py"

_spec = importlib.util.spec_from_file_location("hub_gateway_knowledge", SCRIPT)
kg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kg)

MANAGED = "shared-agent-knowledge-hub"
ENV_KEYS = ("AGENT_KNOWLEDGE_CONFIG", "AGENT_KNOWLEDGE_VAULT",
            "KNOWLEDGE_READ_TELEMETRY_ROOT", "KNOWLEDGE_CARETAKER_STATUS_PATH")


class HubIsolationTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory(prefix="hub-isolation-")
        self.root = Path(self.tmp.name)
        self._saved_home = os.environ.get("HOME")
        self._saved_profile = os.environ.get("USERPROFILE")
        os.environ["HOME"] = str(self.root / "home")
        os.environ["USERPROFILE"] = str(self.root / "home")
        (self.root / "home").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if self._saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._saved_home
        if self._saved_profile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = self._saved_profile
        self.tmp.cleanup()

    def make_vault(self, name="vault"):
        vault = self.root / name
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "knowledge-vault.json").write_text(
            json.dumps({"version": 1, "entry_count": 0, "entries": []}),
            encoding="utf-8")
        return vault

    def make_config(self, name, **fields):
        path = self.root / name
        path.write_text(json.dumps(fields), encoding="utf-8")
        return path

    def home_config(self, **fields):
        path = Path(os.environ["HOME"]) / ".agents" / "knowledge-config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fields), encoding="utf-8")
        return path

    def test_config_override_selects_vault(self):
        vault = self.make_vault()
        cfg = self.make_config("kc.json", managed_by=MANAGED, vault_path=str(vault))
        os.environ["AGENT_KNOWLEDGE_CONFIG"] = str(cfg)
        self.assertEqual(kg.resolve_vault(None), vault.resolve())

    def test_explicit_vault_takes_precedence_over_config(self):
        first = self.make_vault("vault-a")
        second = self.make_vault("vault-b")
        cfg = self.make_config("kc.json", managed_by=MANAGED, vault_path=str(second))
        os.environ["AGENT_KNOWLEDGE_CONFIG"] = str(cfg)
        os.environ["AGENT_KNOWLEDGE_VAULT"] = str(first)
        self.assertEqual(kg.resolve_vault(None), first.resolve())
        self.assertEqual(kg.resolve_vault(str(second)), second.resolve())

    def test_malformed_override_fails_closed_without_fallback(self):
        decoy = self.make_vault("decoy")
        self.home_config(managed_by=MANAGED, vault_path=str(decoy))
        bad = self.root / "bad-config.json"
        bad.write_text("{not json", encoding="utf-8")
        for raw in (str(bad), "relative/path.json", str(self.root / "missing.json")):
            os.environ["AGENT_KNOWLEDGE_CONFIG"] = raw
            with self.assertRaises(kg.GatewayError):
                kg.resolve_vault(None)
        wrong = self.make_config("wrong.json", managed_by="something-else", vault_path=str(decoy))
        os.environ["AGENT_KNOWLEDGE_CONFIG"] = str(wrong)
        with self.assertRaises(kg.GatewayError):
            kg.resolve_vault(None)

    def test_default_home_config_still_used_without_override(self):
        vault = self.make_vault()
        self.home_config(managed_by=MANAGED, vault_path=str(vault))
        self.assertEqual(kg.resolve_vault(None), vault.resolve())

    def test_caretaker_status_observes_selected_config(self):
        vault = self.make_vault()
        status = self.root / "status.json"
        status.write_text("{}", encoding="utf-8")
        cfg = self.make_config("kc.json", managed_by=MANAGED, vault_path=str(vault),
                               caretaker_status_path=str(status))
        os.environ["AGENT_KNOWLEDGE_CONFIG"] = str(cfg)
        self.assertEqual(kg.resolve_caretaker_status_path(vault), status.resolve())

    def test_caretaker_malformed_override_has_no_home_fallback(self):
        vault = self.make_vault()
        decoy = self.root / "decoy-status.json"
        decoy.write_text("{}", encoding="utf-8")
        self.home_config(caretaker_status_path=str(decoy))
        bad = self.root / "bad-config.json"
        bad.write_text("{not json", encoding="utf-8")
        os.environ["AGENT_KNOWLEDGE_CONFIG"] = str(bad)
        self.assertIsNone(kg.resolve_caretaker_status_path(vault))

    def test_telemetry_root_override_and_validation(self):
        vault = self.make_vault()
        tel_root = self.root / "telemetry"
        os.environ["KNOWLEDGE_READ_TELEMETRY_ROOT"] = str(tel_root)
        path = kg.read_telemetry_path(vault)
        self.assertEqual(path.parent.parent, tel_root)
        os.environ["KNOWLEDGE_READ_TELEMETRY_ROOT"] = "relative/dir"
        with self.assertRaises(kg.GatewayError):
            kg.read_telemetry_path(vault)
        os.environ["KNOWLEDGE_READ_TELEMETRY_ROOT"] = str(vault / "tel")
        with self.assertRaises(kg.GatewayError):
            kg.read_telemetry_path(vault)
        del os.environ["KNOWLEDGE_READ_TELEMETRY_ROOT"]
        default = kg.read_telemetry_path(vault)
        self.assertEqual(default.parent.parent.name, "knowledge-read-telemetry")

    def test_empty_bindings_fail_closed(self):
        vault = self.make_vault()
        self.home_config(managed_by=MANAGED, vault_path=str(vault))
        for raw in ("", "   "):
            os.environ["AGENT_KNOWLEDGE_CONFIG"] = raw
            with self.assertRaises(kg.GatewayError):
                kg.resolve_vault(None)
            os.environ.pop("AGENT_KNOWLEDGE_CONFIG", None)
            os.environ["KNOWLEDGE_READ_TELEMETRY_ROOT"] = raw
            with self.assertRaises(kg.GatewayError):
                kg.read_telemetry_path(vault)
            os.environ.pop("KNOWLEDGE_READ_TELEMETRY_ROOT", None)

    def test_caretaker_missing_status_ignores_vault_relative_trap(self):
        vault = self.root / "deep1" / "deep2" / "deep3" / "vault"
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "knowledge-vault.json").write_text(
            json.dumps({"version": 1, "entry_count": 0, "entries": []}),
            encoding="utf-8")
        trap = self.root / "openclaw-data" / "hub-caretaker" / "latest.json"
        trap.parent.mkdir(parents=True, exist_ok=True)
        trap.write_text("{}", encoding="utf-8")
        cfg = self.make_config("kc.json", managed_by=MANAGED, vault_path=str(vault),
                               caretaker_status_path=str(self.root / "missing-status.json"))
        os.environ["AGENT_KNOWLEDGE_CONFIG"] = str(cfg)
        self.assertIsNone(kg.resolve_caretaker_status_path(vault))

    def test_non_string_vault_path_controlled_error(self):
        for bad_vault in (12345, ["x"], {"p": "x"}, True):
            cfg = self.make_config("kc.json", managed_by=MANAGED, vault_path=bad_vault)
            os.environ["AGENT_KNOWLEDGE_CONFIG"] = str(cfg)
            with self.assertRaises(kg.GatewayError):
                kg.resolve_vault(None)

    def test_receipt_selection_flag_unchanged(self):
        vault = self.make_vault()
        explicit = self.root / "receipts"
        self.assertEqual(kg.receipt_root(vault, str(explicit)), explicit.resolve())
        with self.assertRaises(kg.GatewayError):
            kg.receipt_root(vault, str(vault / "inner"))

    def test_deeply_nested_override_config_fails_closed(self):
        vault = self.make_vault()
        trap = self.root / "trap-status.json"
        trap.write_text("{}", encoding="utf-8")
        self.home_config(caretaker_status_path=str(trap))
        bad = self.root / "deep-config.json"
        bad.write_text("[" * 10000 + "]" * 10000, encoding="utf-8")
        os.environ["AGENT_KNOWLEDGE_CONFIG"] = str(bad)
        with self.assertRaises(kg.GatewayError):
            kg.selected_knowledge_config()
        with self.assertRaises(kg.GatewayError):
            kg.resolve_vault(None)
        self.assertIsNone(kg.resolve_caretaker_status_path(vault))

    def test_unresolvable_override_path_has_controlled_error(self):
        self.home_config(managed_by=MANAGED,
                         vault_path=str(self.make_vault("decoy")))
        poisoned = {"AGENT_KNOWLEDGE_CONFIG": "C:\\nope\0tail.json"}
        with mock.patch.object(kg.os, "environ", poisoned):
            with self.assertRaises(kg.GatewayError):
                kg.selected_knowledge_config()
            with self.assertRaises(kg.GatewayError):
                kg.resolve_vault(None)

    def test_caretaker_selection_error_never_falls_back_home(self):
        vault = self.make_vault()
        trap = self.root / "trap-status.json"
        trap.write_text("{}", encoding="utf-8")
        self.home_config(caretaker_status_path=str(trap))
        cfg = self.make_config("kc.json", managed_by=MANAGED,
                               vault_path=str(vault))
        os.environ["AGENT_KNOWLEDGE_CONFIG"] = str(cfg)
        real = kg.selected_knowledge_config

        def boom():
            raise RuntimeError("simulated selection failure")

        kg.selected_knowledge_config = boom
        try:
            self.assertIsNone(kg.resolve_caretaker_status_path(vault))
        finally:
            kg.selected_knowledge_config = real


if __name__ == "__main__":
    unittest.main()
