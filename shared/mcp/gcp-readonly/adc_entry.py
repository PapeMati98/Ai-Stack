"""OAuth read-only transport for the typed GCP MCP surface.

This entrypoint never invokes gcloud or another subprocess. Credential files are
trusted launch inputs, never tool arguments. Google's SDK handles OAuth refresh.
"""
from __future__ import annotations

import argparse
import configparser
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

# -I removes environment and current-directory Python import influence.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server

SCOPES = frozenset({
    "https://www.googleapis.com/auth/cloud-platform.read-only",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
})
ENDPOINTS = {
    "www.googleapis.com": r"/oauth2/v2/userinfo",
    "cloudresourcemanager.googleapis.com": r"/v1/projects/[a-z][a-z0-9-]{4,61}[a-z0-9]",
    "serviceusage.googleapis.com": r"/v1/projects/[0-9]{1,24}/services",
}


def _safe_error(message="read-only transport failed"):
    return server.SafeToolError(message)


def google_transport(profile, credential_file):
    """Load only the installed, explicitly trusted Cloud SDK's dependencies."""
    vendor = Path(profile.gcloud_python_entrypoint).resolve().parent / "third_party"
    if not (vendor / "google" / "auth" / "__init__.py").is_file():
        raise _safe_error("Google authentication library unavailable")
    sys.path.insert(0, str(vendor))
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import AuthorizedSession, Request
    import requests

    class ReadOnlyCredentials(Credentials):
        def refresh(self, request):
            try:
                super().refresh(request)
                granted = set((self.granted_scopes if self.granted_scopes is not None else self.scopes) or ())
                if granted != SCOPES or not self.has_scopes(SCOPES):
                    raise _safe_error("OAuth scopes rejected")
            except server.SafeToolError:
                raise
            except Exception:
                raise _safe_error("OAuth refresh requires attention") from None

    creds = ReadOnlyCredentials.from_authorized_user_file(str(credential_file), scopes=sorted(SCOPES))
    if not creds.refresh_token:
        raise _safe_error("Persistent OAuth authorization required")
    refresh_session = requests.Session()
    refresh_session.trust_env = False
    request = Request(session=refresh_session)
    def bounded_request(*args, **kwargs):
        kwargs["timeout"] = 30
        return request(*args, **kwargs)
    # A real refresh on process startup verifies persistence without a browser.
    creds.refresh(bounded_request)
    session = AuthorizedSession(creds, auth_request=bounded_request)
    session.trust_env = False
    return session


class ReadonlyADCBackend:
    def __init__(self, policy, credential_file, transport_factory=google_transport):
        self.policy = policy
        self.credential_file = Path(credential_file)
        if not self.credential_file.is_absolute():
            raise _safe_error("Credential path rejected")
        resolved = self.credential_file.resolve()
        server._outside_repository(resolved)
        for profile in policy.profiles.values():
            if not server._is_relative_to(resolved, Path(profile.cloudsdk_config).resolve()):
                raise _safe_error("Credential path rejected")
        if len({(p.account, str(Path(p.gcloud_python_entrypoint).resolve())) for p in policy.profiles.values()}) != 1:
            raise _safe_error("OAuth identity must be isolated")
        self.factory = transport_factory
        self.transport = None
        self.oauth_identity = None

    def _http(self, profile):
        if self.transport is None:
            self.transport = self.factory(profile, self.credential_file)
        return self.transport

    def _get(self, profile, url, params, timeout, maximum):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in ENDPOINTS or parts.port or parts.username or parts.password or parts.query or parts.fragment:
            raise _safe_error("Endpoint rejected")
        if not re.fullmatch(ENDPOINTS[parts.hostname], parts.path):
            raise _safe_error("Endpoint rejected")
        try:
            response = self._http(profile).request("GET", url, params=params, timeout=min(float(timeout), 30), allow_redirects=False, stream=True)
            try:
                if response.status_code != 200:
                    raise _safe_error("GCP read unavailable (HTTP %d)" % int(response.status_code))
                content = bytearray()
                for chunk in response.iter_content(chunk_size=4096):
                    content.extend(chunk)
                    if len(content) > maximum:
                        raise _safe_error("GCP response exceeds limit")
                data = json.loads(content, object_pairs_hook=server._pairs)
                if not isinstance(data, dict):
                    raise _safe_error("GCP response rejected")
                return data
            finally:
                response.close()
        except server.SafeToolError:
            raise
        except Exception:
            raise _safe_error() from None

    def _identity(self, profile, timeout, maximum):
        data = self._get(profile, "https://www.googleapis.com/oauth2/v2/userinfo", {"fields": "email,verified_email"}, timeout, maximum)
        if data.get("email") != profile.account or data.get("verified_email") is not True:
            raise _safe_error("OAuth account does not match policy")
        self.oauth_identity = profile.account
        return [{"account": profile.account, "status": "ACTIVE"}]

    def _configuration(self, profile):
        path = Path(profile.cloudsdk_config) / "configurations" / ("config_" + profile.configuration)
        if path.stat().st_size > 65536:
            raise _safe_error("Configuration rejected")
        parser = configparser.ConfigParser(interpolation=None)
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
        account = parser.get("core", "account", fallback=None)
        project = parser.get("core", "project", fallback=None)
        if account != profile.account or project != profile.project_id:
            raise _safe_error("Configuration does not match policy")
        return {"name": profile.configuration, "properties": {"core": {"account": account, "project": project}}}

    def _project(self, profile, timeout, maximum):
        data = self._get(profile, "https://cloudresourcemanager.googleapis.com/v1/projects/" + profile.project_id,
                         {"fields": "projectId,projectNumber,name,lifecycleState"}, timeout, maximum)
        if data.get("projectId") != profile.project_id or not re.fullmatch(r"[0-9]{1,24}", str(data.get("projectNumber", ""))):
            raise _safe_error("Google project does not match policy")
        return data

    def _items(self, profile, url, params, key, timeout, maximum, count_limit):
        items = []
        for _ in range(5):
            data = self._get(profile, url, params, timeout, maximum)
            page = data.get(key, [])
            if not isinstance(page, list):
                raise _safe_error("GCP list rejected")
            items.extend(page)
            token = data.get("nextPageToken")
            if len(items) > count_limit or not token:
                return items[:count_limit + 1]
            if not isinstance(token, str) or len(token) > 4096:
                raise _safe_error("GCP pagination rejected")
            params = {**params, "pageToken": token}
        raise _safe_error("GCP pagination limit reached")

    def run(self, argv, env, timeout, max_output):
        profile = next((p for p in self.policy.profiles.values() if argv[:len(server._prefix(p))] == server._prefix(p)), None)
        if profile is None or env.get("CLOUDSDK_CONFIG") != profile.cloudsdk_config:
            raise _safe_error("Unknown command profile")
        operation = None
        region = service_name = None
        if argv == server.command_config_describe(profile):
            operation = "configuration"
        elif argv == server.command_auth_list(profile):
            operation = "identity"
        elif argv == server.command_project_describe(profile):
            operation = "project"
        elif argv == server.command_enabled_services(profile):
            operation = "apis"
        # No credentials or HTTP are touched until the ENTIRE vector matches.
        if operation is None:
            raise _safe_error("Command is not read-only allowlisted")
        if operation == "configuration":
            result = self._configuration(profile)
        elif operation == "identity":
            result = self._identity(profile, timeout, max_output)
        else:
            self._configuration(profile)
            self._identity(profile, timeout, max_output)
            project = self._project(profile, timeout, max_output)
            if operation == "project":
                result = project
            elif operation == "apis":
                result = self._items(profile, "https://serviceusage.googleapis.com/v1/projects/" + str(project["projectNumber"]) + "/services",
                                     {"filter": "state:ENABLED", "pageSize": 200, "fields": "services(config(name,title)),nextPageToken"}, "services", timeout, max_output, 500)
        raw = json.dumps(result, ensure_ascii=False)
        if len(raw.encode("utf-8")) > max_output:
            raise _safe_error("GCP output exceeds limit")
        return raw


def main():
    parser = argparse.ArgumentParser(description="OAuth read-only GCP metadata MCP")
    parser.add_argument("--policy", required=True)
    parser.add_argument("--credentials-file", required=True)
    args = parser.parse_args()
    try:
        policy = server.load_policy_file(args.policy)
        backend = ReadonlyADCBackend(policy, args.credentials_file)
        return server.serve(sys.stdin.buffer, sys.stdout.buffer, server.Session(policy, backend, allowed_tools=("context_status", "project_info", "enabled_services")))
    except Exception:
        sys.stderr.write("Read-only GCP transport configuration rejected\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
