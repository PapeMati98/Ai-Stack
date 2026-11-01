"""HD-20261014-2cfc5e: the MiMo Token Plan HTTP 429 "quota exhausted" refusal is
named, diagnosed and recorded as pool exhaustion instead of a generic parent
timeout with no stdout.

Synthetic streams only; no provider process is started, no network, no real
key. Temporary directories come from mkdtemp and are never deleted.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fleet
import mimo_runtime
import quota_availability as qa
import routing_v2

REFUSAL_BODY = (
    '{\n    "error": {\n        "code": "429",\n        "message": "quota exhausted",\n'
    '        "type": "limitation"\n    }\n}\n'
)
THROTTLE_BODY = '{"error": {"code": "429", "message": "rate limit exceeded", "type": "rate_limit"}}'
SERVER_BODY = '{"error": {"code": "500", "message": "internal error", "type": "server"}}'
SYNTHETIC_PROMPT = "SYNTHETIC-PROMPT-MARKER do not echo"


def llm_error_line(ts: str, *, status: int | str = 429, body: str | None = REFUSAL_BODY,
                   name: str = "AI_APICallError", provider: str = "xiaomi",
                   level: str = "ERROR", trailing: str = " stream error") -> str:
    error = {
        "name": name,
        "url": "https://token-plan-ams.xiaomimimo.com/v1/chat/completions",
        "requestBodyValues": {
            "model": "mimo-v2.6-pro", "max_tokens": 128000,
            "messages": [{"role": "system", "content": SYNTHETIC_PROMPT}],
        },
        "statusCode": status,
        "responseHeaders": {"content-type": "application/json", "x-mife-upstream-status": str(status)},
        "isRetryable": True,
        "data": {},
    }
    if body is not None:
        error["responseBody"] = body
    return (
        f"{level} {ts} +465ms service=llm providerID={provider} modelID=mimo-v2.6-pro "
        "session.id=ses_synthetic0000000000000000 small=false agent=build mode=primary "
        f"error={json.dumps({'error': error})}{trailing}"
    )


def info_stream_line(ts: str) -> str:
    return (
        f"INFO  {ts} +399ms service=llm providerID=xiaomi modelID=mimo-v2.6-pro "
        "session.id=ses_synthetic0000000000000000 small=false agent=build mode=primary stream"
    )


def refusal_stderr(count: int = 3) -> str:
    stamps = ["2026-10-16T09:51:49", "2026-10-16T09:51:51", "2026-10-16T09:51:55",
              "2026-10-16T09:52:04", "2026-10-16T09:52:19"][:count]
    lines = [
        "INFO  2026-10-16T09:51:46 +640ms service=server-proxy version=0.1.15 args=[\"run\"]",
        "Performing one time database migration, may take a few minutes...",
        "sqlite-migration:done",
    ]
    for ts in stamps:
        lines.append(info_stream_line(ts))
        lines.append(llm_error_line(ts))
    lines.append("INFO  2026-10-16T10:01:44 +0ms service=server status=started method=GET path=/session/status request")
    lines.append("")
    lines.append("Agent Fleet timeout after 600 seconds (including stream drain).")
    return "\n".join(lines) + "\n"


def completed_stdout() -> str:
    events = [
        {"type": "step_start", "sessionID": "ses_s", "part": {"type": "step-start", "messageID": "m1"}},
        {"type": "text", "sessionID": "ses_s", "part": {"type": "text", "messageID": "m1",
                                                        "text": json.dumps({"status": "completed"})}},
        {"type": "step_finish", "sessionID": "ses_s",
         "part": {"type": "step-finish", "reason": "stop", "messageID": "m1",
                  "tokens": {"total": 10, "input": 8, "output": 2, "reasoning": 0,
                             "cache": {"write": 0, "read": 0}}}},
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


class RefusalLineTests(unittest.TestCase):
    def test_anchored_refusal_lines_are_counted_with_their_window(self) -> None:
        summary = mimo_runtime.mimo_quota_refusal(refusal_stderr(3))
        self.assertEqual(summary, {"count": 3, "first_at": "2026-10-16T09:51:49",
                                   "last_at": "2026-10-16T09:51:55"})

    def test_crlf_and_trailing_suffix_do_not_matter(self) -> None:
        text = refusal_stderr(2).replace("\n", "\r\n")
        self.assertEqual(mimo_runtime.mimo_quota_refusal(text)["count"], 2)
        bare = llm_error_line("2026-10-16T09:51:49", trailing="")
        self.assertEqual(mimo_runtime.mimo_quota_refusal(bare)["count"], 1)

    def test_healthy_or_empty_stderr_is_not_a_refusal(self) -> None:
        self.assertIsNone(mimo_runtime.mimo_quota_refusal(""))
        self.assertIsNone(mimo_runtime.mimo_quota_refusal(None))
        healthy = "\n".join([info_stream_line("2026-10-19T13:11:05"),
                             "INFO  2026-10-19T13:11:06 +1ms service=session.processor step=1",
                             "ERROR 2026-10-19T13:19:03 +0ms service=session.processor error=Aborted process"])
        self.assertIsNone(mimo_runtime.mimo_quota_refusal(healthy))

    def test_prose_quoting_the_refusal_is_not_anchored(self) -> None:
        quoted = "INFO  2026-10-16T09:51:49 +1ms service=tool.registry output=" + llm_error_line("2026-10-16T09:51:49")
        self.assertIsNone(mimo_runtime.mimo_quota_refusal(quoted))
        lowered = llm_error_line("2026-10-16T09:51:49", level="WARN")
        self.assertIsNone(mimo_runtime.mimo_quota_refusal(lowered))
        prose = "The provider said AI_APICallError service=llm 429 quota exhausted, please retry."
        self.assertIsNone(mimo_runtime.mimo_quota_refusal(prose))

    def test_other_status_body_name_or_provider_is_not_a_refusal(self) -> None:
        cases = {
            "throttle body": llm_error_line("2026-10-16T09:51:49", body=THROTTLE_BODY),
            "server 500": llm_error_line("2026-10-16T09:51:49", status=500, body=SERVER_BODY),
            "missing body": llm_error_line("2026-10-16T09:51:49", body=None),
            "non-json body": llm_error_line("2026-10-16T09:51:49", body="quota exhausted"),
            "other error name": llm_error_line("2026-10-16T09:51:49", name="AI_RetryError"),
            "foreign provider": llm_error_line("2026-10-16T09:51:49", provider="openai"),
            "broken json": llm_error_line("2026-10-16T09:51:49")[:-40],
        }
        for label, line in cases.items():
            with self.subTest(label):
                self.assertIsNone(mimo_runtime.mimo_quota_refusal(line))

    def test_any_different_native_error_voids_the_refusal(self) -> None:
        refusal = llm_error_line("2026-10-16T09:51:49")
        other = llm_error_line("2026-10-16T09:51:51", status=500, body=SERVER_BODY)
        self.assertIsNone(mimo_runtime.mimo_quota_refusal(refusal + "\n" + other))
        self.assertIsNone(mimo_runtime.mimo_quota_refusal(other + "\n" + refusal))
        throttled = llm_error_line("2026-10-16T09:51:51", body=THROTTLE_BODY)
        self.assertIsNone(mimo_runtime.mimo_quota_refusal(refusal + "\n" + throttled))

    def test_string_status_code_is_accepted(self) -> None:
        line = llm_error_line("2026-10-16T09:51:49", status="429")
        self.assertEqual(mimo_runtime.mimo_quota_refusal(line)["count"], 1)

    def test_parse_bound_never_claims_completeness(self) -> None:
        bound = mimo_runtime.MIMO_MAX_LLM_ERROR_LINES
        self.assertEqual(bound, 256)
        stamps = [f"2026-10-16T{9 + i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}" for i in range(bound + 1)]
        exact = "\n".join(llm_error_line(ts) for ts in stamps[:bound])
        summary = mimo_runtime.mimo_quota_refusal(exact)
        self.assertEqual(summary, {"count": bound, "first_at": stamps[0], "last_at": stamps[bound - 1]})
        self.assertIsNotNone(mimo_runtime.mimo_quota_rejection("", exact, 124))
        # 256 refusals followed by a different native error: the "every line" rule still voids it.
        other = llm_error_line(stamps[bound], status=500, body=SERVER_BODY)
        self.assertIsNone(mimo_runtime.mimo_quota_refusal(exact + "\n" + other))
        self.assertIsNone(mimo_runtime.mimo_quota_rejection("", exact + "\n" + other, 124))
        # 257 refusals: beyond the parse bound the result is unknown, never a block on a partial scan.
        beyond = exact + "\n" + llm_error_line(stamps[bound])
        self.assertIsNone(mimo_runtime.mimo_quota_refusal(beyond))
        self.assertIsNone(mimo_runtime.mimo_quota_rejection("", beyond, 124))


class RejectionTests(unittest.TestCase):
    EXPECTED = {
        "status": "exhausted", "scope": "subscription", "source": "native-provider-error",
        "reason": "mimo-token-plan-quota-exhausted-http429", "reset_at": None,
    }

    def test_timeout_kill_and_native_give_up_are_refusals(self) -> None:
        for code in (124, 1, 4294967295):
            with self.subTest(exit_code=code):
                self.assertEqual(mimo_runtime.mimo_quota_rejection("", refusal_stderr(), code), self.EXPECTED)
        self.assertEqual(mimo_runtime.MIMO_QUOTA_EXHAUSTED_REASON, self.EXPECTED["reason"])

    def test_exit_zero_or_unknown_exit_is_never_a_refusal(self) -> None:
        for code in (0, None, True, "124", 124.0):
            with self.subTest(exit_code=code):
                self.assertIsNone(mimo_runtime.mimo_quota_rejection("", refusal_stderr(), code))

    def test_completed_answer_is_never_a_refusal(self) -> None:
        # An early throttle followed by a served answer keeps the pool open.
        self.assertIsNone(mimo_runtime.mimo_quota_rejection(completed_stdout(), refusal_stderr(), 1))

    def test_non_refusal_stderr_keeps_the_generic_path(self) -> None:
        self.assertIsNone(mimo_runtime.mimo_quota_rejection("", "", 124))
        self.assertIsNone(mimo_runtime.mimo_quota_rejection("", llm_error_line("2026-10-16T09:51:49", status=500, body=SERVER_BODY), 124))
        self.assertIsNone(mimo_runtime.mimo_quota_rejection(None, refusal_stderr(), 124))
        self.assertIsNone(mimo_runtime.mimo_quota_rejection("", None, 124))

    def test_rejection_is_recorded_like_other_native_refusals(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="agent-fleet-mimo-quota-"))
        now = time.time()
        result = mimo_runtime.mimo_quota_rejection("", refusal_stderr(), 124)
        state = qa.record_rejection(root, "mimo", result, lock_factory=fleet.ReleasableKernelLock, now=now)
        self.assertEqual(state["status"], "exhausted")
        self.assertEqual(state["reason"], "mimo-token-plan-quota-exhausted-http429")
        self.assertEqual(state["block_step"], 0)
        self.assertGreater(qa.seconds(state["recheck_at"]), now)
        cached = qa.read_cache(qa.cache_path(root, "mimo"))
        self.assertEqual((cached["status"], cached["scope"], cached["source"], cached["reason"]),
                         ("exhausted", "subscription", "native-provider-error",
                          "mimo-token-plan-quota-exhausted-http429"))
        self.assertIsNone(cached.get("reset_at"))
        self.assertEqual(cached["recheck_at"], state["recheck_at"])
        # The same refusal 20 minutes later lengthens the no-probe backoff.
        again = qa.record_rejection(root, "mimo", result, lock_factory=fleet.ReleasableKernelLock, now=now + 1200)
        self.assertEqual(again["block_step"], 1)


class DiagnosisTests(unittest.TestCase):
    def test_diagnosis_is_bounded_and_echoes_no_provider_text(self) -> None:
        refusal = mimo_runtime.mimo_quota_refusal(refusal_stderr(4))
        timed = mimo_runtime.mimo_quota_diagnosis(refusal, 124, timed_out=True)
        self.assertTrue(timed.startswith("PROVIDER QUOTA REFUSAL (not a handoff problem)"))
        self.assertIn("all 4 native requests between 2026-10-16T09:51:49 and 2026-10-16T09:52:04", timed)
        self.assertIn("parent timeout", timed)
        self.assertIn("mimo-token-plan-quota-exhausted-http429", timed)
        self.assertNotIn(SYNTHETIC_PROMPT, timed)
        self.assertLess(len(timed), 700)
        native = mimo_runtime.mimo_quota_diagnosis(refusal, 1, timed_out=False)
        self.assertIn("exit code 1", native)
        self.assertNotIn("parent timeout", native)
        self.assertIn("every native requests", mimo_runtime.mimo_quota_diagnosis(None, 1, timed_out=False))


class ExecuteSingleTests(unittest.TestCase):
    """The run record and the availability cache after a synthetic MiMo child."""

    def mimo_run(self, *, child: dict) -> tuple[dict, dict, Path, str]:
        root = Path(tempfile.mkdtemp(prefix="agent-fleet-mimo-quota-run-"))
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        mimo = profiles["mimo"]
        store = root / "auth.json"
        store.write_text(json.dumps({"xiaomi": {
            "type": "api",
            "key": "tp-test-only-not-a-real-key",
            "metadata": {"uid": "test-account",
                         "base_url": "https://token-plan-ams.xiaomimimo.com/v1"},
        }}), encoding="utf-8")
        mimo["mimo"]["credential_store"]["path"] = str(store)
        pool = str(mimo.get("quota_pool_id") or "mimo")
        execution = routing_v2.resolve_execution_profile(
            "mimo", mimo, complexity="low", mode="review", required_tools=[]
        )

        def fake_child(command, **kwargs):
            return {"child": dict(child), "queue_wait_seconds": 0.0, "execution_serialized": False}

        env = {k: v for k, v in os.environ.items() if k != "MIMOCODE_COMPACTION_MAX_CONTEXT"}
        env.update({"AGENT_FLEET_DEPTH": "0", "AGENT_FLEET_OFFICE": "0",
                    "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing-billing.json")})
        quota = {"mimo": {"status": "fresh", "headroom": 1.0, "confidence": 1.0}}
        patches = [
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(fleet, "resolve_executable", return_value=str(root / "mimo.exe")),
            mock.patch.object(fleet.mimo_runtime, "require_mimo_native_executable"),
            mock.patch.object(fleet, "probe_command", return_value={"launchable": True, "exit_code": 0, "output": "ok"}),
            mock.patch.object(fleet, "profile_credential_status", return_value={"ready": True}),
            mock.patch.object(fleet, "merge_credential_readiness", return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "readiness_from_probes", return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "runtime_model_visible", return_value=(True, "visible")),
            mock.patch.object(routing_v2, "quota_hard_gates", return_value=[]),
            mock.patch.object(fleet, "current_quota_views", return_value=(quota, {})),
            mock.patch.object(fleet, "run_provider_child", side_effect=fake_child),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            result = fleet.execute_single(
                "Review the synthetic fixture.",
                profiles=profiles, agent_id="mimo", mode="review", kind="review",
                complexity="low", model_override=None, workspace=root, timeout=60,
                allow_write=False, native_subagent_limit=0, enable_web=False,
                inherit_api_keys=False, dry_run=False, output_root=root / "evidence",
                root=root, execution_profile=execution, knowledge_mode="none",
            )
        record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
        return result, record, root, pool

    def test_timed_out_refusal_is_named_diagnosed_and_blocks_the_pool(self) -> None:
        child = {"exit_code": 124, "stdout": "", "stderr": refusal_stderr(5), "timed_out": True,
                 "model_tool_calls": None, "tool_budget_exceeded": False}
        result, record, root, pool = self.mimo_run(child=child)
        self.assertEqual(record["exit_code"], 124)
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["completion_reason"], "mimo-token-plan-quota-exhausted-http429")
        self.assertTrue(str(record["handoff_parse_error"]).startswith("PROVIDER QUOTA REFUSAL"))
        self.assertIn("all 5 native requests", record["handoff_parse_error"])
        self.assertNotIn(SYNTHETIC_PROMPT, json.dumps(record))
        self.assertIsNone(record["handoff"])
        self.assertNotEqual(record["run_status"], "COMPLETED")
        denial = record["quota_collection_after"][pool]["native_denial"]
        self.assertEqual(denial["reason"], "mimo-token-plan-quota-exhausted-http429")
        cached = qa.read_cache(qa.cache_path(root, pool))
        self.assertEqual((cached["status"], cached["source"], cached["reason"]),
                         ("exhausted", "native-provider-error", "mimo-token-plan-quota-exhausted-http429"))
        self.assertEqual(cached["block_step"], 0)

    def test_native_give_up_after_refusals_is_also_recorded(self) -> None:
        child = {"exit_code": 4294967295, "stdout": "", "stderr": refusal_stderr(2), "timed_out": False,
                 "model_tool_calls": None, "tool_budget_exceeded": False}
        _, record, root, pool = self.mimo_run(child=child)
        self.assertEqual(record["completion_reason"], "mimo-token-plan-quota-exhausted-http429")
        self.assertIn("exit code 4294967295", record["handoff_parse_error"])
        self.assertEqual(qa.read_cache(qa.cache_path(root, pool))["status"], "exhausted")

    def test_ordinary_timeout_keeps_the_generic_path_and_no_block(self) -> None:
        child = {"exit_code": 124, "stdout": "", "stderr": "INFO  2026-10-19T13:11:05 +797ms service=server-proxy version=0.1.15\n",
                 "timed_out": True, "model_tool_calls": None, "tool_budget_exceeded": False}
        _, record, root, pool = self.mimo_run(child=child)
        self.assertEqual(record["completion_reason"], "parent-timeout")
        self.assertIn("PROVIDER FAILURE (not a handoff problem)", record["handoff_parse_error"])
        self.assertNotIn("native_denial", record["quota_collection_after"].get(pool, {}))
        self.assertFalse(qa.cache_path(root, pool).exists())


if __name__ == "__main__":
    unittest.main()
