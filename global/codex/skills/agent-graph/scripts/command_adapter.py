"""Runner protocol adapter for a pinned command plus explicit deterministic oracles.

Supports existing Fleet run/quality/security commands, not another model router.
An oracle is executable acceptance evidence, not an invented success threshold.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
sys.dont_write_bytecode = True
import time

from graph_runner import read, write, verify_pins
import agent_graph as graph


def canonical_quota_value(value):
    """Serialize object quotas as canonical JSON strings; preserve scalar/null."""
    if isinstance(value, dict):
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    return value


def expand_worker_run_ids(argv, refs):
    """Expand the graph worker marker into parser-safe repeated run flags."""
    worker_ids = sorted({ref["id"] for ref in refs if ref["kind"] == "fleet_run"})
    expanded = []
    for arg in argv:
        if arg != "{worker_run_ids}":
            expanded.append(arg)
            continue
        if expanded and expanded[-1] == "--run-id":
            expanded.pop()
            for worker_id in worker_ids:
                expanded.extend(("--run-id", worker_id))
        else:
            expanded.extend(worker_ids)
    return expanded


def unique_external_refs(refs):
    """Keep the first occurrence of each graph external reference."""
    unique = []
    seen = set()
    for ref in refs:
        key = (ref["kind"], ref["id"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def run_recipe(path):
    recipe = read(path)
    context = read(os.environ["AGENT_GRAPH_CONTEXT_PATH"])
    node = context["node"]
    workspace = Path.cwd().resolve()
    attempt = os.environ["AGENT_GRAPH_ATTEMPT_ID"]
    result_path = Path(os.environ["AGENT_GRAPH_EVIDENCE_PATH"])
    if set(recipe) != {"argv", "pinned_files", "oracles", "artifact_paths"}:
        raise ValueError("Invalid execution recipe")
    verify_pins(recipe["pinned_files"])
    if set(recipe["oracles"]) != {item["criterion_id"] for item in node["acceptance_criteria"]}:
        raise ValueError("Every criterion needs its own explicit oracle")
    refs = unique_external_refs(
        [
            ref
            for key in node["evidence_from"]
            for ref in context["state"]["nodes"][key]["external_refs"]
            if ref["kind"] in {"fleet_run", "agent_loop"}
        ]
    )

    def invoke(argv, label):
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise ValueError("Commands must be argv arrays")
        executable = Path(argv[0])
        if not executable.is_absolute() or str(executable) not in recipe["pinned_files"]:
            raise ValueError("Recipe executable must be absolute and pinned")
        if executable.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise ValueError("Explicit interpreter required")
        # One token expands into the covered run IDs; never shell interpolation.
        expanded = expand_worker_run_ids(argv, refs)
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        with (result_path.parent / (label + ".out")).open("wb") as out, (result_path.parent / (label + ".err")).open("wb") as err:
            result = subprocess.run(expanded, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    timeout=context["dispatch_bounds"]["timeout"], **options)
        return result.returncode

    began = time.monotonic()
    code = invoke(recipe["argv"], "execute")
    external = None
    if node["executor"].startswith("fleet-"):
        output = read(result_path.parent / "execute.out")
        record_path = Path(output.get("usage_record") or output.get("record_path") or "")
        if not record_path.is_absolute() or not record_path.is_file():
            raise ValueError("Fleet did not return a durable receipt")
        external = read(record_path)
        if node["executor"] == "fleet-run":
            if external.get("run_id") != output.get("run_id"):
                raise ValueError("Fleet output and receipt disagree")
            if external.get("run_status") not in {"COMPLETED", "PARTIAL"}:
                raise ValueError("Fleet run not a usable candidate")
            identity = external.get("model_identity") or {}
            identity_verified = identity.get("status") == "verified"
            # Codex is the first-party local executor. Fleet can attest its
            # selected model and runtime, but cannot observe a provider model
            # identifier from the nested Codex app, so it reports
            # not-applicable. Keep this exception explicit and narrow.
            codex_identity_attested = (
                external.get("agent") == "codex"
                and identity.get("status") == "not-applicable"
                and identity.get("expected_model") == external.get("model")
                and isinstance(external.get("model"), str)
                and bool(external.get("model"))
            )
            # Kimi CLI 0.43.1 omits model IDs from stream-json. Owner decision
            # 2026-09-24: accept protected config attestation only until CLI
            # exposes a model ID; then require verified identity instead.
            kimi_identity_attested = (
                external.get("agent") == "kimi"
                and identity.get("status") == "not-applicable"
                and isinstance(identity.get("expected_model"), str)
                and bool(identity.get("expected_model"))
                and identity.get("expected_model") == external.get("model")
                and not identity.get("observed_models")
                and not identity.get("observed_runtime_models")
                and (external.get("runtime_attestation") or {}).get("status") == "VERIFIED"
                and external.get("model_provider_id") == "moonshot"
            )
            if not (identity_verified or codex_identity_attested or kimi_identity_attested):
                raise ValueError("Fleet model identity unverified")
            if (external.get("runtime_attestation") or {}).get("status") != "VERIFIED":
                raise ValueError("Fleet runtime is not verified")
            if Path(external.get("execution_workspace", "")).resolve() != workspace:
                raise ValueError("Fleet used another workspace; use fleet_bound.py for pre-bound implementation")
            disposition = external.get("artifact_disposition")
            if disposition is not None and (not isinstance(disposition, dict) or disposition.get("status") != "preserved"):
                raise ValueError("Fleet artifacts are not eligible for independent review")
            # Fleet exits 1 for PARTIAL. This is a candidate, not a failed oracle:
            # actual tests and the existing downstream reviewer decide usability.
            if external.get("run_status") == "PARTIAL" and code == 1:
                code = 0
            refs = unique_external_refs(refs + [{"kind": "fleet_run", "id": external["run_id"]}])
        else:
            if not (external.get("independence") or {}).get("passed"):
                raise ValueError("Independent gate not proven")
            expected = {"fleet-quality-gate": "quality", "fleet-security-gate": "security"}[node["executor"]]
            if external.get("gate_id") != output.get("gate_id"):
                raise ValueError("Gate output and receipt disagree")
            if external.get("gate_kind") != expected:
                raise ValueError("Wrong gate kind")
            covered = {row["run_id"] for row in external.get("workers", [])}
            if {ref["id"] for ref in refs if ref["kind"] == "fleet_run"} - covered:
                raise ValueError("Gate omits covered workers")
            refs = unique_external_refs(
                refs
                + [
                    {
                        "kind": graph.EXPECTED_EXTERNAL_REF[node["executor"]],
                        "id": external["gate_id"],
                    }
                ]
            )
            if external.get("validation_error") or external.get("verdict") != "ACCEPT":
                code = 1
    elif node["executor"] != "parent-local":
        raise ValueError("Use a certified executor-specific adapter for " + node["executor"])

    criteria, checks = [], []
    for criterion, argv in recipe["oracles"].items():
        passed = invoke(argv, criterion) == 0
        status = "PASS" if passed and code == 0 else "FAIL"
        reference = "runner:" + attempt + ":" + criterion
        criteria.append({"criterion_id": criterion, "status": status, "evidence_ref": reference})
        checks.append({"name": criterion, "status": status, "evidence_ref": reference})
    artifacts = []
    for relative in recipe["artifact_paths"]:
        file = (workspace / relative).resolve()
        file.relative_to(workspace)
        payload = file.read_bytes()
        artifacts.append({"path": relative, "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)})
    verdict = "accepted" if all(item["status"] == "PASS" for item in criteria) else "rejected"
    evidence = {"schema_version": 1, "evidence_id": "evidence-" + attempt, "node_id": node["node_id"],
        "verdict": verdict, "summary": "Pinned command and explicit criterion oracles; independent downstream gates remain required."
            + (" Fleet candidate status: " + external["run_status"] + "." if external and external.get("run_status") else ""),
        "workspace_root": str(workspace), "changed_paths": recipe["artifact_paths"] if node["write_scope"] else [],
        "artifacts": artifacts, "criteria_results": criteria, "checks": checks, "external_refs": refs,
        "findings": [] if verdict == "accepted" else [{"severity": "medium", "code": "EXECUTION-OR-ORACLE-FAILED",
             "summary": "Execution or declared oracle failed", "evidence_ref": "runner:" + attempt}],
        "telemetry": {"model": external.get("model") if external else None, "input_tokens": None,
             "output_tokens": None, "total_tokens": None, "tool_calls": None, "cost_usd": None,
             "quota_before": canonical_quota_value(external.get("quota_before") if external else None),
             "quota_after": canonical_quota_value(external.get("quota_after") if external else None), "duration_seconds": time.monotonic() - began}}
    write(result_path, {"attempt_id": attempt, "evidence": evidence})


if __name__ == "__main__":
    run_recipe(sys.argv[1])
