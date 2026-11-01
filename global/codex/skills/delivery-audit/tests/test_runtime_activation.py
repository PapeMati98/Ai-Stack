"""Synthetic runtime pin and retained-proof validation without native execution."""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))
import audit_graph as graph
import audit_core as core
from test_runner import packet, prepare, seed_graph


class RuntimeActivation(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ.get("TEAM_TEST_ROOT", str(HERE / ".test-artifacts")))
        self.root = root / ("runtime-activation-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)

    def fixture_pins(self):
        fleet = self.root / "fleet"
        fleet.mkdir()
        payload = fleet / "payload.txt"
        payload.write_bytes(b"synthetic fleet payload")
        rows = [{"path": payload.name, "size": payload.stat().st_size,
                 "sha256": graph.sha_file(payload)}]
        fingerprint = hashlib.sha256((json.dumps(rows, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")).hexdigest()
        graph.write_json(fleet / ".agent-fleet-suite-runtime.json", {
            "managed_by": "agent-fleet-suite-v2-installer", "package_id": "agent-fleet-suite",
            "package_version": "0.9.82", "payload_file_count": 1, "payload_fingerprint": fingerprint})
        script = self.root / "todo_graph.py"
        script.write_text('VERSION = "1.0.1"\ndef inspect_run(): pass\ndef resolve_interrupted(): pass\nCAPABILITIES = ["inspect", "resolve-interrupted", "historical_scout", "deterministic-precheck"]\n', encoding="ascii")
        launcher = self.root / "launcher.bin"
        launcher.write_bytes(b"synthetic launcher")
        return payload, script, launcher, dict(FLEET_INSTALL=fleet, FLEET_FINGERPRINT_SHA=fingerprint,
                                               GRAPH_PATH=script, GRAPH_SHA256=graph.sha_file(script),
                                               HIDDEN_LAUNCH_PATH=launcher, HIDDEN_LAUNCH_SHA=graph.sha_file(launcher))

    def test_synthetic_runtime_pins_and_fleet_tamper(self):
        payload, _, _, pins = self.fixture_pins()
        with patch.multiple(graph, **pins):
            self.assertEqual(graph.verify_runtime_pins(), {"fleet": "present", "graph": "present", "hidden_launch": "present"})
            payload.write_bytes(b"tampered")
            self.assertEqual(graph.verify_runtime_pins()["fleet"], "unavailable")

    def test_graph_and_launcher_tamper_are_refused(self):
        _, script, launcher, pins = self.fixture_pins()
        with patch.multiple(graph, **pins):
            script.write_text("VERSION = 'wrong'\n", encoding="ascii")
            launcher.write_bytes(b"tampered")
            result = graph.verify_runtime_pins()
            self.assertEqual(result["graph"], "fingerprint-mismatch")
            self.assertEqual(result["hidden_launch"], "unavailable")
            self.assertEqual(result["fleet"], "present")

    def test_test_only_acceptance_is_immutable_and_never_native(self):
        p = packet()
        with patch.object(graph, "_excluded_candidates", return_value=[]):
            base, workspace, run_dir, plan = prepare(self.root, p)
        seed_graph(base, workspace, run_dir, plan, p)
        before = {x.relative_to(base).as_posix(): graph.sha_file(x) for x in base.rglob('*') if x.is_file()}
        with patch.object(graph, "run_native", side_effect=AssertionError("must not launch")) as launch:
            report, proof = graph.collect_accepted(base, p, "TEST_ONLY")
            self.assertEqual(proof["verification_class"], "TEST_ONLY")
            self.assertEqual(proof["status"], "ACCEPTED")
            self.assertEqual(before, {x.relative_to(base).as_posix(): graph.sha_file(x) for x in base.rglob('*') if x.is_file()})
            graph.write_json(base / "acceptance.json", proof, immutable=True)
            with self.assertRaisesRegex(ValueError, "AUDIT_IMMUTABLE_INPUT_CHANGED"):
                graph.write_json(base / "acceptance.json", {**proof, "status": "changed"}, immutable=True)
            record = {"status": "COMPLETED", "audit_id": core.audit_id(p), "packet": p, "report": report}
            with self.assertRaisesRegex(ValueError, "AUDIT_TEST_IS_NOT_NATIVE_PROOF"):
                graph.load_accepted(self.root, record)
            launch.assert_not_called()
        self.assertEqual(graph.read_json(base / "acceptance.json"), proof)

if __name__ == "__main__":
    unittest.main()
