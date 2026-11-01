"""Regression coverage for native bounds and per-step provider accounting."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adaptive
import fleet


class BudgetRuntimeTests(unittest.TestCase):
    def test_opencode_sums_all_steps_and_deduplicates_ids(self):
        def step(identity, total, output, cache):
            return {"type": "step_finish", "part": {"id": identity,
                "tokens": {"input": 4169, "output": output, "reasoning": 20,
                           "cache": {"read": cache, "write": 0}, "total": total},
                "cost": 0.01}}
        first, last = step("a", 4252, 63, 0), step("b", 31321, 1706, 26368)
        payload = "\n".join(json.dumps(row) for row in (first, last, last))
        usage = adaptive.extract_token_usage(payload, adapter="opencode")
        self.assertEqual(usage["total_tokens"], 35573)
        self.assertEqual(usage["output_tokens"], 1769)
        self.assertEqual(usage["fresh_input_tokens"], 8338)
        self.assertEqual(usage["cache_read_tokens"], 26368)
        self.assertEqual(usage["cost_usd"], 0.02)

    def test_qwen_message_update_does_not_duplicate_and_cache_is_not_fresh(self):
        def message(output):
            return {"type": "assistant", "message": {"id": "a", "usage": {
                "input_tokens": 1000, "output_tokens": output,
                "cache_read_input_tokens": 900, "total_tokens": 1000 + output}}}
        usage = adaptive.extract_token_usage("\n".join(json.dumps(message(n)) for n in (10, 30, 10)), adapter="qwen")
        self.assertEqual(usage["total_tokens"], 1030)
        self.assertEqual(usage["fresh_input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 30)
        self.assertIsNone(usage["cost_usd"])

    def test_missing_usage_remains_unknown(self):
        for adapter in ("opencode", "qwen", "muse", "minimax"):
            with self.subTest(adapter=adapter):
                usage = adaptive.extract_token_usage('{"type":"step_finish","part":{}}', adapter=adapter)
                self.assertEqual(usage["status"], "unknown")
                self.assertIsNone(usage["total_tokens"])

    def test_standard_claude_explicit_bound_selects_stream_and_verbose(self):
        with tempfile.TemporaryDirectory() as tmp:
            command, _, _ = fleet.build_command(
                "claude", "claude", prompt="bounded fixture", workspace=Path(tmp), model="claude-fable-5-1",
                mode="review", complexity="low", timeout=60, run_dir=Path(tmp),
                native_subagent_limit=0, enable_web=False, materialize=False,
                max_model_tool_calls=120)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", command)
        self.assertNotIn("--dangerously-skip-permissions", command)

    def test_native_caps_override_lower_and_higher_complexity_defaults(self):
        models = {'qwen': ('qwen3.8-max', '--max-tool-calls'),
                  'minimax': ('MiniMax-M3', '--max-steps'),
                  'muse': ('muse-spark-1.3', '--max-model-steps')}
        with tempfile.TemporaryDirectory() as tmp:
            for adapter, (model, flag) in models.items():
                for cap in (2, 120):
                    with self.subTest(adapter=adapter, cap=cap):
                        command, _, _ = fleet.build_command(adapter, adapter,
                            prompt='fixture', workspace=Path(tmp), mode='review', model=model,
                            complexity='medium', timeout=60, run_dir=Path(tmp),
                            native_subagent_limit=0, enable_web=False, materialize=False,
                            max_model_tool_calls=cap)
                        self.assertEqual(int(command[command.index(flag) + 1]), cap)

    def test_standard_native_defaults_are_high_guardrails(self):
        models = {
            'qwen': ('qwen3.8-max', '--max-tool-calls', 256),
            'minimax': ('MiniMax-M3', '--max-steps', 256),
            'muse': ('muse-spark-1.3', '--max-model-steps', 256),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for adapter, (model, flag, expected) in models.items():
                with self.subTest(adapter=adapter):
                    command, _, _ = fleet.build_command(
                        adapter, adapter, prompt='fixture', workspace=Path(tmp),
                        mode='review', model=model, complexity='medium', timeout=60,
                        run_dir=Path(tmp), native_subagent_limit=0, enable_web=False,
                        materialize=False,
                    )
                    self.assertEqual(int(command[command.index(flag) + 1]), expected)
        self.assertEqual(
            fleet.qwen_control_policy(
                model='qwen3.8-max', reasoning_level='medium', complexity='medium',
                timeout=60, max_model_tool_calls=None,
            )['turn_limit'],
            128,
        )

    def test_explicit_local_qwen_bound_is_not_silently_clamped(self):
        self.assertEqual(fleet.qwen_tool_limit('high', 600, local=True), 600)

    def test_required_inputs_preflight_verifies_hash_and_actual_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = workspace / 'fixture.txt'
            source.write_text('synthetic fixture\n', encoding='utf-8')
            manifest = workspace / 'required.json'
            manifest.write_text(json.dumps({'schema_version': 1, 'inputs': [{
                'path': 'fixture.txt', 'sha256': fleet.sha256_bytes(source.read_bytes()),
            }]}), encoding='utf-8')
            _, entries = fleet.load_required_inputs_manifest(manifest)
            verified = fleet.preflight_required_inputs(workspace, entries)
            self.assertEqual(verified[0]['path'], 'fixture.txt')
            self.assertEqual(verified[0]['sha256'], entries[0]['sha256'])

    def test_required_inputs_preflight_rejects_missing_traversal_and_secret_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with self.assertRaisesRegex(fleet.FleetError, 'missing'):
                fleet.preflight_required_inputs(
                    workspace, [{'path': 'missing.txt', 'sha256': 'a' * 64}]
                )
            manifest = workspace / 'required.json'
            manifest.write_text(json.dumps({'inputs': [{
                'path': '../outside.txt', 'sha256': 'a' * 64,
            }]}), encoding='utf-8')
            with self.assertRaisesRegex(fleet.FleetError, 'relative'):
                fleet.load_required_inputs_manifest(manifest)
            manifest.write_text(json.dumps({'inputs': [{
                'path': 'C:relative.txt', 'sha256': 'a' * 64,
            }]}), encoding='utf-8')
            with self.assertRaisesRegex(fleet.FleetError, 'Windows path'):
                fleet.load_required_inputs_manifest(manifest)
            manifest.write_text(json.dumps({'inputs': [{
                'path': 'report.txt:secret', 'sha256': 'a' * 64,
            }]}), encoding='utf-8')
            with self.assertRaisesRegex(fleet.FleetError, 'Windows path'):
                fleet.load_required_inputs_manifest(manifest)
            manifest.write_text(json.dumps({'inputs': [{
                'path': 'secrets/token.txt', 'sha256': 'a' * 64,
            }]}), encoding='utf-8')
            with self.assertRaisesRegex(fleet.FleetError, 'secret'):
                fleet.load_required_inputs_manifest(manifest)

    def test_standard_codex_command_and_watcher_have_incremental_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            command, _, _ = fleet.build_command('codex', 'codex', prompt='fixture',
                workspace=Path(tmp), mode='review', model='gpt-5.6-luna', complexity='low',
                timeout=60, run_dir=Path(tmp), native_subagent_limit=0,
                enable_web=False, materialize=False, max_model_tool_calls=2)
        self.assertIn('--json', command)
        event = json.dumps({'type':'item.started','item':{'id':'one','type':'command_execution'}})
        self.assertEqual(fleet.model_tool_event_ids(event, 'codex'), {'codex:one'})


if __name__ == "__main__":
    unittest.main()
