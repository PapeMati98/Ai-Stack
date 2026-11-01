# Evidence Ledgers

The campaign has exactly three canonical ledgers: `sources.json`,
`claims.json` and `numbers.json`. `campaign.json.ledgers` must name those exact
files. Aliases and alternate paths are rejected so the files validated during
drafting are the same files bound by the campaign seal and final acceptance.
All ledger `schema_version` values are strict JSON integers; boolean `true` is
not accepted as version `1`.

## Source Ledger

Use `sources.json`. Evidence references are relative files inside the campaign
directory and must match their SHA-256. A source becomes usable only with
`verification_status: verified`. Time-sensitive evidence has `expires_on`.

## Claim Ledger

Use `claims.json`. Keep these kinds distinct: `fact`, `founder_statement`,
`assumption`, `projection`, `scenario`, `unknown`. A verified non-unknown claim
must reference at least one current verified source. `unknown` is allowed as an
intake state but blocks dependent drafting.

Every claim carries a valid classification and cannot exceed the campaign
`data_classification`, just like its supporting source.

## Number Ledger

Use `numbers.json`. An input number references verified sources. A derived
number has a small arithmetic formula and an explicit variable-to-number map.
The helper evaluates Decimal arithmetic only. Record unit, period, optional
currency, tolerance and every artifact display value. All displays reconcile to
the canonical value within tolerance.

## Provider Boundary

`campaign.json` names allowed and selected providers. A source may narrow that
list. D2/D3 classification, applicable owner decisions and provider boundaries
are recorded in the existing TODO and campaign/evidence inputs, enforced by the
helper and bound by the existing campaign seal. No separate governance contract
or seal is required. `governance_contract_hash` may remain as optional historical
metadata; it never waives classification, owner consent or provider restrictions.
Absence of a provider decision blocks delegation; it never authorizes all providers.
