"""Bounded Windows collector -> durable proposals. No sends, repairs or timer install.

Pre-17:00 ticks run at most one catch-up per project for the latest missed
Warsaw business day (reboot recovery, never a historical storm). Revisions
fold source content hashes computed in memory; only fixed-schema metadata
persists. Collector alarms fire once per state change via an injectable
audit hook; Telegram/Graph/model dispatch stays unconnected (parent owns it).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'automation'))
from openclaw_ops.helpdesk import Finding, HelpdeskStore, claim_schedule, derive_revision, is_due, missed_catchup_date, process_catchup, process_day, read_with_retry, record_collector_outcome, release_schedule_claim, schedule_key, warsaw_local
import helpdesk_read

PROJECTS = ('example-crm', 'example-workspace')


def _findings_from_items(project, items):
    """Admit collector metadata only. Hash leftover bodies in memory if present."""
    findings = []
    for item in items:
        metadata = {k: item[k] for k in ('issue_id', 'revision', 'status', 'priority', 'url')}
        leftover = any((item.get('title'), item.get('body'),
                        item.get('description'), item.get('comments')))
        if leftover:
            # Collectors hash at the read boundary; this path is only for
            # injectable readers that still carry content fields.
            metadata['revision'] = derive_revision(
                metadata['revision'], title=item.get('title', ''),
                body=item.get('body', item.get('description', '')),
                status=item.get('status', ''),
                severity=item.get('severity', item.get('priority', '')),
                comments=item.get('comments') if item.get('comments') is not None else None)
        findings.append(Finding(project, metadata['issue_id'], metadata['revision'],
                                'internal', json.dumps(metadata, sort_keys=True)))
    return findings


def _verified_source(reader, project):
    result = read_with_retry(reader)
    if (not isinstance(result, dict) or result.get('status') != 'OK'
            or result.get('project') != project
            or result.get('environment') != 'prod'
            or result.get('read_only') is not True
            or not isinstance(result.get('items'), list)):
        return None
    return result


def run_tick(db: Path, *, now=None, readers=None, generation: int = 1, audit_hook=None):
    now = time.time() if now is None else now
    readers = readers if readers is not None else {
        'example-crm': helpdesk_read.crm,
        'example-workspace': helpdesk_read.workspace,
    }
    report = {'schema': '1.0', 'schedule': '17:00 Europe/Warsaw',
              'environment': 'prod', 'observed_at': now, 'projects': {},
              'telegram': 'UNCONNECTED', 'repair_dispatch': 'DISABLED'}
    if not is_due(now):
        if not Path(db).exists():
            report['status'] = 'NOT_DUE'
            return report  # No database creation or source call before due time.
        return _catchup_tick(db, report, now, readers, generation, audit_hook)
    store = HelpdeskStore(str(db))
    try:
        for project in PROJECTS:
            key = schedule_key(project, warsaw_local(now).date().isoformat(), generation)
            if store.has_run(key):
                report['projects'][project] = {'status': 'QUIET_DUPLICATE_RUN'}
                continue
            if not claim_schedule(store, key, now):
                # Concurrent tick already owns this project/date; skip the source.
                report['projects'][project] = {'status': 'QUIET_DUPLICATE_RUN'}
                continue
            claimed = True
            try:
                result = _verified_source(readers[project], project)
                if result is None:
                    # Never persist an empty successful run for an unavailable source.
                    report['projects'][project] = {'status': 'BLOCKED',
                                                  'reason': 'source_not_verified'}
                    health = record_collector_outcome(store, project, ok=False, reason='source_not_verified', now=now, audit_hook=audit_hook)
                    if health["alarm"] is not None:
                        report['projects'][project]['alarm'] = health["alarm"]
                    continue
                findings = _findings_from_items(project, result['items'])
                report['projects'][project] = {'status': 'COLLECTED',
                    **process_day(store, project, generation, findings, now)}
                health = record_collector_outcome(store, project, ok=True, now=now)
                if health["alarm"] is not None:
                    report['projects'][project]['alarm'] = health["alarm"]
                release_schedule_claim(store, key)
                claimed = False
            except Exception:
                report['projects'][project] = {'status': 'BLOCKED',
                                              'reason': 'collector_or_transaction_failed'}
                health = record_collector_outcome(store, project, ok=False, reason='collector_or_transaction_failed', now=now, audit_hook=audit_hook)
                if health["alarm"] is not None:
                    report['projects'][project]['alarm'] = health["alarm"]
            finally:
                if claimed:
                    release_schedule_claim(store, key)
        report['pending_requests'] = len(store.list_requests(status='proposed'))
        report['status'] = 'BLOCKED' if any(p['status'] == 'BLOCKED' for p in report['projects'].values()) else 'COLLECTED'
        # COLLECTED means persisted local proposals only, never notified or approved.
        return report
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    # Reserve a fresh evidence path before any reads. Existing evidence is preserved.
    with args.evidence.open('x', encoding='utf-8') as output:
        report = run_tick(args.db)
        json.dump(report, output, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))
    return 2 if report['status'] == 'BLOCKED' else 0


def _catchup_tick(db, report, now, readers, generation, audit_hook):
    """Bounded pre-17:00 tick: at most one catch-up per project.

    Only the latest missed Warsaw business day is recovered (never a
    historical storm), exactly once per project/date/generation through the
    same durable run marker as the daily pass. BLOCKED sources stay
    retryable and never persist an empty successful run.
    """
    store = HelpdeskStore(str(db))
    try:
        for project in PROJECTS:
            reader = readers.get(project)
            if reader is None:
                report['projects'][project] = {'status': 'SKIPPED',
                                               'reason': 'reader_unconfigured'}
                continue
            due = missed_catchup_date(store, project, generation, now)
            if due is None:
                report['projects'][project] = {'status': 'QUIET_NO_CATCHUP'}
                continue
            key = schedule_key(project, due, generation)
            if not claim_schedule(store, key, now):
                # Concurrent worker already collecting or finished this date.
                report['projects'][project] = {'status': 'QUIET_DUPLICATE_RUN'}
                continue
            claimed = True
            try:
                result = _verified_source(reader, project)
                if result is None:
                    # Never persist an empty successful run for a bad source.
                    health = record_collector_outcome(
                        store, project, ok=False, reason='source_not_verified',
                        now=now, audit_hook=audit_hook)
                    entry = {'status': 'BLOCKED',
                             'reason': 'source_not_verified'}
                    if health["alarm"] is not None:
                        entry['alarm'] = health["alarm"]
                    report['projects'][project] = entry
                    continue
                findings = _findings_from_items(project, result['items'])
                outcome = process_catchup(store, project, generation,
                                          findings, now)
                record_collector_outcome(store, project, ok=True, now=now)
                release_schedule_claim(store, key)
                claimed = False
                if outcome['action'] in ('nothing-missed', 'quiet-duplicate-run'):
                    report['projects'][project] = {'status': 'QUIET_NO_CATCHUP'
                                                   if outcome['action'] == 'nothing-missed'
                                                   else 'QUIET_DUPLICATE_RUN'}
                else:
                    report['projects'][project] = {'status': 'CATCHUP_COLLECTED',
                                                   **outcome}
            except Exception:
                health = record_collector_outcome(
                    store, project, ok=False,
                    reason='collector_or_transaction_failed',
                    now=now, audit_hook=audit_hook)
                entry = {'status': 'BLOCKED',
                         'reason': 'collector_or_transaction_failed'}
                if health["alarm"] is not None:
                    entry['alarm'] = health["alarm"]
                report['projects'][project] = entry
            finally:
                if claimed:
                    release_schedule_claim(store, key)
        report['pending_requests'] = len(store.list_requests(status='proposed'))
        states = [p.get('status') for p in report['projects'].values()]
        if any(s == 'BLOCKED' for s in states):
            report['status'] = 'BLOCKED'
        elif any(s == 'CATCHUP_COLLECTED' for s in states):
            report['status'] = 'CATCHUP_COLLECTED'
        else:
            report['status'] = 'QUIET_NO_CATCHUP'
        return report
    finally:
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
