"""Bounded CLI and stdio JSON-RPC for the local code-search service.

The CLI and MCP servers sit over the existing ``core.SearchService``. The
authorized repository and configuration are bound at process startup; tool
arguments cannot replace them. The CLI exposes ``search`` and ``doctor`` and
the MCP server exposes ``code_search`` and ``code_search_status``. Tool
inputs are bounded, malformed lines and unknown methods are rejected, and
no native process is started until assets are present and verified.

The JSON-RPC server speaks MCP framing (one JSON object per line over stdio).
Only the methods required for read-only search/status are advertised; generic
shell, eval, endpoint or config mutations are intentionally absent.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow direct invocation as ``python global/tools/code-search/code_search.py``.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import core
import snapshot


# Wire-protocol constants; never source from a config or repo file.
PROTOCOL_VERSION = '2024-11-05'
SERVER_NAME = 'agent-stack.code-search'
SERVER_VERSION = '1.0.0'

MAX_LINE_BYTES = 65536           # bound a single stdin/stdout JSON line
MAX_ID_BYTES = 1024             # reserve space for a bounded error reply
MAX_QUERY_BYTES = 8192           # bound the search query itself
MAX_K = 100
MAX_MAX_BYTES = 1048576

EXIT_OK = 0
EXIT_INPUT_ERROR = 2   # argparse usage errors
EXIT_RUNTIME_ERROR = 1  # service errors

# Tools advertised by the MCP server. Names are part of the contract;
# changing them is a breaking change for every adapter.
TOOL_SEARCH = 'code_search'
TOOL_STATUS = 'code_search_status'

PROTOCOL_ERROR = {
    'PARSE_ERROR': {'code': -32700, 'message': 'parse error'},
    'INVALID_REQUEST': {'code': -32600, 'message': 'invalid request'},
    'METHOD_NOT_FOUND': {'code': -32601, 'message': 'method not found'},
    'INVALID_PARAMS': {'code': -32602, 'message': 'invalid params'},
    'INTERNAL_ERROR': {'code': -32603, 'message': 'internal error'},
}

TOOL_SCHEMAS = {
    TOOL_SEARCH: {
        'name': TOOL_SEARCH,
        'description': 'Search an authorized local Git repository for code snippets.',
        'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                        'idempotentHint': True, 'openWorldHint': False},
        'inputSchema': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'query': {'type': 'string', 'minLength': 1, 'maxLength': MAX_QUERY_BYTES},
                'mode': {'type': 'string', 'enum': ['literal', 'semantic', 'agent']},
                'k': {'type': 'integer', 'minimum': 1, 'maximum': MAX_K},
                'max_bytes': {'type': 'integer', 'minimum': 128, 'maximum': MAX_MAX_BYTES},
            },
            'required': ['query'],
        },
    },
    TOOL_STATUS: {
        'name': TOOL_STATUS,
        'description': 'Report the bound repository, asset identity and configuration '
                       'without executing any model.',
        'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                        'idempotentHint': True, 'openWorldHint': False},
        'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': {}},
    },
}


class ServerError(Exception):
    """A closed MCP/CLI failure that is safe to surface to the adapter."""

    def __init__(self, code: str, http_status: int = 1):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _bounded_dumps(payload: dict) -> str:
    """Serialize ``payload`` as one bounded JSON line terminated by '\\n'."""
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    encoded = text.encode('utf-8')
    if len(encoded) > MAX_LINE_BYTES:
        raise ServerError('response_too_large')
    return text + '\n'


def _emit(payload: dict) -> bool:
    """CLI and MCP startup errors use UTF-8 independently of console locale."""
    emitted = True
    try:
        line = _bounded_dumps(payload)
    except ServerError:
        line = _bounded_dumps({'status': 'error', 'error': 'response_too_large'})
        emitted = False
    sys.stdout.buffer.write(line.encode('utf-8'))
    sys.stdout.buffer.flush()
    return emitted


def _open_service(config_path: Path, repo: Path) -> core.SearchService:
    """Bind repo+config once. Tool calls reuse this service."""
    return core.SearchService(config_path, repo)


def _normalize_mode(value):
    if isinstance(value, str) and value in ('literal', 'semantic', 'agent'):
        return value
    return None


def _normalize_optional_int(value, low: int, high: int):
    if value is None:
        return None
    if type(value) is not int or isinstance(value, bool) or not low <= value <= high:
        return None
    return value


def _normalize_query(value):
    if not isinstance(value, str):
        return None
    if not value or '\x00' in value:
        return None
    if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        return None
    if len(value.encode('utf-8')) > MAX_QUERY_BYTES:
        return None
    return value


def _invoke_search(service: core.SearchService, arguments: dict) -> dict:
    """Apply the closed search argument contract before touching the service."""
    if not isinstance(arguments, dict):
        raise ServerError('invalid_search_arguments')
    if any(key not in ('query', 'mode', 'k', 'max_bytes') for key in arguments):
        raise ServerError('invalid_search_arguments')
    query = _normalize_query(arguments.get('query'))
    if query is None:
        raise ServerError('invalid_search_arguments')
    if 'mode' in arguments:
        mode = _normalize_mode(arguments.get('mode'))
        if mode is None:
            raise ServerError('invalid_search_arguments')
    else:
        mode = 'semantic'
    if 'k' in arguments:
        k = _normalize_optional_int(arguments.get('k'), 1, MAX_K)
        if k is None:
            raise ServerError('invalid_search_arguments')
    else:
        k = 8
    if 'max_bytes' in arguments:
        max_bytes = _normalize_optional_int(arguments.get('max_bytes'), 128, MAX_MAX_BYTES)
        if max_bytes is None:
            raise ServerError('invalid_search_arguments')
    else:
        max_bytes = 4096
    result = service.search(query, mode=mode, k=k, max_bytes=max_bytes)
    if not isinstance(result, dict):
        raise ServerError('service_protocol_error')
    return result


def _invoke_status(service: core.SearchService, arguments: dict) -> dict:
    if not isinstance(arguments, dict) or arguments:
        raise ServerError('invalid_status_arguments')
    result = service.doctor()
    if not isinstance(result, dict):
        raise ServerError('service_protocol_error')
    return result


def _call_tool(service: core.SearchService, name: str, arguments: dict) -> tuple[dict, bool]:
    """Return ``(payload, tool_error)``. The doctor always returns ``tool_error=False``."""
    if name == TOOL_SEARCH:
        payload = _invoke_search(service, arguments)
        return payload, payload.get('status') == 'error'
    if name == TOOL_STATUS:
        payload = _invoke_status(service, arguments)
        return payload, False
    raise ServerError('unknown_tool')


def _tool_result(payload: dict, tool_error: bool | None = None) -> dict:
    """Wrap a service response as MCP tool content; ``isError`` is the call outcome.

    The status tool reports readiness in its payload but never fails the tool
    call itself, so its ``isError`` is always false. Search execution failures
    surface as ``isError=true``; unknown tools are protocol errors. When ``tool_error`` is omitted, the
    helper falls back to the payload's own ``status`` field for backward
    compatibility with direct callers.
    """
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    truncated = False
    if len(text.encode('utf-8')) > MAX_LINE_BYTES:
        text = json.dumps({'status': 'error', 'error': 'response_too_large'},
                          ensure_ascii=False, allow_nan=False)
        truncated = True
    if tool_error is None:
        tool_error = payload.get('status') == 'error'
    return {'content': [{'type': 'text', 'text': text}],
            'isError': truncated or tool_error}


def _jsonrpc_error(request_id, code: str) -> dict:
    info = PROTOCOL_ERROR[code]
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': info['code'],
                                                           'message': info['message']}}


def _jsonrpc_result(request_id, result) -> dict:
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def _validate_request(message) -> tuple:
    if not isinstance(message, dict):
        raise ServerError('INVALID_REQUEST')
    jsonrpc = message.get('jsonrpc')
    if jsonrpc != '2.0':
        raise ServerError('INVALID_REQUEST')
    method = message.get('method')
    if not isinstance(method, str) or not method:
        raise ServerError('INVALID_REQUEST')
    if 'id' in message:
        request_id = message['id']
        if type(request_id) not in (str, int):
            raise ServerError('INVALID_REQUEST')
        try:
            if len(json.dumps(request_id, ensure_ascii=False).encode('utf-8')) > MAX_ID_BYTES:
                raise ServerError('INVALID_REQUEST')
        except UnicodeError:
            raise ServerError('INVALID_REQUEST') from None
    else:
        request_id = None
    params = message.get('params', {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ServerError('INVALID_REQUEST')
    return method, params, request_id


def _handle_message(service: core.SearchService, message) -> str | None:
    try:
        method, params, request_id = _validate_request(message)
    except ServerError as exc:
        return _bounded_dumps(_jsonrpc_error(None, exc.code))
    # Notifications never receive replies, including protocol errors.
    if 'id' not in message:
        return None
    try:
        if method == 'initialize':
            result = {'protocolVersion': PROTOCOL_VERSION,
                      'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION},
                      'capabilities': {'tools': {'listChanged': False}}}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': list(TOOL_SCHEMAS.values())}
        elif method == 'tools/call':
            name = params.get('name')
            arguments = params.get('arguments', {})
            if not isinstance(name, str):
                raise ServerError('INVALID_PARAMS')
            if name not in TOOL_SCHEMAS:
                raise ServerError('INVALID_PARAMS')
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise ServerError('INVALID_PARAMS')
            try:
                payload, tool_error = _call_tool(service, name, arguments)
            except ServerError as exc:
                payload = {'status': 'error', 'error': exc.code}
                tool_error = True
            result = _tool_result(payload, tool_error)
        else:
            # JSON-RPC 2.0: notifications (no id) MUST NOT get a reply.
            if 'id' in message and message['id'] is not None:
                return _bounded_dumps(_jsonrpc_error(request_id, 'METHOD_NOT_FOUND'))
            return None
    except ServerError as exc:
        if exc.code in PROTOCOL_ERROR:
            return _bounded_dumps(_jsonrpc_error(request_id, exc.code))
        return _bounded_dumps(_jsonrpc_error(request_id, 'INTERNAL_ERROR'))
    except (OSError, UnicodeError, snapshot.BoundaryError):
        return _bounded_dumps(_jsonrpc_error(request_id, 'INTERNAL_ERROR'))
    if 'id' in message and message['id'] is not None:
        try:
            return _bounded_dumps(_jsonrpc_result(request_id, result))
        except ServerError:
            # Bound the final wire envelope, including escaping of tool JSON.
            if method == 'tools/call':
                small = _tool_result({'status': 'error', 'error': 'response_too_large'}, True)
                return _bounded_dumps(_jsonrpc_result(request_id, small))
            return _bounded_dumps(_jsonrpc_error(request_id, 'INTERNAL_ERROR'))
    return None


def serve_stdio(service: core.SearchService) -> int:
    """Read one bounded JSON object per line from stdin, write replies."""
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        try:
            line = stdin.readline(MAX_LINE_BYTES + 1)
        except (OSError, ValueError):
            return EXIT_RUNTIME_ERROR
        if not line:
            return EXIT_OK
        if len(line) > MAX_LINE_BYTES:
            reply = _bounded_dumps(_jsonrpc_error(None, 'PARSE_ERROR'))
            stdout.write(reply.encode('utf-8'))
            stdout.flush()
            return EXIT_OK
        try:
            text = line.decode('utf-8').strip()
        except UnicodeError:
            reply = _bounded_dumps(_jsonrpc_error(None, 'PARSE_ERROR'))
            stdout.write(reply.encode('utf-8'))
            stdout.flush()
            return EXIT_OK
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            stdout.write(_bounded_dumps(_jsonrpc_error(None, 'PARSE_ERROR')).encode('utf-8'))
            stdout.flush()
            continue
        try:
            reply = _handle_message(service, message)
        except (OSError, ValueError):
            return EXIT_RUNTIME_ERROR
        if reply is None:
            continue
        stdout.write(reply.encode('utf-8'))
        stdout.flush()


def _open_cli_service(args) -> core.SearchService:
    config_path = snapshot.absolute_path(args.config)
    repo = snapshot.absolute_path(args.repo)
    return _open_service(config_path, repo)


def _doctor(args) -> int:
    try:
        service = _open_cli_service(args)
    except snapshot.BoundaryError as exc:
        payload = {'status': 'error', 'error': str(exc)}
        _emit(payload)
        return EXIT_RUNTIME_ERROR
    try:
        payload = service.doctor()
    finally:
        service.close()
    emitted = _emit(payload)
    return EXIT_OK if emitted and payload.get('status') == 'ok' else EXIT_RUNTIME_ERROR


def _search(args) -> int:
    arguments = {'query': args.query, 'mode': args.mode,
                 'k': args.k, 'max_bytes': args.max_bytes}
    try:
        service = _open_cli_service(args)
    except snapshot.BoundaryError as exc:
        payload = {'status': 'error', 'error': str(exc)}
        _emit(payload)
        return EXIT_RUNTIME_ERROR
    try:
        try:
            payload = _invoke_search(service, arguments)
        except ServerError as exc:
            # Argument contract violation: bound and serialize as structured JSON,
            # never as a Python traceback. Exit code stays nonzero.
            payload = {'status': 'error', 'error': exc.code}
    finally:
        service.close()
    emitted = _emit(payload)
    return EXIT_OK if emitted and payload.get('status') == 'ok' else EXIT_RUNTIME_ERROR


def _mcp(args) -> int:
    try:
        service = _open_cli_service(args)
    except snapshot.BoundaryError as exc:
        payload = {'jsonrpc': '2.0', 'id': None,
                   'error': {'code': -32603, 'message': str(exc)}}
        _emit(payload)
        return EXIT_RUNTIME_ERROR
    try:
        return serve_stdio(service)
    finally:
        service.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='code_search', add_help=True,
                                     description='Bounded local code search.')
    parser.add_argument('--config', required=True, help='Path to a v1 JSON config.')
    parser.add_argument('--repo', required=True, help='Path to an authorized Git repo root.')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor', help='Verify configuration and bound repo without executing any model.')
    search = sub.add_parser('search', help='Run a bounded search and print JSON to stdout.')
    search.add_argument('--query', required=True)
    search.add_argument('--mode', choices=('literal', 'semantic', 'agent'), default='semantic')
    search.add_argument('--k', type=int, default=8)
    search.add_argument('--max-bytes', type=int, default=4096)
    sub.add_parser('mcp', help='Serve JSON-RPC over stdio (one JSON object per line).')
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'doctor':
        return _doctor(args)
    if args.command == 'search':
        return _search(args)
    if args.command == 'mcp':
        return _mcp(args)
    parser.error('unknown_command')
    return EXIT_INPUT_ERROR


if __name__ == '__main__':
    raise SystemExit(main())
