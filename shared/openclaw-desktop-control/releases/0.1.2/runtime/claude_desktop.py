"""Claude Desktop / Claude Code CLI adapter for OpenClaw.

Transport is ``claude --resume <session-id> --print <prompt>``.
The subprocess is injected so unit tests never spawn a real CLI.

Named limits of this path: CLI resume is not Desktop computer-use, and a
CLI session id is not proven to be a Desktop UI ``local_`` identifier.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence

from openclaw_ops import models
from openclaw_ops.adapters import Adapter, ProviderStatus, ResumeResult, StaleFenceError

CAUSE_TIMEOUT = "timeout"
CAUSE_NONZERO_EXIT = "nonzero-exit"
CAUSE_UNRECOGNIZED_SESSION = "unrecognized-session"

UNRECOGNIZED_MARKERS = (
    "no conversation found",
    "session not found",
    "unknown session",
    "could not find session",
    "invalid session",
    "unrecognized session",
    "no such session",
    "session does not exist",
    "unable to resume",
    "cannot resume",
    "resume failed",
    "not a valid session",
)

log = logging.getLogger("openclaw.claude_desktop")


@dataclass
class TransportResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class ClaudeCliTransport:
    def run(self, argv: Sequence[str], timeout: float) -> TransportResult:
        try:
            completed = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            return TransportResult(
                returncode=-1, stdout=stdout, stderr=stderr, timed_out=True
            )
        return TransportResult(
            returncode=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            timed_out=False,
        )


def looks_unrecognized_session(stdout: str, stderr: str) -> bool:
    blob = f"{stdout}\n{stderr}".lower()
    return any(marker in blob for marker in UNRECOGNIZED_MARKERS)


def classify_failure(result: TransportResult) -> str:
    if result.timed_out:
        return CAUSE_TIMEOUT
    if looks_unrecognized_session(result.stdout, result.stderr):
        return CAUSE_UNRECOGNIZED_SESSION
    if result.returncode != 0:
        return CAUSE_NONZERO_EXIT
    return ""


def build_resume_argv(session_id: str, prompt: str, cli: str = "claude") -> List[str]:
    return [cli, "--resume", session_id, "--print", prompt]


class ClaudeDesktopAdapter(Adapter):
    name = "claude-desktop"
    supports_idempotent_lookup = True
    supports_fencing = True

    def __init__(
        self,
        transport: Optional[Any] = None,
        *,
        cli: str = "claude",
        timeout: float = 120.0,
        clock: Optional[Callable[[], float]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.transport = transport if transport is not None else ClaudeCliTransport()
        self.cli = cli
        self.timeout = float(timeout)
        self._clock = clock or time.time
        self._log = logger or log
        self.runs: dict[str, str] = {}
        self.max_token: dict[str, int] = {}
        self.max_generation: dict[str, int] = {}
        self._last_status: dict[str, ProviderStatus] = {}
        self._log_records: List[str] = []

    def _now(self) -> float:
        return float(self._clock())

    def _safe_log(self, message: str, *args: object) -> None:
        text = message % args if args else message
        self._log_records.append(text)
        self._log.info(text)

    def get_status(self, task: dict) -> Optional[ProviderStatus]:
        session_id = str(task.get("native_thread") or task.get("session_id") or "")
        if session_id and session_id in self._last_status:
            return self._last_status[session_id]
        now = self._now()
        return ProviderStatus(
            state=models.STATE_UNKNOWN,
            observed_wall=now,
            provider_wall=now,
            native_run_id=None,
            failure_cause=None,
            cause_transient=False,
        )

    def lookup(self, idempotency_key: str) -> Optional[str]:
        return self.runs.get(idempotency_key)

    def resume(
        self, task: dict, idempotency_key: str, fencing_token: int
    ) -> ResumeResult:
        if idempotency_key in self.runs:
            return ResumeResult(self.runs[idempotency_key], "acked")
        gen = int(task.get("generation") or 0)
        task_id = str(task.get("task_id") or "")
        if gen < self.max_generation.get(task_id, 0):
            raise StaleFenceError(f"old generation {gen} for task {task_id}")
        self.max_generation[task_id] = gen
        scope = f"{task_id}:{gen}"
        seen = self.max_token.get(scope, 0)
        if fencing_token <= seen:
            raise StaleFenceError(
                f"stale fencing token {fencing_token} <= {seen} for {scope}"
            )
        self.max_token[scope] = fencing_token
        session_id = str(task.get("native_thread") or task.get("session_id") or "")
        prompt = str(task.get("prompt") or task.get("raw_text") or "")
        argv = build_resume_argv(session_id, prompt, cli=self.cli)
        self._safe_log(
            "claude-desktop resume session=%s timeout=%s", session_id, self.timeout
        )
        result = self.transport.run(argv, self.timeout)
        cause = classify_failure(result)
        now = self._now()
        if cause:
            status = ProviderStatus(
                state=models.STATE_TERMINAL_FAILED,
                observed_wall=now,
                provider_wall=now,
                native_run_id=None,
                failure_cause=cause,
                cause_transient=cause == CAUSE_TIMEOUT,
            )
            if session_id:
                self._last_status[session_id] = status
            self._safe_log(
                "claude-desktop resume failed session=%s cause=%s exit=%s",
                session_id,
                cause,
                result.returncode,
            )
            return ResumeResult("", "unknown")
        run_id = f"{idempotency_key}:claude"
        self.runs[idempotency_key] = run_id
        status = ProviderStatus(
            state=models.STATE_TERMINAL_COMPLETED,
            observed_wall=now,
            provider_wall=now,
            native_run_id=run_id,
            failure_cause=None,
            cause_transient=False,
        )
        if session_id:
            self._last_status[session_id] = status
        self._safe_log("claude-desktop resume acked session=%s", session_id)
        return ResumeResult(run_id, "acked")
