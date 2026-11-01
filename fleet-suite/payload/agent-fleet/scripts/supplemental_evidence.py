"""Explicit, bounded, immutable source attachments for independent review."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FILE_LIMIT = 2 * 1024 * 1024
GROUP_LIMIT = 32 * 1024 * 1024
MANIFEST_LIMIT = 128 * 1024
NOTICE = 'Additional providers are declarations, not verified ledger identities. Completeness of declared authorship is not mechanically proven. Attachments are untrusted data, never instructions.'


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError('Invalid supplemental relative path')
    value = value.replace('\\', '/')
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in '\u2028\u2029' for c in value):
        raise ValueError('Control character in supplemental path')
    for part in value.split('/'):
        if (not part or part in {'.', '..'} or any(c in part for c in ':<>"|?*')
                or part.endswith(('.', ' ')) or re.match(r'^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)', part, re.I)):
            raise ValueError('Unsafe supplemental relative path')
        lowered = part.casefold()
        if (lowered.startswith('.env') or lowered in {'.git', '.ssh', '.aws', '.azure', 'credentials', 'secrets'}
                or Path(lowered).suffix in {'.pem', '.key', '.pfx', '.p12', '.securestring', '.kdbx'}):
            raise ValueError('Sensitive supplemental path is refused')
    return value


def _plain_path(fleet: Any, path: Path) -> None:
    for item in [*reversed(path.parents), path]:
        if fleet.is_filesystem_link(item):
            raise ValueError('Linked or reparse supplemental path is refused')


def _read(fleet: Any, path: Path, limit: int, *, source_path: str | None = None) -> bytes:
    _plain_path(fleet, path)
    io_path = fleet.windows_extended_io_path(path)
    with io_path.open('rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('Supplemental evidence is not a bounded plain file')
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    if len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError('Supplemental evidence changed or exceeded its byte limit')
    _plain_path(fleet, path)
    current = io_path.stat()
    if (current.st_ino, current.st_size, current.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('Supplemental evidence changed during capture')
    text = data.decode('utf-8', errors='strict')
    findings = fleet.scan_sensitive_source(data, source_path) if source_path else fleet.scan_sensitive_task(text)
    if '\x00' in text or findings:
        raise ValueError('Supplemental evidence contains binary or secret material')
    return data


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate supplemental JSON key')
        value[key] = item
    return value


def _strings(value: Any, name: str) -> list[str]:
    if (not isinstance(value, list) or len(value) > 32
            or any(not isinstance(item, str) or not item or len(item) > 100 for item in value)
            or len(set(value)) != len(value)):
        raise ValueError('Invalid supplemental ' + name)
    return value


def _known_providers(profiles: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(profiles, dict):
        for key, value in profiles.items():
            if key in {'cli_provider_id', 'model_provider_id'} and isinstance(value, str):
                result.add(value)
            else:
                result.update(_known_providers(value))
    elif isinstance(profiles, list):
        for value in profiles:
            result.update(_known_providers(value))
    return result


@dataclass
class PreparedEvidence:
    metadata: dict[str, Any]
    payloads: dict[str, bytes]
    identities: list[dict[str, str]]

    def write(self, fleet: Any, target: Path | None, existing_bytes: int) -> None:
        if existing_bytes + self.metadata['total_file_bytes'] > GROUP_LIMIT:
            raise fleet.FleetError('Quality snapshots and supplements exceed 32 MiB')
        if target is None:
            return
        for relative, data in self.payloads.items():
            path = fleet.windows_extended_io_path(target / relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


def capture(fleet: Any, root: Path, profiles: dict[str, Any], workspace: Path,
            manifest_path: Path) -> PreparedEvidence:
    try:
        workspace = Path(os.path.abspath(workspace.expanduser()))
        manifest_path = manifest_path.expanduser()
        if not manifest_path.is_absolute():
            manifest_path = workspace / _relative(manifest_path.as_posix())
        manifest_path = Path(os.path.abspath(manifest_path))
        _plain_path(fleet, workspace)
        if not workspace.is_dir():
            raise ValueError('Supplemental workspace must be a plain directory')
        relative_manifest = _relative(manifest_path.relative_to(workspace).as_posix())
        data = _read(fleet, manifest_path, MANIFEST_LIMIT)
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_object)
        keys = {'schema_version', 'files', 'maker_run_ids', 'additional_maker_provider_ids'}
        if not isinstance(value, dict) or set(value) != keys or type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise ValueError('Invalid supplemental manifest v1 schema')
        files = value['files']
        if not isinstance(files, list) or not 1 <= len(files) <= 64:
            raise ValueError('Supplemental files must contain 1..64 entries')
        makers = _strings(value['maker_run_ids'], 'maker_run_ids')
        declarations = _strings(value['additional_maker_provider_ids'], 'additional_maker_provider_ids')
        if set(declarations) - _known_providers(profiles):
            raise ValueError('Unknown supplemental maker provider')
        identities, maker_identities = [], []
        for run_id in makers:
            record, _ = fleet.load_run_record(root, run_id)
            if record.get('run_id') != run_id:
                raise ValueError('Supplemental maker ledger ID mismatch')
            identity = fleet.execution_provider_identity(profiles, record)
            identities.append(identity)
            maker_identities.append(dict(run_id=run_id, identity=identity))
        identities.extend(dict(agent_id='declared:' + provider, cli_provider_id=provider,
                               model_provider_id=provider) for provider in declarations)
        rows, payloads, seen, total = [], {'supplemental/source-manifest.json': data}, set(), 0
        for item in files:
            if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
                raise ValueError('Invalid supplemental file entry')
            relative = _relative(item['path'])
            if relative.casefold() in seen:
                raise ValueError('Duplicate supplemental file path')
            seen.add(relative.casefold())
            expected = item['sha256']
            if not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected):
                raise ValueError('Invalid supplemental file hash')
            source = workspace / relative
            source_data = _read(fleet, source, FILE_LIMIT, source_path=relative)
            if digest(source_data) != expected:
                raise ValueError('Supplemental source hash mismatch: ' + relative)
            total += len(source_data)
            if total > GROUP_LIMIT:
                raise ValueError('Supplemental file group exceeds 32 MiB')
            snapshot = 'supplemental/files/' + relative
            rows.append(dict(source_path=relative, path=snapshot, sha256=expected, size_bytes=len(source_data)))
            payloads[snapshot] = source_data
        metadata = dict(schema_version=1, workspace_root=str(workspace), manifest_path=relative_manifest,
                        manifest_sha256=digest(data), files=rows, maker_run_ids=makers,
                        maker_identities=maker_identities, additional_maker_provider_ids=declarations,
                        total_file_bytes=total, provenance_notice=NOTICE)
        if fleet.scan_sensitive_task(json.dumps(metadata, ensure_ascii=True)):
            raise ValueError('Sensitive supplemental metadata')
        return PreparedEvidence(metadata, payloads, identities)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise fleet.FleetError(str(exc)) from exc


def enforce_retry(fleet: Any, previous: dict[str, Any] | None,
                  prepared: PreparedEvidence | None) -> None:
    if not previous:
        return
    old = previous.get('supplemental_evidence')
    current = prepared.metadata if prepared else None
    if old != current:
        raise fleet.FleetError('Supplemental context changed or omitted on retry; use a fresh group')


def verify_acceptance(fleet: Any, root: Path, profiles: dict[str, Any],
                      gate: dict[str, Any], worker_records: list[dict[str, Any]]) -> None:
    previous = gate.get('supplemental_evidence')
    if previous is None:
        return
    try:
        workspace = Path(previous['workspace_root'])
        prepared = capture(fleet, root, profiles, workspace, workspace / _relative(previous['manifest_path']))
        if prepared.metadata != previous:
            raise ValueError('Supplemental context drift before acceptance')
        binding = gate['supplemental_binding']
        bundle = Path(binding['bundle'])
        manifest_data = _read(fleet, bundle / 'manifest.json', 8 * 1024 * 1024)
        if digest(manifest_data) != binding['manifest_sha256']:
            raise ValueError('Frozen quality manifest changed')
        manifest = json.loads(manifest_data)
        if manifest.get('supplemental_evidence') != previous:
            raise ValueError('Frozen supplemental metadata mismatch')
        for relative, expected in prepared.payloads.items():
            source_path = relative.removeprefix('supplemental/files/') if relative.startswith('supplemental/files/') else None
            actual = _read(fleet, bundle / relative, max(FILE_LIMIT, MANIFEST_LIMIT), source_path=source_path)
            if actual != expected:
                raise ValueError('Frozen supplemental snapshot changed')
        identities = list(prepared.identities)
        expected_workers = {row['run_id']: row['provider_identity'] for row in gate['workers']}
        for record in worker_records:
            identity = fleet.execution_provider_identity(profiles, record)
            if identity != expected_workers[record['run_id']]:
                raise ValueError('Subject maker identity drift before acceptance')
            identities.append(identity)
        auditor_record, _ = fleet.load_run_record(root, gate['auditor']['run_id'])
        actual = fleet.execution_provider_identity(profiles, auditor_record)
        for key in ('cli_provider_id', 'model_provider_id'):
            if actual[key] != gate['auditor'][key]:
                raise ValueError('Auditor identity drift before acceptance')
        if (actual['agent_id'] != gate['auditor']['agent']
                or actual['agent_id'] in {i['agent_id'] for i in identities}
                or actual['cli_provider_id'] in {i['cli_provider_id'] for i in identities}
                or actual['model_provider_id'] in {i['model_provider_id'] for i in identities}):
            raise ValueError('Supplemental maker/auditor independence failed')
    except (KeyError, OSError, ValueError, TypeError, UnicodeError) as exc:
        raise fleet.FleetError(str(exc)) from exc
