"""One explicit, content-free Muse/CodeBuddy canary on the existing native lane.

No task, recursive execute/preflight, user workspace, retry or cleanup. Unknown
transport, model, completion or tool evidence never clears a cached refusal.
"""
import json
from pathlib import Path
import uuid

import process_supervision
import quota_availability as qa
from terminal_handoff import muse_terminal_text

PROMPT = 'Reply OK only. Do not use any tools or read any files.'
TIMEOUT_S = 30


def tool_events(line):
    """Fail closed on corrupt JSON and on any native tool/permission event."""
    try:
        value = json.loads(line)
    except (ValueError, RecursionError) as exc:
        if line.lstrip().startswith(('{', '[')):
            raise process_supervision.ToolEventUnreadable('Invalid native probe event') from exc
        return set()
    found = set()
    pending = [value]
    visited = 0
    while pending:
        visited += 1
        if visited > 10000:
            raise process_supervision.ToolEventUnreadable('Oversized native probe event')
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            kind = str(item.get('payload_type') or item.get('type') or '')
            if (set(kind.split('.')) & {'tool', 'permission'}
                    or kind in {'tool_use', 'tool_call', 'tool_result'} or item.get('role') == 'tool'
                    or item.get('tool_calls') or item.get('toolCalls')):
                found.add('native-probe-tool')
            pending.extend(v for v in item.values() if isinstance(v, (dict, list)))
    return found


def completion(fleet, adapter, model, result):
    raw = result.get('stdout', '')
    if (result.get('timed_out') or type(result.get('exit_code')) is not int or result['exit_code'] != 0
            or result.get('tool_budget_exceeded') or type(result.get('model_tool_calls')) is not int
            or result['model_tool_calls'] != 0):
        raise ValueError('Native canary did not complete with zero tools')
    if len(raw.encode('utf-8')) > 1024 * 1024:
        raise ValueError('Native canary output exceeds bound')
    if any(tool_events(line) for line in raw.splitlines() if line.strip()):
        raise ValueError('Native canary used tools')
    if adapter == 'muse':
        identity = fleet.muse_model_identity(raw, expected_model=model)
        text, reason, error = muse_terminal_text(raw)
        if error or reason not in {'completed', 'success', 'succeeded', 'done'} or text != 'OK':
            raise ValueError('Missing exact native Muse completion')
    else:
        identity = fleet.codebuddy_model_identity(raw, expected_runtime_model=model)
        events = json.loads(raw)
        events = events if isinstance(events, list) else [events]
        terminals = [e for e in events if isinstance(e, dict) and e.get('type') == 'result']
        if (len(terminals) != 1 or terminals[0].get('is_error')
                or terminals[0].get('subtype', 'success') != 'success'
                or terminals[0].get('result') != 'OK'):
            raise ValueError('Missing exact native CodeBuddy completion')
    if identity.get('status') != 'verified':
        raise ValueError('Native canary model identity unverified')
    return {'status': 'available', 'source': 'native-cli-check', 'model': model,
            'model_identity': 'verified', 'probe_tool_calls': 0}


def native_probe(fleet, profile, root):
    adapter = profile.get('adapter')
    if adapter not in {'muse', 'codebuddy'}:
        raise ValueError('Unsupported native recheck adapter')
    executable = fleet.resolve_executable(profile)
    if not executable:
        raise ValueError('Native recheck executable unavailable')
    model = profile['models']['low']
    if adapter == 'muse':
        fleet.require_safe_muse_model(model)
        fleet.require_muse_native_executable(executable)
    elif model != fleet.CODEBUDDY_PINNED_MODEL:
        raise ValueError('CodeBuddy recheck requires exact pinned model')
    attempt = Path(root) / 'availability-probes' / (adapter + '-' + uuid.uuid4().hex)
    workspace = attempt / 'empty-workspace'
    workspace.mkdir(parents=True)
    env = fleet.profile_runtime_environment(profile, inherit_api_keys=False, native_subagent_limit=0)
    if adapter == 'muse':
        env = fleet.harden_muse_environment(env)
        env = fleet.muse_sandbox_temp_environment(env, workspace, mode='advisory')
    else:
        env = fleet.harden_codebuddy_environment(env, attempt)
    command, stdin_text, _ = fleet.build_command(
        adapter, executable, prompt=PROMPT, workspace=workspace, mode='advisory',
        model=model, complexity='low', timeout=TIMEOUT_S, run_dir=attempt,
        native_subagent_limit=0, enable_web=False, max_model_tool_calls=1,
        profile=profile, reasoning_level='low',
    )
    if adapter == 'codebuddy':
        command[command.index('--tools') + 1] = ''
        command[command.index('--max-turns') + 1] = '1'
    # Existing supervisor launches CNO and stops on the first tool event. Muse
    # retains its native never-approve/read-only sandbox and no personal context.
    result = process_supervision.run(
        command, cwd=workspace, env=env, stdin_text=stdin_text,
        timeout=TIMEOUT_S, tool_limit=0, tool_events=tool_events,
    )
    denial = qa.rejection(adapter, result.get('stderr', ''), result.get('exit_code'),
                          stdout=result.get('stdout', ''))
    if denial:
        return denial
    return completion(fleet, adapter, model, result)
