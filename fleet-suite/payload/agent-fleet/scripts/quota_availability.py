"""Small per-pool availability cache, separate from numeric quota freshness.

Pools with a probe require fresh evidence to clear an exhaustion. Pools without
a probe fail open at the native reset or after a fixed refusal backoff; the next
real run supplies new evidence instead of renewing an uncheckable block forever.
"""
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import time

import adaptive


def stamp(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def seconds(value):
    try:
        return adaptive.parse_timestamp(str(value)).timestamp()
    except (ValueError, TypeError, adaptive.AdaptiveError):
        return 0.0


def cache_path(root, pool):
    key = hashlib.sha256(pool.encode('utf-8')).hexdigest()
    return Path(root) / 'availability' / (key + '.json')


def read_cache(path):
    value, reason = adaptive.read_state_json(path, max_bytes=16384)
    if reason == 'missing':
        return {}
    if reason or not _valid_cache(value):
        # Missing proof is unknown, never a pool-wide or Fleet-wide veto.
        # Leave the file alone; the next real observation replaces it.
        return {'status': 'unknown', 'source': 'availability-cache',
                'reason': 'corrupt-cache-ignored', 'cache_read_reason': 'corrupt-cache-ignored'}
    return value


def _valid_cache(value):
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1 or not isinstance(value.get('status'), str)
            or value['status'] not in {'available', 'exhausted', 'recheck-required', 'unknown'}):
        return False
    def valid_fields(row, depth=0):
        if not isinstance(row, dict) or depth > 8:
            return False
        for key in ('status', 'source', 'reason', 'scope', 'clear_requires', 'recheck_adapter',
                    'probe_day', 'cache_read_reason', 'model', 'error_type'):
            if row.get(key) is not None and not isinstance(row[key], str):
                return False
        for key in ('observed_at', 'started_at', 'recheck_at', 'expires_at', 'reset_at', 'last_attempt_at'):
            if row.get(key) is not None:
                if not isinstance(row[key], str):
                    return False
                if row[key]:
                    try:
                        adaptive.parse_timestamp(row[key])
                    except (ValueError, OverflowError, adaptive.AdaptiveError):
                        return False
        for key in ('block_step', 'probe_count'):
            if key in row and (type(row[key]) is not int or row[key] < 0):
                return False
        for key in ('used', 'limit', 'window_hours'):
            number = row.get(key)
            if number is not None and (isinstance(number, bool) or not isinstance(number, (int, float))
                                       or not math.isfinite(number)):
                return False
        for key in ('last_block', 'last_good'):
            if row.get(key) is not None and not valid_fields(row[key], depth + 1):
                return False
        if row.get('covered_scopes') is not None and (not isinstance(row['covered_scopes'], list)
                or any(not isinstance(scope, str) for scope in row['covered_scopes'])):
            return False
        if row.get('windows') is not None and (not isinstance(row['windows'], list)
                or any(not valid_fields(window, depth + 1) for window in row['windows'])):
            return False
        return True
    try:
        return valid_fields(value)
    except (OSError, ValueError, OverflowError, adaptive.AdaptiveError):
        return False


def _cache_diagnostic(value, previous):
    if previous.get('cache_read_reason'):
        value['cache_read_reason'] = previous['cache_read_reason']
    return value


def local_ledger_evidence(view):
    # A real native refusal is independent of any advisory ledger attached to it.
    if view.get('source') == 'native-provider-error':
        return False
    last_block = view.get('last_block')
    return (str(view.get('source', '')).strip() == 'local-token-ledger'
            or view.get('evidence_source') == 'local-token-ledger'
            or (view.get('source') == 'availability-check' and isinstance(last_block, dict)
                and local_ledger_evidence(last_block)))


def is_exhaustion(view):
    value = view.get('reported_headroom', view.get('headroom'))
    return (not local_ledger_evidence(view)
            and view.get('authority') == 'authoritative' and isinstance(value, (int, float))
            and not isinstance(value, bool) and value <= 0 and not view.get('quota_conflict'))


def positive(value):
    # NaN/Infinity is not a trustworthy reading and never shows allowance.
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def credit_allowance(prior):
    # A pool without a native check (CodeBuddy) recovers from a credit refusal
    # only through a later authoritative reading that shows allowance left:
    # remaining credits when recorded, otherwise a positive plan fraction.
    if local_ledger_evidence(prior):
        return False
    headroom = prior.get('headroom')
    credits = prior.get('credits_remaining')
    if headroom is not None and not positive(headroom):
        return False
    if credits is not None:
        return positive(credits)
    return positive(headroom)


def _qwen_reset_at(observation, match):
    """Resolve a Qwen yearless 'MM-DD HH:MM:SS UTC' reset against the
    observation clock. Only December -> January implies the following year;
    an expired date in another month must not create a year-long subscription ban.
    Absent, malformed, or out-of-range components stay None so the caller
    cannot invent a deadline.
    """
    try:
        month = int(match.group(1))
        day = int(match.group(2))
        hour = int(match.group(3))
        minute = int(match.group(4))
        second = int(match.group(5))
    except (ValueError, TypeError, IndexError):
        return None
    if not (isinstance(month, int) and isinstance(day, int) and isinstance(hour, int)
            and isinstance(minute, int) and isinstance(second, int)):
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23
            and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    if (not isinstance(observation, (int, float)) or isinstance(observation, bool)
            or not math.isfinite(observation)):
        return None
    try:
        now_dt = dt.datetime.fromtimestamp(observation, dt.timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError):
        return None
    try:
        year = now_dt.year + int(now_dt.month == 12 and month == 1)
        candidate = dt.datetime(year, month, day, hour, minute, second,
                                tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    try:
        return stamp(candidate.timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def rejection(adapter, stderr, exit_code, *, stdout=''):
    if adapter == 'muse':
        # Native terminal stderr, never model prose/stdout or a generic 429.
        # Muse reports the subscription refusal separately from its JSONL.
        match = re.search(
            r"(?m)^run ended with Failed: API error 429(?: \[request_id=[A-Za-z0-9-]+\])?: "
            r"Subscription quota exhausted\.(?: Your usage window resets at (\S+)\.)?"
            r" \(rate_limit_error\)[ \t\r]*$", stderr or '') if exit_code != 0 else None
        if not match:
            return None
        reset = None
        if match[1]:
            try:
                parsed = dt.datetime.fromisoformat(match[1].replace('Z', '+00:00'))
                if parsed.tzinfo is not None:
                    reset = stamp(parsed.timestamp())
            except (ValueError, OverflowError, OSError):
                pass
        return {'status': 'exhausted', 'scope': 'subscription', 'source': 'native-provider-error',
                'reason': 'subscription-quota-exhausted', 'reset_at': reset, 'recheck_adapter': 'muse'}
    if adapter == 'claude' and exit_code != 0:
        # A provider failure envelope is distinct from arbitrary model prose.
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if (isinstance(event, dict) and event.get('type') == 'result'
                    and event.get('is_error') is True and event.get('api_error_status') == 429
                    and event.get('terminal_reason') == 'api_error'
                    and isinstance(event.get('result'), str)
                    and event['result'].startswith("You're out of usage credits.")):
                return {'status': 'exhausted', 'scope': 'credits', 'source': 'native-provider-error',
                        'reason': 'usage-credits-exhausted', 'reset_at': None,
                        'applicability': 'current-request-only'}
        return None
    if adapter == 'codebuddy':
        # CodeBuddy reports the refusal on stderr and still exits 0, so the exit
        # code cannot gate this branch the way it does for claude and kimi.
        # Measured 2026-09-25: exit_code=0, stdout empty, stderr carries the line.
        if re.search(r"(?im)^429 Credits exhausted\b", stderr or ''):
            return {'status': 'exhausted', 'scope': 'credits', 'source': 'native-provider-error',
                    'reason': 'plan-credits-exhausted', 'reset_at': None, 'recheck_adapter': 'codebuddy'}
        return None
    if adapter == 'grok':
        # The exact line Fleet's Grok ACP adapter writes when the native CLI
        # stops on HTTP 402 before prompt execution (grok_acp.quota_exhaustion_error);
        # anchored at a line start so model prose quoting it does not match.
        if re.search(r"(?m)^Grok quota exhausted: the native CLI reported "
                     r"'Grok Build usage balance exhausted' \(HTTP 402\)", stderr or ''):
            return {'status': 'exhausted', 'scope': 'credits', 'source': 'native-provider-error',
                    'reason': 'grok-build-balance-exhausted-http402', 'reset_at': None}
        return None
    if adapter == 'qwen':
        # Native terminal result envelope only — assistant prose, streaming
        # tool events, generic HTTP 429 / throttling text and successful
        # envelopes never count. The native execution is the only proof of
        # this monthly refusal; a later /usages read may disagree with the
        # provider for the same window.
        if type(exit_code) is not int or exit_code == 0:
            return None
        terminals = []
        for line in (stdout or '').splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except (ValueError, RecursionError):
                continue
            if isinstance(event, dict) and event.get('type') == 'result':
                terminals.append(event)
        if len(terminals) != 1:
            return None
        terminal = terminals[0]
        if (terminal.get('subtype') != 'error_during_execution'
                or terminal.get('is_error') is not True):
            return None
        error = terminal.get('error')
        if not isinstance(error, dict):
            return None
        message = error.get('message')
        if not isinstance(message, str):
            return None
        # The exact 1-month token-plan phrasing and the insufficient_quota:429
        # cause must both appear — generic 429, weekly or daily envelopes are
        # left alone so unstyled throttling cannot void an otherwise usable
        # pool.
        if not message.startswith('Quota exhausted: Your token-plan 1-month quota has been exhausted.'):
            return None
        if not re.search(r'\binsufficient_quota: 429\b', message):
            return None
        reset = None
        resets = re.findall(r'The quota will reset at ([^\r\n.]+)\.', message)
        if resets and len(set(resets)) == 1:
            match = re.fullmatch(r'(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}) UTC', resets[0])
            if match:
                reset = _qwen_reset_at(time.time(), match)
        # The monthly refusal blocks routing until the provider's known reset
        # (no-probe half-open), with the unrelated-numeric-headroom rule
        # already protecting monthly in check(); clear_requires stays absent
        # because qwen pools have no native CLI recheck lane.
        return {'status': 'exhausted', 'scope': 'monthly',
                'source': 'native-provider-error',
                'reason': 'usage-limit', 'reset_at': reset}
    # Only a native error line, not model prose, prompts or arbitrary tool output.
    if adapter != 'kimi' or exit_code == 0:
        return None
    # auth_error is quota proof only for this anchored HTTP 403 5-hour line.
    # The phrase names the short window; it is not time remaining and not a reset.
    if re.search(r"(?im)^error: failed to run prompt: provider\.auth_error: 403 You've reached your 5-hour usage limit\b", stderr or ''):
        return {'status': 'exhausted', 'scope': 'short-window', 'source': 'native-provider-error',
                'reason': 'usage-limit', 'reset_at': None, 'clear_requires': 'native-cli-check'}
    match = re.search(r"(?im)^error: failed to run prompt: provider\.api_error: 403 You've reached your (monthly|weekly|daily) usage limit\b", stderr)
    if not match:
        return None
    # Native execution proved the refusal, so only native execution may clear it:
    # a later /usages read can disagree with the provider for the same window.
    return {'status': 'exhausted', 'scope': match[1].lower(), 'source': 'native-provider-error',
            'reason': 'usage-limit', 'reset_at': None, 'clear_requires': 'native-cli-check'}


def record_rejection(root, pool, result, *, lock_factory, now=None, concurrent_successes=()):
    # A model-specific credit refusal does not establish exhaustion of other
    # models on the same account. Preserve it in the run, not the shared pool.
    if result.get('applicability') == 'current-request-only':
        return dict(result, persisted=False)
    if local_ledger_evidence(result):
        return dict(result, status='unknown', persisted=False)
    now = time.time() if now is None else now
    path = cache_path(root, pool)
    lock = lock_factory(path.with_suffix('.lock'), timeout=10)
    lock.acquire()
    try:
        recheck = now + 300
        if (result.get('scope') in {'subscription', 'monthly'}
                and seconds(result.get('reset_at')) > now):
            # Retain the provider's known refusal until its own reset. Pools
            # with a probe still require fresh evidence at that deadline.
            recheck = seconds(result['reset_at'])
        # Repeated refusals within two days lengthen a costly pool's backoff.
        # A no-probe half-open retry retains its step even after a long idle;
        # other old or unrelated history starts again at the shortest step.
        previous = read_cache(path)
        previous_at = seconds(previous.get('observed_at'))
        previous_start = seconds(previous.get('started_at'))
        # Persistence can finish out of order. A later completion of a run
        # started before this refusal is not newer availability proof. Legacy
        # caches lack started_at; only an exact trusted ledger match supersedes.
        superseded = (previous.get('status') == 'available' and previous.get('source') == 'native-run'
                      and ((0 < previous_start <= now <= previous_at)
                           or previous_at in concurrent_successes))
        if not superseded and (previous_at > now
                or (seconds(previous.get('observed_at')) == now
                    and (previous.get('status') in {'exhausted', 'recheck-required'}
                         or previous.get('reason') in {'half-open-after-backoff', 'reset-passed-no-probe'}))):
            return previous  # Re-import or older concurrent evidence cannot win.
        step = 0
        if ('block_step' in previous and previous.get('status') != 'available'
                and (now - seconds(previous.get('observed_at')) < 2 * 86400
                     or previous.get('reason') == 'half-open-after-backoff')):
            step = min(_block_step(previous) + 1, len(COSTLY_BACKOFF_S) - 1)
        value = dict(result, schema_version=1, observed_at=stamp(now), recheck_at=stamp(recheck),
                     block_step=step)
        value['expires_at'] = value['recheck_at']
        for key in ('probe_day', 'probe_count'):
            if key in previous:
                value[key] = previous[key]
        _cache_diagnostic(value, previous)
        adaptive.atomic_write(path, value)
        return value
    finally:
        lock.release()


# A costly probe spends a real model turn (a native Grok "OK" check measured
# 15 924 tokens), so it runs only for a block whose backoff has elapsed, never
# on a preflight of an available or never-blocked pool, and at most a few
# times per UTC day. A failed or unaffordable check retains recheck-required;
# expiry schedules another bounded check, never restores dispatch without proof.
COSTLY_BACKOFF_S = (900, 3600, 21600, 86400)
COSTLY_PROBES_PER_DAY = 4
_BLOCK_KEYS = ('status', 'reason', 'scope', 'source', 'observed_at', 'reset_at')


def _utc_day(now):
    return dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime('%Y-%m-%d')


def _block_step(value):
    step = value.get('block_step', 0)
    if type(step) is not int or step < 0:
        return 0
    return min(step, len(COSTLY_BACKOFF_S) - 1)


def _costly_check(path, cached, blocked, probe, now, probes_per_day):
    if not blocked:
        # Available, unknown or absent: passive. Only new refusal evidence
        # (record_rejection or an authoritative exhaustion snapshot) reblocks.
        return cached if cached else {'status': 'unknown', 'source': 'no-cached-refusal'}
    step = _block_step(cached)
    due = max(seconds(cached.get('recheck_at')),
              seconds(cached.get('observed_at')) + COSTLY_BACKOFF_S[step])
    if due > now:
        return cached
    day = _utc_day(now)
    count = cached.get('probe_count', 0) if cached.get('probe_day') == day else 0
    if type(count) is not int or count < 0:
        count = 0
    counters = {'probe_day': day, 'probe_count': count}
    last_block = {k: cached[k] for k in _BLOCK_KEYS if k in cached}
    if probe is None or count >= probes_per_day:
        value = {'schema_version': 1, 'status': 'recheck-required', 'source': 'availability-check',
                 'reason': 'no-check-adapter' if probe is None else 'probe-budget-exhausted',
                 'observed_at': stamp(now), 'recheck_at': stamp(now + COSTLY_BACKOFF_S[step]),
                 'block_step': step, 'last_block': last_block, **counters}
        value['expires_at'] = value['recheck_at']
        _cache_diagnostic(value, cached)
        adaptive.atomic_write(path, value)
        return value
    counters['probe_count'] = count + 1
    try:
        result = probe()
        status = result.get('status')
        if status not in {'available', 'exhausted'}:
            raise ValueError('Unconfirmed availability')
        if status == 'available' and result.get('source') != 'native-cli-check':
            raise ValueError('Availability not proven by native execution')
    except Exception as exc:
        value = {'schema_version': 1, 'status': 'recheck-required', 'source': 'availability-check',
                 'reason': 'check-unavailable', 'error_type': type(exc).__name__,
                 'observed_at': stamp(now),
                 'block_step': min(step + 1, len(COSTLY_BACKOFF_S) - 1),
                 'last_block': last_block, **counters}
        value['recheck_at'] = stamp(now + COSTLY_BACKOFF_S[value['block_step']])
        value['expires_at'] = value['recheck_at']
        _cache_diagnostic(value, cached)
        adaptive.atomic_write(path, value)
        return value
    value = {k: result[k] for k in ('status', 'source', 'scope', 'reset_at', 'reason',
                                    'covered_scopes', 'probe_tokens', 'windows') if k in result}
    value.update(schema_version=1, observed_at=stamp(now), **counters)
    if status == 'available':
        value.update(block_step=0, recheck_at=stamp(now + COSTLY_BACKOFF_S[0]))
    else:
        value['block_step'] = min(step + 1, len(COSTLY_BACKOFF_S) - 1)
        recheck = now + COSTLY_BACKOFF_S[value['block_step']]
        reset = seconds(value.get('reset_at'))
        if reset > now:
            recheck = reset
        value['recheck_at'] = stamp(recheck)
    value['expires_at'] = value['recheck_at']
    _cache_diagnostic(value, cached)
    adaptive.atomic_write(path, value)
    return value


def _trusted_runtime_alias(profile, row, identity, expected):
    """Approve a configured native runtime alias for the trusted catalog model.

    Some providers expose a managed runtime alias (for example
    minimax/MiniMax-M3.1-Flash-Preview) while the run receipt records the
    catalog model (MiniMax-M3.1-Flash). Only the profile's own trusted
    execution profiles may approve that pair, and only when the catalog
    model, the runtime alias and the provider all match the trusted
    configuration exactly. A prefix, an arbitrary verified string, a foreign
    provider, a missing mapping, an unobserved alias or an identity that is
    not verified never matches, and the alias must belong to the agent's own
    configured provider so no other provider's model is approved through it.
    """
    if not isinstance(expected, str) or not expected.strip():
        return False
    model = row.get('model')
    provider = row.get('model_provider_id')
    if not isinstance(model, str) or not model.strip():
        return False
    if not isinstance(provider, str) or not provider.strip():
        return False
    if provider != profile.get('cli_provider_id'):
        return False
    execution_profiles = profile.get('execution_profiles')
    if not isinstance(execution_profiles, dict) or not execution_profiles:
        return False
    approved = False
    for entry in execution_profiles.values():
        if not isinstance(entry, dict):
            continue
        if (entry.get('model') != model
                or entry.get('runtime_model_id') != expected
                or entry.get('model_provider_id') != provider):
            continue
        approved = True
        break
    if not approved:
        return False
    # The native observation must show the approved alias itself, not a
    # neighbouring or merely asserted runtime identity.
    observed = identity.get('observed_runtime_models')
    if not isinstance(observed, list) or not observed:
        return False
    expected_key = expected.strip().casefold()
    return all(isinstance(value, str) and value.strip().casefold() == expected_key
               for value in observed)


def native_run_observation(row, agent, profile, *, now):
    """Bounded ledger metadata only; never infer a numeric quota from a run."""
    if not isinstance(row, dict) or row.get('agent') != agent:
        return None
    pool = str(profile.get('quota_pool_id') or agent)
    if row.get('quota_pool_id') not in (None, pool):
        return None
    attestation = row.get('runtime_attestation') or {}
    usage = row.get('token_usage') or {}
    if not isinstance(attestation, dict) or not isinstance(usage, dict):
        return None
    adapter = row.get('adapter') or usage.get('adapter')
    if (attestation.get('status') != 'VERIFIED' or adapter != profile.get('adapter')
            or not adapter or adapter in {'local', 'openclaw'}):
        return None
    observed = seconds(row.get('completed_at'))
    started = seconds(row.get('started_at'))
    if not 0 < started <= observed <= now:
        return None
    if row.get('run_status') == 'COMPLETED' and type(row.get('exit_code')) is int and row['exit_code'] == 0:
        identity = row.get('model_identity') or {}
        # Reuse the existing native bridge's approved exact-launch exceptions.
        # Keep the original not-applicable identity, never call it verified.
        config_provider = {'codex': 'openai', 'kimi': 'moonshot'}.get(adapter)
        config_attested = (isinstance(identity, dict)
                           and identity.get('status') == 'not-applicable'
                           and config_provider is not None
                           and row.get('model_provider_id') == config_provider
                           and identity.get('error') is None
                           and not identity.get('observed_models')
                           and not identity.get('observed_runtime_models'))
        if (not isinstance(identity, dict) or now - observed >= 86400 or row.get('timed_out') is True
                or not (identity.get('status') == 'verified' or config_attested)
                or not row.get('model')):
            return None
        expected = identity.get('expected_model', identity.get('expected_runtime_model'))
        if expected != row['model']:
            # A managed runtime alias is accepted only through the profile's
            # own trusted execution profile; an unconfigured alias never is.
            if not (identity.get('status') == 'verified'
                    and _trusted_runtime_alias(profile, row, identity, expected)):
                return None
        return {'status': 'available', 'source': 'native-run',
                'observed_at': stamp(observed), 'started_at': stamp(started)}
    # Failures must have actual native provider attempts, not a local preflight
    # or a missing identity/handoff after work. Unknown usage is never zero.
    attempts = row.get('provider_attempts')
    if (row.get('run_status') != 'FAILED' or type(row.get('exit_code')) is not int
            or row['exit_code'] == 0 or not isinstance(attempts, list) or not attempts
            or usage.get('status') != 'known' or type(usage.get('total_tokens')) is not int
            or usage['total_tokens'] != 0):
        return None
    for attempt in attempts:
        if not isinstance(attempt, dict):
            return None
        tokens = attempt.get('token_usage') or {}
        outcome = attempt.get('transport_outcome') or {}
        if (not isinstance(tokens, dict) or not isinstance(outcome, dict)
                or type(attempt.get('exit_code')) is not int or attempt['exit_code'] == 0
                or attempt.get('terminal_reason') not in {'error', 'api_error', 'failed'}
                or outcome.get('status') != 'failed'
                or tokens.get('status') != 'known' or type(tokens.get('total_tokens')) is not int
                or tokens['total_tokens'] != 0):
            return None
    denial = attempts[-1].get('quota_rejection')
    if (adapter == 'antigravity' and isinstance(denial, dict)
            and denial.get('status') == 'exhausted' and denial.get('reason') == 'usage-limit'
            and denial.get('source') == 'native-provider-attempt'
            and denial.get('scope') == 'subscription'):
        duration = denial.get('reset_after_seconds')
        if type(duration) is int and duration > 0:
            try:
                reset = stamp(observed + duration)
            except (ValueError, OverflowError, OSError):
                pass
            else:
                return {'status': 'exhausted', 'source': 'native-provider-attempt',
                        'reason': 'usage-limit', 'scope': 'subscription',
                        'reset_at': reset, 'observed_at': stamp(observed)}
    return {'status': 'recheck-required', 'source': 'native-provider-attempt',
            'reason': 'native-zero-token-failure', 'scope': 'native',
            'clear_requires': 'native-cli-check', 'observed_at': stamp(observed)}


def import_native_observations(root, profiles, records, *, lock_factory, now=None):
    """Reuse read_usage, at most the latest 64 receipts per profile; no new index."""
    now = time.time() if now is None else now
    pools = {}
    diagnostics = {}
    for agent, profile in profiles.items():
        recent = sorted((r for r in records if isinstance(r, dict) and r.get('agent') == agent),
                        key=lambda r: seconds(r.get('completed_at')), reverse=True)[:64]
        evidence = [native_run_observation(r, agent, profile, now=now) for r in recent]
        evidence = [e for e in evidence if e is not None]
        pool = str(profile.get('quota_pool_id') or agent)
        pools.setdefault(pool, []).extend(evidence)
    for pool, evidence in pools.items():
        # Success older than a refusal (including a concurrent run) never clears it.
        failure_at = max((seconds(e['observed_at']) for e in evidence
                          if e['status'] in {'exhausted', 'recheck-required'}), default=0)
        for item in sorted(evidence, key=lambda e: seconds(e['observed_at'])):
            observed = seconds(item['observed_at'])
            try:
                if item['status'] == 'available':
                    if seconds(item['started_at']) > failure_at:
                        record_native_success(root, pool, lock_factory=lock_factory,
                                              now=observed, run_started_at=item['started_at'])
                else:
                    concurrent = tuple(seconds(e['observed_at']) for e in evidence
                                       if e['status'] == 'available'
                                       and seconds(e['started_at']) <= observed <= seconds(e['observed_at']))
                    record_rejection(root, pool, item, lock_factory=lock_factory, now=observed,
                                     concurrent_successes=concurrent)
            except (OSError, ValueError):
                # A damaged/unwritable pool must not interrupt other pools.
                # The subsequent check still applies any readable refusal.
                diagnostics[pool] = {'reason': 'state-write-unavailable'}
                break
    return diagnostics


def record_native_success(root, pool, *, lock_factory, now=None, run_started_at=None):
    """A verified native run is free availability evidence for its pool.

    Records freshness even for a pool without a numeric quota collector.
    A refusal observed after this run started (a concurrent run hit HTTP 402)
    is newer evidence than this success and is kept.
    """
    now = time.time() if now is None else now
    path = cache_path(root, pool)
    lock = lock_factory(path.with_suffix('.lock'), timeout=10)
    lock.acquire()
    try:
        cached = read_cache(path)
        if local_ledger_evidence(cached):
            cached = {}
        if cached.get('status') == 'available' and seconds(cached.get('observed_at')) >= now:
            return cached
        started = seconds(run_started_at) if run_started_at is not None else now
        if seconds(cached.get('observed_at')) > started:
            return cached
        value = {'schema_version': 1, 'status': 'available', 'source': 'native-run',
                 'started_at': stamp(started),
                 'observed_at': stamp(now), 'recheck_at': stamp(now + COSTLY_BACKOFF_S[0]),
                 'expires_at': stamp(now + 86400),
                 'block_step': 0}
        for key in ('probe_day', 'probe_count'):
            if key in cached:
                value[key] = cached[key]
        _cache_diagnostic(value, cached)
        adaptive.atomic_write(path, value)
        return value
    finally:
        lock.release()


def check(root, pool, prior, *, probe=None, fallback=None, lock_factory, now=None,
          costly_probe=False, probes_per_day=COSTLY_PROBES_PER_DAY, force_recheck=False,
          recheck_adapter=None):
    now = time.time() if now is None else now
    path = cache_path(root, pool)
    lock = lock_factory(path.with_suffix('.lock'), timeout=35)
    lock.acquire()
    try:
        cached = read_cache(path)
        cache_read_reason = cached.get('cache_read_reason')
        if (local_ledger_evidence(cached)
                or (cached.get('source') == 'quota-snapshot'
                    and local_ledger_evidence(prior))):
            cached = {}  # A contaminated local estimate never vetoes dispatch.
        lifetime = 86400 if cached.get('source') == 'native-run' else 10800
        if (cached.get('status') == 'available'
                and not 0 <= now - seconds(cached.get('observed_at')) < lifetime):
            cached = dict(cached, status='unknown')
        prior_at = seconds(prior.get('observed_at'))
        cached_at = seconds(cached.get('observed_at'))
        if is_exhaustion(prior) and prior_at > cached_at:
            cached = {'schema_version': 1, 'status': 'exhausted', 'scope': 'unknown',
                      'source': 'quota-snapshot', 'observed_at': prior.get('observed_at'),
                      'recheck_at': stamp(0), 'reset_at': None, 'reason': 'usage-limit'}
            if cache_read_reason:
                cached['cache_read_reason'] = cache_read_reason
        elif (not local_ledger_evidence(prior)
              and prior.get('status') == 'fresh' and prior.get('authority') == 'authoritative'
              and prior_at > cached_at and cached.get('clear_requires') != 'native-cli-check'
              and (credit_allowance(prior) if cached.get('scope') == 'credits' and probe is None
                   else positive(prior.get('headroom'))
                   and cached.get('scope') not in {'monthly', 'daily', 'credits'})):
            cached = {'schema_version': 1, 'status': 'available', 'source': 'quota-snapshot',
                      'observed_at': prior.get('observed_at'), 'recheck_at': stamp(now + 60)}
            if cache_read_reason:
                cached['cache_read_reason'] = cache_read_reason
            adaptive.atomic_write(path, cached)
        # A monthly denial cannot be cleared by an unrelated weekly counter.
        blocked = cached.get('status') in {'exhausted', 'recheck-required'}
        if probe is None:
            if blocked:
                # Muse/CodeBuddy have an explicit availability-recheck lane;
                # passive preflight does not invoke it. Preserve their native
                # refusal rule, including legacy caches without this marker.
                explicit_adapter = recheck_adapter or cached.get('recheck_adapter') or {
                    'subscription-quota-exhausted': 'muse',
                    'plan-credits-exhausted': 'codebuddy',
                }.get(cached.get('reason'))
                reset = seconds(cached.get('reset_at'))
                # Ignore sliding legacy recheck_at values: only the refusal's
                # observation (not a read/check) starts the no-probe backoff.
                due = reset if reset > 0 else seconds(cached.get('observed_at')) + COSTLY_BACKOFF_S[_block_step(cached)]
                value = dict(cached, recheck_at=stamp(due), expires_at=stamp(due))
                if explicit_adapter in {'muse', 'codebuddy'}:
                    value['recheck_adapter'] = explicit_adapter
                    if now >= due:
                        value['status'] = 'recheck-required'
                elif now >= due:
                    value.update(status='unknown',
                                 reason='reset-passed-no-probe' if reset > 0 else 'half-open-after-backoff',
                                 last_block={k: cached[k] for k in _BLOCK_KEYS if k in cached})
                    value.pop('clear_requires', None)
                if value != cached:
                    adaptive.atomic_write(path, value)
                cached = value
            return cached if cached else {'status': 'unknown', 'source': 'no-check-adapter'}
        if costly_probe and not force_recheck:
            return _costly_check(path, cached, blocked, probe, now, probes_per_day)
        # A native refusal that /usages contradicted is cleared only natively.
        native_only = blocked and cached.get('clear_requires') == 'native-cli-check'
        if cached and seconds(cached.get('recheck_at')) > now and not force_recheck:
            return cached
        def stale(result):
            # An exhaustion whose own reset is already behind us is a stale
            # counter: it may trigger a native check, never a fresh block.
            reset = seconds(result.get('reset_at'))
            return (result.get('status') == 'exhausted' and 0 < reset <= now) or (
                result.get('status') == 'unknown'
                and result.get('source') == 'kimi-native-usage'
                and result.get('reason') == 'window-reset-requires-recheck')

        try:
            try:
                result = probe()
                if stale(result):
                    raise ValueError('Exhaustion reset already passed')
                uncovered = (blocked and result.get('status') == 'available'
                             and result.get('source') != 'native-cli-check'
                             and (cached.get('scope', 'unknown') not in result.get('covered_scopes', [])
                                  or native_only))
                if uncovered:
                    raise ValueError('Uncovered exhaustion window')
            except Exception:
                if fallback is None:
                    raise
                result = fallback()
            if result.get('status') not in {'available', 'exhausted'} or stale(result):
                raise ValueError('Unconfirmed availability')
            # The check must cover the exhausted window or prove native execution.
            if (blocked and result['status'] == 'available'
                    and result.get('source') != 'native-cli-check'
                    and (cached.get('scope', 'unknown') not in result.get('covered_scopes', [])
                         or native_only)):
                raise ValueError('Quota check does not cover exhausted window')
            value = {k: result[k] for k in ('status', 'source', 'scope', 'reset_at', 'reason', 'covered_scopes', 'probe_tokens', 'windows', 'clear_requires', 'model', 'model_identity', 'probe_tool_calls') if k in result}
            if native_only and result['status'] == 'exhausted':
                # A newer /usages exhaustion does not lift the native refusal.
                value['clear_requires'] = 'native-cli-check'
            if result['status'] == 'exhausted' and cached.get('recheck_adapter'):
                value['recheck_adapter'] = cached['recheck_adapter']
            if blocked and result['status'] == 'exhausted' and cached.get('scope') in {'monthly', 'daily'}:
                if cached['scope'] != result.get('scope'):
                    value.update(scope=cached['scope'], reset_at=cached.get('reset_at'))
            ttl = 300 if result['status'] == 'available' else 900
            reset = seconds(value.get('reset_at'))
            if result['status'] == 'exhausted' and now < reset < now + ttl:
                ttl = max(1, reset - now)
            value.update(schema_version=1, observed_at=stamp(now), recheck_at=stamp(now + ttl))
        except Exception as exc:
            value = dict(cached)
            last_good = cached.get('last_good')
            if cached.get('status') in {'available', 'exhausted'}:
                last_good = {k: v for k, v in cached.items()
                             if k not in {'last_good', 'attempt_error', 'last_attempt_at'}}
            value.update(last_good=last_good,
                         attempt_error={'reason': type(exc).__name__},
                         last_attempt_at=stamp(now))
            value.update(schema_version=1, status='recheck-required' if blocked or force_recheck else 'unknown',
                         source=value.get('source', 'availability-check'),
                         reason='check-unavailable', error_type=type(exc).__name__,
                         recheck_at=stamp(now + 60))
            value.setdefault('observed_at', stamp(now))
        value['expires_at'] = value['recheck_at']
        _cache_diagnostic(value, cached)
        adaptive.atomic_write(path, value)
        return value
    finally:
        lock.release()


def overlay(prior, state, *, require_check=False):
    if local_ledger_evidence(prior):
        from quota_collectors import normalize_inferred_bailian_ledger
        row = normalize_inferred_bailian_ledger(prior)
    else:
        row = dict(prior)
    status = state.get('status', 'unknown')
    if local_ledger_evidence(state):
        state = dict(state, status='unknown')
        status = 'unknown'
    row['availability'] = state
    row['availability_blocked'] = status in {'exhausted', 'recheck-required'} or (require_check and status != 'available')
    if status == 'available' and seconds(state.get('observed_at')) >= seconds(prior.get('observed_at')):
        # Clear only the availability veto, not invent numeric remaining tokens.
        row['hard_blocking'] = False
        row['availability_verified'] = True
    return row
