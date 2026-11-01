"""Real Windows named-pipe regressions with temporary durable homes."""
from __future__ import annotations
import gc, os, threading, time, unittest, weakref
from unittest import mock
from pathlib import Path
from office_support import OfficeTestCase
from coordfs.office import Office
from coordfs import office_pipe
from coordfs.office_pipe import PIPE_REJECT_REMOTE_CLIENTS, PipeUnavailable, PipeWaiter, namespace

@unittest.skipUnless(os.name == "nt", "Windows named pipes required")
class NamedPipeTests(OfficeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.announce("codex-testhost-alice", "claude-testhost-bob")

    def test_failed_connect_constructor_does_not_drain_unissued_io(self):
        with mock.patch.object(office_pipe.kernel32, "ConnectNamedPipe", return_value=False), \
             mock.patch.object(office_pipe.ctypes, "get_last_error", return_value=1234), \
             mock.patch.object(office_pipe.kernel32, "GetOverlappedResult") as drain:
            with self.assertRaises(PipeUnavailable):
                PipeWaiter(namespace(self.home, "inbox", "claude-testhost-bob"))
        drain.assert_not_called()

    def test_close_drains_pending_io_before_closing_handles(self):
        waiter = PipeWaiter.__new__(PipeWaiter)
        waiter._closed, waiter._pending = False, True
        waiter.handle, waiter.event, waiter.overlapped = 11, 12, office_pipe.OVERLAPPED()
        completed = False
        with mock.patch.object(office_pipe.kernel32, "CancelIoEx", return_value=True), \
             mock.patch.object(office_pipe.kernel32, "WaitForSingleObject",
                               side_effect=lambda *_: office_pipe.WAIT_OBJECT_0 if completed else office_pipe.WAIT_TIMEOUT), \
             mock.patch.object(office_pipe.kernel32, "GetOverlappedResult",
                               side_effect=lambda *_: completed) as drain, \
             mock.patch.object(office_pipe.ctypes, "get_last_error", return_value=office_pipe.ERROR_IO_INCOMPLETE), \
             mock.patch.object(office_pipe.kernel32, "CloseHandle") as close:
            with self.assertRaises(PipeUnavailable): waiter.close()
            self.assertTrue(drain.call_args.args[-1])
            close.assert_not_called()
            completed = True
            office_pipe._REAPER_WAKE.set()
            office_pipe._reap_retained_once()
            with office_pipe._REAP_LOCK:
                pass
            self.assertEqual(close.call_count, 2)

    def test_incomplete_cancel_retains_overlapped_after_gc(self):
        waiter = PipeWaiter.__new__(PipeWaiter)
        waiter._closed, waiter._pending, waiter._reaping = False, True, False
        waiter.handle, waiter.event, waiter.overlapped = 11, 12, office_pipe.OVERLAPPED()
        overlapped_ref = weakref.ref(waiter.overlapped)
        completed = False
        with mock.patch.object(office_pipe.kernel32, "CancelIoEx", return_value=True), \
             mock.patch.object(office_pipe.kernel32, "WaitForSingleObject",
                               side_effect=lambda *_: office_pipe.WAIT_OBJECT_0 if completed else office_pipe.WAIT_TIMEOUT), \
             mock.patch.object(office_pipe.kernel32, "GetOverlappedResult",
                               side_effect=lambda *_: completed), \
             mock.patch.object(office_pipe.ctypes, "get_last_error", return_value=office_pipe.ERROR_IO_INCOMPLETE), \
             mock.patch.object(office_pipe.kernel32, "CloseHandle") as close:
            with self.assertRaises(PipeUnavailable):
                waiter.close()
            del waiter
            gc.collect()
            self.assertIsNotNone(overlapped_ref())
            close.assert_not_called()
            completed = True
            office_pipe._REAPER_WAKE.set()
            office_pipe._reap_retained_once()
            with office_pipe._REAP_LOCK:
                pass
            self.assertEqual(close.call_count, 2)

    def test_failed_cancel_retains_overlapped_before_error(self):
        waiter = PipeWaiter.__new__(PipeWaiter)
        waiter._closed, waiter._pending, waiter._reaping = False, True, False
        waiter.handle, waiter.event, waiter.overlapped = 11, 12, office_pipe.OVERLAPPED()
        overlapped_ref = weakref.ref(waiter.overlapped)
        completed = False
        with mock.patch.object(office_pipe.kernel32, "CancelIoEx", return_value=False), \
             mock.patch.object(office_pipe.ctypes, "get_last_error", return_value=5), \
             mock.patch.object(office_pipe.kernel32, "WaitForSingleObject",
                               side_effect=lambda *_: office_pipe.WAIT_OBJECT_0 if completed else office_pipe.WAIT_TIMEOUT), \
             mock.patch.object(office_pipe.kernel32, "GetOverlappedResult", return_value=True), \
             mock.patch.object(office_pipe.kernel32, "CloseHandle") as close:
            with self.assertRaises(PipeUnavailable):
                waiter.close()
            del waiter
            gc.collect()
            self.assertIsNotNone(overlapped_ref())
            close.assert_not_called()
            completed = True
            office_pipe._REAPER_WAKE.set()
            office_pipe._reap_retained_once()
            with office_pipe._REAP_LOCK:
                pass
            self.assertEqual(close.call_count, 2)

    def test_concurrent_caller_and_reaper_close_each_handle_once(self):
        waiter = PipeWaiter.__new__(PipeWaiter)
        waiter._closed, waiter._pending = False, True
        waiter._reaping, waiter._reap_ready = True, True
        waiter.handle, waiter.event, waiter.overlapped = 1200, 1201, office_pipe.OVERLAPPED()
        entered, release = threading.Event(), threading.Event()
        calls, errors = [], []

        def close_handle(handle):
            if threading.current_thread().name == "test-reaper" and handle == 1200:
                entered.set()
                self.assertTrue(release.wait(2))
            calls.append(handle)
            return True

        def invoke(callback):
            try:
                callback()
            except Exception as error:
                errors.append(error)

        with mock.patch.object(office_pipe.kernel32, "WaitForSingleObject", return_value=office_pipe.WAIT_OBJECT_0), \
             mock.patch.object(office_pipe.kernel32, "GetOverlappedResult", return_value=True), \
             mock.patch.object(office_pipe.kernel32, "CancelIoEx", return_value=True), \
             mock.patch.object(office_pipe.kernel32, "CloseHandle", side_effect=close_handle):
            with office_pipe._RETAINED_LOCK:
                office_pipe._RETAINED_PENDING.add(waiter)
            reaper = threading.Thread(target=lambda: invoke(office_pipe._reap_retained_once), name="test-reaper")
            caller = threading.Thread(target=lambda: invoke(waiter.close), name="test-caller")
            reaper.start()
            self.assertTrue(entered.wait(2))
            caller.start()
            caller.join(.05)
            release.set()
            reaper.join(2)
            caller.join(2)
            self.assertFalse(reaper.is_alive())
            self.assertFalse(caller.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(calls, [1200, 1201])
            with office_pipe._REAP_LOCK:
                pass

    def test_listener_before_recheck_closes_registration_race(self):
        ready, holder = threading.Event(), {}
        original = self.bob._read_cursor
        def delayed(source): ready.set(); time.sleep(.1); return original(source)
        self.bob._read_cursor = delayed
        thread = threading.Thread(target=lambda: holder.setdefault("r", self.bob.wait(3)))
        thread.start(); self.assertTrue(ready.wait(1)); self.alice.send("claude-testhost-bob", "race"); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertEqual(holder["r"]["code"], "message")

    def test_channel_fans_out_three_agents(self):
        agents = [Office(agent_id=n, home=self.home) for n in ("codex-testhost-alice", "claude-testhost-bob", "qwen-testhost-carol")]
        results = {}
        threads = [threading.Thread(target=lambda a=a: results.setdefault(a.agent_id, a.wait(4, channel="team"))) for a in agents]
        for thread in threads: thread.start()
        time.sleep(.2); self.alice.send(None, "all", channel="team")
        for thread in threads: thread.join(2)
        self.assertTrue(all(results[a.agent_id]["code"] == "message" for a in agents))

    def test_wake_after_thirty_seconds_idle_under_two_seconds(self):
        holder = {}
        thread = threading.Thread(target=lambda: holder.setdefault("r", self.bob.wait(40)))
        thread.start(); time.sleep(30.1); sent = time.monotonic(); self.alice.send("claude-testhost-bob", "long idle")
        thread.join(3)
        self.assertFalse(thread.is_alive()); self.assertEqual(holder["r"]["code"], "message")
        self.assertLess(time.monotonic() - sent, 2.0)

    def test_home_isolation_closed_name_and_remote_flag(self):
        other = self.home / "other"
        self.assertNotEqual(namespace(self.home, "inbox", "claude-testhost-bob"), namespace(other, "inbox", "claude-testhost-bob"))
        self.assertEqual(namespace(Path(str(self.home).swapcase()), "inbox", "claude-testhost-bob"), namespace(self.home, "inbox", "claude-testhost-bob"))
        name = namespace(self.home, "inbox", "claude-testhost-bob")
        self.assertTrue(name.startswith(r"\\.\pipe\agent-office-")); self.assertNotIn(str(self.home), name)
        self.assertEqual(PIPE_REJECT_REMOTE_CLIENTS, 8)

    def test_offline_ask_and_correlation(self):
        asked = self.alice.ask("claude-testhost-bob", "question")
        message = self.bob.read()["messages"][0]["untrusted_message"]
        self.assertEqual(message["kind"], "question")
        self.bob.send("codex-testhost-alice", "answer", reply_to=asked["id"])
        self.assertEqual(self.alice.read()["messages"][0]["untrusted_message"]["reply_to"], asked["id"])

    def test_ask_rearms_after_unrelated_wake(self):
        self.bob.presence()
        holder, scans = {}, 0
        original = self.alice._scan_private
        def counted(*args):
            nonlocal scans
            scans += 1
            return original(*args)
        self.alice._scan_private = counted
        thread = threading.Thread(
            target=lambda: holder.setdefault("r", self.alice.ask("claude-testhost-bob", "question", timeout_s=.65))
        )
        thread.start()
        time.sleep(.15)
        self.bob.send("codex-testhost-alice", "unrelated")
        question = self.bob.read()["messages"][0]["untrusted_message"]
        time.sleep(.1)
        self.bob.send("codex-testhost-alice", "answer", reply_to=question["id"])
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(holder["r"]["code"], "reply")
        self.assertLessEqual(scans, 5)
