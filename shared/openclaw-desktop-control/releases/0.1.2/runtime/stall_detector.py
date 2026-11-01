"""Moduł wykrywający zatrzymany wątek w sesji (stall detector).

Moduł wyłącznie do ODCZYTU. Wykrywa wątek, który zatrzymał się bez blokera:
- 'aktywny': wątek ze świeżym ruchem (cisza < próg) lub oczekujący na model po poleceniu użytkownika.
- 'zastoj': ostatnim istotnym rekordem jest wypowiedź asystenta, brak nowego polecenia
  użytkownika, a plik nie rośnie od zadanego progu czasu (cisza >= próg).
- 'czeka-na-decyzje': asystent zadał pytanie lub oczekuje na decyzję właściciela.
  NIE jest zastojem i nie wolno go automatycznie popychać — wymaga eskalacji.
- 'zakonczony': zadanie zostało zrealizowane/zakończone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Optional, Union

STATE_ACTIVE = "aktywny"
STATE_STALLED = "zastoj"
STATE_WAITING_DECISION = "czeka-na-decyzje"
STATE_COMPLETED = "zakonczony"

VALID_STATES = (
    STATE_ACTIVE,
    STATE_STALLED,
    STATE_WAITING_DECISION,
    STATE_COMPLETED,
)

DECISION_TOOL_NAMES = {
    "ask_question",
    "ask_user",
    "ask",
    "confirm",
    "request_decision",
    "request_approval",
    "prompt_user",
    "user_input",
    "get_input",
    # Narzędzia Claude Desktop / Claude Code / Agent Fleet
    "askuserquestion",
    "ask_user_question",
    "askquestion",
    "askuser",
    "user_question",
    "userquestion",
    "ask_followup_question",
    "askfollowupquestion",
    "ask_follow_up_question",
}

DECISION_PHRASES = (
    "proszę o decyzję",
    "proszę o potwierdzenie",
    "proszę o informację",
    "czekam na decyzję",
    "czekam na potwierdzenie",
    "czekam na twoją decyzję",
    "czekam na twoje potwierdzenie",
    "daj znać, jak",
    "daj znać, czy",
    "daj znać, co",
    "daj znać co",
    "daj znać czy",
    "daj znać jak",
    "potrzebuję twojej decyzji",
    "potrzebuję twojej zgody",
    "wymaga twojej decyzji",
    "wymaga twojej zgody",
    "czy wyrażasz zgodę",
    "czy mam kontynuować",
    "czy mam przejść",
    "czy chcesz abym",
    "czy chcesz żebym",
    "czy chcesz, abym",
    "czy chcesz, żebym",
    "którą opcję wybierasz",
    "który wariant wybierasz",
    "który wariant wolisz",
    "wybierz opcję",
    "wybierz wariant",
    "jak wolisz",
    "co wybierasz",
    "please confirm",
    "waiting for your decision",
    "waiting for confirmation",
    "waiting for approval",
    "let me know how to proceed",
    "let me know if you would like",
    "should i proceed",
    "do you want me to",
    "would you like me to",
    "which option do you prefer",
)

COMPLETION_PHRASES = (
    "zadanie zakończone",
    "zadanie zostało zakończone",
    "zadanie zostało wykonane",
    "wszystkie kroki zostały wykonane",
    "prace zostały zakończone",
    "zakończono zadanie",
    "task completed",
    "goal completed",
    "finished all tasks",
    "all tasks completed",
)


@dataclass(frozen=True)
class StallDetectionResult:
    """Ustrukturyzowany wynik analizy stanu wątku."""

    stan: str
    czas_ciszy: float
    uzasadnienie: str

    @property
    def state(self) -> str:
        return self.stan

    @property
    def silence_duration(self) -> float:
        return self.czas_ciszy

    @property
    def silence_seconds(self) -> float:
        return self.czas_ciszy

    @property
    def reason(self) -> str:
        return self.uzasadnienie

    def to_dict(self) -> dict[str, Any]:
        return {
            "stan": self.stan,
            "czas_ciszy": self.czas_ciszy,
            "uzasadnienie": self.uzasadnienie,
            "state": self.state,
            "reason": self.reason,
        }

    def __getitem__(self, item: str) -> Any:
        if item in ("stan", "state"):
            return self.stan
        if item in ("czas_ciszy", "silence_duration", "silence_seconds"):
            return self.czas_ciszy
        if item in ("uzasadnienie", "reason"):
            return self.uzasadnienie
        raise KeyError(f"Invalid key: {item!r}")

    def __contains__(self, item: str) -> bool:
        return item in (
            "stan",
            "state",
            "czas_ciszy",
            "silence_duration",
            "silence_seconds",
            "uzasadnienie",
            "reason",
        )

    def get(self, item: str, default: Any = None) -> Any:
        try:
            return self[item]
        except KeyError:
            return default


def _parse_timestamp(val: Any) -> Optional[float]:
    if isinstance(val, (int, float)):
        if val > 1e11:
            return float(val) / 1000.0
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        try:
            num = float(val)
            if num > 1e11:
                return num / 1000.0
            return num
        except ValueError:
            pass
        try:
            iso_str = val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_str)
            return dt.timestamp()
        except Exception:
            pass
    return None


def _extract_record_timestamp(rec: dict) -> Optional[float]:
    for key in ("timestamp", "created_at", "createdAt", "time", "observed_at"):
        if key in rec:
            ts = _parse_timestamp(rec[key])
            if ts is not None:
                return ts
    msg = rec.get("message")
    if isinstance(msg, dict):
        for key in ("timestamp", "created_at", "createdAt", "time"):
            if key in msg:
                ts = _parse_timestamp(msg[key])
                if ts is not None:
                    return ts
    payload = rec.get("payload")
    if isinstance(payload, dict):
        for key in ("timestamp", "created_at", "createdAt", "time", "create_time"):
            if key in payload:
                ts = _parse_timestamp(payload[key])
                if ts is not None:
                    return ts
    return None


def _extract_tool_input_text(tool_input: Any) -> list[str]:
    parts: list[str] = []
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except Exception:
            if tool_input.strip():
                return [tool_input.strip()]
            return []

    if isinstance(tool_input, dict):
        # 1. Pole questions (Claude AskUserQuestion / OpenAI / standard)
        questions = tool_input.get("questions")
        if isinstance(questions, list):
            for q in questions:
                if isinstance(q, str):
                    if q.strip():
                        parts.append(q.strip())
                elif isinstance(q, dict):
                    for k in ("question", "text", "prompt", "title", "header"):
                        val = q.get(k)
                        if isinstance(val, str) and val.strip():
                            parts.append(val.strip())
                    options = q.get("options")
                    if isinstance(options, list):
                        for opt in options:
                            if isinstance(opt, str) and opt.strip():
                                parts.append(opt.strip())
                            elif isinstance(opt, dict):
                                for ok in ("label", "text", "value"):
                                    oval = opt.get(ok)
                                    if isinstance(oval, str) and oval.strip():
                                        parts.append(oval.strip())
        elif isinstance(questions, str) and questions.strip():
            parts.append(questions.strip())
        elif isinstance(questions, dict):
            for k in ("question", "text", "prompt", "title"):
                val = questions.get(k)
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())

        # 2. Bezpośrednie pola pytania / promptu
        for k in ("question", "prompt", "message", "text"):
            val = tool_input.get(k)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and item.strip():
                        parts.append(item.strip())
                    elif isinstance(item, dict):
                        for sub_k in ("question", "text", "label"):
                            sub_val = item.get(sub_k)
                            if isinstance(sub_val, str) and sub_val.strip():
                                parts.append(sub_val.strip())

    return parts


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if "text" in item and isinstance(item["text"], str):
                    parts.append(item["text"])
                elif item.get("type") == "text" and "text" in item:
                    parts.append(str(item["text"]))

                # Ekstrakcja treści z tool_use / tool_call (np. Claude AskUserQuestion input.questions)
                if item.get("type") in ("tool_use", "tool_call") or "input" in item or "arguments" in item:
                    tool_input = item.get("input") if item.get("input") is not None else item.get("arguments")
                    if tool_input is not None:
                        parts.extend(_extract_tool_input_text(tool_input))
        return "\n".join(parts)
    if isinstance(content, dict):
        parts = []
        if "text" in content and isinstance(content["text"], str):
            parts.append(content["text"])
        if content.get("type") in ("tool_use", "tool_call") or "input" in content or "arguments" in content:
            tool_input = content.get("input") if content.get("input") is not None else content.get("arguments")
            if tool_input is not None:
                parts.extend(_extract_tool_input_text(tool_input))
        if parts:
            return "\n".join(parts)
    return ""


def _extract_tool_names(rec: dict) -> list[str]:
    names: list[str] = []
    msg = rec.get("message")
    content = None
    if isinstance(msg, dict):
        content = msg.get("content")
    elif "content" in rec:
        content = rec.get("content")

    if isinstance(content, dict):
        content = [content]

    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in ("tool_use", "tool_call"):
                    name = item.get("name")
                    if name and isinstance(name, str):
                        names.append(name.lower())

    tool_calls = rec.get("tool_calls") or (
        msg.get("tool_calls") if isinstance(msg, dict) else None
    )
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict):
                func = tc.get("function")
                if isinstance(func, dict):
                    fn_name = func.get("name")
                    if fn_name and isinstance(fn_name, str):
                        names.append(fn_name.lower())
                elif "name" in tc and isinstance(tc["name"], str):
                    names.append(tc["name"].lower())

    payload = rec.get("payload")
    if isinstance(payload, dict):
        fn = payload.get("name")
        if fn and isinstance(fn, str):
            names.append(fn.lower())

    return names


def _classify_record(rec: dict) -> tuple[str, str]:
    if not isinstance(rec, dict):
        return ("other", "")

    rec_type = str(rec.get("type") or "")
    role = str(rec.get("role") or "")
    message = rec.get("message")
    if isinstance(message, dict) and not role:
        role = str(message.get("role") or "")

    if not role:
        if rec_type in ("user", "human"):
            role = "user"
        elif rec_type in ("assistant", "agent", "bot"):
            role = "assistant"
        elif rec_type == "system":
            role = "system"

    if rec_type == "event_msg":
        payload = rec.get("payload", {})
        if isinstance(payload, dict) and payload.get("type") == "task_complete":
            return ("completion", str(payload.get("last_agent_message") or ""))

    if role != "user" and (rec.get("task_complete") is True or rec.get("completed") is True):
        msg = rec.get("message")
        return ("completion", msg if isinstance(msg, str) else "")

    if rec_type in (
        "metadata",
        "queue-operation",
        "progress",
        "file-history-snapshot",
        "heartbeat",
        "ping",
        "system_information",
    ):
        return ("other", "")

    text = ""
    if isinstance(message, str):
        text = message
    elif isinstance(message, dict):
        text = _extract_text(message.get("content"))
    elif "content" in rec:
        text = _extract_text(rec["content"])
    elif "text" in rec:
        text = str(rec["text"])
    elif "prompt" in rec and role == "user":
        text = str(rec["prompt"])
    elif "result" in rec and role == "assistant":
        text = str(rec["result"])
    elif "payload" in rec and isinstance(rec["payload"], dict):
        p = rec["payload"]
        if p.get("type") == "message":
            role = role or str(p.get("role") or "")
            text = _extract_text(p.get("content"))

    if role in ("user", "human"):
        return ("user", text)
    if role in ("assistant", "agent", "bot"):
        return ("assistant", text)
    if role == "system":
        return ("system", text)

    return ("other", text)


def _check_waiting_for_decision(rec: dict, text: str) -> tuple[bool, str]:
    # 1. Sprawdzenie wywołań narzędzi interaktywnych
    tools = _extract_tool_names(rec)
    for tool in tools:
        if tool in DECISION_TOOL_NAMES:
            return (True, f"Wykryto narzędzie interakcji z użytkownikiem: {tool}")

    # 1b. Sprawdzenie bloków tool_use z parametrem pytań w input
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else rec.get("content")
    if isinstance(content, dict):
        content = [content]
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") in ("tool_use", "tool_call"):
                inp = item.get("input") if item.get("input") is not None else item.get("arguments")
                if isinstance(inp, dict) and ("questions" in inp or "question" in inp):
                    tool_name = item.get("name") or "tool"
                    return (True, f"Wykryto narzędzie interakcji ({tool_name}) z pytaniem do użytkownika")

    # 2. Jawne flagi w rekordzie
    if rec.get("waiting_for_input") is True:
        return (True, "Flaga waiting_for_input w rekordzie")
    if rec.get("state") == "waiting_for_input":
        return (True, "Stan waiting_for_input w rekordzie")
    if rec.get("approval_wait") is True:
        return (True, "Flaga approval_wait w rekordzie")
    if str(rec.get("stop_reason") or "").lower() in ("approval_wait", "ask_question", "askuserquestion"):
        return (True, f"stop_reason wskazuje oczekiwanie na decyzję: {rec.get('stop_reason')}")
    if isinstance(msg, dict):
        if msg.get("waiting_for_input") is True:
            return (True, "Flaga waiting_for_input w rekordzie message")
        if str(msg.get("stop_reason") or "").lower() in ("approval_wait", "ask_question", "askuserquestion"):
            return (True, f"stop_reason w message wskazuje oczekiwanie na decyzję: {msg.get('stop_reason')}")

    # 3. Wykrycie fraz oczekiwania na decyzję
    clean_text = text.strip()
    lower_text = clean_text.lower()
    for phrase in DECISION_PHRASES:
        if phrase in lower_text:
            return (True, f"Wykryto frazę oczekiwania na decyzję: '{phrase}'")

    # 4. Wykrycie pytań w wypowiedzi
    if clean_text:
        trimmed = re.sub(r'[\s\'"\`\*\_\)\>\#]+$', '', clean_text)
        if trimmed.endswith("?"):
            return (True, "Ostatnia wypowiedź asystenta kończy się pytaniem do właściciela")

        lines = [line.strip() for line in clean_text.splitlines() if line.strip()]
        if lines:
            last_line = lines[-1]
            trimmed_line = re.sub(r'[\s\'"\`\*\_\)\>\#]+$', '', last_line)
            if trimmed_line.endswith("?"):
                return (True, "Ostatnia linia wypowiedzi asystenta kończy się pytaniem do właściciela")
            if "?" in last_line and len(last_line) < 200:
                return (True, "Ostatnia linia wypowiedzi asystenta zawiera pytanie do właściciela")

        for line in lines:
            trimmed_l = re.sub(r'[\s\'"\`\*\_\)\>\#]+$', '', line)
            if trimmed_l.endswith("?") and any(
                kw in line.lower()
                for kw in (
                    "czy", "jak", "co", "któr", "kiedy", "gdzie",
                    "wybierz", "preferujesz", "wolisz", "should", "would",
                    "do you", "which",
                )
            ):
                return (True, f"Wypowiedź asystenta zawiera pytanie decyzyjne do właściciela: '{line}'")

    return (False, "")


def _check_completion(rec: dict, text: str) -> tuple[bool, str]:
    if rec.get("task_complete") is True or rec.get("completed") is True:
        return (True, "Jawne oznaczenie ukończenia zadania w rekordzie")
    rec_type = str(rec.get("type") or "")
    if rec_type == "event_msg":
        p = rec.get("payload", {})
        if isinstance(p, dict) and p.get("type") == "task_complete":
            return (True, "Zdarzenie task_complete w dzienniku zdarzeń")

    clean_text = text.strip().lower()
    if not clean_text:
        return (False, "")

    # Sprawdzenie negacji ukończenia (np. "nie zostało zakończone")
    if re.search(r'\bnie\s+(?:został[oyea]?\s+|jest\s+)?(?:zakończon[oyea]?|wykonan[oyea]?)', clean_text):
        return (False, "")

    # Sprawdzenie częściowego postępu (np. "na etapie 2 z 5", "krok 1 z 3")
    if re.search(r'\b(?:na\s+etapie|etap|krok)\s+\d+\s*(?:z|/)\s*\d+', clean_text):
        return (False, "")

    for phrase in COMPLETION_PHRASES:
        pattern = rf'\b{re.escape(phrase)}\b'
        if re.search(pattern, clean_text):
            return (True, f"Wykryto frazę ukończenia zadania: '{phrase}'")

    return (False, "")


def detect_stall(
    session_path: Union[str, Path],
    threshold_seconds: Optional[float] = None,
    *,
    threshold: Optional[float] = None,
    prog: Optional[float] = None,
    prog_sekund: Optional[float] = None,
    now: Optional[float] = None,
) -> StallDetectionResult:
    """Wykrywa, czy wątek sesji zatrzymał się bez blokera.

    Operacja jest wyłącznie do ODCZYTU (nie modyfikuje ani nie tworzy plików).

    Parametry:
        session_path: Ścieżka do pliku .jsonl lub katalogu z sesjami.
        threshold_seconds: Próg czasu ciszy (w sekundach) kwalifikujący zastój.
        threshold: Alias dla threshold_seconds.
        prog: Alias dla threshold_seconds.
        prog_sekund: Alias dla threshold_seconds.
        now: Opcjonalne nadpisanie aktualnego czasu (do testów deterministycznych).

    Zwraca:
        StallDetectionResult ze stanem ('aktywny', 'zastoj', 'czeka-na-decyzje', 'zakonczony'),
        czasem ciszy (czas_ciszy) oraz uzasadnieniem.
    """
    thresh = threshold_seconds
    if thresh is None:
        thresh = threshold
    if thresh is None:
        thresh = prog_sekund
    if thresh is None:
        thresh = prog
    if thresh is None:
        thresh = 300.0
    thresh = float(thresh)

    current_time = time.time() if now is None else float(now)

    path = Path(session_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Ścieżka sesji nie istnieje: {path}")

    if path.is_dir():
        jsonl_files = sorted(
            [p for p in path.iterdir() if p.is_file() and p.suffix == ".jsonl"],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not jsonl_files:
            raise FileNotFoundError(f"Brak plików sesji .jsonl w katalogu: {path}")
        session_file = jsonl_files[0]
    else:
        session_file = path

    file_stat = session_file.stat()
    file_mtime = file_stat.st_mtime

    records: list[dict] = []
    with open(session_file, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                rec = json.loads(line_str)
                if isinstance(rec, dict):
                    records.append(rec)
            except json.JSONDecodeError:
                continue

    last_rec_ts: Optional[float] = None
    for rec in reversed(records):
        ts = _extract_record_timestamp(rec)
        if ts is not None:
            last_rec_ts = ts
            break

    if last_rec_ts is not None:
        if file_mtime > current_time:
            last_activity = last_rec_ts
        elif file_mtime - last_rec_ts > 10.0 and last_rec_ts <= current_time:
            last_activity = last_rec_ts
        else:
            last_activity = max(file_mtime, last_rec_ts)
    else:
        last_activity = file_mtime

    silence = max(0.0, current_time - last_activity)

    substantive_records: list[tuple[str, str, dict]] = []
    for rec in records:
        kind, text = _classify_record(rec)
        if kind in ("user", "assistant", "completion"):
            substantive_records.append((kind, text, rec))

    if not substantive_records:
        return StallDetectionResult(
            stan=STATE_ACTIVE,
            czas_ciszy=silence,
            uzasadnienie="Nowa lub pusta sesja; brak istotnych rekordów rozmowy.",
        )

    last_kind, last_text, last_rec = substantive_records[-1]

    # 1. Zdarzenie jawnego ukończenia sesji (completion)
    if last_kind == "completion":
        is_decision, decision_reason = _check_waiting_for_decision(last_rec, last_text)
        if is_decision:
            return StallDetectionResult(
                stan=STATE_WAITING_DECISION,
                czas_ciszy=silence,
                uzasadnienie=f"Wątek oczekuje na decyzję właściciela ({decision_reason}). Nie wolno automatycznie popychać.",
            )
        is_done, done_reason = _check_completion(last_rec, last_text)
        return StallDetectionResult(
            stan=STATE_COMPLETED,
            czas_ciszy=silence,
            uzasadnienie=f"Zadanie zostało zakończone: {done_reason or 'koniec zadania'}",
        )

    # 2. Ostatnim rekordem jest polecenie lub zapytanie użytkownika (przetwarzanie w toku).
    # Pytanie użytkownika (nawet kończące się '?' lub zawierające frazy decyzyjne/ukończenia)
    # oznacza trwające zadanie (in-flight work), a nie oczekiwanie na decyzję właściciela.
    if last_kind == "user":
        return StallDetectionResult(
            stan=STATE_ACTIVE,
            czas_ciszy=silence,
            uzasadnienie="Ostatnim rekordem jest polecenie użytkownika; sesja oczekuje na model lub przetwarza polecenie.",
        )

    # 3. Ostatnim rekordem jest odpowiedź asystenta (assistant)
    if last_kind == "assistant":
        # 3a. Wykrycie pytania do właściciela / oczekiwania na decyzję
        # KLUCZOWE: Sprawdzenie decyzji ma bezwzględny priorytet przed frazami ukończenia.
        # Wypowiedź asystenta zawierająca zarówno frazę ukończenia, jak i pytanie decyzyjne
        # (np. 'Zadanie zakończone na etapie 2 z 5. Czy mam kontynuować?')
        # oczekuje na decyzję właściciela (czeka-na-decyzje) i NIE MOŻE być uznana za zakończoną ani zastój.
        is_decision, decision_reason = _check_waiting_for_decision(last_rec, last_text)
        if is_decision:
            return StallDetectionResult(
                stan=STATE_WAITING_DECISION,
                czas_ciszy=silence,
                uzasadnienie=f"Wątek oczekuje na decyzję właściciela ({decision_reason}). Nie wolno automatycznie popychać.",
            )

        # 3b. Wykrycie stanu zakończenia zadania przez asystenta
        is_done, done_reason = _check_completion(last_rec, last_text)
        if is_done:
            return StallDetectionResult(
                stan=STATE_COMPLETED,
                czas_ciszy=silence,
                uzasadnienie=f"Zadanie zostało zakończone: {done_reason or 'koniec zadania'}",
            )

        # 3c. Ocena ciszy względem progu (zastój vs świeży ruch)
        if silence >= thresh:
            return StallDetectionResult(
                stan=STATE_STALLED,
                czas_ciszy=silence,
                uzasadnienie=(
                    f"Wykryto zastój: ostatnią wypowiedzią jest asystent, brak nowego polecenia użytkownika, "
                    f"a czas ciszy {silence:.1f}s przekracza próg {thresh:.1f}s."
                ),
            )
        return StallDetectionResult(
            stan=STATE_ACTIVE,
            czas_ciszy=silence,
            uzasadnienie=(
                f"Wątek ze świeżym ruchem: czas ciszy {silence:.1f}s nie przekracza progu {thresh:.1f}s."
            ),
        )

    return StallDetectionResult(
        stan=STATE_ACTIVE,
        czas_ciszy=silence,
        uzasadnienie="Wątek w stanie aktywnym.",
    )


class StallDetector:
    """Klasa wykrywająca zastój sesji w trybie tylko do odczytu."""

    def __init__(self, default_threshold_seconds: float = 300.0) -> None:
        self.default_threshold_seconds = float(default_threshold_seconds)

    def detect(
        self,
        session_path: Union[str, Path],
        threshold_seconds: Optional[float] = None,
        *,
        threshold: Optional[float] = None,
        prog: Optional[float] = None,
        prog_sekund: Optional[float] = None,
        now: Optional[float] = None,
    ) -> StallDetectionResult:
        thresh = threshold_seconds
        if thresh is None:
            thresh = threshold
        if thresh is None:
            thresh = prog_sekund
        if thresh is None:
            thresh = prog
        if thresh is None:
            thresh = self.default_threshold_seconds

        return detect_stall(
            session_path,
            threshold_seconds=thresh,
            now=now,
        )

    def __call__(
        self,
        session_path: Union[str, Path],
        threshold_seconds: Optional[float] = None,
        **kwargs: Any,
    ) -> StallDetectionResult:
        return self.detect(session_path, threshold_seconds=threshold_seconds, **kwargs)


__all__ = [
    "detect_stall",
    "StallDetector",
    "StallDetectionResult",
    "STATE_ACTIVE",
    "STATE_STALLED",
    "STATE_WAITING_DECISION",
    "STATE_COMPLETED",
    "VALID_STATES",
]
