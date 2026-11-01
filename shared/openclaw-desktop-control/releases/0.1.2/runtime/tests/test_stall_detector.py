"""Testy jednostkowe dla modułu runtime/stall_detector.py.

Uruchamianie:
  python -m unittest discover -s runtime/tests -t .
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.stall_detector import (
    STATE_ACTIVE,
    STATE_COMPLETED,
    STATE_STALLED,
    STATE_WAITING_DECISION,
    StallDetectionResult,
    StallDetector,
    detect_stall,
)


class TestStallDetector(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.session_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_session_file(
        self,
        records: list[dict],
        filename: str = "test-session.jsonl",
        mtime: float | None = None,
    ) -> Path:
        session_file = self.session_dir / filename
        with open(session_file, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if mtime is not None:
            os.utime(session_file, (mtime, mtime))
        return session_file

    def test_detect_stall_when_assistant_silent_longer_than_threshold(self) -> None:
        """Test dowodzi wykrycia zastoju: ostatnia wypowiedź asystenta i cisza dłuższa niż próg."""
        base_time = 1000.0
        records = [
            {"type": "user", "content": "Zbuduj moduł", "timestamp": base_time},
            {
                "type": "assistant",
                "content": "Rozpoczynam implementację modułu...",
                "timestamp": base_time + 10.0,
            },
        ]
        session_file = self._create_session_file(records, mtime=base_time + 10.0)

        # Cisza: now=1130.0, ostatnia aktywność=1010.0 -> cisza 120s przy progu 60s
        result = detect_stall(session_file, threshold_seconds=60.0, now=base_time + 130.0)

        self.assertEqual(result.stan, STATE_STALLED)
        self.assertEqual(result.state, STATE_STALLED)
        self.assertEqual(result["stan"], STATE_STALLED)
        self.assertAlmostEqual(result.czas_ciszy, 120.0, places=1)
        self.assertIn("zastój", result.uzasadnienie.lower())
        self.assertIn("120.0s", result.uzasadnienie)

    def test_waiting_for_decision_when_assistant_asks_question(self) -> None:
        """Test dowodzi, że wątek z pytaniem do właściciela jest zwracany jako czeka-na-decyzje, a nie jako zastój."""
        base_time = 1000.0
        now_time = base_time + 500.0  # cisza 480s (dużo powyżej progu 60s)

        test_cases = [
            # 1. Pytanie kończące się znakiem zapytania
            [
                {"type": "user", "content": "Zrób porządki w plikach", "timestamp": base_time},
                {
                    "type": "assistant",
                    "content": "Znalazłem 3 zbędne pliki. Czy wyrażasz zgodę na ich usunięcie?",
                    "timestamp": base_time + 20.0,
                },
            ],
            # 2. Pytanie z wyborem wariantu
            [
                {"type": "user", "content": "Wybierz architekturę", "timestamp": base_time},
                {
                    "type": "assistant",
                    "content": "Mamy opcję A (szybka) i B (dokładna). Którą opcję wybierasz?",
                    "timestamp": base_time + 20.0,
                },
            ],
            # 3. Jawna prośba o decyzję / potwierdzenie
            [
                {"type": "user", "content": "Wdróż zmiany", "timestamp": base_time},
                {
                    "type": "assistant",
                    "content": "Kod gotowy. Proszę o decyzję i potwierdzenie przed wdrożeniem.",
                    "timestamp": base_time + 20.0,
                },
            ],
            # 4. Wywołanie narzędzia ask_question / ask_user
            [
                {"type": "user", "content": "Sprawdź konfigurację", "timestamp": base_time},
                {
                    "type": "assistant",
                    "content": [
                        {"type": "text", "text": "Wykryłem niejednoznaczność."},
                        {
                            "type": "tool_use",
                            "name": "ask_question",
                            "input": {"question": "Czy chcesz kontynuować?"},
                        },
                    ],
                    "timestamp": base_time + 20.0,
                },
            ],
            # 5. Jawna flaga waiting_for_input / approval_wait
            [
                {"type": "user", "content": "Wykonaj operację", "timestamp": base_time},
                {
                    "type": "assistant",
                    "content": "Wymagana autoryzacja.",
                    "waiting_for_input": True,
                    "timestamp": base_time + 20.0,
                },
            ],
        ]

        for idx, records in enumerate(test_cases, start=1):
            with self.subTest(case=idx):
                session_file = self._create_session_file(
                    records, filename=f"question-session-{idx}.jsonl", mtime=base_time + 20.0
                )
                result = detect_stall(session_file, threshold_seconds=60.0, now=now_time)

                self.assertEqual(
                    result.stan,
                    STATE_WAITING_DECISION,
                    f"Przypadek {idx} powinien zwrócić '{STATE_WAITING_DECISION}', a zwrócił '{result.stan}'",
                )
                self.assertNotEqual(result.stan, STATE_STALLED)
                self.assertEqual(result.state, STATE_WAITING_DECISION)
                self.assertIn("decyzję", result.uzasadnienie.lower())

    def test_active_when_fresh_activity(self) -> None:
        """Test dowodzi, że wątek ze świeżym ruchem jest zwracany jako aktywny."""
        base_time = 1000.0

        # Scenariusz A: Asystent odpowiedział niedawno (cisza 15s < próg 60s)
        records_assistant_fresh = [
            {"type": "user", "content": "Pracuj dalej", "timestamp": base_time},
            {
                "type": "assistant",
                "content": "Tworzę plik stall_detector.py...",
                "timestamp": base_time + 10.0,
            },
        ]
        session_a = self._create_session_file(
            records_assistant_fresh, filename="fresh-assistant.jsonl", mtime=base_time + 10.0
        )
        result_a = detect_stall(session_a, threshold_seconds=60.0, now=base_time + 25.0)

        self.assertEqual(result_a.stan, STATE_ACTIVE)
        self.assertNotEqual(result_a.stan, STATE_STALLED)
        self.assertAlmostEqual(result_a.czas_ciszy, 15.0, places=1)
        self.assertIn("świeżym ruchem", result_a.uzasadnienie)

        # Scenariusz B: Ostatnim rekordem jest nowe polecenie użytkownika (model przetwarza)
        records_user_fresh = [
            {"type": "user", "content": "Krok 1", "timestamp": base_time},
            {"type": "assistant", "content": "Krok 1 gotowy.", "timestamp": base_time + 10.0},
            {"type": "user", "content": "Krok 2: Uruchom testy", "timestamp": base_time + 20.0},
        ]
        session_b = self._create_session_file(
            records_user_fresh, filename="fresh-user.jsonl", mtime=base_time + 20.0
        )
        # Nawet przy dłuższej ciszy (np. 150s po pytaniu użytkownika, gdy model myśli)
        result_b = detect_stall(session_b, threshold_seconds=60.0, now=base_time + 170.0)

        self.assertEqual(result_b.stan, STATE_ACTIVE)
        self.assertNotEqual(result_b.stan, STATE_STALLED)
        self.assertIn("polecenie użytkownika", result_b.uzasadnienie)

        # Scenariusz C: Pusta sesja
        session_empty = self._create_session_file([], filename="empty.jsonl", mtime=base_time)
        result_empty = detect_stall(session_empty, threshold_seconds=60.0, now=base_time + 30.0)
        self.assertEqual(result_empty.stan, STATE_ACTIVE)

    def test_threshold_is_parameter_and_changes_verdict(self) -> None:
        """Test dowodzi, że próg czasu jest parametrem i zmiana progu zmienia werdykt."""
        base_time = 1000.0
        records = [
            {"type": "user", "content": "Wykonaj zadanie", "timestamp": base_time},
            {
                "type": "assistant",
                "content": "Pobrałem dane wejściowe.",
                "timestamp": base_time + 10.0,
            },
        ]
        session_file = self._create_session_file(records, mtime=base_time + 10.0)

        # Stała cisza = 120s (ostatnia aktywność: 1010.0, now: 1130.0)
        simulated_now = base_time + 130.0

        # 1. Próg 60s -> cisza (120s) >= 60s -> ZASTÓJ
        result_low_threshold = detect_stall(
            session_file, threshold_seconds=60.0, now=simulated_now
        )
        self.assertEqual(result_low_threshold.stan, STATE_STALLED)

        # 2. Próg 300s -> cisza (120s) < 300s -> AKTYWNY
        result_high_threshold = detect_stall(
            session_file, threshold_seconds=300.0, now=simulated_now
        )
        self.assertEqual(result_high_threshold.stan, STATE_ACTIVE)

        # 3. Próg dokładnie równy ciszy (120.0s) -> ZASTÓJ
        result_exact = detect_stall(session_file, threshold_seconds=120.0, now=simulated_now)
        self.assertEqual(result_exact.stan, STATE_STALLED)

        # 4. Próg minimalnie wyższy niż cisza (120.1s) -> AKTYWNY
        result_above = detect_stall(session_file, threshold_seconds=120.1, now=simulated_now)
        self.assertEqual(result_above.stan, STATE_ACTIVE)

        # 5. Użycie parametru przez klasę StallDetector
        detector_short = StallDetector(default_threshold_seconds=60.0)
        detector_long = StallDetector(default_threshold_seconds=300.0)
        self.assertEqual(detector_short.detect(session_file, now=simulated_now).stan, STATE_STALLED)
        self.assertEqual(detector_long.detect(session_file, now=simulated_now).stan, STATE_ACTIVE)

    def test_module_does_not_write_to_session_directory(self) -> None:
        """Test dowodzi, że moduł nie zapisuje niczego do katalogu sesji (jest w 100% read-only)."""
        base_time = 1000.0
        records = [
            {"type": "user", "content": "Sprawdź pliki", "timestamp": base_time},
            {"type": "assistant", "content": "Analizuję...", "timestamp": base_time + 10.0},
        ]
        session_file = self._create_session_file(records, filename="readonly-test.jsonl", mtime=base_time + 10.0)

        # Zrzut stanu katalogu i pliku PRZED wywołaniami detektora
        def snapshot_dir(dir_path: Path) -> dict:
            files_meta = {}
            for p in dir_path.iterdir():
                stat = p.stat()
                with open(p, "rb") as f:
                    sha = hashlib.sha256(f.read()).hexdigest()
                files_meta[p.name] = {
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "sha256": sha,
                }
            return {
                "file_names": sorted([p.name for p in dir_path.iterdir()]),
                "files": files_meta,
            }

        snapshot_before = snapshot_dir(self.session_dir)

        # Wykonujemy wielokrotne sprawdzenia detektorem dla różnych progów i trybów
        detect_stall(session_file, threshold_seconds=30.0, now=base_time + 100.0)
        detect_stall(session_file, threshold_seconds=300.0, now=base_time + 100.0)
        detect_stall(self.session_dir, threshold_seconds=60.0, now=base_time + 100.0)

        detector = StallDetector(default_threshold_seconds=60.0)
        detector.detect(session_file, now=base_time + 100.0)
        detector(self.session_dir, now=base_time + 100.0)

        # Zrzut stanu katalogu i pliku PO wywołaniach detektora
        snapshot_after = snapshot_dir(self.session_dir)

        # Sprawdzenie nienaruszalności:
        self.assertEqual(
            snapshot_before["file_names"],
            snapshot_after["file_names"],
            "W katalogu sesji pojawiły się lub zniknęły pliki!",
        )
        self.assertEqual(
            snapshot_before["files"],
            snapshot_after["files"],
            "Pliki w katalogu sesji zostały zmodyfikowane!",
        )

    def test_completed_state_detection(self) -> None:
        """Test dowodzi wykrywania stanu zakonczony (goal completed / task completed)."""
        base_time = 1000.0
        now_time = base_time + 500.0

        # Scenariusz A: Wypowiedź asystenta stwierdzająca zakończenie zadania
        records_text = [
            {"type": "user", "content": "Zaimplementuj i zakończ", "timestamp": base_time},
            {
                "type": "assistant",
                "content": "Wszystkie kroki zostały wykonane. Zadanie zakończone.",
                "timestamp": base_time + 50.0,
            },
        ]
        session_a = self._create_session_file(records_text, filename="completed-text.jsonl", mtime=base_time + 50.0)
        result_a = detect_stall(session_a, threshold_seconds=60.0, now=now_time)
        self.assertEqual(result_a.stan, STATE_COMPLETED)
        self.assertNotEqual(result_a.stan, STATE_STALLED)

        # Scenariusz B: Zdarzenie task_complete w formacie Codex Desktop
        records_event = [
            {"type": "user", "content": "Zadanie", "timestamp": base_time},
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": "Gotowe."},
                "timestamp": base_time + 50.0,
            },
        ]
        session_b = self._create_session_file(records_event, filename="completed-event.jsonl", mtime=base_time + 50.0)
        result_b = detect_stall(session_b, threshold_seconds=60.0, now=now_time)
        self.assertEqual(result_b.stan, STATE_COMPLETED)
        self.assertNotEqual(result_b.stan, STATE_STALLED)

    def test_structured_result_protocol(self) -> None:
        """Test weryfikuje ustrukturyzowany wynik i dostęp przez atrybuty oraz klucze."""
        res = StallDetectionResult(
            stan="zastoj",
            czas_ciszy=125.5,
            uzasadnienie="Wykryto zastój.",
        )
        self.assertEqual(res.stan, "zastoj")
        self.assertEqual(res.state, "zastoj")
        self.assertEqual(res["stan"], "zastoj")
        self.assertEqual(res["state"], "zastoj")

        self.assertEqual(res.czas_ciszy, 125.5)
        self.assertEqual(res.silence_duration, 125.5)
        self.assertEqual(res.silence_seconds, 125.5)
        self.assertEqual(res["czas_ciszy"], 125.5)
        self.assertEqual(res["silence_duration"], 125.5)

        self.assertEqual(res.uzasadnienie, "Wykryto zastój.")
        self.assertEqual(res.reason, "Wykryto zastój.")
        self.assertEqual(res["uzasadnienie"], "Wykryto zastój.")
        self.assertEqual(res["reason"], "Wykryto zastój.")

        doc = res.to_dict()
        self.assertEqual(doc["stan"], "zastoj")
        self.assertEqual(doc["czas_ciszy"], 125.5)
        self.assertEqual(doc["uzasadnienie"], "Wykryto zastój.")

        self.assertIn("stan", res)
        self.assertIn("czas_ciszy", res)
        self.assertIn("uzasadnienie", res)
        self.assertEqual(res.get("stan"), "zastoj")
        self.assertIsNone(res.get("nieznany_klucz"))

    def test_trailing_user_question_remains_active(self) -> None:
        """Test dowodzi, że zapytanie użytkownika na końcu sesji pozostaje aktywne.

        Nawet jeśli wypowiedź właściciela kończy się znakiem '?' lub zawiera frazę decyzyjną/ukończenia,
        oraz nawet gdy upłynął czas dłuższy niż próg ciszy, sesja musi mieć stan 'aktywny',
        ponieważ model przetwarza polecenie (in-flight work), a nie oczekuje na decyzję właściciela.
        """
        base_time = 1000.0
        now_time = base_time + 400.0  # cisza 380s (dużo powyżej progu 60s)

        test_cases = [
            # 1. Pytanie właściciela kończące się '?'
            ("user-question-1.jsonl", "Czy możesz sprawdzić stan wdrożenia?"),
            # 2. Pytanie właściciela z wyborem wariantu
            ("user-question-2.jsonl", "Którą opcję wybierasz do optymalizacji bazy?"),
            # 3. Polecenie użytkownika zawierające frazę decyzyjną
            ("user-question-3.jsonl", "Proszę o potwierdzenie czy testy przeszły pomyślnie."),
            # 4. Polecenie użytkownika zawierające frazę 'daj znać czy'
            ("user-question-4.jsonl", "Daj znać czy wszystko gotowe."),
            # 5. Pytanie użytkownika zawierające frazę ukończenia
            ("user-question-5.jsonl", "Czy zadanie zostało zakończone?"),
            # 6. Polecenie użytkownika zawierające frazę ukończenia
            ("user-question-6.jsonl", "Zadanie zakończone, przejdź do kolejnego etapu."),
        ]

        for filename, user_prompt in test_cases:
            with self.subTest(prompt=user_prompt):
                records = [
                    {"type": "user", "content": "Start zadania", "timestamp": base_time},
                    {
                        "type": "assistant",
                        "content": "Rozpocząłem przetwarzanie.",
                        "timestamp": base_time + 10.0,
                    },
                    {
                        "type": "user",
                        "content": user_prompt,
                        "timestamp": base_time + 20.0,
                    },
                ]
                session_file = self._create_session_file(
                    records, filename=filename, mtime=base_time + 20.0
                )

                result = detect_stall(session_file, threshold_seconds=60.0, now=now_time)

                self.assertEqual(
                    result.stan,
                    STATE_ACTIVE,
                    f"Pytanie użytkownika '{user_prompt}' powinno zwrócić stan '{STATE_ACTIVE}', a zwróciło '{result.stan}'",
                )
                self.assertNotEqual(result.stan, STATE_WAITING_DECISION)
                self.assertNotEqual(result.stan, STATE_STALLED)
                self.assertNotEqual(result.stan, STATE_COMPLETED)
                self.assertIn("użytkownika", result.uzasadnienie.lower())


    def test_claude_desktop_nested_record_shape_with_silent_ask_user_question(self) -> None:
        """Test dowodzi wykrywania oczekiwania na decyzję w zagnieżdżonym formacie Claude Desktop
        przy użyciu narzędzia AskUserQuestion bez bloku tekstowego i bez pytajnika '?' w tekście.

        Taki stan NIE MOŻE stać się zastojem po upływie progu ciszy — musi być zwrócony
        jako czeka-na-decyzje do eskalacji dla właściciela.
        """
        base_time = 1000.0
        now_time = base_time + 500.0  # cisza 480s > próg 60s
        session_id = "11111111-1111-1111-1111-111111111111"
        local_id = "local_5f81e3e2-d6c5-469c-bbd0-801757608e18"

        records = [
            {
                "type": "metadata",
                "sessionId": session_id,
                "cwd": "C:\\Users\\Owner\\Projects\\Alpha",
                "localId": local_id,
            },
            {
                "type": "user",
                "sessionId": session_id,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Wdróż aplikację na serwer"}],
                },
                "localId": local_id,
                "timestamp": base_time,
            },
            {
                "type": "assistant",
                "sessionId": session_id,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01AskUserQuestion",
                            "name": "AskUserQuestion",
                            "input": {
                                "questions": [
                                    {
                                        "question": "Wybierz docelowe srodowisko wdrozeniowe",  # Bez '?' w tekście
                                        "options": ["staging", "production"],
                                    }
                                ]
                            },
                        }
                    ],
                },
                "localId": local_id,
                "timestamp": base_time + 20.0,
            },
        ]
        session_file = self._create_session_file(
            records, filename=f"{session_id}.jsonl", mtime=base_time + 20.0
        )
        result = detect_stall(session_file, threshold_seconds=60.0, now=now_time)

        self.assertEqual(result.stan, STATE_WAITING_DECISION)
        self.assertNotEqual(result.stan, STATE_STALLED)
        self.assertNotEqual(result.stan, STATE_COMPLETED)
        self.assertEqual(result.state, STATE_WAITING_DECISION)
        self.assertIn("decyzj", result.uzasadnienie.lower())

    def test_claude_desktop_nested_record_shape_with_questions_and_options(self) -> None:
        """Test dowodzi poprawnej obsługi zagnieżdżonego formatu Claude Desktop
        z narzędziem AskUserQuestion zawierającym rozbudowane opcje i tekst asystenta.
        """
        base_time = 1000.0
        now_time = base_time + 500.0
        session_id = "22222222-2222-2222-2222-222222222222"
        local_id = "local_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        records = [
            {
                "type": "user",
                "sessionId": session_id,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Skonfiguruj bazę danych"}],
                },
                "localId": local_id,
                "timestamp": base_time,
            },
            {
                "type": "assistant",
                "sessionId": session_id,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Przygotowałem dwie konfiguracje bazy."},
                        {
                            "type": "tool_use",
                            "id": "toolu_02AskUserQuestion",
                            "name": "AskUserQuestion",
                            "input": {
                                "questions": [
                                    {
                                        "question": "Który silnik bazy danych wybrać?",
                                        "header": "Baza danych",
                                        "options": [
                                            {"label": "PostgreSQL", "value": "postgres"},
                                            {"label": "SQLite", "value": "sqlite"},
                                        ],
                                    }
                                ]
                            },
                        },
                    ],
                },
                "localId": local_id,
                "timestamp": base_time + 20.0,
            },
        ]
        session_file = self._create_session_file(
            records, filename=f"{session_id}.jsonl", mtime=base_time + 20.0
        )
        result = detect_stall(session_file, threshold_seconds=60.0, now=now_time)

        self.assertEqual(result.stan, STATE_WAITING_DECISION)
        self.assertNotEqual(result.stan, STATE_STALLED)
        self.assertNotEqual(result.stan, STATE_COMPLETED)

    def test_decision_detection_precedes_completion_phrase(self) -> None:
        """Test dowodzi, że wykrywanie decyzji ma bezwzględny priorytet przed frazami ukończenia.

        Wypowiedź asystenta typu:
          'Zadanie zakończone na etapie 2 z 5. Czy mam kontynuować?'
        zawiera zarówno frazę z COMPLETION_PHRASES ('zadanie zakończone'), jak i pytanie
        decyzyjne do właściciela ('Czy mam kontynuować?').
        Taki wątek MUSI być zwrócony jako czeka-na-decyzje (do eskalacji), a NIE jako zakonczony.
        """
        base_time = 1000.0
        now_time = base_time + 500.0

        mixed_cases = [
            # 1. Zwykły format: string content
            [
                {"type": "user", "content": "Wykonaj zadanie wieloetapowe", "timestamp": base_time},
                {
                    "type": "assistant",
                    "content": "Zadanie zakończone na etapie 2 z 5. Czy mam kontynuować?",
                    "timestamp": base_time + 20.0,
                },
            ],
            # 2. Zagnieżdżony format Claude Desktop
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Wykonaj migrację"}],
                    },
                    "timestamp": base_time,
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": "Zadanie zakończone na etapie 2 z 5. Czy mam kontynuować?",
                            }
                        ],
                    },
                    "timestamp": base_time + 20.0,
                },
            ],
            # 3. Fraza ukończenia z prośbą o decyzję
            [
                {"type": "user", "content": "Wdróż moduł", "timestamp": base_time},
                {
                    "type": "assistant",
                    "content": "Wszystkie kroki zostały wykonane. Proszę o decyzję przed publikacją na produkcję.",
                    "timestamp": base_time + 20.0,
                },
            ],
            # 4. Narzędzie AskUserQuestion połączone ze stwierdzeniem o zakończeniu etapu
            [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Etap 1"}],
                    },
                    "timestamp": base_time,
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Zadanie zostało wykonane dla etapu 1."},
                            {
                                "type": "tool_use",
                                "name": "AskUserQuestion",
                                "input": {
                                    "questions": [
                                        {"question": "Czy rozpocząć etap 2?", "options": ["Tak", "Nie"]}
                                    ]
                                },
                            },
                        ],
                    },
                    "timestamp": base_time + 20.0,
                },
            ],
        ]

        for idx, records in enumerate(mixed_cases, start=1):
            with self.subTest(case=idx):
                session_file = self._create_session_file(
                    records, filename=f"mixed-decision-completion-{idx}.jsonl", mtime=base_time + 20.0
                )
                result = detect_stall(session_file, threshold_seconds=60.0, now=now_time)

                self.assertEqual(
                    result.stan,
                    STATE_WAITING_DECISION,
                    f"Przypadek {idx} powinien zwrócić '{STATE_WAITING_DECISION}', a zwrócił '{result.stan}'",
                )
                self.assertNotEqual(result.stan, STATE_COMPLETED)
                self.assertNotEqual(result.stan, STATE_STALLED)

    def test_partial_progress_and_negation_not_falsely_completed(self) -> None:
        """Test dowodzi, że częściowy postęp lub negacja nie są fałszywie klasyfikowane jako zakonczony.

        - 'Zadanie zakończone na etapie 2 z 5.' (brak pytania, model zamilkł) -> po przekroczeniu progu to ZASTÓJ.
        - 'Zadanie nie zostało zakończone z powodu błędu.' -> po przekroczeniu progu to ZASTÓJ.
        - 'Zadanie zakończone.' (rzeczywisty koniec) -> ZAKOŃCZONY.
        """
        base_time = 1000.0
        now_time = base_time + 500.0  # cisza 480s > próg 60s

        # Scenariusz A: Częściowy postęp bez pytania decyzyjnego, model się zatrzymał -> ZASTÓJ
        records_partial = [
            {"type": "user", "content": "Kroki 1-5", "timestamp": base_time},
            {
                "type": "assistant",
                "content": "Zadanie zakończone na etapie 2 z 5.",
                "timestamp": base_time + 20.0,
            },
        ]
        session_partial = self._create_session_file(
            records_partial, filename="partial-stage.jsonl", mtime=base_time + 20.0
        )
        result_partial = detect_stall(session_partial, threshold_seconds=60.0, now=now_time)
        self.assertEqual(result_partial.stan, STATE_STALLED)
        self.assertNotEqual(result_partial.stan, STATE_COMPLETED)

        # Scenariusz B: Negacja zakończenia ("nie zostało zakończone"), model się zatrzymał -> ZASTÓJ
        records_negated = [
            {"type": "user", "content": "Wykonaj", "timestamp": base_time},
            {
                "type": "assistant",
                "content": "Zadanie nie zostało zakończone, wystąpił błąd kompilacji.",
                "timestamp": base_time + 20.0,
            },
        ]
        session_negated = self._create_session_file(
            records_negated, filename="negated-completion.jsonl", mtime=base_time + 20.0
        )
        result_negated = detect_stall(session_negated, threshold_seconds=60.0, now=now_time)
        self.assertEqual(result_negated.stan, STATE_STALLED)
        self.assertNotEqual(result_negated.stan, STATE_COMPLETED)

        # Scenariusz C: Rzeczywiste zakończenie bez kwalifikatorów etapu -> ZAKOŃCZONY
        records_real = [
            {"type": "user", "content": "Wykonaj", "timestamp": base_time},
            {
                "type": "assistant",
                "content": "Wszystkie kroki zostały wykonane. Zadanie zakończone.",
                "timestamp": base_time + 20.0,
            },
        ]
        session_real = self._create_session_file(
            records_real, filename="real-completion.jsonl", mtime=base_time + 20.0
        )
        result_real = detect_stall(session_real, threshold_seconds=60.0, now=now_time)
        self.assertEqual(result_real.stan, STATE_COMPLETED)
        self.assertNotEqual(result_real.stan, STATE_STALLED)


if __name__ == "__main__":
    unittest.main()
