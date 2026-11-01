"""Testy czytnika zuzycia Claude i wyznaczania trybu pracy.

Zgodnosc ze specyfikacja Fala 1 (U1) oraz Fala 2 (V4):
- Pola --json: provider, source, source_kind, extra_usage_enabled, observed_at, age_minutes,
  freshness, owner_declaration, mode.
- Stale/missing/invalid daje co najmniej tryb oszczedny (nigdy normalny bez waznej deklaracji z wymus: true).
- Brak twierdzen o dokupionych kredytach bez wyraznej deklaracji wlasciciela; gdy extra_usage_enabled jest false,
  tekst zadnego trybu nie sugeruje pracy na dokupionych kredytach.
- Odpornosc na wszystkie przypadki brzegowe: procenty 0/100/ponad 100/ujemne/NaN/nie-liczby,
  przyszly timestamp, probki nie po kolei, reset okna (spadek sd), pusty plik, zly JSON, brak samples / windows.
- Semantyka --quiet i kodow wyjscia (wagi trybow: 0=normalny, 1=oszczedny, 2=kredytowy).
- Podkomenda `ingest` waliduje JSON ze stdin i atomowo zapisuje claude-plan-usage.json (exit 0)
  albo odrzuca bledne wejscie (exit 2, JSON z kodem bledu).
- Odczyt wybiera najswiezsza wazna probke z obu zrodel (ingest vs history).
"""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import usage_check as uc  # noqa: E402


def _historia(katalog: Path, probki: list[tuple[float, int | float | None, int | float | None]]) -> Path:
    """probki: (wiek_min, fh, sd)."""
    teraz = time.time() * 1000
    p = katalog / "plan-usage-history.json"
    samples = []
    for wiek, fh, sd in probki:
        samples.append(
            {
                "t": teraz - wiek * 60000,
                "org": "test-org",
                "u": {"fh": fh, "sd": sd},
            }
        )
    p.write_text(json.dumps({"version": 2, "samples": samples}), encoding="utf-8")
    return p


def _ingest_plik(
    katalog: Path,
    wiek_min: float,
    fh: int | float,
    sd: int | float,
    extra_enabled: bool = False,
    extra_spent: str = "0.00",
    currency: str = "EUR",
    weekly_fable: int | float | None = 59,
    observed_at: str | None = None,
) -> Path:
    p = katalog / "claude-plan-usage.json"
    if observed_at is None:
        t_sec = time.time() - wiek_min * 60.0
        dt = datetime.datetime.fromtimestamp(t_sec, tz=datetime.timezone.utc)
        observed_at = dt.isoformat().replace("+00:00", "Z")

    windows = [
        {"label": "5-hour limit", "percentUsed": fh, "resetsAt": "2026-10-21T20:00:00.403Z"},
        {"label": "Weekly · all models", "percentUsed": sd, "resetsAt": "2026-10-25T14:00:00.403Z"},
    ]
    weekly_by_model = {}
    if weekly_fable is not None:
        windows.append(
            {"label": "Weekly · Fable", "percentUsed": weekly_fable, "resetsAt": "2026-10-25T14:00:00.404Z"}
        )
        weekly_by_model["Weekly · Fable"] = weekly_fable

    rekord = {
        "schema": "claude_plan_usage_v1",
        "provider": "claude",
        "source": "ccd_get_usage",
        "observed_at": observed_at,
        "five_hour_pct": fh,
        "weekly_all_pct": sd,
        "weekly_by_model": weekly_by_model,
        "resets": {
            "5-hour limit": "2026-10-21T20:00:00.403Z",
            "Weekly · all models": "2026-10-25T14:00:00.403Z",
        },
        "extra_usage_enabled": extra_enabled,
        "extra_usage_spent": extra_spent,
        "currency": currency,
    }
    p.write_text(json.dumps(rekord, indent=2), encoding="utf-8")
    return p


class UsageCheckTest(unittest.TestCase):
    def setUp(self):
        test_root = os.environ.get("AGENT_TEST_ROOT")
        self._tmp = tempfile.TemporaryDirectory(dir=test_root) if test_root else tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._stare = (
            uc.SCIEZKA,
            uc.SCIEZKA_ROZLICZENIE,
            uc.SCIEZKA_INGEST,
            os.environ.get("CLAUDE_PLAN_USAGE_PATH"),
        )
        uc.SCIEZKA = self.dir / "brak-plan-usage-history.json"
        uc.SCIEZKA_ROZLICZENIE = self.dir / "brak-billing-mode.json"
        uc.SCIEZKA_INGEST = self.dir / "brak-claude-plan-usage.json"
        os.environ.pop("CLAUDE_PLAN_USAGE_PATH", None)

    def tearDown(self):
        uc.SCIEZKA, uc.SCIEZKA_ROZLICZENIE, uc.SCIEZKA_INGEST, stare_env = self._stare
        if stare_env is not None:
            os.environ["CLAUDE_PLAN_USAGE_PATH"] = stare_env
        else:
            os.environ.pop("CLAUDE_PLAN_USAGE_PATH", None)
        self._tmp.cleanup()

    def _tryb(self, probki):
        uc.SCIEZKA = _historia(self.dir, probki)
        stan = uc.odczyt()
        return stan, uc.tryb(stan)

    def test_swieza_wysoka_probka_daje_kredytowy(self):
        stan, t = self._tryb([(10, 6, 89)])
        self.assertEqual(stan["pomiar"], "OK")
        self.assertEqual(stan["freshness"], "fresh")
        self.assertEqual(t, "kredytowy")

    def test_swieza_srednia_probka_daje_oszczedny(self):
        stan, t = self._tryb([(5, 10, 75)])
        self.assertEqual(stan["pomiar"], "OK")
        self.assertEqual(stan["freshness"], "fresh")
        self.assertEqual(t, "oszczedny")

    def test_swieza_niska_probka_daje_normalny(self):
        stan, t = self._tryb([(5, 10, 20)])
        self.assertEqual(stan["pomiar"], "OK")
        self.assertEqual(stan["freshness"], "fresh")
        self.assertEqual(t, "normalny")

    def test_nieaktualna_wysoka_probka_nie_wymusza_kredytowego_daje_oszczedny(self):
        # Scenariusz 2026-10-11: ostatnia probka sd=89 sprzed ~2 dni (martwy polling).
        # Stara probka nie wymusza falszywego kredytowego, ale daje stale -> co najmniej oszczedny.
        stan, t = self._tryb([(5000, 0, 90), (4900, 0, 5), (2900, 6, 89)])
        self.assertEqual(stan["pomiar"], "UNKNOWN")
        self.assertEqual(stan["freshness"], "stale")
        self.assertEqual(stan["okno_7d_pct"], 89)  # zachowane informacyjnie
        self.assertNotIn("tempo", stan)
        self.assertEqual(t, "oszczedny")

    def test_swieza_probka_po_resecie_liczy_tempo(self):
        stan, _ = self._tryb([(5000, 0, 90), (4900, 0, 5), (10, 6, 40)])
        self.assertEqual(stan["pomiar"], "OK")
        self.assertEqual(stan["freshness"], "fresh")
        self.assertIn("tempo", stan)

    def test_granica_wieku(self):
        stan, _ = self._tryb([(uc.MAX_WIEK_PROBKI_MIN - 1, 0, 90)])
        self.assertEqual(stan["pomiar"], "OK")
        self.assertEqual(stan["freshness"], "fresh")
        stan, _ = self._tryb([(uc.MAX_WIEK_PROBKI_MIN + 1, 0, 90)])
        self.assertEqual(stan["pomiar"], "UNKNOWN")
        self.assertEqual(stan["freshness"], "stale")

    def test_deklaracja_nadal_podnosi_tryb_przy_unknown(self):
        jutro = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
        uc.SCIEZKA_ROZLICZENIE.write_text(
            json.dumps({"claude": {"tryb": "kredytowy", "wazne_do": jutro}}), encoding="utf-8"
        )
        stan, t = self._tryb([(2900, 0, 10)])
        self.assertEqual(stan["pomiar"], "UNKNOWN")
        self.assertEqual(stan["freshness"], "stale")
        self.assertEqual(t, "kredytowy")

    def test_stale_z_wymuszonym_obnizeniem_wlasciciela_daje_normalny(self):
        jutro = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
        uc.SCIEZKA_ROZLICZENIE.write_text(
            json.dumps({"claude": {"tryb": "normalny", "wazne_do": jutro, "wymus": True}}),
            encoding="utf-8",
        )
        stan, t = self._tryb([(2900, 0, 10)])
        self.assertEqual(stan["freshness"], "stale")
        self.assertEqual(t, "normalny")
        self.assertTrue(stan["owner_declaration"]["valid"])

    def test_stale_z_niewymuszonym_obnizeniem_pozostaje_oszczedny(self):
        jutro = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
        uc.SCIEZKA_ROZLICZENIE.write_text(
            json.dumps({"claude": {"tryb": "normalny", "wazne_do": jutro, "wymus": False}}),
            encoding="utf-8",
        )
        stan, t = self._tryb([(2900, 0, 10)])
        self.assertEqual(stan["freshness"], "stale")
        self.assertEqual(t, "oszczedny")

    def test_stale_z_wygasla_deklaracja_pozostaje_oszczedny(self):
        wczoraj = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        uc.SCIEZKA_ROZLICZENIE.write_text(
            json.dumps({"claude": {"tryb": "normalny", "wazne_do": wczoraj, "wymus": True}}),
            encoding="utf-8",
        )
        stan, t = self._tryb([(2900, 0, 10)])
        self.assertEqual(stan["freshness"], "stale")
        self.assertEqual(t, "oszczedny")
        self.assertFalse(stan["owner_declaration"]["valid"])

    def test_brak_pliku_daje_missing_i_oszczedny(self):
        uc.SCIEZKA = self.dir / "nieistniejacy-plik.json"
        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "missing")
        self.assertEqual(stan["powod"], "MISSING_TELEMETRY")
        self.assertEqual(t, "oszczedny")

    def test_pusty_plik_daje_invalid_i_oszczedny(self):
        p = self.dir / "pusty.json"
        p.write_text("", encoding="utf-8")
        uc.SCIEZKA = p
        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "invalid")
        self.assertEqual(stan["powod"], "INVALID_TELEMETRY")
        self.assertEqual(t, "oszczedny")

    def test_zly_json_daje_invalid_i_oszczedny(self):
        p = self.dir / "zly.json"
        p.write_text("{zly json:", encoding="utf-8")
        uc.SCIEZKA = p
        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "invalid")
        self.assertEqual(stan["powod"], "INVALID_TELEMETRY")
        self.assertEqual(t, "oszczedny")

    def test_brak_klucza_samples_daje_invalid_i_oszczedny(self):
        p = self.dir / "bez_samples.json"
        p.write_text(json.dumps({"version": 2}), encoding="utf-8")
        uc.SCIEZKA = p
        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "invalid")
        self.assertEqual(t, "oszczedny")

    def test_brak_probek_pusta_lista_daje_missing_i_oszczedny(self):
        p = self.dir / "pusta_lista.json"
        p.write_text(json.dumps({"version": 2, "samples": []}), encoding="utf-8")
        uc.SCIEZKA = p
        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "missing")
        self.assertEqual(stan["powod"], "MISSING_TELEMETRY")
        self.assertEqual(t, "oszczedny")

    def test_procenty_zero(self):
        stan, t = self._tryb([(5, 0, 0)])
        self.assertEqual(stan["freshness"], "fresh")
        self.assertEqual(stan["okno_5h_pct"], 0)
        self.assertEqual(stan["okno_7d_pct"], 0)
        self.assertEqual(t, "normalny")

    def test_procenty_sto(self):
        stan, t = self._tryb([(5, 100, 100)])
        self.assertEqual(stan["freshness"], "fresh")
        self.assertEqual(stan["okno_7d_pct"], 100)
        self.assertEqual(t, "kredytowy")

    def test_procenty_ponad_sto(self):
        stan, t = self._tryb([(5, 120, 105)])
        self.assertEqual(stan["freshness"], "fresh")
        self.assertEqual(t, "kredytowy")

    def test_procenty_ujemne(self):
        stan, t = self._tryb([(5, -5, 50)])
        self.assertEqual(stan["freshness"], "invalid")
        self.assertEqual(stan["powod"], "INVALID_TELEMETRY")
        self.assertEqual(t, "oszczedny")

        stan2, t2 = self._tryb([(5, 10, -20)])
        self.assertEqual(stan2["freshness"], "invalid")
        self.assertEqual(t2, "oszczedny")

    def test_procenty_nan(self):
        p = self.dir / "nan.json"
        teraz = time.time() * 1000
        # W pliku zapisujemy rzeczywista wartosc NaN (json.dumps z float('nan') serializuje NaN,
        # a json.loads odczytuje float('nan'), cwiczac bezposrednio galaz math.isnan).
        p.write_text(
            json.dumps({
                "version": 2,
                "samples": [{"t": teraz - 300000, "org": "x", "u": {"fh": 10, "sd": float("nan")}}]
            }),
            encoding="utf-8",
        )
        uc.SCIEZKA = p
        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "invalid")
        self.assertEqual(stan["powod"], "INVALID_TELEMETRY")
        self.assertIsNone(stan["okno_7d_pct"])
        self.assertEqual(t, "oszczedny")

        # Testujemy tez fh jako NaN
        p_fh = self.dir / "nan_fh.json"
        p_fh.write_text(
            json.dumps({
                "version": 2,
                "samples": [{"t": teraz - 300000, "org": "x", "u": {"fh": float("nan"), "sd": 20}}]
            }),
            encoding="utf-8",
        )
        uc.SCIEZKA = p_fh
        stan_fh = uc.odczyt()
        t_fh = uc.tryb(stan_fh)
        self.assertEqual(stan_fh["freshness"], "invalid")
        self.assertEqual(stan_fh["powod"], "INVALID_TELEMETRY")
        self.assertIsNone(stan_fh["okno_5h_pct"])
        self.assertEqual(t_fh, "oszczedny")

    def test_prawidlowy_procent_jednostkowy(self):
        # Sprawdzenie bezposrednie pomocnika: math.isnan i math.isinf musza zwracac False
        self.assertFalse(uc._prawidlowy_procent(float("nan")))
        self.assertFalse(uc._prawidlowy_procent(math.nan))
        self.assertFalse(uc._prawidlowy_procent(float("inf")))
        self.assertFalse(uc._prawidlowy_procent(float("-inf")))
        self.assertFalse(uc._prawidlowy_procent(None))
        self.assertFalse(uc._prawidlowy_procent(True))
        self.assertFalse(uc._prawidlowy_procent(False))
        self.assertFalse(uc._prawidlowy_procent("50"))
        self.assertFalse(uc._prawidlowy_procent(-1))
        self.assertFalse(uc._prawidlowy_procent(-0.1))
        self.assertTrue(uc._prawidlowy_procent(0))
        self.assertTrue(uc._prawidlowy_procent(0.0))
        self.assertTrue(uc._prawidlowy_procent(50))
        self.assertTrue(uc._prawidlowy_procent(100))
        self.assertTrue(uc._prawidlowy_procent(120.5))

    def test_procenty_null(self):
        p = self.dir / "null.json"
        teraz = time.time() * 1000
        p.write_text(
            json.dumps({
                "version": 2,
                "samples": [{"t": teraz - 300000, "org": "x", "u": {"fh": 10, "sd": None}}]
            }),
            encoding="utf-8",
        )
        uc.SCIEZKA = p
        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "invalid")
        self.assertEqual(stan["powod"], "INVALID_TELEMETRY")
        self.assertIsNone(stan["okno_7d_pct"])
        self.assertEqual(t, "oszczedny")

    def test_procenty_infinity(self):
        p = self.dir / "inf.json"
        teraz = time.time() * 1000
        p.write_text(
            json.dumps({
                "version": 2,
                "samples": [{"t": teraz - 300000, "org": "x", "u": {"fh": float("inf"), "sd": 10}}]
            }),
            encoding="utf-8",
        )
        uc.SCIEZKA = p
        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "invalid")
        self.assertEqual(stan["powod"], "INVALID_TELEMETRY")
        self.assertIsNone(stan["okno_5h_pct"])
        self.assertEqual(t, "oszczedny")

    def test_timestamp_nan_i_inf(self):
        p = self.dir / "t_nan.json"
        p.write_text(
            json.dumps({
                "version": 2,
                "samples": [{"t": float("nan"), "org": "x", "u": {"fh": 10, "sd": 20}}]
            }),
            encoding="utf-8",
        )
        uc.SCIEZKA = p
        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "invalid")
        self.assertEqual(stan["powod"], "INVALID_TELEMETRY")
        self.assertEqual(stan["blad"], "niepoprawny timestamp probki")
        self.assertEqual(t, "oszczedny")

        p2 = self.dir / "t_inf.json"
        p2.write_text(
            json.dumps({
                "version": 2,
                "samples": [{"t": float("inf"), "org": "x", "u": {"fh": 10, "sd": 20}}]
            }),
            encoding="utf-8",
        )
        uc.SCIEZKA = p2
        stan2 = uc.odczyt()
        t2 = uc.tryb(stan2)
        self.assertEqual(stan2["freshness"], "invalid")
        self.assertEqual(stan2["powod"], "INVALID_TELEMETRY")
        self.assertEqual(stan2["blad"], "niepoprawny timestamp probki")
        self.assertEqual(t2, "oszczedny")

    def test_procenty_nie_liczby(self):
        p = self.dir / "nie_liczby.json"
        teraz = time.time() * 1000
        p.write_text(
            json.dumps({
                "version": 2,
                "samples": [{"t": teraz - 300000, "org": "x", "u": {"fh": "sto", "sd": True}}]
            }),
            encoding="utf-8",
        )
        uc.SCIEZKA = p
        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "invalid")
        self.assertEqual(t, "oszczedny")

    def test_przyszly_timestamp(self):
        # Timestamp 60 min w przyszlosc
        stan, t = self._tryb([(-60, 10, 20)])
        self.assertEqual(stan["freshness"], "invalid")
        self.assertEqual(stan["powod"], "INVALID_TELEMETRY")
        self.assertEqual(t, "oszczedny")

    def test_probki_nie_po_kolei(self):
        p = self.dir / "nie_po_kolei.json"
        teraz = time.time() * 1000
        # Probki w kolejnosci: wiek 10 min, wiek 1000 min, wiek 2 min (najnowsza)
        p.write_text(
            json.dumps({
                "version": 2,
                "samples": [
                    {"t": teraz - 10 * 60000, "org": "x", "u": {"fh": 50, "sd": 75}},
                    {"t": teraz - 1000 * 60000, "org": "x", "u": {"fh": 90, "sd": 90}},
                    {"t": teraz - 2 * 60000, "org": "x", "u": {"fh": 5, "sd": 20}},
                ],
            }),
            encoding="utf-8",
        )
        uc.SCIEZKA = p
        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "fresh")
        # Powinna zostac wybrana probka najnowsza chronologicznie (sd=20 -> normalny)
        self.assertEqual(stan["okno_7d_pct"], 20)
        self.assertEqual(t, "normalny")

    def test_reset_okna_spadek_sd_oraz_tempo(self):
        # Spadek sd > 15 (90 -> 5) wskazuje reset okna
        stan, _ = self._tryb([(5000, 0, 90), (4900, 0, 5), (10, 6, 40)])
        self.assertEqual(stan["freshness"], "fresh")
        self.assertIn("cykl_start", stan)
        self.assertIn("tempo", stan)

    def test_bez_twierdzen_o_dokupionych_kredytach_bez_deklaracji(self):
        stan, t = self._tryb([(5, 10, 95)])
        self.assertEqual(t, "kredytowy")
        # Dyrektywa nie moze twierdzic o dokupionych kredytach bez dowodu
        self.assertNotIn("DOKUPIONYCH KREDYTACH", uc.TRYBY["kredytowy"][1])
        self.assertNotIn("dokupionych kredytach", uc.TRYBY["kredytowy"][1].lower())

    def test_deklaracja_wlasciciela_kredyty(self):
        jutro = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
        uc.SCIEZKA_ROZLICZENIE.write_text(
            json.dumps({"claude": {"tryb": "kredytowy", "wazne_do": jutro, "powod": "dokupione kredyty"}}),
            encoding="utf-8",
        )
        stan, t = self._tryb([(5, 10, 20)])
        self.assertEqual(t, "kredytowy")
        self.assertIn("dokupione kredyty", stan["deklaracja"])

    def test_deklaracja_niepoprawny_typ_danych(self):
        uc.SCIEZKA_ROZLICZENIE.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        stan, t = self._tryb([(5, 10, 20)])
        self.assertFalse(stan["owner_declaration"]["valid"])
        self.assertIn("blad", stan["owner_declaration"])

    def test_json_struktura_pol_i_brak_innych_dostawcow(self):
        uc.SCIEZKA = _historia(self.dir, [(5, 10, 20)])
        argv = sys.argv
        sys.argv = ["usage_check.py", "--json"]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                kod = uc.main()
        finally:
            sys.argv = argv

        self.assertEqual(kod, 0)
        wynik = json.loads(buf.getvalue())
        # Wymagane klucze wg SPEC U1
        self.assertIn("provider", wynik)
        self.assertEqual(wynik["provider"], "claude")
        self.assertIn("source", wynik)
        self.assertIn("observed_at", wynik)
        self.assertIn("age_minutes", wynik)
        self.assertIn("freshness", wynik)
        self.assertEqual(wynik["freshness"], "fresh")
        self.assertIn("owner_declaration", wynik)
        self.assertIsInstance(wynik["owner_declaration"], dict)
        self.assertIn("tryb", wynik["owner_declaration"])
        self.assertIn("wazne_do", wynik["owner_declaration"])
        self.assertIn("czy_wazna", wynik["owner_declaration"])
        self.assertIn("mode", wynik)
        self.assertEqual(wynik["mode"], "normalny")

        # Zero wnioskow o Codexie i innych dostawcach
        for k in ("codex", "qwen", "kimi", "grok", "antigravity", "openai"):
            self.assertNotIn(k, wynik)

    def test_main_quiet_exit_code_dla_normalny(self):
        uc.SCIEZKA = _historia(self.dir, [(5, 10, 20)])
        argv = sys.argv
        sys.argv = ["usage_check.py", "--quiet"]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                kod = uc.main()
        finally:
            sys.argv = argv
        self.assertEqual(kod, 0)
        self.assertEqual(buf.getvalue().strip(), "normalny")

    def test_main_quiet_exit_code_dla_stale(self):
        uc.SCIEZKA = _historia(self.dir, [(2900, 6, 89)])
        argv = sys.argv
        sys.argv = ["usage_check.py", "--quiet"]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                kod = uc.main()
        finally:
            sys.argv = argv
        # Stale telemetry daje tryb oszczedny, wiec waga = 1
        self.assertEqual(kod, 1)
        self.assertEqual(buf.getvalue().strip(), "oszczedny")

    def test_main_quiet_exit_code_dla_kredytowy(self):
        uc.SCIEZKA = _historia(self.dir, [(5, 10, 95)])
        argv = sys.argv
        sys.argv = ["usage_check.py", "--quiet"]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                kod = uc.main()
        finally:
            sys.argv = argv
        # Kredytowy ma wage 2
        self.assertEqual(kod, 2)
        self.assertEqual(buf.getvalue().strip(), "kredytowy")

    # =========================================================================
    # Testy Fala 2 (V4): get_usage ingest, atomowy zapis, wybor zrodla
    # =========================================================================

    def test_ingest_pelnego_wyniku_sukces(self):
        raw = json.dumps({
            "status": "ok",
            "plan": "Max",
            "windows": [
                {"label": "5-hour limit", "percentUsed": 27, "resetsAt": "2026-10-21T20:00:00.403Z"},
                {"label": "Weekly · all models", "percentUsed": 68, "resetsAt": "2026-10-25T14:00:00.403Z"},
                {"label": "Weekly · Fable", "percentUsed": 59, "resetsAt": "2026-10-25T14:00:00.404Z"},
            ],
            "extraUsage": {"enabled": False, "spent": "0.00", "currency": "EUR"},
        })
        target = self.dir / "target-claude-plan-usage.json"
        kod, payload = uc.ingest(
            raw_input=raw,
            observed_at="2026-10-21T18:00:00Z",
            target_path=target,
        )
        self.assertEqual(kod, 0)
        self.assertTrue(target.exists())
        saved = json.loads(target.read_text(encoding="utf-8"))

        self.assertIn("schema", saved)
        self.assertEqual(saved["provider"], "claude")
        self.assertEqual(saved["source"], "ccd_get_usage")
        self.assertEqual(saved["observed_at"], "2026-10-21T18:00:00Z")
        self.assertEqual(saved["five_hour_pct"], 27)
        self.assertEqual(saved["weekly_all_pct"], 68)
        self.assertEqual(saved["weekly_by_model"], {"Weekly · Fable": 59})
        self.assertEqual(saved["resets"]["5-hour limit"], "2026-10-21T20:00:00.403Z")
        self.assertEqual(saved["resets"]["Weekly · all models"], "2026-10-25T14:00:00.403Z")
        self.assertEqual(saved["resets"]["Weekly · Fable"], "2026-10-25T14:00:00.404Z")
        self.assertFalse(saved["extra_usage_enabled"])
        self.assertEqual(saved["extra_usage_spent"], "0.00")
        self.assertEqual(saved["currency"], "EUR")

    def test_ingest_samego_obiektu_plan_sukces(self):
        # Wejscie zawiera sam obiekt plan
        raw = json.dumps({
            "plan": {
                "status": "ok",
                "plan": "Max",
                "windows": [
                    {"label": "5h", "percentUsed": 15, "resetsAt": "2026-10-21T20:00:00Z"},
                    {"label": "Weekly all models", "percentUsed": 30, "resetsAt": "2026-10-25T14:00:00Z"},
                ],
                "extraUsage": {"enabled": True, "spent": "5.50", "currency": "USD"},
            }
        })
        target = self.dir / "target-plan-only.json"
        kod, payload = uc.ingest(raw_input=raw, target_path=target)
        self.assertEqual(kod, 0)
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(saved["five_hour_pct"], 15)
        self.assertEqual(saved["weekly_all_pct"], 30)
        self.assertTrue(saved["extra_usage_enabled"])
        self.assertEqual(saved["extra_usage_spent"], "5.50")
        self.assertEqual(saved["currency"], "USD")

    def test_ingest_odmowy_status_not_applicable(self):
        raw = json.dumps({"status": "not_applicable", "note": "Plan does not support telemetry"})
        target = self.dir / "target-not-applicable.json"
        kod, payload = uc.ingest(raw_input=raw, target_path=target)
        self.assertEqual(kod, 2)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "not_applicable")
        self.assertFalse(target.exists())

    def test_ingest_odmowy_status_unavailable(self):
        raw = json.dumps({"status": "unavailable", "note": "Service busy"})
        target = self.dir / "target-unavailable.json"
        kod, payload = uc.ingest(raw_input=raw, target_path=target)
        self.assertEqual(kod, 2)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["code"], "unavailable")
        self.assertFalse(target.exists())

    def test_ingest_odmowy_brak_okna_5h(self):
        raw = json.dumps({
            "status": "ok",
            "windows": [
                {"label": "Weekly · all models", "percentUsed": 50},
            ],
        })
        target = self.dir / "target-no-5h.json"
        kod, payload = uc.ingest(raw_input=raw, target_path=target)
        self.assertEqual(kod, 2)
        self.assertEqual(payload["code"], "missing_window")
        self.assertFalse(target.exists())

    def test_ingest_odmowy_brak_okna_weekly_all(self):
        raw = json.dumps({
            "status": "ok",
            "windows": [
                {"label": "5-hour limit", "percentUsed": 20},
            ],
        })
        target = self.dir / "target-no-weekly.json"
        kod, payload = uc.ingest(raw_input=raw, target_path=target)
        self.assertEqual(kod, 2)
        self.assertEqual(payload["code"], "missing_window")
        self.assertFalse(target.exists())

    def test_ingest_odmowy_zly_procent_ponad_sto(self):
        raw = json.dumps({
            "status": "ok",
            "windows": [
                {"label": "5-hour limit", "percentUsed": 105},
                {"label": "Weekly · all models", "percentUsed": 50},
            ],
        })
        target = self.dir / "target-pct-over100.json"
        kod, payload = uc.ingest(raw_input=raw, target_path=target)
        self.assertEqual(kod, 2)
        self.assertEqual(payload["code"], "invalid_percent")
        self.assertFalse(target.exists())

    def test_ingest_odmowy_zly_procent_ujemny(self):
        raw = json.dumps({
            "status": "ok",
            "windows": [
                {"label": "5-hour limit", "percentUsed": -1},
                {"label": "Weekly · all models", "percentUsed": 50},
            ],
        })
        target = self.dir / "target-pct-negative.json"
        kod, payload = uc.ingest(raw_input=raw, target_path=target)
        self.assertEqual(kod, 2)
        self.assertEqual(payload["code"], "invalid_percent")
        self.assertFalse(target.exists())

    def test_ingest_odmowy_zly_procent_nie_liczba(self):
        for zly_pct in ("sto", True, None, float("nan")):
            raw = json.dumps({
                "status": "ok",
                "windows": [
                    {"label": "5-hour limit", "percentUsed": zly_pct},
                    {"label": "Weekly · all models", "percentUsed": 50},
                ],
            })
            target = self.dir / f"target-pct-{type(zly_pct).__name__}.json"
            kod, payload = uc.ingest(raw_input=raw, target_path=target)
            self.assertEqual(kod, 2)
            self.assertEqual(payload["code"], "invalid_percent")
            self.assertFalse(target.exists())

    def test_ingest_odmowy_zly_json(self):
        target = self.dir / "target-zly-json.json"
        kod, payload = uc.ingest(raw_input="{zly json:", target_path=target)
        self.assertEqual(kod, 2)
        self.assertEqual(payload["code"], "invalid_json")
        self.assertFalse(target.exists())

    def test_ingest_cli_subkomenda_sukces_i_odmowa(self):
        raw_ok = json.dumps({
            "status": "ok",
            "windows": [
                {"label": "5-hour limit", "percentUsed": 20},
                {"label": "Weekly · all models", "percentUsed": 40},
            ],
            "extraUsage": {"enabled": False},
        })
        target = self.dir / "cli-target.json"
        uc.SCIEZKA_INGEST = target

        # Sukces przez main() ingest
        argv = sys.argv
        stdin = sys.stdin
        sys.argv = ["usage_check.py", "ingest", "--observed-at", "2026-10-21T19:00:00Z"]
        sys.stdin = io.StringIO(raw_ok)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                kod = uc.main()
        finally:
            sys.argv = argv
            sys.stdin = stdin

        self.assertEqual(kod, 0)
        self.assertTrue(target.exists())
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["five_hour_pct"], 20)

        # Odmowa przez main() ingest (status unavailable)
        raw_bad = json.dumps({"status": "unavailable"})
        argv = sys.argv
        stdin = sys.stdin
        sys.argv = ["usage_check.py", "ingest"]
        sys.stdin = io.StringIO(raw_bad)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                kod_err = uc.main()
        finally:
            sys.argv = argv
            sys.stdin = stdin

        self.assertEqual(kod_err, 2)
        payload_err = json.loads(buf.getvalue())
        self.assertEqual(payload_err["code"], "unavailable")

    def test_wybor_swiezszego_zrodla_ingest_wygrywa(self):
        # Historia ma 30 min, ingest ma 5 min -> wygrywa ingest
        _historia(self.dir, [(30, 5, 20)])
        uc.SCIEZKA = self.dir / "plan-usage-history.json"
        p_ingest = _ingest_plik(self.dir, wiek_min=5, fh=27, sd=68, extra_enabled=False)
        uc.SCIEZKA_INGEST = p_ingest

        stan = uc.odczyt()
        self.assertEqual(stan["source_kind"], "ccd_get_usage")
        self.assertEqual(stan["source"], str(p_ingest))
        self.assertEqual(stan["okno_5h_pct"], 27)
        self.assertEqual(stan["okno_7d_pct"], 68)
        self.assertFalse(stan["extra_usage_enabled"])

    def test_wybor_swiezszego_zrodla_history_wygrywa(self):
        # Historia ma 5 min, ingest ma 30 min -> wygrywa historia
        _historia(self.dir, [(5, 10, 20)])
        uc.SCIEZKA = self.dir / "plan-usage-history.json"
        p_ingest = _ingest_plik(self.dir, wiek_min=30, fh=27, sd=68, extra_enabled=False)
        uc.SCIEZKA_INGEST = p_ingest

        stan = uc.odczyt()
        self.assertEqual(stan["source_kind"], "plan_usage_history")
        self.assertEqual(stan["source"], str(uc.SCIEZKA))
        self.assertEqual(stan["okno_5h_pct"], 10)
        self.assertEqual(stan["okno_7d_pct"], 20)
        self.assertIsNone(stan["extra_usage_enabled"])

    def test_stary_plik_history_plus_swiezy_ingest_daje_mode_z_ingest(self):
        # Historia sprzed 3 dni (2900 min, sd=89) -> sama dalaby stale -> oszczedny.
        # Swiezy ingest sprzed 5 min (sd=20, fh=10) -> daje normalny.
        _historia(self.dir, [(2900, 6, 89)])
        uc.SCIEZKA = self.dir / "plan-usage-history.json"
        p_ingest = _ingest_plik(self.dir, wiek_min=5, fh=10, sd=20, extra_enabled=False)
        uc.SCIEZKA_INGEST = p_ingest

        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "fresh")
        self.assertEqual(stan["source_kind"], "ccd_get_usage")
        self.assertEqual(stan["okno_7d_pct"], 20)
        self.assertEqual(t, "normalny")

    def test_extra_usage_false_brak_twierdzen_o_kredytach_w_tekscie(self):
        # Ingest z wysoka probka (sd=95 -> kredytowy), ale extra_usage_enabled=False
        p_ingest = _ingest_plik(self.dir, wiek_min=5, fh=10, sd=95, extra_enabled=False)
        uc.SCIEZKA_INGEST = p_ingest

        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(t, "kredytowy")
        self.assertFalse(stan["extra_usage_enabled"])

        # Sprawdzenie wyjscia tekstowego main()
        argv = sys.argv
        sys.argv = ["usage_check.py"]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                kod = uc.main()
        finally:
            sys.argv = argv

        output = buf.getvalue()
        self.assertIn("extra usage: wylaczone", output)
        self.assertNotIn("dokupionych kredytach", output.lower())
        self.assertNotIn("praca na kredytach", output.lower())
        self.assertNotIn("dokupionych kredytach", uc.TRYBY["kredytowy"][1].lower())

    def test_extra_usage_true_w_tekscie(self):
        p_ingest = _ingest_plik(self.dir, wiek_min=5, fh=10, sd=20, extra_enabled=True)
        uc.SCIEZKA_INGEST = p_ingest

        argv = sys.argv
        sys.argv = ["usage_check.py"]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                kod = uc.main()
        finally:
            sys.argv = argv

        output = buf.getvalue()
        self.assertIn("extra usage: wlaczone", output)

    def test_odczyt_ingest_przyszly_observed_at_daje_invalid(self):
        teraz_przyszlosc = time.time() + 3600  # 1h w przyszlosci
        dt = datetime.datetime.fromtimestamp(teraz_przyszlosc, tz=datetime.timezone.utc)
        obs_przyszly = dt.isoformat().replace("+00:00", "Z")

        p_ingest = _ingest_plik(self.dir, wiek_min=0, fh=10, sd=20, observed_at=obs_przyszly)
        uc.SCIEZKA_INGEST = p_ingest

        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "invalid")
        self.assertEqual(stan["powod"], "INVALID_TELEMETRY")
        self.assertEqual(t, "oszczedny")

    def test_odczyt_ingest_stale(self):
        p_ingest = _ingest_plik(self.dir, wiek_min=75, fh=10, sd=20)
        uc.SCIEZKA_INGEST = p_ingest

        stan = uc.odczyt()
        t = uc.tryb(stan)
        self.assertEqual(stan["freshness"], "stale")
        self.assertEqual(stan["powod"], "STALE_TELEMETRY")
        self.assertEqual(stan["pomiar"], "UNKNOWN")
        self.assertEqual(t, "oszczedny")

    def test_env_claude_plan_usage_path_w_odczycie(self):
        p_custom = self.dir / "custom-claude-plan-usage.json"
        _ingest_plik(self.dir, wiek_min=5, fh=12, sd=24)
        p_src = self.dir / "claude-plan-usage.json"
        p_src.rename(p_custom)

        os.environ["CLAUDE_PLAN_USAGE_PATH"] = str(p_custom)
        stan = uc.odczyt()
        self.assertEqual(stan["source"], str(p_custom))
        self.assertEqual(stan["source_kind"], "ccd_get_usage")
        self.assertEqual(stan["okno_5h_pct"], 12)
        self.assertEqual(stan["okno_7d_pct"], 24)

    def test_json_source_kind_oraz_extra_usage_dla_obu_zrodel(self):
        # 1. Sprawdzenie zrodla history
        uc.SCIEZKA = _historia(self.dir, [(5, 10, 20)])
        argv = sys.argv
        sys.argv = ["usage_check.py", "--json"]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                uc.main()
        finally:
            sys.argv = argv

        w_h = json.loads(buf.getvalue())
        self.assertEqual(w_h["source_kind"], "plan_usage_history")
        self.assertIsNone(w_h["extra_usage_enabled"])

        # 2. Sprawdzenie zrodla ingest
        p_ingest = _ingest_plik(self.dir, wiek_min=2, fh=15, sd=25, extra_enabled=False)
        uc.SCIEZKA_INGEST = p_ingest
        argv = sys.argv
        sys.argv = ["usage_check.py", "--json"]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                uc.main()
        finally:
            sys.argv = argv

        w_i = json.loads(buf.getvalue())
        self.assertEqual(w_i["source_kind"], "ccd_get_usage")
        self.assertFalse(w_i["extra_usage_enabled"])


if __name__ == "__main__":
    unittest.main()
