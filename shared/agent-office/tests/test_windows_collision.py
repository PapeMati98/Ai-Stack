from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import patch

from support import CoordTestCase

from coordfs.atomic import exclusive_create, exclusive_mutex


class WindowsCollisionTests(CoordTestCase):
    def test_windows_delete_pending_codes_normalize_to_collision(self) -> None:
        target = self.home / "collision.lock"
        for winerror in (5, 32, 80, 183, 303):
            error = PermissionError(13, "simulated Windows collision", str(target))
            error.winerror = winerror  # type: ignore[attr-defined]
            with self.subTest(winerror=winerror):
                with patch("coordfs.atomic.os.open", side_effect=error):
                    with self.assertRaises(FileExistsError):
                        exclusive_create(target, b"payload")

    def test_windows_permission_error_without_winerror_normalizes_to_collision(self) -> None:
        target = self.home / "errno-only-collision.lock"
        error = PermissionError(errno.EACCES, "delete pending without winerror", str(target))

        with patch("coordfs.atomic.os.name", "nt"):
            with patch("coordfs.atomic.os.open", side_effect=error):
                with self.assertRaises(FileExistsError):
                    exclusive_create(target, b"payload")

    def test_mutex_cleanup_retries_transient_permission_error(self) -> None:
        target = self.home / "cleanup.lock"
        real_unlink = Path.unlink
        calls = 0

        def flaky_unlink(path: Path, *args, **kwargs):
            nonlocal calls
            if path.name.startswith(target.name) and calls == 0:
                calls += 1
                error = PermissionError(13, "delete pending", str(path))
                error.winerror = 303  # type: ignore[attr-defined]
                raise error
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", flaky_unlink):
            with exclusive_mutex(target):
                self.assertTrue(target.exists())

        self.assertEqual(calls, 1)
        self.assertFalse(target.exists())
