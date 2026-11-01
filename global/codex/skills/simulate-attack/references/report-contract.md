# Report contract - simulate-attack

## D2 contract binding

Raporty sa wiazane przez `data-gov-security-audit-reports-20260902`
(`docs/cyber-security-stack-2026-09-02/data-governance-contract.json`,
`hash: e8abbabc9aa815b12e9a303c3622a16a725ebddc4f7310ee5dd733e6911ed1a1`).
Pole `target_environment` moze przyjac `TEST`, `STAGING` lub
`isolated_candidate`. `PROD` jest odrzucane.

## Pola raportu (w `metadata.json`)

| Pole                 | Typ         | Opis                                                 |
| -------------------- | ----------- | ---------------------------------------------------- |
| `audit_id`           | string      | `YYYYMMDDTHHMMSSfffZ__<agent>__redteam`              |
| `started_at`         | utc-datetime| Czas rozpoczecia.                                    |
| `trigger`            | enum        | `next_substantive_work` albo `pre_production_release_candidate`. |
| `target_ref`         | redacted    | Bezpieczna etykieta celu, nie sciezka projektu.       |
| `project_path_sha256`| sha256      | Odcisk kanonicznej sciezki zamiast jej tresci.        |
| `target_attestation` | object      | Zrodlo lokalnej atestacji klasy celu i hash sciezki.  |
| `target_environment` | enum        | `TEST`, `STAGING`, `isolated_candidate`.             |
| `iteration_count`    | integer     | Faktyczna liczba iteracji.                           |
| `fresh_token_count`  | integer     | Suma `fresh_tokens` naliczonych przez execution pool.|
| `findings`           | redacted-list| Lista zredagowanych ustaleń.                         |
| `model_workspace`    | object      | Liczba/rozmiar plikow i hash usunietego snapshotu.   |
| `redaction_status`   | enum        | `verified-redacted` po skanie ostatniej mili.        |
| `external_effect_count` | integer   | Musi byc `0`.                                        |

## `remediation.md`

Lista rekomendacji w kolejnosci priorytetow. Format kazdej pozycji:

```markdown
- [SEVERITY] sciezka:linia - krotkie ustalenie.
  - Rekomendacja: ...
  - Dowod: <hash pliku>:<linia>
```

## `iterations.jsonl`

Jedna linia JSON na iteracje. Wymagane pola:

- `iteration` (int)
- `vector` (`string` z `ALLOWED_VECTORS`)
- `redacted` (`true`)
- `finding_status` (`findings` | `no_findings`)
- `findings` (zredagowana lista o scislym schemacie)
- `evidence` (niepusta lista zredagowanych referencji)

## Brak efektow zewnetrznych

Kazdy raport konczy sie `external_effect_count == 0`. Helper utrzymuje
licznik `external_attempts`; kazda proba wywolania niedozwolonej akcji
zwieksza `external_attempts` i konczy sie `BLOCKED`. Raport zapisuje
`0` w `external_effect_count` tylko wtedy, gdy nigdy nie nastapila
proba.

## Sciezka raportu

Kontener macierzysty: `%USERPROFILE%\.agents\security-audits`.
Sciezka absolutna musi spelniac `is_relative_to(USERPROFILE/.agents/security-audits)`.
Kazda proba zapisu poza kontenerem konczy sie `PATH_VIOLATION`
bez zapisu.
