"""Bounded JEV advice from selected Fleet profiles; never executes a worker.

Only a deliberately minimized task file reaches OpenRouter. Fleet readiness,
quota gates and independent review remain authoritative at execution time.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def timestamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def load(path):
    if Path(path).stat().st_size > 1024 * 1024:
        raise ValueError('INPUT_TOO_LARGE')
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_new(path, value):
    # Refuse accidental replacement of an approved plan or another run's evidence.
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def guard_metadata(value):
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    patterns = [r'-----BEGIN [A-Z ]*PRIVATE KEY-----', r'\bsk-[A-Za-z0-9_-]{16,}',
                r'(?i)\bbearer\s+[A-Za-z0-9._-]{16,}',
                r'(?i)(?:api[_-]?key|password|access[_-]?token|secret)\s*[=:]\s*[\"\']?\S{8,}',
                r'(?i)[a-z]:\\\\', r'(?i)/(?:home|Users)/[^/]+/']
    if any(re.search(pattern, text) for pattern in patterns):
        raise ValueError('METADATA_REQUIRES_REDACTION')


def digest(value):
    """Correlation only: a digest never authenticates a report or grants consent."""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def public_snapshot(snapshot):
    return {k: snapshot[k] for k in ('captured_at', 'candidates', 'active_assignments', 'capacity')}


def fresh_time(value, *, now):
    from jev_policy import STALE_SECONDS, _parse_iso
    age = (now - _parse_iso(value)).total_seconds()
    if not 0 <= age <= STALE_SECONDS:
        raise ValueError('REPORT_OR_SNAPSHOT_NOT_FRESH')


def binding_for(plan, task_id, task, snapshot):
    """Bind the whole original plan, not a lossy projection of the selected node."""
    from jev_client import build_request
    from todo_graph import validate
    validate(plan)
    matches = [row for row in plan['tasks'] if row['id'] == task_id]
    if len(matches) != 1:
        raise ValueError('TASK_NOT_UNIQUE')
    node = matches[0]
    for field, expected in (('mode', node.get('mode', 'implement')),
                            ('scope', node['scope']),
                            ('resources', node.get('resources', [])),
                            ('depends_on', node.get('depends_on', []))):
        if task.get(field) != expected:
            raise ValueError('TASK_BINDING_MISMATCH')
    # Validate every minimized field and public candidate before hashing/sending.
    build_request(task, public_snapshot(snapshot))
    return {'version': 1, 'task_id': task_id, 'task_sha256': digest(task),
            'todo_sha256': digest(plan), 'snapshot_sha256': digest(snapshot),
            'snapshot_captured_at': snapshot['captured_at']}


def unavailable_recommendation(task, snapshot, *, now):
    from jev_policy import recommend
    result = recommend(task, {**public_snapshot(snapshot), 'schema_version': 1}, None, now=now)
    result['requires_parent_review'] = True
    if 'JEV_UNAVAILABLE' not in result['requires_parent_review_reasons']:
        result['requires_parent_review_reasons'].append('JEV_UNAVAILABLE')
    return result


def verify_report(report, plan, task_id, task, snapshot, *, now=None):
    """Reject legacy, changed or edited advice before TODO mutation or reuse."""
    from jev_client import validate_normalized_decision
    from jev_policy import _parse_iso, recommend
    current = now or datetime.now(timezone.utc)
    expected_binding = binding_for(plan, task_id, task, snapshot)
    if not isinstance(report, dict) or report.get('schema_version') != 1 \
            or report.get('binding') != expected_binding:
        raise ValueError('TASK_BINDING_MISMATCH')
    fresh_time(snapshot['captured_at'], now=current)
    fresh_time(report.get('created_at'), now=current)
    created = _parse_iso(report['created_at'])
    if created < _parse_iso(snapshot['captured_at']):
        raise ValueError('REPORT_OR_SNAPSHOT_NOT_FRESH')
    if report.get('execution') != 'NOT_DISPATCHED':
        raise ValueError('REPORT_INVALID')
    status = report.get('consultation_status')
    if status == 'AVAILABLE':
        validate_normalized_decision(report.get('decision'), public_snapshot(snapshot))
        if report.get('consultation_error_code') is not None:
            raise ValueError('REPORT_INVALID')
        expected = recommend(task, {**public_snapshot(snapshot), 'schema_version': 1},
                             report['decision'], now=created)
    elif status == 'UNAVAILABLE':
        if report.get('decision') is not None or not isinstance(report.get('consultation_error_code'), str) \
                or not re.fullmatch(r'[a-z_0-9]{3,80}', report['consultation_error_code']):
            raise ValueError('REPORT_INVALID')
        expected = unavailable_recommendation(task, snapshot, now=created)
    else:
        raise ValueError('REPORT_INVALID')
    if report.get('recommendation') != expected:
        raise ValueError('RECOMMENDATION_MISMATCH')
    return expected


def funding(profile):
    # Read the installation-owned declaration, never infer a paid plan from an API key.
    mode = profile.get('advisor_billing')
    if mode in ('subscription', 'local', 'api'):
        return mode
    if profile.get('local_cost_lever') is True:
        return 'local'
    return None


def quota_numbers(quota):
    fresh = quota.get('current') is True and quota.get('status') == 'fresh'
    values = [quota.get(k) for k in ('weekly_remaining_fraction', 'short_window_remaining_fraction')]
    observed = quota.get('observed_at') if fresh else None
    availability = quota.get('availability', {})
    if quota.get('availability_verified') is True and isinstance(availability, dict):
        try:
            observed_time = datetime.fromisoformat(availability['observed_at'].replace('Z', '+00:00'))
            recheck = datetime.fromisoformat(availability['recheck_at'].replace('Z', '+00:00'))
            current = datetime.now(timezone.utc)
            native_fresh = observed_time <= current < recheck and (current - observed_time).total_seconds() <= 300
        except (ValueError, KeyError, TypeError):
            native_fresh = False
        if native_fresh:
            windows = availability.get('windows', [])
            values = [1 - w['used'] / w['limit'] for w in windows
                      if isinstance(w, dict) and number(w.get('used')) and number(w.get('limit'))
                      and w['limit'] > 0 and 0 <= w['used'] <= w['limit']]
            fresh, observed = True, availability['observed_at']
    return (min([v for v in values if number(v) and 0 <= v <= 1], default=None) if fresh else None,
            observed, fresh)


def codex_context_windows(cache, *, now=None):
    """Read only effective default windows from a fresh native model catalogue.

    Maximum/opt-in windows, names and account identity are never inferred.
    Missing, ambiguous or stale evidence stays unknown.
    """
    try:
        current = now or datetime.now(timezone.utc)
        captured = datetime.fromisoformat(cache['fetched_at'].replace('Z', '+00:00'))
        if not 0 <= (current - captured).total_seconds() <= 86400:
            return {}
        rows = cache['models']
        if not isinstance(rows, list):
            return {}
        windows, duplicates = {}, set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            model, window = row.get('slug'), row.get('context_window')
            percent = row.get('effective_context_window_percent')
            if not isinstance(model, str) or not model:
                continue
            if model in windows:
                duplicates.add(model)
            if type(window) is int and window > 0 and type(percent) is int and 1 <= percent <= 100:
                windows[model] = window * percent // 100
            else:
                duplicates.add(model)
        return {model: size for model, size in windows.items() if size > 0 and model not in duplicates}
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return {}


def local_context_windows(profiles):
    if 'codex' not in profiles:
        return {}
    try:
        return codex_context_windows(load(Path.home()/'.codex/models_cache.json'))
    except (OSError, ValueError):
        return {}


MAX_SNAPSHOT_AGENTS = 16


def select_profiles(profiles, requested):
    """Candidate pool for a snapshot and the visible trace of what was left out.

    No selection (or the single word ``all``) means every routing-enabled
    profile: a parent's habitual short list must not silently hide a paid,
    capable pool (Muse and Grok were absent from whole days of plans). An
    explicit subset stays allowed, but each omitted enabled profile is
    recorded as NOT_SELECTED_BY_PARENT in the snapshot diagnostics.
    """
    enabled = sorted(name for name, profile in profiles.items() if profile.get('routing_enabled') is True)
    if not requested or list(requested) == ['all']:
        chosen = enabled
    else:
        chosen = list(dict.fromkeys(requested))
        if set(chosen) - profiles.keys():
            raise ValueError('INVALID_AGENT_SELECTION')
    if not chosen or len(chosen) > MAX_SNAPSHOT_AGENTS:
        raise ValueError('INVALID_AGENT_SELECTION')
    omitted = [{'agent': name, 'reason': 'NOT_SELECTED_BY_PARENT'} for name in enabled if name not in chosen]
    return chosen, omitted


def qualify_snapshot_workspaces(fleet, profiles, workspace):
    """Keep workspace-bound profiles out of every later quota/readiness path unless admitted."""
    admitted, omitted = {}, []
    for agent, profile in profiles.items():
        if profile.get('data_boundary') is None:
            admitted[agent] = profile
            continue
        try:
            if fleet.is_beam_profile(profile):
                # Fleet owns the trial expiry and workspace rules; do not mirror its timestamp here.
                fleet.qualify_beam_workspace(agent, profile, workspace)
            else:
                fleet.validate_profile_workspace_boundary(profile, workspace)
        except Exception:
            # Keep diagnostics local and avoid leaking workspace paths into JEV input.
            omitted.append({'agent': agent, 'reason': 'WORKSPACE_OR_TRIAL_NOT_ADMITTED'})
        else:
            admitted[agent] = profile
    return admitted, omitted


def make_snapshot(fleet, profiles, quotas, readiness, task, *, capacity=1,
                  active_assignments=(), captured_at=None, context_windows=None):
    if type(capacity) is not int or not 1 <= capacity <= 32:
        raise ValueError('INVALID_CAPACITY')
    candidates, excluded, seen = [], [], set()
    import model_tiers
    for agent, profile in sorted(profiles.items()):
        if funding(profile) is None:
            excluded.append({'agent': agent, 'reason': 'BILLING_UNDECLARED'})
            continue
        quota = quotas.get(agent, {})
        gate_rows = fleet.routing_v2.quota_hard_gates(profile, quota)
        quota_failures = [g['code'] for g in gate_rows if not g['passed']]
        remaining, observed, fresh = quota_numbers(quota)
        pool_state = model_tiers.availability_state(quota)
        models = list(dict.fromkeys(profile.get('models', {}).values()))
        for mode in dict.fromkeys((task['mode'], 'review', 'advisory')):
            required = task.get('required_tools', []) if mode == task['mode'] else ['filesystem-read']
            for model in models:
                try:
                    execution = fleet.routing_v2.resolve_execution_profile(
                        agent, profile, complexity='medium', mode=mode,
                        required_tools=required, model_override=model)
                except (ValueError, fleet.routing_v2.RoutingV2Error):
                    continue
                key = (agent, model, mode, execution.get('execution_profile_id'))
                if key in seen:
                    continue
                seen.add(key)
                failures = list(quota_failures)
                if pool_state == 'recheck-required':
                    failures.append('pool-recheck-required')
                if execution.get('lifecycle') not in ('ACTIVE', 'LIMITED', 'CANARY'):
                    failures.append('lifecycle')
                ready = readiness.get(agent)
                if ready is not None and ready.get('ready') is not True:
                    failures.append('runtime-readiness')
                enabled = profile.get('routing_enabled') is True and not failures
                exhausted = 'quota-not-exhausted' in quota_failures
                rows_for_agent = [a for a in active_assignments if a.get('agent') == agent]
                slot_limit = profile.get('advisor_capacity')
                slots = max(0, slot_limit - len(rows_for_agent)) if type(slot_limit) is int else None
                candidate = {
                    'agent': agent, 'model': model,
                    'lineage': execution.get('model_provider_id', ''),
                    'enabled': enabled, 'training_allowed': profile.get('training_allowed') is True or 'contributor' in model.lower(),
                    'billing': funding(profile), 'tools': execution.get('tools', []),
                    # Fleet priors are 0..100; the policy uses normalized 0..1.
                    'modes': [mode], 'fit': {k: v / 100.0 for k, v in profile.get('fit', {}).items() if number(v) and 0 <= v <= 100},
                    'status': 'exhausted' if (exhausted and pool_state != 'recheck-required') or pool_state == 'exhausted' else (
                        'available' if ready and ready.get('ready') is True and pool_state == 'available' else 'unknown'),
                    'quota_remaining': remaining, 'quota_observed_at': observed,
                    'quota_reset_at': quota.get('short_window_reset_at') or quota.get('weekly_reset_at'),
                    'context_window_tokens': execution.get('context_window_tokens'),
                    'capacity_remaining': slots,
                }
                if candidate['context_window_tokens'] is None and agent == 'codex':
                    candidate['context_window_tokens'] = (context_windows or {}).get(model)
                model_class = execution.get('model_class')
                candidate['model_class'] = (model_class if type(model_class) is str and
                                            model_class in {'mythos', 'opus', 'sonnet', 'luna'} else 'unknown')
                rank = execution.get('coding_rank')
                candidate['coding_rank'] = rank if number(rank) and rank > 0 else None
                # Keep material Fleet execution identity local; it is not sent to JEV.
                candidate['_execution_identity'] = {k: execution.get(k) for k in
                                                    ('adapter', 'execution_profile_id',
                                                     'runtime_model_id', 'reasoning_level',
                                                     'model_provider_id')}
                candidate['_failures'] = failures
                candidates.append(candidate)
    # Merge modes ONLY for otherwise identical capabilities. Never give a
    # read-only execution profile the tools of its write-enabled sibling.
    merged = {}
    for candidate in candidates:
        key = json.dumps({k: v for k, v in candidate.items() if k not in ('modes', '_failures')}, sort_keys=True)
        if key not in merged:
            merged[key] = candidate
        else:
            merged[key]['modes'] = sorted(set(merged[key]['modes'] + candidate['modes']))
    candidates = list(merged.values())
    # Tier routing metadata (model_tiers.py): tier, decayed recent-use share within the tier and
    # Claude/Codex conservation below Director. Missing catalog/usage never blocks advice.
    catalog, usage = None, {}
    if os.environ.get("AGENT_GRAPH_TIER_ROUTING", "1") != "0":
        try:
            import model_tiers
            catalog = model_tiers.load_catalog()
            usage = model_tiers.recent_usage(catalog=catalog)
        except Exception as exc:  # advice degrades visibly; the bridge still enforces DENY
            excluded.append({'id': '*', 'agent': '*', 'reason': 'MODEL_CATALOG_UNAVAILABLE:' + type(exc).__name__})
    for candidate in candidates:
        if catalog is None:
            continue
        scored = model_tiers.score_candidate({"agent": candidate["agent"], "model": candidate["model"], "score": 0},
                                             role="maker", task={}, catalog=catalog, usage=usage)
        entry = model_tiers.lookup(catalog, candidate["agent"], candidate["model"])
        candidate["tier"] = entry["tier"] if entry else None
        # Unknown availability must not acquire a balancing advantage from a
        # low call count. The advisor treats this missing signal as neutral.
        recent_share = (scored.get("recent_share")
                        if entry and candidate['status'] == 'available' else None)
        if number(recent_share) and 0 <= recent_share <= 1:
            candidate["recent_share"] = recent_share
        candidate["conserve"] = bool(scored.get("conserve_penalty"))
    for candidate in candidates:
        identity = {k: candidate[k] for k in
                    ('agent', 'model', 'lineage', 'tools', 'context_window_tokens',
                     'model_class', 'coding_rank')}
        identity['execution'] = candidate['_execution_identity']
        candidate['id'] = 'c' + digest(identity)[:32]
        candidate.pop('_execution_identity')
        failures = candidate.pop('_failures')
        if failures:
            excluded.append({'id': candidate['id'], 'agent': candidate['agent'],
                             'reason': ','.join(failures)})
    # Preserve only the coordination metadata the pure policy understands.
    assignments = [{k: a[k] for k in ('candidate_id', 'lineage', 'scope', 'resources') if k in a}
                   for a in active_assignments]
    return {'schema_version': 1, 'captured_at': captured_at or timestamp(),
            'candidates': candidates, 'active_assignments': assignments, 'capacity': capacity,
            'local_diagnostics': excluded, 'readiness_checked': sorted(readiness)}


def apply_advice(todo, task_id, report, *, parent_review_note=''):
    """Produce a new plan, retaining every requirement and execution boundary."""
    if report.get('requires_parent_review') is not False and not parent_review_note.strip():
        raise ValueError('PARENT_REVIEW_REQUIRED')
    result = copy.deepcopy(todo)
    matches = [task for task in result['tasks'] if task['id'] == task_id]
    if len(matches) != 1:
        raise ValueError('TASK_NOT_UNIQUE')
    row = matches[0]
    for role in ('maker', 'reviewer'):
        choices = report.get(role)
        if not isinstance(choices, list) or not choices:
            raise ValueError('NO_QUALIFIED_PAIR')
        row[role] = [{k: c[k] for k in ('agent', 'model', 'score', 'reason')} for c in choices]
    if report['maker'][0].get('lineage') == report['reviewer'][0].get('lineage'):
        raise ValueError('NO_QUALIFIED_PAIR')
    if parent_review_note.strip():
        row['routing_parent_review'] = parent_review_note.strip()
    # Never change scopes, tests, dependencies, max_parallel or approvals from an LLM answer.
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    snap = subs.add_parser('snapshot')
    snap.add_argument('--task', required=True)
    snap.add_argument('--agents', nargs='+', default=None,
                      help="Candidate profiles; omit or 'all' for every routing-enabled profile. "
                           'Omitted enabled profiles are listed in local_diagnostics.')
    snap.add_argument('--funding', required=True, help='Local agent -> subscription/local/api declaration JSON')
    snap.add_argument('--workspace', required=True)
    snap.add_argument('--capacity', type=int, default=1)
    snap.add_argument('--assignments')
    snap.add_argument('--check-readiness', action='store_true', help='Probe only named CLI candidates; no task content is sent')
    snap.add_argument('--output', required=True)
    advise = subs.add_parser('advise')
    advise.add_argument('--task', required=True)
    advise.add_argument('--snapshot', required=True)
    advise.add_argument('--todo', help='Original TODO for applicable, bound advice')
    advise.add_argument('--task-id', help='Selected original TODO node')
    advise.add_argument('--reuse-report', help='Fresh, exactly bound report to copy without another model call')
    advise.add_argument('--max-cost-usd', type=float, help='Required for a new consultation; unused with --reuse-report')
    advise.add_argument('--output', required=True)
    apply = subs.add_parser('draft-todo')
    apply.add_argument('--todo', required=True)
    apply.add_argument('--task-id', required=True)
    apply.add_argument('--report', required=True)
    apply.add_argument('--task', required=True)
    apply.add_argument('--snapshot', required=True)
    apply.add_argument('--output', required=True)
    apply.add_argument('--parent-review-note', default='', help='Planner rationale acknowledging advisory uncertainty; never owner authorization')
    args = parser.parse_args(argv)
    try:
        if Path(args.output).exists():
            raise ValueError('OUTPUT_EXISTS')
        if args.command == 'snapshot':
            import todo_fleet
            fleet = todo_fleet.load_fleet()
            profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
            declared = load(args.funding)
            chosen, omitted = select_profiles(profiles, args.agents)
            profiles = {name: {**profiles[name], 'advisor_billing': declared.get(name)} for name in chosen}
            profiles, boundary_omitted = qualify_snapshot_workspaces(
                fleet, profiles, Path(args.workspace))
            quotas, _ = fleet.current_quota_views(fleet.state_root(None), profiles)
            readiness = fleet.live_readiness(profiles, cwd=Path(args.workspace))[0] if args.check_readiness else {}
            result = make_snapshot(fleet, profiles, quotas, readiness, load(args.task), capacity=args.capacity,
                                   active_assignments=load(args.assignments) if args.assignments else (),
                                   context_windows=local_context_windows(profiles))
            # Local only: public_snapshot() never sends diagnostics to JEV.
            result['local_diagnostics'] = [*omitted, *boundary_omitted, *result['local_diagnostics']]
        elif args.command == 'advise':
            from jev_client import JevError, build_request, request_decision, validate_normalized_decision
            from jev_policy import recommend
            task, snapshot = load(args.task), load(args.snapshot)
            public = public_snapshot(snapshot)
            build_request(task, public)
            bound = args.todo is not None or args.task_id is not None
            if bound and (not args.todo or not args.task_id):
                raise ValueError('BOUND_TODO_REQUIRED')
            plan = load(args.todo) if bound else None
            binding = binding_for(plan, args.task_id, task, snapshot) if bound else None
            current = datetime.now(timezone.utc)
            fresh_time(snapshot['captured_at'], now=current)
            if args.reuse_report:
                if not bound:
                    raise ValueError('BOUND_TODO_REQUIRED')
                result = load(args.reuse_report)
                verify_report(result, plan, args.task_id, task, snapshot, now=current)
            else:
                if args.max_cost_usd is None:
                    raise ValueError('BUDGET_REQUIRED')
                created = timestamp()
                created_dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
                try:
                    guard_metadata(task)
                    # Diagnostics may contain paths or provider exceptions; never transmit them.
                    guard_metadata(public)
                    decision = request_decision(task, public, os.environ.get('OPENROUTER_API_KEY', ''),
                                                max_cost_usd=args.max_cost_usd)
                    validate_normalized_decision(decision, public)
                    recommendation = recommend(task, {**public, 'schema_version': 1}, decision, now=created_dt)
                    consultation_status, error_code = 'AVAILABLE', None
                except JevError as exc:
                    decision = None
                    consultation_status, error_code = 'UNAVAILABLE', exc.code
                    recommendation = unavailable_recommendation(task, snapshot, now=created_dt)
                except Exception:
                    # Unexpected provider/transport/parser failures remain explicit and
                    # cannot print untrusted response text or trigger another model.
                    decision = None
                    consultation_status, error_code = 'UNAVAILABLE', 'advisor_error'
                    recommendation = unavailable_recommendation(task, snapshot, now=created_dt)
                result = {'schema_version': 1, 'created_at': created, 'decision': decision,
                          'binding': binding, 'consultation_status': consultation_status,
                          'consultation_error_code': error_code, 'recommendation': recommendation,
                          'execution': 'NOT_DISPATCHED'}
        else:
            report = load(args.report)
            plan = load(args.todo)
            task, snapshot = load(args.task), load(args.snapshot)
            recommendation = verify_report(report, plan, args.task_id, task, snapshot)
            result = apply_advice(plan, args.task_id, recommendation, parent_review_note=args.parent_review_note)
            from todo_graph import validate
            validate(result)
        write_new(args.output, result)
        unavailable = args.command == 'advise' and result['consultation_status'] == 'UNAVAILABLE'
        print(json.dumps({'status': 'UNAVAILABLE' if unavailable else 'WRITTEN',
                          'output': str(Path(args.output).resolve()), 'execution': 'NOT_DISPATCHED',
                          **({'code': result['consultation_error_code']} if unavailable else {})}))
        return 1 if unavailable else 0
    except Exception as exc:
        # No provider text, key, input body or filesystem content in diagnostics.
        raw_code = getattr(exc, 'code', str(exc))
        code = raw_code.upper() if isinstance(raw_code, str) and re.fullmatch(r'[A-Za-z_0-9]{3,80}', raw_code) else 'ADVISOR_FAILED'
        print(json.dumps({'status': 'ERROR', 'code': code}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
