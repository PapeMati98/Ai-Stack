"""Read the same non-generative /usages resource as Kimi Code's /usage UI.

No inference, token refresh, credential writes, account switching or redirects.
Reference: MoonshotAI/kimi-code packages/oauth/src/managed-usage.ts.
"""
import datetime
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import quota_windows  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def number(value):
    if isinstance(value, bool):
        raise ValueError('Invalid quota number')
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError('Invalid quota number') from None
    if not math.isfinite(value) or value < 0:
        raise ValueError('Invalid quota number')
    return value


# Hours per timeUnit of the /usages window descriptor. TIME_UNIT_MONTH is
# approximate (30 days); pace only uses it as the short-window fallback when
# the scope maps to one.
WINDOW_UNIT_HOURS = {
    'TIME_UNIT_MINUTE': 1.0 / 60.0,
    'TIME_UNIT_HOUR': 1.0,
    'TIME_UNIT_DAY': 24.0,
    'TIME_UNIT_WEEK': 168.0,
    'TIME_UNIT_MONTH': 720.0,
}


def _window_hours(window):
    unit, duration = window.get('timeUnit'), window.get('duration')
    if unit not in WINDOW_UNIT_HOURS:
        return None
    try:
        return number(duration) * WINDOW_UNIT_HOURS[unit]
    except (TypeError, ValueError):
        return None


def _legacy_detail(raw):
    """(used, limit, reset) of one proto3-JSON detail row, or ValueError.

    proto3 JSON omits zero-valued fields: an absent `used` is derived from
    `remaining` when present and is otherwise the proto3 default 0 (the native
    kimi-cli usage view reads it the same way). A present but unreadable value
    and an absent `limit` stay unknown, so the whole answer stays unknown.
    """
    if not isinstance(raw, dict) or raw.get('limit') is None:
        raise ValueError('Kimi usage window has no readable limit')
    limit = number(raw['limit'])
    if 'used' in raw:
        used = number(raw['used'])
    elif 'remaining' in raw:
        used = max(0.0, limit - number(raw['remaining']))
    else:
        used = 0.0
    return used, limit, raw.get('resetTime')


# Current native /usages shape (Kimi Code 0.43 managed-usage.ts): 0..1 ratios.
# limit_month_code is a breakdown of limit_month_total, never its own window.
RATIO_WINDOWS = (('short-window', 'limit_5h', 5.0), ('weekly', 'limit_7d', 168.0),
                 ('monthly', 'limit_month_total', None))


def parse_usage(payload):
    return quota_windows.parse_kimi_payload(payload)


def account_home():
    """The Kimi Code account home the native CLI uses: KIMI_CODE_HOME or ~/.kimi-code.

    Every Kimi path (the /usages read, the native check and the contractor
    run) resolves the account here, so the quota gate judges the same
    account that will serve the run.
    """
    return Path(os.environ.get('KIMI_CODE_HOME') or (Path.home() / '.kimi-code')).expanduser()


def query(home=None, *, opener=None):
    home = Path(home) if home else account_home()
    config_path = home / 'config.toml'
    if config_path.stat().st_size > 1048576:
        raise ValueError('Kimi config too large')
    config = tomllib.loads(config_path.read_text(encoding='utf-8-sig'))
    provider = config.get('providers', {}).get('managed:kimi-code', {})
    base = os.environ.get('KIMI_CODE_BASE_URL') or provider.get('base_url')
    base = str(base).rstrip('/')
    if base not in {'https://api.kimi.com/coding/v1', 'https://api.kimi.ai/coding/v1'}:
        raise ValueError('Kimi usage endpoint is not an official managed endpoint')
    credential = home / 'credentials/kimi-code.json'
    if credential.is_symlink() or credential.stat().st_size > 65536:
        raise ValueError('Invalid Kimi credential store')
    value = json.loads(credential.read_text(encoding='utf-8-sig'))
    token = value.get('access_token')
    if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
        raise ValueError('Kimi native access token unavailable')
    expires = value.get('expires_at')
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires <= time.time():
        # The CLI owns refresh (15-minute tokens); never send a known-dead bearer.
        raise ValueError('Kimi native access token expired')
    request = urllib.request.Request(base + '/usages', headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}, method='GET')
    opener = opener or urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=8) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('Kimi usage response too large')
        return parse_usage(json.loads(raw))
    except urllib.error.HTTPError as exc:
        # Do not print response bodies or echo credentials through diagnostics.
        raise ValueError('Kimi usage HTTP ' + str(exc.code)) from None


def _reset_passed(value, now=None):
    """True only for a readable reset timestamp that is already behind us."""
    parsed = quota_windows.parse_reset(value)[0]
    if parsed is None:
        return False
    return quota_windows.is_reset_elapsed(
        parsed,
        now=datetime.datetime.fromtimestamp(time.time(), tz=datetime.timezone.utc) if now is None
        else datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc),
    )


def native_fallback(fleet, profile, root, *, home=None):
    """Native CLI check, then one /usages re-read on the token it refreshed.

    query() never refreshes the 15-minute OAuth access token; only a native
    CLI run does. Between Kimi runs the direct read therefore fails, and the
    percentage windows were lost while the service still reported them
    (2026-09-26: weekly 100/100 and 5 h 17/100 survived only from an older
    answer, recheck error ValueError). The native check refreshes the token as
    a side effect, so one re-read right after it recovers the windows. A
    native refusal is never overridden by the re-read. A native success
    proves the account served the probe, not the next request: when the later
    re-read reports a window used up whose reset is still ahead (the probe may
    have spent the last of it), that newer exhaustion wins and is cached with
    its reset. A reported reset already in the past marks a stale counter,
    so the native proof stays. Otherwise the success stays the
    native-cli-check result and only borrows the /usages windows as pace
    evidence: quota_availability.check() clears an exhaustion whose scope is
    not a /usages window (`unknown` from an authoritative snapshot, `credits`)
    only for that source. A native refusal keeps its own scope: /usages lends
    a reset only when it reports that same window spent. When /usages shows
    the refused window with capacity left, the counter is contradicted and
    the result carries clear_requires=native-cli-check, so a later /usages
    read (for example after an unrelated monthly reset) cannot clear it. The
    same holds when /usages omitted the refused window or the re-read
    failed altogether, and a spent window
    whose reset already passed never confirms a refusal. When the CLI proved
    nothing and /usages shows only such stale spent windows, the answer is
    inconclusive (the failure is raised), not a fresh exhaustion.
    """
    try:
        native, failure = native_probe(fleet, profile, root), None
    except ValueError as exc:
        # The CLI ran but proved neither availability nor a known refusal.
        native, failure = None, exc
    try:
        usage = query(home)
    except (OSError, ValueError):
        if native is None:
            raise failure from None
        if native.get('status') != 'available':
            # /usages never confirmed the refusal: a later /usages read that
            # shows the window with capacity left may not clear it.
            return dict(native, clear_requires='native-cli-check')
        return native
    if native is None:
        if usage.get('status') == 'exhausted' and _reset_passed(usage.get('reset_at')):
            # Only stale counters say exhausted and the CLI proved nothing:
            # inconclusive, not a fresh 900 s exhaustion.
            raise failure
        return usage
    if native.get('status') == 'available' and usage.get('status') == 'exhausted':
        spent = [w for w in usage.get('windows', []) if isinstance(w, dict)
                 and isinstance(w.get('used'), (int, float)) and isinstance(w.get('limit'), (int, float))
                 and w['used'] >= w['limit']] or [usage]
        live = [w for w in spent if not _reset_passed(w.get('reset_at'))]
        if live:
            # The explicit native success clears only expired counters. All
            # still-blocking windows count; one unknown reset forbids an ETA.
            resets = [(w, quota_windows.parse_reset(w.get('reset_at'))[0]) for w in live]
            unknown = next((w for w, reset in resets if reset is None), None)
            window, reset = (unknown, None) if unknown is not None else max(resets, key=lambda item: item[1])
            return dict(usage, scope=window.get('scope'),
                        reset_at=reset.isoformat().replace('+00:00', 'Z') if reset else None,
                        no_promised_recovery=reset is None,
                        reason='usage-limit-after-native-check')
    if native.get('status') == 'available':
        if usage.get('status') != 'available':
            return native
        return dict(native, windows=usage.get('windows', []),
                    covered_scopes=sorted(set(usage.get('covered_scopes', []))
                                          | set(native.get('covered_scopes', []))))
    scope = native.get('scope')
    if scope not in usage.get('covered_scopes', []):
        # The refusal names a window /usages did not report: keep its scope.
        # /usages never confirmed it, so a later /usages read that shows the
        # window with capacity left may not clear it; only a native check may.
        if usage.get('status') != 'exhausted':
            return dict(native, clear_requires='native-cli-check')
        usage.update(scope=scope, reset_at=native.get('reset_at'),
                     clear_requires='native-cli-check')
        return usage
    # A spent window whose reset has already passed is a stale counter, not a
    # confirmation of the refusal.
    confirmed = [w for w in usage.get('windows', []) if isinstance(w, dict) and w.get('scope') == scope
                 and isinstance(w.get('used'), (int, float)) and isinstance(w.get('limit'), (int, float))
                 and w['used'] >= w['limit'] and not _reset_passed(w.get('reset_at'))]
    if confirmed and usage.get('status') == 'exhausted':
        # /usages agrees on the refused window and knows when it resets.
        usage.update(scope=scope)
        return usage
    # /usages reports the refused window with capacity left. The native
    # refusal wins and keeps its own scope and reset; the contradicted
    # counter may no longer clear it, only a native inference may.
    base = usage if usage.get('status') == 'exhausted' else native
    return dict(base, status='exhausted', scope=scope, reset_at=native.get('reset_at'),
                source=native.get('source'), reason=native.get('reason'),
                clear_requires='native-cli-check')


THINKING_PART_TYPES = {'think', 'thinking', 'reasoning', 'reasoning_content'}


def assistant_text(stdout):
    """Visible reply text from Kimi stream-json, excluding thinking content.

    Only role == "assistant" records count. Thinking parts are dropped on purpose:
    a response made only of reasoning is the failure mode where the output budget
    ran out before any reply, so reasoning never counts as TEXT evidence. The
    separate token branch in `native_probe` still accepts a clean exit with
    provider-reported tokens > 0, which is billed inference whether or not text
    came back.
    """
    parts = []
    for line in str(stdout or '').splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict) or event.get('role') != 'assistant':
            continue
        content = event.get('content')
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for piece in content:
                if not isinstance(piece, dict):
                    continue
                if str(piece.get('type', '')).lower() in THINKING_PART_TYPES:
                    continue
                text = piece.get('text')
                if isinstance(text, str):
                    parts.append(text)
    return ''.join(parts).strip()


def native_probe(fleet, profile, root):
    """One bounded native prompt without task input; OAuth remains CLI-owned."""
    import quota_availability

    directory = Path(root) / 'availability-probes'
    directory.mkdir(parents=True, exist_ok=True)
    model = profile['models']['low']
    with tempfile.TemporaryDirectory(prefix='kimi-', dir=directory) as temporary:
        folder = Path(temporary)
        try:
            home = fleet.prepare_guarded_kimi_home(folder, model=model, mode='advisory',
                                                   native_subagent_limit=0, enable_web=False)
            path = home / 'config.toml'
            config = tomllib.loads(path.read_text(encoding='utf-8'))
            config.pop('permission', None)
            config['merge_all_available_skills'] = False
            # Budgets sized for a non-reasoning model starved the reasoning Kimi k3, and
            # each failure hid the next one. Measured live 2026-09-24 on Kimi CLI 0.43.1:
            #
            #   max_steps_per_turn=1  -> "max_steps_exceeded: Turn exceeded maxSteps=1";
            #                            the think step leaves no step for the reply.
            #   max_output_size=16    -> exit 1, "APIEmptyResponseError: only thinking
            #                            content ... finishReason=truncated, length";
            #                            reasoning spends the whole budget.
            #   max_output_size=64    -> exit 0, reply "OK". 128 and 256 likewise.
            #
            # 128 is twice the smallest measured working budget, because reasoning length
            # varies between calls. `max_retries_per_step` is a deprecated key that CLI
            # 0.43.1 silently ignores ("no longer used; rename it to
            # 'max_attempts_per_step'"), so the CLI was retrying up to 10 times.
            # `max_attempts_per_step=1` is one attempt and zero retries - measured.
            #
            # None of this widens what the model can do: every tool pattern stays denied
            # (deny "*", evaluated per tool call, deny before any allow rule) and there is
            # one printable turn. The native deny rule is the ONLY tool control here:
            # `max_model_tool_calls=0` below is NOT enforced for Kimi, because
            # `fleet.run_bounded_child` applies a tool limit to codex and claude only.
            config['loop_control'] = {'max_attempts_per_step': 1, 'max_steps_per_turn': 2}
            config['background'].update(print_max_turns=1, print_wait_ceiling_s=20)
            config['models'][model]['max_output_size'] = 128
            config['models'][model]['default_effort'] = 'low'
            lines = []
            fleet.emit_toml_table(lines, (), config)
            lines.extend(['[[permission.rules]]', 'decision = "deny"', 'pattern = "*"',
                          'reason = "Availability check: no tools"'])
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            skills = folder / 'empty-skills'
            skills.mkdir()
            env = fleet.profile_runtime_environment(profile, inherit_api_keys=False, native_subagent_limit=0)
            env['KIMI_CODE_HOME'] = str(home)
            env['KIMI_DISABLE_TELEMETRY'] = '1'
            result = fleet.run_bounded_child([
                fleet.resolve_executable(profile), '--model', model, '--skills-dir', str(skills),
                '--output-format', 'stream-json', '--prompt', 'Reply OK only. Do not use tools.'
            ], cwd=folder, env=env, stdin_text=None, timeout=25, adapter_id='kimi', max_model_tool_calls=0)
            denial = quota_availability.rejection('kimi', result.get('stderr', ''), result.get('exit_code', 1))
            if denial:
                return denial
            stdout = result.get('stdout', '')
            usage = fleet.adaptive.extract_token_usage(stdout, adapter='kimi')
            tokens = usage.get('total_tokens') or 0
            # Kimi CLI 0.43.1 emits no usage event in stream-json (measured 2026-09-24:
            # only system.version, the assistant message and session.resume_hint), so a
            # token-only test could never pass even while the model answered. A
            # non-empty assistant TEXT reply is direct proof that the provider served a
            # native completion. Thinking-only output does not count: that is exactly
            # the truncated-reasoning failure mode above.
            reply = assistant_text(stdout)
            # A successful native inference proves all server-enforced windows.
            if result.get('exit_code') == 0 and not result.get('timed_out') and (tokens or reply):
                row = {'status': 'available', 'source': 'native-cli-check',
                       'covered_scopes': ['weekly', 'monthly', 'daily', 'short-window']}
                if tokens:
                    row['probe_tokens'] = tokens
                else:
                    row['probe_evidence'] = 'assistant-text'
                return row
            raise ValueError('Native Kimi availability check did not succeed')
        finally:
            # Remove only our credential directory link before temporary cleanup.
            fleet.remove_guarded_kimi_credential_link(folder)
