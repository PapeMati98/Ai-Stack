"""One maker/reviewer pair; reuses the product loop without graph scheduling."""
import argparse
import json
from pathlib import Path
from todo_graph import FleetAdapter, digest, read, run_task, snapshot, validate, write
import run_telemetry as telemetry
import sys


def _emit_completion_metadata(plan, result, folder):
    """Best-effort metadata hook from the normal installed skill location."""
    try:
        import importlib.util
        path = (Path.home() / ".agents" / "skills" / "delivery-audit" /
                "scripts" / "completion.py")
        if path.is_symlink() or not path.is_file():
            return None
        name = "_agent_pair_audit_completion"
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module.emit_completion(plan, result, folder, "pair")
    except Exception:
        return None


def run_pair(plan, folder, adapter=None, approved=(), *, emit_completion=False):
    validate(plan)
    if adapter is None or isinstance(adapter, FleetAdapter):
        (adapter or FleetAdapter()).validate_plan(plan)
    if len(plan['tasks']) != 1 or plan['tasks'][0].get('depends_on'):
        raise ValueError('Pair requires exactly one independent task; use TODO Graph for dependencies')
    task = plan['tasks'][0]
    if set(task.get('requires_approval', [])) - set(approved):
        return {'status': 'WAITING_APPROVAL', 'reason': 'Missing exact prior approval reference'}
    folder = telemetry.require_absolute_run_dir(folder)
    folder.mkdir(parents=True, exist_ok=True)
    key = digest(plan)
    receipt = folder / 'pair.json'
    if receipt.exists():
        prior = read(receipt)
        if prior.get('plan_digest') != key:
            prior_result = prior.get('result', {})
            if prior_result.get('status') == 'ACCEPTED':
                if prior_result.get('checks_digest') != digest(task.get('checks', [])):
                    raise ValueError('Accepted checks changed; new explicit task required')
                if prior_result.get('inputs_digest') != snapshot(plan['workspace'], task.get('inputs', [])):
                    raise ValueError('Accepted inputs changed; new explicit task required')
            raise ValueError('Existing pair belongs to another plan')
        if prior.get('result', {}).get('status') == 'ACCEPTED':
            accepted = prior['result']
            if accepted.get('artifact') != snapshot(plan['workspace'], task['scope']):
                raise ValueError('Accepted artifact changed; new explicit task required')
            if accepted.get('inputs_digest') != snapshot(plan['workspace'], task.get('inputs', [])):
                raise ValueError('Accepted inputs changed; new explicit task required')
            if accepted.get('checks_digest') != digest(task.get('checks', [])):
                raise ValueError('Accepted checks changed; new explicit task required')
            if emit_completion:
                _emit_completion_metadata(plan, accepted, folder)
            return prior['result']
        if prior.get('result', {}).get('status') in {'BLOCKED', 'WAITING_APPROVAL'}:
            return prior['result']  # No silent restart of a blocked maker.
    result = run_task(plan, task, folder / task['id'], adapter or FleetAdapter())
    if result.get('status') == 'ACCEPTED':
        result['inputs_digest'] = snapshot(plan['workspace'], task.get('inputs', []))
        result['checks_digest'] = digest(task.get('checks', []))
    write(receipt, {'plan_digest': key, 'execution': 'single-product-pair', 'result': result})
    if emit_completion and result.get('status') == 'ACCEPTED':
        _emit_completion_metadata(plan, result, folder)
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--todo', required=True)
    p.add_argument('--run-dir', required=True, help='Absolute evidence directory')
    p.add_argument('--approved', action='append', default=[])
    args=p.parse_args()
    result=run_pair(read(args.todo), args.run_dir, approved=args.approved,
                    emit_completion=True)
    print(json.dumps(result, indent=2))
    return 0 if result.get('status') == 'ACCEPTED' else 3

if __name__ == '__main__':
    raise SystemExit(main())
