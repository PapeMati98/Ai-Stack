"""Exact local Git checkpoint of an already committed canonical publication.

No new curation authority, remote operations or deletion. Call under the owner's
curator lock only after the canonical publisher returns PUBLISHED.

0.1.17 (2026-09-23 incident): a sandboxed intake checkpoint staged its files and
could not unstage them; this checkpoint then stopped on the foreign stage and the
published page stayed outside Git with nothing to retry it. Now the checkpoint
  * holds the Hub's Git-internal intake checkpoint lock (knowledge.py protocol),
    so it never interleaves with an intake checkpoint; a lock it could not write
    or release is reported, never left behind silently;
  * is ONE transaction on the shared index under Git's own index lock, exactly
    as Git commits: `index.lock` is taken first (no Git process can write the
    index until it is released), the index is copied into a private index where
    the foreign-stage check, `add`, `write-tree` and the tree verification run,
    then `commit-tree` and a compare-and-swap `update-ref`, and only then the
    private index is installed through `index.lock`. On any failure before that
    the shared index was never touched, so there is nothing to clean up and
    nothing of another writer to lose;
  * owns an index entry only when it holds this publication's verified blob,
    never by path name; any other staged entry refuses the checkpoint;
  * on a retry accepts a shared file (log.md, wiki/index.md) that a LATER
    publication already committed only when HEAD contains, as whole lines, the
    rows that curation.py's own generator renders for THIS publication - and, for
    the log, only when no other event in the vault renders to the same row;
  * runs Git without inherited GIT_* variables (knowledge.py environment);
  * recovers a permanently stale intake lock only through the trusted
    knowledge.py helpers of this Hub checkout (owner-machine, policy-enabled,
    local-commit-only, git-head policy) before the bounded O_EXCL wait; a live,
    young, foreign or malformed lock is never broken.
"""
import datetime as dt
import hashlib, importlib.util, json, os, subprocess, time, uuid
from caretaker import tick_budget
from pathlib import Path
from .publication import PublicationError, safe_file

#: knowledge.py CHECKPOINT_LOCK_NAME - one lock for every Hub Git checkpoint writer.
INTAKE_LOCK_NAME = 'llm-wiki-intake-checkpoint.lock'
SHARED_FILES = ('log.md', 'wiki/index.md')
LOCK_WAIT_SECONDS = 15.0
_GENERATORS = {}
_GATEWAYS = {}
#: Exact vault-checkout binding, same layout as ``_generator`` (curation.py).
_GATEWAY_RELATIVE = ('skills', 'llm-wiki', 'scripts', 'knowledge.py')
_GATEWAY_PROTOCOL = ('checkpoint_policy', 'recover_stale_checkpoint_lock',
                     'lock_snapshot', 'remove_owned_checkpoint_lock')


def _machine_name() -> str:
    """knowledge.py local_machine_name(): the identity its stale-lock recovery compares."""
    if os.name == 'nt':
        try:
            import ctypes
            size = ctypes.c_uint(256)
            buffer = ctypes.create_unicode_buffer(size.value)
            if ctypes.windll.kernel32.GetComputerNameW(buffer, ctypes.byref(size)):
                return buffer.value
        except (AttributeError, OSError, ValueError):
            pass
        return ''
    import socket
    return socket.gethostname()


def _git_environment() -> dict:
    """knowledge.py git_subprocess_environment(): no inherited GIT_DIR/INDEX/WORK_TREE."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_EDITOR': 'true', 'GIT_ATTR_NOSYSTEM': '1',
                'GIT_CONFIG_NOSYSTEM': '1', 'GIT_NO_REPLACE_OBJECTS': '1'})
    return env


def _create_exclusive(path: Path, busy_reason: str) -> int:
    """O_EXCL creation with a bounded wait; never breaks an existing lock."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_BINARY', 0), 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise PublicationError(busy_reason)
            time.sleep(0.2)


def _set_aside(path: Path, tag: str) -> None:
    """Release a lock file this process created by renaming it (never deleting it)."""
    try:
        os.replace(path, path.with_name(path.name + '.' + tag + '-' + uuid.uuid4().hex))
    except OSError:
        raise PublicationError('checkpoint_lock_release_failed')


def _trusted_gateway(vault: Path):
    """knowledge.py of this Hub checkout: policy and guarded stale-lock recovery.

    Path is bound to the vault checkout layout only (never lock bytes, journal
    or config). An unreadable or protocol-incomplete file is not replaced by a
    weaker local fallback.
    """
    path = vault.parent.joinpath(*_GATEWAY_RELATIVE).resolve()
    if not path.is_file():
        return None
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if key not in _GATEWAYS:
            spec = importlib.util.spec_from_file_location(
                '_caretaker_knowledge_%d' % len(_GATEWAYS), path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if getattr(module, 'CHECKPOINT_MODE', None) != 'local-commit-only':
                return None
            if any(not callable(getattr(module, name, None)) for name in _GATEWAY_PROTOCOL):
                return None
            _GATEWAYS[key] = module
        return _GATEWAYS[key]
    except Exception:
        return None


def timeout_cleanup_context(argv):
    """Return a narrow owned-checkpoint timeout cleanup for the exact CLI call.

    The canonical gateway is resolved through the same trusted vault layout and
    committed policy checks used by checkpoint recovery. Other subprocesses and
    non-owner machines receive no cleanup callback.
    """
    if os.name != 'nt' or len(argv) < 6 or argv[1:3] != ['-X', 'utf8']:
        return (), None, None
    try:
        script = Path(argv[3]).resolve(strict=True)
        if argv[4] != 'record-handoff' or '--vault' not in argv[5:]:
            return (), None, None
        vault_index = list(argv).index('--vault', 5)
        vault = Path(argv[vault_index + 1]).resolve(strict=True)
        expected_script = vault.parent.joinpath(*_GATEWAY_RELATIVE).resolve(strict=True)
        if script != expected_script:
            return (), None, None
        gateway = _trusted_gateway(vault)
        if gateway is None:
            return (), None, None
        if any(not callable(getattr(gateway, name, None)) for name in (
                'archive_timed_out_checkpoint_lock', 'snapshot_owned_timeout_checkpoint_lock',
                'find_git_root', 'git_internal_path')):
            return (), None, None
        root = gateway.find_git_root(vault)
        if root is None:
            return (), None, None
        git_directory = root / '.git'
        if git_directory.is_dir():
            # The common checkout layout needs no Git subprocess to identify
            # these two files. Keep the callback budget for the owned child;
            # linked worktrees still use Git's authoritative path resolver.
            git_directory = git_directory.resolve()
            index_lock = git_directory / 'index.lock'
            lock_path = git_directory / gateway.CHECKPOINT_LOCK_NAME
        else:
            index_lock = gateway.git_internal_path(root, 'index.lock', timeout=1)
            lock_path = gateway.git_internal_path(
                root, gateway.CHECKPOINT_LOCK_NAME, timeout=1,
            )

        def prepare(identity):
            # The file-object identity and lock ID must be captured while the
            # exact spawned gateway is still alive, before cancellation starts.
            return gateway.snapshot_owned_timeout_checkpoint_lock(
                lock_path, index_lock, identity.pid, identity.started_at,
            )

        def cleanup(identity, expected, deadline):
            # archive_timed_out_checkpoint_lock repeats every ownership/policy/
            # index-lock check against the current canonical filesystem state.
            if not isinstance(expected, dict):
                return False
            return gateway.archive_timed_out_checkpoint_lock(
                vault, identity.pid, identity.started_at, expected,
                lock_path, index_lock, deadline,
            )

        return (index_lock,), prepare, cleanup
    except (OSError, IndexError, ValueError, TypeError):
        return (), None, None


def _recover_stale_intake_lock(lock_path: Path, vault: Path) -> bool:
    """Canonical guarded recovery of a permanently stale intake lock.

    Delegates to the trusted gateway's recover_stale_checkpoint_lock (same
    machine / age >= 900s / dead-PID / identity-checked rename guards). Owner
    machine, policy-enabled, local-commit-only and git-head policy are required
    first; any unreadable or invalid input refuses recovery.
    """
    if not lock_path.exists():
        return False
    gateway = _trusted_gateway(vault)
    if gateway is None:
        return False
    try:
        policy = gateway.checkpoint_policy(vault)
        if (not isinstance(policy, dict) or policy.get('enabled') is not True
                or policy.get('mode') != gateway.CHECKPOINT_MODE
                or policy.get('owner_machine') is not True
                or policy.get('policy_source') != 'git-head'):
            return False
        return bool(gateway.recover_stale_checkpoint_lock(lock_path, policy, requested=True))
    except Exception:
        return False


def _acquire_intake_lock(lock_path: Path, vault: Path = None) -> str:
    """knowledge.py acquire_checkpoint_lock protocol; never breaks another lock.

    A permanently stale lock is recovered first via the trusted gateway helpers;
    the bounded O_EXCL wait still never breaks a live lock.
    """
    if vault is not None:
        _recover_stale_intake_lock(lock_path, vault)
    lock_id = uuid.uuid4().hex
    try:
        fd = _create_exclusive(lock_path, 'checkpoint_lock_busy')
    except PublicationError:
        try:
            holder = json.loads(lock_path.read_text(encoding='utf-8')).get('agent') or 'intake'
        except (OSError, ValueError, AttributeError):
            holder = 'unreadable'
        raise PublicationError('checkpoint_lock_busy:' + str(holder)[:40])
    try:
        created = dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        payload = {'lock_id': lock_id, 'pid': os.getpid(), 'machine': _machine_name(),
                   'created_at': created, 'agent': 'openclaw-caretaker'}
        try:
            os.write(fd, (json.dumps(payload) + '\n').encode('utf-8'))
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        # The file exists but is not a valid lock: move it aside (it was created by
        # this call through O_EXCL, so it is not another writer's lock).
        try:
            os.replace(lock_path, lock_path.with_name(lock_path.name + '.failed-' + lock_id))
        except OSError:
            raise PublicationError('checkpoint_lock_acquire_failed_needs_operator')
        raise
    return lock_id


def _release_intake_lock(lock_path: Path, lock_id: str) -> bool:
    """Rename-release like knowledge.py: the owned bytes stay as an audit artifact."""
    try:
        if json.loads(lock_path.read_text(encoding='utf-8')).get('lock_id') != lock_id:
            return False
        os.replace(lock_path, lock_path.with_name(lock_path.name + '.release-' + uuid.uuid4().hex))
        return True
    except (OSError, ValueError, AttributeError):
        return False


def _generator(vault: Path):
    """curation.py of this Hub checkout: the program that regenerates log.md and index.md."""
    path = (vault.parent / 'skills' / 'llm-wiki' / 'scripts' / 'curation.py').resolve()
    if not path.is_file():
        raise PublicationError('checkpoint_generator_missing')
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _GENERATORS:
        spec = importlib.util.spec_from_file_location('_caretaker_curation_%d' % len(_GENERATORS), path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _GENERATORS[key] = module
    return _GENERATORS[key]


def _table_rows(data: bytes) -> set:
    """The generated table rows (title, blank line, header and separator excluded)."""
    return {line for line in data.decode('utf-8').splitlines()[4:] if line.startswith('| ')}


def _log_row_has_twin(gen, vault: Path, own_event_path: str, expected: set) -> bool:
    """A log row carries no event id: it proves this event only if no other event renders to it.

    Every other event is RENDERED and compared: the generator normalizes text (a CR and an
    LF both become a space), so raw field equality would miss a rendered twin.
    """
    own = (vault / own_event_path).resolve()
    for path in (vault / 'events').rglob('*.json'):
        if path.resolve() == own:
            continue
        try:
            other = json.loads(path.read_bytes())
            rendered = _table_rows(gen._merge_log({}, other)) if isinstance(other, dict) else set()
        except Exception:
            return True          # an event that cannot be rendered cannot be ruled out
        if rendered & expected:
            return True
    return False


def _shared_rows_already_in_head(git, vault: Path, rows: list, before: str) -> set:
    """Shared files whose generated rows for THIS publication are already in commit `before`.

    Runs under Git's index lock against the one commit the checkpoint builds on, so a
    concurrent reset cannot make the proof and the commit disagree.
    """
    changed = [r for r in rows if r['path'] in SHARED_FILES
               and hashlib.sha256(safe_file(vault, r['path']).read_bytes()).hexdigest() != r['after']]
    if not changed:
        return set()
    gen = _generator(vault)
    events = [r['path'] for r in rows if r['path'].startswith('events/')]
    pages = [r['path'] for r in rows if r['path'].startswith('wiki/') and r['path'] != 'wiki/index.md']
    done = set()
    for row in changed:
        rel = 'vault/' + row['path']
        if _tree_entry(git, before, rel) != (REGULAR, 'blob', git('hash-object', '--', rel)):
            continue                      # the working file differs from `before`: nothing is proven
        if row['path'] == 'log.md':
            if len(events) != 1:
                continue
            event = json.loads(safe_file(vault, events[0]).read_bytes())
            expected = _table_rows(gen._merge_log({}, event))
            if not expected or _log_row_has_twin(gen, vault, events[0], expected):
                continue
        else:
            if not pages:
                continue
            outputs = [{'relative': p, 'content': safe_file(vault, p).read_bytes()} for p in pages]
            expected = _table_rows(gen._merge_index({}, outputs))
        if expected and expected <= set(git('show', before + ':' + rel).splitlines()):
            done.add(row['path'])
    return done


#: Publication outputs are regular, non-executable files.
REGULAR = '100644'


def _index_entry(git, path: str):
    """The complete stage-0 entry (mode, blob) of `path` in the given index, or None."""
    for line in git('ls-files', '-s', '--', path).splitlines():
        meta, _, name = line.partition('\t')
        parts = meta.split()
        if name == path and len(parts) == 3 and parts[2] == '0':
            return parts[0], parts[1]
    return None


def _tree_entry(git, treeish: str, path: str):
    """The complete tree entry (mode, type, object) of `path` in `treeish`, or None."""
    for line in git('ls-tree', treeish, '--', path).splitlines():
        meta, _, name = line.partition('\t')
        parts = meta.split()
        if name == path and len(parts) == 3:
            return parts[0], parts[1], parts[2]
    return None


def _install_index(private: Path, index_lock: Path, index: Path) -> None:
    """Git's own way to commit an index - the new index becomes index.lock, which is then
    renamed over index - done by renames only, so index.lock never stops existing."""
    os.replace(private, index_lock)
    for attempt in range(20):        # a reader holding `index` open on Windows: retry briefly
        try:
            os.replace(index_lock, index)
            return
        except PermissionError:
            time.sleep(0.1 * (attempt + 1))
    os.replace(index_lock, index)


def _checkpoint_locked(git, git_dir: Path, vault: Path, rows: list, event_id: str,
                       receipt_dir: Path, exact_recovery: bool = False) -> dict:
    # Git reads booleans as true/yes/on/1; `--bool` normalizes them and fails on nonsense.
    split = git('config', '--bool', '--get', 'core.splitIndex', full=True)
    if split.returncode not in (0, 1) or split.stdout.decode('utf-8', 'replace').strip() != (
            'false' if split.returncode == 0 else ''):
        raise PublicationError('checkpoint_split_index_unsupported')
    index, index_lock = git_dir / 'index', git_dir / 'index.lock'
    # Git's own index lock first: from here no Git process can write the shared index,
    # and HEAD, the index snapshot and every proof below refer to one consistent state.
    # The lock is the file's existence (as in Git); the handle is closed at once so the
    # file can later be renamed on Windows.
    os.close(_create_exclusive(index_lock, 'git_index_busy'))
    installed = False
    try:
        snapshot = index.read_bytes() if index.exists() else None
        private = receipt_dir / ('checkpoint-index-' + uuid.uuid4().hex)
        if snapshot is not None:
            private.write_bytes(snapshot)

        def pgit(*args, check=True):
            return git(*args, check=check, index_file=private)
        before = git('rev-parse', '--verify', 'HEAD^{commit}')
        # Recovery has already proved every live row equals its journal hash.
        # A changed row must fail verification, never use the later-publication
        # shared-row proof (which can read a generator outside these inputs).
        in_head = set() if exact_recovery else _shared_rows_already_in_head(git, vault, rows, before)
        stage_rows = [r for r in rows if r['path'] not in in_head]

        def verify():
            for row in stage_rows:
                if hashlib.sha256(safe_file(vault, row['path']).read_bytes()).hexdigest() != row['after']:
                    raise PublicationError('publication_changed_before_checkpoint')
        verify()
        paths = ['vault/' + row['path'] for row in stage_rows]
        # The complete entry every publication path must have: a regular file holding
        # its verified bytes through the same attribute filters `git add` applies.
        want = {p: (REGULAR, git('hash-object', '--', p)) for p in paths}
        verify()
        # Ownership is the complete entry, never the path name: a staged entry is this
        # checkpoint's own leftover only as exactly (regular file, verified blob).
        if any(p not in want or _index_entry(pgit, p) != want[p]
               for p in pgit('diff', '--cached', '--name-only', before).splitlines()):
            raise PublicationError('foreign_staged_changes_keep_pending')
        must = {p for p, (mode, blob) in want.items() if _tree_entry(git, before, p) != (mode, 'blob', blob)}
        # An interrupted applying journal may be retried after the commit, but a
        # first roll-forward must commit every journal path in one exact commit.
        if exact_recovery and must and must != set(paths):
            raise PublicationError('recovery_head_contains_only_part_of_publication')
        pgit('add', '--', *paths)
        verify()
        tree = pgit('write-tree')
        changed = set(git('diff-tree', '-r', '--no-commit-id', '--name-only', '--no-renames',
                          before, tree).splitlines())
        if changed != must or any(_tree_entry(git, tree, p) != (mode, 'blob', blob)
                                  for p, (mode, blob) in want.items()):
            raise PublicationError('checkpoint_index_not_exactly_this_publication')
        if exact_recovery and changed not in (set(), set(paths)):
            raise PublicationError('recovery_commit_paths_not_exact')
        head = before
        if must:
            head = git('commit-tree', tree, '-p', before, '-m', 'Checkpoint reviewed OpenClaw curation ' + event_id)
        # Both guards run always, also for a retry with nothing left to commit, before the
        # private index may replace the shared one.
        if (index.read_bytes() if index.exists() else None) != snapshot:
            # Changed although Git's lock is held: a writer outside Git. Commit nothing.
            raise PublicationError('git_index_changed_outside_git')
        # Compare-and-swap, judged by update-ref's own result: the branch moves only if it
        # still points at `before`; with no new commit it is the same atomic check.
        try:
            git('update-ref', '-m', 'checkpoint ' + event_id, 'HEAD', head, before)
        except PublicationError:
            raise PublicationError('checkpoint_branch_moved_retry')
        try:
            _install_index(private, index_lock, index)
        except OSError:
            raise PublicationError('checkpoint_index_install_failed_needs_operator'
                                   if head != before else 'checkpoint_index_install_failed')
        installed = True
        if exact_recovery and (git('rev-parse', '--verify', 'HEAD^{commit}') != head
                               or any(_tree_entry(git, head, p) != (mode, 'blob', blob)
                                      for p, (mode, blob) in want.items())):
            raise PublicationError('recovery_head_not_exact')
    finally:
        if not installed and index_lock.exists():
            _set_aside(index_lock, 'caretaker-aborted')   # the shared index was never touched
    result = {'status': 'CHECKPOINTED', 'before': before, 'head': head, 'paths': sorted(must),
              'event_id': event_id, 'already_in_head': sorted(in_head)}
    (receipt_dir / 'checkpoint.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


def checkpoint_publication(publication: dict, vault: Path, receipt_dir: Path,
                           *, recovery_lock_id: str = None) -> dict:
    journal_path = Path(publication['transaction'])
    journal = json.loads(journal_path.read_text(encoding='utf-8'))
    recovering = isinstance(recovery_lock_id, str) and bool(recovery_lock_id)
    valid_status = (journal.get('status') == 'applying' and journal.get('lock_id') == recovery_lock_id
                    and isinstance(journal.get('files'), list)
                    and all(isinstance(row, dict) and row.get('state') == 'replaced'
                            for row in journal['files'])) if recovering else journal.get('status') == 'committed'
    if (publication.get('status') != 'PUBLISHED' or not valid_status
            or journal.get('event_id') != publication.get('event_id')
            or Path(journal['vault']).resolve() != vault.resolve()):
        raise PublicationError('publication_journal_not_committed')
    root = vault.parent
    hooks = receipt_dir / 'empty-hooks'; hooks.mkdir(exist_ok=True)
    if any(hooks.iterdir()):
        raise PublicationError('checkpoint_hooks_dir_not_empty')
    base_env = _git_environment()

    def git(*args, check=True, index_file=None, full=False):
        env = dict(base_env, GIT_INDEX_FILE=str(index_file)) if index_file else base_env
        p = subprocess.run(['git', '-c', 'core.autocrlf=false', '-c', 'core.quotePath=false',
                            '-c', 'core.hooksPath=' + str(hooks), '-C', str(root), *args],
                           capture_output=True, timeout=tick_budget.timeout(120, minimum=1), env=env, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        if full:
            return p
        if check and p.returncode:
            raise PublicationError('publication_git_checkpoint_failed')
        return p.stdout.decode('utf-8').strip()

    if git('symbolic-ref', '--short', 'HEAD') != 'main':
        raise PublicationError('checkpoint_requires_owner_main')
    rows = journal['files']
    if not rows or len(rows) > 32:
        raise PublicationError('publication_journal_bounds')
    for row in rows:
        name = row['path']
        if not (name.startswith(('wiki/', 'events/', 'governance/curations/')) or name == 'log.md'):
            raise PublicationError('publication_checkpoint_path_not_allowed')

    def git_path(name):
        path = Path(git('rev-parse', '--git-path', name))
        return path if path.is_absolute() else root / path
    lock_path = git_path(INTAKE_LOCK_NAME)
    git_dir = git_path('index').parent
    lock_id = _acquire_intake_lock(lock_path, vault)
    try:
        return _checkpoint_locked(git, git_dir, vault, rows, publication['event_id'], receipt_dir,
                                  exact_recovery=recovering)
    finally:
        if not _release_intake_lock(lock_path, lock_id):
            # Never silent: a leaked lock stops every intake checkpoint of every agent.
            raise PublicationError('checkpoint_lock_release_failed')
