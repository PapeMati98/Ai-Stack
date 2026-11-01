"""I1 view generator: wiki/registry.md, START.md, WHERE.md from registry v2.

Owner: ZCode (program System Projektow, contract I1). Deterministic, stdlib
only. Input: registry JSON (seed contracts/I1-registry-v2.frozen-v0.json or the
live shared/knowledge-hub/registry/registry-v2.json). Output directory receives
the three generated views plus views-manifest.json. The live WHERE.md is the
merge base: only the mail rows are rewritten, library sections are preserved
byte-for-byte. The vault itself is never written here — publication goes
through the curation transaction (owner: ZCode).

CANDIDATE (program leader, 2026-10-05): v1 parity after tx-a3ee020c - family
titles/descriptions, related, current_state_source and the START.md columns
Slug/Current state/Folder/Aliases/Related come back from registry v2 (schema
v1.2 + contracts/I1-v1-carryover.json); WHERE.md step is idempotent. Gate before
publication: contracts/check_views_parity.py PASS."""

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

# LUDZIE-B n1: strict field whitelist for the People section. Only these keys of a
# person entry may ever reach a generated view; any other key (contacts, style,
# email, phone, ...) is dropped here by construction, not by regex filtering.
PEOPLE_ALLOWED_FIELDS = ("id", "name", "orgs", "role", "projects")

MAIL_ROW_OLD = "| Mail and calendar | Outlook / Gmail connectors of the session, never a local mailbox export | only with the owner's instruction for the task |"
MAIL_ROW_NEW = ("| Mail and calendar | `mail_direct.py` (skill email; POP3/IMAP/Gmail/Graph, read-only) with the owner's instruction; "
                "harvest archive: `%USERPROFILE%\\.agents\\private-artifacts\\kh-mail-harvest\\<alias>-<window>\\` ("
                "private mailboxes are private context, never delegated) | only with the owner's instruction for the task |")
# Narrow recognizer for previously generated mail_direct.py rows (which carried
# a historical per-installation consent claim). Matched only by the stable
# mail_direct.py + owner-consent markers - no private text is retained here.
MAIL_ROW_LEGACY_RE = re.compile(r"(?m)^\| Mail and calendar \|.*mail_direct\.py.*owner consent.*\|$")
MAIL_KNOWLEDGE_ROW_NEW = ("| Mail knowledge & extraction | Kod: `<configured-code-root>\\mail-knowledge`; harvest artifacts: "
                          "`<private-archive-root>\\kh-mail-harvest\\` (ZCode, coverage ledger `coverage-ledger.json`); "
                          "docelowe archiwum: `<private-archive-root>\\mail-knowledge-data\\<alias>\\archive.sqlite` | ekstrakcja wiedzy z maili; archiwum poufne poza OneDrive |")


def fm(title: str, summary: str, tags=("system", "navigation")) -> str:
    tag_lines = "\n".join(f"  - {t}" for t in tags)
    return (f"---\ntitle: \"{title}\"\ntype: system\nstatus: reviewed\n"
            f"summary: \"{summary}\"\nlast_verified: {date.today().isoformat()}\n"
            f"sources: []\ntags:\n{tag_lines}\n---\n")


def org_label(reg: dict, org_id: str | None) -> str:
    if not org_id:
        return "—"
    org = next((o for o in reg.get("organizations", []) if o.get("id") == org_id), None)
    return org.get("display_name") or org_id if org else org_id


def person_business_fields(person: dict) -> dict:
    """Project a person entry down to the PEOPLE_ALLOWED_FIELDS whitelist.

    Builds the row exclusively from the whitelisted keys (id, name, orgs, role,
    projects). No other key of the person entry - contacts, style, email,
    phone or future fields - can reach a rendered view through this helper."""
    return {k: person.get(k) for k in PEOPLE_ALLOWED_FIELDS}


def project_org_links(p: dict) -> str:
    links = p.get("org_links") or []
    if not links:
        return "—"
    return ", ".join(f"{l.get('org')}({l.get('role')}, {l.get('confidence')})" for l in links)


def family_order(reg: dict) -> list[dict]:
    """Families in registry order (v1 carryover: id, title, description); unknown ids fall back to the id."""
    declared = sorted(reg.get("families") or [], key=lambda f: (f.get("order", 10**6), f.get("id")))
    known = {f.get("id") for f in declared}
    used = sorted({p.get("family") or "unassigned" for p in reg.get("projects", {}).values()} - known)
    return declared + [{"id": f, "title": f, "description": None} for f in used]


def render_registry(reg: dict) -> str:
    projects = reg.get("projects", {})
    families: dict[str, list] = {}
    for slug_, p in projects.items():
        families.setdefault(p.get("family") or "unassigned", []).append(slug_)
    v1_projects = {}
    aliases = {}
    for slug_, p in projects.items():
        v1_projects[slug_] = {
            "title": p.get("title") or slug_,
            "family": p.get("family") or "unassigned",
            "main_page": p.get("hub_main_page"),
            "latest_page": p.get("hub_latest_page"),
            "aliases": list(p.get("aliases") or []),
            "related": list(p.get("related") or []),
            "folders": list(p.get("folders") or []),
            "current_state_source": p.get("current_state_source"),
        }
        for alias in (p.get("aliases") or []):
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", alias):
                aliases[alias] = slug_
    body = {
        # v1 shape kept gateway-compatible (knowledge.load_project_registry);
        # the v2 axes ride as an extra key the gateway does not inspect.
        "schema_version": 1,
        "generated_at": reg.get("generated_at"),
        "navigation_page": "wiki/START.md",
        "families": [{"id": f["id"], "title": f.get("title") or f["id"], "projects": sorted(families[f["id"]])}
                     for f in family_order(reg) if f["id"] in families],
        "projects": v1_projects,
        "aliases": aliases,
        "registry_v2": {
            "version": reg.get("version"), "status": reg.get("status"),
            "organizations": reg.get("organizations", []),
            "workspace_groups": reg.get("workspace_groups", []),
            "topics": reg.get("topics", []),
            "people": [person_business_fields(p) for p in reg.get("people", [])],
            "known_gaps": reg.get("known_gaps", []),
        },
    }
    text = fm("Project registry", "Machine-readable registry view: gateway-compatible v1 core (families, canonical slugs, aliases, folders, pages) plus registry_v2 axes (organizations, topics, people, gaps). Human view: START.md.")
    text += ("\n# Project registry\n\nGenerated from the registry v2 source of truth"
             f" (version `{reg.get('version')}`). Curators regenerate this page; do not edit it by hand.\n\n"
             "```json\n" + json.dumps(body, ensure_ascii=False, indent=1) + "\n```\n")
    return text


def page_link(page: str | None) -> str:
    """wiki/projects/x.md -> [x](projects/x.md) relative to wiki/START.md."""
    if not page:
        return "-"
    rel = page[len("wiki/"):] if page.startswith("wiki/") else page
    return f"[{Path(rel).stem}]({rel})"


def state_link(source_id: str | None) -> str:
    match = re.fullmatch(r"src-(\d{4})(\d{2})(\d{2})T\d{9}Z-[a-z0-9-]+", source_id or "")
    if not match:
        return "-"
    year, month, day = match.groups()
    return f"[snapshot {year}-{month}-{day}](../raw/sources/{year}/{month}/{source_id}/original.md)"


def code_list(values, sep: str = ", ") -> str:
    return sep.join(f"`{v}`" for v in values) if values else "-"


def anchor(title: str) -> str:
    return re.sub(r"[^a-z0-9 -]", "", title.lower()).replace(" ", "-")


def render_start(reg: dict) -> str:
    projects = reg.get("projects", {})
    families: dict[str, list] = {}
    for slug, p in sorted(projects.items()):
        families.setdefault(p.get("family") or "unassigned", []).append(slug)
    ordered = [f for f in family_order(reg) if f["id"] in families]
    text = fm("Start here: project map", "One-hop map generated from registry v2: project families (canonical page, newest page, current-state snapshot, folders, aliases, related), organizations axis and topics. Machine-readable copy: registry.md.")
    text += "\n# Start here: project map\n\n"
    text += ("Generated from the registry v2 source of truth"
             f" (version `{reg.get('version')}`). Open the **main page** for the curated synthesis, the **newest page** for the latest "
             "published section, and **current state** for the dated snapshot. **Agents:** `knowledge.py project-brief --project-slug <slug>` "
             "with the slug from the tables below; an alias resolves to its project. Machine view: [registry.md](registry.md). "
             "Where things live: [WHERE.md](WHERE.md).\n\n")
    text += "Families: " + " · ".join(f"[{f.get('title') or f['id']}](#{anchor(f.get('title') or f['id'])})" for f in ordered) + "\n\n"
    text += "## Organizations\n\n| Organization | Status | Projects (role) |\n|---|---|---|\n"
    for org in reg.get("organizations", []):
        linked = [f"{slug} ({l.get('role')})" for slug, p in projects.items()
                  for l in (p.get("org_links") or []) if l.get("org") == org.get("id")]
        text += f"| {org.get('display_name') or org.get('id')} | {org.get('status')} | {', '.join(sorted(linked)) or '—'} |\n"
    groups_wo_org = [g.get("name") for g in reg.get("workspace_groups", []) if not g.get("org")]
    if groups_wo_org:
        text += f"\nGroups without an organization (unresolved, D7): {', '.join(groups_wo_org)}.\n"
    text += "\n## Topics\n\n| Topic | Aliases | Status |\n|---|---|---|\n"
    for t in reg.get("topics", []):
        text += f"| {t.get('id')} | {', '.join(t.get('aliases', []))} | {t.get('status')} |\n"
    if reg.get("people"):
        # I1/I7 + LUDZIE-B n1: business-card fields only, straight from the registry;
        # the row is built exclusively from the PEOPLE_ALLOWED_FIELDS whitelist, so no
        # style profiles and no mail-derived data (contacts/email/phone) can enter
        # any view.
        text += "\n## People\n\n| Person | Stanowisko | Organizations | Projects |\n|---|---|---|---|\n"
        for person in reg.get("people", []):
            pf = person_business_fields(person)
            orgs = ", ".join(pf["orgs"] or []) or "-"
            projs = ", ".join(pf["projects"] or []) or "-"
            role = pf["role"] or "-"
            text += f"| {pf['name'] or pf['id']} (`{pf['id']}`) | {role} | {orgs} | {projs} |\n"
    for fam in ordered:
        text += f"\n## {fam.get('title') or fam['id']}\n\n"
        if fam.get("description"):
            text += fam["description"] + "\n\n"
        text += ("| Project | Slug | Main page | Newest page | Current state | Folder | Aliases | Related | Organizations |\n"
                 "|---|---|---|---|---|---|---|---|---|\n")
        for slug in families[fam["id"]]:
            p = projects[slug]
            main, latest = p.get("hub_main_page"), p.get("hub_latest_page")
            newest = "same" if latest and latest == main else page_link(latest)
            text += (f"| {p.get('title') or slug} | `{slug}` | {page_link(main)} | {newest} | "
                     f"{state_link(p.get('current_state_source'))} | {code_list(p.get('folders') or [], '<br>')} | "
                     f"{code_list(p.get('aliases') or [])} | {code_list(p.get('related') or [])} | {project_org_links(p)} |\n")
    if reg.get("known_gaps"):
        text += "\n## Known gaps\n\n" + "\n".join(f"- {g}" for g in reg.get("known_gaps", [])) + "\n"
    return text


def render_where(live_where: Path) -> str:
    src = live_where.read_text(encoding="utf-8")
    # Mail row migration (fail-closed): exactly one recognized mail row must be
    # present - MAIL_ROW_OLD, the current generic MAIL_ROW_NEW, or one legacy
    # mail_direct.py row (marker-matched, no private text retained). Duplicates
    # or unknown shapes are a hard error; nothing is guessed.
    old_count = src.count(MAIL_ROW_OLD)
    new_count = src.count(MAIL_ROW_NEW)
    if new_count == 1 and old_count == 0:
        out = src
    elif old_count == 1 and new_count == 0:
        probe = src.replace(MAIL_ROW_OLD, "", 1)
        assert MAIL_ROW_LEGACY_RE.search(probe) is None, "duplicate mail row in WHERE.md"
        out = src.replace(MAIL_ROW_OLD, MAIL_ROW_NEW, 1)
    elif old_count == 0 and new_count == 0:
        legacy = MAIL_ROW_LEGACY_RE.findall(src)
        assert len(legacy) == 1, f"expected exactly one legacy mail row, found {len(legacy)}"
        out = MAIL_ROW_LEGACY_RE.sub(lambda _m: MAIL_ROW_NEW, src, count=1)
    else:
        raise AssertionError(f"ambiguous mail rows in WHERE.md: old={old_count} new={new_count}")
    rest = out.replace(MAIL_ROW_NEW, "", 1)
    assert re.search(r"(?m)^\| Mail and calendar \|", rest) is None, "unknown or duplicate mail row in WHERE.md"
    mk_rows = re.findall(r"(?m)^\| Mail knowledge & extraction \|[^\n]*\|$", out)
    assert len(mk_rows) == 1, f"expected exactly one mail-knowledge row, found {len(mk_rows)}"
    mk = re.search(r"(?m)^\| Mail knowledge & extraction \|[^\n]*\|$", out)
    out = out[:mk.start()] + MAIL_KNOWLEDGE_ROW_NEW + out[mk.end():]
    m = re.search(r"(?m)^last_verified: .*$", out)
    out = out[:m.start()] + f"last_verified: {date.today().isoformat()}" + out[m.end():]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True, type=Path)
    ap.add_argument("--live-where", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    reg = json.loads(args.registry.read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    pages = {
        "registry.md": render_registry(reg),
        "START.md": render_start(reg),
        "WHERE.md": render_where(args.live_where),
    }
    manifest = {"generator": "views_generator.py", "registry": {"path": str(args.registry), "version": reg.get("version")}}
    for name, text in pages.items():
        target = args.out / name
        target.write_text(text, encoding="utf-8", newline="\n")
        manifest[name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (args.out / "views-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({k: (v[:16] if isinstance(v, str) else v) for k, v in manifest.items()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
