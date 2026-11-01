"""Pinned local runtimes. No shell, downloads, global cleanup or API fallback."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

from snapshot import (BoundaryError, absolute_path, check_path, hash_checked,
                      make_directory, path_key, read_checked, StateLease,
                      state_usage, reusable_container, complete_container)


class NativeError(BoundaryError):
    pass


def minimal_environment(home: Path, executable: Path | None = None) -> dict:
    """Allowlist OS necessities, never inherit provider auth, proxies or traces."""
    make_directory(home)
    env = {
        'HOME': str(home), 'USERPROFILE': str(home), 'APPDATA': str(home),
        'LOCALAPPDATA': str(home), 'XDG_CONFIG_HOME': str(home),
        'XDG_CACHE_HOME': str(home), 'TMP': str(home), 'TEMP': str(home),
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
        'HF_HUB_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1',
        'OTEL_SDK_DISABLED': 'true', 'NO_COLOR': '1',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_SYSTEM': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
        'GIT_OPTIONAL_LOCKS': '0', 'GIT_ATTR_NOSYSTEM': '1',
        'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1',
        # ureq/HF clients honor proxy environment; loopback inference bypasses
        # this closed proxy. Offline flags alone do not disable ORT bootstrap.
        'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1',
        'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': '127.0.0.1,localhost',
    }
    if os.name == 'nt':
        system = os.environ.get('SystemRoot', r'C:\Windows')
        env['SystemRoot'] = system
        env['WINDIR'] = system
        env['PATH'] = os.pathsep.join([str(executable.parent)] if executable else [])
        env['PATH'] += os.pathsep + str(Path(system) / 'System32')
    else:
        env['PATH'] = '/usr/bin:/bin'
        env['LANG'] = 'C.UTF-8'
    return env


class _WindowsJob:
    """A suspended process enters our kill-on-close job before native code runs."""
    _desktop = None
    _lock = threading.Lock()

    def __init__(self):
        from ctypes import wintypes as w
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int,
                                                        ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.SetInformationJobObject.restype = w.BOOL
        self.kernel.AssignProcessToJobObject.restype = w.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise NativeError('private_job_failed')
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION: BasicLimitInformation then IO,
        # four SIZE_T fields. The LimitFlags offset is 16 on Win32 and Win64.
        size = 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112
        info = ctypes.create_string_buffer(size)
        ctypes.c_uint32.from_buffer(info, 16).value = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, info, size):
            self.close()
            raise NativeError('private_job_failed')
        with self._lock:
            if self._desktop is None:
                user = ctypes.WinDLL('user32', use_last_error=True)
                user.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p,
                                                w.DWORD, w.DWORD, ctypes.c_void_p]
                user.CreateDesktopW.restype = w.HANDLE
                # Never switch or activate this private desktop.
                type(self)._desktop = user.CreateDesktopW(
                    'AgentStackBackground', None, None, 0, 0x10000000, None)
                if not self._desktop:
                    self.close()
                    raise NativeError('private_desktop_failed')

    def attach(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            process.kill()
            raise NativeError('private_job_attach_failed')
        resume = ctypes.WinDLL('ntdll').NtResumeProcess
        resume.argtypes = [ctypes.c_void_p]
        resume.restype = ctypes.c_long
        if resume(int(process._handle)) != 0:
            process.kill()
            raise NativeError('private_process_resume_failed')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class HiddenProcess:
    def __init__(self, argv: list[str], cwd: Path, env: dict, capture: bool = True):
        self.job = _WindowsJob() if os.name == 'nt' else None
        options = {'shell': False, 'stdin': subprocess.DEVNULL,
                   'stdout': subprocess.PIPE if capture else subprocess.DEVNULL,
                   'stderr': subprocess.PIPE if capture else subprocess.DEVNULL,
                   'cwd': str(cwd), 'env': env, 'close_fds': True}
        if os.name == 'nt':
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            startup.lpDesktop = 'AgentStackBackground'
            options.update(creationflags=subprocess.CREATE_NO_WINDOW | 0x4,
                           startupinfo=startup)
        else:
            options['start_new_session'] = True
        self.process = None
        try:
            self.process = subprocess.Popen(argv, **options)
            if self.job:
                self.job.attach(self.process)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.job:
            self.job.close()
        elif self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process is not None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            for stream in (self.process.stdout, self.process.stderr):
                if stream:
                    stream.close()


def run_native(argv: list[str], cwd: Path, env: dict, timeout: float,
               max_output: int = 8 * 1024 * 1024, accepted_codes=(0,),
               state_budget: tuple[Path, int] | None = None) -> bytes:
    """Bound both pipes in memory and kill only our process tree on failure."""
    check_path(cwd, directory=True)
    child = HiddenProcess(argv, cwd, env)
    output, errors = bytearray(), bytearray()
    overflow = threading.Event()
    read_failed = threading.Event()

    def drain(stream, buffer):
        try:
            while block := stream.read(65536):
                if len(buffer) + len(block) > max_output:
                    overflow.set()
                    return
                buffer.extend(block)
        except (OSError, ValueError):
            read_failed.set()

    readers = [threading.Thread(target=drain, args=(child.process.stdout, output), daemon=True),
               threading.Thread(target=drain, args=(child.process.stderr, errors), daemon=True)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    next_size_check = 0.0
    try:
        while child.process.poll() is None or any(r.is_alive() for r in readers):
            if state_budget and time.monotonic() >= next_size_check:
                state_usage(*state_budget)
                next_size_check = time.monotonic() + 1
            if overflow.is_set():
                raise NativeError('native_output_limit')
            if read_failed.is_set():
                raise NativeError('native_pipe_failed')
            if time.monotonic() >= deadline:
                raise NativeError('native_timeout')
            time.sleep(0.01)
        if overflow.is_set():
            raise NativeError('native_output_limit')
        if child.process.returncode not in accepted_codes:
            # Native stderr may echo query/source/auth. Never propagate it.
            raise NativeError('native_failed')
        if state_budget:
            state_usage(*state_budget)
        return bytes(output)
    finally:
        child.close()
        for reader in readers:
            reader.join(timeout=1)


def verify_assets(config: dict, mode: str = 'agent') -> str:
    if mode not in ('semantic', 'agent'):
        raise NativeError('invalid_asset_mode')
    manifest_bytes = read_checked(absolute_path(config['asset_manifest']), 4 * 1024 * 1024)
    try:
        manifest = json.loads(manifest_bytes)
        if manifest.get('version') != 1 or not isinstance(manifest['assets'], list):
            raise ValueError
        expected_runtime = {'colgrep': 'v1.8.5',
                            'source': 'c6c091d7ee14c81206bb28af90d8ff506fd0235e',
                            'llama_cpp': 'b11476'}
        if any(manifest['runtime'].get(k) != v for k, v in expected_runtime.items()):
            raise NativeError('runtime_revision_mismatch')
        archive_pins = {
            'colgrep_archive_sha256': '4f8f240ccbf17740e70d36b798904cf592622719bf11e9c1c86dc01cf9f2c42d',
            'llama_cpp_archive_sha256': '5c71e7b749697da4a8d46e9ee55486845cbba27c9dfbecb4007f31ba6610d523',
        }
        if any(manifest['runtime'].get(k) != v for k, v in archive_pins.items()):
            raise NativeError('runtime_archive_pin_mismatch')
        onnx_metadata = manifest.get('onnx_runtime', {})
        if any(onnx_metadata.get(k) != v for k, v in {
                'version': '1.23.0', 'publisher': 'microsoft/onnxruntime', 'flavor': 'CPU',
        }.items()):
            raise NativeError('onnx_runtime_pin_mismatch')
        # ORT's archive digest is owner-supplied manifest data. The brief pins
        # ColGrep/llama archives, but supplies no independent ORT archive digest.
        if not re.fullmatch('[0-9a-f]{64}', onnx_metadata['archive_sha256']):
            raise NativeError('invalid_onnx_archive_digest')
        for role, repo, revision in [
            ('encoder', 'lightonai/LateOn-Code-edge', '4bcdf5ed93f791259eb130b577a240f753d68dd8'),
            ('model', 'lightonai/colgrep-agent-2B-GGUF', 'c2e69958d03af9188af9ebf89582f8887f0bf534')]:
            metadata = manifest[role]
            if metadata['repo'] != repo or metadata['revision'] != revision or (
                    metadata['license'] != 'Apache-2.0'):
                raise NativeError('model_revision_or_license_mismatch')
        by_path = {}
        for item in manifest['assets']:
            path = absolute_path(item['path'])
            key = path_key(path)
            if key in by_path or not re.fullmatch('[0-9a-f]{64}', item['sha256']):
                raise ValueError
            if type(item['size']) is not int or item['size'] < 0:
                raise ValueError
            by_path[key] = item
        required = [absolute_path(config['colgrep'])]
        if mode == 'agent':
            required += [absolute_path(config[k]) for k in ('llama_server', 'agent_model')]
        if not config.get('onnx_runtime'):
            raise NativeError('onnx_runtime_not_configured')
        ort = absolute_path(config['onnx_runtime'])
        required.append(ort)
        encoder = absolute_path(config['encoder_dir'])
        check_path(encoder, directory=True)
        required += [encoder / name for name in (
            'tokenizer.json', 'config_sentence_transformers.json', 'onnx_config.json')]
        if path_key(encoder / 'model_int8.onnx') not in by_path:
            required.append(encoder / 'model.onnx')
        else:
            required.append(encoder / 'model_int8.onnx')
        if any(path_key(path) not in by_path for path in required):
            raise NativeError('asset_not_manifested')
        # All runtime siblings (DLLs included) must be covered; no DLL planting.
        native_dirs = {absolute_path(config['colgrep']).parent, ort.parent}
        if mode == 'agent':
            native_dirs.add(absolute_path(config['llama_server']).parent)
        for directory in native_dirs:
            check_path(directory, directory=True)
            for path in directory.iterdir():
                if path.suffix.lower() in {'.exe', '.dll', '.so', '.dylib'} and (
                        path_key(path) not in by_path):
                    raise NativeError('unmanifested_native_library')
        for path in encoder.iterdir():
            if path_key(path) not in by_path:
                raise NativeError('unmanifested_encoder_file')
        # The large model and public release archives have independently pinned hashes.
        model = by_path[path_key(absolute_path(config['agent_model']))]
        if model['sha256'] != 'f238da91e6e95b1de1aaf7fefdacd6db8eb47edb60ee038d3a6a6642e9a97ccf' or (
                model['size'] != 2679710912):
            raise NativeError('agent_model_pin_mismatch')
        required_keys = {path_key(path) for path in required}
        for key, item in by_path.items():
            path = absolute_path(item['path'])
            if (key in required_keys or path.is_relative_to(encoder) or
                    path.parent in native_dirs and path.suffix.lower() in {'.exe', '.dll', '.so', '.dylib'}):
                if hash_checked(path, item['size']) != item['sha256']:
                    raise NativeError('asset_hash_mismatch')
        import hashlib
        return hashlib.sha256(manifest_bytes).hexdigest()
    except (KeyError, AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, BoundaryError):
            raise
        raise NativeError('invalid_asset_manifest') from None


def select_device(output: bytes, name: str, minimum_mib: int = 4096) -> str:
    """Parse llama.cpp --list-devices, exact description and explicit free VRAM."""
    text = output.decode('utf-8', errors='strict')
    matches = []
    for line in text.splitlines():
        match = re.fullmatch(r'\s*(Vulkan\d+):\s*(.*?)\s*\((\d+) MiB,\s*(\d+) MiB free\)\s*', line)
        if match and match[2] == name:
            matches.append(match)
    if len(matches) != 1:
        raise NativeError('device_absent_or_ambiguous')
    if int(matches[0][4]) < minimum_mib:
        raise NativeError('device_memory_insufficient')
    return matches[0][1]


class NativeRuntime:
    def __init__(self, config: dict):
        self.config = config
        self.server = None
        self.api_key = None
        self.endpoint = None
        self.device = None
        self.last_metadata = {}
        self._server_lease = None

    def close(self):
        try:
            if self.server is not None:
                self.server.close()
        finally:
            self.server = self.api_key = self.endpoint = self.device = None
            if self._server_lease is not None:
                self._server_lease.close()
                self._server_lease = None

    def start_agent(self, native_dir: Path) -> None:
        if self.server is not None and self.server.process.poll() is None:
            return
        self.close()
        # Retain a separate lease for the server's lifetime. A second service
        # sharing state may use CPU search, but cannot start another GPU child.
        self._server_lease = StateLease(absolute_path(self.config['state_dir']) / 'agent-server')
        try:
            verify_assets(self.config, mode='agent')
            self._start_agent(native_dir)
        except BaseException:
            self.close()
            raise

    def _start_agent(self, native_dir: Path) -> None:
        executable = absolute_path(self.config['llama_server'])
        env = minimal_environment(native_dir / 'home', executable)
        devices = run_native([str(executable), '--list-devices'], native_dir, env,
                             self.config['timeout_seconds'], max_output=65536)
        self.device = select_device(devices, self.config['device_name'])
        self.api_key = ( secrets.token_urlsafe(32) )
        # Only the child environment receives authentication, never argv or disk.
        env['LLAMA_API_KEY'] = self.api_key
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        self.endpoint = f'http://127.0.0.1:{port}/v1'
        argv = [str(executable), '--model', self.config['agent_model'],
                '--device', self.device, '--main-gpu', '0', '--split-mode', 'none',
                '--n-gpu-layers', '999', '--ctx-size', '16384', '--parallel', '1',
                '--host', '127.0.0.1', '--port', str(port), '--no-webui',
                '--special', '--fit', 'off', '--log-disable',
                '--cors-origins', 'http://127.0.0.1', '--cors-methods', 'GET,POST',
                '--cors-headers', 'Authorization,Content-Type', '--no-cors-credentials',
                '--no-agent', '--no-ui-mcp-proxy']
        self.server = HiddenProcess(argv, native_dir, env, capture=False)
        # Bypass OS proxy settings and redirects. Poll only our known loopback child.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        deadline = time.monotonic() + self.config['timeout_seconds']
        try:
            while time.monotonic() < deadline:
                if self.server.process.poll() is not None:
                    raise NativeError('agent_server_failed')
                request = urllib.request.Request(
                    self.endpoint + '/models', headers={'Authorization': 'Bearer ' + self.api_key})
                try:
                    with opener.open(request, timeout=1) as response:
                        if response.status == 200:
                            return
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(0.05)
            raise NativeError('agent_server_timeout')
        except BaseException:
            self.close()
            raise

    def search(self, snapshot, query: str, mode: str, k: int) -> list:
        self.last_metadata = {}
        # Reuse the content-scoped index only after a successful invocation.
        # Partial state is retained; retries use a fresh sibling without delete.
        native_dir, fresh = reusable_container(snapshot.root.parent, 'native', snapshot.fingerprint, 8)
        make_directory(native_dir / 'indices')
        ort_path = absolute_path(self.config['onnx_runtime'])
        _validate_onnx_runtime(ort_path)
        if mode == 'agent':
            self.start_agent(native_dir)
        native_config = {'default_model': self.config['encoder_dir'],
                         'parallel_sessions': 1, 'batch_size': 1,
                         'agent': {'model': self.config['agent_model'],
                                   'endpoint': self.endpoint if mode == 'agent' else None,
                                   'search_k': k, 'context_size': 16384}}
        config_bytes = json.dumps(native_config, sort_keys=True).encode('utf-8')
        # Config is private native state, never part of the source tree.
        config_path = native_dir / 'config.json'
        if config_path.exists():
            check_path(config_path)
        with config_path.open('wb') as stream:
            stream.write(config_bytes)
        executable = absolute_path(self.config['colgrep'])
        env = minimal_environment(native_dir / 'home', executable)
        index_path = str(native_dir / 'indices')
        # SQLite's native Windows API otherwise fails at MAX_PATH in deep
        # task/worktree state. Only extend this already validated, owned path;
        # user-supplied device/UNC paths remain forbidden by absolute_path.
        env['COLGREP_DATA_DIR'] = ('\\\\?\\' + index_path) if os.name == 'nt' else index_path
        env['ORT_DYLIB_PATH'] = str(ort_path)
        env['COLGREP_FORCE_CPU'] = '1'
        if mode == 'agent':
            env['COLGREP_AGENT_API_KEY'] = self.api_key
        argv = [str(executable), '--json', '--yes', '--model', self.config['encoder_dir'],
                '--force-cpu']
        if not fresh:
            # Pinned upstream forbids rebuild/clear recovery in this mode.
            argv.append('--no-update')
        if mode == 'agent':
            argv.append('--agent')
        else:
            argv.extend(['--semantic-only', '-k', str(k)])
        argv.extend(['--', query, str(snapshot.root)])
        snapshot.verify()
        try:
            with snapshot.frozen():
                data = run_native(argv, snapshot.root, env, self.config['timeout_seconds'],
                                  state_budget=(absolute_path(self.config['state_dir']),
                                                self.config['max_state_bytes']))
            result = json.loads(data)
            if mode == 'agent':
                if not isinstance(result, dict) or not isinstance(result.get('locations'), list):
                    raise NativeError('invalid_native_result')
                if result.get('error') or result.get('end_reason') == 'LlmError':
                    raise NativeError('agent_model_failed')
                if (type(result.get('submitted')) is not bool or
                        not isinstance(result.get('end_reason'), str) or result.get('end_reason') not in {
                        'Answered', 'AnsweredEmpty', 'MalformedRetries', 'MaxTurns', 'ContextOverflow'}):
                    raise NativeError('invalid_native_result')
                if result['submitted'] and not result['locations']:
                    raise NativeError('invalid_native_result')
                self.last_metadata = {'submitted': result['submitted'],
                                      'end_reason': result['end_reason'], 'device': self.device}
                for key in ('turns', 'searches', 'reads', 'prompt_tokens', 'cached_prompt_tokens',
                            'completion_tokens', 'llm_seconds', 'tool_seconds', 'seconds'):
                    value = result.get(key)
                    if type(value) not in (int, float) or not 0 <= value < 1e12:
                        raise NativeError('invalid_native_usage')
                    self.last_metadata[key] = value
                result = result['locations']
            if not isinstance(result, list):
                raise NativeError('invalid_native_result')
            state_usage(absolute_path(self.config['state_dir']), self.config['max_state_bytes'],
                        additional=64 if fresh else 0)
            if fresh:
                complete_container(native_dir, snapshot.fingerprint)
            return result
        except (json.JSONDecodeError, UnicodeError):
            raise NativeError('invalid_native_result') from None
        finally:
            snapshot.verify()


def _validate_onnx_runtime(path: Path) -> None:
    """Match upstream's loadability gate before it can enter its downloader."""
    check_path(path)
    try:
        library = ctypes.CDLL(str(path))
        getattr(library, 'OrtGetApiBase')
    except (OSError, AttributeError):
        raise NativeError('onnx_runtime_not_loadable') from None
