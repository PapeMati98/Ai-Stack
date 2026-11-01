"""Deterministic helper for native session journal publishing via canonical Knowledge Hub gateway.

Publishes exactly one canonical record-session for each evidenced native Fleet execution.
Never mutates native or product verdicts.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple

sys.dont_write_bytecode = True

SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
PROJECT_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
ALLOWED_OUTCOMES = frozenset({"COMPLETED", "INTERRUPTED", "BLOCKED", "NO_DURABLE_CHANGE"})
DEFAULT_GATEWAY_TIMEOUT = 240


def _config_path(config_path: Optional[Path | str] = None) -> Path:
    """Resolve the knowledge config path for gateway resolution.

    Precedence: explicit ``config_path`` argument, then the
    ``AGENT_KNOWLEDGE_CONFIG`` environment override, then the installed default
    ``~/.agents/knowledge-config.json``. A supplied override must be exactly one
    absolute path to an existing regular file (not a symlink); a missing,
    relative, or link path fails closed with no home/global fallback.
    """
    if config_path is not None:
        return Path(config_path)
    override = os.environ.get("AGENT_KNOWLEDGE_CONFIG")
    if override is not None:
        if not override.strip():
            raise ValueError("knowledge-config-empty")
        path = Path(override)
        if not path.is_absolute():
            raise ValueError("knowledge-config-not-absolute: " + str(path))
        if not path.is_file() or any(
            anchor.is_symlink() or getattr(anchor, "is_junction", lambda: False)()
            for anchor in (path, *path.parents)
        ):
            raise ValueError("knowledge-config-not-regular: " + str(path))
        return path
    return Path.home() / ".agents" / "knowledge-config.json"


def check_trustworthy_metadata(
    *,
    result: Dict[str, Any],
    record: Dict[str, Any],
    attested: bool,
    identity_ok: bool,
    candidate: Dict[str, Any],
    agent_id: str,
) -> Tuple[bool, str]:
    """Validate that native run metadata is coherent and trustworthy."""
    if not isinstance(result, dict) or not result:
        return False, "no-native-dispatch"

    run_id = result.get("run_id")
    if not run_id or not isinstance(run_id, str):
        return False, "missing-run-id"

    if not attested:
        return False, "runtime-attestation-unverified"

    if not identity_ok:
        return False, "model-identity-unverified"

    if not isinstance(record, dict) or record.get("run_id") != run_id:
        return False, "run-id-mismatch-between-result-and-record"
    if record.get("agent") != agent_id:
        return False, "agent-mismatch-between-candidate-and-record"

    expected_model = candidate.get("model")
    result_model = result.get("model")
    if not expected_model or expected_model != result_model or record.get("model") != expected_model:
        return False, "model-mismatch-between-candidate-and-result"
    if (not SAFE_ID_PATTERN.fullmatch(run_id)
            or not SAFE_ID_PATTERN.fullmatch(str(agent_id)) or len(agent_id) > 64
            or not MODEL_PATTERN.fullmatch(str(expected_model))):
        return False, "invalid-producer-identifiers"
    if result.get("status") not in {"COMPLETED", "PARTIAL", "FAILED", "BLOCKED"}:
        return False, "unknown-native-outcome"

    # Require real completion timestamp / native execution evidence;
    # started_at alone does not prove a terminal episode.
    has_completion = (
        result.get("exit_code") is not None
        or record.get("exit_code") is not None
        or bool(record.get("completed_at"))
        or bool(result.get("completed_at"))
        or bool(record.get("completion_time"))
        or bool(result.get("completion_time"))
        or bool(record.get("finished_at"))
        or bool(result.get("finished_at"))
    )
    if not has_completion:
        return False, "insufficient-execution-evidence"

    return True, ""


def parent_project_slug(plan: Dict[str, Any]) -> Optional[str]:
    """Read only routing metadata from a structurally bound parent receipt."""
    try:
        path = Path(plan["context_receipt"])
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or path.stat().st_nlink != 1 or path.stat().st_size > 1024 * 1024
                or path.name.lower().startswith(("auth", "credentials"))):
            return None
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        binding = data.get("vault_binding") or {}
        session = plan.get("context_receipt_session")
        parent_agent = plan.get("context_receipt_agent")
        project = data.get("project_slug")
        if (data.get("schema_version") == 2 and data.get("action") == "begin-task"
                and data.get("status") == "COMPLETE" and data.get("coherent_read") is True
                and isinstance(parent_agent, str) and parent_agent != "unknown"
                and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", parent_agent)
                and data.get("agent") == parent_agent
                and isinstance(session, str) and bool(session)
                and data.get("session_sha256") == hashlib.sha256(session.encode()).hexdigest()
                and binding.get("version") == 1
                and binding.get("kind") == "local-execution-evidence"
                and re.fullmatch(r"[a-f0-9]{64}", str(binding.get("vault_path_sha256", "")))
                and isinstance(project, str) and PROJECT_PATTERN.fullmatch(project)):
            return project
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def resolve_project_slug(task: Dict[str, Any], request: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Resolve project_slug preferring explicit task/plan slug, detecting conflicts."""
    knowledge_slug = None
    if isinstance(task.get("knowledge"), dict) and task["knowledge"].get("project_slug"):
        knowledge_slug = str(task["knowledge"]["project_slug"]).strip()
    task_slug = str(task["project_slug"]).strip() if task.get("project_slug") else None

    plan_slug = None
    if request.get("project_slug"):
        plan_slug = str(request["project_slug"]).strip()

    receipt_slug = parent_project_slug(request)

    # Detect conflicts among provided project slugs
    known_slugs = [("knowledge", knowledge_slug), ("task", task_slug), ("plan", plan_slug), ("context_receipt", receipt_slug)]
    provided = [(src, s) for src, s in known_slugs if s]
    if len(provided) > 1:
        first_src, first_val = provided[0]
        for src, s in provided[1:]:
            if s != first_val:
                return None, f"project-slug-conflict: {first_src}={first_val} {src}={s}"

    resolved = knowledge_slug or task_slug or plan_slug or receipt_slug
    if not resolved:
        return None, "missing-project-slug: no explicit task or plan project_slug"
    if not PROJECT_PATTERN.fullmatch(resolved):
        return None, "invalid-project-slug"

    return resolved, None


def resolve_gateway(config_path: Optional[Path | str] = None) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve canonical gateway from the knowledge config.

    Config precedence: explicit ``config_path`` argument, then the
    ``AGENT_KNOWLEDGE_CONFIG`` environment override, then the installed default
    ``~/.agents/knowledge-config.json``. A supplied override must be exactly one
    absolute path to an existing regular file (not a symlink); a missing,
    relative, or link path fails closed with no home/global fallback.
    """
    try:
        cfg_path = _config_path(config_path)
    except (OSError, ValueError) as exc:
        return None, str(exc)
    if not cfg_path.is_file():
        return None, f"knowledge-config-missing: {cfg_path}"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return None, f"knowledge-config-invalid: {exc}"

    if not isinstance(cfg, dict):
        return None, "knowledge-config-invalid-fields"
    canonical_skill_path = cfg.get("canonical_skill_path")
    if not isinstance(canonical_skill_path, str) or not canonical_skill_path.strip():
        return None, "canonical_skill_path-missing-in-config"

    gateway = Path(canonical_skill_path) / "scripts" / "knowledge.py"
    if not gateway.is_absolute() or gateway.is_symlink() or not gateway.is_file():
        return None, f"knowledge-gateway-missing: {gateway}"

    return gateway, None


def map_outcome(native_status: Optional[str]) -> str:
    """Map native execution status to record-session outcome."""
    status_upper = str(native_status or "").strip().upper()
    if status_upper == "COMPLETED":
        return "COMPLETED"
    if status_upper == "PARTIAL":
        return "INTERRUPTED"
    return "BLOCKED"


def make_checkpoint_id(task: Dict[str, Any], role: str, run_id: str) -> str:
    """Generate a stable role-distinct checkpoint id."""
    task_id = str(task.get("id") or "task").strip()
    role_str = str(role or "worker").strip()
    return f"{task_id}:{role_str}:{run_id}"


make_checkpoint_key = make_checkpoint_id


def compute_checkpoint_key(agent: str, project_slug: str, session_id: str, checkpoint_id: str) -> str:
    """Compute canonical checkpoint_key as SHA256(JSON([agent,project,session_id,checkpoint_id], separators=(',', ':')))."""
    raw = json.dumps([agent, project_slug, session_id, checkpoint_id], ensure_ascii=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_metadata_note(
    *,
    role: str,
    agent_id: str,
    model: str,
    run_id: str,
    native_status: str,
    outcome: str,
) -> str:
    """Build a fixed-format bounded metadata note with allowlisted fields only."""
    note_payload = {
        "role": role,
        "agent": agent_id,
        "model": model,
        "session_correlation": run_id,
        "native_status": native_status,
        "outcome": outcome,
        "session_id_source": "fleet-run",
        "producer_authentication": "none",
        "transport": "harness-mediated",
        "lifecycle_binding": "unverified",
        "product_acceptance": "separate",
        "note": "COMPLETED native work != accepted product; product acceptance remains separate",
    }
    return json.dumps(note_payload, sort_keys=True)


def record_native_session(
    *,
    request: Dict[str, Any],
    task: Dict[str, Any],
    role: str,
    outdir: Path | str,
    result: Dict[str, Any],
    record: Dict[str, Any],
    attested: bool,
    identity_ok: bool,
    identity: Dict[str, Any],
    candidate: Dict[str, Any],
    agent_id: str,
    fleet: Any = None,
    gateway_runner: Optional[Callable] = None,
    config_path: Optional[Path | str] = None,
    native_execution: bool = True,
) -> Dict[str, Any]:
    """Publish exactly one canonical session journal entry for a native run.

    Failures return explicit BLOCKED or UNKNOWN metadata and never mutate native
    or product verdicts.
    """
    # Check for synthetic profiles or injected fake fleet without explicit runner
    is_fake_fleet = not native_execution and gateway_runner is None
    is_synthetic_adapter = (
        agent_id in {"opencode-beam", "opencode-mercury"}
        or (isinstance(getattr(fleet, "DEFAULT_PROFILES", None), dict)
            and getattr(fleet, "DEFAULT_PROFILES", {}).get(agent_id, {}).get("adapter") in {"synthetic", "fake"})
    )
    if is_fake_fleet or is_synthetic_adapter:
        return {
            "status": "NOT_APPLICABLE",
            "receipt_status": "NOT_APPLICABLE",
            "reason": "synthetic-or-fake-adapter",
            "hub_written": False,
        }

    # Validate trustworthy coherent metadata
    metadata_ok, why_metadata = check_trustworthy_metadata(
        result=result,
        record=record,
        attested=attested,
        identity_ok=identity_ok,
        candidate=candidate,
        agent_id=agent_id,
    )
    if not metadata_ok:
        if why_metadata == "no-native-dispatch":
            return {
                "status": "NOT_APPLICABLE",
                "receipt_status": "NOT_APPLICABLE",
                "reason": why_metadata,
                "hub_written": False,
            }
        return {
            "status": "UNVERIFIED",
            "receipt_status": "UNVERIFIED",
            "reason": why_metadata,
            "session_id_source": "fleet-run",
            "producer_authentication": "none",
            "transport": "harness-mediated",
            "lifecycle_binding": "unverified",
            "hub_written": False,
        }

    # Resolve project slug
    project_slug, why_project = resolve_project_slug(task, request)
    if not project_slug:
        return {
            "status": "BLOCKED",
            "receipt_status": "BLOCKED",
            "cause": "project-slug-error",
            "reason": why_project,
            "session_id_source": "fleet-run",
            "producer_authentication": "none",
            "transport": "harness-mediated",
            "lifecycle_binding": "unverified",
            "hub_written": False,
        }

    # Resolve gateway script
    gateway, why_gateway = resolve_gateway(config_path)
    if not gateway:
        return {
            "status": "BLOCKED",
            "receipt_status": "BLOCKED",
            "cause": "gateway-resolution-error",
            "reason": why_gateway,
            "project_slug": project_slug,
            "session_id_source": "fleet-run",
            "producer_authentication": "none",
            "transport": "harness-mediated",
            "lifecycle_binding": "unverified",
            "hub_written": False,
        }

    # Set up durable outbox directories
    journal_dir = Path(outdir).absolute() / "session-journal"
    for anchor in (journal_dir, *journal_dir.parents):
        if anchor.is_symlink() or (getattr(anchor, "is_junction", lambda: False)()):
            return {"status": "BLOCKED", "cause": "unsafe-journal-path", "hub_written": False}
    journal_dir.mkdir(parents=True, exist_ok=True)
    temp_root = journal_dir / "temp"
    temp_root.mkdir(parents=True, exist_ok=True)
    receipt_root = journal_dir / "receipts"
    receipt_root.mkdir(parents=True, exist_ok=True)

    outbox_file = journal_dir / "outbox.json"
    note_file = journal_dir / "note.md"
    receipt_file = journal_dir / "receipt.json"
    pending_file = journal_dir / "pending.json"
    response_file = journal_dir / "gateway-response.json"
    for file in (outbox_file, note_file, receipt_file, pending_file, response_file):
        if file.exists() and (file.is_symlink() or file.stat().st_nlink != 1):
            return {"status": "BLOCKED", "cause": "unsafe-journal-path", "hub_written": False}

    run_id = str(result["run_id"])
    if role not in {"maker", "reviewer"} or not SAFE_ID_PATTERN.fullmatch(str(task.get("id", ""))):
        return {"status": "UNVERIFIED", "reason": "invalid-role-or-node", "hub_written": False}
    model = str(candidate.get("model") or result.get("model") or "")
    native_status = str(result.get("status") or "BLOCKED")
    outcome = map_outcome(native_status)
    checkpoint_id = make_checkpoint_id(task, role, run_id)
    expected_checkpoint_key = compute_checkpoint_key(agent_id, project_slug, run_id, checkpoint_id)
    expected_session_sha = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    note_text = build_metadata_note(
        role=role,
        agent_id=agent_id,
        model=model,
        run_id=run_id,
        native_status=native_status,
        outcome=outcome,
    )

    payload = {
        "action": "record-session",
        "project_slug": project_slug,
        "agent": agent_id,
        "session_id": run_id,
        "outcome": outcome,
        "checkpoint_id": checkpoint_id,
        "checkpoint_key": expected_checkpoint_key,
        "session_sha256": expected_session_sha,
        "classification": "internal",
        "note": note_text,
        "session_id_source": "fleet-run",
        "producer_authentication": "none",
        "transport": "harness-mediated",
        "lifecycle_binding": "unverified",
    }
    payload_bytes = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    payload_sha = hashlib.sha256(payload_bytes).hexdigest()

    # Idempotent reuse: check existing confirmed receipt
    if receipt_file.is_file():
        try:
            confirmed_receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
            if outbox_file.is_file():
                existing_bytes = outbox_file.read_bytes()
                existing_sha = hashlib.sha256(existing_bytes).hexdigest()
                if existing_sha != payload_sha:
                    return {
                        "status": "BLOCKED",
                        "receipt_status": "CONFLICT",
                        "cause": "payload-conflict",
                        "reason": "Immutable session journal payload conflict on rerun",
                        "project_slug": project_slug,
                        "session_id_source": "fleet-run",
                        "producer_authentication": "none",
                        "transport": "harness-mediated",
                        "lifecycle_binding": "unverified",
                        "hub_written": False,
                    }
            if (not outbox_file.is_file() or not note_file.is_file() or not response_file.is_file()
                    or note_file.read_text(encoding="utf-8") != note_text):
                raise ValueError("confirmation-evidence-missing")
            raw = json.loads(response_file.read_text(encoding="utf-8"))
            expected = {"action": "record-session", "receipt_status": "COMPLETE",
                        "agent": agent_id, "project_slug": project_slug, "outcome": outcome,
                        "session_sha256": expected_session_sha, "checkpoint_key": expected_checkpoint_key,
                        "lifecycle_binding": "unverified"}
            if (not isinstance(raw, dict) or not isinstance(confirmed_receipt, dict)
                    or any(raw.get(k) != v or confirmed_receipt.get(k) != v for k, v in expected.items())
                    or confirmed_receipt.get("status") != "COMPLETE"
                    or confirmed_receipt.get("payload_sha256") != payload_sha
                    or any(not isinstance(raw.get(k), str) or not raw[k].strip()
                           or raw[k] != confirmed_receipt.get(k) for k in ("source_id", "proposal_id"))):
                raise ValueError("confirmation-evidence-mismatch")
            return confirmed_receipt
        except Exception:
            return {"status": "UNKNOWN", "receipt_status": "UNKNOWN", "cause": "confirmation-unverified", "hub_written": None}

    # Pending attempt check: if a pending attempt was saved and not confirmed, do not silently retry
    if pending_file.is_file():
        try:
            pending_data = json.loads(pending_file.read_text(encoding="utf-8"))
        except Exception:
            pending_data = {}
        if pending_data.get("state") == "pending":
            return {
                "status": "UNKNOWN",
                "receipt_status": "UNKNOWN",
                "cause": "pending-attempt-lost",
                "reason": "Previous session journal attempt was pending or timed out; cannot silently retry",
                "project_slug": project_slug,
                "session_id_source": "fleet-run",
                "producer_authentication": "none",
                "transport": "harness-mediated",
                "lifecycle_binding": "unverified",
                "hub_written": None,
                "pending_at": pending_data.get("started_at"),
                "payload_sha256": payload_sha,
            }

    # Reserve the one attempt before any gateway process can be started.
    if pending_file.exists() or outbox_file.exists():
        return {"status": "UNKNOWN", "receipt_status": "UNKNOWN", "cause": "pending-attempt-lost", "hub_written": None}
    try:
        with pending_file.open("x", encoding="utf-8") as handle:
            json.dump({"run_id": run_id, "state": "pending", "payload_sha256": payload_sha}, handle)
    except FileExistsError:
        return {"status": "UNKNOWN", "receipt_status": "UNKNOWN", "cause": "pending-attempt-lost", "hub_written": None}
    # Persist exact immutable outbox and note; retained on every failure.
    outbox_file.write_bytes(payload_bytes)
    note_file.write_text(note_text, encoding="utf-8")
    pending_file.write_text(
        json.dumps({
            "run_id": run_id,
            "state": "pending",
            "started_at": time.time(),
            "payload_sha256": payload_sha,
        }, sort_keys=True),
        encoding="utf-8",
    )

    # Canonical CLI uses --checkpoint-id, --path must be .md or .txt, --classification internal, --retain-temp-root
    argv = [
        sys.executable, "-B", str(gateway or "knowledge.py"), "record-session",
        "--project-slug", project_slug,
        "--agent", agent_id,
        "--session-id", run_id,
        "--outcome", outcome,
        "--checkpoint-id", checkpoint_id,
        "--summary", f"Native Fleet {role} episode {outcome}; product acceptance separate",
        "--path", str(note_file),
        "--classification", "internal",
        "--retain-temp-root", str(temp_root),
        "--receipt-root", str(receipt_root),
    ]

    def default_runner(args, timeout=DEFAULT_GATEWAY_TIMEOUT):
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        if config_path is not None:
            options["env"] = dict(os.environ, AGENT_KNOWLEDGE_CONFIG=str(Path(config_path).absolute()))
        return subprocess.run(args, capture_output=True, timeout=timeout, **options)

    runner_fn = gateway_runner or default_runner

    try:
        proc = runner_fn(argv, timeout=DEFAULT_GATEWAY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {
            "status": "UNKNOWN",
            "receipt_status": "UNKNOWN",
            "cause": "gateway-timeout",
            "reason": "Gateway record-session invocation timed out",
            "project_slug": project_slug,
            "session_id_source": "fleet-run",
            "producer_authentication": "none",
            "transport": "harness-mediated",
            "lifecycle_binding": "unverified",
            "hub_written": None,
            "payload_sha256": payload_sha,
        }
    except Exception as exc:
        return {
            "status": "BLOCKED",
            "receipt_status": "BLOCKED",
            "cause": "gateway-invocation-error",
            "reason": f"Gateway invocation failed: {type(exc).__name__}",
            "project_slug": project_slug,
            "session_id_source": "fleet-run",
            "producer_authentication": "none",
            "transport": "harness-mediated",
            "lifecycle_binding": "unverified",
            "hub_written": None,
        }

    # Preserve a possibly partial gateway confirmation even on nonzero exit.
    # It may contain the exact source/proposal for an explicit reconciliation.
    raw_stdout = getattr(proc, "stdout", b"")
    raw_bytes = raw_stdout if isinstance(raw_stdout, bytes) else str(raw_stdout).encode("utf-8")
    (journal_dir / "gateway-stdout.txt").write_bytes(raw_bytes[:1024 * 1024])
    # Validate process exit code; an absent exit status is not success.
    exit_code = getattr(proc, "returncode", None)
    if exit_code != 0:
        return {
            "status": "BLOCKED",
            "receipt_status": "NONZERO_EXIT",
            "cause": "gateway-nonzero-exit",
            "reason": f"Gateway exited with returncode {exit_code}",
            "exit_code": exit_code,
            "project_slug": project_slug,
            "session_id_source": "fleet-run",
            "producer_authentication": "none",
            "transport": "harness-mediated",
            "lifecycle_binding": "unverified",
            "hub_written": None,
            "gateway_output": str(journal_dir / "gateway-stdout.txt"),
        }

    stdout = getattr(proc, "stdout", "")
    if isinstance(stdout, bytes):
        stdout_str = stdout.decode("utf-8-sig", errors="replace")
    else:
        stdout_str = str(stdout or "")

    try:
        gw_resp = json.loads(stdout_str)
    except Exception:
        return {
            "status": "BLOCKED",
            "receipt_status": "MALFORMED_RESPONSE",
            "cause": "gateway-malformed-response",
            "reason": "Gateway stdout is not valid JSON",
            "project_slug": project_slug,
            "session_id_source": "fleet-run",
            "producer_authentication": "none",
            "transport": "harness-mediated",
            "lifecycle_binding": "unverified",
            "hub_written": None,
        }

    response_file.write_text(json.dumps(gw_resp, indent=2, sort_keys=True), encoding="utf-8")

    if not isinstance(gw_resp, dict):
        return {
            "status": "BLOCKED",
            "receipt_status": "MALFORMED_RESPONSE",
            "cause": "gateway-malformed-response",
            "reason": "Gateway response is not a JSON object",
            "project_slug": project_slug,
            "hub_written": None,
        }

    rec_status = gw_resp.get("receipt_status")
    if rec_status in {"INCOMPLETE", "CHECKPOINT_BLOCKED"}:
        return {
            "status": "BLOCKED",
            "receipt_status": rec_status,
            "cause": f"gateway-{rec_status.lower()}",
            "reason": f"Gateway returned receipt_status={rec_status}",
            "project_slug": project_slug,
            "session_id_source": "fleet-run",
            "producer_authentication": "none",
            "transport": "harness-mediated",
            "lifecycle_binding": "unverified",
            "hub_written": None,
            "gateway_receipt_status": rec_status,
        }

    # Validate agreement between invocation and gateway confirmation
    mismatches = []
    if gw_resp.get("action") != "record-session":
        mismatches.append(f"action: expected 'record-session', got {gw_resp.get('action')!r}")
    if gw_resp.get("agent") != agent_id:
        mismatches.append(f"agent: expected {agent_id!r}, got {gw_resp.get('agent')!r}")
    if gw_resp.get("project_slug") != project_slug:
        mismatches.append(f"project_slug: expected {project_slug!r}, got {gw_resp.get('project_slug')!r}")
    if gw_resp.get("outcome") != outcome:
        mismatches.append(f"outcome: expected {outcome!r}, got {gw_resp.get('outcome')!r}")
    if gw_resp.get("checkpoint_key") != expected_checkpoint_key:
        mismatches.append(f"checkpoint_key: expected {expected_checkpoint_key!r}, got {gw_resp.get('checkpoint_key')!r}")
    if rec_status != "COMPLETE":
        mismatches.append(f"receipt_status: expected 'COMPLETE', got {rec_status!r}")
    if gw_resp.get("session_sha256") != expected_session_sha:
        mismatches.append(f"session_sha256: expected {expected_session_sha!r}, got {gw_resp.get('session_sha256')!r}")
    has_source_or_proposal = all(isinstance(gw_resp.get(key), str) and bool(gw_resp[key].strip())
                                 for key in ("source_id", "proposal_id"))
    if not has_source_or_proposal:
        mismatches.append("missing source/proposal IDs")
    if gw_resp.get("lifecycle_binding") != "unverified":
        mismatches.append("unexpected lifecycle binding")

    if mismatches:
        return {
            "status": "BLOCKED",
            "receipt_status": "CONFIRMATION_MISMATCH",
            "cause": "gateway-confirmation-mismatch",
            "reason": "; ".join(mismatches),
            "project_slug": project_slug,
            "session_id_source": "fleet-run",
            "producer_authentication": "none",
            "transport": "harness-mediated",
            "lifecycle_binding": "unverified",
            "hub_written": None,
            "gateway_receipt_status": rec_status,
        }

    # Confirmation succeeded! Mark pending as completed and save confirmed receipt.
    completed_time = time.time()
    pending_file.write_text(
        json.dumps({
            "run_id": run_id,
            "state": "completed",
            "completed_at": completed_time,
            "payload_sha256": payload_sha,
        }, sort_keys=True),
        encoding="utf-8",
    )

    confirmed_receipt = {
        "status": "COMPLETE",
        "receipt_status": "COMPLETE",
        "action": "record-session",
        "project_slug": project_slug,
        "agent": agent_id,
        "session_id": run_id,
        "outcome": outcome,
        "checkpoint_id": checkpoint_id,
        "checkpoint_key": expected_checkpoint_key,
        "session_sha256": expected_session_sha,
        "source_id": gw_resp.get("source_id"),
        "proposal_id": gw_resp.get("proposal_id"),
        "source_ids": gw_resp.get("source_ids", [gw_resp.get("source_id")] if gw_resp.get("source_id") else []),
        "proposal_ids": gw_resp.get("proposal_ids", [gw_resp.get("proposal_id")] if gw_resp.get("proposal_id") else []),
        "session_id_source": "fleet-run",
        "producer_authentication": "none",
        "transport": "harness-mediated",
        "lifecycle_binding": "unverified",
        "hub_written": True,
        "payload_sha256": payload_sha,
        "confirmed_at": completed_time,
        "completed_at": completed_time,
    }
    receipt_file.write_text(json.dumps(confirmed_receipt, indent=2, sort_keys=True), encoding="utf-8")
    return confirmed_receipt
