from __future__ import annotations

import json
import stat
import unittest
from types import SimpleNamespace
from typing import Any

import adaptive


class SpyStream:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.read_sizes: list[int] = []

    def __enter__(self) -> "SpyStream":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.data[:size]


class ProbePath:
    def __init__(
        self,
        data: bytes,
        *,
        stat_size: int | None = None,
        mode: int = stat.S_IFREG | 0o600,
        symlink: bool = False,
        file_attributes: int = 0,
        lstat_error: OSError | None = None,
    ) -> None:
        self.data = data
        self.stat_size = len(data) if stat_size is None else stat_size
        self.mode = mode
        self.symlink = symlink
        self.file_attributes = file_attributes
        self.lstat_error = lstat_error
        self.open_calls: list[str] = []
        self.stream = SpyStream(data)

    def lstat(self) -> Any:
        if self.lstat_error is not None:
            raise self.lstat_error
        return SimpleNamespace(
            st_mode=self.mode,
            st_size=self.stat_size,
            st_file_attributes=self.file_attributes,
        )

    def is_symlink(self) -> bool:
        return self.symlink

    def open(self, mode: str) -> SpyStream:
        self.open_calls.append(mode)
        return self.stream


class ReadStateJsonTests(unittest.TestCase):
    def test_read_request_is_stat_size_plus_one_not_global_cap_plus_one(self) -> None:
        payload = b'{"ok":true}'
        path = ProbePath(payload)

        value, reason = adaptive.read_state_json(path, max_bytes=8_000_000)  # type: ignore[arg-type]

        self.assertEqual((value, reason), ({"ok": True}, None))
        self.assertEqual(path.open_calls, ["rb"])
        self.assertEqual(path.stream.read_sizes, [len(payload) + 1])

    def test_rejects_file_growth_after_stat(self) -> None:
        payload = b'{"x":1}'
        path = ProbePath(payload, stat_size=len(payload) - 1)

        value, reason = adaptive.read_state_json(path, max_bytes=8_000_000)  # type: ignore[arg-type]

        self.assertIsNone(value)
        self.assertEqual(reason, "corrupt-cache-ignored")
        self.assertEqual(path.stream.read_sizes, [len(payload)])

    def test_accepts_exact_limit_and_rejects_oversize_from_metadata(self) -> None:
        payload = b'{"x":1}'
        exact = ProbePath(payload)
        self.assertEqual(adaptive.read_state_json(exact, max_bytes=len(payload)), ({"x": 1}, None))
        self.assertEqual(exact.stream.read_sizes, [len(payload) + 1])

        oversize = ProbePath(payload)
        self.assertEqual(adaptive.read_state_json(oversize, max_bytes=len(payload) - 1), (None, "corrupt-cache-ignored"))
        self.assertEqual(oversize.open_calls, [])

    def test_symlink_nonregular_and_reparse_files_are_rejected_before_read(self) -> None:
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
        blocked_paths = (
            ProbePath(b"{}", symlink=True),
            ProbePath(b"{}", mode=stat.S_IFDIR | 0o700),
            ProbePath(b"{}", file_attributes=reparse_flag),
        )
        for path in blocked_paths:
            with self.subTest(symlink=path.symlink, mode=path.mode, attributes=path.file_attributes):
                self.assertEqual(adaptive.read_state_json(path, max_bytes=100), (None, "corrupt-cache-ignored"))
                self.assertEqual(path.open_calls, [])

    def test_invalid_json_is_fail_closed(self) -> None:
        path = ProbePath(b"not-json")
        self.assertEqual(adaptive.read_state_json(path, max_bytes=100), (None, "corrupt-cache-ignored"))

    def test_missing_file_is_reported_without_open(self) -> None:
        path = ProbePath(b"", lstat_error=FileNotFoundError())
        self.assertEqual(adaptive.read_state_json(path, max_bytes=100), (None, "missing"))
        self.assertEqual(path.open_calls, [])


if __name__ == "__main__":
    unittest.main()