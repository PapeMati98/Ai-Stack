"""Config tests."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from caretaker.config import ConfigError, load_config


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EXAMPLE = ROOT / "config.example.json"


def synthetic_fallback(plugin_root):
    """Validator-only fixture; the synthetic plugin path is never executed."""
    return {
        "kind": "openclaw_argv", "executable": "synthetic-executable", "argv": [],
        "allowed_backends": ["muse-code"], "allowed_models": ["muse-spark-1.3"],
        "forbidden_backends": ["http-meta", "meta-http"], "subscription_only": True,
        "run_config_inline": {
            "agents": {"defaults": {"model": {"primary": "muse-code/muse-spark-1.3", "fallbacks": []},
                                    "skipBootstrap": True, "heartbeat": {"every": "0m"}}},
            "telemetry": {"enabled": False}, "update": {"checkOnStart": False},
            "models": {"providers": {}}, "mcp": {"servers": {}},
            "tools": {"profile": "full", "allow": [], "deny": ["*"],
                      "exec": {"mode": "deny"}, "fs": {"workspaceOnly": True}, "swarm": False,
                      "web": {"search": {"enabled": False}, "fetch": {"enabled": False}}},
            "plugins": {"enabled": True, "allow": ["muse-code"],
                        "entries": {"muse-code": {"enabled": True}},
                        "load": {"paths": [plugin_root.as_posix()]}},
            "skills": {"allowBundled": [], "load": {"watch": False, "extraDirs": []}},
        },
    }


class ConfigTests(unittest.TestCase):

    def test_load_example(self):
        cfg = load_config(str(EXAMPLE))
        self.assertEqual(cfg.schema_version, 1)
        self.assertTrue(cfg.inputs)
        self.assertIn("codex", cfg.allowlist.tools)
        self.assertEqual(cfg.allowlist.max_excerpt_bytes, 400)
        self.assertEqual(cfg.primary.kind, "openclaw_argv")
        self.assertIsNone(cfg.fallback)
        self.assertEqual(cfg.hub.min_health, "pass")
        self.assertEqual(cfg.hub.min_lint, "pass")
        self.assertTrue(cfg.hub.require_context)

    def test_load_synthetic_opt_in_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "muse-code" / "0.1.0"
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = d
            raw["fallback"] = synthetic_fallback(root)
            p = Path(d) / "c.json"
            p.write_text(json.dumps(raw), "utf-8")
            with patch("caretaker.config._muse_plugin_root", return_value=root):
                cfg = load_config(str(p))
            self.assertIsNotNone(cfg.fallback)
            self.assertEqual(cfg.fallback.allowed_backends, ["muse-code"])
            self.assertEqual(cfg.fallback.allowed_models, ["muse-spark-1.3"])
            self.assertIn("http-meta", cfg.fallback.forbidden_backends)
            self.assertIn("meta-http", cfg.fallback.forbidden_backends)
            self.assertTrue(cfg.fallback.subscription_only)

    def test_reject_missing(self):
        with self.assertRaises(ConfigError):
            load_config("nope.json")

    def test_reject_bad_schema_version(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"schema_version": 99}), "utf-8")
            with self.assertRaises(ConfigError):
                load_config(str(p))

    def test_reject_fallback_without_muse(self):
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = d
            root = Path(d) / "muse-code" / "0.1.0"
            raw["fallback"] = synthetic_fallback(root)
            p = Path(d) / "c.json"
            p.write_text(json.dumps(raw), "utf-8")
            with patch("caretaker.config._muse_plugin_root", return_value=root):
                load_config(str(p))
                raw["fallback"]["allowed_models"] = ["other"]
                p.write_text(json.dumps(raw), "utf-8")
                with self.assertRaises(ConfigError):
                    load_config(str(p))

    def test_reject_fallback_without_subscription_only(self):
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = d
            root = Path(d) / "muse-code" / "0.1.0"
            raw["fallback"] = synthetic_fallback(root)
            p = Path(d) / "c.json"
            p.write_text(json.dumps(raw), "utf-8")
            with patch("caretaker.config._muse_plugin_root", return_value=root):
                load_config(str(p))
                raw["fallback"]["subscription_only"] = False
                p.write_text(json.dumps(raw), "utf-8")
                with self.assertRaises(ConfigError):
                    load_config(str(p))

    def test_reject_primary_advertising_muse(self):
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = d
            root = Path(d) / "muse-code" / "0.1.0"
            raw["fallback"] = synthetic_fallback(root)
            p = Path(d) / "c.json"
            p.write_text(json.dumps(raw), "utf-8")
            with patch("caretaker.config._muse_plugin_root", return_value=root):
                load_config(str(p))
                raw["primary"]["argv"] = ["agent", "exec", "--model",
                                          "muse-code/muse-spark-1.3"]
                p.write_text(json.dumps(raw), "utf-8")
                with self.assertRaises(ConfigError):
                    load_config(str(p))

    def test_excerpt_limit_accepts_16000_and_rejects_16385(self):
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(EXAMPLE.read_text("utf-8"))
            raw["state_dir"] = d
            raw["allowlist"]["max_body_bytes"] = 16_384
            raw["allowlist"]["max_excerpt_bytes"] = 16_000
            p = Path(d) / "c.json"
            p.write_text(json.dumps(raw), "utf-8")

            cfg = load_config(str(p))
            self.assertEqual(cfg.allowlist.max_excerpt_bytes, 16_000)

            raw["allowlist"]["max_excerpt_bytes"] = 16_385
            p.write_text(json.dumps(raw), "utf-8")
            with self.assertRaises(ConfigError):
                load_config(str(p))


if __name__ == "__main__":
    unittest.main()
