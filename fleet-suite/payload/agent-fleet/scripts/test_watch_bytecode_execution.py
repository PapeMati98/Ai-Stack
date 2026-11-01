"""Source-only callback execution, with real Python imports on synthetic data."""
import os
from pathlib import Path
import py_compile
import subprocess
import sys
import unittest
from unittest import mock

import fleet
from test_watch_event import WatchEventTests


class WatchBytecodeExecutionTests(unittest.TestCase):
    def test_ignored_timestamp_cache_is_not_executed(self):
        root, launcher = WatchEventTests().make_runtime()
        runtime = launcher.parent
        module = runtime / "synthetic_dependency.py"
        # Equal-size content and unchanged timestamp make a stale pyc valid.
        module.write_text("VALUE = 'cached'\n", encoding="utf-8")
        stamp = module.stat()
        py_compile.compile(str(module), doraise=True)
        module.write_text("VALUE = 'source'\n", encoding="utf-8")
        os.utime(module, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        (runtime / "office.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
            "import synthetic_dependency\nprint(synthetic_dependency.VALUE)\n",
            encoding="utf-8",
        )
        import json
        marker_path = runtime / ".agent-office-runtime.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["source_fingerprint"] = fleet._watch_office_source_fingerprint(runtime)
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        env = WatchEventTests().safe_environment(root)
        options = {"capture_output": True, "text": True, "timeout": 30, "env": env}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        old = subprocess.run([sys.executable, "-B", "-I", str(runtime / "office.py")], **options)
        self.assertEqual(old.returncode, 0, old.stderr)
        self.assertEqual(old.stdout.strip(), "cached")
        with mock.patch.object(fleet, "office_command_path", return_value=launcher), \
                mock.patch.object(fleet, "office_runtime_is_available", return_value=True), \
                mock.patch.object(fleet, "office_integration_enabled", return_value=True):
            command, reason = fleet._watch_office_runtime_argv()
        self.assertIsNone(reason)
        prefix = Path(next(arg.split("=", 1)[1] for arg in command if arg.startswith("pycache_prefix=")))
        self.assertFalse(prefix.exists())
        actual = subprocess.run(command, **options)
        self.assertEqual(actual.returncode, 0, actual.stderr)
        self.assertEqual(actual.stdout.strip(), "source")
        self.assertFalse(prefix.exists(), "callback must not create a second cache store")

    def test_existing_fresh_prefix_refuses_before_launch(self):
        root, launcher = WatchEventTests().make_runtime()
        from types import SimpleNamespace
        fake = SimpleNamespace(hex="occupied")
        (launcher.parent.parent / ".office-source-only-occupied").mkdir()
        with mock.patch.object(fleet, "office_command_path", return_value=launcher), \
                mock.patch.object(fleet, "office_runtime_is_available", return_value=True), \
                mock.patch.object(fleet, "office_integration_enabled", return_value=True), \
                mock.patch.object(fleet.uuid, "uuid4", return_value=fake):
            command, reason = fleet._watch_office_runtime_argv()
        self.assertIsNone(command)
        self.assertEqual(reason, "office-runtime-unverified")


if __name__ == "__main__":
    unittest.main()
