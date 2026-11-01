"""Claude adapter wiring: enabled+verified uses ClaudeDesktopAdapter."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parent
for extra in (ROOT, ROOT / "transport", ROOT / "automation", ROOT / "native", TESTS):
    text = str(extra)
    if text not in sys.path:
        sys.path.insert(0, text)

import test_runtime as fixture
from openclaw_control.adapters import DisabledAdapter
from runtime.claude_desktop import ClaudeDesktopAdapter
from runtime.config import load_config
from runtime.registry import AdapterRegistry
from runtime.run import _registry


class RuntimeClaudeWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _cfg(self, **over):
        path = fixture._write_cfg(self.tmp.name, **over)
        return load_config(path)

    def test_registry_keeps_verified_claude_adapter(self):
        adapter = ClaudeDesktopAdapter(transport=object())
        registry = AdapterRegistry()
        registry.register("claude-desktop", adapter, verified=True)
        got = registry.get("claude-desktop")
        self.assertIs(got, adapter)
        self.assertIsInstance(got, ClaudeDesktopAdapter)
        self.assertNotIsInstance(got, DisabledAdapter)

    def test_registry_disables_unverified_claude(self):
        adapter = ClaudeDesktopAdapter(transport=object())
        registry = AdapterRegistry()
        registry.register(
            "claude-desktop",
            adapter,
            verified=False,
            reason="claude-unverified",
        )
        got = registry.get("claude-desktop")
        self.assertIsInstance(got, DisabledAdapter)
        self.assertNotIsInstance(got, ClaudeDesktopAdapter)
        self.assertEqual(registry.meta("claude-desktop")["reason"], "claude-unverified")
        self.assertFalse(registry.meta("claude-desktop")["enabled"])
        self.assertFalse(registry.meta("claude-desktop")["verified"])

    def test_runtime_registers_claude_when_enabled_and_verified(self):
        cfg = self._cfg(
            adapters={
                "codex-desktop": {"enabled": False, "threads": {}},
                "claude-desktop": {"enabled": True, "verified": True},
            }
        )
        self.assertTrue(cfg.claude_enabled)
        registry = _registry(cfg, store=None)
        got = registry.get("claude-desktop")
        self.assertIsInstance(got, ClaudeDesktopAdapter)
        self.assertNotIsInstance(got, DisabledAdapter)
        self.assertTrue(registry.meta("claude-desktop")["verified"])

    def test_runtime_keeps_safe_disable_without_flags(self):
        cfg = self._cfg()
        self.assertFalse(cfg.claude_enabled)
        registry = _registry(cfg, store=None)
        got = registry.get("claude-desktop")
        self.assertIsInstance(got, DisabledAdapter)
        self.assertNotIsInstance(got, ClaudeDesktopAdapter)
        meta = registry.meta("claude-desktop")
        self.assertEqual(meta["reason"], "claude-unverified")
        self.assertFalse(meta["enabled"])
        self.assertFalse(meta["verified"])

    def test_runtime_keeps_disable_when_only_enabled(self):
        cfg = self._cfg(
            adapters={
                "codex-desktop": {"enabled": False, "threads": {}},
                "claude-desktop": {"enabled": True, "verified": False},
            }
        )
        self.assertFalse(cfg.claude_enabled)
        registry = _registry(cfg, store=None)
        self.assertIsInstance(registry.get("claude-desktop"), DisabledAdapter)
        self.assertEqual(registry.meta("claude-desktop")["reason"], "claude-unverified")

    def test_codex_desktop_factory_unchanged(self):
        cfg = self._cfg()
        root = Path(self.tmp.name)
        rollout = root / ("rollout-" + fixture.THREAD + ".jsonl")
        rollout.write_text("", encoding="utf-8")
        cfg.codex.enabled = True
        cfg.codex.pipe_path = r"\\.\pipe\fixture"
        cfg.codex.sessions_root = str(root)
        cfg.targets[0].rollout_path = str(rollout.resolve())
        app = fixture.App()
        with patch("codex_desktop.pipe_available", return_value=True), patch(
            "codex_desktop.AppTools", return_value=app
        ) as factory:
            registry = _registry(cfg, store=None)
        factory.assert_called_once()
        self.assertTrue(registry.meta("codex-desktop")["enabled"])
        self.assertNotIsInstance(registry.get("codex-desktop"), DisabledAdapter)
        self.assertIsInstance(registry.get("claude-desktop"), DisabledAdapter)


if __name__ == "__main__":
    unittest.main()
