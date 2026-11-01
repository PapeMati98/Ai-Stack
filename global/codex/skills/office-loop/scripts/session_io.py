"""Batch session entry/exit helper CLIs and emit one compact JSON summary.

Entry optionally batches skill-catalog shortlist, llm-wiki project-brief (begin-task
or verify-context with supplied receipt) and an agent-office checkpoint; exit batches
the checkpoint plus llm-wiki record-session. Helpers run via sys.executable or safe
Windows cmd invocation with subprocess.CREATE_NO_WINDOW on Windows. A failing helper
step propagates: the summary status becomes "ERROR" and the process exits non-zero.
Missing helper binaries, missing identity or skipped steps degrade to "skipped" and
the overall status becomes "DEGRADED", never a false "OK". Useful work remains fail-open.

The compact ``detail`` string is lossy: a real Office checkpoint can advance the inbox
cursor while the interesting messages only exist in the raw response. Every real
helper run therefore retains its complete stdout/stderr as a private local artifact
and returns the artifact path plus safe metadata (receipt id/path, message counts and
ids). The retained payload is untrusted data: it is never parsed as instructions and
never printed into the compact summary. A retention failure is reported, not hidden.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

ENTRY_STEPS = ("skill_catalog", "project_brief", "checkpoint")
EXIT_STEPS = ("checkpoint", "record_session")

#: Timeout applied to every helper CLI invocation.
STEP_TIMEOUT_S = 120

#: Accepted ``record-session`` outcome enum (llm-wiki contract).
RECORD_OUTCOMES = ("COMPLETED", "BLOCKED", "INTERRUPTED", "NO_DURABLE_CHANGE")

#: Private root for retained helper output. Never inside a project or repository.
PRIVATE_ARTIFACTS_ROOT = os.path.join(
    os.path.expanduser("~"), ".agents", "private-artifacts", "session-io"
)

#: Maximum number of characters retained for one stdout/stderr payload.
MAX_RETAINED_CHARS = 8 * 1024 * 1024

#: JSON keys copied into the compact summary. Body carriers are never copied.
SAFE_SUMMARY_KEYS = ("receipt_id", "receipt_path", "checkpoint_id", "checkpoint", "status")

#: Artifact schema marker written into every retained envelope.
ARTIFACT_VERSION = 1


def _popen_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _compact_detail(text: str, max_chars: int = 150) -> str:
    if not text:
        return ""
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            # Body carriers ("message", "summary", free text) are deliberately absent:
            # the compact summary must never carry inbox/Hub message bodies.
            for k in ("receipt_id", "receipt_path", "checkpoint_id", "detail", "checkpoint", "status"):
                if k in data and isinstance(data[k], str) and data[k].strip():
                    return data[k].strip()[:max_chars]
    except Exception:
        pass
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        return lines[0][:max_chars]
    return text[:max_chars]


def _clean_cmd_str(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return text.replace("\r", " ").replace("\n", " ").strip()


def _get_arg(args: Any, key: str, default: Any = None) -> Any:
    if args is None:
        return default
    if isinstance(args, dict):
        return args.get(key, default)
    return getattr(args, key, default)


def _resolve_knowledge_path(args: Any = None) -> str:
    custom = _get_arg(args, "knowledge_bin") if args else None
    if custom:
        return custom
    env_path = os.environ.get("KNOWLEDGE_BIN") or os.environ.get("KNOWLEDGE_PATH")
    if env_path:
        return env_path
    cfg_path = os.path.expanduser("~/.agents/knowledge-config.json")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            canon = data.get("canonical_skill_path")
            if canon:
                canon = os.path.expanduser(canon)
                candidate = canon if canon.endswith("knowledge.py") else os.path.join(canon, "scripts", "knowledge.py")
                return os.path.normpath(candidate)
        except Exception:
            pass
    return os.path.normpath(os.path.expanduser("~/.agents/skills/llm-wiki/scripts/knowledge.py"))


def _resolve_skill_catalog_path(args: Any = None) -> str:
    custom = _get_arg(args, "skill_catalog_bin") if args else None
    if custom:
        return custom
    env_path = os.environ.get("SKILL_CATALOG_BIN") or os.environ.get("SKILL_CATALOG_PATH")
    if env_path:
        return env_path
    return os.path.normpath(os.path.expanduser("~/.agents/skills/select-skills/scripts/skill_catalog.py"))


def _resolve_office_path(args: Any = None) -> str:
    custom = _get_arg(args, "office_bin") if args else None
    if custom:
        return custom
    env_path = os.environ.get("AGENT_OFFICE_BIN") or os.environ.get("OFFICE_BIN")
    if env_path:
        return env_path
    if sys.platform == "win32":
        return os.path.normpath(os.path.expanduser("~/.agents/runtime/agent-office/office.cmd"))
    cmd_candidate = os.path.normpath(os.path.expanduser("~/.agents/runtime/agent-office/office.cmd"))
    if os.path.isfile(cmd_candidate):
        return cmd_candidate
    return os.path.normpath(os.path.expanduser("~/.agents/runtime/agent-office/office"))


def _resolve_agent(args: Any) -> str | None:
    return (
        _get_arg(args, "agent")
        or _get_arg(args, "harness")
        or os.environ.get("COORDFS_HARNESS")
        or os.environ.get("AGENT_HARNESS")
        or os.environ.get("AGENT_ID")
    )


def _resolve_harness(args: Any) -> str | None:
    return (
        _get_arg(args, "harness")
        or _get_arg(args, "agent")
        or os.environ.get("COORDFS_HARNESS")
        or os.environ.get("AGENT_HARNESS")
        or os.environ.get("AGENT_ID")
    )


def _resolve_session_id(args: Any) -> str | None:
    return (
        _get_arg(args, "session_id")
        or _get_arg(args, "session")
        or os.environ.get("COORDFS_SESSION_ID")
        or os.environ.get("AGENT_SESSION_ID")
        or os.environ.get("SESSION_ID")
    )


def _sanitize_component(value: Any, max_len: int = 80) -> str:
    """Turn an identity fragment into a safe single path component."""
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._")
    return (text or "unknown")[:max_len]


def _is_within(path: str, parent: str) -> bool:
    try:
        path_abs = os.path.abspath(path)
        parent_abs = os.path.abspath(parent)
    except Exception:
        return False
    if path_abs == parent_abs:
        return True
    return path_abs.startswith(parent_abs + os.sep)


def _resolve_output_dir(args: Any = None) -> str:
    """Resolve the private directory that holds retained helper output.

    An explicit ``--output-dir`` wins (task-owned private output). The default is
    a private path under the user home keyed by the real session identity, never
    inside a project or repository.
    """
    custom = _get_arg(args, "output_dir") if args else None
    if custom:
        return os.path.abspath(os.path.expanduser(str(custom)))

    root = os.path.normpath(os.path.expanduser(PRIVATE_ARTIFACTS_ROOT))
    harness = _resolve_harness(args) or "unknown-harness"
    session_id = _resolve_session_id(args) or "unknown-session"
    target = os.path.join(root, _sanitize_component(harness), _sanitize_component(session_id))
    # Guard: the default private path must never land inside the current project.
    cwd = os.getcwd()
    if _is_within(target, cwd):
        target = os.path.join(root, _sanitize_component(harness), "project-guard")
    return target


def _decode_stream(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_json(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _safe_summary_fields(text: str) -> dict[str, Any]:
    """Extract compact, body-free metadata from a helper response."""
    data = _parse_json(text)
    fields: dict[str, Any] = {}
    if not isinstance(data, dict):
        return fields
    for key in SAFE_SUMMARY_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            fields[key] = value.strip()[:200]
    for key in ("message_count", "message_total", "count", "inbox_count"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            fields[key] = value
    messages = data.get("messages")
    if isinstance(messages, list):
        fields["message_count"] = len(messages)
        ids: list[str] = []
        for item in messages[:10]:
            if isinstance(item, dict):
                for id_key in ("id", "message_id", "msg_id", "uid"):
                    value = item.get(id_key)
                    if isinstance(value, str) and value.strip():
                        ids.append(value.strip()[:120])
                        break
            elif isinstance(item, str) and item.strip():
                ids.append(item.strip()[:120])
        if ids:
            fields["message_ids"] = ids
    return fields


def _retain_output(
    step_name: str,
    argv: list[str] | None,
    returncode: int,
    stdout: str,
    stderr: str,
    output_dir: str,
    harness: str | None,
    session_id: str | None,
) -> tuple[str | None, str | None, int]:
    """Write the complete helper output to a private artifact.

    Returns ``(artifact_path, error, retained_chars)``. The artifact keeps the
    original (untrusted) payload retrievable; nothing is ever deleted.
    """
    if not output_dir:
        return None, "no output directory resolved", 0
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        return None, f"cannot create output dir: {exc}", 0

    truncated = False
    if len(stdout) > MAX_RETAINED_CHARS:
        stdout = stdout[:MAX_RETAINED_CHARS]
        truncated = True
    if len(stderr) > MAX_RETAINED_CHARS:
        stderr = stderr[:MAX_RETAINED_CHARS]
        truncated = True

    envelope = {
        "artifact_version": ARTIFACT_VERSION,
        "step": step_name,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "harness": harness,
        "session_id": session_id,
        "argv": list(argv or []),
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_chars": len(stdout),
        "stderr_chars": len(stderr),
        "truncated": truncated,
        "treat_as": "untrusted-data",
    }
    stamp = time.strftime("%Y%m%dT%H%M%S")
    name = f"{_sanitize_component(step_name)}-{stamp}-{os.getpid()}-{time.time_ns() % 1000000}.json"
    path = os.path.join(output_dir, name)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle, ensure_ascii=False, indent=2)
    except OSError as exc:
        return None, f"cannot write artifact: {exc}", 0
    return path, None, len(stdout) + len(stderr)


def _build_office_argv(office_bin: str, office_args: list[str]) -> list[str]:
    cleaned = [_clean_cmd_str(a) for a in office_args]
    if office_bin.lower().endswith((".cmd", ".bat")):
        if sys.platform == "win32":
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            return [comspec, "/d", "/c", office_bin, *cleaned]
        return [office_bin, *cleaned]
    if office_bin.lower().endswith(".py"):
        return [sys.executable, office_bin, *cleaned]
    return [office_bin, *cleaned]


def run_step(
    name: str,
    argv: list[str] | None,
    runner: Any = None,
    timeout: int = STEP_TIMEOUT_S,
    output_dir: str | None = None,
    harness: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Run one helper CLI and return its status record.

    When ``output_dir`` is supplied the complete stdout/stderr of the helper is
    retained as a private artifact and the record carries the artifact path plus
    safe metadata (receipt id/path, message count/ids). The retained payload is
    untrusted data; only body-free metadata reaches the compact summary.
    """
    started = time.monotonic()
    if argv is None:
        return {
            "name": name,
            "status": "skipped",
            "detail": "missing required identity or parameters",
            "elapsed_ms": 0,
        }

    # If no custom mock runner is provided, verify binary/script exists before executing
    if runner is None:
        target_path = None
        if len(argv) >= 2 and argv[0] == sys.executable:
            target_path = argv[1]
        elif len(argv) >= 3 and argv[0].lower().endswith("cmd.exe"):
            if argv[1] == "/c":
                target_path = argv[2]
            elif len(argv) >= 4 and argv[2] == "/c":
                target_path = argv[3]
        elif len(argv) >= 1:
            target_path = argv[0]

        if target_path:
            is_installed = os.path.isfile(target_path) or (shutil.which(target_path) is not None)
            if not is_installed:
                return {
                    "name": name,
                    "status": "skipped",
                    "detail": f"helper not installed: {os.path.basename(target_path)}",
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }

    timed_out = False
    stdout = ""
    stderr = ""
    returncode = 0
    try:
        completed = (runner or subprocess.run)(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_popen_kwargs(),
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        returncode = completed.returncode
    except FileNotFoundError as exc:
        return {
            "name": name,
            "status": "skipped",
            "detail": f"helper not installed: {exc.filename or argv[0]}",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = _decode_stream(exc.stdout)
        stderr = _decode_stream(exc.stderr)
        returncode = -1

    elapsed_ms = int((time.monotonic() - started) * 1000)

    # Retain the complete (untrusted) payload before reducing it to a summary.
    output_path: str | None = None
    retain_error: str | None = None
    retained_chars = 0
    if output_dir:
        output_path, retain_error, retained_chars = _retain_output(
            name, argv, returncode, stdout, stderr, output_dir, harness, session_id
        )

    # Receipt ids/paths survive success and failure alike.
    safe = _safe_summary_fields(stderr)
    safe.update(_safe_summary_fields(stdout))

    if timed_out:
        record: dict[str, Any] = {
            "name": name,
            "status": "error",
            "detail": f"timed out after {timeout}s",
            "elapsed_ms": elapsed_ms,
        }
    elif returncode == 0:
        record = {
            "name": name,
            "status": "ok",
            "detail": _compact_detail(stdout) or "ok",
            "elapsed_ms": elapsed_ms,
            "rc": returncode,
        }
    elif returncode == 3 and "MISSING" in stderr:
        record = {
            "name": name,
            "status": "skipped",
            "detail": "helper reported MISSING",
            "elapsed_ms": elapsed_ms,
            "rc": returncode,
        }
    else:
        record = {
            "name": name,
            "status": "error",
            "detail": _compact_detail(stderr or stdout or f"exit {returncode}"),
            "elapsed_ms": elapsed_ms,
            "rc": returncode,
        }

    if output_path:
        record["output_path"] = output_path
        record["output_bytes"] = retained_chars
    elif retain_error:
        record["output_retained"] = False
        record["retention_error"] = retain_error
    if safe:
        record["result"] = safe
        for key in ("receipt_id", "receipt_path"):
            if key in safe:
                record[key] = safe[key]

    if retain_error:
        # Never report a consumed response as a clean OK when it was not retained.
        record["status"] = "error"
        record["detail"] = f"output retention failed: {retain_error}"
        record.setdefault("step_detail", _compact_detail(stdout) or _compact_detail(stderr))

    return record


def default_steps(command: str, args: Any) -> list[tuple[str, list[str] | None]]:
    """Build ``(name, argv)`` steps using the in-repo helper CLI contract."""
    steps = []
    if command == "entry":
        task = _get_arg(args, "task")
        if task:
            skill_cat_bin = _resolve_skill_catalog_path(args)
            steps.append(("skill_catalog", [sys.executable, skill_cat_bin, "--task", str(task), "--compact"]))

        project_slug = _get_arg(args, "project_slug") or _get_arg(args, "project")
        if project_slug:
            knowledge_bin = _resolve_knowledge_path(args)
            agent = _resolve_agent(args)
            session_id = _resolve_session_id(args)
            receipt = _get_arg(args, "context_receipt")
            if not agent or not session_id:
                steps.append(("project_brief", None))
            elif receipt:
                steps.append((
                    "project_brief",
                    [
                        sys.executable,
                        knowledge_bin,
                        "verify-context",
                        "--context-receipt",
                        str(receipt),
                        "--agent",
                        str(agent),
                        "--session-id",
                        str(session_id),
                        "--project-slug",
                        str(project_slug),
                    ],
                ))
            else:
                steps.append((
                    "project_brief",
                    [
                        sys.executable,
                        knowledge_bin,
                        "begin-task",
                        "--agent",
                        str(agent),
                        "--session-id",
                        str(session_id),
                        "--project-slug",
                        str(project_slug),
                        "--require-context",
                    ],
                ))

        office_bin = _resolve_office_path(args)
        harness = _resolve_harness(args)
        session_id = _resolve_session_id(args)
        if not harness or not session_id:
            steps.append(("checkpoint", None))
        else:
            channel = _get_arg(args, "channel") or os.environ.get("OFFICE_CHANNEL") or os.environ.get("COORDFS_CHANNEL") or "general"
            workspace = _get_arg(args, "workspace") or os.getcwd()
            ttl = str(_get_arg(args, "ttl") or "900")
            office_args = [
                "--harness",
                harness,
                "--session-id",
                session_id,
                "checkpoint",
                "--channel",
                channel,
                "--workspace",
                workspace,
                "--ttl",
                ttl,
            ]
            steps.append(("checkpoint", _build_office_argv(office_bin, office_args)))
        return steps

    if command == "exit":
        office_bin = _resolve_office_path(args)
        harness = _resolve_harness(args)
        session_id = _resolve_session_id(args)
        if not harness or not session_id:
            steps.append(("checkpoint", None))
        else:
            channel = _get_arg(args, "channel") or os.environ.get("OFFICE_CHANNEL") or os.environ.get("COORDFS_CHANNEL") or "general"
            workspace = _get_arg(args, "workspace") or os.getcwd()
            ttl = str(_get_arg(args, "ttl") or "900")
            office_args = [
                "--harness",
                harness,
                "--session-id",
                session_id,
                "checkpoint",
                "--channel",
                channel,
                "--workspace",
                workspace,
                "--ttl",
                ttl,
            ]
            steps.append(("checkpoint", _build_office_argv(office_bin, office_args)))

        knowledge_bin = _resolve_knowledge_path(args)
        agent = _resolve_agent(args)
        outcome = _get_arg(args, "outcome")
        if not agent or not session_id or not outcome:
            steps.append(("record_session", None))
        else:
            checkpoint_id = _get_arg(args, "checkpoint_id") or _get_arg(args, "title") or f"closeout-{session_id}"
            record_argv = [
                sys.executable,
                knowledge_bin,
                "record-session",
                "--agent",
                str(agent),
                "--session-id",
                str(session_id),
                "--checkpoint-id",
                str(checkpoint_id),
                "--outcome",
                str(outcome),
            ]
            project_slug = _get_arg(args, "project_slug") or _get_arg(args, "project")
            if project_slug:
                record_argv += ["--project-slug", str(project_slug)]
            summary = _get_arg(args, "summary") or _get_arg(args, "notes")
            if summary:
                record_argv += ["--summary", str(summary)]
            receipt = _get_arg(args, "context_receipt")
            if receipt:
                record_argv += ["--context-receipt", str(receipt)]
            steps.append(("record_session", record_argv))
        return steps

    raise ValueError(f"unknown command: {command}")


def execute(command: str, args: Any, runner: Any = None) -> dict[str, Any]:
    """Run all steps for ``command`` and return the compact summary dict.

    Real helper runs retain their complete output as a private artifact so a
    consumed response (for example a cursor-advancing Office checkpoint) stays
    retrievable. An injected in-process runner is treated as a test seam unless
    an explicit output directory was requested.
    """
    started = time.monotonic()
    steps = default_steps(command, args)
    harness = _resolve_harness(args)
    session_id = _resolve_session_id(args)
    output_dir = _resolve_output_dir(args)
    explicit_output_dir = bool(_get_arg(args, "output_dir"))
    retain = runner is None or explicit_output_dir
    results = [
        run_step(
            name,
            argv,
            runner=runner,
            output_dir=output_dir if retain else None,
            harness=harness,
            session_id=session_id,
        )
        for name, argv in steps
    ]

    if any(item["status"] == "error" for item in results):
        status = "ERROR"
    elif any(item["status"] == "skipped" for item in results) or not results:
        status = "DEGRADED"
    else:
        status = "OK"

    summary = {
        "status": status,
        "command": command,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "steps": results,
    }
    if retain and output_dir:
        summary["output_dir"] = output_dir
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="session_io", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    entry = sub.add_parser("entry", help="Batch session-entry helper CLIs.")
    entry.add_argument("--task", default=None, help="Routing text for skill-catalog shortlist.")
    entry.add_argument("--project-slug", "--project", dest="project_slug", default=None, help="Slug for llm-wiki context.")
    entry.add_argument("--context-receipt", default=None, help="Existing context receipt to verify and reuse.")
    entry.add_argument("--goal", default=None, help="Office goal (accepted for compatibility; not forwarded to checkpoint).")
    entry.add_argument("--harness", default=None, help="Harness name for Office.")
    entry.add_argument("--agent", default=None, help="Agent identity for Knowledge / Office.")
    entry.add_argument("--session-id", "--session", dest="session_id", default=None, help="Native session ID.")
    entry.add_argument("--channel", default=None, help="Office channel.")
    entry.add_argument("--workspace", default=None, help="Workspace path.")
    entry.add_argument("--ttl", default="900", help="Presence lease TTL seconds.")
    entry.add_argument(
        "--output-dir",
        default=None,
        help="Optional private directory for retained helper output (default: ~/.agents/private-artifacts/session-io/<harness>/<session-id>).",
    )
    entry.add_argument("--skill-catalog-bin", default=None, help=argparse.SUPPRESS)
    entry.add_argument("--knowledge-bin", default=None, help=argparse.SUPPRESS)
    entry.add_argument("--office-bin", default=None, help=argparse.SUPPRESS)

    done = sub.add_parser("exit", help="Batch session-exit helper CLIs.")
    done.add_argument("--outcome", default=None, choices=RECORD_OUTCOMES, help="Record-session outcome enum.")
    done.add_argument("--project-slug", "--project", dest="project_slug", default=None, help="Slug for llm-wiki record-session.")
    done.add_argument("--title", default=None, help="Title / checkpoint ID for record-session.")
    done.add_argument("--checkpoint-id", default=None, help="Explicit checkpoint ID.")
    done.add_argument("--summary", default=None, help="Summary forwarded to record-session.")
    done.add_argument("--notes", default=None, help="Notes forwarded to record-session summary.")
    done.add_argument("--context-receipt", default=None, help="Context receipt forwarded to record-session.")
    done.add_argument("--goal", default=None, help="Office goal (accepted for compatibility; not forwarded to checkpoint).")
    done.add_argument("--harness", default=None, help="Harness name for Office.")
    done.add_argument("--agent", default=None, help="Agent identity for Knowledge / Office.")
    done.add_argument("--session-id", "--session", dest="session_id", default=None, help="Native session ID.")
    done.add_argument("--channel", default=None, help="Office channel.")
    done.add_argument("--workspace", default=None, help="Workspace path.")
    done.add_argument("--ttl", default="900", help="Presence lease TTL seconds.")
    done.add_argument(
        "--output-dir",
        default=None,
        help="Optional private directory for retained helper output (default: ~/.agents/private-artifacts/session-io/<harness>/<session-id>).",
    )
    done.add_argument("--knowledge-bin", default=None, help=argparse.SUPPRESS)
    done.add_argument("--office-bin", default=None, help=argparse.SUPPRESS)

    return parser


def main(argv: list[str] | None = None, runner: Any = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = execute(args.command, args, runner=runner)
    except ValueError as exc:
        print(json.dumps({"status": "ERROR", "detail": str(exc)}))
        return 2
    print(json.dumps(summary))
    # Useful work remains fail-open: only ERROR exits non-zero
    return 0 if summary["status"] in ("OK", "DEGRADED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
