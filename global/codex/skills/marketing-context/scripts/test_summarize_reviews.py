#!/usr/bin/env python3
"""Retained synthetic tests for marketing-context review synthesis.

Covers identity duplicates vs equal text/different identities, conflicting
duplicate identities, invalid/provenance URLs, unknown dates, opposing
opinions, denominator consistency, redaction/errors and existing-file refusal.

Complies with workspace rules:
- Accepts --artifact-root and retains all synthetic fixtures without cleanup.
- Never invokes rmtree, unlink, TemporaryDirectory, or deletion.
- Enforces PYTHONDONTWRITEBYTECODE=1.
"""

from __future__ import annotations
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


import argparse
import json
import os
import sys
import unittest
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS_DIR.parent
SKILLS_ROOT = SKILL_ROOT.parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from summarize_reviews import (  # noqa: E402
    ReviewSynthesisError,
    _create_new_file_exclusive,
    _looks_like_secret_file,
    content_key,
    looks_sensitive,
    normalize_record,
    redact_url,
    run_cli,
    safe_emit_label,
    summarize_reviews,
    validate_source_url,
)

DEFAULT_ARTIFACT_ROOT = SCRIPTS_DIR / '.test-artifacts' / 'review-synthesis'
ARTIFACT_ROOT = DEFAULT_ARTIFACT_ROOT


def set_artifact_root(root: Path) -> None:
    global ARTIFACT_ROOT
    ARTIFACT_ROOT = root.resolve()
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)


_explicit_artifact_root: Path | None = None
_cleaned_argv = [sys.argv[0]] if sys.argv else ["test_summarize_reviews.py"]
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


def get_fixture_dir(name: str) -> Path:
    target = ARTIFACT_ROOT / "unit_tests" / name
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def unique_output(fixture: Path, name: str) -> Path:
    """Fresh output path so retained fixtures can be re-run without deletion."""
    return fixture / f"{uuid.uuid4().hex[:8]}_{name}"


def rec(
    rec_id: str,
    source_url: str,
    text: str,
    themes: list[str],
    published_at: str | None = "2026-01-01T00:00:00Z",
    stance: str | None = "positive",
) -> dict:
    value: dict = {
        "id": rec_id,
        "source_url": source_url,
        "text": text,
        "themes": themes,
    }
    if published_at is not None:
        value["published_at"] = published_at
    if stance is not None:
        value["stance"] = stance
    return value


class TestIdentityDeduplication(unittest.TestCase):
    def test_same_identity_same_content_is_one_review_with_duplicate_row(self) -> None:
        rows = [
            rec("r1", "https://reviews.example/item/1", "Setup felt quick", ["onboarding"]),
            rec("r1", "https://reviews.example/item/1", "Setup felt quick", ["onboarding"]),
        ]
        out = summarize_reviews(rows)
        cov = out["coverage"]
        self.assertEqual(cov["total_rows"], 2)
        self.assertEqual(cov["valid_rows"], 2)
        self.assertEqual(cov["duplicate_rows"], 1)
        self.assertEqual(cov["unique_reviews"], 1)
        self.assertEqual(cov["invalid_rows"], 0)
        self.assertEqual(cov["conflict_rows"], 0)

    def test_equal_text_different_identities_count_independently(self) -> None:
        rows = [
            rec("r1", "https://reviews.example/item/1", "same words", ["x"]),
            rec("r2", "https://reviews.example/item/1", "same words", ["x"]),
            rec("r1", "https://blog.example/review", "same words", ["x"]),
        ]
        out = summarize_reviews(rows)
        self.assertEqual(out["coverage"]["unique_reviews"], 3)
        self.assertEqual(out["coverage"]["duplicate_rows"], 0)
        self.assertEqual(out["conflicts"], [])

    def test_same_id_different_source_url_is_distinct_identity(self) -> None:
        rows = [
            rec("r1", "https://a.example/r", "text one", ["x"]),
            rec("r1", "https://b.example/r", "text one", ["x"]),
        ]
        out = summarize_reviews(rows)
        self.assertEqual(out["coverage"]["unique_reviews"], 2)
        self.assertEqual(out["coverage"]["duplicate_rows"], 0)

    def test_text_is_never_used_as_identity(self) -> None:
        a = rec("r1", "https://a.example/r", "hello", ["x"])
        b = rec("r2", "https://a.example/r", "hello", ["x"])
        self.assertNotEqual(
            (a["source_url"], a["id"]),
            (b["source_url"], b["id"]),
        )
        out = summarize_reviews([a, b])
        self.assertEqual(out["coverage"]["unique_reviews"], 2)

    def test_equal_text_counted_only_for_consistent_identities(self) -> None:
        """Worked-example rule: conflicted identity does not count equal text."""
        rows = [
            rec("r1", "https://reviews.example/item/1", "Setup felt quick", ["onboarding"]),
            rec("r1", "https://reviews.example/item/1", "Setup was slow", ["onboarding"]),
            rec("r3", "https://blog.example/review", "Setup felt quick", ["onboarding"]),
        ]
        out = summarize_reviews(rows)
        self.assertEqual(out["coverage"]["unique_reviews"], 1)
        self.assertEqual(out["coverage"]["conflict_identities"], 1)
        self.assertEqual(out["themes"]["onboarding"]["count"], 1)
        self.assertEqual(out["themes"]["onboarding"]["review_ids"], ["r3"])
        self.assertNotIn("r1", out["themes"]["onboarding"]["review_ids"])
        self.assertEqual(out["source_distribution"], {"https://blog.example/review": 1})


class TestConflictingIdentities(unittest.TestCase):
    def test_conflicting_same_identity_is_visible_and_unresolved(self) -> None:
        rows = [
            rec("r1", "https://reviews.example/item/1", "Setup felt quick", ["onboarding"]),
            rec("r1", "https://reviews.example/item/1", "Setup was slow", ["onboarding"]),
        ]
        out = summarize_reviews(rows)
        cov = out["coverage"]
        self.assertEqual(cov["unique_reviews"], 0)
        self.assertEqual(cov["conflict_rows"], 2)
        self.assertEqual(cov["conflict_identities"], 1)
        self.assertEqual(cov["duplicate_rows"], 0)
        self.assertEqual(len(out["conflicts"]), 1)
        conflict = out["conflicts"][0]
        self.assertEqual(conflict["id"], "r1")
        self.assertEqual(conflict["variant_count"], 2)
        self.assertIn("unresolved", conflict["reason"])
        self.assertIn("not silently selected", conflict["reason"])
        dumped = json.dumps(out)
        self.assertNotIn("Setup felt quick", dumped)
        self.assertNotIn("Setup was slow", dumped)

    def test_conflicting_identity_not_counted_as_unique_review(self) -> None:
        rows = [
            rec("r1", "https://a.example/r", "good", ["x"], stance="positive"),
            rec("r1", "https://a.example/r", "bad", ["x"], stance="negative"),
            rec("r2", "https://a.example/r", "fine", ["y"], stance="neutral"),
        ]
        out = summarize_reviews(rows)
        self.assertEqual(out["coverage"]["unique_reviews"], 1)
        self.assertEqual(out["themes"]["y"]["review_ids"], ["r2"])
        self.assertNotIn("x", out["themes"])

    def test_exact_duplicates_inside_unconflicted_identity(self) -> None:
        rows = [
            rec("r1", "https://a.example/r", "same", ["x"]),
            rec("r1", "https://a.example/r", "same", ["x"]),
            rec("r1", "https://a.example/r", "same", ["x"]),
        ]
        out = summarize_reviews(rows)
        self.assertEqual(out["coverage"]["duplicate_rows"], 2)
        self.assertEqual(out["coverage"]["unique_reviews"], 1)
        self.assertEqual(out["conflicts"], [])


class TestProvenanceUrls(unittest.TestCase):
    def test_accepts_http_and_https_only(self) -> None:
        self.assertTrue(validate_source_url("https://example.com/reviews")[0])
        self.assertTrue(validate_source_url("http://example.com/reviews")[0])

    def test_rejects_non_http_schemes(self) -> None:
        for bad in (
            "ftp://example.com/r",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/plain,hi",
            "ws://example.com/feed",
            "gopher://example.com/r",
            "example.com/no-scheme",
            "/relative/path",
            "",
            "   ",
        ):
            ok, reason = validate_source_url(bad)
            self.assertFalse(ok, f"should reject {bad!r}")
            self.assertTrue(reason)

    def test_rejects_credentials_in_url(self) -> None:
        for bad in (
            "https://user:pass@example.com/r",
            "https://user@example.com/r",
            "http://admin:secret@host.internal/path",
        ):
            ok, reason = validate_source_url(bad)
            self.assertFalse(ok, f"should reject {bad!r}")
            self.assertIn("credentials", reason)

    def test_rejects_missing_host(self) -> None:
        ok, _ = validate_source_url("https://")
        self.assertFalse(ok)

    def test_invalid_url_rows_accounted_and_redacted(self) -> None:
        rows = [
            rec("r1", "https://ok.example/r", "fine", ["x"]),
            rec("r2", "https://user:hunter2@bad.example/r", "secret text token=abc", ["x"]),
            rec("r3", "ftp://files.example/r", "also secret", ["x"]),
        ]
        out = summarize_reviews(rows)
        self.assertEqual(out["coverage"]["total_rows"], 3)
        self.assertEqual(out["coverage"]["invalid_rows"], 2)
        self.assertEqual(out["coverage"]["valid_rows"], 1)
        self.assertEqual(out["coverage"]["unique_reviews"], 1)
        dumped = json.dumps(out)
        self.assertNotIn("hunter2", dumped)
        self.assertNotIn("token=abc", dumped)
        self.assertNotIn("secret text", dumped)
        self.assertNotIn("also secret", dumped)
        self.assertIn("https://bad.example/r", dumped)
        self.assertIn("ftp://files.example/r", dumped)

    def test_syntax_valid_url_still_not_content_verified(self) -> None:
        ok, reason = validate_source_url("https://example.com/review/1")
        self.assertTrue(ok)
        self.assertIn("does not verify content", reason)

    def test_redact_url_strips_credentials_and_query(self) -> None:
        self.assertEqual(
            redact_url("https://user:pass@example.com/path?q=secret"),
            "https://example.com/path",
        )
        self.assertEqual(
            redact_url('https://synthetic-user:synthetic-password@example.invalid/reviews?api_key=' + 'SYNTHETIC_SENTINEL_67421'),
            "https://example.invalid/reviews",
        )
        self.assertEqual(redact_url("nope"), "<unparseable-url>")
        self.assertEqual(redact_url(None), "<missing-url>")

    def test_rejects_credential_like_query_parameters(self) -> None:
        for bad in (
            'https://example.invalid/r?api_key=' + 'SYNTHETIC_SENTINEL_67421',
            "https://example.invalid/r?token=abc123",
            "https://example.invalid/r?password=hunter2",
            "https://example.invalid/r?page=2&API_KEY=zzz",
            "https://example.invalid/r?access_token=tok",
        ):
            ok, reason = validate_source_url(bad)
            self.assertFalse(ok, f"should reject {bad!r}")
            self.assertIn("query", reason.lower())
            self.assertNotIn("SYNTHETIC_SENTINEL_67421", reason)

    def test_allows_non_credential_query_parameters(self) -> None:
        ok, _ = validate_source_url("https://example.invalid/reviews?page=2&sort=new")
        self.assertTrue(ok)


class TestUnknownDatesAndOpposingStances(unittest.TestCase):
    def test_unknown_dates_retained_and_reported(self) -> None:
        rows = [
            rec("r1", "https://a.example/r", "dated", ["x"], published_at="2026-02-01T00:00:00Z"),
            rec("r2", "https://a.example/r", "undated", ["x"], published_at=None),
            rec("r3", "https://a.example/r", "also undated", ["x"], published_at=None),
        ]
        out = summarize_reviews(rows)
        self.assertEqual(out["coverage"]["unique_reviews"], 3)
        joined = " ".join(out["limitations"])
        self.assertIn("unknown dates", joined)
        self.assertIn("retained", joined)
        self.assertIn("cannot support temporal claims", joined)
        self.assertEqual(out["stance_breakdown"].get("unspecified", 0), 0)

    def test_missing_published_at_counts_as_unknown(self) -> None:
        rows = [
            {
                "id": "r1",
                "source_url": "https://a.example/r",
                "text": "no date field",
                "themes": ["x"],
            }
        ]
        out = summarize_reviews(rows)
        self.assertEqual(out["coverage"]["unique_reviews"], 1)
        self.assertTrue(any("unknown dates" in item for item in out["limitations"]))

    def test_opposing_stances_both_retained_and_reported(self) -> None:
        rows = [
            rec("r1", "https://a.example/r", "loved it", ["x"], stance="positive"),
            rec("r2", "https://a.example/r", "hated it", ["x"], stance="negative"),
            rec("r3", "https://a.example/r", "mixed", ["x"], stance="neutral"),
        ]
        out = summarize_reviews(rows)
        self.assertEqual(out["stance_breakdown"]["positive"], 1)
        self.assertEqual(out["stance_breakdown"]["negative"], 1)
        self.assertEqual(out["stance_breakdown"]["neutral"], 1)
        joined = " ".join(out["limitations"])
        self.assertIn("opposing or mixed stances", joined)
        self.assertIn("must not be netted", joined)
        self.assertIn("population facts", joined)

    def test_single_stance_has_no_opposing_limitation(self) -> None:
        rows = [rec("r1", "https://a.example/r", "ok", ["x"], stance="positive")]
        out = summarize_reviews(rows)
        self.assertEqual(out["stance_breakdown"], {"positive": 1})
        self.assertFalse(
            any("opposing or mixed stances" in item for item in out["limitations"])
        )

    def test_unspecified_stance_kept(self) -> None:
        rows = [
            rec("r1", "https://a.example/r", "ok", ["x"], stance=None),
        ]
        out = summarize_reviews(rows)
        self.assertEqual(out["stance_breakdown"]["unspecified"], 1)


class TestDenominatorConsistency(unittest.TestCase):
    def _assert_denominators(self, out: dict) -> None:
        cov = out["coverage"]
        self.assertEqual(
            cov["total_rows"],
            cov["valid_rows"] + cov["invalid_rows"],
            "total must split into valid + invalid",
        )
        self.assertEqual(
            cov["valid_rows"],
            cov["unique_reviews"] + cov["duplicate_rows"] + cov["conflict_rows"],
            "valid must split into unique + duplicate + conflict rows",
        )

    def test_mixed_dataset_denominators(self) -> None:
        rows = [
            rec("r1", "https://a.example/r", "keep", ["x"]),
            rec("r1", "https://a.example/r", "keep", ["x"]),
            rec("r2", "https://a.example/r", "other words", ["y"]),
            rec("r2", "https://a.example/r", "conflicting words", ["y"]),
            rec("r3", "https://b.example/r", "third", ["z"]),
            rec("bad", "not-a-url", "nope", ["x"]),
        ]
        out = summarize_reviews(rows)
        self._assert_denominators(out)
        cov = out["coverage"]
        self.assertEqual(cov["total_rows"], 6)
        self.assertEqual(cov["valid_rows"], 5)
        self.assertEqual(cov["invalid_rows"], 1)
        self.assertEqual(cov["duplicate_rows"], 1)
        self.assertEqual(cov["unique_reviews"], 2)
        self.assertEqual(cov["conflict_rows"], 2)

    def test_empty_input(self) -> None:
        out = summarize_reviews([])
        self._assert_denominators(out)
        self.assertEqual(out["coverage"]["unique_reviews"], 0)
        self.assertTrue(out["sample_only"])
        self.assertEqual(out["conflicts"], [])

    def test_source_distribution_and_theme_ids_use_unique_reviews(self) -> None:
        rows = [
            rec("r1", "https://a.example/r", "one", ["pricing", "onboarding"]),
            rec("r1", "https://a.example/r", "one", ["pricing", "onboarding"]),
            rec("r2", "https://b.example/r", "two", ["onboarding"]),
        ]
        out = summarize_reviews(rows)
        self.assertEqual(out["source_distribution"], {
            "https://a.example/r": 1,
            "https://b.example/r": 1,
        })
        self.assertEqual(out["themes"]["onboarding"]["count"], 2)
        self.assertEqual(out["themes"]["onboarding"]["review_ids"], ["r1", "r2"])
        self.assertEqual(out["themes"]["pricing"]["review_ids"], ["r1"])

    def test_sample_only_always_true(self) -> None:
        out = summarize_reviews([rec("r1", "https://a.example/r", "t", ["x"])])
        self.assertIs(out["sample_only"], True)
        self.assertTrue(any("population facts" in item for item in out["limitations"]))


class TestRedactionAndErrors(unittest.TestCase):
    def test_validation_errors_do_not_echo_text(self) -> None:
        rows = [
            {
                "id": "r1",
                "source_url": "https://a.example/r",
                "text": 12345,
                "themes": ["x"],
            }
        ]
        out = summarize_reviews(rows)
        dumped = json.dumps(out)
        self.assertNotIn("12345", dumped)
        self.assertTrue(out["invalid_records"])
        self.assertTrue(any("text" in reason for reason in out["invalid_records"][0]["reasons"]))

    def test_normalize_record_rejects_bad_types_without_values(self) -> None:
        normalized, errors = normalize_record({"text": "super-secret-body"}, 4)
        self.assertIsNone(normalized)
        joined = " ".join(errors)
        self.assertNotIn("super-secret-body", joined)

    def test_non_object_rows_are_invalid(self) -> None:
        out = summarize_reviews(["just a string", 7, None])
        self.assertEqual(out["coverage"]["invalid_rows"], 3)
        self.assertEqual(out["coverage"]["unique_reviews"], 0)
        self._assert_redacted(out)

    def test_non_list_input_rejected(self) -> None:
        with self.assertRaises(ReviewSynthesisError):
            summarize_reviews({"records": []})

    def _assert_redacted(self, out: dict) -> None:
        dumped = json.dumps(out)
        self.assertNotIn("just a string", dumped)

    def test_secret_filename_refused(self) -> None:
        fixture = get_fixture_dir("redaction_errors")
        secret_path = fixture / ".env"
        secret_path.write_text("API_KEY=redacted", encoding="utf-8")
        with self.assertRaises(ReviewSynthesisError):
            run_cli(str(secret_path), None)

    def test_secret_filename_aliases_and_reparse_refused(self) -> None:
        fixture = get_fixture_dir("secret_aliases")
        for name in (".ENV", ".env.local", "secrets.json", "credentials.json", "netrc"):
            alias = fixture / name
            alias.write_text("not-a-review", encoding="utf-8")
            self.assertTrue(_looks_like_secret_file(alias), name)
            with self.assertRaises(ReviewSynthesisError):
                run_cli(str(alias), None)
        nested = fixture / "data" / ".env"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("API_KEY=x", encoding="utf-8")
        reparse = fixture / "data" / "sub" / ".." / ".env"
        self.assertTrue(_looks_like_secret_file(reparse))
        with self.assertRaises(ReviewSynthesisError):
            run_cli(str(reparse), None)

    def test_content_key_differs_on_stance_and_date(self) -> None:
        base = rec("r1", "https://a.example/r", "t", ["x"], published_at="2026-01-01T00:00:00Z", stance="positive")
        other_stance = rec("r1", "https://a.example/r", "t", ["x"], published_at="2026-01-01T00:00:00Z", stance="negative")
        other_date = rec("r1", "https://a.example/r", "t", ["x"], published_at="2026-02-01T00:00:00Z", stance="positive")
        same = rec("r1", "https://a.example/r", "t", ["x", "x"], published_at="2026-01-01T00:00:00Z", stance="positive")
        self.assertNotEqual(content_key(base), content_key(other_stance))
        self.assertNotEqual(content_key(base), content_key(other_date))
        self.assertEqual(content_key(base), content_key(same))


class TestExistingFileRefusalAndCli(unittest.TestCase):
    def test_existing_output_refused_without_modification(self) -> None:
        fixture = get_fixture_dir("existing_file_refusal")
        input_path = write_json(
            fixture / "input.json",
            [rec("r1", "https://a.example/r", "hello", ["x"])],
        )
        output_path = fixture / "summary.json"
        prior = '{"prior": true}\n'
        output_path.write_text(prior, encoding="utf-8")
        with self.assertRaises(ReviewSynthesisError):
            run_cli(str(input_path), str(output_path))
        self.assertEqual(output_path.read_text(encoding="utf-8"), prior)

    def test_output_same_as_input_refused(self) -> None:
        fixture = get_fixture_dir("existing_file_refusal")
        input_path = write_json(
            fixture / "input_shared.json",
            [rec("r1", "https://a.example/r", "hello", ["x"])],
        )
        before = input_path.read_text(encoding="utf-8")
        with self.assertRaises(ReviewSynthesisError):
            run_cli(str(input_path), str(input_path))
        self.assertEqual(input_path.read_text(encoding="utf-8"), before)

    def test_cli_writes_new_output_file(self) -> None:
        fixture = get_fixture_dir("cli_roundtrip")
        input_path = write_json(
            fixture / "input.json",
            [
                rec("r1", "https://a.example/r", "hello", ["x"], stance="positive"),
                rec("r2", "https://a.example/r", "bye", ["y"], stance="negative", published_at=None),
            ],
        )
        output_path = unique_output(fixture, "summary.json")
        self.assertFalse(output_path.exists())
        result = run_cli(str(input_path), str(output_path))
        self.assertTrue(output_path.is_file())
        loaded = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["coverage"]["unique_reviews"], 2)
        self.assertIs(loaded["sample_only"], True)
        self.assertEqual(result["coverage"]["total_rows"], 2)

    def test_cli_main_exit_codes(self) -> None:
        from summarize_reviews import main

        fixture = get_fixture_dir("cli_exit_codes")
        input_path = write_json(
            fixture / "input.json",
            [rec("r1", "https://a.example/r", "hello", ["x"])],
        )
        self.assertEqual(main(["--input", str(input_path)]), 0)
        self.assertEqual(main(["--input", str(fixture / "missing.json")]), 1)

        bad_url_path = write_json(
            fixture / "bad.json",
            [rec("r1", "ftp://x.example/r", "hello", ["x"])],
        )
        output_path = unique_output(fixture, "out.json")
        self.assertEqual(
            main(["--input", str(bad_url_path), "--output", str(output_path)]),
            0,
        )
        self.assertTrue(output_path.is_file())
        self.assertEqual(
            main(["--input", str(bad_url_path), "--output", str(output_path)]),
            1,
        )

    def test_argument_parser_shape(self) -> None:
        from summarize_reviews import build_parser

        parser = build_parser()
        args = parser.parse_args(["--input", "in.json", "--output", "out.json"])
        self.assertEqual(args.input, "in.json")
        self.assertEqual(args.output, "out.json")
        args2 = parser.parse_args(["--input", "in.json"])
        self.assertIsNone(args2.output)


class TestSensitiveMetadataRedaction(unittest.TestCase):
    """Regression for product failure: api_key URL query and password= themes leaked."""

    SENTINEL = "SYNTHETIC_SENTINEL_67421"

    def test_api_key_query_param_rejected_without_leak(self) -> None:
        rows = [
            {
                "id": "r1",
                "source_url": f"https://example.invalid/reviews?api_key={self.SENTINEL}",
                "text": "body",
                "themes": ["onboarding"],
            }
        ]
        out = summarize_reviews(rows)
        dumped = json.dumps(out, indent=2)
        self.assertNotIn(self.SENTINEL, dumped)
        self.assertEqual(out["coverage"]["invalid_rows"], 1)
        self.assertEqual(out["coverage"]["valid_rows"], 0)
        self.assertEqual(
            out["invalid_records"][0]["source_url_redacted"],
            "https://example.invalid/reviews",
        )
        self.assertTrue(
            any("query" in reason.lower() for reason in out["invalid_records"][0]["reasons"])
        )

    def test_password_theme_redacted_without_leak(self) -> None:
        rows = [
            {
                "id": "r1",
                "source_url": "https://example.invalid/reviews",
                "text": "body",
                "themes": [f"password={self.SENTINEL}"],
            }
        ]
        out = summarize_reviews(rows)
        dumped = json.dumps(out, indent=2)
        self.assertNotIn(self.SENTINEL, dumped)
        self.assertNotIn(f"password={self.SENTINEL}", dumped)
        self.assertEqual(out["coverage"]["valid_rows"], 1)
        self.assertEqual(out["coverage"]["unique_reviews"], 1)
        self.assertEqual(len(out["themes"]), 1)
        theme_key = next(iter(out["themes"]))
        self.assertTrue(theme_key.startswith("<redacted:"), theme_key)
        self.assertTrue(
            any("redacted" in item for item in out["limitations"]),
            out["limitations"],
        )

    def test_combined_marker_record_cli_stdout_has_no_leak(self) -> None:
        rows = [
            {
                "id": "review-1",
                "source_url": f"https://example.invalid/reviews?api_key={self.SENTINEL}",
                "text": "great product",
                "themes": [f"password={self.SENTINEL}"],
                "published_at": "2026-01-01T00:00:00Z",
                "stance": "positive",
            }
        ]
        out = summarize_reviews(rows)
        self.assertNotIn(self.SENTINEL, json.dumps(out, indent=2))

        fixture = get_fixture_dir("sensitive_metadata")
        input_path = write_json(fixture / "marker.json", rows)
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from summarize_reviews import main

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main(["--input", str(input_path)])
        combined = stdout_buf.getvalue() + stderr_buf.getvalue()
        self.assertEqual(code, 0)
        self.assertNotIn(self.SENTINEL, combined)
        self.assertIn("sample_only", combined)

        output_path = unique_output(fixture, "summary.json")
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main(["--input", str(input_path), "--output", str(output_path)])
        combined = stdout_buf.getvalue() + stderr_buf.getvalue()
        self.assertEqual(code, 0)
        self.assertNotIn(self.SENTINEL, combined)
        self.assertNotIn(self.SENTINEL, output_path.read_text(encoding="utf-8"))

    def test_identity_uses_original_values_after_redaction(self) -> None:
        rows = [
            rec("r1", "https://a.example/r?page=1", "one", ["x"]),
            rec("r1", "https://a.example/r?page=2", "two", ["x"]),
            rec("r1", "https://a.example/r?page=1", "one", ["x"]),
        ]
        out = summarize_reviews(rows)
        self.assertEqual(out["coverage"]["unique_reviews"], 2)
        self.assertEqual(out["coverage"]["duplicate_rows"], 1)
        self.assertEqual(out["coverage"]["conflict_rows"], 0)
        self.assertEqual(out["source_distribution"], {"https://a.example/r": 2})

    def test_distinct_ids_not_merged_when_one_is_sensitive(self) -> None:
        rows = [
            rec("r1", "https://a.example/r", "one", ["x"]),
            rec(f"password={self.SENTINEL}", "https://a.example/r", "two", ["y"]),
        ]
        out = summarize_reviews(rows)
        self.assertEqual(out["coverage"]["unique_reviews"], 2)
        dumped = json.dumps(out, indent=2)
        self.assertNotIn(self.SENTINEL, dumped)
        ids = set()
        for entry in out["themes"].values():
            ids.update(entry["review_ids"])
        self.assertIn("r1", ids)
        self.assertTrue(any(i.startswith("<redacted:") for i in ids))

    def test_sensitive_stance_and_conflict_labels_redacted(self) -> None:
        rows = [
            rec(
                "r1",
                "https://a.example/r",
                "one",
                ["x"],
                stance=f"token={self.SENTINEL}",
            ),
            rec(
                "r2",
                "https://a.example/r",
                "two",
                ["x"],
                stance="positive",
            ),
        ]
        out = summarize_reviews(rows)
        dumped = json.dumps(out, indent=2)
        self.assertNotIn(self.SENTINEL, dumped)
        self.assertEqual(out["coverage"]["unique_reviews"], 2)
        self.assertIn("positive", out["stance_breakdown"])
        sensitive_labels = [
            label for label in out["stance_breakdown"] if label != "positive"
        ]
        self.assertEqual(len(sensitive_labels), 1)
        self.assertTrue(sensitive_labels[0].startswith("<redacted:"))

        conflict_rows = [
            rec(f"password={self.SENTINEL}", "https://a.example/r", "one", ["x"]),
            rec(f"password={self.SENTINEL}", "https://a.example/r", "two", ["x"]),
        ]
        conflict_out = summarize_reviews(conflict_rows)
        dumped = json.dumps(conflict_out, indent=2)
        self.assertNotIn(self.SENTINEL, dumped)
        self.assertEqual(conflict_out["coverage"]["conflict_identities"], 1)
        self.assertTrue(conflict_out["conflicts"][0]["id"].startswith("<redacted:"))
        self.assertEqual(
            conflict_out["conflicts"][0]["source_url"], "https://a.example/r"
        )

    def test_looks_sensitive_and_safe_emit_label(self) -> None:
        self.assertTrue(looks_sensitive(f"password={self.SENTINEL}"))
        self.assertTrue(looks_sensitive("api_key=abc"))
        self.assertTrue(looks_sensitive(('Bearer ' + _redacted_fixture('bearer-token', 1))))
        self.assertFalse(looks_sensitive("onboarding"))
        self.assertFalse(looks_sensitive("r1"))
        self.assertFalse(looks_sensitive("positive"))
        self.assertEqual(safe_emit_label("onboarding"), "onboarding")
        redacted = safe_emit_label(f"password={self.SENTINEL}")
        self.assertTrue(redacted.startswith("<redacted:"))
        self.assertNotIn(self.SENTINEL, redacted)
        self.assertNotEqual(
            safe_emit_label(f"password={self.SENTINEL}"),
            safe_emit_label("password=other"),
        )

    def test_path_secret_in_source_url_hash_redacted(self) -> None:
        label = redact_url(f"https://example.invalid/password={self.SENTINEL}")
        self.assertNotIn(self.SENTINEL, label)
        self.assertTrue(label.startswith("<redacted-url:"), label)

    def test_create_new_file_exclusive_refuses_existing(self) -> None:
        fixture = get_fixture_dir("atomic_exclusive")
        target = fixture / "out.json"
        target.write_text("prior\n", encoding="utf-8")
        with self.assertRaises(ReviewSynthesisError):
            _create_new_file_exclusive(target, "new\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "prior\n")
        fresh = unique_output(fixture, "fresh.json")
        self.assertFalse(fresh.exists())
        _create_new_file_exclusive(fresh, "created\n")
        self.assertEqual(fresh.read_text(encoding="utf-8"), "created\n")


class TestDocumentationBoundaries(unittest.TestCase):
    """Instruction checks: opt-in pointers only, no new acceptance bureaucracy."""

    def test_marketing_default_flow_and_description_preserved(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Build or audit a product marketing context", skill)
        self.assertIn("observed customer language", skill)
        self.assertIn("open tests", skill.lower())
        self.assertIn("references/contract.md", skill)
        self.assertIn("review-synthesis.md", skill)
        self.assertNotIn("100 reviews", skill.lower())
        self.assertNotIn("minimum 100", skill.lower())
        self.assertNotIn("3 sources", skill.lower())
        self.assertNotIn("ranking weight", skill.lower())

    def test_review_synthesis_reference_declares_limits(self) -> None:
        doc = (SKILL_ROOT / "references" / "review-synthesis.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("sample_only", doc)
        self.assertIn("population", doc.lower())
        self.assertIn("source_url", doc)
        self.assertIn("worked example", doc.lower())
        self.assertIn("does not verify", doc.lower())
        self.assertIn("internally consistent", doc)
        self.assertIn("counted once", doc)
        self.assertNotIn("counted twice by design", doc)
        self.assertIn("redacted", doc.lower())
        self.assertIn("credential-like query", doc.lower())
        self.assertIn("never merges two unrelated records", doc.lower())

    def test_baseline_comparison_reference_constraints(self) -> None:
        doc = (SKILLS_ROOT / "accept-system" / "references" / "baseline-comparison.md").read_text(
            encoding="utf-8"
        )
        lowered = doc.lower()
        self.assertIn("pixel diff", lowered)
        self.assertIn("not uat", lowered)
        self.assertIn("score calculator", lowered)
        self.assertIn("acceptance.py", doc)
        self.assertIn("owner-required", lowered)
        self.assertIn("expected", lowered)
        self.assertIn("actual", lowered)

    def test_odbierz_skill_gets_only_conditional_pointer(self) -> None:
        skill = (SKILLS_ROOT / "accept-system" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("baseline-comparison.md", skill)
        self.assertIn("compare", skill.lower())
        self.assertIn("scripts/acceptance.py", skill)

    def test_helper_source_has_no_network_or_model_calls(self) -> None:
        source = (SCRIPTS_DIR / "summarize_reviews.py").read_text(encoding="utf-8")
        for banned in (
            "urllib.request",
            "http.client",
            "requests.",
            "socket.socket",
            "subprocess",
            "openai",
            "anthropic",
            "os.environ.get",
            "os.getenv",
            "dotenv",
        ):
            self.assertNotIn(banned, source, f"helper must not use {banned}")

    def test_helper_source_has_no_deletion_apis(self) -> None:
        source = (SCRIPTS_DIR / "summarize_reviews.py").read_text(encoding="utf-8")
        for banned in ("rmtree", "unlink(", "TemporaryDirectory", "shutil.rmtree"):
            self.assertNotIn(banned, source)


def main() -> int:
    parser = argparse.ArgumentParser(description="Test suite for review synthesis.")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
        help="Root directory for retained synthetic test fixtures and evidence.",
    )
    args, _unknown = parser.parse_known_args()
    target_root = _explicit_artifact_root or args.artifact_root
    set_artifact_root(target_root)

    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
