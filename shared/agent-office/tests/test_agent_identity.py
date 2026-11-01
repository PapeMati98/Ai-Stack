from __future__ import annotations

import json
import os
import subprocess
import sys

from support import PACKAGE_ROOT, CoordTestCase

from coordfs.identity import agent_identity


class AgentIdentityTests(CoordTestCase):
    def _identity_from_process(
        self,
        *,
        harness: str = "claude-code",
        claude_session: str | None = None,
        coordfs_session: str | None = None,
    ) -> dict[str, str]:
        environment = os.environ.copy()
        for name in ("COORDFS_SESSION_ID", "AGENT_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            environment.pop(name, None)
        if claude_session is not None:
            environment["CLAUDE_CODE_SESSION_ID"] = claude_session
        if coordfs_session is not None:
            environment["COORDFS_SESSION_ID"] = coordfs_session
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json, sys; "
                    "from coordfs.identity import agent_identity; "
                    "print(json.dumps(agent_identity(sys.argv[1], host='workstation')))"
                ),
                harness,
            ],
            cwd=PACKAGE_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return json.loads(completed.stdout)

    def test_two_sessions_of_same_harness_have_distinct_agent_ids(self) -> None:
        first = agent_identity("grok", host="desktop-f96ta3e", session_id="session-a")
        second = agent_identity("grok", host="desktop-f96ta3e", session_id="session-b")
        self.assertEqual(first["strength"], "strong")
        self.assertEqual(second["strength"], "strong")
        self.assertNotEqual(first["agent_id"], second["agent_id"])
        self.assertEqual(first["agent_id"], "grok-desktop-f96ta3e-session-a")

    def test_claude_session_env_is_a_strong_identity(self) -> None:
        identity = agent_identity(
            "claude",
            host="workstation",
            env={"CLAUDE_CODE_SESSION_ID": "claude-session-7"},
        )
        self.assertEqual(identity["agent_id"], "claude-workstation-claude-session-7")
        self.assertEqual(identity["strength"], "strong")
        self.assertEqual(identity["source"], "CLAUDE_CODE_SESSION_ID")

    def test_claude_code_session_env_is_stable_across_processes(self) -> None:
        first = self._identity_from_process(harness="Claude-Code", claude_session="session-7")
        second = self._identity_from_process(harness="Claude-Code", claude_session="session-7")
        self.assertEqual(first["agent_id"], second["agent_id"])
        self.assertEqual(first["agent_id"], "claude-code-workstation-session-7")
        self.assertEqual(first["strength"], "strong")
        self.assertEqual(first["source"], "CLAUDE_CODE_SESSION_ID")

    def test_claude_code_session_env_distinguishes_sessions(self) -> None:
        first = self._identity_from_process(claude_session="session-a")
        second = self._identity_from_process(claude_session="session-b")
        self.assertNotEqual(first["agent_id"], second["agent_id"])

    def test_empty_claude_code_session_env_uses_weak_process_fallback(self) -> None:
        identity = self._identity_from_process(claude_session="")
        self.assertEqual(identity["strength"], "weak")
        self.assertEqual(identity["source"], "process-fallback")
        self.assertTrue(identity["session"].startswith("process-"))

    def test_coordfs_session_env_precedes_claude_code_session_env(self) -> None:
        identity = self._identity_from_process(
            claude_session="claude-session",
            coordfs_session="coordfs-session",
        )
        self.assertEqual(identity["agent_id"], "claude-code-workstation-coordfs-session")
        self.assertEqual(identity["source"], "COORDFS_SESSION_ID")

    def test_explicit_session_precedes_claude_code_session_env(self) -> None:
        identity = agent_identity(
            "claude-code",
            host="workstation",
            session_id="explicit-session",
            env={"CLAUDE_CODE_SESSION_ID": "claude-session"},
        )
        self.assertEqual(identity["agent_id"], "claude-code-workstation-explicit-session")
        self.assertEqual(identity["source"], "explicit-session-id")

    def test_codex_session_env_is_a_strong_identity(self) -> None:
        identity = agent_identity(
            "codex",
            host="workstation",
            env={"CODEX_SESSION_ID": "codex-session-7", "CODEX_THREAD_ID": "thread-7"},
        )
        self.assertEqual(identity["agent_id"], "codex-workstation-codex-session-7")
        self.assertEqual(identity["strength"], "strong")
        self.assertEqual(identity["source"], "CODEX_SESSION_ID")

    def test_codex_thread_id_is_a_compatible_fallback(self) -> None:
        identity = agent_identity("codex", host="workstation", env={"CODEX_THREAD_ID": "thread-7"})
        self.assertEqual(identity["agent_id"], "codex-workstation-thread-7")
        self.assertEqual(identity["strength"], "strong")
        self.assertEqual(identity["source"], "CODEX_THREAD_ID")

    def test_missing_session_signal_is_explicitly_weak_but_process_stable(self) -> None:
        first = agent_identity("qwen", host="workstation", env={})
        second = agent_identity("qwen", host="workstation", env={})
        self.assertEqual(first["agent_id"], second["agent_id"])
        self.assertEqual(first["strength"], "weak")
        self.assertEqual(first["source"], "process-fallback")
