"""office_server_package: pinned, inert server package for the Office 24/7 cutover."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import install_agent_office as installer  # noqa: E402
import office_server_package as pkg  # noqa: E402
from coordfs.office_cli_remote import parse_remote_config_bytes  # noqa: E402
from coordfs.office_remote import OfficeRemoteConfig  # noqa: E402


def _run(*args: str) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, "-B", str(ROOT / pkg.TOOL), *args],
                          capture_output=True, text=True, timeout=120, check=False)
    return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])


class RenderedPackage(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name) / "pkg"
        cls.rc, cls.result = _run("render", "--out", str(cls.out))
        cls.manifest = json.loads((cls.out / pkg.MANIFEST).read_text(encoding="utf-8"))
        cls.package = installer.verify_package()
        cls.fp12 = cls.package.runtime_fingerprint[:12]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def read(self, rel: str) -> str:
        return (self.out / rel).read_text(encoding="utf-8")

    def test_params_use_inert_generic_example_defaults(self) -> None:
        params = pkg.Params()
        self.assertEqual(
            (params.ws_from, params.server_ssh_host, params.server_peer_host, params.ws_peer_host,
             params.windows_client_dir, params.hermes_home, params.hermes_client_dir,
             params.hermes_session_id),
            ("192.0.2.20", "192.0.2.50", "office-server", "workstation",
             "C:/Example/office-client", "/srv/example/hermes/home",
             "/srv/example/hermes/home/.agents/office-client", "main"),
        )
        params.check()
        self.assertEqual(self.manifest["activation"], "NOT_PERFORMED")

    def test_render_pins_the_verified_source_runtime(self) -> None:
        self.assertEqual(self.rc, 0, self.result)
        self.assertEqual(self.result["code"], "package_rendered")
        self.assertEqual(self.manifest["runtime_fingerprint"], self.package.runtime_fingerprint)
        self.assertEqual(self.manifest["package_fingerprint"], self.package.fingerprint)
        self.assertEqual(self.manifest["activation"], "NOT_PERFORMED")
        runtime = {rel: (self.out / "runtime" / self.fp12 / rel).read_bytes()
                   for rel in self.package.runtime_files}
        self.assertEqual(runtime, dict(self.package.runtime_files))
        self.assertEqual(pkg.tree_fingerprint(runtime), self.package.runtime_fingerprint)

    def test_verify_accepts_untouched_package(self) -> None:
        rc, result = _run("verify", "--package", str(self.out))
        self.assertEqual((rc, result["code"]), (0, "package_verified"), result)
        self.assertEqual(result["runtime_fingerprint"], self.package.runtime_fingerprint)

    def test_units_are_pinned_hardened_and_local_store_only(self) -> None:
        for unit, command in (("agent-office-wake-tick", "wake-tick"),
                              ("agent-office-wake-monitor", "wake-monitor --post")):
            text = self.read(f"server/systemd/{unit}.service")
            self.assertIn(f"ExecStart=/usr/bin/python3.14 -B -I /opt/agent-office/runtime/{self.fp12}/office.py "
                          f"--home /var/lib/agent-office/coord --agent office-office-server-", text)
            self.assertTrue(text.split("ExecStart=")[1].splitlines()[0].endswith(command))
            for line in ("User=agentoffice", "ProtectSystem=strict", "ReadWritePaths=/var/lib/agent-office/coord",
                         "NoNewPrivileges=yes", "RestrictAddressFamilies=AF_UNIX", "Type=oneshot"):
                self.assertIn(line + "\n", text)
            self.assertNotIn("--remote-config", text)
            timer = self.read(f"server/systemd/{unit}.timer")
            self.assertIn(f"Unit={unit}.service", timer)
            self.assertIn("WantedBy=timers.target", timer)

    def test_forced_command_keys_bind_principal_host_and_source(self) -> None:
        lines = self.read("server/ssh/authorized_keys.template").splitlines()
        gate = f"/opt/agent-office/runtime/{self.fp12}/office_ssh_gate.py"
        self.assertEqual(lines, [
            f'restrict,from="192.0.2.20",command="/usr/bin/python3.14 -B -I {gate} --principal ws-office '
            f'--home /var/lib/agent-office/coord --peer-host workstation" <WS_OFFICE_PUBLIC_KEY>',
            f'restrict,from="127.0.0.1,::1",command="/usr/bin/python3.14 -B -I {gate} --principal hermes '
            f'--home /var/lib/agent-office/coord --peer-host office-server" <HERMES_PUBLIC_KEY>',
        ])

    def test_install_keys_pins_both_rendered_bindings(self) -> None:
        script = self.read("server/install-keys.sh")
        gate = f"/opt/agent-office/runtime/{self.fp12}/office_ssh_gate.py"
        self.assertIn(
            f'WS_EXPECT=\'restrict,from="192.0.2.20",command="/usr/bin/python3.14 -B -I {gate} '
            f'--principal ws-office --home /var/lib/agent-office/coord --peer-host workstation" \'"$WS"',
            script,
        )
        self.assertIn(
            f'HERMES_EXPECT=\'restrict,from="127.0.0.1,::1",command="/usr/bin/python3.14 -B -I {gate} '
            f'--principal hermes --home /var/lib/agent-office/coord --peer-host office-server" \'"$HE"',
            script,
        )
        self.assertIn('[ "${LINES[0]}" = "$WS_EXPECT" ] || fail authorized_keys_ws_binding', script)
        self.assertIn('[ "${LINES[1]}" = "$HERMES_EXPECT" ] || fail authorized_keys_hermes_binding', script)

    def test_install_keys_pins_custom_source_to_single_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text('{"ws_from":"192.0.2.20/32"}', encoding="utf-8")
            out = Path(tmp) / "pkg"
            rc, result = _run("render", "--out", str(out), "--config", str(config))
            self.assertEqual(rc, 0, result)
            script = (out / "server/install-keys.sh").read_text(encoding="utf-8")
            self.assertIn('WS_EXPECT=\'restrict,from="192.0.2.20/32",command=', script)
            self.assertNotIn('from="[0-9a-fA-F:.,/]+"', script)

    def test_client_connection_files_pass_the_client_parser(self) -> None:
        def parse(rel: str, native: bool) -> OfficeRemoteConfig:
            raw = (self.out / rel).read_bytes()
            return parse_remote_config_bytes(raw) if native else OfficeRemoteConfig(**json.loads(raw))

        win = parse("clients/windows/office-remote.json", os.name == "nt")
        self.assertEqual((win.host, win.user, win.port), ("192.0.2.50", "agentoffice", 22))
        self.assertEqual(str(win.ssh_executable), "C:/Windows/System32/OpenSSH/ssh.exe")
        her = parse("clients/hermes/office-remote.json", os.name != "nt")
        self.assertEqual((her.host, her.user), ("127.0.0.1", "agentoffice"))
        for rel in ("clients/windows/office-remote.json", "clients/hermes/office-remote.json"):
            self.assertNotIn("PRIVATE KEY", self.read(rel))
        mcp = self.read("clients/hermes/enroll-hermes-mcp.sh")
        self.assertIn("export HOME=/srv/example/hermes/home HERMES_HOME=/srv/example/hermes/home/.hermes", mcp)
        add = [line for line in mcp.splitlines() if " mcp add agent_office " in line][0]
        args = add.split(" --args ", 1)[1].split(" ")
        self.assertEqual(args[2], f"/opt/agent-office/runtime/{self.fp12}/office.py")
        self.assertEqual(args[3:9], ["--remote-config",
                                     "/srv/example/hermes/home/.agents/office-client/office-remote.json",
                                     "--harness", "hermes", "--session-id", "main"])
        for banned in ("mcp remove", "mcp rm", "rm ", "systemctl"):
            self.assertNotIn(banned, mcp)
        self.assertEqual(args[-1], "mcp")

    def test_scripts_never_delete_enable_or_start(self) -> None:
        for rel in ("server/install-runtime.sh", "server/install-keys.sh", "clients/hermes/enroll-hermes-mcp.sh"):
            text = self.read(rel)
            for banned in ("rm ", "rm\t", "rmdir", "unlink", "--now", "systemctl enable", "systemctl start",
                           "loginctl", "shred", "mv "):
                self.assertNotIn(banned, text, f"{rel}: {banned}")
        if shutil.which("bash"):
            for rel in ("server/install-runtime.sh", "server/install-keys.sh", "clients/hermes/enroll-hermes-mcp.sh"):
                proc = subprocess.run(["bash", "-n"], input=(self.out / rel).read_bytes(), capture_output=True, timeout=30)
                self.assertEqual(proc.returncode, 0, proc.stderr)


class Refusals(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_render_refuses_non_empty_output(self) -> None:
        out = self.tmp / "busy"
        out.mkdir()
        (out / "keep.txt").write_text("x", encoding="utf-8")
        rc, result = _run("render", "--out", str(out))
        self.assertEqual(rc, 1)
        self.assertIn("not empty", result["reason"])
        self.assertEqual((out / "keep.txt").read_text(encoding="utf-8"), "x")

    def test_verify_detects_changed_extra_and_missing_files(self) -> None:
        out = self.tmp / "pkg"
        self.assertEqual(_run("render", "--out", str(out))[0], 0)
        unit = out / "server/systemd/agent-office-wake-tick.service"
        original = unit.read_bytes()
        unit.write_bytes(original.replace(b"User=agentoffice", b"User=root"))
        rc, result = _run("verify", "--package", str(out))
        self.assertEqual(rc, 1)
        self.assertIn("agent-office-wake-tick.service", result["reason"])
        unit.write_bytes(original)
        (out / "server/extra.sh").write_text("echo", encoding="utf-8")
        rc, result = _run("verify", "--package", str(out))
        self.assertEqual(rc, 1)
        self.assertIn("extra.sh", result["reason"])

    def test_verify_detects_runtime_byte_change_even_with_forged_manifest_row(self) -> None:
        out = self.tmp / "pkg"
        self.assertEqual(_run("render", "--out", str(out))[0], 0)
        manifest_path = out / pkg.MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rel = f"{manifest['runtime_dir']}/office.py"
        data = (out / rel).read_bytes() + b"\n# tampered\n"
        (out / rel).write_bytes(data)
        manifest["files"][rel] = {"bytes": len(data), "sha256": pkg._sha(data)}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        rc, result = _run("verify", "--package", str(out))
        self.assertEqual(rc, 1)
        self.assertIn("runtime fingerprint mismatch", result["reason"])

    def test_consistently_rewritten_package_fails_only_against_the_render_pin(self) -> None:
        out = self.tmp / "pkg"
        rc, rendered = _run("render", "--out", str(out))
        self.assertEqual(rc, 0)
        pin = rendered["package_tree_sha256"]
        self.assertEqual(_run("verify", "--package", str(out), "--expect-tree-sha256", pin)[0], 0)
        # An attacker with write access rewrites a unit AND every manifest field consistently.
        unit = out / "server/systemd/agent-office-wake-tick.service"
        unit.write_bytes(unit.read_bytes().replace(b"User=agentoffice", b"User=root"))
        manifest_path = out / pkg.MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = {rel: (out / rel).read_bytes() for rel in manifest["files"]}
        manifest["files"] = {rel: {"bytes": len(d), "sha256": pkg._sha(d)} for rel, d in files.items()}
        manifest["package_tree_sha256"] = pkg.tree_fingerprint(files)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(_run("verify", "--package", str(out))[0], 0)  # self-consistent forgery
        rc, result = _run("verify", "--package", str(out), "--expect-tree-sha256", pin)
        self.assertEqual(rc, 1)
        self.assertIn("expected pin", result["reason"])

    def test_coreutils_digest_in_scripts_equals_render_pin(self) -> None:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash unavailable")
        out = self.tmp / "pkg"
        rc, rendered = _run("render", "--out", str(out))
        self.assertEqual(rc, 0)
        script = (out / "server/install-runtime.sh").read_text(encoding="utf-8")
        func = script[script.index("tree_digest() {"):script.index("\n}\n", script.index("tree_digest() {")) + 3]
        # Script travels on stdin: Windows argv quoting must not alter shell source.
        proc = subprocess.run([bash, "-s", "--", out.as_posix()],
                              input=func + '\ntree_digest "$1"\n',
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not proc.stdout.strip():
            self.skipTest(f"GNU find/stat/sha256sum unavailable: {proc.stderr[-200:]}")
        self.assertEqual(proc.stdout.strip(), rendered["package_tree_sha256"])

    def test_render_from_explicit_source_enforces_expected_runtime(self) -> None:
        runtime = installer.verify_package().runtime_fingerprint
        rc, result = _run("render", "--out", str(self.tmp / "ok"), "--source", str(ROOT),
                          "--expect-runtime-fingerprint", runtime)
        self.assertEqual((rc, result["runtime_fingerprint"]), (0, runtime), result)
        rc, result = _run("render", "--out", str(self.tmp / "bad"), "--source", str(ROOT),
                          "--expect-runtime-fingerprint", "0" * 64)
        self.assertEqual(rc, 1)
        self.assertIn("differs from the expected", result["reason"])
        self.assertFalse((self.tmp / "bad").exists())
        rc, result = _run("render", "--out", str(self.tmp / "none"), "--source", str(self.tmp),
                          "--expect-runtime-fingerprint", runtime)
        self.assertEqual(rc, 1)
        self.assertIn("not an Office source", result["reason"])
        rc, result = _run("render", "--out", str(self.tmp / "nopin"), "--source", str(ROOT))
        self.assertEqual(rc, 1)
        self.assertIn("requires --expect-runtime-fingerprint", result["reason"])

    def test_foreign_source_is_verified_as_data_never_executed(self) -> None:
        runtime = installer.verify_package().runtime_fingerprint
        fake = self.tmp / "source"
        shutil.copytree(ROOT, fake, ignore=shutil.ignore_patterns("__pycache__", "tests"))
        marker = self.tmp / "EXECUTED"
        hostile = f"open({str(marker)!r}, 'w').close()\nraise SystemExit(0)\n"
        (fake / "install_agent_office.py").write_text(hostile, encoding="utf-8")
        (fake / "coordfs" / "__init__.py").write_text(hostile, encoding="utf-8")
        (fake / "office_store_snapshot.py").write_text(hostile, encoding="utf-8")
        rc, result = _run("render", "--out", str(self.tmp / "out"), "--source", str(fake),
                          "--expect-runtime-fingerprint", runtime)
        # coordfs/__init__.py is a runtime file, so the trusted verifier refuses the changed bytes.
        self.assertEqual(rc, 1, result)
        self.assertFalse(marker.exists(), "renderer executed code from --source")
        shutil.copyfile(ROOT / "coordfs" / "__init__.py", fake / "coordfs" / "__init__.py")
        rc, result = _run("render", "--out", str(self.tmp / "out1"), "--source", str(fake),
                          "--expect-runtime-fingerprint", runtime)
        self.assertEqual(rc, 1)  # the hostile restore tool is a runtime file too
        self.assertIn("office_store_snapshot.py", result["reason"])
        shutil.copyfile(ROOT / pkg.SNAPSHOT_TOOL, fake / pkg.SNAPSHOT_TOOL)
        rc, result = _run("render", "--out", str(self.tmp / "out2"), "--source", str(fake),
                          "--expect-runtime-fingerprint", runtime)
        self.assertEqual((rc, result.get("runtime_fingerprint")), (0, runtime), result)
        self.assertFalse(marker.exists(), "renderer executed code from --source")
        self.assertEqual((self.tmp / "out2" / pkg.SNAPSHOT_TOOL).read_bytes(),
                         installer.verify_package().runtime_files[pkg.SNAPSHOT_TOOL])
        runtime_file = fake / "coordfs" / "office.py"
        runtime_file.write_bytes(runtime_file.read_bytes() + b"\n# changed\n")
        rc, result = _run("render", "--out", str(self.tmp / "out3"), "--source", str(fake),
                          "--expect-runtime-fingerprint", runtime)
        self.assertEqual(rc, 1)
        self.assertFalse((self.tmp / "out3").exists())

    def test_params_refuse_unsafe_values(self) -> None:
        bad = [
            {"state_root": "/var/lib/agent office"}, {"state_root": "/var/lib/%h"},
            {"python": "python3"}, {"runtime_parent": "/opt/../etc"}, {"service_user": "Root!"},
            {"ws_peer_host": "office-server"}, {"ws_peer_host": "WORKSTATION"}, {"tick_interval_s": 5},
            {"tick_interval_s": True}, {"windows_ssh": "ssh.exe"}, {"ws_from": "any\"),command=\"x"},
            {"runtime_parent": "/var/lib/agent-office/runtime"}, {"hermes_session_id": "Robert Main"},
        ]
        for override in bad:
            with self.subTest(override=override):
                with self.assertRaises(pkg.Refused):
                    pkg.Params(**{**pkg.asdict(pkg.Params()), **override}).check()

    def test_config_file_is_closed(self) -> None:
        cfg = self.tmp / "cfg.json"
        cfg.write_text('{"state_root": "/srv/x", "surprise": 1}', encoding="utf-8")
        with self.assertRaises(pkg.Refused):
            pkg.load_params(cfg)
        cfg.write_text('{"state_root": "/srv/x", "state_root": "/srv/y"}', encoding="utf-8")
        with self.assertRaises(pkg.Refused):
            pkg.load_params(cfg)
        cfg.write_text('{"state_root": "/srv/agent-office"}', encoding="utf-8")
        self.assertEqual(pkg.load_params(cfg).state_root, "/srv/agent-office")


if __name__ == "__main__":
    unittest.main()
