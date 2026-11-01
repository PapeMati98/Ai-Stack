"""Redacted permission-denial diagnostics of the Grok ACP broker (candidate).

The classifier must describe WHY a request was refused without leaking paths outside
project/, file contents, commands or options payloads, and must never change the answer."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grok_acp  # noqa: E402

SECRET = "TOP-SECRET-CONTENT-4711"


def request(raw_input, *, kind="edit", options=("allow_once", "reject_once")):
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "session/request_permission",
        "params": {
            "toolCall": {"kind": kind, "title": "C:\\fixtures\\someone\\secret\\x.txt", "rawInput": raw_input},
            "options": [{"kind": k, "optionId": f"opt-{k}", "name": SECRET} for k in options],
        },
    }


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        (self.root / "existing.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def classify(self, raw, **kw):
        record = grok_acp.classify_permission_denial(
            request(raw, **kw), allow_writes=True, workspace_root=self.root, deny_all_tools=False
        )
        dumped = json.dumps(record)
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn("someone", dumped)
        self.assertNotIn(self.tmp.name, dumped)
        return record

    def test_unobserved_variant_is_named_with_relative_target(self):
        r = self.classify({"variant": "Delete", "file_path": "project/new.txt", "content": SECRET})
        self.assertEqual(r["reason"], "write_variant_not_allowed")
        self.assertEqual(r["variant"], "Delete")
        self.assertEqual(r["path_shape"], "project_relative")
        self.assertEqual(r["project_relative_path"], "project/new.txt")
        self.assertEqual(r["raw_input_keys"], ["content", "file_path", "variant"])

    def test_write_without_content_is_refused(self):
        r = self.classify({"variant": "Write", "file_path": "project/new.txt"})
        self.assertEqual(r["reason"], "write_content_missing")

    def test_absolute_drive_path_is_not_leaked(self):
        r = self.classify({"variant": "SearchReplace", "file_path": "C:\\fixtures\\someone\\secret\\x.txt"})
        self.assertEqual(r["reason"], "path_drive_absolute")
        self.assertNotIn("project_relative_path", r)

    def test_posix_unc_traversal_outside(self):
        for raw, reason in (
            ("/etc/passwd", "path_posix_absolute"),
            ("\\\\host\\share\\x", "path_unc"),
            ("project/../x", "path_traversal"),
            ("other/x", "path_relative_outside_project"),
        ):
            r = self.classify({"variant": "SearchReplace", "file_path": raw})
            self.assertEqual(r["reason"], reason, raw)
            self.assertNotIn("project_relative_path", r)

    def test_shell_and_modes(self):
        r = self.classify({"variant": "Bash", "command": "curl " + SECRET})
        self.assertEqual(r["reason"], "shell_command_not_allowed")
        rec = grok_acp.classify_permission_denial(
            request({"variant": "SearchReplace", "file_path": "project/existing.txt"}),
            allow_writes=False, workspace_root=self.root, deny_all_tools=False)
        self.assertEqual(rec["reason"], "writes_not_allowed_in_mode")
        rec = grok_acp.classify_permission_denial(
            request({"variant": "SearchReplace", "file_path": "project/existing.txt"}),
            allow_writes=True, workspace_root=self.root, deny_all_tools=True)
        self.assertEqual(rec["reason"], "deny_all_tools_mode")

    def test_missing_allow_once_and_missing_raw_input(self):
        r = self.classify({"variant": "SearchReplace", "file_path": "project/existing.txt"}, options=("reject_once",))
        self.assertEqual(r["reason"], "no_allow_once_option")
        self.assertEqual(r["option_kinds"], ["reject_once"])
        r = self.classify("not-a-dict")
        self.assertEqual(r["reason"], "raw_input_missing")

    def test_policy_unchanged(self):
        # SearchReplace inside project/ stays allowed; an unobserved variant stays refused.
        ok = grok_acp._permission_allow_once_option(
            request({"variant": "SearchReplace", "file_path": "project/existing.txt"}),
            allow_writes=True, workspace_root=self.root)
        self.assertEqual(ok, "opt-allow_once")
        no = grok_acp._permission_allow_once_option(
            request({"variant": "Delete", "file_path": "project/new.txt"}),
            allow_writes=True, workspace_root=self.root)
        self.assertIsNone(no)


class WritePolicyTests(unittest.TestCase):
    """Observed native Write: {variant, file_path, content}; same path rules as SearchReplace."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        (self.root / "sub").mkdir(parents=True)
        (self.root / "existing.txt").write_text("x", encoding="utf-8")
        (self.root / "adir").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def allow(self, raw, *, allow_writes=True):
        return grok_acp._permission_allow_once_option(request(raw), allow_writes=allow_writes, workspace_root=self.root)

    def test_new_file_and_nested_new_file_allowed(self):
        self.assertEqual(self.allow({"variant": "Write", "file_path": "project/new.txt", "content": "a"}), "opt-allow_once")
        self.assertEqual(self.allow({"variant": "Write", "file_path": "project/sub/deeper/new.py", "content": ""}), "opt-allow_once")

    def test_existing_regular_file_replace_allowed_directory_refused(self):
        self.assertEqual(self.allow({"variant": "Write", "file_path": "project/existing.txt", "content": "b"}), "opt-allow_once")
        self.assertIsNone(self.allow({"variant": "Write", "file_path": "project/adir", "content": "b"}))

    def test_unsafe_paths_refused(self):
        for p in ("C:/x/new.txt", "/tmp/new.txt", "//host/share/new.txt", "project/../new.txt",
                  "other/new.txt", "project/./new.txt", "project/a:b.txt", "project/new.txt.", "project"):
            self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "a"}), p)

    def test_read_only_mode_and_missing_content_refused(self):
        self.assertIsNone(self.allow({"variant": "Write", "file_path": "project/new.txt", "content": "a"}, allow_writes=False))
        self.assertIsNone(self.allow({"variant": "Write", "file_path": "project/new.txt"}))
        self.assertIsNone(self.allow({"variant": "Write", "file_path": "project/new.txt", "content": 5}))

    def test_link_component_refused(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        link = self.root / "linkdir"
        try:
            grok_acp._create_directory_link(outside, link)
        except Exception as exc:  # pragma: no cover - platform without link rights
            self.skipTest(f"cannot create a directory link: {exc}")
        self.assertIsNone(self.allow({"variant": "Write", "file_path": "project/linkdir/new.txt", "content": "a"}))

    def test_control_characters_refused(self):
        for p in ("project/a\x00b.txt", "project/a\nb.txt", "project/a\x7fb.txt"):
            self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "a"}), repr(p))
            self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": p}), repr(p))

    def test_no_variant_targets_repo_metadata_or_broker_scripts(self):
        for ok in ("project/.gitignore", "project/.gitattributes", "project/.github/workflows/ci.yml"):
            self.assertEqual(self.allow({"variant": "Write", "file_path": ok, "content": "a"}), "opt-allow_once", ok)
        for p in ("project/.git/hooks/pre-commit", "project/.git", "project/.GIT/config",
                  "project/.fleet/run.ps1", "project/.Fleet/x.PS1", "project/.fleet/sub/a.cmd"):
            self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "a"}), p)
            self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": p}), p)
        # Fleet evidence remains a legitimate target.
        self.assertEqual(self.allow({"variant": "Write", "file_path": "project/.fleet/evidence/review.md", "content": "e"}), "opt-allow_once")

    def test_more_path_shapes_refused(self):
        for p in ("Project/x.txt", "project//x.txt", "project/nul", "project/CON.txt", "project/sub/lpt1",
                  "project/com9.log"):
            self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "a"}), p)

    def test_hardlinked_file_refused(self):
        import os
        target = self.root / "hl.txt"
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            os.link(outside, target)
        except OSError as exc:  # pragma: no cover
            self.skipTest(f"hard links unavailable: {exc}")
        self.assertIsNone(self.allow({"variant": "Write", "file_path": "project/hl.txt", "content": "a"}))
        self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": "project/hl.txt"}))

    def test_symlink_or_junction_component_refused_without_skip(self):
        # Windows junctions need no privilege, so this vector is always exercised on this host.
        outside = Path(self.tmp.name) / "outside_dir"
        outside.mkdir()
        link = self.root / "jn"
        grok_acp._create_directory_link(outside, link)
        self.assertIsNone(self.allow({"variant": "Write", "file_path": "project/jn/new.txt", "content": "a"}))

    def test_long_project_path_diagnostic_is_unsafe_not_echoed(self):
        long_path = "project/" + "a" * 250 + ".txt"
        rec = grok_acp.classify_permission_denial(
            request({"variant": "Delete", "file_path": long_path}), allow_writes=True,
            workspace_root=self.root, deny_all_tools=False)
        self.assertEqual(rec["path_shape"], "project_relative_unsafe")
        self.assertNotIn("project_relative_path", rec)

    def test_only_allow_once_is_ever_selected(self):
        msg = request({"variant": "Write", "file_path": "project/new.txt", "content": "a"}, options=("allow_always", "reject_once"))
        self.assertIsNone(grok_acp._permission_allow_once_option(msg, allow_writes=True, workspace_root=self.root))


class FailClosedTests(unittest.TestCase):
    def test_nul_path_is_refused_recorded_and_answered(self):
        sent = []

        class Conn(grok_acp._AcpConnection):
            def __init__(self):
                self.deny_all_tools = False
                self.allow_writes = True
                self.workspace_root = Path(tempfile.mkdtemp()) / "project"
                self.workspace_root.mkdir()
                self.state = grok_acp._AcpState(max_tool_calls=4)

            def _send(self, message):
                sent.append(message)

        c = Conn()
        c._handle_reverse_request(request({"variant": "Write", "file_path": "project/a\x00b.txt", "content": "x"}))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["result"]["outcome"]["outcome"], "cancelled")
        self.assertEqual(c.state.permission_requests_denied, 1)
        self.assertEqual(c.state.permission_denials[0]["path_shape"], "control_characters")

    def test_validator_exception_fails_closed(self):
        sent = []

        class Conn(grok_acp._AcpConnection):
            def __init__(self):
                self.deny_all_tools = False
                self.allow_writes = True
                self.workspace_root = Path(tempfile.mkdtemp()) / "project"
                self.workspace_root.mkdir()
                self.state = grok_acp._AcpState(max_tool_calls=4)

            def _send(self, message):
                sent.append(message)

        original = grok_acp._permission_allow_once_option
        grok_acp._permission_allow_once_option = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
        try:
            c = Conn()
            c._handle_reverse_request(request({"variant": "Write", "file_path": "project/ok.txt", "content": "x"}))
        finally:
            grok_acp._permission_allow_once_option = original
        self.assertEqual(sent[0]["result"]["outcome"]["outcome"], "cancelled")
        self.assertEqual(c.state.permission_requests_denied, 1)


class BrokerRecordsTests(unittest.TestCase):
    def test_denial_is_recorded_bounded_and_answer_is_cancelled(self):
        sent = []

        class Conn(grok_acp._AcpConnection):
            def __init__(self):  # bypass process start; only the reverse-request path is exercised
                self.deny_all_tools = False
                self.allow_writes = True
                self.workspace_root = Path(tempfile.mkdtemp()) / "project"
                self.workspace_root.mkdir()
                self.state = grok_acp._AcpState(max_tool_calls=4)

            def _send(self, message):
                sent.append(message)

        c = Conn()
        for _ in range(grok_acp.PERMISSION_DENIAL_RECORD_LIMIT + 5):
            c._handle_reverse_request(request({"variant": "Delete", "file_path": "project/n.txt", "content": SECRET}))
        self.assertEqual(c.state.permission_requests_denied, grok_acp.PERMISSION_DENIAL_RECORD_LIMIT + 5)
        self.assertEqual(len(c.state.permission_denials), grok_acp.PERMISSION_DENIAL_RECORD_LIMIT)
        self.assertTrue(all(m["result"]["outcome"]["outcome"] == "cancelled" for m in sent))
        self.assertNotIn(SECRET, json.dumps(c.state.permission_denials))


if __name__ == "__main__":
    unittest.main()
