"""Render and verify the pinned AI Lab Server package for the Office 24/7 cutover.

Step "Phase 1" of docs/agent-office/CUTOVER-RUNBOOK.md, made concrete in
docs/agent-office/SERVER-CUTOVER-PACKAGE.md. The package is an inert directory: exact runtime
bytes of the verified source package, system units, forced-command key templates, client
connection files and two install scripts. Rendering and verifying never touch a server, a
key, a service or an Office store; nothing is ever deleted or overwritten.

    python office_server_package.py render --out <new or empty dir> [--config <json>]
    python office_server_package.py verify --package <dir>

Each command prints one JSON object. Exit codes: 0 ok, 1 refused or mismatch, 2 usage.
`verify` is stdlib-only and runs on the server from the package's own copy of this file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

SCHEMA = "office.server-package/v1"
MANIFEST = "package-manifest.json"
TOOL = "office_server_package.py"
SNAPSHOT_TOOL = "office_store_snapshot.py"
UNITS = ("agent-office-wake-tick", "agent-office-wake-monitor")
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
# Same component rule the forced-command gate applies to --peer-host.
_COMPONENT = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_SESSION = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_ADDR = re.compile(r"[0-9a-fA-F:.,/]{1,200}\Z")
# systemd specifiers (%), quoting, shell metacharacters and whitespace are refused outright
# instead of escaped: every rendered path is used verbatim in units, key options and scripts.
_POSIX_PATH = re.compile(r"/[A-Za-z0-9._/-]{0,4000}\Z")
_WINDOWS_PATH = re.compile(r"[A-Za-z]:/[A-Za-z0-9._/-]{0,4000}\Z")


class Refused(Exception):
    """A safety rule refused the operation (exit 1)."""


@dataclass(frozen=True)
class Params:
    service_user: str = "agentoffice"
    python: str = "/usr/bin/python3.14"
    runtime_parent: str = "/opt/agent-office/runtime"
    state_root: str = "/var/lib/agent-office"
    server_peer_host: str = "office-server"
    ws_peer_host: str = "workstation"
    ws_from: str = "192.0.2.20"
    tick_interval_s: int = 60
    monitor_interval_s: int = 300
    server_ssh_host: str = "192.0.2.50"
    windows_client_dir: str = "C:/Example/office-client"
    windows_ssh: str = "C:/Windows/System32/OpenSSH/ssh.exe"
    hermes_home: str = "/srv/example/hermes/home"
    hermes_client_dir: str = "/srv/example/hermes/home/.agents/office-client"
    hermes_bin: str = "/usr/local/bin/hermes"
    hermes_ssh: str = "/usr/bin/ssh"
    hermes_session_id: str = "main"

    def check(self) -> None:
        if not _USER.fullmatch(self.service_user):
            raise Refused("service_user is not a plain system user name")
        for name in ("python", "runtime_parent", "state_root", "hermes_home", "hermes_client_dir", "hermes_ssh",
                     "hermes_bin"):
            value = getattr(self, name)
            if not _POSIX_PATH.fullmatch(value) or "//" in value or "/./" in value or "/../" in value \
                    or value.endswith(("/", "/.", "/..")):
                raise Refused(f"{name} must be a plain absolute POSIX path")
        for name in ("windows_client_dir", "windows_ssh"):
            value = getattr(self, name)
            if not _WINDOWS_PATH.fullmatch(value) or "//" in value or "/../" in value or value.endswith("/"):
                raise Refused(f"{name} must be a plain absolute Windows path with forward slashes")
        for name in ("server_peer_host", "ws_peer_host"):
            if not _COMPONENT.fullmatch(getattr(self, name)):
                raise Refused(f"{name} is not a canonical peer-host label")
        if self.server_peer_host == self.ws_peer_host:
            raise Refused("server and workstation need distinct peer-host labels")
        if not _SESSION.fullmatch(self.hermes_session_id):
            raise Refused("hermes_session_id is invalid")
        if not _ADDR.fullmatch(self.ws_from) or not _ADDR.fullmatch(self.server_ssh_host):
            raise Refused("ws_from/server_ssh_host must be literal addresses")
        for name in ("tick_interval_s", "monitor_interval_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 30 <= value <= 3600:
                raise Refused(f"{name} must be an integer 30..3600")
        roots = [PurePosixPath(self.runtime_parent), PurePosixPath(self.state_root)]
        if roots[0] == roots[1] or roots[0] in roots[1].parents or roots[1] in roots[0].parents:
            raise Refused("runtime_parent and state_root must be disjoint")


def load_params(path: Path | None) -> Params:
    if path is None:
        return Params()
    raw = path.read_bytes()
    if len(raw) > 16384:
        raise Refused("config larger than 16 KiB")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise Refused(f"duplicate config field {key!r}")
            out[key] = value
        return out

    data = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    known = {f.name for f in fields(Params)}
    if not isinstance(data, dict) or set(data) - known:
        raise Refused(f"unknown config fields: {sorted(set(data) - known) if isinstance(data, dict) else data!r}")
    return Params(**data)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_fingerprint(files: Mapping[str, bytes]) -> str:
    """Byte-identical to install_agent_office.tree_fingerprint_rows over a virtual tree."""
    digest = hashlib.sha256()
    for path in sorted(files):
        data = files[path]
        digest.update(path.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0"
                      + _sha(data).encode("ascii") + b"\n")
    return digest.hexdigest()


def _empty_real_dir(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            raise Refused(f"output is not a real directory: {path}")
        if any(path.iterdir()):
            raise Refused(f"output directory is not empty: {path}")
    else:
        path.mkdir(parents=True)
    return path.resolve(strict=True)


def _write_new(root: Path, rel: str, data: bytes, written: dict[str, bytes]) -> None:
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts or rel in written:
        raise Refused(f"unsafe or duplicate package path {rel!r}")
    target = root.joinpath(*pure.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "xb") as handle:
        handle.write(data)
    written[rel] = data


def _unit(p: Params, fp12: str, name: str, command: str, interval: int, boot: int) -> dict[str, bytes]:
    coord = f"{p.state_root}/coord"
    office = f"{p.runtime_parent}/{fp12}/office.py"
    service = f"""[Unit]
Description=Agent Office {command} (server store {coord})
After=local-fs.target
ConditionPathIsDirectory={coord}

[Service]
Type=oneshot
User={p.service_user}
Group={p.service_user}
UMask=0077
Environment=HOME={p.state_root}
WorkingDirectory={coord}
ExecStart={p.python} -B -I {office} --home {coord} --agent office-{p.server_peer_host}-{command} {command}{' --post' if command == 'wake-monitor' else ''}
TimeoutStartSec=45s
TimeoutStopSec=10s
Restart=no
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths={coord}
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
RestrictAddressFamilies=AF_UNIX
CapabilityBoundingSet=
MemoryMax=512M
"""
    timer = f"""[Unit]
Description=Agent Office {command} timer

[Timer]
OnBootSec={boot}s
OnUnitInactiveSec={interval}s
AccuracySec=5s
Unit={name}.service

[Install]
WantedBy=timers.target
"""
    return {f"server/systemd/{name}.service": service.encode(), f"server/systemd/{name}.timer": timer.encode()}


def _key_line(p: Params, fp12: str, principal: str, peer: str, source: str, placeholder: str) -> str:
    gate = f"{p.runtime_parent}/{fp12}/office_ssh_gate.py"
    return (f'restrict,from="{source}",command="{p.python} -B -I {gate} --principal {principal} '
            f'--home {p.state_root}/coord --peer-host {peer}" {placeholder}\n')


def _trust_prelude(usage: str, nargs: int) -> str:
    """Root-run preamble. The package is trusted only after an operator-supplied tree digest
    (printed by `render` on the workstation) matches a digest computed here with coreutils,
    in a root-owned location: code from the package is never used to vouch for itself."""
    return f"""set -euo pipefail
PKG="$(cd "$(dirname "${{BASH_SOURCE[0]}}")/.." && pwd -P)"
P="${{OFFICE_PKG_UAT_PREFIX:-}}"
fail() {{ echo "{{\\"status\\":\\"error\\",\\"code\\":\\"$1\\"}}"; exit 1; }}
[ $# -eq {nargs} ] || fail {usage}
[[ "$1" =~ ^[0-9a-f]{{64}}$ ]] || fail {usage}
if [ -z "$P" ] && [ "$(id -u)" != 0 ]; then fail not_root; fi
if [ -z "$P" ]; then
  d="$PKG"
  while :; do
    [ "$(stat -c %u -- "$d")" = 0 ] || fail package_not_in_root_owned_path
    [ $(( 8#$(stat -c %a -- "$d") & 8#022 )) -eq 0 ] || fail package_path_group_or_world_writable
    [ "$d" = / ] && break
    d="$(dirname -- "$d")"
  done
fi
[ -z "$(find "$PKG" ! -type f ! -type d -print -quit)" ] || fail package_special_file
tree_digest() {{
  (cd "$1" && find . -type f ! -path ./{MANIFEST} -printf '%P\\n' | LC_ALL=C sort | while IFS= read -r f; do
    printf '%s\\0%s\\0%s\\n' "$f" "$(stat -c %s -- "$f")" "$(sha256sum < "$f" | cut -d' ' -f1)"; done) \\
    | sha256sum | cut -d' ' -f1
}}
[ "$(tree_digest "$PKG")" = "$1" ] || fail package_digest_mismatch
"""


def _install_runtime_script(p: Params, fp12: str) -> str:
    units = " ".join(f"{u}.service {u}.timer" for u in UNITS)
    return f"""#!/bin/bash
# Phase 1 (prepare, INACTIVE) of SERVER-CUTOVER-PACKAGE.md. OWNER GATE: IAM + server services.
# Creates the service user, root-owned runtime, empty state directories and system units.
# Never deletes, never overwrites a different file, never enables or starts a unit, never
# touches keys, sshd or any existing service. UAT: OFFICE_PKG_UAT_PREFIX=<new dir> runs the
# same steps under that prefix without root, user creation, chown or systemctl.
# Usage (root, package copied into a root-owned directory first):
#   bash <pkg>/server/install-runtime.sh <package_tree_sha256 from render>
{_trust_prelude("usage_install_runtime_expected_tree_sha256", 1)}umask 022
PY={p.python}
if [ -n "$P" ]; then PY="$(command -v python3)"; fi
"$PY" -B -I "$PKG/{TOOL}" verify --package "$PKG" --expect-tree-sha256 "$1" >/dev/null || fail package_verify_failed
own() {{ if [ -z "$P" ]; then chown "$1" "$2"; fi; }}
if [ -z "$P" ]; then
  [ -x {p.python} ] || fail python_missing
  if ! getent passwd {p.service_user} >/dev/null; then
    useradd --system --user-group --home-dir {p.state_root} --no-create-home --shell /bin/sh {p.service_user}
  fi
  [ "$(getent passwd {p.service_user} | cut -d: -f6)" = {p.state_root} ] || fail service_user_home_mismatch
fi
RT="$P{p.runtime_parent}/{fp12}"
[ -e "$RT" ] && fail runtime_exists
install -d -m 0755 "$P{p.runtime_parent}"
own root:root "$P{p.runtime_parent}"
(cd "$PKG/runtime/{fp12}" && find . -type d -print0) | while IFS= read -r -d '' d; do
  install -d -m 0755 "$RT/$d"; own root:root "$RT/$d"; done
(cd "$PKG/runtime/{fp12}" && find . -type f -print0) | while IFS= read -r -d '' f; do
  install -m 0644 "$PKG/runtime/{fp12}/$f" "$RT/$f"; own root:root "$RT/$f"; done
TD="$P{PurePosixPath(p.runtime_parent).parent}/tools/{fp12}"
[ -e "$TD" ] && fail tools_exist
install -d -m 0755 "$P{PurePosixPath(p.runtime_parent).parent}/tools" "$TD"
own root:root "$P{PurePosixPath(p.runtime_parent).parent}/tools"; own root:root "$TD"
install -m 0644 "$PKG/{SNAPSHOT_TOOL}" "$TD/{SNAPSHOT_TOOL}"; own root:root "$TD/{SNAPSHOT_TOOL}"
S="$P{p.state_root}"
for d in "$S" "$S/.ssh"; do
  [ -e "$d" ] || install -d -m 0750 "$d"; own root:{p.service_user} "$d"; done
for d in "$S/coord" "$S/staging"; do
  if [ -e "$d" ]; then [ -z "$(ls -A "$d")" ] || fail state_dir_not_empty; else install -d -m 0700 "$d"; fi
  own {p.service_user}:{p.service_user} "$d"; done
U="$P/etc/systemd/system"
install -d -m 0755 "$U"
for u in {units}; do
  if [ -e "$U/$u" ]; then cmp -s "$PKG/server/systemd/$u" "$U/$u" || fail unit_differs; continue; fi
  install -m 0644 "$PKG/server/systemd/$u" "$U/$u"; own root:root "$U/$u"; done
if [ -z "$P" ]; then systemctl daemon-reload; fi
echo '{{"status":"ok","code":"prepared_inactive","runtime":"{p.runtime_parent}/{fp12}","snapshot_tool":"{PurePosixPath(p.runtime_parent).parent}/tools/{fp12}/{SNAPSHOT_TOOL}","units_enabled":false}}'
"""


def _install_keys_script(p: Params, fp12: str) -> str:
    ws_prefix = _key_line(p, fp12, "ws-office", p.ws_peer_host, p.ws_from,
                          "<WS_OFFICE_PUBLIC_KEY>").split("<WS_OFFICE_PUBLIC_KEY>")[0]
    hermes_prefix = _key_line(p, fp12, "hermes", p.server_peer_host, "127.0.0.1,::1",
                              "<HERMES_PUBLIC_KEY>").split("<HERMES_PUBLIC_KEY>")[0]
    return f"""#!/bin/bash
# OWNER GATE (keys): install the two forced-command public keys for {p.service_user}.
# Arguments are PUBLIC key files (one bare "ssh-ed25519 <base64> [comment]" line each).
# Private keys are never read, copied or printed. Refuses to replace a different file.
# Usage (root, package in a root-owned directory):
#   bash <pkg>/server/install-keys.sh <package_tree_sha256> <ws-office.pub> <hermes-office.pub>
{_trust_prelude("usage_expected_tree_sha256_ws_pub_hermes_pub", 3)}umask 077
shift
# Runs in this shell (no command substitution), so a refusal prints its JSON and exits.
key() {{
  [ -f "$1" ] || fail public_key_missing
  [ "$(wc -l < "$1")" -le 1 ] || fail public_key_not_single_line
  K="$(tr -d '\\r\\n' < "$1")"
  [[ "$K" =~ ^ssh-ed25519\\ [A-Za-z0-9+/]+=*(\\ [A-Za-z0-9@._-]+)?$ ]] || fail public_key_invalid
}}
key "$1"; WS="$K"; key "$2"; HE="$K"
[ "$WS" != "$HE" ] || fail keys_must_differ
T="$(mktemp)"
sed -e "s|<WS_OFFICE_PUBLIC_KEY>|$WS|" -e "s|<HERMES_PUBLIC_KEY>|$HE|" "$PKG/server/ssh/authorized_keys.template" > "$T"
WS_EXPECT='{ws_prefix}'"$WS"
HERMES_EXPECT='{hermes_prefix}'"$HE"
mapfile -t LINES < "$T"
[ "${{#LINES[@]}}" -eq 2 ] || fail authorized_keys_shape
[ "${{LINES[0]}}" = "$WS_EXPECT" ] || fail authorized_keys_ws_binding
[ "${{LINES[1]}}" = "$HERMES_EXPECT" ] || fail authorized_keys_hermes_binding
A="$P{p.state_root}/.ssh/authorized_keys"
if [ -e "$A" ]; then cmp -s "$T" "$A" || fail authorized_keys_differs; echo '{{"status":"ok","code":"already_installed"}}'; exit 0; fi
install -m 0640 "$T" "$A"
if [ -z "$P" ]; then chown root:{p.service_user} "$A"; fi
echo '{{"status":"ok","code":"keys_installed","path":"{p.state_root}/.ssh/authorized_keys"}}'
"""


def _source_commit(source: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=20, check=False)
        dirty = subprocess.run(["git", "-C", str(source), "status", "--porcelain", "--", "."],
                               capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    head = out.stdout.strip()
    if out.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        return "unknown"
    return head + ("+dirty" if dirty.returncode != 0 or dirty.stdout.strip() else "")


def _tool_commit() -> str:
    return _source_commit(Path(__file__).resolve().parent)


def render(out: Path, params: Params, source: Path | None = None,
           expect_runtime: str | None = None) -> dict[str, Any]:
    sys.dont_write_bytecode = True
    params.check()
    # All code runs from THIS trusted checkout. --source only supplies DATA (package.json and the
    # files it lists), which the trusted installer verifier checks; nothing from --source is
    # imported or executed, so a candidate cannot vouch for its own fingerprint.
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    import install_agent_office as installer
    if Path(installer.__file__).resolve().parent != here:
        raise Refused("installer was not imported from this tool's checkout")
    if source is not None:
        source = source.resolve(strict=True)
        if expect_runtime is None:
            raise Refused("--source requires --expect-runtime-fingerprint")
        if not (source / "package.json").is_file():
            raise Refused(f"not an Office source directory: {source}")
    from coordfs.office_cli_remote import parse_remote_config_bytes
    from coordfs.office_remote import OfficeRemoteConfig

    saved = (installer.SOURCE_ROOT, installer.PACKAGE_PATH, installer.SKILL_SOURCE_ROOT)
    try:
        if source is not None:
            installer.SOURCE_ROOT = source
            installer.PACKAGE_PATH = source / "package.json"
            installer.SKILL_SOURCE_ROOT = source / "skill" / installer.PACKAGE_ID
        package = installer.verify_package()
    finally:
        installer.SOURCE_ROOT, installer.PACKAGE_PATH, installer.SKILL_SOURCE_ROOT = saved
    if expect_runtime is not None and package.runtime_fingerprint != expect_runtime:
        raise Refused(f"source runtime {package.runtime_fingerprint} differs from the expected {expect_runtime}")
    runtime = dict(package.runtime_files)
    if tree_fingerprint(runtime) != package.runtime_fingerprint:
        raise Refused("runtime fingerprint algorithm drift")
    for required in ("office.py", "office_ssh_gate.py", "coordfs/office_ssh_gate.py", SNAPSHOT_TOOL):
        if required not in runtime:
            raise Refused(f"runtime lacks {required}")
    fp12 = package.runtime_fingerprint[:12]
    p = params
    files: dict[str, bytes] = {}
    for rel, data in runtime.items():
        files[f"runtime/{fp12}/{rel}"] = data
    files.update(_unit(p, fp12, UNITS[0], "wake-tick", p.tick_interval_s, 60))
    files.update(_unit(p, fp12, UNITS[1], "wake-monitor", p.monitor_interval_s, 300))
    files["server/ssh/authorized_keys.template"] = (
        _key_line(p, fp12, "ws-office", p.ws_peer_host, p.ws_from, "<WS_OFFICE_PUBLIC_KEY>")
        + _key_line(p, fp12, "hermes", p.server_peer_host, "127.0.0.1,::1", "<HERMES_PUBLIC_KEY>")
    ).encode()
    files["server/install-runtime.sh"] = _install_runtime_script(p, fp12).encode()
    files["server/install-keys.sh"] = _install_keys_script(p, fp12).encode()
    windows = {
        "host": p.server_ssh_host, "user": p.service_user,
        "identity_file": f"{p.windows_client_dir}/keys/ws-office-ed25519",
        "known_hosts_file": f"{p.windows_client_dir}/known_hosts",
        "ssh_executable": p.windows_ssh, "port": 22, "connect_timeout_s": 10, "request_timeout_s": 30,
    }
    hermes = {
        "host": "127.0.0.1", "user": p.service_user,
        "identity_file": f"{p.hermes_client_dir}/keys/hermes-office-ed25519",
        "known_hosts_file": f"{p.hermes_client_dir}/known_hosts",
        "ssh_executable": p.hermes_ssh, "port": 22, "connect_timeout_s": 10, "request_timeout_s": 30,
    }
    for label, config, native in (("windows", windows, os.name == "nt"), ("hermes", hermes, os.name != "nt")):
        data = (json.dumps(config, indent=2) + "\n").encode()
        if native:
            parse_remote_config_bytes(data)  # the exact closed parser the client runs
        else:
            # The client parser also requires paths native to ITS OS; off-platform the same
            # dataclass checks run and Params.check already pinned the absolute path shapes.
            OfficeRemoteConfig(**config)
        files[f"clients/{label}/office-remote.json"] = data
    args = ["-B", "-I", f"{p.runtime_parent}/{fp12}/office.py", "--remote-config",
            f"{p.hermes_client_dir}/office-remote.json", "--harness", "hermes",
            "--session-id", p.hermes_session_id, "mcp"]
    quoted = " ".join(args)
    files["clients/hermes/enroll-hermes-mcp.sh"] = f"""#!/bin/bash
# OWNER GATE (Hermes configuration + gateway restart). Native Hermes MCP registration of the
# pinned Office client for the Robert gateway ({p.hermes_home}/.hermes). Adds one entry;
# never removes or edits another MCP server. Restarting hermes-gateway.service is a separate
# approved step after this script prints the list/test result.
set -euo pipefail
export HOME={p.hermes_home} HERMES_HOME={p.hermes_home}/.hermes
if {p.hermes_bin} mcp list 2>/dev/null | grep -qw agent_office; then
  echo '{{"status":"error","code":"agent_office_already_configured"}}'; exit 1; fi
{p.hermes_bin} mcp add agent_office --connect-timeout 30 --command {p.python} --args {quoted}
{p.hermes_bin} mcp list
{p.hermes_bin} mcp test agent_office
""".encode()
    files["clients/hermes/known_hosts.template"] = b"127.0.0.1 <SERVER_HOST_ED25519_PUBLIC_KEY>\n"
    files["clients/windows/known_hosts.template"] = (
        f"{p.server_ssh_host} <SERVER_HOST_ED25519_PUBLIC_KEY>\n".encode())
    files[TOOL] = Path(__file__).read_bytes()
    # The restore tool comes from the verified runtime payload, so the runtime pin covers it.
    files[SNAPSHOT_TOOL] = runtime[SNAPSHOT_TOOL]

    root = _empty_real_dir(out)
    written: dict[str, bytes] = {}
    for rel in sorted(files):
        _write_new(root, rel, files[rel], written)
    manifest = {
        "schema": SCHEMA,
        "created_at": _utc(),
        "activation": "NOT_PERFORMED",
        "source_commit": _source_commit(source or here),
        "source_dir": str(source or here),
        "tool_commit": _tool_commit(),
        "office_package_version": package.manifest.get("package_version"),
        "office_runtime_version": package.manifest.get("runtime_version"),
        "package_fingerprint": package.fingerprint,
        "runtime_fingerprint": package.runtime_fingerprint,
        "runtime_dir": f"runtime/{fp12}",
        "params": asdict(p),
        "files": {rel: {"bytes": len(data), "sha256": _sha(data)} for rel, data in sorted(written.items())},
        "package_tree_sha256": tree_fingerprint(written),
    }
    with open(root / MANIFEST, "xb") as handle:
        handle.write((json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode())
    return {"status": "ok", "code": "package_rendered", "out": str(root), "files": len(written),
            "runtime_fingerprint": package.runtime_fingerprint, "source_commit": manifest["source_commit"],
            "package_tree_sha256": manifest["package_tree_sha256"]}


def verify(package_dir: Path, expect_tree_sha256: str | None = None) -> dict[str, Any]:
    root = package_dir.resolve(strict=True)
    manifest = json.loads((root / MANIFEST).read_bytes().decode("utf-8"))
    if manifest.get("schema") != SCHEMA or not isinstance(manifest.get("files"), dict):
        raise Refused("not an office server package manifest")
    listed: dict[str, dict[str, Any]] = manifest["files"]
    seen: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames) + filenames:
            full = Path(dirpath) / name
            info = os.lstat(full)
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise Refused(f"link or special file in package: {full.relative_to(root).as_posix()}")
        for name in filenames:
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            if rel == MANIFEST or "__pycache__" in rel.split("/"):
                if rel != MANIFEST:
                    raise Refused(f"bytecode cache in package: {rel}")
                continue
            seen[rel] = (root / rel).read_bytes()
    missing = sorted(set(listed) - set(seen))
    extra = sorted(set(seen) - set(listed))
    changed = sorted(rel for rel in set(listed) & set(seen)
                     if listed[rel] != {"bytes": len(seen[rel]), "sha256": _sha(seen[rel])})
    if missing or extra or changed:
        raise Refused(json.dumps({"missing": missing[:20], "extra": extra[:20], "changed": changed[:20]}))
    prefix = str(manifest["runtime_dir"]) + "/"
    runtime = {rel[len(prefix):]: data for rel, data in seen.items() if rel.startswith(prefix)}
    fingerprint = tree_fingerprint(runtime)
    if fingerprint != manifest["runtime_fingerprint"] or prefix != f"runtime/{fingerprint[:12]}/":
        raise Refused("runtime fingerprint mismatch")
    tree = tree_fingerprint(seen)
    if tree != manifest.get("package_tree_sha256"):
        raise Refused("package tree digest mismatch")
    if expect_tree_sha256 is not None and tree != expect_tree_sha256:
        raise Refused("package tree digest differs from the expected pin")
    return {"status": "ok", "code": "package_verified", "files": len(seen),
            "runtime_fingerprint": fingerprint, "source_commit": manifest.get("source_commit"),
            "package_tree_sha256": tree}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="office-server-package")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("render")
    r.add_argument("--out", required=True, type=Path)
    r.add_argument("--config", type=Path)
    r.add_argument("--source", type=Path, help="sealed Office source dir (default: this tool's dir)")
    r.add_argument("--expect-runtime-fingerprint", help="refuse unless the source runtime has this fingerprint")
    v = sub.add_parser("verify")
    v.add_argument("--package", required=True, type=Path)
    v.add_argument("--expect-tree-sha256", help="pin printed by render on the workstation")
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            result = render(args.out, load_params(args.config), args.source, args.expect_runtime_fingerprint)
        else:
            result = verify(args.package, args.expect_tree_sha256)
    except (Refused, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "error", "code": "refused", "reason": str(exc)[:2000]}))
        return 1
    except Exception as exc:  # installer DeployError / CoordError from the source package
        print(json.dumps({"status": "error", "code": "source_refused",
                          "reason": f"{type(exc).__name__}: {str(exc)[:500]}"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
