import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import cloud_org


class CloudOrgTests(unittest.TestCase):
    def setUp(self):
        self.registry = {"schema_version": 2, "organizations": [
            {"organization_id": "org-a", "aliases": ["alpha", "a"], "providers": {
                "azure": {"project_root": "C:/AL Lab/roots/a", "config_root": "C:/Profiles/a/azure", "account": "owner@alpha.example", "tenant_id": "tenant-a", "subscription_id": "sub-a"},
                "graph": {"project_root": "C:/AL Lab/roots/a", "config_root": "C:/Profiles/a/graph", "account": "owner@alpha.example", "tenant_id": "tenant-a", "graph_context": "context-a"},
                "gcp": {"project_root": "C:/AL Lab/roots/a", "config_root": "C:/Profiles/a/gcloud", "account": "owner@alpha.example", "project_id": "project-a", "configuration": "config-a"},
                "google-workspace": {"project_root": "C:/AL Lab/roots/a", "config_root": "C:/Profiles/a/workspace", "account": "owner@alpha.example", "domain": "alpha.example", "configuration": "workspace-a"}}},
            {"organization_id": "org-b", "aliases": ["beta"], "providers": {
                "azure": {"project_root": "D:/roots/b", "config_root": "D:/profiles/b/azure", "account": "owner@beta.example", "tenant_id": "tenant-b", "subscription_id": "sub-b"}}}]}

    def m365_config(self, auth_mode="delegated", connectors=True):
        config = {
            "project_root": "C:/AL Lab/roots/a",
            "config_root": "C:/Profiles/a/m365",
            "tenant_id": "tenant-a",
            "sharepoint_host": "organization-a.sharepoint.example",
            "site_path": "/sites/operations",
            "auth_mode": auth_mode,
            "m365_connection_name": "organization-a-admin",
        }
        identity_field = "account" if auth_mode == "delegated" else "application_id"
        config[identity_field] = "owner@alpha.example" if auth_mode == "delegated" else "application-a"
        if connectors:
            config["mcp_connectors"] = [{
                "connector_id": "m365-connector-a",
                "tenant_id": config["tenant_id"],
                "sharepoint_host": config["sharepoint_host"],
                "site_path": config["site_path"],
                "auth_mode": auth_mode,
                identity_field: config[identity_field],
            }]
        return config

    def m365_registry(self, auth_mode="delegated", connectors=True):
        registry = json.loads(json.dumps(self.registry))
        registry["organizations"][0]["providers"]["m365"] = self.m365_config(auth_mode, connectors)
        return registry

    def two_m365_registry(self):
        registry = self.m365_registry()
        config = self.m365_config()
        config.update({
            "project_root": "D:/roots/b",
            "config_root": "D:/profiles/b/m365",
            "tenant_id": "tenant-b",
            "sharepoint_host": "organization-b.sharepoint.example",
            "site_path": "/sites/finance",
            "account": "owner@beta.example",
            "m365_connection_name": "organization-b-admin",
        })
        config["mcp_connectors"] = [{
            "connector_id": "m365-connector-b",
            "tenant_id": config["tenant_id"],
            "sharepoint_host": config["sharepoint_host"],
            "site_path": config["site_path"],
            "auth_mode": config["auth_mode"],
            "account": config["account"],
        }]
        registry["organizations"][1]["providers"]["m365"] = config
        return registry

    def m365_observed(self, auth_mode="delegated", channel="graph-powershell", connector_id="m365-connector-a"):
        observed = {
            "tenant_id": "tenant-a",
            "sharepoint_host": "organization-a.sharepoint.example",
            "site_path": "/sites/operations",
            "auth_mode": auth_mode,
        }
        if auth_mode == "delegated":
            observed["account"] = "owner@alpha.example"
        else:
            observed["application_id"] = "application-a"
        if channel == "m365-cli":
            observed["connection_name"] = "organization-a-admin"
        elif channel == "mcp":
            observed["connector_id"] = connector_id
        return observed

    def run_cli(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cloud_org.main(argv)
        return code, json.loads(output.getvalue())

    def test_two_orgs_alias_and_path(self):
        org, config = cloud_org.resolve(self.registry, "azure", alias="beta")
        self.assertEqual((org["organization_id"], config["project_root"]), ("org-b", "D:/roots/b"))

    def test_ambiguity_missing_and_provider_mismatch(self):
        ambiguous = {**self.registry, "organizations": self.registry["organizations"] + [{"organization_id": "org-c", "aliases": ["alpha"], "providers": {"azure": {"project_root": "E:/c", "config_root": "E:/profiles/c", "account": "owner@c.example", "tenant_id": "t", "subscription_id": "s"}}}]}
        for call in (lambda: cloud_org.resolve(ambiguous, "azure", alias="alpha"), lambda: cloud_org.resolve(self.registry, "azure", alias="missing"), lambda: cloud_org.resolve(self.registry, "gcp", alias="beta")):
            with self.assertRaises(cloud_org.Denied): call()

    def test_secret_reject_is_recursive(self):
        bad = json.loads(json.dumps(self.registry))
        bad["organizations"][0]["providers"]["azure"]["nested"] = {"private_key": "redacted"}
        with self.assertRaises(cloud_org.Denied): cloud_org.validate_registry(bad)

    def test_explicit_targets_and_deterministic_output(self):
        one = cloud_org.plan_preflight(self.registry, "azure", organization_id="org-a")
        two = cloud_org.plan_preflight(self.registry, "azure", organization_id="org-a")
        self.assertEqual(json.dumps(one, sort_keys=True, separators=(",", ":")), json.dumps(two, sort_keys=True, separators=(",", ":")))
        self.assertEqual(one["plan"]["env"]["AZURE_TENANT_ID"], "tenant-a")
        self.assertEqual(one["plan"]["env"]["AZURE_CONFIG_DIR"], "C:/Profiles/a/azure")
        self.assertFalse(one["plan"]["execute"])
        self.assertEqual(one["decision"], "REGISTRY_ALLOW")
        self.assertEqual(one["live_context_status"], "UNVERIFIED")
        self.assertFalse(one["execution_allowed"])

    def test_project_root_must_be_absolute_but_may_contain_spaces(self):
        cloud_org.validate_registry(self.registry)
        bad = json.loads(json.dumps(self.registry))
        bad["organizations"][0]["providers"]["azure"]["project_root"] = "relative/path"
        with self.assertRaises(cloud_org.Denied):
            cloud_org.validate_registry(bad)

    def test_schema_v2_requires_account_and_isolated_config_root(self):
        for field in ("account", "config_root"):
            bad = json.loads(json.dumps(self.registry))
            del bad["organizations"][0]["providers"]["gcp"][field]
            with self.assertRaises(cloud_org.Denied):
                cloud_org.validate_registry(bad)

    def test_schema_v1_remains_compatible(self):
        legacy = json.loads(json.dumps(self.registry))
        legacy["schema_version"] = 1
        for org in legacy["organizations"]:
            for config in org["providers"].values():
                config.pop("account", None)
                config.pop("config_root", None)
        cloud_org.validate_registry(legacy)

    def test_live_context_requires_exact_match(self):
        verified = cloud_org.verify_context(
            self.registry,
            "azure",
            {"tenant_id": "tenant-a", "subscription_id": "sub-a", "account": "owner@alpha.example"},
            organization_id="org-a",
        )
        self.assertEqual(verified["decision"], "ALLOW")
        self.assertTrue(verified["execution_allowed"])
        with self.assertRaises(cloud_org.Denied):
            cloud_org.verify_context(
                self.registry,
                "azure",
                {"tenant_id": "tenant-a", "subscription_id": "wrong", "account": "owner@alpha.example"},
                organization_id="org-a",
            )

    def test_google_context_requires_exact_account(self):
        observed = {"project_id": "project-a", "configuration": "config-a", "account": "wrong@alpha.example"}
        with self.assertRaises(cloud_org.Denied):
            cloud_org.verify_context(self.registry, "gcp", observed, organization_id="org-a")
        observed["account"] = "owner@alpha.example"
        self.assertEqual(cloud_org.verify_context(self.registry, "gcp", observed, organization_id="org-a")["decision"], "ALLOW")

    def test_m365_sharepoint_host_guard_is_canonical(self):
        self.assertEqual(
            cloud_org._sharepoint_host("organization-a.sharepoint.example", "host"),
            "organization-a.sharepoint.example",
        )
        for value in (
            "Organization-A.sharepoint.example",
            "https://organization-a.sharepoint.example",
            "organization-a.sharepoint.example:443",
            "organization-a.sharepoint.example/sites/a",
            "singlelabel",
        ):
            with self.subTest(value=value), self.assertRaises(cloud_org.Denied):
                cloud_org._sharepoint_host(value, "host")

    def test_m365_site_path_guard_is_canonical(self):
        for value in ("/", "/sites/operations"):
            self.assertEqual(cloud_org._site_path(value, "site"), value)
        for value in (
            "sites/operations", "/sites/operations/", "/sites//operations",
            "/sites/../admin", "/sites/%252e%252e/admin", "/sites/a?x=1",
            "/sites/a#fragment", "/sites\\a", "/sites/ops$(id)",
            "/sites/ops;whoami", "/sites/ops&dir", "/sites/$env:USER",
            "/sites/ops%24(id)", "/sites/ops%3Bwhoami",
        ):
            with self.subTest(value=value), self.assertRaises(cloud_org.Denied):
                cloud_org._site_path(value, "site")

    def test_m365_cli_plan_uses_structured_argv_not_shell_commands(self):
        plan = cloud_org.plan_preflight(
            self.m365_registry(), "m365", organization_id="org-a",
            execution_channel="m365-cli", intent="read",
        )
        steps = {step["action"]: step for step in plan["plan"]["steps"]}
        for action in (
            "list-m365-connections", "select-m365-connection",
            "inspect-m365-tenant", "inspect-m365-site",
        ):
            with self.subTest(action=action):
                self.assertNotIn("command", steps[action])
                self.assertIsInstance(steps[action]["argv"], list)
        self.assertEqual(
            steps["inspect-m365-site"]["argv"],
            ["m365", "spo", "site", "get", "--url", "https://organization-a.sharepoint.example/sites/operations", "--output", "json"],
        )

    def test_m365_auth_mode_and_identity_are_mandatory_and_exclusive(self):
        cloud_org._validate_m365_config(self.m365_config("delegated"), 2, set())
        cloud_org._validate_m365_config(self.m365_config("app-only"), 2, set())
        cases = []
        missing = self.m365_config()
        del missing["auth_mode"]
        cases.append(missing)
        unknown = self.m365_config()
        unknown["auth_mode"] = "automatic"
        cases.append(unknown)
        delegated_with_app = self.m365_config()
        delegated_with_app["application_id"] = "application-a"
        cases.append(delegated_with_app)
        app_with_account = self.m365_config("app-only")
        app_with_account["account"] = "owner@alpha.example"
        cases.append(app_with_account)
        for config in cases:
            with self.subTest(config=config), self.assertRaises(cloud_org.Denied):
                cloud_org._validate_m365_config(config, 2, set())

    def test_m365_requires_schema_v2_and_complete_target(self):
        registry = self.m365_registry()
        cloud_org.validate_registry(registry)
        legacy = json.loads(json.dumps(registry))
        legacy["schema_version"] = 1
        with self.assertRaises(cloud_org.Denied):
            cloud_org.validate_registry(legacy)
        for field in ("tenant_id", "sharepoint_host"):
            bad = json.loads(json.dumps(registry))
            del bad["organizations"][0]["providers"]["m365"][field]
            with self.subTest(field=field), self.assertRaises(cloud_org.Denied):
                cloud_org.validate_registry(bad)

    def test_m365_partial_target_denies_without_connector_side_effects(self):
        for field in ("tenant_id", "sharepoint_host"):
            registry = self.m365_registry(connectors=False)
            del registry["organizations"][0]["providers"]["m365"][field]
            with self.subTest(field=field), self.assertRaises(cloud_org.Denied):
                cloud_org.validate_registry(registry)

    def test_m365_app_only_plan_uses_organization_probe_not_get_mgcontext(self):
        plan = cloud_org.plan_preflight(
            self.m365_registry("app-only"), "m365", organization_id="org-a",
            execution_channel="graph-powershell", intent="read",
        )
        serialized = json.dumps(plan, sort_keys=True)
        self.assertIn("inspect-graph-app-only-organization", serialized)
        self.assertNotIn("Get-MgContext", serialized)
        verified = cloud_org.verify_context(
            self.m365_registry("app-only"), "m365", self.m365_observed("app-only"),
            organization_id="org-a", execution_channel="graph-powershell", intent="read",
        )
        self.assertEqual(verified["decision"], "ALLOW")
        with self.assertRaises(cloud_org.Denied):
            cloud_org.verify_context(
                self.m365_registry("app-only"), "m365",
                {**self.m365_observed("app-only"), "access_token": "not-a-real-token"},
                organization_id="org-a", execution_channel="graph-powershell", intent="read",
            )

    def test_m365_channel_and_intent_are_required(self):
        registry = self.m365_registry()
        plan = cloud_org.plan_preflight(
            registry, "m365", organization_id="org-a",
            execution_channel="graph-powershell", intent="read",
        )
        self.assertEqual((plan["execution_channel"], plan["intent"]), ("graph-powershell", "read"))
        for kwargs in ({"intent": "read"}, {"execution_channel": "graph-powershell"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(cloud_org.Denied):
                cloud_org.plan_preflight(registry, "m365", organization_id="org-a", **kwargs)

    def test_m365_exact_tenant_host_and_site_match(self):
        registry = self.m365_registry()
        observed = self.m365_observed()
        verified = cloud_org.verify_context(
            registry, "m365", observed, organization_id="org-a",
            execution_channel="graph-powershell", intent="read",
        )
        self.assertTrue(verified["execution_allowed"])
        for field, value in (("tenant_id", "tenant-b"), ("sharepoint_host", "organization-b.sharepoint.example"), ("site_path", "/sites/finance")):
            mismatch = {**observed, field: value}
            with self.subTest(field=field), self.assertRaises(cloud_org.Denied):
                cloud_org.verify_context(
                    registry, "m365", mismatch, organization_id="org-a",
                    execution_channel="graph-powershell", intent="read",
                )

    def test_m365_connector_ids_are_global_and_bind_parent_identity(self):
        registry = self.m365_registry()
        cloud_org.validate_registry(registry)
        duplicate = json.loads(json.dumps(registry))
        duplicate["organizations"][1]["providers"]["m365"] = self.m365_config()
        with self.assertRaises(cloud_org.Denied):
            cloud_org.validate_registry(duplicate)
        mismatch = json.loads(json.dumps(registry))
        mismatch["organizations"][0]["providers"]["m365"]["mcp_connectors"][0]["tenant_id"] = "tenant-b"
        with self.assertRaises(cloud_org.Denied):
            cloud_org.validate_registry(mismatch)

    def test_m365_connector_binding_matches_parent_without_duplicate_shortcut(self):
        mismatch = self.m365_registry()
        mismatch["organizations"][0]["providers"]["m365"]["mcp_connectors"][0]["tenant_id"] = "tenant-b"
        with self.assertRaises(cloud_org.Denied):
            cloud_org.validate_registry(mismatch)

    def test_m365_registered_mcp_connector_verifies_exact_identity(self):
        registry = self.m365_registry()
        observed = self.m365_observed(channel="mcp")
        verified = cloud_org.verify_context(
            registry, "m365", observed, organization_id="org-a",
            execution_channel="mcp", intent="mutate", connector_id="m365-connector-a",
        )
        self.assertEqual(verified["decision"], "ALLOW")
        with self.assertRaises(cloud_org.Denied):
            cloud_org.verify_context(
                registry, "m365", {**observed, "account": "wrong@alpha.example"},
                organization_id="org-a", execution_channel="mcp", intent="mutate",
                connector_id="m365-connector-a",
            )

    def test_m365_unregistered_mcp_read_is_warned_and_mutation_denies(self):
        registry = self.m365_registry(connectors=False)
        observed = self.m365_observed(channel="mcp", connector_id="unknown-connector")
        with self.assertRaises(cloud_org.Denied):
            cloud_org.plan_preflight(
                registry, "m365", organization_id="org-a",
                execution_channel="mcp", intent="mutate", connector_id="unknown-connector",
            )
        result = cloud_org.verify_context(
            registry, "m365", observed, organization_id="org-a",
            execution_channel="mcp", intent="read", connector_id="unknown-connector",
        )
        self.assertEqual(result["decision"], "REGISTRY_ALLOW")
        self.assertEqual(result["live_context_status"], "UNVERIFIED")
        self.assertFalse(result["execution_allowed"])
        self.assertTrue(result["read_only_allowed"])
        self.assertTrue(result["warnings"])
        with self.assertRaises(cloud_org.Denied):
            cloud_org.verify_context(
                registry, "m365", observed, organization_id="org-a",
                execution_channel="mcp", intent="mutate", connector_id="unknown-connector",
            )

    def test_m365_unregistered_mcp_verify_mutation_denies_independently(self):
        registry = self.m365_registry(connectors=False)
        observed = self.m365_observed(channel="mcp", connector_id="unknown-connector")
        with self.assertRaises(cloud_org.Denied):
            cloud_org.verify_context(
                registry, "m365", observed, organization_id="org-a",
                execution_channel="mcp", intent="mutate", connector_id="unknown-connector",
            )

    def test_m365_cross_organization_connector_denies_read_and_mutate(self):
        registry = self.two_m365_registry()
        cloud_org.validate_registry(registry)
        observed = self.m365_observed(channel="mcp", connector_id="m365-connector-b")
        for intent in ("read", "mutate"):
            with self.subTest(operation="plan", intent=intent), self.assertRaises(cloud_org.Denied):
                cloud_org.plan_preflight(
                    registry, "m365", organization_id="org-a",
                    execution_channel="mcp", intent=intent, connector_id="m365-connector-b",
                )
            with self.subTest(operation="verify", intent=intent), self.assertRaises(cloud_org.Denied):
                cloud_org.verify_context(
                    registry, "m365", observed, organization_id="org-a",
                    execution_channel="mcp", intent=intent, connector_id="m365-connector-b",
                )

    def test_m365_cli_named_connection_is_scoped_to_that_channel(self):
        registry = self.m365_registry()
        observed = self.m365_observed(channel="m365-cli")
        verified = cloud_org.verify_context(
            registry, "m365", observed, organization_id="org-a",
            execution_channel="m365-cli", intent="read",
        )
        self.assertEqual(verified["decision"], "ALLOW")
        for field, value in (("connection_name", "wrong-connection"), ("tenant_id", "tenant-b")):
            with self.subTest(field=field), self.assertRaises(cloud_org.Denied):
                cloud_org.verify_context(
                    registry, "m365", {**observed, field: value}, organization_id="org-a",
                    execution_channel="m365-cli", intent="read",
                )
        without_name = self.m365_registry()
        del without_name["organizations"][0]["providers"]["m365"]["m365_connection_name"]
        cloud_org.plan_preflight(
            without_name, "m365", organization_id="org-a",
            execution_channel="graph-powershell", intent="read",
        )
        with self.assertRaises(cloud_org.Denied):
            cloud_org.plan_preflight(
                without_name, "m365", organization_id="org-a",
                execution_channel="m365-cli", intent="read",
            )

    def test_m365_only_arguments_are_rejected_for_legacy_providers(self):
        cloud_org.plan_preflight(self.registry, "azure", organization_id="org-a")
        with self.assertRaises(cloud_org.Denied):
            cloud_org.plan_preflight(
                self.registry, "azure", organization_id="org-a",
                execution_channel="graph-powershell", intent="read",
            )
        verified = cloud_org.verify_context(
            self.registry, "graph",
            {"tenant_id": "tenant-a", "graph_context": "context-a", "account": "owner@alpha.example"},
            organization_id="org-a",
        )
        self.assertEqual(verified["decision"], "ALLOW")

    def test_cli_exit_codes_and_m365_receipt_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_path = root / "registry.json"
            observed_path = root / "observed.json"
            registry_path.write_text(json.dumps(self.m365_registry()), encoding="utf-8")
            observed_path.write_text(json.dumps(self.m365_observed()), encoding="utf-8")
            base = [
                "verify-context", "--registry", str(registry_path), "--provider", "m365",
                "--organization-id", "org-a", "--execution-channel", "graph-powershell",
                "--intent", "read", "--observed-file", str(observed_path),
            ]
            code, receipt = self.run_cli(base)
            self.assertEqual((code, receipt["decision"]), (0, "ALLOW"))
            observed_path.write_text(json.dumps({**self.m365_observed(), "tenant_id": "tenant-b"}), encoding="utf-8")
            code, receipt = self.run_cli(base)
            self.assertEqual((code, receipt["decision"]), (2, "DENY"))
            with mock.patch.object(cloud_org, "load_registry", side_effect=RuntimeError("synthetic failure")):
                code, receipt = self.run_cli(["validate-registry", "--registry", str(registry_path)])
            self.assertEqual(code, 1)
            self.assertEqual(receipt["decision"], "DENY")
            self.assertNotIn("synthetic failure", json.dumps(receipt))

    def test_m365_schemas_and_documentation_pin_safe_oracles(self):
        skill_root = Path(__file__).parents[1]
        registry_schema = json.loads((skill_root / "registry.schema.json").read_text(encoding="utf-8"))
        receipt_schema = json.loads((skill_root / "receipt.schema.json").read_text(encoding="utf-8"))
        contract = (skill_root / "references" / "contract.md").read_text(encoding="utf-8")
        self.assertIn("m365", json.dumps(registry_schema))
        for field in ("execution_channel", "intent", "warnings", "read_only_allowed"):
            self.assertIn(field, receipt_schema["properties"])
        for text in (
            "m365 connection list --output json",
            "m365 connection use --name",
            "m365 tenant id get --output json",
            "https://login.microsoftonline.com/<domain>/v2.0/.well-known/openid-configuration",
            "must not be inferred",
        ):
            self.assertIn(text, contract)
        site_path_pattern = registry_schema["$defs"]["sitePath"]["pattern"]
        for value in ("/sites/operations", "/sites/operations%20east"):
            self.assertIsNotNone(re.fullmatch(site_path_pattern, value))
        for value in (
            "/sites/../admin", "/sites/%252e%252e/admin", "/sites/ops$(id)",
            "/sites/ops;whoami", "/sites/ops%24(id)", "/sites/ops%3Bwhoami",
        ):
            with self.subTest(value=value):
                self.assertIsNone(re.fullmatch(site_path_pattern, value))

    def test_cli_process_preserves_deny_exit_code_two(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_path = root / "registry.json"
            observed_path = root / "observed.json"
            registry_path.write_text(json.dumps(self.m365_registry()), encoding="utf-8")
            observed_path.write_text(
                json.dumps({**self.m365_observed(), "tenant_id": "tenant-b"}),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable, str(Path(cloud_org.__file__)), "verify-context",
                    "--registry", str(registry_path), "--provider", "m365",
                    "--organization-id", "org-a", "--execution-channel", "graph-powershell",
                    "--intent", "read", "--observed-file", str(observed_path),
                ],
                check=False, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(json.loads(completed.stdout)["decision"], "DENY")


if __name__ == "__main__":
    unittest.main()
