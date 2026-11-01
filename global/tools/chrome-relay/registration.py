"""Scoped, lossless registration for managed Chrome relay (stdlib only).

Enrollment contains exactly harness, transport, launcher (absolute managed.py)
and config (absolute owner-private managed config), optionally native_format and
Codex-only required. The required flag makes codex exec wait for MCP startup.
No capability, grant or native user config is discovered or read implicitly.
Known layouts: Codex TOML mcp_servers, Claude JSON mcpServers and ZCode/GLM
JSON mcp.servers. Other enrolled harnesses need an explicit, owner-verified
native_format=mcpServers-json declaration; unknown layouts fail honestly.

CLI fallback is a separate chrome-relay-cli-assignment/1 JSON artifact, never
an MCP server entry. Its managed.py cli consumes semantic JSON lines on stdin.
Registration validates setup metadata only. The owner separately runs the
fixed `managed.py validate --config PATH` before activation; no browser or
controller is started here. Synthetic registration does not prove discovery.

Only the managed entry's text span changes. apply retains immutable byte-exact
preimages, refuses stale/conflicting state and never deletes files (failed
staging files remain). No secrets or sibling configuration values in receipts.
"""

import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from broker import MAX_BODY, Reject, exact, parse_json

VERSION = "1.2.0"
SERVER_KEY = "fleet-chrome-relay"
TRANSPORTS = ("mcp", "cli")
CLI_SCHEMA = "chrome-relay-cli-assignment/1"
KNOWN_HARNESSES = frozenset((
    "codex", "claude", "kimi", "glm", "qwen", "grok", "minimax",
    "mistral", "muse", "codebuddy", "antigravity", "mimo", "openclaw",
))
HARNESS_ALIASES = {"zcode": "glm", "glm/zcode": "glm"}
_NATIVE_FORMATS = {"codex": "codex-toml", "claude": "mcpServers-json", "glm": "zcode-json"}
_PATHS = {"codex-toml": ("mcp_servers",), "mcpServers-json": ("mcpServers",),
          "zcode-json": ("mcp", "servers"), "cli-assignment-json": ("assignments",)}


def _absolute(value):
    # Windows isabs also accepts root-relative paths on older Python releases.
    if (not isinstance(value, str) or not value or any(ord(c) < 32 for c in value)
            or not Path(value).is_absolute() or ".." in Path(value).parts):
        raise Reject("bound")
    return value


def normalize_harness(name):
    if not isinstance(name, str):
        raise Reject("unknown_harness")
    key = HARNESS_ALIASES.get(name.strip().lower(), name.strip().lower())
    if key not in KNOWN_HARNESSES:
        raise Reject("unknown_harness")
    return key


def validate_enrollment(record):
    """Validate setup metadata only; never open the private managed config."""
    exact(record, ("harness", "transport", "launcher", "config"), ("native_format", "required"))
    if record["transport"] not in TRANSPORTS:
        raise Reject("unknown_transport")
    launcher = _absolute(record["launcher"])
    if Path(launcher).name != "managed.py":
        raise Reject("launcher")
    result = {"harness": normalize_harness(record["harness"]), "transport": record["transport"],
              "launcher": launcher, "config": _absolute(record["config"])}
    if "native_format" in record:
        fmt = record["native_format"]
        expected = _NATIVE_FORMATS.get(result["harness"], "mcpServers-json")
        if fmt != expected:
            raise Reject("unknown_native_format")
        result["native_format"] = fmt
    if "required" in record:
        if type(record["required"]) is not bool:
            raise Reject("required")
        try:
            fmt, _ = _layout(result)
        except Reject:
            raise Reject("required_unsupported") from None
        if fmt != "codex-toml":
            raise Reject("required_unsupported")
        result["required"] = record["required"]
    return result


def validate_enrollments(records):
    if not isinstance(records, list):
        raise Reject("unknown_field")
    ok, errors = [], []
    for index, record in enumerate(records):
        try:
            ok.append(validate_enrollment(record))
        except Reject as exc:
            errors.append({"index": index, "reason": exc.reason})
    return {"ok": ok, "errors": errors}


def fallback_record(enrollment):
    """An explicit assignment proposal; managed grant must enroll cli too."""
    result = validate_enrollment(enrollment)
    result["transport"] = "cli"
    result.pop("native_format", None)
    result.pop("required", None)
    return result


def overlay_entry(enrollment, python_exe):
    result = validate_enrollment(enrollment)
    entry = {"command": _absolute(python_exe),
             "args": ["-B", result["launcher"], result["transport"], "--config",
                      result["config"], "--harness", result["harness"]]}
    if "required" in result:
        entry["required"] = result["required"]
    return {SERVER_KEY: entry}


def validation_argv(enrollment, python_exe):
    result = validate_enrollment(enrollment)
    return [_absolute(python_exe), "-B", result["launcher"], "validate", "--config", result["config"]]


def _layout(record):
    if record["transport"] == "cli":
        return "cli-assignment-json", "assigned-semantic-cli"
    fmt = _NATIVE_FORMATS.get(record["harness"])
    if fmt is not None:
        return fmt, "native-adapter"
    if "native_format" not in record:
        raise Reject("unknown_native_format")
    return record["native_format"], "enrollment-declaration"


def canonical_bytes(config):
    """Fixture/new-document encoding only; never used to reformat existing files."""
    return (json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def plan(existing, enrollment, python_exe):
    record = validate_enrollment(enrollment)
    fmt, evidence = _layout(record)
    existing = {} if existing is None else existing
    if not isinstance(existing, dict):
        raise Reject("scope")
    if fmt == "cli-assignment-json" and existing:
        exact(existing, ("schema", "assignments"))
        if existing["schema"] != CLI_SCHEMA:
            raise Reject("scope")
    merged = copy.deepcopy(existing)
    if fmt == "cli-assignment-json":
        merged["schema"] = CLI_SCHEMA
    servers = merged
    for key in _PATHS[fmt]:
        servers = servers.setdefault(key, {})
        if not isinstance(servers, dict):
            raise Reject("scope")
    if SERVER_KEY in servers and not isinstance(servers[SERVER_KEY], dict):
        raise Reject("scope")
    detail = {"harness": record["harness"], "transport": record["transport"],
              "native_format": fmt, "format_evidence": evidence,
              "scope": list(_PATHS[fmt]) + [SERVER_KEY],
              "updated": SERVER_KEY in servers,
              "preserved_keys": sorted(key for key in servers if key != SERVER_KEY)}
    servers[SERVER_KEY] = overlay_entry(record, python_exe)[SERVER_KEY]
    return merged, merged != existing, detail


def _decode(data):
    if not isinstance(data, bytes) or len(data) > MAX_BODY:
        raise Reject("bound")
    try:
        return data.decode("utf-8-sig"), b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    except UnicodeError:
        raise Reject("config") from None


def _parse(data, fmt):
    text, _ = _decode(data)
    if fmt != "codex-toml":
        return parse_json(text.encode("utf-8"))
    try:
        return tomllib.loads(text)
    except (ValueError, RecursionError):
        raise Reject("config") from None


def _compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _members(text, start):
    """Find JSON member value spans without rebuilding any sibling text."""
    if text[start] != "{":
        raise Reject("scope")
    decoder = json.JSONDecoder()
    pos, members = start + 1, {}
    while True:
        while text[pos] in " \t\r\n":
            pos += 1
        if text[pos] == "}":
            return members, pos
        key, pos = decoder.raw_decode(text, pos)
        while text[pos] in " \t\r\n":
            pos += 1
        if text[pos] != ":":
            raise Reject("scope")
        pos += 1
        while text[pos] in " \t\r\n":
            pos += 1
        begin = pos
        _, pos = decoder.raw_decode(text, pos)
        members[key] = (begin, pos)
        while text[pos] in " \t\r\n":
            pos += 1
        if text[pos] == ",":
            pos += 1
        elif text[pos] != "}":
            raise Reject("scope")


def _json_patch(data, merged, scope):
    text, bom = _decode(data)
    start = len(text) - len(text.lstrip(" \t\r\n"))
    current = merged
    for index, key in enumerate(scope):
        members, closing = _members(text, start)
        if key not in members:
            # Insert after the last value, keeping the original trailing space.
            where = next(reversed(members.values()))[1] if members else start + 1
            inserted = ("," if members else "") + _compact(key) + ":" + _compact(current[key])
            if index == 0 and "schema" in merged and not members:
                inserted = '"schema":' + _compact(merged["schema"]) + "," + inserted
            return bom + (text[:where] + inserted + text[where:]).encode("utf-8")
        begin, end = members[key]
        if index == len(scope) - 1:
            return bom + (text[:begin] + _compact(current[key]) + text[end:]).encode("utf-8")
        start, current = begin, current[key]
    raise Reject("scope")


def _toml_headers(text):
    """Locate real TOML headers, ignoring comments, strings and array values."""
    headers, quote, depth, pos = [], None, 0, 0
    for line in text.splitlines(keepends=True):
        if quote is None and depth == 0:
            match = re.fullmatch(r'[ \t]*(\[.+\])[ \t]*(?:#[^\r\n]*)?[\r\n]*', line)
            if match:
                header = match.group(1)
                try:
                    node = tomllib.loads(header + "\n__relay_probe__ = true\n")
                    path = []
                    while isinstance(node, dict) and "__relay_probe__" not in node:
                        if len(node) != 1:
                            raise Reject("scope")
                        key, node = next(iter(node.items()))
                        path.append(key)
                    if not isinstance(node, dict):
                        raise Reject("scope")  # Array tables require another native adapter.
                    headers.append((pos, tuple(path)))
                    pos += len(line)
                    continue
                except ValueError:
                    raise Reject("scope") from None
        index = 0
        while index < len(line):
            char = line[index]
            if quote:
                if quote[0] == '"' and char == "\\":
                    index += 2
                    continue
                if line.startswith(quote, index):
                    index += len(quote)
                    quote = None
                    continue
            elif char == "#":
                break
            elif char in "\"'":
                quote = char * 3 if line.startswith(char * 3, index) else char
                index += len(quote)
                continue
            elif char in "[{":
                depth += 1
            elif char in "]}":
                depth -= 1
            index += 1
        pos += len(line)
    return headers


def _toml_patch(data, merged, scope):
    text, bom = _decode(data)
    headers = _toml_headers(text)
    path = tuple(scope)
    ranges = []
    for index, (begin, candidate) in enumerate(headers):
        if candidate[:len(path)] == path:
            end = headers[index + 1][0] if index + 1 < len(headers) else len(text)
            ranges.append((begin, end))
    if ranges and not any(candidate == path for _, candidate in headers):
        raise Reject("scope")
    if any(left[1] != right[0] for left, right in zip(ranges, ranges[1:])):
        raise Reject("scope")  # Do not consume interleaved unrelated tables.
    # Inline/dotted assignments cannot safely be replaced by a table span.
    existing = _parse(data, "codex-toml")
    if "mcp_servers" in existing and not any(candidate[:1] == ("mcp_servers",) for _, candidate in headers):
        raise Reject("scope")
    servers = existing.get("mcp_servers", {})
    if SERVER_KEY in servers and not ranges:
        raise Reject("scope")
    entry = merged["mcp_servers"][SERVER_KEY]
    newline = "\r\n" if "\r\n" in text else "\n"
    block = "[mcp_servers." + _compact(SERVER_KEY) + "]" + newline
    block += "command = " + _compact(entry["command"]) + newline
    block += "args = " + _compact(entry["args"]) + newline
    if "required" in entry:
        block += "required = " + ("true" if entry["required"] else "false") + newline
    if ranges:
        begin, end = ranges[0][0], ranges[-1][1]
        text = text[:begin] + block + text[end:]
    else:
        text += (newline if text and not text.endswith(("\n", "\r")) else "") + block
    return bom + text.encode("utf-8")


def _prepare(original, enrollment, python_exe):
    record = validate_enrollment(enrollment)
    fmt, _ = _layout(record)
    existing = None if original is None else _parse(original, fmt)
    merged, changed, detail = plan(existing, record, python_exe)
    if not changed:
        result = original
    elif original is None:
        result = _toml_patch(b"", merged, detail["scope"]) if fmt == "codex-toml" else canonical_bytes(merged)
    elif fmt == "codex-toml":
        result = _toml_patch(original, merged, detail["scope"])
    else:
        result = _json_patch(original, merged, detail["scope"])
    if _parse(result, fmt) != merged:
        raise Reject("scope")  # Verify the exact parsed mutation, not just the text operation.
    return result, changed, detail


def _receipt(result, changed, detail, enrollment, python_exe):
    return {**detail, "changed": changed, "config_sha256": hashlib.sha256(result).hexdigest(),
            "config_bytes": len(result), "files_written": 0, "files_deleted": 0,
            "native_mcp_modified": False, "discovery_verified": False,
            "managed_policy_validated": False, "relay_version": VERSION,
            "overlay": overlay_entry(enrollment, python_exe),
            "validation_argv": validation_argv(enrollment, python_exe)}


def dry_run(existing_bytes, enrollment, python_exe):
    result, changed, detail = _prepare(existing_bytes, enrollment, python_exe)
    return {"status": "DRY_RUN", **_receipt(result, changed, detail, enrollment, python_exe)}


def _read_bounded(path):
    with open(path, "rb") as stream:
        data = stream.read(MAX_BODY + 1)
    if len(data) > MAX_BODY:
        raise Reject("bound")
    return data


def _preimage(path, original):
    if os.path.lexists(path):
        if Path(path).is_symlink() or not Path(path).is_file() or _read_bounded(path) != original:
            raise Reject("preimage")
        return False
    try:
        with open(path, "xb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if Path(path).is_symlink() or _read_bounded(path) != original:
            raise Reject("preimage") from None
        return False
    return True


def _atomic_write(path, data, original):
    if original is None:
        # Exclusive creation never overwrites a file appearing after the plan.
        try:
            with open(path, "xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise Reject("concurrent_change") from None
        return
    fd, staged = tempfile.mkstemp(dir=str(Path(path).parent), prefix=".relay-", suffix=".tmp")
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    # Failed staging is retained; neither preimages nor failure artifacts are deleted.
    if Path(path).is_symlink() or not Path(path).is_file() or _read_bounded(path) != original:
        raise Reject("concurrent_change")
    os.replace(staged, path)


def _target(config_path, record):
    target = _absolute(os.fspath(config_path))
    fmt, _ = _layout(record)
    suffix = ".toml" if fmt == "codex-toml" else ".json"
    if Path(target).suffix.lower() != suffix or Path(target).is_symlink():
        raise Reject("scope")
    if os.path.lexists(target) and not os.path.isfile(target):
        raise Reject("scope")
    if Path(target).resolve() == Path(record["config"]).resolve() or Path(target).resolve() == Path(record["launcher"]).resolve():
        raise Reject("scope")  # Never mistake owner policy/launcher for the native target.
    return target


def apply(config_path, enrollment, python_exe):
    record = validate_enrollment(enrollment)
    target = _target(config_path, record)
    original = _read_bounded(target) if os.path.isfile(target) else None
    result, changed, detail = _prepare(original, record, python_exe)
    receipt = {"status": "NOOP", "config_path": target, **_receipt(result, changed, detail, record, python_exe),
               "preimage_path": None, "preimage_sha256": None, "preimages_written": 0}
    if not changed:
        return receipt
    if not os.path.isfile(record["launcher"]) or not os.path.isfile(_absolute(python_exe)):
        raise Reject("bound")
    if original is not None:
        digest = hashlib.sha256(original).hexdigest()
        preimage = target + ".preimage-" + digest
        created = _preimage(preimage, original)
        receipt.update(preimage_path=preimage, preimage_sha256=digest, preimages_written=int(created))
    _atomic_write(target, result, original)
    receipt.update(status="APPLIED", files_written=1 + receipt["preimages_written"],
                   native_mcp_modified=record["transport"] == "mcp")
    return receipt


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="registration.py", allow_abbrev=False)
    parser.add_argument("command", choices=("dry-run", "apply", "validate"))
    parser.add_argument("--enrollment", required=True, help="Absolute setup metadata JSON; no capabilities.")
    parser.add_argument("--config", help="Explicit native config, or a separate CLI assignment JSON artifact.")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)
    try:
        enrollment = parse_json(_read_bounded(_absolute(args.enrollment)))
        if args.command == "validate":
            checked = validate_enrollments(enrollment if isinstance(enrollment, list) else [enrollment])
            report = {"status": "INVALID" if checked["errors"] else "VALIDATED", "errors": checked["errors"],
                      "ok": [{"harness": item["harness"], "transport": item["transport"]} for item in checked["ok"]],
                      "managed_policy_validated": False, "discovery_verified": False}
        else:
            if args.config is None:
                raise Reject("scope")
            if args.command == "dry-run":
                target = _target(args.config, validate_enrollment(enrollment))
                report = dry_run(_read_bounded(target) if os.path.isfile(target) else None, enrollment, args.python)
            else:
                report = apply(args.config, enrollment, args.python)
        sys.stdout.write(json.dumps(report, ensure_ascii=True, sort_keys=True) + "\n")
        return int(report["status"] == "INVALID")
    except Reject as exc:
        sys.stderr.write(json.dumps({"status": "denied", "error_type": exc.reason}) + "\n")
        return 1
    except (OSError, ValueError, TypeError, RecursionError):
        sys.stderr.write(json.dumps({"status": "denied", "error_type": "registration_io"}) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
