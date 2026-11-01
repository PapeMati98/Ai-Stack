"""Synthetic tests for the bounded audit closeout hook."""

from __future__ import annotations

import builtins
import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import session_audit_hook as hook


class SessionAuditHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_calls = getattr(builtins, "_session_audit_hook_calls", None)
        self._saved_imports = getattr(builtins, "_session_audit_hook_imports", None)
        builtins._session_audit_hook_calls = []
        builtins._session_audit_hook_imports = []
        self._fake_sources: dict[Path, str] = {}
        self._install_fake_loader()

    def tearDown(self) -> None:
        if self._saved_calls is None:
            del builtins._session_audit_hook_calls
        else:
            builtins._session_audit_hook_calls = self._saved_calls
        if self._saved_imports is None:
            del builtins._session_audit_hook_imports
        else:
            builtins._session_audit_hook_imports = self._saved_imports

    def _install_fake_loader(self) -> None:
        original_spec = importlib.util.spec_from_file_location
        original_is_file = Path.is_file
        sources = self._fake_sources

        class SourceLoader(importlib.abc.Loader):
            def __init__(self, source: str, filename: str) -> None:
                self.source = source
                self.filename = filename

            def create_module(self, spec: object) -> None:
                return None

            def exec_module(self, module: ModuleType) -> None:
                exec(compile(self.source, self.filename, "exec"), module.__dict__)

        def spec_from_file_location(name: str, location: object, *args: object, **kwargs: object):
            filename = Path(location)
            source = sources.get(filename)
            if source is not None:
                return importlib.machinery.ModuleSpec(
                    name, SourceLoader(source, str(filename)), origin=str(filename)
                )
            return original_spec(name, location, *args, **kwargs)

        def is_file(path: Path) -> bool:
            if path in sources:
                return True
            return original_is_file(path)

        self.enterContext(patch.object(importlib.util, "spec_from_file_location", spec_from_file_location))
        self.enterContext(patch.object(Path, "is_file", is_file))

    def _fake_install(self, producer_source: str) -> Path:
        script = Path.cwd() / "_synthetic_installed" / "producer.py"
        self._fake_sources[script] = producer_source
        self._fake_sources[script.parent / "audit_core.py"] = (
            "import builtins\n"
            "builtins._session_audit_hook_imports.append(('audit_core', 'trusted'))\n"
            "marker = 'trusted'\n"
        )
        self._fake_sources[script.parent / "completion.py"] = (
            "import builtins\n"
            "builtins._session_audit_hook_imports.append(('completion', 'trusted'))\n"
            "marker = 'trusted'\n"
        )
        return script

    @staticmethod
    def _receipt() -> dict[str, object]:
        return {
            "action": "record-session",
            "receipt_status": "COMPLETE",
            "outcome": "COMPLETED",
            "project_slug": "demo-project",
            "checkpoint_id": "receipt-checkpoint",
            "metadata": {"stable": [1, 2, 3]},
        }

    @staticmethod
    def _args() -> SimpleNamespace:
        return SimpleNamespace(
            agent="native-agent-id",
            session_id="native-session-id",
            checkpoint_id="checkpoint-from-args",
        )

    def test_incomplete_or_noncompleted_receipts_do_not_load_producer(self) -> None:
        with patch.object(hook, "_call_canonical_producer") as call_producer:
            for changes in (
                {"receipt_status": "PENDING"},
                {"outcome": "BLOCKED"},
                {"action": "inventory"},
            ):
                receipt = self._receipt()
                receipt.update(changes)
                self.assertIs(hook.after_record_session(receipt, self._args()), receipt)
            call_producer.assert_not_called()

    def test_final_fresh_and_retry_receipts_call_once_and_keep_original(self) -> None:
        script = self._fake_install(
            "import builtins\n"
            "import audit_core, completion\n"
            "builtins._session_audit_hook_imports.append(('producer', 'loaded'))\n"
            "def after_hub_closeout(receipt, agent, session_id, project_slug, *, checkpoint_id=None):\n"
            "    builtins._session_audit_hook_calls.append((receipt, agent, session_id, project_slug, checkpoint_id))\n"
            "    receipt['producer_mutation'] = True\n"
        )
        with patch.object(hook, "_canonical_producer_script", return_value=script):
            for _attempt in ("fresh", "retry"):
                receipt = self._receipt()
                original = self._receipt()
                result = hook.after_record_session(receipt, self._args())
                self.assertIs(result, receipt)
                self.assertEqual(receipt, original)

        self.assertEqual(len(builtins._session_audit_hook_calls), 2)
        for snapshot, agent, session_id, project_slug, checkpoint_id in builtins._session_audit_hook_calls:
            self.assertEqual(snapshot["producer_mutation"], True)
            self.assertEqual(agent, "native-agent-id")
            self.assertEqual(session_id, "native-session-id")
            self.assertEqual(project_slug, "demo-project")
            self.assertEqual(checkpoint_id, "checkpoint-from-args")
        self.assertEqual(
            [entry for entry in builtins._session_audit_hook_imports if entry[0] == "producer"],
            [("producer", "loaded"), ("producer", "loaded")],
        )

    def test_producer_exception_and_missing_producer_fail_open(self) -> None:
        script = self._fake_install(
            "def after_hub_closeout(receipt, agent, session_id, project_slug, *, checkpoint_id=None):\n"
            "    receipt['changed'] = True\n"
            "    raise RuntimeError('synthetic producer failure')\n"
        )
        with patch.object(hook, "_canonical_producer_script", return_value=script):
            receipt = self._receipt()
            before = self._receipt()
            self.assertIs(hook.after_record_session(receipt, self._args()), receipt)
            self.assertEqual(receipt, before)

        missing = Path(tempfile.gettempdir()) / "missing-session-audit-producer.py"
        with patch.object(hook, "_canonical_producer_script", return_value=missing):
            receipt = self._receipt()
            self.assertIs(hook.after_record_session(receipt, self._args()), receipt)
            self.assertEqual(receipt, self._receipt())

    def test_canonical_path_ignores_environment_redirect(self) -> None:
        home = Path(tempfile.gettempdir()) / "synthetic-agent-home"
        with patch.object(Path, "home", return_value=home), patch.dict(
            os.environ,
            {"SESSION_AUDIT_PRODUCER_PATH": "C:\\untrusted\\producer.py"},
        ):
            self.assertEqual(
                hook._canonical_producer_script(),
                home / ".agents" / "skills" / "delivery-audit" / "scripts" / "producer.py",
            )

    def test_trusted_sibling_imports_and_host_import_state_are_restored(self) -> None:
        script = self._fake_install(
            "import builtins\n"
            "import audit_core, completion\n"
            "def after_hub_closeout(receipt, agent, session_id, project_slug, *, checkpoint_id=None):\n"
            "    builtins._session_audit_hook_calls.append((audit_core.marker, completion.marker))\n"
        )
        ambient_audit = ModuleType("audit_core")
        ambient_audit.marker = "ambient"
        ambient_completion = ModuleType("completion")
        ambient_completion.marker = "ambient"
        old_audit = sys.modules.get("audit_core")
        old_completion = sys.modules.get("completion")
        sys.modules["audit_core"] = ambient_audit
        sys.modules["completion"] = ambient_completion
        previous_path = sys.path[:]
        previous_meta_path = sys.meta_path[:]
        try:
            with patch.object(hook, "_canonical_producer_script", return_value=script):
                receipt = self._receipt()
                hook.after_record_session(receipt, self._args())
            self.assertEqual(builtins._session_audit_hook_calls, [("trusted", "trusted")])
            self.assertIs(sys.modules["audit_core"], ambient_audit)
            self.assertIs(sys.modules["completion"], ambient_completion)
            self.assertEqual(sys.path, previous_path)
            self.assertEqual(sys.meta_path, previous_meta_path)
        finally:
            if old_audit is None:
                sys.modules.pop("audit_core", None)
            else:
                sys.modules["audit_core"] = old_audit
            if old_completion is None:
                sys.modules.pop("completion", None)
            else:
                sys.modules["completion"] = old_completion

    def test_import_failure_restores_path_and_modules(self) -> None:
        script = self._fake_install("raise RuntimeError('synthetic import failure')\n")
        ambient_audit = ModuleType("audit_core")
        ambient_completion = ModuleType("completion")
        old_audit = sys.modules.get("audit_core")
        old_completion = sys.modules.get("completion")
        sys.modules["audit_core"] = ambient_audit
        sys.modules["completion"] = ambient_completion
        previous_path = sys.path[:]
        previous_meta_path = sys.meta_path[:]
        try:
            with patch.object(hook, "_canonical_producer_script", return_value=script):
                receipt = self._receipt()
                self.assertIs(hook.after_record_session(receipt, self._args()), receipt)
            self.assertIs(sys.modules["audit_core"], ambient_audit)
            self.assertIs(sys.modules["completion"], ambient_completion)
            self.assertEqual(sys.path, previous_path)
            self.assertEqual(sys.meta_path, previous_meta_path)
        finally:
            if old_audit is None:
                sys.modules.pop("audit_core", None)
            else:
                sys.modules["audit_core"] = old_audit
            if old_completion is None:
                sys.modules.pop("completion", None)
            else:
                sys.modules["completion"] = old_completion

    def test_unclassified_producer_result_does_not_change_hub_receipt(self) -> None:
        script = self._fake_install(
            "def after_hub_closeout(receipt, agent, session_id, project_slug, *, checkpoint_id=None):\n"
            "    return 'SKIPPED_UNCLASSIFIED'\n"
        )
        with patch.object(hook, "_canonical_producer_script", return_value=script):
            receipt = self._receipt()
            before = self._receipt()
            self.assertIs(hook.after_record_session(receipt, self._args()), receipt)
            self.assertEqual(receipt, before)


class BytecodeGuardTests(unittest.TestCase):
    def test_trusted_producer_does_not_write_cache_and_restores_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory=Path(temporary)
            (directory/"audit_core.py").write_text("VALUE=1\n",encoding="utf-8")
            (directory/"completion.py").write_text("VALUE=2\n",encoding="utf-8")
            script=directory/"producer.py"
            script.write_text("import audit_core, completion, sys\ndef after_hub_closeout(*args, **kwargs):\n    assert sys.dont_write_bytecode is True\n    return {'status':'SKIPPED'}\n",encoding="utf-8")
            previous=sys.dont_write_bytecode
            sys.dont_write_bytecode=False
            try:
                with patch.object(hook,"_canonical_producer_script",return_value=script):
                    receipt={"action":"record-session","receipt_status":"COMPLETE","outcome":"COMPLETED","project_slug":"fixture"}
                    args=SimpleNamespace(agent="codex",session_id="fixture",checkpoint_id="fixture")
                    self.assertIs(hook.after_record_session(receipt,args),receipt)
                self.assertFalse(sys.dont_write_bytecode)
                self.assertFalse((directory/"__pycache__").exists())
            finally:
                sys.dont_write_bytecode=previous
if __name__ == "__main__":
    unittest.main()
