from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable


VERSION = "0.1.1"
SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAP = SKILL_ROOT / "references" / "arena-model-map.json"
STATE_ROOT_ENV = "AGENT_FLEET_MODEL_RADAR_STATE_ROOT"
DEFAULT_STATE_ROOT = Path(
    os.path.abspath(
        Path(os.environ[STATE_ROOT_ENV]).expanduser()
        if os.environ.get(STATE_ROOT_ENV)
        else Path.home() / ".agents" / "model-radar"
    )
)
DEFAULT_OUTPUT = DEFAULT_STATE_ROOT / "MODEL-BENCHMARK-SNAPSHOT.json"
DATASET = "lmarena-ai/leaderboard-dataset"
DATASET_URL = "https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset"
ROWS_API = "https://datasets-server.huggingface.co/rows"
DEFAULT_CONFIGS = (
    "text_style_control",
    "webdev",
    "agent",
    "search_style_control",
    "vision_style_control",
    "document",
)


class SnapshotError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode(
        "ascii"
    )


def load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"Cannot load {label}: {path}: {exc}") from exc


def normalized_name(value: Any) -> str:
    return str(value or "").strip().casefold()


def model_name(row: dict[str, Any]) -> str:
    for key in ("model_name", "model", "name"):
        if str(row.get(key, "")).strip():
            return str(row[key]).strip()
    return ""


def numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def integer(value: Any) -> int | None:
    parsed = numeric(value)
    return int(parsed) if parsed is not None and parsed >= 0 else None


def choose_row(rows: list[dict[str, Any]], arena_ids: Iterable[str]) -> dict[str, Any] | None:
    wanted = {normalized_name(item) for item in arena_ids}
    matched = [row for row in rows if normalized_name(model_name(row)) in wanted]
    if not matched:
        return None
    overall = [
        row
        for row in matched
        if normalized_name(row.get("category")) in {"overall", "all", "overall score"}
    ]
    candidates = overall or matched
    return sorted(
        candidates,
        key=lambda row: (
            integer(row.get("rank")) or 10**9,
            normalized_name(model_name(row)),
        ),
    )[0]


def percentile_for(row: dict[str, Any], rows: list[dict[str, Any]]) -> float | None:
    rank = integer(row.get("rank"))
    if rank is None or rank < 1:
        return None
    category = normalized_name(row.get("category"))
    population = [
        candidate
        for candidate in rows
        if normalized_name(candidate.get("category")) == category
        and integer(candidate.get("rank")) is not None
    ]
    count = len(population)
    if count <= 1:
        return 100.0 if count == 1 else None
    return round(100.0 * (count - rank) / (count - 1), 3)


def compact_metric(row: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    rating = numeric(row.get("rating"))
    if rating is None:
        rating = numeric(row.get("score"))
    lower = numeric(row.get("rating_lower"))
    if lower is None:
        lower = numeric(row.get("score_ci_lower"))
    upper = numeric(row.get("rating_upper"))
    if upper is None:
        upper = numeric(row.get("score_ci_upper"))
    votes = integer(row.get("vote_count"))
    if votes is None:
        votes = integer(row.get("observation_count"))
    return {
        "status": "matched",
        "arena_model_id": model_name(row),
        "category": row.get("category"),
        "rating_or_score": rating,
        "lower": lower,
        "upper": upper,
        "rank": integer(row.get("rank")),
        "percentile": percentile_for(row, rows),
        "votes_or_observations": votes,
        "sessions": integer(row.get("session_count")),
        "leaderboard_publish_date": row.get("leaderboard_publish_date"),
    }


def validate_map(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise SnapshotError("Arena model map schema_version must be 1")
    if raw.get("source_dataset") != DATASET:
        raise SnapshotError(f"Arena model map source_dataset must be {DATASET}")
    mappings = raw.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise SnapshotError("Arena model map must contain mappings")
    seen: set[str] = set()
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise SnapshotError("Arena model mapping must be an object")
        key = f"{mapping.get('profile')}@{mapping.get('model')}"
        if key in seen or key.startswith("None@") or key.endswith("@None"):
            raise SnapshotError(f"Invalid or duplicate Arena model mapping: {key}")
        seen.add(key)
        aliases = mapping.get("arena_ids")
        if not isinstance(aliases, list) or not aliases or any(
            not str(item).strip() for item in aliases
        ):
            raise SnapshotError(f"Arena model mapping {key} needs arena_ids")
        if mapping.get("confidence") not in {"verified", "candidate"}:
            raise SnapshotError(f"Arena model mapping {key} has invalid confidence")
    return mappings


def build_snapshot(
    datasets: dict[str, list[dict[str, Any]]],
    mapping_document: dict[str, Any],
    *,
    generated_at: str | None = None,
    fetch_warnings: list[str] | None = None,
) -> dict[str, Any]:
    mappings = validate_map(mapping_document)
    models: dict[str, Any] = {}
    for mapping in mappings:
        key = f"{mapping['profile']}@{mapping['model']}"
        benchmarks: dict[str, Any] = {}
        for config, rows in datasets.items():
            row = choose_row(rows, mapping["arena_ids"])
            benchmarks[config] = (
                compact_metric(row, rows)
                if row is not None
                else {"status": "missing", "arena_model_id": None}
            )
        models[key] = {
            "profile": mapping["profile"],
            "model": mapping["model"],
            "arena_ids": mapping["arena_ids"],
            "alias_confidence": mapping["confidence"],
            "benchmarks": benchmarks,
        }
    return {
        "schema_version": 1,
        "generated_at": generated_at or utc_now(),
        "valid_for_days": 7,
        "source": {
            "dataset": DATASET,
            "dataset_url": DATASET_URL,
            "rows_api": ROWS_API,
            "split": "latest",
        },
        "configs": {
            config: {"status": "fetched", "row_count": len(rows)}
            for config, rows in datasets.items()
        },
        "models": models,
        "fetch_warnings": list(fetch_warnings or []),
    }


def fetch_config(config: str, *, timeout: int, page_size: int = 100) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None
    while total is None or offset < total:
        query = urllib.parse.urlencode(
            {
                "dataset": DATASET,
                "config": config,
                "split": "latest",
                "offset": offset,
                "length": page_size,
            }
        )
        request = urllib.request.Request(
            f"{ROWS_API}?{query}",
            headers={"User-Agent": f"model-radar/{VERSION}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise SnapshotError(f"Cannot fetch Arena config {config}: {exc}") from exc
        page = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(page, list):
            raise SnapshotError(f"Arena config {config} returned invalid rows")
        for item in page:
            row = item.get("row") if isinstance(item, dict) else None
            if isinstance(row, dict):
                rows.append(row)
        total = integer(payload.get("num_rows_total"))
        if total is None:
            raise SnapshotError(f"Arena config {config} omitted num_rows_total")
        offset += len(page)
        if not page:
            break
    return rows


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(canonical_json(payload))
    os.replace(temporary, path)


def refresh(
    *,
    mapping_path: Path,
    output_path: Path,
    configs: Iterable[str],
    timeout: int,
) -> dict[str, Any]:
    mapping_document = load_json(mapping_path, "Arena model map")
    validate_map(mapping_document)
    datasets: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    for config in configs:
        try:
            datasets[config] = fetch_config(config, timeout=timeout)
        except SnapshotError as exc:
            warnings.append(str(exc))
    if not datasets:
        detail = "; ".join(warnings[:3])
        raise SnapshotError(
            "No Arena leaderboard config could be fetched; snapshot not replaced. "
            + detail
        )
    snapshot = build_snapshot(
        datasets,
        mapping_document,
        fetch_warnings=warnings,
    )
    atomic_write(output_path, snapshot)
    return {
        "action": "refresh",
        "status": "UPDATED" if not warnings else "UPDATED_WITH_WARNINGS",
        "snapshot_path": str(output_path.resolve()),
        "generated_at": snapshot["generated_at"],
        "configs_fetched": sorted(datasets),
        "warning_count": len(warnings),
        "warnings": warnings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh a source-grounded weekly Arena benchmark snapshot."
    )
    parser.add_argument("action", choices=["refresh"], nargs="?", default="refresh")
    parser.add_argument("--map", dest="mapping_path", default=str(DEFAULT_MAP))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--configs", default=",".join(DEFAULT_CONFIGS))
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--version", action="version", version=f"arena-snapshot {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configs = [item.strip() for item in args.configs.split(",") if item.strip()]
    if not configs or args.timeout < 5 or args.timeout > 300:
        result = {"action": args.action, "status": "INVALID", "error": "Invalid configs or timeout"}
        sys.stderr.buffer.write(canonical_json(result))
        return 3
    try:
        result = refresh(
            mapping_path=Path(args.mapping_path).expanduser().resolve(),
            output_path=Path(args.output).expanduser().resolve(),
            configs=configs,
            timeout=args.timeout,
        )
    except SnapshotError as exc:
        sys.stderr.buffer.write(
            canonical_json({"action": args.action, "status": "FAILED", "error": str(exc)})
        )
        return 2
    sys.stdout.buffer.write(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
