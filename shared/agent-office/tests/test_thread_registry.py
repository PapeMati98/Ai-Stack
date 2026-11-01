"""I4 crosswalk acceptance: exact identities, durable links and zero unsafe sends."""

from __future__ import annotations

import concurrent.futures
import json
import shutil
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coordfs.thread_registry import ThreadRegistry  # noqa: E402


LOCAL = "local_11111111-1111-4111-8111-111111111111"
CLI1 = "22222222-2222-4222-8222-222222222222"
CLI2 = "33333333-3333-4333-8333-333333333333"
CLI3 = "44444444-4444-4444-8444-444444444444"
AGENT1 = "claude-ailab-" + CLI1
AGENT2 = "claude-ailab-" + CLI2
AGENT3 = "claude-ailab-" + CLI3


def record(*, agent: str = AGENT1, local: str = LOCAL, cli: str = CLI1,
           title: str = "Example", observed: str = "2026-10-05T01:00:00Z",
           project: str | None = None, endpoint: str | None = None) -> dict:
    return {
        "office_agent": agent, "tool": "claude", "surface": "desktop",
        "native_ids": {"local_id": local, "cli_session_id": cli},
        "title": title, "project_slug": project, "status": "active",
        "observed_at": observed, "live_endpoint": endpoint or agent,
        "identity_links": [{"a": local, "b": cli, "evidence": "fixture:local-cli-map",
                            "observed_at": observed, "confidence": "strong"}],
    }


class ThreadRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(__file__).parent / ("office-thread-test-" + uuid.uuid4().hex)
        self.temp.mkdir()
        self.home = self.temp / "coord"
        self.registry = ThreadRegistry(self.home)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp)

    def test_register_restart_and_idempotency(self) -> None:
        first = self.registry.register(record())
        self.assertEqual(first["status"], "ok", first)
        self.assertFalse(first["idempotent"])
        second = ThreadRegistry(self.home).register(record())
        self.assertEqual(second["status"], "ok", second)
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["record"], second["record"])
        self.assertEqual(ThreadRegistry(self.home).resolve(AGENT1)["record"]["thread_key"],
                         first["record"]["thread_key"])
        self.assertEqual(ThreadRegistry(self.home).validate_recipient(LOCAL)["office_agent"], AGENT1)

    def test_rotation_preserves_canonical_and_retired_endpoint(self) -> None:
        first = self.registry.register(record())
        self.assertEqual(first["status"], "ok", first)
        rotated = self.registry.register(record(cli=CLI2, endpoint=AGENT2,
                                                observed="2026-10-05T02:00:00Z"))
        self.assertEqual(rotated["status"], "ok", rotated)
        self.assertEqual(rotated["record"]["office_agent"], AGENT1)
        self.assertEqual(rotated["record"]["live_endpoint"], AGENT2)
        self.assertEqual(rotated["record"]["endpoint_history"][0]["office_agent"], AGENT1)
        self.assertEqual(ThreadRegistry(self.home).validate_recipient(LOCAL)["office_agent"], AGENT2)
        self.assertEqual(ThreadRegistry(self.home).validate_recipient(AGENT1)["code"], "endpoint_expired")
        self.assertEqual(ThreadRegistry(self.home).resolve(AGENT1)["record"]["thread_key"],
                         first["record"]["thread_key"])
        self.assertEqual(ThreadRegistry(self.home).register(record(cli=CLI1, endpoint=AGENT1,
                            observed="2026-10-05T03:00:00Z"))["code"], "identity_conflict")

    def test_metadata_upsert_keeps_rotated_endpoint_when_omitted(self) -> None:
        self.assertEqual(self.registry.register(record())["status"], "ok")
        self.assertEqual(self.registry.register(record(cli=CLI2, endpoint=AGENT2,
                                                      observed="2026-10-05T02:00:00Z"))["status"], "ok")
        update = record(cli=CLI2, observed="2026-10-05T03:00:00Z")
        update.pop("live_endpoint")
        update.update(title="Updated", project_slug="one", topic="Current",
                      status="inactive", last_seen="2026-10-05T03:00:00Z")
        result = ThreadRegistry(self.home).register(update)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["record"]["live_endpoint"], AGENT2)
        self.assertEqual([x["office_agent"] for x in result["record"]["endpoint_history"]], [AGENT1])
        self.assertEqual(ThreadRegistry(self.home).validate_recipient(AGENT1)["code"], "endpoint_expired")
        self.assertEqual(ThreadRegistry(self.home).validate_recipient(AGENT2)["status"], "ok")
        partial = {"office_agent": AGENT1, "tool": "claude", "surface": "desktop",
                   "title": "Partial update", "topic": "Still current"}
        persisted = ThreadRegistry(self.home).register(partial)
        self.assertEqual(persisted["status"], "ok", persisted)
        self.assertEqual(persisted["record"]["live_endpoint"], AGENT2)
        self.assertEqual(ThreadRegistry(self.home).validate_recipient(AGENT1)["code"], "endpoint_expired")

    def test_same_cli_endpoint_change_retires_every_previous_address(self) -> None:
        self.registry.register(record())
        self.registry.register(record(cli=CLI2, endpoint=AGENT2, observed="2026-10-05T02:00:00Z"))
        replacement = "claude-ailab-alternate-" + CLI2
        changed = self.registry.register(record(cli=CLI2, endpoint=replacement,
                                                observed="2026-10-05T03:00:00Z"))
        self.assertEqual(changed["status"], "ok", changed)
        self.assertEqual([x["office_agent"] for x in changed["record"]["endpoint_history"]],
                         [AGENT1, AGENT2])
        known = self.home / "office" / "known-agents"
        known.mkdir(parents=True)
        (known / AGENT2).touch()
        self.assertEqual(ThreadRegistry(self.home).validate_recipient(AGENT2)["code"], "endpoint_expired")
        self.assertEqual(ThreadRegistry(self.home).validate_recipient(replacement)["status"], "ok")
        self.assertEqual(self.registry.register(record(cli=CLI2, endpoint=AGENT2,
                            observed="2026-10-05T04:00:00Z"))["code"], "identity_conflict")

    def test_stale_rotation_cannot_steal_current_mapping(self) -> None:
        self.registry.register(record())
        self.registry.register(record(cli=CLI2, endpoint=AGENT2, observed="2026-10-05T03:00:00Z"))
        stale = self.registry.register(record(cli=CLI3, endpoint=AGENT3,
                                              observed="2026-10-05T02:00:00Z"))
        self.assertEqual(stale["code"], "identity_conflict")
        self.assertEqual(self.registry.validate_recipient(LOCAL)["office_agent"], AGENT2)
        self.assertEqual(self.registry.validate_recipient(AGENT3)["code"], "not_found")

    def test_whole_second_observation_cannot_override_later_fractional_one(self) -> None:
        self.assertEqual(self.registry.register(record(
            observed="2026-10-05T01:00:00.500000Z"))["status"], "ok")
        stale = self.registry.register(record(cli=CLI2, endpoint=AGENT2,
                                              observed="2026-10-05T01:00:00Z"))
        self.assertEqual(stale["code"], "identity_conflict")
        self.assertEqual(ThreadRegistry(self.home).validate_recipient(AGENT1)["status"], "ok")
        self.assertEqual(ThreadRegistry(self.home).validate_recipient(AGENT2)["code"], "not_found")

    def test_alias_conflict_never_rebinds(self) -> None:
        self.registry.register(record())
        conflicting = record(agent=AGENT2, cli=CLI2)
        self.assertEqual(self.registry.register(conflicting)["code"], "identity_conflict")
        self.assertEqual(self.registry.validate_recipient(LOCAL)["office_agent"], AGENT1)
        self.assertEqual(self.registry.list()["count"], 1)

    def test_title_only_and_incomplete_tuple_are_not_addresses(self) -> None:
        self.registry.register(record())
        self.assertEqual(self.registry.resolve("Example")["code"], "not_an_address")
        self.assertEqual(self.registry.resolve(project_slug="one", tool="claude",
                                               title="Example")["code"], "not_an_address")
        self.assertEqual(self.registry.validate_recipient("Example")["code"], "not_an_address")

    def test_duplicate_titles_are_ambiguous_only_with_full_tuple(self) -> None:
        self.registry.register(record(project="one"))
        other = record(agent=AGENT2, local="local_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                       cli=CLI2, project="one")
        self.assertEqual(self.registry.register(other)["status"], "ok")
        found = self.registry.resolve(project_slug="one", tool="claude", surface="desktop",
                                      title="Example")
        self.assertEqual(found["code"], "ambiguous")
        self.assertEqual(len(found["candidates"]), 2)
        self.assertEqual(self.registry.resolve(project_slug="two", tool="claude",
                                               surface="desktop", title="Example")["code"], "not_found")

    def test_doubled_truncated_and_weak_even_with_legacy_marker(self) -> None:
        self.registry.register(record())
        known = self.home / "office" / "known-agents"
        known.mkdir(parents=True)
        bad = ["claude-ailab-ailab-" + CLI1, "zcode-ailab-zcode-ailab-process-99-abc",
               "claude-ailab-process-99-abc", AGENT1[:-8]]
        for address in bad:
            (known / address).touch()
        self.assertEqual(self.registry.validate_recipient(bad[0])["code"], "double_prefix")
        self.assertEqual(self.registry.validate_recipient(bad[1])["code"], "double_prefix")
        self.assertEqual(self.registry.validate_recipient(bad[2])["code"], "weak_identity")
        self.assertEqual(self.registry.validate_recipient(bad[3])["code"], "truncated")
        self.assertEqual(self.registry.validate_recipient(bad[3])["suggestions"], [AGENT1])

    def test_repeated_harness_rejected_consistently_before_registration(self) -> None:
        bad = ("zcode-ailab-zcode-northwind-kpi", "claude-code-ailab-claude-code-" + CLI1)
        known = self.home / "office" / "known-agents"
        known.mkdir(parents=True)
        for address in bad:
            (known / address).touch()
            self.assertEqual(self.registry.register(record(agent=address))["code"], "double_prefix")
            self.assertEqual(self.registry.resolve(address)["code"], "double_prefix")
            self.assertEqual(self.registry.validate_recipient(address)["code"], "double_prefix")
        self.assertFalse(self.registry.path.exists())

    def test_full_canonical_prefix_only_suggests_and_never_delivers(self) -> None:
        self.registry.register(record())
        prefix = AGENT1[:-7]
        known = self.home / "office" / "known-agents"
        known.mkdir(parents=True)
        (known / prefix).touch()
        for operation in (self.registry.resolve, self.registry.validate_recipient):
            result = operation(prefix)
            self.assertEqual(result["code"], "truncated", result)
            self.assertEqual(result["suggestions"], [AGENT1])

    def test_expiry_does_not_redirect(self) -> None:
        row = record()
        row["endpoint_expires_at"] = "2000-01-01T00:00:00Z"
        self.registry.register(row)
        self.assertEqual(self.registry.validate_recipient(AGENT1)["code"], "endpoint_expired")
        self.assertEqual(self.registry.validate_recipient(LOCAL)["code"], "endpoint_expired")

    def test_whole_second_expiry_uses_numeric_utc_comparison(self) -> None:
        row = record()
        row["endpoint_expires_at"] = datetime.now(timezone.utc).replace(
            microsecond=0).isoformat().replace("+00:00", "Z")
        self.assertEqual(self.registry.register(row)["status"], "ok")
        self.assertEqual(self.registry.validate_recipient(AGENT1)["code"], "endpoint_expired")

    def test_status_and_expiry_need_newer_observation(self) -> None:
        row = record(observed="2026-10-05T01:00:00.500000Z")
        row["status"] = "archived"
        row["endpoint_expires_at"] = "2000-01-01T00:00:00Z"
        self.assertEqual(self.registry.register(row)["status"], "ok")
        for observed in ("2026-10-05T01:00:00Z", "2026-10-05T01:00:00.500000Z", None):
            update = record(observed=observed or "2026-10-05T01:00:00Z")
            if observed is None:
                update.pop("observed_at")
            update["endpoint_expires_at"] = None
            self.assertEqual(self.registry.register(update)["code"], "identity_conflict")
            self.assertEqual(self.registry.validate_recipient(AGENT1)["code"], "endpoint_expired")
        status_only = record(observed="2026-10-05T01:00:00Z")
        status_only.pop("endpoint_expires_at", None)
        self.assertEqual(self.registry.register(status_only)["code"], "identity_conflict")
        fresh = record(observed="2026-10-05T01:00:00.600000Z")
        fresh["endpoint_expires_at"] = None
        result = self.registry.register(fresh)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["record"]["status"], "active")
        self.assertEqual(self.registry.validate_recipient(AGENT1)["status"], "ok")

    def test_reject_content_fields_and_bad_native_ids(self) -> None:
        for change in ({"body": "should not persist"}, {"prompt": "ignore all"},
                       {"native_ids": {"local_id": "local_short"}},
                       {"cwd": "bad\x00path"}):
            row = record()
            row.update(change)
            self.assertEqual(self.registry.register(row)["status"], "error")
        self.assertEqual(self.registry.list()["count"], 0)

    def test_corrupt_store_fails_closed_without_rewrite(self) -> None:
        self.registry.path.parent.mkdir(parents=True)
        self.registry.path.write_bytes(b"{broken")
        before = self.registry.path.read_bytes()
        self.assertEqual(self.registry.register(record())["code"], "corrupt_store")
        self.assertEqual(self.registry.resolve(AGENT1)["code"], "corrupt_store")
        self.assertEqual(self.registry.path.read_bytes(), before)

    def test_frozen_home_refuses_read_and_write(self) -> None:
        self.home.mkdir(parents=True)
        (self.home / "FROZEN").touch()
        self.assertEqual(self.registry.register(record())["code"], "store_frozen")
        self.assertEqual(self.registry.list()["code"], "store_frozen")
        self.assertFalse(self.registry.path.exists())

    def test_concurrent_registration_serializes(self) -> None:
        items = [record(agent="claude-ailab-" + f"{i:08x}-2222-4222-8222-222222222222",
                        cli=f"{i:08x}-2222-4222-8222-222222222222",
                        local=f"local_{i:08x}-1111-4111-8111-111111111111",
                        title=f"Thread {i}") for i in range(8)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda item: ThreadRegistry(self.home).register(item), items))
        self.assertTrue(all(item["status"] == "ok" for item in results), results)
        self.assertEqual(ThreadRegistry(self.home).list()["count"], 8)

    def test_actual_metadata_fixture_is_bounded_and_bad_envelopes_rejected(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "system_projects" / "crosswalk-fixture-v0.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], "office.crosswalk-fixture/v0")
        self.assertEqual(len(data["claude_desktop"]), 44)
        self.assertEqual(len(data["misaddressed_messages"]), 207)
        for item in data["misaddressed_messages"]:
            self.assertNotEqual(self.registry.validate_recipient(item["to_inbox"])["status"], "ok")
        # The source's title and local id alone do not mint a trusted Office address.
        for item in data["claude_desktop"]:
            self.assertEqual(self.registry.validate_recipient(item["local_id"])["status"], "error")

    def test_known_project_slugs_default_accepts_any_slug(self) -> None:
        # Default (None) keeps existing behaviour: any non-null slug is accepted,
        # null slug remains null.
        accepted = self.registry.register(record(project="legacy-orphan"))
        self.assertEqual(accepted["status"], "ok", accepted)
        nulled = self.registry.register(record(project=None))
        self.assertEqual(nulled["status"], "ok", nulled)

    def test_known_project_slugs_rejects_unknown_with_zero_writes(self) -> None:
        registry = ThreadRegistry(
            self.home, known_project_slugs=frozenset({"example-workspace", "example-project"})
        )
        rejected = registry.register(record(project="ghost-org"))
        self.assertEqual(rejected["status"], "error", rejected)
        self.assertEqual(rejected["code"], "unknown_project", rejected)
        self.assertEqual(rejected["project_slug"], "ghost-org", rejected)
        # Zero writes on rejected IDs: the registry file is not produced.
        self.assertFalse(registry.path.exists())

    def test_known_project_slugs_accepts_known_and_null(self) -> None:
        registry = ThreadRegistry(
            self.home, known_project_slugs=frozenset({"example-workspace", "example-project"})
        )
        ok = registry.register(record(project="example-workspace"))
        self.assertEqual(ok["status"], "ok", ok)
        # Null slug is accepted as null. A null project is a distinct thread,
        # not a metadata update of the previous record, so this call uses a
        # separate canonical identity (office_agent / native_ids).
        nul = registry.register(record(agent=AGENT2,
                                       local="local_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                                       cli=CLI2, project=None))
        self.assertEqual(nul["status"], "ok", nul)
        self.assertEqual(nul["record"]["project_slug"], None)
        self.assertEqual(registry.list()["count"], 2)

    def test_known_project_slugs_is_immutable_per_instance(self) -> None:
        # The Office caller injects the exact set; the registry stores it as
        # frozenset so callers cannot mutate it through the registry.
        slugs = frozenset({"example-workspace", "example-project"})
        registry = ThreadRegistry(self.home, known_project_slugs=slugs)
        self.assertIsInstance(registry.known_project_slugs, frozenset)
        self.assertEqual(registry.known_project_slugs, slugs)

    def test_claude_code_alias_resolves_to_canonical_via_cli_session_id(self) -> None:
        self.registry.register(record())
        alias = "claude-code-ailab-" + CLI1
        result = self.registry.validate_recipient(alias)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["office_agent"], AGENT1)
        self.assertEqual(result["alias_evidence"], "uuid_match")
        self.assertEqual(self.registry.resolve(alias)["record"]["office_agent"], AGENT1)
        # Canonical office_agent is not repinned by an alias hit.
        self.assertEqual(self.registry.list()["records"][0]["office_agent"], AGENT1)

    def test_claude_code_alias_resolves_to_canonical_via_local_id(self) -> None:
        self.registry.register(record())
        local_uuid = LOCAL[len("local_"):]
        alias = "claude-code-ailab-" + local_uuid
        result = self.registry.validate_recipient(alias)
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["office_agent"], AGENT1)
        self.assertEqual(result["alias_evidence"], "uuid_match")
        self.assertEqual(ThreadRegistry(self.home).resolve(alias)["record"]["office_agent"], AGENT1)

    def test_claude_alias_never_selects_other_tool_with_same_uuid(self) -> None:
        foreign = record(agent="zcode-ailab-" + CLI1)
        foreign["tool"] = "zcode"
        self.assertEqual(self.registry.register(foreign)["status"], "ok")
        alias = "claude-code-ailab-" + CLI1
        known = self.home / "office" / "known-agents"
        known.mkdir(parents=True)
        (known / alias).touch()
        self.assertEqual(self.registry.validate_recipient(alias)["code"], "not_found")
        self.assertEqual(self.registry.resolve(alias)["code"], "not_found")
        self.assertEqual(self.registry.register(record())["status"], "ok")
        self.assertEqual(self.registry.validate_recipient(alias)["office_agent"], AGENT1)
        self.assertEqual(self.registry.resolve(alias)["record"]["tool"], "claude")

    def test_claude_alias_requires_current_local_cli_link_and_live_binding(self) -> None:
        row = record()
        row["identity_links"] = []
        self.assertEqual(self.registry.register(row)["status"], "ok")
        for alias in ("claude-code-ailab-" + CLI1,
                      "claude-code-ailab-" + LOCAL[len("local_"):]):
            self.assertEqual(self.registry.resolve(alias)["code"], "weak_identity")
            self.assertEqual(self.registry.validate_recipient(alias)["code"], "weak_identity")

    def test_claude_alias_rejects_mismatched_live_cli_binding(self) -> None:
        row = record(cli=CLI2)
        self.assertEqual(self.registry.register(row)["status"], "ok")
        for alias in ("claude-code-ailab-" + CLI2,
                      "claude-code-ailab-" + LOCAL[len("local_"):]):
            self.assertEqual(self.registry.resolve(alias)["code"], "weak_identity")
            self.assertEqual(self.registry.validate_recipient(alias)["code"], "weak_identity")
        self.assertEqual(self.registry.register({**record(agent="claude-code-ailab-" + CLI3),
                                                 "tool": "zcode"})["code"], "identity_conflict")

    def test_reverse_claude_harness_alias_is_evidenced_and_durable(self) -> None:
        canonical = "claude-code-ailab-" + CLI1
        self.assertEqual(self.registry.register(record(agent=canonical))["status"], "ok")
        restarted = ThreadRegistry(self.home)
        for alias in (AGENT1, "claude-ailab-" + LOCAL.removeprefix("local_")):
            self.assertEqual(restarted.validate_recipient(alias)["office_agent"], canonical)
            self.assertEqual(restarted.resolve(alias)["record"]["office_agent"], canonical)
        self.assertEqual(restarted.list()["records"][0]["office_agent"], canonical)

    def test_reverse_claude_alias_missing_link_and_expiry_refuse(self) -> None:
        canonical = "claude-code-ailab-" + CLI1
        row = record(agent=canonical)
        row["identity_links"] = []
        self.assertEqual(self.registry.register(row)["status"], "ok")
        for result in (self.registry.resolve(AGENT1),
                       self.registry.validate_recipient(AGENT1)):
            self.assertEqual(result["code"], "weak_identity")
        other_home = self.temp / "expired"
        expired_registry = ThreadRegistry(other_home)
        row = record(agent=canonical)
        row["endpoint_expires_at"] = "2000-01-01T00:00:00Z"
        self.assertEqual(expired_registry.register(row)["status"], "ok")
        self.assertEqual(ThreadRegistry(other_home).validate_recipient(AGENT1)["code"],
                         "endpoint_expired")

    def test_claude_code_alias_unknown_uuid_fails_closed_without_guessing(self) -> None:
        self.registry.register(record())
        unknown_uuid = "99999999-9999-4999-8999-999999999999"
        result = self.registry.validate_recipient("claude-code-ailab-" + unknown_uuid)
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["code"], "not_found", result)

    def test_claude_code_alias_does_not_promote_expired_endpoint(self) -> None:
        row = record()
        row["endpoint_expires_at"] = "2000-01-01T00:00:00Z"
        self.registry.register(row)
        alias = "claude-code-ailab-" + CLI1
        result = self.registry.validate_recipient(alias)
        self.assertEqual(result["status"], "error", result)
        self.assertEqual(result["code"], "endpoint_expired", result)


if __name__ == "__main__":
    unittest.main()
