#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("obsidian_hub.py")
SPEC = importlib.util.spec_from_file_location("obsidian_hub", MODULE_PATH)
assert SPEC and SPEC.loader
obsidian_hub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(obsidian_hub)


class ObsidianHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.wiki = self.vault / "wiki"
        self.wiki.mkdir(parents=True)
        (self.vault / "knowledge-vault.json").write_text("{}\n", encoding="utf-8")
        (self.wiki / "index.md").write_text("# Index\n", encoding="utf-8")
        (self.wiki / "overview.base").write_text("views: []\n", encoding="utf-8")
        self.knowledge_config = self.root / "knowledge-config.json"
        self.knowledge_config.write_text(
            json.dumps(
                {
                    "managed_by": "shared-agent-knowledge-hub",
                    "vault_path": str(self.vault),
                }
            ),
            encoding="utf-8",
        )
        self.obsidian_config = self.root / "obsidian.json"
        self.obsidian_config.write_text(
            json.dumps(
                {
                    "cli": True,
                    "vaults": {
                        "test-vault-id": {
                            "path": str(self.vault),
                            "open": True,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def namespace(self, action: str, **overrides: object) -> argparse.Namespace:
        values = {
            "action": action,
            "query": None,
            "path": None,
            "name": None,
            "view": None,
            "extension": None,
            "output_format": "json",
            "limit": 20,
            "case_sensitive": False,
            "timeout": 45,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_resolves_only_managed_vault(self) -> None:
        self.assertEqual(
            obsidian_hub.resolve_vault(self.knowledge_config),
            self.vault.resolve(),
        )

    def test_matches_registered_vault_and_cli_flag(self) -> None:
        cli_enabled, vault_id = obsidian_hub.obsidian_state(self.obsidian_config, self.vault)
        self.assertTrue(cli_enabled)
        self.assertEqual(vault_id, "test-vault-id")

    def test_rejects_untrusted_and_ambiguous_paths(self) -> None:
        bad_paths = [
            "raw/source.md",
            "proposals/codex/item.md",
            "wiki/../raw/source.md",
            "wiki/*.md",
            "wiki/index.md:stream",
            str((self.wiki / "index.md").resolve()),
        ]
        for bad_path in bad_paths:
            with self.subTest(path=bad_path):
                with self.assertRaises(obsidian_hub.BridgeError):
                    obsidian_hub.trusted_relative_path(self.vault, bad_path, {".md"})

    def test_rejects_symlink_when_supported(self) -> None:
        link = self.wiki / "linked.md"
        try:
            link.symlink_to(self.wiki / "index.md")
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable")
        with self.assertRaises(obsidian_hub.BridgeError):
            obsidian_hub.trusted_relative_path(self.vault, "wiki/linked.md", {".md"})

    def test_rejects_hardlink_to_file_outside_wiki(self) -> None:
        outside = self.vault / "outside.md"
        outside.write_text("not trusted\n", encoding="utf-8")
        link = self.wiki / "hardlinked.md"
        try:
            os.link(outside, link)
        except OSError:
            self.skipTest("Hardlink creation is unavailable")
        with self.assertRaises(obsidian_hub.BridgeError):
            obsidian_hub.trusted_relative_path(self.vault, "wiki/hardlinked.md", {".md"})

    def test_obsidian_config_env_override_is_ignored(self) -> None:
        with mock.patch.dict(os.environ, {"OBSIDIAN_CONFIG_PATH": str(self.root / "fake.json")}):
            self.assertNotEqual(
                obsidian_hub.default_obsidian_config(),
                self.root / "fake.json",
            )

    def test_secret_like_environment_names_are_removed(self) -> None:
        values = {
            "SAFE_VALUE": "ok",
            "OPEN_ROUTER_KEY": "redacted",
            "AUTHORIZATION": "redacted",
            "BEARER_HEADER": "redacted",
        }
        with mock.patch.dict(os.environ, values, clear=True):
            scrubbed = obsidian_hub.scrubbed_environment()
        self.assertEqual(scrubbed, {"SAFE_VALUE": "ok"})

    def test_search_is_pinned_to_wiki_and_vault_id(self) -> None:
        args = self.namespace("search", query="shared knowledge", limit=12)
        command = obsidian_hub.build_cli_args(args, self.vault, "test-vault-id")
        self.assertEqual(command[0], "vault=test-vault-id")
        self.assertEqual(command[1], "search")
        self.assertIn("path=wiki", command)
        self.assertIn("limit=12", command)

    def test_base_query_accepts_only_base_under_wiki(self) -> None:
        args = self.namespace("base-query", path="wiki/overview.base", view="Main")
        command = obsidian_hub.build_cli_args(args, self.vault, "test-vault-id")
        self.assertEqual(
            command,
            [
                "vault=test-vault-id",
                "base:query",
                "path=wiki/overview.base",
                "format=json",
                "view=Main",
            ],
        )

    def test_no_write_command_is_exposed(self) -> None:
        forbidden = {
            "append",
            "command",
            "create",
            "delete",
            "eval",
            "move",
            "plugin:install",
            "prepend",
            "property:set",
            "rename",
            "restart",
            "sync:restore",
        }
        self.assertTrue(forbidden.isdisjoint(obsidian_hub.READ_ONLY_ACTIONS))


if __name__ == "__main__":
    unittest.main()
