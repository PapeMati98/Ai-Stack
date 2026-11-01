"""Offline synthetic coverage for the provenance inventory API."""

import hashlib
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

from release_workbench.provenance import audit_provenance, write_notices


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def manifest(*files: tuple[str, str]) -> dict:
    return {
        "version": 1,
        "sources": [
            {
                "id": "synthetic-source",
                "repo": "private fixture omitted",
                "commit": "a" * 40,
                "files": [
                    {"path": f"source/{index}.py", "target": target, "sha256": file_hash}
                    for index, (target, file_hash) in enumerate(files)
                ],
            }
        ],
    }


def component(
    component_id: str,
    targets: list[str],
    *,
    source: str = "fixture-library",
    license_name: str = "MIT",
    notices: list[str] | None = None,
    evidence: object = "recorded synthetic reference",
    status: str = "third-party",
) -> dict:
    return {
        "id": component_id,
        "targets": targets,
        "source": source,
        "license": license_name,
        "notice_paths": [] if notices is None else notices,
        "evidence": evidence,
        "status": status,
    }


class ProvenanceTests(unittest.TestCase):
    def test_exact_coverage_and_component_notice_hash_deduplication(self) -> None:
        code_hash = digest("same archived component bytes")
        notice_hash = digest("same retained notice bytes")
        data = manifest(
            ("copies/first.py", code_hash),
            ("copies/second.py", code_hash),
            ("notices/first.txt", notice_hash),
            ("notices/second.txt", notice_hash),
        )
        report = audit_provenance(
            data,
            [
                component(
                    "copy-a",
                    ["copies/first.py", "notices/first.txt"],
                    notices=["notices/first.txt"],
                ),
                component(
                    "copy-b",
                    ["copies/second.py", "notices/second.txt"],
                    notices=["notices/second.txt"],
                ),
            ],
        )

        self.assertEqual(report["inventory_status"], "PASS")
        self.assertEqual(report["status"], "OPEN")
        self.assertFalse(report["public_ready"])
        self.assertEqual(report["counts"]["covered_files_once"], 4)
        self.assertEqual(report["counts"]["unique_components"], 1)
        self.assertEqual(report["counts"]["duplicate_component_copies"], 1)
        self.assertEqual(report["counts"]["unique_notice_hashes"], 1)

    def test_missing_and_double_file_coverage_are_open(self) -> None:
        data = manifest(("pkg/a.py", digest("a")), ("pkg/b.py", digest("b")))
        missing = audit_provenance(data, [component("a", ["pkg/a.py"])])
        self.assertIn("file_coverage_missing", {item["code"] for item in missing["findings"]})
        self.assertEqual(missing["inventory_status"], "OPEN")

        doubled = audit_provenance(
            manifest(("pkg/a.py", digest("a"))),
            [component("a", ["pkg/a.py"]), component("b", ["pkg/a.py"], source="another")],
        )
        self.assertIn("file_coverage_duplicate", {item["code"] for item in doubled["findings"]})
        self.assertEqual(doubled["counts"]["covered_files_once"], 0)

    def test_unknown_rights_and_empty_or_fake_evidence_remain_open(self) -> None:
        data = manifest(("pkg/a.py", digest("a")))
        empty = audit_provenance(
            data,
            [component("empty", ["pkg/a.py"], license_name="", status="unknown", evidence=[])],
        )
        codes = {item["code"] for item in empty["findings"]}
        self.assertTrue({"license_unknown", "ownership_unknown", "evidence_missing"}.issubset(codes))
        self.assertEqual(empty["status"], "OPEN")

        fake_marker = "DO_NOT_EXPORT_FAKE_EVIDENCE"
        fake = audit_provenance(
            data,
            [component("fake", ["pkg/a.py"], evidence={"unverified": fake_marker})],
        )
        self.assertEqual(fake["status"], "OPEN")
        self.assertIn("rights_evidence_not_independently_verified", {item["code"] for item in fake["findings"]})
        self.assertNotIn(fake_marker, repr(fake))

    def test_ambiguous_and_case_mismatched_notice_paths_stay_open(self) -> None:
        collision = manifest(("LICENSE", digest("one")), ("license", digest("two")))
        ambiguous = audit_provenance(
            collision,
            [component("license-owner", ["LICENSE"], notices=["LICENSE"])],
        )
        self.assertIn("notice_path_ambiguous", {item["code"] for item in ambiguous["findings"]})
        self.assertEqual(ambiguous["inventory_status"], "OPEN")

        case_mismatch = audit_provenance(
            manifest(("NOTICE", digest("notice"))),
            [component("notice-owner", ["NOTICE"], notices=["notice"])],
        )
        self.assertIn("notice_path_case_mismatch", {item["code"] for item in case_mismatch["findings"]})

    def test_write_notices_is_new_bounded_and_does_not_copy_evidence_or_license_text(self) -> None:
        report = audit_provenance(
            manifest(("pkg/a.py", digest("a")), ("LICENSE", digest("license bytes"))),
            [
                component(
                    "pkg-a",
                    ["pkg/a.py", "LICENSE"],
                    notices=["LICENSE"],
                    evidence="PRIVATE_EVIDENCE_VALUE",
                    license_name="owner-declared-label",
                )
            ],
        )
        destination = Path("OWNER_NOTICES.md")
        opened = mock_open()
        with patch.object(Path, "open", autospec=True, return_value=opened()) as open_path:
            result = write_notices(report, destination)
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(open_path.call_args.args[1], "x")
        content = opened().write.call_args.args[0]
        self.assertEqual(result["bytes"], len(content.encode("utf-8")))
        self.assertEqual(result["sha256"], digest(content))
        self.assertIn("Public readiness: `false`", content)
        self.assertIn("Notice SHA256:", content)
        self.assertIn("owner-declared-label", content)
        self.assertNotIn("PRIVATE_EVIDENCE_VALUE", content)
        self.assertNotIn("MIT License", content)
        with patch.object(Path, "open", autospec=True, side_effect=FileExistsError):
            with self.assertRaisesRegex(ValueError, "already exists"):
                write_notices(report, destination)

    def test_oversized_notice_inventory_fails_before_opening_destination(self) -> None:
        report = audit_provenance(
            manifest(("pkg/a.py", digest("a"))),
            [component("pkg-a", ["pkg/a.py"])],
        )
        report["components"][0]["id"] = "component-" + "x" * (128 * 1024)
        with patch.object(Path, "open", autospec=True) as open_path:
            with self.assertRaisesRegex(ValueError, "bounded output size"):
                write_notices(report, "never-created.md")
        open_path.assert_not_called()

    def test_components_require_exact_keys_and_literal_paths(self) -> None:
        data = manifest(("pkg/a.py", digest("a")))
        invalid = component("a", ["pkg/a.py"])
        invalid["unreviewed"] = "ignored fields are unsafe"
        with self.assertRaisesRegex(ValueError, "exactly the documented keys"):
            audit_provenance(data, [invalid])
        with self.assertRaisesRegex(ValueError, "traversal segments"):
            audit_provenance(data, [component("a", ["../escape.py"])])


if __name__ == "__main__":
    unittest.main()
