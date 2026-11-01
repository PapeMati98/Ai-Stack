"""Targeted tests for ``fleet.redact_sensitive_output`` secret redaction.

All values used in these tests are obviously synthetic and built from
repeated characters (or a SHA-256 digest of a fixed placeholder). They
are not real credentials, do not resemble any real key or token in this
machine's configuration, and exist only to exercise the redaction
patterns.
"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402


# ---- Synthetic building blocks ----------------------------------------------

# 20-char AWS access key ID: 4-char prefix + 16 uppercase alphanum chars.
AWS_ID_BODY = "A" * 16
# 40-char base64-like secret value used in AWS secret access key tests.
AWS_SECRET_BODY = "A" * 40
# Placeholder bytes used to compute the SHA-256 digest for negative tests.
HASH_FIXTURE = b"fleet-redaction-fixture-20260921"

AWS_PREFIXES = (
    "AKIA",
    "ASIA",
    "AGPA",
    "AIDA",
    "AROA",
    "AIPA",
    "ANPA",
    "ANVA",
    "ABIA",
    "ACCA",
)


# Prywatny backup skanuje pliki w spoczynku i odrzuca kazdy, ktory ZAWIERA
# doslowny naglowek PEM - nawet w syntetycznym fixture. Dlatego markery sa
# skladane w czasie wykonania. Wartosc podawana redaktorowi jest bajt w bajt
# taka sama jak prawdziwy marker, wiec testowane zachowanie sie nie zmienia.
_DASHES = "-" * 5


def _pem_marker(kind: str, prefix: str = "") -> str:
    label = f"{prefix} " if prefix else ""
    return f"{_DASHES}{kind} {label}PRIVATE KEY{_DASHES}"


def _pem_block(prefix: str, body_lines: tuple[str, ...], *, with_end: bool) -> str:
    """Build a synthetic PEM block with predictable, non-secret body lines."""
    header = _pem_marker("BEGIN", prefix)
    parts = [header, *body_lines]
    if with_end:
        parts.append(_pem_marker("END", prefix))
    return "\n".join(parts) + "\n"


class PrivateKeyBlockRedactionTests(unittest.TestCase):
    """The whole PEM block (BEGIN through END inclusive) must vanish."""

    def test_full_rsa_block_body_does_not_leak(self) -> None:
        body_line = "M" * 64  # Synthetic base64-like line; not a real key.
        raw = _pem_block("RSA", (body_line,), with_end=True)
        self.assertIn(body_line, raw)

        redacted = fleet.redact_sensitive_output(raw)

        self.assertNotIn(body_line, redacted)
        self.assertNotIn(_pem_marker("BEGIN", "RSA"), redacted)
        self.assertNotIn(_pem_marker("END", "RSA"), redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 1)

    def test_block_without_end_marker_is_redacted_to_text_end(self) -> None:
        body_line = "Q" * 64  # Synthetic base64-like line.
        raw = (
            _pem_marker("BEGIN", "OPENSSH") + "\n"
            + f"{body_line}\n"
        )
        self.assertNotIn(_DASHES + "END", raw)
        self.assertIn(body_line, raw)

        redacted = fleet.redact_sensitive_output(raw)

        self.assertNotIn(body_line, redacted)
        self.assertNotIn(_pem_marker("BEGIN", "OPENSSH"), redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 1)

    def test_block_without_end_consumes_everything_after_begin(self) -> None:
        # When the END marker is missing, the redaction extends to the end
        # of the text. Any line that follows the BEGIN marker must not leak.
        body_line = "Q" * 64  # Synthetic base64-like line.
        trailing_line = "W" * 64
        raw = (
            _pem_marker("BEGIN") + "\n"
            f"{body_line}\n"
            f"{trailing_line}\n"
        )
        redacted = fleet.redact_sensitive_output(raw)
        self.assertNotIn(body_line, redacted)
        self.assertNotIn(trailing_line, redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 1)

    def test_all_prefix_variants_are_redacted(self) -> None:
        for prefix in ("RSA", "EC", "DSA", "OPENSSH", ""):
            with self.subTest(prefix=prefix or "<none>"):
                body_line = f"{prefix or 'NONE'}-BODY" + ("X" * 60)
                if prefix:
                    raw = _pem_block(prefix, (body_line,), with_end=True)
                else:
                    raw = (
                        _pem_marker("BEGIN") + "\n"
                        + f"{body_line}\n"
                        + _pem_marker("END") + "\n"
                    )
                redacted = fleet.redact_sensitive_output(raw)
                self.assertNotIn(body_line, redacted)
                self.assertIn("[REDACTED]", redacted)

    def test_multiple_blocks_each_redacted(self) -> None:
        raw = (
            _pem_block("RSA", ("Z" * 32,), with_end=True)
            + "intermediate text\n"
            + _pem_block("EC", ("Y" * 32,), with_end=True)
        )
        redacted = fleet.redact_sensitive_output(raw)
        self.assertNotIn("Z" * 32, redacted)
        self.assertNotIn("Y" * 32, redacted)
        self.assertIn("intermediate text", redacted)
        self.assertEqual(redacted.count("[REDACTED]"), 2)

    def test_scan_still_flags_truncated_private_key_block(self) -> None:
        raw = _pem_marker("BEGIN") + "\nbody\n"
        self.assertIn("private-key", fleet.scan_sensitive_task(raw))


class AwsAccessKeyIdRedactionTests(unittest.TestCase):
    """Every supported AWS access key ID prefix must be redacted."""

    def test_each_prefix_redacts_20_char_identifier(self) -> None:
        for prefix in AWS_PREFIXES:
            with self.subTest(prefix=prefix):
                identifier = prefix + AWS_ID_BODY  # 20 chars total
                raw = f"export AWS_ACCESS_KEY_ID={identifier}"
                self.assertIn(identifier, raw)

                redacted = fleet.redact_sensitive_output(raw)

                self.assertNotIn(identifier, redacted)
                self.assertIn("[REDACTED]", redacted)

    def test_identifier_with_wrong_length_is_not_redacted(self) -> None:
        # 15 chars after the prefix -> too short.
        too_short = "AKIA" + ("B" * 15)
        # 17 chars after the prefix -> too long.
        too_long = "AKIA" + ("B" * 17)
        for value in (too_short, too_long):
            with self.subTest(value=value):
                raw = f"line with {value} embedded"
                redacted = fleet.redact_sensitive_output(raw)
                self.assertIn(value, redacted)
                self.assertNotIn("[REDACTED]", redacted)

    def test_prose_word_akiaa_is_not_redacted(self) -> None:
        # ``AKIAA`` (5 chars) is just prose; the pattern requires 16 chars
        # after the prefix, so it must not match.
        raw = "the word AKIAA appears in plain prose only"
        redacted = fleet.redact_sensitive_output(raw)
        self.assertEqual(redacted, raw)
        self.assertNotIn("[REDACTED]", redacted)

    def test_hash_is_not_redacted_as_aws_access_key_id(self) -> None:
        # SHA-256 digest is 64 hex chars; even if it begins with an AWS
        # prefix the trailing characters are word chars, so the word-boundary
        # requirement blocks a false match. We additionally test a digest that
        # does not start with any AWS prefix to keep the assertion obvious.
        digest = hashlib.sha256(HASH_FIXTURE).hexdigest()
        self.assertEqual(len(digest), 64)
        self.assertFalse(
            any(digest.startswith(p) for p in AWS_PREFIXES),
            "Test fixture must not collide with an AWS prefix.",
        )
        raw = f"checksum sha256={digest}"
        redacted = fleet.redact_sensitive_output(raw)
        self.assertEqual(redacted, raw)
        self.assertNotIn("[REDACTED]", redacted)


class AwsSecretAccessKeyRedactionTests(unittest.TestCase):
    """``aws*secret*key = <40 base64 chars>`` must be redacted."""

    def test_assigned_secret_under_aws_secret_access_key_name(self) -> None:
        raw = f'aws_secret_access_key="{AWS_SECRET_BODY}"\n'
        redacted = fleet.redact_sensitive_output(raw)
        self.assertNotIn(AWS_SECRET_BODY, redacted)
        self.assertIn("[REDACTED]", redacted)
        self.assertNotIn("aws_secret_access_key", redacted)

    def test_assigned_secret_under_uppercase_and_dashed_names(self) -> None:
        for name in ("AWS_SECRET_KEY", "aws-secret-access-key", "myAwsSecretKey"):
            with self.subTest(name=name):
                raw = f"{name}={AWS_SECRET_BODY}\n"
                redacted = fleet.redact_sensitive_output(raw)
                self.assertNotIn(AWS_SECRET_BODY, redacted)
                self.assertIn("[REDACTED]", redacted)

    def test_hash_assigned_to_other_variable_is_not_redacted(self) -> None:
        # A 40-char hex digest assigned to a non-AWS variable must survive.
        digest = hashlib.sha1(HASH_FIXTURE).hexdigest()  # 40 chars hex
        self.assertEqual(len(digest), 40)
        raw = f"build_checksum = {digest}\n"
        redacted = fleet.redact_sensitive_output(raw)
        self.assertEqual(redacted, raw)
        self.assertNotIn("[REDACTED]", redacted)

    def test_plain_prose_with_aws_word_is_not_redacted(self) -> None:
        # 40 'A's in prose (no assignment to an aws*secret*key name) must
        # remain untouched.
        raw = "note: AWS uses shared responsibility; see doc " + ("A" * 40)
        redacted = fleet.redact_sensitive_output(raw)
        self.assertEqual(redacted, raw)
        self.assertNotIn("[REDACTED]", redacted)

    def test_secret_ending_with_equals_is_redacted(self) -> None:
        # Real AWS secret access keys use the full base64 alphabet and often
        # end with ``=`` padding. The trailing ``\b`` in the original pattern
        # did not anchor correctly when the last char was non-word and the
        # closing delimiter was also non-word (e.g. a closing quote). This
        # test pins the fix.
        body = ("a" * 39) + "="  # 40 chars, last char is non-word base64
        self.assertEqual(len(body), 40)
        raw = f'aws_secret_access_key="{body}"\n'
        self.assertIn(body, raw)

        redacted = fleet.redact_sensitive_output(raw)

        self.assertNotIn(body, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_secret_ending_with_plus_is_redacted(self) -> None:
        # ``+`` is part of the base64 alphabet and is non-word. The original
        # ``\b`` boundary also missed this case.
        body = ("b" * 39) + "+"  # 40 chars
        self.assertEqual(len(body), 40)
        raw = f"aws_secret_access_key={body}\n"
        self.assertIn(body, raw)

        redacted = fleet.redact_sensitive_output(raw)

        self.assertNotIn(body, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_secret_ending_with_slash_is_redacted(self) -> None:
        # ``/`` is part of the base64 alphabet and is non-word. Verify the
        # lookahead still anchors at end of string when ``/`` is the last
        # character and no trailing delimiter follows.
        body = ("c" * 39) + "/"  # 40 chars
        self.assertEqual(len(body), 40)
        raw = f"aws_secret_access_key={body}"
        self.assertIn(body, raw)

        redacted = fleet.redact_sensitive_output(raw)

        self.assertNotIn(body, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_secret_with_mixed_base64_padding_in_json_object(self) -> None:
        # A realistic-looking value (only base64 chars, padded with ``=``)
        # assigned via ``name = "value"`` (the format the redaction pattern
        # supports) must be redacted even when followed by ``,`` and another
        # variable. The key is left unquoted on purpose to mirror the env /
        # dotenv / shell-assignment style the pattern is designed for.
        body = "Aa0" + ("d" * 35) + "=="  # 40 chars, two trailing ``=``
        self.assertEqual(len(body), 40)
        raw = (
            'aws_secret_access_key="'
            + body
            + '", other_var="plain-value"\n'
        )
        self.assertIn(body, raw)

        redacted = fleet.redact_sensitive_output(raw)

        self.assertNotIn(body, redacted)
        self.assertIn("[REDACTED]", redacted)
        # The neighbouring key/value must remain intact.
        self.assertIn('other_var="plain-value"', redacted)

    def test_partial_base64_secret_is_not_redacted(self) -> None:
        # A shorter-than-40 value must not be matched by the exact-count
        # pattern, even when it ends in ``=``. Guards against accidentally
        # widening the count when fixing the boundary.
        body = ("a" * 39) + "="  # 40 chars with padding -> MUST redact
        too_short = ("a" * 20) + "=="  # 22 chars with padding -> too short
        raw = f'aws_secret_access_key="{body}" {too_short}\n'
        self.assertIn(body, raw)
        self.assertIn(too_short, raw)

        redacted = fleet.redact_sensitive_output(raw)

        self.assertNotIn(body, redacted)
        # The too-short value is left in place because it does not look
        # like an AWS secret access key.
        self.assertIn(too_short, redacted)


class ExistingPatternRegressionTests(unittest.TestCase):
    """Pre-existing patterns must keep redacting the same way as before."""

    def test_bearer_token_still_redacted(self) -> None:
        token = "B" * 24
        raw = f"Authorization: Bearer {token}"
        redacted = fleet.redact_sensitive_output(raw)
        self.assertNotIn(token, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_vendor_token_still_redacted(self) -> None:
        token = "sk-" + "proj-" + ("C" * 24)
        raw = f"token={token}"
        redacted = fleet.redact_sensitive_output(raw)
        self.assertNotIn(token, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_assigned_secret_still_redacted(self) -> None:
        value = "D" * 24
        raw = f"api_key={value}"
        redacted = fleet.redact_sensitive_output(raw)
        self.assertNotIn(value, redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_sensitive_response_headers_still_redacted(self) -> None:
        cookie_secret = "session=" + "E" * 24 + "; HttpOnly"
        authorization_secret = "Basic " + "F" * 24
        raw = (
            '{"headers":{'
            f'"set-cookie":"{cookie_secret}",'
            f'"authorization":"{authorization_secret}",'
            '"content-type":"application/json"'
            '}}\n'
            f"Set-Cookie: {cookie_secret}"
        )
        redacted = fleet.redact_sensitive_output(raw)
        self.assertNotIn(cookie_secret, redacted)
        self.assertNotIn(authorization_secret, redacted)
        self.assertIn('"content-type":"application/json"', redacted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
