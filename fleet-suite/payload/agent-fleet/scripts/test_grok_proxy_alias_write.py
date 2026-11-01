"""Per-session proxy alias: the broker-created junction <proxy_root>/project must
accept drive-absolute paths that name it exactly, like the equivalent
project/<...> request.

The grok ACP broker launches the grok CLI with cwd=proxy_root, where
proxy_root/project is the junction _prepare_runtime creates to the assigned
physical workspace. From that cwd the CLI emits the real absolute alias path
(for example C:\\ProgramData\\AgentFleet\\grok-acp\\<run_id>\\project\\probe.txt)
when it requests SearchReplace/Write. The broker must accept that path. The
alias is trusted only when it is the broker-created link for this session and
resolves to the assigned physical workspace; sibling sessions, plain
directories, foreign links, the root alone, traversal beyond the alias, .git
metadata, executable .fleet scripts, reparse/hardlink targets, read-only modes
and missing allow_once options stay refused. Diagnostics expose only the
canonical project/<...> rewrite, never the alias root, run id or absolute
workspace path."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grok_acp  # noqa: E402

SECRET = "TOP-SECRET-CONTENT-4711"
# Use a different run id than the hardcoded sibling path in
# fake_grok_acp_server.py ("run-20260820T000000Z-aaaaaaaaaa") so the alias-sibling
# scenario really targets another session, not this one.
RUN_ID = "run-20260820T000000Z-abcdef1234"
SIBLING_RUN_ID = "run-20260820T000000Z-aaaaaaaaaa"
SCHEMA = {
    "type": "object",
    "required": ["status", "summary"],
    "properties": {
        "status": {"type": "string"},
        "summary": {"type": "string"},
    },
}
FAKE_SERVER = Path(__file__).resolve().parent / "fake_grok_acp_server.py"


def request(raw_input, *, kind="edit", options=("allow_once", "reject_once")):
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "session/request_permission",
        "params": {
            "toolCall": {"kind": kind, "title": "C:\\fixtures\\someone\\secret\\x.txt", "rawInput": raw_input},
            "options": [{"kind": k, "optionId": f"opt-{k}", "name": SECRET} for k in options],
        },
    }


class _AliasFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        (self.workspace / "existing.txt").write_text("x", encoding="utf-8")
        (self.workspace / "sub").mkdir()
        self.proxy_base = self.base / "proxy"
        self.proxy_root = self.proxy_base / RUN_ID
        self.proxy_root.mkdir(parents=True)
        self.alias = self.proxy_root / "project"
        try:
            grok_acp._create_directory_link(self.workspace, self.alias)
        except (grok_acp.GrokAcpError, OSError) as exc:
            self.skipTest(f"cannot create a directory link: {exc}")

    def tearDown(self):
        self.tmp.cleanup()

    def allow(self, raw, *, allow_writes=True, proxy_project_root=None):
        return grok_acp._permission_allow_once_option(
            request(raw),
            allow_writes=allow_writes,
            workspace_root=self.workspace,
            proxy_project_root=proxy_project_root,
        )


class ProxyAliasAllowanceTests(_AliasFixture):
    """Drive-absolute paths that map through the broker-created alias are allowed."""

    def test_alias_absolute_existing_file_backslash_allowed(self):
        p = str(self.alias / "existing.txt")
        self.assertEqual(
            self.allow({"variant": "SearchReplace", "file_path": p}, proxy_project_root=self.alias),
            "opt-allow_once",
        )
        self.assertEqual(
            self.allow({"variant": "Write", "file_path": p, "content": "y"}, proxy_project_root=self.alias),
            "opt-allow_once",
        )

    def test_alias_absolute_existing_file_forward_slash_allowed(self):
        p = (self.alias / "existing.txt").as_posix()
        self.assertEqual(
            self.allow({"variant": "SearchReplace", "file_path": p}, proxy_project_root=self.alias),
            "opt-allow_once",
        )
        self.assertEqual(
            self.allow({"variant": "Write", "file_path": p, "content": "y"}, proxy_project_root=self.alias),
            "opt-allow_once",
        )

    def test_alias_absolute_existing_file_windows_case_variant_allowed(self):
        # Upper-case every directory component; Windows is case-insensitive, so
        # containment must still resolve through the alias mapping.
        parts = self.alias.parts
        upper_alias = Path(parts[0]) / "/".join(part.upper() for part in parts[1:])
        p = str(upper_alias / "existing.txt")
        self.assertEqual(
            self.allow({"variant": "SearchReplace", "file_path": p}, proxy_project_root=self.alias),
            "opt-allow_once",
        )
        self.assertEqual(
            self.allow({"variant": "Write", "file_path": p, "content": "y"}, proxy_project_root=self.alias),
            "opt-allow_once",
        )

    def test_alias_nested_new_file_allowed(self):
        p = str(self.alias / "sub" / "deeper" / "new.py")
        self.assertEqual(
            self.allow({"variant": "Write", "file_path": p, "content": ""}, proxy_project_root=self.alias),
            "opt-allow_once",
        )

    def test_relative_project_form_still_allowed(self):
        self.assertEqual(
            self.allow(
                {"variant": "SearchReplace", "file_path": "project/existing.txt"},
                proxy_project_root=self.alias,
            ),
            "opt-allow_once",
        )
        self.assertEqual(
            self.allow(
                {"variant": "Write", "file_path": "project/new.txt", "content": "a"},
                proxy_project_root=self.alias,
            ),
            "opt-allow_once",
        )


class ProxyAliasRefusalTests(_AliasFixture):
    """Drive-absolute paths through anything other than the broker-created alias are refused."""

    def test_alias_absolute_refused_without_proxy_project_root(self):
        # Back-compat: without proxy_project_root the alias absolute is outside workspace_root.
        p = str(self.alias / "existing.txt")
        self.assertIsNone(self.allow({"variant": "SearchReplace", "file_path": p}))
        self.assertIsNone(self.allow({"variant": "Write", "file_path": p, "content": "y"}))

    def test_alias_root_alone_refused(self):
        p = str(self.alias)
        self.assertIsNone(
            self.allow({"variant": "SearchReplace", "file_path": p}, proxy_project_root=self.alias)
        )
        self.assertIsNone(
            self.allow({"variant": "Write", "file_path": p, "content": "x"}, proxy_project_root=self.alias)
        )

    def test_bare_project_relative_root_refused(self):
        self.assertIsNone(
            self.allow({"variant": "SearchReplace", "file_path": "project"}, proxy_project_root=self.alias)
        )
        self.assertIsNone(
            self.allow({"variant": "Write", "file_path": "project", "content": "x"}, proxy_project_root=self.alias)
        )

    def test_plain_directory_as_proxy_project_root_refused(self):
        # The workspace (a plain directory, not a link) is rejected as proxy_project_root.
        self.assertIsNone(
            self.allow(
                {"variant": "SearchReplace", "file_path": str(self.alias / "existing.txt")},
                proxy_project_root=self.workspace,
            )
        )

    def test_alias_pointing_to_wrong_directory_refused(self):
        wrong_target = self.base / "wrong"
        wrong_target.mkdir()
        wrong_link = self.proxy_base / "wrong-link" / "project"
        wrong_link.parent.mkdir(parents=True)
        try:
            grok_acp._create_directory_link(wrong_target, wrong_link)
        except (grok_acp.GrokAcpError, OSError) as exc:
            self.skipTest(f"cannot create a directory link: {exc}")
        try:
            self.assertIsNone(
                self.allow(
                    {"variant": "SearchReplace", "file_path": str(wrong_link / "existing.txt")},
                    proxy_project_root=wrong_link,
                )
            )
        finally:
            grok_acp._remove_directory_link(wrong_link)

    def test_sibling_proxy_root_alias_refused(self):
        sibling_proxy = self.proxy_base / SIBLING_RUN_ID
        sibling_proxy.mkdir(parents=True)
        sibling_alias = sibling_proxy / "project"
        try:
            grok_acp._create_directory_link(self.workspace, sibling_alias)
        except (grok_acp.GrokAcpError, OSError) as exc:
            self.skipTest(f"cannot create a directory link: {exc}")
        try:
            # The other session's alias is not the broker-created link for this session,
            # so its absolute path is refused even when our own alias is registered.
            self.assertIsNone(
                self.allow(
                    {"variant": "SearchReplace", "file_path": str(sibling_alias / "existing.txt")},
                    proxy_project_root=self.alias,
                )
            )
        finally:
            grok_acp._remove_directory_link(sibling_alias)

    def test_traversal_through_alias_refused(self):
        # alias/../outside.txt: lexical check refuses "..".
        p = str(self.alias) + "\\..\\outside.txt"
        self.assertIsNone(
            self.allow({"variant": "Write", "file_path": p, "content": "x"}, proxy_project_root=self.alias)
        )
        # alias/sub/../../x: also refuses "..".
        p = str(self.alias / "sub") + "\\..\\..\\x"
        self.assertIsNone(
            self.allow({"variant": "Write", "file_path": p, "content": "x"}, proxy_project_root=self.alias)
        )
        # Relative project/sub/.. form is refused too.
        self.assertIsNone(
            self.allow(
                {"variant": "Write", "file_path": "project/sub/..", "content": "x"},
                proxy_project_root=self.alias,
            )
        )

    def test_alias_dot_git_refused(self):
        git = self.workspace / ".git"
        git.mkdir()
        (git / "config").write_text("ref: refs/heads/main", encoding="utf-8")
        p = str(self.alias / ".git" / "config")
        self.assertIsNone(
            self.allow({"variant": "SearchReplace", "file_path": p}, proxy_project_root=self.alias)
        )
        self.assertIsNone(
            self.allow({"variant": "Write", "file_path": p, "content": "x"}, proxy_project_root=self.alias)
        )

    def test_alias_dot_fleet_executable_refused(self):
        fleet = self.workspace / ".fleet"
        fleet.mkdir()
        ps1 = fleet / "x.ps1"
        ps1.write_text("echo hi", encoding="utf-8")
        p = str(ps1)
        self.assertIsNone(
            self.allow({"variant": "SearchReplace", "file_path": p}, proxy_project_root=self.alias)
        )
        self.assertIsNone(
            self.allow({"variant": "Write", "file_path": p, "content": "x"}, proxy_project_root=self.alias)
        )

    def test_other_drive_refused(self):
        workspace_drive = str(self.workspace)[0].lower()
        other = "Z" if workspace_drive != "z" else "Y"
        p = f"{other}:\\some\\path\\file.txt"
        self.assertIsNone(
            self.allow({"variant": "SearchReplace", "file_path": p}, proxy_project_root=self.alias)
        )

    def test_read_only_mode_refuses_alias(self):
        p = str(self.alias / "existing.txt")
        self.assertIsNone(
            self.allow({"variant": "SearchReplace", "file_path": p}, allow_writes=False, proxy_project_root=self.alias)
        )
        self.assertIsNone(
            self.allow({"variant": "Write", "file_path": p, "content": "x"}, allow_writes=False, proxy_project_root=self.alias)
        )

    def test_missing_allow_once_refuses_alias(self):
        msg = request(
            {"variant": "Write", "file_path": str(self.alias / "existing.txt"), "content": "x"},
            options=("reject_once",),
        )
        self.assertIsNone(
            grok_acp._permission_allow_once_option(
                msg,
                allow_writes=True,
                workspace_root=self.workspace,
                proxy_project_root=self.alias,
            )
        )

    def test_write_to_directory_through_alias_refused(self):
        d = self.workspace / "adir"
        d.mkdir()
        p = str(self.alias / "adir")
        self.assertIsNone(
            self.allow({"variant": "Write", "file_path": p, "content": "x"}, proxy_project_root=self.alias)
        )

    def test_symlink_component_below_alias_refused(self):
        outside = self.base / "outside_dir"
        outside.mkdir()
        link = self.workspace / "jn"
        try:
            grok_acp._create_directory_link(outside, link)
        except (grok_acp.GrokAcpError, OSError) as exc:
            self.skipTest(f"cannot create a directory link: {exc}")
        p = str(self.alias / "jn" / "new.txt")
        self.assertIsNone(
            self.allow({"variant": "Write", "file_path": p, "content": "x"}, proxy_project_root=self.alias)
        )


class ProxyAliasDiagnosticTests(_AliasFixture):
    """Diagnostics expose only project/<...> for alias-internal paths, never the alias root or run id."""

    def test_alias_internal_path_exposed_as_project_relative(self):
        # Read-only mode denies; the diagnostic must use the project/ rewrite with no leak.
        msg = request(
            {"variant": "Write", "file_path": str(self.alias / "existing.txt"), "content": "x"},
            options=("reject_once",),
        )
        record = grok_acp.classify_permission_denial(
            msg,
            allow_writes=False,
            workspace_root=self.workspace,
            deny_all_tools=False,
            proxy_project_root=self.alias,
        )
        dumped = json.dumps(record)
        self.assertEqual(record["path_shape"], "project_relative")
        self.assertEqual(record["project_relative_path"], "project/existing.txt")
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn(str(self.proxy_root), dumped)
        self.assertNotIn(str(self.alias), dumped)
        self.assertNotIn(str(self.workspace), dumped)
        self.assertNotIn(self.tmp.name, dumped)

    def test_alias_sibling_path_exposes_drive_absolute(self):
        # Build a sibling proxy alias at a different run id pointing to the same workspace.
        sibling_proxy = self.proxy_base / SIBLING_RUN_ID
        sibling_proxy.mkdir(parents=True)
        sibling_alias = sibling_proxy / "project"
        try:
            grok_acp._create_directory_link(self.workspace, sibling_alias)
        except (grok_acp.GrokAcpError, OSError) as exc:
            self.skipTest(f"cannot create a directory link: {exc}")
        try:
            msg = request({"variant": "SearchReplace", "file_path": str(sibling_alias / "existing.txt")})
            record = grok_acp.classify_permission_denial(
                msg,
                allow_writes=True,
                workspace_root=self.workspace,
                deny_all_tools=False,
                proxy_project_root=self.alias,
            )
            dumped = json.dumps(record)
            self.assertEqual(record["path_shape"], "drive_absolute")
            self.assertNotIn("project_relative_path", record)
            self.assertNotIn(str(sibling_proxy), dumped)
            self.assertNotIn(str(self.proxy_root), dumped)
            self.assertNotIn(str(self.alias), dumped)
            self.assertNotIn(str(self.workspace), dumped)
            self.assertNotIn(self.tmp.name, dumped)
            self.assertNotIn(SECRET, dumped)
        finally:
            grok_acp._remove_directory_link(sibling_alias)


class ProxyAliasEndToEndTests(unittest.TestCase):
    """End-to-end run_session with the fake server: alias-absolute allow and deny."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run_dir = self.root / RUN_ID
        self.workspace = self.root / "workspace"
        self.proxy_base = self.root / "proxy"
        self.auth_path = self.root / "auth.json"
        self.run_dir.mkdir()
        self.workspace.mkdir()
        (self.workspace / "fixture.txt").write_text("bounded", encoding="utf-8")
        self.auth_path.write_text('{"token":"fixture-only"}', encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def command(self, *, mode="implement"):
        return grok_acp.build_command(
            sys.executable,
            run_dir=self.run_dir,
            run_id=RUN_ID,
            mode=mode,
            model="grok-4.7",
            reasoning_effort="medium",
            complexity="medium",
            handoff_schema=SCHEMA,
            max_model_tool_calls=8,
            proxy_base=self.proxy_base,
            executable_args=(str(FAKE_SERVER),),
        )

    def environment(self, scenario):
        base = dict(os.environ)
        base["GROK_API_KEY"] = "must-be-removed"
        base["XAI_API_KEY"] = "must-also-be-removed"
        base["FAKE_GROK_ACP_SCENARIO"] = scenario
        return grok_acp.build_environment(base, run_dir=self.run_dir, auth_path=self.auth_path)

    def execute(self, scenario, *, mode="implement"):
        return grok_acp.run_session(
            command=self.command(mode=mode),
            run_dir=self.run_dir,
            run_id=RUN_ID,
            workspace=self.workspace,
            mode=mode,
            prompt="Inspect and return the handoff.",
            timeout=30,
            max_model_tool_calls=8,
            max_additional_prompts=0,
            env=self.environment(scenario),
            continuation_decider=lambda _value: None,
            proxy_base=self.proxy_base,
        )

    def test_permission_write_allow_alias(self):
        result = self.execute("permission-write-allow-alias")
        envelope = json.loads(str(result["stdout"]))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(envelope["_fleet"]["permission_requests_approved"], 1)
        self.assertEqual(envelope["_fleet"]["permission_requests_denied"], 0)

    def test_permission_write_deny_alias_sibling(self):
        result = self.execute("permission-write-deny-alias-sibling")
        envelope = json.loads(str(result["stdout"]))
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(envelope["_fleet"]["permission_requests_denied"], 1)
        self.assertEqual(envelope["_fleet"]["permission_requests_approved"], 0)


if __name__ == "__main__":
    unittest.main()