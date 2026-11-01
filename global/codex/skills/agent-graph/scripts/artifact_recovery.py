"""Local provenance for retained maker work; never an acceptance verdict."""
import hashlib
import os
import re
from pathlib import Path
import subprocess

_GIT_OPTIONS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

def dirty_manifest(workspace):
    """Hash Git changes, including existing WIP, without reading them into prompts."""
    root = Path(workspace).resolve()
    proc = subprocess.run(['git', 'status', '--porcelain=v1', '-z', '--untracked-files=all'],
                          cwd=root, capture_output=True, check=True, **_GIT_OPTIONS)
    fields = proc.stdout.decode('utf-8', errors='strict').split('\0')
    names, index = [], 0
    while index < len(fields):
        entry = fields[index]; index += 1
        if not entry: continue
        names.append(entry[3:])
        if 'R' in entry[:2] or 'C' in entry[:2]:
            if index >= len(fields): raise ValueError('Incomplete Git rename')
            names.append(fields[index]); index += 1
    names = [name for name in names if not set(Path(name).parts) &
             {'.git', '__pycache__', '.venv', 'node_modules'}]
    values = manifest(root, names)
    for name in names:
        # Git records deletions/rename sources; absence is not an unchanged hash.
        values.setdefault(Path(name).as_posix(), '<missing>')
    return values

def manifest(workspace, scope):
    root = Path(workspace).resolve()
    result = {}
    for item in scope:
        rel = Path(item)
        if rel.is_absolute() or '..' in rel.parts or ':' in item:
            raise ValueError('Unsafe scope')
        path = root / rel
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise ValueError('Linked or escaping scope')
        files = [path] if path.is_file() else path.rglob('*') if path.is_dir() else []
        for file in files:
            if set(file.parts) & {'.git', '__pycache__', '.venv', 'node_modules'}:
                continue
            if file.is_symlink() or not file.resolve().is_relative_to(root):
                raise ValueError('Linked artifact')
            if file.is_file():
                result[file.relative_to(root).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result

MINIMAX_STRUCTURED_REJECTION_REASON = "minimax-structured-output-rejected"
MINIMAX_STRUCTURED_REJECTION_EXIT_CODE = 4


def minimax_structured_output_rejection(record, identity):
    """Return True iff Fleet record facts prove the MiniMax-M3 structured-output case.

    A native MiniMax-M3 run that the MiniMax adapter rejected for failing the
    strict-JSON handoff schema - exit_code 4, completion_reason AND
    transport_outcome both equal ``minimax-structured-output-rejected``, with a
    verified runtime attestation and a verified native managed MiniMax identity
    - is a handoff/schema defect: the work may be in the worktree and the
    original Fleet record must NOT be rewritten or promoted to success.

    Rejects every other shape, including timeout, tool/step/context limits,
    wrong model/provider, other error4 paths, missing or conflicting facts,
    free-form provider text and ``not-applicable`` identities. Does not infer
    from error strings; only the structured Fleet record fields above count.
    """
    if not isinstance(record, dict) or not isinstance(identity, dict):
        return False
    if (record.get('agent') != 'minimax' or record.get('model') != 'MiniMax-M3'
            or record.get('model_provider_id') != 'minimax'):
        return False
    if (type(record.get('exit_code')) is not int
            or record['exit_code'] != MINIMAX_STRUCTURED_REJECTION_EXIT_CODE
            or record.get('provider_terminal_reason') != 'failed'):
        return False
    if any(record.get(flag, False) is not False
           for flag in ('timed_out', 'tool_budget_exceeded', 'context_stop_exceeded')):
        return False
    guard = record.get('context_guard')
    if guard is not None and (not isinstance(guard, dict) or guard.get('exceeded', False) is not False):
        return False
    if record.get("completion_reason") != MINIMAX_STRUCTURED_REJECTION_REASON:
        return False
    transport = record.get("transport_outcome")
    if not isinstance(transport, dict):
        return False
    if (transport.get("status") != "failed"
            or transport.get("reason") != MINIMAX_STRUCTURED_REJECTION_REASON):
        return False
    if not valid_runtime_attestation(record.get('runtime_attestation')):
        return False
    if (record.get('model_identity') != identity or identity.get('status') != 'verified'
            or identity.get('expected_runtime_model') != 'minimax/MiniMax-M3'
            or identity.get('observed_runtime_models') != ['minimax/MiniMax-M3']
            or identity.get('provider_source') != 'provider'
            or identity.get('provider_kind') != 'minimax-managed'):
        return False
    return True


def retained_candidate(record, result, *, workspace, scope, base, expected_model,
                       expected_agent, lineage, before, before_dirty=None):
    """Fail closed on missing/conflicting provenance; tests and review still pending."""
    if not isinstance(before, dict) or not isinstance(before_dirty, dict) or not scope or not lineage:
        return None
    identity = record.get('model_identity') or {}
    structured = minimax_structured_output_rejection(record, identity)
    if (record.get('run_id') != result.get('run_id') or not record.get('run_id')
            or record.get('agent') != expected_agent
            or record.get('model') != expected_model or result.get('model') != expected_model
            or record.get('model_provider_id') != lineage
            or (record.get('runtime_attestation') or {}).get('status') != 'VERIFIED'
            or record.get('worktree_base_commit_sha') != base
            or record.get('worktree_base_provenance') not in {'preserved', 'recorded'}
            or result.get('artifact_disposition') == 'quarantined'):
        return None
    disposition=result.get('artifact_disposition')
    if (not isinstance(disposition, dict) or disposition.get('status') != 'preserved'
            or disposition.get('base_commit_sha') != base
            or disposition.get('base_provenance') not in {'preserved','recorded'}):
        return None
    identity_ok = (identity.get('status') == 'verified' or
                   expected_agent == 'codex' and identity.get('status') == 'not-applicable'
                   and identity.get('expected_model') == expected_model)
    if not identity_ok:
        return None
    # Path-specific gate. The proven maker-recovery path stays unchanged:
    # exit_code 0 with a populated handoff_parse_error is preserved as
    # 'handoff-unparsed'. The MiniMax-M3 native structured-output case is a
    # second, narrower entry: exit_code 4 with completion_reason AND
    # transport_outcome both equal ``minimax-structured-output-rejected``,
    # verified runtime attestation and a verified native managed MiniMax
    # identity. No handoff_parse_error is required because the rejection
    # happens before the model emits a handoff.
    if not structured and (record.get('exit_code') != 0
            or not record.get('handoff_parse_error')
            or str(record.get('provider_terminal_reason') or '').casefold()
            not in {'success', 'succeeded', 'completed', 'complete', 'stop',
                    'end_turn', 'turn_complete', 'done'}):
        return None
    try:
        root = Path(workspace).resolve(strict=True)
        if Path(record['execution_workspace']).resolve(strict=True) != root:
            return None
        if Path(disposition['worktree']).resolve(strict=True) != root:
            return None
        after = manifest(root, scope)
        current_base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root,
            capture_output=True, text=True, check=True, **_GIT_OPTIONS).stdout.strip()
        if current_base != base:
            return None
        dirty_after = dirty_manifest(root)
        all_changed = {p for p in set(before_dirty) | set(dirty_after)
                       if before_dirty.get(p) != dirty_after.get(p)}
        if any(not any(p == s or p.startswith(s.rstrip('/') + '/') for s in scope)
               for p in all_changed):
            return None
    except (KeyError, ValueError, OSError, subprocess.SubprocessError):
        return None
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    if not changed or not after or any(p not in after for p in changed):
        return None  # No provenance for a change, or unexpected deletion.
    return {'schema_version': 1, 'run_id': result['run_id'], 'workspace': str(root),
            'scope': list(scope), 'base_commit': base, 'model': expected_model,
            'lineage': lineage, 'files': after, 'changed': changed,
            'transport_status': 'handoff-unparsed', 'artifact_status': 'preserved-verified',
            'acceptance_status': 'PENDING_TESTS_AND_REVIEW', 'knowledge_closeout': 'PARENT_PENDING',
            'provenance': 'local-verified-invocation-not-cryptographic-authentication'}


def valid_runtime_attestation(value):
    """Require the exact Suite attestation shape from the trusted local ledger.

    This checks provenance fields, not cryptographic authenticity of local JSON.
    The original Suite invocation verifies the immutable runtime payload.
    """
    if not isinstance(value, dict) or value.get("status") != "VERIFIED":
        return False
    for key in ("package_fingerprint", "payload_fingerprint"):
        if not isinstance(value.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            return False
    version = value.get("package_version")
    count = value.get("payload_file_count")
    root = value.get("runtime_root")
    return (isinstance(version, str) and bool(re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version))
            and isinstance(count, int) and not isinstance(count, bool) and count > 0
            and isinstance(root, str) and Path(root).is_absolute())


def verify_usage_record(record, result, *, workspace, expected_model, lineage, base=None, run_id=None):
    """Verify local authoritative Fleet usage record against result and workspace."""
    if not isinstance(record, dict) or not isinstance(result, dict):
        return False, "missing record or result dictionary"
    rec_run = record.get("run_id")
    res_run = result.get("run_id")
    if not rec_run or (run_id and rec_run != run_id) or (res_run and rec_run != res_run):
        return False, "run_id mismatch or missing in usage record"
    if record.get("model") != expected_model or (result.get("model") and result.get("model") != expected_model):
        return False, "model mismatch with authoritative usage"
    provider_id = record.get("model_provider_id") or record.get("lineage")
    if provider_id != lineage:
        return False, "lineage mismatch with authoritative usage"
    result_provider = result.get("model_provider_id") or result.get("lineage")
    if result_provider and result_provider != provider_id:
        return False, "lineage mismatch between maker result and usage record"
    attestation = record.get("runtime_attestation")
    if not valid_runtime_attestation(attestation):
        return False, "runtime attestation missing or unverified in usage record"
    base_commit = record.get("worktree_base_commit_sha") or record.get("base")
    if not base_commit or (base and base_commit != base):
        return False, "base commit mismatch or missing in authoritative usage"
    result_base = result.get("worktree_base_commit_sha") or result.get("base_commit") or result.get("base")
    if result_base and result_base != base_commit:
        return False, "base commit mismatch between maker result and usage record"
    if record.get("exit_code") != 0:
        return False, f"non-zero exit code in usage record: {record.get('exit_code')}"
    terminal = str(record.get("provider_terminal_reason") or "").casefold()
    if not terminal or terminal not in {'success', 'succeeded', 'completed', 'complete', 'stop', 'end_turn', 'turn_complete', 'done'}:
        return False, f"provider terminal reason {terminal!r} is not success"
    if result.get("artifact_disposition") == "quarantined":
        return False, "artifact disposition is quarantined"
    exec_ws = record.get("execution_workspace")
    if not exec_ws:
        return False, "missing execution workspace in usage record"
    try:
        if Path(exec_ws).resolve() != Path(workspace).resolve():
            return False, "execution workspace mismatch in usage record"
    except (ValueError, OSError):
        return False, "invalid execution workspace in usage record"
    return True, ""
