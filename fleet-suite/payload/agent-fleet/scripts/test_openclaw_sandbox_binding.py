"""Offline tests use inert core transports, never create/delete Docker objects."""
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import openclaw_runtime as runtime
import sandbox_fleet_mcp as broker


class FakeCore:
    def __init__(self):
        self.calls = []
        self.child_env = None

    def create(self, session):
        self.calls.append(("create", session))
        self.child_env = dict(os.environ)
        return {"session": session}

    def exec_command(self, session, command, timeout):
        self.calls.append(("exec", session, command, timeout))
        return {"exit_code": 0, "stdout": "synthetic"}

    def write_file(self, session, path, content):
        self.calls.append(("write", session, path, content))
        return {"path": path}

    def read_file(self, session, path):
        self.calls.append(("read", session, path))
        return {"content": "synthetic"}

    def status(self, session):
        self.calls.append(("status", session))
        return {"session": session, "running": True}

    def container_name(self, session):
        return "sbx-" + session

    def _docker(self, argv, **kwargs):
        self.calls.append(("docker", argv, kwargs))


class SandboxBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.session = "fleet-" + "a" * 24
        self.assignment = runtime.build_fleet_sandbox_assignment(
            session=self.session, run_home=self.root / "fleet-sandbox-home")

    def config(self):
        return runtime.build_run_config(mode="implement", workspace=self.root,
                                        fleet_sandbox_assignment=self.assignment)

    def test_exact_four_tools_and_no_global_or_destroy_operations(self):
        self.assertEqual(set(broker.TOOL_NAMES), {"exec", "write_file", "read_file", "status"})
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        answer = broker._handle_request(request, session=self.session, core=FakeCore(),
            image_pin=(broker.IMAGE_NAME, broker.IMAGE_ID), ensured={"done": False}, run_home=self.root)
        self.assertEqual(set(x["name"] for x in answer["result"]["tools"]), set(broker.TOOL_NAMES))

    def test_argument_injections_and_wrong_shapes_fail_before_core(self):
        for key in ["session", "source", "python", "docker", "image", "home", "env", "mode", "config"]:
            with self.subTest(key=key), self.assertRaises(broker.BrokerError):
                broker._validate_arguments("exec", {"command": "echo ok", key: "override"})
        for command in [None, {}, [], [1], "", "a\x00b"]:
            with self.subTest(command=command), self.assertRaises(broker.BrokerError):
                broker._validate_arguments("exec", {"command": command})
        for timeout in [True, 0, 121, "30"]:
            with self.subTest(timeout=timeout), self.assertRaises(broker.BrokerError):
                broker._validate_arguments("exec", {"command": "echo ok", "timeout": timeout})

    def test_read_only_modes_reject_assignment_and_keep_empty_mcp(self):
        for mode in ["review", "advisory"]:
            with self.subTest(mode=mode):
                self.assertEqual(runtime.build_run_config(mode=mode, workspace=self.root)["mcp"], {"servers": {}})
                with self.assertRaises(runtime.OpenClawRuntimeError):
                    runtime.build_run_config(mode=mode, workspace=self.root, fleet_sandbox_assignment=self.assignment)

    def test_exact_implement_config_has_gui_python_and_denies_host_shell(self):
        cfg = self.config()
        runtime.validate_run_config_payload(cfg, mode="implement", fleet_sandbox_assignment=self.assignment)
        server = cfg["mcp"]["servers"]["fleet_sandbox"]
        self.assertTrue(server["command"].endswith("pythonw.exe"))
        self.assertEqual(server["args"][:2], ["-I", "-B"])
        self.assertEqual(server["toolFilter"]["include"], list(broker.TOOL_NAMES))
        self.assertEqual(cfg["tools"]["exec"], {"mode": "deny"})
        self.assertIn("exec", cfg["tools"]["deny"])
        self.assertIn("fleet_sandbox__exec", cfg["tools"]["allow"])

    def test_config_tampering_extra_server_tool_command_env_fails(self):
        mutations = [
            lambda c: c["mcp"]["servers"].update(other={}),
            lambda c: c["tools"]["allow"].append("fleet_sandbox__destroy"),
            lambda c: c["mcp"]["servers"]["fleet_sandbox"].update(command="cmd.exe"),
            lambda c: c["mcp"]["servers"]["fleet_sandbox"]["env"].update(API_KEY="fake"),
            lambda c: c["mcp"]["servers"]["fleet_sandbox"].update(cwd=str(self.root)),
        ]
        for mutation in mutations:
            c = self.config(); mutation(c)
            with self.assertRaises(runtime.OpenClawRuntimeError):
                runtime.validate_run_config_payload(c, mode="implement", fleet_sandbox_assignment=self.assignment)

    def test_assignment_tampering_and_owner_mismatch_fails(self):
        for key, value in [("unexpected", True), ("broker_python", "cmd.exe"), ("session", "fleet-" + "b"*24)]:
            c = copy.deepcopy(self.assignment); c[key] = value
            with self.assertRaises(runtime.OpenClawRuntimeError):
                runtime.validate_fleet_sandbox_assignment(c)
        c = copy.deepcopy(self.assignment); c["env"]["FLEET_SANDBOX_CORE_HASH"] = "0"*64
        with self.assertRaises(runtime.OpenClawRuntimeError):
            runtime.validate_fleet_sandbox_assignment(c)
        with self.assertRaises(broker.BrokerError):
            broker._session_only_for_run("fleet-" + "b"*24, Path(self.assignment["run_home"]))

    def test_self_pin_is_required_and_drift_refused(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(broker.BrokerError):
            broker._verify_self_hash()
        with mock.patch.dict(os.environ, {broker.ENV_BROKER_HASH: "0"*64}), self.assertRaises(broker.BrokerError):
            broker._verify_self_hash()

    def test_lazy_create_uses_only_owned_session(self):
        core = FakeCore(); ensured = {"done": False}
        handler = broker._make_handler(self.session, core, (broker.IMAGE_NAME, broker.IMAGE_ID), ensured, self.root)
        handler["exec"]({"command": "echo ok"}); handler["write_file"]({"path": "x", "content": "x"})
        self.assertEqual(handler["status"]({})["owner_session"], self.session)
        self.assertEqual(core.calls[0], ("create", self.session))
        self.assertEqual(sum(x[0] == "create" for x in core.calls), 1)
        self.assertTrue(all(x[1] == self.session for x in core.calls))

    def test_stdio_inert_transport_pins_env_and_stops_without_delete(self):
        core = FakeCore()
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "exec", "arguments": {"command": "echo ok"}}}]
        with mock.patch.dict(os.environ, {**self.assignment["env"], "SYNTHETIC_API_KEY": "not-a-real-key", "DOCKER_HOST": "tcp://example.invalid", "SYSTEMROOT": r"C:\Windows", "WINDIR": r"C:\Windows"}, clear=True), mock.patch.object(broker, "_load_core", return_value=core):
            out = io.StringIO(); broker.serve(io.StringIO("\n".join(json.dumps(m) for m in messages)+"\n"), out)
        self.assertNotIn("SYNTHETIC_API_KEY", core.child_env)
        self.assertEqual({key.casefold():value for key,value in core.child_env.items()}["systemroot"], r"C:\Windows")
        self.assertEqual(core.child_env["SANDBOX_TERMINAL_IMAGE"], broker.IMAGE_ID)
        self.assertEqual(core.child_env["DOCKER_HOST"], broker.DOCKER_HOST)
        self.assertEqual(core.child_env["SANDBOX_TERMINAL_HOME"], self.assignment["run_home"])
        self.assertEqual(core.calls[-1][0], "docker")
        self.assertEqual(core.calls[-1][1][:3], ["stop", "-t", "1"])
        self.assertFalse(json.loads(out.getvalue())["result"]["isError"])

    def test_materializer_thread_and_read_only_profile_gate(self):
        run = self.root / "run"; run.mkdir()
        f = runtime.materialize_run_config(mode="implement", workspace=self.root, run_dir=run, fleet_sandbox_enabled=True)
        self.assertIn("fleet_sandbox", json.loads(f.read_text())["mcp"]["servers"])
        other = self.root / "read-run"; other.mkdir()
        f = runtime.materialize_run_config(mode="review", workspace=self.root, run_dir=other, fleet_sandbox_enabled=True)
        self.assertEqual(json.loads(f.read_text())["mcp"], {"servers": {}})
        self.assertFalse((other / "fleet-sandbox-home").exists())

    def test_protocol_invalid_params_and_unknown_tool_is_error(self):
        for req in [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []},
                    {"jsonrpc": "2.0", "id": True, "method": "tools/list"}]:
            answer = broker._handle_request(req, session=self.session, core=FakeCore(), image_pin=(broker.IMAGE_NAME, broker.IMAGE_ID), ensured={"done": False}, run_home=self.root)
            self.assertIn("error", answer)
        answer = broker._handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "destroy", "arguments": {}}}, session=self.session, core=FakeCore(), image_pin=(broker.IMAGE_NAME, broker.IMAGE_ID), ensured={"done": False}, run_home=self.root)
        self.assertTrue(answer["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
