"""Stdlib-only read paths, preserving Fleet's existing filesystem boundaries."""
import os,re,stat
from pathlib import Path
from typing import Any
PROTECTED={'.git','.zcode','.vibe','.agents','.codex','.claude','.ssh','.aws','.azure','.gnupg','.docker','.kube','.netrc','_netrc','.npmrc','.pypirc','.pgpass','auth.json','token','token.json','tokens.json','access_token','refresh_token','oauth_creds.json','id_rsa','id_dsa','id_ecdsa','id_ed25519','.git-credentials','login data','web data','local state'}
WORDS=('secret','credential','password','passwd','cookie')
SUFFIXES=('.pem','.key','.pfx','.p12','.keystore','.jks','.kdbx','.ppk','.env')
DEVICES={'CON','PRN','AUX','NUL','CONIN$','CONOUT$',*(f'COM{i}' for i in '0123456789¹²³'),*(f'LPT{i}' for i in '0123456789¹²³')}
def _protected(name: str) -> bool:
    name=name.casefold()
    return name in PROTECTED or any(w in name for w in WORDS) or name.endswith(SUFFIXES)
def normalize_relative(value: Any) -> str:
    if type(value) is not str or not 1<=len(value)<=512 or any(ord(c)<32 or c in '<>"|?*:' for c in value): raise ValueError('Relative path refused')
    text=value.replace('\\','/')
    if text.startswith('/') or text.endswith('/'): raise ValueError('Exact regular path required')
    for part in text.split('/'):
        if part in {'','.','..'} or part!=part.strip(' .') or part.split('.')[0].rstrip().upper() in DEVICES or re.search(r'~\d',part) or _protected(part): raise ValueError('Protected or aliased path')
    return text
def _is_link(path: Path,info: os.stat_result) -> bool:
    junction=getattr(os.path,'isjunction',None)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0)&0x400) or bool(junction and junction(path))
def _check_directory_chain(path: Path) -> None:
    for item in (path,*path.parents):
        if _is_link(item,os.lstat(item)): raise ValueError('Linked boundary')
def validate_workspace(value: str,owner_home: str | None=None) -> Path:
    if type(value) is not str or '\0' in value or value.startswith(('\\\\','//')): raise ValueError('Workspace path')
    path=Path(value)
    if not path.is_absolute() or not path.is_dir() or os.path.normcase(str(path.resolve()))!=os.path.normcase(os.path.abspath(path)): raise ValueError('Workspace boundary')
    if owner_home is not None and (type(owner_home) is not str or not owner_home or '\0' in owner_home or owner_home.startswith(('\\\\','//'))): raise ValueError('Owner-home metadata')
    home=Path(owner_home) if owner_home else Path.home()
    if not home.is_absolute() or not home.is_dir(): raise ValueError('Owner-home metadata')
    _check_directory_chain(home); home=home.resolve();base=path.resolve()
    if base.parent==base or home==base or home.is_relative_to(base): raise ValueError('Broad workspace')
    default=False
    try:
        relative=base.relative_to(home/'.agents/agent-fleet/evidence')
        default=len(relative.parts)>=2 and bool(re.fullmatch(r'run-\d{8}T\d{6}Z-[a-f0-9]{10}',relative.parts[0])) and relative.parts[1]=='worktree'
    except ValueError: pass
    for item in (base,*base.parents):
        if _protected(item.name) and not(default and item==home/'.agents'): raise ValueError('Protected workspace')
    _check_directory_chain(base);return base
def _regular_file(path: Path,limit: int) -> os.stat_result:
    info=os.lstat(path)
    if _is_link(path,info) or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>limit: raise ValueError('Not bounded regular file')
    return info
def _read_bytes(path: Path,info: os.stat_result,limit: int) -> bytes:
    with path.open('rb') as stream:
        opened=os.fstat(stream.fileno())
        if (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino) or opened.st_nlink!=1: raise ValueError('Open drift')
        data=stream.read(limit+1)
    if len(data)>limit: raise ValueError('Read bound')
    return data
