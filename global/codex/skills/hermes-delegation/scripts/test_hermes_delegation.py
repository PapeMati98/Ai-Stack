#!/usr/bin/env python3
"""Contract and safety tests for the Hermes delegation bridge."""

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


import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = load_module("hermes_delegate_under_test", "hermes_delegate.py")
worker = load_module("hermes_remote_worker_under_test", "hermes_remote_worker.py")


def contract_for(
    *,
    mode: str = "REPORT_ONLY",
    target: str | None = None,
    workdir: str | None = None,
    action_manifest: dict | None = None,
    skills: list[str] | None = None,
) -> dict:
    if target is None:
        target = {
            "PRIVATE_READ": "mailbox",
            "INFRA_OBSERVE": "lab-server",
            "PREPARE_PATCH": "lab-server",
            "INFRA_CHANGE": "lab-server",
            "EXTERNAL_ACTION": "mailbox",
            "SCHEDULED_AUTONOMY": "lab-server",
        }.get(mode, "hermes")
    value = {
        "schema_version": 1,
        "bridge_version": client.VERSION,
        "task_id": "hdt-20260819T200000Z-0123abcd",
        "created_at": "2026-08-19T20:00:00Z",
        "requested_for": "Mateusz",
        "requesting_agent": "codex",
        "mode": mode,
        "title": "Bounded test",
        "task": "Inspect one bounded item and return evidence.",
        "target": {
            "kind": target,
            "identifier": target,
            "workdir": workdir,
        },
        "acceptance_criteria": ["Return status and evidence."],
        "requested_skills": [] if skills is None else skills,
        "runtime": {
            "max_turns": 8,
            "timeout_seconds": 600,
            "reasoning": "medium",
        },
        "approval": {
            "required": mode in client.SENSITIVE_MODES,
            "policy": "explicit-owner-exact-contract",
            "action_manifest": action_manifest,
        },
        "delegation": {
            "depth": 0,
            "max_depth": 1,
            "return_to": "codex",
        },
    }
    value["contract_sha256"] = client.contract_hash(value)
    return value


def email_manifest() -> dict:
    return {
        "action_type": "send-email",
        "scope": "Send exactly one approved completion notice.",
        "max_actions": 1,
        "sender": "approved-account",
        "recipients": ["recipient@example.invalid"],
        "subject": "Approved subject",
        "body_sha256": "a" * 64,
        "attachment_sha256": ["b" * 64],
    }


def patch_manifest(workdir: str) -> dict:
    return {
        "action_type": "prepare-patch",
        "scope": "Prepare one bounded patch without deployment.",
        "max_actions": 1,
        "repository": "demo",
        "worktree": workdir,
        "allowed_paths": ["src/"],
        "test_commands": ["python -m unittest"],
    }


def approval_for(value: dict, *, valid_minutes: int = 15) -> dict:
    now = client.utc_now()
    return {
        "schema_version": 1,
        "receipt_id": "hap-20260819T200000Z-0123abcd",
        "task_id": value["task_id"],
        "contract_sha256": value["contract_sha256"],
        "approved_by": "Mateusz",
        "approved_at": client.iso_utc(now),
        "valid_until": client.iso_utc(now + timedelta(minutes=valid_minutes)),
        "one_time": True,
        "consumed": False,
        "consumed_at": None,
        "action_manifest_sha256": client.sha256_bytes(
            client.canonical_bytes(value["approval"]["action_manifest"])
        ),
    }


class ClientContractTests(unittest.TestCase):
    def test_emit_survives_cp1250_console_with_unicode(self) -> None:
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1250")
        with mock.patch.object(sys, "stdout", stream):
            client.emit({"message": "\u26a0 gotowe"})
            stream.flush()
        rendered = raw.getvalue().decode("cp1250")
        self.assertIn("\\u26a0", rendered)
        self.assertEqual(json.loads(rendered)["message"], "\u26a0 gotowe")

    def test_report_only_contract_is_valid_without_approval(self) -> None:
        value = contract_for()
        self.assertIs(client.validate_contract(value), value)

    def test_contract_mutation_breaks_hash(self) -> None:
        value = contract_for()
        value["task"] = "A broader task inserted after review."
        with self.assertRaisesRegex(client.BridgeError, "SHA-256 mismatch"):
            client.validate_contract(value)

    def test_prepare_patch_requires_explicit_workdir(self) -> None:
        value = contract_for(
            mode="PREPARE_PATCH",
            target="lab-server",
            action_manifest=patch_manifest("/srv/ai-lab/workspaces/demo"),
        )
        with self.assertRaisesRegex(client.BridgeError, "requires an explicit workdir"):
            client.validate_contract(value)

    def test_sensitive_mode_requires_manifest(self) -> None:
        value = contract_for(mode="EXTERNAL_ACTION")
        with self.assertRaisesRegex(client.BridgeError, "action manifest"):
            client.validate_contract(value)

    def test_external_action_accepts_bound_email_manifest(self) -> None:
        value = contract_for(
            mode="EXTERNAL_ACTION",
            target="mailbox",
            action_manifest=email_manifest(),
            skills=["client-mailbox-knowledge"],
        )
        self.assertIs(client.validate_contract(value), value)

    def test_mode_action_mismatch_is_rejected(self) -> None:
        value = contract_for(
            mode="INFRA_CHANGE",
            target="lab-server",
            action_manifest=email_manifest(),
            skills=["ai-lab-infrastructure-operations"],
        )
        with self.assertRaisesRegex(client.BridgeError, "incompatible"):
            client.validate_contract(value)

    def test_wrong_approval_phrase_is_rejected(self) -> None:
        value = contract_for(
            mode="EXTERNAL_ACTION",
            target="mailbox",
            action_manifest=email_manifest(),
        )
        with tempfile.TemporaryDirectory() as temp:
            contract_path = Path(temp) / "contract.json"
            contract_path.write_text(json.dumps(value), encoding="utf-8")
            args = argparse.Namespace(
                contract=str(contract_path),
                phrase="APPROVE HERMES wrong contract",
                valid_minutes=15,
            )
            with self.assertRaisesRegex(client.BridgeError, "does not match"):
                client.cmd_approve(args)

    def test_approval_receipt_is_exact_and_one_time(self) -> None:
        value = contract_for(
            mode="EXTERNAL_ACTION",
            target="mailbox",
            action_manifest=email_manifest(),
        )
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"HERMES_DELEGATION_STATE": temp}
        ):
            contract_path = Path(temp) / "contract.json"
            contract_path.write_text(json.dumps(value), encoding="utf-8")
            args = argparse.Namespace(
                contract=str(contract_path),
                phrase=client.approval_phrase(value),
                valid_minutes=15,
            )
            result = client.cmd_approve(args)
            receipt = client.read_json(Path(result["receipt_path"]))
            client.validate_receipt(receipt, value)
            receipt["consumed"] = True
            receipt["consumed_at"] = client.iso_utc()
            with self.assertRaisesRegex(client.BridgeError, "already been consumed"):
                client.validate_receipt(receipt, value)

    def test_expired_approval_is_rejected(self) -> None:
        value = contract_for(
            mode="EXTERNAL_ACTION",
            target="mailbox",
            action_manifest=email_manifest(),
        )
        receipt = {
            "schema_version": 1,
            "receipt_id": "hap-20260819T200000Z-0123abcd",
            "task_id": value["task_id"],
            "contract_sha256": value["contract_sha256"],
            "approved_by": "Mateusz",
            "approved_at": client.iso_utc(client.utc_now() - timedelta(minutes=10)),
            "valid_until": client.iso_utc(client.utc_now() - timedelta(minutes=1)),
            "one_time": True,
            "consumed": False,
            "consumed_at": None,
            "action_manifest_sha256": client.sha256_bytes(
                client.canonical_bytes(value["approval"]["action_manifest"])
            ),
        }
        with self.assertRaisesRegex(client.BridgeError, "expired"):
            client.validate_receipt(receipt, value)

    def test_config_cannot_redirect_ssh_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text(
                json.dumps({"ssh_host": "-oProxyCommand=unexpected"}),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"HERMES_DELEGATION_CONFIG": str(config)}
            ), self.assertRaisesRegex(client.BridgeError, "unexpected keys"):
                client.load_config()

    def test_remote_install_requires_current_exact_approval(self) -> None:
        with mock.patch.object(client, "remote_shell") as remote_shell:
            args = argparse.Namespace(approval_phrase="not-approved")
            with self.assertRaisesRegex(client.BridgeError, "exact current approval"):
                client.cmd_install_remote(args)
            remote_shell.assert_not_called()


class RemoteWorkerTests(unittest.TestCase):
    def test_worker_rejects_non_allowlisted_skill(self) -> None:
        value = contract_for(skills=["untrusted-external-skill"])
        with self.assertRaisesRegex(worker.WorkerError, "non-allowlisted"):
            worker.validate_contract(value)

    def test_worker_rejects_write_outside_allowlist(self) -> None:
        workdir = "/etc"
        value = contract_for(
            mode="PREPARE_PATCH",
            target="lab-server",
            workdir=workdir,
            action_manifest=patch_manifest(workdir),
            skills=["systematic-debugging"],
        )
        prefixes = (worker.Path("/srv/ai-lab").resolve(strict=False),)
        with mock.patch.object(
            worker, "WRITE_WORKDIR_PREFIXES", prefixes
        ), self.assertRaisesRegex(worker.WorkerError, "outside allowlisted"):
            worker.validate_contract(value)

    def test_worker_accepts_write_inside_allowlist(self) -> None:
        workdir = "/srv/ai-lab/workspaces/demo"
        value = contract_for(
            mode="PREPARE_PATCH",
            target="lab-server",
            workdir=workdir,
            action_manifest=patch_manifest(workdir),
            skills=["systematic-debugging"],
        )
        prefixes = (worker.Path("/srv/ai-lab").resolve(strict=False),)
        with mock.patch.object(
            worker, "WRITE_WORKDIR_PREFIXES", prefixes
        ), mock.patch.object(worker, "validate_isolated_worktree"):
            self.assertIs(worker.validate_contract(value), value)

    def test_worker_command_has_no_unattended_override(self) -> None:
        value = contract_for()
        command = worker.hermes_command(value)
        self.assertNotIn("--yolo", command)
        self.assertNotIn("--accept-hooks", command)
        self.assertIn("--safe-mode", command)
        self.assertEqual(command[command.index("-t") + 1], "todo")
        self.assertEqual(command.count("-q"), 1)
        prompt = command[command.index("-q") + 1]
        self.assertIn(value["task_id"], prompt)
        self.assertIn(value["contract_sha256"], prompt)

    def test_worker_redacts_common_secret_shapes(self) -> None:
        redacted, count = worker.redact(
            ('api_key=' + _redacted_fixture('assigned-secret', 1) + ' password=synthetic-password "access_token": "synthetic-json-token"')
        )
        self.assertGreaterEqual(count, 3)
        self.assertNotIn((_redacted_fixture('assigned-secret', 1)), redacted)
        self.assertNotIn("synthetic-password", redacted)
        self.assertNotIn("synthetic-json-token", redacted)

    def test_result_envelope_does_not_disclose_raw_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            worker, "ROOT", Path(temp)
        ):
            worker.ensure_layout()
            directory = worker.task_dir("hdt-20260819T200000Z-0123abcd")
            directory.mkdir(mode=0o700)
            result = worker.result_envelope(
                "hdt-20260819T200000Z-0123abcd",
                "COMPLETED",
                "STATUS: COMPLETED\n",
                "",
                0,
            )
            self.assertNotIn("full_stdout_path", result)
            self.assertNotIn("full_stderr_path", result)
            self.assertFalse(result["full_output_retained"])
            self.assertEqual(result["full_output_access"], "none")

    def test_result_envelope_persists_only_redacted_output(self) -> None:
        stdout_secret = ('api_key=' + _redacted_fixture('assigned-secret', 2))
        stderr_secret = "password=stderr-synthetic-secret"
        stdout = f"STATUS: COMPLETED\n{stdout_secret}\n"
        stderr = f"worker warning: {stderr_secret}\n"
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            worker, "ROOT", Path(temp)
        ):
            worker.ensure_layout()
            directory = worker.task_dir("hdt-20260819T200000Z-0123abcd")
            directory.mkdir(mode=0o700)
            result = worker.result_envelope(
                "hdt-20260819T200000Z-0123abcd",
                "COMPLETED",
                stdout,
                stderr,
                0,
            )

            persisted_stdout = (directory / "stdout.txt").read_text(encoding="utf-8")
            persisted_stderr = (directory / "stderr.txt").read_text(encoding="utf-8")
            envelope = json.dumps(result, ensure_ascii=False)
            self.assertNotIn(stdout_secret, persisted_stdout)
            self.assertNotIn(stderr_secret, persisted_stderr)
            self.assertNotIn(stdout_secret, envelope)
            self.assertNotIn(stderr_secret, envelope)
            self.assertIn("[REDACTED]", persisted_stdout)
            self.assertIn("[REDACTED]", persisted_stderr)
            self.assertEqual(result["full_stdout_sha256"], worker.sha256_file(directory / "stdout.txt"))
            self.assertEqual(result["full_stderr_sha256"], worker.sha256_file(directory / "stderr.txt"))
            self.assertGreaterEqual(result["redaction_count"], 2)

    def test_result_requires_declared_final_status(self) -> None:
        self.assertEqual(
            worker.classify_result_status(0, "ordinary prose", ""),
            "FAILED_FORMAT",
        )
        self.assertEqual(
            worker.classify_result_status(0, "STATUS: PARTIAL\n", ""),
            "PARTIAL",
        )

    def test_worker_rejects_external_action_without_receipt(self) -> None:
        value = contract_for(
            mode="EXTERNAL_ACTION",
            target="mailbox",
            action_manifest=email_manifest(),
            skills=["client-mailbox-knowledge"],
        )
        worker.validate_contract(value)
        with self.assertRaisesRegex(worker.WorkerError, "APPROVAL_REQUIRED"):
            worker.validate_approval(None, value)

    def test_report_only_rejects_mail_skill(self) -> None:
        value = contract_for(skills=["himalaya"])
        with self.assertRaisesRegex(worker.WorkerError, "incompatible with mode"):
            worker.validate_contract(value)

    def test_email_contract_rejects_multiple_actions(self) -> None:
        manifest = email_manifest()
        manifest["max_actions"] = 2
        value = contract_for(
            mode="EXTERNAL_ACTION",
            target="mailbox",
            action_manifest=manifest,
            skills=["himalaya"],
        )
        with self.assertRaisesRegex(client.BridgeError, "max_actions=1"):
            client.validate_contract(value)

    def test_remote_approval_claim_survives_task_state(self) -> None:
        value = contract_for(
            mode="EXTERNAL_ACTION",
            target="mailbox",
            action_manifest=email_manifest(),
            skills=["client-mailbox-knowledge"],
        )
        receipt = approval_for(value)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            worker, "ROOT", Path(temp)
        ):
            worker.ensure_layout()
            worker.claim_approval(receipt, value)
            with self.assertRaisesRegex(worker.WorkerError, "already been claimed"):
                worker.claim_approval(receipt, value)

    def test_worker_revalidates_and_consumes_approval_before_execution(self) -> None:
        value = contract_for(
            mode="EXTERNAL_ACTION",
            target="mailbox",
            action_manifest=email_manifest(),
            skills=["client-mailbox-knowledge"],
        )
        receipt = approval_for(value)
        fake_fcntl = SimpleNamespace(LOCK_EX=1, flock=lambda *_args: None)
        completed = subprocess.CompletedProcess(
            args=["hermes"], returncode=0, stdout="STATUS: COMPLETED\n", stderr=""
        )
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            worker, "ROOT", Path(temp)
        ), mock.patch.object(worker, "verify_identity"), mock.patch.object(
            worker, "fcntl", fake_fcntl
        ), mock.patch.object(
            worker.subprocess, "run", return_value=completed
        ) as run:
            worker.ensure_layout()
            directory = worker.task_dir(value["task_id"])
            directory.mkdir(mode=0o700)
            worker.atomic_write_json(directory / "contract.json", value)
            worker.atomic_write_json(directory / "approval.json", receipt)
            worker.update_status(value["task_id"], "QUEUED")
            self.assertEqual(worker.execute(value["task_id"]), 0)
            consumed = worker.read_json(directory / "approval.json")
            self.assertTrue(consumed["consumed"])
            self.assertIsNotNone(consumed["consumed_at"])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(worker.execute(value["task_id"]), 0)
            self.assertEqual(run.call_count, 1)

    def test_expired_queued_approval_blocks_execution(self) -> None:
        value = contract_for(
            mode="EXTERNAL_ACTION",
            target="mailbox",
            action_manifest=email_manifest(),
            skills=["client-mailbox-knowledge"],
        )
        receipt = approval_for(value, valid_minutes=-1)
        fake_fcntl = SimpleNamespace(LOCK_EX=1, flock=lambda *_args: None)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            worker, "ROOT", Path(temp)
        ), mock.patch.object(worker, "verify_identity"), mock.patch.object(
            worker, "fcntl", fake_fcntl
        ), mock.patch.object(worker.subprocess, "run") as run:
            worker.ensure_layout()
            directory = worker.task_dir(value["task_id"])
            directory.mkdir(mode=0o700)
            worker.atomic_write_json(directory / "contract.json", value)
            worker.atomic_write_json(directory / "approval.json", receipt)
            worker.update_status(value["task_id"], "QUEUED")
            self.assertEqual(worker.execute(value["task_id"]), 1)
            status = worker.read_json(worker.status_path(value["task_id"]))
            self.assertEqual(status["status"], "BLOCKED_APPROVAL")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
