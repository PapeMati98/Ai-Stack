"""Focused lifetime and admission regressions for pending named-pipe I/O."""
from __future__ import annotations

import gc
import os
import time
import unittest
import weakref
from unittest import mock

from coordfs import office_pipe
from coordfs.office_pipe import PipeUnavailable, PipeWaiter


@unittest.skipUnless(os.name == "nt", "Windows named pipes required")
class PipeReaperTests(unittest.TestCase):
    def tearDown(self) -> None:
        # Clearing the set alone cannot invalidate a snapshot already captured
        # by the daemon.  Join its ownership barrier before dropping fakes.
        with office_pipe._REAP_LOCK:
            with office_pipe._RETAINED_LOCK:
                office_pipe._RETAINED_PENDING.clear()

    @staticmethod
    def _pending(number: int) -> PipeWaiter:
        waiter = PipeWaiter.__new__(PipeWaiter)
        waiter._closed, waiter._pending, waiter._reaping = False, True, False
        waiter.handle, waiter.event = number * 2, number * 2 + 1
        waiter.overlapped = office_pipe.OVERLAPPED()
        return waiter

    def test_sixty_five_completed_abandoned_operations_are_reaped(self):
        waiters = [self._pending(number) for number in range(1, 66)]
        overlap_ref = weakref.ref(waiters[0].overlapped)
        with mock.patch.object(office_pipe.kernel32, "WaitForSingleObject",
                               return_value=office_pipe.WAIT_OBJECT_0), \
             mock.patch.object(office_pipe.kernel32, "GetOverlappedResult", return_value=True), \
             mock.patch.object(office_pipe.kernel32, "CloseHandle", return_value=True) as close:
            for waiter in waiters:
                waiter._retain_until_complete()
                waiter._arm_reaper()
            del waiter, waiters
            gc.collect()
            self.assertIsNotNone(overlap_ref())
            deadline = time.monotonic() + 2
            while office_pipe._RETAINED_PENDING and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(office_pipe._RETAINED_PENDING)
            self.assertEqual(close.call_count, 130)
            # Keep patched kernel callbacks installed until a daemon pass that
            # may have captured these waiters has completely returned.
            with office_pipe._REAP_LOCK:
                pass
        gc.collect()
        self.assertIsNone(overlap_ref())

    def test_permanently_incomplete_backlog_blocks_admission_without_handles(self):
        stuck = [self._pending(number) for number in range(1, office_pipe.MAX_WAITERS + 1)]
        with office_pipe._RETAINED_LOCK:
            for waiter in stuck:
                waiter._reaping = True
                waiter._reap_ready = False
                office_pipe._RETAINED_PENDING.add(waiter)
        with mock.patch.object(office_pipe.kernel32, "CreateNamedPipeW") as create:
            with self.assertRaisesRegex(PipeUnavailable, "pending-operation limit"):
                PipeWaiter(r"\\.\pipe\bounded-test")
        create.assert_not_called()

    def test_error_five_and_incomplete_never_close_before_completion(self):
        waiter = self._pending(100)
        overlap_ref = weakref.ref(waiter.overlapped)
        completed = False

        def wait_for_single(_event, _milliseconds):
            return office_pipe.WAIT_OBJECT_0 if completed else office_pipe.WAIT_TIMEOUT

        with mock.patch.object(office_pipe.kernel32, "CancelIoEx", return_value=False), \
             mock.patch.object(office_pipe.ctypes, "get_last_error", return_value=5), \
             mock.patch.object(office_pipe.kernel32, "WaitForSingleObject", side_effect=wait_for_single), \
             mock.patch.object(office_pipe.kernel32, "GetOverlappedResult", return_value=False), \
             mock.patch.object(office_pipe.kernel32, "CloseHandle", return_value=True) as close:
            with self.assertRaises(PipeUnavailable):
                waiter.close()
            del waiter
            gc.collect()
            self.assertIsNotNone(overlap_ref())
            self.assertEqual(close.call_count, 0)
            completed = True
            with mock.patch.object(office_pipe.ctypes, "get_last_error",
                                   return_value=office_pipe.ERROR_OPERATION_ABORTED):
                deadline = time.monotonic() + 2
                while office_pipe._RETAINED_PENDING and time.monotonic() < deadline:
                    office_pipe._REAPER_WAKE.set()
                    time.sleep(.01)
            self.assertFalse(office_pipe._RETAINED_PENDING)
            self.assertEqual(close.call_count, 2)
            with office_pipe._REAP_LOCK:
                pass


if __name__ == "__main__":
    unittest.main()
