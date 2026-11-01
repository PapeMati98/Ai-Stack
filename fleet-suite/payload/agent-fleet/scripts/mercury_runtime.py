"""Opt-in Mercury admission/permissions are not an OS sandbox. Ledger != account credit."""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

CRED_TARGET = 'AIStack/Mercury3/Pilot'
CHILD_ENV_VAR = 'INCEPTION_API_KEY'
ALLOWED_PROVIDER = 'inception'
ALLOWED_MODEL = 'mercury-3'
ALLOWED_RUNTIME_MODEL = 'inception/mercury-3'
ALLOWED_BASE_URL = 'https://api.inceptionlabs.ai/v1'
ALLOWED_POOL_ID = 'inception-mercury-free'
WORKSPACE_ROOT = os.path.expanduser('~/.agents/task-work/mercury-pilot-20261020/synthetic')
STATE_ROOT = Path.home() / '.agents' / 'mercury3-pilot'
NATIVE_EXE = os.path.expanduser('~/.local/opencode-cli-1.18.21/node_modules/opencode-ai/bin/opencode.exe')
NATIVE_SHA256 = 'ea4f4d4bec95cd41baf0fc53adc4e34b31e1c8676dc5b2507c8797ab1884af18'
FREE_TOKEN_ALLOWANCE = 100_000_000
EFFECTIVE_INPUT_LIMIT = 32768
EFFECTIVE_OUTPUT_LIMIT = 16384
NATIVE_STEPS = 16
RUN_RESERVATION = NATIVE_STEPS * (EFFECTIVE_INPUT_LIMIT + EFFECTIVE_OUTPUT_LIMIT)
ATTESTATION_MAX_AGE_SECONDS = 24 * 3600
_THREAD_LOCK = threading.Lock()

class MercuryGateError(ValueError):
    """Fail closed without copying native output or secrets into exceptions."""

def _count(value, label, maximum=None):
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise MercuryGateError(f'invalid {label}')
    return value

def _fresh(value, *, now=None):
    current = time.time() if now is None else now
    if (type(value) not in (int, float) or abs(value) > 10**12 or not math.isfinite(value)
            or type(current) not in (int, float) or not math.isfinite(current)
            or not 0 <= current - value <= ATTESTATION_MAX_AGE_SECONDS):
        raise MercuryGateError('state timestamp missing, invalid or expired')

def safe_path(path, *, must_exist=False):
    """Reject symlinks, reparse points and multiply-linked state/files/ancestors."""
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if must_exist:
                raise MercuryGateError('required pilot path missing') from None
            continue
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
            raise MercuryGateError('pilot path is linked')
    return path

def _load(path):
    path = safe_path(path, must_exist=True)
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise MercuryGateError('pilot state too large')
        with path.open(encoding='utf-8') as source:
            data = json.load(source, object_pairs_hook=_unique_object)
    except (OSError, ValueError):
        raise MercuryGateError('pilot state unreadable or malformed') from None
    if not isinstance(data, dict):
        raise MercuryGateError('pilot state must be an object')
    return data

def _unique_object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise MercuryGateError('duplicate pilot state field')
        data[key] = value
    return data

def read_attestation(path, *, now=None):
    data = _load(path)
    if data.get('account_opt_out') is not True or data.get('billing_free_only') is not True:
        raise MercuryGateError('pilot requires opt-out and free-only billing attestation')
    _count(data.get('remaining_free_tokens'), 'remaining snapshot', FREE_TOKEN_ALLOWANCE)
    _fresh(data.get('checked_at'), now=now)
    return data

def provider_block(*, effective=False):
    return {
        'npm': '@ai-sdk/openai-compatible',
        'options': {'baseURL': ALLOWED_BASE_URL, 'apiKey': '{env:INCEPTION_API_KEY}'},
        'models': {ALLOWED_MODEL: {
            'name': ALLOWED_MODEL, 'tool_call': True, 'reasoning': True, 'temperature': True,
            'modalities': {'input': ['text'], 'output': ['text']},
            'limit': {'context': EFFECTIVE_INPUT_LIMIT + EFFECTIVE_OUTPUT_LIMIT if effective else 1_000_000,
                      'input': EFFECTIVE_INPUT_LIMIT if effective else 1_000_000,
                      'output': EFFECTIVE_OUTPUT_LIMIT if effective else 131072},
            'options': {'reasoningEffort': 'medium'},
            'variants': {'medium': {'reasoningEffort': 'medium'}},
        }},
    }

def validate_profile(profile):
    expected = {
        'adapter': 'opencode', 'mercury_pilot': True, 'routing_enabled': True,
        'quota_pool_id': ALLOWED_POOL_ID, 'enabled_providers': [ALLOWED_PROVIDER],
        'provider': {ALLOWED_PROVIDER: provider_block()},
        'credential': {'type': 'windows-credential-manager', 'target': CRED_TARGET, 'env_var': CHILD_ENV_VAR},
        'data_boundary': {'synthetic_only': True, 'workspace_root': WORKSPACE_ROOT},
        'candidates': [NATIVE_EXE], 'deep_probe_args': ['models', ALLOWED_PROVIDER],
        'version_args': ['--version'],
        'office_access': 'none', 'paid_fallback': False,
        'supported_modes': ['advisory', 'review', 'implement'],
        'context_stop_tokens': EFFECTIVE_INPUT_LIMIT,
    }
    if 'local_runtime' in profile or any(profile.get(k) != v or (type(v) in (bool, int) and type(profile.get(k)) is not type(v))
           for k, v in expected.items()):
        raise MercuryGateError('Mercury profile/credential/billing/provider pin drift')
    executions = profile.get('execution_profiles')
    if not isinstance(executions, dict) or not executions:
        raise MercuryGateError('Mercury execution inventory missing')
    for execution in executions.values():
        if (not isinstance(execution, dict) or execution.get('model') != ALLOWED_MODEL
                or execution.get('model_provider_id') != ALLOWED_PROVIDER
                or execution.get('runtime_model_id') != ALLOWED_RUNTIME_MODEL
                or execution.get('reasoning_level') != 'medium'):
            raise MercuryGateError('Mercury execution model/reasoning drift')

def qualify_workspace(workspace):
    root = safe_path(WORKSPACE_ROOT, must_exist=True).resolve(strict=True)
    candidate = safe_path(workspace, must_exist=True).resolve(strict=True)
    if not root.is_dir() or not candidate.is_dir() or not candidate.is_relative_to(root):
        raise MercuryGateError('Mercury requires the pinned synthetic workspace')
    def refuse_scan(error):
        raise MercuryGateError('workspace scan unreadable') from None
    for directory, dirs, files in os.walk(candidate, followlinks=False, onerror=refuse_scan):
        for name in dirs + files:
            safe_path(Path(directory) / name, must_exist=True)
    return candidate

def gate_native_run(profile, workspace):
    validate_profile(profile)
    actual = qualify_workspace(workspace)
    attestation = read_attestation(STATE_ROOT / 'attestation.json')
    # Validate existing allowance/lock state before prompt or key access too.
    # Reservations are acquired once later, immediately before child injection.
    safe_path(str(STATE_ROOT / 'allowance.json') + '.lock')
    _read_ledger(STATE_ROOT / 'allowance.json')
    if attestation['remaining_free_tokens'] < RUN_RESERVATION:
        raise MercuryGateError('free snapshot cannot cover a bounded pilot run')
    return actual, attestation

def require_native(executable):
    if str(executable).replace('\\', '/').casefold() != NATIVE_EXE.replace('\\', '/').casefold():
        raise MercuryGateError('Mercury native executable drift; no ambient shim fallback')
    path = safe_path(executable, must_exist=True)
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != NATIVE_SHA256:
        raise MercuryGateError('Mercury native executable hash drift')
    return str(path)

def _read_native_credential(target=CRED_TARGET):
    if target != CRED_TARGET or os.name != 'nt':
        raise MercuryGateError('Mercury native credential target/platform refused')
    import ctypes
    from ctypes import wintypes
    class Credential(ctypes.Structure):
        _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                    ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                    ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                    ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)),
                    ('Persist', wintypes.DWORD), ('AttributeCount', wintypes.DWORD),
                    ('Attributes', ctypes.c_void_p), ('TargetAlias', wintypes.LPWSTR),
                    ('UserName', wintypes.LPWSTR)]
    pointer = ctypes.POINTER(Credential)()
    api = ctypes.WinDLL('advapi32', use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    if not api.CredReadW(CRED_TARGET, 1, 0, ctypes.byref(pointer)):
        raise MercuryGateError('Mercury credential unavailable')
    try:
        record = pointer.contents
        if record.Type != 1 or record.TargetName != CRED_TARGET or not 0 < record.CredentialBlobSize <= 8192:
            raise MercuryGateError('Mercury credential record invalid')
        blob = ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize)
        return _decode_credential(blob)
    finally:
        api.CredFree(pointer)

def _decode_credential(blob):
    # Native generic blobs are bytes: support Unicode Windows password UI and
    # a direct UTF-8 CredWriteW producer, without consulting another store.
    if not isinstance(blob, bytes) or not 0 < len(blob) <= 8192:
        raise MercuryGateError('Mercury credential blob invalid')
    try:
        secret = blob.decode('utf-16-le' if b'\0' in blob else 'utf-8').rstrip('\0')
    except UnicodeError:
        raise MercuryGateError('Mercury credential encoding invalid') from None
    if not re.fullmatch(r'[A-Za-z0-9._-]{8,4096}', secret):
        raise MercuryGateError('Mercury credential value invalid')
    return secret

@dataclass(frozen=True)
class CredentialStatus:
    available: bool
    fingerprint: str | None

def credential_status(profile, workspace):
    gate_native_run(profile, workspace)
    try:
        secret = _read_native_credential()
    except MercuryGateError:
        return CredentialStatus(False, None)
    return CredentialStatus(True, hashlib.sha256(secret.encode()).hexdigest()[:16])

def inject_credential(env, profile, workspace):
    gate_native_run(profile, workspace)
    env[CHILD_ENV_VAR] = _read_native_credential()
    return env

def harden_environment(env):
    allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'LANG'}
    return {k: v for k, v in env.items() if k.upper() in allowed}

def bind_home(env, guarded):
    env = harden_environment(env)
    env.update(guarded)
    home = Path(guarded['XDG_CONFIG_HOME']).parent
    for name, suffix in (('HOME', ''), ('USERPROFILE', ''), ('APPDATA', 'appdata'), ('LOCALAPPDATA', 'localappdata')):
        target = home / suffix
        target.mkdir(exist_ok=True)
        env[name] = str(target)
    env.update(OPENCODE_AUTO_SHARE='false', OPENCODE_DISABLE_AUTOUPDATE='true',
               OPENCODE_DISABLE_TERMINAL_TITLE='true', DO_NOT_TRACK='1')
    return env

@contextmanager
def _ledger_lock(path):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK:
        lock = safe_path(str(path) + '.lock')
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        locked = False
        try:
            if os.name == 'nt':
                import msvcrt
                for _ in range(500):
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        time.sleep(0.01)
                if not locked:
                    raise MercuryGateError('ledger lock contention')
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
                locked = True
            yield
        finally:
            if locked:
                if os.name == 'nt':
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

def _run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,160}', value):
        raise MercuryGateError('invalid ledger run id')

def _read_ledger(path):
    path = safe_path(path)
    if not path.exists():
        return {'schema_version': 1, 'allowance': FREE_TOKEN_ALLOWANCE,
                'reserved': 0, 'spent_known': 0, 'runs': {}, 'updated_at': time.time()}
    data = _load(path)
    if type(data.get('schema_version')) is not int or data['schema_version'] != 1:
        raise MercuryGateError('ledger schema invalid')
    _count(data.get('allowance'), 'allowance', FREE_TOKEN_ALLOWANCE)
    _fresh(data.get('updated_at'))
    if not isinstance(data.get('runs'), dict):
        raise MercuryGateError('ledger runs invalid')
    reserved = spent = 0
    for run_id, run in data['runs'].items():
        _run_id(run_id)
        if not isinstance(run, dict):
            raise MercuryGateError('ledger run invalid')
        bound = _count(run.get('reservation'), 'stored reservation', FREE_TOKEN_ALLOWANCE)
        if not bound or run.get('status') not in {'reserved', 'reconciled'}:
            raise MercuryGateError('ledger reservation invalid')
        if run['status'] == 'reserved':
            if run.get('actual_total') is not None:
                raise MercuryGateError('reserved run has actual usage')
            reserved += bound
        else:
            spent += _count(run.get('actual_total'), 'actual total')
    if (_count(data.get('reserved'), 'reserved') != reserved
            or _count(data.get('spent_known'), 'spent') != spent):
        raise MercuryGateError('ledger aggregates inconsistent')
    return data

def _write_ledger(path, data):
    path = safe_path(path)
    data['updated_at'] = time.time()
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.mercury-tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as sink:
        json.dump(data, sink, allow_nan=False)
        sink.flush()
        os.fsync(sink.fileno())
    safe_path(path)
    os.replace(temporary, path)

def reserve(path, run_id, worst_case_tokens, *, remaining_snapshot=FREE_TOKEN_ALLOWANCE):
    _run_id(run_id)
    bound = _count(worst_case_tokens, 'reservation', FREE_TOKEN_ALLOWANCE)
    snapshot = _count(remaining_snapshot, 'remaining snapshot', FREE_TOKEN_ALLOWANCE)
    if not bound:
        raise MercuryGateError('reservation must be positive')
    with _ledger_lock(path):
        data = _read_ledger(path)
        previous = data['runs'].get(run_id)
        if previous is not None:
            if previous['reservation'] != bound or previous['status'] != 'reserved':
                raise MercuryGateError('reservation replay disagrees with run')
            return data
        if data['reserved'] + data['spent_known'] + bound > min(data['allowance'], snapshot):
            raise MercuryGateError('conservative free allowance cannot cover reservation')
        data['runs'][run_id] = {'reservation': bound, 'actual_total': None, 'status': 'reserved'}
        data['reserved'] += bound
        _write_ledger(path, data)
        return data

def reconcile(path, run_id, actual_total):
    _run_id(run_id)
    if actual_total is not None:
        _count(actual_total, 'actual total')
    with _ledger_lock(path):
        data = _read_ledger(path)
        run = data['runs'].get(run_id)
        if run is None:
            raise MercuryGateError('reconciliation has no matching reservation')
        if run['status'] == 'reconciled':
            if run['actual_total'] != actual_total:
                raise MercuryGateError('reconciliation replay disagrees with run')
            return data
        if actual_total is None:
            return data
        data['reserved'] -= run['reservation']
        data['spent_known'] += actual_total
        run.update(actual_total=actual_total, status='reconciled')
        _write_ledger(path, data)
        return data

def native_usage(stdout):
    """Main totals include reasoning once; unmeasured auxiliary usage holds reservation."""
    steps = {}
    try:
        for line in stdout.splitlines():
            event = json.loads(line)
            if not isinstance(event, dict) or event.get('type') != 'step_finish':
                continue
            part = event['part']
            tokens = part['tokens']
            key = part['id']
            if not isinstance(key, str) or not key:
                raise ValueError()
            total = _count(tokens['total'], 'native total')
            components = sum(_count(tokens.get(k, 0), k) for k in ('input', 'output', 'reasoning'))
            cache = tokens.get('cache', {})
            components += sum(_count(cache.get(k, 0), k) for k in ('read', 'write'))
            if total != components or (key in steps and steps[key] != total):
                raise ValueError()
            steps[key] = total
    except (ValueError, KeyError, TypeError, AttributeError):
        steps = {}
    return {'status': 'unknown', 'total_tokens': None,
            'observed_main_total_tokens': sum(steps.values()) if steps else None,
            'source': 'opencode-step-finish', 'auxiliary_usage_complete': False,
            'unknown_reason': 'Auxiliary/title usage not attested by step events; reservation held.',
            'cost_usd': None, 'cost_semantics': 'conservative allowance, not authoritative remaining credit'}

def native_rejection(stdout, stderr):
    errors = [stderr]
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') == 'error':
            errors.append(json.dumps(event.get('error', {})))
    text = '\n'.join(errors)
    status_pattern = r'(?:\bHTTP(?:/\d+(?:\.\d+)?)?\s+|\bstatus(?:_?code)?["\x27]?\s*[:=]\s*["\x27]?)(?:401|402|403)\b|\b(?:401\s+unauthorized|403\s+forbidden|402\s+payment required)\b'
    credit_pattern = r'insufficient (?:balance|credit)|credits? (?:exhausted|depleted)|quota exceeded|free.*(?:exhausted|depleted)'
    if re.search(status_pattern + '|' + credit_pattern, text, re.I):
        return {'status': 'recheck-required', 'scope': 'free-pilot', 'source': 'native-provider-error',
                'reason': 'mercury-auth-or-free-allowance-refusal', 'reset_at': None}
    return None

def scrub_output(text, secrets=()):
    text = text.replace(CRED_TARGET, '[REDACTED-CRED-TARGET]')
    for secret in secrets:
        if secret:
            text = text.replace(json.dumps(secret)[1:-1], '[REDACTED]')
            text = text.replace(secret, '[REDACTED]')
    return text
