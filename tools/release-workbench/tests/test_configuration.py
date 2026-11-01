from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from release_workbench.configuration import render_template, validate_config, verify_portability


WORKTREE_ROOT = Path(__file__).resolve().parents[1]


def task_temporary_directory() -> tempfile.TemporaryDirectory:
    """Keep synthetic fixtures inside this task's writable worktree."""
    worktree = Path(__file__).resolve().parents[1]
    return tempfile.TemporaryDirectory(dir=worktree)


def valid_config() -> dict:
    return {
        "version": 1,
        "stack_root": "stack",
        "state_root": "private/state",
        "runtime_root": "runtime",
        "enabled_clis": [],
        "external_integrations": [],
        "network_integrations_enabled": False,
        "credential_references": {},
    }


class ValidateConfigTests(unittest.TestCase):
    def test_accepts_secret_free_config_under_independent_roots(self) -> None:
        with task_temporary_directory() as first, task_temporary_directory() as second:
            first_result = validate_config(valid_config(), owned_root=Path(first))
            second_config = valid_config()
            second_config["stack_root"] = "other-stack"
            second_result = validate_config(second_config, owned_root=Path(second))

        self.assertEqual(first_result["status"], "PASS")
        self.assertEqual(first_result["roots"]["state_root"], "private/state")
        self.assertEqual(second_result["roots"]["stack_root"], "other-stack")
        self.assertNotEqual(first, second)

    def test_rejects_traversal_and_non_literal_paths(self) -> None:
        bad_values = ("../outside", "stack/../../outside", "C:/outside", "%USERPROFILE%/x", "~/private", "a//b", "a/./b")
        for value in bad_values:
            with self.subTest(value=value):
                data = valid_config()
                data["state_root"] = value
                with self.assertRaises(ValueError):
                    validate_config(data, owned_root=WORKTREE_ROOT)

    def test_rejects_symlink_ancestor(self) -> None:
        with task_temporary_directory() as parent, task_temporary_directory() as outside:
            root = Path(parent) / "owned"
            root.mkdir()
            link = root / "redirect"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink unavailable: {type(exc).__name__}")
            data = valid_config()
            data["state_root"] = "redirect/state"
            with self.assertRaisesRegex(ValueError, "symlink or reparse-point"):
                validate_config(data, owned_root=root)

    def test_rejects_malformed_unknown_and_unsafe_integration_schema(self) -> None:
        cases = []
        malformed = valid_config()
        malformed["version"] = True
        cases.append(malformed)
        unknown = valid_config()
        unknown["unexpected"] = 1
        cases.append(unknown)
        secret_assignment = valid_config()
        secret_assignment["api_key"] = "sentinel-secret-value"
        cases.append(secret_assignment)
        enabled_production = valid_config()
        enabled_production["external_integrations"] = [{"id": "production-api", "enabled": True}]
        cases.append(enabled_production)
        extra_integration_field = valid_config()
        extra_integration_field["external_integrations"] = [
            {"id": "example", "enabled": False, "endpoint": "https://example.invalid"}
        ]
        cases.append(extra_integration_field)
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    validate_config(data, owned_root=WORKTREE_ROOT)

    def test_rejects_plaintext_credentials_and_separates_opaque_references(self) -> None:
        data = valid_config()
        data["enabled_clis"] = [{"agent": "codex", "model": "gpt-6.1-sol"}]
        data["credential_references"] = {"codex": "plaintext-secret-value"}
        with self.assertRaises(ValueError) as raised:
            validate_config(data, owned_root=WORKTREE_ROOT)
        self.assertNotIn("plaintext-secret-value", str(raised.exception))

        data["credential_references"] = {"codex": "os-credential://windows-credential-manager/codex"}
        result = validate_config(data, owned_root=WORKTREE_ROOT)
        self.assertEqual(result["credential_reference_count"], 1)
        self.assertNotIn("windows-credential-manager", json.dumps(result))

    def test_rejects_credentials_for_unconfigured_cli_and_enabled_network(self) -> None:
        data = valid_config()
        data["credential_references"] = {"codex": "os-credential://keyring/native-account"}
        with self.assertRaises(ValueError):
            validate_config(data, owned_root=WORKTREE_ROOT)
        data["credential_references"] = {}
        data["network_integrations_enabled"] = True
        with self.assertRaises(ValueError):
            validate_config(data, owned_root=WORKTREE_ROOT)


class RenderTemplateTests(unittest.TestCase):
    def test_substitutes_only_explicit_variables_once(self) -> None:
        rendered = render_template("root={{ROOT}}; keep={{ROOT}}", {"ROOT": "private/project"})
        self.assertEqual(rendered, "root=private/project; keep=private/project")

    def test_rejects_missing_unused_unknown_malformed_and_nested_placeholders(self) -> None:
        cases = (
            ("{{ROOT}}", {}),
            ("literal", {"ROOT": "unused"}),
            ("{{UNKNOWN}}", {"ROOT": "value"}),
            ("{{ROOT", {"ROOT": "value"}),
            ("{{ROOT}}", {"ROOT": "{{OTHER}}"}),
        )
        for text, variables in cases:
            with self.subTest(text=text, variables=variables):
                with self.assertRaises(ValueError):
                    render_template(text, variables)

    def test_rejects_traversal_home_and_environment_expansion_values(self) -> None:
        for value in (
            "../outside",
            "nested/../../outside",
            "~/private",
            "$HOME/private",
            "%USERPROFILE%\\private",
            "C:\\private",
            "C:relative\\private",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    render_template("{{ROOT}}", {"ROOT": value})


class VerifyPortabilityTests(unittest.TestCase):
    def test_scans_the_secret_free_example_config(self) -> None:
        result = verify_portability(WORKTREE_ROOT / "config")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["files_scanned"], 1)
        self.assertEqual(result["findings"], [])

    def test_reports_supplied_literal_without_echoing_it(self) -> None:
        result = verify_portability(WORKTREE_ROOT / "config", forbidden_literals=["runtime"])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["findings"], [{"rule": "topology_literal_1", "path": "stack.example.json", "line": 5}])
        self.assertNotIn("runtime", json.dumps(result))

    def test_reports_owner_path_and_topology_literals_without_values(self) -> None:
        with task_temporary_directory() as root:
            sample = Path(root) / "nested" / "sample.txt"
            sample.parent.mkdir()
            sample.write_text(
                "local D:\\Synthetic Project\\private\\settings.json\nendpoint internal.example\n",
                encoding="utf-8",
            )
            result = verify_portability(root, forbidden_literals=["internal.example"])

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["finding_count"], 2)
        self.assertEqual(
            {(item["rule"], item["line"]) for item in result["findings"]},
            {("owner_path", 1), ("topology_literal_1", 2)},
        )
        serialized = json.dumps(result)
        self.assertNotIn("internal.example", serialized)
        self.assertNotIn("Synthetic Project", serialized)

    def test_flags_generic_other_user_and_root_home_paths(self) -> None:
        with task_temporary_directory() as root:
            sample = Path(root) / "source.py"
            sample.write_text(
                "other = \"C:/Users/other/private/file.py\"\n"
                "privileged = \"/home/root/data/state.json\"\n",
                encoding="utf-8",
            )
            result = verify_portability(root)

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["files_scanned"], 1)
        self.assertEqual(
            [(item["rule"], item["line"]) for item in result["findings"]],
            [("owner_path", 1), ("owner_path", 2)],
        )
        serialized = json.dumps(result)
        self.assertNotIn("C:/Users/other/private/file.py", serialized)
        self.assertNotIn("/home/root/data/state.json", serialized)

    def test_leaves_url_and_relative_paths_clean(self) -> None:
        with task_temporary_directory() as root:
            sample = Path(root) / "source.py"
            sample.write_text(
                "endpoint = \"https://example.invalid/C:/fixture/file.py\"\n"
                "guide = \"docs/home/example/file.md\"\n",
                encoding="utf-8",
            )
            result = verify_portability(root)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["files_scanned"], 1)
        self.assertEqual(result["findings"], [])

    def test_flags_markdown_wrapped_home_paths(self) -> None:
        with task_temporary_directory() as root:
            sample = Path(root) / "notes.md"
            sample.write_text(
                "state in `/home/example/state.json`\n"
                "see [state](/Users/example/state.json)\n",
                encoding="utf-8",
            )
            result = verify_portability(root)

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["files_scanned"], 1)
        self.assertEqual(
            [(item["rule"], item["line"]) for item in result["findings"]],
            [("owner_path", 1), ("owner_path", 2)],
        )
        serialized = json.dumps(result)
        self.assertNotIn("/home/example/state.json", serialized)
        self.assertNotIn("/Users/example/state.json", serialized)

    def test_leaves_url_embedded_drive_text_clean(self) -> None:
        with task_temporary_directory() as root:
            sample = Path(root) / "source.py"
            sample.write_text(
                "endpoint = 'https://example.invalid/archive-C:/fixture'\n"
                "local = 'C:/fixture/state.json'\n",
                encoding="utf-8",
            )
            result = verify_portability(root)

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["files_scanned"], 1)
        self.assertEqual(
            [(item["rule"], item["line"]) for item in result["findings"]],
            [("owner_path", 2)],
        )
        serialized = json.dumps(result)
        self.assertNotIn("C:/fixture/state.json", serialized)

    def test_leaves_url_assignment_paths_clean(self) -> None:
        with task_temporary_directory() as root:
            sample = Path(root) / "source.py"
            sample.write_text(
                "endpoint = \"https://example.invalid/archive=C:/fixture\"\n"
                "state = \"https://example.invalid/archive=/home/example/state.json\"\n"
                "other = \"https://example.invalid/archive=/Users/example/state.json\"\n",
                encoding="utf-8",
            )
            result = verify_portability(root)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["files_scanned"], 1)
        self.assertEqual(result["findings"], [])

    def test_flags_bare_assignment_owner_paths(self) -> None:
        with task_temporary_directory() as root:
            sample = Path(root) / "source.py"
            sample.write_text(
                "OUTPUT=C:/fixture/state.json\n"
                "STATE=/home/example/state.json\n",
                encoding="utf-8",
            )
            result = verify_portability(root)

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["files_scanned"], 1)
        self.assertEqual(
            [(item["rule"], item["line"]) for item in result["findings"]],
            [("owner_path", 1), ("owner_path", 2)],
        )
        serialized = json.dumps(result)
        self.assertNotIn("C:/fixture/state.json", serialized)
        self.assertNotIn("/home/example/state.json", serialized)

    def test_url_punctuation_and_local_paths_on_same_line(self) -> None:
        with task_temporary_directory() as root:
            Path(root, "cases.txt").write_text(
                'url="https://example.invalid/a(C:/fixture)[/home/example]?x=/Users/example#part"\n'
                '[guide](custom+scheme://example.invalid/archive(/home/example))\n'
                'relative/C:/fixture docs/home/example\n'
                'url="https://example.invalid/a(C:/remote)" local="/home/example/state"\n'
                'https://example.invalid/path C://fixture/state\n'
                'ROOT=C:/fixture/state\n'
                '[state](/Users/example/state)\n',
                encoding="utf-8",
            )
            result = verify_portability(root)

        self.assertEqual(
            [(item["rule"], item["line"]) for item in result["findings"]],
            [("owner_path", 4), ("owner_path", 5), ("owner_path", 6), ("owner_path", 7)],
        )
        self.assertNotIn("/home/example/state", json.dumps(result))

    def test_topology_literal_in_url_is_checked_on_original_line(self) -> None:
        with task_temporary_directory() as root:
            Path(root, "cases.txt").write_text(
                "https://example.invalid/archive(/home/example)/Marker-Topology\n",
                encoding="utf-8",
            )
            result = verify_portability(root, forbidden_literals=["marker-topology"])

        self.assertEqual(
            result["findings"],
            [{"rule": "topology_literal_1", "path": "cases.txt", "line": 1}],
        )
        self.assertNotIn("marker-topology", json.dumps(result).lower())

    def test_clean_root_passes_and_unscannable_input_is_reported(self) -> None:
        with task_temporary_directory() as root:
            Path(root, "clean.txt").write_text("example.invalid\n", encoding="utf-8")
            clean = verify_portability(root)
            Path(root, "opaque.bin").write_bytes(b"\x00\x01\xff")
            unscannable = verify_portability(root)

        self.assertEqual(clean["status"], "PASS")
        self.assertEqual(unscannable["status"], "FAIL")
        self.assertIn("unscannable", {item["rule"] for item in unscannable["findings"]})

    def test_rejects_relative_scan_roots(self) -> None:
        with self.assertRaises(ValueError):
            verify_portability("relative/path")


if __name__ == "__main__":
    unittest.main()
