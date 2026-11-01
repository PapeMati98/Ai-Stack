"""Bounded native Antigravity context and quota-reset metadata parsing.

Only agent_response step_update events carry usable input-context signal.
The aggregate outer event=result is cumulative, not a per-step observation,
and is always ignored. See maker-brief.md for the observed native shape.
"""
import json
import re
import sys
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import process_supervision

MAX_STR_LEN = 1024
MAX_INT = 10 ** 12

_UNREADABLE_MESSAGE = 'Antigravity context event could not be read.'


def quota_reset_seconds(terminal, stderr=''):
    """Read a reset from native failure fields, never history/tool prose.

    Fleet already selected the terminal result; do not parse stdout again.
    A refusal marker and the complete ordered d/h/m/s duration must share one
    message. Conflicting resets do not provide a trustworthy expiry.
    """
    messages = []
    if isinstance(terminal, dict):
        if str(terminal.get('status', '')).casefold() not in {'error', 'api_error', 'failed'}:
            return None
        messages.extend(terminal.get(key) for key in ('error', 'response'))
    for line in stderr.splitlines():
        if line.startswith('Error: '):
            messages.append(line[len('Error: '):])
        elif line.startswith('AGY_ERROR: '):
            try:
                error = json.loads(line[len('AGY_ERROR: '):])
            except (ValueError, RecursionError):
                continue
            if isinstance(error, dict):
                messages.append(error.get('short_error'))
    durations = set()
    for message in messages:
        if not isinstance(message, str) or len(message) > 8192:
            continue
        if not re.search(r'\b(?:RESOURCE_EXHAUSTED|HTTP\s+429|quota reached)\b', message, re.I):
            continue
        match = re.search(r'\bResets in (?:([0-9]+)d)?(?:([0-9]+)h)?(?:([0-9]+)m)?(?:([0-9]+)s)?\.?\s*$',
                          message, re.I)
        if not match:
            continue
        try:
            duration = sum(int(value or 0) * unit for value, unit in
                           zip(match.groups(), (86400, 3600, 60, 1)))
        except ValueError:
            continue
        if 0 < duration <= 999999999 * 86400:
            durations.add(duration)
    return next(iter(durations)) if len(durations) == 1 else None


def _valid_identity(step_update):
    conversation_id = step_update.get('conversation_id')
    step_index = step_update.get('step_index')
    if not isinstance(conversation_id, str) or not conversation_id.strip() or not (0 < len(conversation_id) <= MAX_STR_LEN):
        return None
    if type(step_index) is not int or isinstance(step_index, bool):
        return None
    if not (0 <= step_index <= MAX_INT):
        return None
    return (conversation_id, step_index)


def _valid_usage_total(usage):
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get('input_tokens')
    cache_read_tokens = usage.get('cache_read_tokens')
    for value in (input_tokens, cache_read_tokens):
        if type(value) is not int or isinstance(value, bool):
            return None
        if not (0 <= value <= MAX_INT):
            return None
    total = input_tokens + cache_read_tokens
    if total > MAX_INT:
        return None
    return total


def _classify(event):
    """Return None for irrelevant events, else a dict with identity/total/valid."""
    if not isinstance(event, dict):
        return None
    if event.get('event') != 'step_update':
        return None
    step_update = event.get('step_update')
    if not isinstance(step_update, dict):
        return {'identity': None, 'total': None, 'valid': False}
    if 'step_type' not in step_update:
        return {'identity': None, 'total': None, 'valid': False}
    if step_update.get('step_type') != 'agent_response':
        return None
    has_usage_key = 'usage' in step_update
    if step_update.get('state') != 'DONE' and not has_usage_key:
        return None
    identity = _valid_identity(step_update)
    usage_total = _valid_usage_total(step_update.get('usage')) if has_usage_key else None
    valid = identity is not None and usage_total is not None
    return {'identity': identity, 'total': usage_total, 'valid': valid}


def measure_steps(events):
    """Measure native per-step input-context usage from parsed event dicts.

    Dedup by (conversation_id, step_index) in first-seen order, retaining the
    maximum valid observed input for each step. Any relevant but unmeasurable
    step (missing identity or invalid/missing counters) taints completeness
    without discarding other already-known steps.
    """
    order = []
    best = {}
    incomplete = False
    for event in events:
        result = _classify(event)
        if result is None:
            continue
        if not result['valid']:
            incomplete = True
            continue
        identity = result['identity']
        total = result['total']
        if identity not in best:
            order.append(identity)
            best[identity] = total
        elif total > best[identity]:
            best[identity] = total
    return {
        'steps': [best[identity] for identity in order],
        'incomplete': incomplete,
        'tool_result_bytes': None,
        'compaction_events': None,
    }


def stream_context_tokens(line):
    """Stateless per-line callback for process_supervision.run(context_tokens=...).

    Returns a measured per-step input for a relevant terminal/usage-bearing
    agent_response event, None for irrelevant lines/events, and raises
    process_supervision.ContextEventUnreadable for a recognizable malformed
    JSON object or a relevant event with invalid shape/counters.
    """
    try:
        parsed = json.loads(line)
    except (ValueError, TypeError, RecursionError):
        stripped = line.lstrip()
        if stripped[:1] in ('{', '['):
            raise process_supervision.ContextEventUnreadable(_UNREADABLE_MESSAGE)
        return None
    result = _classify(parsed)
    if result is None:
        return None
    if not result['valid']:
        raise process_supervision.ContextEventUnreadable(_UNREADABLE_MESSAGE)
    return result['total']
