#!/usr/bin/env python3
"""Deterministic pre-review checker for a curation publication candidate.

Written from the defects that six independent review rounds actually found in a
single agent-stack candidate on 2026-09-21. Eight of the ten were numbers, dates
or negative claims stated in prose - things a script settles in a second, and
which cost roughly 15.7 million contractor tokens to find by reading.

The rule this encodes: every quantity and every "there is none" claim on a
curation page must be derivable from the manifest, the ledger and the vault.
Model review is then free to do the part only judgement can do - are the
dispositions honest.

This does NOT replace the independent content review required by
references/curation-publication.md. It runs BEFORE it, so a review is never
spent on an arithmetic mistake.

Usage:
    python curation_check.py --candidate <dir> [--vault <path>]

Exit code 0 when every check passes, 2 when any finding is reported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import knowledge


# Open-blocker markers, kept identical to the ones a ledger may use to justify
# an `unresolved` disposition. Deliberately narrow: "pending" appears almost
# everywhere and does not by itself mean an unresolved matter.
# These two tiers must stay IDENTICAL to the generator's. They diverged once and
# the checker then reported an open blocker the generator had never been able to
# see. Case matters: a case-insensitive \bOPEN\b matches "OpenClaw" and flags
# every entry in the vault.
OPEN_MARKERS_CASE = (r"\bOPEN\b", r"\bBLOCKED\b", r"NOT[ _]ACCEPTED", r"NOT[ _]WIRED",
                     r"NIEDOMKNIET", r"\bOPEN_QUESTION", r"\bUNRESOLVED\b")
# Case-INSENSITIVE markers. `\bunresolved\b` and `\bblocked\b` are safe here
# because, unlike a bare `open` (which matches OpenClaw, OpenCode, open source),
# neither word is part of a product name. The case-sensitive-only version lost
# real blockers written in lower case: "Incident unresolved." (1f341d7e),
# "UI acceptance blocked", "native HR still blocked" - measured 2026-09-22 as 16
# genuine open matters across four candidates, all misfiled as historical.
OPEN_MARKERS_ANY = (r"INCIDENT OPEN", r"NOT[ _]FIXED", r"NOT[ _]REPAIRED", r"\bunresolved\b",
                    r"\bblocked\b", r"remains? broken", r"still open", r"remains? open",
                    # A COUNT of open items: "four fixed two open". Spelled-out numbers
                    # or one digit only, so "port 443 open" / "80 open" cannot match.
                    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|[1-9]) (?:still )?open\b",
                    r"not yet accepted", r"not accepted",
                    r"not wired", r"root cause (?:is )?unknown",
                    r"pozostaje otwart", r"nadal otwart", r"awaits owner", r"oczekuje na decyzj",
                    r"nie domkniet", r"nie zamkniet")
DISPOSITIONS = ("synthesized", "historical", "unresolved", "orphan")

_OPEN_CASE = re.compile("|".join(OPEN_MARKERS_CASE))
_OPEN_ANY = re.compile("|".join(OPEN_MARKERS_ANY), re.I)


def entry_detection_text(proposal_text: str, note_texts: dict) -> str:
    """The text an open-blocker detector must see for ONE entry.

    That is the proposal PLUS the raw notes it cites. Scanning the proposal alone
    is blind wherever the proposal is a thin wrapper and the substance lives in
    the note: measured 2026-09-22, 59 entries across three candidates stated an
    open matter ("Status: DIAGNOSED, NOT REPAIRED.", "nadal otwarty") only in
    their raw note and were all misfiled as closed history.

    `note_texts` maps FROZEN source ids to their original.md text. A cited note
    is found by containment of its known id, not by a pattern, so an id whose
    slug happens to contain an 8-hex segment cannot be cut short. Generator and
    checker must both call this; they drifted before whenever each built its own.
    """
    cited = sorted(sid for sid in note_texts if sid in proposal_text)
    return "\n\n".join([proposal_text] + [note_texts[sid] for sid in cited])


def open_blocker_match(text: str):
    """Single source of truth for open-blocker detection.

    A generator MUST call this rather than copy the pattern lists. The two
    definitions drifted twice on 2026-09-21: the checker then reported an open
    blocker the generator could never have seen, and later accepted four entries
    the generator had classified inconsistently. Import it; do not duplicate it.

    Returns the match object, so the caller can quote the exact phrase it hit -
    which the ledger is required to do and this checker verifies.
    """
    return _OPEN_CASE.search(text) or _OPEN_ANY.search(text)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (AttributeError, OSError, ValueError):
                pass


class Report:
    def __init__(self) -> None:
        self.findings: list[dict] = []
        self.passed: list[str] = []

    def ok(self, check: str) -> None:
        self.passed.append(check)

    def fail(self, check: str, detail: str, expected: object = None, found: object = None) -> None:
        row = {"check": check, "detail": detail}
        if expected is not None:
            row["expected"] = expected
        if found is not None:
            row["found"] = found
        self.findings.append(row)

    def verdict(self, check: str, condition: bool, detail: str,
                expected: object = None, found: object = None) -> bool:
        if condition:
            self.ok(check)
        else:
            self.fail(check, detail, expected, found)
        return condition


def load_candidate(candidate: Path) -> tuple[dict, dict, str, Path]:
    manifest = json.loads((candidate / "publication.json").read_text(encoding="utf-8"))
    ledger = json.loads((candidate / manifest["ledger_path"]).read_text(encoding="utf-8"))
    pages = [row for row in manifest["outputs"]
             if row["path"].startswith("wiki/history/")]
    if not pages:
        raise SystemExit("candidate has no wiki/history output to check")
    page_path = candidate / pages[0]["payload_path"]
    return manifest, ledger, page_path.read_text(encoding="utf-8"), Path(pages[0]["path"])


def vault_facts(vault: Path, manifest: dict) -> dict:
    """Recompute from the FROZEN inputs, never from prose."""
    sources, proposals, notes = {}, {}, {}
    for row in manifest["inputs"]:
        path = vault / row["path"]
        if path.name == "source.json":
            data = json.loads(path.read_text(encoding="utf-8"))
            sources[data["source_id"]] = data
        elif path.name == "original.md":
            notes[path.parent.name] = path.read_text(encoding="utf-8", errors="replace")
        elif path.suffix == ".md" and "proposals" in path.parts:
            text = path.read_text(encoding="utf-8", errors="replace")
            pid = knowledge.fm_value(text, "id")
            proposals[pid] = {
                "created": knowledge.fm_value(text, "created_at") or "",
                "title": knowledge.fm_value(text, "title") or "",
                "summary": knowledge.fm_value(text, "state_summary") or "",
                "text": text,
            }
    for item in proposals.values():
        item["detect_text"] = entry_detection_text(item["text"], notes)
    by_hash: dict[str, list[str]] = {}
    for sid, meta in sources.items():
        by_hash.setdefault(meta.get("sha256", ""), []).append(sid)
    created = sorted(item["created"] for item in proposals.values() if item["created"])
    return {
        "proposal_count": len(proposals),
        "source_count": len(sources),
        "span_from": created[0][:10] if created else "",
        "span_to": created[-1][:10] if created else "",
        "latest_created": created[-1] if created else "",
        "duplicate_groups": {h: v for h, v in by_hash.items() if len(v) > 1},
        "open_marker_ids": {pid for pid, item in proposals.items()
                            if open_blocker_match(item["detect_text"])},
        "proposals": proposals,
    }


def check_manifest(report: Report, manifest: dict, ledger: dict, facts: dict) -> None:
    ids, sids = manifest["proposal_ids"], manifest["source_ids"]
    report.verdict("manifest.unique_ids", len(set(ids)) == len(ids) and len(set(sids)) == len(sids),
                   "manifest repeats an id")
    report.verdict("manifest.inputs_cover_ids",
                   facts["proposal_count"] == len(ids) and facts["source_count"] == len(sids),
                   "frozen inputs do not cover exactly the declared ids",
                   {"proposals": len(ids), "sources": len(sids)},
                   {"proposals": facts["proposal_count"], "sources": facts["source_count"]})
    report.verdict("ledger.counts_match",
                   ledger.get("proposal_count") == len(ids) and ledger.get("source_count") == len(sids),
                   "ledger counts disagree with manifest ids",
                   {"proposals": len(ids), "sources": len(sids)},
                   {"proposals": ledger.get("proposal_count"), "sources": ledger.get("source_count")})
    counts = {name: sum(1 for row in ledger.get("proposals", []) if row.get("disposition") == name)
              for name in DISPOSITIONS}
    report.verdict("ledger.dispositions_sum", sum(counts.values()) == len(ids),
                   "dispositions do not sum to the number of proposals",
                   len(ids), counts)
    # A transaction that does not write a project synthesis cannot claim to have
    # carried anything INTO one. This was the first and worst defect found.
    touches_project = any(row["path"].startswith("wiki/projects/") for row in manifest["outputs"])
    report.verdict("ledger.synthesized_requires_project_page",
                   touches_project or counts["synthesized"] == 0,
                   "synthesized used although no wiki/projects page is an output",
                   0, counts["synthesized"])
    declared_unresolved = {row["proposal_id"] for row in ledger.get("proposals", [])
                           if row.get("disposition") == "unresolved"}
    missed = facts["open_marker_ids"] - declared_unresolved
    report.verdict("ledger.open_blockers_flagged", not missed,
                   "proposals report an open blocker but are not marked unresolved",
                   0, sorted(missed)[:5] + (["..."] if len(missed) > 5 else []))
    return counts


def claim_ints(page: str, pattern: str) -> list[int]:
    return [int(value) for value in re.findall(pattern, page)]


def check_page(report: Report, page: str, page_name: Path, manifest: dict,
               ledger: dict, counts: dict, facts: dict) -> None:
    entries = re.findall(r"^### (\S+ \S+) - (.*)$", page, re.M)
    report.verdict("page.entry_count", len(entries) == len(manifest["proposal_ids"]),
                   "inventory entry count differs from manifest proposal_ids",
                   len(manifest["proposal_ids"]), len(entries))
    missing = [pid for pid in manifest["proposal_ids"] if pid not in page]
    report.verdict("page.every_proposal_listed", not missing,
                   "manifest proposal ids missing from the page", 0, missing[:5])

    front = page.split("---", 2)[1] if page.startswith("---") else ""
    declared_sources = re.search(r"^sources: (\[.*\])$", front, re.M)
    if declared_sources:
        report.verdict("page.sources_match_manifest",
                       json.loads(declared_sources.group(1)) == manifest["source_ids"],
                       "frontmatter sources differ from manifest source_ids")
    else:
        report.fail("page.sources_present", "frontmatter has no sources list")

    # Content span must be the real first/last admitted entry, not the run date.
    for label, pattern in (("page.span_composition", r"- \d+ entries, (\d{4}-\d\d-\d\d) to (\d{4}-\d\d-\d\d)\."),
                           ("page.span_summary", r"reports from (\d{4}-\d\d-\d\d) to (\d{4}-\d\d-\d\d)")):
        found = re.search(pattern, page)
        if found:
            report.verdict(label, list(found.groups()) == [facts["span_from"], facts["span_to"]],
                           "declared content span is not the first/last admitted entry",
                           [facts["span_from"], facts["span_to"]], list(found.groups()))

    # The RUN date is the ledger capture, not the end of the content span.
    run_date = str(ledger.get("captured_at", ""))[:10]
    stated_run = re.search(r"(\d{4}-\d\d-\d\d) curation run", page)
    if stated_run:
        report.verdict("page.run_date", stated_run.group(1) == run_date,
                       "curation run date disagrees with ledger captured_at",
                       run_date, stated_run.group(1))
    name_date = re.search(r"(\d{8})", page_name.name)
    if name_date and run_date:
        report.verdict("page.filename_date", name_date.group(1) == run_date.replace("-", ""),
                       "page filename date disagrees with ledger captured_at",
                       run_date.replace("-", ""), name_date.group(1))

    # Every composition count must equal something computed, not a literal.
    totals = claim_ints(page, r"- (\d+) entries,")
    if totals:
        report.verdict("page.total_entries_claim", totals[0] == len(manifest["proposal_ids"]),
                       "stated entry count is wrong", len(manifest["proposal_ids"]), totals[0])
    wrappers = re.search(r"- (\d+) automated .*? wrappers .*? of which (\d+) carry", page, re.S)
    handoffs = claim_ints(page, r"- (\d+) dated state handoffs")
    if wrappers:
        declared_wrappers, declared_boiler = int(wrappers.group(1)), int(wrappers.group(2))
        real_wrappers = sum(1 for _, title in entries if title.startswith("Hub caretaker proposal"))
        report.verdict("page.wrapper_count", declared_wrappers == real_wrappers,
                       "stated wrapper count differs from the inventory",
                       real_wrappers, declared_wrappers)
        if handoffs:
            report.verdict("page.handoff_count", handoffs[0] == len(entries) - real_wrappers,
                           "handoff count does not complement the wrapper count",
                           len(entries) - real_wrappers, handoffs[0])
        # Same number restated later in prose must not drift.
        for restated in claim_ints(page, r"[Ii]n all (\d+) cases") + claim_ints(page, r"all (\d+) share"):
            report.verdict("page.wrapper_count_restated", restated == declared_wrappers,
                           "a restated wrapper count contradicts the composition bullet",
                           declared_wrappers, restated)
        report.verdict("page.boilerplate_le_wrappers", declared_boiler <= declared_wrappers,
                       "more boilerplate wrappers than wrappers")

    # Negative claims must be verified, never asserted.
    groups = facts["duplicate_groups"]
    if re.search(r"[Bb]yte-level duplicates: none|no byte-identical", page):
        report.verdict("page.duplicate_claim", not groups,
                       "page claims no byte-identical sources, but some share a sha256",
                       0, {h[:16]: v for h, v in list(groups.items())[:3]})
    stated_groups = re.search(r"Byte-identical notes: (\d+) group", page)
    if stated_groups:
        report.verdict("page.duplicate_group_count", int(stated_groups.group(1)) == len(groups),
                       "stated duplicate-group count is wrong", len(groups), int(stated_groups.group(1)))

    # Disposition table must match the ledger.
    table = re.search(r"\| synthesized \| historical \| unresolved \| orphan \|\s*\n\|[^\n]*\n\| (\d+) \| (\d+) \| (\d+) \| (\d+) \|", page)
    if table:
        stated = dict(zip(DISPOSITIONS, (int(value) for value in table.groups())))
        report.verdict("page.disposition_table", stated == counts,
                       "disposition table differs from the ledger", counts, stated)

    # A count of live vault state must carry the moment it was taken, and that
    # moment cannot predate the things it counts.
    measured = re.search(r"Measured at (\S+?),\s*\n?\s*AT LEAST (\d+)", page)
    if re.search(r"arrived after (?:that|the) cut", page):
        if not report.verdict("page.delta_is_timestamped", bool(measured),
                              "a live-state count is stated without its measurement time"):
            return
        stamp = measured.group(1).rstrip(".,")
        report.verdict("page.delta_measured_after_content",
                       stamp >= facts["latest_created"],
                       "delta measurement predates the newest admitted entry",
                       facts["latest_created"], stamp)


def check_no_foreign_ids(report: Report, page: str, manifest: dict) -> None:
    """A page may only name ids it actually froze.

    Hardcoded prose about one project's duplicate group leaked verbatim into
    three other projects' pages on 2026-09-21, asserting "0 group" and then
    describing three proposals that were not in those manifests at all. The
    count matched, so a count-only check passed it.
    """
    known = set(manifest["proposal_ids"]) | set(manifest["source_ids"])
    short = {value.rsplit("-", 1)[-1] for value in known} | {value[-8:] for value in known}
    foreign = set()
    for match in re.findall(r"\b(?:prop|src)-\d{8}T\d{9}Z-[0-9a-z-]+", page):
        # Inventory links carry the FILENAME (id plus title slug) and source
        # references are rendered truncated, so a legitimate mention may be
        # either longer or shorter than the id. Match in both directions.
        if not any(match.startswith(value) or value.startswith(match) for value in known):
            foreign.add(match)
    # Short ids such as `aebb084d` are how the prose usually refers to entries.
    for match in re.findall(r"`([0-9a-f]{8})`", page):
        if match not in short:
            foreign.add(match)
    report.verdict("page.no_foreign_ids", not foreign,
                   "page names identifiers that are not frozen in this manifest",
                   0, sorted(foreign)[:6])


def check_unresolved_quotes(report: Report, ledger: dict, facts: dict) -> None:
    """`unresolved` must be backed by words actually present in the entry.

    Keyword detection cannot prove that an entry reports an open blocker, so the
    ledger has to quote the phrase it matched, and the quote has to be real.
    """
    unverifiable = []
    for row in ledger.get("proposals", []):
        if row.get("disposition") != "unresolved":
            continue
        quoted = re.search(r'quoted: "([^"]+)"', str(row.get("reason", "")))
        source = facts["proposals"].get(row["proposal_id"], {}).get("detect_text", "")
        # Compared modulo whitespace: a note wraps "list\nalso pending" across lines,
        # and a quote on a one-line Disposition entry must not break the page.
        if not quoted or " ".join(quoted.group(1).split()) not in " ".join(source.split()):
            unverifiable.append(row["proposal_id"])
    report.verdict("ledger.unresolved_quotes_verifiable", not unverifiable,
                   "unresolved rows without a quote that appears in the frozen proposal "
                   "or in a frozen note it cites",
                   0, unverifiable[:5])


def check_no_completeness_claim(report: Report, page: str) -> None:
    """Pattern detection may never be presented as proof that nothing is open."""
    has_disclaimer = "not a proof of completeness" in page or 'never as "none"' in page
    report.verdict("page.unresolved_completeness_disclaimed", has_disclaimer,
                   "page states an unresolved count without disclaiming completeness")


def main(argv: list[str] | None = None) -> int:
    force_utf8()
    parser = argparse.ArgumentParser(description="Deterministic curation candidate checker.")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--vault")
    args = parser.parse_args(argv)

    candidate = Path(args.candidate)
    manifest, ledger, page, page_name = load_candidate(candidate)
    vault = Path(args.vault) if args.vault else Path(manifest["vault_path"])

    report = Report()
    report.verdict("manifest.sha_in_review_is_current", True, "")  # placeholder, replaced below
    report.passed.pop()

    facts = vault_facts(vault, manifest)
    counts = check_manifest(report, manifest, ledger, facts)
    check_page(report, page, page_name, manifest, ledger, counts, facts)
    check_no_foreign_ids(report, page, manifest)
    check_unresolved_quotes(report, ledger, facts)
    check_no_completeness_claim(report, page)

    # Manifest summary is prose too, and carried a stale count for two rounds.
    stated = re.search(r"the (\d+) pending", str(manifest.get("summary", "")))
    if stated:
        report.verdict("manifest.summary_count",
                       int(stated.group(1)) == len(manifest["proposal_ids"]),
                       "manifest summary states a stale count",
                       len(manifest["proposal_ids"]), int(stated.group(1)))

    result = {
        "action": "curation-check",
        "candidate": str(candidate),
        "manifest_sha256": sha((candidate / "publication.json").read_bytes()),
        "checks_passed": len(report.passed),
        "finding_count": len(report.findings),
        "status": "PASS" if not report.findings else "FAIL",
        "findings": report.findings,
        "note": "Deterministic pre-check only. It does not replace the independent content review.",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not report.findings else 2


if __name__ == "__main__":
    raise SystemExit(main())
