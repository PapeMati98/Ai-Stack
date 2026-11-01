from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SESSION_ID = "11111111-1111-4111-8111-111111111111"


def send(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def response(request_id: Any, result: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def update(value: dict[str, Any]) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": SESSION_ID, "update": value},
        }
    )


def handoff(*, partial: bool = False) -> dict[str, Any]:
    return {
        "status": "partial" if partial else "completed",
        "summary": (
            "Inspected the bounded fixture; one verification remains."
            if partial
            else "Inspected the bounded fixture and verified the requested result."
        ),
        "files_changed": [],
        "verification": ["fixture.txt inspected"],
        "risks": [],
        "next_steps": ["Finish the verification"] if partial else [],
    }


def terminal(
    request_id: Any,
    *,
    partial: bool = False,
    prompt_count: int = 1,
    model_id: str = "grok-4.7",
) -> None:
    value = handoff(partial=partial)
    update(
        {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "PRIVATE_THOUGHT_MUST_NOT_PERSIST"},
        }
    )
    update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": f"read-{prompt_count}",
            "title": "Read fixture",
            "kind": "read",
            "status": "completed",
            "tool": "read_file",
        }
    )
    update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": json.dumps(value)},
        }
    )
    response(
        request_id,
        {
            "stopReason": "end_turn",
            "structuredOutput": value,
            "_meta": {
                "modelId": model_id,
                "usage": {
                    "inputTokens": 40 * prompt_count,
                    "outputTokens": 10 * prompt_count,
                    "totalTokens": 50 * prompt_count,
                },
            },
        },
    )


def main() -> int:
    scenario = os.environ.get("FAKE_GROK_ACP_SCENARIO", "complete")
    prompt_count = 0
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            response(
                request_id,
                {
                    "protocolVersion": "1",
                    "authMethods": [{"id": "cached_token"}],
                    "agentCapabilities": {},
                },
            )
        elif method == "authenticate":
            response(request_id, {})
        elif method == "session/new":
            servers = [{"name": "ambient-danger"}] if scenario == "ambient-mcp" else []
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "_x.ai/notification",
                    "params": {
                        "method": "x.ai/mcp/servers_updated",
                        "params": {"mcpServers": servers},
                    },
                }
            )
            response(request_id, {"sessionId": SESSION_ID})
        elif method == "session/prompt":
            prompt_count += 1
            if scenario == "timeout":
                time.sleep(30)
            elif scenario == "timeout-child":
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                pid_path = os.environ.get("FAKE_GROK_CHILD_PID_PATH")
                if pid_path:
                    with open(pid_path, "w", encoding="ascii") as handle:
                        handle.write(str(child.pid))
                time.sleep(30)
            elif scenario == "orphan-child":
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                pid_path = os.environ.get("FAKE_GROK_CHILD_PID_PATH")
                if pid_path:
                    with open(pid_path, "w", encoding="ascii") as handle:
                        handle.write(str(child.pid))
                terminal(request_id, prompt_count=prompt_count)
                return 0
            elif scenario == "tool-budget":
                for index in range(3):
                    update(
                        {
                            "sessionUpdate": "tool_call",
                            "toolCallId": f"tool-{index}",
                            "title": f"Tool {index}",
                            "kind": "read",
                            "status": "completed",
                            "tool": "read_file",
                        }
                    )
                time.sleep(30)
            elif scenario == "permission":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 900,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": SESSION_ID,
                            "toolCall": {"toolCallId": "danger"},
                            "options": [],
                        },
                    }
                )
                permission_reply = json.loads(sys.stdin.readline())
                if permission_reply.get("id") != 900:
                    return 4
                terminal(request_id, prompt_count=prompt_count)
            elif scenario in {
                "permission-allow",
                "permission-deny",
                "permission-write-allow",
                "permission-write-deny",
                "permission-write-allow-alias",
                "permission-write-deny-alias-sibling",
            }:
                if scenario in {"permission-write-allow", "permission-write-deny"}:
                    raw_input = {
                        "variant": "SearchReplace",
                        "file_path": "project/.fleet/evidence/review.md",
                        "old_string": "",
                        "new_string": "bounded evidence",
                        "replace_all": False,
                    }
                elif scenario == "permission-write-allow-alias":
                    # The grok CLI emits the real absolute alias path from its cwd
                    # (`cwd=proxy_root`); the broker must accept it like the
                    # equivalent project/<...> request.
                    raw_input = {
                        "variant": "SearchReplace",
                        "file_path": str(Path.cwd() / "project" / "fixture.txt"),
                        "old_string": "",
                        "new_string": "bounded evidence",
                        "replace_all": False,
                    }
                elif scenario == "permission-write-deny-alias-sibling":
                    # A sibling proxy_root of another run id must be refused even
                    # though it carries the same "project/fixture.txt" tail.
                    raw_input = {
                        "variant": "SearchReplace",
                        "file_path": str(
                            Path.cwd().parent
                            / "run-20260820T000000Z-aaaaaaaaaa"
                            / "project"
                            / "fixture.txt"
                        ),
                        "old_string": "",
                        "new_string": "bounded evidence",
                        "replace_all": False,
                    }
                else:
                    command = (
                        "pwsh -NoLogo -NoProfile -File project/.fleet/apply-and-verify.ps1"
                        if scenario == "permission-allow"
                        else "pwsh -NoLogo -NoProfile -Command Remove-Item -Recurse project"
                    )
                    raw_input = {
                        "variant": "Bash",
                        "command": command,
                    }
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 902,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": SESSION_ID,
                            "toolCall": {
                                "toolCallId": "bounded-shell",
                                "kind": "execute",
                                "title": "run_terminal_command",
                                "rawInput": raw_input,
                            },
                            "options": [
                                {
                                    "optionId": "allow-once",
                                    "kind": "allow_once",
                                    "name": "Yes, proceed",
                                },
                                {
                                    "optionId": "reject-once",
                                    "kind": "reject_once",
                                    "name": "No",
                                },
                            ],
                        },
                    }
                )
                permission_reply = json.loads(sys.stdin.readline())
                outcome = permission_reply.get("result", {}).get("outcome", {})
                if scenario in {"permission-allow", "permission-write-allow", "permission-write-allow-alias"}:
                    if outcome != {
                        "outcome": "selected",
                        "optionId": "allow-once",
                    }:
                        return 5
                elif outcome != {"outcome": "cancelled"}:
                    return 6
                terminal(request_id, prompt_count=prompt_count)
            elif scenario == "reverse-request":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": 901,
                        "method": "workspace/apply_edit",
                        "params": {"sessionId": SESSION_ID},
                    }
                )
                reverse_reply = json.loads(sys.stdin.readline())
                if reverse_reply.get("id") != 901 or "error" not in reverse_reply:
                    return 5
                terminal(request_id, prompt_count=prompt_count)
            elif scenario == "repair" and prompt_count == 1:
                terminal(request_id, partial=True, prompt_count=prompt_count)
            elif scenario == "model-switch" and prompt_count == 1:
                terminal(
                    request_id,
                    partial=True,
                    prompt_count=prompt_count,
                    model_id="grok-4.5",
                )
            elif scenario == "stderr-thought":
                sys.stderr.write("PRIVATE_STDERR_THOUGHT_MUST_NOT_PERSIST\n")
                sys.stderr.flush()
                terminal(request_id, prompt_count=prompt_count)
            elif scenario == "permission-deny-then-complete":
                if prompt_count == 1:
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": 903,
                            "method": "session/request_permission",
                            "params": {
                                "sessionId": SESSION_ID,
                                "toolCall": {
                                    "toolCallId": "denied-shell",
                                    "kind": "execute",
                                    "title": "run_terminal_command",
                                    "rawInput": {
                                        "variant": "Bash",
                                        "command": "pwsh -NoLogo -NoProfile -Command Remove-Item -Recurse project",
                                    },
                                },
                                "options": [
                                    {
                                        "optionId": "allow-once",
                                        "kind": "allow_once",
                                        "name": "Yes, proceed",
                                    },
                                    {
                                        "optionId": "reject-once",
                                        "kind": "reject_once",
                                        "name": "No",
                                    },
                                ],
                            },
                        }
                    )
                    permission_reply = json.loads(sys.stdin.readline())
                    outcome = permission_reply.get("result", {}).get("outcome", {})
                    if outcome != {"outcome": "cancelled"}:
                        return 6
                    update(
                        {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "denied-shell",
                            "title": "Execute denied",
                            "kind": "execute",
                            "status": "failed",
                            "tool": "bash",
                        }
                    )
                    response(
                        request_id,
                        {
                            "stopReason": "cancelled",
                            "_meta": {
                                "modelId": "grok-4.7",
                                "usage": {
                                    "inputTokens": 40,
                                    "outputTokens": 10,
                                    "totalTokens": 50,
                                },
                            },
                        },
                    )
                else:
                    terminal(request_id, prompt_count=prompt_count)
            else:
                terminal(request_id, prompt_count=prompt_count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
