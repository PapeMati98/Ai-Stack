from __future__ import annotations

import json
import hashlib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OWNERSHIP = REPO_ROOT / "global" / "codex" / "skills-ownership.json"
BACKUP = REPO_ROOT / "global" / "codex" / "shared-skills-backup.json"
ROOT_GITIGNORE = REPO_ROOT / ".gitignore"
RUNTIME = "%USERPROFILE%\\.agents\\coord"
DEPLOY_STATE = "%USERPROFILE%\\.agents\\agent-office-deploy"
RUNTIME_CODE = "%USERPROFILE%\\.agents\\runtime\\agent-office"


class OwnershipBackupTests(unittest.TestCase):
    def test_canonical_ownership_record(self) -> None:
        payload = json.loads(OWNERSHIP.read_text(encoding="utf-8"))
        trees = payload.get("shared_trees")
        self.assertIsInstance(trees, list)
        office = next(item for item in trees if item.get("name") == "agent-office")
        self.assertEqual(office["path"], "shared/agent-office")
        self.assertEqual(office["kind"], "tree")
        self.assertEqual(office["runtime_state"], RUNTIME)
        self.assertIn(RUNTIME, office.get("exclude_from_git", []))
        self.assertIn(RUNTIME, office.get("exclude_from_backup", []))
        self.assertIn(DEPLOY_STATE, office.get("exclude_from_git", []))
        self.assertIn(DEPLOY_STATE, office.get("exclude_from_backup", []))
        self.assertIn(RUNTIME_CODE, office.get("exclude_from_git", []))
        self.assertIn(RUNTIME_CODE, office.get("exclude_from_backup", []))

    def test_dedicated_manager_contract_matches_derived_skill_source(self) -> None:
        payload = json.loads(OWNERSHIP.read_text(encoding="utf-8"))
        contract = payload["manager_contracts"]["agent-office-installer"]
        skill = REPO_ROOT / "shared" / "agent-office" / "skill" / "agent-office" / "SKILL.md"
        data = skill.read_bytes()
        file_hash = hashlib.sha256(data).hexdigest()
        material = f"SKILL.md\0{len(data)}\0{file_hash}\n".encode("utf-8")
        source_fingerprint = hashlib.sha256(material).hexdigest()
        self.assertEqual(contract["marker_name"], ".agent-office-managed.json")
        self.assertEqual(contract["marker_fingerprint_field"], "source_fingerprint")
        self.assertEqual(contract["marker_fingerprint"], source_fingerprint)
        self.assertEqual(contract["receipt_fingerprint_field"], "skill_fingerprint")
        self.assertEqual(contract["receipt_fingerprint"], source_fingerprint)
        self.assertEqual(contract["release_root"], "%REPO_ROOT%\\shared\\agent-office")
        entry = next(item for item in payload["external_managed"] if item["name"] == "agent-office")
        self.assertEqual(entry["managed_by"], "agent-office-installer")
        self.assertEqual(entry["target_state"]["kimi-desktop"], "absent")
        self.assertEqual(entry["target_state"]["codex-direct"], "absent")
        self.assertTrue(all(entry["target_state"][name] == "managed" for name in (
            "shared-agents", "claude-code", "antigravity", "grok-cli", "qwen-code"
        )))

    def test_backup_allowlist_excludes_runtime_state(self) -> None:
        payload = json.loads(BACKUP.read_text(encoding="utf-8"))
        allowlist = payload.get("source_allowlist")
        self.assertIsInstance(allowlist, list)
        office = next(item for item in allowlist if item.get("source_id") == "shared-agent-office")
        self.assertEqual(office["source"], "shared/agent-office")
        self.assertEqual(office["kind"], "tree")
        self.assertIn(RUNTIME, office.get("exclude", []))
        self.assertIn(RUNTIME, payload.get("exclude", []))

    def test_agent_office_release_root_is_admitted_by_gitignore(self) -> None:
        rules = ROOT_GITIGNORE.read_text(encoding="utf-8").splitlines()
        self.assertIn("!/shared/agent-office/**", rules)

    def test_sqlite_sidecars_are_excluded_from_git(self) -> None:
        rules = ROOT_GITIGNORE.read_text(encoding="utf-8").splitlines()
        self.assertIn("**/*.db-wal", rules)
        self.assertIn("**/*.db-shm", rules)
