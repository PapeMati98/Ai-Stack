#!/usr/bin/env python3
"""Resolve a freshness-aware OpenRouter council roster without inference calls."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


UTC = dt.timezone.utc


class RosterError(RuntimeError):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso_z(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def exclusive_lock(path: Path, timeout_seconds: float = 10.0, stale_seconds: float = 60.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} created_at={iso_z(utc_now())}\n".encode("ascii"))
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
                if age > stale_seconds:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise RosterError(f"Timed out waiting for state lock: {path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.close(fd)
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def fetch_json(url: str, timeout: int, api_key: str | None = None) -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": "model-council-roster/1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RosterError(f"HTTP {exc.code} while reading {url}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RosterError(f"Cannot read {url}: {exc}") from exc


def _catalog_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        rows = value["data"]
    elif isinstance(value, dict) and isinstance(value.get("models"), list):
        rows = value["models"]
    elif isinstance(value, list):
        rows = value
    else:
        raise RosterError("Catalog does not contain a model list")
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)]


def load_catalog(
    policy: dict[str, Any],
    state_root: Path,
    refresh: bool,
    catalog_file: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    now = utc_now()
    if catalog_file:
        raw = read_json(catalog_file)
        rows = _catalog_rows(raw)
        return rows, {
            "source": "fixture",
            "path": str(catalog_file.resolve()),
            "fetched_at": iso_z(now),
            "age_days": 0.0,
            "refreshed": False,
            "sha256": canonical_sha256(rows),
        }

    cache_path = state_root / "openrouter-model-catalog.json"
    max_age = float(policy["catalog_max_age_days"])
    cached: dict[str, Any] | None = None
    if cache_path.exists():
        value = read_json(cache_path)
        if isinstance(value, dict):
            cached = value

    fetched_at = parse_time(cached.get("fetched_at") if cached else None)
    age_days = (now - fetched_at).total_seconds() / 86400 if fetched_at else None
    cache_matches_policy = bool(cached and cached.get("catalog_url") == policy["catalog_url"])
    cache_fresh = bool(
        cached
        and cache_matches_policy
        and fetched_at
        and age_days is not None
        and age_days <= max_age
    )

    refreshed = False
    if refresh or not cache_fresh:
        url = str(policy["catalog_url"])
        raw = fetch_json(
            url,
            timeout=int(policy.get("endpoint_probe_timeout_seconds", 20)),
            api_key=os.environ.get("OPENROUTER_API_KEY"),
        )
        rows = _catalog_rows(raw)
        cached = {
            "schema_version": 1,
            "fetched_at": iso_z(now),
            "catalog_url": url,
            "models": rows,
        }
        atomic_write_json(cache_path, cached)
        fetched_at = now
        age_days = 0.0
        refreshed = True
    elif cached:
        rows = _catalog_rows(cached)
    else:
        raise RosterError("Catalog cache is unavailable")

    return rows, {
        "source": "openrouter-cache",
        "path": str(cache_path),
        "endpoint": str(policy["catalog_url"]),
        "fetched_at": iso_z(fetched_at or now),
        "age_days": round(float(age_days or 0.0), 4),
        "max_age_days": max_age,
        "refreshed": refreshed,
        "sha256": canonical_sha256(rows),
    }


def load_health(state_root: Path) -> dict[str, Any]:
    path = state_root / "model-health.json"
    if not path.exists():
        return {"schema_version": 1, "models": {}}
    value = read_json(path)
    if not isinstance(value, dict) or not isinstance(value.get("models"), dict):
        return {"schema_version": 1, "models": {}}
    return value


def is_quarantined(model_id: str, health: dict[str, Any], now: dt.datetime) -> tuple[bool, str | None]:
    row = health.get("models", {}).get(model_id, {})
    until = parse_time(row.get("quarantined_until") if isinstance(row, dict) else None)
    if until and until > now:
        return True, iso_z(until)
    return False, None


def record_health(args: argparse.Namespace) -> dict[str, Any]:
    policy = read_json(Path(args.policy))
    state_root = Path(args.state_root).expanduser().resolve()
    path = state_root / "model-health.json"
    with exclusive_lock(state_root / "model-health.lock"):
        health = load_health(state_root)
        models = health.setdefault("models", {})
        row = models.setdefault(args.model, {})
        now = utc_now()

        if args.success:
            row.update(
                {
                    "consecutive_failures": 0,
                    "last_success_at": iso_z(now),
                    "last_failure_kind": None,
                    "quarantined_until": None,
                }
            )
        else:
            failures = int(row.get("consecutive_failures", 0)) + 1
            row.update(
                {
                    "consecutive_failures": failures,
                    "last_failure_at": iso_z(now),
                    "last_failure_kind": args.failure_kind or "unknown",
                }
            )
            threshold = int(policy.get("failure_threshold", 2))
            if failures >= threshold:
                hours = float(policy.get("quarantine_hours", 24))
                row["quarantined_until"] = iso_z(now + dt.timedelta(hours=hours))

        health["updated_at"] = iso_z(now)
        atomic_write_json(path, health)
    return {
        "status": "RECORDED",
        "model": args.model,
        "success": bool(args.success),
        "consecutive_failures": int(row.get("consecutive_failures", 0)),
        "quarantined_until": row.get("quarantined_until"),
        "state_path": str(path),
    }


def model_expired(model: dict[str, Any], now: dt.datetime) -> bool:
    expires = parse_time(model.get("expiration_date"))
    return bool(expires and expires <= now)


def selector_candidates(
    models: list[dict[str, Any]],
    selector: dict[str, Any],
    health: dict[str, Any],
    used_ids: set[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    patterns = [re.compile(value) for value in selector.get("patterns", [])]
    denied = set(selector.get("deny_ids", []))
    minimum_created = int(selector.get("minimum_created", 0))
    preferred = {value: index for index, value in enumerate(selector.get("preferred_ids", []))}
    now = utc_now()
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []

    for model in models:
        model_id = model["id"]
        if model_id in denied or model_id in used_ids:
            continue
        if not any(pattern.fullmatch(model_id) for pattern in patterns):
            continue
        if int(model.get("created") or 0) < minimum_created:
            continue
        if model_expired(model, now):
            continue
        supported = set(model.get("supported_parameters") or [])
        if supported and not ({"max_tokens", "max_completion_tokens"} & supported):
            continue
        quarantined, until = is_quarantined(model_id, health, now)
        if quarantined:
            warnings.append(f"{model_id} is quarantined until {until}")
            continue
        rows.append(model)

    rows.sort(
        key=lambda row: (
            int(row.get("created") or 0),
            -preferred.get(row["id"], 10_000),
            row["id"],
        ),
        reverse=True,
    )
    return rows, warnings


def endpoint_probe(model_id: str, policy: dict[str, Any]) -> dict[str, Any]:
    encoded = urllib.parse.quote(model_id, safe="/:")
    url = f"https://openrouter.ai/api/v1/models/{encoded}/endpoints"
    raw = fetch_json(
        url,
        timeout=int(policy.get("endpoint_probe_timeout_seconds", 20)),
        api_key=os.environ.get("OPENROUTER_API_KEY"),
    )
    data = raw.get("data", {}) if isinstance(raw, dict) else {}
    endpoints = data.get("endpoints", []) if isinstance(data, dict) else []
    providers = sorted(
        {
            str(row.get("provider_name"))
            for row in endpoints
            if isinstance(row, dict) and row.get("provider_name")
        }
    )
    return {"endpoint_count": len(endpoints), "providers": providers}


def roster_row(role_id: str, role: dict[str, Any], model: dict[str, Any], endpoint: dict[str, Any]) -> dict[str, Any]:
    supported = set(model.get("supported_parameters") or [])
    pricing = model.get("pricing") if isinstance(model.get("pricing"), dict) else {}
    return {
        "role": role_id,
        "label": role["label"],
        "description": role["description"],
        "model": model["id"],
        "model_name": model.get("name") or model["id"],
        "canonical_slug": model.get("canonical_slug"),
        "created": model.get("created"),
        "supports_reasoning": bool({"reasoning", "reasoning_effort"} & supported),
        "endpoint_count": endpoint.get("endpoint_count"),
        "providers": endpoint.get("providers", []),
        "pricing_per_token_usd": {
            "prompt": pricing.get("prompt"),
            "completion": pricing.get("completion"),
        },
    }


def resolve(args: argparse.Namespace) -> dict[str, Any]:
    policy_path = Path(args.policy).expanduser().resolve()
    policy = read_json(policy_path)
    if int(policy.get("schema_version", 0)) != 1:
        raise RosterError("Unsupported roster policy schema")

    state_root = Path(args.state_root).expanduser().resolve()
    catalog_file = Path(args.catalog_file).expanduser().resolve() if args.catalog_file else None
    models, catalog = load_catalog(policy, state_root, args.refresh_catalog, catalog_file)
    model_by_id = {row["id"]: row for row in models}
    health = load_health(state_root)
    warnings: list[str] = []
    skipped: list[dict[str, Any]] = []
    panel: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    retired_model_ids = set(policy.get("retired_model_ids", []))

    role_ids: list[str]
    if args.custom_model:
        role_ids = []
        for index, model_id in enumerate(args.custom_model, start=1):
            if model_id in retired_model_ids:
                raise RosterError(f"Custom model is retired: {model_id}")
            model = model_by_id.get(model_id)
            if not model:
                raise RosterError(f"Custom model is absent from the current catalog: {model_id}")
            if model_expired(model, utc_now()):
                raise RosterError(f"Custom model is expired: {model_id}")
            quarantined, until = is_quarantined(model_id, health, utc_now())
            if quarantined:
                raise RosterError(f"Custom model is quarantined until {until}: {model_id}")
            supported = set(model.get("supported_parameters") or [])
            if supported and not ({"max_tokens", "max_completion_tokens"} & supported):
                raise RosterError(f"Custom model cannot enforce an output limit: {model_id}")
            endpoint = {"endpoint_count": None, "providers": []}
            if not args.skip_endpoint_probe:
                endpoint = endpoint_probe(model_id, policy)
                if endpoint["endpoint_count"] < 1:
                    raise RosterError(f"Custom model has no active endpoint: {model_id}")
            custom_role = {
                "label": f"CUSTOM REVIEWER {index}",
                "description": "Szukaj realnych ryzyk, brakow, blednej sekwencji i prostszego podejscia.",
            }
            panel.append(roster_row(f"custom_{index}", custom_role, model, endpoint))
            used_ids.add(model_id)
        role_ids.extend(args.include_role)
    else:
        role_ids = list(policy["panels"][args.mode])
        role_ids.extend(args.include_role)

    excluded = set(args.exclude_role)
    role_ids = [role_id for role_id in role_ids if role_id not in excluded]
    seen_roles: set[str] = set()
    role_ids = [role_id for role_id in role_ids if not (role_id in seen_roles or seen_roles.add(role_id))]

    for role_id in role_ids:
        role = policy["roles"].get(role_id)
        if not role:
            raise RosterError(f"Unknown role in roster policy: {role_id}")
        candidates, role_warnings = selector_candidates(models, role["selector"], health, used_ids)
        warnings.extend(role_warnings)
        selected: dict[str, Any] | None = None
        endpoint: dict[str, Any] = {"endpoint_count": None, "providers": []}
        probe_errors: list[str] = []
        for candidate in candidates:
            if args.skip_endpoint_probe:
                selected = candidate
                break
            try:
                candidate_endpoint = endpoint_probe(candidate["id"], policy)
            except RosterError as exc:
                probe_errors.append(f"{candidate['id']}: {exc}")
                continue
            if candidate_endpoint["endpoint_count"] < 1:
                probe_errors.append(f"{candidate['id']}: no active endpoints")
                continue
            selected = candidate
            endpoint = candidate_endpoint
            break

        if selected is None:
            skipped.append(
                {
                    "role": role_id,
                    "reason": "no healthy current candidate",
                    "probe_errors": probe_errors,
                }
            )
            continue
        panel.append(roster_row(role_id, role, selected, endpoint))
        used_ids.add(selected["id"])

    minimum = int(policy["minimum_panel_size"]["custom" if args.custom_model else args.mode])
    if len(panel) < minimum:
        raise RosterError(
            f"Resolved panel is too small ({len(panel)}/{minimum}); no inference calls were made"
        )

    return {
        "schema_version": 1,
        "status": "READY",
        "resolved_at": iso_z(utc_now()),
        "mode": "custom" if args.custom_model else args.mode,
        "policy_version": policy["policy_version"],
        "policy_path": str(policy_path),
        "catalog": catalog,
        "panel": panel,
        "skipped": skipped,
        "warnings": sorted(set(warnings)),
        "inference_calls": 0,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    resolve_parser = sub.add_parser("resolve", help="Resolve and validate the current council roster")
    resolve_parser.add_argument("--policy", required=True)
    resolve_parser.add_argument("--state-root", required=True)
    resolve_parser.add_argument("--mode", choices=("quick", "default", "full"), default="default")
    resolve_parser.add_argument("--custom-model", action="append", default=[])
    resolve_parser.add_argument("--include-role", action="append", default=[])
    resolve_parser.add_argument("--exclude-role", action="append", default=[])
    resolve_parser.add_argument("--refresh-catalog", action="store_true")
    resolve_parser.add_argument("--skip-endpoint-probe", action="store_true")
    resolve_parser.add_argument("--catalog-file")

    record_parser = sub.add_parser("record", help="Record a model call outcome for the circuit breaker")
    record_parser.add_argument("--policy", required=True)
    record_parser.add_argument("--state-root", required=True)
    record_parser.add_argument("--model", required=True)
    outcome = record_parser.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--success", action="store_true")
    outcome.add_argument("--failure", action="store_true")
    record_parser.add_argument("--failure-kind", choices=("hard", "transient", "empty", "unknown"))
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        result = record_health(args) if args.command == "record" else resolve(args)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except (RosterError, KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "REFUSED", "error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
