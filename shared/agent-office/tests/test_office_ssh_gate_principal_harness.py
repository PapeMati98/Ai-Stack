"""F2: each SSH principal may present only its own harness names (no look-alike ids)."""

from __future__ import annotations

import io
import json

from office_support import OfficeTestCase

from coordfs.office_ssh_gate import PRINCIPAL_HARNESSES, PRINCIPALS, run


class PrincipalHarnessAllowlistTests(OfficeTestCase):
    def call(self, identity: dict, *, principal: str, peer_host: str | None,
             op: str = "presence", args: dict | None = None) -> tuple[int, dict]:
        header = {"v": 1, "op": op, "identity": identity, "args": {} if args is None else args}
        stdout = io.BytesIO()
        code = run(io.BytesIO(json.dumps(header).encode("utf-8") + b"\n"), stdout,
                   principal=principal, home=self.home, environ={}, peer_host=peer_host)
        return code, json.loads(stdout.getvalue())

    def assert_refused(self, code: int, result: dict, expected: str | set) -> None:
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "error")
        self.assertIn(result["code"], {expected} if isinstance(expected, str) else expected)
        self.assertFalse(self.home.exists(), "a refused request must not create the Office home")

    def test_every_principal_has_an_explicit_harness_allowlist(self) -> None:
        self.assertEqual(set(PRINCIPAL_HARNESSES), set(PRINCIPALS))
        self.assertEqual(PRINCIPAL_HARNESSES["hermes"], frozenset({"hermes"}))
        self.assertNotIn("hermes", PRINCIPAL_HARNESSES["ws-office"])

    def test_workstation_cannot_mint_hermes_look_alike(self) -> None:
        # Reproduces UAT ade7273f H4a: ws-office minted hermes-ailab-uat-spoof.
        code, result = self.call({"harness": "hermes", "session_id": "uat-spoof"},
                                 principal="ws-office", peer_host="ailab")
        self.assert_refused(code, result, "harness_not_allowed")

    def test_non_canonical_spellings_are_stopped_by_header_validation(self) -> None:
        # A different layer than F2: the header grammar refuses these before the allowlist.
        for harness in ("Hermes", " HERMES ", "hermes​"):
            with self.subTest(harness=harness):
                code, result = self.call({"harness": harness, "session_id": "uat-spoof"},
                                         principal="ws-office", peer_host="ailab")
                self.assert_refused(code, result, "invalid_identity")

    def test_allowlist_applies_before_mcp_and_tool_call_ops(self) -> None:
        for op, args in (("mcp", {}), ("tool-call", {"name": "office_who", "arguments": {}})):
            with self.subTest(op=op):
                code, result = self.call({"harness": "hermes", "session_id": "uat-spoof"},
                                         principal="ws-office", peer_host="ailab", op=op, args=args)
                self.assert_refused(code, result, "harness_not_allowed")

    def test_workstation_legacy_id_cannot_use_reserved_hermes_prefix(self) -> None:
        # Sealed 0.1.9 refused this too, but as invalid_identity (evidence red-before-fix.txt);
        # F2 refuses it explicitly in the gate.
        code, result = self.call({"agent_id": "hermes-lab-server-uat-h1"},
                                 principal="ws-office", peer_host=None)
        self.assert_refused(code, result, "identity_mismatch")

    def test_hermes_legacy_id_cannot_claim_another_agent(self) -> None:
        for peer_host in (None, "lab-server"):
            with self.subTest(peer_host=peer_host):
                code, result = self.call({"agent_id": "claude-ailab-uat-spoof"},
                                         principal="hermes", peer_host=peer_host)
                self.assert_refused(code, result,
                                    "identity_mismatch" if peer_host is None else "peer_host_mismatch")

    def test_known_runtime_harnesses_are_allowlisted(self) -> None:
        # Vocabulary drift guard: a harness the stack knows must not be silently locked out.
        from coordfs.identity import SESSION_ENV_BY_HARNESS
        from coordfs.projects import ACTIVITY_PROVIDERS, CONNECTED_HARNESSES
        allowed = set().union(*PRINCIPAL_HARNESSES.values())
        known = set(SESSION_ENV_BY_HARNESS) | set(ACTIVITY_PROVIDERS) | set(CONNECTED_HARNESSES)
        # "mateusz" is the owner identity, not a remote CLI harness.
        self.assertEqual(known - allowed, {"mateusz"})
        self.assertNotIn("mateusz", allowed)
        for harness in ("kimi-code", "kimi-desktop", "qwen-code", "qwen-local", "qwen-local-5070ti"):
            with self.subTest(harness=harness):
                code, result = self.call({"harness": harness, "session_id": "s1"},
                                         principal="ws-office", peer_host="ailab",
                                         args={"status": "uat", "ttl_s": 60})
                self.assertEqual((code, result["status"]), (0, "ok"))
        self.assertIn("unknown", PRINCIPAL_HARNESSES["ws-office"])  # documented default wire harness

    def test_unknown_workstation_harness_fails_closed(self) -> None:
        code, result = self.call({"harness": "totally-new-cli", "session_id": "s1"},
                                 principal="ws-office", peer_host="ailab")
        self.assert_refused(code, result, "harness_not_allowed")

    def test_hermes_principal_accepts_only_the_hermes_harness(self) -> None:
        for harness in ("hermes-evil", "claude", "codex"):
            with self.subTest(harness=harness):
                code, result = self.call({"harness": harness, "session_id": "s1"},
                                         principal="hermes", peer_host="lab-server")
                self.assert_refused(code, result, "harness_not_allowed")

    def test_cross_host_and_unknown_principal_stay_refused(self) -> None:
        code, result = self.call({"harness": "claude", "host": "lab-server", "session_id": "s1"},
                                 principal="ws-office", peer_host="ailab")
        self.assert_refused(code, result, "peer_host_mismatch")
        code, result = self.call({"agent_id": "hermes-lab-server-uat-h1"},
                                 principal="ws-office", peer_host="ailab")
        # The reserved-prefix check runs before the host binding.
        self.assert_refused(code, result, "identity_mismatch")
        code, result = self.call({"harness": "claude", "session_id": "s1"},
                                 principal="owner", peer_host="ailab")
        self.assert_refused(code, result, "unknown_principal")

    def test_allowed_harnesses_still_work_for_both_principals(self) -> None:
        code, result = self.call({"harness": "claude", "session_id": "uat-w1"},
                                 principal="ws-office", peer_host="ailab",
                                 args={"status": "uat", "ttl_s": 60})
        self.assertEqual((code, result["status"]), (0, "ok"))
        self.assertEqual(result["agent"], "claude-ailab-uat-w1")
        code, result = self.call({"harness": "hermes", "session_id": "uat-h1"},
                                 principal="hermes", peer_host="lab-server",
                                 args={"status": "uat", "ttl_s": 60})
        self.assertEqual((code, result["status"]), (0, "ok"))
        self.assertEqual(result["agent"], "hermes-lab-server-uat-h1")
