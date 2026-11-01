# Review synthesis (opt-in)

Use only when a marketing-context task must summarize a large already-collected
set of redacted review records. This is not collection, scraping, publishing, or
a population-research step. Keep the existing evidence/context artifact fields
(source, date, observed customer language vs interpretation, open tests) and
attach the summary as supporting evidence.

There is no minimum review count, source count, CSV export, ranking weight, or
extra role attached to this helper. Do not add them as acceptance gates.

## Data contract

Input is a JSON list of already collected redacted review records. Each record
has:

| Field | Required | Notes |
| --- | --- | --- |
| `id` | yes | User-assigned review identity within its source. |
| `source_url` | yes | Provenance label. http(s) only. |
| `text` | yes | Already redacted review body. |
| `themes` | yes | List of user-assigned strings. No model classification. |
| `published_at` | no | ISO-like string or absent/null for unknown date. |
| `stance` | no | User-assigned label (for example `positive`, `negative`). |

Unknown dates and missing stances are retained and reported. Opposing stances
are never netted into one score.

## CLI

```text
python -B scripts/summarize_reviews.py --input <JSON list> [--output <new JSON file>]
```

- `--input` is read-only. Env/secret filenames, aliases, and reparse paths are
  refused before reading.
- `--output` is optional (stdout when omitted). Creation is atomic: an existing
  path is refused without modification, and the helper refuses to write over
  `--input`.
- No scraping, URL fetching, provider/model call, or population inference.

## Identity and conflicts

- Stable review identity is `source_url` + `id`.
- Equal text from two identities counts as two reviews. Text is never used to
  collapse reviews.
- Same identity with identical content is one review; extra rows are duplicates.
- Same identity with differing content is an unresolved conflict. All rows stay
  visible; the helper never silently selects a winner and excludes the identity
  from `unique_reviews`.

## URL provenance

`source_url` must parse as `http` or `https` with a host and without embedded
credentials. Credential-like query parameters (for example `api_key`, `token`,
`password`) reject the row; the reason never echoes values. Non-http(s) schemes
are invalid rows. A syntactically valid URL is a provenance label only; it does
not verify that the content exists or is true.

## Redaction of emitted labels

Every emitted string/key is redacted when it looks like unredacted credential
material: provenance URLs, identity `id` values, theme names, stance labels, and
conflict rows. Sensitive-looking labels become stable `<redacted:...>` or
`<redacted-url:...>` markers so distinct secrets stay distinct without leaking.

Identity and deduplication always use the original internal `source_url` and
`id`. Sanitization never merges two unrelated records. `source_distribution` is
keyed by redacted provenance (`scheme://host/path`, query/fragment/credentials
stripped); identity-level uniqueness remains in `coverage.unique_reviews`.

Secret-file aliases and reparse paths (`.env*`, `secrets`, `credentials`, and
similar names after resolve) are refused before reading. `--output` creation is
atomic: an existing path is refused without modification.

## Output

Always `sample_only: true`. Required shape:

- `coverage.total_rows`, `valid_rows`, `invalid_rows`, `duplicate_rows`,
  `unique_reviews`, plus `conflict_rows` and `conflict_identities`.
  Denominators are explicit:
  `total_rows == valid_rows + invalid_rows` and
  `valid_rows == unique_reviews + duplicate_rows + conflict_rows`.
- `source_distribution`: unique-review counts per redacted provenance label
  (`scheme://host/path`; query, fragment, and credentials stripped).
- `themes`: count and `review_ids` per user-assigned theme.
- `stance_breakdown`: counts per stance (`unspecified` when absent).
- `conflicts` and `invalid_records`: visible, redacted; never raw `text` and
  never raw sensitive provenance/identity/theme/stance labels.
- `limitations`: sample-only language, unknown dates, opposing stances,
  provenance-only URLs, unresolved conflicts, and redacted-label counts.

Validation errors and output never echo raw review text or credential-bearing
URLs or labels.

## Worked example (synthetic)

Input (`sample_reviews.json`):

```json
[
  {
    "id": "r1",
    "source_url": "https://reviews.example/item/1",
    "text": "Setup felt quick",
    "themes": ["onboarding"],
    "published_at": "2026-01-05T00:00:00Z",
    "stance": "positive"
  },
  {
    "id": "r1",
    "source_url": "https://reviews.example/item/1",
    "text": "Setup was slow",
    "themes": ["onboarding"],
    "published_at": "2026-01-05T00:00:00Z",
    "stance": "negative"
  },
  {
    "id": "r2",
    "source_url": "https://reviews.example/item/2",
    "text": "Pricing page was confusing",
    "themes": ["pricing"],
    "published_at": null,
    "stance": "negative"
  },
  {
    "id": "r3",
    "source_url": "https://blog.example/review",
    "text": "Setup felt quick",
    "themes": ["onboarding"],
    "published_at": "2026-02-01T00:00:00Z",
    "stance": "positive"
  }
]
```

Output (abridged):

```json
{
  "sample_only": true,
  "coverage": {
    "total_rows": 4,
    "valid_rows": 4,
    "invalid_rows": 0,
    "duplicate_rows": 0,
    "unique_reviews": 2,
    "conflict_rows": 2,
    "conflict_identities": 1
  },
  "source_distribution": {
    "https://blog.example/review": 1,
    "https://reviews.example/item/2": 1
  },
  "themes": {
    "onboarding": {"count": 1, "review_ids": ["r3"]},
    "pricing": {"count": 1, "review_ids": ["r2"]}
  },
  "stance_breakdown": {
    "negative": 1,
    "positive": 1
  },
  "conflicts": [
    {
      "id": "r1",
      "source_url": "https://reviews.example/item/1",
      "row_indices": [0, 1],
      "variant_count": 2,
      "reason": "same identity with differing content; unresolved and not silently selected"
    }
  ],
  "limitations": [
    "sample_only=true: counts and proportions describe only this collected redacted sample and must not be sold as population facts",
    "source_url values are provenance labels only; a syntactically valid URL does not verify review content",
    "no scraping, fetching, model classification, or population inference is performed by this helper",
    "1 unique review(s) have unknown dates (missing published_at); they are retained, not dropped, and cannot support temporal claims",
    "opposing or mixed stances present (negative, positive); all are retained in stance_breakdown and must not be netted into a single sentiment score",
    "1 identity conflict(s) remain unresolved and are excluded from unique_reviews"
  ]
}
```

### Inference limits of this example

- Equal text is counted independently only for identities that are
  internally consistent. "Setup felt quick" appears in the input under conflicted `r1`
  and under consistent `r3`, but only `r3` enters the summary (onboarding
  count 1, `review_ids` `["r3"]`); the phrase is counted once here. Across
  distinct consistent identities, equal text still never collapses and is not
  evidence of one repeated author.
- `r1` is an unresolved conflict. Neither variant is treated as the true text.
- `r2` has an unknown date. It cannot support any trend or recency claim.
- Negative and positive stances both appear. The 50/50 split is sample-only and
  must not be described as market sentiment.
- `source_distribution` counts collected unique reviews per provenance label,
  not traffic, reach, or population share.
- Emitted labels are redacted when they look sensitive. Identity and dedup used
  original internal values, so sanitization never merges two unrelated records.
