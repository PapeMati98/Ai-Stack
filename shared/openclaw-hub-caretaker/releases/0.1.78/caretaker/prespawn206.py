"""Narrow proof of Kimi CreateProcess failure before any native process exists.

Unknown usage is not interpreted as zero. The supervisor stack, its terminal
checkpoint and every listed attempt are the positive pre-spawn evidence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import os
import subprocess
import sqlite3
from datetime import datetime
from itertools import product
from .publication import PublicationError

EVIDENCE_ROOT = Path.home() / '.agents/agent-fleet/evidence'


def _bytes(path, limit=2 * 1024 * 1024):
    path = Path(path)
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError('reparse evidence')
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('bounded regular evidence required')
    return path.read_bytes()


def _json(path):
    value = json.loads(_bytes(path).decode('utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('evidence object required')
    return value


def _checkpoint_stderr_matches(error, checkpoint):
    if (len(error) == checkpoint.get('stderr_bytes')
            and hashlib.sha256(error).hexdigest() == checkpoint.get('stderr_sha256')):
        return True
    # Suite123 supervisor hashes raw subprocess stderr. Its return path decodes
    # UTF8(errors=replace), then Fleet writes Windows text. A localized OS error
    # may contain at most two invalid single bytes. Exhaustively prove the raw
    # hash and the documented decoding roundtrip; do not waive a mismatched hash.
    replacement = b'\xef\xbf\xbd'
    pieces = error.split(replacement)
    missing = len(pieces) - 1
    if missing not in (1, 2) or len(error) - 2 * missing != checkpoint.get('stderr_bytes'):
        return False
    for values in product(range(128, 256), repeat=missing):
        raw = pieces[0]
        for value, piece in zip(values, pieces[1:]):
            raw += bytes([value]) + piece
        if hashlib.sha256(raw).hexdigest() != checkpoint.get('stderr_sha256'):
            continue
        decoded = raw.decode('utf-8', errors='replace').replace('\r\n', '\n')
        return decoded.replace('\n', '\r\n').encode('utf-8') == error
    return False


def prove_fleet_prespawn206(result_path, workspace, *, evidence_root=None):
    """Read-only; return pinned terminal proof or None, never assistant content."""
    try:
        result_path = Path(result_path)
        evidence_root = Path(evidence_root or EVIDENCE_ROOT)
        root = result_path.parent
        if (result_path.name != 'result.json' or root.parent.resolve() != evidence_root.resolve()
                or not re.fullmatch(r'run-[a-zA-Z0-9-]+', root.name)):
            return None
        result = _json(result_path)
        workspace = Path(workspace).resolve(strict=True)
        if (result.get('run_id') != root.name or result.get('agent') != 'kimi'
                or result.get('status') != 'FAILED' or result.get('exit_code') != 1
                or result.get('timed_out') is not False
                or result.get('continuation_count') != 0
                or result.get('handoff') is not None
                or result.get('provider_session_id') is not None
                or any(Path(result.get(key) or '').resolve() != workspace
                       for key in ('workspace', 'execution_workspace'))):
            return None
        # A positive native usage/identity/effect contradicts a pre-spawn error;
        # null telemetry supplies no evidence in either direction.
        for owner in [result, *(result.get('provider_attempts') or [])]:
            if not isinstance(owner, dict):
                return None
            if any(owner.get(key) not in (None, '', [], {}, False, 0) for key in (
                    'native_started', 'provider_session_id', 'artifacts', 'effects',
                    'native_artifacts', 'model_tool_calls')):
                return None
            usage = owner.get('token_usage') or {}
            if (not isinstance(usage, dict) or any(
                    isinstance(value, (int, float)) and value > 0
                    for key, value in usage.items() if key.endswith('_tokens'))):
                return None
            identity = owner.get('model_identity') or {}
            if identity.get('observed_models'):
                return None
        attempts = result.get('provider_attempts')
        if not isinstance(attempts, list) or not attempts or len(attempts) > 4:
            return None
        pins = {str(result_path): hashlib.sha256(_bytes(result_path)).hexdigest()}
        supervisors = []
        for number, attempt in enumerate(attempts, 1):
            if (attempt.get('attempt') != number or attempt.get('exit_code') != 1
                    or attempt.get('timed_out') is not False):
                return None
            command = attempt.get('command')
            if (not isinstance(command, list) or not command
                    or Path(command[0]).name.lower() not in ('kimi', 'kimi.exe')
                    or '-p' not in command):
                return None
            stderr_path = root / f'attempt-{number}.stderr.txt'
            stdout_path = root / f'attempt-{number}.stdout.txt'
            if any(Path(attempt.get(key) or '').resolve() != path.resolve()
                   for key, path in (('stderr_path', stderr_path), ('stdout_path', stdout_path))):
                return None
            checkpoint_path = root / f'attempt-{number}.process-checkpoint.json'
            checkpoint = _json(checkpoint_path)
            error = _bytes(stderr_path, 32 * 1024)
            output = _bytes(stdout_path, 32 * 1024)
            # Fleet text log materialization may translate LF to Windows CRLF.
            normalized = error.replace(b'\r\n', b'\n')
            if (checkpoint.get('schema') != 'agent-fleet.process-checkpoint.v1'
                    or checkpoint.get('status') != 'process_failed'
                    or checkpoint.get('exit_code') != 1
                    or not checkpoint.get('finished_at')
                    or type(checkpoint.get('pid')) is not int
                    or output or checkpoint.get('stdout_bytes') != 0
                    or checkpoint.get('stdout_sha256') != hashlib.sha256(b'').hexdigest()
                    or not _checkpoint_stderr_matches(error, checkpoint)):
                return None
            text = normalized.decode('utf-8')
            if (text.count('Traceback (most recent call last):') != 1
                    or 'process_supervision.py' not in text or 'in _child' not in text
                    or 'in _execute_child' not in text
                    or not re.search(r'FileNotFoundError: \[WinError 206\]', text)
                    or not text.rstrip().splitlines()[-1].startswith('FileNotFoundError: [WinError 206]')):
                return None
            for path in (stderr_path, stdout_path, checkpoint_path):
                pins[str(path)] = hashlib.sha256(_bytes(path)).hexdigest()
            supervisors.append({'pid': checkpoint['pid'], 'started_at': checkpoint.get('started_at'),
                                'finished_at': checkpoint['finished_at']})
        return {'classification': 'conclusively_not_started', 'run_id': root.name,
                'workspace': str(workspace), 'pins': pins, 'supervisors': supervisors,
                'history_task_binding': 'NOT_RETAINED'}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def owned_process_active(workspace, config_path, supervisors):
    """Local bounded process read; command lines never leave this function."""
    if os.name != 'nt':
        return True
    script = 'Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CreationDate,CommandLine | ConvertTo-Json -Compress'
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, timeout=12,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        return True
    processes = json.loads(result.stdout.decode('utf-8-sig'))
    if not isinstance(processes, list):
        return True
    ancestors = {os.getpid()}
    while True:
        parents = {p['ParentProcessId'] for p in processes if p['ProcessId'] in ancestors}
        if parents <= ancestors:
            break
        ancestors |= parents
    needles = (str(workspace).casefold(), str(config_path).casefold())
    for process in processes:
        if process['ProcessId'] in ancestors:
            continue
        command = str(process.get('CommandLine') or '').casefold()
        if any(needle in command for needle in needles):
            return True
        for supervisor in supervisors:
            if process['ProcessId'] != supervisor['pid']:
                continue
            # A reused PID is not the former supervisor. CIM's JSON Date
            # representation is interpreted only when provably comparable.
            creation = process.get('CreationDate')
            match = re.fullmatch(r'/Date\((\d+)\)/', str(creation))
            if not match or not supervisor.get('started_at'):
                return True
            started = datetime.fromisoformat(supervisor['started_at'].replace('Z', '+00:00')).timestamp()
            if abs(int(match[1]) / 1000 - started) < 3:
                return True
    return False


def legacy206_plan(config_path, database, row, *, evidence_root=None):
    """Qualify only the already-reserved terminal-generation append repair.

    Cardinality follows from the pinned controller's one selected maker,
    fail-fast wrapper, unchanged initial counters and zero format repairs.
    All exact-workspace Fleet results must independently prove pre-spawn206.
    """
    try:
        import curate_once
        from caretaker.config import load_config
        from caretaker.publication import freeze, safe_file, sha
        from caretaker.pubstate import canonical_verdict, code_version
        from caretaker.registry import read_registry, main_page
        from caretaker.terminal_generation import canonical_proposal_identities, helper_sha256
        from caretaker.repaired_retry import REPAIRED_PUBLICATION_SHA256, retry_token, TERMINAL_GENERATION_RETRY_REASON
        from caretaker.draft_summary import _prior_inventory_body_proof

        cfg = load_config(config_path)
        old = json.loads(row.get('result') or '{}')
        marker = (cfg.raw or {}).get('maintenance_terminal_generation_retry')
        if (row.get('status') != 'UNKNOWN_REQUIRES_RECONCILIATION'
                or old.get('reason') != 'terminal_generation_maker_outcome_unknown'
                or old.get('project') != cfg.project_slug or not isinstance(marker, dict)
                or marker.get('lane') != 'legacy' or marker.get('project') != cfg.project_slug
                or marker.get('helper_sha256') != helper_sha256()
                or marker.get('publication_sha256') != REPAIRED_PUBLICATION_SHA256
                or marker.get('code_version') != code_version()
                or marker.get('cycle_id') != old.get('cycle_id')):
            return None
        identities = canonical_proposal_identities(old.get('paths'), old.get('proposal_ids'), old.get('hashes'))
        if identities is None or marker.get('paths') != [i[1] for i in identities]:
            return None
        paths = [i[1] for i in identities]
        cycle = database.execute('SELECT lane,status,counts FROM audit_cycles WHERE id=?', (marker['cycle_id'],)).fetchone()
        source = database.execute('SELECT status,fingerprint,result FROM publications WHERE batch=?', (marker.get('previous_batch'),)).fetchone()
        if (cycle is None or cycle[0] != 'legacy' or cycle[1] not in ('ACTIVE', 'DRAINED')
                or source is None or source[0] != 'QUARANTINED' or source[1] != marker.get('previous_fingerprint')):
            return None
        selected = marker.get('members')
        source_result = json.loads(source[2])
        if (source_result.get('reason') != 'phase_resume_terminal_no_replay'
                or source_result.get('project') != cfg.project_slug
                or canonical_proposal_identities(source_result.get('paths'), source_result.get('proposal_ids'), source_result.get('hashes')) != identities
                or not isinstance(selected, list) or len(selected) != len(identities)
                or json.loads(cycle[2]).get('repaired_retry') != selected):
            return None
        for item in selected:
            identity = (item.get('proposal_id'), item.get('path'), item.get('sha256'))
            if (identity not in identities or item.get('token') != retry_token(
                    REPAIRED_PUBLICATION_SHA256, cfg.project_slug, *identity,
                    reason=TERMINAL_GENERATION_RETRY_REASON, helper_sha256=helper_sha256())):
                return None
            token = database.execute('SELECT cycle_id,proposal_id,path,sha256,previous_batch FROM repaired_retry_tokens WHERE token=?', (item['token'],)).fetchone()
            if tuple(token or ()) != (marker['cycle_id'], *identity, marker['previous_batch']):
                return None
        if len({(i['proposal_id'], i['path'], i['sha256']) for i in selected}) != len(identities):
            return None
        reserved = database.execute('SELECT proposal_id,path,sha256,project FROM audit_members WHERE cycle_id=?',
                                    (marker['cycle_id'],)).fetchall()
        if (len(reserved) != len(identities)
                or {tuple(item) for item in reserved} != {(*identity, cfg.project_slug) for identity in identities}):
            return None
        vault, phase_state = Path(cfg.hub.vault), Path(cfg.state_dir) / 'curation'
        registry, warning = read_registry(vault)
        if registry is None or warning is not None:
            return None
        frozen = freeze(vault, paths, main_page(cfg.project_slug, registry), attestation_state_dir=Path(cfg.state_dir))
        pointer = curate_once._phase_pointer(phase_state, cfg.project_slug, paths)
        record = _json(pointer)
        progress = record.get('progress') or {}
        if (record.get('cache_sha256') != curate_once._phase_digest(record)
                or record.get('phase') != 'REPAIR_STARTED' or progress.get('phase') != 'REPAIR_STARTED'
                or record.get('project') != cfg.project_slug or record.get('paths') != paths
                or record.get('code_version') != code_version()
                or not curate_once._phase_bindings_equal(record.get('binding'), curate_once._phase_binding(frozen))
                or record.get('candidate') is not None or record.get('manifest') is not None
                or record.get('manifest_sha256') is not None or record.get('pins') != {}
                or progress.get('repair_attempt') != 1
                or progress.get('maker_format_repairs') != 0
                or progress.get('reviewer_format_repairs') != {}
                or any(progress.get(k) not in (None, '', {}, []) for k in ('maker_response', 'review_response', 'final', 'status'))):
            return None
        run_root = safe_file(phase_state, record.get('run_root'))
        if (run_root.parent.resolve() != phase_state.resolve()
                or not re.fullmatch(r'terminal-generation-retry-[a-f0-9]{16}', run_root.name)
                or run_root.is_symlink() or run_root.is_junction() or not run_root.is_dir()):
            return None
        if any(p.name not in ('.fleet_publication_runner.tracebacks.log', 'run-configs') for p in run_root.iterdir()):
            return None
        configs = list((run_root / 'run-configs').iterdir())
        if (len(configs) != 1 or not configs[0].name.endswith('-maker-candidate-review-repair-append0.json')
                or _json(configs[0]) != {}):
            return None
        # A format retry writes a different run-config AND increments
        # maker_calls/format_repairs before the next REPAIR_STARTED checkpoint.
        archives = []
        for archived in phase_state.glob('*/terminal-generation-archive-*/phase.json'):
            prior_record = _json(archived)
            if (prior_record.get('project') == cfg.project_slug and prior_record.get('paths') == paths
                    and prior_record.get('binding') == record.get('binding')):
                archives.append((archived, prior_record))
        if len(archives) != 1:
            return None
        archived, prior_record = archives[0]
        prior_progress = prior_record.get('progress') or {}
        if (prior_record.get('cache_sha256') != curate_once._phase_digest(prior_record)
                or prior_record.get('code_version') != code_version()
                or prior_record.get('phase') != 'REPAIR_STARTED'
                or any(progress.get(k) != prior_progress.get(k) for k in (
                    'maker_calls', 'review_calls', 'maker_format_repairs', 'reviewer_format_repairs', 'assessment', 'review_rounds'))
                or type(progress.get('maker_calls')) is not int or progress['maker_calls'] < 1
                or type(progress.get('review_calls')) is not int or progress['review_calls'] < 1):
            return None
        candidate = archived.parent.parent / 'candidate'
        manifest = candidate / 'publication.json'
        doc = _json(manifest)
        assessment = progress.get('assessment') or {}
        pins = _json(archived.parent / 'candidate-pins.json')
        if (assessment.get('verdict') != 'REPAIR' or assessment.get('manifest_sha256') != sha(_bytes(manifest))
                or not curate_once._manifest_matches_frozen(doc, frozen)
                or doc.get('vault_path') != str(vault) or doc.get('ledger_path') != 'ledger.json'
                or curate_once._phase_pins(phase_state, candidate) != pins
                or sha(_bytes(candidate / 'ledger.json')) != doc.get('ledger_sha256')):
            return None
        for name, checksum in pins.items():
            if sha(_bytes(safe_file(phase_state, name))) != checksum:
                return None
        for output in doc.get('outputs') or []:
            target = safe_file(vault, output['path'])
            if ((sha(_bytes(target)) if target.exists() else None) != output.get('preimage_sha256')
                    or sha(_bytes(safe_file(candidate, output['payload_path']))) != output.get('sha256')):
                return None
        if (candidate / '.curation-transactions').exists():
            return None
        canonical_row = dict(row)
        canonical_value = dict(old, state_dir=str(cfg.state_dir))
        canonical_row['result'] = json.dumps(canonical_value)
        verdict, canonical = canonical_verdict(vault, canonical_row)
        if verdict != 'absent':
            return None
        evidence_root = Path(evidence_root or EVIDENCE_ROOT)
        proofs = []
        results = list(evidence_root.glob('run-*/result.json'))
        if len(results) > 12000:
            return None
        for path in results:
            value = _json(path)
            if any(Path(value.get(k) or '').resolve() == run_root.resolve() for k in ('workspace', 'execution_workspace')):
                proof = prove_fleet_prespawn206(path, run_root, evidence_root=evidence_root)
                if proof is None:
                    return None
                proofs.append(proof)
        # Single initial call follows from the controller/phase/counter/config
        # proof above; multiple dispatches in the same generation are ambiguous.
        if len(proofs) != 1 or len(proofs[0]['supervisors']) != 1:
            return None
        if owned_process_active(run_root, config_path, proofs[0]['supervisors']):
            return None
        prior_inventory = _prior_inventory_body_proof(vault, Path(cfg.state_dir), frozen)
        if prior_inventory is None:
            return None
        return {'cfg': cfg, 'paths': paths, 'frozen': frozen, 'pointer': pointer,
                'record': record, 'progress': progress, 'run_root': run_root,
                'candidate': candidate, 'manifest': manifest, 'pins': pins,
                'assessment': assessment, 'prior_inventory_body': prior_inventory['body'],
                'proof': proofs[0], 'row': dict(row), 'canonical': canonical,
                'archive': archived.parent}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError,
            PublicationError, sqlite3.Error, subprocess.SubprocessError):
        return None


def resume_legacy206(plan, config_path, database, invoke):
    """Consume one transport recovery, preserving original row/phase evidence."""
    import curate_once
    from caretaker.pubstate import record_success, read_row, canonical_verdict
    from caretaker.audit import mark_members
    from caretaker.publication import freeze
    row, cfg = plan['row'], plan['cfg']
    # Recheck the exact row, phase and frozen bytes immediately before the
    # one-time durable intent. A crash after intent is conservative UNKNOWN.
    if read_row(database, row['batch']) != row or _json(plan['pointer']) != plan['record']:
        return None
    current = freeze(Path(cfg.hub.vault), plan['paths'], plan['frozen']['target'],
                     attestation_state_dir=Path(cfg.state_dir))
    if current != plan['frozen']:
        return None
    for name, checksum in plan['proof']['pins'].items():
        if hashlib.sha256(_bytes(name)).hexdigest() != checksum:
            return None
    old = json.loads(row['result'])
    canonical_row = dict(row, result=json.dumps(dict(old, state_dir=str(cfg.state_dir))))
    if canonical_verdict(Path(cfg.hub.vault), canonical_row)[0] != 'absent':
        return None
    if owned_process_active(plan['run_root'], config_path, plan['proof']['supervisors']):
        return None
    receipt_path = plan['run_root'] / 'transport-recovery206.json'
    receipt = {'schema_version': 1, 'classification': 'conclusively_not_started',
               'proof': plan['proof'], 'previous_row': row, 'previous_phase': plan['record'],
               'frozen_binding': curate_once._phase_binding(plan['frozen']),
               'generation': str(plan['run_root']), 'source_archive': str(plan['archive']),
               'history_task_binding': 'NOT_RETAINED', 'max_transport_recoveries': 1}
    try:
        with receipt_path.open('x', encoding='utf-8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return None
    progress = dict(plan['progress'], phase='REPAIR_REQUIRED')
    # repair_attempt, model counters, epoch and all retry tokens retain their
    # values. REPAIR_REQUIRED denotes the unstarted reserved transport call.
    curate_once._save_phase_progress(cfg, plan['paths'], plan['frozen'], plan['run_root'],
        plan['record']['observed'], 'REPAIR_REQUIRED', plan['candidate'], plan['manifest'], progress)
    projected = _json(plan['pointer'])
    continuation = dict(plan, continuation=True, current_pointer=plan['pointer'],
                        current_record=projected, transport_recovery_receipt=str(receipt_path))
    result = _invoke_with_known_budget(continuation, config_path, invoke)
    result.update(batch=row['batch'], cycle_id=old['cycle_id'], project=cfg.project_slug,
                  proposal_ids=old['proposal_ids'], paths=old['paths'], hashes=old['hashes'])
    result['transport_recovery'] = {'classification': 'conclusively_not_started',
                                    'receipt': str(receipt_path), 'previous_row': row,
                                    'run_id': plan['proof']['run_id']}
    result_path = plan['run_root'] / 'transport-recovery206-result.json'
    with result_path.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    if result.get('status') == 'PUBLISHED':
        record_success(database, row['batch'], result)
        mark_members(database, old['cycle_id'],
                     list(zip(old['proposal_ids'], old['paths'])), 'published', 'transport206_reconciled')
    # Any uncertain or failed later attempt retains the original UNKNOWN row;
    # the single consumed intent prevents another maker retry.
    return result


def _budget_phase(cfg, paths, frozen, run_root):
    """Use the unchanged full loader; a started/unknown phase is never safe."""
    import curate_once
    state = Path(cfg.state_dir) / 'curation'
    loaded = curate_once._load_phase_progress(cfg, paths, state, frozen)
    if (not isinstance(loaded, dict)
            or loaded.get('phase') not in {'REPAIR_PREPARED', 'PREPARED', 'REVIEW_ACCEPTED'}
            or Path(loaded.get('run_root_path') or '').resolve() != run_root.resolve()
            or not Path(loaded.get('candidate_path') or '').resolve().is_relative_to(run_root.resolve())):
        return None
    pointer = curate_once._phase_pointer(state, cfg.project_slug, paths)
    record = _json(pointer)
    if (Path(loaded['candidate_path']) / '.curation-transactions').exists():
        return None
    return pointer, record, loaded


def _invoke_with_known_budget(plan, config_path, invoke):
    """Persist a known cooperative stop only when the full phase is prepared.

    In particular the original curator may raise after fresh PASS while entering
    publish. A native timeout with a STARTED phase is never translated here.
    """
    from caretaker import tick_budget
    from caretaker.pubstate import canonical_verdict
    try:
        return dict(invoke(config_path, plan['paths'], _transport_recovery=plan))
    except tick_budget.TickBudgetExceeded as stopped:
        try:
            prepared = _budget_phase(plan['cfg'], plan['paths'], plan['frozen'], plan['run_root'])
            if prepared is None:
                raise stopped
            value = json.loads(plan['row']['result'])
            probe = dict(plan['row'], result=json.dumps(dict(value, state_dir=str(plan['cfg'].state_dir))))
            if canonical_verdict(Path(plan['cfg'].hub.vault), probe)[0] != 'absent':
                raise stopped
            _, record, loaded = prepared
            return {'status': 'DEFERRED_PREPARED', 'reason': 'tick_time_budget_exhausted',
                    'phase': record['phase'], 'run_path': str(plan['run_root']),
                    'candidate_path': loaded['candidate_path'], 'manifest_sha256': record['manifest_sha256'],
                    'maker_calls': loaded['maker_calls'], 'review_calls': loaded['review_calls'],
                    'pre_effect': True,
                    'model_calls': max(0, loaded['maker_calls'] - plan['record']['progress']['maker_calls'])
                                   + max(0, loaded['review_calls'] - plan['record']['progress']['review_calls'])}
        except (OSError, ValueError, TypeError, KeyError, PublicationError):
            raise stopped


def budget206_plan(config_path, database, row):
    """Only a durable known budget defer after consumed206 may continue review.

    The original UNKNOWN/transport receipt/result are retained. This does not
    qualify a fresh transport recovery and never admits an unfinished native call.
    """
    try:
        import curate_once
        from caretaker.config import load_config
        from caretaker.publication import freeze, safe_file
        from caretaker.pubstate import canonical_verdict, code_version
        from caretaker.registry import read_registry, main_page
        from caretaker.terminal_generation import canonical_proposal_identities, helper_sha256
        from caretaker.repaired_retry import REPAIRED_PUBLICATION_SHA256, retry_token, TERMINAL_GENERATION_RETRY_REASON
        from caretaker.draft_summary import _prior_inventory_body_proof
        cfg = load_config(config_path)
        old = json.loads(row.get('result') or '{}')
        marker = (cfg.raw or {}).get('maintenance_terminal_generation_retry')
        if (row.get('status') != 'UNKNOWN_REQUIRES_RECONCILIATION'
                or old.get('reason') != 'terminal_generation_maker_outcome_unknown'
                or old.get('project') != cfg.project_slug or not isinstance(marker, dict)
                or marker.get('lane') != 'legacy' or marker.get('project') != cfg.project_slug
                or marker.get('cycle_id') != old.get('cycle_id')
                or marker.get('helper_sha256') != helper_sha256()
                or marker.get('publication_sha256') != REPAIRED_PUBLICATION_SHA256
                or marker.get('code_version') != code_version()):
            return None
        identities = canonical_proposal_identities(old.get('paths'), old.get('proposal_ids'), old.get('hashes'))
        if not identities or marker.get('paths') != [i[1] for i in identities]:
            return None
        paths = [i[1] for i in identities]
        source = database.execute('SELECT status,result FROM publications WHERE batch=?', (marker.get('previous_batch'),)).fetchone()
        cycle = database.execute('SELECT lane,counts FROM audit_cycles WHERE id=?', (old['cycle_id'],)).fetchone()
        selected = marker.get('members')
        if (source is None or source[0] != 'QUARANTINED' or cycle is None or cycle[0] != 'legacy'
                or json.loads(cycle[1]).get('repaired_retry') != selected
                or not isinstance(selected, list) or len(selected) != len(identities)
                or {(i.get('proposal_id'), i.get('path'), i.get('sha256')) for i in selected} != set(identities)):
            return None
        source_value = json.loads(source[1])
        if (source_value.get('reason') != 'phase_resume_terminal_no_replay'
                or canonical_proposal_identities(source_value.get('paths'), source_value.get('proposal_ids'), source_value.get('hashes')) != identities):
            return None
        for item in selected:
            identity = (item['proposal_id'], item['path'], item['sha256'])
            token = retry_token(REPAIRED_PUBLICATION_SHA256, cfg.project_slug, *identity,
                               reason=TERMINAL_GENERATION_RETRY_REASON, helper_sha256=helper_sha256())
            stored = database.execute('SELECT cycle_id,proposal_id,path,sha256,previous_batch FROM repaired_retry_tokens WHERE token=?', (token,)).fetchone()
            if item.get('token') != token or tuple(stored or ()) != (old['cycle_id'], *identity, marker['previous_batch']):
                return None
        state, vault = Path(cfg.state_dir) / 'curation', Path(cfg.hub.vault)
        pointer = curate_once._phase_pointer(state, cfg.project_slug, paths)
        record = _json(pointer)
        root = safe_file(state, record.get('run_root'))
        if root.parent.resolve() != state.resolve() or not re.fullmatch(r'terminal-generation-retry-[a-f0-9]{16}', root.name):
            return None
        receipt_path = root / 'transport-recovery206.json'
        receipt = _json(receipt_path)
        if (receipt.get('classification') != 'conclusively_not_started'
                or receipt.get('previous_row') != row or receipt.get('generation') != str(root)
                or receipt.get('max_transport_recoveries') != 1):
            return None
        proof = receipt.get('proof') or {}
        for name, digest in proof.get('pins', {}).items():
            if hashlib.sha256(_bytes(name)).hexdigest() != digest:
                return None
        if not proof.get('pins') or Path(proof.get('workspace') or '').resolve() != root.resolve():
            return None
        # Each subsequent review-only call has an immutable intent+terminal
        # result pair. An unmatched intent is an uncertain call and blocks.
        intents = sorted(root.glob('transport-budget-*.intent.json'))
        results = sorted(root.glob('transport-budget-*.result.json'))
        if len(intents) != len(results) or len(results) > 128:
            return None
        latest = _json(root / 'transport-recovery206-result.json')
        receipt_sha = hashlib.sha256(_bytes(receipt_path)).hexdigest()
        for number, (intent_path, result_path) in enumerate(zip(intents, results), 1):
            if (intent_path.name != f'transport-budget-{number:04d}.intent.json'
                    or result_path.name != f'transport-budget-{number:04d}.result.json'):
                return None
            intent, completed = _json(intent_path), _json(result_path)
            if (intent.get('receipt_sha256') != receipt_sha
                    or completed.get('intent_sha256') != hashlib.sha256(_bytes(intent_path)).hexdigest()):
                return None
            latest = completed.get('result')
            if not isinstance(latest, dict):
                return None
        if (latest.get('status') != 'DEFERRED_PREPARED'
                or latest.get('reason') != 'tick_time_budget_exhausted'
                or latest.get('project') != cfg.project_slug
                or latest.get('batch') != row['batch'] or latest.get('cycle_id') != old['cycle_id']
                or canonical_proposal_identities(latest.get('paths'), latest.get('proposal_ids'), latest.get('hashes')) != identities
                or any(latest.get(k) not in (None, '', {}, []) for k in (
                    'publication', 'checkpoint', 'event_id', 'transaction', 'publication_event'))):
            return None
        registry, warning = read_registry(vault)
        if registry is None or warning is not None:
            return None
        frozen = freeze(vault, paths, main_page(cfg.project_slug, registry), attestation_state_dir=Path(cfg.state_dir))
        if not curate_once._phase_bindings_equal(receipt.get('frozen_binding'), curate_once._phase_binding(frozen)):
            return None
        prepared = _budget_phase(cfg, paths, frozen, root)
        if prepared is None:
            return None
        pointer, record, loaded = prepared
        if (latest.get('phase') != record['phase']
                or Path(latest.get('candidate_path') or '').resolve() != Path(loaded['candidate_path']).resolve()
                or latest.get('manifest_sha256') != record.get('manifest_sha256')
                or latest.get('maker_calls') != loaded.get('maker_calls')
                or latest.get('review_calls') != loaded.get('review_calls')):
            return None
        if results and completed.get('phase_sha256') != hashlib.sha256(_bytes(pointer)).hexdigest():
            return None
        canonical_row = dict(row, result=json.dumps(dict(old, state_dir=str(cfg.state_dir))))
        if canonical_verdict(vault, canonical_row)[0] != 'absent':
            return None
        if owned_process_active(root, config_path, proof.get('supervisors') or []):
            return None
        prior_inventory = _prior_inventory_body_proof(vault, Path(cfg.state_dir), frozen)
        if prior_inventory is None:
            return None
        return {'cfg': cfg, 'paths': paths, 'frozen': frozen, 'row': dict(row),
                'pointer': pointer, 'record': record, 'run_root': root,
                'current_pointer': pointer, 'current_record': record, 'continuation': True,
                'budget_continuation': True, 'budget_sequence': len(results) + 1,
                'prior_inventory_body': prior_inventory['body'], 'receipt_sha256': receipt_sha}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError,
            PublicationError, sqlite3.Error, subprocess.SubprocessError):
        return None


def resume_budget206(plan, config_path, database, invoke):
    """Continue a known prepared candidate; preserve all old206 evidence bytes."""
    import curate_once
    from caretaker.pubstate import record_success, read_row
    from caretaker.audit import mark_members
    if read_row(database, plan['row']['batch']) != plan['row'] or _json(plan['pointer']) != plan['record']:
        return None
    root, number = plan['run_root'], plan['budget_sequence']
    intent_path = root / f'transport-budget-{number:04d}.intent.json'
    result_path = root / f'transport-budget-{number:04d}.result.json'
    intent = {'receipt_sha256': plan['receipt_sha256'], 'phase_sha256': hashlib.sha256(_bytes(plan['pointer'])).hexdigest(),
              'classification': 'prepared_budget_continuation', 'generation': str(root)}
    with intent_path.open('x', encoding='utf-8') as stream:
        json.dump(intent, stream, sort_keys=True, indent=2);stream.flush();os.fsync(stream.fileno())
    result = _invoke_with_known_budget(plan, config_path, invoke)
    old = json.loads(plan['row']['result'])
    result.update(batch=plan['row']['batch'], cycle_id=old['cycle_id'], project=old['project'],
                  proposal_ids=old['proposal_ids'], paths=old['paths'], hashes=old['hashes'])
    # Only a durable exact phase may supply a later budget continuation.
    prepared = None
    if result.get('status') == 'DEFERRED_PREPARED' and result.get('reason') == 'tick_time_budget_exhausted':
        prepared = _budget_phase(plan['cfg'], plan['paths'], plan['frozen'], root)
    completed = {'intent_sha256': hashlib.sha256(_bytes(intent_path)).hexdigest(),
                 'result': result, 'phase_sha256': hashlib.sha256(_bytes(prepared[0])).hexdigest() if prepared else None}
    with result_path.open('x', encoding='utf-8') as stream:
        json.dump(completed, stream, sort_keys=True, indent=2);stream.flush();os.fsync(stream.fileno())
    if result.get('status') == 'PUBLISHED':
        result['transport_budget_continuation'] = {'receipt': str(root / 'transport-recovery206.json'), 'result': str(result_path)}
        record_success(database, plan['row']['batch'], result)
        mark_members(database, old['cycle_id'], list(zip(old['proposal_ids'], old['paths'])), 'published', 'transport206_budget_continued')
    return result
