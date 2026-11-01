# Kontrakt odbioru V1

Norma wykonawcza jest `scripts/acceptance.py`. Ten plik wyjasnia pola, nie
zastepuje walidatora.

## Co zamrazamy w READY

- zatwierdzony planning-contract i hash projekcji Agent Graph;
- konkretny identyfikator wykonania `run-graph-*`, przekazany przy `init`;
- srodowisko, dane, odbiorcow i polityke efektow zewnetrznych;
- maker identity;
- komplet materialnych kryteriow, ich applicability i requirement refs;
- oracle: oczekiwany wynik, metoda pomiaru i source biznesowy;
- rodzaje dowodu, maksymalny wiek i wymagany poziom niezaleznosci reviewera;
- dokladne zakresy egzekwowania side-effect safety;
- maksymalna liczbe pelnych generacji odbioru.

Kryterium `user-journey-evidence` moze opcjonalnie ustawic `surface` na
`cli` albo `graphical`; brak pola oznacza `graphical` dla kompatybilnosci.
Kryterium CLI dodatkowo zamraza `cli.argv` (liste argumentow, bez parsowania
shella) oraz `cli.expected_exit_code`. Dowod CLI uzywa `deterministic-check`,
listy `journey.command` zgodnej z argv, rzeczywistego `exit_code` i artefaktow
o nazwach `stdout.*` oraz `stderr.*`. Brak viewportu dotyczy tylko CLI.

Kod, rewizja, polityka uruchomieniowa i artefakty nie sa przepisywane do
kontraktu po kazdej naprawie. Trafiaja do append-only execution ledgeru jako
nowy binding i nowa generacja.

## Trzy oracles

Kazdy kontrakt ma co najmniej po jednym kryterium kazdego rodzaju. Rodzaj moze
byc `not-applicable` tylko z niezalezna podstawa.

1. `business-oracle`: znaczenie biznesowe, wzor, stan, uprawnienie albo kontrakt
   danych. Expected nie moze byc wynikiem wymyslonym przez wykonawce.
2. `user-journey-evidence`: dla `graphical` realna rola, route, viewport,
   action trace oraz expected/actual widoczne dla uzytkownika; dla `cli`
   zamrozona komenda, action trace, output artifacts i exit code.
3. `side-effect-safety`: dowod, co faktycznie blokuje lub ogranicza email,
   platnosc, CRM, kalendarz, upload, webhook i inna mutacje.

## Pokrycie

Unia `requirement_refs` wszystkich kryteriow musi pokrywac kazdy aktywny,
materialny requirement planu. Domeny dobiera sie do zmiany: business, backend,
data, integration, ui-functional, ux, responsive, accessibility, performance,
reliability, security i side-effect. Brak domeny jest jawnie uzasadnionym N/A,
nie cichym pominieciem.

Kazda z tych 12 domen musi wystapic w kontrakcie jako `applicable` albo jako
zaufane `not-applicable`; brak wpisu dla domeny blokuje `READY`.

Kazdy stosowalny workflow user-facing MUSI dodatkowo zamrozic trzy odrebne,
obowiazkowe kryteria `user-journey-evidence`:

- `surface-reachability`: otwierane okno, modal, drawer, menu, picker albo
  komunikat pojawia sie w aktualnym widocznym obszarze; kluczowa tresc, akcja
  podstawowa i zamkniecie sa osiagalne, a focus jest przekazany i przywracany;
- `action-feedback`: akcja daje postrzegalny stan przyjecia/pending oraz
  prawdziwy stan terminalny; operacja nieidempotentna blokuje przypadkowy
  duplikat;
- `visible-failure-recovery`: stosowalne bledy sa widoczne, zrozumiale i
  naprawialne bez falszywego sukcesu, wiecznego spinnera ani utraty kontekstu.

Nie lacz tych trzech kryteriow w ogolne `UI dziala`. Kazde ma osobny oracle i
osobny PASS/FAIL. Dokladny model dowodu opisuje
`user-perception-and-feedback.md`.

`mandatory=false` nie sluzy do obnizania wymagan. Uzywaj go tylko dla jawnego,
niematerialnego eksperymentu. Materialne wymaganie zawsze mapuje sie na co
najmniej jedno stosowalne kryterium obowiazkowe.

Trusted N/A opisuje brak stosowalnosci dodatkowej domeny lub kryterium. Nie moze
byc jedynym pokryciem aktywnego materialnego wymagania planu.

## N/A

Maker nie moze sam wpisac N/A. Legalne zrodla:

- `trusted-discovery`: niezalezny gate wykazal, ze granica nie wystepuje;
- `owner-waiver`: Mateusz jawnie zaakceptowal wykluczenie i jego zakres.

Authority zawiera actor, role i evidence ref. Brak, placeholder albo actor
tozsamy z makerem blokuje `READY`.
