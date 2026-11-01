"""Lease claim/release/renew/ls/who/sweep with atomic create and TTL reclaim."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .atomic import exclusive_create, exclusive_mutex, replace_with_backoff, unlink_with_backoff
from .constants import DEFAULT_TTL_S, LEASE_FIELDS, LINEAGE_META_KEYS, MAX_TTL_S, MIN_TTL_S
from .errors import CoordError
from .events import append_event
from .hashing import resource_hash
from .identity import canonicalize_resource, detect_kind
from .store import (
    ensure_home,
    events_max_bytes,
    events_retain,
    lease_mutex_path,
    leases_dir,
    lock_path,
)


NowFn = Callable[[], float]


def format_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> float:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


def validate_agent_id(agent_id: str) -> str:
    text = agent_id.strip()
    if not text or len(text) > 256:
        raise CoordError("agent_id must be 1..256 characters", code="invalid_agent")
    if any(ord(char) < 32 for char in text):
        raise CoordError("agent_id cannot contain control characters", code="invalid_agent")
    return text


def validate_ttl(ttl_s: int) -> int:
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or not (MIN_TTL_S <= ttl_s <= MAX_TTL_S):
        raise CoordError(f"ttl_s must be an integer in {MIN_TTL_S}..{MAX_TTL_S}", code="invalid_ttl")
    return ttl_s


def validate_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        raise CoordError("meta must be an object", code="invalid_meta")
    for key in meta:
        if not isinstance(key, str):
            raise CoordError("meta keys must be strings", code="invalid_meta")
    return dict(meta)


def lease_payload(
    resource_id: str,
    holder_agent: str,
    acquired_at: str,
    ttl_s: int,
    file_hash: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    return {
        "resource_id": resource_id,
        "holder_agent": holder_agent,
        "acquired_at": acquired_at,
        "ttl_s": ttl_s,
        "file_hash": file_hash,
        "meta": meta,
    }


def encode_lease(lease: dict[str, Any]) -> bytes:
    ordered = {field: lease[field] for field in LEASE_FIELDS}
    return json.dumps(ordered, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def lease_digest(lease: dict[str, Any]) -> str:
    """SHA-256 of the full canonical lease, including meta.

    This is a compare-and-swap freshness proof for :meth:`CoordBoard.release`.
    It is not an authentication secret: the server still checks authenticated
    ownership, while this digest prevents a stale release from deleting a
    successor lease.
    """
    return hashlib.sha256(encode_lease(lease)).hexdigest()


def parse_lease(raw: str | bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if any(field not in data for field in LEASE_FIELDS):
        return None
    if not isinstance(data["resource_id"], str) or not isinstance(data["holder_agent"], str):
        return None
    if not isinstance(data["acquired_at"], str) or not isinstance(data["file_hash"], str):
        return None
    if not isinstance(data["ttl_s"], int) or isinstance(data["ttl_s"], bool):
        return None
    if not isinstance(data["meta"], dict):
        return None
    return {field: data[field] for field in LEASE_FIELDS}


def remaining_ttl(lease: dict[str, Any], now_ts: float) -> float:
    return max(0.0, parse_ts(lease["acquired_at"]) + int(lease["ttl_s"]) - now_ts)


def is_expired(lease: dict[str, Any], now_ts: float) -> bool:
    return remaining_ttl(lease, now_ts) <= 0.0


def _scope_components(scope_id: str) -> list[str]:
    """Split a canonical scope id into lower-cased path components.

    A scope is the body of a ``scope:`` resource_id (no trailing prefix).
    ``scripts/a/`` and ``scripts/ab`` therefore return ``["scripts", "a"]``
    and ``["scripts", "ab"]`` respectively; the comparison is by component,
    not by string prefix, so ``scripts/a`` is NOT seen as a child of
    ``scripts/ab``. ``casefold`` makes ``Scripts`` and ``scripts`` the same
    component.
    """
    body = scope_id[:-1] if scope_id.endswith("/") else scope_id
    return [part.casefold() for part in body.split("/") if part]


def _scopes_overlap(left_id: str, right_id: str) -> str | None:
    """Return the containment relation between two canonical scope ids.

    Both inputs are treated as the body of a scope, not as full resource_ids.
    The relation is reported from the perspective of ``left_id`` (the new
    request): ``"equal"`` when the two scopes are the same after
    ``casefold``; ``"contains"`` when ``left_id`` is the broader scope and
    therefore subsumes ``right_id``; ``"contained"`` when ``left_id`` is
    the narrower scope and is a sub-tree of ``right_id``; ``None`` when
    the two scopes are disjoint. A scope with fewer components is the
    broader one, so the relation is decided by which list is a prefix of
    the other. ``scripts/a`` and ``scripts/ab`` stay disjoint because
    prefix comparison is component-wise, not string-wise.
    """
    left = _scope_components(left_id)
    right = _scope_components(right_id)
    if left == right:
        return "equal"
    if len(left) <= len(right) and left == right[: len(left)]:
        return "contains"
    if len(right) <= len(left) and right == left[: len(right)]:
        return "contained"
    return None


def _is_scope_resource_id(resource_id: str) -> bool:
    """Return ``True`` if ``resource_id`` looks like a canonical scope id.

    A scope id is the body of a ``scope:`` resource (no ``scope:`` prefix;
    the prefix is stripped by :func:`canonicalize_scope`). It uses
    normalized POSIX syntax, has no drive letter, no leading ``/``, no UNC
    prefix, and is not prefixed with ``data:`` or ``agent:``. Anything
    else - absolute paths, ``data:`` and ``agent:`` ids, empty ids - is
    not a scope and never participates in overlap detection.
    """
    if not resource_id:
        return False
    if resource_id.startswith(("data:", "agent:")):
        return False
    if resource_id.startswith(("/", "\\", "//", "\\\\")):
        return False
    if len(resource_id) >= 2 and resource_id[1] == ":":
        return False
    return True


class CoordBoard:
    def __init__(
        self,
        home: Path | None = None,
        cwd: Path | None = None,
        now: NowFn | None = None,
        events_max_bytes_value: int | None = None,
        events_retain_value: int | None = None,
    ) -> None:
        self.home = ensure_home(home)
        self.cwd = cwd if cwd is not None else Path.cwd()
        self._now = now or time.time
        self.events_max_bytes = events_max_bytes() if events_max_bytes_value is None else events_max_bytes_value
        self.events_retain = events_retain() if events_retain_value is None else events_retain_value

    def now(self) -> float:
        from .store import assert_writable
        assert_writable(self.home)
        return float(self._now())

    def _event(self, event_type: str, **fields: Any) -> dict[str, Any]:
        try:
            return append_event(
                self.home,
                event_type,
                now_ts=self.now(),
                max_bytes=self.events_max_bytes,
                retain=self.events_retain,
                **fields,
            )
        except (OSError, CoordError, ValueError) as exc:
            return {
                "status": "warning",
                "code": "event_write_failed",
                "event_type": event_type,
                "message": str(exc),
            }

    def _read_lock(self, path: Path) -> dict[str, Any] | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        lease = parse_lease(raw)
        if lease is not None:
            return lease
        return None

    @staticmethod
    def _same_lease(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return all(
            left.get(field) == right.get(field)
            for field in ("resource_id", "holder_agent", "acquired_at")
        )

    def _result(self, status: str, code: str, **fields: Any) -> dict[str, Any]:
        event_receipt = fields.pop("_event_receipt", None)
        payload = {"status": status, "code": code}
        payload.update(fields)
        if isinstance(event_receipt, dict) and event_receipt.get("code") == "event_write_failed":
            payload["warnings"] = [event_receipt]
        return payload

    def _warning_held(self, lease: dict[str, Any]) -> dict[str, Any]:
        ttl_left = round(remaining_ttl(lease, self.now()), 3)
        expires_at = format_ts(parse_ts(lease["acquired_at"]) + int(lease["ttl_s"]))
        event_receipt = self._event(
            "claim_warning",
            resource_id=lease["resource_id"],
            holder_agent=lease["holder_agent"],
            ttl_remaining_s=ttl_left,
        )
        return self._result(
            "warning",
            "held",
            resource_id=lease["resource_id"],
            holder_agent=lease["holder_agent"],
            ttl_remaining_s=ttl_left,
            ttl_hint_s=ttl_left,
            expires_at=expires_at,
            lease=lease,
            _event_receipt=event_receipt,
            message=(
                f"resource held by {lease['holder_agent']}; "
                f"ttl remaining {ttl_left}s (expires {expires_at})"
            ),
        )

    def _warning_not_holder(
        self,
        lease: dict[str, Any],
        caller_agent_id: str,
        caller_strength: str | None = None,
    ) -> dict[str, Any]:
        """Emit a holder-mismatch response for release/renew from a non-holder.

        ``claim`` keeps its ``held`` contract; this branch is only reached from
        release and renew, where the failure is "you are not the holder"
        rather than "the resource is busy". The status stays ``warning`` so the
        CLI exit code stays 0 (advisory only, no deny hook); the new ``code``
        tells the caller which case actually happened. ``caller_strength``
        surfaces the caller's identity strength (``strong`` from a stable
        session signal, ``weak`` from process-fallback) so a caller with a
        weak/process-only identity can see why two sequential invocations
        end up looking like different agents. ``hint`` carries a short
        pointer to ``--session-id`` / ``COORDFS_SESSION_ID`` to keep the
        identity stable for the lifetime of the lease.
        """
        ttl_left = round(remaining_ttl(lease, self.now()), 3)
        expires_at = format_ts(parse_ts(lease["acquired_at"]) + int(lease["ttl_s"]))
        holder = lease["holder_agent"]
        event_receipt = self._event(
            "not_holder",
            resource_id=lease["resource_id"],
            holder_agent=holder,
            caller_agent_id=caller_agent_id,
            caller_strength=caller_strength,
        )
        strength_fragment = (
            f" (caller identity is {caller_strength})" if caller_strength else ""
        )
        hint = (
            f"caller is not the holder ({holder}){strength_fragment}; "
            "release and renew require the same identity as the original claim "
            "- pass a stable --session-id (or set COORDFS_SESSION_ID) for the lifetime of the lease"
        )
        return self._result(
            "warning",
            "not_holder",
            resource_id=lease["resource_id"],
            holder_agent=holder,
            ttl_remaining_s=ttl_left,
            ttl_hint_s=ttl_left,
            expires_at=expires_at,
            lease=lease,
            caller_agent_id=caller_agent_id,
            caller_strength=caller_strength,
            hint=hint,
            message=hint,
            _event_receipt=event_receipt,
        )

    def _scope_overlap_with_other_holder(
        self,
        resource_id: str,
        agent: str,
    ) -> dict[str, Any] | None:
        """Return the first live scope lease that overlaps ``resource_id``.

        Expired leases are omitted from the read-only snapshot without removing
        their lock files. A lease whose ``holder_agent`` equals ``agent`` is
        ignored: a holder broadening or narrowing its own scope is not a conflict.
        The first overlapping foreign lease wins so that the response stays
        deterministic for the same input. ``kind`` here is fixed to
        ``scope``; ``path`` and ``data`` leases do not participate in scope
        overlap detection. ``equal`` after ``casefold`` (for example
        ``Scripts/`` and ``scripts/``) is treated as a conflict because the
        two canonical ids hash to different lock files but represent the
        same scope.
        """
        # Keep claim's scan linear. ``ls`` additionally builds the O(n^2)
        # reporting snapshot; claim only needs the first matching live lease.
        for lease in self._lease_snapshot():
            other_resource_id = lease.get("resource_id", "")
            if not _is_scope_resource_id(other_resource_id):
                continue
            if lease.get("holder_agent") == agent:
                continue
            relation = _scopes_overlap(resource_id, other_resource_id)
            if relation is None:
                continue
            return {
                "lease": lease,
                "relation": relation,
                "other_resource_id": other_resource_id,
                "other_holder_agent": lease["holder_agent"],
            }
        return None

    def _lease_snapshot(self) -> list[dict[str, Any]]:
        """Read live lock files without sweeping, renewing, writing, or deleting."""
        now_ts = self.now()
        leases: list[dict[str, Any]] = []
        for path in sorted(leases_dir(self.home).glob("*.lock")):
            lease = self._read_lock(path)
            if lease is None or is_expired(lease, now_ts):
                continue
            item = dict(lease)
            item["ttl_remaining_s"] = round(remaining_ttl(lease, now_ts), 3)
            item["lock_name"] = path.name
            leases.append(item)
        return leases

    @staticmethod
    def _scope_overlap_records(
        leases: list[dict[str, Any]],
        *,
        focus_resource_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Describe live foreign-holder scope pairs from one non-atomic snapshot.

        The pair scan is intentionally O(n^2) and reuses ``_scopes_overlap`` so
        path-component and casefold semantics stay identical to claim. A focused
        result (used by ``who``) is oriented from the queried scope, even when its
        exact lock is free; an unfiltered result (used by ``ls`` and Office
        ``who``) emits each unordered pair once.
        This helper only examines the supplied dictionaries and performs no I/O.
        """
        live_scopes = sorted(
            (
                lease
                for lease in leases
                if lease.get("ttl_remaining_s", 0.0) > 0.0
                and _is_scope_resource_id(str(lease.get("resource_id", "")))
            ),
            key=lambda lease: (
                str(lease["resource_id"]).casefold(),
                str(lease["resource_id"]),
                str(lease["holder_agent"]),
            ),
        )

        def record(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any] | None:
            left_holder = left.get("holder_agent")
            if left_holder is not None and left_holder == right["holder_agent"]:
                return None
            relation = _scopes_overlap(left["resource_id"], right["resource_id"])
            if relation is None:
                return None
            return {
                "relation": relation,
                "resource_id": left["resource_id"],
                "holder_agent": left_holder,
                "other_resource_id": right["resource_id"],
                "other_holder_agent": right["holder_agent"],
            }

        if focus_resource_id is not None:
            if not _is_scope_resource_id(focus_resource_id):
                return []
            focused = next(
                (lease for lease in live_scopes if lease["resource_id"] == focus_resource_id),
                {"resource_id": focus_resource_id, "holder_agent": None},
            )
            overlaps: list[dict[str, Any]] = []
            for other in live_scopes:
                if other is focused:
                    continue
                item = record(focused, other)
                if item is not None:
                    overlaps.append(item)
            return overlaps

        overlaps = []
        for index, left in enumerate(live_scopes):
            for right in live_scopes[index + 1 :]:
                item = record(left, right)
                if item is not None:
                    overlaps.append(item)
        return overlaps

    def _warning_scope_overlap(
        self,
        resource_id: str,
        agent: str,
        overlap: dict[str, Any],
    ) -> dict[str, Any]:
        """Emit the held-shaped warning with the overlap fields attached.

        The status, code (``held``), and exit-code contract stay unchanged.
        The new ``overlap`` field documents which live lease blocked the new
        claim and how its scope relates to the requested one. ``path`` and
        ``data`` claims never trigger this branch: the overlap scan is
        guarded by the detected kind (``kind or detect_kind(...) == "scope"``)
        in :meth:`claim`, so an explicit ``kind="path"`` or ``kind="data"``
        always wins over the auto-detector and bypasses this warning.
        """
        other_lease = overlap["lease"]
        ttl_left = round(remaining_ttl(other_lease, self.now()), 3)
        expires_at = format_ts(parse_ts(other_lease["acquired_at"]) + int(other_lease["ttl_s"]))
        event_receipt = self._event(
            "claim_warning",
            resource_id=resource_id,
            holder_agent=other_lease["holder_agent"],
            ttl_remaining_s=ttl_left,
            overlap_relation=overlap["relation"],
            overlap_resource_id=overlap["other_resource_id"],
        )
        return self._result(
            "warning",
            "held",
            resource_id=resource_id,
            holder_agent=other_lease["holder_agent"],
            ttl_remaining_s=ttl_left,
            ttl_hint_s=ttl_left,
            expires_at=expires_at,
            lease=other_lease,
            overlap={
                "relation": overlap["relation"],
                "other_resource_id": overlap["other_resource_id"],
                "other_holder_agent": overlap["other_holder_agent"],
            },
            _event_receipt=event_receipt,
            message=(
                f"scope overlaps with lease held by {other_lease['holder_agent']} "
                f"on {overlap['other_resource_id']} (relation={overlap['relation']}); "
                f"ttl remaining {ttl_left}s (expires {expires_at})"
            ),
        )

    def _stale(self, lease: dict[str, Any], observed: str) -> dict[str, Any]:
        event_receipt = self._event(
            "stale_resource",
            resource_id=lease["resource_id"],
            holder_agent=lease["holder_agent"],
            expected_hash=lease["file_hash"],
            observed_hash=observed,
        )
        return self._result(
            "conflict",
            "stale_resource",
            resource_id=lease["resource_id"],
            holder_agent=lease["holder_agent"],
            expected_hash=lease["file_hash"],
            observed_hash=observed,
            lease=lease,
            _event_receipt=event_receipt,
            message="resource hash changed since claim; lease is stale",
        )

    def _unlink_expired_locked(self, path: Path, lease: dict[str, Any]) -> bool:
        current = self._read_lock(path)
        if current is None:
            return True
        if not self._same_lease(current, lease) or not is_expired(current, self.now()):
            return False
        try:
            removed = unlink_with_backoff(path)
        except OSError:
            return False
        if not removed:
            return False
        self._event(
            "expired",
            resource_id=lease["resource_id"],
            holder_agent=lease["holder_agent"],
        )
        return True

    def _unlink_expired(self, path: Path, lease: dict[str, Any]) -> bool:
        mutex = lease_mutex_path(self.home, lease["resource_id"])
        with exclusive_mutex(mutex):
            return self._unlink_expired_locked(path, lease)

    def sweep(self, record: bool = True) -> dict[str, Any]:
        reclaimed: list[str] = []
        now_ts = self.now()
        for path in sorted(leases_dir(self.home).glob("*.lock")):
            lease = self._read_lock(path)
            if lease is None:
                continue
            if is_expired(lease, now_ts) and self._unlink_expired(path, lease):
                reclaimed.append(lease["resource_id"])
        if record:
            event_receipt = self._event("swept", count=len(reclaimed), resources=reclaimed)
        else:
            event_receipt = None
        return self._result("ok", "swept", reclaimed=reclaimed, count=len(reclaimed), _event_receipt=event_receipt)

    def claim(
        self,
        resource: str,
        agent_id: str,
        ttl_s: int = DEFAULT_TTL_S,
        meta: dict[str, Any] | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        agent = validate_agent_id(agent_id)
        ttl = validate_ttl(ttl_s)
        meta_value = validate_meta(meta)
        resource_id = canonicalize_resource(resource, kind=kind, cwd=self.cwd)
        # The CLI and direct callers frequently pass kind=None; canonicalize_resource
        # auto-detects "scope" from the prefix or trailing slash, but the overlap
        # scan must still run for the auto-detected case or two scope claims with
        # kind=None will silently coexist (SKILL.md documents the collision
        # unconditionally). Explicit kinds win over the detector.
        detected_kind = kind or detect_kind(resource, cwd=self.cwd)
        digest = resource_hash(resource_id)
        path = lock_path(self.home, resource_id)
        if detected_kind == "scope":
            overlap = self._scope_overlap_with_other_holder(resource_id, agent)
            if overlap is not None:
                return self._warning_scope_overlap(resource_id, agent, overlap)
        observed = self._read_lock(path)
        if (
            observed is not None
            and not is_expired(observed, self.now())
            and observed["holder_agent"] != agent
        ):
            return self._warning_held(observed)
        acquired_at = format_ts(self.now())
        lease = lease_payload(resource_id, agent, acquired_at, ttl, digest, meta_value)
        mutex = lease_mutex_path(self.home, resource_id)
        try:
            with exclusive_mutex(mutex):
                existing = self._read_lock(path)
                if existing is None:
                    try:
                        exclusive_create(path, encode_lease(lease))
                    except FileExistsError:
                        existing = self._read_lock(path)
                        if existing is None:
                            raise CoordError("lease create raced and lock is unreadable", code="busy")
                        return self._resolve_existing(existing, lease, path, mutex_held=True)
                    created = "claimed"
                elif is_expired(existing, self.now()):
                    if not self._unlink_expired_locked(path, existing):
                        raced = self._read_lock(path)
                        if raced is None:
                            raise CoordError("lease reclaim raced and lock is unreadable", code="busy")
                        return self._resolve_existing(raced, lease, path, mutex_held=True)
                    exclusive_create(path, encode_lease(lease))
                    created = "reclaimed"
                else:
                    return self._resolve_existing(existing, lease, path, mutex_held=True)
        except CoordError as exc:
            if exc.code != "busy":
                raise
            current = self._read_lock(path)
            if (
                current is not None
                and not is_expired(current, self.now())
                and current["holder_agent"] != agent
            ):
                return self._warning_held(current)
            raise
        event_receipt = self._event(
            created,
            resource_id=resource_id,
            holder_agent=agent,
            lock_name=path.name,
            file_hash=digest,
        )
        return self._result(
            "ok",
            created,
            resource_id=resource_id,
            holder_agent=agent,
            lease=lease,
            lease_digest=lease_digest(lease),
            lock_name=path.name,
            _event_receipt=event_receipt,
        )

    def _resolve_existing(
        self,
        existing: dict[str, Any],
        requested: dict[str, Any],
        path: Path,
        *,
        mutex_held: bool = False,
    ) -> dict[str, Any]:
        if not mutex_held:
            raise CoordError("lease transition requires resource mutex", code="internal_error")
        if is_expired(existing, self.now()):
            if not self._unlink_expired_locked(path, existing):
                raced = self._read_lock(path)
                if raced is None:
                    raise CoordError("lease reclaim raced and lock is unreadable", code="busy")
                return self._warning_held(raced)
            try:
                exclusive_create(path, encode_lease(requested))
            except FileExistsError:
                raced = self._read_lock(path)
                if raced is None:
                    raise CoordError("lease reclaim raced and lock is unreadable", code="busy")
                if raced["holder_agent"] == requested["holder_agent"]:
                    return self._result("ok", "claimed", resource_id=raced["resource_id"], holder_agent=raced["holder_agent"], lease=raced, lock_name=path.name)
                return self._warning_held(raced)
            event_receipt = self._event("reclaimed", resource_id=requested["resource_id"], holder_agent=requested["holder_agent"], lock_name=path.name)
            return self._result("ok", "reclaimed", resource_id=requested["resource_id"], holder_agent=requested["holder_agent"], lease=requested, lock_name=path.name, _event_receipt=event_receipt)
        observed = resource_hash(existing["resource_id"])
        if observed != existing["file_hash"] and existing["holder_agent"] == requested["holder_agent"]:
            return self._stale(existing, observed)
        if existing["holder_agent"] == requested["holder_agent"] and observed == existing["file_hash"]:
            return self._result(
                "ok",
                "held_by_self",
                resource_id=existing["resource_id"],
                holder_agent=existing["holder_agent"],
                lease=existing,
                lock_name=path.name,
            )
        return self._warning_held(existing)

    def _require_own_fresh(
        self,
        resource: str,
        agent_id: str,
        kind: str | None,
        caller_strength: str | None = None,
    ) -> dict[str, Any]:
        agent = validate_agent_id(agent_id)
        resource_id = canonicalize_resource(resource, kind=kind, cwd=self.cwd)
        path = lock_path(self.home, resource_id)
        lease = self._read_lock(path)
        if lease is None:
            return self._result("error", "not_held", resource_id=resource_id, holder_agent=agent, message="no active lease")
        if lease["holder_agent"] != agent:
            return self._warning_not_holder(lease, agent, caller_strength)
        if is_expired(lease, self.now()):
            observed = resource_hash(lease["resource_id"])
            if observed != lease["file_hash"]:
                return self._stale(lease, observed)
            return self._result(
                "error",
                "lease_expired",
                resource_id=resource_id,
                holder_agent=agent,
                lease=lease,
                message="lease expired before commit",
            )
        return self._result("ok", "owned", resource_id=resource_id, holder_agent=agent, lease=lease, lock_name=path.name, path=str(path))

    def release(
        self,
        resource: str,
        agent_id: str,
        kind: str | None = None,
        *,
        caller_strength: str | None = None,
        expected_lease_digest: str | None = None,
        expected_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        owned = self._require_own_fresh(resource, agent_id, kind, caller_strength=caller_strength)
        if owned["status"] != "ok":
            return owned
        if expected_meta is not None and (
            not isinstance(expected_meta, dict)
            or any(not isinstance(key, str) for key in expected_meta)
        ):
            return self._result(
                "conflict",
                "lease_meta_invalid",
                resource_id=owned["resource_id"],
                holder_agent=agent_id,
                lease=owned["lease"],
                message="expected_meta must be an object with string keys",
            )
        path = Path(owned["path"])
        mutex = lease_mutex_path(self.home, owned["resource_id"])
        with exclusive_mutex(mutex):
            current = self._read_lock(path)
            if current is None:
                return self._result("error", "not_held", resource_id=owned["resource_id"], holder_agent=agent_id, message="lease vanished before release")
            if not self._same_lease(current, owned["lease"]):
                return self._warning_held(current)
            if expected_meta is not None and current.get("meta") != expected_meta:
                # Scope proof is checked under the same resource mutex as the
                # unlink. A valid actor/digest for another binding cannot release
                # this lease, and cannot race a metadata change between check/use.
                return self._result(
                    "conflict",
                    "lease_meta_mismatch",
                    resource_id=current["resource_id"],
                    holder_agent=current["holder_agent"],
                    lease=current,
                    message="lease metadata does not match the expected scope; release refused",
                )
            if expected_lease_digest is not None:
                if (not isinstance(expected_lease_digest, str)
                        or re.fullmatch(r"[0-9a-f]{64}", expected_lease_digest) is None):
                    return self._result(
                        "conflict",
                        "lease_digest_invalid",
                        resource_id=current["resource_id"],
                        holder_agent=current["holder_agent"],
                        lease=current,
                        message="expected_lease_digest must be a lowercase SHA-256 digest",
                    )
                observed_digest = lease_digest(current)
                if observed_digest != expected_lease_digest:
                    # Fenced CAS: a foreign, stale or successor proof never
                    # unlinks the lease that is actually on disk.
                    return self._result(
                        "conflict",
                        "lease_digest_mismatch",
                        resource_id=current["resource_id"],
                        holder_agent=current["holder_agent"],
                        lease=current,
                        message="lease digest does not match the current lease; release refused",
                    )
            committed_hash = resource_hash(current["resource_id"])
            if is_expired(current, self.now()):
                if committed_hash != current["file_hash"]:
                    return self._stale(current, committed_hash)
                return self._result("error", "lease_expired", resource_id=current["resource_id"], holder_agent=agent_id, lease=current, message="lease expired before commit")
            if not unlink_with_backoff(path):
                return self._result("error", "not_held", resource_id=owned["resource_id"], holder_agent=agent_id, message="lease vanished before release")
        event_receipt = self._event("released", resource_id=owned["resource_id"], holder_agent=owned["holder_agent"], lock_name=path.name, committed_hash=committed_hash)
        return self._result("ok", "released", resource_id=owned["resource_id"], holder_agent=owned["holder_agent"], lease=owned["lease"], committed_hash=committed_hash, lease_digest=lease_digest(owned["lease"]), _event_receipt=event_receipt)

    def renew(
        self,
        resource: str,
        agent_id: str,
        ttl_s: int | None = None,
        kind: str | None = None,
        *,
        caller_strength: str | None = None,
    ) -> dict[str, Any]:
        owned = self._require_own_fresh(resource, agent_id, kind, caller_strength=caller_strength)
        if owned["status"] != "ok":
            return owned
        lease = dict(owned["lease"])
        ttl = validate_ttl(int(lease["ttl_s"]) if ttl_s is None else ttl_s)
        path = Path(owned["path"])
        tmp = path.with_name(path.name + f".renew-{int(self.now() * 1000)}")
        mutex = lease_mutex_path(self.home, owned["resource_id"])
        with exclusive_mutex(mutex):
            try:
                current = self._read_lock(path)
                if current is None or not self._same_lease(current, owned["lease"]):
                    if current is None:
                        return self._result("error", "not_held", resource_id=lease["resource_id"], holder_agent=agent_id, message="lease vanished before renew")
                    return self._warning_held(current)
                committed_hash = resource_hash(current["resource_id"])
                if is_expired(current, self.now()):
                    if committed_hash != current["file_hash"]:
                        return self._stale(current, committed_hash)
                    return self._result("error", "lease_expired", resource_id=current["resource_id"], holder_agent=agent_id, lease=current, message="lease expired before commit")
                lease["ttl_s"] = ttl
                lease["acquired_at"] = format_ts(self.now())
                lease["file_hash"] = committed_hash
                exclusive_create(tmp, encode_lease(lease))
                if not replace_with_backoff(tmp, path):
                    raise CoordError("lease replace remained busy", code="busy")
            except FileExistsError:
                raise CoordError("renew temp lock already exists", code="busy")
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        event_receipt = self._event("renewed", resource_id=lease["resource_id"], holder_agent=lease["holder_agent"], lock_name=path.name)
        return self._result("ok", "renewed", resource_id=lease["resource_id"], holder_agent=lease["holder_agent"], lease=lease, _event_receipt=event_receipt)

    def ls(self) -> dict[str, Any]:
        leases = self._lease_snapshot()
        return self._result(
            "ok",
            "listed",
            leases=leases,
            count=len(leases),
            scope_overlaps=self._scope_overlap_records(leases),
        )

    def who(self, resource: str, kind: str | None = None) -> dict[str, Any]:
        resource_id = canonicalize_resource(resource, kind=kind, cwd=self.cwd)
        path = lock_path(self.home, resource_id)
        lease = self._read_lock(path)
        if lease is not None and is_expired(lease, self.now()):
            lease = None
        scope_overlaps = self._scope_overlap_records(
            self._lease_snapshot(),
            focus_resource_id=resource_id,
        )
        if lease is None:
            return self._result(
                "ok",
                "free",
                resource_id=resource_id,
                holder_agent=None,
                lease=None,
                scope_overlaps=scope_overlaps,
            )
        ttl_left = round(remaining_ttl(lease, self.now()), 3)
        return self._result(
            "ok",
            "who",
            resource_id=resource_id,
            holder_agent=lease["holder_agent"],
            ttl_remaining_s=ttl_left,
            ttl_hint_s=ttl_left,
            lease=lease,
            scope_overlaps=scope_overlaps,
        )

    def lineage_meta(
        self,
        meta: dict[str, Any] | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
        graph_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        merged = validate_meta(meta)
        for key, value in (
            ("task_id", task_id),
            ("node_id", node_id),
            ("graph_id", graph_id),
            ("run_id", run_id),
        ):
            if value:
                merged[key] = value
        unused = LINEAGE_META_KEYS
        _ = unused
        return merged
