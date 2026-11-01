"""Offline lifecycle and explicit, parent-operated native UAT preparation.

Native execution requires an exported isolation runner. Environment variables
alone are not confinement. Runner evidence is retained for independent review,
never interpreted as public approval or independent product acceptance.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

from .configuration import validate_config, verify_portability
from .exporter import export_candidate, verify_export
from .privacy import _RECOGNIZED_CARETAKER_SOURCE, scan_tree
from .provenance import audit_provenance, write_notices

MAX_JSON_BYTES = 4 * 1024 * 1024
NATIVE_STAGES = ("office", "hub", "rsi", "graph", "fleet")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _encoded(data):
    return (json.dumps(data, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       indent=2) + "\n").encode("utf-8")


def _safe_path(value):
    """Inspect path components before following them; do not inspect auth stores."""
    supplied = os.fspath(value)
    if (supplied.startswith(("\\\\", "//")) or "\x00" in supplied
            or (os.sep != "\\" and "\\" in supplied)):
        raise ValueError("remote or invalid filesystem path refused")
    # Compare the supplied spelling before abspath/Path can erase aliases.
    literal_parts = supplied.replace(os.sep, "/").split("/")
    source_parts = _RECOGNIZED_CARETAKER_SOURCE.split("/")
    recognized_source = (
        literal_parts[-len(source_parts):] == source_parts
        and all(p not in {"", ".", ".."} and not p.endswith((" ", ".")) for p in literal_parts[1:])
        and (not literal_parts[0] or not literal_parts[0].endswith((" ", ".")))
    )
    path = Path(os.path.abspath(supplied))
    for index, part in enumerate(path.parts[1:], start=1):
        low = part.casefold()
        if (low.startswith((".env", "credentials"))
                or (low.startswith("secrets") and not (recognized_source and index == len(path.parts) - 1))
                or (low.startswith("auth") and low.endswith(".json"))
                or low in {".ssh", ".aws", ".gnupg", "vault", "raw", "proposals", "cookies", "cookies.sqlite",
                           ".git-credentials", ".netrc", "_netrc", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"}
                or Path(low).suffix in {".pem", ".key", ".p12", ".pfx"}):
            raise ValueError("sensitive input/output path refused")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if recognized_source:
                raise ValueError("recognized source must be an existing regular file") from None
            break
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked input/output path refused")
        if recognized_source and current == path and not stat.S_ISREG(info.st_mode):
            raise ValueError("recognized source must be an existing regular file")
    return path


def _relative(value):
    if (not isinstance(value, str) or not value or "\\" in value
            or any(c in value for c in ":\x00~$%") or value.startswith("/")):
        raise ValueError("literal relative path required")
    parts = value.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    if any(p.casefold() in {"", ".", "..", ".git"} or p.endswith((" ", "."))
           or p.split(".")[0].upper() in reserved for p in parts):
        raise ValueError("non-portable relative path refused")
    return value


def load_json(path):
    path = _safe_path(path)
    if not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("JSON input must be a bounded regular file")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise ValueError("JSON input exceeds size limit")
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("invalid UTF-8 JSON input") from None


def new_output(path, *, outside=None):
    path = _safe_path(path)
    if path.exists() or not path.parent.is_dir():
        raise ValueError("output must be new with an existing parent; choose a revision path")
    if outside is not None and path.is_relative_to(_safe_path(outside)):
        raise ValueError("reports must be outside the exact candidate inventory")
    return path


def write_json(path, data):
    payload = _encoded(data)
    if len(payload) > MAX_JSON_BYTES:
        raise ValueError("JSON report exceeds size limit")
    target = new_output(path)
    with target.open("xb") as stream:
        stream.write(payload)
    return {"bytes": len(payload), "sha256": _digest(payload)}


def bundle_check(candidate, receipt):
    """Local gates only. External and owner gates cannot be satisfied by tokens."""
    gates = {name: {"status": "UNKNOWN", "reason": "independent evidence not evaluated"}
             for name in ("independent_security_review", "real_secret_scanner",
                          "provenance_rights", "clean_profile_real_cli_uat")}
    gates.update({name: {"status": "PENDING", "reason": "explicit owner decision required"}
                  for name in ("license_owner_decision", "publisher_owner_decision",
                               "public_push_owner_approval")})
    details = {}
    try:
        root = _safe_path(candidate)
        details["export_integrity"] = verify_export(root, receipt)
        gates["export_integrity"] = {"status": "PASS"}
    except (ValueError, OSError, TypeError):
        gates["export_integrity"] = {"status": "FAIL", "reason": "exact inventory verification failed"}
        gates["local_privacy_precheck"] = {"status": "BLOCKED", "reason": "integrity failed"}
        gates["portability"] = {"status": "BLOCKED", "reason": "integrity failed"}
    else:
        for name, check in (("local_privacy_precheck", scan_tree), ("portability", verify_portability)):
            try:
                details[name] = check(root)
                gates[name] = {"status": details[name]["status"]}
            except (ValueError, OSError):
                gates[name] = {"status": "FAIL", "reason": "local check could not complete"}
    local_pass = all(gates[name]["status"] == "PASS" for name in
                     ("export_integrity", "local_privacy_precheck", "portability"))
    return {"version": 1, "status": "PENDING" if local_pass else "FAIL",
            "local_status": "PASS" if local_pass else "FAIL", "public_ready": False,
            "gates": gates, "details": details,
            "limitations": ["Local checks are not independent security or legal review.",
                            "No report, token file or native execution grants public approval."]}


def _environment(workspace):
    # Runtime/system access is distinct from private business state. Do not copy
    # ambient provider tokens, Office endpoints, Hub config or Git overrides.
    env = {key: os.environ[key] for key in ("PATH", "SystemRoot", "WINDIR", "COMSPEC")
           if key in os.environ}
    root = str(workspace)
    env.update({"HOME": root, "USERPROFILE": root, "APPDATA": root, "LOCALAPPDATA": root,
                "XDG_CONFIG_HOME": root, "XDG_DATA_HOME": root, "XDG_CACHE_HOME": root,
                "TMP": root, "TEMP": root, "TMPDIR": root, "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "KIMI_DISABLE_TELEMETRY": "1"})
    return env


def _process(argv, workspace, *, timeout=60):
    # Disk-backed capture avoids unbounded PIPE allocation. It is private task
    # evidence, never copied into stdout or a public candidate.
    with tempfile.TemporaryFile(dir=workspace) as out, tempfile.TemporaryFile(dir=workspace) as err:
        try:
            proc = subprocess.run(argv, cwd=workspace, env=_environment(workspace),
                                  stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                  shell=False, timeout=timeout,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            raise ValueError("native process timeout; preserved prior evidence; no automatic retry") from None
        except OSError:
            raise ValueError("process launch unavailable") from None
        out.seek(0)
        err.seek(0)
        stdout, stderr = out.read(MAX_JSON_BYTES + 1), err.read(MAX_JSON_BYTES + 1)
        if len(stdout) > MAX_JSON_BYTES or len(stderr) > MAX_JSON_BYTES:
            raise ValueError("process output exceeds capture limit")
        return proc.returncode, stdout, stderr


def _git(workspace, *args):
    code, out, _ = _process(["git", "-c", "core.hooksPath=", "-c", "init.templateDir=",
                            "-c", "core.autocrlf=false", "-C", str(workspace), *args], workspace)
    if code:
        raise ValueError("isolated Git command failed")
    return out.decode("utf-8").strip()


def offline_demo(workspace, source):
    """Two real Git-blob exports and the actual four library APIs, offline."""
    if not Path(workspace).is_absolute():
        raise ValueError("demo workspace must be explicit and absolute")
    root = _safe_path(workspace)
    fixture = _safe_path(source)
    if not root.is_absolute() or root.exists() or not root.parent.is_dir():
        raise ValueError("demo workspace must be a new explicit directory")
    if not fixture.is_file() or fixture.stat().st_size > 65536:
        raise ValueError("synthetic demo source must be a small regular file")
    content = fixture.read_bytes()
    root.mkdir()
    repo = root / "synthetic-repo"
    repo.mkdir()
    (repo / "synthetic-source.md").write_bytes(content)
    _git(repo, "init", "--quiet")
    _git(repo, "add", "--", "synthetic-source.md")
    _git(repo, "-c", "user.name=Synthetic Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Synthetic offline fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    manifest = {"version": 1, "max_total_bytes": 65536, "sources": [
        {"id": "synthetic", "repo": str(repo), "commit": commit, "files": [
            {"path": "synthetic-source.md", "target": "synthetic-source.md", "sha256": _digest(content)}]}]}
    write_json(root / "manifest.json", manifest)
    receipts = [export_candidate(manifest, root / name, allowed_output_parent=root)
                for name in ("candidate-a", "candidate-b")]
    for name, receipt in zip(("a", "b"), receipts):
        write_json(root / f"receipt-{name}.json", receipt)
    if receipts[0] != receipts[1]:
        raise ValueError("two-export reproducibility failed")
    config = {"version": 1, "stack_root": "candidate-a", "state_root": "local-state",
              "runtime_root": "local-runtime", "enabled_clis": [], "external_integrations": [],
              "network_integrations_enabled": False, "credential_references": {}}
    write_json(root / "config.json", config)
    config_result = validate_config(config, owned_root=root)
    components = [{"id": "synthetic", "targets": ["synthetic-source.md"], "source": "synthetic-fixture",
                   "license": "UNKNOWN", "notice_paths": [], "evidence": [], "status": "unknown"}]
    write_json(root / "components.json", components)
    rights = audit_provenance(manifest, components)
    write_json(root / "rights.json", rights)
    write_notices(rights, root / "notices.md")
    scan = scan_tree(root / "candidate-a")
    write_json(root / "scan.json", scan)
    bundle = bundle_check(root / "candidate-a", receipts[0])
    write_json(root / "bundle.json", bundle)
    return {"version": 1, "status": "PASS" if bundle["local_status"] == "PASS" else "FAIL",
            "kind": "offline-synthetic-only", "two_exports_equal": True, "tree_hash": receipts[0]["tree_hash"],
            "configuration": config_result["status"], "scan": scan["status"],
            "rights": rights["status"], "bundle": bundle["status"], "native_uat": "NOT_RUN",
            "product_acceptance": "UNKNOWN", "public_ready": False}


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("bounded identifier required")
    return value


def _entrypoint(candidate, receipt, value):
    name = _relative(value)
    if not name.endswith(".py") or name not in {r["target"] for r in receipt["files"]}:
        raise ValueError("native entrypoint must be an exported Python source file")
    path = _safe_path(candidate / name)
    if not path.is_file():
        raise ValueError("exported native entrypoint missing")
    return path


def _arguments(values, substitutions):
    if not isinstance(values, list) or len(values) > 128:
        raise ValueError("bounded native argv list required")
    result = []
    for value in values:
        if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
            raise ValueError("invalid native argument")
        if value in substitutions:
            result.append(substitutions[value])
        elif (not value or any(c in value for c in "{}~$%\\:") or value.startswith("/")
              or ".." in value.split("/") or "prod" in value.casefold()
              or "home" in value.casefold() or "remote" in value.casefold()):
            raise ValueError("native fallback, remote or production argument refused")
        else:
            result.append(value)
    return result


def native_uat(candidate, receipt, config, workspace, plan, *, execute=False):
    """Prepare, or run, a reviewed exported isolation runner and real mechanisms.

    A runner must implement the evidence protocol documented in demo/README.md.
    No runner is synthesized or silently replaced by a private host version.
    Claims in its evidence still require independent parent acceptance.
    """
    report = {"version": 1, "status": "BLOCKED", "stage": "candidate", "stages": [],
              "kind": "native-uat", "execution": "NOT_RUN", "product_acceptance": "UNKNOWN",
              "public_ready": False, "environment": {"os": os.name, "python": sys.version.split()[0]},
              "limitations": ["HOME redirection is not a sandbox; an exported OS isolation runner is required.",
                              "Native accounts may access authorized OS auth and system/runtime libraries.",
                              "Runner evidence is untrusted until independently reviewed; no public approval."]}
    try:
        candidate = _safe_path(candidate)
        verify_export(candidate, receipt)
        report["hashes"] = {"tree_hash": receipt["tree_hash"], "config_sha256": _digest(_encoded(config)),
                            "plan_sha256": _digest(_encoded(plan))}
        report["stage"] = "workspace"
        if not Path(workspace).is_absolute():
            raise ValueError("native workspace must be explicit and absolute")
        workspace = _safe_path(workspace)
        if (workspace == Path(workspace.anchor) or workspace.is_relative_to(candidate)
                or candidate.is_relative_to(workspace) or not workspace.is_dir()):
            raise ValueError("native workspace must be separate from exported code")
        # No home lookup. Host user/profile locations cannot serve as UAT workspaces.
        if any(p.casefold() in {"users", "home", ".agents", ".codex", ".claude"}
               for p in workspace.parts[1:]):
            raise ValueError("host profile or agent-state workspace refused")
        dotgit = _safe_path(workspace / ".git")
        if not dotgit.is_dir():
            raise ValueError("separate workspace needs its own Git directory, not a linked worktree")
        if Path(_git(workspace, "rev-parse", "--show-toplevel")).resolve() != workspace:
            raise ValueError("workspace is not the explicit repository root")
        head = _git(workspace, "rev-parse", "HEAD")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
            raise ValueError("workspace requires an existing Git commit")
        report["environment"]["workspace_git_head"] = head
        report["stage"] = "configuration"
        validate_config(config, owned_root=workspace)
        roots = [config[k] for k in ("stack_root", "state_root", "runtime_root")]
        for value in roots:
            _safe_path(workspace / _relative(value))
        folded_roots = [p.casefold() for p in roots]
        if any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
               for i, a in enumerate(folded_roots) for b in folded_roots[i + 1:]):
            raise ValueError("native config roots must not overlap")
        report["stage"] = "plan"
        if not isinstance(plan, dict) or set(plan) != {"version", "tree_hash", "roles", "isolation", "stages"}:
            raise ValueError("native plan has missing or unknown keys")
        if type(plan["version"]) is not int or plan["version"] != 1 or plan["tree_hash"] != receipt["tree_hash"]:
            raise ValueError("native plan must bind the verified export")
        roles = plan["roles"]
        if not isinstance(roles, dict) or set(roles) != {"maker", "reviewer"}:
            raise ValueError("maker and independent reviewer required")
        enabled = {(r["agent"], r["model"]) for r in config["enabled_clis"]}
        for role in roles.values():
            if not isinstance(role, dict) or set(role) != {"agent", "model", "lineage"}:
                raise ValueError("role needs explicit agent, model and lineage")
            for value in role.values():
                _identifier(value)
            if (role["agent"], role["model"]) not in enabled:
                raise ValueError("native role must name a configured CLI")
        if (roles["maker"]["lineage"].casefold() == roles["reviewer"]["lineage"].casefold()
                or roles["maker"]["agent"].casefold() == roles["reviewer"]["agent"].casefold()
                or roles["maker"]["model"].casefold() == roles["reviewer"]["model"].casefold()):
            raise ValueError("same-lineage reviewer refused")
        report["roles"] = roles
        report["stage"] = "isolation"
        isolation = plan["isolation"]
        if not isinstance(isolation, dict) or set(isolation) != {"entrypoint", "args"}:
            raise ValueError("exported OS isolation runner missing; environment alone is insufficient")
        runner = _entrypoint(candidate, receipt, isolation["entrypoint"])
        required_tokens = {"{entrypoint}", "{args_json}", "{workspace}", "{config}", "{evidence}", "{stage}"}
        if not isinstance(isolation["args"], list) or not required_tokens.issubset(isolation["args"]):
            raise ValueError("isolation runner argv must explicitly bind each execution input")
        report["stage"] = "stages"
        stages = plan["stages"]
        if (not isinstance(stages, list) or len(stages) != len(NATIVE_STAGES)
                or [s.get("id") if isinstance(s, dict) else None for s in stages] != list(NATIVE_STAGES)):
            raise ValueError("all five real mechanisms required in declared dependency order")
        prepared = []
        config_path = new_output(workspace / "native-config.json")
        for s in stages:
            if set(s) != {"id", "entrypoint", "args", "role", "expected", "evidence", "timeout_s"}:
                raise ValueError("native stage has missing or unknown keys")
            if s["role"] not in roles or type(s["timeout_s"]) is not int or not 1 <= s["timeout_s"] <= 240:
                raise ValueError("invalid native role or bounded timeout")
            _identifier(s["expected"])
            entry = _entrypoint(candidate, receipt, s["entrypoint"])
            evidence = new_output(workspace / _relative(s["evidence"]))
            if str(evidence).casefold() == str(config_path).casefold():
                raise ValueError("stage evidence collides with configuration")
            substitutions = {"{workspace}": str(workspace), "{candidate}": str(candidate),
                             "{config}": str(config_path), "{evidence}": str(evidence), "{stage}": s["id"]}
            args = _arguments(s["args"], substitutions)
            substitutions.update({"{entrypoint}": str(entry), "{args_json}": json.dumps(args)})
            argv = [sys.executable, "-B", str(runner), *_arguments(isolation["args"], substitutions)]
            prepared.append((s, entry, evidence, argv))
        if len({str(e).casefold() for _, _, e, _ in prepared}) != len(prepared):
            raise ValueError("native stages need distinct new evidence paths")
        if len({str(e) for _, e, _, _ in prepared}) != len(prepared) or runner in {e for _, e, _, _ in prepared}:
            raise ValueError("native mechanisms and isolation runner need distinct exported entrypoints")
        if stages[3]["role"] != "maker" or stages[4]["role"] != "reviewer":
            raise ValueError("Graph maker and independent Fleet reviewer execution required")
        report["hashes"]["isolation_runner_sha256"] = _digest(runner.read_bytes())
        report["stage"] = "execution"
        if not execute:
            report.update(status="PREPARED", reason="no native command executed; parent review and isolated execution pending")
            report["prepared_stages"] = [s["id"] for s, _, _, _ in prepared]
            return report
        write_json(config_path, config)
        report["execution"] = "STARTED"
        for s, entry, evidence_path, argv in prepared:
            report["stage"] = s["id"]
            record = {"stage": s["id"], "status": "BLOCKED", "role": s["role"],
                      "expected": s["expected"], "actual": "NOT_COMPLETED", "environment": report["environment"].copy(),
                      "hashes": {**report["hashes"], "entrypoint_sha256": _digest(entry.read_bytes())}}
            report["stages"].append(record)
            # Re-check code immediately before every invocation. No host implementation fallback.
            verify_export(candidate, receipt)
            new_output(evidence_path)
            code, out, err = _process(argv, workspace, timeout=s["timeout_s"])
            record["process"] = {"returncode": code, "stdout_bytes": len(out), "stderr_bytes": len(err),
                                 "stdout_sha256": _digest(out), "stderr_sha256": _digest(err)}
            verify_export(candidate, receipt)
            report["execution"] = "PARTIAL"
            if code:
                raise ValueError("exported native stage failed; no private host fallback")
            evidence = load_json(evidence_path)
            record["hashes"]["evidence_sha256"] = _digest(evidence_path.read_bytes())
            _check_native_evidence(evidence, s, roles[s["role"]], record, workspace)
            record.update(status=evidence["status"], actual=evidence["actual"])
            if evidence["status"] != "PASS":
                raise ValueError("native mechanism reported an incomplete stage")
        report.update(status="EXECUTED", stage="independent_acceptance", execution="COMPLETE",
                      reason="real process evidence captured; independent product acceptance remains UNKNOWN")
    except (ValueError, OSError, TypeError, KeyError) as exc:
        # Do not echo native logs, arbitrary JSON values or path-bearing OS errors.
        report["reason"] = str(exc) if type(exc) is ValueError else "native dependency or evidence unavailable"
        report["status"] = "BLOCKED"
    return report


def _check_native_evidence(e, stage, role, record, workspace):
    """Validate evidence shape/binding, without authenticating its assertions."""
    keys = {"version", "stage", "status", "role", "expected", "actual", "environment", "hashes",
            "checks", "repair", "enforcement"}
    if not isinstance(e, dict) or set(e) != keys or e["version"] != 1:
        raise ValueError("native evidence protocol incomplete")
    # evidence_sha256 is added by the caller and cannot be self-referential.
    expected_hashes = {k: v for k, v in record["hashes"].items() if k != "evidence_sha256"}
    if (e["stage"] != stage["id"] or e["role"] != stage["role"] or e["expected"] != stage["expected"]
            or e["status"] not in {"PASS", "FAIL", "BLOCKED"} or e["hashes"] != expected_hashes):
        raise ValueError("native evidence not bound to this stage and input hashes")
    actual = e["actual"]
    if (not isinstance(actual, dict) or set(actual) != {"agent", "model", "lineage", "run_id", "result"}
            or any(actual.get(k) != v for k, v in role.items())):
        raise ValueError("native identity differs from the declared independent roles")
    # An unavailable provider has no real run ID. Keep that fact explicit.
    if actual["run_id"] is not None or e["status"] == "PASS":
        _identifier(actual["run_id"])
    _identifier(actual["result"])
    environment = e["environment"]
    if (not isinstance(environment, dict) or environment.get("workspace_git_head") != record["environment"]["workspace_git_head"]
            or environment.get("private_business_state") not in {"DENIED", "NOT_ESTABLISHED"}
            or environment.get("production_endpoints") not in {"DENIED", "NOT_ESTABLISHED"}
            or environment.get("native_auth") not in {"OS_AUTH_PERMITTED", "NOT_USED", "UNAVAILABLE"}):
        raise ValueError("native business-state isolation evidence missing")
    if e["status"] == "PASS" and (environment["private_business_state"] != "DENIED"
                                    or environment["production_endpoints"] != "DENIED"
                                    or environment["native_auth"] == "UNAVAILABLE"):
        raise ValueError("native isolation or auth unavailable")
    enforcement = e["enforcement"]
    if enforcement is None and e["status"] != "PASS":
        pass  # A blocked containment launcher cannot supply an enforcement trace.
    else:
        if not isinstance(enforcement, dict) or set(enforcement) != {"mechanism", "artifact", "sha256"}:
            raise ValueError("real isolation enforcement artifact required")
        _identifier(enforcement["mechanism"])
        artifact = _safe_path(workspace / _relative(enforcement["artifact"]))
        if artifact == workspace / stage["evidence"] or not artifact.is_file() or artifact.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("isolation enforcement artifact unavailable")
        if not _SHA.fullmatch(str(enforcement["sha256"])) or _digest(artifact.read_bytes()) != enforcement["sha256"]:
            raise ValueError("isolation enforcement artifact hash mismatch")
    checks = e["checks"]
    if not isinstance(checks, list) or (e["status"] == "PASS" and not checks):
        raise ValueError("real native checks required")
    for check in checks:
        if (not isinstance(check, dict) or set(check) != {"name", "status"}
                or check["status"] not in {"PASS", "FAIL", "BLOCKED"}
                or (e["status"] == "PASS" and check["status"] != "PASS")):
            raise ValueError("native product check failed")
        _identifier(check["name"])
    repair = e["repair"]
    if (not isinstance(repair, dict) or set(repair) != {"status", "checks"}
            or repair["status"] not in {"NOT_NEEDED", "APPLIED", "NOT_RUN"} or not isinstance(repair["checks"], list)
            or (e["status"] == "PASS" and repair["status"] == "NOT_RUN")
            or (repair["status"] == "APPLIED" and not repair["checks"])):
        raise ValueError("native repair evidence incomplete")
    for name in repair["checks"]:
        _identifier(name)
    record["environment"] = {k: environment[k] for k in
                             ("workspace_git_head", "private_business_state", "production_endpoints", "native_auth")}
    record["checks"] = checks
    record["repair"] = repair
    record["enforcement"] = enforcement
    record["evidence_trust"] = "runner assertion and artifact; independent review required"
