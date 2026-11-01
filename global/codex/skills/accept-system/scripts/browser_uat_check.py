#!/usr/bin/env python3
"""Browser UAT verifier and evidence validator for accept-system.

Validates human-like interactive browser UAT evidence and performs bounded
loopback HTTP + DOM readiness probes to generate UAT evidence skeletons.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "0.2.0"
SCHEMA_VERSION = "1.0"

# Specific reason codes required by specification
REASON_STATIC_SCREENSHOT_ONLY = "static_screenshot_only"
REASON_MISSING_INTERACTIVE_ACTION = "missing_interactive_action"
REASON_CONSOLE_ERRORS_PRESENT = "console_errors_present"
REASON_HTTP_5XX_PRESENT = "http_5xx_present"
REASON_MISSING_MOBILE_VIEWPORT = "missing_mobile_viewport"
REASON_ARTIFACT_HASH_MISMATCH = "artifact_hash_mismatch"

ALLOWED_ENVIRONMENTS = {"DEV", "TEST", "STAGING"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

DESKTOP_VIEWPORT = (1440, 900)
MOBILE_VIEWPORT = (390, 844)


class BrowserUATValidationError(Exception):
    """Raised when browser UAT evidence report validation fails."""

    def __init__(self, reason_codes: list[str], errors: list[str] | None = None):
        self.reason_codes = reason_codes
        self.errors = errors or []
        super().__init__(
            f"Browser UAT validation failed: {', '.join(reason_codes) if reason_codes else 'unknown errors'}"
        )


def get_subprocess_creation_flags() -> int:
    """Return creation flags ensuring no terminal window is spawned on Windows."""
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hexadecimal digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def is_loopback_host(hostname: str | None) -> bool:
    """Check if hostname is a safe local loopback interface."""
    if not hostname:
        return False
    h = hostname.lower()
    return h in LOOPBACK_HOSTS or h.endswith(".localhost")


class SafeLoopbackRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Ensure redirects only follow safe loopback URLs and never leak credentials."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        new_parsed = urllib.parse.urlparse(newurl)
        if new_parsed.scheme not in ("http", "https"):
            raise ValueError(f"Redirect scheme must be http or https: {new_parsed.scheme}")
        if new_parsed.username is not None or new_parsed.password is not None or "@" in (new_parsed.netloc or ""):
            raise ValueError("Redirect URL contains embedded credentials (userinfo)")
        new_host = (new_parsed.hostname or "").lower()
        if not is_loopback_host(new_host):
            raise ValueError(
                f"External network calls prohibited: redirect target '{new_host}' is not a local/loopback interface"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def parse_viewport_dimensions(vp: Any) -> tuple[int, int] | None:
    """Extract (width, height) from various viewport representations."""
    if isinstance(vp, dict):
        w = vp.get("width")
        h = vp.get("height")
        if isinstance(w, int) and isinstance(h, int) and not isinstance(w, bool):
            return (w, h)
        name = str(vp.get("name", "")).strip().lower()
        if name == "desktop":
            return DESKTOP_VIEWPORT
        if name == "mobile":
            return MOBILE_VIEWPORT
    elif isinstance(vp, str):
        vp_str = vp.strip().lower()
        if vp_str == "desktop":
            return DESKTOP_VIEWPORT
        if vp_str == "mobile":
            return MOBILE_VIEWPORT
        match = re.match(r"^(\d+)[xX](\d+)$", vp_str)
        if match:
            return (int(match.group(1)), int(match.group(2)))
    elif isinstance(vp, (list, tuple)) and len(vp) == 2:
        if isinstance(vp[0], int) and isinstance(vp[1], int) and not isinstance(vp[0], bool) and not isinstance(vp[1], bool):
            return (vp[0], vp[1])
    return None


def check_viewports(report: dict[str, Any]) -> tuple[bool, bool]:
    """Check if desktop (1440x900) and mobile (390x844) viewports are covered."""
    has_desktop = False
    has_mobile = False

    raw_vps = report.get("viewports")
    if isinstance(raw_vps, list):
        for vp in raw_vps:
            dims = parse_viewport_dimensions(vp)
            if dims:
                w, h = dims
                if w == 1440 and h == 900:
                    has_desktop = True
                if w == 390 and h == 844:
                    has_mobile = True

    screenshots = report.get("screenshots")
    if isinstance(screenshots, list):
        for sc in screenshots:
            if isinstance(sc, dict):
                vp = sc.get("viewport")
                dims = parse_viewport_dimensions(vp)
                if dims:
                    w, h = dims
                    if w == 1440 and h == 900:
                        has_desktop = True
                    if w == 390 and h == 844:
                        has_mobile = True

    return has_desktop, has_mobile


def classify_step(step: Any) -> str | None:
    """Classify a step into one of: 'open', 'user_action', 'visible_response', 'terminal_state', 'screenshot', or None."""
    if isinstance(step, str):
        s = step.strip().lower()
        if s in ("open", "navigate", "load", "goto", "url_load"):
            return "open"
        if s in ("user_action", "action", "click", "type", "input", "press", "submit", "select", "hover", "drag"):
            return "user_action"
        if s in ("visible_response", "response", "dom_update", "render", "visible", "state_change"):
            return "visible_response"
        if s in ("terminal_state", "terminal", "completed", "complete", "done", "finish", "final_state"):
            return "terminal_state"
        if s in ("screenshot", "view", "capture"):
            return "screenshot"
        return None

    if isinstance(step, dict):
        step_type = str(step.get("type") or step.get("step_type") or step.get("phase") or "").strip().lower()
        action = str(step.get("action") or step.get("name") or step.get("step") or "").strip().lower()

        # Check type/phase first
        if step_type in ("open", "navigate", "load", "url_load"):
            return "open"
        if step_type in ("user_action", "action", "interaction"):
            return "user_action"
        if step_type in ("visible_response", "response", "assertion"):
            return "visible_response"
        if step_type in ("terminal_state", "terminal", "final"):
            return "terminal_state"
        if step_type in ("screenshot", "capture", "view"):
            return "screenshot"

        # Check action/name
        if action in ("open", "navigate", "load", "goto"):
            return "open"
        if action in ("click", "type", "input", "press", "submit", "select", "hover", "drag", "user_action"):
            return "user_action"
        if action in ("assert_visible", "visible", "render", "dom_update", "visible_response", "response"):
            return "visible_response"
        if action in ("state_check", "terminal_state", "terminal", "complete", "completed", "done", "finish"):
            return "terminal_state"
        if action in ("screenshot", "take_screenshot", "capture"):
            return "screenshot"

    return None


def is_step_executed(step: Any) -> bool:
    """Return True if step represents an executed result rather than a pending skeleton step."""
    if isinstance(step, dict):
        st = str(step.get("status") or "").strip().upper()
        if st in ("PENDING", "TODO", "NOT_RUN", "SKIPPED", "DRAFT", "IN_PROGRESS"):
            return False
        if st in ("FAIL", "FAILED", "ERROR"):
            return False
    return True


def is_static_step(step: Any) -> bool:
    """Return True if step represents a non-interactive static capture/open step."""
    cls = classify_step(step)
    return cls in ("open", "screenshot", None)


def _is_valid_zero_counter(val: Any) -> bool:
    """Check if value is strictly integer 0 (not bool) or an empty list."""
    if isinstance(val, bool):
        return False
    if isinstance(val, int) and val == 0:
        return True
    if isinstance(val, list) and len(val) == 0:
        return True
    return False


def validate_report(
    report_data_or_path: dict[str, Any] | str | Path,
    base_dir: str | Path | None = None,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """Validate browser UAT evidence structure and fail-closed on violations."""
    report: dict[str, Any]
    if isinstance(report_data_or_path, (str, Path)):
        p = Path(report_data_or_path).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Report file not found: {p}")
        if base_dir is None:
            base_dir = p.parent
        with p.open("r", encoding="utf-8") as handle:
            report = json.load(handle)
    elif isinstance(report_data_or_path, dict):
        report = report_data_or_path
    else:
        raise TypeError(f"Expected dict or file path, got {type(report_data_or_path)}")

    reason_codes: list[str] = []
    errors: list[str] = []

    # 1. Route or URL check
    route_or_url = report.get("route") or report.get("url")
    if not route_or_url or not isinstance(route_or_url, str) or not route_or_url.strip():
        errors.append("Report must include a non-empty 'route' or 'url'")

    # 2. Role check
    role = report.get("role")
    if not role or not isinstance(role, str) or not role.strip():
        errors.append("Report must include a non-empty 'role'")

    # 3. Environment check (DEV/TEST/STAGING)
    env = report.get("environment")
    if not env or not isinstance(env, str) or env.strip().upper() not in ALLOWED_ENVIRONMENTS:
        errors.append(f"Report environment must be one of {sorted(ALLOWED_ENVIRONMENTS)}")

    # 4. Viewport coverage
    responsive_required = bool(report.get("responsive_required", False))
    if responsive_required:
        has_desktop, has_mobile = check_viewports(report)
        if not has_mobile:
            reason_codes.append(REASON_MISSING_MOBILE_VIEWPORT)
            errors.append("Responsive check required: mobile viewport (390x844) is missing")
        if not has_desktop:
            errors.append("Responsive check required: desktop viewport (1440x900) is missing")

    # 5. Interactive steps check
    steps = report.get("steps")
    is_static = False
    if report.get("static_screenshot_only") is True or report.get("is_static") is True:
        is_static = True
    elif str(report.get("kind", "")).strip().lower() in ("static", "static_screenshot"):
        is_static = True
    elif str(report.get("evidence_kind", "")).strip().lower() in ("static", "static_screenshot"):
        is_static = True

    if not isinstance(steps, list) or len(steps) == 0:
        is_static = True
        reason_codes.append(REASON_MISSING_INTERACTIVE_ACTION)
        errors.append("Report must include an interactive steps list with executed results")
    else:
        # Check for unexecuted/pending steps
        for s in steps:
            if not is_step_executed(s):
                st_desc = s.get("status") if isinstance(s, dict) else "unexecuted"
                cls_desc = classify_step(s)
                errors.append(f"Step '{cls_desc or s}' has not been executed (status: {st_desc})")

        # If all steps are static or unexecuted, mark as static
        if all(is_static_step(s) or not is_step_executed(s) for s in steps):
            is_static = True

    if is_static and REASON_STATIC_SCREENSHOT_ONLY not in reason_codes:
        reason_codes.append(REASON_STATIC_SCREENSHOT_ONLY)
        errors.append("Report is static-screenshot-only; interactive human-like journey is required")

    # Analyze executed steps
    executed_steps = [s for s in steps if is_step_executed(s)] if isinstance(steps, list) else []
    step_classes: list[str | None] = [classify_step(s) for s in executed_steps]

    has_open = any(c == "open" for c in step_classes)
    has_user_action = any(c == "user_action" for c in step_classes)
    has_visible_response = any(c == "visible_response" for c in step_classes)
    has_terminal_state = any(c == "terminal_state" for c in step_classes)

    if not has_user_action:
        reason_codes.append(REASON_MISSING_INTERACTIVE_ACTION)
        errors.append("Missing executed interactive user_action in steps list")

    if not has_open:
        errors.append("Missing required executed 'open' step in interactive steps sequence")
    if not has_visible_response:
        errors.append("Missing required executed 'visible_response' step in interactive steps sequence")
    if not has_terminal_state:
        errors.append("Missing required executed 'terminal_state' step in interactive steps sequence")

    if has_open and has_user_action and has_visible_response and has_terminal_state:
        open_idx = next(i for i, c in enumerate(step_classes) if c == "open")
        action_idx = next(i for i, c in enumerate(step_classes) if c == "user_action")
        response_idx = next(i for i, c in enumerate(step_classes) if c == "visible_response")
        terminal_idx = next(i for i, c in enumerate(step_classes) if c == "terminal_state")
        if not (open_idx < action_idx < response_idx < terminal_idx):
            errors.append(
                "Interactive steps must follow the progression: open -> user_action -> visible_response -> terminal_state"
            )

    # 6. Console errors == 0 check (fail-closed on absence, invalid types, or non-zero)
    has_console_key = ("console_errors" in report) or ("console_error_count" in report)
    if not has_console_key:
        reason_codes.append(REASON_CONSOLE_ERRORS_PRESENT)
        errors.append("Evidence missing required 'console_errors' or 'console_error_count'")
    else:
        console_ok = True
        if "console_errors" in report and not _is_valid_zero_counter(report["console_errors"]):
            console_ok = False
        if "console_error_count" in report and not _is_valid_zero_counter(report["console_error_count"]):
            console_ok = False
        if not console_ok:
            reason_codes.append(REASON_CONSOLE_ERRORS_PRESENT)
            errors.append(
                f"Console errors present or invalid counter shape/type (console_errors={report.get('console_errors')!r}, console_error_count={report.get('console_error_count')!r})"
            )

    # 7. HTTP 5xx count == 0 check (fail-closed on absence, invalid types, or non-zero)
    has_http_5xx_key = ("http_5xx_count" in report) or ("http_5xx_errors" in report)
    if not has_http_5xx_key:
        reason_codes.append(REASON_HTTP_5XX_PRESENT)
        errors.append("Evidence missing required 'http_5xx_count' or 'http_5xx_errors'")
    else:
        http_5xx_ok = True
        if "http_5xx_count" in report and not _is_valid_zero_counter(report["http_5xx_count"]):
            http_5xx_ok = False
        if "http_5xx_errors" in report and not _is_valid_zero_counter(report["http_5xx_errors"]):
            http_5xx_ok = False
        if not http_5xx_ok:
            reason_codes.append(REASON_HTTP_5XX_PRESENT)
            errors.append(
                f"HTTP 5xx server errors present or invalid counter shape/type (http_5xx_count={report.get('http_5xx_count')!r}, http_5xx_errors={report.get('http_5xx_errors')!r})"
            )

    # 8. Existing screenshots with matching SHA256 (mandatory evidence requirement)
    screenshots = report.get("screenshots")
    if not isinstance(screenshots, list) or len(screenshots) == 0:
        reason_codes.append(REASON_ARTIFACT_HASH_MISMATCH)
        errors.append("Evidence must include existing screenshot files with matching SHA256 digests; none provided")
    else:
        valid_matching_screenshots = 0
        for idx, sc in enumerate(screenshots):
            if not isinstance(sc, dict):
                reason_codes.append(REASON_ARTIFACT_HASH_MISMATCH)
                errors.append(f"Screenshot entry [{idx}] must be a dict")
                continue

            sc_path_str = sc.get("path") or sc.get("file") or sc.get("filename")
            if not sc_path_str or not isinstance(sc_path_str, str) or not sc_path_str.strip():
                reason_codes.append(REASON_ARTIFACT_HASH_MISMATCH)
                errors.append(f"Screenshot entry [{idx}] missing file path")
                continue

            expected_hash = sc.get("sha256") or sc.get("hash") or sc.get("digest")
            if not expected_hash or not isinstance(expected_hash, str) or not expected_hash.strip():
                reason_codes.append(REASON_ARTIFACT_HASH_MISMATCH)
                errors.append(f"Screenshot entry [{idx}] missing expected sha256")
                continue
            expected_hash = expected_hash.strip().lower()

            candidate_path = Path(sc_path_str)
            if not candidate_path.is_absolute():
                if base_dir:
                    candidate_path = (Path(base_dir) / sc_path_str).resolve()
                else:
                    candidate_path = candidate_path.resolve()

            if not candidate_path.exists() or not candidate_path.is_file():
                reason_codes.append(REASON_ARTIFACT_HASH_MISMATCH)
                errors.append(f"Screenshot file does not exist: {sc_path_str}")
                continue

            try:
                actual_hash = sha256_file(candidate_path)
                if actual_hash != expected_hash:
                    reason_codes.append(REASON_ARTIFACT_HASH_MISMATCH)
                    errors.append(
                        f"Screenshot SHA256 mismatch for {sc_path_str}: expected {expected_hash}, got {actual_hash}"
                    )
                else:
                    valid_matching_screenshots += 1
            except Exception as exc:
                reason_codes.append(REASON_ARTIFACT_HASH_MISMATCH)
                errors.append(f"Failed to read screenshot {sc_path_str}: {exc}")

        if valid_matching_screenshots == 0:
            if REASON_ARTIFACT_HASH_MISMATCH not in reason_codes:
                reason_codes.append(REASON_ARTIFACT_HASH_MISMATCH)
            errors.append("No screenshot entry resolved to an existing file with matching SHA256 digest")

    # Deduplicate reason codes preserving encounter order
    deduped_reasons: list[str] = []
    for code in reason_codes:
        if code not in deduped_reasons:
            deduped_reasons.append(code)

    is_valid = (len(deduped_reasons) == 0 and len(errors) == 0)

    if not is_valid and raise_on_error:
        raise BrowserUATValidationError(deduped_reasons, errors)

    return {
        "valid": is_valid,
        "reason_codes": deduped_reasons,
        "errors": errors,
        "report": report,
    }


def probe_url(
    url: str,
    role: str = "user",
    environment: str = "DEV",
    responsive_required: bool = False,
    timeout: float = 3.0,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Perform bounded loopback HTTP probe + DOM readiness check and generate UAT evidence skeleton."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https: {url}")

    if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
        raise ValueError("URLs with embedded credentials (userinfo) are prohibited")

    hostname = (parsed.hostname or "").lower()
    if not is_loopback_host(hostname):
        raise ValueError(f"External network calls prohibited: '{hostname}' is not a local/loopback interface")

    bounded_timeout = max(0.5, min(float(timeout), 10.0))
    status_code: int | None = None
    dom_ready = False
    content_type: str | None = None
    probe_error: str | None = None

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "BrowserUATProbe/1.0 (Local Loopback Readiness Check)",
                "Accept": "text/html,application/xhtml+xml,application/json,*/*",
            },
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),  # Explicitly disable environment proxies
            SafeLoopbackRedirectHandler(),
        )
        with opener.open(req, timeout=bounded_timeout) as resp:
            status_code = resp.status
            content_type = resp.headers.get_content_type()
            raw_body = resp.read(65536)  # Bounded read up to 64KB
            body_text = raw_body.decode("utf-8", errors="replace")
            # DOM readiness check: look for html/body/root/app tags or substantive HTML content
            if re.search(r"<(?:html|body|div|main|app|root)\b", body_text, re.I) or "<!doctype" in body_text.lower():
                dom_ready = True
            elif len(body_text.strip()) > 0 and content_type == "text/html":
                dom_ready = True
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        dom_ready = False
        probe_error = f"HTTP Error {exc.code}: {exc.reason}"
    except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as exc:
        status_code = None
        dom_ready = False
        probe_error = str(getattr(exc, "reason", exc))

    http_5xx_observed = 1 if (status_code is not None and status_code >= 500) else 0

    route = parsed.path or "/"
    if parsed.query:
        route += f"?{parsed.query}"

    viewports: list[dict[str, Any]] = [{"name": "desktop", "width": 1440, "height": 900}]
    if responsive_required:
        viewports.append({"name": "mobile", "width": 390, "height": 844})

    skeleton: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "route": route,
        "url": url,
        "role": role,
        "environment": environment.upper(),
        "responsive_required": responsive_required,
        "probe": {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "url": url,
            "status_code": status_code,
            "dom_ready": dom_ready,
            "content_type": content_type,
            "error": probe_error,
        },
        "viewports": viewports,
        "steps": [
            {
                "step_number": 1,
                "type": "open",
                "action": "navigate",
                "target": url,
                "status": "PASS" if status_code == 200 and dom_ready else "PENDING",
                "notes": "Initial route navigation and DOM readiness check",
            },
            {
                "step_number": 2,
                "type": "user_action",
                "action": "click",
                "target": "#primary-action",
                "status": "PENDING",
                "notes": "Simulated user interaction (click, type, or form submit)",
            },
            {
                "step_number": 3,
                "type": "visible_response",
                "action": "assert_visible",
                "target": "#visible-result",
                "status": "PENDING",
                "notes": "Confirmation of visual or DOM state update",
            },
            {
                "step_number": 4,
                "type": "terminal_state",
                "action": "state_check",
                "target": "completed",
                "status": "PENDING",
                "notes": "Final terminal state confirmation without errors",
            },
        ],
        "console_errors": 0,
        "http_5xx_count": http_5xx_observed,
        "screenshots": [],
    }

    if output_path:
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(skeleton, indent=2) + "\n", encoding="utf-8")

    return skeleton


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Browser UAT verifier and evidence check for accept-system"
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to execute")

    # validate-report subcommand
    val_parser = subparsers.add_parser(
        "validate-report", help="Validate browser UAT evidence report JSON"
    )
    val_parser.add_argument("report", nargs="?", help="Path to UAT report JSON file")
    val_parser.add_argument("--report", dest="report_opt", help="Path to UAT report JSON file")
    val_parser.add_argument(
        "--base-dir", help="Base directory for resolving relative screenshot paths"
    )
    val_parser.add_argument(
        "--json", action="store_true", help="Output validation result as JSON"
    )

    # probe-url subcommand
    probe_parser = subparsers.add_parser(
        "probe-url", help="Perform bounded loopback probe and write UAT skeleton"
    )
    probe_parser.add_argument("url", nargs="?", help="Loopback URL to probe")
    probe_parser.add_argument("--url", dest="url_opt", help="Loopback URL to probe")
    probe_parser.add_argument("--role", default="user", help="User role (default: user)")
    probe_parser.add_argument(
        "--environment",
        "--env",
        default="DEV",
        choices=["DEV", "TEST", "STAGING"],
        help="Target environment (default: DEV)",
    )
    probe_parser.add_argument(
        "--responsive", action="store_true", help="Require both desktop and mobile viewports"
    )
    probe_parser.add_argument(
        "--timeout", type=float, default=3.0, help="Probe timeout in seconds (default: 3.0)"
    )
    probe_parser.add_argument(
        "-o", "--output", help="Path to write structured UAT evidence skeleton JSON"
    )

    args = parser.parse_args(argv)

    if args.subcommand == "validate-report":
        report_path = args.report or args.report_opt
        if not report_path:
            sys.stderr.write("Error: validate-report requires a report file path\n")
            return 2
        p = Path(report_path)
        if not p.exists():
            sys.stderr.write(f"Error: Report file does not exist: {p}\n")
            return 2
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            sys.stderr.write(f"Error: Failed to parse JSON report: {exc}\n")
            return 2
        base_dir = args.base_dir or str(p.parent)
        res = validate_report(data, base_dir=base_dir)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            if res["valid"]:
                print("VALID: Browser UAT report passed all verification gates.")
            else:
                print(f"FAILED: Reason codes: {', '.join(res['reason_codes'])}")
                for err in res["errors"]:
                    print(f"  - {err}")
        return 0 if res["valid"] else 1

    elif args.subcommand == "probe-url":
        url = args.url or args.url_opt
        if not url:
            sys.stderr.write("Error: probe-url requires a loopback URL\n")
            return 2
        try:
            skeleton = probe_url(
                url=url,
                role=args.role,
                environment=args.environment,
                responsive_required=args.responsive,
                timeout=args.timeout,
                output_path=args.output,
            )
            if not args.output:
                print(json.dumps(skeleton, indent=2))
            else:
                print(f"UAT evidence skeleton written to {args.output}")
            return 0
        except Exception as exc:
            sys.stderr.write(f"Error: probe-url failed: {exc}\n")
            return 1

    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
