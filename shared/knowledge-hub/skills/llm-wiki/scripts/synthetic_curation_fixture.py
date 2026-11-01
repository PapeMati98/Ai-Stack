"""Build a small, self-contained synthetic curation candidate for tests."""

import hashlib
import json
from collections import Counter
from pathlib import Path


def make_candidate(root: Path) -> Path:
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    vault = root / "vault"
    inputs = []

    def write(path: Path, text: str) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        path.write_bytes(data)
        return data

    def freeze(relative: str, text: str) -> None:
        data = write(vault / relative, text)
        inputs.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(),
                       "bytes": len(data)})

    def json_text(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2) + "\n"

    sources = [
        ("src-20260919T100000000Z-fixture-note-a1111111", "Synthetic note: INCIDENT OPEN.\n"),
        ("src-20260919T100000001Z-fixture-note-b2222222", "Synthetic note: INCIDENT OPEN.\n"),
        ("src-20260920T100000000Z-fixture-note-c3333333", "Synthetic historical note.\n"),
    ]
    for sid, body in sources:
        data = body.encode("utf-8")
        metadata = {"source_id": sid, "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data)}
        freeze(f"raw/{sid}/source.json", json_text(metadata))
        freeze(f"raw/{sid}/original.md", body)

    proposals = [
        {"id": "prop-20260919T100000000Z-a1111111", "created": "2026-09-19T10:00:00Z",
         "title": "Hub caretaker proposal: synthetic note", "sources": [sources[0][0], sources[1][0]],
         "body": "A synthetic caretaker wrapper cites its notes.", "disposition": "unresolved",
         "reason": 'Open matter quoted: "INCIDENT OPEN".'},
        {"id": "prop-20260920T100000000Z-b2222222", "created": "2026-09-20T10:00:00Z",
         "title": "Synthetic state handoff", "sources": [sources[2][0]],
         "body": "A synthetic historical handoff.", "disposition": "historical",
         "reason": "Recorded as historical."},
    ]

    def proposal_text(row: dict) -> str:
        return ("---\n" + f'id: {row["id"]}\ncreated_at: {row["created"]}\n'
                + f'title: "{row["title"]}"\nstate_summary: "{row["body"]}"\n'
                + "sources:\n" + "".join(f"  - {sid}\n" for sid in row["sources"])
                + f'---\n{row["body"]}\n')

    for row in proposals:
        freeze(f'proposals/{row["id"]}/proposal.md', proposal_text(row))

    captured = "2026-09-21T10:00:00Z"
    measured = "2026-09-21T14:00:00Z"
    later = [
        {"id": f"prop-20260921T{hour:02d}0000000Z-late000{hour}",
         "created": f"2026-09-21T{hour:02d}:00:00Z", "title": "Synthetic later handoff",
         "sources": [], "body": "A later synthetic record."}
        for hour in (11, 12, 13)
    ]
    for row in later:
        write(vault / "proposals" / row["id"] / "proposal.md", proposal_text(row))

    counts = Counter(row["disposition"] for row in proposals)
    ledger = {"captured_at": captured, "proposal_count": len(proposals),
              "source_count": len(sources),
              "proposals": [{"proposal_id": row["id"], "disposition": row["disposition"],
                             "reason": row["reason"]} for row in proposals]}
    write(root / "ledger.json", json_text(ledger))
    dates = sorted(row["created"][:10] for row in proposals)
    wrappers = sum(row["title"].startswith("Hub caretaker proposal") for row in proposals)
    groups = Counter(hashlib.sha256(body.encode("utf-8")).hexdigest() for _, body in sources)
    duplicates = sum(count > 1 for count in groups.values())
    delta = sum(captured < row["created"] <= measured for row in later)
    source_ids = [sid for sid, _ in sources]
    entries = "".join(f'### {row["created"][:10]} {row["created"][11:19]} - {row["title"]}\n'
                      f'{row["id"]}\n\n' for row in proposals)
    page = ("---\n" + f'sources: {json.dumps(source_ids)}\n---\n\n'
            + f'{captured[:10]} curation run.\n\n'
            + "## Composition\n\n"
            + f'- {len(proposals)} entries, {dates[0]} to {dates[-1]}.\n'
            + f'- {wrappers} automated Hub caretaker wrappers in total, of which {wrappers} carry a note.\n'
            + f'- {len(proposals) - wrappers} dated state handoffs.\n'
            + f'In all {wrappers} cases the wrapper cites synthetic notes.\n'
            + f'Byte-identical notes: {duplicates} group.\n\n'
            + '| synthesized | historical | unresolved | orphan |\n| --- | --- | --- | --- |\n'
            + '| ' + ' | '.join(str(counts[name]) for name in
                                ("synthesized", "historical", "unresolved", "orphan")) + ' |\n\n'
            + 'Marker detection is not a proof of completeness and must be described never as "none".\n'
            + f'Measured at {measured}, AT LEAST {delta} proposals arrived after that cut.\n\n'
            + entries)
    payload = "payload/history.md"
    write(root / payload, page)
    manifest = {"vault_path": str(vault), "ledger_path": "ledger.json", "inputs": inputs,
                "proposal_ids": [row["id"] for row in proposals], "source_ids": source_ids,
                "outputs": [{"path": f'wiki/history/curation-{captured[:10].replace("-", "")}.md',
                             "payload_path": payload}],
                "summary": f'Account for the {len(proposals)} pending synthetic handoffs.'}
    write(root / "publication.json", json_text(manifest))
    return root
