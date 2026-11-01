"""Secret-gate tests. Dummy values only — never real credentials."""
def _redacted_fixture(kind, ordinal, binary=False):
    """Return a public fixture derived only from category and ordinal."""
    seed = (len(kind) * 131 + ordinal * 7919 + 17) & 0x7fffffff
    chars = []
    for _ in range(40):
        seed = (seed * 1103515245 + 12345) & 0x7fffffff
        n = seed % 36
        chars.append(chr(97 + n) if n < 26 else chr(48 + n - 26))
    body = ''.join(chars)
    if kind == 'vendor-token':
        body = chr(103) + chr(104) + chr(112) + chr(95) + body
    elif kind == 'aws-access-key-id':
        body = chr(65) + chr(75) + chr(73) + chr(65) + body[:16].upper()
    elif kind == 'private-key':
        marker = (chr(80) + chr(82) + chr(73) + chr(86) + chr(65)
                  + chr(84) + chr(69) + chr(32) + chr(75) + chr(69) + chr(89))
        dash = chr(45) * 5
        body = (dash + chr(66) + chr(69) + chr(71) + chr(73) + chr(78)
                + chr(32) + marker + dash + chr(10) + body + chr(10)
                + dash + chr(69) + chr(78) + chr(68) + chr(32) + marker + dash)
    return body.encode('ascii') if binary else body


import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from caretaker.secrets import Verdict, inspect_path, inspect_text


# Dummy fragments that look like secrets but are not real credentials.
DUMMY_AWS = "AKIA" + ("A" * 16)
DUMMY_GH = "ghp_" + ("a" * 20)
DUMMY_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
    ".dummySignatureXX"
)
DUMMY_PEM = (_redacted_fixture('private-key', 1))
DUMMY_DSN = "postgres://user:dummyPass@localhost/db"


class VerdictShapeTests(unittest.TestCase):

    def test_verdict_is_immutable(self):
        v = inspect_text("plain knowledge note")
        self.assertIsInstance(v, Verdict)
        self.assertTrue(v.allowed)
        with self.assertRaises(Exception):
            v.allowed = False  # type: ignore[misc]


class Layer1DirectoryTests(unittest.TestCase):

    def test_blocked_dirs_rejected_without_opening(self):
        blocked = (
            Path("notes") / ".ssh" / "config",
            Path("notes") / ".aws" / "credentials",
            Path("notes") / ".git" / "config",
        )
        for path in blocked:
            with self.subTest(path=str(path)):
                opener = mock.MagicMock(side_effect=AssertionError("must not open"))
                with mock.patch("builtins.open", opener):
                    with mock.patch.object(Path, "open", opener):
                        with mock.patch.object(Path, "read_text", opener):
                            with mock.patch.object(Path, "read_bytes", opener):
                                verdict = inspect_path(path)
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.rule_id, "blocked_directory")
                opener.assert_not_called()


class Layer2FilenameTests(unittest.TestCase):

    def test_blocked_filenames(self):
        names = (
            ".env",
            ".env.local",
            "id_rsa",
            "id_rsa.pub",
            "credentials.json",
            "server.pem",
            "my_secret_notes.md",
        )
        for name in names:
            with self.subTest(name=name):
                verdict = inspect_path(Path("docs") / name)
                self.assertFalse(verdict.allowed, name)
                self.assertEqual(verdict.rule_id, "blocked_filename")


class Layer3ContentTests(unittest.TestCase):

    def test_dummy_secrets_rejected(self):
        samples = {
            "aws": f"export KEY={DUMMY_AWS}",
            "github": f"token {DUMMY_GH}",
            "jwt": DUMMY_JWT,
            "pem": DUMMY_PEM,
            "dsn": f"url {DUMMY_DSN}",
        }
        for label, body in samples.items():
            with self.subTest(label=label):
                verdict = inspect_text(body)
                self.assertFalse(verdict.allowed)
                self.assertEqual(verdict.rule_id, "secret_in_content")
                blob = f"{verdict.reason}{verdict.rule_id}"
                self.assertNotIn(body, blob)
                self.assertNotIn(DUMMY_AWS, blob)
                self.assertNotIn(DUMMY_GH, blob)
                self.assertNotIn(DUMMY_JWT, blob)
                self.assertNotIn("dummyPass", blob)
                self.assertNotIn("BEGIN OPENSSH", blob)

    def test_plain_text_allowed(self):
        verdict = inspect_text("Meeting notes: ship the files connector.")
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.rule_id, "")

    def test_fail_closed_non_utf8(self):
        verdict = inspect_text(b"\xff\xfe\x00not-utf8")
        self.assertFalse(verdict.allowed)
        self.assertIn(verdict.rule_id, ("not_utf8", "binary_content"))


class VerdictDoesNotLeakTests(unittest.TestCase):

    def test_rejection_has_no_secret_fragment(self):
        body = f"leak-me {DUMMY_AWS} and {DUMMY_GH}"
        verdict = inspect_text(body, path="notes/ok.md")
        self.assertFalse(verdict.allowed)
        dumped = repr(verdict)
        self.assertNotIn(DUMMY_AWS, dumped)
        self.assertNotIn(DUMMY_GH, dumped)
        self.assertNotIn("leak-me", dumped)
        self.assertNotIn(body, dumped)


class InspectPathDoesNotReadTests(unittest.TestCase):

    def test_missing_file_still_classifies_name(self):
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / ".aws" / "never-created"
            verdict = inspect_path(missing)
            self.assertFalse(verdict.allowed)
            self.assertEqual(verdict.rule_id, "blocked_directory")


if __name__ == "__main__":
    unittest.main()
