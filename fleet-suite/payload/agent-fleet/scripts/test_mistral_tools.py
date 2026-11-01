"""Offline tool qualification. Synthetic fixtures only; no auth, inference or cleanup."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import uuid
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fleet
import mistral_runtime as runtime

tools = runtime.tool_module()


class Fixture(unittest.TestCase):
    def setUp(self):
        # Retain synthetic evidence in OS temp, never in the immutable payload.
        # Existing payload/test-output artifacts belong to the parent's packaging
        # exclusion; do not delete or relocate them from this bounded test suite.
        artifacts = Path(tempfile.gettempdir()).resolve()
        if artifacts.is_relative_to(Path(__file__).resolve().parents[2]):
            raise RuntimeError("Synthetic test temp root must be outside the release payload")
        # Inherit the harness's temp ACL: Python 3.13 mkdtemp uses mode 0700 on
        # Windows, whose owner-only ACL prevents restricted-token child writes.
        self.root = artifacts / ("fleet-mistral-synthetic-" + uuid.uuid4().hex)
        self.root.mkdir(exist_ok=False)
        self.workspace = self.root / "workspace"
        self.run = self.root / "run"
        self.workspace.mkdir()
        self.run.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src/auth.py").write_text("answer = 42\n", encoding="utf-8", newline="\n")
        (self.workspace / "input.txt").write_text("synthetic input\n", encoding="utf-8", newline="\n")
        self.dispatch = {"scope": ["src"], "inputs": ["input.txt"],
                         "checks": [["python", "-B", "-m", "unittest", "discover", "-s", "src"]]}
        self.policy = tools.build_policy(workspace=self.workspace, run_dir=self.run,
                                         mode="implement", **self.dispatch)

    def refused(self, fn, *args, **kwargs):
        with self.assertRaises(tools.ToolPolicyError):
            fn(*args, **kwargs)

    def docker_available(self):
        original = Path.is_file
        return patch.object(Path, "is_file", lambda path: True if str(path).replace("\\", "/") == tools.DOCKER_EXECUTABLE else original(path))


class PathAndFileTests(Fixture):
    def test_windows_traversal_devices_ads_case_and_protected_paths(self):
        for name in ("../input.txt", "src/../input.txt", "C:/host.py", "//server/share",
                     "\\\\?\\C:\\host", "src/x.py:stream", "src/CON.py", "src/NUL",
                     "src/LPT1.txt", "src/x.py.", "src/ x.py", "src/x.py ", "src/X~1.py",
                     "src/.git/config", "src/.vibe/config.toml", "src/.agents/state",
                     "src/.env", "src/.env.production", "src/AUTH-stored.JSON",
                     "src/credentials.db", "src/private.key", "src/cookies.txt", "src/\x00x"):
            with self.subTest(name=name):
                self.refused(tools.resolve_admitted, self.policy, name)
        self.refused(tools.read_text, self.policy, "SRC/auth.py")
        self.refused(tools.read_text, self.policy, "src/Auth.py")
        self.assertEqual(tools.read_text(self.policy, "src/auth.py")["content"], "answer = 42\n")

    def test_inputs_read_only_and_no_unadmitted_read_or_write(self):
        self.assertIn("synthetic", tools.read_text(self.policy, "input.txt")["content"])
        self.refused(tools.write_text, self.policy, "input.txt", "changed", True)
        (self.workspace / "unadmitted.txt").write_text("OUTSIDE-DATA", encoding="utf-8")
        self.refused(tools.read_text, self.policy, "unadmitted.txt")
        self.refused(tools.write_text, self.policy, "unadmitted.txt", "changed", True)

    def test_create_update_utf8_explicit_overwrite_and_text_bounds(self):
        result = tools.write_text(self.policy, "src/nested/new.py", "# Łódź\n")
        self.assertTrue(result["created"])
        self.refused(tools.write_text, self.policy, "src/nested/new.py", "other")
        result = tools.write_text(self.policy, "src/nested/new.py", "# żółć\n", True)
        self.assertFalse(result["created"])
        self.assertEqual(tools.read_text(self.policy, "src/nested/new.py")["content"], "# żółć\n")
        self.refused(tools.write_text, self.policy, "src/large.py", "x" * (self.policy["limits"]["max_write_bytes"] + 1))
        self.refused(tools.write_text, self.policy, "src/nul.py", "\0")
        (self.workspace / "src/binary.py").write_bytes(b"\xff\x00")
        self.refused(tools.write_text, self.policy, "src/binary.py", "new", True)
        self.assertEqual((self.workspace / "src/binary.py").read_bytes(), b"\xff\x00")

    def test_read_window_and_multibyte_output_are_bounded(self):
        tools.write_text(self.policy, "src/lines.py", "first\n" + "ż" * 80 + "\nlast\n")
        self.policy["limits"]["max_read_bytes"] = 15
        result = tools.read_text(self.policy, "src/lines.py", 1, 1)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["content"].encode("utf-8")), 15)
        self.refused(tools.read_text, self.policy, "src/lines.py", True)

    def test_read_modes_refuse_writes_and_check_execution(self):
        for mode in ("advisory", "review"):
            policy = dict(self.policy, mode=mode, tools=list(tools.READ_ONLY_TOOLS))
            policy = tools.validate_policy(policy)
            self.refused(tools.write_text, policy, "src/new.py", "new")
            self.refused(tools.run_check, policy, 0)
            state = tools.RunState(policy)
            self.refused(tools.invoke, state, tools.TOOL_WRITE, tools.write_text, "src/new.py", "new")

    def test_hardlinks_are_never_read_updated_listed_or_snapshotted(self):
        os.link(self.workspace / "input.txt", self.workspace / "src/linked.py")
        self.refused(tools.read_text, self.policy, "src/linked.py")
        self.refused(tools.write_text, self.policy, "src/linked.py", "changed", True)
        self.assertNotIn("src/linked.py", tools.list_files(self.policy)["entries"])
        destination = self.run / "snapshot"
        tools.materialize_snapshot(self.policy, destination)
        self.assertFalse((destination / "src/linked.py").exists())

    def test_links_and_reparse_points_are_refused(self):
        source = self.workspace / "src/auth.py"
        real = os.lstat
        def reparse(path, *args, **kwargs):
            info = real(path, *args, **kwargs)
            if Path(path) == source:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(tools.os, "lstat", side_effect=reparse):
            self.refused(tools.read_text, self.policy, "src/auth.py")
            self.refused(tools.write_text, self.policy, "src/auth.py", "new", True)
        link = self.workspace / "src/link"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError:
            return  # Reparse refusal above executes even without Windows symlink privilege.
        self.refused(tools.read_text, self.policy, "src/link/workspace/input.txt")
        self.refused(tools.write_text, self.policy, "src/link/new.txt", "new")

    def test_open_identity_race_does_not_truncate(self):
        target = self.workspace / "src/auth.py"
        real = tools.os.fstat
        def switched(fd):
            info = real(fd)
            return SimpleNamespace(st_ino=info.st_ino + 1, st_dev=info.st_dev, st_nlink=1)
        with patch.object(tools.os, "fstat", side_effect=switched):
            self.refused(tools.write_text, self.policy, "src/auth.py", "new", True)
        self.assertEqual(target.read_text(), "answer = 42\n")

    def test_listing_search_and_snapshot_exclude_protected_and_unadmitted_files(self):
        for relative in ("src/auth-store.json", "src/.env", "unadmitted.txt"):
            (self.workspace / relative).write_text("SYNTHETIC-PROTECTED", encoding="utf-8")
        listing = tools.list_files(self.policy)
        self.assertEqual(set(listing["entries"]), {"src/auth.py", "input.txt"})
        self.assertEqual(tools.list_files(self.policy, query="SYNTHETIC-PROTECTED")["matches"], [])
        self.assertIn("src/auth.py:1:", tools.list_files(self.policy, query="answer")["matches"][0])
        destination = self.run / "snapshot"
        tools.materialize_snapshot(self.policy, destination)
        self.assertEqual(set(str(p.relative_to(destination)).replace("\\", "/") for p in destination.rglob("*") if p.is_file()),
                         {"src/auth.py", "input.txt"})

    def test_listing_output_and_snapshot_count_and_bytes_are_bounded(self):
        self.policy["limits"]["max_read_bytes"] = 10
        self.assertTrue(tools.list_files(self.policy)["truncated"])
        self.policy["limits"]["snapshot_max_files"] = 1
        self.refused(tools.materialize_snapshot, self.policy, self.run / "count-snapshot")
        self.policy["limits"]["snapshot_max_files"] = 100
        self.policy["limits"]["snapshot_max_bytes"] = 1
        self.refused(tools.materialize_snapshot, self.policy, self.run / "byte-snapshot")


class PolicyAndTelemetryTests(Fixture):
    def test_policy_requires_external_existing_run_and_exact_pins(self):
        for change in ({"mode": "unsafe"}, {"runtime": {}}, {"tools": ["shell"]}, {"scope": []},
                       {"run_dir": str(self.workspace)},
                       {"scope": ["src", "SRC"]}):
            self.refused(tools.validate_policy, dict(self.policy, **change))
        limits = dict(self.policy["limits"], max_tool_calls=True)
        self.refused(tools.validate_policy, dict(self.policy, limits=limits))
        self.refused(tools.validate_workspace, self.workspace.anchor)
        self.refused(tools.validate_workspace, str(Path.home()))
        self.refused(tools.validate_workspace, "\\\\server\\share")

    def test_policy_is_exclusive_and_loaded_from_bound_run(self):
        path = tools.write_policy(self.policy)
        self.assertEqual(tools.load_policy(path), self.policy)
        with self.assertRaises(FileExistsError):
            tools.write_policy(self.policy)
        alternate = self.root / tools.POLICY_FILENAME
        alternate.write_text(path.read_text(), encoding="utf-8")
        self.refused(tools.load_policy, alternate)

    def test_metadata_only_telemetry_and_actual_tool_budgets(self):
        telemetry = self.run / tools.TELEMETRY_FILENAME
        telemetry.touch()
        self.policy["limits"]["max_tool_calls"] = 3
        self.policy["limits"]["max_writes"] = 1
        state = tools.RunState(self.policy, telemetry=telemetry)
        tools.invoke(state, tools.TOOL_WRITE, tools.write_text, "src/new.py", "SYNTHETIC-CONTENT")
        self.refused(tools.invoke, state, tools.TOOL_WRITE, tools.write_text, "src/other.py", "new")
        tools.invoke(state, tools.TOOL_READ, tools.read_text, "src/new.py")
        self.refused(tools.invoke, state, tools.TOOL_READ, tools.read_text, "src/new.py")
        raw = telemetry.read_text()
        self.assertNotIn("SYNTHETIC-CONTENT", raw)
        self.assertNotIn("src/new.py", raw)
        summary = tools.read_telemetry(self.run)
        self.assertEqual(summary["calls"], 4)
        self.assertTrue(summary["budget_exceeded"])
        self.assertEqual(summary["written_bytes"], len("SYNTHETIC-CONTENT"))
        self.assertEqual(summary["outcomes"], {"ok": 2, "refused": 2})

    def test_unexpected_errors_do_not_disclose_exception_details(self):
        state = tools.RunState(self.policy)
        def fail(*args, **kwargs):
            raise OSError("SYNTHETIC-PRIVATE-DATA")
        with self.assertRaises(tools.ToolPolicyError) as raised:
            tools.invoke(state, tools.TOOL_READ, fail)
        self.assertNotIn("SYNTHETIC-PRIVATE", str(raised.exception))


class SandboxTests(Fixture):
    def test_complete_docker_argv_is_pinned_offline_readonly_unprivileged(self):
        snapshot = self.run / tools.SNAPSHOT_DIRNAME / "fresh"
        argv = tools.docker_argv(self.policy, snapshot, ["python", "-B", "-m", "unittest"], "synthetic-check")
        self.assertEqual(argv[0], tools.DOCKER_EXECUTABLE)
        for flag in ("--rm", "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges", "--user=65534:65534", "--pids-limit=256",
                     "--memory=2048m", "--memory-swap=2048m", "--cpus=2"):
            self.assertIn(flag, argv)
        self.assertEqual(argv.count("--mount"), 1)
        mount = argv[argv.index("--mount") + 1]
        self.assertTrue(mount.endswith("target=/workspace,readonly"))
        self.assertNotIn(str(self.workspace), mount)
        self.assertEqual(argv[1:5], ["--config", str(self.run / "mistral-docker-config"),
                                     "--host", "npipe:////./pipe/dockerDesktopLinuxEngine"])
        self.assertIn("/tmp:rw,noexec,nosuid,nodev,size=64m", argv)
        self.assertEqual(argv[argv.index(tools.DOCKER_IMAGE) + 1:], ["python", "-B", "-m", "unittest"])
        for flag in ("--privileged", "--device", "--env", "--volume"):
            self.assertNotIn(flag, argv)
        self.refused(tools.docker_argv, self.policy, self.workspace, ["python"], "synthetic-check")

    def test_declared_argv_conversion_supports_windows_and_spaces_without_shell(self):
        host = "C:\\Users\\example\\AppData\\Local\\Programs\\Python\\Python313\\python.exe"
        check = [host, "-B", "-m", "unittest", "discover", "-s", str(self.workspace / "src")]
        argv = tools.convert_check_argv(self.policy, check)
        self.assertEqual(argv, ["python", "-B", "-m", "unittest", "discover", "-s", "/workspace/src"])
        self.assertEqual(tools.convert_check_argv(self.policy, [host, "src/auth.py"]), ["python", "-B", "src/auth.py"])
        self.assertIn("no:cacheprovider", tools.convert_check_argv(self.policy, [host, "-m", "pytest", "src"]))
        self.assertEqual(tools.convert_check_argv(self.policy, [host, "-c", "assert 1 == 1"]),
                         ["python", "-B", "-c", "assert 1 == 1"])

    def test_unsupported_outside_and_incomplete_checks_refuse_without_host_fallback(self):
        for argv in (["cmd", "/c", "echo hi"], ["python", "-m", "pip", "install", "x"],
                     ["python", "-c", "open('C:/host.txt')"], ["python", "../x.py"],
                     ["python", "unadmitted.py"], ["python", "-m", "unittest", str(self.root / "host.py")],
                     ["python"], ["python", "-i", "src/auth.py"]):
            self.refused(tools.convert_check_argv, self.policy, argv)
        for check_id in (-1, True, 1, "0"):
            self.refused(tools.run_check, self.policy, check_id)
        with patch.object(Path, "is_file", return_value=False), patch.object(tools, "_run_bounded") as child:
            self.refused(tools.run_check, self.policy, 0)
            child.assert_not_called()

    def test_check_dispatch_snapshot_budget_exit_and_output_limits(self):
        calls = []
        def fixture_runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 7, stdout=b"x" * 200)
        self.policy["limits"]["max_check_output_bytes"] = 32
        self.policy["limits"]["max_check_runs"] = 1
        state = tools.RunState(self.policy)
        with self.docker_available():
            result = tools.run_check(self.policy, 0, state=state, runner=fixture_runner)
            self.refused(tools.run_check, self.policy, 0, state=state, runner=fixture_runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 7)
        self.assertTrue(result["output_truncated"])
        self.assertLessEqual(len(result["output"].encode()), 32)
        self.assertEqual(len(calls), 1)
        self.assertIn(tools.DOCKER_IMAGE, calls[0][0])
        self.assertEqual(calls[0][1]["output_limit"], 32)

    def test_timeout_kills_only_own_container(self):
        calls = []
        def timed_out(argv, **kwargs):
            calls.append(argv)
            if "run" in argv:
                raise subprocess.TimeoutExpired(argv, 1)
            return subprocess.CompletedProcess(argv, 0, stdout=b"")
        with self.docker_available():
            result = tools.run_check(self.policy, 0, runner=timed_out)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(calls[1], [*calls[0][:5], "kill", calls[0][calls[0].index("--name") + 1]])

    def test_bounded_reader_hides_console_and_drops_ambient_docker_config(self):
        child = SimpleNamespace(stdout=io.BytesIO(b"x" * 1_000_000), returncode=0, wait=lambda **kwargs: None)
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://outside", "DOCKER_CONFIG": "untrusted",
                                    "MISTRAL_API_KEY": "SYNTHETIC"}), \
                patch.object(tools.subprocess, "Popen", return_value=child) as popen:
            result = tools._run_bounded([tools.DOCKER_EXECUTABLE, "version"], timeout=1, output_limit=128)
        self.assertLessEqual(len(result.stdout), 129)
        self.assertNotIn("DOCKER_HOST", popen.call_args.kwargs["env"])
        self.assertNotIn("MISTRAL_API_KEY", popen.call_args.kwargs["env"])
        self.assertEqual(popen.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))


class FleetBindingTests(Fixture):
    def test_requested_fleet_tool_cap_is_registered_bound_and_enforced(self):
        self.assertIn("mistral", fleet.TOOL_BUDGET_ENFORCING_ADAPTERS)
        _, limit = fleet.effective_run_limits("mistral", [], complexity="low",
            max_prompt_bytes=None, max_model_tool_calls=2, context_window_tokens=None)
        self.assertEqual(limit, 2)
        policy = runtime.bind_tool_policy("synthetic", workspace=self.workspace, run_dir=self.run,
            mode="implement", dispatch=self.dispatch, max_tool_calls=limit)
        self.assertEqual(policy["limits"]["max_tool_calls"], 2)
        state = tools.RunState(policy)
        tools.invoke(state, tools.TOOL_READ, tools.read_text, "input.txt")
        tools.invoke(state, tools.TOOL_READ, tools.read_text, "input.txt")
        self.refused(tools.invoke, state, tools.TOOL_READ, tools.read_text, "input.txt")

    def test_default_fleet_state_worktree_and_project_subdirectory_are_admitted(self):
        home = self.root / "synthetic-home"
        home.mkdir()
        run_id = "run-20261011T010000Z-0123456789"
        with patch.object(Path, "home", return_value=home), patch.dict(os.environ, {}, clear=True):
            run = fleet.create_run_dir(None, run_id, state_root=fleet.state_root(None))
            checkout = run / "worktree"
            (checkout / "src").mkdir(parents=True)
            (checkout / "src/auth.py").write_text("answer = 42\n", encoding="utf-8", newline="\n")
            dispatch = dict(self.dispatch, inputs=[])
            policy = runtime.bind_tool_policy(json.dumps(dispatch), workspace=checkout, run_dir=run,
                                             mode="implement", dispatch=dispatch)
            self.assertEqual(policy["workspace"], str(checkout))
            self.assertEqual(tools.read_text(policy, "src/auth.py")["content"], "answer = 42\n")
            tools.write_text(policy, "src/new.py", "# synthetic UTF8: Łódź\n")
            self.refused(tools.resolve_admitted, policy, "src/.agents/private.txt")
            self.refused(tools.resolve_admitted, policy, "src/.git/config")
            # prepare_execution_workspace preserves a caller's project subdirectory.
            nested = tools.build_policy(workspace=checkout / "src", run_dir=run, mode="implement",
                                        scope=["auth.py"], inputs=[], checks=[])
            self.assertEqual(tools.read_text(nested, "auth.py")["content"], "answer = 42\n")
            # A retained Fleet worktree remains usable by a new retry run.
            retry = fleet.create_run_dir(None, "run-20261011T010001Z-0123456789", state_root=fleet.state_root(None))
            retained = tools.build_policy(workspace=checkout, run_dir=retry, mode="implement", **dispatch)
            self.assertEqual(retained["workspace"], str(checkout))
            # Snapshot the admitted files, not the protected parent agent store.
            snapshot = run / "snapshot"
            tools.materialize_snapshot(policy, snapshot)
            self.assertEqual(set(p.relative_to(snapshot).as_posix() for p in snapshot.rglob("*") if p.is_file()),
                             {"src/auth.py", "src/new.py"})

    def test_default_state_exception_does_not_admit_other_agent_directories_or_links(self):
        home = self.root / "synthetic-home"
        home.mkdir()
        evidence = home / ".agents/agent-fleet/evidence"
        run_id = "run-20261011T010000Z-0123456789"
        unsafe = [home / ".agents", home / ".agents/agent-fleet", evidence, evidence / run_id,
                  evidence / "invalid-run/worktree", evidence / run_id / "other",
                  evidence / run_id / "worktree/.vibe", evidence / run_id / "worktree/.agents",
                  home / ".agents/runtime/worktree", home / ".agents/skills/worktree",
                  home / ".codex/agent-fleet/evidence" / run_id / "worktree",
                  self.root / "other-home/.agents/agent-fleet/evidence" / run_id / "worktree"]
        for path in unsafe:
            path.mkdir(parents=True, exist_ok=True)
        checkout = evidence / run_id / "worktree"
        with patch.object(Path, "home", return_value=home):
            for path in unsafe:
                with self.subTest(path=path):
                    self.refused(tools.validate_workspace, str(path))
            real = os.lstat
            def linked_parent(path, *args, **kwargs):
                info = real(path, *args, **kwargs)
                if Path(path) == evidence:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            with patch.object(tools.os, "lstat", side_effect=linked_parent):
                self.refused(tools.validate_workspace, str(checkout))

    def test_prompt_dispatch_binding_and_mismatch_refusal(self):
        text = "TODO\n" + json.dumps(dict(self.dispatch, task="synthetic fix"))
        policy = runtime.bind_tool_policy(text, workspace=self.workspace, run_dir=self.run,
                                           mode="implement", dispatch=self.dispatch)
        self.assertEqual(policy, self.policy)
        for dispatch in (dict(self.dispatch, scope=["input.txt"]), dict(self.dispatch, checks=[])):
            with self.assertRaises(runtime.MistralRuntimeError):
                runtime.bind_tool_policy(text, workspace=self.workspace, run_dir=self.run,
                                         mode="implement", dispatch=dispatch)
        with self.assertRaises(runtime.MistralRuntimeError):
            runtime.bind_tool_policy("no dispatch", workspace=self.workspace, run_dir=self.run, mode="implement")
        self.assertIsNone(runtime.bind_tool_policy("prompt-only", workspace=self.workspace, run_dir=self.run, mode="review"))

    def test_policy_supports_actual_fleet_worktree_below_run_directory(self):
        checkout = self.run / "worktree"
        checkout.mkdir()
        policy = tools.build_policy(workspace=checkout, run_dir=self.run, mode="implement", **self.dispatch)
        self.assertEqual(policy["workspace"], str(checkout))

    def test_explicit_candidate_uat_preserves_routing_and_other_launch_gates(self):
        import inspect
        profile = fleet.load_profiles(fleet.DEFAULT_PROFILES)["mistral"]
        kwargs = dict(candidate_uat=True, dispatch=self.dispatch, knowledge_mode="none", runtime_profile="standard")
        for enabled in (False, True):
            fixture = copy.deepcopy(profile)
            fixture["routing_enabled"] = enabled
            original = copy.deepcopy(fixture)
            with self.subTest(routing_enabled=enabled):
                self.assertTrue(fleet.mistral_candidate_uat_gate(fixture, **kwargs))
                self.assertFalse(fleet.mistral_candidate_uat_gate(fixture, **dict(kwargs, candidate_uat=False)))
                self.assertEqual(fixture, original)
        self.assertEqual(inspect.signature(fleet.execute_single).parameters["mistral_candidate_uat"].default, False)
        for change in ({"knowledge_mode": "snapshot"}, {"dispatch": None}, {"candidate_uat": 1},
                       {"runtime_profile": "l2-thin"}):
            with self.subTest(change=change), self.assertRaisesRegex(fleet.FleetError, "candidate UAT requires"):
                fleet.mistral_candidate_uat_gate(profile, **dict(kwargs, **change))
        with self.assertRaises(fleet.FleetError):
            fleet.mistral_candidate_uat_gate({"adapter": "codex"}, **kwargs)

    def test_bridge_bounded_check_objects_and_incomplete_argv(self):
        payload = dict(self.dispatch, checks=[{"status": "included", "argv": self.dispatch["checks"][0], "omitted_args": 0}])
        policy = runtime.bind_tool_policy(json.dumps(payload), workspace=self.workspace, run_dir=self.run, mode="implement")
        self.assertEqual(policy["checks"], self.dispatch["checks"])
        payload["checks"][0]["omitted_args"] = 1
        with self.assertRaises(runtime.MistralRuntimeError):
            runtime.bind_tool_policy(json.dumps(payload), workspace=self.workspace, run_dir=self.run, mode="implement")

    def test_fleet_command_preparation_and_policy_export_acceptance(self):
        profile = fleet.load_profiles(fleet.DEFAULT_PROFILES)["mistral"]
        kwargs = dict(prompt="synthetic", workspace=self.workspace, mode="implement", model="mistral-large-4",
                      complexity="high", timeout=120, run_dir=self.run, native_subagent_limit=0,
                      enable_web=False, profile=profile, mistral_tool_policy=self.policy)
        home = runtime.prepare_mistral_run(self.run, tool_policy=self.policy)
        command, _, _ = fleet.build_command("mistral", runtime.MISTRAL_NATIVE_EXECUTABLE, **kwargs)
        self.assertEqual(home.parent, self.run / "mistral-workspace")
        self.assertTrue((self.run / tools.POLICY_FILENAME).is_file())
        self.assertEqual(command[command.index("--agent") + 1], "accept-edits")
        self.assertNotIn("--yolo", command)
        self.assertNotIn(str(self.workspace), command)
        self.assertIn(str(self.run / tools.POLICY_FILENAME), command)
        policy_index = command.index(str(self.run / tools.POLICY_FILENAME))
        self.assertEqual(command[policy_index + 1],
                         hashlib.sha256((self.run / tools.POLICY_FILENAME).read_bytes()).hexdigest())
        config = runtime.managed_config(self.policy)
        runtime.validate_native_config(config, tool_policy=self.policy)
        with self.assertRaises(runtime.MistralRuntimeError):
            runtime.validate_native_config(config)
        from test_mistral_runtime import export, history, HANDOFF
        value = export(config=config)
        self.assertEqual(fleet.project_provider_handoff("mistral", history(), native_export=value,
                                                       mistral_tool_policy=self.policy)["handoff"], HANDOFF)
        self.assertIsNone(fleet.project_provider_handoff("mistral", history(), native_export=value)["handoff"])
        self.assertEqual(runtime.mistral_model_identity(value, tool_policy=self.policy)["status"], "unverified")

    def test_config_schema_scope_and_sdk_identity_stay_fail_closed(self):
        for mode in ("review", "implement"):
            policy = dict(self.policy, mode=mode, tools=tools.tools_for(mode, self.policy["checks"]))
            config = runtime.managed_config(policy)
            for field, value in (("enabled_tools", ["bash"]), ("tool_paths", ["outside.py"]),
                                 ("disabled_tools", ["*"]), ("mcp_servers", [{"name": "outside"}])):
                with self.subTest(mode=mode, field=field), self.assertRaises(runtime.MistralRuntimeError):
                    runtime.validate_native_config(dict(config, **{field: value}), tool_policy=policy)

    def test_profile_eventual_qualified_K_needs_real_evidence_fields(self):
        profile = fleet.load_profiles(fleet.DEFAULT_PROFILES)["mistral"]
        persisted = copy.deepcopy(profile)
        profile["routing_enabled"] = True
        profile["routing_blocker"] = None
        # Exercise an explicitly unqualified fixture even after release activation.
        profile["qualification"] = {"status": "pending"}
        with self.assertRaises(runtime.MistralRuntimeError):
            runtime.validate_mistral_profile(profile)
        profile["qualification"].update(status="qualified")
        for gate in ("native_uat", "security", "fleet_uat", "tier_routing"):
            profile["qualification"][gate] = {"status": "PASS", "ref": "synthetic-test-fixture", "sha256": "a" * 64}
        runtime.validate_mistral_profile(profile)
        for gate in ("native_uat", "security", "fleet_uat", "tier_routing"):
            incomplete = copy.deepcopy(profile)
            incomplete["qualification"].pop(gate)
            with self.subTest(missing_gate=gate), self.assertRaises(runtime.MistralRuntimeError):
                runtime.validate_mistral_profile(incomplete)
        # This test fixture demonstrates admissibility; it is never written to the profile.
        self.assertEqual(fleet.load_profiles(fleet.DEFAULT_PROFILES)["mistral"], persisted)

    def test_fleet_resolves_maker_lane_with_its_actual_required_write_capability(self):
        profile = fleet.load_profiles(fleet.DEFAULT_PROFILES)["mistral"]
        original = copy.deepcopy(profile)
        execution = fleet.routing_v2.resolve_execution_profile(
            "mistral", profile, complexity="high", mode="implement", required_tools=["write"], model_override=None)
        self.assertEqual(execution["execution_profile_id"], "mistral-large-4-maker")
        self.assertEqual(execution["model"], "mistral-large-4")
        self.assertEqual(execution["model_tier"], "K")
        self.assertEqual(profile, original)


class NativeToolCompatibilityTests(Fixture):
    def test_actual_bootstrap_preflight_with_managed_home_and_no_cli_inference(self):
        if not Path(runtime.MISTRAL_NATIVE_PYTHON).is_file():
            self.skipTest("pinned native interpreter unavailable")
        home = runtime.prepare_mistral_run(self.run, tool_policy=self.policy)
        script = r'''
import ast, asyncio, socket, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import mistral_runtime as boundary
tree = ast.parse(boundary._RUN_BOOTSTRAP)
# Omit only the real CLI entrypoint import and invocation, retaining its exact preflight.
assert isinstance(tree.body[-2], ast.ImportFrom) and tree.body[-2].module == 'vibe.cli.entrypoint'
tree.body = tree.body[:-2]
sys.argv = ['vibe', str(Path(boundary.__file__).resolve()), sys.argv[2], sys.argv[3]]
def offline_run(coroutine):
    with asyncio.Runner() as runner:
        runner.get_loop()  # Windows self-pipe is initialized before the external network guard.
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('external network forbidden')):
            return runner.run(coroutine)
asyncio.run = offline_run
exec(compile(tree, '<actual-native-preflight>', 'exec'))
print('actual native bootstrap preflight PASS')
'''
        child = subprocess.run([runtime.MISTRAL_NATIVE_PYTHON, "-I", "-B", "-c", script,
                                str(Path(__file__).resolve().parent), str(home.parent), str(self.run / tools.POLICY_FILENAME)],
                               capture_output=True, timeout=40, check=False,
                               env=runtime.harden_mistral_environment(dict(os.environ), run_home=home),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(child.returncode, 0, child.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(child.stdout.decode("utf-8").strip(), "actual native bootstrap preflight PASS")

    def test_pinned_discovery_effective_schema_permissions_and_tool_calls_offline(self):
        if not Path(runtime.MISTRAL_NATIVE_PYTHON).is_file():
            self.skipTest("pinned native interpreter unavailable")
        tools.write_policy(self.policy)
        script = r'''
import asyncio, copy, json, socket, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import mistral_runtime as boundary
from vibe import __version__
assert __version__ == boundary.MISTRAL_NATIVE_VERSION
module = boundary.tool_module()
assert module._NATIVE, 'native API imports unavailable'
policy = module.activate(sys.argv[2])
boundary.install_tool_discovery_guard(module)
from vibe.core.config.harness_files import init_harness_files_manager
from vibe.core.config.builder import ConfigBuilder
from vibe.core.config.vibe_schema import VibeConfigSchema
from vibe.core.config.layers.default import DefaultConfigLayer
from vibe.core.config.layers.overrides import OverridesLayer
from vibe.core.config.layers.agent_profile import AgentProfileLayer
from vibe.core.agents.models import ACCEPT_EDITS
from vibe.core.tools.manager import ToolManager
from vibe.core.types import AvailableTool
from types import SimpleNamespace
init_harness_files_manager()
async def check():
    builder = ConfigBuilder(VibeConfigSchema)
    builder.add_layers([DefaultConfigLayer(schema=VibeConfigSchema), OverridesLayer(data=boundary.managed_config(policy))])
    builder.add_layer(AgentProfileLayer(data=ACCEPT_EDITS.overrides))
    config = await builder.build()
    boundary.validate_native_config(config.model_dump(mode='json'), tool_policy=policy)
    manager = ToolManager(lambda: config, defer_mcp=True)
    assert set(manager.available_tools) == set(policy['tools'])
    assert set(manager.registered_tools) == set(module.MAKER_TOOLS) | set(module.CHROME_TOOLS)
    schemas = boundary.native_tool_schemas(config, policy)
    assert {r['function']['name'] for r in schemas} == set(policy['tools'])
    for row in schemas:
        assert row['function']['parameters']['additionalProperties'] is False
    try: ToolManager._load_tools_from_file(Path(sys.argv[1]) / 'fleet.py')
    except boundary.MistralRuntimeError: pass
    else: raise AssertionError('foreign native module admitted')
    writer = manager.get(module.TOOL_WRITE)
    args = module.FleetWriteArgs(path='src/native.py', content='# synthetic native UTF8: Łódź\n')
    assert writer.resolve_permission(args).permission.value == 'always'
    results = [r async for r in writer.run(args)]
    assert results[0].created
    reader = manager.get(module.TOOL_READ)
    read = [r async for r in reader.run(module.FleetReadArgs(path='src/native.py'))]
    assert 'Łódź' in read[0].content
    # Verify exact SDK request schemas with a network-forbidden fake backend.
    calls = []
    class Backend:
        _server_url = 'https://api.mistral.ai'
        async def complete(self, **kw): raise AssertionError('nonstreaming called')
        async def complete_streaming(self, **kw):
            calls.append(True)
            yield 'native-fixture-chunk'
    class Events:
        async def __anext__(self): raise StopAsyncIteration
    with patch.dict(sys.modules, {'vibe.core.llm.backend.mistral': SimpleNamespace(MistralBackend=Backend),
                                 'mistralai.client.utils.eventstreaming': SimpleNamespace(EventStreamAsync=Events)}):
        boundary.install_model_observer(Path(policy['run_dir']) / 'mistral-model-witness.json', tool_schemas=schemas)
        available = [AvailableTool.model_validate(row) for row in schemas]
        request = dict(model=SimpleNamespace(name='mistral-large-4'), tools=available)
        assert [r async for r in Backend().complete_streaming(**request)] == ['native-fixture-chunk']
        for changed in ([], available[:-1], available + available[:1]):
            try:
                async for r in Backend().complete_streaming(**dict(request, tools=changed)): pass
            except boundary.MistralRuntimeError: pass
            else: raise AssertionError('inexact schema admitted')
        bad = copy.deepcopy(available)
        bad[0].function.parameters['properties']['outside'] = {'type':'string'}
        try:
            async for r in Backend().complete_streaming(**dict(request, tools=bad)): pass
        except boundary.MistralRuntimeError: pass
        else: raise AssertionError('modified schema admitted')
        assert len(calls) == 1
    for mode in ('review', 'advisory'):
        readonly = dict(policy, mode=mode, tools=list(module.READ_ONLY_TOOLS))
        module._STATE = module.RunState(readonly)
        cfg = VibeConfigSchema.model_validate(boundary.managed_config(readonly))
        assert set(ToolManager(lambda: cfg, defer_mcp=True).available_tools) == set(module.READ_ONLY_TOOLS)
        assert writer.resolve_permission(args).permission.value == 'never'
    print('native discovery/schema/file/SDK guards PASS')
async def offline():
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
        await check()
asyncio.run(offline())
'''
        child = subprocess.run([runtime.MISTRAL_NATIVE_PYTHON, "-I", "-B", "-c", script,
                                str(Path(__file__).resolve().parent), str(self.run / tools.POLICY_FILENAME)],
                               capture_output=True, timeout=40, check=False,
                               env=runtime.harden_mistral_environment(dict(os.environ)),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(child.returncode, 0, child.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(child.stdout.decode().strip(), "native discovery/schema/file/SDK guards PASS")


if __name__ == "__main__":
    unittest.main()
