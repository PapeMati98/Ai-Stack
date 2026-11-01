import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, "..", "scripts")))

from session_io import (
    ENTRY_STEPS,
    EXIT_STEPS,
    RECORD_OUTCOMES,
    STEP_TIMEOUT_S,
    _clean_cmd_str,
    _compact_detail,
    _resolve_output_dir,
    build_parser,
    default_steps,
    execute,
    main,
    run_step,
)


class FakeResult:
    def __init__(self, rc=0, out="ok", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def ok_runner(argv, **kwargs):
    assert kwargs.get("timeout") == STEP_TIMEOUT_S
    if os.name == "nt":
        assert kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW
    return FakeResult()


class RunStepTests(unittest.TestCase):
    def test_success_records_ok(self):
        step = run_step("probe", ["echo", "hi"], runner=ok_runner)
        self.assertEqual(step["status"], "ok")
        self.assertEqual(step["rc"], 0)
        self.assertEqual(step["name"], "probe")

    def test_missing_binary_is_skipped(self):
        def missing(argv, **kwargs):
            raise FileNotFoundError("nope")

        step = run_step("probe", ["nope"], runner=missing)
        self.assertEqual(step["status"], "skipped")
        self.assertEqual(step["name"], "probe")
        self.assertIn("helper not installed", step["detail"])

    def test_nonzero_rc_is_error(self):
        def fail(argv, **kwargs):
            return FakeResult(rc=3, err="boom")

        step = run_step("probe", ["x"], runner=fail)
        self.assertEqual(step["status"], "error")
        self.assertEqual(step["rc"], 3)
        self.assertEqual(step["name"], "probe")

    def test_timeout_is_error(self):
        def timed_out(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 120))

        step = run_step("probe", ["sleep"], runner=timed_out)
        self.assertEqual(step["status"], "error")
        self.assertIn("timed out", step["detail"])

    def test_compact_detail_handles_json_and_truncation(self):
        json_payload = json.dumps({"status": "ok", "receipt_id": "rcpt-123456", "huge": "x" * 2000})
        self.assertEqual(_compact_detail(json_payload), "rcpt-123456")
        plain_text = "Line 1 summary\nLine 2 details\nLine 3 extra"
        self.assertEqual(_compact_detail(plain_text), "Line 1 summary")
        long_line = "A" * 300
        self.assertEqual(len(_compact_detail(long_line, max_chars=150)), 150)


class ExecuteMockedTests(unittest.TestCase):
    def test_entry_and_exit_shapes_all_ok(self):
        entry_args = {
            "task": "shortlist skills",
            "project_slug": "test-slug",
            "harness": "codex",
            "session_id": "test-sid",
        }
        entry_payload = execute("entry", entry_args, runner=ok_runner)
        self.assertEqual(entry_payload["status"], "OK")
        self.assertEqual(entry_payload["command"], "entry")
        self.assertIn("elapsed_ms", entry_payload)
        self.assertEqual(
            [s["name"] for s in entry_payload["steps"]],
            [name for name, _ in default_steps("entry", entry_args)],
        )
        self.assertTrue(all(s["status"] == "ok" for s in entry_payload["steps"]))

        exit_args = {
            "harness": "codex",
            "session_id": "test-sid",
            "outcome": "COMPLETED",
            "project_slug": "test-slug",
        }
        exit_payload = execute("exit", exit_args, runner=ok_runner)
        self.assertEqual(exit_payload["status"], "OK")
        self.assertEqual(exit_payload["command"], "exit")
        self.assertTrue(all(s["status"] == "ok" for s in exit_payload["steps"]))

    def test_mixed_statuses_degrades_summary(self):
        calls = {"n": 0}

        def flaky(argv, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FileNotFoundError("gone")
            return FakeResult()

        args = {
            "task": "some task",
            "project_slug": "test-slug",
            "harness": "codex",
            "session_id": "test-sid",
        }
        payload = execute("entry", args, runner=flaky)
        # When any step is skipped, overall status MUST be DEGRADED, not OK
        self.assertEqual(payload["status"], "DEGRADED")
        self.assertEqual(payload["steps"][0]["status"], "skipped")
        self.assertEqual(payload["steps"][1]["status"], "ok")

    def test_all_skipped_degrades_summary(self):
        def all_missing(argv, **kwargs):
            raise FileNotFoundError("missing")

        args = {
            "task": "some task",
            "project_slug": "test-slug",
            "harness": "codex",
            "session_id": "test-sid",
        }
        payload = execute("entry", args, runner=all_missing)
        self.assertEqual(payload["status"], "DEGRADED")
        self.assertTrue(all(s["status"] == "skipped" for s in payload["steps"]))

    def test_error_status_overrides_skipped(self):
        calls = {"n": 0}

        def err_and_skip(argv, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FileNotFoundError("missing")
            return FakeResult(rc=1, err="failed step")

        args = {
            "task": "some task",
            "project_slug": "test-slug",
            "harness": "codex",
            "session_id": "test-sid",
        }
        payload = execute("entry", args, runner=err_and_skip)
        self.assertEqual(payload["status"], "ERROR")

    def test_unsupported_goal_is_not_forwarded(self):
        args = {
            "goal": "legacy-goal-text",
            "harness": "codex",
            "session_id": "test-sid",
        }
        steps = default_steps("entry", args)
        checkpoint_step = [s for s in steps if s[0] == "checkpoint"][0]
        argv = checkpoint_step[1]
        self.assertNotIn("--goal", argv)
        self.assertNotIn("legacy-goal-text", argv)

    def test_missing_identity_records_skipped(self):
        # Empty args and no env vars
        old_env = {k: os.environ.pop(k, None) for k in [
            "COORDFS_HARNESS", "AGENT_HARNESS", "AGENT_ID",
            "COORDFS_SESSION_ID", "AGENT_SESSION_ID", "SESSION_ID"
        ]}
        try:
            steps = default_steps("entry", {})
            self.assertEqual(len(steps), 1)
            name, argv = steps[0]
            self.assertEqual(name, "checkpoint")
            self.assertIsNone(argv)

            step = run_step(name, argv)
            self.assertEqual(step["status"], "skipped")
            self.assertIn("missing required identity", step["detail"])
        finally:
            for k, v in old_env.items():
                if v is not None:
                    os.environ[k] = v


class SubprocessFixtureTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = self.td.name

        # Create tiny executable Python fixtures
        self.cat_log = os.path.join(self.root, "cat.log")
        self.cat_py = os.path.join(self.root, "mock_skill_catalog.py")
        with open(self.cat_py, "w", encoding="utf-8") as f:
            f.write(
                "import os, sys, json\n"
                f"with open(r'{self.cat_log}', 'w', encoding='utf-8') as f:\n"
                "    json.dump(sys.argv[1:], f)\n"
                "print('shortlist ready: select-skills')\n"
            )

        self.knowledge_log = os.path.join(self.root, "knowledge.log")
        self.knowledge_py = os.path.join(self.root, "mock_knowledge.py")
        with open(self.knowledge_py, "w", encoding="utf-8") as f:
            f.write(
                "import os, sys, json\n"
                f"with open(r'{self.knowledge_log}', 'w', encoding='utf-8') as f:\n"
                "    json.dump(sys.argv[1:], f)\n"
                "sub = sys.argv[1] if len(sys.argv) > 1 else ''\n"
                "if sub == 'begin-task':\n"
                "    print(json.dumps({'status': 'ok', 'receipt_id': 'rcpt-begin-001'}))\n"
                "elif sub == 'verify-context':\n"
                "    print(json.dumps({'status': 'ok', 'receipt_id': 'rcpt-verified'}))\n"
                "elif sub == 'record-session':\n"
                "    print(json.dumps({'status': 'ok', 'checkpoint_id': 'cp-closeout-001'}))\n"
                "else:\n"
                "    print(json.dumps({'status': 'ok'}))\n"
            )

        self.office_log = os.path.join(self.root, "office.log")
        self.office_py = os.path.join(self.root, "mock_office.py")
        with open(self.office_py, "w", encoding="utf-8") as f:
            f.write(
                "import os, sys, json\n"
                f"with open(r'{self.office_log}', 'w', encoding='utf-8') as f:\n"
                "    json.dump(sys.argv[1:], f)\n"
                "print(json.dumps({'status': 'ok', 'checkpoint': 'renewed'}))\n"
            )

    def tearDown(self):
        self.td.cleanup()

    def test_real_subprocess_entry_with_spaces_and_unicode(self):
        workspace = os.path.join(self.root, "Przestrzeń robocza z spacjami")
        os.makedirs(workspace, exist_ok=True)
        task = "Dobierz narzędzia dla zadania — część 1"
        project_slug = "projekt-główny-żółć"
        channel = "kanał-ogólny 1"
        harness = "test-harness"
        session_id = "test-sess-001"

        args = [
            "entry",
            "--task", task,
            "--project-slug", project_slug,
            "--workspace", workspace,
            "--channel", channel,
            "--harness", harness,
            "--session-id", session_id,
            "--skill-catalog-bin", self.cat_py,
            "--knowledge-bin", self.knowledge_py,
            "--office-bin", self.office_py,
        ]
        parsed = build_parser().parse_args(args)
        payload = execute("entry", parsed)

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(len(payload["steps"]), 3)
        self.assertTrue(all(s["status"] == "ok" for s in payload["steps"]))

        # Verify skill catalog was invoked correctly with Unicode and spaces
        with open(self.cat_log, "r", encoding="utf-8") as f:
            cat_argv = json.load(f)
        self.assertEqual(cat_argv, ["--task", task, "--compact"])

        # Verify knowledge begin-task was invoked correctly
        with open(self.knowledge_log, "r", encoding="utf-8") as f:
            kb_argv = json.load(f)
        self.assertEqual(kb_argv, [
            "begin-task",
            "--agent", harness,
            "--session-id", session_id,
            "--project-slug", project_slug,
            "--require-context",
        ])

        # Verify office checkpoint was invoked correctly
        with open(self.office_log, "r", encoding="utf-8") as f:
            off_argv = json.load(f)
        self.assertEqual(off_argv, [
            "--harness", harness,
            "--session-id", session_id,
            "checkpoint",
            "--channel", channel,
            "--workspace", workspace,
            "--ttl", "900",
        ])

    def test_real_subprocess_entry_receipt_reuse(self):
        receipt = "receipt-existing-999"
        args = [
            "entry",
            "--project-slug", "my-project",
            "--context-receipt", receipt,
            "--harness", "codex",
            "--session-id", "sess-777",
            "--knowledge-bin", self.knowledge_py,
            "--office-bin", self.office_py,
        ]
        parsed = build_parser().parse_args(args)
        payload = execute("entry", parsed)

        self.assertEqual(payload["status"], "OK")
        # Verify verify-context was called instead of begin-task
        with open(self.knowledge_log, "r", encoding="utf-8") as f:
            kb_argv = json.load(f)
        self.assertEqual(kb_argv, [
            "verify-context",
            "--context-receipt", receipt,
            "--agent", "codex",
            "--session-id", "sess-777",
            "--project-slug", "my-project",
        ])

    def test_real_subprocess_exit_success(self):
        title = "Zamknięcie sesji #1"
        notes = "Podsumowanie z polskimi znakami: ąęćśź i spacjami"
        args = [
            "exit",
            "--outcome", "COMPLETED",
            "--title", title,
            "--notes", notes,
            "--project-slug", "projekt-exit",
            "--context-receipt", "receipt-exit-123",
            "--harness", "codex",
            "--session-id", "sess-exit-888",
            "--knowledge-bin", self.knowledge_py,
            "--office-bin", self.office_py,
        ]
        parsed = build_parser().parse_args(args)
        payload = execute("exit", parsed)

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(len(payload["steps"]), 2)
        self.assertTrue(all(s["status"] == "ok" for s in payload["steps"]))

        with open(self.knowledge_log, "r", encoding="utf-8") as f:
            kb_argv = json.load(f)
        self.assertEqual(kb_argv, [
            "record-session",
            "--agent", "codex",
            "--session-id", "sess-exit-888",
            "--checkpoint-id", title,
            "--outcome", "COMPLETED",
            "--project-slug", "projekt-exit",
            "--summary", notes,
            "--context-receipt", "receipt-exit-123",
        ])

    def test_real_subprocess_missing_tools_is_degraded(self):
        missing_bin = os.path.join(self.root, "does_not_exist.py")
        args = [
            "entry",
            "--task", "some task",
            "--project-slug", "some-proj",
            "--harness", "codex",
            "--session-id", "sess-1",
            "--skill-catalog-bin", missing_bin,
            "--knowledge-bin", missing_bin,
            "--office-bin", missing_bin,
        ]
        parsed = build_parser().parse_args(args)
        payload = execute("entry", parsed)

        self.assertEqual(payload["status"], "DEGRADED")
        for step in payload["steps"]:
            self.assertEqual(step["status"], "skipped")
            self.assertIn("helper not installed", step["detail"])

    def test_real_subprocess_failure_is_error(self):
        fail_py = os.path.join(self.root, "fail_office.py")
        with open(fail_py, "w", encoding="utf-8") as f:
            f.write("import sys\nsys.stderr.write('Fatal Office crash error')\nsys.exit(2)\n")

        args = [
            "entry",
            "--harness", "codex",
            "--session-id", "sess-1",
            "--office-bin", fail_py,
        ]
        parsed = build_parser().parse_args(args)
        payload = execute("entry", parsed)

        self.assertEqual(payload["status"], "ERROR")
        step = payload["steps"][0]
        self.assertEqual(step["status"], "error")
        self.assertIn("Fatal Office crash error", step["detail"])

    def test_main_cli_exit_codes(self):
        # 1. OK returns 0
        rc_ok = main([
            "entry",
            "--task", "task",
            "--harness", "codex",
            "--session-id", "sess-1",
            "--skill-catalog-bin", self.cat_py,
            "--office-bin", self.office_py,
        ])
        self.assertEqual(rc_ok, 0)

        # 2. DEGRADED returns 0 (fail-open)
        missing_bin = os.path.join(self.root, "missing.py")
        rc_degraded = main([
            "entry",
            "--task", "task",
            "--harness", "codex",
            "--session-id", "sess-1",
            "--skill-catalog-bin", missing_bin,
            "--office-bin", missing_bin,
        ])
        self.assertEqual(rc_degraded, 0)

        # 3. ERROR returns 1
        fail_py = os.path.join(self.root, "fail_cat.py")
        with open(fail_py, "w", encoding="utf-8") as f:
            f.write("import sys\nsys.exit(1)\n")

        rc_err = main([
            "entry",
            "--task", "task",
            "--harness", "codex",
            "--session-id", "sess-1",
            "--skill-catalog-bin", fail_py,
            "--office-bin", self.office_py,
        ])
        self.assertEqual(rc_err, 1)

    def test_cmd_newlines_and_spaces_sanitized(self):
        cleaned = _clean_cmd_str("multi\r\nline\ntext  ")
        self.assertEqual(cleaned, "multi  line text")
        self.assertNotIn("\n", cleaned)
        self.assertNotIn("\r", cleaned)

    def test_real_subprocess_missing_identity_is_degraded(self):
        old_env = {k: os.environ.pop(k, None) for k in [
            "COORDFS_HARNESS", "AGENT_HARNESS", "AGENT_ID",
            "COORDFS_SESSION_ID", "AGENT_SESSION_ID", "SESSION_ID"
        ]}
        try:
            args = [
                "entry",
                "--task", "task without identity",
                "--skill-catalog-bin", self.cat_py,
                "--office-bin", self.office_py,
            ]
            parsed = build_parser().parse_args(args)
            payload = execute("entry", parsed)

            self.assertEqual(payload["status"], "DEGRADED")
            cat_step = [s for s in payload["steps"] if s["name"] == "skill_catalog"][0]
            cp_step = [s for s in payload["steps"] if s["name"] == "checkpoint"][0]
            self.assertEqual(cat_step["status"], "ok")
            self.assertEqual(cp_step["status"], "skipped")
            self.assertIn("missing required identity", cp_step["detail"])
        finally:
            for k, v in old_env.items():
                if v is not None:
                    os.environ[k] = v

    def test_real_subprocess_exit_missing_outcome_is_degraded(self):
        args = [
            "exit",
            "--harness", "codex",
            "--session-id", "sess-1",
            "--knowledge-bin", self.knowledge_py,
            "--office-bin", self.office_py,
        ]
        parsed = build_parser().parse_args(args)
        payload = execute("exit", parsed)

        self.assertEqual(payload["status"], "DEGRADED")
        rec_step = [s for s in payload["steps"] if s["name"] == "record_session"][0]
        self.assertEqual(rec_step["status"], "skipped")
        self.assertIn("missing required identity or parameters", rec_step["detail"])


class OutputRetentionTests(unittest.TestCase):
    """Real subprocess fixtures: a consumed response must stay retrievable."""

    BODY_MARKER = "UNTRUSTED-BODY-marker-7f3a9c"

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = self.td.name
        # Task-owned private output directory with spaces and non-ASCII characters.
        self.output_dir = os.path.join(self.root, "katalog wyników prywatnych 1")
        os.makedirs(self.output_dir, exist_ok=True)

        # Office-shaped response: the cursor advances and the only place the
        # message body exists is the raw helper output.
        self.office_py = os.path.join(self.root, "mock_office_cursor.py")
        office_payload = {
            "status": "ok",
            "checkpoint": "renewed",
            "cursor": "cur-000123",
            "next_cursor": "cur-000124",
            "messages": [
                {
                    "id": "synthetic-message-123",
                    "from": "synthetic-sender",
                    "body": self.BODY_MARKER + " cursor advanced",
                }
            ],
        }
        with open(self.office_py, "w", encoding="utf-8") as f:
            f.write("import json\n")
            f.write("print(json.dumps(" + json.dumps(office_payload, ensure_ascii=False) + "))\n")

        # Hub begin-task response: full receipt reference, not only a receipt id.
        self.receipt_file = os.path.join(self.root, "odbiory kontekstu", "rcpt-begin-001.json")
        os.makedirs(os.path.dirname(self.receipt_file), exist_ok=True)
        with open(self.receipt_file, "w", encoding="utf-8") as f:
            json.dump({"receipt_id": "rcpt-begin-001", "context": {"files": ["a.md"]}}, f)
        self.knowledge_py = os.path.join(self.root, "mock_knowledge_receipt.py")
        knowledge_payload = {
            "status": "ok",
            "receipt_id": "rcpt-begin-001",
            "receipt_path": self.receipt_file,
            "context": {"files": ["a.md"], "brief": "pełny wynik z Hub"},
        }
        with open(self.knowledge_py, "w", encoding="utf-8") as f:
            f.write("import json\n")
            f.write("print(json.dumps(" + json.dumps(knowledge_payload, ensure_ascii=False) + "))\n")

    def tearDown(self):
        self.td.cleanup()

    def _entry_args(self):
        return [
            "entry",
            "--project-slug", "projekt-retencji",
            "--harness", "codex",
            "--session-id", "sess-retencja-001",
            "--channel", "kanał testowy",
            "--knowledge-bin", self.knowledge_py,
            "--office-bin", self.office_py,
            "--output-dir", self.output_dir,
        ]

    def test_consumed_office_response_stays_retrievable(self):
        parsed = build_parser().parse_args(self._entry_args())
        payload = execute("entry", parsed)

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["output_dir"], os.path.abspath(self.output_dir))

        office_step = [s for s in payload["steps"] if s["name"] == "checkpoint"][0]
        self.assertEqual(office_step["status"], "ok")
        self.assertIn("output_path", office_step)

        # The path itself is usable on a filesystem with spaces and non-ASCII.
        output_path = office_step["output_path"]
        self.assertTrue(os.path.isfile(output_path))
        self.assertIn("katalog wyników prywatnych 1", output_path)

        with open(output_path, "r", encoding="utf-8") as f:
            envelope = json.load(f)
        self.assertEqual(envelope["treat_as"], "untrusted-data")
        self.assertEqual(envelope["returncode"], 0)
        # The consumed message is fully retrievable from the retained artifact.
        retained = json.loads(envelope["stdout"])
        self.assertEqual(retained["messages"][0]["id"], "synthetic-message-123")
        self.assertIn(self.BODY_MARKER, retained["messages"][0]["body"])
        self.assertEqual(retained["cursor"], "cur-000123")
        # Actual canonical argv is recorded for the caller.
        self.assertIn("checkpoint", envelope["argv"])
        self.assertIn("sess-retencja-001", envelope["argv"])
        self.assertIn("kanał testowy", envelope["argv"])

        # Compact summary: safe metadata only, no message body.
        self.assertEqual(office_step["result"]["message_count"], 1)
        self.assertEqual(office_step["result"]["message_ids"], ["synthetic-message-123"])
        self.assertNotIn(self.BODY_MARKER, json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("cursor", office_step["result"])

    def test_hub_receipt_reference_is_preserved(self):
        parsed = build_parser().parse_args(self._entry_args())
        payload = execute("entry", parsed)

        brief_step = [s for s in payload["steps"] if s["name"] == "project_brief"][0]
        self.assertEqual(brief_step["status"], "ok")
        self.assertEqual(brief_step["receipt_id"], "rcpt-begin-001")
        self.assertEqual(brief_step["receipt_path"], self.receipt_file)
        self.assertTrue(os.path.isfile(brief_step["receipt_path"]))

        with open(brief_step["output_path"], "r", encoding="utf-8") as f:
            envelope = json.load(f)
        retained = json.loads(envelope["stdout"])
        self.assertEqual(retained["receipt_path"], self.receipt_file)
        self.assertEqual(retained["context"]["brief"], "pełny wynik z Hub")

    def test_retention_failure_is_not_a_silent_ok(self):
        blocker = os.path.join(self.root, "plik-blokada")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("not a directory\n")
        broken_dir = os.path.join(blocker, "pod")

        args = self._entry_args()
        args[args.index("--output-dir") + 1] = broken_dir
        parsed = build_parser().parse_args(args)
        payload = execute("entry", parsed)

        self.assertEqual(payload["status"], "ERROR")
        for step in payload["steps"]:
            self.assertEqual(step["status"], "error")
            self.assertIn("output retention failed", step["detail"])
            self.assertFalse(step["output_retained"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 1)

    def test_receipt_id_survives_a_failing_helper(self):
        fail_py = os.path.join(self.root, "mock_office_fail.py")
        with open(fail_py, "w", encoding="utf-8") as f:
            f.write(
                "import json, sys\n"
                "print(json.dumps({'status': 'error', 'receipt_id': 'rcpt-partial-42'}))\n"
                "sys.stderr.write('helper failed after consuming the response')\n"
                "sys.exit(2)\n"
            )
        args = self._entry_args()
        args[args.index("--office-bin") + 1] = fail_py
        parsed = build_parser().parse_args(args)
        payload = execute("entry", parsed)

        self.assertEqual(payload["status"], "ERROR")
        office_step = [s for s in payload["steps"] if s["name"] == "checkpoint"][0]
        self.assertEqual(office_step["status"], "error")
        self.assertEqual(office_step["receipt_id"], "rcpt-partial-42")
        self.assertIn("output_path", office_step)
        with open(office_step["output_path"], "r", encoding="utf-8") as f:
            envelope = json.load(f)
        self.assertEqual(envelope["returncode"], 2)
        self.assertIn("helper failed", envelope["stderr"])

    def test_default_output_dir_is_private_and_session_scoped(self):
        parsed = build_parser().parse_args([
            "entry",
            "--harness", "codex",
            "--session-id", "sess-default-42",
        ])
        default_dir = _resolve_output_dir(parsed)
        home = os.path.expanduser("~")
        self.assertIn(os.path.join(".agents", "private-artifacts", "session-io"), default_dir)
        self.assertTrue(default_dir.startswith(os.path.abspath(home)))
        self.assertIn("sess-default-42", default_dir)
        self.assertIn("codex", default_dir)
        # Never inside the current project/repo checkout.
        self.assertFalse(os.path.abspath(default_dir).startswith(os.path.abspath(os.getcwd())))

        explicit = build_parser().parse_args([
            "entry",
            "--harness", "codex",
            "--session-id", "sess-default-42",
            "--output-dir", self.output_dir,
        ])
        self.assertEqual(_resolve_output_dir(explicit), os.path.abspath(self.output_dir))

    def test_real_run_without_output_dir_uses_private_default(self):
        args = [
            "entry",
            "--harness", "codex",
            "--session-id", "sess-default-artifact-777",
            "--office-bin", self.office_py,
        ]
        parsed = build_parser().parse_args(args)
        payload = execute("entry", parsed)

        self.assertEqual(payload["status"], "OK")
        expected_dir = _resolve_output_dir(parsed)
        self.assertEqual(payload["output_dir"], expected_dir)
        self.assertTrue(expected_dir.startswith(os.path.abspath(os.path.expanduser("~"))))
        self.assertFalse(expected_dir.startswith(os.path.abspath(os.getcwd())))

        office_step = [s for s in payload["steps"] if s["name"] == "checkpoint"][0]
        self.assertTrue(office_step["output_path"].startswith(expected_dir))
        self.assertTrue(os.path.isfile(office_step["output_path"]))
        with open(office_step["output_path"], "r", encoding="utf-8") as f:
            envelope = json.load(f)
        self.assertIn("synthetic-message-123", envelope["stdout"])


if __name__ == "__main__":
    unittest.main()
