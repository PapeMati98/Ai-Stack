"""Dependency-free verifier and retained synthetic staging; never uses an owner profile."""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from managed import ManagedBroker, config_from_dict, extension_config, load_config, make_managed_server

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'tests'))
from client import MCPClient, CANARIES, ok, denied, ref, scenario, load_stage

PYTHON = sys.executable
NODE = shutil.which('node')
POWERSHELL = shutil.which('pwsh')
PLAYWRIGHT = None  # Explicit --playwright-root for browser UAT.
BROWSER = None  # Explicit --browser-exe; the hash pin is still enforced.
BROWSER_SHA256 = '9b07943f834485b43c9d54caea2951c78715f9f07070a80ab6658465ebd3e711'
TASK_HELPER = None  # Explicit --background-helper / CHROME_RELAY_BACKGROUND_HELPER.
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
ACTIONS = ['chrome_attachment','chrome_open','chrome_read','chrome_click','chrome_type','chrome_wait','chrome_close']

def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True), encoding='utf-8')

def runtime_parent(evidence):
    evidence = Path(evidence).resolve()
    return evidence.parent / (evidence.name + '-runtime')

def private_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        os.chmod(path, 0o700)
        return
    # Protected, inheritable DACL: only owner and LocalSystem. No token in argv.
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    descriptor = ctypes.c_void_p()
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.SetFileSecurityW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p]
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW('D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)', 1, ctypes.byref(descriptor), None):
        raise RuntimeError('private_dacl_create_failed')
    try:
        if not advapi.SetFileSecurityW(str(path), 4 | 0x80000000, descriptor):
            raise RuntimeError('private_dacl_set_failed')
    finally:
        ctypes.WinDLL('kernel32').LocalFree(descriptor)

def source_hashes():
    names = ['broker.py','managed.py','registration.py','mcp.py','cli.py','extension/manifest.json','extension/sw.js','extension/login_gate.js',
             'uat.py','README.md','install.ps1','tests/client.py','tests/test_broker.py','tests/test_mcp.py',
             'tests/test_managed.py','tests/test_registration.py','tests/test_saved_login.py','tests/test_saved_login.cjs','tests/test_extension.cjs','tests/fixtures/browser.cjs','tests/fixtures/app.html']
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}

def process_start(pid):
    if os.name != 'nt': return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle: return None
    created, exited, system, user = [ctypes.c_uint64() for _ in range(4)]
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] * 5
    try:
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
            return None
        return created.value
    finally:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)

def current_desktop():
    if os.name != 'nt': return 'non_windows'
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetThreadDesktop.restype = ctypes.c_void_p
    handle = user.GetThreadDesktop(ctypes.WinDLL('kernel32').GetCurrentThreadId())
    needed = ctypes.c_uint32()
    buffer = ctypes.create_unicode_buffer(256)
    user.GetUserObjectInformationW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    if not user.GetUserObjectInformationW(handle, 2, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
        raise RuntimeError('desktop_identity_unavailable')
    return buffer.value

class BrowserHarness:
    def __init__(self, args, profile, fixture_base):
        self.process = subprocess.Popen([args.node_exe, str(ROOT / 'tests' / 'fixtures' / 'browser.cjs')],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=FLAGS)
        self.lines, self.errors = queue.Queue(), []
        threading.Thread(target=self.reader, daemon=True).start()
        threading.Thread(target=self.error_reader, daemon=True).start()
        self.send({'playwright_root':args.playwright_root,'browser_exe':args.browser_exe,
                   'extension_dir':str(ROOT / 'extension'),'profile_dir':str(profile),'fixture_url':fixture_base,
                   'screenshot_path':str(args.evidence / 'fixture.png')})
        self.info = self.receive(35)
        if self.info.get('event') != 'ready': raise RuntimeError('browser_not_ready')

    def reader(self):
        try:
            for line in self.process.stdout:
                self.lines.put(json.loads(line.decode('utf-8')))
        except (ValueError, UnicodeError):
            self.lines.put({'event':'error'})
        self.lines.put({'event':'closed'})

    def error_reader(self):
        for line in self.process.stderr:
            # Never persist raw browser stderr, which may include setup details.
            self.errors.append(hashlib.sha256(line).hexdigest())

    def send(self, value):
        self.process.stdin.write((json.dumps(value, separators=(',',':')) + '\n').encode('utf-8'))
        self.process.stdin.flush()

    def receive(self, timeout=35):
        value = self.lines.get(timeout=timeout)
        if value.get('event') in ('error','closed'): raise RuntimeError('browser_harness_failed')
        return value

    def call(self, command, **params):
        identity = uuid.uuid4().hex
        self.send({'command':command,'id':identity,**params})
        response = self.receive()
        if response.get('id') != identity: raise RuntimeError('harness_response_mismatch')
        return response

    def close(self):
        if self.process.poll() is None:
            try:
                self.call('close')
                self.process.wait(timeout=8)
            except Exception:
                self.process.terminate()
                self.process.wait(timeout=8)

class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        route = self.path.split('?', 1)[0]
        manual_page = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Synthetic owner tab</title>'
                       '<body><h1>Synthetic owner tab</h1><p>No account data.</p>'
                       '<p id="count">Increment count: 0</p><p id="sent">Send count: 0</p>'
                       '<p id="delay">Delay count: 0</p></body></html>')
        frame_pages = {
            '/frameset': '<!doctype html><html><head><title>Synthetic frameset</title></head><frameset cols="30%,70%"><frame src="/frame-menu"><frame src="/frame-content" name="main"></frameset></html>',
            '/frame-menu': '<!doctype html><html><body><h1>Synthetic frame menu</h1><a href="/frame-dashboard" target="main">Frame dashboard</a><a href="/frame-dashboard?credentials=1" target="main">Risky optional URL</a></body></html>',
            '/frame-content': '<!doctype html><html><body><h1>Synthetic child content marker</h1><p>Read only fixture. No invoice records.</p></body></html>',
            '/frame-dashboard': '<!doctype html><html><body><h1>Synthetic observed menu destination</h1><p>Navigation marker confirmed.</p></body></html>',
        }
        if route == '/manual':
            body = manual_page.encode('utf-8')
        elif route in frame_pages:
            body = frame_pages[route].encode('utf-8')
        elif route in ('/app','/popup','/oauth/consent'):
            if route == '/app': time.sleep(0.2)  # Make overlap observable in actual independent client calls.
            body = (ROOT / 'tests' / 'fixtures' / 'app.html').read_bytes()
        else:
            self.send_error(404); return
        self.send_response(200)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Set-Cookie','relay_fixture=COOKIE_CANARY_9f12b34e; HttpOnly; SameSite=Strict; Path=/')
        self.send_header('Cache-Control','no-store')
        self.end_headers(); self.wfile.write(body)

def fixture_server():
    server = ThreadingHTTPServer(('127.0.0.1',0), FixtureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

def control(evidence, command):
    _, stage, _ = load_stage(evidence)
    private = json.loads((Path(stage['runtime_root']) / 'control.json').read_text(encoding='utf-8'))
    data = json.dumps({'command':command,'instance_id':stage['instance_id']}).encode('utf-8')
    request = Request(stage['control_base'] + '/control', data=data,
                      headers={'Authorization':'Bearer ' + private['token'],'Content-Type':'application/json'})
    try:
        with urlopen(request, timeout=40) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        # Keep the rejection status useful to the operator without returning
        # a body, URL, or any controller/browser details into public evidence.
        raise UATFailure(f'control_{command}_http_{exc.code}') from None
    except URLError:
        raise UATFailure(f'control_{command}_unavailable') from None

class UATFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def serve_staging(args):
    evidence = args.evidence.resolve()
    private_root = runtime_parent(evidence)
    private_directory(private_root)
    desktop = current_desktop()
    if os.name == 'nt' and desktop != 'AgentStackBackground':
        raise RuntimeError('staging_requires_private_task_desktop')
    if hashlib.sha256(Path(args.browser_exe).read_bytes()).hexdigest() != args.browser_sha256:
        raise RuntimeError('browser_pin_mismatch')
    initial_hashes = source_hashes()
    instance = uuid.uuid4().hex
    runtime_root = private_root / ('staging-' + instance)
    private_directory(runtime_root)
    fixture = fixture_server()
    fixture_base = 'http://127.0.0.1:' + str(fixture.server_address[1])
    browser = BrowserHarness(args, runtime_root / 'browser-profile', fixture_base)
    state_dir = runtime_root / 'managed-state'
    config_data = {'schema':'chrome-relay-managed/1',
                   'grant':{'grant_id':'synthetic-grant-' + instance,
                            'profile_id':'synthetic-profile-' + instance,
                            'allowed_origins':[fixture_base], 'allowed_actions':ACTIONS,
                            'extension_id':browser.info['extension_id'],
                            'harnesses':{'codex':{'transport':'mcp'}},
                            'persistent':True,'synthetic':True},
                   'broker_base':'http://127.0.0.1:1','state_dir':str(state_dir),
                   'extension_token':secrets.token_urlsafe(48),
                   'controller_key':secrets.token_urlsafe(48),'client_ttl_s':600}
    initial_config = config_from_dict(config_data)
    broker = ManagedBroker(initial_config.grant, initial_config.state_dir,
                           client_ttl_s=initial_config.client_ttl_s,
                           request_timeout=3.0, poll_timeout=0.5)
    server = make_managed_server(broker, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    broker_base = 'http://127.0.0.1:' + str(server.server_address[1])
    config_data['broker_base'] = broker_base
    config = config_from_dict(config_data)
    managed_config_path = runtime_root / 'managed-config.json'
    dump(managed_config_path, config_data)
    configuration = extension_config(config)
    configured = browser.call('configure', config=configuration)
    if source_hashes() != initial_hashes:
        browser.close(); server.shutdown(); server.server_close(); broker.close(); fixture.shutdown()
        raise RuntimeError('source_changed_during_browser_start')
    if configured.get('configured') is not True:
        browser.close(); server.shutdown(); server.server_close(); broker.close(); fixture.shutdown()
        raise RuntimeError('managed_owner_attach_failed')
    control_token = secrets.token_urlsafe(32)
    dump(runtime_root / 'control.json', {'token':control_token})
    stop = threading.Event()
    lock = threading.Lock()
    stage = {'status':'STARTING','instance_id':instance,'staging_pid':os.getpid(),
             'staging_process_start':process_start(os.getpid()),'node_pid':browser.process.pid,
             'node_process_start':process_start(browser.process.pid),'desktop':desktop,
             'broker_base':broker_base,'fixture_base':fixture_base,
             'profile_id':config.grant.profile_id,'managed_config_path':str(managed_config_path),
             'runtime_root':str(runtime_root),
             'managed_generation':config.grant.wire_generation(),'grant_persistent':config.grant.persistent,
             'managed_harness':'codex','synthetic_actor_kind':'managed_fixture_client',
             'native_identity':'parent_receipt_required',
             'manual_tab_id':configured['manual_tab_id'],'python_exe':args.python_exe,
             'browser_sha256':args.browser_sha256,'source_hashes':initial_hashes,
             'created_at':time.time(),'expires_at':time.time() + 7200}
    class ControlHandler(BaseHTTPRequestHandler):
        def log_message(self, *arguments): pass
        def do_POST(self):
            if self.path != '/control':
                self.send_error(403); return
            if self.headers.get('Content-Type') != 'application/json': self.send_error(415); return
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 1024: raise ValueError('length')
                value = json.loads(self.rfile.read(length))
                if set(value) != {'command','instance_id'} or value['instance_id'] != instance: raise ValueError('identity')
                if self.headers.get('Authorization') != 'Bearer ' + control_token: raise ValueError('authorization')
                with lock:
                    command = value['command']
                    if command == 'stop-staging':
                        response = {'status':'STOPPING','instance_id':instance}
                        stop.set()
                    elif command in ('snapshot','owner-gesture','worker-restart','restart','release-dialogs'):
                        response = browser.call(command)
                        if command == 'owner-gesture':
                            stage['manual_tab_id'] = response['manual_tab_id']
                            dump(evidence / 'staging.json', stage)
                    else: raise ValueError('command')
            except Exception:
                self.send_error(400); return
            body = json.dumps(response).encode('utf-8')
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    controller = ThreadingHTTPServer(('127.0.0.1',0),ControlHandler)
    threading.Thread(target=controller.serve_forever, daemon=True).start()
    stage['control_base'] = 'http://127.0.0.1:' + str(controller.server_address[1])
    stage['status'] = 'READY'
    dump(evidence / 'staging.json', stage)
    try:
        # Retain one synthetic tab from a closed MCP process for later stale-session denial.
        with MCPClient(evidence, 'transport_b', transcript=False) as peer:
            tab = ok(peer.call('chrome_open', {'url':fixture_base + '/app'}))['tab_id']
            read = ok(peer.call('chrome_read', {'tab_id':tab}))
            stage['foreign_fixture'] = {'owned_tab_id':tab,'own_ref':ref(read,'Increment'),
                                        'assigned_session_id':peer.sid}
        dump(evidence / 'staging.json', stage)
        while not stop.wait(1):
            if browser.process.poll() is not None: raise RuntimeError('staging_browser_exited')
            if time.time() >= stage['expires_at']: break
    finally:
        browser.close(); server.shutdown(); server.server_close(); controller.shutdown(); controller.server_close()
        broker.close(); fixture.shutdown()
        stage['status'] = 'STOPPED'
        dump(evidence / 'staging.json', stage)
    return 0

def spawn_staging(args):
    if os.name == 'nt' and current_desktop() != 'AgentStackBackground':
        raise RuntimeError('launcher_requires_private_task_desktop')
    args.evidence.mkdir(parents=True, exist_ok=True)
    command = [args.python_exe,'-B',str(ROOT / 'uat.py'),'--serve-staging','--evidence',str(args.evidence),
               '--python-exe',args.python_exe,'--node-exe',args.node_exe,'--playwright-root',args.playwright_root,
               '--browser-exe',args.browser_exe,'--browser-sha256',args.browser_sha256]
    log = (args.evidence / 'staging-process.log').open('ab')
    process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                               creationflags=FLAGS, close_fds=True)
    log.close()
    dump(args.evidence / 'staging-launch.json', {'pid':process.pid,'process_start':process_start(process.pid),
                                               'desktop':current_desktop(),'argv_has_tokens':False})
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        handle = args.evidence / 'staging.json'
        if handle.exists():
            stage = json.loads(handle.read_text(encoding='utf-8'))
            if stage.get('status') == 'READY' and stage.get('foreign_fixture'):
                print(json.dumps({'status':'READY','instance_id':stage['instance_id'],'staging_pid':process.pid}))
                return 0
        if process.poll() is not None: raise RuntimeError('staging_start_failed')
        time.sleep(0.1)
    process.terminate(); process.wait(timeout=5)
    raise RuntimeError('staging_start_timeout')

def start_staging(args):
    existing = args.evidence / 'staging.json'
    if existing.exists():
        stage = json.loads(existing.read_text(encoding='utf-8'))
        if stage.get('status') == 'READY' and process_start(stage['staging_pid']) == stage['staging_process_start']:
            if stage['source_hashes'] != source_hashes(): raise RuntimeError('reuse_source_mismatch')
            return stage
        raise RuntimeError('evidence_already_has_retained_staging_use_new_directory')
    args.evidence.mkdir(parents=True, exist_ok=True)
    try:
        private_directory(runtime_parent(args.evidence))
    except RuntimeError:
        dump(args.evidence / 'staging-failure.json',
             {'status':'FAILED','check':'private_staging_dacl',
              'expected':'owner_and_system_private_acl','actual':'private_dacl_unavailable',
              'browser_started':False,'helper_invoked':False})
        raise
    command = [args.python_exe,'-B',str(ROOT / 'uat.py'),'--spawn-staging','--evidence',str(args.evidence),
               '--python-exe',args.python_exe,'--node-exe',args.node_exe,'--playwright-root',args.playwright_root,
               '--browser-exe',args.browser_exe,'--browser-sha256',args.browser_sha256]
    if os.name == 'nt':
        helper_path = Path(args.background_helper)
        spec = importlib.util.spec_from_file_location('chrome_relay_task_background', helper_path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        result = module.run(command, ROOT, 'chrome-relay-staging-start')
        if result['exit_code'] != 0: raise RuntimeError('private_staging_launcher_failed')
    else:
        subprocess.run(command, cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _, stage, _ = load_stage(args.evidence)
    if not stage.get('foreign_fixture'): raise RuntimeError('staging_fixture_not_ready')
    return stage

def offline_checks(args):
    args.evidence.mkdir(parents=True, exist_ok=True)
    commands = [[args.python_exe,'-B','-m','unittest','discover','-s','tests','-p','test_*.py'],
                [args.node_exe,'tests/test_extension.cjs']]
    results = []
    for index, command in enumerate(commands):
        result = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=FLAGS, timeout=120)
        output = result.stdout.decode('utf-8', errors='replace')
        log = args.evidence / f'offline-{index}.log'; log.write_text(output, encoding='utf-8')
        results.append({'argv':command,'exit_code':result.returncode,'log_sha256':hashlib.sha256(result.stdout).hexdigest(),
                        'log':str(log)})
    installer = installer_checks(args) if os.name == 'nt' else {'status':'NOT_WINDOWS'}
    record = {'status':'VERIFIED' if all(r['exit_code']==0 for r in results) else 'FAILED',
              'checks':results,'installer':installer,'source_hashes':source_hashes(),'completed_at':time.time()}
    dump(args.evidence / 'offline.json',record)
    if record['status'] != 'VERIFIED': raise RuntimeError('offline_checks_failed')
    return record


def installer_checks(args):
    """Execute the inert installer and generated launcher with synthetic capabilities."""
    root = runtime_parent(args.evidence) / ('offline-installer-' + uuid.uuid4().hex)
    # Offline placeholders never authorize a live service; live staging still
    # requires private_directory and fails closed when its DACL cannot be set.
    root.mkdir(parents=True)
    install_root = root / 'versions'
    capability_path = root / 'client.json'
    token = 'synthetic-launcher-not-live-token-canary'
    capability = {'broker_base':'http://127.0.0.1:9','session_id':'synthetic-launcher-session',
                  'profile_id':'synthetic-launcher-profile','token':token}
    dump(capability_path, capability)
    powershell = getattr(args,'powershell_exe',POWERSHELL)
    command = [powershell,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(ROOT / 'install.ps1'),
               '-InstallRoot',str(install_root),'-PythonExe',args.python_exe,'-SourceRoot',str(ROOT),
               '-ClientCapabilityPath',str(capability_path),'-NativeSessionId',capability['session_id'],
               '-ProfileId',capability['profile_id'],'-BrokerBase',capability['broker_base']]
    checks = []
    def execute(label, argv, expected_code, data=None):
        env = dict(os.environ)
        # Windows PowerShell must discover its own modules when invoked from pwsh.
        env.pop('PSModulePath',None)
        result = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=FLAGS, timeout=30, env=env)
        if token.encode() in result.stdout: raise AssertionError('launcher_token_in_output')
        passed = result.returncode == 0
        checks.append({'check':label,'argv':argv,'expected':'success' if expected_code == 0 else 'denied',
                       'actual':'success' if passed else 'denied','exit_code':result.returncode,
                       'output_sha256':hashlib.sha256(result.stdout).hexdigest()})
        if passed != (expected_code == 0):
            dump(args.evidence / 'installer.json', {'status':'FAILED','checks':checks})
            (args.evidence / 'installer-failure.log').write_bytes(result.stdout)
            raise AssertionError('installer_check_failed:' + label)
        return result.stdout
    dry = json.loads(execute('dry_run_no_destination', command + ['-DryRun'], 0))
    if dry['status'] != 'DRY_RUN' or install_root.exists(): raise AssertionError('dry_run_mutated_destination')
    execute('stage_inert', command, 0)
    execute('same_version_idempotent', command, 0)
    installed_versions = sorted(path for path in install_root.iterdir() if path.is_dir())
    if len(installed_versions) != 1:
        raise AssertionError('inert_installer_version_count')
    version = installed_versions[0]
    snippets = list(version.glob('assigned-process-*.json'))
    if len(snippets) != 1: raise AssertionError('assigned_snippet_missing')
    snippet = json.loads(snippets[0].read_text(encoding='utf-8'))
    receipt = json.loads((version / 'install-receipt.json').read_text(encoding='utf-8'))
    if receipt.get('version') != version.name: raise AssertionError('inert_installer_version_mismatch')
    if any(receipt[key] for key in ('broker_started','extension_activated','global_mcp_modified','autostart_modified','policy_modified','files_deleted')):
        raise AssertionError('installer_not_inert')
    if token in snippets[0].read_text(encoding='utf-8') or '--policy-file' in snippet['args']:
        raise AssertionError('controller_policy_or_token_in_snippet')
    messages = [
        {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'synthetic-launcher','version':'1'}}},
        {'jsonrpc':'2.0','method':'notifications/initialized'},
        {'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}}]
    wire = ''.join(json.dumps(value) + '\n' for value in messages).encode('utf-8')
    output = execute('actual_launcher_mcp_stdio', [snippet['command'],*snippet['args']], 0, wire)
    responses = [json.loads(line) for line in output.splitlines()]
    if not any(value.get('id') == 2 and len(value.get('result',{}).get('tools',[])) == len(ACTIONS) for value in responses):
        raise AssertionError('launcher_did_not_expose_actual_mcp')
    cli_args = list(snippet['args']); cli_args[-1] = 'cli'
    cli_output = execute('actual_launcher_cli_schema_denial',[snippet['command'],*cli_args],1,
                         json.dumps({'name':'chrome_attachment','arguments':{'profile_id':'foreign-profile'}}).encode('utf-8'))
    if json.loads(cli_output).get('status') != 'denied': raise AssertionError('cli_did_not_fail_closed')
    for field, bad in (('session_id','other-session'),('profile_id','other-profile'),
                       ('broker_base','http://127.0.0.1:10'),('token','bad')):
        dump(capability_path, dict(capability, **{field:bad}))
        execute('reject_mismatched_' + field,[snippet['command'],*snippet['args']],1,wire)
        execute('installer_reject_mismatched_' + field,command + ['-DryRun'],1)
    dump(capability_path, dict(capability, sessions={'another-session':'synthetic-unassigned-token'}))
    execute('reject_controller_map',[snippet['command'],*snippet['args']],1,wire)
    execute('installer_reject_controller_map',command + ['-DryRun'],1)
    capability_path.write_text('[]',encoding='utf-8')
    execute('reject_nonobject_capability',[snippet['command'],*snippet['args']],1,wire)
    capability_path.write_text(' ' * 65537,encoding='utf-8')
    execute('reject_oversized_capability',[snippet['command'],*snippet['args']],1,wire)
    dump(capability_path, capability)
    actor_root = root / 'actor-tools'
    dump(actor_root / 'staging.json',{'status':'READY','expires_at':time.time()+60,
         'assigned_session_ids':{'a':capability['session_id']},'profile_id':capability['profile_id'],
         'broker_base':capability['broker_base'],'python_exe':args.python_exe})
    actor_capability = actor_root / 'private' / 'client-a.json'
    dump(actor_capability,capability)
    actor_command = [args.python_exe,'-B',str(ROOT / 'tests' / 'client.py'),'--agent','a',
                     '--tools','--evidence',str(actor_root)]
    # No policy.json exists: an actor must not load the controller's token map.
    actor_output = execute('actor_actual_stdio_single_capability_without_controller',actor_command,0)
    if len(json.loads(actor_output)['tools']) != len(ACTIONS): raise AssertionError('actor_tools_missing')
    for field, bad in (('session_id','foreign-session'),('profile_id','foreign-profile'),
                       ('broker_base','http://127.0.0.1:10'),('token','bad')):
        dump(actor_capability,dict(capability, **{field:bad}))
        execute('actor_reject_mismatched_' + field,actor_command,1)
    dump(actor_capability,dict(capability,sessions={'foreign-session':'synthetic-unassigned-token'}))
    execute('actor_reject_controller_map',actor_command,1)
    dump(actor_capability,capability)
    # Native fixture only: an assigned shell sandbox cannot read owner-only
    # controller storage, but may read its one expiring workspace capability.
    native_capability = actor_root / 'native-actors' / 'a' / 'client.json'
    native_stage = json.loads((actor_root / 'staging.json').read_text(encoding='utf-8'))
    native_stage.update(synthetic_actor_kind='fixture_capability_session',
                        native_actor_capability_files={'a':str(native_capability)})
    dump(actor_root / 'staging.json',native_stage)
    dump(native_capability,capability)
    dump(actor_capability,{'controller_is_not_an_actor_capability':True})
    execute('native_fixture_actual_mcp_without_controller_read',actor_command,0)
    dump(native_capability,dict(capability,session_id='foreign-session'))
    execute('native_fixture_foreign_session_denied',actor_command,1)
    dump(native_capability,dict(capability,sessions={'foreign-session':'synthetic-unassigned-token'}))
    execute('native_fixture_controller_map_denied',actor_command,1)
    dump(native_capability,capability)
    dump(actor_root / 'staging.json',dict(native_stage,synthetic_actor_kind='real-profile'))
    execute('native_fixture_override_never_real_profile',actor_command,1)
    dump(actor_root / 'staging.json',dict(native_stage,native_actor_capability_files={'a':str(actor_capability)}))
    execute('native_fixture_wrong_path_denied',actor_command,1)
    dump(actor_root / 'staging.json',native_stage)
    execute('retain_previous_version',command + ['-Version','1.0.1'],0)
    newer = json.loads((install_root / '1.0.1' / 'install-receipt.json').read_text(encoding='utf-8'))
    if str(version) not in newer['previous_versions']: raise AssertionError('rollback_previous_missing')
    modified = version / 'README.md'
    modified.write_text(modified.read_text(encoding='utf-8') + '\nSynthetic differing bytes.\n',encoding='utf-8')
    digest = hashlib.sha256(modified.read_bytes()).hexdigest()
    execute('different_existing_bytes_preserved',command,1)
    if hashlib.sha256(modified.read_bytes()).hexdigest() != digest: raise AssertionError('installer_overwrote_existing_version')
    record = {'status':'VERIFIED','checks':checks,'private_fixture_root':str(root),'files_deleted':0}
    dump(args.evidence / 'installer.json',record)
    return record


def frame_read_navigation(client):
    # Actual assigned MCP tools on a real pinned CFT. No controller/page JS.
    owned = []
    try:
        opened = ok(client.call('chrome_open', {'url':client.stage['fixture_base'] + '/frameset'}))
        tab = opened['tab_id']; owned.append(tab)
        time.sleep(0.2)
        read = ok(client.call('chrome_read', {'tab_id':tab}))
        if read['data'].get('diagnostics', {}).get('reader_status') == 'loading':
            time.sleep(0.2)
            read = ok(client.call('chrome_read', {'tab_id':tab}))
        data = read['data']; diagnostics = data['diagnostics']
        if 'Synthetic child content marker' not in data['excerpt']:
            raise AssertionError('actual_frame_text_missing')
        if diagnostics['main_excerpt_chars'] != 0 or diagnostics['child_excerpt_chars'] <= 0 or diagnostics['same_origin_frame_count'] < 2:
            raise AssertionError('actual_frame_contribution_missing')
        if len(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()) > 4096:
            raise AssertionError('actual_frame_result_bound')
        observed = next(item for item in data['refs'] if item['name'] == 'Frame dashboard')
        if observed.get('url') != client.stage['fixture_base'] + '/frame-dashboard':
            raise AssertionError('actual_observed_frame_link_missing')
        target = ok(client.call('chrome_open', {'url':observed['url']}))['tab_id']; owned.append(target)
        destination = ok(client.call('chrome_read', {'tab_id':target}))
        if 'Synthetic observed menu destination' not in destination['data']['excerpt']:
            raise AssertionError('actual_frame_menu_navigation_missing')
        return {'status':'PASS','diagnostics':diagnostics,'root_excerpt_bytes':len(data['excerpt'].encode()),
                'destination_excerpt_bytes':len(destination['data']['excerpt'].encode()),
                'navigation':'observed_same_origin_information_GET','click_type_calls':0,'business_writes':0}
    finally:
        for tab in reversed(owned): ok(client.call('chrome_close', {'tab_id':tab}))


def concurrent_transport(evidence):
    barrier, completed, records, failures = threading.Barrier(2), threading.Barrier(2), {}, []
    def actor(name):
        try:
            with MCPClient(evidence, name, transcript=False) as client:
                assert_managed_profile_boundary(client)
                barrier.wait(timeout=10)
                started = time.time()
                opened = ok(client.call('chrome_open', {'url':client.stage['fixture_base'] + '/app'}))
                finished = time.time()
                tab = opened['tab_id']
                read = ok(client.call('chrome_read', {'tab_id':tab}))
                ok(client.call('chrome_type', {'tab_id':tab,'ref':ref(read,'Notes'),'text':'transport-fixture'}))
                read = ok(client.call('chrome_read', {'tab_id':tab}))
                ok(client.call('chrome_click', {'tab_id':tab,'ref':ref(read,'Increment')}))
                read = ok(client.call('chrome_read', {'tab_id':tab}))
                if 'Increment count: 1' not in read['data']['excerpt']: raise AssertionError('transport_effect')
                ok(client.call('chrome_click', {'tab_id':tab,'ref':ref(read,'Send synthetic note')}))
                ok(client.call('chrome_wait', {'tab_id':tab,'text':'Send count: 1'}))
                denied(client.call('chrome_open', {'url':client.stage['fixture_base'] + '/oauth/consent'}))
                denied(client.call('chrome_open', {'url':'https://example.invalid/'}))
                denied(client.call('chrome_read', {'tab_id':client.stage['manual_tab_id']}))
                denied(client.call('chrome_close', {'tab_id':client.stage['manual_tab_id']}))
                denied(client.call('chrome_attachment', {'profile_id':'unassigned-profile'}))
                read = ok(client.call('chrome_read', {'tab_id':tab}))
                if len(json.dumps(read['data'],separators=(',',':')).encode('utf-8'))>4096:
                    raise AssertionError('actual_read_bound')
                if any(item.get('name') in ('Password','One time code') for item in read['data']['refs']):
                    raise AssertionError('credential_reference_exposed')
                frame_proof = frame_read_navigation(client)
                records[name] = {'status':'VERIFIED','frame_read_navigation':frame_proof,
                                 'started_at':started,'finished_at':finished,'owned_tab_id':tab,
                                 'own_ref':ref(read,'Increment'),'assigned_session_id':client.sid,
                                 'profile_id':client.stage['profile_id'],
                                 'checks':['actual_mcp_stdio','read_type_click','ordinary_send','wait',
                                            'oauth_origin_denied','foreign_origin_denied','manual_tab_denied',
                                            'wrong_profile_denied','profile_override_absent_from_tools',
                                            'bounded_redacted_read']}
                completed.wait(timeout=15)
                peer = records['transport_b' if name == 'transport_a' else 'transport_a']
                denied(client.call('chrome_read', {'tab_id':peer['owned_tab_id']}))
                denied(client.call('chrome_click', {'tab_id':tab,'ref':peer['own_ref']}))
                denied(client.call('chrome_close', {'tab_id':peer['owned_tab_id']}))
                records[name]['checks'].extend(['live_peer_tab_denied','live_peer_ref_denied'])
        except Exception as exc: failures.append(type(exc).__name__)
    threads = [threading.Thread(target=actor, args=(name,)) for name in ('transport_a','transport_b')]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=35)
    if failures or any(t.is_alive() for t in threads) or len(records)!=2: raise RuntimeError('concurrent_transport_failed')
    a,b = records['transport_a'],records['transport_b']
    if max(a['started_at'],b['started_at']) >= min(a['finished_at'],b['finished_at']): raise AssertionError('requests_did_not_overlap')
    if a['owned_tab_id']==b['owned_tab_id']: raise AssertionError('transport_ownership_collision')
    if not a['assigned_session_id'] or a['assigned_session_id']==b['assigned_session_id']:
        raise AssertionError('managed_clients_share_or_lack_session')
    if a['profile_id'] != b['profile_id']:
        raise AssertionError('managed_clients_not_same_granted_profile')
    a['overlap_verified'] = b['overlap_verified'] = True
    return records

def assert_managed_profile_boundary(client):
    """Actual MCP tools bind the managed grant without caller profile input."""
    if not client.tools:
        raise AssertionError('managed_mcp_tools_missing')
    for tool in client.tools:
        schema = tool.get('inputSchema', {})
        properties = schema.get('properties', {})
        if schema.get('additionalProperties') is not False or 'profile_id' in properties:
            raise AssertionError('managed_mcp_profile_override_exposed')

def managed_sequential(evidence):
    stage = load_stage(evidence)[1]
    first_started = time.time()
    with MCPClient(evidence,'transport_a',transcript=False) as first:
        assert_managed_profile_boundary(first)
        first_attachment = ok(first.call('chrome_attachment'))['data']
        first_sid = first.sid
        if first_attachment.get('profile_id') != stage['profile_id'] or first_attachment.get('stopped') is not False:
            raise AssertionError('managed_first_client_not_attached')
        first_tab = ok(first.call('chrome_open', {'url':stage['fixture_base'] + '/app'}))['tab_id']
        first_read = ok(first.call('chrome_read', {'tab_id':first_tab}))
        ok(first.call('chrome_click', {'tab_id':first_tab,'ref':ref(first_read,'Increment')}))
        first_read = ok(first.call('chrome_read', {'tab_id':first_tab}))
        if 'Increment count: 1' not in first_read['data']['excerpt']:
            raise AssertionError('managed_first_client_effect_missing')
    first_finished = time.time()
    second_started = time.time()
    with MCPClient(evidence,'transport_b',transcript=False) as second:
        assert_managed_profile_boundary(second)
        second_attachment = ok(second.call('chrome_attachment'))['data']
        second_sid = second.sid
        if second_attachment.get('profile_id') != stage['profile_id'] or second_attachment.get('stopped') is not False:
            raise AssertionError('managed_reconnected_client_grant_missing')
        if second_sid == first_sid:
            raise AssertionError('managed_reconnect_reused_session')
        denied(second.call('chrome_read', {'tab_id':first_tab}))
        second_tab = ok(second.call('chrome_open', {'url':stage['fixture_base'] + '/app'}))['tab_id']
        second_read = ok(second.call('chrome_read', {'tab_id':second_tab}))
        if 'Increment count: 0' not in second_read['data']['excerpt']:
            raise AssertionError('managed_sequential_fixture_not_independent')
        ok(second.call('chrome_close', {'tab_id':second_tab}))
    second_finished = time.time()
    if first_finished > second_started:
        raise AssertionError('managed_clients_not_sequential')
    return {'status':'VERIFIED','overlap':False,'first_session_id':first_sid,
            'second_session_id':second_sid,'same_profile_id':stage['profile_id'],
            'same_granted_profile':True,
            'checks':['separate_mcp_stdio_processes','fresh_session_on_reconnect',
                      'persistent_profile_grant','released_client_tab_denied','independent_fixture_state'],
            'first_started_at':first_started,'first_finished_at':first_finished,
            'second_started_at':second_started,'second_finished_at':second_finished}

def lifecycle_checks(evidence, transports):
    _, stage, _ = load_stage(evidence)
    config = load_config(stage['managed_config_path'])
    if not config.grant.persistent or config.grant.wire_generation() != stage['managed_generation']:
        raise AssertionError('managed_persistent_grant_changed')

    def attachment(client, stopped):
        state = ok(client.call('chrome_attachment'))['data']
        if state.get('profile_id') != stage['profile_id'] or state.get('stopped') is not stopped:
            raise AssertionError('managed_attachment_state_changed')
        if not stopped and (state.get('authorized') is not True or state.get('owner_attached') is not True):
            raise AssertionError('managed_grant_not_authorized')
        if stopped and state.get('owner_attached') is not False:
            raise AssertionError('managed_stop_not_latched')
        return state

    # Hold a real modal during an actual injected click: timeout cannot replay it.
    states = {}
    denied_operations = {}
    with MCPClient(evidence,'transport_a',transcript=False) as client:
        states['before_stop'] = attachment(client, False)
        tab = ok(client.call('chrome_open', {'url':stage['fixture_base'] + '/app'}))['tab_id']
        read = ok(client.call('chrome_read', {'tab_id':tab}))
        request_id = 'timeout-' + client.stage['instance_id']
        arguments = {'tab_id':tab,'ref':ref(read,'Delay once'),'request_id':request_id}
        result = client.call('chrome_click', arguments)
        if result.get('status') != 'unknown': raise AssertionError('real_inflight_timeout_expected_unknown')
        repeated = client.call('chrome_click', arguments)
        if repeated.get('status') != 'unknown': raise AssertionError('unknown_retry_replayed')
        control(evidence,'release-dialogs')
        time.sleep(0.5)
        attachment(client, False)
        read = ok(client.call('chrome_read', {'tab_id':tab}))
        if 'Delay count: 1' not in read['data']['excerpt']: raise AssertionError('timeout_effect_was_replayed')
        stopped = control(evidence,'owner-gesture')
        if stopped.get('reason')!='owner_stop': raise AssertionError('owner_stop_failed')
        states['after_owner_stop'] = attachment(client, True)
        denied_operations['read_after_stop'] = denied(client.call('chrome_read', {'tab_id':tab})).get('status') == 'denied'
        stop_session = client.sid

    # A fresh MCP process gets a new client SID; registration cannot clear STOP.
    with MCPClient(evidence,'transport_b',transcript=False) as reconnect:
        if reconnect.sid == stop_session: raise AssertionError('managed_reconnect_session_reused')
        states['after_reconnect'] = attachment(reconnect, True)
        denied_operations['open_after_reconnect'] = denied(reconnect.call('chrome_open', {'url':stage['fixture_base'] + '/app'})).get('status') == 'denied'

    worker_result = control(evidence,'worker-restart')
    if worker_result.get('worker_restarted') is not True or worker_result.get('extension_id') != config.grant.extension_id:
        raise AssertionError('managed_worker_restart_failed')
    with MCPClient(evidence,'transport_a',transcript=False) as after_worker:
        states['after_worker_restart'] = attachment(after_worker, True)
        denied_operations['open_after_worker_restart'] = denied(after_worker.call('chrome_open', {'url':stage['fixture_base'] + '/app'})).get('status') == 'denied'

    control(evidence,'restart')
    with MCPClient(evidence,'transport_b',transcript=False) as after_browser:
        states['after_browser_restart'] = attachment(after_browser, True)
        denied_operations['open_after_browser_restart'] = denied(after_browser.call('chrome_open', {'url':stage['fixture_base'] + '/app'})).get('status') == 'denied'

    attached = control(evidence,'owner-gesture')
    if attached.get('reason')!='owner_attach': raise AssertionError('explicit_reattach_failed')
    with MCPClient(evidence,'transport_a',transcript=False) as restored:
        states['after_explicit_owner_attach'] = attachment(restored, False)
        reopened = ok(restored.call('chrome_open', {'url':stage['fixture_base'] + '/app'}))
        ok(restored.call('chrome_read', {'tab_id':reopened['tab_id']}))
    after = load_config(stage['managed_config_path'])
    if not after.grant.persistent or after.grant.wire_generation() != config.grant.wire_generation():
        raise AssertionError('managed_grant_not_preserved_across_restarts')
    checks = ['real_inflight_timeout_unknown','no_mutation_replay','late_result_cannot_reconcile_mutation',
              'owner_stop','registration_did_not_clear_stop','worker_restart_preserved_grant_and_stop',
              'browser_restart_preserved_grant_and_stop','only_explicit_owner_attach_cleared_stop',
              'fresh_mcp_reconnect_preserved_persistent_grant']
    def public_state(state):
        return {'authorized':state.get('authorized') is True,
                'owner_attached':state.get('owner_attached') is True,
                'stopped':state.get('stopped') is True,
                'granted_profile':state.get('profile_id') == stage['profile_id']}
    after = load_config(stage['managed_config_path'])
    return {'checks':checks,
            'grant':{'persistent_before':config.grant.persistent,
                     'persistent_after':after.grant.persistent,
                     'generation_unchanged':after.grant.wire_generation() == config.grant.wire_generation()},
            'stop_states':{name:public_state(value) for name,value in states.items()},
            'denied_after_stop':denied_operations}

def log_canaries(evidence):
    _, stage, _ = load_stage(evidence)
    config = load_config(stage['managed_config_path'])
    control_token = json.loads((Path(stage['runtime_root']) / 'control.json').read_text(encoding='utf-8'))['token']
    forbidden = CANARIES + (config.grant.extension_token, config.controller_key, control_token)
    paths = sorted(evidence.rglob('*'))
    secret_shape = re.compile(
        rb'crm~[a-f0-9]{64}|bearer\s+[A-Za-z0-9._~+/-]{24,}|'
        rb'(?:set-cookie|cookie)["\']?\s*[:=]\s*["\']?[^,\r\n;"\']{1,512}|'
        rb'relay_fixture\s*=\s*[^;\s"\\]+', re.I)
    scanned = 0
    for path in paths:
        if path.is_symlink():
            raise AssertionError('public_evidence_symlink_unscannable')
        if not path.is_file():
            continue
        if path.stat().st_size > 8 * 1024 * 1024:
            raise AssertionError('public_evidence_file_scan_bound')
        content = path.read_bytes()
        lowered = content.lower()
        if any(word.encode('utf-8').lower() in lowered for word in forbidden) or secret_shape.search(content):
            raise AssertionError('public_evidence_secret_or_cookie_leak')
        scanned += 1
    runtime_root = Path(stage['runtime_root']).resolve()
    private_outside = not runtime_root.is_relative_to(evidence.resolve())
    if not private_outside:
        raise AssertionError('private_runtime_inside_public_evidence')
    return {'status':'VERIFIED','files_scanned':scanned,'scan':'recursive_all_file_types',
            'secret_or_cookie_canaries_found':False,'private_runtime_outside_evidence':private_outside}

def verify_uat_contract(sequential, transports, lifecycle, evidence_scan):
    lifecycle_checks = set(lifecycle.get('checks', []))
    client_checks = set(('oauth_origin_denied','foreign_origin_denied','wrong_profile_denied',
                         'profile_override_absent_from_tools',
                         'manual_tab_denied','live_peer_tab_denied','live_peer_ref_denied'))
    required_lifecycle = set(('registration_did_not_clear_stop','worker_restart_preserved_grant_and_stop',
                              'browser_restart_preserved_grant_and_stop',
                              'only_explicit_owner_attach_cleared_stop',
                              'fresh_mcp_reconnect_preserved_persistent_grant'))
    actors = tuple(transports[name] for name in ('transport_a','transport_b'))
    checks = {
        'managed_sequential_separate_clients': sequential.get('status') == 'VERIFIED' and
            sequential.get('first_session_id') != sequential.get('second_session_id') and
            sequential.get('same_granted_profile') is True,
        'managed_overlapping_separate_clients': all(actor.get('status') == 'VERIFIED' and
            actor.get('overlap_verified') is True for actor in actors) and
            actors[0].get('assigned_session_id') != actors[1].get('assigned_session_id') and
            actors[0].get('profile_id') == actors[1].get('profile_id'),
        'persistent_grant_after_reconnect_worker_and_browser_restart': required_lifecycle.issubset(lifecycle_checks),
        'stop_cleared_only_by_explicit_owner_attach': 'only_explicit_owner_attach_cleared_stop' in lifecycle_checks and
            'registration_did_not_clear_stop' in lifecycle_checks,
        'wrong_profile_origin_and_tab_denials': all(client_checks.issubset(set(actor.get('checks', [])))
            for actor in actors),
        'no_secret_or_cookie_content_in_evidence': evidence_scan.get('status') == 'VERIFIED' and
            evidence_scan.get('secret_or_cookie_canaries_found') is False and
            evidence_scan.get('scan') == 'recursive_all_file_types' and
            evidence_scan.get('private_runtime_outside_evidence') is True,
    }
    states = lifecycle.get('stop_states', {})
    for name in ('after_owner_stop','after_reconnect','after_worker_restart','after_browser_restart'):
        state = states.get(name, {})
        checks['stop_stayed_latched_' + name] = state.get('stopped') is True and state.get('owner_attached') is False
    checks['explicit_owner_attach_restored_access'] = (
        states.get('after_explicit_owner_attach', {}).get('stopped') is False and
        states.get('after_explicit_owner_attach', {}).get('owner_attached') is True)
    checks['denials_persisted_after_stop'] = all(lifecycle.get('denied_after_stop', {}).values()) and len(lifecycle.get('denied_after_stop', {})) == 4
    checks['persistent_grant_generation_unchanged'] = (
        lifecycle.get('grant', {}).get('persistent_before') is True and
        lifecycle.get('grant', {}).get('persistent_after') is True and
        lifecycle.get('grant', {}).get('generation_unchanged') is True)
    if not all(checks.values()):
        raise AssertionError('synthetic_uat_contract_incomplete')
    return {'status':'VERIFIED','checks':checks,
            'actor_identity':'deterministic_managed_MCP_clients_only',
            'native_actor_identity':'not_claimed'}

def verify_agents(args):
    _,stage,_ = load_stage(args.evidence)
    records = [json.loads((args.evidence / f'agent-{agent}.json').read_text(encoding='utf-8')) for agent in ('a','b')]
    if stage['source_hashes']!=source_hashes(): raise AssertionError('source_changed_since_staging')
    for actor,record in zip(('a','b'),records):
        if record['status']!='VERIFIED' or record['instance_id']!=stage['instance_id']: raise AssertionError('actor_not_same_instance')
        if not re.fullmatch(r'c-[a-f0-9]{32}',record['assigned_session_id']): raise AssertionError('actor_session_missing')
        if record.get('synthetic_actor_kind')!='managed_fixture_client': raise AssertionError('actor_not_managed_fixture')
        if record['source_hashes']!=stage['source_hashes']: raise AssertionError('actor_source_mismatch')
        steps = [json.loads(line) for line in (args.evidence / f'agent-{actor}.steps.jsonl').read_text(encoding='utf-8').splitlines()]
        steps = [step for step in steps if step.get('execution_id')==record['execution_id']]
        tools = {step['tool'] for step in steps}
        if not set(ACTIONS).issubset(tools): raise AssertionError('required_real_mcp_tool_missing')
        if not any(step['result'].get('status')=='denied' for step in steps): raise AssertionError('actual_denial_missing')
    if records[0]['owned_tab_id']==records[1]['owned_tab_id']: raise AssertionError('actors_share_owned_tab')
    if records[0]['assigned_session_id']==records[1]['assigned_session_id']: raise AssertionError('actors_share_session')
    if records[1]['foreign_tab_id']!=records[0]['owned_tab_id']: raise AssertionError('actor_b_did_not_test_actual_actor_a')
    record = {'status':'VERIFIED_TRANSPORT_ONLY','instance_id':stage['instance_id'],
              'source_hashes':stage['source_hashes'],'actors':[{'agent':r['agent'],'assigned_session_id':r['assigned_session_id'],
              'owned_tab_id':r['owned_tab_id']} for r in records],
              'native_identity':'parent_receipt_required','model_identity':'not_claimed_by_fixture',
              'log_canaries':log_canaries(args.evidence),'completed_at':time.time()}
    dump(args.evidence / 'two-agents.json',record)
    return record

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('offline-checks','synthetic','start-staging','stop-staging','verify-agents','spawn-staging','serve-staging'):
        modes.add_argument('--' + name, action='store_true')
    parser.add_argument('--evidence',required=True,type=Path)
    parser.add_argument('--python-exe',default=PYTHON); parser.add_argument('--node-exe',default=NODE)
    parser.add_argument('--powershell-exe',default=POWERSHELL)
    parser.add_argument('--playwright-root',default=PLAYWRIGHT); parser.add_argument('--browser-exe',default=BROWSER)
    parser.add_argument('--browser-sha256',default=BROWSER_SHA256)
    parser.add_argument('--background-helper',default=os.environ.get('CHROME_RELAY_BACKGROUND_HELPER',TASK_HELPER))
    args = parser.parse_args()
    if not (args.offline_checks or args.stop_staging or args.verify_agents):
        for name in ('python_exe', 'node_exe', 'browser_exe', 'playwright_root'):
            value = getattr(args, name)
            if not value or not Path(value).is_absolute() or not Path(value).exists():
                parser.error('--' + name.replace('_', '-') + ' must name an absolute existing path')
        if not Path(args.playwright_root).is_dir() or not all(
                Path(getattr(args, name)).is_file() for name in ('python_exe', 'node_exe', 'browser_exe')):
            parser.error('browser runtime file/directory bindings have invalid types')
        if os.name == 'nt' and (args.start_staging or args.synthetic):
            for name in ('powershell_exe', 'background_helper'):
                value = getattr(args, name)
                if not value or not Path(value).is_absolute() or not Path(value).is_file():
                    parser.error('--' + name.replace('_', '-') + ' must name an absolute existing file')
    if not args.evidence.is_absolute(): raise ValueError('evidence_must_be_absolute')
    args.evidence=args.evidence.resolve()
    # Full suites own a fresh evidence/profile instance; never replay lifecycle
    # mutations into an earlier live staging or overwrite retained test logs.
    if (args.synthetic or args.offline_checks) and args.evidence.exists() and any(args.evidence.iterdir()):
        raise ValueError('fresh_evidence_directory_required')
    if args.serve_staging: return serve_staging(args)
    if args.spawn_staging: return spawn_staging(args)
    if args.offline_checks: result=offline_checks(args)
    elif args.start_staging: result=start_staging(args)
    elif args.stop_staging:
        _,stage,_=load_stage(args.evidence)
        if process_start(stage['staging_pid'])!=stage['staging_process_start']: raise RuntimeError('exact_staging_pid_identity_mismatch')
        result=control(args.evidence,'stop-staging')
        deadline=time.monotonic()+15
        while process_start(stage['staging_pid'])==stage['staging_process_start'] and time.monotonic()<deadline:
            time.sleep(0.1)
        if process_start(stage['staging_pid'])==stage['staging_process_start']:
            raise RuntimeError('exact_staging_process_did_not_stop')
        result={'status':'STOPPED','instance_id':stage['instance_id'],'staging_pid':stage['staging_pid'],'files_deleted':0}
    elif args.verify_agents: result=verify_agents(args)
    else:
        stage=start_staging(args)
        sequential=managed_sequential(args.evidence)
        transports=concurrent_transport(args.evidence)
        lifecycle=lifecycle_checks(args.evidence,transports)
        snapshot=control(args.evidence,'snapshot')
        manual = [tab for tab in snapshot['tabs'] if tab['manual']]
        if len(manual) != 1 or not manual[0]['active'] or any(tab['active'] for tab in snapshot['tabs'] if not tab['manual']):
            raise AssertionError('synthetic_manual_active_tab_changed')
        if any(page['increments'] != 'Increment count: 0' or page['sends'] != 'Send count: 0' for page in snapshot['pages'] if page['manual']):
            raise AssertionError('manual_fixture_was_mutated')
        screenshot=args.evidence / 'fixture.png'
        if not snapshot.get('fixture_screenshot_written') or not screenshot.is_file():
            raise RuntimeError('real_browser_screenshot_missing')
        if source_hashes()!=stage['source_hashes']:
            raise RuntimeError('source_changed_during_synthetic_checks')
        evidence_scan = log_canaries(args.evidence)
        contract = verify_uat_contract(sequential,transports,lifecycle,evidence_scan)
        result={'status':'VERIFIED','instance_id':stage['instance_id'],'source_hashes':source_hashes(),
                'offline_checks':'separate_command_required','browser_sha256':stage['browser_sha256'],'desktop':stage['desktop'],
                'managed_grant':{'persistent':stage['grant_persistent'],'generation':stage['managed_generation']},
                'sequential_managed_clients':sequential,'overlapping_transport_clients':transports,
                'lifecycle_checks':lifecycle['checks'],'lifecycle_evidence':lifecycle,
                'passive_browser_snapshot':snapshot,
                'fixture_screenshot_sha256':hashlib.sha256(screenshot.read_bytes()).hexdigest(),
                'uat_contract':contract,
                'focus_check':{'expected':'manual_active_own_tabs_inactive','actual':'manual_active_own_tabs_inactive',
                               'physical_owner_toolbar_focus':'UNVERIFIED'},
                'log_canaries':evidence_scan,'model_identity':'no_models_run_by_this_verifier',
                'actor_identity':'deterministic_managed_MCP_clients_only','staging_retained':True,'completed_at':time.time()}
        dump(args.evidence / 'synthetic.json',result)
        result['log_canaries']=log_canaries(args.evidence)
        result['uat_contract']=verify_uat_contract(sequential,transports,lifecycle,result['log_canaries'])
        dump(args.evidence / 'synthetic.json',result)
    print(json.dumps(result,ensure_ascii=True))
    return 0

if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as exc:
        failure = {'status':'FAILED','error_type':type(exc).__name__}
        if isinstance(exc, UATFailure): failure['failure_code'] = exc.code
        print(json.dumps(failure),file=sys.stderr)
        raise SystemExit(1)
