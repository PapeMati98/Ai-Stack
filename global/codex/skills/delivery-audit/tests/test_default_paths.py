"""Synthetic default-path portability tests; fictional home only, no real state."""
import importlib
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit
import audit_graph
import producer

FAKE_HOME = Path("/fictional/home/contractor-user")

REL = {
    ("audit_graph", "GRAPH_PATH"): (".agents", "skills", "agent-graph", "scripts", "todo_graph.py"),
    ("audit_graph", "FLEET_INSTALL"): (".agents", "runtime", "agent-fleet-suite", "lean-0.9.82"),
    ("audit_graph", "FLEET_RUNS_ROOT"): (".agents", "agent-fleet", "runs"),
    ("audit_graph", "HIDDEN_LAUNCH_PATH"): (".agents", "runtime", "hidden-launch", "1.0.3", "HiddenLaunch.exe"),
    ("audit", "OFFICE_ENTRY"): (".agents", "runtime", "agent-office", "office.py"),
    ("audit", "HELPDESK_ENTRY"): (".agents", "skills", "rsi-helpdesk", "scripts", "helpdesk.py"),
    ("producer", "FLEET_RUN_LEDGER"): (".agents", "agent-fleet", "runs"),
    ("producer", "INSTALLED_AUDIT_CLI"): (".agents", "skills", "delivery-audit", "scripts", "audit.py"),
    ("producer", "AUDIT_STATE_DIR"): (".agents", "audits", "delivery-audit"),
}
MODULES = {"audit_graph": audit_graph, "audit": audit, "producer": producer}


class DefaultPathsPortable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._home = mock.patch("pathlib.Path.home", return_value=FAKE_HOME)
        cls._home.start()
        for mod in (audit_graph, audit, producer):
            importlib.reload(mod)

    @classmethod
    def tearDownClass(cls):
        cls._home.stop()
        for mod in (audit_graph, audit, producer):
            importlib.reload(mod)

    def test_nine_defaults_derive_from_current_home(self):
        self.assertEqual(len(REL), 9)
        for (mod_name, attr), parts in REL.items():
            with self.subTest(default=f"{mod_name}.{attr}"):
                value = getattr(MODULES[mod_name], attr)
                self.assertEqual(value, FAKE_HOME.joinpath(*parts))
                self.assertNotIn("mateu", str(value))

    def test_derived_office_config_follows_entry(self):
        self.assertEqual(audit.OFFICE_CONFIG, audit.OFFICE_ENTRY.parent / "office-remote.json")

    def test_versions_and_trusted_pins_unchanged(self):
        self.assertEqual(audit_graph.FLEET_INSTALL.name, "lean-0.9.82")
        self.assertEqual(audit_graph.GRAPH_VERSION, "1.0.1")
        self.assertEqual(audit_graph.GRAPH_SHA256, "082162b568d9605d75203389d2de7a813b381a823950792abb3d14a750d061b8")
        self.assertEqual(audit_graph.LEGACY_CONTROLLER_SHA256,
                         "6102dff3492605d31d4fb3063340885bb0991c92cf22c316370a7b7cb9021bf6")
        self.assertEqual(audit_graph.FLEET_FINGERPRINT_SHA,
                         "696d5bb1f7719719de02739c328ac81d023654e7eb007dfc5c247dfb69e85d1b")
        self.assertEqual(audit_graph.HIDDEN_LAUNCH_SHA,
                         "62fef0d9763aa674d167d508f68d49f8d69277f22ae1443baef6ff361dbebf48")
        self.assertEqual(audit.HIDDEN_LAUNCH_SHA, audit_graph.HIDDEN_LAUNCH_SHA)
        self.assertEqual(audit.FLEET_INSTALL, audit_graph.FLEET_INSTALL)
        self.assertEqual(audit.POWERSHELL,
                         Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"))
        self.assertEqual(producer.ENQUEUE_TIMEOUT_SECONDS, 8)


if __name__ == "__main__":
    unittest.main()
