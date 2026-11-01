"""Real native, synthetic-only acceptance. Never changes the supplied config.

Fresh task-owned repositories/state are retained next to the evidence. No downloads,
cleanup, cloud provider, production data or automatic mocked success are permitted.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import SearchService
from snapshot import absolute_path, make_directory, read_checked

SENTINEL = 'FORBIDDEN_SYNTHETIC_SENTINEL_DO_NOT_LEAK'
UNICODE = 'zażółć gęślą jaźń 日本語'
HIDDEN = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}


def git(repo, *args):
    subprocess.run(['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=' + os.devnull,
                    '-C', str(repo), *args], check=True, capture_output=True, timeout=30, **HIDDEN)


def fixture(root, other=False):
    make_directory(root)
    git(root, 'init', '-q')
    if other:
        (root / 'other.py').write_text('def isolated_other():\n    return "other_only"\n', encoding='utf-8')
        git(root, 'add', '--', 'other.py')
        return
    make_directory(root / 'src' / 'auth')
    make_directory(root / 'private')
    (root / 'main.py').write_text('def lookup():\n    return "needle"\n\n# ' + UNICODE + '\n', encoding='utf-8')
    (root / 'caller.py').write_text('from main import lookup\ndef caller():\n    return lookup()\n', encoding='utf-8')
    (root / 'src' / 'auth' / 'sessions.py').write_text('def rotate_login_cookie_after_reauthentication():\n    return "rotated"\n', encoding='utf-8')
    (root / '.env').write_text(SENTINEL, encoding='utf-8')
    (root / 'private' / 'inner.py').write_text(SENTINEL, encoding='utf-8')
    git(root, 'add', '-f', '--', 'main.py', 'caller.py', 'src/auth/sessions.py', '.env', 'private/inner.py')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', required=True, help='Existing pinned assets config; read only.')
    ap.add_argument('--evidence', required=True)
    ap.add_argument('--mode', choices=['live'], default='live')
    args = ap.parse_args(argv)
    supplied = absolute_path(args.config)
    evidence = absolute_path(args.evidence)
    cfg_bytes = read_checked(supplied, 65536)
    cfg = json.loads(cfg_bytes)
    make_directory(evidence.parent)
    root = evidence.parent / ('pilot-work-' + uuid.uuid4().hex)
    make_directory(root)
    repo, other, state = root / 'repo', root / 'other', root / 'state'
    fixture(repo)
    fixture(other, True)
    cfg.update(state_dir=str(state), allowed_repos=[str(repo), str(other)], exclude_prefixes=[])
    config = root / 'service.json'
    config.write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')
    steps = []
    source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(ROOT.glob('*.py'))}

    def step(name, action):
        started = time.monotonic()
        item = {'name': name}
        try:
            item.update(action() or {})
            item['status'] = 'PASS'
        except Exception as exc:
            item.update(status='FAIL', error=type(exc).__name__ + ': ' + str(exc)[:2000])
        item['seconds'] = round(time.monotonic() - started, 4)
        steps.append(item)

    # Deliberately hostile console locale; wire encoding must still be UTF-8.
    env = {**os.environ, 'PYTHONIOENCODING': 'cp1250', 'PYTHONUTF8': '0'}
    base = [sys.executable, '-B', str(ROOT / 'code_search.py'), '--config', str(config), '--repo', str(repo)]

    def cli(mode, query):
        proc = subprocess.run(base + ['search', '--query', query, '--mode', mode, '--max-bytes', '4096'],
                              env=env, capture_output=True, timeout=210, **HIDDEN)
        payload = json.loads(proc.stdout.decode('utf-8'))
        assert proc.returncode == 0 and payload.get('status') == 'ok', str(payload)
        assert len(proc.stdout.rstrip()) <= 4096, 'output budget exceeded'
        assert SENTINEL not in proc.stdout.decode('utf-8'), 'forbidden content leaked'
        assert payload['admitted_files'] == 3 and payload['omitted_count'] >= 2, str(payload)
        return payload

    def found(payload, path):
        assert any(x['path'] == path for x in payload['results']), str(payload)
        return {'results': len(payload['results']), 'snapshot': payload['snapshot'],
                'omissions': payload['omissions'], 'agent': payload.get('agent')}

    step('literal_unicode_cli', lambda: found(cli('literal', UNICODE), 'main.py'))
    step('auth_source_cli', lambda: found(cli('literal', 'rotate_login_cookie'), 'src/auth/sessions.py'))
    step('cpu_semantic_cli', lambda: found(cli('semantic', 'function returning the needle string'), 'main.py'))
    step('q8_agent_cli', lambda: found(cli('agent', 'Find the function returning the needle string'), 'main.py'))

    def mcp():
        requests = [
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'pilot','version':'1'}}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'code_search_status','arguments':{}}},
            {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'code_search','arguments':{'query':UNICODE,'mode':'literal'}}},
            {'jsonrpc':'2.0','id':5,'method':'tools/call','params':{'name':'eval','arguments':{}}},
            {'jsonrpc':'2.0','id':6,'method':'tools/call','params':{'name':'code_search','arguments':{'query':'needle','repo':str(other)}}},
            {'jsonrpc':'2.0','id':7,'method':'tools/call','params':{'name':'code_search','arguments':{'query':'function returning the needle string','mode':'semantic'}}},
        ]
        proc = subprocess.run(base + ['mcp'], input=('\n'.join(json.dumps(x,ensure_ascii=False) for x in requests)+'\n').encode('utf-8'),
                              env=env, capture_output=True, timeout=210, **HIDDEN)
        assert proc.returncode == 0, proc.stderr.decode('utf-8',errors='replace')[:1000]
        replies = [json.loads(x) for x in proc.stdout.decode('utf-8').splitlines()]
        assert [x['id'] for x in replies] == [1,2,3,4,5,6,7]
        assert sorted(x['name'] for x in replies[1]['result']['tools']) == ['code_search','code_search_status']
        assert replies[2]['result']['isError'] is False
        status = json.loads(replies[2]['result']['content'][0]['text'])
        assert status['network_fallback'] is False
        literal = json.loads(replies[3]['result']['content'][0]['text'])
        assert UNICODE in json.dumps(literal,ensure_ascii=False)
        assert replies[4]['error']['code'] == -32602 and 'result' not in replies[4]
        assert replies[5]['result']['isError'] is True
        found(json.loads(replies[6]['result']['content'][0]['text']), 'main.py')
        assert SENTINEL not in proc.stdout.decode('utf-8')
        return {'reply_ids':[x['id'] for x in replies], 'protocol_version':replies[0]['result']['protocolVersion']}
    step('native_stdio_mcp', mcp)

    def reuse_mutation():
        with SearchService(config,repo) as service:
            first = service.search('function returning needle',mode='semantic')
            second = service.search('function returning needle',mode='semantic')
            assert first['status'] == second['status'] == 'ok'
            assert first['snapshot']['fingerprint'] == second['snapshot']['fingerprint']
            assert first['state']['files'] == second['state']['files'], 'identical query creates more derived files'
            (repo/'main.py').write_text('def lookup():\n    return "changed_after_edit"\n',encoding='utf-8')
            changed = service.search('changed_after_edit',mode='semantic')
            assert changed['status'] == 'ok'
            assert changed['snapshot']['fingerprint'] != first['snapshot']['fingerprint']
            found(changed,'main.py')
            frozen = service._last_snapshot
            assert not (frozen.root/'.env').exists() and not (frozen.root/'private').exists()
            forbidden = service.search(SENTINEL,mode='literal')
            assert forbidden['status'] == 'ok' and forbidden['results'] == []
            assert SENTINEL not in json.dumps(forbidden)
        with SearchService(config,other) as service:
            result = service.search('other_only',mode='literal')
            assert result['status'] == 'ok' and result['snapshot']['fingerprint'] != changed['snapshot']['fingerprint']
            assert not any(x['path']=='main.py' for x in result['results'])
        assert read_checked(supplied,65536) == cfg_bytes, 'supplied config changed'
        return {'reused':first['snapshot']['fingerprint'],'edited':changed['snapshot']['fingerprint'],
                'other':result['snapshot']['fingerprint'],'sentinel_absent':True,'config_unchanged':True}
    step('cache_mutation_cross_repo_sentinel',reuse_mutation)
    summary = {'mode':'live','backend':'pinned_native','source_hashes':source_hashes,
               'config':str(config),'repo':str(repo),'workdir_retained':str(root),
               'steps':steps,'status':'PASS' if all(x['status']=='PASS' for x in steps) else 'FAIL'}
    evidence.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    sys.stdout.buffer.write((json.dumps({'status':summary['status'],'evidence':str(evidence)})+'\n').encode('utf-8'))
    return 0 if summary['status']=='PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
