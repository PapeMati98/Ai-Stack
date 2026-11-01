#!/usr/bin/env python3
"""Tests for summarize_evidence helper (systematic-debugging 1.0.0).

The aggregator under test is read-only, deterministic, and uses synthetic
fixtures only. Tests retain every artifact under --artifact-root (no cleanup)
and never touch network or external services. Subprocess is used only in CLI
tests; the production aggregator itself does not import subprocess.
"""

from __future__ import annotations
def _redacted_fixture(kind, ordinal, binary=False):
    """Return a deterministic public synthetic fixture value.

    The result is newly authored public data derived only from the pinned
    rule label and a per-file ordinal. It is never derived from any original
    value. Runtime values retain the original detector category.
    """
    alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
    seed = (len(kind) * 131 + int(ordinal) * 7919 + 17) & 0x7FFFFFFF
    chars = []
    for _ in range(40):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        chars.append(alphabet[seed % 36])
    body = ''.join(chars)
    if kind == 'vendor-token':
        body = 'gh' + 'p_' + body
    elif kind == 'aws-access-key-id':
        body = 'AK' + 'IA' + body[:16].upper()
    elif kind == 'private-key':
        marker = 'PRIVATE' + ' KEY'
        body = ('-----' + 'BEGIN ' + marker + '-----' + chr(10) + body + chr(10)
                + '-----' + 'END ' + marker + '-----')
    if binary:
        return body.encode('ascii')
    return body



import argparse
import io
import json
import os
import subprocess
import sys
import unittest
import uuid
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

# Ensure bytecode is never written
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent
HELPER_SCRIPT = SCRIPTS_DIR / "summarize_evidence.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import summarize_evidence as se  # noqa: E402

DEFAULT_ARTIFACT_ROOT = SCRIPTS_DIR / '.test-artifacts' / 'evidence-summary'
ARTIFACT_ROOT = DEFAULT_ARTIFACT_ROOT


def set_artifact_root(root: Path) -> None:
    """Update the retained-artifact root for synthetic fixtures."""
    global ARTIFACT_ROOT
    ARTIFACT_ROOT = root.resolve()
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)


def get_fixture_dir(name: str) -> Path:
    """Return a deterministic sub-directory under ARTIFACT_ROOT (no cleanup)."""
    target = ARTIFACT_ROOT / "unit_tests" / name
    target.mkdir(parents=True, exist_ok=True)
    return target


# Parse --artifact-root early; sanitize sys.argv so unittest discovery never rejects it.
_explicit_artifact_root: Path | None = None
_cleaned_argv = [sys.argv[0]] if sys.argv else ["test_summarize_evidence.py"]
_skip_next = False
for _i in range(1, len(sys.argv)):
    if _skip_next:
        _skip_next = False
        continue
    _arg = sys.argv[_i]
    if _arg == "--artifact-root":
        if _i + 1 < len(sys.argv):
            _explicit_artifact_root = Path(sys.argv[_i + 1])
            set_artifact_root(_explicit_artifact_root)
            _skip_next = True
        continue
    if _arg.startswith("--artifact-root="):
        _explicit_artifact_root = Path(_arg.split("=", 1)[1])
        set_artifact_root(_explicit_artifact_root)
        continue
    _cleaned_argv.append(_arg)
sys.argv = _cleaned_argv


def _write_fixture(name: str, content: str) -> Path:
    """Write a synthetic fixture file under ARTIFACT_ROOT and return its path."""
    fixture_dir = get_fixture_dir("cli_fixtures")
    path = fixture_dir / name
    path.write_text(content, encoding="utf-8")
    return path


def _build_args(**overrides: object) -> argparse.Namespace:
    """Build a populated argparse.Namespace for run()."""
    fields: dict = {
        "input": "",
        "format": "text",
        "command_exit_code": 0,
        "exit_policy": "command",
        "input_truncated": False,
        "top": 20,
        "max_examples": 3,
        "output": None,
        "force": False,
    }
    fields.update(overrides)
    return argparse.Namespace(**fields)


def _capture_main(argv: list[str]) -> tuple[int, str, str]:
    """Invoke se.main(argv) with isolated stdout/stderr; return (rc, stdout, stderr)."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        rc = se.main(argv)
    return rc, stdout.getvalue(), stderr.getvalue()


def _run_cli(args: list[str]) -> tuple[int, str, str]:
    """Invoke the helper as a subprocess; return (rc, stdout, stderr)."""
    cmd = [
        sys.executable,
        "-B",
        str(HELPER_SCRIPT),
    ] + list(args)
    creationflags = 0
    startupinfo = None
    if sys.platform == "win32":
        creationflags = 0x08000000
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        creationflags=creationflags,
        startupinfo=startupinfo,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Input validation & redaction primitives
# ---------------------------------------------------------------------------


class TestInputValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("input_validation")

    def tearDown(self) -> None:
        # Retention policy: keep synthetic fixtures under ARTIFACT_ROOT
        pass

    def test_forbidden_basename_rejected(self) -> None:
        for name in (".env", "secrets.json", "credentials.yaml", "id_rsa"):
            self.assertTrue(se.is_forbidden_input_name(name), name)

    def test_safe_basename_accepted(self) -> None:
        for name in ("audit.log", "captured.txt", "results.jsonl"):
            self.assertFalse(se.is_forbidden_input_name(name), name)

    def test_resolve_input_path_rejects_newlines(self) -> None:
        self.assertIsNone(se.resolve_input_path("multi\nline text"))
        self.assertIsNone(se.resolve_input_path("multi\rline text"))

    def test_resolve_input_path_returns_none_for_missing(self) -> None:
        # Path that does not exist resolves to None; load_input_text then
        # rejects it because --input must be an existing captured file.
        self.assertIsNone(se.resolve_input_path("definitely-missing-1234.txt"))

    def test_load_input_text_inline_rejected(self) -> None:
        # Inline text is not accepted; --input must be an existing captured
        # file.  Missing paths and inline text are both rejected so the helper
        # never silently treats a non-file --input value as inline evidence.
        sample = "inline line one\ninline line two\n"
        with self.assertRaises(se.SummarizeError) as cm:
            se.load_input_text(sample)
        self.assertIn("does not resolve", str(cm.exception))

    def test_missing_single_line_input_rejected(self) -> None:
        # A single-line --input that does not resolve to an existing file must
        # fail; the helper never silently treats it as inline evidence.
        with self.assertRaises(se.SummarizeError) as cm:
            se.load_input_text("definitely-not-a-real-path-1234.txt")
        self.assertIn("does not resolve", str(cm.exception))

    def test_load_input_text_from_file(self) -> None:
        path = self.fixture_dir / "captured.txt"
        path.write_text("hello", encoding="utf-8")
        text, meta = se.load_input_text(str(path))
        self.assertEqual(text, "hello")
        self.assertEqual(meta["source"], "captured_file")
        self.assertEqual(meta["path"], str(path.resolve()))

    def test_validate_input_path_rejects_symlink(self) -> None:
        # Only run on platforms supporting symlinks, and create a synthetic link
        if not hasattr(os, "symlink"):
            self.skipTest("symlink not supported")
        target = self.fixture_dir / "symlink_target.txt"
        target.write_text("ok", encoding="utf-8")
        link = self.fixture_dir / "linked_secret.json"
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError):
            self.skipTest("symlink not creatable on this fs")
        with self.assertRaises(se.SummarizeError):
            se.validate_input_path(link)

    def test_validate_input_path_rejects_forbidden_name(self) -> None:
        path = self.fixture_dir / "secrets.json"
        path.write_text("placeholder", encoding="utf-8")
        with self.assertRaises(se.SummarizeError) as cm:
            se.validate_input_path(path)
        self.assertIn("secret/credential name", str(cm.exception))

    def test_sanitize_url_credentials(self) -> None:
        text = "endpoint=https://user:hunter2@host.example.com/path"
        out, count = se.redact_sensitive(text)
        self.assertIn("[REDACTED:url-userinfo]", out)
        self.assertNotIn("hunter2", out)
        self.assertGreaterEqual(count, 1)

    def test_sanitize_bearer_token(self) -> None:
        text = ("Authorization: Bearer " + _redacted_fixture('bearer-token', 1))
        out, count = se.redact_sensitive(text)
        self.assertIn("[REDACTED", out)
        self.assertNotIn((_redacted_fixture('bearer-token', 1)), out)
        self.assertGreaterEqual(count, 1)

    def test_sanitize_bearer_outside_header(self) -> None:
        # Bare Bearer token (no Authorization key) keeps the specific marker.
        text = ("got Bearer " + _redacted_fixture('bearer-token', 1) + " back")
        out, count = se.redact_sensitive(text)
        self.assertIn("[REDACTED:bearer]", out)
        self.assertNotIn((_redacted_fixture('bearer-token', 1)), out)
        self.assertGreaterEqual(count, 1)

    def test_sanitize_jwt(self) -> None:
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        out, count = se.redact_sensitive(jwt)
        self.assertIn("[REDACTED:jwt]", out)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", out)
        self.assertGreaterEqual(count, 1)

    def test_sanitize_sensitive_keyvalue(self) -> None:
        text = ("password=hunter2 api_key=" + _redacted_fixture('assigned-secret', 2))
        out, count = se.redact_sensitive(text)
        self.assertNotIn("hunter2", out)
        self.assertNotIn((_redacted_fixture('assigned-secret', 2)), out)
        self.assertIn("[REDACTED]", out)
        self.assertGreaterEqual(count, 2)

    def test_unquoted_basic_auth_header_fully_redacted(self) -> None:
        # Unquoted multi-token value must not be cut at the first space:
        # "Basic dXNlcjpwYXNz" is entirely credential material.
        text = "Authorization: Basic dXNlcjpwYXNz"
        out, count = se.redact_sensitive(text)
        self.assertNotIn("dXNlcjpwYXNz", out)
        self.assertIn("[REDACTED]", out)
        self.assertGreaterEqual(count, 1)

    def test_cookie_header_pairs_fully_redacted(self) -> None:
        # Semicolon-separated cookie pairs must not leak past the first pair.
        text = "Cookie: theme=dark; session=supersecret"
        out, count = se.redact_sensitive(text)
        self.assertNotIn("supersecret", out)
        self.assertNotIn("session=supersecret", out)
        self.assertNotIn("theme=dark", out)
        self.assertIn("[REDACTED]", out)
        self.assertGreaterEqual(count, 1)

    def test_set_cookie_header_fully_redacted(self) -> None:
        text = "Set-Cookie: session=supersecret; Path=/; HttpOnly"
        out, count = se.redact_sensitive(text)
        self.assertNotIn("supersecret", out)
        self.assertIn("[REDACTED]", out)
        self.assertGreaterEqual(count, 1)

    def test_cookie_header_with_jwt_prefix_redacts_tail_pairs(self) -> None:
        # JWT substitution runs first and inserts [REDACTED:jwt]. The header
        # pass must still consume the rest of the Cookie value through EOL;
        # stopping at the first [REDACTED marker leaks session=supersecret.
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        text = f"Cookie: {jwt}; session=supersecret"
        out, count = se.redact_sensitive(text)
        self.assertNotIn("supersecret", out)
        self.assertNotIn("session=supersecret", out)
        self.assertNotIn(jwt, out)
        self.assertIn("[REDACTED]", out)
        self.assertGreaterEqual(count, 1)

    def test_authorization_header_with_bearer_prefix_redacts_basic_tail(self) -> None:
        # Bearer substitution runs first and inserts [REDACTED:bearer]. The
        # header pass must still consume the remaining Basic credential on the
        # same Authorization line.
        text = ("Authorization: Bearer " + _redacted_fixture('bearer-token', 1) + " Basic dXNlcjpwYXNz")
        out, count = se.redact_sensitive(text)
        self.assertNotIn("dXNlcjpwYXNz", out)
        self.assertNotIn("Basic", out)
        self.assertNotIn((_redacted_fixture('bearer-token', 1)), out)
        self.assertIn("[REDACTED]", out)
        self.assertGreaterEqual(count, 1)

    def test_cookie_header_with_url_userinfo_prefix_redacts_tail_pairs(self) -> None:
        # URL userinfo substitution inserts [REDACTED:url-userinfo]; the Cookie
        # header pass must still consume session=supersecret through EOL.
        text = (
            "Cookie: https://user:hunter2@host.example.com/x; session=supersecret"
        )
        out, count = se.redact_sensitive(text)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("supersecret", out)
        self.assertNotIn("session=supersecret", out)
        self.assertIn("[REDACTED]", out)
        self.assertGreaterEqual(count, 1)

    def test_header_value_redacted_inside_jsonl_message_signature(self) -> None:
        # derive_jsonl_signature lowercases message text into
        # "message:authorization: basic ..."; the secret must not survive
        # in the signature just because the key is wrapped by "message:".
        secret = "dXNlcjpwYXNz"
        obj = {"msg": "Authorization: Basic " + secret}
        body = se.summarize_jsonl(
            json.dumps(obj) + "\n",
            top=10,
            max_examples=3,
            input_truncated=False,
            redact_counter=[0],
        )
        serialized = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(secret.lower(), serialized)
        self.assertNotIn("basic", serialized.lower().replace("[redacted]", ""))

    def test_redaction_count_is_exact(self) -> None:
        # Count must reflect real replacements only (no double-count from
        # both the replacer callback and the subn return value).
        out, count = se.redact_sensitive(("password=hunter2 api_key=" + _redacted_fixture('assigned-secret', 2)))
        self.assertNotIn("hunter2", out)
        self.assertNotIn((_redacted_fixture('assigned-secret', 2)), out)
        self.assertEqual(count, 2)

    def test_404_500_remain_distinct(self) -> None:
        # No blanket numeric replacement; 404 and 500 produce distinct sigs
        s_404 = se.canonicalize_line_for_signature("404 Not Found: /api/users/42")
        s_500 = se.canonicalize_line_for_signature("500 Internal Server Error: db timeout")
        self.assertNotEqual(s_404, s_500)
        self.assertIn("404", s_404)
        self.assertIn("500", s_500)


# ---------------------------------------------------------------------------
# Text-mode aggregation
# ---------------------------------------------------------------------------


class TestTextAggregation(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("text_aggregation")

    def tearDown(self) -> None:
        pass

    def test_full_counts_vs_bounded_display(self) -> None:
        # 100 lines: 50 of "x" and 50 of "y" alternating
        lines = ["x" if i % 2 == 0 else "y" for i in range(100)]
        body = se.summarize_text(
            "\n".join(lines) + "\n",
            top=1,
            max_examples=2,
            input_truncated=False,
            redact_counter=[0],
        )
        # Terminal newline is a delimiter, not an extra phantom record.
        self.assertEqual(body["coverage"]["total_records"], 100)
        self.assertEqual(body["coverage"]["parsed_records"], 100)
        self.assertEqual(body["coverage"]["skipped_records"], 0)
        self.assertEqual(body["coverage"]["invalid_records"], 0)
        self.assertEqual(len(body["groups"]), 1)
        self.assertEqual(body["diagnostics"]["total_groups"], 2)
        self.assertEqual(body["diagnostics"]["omitted_groups"], 1)
        for group in body["groups"]:
            self.assertLessEqual(len(group["examples"]), 2)

    def test_repeated_signatures_collapse(self) -> None:
        text = "\n".join(["ERROR: kaboom"] * 7 + ["INFO: ok"] * 3) + "\n"
        body = se.summarize_text(
            text, top=20, max_examples=3, input_truncated=False, redact_counter=[0]
        )
        sigs = [g["signature"] for g in body["groups"]]
        self.assertEqual(sigs, ["error: kaboom", "info: ok"])
        counts = {g["signature"]: g["count"] for g in body["groups"]}
        self.assertEqual(counts["error: kaboom"], 7)
        self.assertEqual(counts["info: ok"], 3)

    def test_different_error_codes_preserved(self) -> None:
        # Use the same non-error-code text so the canonicalized signature
        # differs only by the numeric code. The aggregator must NOT merge
        # 404 with 500 via a blanket numeric replacement.
        text = "\n".join(
            [
                "ERROR 404 at endpoint",
                "ERROR 500 at endpoint",
                "ERROR 404 at endpoint",
                "ERROR 500 at endpoint",
            ]
        )
        body = se.summarize_text(
            text, top=20, max_examples=5, input_truncated=False, redact_counter=[0]
        )
        self.assertEqual(len(body["groups"]), 2)
        sigs = sorted(g["signature"] for g in body["groups"])
        self.assertIn("error 404 at endpoint", sigs)
        self.assertIn("error 500 at endpoint", sigs)
        # Both numeric codes remain visible inside their respective signatures
        self.assertTrue(all("404" in s or "500" in s for s in sigs))

    def test_ansi_stripping(self) -> None:
        text = "\x1b[31mERROR 500\x1b[0m at /a\n\x1b[32mERROR 500\x1b[0m at /b\n"
        body = se.summarize_text(
            text, top=20, max_examples=3, input_truncated=False, redact_counter=[0]
        )
        for group in body["groups"]:
            self.assertNotIn("\x1b[", group["signature"])
            for example in group["examples"]:
                self.assertNotIn("\x1b[", example["text"])

    def test_crlf_progress_handling(self) -> None:
        # Lone CR (progress redraw) should not split or duplicate signatures
        text = "PROGRESS 50%\rPROGRESS 50%\rPROGRESS 50%\n"
        body = se.summarize_text(
            text, top=20, max_examples=3, input_truncated=False, redact_counter=[0]
        )
        # Three CR-redrawn lines share one signature; lone CRs don't fragment
        self.assertEqual(body["coverage"]["parsed_records"], 3)
        self.assertEqual(body["diagnostics"]["total_groups"], 1)
        self.assertEqual(body["groups"][0]["count"], 3)
        self.assertEqual(body["groups"][0]["signature"], "progress 50%")

    def test_unicode_preserved(self) -> None:
        text = "Błąd: brak połączenia łącze się ponownie\nBłąd: brak połączenia retry\n"
        body = se.summarize_text(
            text, top=20, max_examples=3, input_truncated=False, redact_counter=[0]
        )
        # Two signatures because content differs (łącze się vs retry)
        self.assertEqual(len(body["groups"]), 2)
        for group in body["groups"]:
            for example in group["examples"]:
                # UTF-8 round-trip via canonicalize preserves Polish diacritics
                self.assertTrue(any(ch in example["text"] for ch in "łączeBłąd"))

    def test_truncated_input_flag(self) -> None:
        body = se.summarize_text(
            "line\n", top=10, max_examples=2, input_truncated=True, redact_counter=[0]
        )
        self.assertTrue(body["diagnostics"]["input_truncated"])
        self.assertFalse(body["coverage"]["input_complete"])

    def test_top_groups_bounded(self) -> None:
        text = "\n".join(f"msg-{i}" for i in range(50)) + "\n"
        body = se.summarize_text(
            text, top=5, max_examples=3, input_truncated=False, redact_counter=[0]
        )
        self.assertEqual(len(body["groups"]), 5)
        self.assertEqual(body["diagnostics"]["omitted_groups"], 45)

    def test_max_examples_bounded(self) -> None:
        text = "\n".join(["dup"] * 20) + "\n"
        body = se.summarize_text(
            text, top=5, max_examples=4, input_truncated=False, redact_counter=[0]
        )
        self.assertEqual(body["groups"][0]["count"], 20)
        self.assertEqual(len(body["groups"][0]["examples"]), 4)
        for example in body["groups"][0]["examples"]:
            self.assertIn("line", example)
            self.assertIn("text", example)


# ---------------------------------------------------------------------------
# JSONL-mode aggregation
# ---------------------------------------------------------------------------


class TestJsonlAggregation(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("jsonl_aggregation")

    def tearDown(self) -> None:
        pass

    def test_jsonl_full_counts(self) -> None:
        # 5 non-empty lines: 3 valid dicts + 1 invalid + 1 list (non-object)
        lines = [
            json.dumps({"status": 200, "msg": "ok"}),
            json.dumps({"status": 200, "msg": "ok"}),
            json.dumps({"status": 500, "msg": "boom"}),
            "{ this is not valid",
            json.dumps(["non-object"]),
        ]
        text = "\n".join(lines)
        body = se.summarize_jsonl(
            text,
            top=10,
            max_examples=3,
            input_truncated=False,
            redact_counter=[0],
        )
        self.assertEqual(body["coverage"]["total_records"], 5)
        self.assertEqual(body["coverage"]["parsed_records"], 4)
        self.assertEqual(body["coverage"]["invalid_records"], 1)
        self.assertEqual(body["coverage"]["skipped_records"], 0)

    def test_jsonl_invalid_records_have_no_raw_parse_data(self) -> None:
        raw_bad_line = "{ malformed password=hunter2"
        text = raw_bad_line + "\n"
        body = se.summarize_jsonl(
            text, top=10, max_examples=3, input_truncated=False, redact_counter=[0]
        )
        self.assertEqual(body["coverage"]["invalid_records"], 1)
        # Raw parse-error data must not appear anywhere in output
        serialized = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(raw_bad_line, serialized)
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("password", serialized)
        self.assertNotIn("Expecting", serialized)  # python's json error prefix
        self.assertNotIn("Unterminated", serialized)  # json error variants
        self.assertNotIn("delimiter", serialized)  # json error variants

    def test_jsonl_signature_field_priority(self) -> None:
        obj = {"status": 200, "level": "INFO", "msg": "should be ignored for sig"}
        sig, source = se.derive_jsonl_signature(obj)
        self.assertTrue(sig.startswith("status:"))
        self.assertEqual(source, "jsonl.field.status")

    def test_jsonl_message_field_signature(self) -> None:
        # No status or level field; signature falls back to message
        obj = {"other": "x", "msg": "kaboom at /x"}
        sig, source = se.derive_jsonl_signature(obj)
        self.assertTrue(sig.startswith("message:"))
        self.assertEqual(source, "jsonl.field.msg")

    def test_jsonl_fallback_hash(self) -> None:
        obj = {"some": "field", "another": "value"}
        sig, source = se.derive_jsonl_signature(obj)
        self.assertTrue(sig.startswith("json:"))
        self.assertEqual(source, "jsonl.hash")

    def test_jsonl_example_redaction(self) -> None:
        obj = {"status": 200, "Authorization": "Bearer xyz123abc"}
        body = se.summarize_jsonl(
            json.dumps(obj) + "\n",
            top=10,
            max_examples=3,
            input_truncated=False,
            redact_counter=[0],
        )
        serialized = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("xyz123abc", serialized)
        self.assertIn("[REDACTED", serialized)


# ---------------------------------------------------------------------------
# Redaction in every output/exception path
# ---------------------------------------------------------------------------


class TestRedaction(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("redaction")

    def tearDown(self) -> None:
        pass

    def test_redaction_applied_to_stdout(self) -> None:
        input_path = _write_fixture(
            "redact_stdout.txt", "Authorization: Bearer abc.xyz.qwe\n"
        )
        args = _build_args(
            input=str(input_path),
            format="text",
            command_exit_code=0,
            exit_policy="command",
        )
        summary = se.run(args)
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("abc.xyz.qwe", serialized)

    def test_redaction_applied_to_output_file(self) -> None:
        # Unique retained output name per run: default no-overwrite stays
        # intact and prior artifacts are never clobbered or deleted.
        out = self.fixture_dir / f"out_{uuid.uuid4().hex}.json"
        secret = ("Bearer " + _redacted_fixture('bearer-token', 3))
        input_path = _write_fixture(
            "redact_output.txt", "Authorization: " + secret + "\n"
        )
        summary = se.run(
            _build_args(
                input=str(input_path),
                format="text",
                command_exit_code=0,
                exit_policy="command",
                output=out,
            )
        )
        written = out.read_text(encoding="utf-8")
        self.assertNotIn(secret, written)
        self.assertNotIn((_redacted_fixture('bearer-token', 3)), written)
        # The summary still reports the output file path
        self.assertEqual(summary["output"]["path"], str(out.resolve()))

    def test_header_secret_absent_from_text_summary(self) -> None:
        # End-to-end: unquoted header credentials must not reach groups,
        # signatures, examples, or the serialized summary.
        secret = "dXNlcjpwYXNz"
        input_path = _write_fixture(
            "header_basic.txt",
            "Authorization: Basic " + secret + "\n"
            "Cookie: theme=dark; session=supersecret\n",
        )
        args = _build_args(
            input=str(input_path),
            format="text",
            command_exit_code=0,
            exit_policy="command",
        )
        summary = se.run(args)
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("supersecret", serialized)

    def test_header_tail_secret_absent_from_text_summary_after_marker(self) -> None:
        # Regression: JWT/Bearer/URL substitution inserts a [REDACTED marker
        # before the header pass. Tail credentials on the same header line
        # must not appear in the serialized text summary.
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        input_path = _write_fixture(
            "header_tail_text.txt",
            f"Cookie: {jwt}; session=supersecret\n"
            "Authorization: Bearer tok1234567890 Basic dXNlcjpwYXNz\n",
        )
        args = _build_args(
            input=str(input_path),
            format="text",
            command_exit_code=0,
            exit_policy="command",
        )
        summary = se.run(args)
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("supersecret", serialized)
        self.assertNotIn("session=supersecret", serialized)
        self.assertNotIn("dXNlcjpwYXNz", serialized)
        self.assertNotIn("tok1234567890", serialized)
        self.assertNotIn(jwt, serialized)

    def test_header_tail_secret_absent_from_jsonl_summary_after_marker(self) -> None:
        # Same regression for JSONL: message/field content that becomes a
        # signature or example must not carry tail secrets past a marker.
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        obj = {
            "msg": f"Cookie: {jwt}; session=supersecret",
            "detail": "Authorization: Bearer tok1234567890 Basic dXNlcjpwYXNz",
        }
        input_path = _write_fixture(
            "header_tail.jsonl", json.dumps(obj) + "\n"
        )
        args = _build_args(
            input=str(input_path),
            format="jsonl",
            command_exit_code=0,
            exit_policy="command",
        )
        summary = se.run(args)
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("supersecret", serialized)
        self.assertNotIn("session=supersecret", serialized)
        self.assertNotIn("dXNlcjpwYXNz", serialized)
        self.assertNotIn("tok1234567890", serialized)
        self.assertNotIn(jwt, serialized)

    def test_header_secret_absent_from_jsonl_summary(self) -> None:
        secret = "dXNlcjpwYXNz"
        obj = {"Authorization": "Basic " + secret, "msg": "ok"}
        input_path = _write_fixture(
            "header_basic.jsonl", json.dumps(obj) + "\n"
        )
        args = _build_args(
            input=str(input_path),
            format="jsonl",
            command_exit_code=0,
            exit_policy="command",
        )
        summary = se.run(args)
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(secret, serialized)

    def test_exception_message_redacted(self) -> None:
        secret_path = self.fixture_dir / "secrets.json"
        secret_path.write_text("placeholder-not-real", encoding="utf-8")
        rc, stdout, stderr = _capture_main(
            [
                "--input",
                str(secret_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
            ]
        )
        self.assertEqual(rc, se.EXIT_INVALID_INPUT)
        # Stderr contains brief error; raw input content is not echoed
        self.assertNotIn("placeholder-not-real", stderr)
        # Stdout is empty on invalid input
        self.assertEqual(stdout, "")


# ---------------------------------------------------------------------------
# exit-policy semantics
# ---------------------------------------------------------------------------


class TestCommandStatus(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("command_status")

    def tearDown(self) -> None:
        pass

    def test_command_policy_success(self) -> None:
        input_path = _write_fixture("cmd_success.txt", "line\n")
        summary = se.run(
            _build_args(
                input=str(input_path),
                command_exit_code=0,
                exit_policy="command",
            )
        )
        self.assertEqual(summary["command"]["command_status"], "SUCCESS")
        self.assertEqual(summary["command"]["status_kind"], "command.success")
        # Helper status is documented independently
        self.assertEqual(summary["helper"]["exit_status"], "SUCCEEDED")

    def test_command_policy_failed(self) -> None:
        input_path = _write_fixture("cmd_failed.txt", "line\n")
        summary = se.run(
            _build_args(
                input=str(input_path),
                command_exit_code=2,
                exit_policy="command",
            )
        )
        self.assertEqual(summary["command"]["command_status"], "FAILED")
        self.assertEqual(summary["helper"]["exit_status"], "SUCCEEDED")

    def test_rg_policy_no_match(self) -> None:
        input_path = _write_fixture("rg_no_match.txt", "line\n")
        summary = se.run(
            _build_args(
                input=str(input_path),
                command_exit_code=1,
                exit_policy="rg",
            )
        )
        self.assertEqual(summary["command"]["command_status"], "NO_MATCH")

    def test_rg_policy_matched(self) -> None:
        input_path = _write_fixture("rg_matched.txt", "line\n")
        summary = se.run(
            _build_args(
                input=str(input_path),
                command_exit_code=0,
                exit_policy="rg",
            )
        )
        self.assertEqual(summary["command"]["command_status"], "MATCHED")

    def test_diff_policy_different(self) -> None:
        input_path = _write_fixture("diff_different.txt", "line\n")
        summary = se.run(
            _build_args(
                input=str(input_path),
                command_exit_code=1,
                exit_policy="diff",
            )
        )
        self.assertEqual(summary["command"]["command_status"], "DIFFERENT")

    def test_diff_policy_same(self) -> None:
        input_path = _write_fixture("diff_same.txt", "line\n")
        summary = se.run(
            _build_args(
                input=str(input_path),
                command_exit_code=0,
                exit_policy="diff",
            )
        )
        self.assertEqual(summary["command"]["command_status"], "SAME")


# ---------------------------------------------------------------------------
# Output/input protection
# ---------------------------------------------------------------------------


class TestOutputInputProtection(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("output_input_protection")

    def tearDown(self) -> None:
        pass

    def test_output_file_refuses_overwrite_by_default(self) -> None:
        out = self.fixture_dir / "existing.json"
        out.write_text('{"stale": true}\n', encoding="utf-8")
        input_path = _write_fixture("ovr_input.txt", "hello\n")
        rc, _stdout, stderr = _capture_main(
            [
                "--input",
                str(input_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
                "--output",
                str(out),
            ]
        )
        self.assertEqual(rc, se.EXIT_INVALID_INPUT)
        self.assertIn("Refusing to overwrite", stderr)
        # Existing file is untouched
        self.assertEqual(out.read_text(encoding="utf-8"), '{"stale": true}\n')

    def test_output_file_overwrites_with_force(self) -> None:
        out = self.fixture_dir / "existing_force.json"
        out.write_text('{"stale": true}\n', encoding="utf-8")
        input_path = _write_fixture("ovr_force_input.txt", "hello\n")
        rc, stdout, _stderr = _capture_main(
            [
                "--input",
                str(input_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
                "--output",
                str(out),
                "--force",
            ]
        )
        self.assertEqual(rc, se.EXIT_SUCCEEDED)
        # Stdout has the summary
        payload = json.loads(stdout)
        self.assertEqual(payload["helper"]["exit_status"], "SUCCEEDED")
        # Existing file is replaced by valid summary
        self.assertNotIn("stale", out.read_text(encoding="utf-8"))

    def test_output_alias_to_input_rejected(self) -> None:
        # Same path used for both --input and --output must be refused
        src = self.fixture_dir / "alias_input.txt"
        src.write_text("hello\n", encoding="utf-8")
        rc, _stdout, stderr = _capture_main(
            [
                "--input",
                str(src),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
                "--output",
                str(src),
                "--force",
            ]
        )
        self.assertEqual(rc, se.EXIT_INVALID_INPUT)
        self.assertIn("aliases the input file", stderr)

    def test_symlink_input_rejected(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink not supported")
        target = self.fixture_dir / "sym_target.txt"
        target.write_text("ok", encoding="utf-8")
        link = self.fixture_dir / "linked_capture.txt"
        try:
            os.symlink(str(target), str(link))
        except (OSError, NotImplementedError):
            self.skipTest("symlink not creatable on this fs")
        rc, _stdout, stderr = _capture_main(
            [
                "--input",
                str(link),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
            ]
        )
        self.assertEqual(rc, se.EXIT_INVALID_INPUT)
        self.assertIn("symlink", stderr)

    def test_forbidden_input_name_rejected(self) -> None:
        path = self.fixture_dir / "secrets.json"
        path.write_text("placeholder", encoding="utf-8")
        rc, _stdout, stderr = _capture_main(
            [
                "--input",
                str(path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
            ]
        )
        self.assertEqual(rc, se.EXIT_INVALID_INPUT)
        self.assertIn("secret/credential name", stderr)

    def test_invalid_command_exit_code(self) -> None:
        # argparse rejects non-integer values before our validator sees them,
        # so the only way to fail validation here is a CLI-level type error.
        input_path = _write_fixture("bad_exit.txt", "x\n")
        rc, _stdout, stderr = _capture_main(
            [
                "--input",
                str(input_path),
                "--format",
                "text",
                "--command-exit-code",
                "not-a-number",
                "--exit-policy",
                "command",
            ]
        )
        self.assertEqual(rc, se.EXIT_INVALID_INPUT)
        self.assertIn("--command-exit-code", stderr)

    def test_windows_ntstatus_exit_code_preserved(self) -> None:
        # STATUS_ACCESS_VIOLATION (-1073741819) is a real Windows crash exit
        # code that must round-trip through the helper and become FAILED
        # under the command policy. A naive -255..255 range restriction would
        # silently drop this evidence.
        input_path = _write_fixture("ntstatus.txt", "x\n")
        rc, stdout, _stderr = _run_cli(
            [
                "--input",
                str(input_path),
                "--format",
                "text",
                "--command-exit-code",
                "-1073741819",
                "--exit-policy",
                "command",
            ]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["command"]["exit_code"], -1073741819)
        self.assertEqual(payload["command"]["command_status"], "FAILED")
        self.assertEqual(payload["command"]["status_kind"], "command.failure")
        # Helper status documents only the helper itself, not the captured run
        self.assertEqual(payload["helper"]["exit_status"], "SUCCEEDED")

    def test_invalid_top(self) -> None:
        input_path = _write_fixture("bad_top.txt", "x\n")
        rc, _stdout, stderr = _capture_main(
            [
                "--input",
                str(input_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
                "--top",
                "0",
            ]
        )
        self.assertEqual(rc, se.EXIT_INVALID_INPUT)
        self.assertIn("--top", stderr)


# ---------------------------------------------------------------------------
# Parser error scrubbing primitives
# ---------------------------------------------------------------------------


class TestErrorScrubbing(unittest.TestCase):
    """Unit-level coverage for SafeArgumentParser.error token scrubbing."""

    def test_mixed_quotes_repr_fully_scrubbed(self) -> None:
        # Python repr of a token containing both quote kinds escapes the
        # apostrophe; the scrubber must replace the entire repr, not stop at
        # the escaped quote and leak the tail.
        token = "PRE'POST\"CRED_MARKER_7f3a9c2e5b"
        raw = f"ignored explicit argument: {token!r}"
        scrubbed = se._QUOTED_TOKEN_RE.sub("'[OMITTED]'", raw)
        self.assertNotIn("CRED_MARKER_7f3a9c2e5b", scrubbed)
        self.assertNotIn("PRE'POST", scrubbed)
        self.assertNotIn('POST"CRED_MARKER', scrubbed)
        self.assertIn("[OMITTED]", scrubbed)

    def test_simple_quoted_token_scrubbed(self) -> None:
        scrubbed = se._QUOTED_TOKEN_RE.sub("'[OMITTED]'", "bad value: 'SECRET'")
        self.assertNotIn("SECRET", scrubbed)

    def test_unquoted_option_value_scrubbed(self) -> None:
        raw = "ambiguous option: --in=SECRET could match: --input, --input-truncated"
        scrubbed = se._OPTION_VALUE_RE.sub(r"\1=[OMITTED]", raw)
        self.assertNotIn("SECRET", scrubbed)
        self.assertIn("--in=[OMITTED]", scrubbed)
        # Option names in the "could match" list remain visible
        self.assertIn("--input", scrubbed)
        self.assertIn("--input-truncated", scrubbed)

    def test_error_scrubs_and_exits_nonzero(self) -> None:
        parser = se.build_parser()
        token = "PRE'POST\"CRED_MARKER_7f3a9c2e5b"
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as cm:
                parser.error(f"ignored explicit argument: {token!r}")
        self.assertEqual(cm.exception.code, se.EXIT_INVALID_INPUT)
        out = stderr.getvalue()
        self.assertNotIn("CRED_MARKER_7f3a9c2e5b", out)
        self.assertNotIn("PRE'POST", out)

    def test_allow_abbrev_disabled(self) -> None:
        parser = se.build_parser()
        self.assertFalse(parser.allow_abbrev)


# ---------------------------------------------------------------------------
# Parser error path: real subprocess CLI regressions
# ---------------------------------------------------------------------------


class TestParserErrorPathRedaction(unittest.TestCase):
    """Real subprocess CLI regressions for argparse error paths.

    argparse used to echo invalid token values (credential-shaped
    ``--command-exit-code``, choice tokens, unknown argv) on stderr BEFORE
    main redaction. SystemExit-only unit assertions cannot observe that
    leak; every case here spawns the real helper and asserts both a nonzero
    exit and absence of the synthetic marker in stdout AND stderr.
    """

    MARKER = "SYNTHETIC_CRED_MARKER_7f3a9c2e5b"
    # Token carrying both quote kinds so Python repr escapes the apostrophe
    # (``'PRE\\'POST"CRED_...' ``). A naive ``'[^']*'`` stops at that escaped
    # quote and leaks the tail on stderr.
    MIXED_QUOTES_MARKER = "PRE'POST\"CRED_MARKER_7f3a9c2e5b"

    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("parser_error_path")
        self.input_path = _write_fixture("parser_base.txt", "line\n")

    def tearDown(self) -> None:
        pass

    def _assert_marker_absent(self, rc: int, stdout: str, stderr: str) -> None:
        self.assertNotEqual(rc, 0)
        self.assertEqual(rc, se.EXIT_INVALID_INPUT)
        self.assertNotIn(self.MARKER, stdout)
        self.assertNotIn(self.MARKER, stderr)
        self.assertNotIn(self.MARKER.lower(), stdout.lower())
        self.assertNotIn(self.MARKER.lower(), stderr.lower())

    def _assert_mixed_marker_absent(self, rc: int, stdout: str, stderr: str) -> None:
        self.assertNotEqual(rc, 0)
        self.assertEqual(rc, se.EXIT_INVALID_INPUT)
        for stream in (stdout, stderr):
            self.assertNotIn(self.MIXED_QUOTES_MARKER, stream)
            self.assertNotIn("CRED_MARKER_7f3a9c2e5b", stream)
            self.assertNotIn("PRE'POST", stream)
            self.assertNotIn('POST"CRED_MARKER', stream)

    def test_invalid_command_exit_code_marker_absent_both_streams(self) -> None:
        # Credential-shaped non-integer --command-exit-code must not be echoed.
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                "text",
                "--command-exit-code",
                self.MARKER,
                "--exit-policy",
                "command",
            ]
        )
        self._assert_marker_absent(rc, stdout, stderr)
        self.assertIn("--command-exit-code", stderr)

    def test_invalid_command_exit_code_equals_form_marker_absent(self) -> None:
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                "text",
                f"--command-exit-code={self.MARKER}",
                "--exit-policy",
                "command",
            ]
        )
        self._assert_marker_absent(rc, stdout, stderr)

    def test_invalid_format_choice_marker_absent_both_streams(self) -> None:
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                self.MARKER,
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
            ]
        )
        self._assert_marker_absent(rc, stdout, stderr)
        self.assertIn("--format", stderr)

    def test_invalid_exit_policy_choice_marker_absent_both_streams(self) -> None:
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                self.MARKER,
            ]
        )
        self._assert_marker_absent(rc, stdout, stderr)
        self.assertIn("--exit-policy", stderr)

    def test_unknown_argument_value_marker_absent_both_streams(self) -> None:
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
                "--unknown-flag",
                self.MARKER,
            ]
        )
        self._assert_marker_absent(rc, stdout, stderr)

    def test_unknown_argument_equals_marker_absent_both_streams(self) -> None:
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
                f"--unknown-flag={self.MARKER}",
            ]
        )
        self._assert_marker_absent(rc, stdout, stderr)

    def test_flag_explicit_value_marker_absent_both_streams(self) -> None:
        # --force=VALUE hits argparse "ignored explicit argument 'VALUE'".
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
                f"--force={self.MARKER}",
            ]
        )
        self._assert_marker_absent(rc, stdout, stderr)

    def test_invalid_top_marker_absent_both_streams(self) -> None:
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
                "--top",
                self.MARKER,
            ]
        )
        self._assert_marker_absent(rc, stdout, stderr)

    def test_invalid_max_examples_marker_absent_both_streams(self) -> None:
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
                "--max-examples",
                self.MARKER,
            ]
        )
        self._assert_marker_absent(rc, stdout, stderr)

    def test_valid_integer_semantics_unchanged_after_parser_fix(self) -> None:
        # Signed Windows NTSTATUS still round-trips; helper exit 0 does not
        # certify the captured command as passed.
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                "text",
                "--command-exit-code",
                "-1073741819",
                "--exit-policy",
                "command",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertEqual(payload["command"]["exit_code"], -1073741819)
        self.assertEqual(payload["command"]["command_status"], "FAILED")
        self.assertEqual(payload["helper"]["exit_status"], "SUCCEEDED")

    def test_abbreviated_option_equals_marker_absent_both_streams(self) -> None:
        # --in=VALUE is an ambiguous abbreviation of --input and
        # --input-truncated. argparse embeds the full raw element unquoted in
        # "ambiguous option: %(option)s"; with allow_abbrev off it becomes an
        # unrecognized argument whose tokens are omitted.
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
                f"--in={self.MARKER}",
            ]
        )
        self._assert_marker_absent(rc, stdout, stderr)

    def test_abbreviated_flag_equals_marker_absent_both_streams(self) -> None:
        # --f=VALUE is an ambiguous abbreviation of --format and --force.
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
                f"--f={self.MARKER}",
            ]
        )
        self._assert_marker_absent(rc, stdout, stderr)

    def test_force_mixed_quotes_marker_absent_both_streams(self) -> None:
        # --force=VALUE with both quote kinds: Python repr escapes the
        # apostrophe and a naive quoted-token regex leaks the tail on stderr.
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
                f"--force={self.MIXED_QUOTES_MARKER}",
            ]
        )
        self._assert_mixed_marker_absent(rc, stdout, stderr)

    def test_input_truncated_mixed_quotes_marker_absent_both_streams(self) -> None:
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(self.input_path),
                "--format",
                "text",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
                f"--input-truncated={self.MIXED_QUOTES_MARKER}",
            ]
        )
        self._assert_mixed_marker_absent(rc, stdout, stderr)


# ---------------------------------------------------------------------------
# Helper exit status separation
# ---------------------------------------------------------------------------


class TestHelperExitContract(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("helper_exit_contract")

    def tearDown(self) -> None:
        pass

    def test_helper_exit_zero_with_failed_command(self) -> None:
        # helper exit 0 must NEVER certify failed tests passed
        input_path = _write_fixture("helper_zero_fail.txt", "line\n")
        summary = se.run(
            _build_args(
                input=str(input_path),
                command_exit_code=2,
                exit_policy="command",
            )
        )
        self.assertEqual(summary["helper"]["exit_status"], "SUCCEEDED")
        self.assertEqual(summary["command"]["command_status"], "FAILED")
        # These two facts must coexist in the same summary object.
        self.assertNotEqual(summary["helper"]["exit_status"], "FIXED")

    def test_helper_exit_zero_via_cli_with_failed_command(self) -> None:
        input_path = _write_fixture("helper_cli_fail.txt", "line\n")
        rc, stdout, _stderr = _run_cli(
            [
                "--input",
                str(input_path),
                "--format",
                "text",
                "--command-exit-code",
                "2",
                "--exit-policy",
                "command",
            ]
        )
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["helper"]["exit_status"], "SUCCEEDED")
        self.assertEqual(payload["command"]["command_status"], "FAILED")

    def test_helper_exit_invalid_for_bad_args(self) -> None:
        input_path = _write_fixture("helper_bad_args.txt", "x\n")
        rc, stdout, stderr = _run_cli(
            [
                "--input",
                str(input_path),
                "--format",
                "not-a-format",
                "--command-exit-code",
                "0",
                "--exit-policy",
                "command",
            ]
        )
        self.assertEqual(rc, se.EXIT_INVALID_INPUT)
        self.assertEqual(stdout, "")
        # Stderr mentions argparse error
        self.assertTrue(stderr)


# ---------------------------------------------------------------------------
# Coverage / completeness semantics
# ---------------------------------------------------------------------------


class TestCoverage(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("coverage")

    def tearDown(self) -> None:
        pass

    def test_coverage_fields_present(self) -> None:
        body = se.summarize_text(
            "a\nb\n\nc\n",
            top=10,
            max_examples=3,
            input_truncated=False,
            redact_counter=[0],
        )
        cov = body["coverage"]
        self.assertEqual(
            set(cov.keys()),
            {
                "total_records",
                "parsed_records",
                "skipped_records",
                "invalid_records",
                "input_complete",
            },
        )
        # "a\nb\n\nc\n" yields exactly 4 records: a, b, internal blank, c.
        # The terminal newline is a delimiter, not an extra phantom record.
        # The internal blank between "b" and "c" is counted and skipped.
        self.assertEqual(cov["total_records"], 4)
        self.assertEqual(cov["parsed_records"], 3)
        self.assertEqual(cov["skipped_records"], 1)

    def test_documented_completeness_field_names(self) -> None:
        # references/large-results.md "Wymuszone pokrycie" documents
        # coverage.input_complete and diagnostics.input_truncated. Operators
        # must not be sent looking for a diagnostics.input_complete field.
        body = se.summarize_text(
            "a\n",
            top=10,
            max_examples=3,
            input_truncated=True,
            redact_counter=[0],
        )
        self.assertIn("input_complete", body["coverage"])
        self.assertIn("input_truncated", body["diagnostics"])
        self.assertNotIn("input_complete", body["diagnostics"])
        self.assertNotIn("input_truncated", body["coverage"])

    def test_input_complete_false_when_truncated(self) -> None:
        body = se.summarize_text(
            "a\n",
            top=10,
            max_examples=3,
            input_truncated=True,
            redact_counter=[0],
        )
        self.assertFalse(body["coverage"]["input_complete"])
        self.assertTrue(body["diagnostics"]["input_truncated"])

    def test_input_complete_true_when_not_truncated(self) -> None:
        body = se.summarize_text(
            "a\n",
            top=10,
            max_examples=3,
            input_truncated=False,
            redact_counter=[0],
        )
        self.assertTrue(body["coverage"]["input_complete"])
        self.assertFalse(body["diagnostics"]["input_truncated"])

    def test_empty_input(self) -> None:
        body = se.summarize_text(
            "",
            top=10,
            max_examples=3,
            input_truncated=False,
            redact_counter=[0],
        )
        # Empty input yields zero records; the terminal-empty fragment that
        # used to inflate the count is no longer reported as a phantom record.
        self.assertEqual(body["coverage"]["total_records"], 0)
        self.assertEqual(body["coverage"]["parsed_records"], 0)
        self.assertEqual(body["coverage"]["skipped_records"], 0)
        self.assertEqual(body["groups"], [])

    def test_four_newline_terminated_records_report_total_four(self) -> None:
        # Terminal newline is a delimiter, not an extra phantom record.
        body = se.summarize_text(
            "r1\nr2\nr3\nr4\n",
            top=10,
            max_examples=3,
            input_truncated=False,
            redact_counter=[0],
        )
        self.assertEqual(body["coverage"]["total_records"], 4)
        self.assertEqual(body["coverage"]["parsed_records"], 4)
        self.assertEqual(body["coverage"]["skipped_records"], 0)
        self.assertEqual(body["coverage"]["invalid_records"], 0)

    def test_internal_blank_counted_and_skipped(self) -> None:
        # Internal blank is counted in total_records and skipped explicitly.
        body = se.summarize_text(
            "r1\n\nr2\nr3\n",
            top=10,
            max_examples=3,
            input_truncated=False,
            redact_counter=[0],
        )
        self.assertEqual(body["coverage"]["total_records"], 4)
        self.assertEqual(body["coverage"]["parsed_records"], 3)
        self.assertEqual(body["coverage"]["skipped_records"], 1)
        self.assertEqual(body["coverage"]["invalid_records"], 0)

    def test_sha256_isolates_bytes_not_truth(self) -> None:
        # SHA-256 of the input bytes must not include plaintext secrets in output
        secret = "VERY_SECRET_VALUE_9999"
        input_path = _write_fixture("sha_bytes.txt", "password=" + secret + "\n")
        summary = se.run(
            _build_args(
                input=str(input_path),
                format="text",
                command_exit_code=0,
                exit_policy="command",
            )
        )
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        # But the input.sha256 is a deterministic hash of the raw bytes
        self.assertEqual(len(summary["input"]["sha256"]), 64)
        # Re-running on the same input must produce the same sha256
        summary2 = se.run(
            _build_args(
                input=str(input_path),
                format="text",
                command_exit_code=0,
                exit_policy="command",
            )
        )
        self.assertEqual(summary["input"]["sha256"], summary2["input"]["sha256"])


# ---------------------------------------------------------------------------
# Group-object schema
# ---------------------------------------------------------------------------


class TestGroupSchema(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture_dir = get_fixture_dir("group_schema")

    def tearDown(self) -> None:
        pass

    def test_group_has_signature_count_examples(self) -> None:
        body = se.summarize_text(
            "ERROR kaboom at /x\nERROR kaboom at /y\nERROR other at /z\n",
            top=10,
            max_examples=3,
            input_truncated=False,
            redact_counter=[0],
        )
        for group in body["groups"]:
            self.assertIn("signature", group)
            self.assertIn("count", group)
            self.assertIn("examples", group)
            self.assertIsInstance(group["signature"], str)
            self.assertIsInstance(group["count"], int)
            self.assertIsInstance(group["examples"], list)
            for example in group["examples"]:
                self.assertIn("line", example)
                self.assertIn("text", example)
                self.assertIsInstance(example["line"], int)
                self.assertIsInstance(example["text"], str)
                # Line numbers are 1-indexed and non-zero
                self.assertGreaterEqual(example["line"], 1)

    def test_line_numbers_preserved(self) -> None:
        # 5 blank lines, then "ERROR /a", "INFO /b", then 2 blank, "ERROR /c"
        text = "\n" * 5 + "ERROR /a\nINFO /b\n\nERROR /c\n"
        body = se.summarize_text(
            text, top=10, max_examples=3, input_truncated=False, redact_counter=[0]
        )
        sig_to_lines: dict[str, list[int]] = {}
        for group in body["groups"]:
            sig_to_lines[group["signature"]] = [ex["line"] for ex in group["examples"]]
        # ERROR /a is on line 6, ERROR /c is on line 10
        self.assertIn("error /a", sig_to_lines)
        self.assertEqual(sig_to_lines["error /a"], [6])


def main() -> int:
    """Test entry point supporting --artifact-root."""
    parser = argparse.ArgumentParser(description="Tests for summarize_evidence.")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
        help="Root directory for retained synthetic test fixtures and evidence.",
    )
    args, unknown = parser.parse_known_args()
    target = _explicit_artifact_root or args.artifact_root
    set_artifact_root(target)

    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
