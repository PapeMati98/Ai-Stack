"""`coord claim --meta` runs the same secret preflight as body, status, refs, and workspace.

Every value this fixture writes is built at run time from a seed and a small
alphabet: no literal secret-shaped text appears in this file, and the private
backup that scans for embedded tokens must not flag it. The preflight is a
write-side rule only; reading or renewing an existing lease must stay untouched
because the validator never re-judges values already on disk.
"""

from __future__ import annotations

_public_fixture_pattern_1 = 'public_fixture_pattern_1'
_public_fixture_pattern_2 = 'public_fixture_pattern_2'
import argparse
import base64
import json
import string
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from coordfs.cli import _meta_from_args
from coordfs.errors import CoordError
from coordfs.office import detect_secret

COORD_PY = PACKAGE_ROOT / "coord.py"


def _synthetic_value(seed: str, length: int = 24) -> str:
    """Assemble a secret-shaped value out of fragments at run time.

    No literal secret-shaped text is written into this file: every value the
    preflight is tested with is built here from a seed and an alphabet.
    """
    alphabet = string.ascii_lowercase + string.digits
    return "".join(
        alphabet[(ord(char) + position * 5) % len(alphabet)]
        for position, char in enumerate((seed * length)[:length])
    )


def _synthetic_json_password(seed: str) -> str:
    """A password as a JSON value: the key is quoted, so no assignment shape sees it."""
    return json.dumps({"password": _synthetic_value(seed)})


def _synthetic_json_password_tab(seed: str) -> str:
    """JSON with a tab between the colon and the password value."""
    return json.dumps({"password": _synthetic_value(seed)}, separators=(",", ":\t"))


def _synthetic_json_password_newline(seed: str) -> str:
    """JSON with a newline between the colon and the password value."""
    return json.dumps({"password": _synthetic_value(seed)}, separators=(",", ":\n"))


def _synthetic_credential_url(seed: str) -> str:
    """A URL carrying login credentials in its userinfo, assembled from fragments."""
    return "".join(
        (
            "post",
            "gres://",
            "app",
            ":",
            _synthetic_value(seed, 18),
            "@",
            "db.internal.example",
            ":5432",
            "/app",
        )
    )


def _synthetic_jwt(seed: str) -> str:
    """Three base64url segments; the header is built here so no literal token is stored."""

    def segment(text: str) -> str:
        return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")

    header = segment('{"ty' + 'p":"JWT","al' + 'g":"none"}')
    claims = segment('{"su' + 'b":"' + seed + '"}')
    signature = segment("signature-" + seed + "-0123456789abcdef")
    return ".".join((header, claims, signature))


def _synthetic_bearer_next_line(seed: str) -> str:
    """`Bearer` with its token on the next line; the newline survives only unwrapped."""
    return "Authorization: Bearer\n" + _synthetic_value(seed)


def _synthetic_quoted_password(seed: str) -> str:
    """A quoted password, the shape JSON escaping used to hide from the raw scan."""
    return "password" + "=" + '"' + _synthetic_value(seed) + '"'


def _json_unicode_escape(char: str) -> str:
    """One character as a JSON Unicode escape sequence, assembled from fragments."""
    return "\\" + "u" + format(ord(char), "04x")


def _assignment_key() -> str:
    """The secret-shaped key name, assembled so no literal pattern sits in this file."""
    return "pass" + "word"


def _synthetic_meta_unicode_escape_in_key(seed: str) -> str:
    """Raw --meta JSON text with one letter of the secret key written as a Unicode escape.

    The escape hides the assignment from every scan that sees the raw text or the
    decoded key alone; only the re-serialized decoded object puts the key and its
    value back into one assignment shape.
    """
    key = _json_unicode_escape("p") + _assignment_key()[1:]
    return '{"' + key + '": "' + _synthetic_value(seed) + '"}'


def _synthetic_meta_unicode_escape_in_value(seed: str) -> str:
    """Raw --meta JSON text with the first character of the secret value escaped."""
    value = _synthetic_value(seed)
    escaped_first = _json_unicode_escape(value[0])
    return (
        '{"'
        + _assignment_key()
        + '": "'
        + escaped_first
        + value[1:]
        + '"}'
    )


def _synthetic_meta_unicode_escape_nested(seed: str) -> str:
    """Raw --meta JSON text with a Unicode-escaped key and value inside a nested object."""
    value = _synthetic_value(seed)
    key = _json_unicode_escape("p") + _assignment_key()[1:]
    escaped_first = _json_unicode_escape(value[0])
    return (
        '{"outer": {"inner": {"'
        + key
        + '": "'
        + escaped_first
        + value[1:]
        + '"}}}'
    )


def _secret_meta_values() -> dict[str, str]:
    return {
        "json-password": _synthetic_json_password("json"),
        "json-password-tab": _synthetic_json_password_tab("jst"),
        "json-password-newline": _synthetic_json_password_newline("jsn"),
        "url-credentials": _synthetic_credential_url("url"),
        "jwt": _synthetic_jwt("jwt"),
        "bearer-next-line": _synthetic_bearer_next_line("bearer"),
    }


def _ordinary_meta_values() -> dict[str, object]:
    return {
        "plain-text": "please rotate your password before the next release",
        "url-without-credentials": "https://example.com/team/alpha/wiki",
        "url-with-port": "https://example.com:8443/team/alpha",
        "non-string-int": 42,
        "non-string-bool": True,
        "non-string-list": [1, 2, 3],
        "json-without-secret": json.dumps(
            {"status": "ok", "note": "the password policy changed"}
        ),
    }


class MetaPreflightTests(unittest.TestCase):
    """The CLI claim path runs the same secret detector as body, refs, and workspace."""

    def _args(self, meta_json: str | None):
        # `_meta_from_args` only reads `args.meta`, so the namespace only needs that field.
        return argparse.Namespace(meta=meta_json)

    def test_every_synthetic_secret_in_meta_is_refused_before_write(self) -> None:
        for label, secret in _secret_meta_values().items():
            with self.subTest(field=label):
                self.assertIsNotNone(detect_secret(secret), secret)
                with self.assertRaises(CoordError) as refusal:
                    _meta_from_args(self._args(json.dumps({"note": secret})))
                self.assertEqual(refusal.exception.code, "secret_detected", refusal.exception)

    def test_plain_meta_values_pass_the_preflight(self) -> None:
        for label, value in _ordinary_meta_values().items():
            with self.subTest(field=label):
                parsed = _meta_from_args(self._args(json.dumps({"note": value})))
                self.assertEqual(parsed["note"], value)

    def test_non_string_meta_values_are_kept_intact(self) -> None:
        meta = {"count": 7, "flag": False, "items": ["a", "b"], "nested": {"k": 1}}
        self.assertEqual(
            _meta_from_args(self._args(json.dumps(meta))), meta,
            "non-string values must reach the lease untouched",
        )

    def test_non_object_meta_is_rejected_with_invalid_meta_code(self) -> None:
        with self.assertRaises(CoordError) as refusal:
            _meta_from_args(self._args(json.dumps(["not", "a", "dict"])))
        self.assertEqual(refusal.exception.code, "invalid_meta")

    def test_nested_secret_values_are_refused(self) -> None:
        secret = _synthetic_json_password("nested")
        with self.assertRaises(CoordError) as refusal:
            _meta_from_args(self._args(json.dumps({"outer": {"inner": secret}})))
        self.assertEqual(refusal.exception.code, "secret_detected")

    def test_secret_values_in_lists_are_refused(self) -> None:
        secret = _synthetic_bearer_next_line("list")
        with self.assertRaises(CoordError) as refusal:
            _meta_from_args(self._args(json.dumps({"items": [secret]})))
        self.assertEqual(refusal.exception.code, "secret_detected")

    def test_secret_in_key_is_refused(self) -> None:
        secret_key = ( _synthetic_quoted_password(_public_fixture_pattern_1) )
        with self.assertRaises(CoordError) as refusal:
            _meta_from_args(self._args(json.dumps({secret_key: "value"})))
        self.assertEqual(refusal.exception.code, "secret_detected")

    def test_refusal_message_does_not_contain_the_raw_key(self) -> None:
        secret_key = ( _synthetic_quoted_password(_public_fixture_pattern_2) )
        meta = json.dumps({secret_key: "value"})
        with self.assertRaises(CoordError) as refusal:
            _meta_from_args(self._args(meta))
        self.assertEqual(refusal.exception.code, "secret_detected")
        self.assertNotIn(secret_key, str(refusal.exception))

    def test_escaped_secret_shapes_are_caught_by_recursive_scan(self) -> None:
        # JSON escaping hides the shape from the raw-string scan; only the
        # recursive walk over decoded keys and values catches it.
        sample = _synthetic_quoted_password("escaped")
        self.assertIsNotNone(detect_secret(sample), sample)
        wrapped = json.dumps({"note": sample})
        self.assertIsNone(detect_secret(wrapped), wrapped)
        with self.assertRaises(CoordError) as refusal:
            _meta_from_args(self._args(wrapped))
        self.assertEqual(refusal.exception.code, "secret_detected")

    def test_unicode_escape_in_key_is_refused(self) -> None:
        # A Unicode escape inside the key hides the assignment from the raw text
        # and from a scan of the decoded key alone; the re-serialized object is
        # what restores the key next to its value.
        raw = _synthetic_meta_unicode_escape_in_key("ukey")
        self.assertIsNone(detect_secret(raw), raw)
        loaded = json.loads(raw)
        self.assertIsNone(detect_secret(next(iter(loaded))), "decoded key alone")
        self.assertIsNone(detect_secret(next(iter(loaded.values()))), "decoded value alone")
        self.assertIsNotNone(
            detect_secret(json.dumps(loaded, ensure_ascii=False)),
            "re-serialized meta must expose the assignment",
        )
        with self.assertRaises(CoordError) as refusal:
            _meta_from_args(self._args(raw))
        self.assertEqual(refusal.exception.code, "secret_detected")

    def test_unicode_escape_in_value_is_refused(self) -> None:
        # A Unicode escape on the first character of the value hides the
        # assignment from the raw text; the decoded value alone is not an
        # assignment either, so only the re-serialized object catches it.
        raw = _synthetic_meta_unicode_escape_in_value("uval")
        self.assertIsNone(detect_secret(raw), raw)
        loaded = json.loads(raw)
        self.assertIsNone(detect_secret(next(iter(loaded))), "decoded key alone")
        self.assertIsNone(detect_secret(next(iter(loaded.values()))), "decoded value alone")
        self.assertIsNotNone(
            detect_secret(json.dumps(loaded, ensure_ascii=False)),
            "re-serialized meta must expose the assignment",
        )
        with self.assertRaises(CoordError) as refusal:
            _meta_from_args(self._args(raw))
        self.assertEqual(refusal.exception.code, "secret_detected")

    def test_unicode_escape_in_nested_meta_is_refused(self) -> None:
        # The same bypass works one level down: a Unicode-escaped key and value
        # inside a nested object still reach claim unless the whole decoded
        # object is re-serialized and scanned.
        raw = _synthetic_meta_unicode_escape_nested("unest")
        self.assertIsNone(detect_secret(raw), raw)
        with self.assertRaises(CoordError) as refusal:
            _meta_from_args(self._args(raw))
        self.assertEqual(refusal.exception.code, "secret_detected")


class MetaPreflightCliTests(unittest.TestCase):
    """End-to-end: the preflight surfaces through the deployed ``coord.py`` subprocess."""

    def setUp(self) -> None:
        # mkdtemp without a registered cleanup: the no-delete constraint forbids
        # adding shutil.rmtree (which TemporaryDirectory.cleanup calls) to this tree.
        self.root = Path(tempfile.mkdtemp(prefix="meta-preflight-test-"))
        self.home = self.root / "coord-home"
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()

    def _run_claim(self, meta: str) -> tuple[int, dict[str, object]]:
        env = {
            "COORDFS_HOME": str(self.home),
            "COORDFS_SESSION_ID": "meta-preflight-session",
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(COORD_PY),
                "claim",
                "--resource",
                "data:meta-preflight",
                "--agent-id",
                "grok",
                "--ttl",
                "30",
                "--meta",
                meta,
            ],
            cwd=str(self.cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        return completed.returncode, json.loads(completed.stdout)

    def test_cli_claim_refuses_a_meta_value_matching_a_secret_pattern(self) -> None:
        meta = json.dumps({"note": "Bearer " + _synthetic_value("b", 24)})
        code, payload = self._run_claim(meta)
        self.assertEqual(code, 1, payload)
        self.assertEqual(payload.get("status"), "error", payload)
        self.assertEqual(payload.get("code"), "secret_detected", payload)

    def test_cli_claim_accepts_a_meta_value_without_a_secret(self) -> None:
        meta = json.dumps({"note": "rotate your password before the next release"})
        code, payload = self._run_claim(meta)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload.get("status"), "ok", payload)
        self.assertEqual(
            payload.get("lease", {}).get("meta", {}).get("note"),
            "rotate your password before the next release",
            payload,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
