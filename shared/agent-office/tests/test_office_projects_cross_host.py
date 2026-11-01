"""Host-qualified project checkout claims and offline legacy migration.

Synthetic acceptance coverage for schema-2 checkout claims in
``coordfs.projects``.  Foreign hosts and foreign path spellings are exercised
purely lexically: tests patch ``Path.resolve``/``Path.lstat``/``Path.is_dir``
to prove no foreign filesystem is ever inspected.  Run directly with
``python test_office_projects_cross_host.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs import checkout_claims
from coordfs.projects import (
    ProjectRegistry,
    ProjectReject,
    ProjectStream,
    ProjectTransport,
    canonicalize_checkout,
)

LOCAL = "test-local-host"
FOREIGN_HOST = "remote-linux-1" if os.name == "nt" else "remote-win-1"
SECOND_FOREIGN_HOST = "remote-linux-2" if os.name == "nt" else "remote-win-2"
LOCAL_STYLE = "windows" if os.name == "nt" else "posix"
# A foreign spelling is always written in its lexical canonical form so the
# dual-read path can validate it without native normalization.
FOREIGN_CHECKOUT = "/srv/hermes/repo" if os.name == "nt" else "d:\\repo\\work"
FOREIGN_STYLE = "posix" if os.name == "nt" else "windows"


def _looks_foreign(text: str) -> bool:
    """True for path spellings belonging to the foreign host in these tests."""
    if os.name == "nt":
        return text.startswith("/srv/")
    return bool(re.match(r"^[A-Za-z]:[\\/]", text))


def _no_foreign_fs():
    """Fail resolve/lstat/is_dir on foreign spellings; pass local paths through."""
    real_resolve = Path.resolve
    real_lstat = Path.lstat
    real_is_dir = Path.is_dir

    def guard(real, kind):
        def wrapped(self, *args, **kwargs):
            if _looks_foreign(str(self)):
                raise AssertionError(f"foreign {kind}: {self}")
            return real(self, *args, **kwargs)

        return wrapped

    return (
        patch("pathlib.Path.resolve", guard(real_resolve, "resolve")),
        patch("pathlib.Path.lstat", guard(real_lstat, "lstat")),
        patch("pathlib.Path.is_dir", guard(real_is_dir, "is_dir")),
    )


def _write_legacy_registry(home: Path, project_id: str, checkout: str) -> bytes:
    """Hand-craft a populated schema-1 registry file, as a legacy binary wrote it."""
    root = home / "office" / "projects"
    root.mkdir(parents=True, exist_ok=True)
    event = {
        "sequence": 1,
        "at": "2026-09-28T00:00:00Z",
        "type": "project_registered",
        "project_id": project_id,
    }
    record = {
        "project_id": project_id,
        "checkout": checkout,
        "aliases": [],
        "streams": {},
        "runtimes": {},
        "deliveries": {},
        "received": {},
        "audits": [],
        "activity": [],
        "created_event": event,
        "updated_event": event,
    }
    state = {"schema": 1, "next_event_seq": 1, "projects": {project_id: record}}
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path = root / "registry.json"
    path.write_text(payload, encoding="utf-8")
    return path.read_bytes()


class CrossHostProjectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="office-cross-host-test-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "synthetic-office-home"
        self.clock = [1_700_000_000.0]
        self.registry = ProjectRegistry(
            home=self.home, now=lambda: self.clock[0], local_host=LOCAL
        )
        self.checkouts = {
            name: self.root / "checkouts" / name for name in ("alpha", "beta", "wt2")
        }
        for checkout in self.checkouts.values():
            checkout.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def local_checkout(self, name: str) -> str:
        return str(self.checkouts[name])

    def transport(
        self,
        project: str,
        harness: str,
        session: str,
        *,
        host: str,
        ack_ttl_s: int = 10,
    ) -> ProjectTransport:
        return ProjectTransport(
            home=self.home,
            project_id=project,
            harness=harness,
            host=host,
            session_id=session,
            now=lambda: self.clock[0],
            ack_ttl_s=ack_ttl_s,
        )

    @staticmethod
    def stream(transport: ProjectTransport, suffix: str = "one") -> ProjectStream:
        return transport.stream(
            task_id=f"task-{suffix}",
            thread_id=f"thread-{suffix}",
            run_id=f"run-{suffix}",
        )

    @staticmethod
    def bodies(payload: dict) -> list[str]:
        return [item["untrusted_message"]["body"] for item in payload["messages"]]

    def registry_schema(self) -> int:
        return json.loads(self.registry.path.read_text(encoding="utf-8"))["schema"]


class HostClaimRegistrationTests(CrossHostProjectTestCase):
    def test_same_spelling_on_two_hosts_binds_one_project_without_collision(self) -> None:
        self.registry.register("alpha", FOREIGN_CHECKOUT, checkout_host=FOREIGN_HOST)
        record = self.registry.register(
            "alpha", FOREIGN_CHECKOUT, checkout_host=SECOND_FOREIGN_HOST
        )
        keys = [checkout_claims.claim_key(item) for item in record["checkout_claims"]]
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2, "same spelling on two hosts must not collide")
        self.assertEqual(
            {item["host"] for item in record["checkout_claims"]},
            {FOREIGN_HOST, SECOND_FOREIGN_HOST},
        )
        self.assertEqual(
            record["checkout"],
            record["checkout_claims"][0]["path"],
            "legacy checkout field is immutable provenance of the first claim",
        )
        self.assertEqual(
            self.registry_schema(), 2, "host-aware writes must use explicit schema 2"
        )

    def test_same_claim_triple_cannot_move_to_another_project(self) -> None:
        self.registry.register("alpha", FOREIGN_CHECKOUT, checkout_host=FOREIGN_HOST)
        with self.assertRaises(ProjectReject) as refusal:
            self.registry.register("beta", FOREIGN_CHECKOUT, checkout_host=FOREIGN_HOST)
        self.assertEqual(refusal.exception.code, "checkout_bound")
        # The same spelling on a third host is a different claim and stays free.
        record = self.registry.register(
            "beta", FOREIGN_CHECKOUT, checkout_host=SECOND_FOREIGN_HOST
        )
        self.assertEqual(record["checkout_claims"][0]["host"], SECOND_FOREIGN_HOST)

    def test_local_claim_triple_conflicts_with_legacy_and_host_records(self) -> None:
        self.registry.register("alpha", self.local_checkout("alpha"), checkout_host=LOCAL)
        with self.assertRaises(ProjectReject) as host_refusal:
            self.registry.register(
                "beta", self.local_checkout("alpha"), checkout_host=LOCAL
            )
        self.assertEqual(host_refusal.exception.code, "checkout_bound")
        with self.assertRaises(ProjectReject) as legacy_refusal:
            self.registry.register("beta", self.checkouts["alpha"])
        self.assertEqual(
            legacy_refusal.exception.code,
            "checkout_bound",
            "a legacy registration must not rebind a host-claimed local checkout",
        )

    def test_multiple_task_worktrees_on_one_host_are_additive_and_deduplicated(self) -> None:
        self.registry.register("alpha", self.local_checkout("alpha"), checkout_host=LOCAL)
        record = self.registry.register(
            "alpha", self.local_checkout("wt2"), checkout_host=LOCAL
        )
        self.assertEqual(len(record["checkout_claims"]), 2)
        again = self.registry.register(
            "alpha", self.local_checkout("wt2"), checkout_host=LOCAL
        )
        self.assertEqual(
            len(again["checkout_claims"]), 2, "exact claim triples are deduplicated"
        )
        keys = [checkout_claims.claim_key(i) for i in again["checkout_claims"]]
        self.assertEqual(len(set(keys)), 2)
        # Aliases still merge on a repeated registration.
        aliased = self.registry.register(
            "alpha", self.local_checkout("alpha"), checkout_host=LOCAL, aliases=("a",)
        )
        self.assertEqual(aliased["aliases"], ["a"])

    def test_foreign_claim_registration_never_touches_the_foreign_filesystem(self) -> None:
        resolve, lstat, is_dir = _no_foreign_fs()
        with resolve, lstat, is_dir:
            record = self.registry.register(
                "alpha", FOREIGN_CHECKOUT, checkout_host=FOREIGN_HOST
            )
            self.assertEqual(
                record["checkout_claims"],
                [
                    {
                        "host": FOREIGN_HOST,
                        "path_style": FOREIGN_STYLE,
                        "path": FOREIGN_CHECKOUT,
                    }
                ],
            )
            reread = self.registry.resolve("alpha")
        self.assertEqual(reread["checkout_claims"], record["checkout_claims"])

    def test_invalid_host_and_traversal_are_refused_on_both_host_kinds(self) -> None:
        with self.assertRaises(ProjectReject) as bad_host:
            self.registry.register("alpha", FOREIGN_CHECKOUT, checkout_host="BAD HOST")
        self.assertEqual(bad_host.exception.code, "invalid_checkout_host")
        with self.assertRaises(ProjectReject) as local_traversal:
            self.registry.register(
                "alpha", str(self.root / "x" / ".." / "y"), checkout_host=LOCAL
            )
        self.assertEqual(local_traversal.exception.code, "invalid_checkout")
        with self.assertRaises(ProjectReject) as local_relative:
            self.registry.register("alpha", "relative-checkout", checkout_host=LOCAL)
        self.assertEqual(local_relative.exception.code, "invalid_checkout")
        resolve, lstat, is_dir = _no_foreign_fs()
        with resolve, lstat, is_dir:
            foreign_traversal = (
                "/srv/../escape" if os.name == "nt" else "d:/repo/../escape"
            )
            with self.assertRaises(ProjectReject) as foreign_refusal:
                self.registry.register(
                    "alpha", foreign_traversal, checkout_host=FOREIGN_HOST
                )
        self.assertEqual(foreign_refusal.exception.code, "invalid_checkout")

    def test_default_local_host_is_the_canonical_identity_component(self) -> None:
        defaulted = ProjectRegistry(home=self.home)
        self.assertEqual(defaulted.local_host, checkout_claims.local_host())
        with self.assertRaises(ProjectReject) as refusal:
            ProjectRegistry(home=self.home, local_host="Not A Host")
        self.assertEqual(refusal.exception.code, "invalid_checkout_host")


class LegacyDualReadTests(CrossHostProjectTestCase):
    def test_foreign_legacy_registry_reads_lexically_without_foreign_fs(self) -> None:
        original = _write_legacy_registry(self.home, "legacy", FOREIGN_CHECKOUT)
        self.assertEqual(self.registry_schema(), 1)
        resolve, lstat, is_dir = _no_foreign_fs()
        with resolve, lstat, is_dir:
            record = self.registry.resolve("legacy")
        self.assertEqual(record["checkout"], FOREIGN_CHECKOUT)
        self.assertNotIn(
            "checkout_claims",
            record,
            "a legacy read must not invent a host for the checkout",
        )
        self.assertEqual(self.registry.path.read_bytes(), original)

    def test_host_claims_on_populated_legacy_registry_require_explicit_migration(
        self,
    ) -> None:
        self.registry.register("alpha", self.checkouts["alpha"])
        self.registry.register("beta", self.checkouts["beta"])
        self.assertEqual(self.registry_schema(), 1)
        with self.assertRaises(ProjectReject) as new_project:
            self.registry.register("gamma", FOREIGN_CHECKOUT, checkout_host=FOREIGN_HOST)
        self.assertEqual(new_project.exception.code, "legacy_owner_required")
        with self.assertRaises(ProjectReject) as existing_project:
            self.registry.register(
                "alpha", self.local_checkout("wt2"), checkout_host=LOCAL
            )
        self.assertEqual(existing_project.exception.code, "legacy_owner_required")
        # The refusal changed nothing.
        self.assertNotIn("checkout_claims", self.registry.resolve("alpha"))
        with self.assertRaises(ProjectReject):
            self.registry.resolve("gamma")


class SchemaBoundaryTests(CrossHostProjectTestCase):
    """Fail-closed schema boundary: schema 1 never carries claims, schema 2 always does."""

    def _rewrite_schema(self, schema: int) -> bytes:
        payload = json.loads(self.registry.path.read_text(encoding="utf-8"))
        payload["schema"] = schema
        self.registry.path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return self.registry.path.read_bytes()

    def test_schema1_file_with_host_claims_fails_closed(self) -> None:
        self.registry.register("alpha", FOREIGN_CHECKOUT, checkout_host=FOREIGN_HOST)
        self.assertEqual(self.registry_schema(), 2)
        original = self._rewrite_schema(1)
        with self.assertRaises(ProjectReject) as refusal:
            self.registry.resolve("alpha")
        self.assertEqual(refusal.exception.code, "registry_corrupt")
        # Mutating paths read first, so they fail closed too and keep the bytes.
        with self.assertRaises(ProjectReject) as register_refusal:
            self.registry.register(
                "beta", FOREIGN_CHECKOUT, checkout_host=SECOND_FOREIGN_HOST
            )
        self.assertEqual(register_refusal.exception.code, "registry_corrupt")
        with self.assertRaises(ProjectReject) as migrate_refusal:
            self.registry.migrate_legacy(LOCAL)
        self.assertEqual(migrate_refusal.exception.code, "registry_corrupt")
        self.assertEqual(self.registry.path.read_bytes(), original)

    def test_schema2_file_without_host_claims_fails_closed(self) -> None:
        self.registry.register("alpha", self.checkouts["alpha"])
        self.assertEqual(self.registry_schema(), 1)
        original = self._rewrite_schema(2)
        with self.assertRaises(ProjectReject) as refusal:
            self.registry.resolve("alpha")
        self.assertEqual(refusal.exception.code, "registry_corrupt")
        with self.assertRaises(ProjectReject) as migrate_refusal:
            self.registry.migrate_legacy(LOCAL)
        self.assertEqual(migrate_refusal.exception.code, "registry_corrupt")
        self.assertEqual(self.registry.path.read_bytes(), original)

    def test_empty_schema2_file_fails_closed(self) -> None:
        # The writer never emits schema 2 without a claim-bearing record, so an
        # empty schema-2 file is not a legitimate registry state.
        self.registry.root.mkdir(parents=True, exist_ok=True)
        self.registry.path.write_text(
            json.dumps({"schema": 2, "next_event_seq": 0, "projects": {}}),
            encoding="utf-8",
        )
        with self.assertRaises(ProjectReject) as refusal:
            self.registry.resolve("anything")
        self.assertEqual(refusal.exception.code, "registry_corrupt")

    def test_mixed_schema2_registry_stays_readable(self) -> None:
        # Schema 2 requires at least one claim-bearing record, not that every
        # record carries claims: a legacy record may join a host-aware registry.
        self.registry.register("alpha", FOREIGN_CHECKOUT, checkout_host=FOREIGN_HOST)
        self.registry.register("beta", self.checkouts["beta"])
        self.assertEqual(self.registry_schema(), 2)
        self.assertNotIn("checkout_claims", self.registry.resolve("beta"))
        self.assertIn("checkout_claims", self.registry.resolve("alpha"))

    def _inject_null_claims_record(self) -> bytes:
        """Add a record with an explicit ``checkout_claims: null`` to a schema-2 file."""
        self.registry.register("alpha", FOREIGN_CHECKOUT, checkout_host=FOREIGN_HOST)
        payload = json.loads(self.registry.path.read_text(encoding="utf-8"))
        event = {
            "sequence": payload["next_event_seq"],
            "at": "2026-09-28T00:00:00Z",
            "type": "project_registered",
            "project_id": "beta",
        }
        payload["projects"]["beta"] = {
            "project_id": "beta",
            "checkout": FOREIGN_CHECKOUT,
            "checkout_claims": None,
            "aliases": [],
            "streams": {},
            "runtimes": {},
            "deliveries": {},
            "received": {},
            "audits": [],
            "activity": [],
            "created_event": event,
            "updated_event": event,
        }
        self.registry.path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return self.registry.path.read_bytes()

    def test_present_null_checkout_claims_fails_closed_on_every_entry(self) -> None:
        # Regression: an explicit ``checkout_claims: null`` is malformed
        # host-aware data, never a legacy record.  Before the fix the validator
        # treated it as legacy, ``migrate_legacy`` skipped it (the key exists)
        # and reported already_migrated, and ``register`` could raise a raw
        # TypeError iterating None.  All three entry points must refuse and the
        # registry bytes must stay untouched.
        original = self._inject_null_claims_record()
        with self.assertRaises(ProjectReject) as read_refusal:
            self.registry.resolve("beta")
        self.assertEqual(read_refusal.exception.code, "registry_corrupt")
        with self.assertRaises(ProjectReject) as migrate_refusal:
            self.registry.migrate_legacy(LOCAL)
        self.assertEqual(migrate_refusal.exception.code, "registry_corrupt")
        with self.assertRaises(ProjectReject) as register_refusal:
            self.registry.register(
                "gamma", FOREIGN_CHECKOUT, checkout_host=SECOND_FOREIGN_HOST
            )
        self.assertEqual(register_refusal.exception.code, "registry_corrupt")
        self.assertEqual(self.registry.path.read_bytes(), original)
        backups = list(self.registry.root.glob("registry.json.legacy-backup-*"))
        self.assertEqual(backups, [], "a refused migration snapshots nothing")

    def test_null_checkout_claims_in_schema1_fails_closed(self) -> None:
        # Same malformed key on a schema-1 file is rejected at the schema
        # boundary, not silently read as a legacy single-checkout record.
        self.registry.register("alpha", self.checkouts["alpha"])
        payload = json.loads(self.registry.path.read_text(encoding="utf-8"))
        payload["projects"]["alpha"]["checkout_claims"] = None
        self.registry.path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        with self.assertRaises(ProjectReject) as refusal:
            self.registry.resolve("alpha")
        self.assertEqual(refusal.exception.code, "registry_corrupt")
        with self.assertRaises(ProjectReject) as migrate_refusal:
            self.registry.migrate_legacy(LOCAL)
        self.assertEqual(migrate_refusal.exception.code, "registry_corrupt")


class MigrateLegacyTests(CrossHostProjectTestCase):
    def test_explicit_migration_backups_up_byte_exact_and_is_idempotent(self) -> None:
        self.registry.register("alpha", self.checkouts["alpha"], aliases=("a",))
        self.registry.register("beta", self.checkouts["beta"])
        original = self.registry.path.read_bytes()
        result = self.registry.migrate_legacy(LOCAL)
        self.assertEqual(result["code"], "migrated")
        self.assertEqual(result["migrated"], 2)
        self.assertEqual(result["projects"], ["alpha", "beta"])
        backup = Path(result["backup_path"])
        self.assertTrue(backup.is_file())
        self.assertEqual(
            backup.read_bytes(), original, "backup must be byte-exact original"
        )
        self.assertEqual(
            result["backup_sha256"], hashlib.sha256(original).hexdigest()
        )
        self.assertEqual(self.registry_schema(), 2)
        for project_id in ("alpha", "beta"):
            record = self.registry.resolve(project_id)
            self.assertEqual(
                record["checkout"],
                canonicalize_checkout(self.checkouts[project_id]),
                "migration never rewrites checkout strings",
            )
            self.assertEqual(
                record["checkout_claims"],
                [
                    {
                        "host": LOCAL,
                        "path_style": LOCAL_STYLE,
                        "path": record["checkout"],
                    }
                ],
            )
        repeat = self.registry.migrate_legacy(LOCAL)
        self.assertEqual(repeat["code"], "already_migrated")
        self.assertEqual(repeat["migrated"], 0)
        self.assertIsNone(repeat["backup_path"])
        self.assertTrue(
            backup.is_file(), "a repeated migration keeps the original backup"
        )
        # Host claims are accepted after the explicit migration.
        record = self.registry.register(
            "alpha", self.local_checkout("wt2"), checkout_host=LOCAL
        )
        self.assertEqual(len(record["checkout_claims"]), 2)

    def test_migration_failure_preserves_the_original_and_writes_no_backup(self) -> None:
        self.registry.register("alpha", self.checkouts["alpha"])
        original = self.registry.path.read_bytes()
        with self.assertRaises(ProjectReject) as refusal:
            self.registry.migrate_legacy("BAD HOST")
        self.assertEqual(refusal.exception.code, "invalid_checkout_host")
        self.assertEqual(self.registry.path.read_bytes(), original)
        backups = list(self.registry.root.glob("registry.json.legacy-backup-*"))
        self.assertEqual(backups, [], "a failed validation must not snapshot anything")
        self.assertEqual(self.registry_schema(), 1)

    def test_foreign_source_migration_is_lexical_attestation_not_ownership(self) -> None:
        original = _write_legacy_registry(self.home, "legacy", FOREIGN_CHECKOUT)
        resolve, lstat, is_dir = _no_foreign_fs()
        with resolve, lstat, is_dir:
            result = self.registry.migrate_legacy(FOREIGN_HOST)
            record = self.registry.resolve("legacy")
        self.assertEqual(result["code"], "migrated")
        self.assertEqual(result["migrated"], 1)
        self.assertEqual(
            Path(result["backup_path"]).read_bytes(), original
        )
        self.assertEqual(record["checkout"], FOREIGN_CHECKOUT)
        self.assertEqual(
            record["checkout_claims"],
            [
                {
                    "host": FOREIGN_HOST,
                    "path_style": FOREIGN_STYLE,
                    "path": FOREIGN_CHECKOUT,
                }
            ],
        )
        self.assertEqual(self.registry_schema(), 2)
        # A repeated migration for any host is a no-op now.
        self.assertEqual(self.registry.migrate_legacy(LOCAL)["code"], "already_migrated")

    def test_foreign_source_migration_of_native_spelled_checkout_stays_lexical(
        self,
    ) -> None:
        # Regression: a legacy checkout spelled in THIS OS's path style but
        # attested to a foreign source host must migrate without resolve/lstat;
        # locality comes from the explicit source_host, never from the spelling.
        spelled = canonicalize_checkout(self.checkouts["alpha"])
        original = _write_legacy_registry(self.home, "legacy", spelled)
        real_resolve = Path.resolve
        real_lstat = Path.lstat

        def banned(kind, real):
            def wrapped(self, *args, **kwargs):
                if os.path.normcase(os.path.abspath(str(self))) == spelled:
                    raise AssertionError(
                        f"native {kind} on foreign-attested checkout: {self}"
                    )
                return real(self, *args, **kwargs)

            return wrapped

        with patch("pathlib.Path.resolve", banned("resolve", real_resolve)), patch(
            "pathlib.Path.lstat", banned("lstat", real_lstat)
        ):
            result = self.registry.migrate_legacy(FOREIGN_HOST)
            record = self.registry.resolve("legacy")
        self.assertEqual(result["code"], "migrated")
        self.assertEqual(Path(result["backup_path"]).read_bytes(), original)
        self.assertEqual(record["checkout"], spelled)
        self.assertEqual(
            record["checkout_claims"],
            [{"host": FOREIGN_HOST, "path_style": LOCAL_STYLE, "path": spelled}],
        )


class CrossHostStreamRoutingTests(CrossHostProjectTestCase):
    def _register_alpha(self) -> None:
        self.registry.register("alpha", self.local_checkout("alpha"), checkout_host=LOCAL)
        self.registry.register("alpha", FOREIGN_CHECKOUT, checkout_host=FOREIGN_HOST)

    def test_windows_and_linux_actors_exchange_delivery_and_ack_in_one_stream(self) -> None:
        self._register_alpha()
        windows = self.transport("alpha", "codex", "maker-1", host="windows-w2")
        linux = self.transport("alpha", "kimi", "worker-1", host="linux-a1")
        stream = self.stream(windows)
        self.assertEqual(stream.checkout, windows.checkout, "checkout stays provenance")

        sent = windows.send(linux, "cross-host hello", stream=stream)
        self.assertEqual(sent["status"], "ok", sent)
        received = linux.read(stream=stream)
        self.assertEqual(received["status"], "ok", received)
        self.assertEqual(self.bodies(received), ["cross-host hello"])
        self.assertEqual(
            received["messages"][0]["untrusted_message"]["trust"], "untrusted"
        )
        ack = linux.acknowledge(sent["delivery_id"])
        self.assertEqual(ack["code"], "acknowledged", ack)
        observed = windows.read(stream=stream)
        self.assertEqual(
            observed["messages"][0]["untrusted_message"]["application"]["ack_result"],
            "acknowledged",
        )
        state = windows.delivery_state(sent["delivery_id"])
        self.assertEqual(state["application_state"], "ACKNOWLEDGED")
        self.assertEqual(
            self.registry.resolve("alpha")["checkout_claims"][1]["host"], FOREIGN_HOST
        )

    def test_cross_project_routing_refusals_survive_host_claims(self) -> None:
        self._register_alpha()
        self.registry.register("beta", self.local_checkout("beta"), checkout_host=LOCAL)
        alpha_sender = self.transport("alpha", "codex", "codex-testhost-sender", host="windows-w2")
        beta_peer = self.transport("beta", "kimi", "peer", host="linux-a1")
        alpha_stream = self.stream(alpha_sender)
        beta_stream = self.stream(beta_peer)

        sent = alpha_sender.send(beta_peer, "wrong project", stream=alpha_stream)
        self.assertEqual(sent["status"], "error", sent)
        self.assertEqual(sent["code"], "cross_project_recipient")

        foreign_read = alpha_sender.read(stream=beta_stream)
        self.assertEqual(foreign_read["status"], "error", foreign_read)
        self.assertEqual(foreign_read["code"], "stream_project_mismatch")

        forged = ProjectStream(
            project_id="alpha",
            checkout=alpha_sender.checkout,
            task_id="task-one",
            thread_id="thread-one",
            run_id="run-one",
            stream_id="stream-" + "0" * 24,
        )
        invalid = alpha_sender.read(stream=forged)
        self.assertEqual(invalid["status"], "error", invalid)
        self.assertEqual(
            invalid["code"],
            "invalid_stream",
            "a non-deterministic stream id stays refused",
        )
        self.assertEqual(beta_peer.read(stream=beta_stream)["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
