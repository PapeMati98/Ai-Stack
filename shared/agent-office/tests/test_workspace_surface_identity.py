"""Unit tests for workspace_surface identity checks, session normalization, and codex relay bindings discovery."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
COORDFS_PATH = REPO_ROOT / "shared" / "agent-office" / "release-candidates" / "20261021-callback-attestation" / "windows"

if str(COORDFS_PATH) not in sys.path:
    sys.path.insert(0, str(COORDFS_PATH))

from coordfs.workspace_surface import (
    _norm_session,
    _prefix_actor_allowed,
    operator_config,
    call,
    WorkspaceError,
    _STRONG_AGENT_PREFIXES,
    _AGENT_ROLES,
)
from coordfs.workspace_tasks import WorkspaceTasks, WorkspaceConfig
from coordfs.codex_relay_cycle import (
    discover_active_codex_bindings,
    BINDING_IDS,
    DRIVER,
)


class TestNormSession(unittest.TestCase):
    def test_strip_local_prefix(self):
        self.assertEqual(_norm_session("local_abc123"), "abc123")
        self.assertEqual(_norm_session("local_thread-xyz"), "thread-xyz")

    def test_strip_only_first_local_prefix(self):
        self.assertEqual(_norm_session("local_local_abc"), "local_abc")

    def test_keep_sess_prefix(self):
        self.assertEqual(_norm_session("sess_12345"), "sess_12345")

    def test_unprefixed_and_empty(self):
        self.assertEqual(_norm_session("abc-123"), "abc-123")
        self.assertEqual(_norm_session(""), "")

    def test_non_string_values(self):
        self.assertIsNone(_norm_session(None))
        self.assertEqual(_norm_session(12345), 12345)
        self.assertEqual(_norm_session({"id": 1}), {"id": 1})


class TestPrefixActorAllowed(unittest.TestCase):
    def test_weak_or_missing_identity(self):
        actor = {"role": "project-agent"}
        self.assertFalse(_prefix_actor_allowed({"strength": "weak", "agent_id": "claude-ailab-1"}, actor, "ws-office", ()))
        self.assertFalse(_prefix_actor_allowed({"agent_id": "claude-ailab-1"}, actor, "ws-office", ()))
        self.assertFalse(_prefix_actor_allowed({"strength": "strong", "agent_id": None}, actor, "ws-office", ()))
        self.assertFalse(_prefix_actor_allowed({"strength": "strong", "agent_id": 123}, actor, "ws-office", ()))

    def test_actor_role_rejected(self):
        identity = {"strength": "strong", "agent_id": "claude-ailab-1"}
        for role in ("owner", "admin", "user", "guest", "operator"):
            self.assertFalse(_prefix_actor_allowed(identity, {"role": role}, "ws-office", ()))

    def test_disallowed_agent_prefix(self):
        identity = {"strength": "strong", "agent_id": "other-agent-123"}
        self.assertFalse(_prefix_actor_allowed(identity, {"role": "project-agent"}, "ws-office", ()))

    def test_allowed_strong_prefixes_ws_office(self):
        for prefix in _STRONG_AGENT_PREFIXES:
            agent_id = f"{prefix}test-session-id"
            for role in ("project-agent", "openclaw"):
                identity = {"strength": "strong", "agent_id": agent_id}
                actor = {"role": role}
                self.assertTrue(_prefix_actor_allowed(identity, actor, "ws-office", ()))

    def test_allowed_prefixes_custom_principal(self):
        identity = {"strength": "strong", "agent_id": "claude-ailab-worker-1"}
        actor = {"role": "project-agent"}
        # Without matching allowed_prefixes
        self.assertFalse(_prefix_actor_allowed(identity, actor, "custom-principal", ()))
        self.assertFalse(_prefix_actor_allowed(identity, actor, "custom-principal", ("codex-ailab-",)))
        # With matching allowed_prefixes
        self.assertTrue(_prefix_actor_allowed(identity, actor, "custom-principal", ("claude-ailab-",)))
        self.assertTrue(_prefix_actor_allowed(identity, actor, "custom-principal", ("codex-ailab-", "claude-ailab-")))


class TestOperatorConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "workspace.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_config(self, principal, cfg):
        data = {"principals": {principal: cfg}}
        self.config_path.write_text(json.dumps(data), encoding="utf-8")

    def test_operator_config_with_allowed_agent_prefixes(self):
        cfg = {
            "base_url": "http://127.0.0.1:8000",
            "token_env": "TEST_TOKEN",
            "allow_loopback_http": True,
            "allowed_agent_prefixes": ["claude-ailab-", "codex-ailab-"],
        }
        self._write_config("ws-office", cfg)
        with patch.dict(os.environ, {"OFFICE_WORKSPACE_CONFIG": str(self.config_path)}):
            config = operator_config("ws-office")
            self.assertEqual(config.allowed_agent_prefixes, ("claude-ailab-", "codex-ailab-"))

    def test_operator_config_omitted_allowed_agent_prefixes(self):
        cfg = {
            "base_url": "http://127.0.0.1:8000",
            "token_env": "TEST_TOKEN",
            "allow_loopback_http": True,
        }
        self._write_config("ws-office", cfg)
        with patch.dict(os.environ, {"OFFICE_WORKSPACE_CONFIG": str(self.config_path)}):
            config = operator_config("ws-office")
            self.assertEqual(config.allowed_agent_prefixes, ())

    def test_operator_config_invalid_allowed_agent_prefixes(self):
        for bad_prefixes in ("not-a-list", ["has space"], ["a" * 81], [""]):
            cfg = {
                "base_url": "http://127.0.0.1:8000",
                "token_env": "TEST_TOKEN",
                "allow_loopback_http": True,
                "allowed_agent_prefixes": bad_prefixes,
            }
            self._write_config("ws-office", cfg)
            with patch.dict(os.environ, {"OFFICE_WORKSPACE_CONFIG": str(self.config_path)}):
                with self.assertRaises(WorkspaceError):
                    operator_config("ws-office")


class TestCallIdentityChecks(unittest.TestCase):
    def test_call_bad_args(self):
        res = call("unknown_tool", {})
        self.assertEqual(res, {"status": "error", "code": "bad_args"})

    def test_call_weak_identity(self):
        res = call(
            "office_workspace_projects",
            {},
            caller_identity={"strength": "weak", "agent_id": "claude-ailab-1", "session": "s1"},
        )
        self.assertEqual(res, {"status": "error", "code": "workspace_identity_mismatch"})

    def test_call_missing_session(self):
        res = call(
            "office_workspace_projects",
            {},
            caller_identity={"strength": "strong", "agent_id": "claude-ailab-1"},
        )
        self.assertEqual(res, {"status": "error", "code": "workspace_identity_mismatch"})

    def test_call_session_mismatch(self):
        args = {
            "task_id": 1,
            "operation_id": "op1",
            "expected_version": 1,
            "session_id": "sess-a",
            "lease_seconds": 30,
        }
        res = call(
            "office_workspace_task_claim",
            args,
            caller_identity={"strength": "strong", "agent_id": "claude-ailab-1", "session": "sess-b"},
        )
        self.assertEqual(res, {"status": "error", "code": "workspace_identity_mismatch"})

    @patch("coordfs.workspace_surface.invoke")
    @patch.object(WorkspaceTasks, "_request")
    @patch("coordfs.workspace_surface._resolve_token", return_value="dummy-token")
    @patch("coordfs.workspace_surface.operator_config")
    def test_call_session_matches_with_local_prefix(
        self, mock_op_cfg, mock_token, mock_request, mock_invoke
    ):
        mock_cfg = MagicMock()
        mock_cfg.allowed_agent_prefixes = ()
        mock_op_cfg.return_value = mock_cfg
        mock_request.return_value = {"role": "project-agent", "actor_id": "claude-ailab-1"}
        mock_invoke.return_value = {"claimed": True}

        args = {
            "task_id": 1,
            "operation_id": "op1",
            "expected_version": 1,
            "session_id": "local_sess-123",
            "lease_seconds": 30,
        }
        res = call(
            "office_workspace_task_claim",
            args,
            caller_identity={"strength": "strong", "agent_id": "claude-ailab-1", "session": "sess-123"},
            transport_principal="ws-office",
        )
        self.assertEqual(res, {"status": "ok", "data": {"claimed": True}})

    @patch("coordfs.workspace_surface.invoke")
    @patch.object(WorkspaceTasks, "_request")
    @patch("coordfs.workspace_surface._resolve_token", return_value="dummy-token")
    @patch("coordfs.workspace_surface.operator_config")
    def test_call_actor_role_rejected(
        self, mock_op_cfg, mock_token, mock_request, mock_invoke
    ):
        mock_cfg = MagicMock()
        mock_cfg.allowed_agent_prefixes = ()
        mock_op_cfg.return_value = mock_cfg
        mock_request.return_value = {"role": "owner", "actor_id": "owner-1"}

        res = call(
            "office_workspace_projects",
            {},
            caller_identity={"strength": "strong", "agent_id": "claude-ailab-1", "session": "sess-1"},
            transport_principal="ws-office",
        )
        self.assertEqual(res, {"status": "error", "code": "agent_role_required"})

    @patch("coordfs.workspace_surface.invoke")
    @patch.object(WorkspaceTasks, "_request")
    @patch("coordfs.workspace_surface._resolve_token", return_value="dummy-token")
    @patch("coordfs.workspace_surface.operator_config")
    def test_call_actor_id_mismatch_without_prefix_allowance(
        self, mock_op_cfg, mock_token, mock_request, mock_invoke
    ):
        mock_cfg = MagicMock()
        mock_cfg.allowed_agent_prefixes = ()
        mock_op_cfg.return_value = mock_cfg
        mock_request.return_value = {"role": "project-agent", "actor_id": "other-agent-99"}

        # Custom principal with no allowed_agent_prefixes
        res = call(
            "office_workspace_projects",
            {},
            caller_identity={"strength": "strong", "agent_id": "claude-ailab-1", "session": "sess-1"},
            transport_principal="custom-principal",
        )
        self.assertEqual(res, {"status": "error", "code": "workspace_identity_mismatch"})

    @patch("coordfs.workspace_surface.invoke")
    @patch.object(WorkspaceTasks, "_request")
    @patch("coordfs.workspace_surface._resolve_token", return_value="dummy-token")
    @patch("coordfs.workspace_surface.operator_config")
    def test_call_actor_id_mismatch_with_prefix_allowance_ws_office(
        self, mock_op_cfg, mock_token, mock_request, mock_invoke
    ):
        mock_cfg = MagicMock()
        mock_cfg.allowed_agent_prefixes = ()
        mock_op_cfg.return_value = mock_cfg
        mock_request.return_value = {"role": "project-agent", "actor_id": "shared-project-actor"}
        mock_invoke.return_value = {"projects": []}

        res = call(
            "office_workspace_projects",
            {},
            caller_identity={"strength": "strong", "agent_id": "claude-ailab-session-xyz", "session": "sess-1"},
            transport_principal="ws-office",
        )
        self.assertEqual(res, {"status": "ok", "data": {"projects": []}})

    @patch("coordfs.workspace_surface.invoke")
    @patch.object(WorkspaceTasks, "_request")
    @patch("coordfs.workspace_surface._resolve_token", return_value="dummy-token")
    @patch("coordfs.workspace_surface.operator_config")
    def test_call_actor_id_mismatch_with_matching_allowed_prefix_custom_principal(
        self, mock_op_cfg, mock_token, mock_request, mock_invoke
    ):
        mock_cfg = MagicMock()
        mock_cfg.allowed_agent_prefixes = ("claude-ailab-",)
        mock_op_cfg.return_value = mock_cfg
        mock_request.return_value = {"role": "project-agent", "actor_id": "shared-project-actor"}
        mock_invoke.return_value = {"projects": []}

        res = call(
            "office_workspace_projects",
            {},
            caller_identity={"strength": "strong", "agent_id": "claude-ailab-session-xyz", "session": "sess-1"},
            transport_principal="custom-principal",
        )
        self.assertEqual(res, {"status": "ok", "data": {"projects": []}})


class TestDiscoverActiveCodexBindings(unittest.TestCase):
    def test_discover_active_bindings_success(self):
        mock_client = MagicMock()
        mock_client.request.return_value = {
            "status": "ok",
            "bindings": [
                {"binding_id": "codex-active-1", "driver": DRIVER, "status": "active", "verified": True},
                {"binding_id": "codex-core", "driver": DRIVER, "status": "active", "verified": True},
                {"binding_id": "codex-active-2", "driver": DRIVER, "status": "active", "verified": True},
            ],
            "has_more": False,
        }

        res = discover_active_codex_bindings(mock_client, {"agent_id": "test"})
        expected = ("codex-active-1", "codex-core", "codex-active-2", "codex-hub-root", "codex-rozwoj")
        self.assertEqual(res, expected)
        mock_client.request.assert_called_once_with("wake-list", {"agent_id": "test"}, {"limit": 10, "offset": 0})

    def test_discover_active_bindings_paginates_and_filters_expired(self):
        mock_client = MagicMock()
        mock_client.request.side_effect = [
            {
                "status": "ok",
                "bindings": [
                    {"binding_id": "codex-page-1", "driver": DRIVER, "status": "active", "verified": True, "expires_at": "2099-01-01T00:00:00Z"},
                    {"binding_id": "codex-expired", "driver": DRIVER, "status": "active", "verified": True, "expires_at": "2020-01-01T00:00:00Z"},
                ],
                "has_more": True,
                "next_offset": 10,
            },
            {
                "status": "ok",
                "bindings": [
                    {"binding_id": "codex-page-2", "driver": DRIVER, "status": "active", "verified": True, "expires_at": "2099-01-01T00:00:00Z"},
                ],
                "has_more": False,
                "next_offset": None,
            },
        ]

        res = discover_active_codex_bindings(mock_client, {"agent_id": "test"})
        self.assertEqual(res, ("codex-page-1", "codex-page-2", *BINDING_IDS))
        self.assertEqual(mock_client.request.call_count, 2)

    def test_discover_active_bindings_filters_non_matching(self):
        mock_client = MagicMock()
        mock_client.request.return_value = {
            "status": "ok",
            "bindings": [
                {"binding_id": "wrong-driver", "driver": "antigravity-desktop-relay", "status": "active", "verified": True},
                {"binding_id": "inactive", "driver": DRIVER, "status": "disabled", "verified": True},
                {"binding_id": "unverified", "driver": DRIVER, "status": "active", "verified": False},
                {"binding_id": "bad name with space", "driver": DRIVER, "status": "active", "verified": True},
                {"binding_id": "valid-1", "driver": DRIVER, "status": "active", "verified": True},
            ],
        }

        res = discover_active_codex_bindings(mock_client, {"agent_id": "test"})
        self.assertEqual(res, ("valid-1", *BINDING_IDS))

    def test_discover_active_bindings_capped_at_16(self):
        mock_client = MagicMock()
        mock_client.request.return_value = {
            "status": "ok",
            "bindings": [
                {"binding_id": f"b-{i:02d}", "driver": DRIVER, "status": "active", "verified": True}
                for i in range(25)
            ],
        }

        res = discover_active_codex_bindings(mock_client, {"agent_id": "test"})
        self.assertEqual(len(res), 16)
        self.assertEqual(res[:3], ("b-00", "b-01", "b-02"))

    def test_discover_active_bindings_error_fallback(self):
        mock_client = MagicMock()
        mock_client.request.side_effect = RuntimeError("network failed")
        res = discover_active_codex_bindings(mock_client, {"agent_id": "test"})
        self.assertEqual(res, BINDING_IDS)

        mock_client.request.side_effect = None
        mock_client.request.return_value = "not a mapping"
        res = discover_active_codex_bindings(mock_client, {"agent_id": "test"})
        self.assertEqual(res, BINDING_IDS)

        mock_client.request.return_value = {"status": "error"}
        res = discover_active_codex_bindings(mock_client, {"agent_id": "test"})
        self.assertEqual(res, BINDING_IDS)


if __name__ == "__main__":
    unittest.main()
