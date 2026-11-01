from __future__ import annotations

from pathlib import Path

from coordfs.constants import LEASE_FIELDS
from coordfs.leases import encode_lease, parse_lease

from support import CoordTestCase


class LeaseSchemaTests(CoordTestCase):
    def test_claim_writes_required_fields_only(self) -> None:
        target = self.cwd / "note.txt"
        target.write_text("hello", encoding="utf-8")
        result = self.board.claim(str(target), "agent-a", ttl_s=30, meta={"task_id": "task-1"})
        self.assertEqual(result["status"], "ok")
        lease = result["lease"]
        self.assertEqual(tuple(LEASE_FIELDS), ("resource_id", "holder_agent", "acquired_at", "ttl_s", "file_hash", "meta"))
        self.assertEqual(set(lease), set(LEASE_FIELDS))
        self.assertEqual(lease["holder_agent"], "agent-a")
        self.assertEqual(lease["ttl_s"], 30)
        self.assertEqual(lease["meta"]["task_id"], "task-1")
        self.assertTrue(lease["file_hash"])
        parsed = parse_lease(encode_lease(lease))
        self.assertEqual(parsed, lease)

    def test_lock_path_is_sha256_of_canonical_resource(self) -> None:
        result = self.board.claim("data:asset-1", "agent-a", ttl_s=30)
        lock_name = result["lock_name"]
        self.assertTrue(lock_name.endswith(".lock"))
        self.assertEqual(len(lock_name), 64 + 5)
        again = self.board.who("data:asset-1")
        self.assertEqual(again["holder_agent"], "agent-a")

    def test_scope_and_data_use_lease_only_not_a_fake_hash(self) -> None:
        scope = self.board.claim("scope:frontend/src/", "agent-scope", ttl_s=30)
        data = self.board.claim("data:asset-lease-only", "agent-data", ttl_s=30)
        self.assertEqual(scope["lease"]["file_hash"], "")
        self.assertEqual(data["lease"]["file_hash"], "")

        package_readme = Path(__file__).resolve().parents[1] / "README.md"
        contract = package_readme.read_text(encoding="utf-8")
        self.assertIn("scope: and data: use lease-only protection", contract)
