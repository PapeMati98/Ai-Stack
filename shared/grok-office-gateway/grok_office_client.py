#!/usr/bin/env python3
"""Grok Office Client

Command-line client for the Grok Bot VM to interact with Agent Office
via the Grok Office Gateway. Standard library only (Python 3.11+).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import sys
import time
import urllib.error
import urllib.request

# Maximum requested polling budget for a single `ask` request. The client
# caps each urllib blocking-operation timeout by the remaining budget; urllib
# does not guarantee a hard wall-clock limit for a whole streamed response.
MAX_TIMEOUT_S = 240.0


class RedirectRefusedError(Exception):
    """Raised when an HTTP redirect is encountered."""

    def __init__(self, code: int = 302, message: str = "redirect_refused"):
        super().__init__(message)
        self.code = code


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses all redirects to prevent credential or token leakage."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def http_error_301(self, req, fp, code, msg, headers):
        raise RedirectRefusedError(code, f"HTTP redirect {code} refused")

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


_CLIENT_OPENER = urllib.request.build_opener(NoRedirectHandler)


def _get_env_token(env: dict) -> str | None:
    token = env.get("GROK_OFFICE_TOKEN")
    if token and token.strip():
        return token.strip()
    token_file = env.get("GROK_OFFICE_TOKEN_FILE")
    if token_file and token_file.strip():
        try:
            return Path(token_file.strip()).read_text(encoding="utf-8").strip()
        except Exception:
            return None
    return None


def _request_json(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    payload: dict | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict]:
    req_headers = dict(headers or {})
    req_headers["User-Agent"] = "grok-office-client/1"
    req_headers["ngrok-skip-browser-warning"] = "1"

    data_bytes = None
    if payload is not None:
        data_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req_headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data_bytes, headers=req_headers, method=method)
    try:
        with _CLIENT_OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"status": "ok", "raw": raw}
    except RedirectRefusedError as exc:
        return exc.code, {
            "status": "error",
            "code": "redirect_refused",
            "message": str(exc),
        }
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400 and exc.code != 304:
            return exc.code, {
                "status": "error",
                "code": "redirect_refused",
                "message": f"HTTP redirect {exc.code} refused",
            }
        try:
            err_body = exc.read().decode("utf-8")
        except Exception:
            # An unreadable or undecodable error body must not escape error
            # normalization; fall through to the generic http_error report.
            err_body = ""
        try:
            return exc.code, json.loads(err_body)
        except Exception:
            return exc.code, {
                "status": "error",
                "code": "http_error",
                "http_status": exc.code,
                "message": str(exc),
            }
    except (TimeoutError, urllib.error.URLError) as exc:
        is_timeout = isinstance(exc, TimeoutError) or (
            isinstance(exc, urllib.error.URLError)
            and isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout))
        ) or "timed out" in str(exc).lower()
        if is_timeout:
            return 0, {
                "status": "error",
                "code": "timeout",
                "message": "request timed out",
            }
        return 0, {
            "status": "error",
            "code": "connection_error",
            "message": str(exc),
        }
    except Exception as exc:
        if "timed out" in str(exc).lower():
            return 0, {
                "status": "error",
                "code": "timeout",
                "message": str(exc),
            }
        return 0, {
            "status": "error",
            "code": "connection_error",
            "message": str(exc),
        }


def run_client(
    argv: list[str] | None = None,
    env: dict | None = None,
    stdout=None,
    stdin=None,
) -> int:
    """Execute grok-office-client command in-process.

    Returns exit code (0 = success, 2 = HTTP/client error, 3 = timeout).
    """
    if argv is None:
        argv = sys.argv[1:]
    if env is None:
        env = dict(os.environ)
    if stdout is None:
        stdout = sys.stdout
    if stdin is None:
        stdin = sys.stdin

    base_url = env.get("GROK_OFFICE_URL")
    if not base_url or not base_url.strip():
        stdout.write(
            json.dumps({
                "status": "error",
                "code": "missing_config",
                "message": "GROK_OFFICE_URL environment variable is required",
            })
            + "\n"
        )
        return 2

    base_url = base_url.strip().rstrip("/")
    token = _get_env_token(env)
    if not token:
        stdout.write(
            json.dumps({
                "status": "error",
                "code": "missing_config",
                "message": "GROK_OFFICE_TOKEN or valid GROK_OFFICE_TOKEN_FILE is required",
            })
            + "\n"
        )
        return 2

    headers = {
        "Authorization": f"Bearer {token}",
    }

    parser = argparse.ArgumentParser(description="Grok Office Client", prog="grok_office_client.py")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # health
    subparsers.add_parser("health", help="Check gateway health")

    # send
    send_parser = subparsers.add_parser("send", help="Send a message")
    send_parser.add_argument("--target", required=True, help="Target alias or 'channel'")
    send_parser.add_argument("--kind", default="note", help="Message kind (note, question, handoff)")
    send_parser.add_argument("--reply-to", dest="reply_to", help="32-hex message id to reply to")
    send_parser.add_argument("--dedupe-key", dest="dedupe_key", help="Deduplication key")
    send_group = send_parser.add_mutually_exclusive_group(required=True)
    send_group.add_argument("--body", help="Message body text")
    send_group.add_argument("--body-stdin", dest="body_stdin", action="store_true", help="Read body from stdin")

    # inbox
    inbox_parser = subparsers.add_parser("inbox", help="Read inbox messages")
    inbox_parser.add_argument("--limit", type=int, default=20, help="Maximum messages to read")
    inbox_parser.add_argument("--peek", action="store_true", help="Peek without advancing cursor")

    # channel
    channel_parser = subparsers.add_parser("channel", help="Read channel messages")
    channel_parser.add_argument("--limit", type=int, default=20, help="Maximum messages to read")
    channel_parser.add_argument("--peek", action="store_true", help="Peek without advancing cursor")

    # ack
    ack_parser = subparsers.add_parser("ack", help="Acknowledge a message")
    ack_parser.add_argument("message_id", help="32-hex message id")
    ack_parser.add_argument("--note", help="Optional acknowledgment note")

    # advance
    advance_parser = subparsers.add_parser("advance", help="Advance read cursor past processed message")
    advance_parser.add_argument("--source", required=True, choices=["inbox", "channel"], help="Source to advance (inbox or channel)")
    advance_parser.add_argument("--through-id", required=True, dest="through_id", help="32-hex message id through which to advance")

    # ask
    ask_parser = subparsers.add_parser("ask", help="Send question and poll for reply")
    ask_parser.add_argument("--target", required=True, help="Target alias")
    ask_parser.add_argument("--body", help="Question body text")
    ask_parser.add_argument("--body-stdin", dest="body_stdin", action="store_true", help="Read question from stdin")
    ask_parser.add_argument("--dedupe-key", dest="dedupe_key", help="Optional dedupe key")
    ask_parser.add_argument("--timeout-s", dest="timeout_s", type=float, default=MAX_TIMEOUT_S, help=f"Polling budget in seconds (default {MAX_TIMEOUT_S:g}, max {MAX_TIMEOUT_S:g})")
    ask_parser.add_argument("--poll-s", dest="poll_s", type=float, default=20.0, help="Poll interval in seconds")

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    cmd = args.subcommand

    if cmd == "health":
        url = f"{base_url}/v1/health"
        code, resp = _request_json(url, method="GET", headers=headers, timeout=30.0)
        stdout.write(json.dumps(resp) + "\n")
        return 0 if (200 <= code < 300) else 2

    if cmd == "send":
        if args.body_stdin:
            body_text = stdin.read().strip()
        else:
            body_text = args.body or ""

        payload = {
            "target": args.target,
            "body": body_text,
            "kind": args.kind,
        }
        if args.reply_to:
            payload["reply_to"] = args.reply_to
        if args.dedupe_key:
            payload["dedupe_key"] = args.dedupe_key

        url = f"{base_url}/v1/send"
        code, resp = _request_json(url, method="POST", headers=headers, payload=payload, timeout=30.0)
        stdout.write(json.dumps(resp) + "\n")
        return 0 if (200 <= code < 300) else 2

    if cmd == "inbox":
        url = f"{base_url}/v1/inbox?limit={args.limit}&peek=1"
        code, resp = _request_json(url, method="GET", headers=headers, timeout=30.0)
        stdout.write(json.dumps(resp) + "\n")
        return 0 if (200 <= code < 300) else 2

    if cmd == "channel":
        url = f"{base_url}/v1/channel?limit={args.limit}&peek=1"
        code, resp = _request_json(url, method="GET", headers=headers, timeout=30.0)
        stdout.write(json.dumps(resp) + "\n")
        return 0 if (200 <= code < 300) else 2

    if cmd == "advance":
        payload = {
            "source": args.source,
            "through_id": args.through_id,
        }
        url = f"{base_url}/v1/advance"
        code, resp = _request_json(url, method="POST", headers=headers, payload=payload, timeout=30.0)
        stdout.write(json.dumps(resp) + "\n")
        return 0 if (200 <= code < 300) else 2

    if cmd == "ack":
        payload = {"message_id": args.message_id}
        if args.note:
            payload["note"] = args.note
        url = f"{base_url}/v1/ack"
        code, resp = _request_json(url, method="POST", headers=headers, payload=payload, timeout=30.0)
        stdout.write(json.dumps(resp) + "\n")
        return 0 if (200 <= code < 300) else 2

    if cmd == "ask":
        if args.body_stdin or args.body is None:
            body_text = stdin.read().strip()
        else:
            body_text = args.body.strip()

        if not body_text:
            stdout.write(
                json.dumps({
                    "status": "error",
                    "code": "empty_body",
                    "message": "Question body cannot be empty",
                })
                + "\n"
            )
            return 2

        if args.dedupe_key:
            dedupe_key = args.dedupe_key
        else:
            raw_key = f"{args.target}\nquestion\n{body_text}"
            dedupe_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:40]

        # Validate the requested polling budget before network I/O. It bounds
        # polling and sleeps; urllib's socket timeout applies to blocking
        # operations, not to the wall-clock duration of an entire response.
        timeout_s = args.timeout_s
        poll_s = args.poll_s
        if not math.isfinite(timeout_s) or timeout_s <= 0 or timeout_s > MAX_TIMEOUT_S:
            stdout.write(
                json.dumps({
                    "status": "error",
                    "code": "invalid_timeout",
                    "message": (
                        f"--timeout-s must be a finite number greater than 0 "
                        f"and at most {MAX_TIMEOUT_S:g} seconds"
                    ),
                    "dedupe_key": dedupe_key,
                })
                + "\n"
            )
            return 2
        if not math.isfinite(poll_s) or poll_s <= 0:
            stdout.write(
                json.dumps({
                    "status": "error",
                    "code": "invalid_poll_interval",
                    "message": "--poll-s must be a finite number greater than 0",
                    "dedupe_key": dedupe_key,
                })
                + "\n"
            )
            return 2

        def _timeout_result(
            sent_id: str | None = None,
            send_outcome: str | None = None,
        ) -> dict:
            # A timeout leaves the answer unknown, not missing. Preserve the
            # correlation material (request id once known, plus the original
            # dedupe key) so the caller can retry idempotently. No secrets.
            result = {
                "status": "error",
                "code": "timeout",
                "message": "timed out waiting for reply",
                "dedupe_key": dedupe_key,
            }
            if sent_id:
                result["sent_id"] = sent_id
            if send_outcome:
                result["send_outcome"] = send_outcome
            return result

        deadline = time.monotonic() + timeout_s

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stdout.write(json.dumps(_timeout_result()) + "\n")
            return 3

        # Cap each urllib blocking-operation timeout by the remaining polling
        # budget. A streamed response can still span multiple blocking reads.
        call_timeout = min(30.0, remaining)
        h_code, h_resp = _request_json(
            f"{base_url}/v1/health", method="GET", headers=headers, timeout=call_timeout
        )
        if time.monotonic() >= deadline or h_resp.get("code") == "timeout":
            stdout.write(json.dumps(_timeout_result()) + "\n")
            return 3

        if h_code < 200 or h_code >= 300:
            health_error = dict(h_resp)
            health_error["dedupe_key"] = dedupe_key
            stdout.write(json.dumps(health_error) + "\n")
            return 2

        target_agents = h_resp.get("target_agents", {})
        expected_from = target_agents.get(args.target)

        send_payload = {
            "target": args.target,
            "body": body_text,
            "kind": "question",
            "dedupe_key": dedupe_key,
        }
        send_url = f"{base_url}/v1/send"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stdout.write(json.dumps(_timeout_result()) + "\n")
            return 3

        call_timeout = min(30.0, remaining)
        code, send_resp = _request_json(
            send_url, method="POST", headers=headers, payload=send_payload, timeout=call_timeout
        )
        # Capture the id from a successful response before the deadline
        # check: a send that lands exactly at the deadline still succeeded,
        # and its id must survive into the timeout report.
        sent_id = send_resp.get("id") if 200 <= code < 300 else None
        if time.monotonic() >= deadline or send_resp.get("code") == "timeout":
            # The send may or may not have been delivered; report the id
            # when the gateway confirmed it. Otherwise preserve the unknown
            # send outcome and dedupe key so the caller can retry idempotently.
            send_outcome = "unconfirmed" if code < 200 or code >= 300 else None
            stdout.write(
                json.dumps(_timeout_result(sent_id, send_outcome=send_outcome)) + "\n"
            )
            return 3

        if code < 200 or code >= 300:
            send_error = dict(send_resp)
            send_error["dedupe_key"] = dedupe_key
            send_error["send_outcome"] = "unconfirmed"
            if sent_id:
                send_error["sent_id"] = sent_id
            stdout.write(json.dumps(send_error) + "\n")
            return 2

        if not sent_id:
            stdout.write(
                json.dumps({
                    "status": "error",
                    "code": "missing_message_id",
                    "message": "Gateway did not return an id for sent question",
                    "dedupe_key": dedupe_key,
                })
                + "\n"
            )
            return 2

        inbox_url = f"{base_url}/v1/inbox?reply_to={sent_id}&peek=1"

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            call_timeout = min(30.0, remaining)
            code, inbox_resp = _request_json(
                inbox_url, method="GET", headers=headers, timeout=call_timeout
            )
            if time.monotonic() >= deadline or inbox_resp.get("code") == "timeout":
                break

            if code < 200 or code >= 300:
                # A polling failure after a successful send must preserve the
                # correlation material so the caller can retry idempotently.
                poll_error = dict(inbox_resp)
                poll_error["sent_id"] = sent_id
                poll_error["dedupe_key"] = dedupe_key
                stdout.write(json.dumps(poll_error) + "\n")
                return 2

            if inbox_resp.get("scan_incomplete"):
                stdout.write(
                    json.dumps({
                        "status": "error",
                        "code": "backlog_exceeds_scan",
                        "message": "More unread messages than the correlation scan window; "
                                   "process and advance the inbox (routine), then retry.",
                        "sent_id": sent_id,
                        "dedupe_key": dedupe_key,
                        "remaining": inbox_resp.get("remaining"),
                    })
                    + "\n"
                )
                return 4

            messages = inbox_resp.get("messages", [])
            matched = None
            for m in messages:
                m_reply_to = m.get("reply_to")
                m_from = m.get("from")
                if m_reply_to == sent_id and expected_from is not None and m_from == expected_from and not m.get("ack_of"):
                    matched = m
                    break

            if matched is not None:
                ack_payload = {
                    "message_id": matched["id"],
                    "note": "processed by grok_office_client ask",
                }
                ack_rem = deadline - time.monotonic()
                if ack_rem > 0:
                    # The ACK's urllib blocking-operation timeout uses only
                    # the remaining polling budget.
                    _request_json(
                        f"{base_url}/v1/ack",
                        method="POST",
                        headers=headers,
                        payload=ack_payload,
                        timeout=min(30.0, ack_rem),
                    )
                stdout.write(json.dumps(matched) + "\n")
                return 0

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_s, remaining))

        stdout.write(json.dumps(_timeout_result(sent_id)) + "\n")
        return 3

    return 2


def main(argv=None) -> None:
    code = run_client(argv)
    sys.exit(code)


if __name__ == "__main__":
    main()
