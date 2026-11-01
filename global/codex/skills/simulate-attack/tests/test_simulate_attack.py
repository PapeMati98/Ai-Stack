"""Deterministic negative tests for simulate-attack.

The tests deliberately use temp directories for targets and the
``SECURITY_AUDIT_ROOT`` environment variable to redirect the report
container. They do not touch the user's real
``~/.agents/security-audits`` tree or the real ``security-audit-cycle``
ledger. The synthetic dry-run fixture under
``tests/fixtures/synthetic-target`` is created and read but never executed
against a real target.

Fleet access is exercised against a synthetic runtime tree
(``_make_fake_fleet_runtime``) whose ``fleet.py`` is a fake subprocess
emitting the real ``fleet.py run`` JSON result shape. The production
default runner is never replaced in these tests except through the
explicit ``set_runner`` injection seam.
"""

from __future__ import annotations
def _redacted_fixture(kind, ordinal, binary=False):
    """Return a public fixture derived only from category and ordinal."""
    seed = (len(kind) * 131 + ordinal * 7919 + 17) & 0x7fffffff
    chars = []
    for _ in range(40):
        seed = (seed * 1103515245 + 12345) & 0x7fffffff
        n = seed % 36
        chars.append(chr(97 + n) if n < 26 else chr(48 + n - 26))
    body = ''.join(chars)
    if kind == 'vendor-token':
        body = chr(103) + chr(104) + chr(112) + chr(95) + body
    elif kind == 'aws-access-key-id':
        body = chr(65) + chr(75) + chr(73) + chr(65) + body[:16].upper()
    elif kind == 'private-key':
        marker = (chr(80) + chr(82) + chr(73) + chr(86) + chr(65)
                  + chr(84) + chr(69) + chr(32) + chr(75) + chr(69) + chr(89))
        dash = chr(45) * 5
        body = (dash + chr(66) + chr(69) + chr(71) + chr(73) + chr(78)
                + chr(32) + marker + dash + chr(10) + body + chr(10)
                + dash + chr(69) + chr(78) + chr(68) + chr(32) + marker + dash)
    return body.encode('ascii') if binary else body


import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
SCRIPT = SKILL_ROOT / "scripts" / "simulate_attack.py"
FLEET_RUNNER = SKILL_ROOT / "scripts" / "fleet_runner.py"
FIXTURE = HERE / "fixtures" / "synthetic-target"

ENV_KEYS = (
    "SECURITY_AUDIT_ROOT",
    "SECURITY_AUDIT_TEST_MODE",
    "AGENT_FLEET_CURRENT_ROOT",
    "AGENT_FLEET_STATE_DIR",
    "FAKE_FLEET_AGENT",
    "FAKE_FLEET_TOKEN_STATUS",
    "FAKE_FLEET_BAD_RESULT",
    "FAKE_FLEET_FINDING",
    "FAKE_FLEET_WORKSPACE_CAPTURE",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("simulate_attack", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_runner():
    spec = importlib.util.spec_from_file_location("sim_atk_fleet_runner", FLEET_RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Placeholder secret material. The strings below are deliberately chosen
# so they do not match any real vendor token regex and are obviously
# synthetic. Do not replace them with a real-shaped example.
PLACEHOLDER_AWS = "AKIAREPLACEWITHACTUALKEYID01"  # invalid: wrong suffix length
PLACEHOLDER_OPENAI = "sk-proj-TOOSHORT"  # invalid: far below the real length
PLACEHOLDER_GITHUB = "ghp_" + "SYNTHETIC_TEST_ONLY"  # invalid: below 30 chars
PLACEHOLDER_EMAIL = "redacted.user@invalid"


def _aws_shaped() -> str:
    """Assembled at runtime so no real-shaped credential is stored."""
    return "AKIA" + "A" * 16


def _openai_shaped() -> str:
    """Assembled at runtime so no real-shaped credential is stored."""
    return "sk-proj-" + "Aa1" * 10


FAKE_FLEET_PY = r'''#!/usr/bin/env python3
"""Synthetic Fleet runtime used only by tests; emits the real result shape."""
import json
import os
import re
import sys

VALUE_FLAGS = {
    "--profiles", "--state-dir", "--task", "--workspace", "--timeout",
    "--agent", "--mode", "--kind", "--complexity", "--max-prompt-bytes",
    "--assessment-file",
}


def main(argv):
    values = {}
    action = None
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in VALUE_FLAGS and index + 1 < len(argv):
            values[item] = argv[index + 1]
            index += 2
            continue
        if item == "run":
            action = "run"
        index += 1
    if (
        action != "run"
        or "--task" not in values
        or "--profiles" not in values
        or "--assessment-file" not in values
    ):
        sys.stderr.write(json.dumps(
            {"action": action, "status": "REFUSED", "error": "bad invocation"}
        ))
        return 2
    with open(values["--assessment-file"], "r", encoding="utf-8") as handle:
        assessment = json.load(handle)
    if assessment.get("red_team_intent") != {"mode": "red-team", "explicit": True}:
        sys.stderr.write(json.dumps(
            {"action": action, "status": "REFUSED", "error": "missing red-team intent"}
        ))
        return 2
    task = values["--task"]
    capture = os.environ.get("FAKE_FLEET_WORKSPACE_CAPTURE")
    if capture:
        with open(capture, "w", encoding="utf-8") as handle:
            handle.write(values.get("--workspace", ""))
    with open(values["--profiles"], "r", encoding="utf-8") as handle:
        profiles = json.load(handle)
    profile = profiles["profiles"]["qwen-local"]
    execution = next(iter(profile["execution_profiles"].values()))
    model = execution["runtime_model_id"]
    fixture = re.search(r"^FIXTURE_ID=(\S+)", task, re.MULTILINE)
    vector = re.search(r"^VECTOR=(\S+)", task, re.MULTILINE)
    if fixture:
        probe = {
            "name": "synthetic_harness_probe",
            "params": {
                "fixture_id": fixture.group(1),
                "expected_outcome": "dry_run_pass",
            },
        }
    else:
        probe = {
            "name": "static_vector_review",
            "params": {"vector": vector.group(1) if vector else "auth-roles"},
        }
    finding = os.environ.get("FAKE_FLEET_FINDING") == "1"
    findings = []
    if finding:
        findings = [{
            "finding_id": "RT-001",
            "severity": "high",
            "location": "app.py:2",
            "summary": "credential-like value " + "sk-proj-" + "Aa1" * 10,
            "recommendation": "replace dynamic evaluation with an allowlist",
            "evidence_ref": "sha256:" + "b" * 64,
        }]
    redteam_result = {
        "probe": probe,
        "finding_status": "findings" if finding else "no_findings",
        "findings": findings,
        "evidence": [] if os.environ.get("FAKE_FLEET_BAD_RESULT") == "1" else ["sha256:" + "a" * 64],
    }
    handoff = {
        "status": "completed",
        "summary": "bounded synthetic red-team reasoning\nREDTEAM_RESULT " + json.dumps(redteam_result),
        "files_changed": [],
        "verification": ["synthetic runtime emits the real result shape"],
        "risks": [],
        "next_steps": [],
    }
    result = {
        "action": "run",
        "status": "COMPLETED",
        "run_id": "run-fake-0001",
        "agent": os.environ.get("FAKE_FLEET_AGENT", "qwen-local"),
        "adapter": "qwen",
        "model": model,
        "model_identity": {
            "status": "verified",
            "expected_model": model,
            "observed_models": [model],
            "error": None,
        },
        "runtime_attestation": {
            "status": "VERIFIED",
            "fleet_version": "0.8.32",
            "package_version": "0.8.32",
        },
        "exit_code": 0,
        "handoff_valid": True,
        "handoff": handoff,
        "handoff_parse_error": None,
        "token_usage": {
            "status": os.environ.get("FAKE_FLEET_TOKEN_STATUS", "known"),
            "fresh_input_tokens": 11,
            "cache_read_tokens": 3,
            "total_tokens": 14,
        },
    }
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


def _make_project(root: Path) -> Path:
    project = root / "sample-project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "app.py").write_text(
        "DEBUG = True\n"
        "def run(value):\n"
        "    return eval(value)\n",
        encoding="utf-8",
    )
    (project / "config.py").write_text(
        ("API_KEY = '" + _redacted_fixture('assigned-secret', 2) + "'\nSECRET_TOKEN = 'placeholder'\n"),
        encoding="utf-8",
    )
    return project


def _make_audit_root(root: Path) -> Path:
    audit = root / "audits"
    audit.mkdir(parents=True, exist_ok=True)
    return audit


def _seed_audit(workspace: Path, project: Path, *, age_days: float = 20) -> None:
    module = _load_module()
    _, project_dir = module.project_identity(project)
    project_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "audit_id": "20260101T000000000000Z__seed",
        "completed_at": (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days)
        ).isoformat().replace("+00:00", "Z"),
        "next_due_at": (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days - 14)
        ).isoformat().replace("+00:00", "Z"),
        "verdict": "PASS",
        "agent": "seed",
        "report": str(project_dir / "audits" / "seed" / "REPORT.md"),
    }
    (project_dir / "latest.json").write_text(json.dumps(payload), encoding="utf-8")


def _make_fake_fleet_runtime(
    root: Path,
    *,
    package_version: str = "0.8.32",
    runtime_model: str = "huihui_ai/Qwen3.8-abliterated",
    include_marker: bool = True,
    include_fleet_py: bool = True,
) -> Path:
    """Build a synthetic Fleet runtime tree for tests.

    The tree carries the real runtime marker name, the real payload layout
    and a fake ``fleet.py`` subprocess that emits the real ``fleet.py run``
    JSON result shape.
    """

    root.mkdir(parents=True, exist_ok=True)
    if include_marker:
        (root / ".agent-fleet-suite-runtime.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "managed_by": "agent-fleet-suite-v2-installer",
                    "package_id": "agent-fleet-suite",
                    "package_version": package_version,
                    "package_fingerprint": "0" * 64,
                    "payload_fingerprint": "0" * 64,
                    "payload_file_count": 1,
                    "transaction_id": "tx-synthetic-test",
                    "installed_at": "2026-09-02T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
    if include_fleet_py:
        scripts = root / "agent-fleet" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "fleet.py").write_text(FAKE_FLEET_PY, encoding="utf-8")
        references = root / "agent-fleet" / "references"
        references.mkdir(parents=True, exist_ok=True)
        (references / "profiles.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profiles": {
                        "qwen-local": {
                            "adapter": "qwen",
                            "execution_profiles": {
                                "qwen-local-redteam": {
                                    "model": runtime_model,
                                    "runtime_model_id": runtime_model,
                                }
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
    return root


class SimulateAttackBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.audit_root = _make_audit_root(self.workspace)
        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["SECURITY_AUDIT_TEST_MODE"] = "1"
        os.environ["SECURITY_AUDIT_ROOT"] = str(self.audit_root)
        self.project = _make_project(self.audit_root / "synthetic-targets")
        (self.audit_root / "target-registry.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "targets": [
                        {
                            "path": str(self.project),
                            "target_class": "isolated_candidate",
                            "synthetic_data": True,
                            "active_attack_allowed": True,
                        },
                        {
                            "path": str(self.project),
                            "target_class": "STAGING",
                            "synthetic_data": True,
                            "active_attack_allowed": True,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        os.environ.pop("AGENT_FLEET_CURRENT_ROOT", None)
        os.environ.pop("AGENT_FLEET_STATE_DIR", None)
        self.env = os.environ.copy()

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()
        _load_runner().reset_runner()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
            encoding="utf-8",
        )

    def enable_fake_fleet(self, **kwargs) -> Path:
        runtime = _make_fake_fleet_runtime(self.workspace / "fleet-current", **kwargs)
        for key in ("AGENT_FLEET_CURRENT_ROOT", "AGENT_FLEET_STATE_DIR"):
            os.environ.pop(key, None)
        os.environ["AGENT_FLEET_CURRENT_ROOT"] = str(runtime)
        os.environ["AGENT_FLEET_STATE_DIR"] = str(self.workspace / "fleet-state")
        self.env = os.environ.copy()
        return runtime


class SimulateAttackHardDenies(SimulateAttackBase):
    def test_missing_budget_is_blocked(self) -> None:
        result = self.run_cli(
            "preflight",
            "--project", str(self.project),
            "--target-class", "TEST",
            "--target-name", "synthetic",
            "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("max_iterations", payload["reason"])

    def test_zero_budget_is_blocked(self) -> None:
        result = self.run_cli(
            "preflight",
            "--project", str(self.project),
            "--target-class", "TEST",
            "--target-name", "synthetic",
            "--max-iterations", "0",
            "--max-fresh-tokens", "1000",
            "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("max_iterations", payload["reason"])

    def test_missing_fresh_token_budget_is_blocked(self) -> None:
        result = self.run_cli(
            "preflight",
            "--project", str(self.project),
            "--target-class", "TEST",
            "--target-name", "synthetic",
            "--max-iterations", "1",
            "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("max_fresh_tokens", payload["reason"])

    def test_production_target_class_is_blocked(self) -> None:
        result = self.run_cli(
            "preflight",
            "--project", str(self.project),
            "--target-class", "PROD",
            "--target-name", "synthetic",
            "--max-iterations", "1",
            "--max-fresh-tokens", "1000",
            "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("target class", payload["reason"])

    def test_target_name_with_prod_token_is_blocked(self) -> None:
        result = self.run_cli(
            "preflight",
            "--project", str(self.project),
            "--target-class", "isolated_candidate",
            "--target-name", "synthetic-prod-mirror",
            "--max-iterations", "1",
            "--max-fresh-tokens", "1000",
            "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("PROD_LOCKED", payload["reason"])

    def test_live_customer_data_flag_is_blocked(self) -> None:
        result = self.run_cli(
            "preflight",
            "--project", str(self.project),
            "--target-class", "TEST",
            "--target-name", "synthetic",
            "--max-iterations", "1",
            "--max-fresh-tokens", "1000",
            "--trigger", "next_substantive_work",
            "--live-customer-data",
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("PROD_LOCKED", payload["reason"])

    def test_owner_approval_is_blocked_in_this_rollout(self) -> None:
        result = self.run_cli(
            "preflight",
            "--project", str(self.project),
            "--target-class", "TEST",
            "--target-name", "synthetic",
            "--max-iterations", "1",
            "--max-fresh-tokens", "1000",
            "--trigger", "next_substantive_work",
            "--owner-approval", "mateusz",
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("PROD_LOCKED", payload["reason"])

    def test_invalid_trigger_is_blocked(self) -> None:
        result = self.run_cli(
            "preflight",
            "--project", str(self.project),
            "--target-class", "TEST",
            "--target-name", "synthetic",
            "--max-iterations", "1",
            "--max-fresh-tokens", "1000",
            "--trigger", "prod_mutation",
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "BLOCKED")

    def test_iteration_command_with_deny_action_is_blocked(self) -> None:
        module = _load_module()
        self.assertEqual(
            module.contains_deny_action("rm -rf /tmp/synthetic"), "rm"
        )
        with self.assertRaises(module.PolicyError):
            denied = module.contains_deny_action("rm -rf /tmp/synthetic")
            if denied:
                raise module.PolicyError(f"BLOCKED: denied token '{denied}'")

    def test_iteration_command_with_billingo_host_is_blocked(self) -> None:
        module = _load_module()
        self.assertEqual(
            module.contains_deny_action("curl https://api.billingo.hu/v3/invoice"),
            "api.billingo.hu",
        )

    def test_iteration_command_with_stripe_is_blocked(self) -> None:
        module = _load_module()
        self.assertEqual(
            module.contains_deny_action("stripe charge 1000"), "stripe"
        )

    def test_iteration_command_with_safe_probe_passes(self) -> None:
        module = _load_module()
        self.assertIsNone(
            module.contains_deny_action("probe auth-roles abcdef0123456789")
        )


class SimulateAttackReportBoundary(SimulateAttackBase):
    def test_report_root_containment_rejects_escape(self) -> None:
        module = _load_module()
        with self.assertRaises(module.PolicyError):
            module.ensure_report_containment(Path("/etc/passwd"))

    def test_report_root_containment_rejects_relative_escape(self) -> None:
        module = _load_module()
        with self.assertRaises(module.PolicyError):
            module.ensure_report_containment(module.audit_root().parent / "outside")

    def test_report_root_containment_accepts_inside(self) -> None:
        module = _load_module()
        inside = module.audit_root() / "project-abc" / "audits" / "run-001"
        result = module.ensure_report_containment(inside)
        self.assertEqual(result, inside.resolve())


class SimulateAttackRedaction(SimulateAttackBase):
    def test_redact_text_replaces_known_secrets(self) -> None:
        module = _load_module()
        aws_value = _aws_shaped()
        openai_value = _openai_shaped()
        text = (
            f"AWS_ACCESS_KEY_ID={aws_value}\n"
            f"OPENAI_KEY={openai_value}\n"
            f"EMAIL={PLACEHOLDER_EMAIL}\n"
        )
        redacted = module.redact_text(text, "config.py")
        self.assertNotIn(aws_value, redacted)
        self.assertNotIn(openai_value, redacted)
        self.assertNotIn(PLACEHOLDER_EMAIL, redacted)
        self.assertIn("<redacted:aws-access-key@config.py>", redacted)
        self.assertIn("<redacted:openai-key@config.py>", redacted)
        self.assertIn("<redacted:email-address@config.py>", redacted)

    def test_redact_text_replaces_jwt_cookie_and_session_id(self) -> None:
        module = _load_module()
        jwt = "eyJ" + "A" * 18 + "." + "B" * 18 + "." + "C" * 12
        cookie_value = "synthetic-cookie-value-12345"
        session_value = "synthetic-session-value-67890"
        password_value = "Synthetic!Passphrase#24680"
        text = (
            f"Authorization: Bearer {jwt}\n"
            f"Set-Cookie: auth={cookie_value}; HttpOnly; Secure\n"
            f"session_id={session_value}\n"
            f"password={password_value}\n"
        )
        redacted = module.redact_text(text, "config.json")
        self.assertNotIn(jwt, redacted)
        self.assertNotIn(cookie_value, redacted)
        self.assertNotIn(session_value, redacted)
        self.assertNotIn(password_value, redacted)
        self.assertIn("<redacted:jwt@config.json>", redacted)
        self.assertIn("<redacted:cookie@config.json>", redacted)
        self.assertIn("<redacted:session-id@config.json>", redacted)
        self.assertIn("<redacted:password@config.json>", redacted)

    def test_sanitized_workspace_and_residual_scan_remove_jwt_cookie(self) -> None:
        module = _load_module()
        source = self.workspace / "jwt-source"
        staged = self.workspace / "jwt-staged"
        source.mkdir()
        jwt = "eyJ" + "D" * 18 + "." + "E" * 18 + "." + "F" * 12
        cookie_value = "synthetic-cookie-value-24680"
        (source / "config.json").write_text(
            f'{{"authorization":"Bearer {jwt}","header":"Cookie: sid={cookie_value}"}}',
            encoding="utf-8",
        )
        module.build_sanitized_workspace(source, staged)
        module.verify_redacted_artifacts(staged)
        combined = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in staged.rglob("*")
            if path.is_file()
        )
        self.assertNotIn(jwt, combined)
        self.assertNotIn(cookie_value, combined)
        self.assertIn("<redacted:jwt@config.json>", combined)
        self.assertIn("<redacted:cookie@config.json>", combined)

    def test_redact_text_is_deterministic(self) -> None:
        module = _load_module()
        text = f"{_aws_shaped()} again"
        first = module.redact_text(text, "config.py")
        second = module.redact_text(text, "config.py")
        self.assertEqual(first, second)

    def test_redact_text_does_not_nest_existing_markers(self) -> None:
        module = _load_module()
        marker = "<redacted:aws-access-key@config.py>"
        text = f"{marker}\nEMAIL={PLACEHOLDER_EMAIL}\n{marker}"
        redacted = module.redact_text(text, "config.py")
        # Existing marker must be preserved without nesting.
        self.assertNotIn("&lt;redacted:", redacted)
        # The marker text must still appear exactly as written once.
        self.assertEqual(redacted.count(marker), 2)

    def test_phone_redaction_requires_plus_prefix_or_explicit_label(self) -> None:
        module = _load_module()
        text = "support +48 123 456 789; phone=501234567"
        redacted = module.redact_text(text, "contact.txt")
        self.assertNotIn("+48 123 456 789", redacted)
        self.assertNotIn("501234567", redacted)
        self.assertEqual(redacted.count("<redacted:phone-number@contact.txt>"), 2)

    def test_phone_redaction_ignores_audit_ids_dates_and_hash_fragments(self) -> None:
        module = _load_module()
        phone_rule = next(
            pattern for kind, pattern in module.REDACT_RULES
            if kind == "phone-number"
        )
        report_metadata = (
            "20260903T001251384051Z__redteam__redteam "
            "2026-09-03T00:13:59.226910Z "
            "synthetic-vault-2026-09-02 "
            "75ba494b5a250c6eb0606ddb658c1339fea5a416f06993d87f367e83aad43e1a"
        )
        self.assertIsNone(phone_rule.search(report_metadata))


class SimulateAttackOneLedger(SimulateAttackBase):
    def test_escalation_uses_existing_audit_root(self) -> None:
        module = _load_module()
        original = (
            Path(__file__).resolve().parents[2]
            / "security-audit-cycle"
            / "scripts"
            / "security_audit.py"
        )
        spec = importlib.util.spec_from_file_location("security_audit_original", original)
        original_module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = original_module
        spec.loader.exec_module(original_module)
        self.assertEqual(
            module.audit_root().__class__,
            original_module.audit_root().__class__,
        )
        project = module.normalize_project(str(self.project))
        old_id, _ = original_module.project_identity(project)
        new_id, _ = module.project_identity(project)
        self.assertEqual(old_id, new_id)
        self.assertEqual(module.MAX_FILES, 4096)

    def test_stale_audit_triggers_escalation(self) -> None:
        module = _load_module()
        _seed_audit(self.workspace, self.project)
        escalation = module.freshness_escalation_required(
            self.project, "next_substantive_work"
        )
        self.assertTrue(escalation["escalation_required"])
        self.assertEqual(
            escalation["trigger_source"], "security-audit-cycle"
        )

    def test_pre_production_release_candidate_always_escalates(self) -> None:
        module = _load_module()
        # No seeded audit; the sole 14-day ledger must still escalate for
        # ``pre_production_release_candidate``.
        escalation = module.freshness_escalation_required(
            self.project, "pre_production_release_candidate"
        )
        self.assertTrue(escalation["escalation_required"])
        self.assertEqual(
            escalation["trigger_source"], "security-audit-cycle"
        )

    def test_no_local_freshness_probe_is_shipped(self) -> None:
        # The duplicate freshness probe must not exist; the skill reuses
        # the sibling ledger through a thin import only.
        self.assertFalse(
            (SKILL_ROOT / "scripts" / "_audit_freshness_probe.py").exists()
        )
        helper_text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("_audit_freshness_probe", helper_text)


class SimulateAttackBothTriggers(SimulateAttackBase):
    def test_next_substantive_work_trigger_runs(self) -> None:
        _seed_audit(self.workspace, self.project)
        self.enable_fake_fleet()
        result = self.run_cli(
            "run",
            "--project", str(self.project),
            "--target-class", "TEST",
            "--target-name", "synthetic",
            "--max-iterations", "2",
            "--max-fresh-tokens", "5000",
            "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn(payload["verdict"], {"PASS", "REVISE"})
        self.assertEqual(payload["fleet_release"], "0.8.32")
        self.assertEqual(
            payload["observed_model"], "huihui_ai/Qwen3.8-abliterated"
        )
        audit_dir = Path(payload["audit_dir"])
        for entry in ("metadata.json", "recon.json", "iterations.jsonl", "remediation.md", "REPORT.md"):
            self.assertTrue((audit_dir / entry).exists(), entry)
        iterations = [
            json.loads(line)
            for line in (audit_dir / "iterations.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(len(iterations), 2)
        self.assertTrue(all(item["redacted"] for item in iterations))
        self.assertTrue(
            all(
                item["observed_model"] == "huihui_ai/Qwen3.8-abliterated"
                for item in iterations
            )
        )
        # Fresh-token counters come from the real Fleet result telemetry.
        self.assertTrue(all(item["fresh_tokens"] == 11 for item in iterations))
        self.assertEqual(iterations[-1]["cumulative_fresh_tokens"], 22)

    def test_pre_production_release_candidate_trigger_runs(self) -> None:
        self.enable_fake_fleet()
        result = self.run_cli(
            "run",
            "--project", str(self.project),
            "--target-class", "isolated_candidate",
            "--target-name", "release-candidate",
            "--max-iterations", "1",
            "--max-fresh-tokens", "1500",
            "--trigger", "pre_production_release_candidate",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn(payload["verdict"], {"PASS", "REVISE"})

    def test_fresh_audit_blocks_next_substantive_trigger(self) -> None:
        _seed_audit(self.workspace, self.project, age_days=1)
        result = self.run_cli(
            "preflight",
            "--project", str(self.project),
            "--target-class", "TEST",
            "--target-name", "synthetic",
            "--max-iterations", "1",
            "--max-fresh-tokens", "1000",
            "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("freshness gate", payload["reason"])


class SimulateAttackExternalEffects(SimulateAttackBase):
    def test_run_records_zero_external_effects(self) -> None:
        _seed_audit(self.workspace, self.project)
        self.enable_fake_fleet()
        result = self.run_cli(
            "run",
            "--project", str(self.project),
            "--target-class", "TEST",
            "--target-name", "synthetic",
            "--max-iterations", "1",
            "--max-fresh-tokens", "1000",
            "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        audit_dir = Path(payload["audit_dir"])
        metadata = json.loads((audit_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["external_effect_count"], 0)
        self.assertEqual(metadata["redaction_status"], "verified-redacted")
        recon = json.loads((audit_dir / "recon.json").read_text(encoding="utf-8"))
        for obs in recon["observations"]:
            # Only bounded metadata is persisted; never source snippets.
            self.assertNotIn("redacted_observation", obs)
            self.assertIn("sha256", obs)
        config_obs = next(
            obs for obs in recon["observations"] if obs["path"] == "config.py"
        )
        self.assertGreater(config_obs["redaction_count"], 0)

    def test_report_under_audit_root_uses_synthetic_root(self) -> None:
        _seed_audit(self.workspace, self.project)
        self.enable_fake_fleet()
        result = self.run_cli(
            "run",
            "--project", str(self.project),
            "--target-class", "TEST",
            "--target-name", "synthetic",
            "--max-iterations", "1",
            "--max-fresh-tokens", "1000",
            "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        audit_dir = Path(payload["audit_dir"])
        self.assertTrue(str(audit_dir).startswith(str(self.audit_root)))


class FleetRunnerContract(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        _load_runner().reset_runner()

    def _runtime(self, **kwargs) -> Path:
        return _make_fake_fleet_runtime(self.workspace / "fleet-current", **kwargs)

    def test_resolve_fleet_rejects_missing_runtime(self) -> None:
        runner = _load_runner()
        empty = self.workspace / "not-installed"
        empty.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(runner, "FLEET_CURRENT_ROOT", empty):
            with self.assertRaises(runner.FleetError) as ctx:
                runner.resolve_fleet()
        self.assertIn("0.8.32", str(ctx.exception))

    def test_resolve_fleet_rejects_old_version(self) -> None:
        runner = _load_runner()
        root = self._runtime(package_version="0.8.31")
        with mock.patch.object(runner, "FLEET_CURRENT_ROOT", root):
            with self.assertRaises(runner.FleetError) as ctx:
                runner.resolve_fleet()
        self.assertIn("0.8.31", str(ctx.exception))

    def test_resolve_fleet_rejects_wrong_model_pin(self) -> None:
        runner = _load_runner()
        root = self._runtime(runtime_model="qwen3.8-27b-local-500k")
        with mock.patch.object(runner, "FLEET_CURRENT_ROOT", root):
            with self.assertRaises(runner.FleetError):
                runner.resolve_fleet()

    def test_resolve_fleet_rejects_missing_fleet_py(self) -> None:
        runner = _load_runner()
        root = self._runtime(include_fleet_py=False)
        with mock.patch.object(runner, "FLEET_CURRENT_ROOT", root):
            with self.assertRaises(runner.FleetError):
                runner.resolve_fleet()

    def test_resolve_fleet_accepts_valid_runtime(self) -> None:
        runner = _load_runner()
        root = self._runtime()
        with mock.patch.object(runner, "FLEET_CURRENT_ROOT", root):
            info = runner.resolve_fleet()
        self.assertEqual(info["package_version"], "0.8.32")
        self.assertEqual(info["agent"], "qwen-local")
        self.assertEqual(info["expected_model"], "huihui_ai/Qwen3.8-abliterated")

    def test_fixture_prompt_pins_exact_probe_without_extra_fields(self) -> None:
        runner = _load_runner()
        prompt = runner.build_iteration_prompt(
            project_id="project-hash",
            iteration=1,
            vector="auth-roles",
            previous_summary="",
            fixture={
                "fixture_id": "synthetic-vault-2026-09-02",
                "target_class": "TEST",
                "data_classification": "synthetic",
            },
        )
        exact_probe = (
            '{"name": "synthetic_harness_probe", "params": '
            '{"expected_outcome": "dry_run_pass", '
            '"fixture_id": "synthetic-vault-2026-09-02"}}'
        )
        self.assertIn("The probe object MUST equal exactly: " + exact_probe, prompt)
        self.assertEqual(prompt.count("REDTEAM_RESULT "), 1)
        self.assertIn(
            "summary field MUST contain exactly one line and nothing else", prompt
        )
        self.assertIn("The directive above must start at column 1", prompt)
        self.assertNotIn("data_classification", prompt)
        self.assertNotIn("target_class", prompt)

    def test_validate_probe_rejects_unknown_name(self) -> None:
        runner = _load_runner()
        with self.assertRaises(runner.FleetError):
            runner.validate_probe({"name": "shell", "params": {"cmd": "rm -rf /"}})

    def test_validate_probe_rejects_unallowed_params(self) -> None:
        runner = _load_runner()
        with self.assertRaises(runner.FleetError):
            runner.validate_probe(
                {
                    "name": "loopback_health_check",
                    "params": {"url": "http://127.0.0.1:9", "cmd": "rm -rf /"},
                }
            )

    def test_validate_probe_rejects_non_loopback_url(self) -> None:
        runner = _load_runner()
        with self.assertRaises(runner.FleetError):
            runner.validate_probe(
                {
                    "name": "loopback_health_check",
                    "params": {"url": "https://api.billingo.hu"},
                }
            )

    def test_loopback_url_accepts_only_loopback(self) -> None:
        runner = _load_runner()
        for url in (
            "http://127.0.0.1:8018/v1",
            "https://127.0.0.8/health",
            "http://localhost:9000/status",
            "http://[::1]:8080/",
        ):
            self.assertTrue(runner.is_loopback_url(url), url)

    def test_loopback_url_rejects_rfc1918_and_other_hosts(self) -> None:
        runner = _load_runner()
        for url in (
            "http://10.0.0.5:8080/",
            "http://192.168.1.10/",
            "http://172.16.0.3/",
            "http://169.254.1.1/",
            "http://0.0.0.0:8000/",
            "https://example.com/",
            "file:///etc/passwd",
            "http://localhost.evil.example/",
            "not-a-url",
            "",
        ):
            self.assertFalse(runner.is_loopback_url(url), url)

    def test_validate_probe_rejects_rfc1918_url(self) -> None:
        runner = _load_runner()
        for url in ("http://10.1.2.3/", "http://192.168.0.1/", "http://172.16.5.5/"):
            with self.assertRaises(runner.FleetError, msg=url):
                runner.validate_probe(
                    {"name": "loopback_health_check", "params": {"url": url}}
                )

    def test_validate_run_result_rejects_wrong_agent(self) -> None:
        runner = _load_runner()
        payload = {
            "action": "run",
            "status": "COMPLETED",
            "run_id": "run-1",
            "agent": "codex",
            "exit_code": 0,
            "model_identity": {
                "status": "verified",
                "expected_model": "huihui_ai/Qwen3.8-abliterated",
                "observed_models": ["huihui_ai/Qwen3.8-abliterated"],
            },
            "runtime_attestation": {"status": "VERIFIED"},
            "handoff_valid": True,
            "handoff": {
                "status": "completed",
                "summary": 'ok\nPROBE {"name": "static_vector_review", "params": {"vector": "auth-roles"}}',
            },
            "token_usage": {
                "status": "known",
                "fresh_input_tokens": 1,
                "cache_read_tokens": 0,
            },
        }
        with self.assertRaises(runner.FleetError):
            runner.validate_run_result(payload)

    def test_validate_run_result_rejects_unknown_telemetry(self) -> None:
        runner = _load_runner()
        payload = {
            "action": "run",
            "status": "COMPLETED",
            "run_id": "run-1",
            "agent": "qwen-local",
            "exit_code": 0,
            "model_identity": {
                "status": "verified",
                "expected_model": "huihui_ai/Qwen3.8-abliterated",
                "observed_models": ["huihui_ai/Qwen3.8-abliterated"],
            },
            "runtime_attestation": {"status": "VERIFIED"},
            "handoff_valid": True,
            "handoff": {
                "status": "completed",
                "summary": 'ok\nPROBE {"name": "static_vector_review", "params": {"vector": "auth-roles"}}',
            },
            "token_usage": {"status": "unknown"},
        }
        with self.assertRaises(runner.FleetError):
            runner.validate_run_result(payload)

    def test_validate_run_result_rejects_invalid_handoff(self) -> None:
        runner = _load_runner()
        payload = {
            "action": "run",
            "status": "COMPLETED",
            "run_id": "run-1",
            "agent": "qwen-local",
            "exit_code": 0,
            "model_identity": {
                "status": "verified",
                "expected_model": "huihui_ai/Qwen3.8-abliterated",
                "observed_models": ["huihui_ai/Qwen3.8-abliterated"],
            },
            "runtime_attestation": {"status": "VERIFIED"},
            "handoff_valid": False,
            "handoff": None,
            "token_usage": {
                "status": "known",
                "fresh_input_tokens": 1,
                "cache_read_tokens": 0,
            },
        }
        with self.assertRaises(runner.FleetError):
            runner.validate_run_result(payload)

    def test_parse_probe_directive_requires_directive(self) -> None:
        runner = _load_runner()
        with self.assertRaises(runner.FleetError):
            runner.parse_probe_directive("summary without a directive")

    def test_default_runner_calls_real_subprocess_and_parses_result(self) -> None:
        runner = _load_runner()
        root = self._runtime()
        with mock.patch.object(runner, "FLEET_CURRENT_ROOT", root):
            result = runner.invoke_iteration(
                project_id="proj",
                iteration=1,
                vector="auth-roles",
                previous_summary="",
                fixture=None,
                marker=None,
                workspace=str(self.workspace),
            )
        self.assertEqual(result.run_id, "run-fake-0001")
        self.assertEqual(result.observed_model, "huihui_ai/Qwen3.8-abliterated")
        self.assertEqual(result.fresh_tokens, 11)
        self.assertEqual(result.cache_read_tokens, 3)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.proposed_probe["name"], "static_vector_review")
        self.assertEqual(result.proposed_probe["params"]["vector"], "auth-roles")

    def test_generated_assessment_passes_real_fleet_red_team_hard_gates(self) -> None:
        runner = _load_runner()
        fleet_root = (
            SKILL_ROOT.parents[3]
            / "fleet-suite"
            / "payload"
            / "agent-fleet"
        )
        profiles_path = fleet_root / "references" / "profiles.json"
        assessment = runner.build_red_team_assessment(profiles_json=profiles_path)
        assessment_path = self.workspace / "real-fleet-assessment.json"
        assessment_path.write_text(json.dumps(assessment), encoding="utf-8")
        check = r'''
import copy
import json
import sys
from pathlib import Path

fleet_root = Path(sys.argv[1])
profiles_path = fleet_root / "references" / "profiles.json"
sys.path.insert(0, str(fleet_root / "scripts"))
import adaptive
import routing_v2

profiles = json.loads(profiles_path.read_text(encoding="utf-8"))["profiles"]
assessment = adaptive.load_assessment(
    Path(sys.argv[2]),
    profile_ids=profiles,
    valid_kinds=set(profiles["qwen-local"]["fit"]),
    valid_complexities=set(profiles["qwen-local"]["models"]),
)
task_profile = adaptive.normalize_task_profile(assessment)
synthetic_profile = copy.deepcopy(profiles["qwen-local"])
synthetic_profile["routing_enabled"] = True
execution_profile_id = str(
    synthetic_profile.get("execution_profile_map_by_mode", {})
    .get("review", {})
    .get("low", synthetic_profile["execution_profile_map"]["low"])
)
synthetic_profile["execution_profiles"][execution_profile_id]["lifecycle"] = "CANARY"
execution = routing_v2.resolve_execution_profile(
    "qwen-local",
    synthetic_profile,
    complexity="low",
    mode="review",
    required_tools=["filesystem-read"],
)
gates = routing_v2.hard_gates(
    execution,
    profile=synthetic_profile,
    assessment=task_profile,
    readiness={"ready": True, "reasons": []},
    quota={"status": "fresh", "headroom": 1.0, "confidence": 1.0},
    mode="review",
)
failures = {gate["code"] for gate in gates if not gate["passed"]}
assert failures == {"lifecycle"}, failures
assert routing_v2.permits_explicit_canary(execution, gates)
'''
        completed = subprocess.run(
            [sys.executable, "-c", check, str(fleet_root), str(assessment_path)],
            text=True,
            capture_output=True,
            check=False,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_default_runner_fails_closed_on_wrong_agent(self) -> None:
        runner = _load_runner()
        root = self._runtime()
        with mock.patch.object(runner, "FLEET_CURRENT_ROOT", root), mock.patch.dict(
            os.environ, {"FAKE_FLEET_AGENT": "codex"}
        ):
            with self.assertRaises(runner.FleetError):
                runner.invoke_iteration(
                    project_id="proj",
                    iteration=1,
                    vector="auth-roles",
                    previous_summary="",
                    fixture=None,
                    marker=None,
                    workspace=str(self.workspace),
                )

    def test_default_runner_fails_closed_on_unknown_telemetry(self) -> None:
        runner = _load_runner()
        root = self._runtime()
        with mock.patch.object(runner, "FLEET_CURRENT_ROOT", root), mock.patch.dict(
            os.environ, {"FAKE_FLEET_TOKEN_STATUS": "unknown"}
        ):
            with self.assertRaises(runner.FleetError):
                runner.invoke_iteration(
                    project_id="proj",
                    iteration=1,
                    vector="auth-roles",
                    previous_summary="",
                    fixture=None,
                    marker=None,
                    workspace=str(self.workspace),
                )

    def test_inject_fake_runner_and_invoke(self) -> None:
        runner = _load_runner()
        calls: list[dict[str, object]] = []

        def fake(**kwargs):
            calls.append(kwargs)
            return runner.FleetResult(
                run_id="RUNID-1",
                observed_model="huihui_ai/Qwen3.8-abliterated",
                fresh_tokens=10,
                cache_read_tokens=2,
                status="completed",
                summary="noop",
                proposed_probe={
                    "name": "synthetic_harness_probe",
                    "params": {
                        "fixture_id": "synthetic-vault-2026-09-02",
                        "expected_outcome": "dry_run_pass",
                    },
                },
                finding_status="no_findings",
                findings=[],
                evidence=["sha256:" + "a" * 64],
            )

        runner.set_runner(fake)
        result = runner.invoke_iteration(
            project_id="proj",
            iteration=1,
            vector="auth-roles",
            previous_summary="",
            fixture=None,
            marker=None,
        )
        self.assertEqual(result.fresh_tokens, 10)
        self.assertEqual(result.cache_read_tokens, 2)
        self.assertEqual(result.proposed_probe["name"], "synthetic_harness_probe")
        self.assertEqual(len(calls), 1)


class SimulateAttackFleetIntegration(SimulateAttackBase):
    def test_run_without_fleet_release_fails_closed(self) -> None:
        runtime = self.enable_fake_fleet()
        (runtime / ".agent-fleet-suite-runtime.json").unlink()
        result = self.run_cli(
            "preflight",
            "--project", str(self.project),
            "--target-class", "TEST",
            "--target-name", "synthetic",
            "--max-iterations", "1",
            "--max-fresh-tokens", "1000",
            "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 4, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "BLOCKED")
        self.assertIn("0.8.32", payload["reason"])

    def test_budget_overrun_terminates_with_policy_error(self) -> None:
        runner = _load_runner()

        def runaway(**kwargs):
            return runner.FleetResult(
                run_id="RUNID-2",
                observed_model="huihui_ai/Qwen3.8-abliterated",
                fresh_tokens=5000,
                cache_read_tokens=0,
                status="completed",
                summary="x",
                proposed_probe={
                    "name": "static_vector_review",
                    "params": {"vector": str(kwargs.get("vector"))},
                },
                finding_status="no_findings",
                findings=[],
                evidence=["sha256:" + "a" * 64],
            )

        runner.set_runner(runaway)
        module = _load_module()
        with self.assertRaises(module.PolicyError):
            module.bounded_fleet_iteration(
                project_id="proj",
                target_name="synthetic",
                iteration=1,
                vector="auth-roles",
                previous_summary="",
                fixture=None,
                fleet_runner=runner,
                fleet_marker=None,
                cumulative_fresh_tokens=1,
                max_fresh_tokens=10,
            )

    def test_synthetic_harness_probe_for_bundled_fixture(self) -> None:
        _seed_audit(self.workspace, self.project)
        self.enable_fake_fleet()
        result = self.run_cli(
            "run",
            "--project", str(self.project),
            "--target-class", "TEST",
            "--target-name", "synthetic",
            "--max-iterations", "1",
            "--max-fresh-tokens", "1000",
            "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        audit_dir = Path(payload["audit_dir"])
        iterations = [
            json.loads(line)
            for line in (audit_dir / "iterations.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(
            iterations[0]["proposed_probe"]["name"], "synthetic_harness_probe"
        )
        # Outcome is reported through the synthetic harness probe; never as
        # a successful real-world exploit.
        self.assertEqual(
            iterations[0]["redacted_observation"]["fleet_status"],
            "completed",
        )
        self.assertEqual(
            iterations[0]["proposed_probe"]["params"]["expected_outcome"],
            "dry_run_pass",
        )


class SecurityBoundaryRegressions(SimulateAttackBase):
    def test_audit_root_override_requires_explicit_test_mode(self) -> None:
        module = _load_module()
        with mock.patch.dict(
            os.environ, {"SECURITY_AUDIT_ROOT": str(self.audit_root)}, clear=False
        ):
            os.environ.pop("SECURITY_AUDIT_TEST_MODE", None)
            with self.assertRaises(module.PolicyError):
                module.audit_root()

    def test_test_class_cannot_be_self_attested_outside_synthetic_root(self) -> None:
        module = _load_module()
        outside = _make_project(self.workspace / "outside")
        with self.assertRaises(module.PolicyError):
            module.attest_target(outside, "TEST")

    def test_isolated_candidate_requires_local_registry_attestation(self) -> None:
        module = _load_module()
        (self.audit_root / "target-registry.json").unlink()
        with self.assertRaises(module.PolicyError):
            module.attest_target(self.project, "isolated_candidate")

    def test_model_workspace_is_redacted_bounded_copy(self) -> None:
        module = _load_module()
        secret = _openai_shaped()
        (self.project / "app.py").write_text(
            f"TOKEN = '{secret}'\nprint('safe')\n", encoding="utf-8"
        )
        (self.project / ".env").write_text(f"TOKEN={secret}\n", encoding="utf-8")
        snapshot = self.workspace / "snapshot"
        facts = module.build_sanitized_workspace(self.project, snapshot)
        copied = (snapshot / "app.py").read_text(encoding="utf-8")
        self.assertNotIn(secret, copied)
        self.assertIn("<redacted:openai-key@app.py>", copied)
        self.assertFalse((snapshot / ".env").exists())
        self.assertLessEqual(facts["total_bytes"], module.MAX_SNAPSHOT_BYTES)
        self.assertIn(secret, (self.project / "app.py").read_text(encoding="utf-8"))

    def test_model_receives_snapshot_not_real_project(self) -> None:
        _seed_audit(self.workspace, self.project)
        self.enable_fake_fleet()
        capture = self.workspace / "workspace-capture.txt"
        os.environ["FAKE_FLEET_WORKSPACE_CAPTURE"] = str(capture)
        self.env = os.environ.copy()
        result = self.run_cli(
            "run", "--project", str(self.project), "--target-class", "TEST",
            "--target-name", "synthetic", "--max-iterations", "1",
            "--max-fresh-tokens", "1000", "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        model_workspace = Path(capture.read_text(encoding="utf-8"))
        self.assertNotEqual(model_workspace, self.project)
        self.assertFalse(model_workspace.exists(), "snapshot must be deleted after the run")

    def test_adversarial_run_preserves_baseline_freshness_receipt(self) -> None:
        _seed_audit(self.workspace, self.project)
        module = _load_module()
        _, project_dir = module.project_identity(self.project)
        baseline = (project_dir / "latest.json").read_bytes()
        self.enable_fake_fleet()
        result = self.run_cli(
            "run", "--project", str(self.project), "--target-class", "TEST",
            "--target-name", "synthetic", "--max-iterations", "1",
            "--max-fresh-tokens", "1000", "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((project_dir / "latest.json").read_bytes(), baseline)
        self.assertTrue((project_dir / "adversarial-latest.json").is_file())
        self.assertTrue((project_dir / "adversarial-ledger.jsonl").is_file())

    def test_missing_explicit_evidence_blocks_without_publishing(self) -> None:
        _seed_audit(self.workspace, self.project)
        self.enable_fake_fleet()
        os.environ["FAKE_FLEET_BAD_RESULT"] = "1"
        self.env = os.environ.copy()
        result = self.run_cli(
            "run", "--project", str(self.project), "--target-class", "TEST",
            "--target-name", "synthetic", "--max-iterations", "1",
            "--max-fresh-tokens", "1000", "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertIn("evidence must be a non-empty list", result.stdout)

    def test_finding_produces_revise_and_persists_only_redacted_value(self) -> None:
        _seed_audit(self.workspace, self.project)
        self.enable_fake_fleet()
        os.environ["FAKE_FLEET_FINDING"] = "1"
        self.env = os.environ.copy()
        result = self.run_cli(
            "run", "--project", str(self.project), "--target-class", "TEST",
            "--target-name", "synthetic", "--max-iterations", "1",
            "--max-fresh-tokens", "1000", "--trigger", "next_substantive_work",
        )
        self.assertEqual(result.returncode, 3, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["verdict"], "REVISE")
        combined = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in Path(payload["audit_dir"]).rglob("*")
            if path.is_file()
        )
        self.assertNotIn(_openai_shaped(), combined)
        self.assertIn("<redacted:openai-key@", combined)

    def test_residual_scan_fails_closed(self) -> None:
        module = _load_module()
        staged = self.workspace / "staged"
        staged.mkdir()
        (staged / "bad.json").write_text(
            json.dumps({"value": _openai_shaped()}), encoding="utf-8"
        )
        with self.assertRaises(module.PolicyError):
            module.verify_redacted_artifacts(staged)


class StrictFleetResultRegressions(unittest.TestCase):
    def test_allowlisted_probe_rejects_denied_executable_payload(self) -> None:
        runner = _load_runner()
        with self.assertRaises(runner.FleetError):
            runner.validate_probe(
                {
                    "name": "loopback_health_check",
                    "params": {"url": "http://127.0.0.1/billingo"},
                }
            )

    def test_redteam_result_rejects_incomplete_finding(self) -> None:
        runner = _load_runner()
        value = {
            "probe": {"name": "static_vector_review", "params": {"vector": "auth-roles"}},
            "finding_status": "findings",
            "findings": [{"finding_id": "RT-1"}],
            "evidence": ["sha256:" + "a" * 64],
        }
        with self.assertRaises(runner.FleetError):
            runner.validate_redteam_result(value)

    def test_redteam_result_requires_explicit_no_findings_evidence(self) -> None:
        runner = _load_runner()
        value = {
            "probe": {"name": "static_vector_review", "params": {"vector": "auth-roles"}},
            "finding_status": "no_findings",
            "findings": [],
            "evidence": [],
        }
        with self.assertRaises(runner.FleetError):
            runner.validate_redteam_result(value)


class SyntheticDryRunFixture(unittest.TestCase):
    """Synthetic dry-run fixture - created and inspected, never executed."""

    def test_fixture_is_present_and_well_formed(self) -> None:
        self.assertTrue(FIXTURE.exists(), f"missing synthetic fixture at {FIXTURE}")
        manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["target_class"], "TEST")
        self.assertEqual(manifest["target_name"], "synthetic-vault")
        # The manifest must never advertise words that the gate rejects.
        for marker in ("prod", "live", "customer", "billing", "mail", "calendar"):
            for key, value in manifest.items():
                if isinstance(value, str):
                    self.assertNotIn(
                        marker,
                        value.lower(),
                        f"{key} contains {marker}",
                    )
        # Confirm the fixture never imports the real helper.
        helper = SCRIPT
        helper_text = helper.read_text(encoding="utf-8")
        self.assertNotIn("subprocess", helper_text.split("def perform_reconnaissance")[0])

    def test_fixture_is_not_executed_against_a_real_target(self) -> None:
        # The fixture's manifest must declare itself as not-executed.
        manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["dry_run_only"])
        self.assertEqual(manifest["executed"], False)


class DenyTokenOrderingRegression(SimulateAttackBase):
    """Regression test for the deny-token ordering defect."""

    def test_billingo_host_is_preferred_over_verb_token(self) -> None:
        module = _load_module()
        # A command that contains both the network host and the verb token
        # must report the more specific host denial.
        self.assertEqual(
            module.contains_deny_action("curl https://api.billingo.hu/v3/invoice"),
            "api.billingo.hu",
        )
        self.assertEqual(
            module.contains_deny_action("stripe charge 1000"),
            "stripe",
        )


class FreshAuditFallbackRegression(SimulateAttackBase):
    """Regression test for the fresh-audit fallback defect."""

    def test_pre_production_release_candidate_escalates_without_payload(self) -> None:
        module = _load_module()
        # Do not seed any audit. ``pre_production_release_candidate`` must
        # still escalate so a missing prior audit cannot bypass the gate.
        escalation = module.freshness_escalation_required(
            self.project, "pre_production_release_candidate"
        )
        self.assertTrue(escalation["escalation_required"])

    def test_next_substantive_work_without_payload_escalates(self) -> None:
        module = _load_module()
        escalation = module.freshness_escalation_required(
            self.project, "next_substantive_work"
        )
        self.assertTrue(escalation["escalation_required"])


class MissingBudgetPolicyExitRegression(unittest.TestCase):
    """Regression test for the missing-budget policy exit defect."""

    def test_budget_check_runs_before_iteration(self) -> None:
        module = _load_module()
        runner = _load_runner()
        with self.assertRaises(module.PolicyError):
            module.bounded_fleet_iteration(
                project_id="proj",
                target_name="synthetic",
                iteration=1,
                vector="auth-roles",
                previous_summary="",
                fixture=None,
                fleet_runner=runner,
                fleet_marker=None,
                cumulative_fresh_tokens=11,
                max_fresh_tokens=10,
            )


class NestedRedactionRegression(unittest.TestCase):
    """Regression test for nested redaction markers."""

    def test_redact_text_does_not_double_wrap(self) -> None:
        module = _load_module()
        marker = "<redacted:aws-access-key@config.py>"
        text = f"{marker}\nEMAIL={PLACEHOLDER_EMAIL}\n"
        first = module.redact_text(text, "config.py")
        # Running redaction again must be idempotent.
        second = module.redact_text(first, "config.py")
        self.assertEqual(first, second)


class ForbiddenFixtureWordingRegression(unittest.TestCase):
    """Regression test for the forbidden fixture wording defect."""

    def test_manifest_contains_no_target_class_violations(self) -> None:
        manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
        forbidden = ("prod", "live", "customer", "billing", "mail", "calendar")
        for key, value in manifest.items():
            if isinstance(value, str):
                lowered = value.lower()
                for marker in forbidden:
                    self.assertNotIn(marker, lowered, f"{key} contains {marker}")


class RealShapedFakeSecretsRegression(unittest.TestCase):
    """Regression test for real-shaped fake secrets in tests."""

    def test_test_placeholders_are_not_real_shaped(self) -> None:
        # The placeholders used in this test file must not match the
        # ``aws-access-key`` or ``openai-key`` redaction regex on their
        # own (otherwise the test is shipping a real-shaped fake secret).
        module = _load_module()
        aws_rule = next(
            pattern for kind, pattern in module.REDACT_RULES
            if kind == "aws-access-key"
        )
        openai_rule = next(
            pattern for kind, pattern in module.REDACT_RULES
            if kind == "openai-key"
        )
        github_rule = next(
            pattern for kind, pattern in module.REDACT_RULES
            if kind == "github-token"
        )
        self.assertFalse(aws_rule.search(PLACEHOLDER_AWS))
        self.assertFalse(openai_rule.search(PLACEHOLDER_OPENAI))
        self.assertFalse(github_rule.search(PLACEHOLDER_GITHUB))


if __name__ == "__main__":
    unittest.main()
