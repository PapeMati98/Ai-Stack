"""I3 navigation API v1 for the Knowledge Hub (owner: ZCode, program contract I3).

Deterministic, stdlib-only, read-only. No second index, no vector store, no
crawler: facets come from the registry v2 file, page metadata from one pass
over vault/wiki frontmatter, and topic over-project reach from the activated
topic feeds (KSEF-SCOUT-CONTRACT format). Standalone CLI today; the gateway
dispatcher entry is added by Codex KH after the interface decision.

Ordering: the project's main card (wiki/projects/<slug>.md) comes first, then
the system map view, then the topic-feed and history stream. EMPTY results are
never padded with the map pointer.

Semantics: EMPTY means the search ran and found nothing; UNAVAILABLE means an
axis or feed cannot answer at all (registry missing, people not yet in the
registry, feed not activated). A gap is never proof of absence of history."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

HUB_ROOT = Path(__file__).resolve().parents[3]
VAULT = HUB_ROOT / "vault"
REGISTRY_LIVE = HUB_ROOT / "registry" / "registry-v2.json"
REGISTRY_SEED = HUB_ROOT.parent.parent / "docs" / "system-projektow-20261005" / "contracts" / "I1-registry-v2.frozen-v0.json"


def _topic_feed_path():
    selected = os.environ.get("KNOWLEDGE_TOPIC_FEEDS")
    if selected is None:
        return HUB_ROOT / "registry" / "topic-feeds.json"
    path = Path(selected)
    if not selected.strip() or not path.is_absolute() or not path.is_file():
        raise ValueError("KNOWLEDGE_TOPIC_FEEDS must name an absolute existing regular file")
    if any(p.is_symlink() or (getattr(p.lstat(), "st_file_attributes", 0) & 0x400)
           for p in (path, *path.parents)):
        raise ValueError("KNOWLEDGE_TOPIC_FEEDS cannot contain filesystem links")
    return path.resolve(strict=True)


TOPIC_FEEDS = _topic_feed_path()
TRUSTED_STATUS = {"reviewed", "verified"}
SYSTEM_MAP_REF = "wiki/START.md"  # generated view: "Start here: project map"


def fm_scalar(content: str, field: str):
    m = re.match(r"(?s)^---\r?\n(.*?)\r?\n---", content)
    if not m:
        return None
    quoted = re.search(rf"(?m)^{re.escape(field)}:\s*\"(.*)\"\s*$", m.group(1))
    plain = None if quoted else re.search(rf"(?m)^{re.escape(field)}:\s*([^\"\r\n].*)$", m.group(1))
    hit = quoted or plain
    if not hit:
        return None
    return hit.group(1).strip()


def load_registry():
    for path, origin in ((REGISTRY_LIVE, "registry-v2"), (REGISTRY_SEED, "seed-frozen-v0")):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")), origin, None
    return None, None, "registry_unavailable"


def registry_facets(reg: dict, topic=None, org=None, project=None, person=None):
    """Resolve facet values (incl. aliases) to concrete slug sets; None = no filter."""
    warnings = []
    projects_filter = None
    if project:
        p = reg.get("projects", {}).get(project)
        if p is None:
            alias_hit = next((s for s, v in reg.get("projects", {}).items()
                              if project in (v.get("aliases") or [])), None)
            if alias_hit is None:
                return None, [f"unknown_project:{project}"]
            project = alias_hit
        projects_filter = {project}
    if org:
        orgs = {o.get("id") for o in reg.get("organizations", [])}
        if org not in orgs:
            return None, [f"unknown_org:{org}"]
        linked = {slug for slug, p in reg.get("projects", {}).items()
                  for l in (p.get("org_links") or []) if l.get("org") == org}
        projects_filter = linked if projects_filter is None else (projects_filter & linked)
    topic_ids = []
    if topic:
        for t in reg.get("topics", []):
            names = {t.get("id"), t.get("name"), *(t.get("aliases") or [])}
            if topic in names:
                topic_ids.append(t.get("id"))
                break
        else:
            topic_ids = [topic]  # free-form topic: keyword path only, still valid
    return {"topic_ids": topic_ids, "projects_filter": projects_filter}, warnings


def topic_feed_records(topic_ids):
    if not topic_ids or not TOPIC_FEEDS.exists():
        return [], ("feed_not_activated" if topic_ids else None)
    feed = json.loads(TOPIC_FEEDS.read_text(encoding="utf-8"))
    if not any(tid.casefold() == str(feed.get("topic", "")).casefold() for tid in topic_ids):
        return [], "feed_topic_mismatch"
    out = []
    for rec in feed.get("curated_records", []):
        observed = rec.get("observed_at")
        basis = "feed"
        if not observed:
            # I3: fill observed_at for feed records without touching the signature;
            # the page's last_verified is the closest dated observation we have.
            page = VAULT / str(rec.get("source_ref") or "").removeprefix("wiki/")
            if page.is_file():
                text = page.read_text("utf-8", errors="replace")
                m = re.match(r"(?s)^---\r?\n(.*?)\r?\n---", text)
                hit = re.search(r"(?m)^last_verified:\s*([^\"\r\n]+)$", m.group(1)) if m else None
                if hit and not str(hit.group(1)).strip().casefold() == "unverified":
                    observed = hit.group(1).strip()
                    basis = "page_last_verified"
        if not observed:
            for sid in rec.get("source_ids") or []:
                m2 = re.match(r"src-(\d{4})(\d{2})(\d{2})T\d{9}Z-", str(sid))
                if m2:
                    observed = "-".join(m2.groups())
                    basis = "source_id_date"
                    break
        out.append({
            "page_ref": rec.get("source_ref"), "project_slug": rec.get("project_slug"),
            "title": rec.get("title"), "org": None, "topics": [feed.get("topic")],
            "status": rec.get("status"), "trust": rec.get("trust"),
            "observed_at": observed,
            "summary": (rec.get("verification_caveat") or "")[:300],
            "source": "topic_feed", "source_sha256": rec.get("source_sha256"),
            "transfer_caveat": rec.get("transfer_caveat"),
        })
        if observed:
            out[-1]["observed_at_basis"] = basis
    return out, None


def wiki_records(vault: Path, topic_ids, projects_filter, keywords):
    records = []
    for page in sorted((vault / "wiki").rglob("*.md")):
        try:
            content = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        slug = fm_scalar(content, "project_slug")
        if projects_filter is not None and slug not in projects_filter:
            continue
        title = fm_scalar(content, "title")
        summary = fm_scalar(content, "summary") or ""
        blob = " ".join(filter(None, [title, summary])).casefold()
        if topic_ids and not any(k.casefold() in blob for k in keywords):
            continue
        if topic_ids and slug is None:
            continue  # topic axis rides on project metadata; pageless history stays out of v1
        rel = "wiki/" + page.relative_to(vault / "wiki").as_posix()
        records.append({
            "page_ref": rel, "project_slug": slug, "title": title,
            "org": None, "topics": topic_ids or [],
            "status": fm_scalar(content, "status"),
            "trust": "trusted" if fm_scalar(content, "status") in TRUSTED_STATUS else "untrusted",
            "observed_at": fm_scalar(content, "last_verified"),
            "summary": summary[:300], "source": "wiki",
        })
    return records


def system_map_record():
    """One record for the generated project-map view; None when the view is absent."""
    page = VAULT / "wiki" / "START.md"
    if not page.is_file():
        return None
    content = page.read_text(encoding="utf-8", errors="replace")
    return {
        "page_ref": SYSTEM_MAP_REF, "project_slug": None,
        "title": fm_scalar(content, "title"),
        "org": None, "topics": [],
        "status": fm_scalar(content, "status"),
        "trust": "trusted" if fm_scalar(content, "status") in TRUSTED_STATUS else "untrusted",
        "observed_at": fm_scalar(content, "last_verified"),
        "summary": (fm_scalar(content, "summary") or "")[:300],
        "source": "wiki", "system_map": True,
    }


def nav_rank(record):
    """I3 ordering: the project's main card first, then the system map, then the rest.

    A main card is the page whose basename equals its project slug; continuation
    and backfill pages carry suffixes and stay in the history stream.
    """
    ref = record.get("page_ref") or ""
    if ref == SYSTEM_MAP_REF:
        return 1
    if ref.startswith("wiki/projects/"):
        base = ref.rsplit("/", 1)[-1]
        slug = record.get("project_slug")
        if slug and base == f"{slug}.md":
            return 0
    return 2


def keywords_for(topic_ids, reg):
    kws = set()
    for tid in topic_ids:
        t = next((x for x in reg.get("topics", []) if x.get("id") == tid), None)
        if t:
            kws.update({t.get("id"), t.get("name"), *(t.get("aliases") or [])})
        kws.add(tid)
    return {k for k in kws if k}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="knowledge.py navigate")
    ap.add_argument("--topic")
    ap.add_argument("--org")
    ap.add_argument("--project")
    ap.add_argument("--person")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = ap.parse_args(argv)
    if not any((args.topic, args.org, args.project, args.person)):
        print(json.dumps({"status": "ERROR", "code": "no_facet"}))
        return 1
    unavailable, warnings = [], []
    reg, reg_origin, reg_error = load_registry()
    if reg is None:
        unavailable.append(f"registry:{reg_error}")
    if args.person:
        unavailable.append("axis:people_not_in_registry_v0")  # I1 seed has no people yet

    facets, facet_warnings = ({}, []) if reg is None else registry_facets(
        reg, topic=args.topic, org=args.org, project=args.project, person=args.person)
    warnings += facet_warnings
    if facets is None:
        print(json.dumps({"status": "ERROR", "code": "unknown_facet_value",
                          "warnings": warnings}, ensure_ascii=False))
        return 1

    records = []
    if reg is not None and not unavailable:
        kws = keywords_for(facets.get("topic_ids") or [], reg)
        feed, feed_note = topic_feed_records(facets.get("topic_ids"))
        if feed_note:
            # a feed that does not cover this topic degrades recall, it does not
            # disable the axis: the wiki scan still ran (I3: EMPTY stays honest)
            warnings.append(f"topic_feed:{feed_note}")
        records = feed + wiki_records(VAULT, facets.get("topic_ids"),
                                      facets.get("projects_filter"), kws or set())
        org_of = {}
        for slug_, p_ in (reg.get("projects") or {}).items():
            links = [l.get("org") for l in (p_.get("org_links") or []) if l.get("org")]
            if links:
                org_of[slug_] = links
        for r in records:
            if r.get("project_slug") in org_of:
                r["org"] = org_of[r["project_slug"]]
        if records and not any(r.get("page_ref") == SYSTEM_MAP_REF for r in records):
            # I3 ordering fix: surface the system map as a visible pointer, but
            # only when the search itself found something - EMPTY must stay EMPTY.
            map_rec = system_map_record()
            if map_rec is not None:
                records.append(map_rec)
        records.sort(key=lambda r: (nav_rank(r), r.get("source") != "topic_feed", r.get("page_ref") or ""))
        for r in records:
            if nav_rank(r) == 0:
                r["main_card"] = True
    total = len(records)
    window = records[args.offset:args.offset + max(1, args.limit)]
    result = {
        "status": "OK" if not unavailable else "PARTIAL",
        "query": {k: getattr(args, k) for k in ("topic", "org", "project", "person") if getattr(args, k)},
        "registry": {"origin": reg_origin, "version": (reg or {}).get("version")},
        "availability": {"unavailable": unavailable,
                         "note": "UNAVAILABLE means the axis cannot answer; EMPTY below means the search found nothing. A gap is not proof of absence."},
        "result": ("UNAVAILABLE" if total == 0 and unavailable else
                   ("EMPTY" if total == 0 else "FOUND")),
        "total": total, "limit": args.limit, "offset": args.offset,
        "has_more": args.offset + args.limit < total,
        "records": window, "warnings": warnings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=1 if args.json else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
