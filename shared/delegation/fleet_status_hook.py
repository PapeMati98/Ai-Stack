#!/usr/bin/env python3
"""
Hook SessionStart: wstrzykuje stan floty do kontekstu agenta.

PO CO: audyt 2026-08-05 wykazal, ze reguly "rozwaz delegacje" nie da sie
egzekwowac samym zapisem w AGENTS.md — agent siegal po wlasne podagenty,
bo byli o jedno wywolanie narzedzia, a stan floty nie byl mu przed oczami.
Ten hook odwraca domyslna: dane o dostepnosci kontraktorow sa w kontekscie
ZANIM padnie pierwsza decyzja o delegacji.

Dodatkowo: Model Radar nie ma komendy `refresh` — to tylko czujnik swiezosci.
Odswiezenie wymaga researchu, czyli pracy agenta. Dlatego przy STALE hook
emituje DYREKTYWE, a nie sama informacje.

Zero zaleznosci zewnetrznych. Nigdy nie blokuje sesji — kazdy blad degraduje
sie do cichej linii informacyjnej.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SUITE = Path(os.environ.get("AGENT_FLEET_SUITE_ROOT")
             or Path.home() / ".agents" / "runtime" / "agent-fleet-suite" / "current")
RADAR = SUITE / "model-radar" / "scripts" / "model_radar.py"
FLEET = SUITE / "agent-fleet" / "scripts" / "fleet.py"


def _run(args: list[str], timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, encoding="utf-8",
                           errors="replace", stdin=subprocess.DEVNULL, timeout=timeout)
        return p.returncode, (p.stdout or "")
    except Exception:
        return 1, ""


def _json_tail(text: str) -> dict:
    """Wyciaga ostatni obiekt JSON ze strumienia (skrypty mieszaja logi z JSON-em)."""
    i = text.rfind("{")
    while i != -1:
        try:
            return json.loads(text[i:])
        except json.JSONDecodeError:
            i = text.rfind("{", 0, i)
    return {}


def main() -> int:
    out: list[str] = []

    # Tryb pracy wg zuzycia limitow — PIERWSZY, bo determinuje cala reszte decyzji.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import usage_check as uc

        stan = uc.odczyt()
        t = uc.tryb(stan)
        if stan.get("blad"):
            out.append(f"[limity] {stan['blad']} — tryb nieustalony, zakladam normalny.")
        elif stan.get("pomiar") == "UNKNOWN":
            out.append(
                f"[limity] pomiar UNKNOWN (probka sprzed {stan['wiek_probki_min'] / 60:.1f} h; "
                f"5h {stan['okno_5h_pct']}% / 7d {stan['okno_7d_pct']}% nieaktualne) — TRYB {t.upper()} "
                "bez swiezego pomiaru. Odswiez: otworz menu Claude w trayu."
            )
        else:
            stary = " (probka nieaktualna)" if (stan.get("wiek_probki_min") or 0) > 20 else ""
            znak = "!! " if t == "kredytowy" else ""
            out.append(
                f"[limity] {znak}okno 5h: {stan['okno_5h_pct']}% | okno 7d: {stan['okno_7d_pct']}%{stary} "
                f"— TRYB {t.upper()}: {uc.TRYBY[t][1]}"
            )
    except Exception:
        out.append("[limity] nie udalo sie odczytac zuzycia — sprawdz recznie przed duzym workflow.")

    if RADAR.exists():
        _, txt = _run([sys.executable, str(RADAR), "check"], 25)
        d = _json_tail(txt)
        status = d.get("status", "?")
        allowed = d.get("routing_allowed")
        age = d.get("age_days")
        age_s = f"{age:.1f}d" if isinstance(age, (int, float)) else "?"

        if status == "FRESH" and allowed:
            out.append(f"[flota] Model Radar FRESH ({age_s}) — routing adaptacyjny dostepny.")
        else:
            out.append(
                f"[flota] !! Model Radar {status} ({age_s}), routing_allowed={allowed}. "
                "WYKONAJ TERAZ, NIE PYTAJ: procedura 'Refresh the model market' ze skilla `model-radar`. "
                "Odswiezenie jest czescia zadania, nie osobna prosba do uzytkownika - radar nie ma komendy "
                "`refresh`, wiec to praca agenta. Nie raportuj przeterminowanego radaru jako blokady. "
                "Jesli odswiezenie sie nie powiedzie - podaj powod i uzyj routingu statycznego, zaznaczajac to."
            )
    else:
        out.append("[flota] Model Radar niedostepny — pomijam sprawdzenie swiezosci.")

    if FLEET.exists():
        _, txt = _run([sys.executable, str(FLEET), "quota-status"], 40)
        d = _json_tail(txt)

        # Znajdz mape agent -> dane. Klucz roznil sie miedzy wersjami pakietu,
        # wiec szukamy po ksztalcie, a nie po nazwie.
        agenci: dict = {}
        for v in d.values():
            if isinstance(v, dict) and v and all(
                isinstance(x, dict) and "status" in x for x in v.values()
            ):
                agenci = v
                break

        znani, nieznani = [], []
        for name, info in sorted(agenci.items()):
            st = info.get("status")
            conf = info.get("confidence") or 0.0
            if st in (None, "unknown") or conf <= 0.0:
                nieznani.append(name)
                continue
            head = info.get("headroom")
            cred = info.get("credits_remaining")
            age = info.get("age_hours")
            opis = f"{name}={st}"
            if head is not None:
                opis += f"/{head}"
            elif cred is not None:
                opis += f"/credits={cred}"
            if isinstance(age, (int, float)) and age > 24:
                opis += f" (snapshot {age/24:.1f}d - PRZETERMINOWANY)"
            znani.append(opis)

        if agenci:
            out.append(f"[flota] quota: {', '.join(znani) if znani else 'brak wiarygodnych danych'}")
            if nieznani:
                out.append(
                    f"[flota] !! BEZ DANYCH O QUOCIE ({len(nieznani)}/{len(agenci)}): {', '.join(nieznani)}. "
                    "Router traktuje `unknown` jako neutralne, wiec bez odswiezenia bedzie wracal do tych samych "
                    "profili, a oplacone i nieuzywane subskrypcje beda staly. Zbierz snapshot przed delegacja "
                    "albo jawnie uzasadnij wybor kontraktora."
                )
        else:
            out.append("[flota] quota-status bez danych - sprawdz recznie przed delegacja (nie zakladaj).")

    out.append(
        "[flota] Regula: rozwaz delegacje jawnie. Jesli pomijasz - podaj `Agent Fleet: SKIP - <konkretny powod>`. "
        "Zlecenia buduj przez `agent-stack/shared/delegation/spec_gate.py` (odmawia ogolnikow; "
        "grok/gemini/qwen wymagaja procedury krok po kroku). Na koncu odpowiedzi: tabela uzytych modeli."
    )

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        print("[flota] hook pominiety (blad wewnetrzny) — sesja nie jest blokowana.")
        raise SystemExit(0)
