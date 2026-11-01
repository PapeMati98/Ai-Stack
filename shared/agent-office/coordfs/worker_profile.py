"""Owner-local, closed native worker profiles. Nothing here comes from a dispatch."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TYPE_CHECKING

from .errors import CoordError

if TYPE_CHECKING:
    from .cli_host import HostEnrollment


_FIELDS = {"binding_id", "driver", "model", "permission", "reasoning", "auth"}
_MODELS = {"codex-cli": {"gpt-6-luna", "gpt-6-sol"},
           "claude-code-cli": {"claude-sonnet-5", "claude-opus-5-5"}}
_BINDING = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,511}\Z")
_MAX_BYTES = 16 * 1024


def _invalid(message: str, code: str = "invalid_worker_profiles") -> None:
    raise CoordError(message, code=code)


@dataclass(frozen=True)
class HostWorkerProfile:
    binding_id: str
    driver: str
    model: str
    permission: str
    reasoning: str
    auth: str

    def __post_init__(self) -> None:
        if not isinstance(self.binding_id, str) or _BINDING.fullmatch(self.binding_id) is None:
            _invalid("invalid profile binding")
        if self.driver not in _MODELS:
            _invalid("invalid profile driver")
        if (not isinstance(self.model, str) or _SAFE_ID.fullmatch(self.model) is None or
                self.model not in _MODELS[self.driver]):
            _invalid("model is not allowlisted")
        if self.permission not in ("read-only", "workspace-write"):
            _invalid("invalid profile permission")
        if self.reasoning not in ("low", "medium", "high"):
            _invalid("invalid profile reasoning")
        if self.auth != "subscription":
            _invalid("subscription auth required")

    @property
    def profile_fingerprint(self) -> str:
        row = {name: getattr(self, name) for name in _FIELDS}
        canonical = json.dumps(row, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


def _closed_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def load_worker_profiles(path: Path | str,
                         enrollments: Iterable[HostEnrollment]) -> dict[str, HostWorkerProfile]:
    """Read one bounded regular file; refuse the entire file on any invalid row."""
    # Import lazily so cli_host can use the profile type without an import cycle.
    from .cli_host import _checked_path

    path = _checked_path(Path(path), kind="file")
    before = path.lstat()
    if before.st_size > _MAX_BYTES:
        _invalid("worker profiles too large")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or
                    (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) !=
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)):
                raise ValueError("profile file changed")
            data = stream.read(_MAX_BYTES + 1)
            finished = os.fstat(stream.fileno())
            if (len(data) != before.st_size or len(data) > _MAX_BYTES or
                    (finished.st_dev, finished.st_ino, finished.st_size, finished.st_mtime_ns) !=
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)):
                raise ValueError("profile file changed")
        _checked_path(path, kind="file")
        after = path.lstat()
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != \
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise ValueError("profile file replaced")
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_closed_pairs)
        if (not isinstance(document, dict) or set(document) != {"schema_version", "profiles"}
                or type(document["schema_version"]) is not int or document["schema_version"] != 1
                or not isinstance(document["profiles"], list)
                or len(document["profiles"]) > 20):
            raise ValueError("invalid profile document")
        enrolled_rows = list(enrollments)
        enrolled = {item.binding_id: item for item in enrolled_rows}
        if len(enrolled) != len(enrolled_rows):
            raise ValueError("duplicate enrollment")
        profiles: dict[str, HostWorkerProfile] = {}
        for row in document["profiles"]:
            if not isinstance(row, dict) or set(row) != _FIELDS:
                raise ValueError("invalid profile row")
            profile = HostWorkerProfile(**row)
            owner = enrolled.get(profile.binding_id)
            if owner is None or owner.driver != profile.driver or profile.binding_id in profiles:
                raise ValueError("profile does not match enrollment")
            if profile.driver == "claude-code-cli":
                _invalid("Claude CLI permission is not an OS sandbox",
                         "permission_not_enforceable")
            profiles[profile.binding_id] = profile
        if set(profiles) != set(enrolled):
            raise ValueError("missing enrolled profile")
        return profiles
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise CoordError("invalid worker profiles", code="invalid_worker_profiles") from exc
