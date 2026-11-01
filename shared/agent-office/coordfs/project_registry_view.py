"""Read-only projection of the owner supplied registry-v2 metadata.

This is a separate snapshot. Import never opens or migrates the legacy Office
project registry, work ledger, or the source registry file.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic import exclusive_create, exclusive_mutex, replace_with_backoff, unlink_with_backoff
from .errors import CoordError
from .store import assert_writable


IMPORT_PRINCIPALS = frozenset({
    "claude-ailab-88900f5b-3914-40b8-bf9b-65be18db6e8c",
    "claude-ailab-4687ee1c-f0ca-4359-914e-6abd7eb0dd5c",
})
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{2,63}\Z")
PROJECT_FIELDS = frozenset({"title", "display_name", "family", "aliases", "org_links", "org_resolution", "folders", "hub_main_page", "hub_latest_page", "status", "canonical", "topics"})
FORBIDDEN = frozenset({"body", "prompt", "contacts", "contact", "style", "transcript", "messages"})
REMOTE_PROJECTION_FIELD = "_agent_office_project_registry_projection_v1"

# Embedded I1 registry-v2 schema 1.2 subset. The source schema has no runtime
# dependency: keep this validation aligned with the frozen contract and only
# implement the keywords used by that schema.
_REGISTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["schema", "version", "generated_at", "organizations", "workspace_groups", "projects", "topics", "people"],
    "properties": {
        "schema": {"const": "agent-stack/registry-v2"},
        "version": {"type": "string", "minLength": 1},
        "status": {"type": "string"},
        "generated_at": {"type": "string", "minLength": 10},
        "generated_by": {"type": "string"},
        "sources": {"type": "object"},
        "rules": {"type": "array", "items": {"type": "string"}},
        "organizations": {"type": "array", "items": {"$ref": "#/$defs/organization"}},
        "workspace_groups": {"type": "array", "items": {"$ref": "#/$defs/workspace_group"}},
        "projects": {
            "type": "object",
            "propertyNames": {"$ref": "#/$defs/slug"},
            "additionalProperties": {"$ref": "#/$defs/project"},
        },
        "topics": {"type": "array", "items": {"$ref": "#/$defs/topic"}},
        "people": {"type": "array", "items": {"$ref": "#/$defs/person"}},
        "known_gaps": {"type": "array", "items": {"type": "string"}},
        "phase": {"enum": ["base", "final"]},
        "frozen_source": {"type": "object"},
        "families": {"type": "array", "items": {"$ref": "#/$defs/family"}},
    },
    "additionalProperties": False,
    "$defs": {
        "slug": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{2,63}$"},
        "nullable_string": {"type": ["string", "null"]},
        "organization": {
            "type": "object",
            "required": ["id", "kind", "status", "aliases"],
            "properties": {
                "id": {"$ref": "#/$defs/slug"},
                "kind": {"type": "string", "minLength": 1},
                "status": {"enum": ["confirmed", "candidate", "pending"]},
                "aliases": {"type": "array", "items": {"type": "string"}},
                "display_name": {"$ref": "#/$defs/nullable_string"},
                "email_domains": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "workspace_group": {
            "type": "object",
            "required": ["name", "org"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "org": {"oneOf": [{"$ref": "#/$defs/slug"}, {"type": "null"}]},
                "note": {"type": "string"},
                "tools": {
                    "type": "array",
                    "items": {"enum": ["claude-desktop", "codex-desktop", "zcode", "kimi-desktop", "antigravity"]},
                },
            },
            "additionalProperties": False,
        },
        "org_link": {
            "type": "object",
            "required": ["org", "role", "status"],
            "properties": {
                "org": {"$ref": "#/$defs/slug"},
                "role": {"enum": ["owner", "client", "partner", "interface"]},
                "status": {"enum": ["confirmed", "inferred", "candidate", "pending"]},
                "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
            },
            "additionalProperties": False,
        },
        "project": {
            "type": "object",
            "required": [
                "title", "family", "aliases", "org_links", "org_resolution", "folders",
                "hub_main_page", "hub_latest_page", "status",
            ],
            "properties": {
                "title": {"$ref": "#/$defs/nullable_string"},
                "display_name": {"$ref": "#/$defs/nullable_string"},
                "family": {"$ref": "#/$defs/nullable_string"},
                "aliases": {"type": "array", "items": {"type": "string"}},
                "org_links": {"type": "array", "items": {"$ref": "#/$defs/org_link"}},
                "org_resolution": {"type": "string", "minLength": 1},
                "folders": {"type": "array", "items": {"type": "string"}},
                "hub_main_page": {"$ref": "#/$defs/nullable_string"},
                "hub_latest_page": {"$ref": "#/$defs/nullable_string"},
                "status": {
                    "oneOf": [
                        {"type": "null"},
                        {"enum": ["active", "paused", "done", "pending", "archived"]},
                    ]
                },
                "canonical": {"oneOf": [{"$ref": "#/$defs/slug"}, {"type": "null"}]},
                "topics": {"type": "array", "items": {"$ref": "#/$defs/slug"}},
                "related": {"type": "array", "items": {"$ref": "#/$defs/slug"}},
                "current_state_source": {
                    "oneOf": [
                        {"type": "null"},
                        {"type": "string", "pattern": "^src-[0-9]{8}T[0-9]{9}Z-[a-z0-9-]+$"},
                    ]
                },
            },
            "additionalProperties": False,
        },
        "topic": {
            "type": "object",
            "required": ["id", "name", "aliases", "status", "projects"],
            "properties": {
                "id": {"$ref": "#/$defs/slug"},
                "name": {"type": "string", "minLength": 1},
                "aliases": {"type": "array", "items": {"type": "string"}},
                "status": {"enum": ["draft", "provisional", "confirmed"]},
                "projects": {"type": "array", "items": {"$ref": "#/$defs/slug"}},
                "page": {"$ref": "#/$defs/nullable_string"},
            },
            "additionalProperties": False,
        },
        "person": {
            "type": "object",
            "required": ["id", "name", "orgs", "projects"],
            "properties": {
                "id": {"$ref": "#/$defs/slug"},
                "name": {"type": "string", "minLength": 1},
                "orgs": {"type": "array", "items": {"$ref": "#/$defs/slug"}},
                "projects": {"type": "array", "items": {"$ref": "#/$defs/slug"}},
                "role": {"$ref": "#/$defs/nullable_string"},
                "page": {"$ref": "#/$defs/nullable_string"},
            },
            "additionalProperties": False,
        },
        "family": {
            "type": "object",
            "required": ["id", "title"],
            "properties": {
                "id": {"$ref": "#/$defs/slug"},
                "title": {"type": "string", "minLength": 1},
                "description": {"$ref": "#/$defs/nullable_string"},
                "order": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
    },
}


def _reject(message: str, code: str = "invalid_registry") -> None:
    raise CoordError(message, code=code)


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        _reject("registry generated_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _reject("registry generated_at is invalid")
    if parsed.tzinfo is None:
        _reject("registry generated_at needs a timezone")
    return parsed.astimezone(timezone.utc)


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict) and all(isinstance(key, str) for key in value)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return type(value) is int or (type(value) is float and math.isfinite(value))
    return False


def _resolve_schema_ref(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        _reject("registry schema reference is unsupported")
    resolved: Any = root
    for part in reference[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(resolved, dict) or part not in resolved:
            _reject("registry schema reference is invalid")
        resolved = resolved[part]
    if not isinstance(resolved, dict):
        _reject("registry schema reference is invalid")
    return resolved


def _validate_schema(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> None:
    if "$ref" in schema:
        _validate_schema(value, _resolve_schema_ref(root, schema["$ref"]), root)

    if "oneOf" in schema:
        matches = 0
        for alternative in schema["oneOf"]:
            try:
                _validate_schema(value, alternative, root)
            except CoordError:
                continue
            matches += 1
        if matches != 1:
            _reject("registry value does not match exactly one schema branch")

    expected_types = schema.get("type")
    if expected_types is not None:
        types = expected_types if isinstance(expected_types, list) else [expected_types]
        if not any(_schema_type_matches(value, expected) for expected in types):
            _reject("registry value has the wrong schema type")

    if "const" in schema and value != schema["const"]:
        _reject("registry value violates schema const")
    if "enum" in schema and value not in schema["enum"]:
        _reject("registry value violates schema enum")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            _reject("registry string is shorter than schema minimum")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            _reject("registry string violates schema pattern")

    if type(value) in (int, float):
        if type(value) is float and not math.isfinite(value):
            _reject("registry number is invalid")
        if "minimum" in schema and value < schema["minimum"]:
            _reject("registry number is below schema minimum")
        if "maximum" in schema and value > schema["maximum"]:
            _reject("registry number is above schema maximum")

    if isinstance(value, list) and "items" in schema:
        for item in value:
            _validate_schema(item, schema["items"], root)

    if isinstance(value, dict):
        if "required" in schema and not set(schema["required"]) <= value.keys():
            _reject("registry object is missing schema fields")
        properties = schema.get("properties", {})
        if "propertyNames" in schema:
            for key in value:
                _validate_schema(key, schema["propertyNames"], root)
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _validate_schema(item, properties[key], root)
            elif additional is False:
                _reject("registry object has unsupported schema fields")
            elif isinstance(additional, dict):
                _validate_schema(item, additional, root)


def _reject_forbidden_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                _reject("registry object keys must be strings")
            if key.casefold() in FORBIDDEN:
                _reject("registry contains forbidden privacy metadata")
            _reject_forbidden_fields(nested)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_fields(item)
    elif value is None or isinstance(value, (str, bool)) or type(value) is int:
        return
    elif type(value) is float:
        if not math.isfinite(value):
            _reject("registry number is invalid")
    else:
        _reject("registry contains a non-JSON value")


def _validate(data: Any) -> dict[str, Any]:
    _reject_forbidden_fields(data)
    _validate_schema(data, _REGISTRY_SCHEMA, _REGISTRY_SCHEMA)
    root = data
    # Preserve the timestamp and isolation constraints of the original importer.
    _timestamp(root["generated_at"])
    return root


def _project_snapshot(projects: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        slug: {key: value for key, value in project.items() if key in PROJECT_FIELDS}
        for slug, project in projects.items()
    }


def _source_digest(validated: dict[str, Any]) -> str:
    encoded = json.dumps(validated, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prepare_remote_registry_import(data: Any) -> dict[str, Any]:
    """Validate the full source locally, then return a PII-free RPC envelope."""
    validated = _validate(data)
    return {
        REMOTE_PROJECTION_FIELD: {
            "schema": validated["schema"],
            "version": validated["version"],
            "generated_at": validated["generated_at"],
            "source_sha256": _source_digest(validated),
            "projects": _project_snapshot(validated["projects"]),
        }
    }


def _validate_remote_projection(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {REMOTE_PROJECTION_FIELD}:
        _reject("remote registry projection envelope is invalid")
    envelope = data[REMOTE_PROJECTION_FIELD]
    expected = {"schema", "version", "generated_at", "source_sha256", "projects"}
    if not isinstance(envelope, dict) or set(envelope) != expected:
        _reject("remote registry projection envelope is invalid")
    _reject_forbidden_fields(envelope)
    if envelope["schema"] != "agent-stack/registry-v2":
        _reject("unsupported registry schema")
    _validate_schema(envelope["version"], _REGISTRY_SCHEMA["properties"]["version"], _REGISTRY_SCHEMA)
    _validate_schema(envelope["generated_at"], _REGISTRY_SCHEMA["properties"]["generated_at"], _REGISTRY_SCHEMA)
    _timestamp(envelope["generated_at"])
    if not isinstance(envelope["source_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", envelope["source_sha256"]) is None:
        _reject("remote registry source hash is invalid")
    _validate_schema(envelope["projects"], _REGISTRY_SCHEMA["properties"]["projects"], _REGISTRY_SCHEMA)
    return envelope


class ProjectRegistryView:
    def __init__(self, home: Path) -> None:
        self.home = Path(home).expanduser()
        self.root = self.home / "office" / "project_registry_view"
        self.path = self.root / "snapshot.json"
        self.mutex_path = self.root / "snapshot.lock"

    def _read(self) -> dict[str, Any] | None:
        assert_writable(self.home)
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError) as exc:
            raise CoordError("project projection is unreadable", code="registry_corrupt") from exc
        if not isinstance(state, dict) or state.get("schema") != 1 or not isinstance(state.get("projects"), dict) or not isinstance(state.get("source_sha256"), str) or not isinstance(state.get("registry_version"), str):
            raise CoordError("project projection is corrupt", code="registry_corrupt")
        return state

    @staticmethod
    def _metadata(state: dict[str, Any] | None) -> dict[str, Any]:
        if state is None:
            return {"registry_version": None, "generated_at": None, "source_sha256": None, "imported_by": None, "freshness": "empty"}
        generated = _timestamp(state["generated_at"])
        age = (datetime.now(timezone.utc) - generated).total_seconds()
        return {key: state[key] for key in ("registry_version", "generated_at", "source_sha256", "imported_by")} | {"freshness": "future" if age < -300 else "fresh" if age <= 14 * 86400 else "stale"}

    def import_registry(self, data: Any, *, imported_by: str | None = None) -> dict[str, Any]:
        assert_writable(self.home)
        if imported_by not in IMPORT_PRINCIPALS:
            raise CoordError("registry importer is not authorized", code="forbidden")
        if isinstance(data, dict) and REMOTE_PROJECTION_FIELD in data:
            envelope = _validate_remote_projection(data)
            version = envelope["version"]
            generated_at = envelope["generated_at"]
            digest = envelope["source_sha256"]
            projected_projects = _project_snapshot(envelope["projects"])
        else:
            validated = _validate(data)
            # Canonical JSON gives the same identity for parsed file and API payload.
            digest = _source_digest(validated)
            version = validated["version"]
            generated_at = validated["generated_at"]
            projected_projects = _project_snapshot(validated["projects"])
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_mutex(self.mutex_path):
            previous = self._read()
            if previous and previous["source_sha256"] == digest and previous["registry_version"] == version:
                return {"status": "ok", "code": "unchanged", "metadata": self._metadata(previous), "drift": False}
            state = {"schema": 1, "registry_version": version, "generated_at": generated_at, "source_sha256": digest, "imported_by": imported_by, "projects": projected_projects}
            temp = self.path.with_name(f"{self.path.name}.tmp-{uuid.uuid4().hex}")
            try:
                exclusive_create(temp, json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                if not replace_with_backoff(temp, self.path):
                    raise CoordError("project projection replace remained busy", code="busy")
            finally:
                if temp.exists():
                    unlink_with_backoff(temp)
            return {"status": "ok", "code": "imported", "metadata": self._metadata(state), "drift": previous is not None, "previous": self._metadata(previous)}

    def metadata(self) -> dict[str, Any]:
        return {"status": "ok", "code": "metadata", "metadata": self._metadata(self._read())}

    def get(self, slug: str) -> dict[str, Any]:
        if not isinstance(slug, str) or not SLUG.fullmatch(slug):
            raise CoordError("invalid project slug", code="invalid_project_slug")
        state = self._read()
        if state is None or slug not in state["projects"]:
            return {"status": "warning", "code": "unknown_project", "project_slug": slug, "project": None, "metadata": self._metadata(state)}
        return {"status": "ok", "code": "found", "project_slug": slug, "project": json.loads(json.dumps(state["projects"][slug])), "metadata": self._metadata(state)}

    def list(self) -> dict[str, Any]:
        state = self._read()
        projects = {} if state is None else state["projects"]
        return {"status": "ok", "code": "listed", "projects": json.loads(json.dumps(projects)), "count": len(projects), "metadata": self._metadata(state)}
