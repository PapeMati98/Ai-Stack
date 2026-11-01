"""Controlled execution of Fleet's real preflight and final binding block.

This is not a substitute for full Fleet/provider integration.
"""
from pathlib import Path
from types import SimpleNamespace
import textwrap
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'fleet-suite/payload/agent-fleet/scripts/fleet.py'


class ControllerFlow(unittest.TestCase):
    def run_flow(self, failure=None, entries=True):
        source = SOURCE.read_text(encoding='utf-8')
        start = source.index('    required_inputs = (', source.index('def execute_single('))
        end = source.index('    local_runtime_state: dict[str, Any] | None = None', start)
        block = textwrap.dedent(source[start:end])
        events = []
        records = [{'path': 'my_secret.py', 'sha256': 'a' * 64, 'size_bytes': 1}]

        class Refused(Exception):
            pass

        class Tools:
            def validate_preflight_paths(self, *, workspace, entries):
                events.append('paths')
                if failure == 'paths':
                    raise ValueError('hard path')

            def create_verified_read_inputs(self, **kw):
                events.append('factory')
                if not kw['records'] or failure == 'factory':
                    raise ValueError('missing records')
                assert kw['records'] == records
                return object()

        def preflight(workspace, rows):
            events.append('preflight')
            if failure == 'preflight':
                raise Refused('hash mismatch')
            return records

        def bind(task, **kw):
            events.append('final')
            assert kw['verified_read_inputs'] is not None
            return {'active': True}

        env = dict(adapter_id='mistral', dry_run=False, execution_workspace=Path('ws'),
                   run_dir=Path('run'), mode='implement', task='task',
                   mistral_tool_dispatch={'scope': ['out.py'], 'inputs': ['my_secret.py'], 'checks': []},
                   max_model_tool_calls=3, chrome_assignment=None,
                   required_inputs_manifest_path=Path('manifest'),
                   required_input_entries=[{'path': 'my_secret.py', 'sha256': 'a' * 64}] if entries else [],
                   preflight_required_inputs=preflight, FleetError=Refused,
                   mistral_runtime=SimpleNamespace(tool_module=lambda: Tools(), bind_tool_policy=bind,
                                                   MistralRuntimeError=Refused))
        try:
            exec(compile(block, str(SOURCE), 'exec'), env)
        except Refused:
            return events, env, False
        return events, env, True

    def test_success(self):
        events, env, success = self.run_flow()
        self.assertTrue(success)
        self.assertEqual(events, ['paths', 'preflight', 'factory', 'final'])
        self.assertEqual(env['required_inputs']['status'], 'VERIFIED')
        self.assertEqual(env['mistral_bound_policy'], {'active': True})

    def test_hard_path_refused_before_preflight(self):
        events, _, success = self.run_flow('paths')
        self.assertFalse(success)
        self.assertEqual(events, ['paths'])

    def test_preflight_failure_refuses_before_factory(self):
        events, _, success = self.run_flow('preflight')
        self.assertFalse(success)
        self.assertEqual(events, ['paths', 'preflight'])

    def test_missing_records_refuses_before_binding(self):
        events, _, success = self.run_flow(entries=False)
        self.assertFalse(success)
        self.assertEqual(events, ['factory'])


class NativeControllerPreparation(unittest.TestCase):
    def controller_slice(self, *, dry_run):
        import hashlib
        import json
        import sys
        import tempfile
        import uuid

        scripts = str(SOURCE.parent)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import fleet
        import mistral_runtime as runtime

        tools = runtime.tool_module()
        root = Path(tempfile.gettempdir()).resolve() / ('fleet-controller-synthetic-' + uuid.uuid4().hex)
        root.mkdir(exist_ok=False)
        workspace, run_dir = root / 'workspace', root / 'run'
        workspace.mkdir()
        run_dir.mkdir()
        (workspace / 'input.txt').write_text('synthetic\n', encoding='utf-8')
        policy = tools.build_policy(workspace=workspace, run_dir=run_dir, mode='review',
                                    scope=[], inputs=['input.txt'], checks=[])
        if dry_run:
            policy = runtime.bind_tool_policy(
                'synthetic', workspace=workspace, run_dir=run_dir, mode='review',
                dispatch={'scope': [], 'inputs': ['input.txt'], 'checks': []}, preview=True,
                source_workspace=workspace,
            )
        profile = json.loads(fleet.DEFAULT_PROFILES.read_text(encoding='utf-8'))['profiles']['mistral']
        profile['candidates'] = [runtime.MISTRAL_NATIVE_EXECUTABLE]
        source = SOURCE.read_text(encoding='utf-8')
        begin = source.index('    zcode_export = {}', source.index('def execute_single('))
        end = source.index('    effective_max_prompt_bytes, effective_max_model_tool_calls =', begin)
        block = textwrap.dedent(source[begin:end])
        env = dict(build_command=fleet.build_command, mistral_runtime=runtime,
                   FleetError=fleet.FleetError, adapter_id='mistral',
                   executable=runtime.MISTRAL_NATIVE_EXECUTABLE, prompt='synthetic',
                   execution_workspace=workspace, mode='review',
                   runtime_model=runtime.MISTRAL_NATIVE_MODEL_ID, complexity='low', timeout=120,
                   run_dir=run_dir, native_subagent_limit=0, enable_web=False, dry_run=dry_run,
                   runtime_profile='standard', max_model_tool_calls=None, command_handoff_schema=None,
                   selected_execution={'reasoning_level': 'off'}, profile=profile,
                   office_mcp_config=None, codex_mcp_args=None, chrome_assignment=None,
                   mistral_bound_policy=policy, zcode_bound_policy=None)
        # This executes the real controller statements and real command builder.
        # No SDK, native login, command launch or provider is invoked.
        exec(compile(block, str(SOURCE), 'exec'), env)
        return env, run_dir, tools

    def test_real_command_slice_prepares_bound_policy_before_build(self):
        import hashlib
        env, run_dir, tools = self.controller_slice(dry_run=False)
        policy_path = run_dir / tools.POLICY_FILENAME
        self.assertTrue(policy_path.is_file())
        command = env['command']
        index = command.index(str(policy_path))
        self.assertEqual(command[index + 1], hashlib.sha256(policy_path.read_bytes()).hexdigest())
        self.assertEqual(env['run_home'], run_dir / 'mistral-workspace' / '.vibe')
        self.assertEqual(env['stdin_text'], None)
        self.assertFalse((run_dir / 'mistral-output').exists())

    def test_real_command_slice_dry_run_never_prepares_or_builds(self):
        env, run_dir, tools = self.controller_slice(dry_run=True)
        self.assertEqual(env['command'], [])
        self.assertIsNone(env['stdin_text'])
        self.assertIsNone(env['final_message'])
        self.assertIsNone(env['run_home'])
        self.assertFalse((run_dir / tools.POLICY_FILENAME).exists())
        self.assertFalse((run_dir / 'mistral-workspace').exists())
        self.assertFalse((run_dir / 'mistral-prompt.txt').exists())


class ProfilePathMaterialization(unittest.TestCase):
    def setUp(self):
        import os
        import re
        from typing import Any
        from unittest.mock import patch
        self.os, self.patch = os, patch
        self.root = str(Path.cwd().resolve())
        source = SOURCE.read_text(encoding='utf-8')
        start = source.index('def materialize_profile_path(')
        end = source.index('def expand_candidate(', start)
        self.env = dict(os=os, re=re, Path=Path, Any=Any,
                        FleetError=type('Refusal', (Exception,), {}),
                        PROFILE_ID_PATTERN=re.compile(r'^[a-z0-9-]+$'))
        exec(compile(source[start:end], str(SOURCE), 'exec'), self.env)

    def test_allowed_and_absolute_paths(self):
        with self.patch.dict(self.os.environ, {'USERPROFILE': self.root, 'LOCALAPPDATA': self.root}):
            expand = self.env['materialize_profile_path']
            self.assertEqual(expand('~/bin/tool'), self.root + '/bin/tool')
            self.assertEqual(expand('%LOCALAPPDATA%/bin/tool'), self.root + '/bin/tool')
            self.assertEqual(expand('tool.exe'), 'tool.exe')
            absolute = str(Path(self.root) / 'tool.exe')
            self.assertEqual(expand(absolute), absolute)

    def test_unknown_and_relative_roots_refused(self):
        with self.patch.dict(self.os.environ, {'LOCALAPPDATA': 'relative', 'UNAPPROVED': self.root}):
            expand = self.env['materialize_profile_path']
            for value in ('%UNAPPROVED%/tool', '%LOCALAPPDATA%/tool', '%UNKNOWN_PROFILE_PATH%/tool'):
                with self.subTest(value=value), self.assertRaises(self.env['FleetError']):
                    expand(value)

    def test_only_declared_fields_materialized(self):
        profile = {'candidates': ['%USERPROFILE%/tool'],
                   'local_runtime': {'ollama_executable_windows': '%LOCALAPPDATA%/tool',
                                     'base_url': 'https://example.invalid/%UNAPPROVED%'},
                   'notes': ['%UNAPPROVED%']}
        with self.patch.dict(self.os.environ, {'USERPROFILE': self.root, 'LOCALAPPDATA': self.root}):
            self.env['bind_native_profile_paths']('qwen-local', profile)
        self.assertEqual(profile['candidates'], [self.root + '/tool'])
        self.assertEqual(profile['local_runtime']['ollama_executable_windows'], self.root + '/tool')
        self.assertEqual(profile['local_runtime']['base_url'], 'https://example.invalid/%UNAPPROVED%')
        self.assertEqual(profile['notes'], ['%UNAPPROVED%'])

    def test_actual_loader_rejects_unknown_before_validation(self):
        raw = {'schema_version': 5, 'profiles': {'example': {'candidates': ['%UNAPPROVED%/tool']}}}
        self.env['load_json'] = lambda path, label: raw
        with self.patch.dict(self.os.environ, {'UNAPPROVED': self.root}):
            with self.assertRaises(self.env['FleetError']):
                self.env['load_profiles'](Path('synthetic.json'))


class DisabledOllamaProfile(unittest.TestCase):
    def setUp(self):
        import importlib.util
        import os
        from unittest.mock import patch
        self.patch = patch
        self.os = os
        path = SOURCE.with_name('ollama_local_runtime.py')
        spec = importlib.util.spec_from_file_location('guard_test_ollama', path)
        self.runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runtime)
        r = self.runtime
        self.root = str(Path.cwd().resolve() / 'synthetic-missing-localappdata')
        self.config = {
            'kind': 'ollama-windows-external', 'state_id': 'qwen-test',
            'base_url': r.PINNED_OLLAMA_BASE_URL, 'native_tags_url': r.PINNED_OLLAMA_TAGS_URL,
            'served_model_name': r.PINNED_QWEN_MODEL,
            'model_manifest_digest': r.PINNED_QWEN_MANIFEST_DIGEST,
            'ollama_executable_windows': r.PINNED_OLLAMA_EXECUTABLE,
            'ollama_executable_sha256': r.PINNED_OLLAMA_EXECUTABLE_SHA256,
            'ollama_version': r.PINNED_OLLAMA_VERSION,
            'context_window_tokens': 16384, 'max_num_seqs': 1,
            'max_wall_time_seconds': 600, 'externally_managed': True,
            'provider_cost_pln': 0, 'provider_cost_display': '0 zł',
        }

    def test_disabled_can_validate_without_binary_but_default_cannot(self):
        with self.patch.dict(self.os.environ, {'LOCALAPPDATA': self.root}):
            self.runtime.validate_config(self.config, require_executable=False)
            self.runtime.validate_pinned_qwen_ollama_contract(self.config, require_executable=False)
            with self.assertRaises(self.runtime.LocalRuntimeError):
                self.runtime.validate_config(self.config)
            with self.assertRaises(self.runtime.LocalRuntimeError):
                self.runtime.validate_pinned_qwen_ollama_contract(self.config)
            with self.assertRaises(self.runtime.LocalRuntimeError):
                with self.runtime.execution_lock(self.config, timeout=1):
                    self.fail('disabled runtime must not execute')

    def test_disabled_still_checks_pins_and_hash_shape(self):
        with self.patch.dict(self.os.environ, {'LOCALAPPDATA': self.root}):
            for key, bad in [('ollama_executable_sha256', 'bad'),
                             ('model_manifest_digest', '0' * 64),
                             ('ollama_version', '9.9.9'),
                             ('base_url', 'http://127.0.0.1:11440/v1')]:
                config = dict(self.config, **{key: bad})
                with self.subTest(key=key), self.assertRaises(self.runtime.LocalRuntimeError):
                    self.runtime.validate_pinned_qwen_ollama_contract(config, require_executable=False)

    def test_execute_single_disabled_guard(self):
        source = SOURCE.read_text(encoding='utf-8')
        start = source.index('    if profile["routing_enabled"] is False and not (', source.index('def execute_single('))
        end = source.index('    if mode not in profile["supported_modes"]:', start)
        guard = textwrap.dedent(source[start:end])
        class Refusal(Exception):
            pass
        env = {'profile': {'routing_enabled': False, 'display_name': 'disabled',
                           'routing_blocker': 'legacy disabled'},
               'mistral_candidate_uat': False, 'zcode_candidate_uat': False,
               'is_beam_profile': lambda profile: False,
               'is_mercury_profile': lambda profile: False, 'FleetError': Refusal}
        with self.assertRaises(Refusal):
            exec(compile(guard, str(SOURCE), 'exec'), env)

    def test_disabled_profile_is_not_route_eligible(self):
        source = SOURCE.read_text(encoding='utf-8')
        anchor = source.index('        legacy_gates_passed = all(')
        start = source.index('        eligible = (', anchor)
        end = source.index('        baseline_score =', start)
        expression = textwrap.dedent(source[start:end])
        env = {'availability': {'qwen-local': True}, 'agent_id': 'qwen-local',
               'routing_enabled': False, 'fit': 100, 'floor': 0,
               'legacy_gates_passed': True}
        exec(compile(expression, str(SOURCE), 'exec'), env)
        self.assertFalse(env['eligible'])


if __name__ == '__main__':
    unittest.main()
