#!/usr/bin/env python3
"""
Bramka specyfikacji zlecenia dla kontraktorow Agent Fleet.

PO CO TO ISTNIEJE (dowod, nie przeczucie):
Audyt 264 zadan delegowanych z Codex Desktop (2026-08-02) wykazal, ze 56% niosло
rework, a jakosc DOWOZU byla wysoka. Zawiodl KONTRAKT ZLECENIA:
  - 58% promptow ponizej 900 znakow, mediana ~800; brak kryteriow akceptacji,
  - 73% promptow to ten sam wklejony szablon ze zmieniona nazwa zadania,
  - 100% dzieci startowalo z BLEDNYM katalogiem roboczym (jeden run spalil
    9,07 mln tokenow i nie zapisal ani jednej zmiany).
Ta bramka ODMAWIA wyslania zlecenia, ktore powtarza ktorykolwiek z tych bledow.

PROFILE KONTRAKTOROW (ocena wlasciciela, 2026-08-05):
  wyrobnik  - grok, antigravity/gemini. NIE liczymy na kreatywnosc.
              Dostaja ponumerowana instrukcje krok po kroku, dokladne komendy,
              jawna procedure testowania i zakaz improwizacji.
              WYMAGANE dodatkowo pole `kroki`.
  inzynier  - claude, codex, kimi. Dostaja cel + ograniczenia + kryterium
              akceptacji; wolno im dobrac droge.
  posredni  - qwen. Merytorycznie mocny (okno 1 mln, wysokie wyniki), ale
              bezobslugowo bez powloki, wiec nie zweryfikuje wlasnej pracy.
              W advisory/review dostaje brief inzynierski, w reszcie procedure.

Profil wyznacza funkcja `profil(contractor, tryb)` — zalezy od OBU, nie tylko
od nazwy kontraktora.

Zero zaleznosci zewnetrznych. Nie wysyla niczego sam - renderuje prompt,
ktory nastepnie idzie do `fleet.py run --task-file`.

Uzycie:
  python spec_gate.py --spec zadanie.json --contractor grok
  python spec_gate.py --spec zadanie.json --contractor codex --out prompt.txt
  python spec_gate.py --template > zadanie.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WYROBNIK = {"grok", "antigravity", "gemini"}
INZYNIER = {"claude", "codex", "kimi"}
# Qwen jest posrodku: merytorycznie mocny (okno 1 mln, wysokie wyniki w benchmarkach),
# ale bezobslugowo nie ma powloki, wiec nie zweryfikuje wlasnej pracy.
# Wniosek (ocena wlasciciela 2026-08-05): w advisory/review traktuj go jak INZYNIERA
# — cel + ograniczenia, wolno mu myslec. W zadaniach mechanicznych jak WYROBNIKA.
POSREDNI = {"qwen", "deepseek", "deepseek-pro", "glm"}


def profil(contractor: str, tryb: str) -> str:
    """Zwraca 'wyrobnik' albo 'inzynier' dla danej pary kontraktor/tryb."""
    if contractor in POSREDNI:
        return "inzynier" if tryb in ("advisory", "review") else "wyrobnik"
    return "wyrobnik" if contractor in WYROBNIK else "inzynier"

# ZDOLNOSCI ADAPTERA W TRYBIE BEZOBSLUGOWYM — zmierzone, nie deklarowane.
# Powod istnienia: 2026-08-05 zlecono Qwenowi zadanie `implement`; wykonal poprawnie
# krok 1, po czym zatrzymal sie zgodnie z procedura, bo w trybie headless NIE MA
# narzedzia powloki. Kontrakt byl bez zarzutu, dobor kontraktora bledny.
# Bramka musi lapac niedopasowanie TRYBU do ZDOLNOSCI, nie tylko braki w specyfikacji.
#
# Dowod dla qwen (qwen-code 0.20.0, --approval-mode auto, wejscie przez stdin):
#   polecenie "uruchom echo" -> odpowiedz doslownie "BRAK_POWLOKI".
ZDOLNOSCI = {
    "qwen":        {"tryby": {"advisory", "review"},              "dowod": "headless bez powloki (zmierzone 2026-08-05)"},
    "grok":        {"tryby": {"advisory", "review"},              "dowod": "runy ucinane, 0% first-pass w rejestrze Fleeta; podnies timeout przed implement"},
    # Decyzja wlasciciela 2026-08-10: Gemini zawodzi jakosciowo na tyle, ze zostaje
    # WYLACZNIE do audytow read-only. Tryb `implement` odebrany mimo tego, ze agy
    # technicznie potrafi pisac (--mode accept-edits). Podstawa: jakosc 38,5,
    # first-pass 9%, udokumentowany incomplete-security-review, trzykrotne
    # nadpisanie cudzych plikow i wprowadzanie nieistniejacych flag.
    "antigravity": {"tryby": {"advisory", "review"}, "dowod": "tylko audyty read-only (decyzja 2026-08-10); ZAKAZ audytow bezpieczenstwa i ZAKAZ zapisu"},
    "gemini":      {"tryby": {"advisory", "review"},              "dowod": "brak zweryfikowanego CLI z prawem zapisu"},
    "codex":       {"tryby": {"advisory", "review", "implement"}, "dowod": "codex exec --sandbox workspace-write"},
    "claude":      {"tryby": {"advisory", "review", "implement"}, "dowod": "pelny dostep do narzedzi"},
    "kimi":        {"tryby": {"advisory", "review", "implement"}, "dowod": "kimi -p; limit wyczerpany do 2026-08-22"},
    # Modele dostepne przez TEN SAM klucz Bailian co Qwen (endpoint token-plan.ap-southeast-1).
    # Wywolanie: qwen -m <id>. Dziedzicza ograniczenie uprzezy qwen — brak powloki headless,
    # wiec advisory/review, nigdy implement. Zweryfikowane wywolaniem 2026-08-05 (kazdy zwrocil "OK").
    "deepseek":     {"tryby": {"advisory", "review"}, "dowod": "przez `qwen -m deepseek-v4-flash-0731`; uprzaz qwen bez powloki"},
    "deepseek-pro": {"tryby": {"advisory", "review"}, "dowod": "przez `qwen -m deepseek-v4-pro`; uprzaz qwen bez powloki"},
    "glm":          {"tryby": {"advisory", "review"}, "dowod": "przez `qwen -m glm-5.2`; uprzaz qwen bez powloki"},
}

# Modele osiagalne przez klucz Bailian — jedna subskrypcja, jeden endpoint.
# Dzieki temu benchmarki miedzy nimi sa bezposrednio porownywalne (identyczna uprzaz).
BAILIAN_MODELE = {
    "qwen": "qwen3.8-max",
    "deepseek": "deepseek-v4-flash-0731",
    "deepseek-pro": "deepseek-v4-pro",
    "glm": "glm-5.2",
}

# Progi wyprowadzone z audytu: mediana nieudanego promptu to ~800 znakow,
# a udane zlecenia (pelny kontrakt) mialy 1874-2642 znakow.
MIN_PROMPT_CHARS = 900
MIN_CEL_CHARS = 30

POLA = [
    ("cel", "1 zdanie: czasownik + rezultat mierzalny"),
    ("katalog_roboczy", "PELNA sciezka bezwzgledna kanonicznego checkoutu"),
    ("zakres", "lista zamknieta plikow/katalogow (sciezki bezwzgledne)"),
    ("zakazy", "czego NIE wolno (commit, push, merge, deploy, instalacja, siec)"),
    ("kontekst", "5-15 linii: stack, konwencje, gdzie lezy profil aplikacji"),
    ("akceptacja", "konkretna komenda => oczekiwany wynik"),
    ("dowod", "jakie artefakty musza wrocic (diff, log, cytaty plik:linia)"),
    ("wyjscie", "dokladny format handoffu"),
    ("budzet", "max minut, max plikow, poziom reasoning"),
    ("tryb", "advisory | review | implement (+ worktree tak/nie)"),
]

TEMPLATE = {
    "cel": "",
    "katalog_roboczy": "",
    "zakres": [],
    "zakazy": ["bez commit", "bez push", "bez merge", "bez deploy", "bez instalacji pakietow"],
    "kontekst": "",
    "akceptacja": [{"komenda": "", "oczekiwany_wynik": ""}],
    "dowod": ["diff", "log komendy akceptacyjnej", "cytaty plik:linia"],
    "wyjscie": "sekcje SUMMARY / FINDINGS / EVIDENCE / RISKS",
    "budzet": {"minuty": 20, "max_plikow": 5, "reasoning": "medium"},
    "tryb": "review",
    "kroki": [],
}


def _pusty(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, (list, dict)):
        return len(v) == 0
    return False


def waliduj(spec: dict, contractor: str) -> list[str]:
    bledy: list[str] = []

    for nazwa, opis in POLA:
        if _pusty(spec.get(nazwa)):
            bledy.append(f"BRAK POLA `{nazwa}` — {opis}")

    kat = (spec.get("katalog_roboczy") or "").strip()
    if kat and not re.match(r"^([a-zA-Z]:[\\/]|/|\\\\)", kat):
        bledy.append(
            f"`katalog_roboczy` musi byc sciezka BEZWZGLEDNA, jest: {kat!r}. "
            "To przyczyna nr 1 reworku — 100% dzieci w audycie startowalo ze zlego katalogu."
        )
    if kat and not Path(kat).is_dir():
        bledy.append(f"`katalog_roboczy` nie istnieje na dysku: {kat}")

    for p in spec.get("zakres") or []:
        if not re.match(r"^([a-zA-Z]:[\\/]|/|\\\\)", str(p)):
            bledy.append(f"`zakres` musi zawierac sciezki bezwzgledne, jest: {p!r}")

    akc = spec.get("akceptacja") or []
    if isinstance(akc, list):
        for i, a in enumerate(akc):
            if not isinstance(a, dict) or _pusty(a.get("komenda")) or _pusty(a.get("oczekiwany_wynik")):
                bledy.append(
                    f"`akceptacja[{i}]` musi miec `komenda` i `oczekiwany_wynik`. "
                    "Bez sprawdzalnej komendy kontraktor nie wie, kiedy skonczyl."
                )

    if len(str(spec.get("cel", ""))) < MIN_CEL_CHARS:
        bledy.append(f"`cel` jest za krotki (min {MIN_CEL_CHARS} znakow) — to bedzie ogolnik.")

    tryb = str(spec.get("tryb", "")).strip().lower()
    zdolnosc = ZDOLNOSCI.get(contractor)
    if zdolnosc and tryb and tryb not in zdolnosc["tryby"]:
        bledy.append(
            f"NIEDOPASOWANIE TRYBU: kontraktor `{contractor}` nie obsluguje trybu `{tryb}`. "
            f"Obsluguje: {', '.join(sorted(zdolnosc['tryby']))}. Powod: {zdolnosc['dowod']}. "
            "Zlecenie w niewspieranym trybie spali run i wroci bez wyniku — wybierz innego "
            "kontraktora albo przeformuluj zadanie na tryb, ktory ten kontraktor uniesie."
        )

    if profil(contractor, tryb) == "wyrobnik":
        kroki = spec.get("kroki") or []
        if len(kroki) < 3:
            bledy.append(
                f"Kontraktor `{contractor}` ma profil WYROBNIK — wymagane min. 3 ponumerowane "
                "`kroki` z dokladnymi komendami. Nie liczymy na jego kreatywnosc."
            )
        for i, k in enumerate(kroki):
            if len(str(k)) < 20:
                bledy.append(f"`kroki[{i}]` jest za ogolny ({len(str(k))} znakow): {k!r}")

    return bledy


def _linie(v) -> str:
    if isinstance(v, list):
        return "\n".join(f"  - {x}" for x in v)
    return f"  {v}"


def renderuj(spec: dict, contractor: str) -> str:
    b = spec.get("budzet") or {}
    akc = "\n".join(
        f"  $ {a['komenda']}\n    => {a['oczekiwany_wynik']}" for a in (spec.get("akceptacja") or [])
    )
    wspolne = f"""KATALOG ROBOCZY (pracuj WYLACZNIE tutaj):
  {spec['katalog_roboczy']}

CEL:
  {spec['cel']}

ZAKRES — lista zamknieta. Nie dotykaj niczego poza nia:
{_linie(spec.get('zakres'))}

ZAKAZY:
{_linie(spec.get('zakazy'))}

KONTEKST:
{_linie(spec.get('kontekst'))}

KRYTERIUM AKCEPTACJI — uruchom i pokaz wynik:
{akc}

WYMAGANY DOWOD:
{_linie(spec.get('dowod'))}

FORMAT ODPOWIEDZI:
  {spec['wyjscie']}
  Odpowiedz bez sekcji EVIDENCE jest odrzucana bez oceny.

BUDZET: {b.get('minuty', '?')} min | max {b.get('max_plikow', '?')} plikow | reasoning={b.get('reasoning', '?')}
TRYB: {spec.get('tryb')}"""

    if contractor in WYROBNIK:
        kroki = "\n".join(f"  {i}. {k}" for i, k in enumerate(spec.get("kroki") or [], 1))
        return f"""Wykonaj DOKLADNIE ponizsza procedure. Nie improwizuj, nie zmieniaj kolejnosci,
nie dodawaj wlasnych ulepszen. Jesli krok sie nie powiedzie — ZATRZYMAJ sie
i zglos to w EVIDENCE zamiast szukac obejscia.

{wspolne}

PROCEDURA KROK PO KROKU:
{kroki}

PO KAZDYM KROKU napisz jedna linie: `KROK <n>: OK` albo `KROK <n>: BLAD — <co dokladnie>`.
Nie przechodz do kolejnego kroku, jesli poprzedni zwrocil BLAD.
Jesli czegos nie ma w procedurze — NIE ROB TEGO. Zglos jako uwage na koncu."""

    return f"""Zadanie inzynierskie. Droge dobierz sam, ale kryterium akceptacji i zakres
sa wiazace — wyjscie poza zakres uniewaznia wynik.

{wspolne}

Jesli uznasz, ze specyfikacja jest bledna albo niewykonalna — NIE realizuj jej
po swojemu. Zwroc FINDINGS z uzasadnieniem i zatrzymaj sie."""


def main() -> int:
    ap = argparse.ArgumentParser(description="Bramka specyfikacji zlecenia dla Agent Fleet.")
    ap.add_argument("--spec", help="plik JSON ze specyfikacja")
    ap.add_argument("--contractor", help="grok|antigravity|gemini|qwen|claude|codex|kimi")
    ap.add_argument("--out", help="zapisz wyrenderowany prompt do pliku")
    ap.add_argument("--template", action="store_true", help="wypisz pusty szablon i zakoncz")
    a = ap.parse_args()

    if a.template:
        print(json.dumps(TEMPLATE, indent=2, ensure_ascii=False))
        return 0
    if not a.spec or not a.contractor:
        sys.exit("Podaj --spec i --contractor (albo --template).")

    contractor = a.contractor.strip().lower()
    if contractor not in WYROBNIK | INZYNIER | POSREDNI:
        sys.exit(f"Nieznany kontraktor: {contractor}. Znane: {sorted(WYROBNIK | INZYNIER | POSREDNI)}")

    spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    bledy = waliduj(spec, contractor)
    if bledy:
        print(f"ODMOWA — specyfikacja niekompletna ({len(bledy)} problemow):\n", file=sys.stderr)
        for b in bledy:
            print(f"  * {b}", file=sys.stderr)
        print(
            "\nNie wysylaj tego zlecenia. Niedospecyfikowane zadanie kosztuje PODWOJNIE:"
            "\nrun kontraktora + tura rodzica na poprawianie.",
            file=sys.stderr,
        )
        return 4

    prompt = renderuj(spec, contractor)
    _p = profil(contractor, str(spec.get("tryb", "")).lower())
    profil_opis = "WYROBNIK (krok po kroku)" if _p == "wyrobnik" else "INZYNIER (cel + ograniczenia)"
    if a.out:
        Path(a.out).write_text(prompt, encoding="utf-8")
        print(f"OK — profil {profil_opis}, {len(prompt)} znakow -> {a.out}")
        print(f"Dalej: fleet.py run --agent {contractor} --task-file {a.out} --mode {spec.get('tryb')}")
    else:
        print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
