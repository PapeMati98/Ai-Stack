#!/usr/bin/env python3
"""Isolated regression tests for the generic Capability Hub V1.2 rollout."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

import capability_rollout as rollout_module
from capability_core import CapabilityError, canonical_json_hash, read_marker
from capability_rollout import (
    APPROVAL_KIND,
    CapabilityRollout,
    ENGINE_PATHS,
    InjectedCrash,
    canonical_json_bytes,
    immutable_write_bytes,
    snapshot_target,
)
from capability_matrix import load_matrix, scan_root_inventory
from capability_revocations import load_revocations
from capability_targets import (
    FIXTURE_PREFIX,
    current_os_identity,
    load_registry,
    resolve_target,
)


POSTURE = "fixture owner-confirmed posture"
LEGACY_ENGINE_COMMIT = "618ca17b027c09bd2a510fb087390d335e25b178"


def file_state(root: Path) -> dict[str, tuple[str, int, int, bytes]]:
    state: dict[str, tuple[str, int, int, bytes]] = {}
    for path in (root, *root.rglob("*")):
        details = path.lstat()
        if path.is_symlink():
            kind = "symlink"
            raw = os.readlink(path).encode("utf-8")
        elif path.is_dir():
            kind = "directory"
            raw = b""
        elif path.is_file():
            kind = "file"
            try:
                raw = path.read_bytes()
            except PermissionError:
                raw = b"<locked>"
        else:
            kind = "other"
            raw = b""
        relative = "." if path == root else path.relative_to(root).as_posix()
        state[relative] = (
            kind,
            stat.S_IMODE(details.st_mode),
            details.st_mtime_ns,
            raw,
        )
    return state


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def git_bytes(repo: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


class RolloutFixture:
    def __init__(self, hub_root: Path, tool: str = "antigravity") -> None:
        self.hub_root = hub_root
        self.root = Path(tempfile.mkdtemp(prefix=FIXTURE_PREFIX))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Capability Test")
        git(self.repo, "config", "user.email", "capability@example.invalid")
        self.registry = load_registry(hub_root / "capability-targets.json")
        self.tool = tool
        self.target = resolve_target(
            self.registry, tool, "fixture-skill", fixture_root=self.root
        )
        self.target.scan_root.mkdir(parents=True)
        for guard_id, path in self.target.guards.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"{guard_id}\n".encode("ascii"))
        self.receipts = self.root / "receipts"
        self.receipts.mkdir()
        self.matrix_inventory_sha256 = scan_root_inventory(self.target.scan_root)
        self.commit_v1 = self._commit_release("0.1.0", "first release")

    def cleanup(self) -> None:
        def make_writable(function, path, error) -> None:
            os.chmod(path, stat.S_IWRITE)
            function(path)

        shutil.rmtree(self.root, onerror=make_writable)

    def _commit_release(self, version: str, body: str) -> str:
        release = self.repo / "capabilities/fixture-skill"
        references = release / "references"
        references.mkdir(parents=True, exist_ok=True)
        (release / "SKILL.md").write_text(
            "---\n"
            "name: fixture-skill\n"
            "description: Fixture-only instruction skill used by transaction tests.\n"
            "---\n\n"
            f"# Fixture Skill\n\n{body}\n",
            encoding="ascii",
            newline="\n",
        )
        (references / "guide.md").write_text(
            f"# Guide\n\nVersion {version}.\n", encoding="ascii", newline="\n"
        )
        manifest = {
            "capability_id": "fixture-skill",
            "capability_version": version,
            "declared_permissions": [],
            "enforcement": "none",
            "entrypoint": "SKILL.md",
            "kind": "instruction-skill",
            "revoked_at": None,
            "schema_version": 1,
        }
        (release / "capability.json").write_text(
            json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            encoding="ascii",
            newline="\n",
        )
        git(self.repo, "add", "--", "capabilities/fixture-skill")
        git(self.repo, "commit", "-q", "-m", f"release {version}")
        return git(self.repo, "rev-parse", "HEAD")

    def commit_v2(self) -> str:
        return self._commit_release("0.1.1", "second release")

    def commit_v3(self) -> str:
        return self._commit_release("0.1.2", "third release")

    def commit_unrelated(self) -> str:
        (self.repo / "README.md").write_text(
            "Unrelated policy change.\n", encoding="ascii", newline="\n"
        )
        git(self.repo, "add", "--", "README.md")
        git(self.repo, "commit", "-q", "-m", "unrelated policy change")
        return git(self.repo, "rev-parse", "HEAD")

    def rollout(self, commit: str) -> CapabilityRollout:
        now = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        return CapabilityRollout(
            repo=self.repo,
            commit=commit,
            capability_id="fixture-skill",
            target=self.target,
            approval_posture=POSTURE,
            registry_sha256=__import__("hashlib")
            .sha256((self.hub_root / "capability-targets.json").read_bytes())
            .hexdigest(),
            matrix_sha256="b" * 64,
            matrix_status="eligible",
            scan_root_inventory_sha256=self.matrix_inventory_sha256,
            policy_commit=commit,
            revocation_catalog=load_revocations(
                self.hub_root / "capability-revocations.json"
            ),
            revocation_checked_at=now,
        )

    def receipt(
        self,
        context: dict[str, object],
        *,
        approval_id: str | None = None,
        actions: list[str] | None = None,
    ) -> Path:
        now = dt.datetime.now(dt.timezone.utc)
        value = {
            "approval_id": approval_id or f"fixture-{uuid.uuid4().hex}",
            "approval_kind": APPROVAL_KIND,
            "approved_at": now.isoformat().replace("+00:00", "Z"),
            "approved_by": "Mateusz",
            "authorized_actions": sorted(
                actions
                or [
                    "deploy",
                    "recover-deploy",
                ]
            ),
            "context": context,
            "context_sha256": __import__(
                "capability_core"
            ).canonical_json_hash(context),
            "expires_at": (now + dt.timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z"),
            "owner_decision": "GO V1.2: fixture-only transaction",
            "schema_version": 1,
        }
        path = self.receipts / f"{value['approval_id']}.json"
        path.write_bytes(canonical_json_bytes(value))
        return path

    def rollback_receipt(
        self,
        rollout: CapabilityRollout,
        *,
        emergency: bool,
        approval_id: str | None = None,
        actions: list[str] | None = None,
    ) -> Path:
        plan = rollout.plan_rollback(emergency=emergency)
        return self.rollback_receipt_from_plan(
            plan,
            emergency=emergency,
            approval_id=approval_id,
            actions=actions,
        )

    def rollback_receipt_from_plan(
        self,
        plan: dict[str, object],
        *,
        emergency: bool,
        approval_id: str | None = None,
        actions: list[str] | None = None,
    ) -> Path:
        selected_actions = actions or list(plan["required_actions"])
        receipt = self.receipt(
            plan["approval_context"],
            approval_id=approval_id,
            actions=selected_actions,
        )
        value = json.loads(receipt.read_text("ascii"))
        fragments = plan.get("required_owner_decision_fragments") or [
            plan["required_owner_decision_fragment"]
        ]
        decision = "GO V1.2: fixture-only transaction; " + "; ".join(
            str(fragment) for fragment in fragments
        )
        if emergency:
            decision += "; EMERGENCY ROLLBACK WITHOUT DISCOVERY"
        value["owner_decision"] = decision
        receipt.write_bytes(canonical_json_bytes(value))
        return receipt

    def cli_fixture(self, commit: str | None = None) -> tuple[Path, list[str]]:
        matrix = json.loads(
            (self.hub_root / "capability-matrix-v1.2.json").read_text("ascii")
        )
        matrix["capability_id"] = "fixture-skill"
        matrix["verified_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        for row in matrix["targets"]:
            if row["tool"] == "antigravity":
                row["approval_posture"] = POSTURE
                row["blocker"] = None
                row["rollout_status"] = "eligible"
                row["scan_root_inventory_sha256"] = scan_root_inventory(
                    self.target.scan_root
                )
        matrix_path = self.root / "matrix.json"
        matrix_path.write_bytes(canonical_json_bytes(matrix))
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        revocations_path = self.root / "revocations.json"
        revocations_path.write_bytes(
            canonical_json_bytes(
                {"entries": [], "schema_version": 1, "updated_at": now}
            )
        )
        common = [
            "--repo",
            str(self.repo),
            "--commit",
            commit or self.commit_v1,
            "--policy-commit",
            commit or self.commit_v1,
            "--capability-id",
            "fixture-skill",
            "--tool",
            "antigravity",
            "--registry",
            str(self.hub_root / "capability-targets.json"),
            "--matrix",
            str(matrix_path),
            "--revocations",
            str(revocations_path),
            "--fixture-root",
            str(self.root),
        ]
        return self.hub_root / "scripts/capability_rollout.py", common

    def engine_at(self, commit: str) -> Path:
        root = self.root / f"engine-{commit[:12]}"
        for relative in ENGINE_PATHS:
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(
                git_bytes(self.hub_root, "show", f"{commit}:{relative}")
            )
        return root / "scripts/capability_rollout.py"

    def discovery_evidence(
        self,
        deployed: dict[str, object],
        *,
        rule: str = "# Fixture Skill",
        target=None,
        output_root: Path | None = None,
    ) -> tuple[dict[str, object], Path]:
        selected_target = target or self.target
        transaction_id = str(deployed["transaction_id"])
        operation = selected_target.operations_root / transaction_id
        journal = json.loads((operation / "journal.json").read_text("ascii"))
        context = journal["context"]
        challenge = str(journal["discovery_challenge"])
        skill_path = str(selected_target.target / "SKILL.md")
        output = "\n".join(
            (
                "Fresh runtime discovery result",
                f"path={skill_path}",
                f"version={context['capability_version']}",
                f"deploy_transaction_id={transaction_id}",
                f"discovery_challenge={challenge}",
                f"distinctive_rule={rule}",
            )
        )
        digest = __import__("hashlib").sha256
        evidence: dict[str, object] = {
            "activation_boundary": context["activation_boundary"],
            "capability_id": context["capability_id"],
            "capability_version": context["capability_version"],
            "deploy_transaction_id": transaction_id,
            "discovered_skill_path": skill_path,
            "discovery_challenge": challenge,
            "distinctive_rule": rule,
            "distinctive_rule_sha256": digest(rule.encode("utf-8")).hexdigest(),
            "fresh_session": True,
            "observed_at": dt.datetime.now(dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "observed_output": output,
            "observed_output_sha256": digest(output.encode("utf-8")).hexdigest(),
            "observer": "fixture fresh process",
            "runtime_version": "fixture-runtime 1.0",
            "schema_version": 2,
            "source_commit": context["source_commit"],
            "source_tree": context["source_tree"],
            "tool": context["tool"],
        }
        path = (output_root or self.root) / f"discovery-{uuid.uuid4().hex}.json"
        path.write_bytes(canonical_json_bytes(evidence))
        return evidence, path

    def record_discovery(
        self, rollout: CapabilityRollout, deployed: dict[str, object]
    ) -> dict[str, object]:
        _, evidence_path = self.discovery_evidence(deployed)
        plan = rollout.plan_discovery(evidence_path)
        receipt = self.receipt(
            plan["approval_context"], actions=["record-discovery"]
        )
        return rollout.record_discovery(
            receipt, evidence_path, owner_authorized=True
        )


class RealModeFixture:
    """Temporary canonical repository that exercises the real public CLI path."""

    def __init__(self, fixture: RolloutFixture) -> None:
        self.fixture = fixture
        self.root = fixture.root / "real-mode"
        self.canonical = self.root / "canonical"
        self.profile_root = self.root / "profile"
        self.scan_root = self.profile_root / "skills"
        self.guard_root = self.profile_root / "guards"
        self.state_parent = self.root / "state"
        self.state_root = self.state_parent / "fixture-skill"
        self.receipts = fixture.receipts
        self.canonical.mkdir(parents=True)
        self.scan_root.mkdir(parents=True)
        self.guard_root.mkdir(parents=True)
        self.state_parent.mkdir(parents=True)

        host, user = current_os_identity()
        platform_name = "windows" if os.name == "nt" else "linux"
        registry = json.loads(
            (fixture.hub_root / "capability-targets.json").read_text("ascii")
        )
        for row in registry["targets"]:
            if row["tool"] != "antigravity":
                continue
            row["expected_host"] = host
            row["expected_user"] = user
            row["platform"] = platform_name
            row["scan_root_template"] = str(self.scan_root)
            row["state_root_template"] = str(self.state_root)
            for guard in row["guard_paths"]:
                guard_path = self.guard_root / f"{guard['id']}.txt"
                guard_path.write_text(
                    f"{guard['id']} real-mode fixture\n", encoding="ascii"
                )
                guard["template"] = str(guard_path)

        now = self._now()
        matrix = json.loads(
            (fixture.hub_root / "capability-matrix-v1.2.json").read_text("ascii")
        )
        matrix["capability_id"] = "fixture-skill"
        matrix["verified_at"] = now
        for row in matrix["targets"]:
            if row["tool"] != "antigravity":
                continue
            row["approval_posture"] = POSTURE
            row["backup_like_count"] = 0
            row["blocker"] = None
            row["capability_target_state"] = "absent"
            row["collision"] = "none"
            row["host"] = host
            row["observed_at"] = now
            row["rollout_status"] = "eligible"
            row["scan_root"] = str(self.scan_root)
            row["scan_root_inventory_sha256"] = scan_root_inventory(
                self.scan_root
            )

        revocations = {
            "entries": [],
            "schema_version": 1,
            "updated_at": now,
        }
        for relative in ENGINE_PATHS:
            destination = self.canonical / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(fixture.hub_root / relative, destination)
        shutil.copytree(
            fixture.repo / "capabilities" / "fixture-skill",
            self.canonical / "capabilities" / "fixture-skill",
        )
        (self.canonical / "capability-targets.json").write_bytes(
            canonical_json_bytes(registry)
        )
        (self.canonical / "capability-matrix-v1.2.json").write_bytes(
            canonical_json_bytes(matrix)
        )
        (self.canonical / "capability-revocations.json").write_bytes(
            canonical_json_bytes(revocations)
        )

        git(self.canonical, "init", "-q", "-b", "main")
        git(self.canonical, "config", "user.name", "Real Mode Test")
        git(
            self.canonical,
            "config",
            "user.email",
            "real-mode@example.invalid",
        )
        git(self.canonical, "config", "core.autocrlf", "false")
        git(self.canonical, "add", "--", ".")
        git(self.canonical, "commit", "-q", "-m", "real mode fixture")
        self.release_commit = git(self.canonical, "rev-parse", "HEAD")
        self.policy_commit = self.release_commit
        self.origin = self.root / "origin.git"
        self.origin.mkdir()
        git(self.origin, "init", "--bare", "-q")
        git(self.canonical, "remote", "add", "origin", str(self.origin))
        self._publish_policy()
        self.target = resolve_target(
            load_registry(self.canonical / "capability-targets.json"),
            "antigravity",
            "fixture-skill",
        )

    @staticmethod
    def _now() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )

    def _publish_policy(self) -> None:
        git(self.canonical, "push", "-q", "-u", "origin", "main")
        git(
            self.canonical,
            "update-ref",
            "-d",
            "refs/remotes/origin/main",
        )
        git(
            self.canonical,
            "fetch",
            "-q",
            "origin",
            "main:refs/remotes/origin/main",
        )

    def fetch_main_with_stale_unchanged_tracking_reflog(self) -> str:
        origin_oid = git(
            self.canonical, "rev-parse", "refs/remotes/origin/main"
        )
        git(self.canonical, "update-ref", "-d", "refs/remotes/origin/main")
        environment = os.environ.copy()
        environment["GIT_COMMITTER_DATE"] = "2020-01-01T00:00:00+00:00"
        subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={self.canonical}",
                "-C",
                str(self.canonical),
                "update-ref",
                "--create-reflog",
                "-m",
                "stale test fetch",
                "refs/remotes/origin/main",
                origin_oid,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        git(
            self.canonical,
            "fetch",
            "-q",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        return origin_oid

    def common(self, policy_commit: str | None = None) -> list[str]:
        return [
            "--repo",
            str(self.canonical),
            "--commit",
            self.release_commit,
            "--policy-commit",
            policy_commit or self.policy_commit,
            "--capability-id",
            "fixture-skill",
            "--tool",
            "antigravity",
        ]

    def command(
        self,
        action: str,
        *extra: str,
        policy_commit: str | None = None,
        check: bool = True,
        python_code: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if python_code is None:
            prefix = [
                sys.executable,
                "-B",
                str(self.canonical / "scripts/capability_rollout.py"),
            ]
        else:
            prefix = [sys.executable, "-B", "-c", python_code]
        return subprocess.run(
            [*prefix, action, *self.common(policy_commit), *extra],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self.canonical,
        )

    def run(
        self,
        action: str,
        *extra: str,
        policy_commit: str | None = None,
        check: bool = True,
    ) -> dict[str, object]:
        completed = self.command(
            action, *extra, policy_commit=policy_commit, check=check
        )
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        return json.loads(stream)

    def snapshot(self) -> dict[str, dict[str, tuple[str, int, int, bytes]]]:
        return {
            "canonical": file_state(self.canonical),
            "profile": file_state(self.profile_root),
            "state_parent": file_state(self.state_parent),
        }

    def commit_policy_change(self, message: str) -> str:
        now = self._now()
        matrix_path = self.canonical / "capability-matrix-v1.2.json"
        matrix = json.loads(matrix_path.read_text("ascii"))
        matrix["verified_at"] = now
        for row in matrix["targets"]:
            if row["tool"] != "antigravity":
                continue
            row["capability_target_state"] = (
                "managed-current" if self.target.target.exists() else "absent"
            )
            row["observed_at"] = now
            row["scan_root_inventory_sha256"] = scan_root_inventory(
                self.scan_root
            )
        matrix_path.write_bytes(canonical_json_bytes(matrix))
        git(self.canonical, "add", "--", "capability-matrix-v1.2.json")
        git(self.canonical, "commit", "-q", "-m", message)
        self.policy_commit = git(self.canonical, "rev-parse", "HEAD")
        self._publish_policy()
        return self.policy_commit

    def discovery_evidence(
        self, deployed: dict[str, object]
    ) -> tuple[dict[str, object], Path]:
        return self.fixture.discovery_evidence(
            deployed,
            target=self.target,
            output_root=self.receipts,
        )

    def crash_after_target_move(
        self,
        action: str,
        *extra: str,
        match_source: bool,
        policy_commit: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        watched = str(Path(os.path.abspath(self.target.target)))
        killer = "\n".join(
            (
                "import os, sys",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(self.canonical / 'scripts')!r})",
                "import capability_rollout as module",
                "original = module.os.replace",
                f"watched = os.path.normcase(os.path.abspath({watched!r}))",
                f"match_source = {match_source!r}",
                "def replace_then_exit(source, destination):",
                "    result = original(source, destination)",
                "    candidate = source if match_source else destination",
                "    if os.path.normcase(os.path.abspath(os.fspath(candidate))) == watched:",
                "        os._exit(93)",
                "    return result",
                "module.os.replace = replace_then_exit",
                "raise SystemExit(module.main(sys.argv[1:]))",
            )
        )
        return self.command(
            action,
            *extra,
            policy_commit=policy_commit,
            check=False,
            python_code=killer,
        )


class CapabilityRolloutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hub_root = Path(__file__).resolve().parents[1]

    def setUp(self) -> None:
        self.fixture = RolloutFixture(self.hub_root)

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_registry_has_exact_seven_fixed_targets(self) -> None:
        registry = self.fixture.registry
        self.assertEqual(
            set(registry.targets),
            {
                "antigravity",
                "claude",
                "codex",
                "grok",
                "hermes",
                "kimi",
                "kimi-work",
            },
        )
        for tool in registry.targets:
            target = resolve_target(
                registry, tool, "fixture-skill", fixture_root=self.fixture.root
            )
            self.assertEqual(target.target.parent, target.scan_root)
            self.assertFalse(
                str(target.state_root).startswith(str(target.scan_root))
            )

    def test_empirical_matrix_has_only_antigravity_unblocked(self) -> None:
        matrix = load_matrix(
            self.hub_root / "capability-matrix-v1.2.json",
            self.fixture.registry,
        )
        unblocked = {
            tool
            for tool, row in matrix.targets.items()
            if row["rollout_status"] in {"eligible", "pilot-verified"}
        }
        self.assertEqual(unblocked, {"antigravity"})

    def test_plan_is_zero_write_and_reports_install(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        before = file_state(self.fixture.root)
        result = rollout.plan()
        self.assertEqual(file_state(self.fixture.root), before)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(
            result["approval_context"]["operation_kind"], "install"
        )
        self.assertEqual(result["writes_performed"], 0)
        evidence = result["approval_context"]["approval_posture_evidence"]
        self.assertEqual(evidence["matrix_approval_posture"], POSTURE)
        self.assertEqual(
            result["approval_context"]["approval_posture_sha256"],
            __import__("capability_core").canonical_json_hash(evidence),
        )

    def test_versioned_revocation_catalog_blocks_exact_release(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        context = rollout.plan()["approval_context"]
        now = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        value = {
            "entries": [
                {
                    "capability_id": "fixture-skill",
                    "manifest_fingerprint": context["manifest_fingerprint"],
                    "payload_fingerprint": context["payload_fingerprint"],
                    "reason": "fixture revocation",
                    "revoked_at": now,
                    "source_commit": context["source_commit"],
                    "source_tree": context["source_tree"],
                }
            ],
            "schema_version": 1,
            "updated_at": now,
        }
        path = self.fixture.root / "revocations.json"
        path.write_text(
            json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            encoding="ascii",
            newline="\n",
        )
        alternate_commit = self.fixture.commit_unrelated()
        revoked = CapabilityRollout(
            repo=self.fixture.repo,
            commit=alternate_commit,
            capability_id="fixture-skill",
            target=self.fixture.target,
            approval_posture=POSTURE,
            registry_sha256="a" * 64,
            matrix_sha256="b" * 64,
            matrix_status="eligible",
            scan_root_inventory_sha256=scan_root_inventory(
                self.fixture.target.scan_root
            ),
            policy_commit=alternate_commit,
            revocation_catalog=load_revocations(path),
            revocation_checked_at=now,
        )
        with self.assertRaisesRegex(CapabilityError, "revocation catalog"):
            revoked.plan()

    def test_revoked_active_release_can_be_planned_and_rolled_back(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        context = rollout.plan()["approval_context"]
        deploy_receipt = self.fixture.receipt(context)
        rollout.deploy(deploy_receipt, owner_authorized=True)

        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        revocations_path = self.fixture.root / "active-revocations.json"
        revocations_path.write_bytes(
            canonical_json_bytes(
                {
                    "entries": [
                        {
                            "capability_id": "fixture-skill",
                            "manifest_fingerprint": context[
                                "manifest_fingerprint"
                            ],
                            "payload_fingerprint": context[
                                "payload_fingerprint"
                            ],
                            "reason": "active fixture release revoked",
                            "revoked_at": now,
                            "source_commit": context["source_commit"],
                            "source_tree": context["source_tree"],
                        }
                    ],
                    "schema_version": 1,
                    "updated_at": now,
                }
            )
        )
        revoked = CapabilityRollout(
            repo=self.fixture.repo,
            commit=self.fixture.commit_v1,
            capability_id="fixture-skill",
            target=self.fixture.target,
            approval_posture=POSTURE,
            registry_sha256=rollout.registry_sha256,
            matrix_sha256=rollout.matrix_sha256,
            matrix_status=rollout.matrix_status,
            scan_root_inventory_sha256=rollout.scan_root_inventory_sha256,
            policy_commit=rollout.policy_commit,
            revocation_catalog=load_revocations(revocations_path),
            revocation_checked_at=now,
        )
        plan = revoked.plan_rollback(emergency=True)
        self.assertEqual(plan["status"], "READY")
        self.assertTrue(plan["active_release_revoked"])
        self.assertEqual(
            plan["active_release_revocation"]["reason"],
            "active fixture release revoked",
        )
        rollback_receipt = self.fixture.rollback_receipt_from_plan(
            plan,
            emergency=True,
        )
        result = revoked.emergency_rollback(
            rollback_receipt,
            owner_authorized=True,
        )
        self.assertEqual(result["status"], "ROLLED_BACK")
        self.assertFalse(self.fixture.target.target.exists())

    def test_revocation_refresh_invalidates_existing_rollback_receipt(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        deploy_context = rollout.plan()["approval_context"]
        rollout.deploy(
            self.fixture.receipt(deploy_context), owner_authorized=True
        )
        stale_receipt = self.fixture.rollback_receipt(
            rollout, emergency=True
        )
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        path = self.fixture.root / "new-active-revocations.json"
        path.write_bytes(
            canonical_json_bytes(
                {
                    "entries": [
                        {
                            "capability_id": "fixture-skill",
                            "manifest_fingerprint": deploy_context[
                                "manifest_fingerprint"
                            ],
                            "payload_fingerprint": deploy_context[
                                "payload_fingerprint"
                            ],
                            "reason": "new revocation after rollback approval",
                            "revoked_at": now,
                            "source_commit": deploy_context["source_commit"],
                            "source_tree": deploy_context["source_tree"],
                        }
                    ],
                    "schema_version": 1,
                    "updated_at": now,
                }
            )
        )
        refreshed = CapabilityRollout(
            repo=self.fixture.repo,
            commit=self.fixture.commit_v1,
            capability_id="fixture-skill",
            target=self.fixture.target,
            approval_posture=POSTURE,
            registry_sha256=rollout.registry_sha256,
            matrix_sha256=rollout.matrix_sha256,
            matrix_status=rollout.matrix_status,
            scan_root_inventory_sha256=rollout.scan_root_inventory_sha256,
            policy_commit=rollout.policy_commit,
            revocation_catalog=load_revocations(path),
            revocation_checked_at=now,
        )
        active_before = snapshot_target(self.fixture.target)
        with self.assertRaisesRegex(CapabilityError, "context mismatch"):
            refreshed.emergency_rollback(
                stale_receipt, owner_authorized=True
            )
        self.assertEqual(snapshot_target(self.fixture.target), active_before)
        self.assertFalse(self.fixture.target.lock_path.exists())

    def test_revoked_prior_release_requires_owner_gated_remove_only(self) -> None:
        first = self.fixture.rollout(self.fixture.commit_v1)
        first_context = first.plan()["approval_context"]
        first.deploy(
            self.fixture.receipt(first_context), owner_authorized=True
        )
        commit_v2 = self.fixture.commit_v2()
        update = self.fixture.rollout(commit_v2)
        update_context = update.plan()["approval_context"]
        deployed = update.deploy(
            self.fixture.receipt(update_context),
            owner_authorized=True,
        )
        active_before = snapshot_target(self.fixture.target)

        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        path = self.fixture.root / "prior-revocations.json"
        path.write_bytes(
            canonical_json_bytes(
                {
                    "entries": [
                        {
                            "capability_id": "fixture-skill",
                            "manifest_fingerprint": update_context[
                                "manifest_fingerprint"
                            ],
                            "payload_fingerprint": update_context[
                                "payload_fingerprint"
                            ],
                            "reason": "active fixture release revoked too",
                            "revoked_at": now,
                            "source_commit": update_context["source_commit"],
                            "source_tree": update_context["source_tree"],
                        },
                        {
                            "capability_id": "fixture-skill",
                            "manifest_fingerprint": first_context[
                                "manifest_fingerprint"
                            ],
                            "payload_fingerprint": first_context[
                                "payload_fingerprint"
                            ],
                            "reason": "prior fixture release revoked",
                            "revoked_at": now,
                            "source_commit": first_context["source_commit"],
                            "source_tree": first_context["source_tree"],
                        }
                    ],
                    "schema_version": 1,
                    "updated_at": now,
                }
            )
        )
        current = CapabilityRollout(
            repo=self.fixture.repo,
            commit=commit_v2,
            capability_id="fixture-skill",
            target=self.fixture.target,
            approval_posture=POSTURE,
            registry_sha256=update.registry_sha256,
            matrix_sha256="c" * 64,
            matrix_status="blocked",
            scan_root_inventory_sha256=scan_root_inventory(
                self.fixture.target.scan_root
            ),
            policy_commit=commit_v2,
            revocation_catalog=load_revocations(path),
            revocation_checked_at=now,
            allow_blocked_removal=True,
        )
        plan = current.plan_rollback(emergency=True)
        self.assertEqual(plan["status"], "READY")
        self.assertTrue(plan["active_release_revoked"])
        self.assertTrue(plan["prior_release_revoked"])
        self.assertTrue(plan["remove_without_restore"])
        self.assertEqual(
            plan["prior_release_revocation"]["reason"],
            "prior fixture release revoked",
        )
        self.assertIn(
            "REMOVE ACTIVE WITHOUT RESTORE",
            plan["required_remove_without_restore_fragment"],
        )

        incomplete_receipt = self.fixture.receipt(
            plan["approval_context"], actions=list(plan["required_actions"])
        )
        incomplete = json.loads(incomplete_receipt.read_text("ascii"))
        incomplete["owner_decision"] = (
            "GO V1.2: fixture-only transaction; "
            f"{plan['required_owner_decision_fragment']}; "
            "EMERGENCY ROLLBACK WITHOUT DISCOVERY"
        )
        incomplete_receipt.write_bytes(canonical_json_bytes(incomplete))
        with self.assertRaisesRegex(
            CapabilityError, "REMOVE ACTIVE WITHOUT RESTORE"
        ):
            current.emergency_rollback(
                incomplete_receipt, owner_authorized=True
            )
        self.assertEqual(snapshot_target(self.fixture.target), active_before)
        self.assertFalse(self.fixture.target.lock_path.exists())

        receipt = self.fixture.rollback_receipt_from_plan(
            plan, emergency=True
        )
        result = current.emergency_rollback(
            receipt, owner_authorized=True
        )
        self.assertEqual(result["status"], "REMOVED_WITHOUT_RESTORE")
        self.assertFalse(result["prior_state_restored"])
        self.assertTrue(result["backup_retained"])
        self.assertFalse(result["revoked_prior_quarantine_retained"])
        self.assertFalse(self.fixture.target.target.exists())
        self.assertFalse(self.fixture.target.active_path.exists())
        backup = (
            self.fixture.target.operations_root
            / str(deployed["transaction_id"])
            / "prior-target"
        )
        self.assertTrue(backup.is_dir())
        replayed = current.emergency_rollback(
            receipt, owner_authorized=True
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["status"], "REMOVED_WITHOUT_RESTORE")

    def test_recovery_never_restores_prior_revoked_after_rollback_started(
        self,
    ) -> None:
        first = self.fixture.rollout(self.fixture.commit_v1)
        first_context = first.plan()["approval_context"]
        first.deploy(
            self.fixture.receipt(first_context), owner_authorized=True
        )
        commit_v2 = self.fixture.commit_v2()
        update = self.fixture.rollout(commit_v2)
        deployed = update.deploy(
            self.fixture.receipt(update.plan()["approval_context"]),
            owner_authorized=True,
        )
        rollback_receipt = self.fixture.rollback_receipt(
            update, emergency=True
        )
        with self.assertRaises(InjectedCrash):
            update.emergency_rollback(
                rollback_receipt,
                owner_authorized=True,
                crash_after="rollback_prepared",
            )

        now = dt.datetime.now(dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        path = self.fixture.root / "prior-revoked-during-recovery.json"
        path.write_bytes(
            canonical_json_bytes(
                {
                    "entries": [
                        {
                            "capability_id": "fixture-skill",
                            "manifest_fingerprint": first_context[
                                "manifest_fingerprint"
                            ],
                            "payload_fingerprint": first_context[
                                "payload_fingerprint"
                            ],
                            "reason": "prior revoked after rollback started",
                            "revoked_at": now,
                            "source_commit": first_context["source_commit"],
                            "source_tree": first_context["source_tree"],
                        }
                    ],
                    "schema_version": 1,
                    "updated_at": now,
                }
            )
        )
        current = CapabilityRollout(
            repo=self.fixture.repo,
            commit=commit_v2,
            capability_id="fixture-skill",
            target=self.fixture.target,
            approval_posture="current recovery posture",
            registry_sha256="c" * 64,
            matrix_sha256="d" * 64,
            matrix_status="blocked",
            scan_root_inventory_sha256=scan_root_inventory(
                self.fixture.target.scan_root
            ),
            policy_commit=commit_v2,
            revocation_catalog=load_revocations(path),
            revocation_checked_at=now,
            allow_blocked_removal=True,
        )
        with self.assertRaises(InjectedCrash):
            current.recover_rollback(
                rollback_receipt,
                owner_authorized=True,
                crash_after="prior_revocation_recorded",
            )
        self.assertTrue(self.fixture.target.target.is_dir())
        with self.assertRaises(InjectedCrash):
            current.recover_rollback(
                rollback_receipt,
                owner_authorized=True,
                crash_after="prior_revoked_not_restored",
            )
        self.assertFalse(self.fixture.target.target.exists())
        recovered = current.recover_rollback(
            rollback_receipt, owner_authorized=True
        )
        self.assertEqual(recovered["status"], "REMOVED_WITHOUT_RESTORE")
        self.assertFalse(recovered["prior_state_restored"])
        self.assertTrue(recovered["backup_retained"])
        self.assertFalse(self.fixture.target.target.exists())
        self.assertFalse(self.fixture.target.active_path.exists())
        backup = (
            self.fixture.target.operations_root
            / str(deployed["transaction_id"])
            / "prior-target"
        )
        self.assertTrue(backup.is_dir())
        replayed = current.recover_rollback(
            rollback_receipt, owner_authorized=True
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["status"], "REMOVED_WITHOUT_RESTORE")

    def test_rollback_recovery_removes_revoked_prior_from_every_checkpoint(
        self,
    ) -> None:
        checkpoints = (
            "rollback_prepared",
            "rollback_target_renamed",
            "current_quarantined",
            "prior_restored",
            "active_restored",
            "rolled_back",
        )
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                fixture = RolloutFixture(self.hub_root)
                try:
                    first = fixture.rollout(fixture.commit_v1)
                    first_context = first.plan()["approval_context"]
                    first.deploy(
                        fixture.receipt(first_context),
                        owner_authorized=True,
                    )
                    commit_v2 = fixture.commit_v2()
                    update = fixture.rollout(commit_v2)
                    update_context = update.plan()["approval_context"]
                    deployed = update.deploy(
                        fixture.receipt(update_context),
                        owner_authorized=True,
                    )
                    rollback_receipt = fixture.rollback_receipt(
                        update, emergency=True
                    )
                    with self.assertRaises(InjectedCrash):
                        update.emergency_rollback(
                            rollback_receipt,
                            owner_authorized=True,
                            crash_after=checkpoint,
                        )
                    lock = json.loads(
                        fixture.target.lock_path.read_text("ascii")
                    )
                    rollback_id = str(lock["transaction_id"])

                    now = dt.datetime.now(dt.timezone.utc).isoformat().replace(
                        "+00:00", "Z"
                    )
                    path = fixture.root / f"revoked-{checkpoint}.json"
                    entries = []
                    for context, reason in (
                        (first_context, "prior revoked after rollback crash"),
                        (update_context, "active revoked after rollback crash"),
                    ):
                        entries.append(
                            {
                                "capability_id": "fixture-skill",
                                "manifest_fingerprint": context[
                                    "manifest_fingerprint"
                                ],
                                "payload_fingerprint": context[
                                    "payload_fingerprint"
                                ],
                                "reason": reason,
                                "revoked_at": now,
                                "source_commit": context["source_commit"],
                                "source_tree": context["source_tree"],
                            }
                        )
                    path.write_bytes(
                        canonical_json_bytes(
                            {
                                "entries": entries,
                                "schema_version": 1,
                                "updated_at": now,
                            }
                        )
                    )
                    current = CapabilityRollout(
                        repo=fixture.repo,
                        commit=commit_v2,
                        capability_id="fixture-skill",
                        target=fixture.target,
                        approval_posture="current recovery posture",
                        registry_sha256="c" * 64,
                        matrix_sha256="d" * 64,
                        matrix_status="blocked",
                        scan_root_inventory_sha256=scan_root_inventory(
                            fixture.target.scan_root
                        ),
                        policy_commit=commit_v2,
                        revocation_catalog=load_revocations(path),
                        revocation_checked_at=now,
                        allow_blocked_removal=True,
                    )
                    recovered = current.recover_rollback(
                        rollback_receipt, owner_authorized=True
                    )
                    self.assertEqual(
                        recovered["status"], "REMOVED_WITHOUT_RESTORE"
                    )
                    self.assertFalse(fixture.target.target.exists())
                    self.assertFalse(fixture.target.active_path.exists())
                    current_quarantine = fixture.target.quarantine_root / (
                        f"rollback-{rollback_id}-current-target"
                    )
                    self.assertTrue(current_quarantine.is_dir())
                    backup = (
                        fixture.target.operations_root
                        / str(deployed["transaction_id"])
                        / "prior-target"
                    )
                    revoked_quarantine = fixture.target.quarantine_root / (
                        f"rollback-{rollback_id}-revoked-prior"
                    )
                    restored_before_recovery = checkpoint in {
                        "prior_restored",
                        "active_restored",
                        "rolled_back",
                    }
                    self.assertEqual(backup.is_dir(), not restored_before_recovery)
                    self.assertEqual(
                        revoked_quarantine.is_dir(), restored_before_recovery
                    )
                    self.assertEqual(
                        recovered["backup_retained"], not restored_before_recovery
                    )
                    self.assertEqual(
                        recovered["revoked_prior_quarantine_retained"],
                        restored_before_recovery,
                    )
                    replayed = current.recover_rollback(
                        rollback_receipt, owner_authorized=True
                    )
                    self.assertTrue(replayed["replayed"])
                    self.assertEqual(
                        replayed["status"], "REMOVED_WITHOUT_RESTORE"
                    )
                finally:
                    fixture.cleanup()

    def test_deploy_recovery_never_restores_newly_revoked_prior(self) -> None:
        first = self.fixture.rollout(self.fixture.commit_v1)
        first_context = first.plan()["approval_context"]
        first.deploy(
            self.fixture.receipt(first_context), owner_authorized=True
        )
        commit_v2 = self.fixture.commit_v2()
        update = self.fixture.rollout(commit_v2)
        update_receipt = self.fixture.receipt(
            update.plan()["approval_context"]
        )
        with self.assertRaises(InjectedCrash):
            update.deploy(
                update_receipt,
                owner_authorized=True,
                crash_after="target_activated",
            )

        now = dt.datetime.now(dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        path = self.fixture.root / "deploy-recovery-prior-revoked.json"
        path.write_bytes(
            canonical_json_bytes(
                {
                    "entries": [
                        {
                            "capability_id": "fixture-skill",
                            "manifest_fingerprint": first_context[
                                "manifest_fingerprint"
                            ],
                            "payload_fingerprint": first_context[
                                "payload_fingerprint"
                            ],
                            "reason": "prior revoked during deploy recovery",
                            "revoked_at": now,
                            "source_commit": first_context["source_commit"],
                            "source_tree": first_context["source_tree"],
                        }
                    ],
                    "schema_version": 1,
                    "updated_at": now,
                }
            )
        )
        current = CapabilityRollout(
            repo=self.fixture.repo,
            commit=commit_v2,
            capability_id="fixture-skill",
            target=self.fixture.target,
            approval_posture="current recovery posture",
            registry_sha256="c" * 64,
            matrix_sha256="d" * 64,
            matrix_status="blocked",
            scan_root_inventory_sha256=scan_root_inventory(
                self.fixture.target.scan_root
            ),
            policy_commit=commit_v2,
            revocation_catalog=load_revocations(path),
            revocation_checked_at=now,
            allow_blocked_removal=True,
        )
        with self.assertRaises(InjectedCrash):
            current.recover_deploy(
                update_receipt,
                owner_authorized=True,
                crash_after="prior_revocation_recorded",
            )
        with self.assertRaises(InjectedCrash):
            current.recover_deploy(
                update_receipt,
                owner_authorized=True,
                crash_after="prior_revoked_not_restored",
            )
        recovered = current.recover_deploy(
            update_receipt, owner_authorized=True
        )
        self.assertEqual(recovered["status"], "REMOVED_WITHOUT_RESTORE")
        self.assertFalse(recovered["prior_state_restored"])
        self.assertTrue(recovered["prior_backup_retained"])
        self.assertTrue(recovered["new_quarantine_retained"])
        self.assertFalse(self.fixture.target.target.exists())
        self.assertFalse(self.fixture.target.active_path.exists())
        replayed = current.recover_deploy(
            update_receipt, owner_authorized=True
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["status"], "REMOVED_WITHOUT_RESTORE")

    def test_deploy_recovery_does_not_commit_newly_revoked_release(self) -> None:
        first = self.fixture.rollout(self.fixture.commit_v1)
        first.deploy(
            self.fixture.receipt(first.plan()["approval_context"]),
            owner_authorized=True,
        )
        commit_v2 = self.fixture.commit_v2()
        update = self.fixture.rollout(commit_v2)
        update_context = update.plan()["approval_context"]
        update_receipt = self.fixture.receipt(update_context)
        with self.assertRaises(InjectedCrash):
            update.deploy(
                update_receipt,
                owner_authorized=True,
                crash_after="committed",
            )

        now = dt.datetime.now(dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        path = self.fixture.root / "deploy-recovery-current-revoked.json"
        path.write_bytes(
            canonical_json_bytes(
                {
                    "entries": [
                        {
                            "capability_id": "fixture-skill",
                            "manifest_fingerprint": update_context[
                                "manifest_fingerprint"
                            ],
                            "payload_fingerprint": update_context[
                                "payload_fingerprint"
                            ],
                            "reason": "current release revoked before recovery",
                            "revoked_at": now,
                            "source_commit": update_context["source_commit"],
                            "source_tree": update_context["source_tree"],
                        }
                    ],
                    "schema_version": 1,
                    "updated_at": now,
                }
            )
        )
        current = CapabilityRollout(
            repo=self.fixture.repo,
            commit=commit_v2,
            capability_id="fixture-skill",
            target=self.fixture.target,
            approval_posture="current recovery posture",
            registry_sha256="c" * 64,
            matrix_sha256="d" * 64,
            matrix_status="blocked",
            scan_root_inventory_sha256=scan_root_inventory(
                self.fixture.target.scan_root
            ),
            policy_commit=commit_v2,
            revocation_catalog=load_revocations(path),
            revocation_checked_at=now,
            allow_blocked_removal=True,
        )
        recovered = current.recover_deploy(
            update_receipt, owner_authorized=True
        )
        self.assertEqual(recovered["status"], "ROLLED_BACK")
        marker = read_marker(
            self.fixture.target.target / ".shared-capability-managed.json"
        )
        self.assertEqual(marker.data["capability_version"], "0.1.0")
        replayed = current.recover_deploy(
            update_receipt, owner_authorized=True
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["status"], "ROLLED_BACK")

    def test_recovery_rechecks_live_rollback_preconditions_before_mutation(
        self,
    ) -> None:
        cases = {
            "discovery": "discovery-drift",
            "guard": "guard-drift",
            "inventory": "scan-root-inventory-drift",
        }
        for drift, expected_reason in cases.items():
            with self.subTest(drift=drift):
                fixture = RolloutFixture(self.hub_root)
                try:
                    rollout = fixture.rollout(fixture.commit_v1)
                    deployed = rollout.deploy(
                        fixture.receipt(rollout.plan()["approval_context"]),
                        owner_authorized=True,
                    )
                    emergency = drift != "discovery"
                    if not emergency:
                        fixture.record_discovery(rollout, deployed)
                    rollback_receipt = fixture.rollback_receipt(
                        rollout, emergency=emergency
                    )
                    rollback = (
                        rollout.emergency_rollback
                        if emergency
                        else rollout.rollback
                    )
                    with self.assertRaises(InjectedCrash):
                        rollback(
                            rollback_receipt,
                            owner_authorized=True,
                            crash_after="rollback_prepared",
                        )
                    if drift == "guard":
                        guard = next(iter(fixture.target.guards.values()))
                        guard.write_bytes(guard.read_bytes() + b"drift\n")
                    elif drift == "inventory":
                        unrelated = fixture.target.scan_root / "unrelated-drift"
                        unrelated.mkdir()
                        (unrelated / "SKILL.md").write_text(
                            "unrelated\n", encoding="ascii"
                        )
                    else:
                        deploy_operation = (
                            fixture.target.operations_root
                            / str(deployed["transaction_id"])
                        )
                        (deploy_operation / "discovery-record.json").unlink()
                    with self.assertRaises(InjectedCrash):
                        rollout.recover_rollback(
                            rollback_receipt,
                            owner_authorized=True,
                            crash_after="recovery_aborted_precondition_drift",
                        )
                    recovered = rollout.recover_rollback(
                        rollback_receipt, owner_authorized=True
                    )
                    self.assertEqual(
                        recovered["status"], "ABORTED_PRECONDITION_DRIFT"
                    )
                    self.assertEqual(recovered["reason"], expected_reason)
                    self.assertTrue(fixture.target.target.is_dir())
                    self.assertFalse(fixture.target.lock_path.exists())
                    replayed = rollout.recover_rollback(
                        rollback_receipt, owner_authorized=True
                    )
                    self.assertTrue(replayed["replayed"])
                    self.assertEqual(replayed["reason"], expected_reason)
                finally:
                    fixture.cleanup()

    def test_policy_refresh_and_blocked_matrix_still_allow_removal(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        rollout.deploy(
            self.fixture.receipt(rollout.plan()["approval_context"]),
            owner_authorized=True,
        )
        policy_commit = self.fixture.commit_unrelated()
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        revocations_path = self.fixture.root / "refreshed-revocations.json"
        revocations_path.write_bytes(
            canonical_json_bytes(
                {"entries": [], "schema_version": 1, "updated_at": now}
            )
        )
        refreshed = CapabilityRollout(
            repo=self.fixture.repo,
            commit=self.fixture.commit_v1,
            capability_id="fixture-skill",
            target=self.fixture.target,
            approval_posture="refreshed rollback-only posture",
            registry_sha256="c" * 64,
            matrix_sha256="d" * 64,
            matrix_status="blocked",
            scan_root_inventory_sha256="e" * 64,
            policy_commit=policy_commit,
            revocation_catalog=load_revocations(revocations_path),
            revocation_checked_at=now,
            allow_blocked_removal=True,
        )
        before = file_state(self.fixture.root)
        plan = refreshed.plan_rollback(emergency=True)
        self.assertEqual(file_state(self.fixture.root), before)
        self.assertEqual(plan["status"], "READY")
        self.assertEqual(
            plan["approval_context"]["deploy_context"]["matrix_sha256"],
            rollout.matrix_sha256,
        )
        self.assertEqual(
            plan["approval_context"]["matrix_sha256"], "d" * 64
        )
        receipt = self.fixture.rollback_receipt_from_plan(
            plan, emergency=True
        )
        result = refreshed.emergency_rollback(
            receipt, owner_authorized=True
        )
        self.assertEqual(result["status"], "ROLLED_BACK")
        self.assertFalse(self.fixture.target.target.exists())

    def test_first_install_and_rollback_restore_exact_absence(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        context = rollout.plan()["approval_context"]
        deploy_receipt = self.fixture.receipt(context)
        deployed = rollout.deploy(deploy_receipt, owner_authorized=True)
        self.assertEqual(deployed["status"], "COMMITTED")
        self.assertTrue(self.fixture.target.target.is_dir())
        marker = read_marker(
            self.fixture.target.target / ".shared-capability-managed.json"
        )
        self.assertEqual(marker.data["capability_version"], "0.1.0")
        with self.assertRaisesRegex(CapabilityError, "requires recorded"):
            rollout.rollback(deploy_receipt, owner_authorized=True)
        self.fixture.record_discovery(rollout, deployed)
        rollback_receipt = self.fixture.rollback_receipt(
            rollout, emergency=False
        )
        rolled = rollout.rollback(rollback_receipt, owner_authorized=True)
        self.assertEqual(rolled["status"], "ROLLED_BACK")
        self.assertFalse(self.fixture.target.target.exists())
        self.assertFalse(self.fixture.target.active_path.exists())
        self.assertTrue(Path(rolled["quarantine"]).is_dir())

    def test_rollback_requires_new_narrow_post_activation_approval(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        context = rollout.plan()["approval_context"]
        broad = self.fixture.receipt(
            context,
            approval_id="fixture-broad-deploy-approval",
            actions=[
                "deploy",
                "recover-deploy",
                "recover-rollback",
                "rollback",
            ],
        )
        deployed = rollout.deploy(broad, owner_authorized=True)
        self.fixture.record_discovery(rollout, deployed)
        with self.assertRaisesRegex(CapabilityError, "context mismatch"):
            rollout.rollback(broad, owner_authorized=True)

        broad_rollback = self.fixture.rollback_receipt_from_plan(
            rollout.plan_rollback(),
            emergency=False,
            actions=[
                "deploy",
                "recover-deploy",
                "recover-rollback",
                "rollback",
            ],
        )
        with self.assertRaisesRegex(CapabilityError, "authorize only"):
            rollout.rollback(broad_rollback, owner_authorized=True)

        same_id = self.fixture.rollback_receipt(
            rollout,
            emergency=False,
            approval_id="fixture-broad-deploy-approval",
        )
        with self.assertRaisesRegex(CapabilityError, "new post-activation"):
            rollout.rollback(same_id, owner_authorized=True)

        stale = self.fixture.rollback_receipt(rollout, emergency=False)
        value = json.loads(stale.read_text("ascii"))
        operation = self.fixture.target.operations_root / deployed["transaction_id"]
        journal = json.loads((operation / "journal.json").read_text("ascii"))
        activated = dt.datetime.fromisoformat(
            str(journal["activated_at"]).replace("Z", "+00:00")
        )
        approved = activated - dt.timedelta(seconds=1)
        value["approved_at"] = approved.isoformat().replace("+00:00", "Z")
        value["expires_at"] = (approved + dt.timedelta(hours=1)).isoformat().replace(
            "+00:00", "Z"
        )
        stale.write_bytes(canonical_json_bytes(value))
        with self.assertRaisesRegex(CapabilityError, "predates target activation"):
            rollout.rollback(stale, owner_authorized=True)

    def test_rollback_requires_exact_transaction_and_emergency_literals(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        rollout.deploy(
            self.fixture.receipt(rollout.plan()["approval_context"]),
            owner_authorized=True,
        )
        plan = rollout.plan_rollback(emergency=True)

        wrong_transaction = self.fixture.rollback_receipt_from_plan(
            plan, emergency=True
        )
        value = json.loads(wrong_transaction.read_text("ascii"))
        value["owner_decision"] = str(value["owner_decision"]).replace(
            str(plan["required_owner_decision_fragment"]),
            f"ROLLBACK DEPLOY {'0' * 32} AFTER ACTIVATION",
        )
        wrong_transaction.write_bytes(canonical_json_bytes(value))
        with self.assertRaisesRegex(
            CapabilityError, "Rollback owner decision must contain"
        ):
            rollout.emergency_rollback(
                wrong_transaction, owner_authorized=True
            )

        missing_emergency = self.fixture.rollback_receipt_from_plan(
            plan, emergency=True
        )
        value = json.loads(missing_emergency.read_text("ascii"))
        value["owner_decision"] = str(value["owner_decision"]).replace(
            "; EMERGENCY ROLLBACK WITHOUT DISCOVERY", ""
        )
        missing_emergency.write_bytes(canonical_json_bytes(value))
        with self.assertRaisesRegex(
            CapabilityError, "EMERGENCY ROLLBACK WITHOUT DISCOVERY"
        ):
            rollout.emergency_rollback(
                missing_emergency, owner_authorized=True
            )
        self.assertTrue(self.fixture.target.target.is_dir())
        self.assertFalse(self.fixture.target.lock_path.exists())

    def test_verify_is_zero_write_and_binds_active_journal(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        deployed = rollout.deploy(receipt, owner_authorized=True)
        before = file_state(self.fixture.root)
        result = rollout.verify()
        self.assertEqual(file_state(self.fixture.root), before)
        self.assertEqual(result["status"], "CURRENT")
        self.assertEqual(
            result["deploy_transaction_id"], deployed["transaction_id"]
        )
        self.assertTrue(result["source_matches_installed"])
        self.assertEqual(result["writes_performed"], 0)
        self.assertEqual(result["discovery"]["status"], "REQUIRED")
        guard = next(iter(self.fixture.target.guards.values()))
        original_guard = guard.read_bytes()
        guard.write_bytes(b"drift\n")
        with self.assertRaisesRegex(CapabilityError, "guard files drifted"):
            rollout.verify()
        guard.write_bytes(original_guard)

    def test_fresh_session_discovery_is_immutable_and_verified(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        context = rollout.plan()["approval_context"]
        receipt = self.fixture.receipt(context)
        deployed = rollout.deploy(receipt, owner_authorized=True)
        evidence, evidence_path = self.fixture.discovery_evidence(deployed)
        before = file_state(self.fixture.root)
        discovery_plan = rollout.plan_discovery(evidence_path)
        self.assertEqual(file_state(self.fixture.root), before)
        receipt = self.fixture.receipt(
            discovery_plan["approval_context"],
            actions=["record-discovery"],
        )
        recorded = rollout.record_discovery(
            receipt, evidence_path, owner_authorized=True
        )
        self.assertEqual(recorded["status"], "RECORDED")
        self.assertEqual(recorded["deploy_transaction_id"], deployed["transaction_id"])
        self.assertEqual(rollout.verify()["discovery"]["status"], "RECORDED")
        repeated = rollout.record_discovery(
            receipt, evidence_path, owner_authorized=True
        )
        self.assertEqual(repeated["writes_scope"], "none-already-recorded")
        operation = self.fixture.target.operations_root / deployed["transaction_id"]
        with (operation / "discovery-evidence.json").open("ab") as handle:
            handle.write(b"tamper\n")
        with self.assertRaises(CapabilityError):
            rollout.verify()

    def test_discovery_refuses_fake_preimages_and_pre_activation_time(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        deployed = rollout.deploy(receipt, owner_authorized=True)
        evidence, evidence_path = self.fixture.discovery_evidence(deployed)
        evidence["observed_output_sha256"] = "d" * 64
        evidence_path.write_bytes(canonical_json_bytes(evidence))
        with self.assertRaisesRegex(CapabilityError, "hash does not match preimage"):
            rollout.plan_discovery(evidence_path)
        operation = (
            self.fixture.target.operations_root / str(deployed["transaction_id"])
        )
        journal = json.loads((operation / "journal.json").read_text("ascii"))
        evidence, evidence_path = self.fixture.discovery_evidence(deployed)
        activated = dt.datetime.fromisoformat(
            str(journal["activated_at"]).replace("Z", "+00:00")
        )
        evidence["observed_at"] = (
            activated - dt.timedelta(seconds=1)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        evidence_path.write_bytes(canonical_json_bytes(evidence))
        with self.assertRaisesRegex(CapabilityError, "predates target activation"):
            rollout.plan_discovery(evidence_path)

    def test_partial_discovery_blocks_standard_but_not_emergency_plan(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        deployed = rollout.deploy(
            self.fixture.receipt(rollout.plan()["approval_context"]),
            owner_authorized=True,
        )
        evidence, _ = self.fixture.discovery_evidence(deployed)
        operation = (
            self.fixture.target.operations_root / str(deployed["transaction_id"])
        )
        (operation / "discovery-evidence.json").write_bytes(
            canonical_json_bytes(evidence)
        )
        before = file_state(self.fixture.root)
        with self.assertRaisesRegex(CapabilityError, "record is incomplete"):
            rollout.plan_rollback()
        self.assertEqual(file_state(self.fixture.root), before)
        emergency = rollout.plan_rollback(emergency=True)
        self.assertEqual(file_state(self.fixture.root), before)
        self.assertEqual(emergency["status"], "READY")
        self.assertEqual(
            emergency["discovery"]["status"], "BYPASSED_EMERGENCY"
        )

    def test_update_and_rollback_restore_exact_previous_release(self) -> None:
        first = self.fixture.rollout(self.fixture.commit_v1)
        first_context = first.plan()["approval_context"]
        first_receipt = self.fixture.receipt(first_context)
        first.deploy(first_receipt, owner_authorized=True)
        self.assertNotEqual(
            scan_root_inventory(self.fixture.target.scan_root),
            self.fixture.matrix_inventory_sha256,
        )
        previous = snapshot_target(self.fixture.target)
        commit_v2 = self.fixture.commit_v2()
        self.assertEqual(first.verify()["status"], "CURRENT")
        update = self.fixture.rollout(commit_v2)
        update_context = update.plan()["approval_context"]
        self.assertEqual(update_context["operation_kind"], "update")
        update_receipt = self.fixture.receipt(update_context)
        deployed = update.deploy(update_receipt, owner_authorized=True)
        self.assertEqual(deployed["operation_kind"], "update")
        self.assertEqual(update.verify()["status"], "CURRENT")
        self.assertEqual(
            read_marker(
                self.fixture.target.target / ".shared-capability-managed.json"
            ).data["capability_version"],
            "0.1.1",
        )
        update_rollback_receipt = self.fixture.rollback_receipt(
            update, emergency=True
        )
        rolled = update.emergency_rollback(
            update_rollback_receipt, owner_authorized=True
        )
        self.assertTrue(rolled["target_present"])
        self.assertEqual(snapshot_target(self.fixture.target), previous)
        active = json.loads(self.fixture.target.active_path.read_text("ascii"))
        self.assertEqual(active["source_commit"], self.fixture.commit_v1)
        first_rollback_receipt = self.fixture.rollback_receipt(
            first, emergency=True
        )
        final = first.emergency_rollback(
            first_rollback_receipt, owner_authorized=True
        )
        self.assertEqual(final["status"], "ROLLED_BACK")
        self.assertFalse(self.fixture.target.target.exists())
        self.assertFalse(self.fixture.target.active_path.exists())

    def test_interrupted_deploy_recovers_prior_absence(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        with self.assertRaises(InjectedCrash):
            rollout.deploy(
                receipt,
                owner_authorized=True,
                crash_after="target_activated",
            )
        result = rollout.recover_deploy(receipt, owner_authorized=True)
        self.assertEqual(result["status"], "ROLLED_BACK")
        replay = rollout.recover_deploy(receipt, owner_authorized=True)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "ROLLED_BACK")
        self.assertFalse(self.fixture.target.target.exists())
        self.assertFalse(self.fixture.target.lock_path.exists())
        self.assertTrue(Path(result["quarantine"]).is_dir())

    def test_interrupted_update_recovers_previous_release(self) -> None:
        first = self.fixture.rollout(self.fixture.commit_v1)
        first_receipt = self.fixture.receipt(first.plan()["approval_context"])
        first.deploy(first_receipt, owner_authorized=True)
        previous = snapshot_target(self.fixture.target)
        commit_v2 = self.fixture.commit_v2()
        update = self.fixture.rollout(commit_v2)
        receipt = self.fixture.receipt(update.plan()["approval_context"])
        with self.assertRaises(InjectedCrash):
            update.deploy(
                receipt,
                owner_authorized=True,
                crash_after="target_activated",
            )
        result = update.recover_deploy(receipt, owner_authorized=True)
        self.assertEqual(result["status"], "ROLLED_BACK")
        self.assertEqual(snapshot_target(self.fixture.target), previous)
        self.assertFalse(self.fixture.target.lock_path.exists())

    def test_interrupted_rollback_is_idempotently_recovered(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        rollout.deploy(receipt, owner_authorized=True)
        rollback_receipt = self.fixture.rollback_receipt(
            rollout, emergency=True
        )
        with self.assertRaises(InjectedCrash):
            rollout.emergency_rollback(
                rollback_receipt,
                owner_authorized=True,
                crash_after="rollback_target_renamed",
            )
        result = rollout.recover_rollback(
            rollback_receipt, owner_authorized=True
        )
        self.assertEqual(result["status"], "ROLLED_BACK")
        replay = rollout.recover_rollback(
            rollback_receipt, owner_authorized=True
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "ROLLED_BACK")
        self.assertFalse(self.fixture.target.target.exists())
        self.assertFalse(self.fixture.target.lock_path.exists())

    def test_first_install_recovery_at_every_forward_checkpoint(self) -> None:
        checkpoints = [
            "preparing",
            "approval_recorded",
            "staged",
            "prepared",
            "target_renamed",
            "target_activated",
            "active_recorded",
            "committed",
        ]
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                fixture = RolloutFixture(self.hub_root)
                try:
                    rollout = fixture.rollout(fixture.commit_v1)
                    receipt = fixture.receipt(
                        rollout.plan()["approval_context"]
                    )
                    with self.assertRaises(InjectedCrash):
                        rollout.deploy(
                            receipt,
                            owner_authorized=True,
                            crash_after=checkpoint,
                        )
                    result = rollout.recover_deploy(
                        receipt, owner_authorized=True
                    )
                    if checkpoint == "committed":
                        self.assertEqual(result["status"], "COMMITTED")
                        self.assertTrue(fixture.target.target.is_dir())
                    else:
                        self.assertEqual(result["status"], "ROLLED_BACK")
                        self.assertFalse(fixture.target.target.exists())
                    self.assertFalse(fixture.target.lock_path.exists())
                finally:
                    fixture.cleanup()

    def test_update_recovery_at_every_forward_checkpoint(self) -> None:
        checkpoints = [
            "preparing",
            "approval_recorded",
            "staged",
            "prepared",
            "prior_moved",
            "target_renamed",
            "target_activated",
            "active_recorded",
            "committed",
        ]
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                fixture = RolloutFixture(self.hub_root)
                try:
                    first = fixture.rollout(fixture.commit_v1)
                    first_receipt = fixture.receipt(
                        first.plan()["approval_context"]
                    )
                    first.deploy(first_receipt, owner_authorized=True)
                    prior = snapshot_target(fixture.target)
                    second = fixture.rollout(fixture.commit_v2())
                    receipt = fixture.receipt(second.plan()["approval_context"])
                    with self.assertRaises(InjectedCrash):
                        second.deploy(
                            receipt,
                            owner_authorized=True,
                            crash_after=checkpoint,
                        )
                    result = second.recover_deploy(
                        receipt, owner_authorized=True
                    )
                    if checkpoint == "committed":
                        self.assertEqual(result["status"], "COMMITTED")
                        self.assertEqual(
                            snapshot_target(fixture.target)["source_commit"],
                            second.commit,
                        )
                    else:
                        self.assertEqual(result["status"], "ROLLED_BACK")
                        self.assertEqual(snapshot_target(fixture.target), prior)
                    self.assertFalse(fixture.target.lock_path.exists())
                finally:
                    fixture.cleanup()

    def test_update_recovery_is_idempotent_at_recovery_checkpoints(self) -> None:
        for forward_checkpoint in ("target_activated", "active_recorded"):
            for recovery_checkpoint in (
                "recovery_target_restored",
                "recovered_rollback",
            ):
                with self.subTest(
                    forward=forward_checkpoint,
                    recovery=recovery_checkpoint,
                ):
                    fixture = RolloutFixture(self.hub_root)
                    try:
                        first = fixture.rollout(fixture.commit_v1)
                        first_receipt = fixture.receipt(
                            first.plan()["approval_context"]
                        )
                        first.deploy(first_receipt, owner_authorized=True)
                        prior = snapshot_target(fixture.target)
                        update = fixture.rollout(fixture.commit_v2())
                        receipt = fixture.receipt(
                            update.plan()["approval_context"]
                        )
                        with self.assertRaises(InjectedCrash):
                            update.deploy(
                                receipt,
                                owner_authorized=True,
                                crash_after=forward_checkpoint,
                            )
                        with self.assertRaises(InjectedCrash):
                            update.recover_deploy(
                                receipt,
                                owner_authorized=True,
                                crash_after=recovery_checkpoint,
                            )
                        result = update.recover_deploy(
                            receipt, owner_authorized=True
                        )
                        self.assertEqual(result["status"], "ROLLED_BACK")
                        self.assertEqual(snapshot_target(fixture.target), prior)
                        active = json.loads(
                            fixture.target.active_path.read_text("ascii")
                        )
                        self.assertEqual(
                            active["source_commit"], fixture.commit_v1
                        )
                    finally:
                        fixture.cleanup()

    def test_recovery_persists_live_prior_active_substitute_before_mutation(
        self,
    ) -> None:
        first = self.fixture.rollout(self.fixture.commit_v1)
        first_receipt = self.fixture.receipt(first.plan()["approval_context"])
        first.deploy(first_receipt, owner_authorized=True)
        prior = snapshot_target(self.fixture.target)
        prior_active = self.fixture.target.active_path.read_bytes()

        update = self.fixture.rollout(self.fixture.commit_v2())
        update_receipt = self.fixture.receipt(update.plan()["approval_context"])
        with self.assertRaises(InjectedCrash):
            update.deploy(
                update_receipt,
                owner_authorized=True,
                crash_after="preparing",
            )
        lock = json.loads(self.fixture.target.lock_path.read_text("ascii"))
        operation = self.fixture.target.operations_root / lock["transaction_id"]
        prior_active_path = operation / "prior-active.json"
        self.assertFalse(prior_active_path.exists())

        with self.assertRaises(InjectedCrash):
            update.recover_deploy(
                update_receipt,
                owner_authorized=True,
                crash_after="recovery_prior_active_recorded",
            )
        self.assertEqual(prior_active_path.read_bytes(), prior_active)
        self.assertEqual(snapshot_target(self.fixture.target), prior)

        recovered = update.recover_deploy(
            update_receipt,
            owner_authorized=True,
        )
        self.assertEqual(recovered["status"], "ROLLED_BACK")
        replayed = update.recover_deploy(
            update_receipt,
            owner_authorized=True,
        )
        self.assertEqual(replayed["status"], "ROLLED_BACK")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(snapshot_target(self.fixture.target), prior)
        self.assertEqual(self.fixture.target.active_path.read_bytes(), prior_active)

    def test_rollback_recovery_is_idempotent_at_recovery_checkpoints(self) -> None:
        checkpoints = (
            "recovery_rollback_target_renamed",
            "prior_restored",
            "active_restored",
            "rolled_back",
        )
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                fixture = RolloutFixture(self.hub_root)
                try:
                    rollout = fixture.rollout(fixture.commit_v1)
                    deploy_receipt = fixture.receipt(
                        rollout.plan()["approval_context"]
                    )
                    rollout.deploy(deploy_receipt, owner_authorized=True)
                    rollback_receipt = fixture.rollback_receipt(
                        rollout, emergency=True
                    )
                    with self.assertRaises(InjectedCrash):
                        rollout.emergency_rollback(
                            rollback_receipt,
                            owner_authorized=True,
                            crash_after="rollback_prepared",
                        )
                    with self.assertRaises(InjectedCrash):
                        rollout.recover_rollback(
                            rollback_receipt,
                            owner_authorized=True,
                            crash_after=checkpoint,
                        )
                    result = rollout.recover_rollback(
                        rollback_receipt, owner_authorized=True
                    )
                    self.assertEqual(result["status"], "ROLLED_BACK")
                    self.assertFalse(fixture.target.target.exists())
                    self.assertFalse(fixture.target.active_path.exists())
                    self.assertFalse(fixture.target.lock_path.exists())
                finally:
                    fixture.cleanup()

    def test_rollback_recovery_at_every_checkpoint(self) -> None:
        checkpoints = [
            "rollback_preparing",
            "approval_recorded",
            "rollback_prepared",
            "rollback_target_renamed",
            "current_quarantined",
            "prior_restored",
            "active_restored",
            "rolled_back",
        ]
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                fixture = RolloutFixture(self.hub_root)
                try:
                    rollout = fixture.rollout(fixture.commit_v1)
                    receipt = fixture.receipt(
                        rollout.plan()["approval_context"]
                    )
                    rollout.deploy(receipt, owner_authorized=True)
                    rollback_receipt = fixture.rollback_receipt(
                        rollout, emergency=True
                    )
                    with self.assertRaises(InjectedCrash):
                        rollout.emergency_rollback(
                            rollback_receipt,
                            owner_authorized=True,
                            crash_after=checkpoint,
                        )
                    result = rollout.recover_rollback(
                        rollback_receipt, owner_authorized=True
                    )
                    self.assertEqual(result["status"], "ROLLED_BACK")
                    self.assertFalse(fixture.target.target.exists())
                    self.assertFalse(fixture.target.active_path.exists())
                    self.assertFalse(fixture.target.lock_path.exists())
                finally:
                    fixture.cleanup()

    def test_update_rollback_recovery_at_every_checkpoint(self) -> None:
        checkpoints = [
            "rollback_preparing",
            "approval_recorded",
            "rollback_prepared",
            "rollback_target_renamed",
            "current_quarantined",
            "prior_restored",
            "active_restored",
            "rolled_back",
        ]
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                fixture = RolloutFixture(self.hub_root)
                try:
                    first = fixture.rollout(fixture.commit_v1)
                    first_receipt = fixture.receipt(
                        first.plan()["approval_context"]
                    )
                    first.deploy(first_receipt, owner_authorized=True)
                    prior = snapshot_target(fixture.target)
                    second = fixture.rollout(fixture.commit_v2())
                    receipt = fixture.receipt(second.plan()["approval_context"])
                    second.deploy(receipt, owner_authorized=True)
                    rollback_receipt = fixture.rollback_receipt(
                        second, emergency=True
                    )
                    with self.assertRaises(InjectedCrash):
                        second.emergency_rollback(
                            rollback_receipt,
                            owner_authorized=True,
                            crash_after=checkpoint,
                        )
                    result = second.recover_rollback(
                        rollback_receipt, owner_authorized=True
                    )
                    self.assertEqual(result["status"], "ROLLED_BACK")
                    self.assertEqual(snapshot_target(fixture.target), prior)
                    self.assertFalse(fixture.target.lock_path.exists())
                finally:
                    fixture.cleanup()

    def test_guard_drift_and_backup_sprawl_fail_closed(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        context = rollout.plan()["approval_context"]
        receipt = self.fixture.receipt(context)
        next(iter(self.fixture.target.guards.values())).write_text(
            "changed\n", encoding="ascii"
        )
        with self.assertRaisesRegex(CapabilityError, "context mismatch"):
            rollout.deploy(receipt, owner_authorized=True)
        next(iter(self.fixture.target.guards.values())).write_text(
            "global-rules\n", encoding="ascii"
        )
        (self.fixture.target.scan_root / "old.backup-test").mkdir()
        with self.assertRaisesRegex(CapabilityError, "Backup-like"):
            rollout.plan()

    def test_unmanaged_existing_target_and_approval_reuse_are_refused(self) -> None:
        unmanaged = self.fixture.target.target
        unmanaged.mkdir()
        (unmanaged / "SKILL.md").write_text("unmanaged\n", encoding="ascii")
        with self.assertRaisesRegex(CapabilityError, "unmanaged"):
            self.fixture.rollout(self.fixture.commit_v1).plan()
        shutil.rmtree(unmanaged)
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        context = rollout.plan()["approval_context"]
        receipt = self.fixture.receipt(context, approval_id="fixture-reuse-0001")
        rollout.deploy(receipt, owner_authorized=True)
        rollback_receipt = self.fixture.rollback_receipt(
            rollout, emergency=True
        )
        rollout.emergency_rollback(rollback_receipt, owner_authorized=True)
        with self.assertRaisesRegex(
            CapabilityError, "already authorized|No active generic capability"
        ):
            rollout.deploy(receipt, owner_authorized=True)

    def test_tampered_active_target_blocks_rollback(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        rollout.deploy(receipt, owner_authorized=True)
        rollback_receipt = self.fixture.rollback_receipt(
            rollout, emergency=True
        )
        with (
            self.fixture.target.target / "SKILL.md"
        ).open("ab") as handle:
            handle.write(b"tamper\n")
        with self.assertRaisesRegex(
            CapabilityError, "payload does not match its managed marker"
        ):
            rollout.emergency_rollback(
                rollback_receipt, owner_authorized=True
            )
        self.assertTrue(self.fixture.target.target.exists())
        self.assertFalse(self.fixture.target.lock_path.exists())

    def test_mutation_receipts_require_companion_recovery_actions(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        context = rollout.plan()["approval_context"]
        deploy_only = self.fixture.receipt(context, actions=["deploy"])
        with self.assertRaisesRegex(CapabilityError, "recover-deploy"):
            rollout.deploy(deploy_only, owner_authorized=True)
        self.assertFalse(self.fixture.target.target.exists())
        self.assertFalse(self.fixture.target.lock_path.exists())

        receipt = self.fixture.receipt(context)
        rollout.deploy(receipt, owner_authorized=True)
        rollback_only = self.fixture.rollback_receipt(
            rollout,
            emergency=True,
            actions=["emergency-rollback"],
        )
        with self.assertRaisesRegex(CapabilityError, "recover-rollback"):
            rollout.emergency_rollback(rollback_only, owner_authorized=True)
        self.assertTrue(self.fixture.target.target.is_dir())
        self.assertFalse(self.fixture.target.lock_path.exists())

    def test_missing_prior_backup_is_refused_before_current_quarantine(self) -> None:
        first = self.fixture.rollout(self.fixture.commit_v1)
        first_receipt = self.fixture.receipt(first.plan()["approval_context"])
        first.deploy(first_receipt, owner_authorized=True)
        second = self.fixture.rollout(self.fixture.commit_v2())
        second_receipt = self.fixture.receipt(second.plan()["approval_context"])
        deployed = second.deploy(second_receipt, owner_authorized=True)
        backup = (
            self.fixture.target.operations_root
            / deployed["transaction_id"]
            / "prior-target"
        )
        shutil.rmtree(backup)
        current = snapshot_target(self.fixture.target)
        active_raw = self.fixture.target.active_path.read_bytes()
        rollback_receipt = self.fixture.rollback_receipt(
            second, emergency=True
        )
        with self.assertRaisesRegex(CapabilityError, "backup is missing"):
            second.emergency_rollback(rollback_receipt, owner_authorized=True)
        self.assertEqual(snapshot_target(self.fixture.target), current)
        self.assertEqual(self.fixture.target.active_path.read_bytes(), active_raw)
        self.assertFalse(self.fixture.target.lock_path.exists())

    def test_missing_prior_active_is_refused_before_current_quarantine(self) -> None:
        first = self.fixture.rollout(self.fixture.commit_v1)
        first_receipt = self.fixture.receipt(first.plan()["approval_context"])
        first.deploy(first_receipt, owner_authorized=True)
        second = self.fixture.rollout(self.fixture.commit_v2())
        second_receipt = self.fixture.receipt(second.plan()["approval_context"])
        deployed = second.deploy(second_receipt, owner_authorized=True)
        operation = (
            self.fixture.target.operations_root / deployed["transaction_id"]
        )
        (operation / "prior-active.json").unlink()
        current = snapshot_target(self.fixture.target)
        active_raw = self.fixture.target.active_path.read_bytes()
        rollback_receipt = self.fixture.rollback_receipt(
            second, emergency=True
        )
        with self.assertRaisesRegex(CapabilityError, "prior-active evidence is missing"):
            second.emergency_rollback(rollback_receipt, owner_authorized=True)
        self.assertEqual(snapshot_target(self.fixture.target), current)
        self.assertEqual(self.fixture.target.active_path.read_bytes(), active_raw)
        self.assertFalse(self.fixture.target.lock_path.exists())

    def test_deploy_recovery_validates_prior_active_before_any_restore(self) -> None:
        first = self.fixture.rollout(self.fixture.commit_v1)
        first_receipt = self.fixture.receipt(first.plan()["approval_context"])
        first.deploy(first_receipt, owner_authorized=True)
        update = self.fixture.rollout(self.fixture.commit_v2())
        update_receipt = self.fixture.receipt(update.plan()["approval_context"])
        with self.assertRaises(InjectedCrash):
            update.deploy(
                update_receipt,
                owner_authorized=True,
                crash_after="active_recorded",
            )
        lock = json.loads(self.fixture.target.lock_path.read_text("ascii"))
        operation = self.fixture.target.operations_root / lock["transaction_id"]
        (operation / "prior-active.json").unlink()
        target_before = snapshot_target(self.fixture.target)
        active_before = self.fixture.target.active_path.read_bytes()
        backup_before = file_state(operation / "prior-target")
        quarantine = (
            self.fixture.target.quarantine_root
            / f"deploy-{lock['transaction_id']}-new-target"
        )
        with self.assertRaisesRegex(CapabilityError, "prior-active evidence is missing"):
            update.recover_deploy(update_receipt, owner_authorized=True)
        self.assertEqual(snapshot_target(self.fixture.target), target_before)
        self.assertEqual(self.fixture.target.active_path.read_bytes(), active_before)
        self.assertEqual(file_state(operation / "prior-target"), backup_before)
        self.assertFalse(quarantine.exists())
        self.assertTrue(self.fixture.target.lock_path.exists())

    def test_active_record_unknown_transaction_is_refused(self) -> None:
        first = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(first.plan()["approval_context"])
        first.deploy(receipt, owner_authorized=True)
        active = json.loads(self.fixture.target.active_path.read_text(encoding="ascii"))
        active["deploy_transaction_id"] = "0" * 32
        self.fixture.target.active_path.write_bytes(canonical_json_bytes(active))
        second = self.fixture.rollout(self.fixture.commit_v2())
        with self.assertRaisesRegex(CapabilityError, "Invalid transaction JSON"):
            second.plan()
        self.assertTrue(self.fixture.target.target.is_dir())

    def test_concurrent_update_race_aborts_without_stale_lock(self) -> None:
        first = self.fixture.rollout(self.fixture.commit_v1)
        first_receipt = self.fixture.receipt(first.plan()["approval_context"])
        first.deploy(first_receipt, owner_authorized=True)

        second = self.fixture.rollout(self.fixture.commit_v2())
        second_receipt = self.fixture.receipt(second.plan()["approval_context"])
        third = self.fixture.rollout(self.fixture.commit_v3())
        third_receipt = self.fixture.receipt(third.plan()["approval_context"])
        original_acquire = third._acquire
        interleaved = False

        def acquire_after_other_update(*args: object, **kwargs: object) -> None:
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                second.deploy(second_receipt, owner_authorized=True)
            original_acquire(*args, **kwargs)

        third._acquire = acquire_after_other_update  # type: ignore[method-assign]
        with self.assertRaisesRegex(CapabilityError, "operation lease"):
            third.deploy(third_receipt, owner_authorized=True)
        self.assertFalse(self.fixture.target.lock_path.exists())
        self.assertEqual(
            snapshot_target(self.fixture.target)["source_commit"], first.commit
        )

    def test_process_loss_after_lock_has_owner_recovery_path(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        with self.assertRaises(InjectedCrash):
            rollout.deploy(
                receipt,
                owner_authorized=True,
                crash_after="lock_acquired",
            )
        result = rollout.recover_deploy(receipt, owner_authorized=True)
        self.assertEqual(result["status"], "ABORTED_NO_WRITES")
        replay = rollout.recover_deploy(receipt, owner_authorized=True)
        self.assertEqual(replay["status"], "ABORTED_NO_WRITES")
        self.assertTrue(replay["replayed"])
        with self.assertRaisesRegex(CapabilityError, "already authorized"):
            rollout.deploy(receipt, owner_authorized=True)
        self.assertFalse(self.fixture.target.lock_path.exists())
        self.assertFalse(self.fixture.target.target.exists())
        self.assertFalse(self.fixture.target.lock_path.exists())

        replacement = self.fixture.receipt(rollout.plan()["approval_context"])
        rollout.deploy(replacement, owner_authorized=True)
        rollback_receipt = self.fixture.rollback_receipt(
            rollout, emergency=True
        )
        with self.assertRaises(InjectedCrash):
            rollout.emergency_rollback(
                rollback_receipt,
                owner_authorized=True,
                crash_after="lock_acquired",
            )
        result = rollout.recover_rollback(
            rollback_receipt, owner_authorized=True
        )
        self.assertEqual(result["status"], "ABORTED_NO_WRITES")
        replay = rollout.recover_rollback(
            rollback_receipt, owner_authorized=True
        )
        self.assertEqual(replay["status"], "ABORTED_NO_WRITES")
        self.assertTrue(replay["replayed"])
        self.assertTrue(self.fixture.target.target.is_dir())
        self.assertFalse(self.fixture.target.lock_path.exists())
        with self.assertRaisesRegex(CapabilityError, "already authorized"):
            rollout.emergency_rollback(
                rollback_receipt, owner_authorized=True
            )
        replacement_rollback = self.fixture.rollback_receipt(
            rollout, emergency=True
        )
        rolled = rollout.emergency_rollback(
            replacement_rollback, owner_authorized=True
        )
        self.assertEqual(rolled["status"], "ROLLED_BACK")
        self.assertFalse(self.fixture.target.target.exists())

    def test_fresh_process_cli_recovers_lock_only_deploy(self) -> None:
        entrypoint, common = self.fixture.cli_fixture()
        plan = subprocess.run(
            [sys.executable, "-B", str(entrypoint), "plan", *common],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        context = json.loads(plan.stdout)["approval_context"]
        receipt = self.fixture.receipt(context)
        crashed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(entrypoint),
                "deploy",
                *common,
                "--approval-file",
                str(receipt),
                "--owner-authorized",
                "--crash-after",
                "lock_acquired",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(crashed.returncode, 2)
        self.assertTrue(self.fixture.target.lock_path.is_file())
        updated_engine = self.fixture.root / "updated-engine"
        for relative in ENGINE_PATHS:
            destination = updated_engine / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.hub_root / relative, destination)
        with (updated_engine / "scripts/capability_matrix.py").open("ab") as handle:
            handle.write(b"\n# compatible engine update fixture\n")
        updated_entrypoint = updated_engine / "scripts/capability_rollout.py"
        recovered = subprocess.run(
            [
                sys.executable,
                "-B",
                str(updated_entrypoint),
                "recover-deploy",
                *common,
                "--approval-file",
                str(receipt),
                "--owner-authorized",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        result = json.loads(recovered.stdout)
        self.assertEqual(result["status"], "ABORTED_NO_WRITES")
        self.assertFalse(self.fixture.target.lock_path.exists())

    def test_parent_engine_locks_recover_with_current_engine(self) -> None:
        current_entrypoint, common = self.fixture.cli_fixture()
        legacy_entrypoint = self.fixture.engine_at(LEGACY_ENGINE_COMMIT)

        def command(
            entrypoint: Path,
            action: str,
            *extra: str,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(entrypoint),
                    action,
                    *common,
                    *extra,
                ],
                check=check,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

        legacy_plan = json.loads(command(legacy_entrypoint, "plan").stdout)
        legacy_deploy_receipt = self.fixture.receipt(
            legacy_plan["approval_context"]
        )
        crashed = command(
            legacy_entrypoint,
            "deploy",
            "--approval-file",
            str(legacy_deploy_receipt),
            "--owner-authorized",
            "--crash-after",
            "lock_acquired",
            check=False,
        )
        self.assertEqual(crashed.returncode, 2, crashed.stderr)
        lock = json.loads(
            self.fixture.target.lock_path.read_text(encoding="ascii")
        )
        self.assertNotIn("requested_action", lock)
        recovered = command(
            current_entrypoint,
            "recover-deploy",
            "--approval-file",
            str(legacy_deploy_receipt),
            "--owner-authorized",
        )
        self.assertEqual(
            json.loads(recovered.stdout)["status"], "ABORTED_NO_WRITES"
        )

        current_plan = json.loads(command(current_entrypoint, "plan").stdout)
        current_receipt = self.fixture.receipt(
            current_plan["approval_context"]
        )
        deployed = command(
            current_entrypoint,
            "deploy",
            "--approval-file",
            str(current_receipt),
            "--owner-authorized",
        )
        self.assertEqual(json.loads(deployed.stdout)["status"], "COMMITTED")

        active = json.loads(
            self.fixture.target.active_path.read_text(encoding="ascii")
        )
        deploy_id = str(active["deploy_transaction_id"])
        deploy_operation = self.fixture.target.operations_root / deploy_id
        deploy_journal = json.loads(
            (deploy_operation / "journal.json").read_text(encoding="ascii")
        )
        legacy_rollback_receipt = self.fixture.receipt(
            deploy_journal["context"],
            actions=["emergency-rollback", "recover-rollback"],
        )
        legacy_value = json.loads(legacy_rollback_receipt.read_text("ascii"))
        legacy_value["owner_decision"] = (
            "GO V1.2: legacy engine compatibility; "
            f"ROLLBACK DEPLOY {deploy_id} AFTER ACTIVATION; "
            "EMERGENCY ROLLBACK WITHOUT DISCOVERY"
        )
        legacy_rollback_receipt.write_bytes(canonical_json_bytes(legacy_value))
        crashed = command(
            legacy_entrypoint,
            "emergency-rollback",
            "--approval-file",
            str(legacy_rollback_receipt),
            "--owner-authorized",
            "--crash-after",
            "lock_acquired",
            check=False,
        )
        self.assertEqual(crashed.returncode, 2, crashed.stderr)
        lock = json.loads(
            self.fixture.target.lock_path.read_text(encoding="ascii")
        )
        self.assertNotIn("requested_action", lock)
        recovered = command(
            current_entrypoint,
            "recover-rollback",
            "--approval-file",
            str(legacy_rollback_receipt),
            "--owner-authorized",
        )
        self.assertEqual(
            json.loads(recovered.stdout)["status"], "ABORTED_NO_WRITES"
        )
        self.assertTrue(self.fixture.target.target.is_dir())
        self.assertFalse(self.fixture.target.lock_path.exists())

    def test_parent_engine_renames_recover_with_current_engine(self) -> None:
        current_entrypoint, common = self.fixture.cli_fixture()
        legacy_entrypoint = self.fixture.engine_at(LEGACY_ENGINE_COMMIT)

        def command(
            entrypoint: Path,
            action: str,
            *extra: str,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(entrypoint),
                    action,
                    *common,
                    *extra,
                ],
                check=check,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

        legacy_plan = json.loads(command(legacy_entrypoint, "plan").stdout)
        legacy_receipt = self.fixture.receipt(
            legacy_plan["approval_context"]
        )
        crashed_deploy = command(
            legacy_entrypoint,
            "deploy",
            "--approval-file",
            str(legacy_receipt),
            "--owner-authorized",
            "--crash-after",
            "target_renamed",
            check=False,
        )
        self.assertEqual(crashed_deploy.returncode, 2, crashed_deploy.stderr)
        self.assertTrue(self.fixture.target.target.is_dir())
        deploy_lock = json.loads(
            self.fixture.target.lock_path.read_text(encoding="ascii")
        )
        interrupted_deploy_id = str(deploy_lock["transaction_id"])
        recovered_deploy = command(
            current_entrypoint,
            "recover-deploy",
            "--approval-file",
            str(legacy_receipt),
            "--owner-authorized",
        )
        self.assertEqual(
            json.loads(recovered_deploy.stdout)["status"], "ROLLED_BACK"
        )
        replayed_deploy = json.loads(
            command(
                current_entrypoint,
                "recover-deploy",
                "--approval-file",
                str(legacy_receipt),
                "--owner-authorized",
            ).stdout
        )
        self.assertTrue(replayed_deploy["replayed"])
        self.assertFalse(self.fixture.target.target.exists())
        self.assertFalse(self.fixture.target.active_path.exists())
        self.assertFalse(self.fixture.target.lock_path.exists())
        self.assertTrue(
            (
                self.fixture.target.quarantine_root
                / f"deploy-{interrupted_deploy_id}-new-target"
            ).is_dir()
        )

        current_plan = json.loads(command(current_entrypoint, "plan").stdout)
        current_receipt = self.fixture.receipt(
            current_plan["approval_context"]
        )
        deployed = json.loads(
            command(
                current_entrypoint,
                "deploy",
                "--approval-file",
                str(current_receipt),
                "--owner-authorized",
            ).stdout
        )
        deploy_id = str(deployed["transaction_id"])
        deploy_operation = self.fixture.target.operations_root / deploy_id
        deploy_journal = json.loads(
            (deploy_operation / "journal.json").read_text("ascii")
        )
        legacy_rollback_receipt = self.fixture.receipt(
            deploy_journal["context"],
            actions=["emergency-rollback", "recover-rollback"],
        )
        value = json.loads(legacy_rollback_receipt.read_text("ascii"))
        value["owner_decision"] = (
            "GO V1.2: legacy engine compatibility; "
            f"ROLLBACK DEPLOY {deploy_id} AFTER ACTIVATION; "
            "EMERGENCY ROLLBACK WITHOUT DISCOVERY"
        )
        legacy_rollback_receipt.write_bytes(canonical_json_bytes(value))
        crashed_rollback = command(
            legacy_entrypoint,
            "emergency-rollback",
            "--approval-file",
            str(legacy_rollback_receipt),
            "--owner-authorized",
            "--crash-after",
            "rollback_target_renamed",
            check=False,
        )
        self.assertEqual(
            crashed_rollback.returncode, 2, crashed_rollback.stderr
        )
        self.assertFalse(self.fixture.target.target.exists())
        rollback_lock = json.loads(
            self.fixture.target.lock_path.read_text(encoding="ascii")
        )
        interrupted_rollback_id = str(rollback_lock["transaction_id"])
        recovered_rollback = command(
            current_entrypoint,
            "recover-rollback",
            "--approval-file",
            str(legacy_rollback_receipt),
            "--owner-authorized",
        )
        self.assertEqual(
            json.loads(recovered_rollback.stdout)["status"], "ROLLED_BACK"
        )
        replayed_rollback = json.loads(
            command(
                current_entrypoint,
                "recover-rollback",
                "--approval-file",
                str(legacy_rollback_receipt),
                "--owner-authorized",
            ).stdout
        )
        self.assertTrue(replayed_rollback["replayed"])
        self.assertFalse(self.fixture.target.target.exists())
        self.assertFalse(self.fixture.target.active_path.exists())
        self.assertFalse(self.fixture.target.lock_path.exists())
        self.assertTrue(
            (
                self.fixture.target.quarantine_root
                / f"rollback-{interrupted_rollback_id}-current-target"
            ).is_dir()
        )

    def test_process_kill_during_first_journal_publish_is_recoverable(self) -> None:
        entrypoint, common = self.fixture.cli_fixture()
        planned = subprocess.run(
            [sys.executable, "-B", str(entrypoint), "plan", *common],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        receipt = self.fixture.receipt(
            json.loads(planned.stdout)["approval_context"]
        )
        killer = "\n".join(
            (
                "import os, sys",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(self.hub_root / 'scripts')!r})",
                "import capability_rollout as module",
                "original = module.os.replace",
                "def replace_then_exit(source, destination):",
                "    if Path(destination).name == 'journal.json':",
                "        os._exit(93)",
                "    return original(source, destination)",
                "module.os.replace = replace_then_exit",
                "raise SystemExit(module.main(sys.argv[1:]))",
            )
        )
        killed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                killer,
                "deploy",
                *common,
                "--approval-file",
                str(receipt),
                "--owner-authorized",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(killed.returncode, 93, killed.stderr)
        lock = json.loads(
            self.fixture.target.lock_path.read_text(encoding="ascii")
        )
        operation = (
            self.fixture.target.operations_root / str(lock["transaction_id"])
        )
        self.assertFalse((operation / "journal.json").exists())
        self.assertEqual(
            len(list(operation.glob(".journal.json.tmp-*"))), 1
        )
        recovered = subprocess.run(
            [
                sys.executable,
                "-B",
                str(entrypoint),
                "recover-deploy",
                *common,
                "--approval-file",
                str(receipt),
                "--owner-authorized",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(
            json.loads(recovered.stdout)["status"], "ABORTED_NO_WRITES"
        )
        replayed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(entrypoint),
                "recover-deploy",
                *common,
                "--approval-file",
                str(receipt),
                "--owner-authorized",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.assertTrue(json.loads(replayed.stdout)["replayed"])
        self.assertFalse(self.fixture.target.target.exists())

    def test_public_cli_blocked_matrix_allows_emergency_removal(self) -> None:
        entrypoint, common = self.fixture.cli_fixture()

        def command(action: str, *extra: str) -> dict[str, object]:
            completed = subprocess.run(
                [sys.executable, "-B", str(entrypoint), action, *common, *extra],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            return json.loads(completed.stdout)

        deploy_plan = command("plan")
        deploy_receipt = self.fixture.receipt(
            deploy_plan["approval_context"]
        )
        command(
            "deploy",
            "--approval-file",
            str(deploy_receipt),
            "--owner-authorized",
        )
        matrix_path = Path(common[common.index("--matrix") + 1])
        matrix = json.loads(matrix_path.read_text("ascii"))
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        matrix["verified_at"] = now
        for row in matrix["targets"]:
            if row["tool"] == "antigravity":
                row["blocker"] = "fixture policy now blocks deployment"
                row["observed_at"] = now
                row["rollout_status"] = "blocked"
                row["scan_root_inventory_sha256"] = scan_root_inventory(
                    self.fixture.target.scan_root
                )
        matrix_path.write_bytes(canonical_json_bytes(matrix))
        blocked_deploy = subprocess.run(
            [sys.executable, "-B", str(entrypoint), "plan", *common],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(blocked_deploy.returncode, 2)
        self.assertIn("matrix blocks this target", blocked_deploy.stderr)
        self.assertTrue(self.fixture.target.target.is_dir())
        rollback_plan = command("plan-emergency-rollback")
        self.assertEqual(rollback_plan["status"], "READY")
        receipt = self.fixture.rollback_receipt_from_plan(
            rollback_plan, emergency=True
        )
        rolled = command(
            "emergency-rollback",
            "--approval-file",
            str(receipt),
            "--owner-authorized",
        )
        self.assertEqual(rolled["status"], "ROLLED_BACK")
        self.assertFalse(self.fixture.target.target.exists())

    def test_public_cli_real_mode_plan_in_temp_canonical_repo(self) -> None:
        canonical = self.fixture.root / "real-canonical"
        canonical.mkdir()
        scan_root = self.fixture.root / "real-profile" / "skills"
        state_root = self.fixture.root / "real-state" / "fixture-skill"
        guard_root = self.fixture.root / "real-profile" / "guards"
        scan_root.mkdir(parents=True)
        guard_root.mkdir(parents=True)
        state_root.parent.mkdir(parents=True)
        host, user = current_os_identity()
        platform_name = "windows" if os.name == "nt" else "linux"

        registry = json.loads(
            (self.hub_root / "capability-targets.json").read_text("ascii")
        )
        for row in registry["targets"]:
            if row["tool"] != "antigravity":
                continue
            row["expected_host"] = host
            row["expected_user"] = user
            row["platform"] = platform_name
            row["scan_root_template"] = str(scan_root)
            row["state_root_template"] = str(state_root)
            for guard in row["guard_paths"]:
                guard_path = guard_root / f"{guard['id']}.txt"
                guard_path.write_text(
                    f"{guard['id']} real-mode fixture\n", encoding="ascii"
                )
                guard["template"] = str(guard_path)

        now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        matrix = json.loads(
            (self.hub_root / "capability-matrix-v1.2.json").read_text("ascii")
        )
        matrix["capability_id"] = "fixture-skill"
        matrix["verified_at"] = now
        for row in matrix["targets"]:
            if row["tool"] != "antigravity":
                continue
            row["backup_like_count"] = 0
            row["blocker"] = None
            row["capability_target_state"] = "absent"
            row["collision"] = "none"
            row["host"] = host
            row["observed_at"] = now
            row["rollout_status"] = "eligible"
            row["scan_root"] = str(scan_root)
            row["scan_root_inventory_sha256"] = scan_root_inventory(scan_root)

        revocations = {
            "entries": [],
            "schema_version": 1,
            "updated_at": now,
        }
        for relative in ENGINE_PATHS:
            destination = canonical / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.hub_root / relative, destination)
        shutil.copytree(
            self.fixture.repo / "capabilities" / "fixture-skill",
            canonical / "capabilities" / "fixture-skill",
        )
        (canonical / "capability-targets.json").write_bytes(
            canonical_json_bytes(registry)
        )
        (canonical / "capability-matrix-v1.2.json").write_bytes(
            canonical_json_bytes(matrix)
        )
        (canonical / "capability-revocations.json").write_bytes(
            canonical_json_bytes(revocations)
        )

        git(canonical, "init", "-q", "-b", "main")
        git(canonical, "config", "user.name", "Real Mode Test")
        git(canonical, "config", "user.email", "real-mode@example.invalid")
        git(canonical, "config", "core.autocrlf", "false")
        git(canonical, "add", "--", ".")
        git(canonical, "commit", "-q", "-m", "real mode fixture")
        policy_commit = git(canonical, "rev-parse", "HEAD")
        origin = self.fixture.root / "real-origin.git"
        origin.mkdir()
        git(origin, "init", "--bare", "-q")
        git(canonical, "remote", "add", "origin", str(origin))
        git(canonical, "push", "-q", "-u", "origin", "main")
        git(canonical, "update-ref", "-d", "refs/remotes/origin/main")
        git(
            canonical,
            "fetch",
            "-q",
            "origin",
            "main:refs/remotes/origin/main",
        )

        before = file_state(scan_root)
        profile_before = file_state(scan_root.parent)
        state_parent_before = file_state(state_root.parent)
        canonical_before = file_state(canonical)
        self.assertFalse(state_root.exists())
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(canonical / "scripts/capability_rollout.py"),
                "plan",
                "--repo",
                str(canonical),
                "--commit",
                policy_commit,
                "--policy-commit",
                policy_commit,
                "--capability-id",
                "fixture-skill",
                "--tool",
                "antigravity",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=canonical,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["writes_performed"], 0)
        self.assertEqual(file_state(scan_root), before)
        self.assertEqual(file_state(scan_root.parent), profile_before)
        self.assertEqual(file_state(state_root.parent), state_parent_before)
        self.assertEqual(file_state(canonical), canonical_before)
        self.assertFalse(state_root.exists())
        self.assertEqual(
            result["approval_context"]["revocation_catalog_commit"],
            policy_commit,
        )
        self.assertEqual(
            result["approval_context"]["engine"]["compatibility_version"], 1
        )
        (canonical / "README.md").write_text(
            "newer local policy\n", encoding="ascii"
        )
        git(canonical, "add", "--", "README.md")
        git(canonical, "commit", "-q", "-m", "newer local policy")
        dummy_receipt = self.fixture.receipts / "real-mode-dummy.json"
        dummy_receipt.write_text("{}\n", encoding="ascii")
        stale_rollback = subprocess.run(
            [
                sys.executable,
                "-B",
                str(canonical / "scripts/capability_rollout.py"),
                "emergency-rollback",
                "--repo",
                str(canonical),
                "--commit",
                policy_commit,
                "--policy-commit",
                policy_commit,
                "--capability-id",
                "fixture-skill",
                "--tool",
                "antigravity",
                "--approval-file",
                str(dummy_receipt),
                "--owner-authorized",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=canonical,
        )
        self.assertEqual(stale_rollback.returncode, 2)
        self.assertIn(
            "policy commit at local main", stale_rollback.stderr
        )
        self.assertEqual(file_state(scan_root), before)
        self.assertFalse(state_root.exists())

        matrix["verified_at"] = "2020-01-01T00:00:00Z"
        for row in matrix["targets"]:
            if row["tool"] == "antigravity":
                row["observed_at"] = "2020-01-01T00:00:00Z"
        (canonical / "capability-matrix-v1.2.json").write_bytes(
            canonical_json_bytes(matrix)
        )
        git(canonical, "add", "--", "capability-matrix-v1.2.json")
        git(canonical, "commit", "-q", "-m", "stale install evidence")
        stale_policy_commit = git(canonical, "rev-parse", "HEAD")
        common_current_policy = [
            "--repo",
            str(canonical),
            "--commit",
            policy_commit,
            "--policy-commit",
            stale_policy_commit,
            "--capability-id",
            "fixture-skill",
            "--tool",
            "antigravity",
        ]
        stale_install_plan = subprocess.run(
            [
                sys.executable,
                "-B",
                str(canonical / "scripts/capability_rollout.py"),
                "plan",
                *common_current_policy,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=canonical,
        )
        self.assertEqual(stale_install_plan.returncode, 2)
        self.assertIn("matrix evidence is stale", stale_install_plan.stderr)
        stale_emergency_plan = subprocess.run(
            [
                sys.executable,
                "-B",
                str(canonical / "scripts/capability_rollout.py"),
                "plan-emergency-rollback",
                *common_current_policy,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=canonical,
        )
        self.assertEqual(stale_emergency_plan.returncode, 2)
        self.assertIn(
            "No active generic capability install record exists",
            stale_emergency_plan.stderr,
        )
        self.assertNotIn("evidence is stale", stale_emergency_plan.stderr)
        self.assertEqual(file_state(scan_root), before)
        self.assertFalse(state_root.exists())

    def test_file_state_oracle_detects_create_delete_root_metadata(self) -> None:
        root = self.fixture.root / "oracle-create-delete"
        root.mkdir()
        before = file_state(root)
        self.assertIn(".", before)
        probe = root / "probe.txt"
        probe.write_text("probe\n", encoding="ascii")
        self.assertNotEqual(file_state(root), before)
        probe.unlink()
        details = root.stat()
        os.utime(
            root,
            ns=(details.st_atime_ns, int(before["."][2]) + 1_000_000_000),
        )
        self.assertNotEqual(file_state(root), before)

    def test_public_cli_real_mode_full_lifecycle_without_fixture_root(self) -> None:
        real = RealModeFixture(self.fixture)
        before = real.snapshot()
        plan = real.run("plan")
        self.assertEqual(plan["status"], "READY")
        self.assertEqual(plan["writes_performed"], 0)
        self.assertEqual(real.snapshot(), before)
        self.assertFalse(real.state_root.exists())

        deploy_receipt = self.fixture.receipt(plan["approval_context"])
        deployed = real.run(
            "deploy",
            "--approval-file",
            str(deploy_receipt),
            "--owner-authorized",
        )
        self.assertEqual(deployed["status"], "COMMITTED")
        self.assertEqual(real.run("verify")["discovery"]["status"], "REQUIRED")
        _, evidence_path = real.discovery_evidence(deployed)
        discovery_plan = real.run(
            "plan-discovery", "--evidence-file", str(evidence_path)
        )
        discovery_receipt = self.fixture.receipt(
            discovery_plan["approval_context"], actions=["record-discovery"]
        )
        recorded = real.run(
            "record-discovery",
            "--approval-file",
            str(discovery_receipt),
            "--evidence-file",
            str(evidence_path),
            "--owner-authorized",
        )
        self.assertEqual(recorded["status"], "RECORDED")
        self.assertEqual(real.run("verify")["discovery"]["status"], "RECORDED")
        rollback_plan = real.run("plan-rollback")
        rollback_receipt = self.fixture.rollback_receipt_from_plan(
            rollback_plan, emergency=False
        )
        rolled = real.run(
            "rollback",
            "--approval-file",
            str(rollback_receipt),
            "--owner-authorized",
        )
        self.assertEqual(rolled["status"], "ROLLED_BACK")
        replayed = real.run(
            "rollback",
            "--approval-file",
            str(rollback_receipt),
            "--owner-authorized",
        )
        self.assertTrue(replayed["replayed"])
        self.assertFalse(real.target.target.exists())

        next_plan = real.run("plan")
        next_receipt = self.fixture.receipt(next_plan["approval_context"])
        redeployed = real.run(
            "deploy",
            "--approval-file",
            str(next_receipt),
            "--owner-authorized",
        )
        self.assertEqual(redeployed["status"], "COMMITTED")
        emergency_plan = real.run("plan-emergency-rollback")
        emergency_receipt = self.fixture.rollback_receipt_from_plan(
            emergency_plan, emergency=True
        )
        emergency = real.run(
            "emergency-rollback",
            "--approval-file",
            str(emergency_receipt),
            "--owner-authorized",
        )
        self.assertEqual(emergency["status"], "ROLLED_BACK")
        emergency_replay = real.run(
            "emergency-rollback",
            "--approval-file",
            str(emergency_receipt),
            "--owner-authorized",
        )
        self.assertTrue(emergency_replay["replayed"])
        self.assertFalse(real.target.target.exists())

    def test_real_mode_accepts_exact_noop_fetch_head_when_reflog_is_stale(
        self,
    ) -> None:
        real = RealModeFixture(self.fixture)
        origin_oid = real.fetch_main_with_stale_unchanged_tracking_reflog()
        self.assertEqual(
            git(real.canonical, "rev-parse", "refs/remotes/origin/main"),
            origin_oid,
        )
        self.assertIn(
            "2020-01-01",
            git(
                real.canonical,
                "reflog",
                "show",
                "-1",
                "--date=iso-strict",
                "--format=%gD",
                "refs/remotes/origin/main",
            ),
        )
        before = real.snapshot()
        plan = real.run("plan")
        self.assertEqual(plan["status"], "READY")
        self.assertEqual(plan["writes_performed"], 0)
        self.assertEqual(real.snapshot(), before)
        self.assertFalse(real.state_root.exists())

    def test_real_mode_rejects_unrelated_or_ambiguous_fetch_head(
        self,
    ) -> None:
        real = RealModeFixture(self.fixture)
        origin_oid = real.fetch_main_with_stale_unchanged_tracking_reflog()
        fetch_head = Path(
            git(
                real.canonical,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "FETCH_HEAD",
            )
        )
        remote = git(real.canonical, "config", "--local", "--get", "remote.origin.url")
        remote_display = remote[:-4] if remote.lower().endswith(".git") else remote
        exact = f"{origin_oid}\t\tbranch 'main' of {remote_display}\n"
        fetch_head.write_text(exact, encoding="utf-8")
        before = real.snapshot()
        accepted = real.run("plan")
        self.assertEqual(accepted["status"], "READY")
        self.assertEqual(accepted["writes_performed"], 0)
        self.assertEqual(real.snapshot(), before)
        self.assertFalse(real.state_root.exists())

        fetch_head.write_text(exact, encoding="utf-8")
        tolerated_skew = dt.datetime.now(dt.timezone.utc).timestamp() + 4 * 60
        os.utime(fetch_head, (tolerated_skew, tolerated_skew))
        before = real.snapshot()
        accepted = real.run("plan")
        self.assertEqual(accepted["status"], "READY")
        self.assertEqual(accepted["writes_performed"], 0)
        self.assertEqual(real.snapshot(), before)
        self.assertFalse(real.state_root.exists())

        cases = {
            "wrong oid": f"{'0' * 40}\t\tbranch 'main' of {remote_display}\n",
            "wrong branch": f"{origin_oid}\t\tbranch 'other' of {remote_display}\n",
            "wrong remote": f"{origin_oid}\t\tbranch 'main' of unrelated.invalid/repo\n",
            "ambiguous append": exact + exact,
        }
        for label, raw in cases.items():
            with self.subTest(label=label):
                fetch_head.write_text(raw, encoding="utf-8")
                before = real.snapshot()
                refused = real.command("plan", check=False)
                self.assertEqual(refused.returncode, 2)
                self.assertIn("fetch evidence is stale", refused.stderr)
                self.assertEqual(real.snapshot(), before)
                self.assertFalse(real.state_root.exists())

        fetch_head.write_text(exact, encoding="utf-8")
        future = dt.datetime.now(dt.timezone.utc).timestamp() + 10 * 60
        os.utime(fetch_head, (future, future))
        before = real.snapshot()
        refused = real.command("plan", check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("fetch evidence is stale or in the future", refused.stderr)
        self.assertEqual(real.snapshot(), before)
        self.assertFalse(real.state_root.exists())

    def test_fetch_evidence_refuses_origin_main_change_during_validation(
        self,
    ) -> None:
        real = RealModeFixture(self.fixture)
        original_oid = real.policy_commit
        moved_oid = real.commit_policy_change("move origin during evidence check")
        git(
            real.canonical,
            "update-ref",
            "refs/remotes/origin/main",
            original_oid,
            moved_oid,
        )

        def move_ref_during_reflog_check(
            repo: Path | str, ref: str, expected_oid: str
        ) -> dt.datetime:
            self.assertEqual(Path(repo), real.canonical)
            self.assertEqual(ref, "refs/remotes/origin/main")
            self.assertEqual(expected_oid, original_oid)
            git(
                real.canonical,
                "update-ref",
                "refs/remotes/origin/main",
                moved_oid,
                original_oid,
            )
            return dt.datetime.now(dt.timezone.utc)

        target_before = file_state(real.profile_root)
        state_before = file_state(real.state_parent)
        with mock.patch.object(
            rollout_module,
            "_origin_reflog_timestamp",
            side_effect=move_ref_during_reflog_check,
        ):
            with self.assertRaisesRegex(
                CapabilityError, "origin/main changed while fetch evidence"
            ):
                rollout_module.remote_fetch_timestamp(
                    real.canonical, expected_oid=original_oid
                )
        self.assertEqual(file_state(real.profile_root), target_before)
        self.assertEqual(file_state(real.state_parent), state_before)

    def test_public_cli_real_mode_recovers_after_policy_change(self) -> None:
        real = RealModeFixture(self.fixture)
        plan = real.run("plan")
        deploy_receipt = self.fixture.receipt(plan["approval_context"])
        killed_deploy = real.crash_after_target_move(
            "deploy",
            "--approval-file",
            str(deploy_receipt),
            "--owner-authorized",
            match_source=False,
        )
        self.assertEqual(killed_deploy.returncode, 93, killed_deploy.stderr)
        self.assertTrue(real.target.target.is_dir())

        deploy_recovery_policy = real.commit_policy_change(
            "policy changes after interrupted deploy"
        )
        stale_deploy_recovery = real.command(
            "recover-deploy",
            "--approval-file",
            str(deploy_receipt),
            "--owner-authorized",
            policy_commit=real.release_commit,
            check=False,
        )
        self.assertEqual(stale_deploy_recovery.returncode, 2)
        self.assertIn(
            "policy commit at local main", stale_deploy_recovery.stderr
        )
        self.assertTrue(real.target.target.is_dir())
        self.assertTrue(real.target.lock_path.is_file())
        recovered_deploy = real.run(
            "recover-deploy",
            "--approval-file",
            str(deploy_receipt),
            "--owner-authorized",
            policy_commit=deploy_recovery_policy,
        )
        self.assertEqual(recovered_deploy["status"], "ROLLED_BACK")
        deploy_replay = real.run(
            "recover-deploy",
            "--approval-file",
            str(deploy_receipt),
            "--owner-authorized",
            policy_commit=deploy_recovery_policy,
        )
        self.assertTrue(deploy_replay["replayed"])
        self.assertFalse(real.target.target.exists())

        current_policy = real.commit_policy_change(
            "refresh absent state after deploy recovery"
        )
        replacement_plan = real.run("plan", policy_commit=current_policy)
        replacement_receipt = self.fixture.receipt(
            replacement_plan["approval_context"]
        )
        deployed = real.run(
            "deploy",
            "--approval-file",
            str(replacement_receipt),
            "--owner-authorized",
            policy_commit=current_policy,
        )
        self.assertEqual(deployed["status"], "COMMITTED")

        rollback_policy = real.commit_policy_change(
            "refresh active state before interrupted rollback"
        )
        rollback_plan = real.run(
            "plan-emergency-rollback", policy_commit=rollback_policy
        )
        rollback_receipt = self.fixture.rollback_receipt_from_plan(
            rollback_plan, emergency=True
        )
        killed_rollback = real.crash_after_target_move(
            "emergency-rollback",
            "--approval-file",
            str(rollback_receipt),
            "--owner-authorized",
            match_source=True,
            policy_commit=rollback_policy,
        )
        self.assertEqual(killed_rollback.returncode, 93, killed_rollback.stderr)
        self.assertFalse(real.target.target.exists())

        rollback_recovery_policy = real.commit_policy_change(
            "policy changes after interrupted rollback"
        )
        stale_rollback_recovery = real.command(
            "recover-rollback",
            "--approval-file",
            str(rollback_receipt),
            "--owner-authorized",
            policy_commit=rollback_policy,
            check=False,
        )
        self.assertEqual(stale_rollback_recovery.returncode, 2)
        self.assertIn(
            "policy commit at local main", stale_rollback_recovery.stderr
        )
        self.assertFalse(real.target.target.exists())
        self.assertTrue(real.target.lock_path.is_file())
        recovered_rollback = real.run(
            "recover-rollback",
            "--approval-file",
            str(rollback_receipt),
            "--owner-authorized",
            policy_commit=rollback_recovery_policy,
        )
        self.assertEqual(recovered_rollback["status"], "ROLLED_BACK")
        rollback_replay = real.run(
            "recover-rollback",
            "--approval-file",
            str(rollback_receipt),
            "--owner-authorized",
            policy_commit=rollback_recovery_policy,
        )
        self.assertTrue(rollback_replay["replayed"])
        self.assertFalse(real.target.target.exists())

    def test_recovery_revalidates_policy_inside_operation_lease(self) -> None:
        real = RealModeFixture(self.fixture)

        def race_code() -> str:
            return "\n".join(
                (
                    "import datetime as dt, json, subprocess, sys",
                    "from pathlib import Path",
                    f"sys.path.insert(0, {str(real.canonical / 'scripts')!r})",
                    "import capability_rollout as module",
                    "original = module.rollout_from_args",
                    "def raced(args):",
                    "    rollout = original(args)",
                    "    path = Path(args.repo) / module.MATRIX_NAME",
                    "    value = json.loads(path.read_text('ascii'))",
                    "    now = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')",
                    "    value['verified_at'] = now",
                    "    for row in value['targets']:",
                    "        if row['tool'] == args.tool:",
                    "            row['capability_target_state'] = ('managed-current' if rollout.target.target.exists() else 'absent')",
                    "            row['observed_at'] = now",
                    "            row['scan_root_inventory_sha256'] = module.scan_root_inventory(rollout.target.scan_root)",
                    "    path.write_bytes(module.canonical_json_bytes(value))",
                    "    git_exe = module.git_executable_identity()['path']",
                    "    subprocess.run([git_exe, '-C', args.repo, 'add', '--', module.MATRIX_NAME], check=True, stdout=subprocess.DEVNULL)",
                    "    subprocess.run([git_exe, '-C', args.repo, 'commit', '-q', '-m', 'policy race after recovery construction'], check=True)",
                    "    return rollout",
                    "module.rollout_from_args = raced",
                    "raise SystemExit(module.main(sys.argv[1:]))",
                )
            )

        def publish_raced_policy() -> str:
            current = git(real.canonical, "rev-parse", "HEAD")
            real.policy_commit = current
            real._publish_policy()
            return current

        plan = real.run("plan")
        deploy_receipt = self.fixture.receipt(plan["approval_context"])
        killed_deploy = real.crash_after_target_move(
            "deploy",
            "--approval-file",
            str(deploy_receipt),
            "--owner-authorized",
            match_source=False,
        )
        self.assertEqual(killed_deploy.returncode, 93, killed_deploy.stderr)
        deploy_lock_before = real.target.lock_path.read_bytes()
        deploy_target_before = file_state(real.target.target)
        raced_deploy = real.command(
            "recover-deploy",
            "--approval-file",
            str(deploy_receipt),
            "--owner-authorized",
            check=False,
            python_code=race_code(),
        )
        self.assertEqual(raced_deploy.returncode, 2, raced_deploy.stderr)
        self.assertIn(
            "Pinned policy commit is no longer current local main",
            raced_deploy.stderr,
        )
        self.assertEqual(real.target.lock_path.read_bytes(), deploy_lock_before)
        self.assertEqual(file_state(real.target.target), deploy_target_before)
        current_policy = publish_raced_policy()
        recovered_deploy = real.run(
            "recover-deploy",
            "--approval-file",
            str(deploy_receipt),
            "--owner-authorized",
            policy_commit=current_policy,
        )
        self.assertEqual(recovered_deploy["status"], "ROLLED_BACK")

        active_policy = real.commit_policy_change(
            "refresh policy after lease-race deploy recovery"
        )
        next_plan = real.run("plan", policy_commit=active_policy)
        next_receipt = self.fixture.receipt(next_plan["approval_context"])
        real.run(
            "deploy",
            "--approval-file",
            str(next_receipt),
            "--owner-authorized",
            policy_commit=active_policy,
        )
        rollback_policy = real.commit_policy_change(
            "refresh policy before lease-race rollback"
        )
        rollback_plan = real.run(
            "plan-emergency-rollback", policy_commit=rollback_policy
        )
        rollback_receipt = self.fixture.rollback_receipt_from_plan(
            rollback_plan, emergency=True
        )
        killed_rollback = real.crash_after_target_move(
            "emergency-rollback",
            "--approval-file",
            str(rollback_receipt),
            "--owner-authorized",
            match_source=True,
            policy_commit=rollback_policy,
        )
        self.assertEqual(killed_rollback.returncode, 93, killed_rollback.stderr)
        rollback_lock_before = real.target.lock_path.read_bytes()
        quarantine_before = file_state(real.target.quarantine_root)
        raced_rollback = real.command(
            "recover-rollback",
            "--approval-file",
            str(rollback_receipt),
            "--owner-authorized",
            policy_commit=rollback_policy,
            check=False,
            python_code=race_code(),
        )
        self.assertEqual(raced_rollback.returncode, 2, raced_rollback.stderr)
        self.assertIn(
            "Pinned policy commit is no longer current local main",
            raced_rollback.stderr,
        )
        self.assertEqual(real.target.lock_path.read_bytes(), rollback_lock_before)
        self.assertEqual(file_state(real.target.quarantine_root), quarantine_before)
        current_policy = publish_raced_policy()
        recovered_rollback = real.run(
            "recover-rollback",
            "--approval-file",
            str(rollback_receipt),
            "--owner-authorized",
            policy_commit=current_policy,
        )
        self.assertEqual(recovered_rollback["status"], "ROLLED_BACK")
        self.assertFalse(real.target.target.exists())
        self.assertFalse(real.target.active_path.exists())

    @unittest.skipUnless(
        os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0,
        "requires an actual POSIX UID 0 process",
    )
    def test_public_cli_real_mode_refuses_actual_root_without_writes(self) -> None:
        real = RealModeFixture(self.fixture)
        before = real.snapshot()
        refused = real.command("plan", check=False)
        self.assertEqual(refused.returncode, 2, refused.stdout)
        result = json.loads(refused.stderr)
        self.assertEqual(
            result["error"],
            "Real capability rollout refuses an elevated/root process",
        )
        self.assertEqual(result["writes_performed"], 0)
        self.assertEqual(real.snapshot(), before)
        self.assertFalse(real.state_root.exists())

    def test_public_cli_full_discovery_and_standard_rollback_lifecycle(self) -> None:
        entrypoint, common = self.fixture.cli_fixture()

        def run(action: str, *extra: str, check: bool = True) -> dict[str, object]:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(entrypoint),
                    action,
                    *common,
                    *extra,
                ],
                check=check,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            stream = completed.stdout if completed.returncode == 0 else completed.stderr
            return json.loads(stream)

        context = run("plan")["approval_context"]
        deploy_receipt = self.fixture.receipt(context)
        deployed = run(
            "deploy",
            "--approval-file",
            str(deploy_receipt),
            "--owner-authorized",
        )
        self.assertEqual(deployed["status"], "COMMITTED")
        self.assertEqual(run("verify")["discovery"]["status"], "REQUIRED")
        _, evidence_path = self.fixture.discovery_evidence(deployed)
        discovery_plan = run(
            "plan-discovery", "--evidence-file", str(evidence_path)
        )
        discovery_receipt = self.fixture.receipt(
            discovery_plan["approval_context"], actions=["record-discovery"]
        )
        recorded = run(
            "record-discovery",
            "--approval-file",
            str(discovery_receipt),
            "--evidence-file",
            str(evidence_path),
            "--owner-authorized",
        )
        self.assertEqual(recorded["status"], "RECORDED")
        self.assertEqual(run("verify")["discovery"]["status"], "RECORDED")
        rollback_plan = run("plan-rollback")
        self.assertEqual(rollback_plan["status"], "READY")
        rollback_receipt = self.fixture.rollback_receipt_from_plan(
            rollback_plan, emergency=False
        )
        rolled = run(
            "rollback",
            "--approval-file",
            str(rollback_receipt),
            "--owner-authorized",
        )
        self.assertEqual(rolled["status"], "ROLLED_BACK")
        replayed = run(
            "rollback",
            "--approval-file",
            str(rollback_receipt),
            "--owner-authorized",
        )
        self.assertTrue(replayed["replayed"])
        self.assertFalse(self.fixture.target.target.exists())

    def test_public_cli_emergency_rollback_and_fresh_process_recovery(self) -> None:
        entrypoint, common = self.fixture.cli_fixture()

        def command(action: str, *extra: str, check: bool = True) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(entrypoint),
                    action,
                    *common,
                    *extra,
                ],
                check=check,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

        plan = json.loads(command("plan").stdout)
        deploy_receipt = self.fixture.receipt(plan["approval_context"])
        deployed = command(
            "deploy",
            "--approval-file",
            str(deploy_receipt),
            "--owner-authorized",
        )
        self.assertEqual(json.loads(deployed.stdout)["status"], "COMMITTED")

        emergency_plan = json.loads(command("plan-emergency-rollback").stdout)
        first_rollback_receipt = self.fixture.rollback_receipt_from_plan(
            emergency_plan, emergency=True
        )
        crashed = command(
            "emergency-rollback",
            "--approval-file",
            str(first_rollback_receipt),
            "--owner-authorized",
            "--crash-after",
            "lock_acquired",
            check=False,
        )
        self.assertEqual(crashed.returncode, 2)
        recovered = command(
            "recover-rollback",
            "--approval-file",
            str(first_rollback_receipt),
            "--owner-authorized",
            check=False,
        )
        self.assertEqual(
            recovered.returncode,
            0,
            recovered.stderr,
        )
        self.assertEqual(
            json.loads(recovered.stdout)["status"], "ABORTED_NO_WRITES"
        )
        self.assertTrue(self.fixture.target.target.is_dir())
        replayed = command(
            "recover-rollback",
            "--approval-file",
            str(first_rollback_receipt),
            "--owner-authorized",
        )
        self.assertTrue(json.loads(replayed.stdout)["replayed"])

        emergency_plan = json.loads(command("plan-emergency-rollback").stdout)
        second_rollback_receipt = self.fixture.rollback_receipt_from_plan(
            emergency_plan, emergency=True
        )
        crashed = command(
            "emergency-rollback",
            "--approval-file",
            str(second_rollback_receipt),
            "--owner-authorized",
            "--crash-after",
            "rollback_target_renamed",
            check=False,
        )
        self.assertEqual(crashed.returncode, 2)
        recovered = command(
            "recover-rollback",
            "--approval-file",
            str(second_rollback_receipt),
            "--owner-authorized",
            check=False,
        )
        self.assertEqual(
            recovered.returncode,
            0,
            recovered.stderr,
        )
        self.assertEqual(json.loads(recovered.stdout)["status"], "ROLLED_BACK")
        self.assertFalse(self.fixture.target.target.exists())

    def test_concurrent_recovery_is_refused_by_os_lease(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        with self.assertRaises(InjectedCrash):
            rollout.deploy(
                receipt,
                owner_authorized=True,
                crash_after="lock_acquired",
            )
        with rollout._operation_lease():
            with self.assertRaisesRegex(CapabilityError, "operation lease"):
                rollout.recover_deploy(receipt, owner_authorized=True)
        result = rollout.recover_deploy(receipt, owner_authorized=True)
        self.assertEqual(result["status"], "ABORTED_NO_WRITES")

    def test_recovery_replays_when_competitor_releases_lock_before_lease(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        with self.assertRaises(InjectedCrash):
            rollout.deploy(
                receipt,
                owner_authorized=True,
                crash_after="lock_acquired",
            )
        original_lease = rollout._operation_lease

        @contextlib.contextmanager
        def deploy_competitor_finishes() -> object:
            with original_lease():
                completed = rollout._recover_deploy_locked(
                    receipt, owner_authorized=True
                )
                self.assertEqual(completed["status"], "ABORTED_NO_WRITES")
                yield

        rollout._operation_lease = deploy_competitor_finishes  # type: ignore[method-assign]
        replay = rollout.recover_deploy(receipt, owner_authorized=True)
        self.assertTrue(replay["replayed"])
        rollout._operation_lease = original_lease  # type: ignore[method-assign]

        deploy_receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        rollout.deploy(deploy_receipt, owner_authorized=True)
        rollback_receipt = self.fixture.rollback_receipt(
            rollout, emergency=True
        )
        with self.assertRaises(InjectedCrash):
            rollout.emergency_rollback(
                rollback_receipt,
                owner_authorized=True,
                crash_after="lock_acquired",
            )

        @contextlib.contextmanager
        def rollback_competitor_finishes() -> object:
            with original_lease():
                completed = rollout._recover_rollback_locked(
                    rollback_receipt, owner_authorized=True
                )
                self.assertEqual(completed["status"], "ABORTED_NO_WRITES")
                yield

        rollout._operation_lease = rollback_competitor_finishes  # type: ignore[method-assign]
        replay = rollout.recover_rollback(
            rollback_receipt, owner_authorized=True
        )
        self.assertTrue(replay["replayed"])
        self.assertTrue(self.fixture.target.target.is_dir())

    def test_live_deploy_excludes_recovery_until_process_finishes(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        paused = threading.Event()
        release = threading.Event()
        outcome: dict[str, object] = {}

        def pause_before_activate() -> None:
            paused.set()
            if not release.wait(timeout=20):
                raise RuntimeError("fixture deploy pause timed out")

        def run_deploy() -> None:
            try:
                outcome["result"] = rollout.deploy(
                    receipt,
                    owner_authorized=True,
                    before_activate=pause_before_activate,
                )
            except Exception as exc:  # pragma: no cover - asserted below
                outcome["error"] = exc

        worker = threading.Thread(target=run_deploy, daemon=True)
        worker.start()
        self.assertTrue(paused.wait(timeout=20))
        try:
            before = file_state(self.fixture.root)
            with self.assertRaisesRegex(CapabilityError, "operation lease"):
                rollout.recover_deploy(receipt, owner_authorized=True)
            self.assertEqual(file_state(self.fixture.root), before)
        finally:
            release.set()
            worker.join(timeout=20)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"]["status"], "COMMITTED")

    def test_lock_publication_crash_never_exposes_partial_final_lock(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        original_publish = rollout._publish_lock_intent

        def crash_before_publish(temporary: Path) -> None:
            value = json.loads(temporary.read_text(encoding="ascii"))
            self.assertEqual(value["operation"], "deploy")
            self.assertEqual(temporary.read_bytes(), canonical_json_bytes(value))
            raise InjectedCrash("lock_intent_fsynced")

        rollout._publish_lock_intent = crash_before_publish  # type: ignore[method-assign]
        with self.assertRaises(InjectedCrash):
            rollout.deploy(receipt, owner_authorized=True)
        self.assertFalse(self.fixture.target.lock_path.exists())
        self.assertFalse(self.fixture.target.target.exists())
        self.assertFalse(
            any(
                self.fixture.target.state_root.glob(".target-lock-intent-*.json")
            )
        )
        rollout._publish_lock_intent = original_publish  # type: ignore[method-assign]
        self.assertEqual(
            rollout.deploy(receipt, owner_authorized=True)["status"], "COMMITTED"
        )

    def test_process_kill_before_atomic_publish_never_exposes_partial_final(self) -> None:
        destination = self.fixture.root / "immutable" / "evidence.json"
        destination.parent.mkdir()
        script = "\n".join(
            (
                "import os, sys",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(self.hub_root / 'scripts')!r})",
                "import capability_rollout as module",
                "module.atomic_publish_no_replace = lambda *_: os._exit(91)",
                "module.immutable_write_bytes(Path(sys.argv[1]), b'complete\\n', 'fixture')",
            )
        )
        killed = subprocess.run(
            [sys.executable, "-B", "-c", script, str(destination)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(killed.returncode, 91)
        self.assertFalse(destination.exists())
        self.assertTrue(
            list(destination.parent.glob(".atomic-evidence.json.tmp-*"))
        )
        immutable_write_bytes(destination, b"complete\n", "fixture")
        self.assertEqual(destination.read_bytes(), b"complete\n")

    def test_process_kill_after_actual_atomic_publish_leaves_complete_final(self) -> None:
        destination = self.fixture.root / "immutable-after" / "evidence.json"
        destination.parent.mkdir()
        script = "\n".join(
            (
                "import os, sys",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(self.hub_root / 'scripts')!r})",
                "import capability_rollout as module",
                "original = module.atomic_publish_no_replace",
                "def publish_then_exit(temporary, destination):",
                "    original(temporary, destination)",
                "    os._exit(92)",
                "module.atomic_publish_no_replace = publish_then_exit",
                "module.immutable_write_bytes(Path(sys.argv[1]), b'complete\\n', 'fixture')",
            )
        )
        killed = subprocess.run(
            [sys.executable, "-B", "-c", script, str(destination)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(killed.returncode, 92)
        self.assertEqual(destination.read_bytes(), b"complete\n")

    def test_atomic_no_replace_concurrent_publish_has_one_complete_winner(self) -> None:
        root = self.fixture.root / "atomic-race"
        root.mkdir()
        destination = root / "winner.json"
        first = root / "first.tmp"
        second = root / "second.tmp"
        first.write_bytes(b"first-complete\n")
        second.write_bytes(b"second-complete\n")
        script = "\n".join(
            (
                "import sys",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(self.hub_root / 'scripts')!r})",
                "from capability_rollout import atomic_publish_no_replace",
                "try:",
                "    atomic_publish_no_replace(Path(sys.argv[1]), Path(sys.argv[2]))",
                "except FileExistsError:",
                "    raise SystemExit(3)",
            )
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-B", "-c", script, str(path), str(destination)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for path in (first, second)
        ]
        outputs = [process.communicate(timeout=30) for process in processes]
        self.assertEqual(
            sorted(process.returncode for process in processes), [0, 3], outputs
        )
        self.assertIn(
            destination.read_bytes(),
            {b"first-complete\n", b"second-complete\n"},
        )

    def test_inventory_drift_blocks_plan_and_pre_activation(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        context = rollout.plan()["approval_context"]
        receipt = self.fixture.receipt(context)
        unrelated = self.fixture.target.scan_root / "unrelated-skill"
        unrelated.mkdir()
        (unrelated / "SKILL.md").write_text("unrelated\n", encoding="ascii")
        with self.assertRaisesRegex(CapabilityError, "inventory differs"):
            rollout.plan()
        with self.assertRaisesRegex(CapabilityError, "inventory differs"):
            rollout.deploy(receipt, owner_authorized=True)
        self.assertFalse(self.fixture.target.state_root.exists())
        shutil.rmtree(unrelated)

        def drift_before_activate() -> None:
            unrelated.mkdir()
            (unrelated / "SKILL.md").write_text("late drift\n", encoding="ascii")

        with self.assertRaisesRegex(CapabilityError, "inventory differs"):
            rollout.deploy(
                receipt,
                owner_authorized=True,
                before_activate=drift_before_activate,
            )
        self.assertFalse(self.fixture.target.target.exists())
        self.assertTrue(self.fixture.target.lock_path.exists())
        shutil.rmtree(unrelated)
        recovered = rollout.recover_deploy(receipt, owner_authorized=True)
        self.assertEqual(recovered["status"], "ROLLED_BACK")

    def test_engine_drift_between_plan_and_deploy_is_refused(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        copied_root = self.fixture.root / "engine-copy"
        for relative in ENGINE_PATHS:
            source = self.hub_root / relative
            destination = copied_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        rollout.engine_root = copied_root
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        with (copied_root / ENGINE_PATHS[0]).open("ab") as handle:
            handle.write(b"\n# drift\n")
        with self.assertRaisesRegex(CapabilityError, "context mismatch"):
            rollout.deploy(receipt, owner_authorized=True)
        self.assertFalse(self.fixture.target.target.exists())

    def test_engine_drift_after_rollback_plan_is_refused(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        rollout.deploy(
            self.fixture.receipt(rollout.plan()["approval_context"]),
            owner_authorized=True,
        )
        copied_root = self.fixture.root / "rollback-engine-copy"
        for relative in ENGINE_PATHS:
            source = self.hub_root / relative
            destination = copied_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        rollout.engine_root = copied_root
        receipt = self.fixture.rollback_receipt(
            rollout, emergency=True
        )
        with (copied_root / ENGINE_PATHS[0]).open("ab") as handle:
            handle.write(b"\n# rollback engine drift\n")
        active_before = snapshot_target(self.fixture.target)
        with self.assertRaisesRegex(CapabilityError, "context mismatch"):
            rollout.emergency_rollback(receipt, owner_authorized=True)
        self.assertEqual(snapshot_target(self.fixture.target), active_before)
        self.assertFalse(self.fixture.target.lock_path.exists())

    def test_actual_git_executable_change_invalidates_rollback_receipt(self) -> None:
        import capability_core as core

        original_identity = core.git_executable_identity()
        original_pin = core._PINNED_GIT_EXECUTABLE
        original_path = Path(original_identity["path"])
        original_path_value = os.environ.get("PATH")
        wrapper_root = self.fixture.root / "pinned-git"
        wrapper_root.mkdir()
        if os.name == "nt":
            git_root = original_path.parent.parent
            isolated_git_root = wrapper_root / "Git"
            (isolated_git_root / "cmd").mkdir(parents=True)
            for child in git_root.iterdir():
                if child.name.casefold() == "cmd" or not child.is_dir():
                    continue
                destination = isolated_git_root / child.name
                try:
                    destination.symlink_to(child, target_is_directory=True)
                except OSError:
                    if destination.exists():
                        continue
                    shutil.copytree(child, destination)
            wrapper = isolated_git_root / "cmd" / "git.exe"
            shutil.copy2(original_path, wrapper)
            second_bytes = None
        else:
            self.assertNotIn("'", str(original_path))
            wrapper = wrapper_root / "git"
            first_bytes = (
                "#!/bin/sh\n"
                f"exec '{original_path}' \"$@\"\n"
            ).encode("ascii")
            second_bytes = (
                "#!/bin/sh\n"
                "# same-path replacement generation\n"
                f"exec '{original_path}' \"$@\"\n"
            ).encode("ascii")
            wrapper.write_bytes(first_bytes)
            wrapper.chmod(0o755)
        try:
            os.environ["PATH"] = str(wrapper.parent) + os.pathsep + (
                original_path_value or ""
            )
            core._PINNED_GIT_EXECUTABLE = None
            first_identity = core.git_executable_identity()
            self.assertEqual(
                Path(first_identity["path"]).resolve(), wrapper.resolve()
            )
            self.assertEqual(
                core.run_git(
                    self.fixture.repo, ["rev-parse", "--verify", "HEAD"]
                )
                .decode("ascii")
                .strip(),
                git(self.fixture.repo, "rev-parse", "HEAD"),
            )
            deployed_by = self.fixture.rollout(self.fixture.commit_v1)
            deploy_context = deployed_by.plan()["approval_context"]
            deployed_by.deploy(
                self.fixture.receipt(deploy_context), owner_authorized=True
            )
            rollback_receipt = self.fixture.rollback_receipt(
                deployed_by, emergency=True
            )
            active_before = snapshot_target(self.fixture.target)

            if second_bytes is None:
                with wrapper.open("ab") as handle:
                    handle.write(b"\nCAPABILITY-SAME-PATH-GENERATION-2\n")
            else:
                wrapper.write_bytes(second_bytes)
                wrapper.chmod(0o755)
            second_identity = core.git_executable_identity()
            self.assertEqual(second_identity["path"], first_identity["path"])
            self.assertNotEqual(
                second_identity["sha256"], first_identity["sha256"]
            )
            self.assertEqual(
                core.run_git(
                    self.fixture.repo, ["rev-parse", "--verify", "HEAD"]
                )
                .decode("ascii")
                .strip(),
                git(self.fixture.repo, "rev-parse", "HEAD"),
            )
            with self.assertRaisesRegex(CapabilityError, "context mismatch"):
                deployed_by.emergency_rollback(
                    rollback_receipt, owner_authorized=True
                )
            self.assertEqual(snapshot_target(self.fixture.target), active_before)
            self.assertFalse(self.fixture.target.lock_path.exists())
        finally:
            if original_path_value is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = original_path_value
            core._PINNED_GIT_EXECUTABLE = original_pin

    def test_git_calls_ignore_hostile_environment_hooks_and_replace_refs(
        self,
    ) -> None:
        import capability_core as core

        expected = git_bytes(
            self.fixture.repo,
            "show",
            f"{self.fixture.commit_v1}:capabilities/fixture-skill/SKILL.md",
        )
        replacement = self.fixture.commit_v2()
        git(
            self.fixture.repo,
            "replace",
            self.fixture.commit_v1,
            replacement,
        )
        hooks = self.fixture.root / "hostile-hooks"
        hooks.mkdir()
        hook = hooks / ("post-checkout.exe" if os.name == "nt" else "post-checkout")
        hook.write_text("hostile hook must never run\n", encoding="ascii")
        global_config = self.fixture.root / "hostile-global.gitconfig"
        global_config.write_text(
            "[core]\n\thooksPath = hostile-hooks\n",
            encoding="ascii",
        )
        hostile = {
            "CAPABILITY_GIT": str(self.fixture.root / "not-git"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(
                self.fixture.root / "hostile-alternates"
            ),
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_SYSTEM": str(global_config),
            "GIT_DIR": str(self.fixture.root / "hostile-git-dir"),
            "GIT_INDEX_FILE": str(self.fixture.root / "hostile-index"),
            "GIT_OBJECT_DIRECTORY": str(
                self.fixture.root / "hostile-objects"
            ),
            "GIT_REPLACE_REF_BASE": "refs/replace/hostile/",
            "GIT_WORK_TREE": str(self.fixture.root / "hostile-worktree"),
        }
        previous = {key: os.environ.get(key) for key in hostile}
        try:
            os.environ.update(hostile)
            actual = core.run_git(
                self.fixture.repo,
                [
                    "show",
                    f"{self.fixture.commit_v1}:capabilities/fixture-skill/SKILL.md",
                ],
            )
            self.assertEqual(actual, expected)
            hooks_path = core.run_git(
                self.fixture.repo, ["config", "--get", "core.hooksPath"]
            ).decode("utf-8").strip()
            self.assertEqual(hooks_path, os.devnull)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_invalid_recovery_receipt_is_strictly_zero_write(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        context = rollout.plan()["approval_context"]
        receipt = self.fixture.receipt(context, approval_id="fixture-valid-recovery")
        with self.assertRaises(InjectedCrash):
            rollout.deploy(
                receipt,
                owner_authorized=True,
                crash_after="lock_acquired",
            )
        invalid = self.fixture.receipt(
            context, approval_id="fixture-wrong-recovery"
        )
        before = file_state(self.fixture.root)
        with self.assertRaisesRegex(CapabilityError, "intent does not match"):
            rollout.recover_deploy(invalid, owner_authorized=True)
        self.assertEqual(file_state(self.fixture.root), before)
        self.assertTrue(self.fixture.target.lock_path.exists())
        rollout.recover_deploy(receipt, owner_authorized=True)

    def test_lock_only_abort_journal_completes_after_result_write_loss(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        with self.assertRaises(InjectedCrash):
            rollout.deploy(
                receipt,
                owner_authorized=True,
                crash_after="lock_acquired",
            )
        original_record = rollout._record_recovery_result

        def lose_result(*args: object, **kwargs: object) -> None:
            raise InjectedCrash("lock_only_journal_persisted")

        rollout._record_recovery_result = lose_result  # type: ignore[method-assign]
        with self.assertRaises(InjectedCrash):
            rollout.recover_deploy(receipt, owner_authorized=True)
        lock = json.loads(self.fixture.target.lock_path.read_text(encoding="ascii"))
        operation = self.fixture.target.operations_root / lock["transaction_id"]
        journal = json.loads((operation / "journal.json").read_text(encoding="ascii"))
        self.assertEqual(journal["state"], "aborted_no_writes")
        self.assertFalse((operation / "recovery-result.json").exists())
        rollout._record_recovery_result = original_record  # type: ignore[method-assign]
        result = rollout.recover_deploy(receipt, owner_authorized=True)
        self.assertEqual(result["status"], "ABORTED_NO_WRITES")
        self.assertFalse(self.fixture.target.lock_path.exists())

    def test_rollback_receipt_is_invalidated_by_guard_and_inventory_drift(
        self,
    ) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        rollout.deploy(
            self.fixture.receipt(rollout.plan()["approval_context"]),
            owner_authorized=True,
        )
        active_before = snapshot_target(self.fixture.target)
        guard = next(iter(self.fixture.target.guards.values()))
        guard_original = guard.read_bytes()
        guard_receipt = self.fixture.rollback_receipt(
            rollout, emergency=True
        )
        guard.write_bytes(guard_original + b"drift\n")
        with self.assertRaisesRegex(CapabilityError, "context mismatch"):
            rollout.emergency_rollback(
                guard_receipt, owner_authorized=True
            )
        self.assertEqual(snapshot_target(self.fixture.target), active_before)
        self.assertFalse(self.fixture.target.lock_path.exists())
        guard.write_bytes(guard_original)

        inventory_receipt = self.fixture.rollback_receipt(
            rollout, emergency=True
        )
        unrelated = self.fixture.target.scan_root / "late-unrelated-skill"
        unrelated.mkdir()
        (unrelated / "SKILL.md").write_text(
            "late inventory drift\n", encoding="ascii"
        )
        try:
            with self.assertRaisesRegex(
                CapabilityError, "Rollback scan-root inventory drifted"
            ):
                rollout.emergency_rollback(
                    inventory_receipt, owner_authorized=True
                )
            self.assertEqual(snapshot_target(self.fixture.target), active_before)
            self.assertFalse(self.fixture.target.lock_path.exists())
        finally:
            shutil.rmtree(unrelated)

    def test_committed_recovery_requires_full_active_link_and_backup(self) -> None:
        first = self.fixture.rollout(self.fixture.commit_v1)
        first_receipt = self.fixture.receipt(first.plan()["approval_context"])
        first.deploy(first_receipt, owner_authorized=True)
        update = self.fixture.rollout(self.fixture.commit_v2())
        receipt = self.fixture.receipt(update.plan()["approval_context"])
        with self.assertRaises(InjectedCrash):
            update.deploy(
                receipt,
                owner_authorized=True,
                crash_after="committed",
            )
        lock = json.loads(self.fixture.target.lock_path.read_text(encoding="ascii"))
        operation = self.fixture.target.operations_root / lock["transaction_id"]
        shutil.rmtree(operation / "prior-target")
        with self.assertRaisesRegex(CapabilityError, "backup is missing"):
            update.recover_deploy(receipt, owner_authorized=True)
        self.assertTrue(self.fixture.target.lock_path.exists())
        self.assertEqual(
            snapshot_target(self.fixture.target)["source_commit"], update.commit
        )

    def test_committed_recovery_rejects_tampered_active_link(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        with self.assertRaises(InjectedCrash):
            rollout.deploy(
                receipt,
                owner_authorized=True,
                crash_after="committed",
            )
        active = json.loads(
            self.fixture.target.active_path.read_text(encoding="ascii")
        )
        active["context_sha256"] = "0" * 64
        self.fixture.target.active_path.write_bytes(canonical_json_bytes(active))
        with self.assertRaisesRegex(CapabilityError, "active.*link|Active-install"):
            rollout.recover_deploy(receipt, owner_authorized=True)
        self.assertTrue(self.fixture.target.lock_path.exists())

    def test_terminal_replay_revalidates_semantics_and_direct_results(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        receipt = self.fixture.receipt(rollout.plan()["approval_context"])
        deployed = rollout.deploy(receipt, owner_authorized=True)
        replayed_deploy = rollout.deploy(receipt, owner_authorized=True)
        self.assertTrue(replayed_deploy["replayed"])
        rollback_receipt = self.fixture.rollback_receipt(
            rollout, emergency=True
        )
        rolled = rollout.emergency_rollback(
            rollback_receipt, owner_authorized=True
        )
        replayed_rollback = rollout.emergency_rollback(
            rollback_receipt, owner_authorized=True
        )
        self.assertTrue(replayed_rollback["replayed"])
        self.assertEqual(replayed_rollback["transaction_id"], rolled["transaction_id"])

        fixture = RolloutFixture(self.hub_root)
        try:
            recovery = fixture.rollout(fixture.commit_v1)
            recovery_receipt = fixture.receipt(
                recovery.plan()["approval_context"]
            )
            with self.assertRaises(InjectedCrash):
                recovery.deploy(
                    recovery_receipt,
                    owner_authorized=True,
                    crash_after="target_activated",
                )
            result = recovery.recover_deploy(
                recovery_receipt, owner_authorized=True
            )
            operation = fixture.target.operations_root / result["transaction_id"]
            wrapper_path = operation / "recovery-result.json"
            wrapper = json.loads(wrapper_path.read_text(encoding="ascii"))
            wrapper["result"]["target_present"] = True
            wrapper["result_sha256"] = canonical_json_hash(wrapper["result"])
            wrapper_path.write_bytes(canonical_json_bytes(wrapper))
            with self.assertRaisesRegex(CapabilityError, "disk state"):
                recovery.recover_deploy(
                    recovery_receipt, owner_authorized=True
                )
        finally:
            fixture.cleanup()
        self.assertEqual(deployed["status"], "COMMITTED")

    def test_partial_discovery_state_is_completed_idempotently(self) -> None:
        rollout = self.fixture.rollout(self.fixture.commit_v1)
        deploy_context = rollout.plan()["approval_context"]
        deploy_receipt = self.fixture.receipt(deploy_context)
        deployed = rollout.deploy(deploy_receipt, owner_authorized=True)
        evidence, evidence_path = self.fixture.discovery_evidence(deployed)
        evidence_raw = canonical_json_bytes(evidence)
        plan = rollout.plan_discovery(evidence_path)
        receipt = self.fixture.receipt(
            plan["approval_context"], actions=["record-discovery"]
        )
        operation = self.fixture.target.operations_root / deployed["transaction_id"]
        (operation / "discovery-evidence.json").write_bytes(evidence_raw)
        result = rollout.record_discovery(
            receipt, evidence_path, owner_authorized=True
        )
        self.assertEqual(result["status"], "RECORDED")
        (operation / "discovery-record.json").unlink()
        completed_again = rollout.record_discovery(
            receipt, evidence_path, owner_authorized=True
        )
        self.assertEqual(completed_again["status"], "RECORDED")
        self.assertEqual(rollout.verify()["discovery"]["status"], "RECORDED")

        changed = dict(evidence)
        changed["observed_output"] = str(changed["observed_output"]) + "\nchanged"
        changed["observed_output_sha256"] = __import__("hashlib").sha256(
            str(changed["observed_output"]).encode("utf-8")
        ).hexdigest()
        changed_path = self.fixture.root / "changed-discovery.json"
        changed_path.write_bytes(canonical_json_bytes(changed))
        with self.assertRaisesRegex(CapabilityError, "context mismatch"):
            rollout.record_discovery(
                receipt, changed_path, owner_authorized=True
            )

    def test_stale_active_record_and_stale_fetch_are_refused(self) -> None:
        self.fixture.target.state_root.mkdir(parents=True)
        self.fixture.target.active_path.write_text("{}\n", encoding="ascii")
        with self.assertRaisesRegex(CapabilityError, "target is absent"):
            self.fixture.rollout(self.fixture.commit_v1).plan()
        self.fixture.target.active_path.unlink()
        with self.assertRaisesRegex(CapabilityError, "fetch evidence"):
            CapabilityRollout(
                repo=self.fixture.repo,
                commit=self.fixture.commit_v1,
                capability_id="fixture-skill",
                target=self.fixture.target,
                approval_posture=POSTURE,
                registry_sha256="a" * 64,
                matrix_sha256="b" * 64,
                matrix_status="eligible",
                scan_root_inventory_sha256=scan_root_inventory(
                    self.fixture.target.scan_root
                ),
                policy_commit=self.fixture.commit_v1,
                revocation_catalog=load_revocations(
                    self.hub_root / "capability-revocations.json"
                ),
                revocation_checked_at="2020-01-01T00:00:00Z",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
