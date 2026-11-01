"""Fail-closed recovery of this caretaker's fully applied curation transaction.

The journal remains canonical gateway state. This module neither edits it nor
rolls back files; it only verifies a dead owner's completed replacements and
uses the existing exact Git checkpoint before releasing the curator lock.
"""
from __future__ import annotations

import datetime as dt
import errno
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .checkpoint import _git_environment, _machine_name, checkpoint_publication
from .publication import PublicationError, model_lineage, safe_file, sha
from .pubstate import ensure_pub_tables, read_row, record_success


AGENT = 'openclaw-caretaker'
SHA = re.compile(r'[0-9a-f]{64}\Z')
TX = re.compile(r'tx-[0-9a-f]+\Z')
EVENT = re.compile(r'evt-[A-Za-z0-9-]+\Z')
UTC_STAMP = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)\Z')


def _process_alive(pid: object) -> bool:
    """Unverifiable process identity is treated as live, never as permission."""
    if type(pid) is not int or not 0 < pid <= 0xffffffff:
        return True
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel.GetExitCodeProcess.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel.GetLastError() != 87  # ERROR_INVALID_PARAMETER: no such PID
        try:
            code = ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH


def _document(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise PublicationError('invalid_recovery_document')
    return value


_model_lineage = model_lineage


def _owned_file(state: Path, path: Path) -> Path:
    if not path.is_absolute():
        raise PublicationError('recovery_path_outside_state')
    try:
        relative = path.relative_to(state).as_posix()
    except ValueError as exc:
        raise PublicationError('recovery_path_outside_state') from exc
    return safe_file(state, relative)


def _journals(state: Path) -> list[tuple[Path, dict]]:
    if not state.is_dir():
        raise PublicationError('caretaker_state_missing')
    found = []
    # The zero-or-more prefix covers legacy and new-event-epoch project states.
    for candidate in sorted(state.glob('**/curation/*/candidate*/.curation-transactions/tx-*.json')):
        path = _owned_file(state, candidate)
        if not path.is_file():
            raise PublicationError('transaction_journal_unreadable')
        found.append((path, _document(path)))
    return found


def _journal_may_have_touched_vault(journal: dict) -> bool:
    # Old committed transactions are historical evidence, not unfinished work.
    if journal.get('status') == 'committed':
        return False
    rows = journal.get('files')
    return (journal.get('status') == 'applying'
            or isinstance(rows, list) and any(isinstance(row, dict)
                                               and row.get('state') == 'replaced'
                                               for row in rows))


def _vault_unchanged_except_lock(vault: Path) -> bool:
    """A journal-less release needs proof that no transaction changed the vault."""
    root = vault.parent
    cmd = ['git', '-c', 'core.autocrlf=false', '-C', str(root), 'status', '--porcelain',
           '--untracked-files=all', '--', 'vault', ':(exclude)vault/governance/curator.lock']
    proc = subprocess.run(cmd, capture_output=True, timeout=30, env=_git_environment())
    if proc.returncode:
        raise PublicationError('recovery_vault_git_unavailable')
    return not proc.stdout.strip()


def _validate_journal(path: Path, journal: dict, state: Path, vault: Path,
                      lock_id: str) -> tuple[dict, dict, list]:
    transaction = journal.get('transaction')
    event_id = journal.get('event_id')
    rows = journal.get('files')
    if (not isinstance(transaction, str) or not TX.fullmatch(transaction)
            or path.name != transaction + '.json'
            or not isinstance(event_id, str) or not EVENT.fullmatch(event_id)
            or journal.get('status') != 'applying' or journal.get('agent') != AGENT
            or journal.get('lock_id') != lock_id
            or not isinstance(journal.get('vault'), str)
            or Path(journal['vault']).resolve() != vault.resolve()
            or not isinstance(rows, list) or not 1 <= len(rows) <= 32):
        raise PublicationError('recovery_journal_not_fully_applying')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('state') != 'replaced':
            raise PublicationError('recovery_journal_not_fully_replaced')
        name, digest = row.get('path'), row.get('after')
        if (not isinstance(name, str) or name in seen
                or not isinstance(digest, str) or not SHA.fullmatch(digest)
                or not (name.startswith(('wiki/', 'events/', 'governance/curations/'))
                        or name == 'log.md')):
            raise PublicationError('recovery_journal_invalid_path_or_hash')
        seen.add(name)
        file = safe_file(vault, name)
        if not file.is_file() or sha(file.read_bytes()) != digest:
            raise PublicationError('recovery_output_hash_mismatch')

    candidate = path.parent.parent
    manifest_path = _owned_file(state, Path(str(journal.get('manifest') or '')))
    if manifest_path != safe_file(candidate, 'publication.json'):
        raise PublicationError('recovery_manifest_not_in_candidate')
    manifest_bytes = manifest_path.read_bytes()
    digest = sha(manifest_bytes)
    if digest != journal.get('manifest_sha256'):
        raise PublicationError('recovery_manifest_hash_mismatch')
    manifest = json.loads(manifest_bytes)
    if (not isinstance(manifest, dict)
            or not isinstance(manifest.get('vault_path'), str)
            or Path(manifest['vault_path']).resolve() != vault.resolve()
            or not isinstance(manifest.get('inputs'), list) or not manifest['inputs']
            or not isinstance(manifest.get('outputs'), list) or not manifest['outputs']
            or not isinstance(manifest.get('proposal_ids'), list)
            or not manifest['proposal_ids']):
        raise PublicationError('recovery_manifest_invalid')
    input_hashes = {}
    for item in manifest['inputs']:
        if not isinstance(item, dict):
            raise PublicationError('recovery_manifest_input_invalid')
        name, expected = item.get('path'), item.get('sha256')
        if (not isinstance(name, str) or name in input_hashes
                or not isinstance(expected, str) or not SHA.fullmatch(expected)):
            raise PublicationError('recovery_manifest_input_invalid')
        source = safe_file(vault, name)
        if not source.is_file() or sha(source.read_bytes()) != expected:
            raise PublicationError('recovery_input_hash_mismatch')
        input_hashes[name] = expected
    outputs = {row['path']: row['after'] for row in rows}
    for item in manifest['outputs']:
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or outputs.get(item['path']) != item.get('sha256')):
            raise PublicationError('recovery_manifest_output_mismatch')

    review = _document(safe_file(candidate, 'review.json'))
    evidence = review.get('evidence')
    if (review.get('verdict') != 'PASS'
            or review.get('manifest_sha256') != digest or not isinstance(evidence, dict)
            or not isinstance(evidence.get('path'), str)
            or not isinstance(evidence.get('sha256'), str)
            or not SHA.fullmatch(evidence['sha256'])):
        raise PublicationError('recovery_review_not_bound')

    maker = review.get('maker')
    if not isinstance(maker, str) or not maker:
        raise PublicationError('recovery_review_not_bound')
    if maker == AGENT:
        maker_lineage = 'qwen'
    else:
        diag_path = safe_file(candidate.parent, 'diagnostics.json')
        if diag_path.is_file():
            diag = _document(diag_path)
            if (diag.get('maker_route') != 'fleet_append'
                    or diag.get('maker_agent') != maker):
                raise PublicationError('recovery_review_not_bound')
            maker_lineage = diag.get('maker_lineage') or _model_lineage(maker, diag.get('primary_model'))
        else:
            maker_lineage = _model_lineage(maker, None)

    reviewer_obj = review.get('reviewer')
    if isinstance(reviewer_obj, dict):
        reviewer_lineage = (reviewer_obj.get('lineage')
                            or _model_lineage(reviewer_obj.get('id'), None))
    else:
        reviewer_lineage = 'meta'

    if (not maker_lineage or not reviewer_lineage
            or str(maker_lineage).casefold() == str(reviewer_lineage).casefold()):
        raise PublicationError('recovery_review_same_lineage')

    evidence_path = safe_file(candidate, evidence['path'])
    if not evidence_path.is_file() or sha(evidence_path.read_bytes()) != evidence['sha256']:
        raise PublicationError('recovery_review_evidence_hash_mismatch')
    return manifest, input_hashes, rows


def _matching_batch(database, candidate: Path,
                    journal_path: Path, manifest: dict, input_hashes: dict) -> tuple[str, dict]:
    ensure_pub_tables(database)
    matches = []
    for (batch,) in database.execute("SELECT batch FROM publications WHERE status IN ('RUNNING','UNKNOWN_REQUIRES_RECONCILIATION','PUBLISHED')"):
        row = read_row(database, batch)
        value = json.loads(row.get('result') or '{}')
        if not isinstance(value, dict):
            continue
        if row['status'] == 'UNKNOWN_REQUIRES_RECONCILIATION':
            value = json.loads(value.get('previous') or '{}')
        if not isinstance(value, dict):
            continue
        if row['status'] == 'PUBLISHED':
            pub = value.get('publication') or {}
            if not isinstance(pub, dict) or pub.get('transaction') != str(journal_path):
                continue
        else:
            owned = value.get('state_dir')
            if not isinstance(owned, str) or Path(owned).resolve() != candidate.parents[2].resolve():
                continue
        pids, paths, hashes = (value.get(key) for key in ('proposal_ids', 'paths', 'hashes'))
        if (not all(isinstance(x, list) and x for x in (pids, paths, hashes))
                or len(pids) != len(paths) or len(paths) != len(hashes)
                or len(set(pids)) != len(pids)
                or set(pids) != set(manifest['proposal_ids'])
                or any(input_hashes.get(p) != h for p, h in zip(paths, hashes))):
            continue
        matches.append((batch, value))
    if len(matches) != 1:
        raise PublicationError('recovery_batch_identity_not_unique')
    return matches[0]


def _same_lock(lock_path: Path, lock_id: str, acquired_at: str) -> bool:
    try:
        value = _document(lock_path)
        return (value.get('lock_id') == lock_id and value.get('agent') == AGENT
                and value.get('acquired_at') == acquired_at
                and str(value.get('machine') or '').casefold() == _machine_name().casefold()
                and not _process_alive(value.get('process_id')))
    except (OSError, ValueError, TypeError):
        return False


def _release_lock(script: str, lock_id: str) -> None:
    # The gateway owns release semantics; do not rename or unlink this lock here.
    from curate_once import command
    ps = str(Path(script).with_suffix('.ps1'))
    try:
        command(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ps,
                 '-Action', 'release-lock', '-LockId', lock_id])
    except json.JSONDecodeError:
        # Some gateway revisions emit no JSON for release. The caller still
        # requires the lock to be absent before it reports success.
        pass


def _reason(exc: Exception) -> str:
    value = str(exc)
    return value if isinstance(exc, PublicationError) and re.fullmatch(r'[a-z0-9_]+', value) else type(exc).__name__


def _stale_lock_minutes(control: dict | None) -> int:
    policy = (control or {}).get('publication', {}) or {}
    minutes = policy.get('stale_curator_lock_minutes', 30)
    if type(minutes) is not int or minutes <= 0:
        raise PublicationError('invalid_stale_curator_lock_minutes')
    return minutes


def _acquired_at_utc(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not UTC_STAMP.fullmatch(value):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None


def recover_stale_curator_lock(database, base: dict, state: Path,
                               control: dict | None = None,
                               now: dt.datetime | None = None) -> dict:
    """Only a verified, aged own lock can be released or rolled forward."""
    from .hub import CanonicalKnowledgeGateway

    try:
        vault = Path(base['hub']['vault'])
        lock_path = safe_file(vault, 'governance/curator.lock')
        if not lock_path.exists():
            return {'status': 'NO_LOCK'}
        lock = _document(lock_path)
        acquired_at = lock.get('acquired_at')
        acquired = _acquired_at_utc(acquired_at)
        moment = now or dt.datetime.now(dt.timezone.utc)
        foreign_agent = lock.get('agent') != AGENT
        foreign_machine = str(lock.get('machine') or '').casefold() != _machine_name().casefold()
        if foreign_agent or foreign_machine:
            owner_identified = (isinstance(lock.get('agent'), str) and bool(lock['agent'])
                                and isinstance(lock.get('machine'), str) and bool(lock['machine']))
            if (owner_identified and acquired is not None and moment.tzinfo is not None
                    and acquired <= moment
                    and moment - acquired < dt.timedelta(minutes=_stale_lock_minutes(control))):
                return {'status': 'LOCK_FRESH'}
            raise PublicationError('other_agent_lock' if foreign_agent else 'other_machine_lock')
        if acquired is None or moment.tzinfo is None or acquired > moment:
            return {'status': 'LOCK_TIME_UNVERIFIED'}
        if moment - acquired < dt.timedelta(minutes=_stale_lock_minutes(control)):
            return {'status': 'LOCK_FRESH'}
        # The gateway writes the short-lived acquire-lock PowerShell PID. A dead
        # PID is not proof that the publication using this lock has stopped.
        if _process_alive(lock.get('process_id')):
            raise PublicationError('live_or_unverified_owner')
        lock_id = lock.get('lock_id')
        if not isinstance(lock_id, str) or not lock_id:
            raise PublicationError('recovery_lock_id_invalid')

        journals = _journals(Path(state))
        matching = [(path, doc) for path, doc in journals if doc.get('lock_id') == lock_id]
        if len(matching) > 1:
            raise PublicationError('recovery_multiple_matching_journals')
        if not matching:
            if (any(_journal_may_have_touched_vault(doc) for _, doc in journals)
                    or not _vault_unchanged_except_lock(vault)):
                raise PublicationError('recovery_no_journal_vault_changed')
            if not _same_lock(lock_path, lock_id, acquired_at):
                raise PublicationError('recovery_lock_changed')
            _release_lock(base['hub']['script'], lock_id)
            if lock_path.exists():
                raise PublicationError('recovery_lock_release_unverified')
            return {'status': 'RELEASED_NO_TRANSACTION', 'released': True}

        journal_path, journal = matching[0]
        report = {'transaction': journal.get('transaction'), 'event_id': journal.get('event_id')}
        manifest, inputs, _rows = _validate_journal(journal_path, journal, Path(state), vault, lock_id)
        batch, value = _matching_batch(database, journal_path.parent.parent,
                                       journal_path, manifest, inputs)
        gateway = CanonicalKnowledgeGateway(
            python_executable=base['hub'].get('python_executable') or sys.executable,
            script=base['hub']['script'], vault=str(vault), timeout=120)
        if gateway.lint().status != 'pass':
            raise PublicationError('recovery_lint_errors')
        if not _same_lock(lock_path, lock_id, acquired_at):
            raise PublicationError('recovery_lock_changed')
        publication = {'status': 'PUBLISHED', 'transaction': str(journal_path),
                       'event_id': journal['event_id']}
        receipt = journal_path.parent.parent
        checkpoint = checkpoint_publication(publication, vault, receipt,
                                            recovery_lock_id=lock_id)
        if checkpoint.get('status') != 'CHECKPOINTED':
            raise PublicationError('recovery_checkpoint_not_verified')
        report.update(status='RECOVERED', batch=batch, commit=checkpoint['head'],
                      released=False)
        stored = {**value, 'status': 'PUBLISHED', 'publication': publication,
                  'checkpoint': checkpoint, 'recovery': report.copy()}
        if read_row(database, batch)['status'] != 'PUBLISHED':
            record_success(database, batch, stored)
        else:
            with database:
                database.execute('UPDATE publications SET result=? WHERE batch=?',
                                 (json.dumps(stored, ensure_ascii=False), batch))
        cycle_id = value.get('cycle_id')
        if isinstance(cycle_id, str) and cycle_id:
            from .audit import mark_members
            mark_members(database, cycle_id, zip(value['proposal_ids'], value['paths']),
                         'published', 'roll_forward_recovery')
        if not _same_lock(lock_path, lock_id, acquired_at):
            raise PublicationError('recovery_lock_changed')
        _release_lock(base['hub']['script'], lock_id)
        if lock_path.exists():
            raise PublicationError('recovery_lock_release_unverified')
        report['released'] = True
        stored['recovery'] = report
        stored['lock_release'] = {'status': 'RELEASED'}
        with database:
            database.execute('UPDATE publications SET result=? WHERE batch=?',
                             (json.dumps(stored, ensure_ascii=False), batch))
        return report
    except Exception as exc:
        return {'status': 'REQUIRES_REVIEW', 'reason': 'stale_curator_lock_requires_review',
                'detail': _reason(exc)}
