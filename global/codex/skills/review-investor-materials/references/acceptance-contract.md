# Acceptance Contract

## Binding

The candidate receipt is bound to a current campaign seal and exact artifact
hashes. Content gates bind to editable-source hashes. Visual and export gates
bind to PDF hashes. Confidentiality binds to the SHA-256 of both hashes joined
as `<editable>:<pdf>`.

The campaign seal binds `campaign.json` and the three exact canonical ledgers:
`sources.json`, `claims.json` and `numbers.json`. The acceptance helper also
pins the SHA-256 of the sibling deterministic campaign validator before loading
it. A ledger alias, post-seal mutation, validator substitution or boolean
schema version invalidates the candidate.

Each gate includes an evidence file and SHA-256. The reviewer must differ from
the responsible maker. A changed artifact or evidence file invalidates the
candidate; previous review prose cannot be reused without a fresh binding.

## Mandatory Gates

Exactly one current PASS is required for each gate:

- `factual`
- `financial`
- `narrative`
- `visual`
- `export`
- `confidentiality`

There is no weighted score and no waiver path for a missing gate. Open P0/P1
findings block acceptance. P2/P3 remain visible in the final receipt and should
be resolved when they conflict with a gate's PASS.

## Artifact Contract

The candidate artifact set must exactly match the types requested by
`campaign.json`. Every item has a unique id, editable source, PDF, hashes and
positive PDF page count measured from the current PDF bytes through `pypdf`.
`one-pager` requires measured page count 1.

## Authority

The final `ACCEPTED` receipt means ready for owner review. It never authorizes
investor contact, upload, email, publication or other distribution.
