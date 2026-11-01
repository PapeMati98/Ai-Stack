"""Agent Fleet 0.9.37: context telemetry, Kimi wire usage, native budgets, Qwen registry.

Every fixture is synthetic (numeric counters only, no real prompts, keys or
settings). Temporary directories come from mkdtemp and are never deleted: the
suite does not remove files. No test launches a provider CLI or uses network.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fleet  # noqa: E402
import routing_v2  # noqa: E402


def make_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="agent-fleet-context-budget-"))


def jsonl(events: list[dict]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def step_finish(identity: str, *, fresh: int, read: int, write: int = 0,
                reasoning: int = 0, output: int = 10) -> dict:
    return {
        "type": "step_finish",
        "part": {
            "type": "step-finish",
            "id": identity,
            "tokens": {
                "total": fresh + read + write + output + reasoning,
                "input": fresh,
                "output": output,
                "reasoning": reasoning,
                "cache": {"read": read, "write": write},
            },
        },
    }


def tool_use(identity: str, output: str) -> dict:
    return {
        "type": "tool_use",
        "part": {
            "type": "tool",
            "id": identity,
            "tool": "read",
            "state": {"status": "completed", "output": output},
        },
    }


def assistant(identity: str, usage: dict, model: str = "synthetic-model") -> dict:
    return {
        "type": "assistant",
        "message": {"id": identity, "model": model, "usage": usage, "content": []},
    }


def tool_result(content) -> dict:
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": content}]},
    }


def write_wire(home: Path, agent: str, rows: list[dict], *, session: str = "session_a") -> Path:
    wire = home / "sessions" / "wd_fixture" / session / "agents" / agent / "wire.jsonl"
    wire.parent.mkdir(parents=True, exist_ok=True)
    wire.write_text(jsonl(rows), encoding="utf-8")
    return wire


def kimi_usage(other: int, read: int, output: int, *, agent: str = "main",
               scope: str = "turn", creation: int = 0) -> dict:
    return {
        "type": "usage.record",
        "agentId": agent,
        "model": "k3",
        "usageScope": scope,
        "usage": {
            "inputOther": other,
            "output": output,
            "inputCacheRead": read,
            "inputCacheCreation": creation,
        },
    }


def kimi_step_end(other: int, read: int, output: int) -> dict:
    # The same measurement Kimi Code writes a second time; never counted.
    return {
        "type": "context.append_loop_event",
        "agentId": "main",
        "event": {
            "type": "step.end",
            "usage": {"inputOther": other, "output": output,
                      "inputCacheRead": read, "inputCacheCreation": 0},
        },
    }


def kimi_tool_result(output: str) -> dict:
    return {
        "type": "context.append_loop_event",
        "agentId": "main",
        "event": {"type": "tool.result", "result": {"output": output, "note": "n"}},
    }


QWEN_SYNTHETIC_SETTINGS = {
    # Same shape as the owner's Qwen settings that caused the swap: the Max
    # route is the FIRST openai entry, Flash and DeepSeek are not registered.
    "modelProviders": {
        "openai": [
            {"id": "qwen3.8-max", "baseUrl": "https://example.invalid/v1",
             "envKey": "SYNTHETIC_KEY_NAME"},
            {"id": "glm-5", "baseUrl": "https://example.invalid/v1",
             "envKey": "SYNTHETIC_KEY_NAME"},
        ]
    },
    "security": {"auth": {"selectedType": "openai"}},
    "model": {"name": "qwen3.8-max"},
    "env": {"SYNTHETIC_KEY_NAME": "not-a-real-value"},
}


class ContextTelemetryTests(unittest.TestCase):
    def test_mimo_step_series_matches_audited_snowball_shape(self) -> None:
        # 7 279 = 3 183 fresh + 4 096 cache read (audited first MiMo step);
        # reasoning is never part of the step input.
        events = [
            step_finish("s1", fresh=3183, read=4096, reasoning=18),
            tool_use("t1", "x" * 1000),
            step_finish("s2", fresh=54609, read=7168, reasoning=5591),
            tool_use("t2", "é" * 500),
            step_finish("s3", fresh=4990, read=197569, write=0, reasoning=14619),
            step_finish("s2", fresh=54609, read=7168, reasoning=5591),  # duplicate
        ]
        telemetry = fleet.context_telemetry(
            [jsonl(events)], adapter="mimo", warn_tokens=150_000
        )
        self.assertEqual(telemetry["steps"], 3)
        self.assertEqual(telemetry["first_step_input"], 7279)
        self.assertEqual(telemetry["max_step_input"], 202559)
        self.assertEqual(telemetry["last_step_input"], 202559)
        self.assertEqual(telemetry["growth_ratio"], round(202559 / 7279, 3))
        self.assertEqual(telemetry["tool_result_bytes"], 1000 + 1000)
        self.assertIsNone(telemetry["compaction_events"])
        self.assertEqual(telemetry["source"], "mimo-step-finish")
        self.assertTrue(fleet.context_snowball_warning(telemetry))

    def test_opencode_uses_the_same_step_finish_semantics(self) -> None:
        events = [
            step_finish("a", fresh=1000, read=0, write=500),
            step_finish("b", fresh=200, read=1500),
            {"type": "text", "part": {"type": "compaction", "id": "c"}},
        ]
        telemetry = fleet.context_telemetry([jsonl(events)], adapter="opencode")
        self.assertEqual(telemetry["steps"], 2)
        self.assertEqual(telemetry["first_step_input"], 1500)
        self.assertEqual(telemetry["max_step_input"], 1700)
        self.assertEqual(telemetry["tool_result_bytes"], 0)
        self.assertEqual(telemetry["compaction_events"], 1)
        self.assertEqual(telemetry["source"], "opencode-step-finish")
        self.assertFalse(fleet.context_snowball_warning(telemetry))

    def test_opencode_repeated_small_steps_report_cumulative_without_false_snowball(self) -> None:
        events = [step_finish(str(i), fresh=1000, read=29_000)
                  for i in range(6)]
        telemetry = fleet.context_telemetry([jsonl(events)], adapter="opencode")
        self.assertEqual(telemetry["max_step_input"], 30_000)
        self.assertEqual(telemetry["cumulative_step_input"], 180_000)
        self.assertFalse(fleet.context_snowball_warning(telemetry))
        self.assertFalse(fleet.context_cumulative_warning(telemetry, 200_000))
        self.assertTrue(fleet.context_cumulative_warning(telemetry, 150_000))
        self.assertIsNone(fleet.context_cumulative_warning(telemetry, None))

    def test_opencode_cumulative_limits_are_separate_and_calibrated(self) -> None:
        profile = fleet.load_profiles(fleet.DEFAULT_PROFILES)["opencode"]
        self.assertEqual(fleet.profile_context_stop_tokens(profile), 2_500_000)
        self.assertEqual(fleet.profile_context_cumulative_warn_tokens(profile), 1_500_000)
        broken = dict(profile, context_cumulative_warn_tokens=2_500_000)
        with self.assertRaisesRegex(fleet.FleetError, "below context_stop_tokens"):
            fleet.profile_context_cumulative_warn_tokens(broken)

    def test_qwen_input_already_includes_cache_and_placeholders_are_not_steps(self) -> None:
        events = [
            {"type": "system", "subtype": "init", "model": "qwen3.8-max"},
            assistant("m0", {"input_tokens": 0, "output_tokens": 0}),
            assistant("m1", {"input_tokens": 10838, "output_tokens": 430,
                             "cache_read_input_tokens": 0}),
            tool_result("abc"),
            assistant("m2", {"input_tokens": 0, "output_tokens": 0}),
            assistant("m2", {"input_tokens": 14483, "output_tokens": 121,
                             "cache_read_input_tokens": 10832}),
            tool_result([{"type": "text", "text": "hello"}]),
            assistant("m3", {"input_tokens": 139690, "output_tokens": 11489,
                             "cache_read_input_tokens": 138618}),
        ]
        telemetry = fleet.context_telemetry([jsonl(events)], adapter="qwen")
        self.assertEqual(telemetry["steps"], 3)
        self.assertEqual(telemetry["first_step_input"], 10838)
        # Cache reads are inside input_tokens for Qwen: never added again.
        self.assertEqual(telemetry["max_step_input"], 139690)
        self.assertEqual(telemetry["tool_result_bytes"], 3 + 5)
        self.assertIsNone(telemetry["compaction_events"])
        self.assertEqual(telemetry["source"], "qwen-stream-json-assistant-usage")

    def test_claude_stream_sums_cache_parts_once_per_message(self) -> None:
        usage = {"input_tokens": 12, "cache_creation_input_tokens": 3000,
                 "cache_read_input_tokens": 9000, "output_tokens": 5}
        events = [
            {"type": "system", "subtype": "init"},
            assistant("msg_1", usage),
            assistant("msg_1", usage),  # one event per content block
            tool_result("0123456789"),
            assistant("msg_2", dict(usage, cache_read_input_tokens=160000)),
            {"type": "system", "subtype": "compact_boundary"},
            {"type": "result", "subtype": "success", "usage": usage},
        ]
        telemetry = fleet.context_telemetry([jsonl(events)], adapter="claude")
        self.assertEqual(telemetry["steps"], 2)
        self.assertEqual(telemetry["first_step_input"], 12012)
        self.assertEqual(telemetry["max_step_input"], 163012)
        self.assertEqual(telemetry["tool_result_bytes"], 10)
        self.assertEqual(telemetry["compaction_events"], 1)
        self.assertTrue(fleet.context_snowball_warning(telemetry))

    def test_claude_result_json_has_no_per_step_usage(self) -> None:
        result = {"type": "result", "subtype": "success",
                  "usage": {"input_tokens": 32, "cache_read_input_tokens": 1230420,
                            "output_tokens": 8577}}
        telemetry = fleet.context_telemetry([json.dumps(result)], adapter="claude")
        for key in ("steps", "first_step_input", "max_step_input",
                    "last_step_input", "growth_ratio", "tool_result_bytes",
                    "compaction_events"):
            self.assertIsNone(telemetry[key], key)
        self.assertEqual(telemetry["source"], "claude-output-without-per-step-usage")
        self.assertIsNone(fleet.context_snowball_warning(telemetry))

    def test_codex_exec_reports_tool_bytes_but_no_step_input(self) -> None:
        events = [
            {"type": "thread.started", "thread_id": "t"},
            {"type": "turn.started"},
            {"type": "item.completed",
             "item": {"type": "command_execution", "aggregated_output": "abcd"}},
            {"type": "item.completed",
             "item": {"type": "mcp_tool_call", "result": {"content": []}}},
            {"type": "turn.completed",
             "usage": {"input_tokens": 1314648, "cached_input_tokens": 1234688,
                       "output_tokens": 9773, "reasoning_output_tokens": 5267}},
        ]
        telemetry = fleet.context_telemetry([jsonl(events)], adapter="codex")
        self.assertIsNone(telemetry["steps"])
        self.assertIsNone(telemetry["max_step_input"])
        self.assertIsNone(telemetry["growth_ratio"])
        self.assertEqual(
            telemetry["tool_result_bytes"],
            4 + len(json.dumps({"content": []}).encode("utf-8")),
        )
        self.assertIsNone(telemetry["compaction_events"])
        self.assertEqual(telemetry["source"], "codex-exec-json-turn-usage-only")
        # A cumulative turn total is not a context size: no warning either way.
        self.assertIsNone(fleet.context_snowball_warning(telemetry))

    def test_grok_and_empty_outputs_stay_unknown_not_zero(self) -> None:
        grok = fleet.context_telemetry(['{"text": "{}"}'], adapter="grok")
        self.assertEqual(grok["source"], "not-reported-per-step")
        self.assertIsNone(grok["steps"])
        self.assertIsNone(grok["tool_result_bytes"])
        empty = fleet.context_telemetry([""], adapter="mimo")
        self.assertEqual(empty["source"], "unknown")
        self.assertIsNone(empty["steps"])
        self.assertIsNone(empty["compaction_events"])

    def test_malformed_provider_values_never_crash_telemetry(self) -> None:
        hostile = jsonl([
            {"type": ["turn.completed"], "item": {"type": ["x"]}},
            {"type": "step_finish", "part": {"type": "step-finish", "id": ["x"],
                                             "tokens": {"input": True, "cache": []}}},
            {"type": "assistant", "message": {"id": ["x"], "usage": {"input_tokens": -1}}},
            {"type": "system", "subtype": ["compact_boundary"]},
            [1, 2, 3],
            "not json",
        ])
        for adapter in ("codex", "mimo", "opencode", "qwen", "claude", "grok"):
            with self.subTest(adapter=adapter):
                telemetry = fleet.context_telemetry([hostile], adapter=adapter)
                self.assertIsNone(telemetry["max_step_input"])
        home = make_root() / "kimi-home"
        write_wire(home, "main", [
            {"type": ["compaction.completed"]},
            {"type": "usage.record", "usageScope": ["turn"],
             "usage": {"inputOther": 5, "output": 1}},
        ])
        wire = fleet.kimi_wire_telemetry(home)
        self.assertEqual(wire["token_usage"]["fresh_input_tokens"], 5)
        self.assertEqual(wire["steps"], [])
        self.assertIsNone(wire["compaction_events"])

    def test_retry_attempts_concatenate_step_series(self) -> None:
        first = jsonl([step_finish("a", fresh=100, read=0)])
        second = jsonl([step_finish("b", fresh=50, read=0),
                        step_finish("c", fresh=400, read=0)])
        telemetry = fleet.context_telemetry([first, second], adapter="mimo")
        self.assertEqual(telemetry["steps"], 3)
        self.assertEqual(telemetry["first_step_input"], 100)
        self.assertEqual(telemetry["max_step_input"], 400)
        self.assertEqual(telemetry["last_step_input"], 400)
        self.assertEqual(telemetry["growth_ratio"], 4.0)

    def test_warning_threshold_comes_from_the_optional_profile_field(self) -> None:
        self.assertEqual(fleet.profile_context_warn_tokens({}), 150_000)
        self.assertEqual(
            fleet.profile_context_warn_tokens({"context_warn_tokens": 5_000}), 5_000
        )
        telemetry = fleet.context_telemetry(
            [jsonl([step_finish("a", fresh=6_000, read=0)])],
            adapter="mimo",
            warn_tokens=5_000,
        )
        self.assertEqual(telemetry["warn_tokens"], 5_000)
        self.assertTrue(fleet.context_snowball_warning(telemetry))
        for invalid in (True, 10, "150000", 20_000_000):
            with self.assertRaises(fleet.FleetError):
                fleet.profile_context_warn_tokens({"context_warn_tokens": invalid})


class KimiWireUsageTests(unittest.TestCase):
    def test_usage_record_only_is_counted_and_reasoning_stays_unknown(self) -> None:
        home = make_root() / "kimi-home"
        write_wire(home, "main", [
            {"type": "metadata"},
            kimi_usage(10489, 34304, 130),
            kimi_step_end(10489, 34304, 130),
            kimi_tool_result("y" * 300),
            kimi_usage(2000, 54021, 90, creation=100),
            kimi_step_end(2000, 54021, 90),
            kimi_usage(500, 0, 7, scope="compaction"),
            {"type": "compaction.completed"},
        ])
        write_wire(home, "sub", [kimi_usage(1000, 0, 10, agent="sub")])
        wire = fleet.kimi_wire_telemetry(home)
        usage = wire["token_usage"]
        self.assertEqual(usage["status"], "known")
        self.assertEqual(usage["source"], "kimi-wire-log")
        self.assertEqual(usage["record_count"], 4)
        self.assertEqual(usage["wire_files"], 2)
        self.assertEqual(usage["fresh_input_tokens"], 10489 + 2000 + 500 + 1000)
        self.assertEqual(usage["cache_read_tokens"], 34304 + 54021)
        self.assertEqual(usage["cache_write_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 130 + 90 + 7 + 10)
        self.assertEqual(
            usage["input_tokens"], 10489 + 2000 + 500 + 1000 + 34304 + 54021 + 100
        )
        self.assertEqual(
            usage["total_tokens"], usage["input_tokens"] + usage["output_tokens"]
        )
        self.assertIsNone(usage["reasoning_tokens"])
        self.assertIsNone(usage["cost_usd"])
        # Context steps: main agent turn records only (not compaction, not sub).
        self.assertEqual(wire["steps"], [10489 + 34304, 2000 + 54021 + 100])
        self.assertEqual(wire["tool_result_bytes"], 300)
        self.assertEqual(wire["compaction_events"], 1)
        telemetry = fleet.context_telemetry([], adapter="kimi", kimi_wire=wire)
        self.assertEqual(telemetry["steps"], 2)
        self.assertEqual(telemetry["first_step_input"], 44793)
        self.assertEqual(telemetry["source"], "kimi-wire-usage-record")

    def test_absent_or_oversized_wire_log_stays_unknown(self) -> None:
        root = make_root()
        missing = fleet.kimi_wire_telemetry(root / "kimi-home")
        self.assertIsNone(missing["token_usage"])
        self.assertEqual(missing["reason"], "kimi-wire-log-absent")
        home = root / "kimi-home-big"
        write_wire(home, "main", [kimi_usage(1, 1, 1)])
        with mock.patch.object(fleet, "KIMI_WIRE_MAX_BYTES", 8):
            big = fleet.kimi_wire_telemetry(home)
        self.assertIsNone(big["token_usage"])
        self.assertEqual(big["reason"], "kimi-wire-log-too-large")
        empty_home = root / "kimi-home-empty"
        write_wire(empty_home, "main", [kimi_step_end(1, 1, 1)])
        self.assertEqual(
            fleet.kimi_wire_telemetry(empty_home)["reason"],
            "kimi-wire-log-has-no-usage-record",
        )
        telemetry = fleet.context_telemetry([], adapter="kimi", kimi_wire=missing)
        self.assertEqual(telemetry["source"], "unknown")
        self.assertIsNone(telemetry["steps"])

    def test_reparse_backfills_historical_kimi_run_from_wire_log(self) -> None:
        root = make_root()
        run_id = "run-20260926T162146Z-0cf8fcc73a"
        output_dir = root / "evidence" / run_id
        output_dir.mkdir(parents=True)
        (output_dir / "stdout.txt").write_text(
            jsonl([{"role": "assistant", "content": "done"}]), encoding="utf-8"
        )
        write_wire(output_dir / "kimi-home", "main", [kimi_usage(100, 900, 20)])
        fleet.persist_run_record(root, {
            "schema_version": 5,
            "run_id": run_id,
            "adapter": "kimi",
            "agent": "kimi",
            "output_dir": str(output_dir),
            "token_usage": {"status": "unknown", "total_tokens": None},
        })
        updated = fleet.reparse_run_usage(root, run_id=run_id)
        self.assertEqual(updated["status"], "UPDATED")
        self.assertEqual(updated["token_usage"]["source"], "kimi-wire-log")
        self.assertEqual(updated["token_usage"]["total_tokens"], 1020)


class NativeContextControlTests(unittest.TestCase):
    def test_capability_map_is_explicit_and_evidenced(self) -> None:
        controls = fleet.NATIVE_CONTEXT_CONTROLS
        self.assertEqual(controls["mimo"]["compaction_max_context"]["status"], "supported")
        self.assertIn("MIMOCODE_COMPACTION_MAX_CONTEXT",
                      controls["mimo"]["compaction_max_context"]["control"])
        self.assertEqual(controls["codex"]["compaction_max_context"]["status"], "supported")
        self.assertIn("model_auto_compact_token_limit",
                      controls["codex"]["compaction_max_context"]["control"])
        self.assertEqual(controls["claude"]["compaction_max_context"]["status"], "supported")
        self.assertIn("--autocompact", controls["claude"]["compaction_max_context"]["control"])
        self.assertEqual(controls["grok"]["compaction_max_context"]["status"], "unsupported")
        self.assertEqual(controls["grok"]["turn_limit"]["status"], "supported")
        self.assertEqual(controls["qwen"]["compaction_max_context"]["status"], "unsupported")
        for adapter, entries in controls.items():
            for name, entry in entries.items():
                self.assertTrue(str(entry.get("evidence", "")).strip(), f"{adapter}.{name}")

    def test_shipped_profiles_activate_claude_and_mimo_budgets(self) -> None:
        payload = json.loads(Path(fleet.DEFAULT_PROFILES).read_text(encoding="utf-8"))
        for agent_id, profile in payload["profiles"].items():
            if agent_id == "mimo":
                self.assertEqual(profile[fleet.NATIVE_COMPACTION_PROFILE_FIELD], 180_000)
            elif agent_id == "claude":
                self.assertEqual(profile[fleet.NATIVE_COMPACTION_PROFILE_FIELD], 150_000)
            else:
                self.assertNotIn(fleet.NATIVE_COMPACTION_PROFILE_FIELD, profile, agent_id)
            self.assertNotIn("context_warn_tokens", profile, agent_id)
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        for agent_id, profile in profiles.items():
            record = fleet.native_context_budget_record(profile)
            if agent_id == "mimo":
                self.assertEqual(record["requested"], 180_000)
                self.assertEqual(record["status"], "applied")
                self.assertTrue(record["applied"])
            elif agent_id == "claude":
                self.assertEqual(record["requested"], 150_000)
                self.assertEqual(record["control"], "--autocompact <tokens>")
                self.assertTrue(record["applied"])
            else:
                self.assertEqual(record["status"], "not-configured")
                self.assertFalse(record["applied"])

    def load_mutated(self, mutate) -> dict:
        payload = json.loads(Path(fleet.DEFAULT_PROFILES).read_text(encoding="utf-8"))
        mutate(payload["profiles"])
        path = make_root() / "profiles.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return fleet.load_profiles(path)

    def test_profile_field_is_validated_at_load_time(self) -> None:
        field = fleet.NATIVE_COMPACTION_PROFILE_FIELD
        loaded = self.load_mutated(
            lambda profiles: profiles["mimo"].update({field: 180_000})
        )
        self.assertEqual(fleet.profile_native_compaction_max_context(loaded["mimo"]), 180_000)
        self.assertEqual(fleet.profile_native_compaction_max_context(loaded["claude"]), 150_000)
        for invalid in (99_999, 1_000_001):
            with self.assertRaisesRegex(fleet.FleetError, "100000..1000000"):
                self.load_mutated(
                    lambda profiles, value=invalid: profiles["claude"].update({field: value})
                )
        with self.assertRaisesRegex(fleet.FleetError, "unsupported for adapter grok"):
            self.load_mutated(lambda profiles: profiles["grok"].update({field: 180_000}))
        with self.assertRaisesRegex(fleet.FleetError, "unsupported for adapter qwen"):
            self.load_mutated(lambda profiles: profiles["qwen"].update({field: 180_000}))
        for invalid in (True, 1_000, "180000", 50_000_000):
            with self.assertRaisesRegex(fleet.FleetError, "must be an integer"):
                self.load_mutated(
                    lambda profiles, value=invalid: profiles["codex"].update({field: value})
                )
        with self.assertRaisesRegex(fleet.FleetError, "context_warn_tokens"):
            self.load_mutated(
                lambda profiles: profiles["mimo"].update({"context_warn_tokens": 0})
            )

    def codex_command(self, profile: dict, root: Path) -> list[str]:
        command, _, _ = fleet.build_command(
            "codex",
            "codex.exe",
            prompt="Review.",
            workspace=root,
            mode="review",
            model="gpt-6-luna",
            complexity="low",
            timeout=60,
            run_dir=root,
            native_subagent_limit=0,
            enable_web=False,
            materialize=False,
            profile=profile,
            codex_mcp_args=[],
        )
        return command

    def test_claude_budget_is_one_native_flag_and_absent_is_unchanged(self) -> None:
        root = make_root()
        def command(profile: dict) -> list[str]:
            argv, _, _ = fleet.build_command(
                "claude", "claude.exe", prompt="Review.", workspace=root,
                mode="review", model="sonnet", complexity="low", timeout=60,
                run_dir=root, native_subagent_limit=0, enable_web=False,
                materialize=False, profile=profile,
            )
            return argv
        unchanged = command({"adapter": "claude"})
        self.assertNotIn("--autocompact", unchanged)
        budgeted = command({"adapter": "claude", fleet.NATIVE_COMPACTION_PROFILE_FIELD: 150_000})
        index = budgeted.index("--autocompact")
        self.assertEqual(budgeted[index + 1], "150000")
        self.assertEqual(budgeted[:index] + budgeted[index + 2:], unchanged)

    def test_codex_budget_is_one_config_override_and_absent_is_unchanged(self) -> None:
        base = {"adapter": "codex"}
        root = make_root()
        unchanged = self.codex_command(base, root)
        self.assertEqual(unchanged, self.codex_command({}, root))
        self.assertFalse(any("model_auto_compact_token_limit" in arg for arg in unchanged))
        budgeted = self.codex_command(
            dict(base, **{fleet.NATIVE_COMPACTION_PROFILE_FIELD: 200_000}), root
        )
        index = budgeted.index("model_auto_compact_token_limit=200000")
        self.assertEqual(budgeted[index - 1], "-c")
        self.assertEqual(budgeted[-1], "-")
        # Exactly one inserted "-c key=value" pair; everything else identical.
        self.assertEqual(budgeted[: index - 1] + budgeted[index + 1 :], unchanged)

    def mimo_run(self, *, budget: int | None) -> tuple[dict, dict, dict]:
        root = make_root()
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
        if budget is not None:
            mimo[fleet.NATIVE_COMPACTION_PROFILE_FIELD] = budget
        else:
            # Exercise the backward-compatible absent-field path separately
            # from the shipped MiMo profile, which now opts in to compaction.
            mimo.pop(fleet.NATIVE_COMPACTION_PROFILE_FIELD, None)
        execution = routing_v2.resolve_execution_profile(
            "mimo", mimo, complexity="low", mode="review", required_tools=[]
        )
        captured: dict = {}
        stdout = jsonl([step_finish("s1", fresh=3183, read=4096),
                        step_finish("s2", fresh=100, read=160_000)])

        def fake_child(command, **kwargs):
            captured["env"] = dict(kwargs["env"])
            return {"child": {"exit_code": 0, "stdout": stdout, "stderr": "",
                              "timed_out": False, "model_tool_calls": None,
                              "tool_budget_exceeded": False},
                    "queue_wait_seconds": 0.0, "execution_serialized": False}

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
        return result, record, captured

    def test_mimo_budget_reaches_the_child_env_only_when_configured(self) -> None:
        _, record, captured = self.mimo_run(budget=180_000)
        self.assertEqual(captured["env"]["MIMOCODE_COMPACTION_MAX_CONTEXT"], "180000")
        self.assertEqual(record["native_context_budget"]["requested"], 180_000)
        self.assertTrue(record["native_context_budget"]["applied"])
        self.assertEqual(
            record["adapter_preflight"]["environment"]["MIMOCODE_COMPACTION_MAX_CONTEXT"],
            "180000",
        )
        # Telemetry is recorded and the warning does not stop the run.
        self.assertEqual(record["context_telemetry"]["steps"], 2)
        self.assertEqual(record["context_telemetry"]["max_step_input"], 160_100)
        self.assertTrue(record["context_snowball_warning"])
        self.assertIsNotNone(record["run_status"])
        _, plain, plain_env = self.mimo_run(budget=None)
        self.assertNotIn("MIMOCODE_COMPACTION_MAX_CONTEXT", plain_env["env"])
        self.assertEqual(plain["native_context_budget"]["status"], "not-configured")


class KimiRunRecordTests(unittest.TestCase):
    def test_kimi_run_record_gets_wire_usage_and_context_telemetry(self) -> None:
        root = make_root()
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        execution = routing_v2.resolve_execution_profile(
            "kimi", profiles["kimi"], complexity="low", mode="review", required_tools=[]
        )

        def fake_home(run_dir, **kwargs):
            home = run_dir / "kimi-home"
            home.mkdir(parents=True)
            return home

        def fake_child(command, **kwargs):
            home = Path(kwargs["env"]["KIMI_CODE_HOME"])
            write_wire(home, "main", [kimi_usage(10489, 34304, 130),
                                      kimi_step_end(10489, 34304, 130),
                                      kimi_usage(2000, 160000, 90)])
            stdout = jsonl([{"role": "assistant",
                             "content": json.dumps({"status": "completed"})}])
            return {"child": {"exit_code": 0, "stdout": stdout, "stderr": "",
                              "timed_out": False, "model_tool_calls": None,
                              "tool_budget_exceeded": False},
                    "queue_wait_seconds": 0.0, "execution_serialized": False}

        quota = {"kimi": {"status": "fresh", "headroom": 1.0, "confidence": 1.0}}
        patches = [
            mock.patch.dict(os.environ, {"AGENT_FLEET_DEPTH": "0", "AGENT_FLEET_OFFICE": "0",
                                         "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing.json")}),
            mock.patch.object(fleet, "resolve_executable", return_value="kimi.exe"),
            mock.patch.object(fleet, "probe_command", return_value={"launchable": True, "exit_code": 0, "output": "ok"}),
            mock.patch.object(fleet, "profile_credential_status", return_value={"ready": True}),
            mock.patch.object(fleet, "merge_credential_readiness", return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "readiness_from_probes", return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "runtime_model_visible", return_value=(True, "visible")),
            mock.patch.object(routing_v2, "quota_hard_gates", return_value=[]),
            mock.patch.object(fleet, "current_quota_views", return_value=(quota, {})),
            mock.patch.object(fleet, "prepare_guarded_kimi_home", side_effect=fake_home),
            mock.patch.object(fleet, "prepare_contractor_skills",
                              side_effect=lambda run_dir, **kw: run_dir / "approved-skills"),
            mock.patch.object(fleet, "run_provider_child", side_effect=fake_child),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            result = fleet.execute_single(
                "Review the synthetic fixture.",
                profiles=profiles, agent_id="kimi", mode="review", kind="review",
                complexity="low", model_override=None, workspace=root, timeout=60,
                allow_write=False, native_subagent_limit=0, enable_web=False,
                inherit_api_keys=False, dry_run=False, output_root=root / "evidence",
                root=root, execution_profile=execution, knowledge_mode="none",
            )
        record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
        usage = record["token_usage"]
        self.assertEqual(usage["status"], "known")
        self.assertEqual(usage["source"], "kimi-wire-log")
        self.assertEqual(usage["record_count"], 2)
        self.assertEqual(usage["fresh_input_tokens"], 12489)
        self.assertIsNone(usage["reasoning_tokens"])
        self.assertEqual(record["context_telemetry"]["steps"], 2)
        self.assertEqual(record["context_telemetry"]["max_step_input"], 162000)
        self.assertTrue(record["context_snowball_warning"])
        self.assertEqual(result["token_usage"], usage)


class QwenModelRegistryTests(unittest.TestCase):
    def settings_path(self, payload: dict) -> Path:
        home = make_root() / "qwen-home"
        home.mkdir()
        path = home / "settings.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def registration(self, model: str, *, payload=QWEN_SYNTHETIC_SETTINGS,
                     system: dict | None = None, env: dict | None = None,
                     workspace: Path | None = None) -> dict:
        return fleet.qwen_model_registration(
            model,
            system_settings=system if system is not None else {"model": {}, "modelFallbacks": ""},
            user_settings_path=self.settings_path(payload),
            env=env or {},
            workspace=workspace,
        )

    def test_unregistered_model_is_proven_to_fall_back_to_the_first_entry(self) -> None:
        flash = self.registration("qwen3.8-flash")
        self.assertEqual(flash["status"], "unregistered")
        self.assertEqual(flash["registry_default"], "qwen3.8-max")
        self.assertEqual(flash["auth_type"], "openai")
        self.assertEqual(flash["registry_source"], "qwen-user-settings")
        self.assertEqual(flash["registered_models"], ["qwen3.8-max", "glm-5"])
        self.assertNotIn("not-a-real-value", json.dumps(flash))
        self.assertEqual(self.registration("qwen3.8-max")["status"], "registered")

    def test_uncertain_configurations_are_unknown_not_blocked(self) -> None:
        cases = {
            "non-registry-credentials-present": dict(env={"OPENAI_BASE_URL": "https://x.invalid"}),
            "settings-credentials-present": dict(payload=dict(
                QWEN_SYNTHETIC_SETTINGS,
                security={"auth": {"selectedType": "openai", "baseUrl": "https://x.invalid"}})),
            "qwen-oauth-allowlist-not-modeled": dict(payload=dict(
                QWEN_SYNTHETIC_SETTINGS, security={"auth": {"selectedType": "qwen-oauth"}})),
            "auth-type-not-in-settings": dict(payload={
                "modelProviders": QWEN_SYNTHETIC_SETTINGS["modelProviders"]}),
            "registry-empty": dict(payload={"security": {"auth": {"selectedType": "openai"}}}),
        }
        for reason, kwargs in cases.items():
            with self.subTest(reason=reason):
                result = self.registration("qwen3.8-flash", **kwargs)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason"], reason)
        missing = fleet.qwen_model_registration(
            "qwen3.8-flash", system_settings={}, env={},
            user_settings_path=make_root() / "absent" / "settings.json",
        )
        self.assertEqual(missing["reason"], "user-settings-unavailable")
        workspace = make_root()
        (workspace / ".qwen").mkdir()
        (workspace / ".qwen" / "settings.json").write_text("{}", encoding="utf-8")
        self.assertEqual(
            self.registration("qwen3.8-flash", workspace=workspace)["reason"],
            "workspace-settings-present",
        )

    def test_fleet_system_registry_replaces_user_registry(self) -> None:
        system = {"modelProviders": {"openai": [{"id": "local-served-model"}]},
                  "security": {"auth": {"selectedType": "openai"}}}
        result = self.registration("local-served-model", system=system)
        self.assertEqual(result["status"], "registered")
        self.assertEqual(result["registry_source"], "fleet-system-settings")
        custom = dict(QWEN_SYNTHETIC_SETTINGS,
                      modelProviders={"bailian": [{"id": "qwen3.8-flash"}]},
                      providerProtocol={"bailian": "openai"})
        self.assertEqual(self.registration("qwen3.8-flash", payload=custom)["status"], "registered")

    def test_user_settings_path_honours_qwen_home(self) -> None:
        self.assertEqual(
            fleet.qwen_user_settings_path({"QWEN_HOME": "C:/qwen-fixture"}),
            Path("C:/qwen-fixture") / "settings.json",
        )
        self.assertEqual(
            fleet.qwen_user_settings_path({}), Path.home() / ".qwen" / "settings.json"
        )

    def qwen_run(self, settings: Path, stdout: str, *, create_run=None):
        root = make_root()
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        execution = routing_v2.resolve_execution_profile(
            "qwen", profiles["qwen"], complexity="medium", mode="advisory",
            required_tools=[],
        )
        self.assertEqual(execution["runtime_model_id"], "qwen3.8-flash")
        child = {"exit_code": 0, "stdout": stdout, "stderr": "", "timed_out": False,
                 "model_tool_calls": None, "tool_budget_exceeded": False}
        quota = {"qwen": {"status": "fresh", "headroom": 1.0, "confidence": 1.0}}
        provider = mock.MagicMock(return_value={"child": child, "queue_wait_seconds": 0.0,
                                                "execution_serialized": False})
        patches = [
            mock.patch.dict(os.environ, {"AGENT_FLEET_DEPTH": "0", "AGENT_FLEET_OFFICE": "0",
                                         "QWEN_HOME": str(settings.parent),
                                         "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing.json")}),
            mock.patch.object(fleet, "resolve_executable", return_value="qwen.cmd"),
            mock.patch.object(fleet, "probe_command", return_value={"launchable": True, "exit_code": 0, "output": "ok"}),
            mock.patch.object(fleet, "profile_credential_status", return_value={"ready": True}),
            mock.patch.object(fleet, "merge_credential_readiness", return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "readiness_from_probes", return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "runtime_model_visible", return_value=(True, "visible")),
            mock.patch.object(routing_v2, "quota_hard_gates", return_value=[]),
            mock.patch.object(fleet, "current_quota_views", return_value=(quota, {})),
            mock.patch.object(fleet, "run_provider_child", provider),
        ]
        if create_run is not None:
            patches.append(mock.patch.object(fleet, "create_run_dir", create_run))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            result = fleet.execute_single(
                "Advise on the synthetic fixture.",
                profiles=profiles, agent_id="qwen", mode="advisory", kind="research",
                complexity="medium", model_override=None, workspace=root, timeout=120,
                allow_write=False, native_subagent_limit=0, enable_web=False,
                inherit_api_keys=False, dry_run=False, output_root=root / "evidence",
                root=root, execution_profile=execution, knowledge_mode="none",
            )
        return result, provider

    def swapped_stream(self) -> str:
        # Replica of the 2026-09-26 failure shape (counters only): Qwen Code
        # reports the registry default in init and in every assistant message.
        return jsonl([
            {"type": "system", "subtype": "init", "model": "qwen3.8-max"},
            assistant("m1", {"input_tokens": 33353, "output_tokens": 1366,
                             "cache_read_input_tokens": 0}, model="qwen3.8-max"),
            {"type": "result", "subtype": "success", "result": "{}",
             "usage": {"input_tokens": 33353, "output_tokens": 1366}},
        ])

    def test_unregistered_model_fails_before_run_dir_and_provider_call(self) -> None:
        settings = self.settings_path(QWEN_SYNTHETIC_SETTINGS)
        create_run = mock.MagicMock(side_effect=AssertionError("run dir created"))
        with self.assertRaisesRegex(
            fleet.FleetError,
            "qwen3.8-flash is not registered.*registry default qwen3.8-max",
        ):
            self.qwen_run(settings, self.swapped_stream(), create_run=create_run)
        create_run.assert_not_called()

    def test_exact_model_gate_stays_fail_closed_when_registry_is_unknown(self) -> None:
        # No settings file: the preflight cannot prove anything, the run goes
        # ahead and the post-run exact-model gate still rejects the swap.
        absent = make_root() / "qwen-home" / "settings.json"
        result, provider = self.qwen_run(absent, self.swapped_stream())
        provider.assert_called_once()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("Qwen exact-model gate failed", str(result["handoff_parse_error"]))
        self.assertEqual(result["model_identity"]["observed_models"], ["qwen3.8-max"])
        self.assertEqual(
            result["adapter_preflight"]["model_registration"]["reason"],
            "user-settings-unavailable",
        )
        self.assertEqual(result["context_telemetry"]["first_step_input"], 33353)
        self.assertFalse(result["context_snowball_warning"])



class TelemetryReviewRegressionTests(unittest.TestCase):
    """Round-3 review: bounded counters, unknown input, empty and cut streams."""

    def test_huge_counters_are_unknown_and_never_overflow(self) -> None:
        huge = 10 ** 400
        stream = jsonl([
            step_finish("a", fresh=100, read=0),
            step_finish("b", fresh=huge, read=0),
        ])
        telemetry = fleet.context_telemetry([stream], adapter="mimo")
        self.assertEqual(telemetry["max_step_input"], 100)
        self.assertTrue(telemetry["incomplete"])
        self.assertIsNone(fleet.context_snowball_warning(telemetry))
        claude = jsonl([
            {"type": "system", "subtype": "init"},
            assistant("m1", {"input_tokens": 1, "cache_read_input_tokens": huge}),
            assistant("m2", {"input_tokens": huge}),
        ])
        # Must not raise OverflowError before the run record is written.
        fleet.context_telemetry([claude], adapter="claude")
        home = make_root() / "kimi-home"
        write_wire(home, "main", [kimi_usage(huge, 0, huge), kimi_usage(10, 0, 5)])
        wire = fleet.kimi_wire_telemetry(home)
        usage = wire["token_usage"]
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])
        self.assertIsNone(usage["total_tokens"])
        telemetry = fleet.context_telemetry([], adapter="kimi", kimi_wire=wire)
        self.assertEqual(telemetry["max_step_input"], 10)
        self.assertTrue(telemetry["incomplete"])
        self.assertIsNone(fleet.context_snowball_warning(telemetry))

    def test_kimi_output_only_record_leaves_input_unknown(self) -> None:
        home = make_root() / "kimi-home"
        write_wire(home, "main", [
            {"type": "usage.record", "agentId": "main", "usageScope": "turn",
             "usage": {"output": 100}},
        ])
        wire = fleet.kimi_wire_telemetry(home)
        usage = wire["token_usage"]
        self.assertEqual(usage["status"], "known")
        self.assertEqual(usage["output_tokens"], 100)
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["fresh_input_tokens"])
        self.assertIsNone(usage["total_tokens"])
        self.assertEqual(wire["steps"], [])
        self.assertTrue(wire["incomplete"])
        # A record missing only one input part is not a zero either.
        home = make_root() / "kimi-home"
        write_wire(home, "main", [
            kimi_usage(10, 20, 5),
            {"type": "usage.record", "agentId": "main", "usageScope": "turn",
             "usage": {"inputOther": 7, "output": 3}},
        ])
        usage = fleet.kimi_wire_telemetry(home)["token_usage"]
        self.assertEqual(usage["fresh_input_tokens"], 17)
        self.assertEqual(usage["output_tokens"], 8)
        self.assertIsNone(usage["cache_read_tokens"])
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["total_tokens"])

    def test_stream_without_step_measurements_reports_unknown_steps(self) -> None:
        cases = {
            "mimo": jsonl([tool_use("t1", "abc")]),
            "claude": jsonl([{"type": "system", "subtype": "init"}]),
            "qwen": jsonl([{"type": "system", "subtype": "init"},
                           assistant("m1", {"input_tokens": 0})]),
        }
        for adapter, stream in cases.items():
            with self.subTest(adapter=adapter):
                telemetry = fleet.context_telemetry([stream], adapter=adapter)
                self.assertIsNone(telemetry["steps"])
                self.assertIsNone(telemetry["max_step_input"])
                self.assertEqual(
                    telemetry["source"], f"{adapter}-output-without-per-step-usage"
                )
                self.assertIsNone(fleet.context_snowball_warning(telemetry))

    def test_truncated_stream_is_incomplete_and_does_not_clear_warning(self) -> None:
        small = jsonl([step_finish("a", fresh=100, read=0)])
        big = jsonl([step_finish("b", fresh=900_000, read=0)])
        with mock.patch.object(fleet, "CONTEXT_TELEMETRY_MAX_PARSE_CHARS", len(small) + 10):
            telemetry = fleet.context_telemetry([small + big], adapter="mimo")
        self.assertEqual(telemetry["max_step_input"], 100)
        self.assertTrue(telemetry["incomplete"])
        self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # A visible peak above the threshold stays a proven warning.
        with mock.patch.object(fleet, "CONTEXT_TELEMETRY_MAX_PARSE_CHARS", len(big) + 10):
            telemetry = fleet.context_telemetry([big + small], adapter="mimo")
        self.assertTrue(telemetry["incomplete"])
        self.assertTrue(fleet.context_snowball_warning(telemetry))
        complete = fleet.context_telemetry([small], adapter="mimo")
        self.assertFalse(complete["incomplete"])
        self.assertFalse(fleet.context_snowball_warning(complete))


class TelemetryReviewRound4Tests(unittest.TestCase):
    """Round-4 review: a line or counter that cannot be read is never a zero."""

    def test_partial_jsonl_line_marks_stream_incomplete(self) -> None:
        small = jsonl([step_finish("a", fresh=100, read=0)])
        cut = json.dumps(step_finish("b", fresh=900_000, read=0))[:40]
        for adapter, stream in {
            "mimo": small + cut + "\n",
            "claude": jsonl([
                {"type": "system", "subtype": "init"},
                assistant("m1", {"input_tokens": 100}),
            ]) + '{"type": "assistant", "message": {"id": "m2", "usa\n',
        }.items():
            with self.subTest(adapter=adapter):
                telemetry = fleet.context_telemetry([stream], adapter=adapter)
                self.assertEqual(telemetry["max_step_input"], 100)
                self.assertTrue(telemetry["incomplete"])
                self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # Non-JSON noise lines stay ignored and do not taint a full stream.
        noisy = "provider banner\n" + small
        telemetry = fleet.context_telemetry([noisy], adapter="mimo")
        self.assertFalse(telemetry["incomplete"])
        self.assertFalse(fleet.context_snowball_warning(telemetry))

    def test_claude_message_without_input_tokens_is_not_measured(self) -> None:
        stream = jsonl([
            {"type": "system", "subtype": "init"},
            assistant("m1", {"input_tokens": 100}),
            assistant("m2", {"cache_read_input_tokens": 50,
                             "cache_creation_input_tokens": 0}),
        ])
        for adapter in ("claude", "qwen"):
            with self.subTest(adapter=adapter):
                telemetry = fleet.context_telemetry([stream], adapter=adapter)
                self.assertEqual(telemetry["steps"], 1)
                self.assertEqual(telemetry["max_step_input"], 100)
                self.assertTrue(telemetry["incomplete"])
                self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # input_tokens reported by a repeated content block of the same
        # message still makes the message fully measured.
        repeated = jsonl([
            {"type": "system", "subtype": "init"},
            assistant("m1", {"cache_read_input_tokens": 50}),
            assistant("m1", {"input_tokens": 100, "cache_read_input_tokens": 50}),
        ])
        telemetry = fleet.context_telemetry([repeated], adapter="claude")
        self.assertEqual(telemetry["max_step_input"], 150)
        self.assertFalse(telemetry["incomplete"])
        self.assertFalse(fleet.context_snowball_warning(telemetry))

    def test_invalid_kimi_usage_record_makes_totals_unknown(self) -> None:
        home = make_root() / "kimi-home"
        write_wire(home, "main", [
            kimi_usage(100, 0, 10),
            {"type": "usage.record", "agentId": "main", "usageScope": "turn",
             "usage": {"inputOther": "bad", "output": -1}},
        ])
        wire = fleet.kimi_wire_telemetry(home)
        usage = wire["token_usage"]
        self.assertEqual(usage["record_count"], 2)
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])
        self.assertIsNone(usage["total_tokens"])
        self.assertTrue(wire["incomplete"])
        telemetry = fleet.context_telemetry([], adapter="kimi", kimi_wire=wire)
        self.assertEqual(telemetry["max_step_input"], 100)
        self.assertTrue(telemetry["incomplete"])
        self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # A usage.record whose usage is not an object is equally unreadable.
        home = make_root() / "kimi-home"
        write_wire(home, "main", [
            kimi_usage(100, 0, 10),
            {"type": "usage.record", "agentId": "main", "usage": "n/a"},
        ])
        wire = fleet.kimi_wire_telemetry(home)
        self.assertIsNone(wire["token_usage"]["input_tokens"])
        self.assertTrue(wire["incomplete"])
        # Only unreadable records: no usable usage at all.
        home = make_root() / "kimi-home"
        write_wire(home, "main", [
            {"type": "usage.record", "agentId": "main", "usage": {"output": "x"}},
        ])
        wire = fleet.kimi_wire_telemetry(home)
        self.assertIsNone(wire["token_usage"])
        self.assertEqual(wire["reason"], "kimi-wire-log-has-no-usable-usage-record")

    def test_partial_kimi_wire_line_is_incomplete(self) -> None:
        home = make_root() / "kimi-home"
        wire_path = write_wire(home, "main", [kimi_usage(100, 0, 10)])
        cut = json.dumps(kimi_usage(900_000, 0, 10))[:60]
        with wire_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(cut + "\n")
        wire = fleet.kimi_wire_telemetry(home)
        self.assertTrue(wire["incomplete"])
        # The cut line was a usage.record: the partial sum is not a total.
        self.assertIsNone(wire["token_usage"]["input_tokens"])
        telemetry = fleet.context_telemetry([], adapter="kimi", kimi_wire=wire)
        self.assertEqual(telemetry["max_step_input"], 100)
        self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # A cut non-usage line leaves totals intact but steps incomplete.
        home = make_root() / "kimi-home"
        wire_path = write_wire(home, "main", [kimi_usage(100, 0, 10)])
        with wire_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write('{"type": "context.append_loop_event", "event": {"ty\n')
        wire = fleet.kimi_wire_telemetry(home)
        self.assertTrue(wire["incomplete"])
        self.assertEqual(wire["token_usage"]["input_tokens"], 100)



class TelemetryReviewRound5Tests(unittest.TestCase):
    """Round-5 review: torn-only Kimi logs and the resumed Qwen worktree."""

    def test_kimi_log_with_only_a_truncated_usage_line_stays_incomplete(self) -> None:
        home = make_root() / "kimi-home"
        wire_path = write_wire(home, "main", [])
        cut = json.dumps(kimi_usage(900_000, 0, 10))[:60]
        self.assertIn('"usage.record"', cut)
        with wire_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(cut + "\n")
        wire = fleet.kimi_wire_telemetry(home)
        self.assertIsNone(wire["token_usage"])
        self.assertTrue(wire["incomplete"])
        telemetry = fleet.context_telemetry([], adapter="kimi", kimi_wire=wire)
        self.assertTrue(telemetry["incomplete"])
        self.assertIsNone(telemetry["max_step_input"])
        self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # A log holding only a torn line of unknown type is equally incomplete.
        home = make_root() / "kimi-home"
        wire_path = write_wire(home, "main", [])
        with wire_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write('{"type": "usage.rec\n')
        wire = fleet.kimi_wire_telemetry(home)
        self.assertTrue(wire["incomplete"])
        telemetry = fleet.context_telemetry([], adapter="kimi", kimi_wire=wire)
        self.assertTrue(telemetry["incomplete"])

    def test_kimi_line_cut_before_the_type_name_makes_totals_unknown(self) -> None:
        for cut in ('{"type": "usage.rec', '{"agentId": "main", "usage": {"inputOther": 9',
                    '{"ty'):
            with self.subTest(cut=cut):
                home = make_root() / "kimi-home"
                wire_path = write_wire(home, "main", [kimi_usage(100, 0, 10)])
                with wire_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(cut + "\n")
                wire = fleet.kimi_wire_telemetry(home)
                self.assertTrue(wire["incomplete"])
                self.assertIsNone(wire["token_usage"]["input_tokens"])
                self.assertIsNone(wire["token_usage"]["output_tokens"])
        # A torn line whose complete type proves it is not usage keeps totals.
        home = make_root() / "kimi-home"
        wire_path = write_wire(home, "main", [kimi_usage(100, 0, 10)])
        with wire_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write('{"type": "context.append_loop_event", "event": {"ty\n')
        wire = fleet.kimi_wire_telemetry(home)
        self.assertTrue(wire["incomplete"])
        self.assertEqual(wire["token_usage"]["input_tokens"], 100)

    def test_qwen_preflight_reads_the_resumed_execution_workspace(self) -> None:
        base = make_root()
        resumed = make_root()
        (resumed / ".qwen").mkdir()
        (resumed / ".qwen" / "settings.json").write_text("{}", encoding="utf-8")
        seen: list = []

        class Stop(Exception):
            pass

        def capture(*_args, **kwargs):
            seen.append(kwargs.get("workspace"))
            raise Stop()

        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        execution = routing_v2.resolve_execution_profile(
            "qwen", profiles["qwen"], complexity="medium", mode="implement",
            required_tools=[],
        )
        quota = {"qwen": {"status": "fresh", "headroom": 1.0, "confidence": 1.0}}
        patches = [
            mock.patch.dict(os.environ, {"AGENT_FLEET_DEPTH": "0", "AGENT_FLEET_OFFICE": "0",
                                         "QWEN_HOME": str(base / "qwen-home"),
                                         "AGENT_FLEET_BILLING_MODE_PATH": str(base / "missing.json")}),
            mock.patch.object(fleet, "resolve_executable", return_value="qwen.cmd"),
            mock.patch.object(fleet, "probe_command", return_value={"launchable": True, "exit_code": 0, "output": "ok"}),
            mock.patch.object(fleet, "profile_credential_status", return_value={"ready": True}),
            mock.patch.object(fleet, "merge_credential_readiness", return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "readiness_from_probes", return_value={"ready": True, "reasons": []}),
            mock.patch.object(routing_v2, "runtime_model_visible", return_value=(True, "visible")),
            mock.patch.object(routing_v2, "quota_hard_gates", return_value=[]),
            mock.patch.object(fleet, "current_quota_views", return_value=(quota, {})),
            mock.patch.object(fleet, "qwen_model_registration", side_effect=capture),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with self.assertRaises(Stop):
                fleet.execute_single(
                    "Implement the synthetic fixture.",
                    profiles=profiles, agent_id="qwen", mode="implement", kind="code",
                    complexity="medium", model_override=None, workspace=base, timeout=120,
                    allow_write=True, native_subagent_limit=0, enable_web=False,
                    inherit_api_keys=False, dry_run=False, output_root=base / "evidence",
                    root=base, execution_profile=execution, knowledge_mode="none",
                    existing_execution_workspace=resumed,
                    preserved_base_commit_sha="0" * 40,
                    preserved_base_provenance="recorded",
                )
        self.assertEqual(seen, [resumed])


class TelemetryReviewRound6Tests(unittest.TestCase):
    """Round-6 review: unmeasured messages, unknown Kimi scope, headless fragments."""

    def test_message_with_output_but_no_input_tokens_is_a_gap(self) -> None:
        stream = jsonl([
            {"type": "system", "subtype": "init"},
            assistant("m1", {"input_tokens": 100, "output_tokens": 5}),
            assistant("m2", {"output_tokens": 40}),
        ])
        for adapter in ("claude", "qwen"):
            with self.subTest(adapter=adapter):
                telemetry = fleet.context_telemetry([stream], adapter=adapter)
                self.assertEqual(telemetry["max_step_input"], 100)
                self.assertTrue(telemetry["incomplete"])
                self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # A zero-usage placeholder without input_tokens is still not a request.
        placeholder = jsonl([
            {"type": "system", "subtype": "init"},
            assistant("m1", {"input_tokens": 100, "output_tokens": 5}),
            assistant("m2", {"output_tokens": 0}),
        ])
        telemetry = fleet.context_telemetry([placeholder], adapter="claude")
        self.assertFalse(telemetry["incomplete"])
        self.assertFalse(fleet.context_snowball_warning(telemetry))

    def test_kimi_main_record_with_unknown_scope_is_a_gap(self) -> None:
        for scope in (["turn"], {"k": "turn"}, 7, "unexpected-scope"):
            with self.subTest(scope=scope):
                home = make_root() / "kimi-home"
                write_wire(home, "main", [
                    kimi_usage(100, 0, 10),
                    {"type": "usage.record", "agentId": "main", "usageScope": scope,
                     "usage": {"inputOther": 200_000, "inputCacheRead": 0,
                               "inputCacheCreation": 0, "output": 10}},
                ])
                wire = fleet.kimi_wire_telemetry(home)
                self.assertTrue(wire["incomplete"])
                telemetry = fleet.context_telemetry([], adapter="kimi", kimi_wire=wire)
                self.assertEqual(telemetry["max_step_input"], 100)
                self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # An unreadable agentId cannot prove the record is a sub-agent's.
        home = make_root() / "kimi-home"
        write_wire(home, "main", [
            kimi_usage(100, 0, 10),
            dict(kimi_usage(200_000, 0, 10), agentId=["main"]),
        ])
        self.assertTrue(fleet.kimi_wire_telemetry(home)["incomplete"])
        # Documented non-step scope and a sub-agent record stay clean.
        home = make_root() / "kimi-home"
        write_wire(home, "main", [
            kimi_usage(100, 0, 10),
            kimi_usage(500, 0, 7, scope="compaction"),
            kimi_usage(900, 0, 7, agent="sub"),
        ])
        wire = fleet.kimi_wire_telemetry(home)
        self.assertFalse(wire["incomplete"])
        telemetry = fleet.context_telemetry([], adapter="kimi", kimi_wire=wire)
        self.assertFalse(fleet.context_snowball_warning(telemetry))

    def test_jsonl_fragment_without_opening_brace_is_incomplete(self) -> None:
        small = jsonl([step_finish("a", fresh=100, read=0)])
        whole = json.dumps(step_finish("b", fresh=900_000, read=0))
        for fragment in (whole[1:], whole[whole.index('"part"'):],
                         'put": 900000, "cache": {"read": 0}}}}'):
            with self.subTest(fragment=fragment[:20]):
                telemetry = fleet.context_telemetry(
                    [small + fragment + "\n"], adapter="mimo"
                )
                self.assertEqual(telemetry["max_step_input"], 100)
                self.assertTrue(telemetry["incomplete"])
                self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # Plain provider banners are still ignored.
        for banner in ("provider banner", "Loading model: qwen3.8-max", "done."):
            with self.subTest(banner=banner):
                telemetry = fleet.context_telemetry([banner + "\n" + small], adapter="mimo")
                self.assertFalse(telemetry["incomplete"])
        # The same fragment in a Kimi wire log is an unread usage record.
        home = make_root() / "kimi-home"
        wire_path = write_wire(home, "main", [kimi_usage(100, 0, 10)])
        with wire_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(kimi_usage(900_000, 0, 10))[1:] + "\n")
        wire = fleet.kimi_wire_telemetry(home)
        self.assertTrue(wire["incomplete"])
        self.assertIsNone(wire["token_usage"]["input_tokens"])



class TelemetryReviewRound7Tests(unittest.TestCase):
    """Round-7 review: id-less usage, malformed step_finish, lone surrogates."""

    def test_assistant_usage_without_message_id_is_a_gap(self) -> None:
        stream = jsonl([
            {"type": "system", "subtype": "init"},
            assistant("m1", {"input_tokens": 100, "output_tokens": 5}),
            {"type": "assistant",
             "message": {"model": "synthetic-model", "content": [],
                         "usage": {"input_tokens": 900_000, "output_tokens": 5}}},
        ])
        for adapter in ("claude", "qwen"):
            with self.subTest(adapter=adapter):
                telemetry = fleet.context_telemetry([stream], adapter=adapter)
                self.assertEqual(telemetry["max_step_input"], 100)
                self.assertTrue(telemetry["incomplete"])
                self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # An unreadable usage object on an identified message is a gap too.
        broken = jsonl([
            {"type": "system", "subtype": "init"},
            assistant("m1", {"input_tokens": 100, "output_tokens": 5}),
            {"type": "assistant",
             "message": {"id": "m2", "content": [], "usage": "900000"}},
        ])
        self.assertTrue(fleet.context_telemetry([broken], adapter="claude")["incomplete"])
        # An id-less zero-usage placeholder is still not a request.
        placeholder = jsonl([
            {"type": "system", "subtype": "init"},
            assistant("m1", {"input_tokens": 100, "output_tokens": 5}),
            {"type": "assistant",
             "message": {"content": [], "usage": {"input_tokens": 0, "output_tokens": 0}}},
        ])
        telemetry = fleet.context_telemetry([placeholder], adapter="claude")
        self.assertFalse(telemetry["incomplete"])
        self.assertFalse(fleet.context_snowball_warning(telemetry))

    def test_step_finish_without_usable_part_is_a_gap(self) -> None:
        small = step_finish("a", fresh=100, read=0)
        for part in (None, "step-finish", ["x"], {"type": "unexpected"}):
            with self.subTest(part=part):
                broken = {"type": "step_finish"}
                if part is not None:
                    broken["part"] = part
                telemetry = fleet.context_telemetry(
                    [jsonl([small, broken])], adapter="mimo"
                )
                self.assertEqual(telemetry["max_step_input"], 100)
                self.assertTrue(telemetry["incomplete"])
                self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # Other part-less events stay ignored.
        telemetry = fleet.context_telemetry(
            [jsonl([small, {"type": "text"}])], adapter="mimo"
        )
        self.assertFalse(telemetry["incomplete"])

    def test_codex_mcp_result_with_lone_surrogate_does_not_raise(self) -> None:
        # Valid JSON whose escaped lone surrogate decodes to an unpaired
        # code point that strict UTF-8 encoding rejects.
        stream = (
            '{"type": "thread.started", "thread_id": "t"}\n'
            '{"type": "item.completed", "item": {"type": "mcp_tool_call", '
            '"result": {"content": [{"type": "text", "text": "a\\ud800b"}]}}}\n'
            '{"type": "turn.completed", "usage": {"input_tokens": 10, '
            '"cached_input_tokens": 0, "output_tokens": 1}}\n'
        )
        telemetry = fleet.context_telemetry([stream], adapter="codex")
        self.assertIsInstance(telemetry["tool_result_bytes"], int)
        self.assertGreater(telemetry["tool_result_bytes"], 0)
        self.assertIsNone(fleet.context_snowball_warning(telemetry))



def nested_json(depth: int, leaf: str = '"abc"') -> str:
    return "[" * depth + leaf + "]" * depth


def run_kimi_fixture(root: Path, wire_rows: list, *, extra_patches=(), wire_text: str = ""):
    """execute_single for Kimi with a synthetic wire log; returns (result, record)."""
    profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
    execution = routing_v2.resolve_execution_profile(
        "kimi", profiles["kimi"], complexity="low", mode="review", required_tools=[]
    )

    def fake_home(run_dir, **kwargs):
        home = run_dir / "kimi-home"
        home.mkdir(parents=True)
        return home

    def fake_child(command, **kwargs):
        home = Path(kwargs["env"]["KIMI_CODE_HOME"])
        wire = write_wire(home, "main", wire_rows)
        if wire_text:
            with wire.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(wire_text)
        stdout = jsonl([{"role": "assistant",
                         "content": json.dumps({"status": "completed"})}])
        return {"child": {"exit_code": 0, "stdout": stdout, "stderr": "",
                          "timed_out": False, "model_tool_calls": None,
                          "tool_budget_exceeded": False},
                "queue_wait_seconds": 0.0, "execution_serialized": False}

    quota = {"kimi": {"status": "fresh", "headroom": 1.0, "confidence": 1.0}}
    patches = [
        mock.patch.dict(os.environ, {"AGENT_FLEET_DEPTH": "0", "AGENT_FLEET_OFFICE": "0",
                                     "AGENT_FLEET_BILLING_MODE_PATH": str(root / "missing.json")}),
        mock.patch.object(fleet, "resolve_executable", return_value="kimi.exe"),
        mock.patch.object(fleet, "probe_command", return_value={"launchable": True, "exit_code": 0, "output": "ok"}),
        mock.patch.object(fleet, "profile_credential_status", return_value={"ready": True}),
        mock.patch.object(fleet, "merge_credential_readiness", return_value={"ready": True, "reasons": []}),
        mock.patch.object(routing_v2, "readiness_from_probes", return_value={"ready": True, "reasons": []}),
        mock.patch.object(routing_v2, "runtime_model_visible", return_value=(True, "visible")),
        mock.patch.object(routing_v2, "quota_hard_gates", return_value=[]),
        mock.patch.object(fleet, "current_quota_views", return_value=(quota, {})),
        mock.patch.object(fleet, "prepare_guarded_kimi_home", side_effect=fake_home),
        mock.patch.object(fleet, "prepare_contractor_skills",
                          side_effect=lambda run_dir, **kw: run_dir / "approved-skills"),
        mock.patch.object(fleet, "run_provider_child", side_effect=fake_child),
        *extra_patches,
    ]
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        result = fleet.execute_single(
            "Review the synthetic fixture.",
            profiles=profiles, agent_id="kimi", mode="review", kind="review",
            complexity="low", model_override=None, workspace=root, timeout=60,
            allow_write=False, native_subagent_limit=0, enable_web=False,
            inherit_api_keys=False, dry_run=False, output_root=root / "evidence",
            root=root, execution_profile=execution, knowledge_mode="none",
        )
    record = json.loads(Path(result["usage_record"]).read_text(encoding="utf-8"))
    return result, record


def injected_errors() -> list:
    return [
        RecursionError("synthetic"),
        UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed"),
        TypeError("synthetic"),
        ValueError("synthetic"),
        KeyError("synthetic"),
    ]


class TelemetryRobustnessClassTests(unittest.TestCase):
    """Class 1: warn-only telemetry never aborts execute_single or the record."""

    def test_deep_tool_result_is_counted_iteratively(self) -> None:
        # 900 levels parse as valid JSON; the old recursive counter raised
        # RecursionError on ~500 of them.
        deep = nested_json(900)
        stream = (
            jsonl([{"type": "system", "subtype": "init"},
                   assistant("m1", {"input_tokens": 100, "output_tokens": 5})])
            + '{"type": "user", "message": {"content": [{"type": "tool_result", '
            + '"content": ' + deep + '}]}}\n'
        )
        for adapter in ("claude", "qwen"):
            with self.subTest(adapter=adapter):
                telemetry = fleet.context_telemetry([stream], adapter=adapter)
                self.assertEqual(telemetry["tool_result_bytes"], 3)
                self.assertFalse(telemetry["incomplete"])
                self.assertIsNone(telemetry["reason"])
                self.assertFalse(fleet.context_snowball_warning(telemetry))
        mimo = jsonl([step_finish("a", fresh=100, read=0)]) + json.dumps(
            {"type": "tool_use", "part": {"type": "tool", "id": "t",
                                          "state": {"output": json.loads(deep)}}}
        ) + "\n"
        telemetry = fleet.context_telemetry([mimo], adapter="mimo")
        self.assertEqual(telemetry["tool_result_bytes"], 3)
        self.assertFalse(telemetry["incomplete"])
        home = make_root() / "kimi-home"
        write_wire(home, "main", [kimi_usage(100, 0, 10),
                                  kimi_tool_result(json.loads(deep))])
        wire = fleet.kimi_wire_telemetry(home)
        self.assertEqual(wire["tool_result_bytes"], 3)
        self.assertFalse(wire["incomplete"])

    def test_walk_bounds_degrade_to_incomplete_telemetry(self) -> None:
        # Deeper than any JSON line can be: built in memory, 50 000 levels.
        deep: list = ["abc"]
        for _ in range(50_000):
            deep = [deep]
        with self.assertRaises(fleet.ContextTelemetryLimit):
            fleet._utf8_size(deep)
        wide = ["x"] * (fleet.CONTEXT_TELEMETRY_MAX_WALK_NODES + 1)
        with self.assertRaises(fleet.ContextTelemetryLimit):
            fleet._utf8_size(wide)
        events = [{"type": "system", "subtype": "init"},
                  assistant("m1", {"input_tokens": 900_000, "output_tokens": 5}),
                  {"type": "user", "message": {"content": [
                      {"type": "tool_result", "content": deep}]}}]
        with mock.patch.object(fleet, "_telemetry_events", return_value=(events, False)):
            telemetry = fleet.context_telemetry(["x"], adapter="claude")
        self.assertTrue(telemetry["incomplete"])
        self.assertEqual(telemetry["reason"], "telemetry-error:ContextTelemetryLimit")
        self.assertIsNone(telemetry["max_step_input"])
        self.assertIsNone(fleet.context_snowball_warning(telemetry))

    def test_json_nested_5000_levels_is_incomplete_not_a_crash(self) -> None:
        deep = nested_json(5000)
        small = jsonl([step_finish("a", fresh=100, read=0)])
        claude_head = jsonl([{"type": "system", "subtype": "init"},
                             assistant("m1", {"input_tokens": 100, "output_tokens": 5})])
        streams = {
            "mimo": small + '{"type": "tool_use", "part": ' + deep + "}\n",
            "claude": claude_head + '{"type": "user", "message": {"content": ' + deep + "}}\n",
            "qwen": claude_head + '{"type": "assistant", "message": ' + deep + "}\n",
            "codex": '{"type": "turn.started"}\n{"type": "item.completed", "item": '
                     + deep + "}\n",
        }
        for adapter, stream in streams.items():
            with self.subTest(adapter=adapter):
                telemetry = fleet.context_telemetry([stream], adapter=adapter)
                self.assertTrue(telemetry["incomplete"])
                self.assertIsNone(fleet.context_snowball_warning(telemetry))
        home = make_root() / "kimi-home"
        wire_path = write_wire(home, "main", [kimi_usage(100, 0, 10)])
        with wire_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write('{"type": "usage.record", "usage": ' + deep + "}\n")
        wire = fleet.kimi_wire_telemetry(home)
        self.assertTrue(wire["incomplete"])
        telemetry = fleet.context_telemetry([], adapter="kimi", kimi_wire=wire)
        self.assertTrue(telemetry["incomplete"])
        self.assertIsNone(fleet.context_snowball_warning(telemetry))

    def test_lone_surrogate_in_tool_results_never_raises(self) -> None:
        claude = (
            jsonl([{"type": "system", "subtype": "init"},
                   assistant("m1", {"input_tokens": 100, "output_tokens": 5})])
            + '{"type": "user", "message": {"content": [{"type": "tool_result", '
            '"content": [{"type": "text", "text": "a\\ud800b"}]}]}}\n'
        )
        telemetry = fleet.context_telemetry([claude], adapter="claude")
        self.assertEqual(telemetry["tool_result_bytes"], 3)
        self.assertFalse(telemetry["incomplete"])
        mimo = jsonl([step_finish("a", fresh=100, read=0)]) + (
            '{"type": "tool_use", "part": {"type": "tool", "id": "t", '
            '"state": {"output": "x\\udfffy"}}}\n'
        )
        self.assertEqual(
            fleet.context_telemetry([mimo], adapter="mimo")["tool_result_bytes"], 3
        )
        home = make_root() / "kimi-home"
        wire_path = write_wire(home, "main", [kimi_usage(100, 0, 10)])
        with wire_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write('{"type": "context.append_loop_event", "agentId": "main", '
                         '"event": {"type": "tool.result", '
                         '"result": {"output": "q\\ud800"}}}\n')
        wire = fleet.kimi_wire_telemetry(home)
        self.assertEqual(wire["tool_result_bytes"], 2)
        self.assertFalse(wire["incomplete"])

    def test_any_parser_exception_degrades_to_incomplete_telemetry(self) -> None:
        streams = {
            "_step_finish_telemetry": ("mimo", jsonl([step_finish("a", fresh=1, read=0)])),
            "_message_stream_telemetry": ("claude", jsonl([{"type": "system", "subtype": "init"}])),
            "_codex_exec_telemetry": ("codex", jsonl([{"type": "turn.started"}])),
            "_telemetry_events": ("qwen", "{}"),
        }
        for target, (adapter, stream) in streams.items():
            for error in injected_errors():
                with self.subTest(target=target, error=type(error).__name__):
                    with mock.patch.object(fleet, target, side_effect=error):
                        telemetry = fleet.context_telemetry([stream], adapter=adapter)
                    self.assertTrue(telemetry["incomplete"])
                    self.assertEqual(
                        telemetry["reason"], f"telemetry-error:{type(error).__name__}"
                    )
                    self.assertIsNone(telemetry["max_step_input"])
                    self.assertIsNone(fleet.context_snowball_warning(telemetry))
        home = make_root() / "kimi-home"
        write_wire(home, "main", [kimi_usage(100, 0, 10)])
        for error in injected_errors():
            with self.subTest(target="kimi-json", error=type(error).__name__):
                with mock.patch.object(fleet.json, "loads", side_effect=error):
                    wire = fleet.kimi_wire_telemetry(home)
                # Parse-family errors are torn lines; any other failure is a
                # telemetry error. Both leave the log incomplete.
                self.assertTrue(wire["incomplete"])
                self.assertIsNone(wire["token_usage"])
                telemetry = fleet.context_telemetry([], adapter="kimi", kimi_wire=wire)
                self.assertTrue(telemetry["incomplete"])
                self.assertIsNone(fleet.context_snowball_warning(telemetry))
        with mock.patch.object(fleet, "kimi_wire_files", side_effect=TypeError("x")):
            wire = fleet.kimi_wire_telemetry(home)
        self.assertEqual(wire["reason"], "telemetry-error:TypeError")
        telemetry = fleet.context_telemetry([], adapter="kimi", kimi_wire=wire)
        self.assertEqual(telemetry["reason"], "telemetry-error:TypeError")
        self.assertTrue(telemetry["incomplete"])
        # A broken telemetry dict never makes the warning raise.
        self.assertIsNone(fleet.context_snowball_warning({"max_step_input": 5}))
        self.assertIsNone(fleet.context_snowball_warning(
            {"max_step_input": 5, "warn_tokens": "x"}))

    def test_execute_single_writes_the_run_record_when_telemetry_fails(self) -> None:
        for target in ("_kimi_wire_telemetry_measured", "_context_telemetry_measured"):
            for error in injected_errors():
                with self.subTest(target=target, error=type(error).__name__):
                    root = make_root()
                    result, record = run_kimi_fixture(
                        root, [kimi_usage(100, 0, 10)],
                        extra_patches=[mock.patch.object(fleet, target, side_effect=error)],
                    )
                    reason = f"telemetry-error:{type(error).__name__}"
                    self.assertTrue(record["context_telemetry"]["incomplete"])
                    self.assertEqual(record["context_telemetry"]["reason"], reason)
                    self.assertIsNone(record["context_snowball_warning"])
                    self.assertEqual(result["context_telemetry"], record["context_telemetry"])
                    if target == "_kimi_wire_telemetry_measured":
                        self.assertEqual(record["token_usage"].get("unknown_reason"), reason)
        # A deep wire tool result and a 5000-level usage line: record written.
        root = make_root()
        deep = json.loads(nested_json(900))
        result, record = run_kimi_fixture(
            root,
            [kimi_usage(100, 0, 10), kimi_tool_result(deep)],
            wire_text='{"type": "usage.record", "usage": ' + nested_json(5000) + "}\n",
        )
        telemetry = record["context_telemetry"]
        self.assertTrue(telemetry["incomplete"])
        self.assertIsNone(telemetry["reason"])
        self.assertEqual(telemetry["max_step_input"], 100)
        self.assertEqual(telemetry["tool_result_bytes"], 3)
        self.assertIsNone(record["context_snowball_warning"])


class TelemetryCompletenessClassTests(unittest.TestCase):
    """Class 2: every step-like event must be fully measured, per parser."""

    def test_counter_rule(self) -> None:
        rule = fleet._telemetry_steps_result
        self.assertEqual(rule({"a": None, "b": None}, {"a": 5, "b": 7}, set(), 0), ([5, 7], False))
        self.assertEqual(rule({"a": None, "b": None}, {"a": 5}, set(), 0), ([5], True))
        self.assertEqual(rule({"a": None}, {"a": 5}, {"a"}, 0), ([], True))
        self.assertEqual(rule({"a": None}, {"a": 5}, set(), 1), ([5], True))
        self.assertEqual(rule({}, {}, set(), 0), ([], False))

    def test_message_stream_unreadable_message_or_missing_usage_is_a_gap(self) -> None:
        head = [{"type": "system", "subtype": "init"},
                assistant("m1", {"input_tokens": 100, "output_tokens": 5})]
        gaps = {
            "message-not-object": {"type": "assistant", "message": "oops"},
            "message-missing": {"type": "assistant"},
            "usage-missing": {"type": "assistant",
                              "message": {"id": "m2", "content": [{"type": "text", "text": "hi"}]}},
            "usage-empty": assistant("m2", {}),
            "zero-input-positive-output": assistant("m2", {"input_tokens": 0, "output_tokens": 40}),
        }
        for adapter in ("claude", "qwen"):
            for name, event in gaps.items():
                with self.subTest(adapter=adapter, case=name):
                    telemetry = fleet.context_telemetry([jsonl(head + [event])], adapter=adapter)
                    self.assertEqual(telemetry["max_step_input"], 100)
                    self.assertTrue(telemetry["incomplete"])
                    self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # Qwen input already includes cache: 0 input next to cache is a gap;
        # for Claude the same event is a measured 50-token step.
        cached = assistant("m2", {"input_tokens": 0, "cache_read_input_tokens": 50,
                                  "output_tokens": 5})
        qwen = fleet.context_telemetry([jsonl(head + [cached])], adapter="qwen")
        self.assertTrue(qwen["incomplete"])
        claude = fleet.context_telemetry([jsonl(head + [cached])], adapter="claude")
        self.assertFalse(claude["incomplete"])
        self.assertEqual(claude["steps"], 2)

    def test_message_measured_by_a_later_event_of_the_same_id_is_complete(self) -> None:
        stream = jsonl([
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"id": "m1", "content": []}},
            assistant("m1", {"input_tokens": 0, "output_tokens": 0}),
            assistant("m1", {"input_tokens": 100, "output_tokens": 5}),
        ])
        telemetry = fleet.context_telemetry([stream], adapter="claude")
        self.assertEqual(telemetry["steps"], 1)
        self.assertFalse(telemetry["incomplete"])
        self.assertFalse(fleet.context_snowball_warning(telemetry))
        # A never-measured id stays a gap to the end of the stream.
        stream = jsonl([
            {"type": "system", "subtype": "init"},
            assistant("m1", {"input_tokens": 100, "output_tokens": 5}),
            {"type": "assistant", "message": {"id": "m2", "content": []}},
            assistant("m3", {"input_tokens": 120, "output_tokens": 5}),
        ])
        telemetry = fleet.context_telemetry([stream], adapter="qwen")
        self.assertEqual(telemetry["steps"], 2)
        self.assertTrue(telemetry["incomplete"])

    def test_step_finish_without_id_or_input_is_a_gap(self) -> None:
        small = step_finish("a", fresh=100, read=0)
        no_id = step_finish("x", fresh=900_000, read=0)
        del no_id["part"]["id"]
        zero_input = step_finish("b", fresh=0, read=0, output=40)
        no_tokens = {"type": "step_finish", "part": {"type": "step-finish", "id": "c"}}
        bad_output = step_finish("d", fresh=10, read=0)
        bad_output["part"]["tokens"]["output"] = "many"
        bad_cache = step_finish("e", fresh=10, read=0)
        bad_cache["part"]["tokens"]["cache"] = [1]
        for name, event in {"no-id": no_id, "zero-input": zero_input,
                            "no-tokens": no_tokens, "bad-output": bad_output,
                            "bad-cache": bad_cache}.items():
            for adapter in ("mimo", "opencode"):
                with self.subTest(case=name, adapter=adapter):
                    telemetry = fleet.context_telemetry([jsonl([small, event])], adapter=adapter)
                    self.assertEqual(telemetry["max_step_input"], 100)
                    self.assertTrue(telemetry["incomplete"])
                    self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # messageID is an accepted id; an all-zero step stays a measured 0.
        by_message = step_finish("ignored", fresh=300, read=0)
        by_message["part"]["messageID"] = by_message["part"].pop("id")
        zero = step_finish("z", fresh=0, read=0, output=0)
        telemetry = fleet.context_telemetry([jsonl([small, by_message, zero])], adapter="mimo")
        self.assertEqual(telemetry["steps"], 3)
        self.assertEqual(telemetry["max_step_input"], 300)
        self.assertFalse(telemetry["incomplete"])

    def test_kimi_zero_input_with_output_is_a_gap(self) -> None:
        home = make_root() / "kimi-home"
        write_wire(home, "main", [kimi_usage(100, 0, 10), kimi_usage(0, 0, 40)])
        wire = fleet.kimi_wire_telemetry(home)
        self.assertTrue(wire["incomplete"])
        self.assertEqual(wire["steps"], [100])
        telemetry = fleet.context_telemetry([], adapter="kimi", kimi_wire=wire)
        self.assertIsNone(fleet.context_snowball_warning(telemetry))
        # A missing input part on a main step is a gap; sub-agent records
        # and documented non-step scopes are not step-like.
        home = make_root() / "kimi-home"
        write_wire(home, "main", [
            kimi_usage(100, 0, 10),
            {"type": "usage.record", "agentId": "main", "usageScope": "turn",
             "usage": {"inputOther": 5, "output": 1}},
        ])
        self.assertTrue(fleet.kimi_wire_telemetry(home)["incomplete"])
        home = make_root() / "kimi-home"
        write_wire(home, "main", [
            kimi_usage(100, 0, 10),
            kimi_usage(0, 0, 40, agent="sub"),
            kimi_usage(0, 0, 40, scope="compaction"),
        ])
        self.assertFalse(fleet.kimi_wire_telemetry(home)["incomplete"])

    def test_codex_turn_without_readable_usage_is_incomplete(self) -> None:
        base = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}]
        good = {"type": "turn.completed",
                "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 1}}
        telemetry = fleet.context_telemetry([jsonl(base + [good])], adapter="codex")
        self.assertFalse(telemetry["incomplete"])
        for name, event in {
            "no-usage": {"type": "turn.completed"},
            "usage-not-object": {"type": "turn.completed", "usage": "10"},
            "no-input": {"type": "turn.completed", "usage": {"output_tokens": 1}},
            "bad-output": {"type": "turn.completed",
                           "usage": {"input_tokens": 10, "output_tokens": -1}},
        }.items():
            with self.subTest(case=name):
                telemetry = fleet.context_telemetry(
                    [jsonl(base + [good, event])], adapter="codex"
                )
                self.assertTrue(telemetry["incomplete"])
                self.assertIsNone(telemetry["max_step_input"])
                self.assertIsNone(fleet.context_snowball_warning(telemetry))

if __name__ == "__main__":
    unittest.main()
