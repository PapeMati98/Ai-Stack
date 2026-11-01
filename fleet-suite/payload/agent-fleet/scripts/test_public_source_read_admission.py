from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet


COMMIT = "a" * 40
SOURCE_PATH = "inputs/upstream/src/secrets/store/secret-store.ts"
SENTINEL = b'export const PUBLIC_SENTINEL = "two-gap-synthetic-public-source-v1";\n'
SENTINEL_SHA = hashlib.sha256(SENTINEL).hexdigest()
PUBLIC_URL = (
    "https://raw.githubusercontent.com/example/public-source/"
    + COMMIT
    + "/inputs/upstream/src/secrets/store/secret-store.ts"
)
WORKER_RUN = "run-20260913T010000Z-aaaaaaaaaa"
GATE_ID = "qgate-20260913T020000Z-bbbbbbbbbb"
TASK_ID = "task-worker-0001"
SNAPSHOT_REL = f"snapshots/{TASK_ID}/{SOURCE_PATH}"
NEIGHBOR_REL = f"snapshots/{TASK_ID}/inputs/upstream/src/secrets/store/neighbor.ts"


def glob_match(pattern: str, path: str) -> bool:
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    escaped = re.escape(pattern)
    escaped = escaped.replace(r"\*\*", "\0DOUBLE\0")
    escaped = escaped.replace(r"\*", "[^/]*")
    escaped = escaped.replace(r"\?", "[^/]")
    escaped = escaped.replace("\0DOUBLE\0", ".*")
    return re.fullmatch(escaped, path) is not None


def last_match_read_action(config: dict, request_path: str) -> str:
    rules = config["permission"]["read"]
    action = str(rules.get("*") or config["permission"].get("*") or "deny")
    for pattern, value in rules.items():
        if pattern == "*":
            continue
        if glob_match(str(pattern), request_path):
            action = str(value)
    return action


def provenance(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "worker_run_id": WORKER_RUN,
        "source_path": SOURCE_PATH,
        "sha256": SENTINEL_SHA,
        "url": PUBLIC_URL,
        "commit": COMMIT,
        "classification": "public-source",
        "authorship_identity": False,
        "quality_verdict": False,
        "size_bytes": len(SENTINEL),
    }
    payload.update(overrides)
    return payload


def make_synthetic_bundle(*, mutate=None, neighbor: bool = True) -> Path:
        root = Path(tempfile.mkdtemp())
        bundle = root / GATE_ID
        snapshot = bundle / Path(*SNAPSHOT_REL.split("/"))
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(SENTINEL)
        if neighbor:
            other = bundle / Path(*NEIGHBOR_REL.split("/"))
            other.write_bytes(b'export const NEIGHBOR = "protected-neighbor";\n')
        manifest = {
            "schema_version": 2,
            "gate_id": GATE_ID,
            "public_source": {
                "schema_version": 1,
                "classification": "public-source",
                "authorship_identity": False,
                "quality_verdict": False,
                "files": [provenance()],
            },
            "workers": [
                {
                    "run_id": WORKER_RUN,
                    "root_task_id": TASK_ID,
                    "snapshots": [
                        {
                            "path": SNAPSHOT_REL,
                            "source_path": SOURCE_PATH,
                            "sha256": SENTINEL_SHA,
                            "size_bytes": len(SENTINEL),
                            "public_source": provenance(),
                        }
                    ],
                }
            ],
        }
        if mutate is not None:
            mutate(bundle, manifest)
        (bundle / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        return bundle


class PublicSourceReadAdmissionTests(unittest.TestCase):
    def _bundle(self, *, mutate=None, neighbor: bool = True) -> Path:
        bundle = make_synthetic_bundle(mutate=mutate, neighbor=neighbor)
        self.addCleanup(shutil.rmtree, bundle.parent, ignore_errors=True)
        return bundle

    def test_parameterless_config_stays_default_deny_for_secrets(self) -> None:
        config = json.loads(fleet.opencode_runtime_config())
        self.assertEqual(config["permission"]["read"]["**/secrets/**"], "deny")
        self.assertEqual(config["permission"]["read"]["**/credentials/**"], "deny")
        extra_allows = [
            key
            for key, value in config["permission"]["read"].items()
            if value == "allow" and key != "*"
        ]
        self.assertEqual(extra_allows, [])
        self.assertEqual(
            last_match_read_action(config, SNAPSHOT_REL),
            "deny",
        )

    def _native_pattern(self, bundle: Path, relative: str) -> str:
        resolved = (bundle / Path(*relative.split("/"))).resolve()
        worktree = fleet.native_opencode_instance_worktree(bundle)
        return resolved.relative_to(worktree).as_posix()

    def test_current_gate_exact_file_is_admitted_after_revalidation(self) -> None:
        bundle = self._bundle()
        admissions = fleet.verified_captured_public_source_read_admissions(bundle)
        self.assertEqual(len(admissions), 1)
        self.assertEqual(admissions[0]["relative"], SNAPSHOT_REL)
        self.assertEqual(admissions[0]["sha256"], SENTINEL_SHA)
        native = self._native_pattern(bundle, SNAPSHOT_REL)
        self.assertEqual(admissions[0]["patterns"], [native])
        config = json.loads(
            fleet.opencode_runtime_config(verified_read_admissions=admissions)
        )
        allows = [
            key
            for key, value in config["permission"]["read"].items()
            if value == "allow" and key != "*"
        ]
        self.assertEqual(allows, [native])
        self.assertEqual(config["permission"]["read"]["**/secrets/**"], "deny")
        self.assertEqual(config["permission"]["read"][native], "allow")
        self.assertEqual(last_match_read_action(config, native), "allow")
        neighbor = self._native_pattern(bundle, NEIGHBOR_REL)
        self.assertEqual(last_match_read_action(config, neighbor), "deny")
        self.assertEqual(config["permission"]["edit"], "deny")
        self.assertEqual(config["permission"]["bash"], "deny")

    def test_native_rule_is_drive_relative_outside_git(self) -> None:
        bundle = self._bundle()
        current = fleet.lexical_absolute_path(bundle)
        while True:
            try:
                (current / ".git").lstat()
            except OSError:
                pass
            else:
                self.skipTest("synthetic bundle ancestor is a Git worktree")
            parent = current.parent
            if parent == current:
                break
            current = parent
        worktree = fleet.native_opencode_instance_worktree(bundle)
        self.assertEqual(worktree, Path(fleet.lexical_absolute_path(bundle).anchor))
        admissions = fleet.verified_captured_public_source_read_admissions(bundle)
        native = Path(admissions[0]["resolved"]).relative_to(worktree).as_posix()
        self.assertEqual(admissions[0]["patterns"], [native])
        self.assertNotEqual(native, SNAPSHOT_REL)
        self.assertFalse(native.startswith("/") or ":" in native[:3])

    def test_native_rule_is_git_worktree_relative_inside_git(self) -> None:
        bundle = self._bundle()
        subprocess.run(["git", "init", "-q"], cwd=bundle, check=True)
        worktree = fleet.native_opencode_instance_worktree(bundle)
        self.assertEqual(worktree, fleet.lexical_absolute_path(bundle))
        admissions = fleet.verified_captured_public_source_read_admissions(bundle)
        self.assertEqual(admissions[0]["patterns"], [SNAPSHOT_REL])
        config = json.loads(
            fleet.opencode_runtime_config(verified_read_admissions=admissions)
        )
        self.assertEqual(config["permission"]["read"][SNAPSHOT_REL], "allow")
        self.assertEqual(config["permission"]["read"]["**/secrets/**"], "deny")

    def test_changed_bytes_malformed_escape_and_other_gate_are_refused(self) -> None:
        changed = self._bundle()
        (changed / Path(*SNAPSHOT_REL.split("/"))).write_bytes(SENTINEL + b"tampered\n")
        with self.assertRaisesRegex(fleet.FleetError, "do not match provenance hash"):
            fleet.verified_captured_public_source_read_admissions(changed)

        def bad_commit(_bundle, manifest):
            manifest["public_source"]["files"][0]["commit"] = "b" * 40
            manifest["workers"][0]["snapshots"][0]["public_source"]["commit"] = "b" * 40

        malformed = self._bundle(mutate=bad_commit)
        with self.assertRaisesRegex(fleet.FleetError, "URL commit does not match"):
            fleet.verified_captured_public_source_read_admissions(malformed)

        def other_gate(_bundle, manifest):
            manifest["gate_id"] = "qgate-20260913T999999Z-cccccccccc"

        foreign = self._bundle(mutate=other_gate)
        with self.assertRaisesRegex(fleet.FleetError, "not bound to the current-gate"):
            fleet.verified_captured_public_source_read_admissions(foreign)

        escaped = self._bundle()
        with self.assertRaisesRegex(
            fleet.FleetError, "cannot waive a secret directory|glob waiver"
        ):
            fleet.opencode_runtime_config(
                verified_read_admissions=[
                    {
                        "gate_id": GATE_ID,
                        "patterns": ["**/secrets/**"],
                    }
                ]
            )

        def escaped_path(_bundle, manifest):
            manifest["workers"][0]["snapshots"][0]["path"] = "../secret-store.ts"

        escape = self._bundle(mutate=escaped_path)
        with self.assertRaisesRegex(
            fleet.FleetError, "Captured public-source path is not a confined relative file"
        ):
            fleet.verified_captured_public_source_read_admissions(escape)

        def missing_provenance(_bundle, manifest):
            del manifest["public_source"]["files"][0]["url"]

        missing = self._bundle(mutate=missing_provenance)
        with self.assertRaisesRegex(
            fleet.FleetError, "Malformed captured public-source provenance"
        ):
            fleet.verified_captured_public_source_read_admissions(missing)

        def ambiguous_snapshot(_bundle, manifest):
            duplicate = copy.deepcopy(manifest["workers"][0]["snapshots"][0])
            manifest["workers"][0]["snapshots"].append(duplicate)

        ambiguous = self._bundle(mutate=ambiguous_snapshot)
        with self.assertRaisesRegex(
            fleet.FleetError,
            "Captured public-source file is not uniquely bound to this gate snapshot",
        ):
            fleet.verified_captured_public_source_read_admissions(ambiguous)

    def test_root_junction_admission_is_refused(self) -> None:
        bundle = self._bundle()
        holder = Path(tempfile.mkdtemp())
        alias = holder / "alias"

        def cleanup() -> None:
            try:
                if alias.exists() or fleet.is_filesystem_link(alias):
                    fleet.remove_guarded_directory_link(alias, "test junction")
            except (OSError, fleet.FleetError):
                pass
            shutil.rmtree(holder, ignore_errors=True)

        self.addCleanup(cleanup)
        try:
            fleet.create_directory_link(bundle.parent, alias)
        except fleet.FleetError:
            self.skipTest("directory junction/symlink creation is not permitted")
        linked = alias / bundle.name
        self.assertTrue(linked.is_dir())
        with self.assertRaisesRegex(
            fleet.FleetError, "cannot be linked|reparse|Linked"
        ):
            fleet.verified_captured_public_source_read_admissions(linked)
        admitted = fleet.verified_captured_public_source_read_admissions(bundle)
        self.assertEqual(len(admitted), 1)

    def test_execute_single_quality_bundle_threads_admissions(self) -> None:
        bundle = self._bundle()
        admissions = fleet.verified_captured_public_source_read_admissions(bundle)
        expected = tuple(admissions[0]["patterns"])
        captured: dict[str, object] = {}

        def fake_validate(executable, workspace, env, expected_read_admissions=(), expected_office_mcp=None):
            captured["office_mcp"] = expected_office_mcp
            captured["config"] = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            captured["expected"] = expected_read_admissions
            captured["workspace"] = Path(workspace)
            return {"status": "PASS"}

        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        profile = copy.deepcopy(profiles["opencode"])
        profiles = {**profiles, "opencode": profile}
        execution_profile = {
            "execution_profile_id": "opencode-glm-5-3-high",
            "execution_profile_fingerprint": "f" * 64,
            "lifecycle": "ACTIVE",
            "model": "glm-5.3",
            "runtime_model_id": "zai-coding-plan/glm-5.3",
            "reasoning_level": "high",
            "supported_modes": ["review"],
            "tools": ["filesystem-read"],
        }
        handoff = {
            "status": "completed",
            "summary": "Reviewed the bounded public-source snapshot.",
            "files_changed": [],
            "verification": ["Read the frozen public snapshot."],
            "risks": [],
            "next_steps": [],
        }
        child_stdout = json.dumps(
            {
                "type": "text",
                "part": {"type": "text", "text": json.dumps(handoff)},
            }
        ) + "\n" + json.dumps(
            {
                "type": "step_finish",
                "part": {"reason": "stop", "tokens": {"input": 4, "output": 5}},
            }
        )
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with (
            mock.patch.dict(os.environ, {"AGENT_FLEET_DEPTH": "0"}),
            mock.patch("fleet.resolve_executable", return_value="opencode.exe"),
            mock.patch(
                "fleet.profile_credential_status",
                return_value={"required": True, "ready": True},
            ),
            mock.patch(
                "fleet.apply_profile_credential",
                side_effect=lambda env, _profile: env,
            ),
            mock.patch(
                "fleet.validate_opencode_resolved_config",
                side_effect=fake_validate,
            ),
            mock.patch(
                "fleet.probe_command",
                return_value={"launchable": True, "exit_code": 0, "output": "ok"},
            ),
            mock.patch(
                "fleet.routing_v2.readiness_from_probes",
                return_value={"ready": True, "reasons": []},
            ),
            mock.patch(
                "fleet.routing_v2.runtime_model_visible",
                return_value=(True, "visible"),
            ),
            mock.patch("fleet.adaptive.latest_quotas", return_value={}),
            mock.patch(
                "fleet.run_provider_child",
                return_value={
                    "child": {
                        "exit_code": 0,
                        "stdout": child_stdout,
                        "stderr": (
                            'level=INFO message="llm runtime selected" '
                            "llm.provider=zai-coding-plan llm.model=glm-5.3\n"
                        ),
                        "timed_out": False,
                        "model_tool_calls": None,
                        "tool_budget_exceeded": False,
                    },
                    "queue_wait_seconds": 0.0,
                    "execution_serialized": False,
                },
            ),
        ):
            fleet.execute_single(
                "Review the frozen public snapshot.",
                profiles=profiles,
                agent_id="opencode",
                mode="review",
                kind="review",
                complexity="high",
                model_override=None,
                workspace=bundle,
                timeout=60,
                allow_write=False,
                native_subagent_limit=0,
                enable_web=False,
                inherit_api_keys=False,
                dry_run=False,
                output_root=root / "evidence",
                root=root,
                execution_profile=execution_profile,
                knowledge_mode="none",
                quality_evidence_bundle=bundle,
            )
        self.assertIn("config", captured)
        config = captured["config"]
        assert isinstance(config, dict)
        self.assertEqual(captured["expected"], expected)
        self.assertEqual(config["permission"]["read"][expected[0]], "allow")
        self.assertEqual(config["permission"]["read"]["**/secrets/**"], "deny")
        extra_allows = [
            key
            for key, value in config["permission"]["read"].items()
            if value == "allow" and key not in {"*", *expected}
        ]
        self.assertEqual(extra_allows, [])
        self.assertEqual(
            os.path.normcase(str(captured["workspace"])),
            os.path.normcase(str(bundle)),
        )

    def test_run_quality_gate_passes_current_bundle_not_caller_claims(self) -> None:
        import inspect
        import public_source_capture
        import test_public_source_capture as psc

        profiles = fleet.load_profiles(fleet.DEFAULT_PROFILES)
        transport = public_source_capture.set_transport(
            psc.FakeTransport({psc.PUBLIC_URL: psc.SOURCE_BYTES})
        )
        self.addCleanup(lambda: public_source_capture.set_transport(transport))
        capture = psc.PublicSourceCaptureTests()
        capture.profiles = profiles
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        worktree = capture._git_worktree(root, psc.PUBLIC_PATH, psc.SOURCE_BYTES)
        public_manifest = capture._manifest(root / "public-source.json")
        bundle, manifest = capture._prepare(root, worktree, manifest=public_manifest)
        self.assertTrue(manifest.get("public_source"))
        admissions = fleet.verified_captured_public_source_read_admissions(bundle)
        self.assertEqual(len(admissions), 1)
        self.assertTrue(str(admissions[0]["relative"]).endswith("secret-store.ts"))
        self.assertEqual(len(admissions[0]["patterns"]), 1)
        self.assertNotIn("**/secrets/**", admissions[0]["patterns"])
        config = json.loads(
            fleet.opencode_runtime_config(verified_read_admissions=admissions)
        )
        self.assertEqual(config["permission"]["read"]["**/secrets/**"], "deny")
        self.assertEqual(
            config["permission"]["read"][admissions[0]["patterns"][0]],
            "allow",
        )
        gate_source = inspect.getsource(fleet.run_quality_gate)
        execute_source = inspect.getsource(fleet.execute_single)
        self.assertIn("quality_evidence_bundle=None if dry_run else bundle", gate_source)
        self.assertIn("verified_captured_public_source_read_admissions", execute_source)
        self.assertIn("quality_evidence_bundle", inspect.signature(fleet.execute_single).parameters)

    def test_caller_supplied_directory_waiver_is_rejected(self) -> None:
        with self.assertRaisesRegex(fleet.FleetError, "cannot waive a secret directory"):
            fleet.opencode_runtime_config(
                verified_read_admissions=[
                    {"gate_id": GATE_ID, "patterns": ["secrets/**"]}
                ]
            )


if __name__ == "__main__":
    unittest.main()
