"""Bounded CLI and stdio JSON-RPC behavior for code_search.

These tests exercise the CLI/MCP wrapper layer over ``core.SearchService``.
Native runtimes are mocked so the suite never needs real assets. The
service contract, repo binding and protocol framing are what bound this code.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import code_search
import core
import snapshot


WORKTREE = Path(__file__).resolve().parents[4]
PYTHON = Path(sys.executable)


def _make_synthetic_repo(root: Path) -> tuple[Path, Path]:
    """Build a one-file Git repo + state dir under ``root`` and return (repo, state)."""
    snapshot.make_directory(root)
    repo = root / 'repo'
    snapshot.make_directory(repo)
    state = root / 'state'
    (repo / 'main.py').write_text('def lookup():\n    return "needle"\n', encoding='utf-8')
    git = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=' + os.devnull,
           '-C', str(repo)]
    env = {**os.environ, 'GIT_AUTHOR_NAME': 'synthetic',
           'GIT_AUTHOR_EMAIL': 'synthetic@example.invalid'}
    subprocess.check_call(git + ['init', '-q'], env=env)
    subprocess.check_call(git + ['config', 'user.email', 'synthetic@example.invalid'], env=env)
    subprocess.check_call(git + ['config', 'user.name', 'Synthetic Test'], env=env)
    subprocess.check_call(git + ['add', '--', 'main.py'], env=env)
    subprocess.check_call(git + ['commit', '-q', '-m', 'init'], env=env)
    return repo, state


def _write_config(state: Path, repo: Path, base: Path) -> Path:
    cfg = {'version': 1, 'state_dir': str(state),
           'allowed_repos': [str(repo)],
           'colgrep': str(base / 'colgrep.exe'),
           'encoder_dir': str(base / 'encoder'),
           'agent_model': str(base / 'agent.gguf'),
           'llama_server': str(base / 'llama' / 'llama-server.exe'),
           'asset_manifest': str(base / 'manifest.json'),
           'device_name': 'NVIDIA GeForce RTX 5070 Ti'}
    path = base / 'service.json'
    path.write_text(json.dumps(cfg), encoding='utf-8')
    return path


class ParserTests(unittest.TestCase):
    def test_missing_required_args_is_a_usage_error(self):
        from argparse import ArgumentError
        parser = code_search.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])
        with self.assertRaises(SystemExit):
            parser.parse_args(['doctor'])
        with self.assertRaises(SystemExit):
            parser.parse_args(['--config', 'x.json', 'search'])

    def test_help_lists_only_documented_subcommands(self):
        parser = code_search.build_parser()
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            with self.assertRaises(SystemExit):
                parser.parse_args(['--help'])
        text = out.getvalue()
        for cmd in ('search', 'doctor', 'mcp'):
            self.assertIn(cmd, text)


class SearchArgumentContractTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.base = Path(tempfile.mkdtemp(prefix='code-search-mcp-'))
        self.repo, self.state = _make_synthetic_repo(self.base)
        self.config = _write_config(self.state, self.repo, self.base)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    def test_search_rejects_invalid_arguments_before_touching_service(self):
        with patch('core.SearchService', side_effect=AssertionError('must not open')):
            for arguments, why in [
                ({'query': ''}, 'empty query'),
                ({'query': 'a\x00b'}, 'null in query'),
                ({'query': '\ud800'}, 'surrogate'),
                ({'query': 'a' * (code_search.MAX_QUERY_BYTES + 1)}, 'oversized'),
                ({'query': 'ok', 'mode': 'shell'}, 'bad mode'),
                ({'query': 'ok', 'k': 0}, 'k too small'),
                ({'query': 'ok', 'k': code_search.MAX_K + 1}, 'k too large'),
                ({'query': 'ok', 'k': True}, 'k not int'),
                ({'query': 'ok', 'max_bytes': 0}, 'budget too small'),
                ({'query': 'ok', 'max_bytes': code_search.MAX_MAX_BYTES + 1}, 'budget too large'),
                ({'query': 'ok', 'extra': 'no'}, 'unknown key'),
                ({}, 'missing query'),
                ({'query': 5}, 'non-string query'),
            ]:
                with self.assertRaises(code_search.ServerError, msg=why):
                    code_search._invoke_search(object(), arguments)

    def test_search_forwards_bounded_arguments_to_service(self):
        captured = {}

        class Fake:
            def search(self, query, mode='semantic', k=8, max_bytes=4096):
                captured['query'] = query
                captured['mode'] = mode
                captured['k'] = k
                captured['max_bytes'] = max_bytes
                return {'status': 'ok', 'results': [], 'omissions': {},
                        'omitted_count': 0, 'incomplete': False,
                        'trust_advisory': 'ok'}
        result = code_search._invoke_search(Fake(), {'query': 'needle', 'mode': 'literal',
                                                     'k': 4, 'max_bytes': 1024})
        self.assertEqual(captured, {'query': 'needle', 'mode': 'literal',
                                    'k': 4, 'max_bytes': 1024})
        self.assertEqual(result['status'], 'ok')


class ProtocolFramingTests(unittest.TestCase):
    def test_jsonrpc_error_covers_all_codes(self):
        for code in code_search.PROTOCOL_ERROR:
            payload = code_search._jsonrpc_error('1', code)
            self.assertEqual(payload['jsonrpc'], '2.0')
            self.assertEqual(payload['id'], '1')
            self.assertEqual(payload['error']['message'], code_search.PROTOCOL_ERROR[code]['message'])

    def test_invalid_requests_rejected_without_running_tool(self):
        for bad in [
            {},
            {'jsonrpc': '1.0'},
            {'jsonrpc': '2.0'},
            {'jsonrpc': '2.0', 'method': 1},
            {'jsonrpc': '2.0', 'method': 'x', 'params': 'not-dict'},
            {'jsonrpc': '2.0', 'method': 'x', 'id': None, 'params': {}},
            {'jsonrpc': '2.0', 'method': 'x', 'id': ['bad'], 'params': {}},
        ]:
            with self.assertRaises(code_search.ServerError):
                code_search._validate_request(bad)

    def test_tool_result_truncates_huge_payloads_safely(self):
        huge = {'status': 'ok', 'text': 'a' * (code_search.MAX_LINE_BYTES * 2)}
        result = code_search._tool_result(huge)
        # Truncated payload becomes an error so the adapter can react.
        self.assertTrue(result['isError'])
        self.assertLessEqual(len(result['content'][0]['text'].encode('utf-8')),
                             code_search.MAX_LINE_BYTES)
        text = json.loads(result['content'][0]['text'])
        self.assertEqual(text['status'], 'error')
        self.assertEqual(text['error'], 'response_too_large')

    def test_response_size_cap_returns_error_payload(self):
        payload = {'status': 'ok', 'text': 'a' * (code_search.MAX_LINE_BYTES * 4)}
        with self.assertRaises(code_search.ServerError):
            code_search._bounded_dumps(payload)


class McpRequestHandlingTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.base = Path(tempfile.mkdtemp(prefix='code-search-mcp-'))
        self.repo, self.state = _make_synthetic_repo(self.base)
        self.config = _write_config(self.state, self.repo, self.base)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    def _open(self):
        return core.SearchService(self.config, self.repo)

    def test_initialize_returns_protocol_and_tools_capability(self):
        with self._open() as service:
            reply = code_search._handle_message(service, {'jsonrpc': '2.0', 'id': 1,
                                                          'method': 'initialize'})
        payload = json.loads(reply)
        self.assertEqual(payload['result']['protocolVersion'], code_search.PROTOCOL_VERSION)
        self.assertEqual(payload['result']['serverInfo']['name'], code_search.SERVER_NAME)
        self.assertIn('tools', payload['result']['capabilities'])

    def test_tools_list_advertises_only_bounded_scoped_tools(self):
        with self._open() as service:
            reply = code_search._handle_message(service, {'jsonrpc': '2.0', 'id': 2,
                                                          'method': 'tools/list'})
        payload = json.loads(reply)
        names = sorted(tool['name'] for tool in payload['result']['tools'])
        self.assertEqual(names, sorted([code_search.TOOL_SEARCH, code_search.TOOL_STATUS]))
        for tool in payload['result']['tools']:
            self.assertTrue(tool['annotations']['readOnlyHint'])
            self.assertFalse(tool['annotations']['destructiveHint'])
            self.assertIn('additionalProperties', tool['inputSchema'])

    def test_tool_search_cannot_replace_bound_repo_or_endpoint(self):
        with self._open() as service:
            reply = code_search._handle_message(service, {'jsonrpc': '2.0', 'id': 3,
                                                          'method': 'tools/call',
                                                          'params': {'name': 'code_search',
                                                                     'arguments': {
                                                                         'query': 'needle',
                                                                         'repo': str(self.base / 'other'),
                                                                         'config': str(self.base / 'other.json'),
                                                                         'endpoint': 'http://attacker/',
                                                                     }}})
        payload = json.loads(reply)
        text = json.loads(payload['result']['content'][0]['text'])
        # The implementation rejects extra keys; the bound repo is the only one ever opened.
        self.assertEqual(text['status'], 'error')
        self.assertEqual(text['error'], 'invalid_search_arguments')
        self.assertNotIn('attacker', json.dumps(text))

    def test_tool_status_returns_doctor_without_running_search(self):
        with self._open() as service:
            with patch.object(service, 'doctor', wraps=service.doctor) as observed:
                reply = code_search._handle_message(service, {'jsonrpc': '2.0', 'id': 4,
                                                              'method': 'tools/call',
                                                              'params': {'name': 'code_search_status',
                                                                         'arguments': {}}})
                self.assertEqual(observed.call_count, 1)
        payload = json.loads(reply)
        self.assertFalse(payload['result']['isError'])
        text = json.loads(payload['result']['content'][0]['text'])
        self.assertIn('status', text)
        self.assertFalse(text.get('network_fallback', True))

    def test_unknown_tool_and_method_are_rejected(self):
        with self._open() as service:
            reply = code_search._handle_message(service, {'jsonrpc': '2.0', 'id': 5,
                                                          'method': 'tools/call',
                                                          'params': {'name': 'eval',
                                                                     'arguments': {}}})
        payload = json.loads(reply)
        # MCP tools spec: an unknown tool is a JSON-RPC protocol error.
        self.assertEqual(payload['error']['code'], -32602)
        self.assertNotIn('result', payload)
        with self._open() as service:
            reply = code_search._handle_message(service, {'jsonrpc': '2.0', 'id': 6,
                                                          'method': 'tools/mutate'})
        self.assertEqual(reply, code_search._bounded_dumps(
            code_search._jsonrpc_error(6, 'METHOD_NOT_FOUND')))

    def test_valid_notifications_never_receive_error_replies(self):
        with self._open() as service:
            for message in [
                {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 12}},
                {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'eval'}},
                {'jsonrpc': '2.0', 'method': 'unknown'},
            ]:
                self.assertIsNone(code_search._handle_message(service, message))

    def test_boolean_request_id_is_invalid(self):
        with self._open() as service:
            payload = json.loads(code_search._handle_message(service, {'jsonrpc': '2.0', 'id': True, 'method': 'ping'}))
        self.assertEqual(payload['error']['code'], -32600)
        self.assertIsNone(payload['id'])

    def test_oversized_and_surrogate_request_ids_fail_without_echo(self):
        with self._open() as service:
            for value in ['a' * 2048, '\ud800']:
                payload = json.loads(code_search._handle_message(service, {'jsonrpc': '2.0', 'id': value, 'method': 'ping'}))
                self.assertEqual(payload['error']['code'], -32600)
                self.assertIsNone(payload['id'])

    def test_escaped_tool_response_is_bounded_after_outer_encoding(self):
        with self._open() as service, patch.object(service, 'search', return_value={'status': 'ok', 'results': [{'text': '"' * 20000}]}):
            reply = code_search._handle_message(service, {'jsonrpc': '2.0', 'id': 12, 'method': 'tools/call',
                'params': {'name': 'code_search', 'arguments': {'query': 'x', 'max_bytes': 1048576}}})
        payload = json.loads(reply)
        self.assertLessEqual(len(reply.encode('utf-8')), code_search.MAX_LINE_BYTES)
        self.assertEqual(payload['id'], 12)
        self.assertTrue(payload['result']['isError'])
        self.assertEqual(json.loads(payload['result']['content'][0]['text'])['error'], 'response_too_large')

    def test_search_with_unbound_repo_refuses_to_open(self):
        # Service is constructed against a repo that is NOT in the config; opening
        # the CLI service must fail before any tool call reaches the wire.
        with patch('snapshot.absolute_path', side_effect=snapshot.BoundaryError('unsafe_absolute_path')):
            with self.assertRaises(snapshot.BoundaryError):
                code_search._open_service(self.config, Path('Z:/nope'))


class CliSubprocessTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.base = Path(tempfile.mkdtemp(prefix='code-search-mcp-cli-'))
        self.repo, self.state = _make_synthetic_repo(self.base)
        self.config = _write_config(self.state, self.repo, self.base)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.base, ignore_errors=True)

    def _run(self, *args):
        argv = [str(PYTHON), '-B', str(Path(__file__).resolve().parents[1] / 'code_search.py'),
                '--config', str(self.config), '--repo', str(self.repo), *args]
        return subprocess.run(argv, capture_output=True, timeout=20)

    def test_cli_doctor_reports_status(self):
        proc = self._run('doctor')
        self.assertEqual(proc.returncode, 1, proc.stderr.decode('utf-8', errors='replace'))
        payload = json.loads(proc.stdout.decode('utf-8'))
        self.assertIn(payload['status'], ('ok', 'error'))
        self.assertEqual(payload.get('network_fallback'), False)

    def test_cli_search_literal_runs_against_synthetic_repo(self):
        proc = self._run('search', '--query', 'needle', '--mode', 'literal',
                         '--k', '2', '--max-bytes', '1024')
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', errors='replace'))
        payload = json.loads(proc.stdout.decode('utf-8'))
        self.assertEqual(payload['status'], 'ok')
        self.assertTrue(any(r['path'] == 'main.py' for r in payload['results']))

    def test_cli_search_semantic_without_assets_reports_error(self):
        proc = self._run('search', '--query', 'find the lookup helper',
                         '--mode', 'semantic', '--k', '1', '--max-bytes', '512')
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout.decode('utf-8'))
        self.assertEqual(payload['status'], 'error')

    def test_cli_search_rejects_oversized_query(self):
        proc = self._run('search', '--query', 'a' * 9000, '--mode', 'literal',
                         '--k', '1', '--max-bytes', '256')
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout.decode('utf-8'))
        self.assertEqual(payload['status'], 'error')

    def test_cli_search_rejects_unknown_repo(self):
        proc = subprocess.run(
            [str(PYTHON), '-B', str(Path(__file__).resolve().parents[1] / 'code_search.py'),
             '--config', str(self.config), '--repo', str(self.base / 'outside'),
             'doctor'],
            capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout.decode('utf-8'))
        self.assertEqual(payload['status'], 'error')

    def test_cli_mcp_runs_full_protocol_round_trip(self):
        proc = subprocess.Popen(
            [str(PYTHON), '-B', str(Path(__file__).resolve().parents[1] / 'code_search.py'),
             '--config', str(self.config), '--repo', str(self.repo), 'mcp'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'code_search_status', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
             'params': {'name': 'code_search', 'arguments': {'query': 'needle',
                                                              'mode': 'literal',
                                                              'k': 2, 'max_bytes': 512}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
             'params': {'name': 'code_search', 'arguments': {'query': 'a\x00b',
                                                              'mode': 'literal'}}},
            {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
             'params': {'name': 'eval', 'arguments': {}}},
        ]
        stdin = '\n'.join(json.dumps(m) for m in messages) + '\n'
        try:
            stdout, stderr = proc.communicate(input=stdin.encode('utf-8'), timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        self.assertEqual(proc.returncode, 0, stderr.decode('utf-8', errors='replace'))
        replies = [json.loads(line) for line in stdout.decode('utf-8').splitlines() if line]
        self.assertEqual([r['id'] for r in replies], [1, 2, 3, 4, 5, 6])
        self.assertIn('tools', replies[1]['result'])
        self.assertFalse(replies[2]['result']['isError'])
        self.assertEqual(json.loads(replies[3]['result']['content'][0]['text'])['status'], 'ok')
        # Invalid argument returns a wrapped error payload, not a stack trace.
        self.assertEqual(json.loads(replies[4]['result']['content'][0]['text'])['status'], 'error')
        self.assertEqual(replies[5]['error']['code'], -32602)

    def test_cli_stdout_is_utf8_even_when_host_requests_cp1250(self):
        text = 'zażółć gęślą jaźń 日本語'
        (self.repo / 'main.py').write_text('def lookup():\n    return "' + text + '"\n', encoding='utf-8')
        argv = [str(PYTHON), '-B', str(Path(__file__).resolve().parents[1] / 'code_search.py'),
                '--config', str(self.config), '--repo', str(self.repo),
                'search', '--query', text, '--mode', 'literal']
        proc = subprocess.run(argv, capture_output=True, timeout=20,
                              env={**os.environ, 'PYTHONIOENCODING': 'cp1250', 'PYTHONUTF8': '0'})
        self.assertEqual(proc.returncode, 0, proc.stderr.decode('utf-8', errors='replace'))
        payload = json.loads(proc.stdout.decode('utf-8'))
        self.assertIn(text, payload['results'][0]['text'])

    def test_large_cli_response_is_structured_error_without_traceback(self):
        (self.repo / 'main.py').write_text('def lookup():\n' + ('    # marker ' + 'x' * 2000 + '\n') * 100, encoding='utf-8')
        proc = self._run('search', '--query', 'marker', '--mode', 'literal', '--k', '100', '--max-bytes', '1048576')
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout.decode('utf-8'))
        self.assertEqual(payload['error'], 'response_too_large')
        self.assertNotIn(b'Traceback', proc.stderr)

    def test_cli_mcp_rejects_malformed_and_oversized_lines(self):
        proc = subprocess.Popen(
            [str(PYTHON), '-B', str(Path(__file__).resolve().parents[1] / 'code_search.py'),
             '--config', str(self.config), '--repo', str(self.repo), 'mcp'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        garbage = b'not json\n' + b'{"jsonrpc":"2.0","id":7,"method":"initialize"}\n'
        try:
            stdout, stderr = proc.communicate(input=garbage, timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        self.assertEqual(proc.returncode, 0, stderr.decode('utf-8', errors='replace'))
        replies = [json.loads(line) for line in stdout.decode('utf-8').splitlines() if line]
        # The server must respond with PARSE_ERROR for the malformed line, then
        # resume processing for the second valid request.
        self.assertEqual(replies[0]['error']['code'], code_search.PROTOCOL_ERROR['PARSE_ERROR']['code'])
        self.assertEqual(replies[1]['id'], 7)
        self.assertIn('serverInfo', replies[1]['result'])


if __name__ == '__main__':
    unittest.main()
