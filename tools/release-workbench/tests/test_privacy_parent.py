"""Parent regressions for credential precision and refusal-before-read."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from release_workbench.privacy import _MAX_FILE_BYTES, scan_tree, validate_policy


def _opaque(label: str) -> str:
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()
    return "zz" + digest[:24]


def _identifier(label: str) -> str:
    return "v" + hashlib.sha256(label.encode("utf-8")).hexdigest()[:12]


class ParentPrivacyRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="privacy-parent-", dir=Path(__file__).resolve().parents[1]
        )
        self.base = Path(self.temp.name)
        self.root = self.base / "candidate"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative: str, content: bytes | str) -> Path:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        target.write_bytes(content)
        return target

    @staticmethod
    def rules(report):
        return {(item["rule"], item["path"], item["line"]) for item in report["findings"]}

    def assert_value_hidden(self, report, *values: str):
        serialized = json.dumps(report)
        for value in values:
            self.assertNotIn(value, serialized)
        for finding in report["findings"]:
            self.assertEqual({"rule", "path", "line"}, set(finding))
        self.assertNotIn("content_sha256", serialized)
        self.assertIn("not a complete secret", report["limitations"][0])
        self.assertIn("permission to publish", report["limitations"][0])
        self.assertNotIn("public_ready", report)

    def test_code_references_are_not_credential_assignments(self):
        ident = _identifier("python-variable")
        compared = _opaque("comparison-call")
        source = "\n".join([
            "password" + " = " + "get_password()",
            "api_key" + " = " + "config['api_key']",
            "token" + " = " + "os.environ.get('TOKEN')",
            "secret" + " = " + "settings.secret",
            "client_secret" + " = " + "credentials['client_secret']",
            "refresh_token" + " = " + "os.getenv('REFRESH_TOKEN')",
            "access_token" + " = " + "self.access_token",
            "aws_secret_access_key" + " = " + "os.environ['AWS_SECRET_ACCESS_KEY']",
            "private_key" + " = " + ident,
            "token" + " == " + "get_password()",
            "password" + " != " + ident,
            "api_token" + " = " + "f\"{" + "user" + "}\"",
            "data" + " = " + "{\"password\": os.environ[\"PASSWORD\"]}",
        ]) + "\n"
        self.write("module.py", source)
        javascript = "\n".join([
            "const " + "password" + " = getPassword();",
            "let " + "apiKey" + " = config[\"apiKey\"];",
            "var " + "secret" + " = obj.secret;",
            "const " + "token" + " = process.env.TOKEN;",
            "const " + "refreshToken" + " = process.env[\"REFRESH_TOKEN\"];",
            "const " + "clientSecret" + " = " + ident + ";",
        ]) + "\n"
        self.write("app.js", javascript)
        self.write("ignored.txt", "token" + " == " + repr(compared) + "\n")

        report = scan_tree(self.root)

        self.assertEqual(
            {("privacy.credential_assignment", "ignored.txt", 1)},
            self.rules(report),
        )
        self.assert_value_hidden(report, ident, compared)

    def test_quoted_multiline_and_annotated_literals_are_detected(self):
        value = _opaque("quoted-literal")
        self.write("quoted.py", "api_token" + " = " + "'" + value + "'\n")
        self.write("multi.py", "password" + " = \"\"\"\n" + value + "\n\"\"\"\n")
        self.write("wrapped.py", "client_secret" + " = (\n    '" + value + "'\n)\n")
        self.write("annotated.py", "secret" + ": str = " + repr(value) + "\n")
        self.write("rawset.py", "token" + " = r\"" + value + "\"\n")
        self.write("block.yaml", "refresh_token: |\n  " + value + "\n")
        self.write("auth.py", "access_token" + " = " + repr(value) + "\n")

        report = scan_tree(self.root)

        self.assertEqual("FAIL", report["status"])
        expected = {
            ("privacy.credential_assignment", "quoted.py", 1),
            ("privacy.credential_assignment", "multi.py", 1),
            ("privacy.credential_assignment", "wrapped.py", 1),
            ("privacy.credential_assignment", "annotated.py", 1),
            ("privacy.credential_assignment", "rawset.py", 1),
            ("privacy.credential_assignment", "block.yaml", 1),
            ("privacy.credential_assignment", "auth.py", 1),
        }
        self.assertEqual(expected, self.rules(report))
        self.assert_value_hidden(report, value)

    def test_quoted_json_and_yaml_values_are_not_broadly_whitelisted(self):
        value = _opaque("json-literal")
        mixed = "example-test-synthetic-" + value
        embedded = "zzexampletest" + hashlib.sha256(b"embedded").hexdigest()[:10]
        self.write(
            "accounts.json",
            json.dumps({"password": value, "api_key": "your-token-here"}, indent=2) + "\n",
        )
        self.write(
            "split.json",
            "{\n" + '"client_secret":\n' + '  "' + value + '"\n}\n',
        )
        self.write(
            "config.yaml",
            "\n".join([
                '"refresh_token": "' + value + '"',
                "api_key: \"your-token-here\"",
                "client_secret: \"" + mixed + "\"",
                "password: \"" + embedded + "\"",
                "token: \"example-key\"",
                "secret: \"${SECRET}\"",
            ]) + "\n",
        )

        report = scan_tree(self.root)

        self.assertEqual(
            {
                ("privacy.credential_assignment", "accounts.json", 2),
                ("privacy.credential_assignment", "split.json", 2),
                ("privacy.credential_assignment", "config.yaml", 1),
                ("privacy.credential_assignment", "config.yaml", 3),
                ("privacy.credential_assignment", "config.yaml", 4),
            },
            self.rules(report),
        )
        self.assert_value_hidden(report, value, mixed, embedded)

    def test_config_literals_differ_from_variable_references(self):
        value = _opaque("config-literal")
        ident = _identifier("config-bareword")
        self.write(
            "settings.conf",
            "\n".join([
                "password=${PASSWORD}",
                "api_token=$API_TOKEN",
                "secret={{SECRET}}",
                "token=%TOKEN%",
                "client_secret=" + value,
                "refresh_token=" + ident,
            ]) + "\n",
        )
        self.write("module.py", "password" + " = " + ident + "\n")

        report = scan_tree(self.root)

        self.assertEqual(
            {
                ("privacy.credential_assignment", "settings.conf", 5),
                ("privacy.credential_assignment", "settings.conf", 6),
            },
            self.rules(report),
        )
        self.assert_value_hidden(report, value, ident)

    def test_env_and_credentials_paths_are_refused_before_read(self):
        marker = _opaque("unopened-marker")
        harmless = "feature = 1\n# " + marker + "\n"
        self.write(".envrc", harmless)
        self.write(".ENV", harmless)
        self.write("config/.environment", harmless)
        self.write("nested/.ENVRC/local.txt", harmless)
        self.write("NEST/Credentials/info.txt", harmless)
        self.write("Team/CREDENTIALS-PROD.JSON", harmless)
        self.write("file.env", "print('ok')\n")
        self.write("readme.txt", "print('hello')\n")

        opened: list[str] = []
        real_open = os.open

        def spy(path, flags, *args, **kwargs):
            opened.append(Path(path).as_posix())
            return real_open(path, flags, *args, **kwargs)

        with mock.patch("release_workbench.privacy.os.open", spy):
            report = scan_tree(self.root)

        def blocked(relative: str) -> bool:
            return any(
                part.casefold().startswith(".env") or part.casefold().startswith("credentials")
                for part in relative.split("/")
            )

        opened_names = {Path(path).name.casefold() for path in opened}
        self.assertTrue(opened)
        self.assertTrue(all(not blocked(path) for path in opened))
        self.assertIn("readme.txt", opened_names)
        self.assertIn("file.env", opened_names)
        self.assertEqual("FAIL", report["status"])
        self.assertEqual(2, report["files_scanned"])
        self.assertEqual(
            {
                ("scan.refused_sensitive_path", ".envrc", None),
                ("scan.refused_sensitive_path", ".ENV", None),
                ("scan.refused_sensitive_path", "config/.environment", None),
                ("scan.refused_sensitive_path", "nested/.ENVRC", None),
                ("scan.refused_sensitive_path", "NEST/Credentials", None),
                ("scan.refused_sensitive_path", "Team/CREDENTIALS-PROD.JSON", None),
            },
            self.rules(report),
        )
        self.assert_value_hidden(report, marker)
        self.assertNotIn(marker, json.dumps(report))

    def test_binary_oversize_link_and_exact_allowlist_remain_enforced(self):
        value = _opaque("allowlisted-literal")
        target = self.write("sample.txt", "token=" + value + "\n")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        policy = {
            "version": 1,
            "forbidden_literals": [],
            "allowlist": [{
                "rule": "privacy.credential_assignment",
                "path": "sample.txt",
                "content_sha256": digest,
            }],
        }
        allowed = scan_tree(self.root, policy=policy)
        self.assertEqual("PASS", allowed["status"])
        self.assertEqual(1, allowed["allowlisted_findings"])
        self.assertEqual([], allowed["findings"])
        validated = validate_policy(policy)
        self.assertEqual(1, validated["allowlist_count"])
        self.assertNotIn(value, json.dumps(validated))

        wrong_rule = {
            "version": 1,
            "forbidden_literals": [],
            "allowlist": [{
                "rule": "privacy.owner_path",
                "path": "sample.txt",
                "content_sha256": digest,
            }],
        }
        rejected = scan_tree(self.root, policy=wrong_rule)
        self.assertEqual("FAIL", rejected["status"])
        self.assertIn(("privacy.credential_assignment", "sample.txt", 1), self.rules(rejected))

        self.write("image.dat", b"\x89PNG\x00binary")
        oversized = self.root / "big.txt"
        with oversized.open("wb") as handle:
            handle.seek(_MAX_FILE_BYTES + 1)
            handle.write(b"x")
        self.assertGreater(oversized.stat().st_size, _MAX_FILE_BYTES)
        outside = self.base / "outside-secret.txt"
        outside.write_text("token=" + value + "\n", encoding="utf-8")
        link = self.root / "escape-link"
        link_ok = True
        try:
            os.symlink(outside, link, target_is_directory=False)
        except (OSError, NotImplementedError):
            link_ok = False

        opened: list[str] = []
        real_open = os.open

        def spy(path, flags, *args, **kwargs):
            opened.append(Path(path).name)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch("release_workbench.privacy.os.open", spy):
            report = scan_tree(self.root)

        self.assertNotIn("big.txt", opened)
        self.assertIn(("scan.binary_file", "image.dat", None), self.rules(report))
        self.assertIn(("scan.file_too_large", "big.txt", None), self.rules(report))
        if link_ok:
            self.assertNotIn(outside.name, opened)
            self.assertNotIn("escape-link", opened)
            self.assertIn(("scan.refused_link", "escape-link", None), self.rules(report))
        self.assert_value_hidden(report, value)


if __name__ == "__main__":
    unittest.main()
