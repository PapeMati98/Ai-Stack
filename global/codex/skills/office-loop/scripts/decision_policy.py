#!/usr/bin/env python3
"""Shadow policy for the repeatable continue-versus-swap decision.

After a rejected repair round, the observed maker of the next round either
matches the maker of the repair round (continue) or differs (swap). This
tool fits an undiscounted expected-round model from local telemetry and
writes a shadow recommendation. It does not call a model, send an Office
message, mutate a loop, or grant authority: every result has
release_authority false. Uncovered states stay with autopsy or a later LLM
judgment.

Telemetry lines follow the journal shape read by idea-maker collect:
one JSON object per line, events provider_result, repair, autopsy and
node_terminal. Only catalog profile pairs (agent, model) are counted.
A provider_result outside that allowlist is excluded from episode length
and transition cost as well as from the decision itself. A repair or
autopsy counts toward episode length only in a round that has an allowed
maker; a synthetic maker cannot stretch the episode through those events.
Transition probabilities use add-one smoothing over success, failure and
any observed next repair state, including success and failure when that
outcome was not seen. A journal without node_terminal is unresolved: it
is reported separately and is not stored as a failure or used to estimate
the policy. The decision point still counts in the state inventory and in
the saturation curve, so an open episode cannot hide a new state.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from dtd_core import beta_interval, beta_mean, saturation, value_iteration

SKILL_ROOT = SCRIPT_DIR.parent
SCHEMA_VERSION = 1
SKILL_NAME = "office-loop"

DEFAULT_TELEMETRY_ROOT = Path.home() / ".agents" / "telemetry" / "todo"
SIBLING_CATALOG = SKILL_ROOT.parent / "agent-graph" / "references" / "model-catalog.json"
HOME_CATALOG = Path.home() / ".claude" / "skills" / "agent-graph" / "references" / "model-catalog.json"

DEFAULT_MAX_FILES = 20000
DEFAULT_MAX_LINES = 10000
DEFAULT_MAX_LINE_BYTES = 65536
DEFAULT_MIN_SUPPORT = 5
DEFAULT_FAILURE_COST = 10.0

MAX_CATALOG_BYTES = 5_000_000
MAX_TEXT = 200
MAX_CODE = 40
MAX_ROUND = 1_000_000
MAX_TOKEN = 1_000_000_000_000
MAX_EXCLUDE = 100

ACTIONS = ("continue", "swap")
TERM_SUCCESS = ("__terminal__", "success")
TERM_FAILURE = ("__terminal__", "failure")

CAVEAT = (
    "Dane sa obserwacyjne, a przypisanie akcji jest nielosowe. "
    "Porownanie cieniowe nie jest dowodem przyczynowosci."
)
GAP_NO_DATA = (
    "Brak telemetrii albo brak punktow decyzji po filtrze katalogu. "
    "Polityka nie zostala wyznaczona."
)
GAP_UNRESOLVED = (
    "Punkty decyzji nie maja koncowego node_terminal. "
    "Brak koncowego wyniku nie jest porazka i nie wchodzi do polityki."
)

CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PolicyError(ValueError):
    """Fail-closed input or configuration error. The store is left unchanged."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return CONTROL.sub(" ", value).strip()[:limit]


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return None
        # A positive offset on year 1 cannot be represented as UTC.
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def parse_round(value: Any) -> int | None:
    if isinstance(value, bool) or type(value) is not int:
        return None
    if value < 0 or value > MAX_ROUND:
        return None
    return value


def parse_token(value: Any) -> int | None:
    if isinstance(value, bool) or type(value) is not int:
        return None
    if value < 0 or value > MAX_TOKEN:
        return None
    return value


def round_bucket(round_number: int) -> str:
    if round_number <= 1:
        return "1"
    if round_number == 2:
        return "2"
    return "3+"


def inside_skill(path: Path) -> bool:
    resolved = path.resolve()
    skill = SKILL_ROOT.resolve()
    return resolved == skill or resolved.is_relative_to(skill)


def atomic_text(path: Path, text: str) -> None:
    if inside_skill(path) or inside_skill(path.parent):
        raise PolicyError("OUT_IN_SKILL", "refusing to write inside the skill tree")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def ensure_out_dir(path: Path) -> Path:
    if not path.is_absolute():
        raise PolicyError("OUT_NOT_ABSOLUTE", "--out must be an absolute directory")
    if inside_skill(path):
        raise PolicyError("OUT_IN_SKILL", "--out must not lie inside the skill tree")
    if path.exists() and not path.is_dir():
        raise PolicyError("OUT_NOT_DIRECTORY", "--out must be a directory")
    return path


def resolve_catalog(explicit: Path | None) -> Path:
    if explicit is not None:
        candidate = explicit
        if not candidate.is_file():
            raise PolicyError("CATALOG_MISSING", "model catalog not found")
        return candidate
    env = os.environ.get("AGENT_MODEL_CATALOG")
    if env:
        candidate = Path(env)
        if not candidate.is_file():
            raise PolicyError("CATALOG_MISSING", "AGENT_MODEL_CATALOG is not a file")
        return candidate
    if SIBLING_CATALOG.is_file():
        return SIBLING_CATALOG
    if HOME_CATALOG.is_file():
        return HOME_CATALOG
    raise PolicyError("CATALOG_MISSING", "model catalog not found")


def load_catalog(path: Path) -> dict[tuple[str, str], str]:
    try:
        if not path.is_file():
            raise PolicyError("CATALOG_MISSING", "model catalog not found")
        if path.stat().st_size > MAX_CATALOG_BYTES:
            raise PolicyError("CATALOG_INVALID", "model catalog exceeds size limit")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except PolicyError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PolicyError("CATALOG_INVALID", "model catalog is not readable JSON") from exc
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise PolicyError("CATALOG_INVALID", "catalog models list missing")
    allow: dict[tuple[str, str], str] = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        tier = clean_text(model.get("tier"), MAX_CODE)
        profiles = model.get("profiles")
        if not tier or not isinstance(profiles, list):
            continue
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            agent = clean_text(profile.get("agent"), MAX_TEXT)
            name = clean_text(profile.get("model"), MAX_TEXT)
            if agent and name and (agent, name) not in allow:
                allow[(agent, name)] = tier
    if not allow:
        raise PolicyError("CATALOG_INVALID", "catalog has no agent/model profiles")
    return allow


def _read_journal(path: Path, max_lines: int, max_line_bytes: int) -> tuple[list[str], int, bool]:
    """Read journal records. max_line_bytes counts UTF-8 bytes, including the break."""
    lines: list[str] = []
    invalid = 0
    first_chunk = True
    with path.open("rb") as stream:
        for _ in range(max_lines):
            try:
                chunk = stream.readline(max_line_bytes + 1)
            except OSError:
                invalid += 1
                return lines, invalid, False
            if chunk == b"":
                return lines, invalid, False
            is_first = first_chunk
            first_chunk = False
            if len(chunk) > max_line_bytes:
                invalid += 1
                while chunk and not chunk.endswith((b"\n", b"\r")):
                    try:
                        chunk = stream.readline(max_line_bytes + 1)
                    except OSError:
                        return lines, invalid, False
                continue
            if is_first and chunk.startswith(b"\xef\xbb\xbf"):
                chunk = chunk[3:]
            try:
                lines.append(chunk.decode("utf-8"))
            except UnicodeError:
                invalid += 1
        else:
            try:
                extra = stream.readline(1)
            except OSError:
                extra = b""
            if extra:
                return lines, invalid, True
    return lines, invalid, False


def _event_from_row(row: dict[str, Any], order: int) -> dict[str, Any] | None:
    stamp = parse_utc(row.get("utc"))
    event = row.get("event")
    if stamp is None or not isinstance(event, str):
        return None
    if event not in {"provider_result", "repair", "autopsy", "node_terminal"}:
        return None
    node = clean_text(row.get("node"), MAX_TEXT)
    if not node:
        return None
    parsed: dict[str, Any] = {"utc": stamp, "order": order, "event": event, "node": node}
    if event == "provider_result":
        round_number = parse_round(row.get("round"))
        if round_number is None:
            return None
        agent = clean_text(row.get("agent"), MAX_TEXT)
        model = clean_text(row.get("model"), MAX_TEXT)
        parsed.update({
            "round": round_number,
            "role": clean_text(row.get("role"), MAX_CODE),
            "agent": agent,
            "model": model,
            "fresh_input_tokens": parse_token(row.get("fresh_input_tokens")),
        })
        return parsed
    if event == "repair":
        round_number = parse_round(row.get("round"))
        code = clean_text(row.get("code"), MAX_CODE)
        if round_number is None or not code:
            return None
        parsed.update({"round": round_number, "code": code})
        return parsed
    if event == "node_terminal":
        status = clean_text(row.get("status"), MAX_TEXT)
        if not status:
            return None
        parsed.update({"status": status, "round": parse_round(row.get("round"))})
        return parsed
    round_number = parse_round(row.get("round"))
    parsed.update({"round": round_number, "code": clean_text(row.get("code"), MAX_CODE)})
    return parsed


def load_telemetry(
    root: Path,
    allow: dict[tuple[str, str], str],
    exclude_agents: set[str],
    max_files: int,
    max_lines: int,
    max_line_bytes: int,
) -> dict[str, Any]:
    empty = {
        "journals": 0,
        "files_matched": 0,
        "files_read": 0,
        "truncated_files": 0,
        "invalid_lines": 0,
        "excluded_records": 0,
        "excluded_journals": 0,
        "nodes": [],
    }
    if not root.exists():
        return empty
    if not root.is_dir():
        raise PolicyError("TELEMETRY_NOT_DIRECTORY", "--telemetry-root must be a directory")

    matched = sorted(path for path in root.glob("*/*.jsonl"))
    chosen = matched[:max_files]
    journals: list[dict[str, Any]] = []
    invalid_lines = 0
    truncated_files = 0
    excluded_records = 0
    excluded_journals = 0

    for path in chosen:
        try:
            raw_lines, bad, truncated = _read_journal(path, max_lines, max_line_bytes)
        except (OSError, UnicodeError):
            invalid_lines += 1
            continue
        invalid_lines += bad
        if truncated:
            truncated_files += 1
        events: list[dict[str, Any]] = []
        journal_excluded = False
        for index, line in enumerate(raw_lines, start=1):
            try:
                # ValueError covers integers over the conversion limit; they are not JSONDecodeError.
                # RecursionError is a nested value still inside the byte cap; skip that line only.
                row = json.loads(line)
            except (ValueError, OverflowError, RecursionError):
                invalid_lines += 1
                continue
            if not isinstance(row, dict):
                invalid_lines += 1
                continue
            event_name = row.get("event")
            try:
                parsed = _event_from_row(row, index)
            except (ValueError, OverflowError):
                invalid_lines += 1
                continue
            if parsed is None:
                if isinstance(event_name, str) and event_name in {
                    "provider_result", "repair", "autopsy", "node_terminal",
                }:
                    invalid_lines += 1
                continue
            if parsed["event"] == "provider_result":
                pair = (parsed["agent"], parsed["model"])
                allowed = bool(pair[0]) and pair[0] not in exclude_agents and pair in allow
                parsed["allowed"] = allowed
                parsed["tier"] = allow.get(pair) if allowed else None
                if not allowed:
                    excluded_records += 1
                    journal_excluded = True
            events.append(parsed)
        if journal_excluded:
            excluded_journals += 1
        journals.append({"path": str(path), "events": events})

    return {
        "journals": len(journals),
        "files_matched": len(matched),
        "files_read": len(chosen),
        "truncated_files": truncated_files,
        "invalid_lines": invalid_lines,
        "excluded_records": excluded_records,
        "excluded_journals": excluded_journals,
        "nodes": journals,
    }


def _terminal(events: list[dict[str, Any]]) -> tuple[str | None, int | None]:
    status = None
    round_number = None
    for event in events:
        if event["event"] == "node_terminal":
            status = event["status"]
            round_number = event["round"]
    return status, round_number


def _token_sum(events: list[dict[str, Any]], start_round: int) -> int | None:
    total = None
    for event in events:
        if event["event"] != "provider_result" or not event.get("allowed"):
            continue
        if event["round"] < start_round:
            continue
        tokens = event.get("fresh_input_tokens")
        if tokens is None:
            continue
        total = tokens if total is None else total + tokens
    return total


def decision_points(loaded: dict[str, Any]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for journal in loaded["nodes"]:
        by_node: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in journal["events"]:
            by_node[event["node"]].append(event)
        for node, events in by_node.items():
            makers: dict[int, dict[str, Any]] = {}
            repairs: dict[int, dict[str, Any]] = {}
            for event in events:
                if event["event"] == "provider_result" and event.get("role") == "maker" and event.get("allowed"):
                    makers[event["round"]] = event
                elif event["event"] == "repair":
                    repairs[event["round"]] = event
            status, terminal_round = _terminal(events)
            # Excluded providers are not episode length. Repair and autopsy in a
            # round with no allowed maker are dependent on that synthetic maker
            # and must not move end_round or the success cost.
            observed_rounds = []
            for event in events:
                round_number = event.get("round")
                if not isinstance(round_number, int):
                    continue
                if event["event"] == "provider_result" and event.get("allowed") is False:
                    continue
                if event["event"] in {"repair", "autopsy"} and round_number not in makers:
                    continue
                observed_rounds.append(round_number)
            if terminal_round is not None:
                observed_rounds.append(terminal_round)
            end_round = max(observed_rounds) if observed_rounds else 0
            # No node_terminal means the episode is still open or truncated.
            # That is not evidence of failure.
            resolved = status is not None
            success = status.startswith("ACCEPTED") if resolved else None
            for round_number, repair in repairs.items():
                maker = makers.get(round_number)
                nxt = makers.get(round_number + 1)
                if maker is None or nxt is None:
                    continue
                action = "continue" if (nxt["agent"], nxt["model"]) == (maker["agent"], maker["model"]) else "swap"
                state = (repair["code"], round_bucket(round_number), maker["tier"])
                points.append({
                    "utc": repair["utc"],
                    "order": (repair["utc"], journal["path"], repair["order"], node, round_number),
                    "journal": journal["path"],
                    "node": node,
                    "round": round_number,
                    "state": state,
                    "action": action,
                    "resolved": resolved,
                    "success": success,
                    "additional_rounds": max(0, end_round - round_number) if resolved else None,
                    "fresh_input_tokens": _token_sum(events, round_number + 1),
                })
    points.sort(key=lambda point: point["order"])
    return points


def _outcome_sort(name: tuple) -> tuple:
    return (name[0], name[1] if len(name) > 1 else ())


def _fit(points: list[dict[str, Any]], failure_cost: float) -> tuple[dict[Any, float], dict[Any, str]]:
    grouped: dict[tuple[Any, str], dict[tuple, list[float]]] = defaultdict(lambda: defaultdict(list))
    resolved_points = [point for point in points if point.get("resolved")]
    for point in resolved_points:
        later = [
            other for other in resolved_points
            if other["journal"] == point["journal"] and other["node"] == point["node"] and other["round"] > point["round"]
        ]
        if later:
            nxt = min(later, key=lambda other: other["round"])
            kind = ("next", nxt["state"])
            cost = float(nxt["round"] - point["round"])
        elif point["success"]:
            kind = ("success",)
            cost = float(point["additional_rounds"])
        else:
            kind = ("failure",)
            cost = float(failure_cost)
        grouped[(point["state"], point["action"])][kind].append(cost)

    transitions: dict[tuple[Any, str], list[tuple[float, Any, float]]] = {}
    for key, outcomes in grouped.items():
        # Success and failure stay in the support. Add-one on only the observed
        # kinds would assign probability 1 when every sample succeeded.
        outcomes.setdefault(("success",), [])
        outcomes.setdefault(("failure",), [])
        total = sum(len(costs) for costs in outcomes.values())
        names = sorted(outcomes, key=_outcome_sort)
        kinds = len(names)
        weights = [(len(outcomes[name]) + 1) / (total + kinds) for name in names]
        scale = sum(weights)
        probs = [weight / scale for weight in weights]
        probs[-1] += 1.0 - sum(probs)
        triples = []
        for name, prob in zip(names, probs):
            samples = outcomes[name]
            if name[0] == "success":
                destination = TERM_SUCCESS
                # No observed success round-count: the pseudo-count has step cost 0.
                mean_cost = sum(samples) / len(samples) if samples else 0.0
            elif name[0] == "failure":
                destination = TERM_FAILURE
                mean_cost = float(failure_cost)
            else:
                destination = name[1]
                mean_cost = sum(samples) / len(samples)
            triples.append((prob, destination, mean_cost))
        transitions[key] = triples
    if not transitions:
        return {}, {}
    try:
        return value_iteration(transitions, {TERM_SUCCESS: 0.0, TERM_FAILURE: 0.0})
    except ValueError as exc:
        raise PolicyError("MODEL_INVALID", "transition model was rejected") from exc
    except RuntimeError as exc:
        raise PolicyError("VALUE_ITERATION", "value iteration did not converge") from exc


def _beta_row(successes: int, trials: int) -> dict[str, Any]:
    low, high = beta_interval(successes, trials)
    return {
        "successes": successes,
        "trials": trials,
        "mean": beta_mean(successes, trials),
        "low": low,
        "high": high,
    }


def _state_dict(state: tuple[str, str, str]) -> dict[str, str]:
    return {"code": state[0], "round_bucket": state[1], "tier": state[2]}


def _percent(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return 100.0 * part / whole


def build_result(
    loaded: dict[str, Any],
    points: list[dict[str, Any]],
    min_support: int,
    failure_cost: float,
) -> dict[str, Any]:
    usable = [point for point in points if point.get("resolved")]
    support: dict[tuple[str, str, str], dict[str, int]] = defaultdict(lambda: {action: 0 for action in ACTIONS})
    successes: dict[tuple[str, str, str], dict[str, int]] = defaultdict(lambda: {action: 0 for action in ACTIONS})
    codes: dict[str, int] = defaultdict(int)
    actions = {action: 0 for action in ACTIONS}
    token_sum = 0
    token_present = 0
    for point in usable:
        state = point["state"]
        action = point["action"]
        support[state][action] += 1
        successes[state][action] += 1 if point["success"] else 0
        codes[state[0]] += 1
        actions[action] += 1
        if point["fresh_input_tokens"] is None:
            continue
        token_present += 1
        token_sum += point["fresh_input_tokens"]

    values, policy = _fit(usable, failure_cost) if usable else ({}, {})
    covered_states = {
        state for state, counts in support.items()
        if counts["continue"] >= min_support and counts["swap"] >= min_support
    }
    rows = []
    for state in sorted(support):
        covered = state in covered_states
        solver_action = policy.get(state)
        rows.append({
            "state": _state_dict(state),
            "recommendation": solver_action if covered and solver_action in ACTIONS else "uncovered",
            "solver_action": solver_action if solver_action in ACTIONS else None,
            "covered": covered,
            "support": dict(support[state]),
            "p_success": {action: _beta_row(successes[state][action], support[state][action]) for action in ACTIONS},
            "expected_cost": values.get(state) if state in values and math.isfinite(values[state]) else None,
        })

    shadow = {
        "covered_points": 0,
        "agree": 0,
        "disagree": 0,
        "agree_success": 0,
        "agree_not_success": 0,
        "disagree_success": 0,
        "disagree_not_success": 0,
        "observational": True,
        "causal_claim": False,
        "caveat": CAVEAT,
    }
    for point in usable:
        if point["state"] not in covered_states:
            continue
        recommended = policy.get(point["state"])
        if recommended not in ACTIONS:
            continue
        shadow["covered_points"] += 1
        side = "agree" if point["action"] == recommended else "disagree"
        shadow[side] += 1
        shadow[side + ("_success" if point["success"] else "_not_success")] += 1

    covered_points = sum(1 for point in points if point["state"] in covered_states)
    # State inventory and saturation see every decision point, including
    # episodes that never reached node_terminal. Dropping those points hides
    # new states and makes the rule-of-three bound look tighter than the
    # journals support. Transition counts above stay on resolved outcomes.
    observed_states = [point["state"] for point in points]
    sat = saturation(observed_states)
    if usable:
        status = "SHADOW"
        message = CAVEAT
        gap = None
    elif points:
        status = "NO_DATA"
        message = GAP_UNRESOLVED
        gap = GAP_UNRESOLVED
    else:
        status = "NO_DATA"
        message = GAP_NO_DATA
        gap = GAP_NO_DATA
    return {
        "schema_version": SCHEMA_VERSION,
        "skill": SKILL_NAME,
        "command": "analyze",
        "mode": "shadow",
        "status": status,
        "code": status,
        "release_authority": False,
        "message": message,
        "journals": loaded["journals"],
        "files_matched": loaded["files_matched"],
        "files_read": loaded["files_read"],
        "truncated_files": loaded["truncated_files"],
        "invalid_lines": loaded["invalid_lines"],
        "excluded_records": loaded["excluded_records"],
        "excluded_journals": loaded["excluded_journals"],
        "decision_points": len(points),
        "resolved_points": len(usable),
        "unresolved_points": len(points) - len(usable),
        "states": len(set(observed_states)),
        "min_support": min_support,
        "failure_cost": int(failure_cost) if failure_cost == int(failure_cost) else failure_cost,
        "actions": actions,
        "repair_codes": dict(sorted(codes.items())),
        "coverage": {
            "covered_points": covered_points,
            "decision_points": len(points),
            "percent": _percent(covered_points, len(points)),
        },
        "saturation": {
            "unique": sat["unique"],
            "since_last_new": sat["since_last_new"],
            "p_new_upper": sat["p_new_upper"],
            "first_seen": sat["first_seen"],
        },
        "policy": rows,
        "shadow": shadow,
        "fresh_input_tokens": {
            "sum": token_sum if token_present else None,
            "present": token_present,
            "missing": len(usable) - token_present,
        },
        "gap": gap,
    }


def _fmt(value: float | None) -> str:
    if value is None:
        return "null"
    return f"{value:.6f}"


def _prob_cell(row: dict[str, Any]) -> str:
    return f"{row['mean']:.4f} przedzial [{row['low']:.4f}, {row['high']:.4f}] n={row['trials']}"


def _cell(value: str) -> str:
    return value.replace("|", "/").replace("\n", " ")


def render_report(result: dict[str, Any]) -> str:
    actions = result["actions"]
    total = result["decision_points"]
    fitted = result["resolved_points"]
    lines = [
        "# Polityka cieniowa continue/swap",
        "",
        f"status: {result['status']}",
        "release_authority: false",
        "",
        "## Liczniki",
        "",
        f"- dzienniki: {result['journals']} (dopasowane pliki {result['files_matched']}, czytane {result['files_read']}, uciete {result['truncated_files']})",
        f"- punkty decyzji: {total}",
        f"- rozstrzygniete: {result['resolved_points']}",
        (
            f"- nierozstrzygniete bez node_terminal: {result['unresolved_points']}"
            " (bez przypisania porazki)"
        ),
        f"- stany: {result['states']} (wszystkie punkty decyzji, takze bez node_terminal)",
        f"- zle linie: {result['invalid_lines']}",
        f"- wykluczone rekordy: {result['excluded_records']}",
        f"- wykluczone dzienniki: {result['excluded_journals']}",
        "",
        "## Nierownowaga akcji",
        "",
        f"- continue: {actions['continue']} ({_percent(actions['continue'], fitted):.1f}%)",
        f"- swap: {actions['swap']} ({_percent(actions['swap'], fitted):.1f}%)",
        "- Raport pokazuje empiryczna nierownowage continue wobec swap oraz rozklad kodow naprawy.",
        "",
        "## Kody naprawy",
        "",
    ]
    if result["repair_codes"]:
        for code, count in result["repair_codes"].items():
            lines.append(f"- {code}: {count}")
    else:
        lines.append("- brak")
    sat = result["saturation"]
    lines.extend([
        "",
        "## Saturacja (rule of three)",
        "",
        f"- unique: {sat['unique']}",
        f"- since_last_new: {sat['since_last_new']}",
        f"- p_new_upper: {_fmt(sat['p_new_upper'])}",
        f"- first_seen: {sat['first_seen']}",
        "- Krzywa obejmuje wszystkie punkty decyzji, takze bez node_terminal. Nierozstrzygniety wynik nie wchodzi do estymacji polityki.",
        "",
        "## Pokrycie",
        "",
        (
            f"- coverage: {result['coverage']['covered_points']} / {result['coverage']['decision_points']}"
            f" ({result['coverage']['percent']:.1f}%)"
        ),
        f"- min_support: {result['min_support']} dla obu akcji (continue i swap)",
        "- Stan covered wymaga progu min_support dla continue i dla swap. Inaczej rekomendacja to uncovered (autopsja albo LLM).",
        "",
        "## Tabela polityki",
        "",
        "| stan | rekomendacja | P(sukces) continue | przedzial continue | P(sukces) swap | przedzial swap | support continue | support swap | covered |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    if not result["policy"]:
        lines.append("| brak | uncovered | brak | brak | brak | brak | 0 | 0 | false |")
    for row in result["policy"]:
        state = row["state"]
        label = _cell(f"{state['code']} / {state['round_bucket']} / {state['tier']}")
        cont = row["p_success"]["continue"]
        swap = row["p_success"]["swap"]
        lines.append(
            "| {state} | {rec} | {cm} | {ci} | {sm} | {si} | {sc} | {ss} | {cov} |".format(
                state=label,
                rec=row["recommendation"],
                cm=f"{cont['mean']:.4f}",
                ci=f"[{cont['low']:.4f}, {cont['high']:.4f}]",
                sm=f"{swap['mean']:.4f}",
                si=f"[{swap['low']:.4f}, {swap['high']:.4f}]",
                sc=row["support"]["continue"],
                ss=row["support"]["swap"],
                cov="true" if row["covered"] else "false",
            )
        )
    shadow = result["shadow"]
    lines.extend([
        "",
        "## Porownanie cieniowe",
        "",
        f"- punkty w stanach covered: {shadow['covered_points']}",
        f"- zgodne z polityka: {shadow['agree']} (sukces {shadow['agree_success']}, inny wynik {shadow['agree_not_success']})",
        f"- niezgodne z polityka: {shadow['disagree']} (sukces {shadow['disagree_success']}, inny wynik {shadow['disagree_not_success']})",
        "",
        "## Zastrzezenie",
        "",
        CAVEAT,
        "",
    ])
    if result["gap"]:
        lines.extend(["## Luka", "", result["gap"], ""])
    return "\n".join(lines)


def _positive_int(value: Any, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or type(value) is not int or value < minimum:
        raise PolicyError("BAD_ARGUMENT", f"{name} must be an integer >= {minimum}")
    return value


def _cost(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyError("BAD_ARGUMENT", "failure_cost must be a finite number >= 0")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise PolicyError("BAD_ARGUMENT", "failure_cost must be a finite number >= 0")
    return number


def analyze(
    telemetry_root: Path,
    catalog: Path,
    out_dir: Path,
    min_support: int = DEFAULT_MIN_SUPPORT,
    failure_cost: float = DEFAULT_FAILURE_COST,
    exclude_agents: list[str] | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_lines: int = DEFAULT_MAX_LINES,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> dict[str, Any]:
    out_dir = ensure_out_dir(Path(out_dir))
    min_support = _positive_int(min_support, "min_support")
    failure_cost = _cost(failure_cost)
    max_files = _positive_int(max_files, "max_files")
    max_lines = _positive_int(max_lines, "max_lines")
    max_line_bytes = _positive_int(max_line_bytes, "max_line_bytes")
    excluded = []
    for agent in exclude_agents or []:
        cleaned = clean_text(agent, MAX_TEXT)
        if not cleaned:
            raise PolicyError("BAD_ARGUMENT", "exclude-agent must be a nonempty string")
        excluded.append(cleaned)
    if len(excluded) > MAX_EXCLUDE:
        raise PolicyError("BAD_ARGUMENT", "too many exclude-agent values")
    allow = load_catalog(Path(catalog))
    loaded = load_telemetry(
        Path(telemetry_root), allow, set(excluded), max_files, max_lines, max_line_bytes,
    )
    result = build_result(loaded, decision_points(loaded), min_support, failure_cost)
    result["out_dir"] = str(out_dir)
    atomic_text(out_dir / "policy.json", dump_json(result))
    atomic_text(out_dir / "report.md", render_report(result))
    return result


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise PolicyError("BAD_ARGUMENT", clean_text(message, 300) or "invalid arguments")


def _parser() -> argparse.ArgumentParser:
    parser = Parser(prog="decision_policy.py", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    analyze_parser = commands.add_parser("analyze")
    analyze_parser.add_argument("--telemetry-root", type=Path, default=DEFAULT_TELEMETRY_ROOT)
    analyze_parser.add_argument("--catalog", type=Path)
    analyze_parser.add_argument("--out", type=Path, required=True)
    analyze_parser.add_argument("--min-support", type=int, default=DEFAULT_MIN_SUPPORT)
    analyze_parser.add_argument("--failure-cost", type=float, default=DEFAULT_FAILURE_COST)
    analyze_parser.add_argument("--exclude-agent", action="append", dest="exclude_agents")
    analyze_parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    analyze_parser.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    analyze_parser.add_argument("--max-line-bytes", type=int, default=DEFAULT_MAX_LINE_BYTES)
    return parser


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(dump_json(payload))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        args = _parser().parse_args(argv)
        result = analyze(
            telemetry_root=args.telemetry_root,
            catalog=resolve_catalog(args.catalog),
            out_dir=args.out,
            min_support=args.min_support,
            failure_cost=args.failure_cost,
            exclude_agents=args.exclude_agents,
            max_files=args.max_files,
            max_lines=args.max_lines,
            max_line_bytes=args.max_line_bytes,
        )
    except PolicyError as exc:
        _emit({
            "status": "ERROR",
            "code": exc.code,
            "message": exc.message,
            "release_authority": False,
        })
        return 1
    except OSError:
        _emit({
            "status": "ERROR",
            "code": "IO_ERROR",
            "message": "failed to write the policy report",
            "release_authority": False,
        })
        return 1
    except (ValueError, OverflowError, RecursionError):
        # Sanitized: a nested line that escapes the parser must not echo its text.
        _emit({
            "status": "ERROR",
            "code": "INVALID_TELEMETRY",
            "message": "telemetry value could not be parsed",
            "release_authority": False,
        })
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
