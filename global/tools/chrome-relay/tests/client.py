"""Actual stdio MCP client for assigned synthetic actors; no model attestation."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
CANARIES = ('BROWSER_READ_CANARY_7a89c75c', 'INPUT_VALUE_CANARY_2962a2d1',
            'PASSWORD_CANARY_95012334', 'COOKIE_CANARY_9f12b34e')
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
MANAGED_HARNESSES = {'a':'codex', 'b':'codex', 'transport_a':'codex', 'transport_b':'codex'}
SECRET_SHAPE = re.compile(
    r'crm~[a-f0-9]{64}|Bearer\s+[A-Za-z0-9._~+/-]{24,}|'
    r'(?:set-cookie|cookie)["\']?\s*[:=]\s*["\']?[^,\r\n;"\']{1,512}|'
    r'relay_fixture\s*=\s*[^;\s"\\]+', re.I)

def load_stage(evidence: Path, *, controller: bool = False):
    evidence = evidence.resolve()
    stage = json.loads((evidence / 'staging.json').read_text(encoding='utf-8'))
    if stage.get('status') != 'READY' or time.time() >= stage['expires_at']:
        raise RuntimeError('staging_not_ready_or_expired')
    private = json.loads((evidence / 'private' / 'policy.json').read_text(encoding='utf-8')) if controller else None
    return evidence, stage, private

def unpack(response):
    if 'error' in response:
        return {'status': 'denied', 'data': {'reason': 'mcp_error'}}
    result = response.get('result', {})
    if 'structuredContent' in result:
        return result['structuredContent']
    for item in result.get('content', []):
        if item.get('type') == 'text':
            try:
                value = json.loads(item['text'])
                if isinstance(value, dict):
                    return value
            except (ValueError, TypeError):
                pass
    raise RuntimeError('mcp_result_not_structured')

class MCPClient:
    def __init__(self, evidence: Path, agent: str, transcript: bool = True):
        self.evidence, self.stage, _ = load_stage(evidence)
        self.agent, self.transcript = agent, transcript
        self.execution_id = uuid.uuid4().hex
        self.sid = None
        self.capability = None
        self.forbidden = CANARIES
        managed_config_path = self.stage.get('managed_config_path')
        if managed_config_path:
            if agent not in MANAGED_HARNESSES:
                raise RuntimeError('unknown_managed_fixture_harness')
            managed_path = Path(managed_config_path).resolve()
            if not managed_path.is_absolute() or managed_path.parent != Path(self.stage['runtime_root']).resolve():
                raise RuntimeError('managed_fixture_config_path_mismatch')
            with managed_path.open('rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536: raise RuntimeError('managed_config_bound')
            managed_config = json.loads(raw)
            if (not isinstance(managed_config, dict) or
                managed_config.get('schema') != 'chrome-relay-managed/1' or
                managed_config.get('grant', {}).get('persistent') is not True or
                managed_config.get('grant', {}).get('synthetic') is not True):
                raise RuntimeError('managed_persistent_fixture_required')
            self.forbidden += tuple(managed_config[key] for key in ('extension_token','controller_key')
                                    if isinstance(managed_config.get(key), str))
            env = {key:os.environ[key] for key in ('SYSTEMROOT','SystemRoot') if key in os.environ}
            env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
            command = [self.stage['python_exe'], '-B', str(ROOT / 'managed.py'), 'mcp',
                       '--config', str(managed_path), '--harness', MANAGED_HARNESSES[agent]]
        else:
            self.sid = self.stage['assigned_session_ids'][agent]
            capability_path = self.evidence / 'private' / ('client-' + agent + '.json')
            native_files = self.stage.get('native_actor_capability_files', {})
            if agent in native_files:
                if self.stage.get('synthetic_actor_kind') != 'fixture_capability_session':
                    raise RuntimeError('native_fixture_capability_requires_synthetic_stage')
                expected = self.evidence / 'native-actors' / agent / 'client.json'
                if Path(native_files[agent]).resolve() != expected.resolve():
                    raise RuntimeError('native_fixture_capability_path_mismatch')
                capability_path = expected
            with capability_path.open('rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536: raise RuntimeError('client_capability_bound')
            self.capability = json.loads(raw)
            if not isinstance(self.capability,dict) or set(self.capability) != {'broker_base','session_id','profile_id','token'}:
                raise RuntimeError('invalid_single_session_capability')
            if (self.capability['session_id'] != self.sid or self.capability['profile_id'] != self.stage['profile_id'] or
                self.capability['broker_base'] != self.stage['broker_base']):
                raise RuntimeError('explicit_client_assignment_mismatch')
            token = self.capability['token']
            if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9._~+/-]{16,256}={0,2}',token):
                raise RuntimeError('invalid_client_capability_token')
            self.forbidden += (token,)
            env = dict(os.environ, CHROME_RELAY_BROKER=self.stage['broker_base'],
                       CHROME_RELAY_SESSION_ID=self.sid, CHROME_RELAY_TOKEN=token,
                       PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
            env.pop('CHROME_RELAY_CLIENT_FILE', None)
            command = [self.stage['python_exe'], '-B', str(ROOT / 'mcp.py')]
        self.process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        creationflags=CREATE_FLAGS)
        self.messages = queue.Queue()
        self.stderr = []
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._errors, daemon=True).start()
        self.sequence = 0
        self.rpc('initialize', {'protocolVersion':'2024-11-05', 'capabilities':{},
                               'clientInfo':{'name':'chrome-relay-synthetic-client','version':'1.0.0'}})
        self.send({'jsonrpc':'2.0','method':'notifications/initialized'})
        self.tools = self.rpc('tools/list', {})['result']['tools']

    def _reader(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line.decode('utf-8')))
        except (ValueError, UnicodeError):
            self.messages.put({'transport_error':True})
        self.messages.put({'transport_closed':True})

    def _errors(self):
        for line in self.process.stderr:
            self.stderr.append(line.decode('utf-8', errors='replace'))

    def send(self, message):
        self.process.stdin.write((json.dumps(message, separators=(',', ':')) + '\n').encode('utf-8'))
        self.process.stdin.flush()

    def rpc(self, method, params):
        self.sequence += 1
        sequence = self.sequence
        self.send({'jsonrpc':'2.0','id':sequence,'method':method,'params':params})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            if result.get('transport_error') or result.get('transport_closed'):
                raise RuntimeError('mcp_transport_failed')
            if result.get('id') == sequence:
                return result
        raise RuntimeError('mcp_response_timeout')

    def call(self, name, arguments=None):
        arguments = dict(arguments or {})
        arguments.setdefault('request_id', uuid.uuid4().hex)
        started = time.time()
        wire = self.rpc('tools/call', {'name':name,'arguments':arguments})
        value = unpack(wire)
        serialized = json.dumps(value)
        if any(secret in serialized for secret in self.forbidden) or SECRET_SHAPE.search(serialized):
            raise RuntimeError('secret_canary_in_tool_result')
        actual_sid = value.get('session_id')
        if actual_sid is not None:
            if self.sid is None:
                if not re.fullmatch(r'c-[a-f0-9]{32}', actual_sid):
                    raise RuntimeError('invalid_managed_session_id')
                self.sid = actual_sid
            elif actual_sid != self.sid:
                raise RuntimeError('mcp_session_mismatch')
        record = {'instance_id':self.stage['instance_id'], 'execution_id':self.execution_id, 'assigned_session_id':self.sid,
                  'synthetic_actor_kind':self.stage.get('synthetic_actor_kind','fixture_capability_session'),
                  'native_identity':'parent_receipt_required', 'tool':name,
                  'argument_keys':sorted(arguments), 'request_id':arguments['request_id'],
                  'tab_id':arguments.get('tab_id'), 'ref':arguments.get('ref'),
                  'text_length':len(arguments.get('text','')), 'started_at':started,
                  'finished_at':time.time(), 'result':value}
        # Form values and URLs are deliberately absent from arguments in evidence.
        if self.transcript:
            with (self.evidence / f'agent-{self.agent}.steps.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, separators=(',', ':')) + '\n')
        return value

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=3)
        for pipe in (self.process.stdout, self.process.stderr):
            pipe.close()
        stderr = ''.join(self.stderr)
        if any(secret in stderr for secret in self.forbidden) or SECRET_SHAPE.search(stderr):
            raise RuntimeError('secret_in_mcp_stderr')

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb): self.close()

def ok(value):
    if value.get('status') != 'ok':
        raise AssertionError('expected_ok:' + str(value.get('data',{}).get('reason','unknown')))
    return value

def denied(value):
    if value.get('status') != 'denied':
        raise AssertionError('expected_denied')
    return value

def ref(value, name):
    items = value.get('data',{}).get('refs', [])
    matches = [item['ref'] for item in items if item.get('name') == name]
    if len(matches) != 1: raise AssertionError('missing_or_ambiguous_fixture_control:' + name)
    return matches[0]

def scenario(evidence: Path, agent: str):
    with MCPClient(evidence, agent) as client:
        source_hashes = client.stage['source_hashes']
        for relative, digest in source_hashes.items():
            if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != digest:
                raise AssertionError('staging_source_changed')
        attachment = ok(client.call('chrome_attachment'))
        tab = ok(client.call('chrome_open', {'url':client.stage['fixture_base'] + '/app'}))['tab_id']
        read = ok(client.call('chrome_read', {'tab_id':tab}))
        stale = ref(read, 'Increment')
        ok(client.call('chrome_type', {'tab_id':tab, 'ref':ref(read,'Notes'), 'text':'synthetic-note-' + agent}))
        # A new snapshot replaces every earlier reference even for an unchanged DOM.
        read = ok(client.call('chrome_read', {'tab_id':tab}))
        denied(client.call('chrome_click', {'tab_id':tab,'ref':stale}))
        request_id = 'increment-' + agent + '-' + client.execution_id
        click_args = {'tab_id':tab,'ref':ref(read,'Increment'),'request_id':request_id}
        ok(client.call('chrome_click', click_args))
        repeated = ok(client.call('chrome_click', click_args))
        read = ok(client.call('chrome_read', {'tab_id':tab}))
        if 'Increment count: 1' not in read.get('data',{}).get('excerpt',''):
            raise AssertionError('idempotency_actual_effect_failed')
        ok(client.call('chrome_click', {'tab_id':tab,'ref':ref(read,'Send synthetic note')}))
        ok(client.call('chrome_wait', {'tab_id':tab,'text':'Send count: 1'}))
        read = ok(client.call('chrome_read', {'tab_id':tab}))
        denied(client.call('chrome_open', {'url':client.stage['fixture_base'] + '/oauth/consent'}))
        if any(item.get('name') in ('Password','One time code') for item in read['data']['refs']):
            raise AssertionError('credential_control_reference_exposed')
        denied(client.call('chrome_open', {'url':'https://example.invalid/'}))
        denied(client.call('chrome_read', {'tab_id':client.stage['manual_tab_id']}))
        denied(client.call('chrome_close', {'tab_id':client.stage['manual_tab_id']}))
        denied(client.call('chrome_attachment', {'profile_id':'unassigned-profile'}))
        peer = client.stage['foreign_fixture']
        if agent == 'b' and (client.evidence / 'agent-a.json').exists():
            peer = json.loads((client.evidence / 'agent-a.json').read_text(encoding='utf-8'))
        foreign_tab = peer['owned_tab_id']
        foreign_ref = peer['own_ref']
        denied(client.call('chrome_read', {'tab_id':foreign_tab}))
        denied(client.call('chrome_click', {'tab_id':tab,'ref':foreign_ref}))
        denied(client.call('chrome_type', {'tab_id':foreign_tab,'ref':foreign_ref,'text':'cross-deny-canary'}))
        denied(client.call('chrome_close', {'tab_id':foreign_tab}))
        temporary = ok(client.call('chrome_open', {'url':client.stage['fixture_base'] + '/app'}))['tab_id']
        ok(client.call('chrome_close', {'tab_id':temporary}))
        final = ok(client.call('chrome_read', {'tab_id':tab}))
        for relative,digest in source_hashes.items():
            if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()!=digest:
                raise AssertionError('source_changed_during_actor_checks')
        record = {'status':'VERIFIED', 'instance_id':client.stage['instance_id'],
                  'execution_id':client.execution_id,
                  'source_hashes':source_hashes, 'agent':agent, 'assigned_session_id':client.sid,
                  'synthetic_actor_kind':client.stage.get('synthetic_actor_kind','fixture_capability_session'), 'native_identity':'parent_receipt_required',
                  'owned_tab_id':tab, 'own_ref':ref(final,'Increment'), 'foreign_tab_id':foreign_tab,
                  'manual_tab_id':client.stage['manual_tab_id'], 'foreign_owner':peer.get('assigned_session_id'),
                  'checks':['actual_mcp_stdio','own_read_type_click','ordinary_send','fresh_refs',
                            'stable_request_id_one_effect','manual_denied','foreign_tab_denied',
                            'foreign_ref_denied','profile_override_denied','oauth_denied','origin_denied',
                            'credentials_without_refs','own_close','bounded_read_without_canaries'], 'completed_at':time.time()}
        (client.evidence / f'agent-{agent}.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
        return record

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', required=True, choices=['a','b','transport_a','transport_b'])
    parser.add_argument('--evidence', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--call', action='store_true', help='Read tools/call params JSON from stdin')
    mode.add_argument('--verify', action='store_true', help='Run and retain an actual scripted MCP scenario')
    mode.add_argument('--tools', action='store_true')
    args = parser.parse_args()
    if args.verify:
        if args.agent not in ('a','b'): raise ValueError('model_actor_must_be_a_or_b')
        value = scenario(args.evidence, args.agent)
    else:
        with MCPClient(args.evidence, args.agent) as client:
            if args.tools:
                value = {'tools':client.tools}
            else:
                message = json.load(sys.stdin)
                if message.get('method') == 'tools/call': message = message['params']
                if set(message) != {'name','arguments'}: raise ValueError('expected_tools_call_params')
                value = client.call(message['name'], message['arguments'])
    print(json.dumps(value, ensure_ascii=True))
    return 0

if __name__ == '__main__':
    try: raise SystemExit(main())
    except Exception as exc:
        # Exception text is deliberately not forwarded: it may contain setup secrets.
        print(json.dumps({'status':'FAILED','error_type':type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1)
