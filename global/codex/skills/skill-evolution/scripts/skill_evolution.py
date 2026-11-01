#!/usr/bin/env python3
"""Deterministic shadow-only skill evolution control plane."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # pragma: no cover - platform branch
    import msvcrt
except ImportError:  # pragma: no cover - platform branch
    msvcrt = None

try:  # pragma: no cover - platform branch
    import fcntl
except ImportError:  # pragma: no cover - platform branch
    fcntl = None


VERSION = "0.1.1"
SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 1_048_576
MAX_INPUT_JSON_DEPTH = 64
PUBLIC_COMMANDS = (
    "doctor",
    "init",
    "ingest",
    "compile-patterns",
    "register-candidate",
    "plan-verification",
    "record-verification",
    "decide",
    "report",
    "validate",
)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
SHA_RE = re.compile(r"^[a-f0-9]{64}$")
PATTERN_ID_RE = re.compile(r"^pattern-[a-f0-9]{16}$")
EVIDENCE_ID_RE = re.compile(r"^evidence-[a-z0-9][a-z0-9-]{2,95}$")
RUN_ID_RE = re.compile(r"^run-[a-z0-9][a-z0-9-]{2,95}$")
CANDIDATE_ID_RE = re.compile(r"^candidate-[a-z0-9][a-z0-9-]{2,95}$")
TEST_ID_RE = re.compile(r"^test-[a-z0-9][a-z0-9-]{2,95}$")

SIGNATURE_KEYS = (
    "task_family",
    "role",
    "model_family",
    "runtime",
    "tool_surface",
    "environment",
    "symptom",
    "expected_behavior",
    "outcome",
)
ACTOR_KEYS = ("actor_id", "provider_lineage")
CONTRACT_KEYS = (
    "schema_version",
    "run_id",
    "project_slug",
    "target_skill",
    "mode",
    "created_by",
    "planning_contract_sha256",
    "governance_contract_sha256",
    "graph_sha256",
    "max_evidence",
    "max_patterns",
    "max_candidates",
    "max_tests_per_phase",
)
EVIDENCE_KEYS = (
    "schema_version",
    "evidence_id",
    "source_run_id",
    "source_kind",
    "producer",
    "source_artifact_sha256",
    "signature",
)
CANDIDATE_KEYS = (
    "schema_version",
    "candidate_id",
    "target_skill",
    "pattern_ids",
    "applicability",
    "supported_model_families",
    "supported_runtimes",
    "expected_behavior",
    "candidate_sha256",
    "proposer",
)
CATALOG_KEYS = ("schema_version", "tests")
TEST_KEYS = (
    "test_id",
    "kind",
    "task_family",
    "model_family",
    "runtime",
    "tool_surface",
    "environment",
    "expected_behavior",
    "condition_sha256",
)
RESULT_KEYS = (
    "schema_version",
    "result_id",
    "candidate_id",
    "phase",
    "reviewer",
    "observations",
)
OBSERVATION_KEYS = (
    "test_id",
    "baseline_condition_sha256",
    "candidate_condition_sha256",
    "baseline_outcome",
    "candidate_outcome",
    "attributable",
    "behavior_preserved",
)

EXECUTABLE_MARKERS = (
    "powershell",
    "cmd.exe",
    "-cmd",
    "bash",
    "sh -c",
    "python -c",
    "curl ",
    "wget ",
    "invoke-expression",
    "start-process",
    "apply_patch",
    "git push",
    "git commit",
)
SECRET_MARKERS = (
    "api_key=",
    "apikey=",
    "authorization: bearer",
    "private key-----",
    "password=",
    "secret=",
    "token=",
    "sk-proj-",
)


class EvolutionError(Exception):
    """Expected fail-closed validation error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_bytes(value: Any) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return unicodedata.normalize("NFC", text).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(131_072)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def is_reparse_point(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _existing_components(path: Path) -> Iterator[Path]:
    current = path
    components: list[Path] = []
    while current != current.parent:
        components.append(current)
        current = current.parent
    components.append(current)
    yield from reversed(components)


def resolve_state_root(value: str | None) -> Path:
    raw = value or str(Path.home() / ".agents" / "skill-evolution" / "runs")
    normalized = raw.replace("\\", "/")
    if normalized.startswith("//"):
        raise EvolutionError("unsafe-state-root", "UNC state roots are forbidden")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise EvolutionError("unsafe-state-root", "state root must be absolute")
    if path == path.parent:
        raise EvolutionError("unsafe-state-root", "filesystem root is forbidden")
    for component in _existing_components(path):
        if component.exists() and is_reparse_point(component):
            raise EvolutionError("unsafe-state-root", "reparse state roots are forbidden")
    return path.resolve(strict=False)


def _resolve_input_file(value: str) -> Path:
    normalized = value.replace("\\", "/")
    if normalized.startswith("//"):
        raise EvolutionError("unsafe-input-path", "UNC input paths are forbidden")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise EvolutionError("unsafe-input-path", "input path must be absolute")
    for component in _existing_components(path):
        if component.exists() and is_reparse_point(component):
            raise EvolutionError("unsafe-input-path", "reparse input paths are forbidden")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise EvolutionError("input-not-file", "input must be a regular file")
    if resolved.stat().st_size > MAX_INPUT_BYTES:
        raise EvolutionError("input-too-large", "input exceeds the one MiB limit")
    return resolved


def load_json_file(value: str) -> dict[str, Any]:
    path = _resolve_input_file(value)
    try:
        raw = path.read_text(encoding="utf-8")
        # Python JSON recursion limits vary by version. Bound syntax depth
        # before parsing, while ignoring braces inside quoted/escaped text.
        depth, quoted, escaped = 0, False, False
        for char in raw:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > MAX_INPUT_JSON_DEPTH:
                    raise EvolutionError("invalid-json", "input is not canonical UTF-8 JSON: nesting limit exceeded")
            elif char in "]}":
                depth -= 1
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise EvolutionError("invalid-json", "input is not canonical UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise EvolutionError("schema-type", "top-level JSON value must be an object")
    return payload


def _exact_keys(value: dict[str, Any], expected: tuple[str, ...], label: str) -> None:
    if set(value) != set(expected):
        raise EvolutionError("schema-keys", f"{label} contains missing or unknown fields")


def _integer(value: Any, low: int, high: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise EvolutionError("schema-value", f"{label} is outside its bounded integer range")
    return value


def _identifier(value: Any, label: str, pattern: re.Pattern[str] = ID_RE) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise EvolutionError("schema-value", f"{label} has an invalid identifier")
    _safe_text(value)
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise EvolutionError("schema-value", f"{label} must be a lowercase SHA-256")
    return value


def _safe_text(value: str) -> None:
    lowered = value.lower()
    if any(marker in lowered for marker in EXECUTABLE_MARKERS):
        raise EvolutionError("executable-content", "executable content is forbidden")
    if any(marker in lowered for marker in SECRET_MARKERS):
        raise EvolutionError("secret-content", "secret-like content is forbidden")


def _string_list(value: Any, label: str, low: int, high: int) -> list[str]:
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise EvolutionError("schema-value", f"{label} has an invalid item count")
    result = [_identifier(item, label) for item in value]
    if len(set(result)) != len(result):
        raise EvolutionError("schema-value", f"{label} must contain unique values")
    return result


def _actor(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise EvolutionError("schema-type", f"{label} must be an object")
    _exact_keys(value, ACTOR_KEYS, label)
    return {
        "actor_id": _identifier(value["actor_id"], f"{label}.actor_id"),
        "provider_lineage": _identifier(
            value["provider_lineage"], f"{label}.provider_lineage"
        ),
    }


def validate_contract(value: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(value, CONTRACT_KEYS, "contract")
    if value["schema_version"] != SCHEMA_VERSION:
        raise EvolutionError("schema-version", "unsupported contract schema")
    if value["mode"] != "shadow-only":
        raise EvolutionError("authority", "only shadow-only mode is supported")
    run_id = _identifier(value["run_id"], "run_id", RUN_ID_RE)
    project_slug = _identifier(value["project_slug"], "project_slug", SLUG_RE)
    target_skill = _identifier(value["target_skill"], "target_skill", SLUG_RE)
    result = dict(value)
    result["run_id"] = run_id
    result["project_slug"] = project_slug
    result["target_skill"] = target_skill
    result["created_by"] = _actor(value["created_by"], "created_by")
    for key in (
        "planning_contract_sha256",
        "governance_contract_sha256",
        "graph_sha256",
    ):
        result[key] = _sha(value[key], key)
    result["max_evidence"] = _integer(value["max_evidence"], 2, 256, "max_evidence")
    result["max_patterns"] = _integer(value["max_patterns"], 1, 128, "max_patterns")
    result["max_candidates"] = _integer(value["max_candidates"], 1, 32, "max_candidates")
    result["max_tests_per_phase"] = _integer(
        value["max_tests_per_phase"], 5, 64, "max_tests_per_phase"
    )
    return result


def validate_evidence(value: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(value, EVIDENCE_KEYS, "evidence")
    if value["schema_version"] != SCHEMA_VERSION:
        raise EvolutionError("schema-version", "unsupported evidence schema")
    if value["source_kind"] not in {"agent-fleet", "agent-graph", "agent-loop", "acceptance"}:
        raise EvolutionError("schema-value", "unsupported evidence source kind")
    signature = value["signature"]
    if not isinstance(signature, dict):
        raise EvolutionError("schema-type", "signature must be an object")
    _exact_keys(signature, SIGNATURE_KEYS, "signature")
    clean_signature: dict[str, str] = {}
    for key in SIGNATURE_KEYS[:-1]:
        clean_signature[key] = _identifier(signature[key], f"signature.{key}")
    if signature["outcome"] not in {"success", "failure", "recovered"}:
        raise EvolutionError("schema-value", "invalid outcome")
    clean_signature["outcome"] = signature["outcome"]
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_id": _identifier(value["evidence_id"], "evidence_id", EVIDENCE_ID_RE),
        "source_run_id": _identifier(value["source_run_id"], "source_run_id", RUN_ID_RE),
        "source_kind": value["source_kind"],
        "producer": _actor(value["producer"], "producer"),
        "source_artifact_sha256": _sha(
            value["source_artifact_sha256"], "source_artifact_sha256"
        ),
        "signature": clean_signature,
    }


def validate_candidate(value: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(value, CANDIDATE_KEYS, "candidate")
    if value["schema_version"] != SCHEMA_VERSION:
        raise EvolutionError("schema-version", "unsupported candidate schema")
    pattern_ids = _string_list(value["pattern_ids"], "pattern_ids", 1, 64)
    if not all(PATTERN_ID_RE.fullmatch(item) for item in pattern_ids):
        raise EvolutionError("schema-value", "invalid pattern id")
    if value["applicability"] not in {
        "universal",
        "model-family",
        "runtime-specific",
        "task-specific",
    }:
        raise EvolutionError("schema-value", "invalid applicability")
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": _identifier(
            value["candidate_id"], "candidate_id", CANDIDATE_ID_RE
        ),
        "target_skill": _identifier(value["target_skill"], "target_skill", SLUG_RE),
        "pattern_ids": pattern_ids,
        "applicability": value["applicability"],
        "supported_model_families": _string_list(
            value["supported_model_families"], "supported_model_families", 1, 32
        ),
        "supported_runtimes": _string_list(
            value["supported_runtimes"], "supported_runtimes", 1, 32
        ),
        "expected_behavior": _identifier(
            value["expected_behavior"], "expected_behavior"
        ),
        "candidate_sha256": _sha(value["candidate_sha256"], "candidate_sha256"),
        "proposer": _actor(value["proposer"], "proposer"),
    }


def validate_catalog(value: dict[str, Any], maximum: int) -> dict[str, Any]:
    _exact_keys(value, CATALOG_KEYS, "catalog")
    if value["schema_version"] != SCHEMA_VERSION or not isinstance(value["tests"], list):
        raise EvolutionError("schema-value", "invalid catalog")
    if not 1 <= len(value["tests"]) <= maximum * 3:
        raise EvolutionError("schema-value", "catalog exceeds the bounded test count")
    clean: list[dict[str, Any]] = []
    ids: set[str] = set()
    kind_counts = {"targeted": 0, "preservation": 0, "confirmation": 0}
    for item in value["tests"]:
        if not isinstance(item, dict):
            raise EvolutionError("schema-type", "catalog test must be an object")
        _exact_keys(item, TEST_KEYS, "catalog test")
        test_id = _identifier(item["test_id"], "test_id", TEST_ID_RE)
        if test_id in ids:
            raise EvolutionError("duplicate-test", "catalog test ids must be unique")
        ids.add(test_id)
        kind = item["kind"]
        if kind not in kind_counts:
            raise EvolutionError("schema-value", "invalid test kind")
        kind_counts[kind] += 1
        if kind_counts[kind] > maximum:
            raise EvolutionError("schema-value", "test phase exceeds contract limit")
        clean.append(
            {
                "test_id": test_id,
                "kind": kind,
                "task_family": _identifier(item["task_family"], "task_family"),
                "model_family": _identifier(item["model_family"], "model_family"),
                "runtime": _identifier(item["runtime"], "runtime"),
                "tool_surface": _identifier(item["tool_surface"], "tool_surface"),
                "environment": _identifier(item["environment"], "environment"),
                "expected_behavior": _identifier(
                    item["expected_behavior"], "expected_behavior"
                ),
                "condition_sha256": _sha(item["condition_sha256"], "condition_sha256"),
            }
        )
    return {"schema_version": SCHEMA_VERSION, "tests": clean}


def validate_result(value: dict[str, Any]) -> dict[str, Any]:
    _exact_keys(value, RESULT_KEYS, "verification result")
    if value["schema_version"] != SCHEMA_VERSION:
        raise EvolutionError("schema-version", "unsupported result schema")
    if value["phase"] not in {"initial", "confirmation"}:
        raise EvolutionError("schema-value", "invalid verification phase")
    if not isinstance(value["observations"], list) or not 1 <= len(value["observations"]) <= 128:
        raise EvolutionError("schema-value", "invalid observation count")
    observations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value["observations"]:
        if not isinstance(item, dict):
            raise EvolutionError("schema-type", "observation must be an object")
        _exact_keys(item, OBSERVATION_KEYS, "observation")
        test_id = _identifier(item["test_id"], "test_id", TEST_ID_RE)
        if test_id in seen:
            raise EvolutionError("duplicate-test", "result test ids must be unique")
        seen.add(test_id)
        for key in ("baseline_outcome", "candidate_outcome"):
            if item[key] not in {"PASS", "FAIL", "BLOCKED"}:
                raise EvolutionError("schema-value", f"invalid {key}")
        for key in ("attributable", "behavior_preserved"):
            if not isinstance(item[key], bool):
                raise EvolutionError("schema-type", f"{key} must be boolean")
        observations.append(
            {
                "test_id": test_id,
                "baseline_condition_sha256": _sha(
                    item["baseline_condition_sha256"], "baseline_condition_sha256"
                ),
                "candidate_condition_sha256": _sha(
                    item["candidate_condition_sha256"], "candidate_condition_sha256"
                ),
                "baseline_outcome": item["baseline_outcome"],
                "candidate_outcome": item["candidate_outcome"],
                "attributable": item["attributable"],
                "behavior_preserved": item["behavior_preserved"],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "result_id": _identifier(value["result_id"], "result_id"),
        "candidate_id": _identifier(
            value["candidate_id"], "candidate_id", CANDIDATE_ID_RE
        ),
        "phase": value["phase"],
        "reviewer": _actor(value["reviewer"], "reviewer"),
        "observations": observations,
    }


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def state_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".skill-evolution.lock"
    with lock_path.open("a+b") as handle:
        if os.name == "nt":  # pragma: no cover - exercised on Windows integration
            if msvcrt is None:
                raise EvolutionError("locking-unavailable", "Windows locking is unavailable")
            if os.fstat(handle.fileno()).st_size == 0:
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - exercised on POSIX CI
            if fcntl is None:
                raise EvolutionError("locking-unavailable", "POSIX locking is unavailable")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":  # pragma: no cover
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _event_hash(event: dict[str, Any]) -> str:
    payload = dict(event)
    payload.pop("event_sha256", None)
    return sha256_value(payload)


def append_event(state: dict[str, Any], kind: str, object_id: str, payload: Any) -> None:
    if len(state["events"]) >= state["event_limit"]:
        raise EvolutionError("event-limit", "event budget is exhausted")
    previous = state["events"][-1]["event_sha256"] if state["events"] else "0" * 64
    event = {
        "sequence": len(state["events"]) + 1,
        "kind": kind,
        "object_id": object_id,
        "payload_sha256": sha256_value(payload),
        "previous_event_sha256": previous,
    }
    event["event_sha256"] = _event_hash(event)
    state["events"].append(event)


def validate_event_chain(state: dict[str, Any]) -> None:
    events = state.get("events")
    if not isinstance(events, list):
        raise EvolutionError("event-chain", "event chain is missing")
    previous = "0" * 64
    for sequence, event in enumerate(events, 1):
        if not isinstance(event, dict):
            raise EvolutionError("event-chain", "event is not an object")
        if event.get("sequence") != sequence or event.get("previous_event_sha256") != previous:
            raise EvolutionError("event-chain", "event order or predecessor is invalid")
        if event.get("event_sha256") != _event_hash(event):
            raise EvolutionError("event-chain", "event hash mismatch")
        previous = event["event_sha256"]


def _event_limit(contract: dict[str, Any]) -> int:
    return 2 + contract["max_evidence"] + (5 * contract["max_candidates"])


def _events_for(state: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [item for item in state["events"] if item["kind"] == kind]


def _assert_event_payload(
    state: dict[str, Any], kind: str, object_id: str, payload: Any
) -> None:
    matches = [
        item
        for item in state["events"]
        if item["kind"] == kind and item["object_id"] == object_id
    ]
    if len(matches) != 1 or matches[0]["payload_sha256"] != sha256_value(payload):
        raise EvolutionError("event-chain", f"{kind} payload is not bound to current state")


def validate_state_bindings(
    state: dict[str, Any], contract: dict[str, Any]
) -> None:
    if state.get("event_limit") != _event_limit(contract):
        raise EvolutionError("event-chain", "event limit does not match the contract")
    if len(state["events"]) > state["event_limit"]:
        raise EvolutionError("event-chain", "event count exceeds the contract")
    _assert_event_payload(state, "run-initialized", state["run_id"], contract)

    bindings = (
        ("evidence-ingested", "evidence", "evidence_id"),
        ("candidate-registered", "candidates", "candidate_id"),
        ("verification-recorded", "verification_results", "result_id"),
        ("shadow-decision-recorded", "decisions", "candidate_id"),
    )
    for kind, collection_name, identity_key in bindings:
        collection = state.get(collection_name)
        if not isinstance(collection, list) or len(_events_for(state, kind)) != len(collection):
            raise EvolutionError("event-chain", f"{collection_name} event count mismatch")
        for item in collection:
            _assert_event_payload(state, kind, item[identity_key], item)

    pattern_events = _events_for(state, "patterns-compiled")
    if len(pattern_events) > 1:
        raise EvolutionError("event-chain", "patterns may be compiled only once")
    if pattern_events:
        if pattern_events[0]["payload_sha256"] != sha256_value(state["patterns"]):
            raise EvolutionError("event-chain", "patterns are not bound to current state")
    elif state.get("patterns"):
        raise EvolutionError("event-chain", "patterns lack a compilation event")

    plans = state.get("verification_plans")
    catalogs = state.get("catalogs")
    if not isinstance(plans, list) or not isinstance(catalogs, list):
        raise EvolutionError("event-chain", "verification state is malformed")
    if len(plans) != len(catalogs) or len(_events_for(state, "verification-planned")) != len(plans):
        raise EvolutionError("event-chain", "verification plan event count mismatch")
    catalogs_by_candidate = {item["candidate_id"]: item for item in catalogs}
    if len(catalogs_by_candidate) != len(catalogs):
        raise EvolutionError("event-chain", "duplicate verification catalog")
    for plan in plans:
        catalog = catalogs_by_candidate.get(plan["candidate_id"])
        if catalog is None:
            raise EvolutionError("event-chain", "verification plan lacks its catalog")
        _assert_event_payload(
            state,
            "verification-planned",
            plan["plan_id"],
            {"plan": plan, "catalog": catalog},
        )

    expected_events = (
        1
        + len(state["evidence"])
        + len(pattern_events)
        + len(state["candidates"])
        + len(plans)
        + len(state["verification_results"])
        + len(state["decisions"])
    )
    if len(state["events"]) != expected_events:
        raise EvolutionError("event-chain", "event ledger contains an unknown or stale event")


def _run_paths(root: Path, run_id: str) -> tuple[Path, Path, Path]:
    _identifier(run_id, "run_id", RUN_ID_RE)
    run_dir = root / run_id
    return run_dir, run_dir / "contract.json", run_dir / "state.json"


def load_run(root: Path, run_id: str) -> tuple[dict[str, Any], dict[str, Any], Path]:
    run_dir, contract_path, state_path = _run_paths(root, run_id)
    if not contract_path.is_file() or not state_path.is_file():
        raise EvolutionError("run-not-found", "shadow run does not exist")
    contract = validate_contract(load_json_file(str(contract_path)))
    state = load_json_file(str(state_path))
    if not isinstance(state, dict) or state.get("run_id") != run_id:
        raise EvolutionError("state-invalid", "shadow state identity mismatch")
    if state.get("contract_sha256") != sha256_value(contract):
        raise EvolutionError("state-invalid", "contract hash mismatch")
    validate_event_chain(state)
    validate_state_bindings(state, contract)
    return contract, state, state_path


def save_state(path: Path, state: dict[str, Any], contract: dict[str, Any]) -> None:
    validate_event_chain(state)
    validate_state_bindings(state, contract)
    atomic_json(path, state)


def command_doctor(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_state_root(args.state_root)
    return {
        "status": "READY",
        "runtime_version": VERSION,
        "mode": "shadow-only",
        "state_root": str(root),
        "writes_performed": 0,
        "release_authority": False,
    }


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_state_root(args.state_root)
    contract = validate_contract(load_json_file(args.contract))
    run_id = contract["run_id"]
    run_dir, _, _ = _run_paths(root, run_id)
    with state_lock(root):
        if run_dir.exists():
            raise EvolutionError("run-exists", "shadow run already exists")
        staging = root / f".{run_id}.{os.getpid()}.staging"
        if staging.exists():
            raise EvolutionError("staging-exists", "staging path already exists")
        staging.mkdir()
        state = {
            "schema_version": SCHEMA_VERSION,
            "runtime_version": VERSION,
            "run_id": run_id,
            "contract_sha256": sha256_value(contract),
            "event_limit": _event_limit(contract),
            "evidence": [],
            "patterns": [],
            "candidates": [],
            "catalogs": [],
            "verification_plans": [],
            "verification_results": [],
            "decisions": [],
            "events": [],
        }
        append_event(state, "run-initialized", run_id, contract)
        try:
            atomic_json(staging / "contract.json", contract)
            atomic_json(staging / "state.json", state)
            os.replace(staging, run_dir)
            _fsync_directory(root)
        finally:
            if staging.exists():
                for child in staging.iterdir():
                    child.unlink()
                staging.rmdir()
    return {
        "status": "INITIALIZED",
        "run_id": run_id,
        "contract_sha256": sha256_value(contract),
        "release_authority": False,
    }


def command_ingest(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_state_root(args.state_root)
    receipt = validate_evidence(load_json_file(args.receipt))
    with state_lock(root):
        contract, state, state_path = load_run(root, args.run_id)
        if len(state["evidence"]) >= contract["max_evidence"]:
            raise EvolutionError("evidence-limit", "evidence budget is exhausted")
        if any(item["evidence_id"] == receipt["evidence_id"] for item in state["evidence"]):
            raise EvolutionError("duplicate-evidence", "evidence id already exists")
        if any(
            item["source_artifact_sha256"] == receipt["source_artifact_sha256"]
            for item in state["evidence"]
        ):
            raise EvolutionError("duplicate-evidence", "source artifact is already recorded")
        state["evidence"].append(receipt)
        state["evidence"].sort(key=lambda item: item["evidence_id"])
        append_event(state, "evidence-ingested", receipt["evidence_id"], receipt)
        save_state(state_path, state, contract)
    return {"status": "INGESTED", "evidence_id": receipt["evidence_id"]}


def command_compile_patterns(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_state_root(args.state_root)
    with state_lock(root):
        contract, state, state_path = load_run(root, args.run_id)
        if _events_for(state, "patterns-compiled"):
            raise EvolutionError("patterns-final", "patterns are already compiled for this run")
        groups: dict[bytes, list[dict[str, Any]]] = {}
        for receipt in state["evidence"]:
            groups.setdefault(canonical_bytes(receipt["signature"]), []).append(receipt)
        if len(groups) > contract["max_patterns"]:
            raise EvolutionError("pattern-limit", "pattern budget is exhausted")
        patterns: list[dict[str, Any]] = []
        for signature_bytes in sorted(groups):
            receipts = groups[signature_bytes]
            signature = receipts[0]["signature"]
            evidence_ids = sorted(item["evidence_id"] for item in receipts)
            runs = sorted({item["source_run_id"] for item in receipts})
            actors = sorted({item["producer"]["actor_id"] for item in receipts})
            lineages = sorted({item["producer"]["provider_lineage"] for item in receipts})
            models = sorted({item["signature"]["model_family"] for item in receipts})
            runtimes = sorted({item["signature"]["runtime"] for item in receipts})
            base = {
                "schema_version": SCHEMA_VERSION,
                "signature": signature,
                "evidence_ids": evidence_ids,
                "independent_run_count": len(runs),
                "independent_actor_count": len(actors),
                "supported_model_families": models,
                "supported_runtimes": runtimes,
                "applicability": "task-specific",
                "status": (
                    "ELIGIBLE"
                    if len(runs) >= 2 and len(actors) >= 2 and len(lineages) >= 2
                    else "NEEDS_EVIDENCE"
                ),
            }
            pattern_id = f"pattern-{sha256_value(base)[:16]}"
            pattern = {"pattern_id": pattern_id, **base}
            pattern["pattern_sha256"] = sha256_value(pattern)
            patterns.append(pattern)
        state["patterns"] = sorted(patterns, key=lambda item: item["pattern_id"])
        append_event(state, "patterns-compiled", args.run_id, state["patterns"])
        save_state(state_path, state, contract)
    return {
        "status": "PATTERNS_COMPILED",
        "pattern_count": len(patterns),
        "eligible_count": sum(item["status"] == "ELIGIBLE" for item in patterns),
    }


def command_register_candidate(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_state_root(args.state_root)
    candidate = validate_candidate(load_json_file(args.candidate))
    with state_lock(root):
        contract, state, state_path = load_run(root, args.run_id)
        if candidate["target_skill"] != contract["target_skill"]:
            raise EvolutionError("target-mismatch", "candidate targets another skill")
        if len(state["candidates"]) >= contract["max_candidates"]:
            raise EvolutionError("candidate-limit", "candidate budget is exhausted")
        if any(item["candidate_id"] == candidate["candidate_id"] for item in state["candidates"]):
            raise EvolutionError("duplicate-candidate", "candidate id already exists")
        patterns = {item["pattern_id"]: item for item in state["patterns"]}
        selected = []
        for pattern_id in candidate["pattern_ids"]:
            pattern = patterns.get(pattern_id)
            if not pattern or pattern["status"] != "ELIGIBLE":
                raise EvolutionError("unsupported-candidate", "candidate cites ineligible evidence")
            if pattern["signature"]["expected_behavior"] != candidate["expected_behavior"]:
                raise EvolutionError("unsupported-candidate", "pattern behavior mismatch")
            selected.append(pattern)
        models = {model for item in selected for model in item["supported_model_families"]}
        runtimes = {runtime for item in selected for runtime in item["supported_runtimes"]}
        if not set(candidate["supported_model_families"]).issubset(models):
            raise EvolutionError("unsupported-candidate", "unsupported model family claim")
        if not set(candidate["supported_runtimes"]).issubset(runtimes):
            raise EvolutionError("unsupported-candidate", "unsupported runtime claim")
        if candidate["applicability"] == "universal" and (
            len(models) < 2
            or len(runtimes) < 2
            or len(candidate["supported_model_families"]) < 2
            or len(candidate["supported_runtimes"]) < 2
        ):
            raise EvolutionError(
                "unsupported-universal",
                "universal candidates require two model families and two runtimes",
            )
        state["candidates"].append(candidate)
        state["candidates"].sort(key=lambda item: item["candidate_id"])
        append_event(state, "candidate-registered", candidate["candidate_id"], candidate)
        save_state(state_path, state, contract)
    return {"status": "CANDIDATE_REGISTERED", "candidate_id": candidate["candidate_id"]}


def _candidate(state: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    for item in state["candidates"]:
        if item["candidate_id"] == candidate_id:
            return item
    raise EvolutionError("candidate-not-found", "candidate does not exist")


def _coverage(tests: list[dict[str, Any]]) -> tuple[int, int]:
    return len({item["model_family"] for item in tests}), len({item["runtime"] for item in tests})


def command_plan_verification(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_state_root(args.state_root)
    with state_lock(root):
        contract, state, state_path = load_run(root, args.run_id)
        candidate = _candidate(state, args.candidate_id)
        if any(item["candidate_id"] == candidate["candidate_id"] for item in state["decisions"]):
            raise EvolutionError("decision-final", "candidate already has a final shadow decision")
        if any(item["candidate_id"] == candidate["candidate_id"] for item in state["verification_plans"]):
            raise EvolutionError("plan-final", "candidate already has a verification plan")
        catalog = validate_catalog(load_json_file(args.catalog), contract["max_tests_per_phase"])
        tests = catalog["tests"]
        targeted = sorted(
            (
                item
                for item in tests
                if item["kind"] == "targeted"
                and item["expected_behavior"] == candidate["expected_behavior"]
            ),
            key=lambda item: item["test_id"],
        )[:5]
        preservation = sorted(
            (item for item in tests if item["kind"] == "preservation"),
            key=lambda item: item["test_id"],
        )[:3]
        confirmation = sorted(
            (
                item
                for item in tests
                if item["kind"] == "confirmation"
                and item["expected_behavior"] == candidate["expected_behavior"]
            ),
            key=lambda item: item["test_id"],
        )[:5]
        reasons: list[str] = []
        if len(targeted) < 5:
            reasons.append("targeted-tests-missing")
        if len(preservation) < 3:
            reasons.append("preservation-tests-missing")
        if len(confirmation) < 5:
            reasons.append("confirmation-tests-missing")
        targeted_ids = [item["test_id"] for item in targeted]
        confirmation_ids = [item["test_id"] for item in confirmation]
        overlap = len(set(targeted_ids) & set(confirmation_ids))
        if overlap > 2:
            reasons.append("confirmation-overlap")
        if candidate["applicability"] == "universal":
            for label, phase_tests in (("targeted", targeted), ("confirmation", confirmation)):
                models, runtimes = _coverage(phase_tests)
                if models < 2 or runtimes < 2:
                    reasons.append(f"{label}-coverage-missing")
        catalog_hash = sha256_value(catalog)
        base = {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": candidate["candidate_id"],
            "targeted_test_ids": targeted_ids,
            "preservation_test_ids": [item["test_id"] for item in preservation],
            "confirmation_test_ids": confirmation_ids,
            "confirmation_overlap_count": overlap,
            "status": "NEEDS_EVIDENCE" if reasons else "READY",
            "reason_codes": sorted(set(reasons)),
            "catalog_sha256": catalog_hash,
        }
        plan_id = f"verification-{sha256_value(base)[:16]}"
        plan = {"plan_id": plan_id, **base}
        plan["plan_sha256"] = sha256_value(plan)
        state["verification_plans"] = [
            item for item in state["verification_plans"] if item["candidate_id"] != candidate["candidate_id"]
        ]
        state["verification_plans"].append(plan)
        state["verification_plans"].sort(key=lambda item: item["candidate_id"])
        state["catalogs"] = [
            item for item in state["catalogs"] if item["candidate_id"] != candidate["candidate_id"]
        ]
        state["catalogs"].append(
            {"candidate_id": candidate["candidate_id"], "catalog_sha256": catalog_hash, "tests": tests}
        )
        state["catalogs"].sort(key=lambda item: item["candidate_id"])
        catalog_record = {
            "candidate_id": candidate["candidate_id"],
            "catalog_sha256": catalog_hash,
            "tests": tests,
        }
        append_event(
            state,
            "verification-planned",
            plan_id,
            {"plan": plan, "catalog": catalog_record},
        )
        save_state(state_path, state, contract)
    return {
        "status": "VERIFICATION_PLANNED",
        "plan_id": plan_id,
        "plan_status": plan["status"],
        "reason_codes": plan["reason_codes"],
    }


def _plan(state: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    for item in state["verification_plans"]:
        if item["candidate_id"] == candidate_id:
            return item
    raise EvolutionError("plan-not-found", "verification plan does not exist")


def _catalog(state: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    for item in state["catalogs"]:
        if item["candidate_id"] == candidate_id:
            return item
    raise EvolutionError("catalog-not-found", "verification catalog does not exist")


def command_record_verification(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_state_root(args.state_root)
    result = validate_result(load_json_file(args.result))
    with state_lock(root):
        contract, state, state_path = load_run(root, args.run_id)
        candidate = _candidate(state, result["candidate_id"])
        if any(item["candidate_id"] == result["candidate_id"] for item in state["decisions"]):
            raise EvolutionError("decision-final", "candidate already has a final shadow decision")
        plan = _plan(state, result["candidate_id"])
        if plan["status"] != "READY":
            raise EvolutionError("plan-not-ready", "verification plan needs more evidence")
        proposer = candidate["proposer"]
        reviewer = result["reviewer"]
        if (
            reviewer["actor_id"] == proposer["actor_id"]
            or reviewer["provider_lineage"] == proposer["provider_lineage"]
        ):
            raise EvolutionError("self-review", "maker and reviewer must be independent")
        if any(item["result_id"] == result["result_id"] for item in state["verification_results"]):
            raise EvolutionError("duplicate-result", "result id already exists")
        if any(
            item["candidate_id"] == result["candidate_id"] and item["phase"] == result["phase"]
            for item in state["verification_results"]
        ):
            raise EvolutionError("duplicate-phase", "candidate phase is already recorded")
        expected = (
            plan["targeted_test_ids"]
            if result["phase"] == "initial"
            else plan["confirmation_test_ids"]
        ) + plan["preservation_test_ids"]
        actual = [item["test_id"] for item in result["observations"]]
        if len(actual) != len(expected) or set(actual) != set(expected):
            raise EvolutionError("incomplete-result", "result must cover the exact planned phase")
        catalog = _catalog(state, result["candidate_id"])
        by_id = {item["test_id"]: item for item in catalog["tests"]}
        for observation in result["observations"]:
            condition = by_id[observation["test_id"]]["condition_sha256"]
            if (
                observation["baseline_condition_sha256"] != condition
                or observation["candidate_condition_sha256"] != condition
            ):
                raise EvolutionError("condition-mismatch", "baseline and candidate conditions differ")
        state["verification_results"].append(result)
        state["verification_results"].sort(key=lambda item: (item["candidate_id"], item["phase"]))
        append_event(state, "verification-recorded", result["result_id"], result)
        save_state(state_path, state, contract)
    return {"status": "VERIFICATION_RECORDED", "result_id": result["result_id"]}


def command_decide(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_state_root(args.state_root)
    with state_lock(root):
        contract, state, state_path = load_run(root, args.run_id)
        _candidate(state, args.candidate_id)
        if any(item["candidate_id"] == args.candidate_id for item in state["decisions"]):
            raise EvolutionError("decision-final", "candidate already has a final shadow decision")
        plans = [item for item in state["verification_plans"] if item["candidate_id"] == args.candidate_id]
        results = [item for item in state["verification_results"] if item["candidate_id"] == args.candidate_id]
        reasons: list[str] = []
        decision = "NEEDS_EVIDENCE"
        if not plans or plans[0]["status"] != "READY":
            reasons.append("verification-plan-incomplete")
        elif {item["phase"] for item in results} != {"initial", "confirmation"}:
            reasons.append("paired-verification-incomplete")
        else:
            observations = [item for result in results for item in result["observations"]]
            regressions = [
                item
                for item in observations
                if item["attributable"]
                and item["baseline_outcome"] == "PASS"
                and item["candidate_outcome"] == "FAIL"
            ]
            preservation_failures = [
                item
                for item in observations
                if item["test_id"] in plans[0]["preservation_test_ids"]
                and (item["candidate_outcome"] != "PASS" or not item["behavior_preserved"])
            ]
            recoveries = [
                item
                for item in observations
                if item["attributable"]
                and item["baseline_outcome"] == "FAIL"
                and item["candidate_outcome"] == "PASS"
            ]
            if regressions:
                decision = "REJECTED"
                reasons.append("attributable-regression")
            elif preservation_failures:
                decision = "REJECTED"
                reasons.append("preservation-regression")
            elif not recoveries:
                decision = "REJECTED"
                reasons.append("no-attributable-improvement")
            else:
                decision = "SHADOW_ACCEPTED"
                reasons.append("paired-evidence-accepted")
        record = {
            "candidate_id": args.candidate_id,
            "decision": decision,
            "reason_codes": sorted(set(reasons)),
            "release_authority": False,
        }
        state["decisions"] = [
            item for item in state["decisions"] if item["candidate_id"] != args.candidate_id
        ]
        state["decisions"].append(record)
        state["decisions"].sort(key=lambda item: item["candidate_id"])
        append_event(state, "shadow-decision-recorded", args.candidate_id, record)
        save_state(state_path, state, contract)
    return record


def command_report(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_state_root(args.state_root)
    contract, state, _state_path = load_run(root, args.run_id)
    return {
        "status": "REPORTED",
        "run_id": args.run_id,
        "target_skill": contract["target_skill"],
        "mode": contract["mode"],
        "counts": {
            "evidence": len(state["evidence"]),
            "patterns": len(state["patterns"]),
            "candidates": len(state["candidates"]),
            "verification_results": len(state["verification_results"]),
            "decisions": len(state["decisions"]),
        },
        "decisions": state["decisions"],
        "release_authority": False,
        "writes_performed": 0,
    }


def command_validate(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_state_root(args.state_root)
    contract, state, _state_path = load_run(root, args.run_id)
    if state["runtime_version"] != VERSION or state["schema_version"] != SCHEMA_VERSION:
        raise EvolutionError("state-invalid", "runtime or schema version mismatch")
    return {
        "status": "VALID",
        "run_id": args.run_id,
        "contract_sha256": sha256_value(contract),
        "event_count": len(state["events"]),
        "release_authority": False,
        "writes_performed": 0,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--state-root")
    doctor.set_defaults(handler=command_doctor)

    init = commands.add_parser("init")
    init.add_argument("--contract", required=True)
    init.add_argument("--state-root")
    init.set_defaults(handler=command_init)

    for name, handler in (
        ("ingest", command_ingest),
        ("compile-patterns", command_compile_patterns),
        ("register-candidate", command_register_candidate),
        ("plan-verification", command_plan_verification),
        ("record-verification", command_record_verification),
        ("decide", command_decide),
        ("report", command_report),
        ("validate", command_validate),
    ):
        command = commands.add_parser(name)
        command.add_argument("--run-id", required=True)
        command.add_argument("--state-root")
        if name == "ingest":
            command.add_argument("--receipt", required=True)
        elif name == "register-candidate":
            command.add_argument("--candidate", required=True)
        elif name == "plan-verification":
            command.add_argument("--candidate-id", required=True)
            command.add_argument("--catalog", required=True)
        elif name == "record-verification":
            command.add_argument("--result", required=True)
        elif name == "decide":
            command.add_argument("--candidate-id", required=True)
        command.set_defaults(handler=handler)
    return parser


def _emit(value: dict[str, Any], error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(json.dumps(value, ensure_ascii=True, sort_keys=True) + "\n")


def get_focused_trials_helper():
    """Return supporting focused_trials module if available."""
    try:
        import focused_trials
        return focused_trials
    except ImportError:
        return None


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        payload = args.handler(args)
        _emit(payload)
        return 0
    except EvolutionError as exc:
        _emit({"status": "ERROR", "code": exc.code, "message": exc.message}, error=True)
        return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _emit(
            {"status": "ERROR", "code": "internal-validation", "message": str(exc)},
            error=True,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
