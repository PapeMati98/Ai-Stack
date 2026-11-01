"""Owner-configured Windows maintenance tick; no task resumption or messaging.

Durable intake precedes scanner checkpoints. Projects require an explicit path
or session mapping; unknown project records remain in a visible local queue.
"""
import argparse,datetime as dt,glob,hashlib,json,ntpath,os,re,sqlite3,subprocess,sys,uuid
from contextlib import closing, contextmanager
from pathlib import Path
from caretaker import tick_budget
from caretaker.connectors import CONNECTOR_SCAN_SCHEMA_VERSION,SourceSpec,collect_sources
from caretaker.fleet_receipts import inspect_receipt
from caretaker.store import Store


def route(event,control):
    exact=control.get('session_projects',{}).get(event.get('native_session_id'))
    if exact:return exact if re.fullmatch(r'[a-z0-9][a-z0-9-]{0,100}',exact) else None
    explicit=event.get('project_slug') or event.get('project')
    if isinstance(explicit,str) and explicit in control.get('known_projects',[]) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,100}',explicit):return explicit
    cwd=event.get('cwd')
    if not isinstance(cwd,str):return None
    path=os.path.normcase(os.path.abspath(cwd))
    matches=[]
    for prefix,project in control.get('workspace_projects',{}).items():
        tick_budget.check('maintenance.py:metadata_or_work')
        root=os.path.normcase(os.path.abspath(prefix))
        if path==root or path.startswith(root+os.sep):matches.append((len(root),project))
    result=max(matches)[1] if matches else None
    return result if isinstance(result,str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,100}',result) else None


def event_id(event):
    return hashlib.sha256('\x1f'.join(str(event[k]) for k in ['tool','native_session_id','cursor']).encode()).hexdigest()


def _utc_completion(value):
    """Accept only an explicit, timezone-aware completion instant."""
    if not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)', value):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _is_caretaker_self_receipt(event, state):
    """Recognize Fleet reviews run within this caretaker's own state tree."""
    cwd = event.get('cwd')
    if (state is None or event.get('completion_kind') != 'verified_fleet_handoff'
            or not str(event.get('native_session_id') or '').startswith('logical:fleet-receipt:')
            or not isinstance(cwd, str) or not os.path.isabs(cwd)):
        return False
    root = os.path.normcase(os.path.abspath(state))
    path = os.path.normcase(os.path.abspath(cwd))
    return path == root or path.startswith(root + os.sep)


def _unassigned_by_design(event, control):
    cwd = event.get('cwd')
    prefixes = control.get('unassigned_by_design_workspace_prefixes', [])
    if not isinstance(cwd, str) or not cwd or not isinstance(prefixes, list):
        return False
    path = ntpath.normcase(ntpath.normpath(cwd))
    for prefix in prefixes:
        tick_budget.check('maintenance.py:metadata_or_work')
        if not isinstance(prefix, str) or not prefix:
            continue
        root = ntpath.normcase(ntpath.normpath(os.path.expandvars(prefix)))
        if path == root or path.startswith(root.rstrip('\\') + '\\'):
            return True
    return False


def _design_unassigned_count(database, control, *, new_lane=False, exclude_self=False):
    if not control.get('unassigned_by_design_workspace_prefixes'):
        return 0
    query = ("SELECT q.payload FROM queue q JOIN new_epoch_events n ON n.event_id=q.id "
             "WHERE q.project IS NULL AND n.status='admitted'" if new_lane else
             "SELECT q.payload FROM queue q LEFT JOIN new_epoch_events n ON n.event_id=q.id "
             "WHERE q.project IS NULL AND coalesce(n.reason,'')!='caretaker_self_activity'"
             if exclude_self else "SELECT payload FROM queue WHERE project IS NULL")
    return sum(_unassigned_by_design(json.loads(payload), control)
               for (payload,) in database.execute(query))


def _ensure_new_epoch_tables(database):
    database.executescript('''
        CREATE TABLE IF NOT EXISTS new_event_epoch (
            id INTEGER PRIMARY KEY CHECK(id=1), activated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS new_epoch_baseline_events (
            event_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS new_epoch_baseline_proposals (
            path TEXT PRIMARY KEY, proposal_id TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS new_epoch_baseline_proposal_ids
            ON new_epoch_baseline_proposals(proposal_id);
        CREATE TABLE IF NOT EXISTS new_epoch_events (
            event_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            reason TEXT NOT NULL, completed_at TEXT,
            forwarded INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS new_epoch_migrations (
            version TEXT PRIMARY KEY);
    ''')


def _new_epoch_table_exists(database):
    return database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='new_event_epoch'").fetchone() is not None


def _table_exists(database, name):
    return database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def activate_new_event_epoch(database, vault, now=None):
    """Explicit owner activation; preserve baseline rows and the first UTC instant."""
    from caretaker.backlog import proposal_identities

    _ensure_new_epoch_tables(database)
    existing = database.execute('SELECT activated_at FROM new_event_epoch WHERE id=1').fetchone()
    if existing:
        return existing[0]
    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        raise ValueError('epoch activation requires aware UTC time')
    activated_at = moment.astimezone(dt.timezone.utc).isoformat()
    proposals = proposal_identities(Path(vault))
    with database:
        database.execute('INSERT INTO new_event_epoch(id,activated_at) VALUES(1,?)', (activated_at,))
        database.execute('INSERT INTO new_epoch_baseline_events(event_id) SELECT id FROM queue')
        database.executemany('INSERT INTO new_epoch_baseline_proposals(path,proposal_id) VALUES(?,?)', proposals)
    return activated_at


def _document_epoch_source_ids(control):
    """Only explicitly scheduled file sources may enter the document lane."""
    policy = (control or {}).get('document_epoch', {}) or {}
    if policy.get('enabled') is not True:
        return set()
    requested = policy.get('source_ids', [])
    if not isinstance(requested, list) or not all(isinstance(x, str) and x for x in requested):
        raise ValueError('invalid_document_epoch_source_ids')
    file_sources = [source for source in (control or {}).get('sources', [])
                    if isinstance(source, dict) and source.get('kind') == 'files']
    all_ids = [source.get('id') for source in file_sources]
    if not all(isinstance(ident, str) and ident for ident in all_ids):
        raise ValueError('invalid_file_source_id')
    if len(all_ids) != len(set(all_ids)):
        raise ValueError('duplicate_file_source_id')
    available = {source.get('id') for source in file_sources
                 if isinstance(source, dict) and source.get('kind') == 'files'
                 and source.get('scheduled') is True and source.get('status') == 'supported'
                 and source.get('roots') and source.get('file_observation_mode') == 'scan_time'}
    if not set(requested) <= available:
        raise ValueError('document_epoch_source_not_eligible')
    return set(requested)


def _admit_new_epoch_events(database, document_source_ids=(), state=None):
    row = database.execute('SELECT activated_at FROM new_event_epoch WHERE id=1').fetchone()
    if not row:
        return
    activation = _utc_completion(row[0])
    if activation is None:
        raise ValueError('invalid persisted new-event epoch')
    rows = database.execute('''
        SELECT q.id,q.payload FROM queue q
        LEFT JOIN new_epoch_baseline_events b ON b.event_id=q.id
        LEFT JOIN new_epoch_events n ON n.event_id=q.id
        WHERE b.event_id IS NULL AND n.event_id IS NULL''').fetchall()
    # A source first discovered after activation may have been observed before
    # the cutoff but lack a completion timestamp. Keep it deferred, while
    # separating it from ambiguous post-cutoff observations in lane health.
    older_missing = database.execute('''
        SELECT n.event_id,q.payload FROM new_epoch_events n JOIN queue q ON q.id=n.event_id
        WHERE n.status='deferred' AND n.reason='missing_completed_at'
    ''').fetchall()
    with database:
        if state is not None:
            # Existing rows are reclassified in place, without changing their
            # queue identity, payload, or forwarding history. The predicate
            # makes subsequent ticks no-ops.
            stored = database.execute('''
                SELECT n.event_id,q.payload FROM new_epoch_events n
                JOIN queue q ON q.id=n.event_id
                WHERE n.status!='deferred' OR n.reason!='caretaker_self_activity'
            ''').fetchall()
            for ident, payload in stored:
                tick_budget.check('maintenance.py:metadata_or_work')
                if _is_caretaker_self_receipt(json.loads(payload), state):
                    database.execute('''UPDATE new_epoch_events
                                        SET status='deferred',reason='caretaker_self_activity'
                                        WHERE event_id=?''', (ident,))
        recover_claude = database.execute(
            "SELECT 1 FROM new_epoch_migrations WHERE version='claude_stop_reason_021'"
        ).fetchone() is None
        for ident, payload in older_missing:
            tick_budget.check('maintenance.py:metadata_or_work')
            event = json.loads(payload)
            if _is_caretaker_self_receipt(event, state):
                continue
            if event.get('event_type') == 'user_message.v1':
                database.execute('UPDATE new_epoch_events SET reason=? WHERE event_id=?',
                                 ('not_completed_turn', ident))
                continue
            observed = _utc_completion(event.get('observed_at'))
            if (recover_claude and event.get('tool') == 'claude'
                    and event.get('event_type') == 'completed_turn.v1'
                    and str(event.get('completion_kind') or '').startswith('stop_reason:')
                    and event.get('completed_at') in (None, '') and observed is not None):
                # Repair only records deferred before this release.  A new row
                # without a native timestamp must remain deferred on later ticks.
                event['completed_at'] = event['observed_at']
                status, reason = (('deferred', 'completed_before_activation')
                                  if observed < activation else ('admitted', ''))
                database.execute('UPDATE queue SET payload=? WHERE id=?',
                                 (json.dumps(event, ensure_ascii=False), ident))
                database.execute('''UPDATE new_epoch_events
                                    SET status=?,reason=?,completed_at=? WHERE event_id=?''',
                                 (status, reason, observed.isoformat(), ident))
                continue
            if observed is not None and observed < activation:
                database.execute('UPDATE new_epoch_events SET reason=? WHERE event_id=?',
                                 ('observed_before_activation_without_completion', ident))
        if recover_claude:
            database.execute("INSERT INTO new_epoch_migrations(version) VALUES('claude_stop_reason_021')")
        for ident, payload in rows:
            tick_budget.check('maintenance.py:metadata_or_work')
            event = json.loads(payload)
            raw = event.get('completed_at')
            completed = _utc_completion(raw)
            observed = _utc_completion(event.get('observed_at'))
            # A document is an observation, not a completed agent turn. Its
            # mtime can be historical, so require a fresh connector observation
            # and an explicitly opted-in source, never the file mtime.
            source_ref = event.get('source_ref') or {}
            document_allowed = (event.get('tool') == 'files'
                and event.get('event_type') == 'owner_document.v1'
                and event.get('completion_kind') == 'owner_document'
                and isinstance(source_ref, dict)
                and source_ref.get('source_id') in document_source_ids
                and not source_ref.get('rejection_reason')
                and bool(source_ref.get('relative_path'))
                and bool(re.fullmatch(r'[0-9a-f]{64}', str(source_ref.get('sha256') or '')))
                and observed is not None and observed >= activation)
            if _is_caretaker_self_receipt(event, state):
                status, reason = 'deferred', 'caretaker_self_activity'
            elif document_allowed:
                status, reason = 'admitted', ''
            elif (event.get('event_type') in {'owner_document.v1', 'user_message.v1'}
                    or event.get('completion_kind') in {'owner_document', 'user_statement'}):
                status, reason = 'deferred', 'not_completed_turn'
            elif completed is None:
                status = 'deferred'
                reason = ('observed_before_activation_without_completion'
                          if raw in (None, '') and observed is not None and observed < activation
                          else 'missing_completed_at' if raw in (None, '') else 'invalid_completed_at')
            elif completed < activation:
                status, reason = 'deferred', 'completed_before_activation'
            else:
                status, reason = 'admitted', ''
            database.execute('INSERT INTO new_epoch_events(event_id,status,reason,completed_at) VALUES(?,?,?,?)',
                             (ident, status, reason, completed.isoformat() if completed else None))
        _mark_superseded_codex_items(database)


def _mark_superseded_codex_items(database):
    """Resolve only Codex items linked to the same user and a later admitted turn."""
    pending = database.execute('''
        SELECT n.event_id,q.payload FROM new_epoch_events n JOIN queue q ON q.id=n.event_id
        WHERE n.status='deferred' AND n.reason='missing_completed_at'
    ''').fetchall()
    items = []
    for ident, payload in pending:
        tick_budget.check('maintenance.py:metadata_or_work')
        event = json.loads(payload)
        if (event.get('tool') == 'codex'
                and event.get('event_type') == 'completed_turn.v1'
                and event.get('completion_kind') == 'event:item.completed'
                and event.get('native_session_id') and event.get('linked_user_cursor')):
            observed = _utc_completion(event.get('observed_at'))
            if observed is not None:
                items.append((ident, event, observed))
    if not items:
        return
    completed = {}
    for payload, completed_at in database.execute('''
        SELECT q.payload,n.completed_at FROM new_epoch_events n JOIN queue q ON q.id=n.event_id
        WHERE n.status='admitted'
    '''):
        tick_budget.check('maintenance.py:metadata_or_work')
        event = json.loads(payload)
        if (event.get('tool') != 'codex'
                or event.get('event_type') != 'completed_turn.v1'
                or event.get('completion_kind') not in {'event:task.complete', 'event:turn.completed'}
                or not event.get('native_session_id') or not event.get('linked_user_cursor')):
            continue
        instant = _utc_completion(completed_at)
        if instant is not None:
            key = (event['native_session_id'], event['linked_user_cursor'])
            completed.setdefault(key, []).append((instant, event.get('project')))
    for ident, event, observed in items:
        tick_budget.check('maintenance.py:metadata_or_work')
        key = (event['native_session_id'], event['linked_user_cursor'])
        if any(instant >= observed and (not event.get('project') or not project
                                        or event['project'] == project)
               for instant, project in completed.get(key, ())):
            database.execute('UPDATE new_epoch_events SET reason=? WHERE event_id=?',
                             ('superseded_by_turn_completion', ident))


def _claude_duplicate_ids(event):
    """Canonical and 0.1.39 path-scoped queue ids of one stored Claude payload."""
    reference = event.get('source_ref')
    path_or_store = reference.get('path_or_store') if isinstance(reference, dict) else None
    identity = (event.get('tool'), event.get('native_session_id'), event.get('cursor'))
    if not all(isinstance(value, str) and value for value in identity) \
            or not isinstance(path_or_store, str) or not path_or_store:
        return None
    tool, session, cursor = identity
    canonical = hashlib.sha256('\x1f'.join((tool, session, cursor)).encode('utf-8')).hexdigest()
    scoped = hashlib.sha256('\x1f'.join((tool, session, cursor, path_or_store)).encode('utf-8')).hexdigest()
    return canonical, scoped


def reconcile_claude_duplicates(database, apply=False):
    """Report Claude rows whose 0.1.39 path-scoped id duplicates a canonical row.

    Dry-run by default: the report counts what a run found without writing.
    Applying never deletes a record; the duplicate keeps its payload and is
    marked ``forwarded=-2`` so the tick stops forwarding it, and the new-event
    lane states that the canonical event supersedes it. ``checked`` counts every
    queue row examined, not only Claude rows.
    """
    report = {'checked': 0, 'duplicates_found': 0, 'applied': bool(apply),
              'updated_queue': 0, 'updated_new_epoch': 0}
    if not _table_exists(database, 'queue'):
        return report
    rows = database.execute('SELECT id,payload FROM queue').fetchall()
    report['checked'] = len(rows)
    known = {row[0] for row in rows}
    duplicates = []
    for ident, payload in rows:
        tick_budget.check('maintenance.py:metadata_or_work')
        try:
            event = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get('tool') != 'claude':
            continue
        ids = _claude_duplicate_ids(event)
        if ids is None:
            continue
        canonical, scoped = ids
        if scoped == ident and canonical != ident and canonical in known:
            duplicates.append(ident)
    report['duplicates_found'] = len(duplicates)
    if not (apply and duplicates):
        return report
    lane = _table_exists(database, 'new_epoch_events')
    with database:
        for ident in duplicates:
            tick_budget.check('maintenance.py:metadata_or_work')
            report['updated_queue'] += database.execute(
                'UPDATE queue SET forwarded=-2 WHERE id=? AND forwarded=0',
                (ident,)).rowcount
            if lane:
                report['updated_new_epoch'] += database.execute(
                    '''UPDATE new_epoch_events
                       SET status='duplicate',reason='superseded_by_canonical_event_id',forwarded=-2
                       WHERE event_id=? AND forwarded=0''', (ident,)).rowcount
    return report


def _new_epoch_counts(database, control=None):
    if not _new_epoch_table_exists(database):
        return {'status': 'NOT_ACTIVATED', 'activated_at': None, 'baseline_events': 0,
                'baseline_proposals': 0, 'admitted_events': 0, 'pending_events': 0,
                'unassigned_events': 0, 'unassigned_by_design': 0, 'rejected_events': 0,
                'deferred_events': 0, 'deferred_reasons': {}}
    row = database.execute('SELECT activated_at FROM new_event_epoch WHERE id=1').fetchone()
    if not row:
        return {'status': 'NOT_ACTIVATED', 'activated_at': None, 'baseline_events': 0,
                'baseline_proposals': 0, 'admitted_events': 0, 'pending_events': 0,
                'unassigned_events': 0, 'unassigned_by_design': 0, 'rejected_events': 0,
                'deferred_events': 0, 'deferred_reasons': {}}
    reasons = dict(database.execute("SELECT reason,count(*) FROM new_epoch_events WHERE status='deferred' GROUP BY reason"))
    by_design = _design_unassigned_count(database, control or {}, new_lane=True)
    return {
        'activated_at': row[0],
        'baseline_events': database.execute('SELECT count(*) FROM new_epoch_baseline_events').fetchone()[0],
        'baseline_proposals': database.execute('SELECT count(*) FROM new_epoch_baseline_proposals').fetchone()[0],
        'admitted_events': database.execute("SELECT count(*) FROM new_epoch_events WHERE status='admitted'").fetchone()[0],
        'pending_events': database.execute("SELECT count(*) FROM new_epoch_events WHERE status='admitted' AND forwarded=0").fetchone()[0],
        'unassigned_events': database.execute("SELECT count(*) FROM new_epoch_events n JOIN queue q ON q.id=n.event_id WHERE n.status='admitted' AND q.project IS NULL").fetchone()[0] - by_design,
        'unassigned_by_design': by_design,
        'rejected_events': database.execute("SELECT count(*) FROM new_epoch_events WHERE status='admitted' AND forwarded=-1").fetchone()[0],
        'deferred_events': sum(reasons.values()), 'deferred_reasons': reasons,
    }


def _proposal_created_at(text, proposal_id):
    """Require every available creation timestamp to be after the cutoff."""
    from caretaker.publication import fm

    declared = fm(text, 'created_at')
    match = re.fullmatch(r'prop-(\d{8}T\d{9}Z)-[A-Za-z0-9._-]+', proposal_id)
    from_id = None
    if match:
        try:
            from_id = dt.datetime.strptime(match[1], '%Y%m%dT%H%M%S%fZ').replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    if declared is None:
        return from_id
    from_metadata = _utc_completion(declared)
    if from_metadata is None:
        return None
    return min(from_id, from_metadata) if from_id is not None else from_metadata


def _caretaker_proposal(meta):
    return (meta.get('producer') == 'openclaw-caretaker'
            or str(meta.get('path') or '').startswith('proposals/openclaw-caretaker/'))


def _agent_proposal_inventory(database, vault, control):
    """Count post-activation agent proposals without exposing their contents."""
    from caretaker.audit import accounted_proposal_ids
    from caretaker.publication import (PublicationError, canonical_project_slug, fm, _ids,
                                       no_durable_session_project,
                                       proposal_project_identity, safe_file)
    from caretaker.registry import read_registry

    counts = {'eligible': 0, 'accounted': 0, 'pending': 0,
              'unroutable': 0, 'unsupported': 0}
    result = {'counts': counts, 'pending': set(), 'accounted_ids': set(),
              'available': True}
    registry, registry_warning = read_registry(Path(vault))
    if registry_warning:
        result['registry_warning'] = registry_warning
    if not _new_epoch_table_exists(database):
        return result
    row = database.execute('SELECT activated_at FROM new_event_epoch WHERE id=1').fetchone()
    activation = _utc_completion(row[0]) if row else None
    if activation is None:
        return result
    baseline_paths = {path for (path,) in database.execute('SELECT path FROM new_epoch_baseline_proposals')}
    baseline_ids = {ident for (ident,) in database.execute('SELECT proposal_id FROM new_epoch_baseline_proposals') if ident}
    try:
        accounted = accounted_proposal_ids(Path(vault))
        result['accounted_ids'] = accounted
        paths = sorted((Path(vault) / 'proposals').glob('*/*.md'))
    except (PublicationError, OSError):
        result['available'] = False
        return result
    configured = control.get('permitted_projects')
    permitted = {slug for slug in (configured if isinstance(configured, list) else [])
                 if isinstance(slug, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,100}', slug)}
    for path in paths:
        tick_budget.check('maintenance.py:metadata_or_work')
        name = path.relative_to(vault).as_posix()
        try:
            text = safe_file(Path(vault), name).read_text(encoding='utf-8-sig').replace('\r\n', '\n')
            proposal_id = fm(text, 'id')
            if (not isinstance(proposal_id, str) or not re.fullmatch(r'prop-[A-Za-z0-9._-]+', proposal_id)
                    or name in baseline_paths or proposal_id in baseline_ids
                    or fm(text, 'classification') not in {'internal', 'public'}
                    or _caretaker_proposal({'path': name, 'producer':
                                            fm(text, 'agent') or fm(text, 'producer')
                                            or fm(text, 'reporter')})):
                continue
            created = _proposal_created_at(text, proposal_id)
            if created is None or created < activation:
                continue
            declared = fm(text, 'project_slug')
            if isinstance(declared, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,100}', declared):
                project = canonical_project_slug(declared, registry) if registry else declared
            elif declared in (None, ''):
                journal_project = no_durable_session_project(text)
                project = (canonical_project_slug(journal_project, registry)
                           if registry else journal_project)
                if project is None and registry:
                    project = proposal_project_identity(text, registry)
            else:
                project = None
            if project not in permitted or proposal_project_identity(text, registry) != project:
                counts['unroutable'] += 1
                continue
            if not _ids(text, 'source_ids'):
                counts['unsupported'] += 1
                continue
            counts['eligible'] += 1
            if proposal_id in accounted:
                counts['accounted'] += 1
            else:
                counts['pending'] += 1
                result['pending'].add((name, proposal_id))
        except (PublicationError, OSError, UnicodeError, ValueError, TypeError):
            counts['unsupported'] += 1
    return result


def _new_epoch_proposal_filter(database, state, control=None, vault=None):
    """Admit new agent proposals or caretaker proposals with complete receipts."""
    admitted = {}
    for project, payload in database.execute('''
        SELECT q.project,q.payload FROM new_epoch_events n JOIN queue q ON q.id=n.event_id
        WHERE n.status='admitted' AND n.forwarded>=0 AND q.project IS NOT NULL'''):
        tick_budget.check('maintenance.py:metadata_or_work')
        event = json.loads(payload)
        admitted.setdefault(project, set()).add(
            ':'.join(str(event[k]) for k in ('tool', 'native_session_id', 'cursor')))
    baseline_paths = {row[0] for row in database.execute('SELECT path FROM new_epoch_baseline_proposals')}
    baseline_ids = {row[0] for row in database.execute('SELECT proposal_id FROM new_epoch_baseline_proposals') if row[0]}
    inventory = _agent_proposal_inventory(database, vault, control) if vault is not None and control is not None else None
    receipts = {}

    def eligible(meta):
        project = meta['project']
        if (meta['path'] in baseline_paths or meta['proposal_id'] in baseline_ids
                or (inventory is not None and (not inventory['available']
                     or meta['proposal_id'] in inventory['accounted_ids']))):
            return False
        if inventory is not None:
            if not _caretaker_proposal(meta):
                return (meta['path'], meta['proposal_id']) in inventory['pending']
        if not admitted.get(project):
            return False
        if project not in receipts:
            receipts[project] = set()
            store_path = Path(state) / 'new-event-epoch' / 'projects' / project / 'state' / 'caretaker.sqlite'
            if store_path.is_file():
                try:
                    with closing(sqlite3.connect(store_path.as_uri() + '?mode=ro', uri=True)) as store:
                        rows = store.execute("SELECT request_json FROM outbox WHERE status IN ('recorded','deduplicated')").fetchall()
                    for (raw,) in rows:
                        tick_budget.check('maintenance.py:metadata_or_work')
                        request = json.loads(raw)
                        if not isinstance(request, dict):
                            continue
                        receipt = request.get('receipt') or {}
                        if not isinstance(receipt, dict):
                            continue
                        refs = request.get('source_refs') or []
                        if (request.get('kind') == 'handoff' and request.get('project_slug') == project
                                and receipt.get('receipt_status') == 'COMPLETE'
                                and isinstance(receipt.get('proposal_id'), str)
                                and isinstance(receipt.get('source_id'), str)
                                and isinstance(refs, list) and refs
                                and all(isinstance(ref, str) and ref in admitted[project] for ref in refs)):
                            receipts[project].add((receipt.get('proposal_id'), receipt.get('source_id')))
                except (OSError, sqlite3.Error, ValueError, TypeError):
                    pass
        return bool(meta['source_ids']) and all(
            (meta['proposal_id'], sid) in receipts[project] for sid in meta['source_ids'])

    return eligible


def source_alias_only(first,second):
    """Same completed event copied between logs; retain both provenance rows."""
    return (first != second and
            {k:v for k,v in first.items() if k!='source_ref'} ==
            {k:v for k,v in second.items() if k!='source_ref'})


FILE_EVENT_VOLATILE_FIELDS = ('observed_at', 'mtime', 'completed_at')


def _file_content_digest(event):
    """Content digest of an owner document, or '' when it carries none."""
    digest = event.get('sha256')
    reference = event.get('source_ref')
    if not isinstance(digest, str) or not digest:
        digest = reference.get('sha256') if isinstance(reference, dict) else None
    return digest if isinstance(digest, str) else ''


def _without_file_volatiles(event):
    """Comparison view that ignores re-observation timestamps."""
    stable = {k: v for k, v in event.items() if k not in FILE_EVENT_VOLATILE_FIELDS}
    reference = stable.get('source_ref')
    if isinstance(reference, dict):
        stable['source_ref'] = {k: v for k, v in reference.items() if k != 'mtime'}
    return stable


def _same_file_content_event(first, second):
    """Same owner document observed again; only its timestamps moved.

    The files connector identifies a document by its content digest, so an
    unchanged file re-observed on a later scan keeps its event identity while
    ``observed_at``/``mtime`` advance. That is not a payload conflict.
    """
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    for event in (first, second):
        tick_budget.check('maintenance.py:metadata_or_work')
        if event.get('tool') != 'files' or event.get('event_type') != 'owner_document.v1':
            return False
    digest = _file_content_digest(first)
    if not digest or digest != _file_content_digest(second):
        return False
    return _without_file_volatiles(first) == _without_file_volatiles(second)


def _source_label(source):
    """Stable identifier for a control.json source entry."""
    if not isinstance(source, dict):
        return 'unknown'
    for key in ('id', 'source_id', 'sourceId'):
        tick_budget.check('maintenance.py:metadata_or_work')
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tool = source.get('tool')
    if isinstance(tool, str) and tool.strip():
        return tool.strip()
    return 'unknown'


def _summarize_source_issues(issues):
    """Keep full counts while bounding only the detailed tick report."""
    detail = {}
    summary = {}
    for issue in issues:
        tick_budget.check('maintenance.py:metadata_or_work')
        count = max(1, int(issue.get('count', 1)))
        key = (issue.get('source_key') or issue.get('source') or '',
               issue.get('tool') or '', issue.get('reason') or '',
               issue.get('status') or '')
        if key not in detail:
            detail[key] = {k: v for k, v in issue.items() if k not in {'line_no', 'count'}}
            detail[key]['count'] = 0
        detail[key]['count'] += count
        summary_key = (key[1], key[2], key[3])
        summary[summary_key] = summary.get(summary_key, 0) + count
    return (sum(item['count'] for item in detail.values()),
            list(detail.values())[:100],
            [{'tool': tool, 'reason': reason, 'status': status, 'count': count}
             for (tool, reason, status), count in sorted(summary.items())])


def _stable_tick_source_reason(exc):
    """Map a per-source failure to a stable report code; never include payload text."""
    from caretaker.connectors import _stable_source_exception_reason
    return _stable_source_exception_reason(exc)


def _enqueue_source_events(database, events, report, source_label, control):
    """Write admitted events to queue. Secret-rejected payloads are never stored."""
    from caretaker.connectors import event_to_dict as _event_to_dict
    queued = 0
    for event in events:
        tick_budget.check('maintenance.py:metadata_or_work')
        if hasattr(event, 'to_dict') and not isinstance(event, dict):
            event = _event_to_dict(event)
        if not isinstance(event, dict):
            continue
        if event.get('rejection_reason'):
            continue
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', event.get('tool', '')):
            continue
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True)
        previous = database.execute('SELECT payload FROM queue WHERE id=?', (event_id(event),)).fetchone()
        stored = json.loads(previous[0]) if previous else None
        if stored is not None and stored != event:
            database.execute('INSERT OR IGNORE INTO conflicts VALUES(?,?,?)', (event_id(event), hashlib.sha256(encoded.encode()).hexdigest(), encoded))
            if not source_alias_only(stored, event) and not _same_file_content_event(stored, event):
                report['source_issues'].append({'source': source_label, 'status': 'conflict', 'reason': 'same_event_identity_changed_payload'})
            continue
        before = database.total_changes
        project = None if _unassigned_by_design(event, control) else route(event, control)
        database.execute('INSERT OR IGNORE INTO queue(id,project,payload) VALUES(?,?,?)', (event_id(event), project, json.dumps(event, ensure_ascii=False)))
        queued += database.total_changes - before
    report['new_events'] += queued
    return queued


def _collect_files_source(source, state):
    """Run the folder connector once for the whole files source (no glob, no scans)."""
    mapping = dict(source)
    mapping['state_dir'] = str(state)
    return collect_sources([mapping])


def _select_source_candidates(candidates,limit,rotation=0):
    """Bound source work while giving every source family a durable turn."""
    if limit<=0 or not candidates:return []
    groups={}
    for candidate in candidates:
        tick_budget.check('maintenance.py:metadata_or_work')
        source=candidate[3]
        key=(str(source.get('tool','')),str(source.get('kind','')))
        groups.setdefault(key,[]).append(candidate)
    for bucket in groups.values():bucket.sort(key=lambda item:(item[0],item[1]))
    order=sorted(groups,key=lambda key:(groups[key][0][0],key))
    start=int(rotation)%len(order)
    order=order[start:]+order[:start]
    selected=[]
    while len(selected)<limit:
        tick_budget.check('maintenance.py:metadata_or_work')
        progressed=False
        for key in order:
            tick_budget.check('maintenance.py:metadata_or_work')
            bucket=groups[key]
            if not bucket:continue
            selected.append(bucket.pop(0));progressed=True
            if len(selected)>=limit:break
        if not progressed:break
    return selected


def _audit_fields(summary):
    return {
        'raw_eligible': summary.get('raw_eligible', 0),
        'group_count': summary.get('group_count', 0),
        'ready_group_count': summary.get('ready_group_count', 0),
        'oldest_age_hours': summary.get('oldest_age_hours', 0),
        'trigger': summary.get('trigger'),
        'deferred': summary.get('deferred', 0),
        'unsupported': summary.get('unsupported', 0),
        'quarantined_proposals': summary.get('quarantined_proposals', 0),
        'count_source': summary.get('count_source', 'proposals'),
    }


def _empty_audit_fields():
    return {
        'raw_eligible': 0, 'group_count': 0, 'ready_group_count': 0,
        'oldest_age_hours': 0, 'trigger': None, 'deferred': 0, 'unsupported': 0,
        'count_source': 'proposals', 'model_calls': 0,
    }


def _live_member_hash(vault, path):
    from caretaker.publication import safe_file, sha
    return sha(safe_file(Path(vault), path).read_bytes())


def _resolved_path_is_within(path, root):
    try:
        resolved_path = os.path.normcase(str(Path(path).resolve(strict=False)))
        resolved_root = os.path.normcase(str(Path(root).resolve(strict=False)))
        return os.path.normcase(os.path.commonpath((resolved_path, resolved_root))) == resolved_root
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _curation_state_evidence(project_state):
    """Return True/False for durable curation files, None when inspection is ambiguous."""
    root = Path(project_state) / 'curation'
    try:
        if root.is_symlink() or root.is_junction():
            return None
        if not root.exists():
            return False
        if not root.is_dir():
            return None
        for path in root.rglob('*'):
            tick_budget.check('maintenance.py:metadata_or_work')
            if path.is_symlink() or path.is_junction():
                return None
            if path.is_file():
                return True
        return False
    except (OSError, RuntimeError, PermissionError):
        return None


def _database_has_project_state_owner(database, project, project_state):
    """Resolve durable publication owner paths; cycle ids identify lanes, not owners."""
    try:
        if not _table_exists(database, 'publications'):
            return False
        rows = database.execute(
            "SELECT result FROM publications WHERE status IN "
            "('RUNNING','PREPARED_DEFERRED','UNKNOWN_REQUIRES_RECONCILIATION')"
        ).fetchall()
    except (sqlite3.Error, AttributeError, TypeError):
        return None
    new_project_state = Path(project_state)
    new_lane_root = new_project_state.parent.parent.parent
    legacy_project_state = (
        new_lane_root.parent / 'projects' / project / 'state'
        if new_lane_root.name == 'new-event-epoch' else None
    )
    row_owners = set()
    matching_rows = 0
    for (raw,) in rows:
        tick_budget.check('maintenance.py:metadata_or_work')
        try:
            value = json.loads(raw or '{}')
        except (TypeError, ValueError):
            return None
        pending = [value]
        previous = value.get('previous') if isinstance(value, dict) else None
        if previous is not None:
            if not isinstance(previous, str):
                return None
            try:
                pending.append(json.loads(previous))
            except (TypeError, ValueError):
                return None
        matched = False
        owners = set()
        ambiguous = False
        for item in pending:
            if not isinstance(item, dict) or item.get('project') != project:
                continue
            matched = True
            for field in ('state_dir', 'run_path'):
                if field not in item or item[field] is None:
                    continue
                owner = item[field]
                if not isinstance(owner, str) or not owner:
                    ambiguous = True
                    continue
                is_new = _resolved_path_is_within(owner, new_project_state)
                if is_new is None:
                    ambiguous = True
                elif is_new:
                    owners.add('new')
                    continue
                if legacy_project_state is None:
                    ambiguous = True
                    continue
                is_legacy = _resolved_path_is_within(owner, legacy_project_state)
                if is_legacy is None:
                    ambiguous = True
                elif is_legacy:
                    owners.add('legacy')
                else:
                    ambiguous = True
        if not matched:
            continue
        matching_rows += 1
        if ambiguous or len(owners) != 1:
            return None
        row_owners.update(owners)
        if len(row_owners) != 1:
            return None
    if matching_rows == 0:
        return False
    return row_owners == {'new'}


def _canonical_project_runtime_state(state, project, lane='legacy', database=None):
    """Resolve one project's canonical lane owner without creating or copying state."""
    runtime_state = Path(state)
    if lane != 'new':
        return runtime_state
    new_state = (runtime_state if runtime_state.name == 'new-event-epoch'
                 else runtime_state / 'new-event-epoch')
    if (not isinstance(project, str)
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,100}', project)):
        return new_state
    new_project_state = new_state / 'projects' / project / 'state'
    new_files = _curation_state_evidence(new_project_state)
    new_row = _database_has_project_state_owner(database, project, new_project_state)
    # New lane ownership is sticky, including an unreadable or ambiguous state.
    if new_files is not False or new_row is not False:
        return new_state
    legacy_state = new_state.parent
    legacy_project_state = legacy_state / 'projects' / project / 'state'
    legacy_files = _curation_state_evidence(legacy_project_state)
    if legacy_files is True:
        return legacy_state
    return new_state


def _runtime_state_root_from_cfg(cfg, lane):
    explicit = cfg.get('maintenance_state_dir') if isinstance(cfg, dict) else None
    if isinstance(explicit, str) and explicit:
        root = Path(explicit)
    else:
        project_state = Path(cfg.get('state_dir') or '.')
        if (project_state.name == 'state' and project_state.parent.parent.name == 'projects'):
            root = project_state.parent.parent.parent
            if root.name == 'new-event-epoch':
                root = root.parent
        else:
            root = project_state
    if lane == 'new' and root.name != 'new-event-epoch':
        root = root / 'new-event-epoch'
    return root


def _audit_summary(vault, policy, database, moment, eligible_filter=None,
                   attestation_state_dir_for_project=None,
                   summary_repair_preflight=None,
                   inventory_history_preflight=None,
                   terminal_generation_preflight=None):
    """Quarantined bytes cannot inflate the ready threshold for fresh work."""
    from caretaker.audit import summarize
    from caretaker.pubstate import fingerprint, code_version
    from caretaker.publication import safe_file, sha, PublicationError
    from caretaker.registry import main_page, read_registry
    from caretaker.repaired_retry import (
        INVENTORY_HISTORY_SOURCE_REASON, TARGET_PROVENANCE_RETRY_REASON,
        SUMMARY_REPAIR_SOURCE_REASON, TERMINAL_GENERATION_SOURCE_REASON,
    )
    registry, _ = read_registry(vault)
    excluded = set()
    version = code_version()
    rows = database.execute("SELECT status,result,fingerprint FROM publications WHERE status IN ('QUARANTINED','BLOCKED_REQUIRES_DIAGNOSIS','UNKNOWN_REQUIRES_RECONCILIATION')").fetchall()
    for status, raw, stored_fp in rows:
        tick_budget.check('maintenance.py:metadata_or_work')
        try:
            value = json.loads(raw or '{}')
            if status == 'UNKNOWN_REQUIRES_RECONCILIATION' and isinstance(value.get('previous'),str):
                prior = json.loads(value['previous'])
                value = {**prior, **value}
            pids, paths, hashes = (value.get(k) or [] for k in ('proposal_ids','paths','hashes'))
            project = value.get('project')
            if not project and paths:
                # Successful/failure result rows retain project through meta below.
                continue
            if not pids or len(pids)!=len(paths) or len(paths)!=len(hashes):
                continue
            page = safe_file(vault, main_page(project, registry))
            context_hash = sha(page.read_bytes()) if page.is_file() else 'missing_target'
            current_fp = fingerprint(project, paths, hashes+[context_hash], policy, version)
            if status == 'UNKNOWN_REQUIRES_RECONCILIATION' or current_fp == stored_fp:
                recovered_target_provenance = False
                if (status == 'QUARANTINED'
                        and value.get('reason') == TARGET_PROVENANCE_RETRY_REASON
                        and callable(attestation_state_dir_for_project)):
                    try:
                        from caretaker.publication import freeze
                        owner_state = attestation_state_dir_for_project(project)
                        frozen = freeze(
                            vault, paths, main_page(project, registry),
                            attestation_state_dir=Path(owner_state),
                        )
                        expected = set(zip(pids, paths, hashes))
                        current = {
                            (item.get('id'), item.get('path'), item.get('sha256'))
                            for item in frozen.get('proposals', [])
                            if isinstance(item, dict)
                        }
                        recovered_target_provenance = bool(expected and current == expected)
                    except (PublicationError, OSError, ValueError, TypeError,
                            KeyError, RuntimeError):
                        recovered_target_provenance = False
                recovered_summary = False
                if (status == 'QUARANTINED'
                        and value.get('reason') == SUMMARY_REPAIR_SOURCE_REASON
                        and callable(summary_repair_preflight)):
                    try:
                        recovered_summary = summary_repair_preflight(
                            project, value, stored_fp,
                        ) is True
                    except Exception:
                        recovered_summary = False
                recovered_inventory_history = False
                if (status == 'QUARANTINED'
                        and value.get('reason') == INVENTORY_HISTORY_SOURCE_REASON
                        and callable(inventory_history_preflight)):
                    try:
                        recovered_inventory_history = inventory_history_preflight(
                            project, value, stored_fp,
                        ) is True
                    except Exception:
                        recovered_inventory_history = False
                recovered_terminal_generation = False
                if (status == 'QUARANTINED'
                        and value.get('reason') == TERMINAL_GENERATION_SOURCE_REASON
                        and callable(terminal_generation_preflight)):
                    try:
                        recovered_terminal_generation = terminal_generation_preflight(
                            project, value, stored_fp,
                        ) is True
                    except Exception:
                        recovered_terminal_generation = False
                if (recovered_target_provenance or recovered_summary
                        or recovered_inventory_history or recovered_terminal_generation):
                    continue
                excluded.update(zip(pids,paths,hashes))
        except (ValueError, TypeError, OSError, PublicationError):
            continue
    return summarize(vault, policy, now=moment.timestamp(), excluded=excluded,
                     eligible_filter=eligible_filter)


def _summary_repair_preflight(vault, state_dir, project, value):
    """Prove one exact no-effect summary-only REPAIR continuation."""
    try:
        from caretaker.draft_summary import terminal_summary_retry_plan
        from caretaker.publication import freeze
        from caretaker.registry import main_page, read_registry

        paths = value.get('paths') if isinstance(value, dict) else None
        if (not isinstance(paths, list) or not paths
                or paths != sorted(paths)
                or not isinstance(project, str)):
            return False
        registry, warning = read_registry(vault)
        if registry is None or warning is not None:
            return False
        frozen = freeze(
            vault, paths, main_page(project, registry),
            attestation_state_dir=Path(state_dir),
        )
        return terminal_summary_retry_plan(
            Path(vault), Path(state_dir), frozen, value,
            project=project, paths=paths,
        ) is not None
    except Exception:
        return False


def _existing_publication_phase_config(state, project, lane, database, vault, policy_raw):
    # Admission may read existing canonical state, never create a project
    # runtime/context as _project_runtime_cfg does during selected execution.
    from caretaker.config import load_config
    from caretaker.publication import safe_file
    if not _publication_project_allowed(project, policy_raw):
        raise ValueError('recovery_project_outside_scope')
    owner = _canonical_project_runtime_state(state, project, lane, database)
    expected_state = owner / 'projects' / project / 'state'
    existing_config = safe_file(owner, 'projects/' + project + '/config.json')
    phase_cfg = load_config(existing_config)
    if (phase_cfg.project_slug != project
            or Path(phase_cfg.state_dir).resolve() != expected_state.resolve()
            or Path(phase_cfg.hub.vault).resolve() != vault.resolve()):
        raise ValueError('recovery_runtime_owner_mismatch')
    return phase_cfg


def _publication_recovery_preflights(vault, policy, database, lane,
                                   phase_config_for_project):
    """Shared read-only recovery proofs for global admission and project selection."""
    from caretaker.publication import freeze
    from caretaker.pubstate import code_version
    from caretaker.registry import main_page, read_registry
    registry, _ = read_registry(vault)

    def _inventory_history_preflight(retry_project, value, _stored_fp):
        project = retry_project
        try:
            from caretaker.draft_summary import inventory_history_repair_plan

            if not isinstance(value, dict):
                return False
            paths = value.get('paths')
            if (value.get('project') != project
                    or not isinstance(paths, list) or not paths
                    or paths != sorted(paths)):
                return False
            phase_cfg = phase_config_for_project(retry_project)
            if phase_cfg.project_slug != project:
                return False
            frozen = freeze(
                vault, paths, main_page(project, registry),
                attestation_state_dir=Path(phase_cfg.state_dir),
            )
            return inventory_history_repair_plan(
                phase_cfg, paths, frozen, value,
            ) is not None
        except Exception:
            return False

    terminal_generation_plans = {}

    def _terminal_generation_source_preflight(retry_project, value, stored_fp):
        """Qualify only the terminal Docs generation chain; never replays its old call."""
        project = retry_project
        try:
            from caretaker.publication import safe_file, sha
            from caretaker.pubstate import fingerprint
            from caretaker.terminal_generation import (
                canonical_proposal_identities, terminal_generation_plan,
            )

            if (lane != 'legacy' or not isinstance(value, dict)
                    or value.get('project') != project):
                return False
            paths = value.get('paths')
            pids = value.get('proposal_ids')
            hashes = value.get('hashes')
            identities = canonical_proposal_identities(paths, pids, hashes)
            if identities is None or len(paths) > policy['max_batch']:
                return False
            ordered_paths = [item[1] for item in identities]
            ordered_hashes = [item[2] for item in identities]
            phase_cfg = phase_config_for_project(retry_project)
            if phase_cfg.project_slug != project:
                return False
            frozen = freeze(
                vault, ordered_paths, main_page(project, registry),
                attestation_state_dir=Path(phase_cfg.state_dir),
            )
            exact = set(identities)
            current = {
                (item.get('id'), item.get('path'), item.get('sha256'))
                for item in frozen.get('proposals', []) if isinstance(item, dict)
            }
            if not exact or current != exact or len(current) != len(paths):
                return False
            page = safe_file(vault, main_page(project, registry))
            context_hash = sha(page.read_bytes()) if page.is_file() else 'missing_target'
            if fingerprint(project, ordered_paths, ordered_hashes + [context_hash],
                           policy, code_version()) != stored_fp:
                return False
            plan = terminal_generation_plan(phase_cfg, ordered_paths, frozen, value)
            if plan is None:
                return False

            source_cycle = database.execute(
                'SELECT lane,status FROM audit_cycles WHERE id=?', (value.get('cycle_id'),),
            ).fetchone()
            if source_cycle != ('new', 'DRAINED'):
                return False
            source_members = database.execute(
                'SELECT proposal_id,path,sha256,project,status FROM audit_members WHERE cycle_id=?',
                (value.get('cycle_id'),),
            ).fetchall()
            expected = set(identities)
            exact_source_rows = [row for row in source_members if row[:3] in expected]
            if (len(exact_source_rows) != len(expected)
                    or {(row[0], row[1], row[2]) for row in exact_source_rows} != expected
                    or any(row[3] != project or row[4] not in {'failed', 'quarantined'}
                           for row in exact_source_rows)):
                return False

            # Query each exact identity separately to avoid broad project scans.
            memberships = []
            for pid, path, digest in sorted(expected):
                rows = database.execute(
                    'SELECT m.proposal_id,m.path,m.sha256,m.project,m.status,c.id,c.lane,c.status '
                    'FROM audit_members m JOIN audit_cycles c ON c.id=m.cycle_id '
                    'WHERE m.proposal_id=? AND m.path=?', (pid, path),
                ).fetchall()
                if any(row[2] != digest or row[3] != project for row in rows):
                    return False
                memberships.extend(rows)
            if any(row[4] not in {'failed', 'quarantined', 'pending'} for row in memberships):
                return False
            pending_rows = [row for row in memberships if row[4] == 'pending']
            pending_cycle = None
            if pending_rows:
                pending_cycles = {row[5] for row in pending_rows}
                if len(pending_cycles) != 1:
                    return False
                pending_cycle = next(iter(pending_cycles))
                cycle_rows = database.execute(
                    'SELECT m.proposal_id,m.path,m.sha256,m.project,m.status,c.lane,c.status '
                    'FROM audit_members m JOIN audit_cycles c ON c.id=m.cycle_id '
                    'WHERE m.cycle_id=?', (pending_cycle,),
                ).fetchall()
                if (len(cycle_rows) != len(expected)
                        or {(row[0], row[1], row[2]) for row in cycle_rows} != expected
                        or any(row[3] != project or row[4] != 'pending'
                               or row[5] != 'legacy' or row[6] != 'ACTIVE'
                               for row in cycle_rows)):
                    return False
                counts_row = database.execute(
                    'SELECT counts FROM audit_cycles WHERE id=?', (pending_cycle,),
                ).fetchone()
                counts = json.loads(counts_row[0] or '{}') if counts_row else {}
                if not isinstance(counts, dict) or counts.get('repaired_retry'):
                    return False

            # Any other publication record for one of these identities is a
            # conflicting history unless the normal repair row itself owns it.
            for other_batch, raw in database.execute(
                    'SELECT batch,result FROM publications ORDER BY batch').fetchall():
                if other_batch == value.get('batch'):
                    continue
                try:
                    other = json.loads(raw or '{}')
                except (TypeError, ValueError):
                    return False
                if not isinstance(other, dict) or other.get('project') != project:
                    continue
                other_ids, other_paths = other.get('proposal_ids'), other.get('paths')
                if not isinstance(other_ids, list) or not isinstance(other_paths, list):
                    continue
                if any((pid, path) in set(zip(other_ids, other_paths))
                       for pid, path, _digest in expected):
                    return False
            plan['cycle_mode'] = 'reuse' if pending_cycle else 'create'
            plan['pending_cycle'] = pending_cycle
            terminal_generation_plans[value.get('batch')] = plan
            return True
        except Exception:
            return False

    return (_inventory_history_preflight,
            _terminal_generation_source_preflight, terminal_generation_plans)


def _validated_summary_retry_marker(cfg, paths):
    """Validate the one-time retry token opened by the ordinary audit cycle."""
    marker = (cfg.raw or {}).get('maintenance_summary_repair_retry')
    if not isinstance(marker, dict):
        return None
    try:
        from caretaker.draft_summary import helper_sha256, summary_retry_lane_matches
        from caretaker.pubstate import code_version
        from caretaker.repaired_retry import (
            INVENTORY_HISTORY_RETRY_REASON, INVENTORY_HISTORY_SOURCE_REASON,
            SUMMARY_REPAIR_REASON, SUMMARY_REPAIR_SOURCE_REASON,
            REPAIRED_PUBLICATION_SHA256, publication_sha256, retry_token,
        )

        project = cfg.project_slug
        lane = marker.get('lane')
        ordered_paths = sorted(paths)
        helper_digest = helper_sha256()
        retry_reason = marker.get('reason')
        source_reason = (
            INVENTORY_HISTORY_SOURCE_REASON
            if retry_reason == INVENTORY_HISTORY_RETRY_REASON
            else SUMMARY_REPAIR_SOURCE_REASON
        )
        publication_path = Path(__file__).resolve().parent / 'caretaker' / 'publication.py'
        pub_digest = publication_sha256(publication_path)
        if (marker.get('schema') != 1
                or retry_reason not in {SUMMARY_REPAIR_REASON, INVENTORY_HISTORY_RETRY_REASON}
                or marker.get('project') != project
                or lane not in {'legacy', 'new'}
                or marker.get('paths') != ordered_paths
                or marker.get('helper_sha256') != helper_digest
                or marker.get('publication_sha256') != pub_digest
                or pub_digest != REPAIRED_PUBLICATION_SHA256
                or marker.get('code_version') != code_version()):
            return None
        state_root = (cfg.raw or {}).get('maintenance_state_dir')
        if not isinstance(state_root, str) or not state_root:
            return None
        db_path = Path(state_root) / 'intake.sqlite'
        if (not db_path.is_file() or db_path.is_symlink() or db_path.is_junction()):
            return None
        cycle_id = marker.get('cycle_id')
        if not summary_retry_lane_matches(lane, cycle_id):
            return None
        retries = marker.get('members')
        if not isinstance(retries, list) or len(retries) != len(ordered_paths):
            return None
        identities = [(item.get('proposal_id'), item.get('path'), item.get('sha256'))
                      for item in retries if isinstance(item, dict)]
        if (len(identities) != len(retries)
                or sorted(item[1] for item in identities) != ordered_paths
                or len(set(identities)) != len(identities)):
            return None
        uri = 'file:' + __import__('urllib.parse', fromlist=['quote']).quote(
            str(db_path.resolve()).replace('\\', '/'), safe=':/') + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as check_db:
            cycle = check_db.execute(
                'SELECT lane,status FROM audit_cycles WHERE id=?', (cycle_id,),
            ).fetchone()
            if cycle != (lane, 'ACTIVE'):
                return None
            previous_batch = marker.get('previous_batch')
            old_row = check_db.execute(
                'SELECT status,fingerprint,result FROM publications WHERE batch=?',
                (previous_batch,),
            ).fetchone()
            if (old_row is None or old_row[0] != 'QUARANTINED'
                    or old_row[1] != marker.get('previous_fingerprint')):
                return None
            old_result = json.loads(old_row[2] or '{}')
            if (not isinstance(old_result, dict)
                    or old_result.get('reason') != source_reason
                    or old_result.get('original_reason') != source_reason
                    or old_result.get('project') != project
                    or old_result.get('paths') != ordered_paths
                    or not summary_retry_lane_matches(lane, old_result.get('cycle_id'))):
                return None
            old_cycle = check_db.execute(
                'SELECT lane FROM audit_cycles WHERE id=?',
                (old_result.get('cycle_id'),),
            ).fetchone()
            if old_cycle != (lane,):
                return None
            old_ids = old_result.get('proposal_ids')
            old_hashes = old_result.get('hashes')
            if (not isinstance(old_ids, list) or not isinstance(old_hashes, list)
                    or len(old_ids) != len(ordered_paths)
                    or len(old_hashes) != len(ordered_paths)):
                return None
            expected = set(zip(old_ids, ordered_paths, old_hashes))
            if set(identities) != expected:
                return None
            for item in retries:
                if (item.get('reason') != retry_reason
                        or item.get('project') != project
                        or item.get('previous_batch') != previous_batch
                        or item.get('helper_sha256') != helper_digest
                        or item.get('publication_sha256') != pub_digest
                        or item.get('code_version') != code_version()
                        or item.get('token') != retry_token(
                            pub_digest, project, item['proposal_id'], item['path'],
                            item['sha256'], reason=retry_reason,
                            helper_sha256=helper_digest)):
                    return None
                token_row = check_db.execute(
                    'SELECT cycle_id,publication_sha256,code_version,proposal_id,path,sha256,previous_batch '
                    'FROM repaired_retry_tokens WHERE token=?', (item['token'],),
                ).fetchone()
                expected_token_row = (
                    cycle_id, pub_digest, code_version(), item['proposal_id'],
                    item['path'], item['sha256'], previous_batch,
                )
                if token_row != expected_token_row:
                    return None
            if retry_reason == INVENTORY_HISTORY_RETRY_REASON:
                from caretaker.draft_summary import inventory_history_repair_plan
                from caretaker.publication import freeze
                from caretaker.registry import main_page, read_registry

                registry, warning = read_registry(Path(cfg.hub.vault))
                if registry is None or warning is not None:
                    return None
                frozen = freeze(
                    Path(cfg.hub.vault), ordered_paths,
                    main_page(project, registry),
                    attestation_state_dir=Path(cfg.state_dir),
                )
                if inventory_history_repair_plan(
                        cfg, ordered_paths, frozen, old_result) is None:
                    return None
        return {'old_result': old_result, 'previous_batch': previous_batch,
                'helper_sha256': helper_digest, 'reason': retry_reason}
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError,
            RuntimeError, ImportError):
        return None


def _write_summary_retry_marker(config_path, cfg, database, cycle, members,
                                retry_members, paths, *, lane):
    """Carry a consumed exact draft-repair token into the curator."""
    from caretaker.repaired_retry import (
        INVENTORY_HISTORY_RETRY_REASON, SUMMARY_REPAIR_REASON,
    )
    from caretaker.draft_summary import summary_retry_lane_matches

    selected = [item for item in retry_members
                if isinstance(item, dict)
                and item.get('reason') in {
                    SUMMARY_REPAIR_REASON, INVENTORY_HISTORY_RETRY_REASON,
                }]
    if not selected:
        return True
    try:
        if len(selected) != len(members):
            return False
        reasons = {item.get('reason') for item in selected}
        if len(reasons) != 1:
            return False
        if (lane not in {'legacy', 'new'}
                or not summary_retry_lane_matches(lane, cycle.get('id'))):
            return False
        previous_batches = {item.get('previous_batch') for item in selected}
        helper_digests = {item.get('helper_sha256') for item in selected}
        publication_digests = {item.get('publication_sha256') for item in selected}
        code_versions = {item.get('code_version') for item in selected}
        if (len(previous_batches) != 1 or len(helper_digests) != 1
                or len(publication_digests) != 1 or len(code_versions) != 1
                or None in previous_batches or None in helper_digests):
            return False
        identities = {(item.get('proposal_id'), item.get('path'), item.get('sha256'))
                      for item in selected}
        member_identities = {(item.get('proposal_id'), item.get('path'), item.get('sha256'))
                             for item in members}
        if (identities != member_identities or len(identities) != len(selected)
                or sorted(item[1] for item in identities) != sorted(paths)):
            return False
        previous_batch = next(iter(previous_batches))
        row = database.execute(
            'SELECT status,fingerprint,result FROM publications WHERE batch=?',
            (previous_batch,),
        ).fetchone()
        if row is None or row[0] != 'QUARANTINED':
            return False
        marker = {
            'schema': 1, 'reason': next(iter(reasons)),
            'project': cfg['project_slug'], 'paths': sorted(paths),
            'lane': lane,
            'cycle_id': cycle['id'], 'previous_batch': previous_batch,
            'previous_fingerprint': row[1],
            'helper_sha256': next(iter(helper_digests)),
            'publication_sha256': next(iter(publication_digests)),
            'code_version': next(iter(code_versions)),
            'members': sorted(selected, key=lambda item: (item['proposal_id'], item['path'])),
        }
        encoded = json.dumps(marker, ensure_ascii=False, separators=(',', ':'))
        if len(encoded.encode('utf-8')) > 128 * 1024:
            return False
        path = Path(config_path)
        expected_parent = Path(cfg['state_dir']).parent.resolve()
        if (not path.is_file() or path.is_symlink() or path.is_junction()
                or path.resolve().parent != expected_parent):
            return False
        config = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(config, dict) or config.get('project_slug') != cfg['project_slug']:
            return False
        config['maintenance_summary_repair_retry'] = marker
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        return True
    except (OSError, sqlite3.Error, UnicodeError, ValueError, TypeError, KeyError):
        return False


def _write_terminal_generation_retry_marker(config_path, cfg, database, cycle,
                                            members, retry_members, paths, *, lane):
    """Carry the exact one-shot terminal-generation retry into its project config."""
    from caretaker.repaired_retry import (
        REPAIRED_PUBLICATION_SHA256, TERMINAL_GENERATION_RETRY_REASON,
        retry_token, publication_sha256,
    )
    from caretaker.terminal_generation import helper_sha256, terminal_generation_plan
    from caretaker.publication import freeze
    from caretaker.registry import main_page, read_registry
    from caretaker.pubstate import code_version

    selected = [item for item in retry_members if isinstance(item, dict)
                and item.get('reason') == TERMINAL_GENERATION_RETRY_REASON]
    if not selected:
        return True
    try:
        if lane != 'legacy' or len(selected) != len(members) or len(selected) != len(paths):
            return False
        identities = {(item.get('proposal_id'), item.get('path'), item.get('sha256'))
                      for item in selected}
        member_identities = {(item.get('proposal_id'), item.get('path'), item.get('sha256'))
                             for item in members}
        if (identities != member_identities or len(identities) != len(selected)
                or sorted(item[1] for item in identities) != sorted(paths)):
            return False
        project = cfg['project_slug']
        previous_batches = {item.get('previous_batch') for item in selected}
        helper_digests = {item.get('helper_sha256') for item in selected}
        publication_digests = {item.get('publication_sha256') for item in selected}
        versions = {item.get('code_version') for item in selected}
        if (len(previous_batches) != 1 or len(helper_digests) != 1
                or len(publication_digests) != 1 or len(versions) != 1
                or next(iter(helper_digests)) != helper_sha256()
                or next(iter(publication_digests)) != REPAIRED_PUBLICATION_SHA256
                or next(iter(versions)) != code_version()):
            return False
        cycle_row = database.execute(
            'SELECT lane,status,counts FROM audit_cycles WHERE id=?', (cycle.get('id'),),
        ).fetchone()
        if cycle_row is None or cycle_row[0] != lane or cycle_row[1] != 'ACTIVE':
            return False
        counts = json.loads(cycle_row[2] or '{}')
        count_retries = counts.get('repaired_retry') if isinstance(counts, dict) else None
        if (not isinstance(count_retries, list)
                or {(item.get('proposal_id'), item.get('path'), item.get('sha256'))
                    for item in count_retries if isinstance(item, dict)} != identities
                or len(count_retries) != len(selected)):
            return False
        previous_batch = next(iter(previous_batches))
        row = database.execute(
            'SELECT status,fingerprint,result FROM publications WHERE batch=?',
            (previous_batch,),
        ).fetchone()
        if row is None or row[0] != 'QUARANTINED':
            return False
        old_result = json.loads(row[2] or '{}')
        if (not isinstance(old_result, dict)
                or old_result.get('reason') != 'phase_resume_terminal_no_replay'
                or old_result.get('original_reason') != 'phase_resume_terminal_no_replay'
                or old_result.get('project') != project):
            return False
        for item in selected:
            identity = (item.get('proposal_id'), item.get('path'), item.get('sha256'))
            if (item.get('project') != project
                    or item.get('previous_batch') != previous_batch
                    or item.get('token') != retry_token(
                        REPAIRED_PUBLICATION_SHA256, project, *identity,
                        reason=TERMINAL_GENERATION_RETRY_REASON,
                        helper_sha256=helper_sha256(),
                    )):
                return False
            token_row = database.execute(
                'SELECT cycle_id,publication_sha256,code_version,proposal_id,path,sha256,previous_batch '
                'FROM repaired_retry_tokens WHERE token=?', (item['token'],),
            ).fetchone()
            if token_row != (
                    cycle['id'], REPAIRED_PUBLICATION_SHA256, code_version(),
                    item['proposal_id'], item['path'], item['sha256'], previous_batch):
                return False
        registry, warning = read_registry(Path(cfg['hub']['vault']))
        if registry is None or warning is not None:
            return False
        frozen = freeze(
            Path(cfg['hub']['vault']), sorted(paths), main_page(project, registry),
            attestation_state_dir=Path(cfg['state_dir']),
        )
        phase_cfg = __import__('caretaker.config', fromlist=['load_config']).load_config(config_path)
        plan = terminal_generation_plan(phase_cfg, sorted(paths), frozen, old_result)
        if plan is None:
            return False
        marker = {
            'schema': 1, 'reason': TERMINAL_GENERATION_RETRY_REASON,
            'project': project, 'paths': sorted(paths), 'lane': lane,
            'cycle_id': cycle['id'], 'previous_batch': previous_batch,
            'previous_fingerprint': row[1], 'helper_sha256': helper_sha256(),
            'publication_sha256': REPAIRED_PUBLICATION_SHA256,
            'code_version': code_version(),
            'members': sorted(selected, key=lambda item: (item['proposal_id'], item['path'])),
        }
        encoded = json.dumps(marker, ensure_ascii=False, separators=(',', ':'))
        if len(encoded.encode('utf-8')) > 128 * 1024:
            return False
        config_path = Path(config_path)
        if (not config_path.is_file() or config_path.is_symlink() or config_path.is_junction()
                or config_path.resolve().parent != Path(cfg['state_dir']).parent.resolve()):
            return False
        config = json.loads(config_path.read_text(encoding='utf-8'))
        if not isinstance(config, dict) or config.get('project_slug') != project:
            return False
        config['maintenance_terminal_generation_retry'] = marker
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        return True
    except Exception:
        return False


def _validated_terminal_generation_marker(cfg, paths, *, database=None):
    marker = (cfg.raw or {}).get('maintenance_terminal_generation_retry')
    if not isinstance(marker, dict):
        return None
    try:
        from caretaker.config import load_config
        from caretaker.publication import freeze
        from caretaker.pubstate import code_version
        from caretaker.repaired_retry import (
            REPAIRED_PUBLICATION_SHA256, TERMINAL_GENERATION_RETRY_REASON,
            publication_sha256, retry_token,
        )
        from caretaker.registry import main_page, read_registry
        from caretaker.terminal_generation import (
            canonical_proposal_identities, helper_sha256,
            terminal_generation_plan,
        )

        project = cfg.project_slug
        lane = marker.get('lane')
        ordered_paths = sorted(paths)
        publication_path = Path(__file__).resolve().parent / 'caretaker' / 'publication.py'
        helper_digest = helper_sha256()
        publication_digest = publication_sha256(publication_path)
        if (marker.get('schema') != 1
                or marker.get('reason') != TERMINAL_GENERATION_RETRY_REASON
                or marker.get('project') != project or lane != 'legacy'
                or marker.get('paths') != ordered_paths
                or marker.get('helper_sha256') != helper_digest
                or marker.get('publication_sha256') != publication_digest
                or publication_digest != REPAIRED_PUBLICATION_SHA256
                or marker.get('code_version') != code_version()):
            return None
        state_root_value = (cfg.raw or {}).get('maintenance_state_dir')
        if not isinstance(state_root_value, str) or not state_root_value:
            return None
        state_root = Path(state_root_value)
        db_path = state_root / 'intake.sqlite'
        if (not db_path.is_file() or db_path.is_symlink() or db_path.is_junction()):
            return None
        if database is None:
            uri = 'file:' + __import__('urllib.parse', fromlist=['quote']).quote(
                str(db_path.resolve()).replace('\\', '/'), safe=':/') + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True)) as check_db:
                check_db.execute('PRAGMA query_only=ON')
                return _validated_terminal_generation_marker(cfg, paths, database=check_db)
        cycle_id = marker.get('cycle_id')
        cycle = database.execute(
            'SELECT lane,status,counts FROM audit_cycles WHERE id=?', (cycle_id,),
        ).fetchone()
        if cycle is None or cycle[0] != lane or cycle[1] != 'ACTIVE':
            return None
        counts = json.loads(cycle[2] or '{}')
        retries = counts.get('repaired_retry') if isinstance(counts, dict) else None
        selected = marker.get('members')
        if not isinstance(selected, list) or not selected or retries != selected:
            return None
        previous_batch = marker.get('previous_batch')
        old_row = database.execute(
            'SELECT status,fingerprint,result FROM publications WHERE batch=?',
            (previous_batch,),
        ).fetchone()
        if (old_row is None or old_row[0] != 'QUARANTINED'
                or old_row[1] != marker.get('previous_fingerprint')):
            return None
        old_result = json.loads(old_row[2] or '{}')
        if (not isinstance(old_result, dict)
                or old_result.get('reason') != 'phase_resume_terminal_no_replay'
                or old_result.get('original_reason') != 'phase_resume_terminal_no_replay'
                or old_result.get('project') != project):
            return None
        old_ids = old_result.get('proposal_ids')
        old_paths = old_result.get('paths')
        old_hashes = old_result.get('hashes')
        old_identities = canonical_proposal_identities(old_paths, old_ids, old_hashes)
        if (old_identities is None
                or [item[1] for item in old_identities] != ordered_paths):
            return None
        expected = set(old_identities)
        identities = {(item.get('proposal_id'), item.get('path'), item.get('sha256'))
                      for item in selected if isinstance(item, dict)}
        if identities != expected or len(identities) != len(selected):
            return None
        members = database.execute(
            'SELECT proposal_id,path,sha256,project,status FROM audit_members WHERE cycle_id=?',
            (cycle_id,),
        ).fetchall()
        if (len(members) != len(expected)
                or {(row[0], row[1], row[2]) for row in members} != expected
                or any(row[3] != project or row[4] != 'pending' for row in members)):
            return None
        for item in selected:
            identity = (item['proposal_id'], item['path'], item['sha256'])
            if (item.get('reason') != TERMINAL_GENERATION_RETRY_REASON
                    or item.get('project') != project
                    or item.get('previous_batch') != previous_batch
                    or item.get('helper_sha256') != helper_digest
                    or item.get('publication_sha256') != publication_digest
                    or item.get('code_version') != code_version()
                    or item.get('token') != retry_token(
                        publication_digest, project, *identity,
                        reason=TERMINAL_GENERATION_RETRY_REASON,
                        helper_sha256=helper_digest)):
                return None
            token_row = database.execute(
                'SELECT cycle_id,publication_sha256,code_version,proposal_id,path,sha256,previous_batch '
                'FROM repaired_retry_tokens WHERE token=?', (item['token'],),
            ).fetchone()
            if token_row != (
                    cycle_id, publication_digest, code_version(), *identity, previous_batch):
                return None
        registry, warning = read_registry(Path(cfg.hub.vault))
        if registry is None or warning is not None:
            return None
        frozen = freeze(
            Path(cfg.hub.vault), ordered_paths, main_page(project, registry),
            attestation_state_dir=Path(cfg.state_dir),
        )
        plan = terminal_generation_plan(cfg, ordered_paths, frozen, old_result)
        if plan is None:
            return None
        return {"old_result": old_result, "plan": plan,
                "previous_batch": previous_batch, "helper_sha256": helper_digest,
                "current_progress": plan["progress"]}
    except Exception:
        return None


def _attach_terminal_generation_retry(database, cycle, members, retries, lane):
    """Attach exact retry tokens to an already frozen exact pending successor."""
    from caretaker.repaired_retry import (
        REPAIRED_PUBLICATION_SHA256, TERMINAL_GENERATION_RETRY_REASON,
        ensure_retry_table,
    )
    from caretaker.pubstate import code_version
    from caretaker.terminal_generation import helper_sha256

    try:
        if lane != 'legacy' or not isinstance(cycle, dict):
            return False
        cycle_id = cycle.get('id')
        cycle_row = database.execute(
            'SELECT lane,status,counts FROM audit_cycles WHERE id=?', (cycle_id,),
        ).fetchone()
        if cycle_row is None or cycle_row[0] != lane or cycle_row[1] != 'ACTIVE':
            return False
        counts = json.loads(cycle_row[2] or '{}')
        if not isinstance(counts, dict) or counts.get('repaired_retry'):
            return False
        identities = {(item.get('proposal_id'), item.get('path'), item.get('sha256'))
                      for item in retries if isinstance(item, dict)}
        if (not identities or len(identities) != len(retries)
                or identities != {(item.get('proposal_id'), item.get('path'), item.get('sha256'))
                                  for item in members}):
            return False
        cycle_members = database.execute(
            'SELECT proposal_id,path,sha256,project,status FROM audit_members WHERE cycle_id=?',
            (cycle_id,),
        ).fetchall()
        if (len(cycle_members) != len(identities)
                or {(row[0], row[1], row[2]) for row in cycle_members} != identities
                or any(row[3] != members[0].get('project') or row[4] != 'pending'
                       for row in cycle_members)):
            return False
        project = members[0].get('project')
        previous_batches = {item.get('previous_batch') for item in retries}
        helper_digests = {item.get('helper_sha256') for item in retries}
        publication_digests = {item.get('publication_sha256') for item in retries}
        versions = {item.get('code_version') for item in retries}
        if (len(previous_batches) != 1 or len(helper_digests) != 1
                or len(publication_digests) != 1 or len(versions) != 1
                or next(iter(helper_digests)) != helper_sha256()
                or next(iter(publication_digests)) != REPAIRED_PUBLICATION_SHA256
                or next(iter(versions)) != code_version()
                or any(item.get('reason') != TERMINAL_GENERATION_RETRY_REASON
                       or item.get('project') != project for item in retries)):
            return False
        ensure_retry_table(database)
        counts['repaired_retry'] = retries
        encoded_counts = json.dumps(counts, sort_keys=True)
        with database:
            for item in retries:
                inserted = database.execute(
                    'INSERT OR IGNORE INTO repaired_retry_tokens('
                    'token,cycle_id,publication_sha256,code_version,proposal_id,path,sha256,previous_batch) '
                    'VALUES(?,?,?,?,?,?,?,?)',
                    (item['token'], cycle_id, item['publication_sha256'], item['code_version'],
                     item['proposal_id'], item['path'], item['sha256'], item['previous_batch']),
                )
                if inserted.rowcount != 1:
                    raise RuntimeError('terminal_generation_retry_token_consumed')
            updated = database.execute(
                'UPDATE audit_cycles SET counts=? WHERE id=? AND lane=? AND status=? AND counts=?',
                (encoded_counts, cycle_id, lane, 'ACTIVE', cycle_row[2]),
            )
            if updated.rowcount != 1:
                raise RuntimeError('terminal_generation_cycle_changed')
        return True
    except (OSError, sqlite3.Error, UnicodeError, ValueError, TypeError, KeyError, RuntimeError):
        return False


def _invoke_curate(config_path, paths, *, _transport_recovery=None):
    from caretaker.publication import PublicationError
    from caretaker.transports import TransportError as _TransportError
    class _TerminalGenerationFleetUnavailable(RuntimeError):
        pass

    class _TerminalGenerationMakerOutcomeUnknown(RuntimeError):
        pass

    prior = os.environ.get('HUB_CARETAKER_ALLOW_EXEC')
    os.environ['HUB_CARETAKER_ALLOW_EXEC'] = '1'
    _curator = None
    original_command = None
    original_save_phase = None
    original_load_phase = None
    original_build_candidate = None
    original_is_format_error = None
    original_repair_prompt = None
    original_decision_from_maker = None
    original_fleet_maker_rule = None
    original_fleet_append_makers = None
    original_wire_fleet_publication = None
    safe_repair_phase = None
    started_repair_phase = None
    summary_repair_decision = None
    summary_decision_path = None
    summary_only_mode = False
    inventory_history_relocation_active = False
    inventory_history_relocation_in_progress = False
    inventory_history_prior_body = None
    inventory_source_repair_mode = False
    inventory_source_repair_prompt_used = False
    terminal_generation_active = False
    terminal_generation_plan_value = None
    terminal_generation_required_phase = None
    try:
        import curate_once as _curator_module
        _curator = _curator_module
        from caretaker.config import load_config as _load_config
        curator_cfg = _load_config(config_path)
        original_command = _curator.command
        original_build_candidate = _curator.build_candidate
        original_is_format_error = _curator.is_format_error
        original_repair_prompt = _curator.repair_prompt
        original_decision_from_maker = _curator._decision_from_maker
        original_fleet_maker_rule = _curator._fleet_maker_rule
        original_fleet_append_makers = _curator._build_fleet_append_makers
        original_wire_fleet_publication = _curator._wire_fleet_publication
        expected_engine = Path(curator_cfg.hub.script).with_name('curation.py').resolve()

        def _build_candidate_with_draft_summary(frozen, decision, candidate,
                                                observed, *args, **kwargs):
            nonlocal summary_repair_decision, summary_decision_path
            from caretaker.draft_summary import (
                corrected_draft_decision, patch_draft_summary_candidate,
            )
            from caretaker.publication import (
                PROMPT_BYTE_LIMIT, _validate_publication_decision, fm,
            )
            from caretaker.filter import contains_secret, has_reasoning_marker

            corrected = corrected_draft_decision(frozen, decision)
            selected = corrected if corrected is not None else decision
            try:
                manifest = original_build_candidate(
                    frozen, selected, candidate, observed, *args, **kwargs,
                )
            except PublicationError as exc:
                if str(exc) == 'invalid summary':
                    # Verify every other publication gate before retaining the
                    # public parsed decision for its summary-only repair.
                    probe = dict(selected)
                    probe['summary'] = 'Summary repair pending.'
                    _validate_publication_decision(frozen, probe)
                    summary_repair_decision = dict(selected)
                    summary_decision_path = None
                    record = {
                        'schema_version': 1,
                        'reason': 'invalid summary',
                        'project': curator_cfg.project_slug,
                        'target': frozen.get('target'),
                        'snapshot_sha256': frozen.get('snapshot_sha256'),
                        'proposal_ids': sorted(p['id'] for p in frozen.get('proposals', [])),
                        'decision': selected,
                    }
                    encoded = json.dumps(record, ensure_ascii=False).encode('utf-8')
                    if (len(encoded) <= PROMPT_BYTE_LIMIT
                            and not contains_secret(encoded.decode('utf-8'))
                            and not has_reasoning_marker(encoded.decode('utf-8'))):
                        summary_decision_path = Path(candidate).parent / 'summary-repair-decision.json'
                        _curator._durable_json(summary_decision_path, record)
                raise
            if (corrected is not None
                    and fm(frozen.get('before', ''), 'summary') != corrected.get('summary')
                    and not patch_draft_summary_candidate(
                        frozen, selected, Path(candidate), Path(manifest))):
                raise PublicationError('attested_draft_summary_update_failed')
            if (inventory_history_relocation_active
                    and not inventory_history_relocation_in_progress):
                from caretaker.draft_summary import _relocate_inventory_history_candidate
                if (not isinstance(inventory_history_prior_body, str)
                        or not _relocate_inventory_history_candidate(
                            frozen, Path(candidate), Path(manifest),
                            inventory_history_prior_body, observed,
                        )):
                    raise PublicationError('attested_inventory_history_relocation_failed')
            return manifest

        _curator.build_candidate = _build_candidate_with_draft_summary

        def _format_error_with_invalid_summary(reason):
            return (str(reason) == 'invalid summary'
                    or original_is_format_error(reason))

        def _summary_only_repair_prompt(frozen, error_code, details=''):
            nonlocal summary_only_mode, inventory_source_repair_prompt_used
            if (str(error_code) == 'review_REPAIR'
                    and inventory_source_repair_mode
                    and not inventory_source_repair_prompt_used):
                from caretaker.publication import PROMPT_BYTE_LIMIT, PublicationError
                base_prompt = original_repair_prompt(frozen, error_code, details)
                source_guidance = (
                    '\n\nSOURCE-MISATTRIBUTION CORRECTION. Correct only the new current '
                    'section using source IDs attached to the current proposal and the '
                    'current source text. If a claim lacks support in those sources, '
                    'narrow it, correct it, or remove it. Do not import citation IDs '
                    'from the frozen before text. The before text and its citations are '
                    'retained verbatim in the separate Historical evidence section; do '
                    'not copy that historical claim into the current section. A source '
                    'that says names, addresses, and contacts should not be added to the '
                    'Wiki does not establish that all personal data remained outside '
                    'the entire Hub. Preserve the actual scope stated by the source.'
                )
                repaired_prompt = base_prompt + source_guidance
                if len(repaired_prompt.encode('utf-8')) > PROMPT_BYTE_LIMIT:
                    raise PublicationError('inventory_source_repair_prompt_exceeds_bound')
                inventory_source_repair_prompt_used = True
                return repaired_prompt
            if (str(error_code) != 'invalid summary'
                    or not isinstance(summary_repair_decision, dict)):
                return original_repair_prompt(frozen, error_code, details)
            from caretaker.publication import PROMPT_BYTE_LIMIT, PublicationError
            body_and_dispositions = {
                'body': summary_repair_decision.get('body'),
                'dispositions': summary_repair_decision.get('dispositions'),
                'source_refs': sorted({
                    source_id for proposal in frozen.get('proposals', [])
                    for source_id in proposal.get('source_ids', [])
                }),
            }
            text = (
                'SUMMARY-ONLY FORMAT REPAIR. The parsed publication decision already '
                'passed all checks except its summary field. Return exactly one JSON '
                'object with the single key "summary" and a string value from 1 to '
                '300 characters. Use only the validated decision content below. Do '
                'not return or change body, dispositions, citations, or source refs; '
                'do not invent facts or truncate other content.\n'
                'DEFECT: invalid summary\n'
                'VALIDATED DECISION CONTENT:\n'
                + json.dumps(body_and_dispositions, ensure_ascii=False)
            )
            if len(text.encode('utf-8')) > PROMPT_BYTE_LIMIT:
                raise PublicationError('summary_repair_prompt_exceeds_bound')
            summary_only_mode = True
            return text

        def _summary_only_fleet_rule():
            if not summary_only_mode:
                return original_fleet_maker_rule()
            return (
                '\n\nANSWER PLACEMENT. Agent Fleet requires its normal handoff envelope. '
                'The handoff summary must be exactly one JSON object with only the key '
                '"summary" and a string value between 1 and 300 characters. Do not put '
                'the full publication decision in the handoff summary; do not add prose '
                'or markdown.'
            )

        def _decision_from_summary_only_repair(env):
            nonlocal summary_repair_decision, summary_only_mode
            if not summary_only_mode:
                return original_decision_from_maker(env)
            original = summary_repair_decision
            if not isinstance(original, dict):
                raise PublicationError('summary_format_repair_proof_missing')
            from caretaker.publication import response_object
            try:
                parsed = response_object(env.final)
            except (TypeError, ValueError) as exc:
                raise PublicationError('summary_format_repair_response_invalid') from exc
            value = None
            if isinstance(parsed, dict) and set(parsed) == {'summary'}:
                value = parsed['summary']
            elif isinstance(parsed, dict) and isinstance(parsed.get('summary'), str):
                # Fleet wraps the requested one-field JSON inside handoff.summary.
                try:
                    nested = response_object(parsed['summary'])
                except (TypeError, ValueError):
                    nested = None
                if isinstance(nested, dict) and set(nested) == {'summary'}:
                    value = nested['summary']
            if not isinstance(value, str) or not 1 <= len(value) <= 300:
                raise PublicationError('invalid summary')
            from caretaker.filter import contains_secret
            if contains_secret(value):
                raise PublicationError('summary_format_repair_unsafe')
            repaired = dict(original)
            repaired['summary'] = value
            summary_repair_decision = None
            summary_only_mode = False
            return repaired

        _curator.is_format_error = _format_error_with_invalid_summary
        _curator.repair_prompt = _summary_only_repair_prompt
        _curator._fleet_maker_rule = _summary_only_fleet_rule
        _curator._decision_from_maker = _decision_from_summary_only_repair

        def _command_with_publish_timeout_provenance(argv, *args, **kwargs):
            try:
                return original_command(argv, *args, **kwargs)
            except subprocess.TimeoutExpired as exc:
                if (isinstance(argv, (list, tuple)) and len(argv) == 14
                        and argv[0] == curator_cfg.hub.python_executable
                        and argv[1:6] == ['-B', '-X', 'utf8', str(expected_engine), 'publish']
                        and argv[6] == '--manifest' and argv[8] == '--review'
                        and argv[10] == '--lock-id' and argv[12] == '--agent'
                        and argv[13] == 'openclaw-caretaker'):
                    setattr(exc, '_caretaker_canonical_publish_command', True)
                raise

        _curator.command = _command_with_publish_timeout_provenance
        original_save_phase = _curator._save_phase_progress
        original_load_phase = _curator._load_phase_progress

        def _read_phase_pointer(cfg, proposal_paths):
            phase_state = Path(cfg.state_dir) / 'curation'
            pointer = _curator._phase_pointer(
                phase_state, cfg.project_slug, proposal_paths)
            safe_pointer = _curator._phase_regular_file(
                phase_state, pointer.name, limit=_curator._PHASE_CACHE_LIMIT)
            value = json.loads(safe_pointer.read_text(encoding='utf-8'))
            if (not isinstance(value, dict)
                    or value.get('cache_sha256') != _curator._phase_digest(value)):
                return None, None
            return safe_pointer, value

        def _save_phase_with_repair_checkpoint(
                cfg, proposal_paths, frozen, run_root, observed, phase,
                candidate, manifest, progress):
            nonlocal safe_repair_phase, started_repair_phase
            result = original_save_phase(
                cfg, proposal_paths, frozen, run_root, observed, phase,
                candidate, manifest, progress)
            try:
                if phase == 'REPAIR_REQUIRED' and candidate is not None and manifest is not None:
                    pointer, record = _read_phase_pointer(cfg, proposal_paths)
                    saved_progress = record.get('progress') if record else None
                    assessment = (saved_progress.get('assessment')
                                  if isinstance(saved_progress, dict) else None)
                    review_response = (saved_progress.get('review_response')
                                      if isinstance(saved_progress, dict) else None)
                    if (record is not None and record.get('phase') == 'REPAIR_REQUIRED'
                            and isinstance(assessment, dict)
                            and assessment.get('verdict') == 'REPAIR'
                            and isinstance(review_response, dict)
                            and isinstance(review_response.get('provider'), str)
                            and isinstance(review_response.get('model'), str)
                            and isinstance(review_response.get('final'), str)
                            and isinstance(record.get('manifest_sha256'), str)
                            and isinstance(record.get('pins'), dict)
                            and bool(record['pins'])):
                        safe_repair_phase = (pointer, record)
                elif phase == 'REPAIR_STARTED':
                    pointer, record = _read_phase_pointer(cfg, proposal_paths)
                    if record is not None and record.get('phase') == 'REPAIR_STARTED':
                        started_repair_phase = (pointer, record)
            except Exception:
                # Capturing retry context is opportunistic. The ordinary
                # phase loader remains authoritative if a timeout occurs.
                pass
            return result

        def _load_phase_with_budget_retry(cfg, proposal_paths, state, frozen):
            nonlocal safe_repair_phase
            nonlocal inventory_history_relocation_active
            nonlocal inventory_history_relocation_in_progress
            nonlocal inventory_history_prior_body
            nonlocal inventory_source_repair_mode
            active_error = sys.exc_info()[1]
            if (getattr(active_error, '_caretaker_budget_clipped_timeout', False) is True
                    and safe_repair_phase is not None
                    and started_repair_phase is not None):
                try:
                    pointer, record = _read_phase_pointer(cfg, proposal_paths)
                    expected_pointer, started = started_repair_phase
                    safe_pointer, safe = safe_repair_phase
                    safe_progress = safe.get('progress')
                    started_progress = (started.get('progress')
                                        if isinstance(started, dict) else None)
                    safe_assessment = (safe_progress.get('assessment')
                                       if isinstance(safe_progress, dict) else None)
                    if (pointer == expected_pointer == safe_pointer
                            and record is not None and isinstance(started, dict)
                            and record.get('phase') == 'REPAIR_STARTED'
                            and record.get('cache_sha256') == started.get('cache_sha256')
                            and record.get('code_version') == started.get('code_version')
                            and record.get('project') == safe.get('project') == cfg.project_slug
                            and record.get('paths') == safe.get('paths') == sorted(proposal_paths)
                            and record.get('run_root') == safe.get('run_root')
                            and record.get('binding') == safe.get('binding')
                            and record.get('code_version') == safe.get('code_version')
                            and isinstance(safe_assessment, dict)
                            and safe_assessment.get('verdict') == 'REPAIR'
                            and isinstance(started_progress, dict)
                            and started_progress.get('assessment') == safe_assessment
                            and started_progress.get('repair_attempt') == 1):
                        # A budget-clipped model call has no completed answer.
                        # Reinstall only the immediately preceding REPAIR_REQUIRED
                        # checkpoint; the ordinary loader then revalidates all
                        # bindings, source/target preimages, pins and journal state.
                        _curator._durable_json(pointer, safe)
                except Exception:
                    # Fail closed: leave the terminal phase for the ordinary
                    # loader to reject rather than manufacture retry evidence.
                    pass
            marker = _validated_summary_retry_marker(cfg, proposal_paths)
            if marker is not None:
                from caretaker.repaired_retry import INVENTORY_HISTORY_RETRY_REASON
                if marker.get('reason') == INVENTORY_HISTORY_RETRY_REASON:
                    from caretaker.draft_summary import (
                        archive_and_relocate_inventory_history,
                        inventory_history_repair_plan,
                    )
                    plan = inventory_history_repair_plan(
                        cfg, sorted(proposal_paths), frozen, marker['old_result'],
                    )
                    rerender = archive_and_relocate_inventory_history
                else:
                    from caretaker.draft_summary import (
                        archive_and_rerender, terminal_summary_retry_plan,
                    )
                    plan = terminal_summary_retry_plan(
                        Path(cfg.hub.vault), Path(cfg.state_dir), frozen,
                        marker['old_result'], project=cfg.project_slug,
                        paths=sorted(proposal_paths),
                    )
                    rerender = lambda plan, frozen, cfg, paths, build, save: archive_and_rerender(
                        plan, frozen, cfg, paths, build, save, terminal_retry=True,
                    )
                if plan is not None:
                    inventory_history_prior_body = plan.get('prior_inventory_body')
                    inventory_source_repair_mode = (
                        plan.get('source_misattribution_repair') is True
                    )
                    inventory_history_relocation_in_progress = True
                    try:
                        rerendered = rerender(
                            plan, frozen, cfg, sorted(proposal_paths),
                            _curator.build_candidate, original_save_phase,
                        )
                    finally:
                        inventory_history_relocation_in_progress = False
                    if rerendered is not None:
                        inventory_history_relocation_active = True
                        return original_load_phase(
                            cfg, proposal_paths, state, frozen,
                        )
            from caretaker import prepared_rebase
            loaded = prepared_rebase.load_with_prepared_rebase(
                original_load_phase, cfg, proposal_paths, state, frozen)
            if loaded is not None:
                from caretaker.draft_summary import (
                    archive_and_rerender, prepared_summary_repair_plan,
                )
                plan = prepared_summary_repair_plan(
                    cfg, proposal_paths, frozen, loaded,
                )
                if plan is not None:
                    inventory_history_prior_body = plan.get('prior_inventory_body')
                    inventory_history_relocation_in_progress = True
                    try:
                        rerendered = archive_and_rerender(
                            plan, frozen, cfg, sorted(proposal_paths),
                            _curator.build_candidate, original_save_phase,
                        )
                    finally:
                        inventory_history_relocation_in_progress = False
                    if rerendered is None:
                        raise PublicationError('attested_draft_summary_rerender_failed')
                    inventory_history_relocation_active = True
                    loaded = prepared_rebase.load_with_prepared_rebase(
                        original_load_phase, cfg, proposal_paths, state, frozen,
                    )
            if loaded is not None:
                from caretaker.draft_summary import (
                    archive_and_relocate_inventory_history,
                    prepared_inventory_history_plan,
                )
                plan = prepared_inventory_history_plan(
                    cfg, proposal_paths, frozen, loaded,
                )
                if plan is not None:
                    inventory_history_prior_body = plan.get('prior_inventory_body')
                    inventory_history_relocation_in_progress = True
                    try:
                        rerendered = archive_and_relocate_inventory_history(
                            plan, frozen, cfg, sorted(proposal_paths),
                            _curator.build_candidate, original_save_phase,
                        )
                    finally:
                        inventory_history_relocation_in_progress = False
                    if rerendered is None:
                        raise PublicationError('inventory_history_relocation_failed')
                    inventory_history_relocation_active = True
                    loaded = prepared_rebase.load_with_prepared_rebase(
                        original_load_phase, cfg, proposal_paths, state, frozen,
                    )
            if (loaded is not None and loaded.get('phase') == 'REPAIR_REQUIRED'
                    and safe_repair_phase is None):
                try:
                    pointer, record = _read_phase_pointer(cfg, proposal_paths)
                    progress = record.get('progress') if record else None
                    assessment = (progress.get('assessment')
                                  if isinstance(progress, dict) else None)
                    review_response = (progress.get('review_response')
                                      if isinstance(progress, dict) else None)
                    if (record is not None and isinstance(assessment, dict)
                            and assessment.get('verdict') == 'REPAIR'
                            and isinstance(review_response, dict)
                            and isinstance(review_response.get('provider'), str)
                            and isinstance(review_response.get('model'), str)
                            and isinstance(review_response.get('final'), str)
                            and isinstance(record.get('manifest_sha256'), str)
                            and isinstance(record.get('pins'), dict)
                            and bool(record['pins'])):
                        safe_repair_phase = (pointer, record)
                except Exception:
                    pass
            return loaded

        _curator._save_phase_progress = _save_phase_with_repair_checkpoint
        _curator._load_phase_progress = _load_phase_with_budget_retry

        marker = (curator_cfg.raw or {}).get('maintenance_terminal_generation_retry')
        if (isinstance(marker, dict)
                and marker.get('project') == curator_cfg.project_slug
                and marker.get('paths') == sorted(paths)):
            validated = ({'plan': _transport_recovery} if isinstance(_transport_recovery, dict)
                         else _validated_terminal_generation_marker(curator_cfg, paths))
            if not isinstance(validated, dict) or not isinstance(validated.get('plan'), dict):
                raise PublicationError('terminal_generation_retry_marker_invalid')
            from caretaker.terminal_generation import prepare_terminal_generation_resume

            plan = validated['plan']
            terminal_generation_plan_value = plan
            inventory_history_prior_body = plan.get('prior_inventory_body')
            inventory_history_relocation_active = True
            inventory_source_repair_mode = True
            terminal_generation_active = True
            if not plan.get('continuation'):
                prepared = prepare_terminal_generation_resume(
                    plan, curator_cfg, sorted(paths), plan['frozen'], original_save_phase,
                )
                if prepared is None:
                    raise PublicationError('terminal_generation_resume_prepare_failed')
                pointer, record = _read_phase_pointer(curator_cfg, sorted(paths))
                if (pointer is None or record is None
                        or record.get('phase') != 'REPAIR_REQUIRED'):
                    raise PublicationError('terminal_generation_resume_checkpoint_missing')
                terminal_generation_required_phase = (pointer, record)
            elif (isinstance(plan.get('current_record'), dict)
                  and plan['current_record'].get('phase') == 'REPAIR_REQUIRED'):
                terminal_generation_required_phase = (
                    plan.get('current_pointer'), plan['current_record'],
                )
            phase_record = (plan.get('current_record') if plan.get('continuation')
                            else terminal_generation_required_phase[1])

            def _deferred_terminal_generation(reason):
                from caretaker.publication import safe_file

                phase_state = Path(curator_cfg.state_dir) / 'curation'
                run_root = safe_file(phase_state, phase_record.get('run_root'))
                candidate_value = phase_record.get('candidate')
                manifest_value = phase_record.get('manifest')
                candidate = (safe_file(phase_state, candidate_value)
                             if isinstance(candidate_value, str) and candidate_value
                             else None)
                progress = phase_record.get('progress') or {}
                frozen = plan['frozen']
                proposals = frozen.get('proposals') or []
                return {
                    'status': 'DEFERRED_PREPARED', 'reason': reason,
                    'phase': phase_record.get('phase'),
                    'run_path': str(run_root),
                    'candidate_path': str(candidate) if candidate is not None else None,
                    'manifest_sha256': phase_record.get('manifest_sha256'),
                    'maker_calls': progress.get('maker_calls', 0),
                    'review_calls': progress.get('review_calls', 0),
                    'proposal_ids': [item.get('id') for item in proposals],
                    'paths': [item.get('path') for item in proposals],
                    'hashes': [item.get('sha256') for item in proposals],
                    'project': curator_cfg.project_slug,
                    'snapshot_sha256': frozen.get('snapshot_sha256'),
                    'target': frozen.get('target'),
                    'model_calls': 0, 'pre_effect': True,
                }

            phase_needs_fleet_maker = phase_record.get('phase') == 'REPAIR_REQUIRED'
            if plan.get('budget_continuation') is True:
                class _NoContinuationMaker:
                    def invoke(self, **kwargs):
                        raise PublicationError('budget_continuation_maker_forbidden')

                def _no_continuation_append_makers(cfg, transport_factory=None):
                    return [dict(item, transport=_NoContinuationMaker()) for item in
                            original_fleet_append_makers(cfg, transport_factory)]

                _curator._build_fleet_append_makers = _no_continuation_append_makers
            append_spec = getattr(curator_cfg.publication, 'append_maker', None)
            configured_makers = list(getattr(append_spec, 'candidates', []) or [])
            if phase_needs_fleet_maker and not configured_makers:
                return _deferred_terminal_generation(
                    'terminal_generation_fleet_maker_unavailable',
                )

            if phase_needs_fleet_maker:
                def _terminal_fleet_append_makers(cfg, transport_factory=None):
                    candidates = original_fleet_append_makers(cfg, transport_factory)
                    if not candidates:
                        raise _TerminalGenerationFleetUnavailable(
                            'fleet_append_maker_unavailable',
                        )
                    selected = dict(candidates[0])
                    transport = selected.get('transport')

                    class _TerminalFleetMaker:
                        def invoke(self, **kwargs):
                            try:
                                return transport.invoke(**kwargs)
                            except _TransportError as exc:
                                reason = getattr(exc, 'reason_code', 'transport_error')
                                if reason in {
                                        'fleet_runtime_unavailable',
                                        'fleet_profiles_unavailable',
                                        'native_prespawn_win206'}:
                                    raise _TerminalGenerationFleetUnavailable(reason) from exc
                                raise _TerminalGenerationMakerOutcomeUnknown(reason) from exc
                            except tick_budget.TickBudgetExceeded:
                                raise
                            except Exception as exc:
                                raise _TerminalGenerationMakerOutcomeUnknown(
                                    type(exc).__name__,
                                ) from exc

                    selected['transport'] = _TerminalFleetMaker()
                    return [selected]

                _curator._build_fleet_append_makers = _terminal_fleet_append_makers

        def _restore_terminal_generation_required_phase():
            if terminal_generation_required_phase is None:
                return False
            try:
                required_pointer, required_record = terminal_generation_required_phase
                pointer, record = _read_phase_pointer(curator_cfg, sorted(paths))
                if (pointer == required_pointer and record == required_record):
                    return True
                progress = record.get('progress') if isinstance(record, dict) else None
                required_progress = required_record.get('progress')
                if (pointer != required_pointer or not isinstance(progress, dict)
                        or not isinstance(required_progress, dict)
                        or record.get('phase') != 'REPAIR_STARTED'
                        or record.get('project') != required_record.get('project')
                        or record.get('paths') != required_record.get('paths')
                        or record.get('run_root') != required_record.get('run_root')
                        or record.get('binding') != required_record.get('binding')
                        or record.get('code_version') != required_record.get('code_version')
                        or progress.get('assessment') != required_progress.get('assessment')
                        or progress.get('maker_calls') != required_progress.get('maker_calls')
                        or progress.get('review_calls') != required_progress.get('review_calls')
                        or progress.get('repair_attempt') != 1
                        or any(progress.get(key) not in (None, '', {}, [])
                               for key in ('maker_response', 'review_response', 'final'))):
                    return False
                _curator._durable_json(required_pointer, required_record)
                return True
            except Exception:
                return False

        from curate_once import run
        try:
            continuation_kwargs = ({'maker': _NoContinuationMaker()}
                                   if terminal_generation_plan_value and terminal_generation_plan_value.get('budget_continuation') is True else {})
            return run(str(config_path), paths, True, maintenance_lock_held=True, **continuation_kwargs)
        except _TerminalGenerationFleetUnavailable as exc:
            if not _restore_terminal_generation_required_phase():
                return {
                    'status': 'UNKNOWN_REQUIRES_RECONCILIATION',
                    'reason': 'terminal_generation_pre_call_restore_unproven',
                    'model_calls': 0,
                }
            return _deferred_terminal_generation(str(exc))
        except _TerminalGenerationMakerOutcomeUnknown as exc:
            return {
                'status': 'UNKNOWN_REQUIRES_RECONCILIATION',
                'reason': 'terminal_generation_maker_outcome_unknown',
                'error_code': str(exc), 'model_calls': 1,
            }
    except tick_budget.TickBudgetExceeded:
        raise
    except subprocess.TimeoutExpired as exc:
        from caretaker.repaired_retry import prepared_publish_timeout_proof
        prepared = prepared_publish_timeout_proof(config_path, paths, exc)
        if prepared is not None:
            return prepared
        return {'status': 'BLOCKED', 'reason': 'TimeoutExpired'}
    except Exception as exc:
        if isinstance(exc, _TransportError):
            return {'status': 'BLOCKED', 'reason': exc.reason_code}
        if isinstance(exc, PublicationError):
            return {'status': 'BLOCKED', 'reason': str(exc)}
        return {'status': 'BLOCKED', 'reason': type(exc).__name__}
    finally:
        if _curator is not None and original_command is not None:
            _curator.command = original_command
        if _curator is not None and original_save_phase is not None:
            _curator._save_phase_progress = original_save_phase
        if _curator is not None and original_load_phase is not None:
            _curator._load_phase_progress = original_load_phase
        if _curator is not None and original_build_candidate is not None:
            _curator.build_candidate = original_build_candidate
        if _curator is not None and original_is_format_error is not None:
            _curator.is_format_error = original_is_format_error
        if _curator is not None and original_repair_prompt is not None:
            _curator.repair_prompt = original_repair_prompt
        if _curator is not None and original_decision_from_maker is not None:
            _curator._decision_from_maker = original_decision_from_maker
        if _curator is not None and original_fleet_maker_rule is not None:
            _curator._fleet_maker_rule = original_fleet_maker_rule
        if _curator is not None and original_fleet_append_makers is not None:
            _curator._build_fleet_append_makers = original_fleet_append_makers
        summary_repair_decision = None
        summary_only_mode = False
        if prior is None:
            os.environ.pop('HUB_CARETAKER_ALLOW_EXEC', None)
        else:
            os.environ['HUB_CARETAKER_ALLOW_EXEC'] = prior


def _prepared_phase_is_reviewable(phase, *, allow_repair=False):
    """Accept only a fully bound pre-effect phase that still needs review."""
    from curate_once import _LEGACY_PREPARED_CODE_VERSION
    from caretaker.repaired_retry import PHASE_RESUME_CODE_VERSION

    if not isinstance(phase, dict) or phase.get('phase') not in {
            'PREPARED', 'REPAIR_PREPARED'}:
        return False
    if phase.get('status') in {
            'UNKNOWN', 'UNKNOWN_REQUIRES_RECONCILIATION', 'RUNNING',
            'PUBLISHED', 'ACCOUNTED', 'accounted_elsewhere', 'unsafe_source'}:
        return False
    phase_name = phase.get('phase')
    if phase_name == 'PREPARED':
        return bool(
            phase.get('code_version') in {
                _LEGACY_PREPARED_CODE_VERSION,
                '6d74ee300e9032e35c27ced6fd2f274e',
                PHASE_RESUME_CODE_VERSION,
            }
            and phase.get('maker_calls') == 1
            and phase.get('review_calls') == 0
            and phase.get('review_rounds', []) == []
            and phase.get('reviewer_format_repairs', {}) == {}
            and phase.get('assessment') is None
            and not phase.get('review_response')
            and not phase.get('fleet_review_path')
        )
    if allow_repair is not True:
        return False
    try:
        assessment = phase.get('assessment')
        rounds = phase.get('review_rounds')
        if (phase.get('code_version') != PHASE_RESUME_CODE_VERSION
                or type(phase.get('maker_calls')) is not int
                or phase['maker_calls'] < 2
                or type(phase.get('review_calls')) is not int
                or phase['review_calls'] < 1
                or phase.get('repair_attempt') != 1
                or not isinstance(assessment, dict)
                or assessment.get('verdict') != 'REPAIR'
                or not isinstance(assessment.get('manifest_sha256'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', assessment['manifest_sha256'])
                or not isinstance(assessment.get('findings'), list)
                or not isinstance(rounds, list) or not rounds
                or not isinstance(rounds[-1], dict)
                or rounds[-1].get('verdict') != 'REPAIR'
                or not isinstance(phase.get('maker_provider'), str)
                or not isinstance(phase.get('maker_model'), str)
                or not isinstance(phase.get('maker_route'), str)
                or not phase.get('maker_route')
                or not isinstance(phase.get('manifest_sha256'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', phase['manifest_sha256'])):
            return False
        return True
    except Exception:
        return False


def publication_tick(config_path, cfg, control, database, now=None,
                     lane='legacy', eligible_filter=None,
                     prepare_priority_seen_key=None,
                     _hub_preflight_state=None,
                     _require_committed_inputs=False):
    """Drain at most one due batch for this project from the frozen audit cycle.

    Empty work never invokes a model. Postponed/quarantined batches are
    skipped so other frozen work can advance. Publication state is recovered
    from canonical events/journals after a crash; status strings are not proof.
    """
    policy_raw = control.get('publication', {}) or {}
    if policy_raw.get('enabled') is not True:
        return {'status': 'DISABLED'}
    if _hub_preflight_state is not None:
        if type(_hub_preflight_state) is not dict:
            return _deferred_hub_preflight({'reason': 'hub_preflight_invalid'})
        cached_preflight = _hub_preflight_state.get('result')
        if cached_preflight is not None and not _valid_hub_preflight(cached_preflight):
            return _deferred_hub_preflight(cached_preflight)
    from caretaker.audit import (
        accounted_proposal_ids, active_cycle, apply_oversize_blocks,
        ensure_tables, group_key, mark_member, mark_members, maybe_start_cycle, policy_from,
        project_batches, partition_batch_members, source_metadata_index, summarize,
    )
    from caretaker.publication import PublicationError, freeze
    from caretaker.registry import main_page, read_registry
    from caretaker.pubstate import (
        batch_key, canonical_verdict, code_version, due_to_run,
        ensure_pub_tables, fingerprint, read_row, reconcile_all_running,
        reconcile_running, record_budget_deferred, record_failure,
        record_prepared_deferred,
        record_start, record_success,
    )
    vault = Path(cfg['hub']['vault'])
    registry, _ = read_registry(vault)
    project = cfg['project_slug']
    if not _publication_project_allowed(project, policy_raw):
        return {'status':'HELD_PROJECT_SCOPE', 'project':project, 'model_calls':0}
    policy = policy_from(policy_raw)
    moment = now or dt.datetime.now(dt.timezone.utc)
    now_iso = moment.isoformat()
    ensure_tables(database)
    ensure_pub_tables(database)
    if lane == 'legacy':
        reconcile_all_running(database, vault)
    runtime_root = _runtime_state_root_from_cfg(cfg, lane)

    if lane == 'legacy' and not _pending_checkpoint_rows(database, lane):
        from caretaker.prespawn206 import legacy206_plan, resume_legacy206, budget206_plan, resume_budget206
        # Reserved UNKNOWN work cannot enter the fresh-source planner. Only
        # this exact positive pre-spawn proof may resume its existing generation.
        unknown_rows = database.execute(
            "SELECT batch FROM publications WHERE status='UNKNOWN_REQUIRES_RECONCILIATION'")
        for unknown_key in list(unknown_rows):
            unknown_row = read_row(database, unknown_key[0])
            try:
                unknown_value = json.loads(unknown_row.get('result') or '{}')
            except (TypeError, ValueError):
                continue
            if unknown_value.get('project') != project:
                continue
            plan206 = legacy206_plan(config_path, database, unknown_row)
            if plan206 is None:
                plan206 = budget206_plan(config_path, database, unknown_row)
            if plan206 is not None:
                if _require_committed_inputs:
                    from caretaker.committed_inputs import inputs_match_head
                    if inputs_match_head(plan206['frozen']) is not True:
                        return {'status': 'BLOCKED', 'reason': 'selected_inputs_not_in_head',
                                'project': project, 'model_calls': 0}
                recovery_preflight = ((_hub_preflight_state or {}).get('result')
                                      or _global_hub_preflight(cfg))
                if not _valid_hub_preflight(recovery_preflight):
                    return _deferred_hub_preflight(recovery_preflight)
                resume206 = resume_budget206 if plan206.get('budget_continuation') is True else resume_legacy206
                resumed206 = resume206(plan206, config_path, database, _invoke_curate)
                if resumed206 is not None:
                    return resumed206

    def _attestation_state_for_project(retry_project):
        owner = _canonical_project_runtime_state(
            runtime_root, retry_project, lane, database,
        )
        return owner / 'projects' / retry_project / 'state'

    def _summary_preflight(retry_project, value, _stored_fp):
        return _summary_repair_preflight(
            vault, _attestation_state_for_project(retry_project),
            retry_project, value,
        )

    def _phase_config_for_project(retry_project):
        if retry_project != project:
            raise ValueError('recovery_project_mismatch')
        from caretaker.config import load_config
        return load_config(config_path)

    (_inventory_history_preflight, _terminal_generation_source_preflight,
     terminal_generation_plans) = _publication_recovery_preflights(
        vault, policy, database, lane, _phase_config_for_project,
    )

    def _terminal_generation_candidate_preflight(old, metas):
        if not isinstance(old, dict) or not isinstance(old.get('result'), dict):
            return None
        batch = old.get('batch')
        plan = terminal_generation_plans.get(batch)
        if plan is None and _terminal_generation_source_preflight(
                old.get('project'), old['result'], old.get('fingerprint')):
            plan = terminal_generation_plans.get(batch)
        expected = set(zip(old['result'].get('proposal_ids') or [],
                           old['result'].get('paths') or [],
                           old['result'].get('hashes') or []))
        actual = {(meta.get('proposal_id'), meta.get('path'), meta.get('sha256'))
                  for meta in metas if isinstance(meta, dict)
                  and meta.get('project') == old.get('project')}
        return plan if plan is not None and expected == actual else None

    def _terminal_generation_pending_preflight(old, identity, cycle_id):
        plan = terminal_generation_plans.get(old.get('batch')) if isinstance(old, dict) else None
        return bool(plan and plan.get('cycle_mode') == 'reuse'
                    and plan.get('pending_cycle') == cycle_id)

    try:
        summary = _audit_summary(
            vault, policy, database, moment, eligible_filter,
            attestation_state_dir_for_project=_attestation_state_for_project,
            summary_repair_preflight=_summary_preflight,
            inventory_history_preflight=_inventory_history_preflight,
            terminal_generation_preflight=_terminal_generation_source_preflight,
        )
    except PublicationError as exc:
        out = {'status': 'BLOCKED', 'reason': str(exc)}
        out.update(_empty_audit_fields())
        return out
    tick_budget.check('publication_audit')
    fields = _audit_fields(summary)
    if lane == 'legacy' and policy_raw.get('include_existing_hub_backlog') is not True:
        fields['status'] = 'HUB_BACKLOG_NOT_INCLUDED'
        fields['model_calls'] = 0
        return fields
    version = code_version()
    if not _pending_checkpoint_rows(database, lane):
        from caretaker.repaired_retry import repaired_retry_candidates

        def _retry_source_preflight(meta):
            try:
                freeze(
                    vault, [meta['path']], main_page(meta['project'], registry),
                    attestation_state_dir=_attestation_state_for_project(meta['project']),
                )
                return True
            except Exception:
                return False

        def _summary_retry_candidate_preflight(old):
            return _summary_repair_preflight(
                vault, _attestation_state_for_project(old['project']),
                old['project'], old['result'],
            )

        stale_summary_cycles = {}

        def _summary_retry_stale_pending_preflight(old, identity, cycle_id):
            """Qualify only a spent, exact, never-started summary retry cycle."""
            try:
                from caretaker.draft_summary import helper_sha256
                from caretaker.pubstate import canonical_verdict
                from caretaker.repaired_retry import (
                    REPAIRED_PUBLICATION_SHA256, SUMMARY_REPAIR_REASON,
                    SUMMARY_REPAIR_SOURCE_REASON,
                    retry_batch_key, retry_token,
                )

                if (old.get('reason') != SUMMARY_REPAIR_SOURCE_REASON
                        or not isinstance(cycle_id, str)):
                    return False
                cycle_row = database.execute(
                    'SELECT lane,status,counts FROM audit_cycles WHERE id=?', (cycle_id,),
                ).fetchone()
                if cycle_row is None or cycle_row[0] != lane or cycle_row[1] != 'ACTIVE':
                    return False
                counts = json.loads(cycle_row[2] or '{}')
                retries = counts.get('repaired_retry') if isinstance(counts, dict) else None
                expected = set(zip(old.get('proposal_ids') or [], old.get('paths') or [],
                                   old.get('hashes') or []))
                if (not expected or identity not in expected
                        or not isinstance(retries, list) or len(retries) != len(expected)):
                    return False
                if any(not isinstance(item, dict) for item in retries):
                    return False
                retry_ids = {
                    (item.get('proposal_id'), item.get('path'), item.get('sha256'))
                    for item in retries
                }
                current_helper = helper_sha256()
                current_version = code_version()
                if retry_ids != expected or len(retry_ids) != len(retries):
                    return False
                for item in retries:
                    old_helper = item.get('helper_sha256')
                    if (item.get('reason') != SUMMARY_REPAIR_REASON
                            or item.get('project') != old.get('project')
                            or item.get('previous_batch') != old.get('batch')
                            or item.get('publication_sha256') != REPAIRED_PUBLICATION_SHA256
                            or item.get('code_version') != current_version
                            or not isinstance(old_helper, str)
                            or not re.fullmatch(r'[a-f0-9]{64}', old_helper)
                            or old_helper == current_helper
                            or item.get('token') != retry_token(
                                REPAIRED_PUBLICATION_SHA256, old['project'],
                                item['proposal_id'], item['path'], item['sha256'],
                                reason=SUMMARY_REPAIR_REASON, helper_sha256=old_helper)):
                        return False
                    token_row = database.execute(
                        'SELECT cycle_id,publication_sha256,code_version,proposal_id,path,sha256,previous_batch '
                        'FROM repaired_retry_tokens WHERE token=?', (item['token'],),
                    ).fetchone()
                    if token_row != (
                            cycle_id, REPAIRED_PUBLICATION_SHA256, current_version,
                            item['proposal_id'], item['path'], item['sha256'], old['batch']):
                        return False
                members = database.execute(
                    'SELECT proposal_id,path,sha256,project,status FROM audit_members WHERE cycle_id=?',
                    (cycle_id,),
                ).fetchall()
                if (len(members) != len(expected)
                        or {(row[0], row[1], row[2]) for row in members} != expected
                        or any(row[3] != old['project'] or row[4] != 'pending' for row in members)):
                    return False
                stale_batch = retry_batch_key(old['project'], [
                    {'proposal_id': pid, 'path': path, 'sha256': digest}
                    for pid, path, digest in sorted(expected)
                ], retries)
                if database.execute(
                        'SELECT 1 FROM publications WHERE batch=?', (stale_batch,)).fetchone():
                    return False
                state_dir = _attestation_state_for_project(old['project'])
                absent, _evidence = canonical_verdict(vault, {
                    'batch': stale_batch,
                    'result': json.dumps({
                        'status': 'RUNNING', 'proposal_ids': [pid for pid, _path, _sha in sorted(expected)],
                        'paths': [path for _pid, path, _sha in sorted(expected)],
                        'hashes': [digest for _pid, _path, digest in sorted(expected)],
                        'state_dir': str(state_dir),
                    }),
                })
                if absent != 'absent':
                    return False
                stale_summary_cycles.setdefault(cycle_id, set()).add(identity)
                return True
            except Exception:
                return False

        def _pre_candidate_summary_format_retry_preflight(old):
            try:
                from caretaker.publication import safe_file, sha

                project = old.get('project')
                paths = old.get('paths')
                proposal_ids = old.get('proposal_ids')
                hashes = old.get('hashes')
                if (not isinstance(project, str) or not isinstance(paths, list)
                        or not isinstance(proposal_ids, list)
                        or not isinstance(hashes, list)
                        or not paths or len(paths) != len(proposal_ids)
                        or len(paths) != len(hashes)):
                    return False
                target = main_page(project, registry)
                frozen = freeze(
                    vault, paths, target,
                    attestation_state_dir=_attestation_state_for_project(project),
                )
                expected = set(zip(proposal_ids, paths, hashes))
                current = {
                    (item.get('id'), item.get('path'), item.get('sha256'))
                    for item in frozen.get('proposals', [])
                    if isinstance(item, dict)
                }
                if not expected or current != expected or len(current) != len(paths):
                    return False
                page = safe_file(vault, target)
                context_hash = sha(page.read_bytes()) if page.is_file() else 'missing_target'
                current_fp = fingerprint(
                    project, paths, hashes + [context_hash], policy, code_version(),
                )
                return current_fp == old.get('fingerprint')
            except Exception:
                return False

        def _phase_cache_preflight(retry_project, metas):
            try:
                from curate_once import _load_phase_progress
                from caretaker import prepared_rebase
                from caretaker.config import load_config

                if (not metas or any(not isinstance(meta, dict)
                                     or meta.get('project') != retry_project for meta in metas)):
                    return False
                paths = [meta.get('path') for meta in metas]
                if (any(not isinstance(path, str) for path in paths)
                        or len(set(paths)) != len(paths)):
                    return False
                paths = sorted(paths)
                phase_cfg = load_config(config_path)
                if phase_cfg.project_slug != retry_project:
                    return False
                phase_state = Path(phase_cfg.state_dir)
                frozen = freeze(
                    vault, paths, main_page(retry_project, registry),
                    attestation_state_dir=phase_state,
                )
                phase = prepared_rebase.load_with_prepared_rebase(
                    _load_phase_progress, phase_cfg, paths,
                    phase_state / 'curation', frozen,
                )
                from caretaker.repaired_retry import NO_INDEPENDENT_REVIEWER_PHASE_REASON
                allow_repair = metas[0].get('_phase_resume_reason') == (
                    NO_INDEPENDENT_REVIEWER_PHASE_REASON
                )
                return _prepared_phase_is_reviewable(phase, allow_repair=allow_repair)
            except Exception:
                return False

        def _no_reviewer_prepared_defer(value, batch):
            """Preserve only the exact valid pre-effect phase after reviewer collision."""
            try:
                from curate_once import _load_phase_progress, _model_lineage
                from caretaker import prepared_rebase
                from caretaker.config import load_config
                from caretaker.repaired_retry import NO_INDEPENDENT_REVIEWER_PHASE_REASON

                if (not isinstance(value, dict) or value.get('status') != 'PENDING'
                        or value.get('reason') != NO_INDEPENDENT_REVIEWER_PHASE_REASON
                        or value.get('error_code') != NO_INDEPENDENT_REVIEWER_PHASE_REASON
                        or any(value.get(name) not in (None, '', {}, []) for name in (
                            'publication', 'checkpoint', 'event_id', 'manifest',
                            'manifest_sha256', 'ledger_path', 'publication_event',
                            'transaction',
                        ))):
                    return None
                phase_cfg = load_config(config_path)
                if phase_cfg.project_slug != project:
                    return None
                phase_state = Path(phase_cfg.state_dir)
                phase_paths = value.get('paths')
                if (not isinstance(phase_paths, list) or not phase_paths
                        or any(not isinstance(path, str) for path in phase_paths)
                        or sorted(phase_paths) != phase_paths
                        or len(set(phase_paths)) != len(phase_paths)):
                    return None
                frozen = freeze(
                    vault, phase_paths, main_page(project, registry),
                    attestation_state_dir=phase_state,
                )
                phase = prepared_rebase.load_with_prepared_rebase(
                    _load_phase_progress, phase_cfg, phase_paths,
                    phase_state / 'curation', frozen,
                )
                if not _prepared_phase_is_reviewable(phase, allow_repair=True):
                    return None
                expected = {
                    (item.get('id'), item.get('path'), item.get('sha256'))
                    for item in frozen.get('proposals', []) if isinstance(item, dict)
                }
                supplied = set(zip(
                    value.get('proposal_ids') or [],
                    phase_paths,
                    value.get('hashes') or [],
                ))
                if (not expected or supplied != expected
                        or len(supplied) != len(phase_paths)
                        or value.get('target') != frozen.get('target')
                        or value.get('maker_calls') != phase.get('maker_calls')
                        or value.get('review_calls') != phase.get('review_calls')
                        or value.get('maker_agent') != phase.get('maker_provider')
                        or value.get('primary_model') != phase.get('maker_model')
                        or value.get('maker_lineage') != _model_lineage(
                            phase.get('maker_provider'), phase.get('maker_model'))):
                    return None
                candidate_path = value.get('candidate_path')
                run_path = value.get('run_path')
                if (not isinstance(candidate_path, str) or not isinstance(run_path, str)
                        or Path(candidate_path).resolve()
                        != Path(phase.get('candidate_path', '')).resolve()
                        or Path(run_path).resolve()
                        != Path(phase.get('run_root_path', '')).resolve()):
                    return None
                row = read_row(database, batch)
                if row is None or row.get('status') != 'RUNNING':
                    return None
                verdict, _evidence = canonical_verdict(vault, row)
                if verdict != 'absent':
                    return None
                return {
                    'status': 'DEFERRED_PREPARED',
                    'reason': NO_INDEPENDENT_REVIEWER_PHASE_REASON,
                    'error_code': NO_INDEPENDENT_REVIEWER_PHASE_REASON,
                    'phase': phase['phase'],
                    'candidate_path': phase['candidate_path'],
                    'run_path': phase['run_root_path'],
                    'manifest_sha256': phase['manifest_sha256'],
                    'maker_calls': phase['maker_calls'],
                    'review_calls': phase['review_calls'],
                    'pre_effect': True,
                }
            except Exception:
                return None

        repaired = repaired_retry_candidates(
            database, summary, lane, _publication_scope_predicate(policy_raw), vault,
            source_preflight=_retry_source_preflight,
            publication_path=Path(__file__).resolve().parent / 'caretaker' / 'publication.py',
            curate_path=Path(__file__).resolve().parent / 'curate_once.py',
            phase_cache_preflight=_phase_cache_preflight,
            summary_repair_preflight=_summary_retry_candidate_preflight,
            summary_repair_path=Path(__file__).resolve().parent / 'caretaker' / 'draft_summary.py',
            inventory_history_preflight=lambda old: _inventory_history_preflight(
                old.get('project'), old.get('result'), old.get('fingerprint'),
            ),
            inventory_history_path=Path(__file__).resolve().parent / 'caretaker' / 'draft_summary.py',
            pre_candidate_summary_format_preflight=_pre_candidate_summary_format_retry_preflight,
            pre_candidate_summary_format_paths=(
                Path(__file__).resolve(),
                Path(__file__).resolve().parent / 'caretaker' / 'repaired_retry.py',
                Path(__file__).resolve().parent / 'curate_once.py',
                Path(__file__).resolve().parent / 'caretaker' / 'draft_summary.py',
            ),
            stale_pending_preflight=_summary_retry_stale_pending_preflight,
            terminal_generation_preflight=_terminal_generation_candidate_preflight,
            terminal_generation_path=(
                Path(__file__).resolve().parent / 'caretaker' / 'terminal_generation.py'
            ),
            terminal_generation_pending_preflight=_terminal_generation_pending_preflight,
        )
        if repaired:
            from caretaker.repaired_retry import (
                INVALID_SUMMARY_FORMAT_RETRY_REASON, INVENTORY_HISTORY_RETRY_REASON,
                SUMMARY_REPAIR_REASON, TERMINAL_GENERATION_RETRY_REASON,
            )
            summary_repairs = [item for item in repaired
                               if item.get('reason') == SUMMARY_REPAIR_REASON]
            format_repairs = [item for item in repaired
                              if item.get('reason') == INVALID_SUMMARY_FORMAT_RETRY_REASON]
            inventory_repairs = [item for item in repaired
                                 if item.get('reason') == INVENTORY_HISTORY_RETRY_REASON]
            terminal_generation_repairs = [item for item in repaired
                                           if item.get('reason') == TERMINAL_GENERATION_RETRY_REASON]
            exact_repairs = (summary_repairs + format_repairs + inventory_repairs
                             + terminal_generation_repairs)
            if exact_repairs:
                repaired_ids = {
                    (item.get('proposal_id'), item.get('path'), item.get('sha256'))
                    for item in exact_repairs
                }
                exact_members = [meta for meta in summary.get('eligible') or []
                                 if isinstance(meta, dict)
                                 and (meta.get('proposal_id'), meta.get('path'), meta.get('sha256'))
                                 in repaired_ids]
                if len(exact_members) != len(repaired_ids):
                    repaired = [item for item in repaired
                                if item.get('reason') not in {
                                    SUMMARY_REPAIR_REASON,
                                    INVALID_SUMMARY_FORMAT_RETRY_REASON,
                                    INVENTORY_HISTORY_RETRY_REASON,
                                    TERMINAL_GENERATION_RETRY_REASON,
                                }]
                    summary_repairs = []
                    format_repairs = []
                else:
                    # Open one exact normal cycle for a proven recovery. Other
                    # fresh work remains eligible on the next audit.
                    repaired = exact_repairs
                    summary = dict(summary)
                    summary['eligible'] = exact_members
                    summary['repaired_retry'] = exact_repairs
                    summary['raw_eligible'] = len(exact_members)
                    summary['group_count'] = len({
                        group_key(meta) for meta in exact_members
                    })
                    summary['ready_group_count'] = summary['group_count']
            if summary_repairs:
                summary = dict(summary)
                summary['repaired_retry'] = repaired
            else:
                repaired = [item for item in repaired
                            if item.get('reason') != 'attested_draft_summary_inventory_review']
            summary = dict(summary)
            summary['repaired_retry'] = repaired
        stale_cycles_to_retire = {}
        summary_repair_ids = {
            (item.get('proposal_id'), item.get('path'), item.get('sha256'))
            for item in repaired if isinstance(item, dict)
            and item.get('reason') == SUMMARY_REPAIR_REASON
        }
        for stale_cycle_id, stale_ids in stale_summary_cycles.items():
            if stale_ids and stale_ids.issubset(summary_repair_ids):
                stale_cycles_to_retire[stale_cycle_id] = stale_ids
        for stale_cycle_id, stale_ids in stale_cycles_to_retire.items():
            current = active_cycle(database, lane)
            if (current is None or current.get('id') != stale_cycle_id
                    or {(m.get('proposal_id'), m.get('path'), m.get('sha256'))
                        for m in current.get('members') or []}
                    != stale_ids
                    or any(m.get('status') != 'pending' for m in current.get('members') or [])):
                stale_cycles_to_retire.clear()
                break
            mark_members(
                database, stale_cycle_id,
                [(pid, path) for pid, path, _digest in sorted(stale_ids)],
                'failed', 'stale_unstarted_summary_retry_superseded',
            )
        stale_cycle_retired = bool(stale_cycles_to_retire)
    cycle = None
    if repaired:
        # A validated one-time code repair may need to pass an unrelated
        # ACTIVE backlog cycle. Open the ordinary scoped successor so those
        # pending rows are preserved and restored after this exact retry set.
        current = active_cycle(database, lane)
        allowed = _publication_scope_predicate(policy_raw)
        has_pending = bool(current and any(
            member.get('status') == 'pending' and allowed(member.get('project'))
            for member in current.get('members') or []
        ))
        repaired_ids = {
            (item.get('proposal_id'), item.get('path'), item.get('sha256'))
            for item in repaired if isinstance(item, dict)
        }
        repaired_members = [
            meta for meta in summary.get('eligible') or []
            if isinstance(meta, dict)
            and (meta.get('proposal_id'), meta.get('path'), meta.get('sha256')) in repaired_ids
            and allowed(meta.get('project'))
        ]
        terminal_generation_ids = {
            (item.get('proposal_id'), item.get('path'), item.get('sha256'))
            for item in repaired if isinstance(item, dict)
            and item.get('reason') == TERMINAL_GENERATION_RETRY_REASON
        }
        terminal_generation_exact = bool(terminal_generation_ids) and (
            terminal_generation_ids == repaired_ids
        )
        if (terminal_generation_exact and current and has_pending
                and len(repaired_members) == len(repaired_ids)):
            terminal_plan = terminal_generation_plans.get(
                next((item.get('previous_batch') for item in repaired
                      if item.get('reason') == TERMINAL_GENERATION_RETRY_REASON), None)
            )
            if (terminal_plan is not None
                    and terminal_plan.get('cycle_mode') == 'reuse'
                    and terminal_plan.get('pending_cycle') == current.get('id')):
                if _attach_terminal_generation_retry(
                        database, current, repaired_members, repaired, lane):
                    cycle = active_cycle(database, lane)
        if cycle is None and current and has_pending and len(repaired_members) == len(repaired_ids):
            from caretaker.audit import _open_cycle
            cycle = _open_cycle(
                database, summary, repaired_members, now_iso, lane,
                policy['max_batch'], predecessor_id=current['id'],
            )
        elif (cycle is None and (terminal_generation_exact
                                 or stale_cycle_retired) and current is None
                and len(repaired_members) == len(repaired_ids)):
            from caretaker.audit import _open_cycle
            cycle = _open_cycle(
                database, summary, repaired_members, now_iso, lane,
                policy['max_batch'],
            )
    if cycle is None:
        cycle = maybe_start_cycle(
            database, summary, now_iso=now_iso, lane=lane,
            project_allowed=_publication_scope_predicate(policy_raw),
        )
    if cycle is None:
        fields['status'] = 'IDLE'
        fields['model_calls'] = 0
        return fields
    try:
        accounted = accounted_proposal_ids(vault)
    except PublicationError as exc:
        fields.update(status='BLOCKED', reason=str(exc), cycle_id=cycle['id'], model_calls=0)
        return fields
    already_accounted = [
        (m['proposal_id'], m['path']) for m in cycle['members']
        if m['status'] == 'pending' and m['proposal_id'] in accounted
    ]
    if already_accounted:
        mark_members(database, cycle['id'], already_accounted, 'accounted_elsewhere',
                     'accounted_by_another_ledger_after_cycle_froze')
        cycle_id = cycle['id']
        cycle = active_cycle(database, lane)
        if cycle is None:
            fields.update(status='NO_PENDING', cycle_id=cycle_id, model_calls=0)
            return fields
    cycle = apply_oversize_blocks(database, cycle, policy['max_batch']) or cycle
    batches, blocked = project_batches(cycle, policy['max_batch'])
    for item in blocked:
        tick_budget.check('maintenance.py:metadata_or_work')
        mark_members(
            database, cycle['id'],
            [(m['proposal_id'], m['path']) for m in item['members']],
            'blocked', item['reason'],
        )
    skipped = []
    quarantined = []
    fields['quarantined'] = []
    source_index = None
    version = code_version()
    from caretaker.repaired_retry import (
        INVALID_SUMMARY_FORMAT_SOURCE_REASON, retry_batch_key,
    )
    retry_by_identity = {
        (item.get('proposal_id'), item.get('path'), item.get('sha256')): item
        for item in cycle.get('counts', {}).get('repaired_retry', [])
        if isinstance(item, dict)
    }
    for members in batches.get(project) or []:
        tick_budget.check('maintenance.py:metadata_or_work')
        paths = [m['path'] for m in members]
        hashes = [m['sha256'] for m in members]
        pids = [m['proposal_id'] for m in members]
        stale_members = []
        for member in members:
            tick_budget.check('maintenance.py:metadata_or_work')
            try:
                live = _live_member_hash(vault, member['path'])
            except PublicationError:
                live = None
            if live != member['sha256']:
                stale_members.append(member)
        if stale_members:
            # A changed proposal hash needs a fresh review for that member
            # only; the other members of the batch stay reviewable.
            mark_members(
                database, cycle['id'],
                [(m['proposal_id'], m['path']) for m in stale_members],
                'stale', 'frozen_hash_changed_needs_fresh_review',
            )
            skipped.append({
                'reason': 'stale_hash',
                'proposal_ids': [m['proposal_id'] for m in stale_members],
            })
            stale_ids = {m['proposal_id'] for m in stale_members}
            members = [m for m in members if m['proposal_id'] not in stale_ids]
            if not members:
                continue
            paths = [m['path'] for m in members]
            hashes = [m['sha256'] for m in members]
            pids = [m['proposal_id'] for m in members]
        # One poisoned member must not reject the whole batch. The dependency
        # closure of every member is checked before the first model call, so
        # exactly the bad members are isolated with a preserved, hash-specific
        # outcome and the independent ones still advance in one reviewed
        # subset transaction.
        if source_index is None:
            source_index = source_metadata_index(vault)
        try:
            eligible_members, isolated = partition_batch_members(
                vault, members, project, registry,
                index=source_index, accounted=accounted,
            )
        except PublicationError as exc:
            # The closure of the batch cannot be established: fail closed
            # without a model call and without touching any member.
            fields.update(status='BLOCKED', reason=str(exc),
                          cycle_id=cycle['id'], model_calls=0)
            return fields
        for record in isolated:
            status = ('accounted_elsewhere'
                      if record.get('code') == 'proposal_already_accounted'
                      else 'quarantined')
            mark_member(
                database, cycle['id'], record.get('proposal_id') or '',
                record.get('path') or '', status, record.get('reason') or '',
            )
        if isolated:
            quarantined.append({
                'proposal_ids': [r.get('proposal_id') for r in isolated],
                'reasons': [r.get('reason') for r in isolated],
            })
            fields['quarantined'] = list(quarantined)
        if not eligible_members:
            # Nothing publishable remains in this batch: no model call.
            skipped.append({
                'reason': 'all_members_ineligible',
                'proposal_ids': [m['proposal_id'] for m in members],
            })
            continue
        members = eligible_members
        paths = [m['path'] for m in members]
        hashes = [m['sha256'] for m in members]
        pids = [m['proposal_id'] for m in members]
        retry_members = [
            retry_by_identity[(m['proposal_id'], m['path'], m['sha256'])]
            for m in members
            if (m['proposal_id'], m['path'], m['sha256']) in retry_by_identity
        ]
        key = (retry_batch_key(project, members, retry_members)
               if retry_members else batch_key(project, paths))
        from caretaker.publication import safe_file, sha
        page = safe_file(vault, main_page(project, registry))
        context_hash = sha(page.read_bytes()) if page.is_file() else 'missing_target'
        current_fp = fingerprint(project, paths, hashes + [context_hash], policy, version)
        row = read_row(database, key)
        if (row and row.get('status') == 'BLOCKED'
                and row.get('fingerprint') == current_fp
                and not retry_members):
            try:
                prior_result = json.loads(row.get('result') or '{}')
            except (TypeError, ValueError):
                prior_result = {}
            if (isinstance(prior_result, dict)
                    and prior_result.get('reason') == INVALID_SUMMARY_FORMAT_SOURCE_REASON):
                mark_members(
                    database, cycle['id'],
                    [(m['proposal_id'], m['path']) for m in members],
                    'failed', 'pre_candidate_invalid_summary_retry_not_admitted',
                )
                skipped.append({
                    'reason': 'pre_candidate_invalid_summary_retry_not_admitted',
                    'batch': key,
                })
                continue
        if row and row.get('status') == 'RUNNING':
            verdict, evidence = canonical_verdict(vault, row)
            rec = reconcile_running(database, key, verdict, evidence)
            skipped.append({'reason': rec.get('status'), 'batch': key, 'verdict': verdict})
            if rec.get('status') == 'PUBLISHED':
                mark_members(
                    database, cycle['id'],
                    [(m['proposal_id'], m['path']) for m in members],
                    'published', 'reconciled',
                )
            continue
        due, why = due_to_run(row, now_iso, current_fp)
        if not due:
            member_status = {
                'already_published': 'published',
                'quarantined_same_fingerprint': 'quarantined',
                'unknown_needs_reconciliation': 'unknown',
                'backoff_not_due': 'pending',
            }.get(why)
            if member_status and member_status != 'pending':
                mark_members(
                    database, cycle['id'],
                    [(m['proposal_id'], m['path']) for m in members],
                    member_status, why,
                )
            skipped.append({'reason': why, 'batch': key})
            continue
        meta = {
            'proposal_ids': pids, 'paths': paths, 'hashes': hashes,
            'project': project, 'cycle_id': cycle['id'], 'state_dir': cfg['state_dir'],
        }
        priority_marker = (
            prepare_priority_seen_key
            if _valid_prepared_priority_marker(prepare_priority_seen_key)
            else None
        )
        if priority_marker is None and row and row.get('status') == 'PREPARED_DEFERRED':
            try:
                prior_result = json.loads(row.get('result') or '{}')
            except (TypeError, ValueError):
                prior_result = {}
            prior_marker = (prior_result.get('prepare_priority_seen_key')
                            if isinstance(prior_result, dict) else None)
            if _valid_prepared_priority_marker(prior_marker):
                priority_marker = prior_marker
        if priority_marker is not None:
            meta['prepare_priority_seen_key'] = priority_marker
        if retry_members:
            meta['repaired_retry'] = retry_members
        if _require_committed_inputs:
            try:
                from caretaker.committed_inputs import inputs_match_head
                committed_frozen = freeze(
                    vault, paths, main_page(project, registry),
                    attestation_state_dir=Path(cfg['state_dir']),
                )
                expected_inputs = set(zip(pids, paths, hashes))
                frozen_inputs = {
                    (item.get('id'), item.get('path'), item.get('sha256'))
                    for item in committed_frozen.get('proposals', [])
                    if isinstance(item, dict)
                }
                if (frozen_inputs != expected_inputs
                        or len(frozen_inputs) != len(paths)
                        or inputs_match_head(committed_frozen) is not True):
                    fields.update(
                        status='BLOCKED', reason='selected_inputs_not_in_head',
                        project=project, batch=key, cycle_id=cycle['id'],
                        proposal_ids=pids, paths=paths, hashes=hashes,
                        model_calls=0,
                    )
                    return fields
            except Exception:
                fields.update(
                    status='BLOCKED', reason='selected_input_head_proof_unavailable',
                    project=project, batch=key, cycle_id=cycle['id'],
                    proposal_ids=pids, paths=paths, hashes=hashes,
                    model_calls=0,
                )
                return fields
        if not _write_summary_retry_marker(
                config_path, cfg, database, cycle, members, retry_members, paths,
                lane=lane):
            mark_members(
                database, cycle['id'],
                [(m['proposal_id'], m['path']) for m in members],
                'failed', 'summary_retry_token_validation_failed',
            )
            fields.update(status='BLOCKED', reason='summary_retry_token_validation_failed',
                          cycle_id=cycle['id'], model_calls=0)
            return fields
        if not _write_terminal_generation_retry_marker(
                config_path, cfg, database, cycle, members, retry_members, paths,
                lane=lane):
            mark_members(
                database, cycle['id'],
                [(m['proposal_id'], m['path']) for m in members],
                'failed', 'terminal_generation_retry_token_validation_failed',
            )
            fields.update(status='BLOCKED',
                          reason='terminal_generation_retry_token_validation_failed',
                          cycle_id=cycle['id'], model_calls=0)
            return fields
        record_start(database, key, now_iso, current_fp, meta)
        try:
            result = dict(_invoke_curate(config_path, paths))
        except tick_budget.TickBudgetExceeded:
            # Cooperative stops occur before a model or canonical publication;
            # subprocess outcomes remain reconciled by their existing journals.
            from caretaker.pubstate import record_budget_deferred
            record_budget_deferred(database, key, current_fp)
            raise
        if priority_marker is not None:
            result['prepare_priority_seen_key'] = priority_marker
        result.setdefault('proposal_ids', pids)
        result.setdefault('paths', paths)
        result.setdefault('hashes', hashes)
        result.setdefault('project', project)
        result['batch'] = key
        result['cycle_id'] = cycle['id']
        result.update(fields)
        result.setdefault('model_calls', 1)
        result['skipped'] = skipped
        if (result.get('status') == 'PENDING'
                and result.get('reason') == 'no_independent_reviewer'):
            prepared_defer = _no_reviewer_prepared_defer(result, key)
            if prepared_defer is not None:
                result.update(prepared_defer)
        if (result.get('status') == 'BLOCKED'
                and result.get('reason') == 'hub_preflight_failed'):
            cached_failure = {
                'status': 'DEFERRED_HUB_PREFLIGHT',
                'reason': 'hub_preflight_failed',
            }
            if type(_hub_preflight_state) is dict:
                _hub_preflight_state['result'] = cached_failure
            prior_prepared = None
            if row and row.get('status') == 'PREPARED_DEFERRED':
                try:
                    prior_prepared = json.loads(row.get('result') or '{}')
                except (TypeError, ValueError):
                    prior_prepared = None
            if isinstance(prior_prepared, dict):
                # The phase cache was already revalidated by the normal retry
                # path. Restore its prior accepted prepared row after record_start.
                record_prepared_deferred(
                    database, key, current_fp, prior_prepared,
                )
            else:
                # This failure happened before a usable phase was accepted.
                # Keep the batch due without consuming an attempt or failure.
                record_budget_deferred(database, key, current_fp)
            deferred = _deferred_hub_preflight(cached_failure)
            deferred.update(
                project=project, batch=key, cycle_id=cycle['id'],
                proposal_ids=pids, paths=paths, hashes=hashes,
            )
            return deferred
        if result.get('status') == 'PUBLISHED':
            record_success(database, key, result)
            mark_members(
                database, cycle['id'],
                [(m['proposal_id'], m['path']) for m in members],
                'published', '',
            )
            return result
        if result.get('status') == 'DEFERRED_PREPARED':
            prepared = record_prepared_deferred(
                database, key, current_fp, result,
            )
            result.update(
                publication_status=prepared['status'],
                pre_effect=True,
            )
            # Keep the frozen audit member pending. The immutable phase cache
            # will validate source, target, manifest, and attempt evidence on
            # the next ordinary tick before it resumes.
            return result
        reason = result.get('reason') or result.get('error_code') or result.get('status') or 'BLOCKED'
        if reason == 'proposals_accounted_elsewhere':
            try:
                accounted = accounted_proposal_ids(vault)
            except PublicationError as exc:
                persisted = record_failure(database, key, reason, result, current_fp, now=moment)
                result.update(persisted)
                result['reason'] = str(exc)
                return result
            persisted = record_failure(database, key, reason, result, current_fp, now=moment)
            mark_members(
                database, cycle['id'],
                [(m['proposal_id'], m['path']) for m in members
                 if m['proposal_id'] in accounted],
                'accounted_elsewhere', 'accounted_by_another_ledger_after_cycle_froze',
            )
            result.update(persisted)
            return result
        persisted = record_failure(database, key, reason, result, current_fp, now=moment)
        member_status = 'quarantined' if persisted['status'] == 'QUARANTINED' else (
            'deferred' if persisted['status'] == 'BACKOFF_DEFERRED' else 'failed')
        if persisted['status'] == 'BACKOFF_DEFERRED':
            member_status = 'pending'
        mark_members(
            database, cycle['id'],
            [(m['proposal_id'], m['path']) for m in members],
            member_status, persisted['status'],
        )
        result.update(persisted)
        return result
    other_skips = [s for s in skipped if s.get('reason') != 'all_members_ineligible']
    if quarantined and not other_skips:
        fields['status'] = 'QUARANTINED_MEMBERS_ONLY'
    elif skipped:
        fields['status'] = 'SKIPPED_NO_DUE_BATCH'
    else:
        fields['status'] = 'NO_PENDING'
    fields['skipped'] = skipped
    fields['quarantined'] = quarantined
    fields['cycle_id'] = cycle['id']
    fields['model_calls'] = 0
    fields['deferred'] = summary.get('deferred', 0)
    return fields


def _runtime_base_with_control_publication(base, control):
    """Copy base, then overlay publication from control.json only.

    base-config.json is never the source of truth for publication budgets.
    A missing control publication section leaves the overlay absent so
    load_config keeps the legacy 360 s / muse defaults.
    """
    cfg = json.loads(json.dumps(base))
    cfg.pop('publication', None)
    if isinstance(control, dict) and 'publication' in control:
        publication = control.get('publication')
        if publication is not None:
            cfg['publication'] = json.loads(json.dumps(publication))
    return cfg


def _project_runtime_cfg(base, project, state, lane='legacy', database=None):
    state = _canonical_project_runtime_state(state, project, lane, database)
    project_root = Path(state) / 'projects' / project
    project_root.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(json.dumps(base))
    cfg.update(
        project_slug=project,
        session_id='logical:openclaw-maintenance:' + project,
        state_dir=str(project_root / 'state'),
        maintenance_state_dir=str(Path(state).parent if Path(state).name == 'new-event-epoch'
                                  else state),
    )
    cfg.setdefault('hub', {})
    cfg['hub']['context_receipt'] = str(project_root / 'receipts/context.json')
    cfg.setdefault('office', {})
    cfg['office']['session_id'] = cfg['session_id']
    allow = cfg.setdefault('allowlist', {})
    tools = list(allow.get('tools') or [])
    if 'openclaw' not in tools:
        tools.append('openclaw')
    allow['tools'] = tools
    if not cfg.get('inputs'):
        empty = project_root / 'empty.jsonl'
        empty.write_text('', encoding='utf-8')
        cfg['inputs'] = [{'path': str(empty), 'kind': 'normalized_events', 'tool': 'openclaw'}]
    config_path = project_root / 'config.json'
    config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    return config_path, cfg


#: A PUBLISHED batch that still needs attention: its local Git checkpoint is
#: pending, or the curator lock could not be released (0.1.17).
POST_PUBLICATION_ATTENTION = frozenset({'checkpoint_pending', 'curator_lock_release_failed'})


def _pending_checkpoint_rows(database, lane=None):
    rows = database.execute(
        "SELECT batch, result FROM publications WHERE status='PUBLISHED' AND result LIKE '%PENDING%'"
    ).fetchall()
    out = []
    for batch, raw in rows:
        tick_budget.check('maintenance.py:metadata_or_work')
        try:
            value = json.loads(raw or '{}')
        except (ValueError, TypeError):
            continue
        is_new = str(value.get('cycle_id') or '').startswith('audit-new-') if isinstance(value, dict) else False
        if lane == 'new' and not is_new or lane == 'legacy' and is_new:
            continue
        checkpoint = value.get('checkpoint') if isinstance(value, dict) else None
        if (isinstance(checkpoint, dict) and checkpoint.get('status') == 'PENDING'
                and isinstance(value.get('publication'), dict)):
            out.append((batch, value))
    return out


def _legacy_running_count(database):
    count = 0
    for (raw,) in database.execute("SELECT result FROM publications WHERE status='RUNNING'"):
        tick_budget.check('maintenance.py:metadata_or_work')
        try:
            value = json.loads(raw or '{}')
        except (ValueError, TypeError):
            value = {}
        if not isinstance(value, dict) or not str(value.get('cycle_id') or '').startswith('audit-new-'):
            count += 1
    return count


def complete_pending_checkpoints(database, base, lane=None):
    """Finish the local Git checkpoint of publications that are already canonical.

    Runs before any new publication: a new one would rewrite the shared log and
    index and could leave the pending checkpoint unprovable. Returns what is still
    pending; the caller publishes nothing new while that list is not empty.
    """
    from caretaker.publication import PublicationError
    from caretaker.checkpoint import checkpoint_publication
    from curate_once import curator_lock
    vault = Path(base['hub']['vault'])
    remaining = []
    for batch, value in _pending_checkpoint_rows(database, lane):
        tick_budget.check('maintenance.py:metadata_or_work')
        publication = value['publication']
        receipt = Path(str(value['checkpoint'].get('receipt_dir') or ''))
        try:
            if not str(value['checkpoint'].get('receipt_dir') or '') or not receipt.is_dir():
                raise PublicationError('pending_checkpoint_receipt_missing')
            with curator_lock(base['hub']['script'], vault, receipt):
                done = checkpoint_publication(publication, vault, receipt)
        except tick_budget.TickBudgetExceeded:
            raise
        except Exception as exc:   # stays PENDING; retried on the next tick
            reason = str(exc) if isinstance(exc, PublicationError) else type(exc).__name__
            remaining.append({'batch': batch, 'event_id': publication.get('event_id'), 'reason': reason})
            continue
        value['checkpoint'] = {**done, 'completed_after_pending': True}
        with database:
            database.execute(
                'UPDATE publications SET result=?, updated_at=? WHERE batch=?',
                (json.dumps(value, ensure_ascii=False), dt.datetime.now(dt.timezone.utc).isoformat(), batch),
            )
    return remaining


def _publication_scope_predicate(policy):
    """In-scope test for cycle retirement. held_projects override the allowlist."""
    def allowed(project):
        return _publication_project_allowed(project, policy)
    return allowed


def _publication_project_allowed(project, policy):
    held = policy.get('held_projects', [])
    allowed = policy.get('projects')
    if not isinstance(held, list) or any(not isinstance(p,str) for p in held):
        raise ValueError('publication held_projects must be a list of slugs')
    if allowed is not None and (not isinstance(allowed,list) or any(not isinstance(p,str) for p in allowed)):
        raise ValueError('publication projects must be a list of slugs')
    return project not in held and (allowed is None or project in allowed)


_PREPARED_PRIORITY_PHASES = frozenset({
    'PREPARED', 'REPAIR_REQUIRED', 'REPAIR_PREPARED', 'REVIEW_ACCEPTED',
    'REVIEW_RESULT', 'REVIEW2_RESULT', 'REPAIR_RESULT',
})


def _prepared_priority_phase_cache_manifest(result, batch, runtime_state,
                                           require_quiescent=False):
    """Read only the cache pointer bound to this pending batch/project state."""
    project = result.get('project') if isinstance(result, dict) else None
    if (not isinstance(project, str)
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,100}', project)
            or not isinstance(batch, str)
            or not re.fullmatch(r'[a-f0-9]{64}', batch)):
        return None
    pointer = (Path(runtime_state) / 'projects' / project / 'state' / 'curation'
               / ('phase-' + batch + '.json'))
    try:
        if (not pointer.is_file() or pointer.is_symlink() or pointer.is_junction()
                or pointer.stat().st_size > 262144):
            return None
        root = Path(runtime_state).resolve()
        resolved = pointer.resolve(strict=True)
        if os.path.commonpath((str(root), str(resolved))) != str(root):
            return None
        record = json.loads(pointer.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError, UnicodeError):
        return None
    if not isinstance(record, dict):
        return None
    cache_sha = record.get('cache_sha256')
    body = dict(record)
    body.pop('cache_sha256', None)
    canonical = (json.dumps(body, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if (not isinstance(cache_sha, str)
            or hashlib.sha256(canonical).hexdigest() != cache_sha):
        return None
    phase = result.get('phase')
    paths = result.get('paths')
    proposal_ids = result.get('proposal_ids')
    hashes = result.get('hashes')
    binding = record.get('binding')
    progress = record.get('progress')
    if (record.get('schema_version') != 1
            or record.get('project') != project
            or record.get('phase') != phase
            or record.get('paths') != sorted(paths or [])
            or not isinstance(binding, dict) or not isinstance(progress, dict)
            or type(progress.get('maker_calls')) is not int
            or type(progress.get('review_calls')) is not int
            or progress.get('phase') != phase
            or progress.get('maker_calls') != result.get('maker_calls')
            or progress.get('review_calls') != result.get('review_calls')):
        return None
    if require_quiescent and (
        progress.get('status') in {
            'UNKNOWN', 'UNKNOWN_REQUIRES_RECONCILIATION', 'RUNNING', 'PUBLISHED',
            'ACCOUNTED', 'accounted_elsewhere', 'unsafe_source',
        }
        or record.get('publication') or record.get('checkpoint')
        or progress.get('publication') or progress.get('checkpoint')
    ):
        return None
    if (not isinstance(proposal_ids, list) or not isinstance(paths, list)
            or not isinstance(hashes, list)
            or len(proposal_ids) != len(paths) or len(paths) != len(hashes)):
        return None
    bound_proposals = binding.get('proposals')
    if not isinstance(bound_proposals, list):
        return None
    result_identities = set(zip(proposal_ids, paths, hashes))
    cache_identities = {
        (item.get('id'), item.get('path'), item.get('sha256'))
        for item in bound_proposals if isinstance(item, dict)
    }
    if not result_identities or result_identities != cache_identities:
        return None
    manifest_sha = record.get('manifest_sha256')
    if not isinstance(manifest_sha, str) or not re.fullmatch(r'[a-f0-9]{64}', manifest_sha):
        return None
    if result.get('manifest_sha256') not in (None, manifest_sha):
        return None
    try:
        from curate_once import _phase_record_code_compatible
        if not _phase_record_code_compatible(record):
            return None
    except Exception:
        return None
    return manifest_sha


def _prepared_priority_phase_key(result, manifest_sha=None):
    if not isinstance(result, dict):
        return None
    phase = result.get('phase')
    manifest = manifest_sha or result.get('manifest_sha256')
    maker_calls = result.get('maker_calls')
    review_calls = result.get('review_calls')
    if (phase not in _PREPARED_PRIORITY_PHASES
            or not isinstance(manifest, str)
            or not re.fullmatch(r'[a-f0-9]{64}', manifest)
            or type(maker_calls) is not int or maker_calls < 0
            or type(review_calls) is not int or review_calls < 0):
        return None
    return f'{phase}|{manifest}|{maker_calls}|{review_calls}'


def _valid_prepared_priority_marker(value):
    if not isinstance(value, str) or len(value) > 180:
        return False
    parts = value.split('|')
    return (
        len(parts) == 4
        and parts[0] in _PREPARED_PRIORITY_PHASES
        and re.fullmatch(r'[a-f0-9]{64}', parts[1]) is not None
        and re.fullmatch(r'(?:0|[1-9][0-9]*)', parts[2]) is not None
        and re.fullmatch(r'(?:0|[1-9][0-9]*)', parts[3]) is not None
    )


def _prepared_priority_candidate(database, cycle, projects, policy, now_iso,
                                 runtime_state=None, runtime_state_for_project=None,
                                 allow_budget_retry=False):
    """Find one resumable pre-effect row already pending in this frozen cycle.

    This only orders work. publication_tick still revalidates the complete
    proposal, source, target, phase-cache, review, and role bindings.
    """
    if not isinstance(cycle, dict) or cycle.get('status') != 'ACTIVE':
        return None
    allowed_projects = {project for project in projects
                        if _publication_project_allowed(project, policy)}
    if not allowed_projects:
        return None
    pending = {
        (member.get('proposal_id'), member.get('path'), member.get('sha256'))
        for member in cycle.get('members') or []
        if isinstance(member, dict) and member.get('status') == 'pending'
        and member.get('project') in allowed_projects
    }
    if not pending:
        return None
    candidates = []
    rows = database.execute(
        "SELECT batch, status, result, next_due, updated_at FROM publications "
        "WHERE status='PREPARED_DEFERRED'"
    ).fetchall()
    for batch, status, raw, next_due, updated_at in rows:
        tick_budget.check('maintenance.py:metadata_or_work')
        if status != 'PREPARED_DEFERRED' or (next_due and next_due > now_iso):
            continue
        try:
            result = json.loads(raw or '{}')
        except (TypeError, ValueError):
            continue
        if (not isinstance(result, dict)
                or result.get('status') != 'PREPARED_DEFERRED'
                or result.get('pre_effect') is not True
                or result.get('cycle_id') != cycle.get('id')
                or result.get('batch') != batch
                or result.get('publication') or result.get('checkpoint')):
            continue
        project = result.get('project')
        if project not in allowed_projects:
            continue
        manifest_sha = result.get('manifest_sha256')
        phase_state = (runtime_state_for_project(project)
                       if callable(runtime_state_for_project) else runtime_state)
        if phase_state is not None:
            manifest_sha = _prepared_priority_phase_cache_manifest(
                result, batch, phase_state, require_quiescent=allow_budget_retry,
            )
        phase_key = _prepared_priority_phase_key(result, manifest_sha)
        if phase_key is None:
            continue
        seen_key = result.get('prepare_priority_seen_key')
        if seen_key is not None and not _valid_prepared_priority_marker(seen_key):
            continue
        if seen_key == phase_key:
            # An exact persisted pre-review budget stop did no model/phase
            # work. Its seen marker must not permanently spend this candidate's
            # priority. Require a current verified cache (not result fallback);
            # publication_tick still performs every source/target/resume gate.
            if not (
                allow_budget_retry and phase_state is not None
                and manifest_sha is not None
                and result.get('phase') in {'PREPARED', 'REPAIR_PREPARED'}
                and result.get('reason') == 'tick_time_budget_exhausted'
                and type(result.get('model_calls')) is int
                and result['model_calls'] == 0
                and not result.get('error_code')
            ):
                continue
        proposal_ids, paths, hashes = (
            result.get(name) for name in ('proposal_ids', 'paths', 'hashes')
        )
        if (not isinstance(proposal_ids, list) or not proposal_ids
                or not isinstance(paths, list) or not isinstance(hashes, list)
                or len(proposal_ids) != len(paths) or len(paths) != len(hashes)
                or any(not isinstance(item, str) or not item for item in proposal_ids + paths)
                or any(not isinstance(item, str) or not re.fullmatch(r'[a-f0-9]{64}', item)
                       for item in hashes)):
            continue
        identities = set(zip(proposal_ids, paths, hashes))
        if not identities or not identities.issubset(pending):
            continue
        candidates.append((str(updated_at or ''), project, batch, phase_key))
    if not candidates:
        return None
    _updated, project, _batch, phase_key = min(candidates)
    return project, phase_key


def _global_hub_preflight(base):
    """Run the canonical read-only Hub health and full-lint gate once per tick.

    Publication failures caused by a repository-wide Hub problem must defer
    the whole drain before audit rows or per-proposal attempt state can change.
    Keep only a coarse reason in maintenance metadata; gateway notes may expose
    local paths or other runtime details.
    """
    try:
        from caretaker.hub import (
            CanonicalKnowledgeGateway, HealthResult, LintResult,
        )

        hub = base.get('hub') if isinstance(base, dict) else None
        if not isinstance(hub, dict):
            return {'status': 'DEFERRED_HUB_PREFLIGHT',
                    'reason': 'hub_preflight_config_invalid'}
        python_executable = hub.get('python_executable')
        script = hub.get('script')
        vault = hub.get('vault')
        if any(not isinstance(value, str) or not value.strip() or '\0' in value
               for value in (python_executable, script, vault)):
            return {'status': 'DEFERRED_HUB_PREFLIGHT',
                    'reason': 'hub_preflight_config_invalid'}
        receipt = hub.get('context_receipt') or ''
        if not isinstance(receipt, str) or '\0' in receipt:
            return {'status': 'DEFERRED_HUB_PREFLIGHT',
                    'reason': 'hub_preflight_config_invalid'}
        receipt_path = Path(receipt)
        gateway = CanonicalKnowledgeGateway(
            python_executable=python_executable,
            script=script,
            vault=vault,
            receipt_root=str(receipt_path.parent if receipt_path.name else receipt_path),
        )
        health = gateway.health()
        if (type(health) is not HealthResult
                or type(getattr(health, 'status', None)) is not str
                or health.status != 'pass'
                or type(getattr(health, 'notes', None)) is not str
                or not health.notes.strip()):
            return {'status': 'DEFERRED_HUB_PREFLIGHT',
                    'reason': 'hub_health_failed'}
        lint = gateway.lint()
        if (type(lint) is not LintResult
                or type(getattr(lint, 'status', None)) is not str
                or lint.status != 'pass'
                or type(getattr(lint, 'notes', None)) is not str
                or not lint.notes.strip()):
            return {'status': 'DEFERRED_HUB_PREFLIGHT',
                    'reason': 'hub_lint_failed'}
        return {
            'status': 'PASS',
            'health_status': health.status,
            'lint_status': lint.status,
        }
    except tick_budget.TickBudgetExceeded:
        raise
    except Exception:
        return {'status': 'DEFERRED_HUB_PREFLIGHT',
                'reason': 'hub_preflight_invalid'}


def _valid_hub_preflight(result):
    return (type(result) is dict
            and type(result.get('status')) is str
            and result.get('status') == 'PASS'
            and type(result.get('health_status')) is str
            and result.get('health_status') == 'pass'
            and type(result.get('lint_status')) is str
            and result.get('lint_status') == 'pass')


def _deferred_hub_preflight(result):
    reason = result.get('reason') if isinstance(result, dict) else None
    if reason not in {'hub_health_failed', 'hub_lint_failed',
                      'hub_preflight_config_invalid', 'hub_preflight_invalid',
                      'hub_preflight_failed'}:
        reason = 'hub_preflight_invalid'
    return {
        'status': 'DEFERRED_HUB_PREFLIGHT',
        'reason': reason,
        'model_calls': 0,
        'batches': [],
    }


def publication_drain(control, base, database, state, now=None, lane='legacy',
                      recovery=None, _hub_preflight=None,
                      _hub_preflight_state=None,
                      _require_committed_inputs=False):
    """Independent Hub publication drain; not gated on ingest pipeline health."""
    policy_raw = control.get('publication', {}) or {}
    if policy_raw.get('enabled') is not True:
        return {'status': 'DISABLED'}
    # Runtime callers supply one canonical global health+full-lint result for
    # the tick. A failed or malformed supplied result is a no-op before lock,
    # audit, cycle, or proposal-attempt reconciliation can mutate state.
    if _hub_preflight is not None and not _valid_hub_preflight(_hub_preflight):
        return _deferred_hub_preflight(_hub_preflight)
    if _hub_preflight_state is not None:
        if type(_hub_preflight_state) is not dict:
            return _deferred_hub_preflight({
                'reason': 'hub_preflight_invalid',
            })
        if 'result' not in _hub_preflight_state:
            _hub_preflight_state['result'] = _global_hub_preflight(base)
        tick_preflight = _hub_preflight_state.get('result')
        if not _valid_hub_preflight(tick_preflight):
            return _deferred_hub_preflight(tick_preflight)
    # This must precede RUNNING reconciliation: an applying journal with all
    # files replaced is not yet a canonical committed journal.
    if recovery is None:
        from caretaker.roll_forward import recover_stale_curator_lock
        recovery = recover_stale_curator_lock(database, base, state, control=control, now=now)
    if recovery['status'] in {'RECOVERED', 'RELEASED_NO_TRANSACTION'}:
        return {'status': 'RECOVERED', 'curator_lock_recovery': recovery,
                'model_calls': 0, 'batches': []}
    if recovery['status'] in {'LOCK_FRESH', 'LOCK_TIME_UNVERIFIED'}:
        return {'status': 'BLOCKED', 'reason': 'curator_lock_held',
                'curator_lock_recovery': recovery, 'model_calls': 0, 'batches': []}
    if recovery['status'] != 'NO_LOCK':
        return {'status': 'DEGRADED', 'reason': 'stale_curator_lock_requires_review',
                'curator_lock_recovery': recovery, 'model_calls': 0, 'batches': []}
    from caretaker.audit import (
        active_cycle, cycle_projects, ensure_tables, maybe_start_cycle,
        policy_from, summarize,
    )
    from caretaker.publication import PublicationError
    from caretaker.pubstate import ensure_pub_tables, reconcile_all_running
    vault = Path(base['hub']['vault'])
    policy = policy_from(policy_raw)
    moment = now or dt.datetime.now(dt.timezone.utc)
    ensure_tables(database)
    ensure_pub_tables(database)
    if lane == 'legacy':
        reconcile_all_running(database, vault)
    elif _pending_checkpoint_rows(database, 'legacy') or _legacy_running_count(database):
        return {'status': 'BLOCKED', 'reason': 'legacy_publication_unreconciled',
                'model_calls': 0, 'batches': []}
    eligible_filter = (_new_epoch_proposal_filter(database, state, control, vault)
                       if lane == 'new' else None)

    def _project_runtime_state(project):
        return _canonical_project_runtime_state(state, project, lane, database)

    def _attestation_state_for_project(project):
        owner = _project_runtime_state(project)
        return owner / 'projects' / project / 'state'

    def _summary_preflight(project, value, _stored_fp):
        return _summary_repair_preflight(
            vault, _attestation_state_for_project(project), project, value,
        )

    def _existing_phase_config(project):
        return _existing_publication_phase_config(
            state, project, lane, database, vault, policy_raw,
        )

    (inventory_history_preflight, terminal_generation_preflight,
     _unused_terminal_plans) = _publication_recovery_preflights(
        vault, policy, database, lane, _existing_phase_config,
    )

    try:
        summary = _audit_summary(
            vault, policy, database, moment, eligible_filter,
            attestation_state_dir_for_project=_attestation_state_for_project,
            summary_repair_preflight=_summary_preflight,
            inventory_history_preflight=inventory_history_preflight,
            terminal_generation_preflight=terminal_generation_preflight,
        )
    except PublicationError as exc:
        out = {'status': 'BLOCKED', 'reason': str(exc)}
        out.update(_empty_audit_fields())
        return out
    fields = _audit_fields(summary)
    pending = complete_pending_checkpoints(database, base, lane=lane)
    if pending:
        fields.update(status='CHECKPOINT_PENDING', pending_checkpoints=pending, batches=[], model_calls=0)
        return fields
    cycle = maybe_start_cycle(
        database, summary, now_iso=moment.isoformat(), lane=lane,
        project_allowed=_publication_scope_predicate(policy_raw),
    )
    projects = cycle_projects(cycle)
    projects = [p for p in projects if _publication_project_allowed(p, policy_raw)]
    frozen_projects = list(dict.fromkeys(projects))
    # An active project must not hide other eligible projects from their closed
    # per-project retry planner. Keep both sets inside the same fair bounded loop.
    projects = list(dict.fromkeys(projects + sorted({
        member['project'] for member in summary.get('eligible') or []
        if isinstance(member, dict) and isinstance(member.get('project'), str)
        and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,100}', member['project'])
        and _publication_project_allowed(member['project'], policy_raw)
    })))
    transport_resume_projects = {}
    if lane == 'legacy':
        # UNKNOWN is deliberately absent from fresh-source eligibility. Route
        # its exact reserved append-generation to the same bounded project
        # loop; publication_tick still requires the complete positive proof.
        for (raw_unknown,) in database.execute(
                "SELECT result FROM publications WHERE status='UNKNOWN_REQUIRES_RECONCILIATION'"):
            try:
                value = json.loads(raw_unknown)
                retry_project = value.get('project')
                if (value.get('reason') != 'terminal_generation_maker_outcome_unknown'
                        or not isinstance(retry_project, str)
                        or not _publication_project_allowed(retry_project, policy_raw)):
                    continue
                existing = _existing_phase_config(retry_project)
                marker = (existing.raw or {}).get('maintenance_terminal_generation_retry')
                if isinstance(marker, dict) and marker.get('lane') == 'legacy':
                    transport_resume_projects[retry_project] = marker
            except (OSError, ValueError, TypeError, KeyError):
                continue
        projects = list(dict.fromkeys(projects + sorted(transport_resume_projects)))
    # Persist rotation independently of intake. A large first project must
    # not consume every publication tick while other projects wait.
    rotation_table = 'new_publication_rotation' if lane == 'new' else 'publication_rotation'
    database.execute(f'CREATE TABLE IF NOT EXISTS {rotation_table}(id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL)')
    rotation_row = database.execute(f'SELECT value FROM {rotation_table} WHERE id=1').fetchone()
    rotation = int(rotation_row[0]) if rotation_row else 0
    rotation_had_projects = bool(projects)
    limit = min(8, max(1, int(control.get('max_projects_per_tick', 4))))
    budget = min(16, max(1, int(policy_raw.get('max_batches_per_tick', limit))))
    slots = min(limit, budget)
    probes = [project for project in projects if project not in frozen_projects]
    runtime_state = Path(state) / 'new-event-epoch' if lane == 'new' else Path(state)
    priority_projects = frozen_projects + probes
    priority = _prepared_priority_candidate(
        database, cycle, priority_projects, policy_raw, moment.isoformat(), runtime_state,
        runtime_state_for_project=_project_runtime_state,
    )
    budget_retry = None
    if priority is None:
        budget_retry = _prepared_priority_candidate(
            database, cycle, priority_projects, policy_raw, moment.isoformat(), runtime_state,
            runtime_state_for_project=_project_runtime_state, allow_budget_retry=True,
        )
    budget_priority = bool(budget_retry and (slots > 1 or rotation % 2 == 1))
    if budget_priority:
        priority = budget_retry
    # Frozen pending batches can make progress now. Future eligible projects
    # still need their ordinary closed per-project retry admission, so retain
    # one final discovery slot without letting them consume every frozen slot.
    both_pools = bool(frozen_projects and probes)
    # A one-slot budget retry shares alternate turns with ordinary rotation;
    # ordinary projects advance on their own turns, including even-sized pools.
    ordinary_rotation = rotation // 2 if slots == 1 and budget_retry else rotation
    pool_rotation = ordinary_rotation // 2 if both_pools and slots == 1 else ordinary_rotation
    def _rotated(pool):
        offset = pool_rotation % len(pool) if pool else 0
        return pool[offset:] + pool[:offset]
    frozen_projects, probes = _rotated(frozen_projects), _rotated(probes)
    if both_pools and slots == 1:
        # Each pool advances on its own turns, including even-sized pools.
        projects = probes[:1] if ordinary_rotation % 2 else frozen_projects
    elif both_pools:
        projects = frozen_projects[:slots - 1] + probes[:1] + frozen_projects[slots - 1:]
    else:
        projects = frozen_projects or probes
    priority_project, priority_key = priority if priority else (None, None)
    if priority_project is not None:
        projects = [priority_project] + [project for project in projects
                                         if project != priority_project]
    batches = []
    model_calls = 0
    seen = set()
    attempted_projects = set()
    normal_project_attempted = False
    if not projects:
        seed = None
        known = control.get('known_projects') or []
        known = [p for p in known if _publication_project_allowed(p, policy_raw)]
        if known:
            seed = known[0]
        elif base.get('project_slug'):
            if _publication_project_allowed(base['project_slug'], policy_raw):
                seed = base['project_slug']
        if not seed:
            fields['status'] = 'IDLE'
            fields['model_calls'] = 0
            return fields
        config_path, cfg = _project_runtime_cfg(
            _runtime_base_with_control_publication(base, control), seed, runtime_state,
            lane=lane, database=database)
        tick_budget.check('publication:' + lane)
        result = publication_tick(config_path, cfg, control, database, now=moment,
                                  lane=lane, eligible_filter=eligible_filter,
                                  _hub_preflight_state=_hub_preflight_state,
                                  _require_committed_inputs=_require_committed_inputs)
        tick_budget.completed(lane, result)
        if result.get('status') == 'DEFERRED_HUB_PREFLIGHT':
            return _deferred_hub_preflight(result)
        batches.append({
            'project': seed,
            'status': result.get('status'),
            'reason': result.get('reason'),
            'batch': result.get('batch'),
        })
        model_calls += int(result.get('model_calls') or 0)
        seen.add(seed)
        attempted_projects.add(seed)
        cycle = active_cycle(database, lane)
        projects = [p for p in cycle_projects(cycle) if p not in seen
                    and _publication_project_allowed(p, policy_raw)]
        if result.get('status') == 'PUBLISHED':
            projects.append(seed)
        fields.update(_audit_fields({k: result.get(k, fields.get(k)) for k in fields}))
        fields['cycle_id'] = result.get('cycle_id') or (cycle or {}).get('id')
    from collections import deque
    selected = []
    admitted_projects = set(seen)
    for project in dict.fromkeys(projects):
        tick_budget.check('maintenance.py:metadata_or_work')
        if project not in admitted_projects and len(admitted_projects) >= limit:
            continue
        admitted_projects.add(project)
        selected.append(project)
    projects = deque(selected)
    multi = 'max_batches_per_tick' in policy_raw
    while projects and len(batches) < budget:
        tick_budget.check('maintenance.py:metadata_or_work')
        project = projects.popleft()
        cycle_before = active_cycle(database, lane)
        config_path, cfg = _project_runtime_cfg(
            _runtime_base_with_control_publication(base, control), project, runtime_state,
            lane=lane, database=database)
        if project in transport_resume_projects:
            # Preserve the existing reserved token binding when refreshing the
            # selected runtime config; do not mint or rebase a retry token.
            cfg['maintenance_terminal_generation_retry'] = transport_resume_projects[project]
            Path(config_path).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        tick_budget.check('publication:' + lane)
        priority_kwargs = ({'prepare_priority_seen_key': priority_key}
                           if project == priority_project else {})
        result = publication_tick(
            config_path, cfg, control, database, now=moment,
            lane=lane, eligible_filter=eligible_filter,
            _hub_preflight_state=_hub_preflight_state,
            _require_committed_inputs=_require_committed_inputs,
            **priority_kwargs,
        )
        if project != priority_project:
            normal_project_attempted = True
        tick_budget.completed(lane, result)
        if result.get('status') == 'DEFERRED_HUB_PREFLIGHT':
            deferred = _deferred_hub_preflight(result)
            deferred['model_calls'] = model_calls + int(result.get('model_calls') or 0)
            deferred['batches'] = batches + [{
                'project': project,
                'status': result.get('status'),
                'reason': result.get('reason'),
                'batch': result.get('batch'),
            }]
            return deferred
        batches.append({
            'project': project,
            'status': result.get('status'),
            'reason': result.get('reason'),
            'batch': result.get('batch'),
        })
        model_calls += int(result.get('model_calls') or 0)
        fields.update(_audit_fields({k: result.get(k, fields.get(k)) for k in fields}))
        fields['cycle_id'] = result.get('cycle_id') or (cycle or {}).get('id')
        attempted_projects.add(project)
        refreshed_cycle = active_cycle(database, lane)
        cycle_before_id = cycle_before.get('id') if cycle_before else None
        if refreshed_cycle and refreshed_cycle.get('id') != cycle_before_id:
            # A per-project tick can open an exact scoped successor (for
            # example, a qualified summary retry). Drop the frozen predecessor
            # queue and continue from the new ACTIVE cycle, without revisiting
            # projects already attempted in this drain or exceeding its project
            # cap. The predecessor's pending members remain in SCOPED_DRAINED.
            remaining_project_slots = max(0, limit - len(attempted_projects))
            refreshed_projects = [
                candidate for candidate in cycle_projects(refreshed_cycle)
                if candidate not in attempted_projects
                and _publication_project_allowed(candidate, policy_raw)
            ]
            projects = deque(refreshed_projects[:remaining_project_slots])
            cycle = refreshed_cycle
            fields['cycle_id'] = refreshed_cycle['id']
        # Continue only after actual progress. A failed/empty/no-model attempt
        # consumes a batch slot and never spins on the same project.
        elif multi and result.get('status') == 'PUBLISHED':
            projects.append(project)
    # A priority-only attempt is extra to the frozen rotation. Keep the next
    # ordinary project at the front for the next tick; if ordinary work also
    # ran, advance rotation once as before.
    if rotation_had_projects and (priority_project is None or normal_project_attempted
                                 or budget_priority):
        with database:
            database.execute(
                f'INSERT OR REPLACE INTO {rotation_table} VALUES(1,?)',
                (rotation + 1,),
            )
    status = 'OK'
    if any(
        str(item.get('status') or '').startswith('BLOCKED')
        or item.get('status') in {'QUARANTINED', 'UNKNOWN_REQUIRES_RECONCILIATION'}
        or item.get('reason') in POST_PUBLICATION_ATTENTION
        for item in batches
    ):
        status = 'DEGRADED'
    elif model_calls == 0 and (not batches or all(
        item.get('status') in {'IDLE', 'NO_PENDING', 'SKIPPED_NO_DUE_BATCH', 'DISABLED'}
        for item in batches
    )):
        status = batches[0]['status'] if batches else 'IDLE'
    fields['status'] = status
    fields['batches'] = batches
    fields['model_calls'] = model_calls
    return fields


@contextmanager
def caretaker_maintenance_lock(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    # A kernel lock is released on process exit, including crash. The persistent
    # file is never deleted and a second tick cannot own it concurrently.
    try:
        lock=open(state/'maintenance.lock','a+b')
    except OSError:
        yield False
        return
    try:
        import msvcrt
        try:
            lock.seek(0)
            if lock.read(1)==b'':lock.write(b'0');lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
    finally:
        lock.close()


def _tick_family_control(control, state):
    """Give each enabled normal-work family a full-budget slot in rotation.

    The cursor is a small runtime counter protected by the maintenance lock.
    Only the in-memory copy is narrowed; the owner control file is never edited.
    """
    publication = control.get('publication', {}) or {}
    new_lane = control.get('new_event_epoch', {}) or {}
    if not isinstance(publication, dict) or publication.get('enabled') is not True:
        return control, None, []
    families = []
    if publication.get('include_existing_hub_backlog') is True:
        families.append('legacy_publication')
    if isinstance(new_lane, dict) and new_lane.get('enabled') is True:
        families.append('new_publication')
    if not families:
        return control, None, []
    families.append('source_intake')

    cursor_path = Path(state) / 'publication_family_rotation.json'
    next_slot = 0
    try:
        cursor = json.loads(cursor_path.read_text(encoding='utf-8'))
        candidate = cursor.get('next_slot') if isinstance(cursor, dict) else None
        if (isinstance(cursor, dict) and cursor.get('schema') == 1
                and isinstance(candidate, int)
                and not isinstance(candidate, bool) and 0 <= candidate < len(families)):
            next_slot = candidate
    except (OSError, ValueError, TypeError):
        pass

    family = families[next_slot]
    # Persist the next slot before any potentially long work, so a failed or
    # budget-exhausted tick cannot starve the other families again.
    tick_budget.atomic_json(cursor_path, {
        'schema': 1,
        'next_slot': (next_slot + 1) % len(families),
    })
    runtime_control = json.loads(json.dumps(control))
    if family == 'new_publication':
        runtime_control['publication']['include_existing_hub_backlog'] = False
    elif family == 'source_intake':
        runtime_control['publication']['enabled'] = False
    runtime_control['_runtime_work_family'] = family
    deferred = [item for item in families if item != family]
    return runtime_control, family, deferred


def tick(control_path):
    control=json.loads(Path(control_path).read_text(encoding='utf-8'))
    state=Path(control['state_dir'])
    with caretaker_maintenance_lock(state) as acquired:
        if not acquired:
            return {'status':'ALREADY_RUNNING'}
        budget = tick_budget.TickBudget(control.get('max_tick_seconds', 1200), state)
        with budget.activate():
            try:
                runtime_control, family, deferred = _tick_family_control(control, state)
                if family:
                    budget.report.update(
                        work_stage='publication_family:' + family,
                        runtime_deferred=deferred,
                    )
                report = _locked_tick(runtime_control,state)
                if family:
                    report.update(
                        work_stage='publication_family:' + family,
                        runtime_deferred=deferred,
                    )
            except tick_budget.TickBudgetExceeded:
                return budget.finish(budget.report, 'DEFERRED_BUDGET')
            except Exception as exc:
                budget.report['error_type'] = type(exc).__name__
                budget.finish(budget.report, 'FAILED')
                raise
            return budget.finish(report)


def _held_legacy_publication(base, control, database, state, now):
    """Report historical work without starting or draining its audit cycle."""
    from caretaker.audit import active_cycle, ensure_tables, policy_from, summarize
    from caretaker.publication import PublicationError

    ensure_tables(database)
    vault = Path(base['hub']['vault'])
    new_filter = _new_epoch_proposal_filter(database, state, control, vault)
    try:
        summary = summarize(vault, policy_from(control.get('publication', {}) or {}),
                            now=now.timestamp(), eligible_filter=lambda meta: not new_filter(meta))
    except PublicationError as exc:
        return {'status': 'DEGRADED', 'reason': str(exc)}
    cycle = active_cycle(database)
    pending_queue = database.execute('''
        SELECT count(*) FROM queue q
        LEFT JOIN new_epoch_baseline_events b ON b.event_id=q.id
        LEFT JOIN new_epoch_events n ON n.event_id=q.id
        WHERE q.forwarded=0 AND coalesce(n.reason,'')!='caretaker_self_activity'
          AND (b.event_id IS NOT NULL OR n.status IS NULL OR n.status!='admitted')
    ''').fetchone()[0]
    pending_checkpoints = len(_pending_checkpoint_rows(database, 'legacy'))
    running = _legacy_running_count(database)
    has_legacy = bool(summary['raw_eligible'] or cycle or pending_queue or pending_checkpoints or running)
    return {'status': 'DEGRADED' if has_legacy else 'IDLE',
            'reason': 'legacy_backlog_held' if has_legacy else None,
            'raw_eligible': summary['raw_eligible'],
            'pending_queue': pending_queue,
            'pending_checkpoints': pending_checkpoints, 'running_publications': running,
            'active_cycle_id': cycle['id'] if cycle else None, 'model_calls': 0}


def _scheduled_legacy_publication(result, runtime_work_family):
    """Label only the backlog intentionally skipped by the new-lane slot."""
    if (runtime_work_family == 'new_publication'
            and isinstance(result, dict)
            and result.get('status') == 'DEGRADED'
            and result.get('reason') == 'legacy_backlog_held'):
        result = dict(result)
        result.update(
            status='DEFERRED_SCHEDULED',
            reason='legacy_publication_deferred_by_family_rotation',
            scheduled=True,
        )
    return result



def drain_intake_checkpoints(base):
    # The existing caretaker completes bounded retained receipts before staging
    # new sources. Canonical gateway alone validates and commits exact pairs.
    from caretaker.checkpoint import _trusted_gateway
    vault = Path(base['hub']['vault'])
    gateway = _trusted_gateway(vault)
    if gateway is None or not callable(getattr(gateway, 'recover_pending_intake', None)):
        return {'status': 'UNAVAILABLE', 'new_sources': 0}
    import sys as gateway_sys
    # Dynamic gateway loading must resolve its reviewed sibling lint module.
    # Use only that trusted code directory, restore caller import paths.
    previous_path = list(gateway_sys.path)
    try:
        directory = str(Path(gateway.__file__).resolve().parent)
        if directory not in gateway_sys.path:
            gateway_sys.path.insert(0, directory)
        return gateway.recover_pending_intake(vault, limit=8)
    except tick_budget.TickBudgetExceeded:
        raise
    except Exception as exc:
        return {'status': 'BLOCKED', 'reason': type(exc).__name__, 'new_sources': 0}
    finally:
        gateway_sys.path[:] = previous_path


def _safe_partial_checkpoint_recovery(value):
    """Allow draining retained publications after only unrelated lock blocks."""
    if (type(value) is not dict or value.get('status') != 'PARTIAL'
            or type(value.get('results')) is not list or not value['results']
            or type(value.get('candidate_count')) is not int
            or value['candidate_count'] < len(value['results'])
            or type(value.get('new_sources')) is not int or value['new_sources'] != 0):
        return False
    allowed = {'CHECKPOINTED', 'ALREADY_CHECKPOINTED', 'BLOCKED_CHECKPOINT_LOCK',
               'BLOCKED_CONCURRENT_PATH_CHANGE'}
    seen = set()
    blocked = 0
    for item in value['results']:
        if (type(item) is not dict or item.get('action') != 'checkpoint-intake'
                or item.get('required') is not True
                or item.get('mode') != 'local-commit-only'
                or item.get('policy_source') != 'git-head'
                or not isinstance(item.get('machine'), str) or not item['machine'].strip()
                or item.get('curator_machine') != item.get('machine')
                or item.get('status') not in allowed
                or any(item.get(key) not in (None, '', 0, False)
                       for key in ('error', 'error_type', 'exception'))
                or ('lint_error_count' in item
                    and (type(item['lint_error_count']) is not int
                         or item['lint_error_count'] != 0))
                or ('lint_warning_count' in item
                    and (type(item['lint_warning_count']) is not int
                         or item['lint_warning_count'] < 0))
                or item.get('dirty_tree_write_blocked') is True
                or item.get('dirty_tree_state') not in (None, 'clean')):
            return False
        proposal_id = item.get('proposal_id')
        if (not isinstance(proposal_id, str)
                or not re.fullmatch(r'prop-[A-Za-z0-9._-]{1,160}', proposal_id)
                or proposal_id in seen):
            return False
        seen.add(proposal_id)
        status = item['status']
        if status in {'CHECKPOINTED', 'ALREADY_CHECKPOINTED'}:
            commit = item.get('commit')
            if (not isinstance(commit, str) or not re.fullmatch(r'[a-f0-9]{40}', commit)
                    or type(item.get('path_count')) is not int or item['path_count'] < 1
                    or type(item.get('lint_error_count')) is not int
                    or item['lint_error_count'] != 0
                    or type(item.get('lint_warning_count')) is not int
                    or item['lint_warning_count'] < 0
                    or item.get('stale_lock_recovered') is True):
                return False
        else:
            if item.get('commit') not in (None, '') or item.get('git_summary') not in (None, ''):
                return False
            blocked += 1
    return blocked > 0


def _locked_tick(control,state):
    runtime_work_family = control.pop('_runtime_work_family', None)
    if runtime_work_family not in (None, 'legacy_publication', 'new_publication', 'source_intake'):
        raise ValueError('invalid_runtime_work_family')
    publication_only = runtime_work_family in {'legacy_publication', 'new_publication'}
    tick_budget.check('initializing')
    database=sqlite3.connect(state/'intake.sqlite')
    if tick_budget.current():
        tick_budget.current().cleanups.append(database.close)
    database.execute('PRAGMA journal_mode=WAL')
    database.executescript('CREATE TABLE IF NOT EXISTS queue (id TEXT PRIMARY KEY, project TEXT, payload TEXT NOT NULL, forwarded INTEGER NOT NULL DEFAULT 0); CREATE TABLE IF NOT EXISTS scans (path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, observed TEXT NOT NULL); CREATE TABLE IF NOT EXISTS source_rotation (id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL);')
    database.execute('CREATE TABLE IF NOT EXISTS project_runs(project TEXT PRIMARY KEY, observed TEXT NOT NULL)')
    database.execute('CREATE TABLE IF NOT EXISTS conflicts(id TEXT, payload_hash TEXT, payload TEXT, PRIMARY KEY(id,payload_hash))')
    database.execute('CREATE TABLE IF NOT EXISTS publications(batch TEXT PRIMARY KEY,status TEXT,failures INTEGER,result TEXT)')
    database.execute('CREATE TABLE IF NOT EXISTS pipeline_failures(project TEXT PRIMARY KEY, signature TEXT, count INTEGER, retry_epoch TEXT)')
    if 'next_due' not in {r[1] for r in database.execute('PRAGMA table_info(pipeline_failures)')}:
        database.execute("ALTER TABLE pipeline_failures ADD COLUMN next_due TEXT NOT NULL DEFAULT ''")
    new_control = control.get('new_event_epoch', {}) or {}
    if not isinstance(new_control, dict):
        raise ValueError('invalid_new_event_epoch_control')
    document_ids = _document_epoch_source_ids(control)
    new_lane_enabled = new_control.get('enabled') is True
    if new_lane_enabled or new_control.get('activate') is True or _new_epoch_table_exists(database):
        _ensure_new_epoch_tables(database)
    base=json.loads(Path(control['base_config']).read_text(encoding='utf-8'))
    if new_control.get('activate') is True and runtime_work_family is None:
        activate_new_event_epoch(database, base['hub']['vault'])
    epoch_active = (_new_epoch_table_exists(database) and
                    database.execute('SELECT 1 FROM new_event_epoch WHERE id=1').fetchone() is not None)
    now=dt.datetime.now(dt.timezone.utc).isoformat();report={'observed_at':now,'status':'OK','new_events':0,'source_issues':[],'projects':[]}
    if tick_budget.current():
        tick_budget.current().attach(report)
    from caretaker.transports import TransportError, resolve_fleet_runtime
    try:
        runtime = resolve_fleet_runtime()
        report['fleet_runtime'] = {'version': runtime['version_text'],
                                   'path': str(runtime['path']),
                                   'pinned': runtime['pinned']}
    except (TransportError, OSError):
        report['fleet_runtime'] = {'status': 'unavailable',
                                   'pinned': os.environ.get('AGENT_FLEET_SUITE_PIN', '').strip().casefold()
                                             in {'1', 'true', 'yes', 'on'}}
    from caretaker.registry import read_registry
    _, registry_warning = read_registry(Path(base['hub']['vault']))
    if registry_warning:
        report['registry_warning'] = registry_warning
    from caretaker.roll_forward import recover_stale_curator_lock
    tick_budget.check('intake')
    recovery = recover_stale_curator_lock(database, base, state, control=control)
    report['curator_lock_recovery'] = recovery
    report['pending_intake_recovery'] = drain_intake_checkpoints(base)
    pending_recovery = report['pending_intake_recovery']
    partial_checkpoint_recovery = _safe_partial_checkpoint_recovery(pending_recovery)
    if (type(pending_recovery) is not dict
            or type(pending_recovery.get('status')) is not str
            or pending_recovery.get('status') not in {'COMPLETED', 'IDLE'}
            and not partial_checkpoint_recovery):
        report.update(
            status='DEFERRED_INTAKE_RECOVERY',
            publication={
                'status': 'DEFERRED_INTAKE_RECOVERY',
                'reason': 'intake_checkpoint_recovery_incomplete',
                'model_calls': 0,
                'batches': [],
            },
        )
        if new_lane_enabled:
            report['new_lane'] = {
                'status': 'DEFERRED_INTAKE_RECOVERY',
                'publication': report['publication'],
            }
        elif epoch_active:
            report['new_lane'] = {'status': 'PAUSED'}
        else:
            report['new_lane'] = {'status': 'DISABLED'}
        if tick_budget.current() is None:
            database.close()
            return tick_budget.TickBudget(state=state).finish(report)
        return report
    publication_preflight_state = {}

    def _finish_global_hub_defer(result):
        deferred = _deferred_hub_preflight(result)
        if type(result) is dict:
            reported_calls = result.get('model_calls')
            reported_batches = result.get('batches')
            if type(reported_calls) is int and reported_calls >= 0:
                deferred['model_calls'] = reported_calls
            if type(reported_batches) is list:
                deferred['batches'] = reported_batches
        report.update(
            status='DEFERRED_HUB_PREFLIGHT',
            global_hub_preflight=deferred['reason'],
            publication=deferred,
        )
        if new_lane_enabled:
            report['new_lane'] = {
                'status': 'DEFERRED_HUB_PREFLIGHT', 'publication': deferred,
            }
        elif epoch_active:
            report['new_lane'] = {'status': 'PAUSED'}
        else:
            report['new_lane'] = {'status': 'DISABLED'}
        if tick_budget.current() is None:
            database.close()
            return tick_budget.TickBudget(state=state).finish(report)
        return report

    def _drain_publication(lane='legacy'):
        return publication_drain(
            control, base, database, state, lane=lane, recovery=recovery,
            _hub_preflight_state=publication_preflight_state,
            _require_committed_inputs=partial_checkpoint_recovery,
        )

    tick_budget.check('publication')
    # Drain already-admitted knowledge before source discovery or model work.
    # Keep exactly one drain per lane and the existing locks/budgets; proposals
    # produced later in this tick become eligible on the next normal tick.
    if runtime_work_family is not None:
        # The same independent Hub gate also protects the intake-only slot;
        # a skipped publication drain must not become a way around that gate.
        publication_preflight_state['result'] = _global_hub_preflight(base)
        if not _valid_hub_preflight(publication_preflight_state['result']):
            return _finish_global_hub_defer(publication_preflight_state['result'])
        if (runtime_work_family == 'source_intake' and not partial_checkpoint_recovery
                and new_control.get('activate') is True):
            activate_new_event_epoch(database, base['hub']['vault'])
            epoch_active = database.execute('SELECT 1 FROM new_event_epoch WHERE id=1').fetchone() is not None

        def _scheduled_family(family):
            return {'status': 'DEFERRED_SCHEDULED', 'scheduled': True,
                    'reason': family + '_deferred_by_family_rotation',
                    'model_calls': 0, 'batches': []}

        ready_publication = (_drain_publication()
                             if runtime_work_family == 'legacy_publication'
                             else _scheduled_family('legacy_publication'))
        ready_new_publication = (_drain_publication(lane='new')
                                 if runtime_work_family == 'new_publication' and new_lane_enabled and epoch_active
                                 else _scheduled_family('new_publication'))
    elif new_lane_enabled or epoch_active:
        ready_publication = (_drain_publication()
                             if control.get('publication',{}).get('include_existing_hub_backlog') is True
                             else None)
        if (type(ready_publication) is dict
                and ready_publication.get('status') == 'DEFERRED_HUB_PREFLIGHT'):
            return _finish_global_hub_defer(ready_publication)
        ready_new_publication = (_drain_publication(lane='new')
                                 if new_lane_enabled and epoch_active else None)
    else:
        ready_publication = _drain_publication()
        ready_new_publication = None
    if (type(ready_publication) is dict
            and ready_publication.get('status') == 'DEFERRED_HUB_PREFLIGHT'):
        return _finish_global_hub_defer(ready_publication)
    if (type(ready_new_publication) is dict
            and ready_new_publication.get('status') == 'DEFERRED_HUB_PREFLIGHT'):
        return _finish_global_hub_defer(ready_new_publication)
    # Keep work completed by the publication drain in the tick's durable
    # report before source discovery can consume the remaining budget.
    if ready_publication is not None:
        report['publication'] = _scheduled_legacy_publication(
            ready_publication, runtime_work_family,
        )
    if new_lane_enabled or epoch_active:
        early_lane = _new_epoch_counts(database, control)
        if new_lane_enabled and epoch_active:
            early_lane['publication'] = ready_new_publication
            early_lane['status'] = (ready_new_publication.get('status', 'IDLE')
                                    if isinstance(ready_new_publication, dict) else 'IDLE')
        elif epoch_active:
            early_lane['status'] = 'PAUSED'
        report['new_lane'] = early_lane
    else:
        report['new_lane'] = {**_new_epoch_counts(database, control), 'status': 'DISABLED'}
    if not publication_only:
        tick_budget.check('source_discovery')
    candidates=[]
    files_sources=[]
    for source in ([] if partial_checkpoint_recovery or publication_only else control['sources']):
        tick_budget.check('maintenance.py:metadata_or_work')
        source_label=_source_label(source)
        try:
            if str(source.get('kind') or '').strip().lower()=='files':
                if (source.get('scheduled') is not False
                        and source.get('status') not in {'disabled', 'unsupported'}):
                    files_sources.append(source)
                continue
            for name in glob.iglob(source['glob'],recursive=True):
                tick_budget.check('maintenance.py:metadata_or_work')
                p=Path(name)
                if not p.is_file() or p.is_symlink() or p.is_junction():continue
                stat=p.stat()
                # The opt-in changes the collector's evidence contract even when
                # the native file is unchanged.  Include it in the durable scan
                # fingerprint so enabling user messages re-reads completed files
                # that were already marked scanned while the option was off.
                include_users = bool(source.get('include_user_messages',False))
                finger=f'scan_schema={CONNECTOR_SCAN_SCHEMA_VERSION}:{stat.st_mtime_ns}:{stat.st_size}:include_user_messages={int(include_users)}'
                if source['kind']=='sqlite':
                    wal=Path(str(p)+'-wal')
                    if wal.is_file():
                        ws=wal.stat();finger+=f':wal:{ws.st_mtime_ns}:{ws.st_size}'
                old=database.execute('SELECT fingerprint,observed FROM scans WHERE path=?',(str(p),)).fetchone()
                if old and old[0]==finger:continue
                candidates.append((old[1] if old else '',str(p),finger,source))
        except (OSError,ValueError,KeyError,TypeError) as exc:
            report['source_issues'].append({'source':source_label,'status':'error','reason':_stable_tick_source_reason(exc)})
    rotation_row=database.execute('SELECT value FROM source_rotation WHERE id=1').fetchone()
    rotation=rotation_row[0] if rotation_row else 0
    selected=_select_source_candidates(candidates,control.get('max_sources_per_tick',24),rotation)
    if candidates:
        with database:database.execute('INSERT OR REPLACE INTO source_rotation VALUES(1,?)',(rotation+1,))
    for _,name,finger,source in selected:
        tick_budget.check('maintenance.py:metadata_or_work')
        include_users = bool(source.get('include_user_messages',False))
        events=[]
        try:
            if source['kind']=='fleet_receipt':
                item=inspect_receipt(Path(name));events=[item.event] if item.event else []
                if item.status!='accepted':report['source_issues'].append({'source':name,'status':item.status,'reason':item.reason})
            else:
                result=collect_sources([SourceSpec(tool=source['tool'],path=name,kind=source['kind'],exclude_native_session_ids=tuple(control.get('exclude_native_session_ids',[])),max_records=2048,max_bytes=128*1024*1024,max_excerpt_bytes=16000,include_user_messages=include_users)])
                from caretaker.connectors import event_to_dict as _event_to_dict
                events=[_event_to_dict(e) for e in result.events]
                report['source_issues'].extend(i.to_dict() for i in result.issues)
            with database:
                tick_budget.check('source_commit')
                _enqueue_source_events(database,events,report,name,control)
                database.execute('INSERT OR REPLACE INTO scans VALUES(?,?,?)',(name,finger,now))
        except (OSError,ValueError,sqlite3.Error,KeyError,TypeError) as exc:
            report['source_issues'].append({'source':name,'status':'error','reason':_stable_tick_source_reason(exc)})
    for source in files_sources:
        tick_budget.check('maintenance.py:metadata_or_work')
        source_label=_source_label(source)
        try:
            result=_collect_files_source(source,state)
            report['source_issues'].extend(i.to_dict() for i in result.issues)
            with database:
                _enqueue_source_events(database,result.events,report,source_label,control)
        except (OSError,ValueError,sqlite3.Error,KeyError,TypeError) as exc:
            report['source_issues'].append({'source':source_label,'status':'error','reason':_stable_tick_source_reason(exc)})
    # Reclassify stored events when the optional by-design prefixes are enabled.
    if not publication_only and not partial_checkpoint_recovery and control.get('unassigned_by_design_workspace_prefixes'):
        with database:
            for ident, payload in database.execute('SELECT id,payload FROM queue WHERE project IS NOT NULL').fetchall():
                tick_budget.check('maintenance.py:metadata_or_work')
                if _unassigned_by_design(json.loads(payload), control):
                    database.execute('UPDATE queue SET project=NULL WHERE id=?', (ident,))
    # Mappings may have been explicitly completed since the previous tick.
    if not publication_only and not partial_checkpoint_recovery:
        with database:
            for ident,payload in database.execute('SELECT id,payload FROM queue WHERE project IS NULL').fetchall():
                tick_budget.check('maintenance.py:metadata_or_work')
                event=json.loads(payload)
                project=None if _unassigned_by_design(event, control) else route(event,control)
                if project:database.execute('UPDATE queue SET project=? WHERE id=?',(project,ident))
    if epoch_active and not publication_only and not partial_checkpoint_recovery:
        _admit_new_epoch_events(database, document_ids, state=state)
    if partial_checkpoint_recovery or publication_only:
        projects = []
    elif new_lane_enabled:
        projects=[x[0] for x in database.execute('''
            SELECT DISTINCT q.project FROM new_epoch_events n JOIN queue q ON q.id=n.event_id
            LEFT JOIN project_runs p ON q.project=p.project
            WHERE n.status='admitted' AND n.forwarded>=0 AND q.project IS NOT NULL
            ORDER BY coalesce(p.observed,''),q.project''')] if epoch_active else []
    elif epoch_active:
        projects=[]
    else:
        projects=[x[0] for x in database.execute("SELECT DISTINCT q.project FROM queue q LEFT JOIN project_runs p ON q.project=p.project WHERE q.project IS NOT NULL ORDER BY coalesce(p.observed,''),q.project")]
    if (not publication_only and not partial_checkpoint_recovery and not new_lane_enabled and not epoch_active
            and control.get('publication',{}).get('include_existing_hub_backlog') is True):
        from caretaker.backlog import pending_projects
        for project in pending_projects(Path(base['hub']['vault']),control.get('known_projects',[])):
            tick_budget.check('maintenance.py:metadata_or_work')
            if project not in projects:projects.append(project)
        observed=dict(database.execute('SELECT project,observed FROM project_runs'))
        projects.sort(key=lambda p:(observed.get(p,''),p))
    for project in projects[:control.get('max_projects_per_tick',4)]:
        tick_budget.check('maintenance.py:metadata_or_work')
        project_root=state/('new-event-epoch' if new_lane_enabled else '')/'projects'/project;project_root.mkdir(parents=True,exist_ok=True)
        if new_lane_enabled:
            events=[json.loads(x[0]) for x in database.execute('''
                SELECT q.payload FROM queue q JOIN new_epoch_events n ON n.event_id=q.id
                WHERE q.project=? AND n.status='admitted' AND n.forwarded=0 LIMIT 64''',(project,))]
        else:
            events=[json.loads(x[0]) for x in database.execute('SELECT payload FROM queue WHERE project=? AND forwarded=0 LIMIT 64',(project,))]
        cfg=json.loads(json.dumps(base));cfg.update(project_slug=project,session_id='logical:openclaw-maintenance:'+project,state_dir=str(project_root/'state'),maintenance_state_dir=str(state))
        cfg['hub']['context_receipt']=str(project_root/'receipts/context.json')
        cfg['office']['session_id']=cfg['session_id']
        tools=sorted({e['tool'] for e in events})
        cfg['allowlist'].update(tools=tools or ['openclaw'],max_body_bytes=16384,max_excerpt_bytes=16000)
        cfg['limits'].update(max_records_per_run=8,max_prompt_bytes=100000)
        cfg['inputs']=[]
        for tool in tools:
            tick_budget.check('maintenance.py:metadata_or_work')
            p=project_root/(tool+'.jsonl')
            p.write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in events if e['tool']==tool),encoding='utf-8')
            cfg['inputs'].append({'path':str(p),'kind':'normalized_events','tool':tool})
        if not cfg['inputs']:
            p=project_root/'empty.jsonl';p.write_text('',encoding='utf-8');cfg['inputs']=[{'path':str(p),'kind':'normalized_events','tool':'openclaw'}]
        config_path=project_root/'config.json';config_path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
        retry_epoch=str(control.get('retry_epoch','initial'))
        previous_failure=database.execute('SELECT count,retry_epoch,next_due FROM pipeline_failures WHERE project=?',(project,)).fetchone()
        if previous_failure and previous_failure[2] and previous_failure[2]>now:
            report['projects'].append({'project':project,'pipeline':{'exit_code':1,'status':'BACKOFF_DEFERRED','next_due':previous_failure[2]},'publication':{'status':'DECOUPLED'}})
            with database:database.execute('INSERT OR REPLACE INTO project_runs VALUES(?,?)',(project,now))
            continue
        env=os.environ.copy()
        if tick_budget.current():
            env['HUB_CARETAKER_TICK_DEADLINE'] = str(tick_budget.current().deadline)
        env.update(HUB_CARETAKER_ALLOW_EXEC='1',PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
        try:
            if tick_budget.current():
                # Run the existing CLI entry in-process: the cooperative budget
                # reaches metadata, fallback and transports, and no timeout can
                # abandon an extra parent process with native children.
                import io
                from contextlib import redirect_stdout
                from run import main as pipeline_main
                output = io.StringIO()
                previous_exec = os.environ.get('HUB_CARETAKER_ALLOW_EXEC')
                os.environ['HUB_CARETAKER_ALLOW_EXEC'] = '1'
                try:
                    with tick_budget.current().limit_to(600), redirect_stdout(output):
                        exit_code = pipeline_main(['--config',str(config_path),'--once','--worktree-root',''])
                    result=json.loads(output.getvalue()); result['exit_code']=exit_code
                finally:
                    if previous_exec is None:
                        os.environ.pop('HUB_CARETAKER_ALLOW_EXEC',None)
                    else:
                        os.environ['HUB_CARETAKER_ALLOW_EXEC']=previous_exec
            else:
                proc=subprocess.run([control['python'],'-B','-X','utf8',str(Path(__file__).with_name('run.py')),'--config',str(config_path),'--once','--worktree-root',''],env=env,capture_output=True,encoding='utf-8',errors='replace',timeout=600,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                result=json.loads(proc.stdout);result['exit_code']=proc.returncode
            tick_budget.check('pipeline_result')
        except (subprocess.TimeoutExpired,ValueError,OSError) as exc:
            result={'status':'BLOCKED','error_type':type(exc).__name__}
        signature=json.dumps([result.get('incidents'),result.get('notes'),result.get('error_type')],sort_keys=True)
        previous=database.execute('SELECT signature,count,retry_epoch FROM pipeline_failures WHERE project=?',(project,)).fetchone()
        count=(previous[1]+1 if previous and previous[0]==signature and previous[2]==retry_epoch else 1) if result.get('exit_code')!=0 else 0
        from caretaker.pubstate import backoff_due_iso
        next_due=backoff_due_iso(count) if count else ''
        with database:database.execute('INSERT OR REPLACE INTO pipeline_failures(project,signature,count,retry_epoch,next_due) VALUES(?,?,?,?,?)',(project,signature,count,retry_epoch,next_due))
        store=Store(str(project_root/'state/caretaker.sqlite'))
        with database:
            for event in events:
                tick_budget.check('maintenance.py:metadata_or_work')
                if store.cursor_seen(tool=event['tool'],native_session_id=event['native_session_id'],cursor=event['cursor']):
                    if new_lane_enabled:
                        database.execute('UPDATE new_epoch_events SET forwarded=1 WHERE event_id=?',(event_id(event),))
                    else:
                        database.execute('UPDATE queue SET forwarded=1 WHERE id=?',(event_id(event),))
                else:
                    rejected=store._conn.execute('SELECT filter_reason FROM events WHERE identity_tool=? AND native_session_id=? AND cursor=? AND filter_reason!=? LIMIT 1',(event['tool'],event['native_session_id'],event['cursor'],'accepted')).fetchone()
                    if rejected:
                        if new_lane_enabled:
                            database.execute('UPDATE new_epoch_events SET forwarded=-1 WHERE event_id=?',(event_id(event),))
                        else:
                            database.execute('UPDATE queue SET forwarded=-1 WHERE id=?',(event_id(event),))
                        report['source_issues'].append({'event_id':event_id(event),'status':'rejected','reason':rejected[0]})
        store.close()
        report['projects'].append({'project':project,'pipeline':result,'publication':{'status':'DECOUPLED'}})
        with database:database.execute('INSERT OR REPLACE INTO project_runs VALUES(?,?)',(project,now))
    if epoch_active:
        report['unassigned_events']=database.execute('''
            SELECT count(*) FROM queue q LEFT JOIN new_epoch_events n ON n.event_id=q.id
            WHERE q.project IS NULL AND coalesce(n.reason,'')!='caretaker_self_activity'
        ''').fetchone()[0]
        report['pending_forward']=database.execute('''
            SELECT count(*) FROM queue q LEFT JOIN new_epoch_events n ON n.event_id=q.id
            WHERE q.forwarded=0 AND q.project IS NOT NULL
              AND coalesce(n.reason,'')!='caretaker_self_activity'
        ''').fetchone()[0]
    else:
        report['unassigned_events']=database.execute('SELECT count(*) FROM queue WHERE project IS NULL').fetchone()[0]
        report['pending_forward']=database.execute('SELECT count(*) FROM queue WHERE forwarded=0 AND project IS NOT NULL').fetchone()[0]
    report['unassigned_by_design'] = _design_unassigned_count(database, control, exclude_self=epoch_active)
    report['unassigned_events'] -= report['unassigned_by_design']
    if epoch_active:
        report['rejected_events']=database.execute('''
            SELECT count(*) FROM queue q LEFT JOIN new_epoch_events n ON n.event_id=q.id
            WHERE q.forwarded=-1 AND coalesce(n.reason,'')!='caretaker_self_activity'
        ''').fetchone()[0]
    else:
        report['rejected_events']=database.execute('SELECT count(*) FROM queue WHERE forwarded=-1').fetchone()[0]
    variants=database.execute('SELECT q.payload,c.payload FROM conflicts c JOIN queue q ON c.id=q.id').fetchall()
    report['source_alias_events']=sum(source_alias_only(json.loads(a),json.loads(b)) for a,b in variants)
    report['conflicting_events']=len(variants)-report['source_alias_events']
    if new_lane_enabled or epoch_active:
        report['publication'] = (ready_publication if ready_publication is not None
                                 else _held_legacy_publication(base, control, database, state,
                                                              dt.datetime.fromisoformat(now)))
        report['publication'] = _scheduled_legacy_publication(
            report['publication'], runtime_work_family,
        )
        lane = _new_epoch_counts(database, control)
        if new_lane_enabled and epoch_active:
            lane['publication'] = ready_new_publication
            lane['pipeline_failures'] = sum(p['pipeline'].get('exit_code') != 0 for p in report['projects'])
            curator_lock_held = (lane['publication'].get('status') == 'BLOCKED'
                                 and lane['publication'].get('reason') == 'curator_lock_held')
            ambiguous_deferred = sum(count for reason, count in lane['deferred_reasons'].items()
                                     if reason not in {'completed_before_activation', 'not_completed_turn',
                                                       'superseded_by_turn_completion',
                                                       'observed_before_activation_without_completion',
                                                       'caretaker_self_activity'})
            lane['status'] = ('DEGRADED' if ambiguous_deferred or lane['rejected_events'] or
                              lane['unassigned_events'] or lane['pipeline_failures'] or
                              (not curator_lock_held and lane['publication'].get('status') in
                               {'DEGRADED','BLOCKED','QUARANTINED',
                                'UNKNOWN_REQUIRES_RECONCILIATION','CHECKPOINT_PENDING'})
                              else 'LOCK_HELD' if curator_lock_held
                              else lane['publication'].get('status', 'IDLE'))
        elif epoch_active:
            lane['status'] = 'PAUSED'
        monitored = _agent_proposal_inventory(database, Path(base['hub']['vault']), control)
        lane['agent_proposals'] = monitored['counts']
        lane['agent_proposals_available'] = monitored['available']
        report['new_lane'] = lane
    else:
        report['publication']=ready_publication
        report['new_lane'] = {**_new_epoch_counts(database, control), 'status': 'DISABLED'}
    tick_budget.check('final_metadata')
    if not publication_only and control.get('sync_hermes') is True:
        try:
            from caretaker.sync import sync_hermes
            report['hermes_sync']=sync_hermes(Path(base['hub']['vault']))
        except Exception as exc:report['hermes_sync']={'status':'BLOCKED','reason':type(exc).__name__}
    pub=report.get('publication') or {}
    pub_bad=(str(pub.get('status') or '').startswith('BLOCKED')
             and pub.get('reason') != 'curator_lock_held') or pub.get('status') in {'QUARANTINED','UNKNOWN_REQUIRES_RECONCILIATION','DEGRADED','CHECKPOINT_PENDING'}
    if any(str(item.get('status') or '').startswith('BLOCKED') or item.get('status') in {'QUARANTINED','UNKNOWN_REQUIRES_RECONCILIATION'} or item.get('reason') in POST_PUBLICATION_ATTENTION for item in pub.get('batches') or []):
        pub_bad=True
    lane_bad = report['new_lane']['status'] in {'DEGRADED','NOT_ACTIVATED','BLOCKED','QUARANTINED','UNKNOWN_REQUIRES_RECONCILIATION'}
    if any(i.get('status') in {'error','warning','skipped','unsupported','unavailable','rejected','conflict'} for i in report['source_issues']) or report['unassigned_events'] or report['rejected_events'] or report['conflicting_events'] or report.get('hermes_sync',{}).get('status')=='BLOCKED' or pub_bad or lane_bad or recovery['status']=='REQUIRES_REVIEW' or any(p['pipeline'].get('exit_code')!=0 for p in report['projects']):report['status']='DEGRADED'
    report['source_issue_count'], report['source_issues'], report['source_issue_summary'] = _summarize_source_issues(report['source_issues'])
    if tick_budget.current() is None:
        tick_budget.TickBudget(state=state).finish(report)
    database.close();return report


def reconcile_claude_duplicates_command(control_path, apply=False):
    """Reconcile the tick's own intake queue under the maintenance lock."""
    control = json.loads(Path(control_path).read_text(encoding='utf-8'))
    state = Path(control['state_dir'])
    with caretaker_maintenance_lock(state) as acquired:
        if not acquired:
            return {'status': 'ALREADY_RUNNING'}
        path = state / 'intake.sqlite'
        if not path.is_file():
            return {'checked': 0, 'duplicates_found': 0, 'applied': bool(apply),
                    'updated_queue': 0, 'updated_new_epoch': 0}
        database = sqlite3.connect(path)
        try:
            return reconcile_claude_duplicates(database, apply=apply)
        finally:
            database.close()


def build_parser():
    parser = argparse.ArgumentParser(
        prog='maintenance',
        description='Owner-configured maintenance tick; no task resumption or messaging.',
    )
    parser.add_argument('--control', required=True,
                        help='Path to the owner-configured control JSON.')
    parser.add_argument('--reconcile-claude-duplicates', action='store_true',
                        help='Report Claude queue rows whose 0.1.39 path-scoped id '
                             'duplicates a canonical row, instead of running a tick.')
    parser.add_argument('--apply', action='store_true',
                        help='Mark the reported duplicates superseded; the default is a dry run.')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.reconcile_claude_duplicates:
        report = reconcile_claude_duplicates_command(args.control, apply=args.apply)
    else:
        report = tick(args.control)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
