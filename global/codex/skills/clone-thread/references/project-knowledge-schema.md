# Standard wiedzy projektowej (`PROJECT_KNOWLEDGE.md` + `.agents/desktop-threads.json`)

Ten dokument definiuje kanoniczny format wiedzy projektowej zapisywanej bezpośrednio w folderze projektu na dysku `D:\` oraz rejestru wątków narzędzi Desktop obsługiwanych przez skill `clone-thread` (`/clone-thread`).

Przyklady nazw, sciezek i identyfikatorow ponizej sa syntetyczne.

## 1. Podział ról (zero duplikacji ze stackiem)

| Plik / system | Lokalizacja | Odpowiedzialność |
|---|---|---|
| `APP_PROFILE.md` | `<ProjectRoot>/APP_PROFILE.md` | Statyczna karta techniczna i operacyjna: stack, porty, serwer, ścieżka deployu, zakazy produkcyjne. |
| `PROJECT_KNOWLEDGE.md` | `<ProjectRoot>/PROJECT_KNOWLEDGE.md` | Żywa wiedza projektowo-biznesowa wyciągnięta z pracy z Mateuszem: podjęte decyzje, kontekst domenowy, historyczne problemy/pułapki oraz bieżący stan grup modułów (`### <Moduł>`). |
| `.agents/desktop-threads.json` | `<ProjectRoot>/.agents/desktop-threads.json` | Indeks watkow i diagnostyczne watermarki plikow. `tools_with_threads` obejmuje narzedzia Desktop z watkami; metadane koordynatora `agent_office` sa osobno w `desktops` i `threads`. Kimi obsluguje tylko rejestracje biezacego watku, bez adaptera transkrypcji. |
| `llm-wiki` | Vault (`~/.agents/knowledge-config.json`) | Przekrojowa pamięć sesji (`record-session`), handoffy produkcyjne (`record-handoff`) i wiedza międzyprojektowa. |

## 2. Struktura `PROJECT_KNOWLEDGE.md`

Plik znajduje sie w katalogu glownym projektu (`<ProjectRoot>/PROJECT_KNOWLEDGE.md`). Limit to **400 linii / 25 KiB UTF-8** wraz z koncowym znakiem nowej linii. Kompaktowanie usuwa najstarsze wiersze `SUPERSEDED`, potem najstarsze niechronione problemy i odtwarzalne punkty podsumowan. Chroni naglowki, aktywne decyzje i wiersze `[MANUAL]`. Jesli nie mozna zmiescic chronionej tresci, zwraca blad przed zapisem. Nie kasuje plikow ani nie tworzy archiwow.

Datowane wiersze tabel sekcji 2 i 4 sa zachowywane i scalane bez duplikatow, takze przy bezposredniej edycji bez `[MANUAL]`. `record` dodaje ten znacznik automatycznie. Sekcje 3 i 5 sa generowane ponownie; trwale ustalenia zapisuj w tabelach. Brief zawiera takze zachowane wiersze, bez decyzji oznaczonych `SUPERSEDED`.

Wymagane nagłówki H2 (`## 1.` .. `## 5.`):

```markdown
# PROJECT_KNOWLEDGE - <Nazwa Projektu>

> Kanoniczna pamięć projektowa dla wszystkich narzędzi Desktop (Claude, Codex, ZCode, Antigravity, Kimi).
> Projekt: `<ProjectRoot>` | Slug: `<project_slug>` | Ostatnia normalizacja: `<ISO-8601>`

## 1. Jak czytać (granice źródeł)
- `APP_PROFILE.md` — stack, infrastruktura, deploy i ograniczenia produkcyjne.
- `PROJECT_KNOWLEDGE.md` (ten plik) — decyzje właściciela (Mateusz), kontekst biznesowy/domenowy, historyczne problemy i stan modułów.
- `.agents/desktop-threads.json` — lista narzędzi Desktop posiadających wątki w tym projekcie (`tools_with_threads`) oraz rejestr wątków `<Projekt> - <Moduł>`.

## 2. Decyzje właściciela (Mateusz) i reguły biznesowe
| Data | Moduł / Wątek | Decyzja / Reguła | Kontekst / Źródło | Status |
|---|---|---|---|---|
| 2026-10-24 | Portfel (`codex:00000000`) | ... | ... | ACTIVE |

## 3. Kontekst biznesowy i domenowy
- Klient / Organizacja: ...
- Główne moduły i strumienie prac (`<Projekt> - <Moduł>`): ...
- Kluczowe ustalenia domenowe: ...

## 4. Historyczne problemy i pułapki
| Data | Moduł / Wątek | Objaw / Problem | Przyczyna / Rozwiązanie (czego unikać) |
|---|---|---|---|
| 2026-10-24 | CRM (`claude:c9cc2a34`) | ... | ... |

## 5. Stan modułów (<Moduł>)
### Portfel
- **Ostatni aktywny wątek**: `Example CRM - Portfel` (`codex:00000000-0000-4000-8000-000000000001`, aktualizacja `2026-10-24`)
- **Ostatnie ustalenia i postęp**:
  - ...
- **Otwarte tematy / następne kroki**:
  - ...
```

## 3. Schemat `.agents/desktop-threads.json` (v1)

Liczniki `desktops.<tool>.thread_count` odpowiadaja wpisom `threads` po uwzglednieniu biezacej sesji. Tozsamosc to `<tool>:<session_id>`; dwa watki o identycznym tytule nadal sa dwiema sesjami. Bez ID biezacy wpis ma `native_id: "active-session"`. Watermarki sa diagnostyczne; obecna wersja ponownie czyta transkrypcje w calosci.

```json
{
  "schema_version": 1,
  "project_name": "Example CRM",
  "project_slug": "example-crm",
  "project_root": "C:\\Example\\Projects\\ExampleCRM",
  "thread_prefix": "Example CRM",
  "updated_at": "2026-10-24T20:15:00+02:00",
  "tools_with_threads": [
    "antigravity",
    "claude",
    "codex",
    "zcode"
  ],
  "desktops": {
    "claude": {
      "present": true,
      "thread_count": 6,
      "last_thread_at": "2026-10-24T19:40:00+02:00"
    },
    "codex": {
      "present": true,
      "thread_count": 4,
      "last_thread_at": "2026-10-24T19:50:00+02:00"
    },
    "zcode": {
      "present": true,
      "thread_count": 1,
      "last_thread_at": "2026-10-24T12:15:00+02:00"
    },
    "antigravity": {
      "present": true,
      "thread_count": 2,
      "last_thread_at": "2026-10-23T22:10:00+02:00"
    },
    "kimi": {
      "present": false,
      "thread_count": 0,
      "last_thread_at": null
    },
    "agent_office": {
      "present": true,
      "thread_count": 1,
      "last_thread_at": "2026-10-24T20:15:00+02:00"
    }
  },
  "threads": [
    {
      "id": "codex:00000000-0000-4000-8000-000000000001",
      "desktop": "codex",
      "native_id": "00000000-0000-4000-8000-000000000001",
      "title": "Example CRM - Portfel",
      "module": "Portfel",
      "cwd": "C:\\Example\\Projects\\ExampleCRM",
      "store_path": "C:\\Example\\Profile\\.codex\\sessions\\2026\\10\\21\\rollout-2026-10-21T09-12-00-00000000-0000-4000-8000-000000000001.jsonl",
      "updated_at": "2026-10-24T18:20:00+02:00",
      "message_count": 42,
      "decisions_extracted": 5,
      "problems_extracted": 3,
      "watermark": {
        "last_scanned_mtime": 1792858800.0,
        "last_byte_offset": 184320,
        "head_sha256": "a1b2c3d4..."
      }
    }
  ]
}
```

## 4. Dwupoziomowa ekstrakcja (`Tier 1` vs `Tier 2`)

Podczas wywołania `/clone-thread "<Projekt> - <Moduł>"`:
1. **Tier 1 (Wątki docelowego modułu `<Projekt> - <Moduł>`)**:
   - Heurystyczna ekstrakcja decyzji, ustalen i problemow (do 10 decyzji i 10 problemow na watek) oraz skrot stanu w sekcji `### <Modul>`.
2. **Tier 2 (Wszystkie pozostałe wątki projektu `<Projekt> - *` oraz sesje w tym samym `cwd` projektu)**:
   - Heurystyczna ekstrakcja przekrojowa (do 4 decyzji i 4 problemow na watek) oraz skrotowy stan pozostalych modulow. Pojedynczy skan wnosi maksymalnie 25 decyzji i 20 problemow; nie jest pelna kopia rozmow.

## 5. Bezpieczeństwo i higiena ekstrakcji

- **Tylko odczyt danych sesji**: bazy SQLite (`.zcode`, `antigravity`) sa otwierane wylacznie przez URI `file:...?mode=ro`, a transkrypcje bez modyfikacji. Wyjatek metadanych: jawnie wskazana sesja Codexa moze otrzymac tytul przez `session_index.jsonl`. `status` nie zmienia zadnych plikow ani rejestracji.
- **Redakcja sekretow**: rozpoznawane wzorce tokenow API, hasel, naglowkow Bearer, kluczy prywatnych i ciagow polaczeniowych sa zastepowane przez `[REDACTED:<TYP>]` w wyciagnietej tresci. Wzorce nie gwarantuja wykrycia dowolnego sekretu; nie umieszczaj sekretow w nazwach projektu, modulu ani watku.
- **Filtr prompt-injection**: surowe wypowiedzi sa danymi niezaufanymi; znane wzorce dyrektyw sa odrzucane. Wpis `ACTIVE` opisuje zapisane ustalenie, nie udziela zgody ani uprawnien. Agent nadal ocenia zrodlo i stosuje aktualne granice zadania.
- **Wiazanie projektu**: brak rozpoznania katalogu zwraca blad bez zapisu, a `--project-root` wymaga istniejacego katalogu. Biezacy katalog nie jest domyslnym celem zapisu.
