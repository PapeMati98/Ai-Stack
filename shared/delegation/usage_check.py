#!/usr/bin/env python3
"""
Odczyt zuzycia limitow Claude i wyznaczenie TRYBU PRACY.

Zgodnosc z Fala 1 (U1) oraz Fala 2 (V4):
- Podkomenda `ingest`: czyta JSON get_usage ze stdin, waliduje i atomowo zapisuje claude-plan-usage.json.
- Odczyt trybu wybiera najswiezsza wazna probke z dwoch zrodel (ingest oraz plan-usage-history.json).
- Wynik (--json) zawiera: provider, source, source_kind, extra_usage_enabled, observed_at,
  age_minutes, freshness, owner_declaration, mode.
- stale, missing, invalid telemetria daje mode co najmniej oszczedny (nigdy normalny bez waznej deklaracji wlasciciela z wymus: true).
- Uczciwy pomiar: brak twierdzen o dokupionych kredytach bez wyraznej waznej deklaracji wlasciciela;
  gdy extra_usage_enabled jest false, tekst zadnego trybu nie sugeruje pracy na dokupionych kredytach.
- Odpornosc na bledne wejscia: procenty 0/100/ponad 100/ujemne/NaN/nie-liczby, przyszly timestamp,
  probki nie po kolei, reset okna, pusty plik, zly JSON, brak samples / windows.
- Zero wnioskow o innych dostawcach; zachowana semantyka --quiet i wag trybow.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
import time
from pathlib import Path

SCIEZKA = Path(os.environ.get("APPDATA", "")) / "Claude" / "plan-usage-history.json"
SCIEZKA_INGEST = Path.home() / ".agents" / "usage" / "claude-plan-usage.json"


def get_sciezka_ingest() -> Path:
    env = os.environ.get("CLAUDE_PLAN_USAGE_PATH")
    if env:
        return Path(env)
    return SCIEZKA_INGEST


# Reczna deklaracja zrodla rozliczenia. Uzupelnia pomiar tam, gdzie pomiar jest slepy.
#   {"claude": {"tryb": "kredytowy", "od": "2026-08-10", "wazne_do": "2026-08-17",
#               "powod": "...", "wymus": false}}
# `wazne_do` jest OBOWIAZKOWE i celowe: deklaracja wygasa sama, zeby zapomniana
# flaga nie trzymala pracy w trybie kredytowym po odnowieniu subskrypcji.
SCIEZKA_ROZLICZENIE = Path.home() / ".agents" / "billing-mode.json"

# Progi zuzycia okien subskrypcyjnych.
PROG_UWAGA = 70
PROG_KREDYT = 85
PROG_OKNO_5H = 90

# Tlo odpytuje co 15 min; starsza probka = stale / UNKNOWN.
MAX_WIEK_PROBKI_MIN = 60

TRYBY = {
    "normalny": (
        0,
        "Limity w normie. Wolno uzywac wlasnych podagentow tam, gdzie sa lepszym narzedziem, "
        "ale regula rozwazenia delegacji obowiazuje jak zwykle.",
    ),
    "oszczedny": (
        1,
        "Zblizasz sie do limitu lub brak swiezej telemetrii. Wszystko, co kontraktor UMIE zrobic, ma isc do kontraktora. "
        "Wlasne podagenty tylko tam, gdzie kontraktor fizycznie nie da rady.",
    ),
    "kredytowy": (
        2,
        "Wysokie zuzycie limitu subskrypcji lub deklaracja wlasciciela. Nie odpalaj wlasnych podagentow ani workflow, "
        "jesli zadanie da sie zlecic. Dostepni: qwen (advisory/review, mocny merytorycznie), "
        "grok i gemini/antigravity (wyrobnicy - tylko procedura krok po kroku). "
        "Wlasny podagent wymaga uzasadnienia w raporcie.",
    ),
}


def _prawidlowy_procent(val) -> bool:
    if val is None or isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    if math.isnan(val) or math.isinf(val):
        return False
    if val < 0:
        return False
    return True


def _prawidlowy_procent_ingest(val) -> bool:
    if not _prawidlowy_procent(val):
        return False
    if val > 100:
        return False
    return True


def _parse_iso(s: str) -> datetime.datetime:
    s = s.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _is_5h_window(label: str) -> bool:
    l = label.lower()
    return "5-hour" in l or "5 hour" in l or "5h" in l or "5_hour" in l or "5-h" in l


def _is_weekly_all_window(label: str) -> bool:
    l = label.lower()
    is_weekly = "weekly" in l or "7d" in l or "tydzien" in l or "tydzień" in l
    is_all = "all models" in l or "all-models" in l or "all_models" in l or ("all" in l and "model" in l)
    return is_weekly and is_all


def _is_weekly_window(label: str) -> bool:
    l = label.lower()
    return "weekly" in l or "7d" in l or "tydzien" in l or "tydzień" in l


def ingest(
    raw_input: str | dict | None = None,
    observed_at: str | None = None,
    target_path: Path | None = None,
) -> tuple[int, dict]:
    """Walidacja i atomowy zapis danych get_usage do claude-plan-usage.json.

    Zwraca (kod_wyjscia, payload_wynikowy).
    Kod 0 = sukces, kod 2 = odmowa (zgodnie ze specyfikacja V4).
    """
    if raw_input is None:
        raw_input = sys.stdin.read()

    if isinstance(raw_input, str):
        if not raw_input.strip():
            return 2, {"status": "error", "code": "invalid_json", "error": "puste wejscie"}
        try:
            d = json.loads(raw_input)
        except json.JSONDecodeError as e:
            return 2, {"status": "error", "code": "invalid_json", "error": f"niepoprawny JSON: {e}"}
    elif isinstance(raw_input, dict):
        d = raw_input
    else:
        return 2, {"status": "error", "code": "invalid_input", "error": "oczekiwano obiektu JSON"}

    if not isinstance(d, dict):
        return 2, {"status": "error", "code": "invalid_input", "error": "oczekiwano obiektu JSON"}

    # Wejscie moze byc calym wynikiem get_usage lub samym obiektem plan
    if "plan" in d and isinstance(d["plan"], dict):
        plan_obj = d["plan"]
    else:
        plan_obj = d

    status = plan_obj.get("status") or d.get("status")
    if status != "ok":
        err_code = status if isinstance(status, str) and status else "status_not_ok"
        return 2, {
            "status": "error",
            "code": err_code,
            "error": f"status '{status}' inny niz 'ok'",
        }

    windows = plan_obj.get("windows") or d.get("windows")
    if not isinstance(windows, list):
        return 2, {
            "status": "error",
            "code": "missing_window",
            "error": "brak lub niepoprawna lista windows",
        }

    five_hour_pct = None
    weekly_all_pct = None
    weekly_by_model: dict[str, int | float] = {}
    resets: dict[str, str] = {}

    for w in windows:
        if not isinstance(w, dict):
            return 2, {"status": "error", "code": "invalid_window", "error": "okno nie jest obiektem"}
        label = w.get("label")
        if not label or not isinstance(label, str):
            return 2, {"status": "error", "code": "invalid_window", "error": "brak etykiety okna"}
        pct = w.get("percentUsed")
        if not _prawidlowy_procent_ingest(pct):
            return 2, {
                "status": "error",
                "code": "invalid_percent",
                "error": f"niepoprawny procent ({pct!r}) w oknie '{label}' (musi byc liczba 0..100)",
            }

        resets_at = w.get("resetsAt")
        if resets_at is not None:
            resets[label] = str(resets_at)

        if _is_5h_window(label):
            five_hour_pct = pct
        elif _is_weekly_all_window(label):
            weekly_all_pct = pct
        elif _is_weekly_window(label):
            weekly_by_model[label] = pct

    if five_hour_pct is None or weekly_all_pct is None:
        brakujace = []
        if five_hour_pct is None:
            brakujace.append("5-hour")
        if weekly_all_pct is None:
            brakujace.append("Weekly · all models")
        return 2, {
            "status": "error",
            "code": "missing_window",
            "error": f"brak wymaganych okien: {', '.join(brakujace)}",
        }

    extra_obj = (
        plan_obj.get("extraUsage")
        or d.get("extraUsage")
        or plan_obj.get("extra_usage")
        or d.get("extra_usage")
        or {}
    )
    if isinstance(extra_obj, dict):
        extra_usage_enabled = bool(extra_obj.get("enabled", False))
        extra_usage_spent = extra_obj.get("spent")
        currency = extra_obj.get("currency")
    else:
        extra_usage_enabled = False
        extra_usage_spent = None
        currency = None

    if observed_at is not None:
        try:
            dt_obs = _parse_iso(observed_at)
            obs_iso = dt_obs.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception as e:
            return 2, {
                "status": "error",
                "code": "invalid_timestamp",
                "error": f"niepoprawny format observed_at: {e}",
            }
    else:
        obs_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    rekord = {
        "schema": "claude_plan_usage_v1",
        "provider": "claude",
        "source": "ccd_get_usage",
        "observed_at": obs_iso,
        "five_hour_pct": five_hour_pct,
        "weekly_all_pct": weekly_all_pct,
        "weekly_by_model": weekly_by_model,
        "resets": resets,
        "extra_usage_enabled": extra_usage_enabled,
        "extra_usage_spent": extra_usage_spent,
        "currency": currency,
    }

    docelowa = target_path or get_sciezka_ingest()
    docelowa.parent.mkdir(parents=True, exist_ok=True)
    tmp_plik = docelowa.with_name(f"{docelowa.name}.tmp.{os.getpid()}_{time.time_ns()}")
    try:
        tmp_plik.write_text(json.dumps(rekord, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_plik, docelowa)
    except OSError as e:
        return 2, {
            "status": "error",
            "code": "io_error",
            "error": f"nie udalo sie zapisac pliku: {e}",
        }
    finally:
        if tmp_plik.exists():
            try:
                tmp_plik.unlink()
            except OSError:
                pass

    return 0, rekord


def _odczytaj_history(sciezka: Path) -> dict:
    stan = {
        "provider": "claude",
        "source": str(sciezka),
        "source_kind": "plan_usage_history",
        "observed_at": None,
        "age_minutes": None,
        "freshness": "missing",
        "okno_5h_pct": None,
        "okno_7d_pct": None,
        "wiek_probki_min": None,
        "probek": 0,
        "pomiar": "UNKNOWN",
        "extra_usage_enabled": None,
        "owner_declaration": {
            "tryb": None,
            "wazne_do": None,
            "czy_wazna": False,
            "valid": False,
        },
        "mode": None,
    }

    if not sciezka.exists():
        stan["blad"] = f"brak pliku {sciezka}"
        stan["powod"] = "MISSING_TELEMETRY"
        return stan

    try:
        tekst = sciezka.read_text(encoding="utf-8-sig")
    except OSError as e:
        stan["freshness"] = "invalid"
        stan["blad"] = f"nie mozna odczytac: {e}"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    if not tekst.strip():
        stan["freshness"] = "invalid"
        stan["blad"] = "pusty plik"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    try:
        d = json.loads(tekst)
    except json.JSONDecodeError as e:
        stan["freshness"] = "invalid"
        stan["blad"] = f"nie mozna odczytac: {e}"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    if not isinstance(d, dict):
        stan["freshness"] = "invalid"
        stan["blad"] = "niepoprawny format danych (oczekiwano obiektu JSON)"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    if "samples" not in d:
        stan["freshness"] = "invalid"
        stan["blad"] = "brak klucza 'samples' w pliku"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    probki_surowe = d.get("samples")
    if not isinstance(probki_surowe, list):
        stan["freshness"] = "invalid"
        stan["blad"] = "klucz 'samples' nie jest lista"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    if not probki_surowe:
        stan["freshness"] = "missing"
        stan["blad"] = "brak probek w historii"
        stan["powod"] = "MISSING_TELEMETRY"
        return stan

    # Walidacja probek i sortowanie chronologiczne po `t`
    posortowane = []
    for x in probki_surowe:
        if not isinstance(x, dict):
            stan["freshness"] = "invalid"
            stan["blad"] = "probka nie jest obiektem"
            stan["powod"] = "INVALID_TELEMETRY"
            return stan
        t = x.get("t")
        if not isinstance(t, (int, float)) or isinstance(t, bool) or math.isnan(t) or math.isinf(t):
            stan["freshness"] = "invalid"
            stan["blad"] = "niepoprawny timestamp probki"
            stan["powod"] = "INVALID_TELEMETRY"
            return stan
        posortowane.append(x)

    posortowane.sort(key=lambda item: item["t"])
    ost = posortowane[-1]
    t_ost = ost["t"]

    teraz_ms = time.time() * 1000.0
    wiek_ms = teraz_ms - t_ost
    wiek_min = wiek_ms / 60000.0

    try:
        dt = datetime.datetime.fromtimestamp(t_ost / 1000.0, tz=datetime.timezone.utc)
        observed_at = dt.isoformat()
    except (ValueError, OSError, OverflowError):
        observed_at = None

    stan["probek"] = len(posortowane)
    stan["observed_at"] = observed_at

    # Przyszly timestamp (> 60s w przyszlosci) = nieprawidlowe dane
    if wiek_ms < -60000.0:
        stan["freshness"] = "invalid"
        stan["age_minutes"] = round(wiek_min, 1)
        stan["wiek_probki_min"] = round(wiek_min, 1)
        stan["blad"] = "probka z przyszlym timestampem"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    if wiek_min < 0:
        wiek_min = 0.0

    stan["age_minutes"] = round(wiek_min, 1)
    stan["wiek_probki_min"] = round(wiek_min, 1)

    u = ost.get("u")
    if not isinstance(u, dict):
        stan["freshness"] = "invalid"
        stan["blad"] = "brak lub zly format obiektu 'u' w probce"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    fh = u.get("fh")
    sd = u.get("sd")

    if not _prawidlowy_procent(fh) or not _prawidlowy_procent(sd):
        stan["freshness"] = "invalid"
        stan["okno_5h_pct"] = fh if _prawidlowy_procent(fh) else None
        stan["okno_7d_pct"] = sd if _prawidlowy_procent(sd) else None
        stan["blad"] = "nieprawidlowe wartosci procentowe (NaN, ujemne lub nie-liczby)"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    stan["okno_5h_pct"] = fh
    stan["okno_7d_pct"] = sd

    if wiek_min > MAX_WIEK_PROBKI_MIN:
        # Informacyjnie zachowujemy odczytane wartosci, ale pomiar jest nieaktualny
        stan["freshness"] = "stale"
        stan["powod"] = "STALE_TELEMETRY"
        stan["pomiar"] = "UNKNOWN"
        return stan

    stan["freshness"] = "fresh"
    stan["pomiar"] = "OK"
    stan.update(tempo_tygodnia(posortowane, sd))
    return stan


def _odczytaj_ingest(sciezka: Path) -> dict:
    stan = {
        "provider": "claude",
        "source": str(sciezka),
        "source_kind": "ccd_get_usage",
        "observed_at": None,
        "age_minutes": None,
        "freshness": "missing",
        "okno_5h_pct": None,
        "okno_7d_pct": None,
        "wiek_probki_min": None,
        "probek": 0,
        "pomiar": "UNKNOWN",
        "extra_usage_enabled": None,
        "extra_usage_spent": None,
        "currency": None,
        "weekly_by_model": {},
        "resets": {},
        "owner_declaration": {
            "tryb": None,
            "wazne_do": None,
            "czy_wazna": False,
            "valid": False,
        },
        "mode": None,
    }

    if not sciezka.exists():
        stan["blad"] = f"brak pliku {sciezka}"
        stan["powod"] = "MISSING_TELEMETRY"
        return stan

    try:
        tekst = sciezka.read_text(encoding="utf-8-sig")
    except OSError as e:
        stan["freshness"] = "invalid"
        stan["blad"] = f"nie mozna odczytac: {e}"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    if not tekst.strip():
        stan["freshness"] = "invalid"
        stan["blad"] = "pusty plik"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    try:
        d = json.loads(tekst)
    except json.JSONDecodeError as e:
        stan["freshness"] = "invalid"
        stan["blad"] = f"nie mozna odczytac: {e}"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    if not isinstance(d, dict):
        stan["freshness"] = "invalid"
        stan["blad"] = "niepoprawny format danych (oczekiwano obiektu JSON)"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    obs_str = d.get("observed_at")
    if not isinstance(obs_str, str):
        stan["freshness"] = "invalid"
        stan["blad"] = "brak lub niepoprawny observed_at"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    try:
        obs_dt = _parse_iso(obs_str)
        t_ms = obs_dt.timestamp() * 1000.0
    except Exception as e:
        stan["freshness"] = "invalid"
        stan["blad"] = f"niepoprawny format observed_at: {e}"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    teraz_ms = time.time() * 1000.0
    wiek_ms = teraz_ms - t_ms
    wiek_min = wiek_ms / 60000.0

    stan["probek"] = 1
    stan["observed_at"] = obs_str
    stan["extra_usage_enabled"] = d.get("extra_usage_enabled")
    stan["extra_usage_spent"] = d.get("extra_usage_spent")
    stan["currency"] = d.get("currency")
    stan["weekly_by_model"] = d.get("weekly_by_model", {})
    stan["resets"] = d.get("resets", {})

    if wiek_ms < -60000.0:
        stan["freshness"] = "invalid"
        stan["age_minutes"] = round(wiek_min, 1)
        stan["wiek_probki_min"] = round(wiek_min, 1)
        stan["blad"] = "probka z przyszlym timestampem"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    if wiek_min < 0:
        wiek_min = 0.0

    stan["age_minutes"] = round(wiek_min, 1)
    stan["wiek_probki_min"] = round(wiek_min, 1)

    fh = d.get("five_hour_pct")
    sd = d.get("weekly_all_pct")

    if not _prawidlowy_procent(fh) or not _prawidlowy_procent(sd):
        stan["freshness"] = "invalid"
        stan["okno_5h_pct"] = fh if _prawidlowy_procent(fh) else None
        stan["okno_7d_pct"] = sd if _prawidlowy_procent(sd) else None
        stan["blad"] = "nieprawidlowe wartosci procentowe (NaN, ujemne lub nie-liczby)"
        stan["powod"] = "INVALID_TELEMETRY"
        return stan

    stan["okno_5h_pct"] = fh
    stan["okno_7d_pct"] = sd

    if wiek_min > MAX_WIEK_PROBKI_MIN:
        stan["freshness"] = "stale"
        stan["powod"] = "STALE_TELEMETRY"
        stan["pomiar"] = "UNKNOWN"
        return stan

    stan["freshness"] = "fresh"
    stan["pomiar"] = "OK"
    return stan


def _wiek_zrodla(s: dict) -> float:
    w = s.get("age_minutes")
    if w is None or not isinstance(w, (int, float)) or math.isnan(w):
        return float("inf")
    return float(w)


def _wybierz_zrodlo(historia: dict, ingest: dict) -> dict:
    f_h = historia.get("freshness")
    f_i = ingest.get("freshness")

    # Gdy oba sa swieze: wygrywa swiezsze (mniejszy wiek)
    if f_i == "fresh" and f_h == "fresh":
        return ingest if _wiek_zrodla(ingest) <= _wiek_zrodla(historia) else historia

    # Gdy jedno jest swieze: wygrywa swieze
    if f_i == "fresh":
        return ingest
    if f_h == "fresh":
        return historia

    # Gdy oba sa stale: wygrywa swiezsze z dwoch nieaktualnych
    if f_i == "stale" and f_h == "stale":
        return ingest if _wiek_zrodla(ingest) <= _wiek_zrodla(historia) else historia

    # Gdy jedno jest stale, a drugie missing/invalid: stale niesie jakas informacje
    if f_i == "stale":
        return ingest
    if f_h == "stale":
        return historia

    # Gdy jedno jest invalid, a drugie missing: raportuj blad invalid
    if f_h == "invalid" and f_i == "missing":
        return historia
    if f_i == "invalid" and f_h == "missing":
        return ingest
    if f_i == "invalid" and f_h == "invalid":
        return ingest if _wiek_zrodla(ingest) <= _wiek_zrodla(historia) else historia

    # Domyslnie (np. oba missing): zachowaj historie
    return historia


def odczyt() -> dict:
    stan_historia = _odczytaj_history(SCIEZKA)
    stan_ingest = _odczytaj_ingest(get_sciezka_ingest())
    return _wybierz_zrodlo(stan_historia, stan_ingest)


def tempo_tygodnia(probki: list, sd) -> dict:
    """Tempo spalania biezacego cyklu tygodniowego.

    Cykl tygodniowy resetuje sie w okolicach niedzieli ~16:00.
    Prog wykrycia resetu (spadek o >15 pkt).
    """
    if not _prawidlowy_procent(sd):
        return {}
    prev = None
    start = None
    for x in probki:
        if not isinstance(x, dict):
            continue
        u = x.get("u")
        if not isinstance(u, dict):
            continue
        v = u.get("sd")
        if not _prawidlowy_procent(v):
            continue
        if prev is not None and v < prev - 15:
            start = x.get("t")
        prev = v
    if (
        start is None
        or not isinstance(start, (int, float))
        or isinstance(start, bool)
        or math.isnan(start)
        or math.isinf(start)
    ):
        return {}
    uplyw_dni = (time.time() * 1000 - start) / 86400000
    if uplyw_dni <= 0.05:
        return {"cykl_start": start}
    uplyw_pct = uplyw_dni / 7 * 100
    tempo = sd / uplyw_pct if uplyw_pct else 0
    wynik = {
        "cykl_start": time.strftime("%Y-%m-%d %H:%M", time.localtime(start / 1000)),
        "cykl_reset": time.strftime("%a %Y-%m-%d %H:%M", time.localtime((start + 7 * 86400000) / 1000)),
        "uplynelo_pct": round(uplyw_pct, 1),
        "tempo": round(tempo, 2),
    }
    if tempo > 1.0 and sd > 0:
        wynik["projekcja_na_koniec_pct"] = round(tempo * 100)
        dni_do_sciany = (100 / sd) * uplyw_dni
        if dni_do_sciany < 7:
            wynik["sciana"] = time.strftime(
                "%a %Y-%m-%d %H:%M", time.localtime((start + dni_do_sciany * 86400000) / 1000)
            )
    return wynik


def tryb_z_pomiaru(stan: dict) -> str:
    freshness = stan.get("freshness")
    if freshness != "fresh":
        return "oszczedny"
    sd = stan.get("okno_7d_pct")
    fh = stan.get("okno_5h_pct")
    if not _prawidlowy_procent(sd) or not _prawidlowy_procent(fh):
        return "oszczedny"
    if sd >= PROG_KREDYT or fh >= PROG_OKNO_5H:
        return "kredytowy"
    if sd >= PROG_UWAGA:
        return "oszczedny"
    return "normalny"


def deklaracja() -> dict | None:
    """Reczna deklaracja rozliczenia dla Claude, o ile wazna."""
    if not SCIEZKA_ROZLICZENIE.exists():
        return None
    try:
        surowe = json.loads(SCIEZKA_ROZLICZENIE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"blad_ksztaltu": "plik nieczytelny (bledny JSON)"}
    if not isinstance(surowe, dict):
        return {"blad_ksztaltu": "niepoprawny format deklaracji (oczekiwano obiektu JSON)"}
    d = None
    gniazdo = surowe.get("narzedzia")
    if isinstance(gniazdo, dict) and isinstance(gniazdo.get("claude"), dict):
        d = gniazdo["claude"]
    elif isinstance(surowe.get("claude"), dict):
        d = surowe["claude"]
    if d is None:
        znane = {"narzedzia", "claude", "codex", "qwen", "kimi", "grok", "antigravity"}
        obce = [k for k in surowe if not k.startswith("_") and k not in znane]
        if obce:
            return {"blad_ksztaltu": f"nierozpoznane klucze najwyzszego poziomu: {obce} - SPRAWDZ RECZNIE"}
        return None
    if d.get("tryb") not in TRYBY:
        return {"blad_ksztaltu": f"nieznany tryb: {d.get('tryb')!r}"}
    do = str(d.get("wazne_do") or "")
    if do < time.strftime("%Y-%m-%d"):
        return {"wygasla": True, "wazne_do": do, "tryb": d["tryb"]}
    return d


def _zbuduj_owner_declaration(d: dict | None) -> dict:
    if d is None:
        return {
            "tryb": None,
            "wazne_do": None,
            "czy_wazna": False,
            "valid": False,
        }
    if d.get("blad_ksztaltu"):
        return {
            "tryb": None,
            "wazne_do": None,
            "czy_wazna": False,
            "valid": False,
            "blad": d["blad_ksztaltu"],
        }
    if d.get("wygasla"):
        return {
            "tryb": d.get("tryb"),
            "wazne_do": d.get("wazne_do"),
            "czy_wazna": False,
            "valid": False,
            "wygasla": True,
        }
    return {
        "tryb": d.get("tryb"),
        "wazne_do": d.get("wazne_do"),
        "czy_wazna": True,
        "valid": True,
        "wymus": bool(d.get("wymus")),
        "powod": d.get("powod"),
    }


def tryb(stan: dict) -> str:
    """Deklaracja podnosi tryb zawsze; obniza tylko przy jawnym `wymus: true`."""
    zmierzony = tryb_z_pomiaru(stan)
    stan["tryb_zmierzony"] = zmierzony
    d = deklaracja()
    owner_decl = _zbuduj_owner_declaration(d)
    stan["owner_declaration"] = owner_decl

    if not d:
        stan["mode"] = zmierzony
        stan["tryb"] = zmierzony
        return zmierzony
    if d.get("blad_ksztaltu"):
        stan["deklaracja"] = f"!! {d['blad_ksztaltu']}"
        t_final = zmierzony if TRYBY[zmierzony][0] >= 1 else "oszczedny"
        stan["mode"] = t_final
        stan["tryb"] = t_final
        return t_final
    if d.get("wygasla"):
        stan["deklaracja"] = f"WYGASLA {d['wazne_do']} (byla: {d['tryb']}) - obowiazuje pomiar"
        stan["mode"] = zmierzony
        stan["tryb"] = zmierzony
        return zmierzony

    zadeklarowany = d["tryb"]
    if TRYBY[zadeklarowany][0] > TRYBY[zmierzony][0] or d.get("wymus"):
        stan["deklaracja"] = (
            f"{zadeklarowany} do {d['wazne_do']}"
            + (f" - {d['powod']}" if d.get("powod") else "")
            + (" [WYMUSZONE OBNIZENIE]" if TRYBY[zadeklarowany][0] < TRYBY[zmierzony][0] else "")
        )
        stan["mode"] = zadeklarowany
        stan["tryb"] = zadeklarowany
        return zadeklarowany

    stan["deklaracja"] = f"{zadeklarowany} do {d['wazne_do']} (pomiar surowszy, wygrywa pomiar)"
    stan["mode"] = zmierzony
    stan["tryb"] = zmierzony
    return zmierzony


def main() -> int:
    ap = argparse.ArgumentParser(description="Tryb pracy wg zuzycia limitow Claude.")
    subparsers = ap.add_subparsers(dest="subcommand")

    ingest_parser = subparsers.add_parser("ingest", help="Ingest get_usage JSON ze stdin")
    ingest_parser.add_argument(
        "--observed-at",
        dest="observed_at",
        default=None,
        help="Opcjonalny timestamp probki ISO (UTC)",
    )

    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if a.subcommand == "ingest":
        raw = sys.stdin.read()
        kod, payload = ingest(raw_input=raw, observed_at=a.observed_at)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return kod

    stan = odczyt()
    t = tryb(stan)
    waga, opis = TRYBY[t]
    stan["mode"] = t
    stan["tryb"] = t
    stan["dyrektywa"] = opis

    if a.json:
        print(json.dumps(stan, indent=2, ensure_ascii=False))
    elif a.quiet:
        print(t)
    elif stan.get("blad") and stan.get("freshness") != "fresh":
        print(f"[limity] {stan['blad']} - brak swiezych danych ({stan.get('freshness')}), tryb: {t.upper()}.")
        if stan.get("extra_usage_enabled") is not None:
            extra_str = "wlaczone" if stan["extra_usage_enabled"] else "wylaczone"
            print(f"[limity] extra usage: {extra_str}")
        if stan.get("deklaracja"):
            print(f"[limity] deklaracja rozliczenia: {stan['deklaracja']}")
        print(f"[limity] TRYB: {t.upper()} - {opis}")
    elif stan.get("freshness") == "stale" or stan.get("pomiar") == "UNKNOWN":
        wiek_h = (stan.get("age_minutes") or 0) / 60.0
        print(
            f"[limity] pomiar UNKNOWN (stale): ostatnia probka sprzed {wiek_h:.1f} h "
            f"(5h: {stan.get('okno_5h_pct')}%, tydzien: {stan.get('okno_7d_pct')}% - NIEAKTUALNE, nie licze z nich trybu)."
        )
        print("[limity] Odswiez: w sesji Claude Desktop odczytaj get_usage i przekaz wynik do `usage_check.py ingest`.")
        if stan.get("extra_usage_enabled") is not None:
            extra_str = "wlaczone" if stan["extra_usage_enabled"] else "wylaczone"
            print(f"[limity] extra usage: {extra_str}")
        if stan.get("deklaracja"):
            print(f"[limity] deklaracja rozliczenia: {stan['deklaracja']}")
        print(f"[limity] TRYB: {t.upper()} (brak swiezego pomiaru) - {opis}")
    else:
        wiek = stan.get("age_minutes")
        wiek_str = f"{wiek:.0f} min" if isinstance(wiek, (int, float)) else "?"
        print(f"[limity] okno 5h: {stan.get('okno_5h_pct')}% | tydzien: {stan.get('okno_7d_pct')}% "
              f"(claude, zrodlo {stan.get('source_kind') or '?'}, wiek {wiek_str}, {stan.get('freshness') or '?'})")
        if stan.get("tempo"):
            sciana = f" -> SCIANA {stan['sciana']}" if stan.get("sciana") else ""
            print(
                f"[limity] cykl do {stan.get('cykl_reset')} | uplynelo {stan.get('uplynelo_pct')}% "
                f"| tempo {stan.get('tempo')}x{sciana}"
            )
        if stan.get("extra_usage_enabled") is not None:
            extra_str = "wlaczone" if stan["extra_usage_enabled"] else "wylaczone"
            print(f"[limity] extra usage: {extra_str}")
        if stan.get("deklaracja"):
            print(f"[limity] deklaracja rozliczenia: {stan['deklaracja']}")
            if t != stan.get("tryb_zmierzony"):
                print(f"[limity] procent sugerowalby '{stan.get('tryb_zmierzony')}' - NIE UFAJ MU, deklaracja jest zrodlem prawdy")
        print(f"[limity] TRYB: {t.upper()} - {opis}")

    return waga


if __name__ == "__main__":
    raise SystemExit(main())
