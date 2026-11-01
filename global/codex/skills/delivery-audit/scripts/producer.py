"""Metadata-only Hub closeout producer for the realization audit."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import audit_core
import completion

FLEET_RUN_LEDGER = Path.home() / ".agents" / "agent-fleet" / "runs"
INSTALLED_AUDIT_CLI = Path.home() / ".agents" / "skills" / "delivery-audit" / "scripts" / "audit.py"
AUDIT_STATE_DIR = Path.home() / ".agents" / "audits" / "delivery-audit"
ENQUEUE_TIMEOUT_SECONDS = 8
MAX_LEDGER_RECORD_BYTES = 1024 * 1024
_CHECKPOINT = re.compile(r"[A-Za-z0-9_.:-]{1,512}\Z", re.ASCII)
_SUCCESS_TERMINALS = frozenset(("success", "succeeded", "completed", "complete", "stop",
                                "end_turn", "turn_complete", "done"))
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _skip(code: str) -> dict:
    status = "SKIPPED_UNCLASSIFIED" if code == "UNCLASSIFIED" else "SKIPPED"
    if code == "SKIPPED_AMBIGUOUS":
        status = "SKIPPED_AMBIGUOUS"
    return {"status": status, "code": code}


def _hub_checkpoint_key(agent: str, project: str, session: str, checkpoint: str) -> str:
    body = json.dumps([agent, project, session, checkpoint], ensure_ascii=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _safe_checkpoint(value) -> bool:
    return (type(value) is str and _CHECKPOINT.fullmatch(value) is not None
            and ".." not in value)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = Path(path).lstat()
    except OSError:
        return True
    return Path(path).is_symlink() or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _read_run_record(run_id: str, ledger_root: Path) -> dict | None:
    """Read one exact canonical Fleet record; discard every non-allowlisted field."""
    try:
        audit_core._identifier(run_id)
        root = Path(ledger_root)
        if not root.is_absolute() or _is_link_or_reparse(root) or not root.is_dir():
            return None
        cursor = root
        while cursor != cursor.parent:
            if _is_link_or_reparse(cursor):
                return None
            cursor = cursor.parent
        resolved_root = root.resolve(strict=True)
        path = root / (run_id + ".json")
        if _is_link_or_reparse(path) or not path.is_file():
            return None
        resolved_path = path.resolve(strict=True)
        if not resolved_path.is_relative_to(resolved_root) or path.stat().st_size > MAX_LEDGER_RECORD_BYTES:
            return None
        raw = path.read_bytes()
        if len(raw) > MAX_LEDGER_RECORD_BYTES:
            return None
        record = json.loads(raw, object_pairs_hook=completion._object)
        if type(record) is not dict:
            return None
        allowed = (
            "run_id", "agent", "model", "model_provider_id", "run_status", "exit_code",
            "protocol_terminal_completed", "provider_terminal_reason", "runtime_attestation",
            "model_identity", "input_tokens", "cached_input_tokens", "output_tokens",
            "tool_calls", "token_usage", "execution_workspace", "workspace", "output_dir",
        )
        return {key: record.get(key) for key in allowed}
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError,
            json.JSONDecodeError):
        return None


def _identity_verified(record: dict) -> bool:
    identity = record.get("model_identity")
    runtime = record.get("runtime_attestation")
    if type(identity) is not dict or type(runtime) is not dict or runtime.get("status") != "VERIFIED":
        return False
    if identity.get("status") == "verified":
        return True
    if (record.get("agent") == "codex" and identity.get("status") == "not-applicable"
            and identity.get("expected_model") == record.get("model")):
        return True
    # Keep todo_fleet's protected-config exception for Kimi stream-json output.
    return (
        record.get("agent") == "kimi" and identity.get("status") == "not-applicable"
        and isinstance(identity.get("expected_model"), str)
        and bool(identity.get("expected_model"))
        and identity.get("expected_model") == record.get("model")
        and not identity.get("observed_models")
        and not identity.get("observed_runtime_models")
        and record.get("model_provider_id") == "moonshot"
    )


def _terminal_verified(record: dict) -> bool:
    terminal = record.get("provider_terminal_reason")
    runtime = record.get("runtime_attestation")
    return (
        record.get("run_status") == "COMPLETED"
        and record.get("exit_code") == 0
        and record.get("protocol_terminal_completed") is True
        and type(terminal) is str and terminal.casefold() in _SUCCESS_TERMINALS
        and type(runtime) is dict and runtime.get("status") == "VERIFIED"
    )


def _status(record: dict | None) -> str:
    value = record.get("run_status") if type(record) is dict else None
    return {"COMPLETED": "completed", "PARTIAL": "partial", "FAILED": "failed",
            "BLOCKED": "blocked", "INTERRUPTED": "blocked"}.get(value, "unknown")


def _safe_label(value, prefix="id-") -> str:
    if type(value) is str:
        try:
            return audit_core._identifier(value)
        except ValueError:
            pass
    if value is None:
        return "unknown"
    return prefix + hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:32]


def _token_value(record, key):
    direct = record.get(key)
    usage = record.get("token_usage")
    if direct is None and type(usage) is dict:
        direct = usage.get(key)
    if type(direct) is int and 0 <= direct <= audit_core.MAX_INTEGER:
        return direct
    return None


def _record_proves_run(run: dict, record: dict | None, workspace_id: str | None) -> bool:
    if record is None:
        return False
    output_sha = run.get("output_dir_sha256")
    workspace_sha = run.get("workspace_sha256")
    role = run.get("role")
    scheduler_role = run.get("scheduler_role")
    role_binding_ok = (
        (scheduler_role == "maker" and role in {"maker", "scout"})
        or (scheduler_role in {"reviewer", "autopsy"} and role == "reviewer")
    )
    return (
        role_binding_ok
        and record.get("run_id") == run.get("run_id")
        and type(record.get("agent")) is str and bool(record["agent"])
        and type(record.get("model")) is str and bool(record["model"])
        and type(record.get("model_provider_id")) is str and bool(record["model_provider_id"])
        and type(output_sha) is str and completion._HEX.fullmatch(output_sha) is not None
        and workspace_sha == workspace_id
        and completion._path_hash(record.get("output_dir")) == output_sha
        and completion._path_hash(record.get("execution_workspace")) == workspace_sha
        and (record.get("workspace") is None
             or completion._path_hash(record.get("workspace")) == workspace_sha)
        and _terminal_verified(record)
        and _identity_verified(record)
    )


def _verify_native_runs(candidate_runs: list, *, ledger_root: Path = FLEET_RUN_LEDGER,
                        catalog_path=None, workspace_id=None) -> list:
    """Create packet run rows from exact local Fleet receipts, never their verified flag."""
    result = []
    seen = set()
    for source in candidate_runs:
        if type(source) is not dict or set(source) != {
            "run_id", "node_ref", "role", "scheduler_role", "output_dir_sha256", "workspace_sha256",
        }:
            continue
        run_id = source.get("run_id")
        try:
            audit_core._identifier(run_id)
            audit_core._identifier(source.get("node_ref"))
        except (ValueError, TypeError):
            continue
        role = source.get("role")
        scheduler_role = source.get("scheduler_role")
        if (role not in {"maker", "reviewer", "scout"}
                or scheduler_role not in {"maker", "reviewer", "autopsy"}
                or run_id in seen):
            continue
        seen.add(run_id)
        record = _read_run_record(run_id, ledger_root)
        agent = _safe_label(record.get("agent") if record else None)
        model = _safe_label(record.get("model") if record else None, "model-")
        provider = _safe_label(record.get("model_provider_id") if record else None, "provider-")
        tier = completion._tier(record.get("agent"), record.get("model"), catalog_path) if record else "unknown"
        if tier not in {"D", "M", "K", "W"}:
            tier = "unknown"
        tokens = {key: _token_value(record, key) if record else None
                  for key in ("input_tokens", "cached_input_tokens", "output_tokens", "tool_calls")}
        if (tokens["input_tokens"] is not None and tokens["cached_input_tokens"] is not None
                and tokens["cached_input_tokens"] > tokens["input_tokens"]):
            tokens["cached_input_tokens"] = None
        result.append({
            "run_id": run_id, "actor_id": agent, "model_id": model, "provider": provider,
            "tier": tier, "role": role, "status": _status(record),
            "verified": _record_proves_run(source, record, workspace_id), **tokens,
        })
    return result


def _packet(candidate, outcome, *, session_id, ledger_root, catalog_path=None):
    runs = _verify_native_runs(candidate["runs"], ledger_root=ledger_root,
                               catalog_path=catalog_path,
                               workspace_id=candidate.get("workspace_id"))
    requirements = candidate["requirements"]
    metrics = dict(candidate["metrics"])
    uat_required, uat_status = metrics.get("uat_required"), metrics.get("uat_status", "unknown")
    complete_checks = bool(requirements) and all(
        req.get("status") == "pass" and bool(req.get("evidence_refs")) for req in requirements)
    uat_known = uat_required is False or (uat_required is True and uat_status == "pass")
    native_runs_complete = bool(runs) and all(
        run.get("verified") is True and run.get("status") == "completed" for run in runs)
    metrics["evidence_complete"] = (
        True if complete_checks and uat_known and native_runs_complete else None
    )
    packet = {
        "schema_version": 1, "task_id": candidate["task_id"],
        "session_id": session_id,
        "project_slug": candidate["project_slug"], "harness": candidate["harness"],
        "task_kind": candidate["task_kind"], "task_family": candidate["task_family"],
        "outcome": outcome, "workspace_id": candidate["workspace_id"],
        "requirements": requirements, "runs": runs, "metrics": metrics,
        "delegation": {"parent_tier": candidate["parent_tier"],
                       "eligible": candidate["eligible"], "exception": candidate["exception"],
                       "planned_tasks": candidate["planned_tasks"]},
        "observations": candidate["observations"],
        "evidence_refs": candidate["evidence_refs"],
    }
    return audit_core.validate_packet(packet)


def _production_enqueue(packet: dict) -> dict:
    """Call only the fixed installed CLI; it persists and starts work asynchronously."""
    packet = audit_core.validate_packet(packet)
    if not audit_core.should_audit(packet):
        return {"status": "SKIPPED"}
    if (os.name != "nt" or _is_link_or_reparse(INSTALLED_AUDIT_CLI)
            or not INSTALLED_AUDIT_CLI.is_file()):
        return {"status": "ENQUEUE_FAILED", "code": "INSTALLED_CLI_UNAVAILABLE"}
    if _is_link_or_reparse(AUDIT_STATE_DIR) or not AUDIT_STATE_DIR.is_dir():
        return {"status": "ENQUEUE_FAILED", "code": "AUDIT_STATE_UNAVAILABLE"}
    argv = [sys.executable, "-B", str(INSTALLED_AUDIT_CLI), "enqueue",
            "--packet", "-", "--state-dir", str(AUDIT_STATE_DIR)]
    try:
        response = subprocess.run(
            argv, cwd=str(AUDIT_STATE_DIR), input=audit_core._json_bytes(packet),
            capture_output=True, timeout=ENQUEUE_TIMEOUT_SECONDS, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"status": "ENQUEUE_OUTCOME_UNKNOWN", "code": "ENQUEUE_TIMEOUT"}
    except (OSError, subprocess.SubprocessError):
        return {"status": "ENQUEUE_FAILED", "code": "ENQUEUE_PROCESS_FAILED"}
    if response.returncode != 0 or len(response.stdout) > 4096:
        return {"status": "ENQUEUE_FAILED", "code": "ENQUEUE_REJECTED"}
    try:
        result = json.loads(response.stdout.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        return {"status": "ENQUEUE_OUTCOME_UNKNOWN", "code": "ENQUEUE_RECEIPT_INVALID"}
    status = result.get("status") if type(result) is dict else None
    audit_id = result.get("audit_id") if type(result) is dict else None
    expected = audit_core.audit_id(packet)
    if status in ("QUEUED", "DUPLICATE") and audit_id == expected:
        return {"status": status, "audit_id": expected}
    if status == "BLOCKED":
        return {"status": "ENQUEUE_FAILED", "code": "AUDIT_RUNNER_BLOCKED"}
    return {"status": "ENQUEUE_OUTCOME_UNKNOWN", "code": "ENQUEUE_RECEIPT_MISMATCH"}


def after_hub_closeout(final_receipt, runtime_agent: str, session_id: str,
                       project_slug: str, typed_task_event=None, *, checkpoint_id=None,
                       dispatch=None, outbox_root=None, ledger_root=FLEET_RUN_LEDGER,
                       catalog_path=None, environ=None) -> dict:
    """Best-effort hook over the existing Hub receipt and matching local sidecar.

    typed_task_event is retained for API compatibility and never trusted. The
    event is rebuilt from the local sidecar, final Hub receipt and exact native
    Fleet ledger entries.
    """
    _ = typed_task_event
    if type(final_receipt) is not dict or final_receipt.get("action") != "record-session":
        return _skip("UNCLASSIFIED")
    if final_receipt.get("receipt_status") != "COMPLETE":
        return _skip("CLOSEOUT_NOT_FINAL")
    try:
        audit_core._identifier(runtime_agent)
        audit_core._identifier(session_id)
        audit_core._identifier(project_slug)
    except (ValueError, TypeError):
        return _skip("INVALID_BINDING")
    if (final_receipt.get("agent") != runtime_agent
            or final_receipt.get("project_slug") != project_slug
            or final_receipt.get("session_sha256") != hashlib.sha256(session_id.encode("utf-8")).hexdigest()
            or final_receipt.get("work_success_attested") is not False):
        return _skip("INVALID_BINDING")
    if final_receipt.get("outcome") != "COMPLETED":
        return _skip("NOT_COMPLETED")
    if not _safe_checkpoint(checkpoint_id):
        return _skip("CHECKPOINT_UNAVAILABLE")
    checkpoint_key = _hub_checkpoint_key(runtime_agent, project_slug, session_id, checkpoint_id)
    if final_receipt.get("checkpoint_key") != checkpoint_key:
        return _skip("INVALID_BINDING")
    candidate, binding, error = completion.resolve_candidate(
        runtime_agent, session_id, project_slug, checkpoint_key,
        outbox_root=outbox_root, environ=environ,
    )
    if error:
        return _skip(error)
    if candidate.get("episode_role") != "top-level":
        return _skip("ROLE_EXCLUDED")
    if candidate.get("task_kind") not in {"delivery", "repair"}:
        return _skip("ROLE_EXCLUDED")
    if candidate.get("exception") == "worker-node":
        return _skip("ROLE_EXCLUDED")

    event, event_error = completion.existing_event(
        candidate, binding, checkpoint_key, outbox_root=outbox_root)
    if event_error:
        return _skip(event_error)
    if event is None:
        try:
            packet = _packet(candidate, final_receipt["outcome"], session_id=session_id,
                             ledger_root=ledger_root, catalog_path=catalog_path)
        except (ValueError, TypeError, KeyError):
            return _skip("PRIVACY_OR_SCHEMA_REJECTED")
        if (hashlib.sha256(packet["session_id"].encode("utf-8")).hexdigest()
                != candidate["session_sha256"]
                or packet["session_id"] != session_id
                or packet["project_slug"] != project_slug
                or packet["outcome"] != final_receipt["outcome"]
                or not audit_core.should_audit(packet)):
            return _skip("ROLE_EXCLUDED")
        event, event_error = completion.store_event(
            candidate, binding, checkpoint_key, packet, audit_core.audit_id(packet),
            outbox_root=outbox_root,
        )
        if event_error:
            return _skip(event_error)
    try:
        packet = audit_core.validate_packet(event["packet"])
        audit_id = audit_core.audit_id(packet)
    except (ValueError, TypeError, KeyError):
        return _skip("PRIVACY_OR_SCHEMA_REJECTED")
    if (event.get("audit_id") != audit_id or packet["outcome"] != "COMPLETED"
            or packet["project_slug"] != project_slug
            or packet["session_id"] != session_id
            or hashlib.sha256(packet["session_id"].encode("utf-8")).hexdigest()
            != candidate["session_sha256"]):
        return _skip("INVALID_BINDING")
    enqueue = dispatch or _production_enqueue
    try:
        result = enqueue(json.loads(audit_core._json_bytes(packet)))
    except Exception:
        return {"status": "ENQUEUE_FAILED", "code": "DISPATCH_FAILED", "audit_id": audit_id}
    if type(result) is not dict or result.get("status") not in ("QUEUED", "DUPLICATE", "SKIPPED"):
        return {"status": "ENQUEUE_FAILED", "code": "DISPATCH_RECEIPT_INVALID", "audit_id": audit_id}
    if result.get("audit_id") not in (None, audit_id):
        return {"status": "ENQUEUE_FAILED", "code": "DISPATCH_RECEIPT_MISMATCH", "audit_id": audit_id}
    return {"status": result["status"], "audit_id": audit_id}

