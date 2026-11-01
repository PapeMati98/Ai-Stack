"""Frozen I1 projection contract and legacy-store isolation."""

import copy
import hashlib
import json
import shutil
import unittest
import uuid
from pathlib import Path

from coordfs.errors import CoordError
from coordfs.project_registry_view import (
    IMPORT_PRINCIPALS,
    PROJECT_FIELDS,
    ProjectRegistryView,
    prepare_remote_registry_import,
)


FIXTURE = Path(__file__).parent / "fixtures" / "system_projects" / "I1-registry-v2.frozen-v0.json"
CANDIDATE_FIXTURE = Path(__file__).parent / "fixtures" / "system_projects" / "I1-registry-v2.schema-1.2.json"
PRINCIPAL = sorted(IMPORT_PRINCIPALS)[0]


def canonical_source_sha(registry):
    encoded = json.dumps(registry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def synthetic_v6_registry():
    return {
        "schema": "agent-stack/registry-v2",
        "version": "v6-synthetic-1.2",
        "generated_at": "2026-10-05T12:30:00+00:00",
        "generated_by": "synthetic-test",
        "organizations": [{
            "id": "synthetic-org",
            "kind": "synthetic-kind",
            "status": "confirmed",
            "aliases": [],
            "email_domains": ["EMAIL_DOMAIN_SENTINEL.invalid"],
        }],
        "workspace_groups": [],
        "projects": {
            "synthetic-project": {
                "title": "Synthetic project",
                "family": "synthetic-family",
                "aliases": [],
                "org_links": [{"org": "synthetic-org", "role": "owner", "status": "confirmed"}],
                "org_resolution": "synthetic-test",
                "folders": [],
                "hub_main_page": None,
                "hub_latest_page": None,
                "status": "active",
            }
        },
        "topics": [],
        "people": [{
            "id": "synthetic-person",
            "name": "PERSON_NAME_SENTINEL",
            "orgs": ["synthetic-org"],
            "projects": ["synthetic-project"],
            "role": "synthetic role",
            "page": "wiki/people/synthetic-only.md",
        }],
        "sources": {"names": {"private_source_sentinel": "SOURCE_VALUE_SENTINEL"}},
        "phase": "final",
        "families": [{"id": "synthetic-family", "title": "Synthetic family"}],
    }


class ProjectRegistryViewTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(__file__).parent / f"_registry_test_{uuid.uuid4().hex}"
        self.home.mkdir()
        self.addCleanup(shutil.rmtree, self.home)
        self.view = ProjectRegistryView(self.home)
        self.seed = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_real_frozen_seed_idempotence_and_drift(self):
        result = self.view.import_registry(self.seed, imported_by=PRINCIPAL)
        self.assertEqual(result["code"], "imported")
        self.assertFalse(result["drift"])
        self.assertEqual(result["metadata"]["registry_version"], "frozen-v0")
        self.assertRegex(result["metadata"]["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(self.view.get("agent-stack")["project"], self.seed["projects"]["agent-stack"])
        self.assertEqual(self.view.list()["count"], len(self.seed["projects"]))
        returned = self.view.get("agent-stack")["project"]
        returned["title"] = "caller mutation"
        self.assertEqual(self.view.get("agent-stack")["project"]["title"], self.seed["projects"]["agent-stack"]["title"])
        before = self.view.path.read_bytes()
        self.assertEqual(self.view.import_registry(copy.deepcopy(self.seed), imported_by=PRINCIPAL)["code"], "unchanged")
        self.assertEqual(self.view.path.read_bytes(), before)
        changed = copy.deepcopy(self.seed)
        changed["version"] = "frozen-v1"
        changed["projects"]["agent-stack"]["title"] = "Agent stack v1"
        result = self.view.import_registry(changed, imported_by=PRINCIPAL)
        self.assertTrue(result["drift"])
        self.assertEqual(result["previous"]["source_sha256"], hashlib.sha256(json.dumps(self.seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        self.assertEqual(ProjectRegistryView(self.home).get("agent-stack")["project"]["title"], "Agent stack v1")

    def test_unknown_read_never_creates_project(self):
        self.view.import_registry(self.seed, imported_by=PRINCIPAL)
        before = self.view.path.read_bytes()
        result = self.view.get("unknown-proj")
        self.assertEqual((result["status"], result["code"]), ("warning", "unknown_project"))
        self.assertIsNone(result["project"])
        self.assertEqual(before, self.view.path.read_bytes())

    def test_same_version_changed_metadata_is_reported_as_drift(self):
        self.view.import_registry(self.seed, imported_by=PRINCIPAL)
        changed = copy.deepcopy(self.seed)
        changed["projects"]["agent-stack"]["title"] = "Changed title"
        result = self.view.import_registry(changed, imported_by=PRINCIPAL)
        self.assertTrue(result["drift"])
        self.assertEqual(result["metadata"]["registry_version"], result["previous"]["registry_version"])
        self.assertNotEqual(result["metadata"]["source_sha256"], result["previous"]["source_sha256"])

    def test_candidate_schema_1_2_projects_and_discards_nonrouting_metadata(self):
        candidate = json.loads(CANDIDATE_FIXTURE.read_text(encoding="utf-8"))
        paw = candidate["projects"]["paw"]
        self.assertIn("phase", candidate)
        self.assertIn("frozen_source", candidate)
        self.assertTrue(candidate["families"])
        self.assertIn("related", paw)
        self.assertIn("current_state_source", paw)
        self.assertIn("names", candidate["sources"])

        expected_sha = canonical_source_sha(candidate)
        result = self.view.import_registry(candidate, imported_by=PRINCIPAL)
        self.assertEqual(result["code"], "imported")
        self.assertEqual(result["metadata"]["source_sha256"], expected_sha)

        stored = json.loads(self.view.path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(stored),
            {"schema", "registry_version", "generated_at", "source_sha256", "imported_by", "projects"},
        )
        expected_project = {key: value for key, value in paw.items() if key in PROJECT_FIELDS}
        self.assertEqual(stored["projects"]["paw"], expected_project)
        self.assertNotIn("related", stored["projects"]["paw"])
        self.assertNotIn("current_state_source", stored["projects"]["paw"])
        self.assertEqual(self.view.import_registry(copy.deepcopy(candidate), imported_by=PRINCIPAL)["code"], "unchanged")

        changed_source_metadata = copy.deepcopy(candidate)
        changed_source_metadata["sources"]["names"]["short_names"] += 1
        changed = self.view.import_registry(changed_source_metadata, imported_by=PRINCIPAL)
        self.assertTrue(changed["drift"])
        self.assertEqual(changed["metadata"]["source_sha256"], canonical_source_sha(changed_source_metadata))

    def test_candidate_related_and_current_state_source_are_validated(self):
        candidate = json.loads(CANDIDATE_FIXTURE.read_text(encoding="utf-8"))
        self.view.import_registry(candidate, imported_by=PRINCIPAL)
        before = self.view.path.read_bytes()

        for field, invalid in (("related", ["Bad slug"]), ("current_state_source", "not-a-source-id")):
            bad = copy.deepcopy(candidate)
            bad["projects"]["paw"][field] = invalid
            with self.subTest(field=field), self.assertRaises(CoordError):
                self.view.import_registry(bad, imported_by=PRINCIPAL)
        self.assertEqual(before, self.view.path.read_bytes())

    def test_v6_people_and_email_domains_are_accepted_but_never_projected(self):
        source = synthetic_v6_registry()
        expected_sha = canonical_source_sha(source)
        result = self.view.import_registry(source, imported_by=PRINCIPAL)
        self.assertEqual(result["code"], "imported")
        self.assertEqual(result["metadata"]["source_sha256"], expected_sha)

        state = json.loads(self.view.path.read_text(encoding="utf-8"))
        self.assertEqual(state["source_sha256"], expected_sha)
        expected_project = {
            key: value for key, value in source["projects"]["synthetic-project"].items()
            if key in PROJECT_FIELDS
        }
        self.assertEqual(state["projects"]["synthetic-project"], expected_project)
        safe_output = json.dumps({"state": state, "get": self.view.get("synthetic-project"), "list": self.view.list()})
        for sentinel in ("PERSON_NAME_SENTINEL", "EMAIL_DOMAIN_SENTINEL.invalid", "SOURCE_VALUE_SENTINEL"):
            self.assertNotIn(sentinel, safe_output)

        remote_payload = prepare_remote_registry_import(source)
        remote_text = json.dumps(remote_payload)
        for sentinel in ("PERSON_NAME_SENTINEL", "EMAIL_DOMAIN_SENTINEL.invalid", "SOURCE_VALUE_SENTINEL"):
            self.assertNotIn(sentinel, remote_text)
        remote_view = ProjectRegistryView(self.home / "remote")
        imported = remote_view.import_registry(remote_payload, imported_by=PRINCIPAL)
        self.assertEqual(imported["metadata"]["source_sha256"], expected_sha)
        self.assertEqual(remote_view.import_registry(remote_payload, imported_by=PRINCIPAL)["code"], "unchanged")
        remote_state = remote_view.path.read_text(encoding="utf-8")
        for sentinel in ("PERSON_NAME_SENTINEL", "EMAIL_DOMAIN_SENTINEL.invalid", "SOURCE_VALUE_SENTINEL"):
            self.assertNotIn(sentinel, remote_state)
        remote_output = json.dumps({"import": imported, "get": remote_view.get("synthetic-project"), "list": remote_view.list()})
        for sentinel in ("PERSON_NAME_SENTINEL", "EMAIL_DOMAIN_SENTINEL.invalid", "SOURCE_VALUE_SENTINEL"):
            self.assertNotIn(sentinel, remote_output)

        changed_source = copy.deepcopy(source)
        changed_source["people"][0]["name"] = "PERSON_NAME_CHANGED_SENTINEL"
        changed_source["organizations"][0]["email_domains"] = ["CHANGED_EMAIL_DOMAIN_SENTINEL.invalid"]
        changed = self.view.import_registry(changed_source, imported_by=PRINCIPAL)
        self.assertTrue(changed["drift"])
        self.assertEqual(changed["previous"]["source_sha256"], expected_sha)
        self.assertEqual(changed["metadata"]["source_sha256"], canonical_source_sha(changed_source))
        self.assertEqual(self.view.get("synthetic-project")["project"], state["projects"]["synthetic-project"])

    def test_malformed_and_unauthorized_imports_do_not_change_snapshot(self):
        self.view.import_registry(self.seed, imported_by=PRINCIPAL)
        before = self.view.path.read_bytes()
        for malicious in ("body", "prompt", "contacts", "style"):
            bad = copy.deepcopy(self.seed)
            bad["projects"]["agent-stack"][malicious] = "unexpected text"
            with self.subTest(malicious=malicious), self.assertRaises(CoordError):
                self.view.import_registry(bad, imported_by=PRINCIPAL)
        for malicious in ("contact", "transcript", "messages"):
            bad = copy.deepcopy(self.seed)
            canary = f"FORBIDDEN_VALUE_{malicious.upper()}"
            bad["sources"] = {"arbitrary-extension": {"nested": [{malicious: canary}]}}
            with self.subTest(nested_privacy_key=malicious), self.assertRaises(CoordError) as rejected:
                self.view.import_registry(bad, imported_by=PRINCIPAL)
            self.assertNotIn(canary, str(rejected.exception))
        bad = copy.deepcopy(self.seed)
        bad["generated_at"] = "not a timestamp"
        with self.assertRaises(CoordError):
            self.view.import_registry(bad, imported_by=PRINCIPAL)
        bad = copy.deepcopy(self.seed)
        bad["projects"]["agent-stack"]["email_domains"] = ["example.invalid"]
        with self.assertRaises(CoordError):
            self.view.import_registry(bad, imported_by=PRINCIPAL)
        with self.assertRaises(CoordError) as denied:
            self.view.import_registry(self.seed, imported_by="claude-ailab-88900f5b")
        self.assertEqual(denied.exception.code, "forbidden")
        self.assertEqual(before, self.view.path.read_bytes())

    def test_legacy_project_and_work_bytes_are_untouched(self):
        legacy_project = self.home / "office" / "projects" / "registry.json"
        legacy_work1 = self.home / "work" / "abc.json"
        legacy_work2 = self.home / "work" / "def.json"
        for path, payload in ((legacy_project, b'{ "schema":1, "projects":{"old":{}} }\r\n'), (legacy_work1, b'{ "schema_version":1, "task_id":"old" }\r\n'), (legacy_work2, b'{ "schema_version":1, "task_id":"other" }\n')):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        before = {path: path.read_bytes() for path in (legacy_project, legacy_work1, legacy_work2)}
        self.view.import_registry(self.seed, imported_by=PRINCIPAL)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_frozen_and_corrupt_state_fail_closed(self):
        self.view.root.mkdir(parents=True)
        self.view.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(CoordError) as corrupt:
            self.view.list()
        self.assertEqual(corrupt.exception.code, "registry_corrupt")
        (self.home / "office" / "FROZEN").write_text("frozen", encoding="utf-8")
        with self.assertRaises(CoordError) as frozen:
            self.view.get("agent-stack")
        self.assertEqual(frozen.exception.code, "store_frozen")


if __name__ == "__main__":
    unittest.main()
