"""Verify actual strong-planner participation, not a self-declared role label.

Local runtime transcripts/receipts are operational evidence, not signatures and
not protection against a malicious process running as the same Windows user.
"""
import hashlib
import json
from pathlib import Path
import re


MODEL_PATTERNS = (
    r"(?:openai/)?gpt-6-astra(?:-.+)?",
    r"(?:anthropic/)?claude-fable-5(?:[.-].+)?",
    r"qwen3\.8-max(?:-\d{4,8})?",
    # Owner decision 2026-10-24: Kimi K3.1 Pro (runtime id k3.1-pro) replaces
    # the deprecated K3 as the qualified moonshot planner family. Plain K3
    # ("k3", "kimi-k3", "kimi-k3-max") is no longer qualified.
    r"(?:(?:moonshot|kimi-code)/)?(?:kimi-)?k3\.1-pro(?:-.+)?",
)


def qualified(model):
    return isinstance(model, str) and any(re.fullmatch(pattern, model, re.I) for pattern in MODEL_PATTERNS)


def verify(path, prompt_hash, plan_hash):
    path = Path(path).resolve()
    witness = json.loads(path.read_text(encoding="utf-8-sig"))
    return verify_descriptor(witness, prompt_hash, plan_hash, path.parent)


def verify_descriptor(witness, prompt_hash, plan_hash, base_dir=None):
    """Re-read the same bound source at approval and every later validation."""
    if not isinstance(witness, dict) or set(witness) != {"kind", "source", "source_sha256"}:
        raise ValueError("Planner witness must contain kind, source and source_sha256")
    source = Path(witness["source"])
    if not source.is_absolute():
        if base_dir is None:
            raise ValueError("Stored planner witness source must be absolute")
        source = Path(base_dir) / source
    source = source.resolve()
    payload = source.read_bytes()
    if hashlib.sha256(payload).hexdigest() != witness["source_sha256"]:
        raise ValueError("Planner witness source changed")
    marker = "PLAN_REVIEWED " + prompt_hash + " " + plan_hash
    model, session = None, None
    if witness["kind"] == "fleet":
        record = json.loads(payload)
        if record.get("run_status") != "COMPLETED":
            raise ValueError("Planner run is incomplete")
        if (record.get("runtime_attestation") or {}).get("status") != "VERIFIED":
            raise ValueError("Planner runtime attestation is missing")
        model = record.get("model")
        if record.get("adapter") == "codex":
            if record.get("execution_profile_id") != "codex-" + str(model):
                raise ValueError("Codex planner execution profile is not bound to its model")
            observed = []
            for attempt in record.get("provider_attempts") or []:
                command = attempt.get("command") or []
                for index, item in enumerate(command[:-1]):
                    if item == "--model":
                        observed.append(command[index + 1])
            if not observed or observed != [model] or not qualified(model):
                raise ValueError("Codex provider model is not bound to a qualified architect family")
        elif record.get("adapter") == "opencode":
            identity = fleet_model_identity(record)
            if not qualified(identity['model']):
                raise ValueError("Observed planner model is not in the qualified architect family")
        else:
            identity = record.get("model_identity") or {}
            if identity.get("status") != "verified":
                raise ValueError("Planner model identity is unverified")
            observed = identity.get("observed_models", [])
            if not observed or not all(qualified(item) and model_matches(model, item) for item in observed):
                raise ValueError("Observed planner model is not in the qualified architect family")
        summary = (record.get("handoff") or {}).get("summary", "")
        if marker not in summary.splitlines():
            raise ValueError("Planner response does not approve this exact intake and plan")
        session = record.get("run_id")
    elif witness["kind"] == "codex-native":
        current_model, current_session = None, None
        for line in payload.decode("utf-8-sig").splitlines():
            row = json.loads(line)
            body = row.get("payload") or {}
            if row.get("type") == "session_meta":
                current_session = body.get("id")
            if row.get("type") == "turn_context":
                current_model = body.get("model")
            if row.get("type") == "response_item" and body.get("type") == "message" and body.get("role") == "assistant":
                text = "\n".join(item.get("text", "") for item in body.get("content", []) if item.get("type") in {"output_text", "text"})
                if marker in text.splitlines() and qualified(current_model):
                    model, session = current_model, current_session
        if not model:
            raise ValueError("No bound review from a qualified native planner in the transcript")
    elif witness["kind"] == "claude-native":
        for line in payload.decode("utf-8-sig").splitlines():
            row = json.loads(line)
            body = row.get("message") or {}
            if row.get("type") != "assistant" or body.get("role") != "assistant":
                continue
            content = "\n".join(part.get("text", "") for part in body.get("content", []) if part.get("type") == "text")
            if marker in content.splitlines() and qualified(body.get("model")):
                model, session = body["model"], row.get("sessionId")
        if not model:
            raise ValueError("No bound qualified Claude parent review in native transcript")
    else:
        raise ValueError("Unsupported planner witness kind")
    if not qualified(model) or not isinstance(session, str) or not session.strip():
        raise ValueError("Planner model/session identity is not qualified")
    return {"status": "VERIFIED", "kind": witness["kind"], "source": str(source), "model": model, "session_id": session,
            "prompt_sha256": prompt_hash, "plan_sha256": plan_hash,
            "source_sha256": witness["source_sha256"]}


def model_matches(expected, observed):
    if not isinstance(expected, str) or not isinstance(observed, str):
        return False
    return observed == expected or (
        expected == 'qwen3.8-max' and re.fullmatch(r'qwen3\.8-max-\d{4,8}', observed) is not None)


def model_lineage(model):
    """Conservative provider families; an unknown identity fails closed."""
    if not isinstance(model, str):
        raise ValueError('Missing model identity')
    if 'k3.1-pro' in model:
        # Kimi K3.1 Pro runtime ids: "k3.1-pro", "kimi-k3.1-pro",
        # "kimi-code/k3.1-pro", "moonshot/k3.1-pro" (owner decision 2026-10-24).
        return 'moonshot'
    for prefix, lineage in (('gpt-', 'openai'), ('claude-', 'anthropic'),
                            ('qwen', 'alibaba'), ('glm-', 'zai'),
                            ('kimi-', 'moonshot')):
        if model.startswith(prefix):
            return lineage
    raise ValueError('Unsupported reviewer model lineage')


def fleet_model_identity(record):
    """Read Fleet schema 5 identity; OpenCode reports runtime model IDs."""
    model = record.get('model')
    lineage = model_lineage(model)
    identity = record.get('model_identity')
    if not isinstance(identity, dict):
        raise ValueError('Fleet model identity is missing')
    adapter = record.get('adapter')
    if adapter == 'opencode':
        namespace = {'zai': 'zai-coding-plan', 'moonshot': 'moonshot',
                     'openai': 'openai', 'anthropic': 'anthropic', 'alibaba': 'alibaba'}[lineage]
        expected = namespace + '/' + model
        if identity.get('status') != 'verified' or identity.get('expected_runtime_model') != expected:
            raise ValueError('OpenCode runtime model identity is unverified or mismatched')
        observed = identity.get('observed_runtime_models')
        if not isinstance(observed, list) or not observed or any(item != expected for item in observed):
            raise ValueError('OpenCode observed runtime model differs from expected identity')
        profile = record.get('execution_profile_id')
        prefix = 'opencode-' + model.replace('.', '-')
        if not isinstance(profile, str) or not (profile == prefix or profile.startswith(prefix + '-')):
            raise ValueError('OpenCode execution profile differs from model identity')
    elif adapter == 'codex':
        if lineage != 'openai' or identity.get('status') not in {'verified', 'not-applicable'}:
            raise ValueError('Codex model identity is unverified')
        if identity.get('expected_model') != model or record.get('execution_profile_id') != 'codex-' + model:
            raise ValueError('Codex execution profile differs from model identity')
        observed = [command[index + 1]
                    for attempt in record.get('provider_attempts', [])
                    for command in [attempt.get('command', [])]
                    for index, item in enumerate(command[:-1]) if item == '--model']
        if not observed or any(item != model for item in observed):
            raise ValueError('Codex provider command does not bind the declared model')
        if identity['status'] == 'verified' and identity.get('observed_models') != [model]:
            raise ValueError('Codex observed model identity is mismatched')
    else:
        expected_adapter = {'alibaba': 'qwen', 'anthropic': 'claude', 'moonshot': 'kimi'}.get(lineage)
        if adapter != expected_adapter or expected_adapter is None:
            raise ValueError('Fleet adapter does not match observed model lineage')
        if identity.get('status') != 'verified' or identity.get('expected_model') != model:
            raise ValueError('Fleet model identity is unverified or mismatched')
        observed = identity.get('observed_models')
        if not isinstance(observed, list) or not observed or not all(model_matches(model, item) for item in observed):
            raise ValueError('Fleet observed model differs from expected identity')
    return {'model': model, 'lineage': lineage}


def has_explicit_pass(summary, dimension):
    """A labeled PASS may carry an explanation; contradictory verdicts fail."""
    if not isinstance(summary, str):
        return False
    prefix = re.escape(dimension)
    verdicts = re.findall(
        rf'(?m)^[ \t]*{prefix}:?[ \t]+(PASS|REPAIR|BLOCKED|FAIL)'
        r'(?:[ \t]*(?::|[-\u2013\u2014])[ \t]*[^\r\n]*)?[ \t]*$', summary)
    return bool(verdicts) and all(verdict == 'PASS' for verdict in verdicts)


def verify_reviewer(source, source_hash, prompt_hash, plan_hash, auditor):
    """Verify a durable terminal Fleet handoff, never a caller's PASS label."""
    payload = Path(source).read_bytes()
    if hashlib.sha256(payload).hexdigest() != source_hash:
        raise ValueError('Reviewer source hash mismatch')
    record = json.loads(payload)
    if not isinstance(record, dict) or record.get('run_status') != 'COMPLETED':
        raise ValueError('Reviewer run is incomplete')
    if (record.get('runtime_attestation') or {}).get('status') != 'VERIFIED':
        raise ValueError('Reviewer runtime is unverified')
    if record.get('exit_code') != 0 or record.get('timed_out') is not False:
        raise ValueError('Reviewer process did not complete successfully')
    identity = fleet_model_identity(record)
    if (record.get('agent') != auditor.get('identity') or record.get('run_id') != auditor.get('session_id')
            or identity['model'] != auditor.get('model') or identity['lineage'] != auditor.get('lineage')):
        raise ValueError('Reviewer identity/model/session/lineage differs from auditor')
    handoff = record.get('handoff')
    if (record.get('handoff_valid') is not True or record.get('handoff_status') != 'completed'
            or not isinstance(handoff, dict) or handoff.get('status') != 'completed'):
        raise ValueError('Reviewer terminal handoff is incomplete')
    for key in ('files_changed', 'verification', 'risks', 'next_steps'):
        if not isinstance(handoff.get(key), list) or not all(isinstance(item, str) for item in handoff[key]):
            raise ValueError('Reviewer terminal handoff is malformed')
    if not handoff['verification'] or not isinstance(handoff.get('summary'), str):
        raise ValueError('Reviewer terminal handoff lacks verification')
    marker = f'FULL_COMBINED_REVIEW_PASS {prompt_hash} {plan_hash}'
    if (marker not in handoff['summary'].splitlines()
            or not all(has_explicit_pass(handoff['summary'], dimension)
                       for dimension in ('FIDELITY', 'SUFFICIENCY'))):
        raise ValueError('Reviewer terminal handoff lacks separate PASS verdicts or exact marker')
