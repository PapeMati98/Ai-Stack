"""Filter tests."""
def _redacted_fixture_2(kind, ordinal, binary=False):
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

import unittest
import json
import tempfile
from pathlib import Path

from caretaker.filter import (contains_secret, content_hash, filter_records,
                              has_reasoning_marker, looks_like_raw_tool_dump,
                              looks_like_tool_dump_structure, TOOL_RAW_FIELDS,
                              redact_excerpt)
from caretaker.ingest import RawRecord, read_inputs

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"


class FilterHelperTests(unittest.TestCase):

    def test_contains_secret_openai(self):
        self.assertTrue(contains_secret('sk' + '-' + 'SYNTHETIC' + 'x' * 32))
        self.assertFalse(contains_secret("hello world"))

    def test_contains_secret_anthropic(self):
        self.assertTrue(contains_secret('sk' + '-ant-' +
                                       _redacted_fixture_2('assigned-secret', 1)))

    def test_contains_secret_pem(self):
        self.assertTrue(contains_secret(
            _redacted_fixture('private-key', 1).replace('BEGIN ', 'BEGIN RSA ')
            .replace('END ', 'END RSA ')))

    def test_contains_secret_password(self):
        self.assertTrue(contains_secret("password: hunter2"))

    def test_contains_secret_bearer(self):
        self.assertTrue(contains_secret(('Bearer ' + _redacted_fixture_2('bearer-token', 2))))

    def test_reasoning_marker(self):
        self.assertTrue(has_reasoning_marker("chain_of_thought block here"))
        self.assertFalse(has_reasoning_marker("no marker in body"))

    def test_tool_raw_dump(self):
        self.assertTrue(looks_like_raw_tool_dump(
            "tool_calls: foo\ntool_results: bar"))
        self.assertFalse(looks_like_raw_tool_dump("plain text body"))
        self.assertTrue(looks_like_raw_tool_dump("Windows arguments were repaired."))

    def test_tool_dump_structure_accepts_prose_and_inline_mentions(self):
        for body in ("Windows arguments were repaired.",
                     "The tool output was reviewed.",
                     "The `tool_calls` field is documented.",
                     "The word tool_input appears mid sentence.",
                     "Longer argumentation stays ordinary prose."):
            with self.subTest(body=body):
                self.assertFalse(looks_like_tool_dump_structure(body))

    def test_tool_dump_structure_rejects_fields_keys_and_tags(self):
        for field in TOOL_RAW_FIELDS:
            for body in (f'{{"{field}": {{}}}}', f"'{field}': value",
                         f"{field}: value", f"prefix, {field}=value",
                         f"<{field}>value</{field}>", f"< {field.upper()} />"):
                with self.subTest(field=field, body=body):
                    self.assertTrue(looks_like_tool_dump_structure(body))
        self.assertFalse(looks_like_tool_dump_structure(""))

    def test_redact_excerpt_strips_marker_lines(self):
        text = "ok line\nchain_of_thought: hidden\ntool_calls: hidden\ndone"
        out = redact_excerpt(text, 1024)
        self.assertNotIn("chain_of_thought", out)
        self.assertNotIn("tool_calls", out)
        self.assertIn("ok line", out)

    def test_content_hash_is_stable(self):
        h1 = content_hash("a", "b")
        h2 = content_hash("a", "b")
        self.assertEqual(h1, h2)
        h3 = content_hash("a", "c")
        self.assertNotEqual(h1, h3)


class FilterRecordTests(unittest.TestCase):

    def _run(self, path, tool, kind):
        # The public assets retain their native schemas but no private dump or
        # credential examples. Add explicit synthetic cases to a fresh local
        # copy, then exercise the real JSONL reader and filter as before.
        text = path.read_text(encoding='utf-8')
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        seed = rows[0]
        if path.name == 'codex_session.jsonl':
            bodies = ['chain_of_thought: synthetic test marker']
        elif path.name == 'claude_session.jsonl':
            bodies = ['Bearer ' + _redacted_fixture_2('bearer-token', 21),
                      'tool_calls: synthetic test marker']
        elif path.name == 'normalized_event.jsonl':
            bodies = ['Bearer ' + _redacted_fixture_2('bearer-token', 22)]
        else:
            raise AssertionError('Unexpected public fixture asset')
        for ordinal, body in enumerate(bodies, 1):
            row = dict(seed)
            row['cursor'] = 'synthetic-filter-' + str(ordinal)
            row['classification'] = 'internal'
            row['excerpt' if kind == 'normalized_events' else 'content'] = body
            rows.append(row)
        local = Path(tempfile.mkdtemp(prefix='filter-public-fixture-')) / path.name
        local.write_text(''.join(json.dumps(row) + '\n' for row in rows),
                         encoding='utf-8')
        recs, _, _ = read_inputs([(str(local), kind, tool)])
        return recs

    def test_codex_secret_rejected(self):
        # All codex records in the fixture are non-secret; secret comes from
        # claude and normalized fixtures.
        claude_recs = self._run(FIXTURES / "claude_session.jsonl",
                                "claude", "session_jsonl")
        norm_recs = self._run(FIXTURES / "normalized_event.jsonl",
                              "openclaw", "normalized_events")
        outcomes = filter_records(
            claude_recs + norm_recs,
            allowlist_tools=("codex", "openclaw", "claude"),
            max_body_bytes=8192, max_excerpt_bytes=400,
        )
        reasons = [o.reason for o in outcomes]
        self.assertIn("secret_detected", reasons)

    def test_reasoning_rejected(self):
        codex_recs = self._run(FIXTURES / "codex_session.jsonl",
                               "codex", "session_jsonl")
        outcomes = filter_records(
            codex_recs,
            allowlist_tools=("codex", "openclaw", "claude"),
            max_body_bytes=8192, max_excerpt_bytes=400,
        )
        reasons = [o.reason for o in outcomes]
        self.assertIn("reasoning_dump", reasons)

    def test_tool_raw_rejected(self):
        claude_recs = self._run(FIXTURES / "claude_session.jsonl",
                                "claude", "session_jsonl")
        outcomes = filter_records(
            claude_recs,
            allowlist_tools=("codex", "openclaw", "claude"),
            max_body_bytes=8192, max_excerpt_bytes=400,
        )
        reasons = [o.reason for o in outcomes]
        self.assertIn("tool_raw_dump", reasons)

    def test_source_intake_still_rejects_bare_tool_field_word(self):
        record = RawRecord(
            identity_tool="openclaw", source="normalized",
            native_session_id="session-1", cursor="cursor-1",
            observed_at="2026-09-17T10:00:00Z", title="Report",
            body="Windows arguments were repaired.",
        )
        outcome = filter_records(
            [record], allowlist_tools=("openclaw",),
            max_body_bytes=8192, max_excerpt_bytes=400,
        )[0]
        self.assertEqual(outcome.reason, "tool_raw_dump")

    def test_tool_not_allowlisted(self):
        claude_recs = self._run(FIXTURES / "claude_session.jsonl",
                                "claude", "session_jsonl")
        outcomes = filter_records(
            claude_recs, allowlist_tools=("codex",),  # claude excluded
            max_body_bytes=8192, max_excerpt_bytes=400,
        )
        reasons = [o.reason for o in outcomes]
        self.assertTrue(all(r == "tool_not_allowlisted" for r in reasons))

    def test_max_excerpt_preserves_16000_byte_report(self):
        body = "x" * 16_000
        record = RawRecord(
            identity_tool="openclaw", source="normalized",
            native_session_id="session-1", cursor="cursor-1",
            observed_at="2026-09-17T10:00:00Z", title="Report", body=body,
        )
        outcomes = filter_records(
            [record], allowlist_tools=("openclaw",),
            max_body_bytes=16_384, max_excerpt_bytes=16_000,
        )
        self.assertTrue(outcomes[0].accepted)
        self.assertEqual(outcomes[0].excerpt, body)


if __name__ == "__main__":
    unittest.main()
