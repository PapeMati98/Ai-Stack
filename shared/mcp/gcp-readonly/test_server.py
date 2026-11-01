from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import server


ROOT = Path(__file__).resolve().parent
SERVER_PATH = ROOT / "server.py"
SECRET = "ya29.SECRETBLOCK"
MALICIOUS_REGIONS = [
    "us-central1;calc",
    "us-central1 && whoami",
    "../us-central1",
    "https://attacker.example.invalid/us-central1",
    "--region",
    "us-central1\n--impersonate-service-account=x",
    "*",
    "",
    "US-CENTRAL1",
    "us-central1/../evil",
]
MALICIOUS_SERVICES = ["api;rm", "--help", "https://x", "../api", "Api", "bad--name", "a b"]


class FakeBackend:
    def __init__(self, profile: server.Profile, mode: str = "ok", describe=None, auth=None):
        self.calls = []
        self.mode = mode
        self.profile = profile
        self.describe = describe
        self.auth = auth

    def run(self, argv, env, timeout, max_output):
        self.calls.append(
            {"argv": list(argv), "env": dict(env), "timeout": timeout, "max_output": max_output}
        )
        if self.mode == "raise":
            raise RuntimeError(f"{SECRET} private_key=abc access_token=abc")
        if self.mode == "malformed":
            return f"not-json {SECRET}"
        if self.mode == "oversize":
            return ("A" * (max_output + 20)) + SECRET
        command = argv[argv.index("--quiet") + 1 :]
        if command[:3] == ["config", "configurations", "describe"]:
            body = self.describe or {
                "name": self.profile.configuration,
                "properties": {
                    "core": {
                        "account": self.profile.account,
                        "project": self.profile.project_id,
                        "access_token": SECRET,
                    }
                },
            }
        elif command[:2] == ["auth", "list"]:
            body = self.auth or [{"account": self.profile.account, "status": "ACTIVE", "token": SECRET}]
        elif command[:2] == ["projects", "describe"]:
            body = {
                "projectId": self.profile.project_id,
                "projectNumber": "123456789012",
                "name": "Example Pilot",
                "lifecycleState": "ACTIVE",
                "labels": {"secret": SECRET},
                "environment": {"GOOGLE_APPLICATION_CREDENTIALS": r"C:\keys\sa.json"},
            }
        elif command[:3] == ["run", "services", "list"]:
            body = [
                {
                    "metadata": {"name": "api", "annotations": {"token": SECRET}},
                    "spec": {"template": {"spec": {"containers": [{"env": [{"value": SECRET}]}]}}},
                    "status": {
                        "url": "https://api.example.invalid",
                        "conditions": [{"type": "Ready", "status": "True", "message": SECRET}],
                    },
                }
            ]
        elif command[:3] == ["run", "services", "describe"]:
            body = {
                "metadata": {"name": command[3], "annotations": {"access_token": SECRET}},
                "spec": {"env": SECRET},
                "status": {
                    "latestReadyRevisionName": "api-00001",
                    "url": "https://api.example.invalid",
                    "conditions": [{"type": "Ready", "status": "True", "message": "private_key"}],
                },
            }
        elif command[:2] == ["services", "list"]:
            body = [
                {
                    "config": {
                        "name": "run.googleapis.com",
                        "title": "Cloud Run Admin API",
                        "authentication": {"secret": SECRET},
                    }
                }
            ]
        else:
            raise AssertionError(command)
        return json.dumps(body)


def policy_text(cloudsdk: Path, python_path: Path, entrypoint: Path, **overrides: str) -> str:
    profile = {
        "account": "reader@example.invalid",
        "project_id": "example-readonly-pilot",
        "gcloud_configuration": "gcp-readonly-example",
        "cloudsdk_config": str(cloudsdk),
        "python_executable": str(python_path),
        "gcloud_python_entrypoint": str(entrypoint),
    }
    profile.update(overrides)
    return json.dumps({"profiles": {"example": profile}})


def load_temp_policy(tmp: Path, **overrides: str) -> server.Policy:
    cloudsdk = tmp / "isolated-config"
    cloudsdk.mkdir(exist_ok=True)
    entry = tmp / "gcloud.py"
    raw = policy_text(cloudsdk, Path(sys.executable), entry, **overrides)
    return server.load_policy(raw)


def call_tool(session: server.Session, name: str, arguments: dict) -> dict:
    session.initialized = True
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        session,
    )
    assert response is not None
    return response


def tool_text(response: dict) -> str:
    return response["result"]["content"][0]["text"]


class PolicyTests(unittest.TestCase):
    def test_example_policy_is_synthetic_and_valid(self) -> None:
        raw = (ROOT / "policy.example.json").read_text(encoding="utf-8")
        self.assertNotIn("example-project", raw.lower())
        policy = server.load_policy(raw)
        profile = policy.profiles["example"]
        self.assertEqual(profile.account, "reader@example.invalid")
        self.assertEqual(profile.project_id, "example-readonly-pilot")

    def test_unknown_policy_property_hides_values(self) -> None:
        raw = (ROOT / "policy.example.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        data["access_token"] = SECRET
        with self.assertRaises(server.PolicyError) as caught:
            server.load_policy(json.dumps(data))
        self.assertEqual(caught.exception.public_message, "unexpected policy property")
        self.assertNotIn(SECRET, caught.exception.public_message)

    def test_relative_cloudsdk_rejected(self) -> None:
        raw = json.loads((ROOT / "policy.example.json").read_text(encoding="utf-8"))
        raw["profiles"]["example"]["cloudsdk_config"] = "relative\\config"
        with self.assertRaises(server.PolicyError):
            server.load_policy(json.dumps(raw))


class EnvironmentTests(unittest.TestCase):
    def test_child_environment_strips_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy = load_temp_policy(Path(tmp))
        profile = policy.profiles["example"]
        source = {
            "PATH": "C:\\Windows",
            "SYSTEMROOT": "C:\\Windows",
            "GOOGLE_APPLICATION_CREDENTIALS": r"C:\keys\sa.json",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "http_proxy": "http://127.0.0.1:9",
            "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT": "evil@example.invalid",
            "CLOUDSDK_AUTH_ACCESS_TOKEN": SECRET,
            "CLOUDSDK_API_ENDPOINT_OVERRIDES_RUN": "https://attacker.example.invalid",
            "CLOUDSDK_CORE_PROJECT": "other-project-id",
        }
        env = server.child_environment(profile, source)
        self.assertEqual(env["CLOUDSDK_CONFIG"], profile.cloudsdk_config)
        self.assertEqual(env["CLOUDSDK_CORE_DISABLE_PROMPTS"], "1")
        self.assertEqual(env["CLOUDSDK_CORE_DISABLE_USAGE_REPORTING"], "1")
        self.assertEqual(env["PATH"], "C:\\Windows")
        for key in (
            "GOOGLE_APPLICATION_CREDENTIALS",
            "HTTPS_PROXY",
            "http_proxy",
            "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
            "CLOUDSDK_AUTH_ACCESS_TOKEN",
            "CLOUDSDK_API_ENDPOINT_OVERRIDES_RUN",
            "CLOUDSDK_CORE_PROJECT",
        ):
            self.assertNotIn(key, env)
        self.assertNotIn(SECRET, json.dumps(env))


class ToolProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.policy = load_temp_policy(Path(self.tmp.name))
        self.profile = self.policy.profiles["example"]
        self.backend = FakeBackend(self.profile)
        self.session = server.Session(self.policy, self.backend, timeout=2, max_output=4096)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_tools_list_annotations(self) -> None:
        self.session.initialized = True
        response = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, self.session)
        tools = {item["name"]: item for item in response["result"]["tools"]}
        self.assertEqual(
            set(tools),
            {
                "context_status",
                "project_info",
                "cloud_run_services",
                "cloud_run_service_status",
                "enabled_services",
            },
        )
        for item in tools.values():
            self.assertEqual(item["annotations"], {"readOnlyHint": True, "destructiveHint": False})
            self.assertFalse(item["inputSchema"]["additionalProperties"])
        self.assertEqual(self.backend.calls, [])

    def test_positive_tools_project_identity_and_redaction(self) -> None:
        expected = {
            "context_status": {"profile": "example"},
            "project_info": {"profile": "example"},
            "cloud_run_services": {"profile": "example", "region": "us-central1"},
            "cloud_run_service_status": {
                "profile": "example",
                "region": "us-central1",
                "service": "api",
            },
            "enabled_services": {"profile": "example"},
        }
        for name, arguments in expected.items():
            self.backend.calls.clear()
            response = call_tool(self.session, name, arguments)
            self.assertFalse(response["result"]["isError"], tool_text(response))
            text = tool_text(response)
            self.assertNotIn(SECRET, text)
            self.assertNotIn("sa.json", text)
            self.assertNotIn("private_key", text)
            self.assertNotIn("access_token", text)
            self.assertNotIn("https://", text)
            self.assertNotIn(self.profile.cloudsdk_config, text)
            self.assertNotIn("spec", text)
            self.assertGreaterEqual(len(self.backend.calls), 2)
            for call in self.backend.calls:
                argv = call["argv"]
                self.assertEqual(
                    argv[2:9],
                    [
                        "--configuration",
                        self.profile.configuration,
                        "--account",
                        self.profile.account,
                        "--project",
                        self.profile.project_id,
                        "--quiet",
                    ],
                )
                self.assertTrue(str(argv[1]).endswith("gcloud.py"))
                self.assertEqual(call["env"]["CLOUDSDK_CONFIG"], self.profile.cloudsdk_config)
                self.assertNotIn("shell", argv)
        project = json.loads(tool_text(call_tool(self.session, "project_info", {"profile": "example"})))
        self.assertEqual(project["project_id"], self.profile.project_id)
        self.assertEqual(project["lifecycle_state"], "ACTIVE")
        listed = json.loads(
            tool_text(
                call_tool(
                    self.session,
                    "cloud_run_services",
                    {"profile": "example", "region": "us-central1"},
                )
            )
        )
        self.assertEqual(listed["services"], [{"name": "api", "ready": True, "region": "us-central1"}])
        status = json.loads(
            tool_text(
                call_tool(
                    self.session,
                    "cloud_run_service_status",
                    {"profile": "example", "region": "us-central1", "service": "api"},
                )
            )
        )
        self.assertEqual(status["latest_ready_revision"], "api-00001")
        self.assertEqual(status["conditions"], [{"status": "True", "type": "Ready"}])
        apis = json.loads(tool_text(call_tool(self.session, "enabled_services", {"profile": "example"})))
        self.assertEqual(apis["services"], [{"name": "run.googleapis.com", "title": "Cloud Run Admin API"}])

    def test_wrong_profile_account_and_project_skip_cloud_reads(self) -> None:
        response = call_tool(self.session, "project_info", {"profile": "other"})
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(tool_text(response), "unknown profile")
        self.assertEqual(self.backend.calls, [])

        self.backend.describe = {
            "name": self.profile.configuration,
            "properties": {"core": {"account": "other-pilot@example.invalid", "project": self.profile.project_id}},
        }
        response = call_tool(self.session, "project_info", {"profile": "example"})
        self.assertEqual(tool_text(response), "account does not match policy")
        self.assertEqual(len(self.backend.calls), 1)
        self.assertNotIn("projects", self.backend.calls[0]["argv"])

        self.backend.calls.clear()
        self.backend.describe = {
            "name": self.profile.configuration,
            "properties": {"core": {"account": self.profile.account, "project": "other-readonly-pilot"}},
        }
        response = call_tool(self.session, "enabled_services", {"profile": "example"})
        self.assertEqual(tool_text(response), "project does not match policy")
        self.assertEqual(len(self.backend.calls), 1)

        self.backend.calls.clear()
        self.backend.describe = None
        self.backend.auth = [{"account": "other-pilot@example.invalid", "status": "ACTIVE"}]
        response = call_tool(self.session, "cloud_run_services", {"profile": "example", "region": "us-central1"})
        self.assertEqual(tool_text(response), "active account is not the policy account")
        self.assertEqual(len(self.backend.calls), 2)
        joined = " ".join(self.backend.calls[-1]["argv"])
        self.assertNotIn("run services list", joined)
        self.assertNotIn("projects", self.backend.calls[-1]["argv"])

    def test_rejected_input_does_not_call_backend(self) -> None:
        samples = [
            ("context_status", {"profile": "example", "command": "auth login"}),
            ("context_status", {"profile": "example", "argv": ["gcloud", "iam", "list"]}),
            ("context_status", {"profile": "example", "url": f"https://attacker.example.invalid/{SECRET}"}),
            ("context_status", {"profile": "example", "flags": "--format=json"}),
            ("context_status", {"profile": "example", "credentials": SECRET}),
            ("context_status", {"profile": "example", "config": r"C:\Users\Example"}),
            ("project_info", {"profile": "example;calc"}),
            ("cloud_run_services", {"profile": "example", "region": MALICIOUS_REGIONS[0], "command": "calc"}),
            ("print-access-token", {"profile": "example"}),
            ("login", {"profile": "example"}),
        ]
        for region in MALICIOUS_REGIONS:
            samples.append(("cloud_run_services", {"profile": "example", "region": region}))
        for service in MALICIOUS_SERVICES:
            samples.append(
                ("cloud_run_service_status", {"profile": "example", "region": "us-central1", "service": service})
            )
        for name, arguments in samples:
            self.backend.calls.clear()
            response = call_tool(self.session, name, arguments)
            self.assertTrue(response["result"]["isError"])
            self.assertNotIn(SECRET, tool_text(response))
            self.assertNotIn("http", tool_text(response))
            self.assertEqual(self.backend.calls, [], arguments)

    def test_malformed_oversize_and_exception_are_redacted(self) -> None:
        self.backend.mode = "malformed"
        response = call_tool(self.session, "context_status", {"profile": "example"})
        self.assertEqual(tool_text(response), "command returned unreadable output")
        self.assertNotIn(SECRET, tool_text(response))
        self.assertEqual(len(self.backend.calls), 1)

        self.backend.calls.clear()
        self.backend.mode = "oversize"
        response = call_tool(self.session, "context_status", {"profile": "example"})
        self.assertEqual(tool_text(response), "output exceeded limit")
        self.assertNotIn(SECRET, tool_text(response))
        self.assertNotIn("AAAA", tool_text(response))

        self.backend.calls.clear()
        self.backend.mode = "raise"
        response = call_tool(self.session, "project_info", {"profile": "example"})
        self.assertEqual(tool_text(response), "tool failed")
        self.assertNotIn(SECRET, json.dumps(response))
        self.assertNotIn("private_key", json.dumps(response))


class SubprocessServerTests(unittest.TestCase):
    def test_protocol_end_to_end_hidden_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "calls.jsonl"
            entry = root / "gcloud.py"
            entry.write_text(FAKE_GCLOUD, encoding="utf-8")
            cloudsdk = root / "isolated-config"
            cloudsdk.mkdir()
            policy_path = root / "policy.json"
            policy_path.write_text(
                policy_text(cloudsdk, Path(sys.executable), entry),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "FAKE_GCLOUD_LOG": str(log_path),
                    "GOOGLE_APPLICATION_CREDENTIALS": r"C:\keys\sa.json",
                    "HTTPS_PROXY": "http://127.0.0.1:9",
                    "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT": "evil@example.invalid",
                    "CLOUDSDK_AUTH_ACCESS_TOKEN": SECRET,
                    "CLOUDSDK_API_ENDPOINT_OVERRIDES_RUN": "https://attacker.example.invalid",
                }
            )
            proc = subprocess.Popen(
                [sys.executable, str(SERVER_PATH), "--policy", str(policy_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                self.assertIsNotNone(proc.stdin)
                self.assertIsNotNone(proc.stdout)
                send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
                send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                send(proc, {"jsonrpc": "2.0", "id": 2, "method": "ping"})
                send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
                calls = [
                    (4, "context_status", {"profile": "example"}),
                    (5, "project_info", {"profile": "example"}),
                    (6, "cloud_run_services", {"profile": "example", "region": "europe-central2"}),
                    (7, "cloud_run_service_status", {"profile": "example", "region": "europe-central2", "service": "api"}),
                    (8, "enabled_services", {"profile": "example"}),
                ]
                for request_id, name, arguments in calls:
                    send(
                        proc,
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "tools/call",
                            "params": {"name": name, "arguments": arguments},
                        },
                    )
                initialized = read_message(proc)
                ping = read_message(proc)
                listed = read_message(proc)
                results = [read_message(proc) for _ in calls]
                proc.stdin.close()
                stderr = proc.stderr.read()
                self.assertEqual(proc.wait(timeout=20), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
            self.assertEqual(initialized["id"], 1)
            self.assertEqual(initialized["result"]["serverInfo"]["name"], "gcp-readonly")
            self.assertEqual(ping["id"], 2)
            self.assertEqual(ping["result"], {})
            self.assertEqual(len(listed["result"]["tools"]), 5)
            self.assertEqual(stderr, b"")
            for response in results:
                self.assertFalse(response["result"]["isError"], response)
                text = response["result"]["content"][0]["text"]
                self.assertNotIn(SECRET, text)
                self.assertNotIn("sa.json", text)
                self.assertNotIn("private_key", text)
            records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertGreaterEqual(len(records), 14)
            for record in records:
                self.assertTrue(str(record["argv"][0]).endswith("gcloud.py"))
                self.assertIn("--quiet", record["argv"])
                self.assertIn("--account", record["argv"])
                self.assertEqual(record["cloudsdk"], str(cloudsdk))
                self.assertEqual(record["prompts"], "1")
                self.assertEqual(record["usage"], "1")
                self.assertIsNone(record["gac"])
                self.assertIsNone(record["proxy"])
                self.assertIsNone(record["imp"])
                self.assertIsNone(record["token"])
                self.assertIsNone(record["endpoint"])
                self.assertNotIn(SECRET, json.dumps(record["argv"]))

    def test_rejected_subprocess_call_does_not_start_gcloud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "calls.jsonl"
            entry = root / "gcloud.py"
            entry.write_text(FAKE_GCLOUD, encoding="utf-8")
            cloudsdk = root / "isolated-config"
            cloudsdk.mkdir()
            policy_path = root / "policy.json"
            policy_path.write_text(policy_text(cloudsdk, Path(sys.executable), entry), encoding="utf-8")
            env = os.environ.copy()
            env["FAKE_GCLOUD_LOG"] = str(log_path)
            proc = subprocess.Popen(
                [sys.executable, str(SERVER_PATH), "--policy", str(policy_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
                send(
                    proc,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "cloud_run_services",
                            "arguments": {
                                "profile": "example",
                                "region": "https://attacker.example.invalid",
                                "command": "gcloud auth print-access-token",
                                "url": SECRET,
                            },
                        },
                    },
                )
                init = read_message(proc)
                denied = read_message(proc)
                proc.stdin.close()
                proc.wait(timeout=10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
            self.assertEqual(init["id"], 1)
            self.assertTrue(denied["result"]["isError"])
            self.assertNotIn(SECRET, json.dumps(denied))
            self.assertFalse(log_path.exists())

    def test_timeout_oversize_malformed_and_failed_subprocess(self) -> None:
        cases = {
            "sleep": ("import time\ntime.sleep(30)\n", "command timed out", 1.0, 4096),
            "huge": (
                f"import sys\nsys.stdout.write('A'*300000 + {SECRET!r})\n",
                "output exceeded limit",
                5.0,
                1024,
            ),
            "badjson": (
                f"import sys\nsys.stdout.write('not-json {SECRET} private_key')\n",
                "command returned unreadable output",
                5.0,
                4096,
            ),
            "fail": (
                f"import sys\nsys.stderr.write('{SECRET} access_token')\nraise SystemExit(2)\n",
                "command failed",
                5.0,
                4096,
            ),
        }
        for name, (script, message, timeout, limit) in cases.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    entry = root / "gcloud.py"
                    entry.write_text(script, encoding="utf-8")
                    cloudsdk = root / "isolated-config"
                    cloudsdk.mkdir()
                    policy = server.load_policy(policy_text(cloudsdk, Path(sys.executable), entry))
                    session = server.Session(policy, server.SubprocessBackend(), timeout=timeout, max_output=limit)
                    response = call_tool(session, "context_status", {"profile": "example"})
                    text = tool_text(response)
                    self.assertEqual(text, message)
                    self.assertNotIn(SECRET, json.dumps(response))
                    self.assertNotIn("AAAA", text)

    def test_missing_runtime_does_not_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cloudsdk = root / "isolated-config"
            cloudsdk.mkdir()
            policy = server.load_policy(
                policy_text(
                    cloudsdk,
                    Path(r"C:\missing-python\python.exe"),
                    Path(r"C:\missing-sdk\lib\gcloud.py"),
                )
            )
            spawned = []
            original = server.subprocess.Popen

            def spy(*args, **kwargs):
                spawned.append(args)
                return original(*args, **kwargs)

            server.subprocess.Popen = spy
            try:
                session = server.Session(policy, server.SubprocessBackend(), timeout=2, max_output=1024)
                response = call_tool(session, "context_status", {"profile": "example"})
            finally:
                server.subprocess.Popen = original
            self.assertEqual(spawned, [])
            self.assertEqual(tool_text(response), "command failed")

    def test_cli_rejects_unknown_property_without_echoing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cloudsdk = root / "isolated-config"
            cloudsdk.mkdir()
            data = json.loads(policy_text(cloudsdk, Path(sys.executable), root / "gcloud.py"))
            data["access_token"] = SECRET
            policy_path = root / "policy.json"
            policy_path.write_text(json.dumps(data), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(SERVER_PATH), "--policy", str(policy_path)],
                input=b"",
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            combined = completed.stdout + completed.stderr
            self.assertEqual(completed.returncode, 2)
            self.assertNotIn(SECRET.encode(), combined)
            self.assertIn(b"unexpected policy property", completed.stderr)


def send(proc: subprocess.Popen, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    assert proc.stdin is not None
    proc.stdin.write(body + b"\n")
    proc.stdin.flush()


def read_message(proc: subprocess.Popen) -> dict:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        raise AssertionError("closed before a JSON-RPC line")
    return json.loads(line.decode("utf-8"))


FAKE_GCLOUD = r"""
import json
import os
import sys
from pathlib import Path

log = Path(os.environ["FAKE_GCLOUD_LOG"])
argv = sys.argv[1:]
record = {
    "argv": sys.argv,
    "cloudsdk": os.environ.get("CLOUDSDK_CONFIG"),
    "prompts": os.environ.get("CLOUDSDK_CORE_DISABLE_PROMPTS"),
    "usage": os.environ.get("CLOUDSDK_CORE_DISABLE_USAGE_REPORTING"),
    "gac": os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
    "proxy": os.environ.get("HTTPS_PROXY"),
    "imp": os.environ.get("CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"),
    "token": os.environ.get("CLOUDSDK_AUTH_ACCESS_TOKEN"),
    "endpoint": os.environ.get("CLOUDSDK_API_ENDPOINT_OVERRIDES_RUN"),
}
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\n")

def flag(name):
    return argv[argv.index(name) + 1]

account = flag("--account")
project = flag("--project")
config = flag("--configuration")
command = argv[argv.index("--quiet") + 1 :]
if command[:3] == ["config", "configurations", "describe"]:
    body = {"name": config, "properties": {"core": {"account": account, "project": project, "access_token": "ya29.SECRETBLOCK"}}}
elif command[:2] == ["auth", "list"]:
    body = [{"account": account, "status": "ACTIVE", "token": "ya29.SECRETBLOCK"}]
elif command[:2] == ["projects", "describe"]:
    body = {
        "projectId": project,
        "projectNumber": "123456789012",
        "name": "Example Pilot",
        "lifecycleState": "ACTIVE",
        "labels": {"secret": "ya29.SECRETBLOCK"},
        "environment": {"GOOGLE_APPLICATION_CREDENTIALS": "C:\\keys\\sa.json"},
    }
elif command[:3] == ["run", "services", "list"]:
    body = [{
        "metadata": {"name": "api", "annotations": {"token": "ya29.SECRETBLOCK"}},
        "spec": {"template": {"spec": {"containers": [{"env": [{"value": "ya29.SECRETBLOCK"}]}]}}},
        "status": {"url": "https://api.example.invalid", "conditions": [{"type": "Ready", "status": "True", "message": "ya29.SECRETBLOCK"}]},
    }]
elif command[:3] == ["run", "services", "describe"]:
    body = {
        "metadata": {"name": command[3], "annotations": {"access_token": "ya29.SECRETBLOCK"}},
        "spec": {"env": "ya29.SECRETBLOCK"},
        "status": {
            "latestReadyRevisionName": "api-00001",
            "url": "https://api.example.invalid",
            "conditions": [{"type": "Ready", "status": "True", "message": "private_key"}],
        },
    }
elif command[:2] == ["services", "list"]:
    body = [{"config": {"name": "run.googleapis.com", "title": "Cloud Run Admin API", "authentication": {"secret": "ya29.SECRETBLOCK"}}}]
else:
    sys.stderr.write("ya29.SECRETBLOCK")
    raise SystemExit(4)
sys.stdout.write(json.dumps(body))
"""


if __name__ == "__main__":
    unittest.main()
