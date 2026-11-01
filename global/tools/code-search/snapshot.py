"""Tracked, bounded text snapshots. Repository contents are untrusted data."""
from __future__ import annotations

import ctypes
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from dataclasses import dataclass
import uuid


class BoundaryError(ValueError):
    """A closed boundary; messages must never contain source bytes."""


EXTENSIONS = frozenset({
    '.py', '.pyi', '.rs', '.go', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs',
    '.c', '.h', '.cpp', '.hpp', '.cc', '.cs', '.java', '.kt', '.kts', '.scala',
    '.rb', '.php', '.swift', '.m', '.mm', '.sh', '.bash', '.ps1', '.psm1',
    '.lua', '.r', '.jl', '.ex', '.exs', '.erl', '.hrl', '.hs', '.fs', '.fsx',
    '.vue', '.svelte', '.html', '.css', '.scss', '.sass', '.less', '.md', '.mdx',
})
FORBIDDEN_DIRS = frozenset({
    '.git', '.hg', '.svn', '.ssh', '.aws', '.azure', '.gnupg', '.codex',
    'secrets', 'secret', 'credentials', 'private', 'restricted', 'logs', 'log',
    'mail', 'mails', 'email', 'emails', 'uploads', 'upload', 'database',
    'databases', 'db', 'data', 'storage', 'cache', '.cache', '__pycache__',
    'node_modules', 'vendor', 'venv', '.venv', 'env', 'dist', 'build', 'target',
    'models', 'backups', 'backup', 'cookies', 'auth',
})
SECRET_NAME = re.compile(
    r'(^|[._-])(secrets?|credentials?|cookies?|tokens?|passwords?|id_rsa|id_ed25519)'
    r'([._-]|$)|^\.env(?:[._-]|$)', re.I)
AUTH_NAME = re.compile(r'^auth(?:[._-]|$)', re.I)
AUTH_CODE_EXTENSIONS = EXTENSIONS - {'.md', '.mdx'}
SECRET_CONTENT = re.compile(
    r'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|'
    r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|'
    r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|'
    r'sk-(?:proj-)?[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{20,})\b|'
    r'(?im:^\s*(?:export\s+)?[\w.-]*(?:api[_-]?key|access[_-]?token|'
    r'client[_-]?secret|password|passwd|private[_-]?key)[\w.-]*\s*[:=]'
    r'\s*[\"\']?[A-Za-z0-9_+/=-]{12,})')


def path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def absolute_path(value: str | Path) -> Path:
    text = str(value)
    if not text or '\x00' in text or text.startswith(('\\\\', '//')):
        raise BoundaryError('unsafe_absolute_path')
    path = Path(text)
    if not path.is_absolute() or '..' in path.parts:
        raise BoundaryError('unsafe_absolute_path')
    # Reject alternate data streams and Win32 device names on every platform.
    for part in path.parts[1:]:
        if ':' in part or part.endswith((' ', '.')) or re.match(
                r'(?i)^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part):
            raise BoundaryError('unsafe_absolute_path')
    return path


def _ordinary(info: os.stat_result, directory: bool = False) -> None:
    if stat.S_ISLNK(info.st_mode) or (getattr(info, 'st_file_attributes', 0) or 0) & 0x400:
        raise BoundaryError('reparse_or_symlink')
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise BoundaryError('not_directory')
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BoundaryError('nonregular_or_hardlink')


def check_path(path: Path, directory: bool = False) -> os.stat_result:
    path = absolute_path(path)
    for parent in reversed(path.parents):
        _ordinary(parent.lstat(), directory=True)
    info = path.lstat()
    _ordinary(info, directory=directory)
    return info


def make_directory(path: Path) -> None:
    path = absolute_path(path)
    for node in [*reversed(path.parents), path]:
        try:
            info = node.lstat()
        except FileNotFoundError:
            try:
                node.mkdir(mode=0o755 if os.name == 'nt' else 0o700)
            except FileExistsError:
                pass
            info = node.lstat()
        _ordinary(info, directory=True)


def _identity(info: os.stat_result) -> tuple:
    # Python 3.13 Windows lstat uses creation time for deprecated st_ctime;
    # fstat can expose last-change time. Compare the explicit birth timestamp.
    stamp = getattr(info, 'st_birthtime_ns', None) if os.name == 'nt' else info.st_ctime_ns
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, stamp)


def _windows_native_handle_path(handle) -> Path:
    from ctypes import wintypes
    fn = ctypes.WinDLL('kernel32', use_last_error=True).GetFinalPathNameByHandleW
    fn.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    fn.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = fn(handle, buffer, len(buffer), 0)
    if not 0 < length < len(buffer):
        raise BoundaryError('handle_path_failed')
    name = buffer.value
    if name.startswith('\\\\?\\') and not name.startswith('\\\\?\\UNC\\'):
        name = name[4:]
    return absolute_path(name)


def _windows_handle_path(fd: int) -> Path:
    import msvcrt
    return _windows_native_handle_path(msvcrt.get_osfhandle(fd))


def open_checked(path: Path):
    """Open without following links; check the held identity before reading."""
    before = check_path(path)
    if os.name == 'nt':
        import msvcrt
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                           w.DWORD, w.DWORD, w.HANDLE]
        create.restype = w.HANDLE
        # No write/delete sharing while the handle is held. OPEN_REPARSE_POINT
        # prevents final-component traversal; final-path verification below
        # detects redirected ancestors before any content read.
        handle = create(str(path), 0x80000000, 1, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle.argtypes = [w.HANDLE]
            kernel.CloseHandle(handle)
            raise
    else:
        parent_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    try:
        held = os.fstat(fd)
        _ordinary(held)
        if _identity(before) != _identity(held):
            raise BoundaryError('file_changed_before_read')
        if os.name == 'nt' and path_key(_windows_handle_path(fd)) != path_key(path):
            raise BoundaryError('redirected_handle')
        check_path(path)
        return os.fdopen(fd, 'rb'), held
    except BaseException:
        os.close(fd)
        raise


def read_checked(path: Path, limit: int) -> bytes:
    stream, held = open_checked(path)
    with stream:
        if held.st_size > limit:
            raise BoundaryError('file_size_limit')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if len(data) > limit or _identity(held) != _identity(after):
            raise BoundaryError('file_changed_during_read')
        if _identity(held) != _identity(check_path(path)):
            raise BoundaryError('path_changed_during_read')
    return data


def hash_checked(path: Path, expected_size: int) -> str:
    stream, held = open_checked(path)
    with stream:
        if held.st_size != expected_size:
            raise BoundaryError('asset_size_mismatch')
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if _identity(held) != _identity(os.fstat(stream.fileno())) or (
                _identity(held) != _identity(check_path(path))):
            raise BoundaryError('asset_changed_during_read')
        return digest


class StateLease:
    """Nonblocking process lock over owned state; release handles, never unlink."""
    def __init__(self, state: Path):
        self.fd = None
        path = state / 'search.lock'
        make_directory(state)
        if path.exists():
            check_path(path)
        if os.name == 'nt':
            import msvcrt
            from ctypes import wintypes as w
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                               w.DWORD, w.DWORD, w.HANDLE]
            create.restype = w.HANDLE
            handle = create(str(path), 0xC0000000, 3, None, 4, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise BoundaryError('state_lock_unavailable')
            self.fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        else:
            self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self.fd)
            _ordinary(info)
            if info.st_size > 1:
                raise BoundaryError('invalid_state_lock')
            check_path(path)
            if os.name == 'nt' and path_key(_windows_handle_path(self.fd)) != path_key(path):
                raise BoundaryError('redirected_state_lock')
            if os.name == 'nt':
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BoundaryError) as exc:
            self.close()
            if isinstance(exc, BoundaryError):
                raise
            raise BoundaryError('state_busy') from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def admissible_name(name: str) -> bool:
    if '\\' in name or ':' in name or '\x00' in name:
        return False
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        return False
    auth_code = p.suffix.casefold() in AUTH_CODE_EXTENSIONS
    if any(part.startswith('.') or SECRET_NAME.search(part) or
           (part.casefold() in FORBIDDEN_DIRS and
            not (part.casefold() == 'auth' and auth_code)) or
           (AUTH_NAME.search(part) and not auth_code) for part in p.parts):
        return False
    return p.suffix.casefold() in EXTENSIONS


def validate_exclude_prefixes(value) -> tuple[str, ...]:
    """Owner-only narrowing of tracked directory selection; no path resolution."""
    if not isinstance(value, list) or len(value) > 128:
        raise BoundaryError('invalid_exclude_prefix')
    prefixes = set()
    for prefix in value:
        if (not isinstance(prefix, str) or not prefix.endswith('/') or
                len(prefix.encode('utf-8')) > 2048 or
                any(char in prefix for char in '\\:\x00<>"|?*')):
            raise BoundaryError('invalid_exclude_prefix')
        parts = prefix[:-1].split('/')
        if any(part in ('', '.', '..') or part.endswith((' ', '.')) or re.match(
                r'(?i)^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part)
               for part in parts):
            raise BoundaryError('invalid_exclude_prefix')
        prefixes.add(prefix.casefold() if os.name == 'nt' else prefix)
    return tuple(sorted(prefixes))


def source_text(data: bytes) -> str:
    if b'\x00' in data:
        raise BoundaryError('binary_source')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise BoundaryError('non_utf8_source') from None
    if any(ord(char) < 32 and char not in '\t\n\r\f' for char in text):
        raise BoundaryError('binary_source')
    if SECRET_CONTENT.search(text):
        raise BoundaryError('secret_bearing_source')
    return text


def git_view(repo: Path, state: Path, max_metadata_bytes: int,
             state_limit: int | None = None) -> tuple[Path, str]:
    """Copy only the index into private Git metadata, without local config reads.

    Git's local include.path can otherwise read outside the repository even with
    system/global config disabled. Worktree pointers are resolved only for these
    fixed metadata names; no repository config, hooks, attributes or objects run.
    """
    dotgit = repo / '.git'
    info = dotgit.lstat()
    if stat.S_ISDIR(info.st_mode):
        check_path(dotgit, directory=True)
        gitdir = dotgit
    else:
        pointer = read_checked(dotgit, 4096).decode('utf-8').strip()
        if not pointer.startswith('gitdir: '):
            raise BoundaryError('repo_not_exact_git_root')
        value = pointer[8:]
        gitdir = Path(value) if Path(value).is_absolute() else repo / value
        gitdir = absolute_path(os.path.normpath(str(gitdir)))
        check_path(gitdir, directory=True)
    common = gitdir
    common_file = gitdir / 'commondir'
    if common_file.exists():
        value = read_checked(common_file, 4096).decode('utf-8').strip()
        common = Path(value) if Path(value).is_absolute() else gitdir / value
        common = absolute_path(os.path.normpath(str(common)))
        check_path(common, directory=True)
    head = read_checked(gitdir / 'HEAD', 256).decode('ascii').strip()
    if head.startswith('ref: '):
        ref = head[5:]
        if not re.fullmatch(r'refs/heads/[A-Za-z0-9_/.-]+', ref) or any(
                part in ('.', '..', '') or part.startswith('.') for part in ref.split('/')):
            raise BoundaryError('unsupported_git_ref')
        ref_file = common.joinpath(*ref.split('/'))
        if ref_file.exists():
            head = read_checked(ref_file, 256).decode('ascii').strip()
        else:
            head = 'UNBORN'
            packed = common / 'packed-refs'
            if packed.exists():
                for line in read_checked(packed, max_metadata_bytes).decode('ascii').splitlines():
                    fields = line.split(' ')
                    if len(fields) == 2 and fields[1] == ref:
                        head = fields[0]
                        break
    if head != 'UNBORN' and not re.fullmatch('[0-9a-f]{40,64}', head):
        raise BoundaryError('invalid_git_head')
    # One private view per exact checkout. Refresh its index under StateLease;
    # repeated searches and service restarts must not accumulate full copies.
    checkout = path_key(repo).encode('utf-8')
    # Git for Windows reserves room for internal paths under GIT_DIR. Keep this
    # component short, and verify the full checkout identity to reject collisions.
    view = state / 'git-views' / hashlib.sha256(checkout).hexdigest()[:16]
    make_directory(view / 'objects')
    make_directory(view / 'refs')
    metadata = [('checkout', checkout), ('HEAD', b'ref: refs/heads/snapshot\n'),
                ('config', b'[core]\nrepositoryformatversion = 0\nbare = false\n')]
    index = gitdir / 'index'
    if index.exists():
        data = read_checked(index, max_metadata_bytes)
    else:
        # A valid empty v2 index replaces stale entries without deleting a file.
        header = b'DIRC\x00\x00\x00\x02\x00\x00\x00\x00'
        data = header + hashlib.sha1(header).digest()
    if state_limit is not None:
        growth = sum(max(0, len(content) - (check_path(view / name).st_size if (
            view / name).exists() else 0)) for name, content in [*metadata, ('index', data)])
        state_usage(state, state_limit, additional=growth)
    for name, content in metadata:
        path = view / name
        if path.exists():
            if read_checked(path, len(content)) != content:
                raise BoundaryError('git_view_metadata_mismatch')
        else:
            with path.open('xb') as target:
                target.write(content)
    copied = view / 'index'
    if not copied.exists() or read_checked(copied, max_metadata_bytes) != data:
        if copied.exists():
            check_path(copied)
        with copied.open('wb') as target:
            target.write(data)
    return view, head


def state_usage(state: Path, limit: int, additional: int = 0) -> dict:
    """Measure only owned derived state, never source or model bytes. No cleanup."""
    check_path(state, directory=True)
    total, count = 0, 0
    for directory, dirs, files in os.walk(state, followlinks=False):
        check_path(Path(directory), directory=True)
        for name in dirs:
            check_path(Path(directory) / name, directory=True)
        for name in files:
            info = (Path(directory) / name).lstat()
            _ordinary(info)
            total += info.st_size
            count += 1
            if total + additional > limit:
                raise BoundaryError('state_size_limit')
            if count > 1000000:
                raise BoundaryError('state_file_count_limit')
    if total + additional > limit:
        raise BoundaryError('state_size_limit')
    return {'bytes': total, 'files': count, 'limit_bytes': limit}


def reusable_container(parent: Path, key: str, identity: str, maximum: int = 128) -> tuple[Path, bool]:
    """A last-written marker permits reuse; interrupted copies remain untouched."""
    make_directory(parent)
    candidates = sorted((p for p in parent.iterdir() if p.name == key or
                         re.fullmatch(re.escape(key) + r'-retry-[0-9a-f]{16}', p.name)),
                        key=lambda p: (p.name != key, p.name))
    for path in candidates:
        check_path(path, directory=True)
        marker = path / 'complete'
        try:
            data = read_checked(marker, 128)
        except FileNotFoundError:
            continue
        if data != identity.encode('ascii'):
            raise BoundaryError('completion_marker_mismatch')
        return path, False
    if len(candidates) >= maximum:
        raise BoundaryError('incomplete_container_limit')
    path = parent / (key if not candidates else key + '-retry-' + uuid.uuid4().hex[:16])
    path.mkdir(mode=0o755 if os.name == 'nt' else 0o700)
    check_path(path, directory=True)
    return path, True


def complete_container(container: Path, identity: str) -> None:
    with (container / 'complete').open('xb') as stream:
        stream.write(identity.encode('ascii'))
        stream.flush()
        os.fsync(stream.fileno())


@dataclass(frozen=True)
class Snapshot:
    root: Path
    repo: Path
    fingerprint: str
    head: str
    files: dict
    omissions: dict

    @contextmanager
    def frozen(self):
        """Keep no-write/no-delete source handles throughout a native read."""
        self.verify()
        with ExitStack() as stack:
            if os.name == 'nt':
                from ctypes import wintypes as w
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                create = kernel.CreateFileW
                create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                   w.DWORD, w.DWORD, w.HANDLE]
                create.restype = w.HANDLE
                kernel.CloseHandle.argtypes = [w.HANDLE]
                directories = {self.root}
                for name in self.files:
                    directories.update(p for p in (self.root / name).parents
                                       if p == self.root or p.is_relative_to(self.root))
                paths = [(p, True) for p in sorted(directories)]
                paths += [(self.root / name, False) for name in self.files]
                # Hold native HANDLEs directly. Converting all of them through
                # msvcrt.open_osfhandle would exceed the CRT's 8192-fd ceiling.
                # Content/identity verification below uses one transient fd.
                for path, directory in paths:
                    check_path(path, directory=directory)
                    handle = create(str(path), 0x80000000, 1, None, 3, 0x02200000, None)
                    if handle == ctypes.c_void_p(-1).value:
                        raise BoundaryError('snapshot_handle_lock_failed')
                    stack.callback(kernel.CloseHandle, handle)
                    if path_key(_windows_native_handle_path(handle)) != path_key(path):
                        raise BoundaryError('redirected_snapshot_handle')
            else:
                for name in self.files:
                    stream, _ = open_checked(self.root / name)
                    stack.enter_context(stream)
            self.verify()
            try:
                yield self
            finally:
                self.verify()

    def verify(self) -> None:
        check_path(self.root, directory=True)
        observed = set()
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            for name in dirs:
                check_path(Path(directory) / name, directory=True)
            for name in files:
                path = Path(directory) / name
                rel = path.relative_to(self.root).as_posix()
                if rel not in self.files:
                    raise BoundaryError('unexpected_snapshot_file')
                expected = self.files[rel]
                if hash_checked(path, expected['size']) != expected['sha256']:
                    raise BoundaryError('snapshot_content_mismatch')
                observed.add(rel)
        if observed != set(self.files):
            raise BoundaryError('snapshot_file_missing')


def build_snapshot(repo: Path, state: Path, model_identity: str, limits: dict,
                   git_run) -> Snapshot:
    """git_run is a fixed Git runner, never repository-supplied executable code."""
    check_path(repo, directory=True)
    git_limit = max(1048576, limits['max_files'] * 4096)
    top = git_run(['rev-parse', '--show-toplevel'], git_limit).decode('utf-8').strip()
    if path_key(absolute_path(top)) != path_key(repo):
        raise BoundaryError('repo_not_exact_git_root')
    head_bytes = git_run(['rev-parse', '--verify', 'HEAD'], 256, allow_unborn=True)
    head = head_bytes.decode('ascii').strip() if head_bytes else 'UNBORN'
    if head != 'UNBORN' and not re.fullmatch('[0-9a-f]{40,64}', head):
        raise BoundaryError('invalid_git_head')
    paths = git_run(['ls-files', '-z', '--cached'], git_limit).split(b'\x00')
    excluded = validate_exclude_prefixes(limits.get('exclude_prefixes', []))
    admitted, omissions, total = {}, {}, 0
    for raw in sorted(set(paths)):
        if not raw:
            continue
        reason = None
        try:
            name = raw.decode('utf-8')
            if not admissible_name(name):
                reason = 'forbidden_or_unsupported_name'
            elif (name.casefold() if os.name == 'nt' else name).startswith(excluded):
                reason = 'owner_excluded_prefix'
            elif len(admitted) >= limits['max_files']:
                reason = 'file_count_limit'
            else:
                path = repo.joinpath(*PurePosixPath(name).parts)
                info = check_path(path)
                if total + info.st_size > limits['max_total_bytes']:
                    reason = 'total_size_limit'
                else:
                    data = read_checked(path, limits['max_file_bytes'])
                    text = source_text(data)
                    if total + len(data) > limits['max_total_bytes']:
                        raise BoundaryError('total_size_limit')
                    total += len(data)
                    admitted[name] = {'sha256': hashlib.sha256(data).hexdigest(),
                                      'size': len(data), 'text': text}
        except (OSError, UnicodeError, BoundaryError) as exc:
            reason = str(exc) if isinstance(exc, BoundaryError) else 'unsafe_or_missing_file'
        if reason:
            omissions[reason] = omissions.get(reason, 0) + 1
    manifest = {'version': 1, 'repo': path_key(repo), 'model_identity': model_identity,
                'files': {name: {k: v for k, v in item.items() if k != 'text'}
                          for name, item in admitted.items()}}
    encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode('utf-8')
    fingerprint = hashlib.sha256(encoded).hexdigest()
    container, fresh = reusable_container(state / 'snapshots', fingerprint, fingerprint)
    root = container / 'source'
    if fresh:
        state_usage(state, limits['max_state_bytes'], additional=total + len(encoded) + 64)
        make_directory(root)
        for name, item in admitted.items():
            target = root.joinpath(*PurePosixPath(name).parts)
            make_directory(target.parent)
            with target.open('xb') as stream:
                stream.write(item['text'].encode('utf-8'))
            target.chmod(stat.S_IREAD if os.name == 'nt' else 0o400)
        with (container / 'snapshot.json').open('xb') as stream:
            stream.write(encoded)
        if os.name != 'nt':
            for directory, _, _ in os.walk(root, topdown=False):
                Path(directory).chmod(0o500)
    elif read_checked(container / 'snapshot.json', len(encoded) + 1) != encoded:
        raise BoundaryError('snapshot_manifest_mismatch')
    snapshot = Snapshot(root, repo, fingerprint, head, admitted, omissions)
    snapshot.verify()
    if fresh:
        complete_container(container, fingerprint)
    return snapshot
