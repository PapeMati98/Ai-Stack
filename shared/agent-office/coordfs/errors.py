"""Typed failures for coordfs. Callers map these to JSON payloads."""

from __future__ import annotations

from typing import Any


class CoordError(Exception):
    def __init__(
        self,
        message: str,
        code: str = "error",
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload or {}
