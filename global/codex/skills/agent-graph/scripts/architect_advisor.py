"""Select an on-demand subscription architect from explicit Fleet model metadata.

Read-only local operation. No API calls, model dispatch or authorization.
"""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path

from escalation_policy import select_architect
from routing_advisor import load, make_snapshot, write_new, local_context_windows


def architect_snapshot(fleet, profiles, quotas, readiness, *, capacity=1, context_windows=None):
    expanded = copy.deepcopy(profiles)
    metadata = {}
    for agent, profile in expanded.items():
        by_model = {}
        for entry in profile.get('execution_profiles', {}).values():
            if not isinstance(entry, dict) or not isinstance(entry.get('model'), str):
                continue
            classification = entry.get('model_class')
            rank = entry.get('coding_rank')
            by_model.setdefault(entry['model'], set()).add((
                classification if isinstance(classification, str) else 'unknown',
                rank if type(rank) in (int, float) and rank > 0 else None))
        metadata[agent] = by_model
        # Include explicit-only architects such as Fable/Astra, not just the
        # ordinary low/medium/high worker defaults. Class/rank is never guessed.
        profile['models'] = {model: model for model in by_model}
    task = {'mode': 'advisory', 'required_tools': ['filesystem-read']}
    kwargs = {'context_windows': context_windows} if context_windows is not None else {}
    snapshot = make_snapshot(fleet, expanded, quotas, readiness, task, capacity=capacity, **kwargs)
    for candidate in snapshot['candidates']:
        values = metadata[candidate['agent']].get(candidate['model'], set())
        if len(values) == 1:
            classification, rank = next(iter(values))
        else:
            # Ambiguous profiles need repair; a stronger-looking entry cannot
            # silently lend its classification to another execution profile.
            classification, rank = 'unknown', None
        candidate.update(model_class=classification, coding_rank=rank)
    return snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', nargs='+', required=True)
    parser.add_argument('--funding', required=True)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--context-tokens', type=int, required=True)
    parser.add_argument('--capacity', type=int, default=1)
    parser.add_argument('--check-readiness', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        if Path(args.output).exists():
            raise ValueError('OUTPUT_EXISTS')
        if not Path(args.workspace).is_absolute() or not Path(args.workspace).is_dir():
            raise ValueError('WORKSPACE_INVALID')
        import todo_fleet
        fleet = todo_fleet.load_fleet()
        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        declared = load(args.funding)
        if not 1 <= len(args.agents) <= 16 or set(args.agents) - profiles.keys():
            raise ValueError('INVALID_AGENT_SELECTION')
        selected = {a: {**profiles[a], 'advisor_billing': declared.get(a)} for a in args.agents}
        quotas, _ = fleet.current_quota_views(fleet.state_root(None), selected)
        readiness = fleet.live_readiness(selected, cwd=Path(args.workspace))[0] if args.check_readiness else {}
        snapshot = architect_snapshot(fleet, selected, quotas, readiness, capacity=args.capacity,
                                      context_windows=local_context_windows(selected))
        result = select_architect(snapshot['candidates'], args.context_tokens)
        result['snapshot'] = snapshot
        result['requires_execution_preflight'] = True
        write_new(args.output, result)
        print(json.dumps({'status': result['status'], 'output': str(Path(args.output).resolve()),
                          'execution': 'NOT_DISPATCHED'}))
        return 0
    except Exception:
        print(json.dumps({'status': 'ERROR', 'code': 'ARCHITECT_ADVISOR_FAILED'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
