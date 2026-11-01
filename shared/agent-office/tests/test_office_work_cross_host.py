"""Host-qualified checkout claims stay lexical outside the local host."""

from __future__ import annotations

import hashlib
import os
from unittest.mock import patch

from support import CoordTestCase

from coordfs.errors import CoordError
from coordfs.work import WorkBoard


SHA = "b" * 40
INTENT = "parent will perform the final integration review"


class CrossHostWorkTests(CoordTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.work = WorkBoard(home=self.home, now=lambda: self.clock[0], local_host="local-host")
        self.local_checkout = self.cwd / "checkout"
        self.local_checkout.mkdir()

    def register(
        self,
        task_id: str,
        checkout: str,
        *,
        checkout_host: str | None = None,
    ) -> dict:
        return self.work.register_task(
            project="agent-office",
            task_id=task_id,
            checkout=checkout,
            checkout_host=checkout_host,
            scopes=["shared/agent-office/coordfs/"],
            base_sha=SHA,
            commit_intent=INTENT,
        )

    def test_foreign_windows_and_posix_paths_are_lexical_claims(self) -> None:
        with patch("coordfs.work._checkout", side_effect=AssertionError("foreign native validation")):
            windows = self.register("windows-remote", r"D:\Remote\Repo", checkout_host="windows-host")
            posix = self.register("posix-remote", "/srv/remote/repo", checkout_host="linux-host")
            windows_listed = self.work.list_tasks(checkout=r"D:\Remote\Repo", checkout_host="windows-host")
            posix_listed = self.work.list_tasks(checkout="/srv/remote/repo", checkout_host="linux-host")

        self.assertEqual(windows["task"]["schema_version"], 2)
        self.assertEqual(windows["task"]["checkout"], r"D:\Remote\Repo")
        self.assertEqual(
            windows["task"]["checkout_claim"],
            {"host": "windows-host", "path_style": "windows", "path": r"d:\remote\repo"},
        )
        self.assertEqual(
            posix["task"]["checkout_claim"],
            {"host": "linux-host", "path_style": "posix", "path": "/srv/remote/repo"},
        )
        self.assertEqual([task["task_id"] for task in windows_listed["tasks"]], ["windows-remote"])
        self.assertEqual([task["task_id"] for task in posix_listed["tasks"]], ["posix-remote"])
        windows_updated = self.work.update_task(
            project="agent-office",
            task_id="windows-remote",
            checkout=r"D:\Remote\Repo",
            checkout_host="windows-host",
            status="ready",
        )
        self.assertEqual(windows_updated["task"]["status"], "ready")
        posix_updated = self.work.update_task(
            project="agent-office",
            task_id="posix-remote",
            checkout="/srv/remote/repo",
            checkout_host="linux-host",
            status="ready",
        )
        self.assertEqual(posix_updated["task"]["status"], "ready")

    def test_same_spelling_on_distinct_hosts_cannot_rebind_a_task(self) -> None:
        checkout = "/srv/shared/repo"
        self.register("host-a", checkout, checkout_host="host-a")
        self.register("host-b", checkout, checkout_host="host-b")
        self.register("local-pinned", str(self.local_checkout), checkout_host="local-host")

        listed = self.work.list_tasks(checkout=checkout, checkout_host="host-a")
        self.assertEqual([task["task_id"] for task in listed["tasks"]], ["host-a"])
        with self.assertRaises(CoordError) as mismatch:
            self.work.update_task(
                project="agent-office",
                task_id="host-a",
                checkout=checkout,
                checkout_host="host-b",
            )
        self.assertEqual(mismatch.exception.code, "binding_mismatch")
        with self.assertRaises(CoordError) as other_path:
            self.work.update_task(
                project="agent-office",
                task_id="host-a",
                checkout="/srv/other/repo",
                checkout_host="host-a",
            )
        self.assertEqual(other_path.exception.code, "binding_mismatch")
        with self.assertRaises(CoordError) as omitted_host:
            self.work.update_task(
                project="agent-office",
                task_id="local-pinned",
                checkout=str(self.local_checkout),
            )
        self.assertEqual(omitted_host.exception.code, "binding_mismatch")

        ordinary = self.work.update_task(
            project="agent-office",
            task_id="host-a",
            checkout_host="host-b",
            status="ready",
        )
        self.assertEqual(ordinary["task"]["status"], "ready")

        matched = self.work.update_task(
            project="agent-office",
            task_id="host-a",
            checkout=checkout,
            checkout_host="host-a",
            status="complete",
        )
        self.assertEqual(matched["task"]["status"], "complete")

        unqualified = self.work.list_tasks(checkout=str(self.local_checkout))
        self.assertEqual(unqualified["tasks"], [])

    def test_explicit_local_host_retains_native_existing_directory_protection(self) -> None:
        with self.assertRaises(CoordError) as error:
            self.register(
                "missing-local",
                str(self.cwd / "does-not-exist"),
                checkout_host="local-host",
            )
        self.assertEqual(error.exception.code, "invalid_checkout")

        registered = self.register("local", str(self.local_checkout), checkout_host="local-host")
        self.assertEqual(registered["task"]["schema_version"], 2)
        self.assertEqual(registered["task"]["checkout"], os.path.normcase(str(self.local_checkout.resolve())))

    def test_schema1_is_readable_then_migrates_with_a_byte_exact_backup(self) -> None:
        legacy = self.register("legacy", str(self.local_checkout))
        self.assertEqual(legacy["task"]["schema_version"], 1)
        path, _ = self.work._paths("agent-office", "legacy")
        original = path.read_bytes()
        self.assertEqual(self.work.get_task(project="agent-office", task_id="legacy")["task"]["schema_version"], 1)
        with self.assertRaises(CoordError) as unmigrated:
            self.work.update_task(
                project="agent-office",
                task_id="legacy",
                checkout=str(self.local_checkout),
                checkout_host="local-host",
            )
        self.assertEqual(unmigrated.exception.code, "migration_required")
        legacy_filter = self.work.list_tasks(
            checkout=str(self.local_checkout),
            checkout_host="local-host",
        )
        self.assertEqual(legacy_filter["tasks"], [])
        self.assertEqual(legacy_filter["warnings"][0]["code"], "migration_required")

        migrated = self.work.migrate_legacy(source_host="local-host")
        self.assertEqual(migrated["count"], 1)
        self.assertEqual(migrated["hashes"], [hashlib.sha256(original).hexdigest()])
        backup = self.work._directory() / migrated["backups"][0]
        self.assertEqual(backup.read_bytes(), original)
        task = self.work.get_task(project="agent-office", task_id="legacy")["task"]
        self.assertEqual(task["schema_version"], 2)
        self.assertEqual(task["checkout_claim"]["host"], "local-host")

        repeated = self.work.migrate_legacy(source_host="local-host")
        self.assertEqual(repeated["count"], 0)
        self.assertEqual(repeated["backups"], [])
        self.assertEqual(repeated["hashes"], [])

    def test_failed_migration_never_overwrites_legacy_bytes(self) -> None:
        self.register("legacy-failure", str(self.local_checkout))
        path, _ = self.work._paths("agent-office", "legacy-failure")
        original = path.read_bytes()
        with patch.object(self.work, "_write_locked", side_effect=CoordError("write failed", code="busy")):
            with self.assertRaises(CoordError) as failure:
                self.work.migrate_legacy(source_host="local-host")
        self.assertEqual(failure.exception.code, "busy")
        self.assertEqual(path.read_bytes(), original)
        backups = list(self.work._directory().glob("*.legacy-backup"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
