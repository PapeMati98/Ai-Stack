#!/usr/bin/env python3
"""
Testy dziennego digestu dla Mateusza (docs/org-fala3-20261021/SPEC-O6.md).

Kazdy test dziala na syntetycznym katalogu w AGENT_TEST_ROOT (lub
~/.agents/test-runs/owner-digest) ustawionym jako AGENTS_HOME. Prawdziwe
~/.agents nigdy nie jest czytane ani zapisywane. Bez sieci, bez modeli, bez
procesow, bez usuwania plikow.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("owner_digest.py")
SPEC = importlib.util.spec_from_file_location("owner_digest", MODULE_PATH)
assert SPEC and SPEC.loader
od = importlib.util.module_from_spec(SPEC)
sys.modules["owner_digest"] = od
SPEC.loader.exec_module(od)

NOW = dt.datetime(2026, 10, 21, 21, 0, 0, tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------
# Fixture: syntetyczny katalog zrodel
# --------------------------------------------------------------------------


def _test_root() -> Path:
    """Katalog testowy poza repo (zgodnie z konwencja AGENT_TEST_ROOT)."""
    env = os.environ.get("AGENT_TEST_ROOT")
    base = Path(env) if env else Path.home() / ".agents" / "test-runs" / "owner-digest"
    base = base.resolve()
    repo = Path(__file__).resolve()
    for parent in repo.parents:
        if (parent / ".git").exists():
            if base == parent or base.is_relative_to(parent):
                raise pytest.UsageError("AGENT_TEST_ROOT musi byc poza repozytorium")
            break
    base.mkdir(parents=True, exist_ok=True)
    return base


@pytest.fixture
def home(monkeypatch):
    """Syntetyczny AGENTS_HOME (nigdy prawdziwy ~/.agents)."""
    root = Path(tempfile.mkdtemp(prefix="case-", dir=str(_test_root())))
    monkeypatch.setenv("AGENTS_HOME", str(root))
    return root


def write_json(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def write_text(path: Path, text: str, append: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if append:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def write_result(home: Path, task: str, run: str, payload: dict, when: dt.datetime = NOW) -> Path:
    """result.json z deterministycznym mtime (okno TODO jest liczone po mtime)."""
    path = home / "task-work" / task / run / "role" / "result.json"
    write_json(path, payload)
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def write_result_at(home: Path, relative: str, payload: dict, when: dt.datetime = NOW) -> Path:
    """result.json w dowolnym miejscu drzewa task-work (mtime = `when`)."""
    path = home / "task-work" / relative
    write_json(path, payload)
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))
    return path


def fleet_run(home: Path, name: str, **fields) -> Path:
    payload = {
        "run_id": name,
        "agent": "codex",
        "model": "gpt-x",
        "mode": "implement",
        "run_status": "COMPLETED",
        "completion_reason": "done",
        "duration_seconds": 60.0,
        "started_at": "2026-10-21T12:00:00Z",
    }
    payload.update(fields)
    return write_json(home / "agent-fleet" / "runs" / f"{name}.json", payload)


def ticket(home: Path, name: str, **fields) -> Path:
    payload = {
        "id": name,
        "status": "open",
        "component": "office",
        "symptom": "brak odpowiedzi",
        "count": 2,
        "first_seen": "2026-10-21T09:00:00Z",
        "last_seen": "2026-10-21T18:00:00Z",
    }
    payload.update(fields)
    return write_json(home / "helpdesk" / "tickets" / f"{name}.json", payload)


def build(home: Path, **kwargs):
    params = {"home": home, "since_hours": 24.0, "now": NOW}
    params.update(kwargs)
    return od.build_digest(**params)


# --------------------------------------------------------------------------
# Katalog zrodel
# --------------------------------------------------------------------------


def test_home_env_is_used_and_never_real_agents(home):
    real = Path.home() / ".agents"
    assert od.agents_home() == home
    assert home != real
    # katalog testowy jest syntetyczny: to nie jest prawdziwy ~/.agents
    assert home.is_dir()
    assert od.default_output_path(od.agents_home(), "2026-10-21").is_relative_to(home)


def test_all_five_sources_parsed(home):
    fleet_run(home, "run-1", agent="codex", model="gpt-x", duration_seconds=120.0)
    ticket(home, "t1", symptom="logowanie nie dziala", awaiting_owner=True)
    write_text(
        home / "owner-decisions" / "decisions.jsonl",
        json.dumps(
            {
                "ts": "2026-10-21T10:00:00Z",
                "project": "organizacja-agentow",
                "category": "technical",
                "question": "Czy lecimy z MVP?",
                "choice": "Tak",
            },
            ensure_ascii=False,
        )
        + "\n",
    )
    write_json(
        home / "usage" / "claude-plan-usage.json",
        {
            "observed_at": "2026-10-21T20:30:00Z",
            "five_hour_pct": 42,
            "weekly_all_pct": 30,
            "extra_usage_enabled": False,
        },
    )
    write_result(
        home,
        "zadanie-1",
        "run-abc",
        {"acceptance_status": "BLOCKED", "reason": "brak zgody wlasciciela"},
    )

    payload = build(home)
    sources = payload["sources"]
    assert sources["fleet"]["status"] == "ok"
    assert sources["fleet"]["total_runs"] == 1
    assert sources["helpdesk"]["status"] == "ok"
    assert len(sources["helpdesk"]["awaiting_owner"]) == 1
    assert sources["decisions"]["status"] == "ok"
    assert sources["decisions"]["items"][0]["choice"] == "Tak"
    assert sources["claude_limit"]["status"] == "ok"
    assert sources["claude_limit"]["five_hour_pct"] == 42
    assert sources["todo"]["status"] == "ok"
    assert sources["todo"]["counts"] == {"BLOCKED": 1}
    assert len(payload["decision_needed"]["todo_blocked"]) == 1


def test_missing_sources_give_no_data_sections_not_exceptions(home):
    payload = build(home)
    sources = payload["sources"]
    for name in ("fleet", "helpdesk", "decisions", "claude_limit", "todo"):
        assert sources[name]["status"] in ("missing", "no_data")
        assert sources[name]["reason"]
    markdown = od.render_markdown(payload)
    assert "brak danych:" in markdown
    assert "## Wymaga Twojej decyzji" in markdown


def test_missing_sources_expose_zero_totals(home):
    """Brak katalogu zrodla = zerowe liczniki, nie wyjatek (regresja: KeyError)."""
    payload = build(home)
    fleet = payload["sources"]["fleet"]
    helpdesk = payload["sources"]["helpdesk"]
    decisions = payload["sources"]["decisions"]
    todo = payload["sources"]["todo"]

    assert fleet["rows_total"] == 0
    assert fleet["failed_groups_total"] == 0
    assert helpdesk["open_total"] == 0
    assert helpdesk["new_total"] == 0
    assert helpdesk["needs_owner_total"] == 0
    assert helpdesk["awaiting_owner_total"] == 0
    assert decisions["items_total"] == 0
    assert todo["blocked_total"] == 0

    needed = payload["decision_needed"]
    assert needed["helpdesk_needs_owner_total"] == 0
    assert needed["helpdesk_awaiting_owner_total"] == 0
    assert needed["todo_blocked_total"] == 0
    assert needed["count"] == 0
    assert od.render_markdown(payload)


def test_invalid_json_files_are_skipped_gracefully(home):
    write_text(home / "agent-fleet" / "runs" / "run-bad.json", "{to nie json")
    write_text(home / "helpdesk" / "tickets" / "bad.json", "]]]")
    write_text(
        home / "owner-decisions" / "decisions.jsonl",
        "to nie json\n" + json.dumps({"ts": "2026-10-21T10:00:00Z", "question": "q", "choice": "c"}),
    )
    write_text(home / "usage" / "claude-plan-usage.json", "{nope")
    bad_result = write_text(home / "task-work" / "t" / "run-1" / "r" / "result.json", "nope")
    inside = (NOW - dt.timedelta(hours=1)).timestamp()
    os.utime(bad_result, (inside, inside))  # plik w oknie, ale z trescia nie-JSON

    payload = build(home)
    sources = payload["sources"]
    assert sources["fleet"]["skipped_files"] == 1
    assert sources["helpdesk"]["skipped_files"] == 1
    assert sources["decisions"]["skipped_lines"] == 1
    assert sources["decisions"]["items"]  # dobry wiersz przeszedl
    assert sources["claude_limit"]["status"] == "invalid"
    assert sources["todo"]["skipped_files"] == 1
    assert od.render_markdown(payload)


# --------------------------------------------------------------------------
# Okno czasowe
# --------------------------------------------------------------------------


def test_window_boundaries_are_inclusive(home):
    fleet_run(home, "run-in", started_at="2026-10-20T21:00:00Z")
    fleet_run(home, "run-out", started_at="2026-10-20T20:59:59Z")
    fleet_run(home, "run-end", started_at="2026-10-21T21:00:00Z")
    payload = build(home)
    rows = payload["sources"]["fleet"]["rows"]
    assert payload["sources"]["fleet"]["total_runs"] == 2
    assert len(rows) == 1
    assert rows[0]["runs"] == 2


def test_decisions_only_from_window(home):
    store = home / "owner-decisions" / "decisions.jsonl"
    lines = [
        {"ts": "2026-10-19T10:00:00Z", "question": "stare", "choice": "nie"},
        {"ts": "2026-10-21T10:00:00Z", "question": "nowe", "choice": "tak"},
    ]
    write_text(store, "\n".join(json.dumps(item, ensure_ascii=False) for item in lines))
    payload = build(home)
    items = payload["sources"]["decisions"]["items"]
    assert [item["question"] for item in items] == ["nowe"]


def test_todo_uses_mtime_in_window(home):
    result = home / "task-work" / "t" / "run-1" / "role" / "result.json"
    write_json(result, {"acceptance_status": "ACCEPTED"})
    outside = (NOW - dt.timedelta(days=5)).timestamp()
    os.utime(result, (outside, outside))
    assert build(home)["sources"]["todo"]["total"] == 0
    inside = (NOW - dt.timedelta(hours=1)).timestamp()
    os.utime(result, (inside, inside))
    assert build(home)["sources"]["todo"]["total"] == 1


def test_claude_limit_sample_stale_after_60_minutes(home):
    write_json(
        home / "usage" / "claude-plan-usage.json",
        {"observed_at": "2026-10-21T19:00:00Z", "five_hour_pct": 10, "weekly_all_pct": 20},
    )
    payload = build(home)
    limit = payload["sources"]["claude_limit"]
    assert limit["age_minutes"] == 120.0
    assert limit["stale"] is True
    assert "nieaktualna" in od.render_markdown(payload)

    write_json(
        home / "usage" / "claude-plan-usage.json",
        {"observed_at": "2026-10-21T20:30:00Z", "five_hour_pct": 10, "weekly_all_pct": 20},
    )
    payload = build(home)
    assert payload["sources"]["claude_limit"]["stale"] is False
    assert "nieaktualna" not in od.render_markdown(payload)


# --------------------------------------------------------------------------
# Kolejnosc sekcji i decyzje
# --------------------------------------------------------------------------


def test_decision_needed_section_is_first(home):
    fleet_run(home, "run-1")
    ticket(home, "t1", awaiting_owner=True, symptom="potrzebna decyzja")
    write_result(
        home, "t", "run-1", {"acceptance_status": "BLOCKED", "reason": "czeka na wlasciciela"}
    )
    markdown = od.render_markdown(build(home))
    head = markdown.index("## Wymaga Twojej decyzji")
    for other in ("## Fleet", "## Helpdesk RSI", "## Decyzje wlasciciela", "## Limit Claude", "## Zadania TODO"):
        assert head < markdown.index(other)
    assert "potrzebna decyzja" in markdown
    assert "czeka na wlasciciela" in markdown
    assert markdown.index("potrzebna decyzja") < markdown.index("## Fleet")


# --------------------------------------------------------------------------
# Maskowanie sekretow
# --------------------------------------------------------------------------


def test_secret_like_content_is_masked(home):
    secret = "sk" + "-" + "abcdefghijklmnop1234"
    vault = "op://vault/prod/api-key"
    ticket(
        home,
        "t1",
        symptom=f"awaria {secret} {vault} api_key=" + "ABCDEF" + "123456",
        component="office",
    )
    write_result(
        home,
        "t",
        "run-1",
        {"acceptance_status": "BLOCKED", "reason": f"password=hunter2 {secret}"},
    )
    payload = build(home)
    markdown = od.render_markdown(payload)
    blob = json.dumps(payload, ensure_ascii=False)
    for text in (markdown, blob):
        assert secret not in text
        assert vault not in text
        assert "ABCDEF123456" not in text
        assert "hunter2" not in text
        assert od.MASK in text
    assert "api_key=<ukryte>" in markdown


def test_control_characters_removed_and_text_truncated(home):
    long_text = "x" * 400
    ticket(home, "t1", symptom=f"a\x00b\x1fc {long_text}")
    payload = build(home)
    symptom = payload["sources"]["helpdesk"]["open"][0]["symptom"]
    assert "\x00" not in symptom
    assert "\x1f" not in symptom
    assert len(symptom) <= od.MAX_LEN
    assert symptom.endswith("...")


def test_missing_field_is_question_mark(home):
    fleet_run(home, "run-1", agent=None, model=None, duration_seconds=None)
    row = build(home)["sources"]["fleet"]["rows"][0]
    assert row["agent"] == "?"
    assert row["model"] == "?"
    assert row["minutes_display"] == "?"


# --------------------------------------------------------------------------
# Udzialy, czas, liczby
# --------------------------------------------------------------------------


def test_share_percentages_and_total_time(home):
    fleet_run(home, "run-a1", agent="codex", model="gpt-x", duration_seconds=60.0)
    fleet_run(home, "run-a2", agent="codex", model="gpt-x", duration_seconds=60.0)
    fleet_run(home, "run-b1", agent="claude", model="claude-x", duration_seconds=120.0)
    payload = build(home)
    rows = {row["agent"]: row for row in payload["sources"]["fleet"]["rows"]}
    assert rows["codex"]["share_pct"] == 66.7
    assert rows["claude"]["share_pct"] == 33.3
    assert rows["codex"]["minutes"] == 2.0
    assert rows["claude"]["minutes"] == 2.0
    assert payload["sources"]["fleet"]["total_runs"] == 3


def test_status_columns_count_correctly(home):
    fleet_run(home, "run-1", run_status="COMPLETED")
    fleet_run(home, "run-2", run_status="PARTIAL")
    fleet_run(home, "run-3", run_status="FAILED")
    fleet_run(home, "run-4", run_status="BLOCKED")
    row = build(home)["sources"]["fleet"]["rows"][0]
    assert row["completed"] == 1
    assert row["partial"] == 1
    assert row["failed_blocked"] == 2
    assert row["runs"] == 4


def test_todo_counts_per_status_and_blocked_list(home):
    write_result(home, "a", "run-1", {"acceptance_status": "ACCEPTED"})
    write_result(
        home,
        "b",
        "run-2",
        {"acceptance_status": "BLOCKED", "reason": "czeka na wlasciciela"},
    )
    todo = build(home)["sources"]["todo"]
    assert todo["counts"] == {"ACCEPTED": 1, "BLOCKED": 1}
    assert todo["blocked"][0]["run"] == str(Path("task-work") / "b" / "run-2")
    assert todo["blocked"][0]["reason"] == "czeka na wlasciciela"


# --------------------------------------------------------------------------
# Zapis atomowy
# --------------------------------------------------------------------------


def test_atomic_write_leaves_no_temporary_files(home):
    target = home / "digest" / "2026-10-21.md"
    text = "# Digest\n"
    od.write_atomic(target, text)
    assert target.read_text(encoding="utf-8") == text
    leftovers = [p.name for p in target.parent.iterdir() if p.name != target.name]
    assert leftovers == []


def test_atomic_write_replaces_content_in_place(home):
    target = home / "digest" / "2026-10-21.md"
    od.write_atomic(target, "stara tresc")
    od.write_atomic(target, "nowa tresc")
    assert target.read_text(encoding="utf-8") == "nowa tresc"
    assert len(list(target.parent.iterdir())) == 1


def test_default_output_path_under_agents_home(home):
    path = od.default_output_path(home, "2026-10-21")
    assert path == home / "digest" / "2026-10-21.md"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_build_writes_markdown_file(home, capsys):
    fleet_run(home, "run-1")
    code = od.main(["build", "--now", "2026-10-21T21:00:00Z", "--since-hours", "24"])
    captured = capsys.readouterr()
    assert code == 0
    out_file = home / "digest" / "2026-10-21.md"
    assert out_file.is_file()
    assert out_file.read_text(encoding="utf-8").startswith("# Digest dla Mateusza")
    assert "## Wymaga Twojej decyzji" in captured.out


def test_cli_json_matches_markdown(home, capsys):
    fleet_run(home, "run-1", agent="codex", model="gpt-x")
    ticket(home, "t1", symptom="problem X", awaiting_owner=True)
    out = home / "out" / "digest.md"
    assert od.main(["build", "--now", "2026-10-21T21:00:00Z", "--out", str(out), "--json"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    markdown = out.read_text(encoding="utf-8")

    assert payload["date"] == "2026-10-21"
    assert payload["sources"]["fleet"]["rows"][0]["agent"] == "codex"
    assert "codex" in markdown
    assert "problem X" in markdown
    assert payload["sources"]["fleet"]["total_runs"] == 1
    assert "Razem runow w oknie: 1." in markdown
    assert payload["decision_needed"]["count"] == 1
    assert payload["sources"]["helpdesk"]["awaiting_owner"][0]["symptom"] == "problem X"


def test_cli_rejects_bad_now(home, capsys):
    assert od.main(["build", "--now", "nie-data"]) == 2
    assert not (home / "digest").exists()


def test_cli_rejects_non_positive_window(home):
    assert od.main(["build", "--since-hours", "0"]) == 2


# --------------------------------------------------------------------------
# Znalezisko 1: `build --out` nie moze wywalic sie na konsoli cp1250
# --------------------------------------------------------------------------


class _Cp1250Stdout:
    """Udaje konsole Windows cp1250: brak `reconfigure`, wywala
    UnicodeEncodeError dla znakow spoza cp1250."""

    encoding = "cp1250"

    def __init__(self) -> None:
        self._buffer = io.StringIO()

    def write(self, text: str) -> int:
        text.encode(self.encoding)  # rzuca UnicodeEncodeError dla "→"
        return self._buffer.write(text)

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        return self._buffer.getvalue()


def test_cli_with_out_prints_only_path_not_markdown(home, monkeypatch):
    fleet_run(home, "run-1", agent="codex", model="gpt-x")
    ticket(home, "t1", symptom="problem X", awaiting_owner=True)
    out = home / "out" / "digest.md"
    fake = _Cp1250Stdout()
    monkeypatch.setattr(sys, "stdout", fake)

    code = od.main(["build", "--now", "2026-10-21T21:00:00Z", "--out", str(out)])

    assert code == 0
    printed = fake.getvalue()
    assert out.is_file()
    assert "zapisano:" in printed
    assert str(out) in printed
    # Tresci markdownu NIE moze trafic na stdout (to wlasnie wywalalo Pythona).
    assert "## Fleet" not in printed
    assert "problem X" not in printed


def test_cli_with_out_survives_non_cp1250_path(home, monkeypatch):
    """Znak spoza cp1250 w sciezce nie moze wywalic UnicodeEncodeError."""
    out = home / "out" / "katalog→" / "digest.md"
    fake = _Cp1250Stdout()
    monkeypatch.setattr(sys, "stdout", fake)

    code = od.main(["build", "--now", "2026-10-21T21:00:00Z", "--out", str(out)])

    assert code == 0
    assert out.is_file()
    printed = fake.getvalue()
    assert "zapisano:" in printed
    # Znak spoza cp1250 zostal zastapiony, ale nic nie wywalilo.
    assert "?" in printed


def test_safe_write_uses_reconfigure_when_available():
    class _Reconfigurable(io.StringIO):
        encoding = "cp1250"

        def __init__(self) -> None:
            super().__init__()
            self.calls = []

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)
            self.encoding = kwargs.get("encoding", self.encoding)

    stream = _Reconfigurable()
    od.safe_write(stream, "tekst\n")
    assert stream.getvalue() == "tekst\n"
    assert stream.calls and stream.calls[0].get("errors") == "replace"


# --------------------------------------------------------------------------
# Znalezisko 2: zgloszenia 'needs-owner' i kandydaci w "Wymaga Twojej decyzji"
# --------------------------------------------------------------------------


def test_needs_owner_ticket_uses_last_needs_owner_note(home):
    ticket(
        home,
        "t-needs",
        status="needs-owner",
        symptom="objaw",
        history=[
            {"status": "open", "note": "to nie jest pytanie"},
            {"status": "needs-owner", "note": "pierwsze pytanie"},
            {"status": "open", "note": "nadal nie pytanie"},
            {"status": "needs-owner", "note": "ostatnie pytanie"},
        ],
    )
    payload = build(home)
    needs = payload["sources"]["helpdesk"]["needs_owner"]
    assert len(needs) == 1
    assert needs[0]["question"] == "ostatnie pytanie"
    assert payload["decision_needed"]["helpdesk_needs_owner"][0]["id"] == "t-needs"


def test_needs_owner_question_is_masked_and_truncated(home):
    secret = "sk" + "-" + "abcdefghijklmnop1234"
    ticket(
        home,
        "t-needs",
        status="needs-owner",
        history=[{"status": "needs-owner", "note": "x" * 400 + " " + secret}],
    )
    payload = build(home)
    question = payload["sources"]["helpdesk"]["needs_owner"][0]["question"]
    assert secret not in question
    assert od.MASK in question
    assert len(question) <= od.MAX_LEN
    assert question.endswith("...")


def test_needs_owner_tickets_are_listed_first(home):
    ticket(
        home,
        "t-needs",
        status="needs-owner",
        history=[{"status": "needs-owner", "note": "pytanie wlasciciela"}],
    )
    ticket(home, "t-await", status="open", symptom="czekajace zgloszenie", awaiting_owner=True)
    payload = build(home)
    markdown = od.render_markdown(payload)
    assert "pytanie wlasciciela" in markdown
    assert markdown.index("pytanie wlasciciela") < markdown.index("czekajace zgloszenie")
    assert markdown.index("pytanie wlasciciela") < markdown.index("## Fleet")


def test_candidate_awaiting_owner_stays_and_is_labelled(home):
    ticket(
        home,
        "t-needs",
        status="needs-owner",
        history=[{"status": "needs-owner", "note": "pytanie A"}],
    )
    ticket(home, "t-cand", status="candidate", symptom="kandydat X", awaiting_owner=True)
    ticket(home, "t-open", status="open", symptom="zwykly Y", awaiting_owner=True)

    payload = build(home)
    awaiting = payload["sources"]["helpdesk"]["awaiting_owner"]
    by_id = {item["id"]: item for item in awaiting}
    assert "t-cand" in by_id
    assert by_id["t-cand"]["label"] == od.CANDIDATE_LABEL
    assert by_id["t-open"]["label"] == od.AWAITING_LABEL

    markdown = od.render_markdown(payload)
    assert od.CANDIDATE_LABEL in markdown
    assert markdown.index("pytanie A") < markdown.index("kandydat X")
    assert markdown.index("kandydat X") < markdown.index("zwykly Y")


def test_needs_owner_without_history_is_still_shown(home):
    ticket(home, "t-needs", status="needs-owner", symptom="cos bez historii")
    payload = build(home)
    needs = payload["sources"]["helpdesk"]["needs_owner"]
    assert len(needs) == 1
    assert needs[0]["question"] == "?"
    assert "t-needs" in od.render_markdown(payload)


# --------------------------------------------------------------------------
# Znalezisko 3: BLOCKED nadpisany pozniejszym ACCEPTED jest szumem
# --------------------------------------------------------------------------


def test_blocked_superseded_by_later_accepted_same_task_id(home):
    write_result(
        home,
        "t",
        "run-1",
        {"acceptance_status": "BLOCKED", "reason": "czekamy na decyzje"},
        when=NOW - dt.timedelta(hours=3),
    )
    write_result(
        home,
        "t",
        "run-2",
        {"acceptance_status": "ACCEPTED"},
        when=NOW - dt.timedelta(hours=1),
    )
    todo = build(home)["sources"]["todo"]
    assert todo["blocked"] == []
    assert todo["superseded"] == 1
    assert "BLOCKED" not in todo["counts"]


def test_blocked_superseded_by_later_accepted_same_run_prefix(home):
    write_result_at(
        home,
        "t/run-v2/implement/result.json",
        {"acceptance_status": "BLOCKED", "reason": "stara wersja"},
        when=NOW - dt.timedelta(hours=2),
    )
    write_result_at(
        home,
        "t/run-v2b/review/result.json",
        {"acceptance_status": "ACCEPTED"},
        when=NOW - dt.timedelta(hours=1),
    )
    todo = build(home)["sources"]["todo"]
    assert todo["blocked"] == []
    assert todo["superseded"] == 1


def test_blocked_not_superseded_by_older_accepted_stays(home):
    write_result_at(
        home,
        "t/run-v2/implement/result.json",
        {"acceptance_status": "BLOCKED", "reason": "nadal zablokowane"},
        when=NOW - dt.timedelta(hours=1),
    )
    write_result_at(
        home,
        "t/run-v2b/review/result.json",
        {"acceptance_status": "ACCEPTED"},
        when=NOW - dt.timedelta(hours=5),
    )
    todo = build(home)["sources"]["todo"]
    assert len(todo["blocked"]) == 1
    assert todo["superseded"] == 0
    assert todo["counts"]["BLOCKED"] == 1


def test_blocked_without_any_accepted_stays(home):
    write_result(
        home,
        "t",
        "run-1",
        {"acceptance_status": "BLOCKED", "reason": "czekamy na wlasciciela"},
    )
    todo = build(home)["sources"]["todo"]
    assert len(todo["blocked"]) == 1
    assert todo["superseded"] == 0


def test_duplicate_blocked_entries_are_deduplicated(home):
    write_result_at(
        home,
        "t/run-1/implement/result.json",
        {"acceptance_status": "BLOCKED", "reason": "ten sam powod"},
        when=NOW - dt.timedelta(hours=2),
    )
    write_result_at(
        home,
        "t/run-1/review/result.json",
        {"acceptance_status": "BLOCKED", "reason": "ten sam powod"},
        when=NOW - dt.timedelta(hours=1),
    )
    todo = build(home)["sources"]["todo"]
    assert len(todo["blocked"]) == 1
    assert todo["blocked"][0]["reason"] == "ten sam powod"


# --------------------------------------------------------------------------
# Znalezisko 4: digest ma byc krotki
# --------------------------------------------------------------------------


def test_failed_runs_are_grouped_in_a_table(home):
    for i in range(3):
        fleet_run(
            home,
            f"run-fail-{i}",
            agent="codex",
            model="gpt-x",
            run_status="FAILED",
            completion_reason="timeout",
        )
    fleet_run(
        home,
        "run-fail-other",
        agent="claude",
        model="claude-x",
        run_status="BLOCKED",
        completion_reason="brak czasu",
    )
    markdown = od.render_markdown(build(home))
    assert "| agent | model | run_status | completion_reason | liczba |" in markdown
    assert "| codex | gpt-x | FAILED | timeout | 3 |" in markdown
    assert "| claude | claude-x | BLOCKED | brak czasu | 1 |" in markdown
    # Stary, per-run wykaz zniknal.
    assert "Nieudane runy:" not in markdown


def test_lists_are_capped_at_ten_items(home):
    for i in range(25):
        ticket(home, f"t-{i:03d}", symptom=f"objaw {i}", awaiting_owner=True)
    for i in range(25):
        write_result(
            home,
            f"task-{i:03d}",
            f"run-{i:03d}",
            {"acceptance_status": "BLOCKED", "reason": f"powod {i}"},
        )
    markdown = od.render_markdown(build(home))
    assert "... i 15 wiecej" in markdown
    # Nie wypisujemy wszystkich 25 pozycji w sekcji decyzji.
    decision_block = markdown.split("## Fleet")[0]
    assert decision_block.count("- Helpdesk") == od.MAX_LIST_ITEMS
    assert decision_block.count("- TODO zablokowane") == od.MAX_LIST_ITEMS


def test_digest_stays_short_on_synthetic_volume(home):
    """~450 runow i ~500 zgloszen -> markdown ponizej 15 KB."""
    for i in range(450):
        status = "COMPLETED" if i % 3 else "FAILED"
        fleet_run(
            home,
            f"run-{i:04d}",
            agent=f"agent-{i % 5}",
            model=f"model-{i % 7}",
            run_status=status,
            completion_reason="timeout" if status == "FAILED" else "done",
            duration_seconds=60.0 * (i % 9 + 1),
        )
    for i in range(500):
        ticket(
            home,
            f"t-{i:04d}",
            status="open",
            component=f"comp-{i % 6}",
            symptom=f"objaw zgloszenia numer {i} z dlugim opisem " * 2,
            count=i,
            awaiting_owner=(i % 20 == 0),
        )
    for i in range(30):
        write_text(
            home / "owner-decisions" / "decisions.jsonl",
            json.dumps(
                {
                    "ts": "2026-10-21T10:00:00Z",
                    "project": f"projekt-{i}",
                    "category": "technical",
                    "question": f"pytanie numer {i}",
                    "choice": "tak",
                },
                ensure_ascii=False,
            )
            + "\n",
            append=True,
        )
    write_json(
        home / "usage" / "claude-plan-usage.json",
        {
            "observed_at": "2026-10-21T20:30:00Z",
            "five_hour_pct": 42,
            "weekly_all_pct": 30,
            "extra_usage_enabled": False,
        },
    )
    for i in range(120):
        write_result(
            home,
            f"task-{i:04d}",
            f"run-{i:04d}",
            {"acceptance_status": "ACCEPTED" if i % 2 else "BLOCKED", "reason": "powod testowy"},
        )

    payload = build(home)
    markdown = od.render_markdown(payload)
    size = len(markdown.encode("utf-8"))
    assert payload["sources"]["fleet"]["total_runs"] == 450
    assert payload["sources"]["helpdesk"]["open_total"] == 500
    assert len(payload["sources"]["helpdesk"]["open"]) == od.MAX_LIST_ITEMS
    assert size < 15 * 1024, f"digest za dlugi: {size} B"
    # Nadal wszystko istotne jest w raporcie.
    assert "## Wymaga Twojej decyzji" in markdown
    assert "## Fleet" in markdown
    assert "Razem runow w oknie: 450." in markdown


# --------------------------------------------------------------------------
# Raporty hookow Claude i kolejka RSI T2
# --------------------------------------------------------------------------


def fake_hooks(tmp_path: Path, delegation: str, browser: str) -> Path:
    """Tworzy lokalne skrypty raportow, bez dostepu do prawdziwego home."""
    hooks = tmp_path / "fake-hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "delegation_guard.py").write_text(delegation, encoding="utf-8")
    (hooks / "browser_guard.py").write_text(browser, encoding="utf-8")
    return hooks


def report_script(payload: dict, *, exit_code: int = 0) -> str:
    encoded = json.dumps(payload, ensure_ascii=False)
    return f"import sys\nprint({encoded!r})\nsys.exit({exit_code})\n"


def hook_section(markdown: str) -> list[str]:
    body = markdown.split("## Hooki Claude (ostatnia doba)\n", 1)[1]
    return body.split("\n## ", 1)[0].strip().splitlines()


def t2_section(markdown: str) -> list[str]:
    body = markdown.split("## RSI - czeka na Twoja decyzje (T2)\n", 1)[1]
    return body.split("\n## ", 1)[0].strip().splitlines()


def t2_state(home: Path, triage=None, owner_queue=None) -> Path:
    return write_json(
        home / "helpdesk" / "triage-state.json",
        {"last_result": triage, "owner_queue_last_result": owner_queue},
    )


def test_claude_guards_parse_reports_mask_secrets_and_render(home, tmp_path, monkeypatch):
    secret = "sk" + "-" + "syntheticGuardFixture123456"
    delegation = {
        "totals": {"sessions": 2, "code_edits": 9, "delegations": 1, "exceptions": 1},
        "sessions": [
            {
                "session": "1234567890abcdef",
                "code_edits": 8,
                "limit": 6,
                "exceptions": [
                    {"ts": "2026-10-21T10:00:00Z", "grant": 25, "reason": f"wyjatek {secret}"}
                ],
            }
        ],
    }
    browser = {
        "totals": {"sessions": 1, "blocked": 2, "allowed": 3, "grants": 1},
        "sessions": [
            {
                "session": "abcdefgh1234",
                "grants": [
                    {
                        "ts": "2026-10-21T11:00:00Z",
                        "expires_at": "2026-10-21T12:00:00Z",
                        "reason": f"zgoda {secret}",
                    }
                ],
            }
        ],
    }
    hooks = fake_hooks(
        tmp_path,
        report_script(delegation),
        report_script(browser),
    )
    monkeypatch.setenv("OWNER_DIGEST_HOOKS_DIR", str(hooks))

    payload = build(home)
    guards = payload["sources"]["claude_guards"]
    assert guards["delegation"]["sessions"] == 2
    assert guards["delegation"]["code_edits"] == 9
    assert guards["delegation"]["delegations"] == 1
    assert guards["delegation"]["exceptions_total"] == 1
    assert guards["delegation"]["over_limit"] == 1
    assert guards["delegation"]["exceptions"][0]["session"] == "12345678"
    assert guards["browser"]["blocked"] == 2
    assert guards["browser"]["allowed"] == 3
    assert guards["browser"]["grants"] == 1

    markdown = od.render_markdown(payload)
    assert "Delegacje: sesje 2 | wlasne edycje kodu 9 | delegacje 1 | wyjatki 1 | sesje ponad limit 1" in markdown
    assert "Prywatny Chrome: blokady 2 | dozwolone 3 | granty 1" in markdown
    assert "wyjatek 2026-10-21T10:00:00Z sesja 12345678 (grant 25): wyjatek <ukryte>" in markdown
    assert "grant 2026-10-21T11:00:00Z sesja abcdefgh (wazny do 2026-10-21T12:00:00Z): zgoda <ukryte>" in markdown
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert secret not in markdown


def test_claude_guard_subprocess_environment_and_windows_flags(home, tmp_path, monkeypatch):
    script = (
        "import json, os\n"
        "print(json.dumps({'totals': {'sessions': 1}, 'sessions': [], 'seen': "
        "[os.environ.get('DELEGATION_GUARD_HOME'), os.environ.get('BROWSER_GUARD_HOME')]}))\n"
    )
    hooks = fake_hooks(tmp_path, script, script)
    monkeypatch.setenv("OWNER_DIGEST_HOOKS_DIR", str(hooks))
    real_run = od.subprocess.run
    captured = []

    def capture_run(*args, **kwargs):
        completed = real_run(*args, **kwargs)
        captured.append((args, kwargs, json.loads(completed.stdout)))
        return completed

    monkeypatch.setattr(od.subprocess, "run", capture_run)
    result = od.read_claude_guards(home)

    assert len(captured) == 2
    for args, kwargs, output in captured:
        assert kwargs["stdin"] == od.subprocess.DEVNULL
        assert kwargs["capture_output"] is True
        assert kwargs["timeout"] == od.GUARD_TIMEOUT_SECONDS
        assert kwargs["shell"] is False
        assert kwargs["env"]["DELEGATION_GUARD_HOME"] == str(home / "delegation-guard")
        assert kwargs["env"]["BROWSER_GUARD_HOME"] == str(home / "browser-guard")
        assert output["seen"] == [str(home / "delegation-guard"), str(home / "browser-guard")]
        assert kwargs["creationflags"] == getattr(od.subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt":
            assert kwargs["creationflags"] & od.subprocess.CREATE_NO_WINDOW
        assert args[0][2:6] == ["report", "--days", "1", "--json"]
    assert result["delegation"]["sessions"] == 1
    assert result["browser"]["sessions"] == 1


def test_claude_guard_reason_outside_console_codepage_stays_available(home, tmp_path, monkeypatch):
    # Powod spoza cp1250/cp1252 (strzalka, emoji) nie moze wywrocic raportu hooka.
    reason = "integracja → odbior \U0001F680 zażółć gęślą jaźń"
    delegation = {
        "totals": {"sessions": 1, "code_edits": 1, "delegations": 0, "exceptions": 1},
        "sessions": [
            {
                "session": "feedfacecafe",
                "code_edits": 1,
                "limit": 6,
                "exceptions": [{"ts": "2026-10-21T10:00:00Z", "grant": 25, "reason": reason}],
            }
        ],
    }
    script = (
        "import json\n"
        f"print(json.dumps({delegation!r}, ensure_ascii=False))\n"
    )
    hooks = fake_hooks(tmp_path, script, report_script({"totals": {"sessions": 0}, "sessions": []}))
    monkeypatch.setenv("OWNER_DIGEST_HOOKS_DIR", str(hooks))
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)

    result = od.read_claude_guards(home)

    assert result["delegation"]["status"] == "ok", result["delegation"]["reason"]
    assert result["delegation"]["exceptions"][0]["reason"] == reason


def test_claude_guard_missing_script_is_one_unavailable_line(home, tmp_path, monkeypatch):
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    monkeypatch.setenv("OWNER_DIGEST_HOOKS_DIR", str(hooks))
    markdown = od.render_markdown(build(home))
    lines = hook_section(markdown)
    assert lines == [
        "delegation_guard: niedostepne: brak skryptu delegation_guard.py",
        "browser_guard: niedostepne: brak skryptu browser_guard.py",
    ]


def test_claude_guard_nonzero_exit_invalid_json_and_bad_totals(home, tmp_path, monkeypatch):
    hooks = fake_hooks(
        tmp_path,
        report_script({"totals": {}, "sessions": []}, exit_code=2),
        "print('not json')\n",
    )
    monkeypatch.setenv("OWNER_DIGEST_HOOKS_DIR", str(hooks))
    reports = od.read_claude_guards(home)
    assert reports["delegation"]["status"] == "unavailable"
    assert reports["delegation"]["reason"] == "kod wyjscia 2"
    assert reports["browser"]["status"] == "unavailable"
    assert reports["browser"]["reason"] == "niepoprawny JSON"

    hooks = fake_hooks(
        tmp_path / "bad-totals",
        report_script({"sessions": []}),
        report_script({"totals": [], "sessions": []}),
    )
    monkeypatch.setenv("OWNER_DIGEST_HOOKS_DIR", str(hooks))
    reports = od.read_claude_guards(home)
    assert reports["delegation"]["reason"] == "brak lub zle totals"
    assert reports["browser"]["reason"] == "brak lub zle totals"


def test_claude_guard_timeout_is_unavailable(home, tmp_path, monkeypatch):
    hooks = fake_hooks(tmp_path, "import time\ntime.sleep(2)\n", "import time\ntime.sleep(2)\n")
    monkeypatch.setenv("OWNER_DIGEST_HOOKS_DIR", str(hooks))
    monkeypatch.setattr(od, "GUARD_TIMEOUT_SECONDS", 0.05)
    reports = od.read_claude_guards(home)
    assert reports["delegation"]["status"] == "unavailable"
    assert reports["delegation"]["reason"] == "timeout"
    assert reports["browser"]["status"] == "unavailable"
    assert reports["browser"]["reason"] == "timeout"


def test_real_claude_hooks_report_empty_temporary_home(home, monkeypatch):
    hooks = Path(__file__).resolve().parents[2] / "global" / "claude" / "hooks"
    monkeypatch.setenv("OWNER_DIGEST_HOOKS_DIR", str(hooks))
    reports = od.read_claude_guards(home)
    assert reports["delegation"]["status"] == "ok"
    assert reports["delegation"]["sessions"] == 0
    assert reports["delegation"]["code_edits"] == 0
    assert reports["delegation"]["delegations"] == 0
    assert reports["delegation"]["exceptions_total"] == 0
    assert reports["browser"]["status"] == "ok"
    assert reports["browser"]["sessions"] == 0
    assert reports["browser"]["blocked"] == 0
    assert reports["browser"]["allowed"] == 0
    assert reports["browser"]["grants"] == 0


def test_rsi_t2_missing_and_invalid_state_have_one_line(home):
    markdown = od.render_markdown(build(home))
    assert t2_section(markdown) == [f"niedostepne: brak pliku {home / 'helpdesk' / 'triage-state.json'}"]

    state_path = home / "helpdesk" / "triage-state.json"
    write_text(state_path, "{bad json")
    reports = od.read_rsi_t2(home)
    assert reports["status"] == "unavailable"
    assert "niepoprawny JSON" in reports["reason"]


def test_rsi_t2_union_context_and_ticket_enrichment(home):
    write_json(
        home / "helpdesk" / "tickets" / "t2-a.json",
        {"component": "login", "symptom": "brak hasla", "status": "open"},
    )
    t2_state(
        home,
        {"status": "completed", "result": "triage done", "t2_waiting": ["t2-b", "t2-a"]},
        {"status": "failed", "result": "owner queue", "t2_waiting": ["t2-a", "t2-c"]},
    )
    reports = od.read_rsi_t2(home)
    assert reports["status"] == "ok"
    assert reports["items_total"] == 3
    by_id = {item["id"]: item for item in reports["items"]}
    assert by_id["t2-a"]["sources"] == ["triage", "owner-queue"]
    assert by_id["t2-b"]["sources"] == ["triage"]
    assert by_id["t2-c"]["sources"] == ["owner-queue"]
    assert by_id["t2-a"]["component"] == "login"
    assert by_id["t2-a"]["symptom"] == "brak hasla"
    markdown = od.render_markdown(build(home))
    assert "triage: completed (triage done) | owner-queue: failed (owner queue)" in markdown
    assert "t2-a [login] brak hasla (zrodlo: triage/owner-queue)" in markdown


def test_rsi_t2_null_and_missing_records_are_empty(home):
    t2_state(home, None, {"status": "completed", "result": "nic do decyzji"})
    reports = od.read_rsi_t2(home)
    assert reports["status"] == "ok"
    assert reports["items"] == []
    assert reports["contexts"][0] == {"source": "triage", "status": "?", "result": "?"}
    markdown = od.render_markdown(build(home))
    assert t2_section(markdown)[0] == "- Brak - triaz nic nie odlozyl do Twojej decyzji."


def test_rsi_t2_unsafe_ids_are_shown_but_not_used_as_paths(home):
    write_json(home / "helpdesk" / "outside.json", {"component": "unsafe", "symptom": "must not read"})
    t2_state(home, {"t2_waiting": ["../outside"]}, None)
    reports = od.read_rsi_t2(home)
    assert reports["status"] == "ok"
    assert reports["items"][0]["component"] == "?"
    assert reports["items"][0]["symptom"] == "?"
    assert reports["items"][0]["id"] == "../outside"


def test_rsi_t2_masks_ticket_text_and_deduplicates_decision_list(home):
    secret = "sk" + "-" + "syntheticTriageFixture123456"
    ticket(home, "shared", awaiting_owner=True)
    ticket(home, "t2-only", component=f"component {secret}", symptom=f"symptom {secret}")
    t2_state(home, {"status": "completed", "result": "done", "t2_waiting": ["shared", "t2-only"]}, None)
    payload = build(home)
    decisions = payload["decision_needed"]
    assert decisions["rsi_t2_waiting_total"] == 1
    assert [item["id"] for item in decisions["rsi_t2_waiting"]] == ["t2-only"]
    assert decisions["count"] == 2
    markdown = od.render_markdown(payload)
    assert markdown.count("RSI T2 shared:") == 0
    assert markdown.count("shared [office] brak odpowiedzi (zrodlo: triage)") == 1
    assert "- RSI T2 t2-only: symptom <ukryte> [component <ukryte>] (czeka na Twoja decyzje)" in markdown
    assert secret not in markdown
