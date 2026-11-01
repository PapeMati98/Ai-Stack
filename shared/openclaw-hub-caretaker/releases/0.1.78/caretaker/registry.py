"""Read the curated project alias registry without trusting its prose."""
from __future__ import annotations

import json
import re
from pathlib import Path


SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,100}\Z")
JSON_FENCE = re.compile(r"^```json[ \t]*\r?\n(.*?)^```[ \t]*$", re.M | re.S)
PROJECT_PAGE = re.compile(r"wiki/projects/[a-z0-9][a-z0-9-]*\.md\Z")


def read_registry(vault: Path) -> tuple[dict | None, str | None]:
    """Return validated registry data, or a content-free warning code."""
    root = Path(vault)
    path = root / "wiki" / "registry.md"
    if not path.is_file():
        return None, "registry_missing"
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            return None, "registry_invalid"
        data = path.read_bytes()
        if len(data) > 1024 * 1024:
            return None, "registry_invalid"
        match = JSON_FENCE.search(data.decode("utf-8-sig").replace("\r\n", "\n"))
        value = json.loads(match.group(1)) if match else None
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            return None, "registry_invalid"
        projects, aliases = value.get("projects"), value.get("aliases")
        if not isinstance(projects, dict) or not isinstance(aliases, dict):
            return None, "registry_invalid"
        if any(not isinstance(k, str) or not SLUG.fullmatch(k)
               or not isinstance(v, dict) or (v.get("main_page") is not None
                   and (not isinstance(v["main_page"], str)
                        or not PROJECT_PAGE.fullmatch(v["main_page"])))
               for k, v in projects.items()):
            return None, "registry_invalid"
        if any(not isinstance(alias, str) or not SLUG.fullmatch(alias)
               or alias in projects or not isinstance(target, str)
               or target not in projects for alias, target in aliases.items()):
            return None, "registry_invalid"
        return value, None
    except (OSError, UnicodeError, ValueError, TypeError):
        return None, "registry_invalid"


def resolve_alias(slug: str | None, registry: dict | None) -> str | None:
    if slug is None or registry is None:
        return slug
    return registry["aliases"].get(slug, slug)


def main_page(slug: str, registry: dict | None) -> str:
    project = (registry or {}).get("projects", {}).get(slug, {})
    return project.get("main_page") or f"wiki/projects/{slug}.md"
