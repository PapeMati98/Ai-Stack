"""Registration overlay tests; synthetic fixtures retained in the worktree."""

import copy
import hashlib
import json
import os
import sys
import io
import secrets
import subprocess
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from broker import Reject
from registration import (SERVER_KEY, VERSION, apply, canonical_bytes, dry_run,
                          fallback_record, normalize_harness, overlay_entry,
                          plan, validate_enrollment, validate_enrollments, main, validation_argv)
import registration

TOKEN = "SYNTHETIC-TOKEN-AAAAAAAAAAAAAAAA"
PYTHON = sys.executable


def enrollment(harness="claude", transport="mcp", tmp=None):
    base = Path(tmp) if tmp else Path("C:/synthetic")
    return {"harness": harness, "transport": transport,
            "launcher": str(base / "1.2.0" / "managed.py"),
            "config": str(base / "private" / "managed-config.json")}


def existing_config():
    return {"mcpServers": {"office-docs": {"command": "office", "args": []},
                           "hub-tools": {"command": "hub", "args": ["--x"]}},
            "topLevel": {"keep": True}}


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[4] / "task-evidence" / "chrome-relay-tests"
        self.root = root / ("registration-" + secrets.token_hex(8))
        (self.root / "1.2.0").mkdir(parents=True)
        self.launcher = self.root / "1.2.0" / "managed.py"
        self.launcher.write_text("# synthetic launcher", encoding="utf-8")

    def record(self, **overrides):
        data = enrollment(tmp=self.root)
        data["launcher"] = str(self.launcher)
        data.update(overrides)
        return data

    def test_all_enrolled_harnesses_validate(self):
        names = ["codex", "claude", "kimi", "glm/zcode", "qwen", "grok",
                 "minimax", "mistral", "muse", "codebuddy", "antigravity", "mimo"]
        report = validate_enrollments([self.record(harness=n) for n in names])
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(report["ok"]), 12)
        self.assertEqual(normalize_harness("glm/zcode"), "glm")
        self.assertEqual(normalize_harness("zcode"), "glm")
        self.assertEqual(normalize_harness(" GLM "), "glm")

    def test_unknown_harness_and_transport_rejected_honestly(self):
        with self.assertRaises(Reject) as ctx:
            validate_enrollment(self.record(harness="unknown-cli"))
        self.assertEqual(ctx.exception.reason, "unknown_harness")
        for bad in ("sse", "websocket", "", "MCP", None):
            with self.assertRaises(Reject) as ctx:
                validate_enrollment(self.record(transport=bad))
            self.assertEqual(ctx.exception.reason, "unknown_transport")
        report = validate_enrollments([self.record(), self.record(harness="nope"),
                                       self.record(transport="sse")])
        self.assertEqual(len(report["ok"]), 1)
        self.assertEqual([e["reason"] for e in report["errors"]],
                         ["unknown_harness", "unknown_transport"])

    def test_cli_fallback_assignment_is_explicit(self):
        record = fallback_record(self.record(harness="kimi", transport="mcp"))
        self.assertEqual(record["transport"], "cli")
        entry = overlay_entry(record, PYTHON)[SERVER_KEY]
        self.assertEqual(entry["args"], ["-B", str(self.launcher), "cli", "--config",
                                        record["config"], "--harness", "kimi"])
        with self.assertRaises(Reject):
            dry_run(canonical_bytes(existing_config()), record, PYTHON)
        target = self.root / "assignment.json"
        receipt = apply(str(target), record, PYTHON)
        doc = json.loads(target.read_bytes())
        self.assertEqual(doc["schema"], "chrome-relay-cli-assignment/1")
        self.assertEqual(doc["assignments"][SERVER_KEY], entry)
        self.assertFalse(receipt["native_mcp_modified"])
        self.assertNotIn("mcpServers", doc)

    def test_argv_exact_and_secret_free(self):
        record = self.record()
        entry = overlay_entry(record, PYTHON)[SERVER_KEY]
        self.assertEqual(entry["args"],
                         ["-B", record["launcher"], "mcp", "--config", record["config"],
                          "--harness", "claude"])
        self.assertEqual(entry["command"], PYTHON)
        self.assertEqual(set(entry), {"command", "args"})
        self.assertEqual(validation_argv(record, PYTHON),
                         [PYTHON, "-B", record["launcher"], "validate", "--config", record["config"]])
        self.assertNotIn("token", json.dumps(entry).lower())
        blob = (json.dumps(dry_run(None, record, PYTHON)) +
                json.dumps(apply(str(self.root / "fresh.json"), record, PYTHON))).encode()
        self.assertNotIn(TOKEN.encode(), blob)
        self.assertNotIn(b"client_file", blob)
        self.assertNotIn(b"session_id", blob)

    def test_dry_run_writes_nothing(self):
        target = self.root / "config.json"
        before = canonical_bytes(existing_config())
        target.write_bytes(before)
        report = dry_run(target.read_bytes(), self.record(), PYTHON)
        self.assertTrue(report["changed"])
        self.assertEqual(report["files_written"], 0)
        self.assertEqual(report["files_deleted"], 0)
        self.assertFalse(report["native_mcp_modified"])
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.preimage")), [])

    def test_apply_preserves_unrelated_and_never_deletes(self):
        target = self.root / "config.json"
        target.write_bytes(canonical_bytes(existing_config()))
        receipt = apply(str(target), self.record(), PYTHON)
        self.assertEqual(receipt["status"], "APPLIED")
        self.assertEqual(receipt["files_deleted"], 0)
        merged = json.loads(target.read_bytes().decode("utf-8"))
        self.assertEqual(merged["mcpServers"]["office-docs"], {"command": "office", "args": []})
        self.assertEqual(merged["mcpServers"]["hub-tools"], {"command": "hub", "args": ["--x"]})
        self.assertEqual(merged["topLevel"], {"keep": True})
        self.assertIn(SERVER_KEY, merged["mcpServers"])
        self.assertEqual(receipt["preserved_keys"], ["hub-tools", "office-docs"])

    def test_apply_preimage_and_noop(self):
        target = self.root / "config.json"
        original = canonical_bytes(existing_config())
        target.write_bytes(original)
        receipt = apply(str(target), self.record(), PYTHON)
        preimage = Path(receipt["preimage_path"])
        self.assertTrue(preimage.is_file())
        self.assertEqual(preimage.read_bytes(), original)
        self.assertEqual(receipt["preimage_sha256"], hashlib.sha256(original).hexdigest())
        again = apply(str(target), self.record(), PYTHON)
        self.assertEqual(again["status"], "NOOP")
        self.assertFalse(again["changed"])
        self.assertEqual(again["files_written"], 0)
        self.assertIsNone(again["preimage_path"])

    def test_preimage_conflict_refuses_and_preserves_config(self):
        target = self.root / "config.json"
        target.write_bytes(canonical_bytes(existing_config()))
        first = apply(str(target), self.record(transport="mcp"), PYTHON)
        before = target.read_bytes()
        Path(first["preimage_path"]).write_bytes(b'{"tampered":true}')
        digest = hashlib.sha256(before).hexdigest()
        Path(str(target) + ".preimage-" + digest).write_bytes(b"conflicting snapshot")
        with self.assertRaises(Reject) as ctx:
            apply(str(target), self.record(config=str(self.root / "new-managed.json")), PYTHON)
        self.assertEqual(ctx.exception.reason, "preimage")
        self.assertEqual(target.read_bytes(), before)

    def test_determinism(self):
        first = dry_run(canonical_bytes(existing_config()), self.record(), PYTHON)
        second = dry_run(canonical_bytes(existing_config()), self.record(), PYTHON)
        self.assertEqual(first, second)
        left, right = self.root / "a.json", self.root / "b.json"
        for path in (left, right):
            path.write_bytes(canonical_bytes(existing_config()))
        self.assertEqual(apply(str(left), self.record(), PYTHON)["config_sha256"],
                         apply(str(right), self.record(), PYTHON)["config_sha256"])
        self.assertEqual(left.read_bytes(), right.read_bytes())

    def test_new_config_apply_has_no_preimage(self):
        target = self.root / "fresh.json"
        receipt = apply(str(target), self.record(), PYTHON)
        self.assertEqual(receipt["status"], "APPLIED")
        self.assertIsNone(receipt["preimage_path"])
        merged = json.loads(target.read_bytes().decode("utf-8"))
        self.assertEqual(set(merged["mcpServers"]), {SERVER_KEY})

    def test_scope_and_value_validation(self):
        with self.assertRaises(Reject) as ctx:
            plan(["not", "an", "object"], self.record(), PYTHON)
        self.assertEqual(ctx.exception.reason, "scope")
        with self.assertRaises(Reject):
            plan({"mcpServers": []}, self.record(), PYTHON)
        bad = dict(existing_config())
        with self.assertRaises(Reject):
            dry_run(canonical_bytes(bad) + b"\x00", self.record(), PYTHON)
        for field, value in (("launcher", "relative/path.py"),
                             ("launcher", str(self.root / "launch-session.py")),
                             ("config", "C:\\x\\..\\y"),
                             ("config", "\\root-relative.json"),
                             ("config", "C:drive-relative.json"),
                             ("harness", 42)):
            with self.assertRaises(Reject):
                validate_enrollment(self.record(**{field: value}))
        extra = self.record()
        extra["token"] = TOKEN
        with self.assertRaises(Reject):
            validate_enrollment(extra)
        with self.assertRaises(Reject):
            apply(str(self.root / "missing-launcher.json"),
                  self.record(launcher=str(self.root / "absent.py")), PYTHON)
        self.assertFalse((self.root / "missing-launcher.json").exists())

    def test_existing_entry_update_keeps_others(self):
        target = self.root / "config.json"
        stale = copy.deepcopy(existing_config())
        stale["mcpServers"][SERVER_KEY] = {"command": "stale", "args": []}
        target.write_bytes(canonical_bytes(stale))
        receipt = apply(str(target), self.record(), PYTHON)
        self.assertTrue(receipt["updated"])
        merged = json.loads(target.read_bytes().decode("utf-8"))
        self.assertEqual(merged["mcpServers"][SERVER_KEY]["args"][-2:],
                         ["--harness", "claude"])
        self.assertIn("office-docs", merged["mcpServers"])

    def test_rejects_every_legacy_binding_field(self):
        for field in ("client_file", "session_id", "profile_id", "broker_base", "token"):
            with self.subTest(field=field), self.assertRaises(Reject):
                validate_enrollment(self.record(**{field: "legacy"}))

    def test_json_bytes_preserved_on_add_and_update(self):
        prefix = '{\r\n  "Z": "Za\u017c\u00f3\u0142\u0107", "mcpServers" : { "office-docs" : {"args":["\u0119"], "command":"office"},\r\n "hub-tools": { "command":"hub" }'
        suffix = ' }, "A": [3,  2,1]  }\r\n'
        target = self.root / "native.json"
        original = (prefix + suffix).encode()
        target.write_bytes(original)
        report = dry_run(original, self.record(), PYTHON)
        receipt = apply(str(target), self.record(), PYTHON)
        changed = target.read_bytes()
        self.assertTrue(changed.startswith(prefix.encode()))
        self.assertTrue(changed.endswith(suffix.encode()))
        self.assertEqual(receipt["config_sha256"], report["config_sha256"])
        old_value = json.dumps(overlay_entry(self.record(), PYTHON)[SERVER_KEY], ensure_ascii=False,
                               separators=(",", ":")).encode()
        begin = changed.index(old_value)
        next_record = self.record(config=str(self.root / "different.json"))
        apply(str(target), next_record, PYTHON)
        updated = target.read_bytes()
        self.assertEqual(updated[:begin], changed[:begin])
        self.assertTrue(updated.endswith(changed[begin + len(old_value):]))
        self.assertEqual(Path(receipt["preimage_path"]).read_bytes(), original)

    def test_zcode_exact_native_shape_and_allowed_fields(self):
        original = b'{ "mcpServers": {"decoy":true}, "mcp": { "enabled":true, "servers": {"office": {"command":"office"}} }, "theme":"dark" }'
        target = self.root / "config.json"
        target.write_bytes(original)
        report = apply(str(target), self.record(harness="zcode"), PYTHON)
        doc = json.loads(target.read_bytes())
        entry = doc["mcp"]["servers"][SERVER_KEY]
        self.assertEqual(set(entry), {"command", "args"})
        self.assertEqual(entry["args"][-1], "glm")
        self.assertEqual(doc["mcpServers"], {"decoy": True})
        self.assertEqual(report["scope"], ["mcp", "servers", SERVER_KEY])
        self.assertTrue(target.read_bytes().startswith(original[:original.index(b'} }, "theme"')]))

    def test_json_missing_parents_bom_and_empty_object(self):
        for original in (b'{}', b'{"mcp":{ }}', b'\xef\xbb\xbf{ "label": "native" }\r\n'):
            target = self.root / (secrets.token_hex(4) + ".json")
            target.write_bytes(original)
            apply(str(target), self.record(harness="glm"), PYTHON)
            doc = json.loads(target.read_bytes().decode("utf-8-sig"))
            self.assertIn(SERVER_KEY, doc["mcp"]["servers"])
            if original.startswith(b'\xef\xbb\xbf'):
                self.assertTrue(target.read_bytes().startswith(b'\xef\xbb\xbf{ "label": "native"'))

    def test_unknown_native_layout_requires_explicit_declaration(self):
        for name in ("kimi", "qwen", "grok", "minimax", "mistral", "muse",
                     "codebuddy", "antigravity", "mimo"):
            with self.subTest(name=name), self.assertRaises(Reject) as ctx:
                dry_run(None, self.record(harness=name), PYTHON)
            self.assertEqual(ctx.exception.reason, "unknown_native_format")
            report = dry_run(None, self.record(harness=name, native_format="mcpServers-json"), PYTHON)
            self.assertEqual(report["format_evidence"], "enrollment-declaration")
            self.assertEqual(report["discovery_verified"], False)
        for name, bad in (("glm", "mcpServers-json"), ("codex", "mcpServers-json"),
                          ("claude", "unknown")):
            with self.assertRaises(Reject):
                dry_run(None, self.record(harness=name, native_format=bad), PYTHON)

    def test_codex_toml_add_update_and_comments_remain(self):
        prefix = '# Native configuration\r\nmodel = "local"\r\n[mcp_servers.office]\r\ncommand = "office" # untouched\r\nargs = ["\u0119"]\r\n'
        target = self.root / "config.toml"
        target.write_bytes(prefix.encode())
        record = self.record(harness="codex")
        apply(str(target), record, PYTHON)
        self.assertTrue(target.read_bytes().startswith(prefix.encode()))
        doc = tomllib.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(doc["mcp_servers"][SERVER_KEY], overlay_entry(record, PYTHON)[SERVER_KEY])
        suffix = '\r\n[mcp_servers.hub]\r\ncommand = "hub"\r\n'
        target.write_bytes(target.read_bytes() + suffix.encode())
        apply(str(target), self.record(harness="codex", config=str(self.root / "other.json")), PYTHON)
        self.assertTrue(target.read_bytes().startswith(prefix.encode()))
        self.assertTrue(target.read_bytes().endswith(suffix.encode()))
        self.assertEqual(apply(str(target), self.record(harness="codex", config=str(self.root / "other.json")), PYTHON)["status"], "NOOP")

    def test_codex_required_true_and_false_are_rendered_and_parsed(self):
        for required in (True, False):
            with self.subTest(required=required):
                record = self.record(harness="codex", required=required)
                merged, changed, _ = plan(None, record, PYTHON)
                rendered, rendered_changed, _ = registration._prepare(None, record, PYTHON)
                parsed = registration._parse(rendered, "codex-toml")
                expected_line = "required = " + ("true" if required else "false")
                lines = rendered.decode("utf-8").splitlines()
                self.assertTrue(changed)
                self.assertTrue(rendered_changed)
                self.assertEqual(parsed, merged)
                self.assertEqual(parsed["mcp_servers"][SERVER_KEY]["required"], required)
                self.assertEqual(overlay_entry(record, PYTHON)[SERVER_KEY]["required"], required)
                required_index = lines.index(expected_line)
                self.assertEqual(lines[required_index - 1].split("=", 1)[0].strip(), "args")

    def test_codex_required_missing_retains_legacy_bytes(self):
        prefix = ('# Native configuration\r\nmodel = "local"\r\n'
                  '[mcp_servers.office]\r\ncommand = "office"\r\nargs = []\r\n')
        original = prefix.encode("utf-8")
        record = self.record(harness="codex")
        result, changed, _ = registration._prepare(original, record, PYTHON)
        entry = {"command": PYTHON,
                 "args": ["-B", record["launcher"], "mcp", "--config", record["config"],
                          "--harness", "codex"]}
        compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        legacy = (prefix + '[mcp_servers."fleet-chrome-relay"]\r\n'
                  + "command = " + compact(entry["command"]) + "\r\n"
                  + "args = " + compact(entry["args"]) + "\r\n").encode("utf-8")
        self.assertTrue(changed)
        self.assertEqual(set(overlay_entry(record, PYTHON)[SERVER_KEY]), {"command", "args"})
        self.assertEqual(result, legacy)

    def test_required_is_codex_only_and_exact_boolean(self):
        unsupported = [self.record(harness="claude", required=True),
                       self.record(harness="antigravity", native_format="mcpServers-json", required=True),
                       self.record(harness="codex", transport="cli", required=True)]
        for record in unsupported:
            with self.subTest(harness=record["harness"], transport=record["transport"]):
                with self.assertRaises(Reject) as ctx:
                    validate_enrollment(record)
                self.assertEqual(ctx.exception.reason, "required_unsupported")
        for value in ("true", 1, 0, None):
            with self.subTest(value=value), self.assertRaises(Reject) as ctx:
                validate_enrollment(self.record(harness="codex", required=value))
            self.assertEqual(ctx.exception.reason, "required")
        fallback = fallback_record(self.record(harness="codex", required=True))
        self.assertEqual(fallback["transport"], "cli")
        self.assertNotIn("required", fallback)

    def test_codex_required_apply_replaces_entry_and_preserves_other_sections(self):
        prefix = ('# Native configuration\r\n[mcp_servers.office]\r\n'
                  'command = "office" # untouched\r\nargs = []\r\n\r\n')
        old_entry = ('[mcp_servers."fleet-chrome-relay"]\r\n'
                     'command = "old"\r\nargs = ["old"]\r\n')
        suffix = ('\r\n[other]\r\nvalue = "keep this section byte-for-byte"\r\n')
        target = self.root / "required.toml"
        original = (prefix + old_entry + suffix).encode("utf-8")
        target.write_bytes(original)
        record = self.record(harness="codex", required=True)
        receipt = apply(str(target), record, PYTHON)
        changed = target.read_bytes()
        parsed = tomllib.loads(changed.decode("utf-8"))
        self.assertEqual(receipt["status"], "APPLIED")
        self.assertTrue(changed.startswith(prefix.encode("utf-8")))
        self.assertTrue(changed.endswith(suffix.encode("utf-8")))
        self.assertIn(b"args = ", changed)
        self.assertIn(b"required = true\r\n", changed)
        self.assertEqual(parsed["mcp_servers"][SERVER_KEY], overlay_entry(record, PYTHON)[SERVER_KEY])
        self.assertEqual(parsed["other"], {"value": "keep this section byte-for-byte"})

    def test_malformed_ambiguous_or_wrong_scope_rejected_without_write(self):
        fixtures = [("claude", b'{"mcpServers":{},"mcpServers":{}}'),
                    ("glm", b'{"mcp":{"servers":[]}}'),
                    ("claude", b'{"mcpServers":{"fleet-chrome-relay":42}}'),
                    ("codex", b'mcp_servers = { office = { command = "office" } }\n')]
        for name, original in fixtures:
            target = self.root / (secrets.token_hex(4) + (".toml" if name == "codex" else ".json"))
            target.write_bytes(original)
            with self.subTest(name=name), self.assertRaises(Reject):
                apply(str(target), self.record(harness=name), PYTHON)
            self.assertEqual(target.read_bytes(), original)

    def test_receipts_do_not_expose_config_values_or_paths_to_capabilities(self):
        raw = b'{"privateSetting":"SYNTHETIC-SECRET", "mcpServers":{"office":{"env":{"TEST":"SYNTHETIC-SECRET"}}}}'
        target = self.root / "config.json"
        target.write_bytes(raw)
        receipts = [dry_run(raw, self.record(), PYTHON), apply(str(target), self.record(), PYTHON)]
        for receipt in receipts:
            text = json.dumps(receipt)
            self.assertNotIn("SYNTHETIC-SECRET", text)
            self.assertEqual(set(receipt["overlay"][SERVER_KEY]), {"command", "args"})
            self.assertNotIn("session_id", text)

    def test_scope_checks_precede_private_config_reads(self):
        record = self.record(config=str(self.root / "owner-private.json"))
        enrollment_path = self.root / "enrollment.json"
        enrollment_path.write_text(json.dumps(record))
        for action in ("dry-run", "apply"):
            out, err = io.StringIO(), io.StringIO()
            with patch.object(registration, "_read_bounded", wraps=registration._read_bounded) as reader:
                with redirect_stdout(out), redirect_stderr(err):
                    code = main([action, "--enrollment", str(enrollment_path), "--config", record["config"]])
                self.assertEqual(code, 1)
                self.assertEqual([args.args[0] for args in reader.call_args_list], [str(enrollment_path)])
                self.assertEqual(json.loads(err.getvalue())["error_type"], "scope")

    def test_concurrent_change_is_retained_with_preimage(self):
        target = self.root / "native.json"
        original = canonical_bytes(existing_config())
        target.write_bytes(original)
        changed_elsewhere = b'{"mcpServers":{"hub":{"command":"changed-by-other-writer"}}}'
        save = registration._preimage
        def race(path, data):
            result = save(path, data)
            target.write_bytes(changed_elsewhere)
            return result
        with patch.object(registration, "_preimage", side_effect=race), self.assertRaises(Reject) as ctx:
            apply(str(target), self.record(), PYTHON)
        self.assertEqual(ctx.exception.reason, "concurrent_change")
        self.assertEqual(target.read_bytes(), changed_elsewhere)
        self.assertEqual(next(self.root.glob("native.json.preimage-*")).read_bytes(), original)
        self.assertEqual(len(list(self.root.glob(".relay-*.tmp"))), 1)

    def test_new_target_race_and_io_failure_never_overwrite_or_delete(self):
        target = self.root / "native.json"
        other = b'{"changed-by-other-writer":true}'
        save = registration._atomic_write
        def race(path, data, original):
            target.write_bytes(other)
            return save(path, data, original)
        with patch.object(registration, "_atomic_write", side_effect=race), self.assertRaises(Reject) as ctx:
            apply(str(target), self.record(), PYTHON)
        self.assertEqual(ctx.exception.reason, "concurrent_change")
        self.assertEqual(target.read_bytes(), other)
        with patch.object(registration.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                apply(str(target), self.record(), PYTHON)
        self.assertEqual(target.read_bytes(), other)
        self.assertEqual(len(list(self.root.glob(".relay-*.tmp"))), 1)

    def test_toml_multiline_strings_and_arrays_do_not_define_fake_scope(self):
        original = ('note = """example\n[mcp_servers.fleet-chrome-relay]\ncommand = \\\"fake\\\"\n"""\n'
                    'items = [\n["array-value"],\n]\n[mcp_servers.office]\ncommand = "office"\n').encode()
        target = self.root / "native.toml"
        target.write_bytes(original)
        record = self.record(harness="codex")
        apply(str(target), record, PYTHON)
        self.assertTrue(target.read_bytes().startswith(original))
        self.assertEqual(tomllib.loads(target.read_text(encoding="utf-8"))["mcp_servers"][SERVER_KEY],
                         overlay_entry(record, PYTHON)[SERVER_KEY])

    def test_validation_cli_reports_errors_with_nonzero_exit_and_redacted_records(self):
        target = self.root / "enrollment.json"
        target.write_text(json.dumps([self.record(), self.record(harness="unknown-cli")]))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["validate", "--enrollment", str(target)])
        self.assertEqual(code, 1)
        report = json.loads(out.getvalue())
        self.assertEqual(report["status"], "INVALID")
        self.assertNotIn(self.record()["config"], out.getvalue())
        self.assertEqual(report["errors"][0]["reason"], "unknown_harness")

    def test_registration_cli_dry_run_apply_noop_and_errors(self):
        record_path, target = self.root / "enrollment.json", self.root / "native.json"
        record_path.write_text(json.dumps(self.record()))
        command = [PYTHON, "-B", str(Path(__file__).resolve().parents[1] / "registration.py")]
        options = ["--enrollment", str(record_path), "--config", str(target), "--python", PYTHON]
        def run(action, extra=()):
            return subprocess.run(command + [action] + options + list(extra), capture_output=True,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                  shell=False, timeout=30)
        dry = run("dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(target.exists())
        applied = run("apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(json.loads(applied.stdout)["config_sha256"], json.loads(dry.stdout)["config_sha256"])
        self.assertEqual(json.loads(run("apply").stdout)["status"], "NOOP")
        self.assertNotEqual(run("dry-run", ["--session-id", "override"]).returncode, 0)

    def test_installer_manifest_and_synthetic_dry_run(self):
        source = Path(__file__).resolve().parents[1]
        script = (source / "install.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("$Version = '1.2.0-billingo3'", script)
        self.assertIn("'managed.py'", script)
        self.assertIn("'tests\\test_managed.py'", script)
        self.assertNotIn("test_registration_cli.py", script)
        target = self.root / "synthetic-install"
        result = subprocess.run(["pwsh.exe", "-NoProfile", "-NonInteractive", "-File",
                                 str(source / "install.ps1"), "-InstallRoot", str(target),
                                 "-PythonExe", PYTHON, "-SourceRoot", str(source), "-DryRun"],
                                capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                shell=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["version"], "1.2.0-billingo3")
        self.assertIn("managed.py", report["source_sha256"])
        self.assertIn("tests\\test_managed.py", report["source_sha256"])
        self.assertIn("tests\\test_login_gate.cjs", report["source_sha256"])
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
