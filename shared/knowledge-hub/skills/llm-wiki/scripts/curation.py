"""Owner-only, source-preserving transactional Knowledge Hub publisher.

The parent must authorize the current publish/rollback operation and hold the
curator lock. Review PASS is evidence, never authorization. The atomic lock is
advisory coordination, not a security boundary against a hostile local OS user.
Only per-file replacement is atomic; readers needing a consistent multi-file
view must cooperate with the curator lock.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parent
DEFAULT_VAULT = ROOT.parents[2] / "vault"
try:  # honor the configured vault exactly like the contributor gateway
    _configured_vault = json.loads(
        (ROOT.parents[2] / "knowledge-config.json").read_text(encoding="utf-8")
    ).get("vault_path")
    if _configured_vault:
        DEFAULT_VAULT = Path(_configured_vault)
except (OSError, ValueError):
    pass
_spec = importlib.util.spec_from_file_location("knowledge", ROOT / "knowledge.py")
knowledge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(knowledge)
_spec_fence = importlib.util.spec_from_file_location("event_write_lock", ROOT / "event_write_lock.py")
event_write_lock = importlib.util.module_from_spec(_spec_fence)
_spec_fence.loader.exec_module(event_write_lock)

_KERNEL32 = None
_GET_FILE_ATTRIBUTES_W = None
if os.name == "nt":
    try:
        _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _GET_FILE_ATTRIBUTES_W = _KERNEL32.GetFileAttributesW
        _GET_FILE_ATTRIBUTES_W.argtypes = [ctypes.c_wchar_p]
        _GET_FILE_ATTRIBUTES_W.restype = ctypes.c_uint32
    except Exception:
        _KERNEL32 = None
        _GET_FILE_ATTRIBUTES_W = None

HEX64 = re.compile(r"[0-9a-f]{64}")
TX_ID = re.compile(r"tx-[0-9a-f]{32}")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,240}")
RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9\u00b9\u00b2\u00b3]|LPT[1-9\u00b9\u00b2\u00b3])", re.I)
IO_WORKERS = 4
DISPOSITIONS = {"synthesized", "historical", "unresolved", "orphan"}
MANIFEST_KEYS = {"schema_version", "vault_path", "snapshot_sha256", "inputs", "outputs",
                 "source_ids", "proposal_ids", "ledger_path", "ledger_sha256", "summary"}
RESOLUTION_KEYS = {"schema_version", "vault_path", "resolution_id", "proposal_id", "supersedes",
                   "effective_disposition", "reason", "evidence"}
RESOLUTION_REVIEW_KEYS = {"schema_version", "verdict", "maker", "reviewer",
                          "resolution_sha256", "evidence"}
RESOLUTION_RECORD_KEYS = {"schema_version", "resolution_id", "proposal_id", "supersedes",
                          "effective_disposition", "reason", "evidence", "review", "review_sha256",
                          "resolution_sha256", "recorded_at", "agent", "event_id"}


class CurationError(RuntimeError):
    pass


def _fail(message: str) -> None:
    raise CurationError(message)


def _ordered_bounded_map(executor: ThreadPoolExecutor, function, values):
    """Run independent filesystem checks concurrently, preserving input order and bounded memory."""
    iterator = iter(values)
    pending = []

    def fill():
        while len(pending) < IO_WORKERS:
            try:
                value = next(iterator)
            except StopIteration:
                return
            pending.append(executor.submit(function, value))

    fill()
    try:
        while pending:
            future = pending.pop(0)
            result = future.result()
            yield result
            fill()
    except BaseException:
        for future in pending:
            future.cancel()
        raise


def _parallel_ordered_map(function, values):
    with ThreadPoolExecutor(max_workers=IO_WORKERS, thread_name_prefix="curation-io") as executor:
        yield from _ordered_bounded_map(executor, function, values)


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_relative(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        _fail(f"unsafe {label} path")
    parts = value.split("/")
    for part in parts:
        if (part in {"", ".", ".."} or part.endswith((".", " "))
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)
                or RESERVED.fullmatch(part.split(".")[0])):
            _fail(f"unsafe {label} path: {value}")
    return value


def _absolute(path: Path) -> Path:
    # Inspect lexical components BEFORE resolve() can erase a symlink or '..'.
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    _safe_relative("/".join(path.parts[1:]), label="absolute")
    for cursor in reversed((path, *path.parents)):
        reparse = _native_reparse_status(cursor)
        if reparse is None:
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                continue
            reparse = stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0) & 0x400
            )
        if reparse:
            _fail(f"reparse or symlink path: {cursor}")
    return path


def _windows_attribute_path(path: Path) -> str | None:
    """Use an extended Win32 path so attribute probes also handle long paths."""
    value = os.fspath(path)
    if value.startswith("\\\\?\\"):
        if value[:8].casefold() == "\\\\?\\unc\\":
            return value
        if (len(value) >= 7 and value[4].upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                and value[5:7] == ":\\"):
            return value
        return None
    if value.startswith("\\\\.\\"):
        return None
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    if len(value) >= 3 and value[1:3] == ":\\":
        return "\\\\?\\" + value
    return None


def _native_reparse_status(path: Path) -> bool | None:
    """Return the Win32 reparse bit, or None to use the full lstat fallback."""
    function = _GET_FILE_ATTRIBUTES_W
    if function is None:
        return None
    api_path = _windows_attribute_path(path)
    if api_path is None:
        return None
    try:
        attributes = int(function(api_path))
    except Exception:
        return None
    if attributes == 0xFFFFFFFF:
        return None
    return bool(attributes & 0x400)


def _safe_path(root: Path, relative: str, *, label: str, allow_missing: bool = False) -> Path:
    relative = _safe_relative(relative, label=label)
    root = _absolute(root)
    path = _absolute(root / relative)
    cursor = root
    for part in relative.split("/"):
        if cursor.is_dir():
            matches = [p.name for p in cursor.iterdir() if p.name.casefold() == part.casefold()]
            if matches and matches != [part]:
                _fail(f"case conflict in {label}: {relative}")
        cursor /= part
    if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
        _fail(f"{label} is not a regular file: {relative}")
    if not path.exists() and not allow_missing:
        _fail(f"missing {label}: {relative}")
    return path


def _read(path: Path) -> bytes:
    path = _absolute(path)
    try:
        # Bound reads to the file's current size plus the gateway's one-byte
        # growth check, instead of allocating a 1 MiB buffer for small files.
        return knowledge.read_regular_file_snapshot(
            path.parent, path, max_bytes=path.lstat().st_size
        )[0]
    except knowledge.GatewayError as exc:
        raise CurationError(str(exc)) from exc


def digest_file(path: Path) -> str:
    return digest_bytes(_read(path))


def _pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode_json(data: bytes) -> object:
    try:
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: _fail("non-finite JSON value"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CurationError("invalid JSON") from exc


def _json(path: Path) -> object:
    return _decode_json(_read(path))


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _sha_field(value: object, label: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        _fail(f"invalid SHA-256 for {label}")
    return value


def _exact_keys(row: object, keys: set[str], label: str) -> dict:
    if not isinstance(row, dict) or set(row) != keys:
        _fail(f"{label} schema mismatch")
    return row


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be nonempty text")
    return value


def _unique(values: object, label: str) -> list[str]:
    if not isinstance(values, list) or any(not isinstance(v, str) or not ID.fullmatch(v) for v in values):
        _fail(f"invalid {label}")
    if len(values) != len({v.casefold() for v in values}):
        _fail(f"duplicate {label}")
    return values


def _scan(data: bytes, label: str, *, untrusted: bool = False) -> None:
    risk = knowledge.risk_assessment(data.decode("utf-8-sig"))
    if risk["secret_hits"] or (risk["injection_hits"] and not untrusted):
        _fail(f"risk indicator in {label}")


def _owned_vault(requested: object) -> Path:
    if not isinstance(requested, (str, Path)):
        _fail("invalid vault path")
    vault = _absolute(Path(requested))
    default = _absolute(DEFAULT_VAULT)
    if not default.is_dir() or vault != default:
        _fail("vault path is not DEFAULT_VAULT")
    return default


def _external(path: Path, vault: Path) -> Path:
    path = _absolute(path)
    if path.is_relative_to(vault):
        _fail("transaction/review/manifest artifacts must be outside vault")
    return path


def _validate_lock(vault: Path, lock_id: str, agent: str, expected: dict | None = None) -> dict:
    vault = _owned_vault(vault)
    if not isinstance(lock_id, str) or not ID.fullmatch(lock_id) or not knowledge.AGENT_PATTERN.fullmatch(agent):
        _fail("invalid lock ID or agent")
    config_bytes = _read(_safe_path(vault, "knowledge-vault.json", label="config"))
    lock_bytes = _read(_safe_path(vault, "governance/curator.lock", label="lock"))
    config, lock = _decode_json(config_bytes), _decode_json(lock_bytes)
    if not isinstance(config, dict) or config.get("curation_scope") != "designated-machine-only":
        _fail("vault is not designated-machine-only")
    machine = knowledge.local_machine_name()
    owner = config.get("curator_machine")
    if not machine or not isinstance(owner, str) or owner.casefold() != machine.casefold():
        _fail("current machine is not the configured curator machine")
    if (not isinstance(lock, dict) or lock.get("lock_id") != lock_id or lock.get("agent") != agent
            or not isinstance(lock.get("machine"), str) or lock["machine"].casefold() != machine.casefold()):
        _fail("curator lock does not match lock-id, trusted machine, and agent")
    binding = {"config_sha256": digest_bytes(config_bytes), "lock_sha256": digest_bytes(lock_bytes)}
    if expected is not None and expected != binding:
        _fail("held lock or owner configuration changed")
    return binding


def _validate_review(review_path: Path, manifest_digest: str, agent: str, vault: Path) -> tuple[dict, Path]:
    review = _exact_keys(_json(review_path), {"schema_version", "verdict", "maker", "reviewer",
                                               "manifest_sha256", "evidence"}, "review")
    if type(review["schema_version"]) is not int or review["schema_version"] != 1 or review["verdict"] != "PASS":
        _fail("unsupported review schema or verdict")
    if _sha_field(review["manifest_sha256"], "review manifest") != manifest_digest:
        _fail("review is not bound to the exact manifest bytes")
    reviewer = _exact_keys(review["reviewer"], {"id", "lineage"}, "reviewer")
    maker = _text(review["maker"], "maker")
    identity = _text(reviewer["id"], "reviewer ID")
    _text(reviewer["lineage"], "reviewer lineage")
    if identity.casefold() in {maker.casefold(), agent.casefold()}:
        _fail("reviewer identity must be distinct from maker")
    evidence = _exact_keys(review["evidence"], {"path", "sha256"}, "review evidence")
    evidence_name = _text(evidence["path"], "evidence path")
    evidence_path = Path(evidence_name)
    if not evidence_path.is_absolute():
        evidence_path = _safe_path(review_path.parent, evidence_name, label="review evidence")
    evidence_path = _external(evidence_path, vault)
    if digest_file(evidence_path) != _sha_field(evidence["sha256"], "review evidence"):
        _fail("review evidence hash mismatch")
    return review, evidence_path


def _tree_files(vault: Path, directory: str) -> list[Path]:
    base = _absolute(vault / directory)
    if not base.exists():
        return []
    result = []
    with ThreadPoolExecutor(max_workers=IO_WORKERS, thread_name_prefix="curation-tree") as executor:
        for current, dirs, files in os.walk(base, followlinks=False):
            dirs.sort(key=lambda name: (name.casefold(), name))
            files.sort(key=lambda name: (name.casefold(), name))
            names = dirs + files
            if len(names) != len({name.casefold() for name in names}):
                _fail(f"case conflict in directory: {current}")
            for name in names:
                _safe_relative(name, label="tree")
            # Wait for every entry check before os.walk advances into child
            # directories. _absolute retains the full ancestor reparse check.
            tuple(_ordered_bounded_map(executor, _absolute,
                                      (Path(current) / name for name in names)))
            result.extend(Path(current) / name for name in files)
    return sorted(result)


def _source_index(vault: Path) -> dict:
    result = {}
    seen = set()

    def read_source(path: Path) -> tuple[str, tuple[dict, Path]]:
        metadata = _json(path)
        if not isinstance(metadata, dict):
            _fail("invalid source metadata")
        sid = metadata.get("source_id")
        if (not isinstance(sid, str) or not knowledge.SOURCE_ID_PATTERN.fullmatch(sid)
                or path.parent.name != sid):
            _fail("duplicate or invalid source ID")
        # Safe even for nonselected sources before the shared linter opens them.
        _safe_path(path.parent, metadata.get("original_file"), label="source original")
        return sid, (metadata, path)

    paths = (path for path in _tree_files(vault, "raw/sources") if path.name == "source.json")
    for sid, record in _parallel_ordered_map(read_source, paths):
        if sid.casefold() in seen:
            _fail("duplicate or invalid source ID")
        seen.add(sid.casefold())
        result[sid] = record
    return result


def _proposal_index(vault: Path) -> dict:
    result = {}
    seen = set()

    def read_proposal(path: Path) -> tuple[str, tuple[str, Path]]:
        content = _read(path).decode("utf-8-sig")
        pid = knowledge.fm_value(content, "id")
        if (not isinstance(pid, str) or not knowledge.PROPOSAL_ID_PATTERN.fullmatch(pid)
                or not path.name.startswith(pid + "-")):
            _fail("duplicate or invalid proposal ID")
        return pid, (content, path)

    paths = (path for path in _tree_files(vault, "proposals") if path.suffix == ".md")
    for pid, record in _parallel_ordered_map(read_proposal, paths):
        if pid.casefold() in seen:
            _fail("duplicate or invalid proposal ID")
        seen.add(pid.casefold())
        result[pid] = record
    return result


def _validate_inputs(vault: Path, manifest: dict, *, current_event_id: str | None = None) -> dict:
    frozen = {}
    for row in manifest["inputs"]:
        _exact_keys(row, {"path", "sha256", "bytes"}, "input")
        name = _safe_relative(row["path"], label="input")
        if name.casefold() in {p.casefold() for p in frozen}:
            _fail("duplicate input path")
        data = _read(_safe_path(vault, name, label="input"))
        if type(row["bytes"]) is not int or row["bytes"] < 0 or len(data) != row["bytes"]:
            _fail(f"input byte count mismatch: {name}")
        if digest_bytes(data) != _sha_field(row["sha256"], "input"):
            _fail(f"input hash mismatch: {name}")
        frozen[name] = data
    sources, proposals = _source_index(vault), _proposal_index(vault)
    selected_sources = {sid for sid, (_, p) in sources.items() if p.relative_to(vault).as_posix() in frozen}
    selected_proposals = {pid for pid, (_, p) in proposals.items() if p.relative_to(vault).as_posix() in frozen}
    if selected_sources != set(manifest["source_ids"]) or selected_proposals != set(manifest["proposal_ids"]):
        _fail("every source metadata and proposal ID/path must be frozen in inputs exactly once")
    _reject_previously_accounted_proposals(vault, manifest["proposal_ids"], current_event_id=current_event_id)

    def validate_source(sid: str) -> None:
        metadata, path = sources[sid]
        meta_data = _read(path)
        if meta_data != frozen[path.relative_to(vault).as_posix()]:
            _fail(f"source metadata changed after input read: {sid}")
        original = _safe_path(path.parent, metadata["original_file"], label="source original")
        data = _read(original)
        if (digest_bytes(data) != metadata.get("sha256") or type(metadata.get("byte_length")) is not int
                or len(data) != metadata["byte_length"]):
            _fail(f"source original does not match metadata: {sid}")
        if frozen.get(original.relative_to(vault).as_posix()) != data:
            _fail(f"source original is not frozen in inputs: {sid}")
        _scan(data, "source original", untrusted=True)
        _scan(meta_data, "source metadata")

    for _ in _parallel_ordered_map(validate_source, manifest["source_ids"]):
        pass

    def validate_proposal(pid: str) -> None:
        content, path = proposals[pid]
        if _read(path) != frozen[path.relative_to(vault).as_posix()]:
            _fail(f"proposal changed after input read: {pid}")
        refs = _unique(knowledge.fm_list(content, "source_ids"), "proposal source IDs")
        if not set(refs).issubset(manifest["source_ids"]):
            _fail("proposal refers to an unfrozen source ID")
        _scan(frozen[path.relative_to(vault).as_posix()], "proposal", untrusted=True)

    for _ in _parallel_ordered_map(validate_proposal, manifest["proposal_ids"]):
        pass
    return {"frozen": frozen, "sources": sources, "proposals": proposals}


def _reject_previously_accounted_proposals(
    vault: Path, proposal_ids: list[str], *, current_event_id: str | None = None
) -> None:
    selected = {pid.casefold(): pid for pid in proposal_ids}
    if not selected:
        return
    for path in _tree_files(vault, "governance/curations"):
        if path.stem == current_event_id:
            continue
        if path.suffix.casefold() != ".json":
            _fail(f"invalid curation ledger path: {path.name}")
        ledger = _json(path)
        if not isinstance(ledger, dict) or not isinstance(ledger.get("proposals"), list):
            _fail(f"invalid prior curation ledger: {path.name}")
        for row in ledger["proposals"]:
            if not isinstance(row, dict) or not isinstance(row.get("proposal_id"), str):
                _fail(f"invalid prior curation proposal: {path.name}")
            prior = selected.get(row["proposal_id"].casefold())
            if prior is not None:
                _fail(f"proposal already accounted by {path.name}: {prior}")


def _wiki_path(name: object, *, generated: bool = False) -> str:
    name = _safe_relative(name, label="wiki output")
    if not name.startswith("wiki/") or not name.endswith(".md"):
        _fail(f"output is outside wiki Markdown: {name}")
    if name.casefold() == "wiki/index.md" and (not generated or name != "wiki/index.md"):
        _fail("wiki/index.md is reserved for the generator")
    return name


def _current(vault: Path, name: str) -> str | None:
    target = _safe_path(vault, name, label="target", allow_missing=True)
    return digest_file(target) if target.exists() else None


def _validate_outputs(vault: Path, manifest: dict, manifest_dir: Path) -> list[dict]:
    result, seen = [], set()
    for row in manifest["outputs"]:
        _exact_keys(row, {"path", "payload_path", "sha256", "preimage_sha256"}, "output")
        name = _wiki_path(row["path"])
        if name.casefold() in seen:
            _fail("duplicate output path (casefold)")
        seen.add(name.casefold())
        after = _sha_field(row["sha256"], "output")
        before = row["preimage_sha256"]
        if before is not None:
            _sha_field(before, "preimage")
        payload = _safe_path(manifest_dir, row["payload_path"], label="payload")
        data = _read(payload)
        if digest_bytes(data) != after:
            _fail(f"payload hash mismatch: {name}")
        if _current(vault, name) != before:
            _fail(f"output preimage mismatch: {name}")
        _scan(data, "payload")
        text = data.decode("utf-8-sig")
        producer = "producer" in (knowledge.fm_value(text, "verification_scope") or "").casefold()
        if (producer or knowledge.fm_value(text, "type") == "evidence-ledger") and knowledge.fm_value(text, "status") == "verified":
            _fail("source-report output cannot claim verified status")
        result.append({"relative": name, "target": vault / name, "payload": payload,
                       "content": data, "after": after, "before": before})
    return result


def _validate_ledger(vault: Path, manifest: dict, manifest_dir: Path, inputs: dict, outputs: list[dict]) -> dict:
    path = _safe_path(manifest_dir, manifest["ledger_path"], label="ledger")
    data = _read(path)
    if digest_bytes(data) != _sha_field(manifest["ledger_sha256"], "ledger"):
        _fail("ledger hash mismatch")
    ledger = _decode_json(data)
    if not isinstance(ledger, dict) or type(ledger.get("schema_version")) is not int or ledger["schema_version"] != 1:
        _fail("invalid ledger schema")
    if ledger.get("snapshot_sha256") != manifest["snapshot_sha256"]:
        _fail("ledger snapshot mismatch")
    planned = {row["relative"]: row["content"] for row in outputs}
    for key, ids in (("sources", manifest["source_ids"]), ("proposals", manifest["proposal_ids"])):
        rows = ledger.get(key)
        count = ledger.get("source_count" if key == "sources" else "proposal_count")
        if not isinstance(rows, list) or type(count) is not int or count != len(ids):
            _fail(f"ledger {key} count mismatch")
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                _fail("invalid ledger row")
            ident = row.get("source_id" if key == "sources" else "proposal_id")
            if not isinstance(ident, str) or ident not in ids or ident.casefold() in seen:
                _fail(f"ledger {key} dispositions are not exactly once")
            seen.add(ident.casefold())
            if row.get("disposition") not in DISPOSITIONS:
                _fail("invalid ledger disposition")
            _text(row.get("reason"), "ledger reason")
            if key == "sources":
                meta, meta_path = inputs["sources"][ident]
                source_name = (meta_path.parent / meta["original_file"]).relative_to(vault).as_posix()
                if row.get("path") != source_name or row.get("sha256") != meta["sha256"]:
                    _fail("ledger source path/hash mismatch")
            else:
                content, proposal_path = inputs["proposals"][ident]
                name = proposal_path.relative_to(vault).as_posix()
                if row.get("sha256") != digest_bytes(inputs["frozen"][name]):
                    _fail("ledger proposal hash mismatch")
                if "path" in row and row["path"] != name:
                    _fail("ledger proposal path mismatch")
                if "source_ids" in row and set(_unique(row["source_ids"], "ledger source IDs")) != set(knowledge.fm_list(content, "source_ids")):
                    _fail("ledger proposal source IDs mismatch")
            if key == "proposals" or "history_path" in row:
                history = _safe_relative(row.get("history_path"), label="ledger history")
                if not history.startswith("wiki/history/") or not history.endswith(".md"):
                    _fail("ledger history must be under wiki/history")
                history_data = planned.get(history)
                if history_data is None:
                    history_data = _read(_safe_path(vault, history, label="ledger history"))
                if knowledge.fm_value(history_data.decode("utf-8-sig"), "type") != "evidence-ledger":
                    _fail("ledger history page must be an evidence-ledger")
        if len(seen) != len(ids):
            _fail(f"ledger {key} dispositions are not exactly once")
    _scan(data, "ledger")
    return ledger


def _lint(vault: Path, label: str) -> dict:
    result = knowledge.lint(vault)
    if result.get("error_count") != 0:
        _fail(f"{label} lint failed: {result.get('error_count')} errors")
    return result


def _regular_file_identity(path: Path, label: str) -> tuple[int, int, int, int, int]:
    path = _absolute(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        _fail(f"{label} is not a safe regular file: {path}")
    return knowledge.stat_identity(info)


def _snapshot(vault: Path) -> dict[str, object]:
    files = [vault / "knowledge-vault.json"]
    if (vault / "log.md").exists():
        files.append(vault / "log.md")
    for directory in ("raw/sources", "proposals", "wiki", "events", "governance"):
        files.extend(_tree_files(vault, directory))

    def snapshot_file(p: Path) -> tuple[str, object]:
        rel = p.relative_to(vault).as_posix()
        if rel.startswith(("raw/sources/", "proposals/")):
            return rel, _regular_file_identity(p, "snapshot input")
        return rel, _read(p)

    return dict(_parallel_ordered_map(snapshot_file, files))


def _baseline_changed(before: dict[str, bytes], current: dict[str, bytes]) -> bool:
    """Freeze existing bytes and publication outputs, but permit new intake records.

    Other agents may add intake records while a curator stages a reviewed
    publication. Every preexisting file remains frozen. The event set is
    rebased into log.md at the publication boundary and frozen thereafter.
    Selected inputs are also revalidated at the write boundary.
    """
    if any(current.get(name) != data for name, data in before.items()):
        return True
    append_only = ("raw/sources/", "proposals/", "events/")
    return any(not name.startswith(append_only) for name in current.keys() - before.keys())


def _event_snapshot(vault: Path) -> dict[str, bytes]:
    paths = (path for path in _tree_files(vault, "events")
             if path.suffix == ".json" and not path.name.startswith("."))
    ordered_paths = list(paths)
    groups: dict[Path, list[tuple[Path, str]]] = {}
    for path in ordered_paths:
        groups.setdefault(path.parent, []).append(
            (path, path.relative_to(vault).as_posix()),
        )

    def read_group(group: tuple[Path, list[tuple[Path, str]]]) -> list[tuple[str, bytes]]:
        parent, entries = group
        result = []
        try:
            with knowledge.invocation_read_scope(parent):
                for path, relative in entries:
                    # Keep the lexical per-file walk and secure snapshot check.
                    result.append((relative, _read(path)))
                # The scope closes directory handles on exit; revalidate first.
                knowledge._recheck_invocation_ancestors()
        except knowledge.GatewayError as exc:
            raise CurationError(str(exc)) from exc
        return result

    read_by_path = {}
    for group_result in _parallel_ordered_map(read_group, groups.items()):
        read_by_path.update(group_result)
    # Groups may have interleaved paths in the original tree walk. Rebuild in
    # that original order rather than returning grouped order.
    return {
        path.relative_to(vault).as_posix(): read_by_path[path.relative_to(vault).as_posix()]
        for path in ordered_paths
    }


def _cell(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ").replace("[", "\\[").replace("]", "\\]")


def _merge_index(baseline: dict, outputs: list[dict]) -> bytes:
    pages = {name: data for name, data in baseline.items() if name.startswith("wiki/") and name.endswith(".md") and name != "wiki/index.md"}
    pages.update({item["relative"]: item["content"] for item in outputs})
    rows = []
    for name, data in sorted(pages.items(), key=lambda item: item[0].casefold()):
        content = data.decode("utf-8-sig")
        value = lambda key: knowledge.fm_value(content, key) or ""
        scope = value("verification_scope")
        producer = "producer" in scope.casefold() or value("type") == "evidence-ledger"
        last_verified = "not independently verified" if producer else value("last_verified")
        rows.append(f"| [{_cell(value('title'))}]({quote(name[5:])}) | {_cell(value('type'))} | {_cell(value('status'))} | "
                    f"{_cell(scope)} | {_cell(last_verified)} | {_cell(value('last_reviewed'))} | {_cell(value('summary'))} |")
    return ("# Knowledge Index\n\n| Page | Type | Status | Verification scope | Last verified | Last reviewed | Summary |\n"
            "|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n").encode("utf-8")


def _merge_log(baseline: dict, event: dict) -> bytes:
    events = {}
    for name, data in baseline.items():
        if name.startswith("events/") and name.endswith(".json"):
            value = _decode_json(data)
            if not isinstance(value, dict) or value.get("event_id") in events:
                _fail("invalid or duplicate log event")
            events[value["event_id"]] = value
    if event["event_id"] in events:
        _fail("curation event already exists")
    events[event["event_id"]] = event
    rows = ["| " + " | ".join(_cell(item.get(key)) for key in ("occurred_at", "agent", "action", "summary")) + " |"
            for item in sorted(events.values(), key=lambda v: (v.get("occurred_at", ""), v["event_id"]), reverse=True)]
    return ("# Knowledge Event Log\n\n| Time | Agent | Action | Summary |\n|---|---|---|---|\n"
            + "\n".join(rows) + "\n").encode("utf-8")


def _event_paths(event_id: str, occurred_at: str) -> tuple[str, str]:
    return (f"events/{occurred_at[:4]}/{occurred_at[5:7]}/{event_id}.json",
            f"governance/curations/{event_id}.json")


def _planned_files(ctx: dict) -> list[dict]:
    files = list(ctx["outputs"])
    event = {"event_id": ctx["event_id"], "occurred_at": ctx["occurred_at"], "agent": ctx["agent"],
             "action": "record-curation", "proposal_ids": ctx["manifest"]["proposal_ids"],
             "source_ids": ctx["manifest"]["source_ids"], "summary": ctx["manifest"]["summary"],
             "pages_changed": [item["relative"][5:] for item in files] + ["index.md"]}
    ctx["curation_event"] = event
    event_path, ledger_path = _event_paths(ctx["event_id"], ctx["occurred_at"])
    # Exact manifest-only staging (root KH request 2026-10-05): a manifest may opt
    # out of regenerating the shared index/log so a transaction touches only its
    # own manifest pages plus its own brand-new event/ledger records.
    gen = ctx["manifest"].get("generate") or {}
    event["pages_changed"] = [x for x in event["pages_changed"] if gen.get("index", True) or x != "index.md"]
    ledger = dict(ctx["ledger"])
    ledger.update(event_id=ctx["event_id"], transaction=ctx["transaction"],
                  manifest_sha256=ctx["manifest_sha256"], ledger_sha256=ctx["manifest"]["ledger_sha256"],
                  review=ctx["review"], review_sha256=ctx["review_sha256"], summary=ctx["manifest"]["summary"],
                  counts={key: {d: sum(row["disposition"] == d for row in ctx["ledger"][key])
                                for d in sorted(DISPOSITIONS)} for key in ("sources", "proposals")})
    generated = ([("wiki/index.md", _merge_index(ctx["baseline"], files))] if gen.get("index", True) else [])         + ([("log.md", _merge_log(ctx["baseline"], event))] if gen.get("log", True) else [])         + [(event_path, _json_bytes(event)), (ledger_path, _json_bytes(ledger))]
    for name, content in generated:
        before = ctx["baseline"].get(name)
        if name in (event_path, ledger_path) and before is not None:
            _fail("transaction audit record already exists")
        files.append({"relative": name, "target": ctx["vault"] / name, "content": content,
                      "before": digest_bytes(before) if before is not None else None,
                      "after": digest_bytes(content)})
    for item in files:
        before = ctx["baseline"].get(item["relative"])
        before_bytes = before if isinstance(before, bytes) else (_read(ctx["vault"] / item["relative"]) if before is not None else None)
        if (digest_bytes(before_bytes) if before_bytes is not None else None) != item["before"]:
            _fail(f"preimage changed during planning: {item['relative']}")
        item["preimage_content"] = before_bytes
    return files


def _link_or_copy_overlay_input(vault: Path, target: Path, relative: str,
                                expected_identity: tuple[int, int, int, int, int]) -> None:
    _safe_relative(relative, label="overlay input")
    source = vault / relative
    if _regular_file_identity(source, "overlay input") != expected_identity:
        _fail(f"overlay input changed after snapshot: {relative}")
    _absolute(target.parent)
    try:
        os.link(source, target)
    except OSError:
        # Cross-volume, filesystem, or policy restrictions fall back to the
        # same secure snapshot read used before this optimization.
        if os.path.lexists(target):
            _fail(f"unexpected overlay target: {relative}")
        content = _read(source)
        if _regular_file_identity(source, "overlay input") != expected_identity:
            _fail(f"overlay input changed while copying: {relative}")
        _absolute(target)
        with target.open("xb") as stream:
            stream.write(content)
        return
    if (_regular_file_identity(source, "overlay input") != expected_identity
            or _regular_file_identity(target, "overlay link") != expected_identity):
        _fail(f"overlay input changed while linking: {relative}")


def _lint_overlay(ctx: dict) -> None:
    overlay_parent = _absolute(ctx["manifest_path"].parent)
    try:
        temporary = tempfile.TemporaryDirectory(prefix="hub-curation-lint-", dir=str(overlay_parent))
    except OSError:
        temporary = tempfile.TemporaryDirectory(prefix="hub-curation-lint-")
    with temporary as directory:
        overlay = _external(Path(directory), ctx["vault"])
        contents = dict(ctx["baseline"])
        contents.update({item["relative"]: item["content"] for item in ctx["files"]})
        for name, data in contents.items():
            path = overlay / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if (isinstance(data, tuple) and name.startswith(("raw/sources/", "proposals/"))):
                _link_or_copy_overlay_input(ctx["vault"], path, name, data)
            elif isinstance(data, bytes):
                path.write_bytes(data)
            else:
                path.write_bytes(_read(ctx["vault"] / name))
        _lint(overlay, "staged overlay")


def validate(manifest_path: Path, review_path: Path, lock_id: str, agent: str) -> dict:
    vault = _owned_vault(DEFAULT_VAULT)
    manifest_path = _external(manifest_path, vault)
    review_path = _external(review_path, vault)
    raw = _read(manifest_path)
    _decoded = _decode_json(raw)
    if not isinstance(_decoded, dict) or not (
            set(_decoded) == MANIFEST_KEYS or set(_decoded) == MANIFEST_KEYS | {"generate"}):
        _fail("manifest schema mismatch")
    manifest = _decoded
    _gen = manifest.get("generate")
    if _gen is not None and (not isinstance(_gen, dict)
                             or set(_gen) - {"index", "log"}
                             or any(not isinstance(_gen[k], bool) for k in _gen)):
        _fail("manifest generate flag must be {index?: bool, log?: bool}")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        _fail("unsupported manifest schema")
    _owned_vault(manifest["vault_path"])
    binding = _validate_lock(vault, lock_id, agent)
    review_raw = _read(review_path)
    review, evidence = _validate_review(review_path, digest_bytes(raw), agent, vault)
    if _decode_json(review_raw) != review:
        _fail("review changed while being validated")
    _sha_field(manifest["snapshot_sha256"], "snapshot")
    _unique(manifest["source_ids"], "source IDs")
    _unique(manifest["proposal_ids"], "proposal IDs")
    if not isinstance(manifest["inputs"], list) or not isinstance(manifest["outputs"], list) or not manifest["outputs"]:
        _fail("manifest inputs/outputs must be lists with nonempty outputs")
    _text(manifest["summary"], "summary")
    _scan(manifest["summary"].encode(), "summary")
    inputs = _validate_inputs(vault, manifest)
    outputs = _validate_outputs(vault, manifest, manifest_path.parent)
    ledger = _validate_ledger(vault, manifest, manifest_path.parent, inputs, outputs)
    baseline = _snapshot(vault)
    _lint(vault, "preflight")
    now = knowledge.now_utc()
    txid = "tx-" + uuid.uuid4().hex
    ctx = {"manifest": manifest, "manifest_path": manifest_path, "manifest_raw": raw,
           "manifest_sha256": digest_bytes(raw), "review": review, "review_path": review_path,
           "review_sha256": digest_bytes(review_raw), "evidence_path": evidence,
           "vault": vault, "agent": agent, "lock_id": lock_id, "lock_binding": binding, "ledger": ledger,
           "outputs": outputs, "baseline": baseline, "transaction": txid,
           "occurred_at": knowledge.iso_time(now), "event_id": f"evt-{knowledge.id_time(now)}-{txid[3:11]}"}
    ctx["files"] = _planned_files(ctx)
    if {name.casefold() for name in inputs["frozen"]} & {item["relative"].casefold() for item in ctx["files"]}:
        _fail("frozen inputs overlap publication outputs")
    _lint_overlay(ctx)
    return ctx


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _atomic_replace(target: Path, content: bytes, *, guard=None, create_only: bool = False) -> None:
    _absolute(target)
    if guard:
        guard()
    target.parent.mkdir(parents=True, exist_ok=True)
    _absolute(target.parent)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _absolute(target)
        if guard:
            guard()
        if create_only:
            # Atomic create-if-absent, including arrivals after the last check.
            os.link(name, target)
        else:
            os.replace(name, target)
        _sync_directory(target.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _create_resolution_output(target: Path, content: bytes, staging_dir: Path, *, guard) -> None:
    """Create a vault output atomically; temporary bytes stay in the task workspace."""
    staging_dir = _absolute(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    guard()
    fd, name = tempfile.mkstemp(prefix=".resolution-stage-", dir=str(staging_dir))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        guard()
        os.link(name, target)
        _sync_directory(target.parent)
    finally:
        # This file was created by this operation inside its task workspace.
        if os.path.exists(name):
            os.unlink(name)


def _journal_path(manifest_path: Path, txid: str) -> Path:
    return manifest_path.parent / ".curation-transactions" / (txid + ".json")


def _write_journal(path: Path, journal: dict) -> None:
    _atomic_replace(path, _json_bytes(journal))


def _artifact_name(txid: str, category: str, relative: str) -> str:
    return f"{txid}.{category}/{digest_bytes(relative.encode('utf-8'))}.bin"


def _check_evidence(ctx: dict) -> None:
    for path, expected, label in (
            (ctx["manifest_path"], ctx["manifest_sha256"], "manifest"),
            (ctx["review_path"], ctx["review_sha256"], "review"),
            (ctx["evidence_path"], ctx["review"]["evidence"]["sha256"], "review evidence"),
            (_safe_path(ctx["manifest_path"].parent, ctx["manifest"]["ledger_path"], label="ledger"),
             ctx["manifest"]["ledger_sha256"], "ledger")):
        if digest_file(path) != expected:
            _fail(f"{label} changed at publication boundary")


def _regenerate_log(ctx: dict, journal: dict, journal_path: Path, current: dict) -> bool:
    """Rebase the generated log onto the current event set; True when changed.

    The transaction's own event record comes from ctx, never from the vault,
    so every event is merged exactly once. The durable staged payload and its
    journal hash always move together, keeping the hash binding intact.
    """
    item = next((row for row in ctx["files"] if row["relative"] == "log.md"), None)
    if item is None:
        return False
    event_path, _ = _event_paths(ctx["event_id"], ctx["occurred_at"])
    merged = {name: data for name, data in current.items()
              if name != event_path and name.startswith("events/") and name.endswith(".json")
              and not Path(name).name.startswith(".")}
    content = _merge_log(merged, ctx["curation_event"])
    if content == item["content"]:
        return False
    item["content"] = content
    item["after"] = digest_bytes(content)
    record = next(row for row in journal["files"] if row["path"] == "log.md")
    record["after"] = item["after"]
    staged = _safe_path(journal_path.parent, record["staged"], label="staged payload")
    _atomic_replace(staged, content)
    return True


def _validate_event_intake(ctx: dict, current: dict | None = None) -> None:
    """Freeze preexisting events; tolerate only valid append-only intake.

    Every event present at the fenced boundary (plus this transaction's own
    staged event once applied) must survive byte-identical; a new entry must
    be a parseable event whose identity matches its canonical path. Mutation,
    deletion, or an invalid new identity remains a hard error and rolls back.
    """
    if current is None:
        current = _event_snapshot(ctx["vault"])
    expected = dict(ctx["event_snapshot"])
    event_path, _ = _event_paths(ctx["event_id"], ctx["occurred_at"])
    if event_path in current:
        event_output = next(item for item in ctx["files"] if item["relative"] == event_path)
        expected[event_path] = event_output["content"]
    for name, data in expected.items():
        if current.get(name) != data:
            _fail(f"event mutated or deleted during publication: {name}")
    for name in current.keys() - expected.keys():
        try:
            event = _decode_json(current[name])
        except CurationError:
            _fail(f"invalid new event during publication: {name}")
        event_id = event.get("event_id") if isinstance(event, dict) else None
        occurred = event.get("occurred_at") if isinstance(event, dict) else None
        if (not isinstance(event_id, str) or not ID.fullmatch(event_id)
                or not isinstance(occurred, str)
                or name != _event_paths(event_id, occurred)[0]):
            _fail(f"invalid new event identity during publication: {name}")


def _publish_guard(ctx: dict, item: dict) -> None:
    _validate_lock(ctx["vault"], ctx["lock_id"], ctx["agent"], ctx["lock_binding"])
    _check_evidence(ctx)
    if "payload" in item and digest_file(item["payload"]) != item["after"]:
        _fail(f"payload changed at write boundary: {item['relative']}")
    if _current(ctx["vault"], item["relative"]) != item["before"]:
        _fail(f"target preimage changed at write boundary: {item['relative']}")


def publish(manifest_path: Path, review_path: Path, lock_id: str, agent: str) -> dict:
    ctx = validate(manifest_path, review_path, lock_id, agent)
    txid, vault = ctx["transaction"], ctx["vault"]
    journal_path = _external(_journal_path(ctx["manifest_path"], txid), vault)
    if journal_path.exists():
        _fail("transaction already exists")
    _validate_lock(vault, lock_id, agent, ctx["lock_binding"])
    journal = {"schema_version": 1, "transaction": txid, "status": "planned", "vault": str(vault),
               "manifest": str(ctx["manifest_path"]), "manifest_sha256": ctx["manifest_sha256"],
               "lock_id": lock_id, "agent": agent, "event_id": ctx["event_id"],
               "occurred_at": ctx["occurred_at"], "files": []}
    for item in ctx["files"]:
        staged = _artifact_name(txid, "staged", item["relative"])
        preimage = _artifact_name(txid, "preimages", item["relative"]) if item["before"] is not None else None
        for name, data in ((staged, item["content"]), (preimage, item["preimage_content"])):
            if name is not None:
                _validate_lock(vault, lock_id, agent, ctx["lock_binding"])
                _atomic_replace(_external(journal_path.parent / name, vault), data, create_only=True)
        journal["files"].append({"path": item["relative"], "before": item["before"], "after": item["after"],
                                 "exists_before": item["before"] is not None, "preimage": preimage,
                                 "staged": staged, "state": "planned"})
    _atomic_replace(journal_path.with_name(txid + ".manifest.json"), ctx["manifest_raw"], create_only=True)
    _validate_lock(vault, lock_id, agent, ctx["lock_binding"])
    _write_journal(journal_path, journal)
    try:
        # Full read-set recheck after staging; per-file guards run immediately at replacement.
        _validate_inputs(vault, ctx["manifest"], current_event_id=ctx["event_id"])
        # Short shared writer fence: snapshot the event set and commit the
        # rebased generated log plus journal as one atomic unit. Cooperative
        # contributors (knowledge.write_event) wait bounded; the fence never
        # spans model calls, whole-vault lint, or output replacement.
        boundary = _snapshot(vault)
        if _baseline_changed(ctx["baseline"], boundary):
            _fail("vault baseline changed at transaction boundary")
        with event_write_lock.event_write_fence(vault):
            # A complete intake receipt can arrive during staging. Refresh the
            # generated log and its durable staged hash before replacing any
            # vault file; later intake is validated as append-only below.
            ctx["event_snapshot"] = {name: data for name, data in boundary.items()
                                     if name.startswith("events/") and name.endswith(".json")
                                     and not Path(name).name.startswith(".")}
            events = _event_snapshot(vault)
            _validate_event_intake(ctx, events)
            _regenerate_log(ctx, journal, journal_path, events)
            _write_journal(journal_path, journal)
            _validate_event_intake(ctx)
        for item in ctx["files"]:
            _publish_guard(ctx, item)
        for item, record in zip(ctx["files"], journal["files"]):
            staged = _safe_path(journal_path.parent, record["staged"], label="staged payload")
            content = _read(staged)
            if digest_bytes(content) != record["after"]:
                _fail("staged payload hash mismatch")
            _atomic_replace(item["target"], content, guard=lambda item=item: _publish_guard(ctx, item),
                            create_only=item["before"] is None)
            record["state"] = "replaced"
            journal["status"] = "applying"
            _validate_lock(vault, lock_id, agent, ctx["lock_binding"])
            _write_journal(journal_path, journal)
        _validate_inputs(vault, ctx["manifest"], current_event_id=ctx["event_id"])
        _check_evidence(ctx)
        for item in ctx["files"]:
            if _current(vault, item["relative"]) != item["after"]:
                _fail("published output changed before final lint")
        # Valid intake that arrived during apply is merged into the generated
        # log exactly once, under the same short fence, before the final lint.
        with event_write_lock.event_write_fence(vault):
            current_events = _event_snapshot(vault)
            _validate_event_intake(ctx, current_events)
            log_item = next((item for item in ctx["files"] if item["relative"] == "log.md"), None)
            if log_item is not None:
                written_after = log_item["after"]
                if _current(vault, "log.md") != written_after:
                    _fail("published output changed before final lint")
                if _regenerate_log(ctx, journal, journal_path, current_events):
                    def _log_guard(written_after=written_after):
                        _validate_lock(vault, lock_id, agent, ctx["lock_binding"])
                        _check_evidence(ctx)
                        if _current(vault, "log.md") != written_after:
                            _fail("published log changed at final log commit")
                    _atomic_replace(log_item["target"], log_item["content"], guard=_log_guard)
                    _write_journal(journal_path, journal)
            ctx["event_snapshot"] = current_events
        _lint(vault, "final")
        _validate_inputs(vault, ctx["manifest"], current_event_id=ctx["event_id"])
        _check_evidence(ctx)
        for item in ctx["files"]:
            if _current(vault, item["relative"]) != item["after"]:
                _fail("published output changed during final verification")
        expected = dict(ctx["baseline"])
        expected.update({item['relative']: item['content'] for item in ctx['files']})
        if _baseline_changed(expected, _snapshot(vault)):
            _fail('vault baseline changed during final verification')
        # Intake can complete during full final lint. Bind that final event set,
        # generated log, and committed journal while contributors briefly wait.
        with event_write_lock.event_write_fence(vault):
            events = _event_snapshot(vault)
            _validate_event_intake(ctx, events)
            log_item = next((item for item in ctx['files'] if item['relative'] == 'log.md'), None)
            if log_item is not None:
                prior = log_item['after']
                if _regenerate_log(ctx, journal, journal_path, events):
                    def final_log_guard():
                        _validate_lock(vault, lock_id, agent, ctx['lock_binding'])
                        _check_evidence(ctx)
                        if _current(vault, 'log.md') != prior:
                            _fail('published log changed at final commit')
                    _atomic_replace(log_item['target'], log_item['content'], guard=final_log_guard)
            _validate_lock(vault, lock_id, agent, ctx['lock_binding'])
            _check_evidence(ctx)
            journal['status'] = 'committed'
            _write_journal(journal_path, journal)
        return {"status": "PUBLISHED", "transaction": str(journal_path), "event_id": ctx["event_id"],
                "manifest_sha256": ctx["manifest_sha256"],
                "pages_changed": [item["relative"][5:] for item in ctx["files"] if item["relative"].startswith("wiki/")]}
    except Exception as exc:
        try:
            rollback(journal_path, lock_id, agent)
        except Exception as rollback_exc:
            raise CurationError(f"publish failed: {exc}; rollback blocked: {rollback_exc}; transaction: {journal_path}") from exc
        raise CurationError(f"publish failed; transaction rolled back: {exc}") from exc


def _load_transaction(transaction: Path, agent: str) -> tuple[dict, dict]:
    journal = _exact_keys(_json(transaction), {"schema_version", "transaction", "status", "vault", "manifest",
                          "manifest_sha256", "lock_id", "agent", "event_id", "occurred_at", "files"}, "transaction")
    if type(journal["schema_version"]) is not int or journal["schema_version"] != 1:
        _fail("invalid journal schema version")
    txid = journal["transaction"]
    if (not isinstance(txid, str) or not TX_ID.fullmatch(txid) or transaction.name != txid + ".json"
            or transaction.parent.name != ".curation-transactions"):
        _fail("invalid transaction ID/path")
    vault = _owned_vault(journal["vault"])
    if journal["agent"] != agent or not isinstance(journal["lock_id"], str) or not ID.fullmatch(journal["lock_id"]):
        _fail("invalid transaction agent/lock")
    if journal["status"] not in {"planned", "applying", "committed", "rolling_back", "rolled_back"}:
        _fail("invalid transaction status")
    _external(Path(_text(journal["manifest"], "manifest path")), vault)
    try:
        when = dt.datetime.fromisoformat(journal["occurred_at"].replace("Z", "+00:00"))
        if when.tzinfo is None or journal["occurred_at"] != knowledge.iso_time(when):
            _fail("noncanonical transaction time")
    except (ValueError, TypeError, AttributeError) as exc:
        raise CurationError("invalid transaction time") from exc
    if journal["event_id"] != f"evt-{knowledge.id_time(when)}-{txid[3:11]}":
        _fail("transaction event ID mismatch")
    manifest_raw = _read(_safe_path(transaction.parent, txid + ".manifest.json", label="archived manifest"))
    if digest_bytes(manifest_raw) != _sha_field(journal["manifest_sha256"], "journal manifest"):
        _fail("archived manifest hash mismatch")
    manifest = _exact_keys(_decode_json(manifest_raw), MANIFEST_KEYS, "archived manifest")
    _owned_vault(manifest["vault_path"])
    event_path, ledger_path = _event_paths(journal["event_id"], journal["occurred_at"])
    expected = {"wiki/index.md": None, "log.md": None, event_path: None, ledger_path: None}
    if not isinstance(manifest["outputs"], list):
        _fail("invalid archived manifest outputs")
    for row in manifest["outputs"]:
        _exact_keys(row, {"path", "payload_path", "sha256", "preimage_sha256"}, "archived output")
        name = _wiki_path(row["path"])
        if name.casefold() in {key.casefold() for key in expected}:
            _fail("duplicate archived output")
        expected[name] = row
    if not isinstance(journal["files"], list) or len(journal["files"]) != len(expected):
        _fail("journal output cardinality mismatch")
    records, seen = {}, set()
    for row in journal["files"]:
        _exact_keys(row, {"path", "before", "after", "exists_before", "preimage", "staged", "state"}, "journal file")
        name = _safe_relative(row["path"], label="journal target")
        if name not in expected or name.casefold() in seen:
            _fail("foreign or duplicate journal target")
        seen.add(name.casefold())
        _sha_field(row["after"], "journal after")
        if row["before"] is not None:
            _sha_field(row["before"], "journal before")
        if type(row["exists_before"]) is not bool or row["exists_before"] != (row["before"] is not None):
            _fail("invalid journal preimage state")
        if row["state"] not in {"planned", "replaced", "restored"}:
            _fail("invalid journal file state")
        original = expected[name]
        if original is not None and (row["after"] != original["sha256"] or row["before"] != original["preimage_sha256"]):
            _fail("journal target hashes do not match reviewed manifest")
        if name in (event_path, ledger_path) and row["before"] is not None:
            _fail("transaction event/governance must be new")
        required_preimage = _artifact_name(txid, "preimages", name) if row["before"] is not None else None
        if row["preimage"] != required_preimage or row["staged"] != _artifact_name(txid, "staged", name):
            _fail("foreign transaction preimage/staged path")
        preimage = None
        if required_preimage is not None:
            preimage = _read(_safe_path(transaction.parent, required_preimage, label="preimage"))
            if digest_bytes(preimage) != row["before"]:
                _fail("tampered rollback preimage")
        staged = _read(_safe_path(transaction.parent, row["staged"], label="staged payload"))
        if digest_bytes(staged) != row["after"]:
            _fail("tampered rollback staged payload")
        current = _current(vault, name)
        if current not in (row["before"], row["after"]):
            _fail(f"refusing rollback over foreign or concurrent edit: {name}")
        records[name] = {"row": row, "preimage": preimage, "staged": staged}
    event = _decode_json(records[event_path]["staged"])
    if (not isinstance(event, dict) or event.get("event_id") != journal["event_id"]
            or event.get("action") != "record-curation" or event.get("agent") != agent
            or event.get("pages_changed") != [r["path"][5:] for r in manifest["outputs"]] + ["index.md"]):
        _fail("invalid staged transaction event")
    # Delete our event before its pages, and refuse removal if a foreign event needs them.
    deleted_pages = {name[5:].casefold() for name, item in records.items()
                     if name.startswith("wiki/") and item["row"]["before"] is None}
    for path in _tree_files(vault, "events"):
        if path.suffix != ".json" or path.relative_to(vault).as_posix() == event_path:
            continue
        other = _json(path)
        if not isinstance(other, dict) or not isinstance(other.get("pages_changed"), list):
            _fail("invalid foreign event")
        if any(_safe_relative(p, label="event page").casefold() in deleted_pages for p in other["pages_changed"]):
            _fail("rollback would break a foreign event page link")
    return journal, records


def rollback(transaction: Path, lock_id: str, agent: str) -> dict:
    vault = _owned_vault(DEFAULT_VAULT)
    transaction = _external(transaction, vault)
    binding = _validate_lock(vault, lock_id, agent)
    journal_hash = digest_file(transaction)
    journal, records = _load_transaction(transaction, agent)
    if digest_file(transaction) != journal_hash:
        _fail("transaction journal changed during validation")
    event_path, ledger_path = _event_paths(journal["event_id"], journal["occurred_at"])
    order = [event_path, ledger_path] + [name for name in reversed(list(records)) if name not in (event_path, ledger_path)]

    def guard(name):
        _validate_lock(vault, lock_id, agent, binding)
        if digest_file(transaction) != journal_hash:
            _fail("transaction journal changed during rollback")
        row = records[name]["row"]
        if row["before"] is not None:
            if digest_file(_safe_path(transaction.parent, row["preimage"], label="preimage")) != row["before"]:
                _fail("rollback preimage changed")
        if _current(vault, name) != row["after"]:
            _fail(f"foreign or concurrent rollback edit: {name}")

    for name in order:
        row = records[name]["row"]
        current = _current(vault, name)
        if current == row["before"]:
            continue
        if current != row["after"]:
            _fail(f"foreign or concurrent rollback edit: {name}")
        guard(name)
        target = _safe_path(vault, name, label="rollback target")
        if row["before"] is None:
            guard(name)
            target.unlink()
            _sync_directory(target.parent)
        else:
            _atomic_replace(target, records[name]["preimage"], guard=lambda name=name: guard(name))
        row["state"] = "restored"
        journal["status"] = "rolling_back"
        _validate_lock(vault, lock_id, agent, binding)
        if digest_file(transaction) != journal_hash:
            _fail("transaction journal changed during rollback")
        _write_journal(transaction, journal)
        journal_hash = digest_file(transaction)
    for name, item in records.items():
        if _current(vault, name) != item["row"]["before"]:
            _fail("rollback postimage mismatch")
    _validate_lock(vault, lock_id, agent, binding)
    if digest_file(transaction) != journal_hash:
        _fail("transaction journal changed during rollback")
    journal["status"] = "rolled_back"
    _write_journal(transaction, journal)
    return {"status": "ROLLED_BACK", "transaction": str(transaction)}


def _validate_resolution_review(review_path: Path, resolution_digest: str, agent: str, vault: Path) -> tuple[dict, Path]:
    review = _exact_keys(_json(review_path), RESOLUTION_REVIEW_KEYS, "resolution review")
    if type(review["schema_version"]) is not int or review["schema_version"] != 1 or review["verdict"] != "PASS":
        _fail("unsupported resolution review schema or verdict")
    if _sha_field(review["resolution_sha256"], "resolution review") != resolution_digest:
        _fail("resolution review is not bound to the exact resolution content")
    reviewer = _exact_keys(review["reviewer"], {"id", "lineage"}, "resolution reviewer")
    maker = _text(review["maker"], "resolution maker")
    identity = _text(reviewer["id"], "resolution reviewer ID")
    _text(reviewer["lineage"], "resolution reviewer lineage")
    if identity.casefold() in {maker.casefold(), agent.casefold()}:
        _fail("resolution reviewer identity must be distinct from maker and agent")
    evidence = _exact_keys(review["evidence"], {"path", "sha256"}, "resolution review evidence")
    evidence_name = _text(evidence["path"], "resolution review evidence path")
    evidence_path = _safe_path(vault, evidence_name, label="resolution review evidence")
    evidence_hash = _sha_field(evidence["sha256"], "resolution review evidence")
    evidence_errors = knowledge._verify_resolution_evidence(
        vault, evidence_name, evidence_hash, "resolution review evidence", str(review_path)
    )
    if evidence_errors:
        _fail("; ".join(evidence_errors))
    return review, evidence_path


def _proposal_ledger_pairs(vault: Path) -> dict[str, set[tuple[str, str]]]:
    """Exact (curation event ID, disposition) pairs each accounted proposal gathered."""
    state = knowledge.collect_disposition_state(vault)
    return {proposal_id: set(rows) for proposal_id, rows in state["pairs"]["proposals"].items()}


def _validate_supersedes(
    supersedes: object,
    proposal_id: str,
    pairs: dict[str, set[tuple[str, str]]],
) -> list[dict]:
    if not isinstance(supersedes, list) or not supersedes:
        _fail("resolution supersedes must be a non-empty list of event/disposition pairs")
    listed: set[tuple[str, str]] = set()
    rows: list[dict] = []
    for row in supersedes:
        _exact_keys(row, {"event_id", "disposition"}, "resolution supersedes row")
        event_id = _text(row["event_id"], "superseded event ID")
        disposition = row["disposition"]
        if not isinstance(disposition, str) or disposition not in DISPOSITIONS:
            _fail("invalid superseded disposition")
        pair = (event_id, disposition)
        if pair in listed:
            _fail(f"ambiguous duplicate supersedes pair {event_id}/{disposition}")
        listed.add(pair)
        rows.append({"event_id": event_id, "disposition": disposition})
    known = pairs.get(proposal_id, set())
    if not known:
        _fail("resolution proposal has no accounted curation disposition pairs")
    if len(known) < 2:
        _fail("resolution requires at least two curation disposition pairs to reconcile")
    if listed != known:
        missing = sorted(known - listed)
        extra = sorted(listed - known)
        if missing:
            _fail(f"partial resolution supersedes; missing pairs: {missing}")
        if extra:
            _fail(f"stale or mismatched supersedes; unexpected pairs: {extra}")
    return rows


def _validate_resolution_document(raw: bytes, vault: Path) -> dict:
    document = _exact_keys(_decode_json(raw), RESOLUTION_KEYS, "resolution")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        _fail("unsupported resolution schema")
    _owned_vault(document["vault_path"])
    resolution_id = _text(document["resolution_id"], "resolution ID")
    if not knowledge.RESOLUTION_ID_PATTERN.fullmatch(resolution_id):
        _fail("invalid resolution ID")
    proposal_id = _text(document["proposal_id"], "resolution proposal ID")
    if not knowledge.PROPOSAL_ID_PATTERN.fullmatch(proposal_id):
        _fail("invalid resolution proposal ID")
    if not isinstance(document["effective_disposition"], str) or document["effective_disposition"] not in DISPOSITIONS:
        _fail("invalid effective disposition")
    reason = _text(document["reason"], "resolution reason")
    _scan(reason.encode("utf-8"), "resolution reason")
    evidence = _exact_keys(document["evidence"], {"path", "sha256"}, "resolution evidence")
    evidence_name = _text(evidence["path"], "resolution evidence path")
    _safe_path(vault, evidence_name, label="resolution evidence")
    evidence_errors = knowledge._verify_resolution_evidence(
        vault, evidence_name, _sha_field(evidence["sha256"], "resolution evidence"),
        "resolution evidence", "resolution document"
    )
    if evidence_errors:
        _fail("; ".join(evidence_errors))
    return document


def _existing_resolutions(vault: Path) -> dict[str, dict]:
    root = _absolute(vault / "governance" / "resolutions")
    found: dict[str, dict] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*.json")):
        label = path.relative_to(vault).as_posix()
        if knowledge.path_has_reparse_component(vault, path):
            _fail(f"symlinked resolution record: {label}")
        record = _json(_safe_path(vault, label, label="resolution record"))
        if not isinstance(record, dict) or not isinstance(record.get("proposal_id"), str):
            _fail(f"malformed resolution record: {label}")
        proposal_id = record["proposal_id"]
        if proposal_id in found:
            _fail(f"duplicate resolution record for proposal: {proposal_id}")
        found[proposal_id] = record
    return found


def validate_resolution(resolution_path: Path, review_path: Path, lock_id: str, agent: str) -> dict:
    vault = _owned_vault(DEFAULT_VAULT)
    resolution_path = _external(resolution_path, vault)
    review_path = _external(review_path, vault)
    raw = _read(resolution_path)
    document = _validate_resolution_document(raw, vault)
    binding = _validate_lock(vault, lock_id, agent)
    resolution_digest = knowledge.resolution_binding_sha256(
        {key: document[key] for key in knowledge.RESOLUTION_BINDING_FIELDS}
    )
    review_raw = _read(review_path)
    review_file_sha256 = digest_bytes(review_raw)
    review, review_evidence = _validate_resolution_review(review_path, resolution_digest, agent, vault)
    if _decode_json(review_raw) != review:
        _fail("resolution review changed while being validated")
    record_review = {
        **review,
        "evidence": {"path": review["evidence"]["path"], "sha256": review["evidence"]["sha256"]},
    }
    pairs = _proposal_ledger_pairs(vault)
    proposal_id = document["proposal_id"]
    supersedes = _validate_supersedes(document["supersedes"], proposal_id, pairs)
    existing = _existing_resolutions(vault)
    if proposal_id in existing:
        _fail(f"duplicate resolution for proposal {proposal_id}; already resolved by "
              f"{existing[proposal_id].get('resolution_id')}")
    if document["resolution_id"] in {
        str(record.get("resolution_id")) for record in existing.values()
    }:
        _fail("duplicate resolution ID")
    evidence = document["evidence"]
    evidence_path = _safe_path(vault, evidence["path"], label="resolution evidence")
    if digest_file(evidence_path) != _sha_field(evidence["sha256"], "resolution evidence"):
        _fail("resolution evidence hash mismatch")
    _lint(vault, "resolution preflight")
    now = knowledge.now_utc()
    resolution_id = document["resolution_id"]
    event_id = f"evt-{knowledge.id_time(now)}-{uuid.uuid4().hex[:8]}"
    record = {
        "schema_version": 1,
        "resolution_id": resolution_id,
        "proposal_id": proposal_id,
        "supersedes": supersedes,
        "effective_disposition": document["effective_disposition"],
        "reason": document["reason"],
        "evidence": {"path": evidence["path"], "sha256": evidence["sha256"]},
        "review": record_review,
        "review_sha256": knowledge.canonical_json_sha256(record_review),
        "resolution_sha256": resolution_digest,
        "recorded_at": knowledge.iso_time(now),
        "agent": agent,
        "event_id": event_id,
    }
    event = {
        "event_id": event_id,
        "occurred_at": knowledge.iso_time(now),
        "agent": agent,
        "action": "resolve-disposition",
        "proposal_ids": [proposal_id],
        "pages_changed": [],
        "source_ids": [],
        "summary": f"Resolved curation dispositions for {proposal_id} via {resolution_id}",
        "resolution_id": resolution_id,
    }
    event_path = _event_paths(event_id, knowledge.iso_time(now))[0]
    record_path = f"governance/resolutions/{resolution_id}.json"
    return {
        "document": document,
        "raw": raw,
        "resolution_digest": resolution_digest,
        "review": record_review,
        "review_raw": review_raw,
        "resolution_file_sha256": digest_bytes(raw),
        "review_file_sha256": review_file_sha256,
        "review_evidence": review_evidence,
        "evidence_path": evidence_path,
        "vault": vault,
        "agent": agent,
        "lock_id": lock_id,
        "lock_binding": binding,
        "record": record,
        "event": event,
        "event_path": event_path,
        "record_path": record_path,
        "resolution_id": resolution_id,
        "event_id": event_id,
        "pairs": pairs,
    }


def _recover_resolution_journal(journal_path: Path, vault: Path, resolution_id: str) -> None:
    if not journal_path.exists():
        return
    journal = _json(journal_path)
    if not isinstance(journal, dict) or journal.get("schema_version") != 1:
        _fail(f"malformed resolution journal: {journal_path.name}")
    if journal.get("resolution_id") != resolution_id:
        _fail(f"resolution journal does not match resolution {resolution_id}")
    status = journal.get("status")
    if status == "committed":
        _fail("duplicate resolution: already committed by a prior attempt")
    if status == "rolled_back":
        return
    if status not in {"planned", "applying", "archiving"}:
        _fail(f"invalid resolution journal status: {status}")
    expected_record = f"governance/resolutions/{resolution_id}.json"
    git_root = knowledge.find_git_root(vault)
    archive_relative = journal.get("archive_dir")
    if (not isinstance(archive_relative, str)
            or not re.fullmatch(r"governance/_do-usuniecia-\d{8}/" + re.escape(resolution_id), archive_relative)):
        _fail("invalid resolution archive location")
    archive_dir = _safe_path(vault, archive_relative + "/record.json",
                             label="resolution archive", allow_missing=True).parent
    to_archive: list[tuple[Path, Path, str]] = []
    live_targets: list[Path] = []
    for path_key, hash_key in (("record_path", "record_sha256"), ("event_path", "event_sha256")):
        rel = journal.get(path_key)
        expected_hash = journal.get(hash_key)
        if not isinstance(rel, str) or not isinstance(expected_hash, str) or not HEX64.fullmatch(expected_hash):
            _fail(f"malformed resolution journal entry: {journal_path.name}")
        if not (rel.startswith("governance/resolutions/") or rel.startswith("events/")):
            _fail(f"resolution journal target outside allowed directories: {rel}")
        if path_key == "record_path" and rel != expected_record:
            _fail(f"resolution journal record path mismatch: {rel}")
        if path_key == "event_path" and not re.match(
            r"^events/\d{4}/\d{2}/evt-[A-Za-z0-9][A-Za-z0-9._-]{0,240}\.json$", rel
        ):
            _fail(f"resolution journal event path mismatch: {rel}")
        path = _safe_path(vault, rel, label="resolution journal target", allow_missing=True)
        if path.exists():
            if git_root is None:
                _fail(f"cannot prove ownership of resolution output without Git: {rel}")
            repository_path = path.relative_to(git_root).as_posix()
            probe = knowledge.run_git(git_root, ["ls-files", "--error-unmatch", "--", repository_path], check=False)
            if probe.returncode == 0:
                _fail(f"refusing to archive tracked resolution output: {rel}")
            if probe.returncode != 1:
                _fail(f"cannot prove resolution output is untracked: {rel} (git exit {probe.returncode})")
            live_targets.append(path)
        archived = archive_dir / ("record.json" if path_key == "record_path" else "event.json")
        if archived.exists():
            if digest_file(archived) != expected_hash or path.exists():
                _fail(f"resolution archive conflict: {rel}")
            continue
        if path.exists():
            if digest_file(path) != expected_hash:
                _fail(f"resolution journal target was modified: {rel}")
            if path_key == "event_path":
                event = _json(path)
                if (not isinstance(event, dict) or event.get("action") != "resolve-disposition"
                        or event.get("resolution_id") != resolution_id):
                    _fail(f"resolution journal event does not match resolution: {rel}")
            else:
                record = _json(path)
                if (not isinstance(record, dict) or record.get("resolution_id") != resolution_id
                        or record.get("event_id") != Path(journal["event_path"]).stem):
                    _fail(f"resolution journal record does not match resolution: {rel}")
            to_archive.append((path, archived, rel))
    record_target = _safe_path(vault, expected_record, label="resolution journal record", allow_missing=True)
    if record_target.exists():
        record = _json(record_target)
        if not isinstance(record, dict):
            _fail("malformed resolution journal record")
        event_id = record.get("event_id")
        recorded_at = record.get("recorded_at")
        if (not isinstance(event_id, str) or not knowledge.EVENT_ID_PATTERN.fullmatch(event_id)
                or not isinstance(recorded_at, str) or knowledge.parse_recorded_time(recorded_at) is None):
            _fail("resolution journal record has no valid event binding")
        canonical_event = _event_paths(event_id, recorded_at)[0]
        if journal["event_path"] != canonical_event:
            _fail("resolution journal event path does not match the record")
        if any((vault / "events").glob(f"*/*/{event_id}.json")) and len(live_targets) != 2:
            _fail("resolution journal cannot archive a record with an existing event")
    if len(live_targets) == 2:
        # A complete pair may be a valid prior publication, including a crash
        # after both creates. Never displace it on the word of an external journal.
        _lint(vault, "complete resolution recovery")
        journal["status"] = "committed"
        _write_journal(journal_path, journal)
        _fail("duplicate resolution: complete record and event already exist")
    if to_archive:
        journal["status"] = "archiving"
        _write_journal(journal_path, journal)
        archive_dir.mkdir(parents=True, exist_ok=True)
        for path, archived, rel in to_archive:
            if archived.exists():
                _fail(f"resolution archive target already exists: {rel}")
            os.rename(path, archived)
            _sync_directory(path.parent)
            _sync_directory(archived.parent)
    journal["status"] = "rolled_back"
    _write_journal(journal_path, journal)


def resolve_disposition(resolution_path: Path, review_path: Path, lock_id: str, agent: str) -> dict:
    vault = _owned_vault(DEFAULT_VAULT)
    resolution_path = _external(resolution_path, vault)
    raw = _read(resolution_path)
    document = _validate_resolution_document(raw, vault)
    journal_path = _external(
        resolution_path.parent / f".resolution-tx-{document['resolution_id']}.json", vault
    )
    _validate_lock(vault, lock_id, agent)
    _recover_resolution_journal(journal_path, vault, document["resolution_id"])
    ctx = validate_resolution(resolution_path, review_path, lock_id, agent)
    record_bytes = _json_bytes(ctx["record"])
    event_bytes = _json_bytes(ctx["event"])
    record_target = _safe_path(vault, ctx["record_path"], label="resolution record", allow_missing=True)
    event_target = _safe_path(vault, ctx["event_path"], label="resolution event", allow_missing=True)
    if record_target.exists() or event_target.exists():
        _fail("resolution audit record already exists")
    _validate_lock(vault, ctx["lock_id"], ctx["agent"], ctx["lock_binding"])
    journal = {
        "schema_version": 1,
        "resolution_id": ctx["resolution_id"],
        "status": "planned",
        "vault": str(vault),
        "record_path": ctx["record_path"],
        "event_path": ctx["event_path"],
        "record_sha256": digest_bytes(record_bytes),
        "event_sha256": digest_bytes(event_bytes),
        "archive_dir": f"governance/_do-usuniecia-{dt.datetime.now(dt.timezone.utc):%Y%m%d}/{ctx['resolution_id']}",
    }
    _write_journal(journal_path, journal)

    def guard() -> None:
        _validate_lock(vault, ctx["lock_id"], ctx["agent"], ctx["lock_binding"])
        if digest_file(resolution_path) != ctx["resolution_file_sha256"]:
            _fail("resolution document changed at write boundary")
        if digest_file(ctx["evidence_path"]) != ctx["document"]["evidence"]["sha256"]:
            _fail("resolution evidence changed at write boundary")
        if digest_file(review_path) != ctx["review_file_sha256"]:
            _fail("resolution review changed at write boundary")
        if digest_file(ctx["review_evidence"]) != ctx["review"]["evidence"]["sha256"]:
            _fail("resolution review evidence changed at write boundary")

    created: list[tuple[Path, bytes]] = []
    try:
        guard()
        _create_resolution_output(record_target, record_bytes, resolution_path.parent, guard=guard)
        created.append((record_target, record_bytes))
        journal["status"] = "applying"
        _write_journal(journal_path, journal)
        guard()
        _create_resolution_output(event_target, event_bytes, resolution_path.parent, guard=guard)
        created.append((event_target, event_bytes))
        journal["status"] = "applying"
        _write_journal(journal_path, journal)
        guard()
        _lint(vault, "resolution postflight")
        for path, expected in created:
            if _read(path) != expected:
                _fail("resolution audit record changed before commit")
        _validate_lock(vault, ctx["lock_id"], ctx["agent"], ctx["lock_binding"])
        journal["status"] = "committed"
        _write_journal(journal_path, journal)
    except Exception as exc:
        try:
            _validate_lock(vault, ctx["lock_id"], ctx["agent"], ctx["lock_binding"])
            _recover_resolution_journal(journal_path, vault, ctx["resolution_id"])
        except Exception as recovery_exc:
            raise CurationError(
                f"resolve-disposition failed: {exc}; partial outputs retained for recovery: {recovery_exc}"
            ) from exc
        raise CurationError(f"resolve-disposition failed; outputs archived without deletion: {exc}") from exc
    return {
        "status": "RESOLVED",
        "resolution_id": ctx["resolution_id"],
        "event_id": ctx["event_id"],
        "proposal_id": ctx["record"]["proposal_id"],
        "effective_disposition": ctx["record"]["effective_disposition"],
        "record_path": str(record_target),
        "event_path": str(event_target),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Owner-authorized transactional Hub publisher (advisory curator lock)")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("validate", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--review", type=Path, required=True)
        command.add_argument("--lock-id", required=True)
        command.add_argument("--agent", required=True)
    command = commands.add_parser("rollback")
    command.add_argument("--transaction", type=Path, required=True)
    command.add_argument("--lock-id", required=True)
    command.add_argument("--agent", required=True)
    command = commands.add_parser(
        "resolve-disposition",
        help="Owner-only append-only reconciliation of duplicated curation dispositions",
    )
    command.add_argument("--resolution", type=Path, required=True)
    command.add_argument("--review", type=Path, required=True)
    command.add_argument("--lock-id", required=True)
    command.add_argument("--agent", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "validate":
            ctx = validate(args.manifest, args.review, args.lock_id, args.agent)
            result = {"status": "VALID", "manifest_sha256": ctx["manifest_sha256"],
                      "outputs": len(ctx["outputs"]), "staged_lint": "PASS"}
        elif args.action == "publish":
            result = publish(args.manifest, args.review, args.lock_id, args.agent)
        elif args.action == "resolve-disposition":
            result = resolve_disposition(args.resolution, args.review, args.lock_id, args.agent)
        else:
            result = rollback(args.transaction, args.lock_id, args.agent)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (CurationError, knowledge.GatewayError, OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
