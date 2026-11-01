"""Explicit bounded curator run using OpenClaw/Qwen and Muse subscription review.

The parent/installed owner policy supplies exact proposal paths. No discovery of
credentials, remote/API fallback, arbitrary model tools, or unreviewed publication.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import datetime as dt
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

from caretaker import tick_budget
from caretaker.config import TransportSpec, load_config
from caretaker.hub import CanonicalKnowledgeGateway
from caretaker.pipeline import _write_run_config
from caretaker.filter import contains_secret
from caretaker.privacy import outbound_text_safe
from caretaker.publication import (
    PublicationError, freeze, synthesis_prompt, build_candidate, bind_review,
    sha, json_bytes, response_object, is_format_error, repair_prompt,
    safe_diagnostics, normalize_review_finding, review_allows_publication,
    review_has_blocking_findings, semantic_review_prompt_prefix,
)
from caretaker.checkpoint import checkpoint_publication
from caretaker.audit import accounted_proposal_ids
from caretaker.transports import (
    AgentFleetArgvTransport, OpenClawArgvTransport, PREVIOUS_OUTPUT_LIMIT,
    TransportError,
)
import logging

_LOG = logging.getLogger(__name__)


def _format_canonical_error(code: str, raw_reason: str) -> str:
    """Format canonical failure into canonical:<kod>:<krótki_powód> <= 120 chars, printable, no user paths."""
    clean_code = re.sub(r'[^A-Za-z0-9_-]', '_', str(code or 'error').strip().lower())[:30] or 'error'
    if any(k in clean_code for k in ("users", "home", "user", "appdata", "slash")):
        clean_code = "error"
    text = str(raw_reason or '').strip()
    # Redact user paths with single or escaped backslashes, forward slashes, spaces, commas, and apostrophes
    text = re.sub(r'(?i)[a-z]:\\+users\\+[^:"\r\n\t}{<>|*?`]+', '[USER_PATH]', text)
    text = re.sub(r'(?i)/+(?:home|users)/+[^:"\r\n\t}{<>|*?`]+', '[USER_PATH]', text)
    text = " ".join(text.split())
    text = "".join(ch for ch in text if ch.isprintable())
    prefix = f"canonical:{clean_code}:"
    budget = 120 - len(prefix)
    if budget <= 0:
        return prefix[:120]
    short_reason = text[:budget].rstrip()
    return f"{prefix}{short_reason}" if short_reason else prefix.rstrip(":")


def command(argv, timeout=180):
    timeout = tick_budget.timeout(timeout, minimum=10, margin=5)
    p = subprocess.run(argv, capture_output=True, timeout=timeout, encoding='utf-8', errors='replace', creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if p.returncode:
        stderr = (p.stderr or '').strip()
        parsed_error = None
        for part in [stderr, *stderr.splitlines()]:
            try:
                diagnostic = json.loads(part)
            except json.JSONDecodeError:
                continue
            if isinstance(diagnostic, dict) and isinstance(diagnostic.get('error'), str):
                err = diagnostic['error'].strip()
                if err:
                    parsed_error = err
                    break
        if parsed_error:
            low = parsed_error.lower()
            if "baseline_changed" in low:
                code = "baseline_changed"
            elif "curator_lock" in low or "lock_held" in low:
                code = "curator_lock_held"
            elif "transaction rolled back" in low:
                code = "transaction_rollback"
            elif "accounted_elsewhere" in low:
                code = "accounted_elsewhere"
            elif ":" in parsed_error:
                potential = parsed_error.split(":", 1)[0].strip()
                if 2 <= len(potential) <= 24 and " " not in potential and "/" not in potential and "\\" not in potential:
                    code = potential
                else:
                    code = "error"
            else:
                code = "error"
            raise PublicationError(_format_canonical_error(code, parsed_error))

        # Bound unstructured output before it can reach the maintenance log.
        lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        last_line = lines[-1] if lines else f"exit_{p.returncode}"
        low = last_line.lower()
        if "baseline_changed" in low:
            code = "baseline_changed"
        elif "curator_lock" in low or "lock_held" in low:
            code = "curator_lock_held"
        elif "transaction rolled back" in low:
            code = "transaction_rollback"
        elif "accounted_elsewhere" in low:
            code = "accounted_elsewhere"
        else:
            code = f"exit_{p.returncode}"
        raise PublicationError(_format_canonical_error(code, last_line))
    return json.loads(p.stdout)


def _envelope_raw(env) -> dict:
    if isinstance(env, dict):
        return env
    if isinstance(getattr(env, "raw", None), dict):
        return env.raw
    return {
        "ok": getattr(env, "ok", True),
        "status": getattr(env, "status", "ok"),
        "provider": getattr(env, "provider", None),
        "model": getattr(env, "model", None),
        "sessionId": getattr(env, "session_id", None) or getattr(env, "sessionId", None),
        "final": getattr(env, "final", None),
    }


def _is_placeholder_or_tool_denial(obj) -> bool:
    """Detect tool denials and placeholders in maker envelope/summary without false positives on body text."""
    if isinstance(obj, str):
        low = obj.strip().lower()
        if low in {"test", '"test"', "'test'", "placeholder", '{"summary": "test"}'}:
            return True
        if low.startswith("tool call failed:") or low.startswith("permission denied"):
            return True
        if "permission_denials" in low or "permission denied for write" in low:
            return True
        return False
    elif isinstance(obj, dict):
        summary_val = obj.get("summary")
        if isinstance(summary_val, str):
            s_low = summary_val.strip().lower()
            if s_low in {"test", "placeholder"} or s_low.startswith("tool call failed:") or "permission denied for write" in s_low:
                return True
        elif isinstance(summary_val, dict):
            if _is_placeholder_or_tool_denial(summary_val):
                return True

        if obj.get("verification") == ["test"] or obj.get("verification") == "test":
            return True

        body_val = obj.get("body")
        if isinstance(body_val, str):
            b_low = body_val.strip().lower()
            if b_low in {"test", "placeholder"} or b_low.startswith("tool call failed: permission denied"):
                return True

        if obj.get("permission_denials"):
            return True
        if obj.get("terminal_reason") == "permission_denied":
            return True
    elif isinstance(obj, list):
        if obj == ["test"]:
            return True
        for item in obj:
            if isinstance(item, str) and item.strip().lower() in {"test", "placeholder"}:
                return True
            if isinstance(item, dict) and _is_placeholder_or_tool_denial(item):
                return True
    return False


def _maker_summary_object(summary: str) -> dict:
    """Recover only one excess closing brace on a complete Fleet decision.

    Keep the producer response intact. Content and source validation still run
    after this transport-only normalization; other malformed JSON is rejected.
    """
    try:
        return response_object(summary)
    except json.JSONDecodeError as exc:
        if exc.msg != "Extra data":
            raise
        text = summary.strip()
        value, end = json.JSONDecoder().raw_decode(text)
        if (text[end:].strip() != "}"
                or not isinstance(value, dict)
                or set(value) != {"body", "summary", "dispositions"}
                or not isinstance(value["body"], str)
                or not isinstance(value["summary"], str)
                or not isinstance(value["dispositions"], dict)):
            raise
        return value


def _decision_from_maker(env) -> dict:
    try:
        parsed = response_object(env.final)
        # Fleet requires a handoff envelope; the publication decision lives
        # in summary, while the local maker returns the decision directly.
        if "summary" in parsed and "dispositions" not in parsed:
            summary = parsed["summary"]
            if _is_placeholder_or_tool_denial(summary) or _is_placeholder_or_tool_denial(parsed):
                raise PublicationError("maker_tool_use_or_placeholder_prohibited")
            if isinstance(summary, dict):
                if _is_placeholder_or_tool_denial(summary):
                    raise PublicationError("maker_tool_use_or_placeholder_prohibited")
                return summary
            if isinstance(summary, str):
                inner = _maker_summary_object(summary)
                if _is_placeholder_or_tool_denial(inner):
                    raise PublicationError("maker_tool_use_or_placeholder_prohibited")
                return inner
            raise PublicationError("maker_response_not_json")
        if _is_placeholder_or_tool_denial(parsed):
            raise PublicationError("maker_tool_use_or_placeholder_prohibited")
        return parsed
    except json.JSONDecodeError as exc:
        raise PublicationError("maker_response_not_json") from exc


def _model_lineage(provider, model):
    """Compare model families across Fleet adapters and local transports."""
    name = str(model or "").casefold()
    for family in ("claude", "sonnet", "opus", "haiku", "qwen", "minimax",
                   "muse", "grok", "gemini", "deepseek", "gpt"):
        if family in name:
            return "claude" if family in {"sonnet", "opus", "haiku"} else family
    return str(provider or "").casefold()


def _phase_timeout(configured: int, *, reserve: int = 200, minimum: int = 60) -> int:
    """Bound one model phase while retaining time to persist its next phase."""
    budget = tick_budget.current()
    if budget is None:
        return configured
    remaining = budget.deadline - budget.clock()
    required = min(configured, minimum) + reserve + 5
    if remaining < required:
        raise tick_budget.TickBudgetExceeded("tick_time_budget_exhausted")
    return min(configured, max(minimum, int(remaining - reserve - 5)))


def _fleet_maker_rule():
    return ("\n\nANSWER PLACEMENT. Agent Fleet requires a handoff envelope. "
            "Put the publication decision JSON required above as the ENTIRE "
            "summary string, without prose or markdown. Set files_changed to []. "
            "DO NOT use tools, do not invoke Write, Edit, Bash or any other tool, "
            "and do not write files. Do not emit placeholder text like 'test'. "
            "Emit the complete valid publication decision JSON directly in summary.")


def _skipped_candidate(candidate: dict, reason_code: str) -> dict:
    """Record only declared identity and a stable code, never error text."""
    argv = list(getattr(candidate.get("spec"), "argv", ()) or ())
    def flag(name):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None
    transport = candidate.get("transport")
    return {
        "agent": (candidate.get("lineage") or flag("--agent")
                  or getattr(transport, "provider", None) or "unknown"),
        "model": (candidate.get("model") or flag("--model")
                  or getattr(transport, "model", None) or "unknown"),
        "reason_code": reason_code or "transport_error",
    }


_MAX_DIAGNOSTIC_TEXT = 2000


def _sanitize_diagnostic_text(value) -> str:
    """Apply the same secret filter as the repair prompt; bound untrusted length."""
    text = str(value)
    if contains_secret(text):
        return ""
    if len(text) > _MAX_DIAGNOSTIC_TEXT:
        return text[:_MAX_DIAGNOSTIC_TEXT]
    return text


def _sanitize_finding_items(findings) -> list:
    """Drop secret-bearing reviewer findings; keep remaining items verbatim."""
    if not isinstance(findings, list) or not findings:
        return []
    parts = []
    for item in findings:
        if isinstance(item, dict):
            text = normalize_review_finding(item)["text"]
        else:
            text = item
        text = _sanitize_diagnostic_text(text)
        if not text:
            continue
        parts.append(text)
    return parts


def _finding_detail_records(findings) -> list:
    """Sanitized finding rows with weight and whether a fix was supplied."""
    if not isinstance(findings, list) or not findings:
        return []
    rows = []
    for item in findings:
        normalized = normalize_review_finding(item)
        text = _sanitize_diagnostic_text(normalized["text"])
        if not text:
            continue
        fix = normalized.get("fix")
        source_id = ""
        if isinstance(fix, dict):
            sid = fix.get("source_id")
            if isinstance(sid, str):
                source_id = _sanitize_diagnostic_text(sid)
        raw_class = normalized.get("class")
        finding_class = None
        if isinstance(raw_class, str) and raw_class:
            finding_class = _sanitize_diagnostic_text(raw_class) or None
        rows.append({
            "text": text,
            "weight": normalized["weight"],
            "class": finding_class,
            "fix_supplied": bool(fix),
            "source_id": source_id or None,
        })
    return rows


def _sanitize_findings(findings) -> str:
    """Join reviewer findings without length clipping.

    Secret-bearing items are dropped here; prompt-bound clipping happens
    only in ``repair_prompt`` so the maker sees complete findings when they
    fit, and an explicit truncation notice when they do not.
    """
    parts = _sanitize_finding_items(findings)
    return "\n".join(parts) if parts else "review_requested_repair"


def _review_round_record(round_number: int, parsed) -> dict:
    """Sanitized per-round reviewer charges for the diagnostics file only."""
    raw_verdict = parsed.get("verdict") if isinstance(parsed, dict) else None
    if not isinstance(raw_verdict, str) or not raw_verdict:
        verdict = "not_accepted"
    else:
        verdict = _sanitize_diagnostic_text(raw_verdict) or "not_accepted"
    findings = parsed.get("findings") if isinstance(parsed, dict) else None
    if verdict in {"PASS", "REPAIR"} and isinstance(findings, list) and not review_has_blocking_findings(findings):
        summary = _advisory_summary(findings)
        record = {"round": round_number, "verdict": verdict,
                  "advisory_count": summary["count"],
                  "advisory_classes": summary["classes"],
                  "no_findings": not findings}
        if not findings:
            record.update({"findings": [], "finding_details": []})
        return record
    details = _finding_detail_records(findings)
    if verdict == "PASS" and not details:
        return {
            "round": round_number,
            "verdict": "PASS",
            "findings": [],
            "finding_details": [],
            "no_findings": True,
        }
    items = [row["text"] for row in details]
    if not items:
        items = ["review_requested_repair"]
        details = [{
            "text": "review_requested_repair",
            "weight": "blocking",
            "class": None,
            "fix_supplied": False,
            "source_id": None,
        }]
    return {
        "round": round_number,
        "verdict": verdict,
        "findings": items,
        "finding_details": details,
        "no_findings": False,
    }


def _advisory_summary(findings: list) -> dict:
    """Retain advisory categories and count, never reviewer prose or fixes."""
    classes = set()
    for item in findings:
        value = normalize_review_finding(item).get("class")
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value) and not contains_secret(value):
            classes.add(value)
    return {"count": len(findings), "classes": sorted(classes)}


def _post_review_error(exc: Exception) -> tuple[str, str]:
    """Stable content-free error code and underlying exception class."""
    raw = str(exc) if isinstance(exc, PublicationError) else ""
    code = raw if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", raw) and not contains_secret(raw) else type(exc).__name__.lower()
    return code, type(exc).__name__


#: Stable deferral reasons emitted by ``_invoke_fleet_review`` when the
#: reviewer's verdict cannot be accepted. These codes MUST NOT include any
#: review content (findings, prose, or verdict text); the operator must
#: see what went wrong, not the leaked review.
_FLEET_DEFER_REASON_VERDICT_MISSING = "fleet_verdict_missing"
_FLEET_DEFER_REASON_VERDICT_MANIFEST_MISMATCH = "fleet_verdict_manifest_mismatch"
_FLEET_DEFER_REASON_VERDICT_INVALID = "fleet_verdict_invalid"
_FLEET_DEFER_REASON_ENVELOPE_NOT_OBJECT = "fleet_envelope_not_object"

#: Field names searched (in order) for an embedded verdict JSON block.
#: The 2026-09-20 Fleet envelope exposes the reviewer's verdict as a
#: JSON block inside the handoff's ``summary`` string; the helper still
#: inspects ``final`` and ``handoff_summary`` so future Fleet response
#: shapes do not silently bypass extraction.
_VERDICT_BLOCK_FIELDS = ("summary", "final", "handoff_summary", "verdict_text")

#: Re-prompts of the SAME reviewer after an answer in the wrong shape.
#: Measured 2026-09-23 on the caretaker's own prompt: five of seven Fleet
#: reviewers judged the planted defect correctly yet were discarded for the
#: envelope shape (prose summary, schema pasted instead of an instance, an
#: unparseable JSON string). The prompt asked for "only JSON" while Fleet
#: demands its own handoff envelope - a contract conflict, not a model fault.
FLEET_FORMAT_REPAIR_ATTEMPTS = 2

#: Upper bound of a format-repair task in characters. Measured 2026-09-23: a
#: 35.8 kB repair task failed before the model started (WinError 206) because the
#: Antigravity adapter passes it on the command line; Fleet adds ~2.5 kB and
#: Polish text is wider in UTF-8, hence the margin below 32767.
REPAIR_PROMPT_MAX_CHARS = 26000

#: Fleet agents whose adapter passes the task as a command-line argument.
ARGV_TASK_AGENTS = frozenset({"antigravity"})

#: Transport failures about the SHAPE of the answer. Everything else (quota,
#: gate refusal, timeout, spawn, identity) is not fixable by re-prompting.
_REPAIRABLE_TRANSPORT_CODES = frozenset({"handoff_contract", "invalid_envelope"})


def fleet_envelope_rule(digest):
    """Where the verdict goes when the reviewer runs inside Agent Fleet.

    Resolves the conflict between "return only JSON" and Fleet's mandatory
    handoff envelope: the verdict JSON is the ENTIRE value of ``summary``.
    """
    return (
        "\n\nANSWER PLACEMENT. You run inside Agent Fleet, which requires its own handoff "
        "envelope (status, summary, files_changed, verification, risks, next_steps). That is "
        "compatible with the instruction above: put your verdict object, serialized as JSON, as "
        "the ENTIRE value of the envelope's `summary` string - no prose, no markdown fence, "
        "nothing before or after it. Exactly: {\"verdict\":\"PASS\" or \"REPAIR\","
        "\"manifest_sha256\":\"" + digest + "\",\"findings\":[...]}, each finding shaped as "
        "described above. `verification`, `risks` and `next_steps` are arrays of short plain "
        "strings and `files_changed` is []. Fill the object with YOUR verdict; never paste this "
        "description or a schema back."
    )


def _transport_format_problem(exc):
    """Deterministic feedback for a repairable transport failure."""
    detail = getattr(exc, "detail", None)
    if exc.reason_code == "handoff_contract" and detail:
        return "Agent Fleet rejected your handoff envelope: " + str(detail)[:1500]
    return "Agent Fleet could not read a handoff envelope from your answer"


def _missing_verdict_problem(parsed):
    """Say precisely why no verdict was found, so a retry can fix exactly that."""
    for text in _candidate_texts(parsed):
        block = _extract_verdict_block(text)
        if block is not None:
            return ('your verdict object has "verdict": ' + json.dumps(block.get("verdict"))[:40]
                    + '; it must be exactly "PASS" or "REPAIR"')
    return ("no JSON object with a top-level \"verdict\" key was found in the envelope fields "
            + "/".join(_VERDICT_BLOCK_FIELDS) + "; a verdict written as prose is never accepted")


def _candidate_texts(parsed):
    targets = [parsed]
    if isinstance(parsed, dict) and isinstance(parsed.get("handoff"), dict):
        targets.append(parsed["handoff"])
    for target in targets:
        if not isinstance(target, dict):
            continue
        for field in _VERDICT_BLOCK_FIELDS:
            if isinstance(target.get(field), str):
                yield target[field]


def _format_repair_prompt(review_prompt, digest, problem, previous, attempt):
    """Re-prompt for the SAME reviewer: exact reason, its own answer, the task.

    Each Fleet run is a fresh conversation, so the unchanged task travels with
    the repair; the previous answer lets the reviewer reformat its analysis
    instead of redoing it. Nothing here is persisted into a publication result.
    """
    head = ("FORMAT REPAIR %d/%d. Your previous answer to the review task below could not be "
            "used, for this exact reason:\n  %s\n" % (attempt, FLEET_FORMAT_REPAIR_ATTEMPTS, problem))
    tail = ("Answer again with the Agent Fleet handoff envelope whose `summary` is EXACTLY one JSON "
            "object: {\"verdict\":\"PASS\" or \"REPAIR\",\"manifest_sha256\":\"" + digest + "\","
            "\"findings\":[...]}. A verdict written as prose is never accepted.\n"
            "The original review task follows unchanged.\n\n")
    parts = [head]
    # 0.1.17: the whole repair must still fit a Windows command line (32767
    # characters, WinError 206), because the Antigravity adapter passes the task
    # as an argument. The previous answer is the part that gives way.
    room = REPAIR_PROMPT_MAX_CHARS - len(head) - len(tail) - len(review_prompt) - 200
    keep = min(PREVIOUS_OUTPUT_LIMIT, room)
    if previous and keep >= 500:
        parts.append(
            "Your previous answer, for reference (reuse your own analysis; redo the review "
            "only if it is empty or unusable):\n<<<\n" + previous[:keep]
            + "\n>>>\n")
    parts.append(tail)
    return "".join(parts) + review_prompt


def _extract_verdict_block(text):
    """Find the first JSON object with a top-level ``verdict`` key.

    Walks all brace-prefixed substrings and tries to ``json.JSONDecoder``-
    decode each. Returns the first object whose top-level ``verdict`` is
    a string, or ``None`` when no candidate decodes to such an object.
    A bare ``PASS`` token in prose is NEVER accepted: a verdict must be
    a structured object with at least ``verdict`` (and ideally
    ``manifest_sha256`` and ``findings``). ``text`` that is not a string
    yields ``None``.
    """
    if not isinstance(text, str):
        return None
    decoder = json.JSONDecoder()
    i = 0
    n = len(text)
    while i < n:
        brace = text.find("{", i)
        if brace < 0:
            return None
        try:
            obj, _end = decoder.raw_decode(text, brace)
        except json.JSONDecodeError:
            i = brace + 1
            continue
        if isinstance(obj, dict) and isinstance(obj.get("verdict"), str):
            return obj
        i = brace + 1
    return None


def _wrap_verdict(obj):
    """Build the canonical ``{verdict, manifest_sha256, findings}`` shape.

    Returns ``None`` when ``obj`` does not carry an accepted verdict
    token (``"PASS"`` or ``"REPAIR"``). ``manifest_sha256`` and
    ``findings`` are normalised so the caller can rely on their types
    regardless of how the reviewer's envelope shaped them.
    """
    if not isinstance(obj, dict):
        return None
    verdict = obj.get("verdict")
    if not isinstance(verdict, str) or verdict not in {"PASS", "REPAIR"}:
        return None
    manifest_sha = obj.get("manifest_sha256")
    if not isinstance(manifest_sha, str):
        manifest_sha = None
    findings = obj.get("findings")
    if not isinstance(findings, list):
        findings = []
    else:
        findings = list(findings)
    return {
        "verdict": verdict,
        "manifest_sha256": manifest_sha,
        "findings": findings,
    }


def _extract_fleet_verdict(parsed, expected_digest):
    """Pull a structured verdict out of a parsed Fleet envelope.

    Three paths, ordered:

    1. Top-level verdict in ``parsed`` — used when the envelope's
       ``final`` already carries the review JSON directly, or when a
       future Fleet response lifts the verdict into the handoff root.
    2. Verdict embedded in a string field (``summary``, ``final``,
       ``handoff_summary``) — the 2026-09-20 Fleet envelope returns
       the verdict inside the handoff's ``summary`` as a JSON block
       mixed with prose. The same scan is repeated one level deep on a
       nested ``handoff`` dict, in case the real envelope wraps the
       review content in an additional layer.
    3. (Forward compatibility) Verdict directly in a nested
       ``handoff`` dict — handled implicitly by the top-level scan
       because the real envelope's ``final`` IS the handoff dict.

    A bare ``PASS`` token in prose is NEVER accepted: the helper only
    accepts a structured verdict object (a JSON object with a top-level
    ``verdict`` string field).

    Returns ``(status, verdict_dict)`` where ``status`` is one of:

    - ``"ok"`` — verdict was extracted and ``manifest_sha256`` matches
      ``expected_digest``.
    - ``"manifest_mismatch"`` — verdict was extracted but its
      ``manifest_sha256`` does not match ``expected_digest``. The
      verdict dict is returned so the caller can raise a stable
      deferral reason without leaking the verdict contents.
    - ``"missing"`` — no structured verdict could be parsed.

    The caller MUST treat ``"manifest_mismatch"`` and ``"missing"`` as
    a deferral for this candidate, never as a silent REJECT.
    """
    if not isinstance(parsed, dict):
        return ("missing", None)

    # Inspect the parsed root and any nested ``handoff`` dict (some
    # envelope shapes embed an extra wrapper).
    inspect_targets = [parsed]
    nested = parsed.get("handoff")
    if isinstance(nested, dict) and nested is not parsed:
        inspect_targets.append(nested)

    # Path 1: verdict already at the top level of one of the dicts.
    for target in inspect_targets:
        wrapped = _wrap_verdict(target)
        if wrapped is None:
            continue
        if wrapped["manifest_sha256"] != expected_digest:
            return ("manifest_mismatch", wrapped)
        return ("ok", wrapped)

    # Path 2: scan known string fields for an embedded verdict JSON.
    for target in inspect_targets:
        for field in _VERDICT_BLOCK_FIELDS:
            text = target.get(field)
            if not isinstance(text, str):
                continue
            block = _extract_verdict_block(text)
            if block is None:
                continue
            wrapped = _wrap_verdict(block)
            if wrapped is None:
                continue
            if wrapped["manifest_sha256"] != expected_digest:
                return ("manifest_mismatch", wrapped)
            return ("ok", wrapped)

    return ("missing", None)


def produce_reviewed_candidate(*, frozen: dict, run_root: Path, observed: str,
                               cfg, rid: str, maker=None, reviewer=None,
                               fleet_reviewers=None, fleet_append_makers=None,
                               resume_state=None, progress_callback=None) -> dict:
    """One maker pass, at most one format repair, independent Muse review.

    A review REPAIR gets at most one targeted maker repair and then a NEW
    independent review of the changed manifest. A previous PASS is never
    reused. Reviewer transport failure defers; there is no self-review.

    Advisory-only findings do not block. The curator may then supply an
    evidence-bounded editorial amendment of the NEW section only. That
    amendment is not self-acceptance of maker code: the maker still cannot
    review itself, and curator text must pass the same deterministic gates
    as maker body. A failed gate is treated as a blocking review charge.

    When ``fleet_reviewers`` is supplied, ``reviewer`` is ignored and the
    function walks the candidate list, picking the first transport that
    succeeds AND whose real envelope identity differs from the maker's.
    No candidate at all defers with ``independent_review_deferred``; the
    maker's own lineage is never reused as a reviewer.
    """
    # Resolve budgets and reviewer mode from the publication section when
    # present; fall back to the legacy 360 s values so existing production
    # configs without ``publication`` behave exactly as today.
    pub = getattr(cfg, "publication", None)
    if pub is not None:
        maker_timeout = pub.maker_timeout_seconds
        reviewer_timeout = pub.reviewer_timeout_seconds
        reviewer_mode = pub.reviewer.mode
    else:
        maker_timeout = 360
        reviewer_timeout = 360
        reviewer_mode = "muse"
    primary = maker or OpenClawArgvTransport(expected_identity=("vllm", "Qwen3.8-27B"))
    use_fleet_mode = (reviewer_mode == "fleet" and fleet_reviewers is not None)
    if not use_fleet_mode:
        muse = reviewer or OpenClawArgvTransport(expected_identity=("muse-code", "muse-spark-1.3"))
    resume_state = dict(resume_state or {})
    resume_phase = resume_state.get("phase")
    maker_calls = int(resume_state.get("maker_calls", 0))
    review_calls = int(resume_state.get("review_calls", 0))
    maker_format_repairs = int(resume_state.get("maker_format_repairs", 0))
    reviewer_format_repairs = dict(resume_state.get("reviewer_format_repairs") or {})
    last_error = ""
    last_env = None
    last_reviewed = None
    fleet_review_path = None
    review_rounds = list(resume_state.get("review_rounds") or [])
    maker_attempts = []
    reviewer_attempts = []
    use_append_maker = (frozen.get("preimage_sha256") is not None
                        and fleet_append_makers is not None)
    maker_route = ("fleet_append" if use_append_maker else
                   "local_fallback" if frozen.get("preimage_sha256") is not None
                   else "local_new_page")
    if resume_state.get("maker_provider") or resume_state.get("maker_model"):
        from types import SimpleNamespace
        last_env = SimpleNamespace(
            provider=resume_state.get("maker_provider"),
            model=resume_state.get("maker_model"),
        )
        maker_route = resume_state.get("maker_route") or maker_route

    def checkpoint(phase, candidate, manifest, **extra):
        if progress_callback is None:
            return
        payload = {
            "phase": phase,
            "maker_calls": maker_calls,
            "review_calls": review_calls,
            "maker_format_repairs": maker_format_repairs,
            "reviewer_format_repairs": dict(reviewer_format_repairs),
            "maker_provider": getattr(last_env, "provider", None),
            "maker_model": getattr(last_env, "model", None),
            "maker_route": maker_route,
            "review_rounds": list(review_rounds),
        }
        payload.update(extra)
        progress_callback(phase, candidate, manifest, payload)

    def invoke_maker(prompt: str, tag: str, timeout_override=None):
        nonlocal maker_calls, last_env, maker_route
        if use_append_maker:
            for idx, candidate in enumerate(fleet_append_makers):
                tick_budget.check('curate_once.py:metadata_or_work')
                spec = candidate["spec"]
                maker_cfg = _write_run_config(
                    spec, str(run_root), str(run_root), rid + "-" + tag
                    + "-append" + str(idx))
                try:
                    env = candidate["transport"].invoke(
                        spec=spec, cwd=str(run_root),
                        prompt=prompt + _fleet_maker_rule(),
                        run_config_path=maker_cfg, state_dir=str(run_root),
                        timeout=(timeout_override if timeout_override is not None
                                 else _phase_timeout(maker_timeout)))
                except TransportError as exc:
                    maker_attempts.append(_skipped_candidate(
                        candidate, getattr(exc, "reason_code", None)
                        or "transport_error"))
                    continue
                last_env = env
                maker_calls += 1
                maker_route = "fleet_append"
                return env
            maker_route = "local_fallback"
        primary_cfg = _write_run_config(cfg.primary, str(run_root), str(run_root), rid + "-" + tag)
        last_env = primary.invoke(
            spec=cfg.primary, cwd=str(run_root), prompt=prompt,
            run_config_path=primary_cfg, state_dir=str(run_root),
            timeout=(timeout_override if timeout_override is not None
                     else _phase_timeout(maker_timeout)),
        )
        maker_calls += 1
        return last_env

    def checkpoint_repair_result(env):
        checkpoint("REPAIR_RESULT", None, None,
                   observed=observed, assessment=repair_assessment,
                   maker_response={
                       "provider": getattr(env, "provider", None),
                       "model": getattr(env, "model", None),
                       "session_id": (getattr(env, "session_id", None)
                                      or getattr(env, "sessionId", None)),
                       "final": getattr(env, "final", None),
                       "ok": getattr(env, "ok", True),
                       "status": getattr(env, "status", "ok"),
                   },
                   maker_purpose="candidate-review-repair", repair_attempt=1)

    def review_response_payload():
        return {
            "provider": getattr(last_reviewed, "provider", None),
            "model": getattr(last_reviewed, "model", None),
            "final": getattr(last_reviewed, "final", None),
            "session_id": (getattr(last_reviewed, "session_id", None)
                           or getattr(last_reviewed, "sessionId", None)),
        }

    def checkpoint_review_result(phase, parsed, digest, candidate, manifest):
        response = review_response_payload()
        # The legacy native reviewer has a durable exact-manifest receipt but
        # no returned envelope. Its candidate review files are the proof.
        if not ((response.get("provider") and response.get("model")
                 and isinstance(response.get("final"), str)
                 and isinstance(response.get("session_id"), str))
                or fleet_review_path is not None):
            return
        checkpoint(phase, candidate, manifest, observed=observed,
                   assessment=parsed, review_response=response,
                   fleet_review_path=(str(fleet_review_path)
                                      if fleet_review_path else None),
                   manifest_sha256=digest)

    def one_candidate(prompt: str, name: str, allow_format_repair: bool,
                      *, env_override=None, timeout_override=None):
        nonlocal last_error, maker_format_repairs, last_env
        env = env_override or invoke_maker(
            prompt, "maker-" + name, timeout_override=timeout_override)
        if env_override is not None:
            last_env = env_override
        elif name == "candidate-review-repair":
            checkpoint_repair_result(env)
        try:
            decision = _decision_from_maker(env)
            candidate = run_root / name
            manifest = build_candidate(frozen, decision, candidate, observed)
            return manifest, candidate, False
        except PublicationError as exc:
            last_error = str(exc)
            if not (allow_format_repair and is_format_error(exc)
                    and maker_format_repairs < 2):
                return None, None, is_format_error(exc)
            maker_format_repairs += 1
            repair_timeout = _phase_timeout(maker_timeout)
            if name == "candidate-review-repair":
                checkpoint("REPAIR_STARTED", None, None,
                           observed=observed, assessment=repair_assessment,
                           repair_attempt=1)
            env = invoke_maker(repair_prompt(frozen, str(exc)), "maker-" + name + "-repair",
                               timeout_override=repair_timeout)
            if name == "candidate-review-repair":
                checkpoint_repair_result(env)
            try:
                decision = _decision_from_maker(env)
                candidate = run_root / (name + "-repaired")
                manifest = build_candidate(frozen, decision, candidate, observed)
                return manifest, candidate, False
            except PublicationError as exc2:
                last_error = str(exc2)
                return None, None, True

    def pending(**extra):
        payload = {
            "status": "PENDING",
            "reason": last_error or extra.get("reason", "pending"),
            "maker_calls": maker_calls,
            "review_calls": review_calls,
            "run_path": str(run_root),
            "candidate_path": extra.get("candidate_path"),
            "result_path": str(run_root / "result.json"),
            "primary_model": getattr(last_env, "model", None),
            "maker_agent": getattr(last_env, "provider", None),
            "maker_lineage": _model_lineage(getattr(last_env, "provider", None),
                                             getattr(last_env, "model", None)),
            "maker_route": maker_route,
            "reviewer_model": getattr(last_reviewed, "model", None),
            "proposal_ids": [p["id"] for p in frozen.get("proposals", [])],
            "snapshot_sha256": frozen.get("snapshot_sha256"),
            "target": frozen.get("target"),
            "review_rounds": list(review_rounds),
            "maker_attempts": list(maker_attempts),
            "reviewer_attempts": list(reviewer_attempts),
        }
        payload.update(extra)
        payload["review_rounds"] = list(review_rounds)
        (run_root / "diagnostics.json").write_bytes(json_bytes(safe_diagnostics(payload)))
        return payload

    def invoke_review(manifest: Path, tag: str):
        nonlocal review_calls, last_reviewed, fleet_review_path
        from caretaker.privacy import outbound_packet_safe
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
        candidate_dir = manifest.parent
        candidate_payloads = {
            x["path"]: (candidate_dir / x["payload_path"]).read_text(encoding="utf-8")
            for x in manifest_value["outputs"]
        }
        ledger_value = json.loads((candidate_dir / "ledger.json").read_text(encoding="utf-8"))
        if not outbound_packet_safe(frozen, candidate_payloads, manifest_value, ledger_value):
            raise PublicationError("review_packet_restricted_keep_pending")
        if use_fleet_mode:
            return _invoke_fleet_review(manifest, tag)
        if reviewer is None:
            from types import SimpleNamespace
            from caretaker.fleet_auditor import review, FleetReviewError
            digest = sha(manifest.read_bytes())
            review_calls += 1
            try:
                fleet_review_path, receipt = review(
                    manifest.parent, python=sys.executable,
                    bridge=Path.home()/'.agents/skills/agent-graph/scripts/todo_fleet.py',
                    suite=Path.home()/'.agents/runtime/agent-fleet-suite/lean-0.9.6')
                last_reviewed = SimpleNamespace(model=receipt['model'])
                if (_model_lineage(None, receipt['model'])
                        == _model_lineage(getattr(last_env, 'provider', None),
                                          getattr(last_env, 'model', None))):
                    raise PublicationError('no_independent_reviewer')
                return {'verdict':'PASS','manifest_sha256':digest,'findings':[]}, digest
            except FleetReviewError as exc:
                if exc.code == 'independent_review_requires_repair':
                    return {'verdict':'REPAIR','manifest_sha256':digest,'findings':exc.findings}, digest
                if exc.code == 'independent_review_transport_unavailable':
                    raise PublicationError('independent_review_deferred:transport_unavailable') from exc
                raise PublicationError(exc.code) from exc
        if cfg.fallback is None:
            raise PublicationError("independent_review_unavailable_or_oversize")
        digest = sha(manifest.read_bytes())
        candidate_dir = manifest.parent
        payloads = {
            x["path"]: (candidate_dir / x["payload_path"]).read_text(encoding="utf-8")
            for x in json.loads(manifest.read_text(encoding="utf-8"))["outputs"]
        }
        review_prompt = (
            semantic_review_prompt_prefix(digest)
            + json.dumps(
                {
                    "manifest": json.loads(manifest.read_text(encoding="utf-8")),
                    "frozen_evidence": frozen,
                    "candidate_outputs": payloads,
                },
                ensure_ascii=False,
            )
        )
        if len(review_prompt.encode()) > 160000:
            raise PublicationError("independent_review_unavailable_or_oversize")
        if not outbound_text_safe(review_prompt):
            raise PublicationError("review_packet_restricted_keep_pending")
        review_cfg = _write_run_config(cfg.fallback, str(run_root), str(run_root), rid + "-" + tag)
        try:
            last_reviewed = muse.invoke(
                spec=cfg.fallback, cwd=str(run_root), prompt=review_prompt,
                run_config_path=review_cfg, state_dir=str(run_root),
                timeout=_phase_timeout(reviewer_timeout),
            )
        except TransportError as exc:
            raise PublicationError("independent_review_deferred:" + exc.reason_code) from exc
        if (_model_lineage(last_reviewed.provider, last_reviewed.model)
                == _model_lineage(getattr(last_env, 'provider', None),
                                  getattr(last_env, 'model', None))):
            raise PublicationError('no_independent_reviewer')
        review_calls += 1
        try:
            parsed = response_object(last_reviewed.final or "null")
        except (json.JSONDecodeError, PublicationError) as exc:
            raise PublicationError("independent_review_not_accepted") from exc
        return parsed, digest

    def _invoke_fleet_review(manifest: Path, tag: str):
        """Walk the injected fleet of reviewer transports.

        The first transport that returns a real envelope whose provider+model
        differ from the maker's wins. Anything else (transport error, response
        that fails JSON parsing, identity collision with the maker) causes the
        next candidate to be tried. If every candidate is exhausted, the
        publication is deferred with ``independent_review_deferred``.

        The successful candidate's lineage is written into ``review.json`` so
        the canonical Muse-only ``bind_review`` is bypassed (the existing
        field ``fleet_review_path`` short-circuits the muse check at line
        332). Each candidate dict MAY carry an explicit ``lineage``; when
        absent the candidate's transport provider is recorded verbatim.
        """
        nonlocal review_calls, last_reviewed, fleet_review_path
        digest = sha(manifest.read_bytes())
        candidate_dir = manifest.parent
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
        payloads = {
            x["path"]: (candidate_dir / x["payload_path"]).read_text(encoding="utf-8")
            for x in manifest_value["outputs"]
        }
        review_prompt = (
            semantic_review_prompt_prefix(digest)
            + json.dumps(
                {
                    "manifest": manifest_value,
                    "frozen_evidence": frozen,
                    "candidate_outputs": payloads,
                },
                ensure_ascii=False,
            )
            + fleet_envelope_rule(digest)
        )
        if len(review_prompt.encode()) > 160000:
            raise PublicationError("independent_review_unavailable_or_oversize")
        if not outbound_text_safe(review_prompt):
            raise PublicationError("review_packet_restricted_keep_pending")
        maker_provider = getattr(last_env, "provider", None)
        maker_model = getattr(last_env, "model", None)
        last_candidate_error = None
        for idx, candidate in enumerate(fleet_reviewers or ()):
            tick_budget.check('curate_once.py:metadata_or_work')
            cand_transport = candidate["transport"]
            cand_spec = candidate["spec"]
            cand_lineage = (candidate.get("lineage")
                            or getattr(cand_transport, "name", None)
                            or "fleet")
            candidate_key = str(cand_lineage) + "/" + str(candidate.get("model") or "")
            if (use_append_maker and
                    _model_lineage(cand_lineage, candidate.get("model"))
                    == _model_lineage(maker_provider, maker_model)):
                last_candidate_error = "reviewer_same_lineage_as_maker"
                reviewer_attempts.append(_skipped_candidate(
                    candidate, last_candidate_error))
                continue
            # FORMAT REPAIR LOOP. A reviewer that judged correctly but
            # answered in the wrong shape gets the exact, deterministic reason
            # and its own previous answer back, and re-emits the verdict.
            # Skipping it outright (0.1.15) wasted five of seven reviewers
            # measured on 2026-09-23. Quota, gate refusals, timeouts and a
            # same-lineage identity are never "repaired": next candidate.
            accepted = False
            attempt_prompt = review_prompt
            prior_repairs = int(reviewer_format_repairs.get(candidate_key, 0))
            retry = resume_state.get("reviewer_retry")
            if prior_repairs:
                if (not isinstance(retry, dict)
                        or retry.get("candidate_key") != candidate_key
                        or retry.get("attempt") != prior_repairs):
                    reviewer_attempts.append(_skipped_candidate(
                        candidate, "reviewer_format_repair_proof_missing"))
                    continue
                attempt_prompt = _format_repair_prompt(
                    review_prompt, digest,
                    retry.get("problem") or "unusable answer",
                    retry.get("previous_output"), prior_repairs + 1,
                )
            for attempt in range(prior_repairs, 1 + FLEET_FORMAT_REPAIR_ATTEMPTS):
                tick_budget.check('curate_once.py:metadata_or_work')
                if not outbound_text_safe(attempt_prompt):
                    raise PublicationError("review_packet_restricted_keep_pending")
                review_cfg = _write_run_config(
                    cand_spec, str(run_root), str(run_root),
                    rid + "-" + tag + "-cand" + str(idx)
                    + ("-fix" + str(attempt) if attempt else ""),
                )
                problem = previous = None
                try:
                    env = cand_transport.invoke(
                        spec=cand_spec, cwd=str(run_root), prompt=attempt_prompt,
                        run_config_path=review_cfg, state_dir=str(run_root),
                        timeout=_phase_timeout(reviewer_timeout),
                    )
                except TransportError as exc:
                    last_candidate_error = (getattr(exc, "reason_code", None)
                                            or "transport_error")
                    if last_candidate_error not in _REPAIRABLE_TRANSPORT_CODES:
                        break
                    problem = _transport_format_problem(exc)
                    previous = getattr(exc, "previous_output", None)
                else:
                    # Reject self-review: the maker's real (provider, model)
                    # must not reappear as the reviewer's identity. A
                    # same-lineage hit consumes this candidate.
                    if (maker_provider and maker_model and
                            _model_lineage(env.provider, env.model)
                            == _model_lineage(maker_provider, maker_model)):
                        last_candidate_error = (
                            "reviewer_same_lineage_as_maker:" + env.provider + "/"
                            + env.model)
                        break
                    last_reviewed = env
                    review_calls += 1
                    previous = (env.final or "")[:PREVIOUS_OUTPUT_LIMIT] or None
                    try:
                        parsed = response_object(env.final or "null")
                    except (json.JSONDecodeError, PublicationError):
                        # Stable reason; never ``str(exc)`` (a position string).
                        last_candidate_error = _FLEET_DEFER_REASON_ENVELOPE_NOT_OBJECT
                        problem = ("the envelope's final text could not be read as a "
                                   "JSON object")
                    else:
                        verdict_status, verdict_obj = _extract_fleet_verdict(
                            parsed, digest)
                        if verdict_status == "missing":
                            last_candidate_error = _FLEET_DEFER_REASON_VERDICT_MISSING
                            problem = _missing_verdict_problem(parsed)
                        elif verdict_status == "manifest_mismatch":
                            # NOT a format defect (independent review of 0.1.16):
                            # handing the reviewer the right digest would let it
                            # copy the digest onto a judgement made about some
                            # other manifest. The binding stays as in 0.1.15 -
                            # next candidate, no repair. Stable reason only.
                            last_candidate_error = (
                                _FLEET_DEFER_REASON_VERDICT_MANIFEST_MISMATCH)
                            break
                        elif verdict_obj.get("verdict") not in {"PASS", "REPAIR"}:
                            last_candidate_error = _FLEET_DEFER_REASON_VERDICT_INVALID
                            problem = 'the "verdict" value must be exactly "PASS" or "REPAIR"'
                        else:
                            accepted = True
                            break
                if attempt == FLEET_FORMAT_REPAIR_ATTEMPTS:
                    break
                reviewer_format_repairs[candidate_key] = attempt + 1
                checkpoint(
                    resume_phase or "PREPARED", manifest.parent, manifest,
                    reviewer_format_repairs=dict(reviewer_format_repairs),
                    reviewer_retry={"candidate_key": candidate_key, "attempt": attempt + 1,
                                    "problem": problem or "unusable answer",
                                    "previous_output": previous},
                )
                _LOG.info("fleet reviewer %s format repair %s: %s", idx, attempt + 1,
                          last_candidate_error)
                attempt_prompt = _format_repair_prompt(
                    review_prompt, digest, problem or "unusable answer",
                    previous, attempt + 1)
                if (cand_lineage in ARGV_TASK_AGENTS
                        and len(attempt_prompt) > REPAIR_PROMPT_MAX_CHARS):
                    # The adapter passes the task on the Windows command line; a
                    # longer one fails before the model starts (WinError 206).
                    last_candidate_error = "format_repair_prompt_exceeds_command_line"
                    break
            if not accepted:
                reviewer_attempts.append(_skipped_candidate(
                    candidate, (last_candidate_error or "transport_error").split(":", 1)[0]))
                continue
            # ``verdict_obj`` is the canonical ``{"verdict": "PASS"|"REPAIR",
            # "manifest_sha256": str, "findings": list}``; the downstream
            # flow is unchanged from 0.1.15.
            if review_allows_publication(verdict_obj):
                # Write fleet-shaped review.json + semantic-review.json so
                # the muse-only ``bind_review`` is bypassed via
                # the existing ``fleet_review_path`` short-circuit.
                # Advisory-only charges still bind: they do not block.
                evidence = {
                    "transport": "fleet-cli/candidate",
                    "provider": env.provider,
                    "model": env.model,
                    "lineage": cand_lineage,
                    "verdict": "PASS",
                    "manifest_sha256": digest,
                }
                evidence_path = candidate_dir / "semantic-review.json"
                evidence_path.write_bytes(json_bytes(evidence))
                review_record = {
                    "schema_version": 1,
                    "verdict": "PASS",
                    "maker": (getattr(last_env, "provider", None)
                              if maker_route == "fleet_append"
                              else "openclaw-caretaker"),
                    "reviewer": {
                        "id": env.provider + "-"
                        + (env.session_id or "fleet"),
                        "lineage": cand_lineage,
                    },
                    "manifest_sha256": digest,
                    "evidence": {
                        "path": evidence_path.name,
                        "sha256": sha(evidence_path.read_bytes()),
                    },
                }
                rp = candidate_dir / "review.json"
                rp.write_bytes(json_bytes(review_record))
                fleet_review_path = rp
            return verdict_obj, digest
        # All candidates exhausted (or list empty): defer; never publish
        # without a verified independent review.
        if use_append_maker:
            raise PublicationError("no_independent_reviewer")
        reason = last_candidate_error or "no_fleet_candidate_available"
        raise PublicationError("independent_review_deferred:" + reason)

    manifest = candidate = None
    parsed = None
    digest = None
    repair_assessment = resume_state.get("assessment")
    reviewer_response = resume_state.get("review_response")
    if resume_phase in {"PREPARED", "REPAIR_REQUIRED", "REPAIR_PREPARED",
                        "REVIEW_RESULT", "REVIEW2_RESULT", "REVIEW_ACCEPTED"}:
        candidate = Path(resume_state["candidate_path"])
        manifest = Path(resume_state["manifest_path"])
        digest = resume_state["manifest_sha256"]
        if resume_phase in {"REVIEW_RESULT", "REVIEW2_RESULT", "REVIEW_ACCEPTED"}:
            if not isinstance(reviewer_response, dict):
                raise PublicationError("phase_resume_review_proof_missing")
            from types import SimpleNamespace
            last_reviewed = SimpleNamespace(
                provider=reviewer_response.get("provider"),
                model=reviewer_response.get("model"),
                final=reviewer_response.get("final"),
                session_id=reviewer_response.get("session_id"),
            )
            parsed = resume_state.get("assessment")
            if not isinstance(parsed, dict):
                raise PublicationError("phase_resume_review_proof_missing")
            digest = resume_state.get("manifest_sha256")
            fleet_review_path = (Path(resume_state["fleet_review_path"])
                                 if resume_state.get("fleet_review_path") else None)

    if resume_phase not in {"REPAIR_REQUIRED", "REPAIR_RESULT", "REPAIR_PREPARED", "REVIEW_ACCEPTED"}:
        if resume_phase is None:
            prompt = synthesis_prompt(frozen)
            manifest, candidate, format_exhausted = one_candidate(prompt, "candidate", True)
            if manifest is None:
                return pending(format_repair_exhausted=bool(format_exhausted), error_code=last_error)
            checkpoint("PREPARED", candidate, manifest,
                       observed=observed, assessment=None)
        if resume_phase == "REVIEW_RESULT":
            parsed = resume_state.get("assessment")
            digest = resume_state.get("manifest_sha256")
        else:
            try:
                parsed, digest = invoke_review(manifest, "review")
            except PublicationError as exc:
                if str(exc) == "no_independent_reviewer":
                    return pending(reason="no_independent_reviewer",
                                   error_code="no_independent_reviewer",
                                   candidate_path=str(candidate))
                if str(exc).startswith("independent_review_deferred:"):
                    if progress_callback is None:
                        raise
                    return {
                        **pending(reason=str(exc), error_code="independent_review_deferred",
                                  candidate_path=str(candidate)),
                        "status": "DEFERRED_PREPARED", "phase": "PREPARED",
                    }
                raise
            checkpoint_review_result("REVIEW_RESULT", parsed, digest, candidate, manifest)
        review_rounds.append(_review_round_record(len(review_rounds) + 1, parsed))
        if review_has_blocking_findings(parsed.get("findings") if isinstance(parsed, dict) else None):
            response = {
                "provider": getattr(last_reviewed, "provider", None),
                "model": getattr(last_reviewed, "model", None),
                "final": getattr(last_reviewed, "final", None),
                "session_id": (getattr(last_reviewed, "session_id", None)
                               or getattr(last_reviewed, "sessionId", None)),
            }
            checkpoint("REPAIR_REQUIRED", candidate, manifest,
                       observed=observed, assessment=parsed, review_response=response,
                       fleet_review_path=str(fleet_review_path) if fleet_review_path else None,
                       manifest_sha256=digest)
            repair_assessment = parsed
            resume_phase = "REPAIR_REQUIRED"
        else:
            if review_allows_publication(parsed if isinstance(parsed, dict) else {}):
                response = {
                    "provider": getattr(last_reviewed, "provider", None),
                    "model": getattr(last_reviewed, "model", None),
                    "final": getattr(last_reviewed, "final", None),
                    "session_id": (getattr(last_reviewed, "session_id", None)
                                   or getattr(last_reviewed, "sessionId", None)),
                }
                checkpoint("REVIEW_ACCEPTED", candidate, manifest,
                           observed=observed, assessment=parsed, review_response=response,
                           fleet_review_path=str(fleet_review_path) if fleet_review_path else None,
                           manifest_sha256=digest)
                resume_phase = "REVIEW_ACCEPTED"
            else:
                last_error = "independent_review_not_accepted"
                checkpoint("REVIEW_REJECTED", candidate, manifest,
                           observed=observed, assessment=parsed,
                           manifest_sha256=digest)
                return pending(error_code=last_error, candidate_path=str(candidate),
                               manifest_sha256=digest)

    if resume_phase in {"REPAIR_REQUIRED", "REPAIR_RESULT"}:
        if not isinstance(repair_assessment, dict):
            raise PublicationError("phase_resume_assessment_missing")
        detail = _sanitize_findings(repair_assessment.get("findings"))
        repaired_prompt = repair_prompt(frozen, "review_REPAIR", detail)
        timeout_override = None
        env_override = None
        if resume_phase == "REPAIR_RESULT":
            response = resume_state.get("maker_response")
            if not isinstance(response, dict):
                raise PublicationError("phase_resume_maker_proof_missing")
            from types import SimpleNamespace
            env_override = SimpleNamespace(
                ok=response.get("ok", True),
                status=response.get("status", "ok"),
                provider=response["provider"], model=response["model"],
                session_id=response.get("session_id"), final=response["final"],
            )
        else:
            # Check capacity before marking a model invocation as started.
            # A saved REPAIR_REQUIRED phase remains replayable if no budget is
            # available; a launched call without a durable response is not.
            timeout_override = _phase_timeout(maker_timeout)
            checkpoint("REPAIR_STARTED", None, None, observed=observed,
                       assessment=repair_assessment, repair_attempt=1)
        manifest, candidate, format_exhausted = one_candidate(
            repaired_prompt, "candidate-review-repair", True,
            env_override=env_override, timeout_override=timeout_override,
        )
        if manifest is None:
            checkpoint("REPAIR_FAILED", None, None, observed=observed,
                       assessment=repair_assessment,
                       format_repair_exhausted=bool(format_exhausted))
            return pending(
                review_repair_exhausted=True,
                format_repair_exhausted=bool(format_exhausted),
                error_code=last_error or "review_repair_failed",
                candidate_path=None,
            )
        checkpoint("REPAIR_PREPARED", candidate, manifest,
                   observed=observed, assessment=repair_assessment,
                   manifest_sha256=sha(manifest.read_bytes()), repair_attempt=1)
        resume_phase = "REPAIR_PREPARED"

    if resume_phase in {"REPAIR_PREPARED", "REVIEW2_RESULT"}:
        if resume_phase == "REVIEW2_RESULT":
            parsed = resume_state.get("assessment")
            digest = resume_state.get("manifest_sha256")
            response = resume_state.get("review_response")
            if not isinstance(response, dict):
                raise PublicationError("phase_resume_review_proof_missing")
            from types import SimpleNamespace
            last_reviewed = SimpleNamespace(
                provider=response.get("provider"), model=response.get("model"),
                final=response.get("final"), session_id=response.get("session_id"),
            )
            fleet_review_path = (Path(resume_state["fleet_review_path"])
                                 if resume_state.get("fleet_review_path") else None)
        else:
            try:
                parsed, digest = invoke_review(manifest, "review-2")
            except PublicationError as exc:
                if str(exc) == "no_independent_reviewer":
                    return pending(reason="no_independent_reviewer",
                                   error_code="no_independent_reviewer",
                                   candidate_path=str(candidate))
                if str(exc).startswith("independent_review_deferred:"):
                    if progress_callback is None:
                        raise
                    return {
                        **pending(reason=str(exc), error_code="independent_review_deferred",
                                  candidate_path=str(candidate)),
                        "status": "DEFERRED_PREPARED", "phase": "REPAIR_PREPARED",
                    }
                raise
            checkpoint_review_result("REVIEW2_RESULT", parsed, digest, candidate, manifest)
        review_rounds.append(_review_round_record(len(review_rounds) + 1, parsed))

    if not review_allows_publication(parsed if isinstance(parsed, dict) else {}):
        last_error = "independent_review_not_accepted"
        checkpoint("REPAIR_REJECTED", candidate, manifest,
                   observed=observed, assessment=parsed,
                   manifest_sha256=digest)
        return pending(
            review_repair_exhausted=True,
            error_code=last_error,
            candidate_path=str(candidate) if candidate else None,
            manifest_sha256=digest,
        )

    if resume_phase != "REVIEW_ACCEPTED":
        response = {
            "provider": getattr(last_reviewed, "provider", None),
            "model": getattr(last_reviewed, "model", None),
            "final": getattr(last_reviewed, "final", None),
            "session_id": (getattr(last_reviewed, "session_id", None)
                           or getattr(last_reviewed, "sessionId", None)),
        }
        checkpoint("REVIEW_ACCEPTED", candidate, manifest,
                   observed=observed, assessment=parsed, review_response=response,
                   fleet_review_path=str(fleet_review_path) if fleet_review_path else None,
                   manifest_sha256=digest)

    envelope = _envelope_raw(last_reviewed)
    findings = parsed.get("findings") if isinstance(parsed, dict) else []
    advisory = _advisory_summary(findings)
    try:
        if fleet_review_path is not None:
            review_path = fleet_review_path
        else:
            review_path = bind_review(candidate, envelope)
        if sha((candidate / "publication.json").read_bytes()) != digest:
            raise PublicationError("reviewed_manifest_changed")
    except tick_budget.TickBudgetExceeded:
        raise
    except Exception as exc:
        code, exception_class = _post_review_error(exc)
        last_error = "post_review_step_failed:" + code
        return pending(
            error_code=last_error,
            post_review_error_code=code,
            post_review_exception=exception_class,
            candidate_path=str(candidate) if candidate else None,
            manifest_sha256=digest,
        )

    accepted = {
        "status": "REVIEWED_CANDIDATE",
        "manifest": str(manifest),
        "manifest_sha256": digest,
        "review": str(review_path),
        "primary_model": getattr(last_env, "model", None),
        "maker_agent": getattr(last_env, "provider", None),
        "maker_lineage": _model_lineage(getattr(last_env, "provider", None),
                                         getattr(last_env, "model", None)),
        "maker_route": maker_route,
        "reviewer_model": getattr(last_reviewed, "model", None),
        "maker_calls": maker_calls,
        "review_calls": review_calls,
        "run_path": str(run_root),
        "candidate_path": str(candidate),
        "result_path": str(run_root / "result.json"),
        "proposal_ids": [p["id"] for p in frozen.get("proposals", [])],
        "snapshot_sha256": frozen.get("snapshot_sha256"),
        "target": frozen.get("target"),
        "review_rounds": list(review_rounds),
        "maker_attempts": list(maker_attempts),
        "reviewer_attempts": list(reviewer_attempts),
        "advisory_count": advisory["count"],
        "advisory_classes": advisory["classes"],
    }
    (run_root / "diagnostics.json").write_bytes(json_bytes(safe_diagnostics(accepted)))
    return accepted


def _default_fleet_transport(agent, model):
    """Production transport for a fleet reviewer candidate.

    ``agent`` and ``model`` are part of the factory contract so tests can
    inject a stub keyed by identity; the real Agent Fleet transport reads
    identity from the envelope, not from these arguments.
    """
    return AgentFleetArgvTransport()


def _build_fleet_reviewers(cfg, transport_factory=None):
    """Build ``fleet_reviewers`` from ``cfg.publication.reviewer.candidates``.

    Returns ``None`` for the legacy ``"muse"`` mode and for configurations
    without a reviewer section, so the existing publication behaviour is
    preserved bit-for-bit when the owner has not opted into the fleet path.

    Returns a list of ``{"transport", "spec", "lineage"}`` dicts in the
    same order as the configuration's ``candidates`` array when the mode is
    ``"fleet"`` and the list can be built. Each entry pairs a Fleet
    transport with a launch spec that names the candidate's agent and
    model; identity is later proven from the parsed Fleet envelope, never
    from the constructor or from the assistant text.

    ``transport_factory`` is an optional callable ``(agent, model) ->
    transport``. When omitted, each candidate uses
    ``AgentFleetArgvTransport``. Tests inject stubs through this factory
    so they reach the same production list-building path without launching
    Agent Fleet.

    Raises ``PublicationError`` when the mode is ``"fleet"`` but the
    candidate list cannot be built. The caller must defer with a clear
    reason in that case; a silent fallback to Muse would hide the broken
    fleet wiring from the operator.
    """
    pub = getattr(cfg, "publication", None)
    if pub is None:
        return None
    reviewer = getattr(pub, "reviewer", None)
    if reviewer is None:
        return None
    mode = getattr(reviewer, "mode", None)
    if mode != "fleet":
        return None
    candidates = list(getattr(reviewer, "candidates", []) or [])
    if not candidates:
        raise PublicationError(
            "fleet_reviewers_unavailable:no_candidates_in_config")
    return _build_fleet_candidates(
        candidates, transport_factory,
        data_classification=getattr(reviewer, "data_classification", None),
        fleet_role="reviewer")


def _build_fleet_candidates(candidates, transport_factory=None, *,
                            data_classification=None, fleet_role=None):
    out = []
    for candidate in candidates:
        agent = candidate.agent
        model = candidate.model
        # Production spec for Agent Fleet. The transport resolves
        # ``${fleet_py}`` against ``AGENT_FLEET_SUITE_ROOT`` (default
        # ``~/.agents/runtime/agent-fleet-suite/lean-0.9.7/agent-fleet/scripts/fleet.py``)
        # at invoke time and stages the prompt body via ``--task-file``; it
        # never relies on the placeholder ``agent-fleet.cmd`` binary that the
        # 2026-09-20 incident tried to launch. Only ``--task-file``,
        # ``--agent`` and ``--model`` are passed because the 2026-09-20 probe
        # confirmed they are the minimum needed for Fleet to return a
        # parseable JSON envelope; other flags documented in Fleet's argparse
        # were either only exercised in dry-run or never exercised at all
        # (see ``FLEET-INVOCATION.md``).
        spec = TransportSpec(
            kind="openclaw_argv",
            executable=sys.executable,
            argv=[
                "${fleet_py}",
                "run",
                "--task-file", "${task_file}",
                "--agent", agent,
                "--model", model,
            ],
            data_classification=data_classification,
            fleet_role=fleet_role,
        )
        factory = transport_factory or _default_fleet_transport
        transport = factory(agent, model)
        out.append({
            "transport": transport,
            "spec": spec,
            "lineage": agent,
            "model": model,
        })
    return out


def _build_fleet_append_makers(cfg, transport_factory=None):
    pub = getattr(cfg, "publication", None)
    append = getattr(pub, "append_maker", None)
    if append is None:
        return None
    return _build_fleet_candidates(
        append.candidates, transport_factory,
        data_classification=getattr(append, "data_classification", None),
        fleet_role="maker")


def _wire_fleet_publication(*, frozen, run_root, observed, cfg, rid,
                            maker, reviewer, transport_factory=None,
                            resume_state=None, progress_callback=None):
    """Build ``fleet_reviewers`` and dispatch to ``produce_reviewed_candidate``.

    When ``cfg.publication.reviewer.mode == "fleet"`` and the candidate list
    cannot be built, returns a PENDING result with a clear reason. NEVER
    silently falls back to Muse: a broken fleet configuration must surface
    to the operator rather than be hidden behind a successful review.

    ``transport_factory`` is forwarded to ``_build_fleet_reviewers``. Omit
    it in production so each candidate is an ``AgentFleetArgvTransport``.
    """
    try:
        fleet_reviewers = _build_fleet_reviewers(
            cfg, transport_factory=transport_factory)
    except PublicationError as exc:
        return {
            "status": "PENDING",
            "reason": str(exc),
            "error_code": "fleet_reviewers_unavailable",
            "run_path": str(run_root),
            "result_path": str(run_root / "result.json"),
            "primary_model": None,
            "reviewer_model": None,
            "maker_attempts": [],
            "reviewer_attempts": [],
            "proposal_ids": [p["id"] for p in frozen.get("proposals", [])],
            "snapshot_sha256": frozen.get("snapshot_sha256"),
            "target": frozen.get("target"),
        }
    return produce_reviewed_candidate(
        frozen=frozen, run_root=run_root, observed=observed, cfg=cfg, rid=rid,
        maker=maker, reviewer=reviewer,
        fleet_reviewers=fleet_reviewers,
        fleet_append_makers=_build_fleet_append_makers(
            cfg, transport_factory=transport_factory),
        resume_state=resume_state, progress_callback=progress_callback,
    )


def run(config_path: str, paths: list[str], publish: bool = False,
        maker=None, reviewer=None, maintenance_lock_held: bool = False) -> dict:
    cfg = load_config(config_path)
    if publish and not maintenance_lock_held:
        from maintenance import caretaker_maintenance_lock
        with caretaker_maintenance_lock(_maintenance_state_dir(cfg)) as acquired:
            if not acquired:
                raise PublicationError('caretaker_tick_running')
            return _run_with_config(cfg, paths, publish, maker, reviewer)
    return _run_with_config(cfg, paths, publish, maker, reviewer)


def _maintenance_state_dir(cfg) -> Path:
    explicit = (cfg.raw or {}).get('maintenance_state_dir')
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit.strip():
            raise PublicationError('invalid_maintenance_state_dir')
        return Path(explicit)
    state = Path(cfg.state_dir)
    # Older generated project configs lack maintenance_state_dir. Their
    # project state sits under the scheduled tick's shared state root.
    if state.name == 'state' and state.parent.parent.name == 'projects':
        root = state.parent.parent.parent
        return root.parent if root.name == 'new-event-epoch' else root
    raise PublicationError('maintenance_state_dir_required')


def _run_with_config(cfg, paths, publish, maker, reviewer):
    state = Path(cfg.state_dir)/'curation'
    state.mkdir(parents=True, exist_ok=True)
    gateway = CanonicalKnowledgeGateway(python_executable=cfg.hub.python_executable, script=cfg.hub.script, vault=cfg.hub.vault, timeout=120)
    if gateway.health().status != 'pass' or gateway.lint().status != 'pass':
        raise PublicationError('hub_preflight_failed')
    from caretaker.registry import main_page, read_registry
    registry, _ = read_registry(Path(cfg.hub.vault))
    target = main_page(cfg.project_slug, registry)
    try:
        frozen = freeze(Path(cfg.hub.vault), paths, target,
                        attestation_state_dir=Path(cfg.state_dir))
    except PublicationError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError) as exc:
        # A malformed or unreadable frozen input (for example a source
        # metadata file that is not valid JSON) must fail closed with a
        # stable reason instead of crashing the tick.
        raise PublicationError('frozen_input_unreadable') from exc
    retained = _load_reviewed_candidate(cfg, paths, state, frozen=frozen)
    if retained is not None:
        result, run_root = retained
        result.update(status='REVIEWED_CANDIDATE', model_calls=0, resumed_reviewed=True)
        if publish:
            _publish_retained(cfg, paths, run_root, result)
        return result
    phase = _load_phase_progress(cfg, paths, state, frozen)
    if phase is not None:
        run_root = Path(phase['run_root_path'])
        rid = run_root.name
        observed = phase['observed']
    else:
        rid = uuid.uuid4().hex
        run_root = state/rid
        run_root.mkdir()
        observed = dt.datetime.now(dt.timezone.utc).isoformat()

    def persist_phase(phase_name, candidate, manifest, progress):
        _save_phase_progress(
            cfg, paths, frozen, run_root, observed, phase_name,
            candidate, manifest, progress,
        )

    start_calls = (
        int(phase.get('maker_calls', 0)) if phase else 0,
        int(phase.get('review_calls', 0)) if phase else 0,
    )
    try:
        result = _wire_fleet_publication(
            frozen=frozen, run_root=run_root, observed=observed, cfg=cfg, rid=rid,
            maker=maker, reviewer=reviewer, resume_state=phase,
            progress_callback=persist_phase,
        )
    except tick_budget.TickBudgetExceeded:
        # A phase checkpoint is sufficient to report a safe pre-effect defer.
        # Without one, the interrupted maker has no completed candidate to resume.
        saved = _load_phase_progress(cfg, paths, state, frozen)
        if saved is None:
            raise
        result = {
            'status': 'DEFERRED_PREPARED',
            'reason': 'tick_time_budget_exhausted',
            'phase': saved['phase'],
            'run_path': saved['run_root_path'],
            'candidate_path': saved.get('candidate_path'),
            'manifest_sha256': saved.get('manifest_sha256'),
            'maker_calls': int(saved.get('maker_calls', 0)),
            'review_calls': int(saved.get('review_calls', 0)),
            'proposal_ids': [p['id'] for p in frozen.get('proposals', [])],
            'snapshot_sha256': frozen.get('snapshot_sha256'),
            'target': frozen.get('target'),
        }
    result['model_calls'] = max(
        0, int(result.get('maker_calls', 0)) - start_calls[0]
        + int(result.get('review_calls', 0)) - start_calls[1],
    )
    if result.get("status") != "REVIEWED_CANDIDATE":
        (run_root / "diagnostics.json").write_bytes(json_bytes(safe_diagnostics(result)))
        (run_root / "result.json").write_bytes(json_bytes(result))
        return result
    manifest = Path(result["manifest"])
    review_path = Path(result["review"])
    # Persist a reviewed immutable candidate before the first publication effect.
    # A contributor or curator contention must not spend another model call.
    _save_reviewed_candidate(cfg, paths, run_root, result)
    if publish:
        if Path(cfg.hub.vault).drive.casefold() != run_root.drive.casefold():
            raise PublicationError('curator_state_must_share_vault_volume_for_nondeleting_lock_release')
        _publish_retained(cfg, paths, run_root, result)
    (run_root/'result.json').write_bytes(json_bytes(result))
    return result


def _reviewed_pointer(state, project, paths):
    from caretaker.pubstate import batch_key
    return state / ('reviewed-' + batch_key(project, paths) + '.json')


def _phase_pointer(state, project, paths):
    from caretaker.pubstate import batch_key
    return state / ('phase-' + batch_key(project, paths) + '.json')


_RESUMABLE_PHASES = frozenset({
    "PREPARED", "REPAIR_REQUIRED", "REPAIR_PREPARED", "REVIEW_ACCEPTED",
    "REVIEW_RESULT", "REVIEW2_RESULT", "REPAIR_RESULT",
})
_TERMINAL_PHASES = frozenset({
    "REVIEW_STARTED", "REVIEW2_STARTED", "REPAIR_STARTED",
    "REVIEW_REJECTED", "REPAIR_REJECTED", "REPAIR_FAILED",
})
_PHASE_CACHE_LIMIT = 1024 * 1024
_PHASE_ARTIFACT_LIMIT = 2 * 1024 * 1024
_PHASE_TOTAL_PIN_LIMIT = 4 * 1024 * 1024
_LEGACY_PREPARED_CODE_VERSION = "416088b2471fa1a108e270bd91744595"


def _phase_binding(frozen):
    return {
        "project": frozen.get("project_slug"),
        "target": frozen.get("target"),
        "preimage_sha256": frozen.get("preimage_sha256"),
        "snapshot_sha256": frozen.get("snapshot_sha256"),
        "inputs": sorted([
            {key: item.get(key) for key in ("path", "sha256", "bytes")}
            for item in frozen.get("inputs", [])
        ], key=lambda item: (item["path"], item["sha256"], item["bytes"])),
        "proposals": sorted([
            {
                "id": item.get("id"), "path": item.get("path"),
                "sha256": item.get("sha256"),
                "source_ids": sorted(item.get("source_ids") or []),
            }
            for item in frozen.get("proposals", [])
        ], key=lambda item: (item["id"], item["path"], item["sha256"], item["source_ids"])),
    }


def _normalized_phase_binding(value):
    """Normalize only proposal/input order while rejecting malformed bindings."""
    expected_keys = {
        "project", "target", "preimage_sha256", "snapshot_sha256", "inputs", "proposals",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        return None
    inputs = value.get("inputs")
    proposals = value.get("proposals")
    if not isinstance(inputs, list) or not isinstance(proposals, list):
        return None
    normalized_inputs = []
    for item in inputs:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
            return None
        if (not isinstance(item.get("path"), str)
                or not isinstance(item.get("sha256"), str)
                or type(item.get("bytes")) is not int):
            return None
        normalized_inputs.append({
            "path": item["path"], "sha256": item["sha256"], "bytes": item["bytes"],
        })
    normalized_proposals = []
    for item in proposals:
        if (not isinstance(item, dict)
                or set(item) != {"id", "path", "sha256", "source_ids"}
                or not isinstance(item.get("id"), str)
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("sha256"), str)
                or not isinstance(item.get("source_ids"), list)
                or any(not isinstance(source_id, str) for source_id in item["source_ids"])):
            return None
        normalized_proposals.append({
            "id": item["id"], "path": item["path"], "sha256": item["sha256"],
            "source_ids": sorted(item["source_ids"]),
        })
    return {
        "project": value.get("project"), "target": value.get("target"),
        "preimage_sha256": value.get("preimage_sha256"),
        "snapshot_sha256": value.get("snapshot_sha256"),
        "inputs": sorted(normalized_inputs,
                         key=lambda item: (item["path"], item["sha256"], item["bytes"])),
        "proposals": sorted(normalized_proposals,
                            key=lambda item: (item["id"], item["path"], item["sha256"],
                                             item["source_ids"])),
    }


def _phase_bindings_equal(left, right):
    normalized_left = _normalized_phase_binding(left)
    normalized_right = _normalized_phase_binding(right)
    return normalized_left is not None and normalized_left == normalized_right


def _phase_record_code_compatible(record):
    """Accept current records and the exact, review-free deployed PREPARED pin."""
    from caretaker.pubstate import code_version

    if not isinstance(record, dict):
        return False
    saved_version = record.get("code_version")
    if saved_version == code_version():
        return True
    if saved_version not in {_LEGACY_PREPARED_CODE_VERSION, "6d74ee300e9032e35c27ced6fd2f274e"}:
        return False
    progress = record.get("progress")
    manifest_sha = record.get("manifest_sha256")
    return (
        record.get("phase") == "PREPARED"
        and isinstance(progress, dict)
        and progress.get("phase") == "PREPARED"
        and type(progress.get("maker_calls")) is int
        and progress.get("maker_calls") == 1
        and type(progress.get("review_calls")) is int
        and progress.get("review_calls") == 0
        and progress.get("review_rounds", []) == []
        and progress.get("reviewer_format_repairs", {}) == {}
        and progress.get("assessment") is None
        and not progress.get("review_response")
        and not progress.get("fleet_review_path")
        and isinstance(record.get("candidate"), str)
        and bool(record.get("candidate"))
        and isinstance(record.get("manifest"), str)
        and Path(record["manifest"]).name == "publication.json"
        and isinstance(record.get("pins"), dict)
        and bool(record.get("pins"))
        and isinstance(manifest_sha, str)
        and re.fullmatch(r"[a-f0-9]{64}", manifest_sha) is not None
        and progress.get("status") not in {
            "UNKNOWN", "UNKNOWN_REQUIRES_RECONCILIATION", "RUNNING", "PUBLISHED",
            "ACCOUNTED", "accounted_elsewhere", "unsafe_source",
        }
    )


def _phase_resume_frozen(record, frozen):
    """Bind a retained phase to the same deterministic forced continuation."""
    binding = record.get("binding") if isinstance(record, dict) else None
    if _phase_bindings_equal(binding, _phase_binding(frozen)):
        return frozen
    if (not _phase_record_code_compatible(record)
            or record.get("phase") not in {
                "PREPARED", "REPAIR_REQUIRED", "REPAIR_PREPARED", "REVIEW_RESULT",
                "REVIEW2_RESULT", "REVIEW_ACCEPTED",
            }):
        return None
    rotated = dict(frozen)
    try:
        from caretaker.publication import rotate_oversized_page
        rotate_oversized_page(rotated, force=True)
    except (OSError, ValueError, TypeError, KeyError, PublicationError):
        return None
    return rotated if _phase_bindings_equal(binding, _phase_binding(rotated)) else None


def _phase_regular_file(root: Path, name: str, *, limit=_PHASE_ARTIFACT_LIMIT):
    from caretaker.publication import safe_file
    path = safe_file(root, name)
    try:
        mode = path.stat(follow_symlinks=False).st_mode
        size = path.stat(follow_symlinks=False).st_size
    except OSError as exc:
        raise PublicationError("phase_resume_artifact_missing") from exc
    if not stat.S_ISREG(mode) or size > limit:
        raise PublicationError("phase_resume_artifact_invalid")
    return path


def _phase_digest(record):
    value = dict(record)
    value.pop("cache_sha256", None)
    return sha(json_bytes(value))


def _phase_pins(state: Path, candidate: Path):
    from caretaker.publication import safe_file
    pins = {}
    total = 0
    if not candidate.is_dir() or candidate.is_symlink() or candidate.is_junction():
        raise PublicationError("phase_resume_candidate_invalid")
    for path in candidate.rglob("*"):
        if path.is_dir():
            if path.is_symlink() or path.is_junction():
                raise PublicationError("phase_resume_reparse_path_refused")
            continue
        relative = path.relative_to(candidate).as_posix()
        item = _phase_regular_file(candidate, relative)
        total += item.stat().st_size
        if total > _PHASE_TOTAL_PIN_LIMIT:
            raise PublicationError("phase_resume_cache_exceeds_bound")
        pins[item.relative_to(state).as_posix()] = sha(item.read_bytes())
    return pins


def _save_phase_progress(cfg, paths, frozen, run_root, observed, phase,
                         candidate, manifest, progress):
    from caretaker.publication import safe_file
    from caretaker.pubstate import code_version
    if phase not in _RESUMABLE_PHASES | _TERMINAL_PHASES:
        raise PublicationError("phase_resume_phase_invalid")
    state = Path(cfg.state_dir) / "curation"
    state_resolved = state.resolve()
    run_root = Path(run_root)
    if not run_root.resolve().is_relative_to(state_resolved):
        raise PublicationError("phase_resume_run_path_invalid")
    candidate_name = manifest_name = manifest_digest = None
    pins = {}
    if manifest is not None:
        manifest = Path(manifest)
        candidate = Path(candidate)
        if (not candidate.resolve().is_relative_to(state_resolved)
                or manifest.parent.resolve() != candidate.resolve()
                or not manifest.resolve().is_relative_to(state_resolved)):
            raise PublicationError("phase_resume_candidate_path_invalid")
        candidate_name = candidate.relative_to(state).as_posix()
        manifest_name = manifest.relative_to(state).as_posix()
        manifest = _phase_regular_file(state, manifest_name)
        if manifest.name != "publication.json":
            raise PublicationError("phase_resume_manifest_invalid")
        manifest_digest = sha(manifest.read_bytes())
        pins = _phase_pins(state, candidate)
    clean_progress = dict(progress or {})
    if "review_response" in clean_progress:
        response = clean_progress["review_response"]
        if not isinstance(response, dict) or not isinstance(response.get("final"), (str, type(None))):
            raise PublicationError("phase_resume_review_proof_invalid")
        if len((response.get("final") or "").encode("utf-8")) > 256 * 1024:
            raise PublicationError("phase_resume_review_proof_exceeds_bound")
    record = {
        "schema_version": 1,
        "project": cfg.project_slug,
        "paths": sorted(paths),
        "phase": phase,
        "run_root": run_root.relative_to(state).as_posix(),
        "candidate": candidate_name,
        "manifest": manifest_name,
        "manifest_sha256": manifest_digest,
        "binding": _phase_binding(frozen),
        "code_version": code_version(),
        "observed": observed,
        "pins": pins,
        "progress": clean_progress,
    }
    record["cache_sha256"] = _phase_digest(record)
    encoded = json_bytes(record)
    if len(encoded) > _PHASE_CACHE_LIMIT:
        raise PublicationError("phase_resume_cache_exceeds_bound")
    _durable_json(_phase_pointer(state, cfg.project_slug, paths), record)


def _recover_deferred_prepared(record, cfg, paths, state):
    """Recover only retained pre-publication bytes; never recover an ACCEPT.

    An interrupted model phase has no publication authority. The normal
    loader below must still validate the full current frozen closure, target
    preimages, candidate pins, accounting and transaction journals, and the
    resumed candidate always needs a fresh independent review.
    """
    from caretaker.publication import safe_file
    if (record.get("phase") not in {"REVIEW_STARTED", "REVIEW2_STARTED", "REPAIR_STARTED"}
            or record.get("schema_version") != 1
            or record.get("code_version") != "6d74ee300e9032e35c27ced6fd2f274e"
            or record.get("project") != cfg.project_slug
            or record.get("paths") != sorted(paths)
            or record.get("cache_sha256") != _phase_digest(record)):
        return None
    try:
        run = safe_file(state, record.get("run_root"))
        if not run.is_dir() or run.is_symlink() or run.is_junction():
            return None
        result_file = _phase_regular_file(state, (run / "result.json").relative_to(state).as_posix())
        result = json.loads(result_file.read_text(encoding="utf-8"))
        binding = record.get("binding")
        if not isinstance(result, dict) or not isinstance(binding, dict):
            return None
        candidate = safe_file(state, (run / "candidate").relative_to(state).as_posix())
        manifest = _phase_regular_file(state, (candidate / "publication.json").relative_to(state).as_posix())
        if (not candidate.is_dir() or candidate.is_symlink() or candidate.is_junction()
                or result.get("status") != "DEFERRED_PREPARED"
                or result.get("reason") != "tick_time_budget_exhausted"
                or result.get("run_path") != str(run)
                or result.get("candidate_path") != str(candidate)
                or result.get("manifest_sha256") != sha(manifest.read_bytes())
                or result.get("snapshot_sha256") != binding.get("snapshot_sha256")
                or result.get("target") != binding.get("target")
                or sorted(result.get("proposal_ids") or []) != sorted(p["id"] for p in binding["proposals"])
                or result.get("publication") is not None
                or result.get("checkpoint") is not None):
            return None
        manifest_doc = json.loads(manifest.read_text(encoding="utf-8"))
        outputs = manifest_doc.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            return None
        for item in outputs:
            if not isinstance(item, dict):
                return None
            payload = _phase_regular_file(candidate, item.get("payload_path"))
            if sha(payload.read_bytes()) != item.get("sha256"):
                return None
        ledger = _phase_regular_file(candidate, manifest_doc.get("ledger_path"))
        if sha(ledger.read_bytes()) != manifest_doc.get("ledger_sha256"):
            return None
        prior_progress = record.get("progress")
        if (not isinstance(prior_progress, dict)
                or prior_progress.get("status") in {"UNKNOWN", "UNKNOWN_REQUIRES_RECONCILIATION", "RUNNING", "PUBLISHED", "ACCOUNTED", "accounted_elsewhere", "unsafe_source"}
                or type(prior_progress.get("maker_calls")) is not int
                or prior_progress["maker_calls"] < 1):
            return None
        # This is a new review cycle over the original maker bytes, not reuse
        # of the old independent review. Keep the prior call totals explicitly.
        progress = {k: prior_progress[k] for k in ("maker_provider", "maker_model", "maker_route", "observed") if k in prior_progress}
        progress.update(phase="PREPARED", maker_calls=1, review_calls=0,
                        maker_format_repairs=0, reviewer_format_repairs={},
                        review_rounds=[], prior_interrupted_calls={
                            "maker": prior_progress.get("maker_calls"),
                            "reviewer": prior_progress.get("review_calls")})
        recovered = dict(record)
        recovered.update(phase="PREPARED", candidate=candidate.relative_to(state).as_posix(),
                         manifest=manifest.relative_to(state).as_posix(),
                         manifest_sha256=sha(manifest.read_bytes()),
                         pins=_phase_pins(state, candidate), progress=progress)
        recovered["cache_sha256"] = _phase_digest(recovered)
        return recovered
    except (OSError, ValueError, TypeError, KeyError, PublicationError):
        return None


def _load_phase_progress(cfg, paths, state, frozen):
    from caretaker.publication import safe_file
    pointer_path = _phase_pointer(state, cfg.project_slug, paths)
    if not pointer_path.exists():
        return None
    pointer_path = _phase_regular_file(state, pointer_path.name, limit=_PHASE_CACHE_LIMIT)
    try:
        record = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise PublicationError("phase_resume_cache_malformed") from exc
    if not isinstance(record, dict):
        raise PublicationError("phase_resume_cache_malformed")
    if record.get("phase") in _TERMINAL_PHASES:
        recovered = _recover_deferred_prepared(record, cfg, paths, state)
        if recovered is None:
            raise PublicationError("phase_resume_terminal_no_replay")
        record = recovered
    raw_progress = record.get("progress")
    if (isinstance(raw_progress, dict) and raw_progress.get("status") in {
        "UNKNOWN", "UNKNOWN_REQUIRES_RECONCILIATION", "RUNNING", "PUBLISHED",
        "ACCOUNTED", "accounted_elsewhere", "unsafe_source",
    }):
        raise PublicationError("phase_resume_ambiguous_effect_no_replay")
    if (record.get("phase") not in _RESUMABLE_PHASES
            or record.get("schema_version") != 1
            or record.get("project") != cfg.project_slug
            or record.get("paths") != sorted(paths)
            or record.get("cache_sha256") != _phase_digest(record)
            or not _phase_record_code_compatible(record)):
        raise PublicationError("phase_resume_binding_invalid")
    phase_frozen = _phase_resume_frozen(record, frozen)
    if phase_frozen is None:
        raise PublicationError("phase_resume_binding_invalid")
    progress = record.get("progress")
    pins = record.get("pins")
    if not isinstance(progress, dict) or not isinstance(pins, dict) or len(pins) > 128:
        raise PublicationError("phase_resume_cache_malformed")
    if progress.get("status") in {
        "UNKNOWN", "UNKNOWN_REQUIRES_RECONCILIATION", "RUNNING", "PUBLISHED",
        "ACCOUNTED", "accounted_elsewhere", "unsafe_source",
    }:
        raise PublicationError("phase_resume_ambiguous_effect_no_replay")
    run_root = safe_file(state, record.get("run_root"))
    if not run_root.is_dir() or run_root.is_symlink() or run_root.is_junction():
        raise PublicationError("phase_resume_run_path_invalid")
    total = 0
    for name, digest in pins.items():
        path = _phase_regular_file(state, name)
        total += path.stat().st_size
        if total > _PHASE_TOTAL_PIN_LIMIT or sha(path.read_bytes()) != digest:
            raise PublicationError("phase_resume_artifact_changed")
    manifest_name = record.get("manifest")
    candidate_name = record.get("candidate")
    if record.get("phase") == "PREPARED" and manifest_name is None:
        raise PublicationError("phase_resume_manifest_missing")
    if manifest_name is not None:
        manifest = _phase_regular_file(state, manifest_name)
        candidate = safe_file(state, candidate_name)
        if (manifest.name != "publication.json" or manifest.parent != candidate
                or sha(manifest.read_bytes()) != record.get("manifest_sha256")):
            raise PublicationError("phase_resume_manifest_changed")
        if _phase_pins(state, candidate) != pins:
            raise PublicationError("phase_resume_artifact_changed")
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        outputs = doc.get("outputs")
        if not _manifest_matches_frozen(doc, phase_frozen):
            raise PublicationError("phase_resume_manifest_binding_invalid")
        vault = Path(cfg.hub.vault)
        if set(doc.get("proposal_ids") or []) & accounted_proposal_ids(vault):
            raise PublicationError("proposals_accounted_elsewhere")
        for item in outputs:
            current = safe_file(vault, item["path"])
            if current.exists() and not current.is_file():
                raise PublicationError("phase_resume_target_changed")
            actual = sha(current.read_bytes()) if current.is_file() else None
            if actual != item.get("preimage_sha256"):
                raise PublicationError("phase_resume_target_changed")
        txdir = manifest.parent / ".curation-transactions"
        if txdir.exists():
            for journal_path in txdir.glob("tx-*.json"):
                if journal_path.name.endswith(".manifest.json"):
                    continue
                journal = json.loads(safe_file(
                    manifest.parent, journal_path.relative_to(manifest.parent).as_posix()
                ).read_text(encoding="utf-8"))
                if journal.get("status") != "rolled_back":
                    raise PublicationError("phase_resume_journal_requires_reconciliation")
        record["candidate_path"] = str(candidate)
        record["manifest_path"] = str(manifest)
    elif record.get("phase") not in _RESUMABLE_PHASES:
        raise PublicationError("phase_resume_manifest_missing")
    record.update(progress)
    record["run_root_path"] = str(run_root)
    if not isinstance(record.get("observed"), str) or not record["observed"]:
        raise PublicationError("phase_resume_observed_missing")
    if record["phase"] in {"REPAIR_REQUIRED", "REPAIR_PREPARED"}:
        if (not isinstance(record.get("assessment"), dict)
                or record["assessment"].get("verdict") not in {"PASS", "REPAIR"}
                or not isinstance(record["assessment"].get("findings"), list)):
            raise PublicationError("phase_resume_assessment_missing")
    if record["phase"] == "REPAIR_RESULT":
        response = record.get("maker_response")
        if (not isinstance(record.get("assessment"), dict)
                or not isinstance(response, dict)
                or not isinstance(response.get("provider"), str)
                or not isinstance(response.get("model"), str)
                or not isinstance(response.get("final"), str)
                or record.get("repair_attempt") != 1):
            raise PublicationError("phase_resume_maker_proof_missing")
    if record["phase"] in {"REVIEW_RESULT", "REVIEW2_RESULT"}:
        assessment = record.get("assessment")
        response = record.get("review_response")
        if (not isinstance(assessment, dict)
                or assessment.get("verdict") not in {"PASS", "REPAIR"}
                or assessment.get("manifest_sha256") != record.get("manifest_sha256")
                or not isinstance(response, dict)):
            raise PublicationError("phase_resume_review_proof_missing")
        fleet_review_path = record.get("fleet_review_path")
        if fleet_review_path:
            review_path = safe_file(state, Path(fleet_review_path).relative_to(state).as_posix())
            if review_path.relative_to(state).as_posix() not in pins:
                raise PublicationError("phase_resume_review_proof_missing")
            review_doc = json.loads(_phase_regular_file(
                review_path.parent, review_path.name
            ).read_text(encoding="utf-8"))
            if (review_doc.get("verdict") != "PASS"
                    or review_doc.get("manifest_sha256") != record.get("manifest_sha256")
                    or assessment.get("verdict") != "PASS"
                    or not isinstance(review_doc.get("reviewer"), dict)
                    or _model_lineage(review_doc["reviewer"].get("lineage"), None)
                    == _model_lineage(record.get("maker_provider"), record.get("maker_model"))):
                raise PublicationError("phase_resume_review_proof_invalid")
            evidence_record = review_doc.get("evidence")
            if not isinstance(evidence_record, dict):
                raise PublicationError("phase_resume_review_proof_invalid")
            evidence = _phase_regular_file(review_path.parent, evidence_record.get("path"))
            if sha(evidence.read_bytes()) != evidence_record.get("sha256"):
                raise PublicationError("phase_resume_review_proof_invalid")
        else:
            if (not isinstance(response.get("provider"), str)
                    or not isinstance(response.get("model"), str)
                    or not isinstance(response.get("session_id"), str)
                    or not isinstance(response.get("final"), str)):
                raise PublicationError("phase_resume_review_proof_missing")
            try:
                response_value = response_object(response["final"])
            except (json.JSONDecodeError, PublicationError) as exc:
                raise PublicationError("phase_resume_review_proof_invalid") from exc
            if response_value != assessment:
                fleet_status, fleet_value = _extract_fleet_verdict(
                    response_value, record.get("manifest_sha256"))
                if fleet_status != "ok" or fleet_value != assessment:
                    raise PublicationError("phase_resume_review_proof_invalid")
            if (_model_lineage(response["provider"], response["model"])
                    == _model_lineage(record.get("maker_provider"), record.get("maker_model"))):
                raise PublicationError("phase_resume_review_identity_invalid")
    if record["phase"] == "REVIEW_ACCEPTED":
        if not isinstance(record.get("assessment"), dict) or not isinstance(record.get("review_response"), dict):
            raise PublicationError("phase_resume_review_proof_missing")
        response = record["review_response"]
        fleet_review_path = record.get("fleet_review_path")
        if fleet_review_path:
            review_path = safe_file(state, Path(fleet_review_path).relative_to(state).as_posix())
            if review_path.relative_to(state).as_posix() not in pins:
                raise PublicationError("phase_resume_review_proof_missing")
            review_doc = json.loads(_phase_regular_file(
                review_path.parent, review_path.name
            ).read_text(encoding="utf-8"))
            if (review_doc.get("verdict") != "PASS"
                    or review_doc.get("manifest_sha256") != record.get("manifest_sha256")
                    or record["assessment"].get("verdict") != "PASS"
                    or record["assessment"].get("manifest_sha256") != record.get("manifest_sha256")
                    or not isinstance(review_doc.get("reviewer"), dict)
                    or _model_lineage(review_doc["reviewer"].get("lineage"), None)
                    == _model_lineage(record.get("maker_provider"), record.get("maker_model"))):
                raise PublicationError("phase_resume_review_proof_invalid")
            evidence_record = review_doc.get("evidence")
            if not isinstance(evidence_record, dict):
                raise PublicationError("phase_resume_review_proof_invalid")
            evidence = _phase_regular_file(review_path.parent, evidence_record.get("path"))
            if sha(evidence.read_bytes()) != evidence_record.get("sha256"):
                raise PublicationError("phase_resume_review_proof_invalid")
        else:
            if (not isinstance(response.get("provider"), str)
                    or not isinstance(response.get("model"), str)
                    or not isinstance(response.get("session_id"), str)
                    or not isinstance(response.get("final"), str)):
                raise PublicationError("phase_resume_review_proof_missing")
            envelope = {
                "ok": True, "status": "ok", "provider": response["provider"],
                "model": response["model"], "sessionId": "retained-review-proof",
                "final": response["final"],
            }
            try:
                parsed = response_object(envelope["final"])
            except (json.JSONDecodeError, PublicationError) as exc:
                raise PublicationError("phase_resume_review_proof_invalid") from exc
            if (parsed != record.get("assessment")
                    or parsed.get("manifest_sha256") != record.get("manifest_sha256")
                    or _model_lineage(response["provider"], response["model"])
                    == _model_lineage(record.get("maker_provider"), record.get("maker_model"))):
                raise PublicationError("phase_resume_review_proof_invalid")
    if phase_frozen is not frozen:
        frozen.clear()
        frozen.update(phase_frozen)
    return record


def _manifest_matches_frozen(doc, frozen):
    expected = _phase_binding(frozen)
    actual_inputs = [
        {key: item.get(key) for key in ("path", "sha256", "bytes")}
        for item in doc.get("inputs", [])
    ]
    outputs = doc.get("outputs")
    return (
        doc.get("snapshot_sha256") == frozen.get("snapshot_sha256")
        and actual_inputs == expected["inputs"]
        and sorted(doc.get("proposal_ids") or [])
        == sorted(item["id"] for item in frozen.get("proposals", []))
        and isinstance(outputs, list) and bool(outputs)
        and outputs[0].get("path") == frozen.get("target")
        and outputs[0].get("preimage_sha256") == frozen.get("preimage_sha256")
    )


def _durable_json(path, value):
    tmp = path.with_name(path.name + '.pending-' + uuid.uuid4().hex)
    with open(tmp, 'xb') as handle:
        handle.write(json_bytes(value)); handle.flush(); os.fsync(handle.fileno())
    os.replace(tmp, path)


def _save_reviewed_candidate(cfg, paths, run_root, result):
    state = Path(cfg.state_dir) / 'curation'
    manifest = Path(result['manifest'])
    review = Path(result['review'])
    doc = json.loads(manifest.read_text(encoding='utf-8'))
    record = json.loads(review.read_text(encoding='utf-8'))
    if not isinstance(record.get('evidence'), dict):
        raise PublicationError('retained_candidate_review_proof_invalid')
    pins = _phase_pins(state, manifest.parent)
    pointer = {'schema_version':1, 'project':cfg.project_slug, 'paths':sorted(paths),
               'run_root':run_root.relative_to(state).as_posix(), 'pins':pins, 'result':result}
    _durable_json(run_root/'result.json', result)
    _durable_json(_reviewed_pointer(state, cfg.project_slug, paths), pointer)


def _load_reviewed_candidate(cfg, paths, state, *, frozen=None):
    from caretaker.publication import safe_file
    pointer_path = _reviewed_pointer(state, cfg.project_slug, paths)
    if not pointer_path.exists():
        return None
    pointer_path = _phase_regular_file(state, pointer_path.name, limit=_PHASE_CACHE_LIMIT)
    try:
        pointer = json.loads(pointer_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise PublicationError('retained_candidate_pointer_malformed') from exc
    if pointer.get('schema_version') != 1 or pointer.get('project') != cfg.project_slug or pointer.get('paths') != sorted(paths):
        raise PublicationError('retained_candidate_binding_invalid')
    if not isinstance(pointer.get('pins'), dict) or len(pointer['pins']) > 128:
        raise PublicationError('retained_candidate_binding_invalid')
    total = 0
    for name, digest in pointer['pins'].items():
        pinned = _phase_regular_file(state, name)
        total += pinned.stat().st_size
        if total > _PHASE_TOTAL_PIN_LIMIT or sha(pinned.read_bytes()) != digest:
            raise PublicationError('retained_candidate_artifact_changed')
    result = dict(pointer['result'])
    manifest = _phase_regular_file(state, Path(result['manifest']).relative_to(state).as_posix())
    if (result.get('status') != 'REVIEWED_CANDIDATE'
            or result.get('manifest_sha256') != sha(manifest.read_bytes())):
        raise PublicationError('retained_candidate_review_proof_invalid')
    doc = json.loads(manifest.read_text(encoding='utf-8'))
    if frozen is not None and not _manifest_matches_frozen(doc, frozen):
        raise PublicationError('retained_candidate_stale_requires_fresh_review')
    review = _phase_regular_file(
        state, Path(result['review']).relative_to(state).as_posix())
    try:
        review_doc = json.loads(review.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise PublicationError('retained_candidate_review_proof_invalid') from exc
    if (review_doc.get('verdict') != 'PASS'
            or review_doc.get('manifest_sha256') != result.get('manifest_sha256')
            or not isinstance(review_doc.get('reviewer'), dict)):
        raise PublicationError('retained_candidate_review_proof_invalid')
    reviewer_lineage = review_doc['reviewer'].get('lineage')
    maker_lineage = result.get('maker_lineage')
    if (not isinstance(reviewer_lineage, str) or not reviewer_lineage
            or reviewer_lineage == maker_lineage):
        raise PublicationError('retained_candidate_review_identity_invalid')
    evidence_record = review_doc.get('evidence')
    if not isinstance(evidence_record, dict):
        raise PublicationError('retained_candidate_review_proof_invalid')
    evidence = _phase_regular_file(review.parent, evidence_record.get('path'))
    if sha(evidence.read_bytes()) != evidence_record.get('sha256'):
        raise PublicationError('retained_candidate_review_proof_invalid')
    if set(doc['proposal_ids']) & accounted_proposal_ids(Path(cfg.hub.vault)):
        raise PublicationError('proposals_accounted_elsewhere')
    # An interrupted journal is not a harmless lock conflict. Reconciliation
    # must use the existing canonical recovery path before any new write.
    txdir = manifest.parent / '.curation-transactions'
    if txdir.exists():
        for path in txdir.glob('tx-*.json'):
            if path.name.endswith('.manifest.json'):
                continue
            journal = json.loads(safe_file(manifest.parent, path.relative_to(manifest.parent).as_posix()).read_text())
            if journal.get('status') != 'rolled_back':
                raise PublicationError('retained_candidate_journal_requires_reconciliation')
    if _phase_pins(state, manifest.parent) != pointer['pins']:
        raise PublicationError('retained_candidate_artifact_changed')
    vault = Path(cfg.hub.vault)
    for item in doc['inputs']:
        if sha(safe_file(vault, item['path']).read_bytes()) != item['sha256']:
            raise PublicationError('retained_candidate_stale_requires_fresh_review')
    for item in doc['outputs']:
        path = safe_file(vault, item['path'])
        actual = sha(path.read_bytes()) if path.exists() else None
        if actual != item['preimage_sha256']:
            raise PublicationError('retained_candidate_stale_requires_fresh_review')
    return result, safe_file(state, pointer['run_root'])


def _publish_retained(cfg, paths, run_root, result):
    from caretaker.pubstate import classify_failure
    try:
        _publish_under_lock(cfg, Path(result['manifest']), Path(result['review']), run_root, result)
    except tick_budget.TickBudgetExceeded:
        raise
    except Exception as exc:
        if not isinstance(exc, PublicationError):
            raise
        reason = str(exc)
        if classify_failure(reason) != 'transient':
            raise
        result.update(status='REVIEWED_CANDIDATE', reason=reason, retained_for_retry=True)
    _durable_json(run_root/'result.json', result)


def _publish_under_lock(cfg, manifest, review_path, run_root, result):
    """Canonical publication plus its local Git checkpoint, under one curator lock hold."""
    tick_budget.check('canonical_publication', minimum=60)
    try:
        with curator_lock(cfg.hub.script, cfg.hub.vault, run_root) as lock_id:
            if set(result.get('proposal_ids') or []) & accounted_proposal_ids(Path(cfg.hub.vault)):
                raise PublicationError('proposals_accounted_elsewhere')
            engine = str(Path(cfg.hub.script).with_name('curation.py'))
            args = [cfg.hub.python_executable,'-B','-X','utf8',engine,'publish','--manifest',str(manifest),'--review',str(review_path),'--lock-id',lock_id,'--agent','openclaw-caretaker']
            # Canonical validation snapshots the whole existing vault and checks
            # Windows reparse boundaries per file; thousands of sources can
            # exceed a model-call timeout before any publication occurs.
            result['publication'] = command(args, timeout=1800)
            if result['publication'].get('status') != 'PUBLISHED':
                raise PublicationError('canonical_publish_not_complete')
            result['status']='PUBLISHED'
            result['checkpoint'] = checkpoint_or_pending(result['publication'], Path(cfg.hub.vault), run_root)
            if result['checkpoint'].get('status') == 'PENDING':
                result['reason'] = 'checkpoint_pending'
            result['status']='PUBLISHED'
    except tick_budget.TickBudgetExceeded:
        raise
    except Exception as exc:
        if result.get('status') != 'PUBLISHED':
            raise
        # 0.1.17: the canonical publication is committed; only releasing the
        # curator lock failed. Recording that as a failed batch would quarantine
        # a published batch (independent review of 0.1.17).
        reason = str(exc) if isinstance(exc, PublicationError) else type(exc).__name__
        _LOG.warning('curator lock release failed after publication: %s', reason)
        result['lock_release'] = {'status': 'FAILED', 'reason': reason}
        result['reason'] = 'curator_lock_release_failed'
    return result


@contextmanager
def curator_lock(hub_script, vault, run_root):
    """Canonical owner lock; never remove/recover another process's lock."""
    ps = str(Path(hub_script).with_suffix('.ps1'))
    lock = command(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',ps,'-Action','acquire-lock','-Vault',str(vault),'-Agent','openclaw-caretaker'])
    lock_id = lock['lock_id']
    try:
        yield lock_id
    finally:
        # Preserve the task-owned lock as an audit artifact instead of deleting.
        source = Path(lock['lock_path'])
        value = json.loads(source.read_text(encoding='utf-8-sig'))
        if value.get('lock_id') != lock_id or value.get('agent') != 'openclaw-caretaker':
            raise PublicationError('curator_lock_changed_do_not_release')
        # Rename only, under checked task-controlled destinations; no recursive move.
        os.rename(source, Path(run_root)/('released-curator-'+lock_id+'.json'))


def checkpoint_or_pending(publication, vault, run_root):
    """Local Git checkpoint of a canonical publication, or an explicit PENDING one.

    The canonical publication is already committed when this runs; only the local
    Git checkpoint can still fail. On 2026-09-23 that failure was reported as a
    failed batch: the batch was quarantined and the published page stayed outside
    Git with nothing to retry it. Now it is PENDING, and the maintenance drain
    completes it before any new publication (maintenance.complete_pending_checkpoints).
    """
    try:
        return checkpoint_publication(publication, Path(vault), Path(run_root))
    except Exception as exc:   # any local failure: the publication itself is already canonical
        reason = str(exc) if isinstance(exc, PublicationError) else type(exc).__name__
        _LOG.warning('checkpoint pending for %s: %s', publication.get('event_id'), reason)
        return {'status': 'PENDING', 'reason': reason, 'receipt_dir': str(run_root)}


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('--proposal',action='append',required=True);p.add_argument('--publish',action='store_true');a=p.parse_args()
    try:
        result=run(a.config,a.proposal,a.publish)
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result.get('status') in {'PUBLISHED','REVIEWED_CANDIDATE'} else 1
    except tick_budget.TickBudgetExceeded:
        raise
    except Exception as exc:
        print(json.dumps({'status':'BLOCKED','error_type':type(exc).__name__,'reason':str(exc) if isinstance(exc,PublicationError) else 'review_or_transport_failed'}));return 1

if __name__=='__main__':raise SystemExit(main())
