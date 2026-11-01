"""RSI Helpdesk: ticket queue for defects of the AI stack itself.

Agents file a ticket in one command and continue their task. A deterministic
scanner turns repeated Fleet failures into tickets. The store under
~/.agents/helpdesk is the single source of ticket state; Office #rsi-helpdesk only
announces. Nothing here calls a model, deletes files or deploys.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

SCHEMA_VERSION = 1
STATUSES = ("open", "claimed", "candidate", "deployed", "closed", "needs-owner", "wontfix")
ACTIVE = {"open", "claimed", "candidate", "deployed", "needs-owner"}
TERMINAL = {"closed", "wontfix"}
# Queue priority: P1 (first) .. P3 (last). A ticket without a priority is last.
PRIORITIES = ("P1", "P2", "P3")
PRIORITY_RANK = {"P1": 0, "P2": 1, "P3": 2, None: 3}
# A claim spans the whole repair: claimed -> candidate -> deployed keep it.
CLAIM_HELD = {"claimed", "candidate", "deployed"}
DEFAULT_CLAIM_MINUTES = 240
OFFICE_CHANNEL = "rsi-helpdesk"
TASK_NAME = "AgentStack RSI Helpdesk"
# Not a stack defect: limits and provider outages resolve themselves.
TRANSIENT_PATTERNS = re.compile(
    r"quota|exhaust|rate.?limit|\b429\b|\b402\b|insufficient.?(balance|credits?)|out of credits|"
    r"payment required|subscription (limit|quota|expired)|billing (limit|hard limit)|"
    r"overloaded|\b503\b|temporarily unavailable",
    re.I,
)
# Secret-looking values never enter tickets, prompts or Office posts.
SECRET_PATTERNS = re.compile(
    r"\b(sk|pk|rk|ghp|gho|ghs|xox[abp]|glpat|AIza)[-_][A-Za-z0-9_\-]{12,}"
    r"|bearer\s+[A-Za-z0-9._\-]{12,}"
    r"|(password|passwd|pwd|token|secret|api[_-]?key|cookie)\s*[=:]\s*\S+"
    # Long mixed base64-like blobs; plain hex (hashes, fingerprints) stays readable.
    r"|\b(?![0-9a-f]{40,}\b)(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[A-Za-z])[A-Za-z0-9+/]{40,}={0,2}",
    re.I,
)
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_EVIDENCE_PER_FILING = 10
# Every counted run id is kept (far above a day of Fleet failures) so rescans stay idempotent.
MAX_RUN_IDS = 5000
# Lessons are appended via the --lesson option on update(): short, claim-tied notes
# meant for the next resolver in the same domain. Bounded so the projection stays
# under the byte cap without scanning the ticket file again.
LESSON_TEXT_LIMIT = 240
MAX_LESSONS_PER_TICKET = 12
_UNSET = object()
# Optional advisory context attached to a lesson via --lesson-context. Strict shape,
# not a free-form note: it tells the next resolver *what kind of fix this applies to*
# and *whether the previous attempt worked*, but it is never authority and never an
# executable command. Conditions live alongside the lesson (same version), survive
# reopen, and are surfaced unchanged through the bounded related-history projection.
LESSON_CONTEXT_KEYS = frozenset({"target", "outcome", "applicability"})
LESSON_CONTEXT_TARGETS = frozenset({"harness", "model", "artifact"})
LESSON_CONTEXT_OUTCOMES = frozenset({"effective", "ineffective", "unknown"})
LESSON_CONTEXT_APPLICABILITY_LIMIT = 240
LESSON_CONTEXT_TOTAL_BYTES = 512
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def clean(text: Any, limit: int) -> str:
    """Untrusted reporter/log text: no control chars, no secret-looking values, bounded."""
    text = CONTROL_CHARS.sub(" ", str(text or ""))
    return SECRET_PATTERNS.sub("<redacted>", text).strip()[:limit]


def validate_lesson_context(context: Any) -> dict[str, str]:
    """Strict validation for --lesson-context. Returns the sanitized object on success;
    raises SystemExit with a single actionable reason on any rejection. The contract:

      * Only the three keys ``target``, ``outcome``, ``applicability`` are allowed.
      * ``target`` must be one of harness|model|artifact.
      * ``outcome`` must be one of effective|ineffective|unknown.
      * ``applicability`` must be a short non-empty string.
      * No control chars or secret-looking patterns (we *reject*, not redact).
      * Serialized JSON must fit within LESSON_CONTEXT_TOTAL_BYTES so the projection
        stays under its 4096-byte cap without further truncation.
      * Numbers, booleans, lists, nested objects and missing keys are refused.
    """
    if not isinstance(context, dict):
        raise SystemExit("--lesson-context must be a JSON object with target/outcome/applicability")
    keys = set(context.keys())
    unknown = sorted(keys - LESSON_CONTEXT_KEYS)
    if unknown:
        raise SystemExit(f"--lesson-context unknown keys: {unknown}")
    missing = sorted(LESSON_CONTEXT_KEYS - keys)
    if missing:
        raise SystemExit(f"--lesson-context missing keys: {missing}")
    target = context.get("target")
    outcome = context.get("outcome")
    applicability = context.get("applicability")
    if not isinstance(target, str):
        raise SystemExit("--lesson-context.target must be a string")
    if target not in LESSON_CONTEXT_TARGETS:
        raise SystemExit(
            f"--lesson-context.target must be one of {sorted(LESSON_CONTEXT_TARGETS)} (got {target!r})"
        )
    if not isinstance(outcome, str):
        raise SystemExit("--lesson-context.outcome must be a string")
    if outcome not in LESSON_CONTEXT_OUTCOMES:
        raise SystemExit(
            f"--lesson-context.outcome must be one of {sorted(LESSON_CONTEXT_OUTCOMES)} (got {outcome!r})"
        )
    if not isinstance(applicability, str):
        raise SystemExit("--lesson-context.applicability must be a string")
    if not applicability.strip():
        raise SystemExit("--lesson-context.applicability cannot be empty")
    if len(applicability) > LESSON_CONTEXT_APPLICABILITY_LIMIT:
        raise SystemExit(
            f"--lesson-context.applicability exceeds {LESSON_CONTEXT_APPLICABILITY_LIMIT} chars"
        )
    if CONTROL_CHARS.search(applicability):
        raise SystemExit("--lesson-context.applicability contains control characters")
    if SECRET_PATTERNS.search(applicability):
        raise SystemExit("--lesson-context.applicability contains secret-looking content")
    sanitized_applicability = clean(applicability, LESSON_CONTEXT_APPLICABILITY_LIMIT)
    if not sanitized_applicability:
        raise SystemExit("--lesson-context.applicability cannot be empty after cleaning")
    sanitized: dict[str, str] = {
        "target": target,
        "outcome": outcome,
        "applicability": sanitized_applicability,
    }
    serialized = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
    if len(serialized.encode("utf-8")) > LESSON_CONTEXT_TOTAL_BYTES:
        raise SystemExit(
            f"--lesson-context exceeds {LESSON_CONTEXT_TOTAL_BYTES} bytes when serialized"
        )
    return sanitized


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_iso() -> str:
    """Stamp of a write (used for updated_at).

    Microsecond precision on purpose: the cycle race guard (update_if_unchanged)
    compares the snapshot taken at the start of a cycle with the current value, and
    two writes inside the same second must not look like "nothing changed".
    """
    return now().astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None  # naive times cannot be compared safely


def home() -> Path:
    return Path(os.environ.get("RSI_HELPDESK_HOME") or Path.home() / ".agents" / "helpdesk")


def tickets_dir(create: bool = False) -> Path:
    path = home() / "tickets"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_symptom(text: str) -> str:
    """Stable signature text: drop ids, hashes, numbers, paths and quotes."""
    text = text.lower()
    text = re.sub(r"run-\d{8}t\d{6}z-[0-9a-f]+", "<run>", text)
    text = re.sub(r"[a-z]:[\\/][^\s'\"]+|/[\w.\-/]+", "<path>", text)
    text = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", text)
    text = re.sub(r"\d+", "<n>", text)
    text = re.sub(r"[\"'`]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:240]


def signature(component: str, symptom: str, model: str = "") -> str:
    """Model ids keep their digits (qwen3.8-max != qwen3.9-max); the symptom is normalized."""
    subject = component.strip().lower() + (f"@{model.strip().lower()}" if model.strip() else "")
    raw = f"{subject}|{normalize_symptom(symptom)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def ticket_path(sig: str) -> Path:
    return tickets_dir() / f"{sig}.json"


class CorruptTicket(ValueError):
    """A ticket file exists but is not a valid ticket. Never overwritten automatically."""


def parse_ticket(path: Path) -> dict[str, Any]:
    try:
        ticket = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise CorruptTicket(f"{path.name}: {exc}") from exc
    if not _valid_ticket(ticket):
        raise CorruptTicket(f"{path.name}: not a valid ticket object")
    return ticket


def _valid_ticket(ticket: Any) -> bool:
    """Every field the queue, claims and scanner consume has its expected type."""
    required = ("signature", "id", "status", "component", "symptom", "evidence", "runs", "reporters",
                "history", "count", "first_seen", "last_seen", "claim")
    if not isinstance(ticket, dict) or any(key not in ticket for key in required):
        return False
    if not isinstance(ticket.get("signature"), str):
        return False
    if ticket.get("status") not in STATUSES:
        return False
    if not isinstance(ticket.get("count", 0), int) or isinstance(ticket.get("count", 0), bool):
        return False
    claim_info = ticket.get("claim")
    if claim_info is not None and not (isinstance(claim_info, dict) and isinstance(claim_info.get("by"), str)
                                       and isinstance(claim_info.get("until"), str)):
        return False
    for key in ("evidence", "runs", "reporters", "retired_claims"):
        value = ticket.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            return False
    history_items = ticket.get("history")
    if not isinstance(history_items, list) or not all(
            isinstance(h, dict) and all(isinstance(h.get(k, ""), str) for k in ("ts", "by", "status", "note"))
            for h in history_items):
        return False
    for key in ("first_seen", "last_seen", "id", "component", "symptom"):
        if not isinstance(ticket.get(key, ""), str):
            return False
    return ticket.get("resolved_at") is None or isinstance(ticket.get("resolved_at"), str)


def load(sig: str) -> dict[str, Any] | None:
    """None when absent; CorruptTicket when present but unreadable (fail closed for that signature)."""
    path = ticket_path(sig)
    if not path.exists():
        return None
    return parse_ticket(path)


def save(ticket: dict[str, Any]) -> None:
    tickets_dir(create=True)
    path = ticket_path(ticket["signature"])
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(ticket, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _after_save(ticket)


def _after_save(ticket: dict[str, Any]) -> None:
    """Maintain the repair-history domain index. Failures are isolated: the ticket
    file above is authoritative and the index is a derived cache. Lazy import keeps
    helpdesk importable when repair_history is not yet on sys.path."""
    try:
        import repair_history  # noqa: WPS433 (lazy import)
    except Exception:
        return
    try:
        repair_history.upsert_ticket(ticket)
    except Exception as exc:  # never let the index break filing or repair
        sys.stderr.write(f"warning: repair-history upsert failed: {type(exc).__name__}: {exc}\n")


@contextlib.contextmanager
def store_lock() -> Iterator[None]:
    """OS byte lock on a persistent file; released by the OS if we die."""
    path = home() / "store.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            import time
            for _ in range(200):
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                raise SystemExit("helpdesk store busy")
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            with contextlib.suppress(OSError):
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def all_tickets() -> list[dict[str, Any]]:
    out = []
    if not tickets_dir().is_dir():
        return out
    for path in sorted(tickets_dir().glob("*.json")):
        with contextlib.suppress(CorruptTicket):  # one bad file must not block the healthy queue
            out.append(parse_ticket(path))
    return out


def history(ticket: dict[str, Any], by: str, status: str, note: str) -> None:
    ticket["status"] = status
    ticket["updated_at"] = write_iso()
    ticket.setdefault("history", []).append(
        {"ts": ticket["updated_at"], "by": by, "status": status, "note": note[:2000]}
    )


def file_ticket(
    *, component: str, symptom: str, evidence: list[str], impact: str, workaround: str,
    reporter: str, source: str = "agent", runs: list[str] | None = None, model: str = "",
    run_stamps: dict[str, str] | None = None, min_runs: int = 0,
) -> tuple[dict[str, Any] | None, str]:
    """Create, merge or reopen. Returns (ticket, action): new|merged|reopened|noted|unchanged.

    Scanner filings pass run_stamps: only runs after the last resolution and not yet
    counted are new observations, decided under the store lock so a resolver closing
    the ticket concurrently cannot be undone by stale runs.
    """
    component, symptom, model = clean(component, 120), clean(symptom, 1000), clean(model, 80)
    if not component or not symptom:
        raise ValueError("component and symptom are required")
    reporter = clean(reporter, 120) or "unknown-agent"
    evidence = [clean(e, 300) for e in evidence[:MAX_EVIDENCE_PER_FILING] if clean(e, 300)]
    sig = signature(component, symptom, model)
    stamp = iso(now())
    with store_lock():
        ticket = load(sig)
        new_runs = list(runs or [])
        if run_stamps is not None:
            resolved = parse_iso((ticket or {}).get("resolved_at"))
            new_runs = [r for r, s in run_stamps.items() if not resolved or (parse_iso(s) or now()) > resolved]
            if len(new_runs) < min_runs:
                return ticket, "unchanged"
            new_runs = [r for r in new_runs if r not in (ticket or {}).get("runs", [])]
            if ticket is not None and not new_runs:
                return ticket, "unchanged"
        if ticket is None:
            ticket = {
                "schema_version": SCHEMA_VERSION,
                "id": f"HD-{now():%Y%m%d}-{sig[:6]}",
                "signature": sig,
                "component": component,
                "model": model or None,
                "symptom": symptom,
                "impact": clean(impact, 500),
                "workaround": clean(workaround, 500),
                "evidence": [],
                "runs": [],
                "reporters": [],
                "source": source,
                "count": 0,
                "priority": None,
                "first_seen": stamp,
                "last_seen": stamp,
                "claim": None,
            }
            history(ticket, reporter, "open", "filed")
            action = "new"
        elif ticket["status"] == "wontfix" and source == "fleet-scan":
            action = "noted"  # an owner/resolver verdict is not overturned by the scanner
        elif ticket["status"] in TERMINAL:
            ticket["claim"] = None
            ticket.pop("eligible_at", None)
            history(ticket, reporter, "open", f"reopened: seen again after {ticket['status']}")
            action = "reopened"
        else:
            action = "merged"
        ticket["count"] = int(ticket.get("count", 0)) + (len(new_runs) if run_stamps is not None else 1)
        ticket["last_seen"] = stamp
        for ref in evidence:
            if ref not in ticket["evidence"]:
                ticket["evidence"].append(ref)
        ticket["evidence"] = ticket["evidence"][-30:]
        for run_id in new_runs:
            if run_id not in ticket["runs"]:
                ticket["runs"].append(run_id)
        ticket["runs"] = ticket["runs"][-MAX_RUN_IDS:]
        if reporter not in ticket["reporters"]:
            ticket["reporters"].append(reporter)
            ticket["reporters"] = ticket["reporters"][-30:]
        if impact and not ticket.get("impact"):
            ticket["impact"] = clean(impact, 500)
        if workaround and not ticket.get("workaround"):
            ticket["workaround"] = clean(workaround, 500)
        save(ticket)
    return ticket, action


def _claim_until(ticket: dict[str, Any]) -> dt.datetime | None:
    return parse_iso((ticket.get("claim") or {}).get("until"))


def claim(sig: str, by: str, minutes: int = DEFAULT_CLAIM_MINUTES) -> dict[str, Any]:
    """Take an open ticket, take over a parked one, or extend your own claim."""
    with store_lock():
        ticket = load(sig)
        if ticket is None:
            raise SystemExit(f"no ticket {sig}")
        current = ticket.get("claim") or {}
        until = _claim_until(ticket)
        if by in ticket.get("retired_claims", []):
            raise SystemExit(f"{by} already ended its claim on {sig}; a new session must take it")
        live_claim = bool(until and until > now())
        own = current.get("by") == by and live_claim
        if ticket["status"] in CLAIM_HELD and not own and live_claim:
            raise SystemExit(f"ticket {sig} claimed by {current.get('by')} until {current.get('until')}")
        # A lapsed claim of the same holder is not renewable here: only the cycle may
        # expire it (expire_claims), which retires the holder and reopens the ticket.
        # Until then the ticket stays claimed and the stale session must not write.
        if ticket["status"] in CLAIM_HELD and current.get("by") == by and not live_claim:
            raise SystemExit(
                f"claim of {by} on {sig} expired without result; the cycle must expire it "
                "before the ticket can be claimed again")
        # A parked candidate/deployed ticket without a live claim is not a repair in
        # progress (the session ended or the claim was released): a resolver may take it
        # over, and the cycle then sees a claimed ticket and skips it.
        takeover = ticket["status"] in CLAIM_HELD and not live_claim
        if not (ticket["status"] == "open" or own or takeover):
            raise SystemExit(f"ticket {sig} is {ticket['status']}, not claimable by {by}")
        if not own:
            raw_eligible = ticket.get("eligible_at")
            if raw_eligible is not None and raw_eligible != "":
                if not isinstance(raw_eligible, str):
                    raise SystemExit(f"ticket {sig} has malformed eligible_at")
                parsed_eligible = parse_iso(raw_eligible)
                if parsed_eligible is None:
                    raise SystemExit(f"ticket {sig} has malformed eligible_at")
                if parsed_eligible > now():
                    raise SystemExit(f"ticket {sig} is deferred until {raw_eligible}")
        ticket["claim"] = {"by": by, "until": iso(now() + dt.timedelta(minutes=minutes))}
        ticket.pop("eligible_at", None)
        status = ticket["status"] if own and ticket["status"] in CLAIM_HELD else "claimed"
        history(ticket, by, status, f"claim {'extended' if own else 'taken'} for {minutes} min")
        save(ticket)
        return ticket


def _retire(ticket: dict[str, Any], holder: str | None) -> None:
    """A run id whose claim ended can never claim this ticket again."""
    ticket["claim"] = None
    if holder:
        retired = ticket.setdefault("retired_claims", [])
        if holder not in retired:
            retired.append(holder)  # never trimmed: one short id per finished session


def update(sig: str, by: str, status: str, note: str, force: bool = False,
           release_claim: bool = False, defer_until: str | dt.datetime | None = None,
           lesson: str | None = None,
           lesson_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Change status. Only the current, unexpired claim holder may do it; force is for
    the owner by hand. `release_claim` with status candidate ends the session's work:
    the finished candidate waits for the owner and is not resumed automatically.
    ``lesson`` is an optional short string appended (versioned, claim-tied) to the
    ticket's ``lessons`` array so future resolvers in the same domain can reuse it.
    Lessons are append-only; reopening preserves previous ones. ``lesson_context`` is
    an optional advisory object (``target``/``outcome``/``applicability``) attached to
    the lesson when both are supplied; it is never authority or an executable command,
    only evidence the next resolver re-verifies against the live stack."""
    if status not in STATUSES or status == "claimed":
        raise SystemExit(f"status must be one of {[s for s in STATUSES if s != 'claimed']} (use claim)")
    if release_claim and status != "candidate":
        raise SystemExit("--release only applies to --status candidate")
    if defer_until is not None and status != "open":
        raise SystemExit("--defer-until only applies to --status open")
    if lesson_context is not None and not lesson:
        raise SystemExit("--lesson-context requires --lesson (context is attached to a lesson)")
    validated_context: dict[str, str] | None = None
    if lesson_context is not None:
        validated_context = validate_lesson_context(lesson_context)
    defer_dt: dt.datetime | None = None
    if defer_until is not None:
        if isinstance(defer_until, dt.datetime):
            defer_dt = defer_until if defer_until.tzinfo is not None else None
        elif isinstance(defer_until, str):
            defer_dt = parse_iso(defer_until)
        if defer_dt is None or defer_dt <= now():
            raise SystemExit("--defer-until must be a valid future timezone-aware ISO8601 timestamp")
    with store_lock():
        ticket = load(sig)
        if ticket is None:
            raise SystemExit(f"no ticket {sig}")
        holder = (ticket.get("claim") or {}).get("by")
        until = _claim_until(ticket)
        if not force and (ticket["status"] not in CLAIM_HELD or holder != by or not until or until <= now()):
            raise SystemExit(
                f"ticket {sig} is {ticket['status']} held by {holder}; {by} has no claim "
                "(it expired or was taken over): record findings with `file`, do not change status")
        if status not in CLAIM_HELD or release_claim:
            _retire(ticket, holder)  # candidate/deployed otherwise keep the claim of the repair in progress
        ticket["awaiting_owner"] = status == "candidate" and release_claim
        if status in TERMINAL:
            ticket["resolved_at"] = iso(now())
        if defer_dt is not None:
            ticket["eligible_at"] = defer_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        elif status == "open" or status in TERMINAL:
            ticket.pop("eligible_at", None)
        defer_note = f" [deferred until {ticket['eligible_at']}]" if defer_dt is not None else ""
        history(ticket, by, status, clean(note, 2000) + defer_note + (" [force]" if force else "")
                + (" [handed to owner]" if ticket["awaiting_owner"] else ""))
        if lesson:
            lesson_text = clean(lesson, LESSON_TEXT_LIMIT)
            if lesson_text:
                existing_lessons = list(ticket.get("lessons") or [])
                last_version = 0
                for prior in existing_lessons:
                    try:
                        v = int(prior.get("version", 0) or 0)
                    except (TypeError, ValueError):
                        v = 0
                    if v > last_version:
                        last_version = v
                lesson_entry: dict[str, Any] = {
                    "version": last_version + 1,
                    "ts": ticket["updated_at"],
                    "by": clean(by, 80),
                    "status": status,
                    "text": lesson_text,
                }
                if validated_context is not None:
                    lesson_entry["context"] = validated_context
                existing_lessons.append(lesson_entry)
                ticket["lessons"] = existing_lessons
        save(ticket)
        return ticket


def priority_of(ticket: dict[str, Any]) -> str | None:
    """The queue priority of a ticket: P1, P2, P3 or None. A missing, malformed or
    unknown value reads as None (last in the queue); nothing is invented here."""
    value = ticket.get("priority")
    if isinstance(value, str) and value in PRIORITIES:
        return value
    return None


def set_priority(sig: str, priority: str | None, by: str, note: str) -> dict[str, Any]:
    """Set the queue priority of a ticket.

    No claim is needed and the status is never changed: only ``priority`` and the
    ticket history are written (the history entry keeps the current status). The
    ticket file is written atomically by save() under the store lock. An unknown
    ticket id is an error and leaves the store untouched.
    """
    label = priority if priority in PRIORITIES else None
    with store_lock():
        ticket = load(sig)
        if ticket is None:
            raise SystemExit(f"no ticket {sig}")
        if ticket.get("status") == "claimed" or ticket.get("claim"):
            raise SystemExit(f"ticket {sig} is claimed; priority was not changed")
        ticket["priority"] = label
        by_clean = clean(by, 120) or "unknown-agent"
        note_clean = clean(note, 2000)
        history(ticket, by_clean, ticket["status"], f"priority {label or 'none'}: {note_clean}")
        save(ticket)
        return ticket


def update_if_unchanged(
    sig: str,
    expected_updated_at: str | None,
    *,
    expected_status: str,
    by: str,
    note: str,
    status: str | None = None,
    priority: str | None | object = _UNSET,
) -> dict[str, Any] | None:
    """Atomically update an unclaimed ticket only if its cycle snapshot is current.

    Returns ``None`` when a resolver or another writer changed the ticket after the
    cycle took its snapshot. ``status`` and ``priority`` are independent optional
    mutations; omitting priority preserves its current value.
    """
    if status is not None and (status not in STATUSES or status == "claimed"):
        raise SystemExit(f"invalid status {status!r}")
    if priority is not _UNSET and priority is not None and priority not in PRIORITIES:
        raise SystemExit(f"invalid priority {priority!r}")
    with store_lock():
        ticket = load(sig)
        if ticket is None:
            return None
        if (ticket.get("updated_at") != expected_updated_at
                or ticket.get("status") != expected_status
                or ticket.get("claim")):
            return None
        if status is not None:
            ticket["status"] = status
            ticket["awaiting_owner"] = False
            if status in TERMINAL:
                ticket["resolved_at"] = iso(now())
            elif status == "open":
                ticket.pop("resolved_at", None)
                ticket.pop("eligible_at", None)
        if priority is not _UNSET:
            ticket["priority"] = priority
        history(ticket, clean(by, 120) or "unknown-agent", ticket["status"], clean(note, 2000))
        save(ticket)
        return ticket


def expire_claims(by: str = "rsi-helpdesk-cycle", protect: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Release dead repairs. Interrupted candidate/deployed work returns to open with
    recover_from so the next resolver verifies the real state. A candidate handed to
    the owner has no claim and is left alone. Claims of run ids in `protect` (a
    resolver process that may still be alive) are never released here."""
    released = []
    with store_lock():
        for ticket in all_tickets():
            claim_info = ticket.get("claim") or {}
            holder = claim_info.get("by")
            until = _claim_until(ticket)
            status = ticket.get("status")
            if status not in CLAIM_HELD or not claim_info or holder in protect:
                continue
            if until is not None and until > now():
                continue
            _retire(ticket, holder)
            if status != "claimed":
                ticket["recover_from"] = {"status": status, "by": holder, "at": iso(now())}
            note = "claim expired without result" if status == "claimed" else (
                f"claim of {holder} expired in status {status}: verify what exists before new work")
            history(ticket, by, "open", note)
            save(ticket)
            released.append(ticket["signature"])
    return released


def release(sig: str, by: str, note: str) -> None:
    """Return a ticket to open if `by` still holds it (launch failure, confirmed stop).
    A corrupt ticket is left untouched; supervision of other tickets continues."""
    with store_lock():
        try:
            ticket = load(sig)
        except CorruptTicket:
            return
        if ticket and ticket.get("status") in CLAIM_HELD and (ticket.get("claim") or {}).get("by") == by:
            if ticket["status"] != "claimed":
                ticket["recover_from"] = {"status": ticket["status"], "by": by, "at": iso(now())}
            _retire(ticket, by)
            history(ticket, "rsi-helpdesk-cycle", "open", clean(note, 2000))
            save(ticket)


def owner_resumed(ticket: dict[str, Any]) -> bool:
    """Whether a manual owner reopen is still waiting for the resolver's next attempt.

    Priority annotations and automated triage/director events do not count as an
    owner action and cannot clear or create this flag.
    """
    hist = ticket.get("history") or []
    for index in range(len(hist) - 1, -1, -1):
        event = hist[index]
        if not isinstance(event, dict):
            continue
        by = str(event.get("by", ""))
        note = str(event.get("note", "")).strip().lower()
        if (by.startswith(("rsi-triage:", "rsi-director:"))
                or note.startswith("priority ")):
            continue
        return bool(
            index > 0
            and event.get("status") == "open"
            and note.endswith("[force]")
            and isinstance(hist[index - 1], dict)
            and hist[index - 1].get("status") in {"candidate", "needs-owner"}
        )
    return False


def is_ticket_eligible(ticket: dict[str, Any]) -> bool:
    raw = ticket.get("eligible_at")
    if raw is None or raw == "":
        return True
    if not isinstance(raw, str):
        sys.stderr.write(f"warning: ticket {ticket.get('signature', '?')}: malformed eligible_at\n")
        return False
    parsed = parse_iso(raw)
    if parsed is None:
        sys.stderr.write(f"warning: ticket {ticket.get('signature', '?')}: malformed eligible_at\n")
        return False
    return parsed <= now()


def _safe_ts(raw):
    """Parse an ISO timestamp or return None; malformed values never crash the queue."""
    try:
        return parse_iso(raw) if isinstance(raw, str) else None
    except Exception:
        return None


def _service_ts(ticket):
    """Oldest-service key: the last valid 'claim taken' history timestamp (claim
    extensions never refresh it), else first_seen for never-attempted tickets.
    Unknown or malformed history/first_seen yields None (sorted last); nothing
    here invents timestamps or claims."""
    last = None
    history_items = ticket.get("history")
    if isinstance(history_items, list):
        for entry in history_items:
            if not isinstance(entry, dict):
                continue
            if entry.get("status") == "claimed" and "claim taken" in str(entry.get("note", "")):
                parsed = _safe_ts(entry.get("ts"))
                if parsed is not None:
                    last = parsed
    if last is not None:
        return last
    return _safe_ts(ticket.get("first_seen"))


def next_open(skip: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any] | None:
    """Eligible open tickets: owner-resumed candidates first, then priority (P1, P2, P3,
    no priority last), then oldest service timestamp (last valid claim TAKEN, else
    first_seen), then frequent, old, signature. ``skip`` drops tickets (e.g. a component
    already being repaired) before the head is chosen; the order of the rest is unchanged."""
    candidates = [t for t in all_tickets() if t.get("status") == "open" and is_ticket_eligible(t)]
    if skip is not None:
        candidates = [t for t in candidates if not skip(t)]
    def _freq(t):
        try:
            return -int(t.get("count", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _queue_key(t):
        service = _service_ts(t)
        return (not owner_resumed(t), PRIORITY_RANK[priority_of(t)], service is None, service, _freq(t),
                str(t.get("first_seen", "")), str(t.get("signature", "")))

    candidates.sort(key=_queue_key)
    return candidates[0] if candidates else None


# ---------------------------------------------------------------- weekly stats

# A closure is the last closing event of a ticket: closed, wontfix or deployed.
STATS_CLOSING = ("closed", "wontfix", "deployed")
STATS_PRIORITY_KEYS = ("P1", "P2", "P3", "none")
# Resolver run directories are named rsi-<YYYYMMDDTHHMMSSZ>-<sig6>-<resolver>.
RUN_DIR_RE = re.compile(r"^rsi-(\d{8}T\d{6}Z)-([0-9a-fA-F]{6})-(.+)$")


def _parse_run_stamp(name: str) -> dt.datetime | None:
    """The start time encoded in a resolver run directory name, or None."""
    match = RUN_DIR_RE.match(name)
    if not match:
        return None
    try:
        return dt.datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _median(values: list[float]) -> float | None:
    """Median of a sample; None when the sample is empty (no data, never a fake 0)."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


def _collect_stats(days: int) -> tuple[dict[str, Any], dt.datetime, dt.datetime]:
    """Queue metrics of the window [now - days, now] (UTC).

    Read-only: the ticket store is only read, never written. Counters are null when
    the store holds no ticket at all (no data is not a zero), and a metric without
    samples (medians, claude share) is null instead of a made-up number.
    """
    if days < 1:
        raise SystemExit("--days must be a positive number of days")
    end = now()
    start = end - dt.timedelta(days=days)
    tickets = all_tickets()
    have_data = bool(tickets)

    inflow = 0
    closures = {"closed": 0, "wontfix": 0, "deployed": 0}
    reopened = 0
    first_claim_hours: list[float] = []
    close_hours: list[float] = []
    components: dict[str, int] = {}
    open_count = 0
    open_by_priority = {key: 0 for key in STATS_PRIORITY_KEYS}
    recurring: list[dict[str, Any]] = []

    for ticket in tickets:
        seen = _safe_ts(ticket.get("first_seen"))
        if seen is not None and start <= seen <= end:
            inflow += 1
            component = str(ticket.get("component") or "")
            components[component] = components.get(component, 0) + 1
        events = ticket.get("history")
        if not isinstance(events, list):
            events = []
        closing_status: str | None = None
        closing_ts: dt.datetime | None = None
        first_claim: dt.datetime | None = None
        was_reopened = False
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                continue
            status = event.get("status")
            stamp = _safe_ts(event.get("ts"))
            if status == "claimed" and first_claim is None:
                first_claim = stamp
            if status in STATS_CLOSING:
                closing_status, closing_ts = status, stamp
            if (status == "open" and index > 0 and isinstance(events[index - 1], dict)
                    and events[index - 1].get("status") in STATS_CLOSING
                    and stamp is not None and start <= stamp <= end):
                was_reopened = True
        if was_reopened:
            reopened += 1
        if closing_status is not None and closing_ts is not None and start <= closing_ts <= end:
            closures[closing_status] = closures.get(closing_status, 0) + 1
            if seen is not None:
                close_hours.append((closing_ts - seen).total_seconds() / 3600.0)
        if first_claim is not None and start <= first_claim <= end and seen is not None:
            first_claim_hours.append((first_claim - seen).total_seconds() / 3600.0)
        if ticket.get("status") == "open":
            open_count += 1
            open_by_priority[priority_of(ticket) or "none"] += 1
            try:
                count = int(ticket.get("count", 0) or 0)
            except (TypeError, ValueError):
                count = 0
            if count >= 3 or was_reopened:
                recurring.append({
                    "id": str(ticket.get("id") or ""),
                    "component": str(ticket.get("component") or ""),
                    "count": count,
                    "symptom": str(ticket.get("symptom") or "")[:100],
                })

    resolver_counts: dict[str, int] = {}
    total_runs = 0
    claude_runs = 0
    runs_dir = home() / "runs"
    if runs_dir.is_dir():
        for path in sorted(runs_dir.iterdir()):
            if not path.is_dir():
                continue
            match = RUN_DIR_RE.match(path.name)
            if not match:
                continue
            stamp = _parse_run_stamp(path.name)
            if stamp is None or not (start <= stamp <= end):
                continue
            resolver = match.group(3)
            resolver_counts[resolver] = resolver_counts.get(resolver, 0) + 1
            total_runs += 1
            if resolver.lower().startswith("claude"):
                claude_runs += 1

    report: dict[str, Any] = {
        "window_days": days,
        "generated_at": iso(end),
        "inflow": inflow if have_data else None,
        "closures": closures if have_data else {key: None for key in STATS_CLOSING},
        "net_backlog_delta": (inflow - sum(closures.values())) if have_data else None,
        "open_now": open_count if have_data else None,
        "open_by_priority": open_by_priority if have_data else None,
        "median_hours_to_first_claim": _median(first_claim_hours),
        "median_hours_to_close": _median(close_hours),
        "reopened": reopened if have_data else None,
        "resolver_runs": dict(sorted(resolver_counts.items())),
        "claude_share_pct": round(100.0 * claude_runs / total_runs, 1) if total_runs else None,
        "top_components_inflow": [
            {"component": component, "count": count}
            for component, count in sorted(components.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        ],
        "top_recurring": sorted(recurring, key=lambda row: (-row["count"], row["id"]))[:5],
    }
    return report, start, end


def collect_stats(days: int = 7) -> dict[str, Any]:
    """Queue metrics of the last `days` days (default 7). Read-only."""
    report, _start, _end = _collect_stats(days)
    return report


def _stat_text(value: Any, suffix: str = "") -> str:
    if value is None:
        return "brak danych"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def stats_lines(report: dict[str, Any], start: dt.datetime, end: dt.datetime) -> list[str]:
    """Short Polish summary of the metrics (no diacritics, like the other files)."""
    closures = report.get("closures") or {}
    closed = closures.get("closed")
    wontfix = closures.get("wontfix")
    deployed = closures.get("deployed")
    open_by_priority = report.get("open_by_priority") or {}
    resolver_runs = report.get("resolver_runs") or {}
    runs_text = ", ".join(f"{name} {count}" for name, count in resolver_runs.items()) or "brak danych"
    components = report.get("top_components_inflow") or []
    components_text = ", ".join(f"{row['component']} {row['count']}" for row in components) or "brak danych"
    recurring = report.get("top_recurring") or []
    recurring_text = ", ".join(f"{row['id']} ({row['component']}, {row['count']}x)" for row in recurring) or "brak danych"
    return [
        f"Okno: {report.get('window_days')} dni ({iso(start)} - {iso(end)})",
        f"Naplyw: {_stat_text(report.get('inflow'))} | Zamkniecia: closed {_stat_text(closed)}, "
        f"wontfix {_stat_text(wontfix)}, deployed {_stat_text(deployed)} | "
        f"Bilans: {_stat_text(report.get('net_backlog_delta'))}",
        f"Otwarte teraz: {_stat_text(report.get('open_now'))} "
        f"(P1 {_stat_text(open_by_priority.get('P1'))}, P2 {_stat_text(open_by_priority.get('P2'))}, "
        f"P3 {_stat_text(open_by_priority.get('P3'))}, brak {_stat_text(open_by_priority.get('none'))})",
        f"Mediany: do pierwszego claimu {_stat_text(report.get('median_hours_to_first_claim'), ' h')}, "
        f"do zamkniecia {_stat_text(report.get('median_hours_to_close'), ' h')}",
        f"Reopeny: {_stat_text(report.get('reopened'))} | Runy resolverow: {runs_text} | "
        f"Udzial claude: {_stat_text(report.get('claude_share_pct'), ' %')}",
        f"Najczestsze komponenty: {components_text}",
        f"Powtarzajace sie zgloszenia: {recurring_text}",
    ]


# ---------------------------------------------------------------- Fleet scan

def fleet_runs_dir() -> Path:
    return Path(os.environ.get("RSI_HELPDESK_FLEET_RUNS") or Path.home() / ".agents" / "agent-fleet" / "runs")


def stderr_roots() -> list[Path]:
    """Only the stack's own work areas: client and project run directories are never read."""
    configured = os.environ.get("RSI_HELPDESK_STDERR_ROOTS")
    raw = configured.split(os.pathsep) if configured is not None else [
        str(Path(__file__).resolve().parents[5]), str(fleet_runs_dir())]
    roots = [Path(r) for r in raw if r.strip()]
    if any(not root.is_absolute() for root in roots):
        raise ValueError("RSI_HELPDESK_STDERR_ROOTS must contain absolute paths")
    return [root.resolve() for root in roots]


def stderr_tail(run: dict[str, Any]) -> list[str]:
    output_dir = str(run.get("output_dir") or "")
    if not output_dir or output_dir.startswith(("\\\\", "//")):
        return []
    try:
        path = (Path(output_dir) / "stderr.txt").resolve()
    except (OSError, ValueError):
        return []
    if not any(path.is_relative_to(root) for root in stderr_roots()):
        return []
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4000))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return [line.strip() for line in tail.splitlines() if line.strip()][-20:]


def run_error_class(run: dict[str, Any]) -> str | None:
    if str(run.get("run_status") or "").upper() != "FAILED":
        return None
    # The complete untruncated central error is checked for quota/transient causes before
    # the first-line display form is built: truncation can drop a late or line-2 marker.
    central_error = str(run.get("handoff_parse_error") or "")
    if TRANSIENT_PATTERNS.search(central_error):
        return None
    handoff_error = central_error.split("\n")[0][:120]
    parts = [handoff_error, str(run.get("completion_reason") or "")]
    tail = stderr_tail(run)
    if any(TRANSIENT_PATTERNS.search(line) for line in tail):
        return None
    if tail and ("provider" in handoff_error.lower() or not handoff_error):
        parts.append(clean(tail[-1][-200:], 200))
    text = " | ".join(p for p in parts if p and p != "None")
    if not text or TRANSIENT_PATTERNS.search(text):
        return None
    return normalize_symptom(text)


def scan_fleet(
    *, lookback_hours: int = 24, threshold: int = 3, min_workspaces: int = 2,
    file: bool = True,
) -> list[dict[str, Any]]:
    """File one ticket per repeated (agent, model, error) failure pattern.

    Runs older than the matching ticket's last resolution are ignored, so a fixed
    defect only reopens when it recurs after the fix. Rescanning the same runs
    changes nothing.
    """
    cutoff = now() - dt.timedelta(hours=lookback_hours)
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    root = fleet_runs_dir()
    if not root.is_dir():
        return []
    for path in root.glob("run-*.json"):
        try:
            stamp = dt.datetime.strptime(path.name[4:20], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if stamp < cutoff:
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        error = run_error_class(run)
        if error is None:
            continue
        key = (clean(run.get("agent") or "?", 60), clean(run.get("model") or "?", 80), error)
        groups.setdefault(key, []).append({"run_id": clean(run.get("run_id") or path.stem, 80), "stamp": stamp,
                                           "workspace": str(run.get("workspace") or "")})
    findings = []
    for (agent, model, error), rows in sorted(groups.items()):
        component = f"fleet-adapter:{agent}"
        try:
            previous = load(signature(component, error, model))
        except CorruptTicket:
            findings.append({"component": component, "model": model, "symptom": f"{model}: {error}",
                             "runs": [r["run_id"] for r in rows], "action": "corrupt-ticket"})
            continue
        resolved = parse_iso((previous or {}).get("resolved_at"))
        if resolved:
            rows = [r for r in rows if r["stamp"] > resolved]
        workspaces = {r["workspace"] for r in rows}
        if len(rows) < threshold or len(workspaces) < min_workspaces:
            continue
        rows.sort(key=lambda r: r["stamp"])
        finding = {"component": component, "model": model, "symptom": f"{model}: {error}",
                   "runs": [r["run_id"] for r in rows], "workspaces": len(workspaces)}
        if file:
            ticket, action = file_ticket(
                component=component, symptom=error, model=model,
                evidence=[f"fleet run {r['run_id']}" for r in rows[-5:]],
                impact=f"{len(rows)} FAILED runs in {lookback_hours} h across {len(workspaces)} workspaces",
                workaround="callers switched agent or model", reporter="rsi-helpdesk-fleet-scan",
                source="fleet-scan", run_stamps={r["run_id"]: iso(r["stamp"]) for r in rows},
                min_runs=threshold,
            )
            finding.update(ticket=(ticket or {}).get("id"), signature=(ticket or {}).get("signature"),
                           action=action)
            if ticket and action in {"new", "reopened"}:
                announce(ticket, action)
        findings.append(finding)
    return findings


# ---------------------------------------------------------------- Office / trigger

def office_script() -> Path | None:
    configured = os.environ.get("RSI_HELPDESK_OFFICE")
    candidates = [Path(configured)] if configured is not None else [
        Path(__file__).resolve().parents[5] / "shared" / "agent-office" / "office.py",
        Path.home() / ".agents" / "runtime" / "agent-office" / "office.py"]
    for candidate in candidates:
        if candidate.is_absolute() and candidate.is_file():
            return candidate
    return None


def announce(ticket: dict[str, Any], action: str) -> bool:
    """Best-effort Office post; the ticket store stays authoritative."""
    if os.environ.get("RSI_HELPDESK_NO_OFFICE") == "1":
        return False
    script = office_script()
    if script is None:
        return False
    body = (
        f"[{ticket['id']}] {action}: {ticket['component']}"
        + (f" ({ticket['model']})" if ticket.get("model") else "") + "\n"
        f"Objaw: {ticket['symptom'][:600]}\n"
        f"Skutek: {ticket.get('impact') or '-'} | Obejscie: {ticket.get('workaround') or '-'}\n"
        f"Licznik: {ticket.get('count')} | sygnatura {ticket['signature']}\n"
        "Obsluga: RSI Helpdesk (rsi-helpdesk). Nie naprawiaj tego w swoim zadaniu; dopisz dowod przez helpdesk.py file."
    )
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(script), "--agent", "rsi-helpdesk", "--harness", "script",
             "send", "--channel", OFFICE_CHANNEL, "--kind", "alert", "--body-stdin",
             "--dedupe-key", f"hd-{ticket['signature']}-{action}-{ticket.get('count')}"],
            input=body.encode("utf-8"), capture_output=True, timeout=60,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def trigger_cycle() -> bool:
    """Ask Task Scheduler to run the cycle task now.

    The task is the only launcher: it runs through HiddenLaunch on the private
    AgentStackBackground desktop, so neither the cycle nor any resolver descendant
    can open a window on the user's desktop. No task means no trigger; the
    schedule (when installed) picks the ticket up instead.
    """
    if os.environ.get("RSI_HELPDESK_NO_TRIGGER") == "1" or os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Run", "/TN", TASK_NAME], capture_output=True, timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------- CLI

def summary(ticket: dict[str, Any]) -> dict[str, Any]:
    return {k: ticket.get(k) for k in ("id", "signature", "status", "component", "model", "count", "priority",
                                       "last_seen", "claim", "awaiting_owner", "eligible_at")} | {
        "symptom": (ticket.get("symptom") or "")[:160]}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="helpdesk", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("file", help="File or merge a ticket for a stack defect")
    f.add_argument("--component", required=True, help="e.g. fleet-adapter:muse, office, hub, skill:agent-graph")
    f.add_argument("--symptom", required=True, help="What fails, one or two sentences, no secrets")
    f.add_argument("--evidence", action="append", default=[], help="run id, log path, message id (repeatable)")
    f.add_argument("--model", default="", help="model id when the defect is model-specific")
    f.add_argument("--impact", default="")
    f.add_argument("--workaround", default="")
    f.add_argument("--reporter", default=os.environ.get("RSI_HELPDESK_REPORTER", "unknown-agent"))
    f.add_argument("--no-trigger", action="store_true")
    sub.add_parser("list", help="Active tickets").add_argument("--all", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("ticket")
    c = sub.add_parser("claim")
    c.add_argument("ticket")
    c.add_argument("--by", required=True)
    c.add_argument("--minutes", type=int, default=DEFAULT_CLAIM_MINUTES)
    u = sub.add_parser("update")
    u.add_argument("ticket")
    u.add_argument("--by", required=True)
    u.add_argument("--status", required=True, choices=[x for x in STATUSES if x != "claimed"])
    u.add_argument("--note", required=True)
    u.add_argument("--lesson", default=None,
                   help="short reusable lesson (max 240 chars) appended under the current claim for future resolvers")
    u.add_argument("--lesson-context", default=None,
                   help='JSON object with target/outcome/applicability attached to --lesson; advisory only, requires --lesson')
    u.add_argument("--force", action="store_true", help="owner by hand only: change status without the claim")
    u.add_argument("--release", action="store_true",
                   help="with --status candidate: work finished, candidate waits for the owner")
    u.add_argument("--defer-until", default=None,
                   help="with --status open: defer ticket eligibility until timezone-aware ISO8601 timestamp")
    sub.add_parser("next", help="Most urgent open ticket")
    sp = sub.add_parser("set-priority", help="Set the queue priority (no claim, status unchanged)")
    sp.add_argument("ticket")
    sp.add_argument("--priority", required=True, choices=[*PRIORITIES, "none"])
    sp.add_argument("--by", required=True)
    sp.add_argument("--note", default="")
    st = sub.add_parser("stats", help="Weekly queue metrics (read-only)")
    st.add_argument("--days", type=int, default=7, help="window length in days (default 7)")
    st.add_argument("--json", action="store_true", help="print the raw JSON report")
    st.add_argument("--out", default=None, help="write the JSON report to this path")
    r = sub.add_parser("related",
                       help="Same-domain prior history with reusable lessons (bounded projection for the resolver prompt)")
    r.add_argument("ticket")
    r.add_argument("--limit", type=int, default=5, help="max entries (default 5)")
    r.add_argument("--byte-limit", type=int, default=4096,
                   help="max UTF-8 bytes including metadata (default 4096)")
    sc = sub.add_parser("scan-fleet", help="File tickets for repeated Fleet failures")
    sc.add_argument("--lookback-hours", type=int, default=24)
    sc.add_argument("--threshold", type=int, default=3)
    sc.add_argument("--min-workspaces", type=int, default=2)
    sc.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    def resolve(ref: str) -> str:
        ref_clean = ref.strip()
        # Fast path 1: direct 12-hex signature
        if re.fullmatch(r"[0-9a-f]{12}", ref_clean, re.I):
            sig = ref_clean.lower()
            if ticket_path(sig).is_file():
                return sig
        # Fast path 2: HD-date-6hex id (e.g. HD-20261017-abcdef)
        m = re.fullmatch(r"HD-\d{8}-([0-9a-f]{6})", ref_clean, re.I)
        if m:
            prefix = m.group(1).lower()
            matches = []
            if tickets_dir().is_dir():
                for tp in tickets_dir().glob(f"{prefix}*.json"):
                    with contextlib.suppress(CorruptTicket):
                        t = parse_ticket(tp)
                        if t.get("id") == ref_clean:
                            matches.append(t["signature"])
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise SystemExit(f"ambiguous ticket id {ref}")
            raise SystemExit(f"no ticket {ref}")
        # For hot "related" command, do NOT scan unrelated tickets
        if args.command == "related":
            raise SystemExit(f"no ticket {ref}")
        # Fallback for other commands to preserve compatibility
        for ticket in all_tickets():
            if ref in (ticket.get("signature"), ticket.get("id")):
                return ticket["signature"]
        raise SystemExit(f"no ticket {ref}")

    if args.command == "file":
        ticket, action = file_ticket(
            component=args.component, symptom=args.symptom, evidence=args.evidence,
            impact=args.impact, workaround=args.workaround, reporter=args.reporter, model=args.model,
        )
        announced = announce(ticket, action) if action in {"new", "reopened"} else False
        triggered = trigger_cycle() if action in {"new", "reopened"} and not args.no_trigger else False
        print(json.dumps({"action": action, "announced": announced, "triggered": triggered} | summary(ticket),
                         ensure_ascii=False))
    elif args.command == "list":
        rows = [summary(t) for t in all_tickets() if args.all or t.get("status") in ACTIVE]
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    elif args.command == "show":
        print(json.dumps(load(resolve(args.ticket)), ensure_ascii=False, indent=1))
    elif args.command == "claim":
        print(json.dumps(summary(claim(resolve(args.ticket), args.by, args.minutes)), ensure_ascii=False))
    elif args.command == "update":
        context_obj: dict[str, Any] | None = None
        if args.lesson_context is not None:
            try:
                parsed = json.loads(args.lesson_context)
            except ValueError as exc:
                raise SystemExit(f"--lesson-context must be valid JSON: {exc}")
            if not isinstance(parsed, dict):
                raise SystemExit("--lesson-context must decode to a JSON object")
            context_obj = parsed
        print(json.dumps(summary(update(resolve(args.ticket), args.by, args.status, args.note, force=args.force,
                                         release_claim=args.release, defer_until=args.defer_until,
                                         lesson=args.lesson, lesson_context=context_obj)), ensure_ascii=False))
    elif args.command == "next":
        ticket = next_open()
        print(json.dumps(summary(ticket) if ticket else None, ensure_ascii=False))
    elif args.command == "set-priority":
        priority = None if args.priority == "none" else args.priority
        print(json.dumps(summary(set_priority(resolve(args.ticket), priority, args.by, args.note)),
                         ensure_ascii=False))
    elif args.command == "stats":
        report, window_start, window_end = _collect_stats(args.days)
        if args.out:
            out_path = Path(args.out)
            if out_path.parent and not out_path.parent.exists():
                out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=1))
        else:
            for line in stats_lines(report, window_start, window_end):
                print(line)
    elif args.command == "related":
        target = load(resolve(args.ticket))
        if target is None:
            raise SystemExit(f"no ticket {args.ticket}")
        try:
            import repair_history  # noqa: WPS433 (lazy import to keep helpdesk importable alone)
        except Exception as exc:
            raise SystemExit(f"repair-history unavailable: {exc}")
        projection = repair_history.related_history(target, limit=args.limit, byte_limit=args.byte_limit)
        print(json.dumps(projection, ensure_ascii=False, indent=1))
    elif args.command == "scan-fleet":
        print(json.dumps(scan_fleet(lookback_hours=args.lookback_hours, threshold=args.threshold,
                                    min_workspaces=args.min_workspaces, file=not args.dry_run),
                         ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
