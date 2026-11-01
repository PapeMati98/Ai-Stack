"""Host-qualified checkout metadata; purely lexical and never an access grant.

The connection identity supplies ``host``. Public RPC payloads must not supply
a separate host override. The caller-host validator retains native filesystem
checks; this module describes paths that the Office server may not own.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from .errors import CoordError
from .identity import MAX_RESOURCE_CHARS, WINDOWS_RESERVED_NAMES, agent_identity

_HOST = re.compile(r'[a-z0-9][a-z0-9._-]{0,127}\Z')
_DRIVE_ABSOLUTE = re.compile(r'^[A-Za-z]:[\\/]')
_WINDOWS_BAD = re.compile(r'[<>:"|?*]')


def canonical_host(host: Any) -> str:
    """Require a canonical host from identity, without normalizing a forged claim."""
    if not isinstance(host, str) or _HOST.fullmatch(host) is None:
        raise CoordError('checkout host must be a canonical identity component', code='invalid_checkout_host')
    return host


def local_host() -> str:
    """The same canonical host component used by native Office identities."""
    if os.name == 'nt':
        import ctypes

        size = ctypes.c_uint32(256)
        name = ctypes.create_unicode_buffer(size.value)
        if not ctypes.windll.kernel32.GetComputerNameW(name, ctypes.byref(size)):
            raise OSError('GetComputerNameW failed')
        native_name = name.value
    else:
        native_name = os.uname().nodename
    if not native_name:
        raise OSError('native computer name is empty')
    return agent_identity('office', host=native_name, session_id='server', env={})['host']


def _refuse() -> None:
    raise CoordError('checkout must be an unambiguous absolute lexical path', code='invalid_checkout')


def checkout_claim(raw: Any, *, host: str, path_style: str | None = None) -> dict[str, str]:
    """Canonical host/path/style triple without resolve, stat, expansion or I/O.

    Reject traversal *before* normalization. Windows devices, ADS, trailing
    spaces/dots and extended namespaces are refused rather than interpreted.
    An explicit style handles POSIX's double-slash/UNC spelling ambiguity.
    """
    bound_host = canonical_host(host)
    if not isinstance(raw, str) or not raw or len(raw) > MAX_RESOURCE_CHARS:
        _refuse()
    text = unicodedata.normalize('NFC', raw)
    if text != text.strip() or not text.isprintable():
        _refuse()
    style = path_style
    if style is None:
        if _DRIVE_ABSOLUTE.match(text) or text.startswith(('\\\\', '//')):
            style = 'windows'
        elif text.startswith('/'):
            style = 'posix'
        else:
            _refuse()
    if style == 'windows':
        normalized = text.replace('/', '\\')
        if normalized.startswith(('\\\\?\\', '\\\\.\\')):
            _refuse()
        drive, tail = ntpath.splitdrive(normalized)
        if _DRIVE_ABSOLUTE.match(normalized):
            components = tail.split('\\')
        elif normalized.startswith('\\\\'):
            anchor = drive[2:].split('\\')
            if (len(anchor) != 2 or not all(anchor) or any(part in {'.', '..'} for part in anchor)
                    or (tail and not tail.startswith('\\'))):
                _refuse()
            components = anchor + tail.split('\\')
        else:
            _refuse()
        for part in components:
            if not part or part == '.':
                continue
            if part == '..' or part.endswith((' ', '.')) or _WINDOWS_BAD.search(part):
                _refuse()
            if part.split('.', 1)[0].lower() in WINDOWS_RESERVED_NAMES:
                _refuse()
        path = ntpath.normcase(ntpath.normpath(normalized))
    elif style == 'posix':
        if not text.startswith('/') or '\\' in text or '..' in text.split('/'):
            _refuse()
        # A POSIX // path is represented with one slash, never recast as UNC.
        path = '/' + posixpath.normpath(text).lstrip('/')
    else:
        _refuse()
    return {'host': bound_host, 'path_style': style, 'path': path}


def validate_claim(value: Any) -> dict[str, str]:
    """Validate persisted data without silently fixing its binding or ownership."""
    if not isinstance(value, Mapping) or set(value) != {'host', 'path_style', 'path'}:
        _refuse()
    canonical = checkout_claim(value['path'], host=value['host'], path_style=value['path_style'])
    if dict(value) != canonical:
        _refuse()
    return canonical


def claim_key(value: Any) -> tuple[str, str, str]:
    """A path spelling on two hosts identifies two different checkout claims."""
    claim = validate_claim(value)
    return claim['host'], claim['path_style'], claim['path']
