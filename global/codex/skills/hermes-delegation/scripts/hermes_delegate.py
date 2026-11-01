#!/usr/bin/env python3
"""Deterministic Windows-side client for Robert/Hermes on AI Lab Server."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

VERSION = "0.1.0"
SCHEMA_VERSION = 1
MODES = {
    "REPORT_ONLY",
    "RESEARCH_ONLY",
    "PRIVATE_READ",
    "INFRA_OBSERVE",
    "PREPARE_PATCH",
    "INFRA_CHANGE",
    "EXTERNAL_ACTION",
    "SCHEDULED_AUTONOMY",
}
SENSITIVE_MODES = {
    "PRIVATE_READ",
    "INFRA_OBSERVE",
    "PREPARE_PATCH",
    "INFRA_CHANGE",
    "EXTERNAL_ACTION",
    "SCHEDULED_AUTONOMY",
}
TARGETS = {
    "hermes",
    "lab-server",
    "northwind-ubuntu-vm",
    "win11-worker",
    "mailbox",
    "calendar",
    "external",
}
TASK_ID_RE = re.compile(r"^hdt-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")
RECEIPT_ID_RE = re.compile(r"^hap-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}$")
HEX64_RE = re.compile(r"^[a-f0-9]{64}$")
SAFE_REMOTE_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
DEFAULT_REMOTE_ROOT = (
    "/srv/ai-lab/hermes-standalone/home/.hermes/delegation-bridge"
)
DEFAULTS = {
    "ssh_host": "lab-server",
    "expected_hostname": "lab-server",
    "expected_user": "operator",
    "remote_root": DEFAULT_REMOTE_ROOT,
    "remote_worker": f"{DEFAULT_REMOTE_ROOT}/current/hermes_remote_worker.py",
    "ssh_connect_timeout_seconds": 10,
}
CONFIGURABLE_KEYS = {"ssh_connect_timeout_seconds"}
MODE_TARGETS = {
    "REPORT_ONLY": {"hermes"},
    "RESEARCH_ONLY": {"hermes", "external"},
    "PRIVATE_READ": {"mailbox", "calendar"},
    "INFRA_OBSERVE": {"lab-server", "northwind-ubuntu-vm", "win11-worker"},
    "PREPARE_PATCH": {"lab-server", "northwind-ubuntu-vm", "win11-worker"},
    "INFRA_CHANGE": {"lab-server", "northwind-ubuntu-vm", "win11-worker"},
    "EXTERNAL_ACTION": {"mailbox", "calendar", "external"},
    "SCHEDULED_AUTONOMY": set(TARGETS),
}
MODE_ALLOWED_SKILLS = {
    "REPORT_ONLY": set(),
    "RESEARCH_ONLY": set(),
    "PRIVATE_READ": {
        "client-mailbox-knowledge",
        "google-workspace",
        "himalaya",
        "personal-operations-orchestration",
    },
    "INFRA_OBSERVE": {
        "ai-lab-infrastructure-operations",
        "northwind-workflow",
        "shared-agent-knowledge",
    },
    "PREPARE_PATCH": {
        "northwind-workflow",
        "requesting-code-review",
        "shared-agent-knowledge",
        "systematic-debugging",
        "test-driven-development",
    },
    "INFRA_CHANGE": {
        "ai-lab-infrastructure-operations",
        "northwind-workflow",
        "shared-agent-knowledge",
    },
    "EXTERNAL_ACTION": {
        "client-mailbox-knowledge",
        "google-workspace",
        "himalaya",
        "personal-operations-orchestration",
    },
    "SCHEDULED_AUTONOMY": {
        "ai-lab-infrastructure-operations",
        "northwind-workflow",
        "personal-operations-orchestration",
        "shared-agent-knowledge",
    },
}
ACTION_FIELDS = {
    "send-email": {
        "action_type", "scope", "max_actions", "sender", "recipients", "cc",
        "bcc", "subject", "body_sha256", "attachment_sha256",
    },
    "private-read": {
        "action_type", "scope", "max_actions", "account", "data_kind", "query",
        "max_items",
    },
    "infra-observe": {
        "action_type", "scope", "max_actions", "target", "purpose", "read_scope",
    },
    "prepare-patch": {
        "action_type", "scope", "max_actions", "repository", "worktree",
        "allowed_paths", "test_commands",
    },
    "calendar-write": {
        "action_type", "scope", "max_actions", "calendar", "title", "start", "end",
        "timezone",
    },
    "production-change": {
        "action_type", "scope", "max_actions", "target", "operation",
        "expected_effect", "rollback",
    },
    "vm-control": {
        "action_type", "scope", "max_actions", "vm", "operation", "preconditions",
        "rollback",
    },
    "scheduled-job": {
        "action_type", "scope", "max_actions", "target", "schedule", "action",
        "expires_at", "max_runs",
    },
    "account-change": {
        "action_type", "scope", "max_actions", "account", "operation", "rollback",
    },
    "public-message": {
        "action_type", "scope", "max_actions", "channel", "audience", "content_sha256",
    },
    "other-sensitive": {
        "action_type", "scope", "max_actions", "target", "operation",
        "expected_effect", "rollback",
    },
}


class BridgeError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    value = value or utc_now()
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def contract_hash(contract: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(contract)
    unsigned.pop("contract_sha256", None)
    return sha256_bytes(canonical_bytes(unsigned))


def emit(value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    output_encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe_payload = payload.encode(
        output_encoding, errors="backslashreplace"
    ).decode(output_encoding)
    print(safe_payload)


def state_root() -> Path:
    configured = os.environ.get("HERMES_DELEGATION_STATE")
    root = Path(configured).expanduser() if configured else Path.home() / ".agents" / "hermes-delegation"
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_path() -> Path:
    configured = os.environ.get("HERMES_DELEGATION_CONFIG")
    return Path(configured).expanduser() if configured else state_root() / "config.json"


def load_config() -> dict[str, Any]:
    config = dict(DEFAULTS)
    path = config_path()
    if path.exists():
        loaded = read_json(path)
        if not isinstance(loaded, dict):
            raise BridgeError(f"Config must be a JSON object: {path}")
        unexpected = sorted(set(loaded) - CONFIGURABLE_KEYS)
        if unexpected:
            raise BridgeError(
                "Config may change only ssh_connect_timeout_seconds; "
                f"unexpected keys: {unexpected}"
            )
        for key in CONFIGURABLE_KEYS:
            if key in loaded:
                config[key] = loaded[key]
    timeout = config["ssh_connect_timeout_seconds"]
    if not isinstance(timeout, int) or not 1 <= timeout <= 60:
        raise BridgeError("ssh_connect_timeout_seconds must be between 1 and 60")
    for key in ("remote_root", "remote_worker"):
        if not isinstance(config[key], str) or not SAFE_REMOTE_RE.fullmatch(config[key]):
            raise BridgeError(f"Unsafe remote path in config: {key}")
    return config


def read_text(path: Path, max_bytes: int = 2_000_000) -> str:
    if not path.is_file():
        raise BridgeError(f"File does not exist: {path}")
    if path.stat().st_size > max_bytes:
        raise BridgeError(f"File is too large: {path}")
    return path.read_text(encoding="utf-8-sig")


def read_json(path: Path, max_bytes: int = 2_000_000) -> Any:
    try:
        return json.loads(read_text(path, max_bytes=max_bytes))
    except json.JSONDecodeError as exc:
        raise BridgeError(f"Invalid JSON in {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp.write_text(payload, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def validate_task_id(task_id: str) -> str:
    if not TASK_ID_RE.fullmatch(task_id):
        raise BridgeError(f"Invalid task ID: {task_id}")
    return task_id


def validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(contract, dict):
        raise BridgeError("Contract must be a JSON object")
    required = {
        "schema_version",
        "bridge_version",
        "task_id",
        "created_at",
        "requested_for",
        "requesting_agent",
        "mode",
        "title",
        "task",
        "target",
        "acceptance_criteria",
        "requested_skills",
        "runtime",
        "approval",
        "delegation",
        "contract_sha256",
    }
    missing = sorted(required - set(contract))
    extra = sorted(set(contract) - required)
    if missing or extra:
        raise BridgeError(f"Contract shape mismatch: missing={missing}; extra={extra}")
    if contract["schema_version"] != SCHEMA_VERSION:
        raise BridgeError("Unsupported contract schema")
    validate_task_id(str(contract["task_id"]))
    if contract["requested_for"] != "Mateusz":
        raise BridgeError("requested_for identity mismatch")
    mode = str(contract["mode"])
    if mode not in MODES:
        raise BridgeError(f"Unsupported mode: {mode}")
    target = contract["target"]
    if not isinstance(target, dict) or target.get("kind") not in TARGETS:
        raise BridgeError("Invalid target")
    if target["kind"] not in MODE_TARGETS[mode]:
        raise BridgeError(f"Target {target['kind']} is incompatible with mode {mode}")
    if not isinstance(contract["title"], str) or not contract["title"].strip():
        raise BridgeError("Contract title is empty")
    if not isinstance(contract["task"], str) or not contract["task"].strip():
        raise BridgeError("Contract task is empty")
    if len(contract["task"].encode("utf-8")) > 200_000:
        raise BridgeError("Contract task is too large")
    runtime = contract["runtime"]
    if not isinstance(runtime, dict):
        raise BridgeError("runtime must be an object")
    max_turns = runtime.get("max_turns")
    timeout_seconds = runtime.get("timeout_seconds")
    if not isinstance(max_turns, int) or not 1 <= max_turns <= 500:
        raise BridgeError("max_turns must be between 1 and 500")
    if not isinstance(timeout_seconds, int) or not 60 <= timeout_seconds <= 43_200:
        raise BridgeError("timeout_seconds must be between 60 and 43200")
    approval = contract["approval"]
    if not isinstance(approval, dict):
        raise BridgeError("approval must be an object")
    required_approval = mode in SENSITIVE_MODES
    if approval.get("required") is not required_approval:
        raise BridgeError("approval.required does not match mode")
    if required_approval:
        manifest = validate_action_manifest(approval.get("action_manifest"))
        validate_mode_action(mode, manifest)
    elif approval.get("action_manifest") is not None:
        raise BridgeError("Non-sensitive contract cannot contain an action manifest")
    delegation = contract["delegation"]
    if not isinstance(delegation, dict) or delegation.get("depth") != 0 or delegation.get("max_depth") != 1:
        raise BridgeError("Delegation depth must be 0/1")
    expected_hash = contract_hash(contract)
    if contract.get("contract_sha256") != expected_hash:
        raise BridgeError("Contract SHA-256 mismatch")
    if mode == "PREPARE_PATCH" and not target.get("workdir"):
        raise BridgeError("PREPARE_PATCH requires an explicit workdir")
    if mode == "PREPARE_PATCH":
        manifest = approval["action_manifest"]
        if manifest.get("worktree") != target.get("workdir"):
            raise BridgeError("prepare-patch worktree must equal target.workdir")
    skills = contract["requested_skills"]
    if not isinstance(skills, list) or not all(isinstance(item, str) for item in skills):
        raise BridgeError("requested_skills must be an array of strings")
    unsupported = sorted(set(skills) - MODE_ALLOWED_SKILLS[mode])
    if unsupported:
        raise BridgeError(
            f"Skills are incompatible with mode {mode}: {', '.join(unsupported)}"
        )
    reasoning = runtime.get("reasoning")
    if reasoning not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise BridgeError("Invalid reasoning level")
    return contract


def require_fields(manifest: dict[str, Any], names: tuple[str, ...]) -> None:
    missing = [name for name in names if manifest.get(name) in (None, "", [])]
    if missing:
        raise BridgeError(f"Action manifest missing fields: {', '.join(missing)}")


def validate_sha(value: Any, field: str) -> None:
    if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
        raise BridgeError(f"{field} must be a lowercase SHA-256")


def validate_action_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BridgeError("Sensitive mode requires an action manifest object")
    action_type = value.get("action_type")
    allowed = {
        "send-email",
        "private-read",
        "infra-observe",
        "prepare-patch",
        "calendar-write",
        "production-change",
        "vm-control",
        "scheduled-job",
        "account-change",
        "public-message",
        "other-sensitive",
    }
    if action_type not in allowed:
        raise BridgeError(f"Unsupported action_type: {action_type}")
    unexpected = sorted(set(value) - ACTION_FIELDS[action_type])
    if unexpected:
        raise BridgeError(f"Action manifest contains unsupported fields: {unexpected}")
    require_fields(value, ("scope", "max_actions"))
    if not isinstance(value["scope"], str):
        raise BridgeError("scope must be a string")
    if type(value["max_actions"]) is not int or value["max_actions"] != 1:
        raise BridgeError("Every sensitive contract requires max_actions=1")
    if action_type == "send-email":
        require_fields(value, ("sender", "recipients", "subject", "body_sha256"))
        validate_sha(value["body_sha256"], "body_sha256")
        for item in value.get("attachment_sha256", []):
            validate_sha(item, "attachment_sha256")
        recipients = value["recipients"]
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= 50:
            raise BridgeError("recipients must be an array with 1-50 entries")
        email_re = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
        if any(not isinstance(item, str) or not email_re.fullmatch(item) for item in recipients):
            raise BridgeError("recipients contains an invalid email address")
        for field in ("cc", "bcc"):
            copy_to = value.get(field, [])
            if not isinstance(copy_to, list) or any(
                not isinstance(item, str) or not email_re.fullmatch(item)
                for item in copy_to
            ):
                raise BridgeError(f"{field} must be an array of email addresses")
    elif action_type == "private-read":
        require_fields(value, ("account", "data_kind", "query", "max_items"))
        if type(value["max_items"]) is not int or not 1 <= value["max_items"] <= 500:
            raise BridgeError("max_items must be between 1 and 500")
    elif action_type == "infra-observe":
        require_fields(value, ("target", "purpose", "read_scope"))
    elif action_type == "prepare-patch":
        require_fields(value, ("repository", "worktree", "allowed_paths", "test_commands"))
        if not isinstance(value["allowed_paths"], list) or not value["allowed_paths"]:
            raise BridgeError("allowed_paths must be a non-empty array")
        if any(
            not isinstance(item, str)
            or not item
            or Path(item).is_absolute()
            or ".." in Path(item).parts
            for item in value["allowed_paths"]
        ):
            raise BridgeError("allowed_paths must contain safe relative paths")
        if not isinstance(value["test_commands"], list) or any(
            not isinstance(item, str) or not item.strip()
            for item in value["test_commands"]
        ):
            raise BridgeError("test_commands must be an array")
    elif action_type == "calendar-write":
        require_fields(value, ("calendar", "title", "start", "end", "timezone"))
        start = parse_iso(str(value["start"]))
        end = parse_iso(str(value["end"]))
        if end <= start:
            raise BridgeError("calendar-write end must be after start")
    elif action_type == "production-change":
        require_fields(value, ("target", "operation", "expected_effect", "rollback"))
    elif action_type == "vm-control":
        require_fields(value, ("vm", "operation", "preconditions", "rollback"))
    elif action_type == "scheduled-job":
        require_fields(value, ("target", "schedule", "action", "expires_at", "max_runs"))
        if type(value["max_runs"]) is not int or not 1 <= value["max_runs"] <= 10_000:
            raise BridgeError("max_runs must be between 1 and 10000")
        if parse_iso(str(value["expires_at"])) <= utc_now():
            raise BridgeError("scheduled-job expires_at must be in the future")
    elif action_type == "account-change":
        require_fields(value, ("account", "operation", "rollback"))
    elif action_type == "public-message":
        require_fields(value, ("channel", "audience", "content_sha256"))
        validate_sha(value["content_sha256"], "content_sha256")
    else:
        require_fields(value, ("target", "operation", "expected_effect", "rollback"))
    return value


def validate_mode_action(mode: str, manifest: dict[str, Any]) -> None:
    action_type = manifest["action_type"]
    allowed_by_mode = {
        "PRIVATE_READ": {"private-read"},
        "INFRA_OBSERVE": {"infra-observe"},
        "PREPARE_PATCH": {"prepare-patch"},
        "INFRA_CHANGE": {"production-change", "vm-control", "other-sensitive"},
        "EXTERNAL_ACTION": {
            "send-email",
            "calendar-write",
            "account-change",
            "public-message",
            "other-sensitive",
        },
        "SCHEDULED_AUTONOMY": {"scheduled-job"},
    }
    if action_type not in allowed_by_mode[mode]:
        raise BridgeError(f"action_type {action_type} is incompatible with mode {mode}")


def default_skills(mode: str, target: str) -> list[str]:
    mapping = {
        ("PRIVATE_READ", "mailbox"): ["client-mailbox-knowledge", "himalaya"],
        ("PRIVATE_READ", "calendar"): ["google-workspace"],
        ("INFRA_OBSERVE", "lab-server"): ["ai-lab-infrastructure-operations"],
        ("INFRA_OBSERVE", "northwind-ubuntu-vm"): [
            "ai-lab-infrastructure-operations",
            "northwind-workflow",
        ],
        ("INFRA_OBSERVE", "win11-worker"): ["ai-lab-infrastructure-operations"],
        ("INFRA_CHANGE", "lab-server"): ["ai-lab-infrastructure-operations"],
        ("INFRA_CHANGE", "northwind-ubuntu-vm"): [
            "ai-lab-infrastructure-operations",
            "northwind-workflow",
        ],
        ("INFRA_CHANGE", "win11-worker"): ["ai-lab-infrastructure-operations"],
        ("EXTERNAL_ACTION", "mailbox"): ["client-mailbox-knowledge", "himalaya"],
        ("EXTERNAL_ACTION", "calendar"): ["google-workspace"],
        ("EXTERNAL_ACTION", "external"): ["personal-operations-orchestration"],
    }
    return mapping.get((mode, target), [])


def cmd_plan(args: argparse.Namespace) -> dict[str, Any]:
    mode = args.mode
    if mode not in MODES:
        raise BridgeError(f"Unsupported mode: {mode}")
    if args.target not in TARGETS:
        raise BridgeError(f"Unsupported target: {args.target}")
    if args.target not in MODE_TARGETS[mode]:
        raise BridgeError(f"Target {args.target} is incompatible with mode {mode}")
    task = read_text(Path(args.task_file), max_bytes=200_000).strip()
    if not task:
        raise BridgeError("Task file is empty")
    acceptance: list[str] = []
    if args.acceptance_file:
        acceptance = [
            line.strip()
            for line in read_text(Path(args.acceptance_file), max_bytes=100_000).splitlines()
            if line.strip()
        ]
    if not acceptance:
        acceptance = [
            "Return a clear status and evidence handles.",
            "State every side effect and unresolved blocker.",
            "Do not expose secrets or copy raw mailbox content.",
        ]
    skills = sorted(set(args.skill or default_skills(mode, args.target)))
    unsupported = sorted(set(skills) - MODE_ALLOWED_SKILLS[mode])
    if unsupported:
        raise BridgeError(
            f"Skills are incompatible with mode {mode}: {', '.join(unsupported)}"
        )
    manifest = None
    if mode in SENSITIVE_MODES:
        if not args.action_manifest:
            raise BridgeError("Sensitive mode requires --action-manifest")
        manifest = validate_action_manifest(read_json(Path(args.action_manifest)))
        validate_mode_action(mode, manifest)
    elif args.action_manifest:
        raise BridgeError("--action-manifest is allowed only for a sensitive mode")
    if args.workdir and ("\x00" in args.workdir or "\n" in args.workdir):
        raise BridgeError("Invalid workdir")
    if mode == "PREPARE_PATCH" and not args.workdir:
        raise BridgeError("PREPARE_PATCH requires --workdir")
    task_id = f"hdt-{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    contract: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "bridge_version": VERSION,
        "task_id": task_id,
        "created_at": iso_utc(),
        "requested_for": "Mateusz",
        "requesting_agent": args.requesting_agent,
        "mode": mode,
        "title": args.title.strip(),
        "task": task,
        "target": {
            "kind": args.target,
            "identifier": args.target_identifier or args.target,
            "workdir": args.workdir,
        },
        "acceptance_criteria": acceptance,
        "requested_skills": skills,
        "runtime": {
            "max_turns": args.max_turns,
            "timeout_seconds": args.timeout_seconds,
            "reasoning": args.reasoning,
        },
        "approval": {
            "required": mode in SENSITIVE_MODES,
            "policy": "explicit-owner-exact-contract",
            "action_manifest": manifest,
        },
        "delegation": {
            "depth": 0,
            "max_depth": 1,
            "return_to": args.requesting_agent,
        },
    }
    contract["contract_sha256"] = contract_hash(contract)
    validate_contract(contract)
    path = state_root() / "contracts" / f"{task_id}.json"
    atomic_write_json(path, contract)
    result = {
        "action": "plan",
        "status": "APPROVAL_REQUIRED" if mode in SENSITIVE_MODES else "READY_TO_SUBMIT",
        "task_id": task_id,
        "mode": mode,
        "contract_path": str(path),
        "contract_sha256": contract["contract_sha256"],
        "approval_required": mode in SENSITIVE_MODES,
        "writes_performed": 1,
    }
    if mode in SENSITIVE_MODES:
        result["approval_phrase"] = approval_phrase(contract)
    return result


def approval_phrase(contract: dict[str, Any]) -> str:
    return f"APPROVE HERMES {contract['task_id']} {contract['contract_sha256'][:12]}"


def approval_path(task_id: str) -> Path:
    return state_root() / "approvals" / f"{validate_task_id(task_id)}.json"


def cmd_approve(args: argparse.Namespace) -> dict[str, Any]:
    contract = validate_contract(read_json(Path(args.contract)))
    if not contract["approval"]["required"]:
        raise BridgeError("This contract does not require approval")
    expected = approval_phrase(contract)
    if args.phrase != expected:
        raise BridgeError("Approval phrase does not match the exact contract")
    if not 1 <= args.valid_minutes <= 1_440:
        raise BridgeError("valid_minutes must be between 1 and 1440")
    now = utc_now()
    receipt = {
        "schema_version": 1,
        "receipt_id": f"hap-{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}",
        "task_id": contract["task_id"],
        "contract_sha256": contract["contract_sha256"],
        "approved_by": "Mateusz",
        "approved_at": iso_utc(now),
        "valid_until": iso_utc(now + timedelta(minutes=args.valid_minutes)),
        "one_time": True,
        "consumed": False,
        "consumed_at": None,
        "action_manifest_sha256": sha256_bytes(
            canonical_bytes(contract["approval"]["action_manifest"])
        ),
    }
    path = approval_path(contract["task_id"])
    if path.exists():
        raise BridgeError(
            "An approval receipt already exists for this exact contract; "
            "create a new plan for any retry"
        )
    atomic_write_json(path, receipt)
    return {
        "action": "approve",
        "status": "APPROVED",
        "receipt_id": receipt["receipt_id"],
        "receipt_path": str(path),
        "task_id": contract["task_id"],
        "contract_sha256": contract["contract_sha256"],
        "valid_until": receipt["valid_until"],
        "writes_performed": 1,
    }


def parse_iso(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (AttributeError, ValueError) as exc:
        raise BridgeError("Invalid ISO-8601 timestamp") from exc


def validate_receipt(receipt: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "receipt_id",
        "task_id",
        "contract_sha256",
        "approved_by",
        "approved_at",
        "valid_until",
        "one_time",
        "consumed",
        "consumed_at",
        "action_manifest_sha256",
    }
    if set(receipt) != required:
        raise BridgeError("Approval receipt shape mismatch")
    if not RECEIPT_ID_RE.fullmatch(str(receipt["receipt_id"])):
        raise BridgeError("Approval receipt ID is invalid")
    if receipt["task_id"] != contract["task_id"] or receipt["contract_sha256"] != contract["contract_sha256"]:
        raise BridgeError("Approval receipt is bound to another contract")
    if receipt["approved_by"] != "Mateusz" or receipt["one_time"] is not True:
        raise BridgeError("Approval receipt identity/policy mismatch")
    if receipt["consumed"] is not False:
        raise BridgeError("Approval receipt has already been consumed")
    if parse_iso(receipt["valid_until"]) <= utc_now():
        raise BridgeError("Approval receipt has expired")
    expected_manifest = sha256_bytes(canonical_bytes(contract["approval"]["action_manifest"]))
    if receipt["action_manifest_sha256"] != expected_manifest:
        raise BridgeError("Approval action manifest mismatch")
    return receipt


def ssh_base(config: dict[str, Any]) -> list[str]:
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={int(config['ssh_connect_timeout_seconds'])}",
        "--",
        config["ssh_host"],
    ]


def run_process(
    argv: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 60,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            argv,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            shell=False,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise BridgeError(f"Executable not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"Command timed out after {timeout}s: {argv[0]}") from exc
    if completed.returncode != 0 and not allow_failure:
        stderr = completed.stderr.strip()[-2_000:]
        raise BridgeError(f"Command failed ({completed.returncode}): {stderr}")
    return completed


def remote_call(
    operation: str,
    *,
    payload: dict[str, Any] | None = None,
    extra: list[str] | None = None,
    timeout: int = 60,
    allow_failure: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    config = load_config()
    argv = ssh_base(config) + ["python3", config["remote_worker"], operation]
    if extra:
        argv.extend(extra)
    input_text = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    completed = run_process(argv, input_text=input_text, timeout=timeout, allow_failure=allow_failure)
    parsed = None
    if completed.stdout.strip():
        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError:
            if not allow_failure:
                raise BridgeError("Remote worker returned non-JSON output")
    return completed, parsed


def verify_remote_identity(value: dict[str, Any]) -> None:
    config = load_config()
    if value.get("hostname") != config["expected_hostname"]:
        raise BridgeError("Remote hostname mismatch")
    if value.get("user") != config["expected_user"]:
        raise BridgeError("Remote user mismatch")


def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    completed, remote = remote_call("doctor", timeout=30, allow_failure=True)
    if not remote:
        return {
            "action": "doctor",
            "status": "BLOCKED_WORKER",
            "error": completed.stderr.strip()[-2_000:] or "remote worker unavailable",
            "writes_performed": 0,
        }
    verify_remote_identity(remote)
    status = "READY_STATIC" if remote.get("status") == "READY_STATIC" else "BLOCKED"
    result: dict[str, Any] = {
        "action": "doctor",
        "status": status,
        "remote": remote,
        "writes_performed": 0,
    }
    if args.live and status == "READY_STATIC":
        _, live = remote_call("live-probe", timeout=90, allow_failure=True)
        result["live_probe"] = live
        result["status"] = live.get("status", "BLOCKED_LIVE") if live else "BLOCKED_LIVE"
    return result


def cmd_submit(args: argparse.Namespace) -> dict[str, Any]:
    contract = validate_contract(read_json(Path(args.contract)))
    receipt = None
    receipt_path = None
    if contract["approval"]["required"]:
        receipt_path = Path(args.approval_receipt) if args.approval_receipt else approval_path(contract["task_id"])
        if not receipt_path.is_file():
            raise BridgeError(f"Approval receipt is required: {receipt_path}")
        receipt = validate_receipt(read_json(receipt_path), contract)
    envelope = {
        "schema_version": 1,
        "client_version": VERSION,
        "contract": contract,
        "approval_receipt": receipt,
    }
    _, remote = remote_call("submit", payload=envelope, timeout=60)
    if not remote:
        raise BridgeError("Remote submit returned no result")
    verify_remote_identity(remote)
    if remote.get("task_id") != contract["task_id"] or remote.get("contract_sha256") != contract["contract_sha256"]:
        raise BridgeError("Remote acceptance identity mismatch")
    if remote.get("status") not in {"QUEUED", "RUNNING", "COMPLETED"}:
        raise BridgeError(f"Remote task was not accepted: {remote.get('status')}")
    if receipt is not None and receipt_path is not None:
        receipt["consumed"] = True
        receipt["consumed_at"] = iso_utc()
        atomic_write_json(receipt_path, receipt)
    run_receipt = {
        "schema_version": 1,
        "task_id": contract["task_id"],
        "contract_sha256": contract["contract_sha256"],
        "submitted_at": iso_utc(),
        "remote": remote,
        "approval_receipt_id": receipt.get("receipt_id") if receipt else None,
    }
    run_path = state_root() / "runs" / f"{contract['task_id']}.json"
    atomic_write_json(run_path, run_receipt)
    return {
        "action": "submit",
        "status": remote["status"],
        "task_id": contract["task_id"],
        "contract_sha256": contract["contract_sha256"],
        "unit": remote.get("unit"),
        "run_receipt": str(run_path),
        "approval_receipt_id": run_receipt["approval_receipt_id"],
        "writes_performed": 1 + (1 if receipt else 0),
    }


def task_operation(operation: str, task_id: str, timeout: int = 30) -> dict[str, Any]:
    validate_task_id(task_id)
    _, remote = remote_call(operation, extra=[task_id], timeout=timeout)
    if not remote:
        raise BridgeError(f"Remote {operation} returned no result")
    verify_remote_identity(remote)
    if remote.get("task_id") != task_id:
        raise BridgeError("Remote task ID mismatch")
    return remote


def cmd_task_op(args: argparse.Namespace) -> dict[str, Any]:
    operation = args.command
    remote = task_operation(operation, args.task_id)
    return {
        "action": operation,
        "status": remote.get("status"),
        "task_id": args.task_id,
        "remote": remote,
        "writes_performed": 1 if operation == "cancel" else 0,
    }


def cmd_list(args: argparse.Namespace) -> dict[str, Any]:
    _, remote = remote_call("list", extra=[str(args.limit)], timeout=30)
    if not remote:
        raise BridgeError("Remote list returned no result")
    verify_remote_identity(remote)
    return {"action": "list", **remote, "writes_performed": 0}


def remote_shell(command: str, *, timeout: int = 60, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
    return run_process(ssh_base(load_config()) + [command], timeout=timeout, allow_failure=allow_failure)


def deployment_approval_phrase(fingerprint: str) -> str:
    return f"APPROVE HERMES DEPLOY AILABSERVER {fingerprint[:12]}"


def cmd_deployment_plan(args: argparse.Namespace) -> dict[str, Any]:
    del args
    source = Path(__file__).with_name("hermes_remote_worker.py").resolve()
    if not source.is_file():
        raise BridgeError(f"Bundled remote worker is missing: {source}")
    fingerprint = sha256_file(source)
    return {
        "action": "deployment-plan",
        "status": "APPROVAL_REQUIRED",
        "target": "lab-server",
        "worker_sha256": fingerprint,
        "approval_phrase": deployment_approval_phrase(fingerprint),
        "effect": "Activate one user-scoped versioned Hermes delegation worker",
        "automatic_recovery": "Restore previous current symlink or exact absence on failure",
        "writes_performed": 0,
    }


def cmd_install_remote(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    source = Path(__file__).with_name("hermes_remote_worker.py").resolve()
    if not source.is_file():
        raise BridgeError(f"Bundled remote worker is missing: {source}")
    fingerprint = sha256_file(source)
    expected_approval = deployment_approval_phrase(fingerprint)
    if args.approval_phrase != expected_approval:
        raise BridgeError("Remote deployment requires the exact current approval phrase")
    transaction_id = f"hrd-{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    root = config["remote_root"]
    release_rel = f"releases/{transaction_id}"
    release_dir = f"{root}/{release_rel}"
    remote_file = f"{release_dir}/hermes_remote_worker.py"
    current = f"{root}/current"
    mkdir = f"umask 077; mkdir -p {shlex.quote(release_dir)} {shlex.quote(root + '/deployments')}"
    remote_shell(mkdir)
    run_process(
        [
            "scp",
            "-q",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(config['ssh_connect_timeout_seconds'])}",
            "--",
            str(source),
            f"{config['ssh_host']}:{remote_file}",
        ],
        timeout=120,
    )
    verify = remote_shell(
        f"chmod 700 {shlex.quote(remote_file)}; sha256sum {shlex.quote(remote_file)}",
        timeout=30,
    )
    observed = verify.stdout.strip().split()[0] if verify.stdout.strip() else ""
    if observed != fingerprint:
        raise BridgeError("Remote worker SHA-256 mismatch before activation")
    previous_check = remote_shell(
        f"if [ -L {shlex.quote(current)} ]; then readlink {shlex.quote(current)}; "
        "else printf '__ABSENT__'; fi",
        timeout=30,
    )
    previous_target = previous_check.stdout.strip() or "__ABSENT__"
    if previous_target != "__ABSENT__" and not re.fullmatch(
        r"releases/hrd-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}", previous_target
    ):
        raise BridgeError(
            f"Refusing activation over an unmanaged current target: {previous_target!r}"
        )
    temp_link = f"{root}/.current-{transaction_id}"
    activate = (
        f"ln -s {shlex.quote(release_rel)} {shlex.quote(temp_link)} && "
        f"mv -Tf {shlex.quote(temp_link)} {shlex.quote(current)}"
    )
    receipt = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "installed_at": iso_utc(),
        "ssh_host": config["ssh_host"],
        "remote_root": root,
        "release_target": release_rel,
        "previous_target": previous_target,
        "worker_sha256": fingerprint,
    }
    try:
        remote_shell(activate, timeout=30)
        _, doctor = remote_call("doctor", timeout=30)
        if not doctor:
            raise BridgeError("Activated worker failed doctor")
        verify_remote_identity(doctor)
        if doctor.get("status") != "READY_STATIC":
            raise BridgeError("Activated worker is not statically ready")
        if doctor.get("worker_sha256") != fingerprint:
            raise BridgeError("Activated worker fingerprint mismatch")
        remote_call("record-deployment", payload=receipt, timeout=30)
    except Exception:
        if previous_target == "__ABSENT__":
            rollback = (
                f"if [ -L {shlex.quote(current)} ]; then "
                f"unlink {shlex.quote(current)}; fi"
            )
        elif re.fullmatch(
            r"releases/hrd-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}", previous_target
        ):
            rollback_link = f"{root}/.rollback-{transaction_id}"
            rollback = (
                f"ln -s {shlex.quote(previous_target)} {shlex.quote(rollback_link)} && "
                f"mv -Tf {shlex.quote(rollback_link)} {shlex.quote(current)}"
            )
        else:
            raise BridgeError(
                "Deployment failed and previous target is unsafe for automatic rollback"
            )
        remote_shell(rollback, timeout=30, allow_failure=True)
        raise
    receipt_path = state_root() / "deployments" / f"{transaction_id}.json"
    atomic_write_json(receipt_path, receipt)
    return {
        "action": "install-remote",
        "status": "DEPLOYED",
        "transaction_id": transaction_id,
        "worker_sha256": fingerprint,
        "previous_target": previous_target,
        "current_target": release_rel,
        "receipt_path": str(receipt_path),
        "writes_performed": 1,
    }


def cmd_verify_remote(args: argparse.Namespace) -> dict[str, Any]:
    del args
    source = Path(__file__).with_name("hermes_remote_worker.py").resolve()
    expected = sha256_file(source)
    _, doctor = remote_call("doctor", timeout=30)
    if not doctor:
        raise BridgeError("Remote doctor returned no result")
    verify_remote_identity(doctor)
    status = "PASS" if doctor.get("worker_sha256") == expected else "FAIL"
    return {
        "action": "verify-remote",
        "status": status,
        "expected_worker_sha256": expected,
        "observed_worker_sha256": doctor.get("worker_sha256"),
        "remote": doctor,
        "writes_performed": 0,
    }


def cmd_rollback_remote(args: argparse.Namespace) -> dict[str, Any]:
    receipt = read_json(Path(args.receipt))
    required = {
        "schema_version",
        "transaction_id",
        "installed_at",
        "ssh_host",
        "remote_root",
        "release_target",
        "previous_target",
        "worker_sha256",
    }
    if set(receipt) != required:
        raise BridgeError("Deployment receipt shape mismatch")
    expected_approval = (
        f"APPROVE HERMES ROLLBACK {receipt['transaction_id']} "
        f"{str(receipt['worker_sha256'])[:12]}"
    )
    if args.approval_phrase != expected_approval:
        raise BridgeError("Remote rollback requires the exact receipt-bound approval phrase")
    config = load_config()
    if receipt["ssh_host"] != config["ssh_host"] or receipt["remote_root"] != config["remote_root"]:
        raise BridgeError("Deployment receipt target mismatch")
    root = config["remote_root"]
    current = f"{root}/current"
    observed = remote_shell(
        f"if [ -L {shlex.quote(current)} ]; then readlink {shlex.quote(current)}; "
        "else printf '__ABSENT__'; fi"
    ).stdout.strip()
    if observed != receipt["release_target"]:
        raise BridgeError(f"Refusing rollback: current target is {observed!r}")
    expected_release = f"releases/{receipt['transaction_id']}"
    if receipt["release_target"] != expected_release:
        raise BridgeError("Deployment receipt release target mismatch")
    if receipt["previous_target"] == "__ABSENT__":
        remote_shell(f"unlink {shlex.quote(current)}")
        restored = "__ABSENT__"
    else:
        previous = str(receipt["previous_target"])
        if not re.fullmatch(r"releases/hrd-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}", previous):
            raise BridgeError("Unsafe previous target in receipt")
        temp = f"{root}/.rollback-{receipt['transaction_id']}"
        remote_shell(
            f"ln -s {shlex.quote(previous)} {shlex.quote(temp)} && "
            f"mv -Tf {shlex.quote(temp)} {shlex.quote(current)}"
        )
        restored = previous
    return {
        "action": "rollback-remote",
        "status": "ROLLED_BACK",
        "transaction_id": receipt["transaction_id"],
        "restored_target": restored,
        "release_retained": receipt["release_target"],
        "writes_performed": 1,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--live", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    plan = sub.add_parser("plan")
    plan.add_argument("--title", required=True)
    plan.add_argument("--mode", required=True, choices=sorted(MODES))
    plan.add_argument("--target", required=True, choices=sorted(TARGETS))
    plan.add_argument("--target-identifier")
    plan.add_argument("--workdir")
    plan.add_argument("--task-file", required=True)
    plan.add_argument("--acceptance-file")
    plan.add_argument("--action-manifest")
    plan.add_argument("--skill", action="append")
    plan.add_argument("--requesting-agent", required=True)
    plan.add_argument("--max-turns", type=int, default=80)
    plan.add_argument("--timeout-seconds", type=int, default=3_600)
    plan.add_argument(
        "--reasoning",
        choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        default="medium",
    )
    plan.set_defaults(func=cmd_plan)

    approve = sub.add_parser("approve")
    approve.add_argument("--contract", required=True)
    approve.add_argument("--phrase", required=True)
    approve.add_argument("--valid-minutes", type=int, default=30)
    approve.set_defaults(func=cmd_approve)

    submit = sub.add_parser("submit")
    submit.add_argument("--contract", required=True)
    submit.add_argument("--approval-receipt")
    submit.set_defaults(func=cmd_submit)

    for name in ("status", "result", "cancel"):
        command = sub.add_parser(name)
        command.add_argument("--task-id", required=True)
        command.set_defaults(func=cmd_task_op)

    list_command = sub.add_parser("list")
    list_command.add_argument("--limit", type=int, default=20)
    list_command.set_defaults(func=cmd_list)

    deployment_plan = sub.add_parser("deployment-plan")
    deployment_plan.set_defaults(func=cmd_deployment_plan)

    install = sub.add_parser("install-remote")
    install.add_argument("--approval-phrase", required=True)
    install.set_defaults(func=cmd_install_remote)

    verify = sub.add_parser("verify-remote")
    verify.set_defaults(func=cmd_verify_remote)

    rollback = sub.add_parser("rollback-remote")
    rollback.add_argument("--receipt", required=True)
    rollback.add_argument("--approval-phrase", required=True)
    rollback.set_defaults(func=cmd_rollback_remote)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        result = args.func(args)
        emit(result)
        blocked = {
            "FAIL",
            "FAILED",
            "FAILED_FORMAT",
            "ERROR",
            "BLOCKED_WORKER",
            "BLOCKED",
            "BLOCKED_AUTH",
            "BLOCKED_APPROVAL",
            "BLOCKED_LIVE",
            "PARTIAL",
            "TIMED_OUT",
            "CANCELED",
        }
        return 0 if result.get("status") not in blocked else 1
    except BridgeError as exc:
        emit({"status": "ERROR", "error": str(exc)})
        return 1
    except KeyboardInterrupt:
        emit({"status": "CANCELED", "error": "Interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
