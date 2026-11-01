# Attack policy - simulate-attack

## Twarde deny (zawsze odrzucane)

Kazda iteracja jest ograniczona trzema warstwami wykonywalnej polityki:
profil Fleet nie udostepnia narzedzi mutujacych ani sieci publicznej,
`validate_probe()` dopuszcza tylko zamknieta liste strukturalnych prob, a pola
wykonawcze (`url`, `host`, `endpoint`, `action`, `operation`, `command`) sa
sprawdzane przez `DENY_ACTIONS` i `DENY_NETWORK_HOSTS` przed przyjeciem
proby. Ktorykolwiek z nastepujacych warunkow konczy przebieg stanem
`BLOCKED` i kodem wyjscia `4` bez zapisu raportu:

- kasowanie plikow lub katalogow (`os.remove`, `shutil.rmtree`, `unlink`),
- podtrzymywany DoS, flood, wielosekundowe obciazenie,
- realna mutacja danych (CRUD poza read-only, INSERT/UPDATE/DELETE w bazie,
  zapis poza katalogiem raportu),
- wysylka maila (SMTP, Graph, SendGrid, Mailgun, SES),
- inicjacja platnosci (Stripe, PayU, Przelewy24, Billingo API),
- zapis do CRM (HubSpot, Pipedrive, Salesforce, dowolny custom CRM),
- zapis do Billingo poza trybem read-only,
- zapis do kalendarza (Google, Microsoft, CalDAV),
- publiczne integracje (webhook, publiczny tunel, publiczny post).

Helper nie przyjmuje dowolnych polecen `subprocess`, `urllib`, `requests`,
`httpx` ani `smtplib`. Narracja ustalenia nie jest traktowana jak polecenie;
kontrola deny obejmuje wylacznie strukturalny payload proponowanej proby,
zanim jakikolwiek executor moglby go wykorzystac.

## Aktywne klasy celu

| Klasa            | Opis                                                  | Dozwolone |
| ---------------- | ----------------------------------------------------- | --------- |
| `TEST`           | Cialo testowe, dane syntetyczne, bez realnych userow. | tak       |
| `STAGING`        | Kopia staging na allowliscie Mateusza.                | tak       |
| `isolated_candidate` | Wydzielony kandydat release, brak polaczen z PROD. | tak       |
| `PROD`           | Zywa produkcja lub dane realnych klientow.            | nigdy     |

`--target-class` nie jest dowodem klasy. `TEST` musi rozwiazac sie pod
`~/.agents/security-audits/synthetic-targets`. `STAGING` i
`isolated_candidate` wymagaja zgodnego wpisu w lokalnym
`~/.agents/security-audits/target-registry.json` z
`synthetic_data=true` i `active_attack_allowed=true`. Symlink lub Windows
reparse point w sciezce celu konczy sie odmowa. Slowo kluczowe `prod` w
`--target-name` albo `--live-customer-data` nadal wymusza odmowe.

## Wyzwalacze

Skill nie wprowadza drugiego licznika swiezosci. Korzysta z jedynego
14-dniowego ledgera `security-audit-cycle`. Wyzwalacz
`--trigger` moze przyjac:

- `next_substantive_work` - eskalacja przy audycie starszym niz 14 dni
  przy nastepnej istotnej pracy.
- `pre_production_release_candidate` - eskalacja przed duzym wydaniem
  produkcyjnym, wykonywana na izolowanym kandydacie.

`preflight` czyta istniejacy ledger `security_audit.py` przez thin import
`status_payload` (bez drugiego licznika i bez lokalnego probe) i weryfikuje,
czy `adversarial_triggers[<trigger>]` jest spojne z `--trigger`. Rozbieznosc
albo niedostepnosc ledgera konczy sie `BLOCKED`.

## Budzet

`max_iterations` i `max_fresh_tokens` musza byc dodatnimi liczbami
calkowitymi. Brak wartosci, wartosc `0`, ujemna lub nieliczbowa oznacza
`BUDGET_EXHAUSTED`. Helper nie moze sam odblokowac budzetu.

Wykonanie pojedynczej iteracji konsumuje `1` z `max_iterations`.
Przekroczenie limitu konczy sie `BUDGET_EXHAUSTED` po zamknieciu
biezacej iteracji.

## Redakcja

Kazde pole wykryte jako `redact_target` jest zamieniane na
`<redacted:TYPE@RELATIVE_PATH>`. Typy obejmuja:

- `aws-access-key`, `openai-key`, `github-token`, `huggingface-token`,
- `slack-token`, `private-key`, `credential-uri`, `generic-api-key`,
- `password`, `jwt`, `cookie`, `email-address`, `phone-number`.

Sekwencja `<redacted:...>` jest deterministyczna; ten sam wejsciowy
dokument daje ten sam zredagowany dokument.

## Raport

Kazdy przebieg zapisuje:

```text
%USERPROFILE%\.agents\security-audits\<project-id>\<run-id>\
  metadata.json
  recon.json
  iterations.jsonl
  remediation.md
  REPORT.md
```

Kontener `<project-id>` jest wspoldzielony z `security-audit-cycle`,
ale wynik adversarial ma osobne `adversarial-latest.json` i
`adversarial-ledger.jsonl`. Bazowe `latest.json` i `ledger.jsonl` pozostaja
niezmienione, dlatego audyt adversarial nie moze sam odswiezyc 14-dniowego
baseline.
Kontener `<run-id>` jest `YYYYMMDDTHHMMSSfffZ__<agent>__redteam`.

Raport zostaje usuniety wylacznie na polecenie Mateusza.
