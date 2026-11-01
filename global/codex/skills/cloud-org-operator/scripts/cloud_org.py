"""Stdlib-only, local, read-only cloud organization gate."""
import argparse, json, os, re, sys
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

DEFAULT_REGISTRY = "~/.agents/cloud-organizations.json"
PROVIDERS = ("azure", "graph", "gcp", "google-workspace", "m365")
SCHEMA_VERSIONS = (1, 2)
EXECUTION_CHANNELS = ("graph-powershell", "m365-cli", "mcp")
INTENTS = ("read", "mutate")
AUTH_MODES = ("delegated", "app-only")
SECRET_KEY = re.compile(r"(?:secret|token|password|private[_-]?key|api[_-]?key|credential)", re.I)
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
ACCOUNT = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DNS_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)

class Denied(ValueError):
    pass

class CliFailure(ValueError):
    pass

class GateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CliFailure(message)

def _secret_scan(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_KEY.search(str(key)):
                raise Denied("secret-looking field at " + path + "." + str(key))
            _secret_scan(child, path + "." + str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _secret_scan(child, path + "[" + str(index) + "]")

def _text(value, label):
    if not isinstance(value, str) or not value.strip() or not IDENTIFIER.fullmatch(value):
        raise Denied("invalid " + label)
    return value

def _account(value, label):
    if not isinstance(value, str) or not ACCOUNT.fullmatch(value):
        raise Denied("invalid " + label)
    return value

def _project_root(value, label):
    if not isinstance(value, str) or not value.strip():
        raise Denied("invalid " + label)
    if any(ord(character) < 32 for character in value):
        raise Denied("invalid " + label)
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise Denied(label + " must be absolute")
    return value

def _sharepoint_host(value, label):
    if not isinstance(value, str) or value != value.lower() or not DNS_HOST.fullmatch(value):
        raise Denied("invalid " + label)
    return value

def _site_path(value, label):
    if not isinstance(value, str) or not value.startswith("/"):
        raise Denied("invalid " + label)
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "\\" in value:
        raise Denied("invalid " + label)
    if value != "/" and (value.endswith("/") or "//" in value):
        raise Denied("invalid " + label)
    decoded = unquote(value)
    if unquote(decoded) != decoded:
        raise Denied("invalid " + label)
    if "\\" in decoded or "//" in decoded or any(part in (".", "..") for part in decoded.split("/")):
        raise Denied("invalid " + label)
    if any(not (character.isalnum() or character in "/-._~ ") for character in decoded):
        raise Denied("invalid " + label)
    canonical = quote(decoded, safe="/-._~")
    if canonical != value:
        raise Denied("invalid " + label)
    return value

def _mode_identity(config, label):
    mode = config.get("auth_mode")
    if mode not in AUTH_MODES:
        raise Denied("invalid " + label + ".auth_mode")
    if mode == "delegated":
        if "application_id" in config:
            raise Denied("invalid " + label + " identity")
        return "account", _account(config.get("account"), label + ".account")
    if "account" in config:
        raise Denied("invalid " + label + " identity")
    return "application_id", _text(config.get("application_id"), label + ".application_id")

def _validate_m365_config(config, schema_version, connector_ids):
    if schema_version != 2:
        raise Denied("m365 requires registry schema v2")
    if not isinstance(config, dict):
        raise Denied("invalid m365 configuration")
    allowed = {
        "project_root", "config_root", "tenant_id", "sharepoint_host", "site_path",
        "auth_mode", "account", "application_id", "m365_connection_name", "mcp_connectors",
    }
    if set(config) - allowed:
        raise Denied("invalid m365 configuration")
    _project_root(config.get("project_root"), "m365.project_root")
    _project_root(config.get("config_root"), "m365.config_root")
    _text(config.get("tenant_id"), "m365.tenant_id")
    _sharepoint_host(config.get("sharepoint_host"), "m365.sharepoint_host")
    if "site_path" in config:
        _site_path(config["site_path"], "m365.site_path")
    identity_field, identity_value = _mode_identity(config, "m365")
    if "m365_connection_name" in config:
        _text(config["m365_connection_name"], "m365.m365_connection_name")
    connectors = config.get("mcp_connectors", [])
    if not isinstance(connectors, list):
        raise Denied("invalid m365.mcp_connectors")
    parent_fields = ("tenant_id", "sharepoint_host", "auth_mode", identity_field)
    if "site_path" in config:
        parent_fields += ("site_path",)
    for connector in connectors:
        if not isinstance(connector, dict):
            raise Denied("invalid m365 connector")
        connector_identity, connector_identity_value = _mode_identity(connector, "m365.connector")
        required = {"connector_id", *parent_fields}
        if set(connector) != required or connector_identity != identity_field:
            raise Denied("invalid m365 connector shape")
        connector_id = _text(connector.get("connector_id"), "m365.connector.connector_id")
        if connector_id in connector_ids:
            raise Denied("duplicate m365 connector_id")
        connector_ids.add(connector_id)
        _text(connector.get("tenant_id"), "m365.connector.tenant_id")
        _sharepoint_host(connector.get("sharepoint_host"), "m365.connector.sharepoint_host")
        if "site_path" in connector:
            _site_path(connector["site_path"], "m365.connector.site_path")
        for field in parent_fields:
            if connector.get(field) != config.get(field):
                raise Denied("m365 connector parent mismatch: " + field)
        if connector_identity_value != identity_value:
            raise Denied("m365 connector parent mismatch: " + identity_field)
    return True

def validate_registry(data):
    _secret_scan(data)
    if not isinstance(data, dict) or data.get("schema_version") not in SCHEMA_VERSIONS:
        raise Denied("unsupported registry schema")
    schema_version = data["schema_version"]
    organizations = data.get("organizations")
    if not isinstance(organizations, list) or not organizations:
        raise Denied("organizations must be a non-empty list")
    ids, aliases, connector_ids = set(), set(), set()
    allowed_fields = {"project_root", "config_root", "account", "tenant_id", "subscription_id", "project_id", "configuration", "domain", "graph_context"}
    required = {"azure": ("tenant_id", "subscription_id"), "graph": ("tenant_id", "graph_context"), "gcp": ("project_id", "configuration"), "google-workspace": ("domain", "configuration")}
    for org in organizations:
        if not isinstance(org, dict) or set(org) != {"organization_id", "aliases", "providers"}:
            raise Denied("invalid organization shape")
        org_id = _text(org["organization_id"], "organization_id")
        if org_id in ids:
            raise Denied("duplicate organization_id")
        ids.add(org_id)
        if not isinstance(org["aliases"], list) or len(set(org["aliases"])) != len(org["aliases"]):
            raise Denied("invalid aliases")
        for alias in org["aliases"]:
            alias = _text(alias, "alias")
            if alias in aliases:
                raise Denied("duplicate alias")
            aliases.add(alias)
        providers = org["providers"]
        if not isinstance(providers, dict) or not providers or not set(providers).issubset(PROVIDERS):
            raise Denied("invalid providers")
        for provider, config in providers.items():
            if provider == "m365":
                _validate_m365_config(config, schema_version, connector_ids)
                continue
            if not isinstance(config, dict) or set(config) - allowed_fields:
                raise Denied("invalid " + provider + " configuration")
            _project_root(config.get("project_root"), provider + ".project_root")
            provider_required = required[provider]
            if schema_version >= 2:
                _project_root(config.get("config_root"), provider + ".config_root")
                provider_required = provider_required + ("account",)
            for field in provider_required:
                validator = _account if field == "account" else _text
                validator(config.get(field), provider + "." + field)
    return True

def load_registry(path=None):
    try:
        with Path(os.path.expanduser(path or DEFAULT_REGISTRY)).open(encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError) as exc:
        raise Denied("cannot read registry") from exc
    validate_registry(data)
    return data

def resolve(data, provider, organization_id=None, alias=None):
    validate_registry(data)
    if provider not in PROVIDERS:
        raise Denied("unsupported provider")
    if bool(organization_id) == bool(alias):
        raise Denied("exactly one explicit organization_id or alias is required")
    matches = [org for org in data["organizations"] if (org["organization_id"] == organization_id if organization_id else alias in org["aliases"])]
    if len(matches) != 1:
        raise Denied("missing or ambiguous organization target")
    org = matches[0]
    if provider not in org["providers"]:
        raise Denied("provider mismatch")
    return org, org["providers"][provider]

def _target(org, provider, config):
    safe_fields = (
        "project_root", "config_root", "account", "application_id", "tenant_id",
        "subscription_id", "project_id", "configuration", "domain", "graph_context",
        "sharepoint_host", "site_path", "auth_mode", "m365_connection_name",
    )
    return {
        "organization_id": org["organization_id"],
        "provider": provider,
        **{key: config[key] for key in safe_fields if key in config},
    }

def _context_fields(provider, config):
    fields = {
        "azure": ("tenant_id", "subscription_id"),
        "graph": ("tenant_id", "graph_context"),
        "gcp": ("project_id", "configuration"),
        "google-workspace": ("domain", "configuration"),
    }[provider]
    return fields + (("account",) if "account" in config else ())

def _validate_m365_request(execution_channel, intent, connector_id=None):
    if execution_channel not in EXECUTION_CHANNELS or intent not in INTENTS:
        raise Denied("m365 requires execution_channel and intent")
    if execution_channel == "mcp":
        _text(connector_id, "connector_id")
    elif connector_id is not None:
        raise Denied("connector_id is valid only for mcp")

def _reject_m365_request_for_legacy(execution_channel, intent, connector_id):
    if any(value is not None for value in (execution_channel, intent, connector_id)):
        raise Denied("m365-only argument used with legacy provider")

def _find_connector(data, organization_id, connector_id):
    matches = []
    for candidate in data["organizations"]:
        config = candidate.get("providers", {}).get("m365")
        if config is None:
            continue
        for connector in config.get("mcp_connectors", []):
            if connector["connector_id"] == connector_id:
                matches.append((candidate["organization_id"], connector))
    if len(matches) > 1:
        raise Denied("ambiguous m365 connector")
    if not matches:
        return None
    connector_organization_id, connector = matches[0]
    if connector_organization_id != organization_id:
        raise Denied("m365 connector organization mismatch")
    return connector

def _m365_expected_context(config, execution_channel, connector=None, connector_id=None):
    source = connector or config
    fields = ["tenant_id", "sharepoint_host"]
    if "site_path" in source:
        fields.append("site_path")
    fields.append("auth_mode")
    identity_field = "account" if source["auth_mode"] == "delegated" else "application_id"
    fields.append(identity_field)
    expected = {field: source[field] for field in fields}
    if execution_channel == "m365-cli":
        if "m365_connection_name" not in config:
            raise Denied("m365-cli requires m365_connection_name")
        expected["connection_name"] = config["m365_connection_name"]
    elif execution_channel == "mcp":
        expected["connector_id"] = connector_id
    return expected

def _validate_observed_m365(observed, expected):
    if not isinstance(observed, dict) or set(observed) != set(expected):
        raise Denied("invalid observed context shape")
    validators = {
        "account": _account,
        "sharepoint_host": _sharepoint_host,
        "site_path": _site_path,
    }
    for field, expected_value in expected.items():
        if field == "auth_mode":
            if observed.get(field) not in AUTH_MODES:
                raise Denied("invalid observed.auth_mode")
        else:
            validators.get(field, _text)(observed.get(field), "observed." + field)
        if observed[field] != expected_value:
            raise Denied("live context mismatch: " + field)

def _m365_plan(config, execution_channel, intent, connector=None, connector_id=None):
    env = {
        "AZURE_CONFIG_DIR": config["config_root"],
        "AZURE_TENANT_ID": config["tenant_id"],
    }
    site_path = config.get("site_path", "/")
    site_url = "https://" + config["sharepoint_host"] + site_path
    if execution_channel == "graph-powershell":
        if config["auth_mode"] == "delegated":
            steps = [{
                "action": "inspect-graph-delegated-context",
                "read_only": True,
                "Get-MgContext": {"TenantId": config["tenant_id"], "Account": config["account"]},
            }]
        else:
            steps = [{
                "action": "inspect-graph-app-only-organization",
                "read_only": True,
                "request": {"method": "GET", "url": "https://graph.microsoft.com/v1.0/organization?$select=id"},
                "assert": "organization.id equals registry tenant_id",
                "application_id": config["application_id"],
            }]
        steps.append({
            "action": "inspect-sharepoint-site",
            "read_only": True,
            "request": {
                "method": "GET",
                "url": "https://graph.microsoft.com/v1.0/sites/" + config["sharepoint_host"] + ":" + site_path + "?$select=id,webUrl",
            },
            "assert": "webUrl host and path equal registry target",
        })
    elif execution_channel == "m365-cli":
        connection_name = config.get("m365_connection_name")
        if not connection_name:
            raise Denied("m365-cli requires m365_connection_name")
        steps = [
            {"action": "list-m365-connections", "read_only": True, "argv": ["m365", "connection", "list", "--output", "json"]},
            {"action": "select-m365-connection", "cloud_mutation": False, "local_context_change": True, "argv": ["m365", "connection", "use", "--name", connection_name]},
            {"action": "inspect-m365-tenant", "read_only": True, "argv": ["m365", "tenant", "id", "get", "--output", "json"], "assert": "tenantId equals registry tenant_id"},
            {"action": "inspect-m365-site", "read_only": True, "argv": ["m365", "spo", "site", "get", "--url", site_url, "--output", "json"], "assert": "site URL equals registry target"},
        ]
    else:
        if connector is None and intent == "mutate":
            raise Denied("unregistered mcp connector cannot mutate")
        steps = [{
            "action": "inspect-mcp-connector-identity",
            "read_only": True,
            "connector_id": connector_id,
            "oracle": "connector get_me or organization identity",
        }, {
            "action": "inspect-mcp-sharepoint-site",
            "read_only": True,
            "connector_id": connector_id,
            "expected_url": site_url,
        }]
    return {"read_only": True, "execute": False, "env": env, "steps": steps}

def plan_preflight(
    data, provider, organization_id=None, alias=None,
    execution_channel=None, intent=None, connector_id=None,
):
    org, config = resolve(data, provider, organization_id, alias)
    if provider == "m365":
        _validate_m365_request(execution_channel, intent, connector_id)
        connector = _find_connector(data, org["organization_id"], connector_id) if execution_channel == "mcp" else None
        if execution_channel == "mcp" and connector is None and intent == "mutate":
            raise Denied("unregistered mcp connector cannot mutate")
        target = _target(org, provider, config)
        if connector_id is not None:
            target["connector_id"] = connector_id
        result = {
            "decision": "REGISTRY_ALLOW",
            "organization_id": org["organization_id"],
            "provider": provider,
            "target": target,
            "reasons": [],
            "live_context_status": "UNVERIFIED",
            "execution_allowed": False,
            "execution_channel": execution_channel,
            "intent": intent,
            "plan": _m365_plan(config, execution_channel, intent, connector, connector_id),
        }
        if execution_channel == "mcp" and connector is None:
            result["read_only_allowed"] = True
            result["warnings"] = ["unregistered mcp connector: read-only inspection is unverified and grants no execution authorization"]
        return result
    _reject_m365_request_for_legacy(execution_channel, intent, connector_id)
    root = Path(config["project_root"])
    config_root = config.get("config_root")
    if provider in ("azure", "graph"):
        env = {"AZURE_CONFIG_DIR": config_root or str(root / ".azure"), "AZURE_TENANT_ID": config["tenant_id"]}
        if provider == "azure":
            env["AZURE_SUBSCRIPTION_ID"] = config["subscription_id"]
        steps = [{"action": "inspect-azure-context", "read_only": True, **({"account": config["account"]} if "account" in config else {})}]
        if provider == "graph":
            steps = [{"action": "inspect-graph-context", "read_only": True, "TenantId": config["tenant_id"], "Get-MgContext": config["graph_context"], **({"account": config["account"]} if "account" in config else {})}]
    elif provider == "gcp":
        env = {"CLOUDSDK_CONFIG": config_root or str(root / ".config" / "gcloud"), "CLOUDSDK_ACTIVE_CONFIG_NAME": config["configuration"], "CLOUDSDK_CORE_PROJECT": config["project_id"]}
        if "account" in config:
            env["CLOUDSDK_CORE_ACCOUNT"] = config["account"]
        steps = [{"action": "inspect-gcloud-context", "read_only": True, "configuration": config["configuration"], "project": config["project_id"], **({"account": config["account"]} if "account" in config else {})}]
    else:
        env = {"GOOGLE_WORKSPACE_CLI_CONFIG_DIR": config_root or str(root / ".config" / "google-workspace")}
        steps = [{"action": "inspect-google-workspace-context", "read_only": True, "configuration": config["configuration"], "domain": config["domain"], **({"account": config["account"]} if "account" in config else {})}]
    return {"decision": "REGISTRY_ALLOW", "organization_id": org["organization_id"], "provider": provider, "target": _target(org, provider, config), "reasons": [], "live_context_status": "UNVERIFIED", "execution_allowed": False, "plan": {"read_only": True, "execute": False, "env": env, "steps": steps}}

def verify_context(
    data, provider, observed, organization_id=None, alias=None,
    execution_channel=None, intent=None, connector_id=None,
):
    org, config = resolve(data, provider, organization_id, alias)
    _secret_scan(observed)
    if provider == "m365":
        _validate_m365_request(execution_channel, intent, connector_id)
        connector = _find_connector(data, org["organization_id"], connector_id) if execution_channel == "mcp" else None
        expected = _m365_expected_context(config, execution_channel, connector, connector_id)
        _validate_observed_m365(observed, expected)
        target = _target(org, provider, config)
        if connector_id is not None:
            target["connector_id"] = connector_id
        if execution_channel == "mcp" and connector is None:
            if intent == "mutate":
                raise Denied("unregistered mcp connector cannot mutate")
            return {
                "decision": "REGISTRY_ALLOW",
                "organization_id": org["organization_id"],
                "provider": provider,
                "target": target,
                "reasons": [],
                "live_context_status": "UNVERIFIED",
                "execution_allowed": False,
                "execution_channel": execution_channel,
                "intent": intent,
                "read_only_allowed": True,
                "warnings": ["unregistered mcp connector: read-only inspection is unverified and grants no execution authorization"],
            }
        return {
            "decision": "ALLOW",
            "organization_id": org["organization_id"],
            "provider": provider,
            "target": target,
            "reasons": [],
            "live_context_status": "VERIFIED",
            "execution_allowed": True,
            "execution_channel": execution_channel,
            "intent": intent,
            "read_only_allowed": intent == "read",
            "warnings": [],
        }
    _reject_m365_request_for_legacy(execution_channel, intent, connector_id)
    fields = _context_fields(provider, config)
    if not isinstance(observed, dict) or set(observed) != set(fields):
        raise Denied("invalid observed context shape")
    for field in fields:
        validator = _account if field == "account" else _text
        validator(observed.get(field), "observed." + field)
        if observed[field] != config[field]:
            raise Denied("live context mismatch: " + field)
    return {"decision": "ALLOW", "organization_id": org["organization_id"], "provider": provider, "target": _target(org, provider, config), "reasons": [], "live_context_status": "VERIFIED", "execution_allowed": True}

def _denied(reason):
    return {"decision": "DENY", "organization_id": None, "provider": None, "target": {}, "reasons": [reason]}

def main(argv=None):
    parser = GateArgumentParser()
    parser.add_argument("command", choices=("validate-registry", "resolve", "plan-preflight", "verify-context"))
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    parser.add_argument("--provider", choices=PROVIDERS)
    parser.add_argument("--organization-id")
    parser.add_argument("--alias")
    parser.add_argument("--observed-file")
    parser.add_argument("--execution-channel", choices=EXECUTION_CHANNELS)
    parser.add_argument("--intent", choices=INTENTS)
    parser.add_argument("--connector-id")
    try:
        args = parser.parse_args(argv)
        data = load_registry(args.registry)
        if args.command == "validate-registry":
            result = {"decision": "ALLOW", "status": "PASS", "reasons": [], "organizations": len(data["organizations"])}
        elif args.command == "resolve":
            org, config = resolve(data, args.provider, args.organization_id, args.alias)
            result = {"decision": "REGISTRY_ALLOW", "organization_id": org["organization_id"], "provider": args.provider, "target": _target(org, args.provider, config), "reasons": [], "live_context_status": "UNVERIFIED", "execution_allowed": False}
        elif args.command == "plan-preflight":
            result = plan_preflight(
                data, args.provider, args.organization_id, args.alias,
                args.execution_channel, args.intent, args.connector_id,
            )
        else:
            if not args.observed_file:
                raise Denied("verify-context requires --observed-file")
            try:
                with Path(args.observed_file).open(encoding="utf-8") as stream:
                    observed = json.load(stream)
            except (OSError, ValueError) as exc:
                raise Denied("cannot read observed context") from exc
            result = verify_context(
                data, args.provider, observed, args.organization_id, args.alias,
                args.execution_channel, args.intent, args.connector_id,
            )
        print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except Denied as exc:
        print(json.dumps(_denied(str(exc)), ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 2
    except CliFailure:
        print(json.dumps(_denied("invalid command line"), ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 1
    except Exception:
        print(json.dumps(_denied("unexpected internal error"), ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 1

if __name__ == "__main__":
    sys.exit(main())
