# Prompty dla narzedzi desktop

Wstaw pelna sciezke projektu. Nie wklejaj sekretow ani zawartosci `.env`.

## Codex Desktop

```text
Uzyj skilla $security-audit-cycle dla projektu:
<PELNA_SCIEZKA>

Najpierw sprawdz status 14-dniowej bramki. Jezeli audyt jest wymagany, wykonaj
lokalny audyt read-only. Nie czytaj .env, kluczy, baz, logow, uploadow ani danych
klientow. Nie wysylaj kodu do chmury, nie instaluj zaleznosci, nie naprawiaj,
nie deployuj i nie zmieniaj projektu. Zweryfikuj recznie findings automatu.
Na koncu podaj audit_id, werdykt, liczby severity, date kolejnego audytu oraz
pelna sciezke do REPORT.md.
```

## Claude Code Desktop

```text
Pracujesz jako niezalezny audytor read-only. Uzyj skilla
security-audit-cycle dla:
<PELNA_SCIEZKA>

Write-set: brak. Zakazane: odczyt .env/sekretow/baz/logow/uploadow, internet,
instalacje, migracje, restarty, deploy i zmiany konfiguracji. Sprawdz auth/RBAC/
IDOR, walidacje wejscia, CORS/debug/public exposure, ryzykowne wykonanie komend,
path traversal, XSS, logowanie danych oraz recovery. Nie wypisuj wartosci
sekretow. Zapisz findings jako severity + plik:linia + dowod bez sekretu +
rekomendacja + pewnosc. Podaj ograniczenia i werdykt PASS/REVISE/NO-GO.
```

## Antigravity

```text
Wykonaj niezalezny audyt bezpieczenstwa tylko do odczytu dla:
<PELNA_SCIEZKA>

Najpierw zastosuj skill security-audit-cycle i jego 14-dniowa bramke.
Nie edytuj plikow i nie korzystaj z sieci. Nie otwieraj .env, kluczy, tokenow,
baz danych, logow, uploadow ani plikow klientow. Ocen warstwy frontend, API,
auth/uprawnienia, storage, ekspozycje, walidacje, logowanie i recovery. Wynik:
zakres, findings z severity i plik:linia, false positives, residual risk,
PASS/REVISE/NO-GO. Nie twierdz, ze brak findingow gwarantuje bezpieczenstwo.
```
