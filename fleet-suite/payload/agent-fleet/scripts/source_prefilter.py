"""Exact-byte, parser-backed exceptions for nonliteral TS source references.

Generic prompts/config/output keep their original scanner. Missing parser, malformed
source, unknown language or unbound diff context never create an exception.
"""
from __future__ import annotations

import functools
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
SOURCE_EXTENSIONS = {'.ts', '.tsx', '.mts', '.cts'}
FILE_LIMIT = 2 * 1024 * 1024


@functools.lru_cache(maxsize=32)
def reference_ranges(data: bytes, extension: str) -> tuple[tuple[int, int], ...]:
    if extension not in SOURCE_EXTENSIONS or len(data) > FILE_LIMIT:
        return ()
    try:
        text = data.decode('utf-8', errors='strict')
        if '\x00' in text:
            return ()
        node = shutil.which('node')
        if not node:
            return ()
        env = {key: value for key, value in os.environ.items()
               if key.upper() not in {'NODE_OPTIONS', 'NODE_PATH'}}
        result = subprocess.run([node, str(HERE / 'source_references.cjs')],
            input=json.dumps({'text': text, 'extension': extension}).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=HERE, env=env,
            timeout=10, check=False, creationflags=0x08000000 if os.name == 'nt' else 0)
        if result.returncode or len(result.stdout) > 256 * 1024:
            return ()
        value = json.loads(result.stdout)
        if value.get('version') != '5.9.3' or value.get('valid') is not True:
            return ()
        return tuple((len(data[:start].decode('utf-8')), len(data[:end].decode('utf-8')))
                     for start, end in value['ranges'] if 0 <= start < end <= len(data))
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, subprocess.TimeoutExpired):
        return ()


def mask_references(data: bytes, path: str, patterns) -> str:
    text = data.decode('utf-8', errors='replace')
    assigned = next(pattern for label, pattern in patterns if label == 'assigned-secret')
    matches = list(assigned.finditer(text))
    if not matches:
        return text
    ranges = dict(reference_ranges(data, Path(path).suffix.casefold()))
    pieces, cursor = [], 0
    for match in matches:
        end = ranges.get(match.start())
        # The old broad regex can consume trailing statement punctuation, but
        # never allow a literal, fallback, operator, second assignment or comment.
        if end is None or match.end() < end or not re.fullmatch(r'[;,)}\]]*', text[end:match.end()]):
            continue
        pieces.extend((text[cursor:match.start()], re.sub(r'[^\r\n]', ' ', match.group())))
        cursor = match.end()
    pieces.append(text[cursor:])
    return ''.join(pieces)


def scan(data: bytes, path: str, patterns) -> list[str]:
    original = data.decode('utf-8', errors='replace')
    masked = mask_references(data, path, patterns)
    return [label for label, pattern in patterns
            if pattern.search(masked if label == 'assigned-secret' else original)]


class DiffSources:
    """Internal capture context, populated from full Git/worktree source bytes."""
    def __init__(self, loader, patterns):
        self.loader = loader
        self.patterns = patterns
        self.cache = {}

    def line(self, side: str, path: str, number: int, payload: str) -> str:
        key = (side, path)
        if key not in self.cache:
            data = self.loader(side, path)
            if data is None or len(data) > FILE_LIMIT:
                self.cache[key] = None
            else:
                try:
                    original = data.decode('utf-8', errors='strict').split('\n')
                    masked = mask_references(data, path, self.patterns).split('\n')
                    self.cache[key] = (original, masked)
                except UnicodeError:
                    self.cache[key] = None
        pair = self.cache[key]
        if pair is None or number < 1 or number > len(pair[0]):
            return payload
        actual = pair[0][number - 1].removesuffix('\r')
        if actual != payload:
            return payload
        return pair[1][number - 1].removesuffix('\r')


def git_sources(worktree: Path, old_revision: str, new_revision: str | None, patterns):
    def load(side, path):
        if path == '/dev/null' or Path(path).suffix.casefold() not in SOURCE_EXTENSIONS:
            return None
        revision = old_revision if side == 'old' else new_revision
        if revision is None:
            source = worktree / path
            for part in [source, *source.parents]:
                if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
                    return None
                if part == worktree:
                    break
            if not source.is_file() or source.stat().st_size > FILE_LIMIT:
                return None
            with source.open('rb') as handle:
                return handle.read(FILE_LIMIT + 1)
        # Git objects are immutable. No checkout, hooks, textconv or source execution.
        size = subprocess.run(['git', 'cat-file', '-s', f'{revision}:{path}'], cwd=worktree,
            capture_output=True, timeout=10, check=False)
        if size.returncode or not size.stdout.strip().isdigit() or int(size.stdout) > FILE_LIMIT:
            return None
        result = subprocess.run(['git', 'cat-file', 'blob', f'{revision}:{path}'], cwd=worktree,
            capture_output=True, timeout=10, check=False)
        return result.stdout if result.returncode == 0 else None
    return DiffSources(load, patterns)
