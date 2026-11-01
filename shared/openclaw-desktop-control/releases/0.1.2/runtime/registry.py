"""Narrow adapter registry. Unverified names stay DisabledAdapter.

Broker fencing (single-worker serialization) is NOT receiver CAS.
Unverified names stay disabled; there is no CLI fallback.
"""

from __future__ import annotations

from typing import Any, Optional

from openclaw_control.adapters import DisabledAdapter, is_capable


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Any] = {}
        self._meta: dict[str, dict] = {}

    def register(
        self,
        name: str,
        adapter: Any,
        *,
        verified: bool,
        capabilities: Optional[dict] = None,
        reason: str = "",
    ) -> None:
        caps = dict(capabilities or {})
        if not verified:
            disabled = DisabledAdapter(name)
            self._adapters[name] = disabled
            self._meta[name] = {
                "name": name,
                "verified": False,
                "enabled": False,
                "reason": reason or "unverified-disabled",
                "cli_fallback": False,
                "capabilities": caps,
            }
            return
        self._adapters[name] = adapter
        ok, missing = is_capable(adapter)
        self._meta[name] = {
            "name": name,
            "verified": True,
            "enabled": ok,
            "reason": reason or ("" if ok else "missing:" + ",".join(missing)),
            "cli_fallback": False,
            "supports_cancel": bool(getattr(adapter, "supports_cancel", False)),
            "supports_atomic_last_turn_cas": bool(
                getattr(adapter, "supports_atomic_last_turn_cas", False)
            ),
            "supports_literal_prefixes": bool(
                getattr(adapter, "supports_literal_prefixes", False)
            ),
            "capabilities": caps,
        }

    def get(self, name: str) -> Any:
        if name in self._adapters:
            return self._adapters[name]
        return DisabledAdapter(name)

    def meta(self, name: str) -> dict:
        if name in self._meta:
            return dict(self._meta[name])
        return {
            "name": name,
            "verified": False,
            "enabled": False,
            "reason": "unregistered",
            "cli_fallback": False,
        }

    def listing(self) -> list:
        names = sorted(set(self._meta) | set(self._adapters))
        return [self.meta(n) for n in names]

    def close(self):
        for adapter in self._adapters.values():
            app = getattr(getattr(adapter, 'desktop', None), 'app', None)
            if app is not None and hasattr(app, 'close'):
                app.close()
