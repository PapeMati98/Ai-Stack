from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from typing import Any


VERSION = "0.3.5"
SKILL_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_AUDIT_SEED = SKILL_ROOT / "MODEL-MARKET-AUDIT.md"
BUNDLED_BENCHMARK_SEED = SKILL_ROOT / "MODEL-BENCHMARK-SNAPSHOT.json"
STATE_ROOT_ENV = "AGENT_FLEET_MODEL_RADAR_STATE_ROOT"
DEFAULT_PROFILES = (
    SKILL_ROOT.parent / "agent-fleet" / "references" / "profiles.json"
)
DEFAULT_LOCAL_SUITE = SKILL_ROOT / "references" / "local-benchmark-suite.json"
REQUIRED_SECTIONS = (
    "## Current routing snapshot",
    "## Evidence and uncertainty",
    "## Primary sources",
    "## Update log",
)
ARENA_DATASET = "lmarena-ai/leaderboard-dataset"
ARENA_DATASET_URL = "https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset"
VALID_LIFECYCLES = {
    "DISCOVERED",
    "PROFILED",
    "CANARY",
    "LIMITED",
    "ACTIVE",
    "DEPRECATED",
}
VALID_LOCAL_KINDS = {
    "architecture",
    "debugging",
    "docs",
    "frontend",
    "implementation",
    "large-context",
    "multimodal",
    "repeatable",
    "research",
    "review",
}


class AuditError(RuntimeError):
    pass


def default_state_root() -> Path:
    configured = os.environ.get(STATE_ROOT_ENV)
    root = Path(configured).expanduser() if configured else Path.home() / ".agents" / "model-radar"
    return Path(os.path.abspath(root))


def state_paths(root: Path) -> tuple[Path, Path]:
    resolved = Path(os.path.abspath(root.expanduser()))
    return (
        resolved / "MODEL-MARKET-AUDIT.md",
        resolved / "MODEL-BENCHMARK-SNAPSHOT.json",
    )


def is_filesystem_link(path: Path) -> bool:
    metadata = path.lstat()
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def ensure_plain_state_root(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.exists():
        if not expanded.is_dir() or is_filesystem_link(expanded):
            raise AuditError(f"Model Radar state root is not a plain directory: {expanded}")
    else:
        expanded.mkdir(parents=True)
    return expanded.resolve()


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True) + "\n").encode(
        "ascii"
    )


def parse_timestamp(value: str) -> dt.datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AuditError(f"Invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise AuditError("timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AuditError("Audit must start with YAML-like frontmatter")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise AuditError("Audit frontmatter is not closed") from exc
    values: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition(":")
        if not separator or not key.strip() or not value.strip():
            raise AuditError(f"Invalid frontmatter line: {line}")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def current_utc(now: dt.datetime | None) -> dt.datetime:
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(dt.timezone.utc)


def freshness_fields(
    timestamp: dt.datetime,
    *,
    valid_for_days: int,
    now: dt.datetime | None,
) -> dict[str, Any]:
    current = current_utc(now)
    if timestamp > current + dt.timedelta(minutes=5):
        raise AuditError("timestamp-is-in-the-future")
    age = max(dt.timedelta(0), current - timestamp)
    fresh = age <= dt.timedelta(days=valid_for_days)
    return {
        "fresh": fresh,
        "refresh_required": not fresh,
        "last_verified_at": timestamp.isoformat().replace("+00:00", "Z"),
        "valid_until": (timestamp + dt.timedelta(days=valid_for_days))
        .isoformat()
        .replace("+00:00", "Z"),
        "age_days": round(age.total_seconds() / 86400, 4),
        "effective_max_age_days": valid_for_days,
    }


def inspect_audit(
    path: Path,
    *,
    max_age_days: int = 7,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    base = {
        "component": "model-market-audit",
        "audit_path": str(resolved),
        "max_age_days": max_age_days,
    }
    if not resolved.is_file():
        return {
            **base,
            "status": "MISSING",
            "fresh": False,
            "refresh_required": True,
            "error": "audit-file-not-found",
        }
    try:
        raw = resolved.read_bytes()
        text = raw.decode("utf-8")
        frontmatter = parse_frontmatter(text)
        if frontmatter.get("schema_version") != "1":
            raise AuditError("schema_version must be 1")
        declared_days = int(frontmatter.get("valid_for_days", "7"))
        if declared_days < 1 or declared_days > 7:
            raise AuditError("valid_for_days must be between 1 and 7")
        effective_days = min(max_age_days, declared_days, 7)
        verified_at = parse_timestamp(frontmatter["last_verified_at"])
        missing_sections = [item for item in REQUIRED_SECTIONS if item not in text]
        if missing_sections:
            raise AuditError("Missing sections: " + ", ".join(missing_sections))
        source_count = len(set(re.findall(r"https://[^\s)>]+", text)))
        if source_count < 6:
            raise AuditError("Audit must contain at least six primary-source URLs")
        freshness = freshness_fields(
            verified_at, valid_for_days=effective_days, now=now
        )
        # Registry verification must not renew unrelated market evidence.
        registry: dict[str, Any] = {}
        if "registry_sha256" in frontmatter or "registry_verified_at" in frontmatter:
            digest = frontmatter.get("registry_sha256", "")
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise AuditError("registry-sha256-missing-or-invalid")
            registry_time = parse_timestamp(frontmatter["registry_verified_at"])
            registry_freshness = freshness_fields(
                registry_time, valid_for_days=effective_days, now=now
            )
            registry = {
                "registry_sha256": digest,
                "registry_verified_at": registry_freshness["last_verified_at"],
                "registry_fresh": registry_freshness["fresh"],
                "registry_valid_until": registry_freshness["valid_until"],
            }
    except (AuditError, KeyError, OSError, UnicodeDecodeError, ValueError) as exc:
        return {
            **base,
            "status": "INVALID",
            "fresh": False,
            "refresh_required": True,
            "error": str(exc),
        }
    return {
        **base,
        "status": "FRESH" if freshness["fresh"] else "STALE",
        **freshness,
        **registry,
        "source_count": source_count,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "error": None,
    }


def inspect_benchmark_snapshot(
    path: Path,
    *,
    max_age_days: int = 7,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    base = {
        "component": "arena-benchmark-snapshot",
        "snapshot_path": str(resolved),
        "max_age_days": max_age_days,
    }
    if not resolved.is_file():
        return {
            **base,
            "status": "MISSING",
            "fresh": False,
            "refresh_required": True,
            "error": "benchmark-snapshot-not-found",
        }
    try:
        raw = resolved.read_bytes()
        document = json.loads(raw.decode("ascii"))
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise AuditError("benchmark schema_version must be 1")
        source = document.get("source")
        if not isinstance(source, dict) or source.get("dataset") != ARENA_DATASET:
            raise AuditError(f"benchmark dataset must be {ARENA_DATASET}")
        if source.get("dataset_url") != ARENA_DATASET_URL:
            raise AuditError("benchmark dataset_url is not the official Arena dataset")
        if source.get("split") != "latest":
            raise AuditError("benchmark split must be latest")
        configs = document.get("configs")
        models = document.get("models")
        if not isinstance(configs, dict) or not configs:
            raise AuditError("benchmark snapshot has no configs")
        if not isinstance(models, dict) or not models:
            raise AuditError("benchmark snapshot has no mapped models")
        declared_days = int(document.get("valid_for_days", 7))
        if declared_days < 1 or declared_days > 7:
            raise AuditError("benchmark valid_for_days must be between 1 and 7")
        effective_days = min(max_age_days, declared_days, 7)
        generated_at = parse_timestamp(str(document["generated_at"]))
        freshness = freshness_fields(
            generated_at, valid_for_days=effective_days, now=now
        )
        matched = 0
        missing = 0
        for model in models.values():
            if not isinstance(model, dict) or not isinstance(model.get("benchmarks"), dict):
                raise AuditError("benchmark model entry is invalid")
            for metric in model["benchmarks"].values():
                if not isinstance(metric, dict) or metric.get("status") not in {
                    "matched",
                    "missing",
                }:
                    raise AuditError("benchmark metric entry is invalid")
                matched += int(metric["status"] == "matched")
                missing += int(metric["status"] == "missing")
    except (AuditError, KeyError, OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        return {
            **base,
            "status": "INVALID",
            "fresh": False,
            "refresh_required": True,
            "error": str(exc),
        }
    return {
        **base,
        "status": "FRESH" if freshness["fresh"] else "STALE",
        **freshness,
        "config_count": len(configs),
        "model_count": len(models),
        "matched_metric_count": matched,
        "missing_metric_count": missing,
        "fetch_warning_count": len(document.get("fetch_warnings", [])),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "error": None,
    }


def inspect_execution_profiles(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    base = {
        "component": "execution-profile-registry",
        "profiles_path": str(resolved),
    }
    if not resolved.is_file():
        return {**base, "status": "MISSING", "valid": False, "error": "profiles-file-not-found"}
    try:
        raw = resolved.read_bytes()
        document = json.loads(raw.decode("utf-8"))
        schema_version = document.get("schema_version") if isinstance(document, dict) else None
        if schema_version not in {4, 5}:
            raise AuditError("execution profile registry schema_version must be 4 or 5")
        profiles = document.get("profiles")
        if not isinstance(profiles, dict) or not profiles:
            raise AuditError("execution profile registry has no CLI profiles")
        lifecycle_counts = {item: 0 for item in sorted(VALID_LIFECYCLES)}
        execution_ids: set[str] = set()
        mapped_ids: set[str] = set()
        for profile_id, profile in profiles.items():
            if not isinstance(profile, dict):
                raise AuditError(f"CLI profile {profile_id} is invalid")
            if schema_version >= 5 and not str(profile.get("cli_provider_id", "")).strip():
                raise AuditError(f"CLI profile {profile_id} misses cli_provider_id")
            execution_profiles = profile.get("execution_profiles")
            execution_map = profile.get("execution_profile_map")
            if not isinstance(execution_profiles, dict) or not execution_profiles:
                raise AuditError(f"CLI profile {profile_id} has no execution profiles")
            if not isinstance(execution_map, dict) or set(execution_map) != {
                "low",
                "medium",
                "high",
            }:
                raise AuditError(f"CLI profile {profile_id} has an invalid complexity map")
            for execution_id, row in execution_profiles.items():
                if execution_id in execution_ids:
                    raise AuditError(f"duplicate execution profile ID: {execution_id}")
                execution_ids.add(execution_id)
                if not isinstance(row, dict):
                    raise AuditError(f"execution profile {execution_id} is invalid")
                lifecycle = str(row.get("lifecycle", "")).upper()
                if lifecycle not in VALID_LIFECYCLES:
                    raise AuditError(
                        f"execution profile {execution_id} has invalid lifecycle"
                    )
                lifecycle_counts[lifecycle] += 1
                for key in (
                    "model",
                    "runtime_model_id",
                    "reasoning_level",
                    "supported_modes",
                    "tools",
                    "capabilities",
                    "efficiency_priors",
                ):
                    if key not in row:
                        raise AuditError(
                            f"execution profile {execution_id} misses {key}"
                        )
                if schema_version >= 5 and not str(row.get("model_provider_id", "")).strip():
                    raise AuditError(
                        f"execution profile {execution_id} misses model_provider_id"
                    )
            mapped_ids.update(str(item) for item in execution_map.values())
        missing_mappings = sorted(mapped_ids - execution_ids)
        if missing_mappings:
            raise AuditError(
                "complexity map references missing profiles: "
                + ", ".join(missing_mappings)
            )
    except (
        AuditError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        return {**base, "status": "INVALID", "valid": False, "error": str(exc)}
    return {
        **base,
        "status": "VALID",
        "valid": True,
        "profile_count": len(profiles),
        "execution_profile_count": len(execution_ids),
        "lifecycle_counts": lifecycle_counts,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "error": None,
    }


def inspect_local_benchmark_suite(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    base = {
        "component": "local-benchmark-suite",
        "suite_path": str(resolved),
    }
    if not resolved.is_file():
        return {**base, "status": "MISSING", "valid": False, "error": "suite-file-not-found"}
    try:
        raw = resolved.read_bytes()
        document = json.loads(raw.decode("utf-8"))
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise AuditError("local benchmark suite schema_version must be 1")
        cases = document.get("cases")
        if not isinstance(cases, list) or len(cases) < 6:
            raise AuditError("local benchmark suite needs at least six cases")
        ids: set[str] = set()
        kinds: set[str] = set()
        for case in cases:
            if not isinstance(case, dict):
                raise AuditError("local benchmark case is invalid")
            case_id = str(case.get("id", ""))
            if not case_id or case_id in ids:
                raise AuditError(f"local benchmark case ID is invalid: {case_id}")
            ids.add(case_id)
            kind = str(case.get("kind", ""))
            if kind not in VALID_LOCAL_KINDS:
                raise AuditError(f"local benchmark kind is invalid: {kind}")
            kinds.add(kind)
            if case.get("mode") not in {"advisory", "review", "implement"}:
                raise AuditError(f"local benchmark mode is invalid: {case_id}")
            if case.get("risk") not in {"low", "medium", "high", "critical"}:
                raise AuditError(f"local benchmark risk is invalid: {case_id}")
            if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
                raise AuditError(f"local benchmark prompt is missing: {case_id}")
            verification = case.get("verification")
            if (
                not isinstance(verification, list)
                or not verification
                or any(not isinstance(item, str) or not item.strip() for item in verification)
            ):
                raise AuditError(
                    f"local benchmark verification is invalid: {case_id}"
                )
    except (
        AuditError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        return {**base, "status": "INVALID", "valid": False, "error": str(exc)}
    return {
        **base,
        "status": "VALID",
        "valid": True,
        "suite_version": document.get("suite_version"),
        "case_count": len(cases),
        "covered_kinds": sorted(kinds),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "error": None,
    }


def initialize_state(
    state_root: Path,
    *,
    audit_source: Path = BUNDLED_AUDIT_SEED,
    benchmark_source: Path = BUNDLED_BENCHMARK_SEED,
) -> dict[str, Any]:
    root = ensure_plain_state_root(state_root)
    audit_target, benchmark_target = state_paths(root)
    sources = (
        ("audit", audit_source.expanduser().resolve(), audit_target, inspect_audit),
        (
            "benchmark",
            benchmark_source.expanduser().resolve(),
            benchmark_target,
            inspect_benchmark_snapshot,
        ),
    )
    source_checks: dict[str, dict[str, Any]] = {}
    for label, source, _, inspector in sources:
        result = inspector(source)
        if result["status"] in {"MISSING", "INVALID"}:
            raise AuditError(
                f"Cannot initialize {label} state from {source}: {result.get('error')}"
            )
        source_checks[label] = result

    for label, _, target, inspector in sources:
        if not target.exists():
            continue
        if not target.is_file() or is_filesystem_link(target):
            raise AuditError(f"Model Radar {label} state is not a plain file: {target}")
        result = inspector(target)
        if result["status"] in {"MISSING", "INVALID"}:
            raise AuditError(
                f"Existing {label} state is invalid and was preserved: {target}: "
                f"{result.get('error')}"
            )

    writes = 0
    initialized: list[str] = []
    preserved: list[str] = []
    for label, source, target, _ in sources:
        if target.exists():
            preserved.append(label)
            continue
        atomic_write_bytes(target, source.read_bytes())
        initialized.append(label)
        writes += 1
    return {
        "action": "init-state",
        "status": "INITIALIZED" if writes else "CURRENT",
        "state_root": str(root),
        "audit_path": str(audit_target),
        "benchmark_path": str(benchmark_target),
        "initialized": initialized,
        "preserved": preserved,
        "source_sha256": {
            label: result.get("sha256") for label, result in source_checks.items()
        },
        "writes_performed": writes,
    }


def combined_check(
    audit_path: Path,
    benchmark_path: Path,
    *,
    max_age_days: int,
    now: dt.datetime | None,
    profiles_path: Path | None = None,
    local_suite_path: Path | None = None,
) -> dict[str, Any]:
    audit = inspect_audit(audit_path, max_age_days=max_age_days, now=now)
    benchmark = inspect_benchmark_snapshot(
        benchmark_path, max_age_days=max_age_days, now=now
    )
    execution_profiles = (
        inspect_execution_profiles(profiles_path)
        if profiles_path is not None
        else {
            "component": "execution-profile-registry",
            "status": "NOT_CHECKED",
            "valid": None,
        }
    )
    local_suite = (
        inspect_local_benchmark_suite(local_suite_path)
        if local_suite_path is not None
        else {
            "component": "local-benchmark-suite",
            "status": "NOT_CHECKED",
            "valid": None,
        }
    )
    warnings: list[str] = []
    if benchmark["status"] != "FRESH":
        warnings.append(
            "Arena benchmark snapshot is not fresh; static capability routing remains available"
        )
    elif benchmark.get("fetch_warning_count", 0):
        warnings.append(
            "Arena benchmark snapshot is fresh but partial; unmatched categories use static capability routing"
        )
    if local_suite.get("valid") is False:
        warnings.append(
            "Local benchmark suite is missing or invalid; canary calibration is unavailable"
        )
    registry_current = profiles_path is None or (
        execution_profiles.get("valid") is True
        and audit.get("registry_fresh") is True
        and audit.get("registry_sha256") == execution_profiles.get("sha256")
    )
    market_fresh = audit.get("fresh") is True
    fresh = market_fresh and registry_current
    coverage_error = None if registry_current else "registry-coverage-missing-stale-or-mismatched"
    if coverage_error:
        warnings.append(
            "Verify the current profile registry and bind its SHA-256 and registry_verified_at; "
            "do not renew last_verified_at without a market review"
        )
    return {
        "action": "check",
        "radar_version": VERSION,
        **audit,
        "market_fresh": market_fresh,
        "registry_current": registry_current,
        "status": "STALE" if market_fresh and not registry_current else audit["status"],
        "fresh": fresh,
        "refresh_required": not fresh,
        "error": audit.get("error") or coverage_error,
        "routing_allowed": fresh,
        "benchmark": benchmark,
        "execution_profiles": execution_profiles,
        "local_benchmark_suite": local_suite,
        "warnings": warnings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check model-market and optional Arena benchmark freshness."
    )
    parser.add_argument(
        "action",
        choices=["check", "validate", "check-benchmarks", "init-state", "paths"],
        nargs="?",
        default="check",
    )
    parser.add_argument("--state-root")
    parser.add_argument("--audit")
    parser.add_argument("--benchmark")
    parser.add_argument("--audit-source")
    parser.add_argument("--benchmark-source")
    parser.add_argument("--profiles", default=str(DEFAULT_PROFILES))
    parser.add_argument("--local-suite", default=str(DEFAULT_LOCAL_SUITE))
    parser.add_argument("--max-age-days", type=int, default=7)
    parser.add_argument("--now", help="ISO-8601 timestamp used only for deterministic tests")
    parser.add_argument("--version", action="version", version=f"model-radar {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_age_days < 1 or args.max_age_days > 7:
        result = {
            "action": args.action,
            "status": "INVALID",
            "fresh": False,
            "refresh_required": True,
            "error": "--max-age-days must be between 1 and 7",
        }
        sys.stderr.buffer.write(canonical_json(result))
        return 3
    try:
        now = parse_timestamp(args.now) if args.now else None
    except AuditError as exc:
        sys.stderr.buffer.write(
            canonical_json(
                {
                    "action": args.action,
                    "status": "INVALID",
                    "fresh": False,
                    "refresh_required": True,
                    "error": str(exc),
                }
            )
        )
        return 3
    state_root = (
        Path(os.path.abspath(Path(args.state_root).expanduser()))
        if args.state_root
        else default_state_root()
    )
    default_audit, default_benchmark = state_paths(state_root)
    audit_path = Path(args.audit).expanduser().resolve() if args.audit else default_audit
    benchmark_path = (
        Path(args.benchmark).expanduser().resolve()
        if args.benchmark
        else default_benchmark
    )
    if args.action == "paths":
        result = {
            "action": "paths",
            "status": "READY",
            "state_root": str(state_root),
            "audit_path": str(audit_path),
            "benchmark_path": str(benchmark_path),
            "bundled_audit_seed": str(BUNDLED_AUDIT_SEED.resolve()),
            "bundled_benchmark_seed": str(BUNDLED_BENCHMARK_SEED.resolve()),
            "writes_performed": 0,
        }
        sys.stdout.buffer.write(canonical_json(result))
        return 0
    if args.action == "init-state":
        try:
            result = initialize_state(
                state_root,
                audit_source=(
                    Path(args.audit_source).expanduser().resolve()
                    if args.audit_source
                    else BUNDLED_AUDIT_SEED
                ),
                benchmark_source=(
                    Path(args.benchmark_source).expanduser().resolve()
                    if args.benchmark_source
                    else BUNDLED_BENCHMARK_SEED
                ),
            )
        except (AuditError, OSError) as exc:
            sys.stderr.buffer.write(
                canonical_json(
                    {
                        "action": "init-state",
                        "status": "INVALID",
                        "error": str(exc),
                        "writes_performed": 0,
                    }
                )
            )
            return 3
        sys.stdout.buffer.write(canonical_json(result))
        return 0
    if args.action == "check-benchmarks":
        result = inspect_benchmark_snapshot(
            benchmark_path, max_age_days=args.max_age_days, now=now
        )
    else:
        result = combined_check(
            audit_path,
            benchmark_path,
            max_age_days=args.max_age_days,
            now=now,
            profiles_path=Path(args.profiles),
            local_suite_path=Path(args.local_suite),
        )
        result["state_root"] = str(state_root)
    stream = sys.stdout if result["status"] in {"FRESH", "STALE"} else sys.stderr
    stream.buffer.write(canonical_json(result))
    return 0 if result["status"] == "FRESH" else 2 if result["status"] == "STALE" else 3


if __name__ == "__main__":
    raise SystemExit(main())
