"""Red regression tests: caretaker publication native timeout mismatch.

Covers: ${native_timeout_seconds} placeholder derived below outer budget,
legacy literal argv, invalid budgets, structured-timeout-only native_timeout,
malformed envelopes, identity preservation, sanitized publication reason,
two-failure breaker. No live calls; all runners injected.
"""
import hashlib
import json
import secrets
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from caretaker.config import TransportSpec
from caretaker import tick_budget
from caretaker.transports import OpenClawArgvTransport, TransportError


def _synthetic_test_credential():
    return "synthetic-test-only-" + secrets.token_hex(16)


def _write_source(vault):
    folder = vault / "raw" / "sources" / "2026" / "10" / "src-1"
    folder.mkdir(parents=True, exist_ok=True)
    data = b"# Session note\n\nA plain synthetic note about the project.\n"
    meta = {"source_id": "src-1", "classification": "internal",
            "local_only": False, "original_file": "original.md",
            "sha256": hashlib.sha256(data).hexdigest(), "byte_length": len(data)}
    (folder / "source.json").write_bytes(json.dumps(meta).encode("utf-8"))
    (folder / "original.md").write_bytes(data)


def _write_publication_config(directory):
    root = Path(directory)
    cfg = {
        "schema_version": 1, "project_slug": "one", "agent": "hub-caretaker",
        "session_id": "fixture-session", "state_dir": str(root / "state"),
        "inputs": [{"path": str(root / "events.jsonl"),
                    "kind": "normalized_events", "tool": "fixture"}],
        "allowlist": {"tools": ["fixture"]},
        "primary": {"kind": "openclaw_argv", "executable": str(root / "inert"),
                    "argv": ["fixture"]},
        "hub": {"python_executable": str(root / "inert"),
                "script": str(root / "hub.py"), "vault": str(root / "vault"),
                "context_receipt": str(root / "receipt.json")},
        "office": {"python_executable": str(root / "inert"),
                   "script": str(root / "office.py"), "home": str(root / "office"),
                   "harness": str(root / "harness"), "host": str(root / "host"),
                   "session_id": str(root / "session")},
    }
    config_path = root / "caretaker.json"
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    return config_path, cfg


def _spec(argv, executable="openclaw.cmd"):
    return TransportSpec(kind="openclaw_argv", executable=executable,
                         argv=argv)


def _ok_envelope(**over):
    base = {"ok": True, "status": "ok", "final": "{}",
            "model": "Qwen3.8-27B", "provider": "vllm"}
    base.update(over)
    return json.dumps(base)


def _timeout_envelope():
    return json.dumps({"ok": False, "status": "timeout", "final": "{}",
                       "model": "Qwen3.8-27B", "provider": "vllm",
                       "error": "timedOutByRunBudget"})


class PlaceholderBudgetTests(unittest.TestCase):
    def test_primary_placeholder_derived_below_outer_budget(self):
        seen = {}
        def runner(*, executable, argv, cwd, prompt, timeout):
            seen["argv"] = list(argv)
            seen["timeout"] = timeout
            return 0, _ok_envelope(), ""
        t = OpenClawArgvTransport(run_callable=runner)
        spec = _spec(["agent", "exec", "--timeout",
                      "${native_timeout_seconds}", "--json"])
        t.invoke(spec=spec, cwd="c", prompt="hello",
                 run_config_path="r", state_dir="s", timeout=360)
        self.assertEqual(seen["timeout"], 360)
        idx = seen["argv"].index("--timeout") + 1
        child = int(seen["argv"][idx])
        self.assertLess(child, 360)
        self.assertGreaterEqual(child, 1)
        self.assertEqual(child, 330)  # documented 30s cleanup margin

    def test_fallback_placeholder_derived_below_outer_budget(self):
        seen = {}
        def runner(*, executable, argv, cwd, prompt, timeout):
            seen["argv"] = list(argv)
            return 0, _ok_envelope(model="muse-spark-1.3",
                                   provider="muse-code"), ""
        t = OpenClawArgvTransport(run_callable=runner)
        spec = _spec(["agent", "exec", "--timeout",
                      "${native_timeout_seconds}", "--json"])
        t.invoke(spec=spec, cwd="c", prompt="hello",
                 run_config_path="r", state_dir="s", timeout=120)
        idx = seen["argv"].index("--timeout") + 1
        self.assertEqual(int(seen["argv"][idx]), 110)  # documented 10s margin

    def test_legacy_literal_argv_preserved(self):
        seen = {}
        def runner(*, executable, argv, cwd, prompt, timeout):
            seen["argv"] = list(argv)
            return 0, _ok_envelope(), ""
        t = OpenClawArgvTransport(run_callable=runner)
        spec = _spec(["agent", "exec", "--timeout", "120", "--json"])
        t.invoke(spec=spec, cwd="c", prompt="hello",
                 run_config_path="r", state_dir="s", timeout=360)
        idx = seen["argv"].index("--timeout") + 1
        self.assertEqual(seen["argv"][idx], "120")

    def test_arbitrary_tokens_not_rewritten(self):
        seen = {}
        def runner(*, executable, argv, cwd, prompt, timeout):
            seen["argv"] = list(argv)
            return 0, _ok_envelope(), ""
        t = OpenClawArgvTransport(run_callable=runner)
        spec = _spec(["agent", "exec", "${timeout}", "$timeout",
                      "--timeout", "120"])
        t.invoke(spec=spec, cwd="c", prompt="hello",
                 run_config_path="r", state_dir="s", timeout=360)
        self.assertIn("${timeout}", seen["argv"])
        self.assertIn("$timeout", seen["argv"])

    def test_invalid_budgets_rejected_before_spawn(self):
        for bad in (0, -5, "360", 3.5, True, None, float("inf")):
            calls = []
            def runner(*, executable, argv, cwd, prompt, timeout):
                calls.append(timeout)
                return 0, _ok_envelope(), ""
            t = OpenClawArgvTransport(run_callable=runner)
            spec = _spec(["agent", "exec", "--timeout",
                          "${native_timeout_seconds}"])
            with self.subTest(bad=bad):
                with self.assertRaises(TransportError) as cm:
                    t.invoke(spec=spec, cwd="c", prompt="hello",
                             run_config_path="r", state_dir="s",
                             timeout=bad)
                self.assertEqual(cm.exception.reason_code, "invalid_budget")
                self.assertEqual(calls, [])


class ReasonCodeTests(unittest.TestCase):
    def test_budget_clipped_outer_timeout_is_cooperative_and_marked(self):
        import subprocess
        def runner(*, executable, argv, cwd, prompt, timeout):
            raise subprocess.TimeoutExpired(cmd=executable, timeout=timeout)
        budget = tick_budget.TickBudget(seconds=40, clock=lambda: 0.0)
        transport = OpenClawArgvTransport(run_callable=runner)
        with budget.activate():
            with self.assertRaises(tick_budget.TickBudgetExceeded) as cm:
                transport.invoke(spec=_spec(["agent"]), cwd="c", prompt="hello",
                                 run_config_path="r", state_dir="s", timeout=120)
        self.assertIs(getattr(cm.exception, "_caretaker_budget_clipped_timeout", False), True)

    def test_unclipped_outer_timeout_remains_transport_failure(self):
        import subprocess
        def runner(*, executable, argv, cwd, prompt, timeout):
            raise subprocess.TimeoutExpired(cmd=executable, timeout=timeout)
        budget = tick_budget.TickBudget(seconds=240, clock=lambda: 0.0)
        transport = OpenClawArgvTransport(run_callable=runner)
        with budget.activate():
            with self.assertRaises(TransportError) as cm:
                transport.invoke(spec=_spec(["agent"]), cwd="c", prompt="hello",
                                 run_config_path="r", state_dir="s", timeout=120)
        self.assertEqual(cm.exception.reason_code, "process_timeout")

    def test_outer_clipping_does_not_promote_unchanged_native_timeout(self):
        seen = {}

        def runner(*, executable, argv, cwd, prompt, timeout):
            seen["argv"] = list(argv)
            seen["timeout"] = timeout
            return 0, _timeout_envelope(), ""

        budget = tick_budget.TickBudget(seconds=40, clock=lambda: 0.0)
        transport = OpenClawArgvTransport(run_callable=runner)
        with budget.activate():
            with self.assertRaises(TransportError) as cm:
                transport.invoke(
                    spec=_spec(["agent", "exec", "--timeout", "10", "--json"]),
                    cwd="c", prompt="hello", run_config_path="r",
                    state_dir="s", timeout=120)

        self.assertEqual(cm.exception.reason_code, "native_timeout")
        self.assertEqual(seen["timeout"], 35)
        self.assertEqual(seen["argv"][seen["argv"].index("--timeout") + 1], "10")

    def test_native_timeout_clipped_by_tick_is_cooperative(self):
        seen = {}

        def runner(*, executable, argv, cwd, prompt, timeout):
            seen["argv"] = list(argv)
            seen["timeout"] = timeout
            return 0, _timeout_envelope(), ""

        budget = tick_budget.TickBudget(seconds=40, clock=lambda: 0.0)
        transport = OpenClawArgvTransport(run_callable=runner)
        with budget.activate():
            with self.assertRaises(tick_budget.TickBudgetExceeded) as cm:
                transport.invoke(
                    spec=_spec(["agent", "exec", "--timeout", "100", "--json"]),
                    cwd="c", prompt="hello", run_config_path="r",
                    state_dir="s", timeout=120)

        self.assertIs(getattr(cm.exception, "_caretaker_budget_clipped_timeout", False), True)
        self.assertEqual(seen["timeout"], 35)
        self.assertEqual(seen["argv"][seen["argv"].index("--timeout") + 1], "25")

    def test_timeout_envelope_with_nonzero_exit_is_native_timeout(self):
        def runner(*, executable, argv, cwd, prompt, timeout):
            return 1, _timeout_envelope(), "exit 1 noise"
        t = OpenClawArgvTransport(run_callable=runner)
        with self.assertRaises(TransportError) as cm:
            t.invoke(spec=_spec(["agent"]), cwd="c", prompt="hello",
                     run_config_path="r", state_dir="s", timeout=120)
        self.assertEqual(cm.exception.reason_code, "native_timeout")

    def test_timeout_envelope_exit_zero_is_native_timeout(self):
        def runner(*, executable, argv, cwd, prompt, timeout):
            return 0, _timeout_envelope(), ""
        t = OpenClawArgvTransport(run_callable=runner)
        with self.assertRaises(TransportError) as cm:
            t.invoke(spec=_spec(["agent"]), cwd="c", prompt="hello",
                     run_config_path="r", state_dir="s", timeout=120)
        self.assertEqual(cm.exception.reason_code, "native_timeout")

    def test_nonzero_exit_without_timeout_envelope(self):
        def runner(*, executable, argv, cwd, prompt, timeout):
            return 3, "{}", "boom"
        t = OpenClawArgvTransport(run_callable=runner)
        with self.assertRaises(TransportError) as cm:
            t.invoke(spec=_spec(["agent"]), cwd="c", prompt="hello",
                     run_config_path="r", state_dir="s", timeout=120)
        self.assertEqual(cm.exception.reason_code, "nonzero_exit")

    def test_process_timeout_maps_to_process_timeout(self):
        import subprocess
        def runner(*, executable, argv, cwd, prompt, timeout):
            raise subprocess.TimeoutExpired(cmd=executable, timeout=timeout)
        t = OpenClawArgvTransport(run_callable=runner)
        with self.assertRaises(TransportError) as cm:
            t.invoke(spec=_spec(["agent"]), cwd="c", prompt="hello",
                     run_config_path="r", state_dir="s", timeout=120)
        self.assertEqual(cm.exception.reason_code, "process_timeout")

    def test_non_json_stdout_is_invalid_envelope(self):
        def runner(*, executable, argv, cwd, prompt, timeout):
            return 0, "not json at all", ""
        t = OpenClawArgvTransport(run_callable=runner)
        with self.assertRaises(TransportError) as cm:
            t.invoke(spec=_spec(["agent"]), cwd="c", prompt="hello",
                     run_config_path="r", state_dir="s", timeout=120)
        self.assertEqual(cm.exception.reason_code, "invalid_envelope")

    def test_non_dict_stdout_safely_fails(self):
        for body in ('[1,2]', '"str"', '42', 'null'):
            def runner(*, executable, argv, cwd, prompt, timeout,
                       _b=body):
                return 0, _b, ""
            t = OpenClawArgvTransport(run_callable=runner)
            with self.subTest(body=body):
                with self.assertRaises(TransportError) as cm:
                    t.invoke(spec=_spec(["agent"]), cwd="c", prompt="x",
                             run_config_path="r", state_dir="s", timeout=120)
                self.assertEqual(cm.exception.reason_code,
                                 "invalid_envelope")

    def test_identity_mismatch_code(self):
        def runner(*, executable, argv, cwd, prompt, timeout):
            return 0, _ok_envelope(model="other", provider="other"), ""
        t = OpenClawArgvTransport(run_callable=runner,
                                  expected_identity=("vllm", "Qwen3.8-27B"))
        with self.assertRaises(TransportError) as cm:
            t.invoke(spec=_spec(["agent"]), cwd="c", prompt="hello",
                     run_config_path="r", state_dir="s", timeout=120)
        self.assertEqual(cm.exception.reason_code, "identity_mismatch")

    def test_no_inference_from_assistant_text(self):
        seen = {}
        def runner(*, executable, argv, cwd, prompt, timeout):
            seen["ok"] = True
            return 0, _ok_envelope(
                final='{"note":"timedOutByRunBudget timeout in prose"}'), ""
        t = OpenClawArgvTransport(run_callable=runner)
        env = t.invoke(spec=_spec(["agent"]), cwd="c", prompt="hello",
                       run_config_path="r", state_dir="s", timeout=120)
        self.assertTrue(seen["ok"])
        self.assertEqual(env.status, "ok")

    def test_secret_bearing_stderr_not_leaked(self):
        secret = "".join(("sk", "-ant-", _synthetic_test_credential()))
        def runner(*, executable, argv, cwd, prompt, timeout):
            return 2, "{}", f"token {secret} leaked?"
        t = OpenClawArgvTransport(run_callable=runner)
        with self.assertRaises(TransportError) as cm:
            t.invoke(spec=_spec(["agent"]), cwd="c", prompt="hello",
                     run_config_path="r", state_dir="s", timeout=120)
        self.assertEqual(cm.exception.reason_code, "nonzero_exit")
        self.assertNotIn(secret, str(cm.exception))
        self.assertNotIn(secret, repr(cm.exception))


class PublicationSanitizationTests(unittest.TestCase):
    def test_publication_reports_reason_code_without_raw_secret(self):
        import maintenance
        secret = "".join(("sk", "-ant-", _synthetic_test_credential()))
        TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"
        TASK_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            vault = Path(directory) / "vault"
            folder = vault / "proposals" / "codex"
            folder.mkdir(parents=True)
            (folder / "prop-1.md").write_text(
                '---\nid: "prop-1"\nclassification: "internal"\nagent: "codex"\n'
                'project_slug: "one"\ntarget_pages: ["wiki/projects/one.md"]\n'
                'source_ids: ["src-1"]\n---\nbody\n', encoding="utf-8")
            _write_source(vault)
            db = sqlite3.connect(":memory:")
            config_path, cfg = _write_publication_config(directory)
            control = {"publication": {"enabled": True,
                                       "include_existing_hub_backlog": True,
                                       "min_ready_groups": 1,
                                       "max_wait_hours": 24, "max_batch": 8}}
            err = TransportError(f"wrapped {secret} stdout",
                                 reason_code="native_timeout")
            with patch("curate_once.run", side_effect=err):
                result = maintenance.publication_tick(config_path, cfg, control, db)
            self.assertEqual(result["status"], "BACKOFF_DEFERRED")
            self.assertEqual(result.get("reason"), "native_timeout")
            self.assertNotIn(secret, json.dumps(result))
            db.close()

    def test_transient_outage_uses_backoff_not_two_error_freeze(self):
        import maintenance
        TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"
        TASK_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            vault = Path(directory) / "vault"
            folder = vault / "proposals" / "codex"
            folder.mkdir(parents=True)
            (folder / "prop-1.md").write_text(
                '---\nid: "prop-1"\nclassification: "internal"\nagent: "codex"\n'
                'project_slug: "one"\ntarget_pages: ["wiki/projects/one.md"]\n'
                'source_ids: ["src-1"]\n---\nbody\n', encoding="utf-8")
            _write_source(vault)
            db = sqlite3.connect(":memory:")
            config_path, cfg = _write_publication_config(directory)
            control = {"publication": {"enabled": True,
                                       "include_existing_hub_backlog": True,
                                       "min_ready_groups": 1,
                                       "max_wait_hours": 24, "max_batch": 8}}
            with patch("curate_once.run",
                       side_effect=TransportError("t", reason_code="process_timeout")) as run:
                first = maintenance.publication_tick(config_path, cfg, control, db)
                second = maintenance.publication_tick(config_path, cfg, control, db)
                self.assertEqual(first["status"], "BACKOFF_DEFERRED")
                self.assertEqual(second["status"], "SKIPPED_NO_DUE_BATCH")
                self.assertEqual(run.call_count, 1)
            db.close()

    def test_incomplete_publication_fixture_remains_quarantined(self):
        import maintenance
        TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"
        TASK_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            vault = Path(directory) / "vault"
            folder = vault / "proposals" / "codex"
            folder.mkdir(parents=True)
            (folder / "prop-1.md").write_text(
                '---\nid: "prop-1"\nclassification: "internal"\nagent: "codex"\n'
                'project_slug: "one"\ntarget_pages: ["wiki/projects/one.md"]\n'
                'source_ids: ["src-1"]\n---\nbody\n', encoding="utf-8")
            db = sqlite3.connect(":memory:")
            cfg = {"project_slug": "one", "hub": {"vault": str(vault)},
                   "agent": "hub-caretaker", "state_dir": str(Path(directory) / "state")}
            control = {"publication": {"enabled": True,
                                       "include_existing_hub_backlog": True,
                                       "min_ready_groups": 1,
                                       "max_wait_hours": 24, "max_batch": 8}}
            with patch("curate_once.run") as run:
                result = maintenance.publication_tick("config", cfg, control, db)
                self.assertEqual(result["status"], "QUARANTINED_MEMBERS_ONLY")
                run.assert_not_called()
            db.close()


class BudgetValidationTests(unittest.TestCase):
    def test_legacy_small_budget_still_spawns(self):
        seen = {}
        def runner(*, executable, argv, cwd, prompt, timeout):
            seen["timeout"] = timeout
            return 0, _ok_envelope(), ""
        t = OpenClawArgvTransport(run_callable=runner)
        env = t.invoke(spec=_spec(["agent", "exec", "--timeout", "120"]),
                       cwd="c", prompt="hello", run_config_path="r",
                       state_dir="s", timeout=1)
        self.assertEqual(env.status, "ok")
        self.assertEqual(seen["timeout"], 1)

    def test_legacy_invalid_budgets_rejected(self):
        for bad in (0, -3, "120", 2.5, True, None):
            def runner(*, executable, argv, cwd, prompt, timeout):
                raise AssertionError("must not spawn")
            t = OpenClawArgvTransport(run_callable=runner)
            with self.subTest(bad=bad):
                with self.assertRaises(TransportError) as cm:
                    t.invoke(spec=_spec(["agent"]), cwd="c", prompt="hello",
                             run_config_path="r", state_dir="s", timeout=bad)
                self.assertEqual(cm.exception.reason_code, "invalid_budget")

    def test_placeholder_requires_margin_room(self):
        def runner(*, executable, argv, cwd, prompt, timeout):
            raise AssertionError("must not spawn")
        t = OpenClawArgvTransport(run_callable=runner)
        spec = _spec(["agent", "exec", "--timeout",
                      "${native_timeout_seconds}"])
        with self.assertRaises(TransportError) as cm:
            t.invoke(spec=spec, cwd="c", prompt="hello",
                     run_config_path="r", state_dir="s", timeout=10)
        self.assertEqual(cm.exception.reason_code, "invalid_budget")


if __name__ == "__main__":
    unittest.main()
