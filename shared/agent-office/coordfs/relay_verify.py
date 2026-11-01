"""Local, fail-closed authorization of Claude Desktop relay dispatch text."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from string import Formatter
from typing import Any, Mapping

from .errors import CoordError
from .wake import (
    CHILD_TERMINAL_PROMPT_TEMPLATE,
    CORRELATED_WAKE_PROMPT_TEMPLATE,
    SCHEDULE_PROMPT_TEMPLATE,
    TERMINAL_RUN_STATES,
    WAKE_PROMPT_TEMPLATE,
)


MAX_ALLOW_BYTES = 64 * 1024
MAX_TEXT_BYTES = 16 * 1024
_BINDING = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_SCHEDULE = _BINDING
_LOCAL_ID = re.compile(r"local_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_RESULT_REF = re.compile(r"[^\x00-\x1f\x7f]{1,4096}\Z")
_TRIGGERS = frozenset({"message", "schedule", "child-terminal"})
_FIELDS = {
    "message_id": r"[0-9a-f]{32}",
    "sender": r"[a-z0-9][a-z0-9._-]{0,399}",
    # The source rule (wake_correlation._ID_TEXT_RE), not the wider prompt sanitizer: a
    # compromised Office cannot smuggle a sentence through these fields.
    "task_id": r"[a-z0-9][a-z0-9._-]{0,127}",
    "correlation_id": r"[a-z0-9][a-z0-9._-]{0,127}",
    "schedule_id": r"[a-z0-9][a-z0-9._-]{0,127}",
    # A child run id is an identifier, never text; the result path is pinned locally
    # (allow "children"), so neither field can carry a sentence into the prompt.
    "run_id": r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
    "terminal_state": "(?:" + "|".join(sorted(TERMINAL_RUN_STATES)) + ")",
    # run_id and result_ref stay wide (they carry paths); a binding allows the
    # child-terminal trigger only on purpose.
    "result_ref": r"[^\x00-\x1f\x7f]{1,4096}",
    # Only task_text may contain line breaks. The trusted digest binds its contents.
    "task_text": r"[\s\S]*?",
}


def _invalid_allow() -> CoordError:
    return CoordError("Invalid local relay allow file", code="relay_allow_invalid")


def _validate_allow(allow: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(allow, Mapping) or set(allow) != {"version", "bindings"}:
        raise _invalid_allow()
    if type(allow["version"]) is not int or allow["version"] != 1:
        raise _invalid_allow()
    bindings = allow["bindings"]
    if not isinstance(bindings, Mapping):
        raise _invalid_allow()
    for binding_id, binding in bindings.items():
        if not isinstance(binding_id, str) or not _BINDING.fullmatch(binding_id):
            raise _invalid_allow()
        if not isinstance(binding, Mapping) or not {"target_local_id", "triggers"} <= set(binding):
            raise _invalid_allow()
        if set(binding) - {"target_local_id", "triggers", "schedules", "children"}:
            raise _invalid_allow()
        target = binding["target_local_id"]
        if not isinstance(target, str) or not _LOCAL_ID.fullmatch(target):
            raise _invalid_allow()
        triggers = binding["triggers"]
        if (
            not isinstance(triggers, list)
            or not triggers
            or any(not isinstance(item, str) or item not in _TRIGGERS for item in triggers)
            or len(triggers) != len(set(triggers))
        ):
            raise _invalid_allow()
        children = binding.get("children", {})
        if not isinstance(children, Mapping):
            raise _invalid_allow()
        for run_id, result_ref in children.items():
            if (not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id)
                    or not isinstance(result_ref, str) or not _RESULT_REF.fullmatch(result_ref)):
                raise _invalid_allow()
        schedules = binding.get("schedules", {})
        if not isinstance(schedules, Mapping):
            raise _invalid_allow()
        for schedule_id, digest in schedules.items():
            if (not isinstance(schedule_id, str) or not _SCHEDULE.fullmatch(schedule_id)
                    or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
                raise _invalid_allow()
    return bindings


def load_allow(path: str | Path) -> dict[str, Any]:
    """Load a small, explicitly local allow file without reading Office state."""
    try:
        source = Path(path)
        if not source.is_file() or source.stat().st_size > MAX_ALLOW_BYTES:
            raise _invalid_allow()
        with source.open("rb") as stream:
            raw = stream.read(MAX_ALLOW_BYTES + 1)
        if len(raw) > MAX_ALLOW_BYTES:
            raise _invalid_allow()
        value = json.loads(raw.decode("utf-8"))
        _validate_allow(value)
        return value
    except (OSError, ValueError, UnicodeError, TypeError) as exc:
        raise _invalid_allow() from exc


def _pattern(template: str, *, strict: bool) -> re.Pattern[str]:
    pieces: list[str] = []
    seen: set[str] = set()
    for literal, field, format_spec, conversion in Formatter().parse(template):
        pieces.append(re.escape(literal))
        if field is None:
            continue
        if field not in _FIELDS or format_spec or conversion:
            raise ValueError("Unsupported Wake template field")
        if field in seen:
            pieces.append(f"(?P={field})")
        else:
            # The relaxed pattern distinguishes invalid field data from changed
            # constant text. It never grants authorization.
            expression = _FIELDS[field] if strict else (r"[\s\S]*?" if field == "task_text" else r"[^\n]*?")
            pieces.append(f"(?P<{field}>{expression})")
            seen.add(field)
    return re.compile("".join(pieces))


_TEMPLATES = {
    "correlated": ("message", CORRELATED_WAKE_PROMPT_TEMPLATE),
    "message": ("message", WAKE_PROMPT_TEMPLATE),
    "schedule": ("schedule", SCHEDULE_PROMPT_TEMPLATE),
    "child_terminal": ("child-terminal", CHILD_TERMINAL_PROMPT_TEMPLATE),
}
_PATTERNS = {
    name: (_pattern(template, strict=True), _pattern(template, strict=False))
    for name, (_, template) in _TEMPLATES.items()
}
_SCHEDULE_END = SCHEDULE_PROMPT_TEMPLATE.split("{task_text}", 1)[1]


def verify_dispatch(dispatch: Mapping[str, Any], allow: Mapping[str, Any]) -> dict[str, Any]:
    """Check only locally pinned target, trigger, template and schedule text."""
    bindings = _validate_allow(allow)
    binding_id = dispatch.get("binding_id") if isinstance(dispatch, Mapping) else None
    binding_id = binding_id if isinstance(binding_id, str) else None
    reasons: list[str] = []

    def reject(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if not isinstance(dispatch, Mapping):
        reject("dispatch_invalid")
        dispatch = {}
    required = ("binding_id", "target_local_id", "trigger", "text")
    if any(not isinstance(dispatch.get(key), str) for key in required):
        reject("dispatch_invalid")
    text = dispatch.get("text")
    if isinstance(text, str):
        try:
            if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                reject("dispatch_invalid")
        except UnicodeError:
            reject("dispatch_invalid")
    binding = bindings.get(binding_id) if binding_id is not None else None
    if binding_id is not None and binding is None:
        reject("binding_not_allowed")
    if binding is not None:
        if dispatch.get("target_local_id") != binding["target_local_id"]:
            reject("target_mismatch")
        if dispatch.get("trigger") not in binding["triggers"]:
            reject("trigger_not_allowed")

    template_name: str | None = None
    fields: dict[str, str] = {}
    if isinstance(text, str) and "dispatch_invalid" not in reasons:
        for name, (expected_trigger, _) in _TEMPLATES.items():
            strict, loose = _PATTERNS[name]
            matched = strict.fullmatch(text)
            if matched:
                template_name = name
                fields = matched.groupdict()
                if dispatch.get("trigger") != expected_trigger:
                    reject("template_mismatch")
                break
            if loose.fullmatch(text):
                reject("field_invalid")
        if template_name is None and "field_invalid" not in reasons:
            if "\n" in text and not text.startswith("[Office Wake] zadanie z harmonogramu "):
                reject("field_invalid")
            else:
                reject("template_mismatch")

    if template_name == "child_terminal":
        # Fail closed without local context: only a child the relay host pinned itself (run
        # id -> exact result path) is relayed; Office cannot introduce or redirect one.
        children = binding.get("children", {}) if binding is not None else {}
        pinned = children.get(fields["run_id"])
        if pinned is None:
            reject("child_not_allowed")
        elif fields["result_ref"] != pinned:
            reject("result_ref_mismatch")

    if template_name == "schedule":
        schedule_id = fields["schedule_id"]
        task_text = fields["task_text"]
        if _SCHEDULE_END in task_text:
            reject("field_invalid")
        schedules = binding.get("schedules", {}) if binding is not None else {}
        digest = schedules.get(schedule_id)
        if digest is None:
            reject("schedule_not_allowed")
        elif hashlib.sha256(task_text.encode("utf-8")).hexdigest() != digest:
            reject("task_text_changed")

    if reasons:
        return {"status": "error", "code": "relay_verify_rejected",
                "reasons": reasons, "binding_id": binding_id}
    return {"status": "ok", "code": "relay_dispatch_verified",
            "binding_id": binding_id, "template": template_name}
