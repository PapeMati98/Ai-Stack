"""Deadline-bound CLI execution, including inherited stdout/stderr handles."""
import codecs
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

CLEANUP_GRACE_SECONDS = 2.0
MAX_CONTEXT_LINE_CHARS = 1024 * 1024
CHECKPOINT_INTERVAL_SECONDS = 1.0
CHECKPOINT_INTERVAL_BYTES = 64 * 1024
MAX_CHECKPOINT_BYTES = 4096
CHECKPOINT_FINAL_JOIN_SECONDS = 1.0
PROGRESS_PROBE_INTERVAL_SECONDS = 1.0


class TerminalFailureOutcome:
    """Typed terminal callback result that stops an owned process as failure."""

    __slots__ = ('exit_code',)

    def __init__(self, exit_code):
        if type(exit_code) is not int:
            raise TypeError('terminal failure exit_code must be a strict int')
        self.exit_code = exit_code

    def __bool__(self):
        return False


def _checkpoint_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _write_checkpoint_atomically(path, payload):
    """Replace one explicit checkpoint file with bounded metadata."""
    # The isolated -I child bootstrap never writes checkpoints and must remain
    # importable without sibling modules. Only the parent needs persistence.
    import adaptive

    encoded = (json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise ValueError('checkpoint metadata exceeded its size bound')
    adaptive.atomic_write_bytes(path, encoded, mode=0o600)


class _ProcessCheckpoint:
    """Asynchronously persist only bounded process and stream metadata."""

    def __init__(self, path, pid, began):
        self.path = Path(path)
        self.run_correlation = uuid.uuid4().hex
        self.pid = int(pid)
        self.began = began
        self.started_at = _checkpoint_timestamp()
        self.condition = threading.Condition()
        self.byte_counts = {'stdout': 0, 'stderr': 0}
        self.hashes = {'stdout': hashlib.sha256(), 'stderr': hashlib.sha256()}
        self.status = 'running'
        self.exit_code = None
        self.finished_at = None
        self.dirty = True
        self.finished = False
        self.writer_done = False
        self.last_attempt = None
        self.last_attempt_bytes = 0
        self.retry_after = None
        self.write_failures = 0
        self.warning_codes = set()
        self.final_persisted = False
        self.writer_started = False
        self.writer = threading.Thread(
            target=self._write_loop, name='agent-fleet-process-checkpoint', daemon=True
        )

    def start(self):
        try:
            self.writer.start()
            self.writer_started = True
        except Exception:
            with self.condition:
                self.write_failures += 1
                self.warning_codes.add('writer_start_failed')
                self.writer_done = True

    def record(self, stream_name, data):
        with self.condition:
            self.byte_counts[stream_name] += len(data)
            self.hashes[stream_name].update(data)
            self.dirty = True
            self.condition.notify_all()

    def _snapshot_locked(self):
        return {
            'schema': 'agent-fleet.process-checkpoint.v1',
            'run_correlation': self.run_correlation,
            'pid': self.pid,
            'status': self.status,
            'exit_code': self.exit_code,
            'stdout_bytes': self.byte_counts['stdout'],
            'stdout_sha256': self.hashes['stdout'].copy().hexdigest(),
            'stderr_bytes': self.byte_counts['stderr'],
            'stderr_sha256': self.hashes['stderr'].copy().hexdigest(),
            'started_at': self.started_at,
            'updated_at': _checkpoint_timestamp(),
            'finished_at': self.finished_at,
            'elapsed_seconds': round(max(0.0, time.monotonic() - self.began), 3),
        }

    def _write_loop(self):
        while True:
            with self.condition:
                while True:
                    if self.writer_done:
                        return
                    now = time.monotonic()
                    total_bytes = self.byte_counts['stdout'] + self.byte_counts['stderr']
                    if self.dirty and (self.finished or self.last_attempt is None):
                        break
                    if self.dirty and self.retry_after is not None and now < self.retry_after:
                        self.condition.wait(timeout=max(0.01, self.retry_after - now))
                        continue
                    if self.dirty and self.last_attempt is not None:
                        elapsed = now - self.last_attempt
                        received = total_bytes - self.last_attempt_bytes
                        if elapsed >= CHECKPOINT_INTERVAL_SECONDS or received >= CHECKPOINT_INTERVAL_BYTES:
                            break
                        self.condition.wait(timeout=max(0.01, CHECKPOINT_INTERVAL_SECONDS - elapsed))
                        continue
                    if self.finished:
                        self.writer_done = True
                        self.condition.notify_all()
                        return
                    self.condition.wait()

                snapshot = self._snapshot_locked()
                final_snapshot = self.finished
                total_bytes = self.byte_counts['stdout'] + self.byte_counts['stderr']
                self.dirty = False
                self.last_attempt = now
                self.last_attempt_bytes = total_bytes

            try:
                _write_checkpoint_atomically(self.path, snapshot)
            except Exception:
                with self.condition:
                    self.write_failures += 1
                    self.warning_codes.add('write_failed')
                    if final_snapshot:
                        self.writer_done = True
                    else:
                        self.dirty = True
                        self.retry_after = time.monotonic() + CHECKPOINT_INTERVAL_SECONDS
                    self.condition.notify_all()
                if final_snapshot:
                    return
            else:
                with self.condition:
                    self.retry_after = None
                    if final_snapshot:
                        self.final_persisted = True
                        self.writer_done = True
                        self.condition.notify_all()
                        return
                    self.condition.notify_all()

    def finish(self, status, exit_code):
        with self.condition:
            self.status = status
            self.exit_code = int(exit_code)
            self.finished_at = _checkpoint_timestamp()
            self.finished = True
            self.dirty = True
            self.condition.notify_all()

        if self.writer_started:
            self.writer.join(timeout=CHECKPOINT_FINAL_JOIN_SECONDS)
        with self.condition:
            if self.writer_started and self.writer.is_alive():
                self.warning_codes.add('writer_timeout')
            if not self.final_persisted and not self.warning_codes:
                self.warning_codes.add('final_checkpoint_missing')
            return {
                'status': 'ok' if not self.warning_codes and self.final_persisted else 'warning',
                'run_correlation': self.run_correlation,
                'write_failures': self.write_failures,
                'final_persisted': self.final_persisted,
                'warnings': sorted(self.warning_codes),
            }


class ToolEventUnreadable(ValueError):
    """Raised by a tool_events callback for an event it cannot read in full.

    The count becomes unknown and the run only gets a warning. Any other
    callback exception is a parser fault and still fails supervision.
    """


class ContextEventUnreadable(ValueError):
    """Raised by a context_tokens callback for an event it cannot read in full.

    Retains captured output and marks context_scan_incomplete; missing values
    remain unknown. Any other callback exception is a parser fault and fails
    supervision.
    """


def _emergency_job_close(job, handle):
    """Use the same private kernel handle if the regular close path failed."""
    kernel = job._kernel32
    kernel.TerminateJobObject.argtypes = [job._wintypes.HANDLE, job._wintypes.UINT]
    kernel.TerminateJobObject.restype = job._wintypes.BOOL
    if not kernel.TerminateJobObject(handle, 126):
        raise OSError('Private job termination failed')
    if not kernel.CloseHandle(handle):
        raise OSError('Private job fallback close failed')
    job._handle = None


def _child():
    # No CLI can start until the parent contains this bootstrap and sends input.
    request = json.loads(sys.stdin.buffer.readline())
    result = subprocess.run(
        request['command'],
        input=request['stdin_text'],
        stdin=None if request['stdin_text'] is not None else subprocess.DEVNULL,
        text=True,
        encoding='utf-8',
        errors='replace',
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    return result.returncode


def run(
    command,
    *,
    cwd,
    env,
    stdin_text,
    timeout,
    tool_limit=None,
    tool_events=None,
    launch_observer=None,
    tool_count_probe=None,
    context_limit=None,
    context_tokens=None,
    context_fail_closed=False,
    context_oversized_critical=None,
    checkpoint_path=None,
    terminal_completion=None,
    progress_probe=None,
    progress_stall_seconds=None,
):
    if terminal_completion is not None and not callable(terminal_completion):
        raise TypeError('terminal_completion must be callable')
    if (context_limit is None) != (context_tokens is None):
        raise ValueError('context_limit and context_tokens must be configured together')
    if context_limit is not None:
        if type(context_limit) is not int or isinstance(context_limit, bool) or context_limit <= 0:
            raise ValueError(f'context_limit must be a strictly positive int (not bool), got {context_limit!r}')
        if not callable(context_tokens):
            raise TypeError('context_tokens must be callable')
    if context_fail_closed and context_limit is None:
        raise ValueError('context_fail_closed requires context_limit')
    if context_oversized_critical is not None and not callable(context_oversized_critical):
        raise TypeError('context_oversized_critical must be callable')
    if (progress_probe is None) != (progress_stall_seconds is None):
        raise ValueError('progress_probe and progress_stall_seconds must be configured together')
    if progress_probe is not None:
        if not callable(progress_probe):
            raise TypeError('progress_probe must be callable')
        if (not isinstance(progress_stall_seconds, (int, float))
                or isinstance(progress_stall_seconds, bool)
                or progress_stall_seconds <= 0):
            raise ValueError(f'progress_stall_seconds must be a strictly positive int or float (not bool), got {progress_stall_seconds!r}')
    if checkpoint_path is not None:
        checkpoint_path = Path(checkpoint_path)

    began = time.monotonic()
    deadline = began + timeout
    windows_job = None
    process = None
    checkpoint = None
    readers = []
    writer = None
    observer = None
    launch_protection = None
    chunks = {'stdout': [], 'stderr': []}
    events = set()
    limit_hit = threading.Event()
    context_limit_hit = threading.Event()
    context_scan_incomplete = threading.Event()
    context_scan_stopped = threading.Event()
    context_peak = None
    context_lock = threading.Lock()
    io_failed = threading.Event()
    # Tool counting is telemetry plus a lower-bound limit: an event the
    # scanner cannot read makes the count unknown and is only a warning.
    scan_incomplete = threading.Event()
    probe_failed = False
    probe_seen = False
    probe_count = 0
    next_probe = began
    process_contained = False
    timed_out = False
    terminal_seen = threading.Event()
    terminal_seen_at = None
    terminal_failure_exit_code = None
    terminal_scan_incomplete = threading.Event()
    native_exit_before_cleanup = None
    cleanup_errors = []
    progress_stalled = False
    progress_last_observed_seconds = None
    progress_probe_failed = False
    next_progress_probe = began
    try:
        if os.name == 'nt':
            try:
                from grok_acp import _WindowsJob
            except ImportError:
                candidate_dir = str(Path(__file__).resolve().parent)
                if candidate_dir not in sys.path:
                    sys.path.insert(0, candidate_dir)
                from grok_acp import _WindowsJob
            windows_job = _WindowsJob()
        process = subprocess.Popen(
            [sys.executable, '-I', '-u', str(Path(__file__).resolve()), '--child'],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=os.name != 'nt',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if windows_job:
            windows_job.assign(process)
        process_contained = True
        if checkpoint_path is not None:
            checkpoint = _ProcessCheckpoint(checkpoint_path, process.pid, began)
            checkpoint.start()

        def count_tool_events(line):
            try:
                events.update(tool_events(line))
            except ToolEventUnreadable:
                scan_incomplete.set()
            if len(events) > tool_limit:
                limit_hit.set()

        def count_context_tokens(line):
            nonlocal context_peak
            try:
                tokens = context_tokens(line)
            except ContextEventUnreadable:
                context_scan_incomplete.set()
                if context_fail_closed:
                    context_scan_stopped.set()
                return
            except Exception:
                io_failed.set()
                return
            if tokens is None:
                return
            if type(tokens) is not int or isinstance(tokens, bool) or tokens < 0:
                io_failed.set()
                return
            with context_lock:
                if context_peak is None or tokens > context_peak:
                    context_peak = tokens
            if context_limit is not None and tokens > context_limit:
                context_limit_hit.set()

        def mark_oversized_context_line(prefix):
            try:
                critical = (context_oversized_critical(prefix)
                            if context_oversized_critical is not None else True)
            except Exception:
                io_failed.set()
                return
            if critical:
                context_scan_incomplete.set()
                if context_fail_closed:
                    context_scan_stopped.set()

        def read_stream(name, stream):
            nonlocal terminal_seen_at, terminal_failure_exit_code
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            tool_pending = ''
            terminal_pending = ''
            terminal_oversized = False
            context_pending = ''
            context_oversized = False
            try:
                while True:
                    data = stream.read(65536)
                    if not data:
                        break
                    chunks[name].append(data)
                    if checkpoint is not None:
                        checkpoint.record(name, data)
                    if name == 'stdout':
                        text = None
                        if terminal_completion is not None:
                            text = decoder.decode(data)
                            if terminal_oversized:
                                if '\n' in text:
                                    _, terminal_pending = text.split('\n', 1)
                                    terminal_oversized = False
                            else:
                                terminal_pending += text
                            while not terminal_oversized and '\n' in terminal_pending:
                                line, terminal_pending = terminal_pending.split('\n', 1)
                                if len(line) > MAX_CONTEXT_LINE_CHARS:
                                    terminal_scan_incomplete.set()
                                elif not terminal_seen.is_set():
                                    outcome = terminal_completion(line)
                                    if isinstance(outcome, TerminalFailureOutcome):
                                        terminal_failure_exit_code = outcome.exit_code
                                    elif not outcome:
                                        continue
                                    terminal_seen_at = time.monotonic()
                                    terminal_seen.set()
                            if len(terminal_pending) > MAX_CONTEXT_LINE_CHARS:
                                terminal_pending = ''
                                terminal_oversized = True
                                terminal_scan_incomplete.set()
                        if tool_limit is not None and tool_events is not None:
                            if text is None:
                                text = decoder.decode(data)
                            tool_pending += text
                            while '\n' in tool_pending:
                                tline, tool_pending = tool_pending.split('\n', 1)
                                count_tool_events(tline)
                        if context_tokens is not None:
                            if text is None:
                                text = decoder.decode(data)
                            if context_oversized:
                                if '\n' in text:
                                    _, rest = text.split('\n', 1)
                                    context_oversized = False
                                    context_pending = rest
                                else:
                                    context_pending = ''
                            else:
                                context_pending += text

                            while not context_oversized and '\n' in context_pending:
                                cline, context_pending = context_pending.split('\n', 1)
                                if len(cline) > MAX_CONTEXT_LINE_CHARS:
                                    mark_oversized_context_line(cline[:4096])
                                else:
                                    if cline.endswith('\r'):
                                        cline = cline[:-1]
                                    count_context_tokens(cline)

                            if len(context_pending) > MAX_CONTEXT_LINE_CHARS:
                                mark_oversized_context_line(context_pending[:4096])
                                context_oversized = True
                                context_pending = ''
                if name == 'stdout':
                    final_text = decoder.decode(b'', final=True)
                    if tool_limit is not None and tool_events is not None:
                        tool_pending += final_text
                        if tool_pending:
                            count_tool_events(tool_pending)
                    if context_tokens is not None:
                        if not context_oversized:
                            context_pending += final_text
                            if context_pending:
                                if len(context_pending) > MAX_CONTEXT_LINE_CHARS:
                                    mark_oversized_context_line(context_pending[:4096])
                                else:
                                    if context_pending.endswith('\r'):
                                        context_pending = context_pending[:-1]
                                    count_context_tokens(context_pending)
            except Exception:
                io_failed.set()

        def write_request():
            try:
                payload = (json.dumps({'command': command, 'stdin_text': stdin_text}) + '\n').encode()
                view = memoryview(payload)
                while view:
                    size = process.stdin.write(view)
                    if not size:
                        raise BrokenPipeError()
                    view = view[size:]
            except (BrokenPipeError, OSError):
                pass
            finally:
                process.stdin.close()

        for name in ['stdout', 'stderr']:
            thread = threading.Thread(target=read_stream, args=(name, getattr(process, name)), daemon=True)
            thread.start()
            readers.append(thread)
        writer = threading.Thread(target=write_request, daemon=True)
        writer.start()
        if launch_observer is not None:
            def observe_launch():
                nonlocal launch_protection
                try:
                    launch_protection = launch_observer(process)
                except Exception:
                    io_failed.set()
            observer = threading.Thread(target=observe_launch, daemon=True)
            observer.start()
        while True:
            if tool_count_probe is not None and time.monotonic() >= next_probe:
                try:
                    observed = tool_count_probe()
                    if observed is not None:
                        if type(observed) is not int or observed < 0:
                            raise ValueError('invalid tool-count probe result')
                        probe_seen = True
                        probe_count = max(probe_count, observed)
                        if tool_limit is not None and probe_count > tool_limit:
                            limit_hit.set()
                except Exception:
                    probe_failed = True
                    io_failed.set()
                next_probe = time.monotonic() + 0.5
            if progress_probe is not None and not progress_probe_failed and time.monotonic() >= next_progress_probe:
                try:
                    observed_progress = progress_probe()
                    if observed_progress is not None and not (isinstance(observed_progress, (int, float)) and not isinstance(observed_progress, bool) and observed_progress >= 0):
                        progress_probe_failed = True
                    else:
                        progress_last_observed_seconds = observed_progress
                        if observed_progress is not None and observed_progress > progress_stall_seconds:
                            progress_stalled = True
                            timed_out = True
                            break
                except Exception:
                    progress_probe_failed = True
                next_progress_probe = time.monotonic() + PROGRESS_PROBE_INTERVAL_SECONDS
            if (context_limit_hit.is_set() or context_scan_stopped.is_set()
                    or limit_hit.is_set() or io_failed.is_set()):
                break
            # A provider-owned final result ends this invocation; its MCP
            # children may keep stdio alive. Close only our private job, with
            # guard/IO failures taking precedence and raw exit preserved below.
            if terminal_seen.is_set() and time.monotonic() - terminal_seen_at >= 0.2:
                break
            if (process.poll() is not None and all(not t.is_alive() for t in readers)
                    and not writer.is_alive() and (observer is None or not observer.is_alive())):
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.02)
    finally:
        if process is not None:
            native_exit_before_cleanup = process.poll()
        # The private job/session remains authoritative even after the wrapper exits.
        cleanup_deadline = time.monotonic() + CLEANUP_GRACE_SECONDS
        if windows_job:
            handle = windows_job._handle
            try:
                windows_job.close()
            except Exception:
                cleanup_errors.append('job-close-failed')
                try:
                    _emergency_job_close(windows_job, handle)
                except Exception:
                    cleanup_errors.append('job-fallback-failed')
        elif process is not None and process_contained:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                cleanup_errors.append('session-cleanup-failed')
        if process is not None:
            if (not process_contained or cleanup_errors) and process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                cleanup_errors.append('process-not-settled')
            for thread in [*readers, writer, observer]:
                if thread:
                    thread.join(timeout=max(0, cleanup_deadline - time.monotonic()))
            for stream in [process.stdin, process.stdout, process.stderr]:
                if stream and not stream.closed:
                    stream.close()
    if any(t.is_alive() for t in [*readers, writer] if t):
        cleanup_errors.append('stream-not-settled')
    if observer is not None and observer.is_alive():
        cleanup_errors.append('launch-observer-not-settled')
    if tool_count_probe is not None and not probe_failed:
        try:
            observed = tool_count_probe()
            if observed is not None:
                if type(observed) is not int or observed < 0:
                    raise ValueError('invalid tool-count probe result')
                probe_seen = True
                probe_count = max(probe_count, observed)
                if tool_limit is not None and probe_count > tool_limit:
                    limit_hit.set()
        except Exception:
            probe_failed = True
    if tool_count_probe is not None and not probe_seen:
        probe_failed = True
    if probe_failed and not limit_hit.is_set():
        scan_incomplete.set()
        io_failed.set()
    supervision_failed = bool(cleanup_errors) or io_failed.is_set()
    stdout = b''.join(chunks['stdout']).decode('utf-8', errors='replace').replace('\r\n', '\n')
    stderr = b''.join(chunks['stderr']).decode('utf-8', errors='replace').replace('\r\n', '\n')
    if supervision_failed:
        stderr += '\nAgent Fleet supervision failed; output is incomplete.'
    if scan_incomplete.is_set():
        stderr += '\nAgent Fleet could not read every model tool event; tool count is unknown.'
    if context_scan_incomplete.is_set():
        stderr += '\nAgent Fleet could not read every model context event; context token count is incomplete.'
    if probe_failed:
        if limit_hit.is_set():
            stderr += '\nAgent Fleet final OpenClaw transcript probe failed after the tool limit was already confirmed.'
        else:
            stderr += '\nAgent Fleet OpenClaw transcript tool-budget probe failed or was unavailable.'
    if progress_probe_failed:
        stderr += '\nAgent Fleet native progress probe failed; stall detection was unavailable.'
    if context_limit_hit.is_set():
        stderr += f'\nExecutor stopped after observed context tokens {context_peak}; limit={context_limit}.'
    elif context_scan_stopped.is_set():
        stderr += '\nExecutor stopped because context usage could not be measured safely.'
    elif limit_hit.is_set():
        counted = probe_count if tool_count_probe is not None else len(events)
        stderr += f'\nExecutor stopped after {counted} model tool calls; limit={tool_limit}.'
    elif timed_out:
        if progress_stalled:
            observed = progress_last_observed_seconds if progress_last_observed_seconds is not None else 0
            stderr += f'\nAgent Fleet stopped the provider after {observed:.0f} seconds without native progress (limit={progress_stall_seconds} seconds).'
        else:
            stderr += f'\nAgent Fleet timeout after {timeout} seconds (including stream drain).'
    if tool_count_probe is not None:
        model_tool_calls = probe_count if probe_seen and (not probe_failed or limit_hit.is_set()) else None
    else:
        model_tool_calls = (len(events) if tool_limit is not None
            and (limit_hit.is_set() or not scan_incomplete.is_set()) else None)
    exit_code = (
        126 if supervision_failed
        else 127 if context_limit_hit.is_set() or context_scan_stopped.is_set()
        else 125 if limit_hit.is_set()
        else 124 if timed_out
        else native_exit_before_cleanup
        if native_exit_before_cleanup not in (None, 0)
        else terminal_failure_exit_code
        if terminal_failure_exit_code is not None
        else 0 if terminal_seen.is_set()
        else process.returncode
    )
    checkpoint_result = None
    if checkpoint is not None:
        if supervision_failed:
            checkpoint_status = 'supervision_failed'
        elif context_limit_hit.is_set():
            checkpoint_status = 'context_limit_exceeded'
        elif context_scan_stopped.is_set():
            checkpoint_status = 'context_scan_stopped'
        elif limit_hit.is_set():
            checkpoint_status = 'tool_limit_exceeded'
        elif progress_stalled:
            checkpoint_status = 'stalled'
        elif timed_out:
            checkpoint_status = 'timed_out'
        elif exit_code == 0:
            checkpoint_status = 'completed'
        else:
            checkpoint_status = 'process_failed'
        checkpoint_result = checkpoint.finish(checkpoint_status, exit_code)
    result = {
        'exit_code': exit_code,
        'native_process_exit_code': process.returncode,
        'native_exit_before_cleanup': native_exit_before_cleanup,
        'protocol_terminal_completed': terminal_seen.is_set(),
        'terminal_scan_incomplete': terminal_scan_incomplete.is_set(),
        'stdout': stdout,
        'stderr': stderr,
        'timed_out': timed_out,
        # A count past the limit is enforcement evidence even as a lower
        # bound; otherwise an incomplete scan is an unknown count.
        'model_tool_calls': model_tool_calls,
        'model_tool_scan_incomplete': scan_incomplete.is_set(),
        'tool_budget_exceeded': limit_hit.is_set(),
        'context_limit_tokens': context_limit,
        'context_peak_tokens': context_peak,
        'context_scan_incomplete': context_scan_incomplete.is_set(),
        'context_scan_stopped': context_scan_stopped.is_set(),
        'context_budget_exceeded': context_limit_hit.is_set(),
        'supervision_failed': supervision_failed,
        'cleanup_errors': cleanup_errors,
        'execution_seconds': round(time.monotonic() - began, 6),
        'launch_protection': launch_protection,
        'progress_stalled': progress_stalled,
        'progress_stall_seconds': progress_stall_seconds,
        'progress_last_observed_seconds': progress_last_observed_seconds,
        'progress_probe_failed': progress_probe_failed,
    }
    if checkpoint_result is not None:
        result['process_checkpoint'] = checkpoint_result
    return result


if __name__ == '__main__':
    if sys.argv[1:] != ['--child']:
        raise SystemExit('Internal bootstrap only')
    raise SystemExit(_child())
