from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from support import PACKAGE_ROOT, CoordTestCase

COORD_PY = PACKAGE_ROOT / "coord.py"
COORD_HOOK_PY = PACKAGE_ROOT / "coord_hook.py"


class CliTests(CoordTestCase):
    def _run(self, args: list[str], extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["COORDFS_HOME"] = str(self.home)
        env["COORDFS_SESSION_ID"] = "cli-test-session"
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-I", str(COORD_PY), *args],
            cwd=str(self.cwd),
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def _payload(self, args: list[str], extra_env: dict[str, str] | None = None) -> tuple[int, dict]:
        completed = self._run(args, extra_env=extra_env)
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        return completed.returncode, json.loads(completed.stdout)

    def test_cli_commands_json_default_and_warning(self) -> None:
        code, claimed = self._payload(["claim", "--resource", "data:cli", "--agent-id", "agent-a", "--ttl", "30", "--task-id", "task-9"])
        self.assertEqual(code, 0)
        self.assertEqual(claimed["status"], "ok")
        self.assertEqual(claimed["lease"]["meta"]["task_id"], "task-9")

        code, warning = self._payload(["--json", "claim", "data:cli", "--agent-id", "agent-b"])
        self.assertEqual(code, 0)
        self.assertEqual(warning["status"], "warning")
        self.assertEqual(warning["code"], "held")
        self.assertEqual(warning["holder_agent"], claimed["holder_agent"])

        code, who = self._payload(["who", "--resource", "data:cli"])
        self.assertEqual(code, 0)
        self.assertEqual(who["holder_agent"], claimed["holder_agent"])

        code, listed = self._payload(["ls"])
        self.assertEqual(code, 0)
        self.assertEqual(listed["count"], 1)

        code, renewed = self._payload(["renew", "--resource", "data:cli", "--agent-id", "agent-a", "--ttl", "40"])
        self.assertEqual(code, 0)
        self.assertEqual(renewed["code"], "renewed")

        code, released = self._payload(["release", "--resource", "data:cli", "--agent-id", "agent-a"])
        self.assertEqual(code, 0)
        self.assertEqual(released["code"], "released")

        code, swept = self._payload(["sweep"])
        self.assertEqual(code, 0)
        self.assertEqual(swept["code"], "swept")

        code, health = self._payload(["doctor"])
        self.assertEqual(code, 0)
        self.assertIn(health["status"], {"ok", "warning"})
        self.assertEqual(Path(health["home"]), self.home)

    def test_changed_file_cli_release_commits(self) -> None:
        target = self.cwd / "cli-file.txt"
        target.write_text("a", encoding="utf-8")
        code, claimed = self._payload(["claim", "--resource", str(target), "--agent-id", "agent-a", "--kind", "path"])
        self.assertEqual(code, 0)
        target.write_text("b", encoding="utf-8")
        code, released = self._payload(["release", "--resource", str(target), "--agent-id", "agent-a", "--kind", "path"])
        self.assertEqual(code, 0)
        self.assertEqual(released["code"], "released")
        self.assertIn("committed_hash", released)

    def test_same_harness_in_another_session_cannot_release_lease(self) -> None:
        code, claimed = self._payload(
            ["claim", "data:session-isolation", "--agent-id", "grok", "--ttl", "30"],
            {"COORDFS_SESSION_ID": "session-one"},
        )
        self.assertEqual(code, 0)
        self.assertIn("session-one", claimed["holder_agent"])

        code, blocked = self._payload(
            ["release", "data:session-isolation", "--agent-id", "grok"],
            {"COORDFS_SESSION_ID": "session-two"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(blocked["status"], "warning")
        self.assertEqual(blocked["holder_agent"], claimed["holder_agent"])

    def test_hook_entrypoint_runs_under_isolated_python(self) -> None:
        env = os.environ.copy()
        env["COORDFS_HOME"] = str(self.home)
        completed = subprocess.run(
            [sys.executable, "-I", str(COORD_HOOK_PY)],
            cwd=str(self.cwd),
            env=env,
            input="{}\n",
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {})

    def _run_no_session(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Run a CLI process with every session-id signal cleared.

        The identity module falls back to ``process-<pid>-<uuid>`` when no
        session-id env var is set, so two invocations of this helper produce
        different agent_ids even with the same ``--agent-id`` flag. That is
        exactly the on-machine scenario the not_holder payload must explain.
        """
        env = os.environ.copy()
        env["COORDFS_HOME"] = str(self.home)
        for key in (
            "COORDFS_SESSION_ID",
            "AGENT_SESSION_ID",
            "CLAUDE_CODE_SESSION_ID",
            "CODEX_SESSION_ID",
            "CODEX_THREAD_ID",
        ):
            env.pop(key, None)
        return subprocess.run(
            [sys.executable, "-I", str(COORD_PY), *args],
            cwd=str(self.cwd),
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def test_release_by_non_holder_reports_weak_caller_identity(self) -> None:
        """Process 1 claims without session-id, process 2 releases without session-id.

        Each subprocess picks up a different process-fallback session, so they
        look like different agents. The release payload must surface the weak
        caller identity, the missing-session hint, and the identity strength
        without dropping the holder_agent that protects the lease.
        """
        claim_proc = self._run_no_session(
            ["claim", "--resource", "data:not-holder-release", "--agent-id", "grok", "--ttl", "30"]
        )
        self.assertEqual(claim_proc.returncode, 0, claim_proc.stderr)
        claimed = json.loads(claim_proc.stdout)
        self.assertEqual(claimed["status"], "ok")
        holder = claimed["holder_agent"]

        release_proc = self._run_no_session(
            ["release", "--resource", "data:not-holder-release", "--agent-id", "grok"]
        )
        self.assertEqual(release_proc.returncode, 0, release_proc.stderr)
        blocked = json.loads(release_proc.stdout)
        self.assertEqual(blocked["status"], "warning")
        self.assertEqual(blocked["code"], "not_holder")
        self.assertEqual(blocked["holder_agent"], holder)
        self.assertNotEqual(blocked["caller_agent_id"], holder)
        self.assertEqual(blocked["caller_strength"], "weak")
        self.assertIn("weak", blocked["message"])
        self.assertIn("--session-id", blocked["message"])
        self.assertIn("COORDFS_SESSION_ID", blocked["message"])
        self.assertIn(holder, blocked["message"])

    def test_renew_by_non_holder_reports_strong_caller_identity(self) -> None:
        """Same identity shape for renew, but with a strong (env-supplied) session.

        The holder and the caller both have strong identities - they differ
        only because they were started in different sessions. The response
        keeps the ``not_holder`` code, points at the holder, and surfaces the
        strong strength so the caller can tell identity matched in type but
        not in value.
        """
        code, claimed = self._payload(
            ["claim", "--resource", "data:not-holder-renew", "--agent-id", "grok", "--ttl", "30"],
            {"COORDFS_SESSION_ID": "owner-session"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(claimed["status"], "ok")

        code, blocked = self._payload(
            ["renew", "--resource", "data:not-holder-renew", "--agent-id", "grok", "--ttl", "45"],
            {"COORDFS_SESSION_ID": "intruder-session"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(blocked["status"], "warning")
        self.assertEqual(blocked["code"], "not_holder")
        self.assertEqual(blocked["holder_agent"], claimed["holder_agent"])
        self.assertEqual(blocked["caller_strength"], "strong")
        self.assertIn(claimed["holder_agent"], blocked["message"])
        self.assertIn("--session-id", blocked["message"])

    def test_release_with_matching_session_id_succeeds_across_processes(self) -> None:
        """claim and release from different subprocesses with the same session id.

        The two subprocesses share ``COORDFS_SESSION_ID=shared`` and therefore
        derive the same strong agent_id; release must return ``released`` and
        must NOT take the not_holder branch.
        """
        shared = {"COORDFS_SESSION_ID": "shared-session-1"}
        code, claimed = self._payload(
            ["claim", "--resource", "data:matched-release", "--agent-id", "grok", "--ttl", "30"],
            shared,
        )
        self.assertEqual(code, 0)
        self.assertEqual(claimed["status"], "ok")

        code, released = self._payload(
            ["release", "--resource", "data:matched-release", "--agent-id", "grok"],
            shared,
        )
        self.assertEqual(code, 0)
        self.assertEqual(released["status"], "ok")
        self.assertEqual(released["code"], "released")
        self.assertEqual(released["holder_agent"], claimed["holder_agent"])
        self.assertNotIn("caller_agent_id", released)

    def test_claim_by_second_agent_still_returns_held_code(self) -> None:
        """The non-holder branch for ``claim`` is unchanged.

        A claim that races an existing live lease must still emit
        ``status=warning, code=held`` (the existing contract). This guards the
        "claim without changes" boundary of the task and keeps the warning
        payload compatible with anything that pattern-matches on ``held``.
        """
        code, first = self._payload(
            ["claim", "--resource", "data:claim-held-unchanged", "--agent-id", "agent-a", "--ttl", "30"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(first["status"], "ok")

        code, warned = self._payload(
            ["claim", "--resource", "data:claim-held-unchanged", "--agent-id", "agent-b", "--ttl", "30"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(warned["status"], "warning")
        self.assertEqual(warned["code"], "held")
        self.assertEqual(warned["holder_agent"], first["holder_agent"])
        self.assertNotIn("caller_agent_id", warned)

    def test_release_by_non_holder_does_not_remove_lease(self) -> None:
        """A not_holder release must not actually unlink the lock.

        The lease must still be held afterwards; only the same identity that
        claimed can release. This pins the "non-owner still cannot release"
        boundary of the task.
        """
        claim_proc = self._run_no_session(
            ["claim", "--resource", "data:not-released", "--agent-id", "grok", "--ttl", "30"]
        )
        self.assertEqual(claim_proc.returncode, 0, claim_proc.stderr)
        claimed = json.loads(claim_proc.stdout)

        release_proc = self._run_no_session(
            ["release", "--resource", "data:not-released", "--agent-id", "grok"]
        )
        self.assertEqual(release_proc.returncode, 0, release_proc.stderr)
        blocked = json.loads(release_proc.stdout)
        self.assertEqual(blocked["code"], "not_holder")

        # The lease is still owned by the original claimant; who still reports them.
        code, who = self._payload(["who", "--resource", "data:not-released"])
        self.assertEqual(code, 0)
        self.assertEqual(who["holder_agent"], claimed["holder_agent"])

    def test_not_holder_response_carries_ttl_hint_for_recovery(self) -> None:
        """The not_holder payload includes the same ttl hint shape as ``held``.

        The caller can use the ttl hint to decide whether to wait for the
        holder to release or to surface a stale-lease sweep. We keep the
        keys stable (``ttl_remaining_s``, ``ttl_hint_s``, ``expires_at``)
        so existing consumers that pattern-match on these fields continue
        to work.
        """
        code, claimed = self._payload(
            ["claim", "--resource", "data:ttl-hint", "--agent-id", "grok", "--ttl", "120"],
            {"COORDFS_SESSION_ID": "ttl-owner"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(claimed["status"], "ok")

        code, blocked = self._payload(
            ["release", "--resource", "data:ttl-hint", "--agent-id", "grok"],
            {"COORDFS_SESSION_ID": "ttl-other"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(blocked["status"], "warning")
        self.assertEqual(blocked["code"], "not_holder")
        for key in ("ttl_remaining_s", "ttl_hint_s", "expires_at"):
            self.assertIn(key, blocked)
        self.assertEqual(blocked["ttl_remaining_s"], blocked["ttl_hint_s"])
        self.assertGreater(blocked["ttl_remaining_s"], 0)
        self.assertLessEqual(blocked["ttl_remaining_s"], 120)
