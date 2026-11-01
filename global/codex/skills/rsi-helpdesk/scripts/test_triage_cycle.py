"""Synthetic tests for RSI Helpdesk triage_cycle.py.

No models, no network, no production deploy. All subprocesses CREATE_NO_WINDOW.
No file deletions (tests leave temp directories in place).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import helpdesk  # noqa: E402
import resolve_cycle  # noqa: E402
import triage_cycle  # noqa: E402


class FakeRunner:
    def __init__(
        self,
        worker_triage: list[dict[str, Any]] | None = None,
        director_verdict: str = "ZATWIERDZAM",
        director_summary: str | None = None,
        director_handoff_text: str | None = None,
        director_model: str = "gemini-4-argon-high",
        worker_model: str = "MiniMax-M3.1-Flash",
        worker_status: str = "CANDIDATE",
        director_status: str = "CANDIDATE",
        raise_on_role: str | None = None,
        skip_worker_triage_file: bool = False,
        owner_queue_summary: str | None = None,
    ):
        self.worker_triage = worker_triage
        self.director_verdict = director_verdict
        self.director_summary = director_summary
        self.director_handoff_text = director_handoff_text
        self.director_model = director_model
        self.worker_model = worker_model
        self.worker_status = worker_status
        self.director_status = director_status
        self.raise_on_role = raise_on_role
        self.skip_worker_triage_file = skip_worker_triage_file
        self.owner_queue_summary = owner_queue_summary
        self.calls: list[dict[str, Any]] = []

    def __call__(self, req_path: Path, out_path: Path) -> dict[str, Any]:
        req = json.loads(req_path.read_text(encoding="utf-8"))
        role = req.get("role")
        self.calls.append({"role": role, "req_path": req_path, "out_path": out_path})

        if self.raise_on_role and self.raise_on_role == role:
            raise RuntimeError(f"fake error on {role}")

        if role == "maker":
            if not self.skip_worker_triage_file and self.worker_triage is not None:
                ws = Path(req["workspace"])
                (ws / "triage.json").write_text(
                    json.dumps(self.worker_triage, ensure_ascii=False, indent=1), encoding="utf-8"
                )
            out = {
                "status": self.worker_status,
                "agent": "minimax",
                "model": self.worker_model,
                "handoff": {"summary": "triage.json generated"},
            }
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            return out

        if role == "director":
            goal = str(req.get("goal") or "")
            summary = self.director_summary or self.director_verdict
            if self.owner_queue_summary is not None and ("owner-queue" in goal or "candidate" in goal or "needs-owner" in goal):
                summary = self.owner_queue_summary
            handoff: dict[str, Any] = {"summary": summary}
            if self.director_handoff_text is not None:
                handoff["text"] = self.director_handoff_text
            out = {
                "status": self.director_status,
                "agent": "antigravity",
                "model": self.director_model,
                "handoff": handoff,
                "summary": summary,
            }
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            return out

        return {}


class TriageBase(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ.get("TEAM_TEST_ROOT") or tempfile.gettempdir())
        root.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="rsi-triage-", dir=root))
        self.hd = self.root / "hd"
        self.env = mock.patch.dict(os.environ, {
            "RSI_HELPDESK_HOME": str(self.hd),
            "RSI_HELPDESK_NO_OFFICE": "1",
            "RSI_HELPDESK_NO_TRIGGER": "1",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def file(self, symptom: str = "Test symptom", component: str = "fleet-adapter:test", **kwargs) -> dict[str, Any]:
        args = {
            "component": component,
            "symptom": symptom,
            "evidence": ["e1"],
            "impact": "impact",
            "workaround": "workaround",
            "reporter": "test-agent",
        }
        args.update(kwargs)
        ticket, _ = helpdesk.file_ticket(**args)
        return ticket

    def file_candidate(self, symptom: str = "Candidate symptom", hours_ago: float = 25.0, **kwargs) -> dict[str, Any]:
        t = self.file(symptom=symptom, **kwargs)
        helpdesk.claim(t["signature"], by="test-resolver", minutes=60)
        t = helpdesk.update(t["signature"], by="test-resolver", status="candidate", note="candidate ready",
                            force=True, release_claim=True)
        old_ts = helpdesk.iso(helpdesk.now() - dt.timedelta(hours=hours_ago))
        t["first_seen"] = old_ts
        t["last_seen"] = old_ts
        t["updated_at"] = old_ts
        for h in t.get("history", []):
            h["ts"] = old_ts
        helpdesk.save(t)
        return helpdesk.load(t["signature"])

    def file_needs_owner(self, symptom: str = "Needs owner symptom", hours_ago: float = 25.0, **kwargs) -> dict[str, Any]:
        t = self.file(symptom=symptom, **kwargs)
        t = helpdesk.update(t["signature"], by="test-agent", status="needs-owner", note="needs owner attention", force=True)
        old_ts = helpdesk.iso(helpdesk.now() - dt.timedelta(hours=hours_ago))
        t["first_seen"] = old_ts
        t["last_seen"] = old_ts
        t["updated_at"] = old_ts
        for h in t.get("history", []):
            h["ts"] = old_ts
        helpdesk.save(t)
        return helpdesk.load(t["signature"])


class TriageDueAndStatusTests(TriageBase):
    def test_due_conditions(self):
        # 0 untriaged tickets -> not due
        due, reason = triage_cycle.is_due()
        self.assertFalse(due)
        self.assertIn("0 untriaged", reason)

        # File 5 tickets (less than min_untriaged=10)
        tickets = [self.file(symptom=f"Symptom alpha {chr(ord('a') + i)}") for i in range(5)]
        due, reason = triage_cycle.is_due()
        self.assertFalse(due)

        # File 5 more -> 10 untriaged
        for i in range(5, 10):
            tickets.append(self.file(symptom=f"Symptom alpha {chr(ord('a') + i)}"))

        # Case: 10 untriaged, last run was 25 hours ago -> DUE
        past_25h = helpdesk.iso(helpdesk.now() - dt.timedelta(hours=25))
        triage_cycle.save_state({"last_run": past_25h, "last_result": "done"})
        due, reason = triage_cycle.is_due()
        self.assertTrue(due)
        self.assertIn(">= 24h", reason)

        # Case: 10 untriaged, last run was 10 hours ago -> NOT DUE
        past_10h = helpdesk.iso(helpdesk.now() - dt.timedelta(hours=10))
        triage_cycle.save_state({"last_run": past_10h, "last_result": "done"})
        due, reason = triage_cycle.is_due()
        self.assertFalse(due)

        # Case: 2 untriaged, last run was 8 days ago -> DUE (>= 7d and >= 1 untriaged)
        # Prioritize 8 tickets so only 2 remain untriaged
        for t in tickets[2:]:
            helpdesk.set_priority(t["signature"], "P2", "test", "prio")
        past_8d = helpdesk.iso(helpdesk.now() - dt.timedelta(days=8))
        triage_cycle.save_state({"last_run": past_8d, "last_result": "done"})
        due, reason = triage_cycle.is_due()
        self.assertTrue(due)
        self.assertIn(">= 7d", reason)

        # Force due
        triage_cycle.save_state({"last_run": past_10h, "last_result": "done"})
        due, reason = triage_cycle.is_due(force_due=True)
        self.assertTrue(due)
        self.assertIn("forced", reason)

        # STOP file blocks even if forced
        self.hd.mkdir(parents=True, exist_ok=True)
        (self.hd / "STOP").write_text("", encoding="utf-8")
        due, reason = triage_cycle.is_due(force_due=True)
        self.assertFalse(due)
        self.assertEqual(reason, "stopped")

    def test_status_cli(self):
        self.file(symptom="cli status test")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ret = triage_cycle.main(["status"])
        self.assertEqual(ret, 0)
        data = json.loads(buf.getvalue())
        self.assertIn("untriaged_tickets", data)
        self.assertEqual(data["untriaged_tickets"], 1)
        self.assertIn("state", data)


class TriageExecutionTests(TriageBase):
    def test_director_verdict_only_accepts_first_nonempty_line_prefix(self):
        # A negation in the first line of a field rejects the whole response.
        self.assertEqual(triage_cycle.parse_director_verdict("NIE ZATWIERDZAM\n| id | ... |"), ("ODRZUCAM", []))
        # A field with a preamble carries no verdict (the next field is checked).
        self.assertEqual(triage_cycle.parse_director_verdict("Wstep\nZATWIERDZAM"), (None, []))
        self.assertEqual(triage_cycle.parse_director_verdict("** ZATWIERDZAM **"), ("ZATWIERDZAM", []))
        self.assertEqual(triage_cycle.parse_director_verdict("# > ZATWIERDZAM Z POPRAWKAMI"),
                         ("ZATWIERDZAM Z POPRAWKAMI", []))

    def test_director_verdict_takes_the_first_field_with_a_verdict(self):
        # Fields are read in order: handoff.summary, handoff.text, public-handoff.json.
        fields = ["Wstep: przejrzalem propozycje triazu.", "ZATWIERDZAM"]
        self.assertEqual(triage_cycle.parse_director_verdict(fields)[0], "ZATWIERDZAM")
        # A preamble in the first field does not stop the search.
        self.assertEqual(
            triage_cycle.parse_director_verdict(["Preambula", "ZATWIERDZAM Z POPRAWKAMI"])[0],
            "ZATWIERDZAM Z POPRAWKAMI")
        # A negation in any field rejects everything.
        self.assertEqual(
            triage_cycle.parse_director_verdict(["NIE ZATWIERDZAM", "ZATWIERDZAM"])[0],
            "ODRZUCAM")

    def test_director_verdict_preamble_falls_back_to_text_and_negation_rejects(self):
        approved = self.file(symptom="Director field order")
        plan = [{"id": approved["id"], "kategoria": "ZOSTAW", "canonical": None,
                 "priorytet": "P2", "powod": "worker"}]
        runner = FakeRunner(
            worker_triage=plan,
            director_summary="Wstep: przejrzalem propozycje triazu.",
            director_handoff_text="ZATWIERDZAM",
        )
        report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["director_verdict"], "ZATWIERDZAM")
        self.assertEqual(helpdesk.load(approved["signature"])["priority"], "P2")

        rejected = self.file(symptom="Director negation")
        plan2 = [{"id": rejected["id"], "kategoria": "ZOSTAW", "canonical": None,
                  "priorytet": "P2", "powod": "worker"}]
        runner2 = FakeRunner(
            worker_triage=plan2,
            director_summary="NIE ZATWIERDZAM",
            director_handoff_text="ZATWIERDZAM",
        )
        report2 = triage_cycle.run_cycle(force_due=True, fleet_runner=runner2)
        self.assertEqual(report2["status"], "failed")
        self.assertIn("ODRZUCAM", report2["result"])
        self.assertIsNone(helpdesk.load(rejected["signature"])["priority"])

    def test_amendment_set_is_all_or_nothing(self):
        t1 = self.file(symptom="amendment primary")
        t2 = self.file(symptom="amendment secondary")
        plan = [
            {"id": t1["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "worker 1"},
            {"id": t2["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P3", "powod": "worker 2"},
        ]
        invalid_rows = [
            ("ZATWIERDZAM Z POPRAWKAMI", None, "at least one parsed amendment"),
            ("ZATWIERDZAM Z POPRAWKAMI", f"| {t1['id']} | ZOSTAW | P4 | bad priority |", "Invalid Director priority"),
            ("ZATWIERDZAM Z POPRAWKAMI", f"| HD-20990101-000000 | ZOSTAW | P1 | unknown id |", "outside triage plan"),
            ("ZATWIERDZAM Z POPRAWKAMI", f"| {t1['id']} | ARCHIVE | - | bad category |", "Invalid Director amendment category"),
            ("ZATWIERDZAM Z POPRAWKAMI", f"| {t1['id']} | DUPLIKAT | missing-canonical | bad canonical |", "Invalid Director canonical"),
        ]
        original_history = {t["signature"]: len(t["history"]) for t in (t1, t2)}
        for verdict, row, reason in invalid_rows:
            with self.subTest(reason=reason):
                summary = verdict + ("\n" + row if row else "")
                report = triage_cycle.run_cycle(
                    force_due=True,
                    fleet_runner=FakeRunner(worker_triage=plan, director_summary=summary),
                )
                self.assertEqual(report["status"], "failed")
                self.assertIn("director-amendments-validation-error", report["result"])
                self.assertIn(reason.lower(), triage_cycle.load_state()["last_result"]["result"].lower())
                for ticket in (t1, t2):
                    current = helpdesk.load(ticket["signature"])
                    self.assertEqual(current["status"], "open")
                    self.assertIsNone(current["priority"])
                    self.assertEqual(len(current["history"]), original_history[ticket["signature"]])

    def test_director_text_can_come_from_handoff_text_or_public_handoff(self):
        ticket = self.file(symptom="response source")
        plan = [{"id": ticket["id"], "kategoria": "ZOSTAW", "canonical": None,
                 "priorytet": "P2", "powod": "worker"}]

        def runner(req_path: Path, out_path: Path) -> dict[str, Any]:
            req = json.loads(req_path.read_text(encoding="utf-8"))
            if req["role"] == "maker":
                ws = Path(req["workspace"])
                (ws / "triage.json").write_text(json.dumps(plan), encoding="utf-8")
                return {"status": "CANDIDATE"}
            return {"status": "CANDIDATE", "handoff": {
                "summary": "ZATWIERDZAM", "text": "odpowiedz z pola handoff.text"}}

        report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(helpdesk.load(ticket["signature"])["priority"], "P2")

    def test_default_director_candidates_start_with_kimi_k31_pro(self):
        # Owner decision 2026-10-24: Kimi K3.1 Pro (k3.1-pro) is the tier D
        # default Director model; Argon moved down to tier M.
        self.assertEqual(
            triage_cycle.DEFAULT_DIRECTOR_CANDIDATES[0],
            {"agent": "kimi", "model": "k3.1-pro"})
        self.assertEqual(
            triage_cycle.DEFAULT_DIRECTOR_CANDIDATES[1],
            {"agent": "muse", "model": "muse-wonder-full-1.0"})
        self.assertNotIn(
            "gemini-4-argon-high",
            [c["model"] for c in triage_cycle.DEFAULT_DIRECTOR_CANDIDATES])
        req = triage_cycle.build_director_request(self.root, {"triage": {}})
        self.assertEqual(req["task"]["maker"][0],
                         {"agent": "kimi", "model": "k3.1-pro"})

    def test_default_fleet_runner_requires_zero_exit_and_success_status(self):
        out_path = self.root / "runner-out.json"
        req_path = self.root / "runner-req.json"
        req_path.write_text("{}", encoding="utf-8")
        out_path.write_text(json.dumps({"status": "CANDIDATE"}), encoding="utf-8")
        with mock.patch.object(triage_cycle.subprocess, "run", return_value=mock.Mock(returncode=2, stderr=b"failed")):
            with self.assertRaisesRegex(RuntimeError, "exit code 2"):
                triage_cycle.default_fleet_runner(req_path, out_path)
        with mock.patch.object(triage_cycle.subprocess, "run", return_value=mock.Mock(returncode=0, stderr=b"")):
            out_path.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "status is not successful"):
                triage_cycle.default_fleet_runner(req_path, out_path)
            out_path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
            self.assertEqual(triage_cycle.default_fleet_runner(req_path, out_path)["status"], "completed")

    def test_t2_gate_avoids_context_false_hits_and_scans_workaround(self):
        context_only = {
            "component": "token-contexts",
            "symptom": "tokeny kontekstu nie wczytuja sie",
            "impact": "permission denied from client HTTP forecast",
            "workaround": "retry the request",
            "history": [{"note": "tokenizer context only"}],
        }
        self.assertFalse(triage_cycle.is_t2_ticket(context_only))
        for wording in ("usun credential", "haslo", "hasła", "API key", "api_key", "api-key",
                        "klucz prywatny", "klucz dostepu", "access token", "rm -rf", "płatne koszty",
                        "secret", "sekret", "credential", "bearer", "delete"):
            ticket = context_only | {"workaround": wording}
            self.assertTrue(triage_cycle.is_t2_ticket(ticket), wording)

    def test_t2_gate_catches_every_polish_password_word_form(self):
        base = {"component": "runtime", "symptom": "Zgloszenie w module CLI",
                "impact": "zwykly opis", "workaround": "retry the request", "history": []}
        # Both Polish stems are covered: has + l (haslo, hasla, haslem, hasle,
        # haslami) and has + el (the genitive plural, which folds to "hasel").
        for wording in ("haslo", "hasla", "haslem", "hasle", "hasel", "haslami",
                        "hasła", "hasłem", "hasłami", "haseł"):
            with self.subTest(wording=wording):
                ticket = base | {"workaround": f"zgloszenie z {wording} w workaround"}
                self.assertTrue(triage_cycle.is_t2_ticket(ticket), wording)

    def test_t2_gate_ignores_iam_in_host_names_but_catches_iam_decisions(self):
        base = {"component": "runtime", "symptom": "Blad w module CLI",
                "impact": "zwykly opis", "workaround": "retry the request", "history": []}
        for wording in ("IAM", "iam", "uprawnienia IAM", "rola IAM", "brak uprawnien IAM"):
            with self.subTest(wording=wording):
                self.assertTrue(triage_cycle.is_t2_ticket(base | {"symptom": wording}), wording)
        for wording in ("iam.googleapis.com", "iam-proxy", "iam-proxy.local",
                        "proxy.iam.example.com", "iam.gserviceaccount.com"):
            with self.subTest(wording=wording):
                self.assertFalse(triage_cycle.is_t2_ticket(base | {"symptom": wording}), wording)

    def test_t2_gate_matches_cost_phrases_only_together_with_cost(self):
        base = {"component": "runtime", "symptom": "Blad w module", "impact": "zwykly opis",
                "workaround": "retry the request", "history": []}
        self.assertTrue(triage_cycle.is_t2_ticket(base | {"workaround": "paid cost"}))
        self.assertTrue(triage_cycle.is_t2_ticket(base | {"workaround": "paid monthly cost"}))
        # A bare cost word is not a T2 marker.
        self.assertFalse(triage_cycle.is_t2_ticket(base | {"workaround": "koszt wdrozenia"}))
        self.assertFalse(triage_cycle.is_t2_ticket(base | {"symptom": "client HTTP 500 cost"}))
        # "platn*" and "paid" count only together with a cost word, in any order
        # and with or without Polish diacritics.
        self.assertTrue(triage_cycle.is_t2_ticket(base | {"symptom": "płatne koszty API"}))
        self.assertTrue(triage_cycle.is_t2_ticket(base | {"symptom": "koszty platne miesiecznie"}))
        self.assertFalse(triage_cycle.is_t2_ticket(base | {"symptom": "platne zgloszenie oplat"}))
        self.assertFalse(triage_cycle.is_t2_ticket(base | {"symptom": "platnosc za fakture"}))
        self.assertFalse(triage_cycle.is_t2_ticket(base | {"symptom": "paid support"}))
        self.assertFalse(triage_cycle.is_t2_ticket(base | {"symptom": "klient platne konto"}))

    def test_director_response_is_read_from_the_director_workspace(self):
        ticket = self.file(symptom="public handoff source")
        plan = [{"id": ticket["id"], "kategoria": "ZOSTAW", "canonical": None,
                 "priorytet": "P2", "powod": "worker"}]

        def runner(req_path: Path, out_path: Path) -> dict[str, Any]:
            req = json.loads(req_path.read_text(encoding="utf-8"))
            workspace = Path(req["workspace"])
            if req["role"] == "maker":
                (workspace / "triage.json").write_text(json.dumps(plan), encoding="utf-8")
                return {"status": "CANDIDATE"}
            # The Director publishes its verdict only as a public handoff file
            # inside the workspace it was given.
            (workspace / "public-handoff.json").write_text(
                json.dumps({"summary": "ZATWIERDZAM"}), encoding="utf-8")
            return {"status": "CANDIDATE"}

        report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["director_verdict"], "ZATWIERDZAM")
        self.assertEqual(helpdesk.load(ticket["signature"])["priority"], "P2")

    def test_cycle_fails_closed_when_runner_status_is_not_successful(self):
        t1 = self.file(symptom="Unsuccessful runner status")
        plan = [{"id": t1["id"], "kategoria": "ZOSTAW", "canonical": None,
                 "priorytet": "P2", "powod": "ok"}]

        def runner(req_path: Path, out_path: Path) -> dict[str, Any]:
            req = json.loads(req_path.read_text(encoding="utf-8"))
            if req["role"] == "maker":
                ws = Path(req["workspace"])
                (ws / "triage.json").write_text(json.dumps(plan), encoding="utf-8")
                return {"status": "CANDIDATE"}
            return {"status": "failed", "handoff": {"summary": "ZATWIERDZAM"}}

        report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner)
        self.assertEqual(report["status"], "failed")
        self.assertIn("director-runner-failed", report["result"])
        self.assertIsNone(helpdesk.load(t1["signature"])["priority"])

    def test_cycle_fails_closed_when_runner_reports_no_status_at_all(self):
        """A runner without a status is a runner error, never a silent success.

        Fails on the pre-fix code: runner_output_is_successful() accepted a
        missing or empty status, so the cycle went on and changed the ticket.
        """
        t1 = self.file(symptom="Runner without status")
        plan = [{"id": t1["id"], "kategoria": "ZOSTAW", "canonical": None,
                 "priorytet": "P2", "powod": "ok"}]

        def make_runner(worker_out: dict[str, Any], director_out: dict[str, Any]):
            def runner(req_path: Path, out_path: Path) -> dict[str, Any]:
                req = json.loads(req_path.read_text(encoding="utf-8"))
                if req["role"] == "maker":
                    ws = Path(req["workspace"])
                    (ws / "triage.json").write_text(json.dumps(plan), encoding="utf-8")
                    return dict(worker_out)
                return dict(director_out)
            return runner

        ok_worker = {"status": "CANDIDATE"}
        cases = [
            ("worker-missing-status", {}, ok_worker, "worker-runner-failed"),
            ("worker-empty-status", {"status": ""}, ok_worker, "worker-runner-failed"),
            ("director-missing-status", ok_worker,
             {"handoff": {"summary": "ZATWIERDZAM"}}, "director-runner-failed"),
            ("director-empty-status", ok_worker,
             {"status": "", "handoff": {"summary": "ZATWIERDZAM"}}, "director-runner-failed"),
        ]
        for name, worker_out, director_out, expected in cases:
            with self.subTest(case=name):
                report = triage_cycle.run_cycle(
                    force_due=True, fleet_runner=make_runner(worker_out, director_out))
                self.assertEqual(report["status"], "failed")
                self.assertIn(expected, report["result"])
                stored = helpdesk.load(t1["signature"])
                self.assertEqual(stored["status"], "open")
                self.assertIsNone(stored["priority"])
                state = triage_cycle.load_state()
                self.assertEqual(state["last_result"]["status"], "failed")
                self.assertIn(expected, state["last_result"]["result"])

    def test_apply_triage_decisions_never_invents_a_priority(self):
        t1 = self.file(symptom="Missing priority item")
        items = [{"id": t1["id"], "kategoria": "ZOSTAW", "canonical": None,
                  "priorytet": None, "powod": "no priority"}]
        stats = triage_cycle.apply_triage_decisions(items, "director-model", [t1])
        self.assertEqual(stats["prioritized"], 0)
        self.assertEqual(stats["skipped_invalid"], 1)
        stored = helpdesk.load(t1["signature"])
        self.assertEqual(stored["status"], "open")
        self.assertIsNone(stored.get("priority"))

    def test_t2_tickets_are_never_changed_by_triage(self):
        secret = self.file(symptom="Secret report issue")
        plain = self.file(symptom="Ordinary report issue")
        secret2 = self.file(symptom="Credential report issue")
        plan = [
            {"id": secret["id"], "kategoria": "DUPLIKAT", "canonical": plain["id"], "priorytet": None, "powod": "duplicate"},
            {"id": plain["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P1", "powod": "active"},
            {"id": secret2["id"], "kategoria": "DUPLIKAT", "canonical": secret["id"], "priorytet": None, "powod": "sensitive duplicate"},
        ]
        report = triage_cycle.run_cycle(force_due=True, fleet_runner=FakeRunner(
            worker_triage=plan, director_summary="ZATWIERDZAM"))
        self.assertEqual(report["status"], "completed")

        # A T2 ticket is never closed and never gets an automatic priority:
        # the triage leaves status and priority untouched.
        first = helpdesk.load(secret["signature"])
        self.assertEqual(first["status"], "open")
        self.assertIsNone(first.get("priority"))
        second = helpdesk.load(secret2["signature"])
        self.assertEqual(second["status"], "open")
        self.assertIsNone(second.get("priority"))

        # The ordinary ticket is triaged as usual.
        self.assertEqual(helpdesk.load(plain["signature"])["priority"], "P1")

        # Both T2 tickets wait for the owner.
        self.assertEqual(set(report["t2_waiting"]), {secret["id"], secret2["id"]})

    def test_triage_skips_claimed_and_same_status_updated_after_snapshot(self):
        claimed = self.file(symptom="Triage claim race")
        changed = self.file(symptom="Triage update race")
        plan = [
            {"id": claimed["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "worker"},
            {"id": changed["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P3", "powod": "worker"},
        ]
        base_runner = FakeRunner(worker_triage=plan, director_summary="ZATWIERDZAM")

        def runner_hook(req_path: Path, out_path: Path) -> dict[str, Any]:
            req = json.loads(req_path.read_text(encoding="utf-8"))
            if req.get("role") == "director":
                helpdesk.claim(claimed["signature"], "concurrent-resolver", 60)
                helpdesk.set_priority(changed["signature"], "P1", "owner", "same status, newer timestamp")
            return base_runner(req_path, out_path)

        report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner_hook)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["applied"]["prioritized"], 0)
        self.assertEqual(report["applied"]["skipped_not_open"], 2)
        self.assertEqual(helpdesk.load(claimed["signature"])["status"], "claimed")
        changed_ticket = helpdesk.load(changed["signature"])
        self.assertEqual((changed_ticket["status"], changed_ticket["priority"]), ("open", "P1"))

    def test_triage_lock_non_owner_does_not_touch_file_and_unknown_probe_is_busy(self):
        self.file(symptom="Lock fixture")
        lock_path = self.hd / "triage.lock"
        with mock.patch.object(resolve_cycle, "process_identity", return_value=12345), \
             mock.patch.object(resolve_cycle, "probe", return_value=("alive", 12345)):
            with triage_cycle.triage_lock():
                active = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(active["status"], "active")
                self.assertTrue(active["started_at"])
                self.assertEqual(active["created"], 12345)
                before = lock_path.read_bytes()
                with self.assertRaises(triage_cycle.LockBusyError):
                    with triage_cycle.triage_lock():
                        pass
                self.assertEqual(lock_path.read_bytes(), before)
        lock_path.write_text(json.dumps({"pid": 12345, "created": 12345, "status": "active"}), encoding="utf-8")
        before_unknown = lock_path.read_bytes()
        with mock.patch.object(resolve_cycle, "probe", return_value=("unknown", None)):
            with self.assertRaises(triage_cycle.LockBusyError):
                with triage_cycle.triage_lock():
                    pass
            self.assertEqual(lock_path.read_bytes(), before_unknown)
            due, reason = triage_cycle.is_due(force_due=True)
            self.assertFalse(due)
            self.assertIn("locked", reason)
        with mock.patch.object(resolve_cycle, "probe", return_value=("alive", 54321)):
            due, reason = triage_cycle.is_due(force_due=True)
            self.assertTrue(due)
            self.assertNotIn("locked", reason)

    def test_happy_path_with_director_amendments(self):
        t1 = self.file(symptom="Office send failure primary", component="office")
        t2 = self.file(symptom="Office send failure secondary", component="office")
        t3 = self.file(symptom="Hub old bug", component="hub")
        t4 = self.file(symptom="Agent CLI failure", component="agent-cli")

        # Worker proposes:
        # t1 -> DUPLIKAT of t2
        # t2 -> ZOSTAW P2
        # t3 -> NIEAKTUALNE
        # t4 -> ZOSTAW P3
        worker_plan = [
            {"id": t1["id"], "kategoria": "DUPLIKAT", "canonical": t2["id"], "priorytet": None, "powod": "duplikat zgloszenia office"},
            {"id": t2["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "glowny defekt office"},
            {"id": t3["id"], "kategoria": "NIEAKTUALNE", "canonical": None, "priorytet": None, "powod": "nieaktualny w v2"},
            {"id": t4["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P3", "powod": "usterka cli"},
        ]

        # Director changes t4 from P3 to P1
        director_summary = (
            "ZATWIERDZAM Z POPRAWKAMI\n"
            "Zwiekszam priorytet zgloszenia t4 na P1 z uwagi na blokade:\n"
            f"| {t4['id']} | ZOSTAW | P1 | krytyczna blokada cli |\n"
        )

        runner = FakeRunner(
            worker_triage=worker_plan,
            director_verdict="ZATWIERDZAM Z POPRAWKAMI",
            director_summary=director_summary,
            director_model="gemini-4-argon-high",
        )

        report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["director_verdict"], "ZATWIERDZAM Z POPRAWKAMI")

        # Verify t1 is closed as DUPLIKAT
        t1_loaded = helpdesk.load(t1["signature"])
        self.assertEqual(t1_loaded["status"], "closed")
        self.assertIn(f"DUPLIKAT {t2['id']}", t1_loaded["history"][-1]["note"])
        self.assertIn("rsi-triage:gemini-4-argon-high", t1_loaded["history"][-1]["by"])

        # Verify t3 is closed as NIEAKTUALNE
        t3_loaded = helpdesk.load(t3["signature"])
        self.assertEqual(t3_loaded["status"], "closed")
        self.assertIn("NIEAKTUALNE", t3_loaded["history"][-1]["note"])

        # Verify t2 is open with P2
        t2_loaded = helpdesk.load(t2["signature"])
        self.assertEqual(t2_loaded["status"], "open")
        self.assertEqual(t2_loaded["priority"], "P2")

        # Verify t4 is open with P1 (Director amendment!)
        t4_loaded = helpdesk.load(t4["signature"])
        self.assertEqual(t4_loaded["status"], "open")
        self.assertEqual(t4_loaded["priority"], "P1")

        # Verify triage state updated
        state = triage_cycle.load_state()
        self.assertIsNotNone(state["last_run"])
        self.assertIn("1 duplicates closed", state["last_result"]["result"])
        self.assertIn("1 obsolete closed", state["last_result"]["result"])
        self.assertIn("2 prioritized", state["last_result"]["result"])

    def test_safety_gate_protects_sensitive_tickets(self):
        # Tickets with sensitive keywords
        t_sec = self.file(symptom="Wyciek sekret i token w naglowkach", component="fleet-adapter:sec")
        t_del = self.file(symptom="Blad usuniecia przy kasowaniu plikow", component="runtime:fs")
        t_plain = self.file(symptom="Zwykly blad logowania", component="fleet")
        history_len = {
            t_sec["signature"]: len(t_sec["history"]),
            t_del["signature"]: len(t_del["history"]),
        }

        worker_plan = [
            {"id": t_sec["id"], "kategoria": "NIEAKTUALNE", "canonical": None, "priorytet": None, "powod": "stary wyciek"},
            {"id": t_del["id"], "kategoria": "NIEAKTUALNE", "canonical": None, "priorytet": None, "powod": "juz nie kasuje"},
            {"id": t_plain["id"], "kategoria": "NIEAKTUALNE", "canonical": None, "priorytet": None, "powod": "naprawione"},
        ]

        runner = FakeRunner(
            worker_triage=worker_plan,
            director_verdict="ZATWIERDZAM",
            director_summary="ZATWIERDZAM\nWszystko ok.",
        )

        report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["applied"]["skipped_t2"], 2)
        self.assertEqual(report["applied"]["safety_overrides"], 2)
        self.assertEqual(report["applied"]["closed_obsolete"], 1)

        # t_sec and t_del stay open, without priority and without a triage note:
        # a T2 decision belongs to the owner, not to the triage.
        s1 = helpdesk.load(t_sec["signature"])
        self.assertEqual(s1["status"], "open")
        self.assertIsNone(s1.get("priority"))
        self.assertEqual(len(s1["history"]), history_len[t_sec["signature"]])

        s2 = helpdesk.load(t_del["signature"])
        self.assertEqual(s2["status"], "open")
        self.assertIsNone(s2.get("priority"))
        self.assertEqual(len(s2["history"]), history_len[t_del["signature"]])

        # Their ids are reported to the owner.
        self.assertEqual(set(report["t2_waiting"]), {t_sec["id"], t_del["id"]})
        self.assertEqual(set(triage_cycle.load_state()["last_result"]["t2_waiting"]),
                         {t_sec["id"], t_del["id"]})

        # t_plain can be closed
        p = helpdesk.load(t_plain["signature"])
        self.assertEqual(p["status"], "closed")

    def test_validation_errors_fail_closed(self):
        t1 = self.file(symptom="Validation test primary")
        t2 = self.file(symptom="Validation test secondary")

        test_cases = [
            ("missing_id", [
                {"id": t1["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "ok"},
            ]),
            ("duplicate_id", [
                {"id": t1["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "ok"},
                {"id": t1["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "ok"},
            ]),
            ("invalid_category", [
                {"id": t1["id"], "kategoria": "UNKNOWN", "canonical": None, "priorytet": "P2", "powod": "ok"},
                {"id": t2["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "ok"},
            ]),
            ("invalid_canonical", [
                {"id": t1["id"], "kategoria": "DUPLIKAT", "canonical": "HD-nonexistent", "priorytet": None, "powod": "dup"},
                {"id": t2["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "ok"},
            ]),
            ("cycle_in_chain", [
                {"id": t1["id"], "kategoria": "DUPLIKAT", "canonical": t2["id"], "priorytet": None, "powod": "dup1"},
                {"id": t2["id"], "kategoria": "DUPLIKAT", "canonical": t1["id"], "priorytet": None, "powod": "dup2"},
            ]),
            ("canonical_to_nieaktualne", [
                {"id": t1["id"], "kategoria": "DUPLIKAT", "canonical": t2["id"], "priorytet": None, "powod": "dup1"},
                {"id": t2["id"], "kategoria": "NIEAKTUALNE", "canonical": None, "priorytet": None, "powod": "nieakt"},
            ]),
            ("zostaw_missing_priority", [
                {"id": t1["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": None, "powod": "brak prio"},
                {"id": t2["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "ok"},
            ]),
        ]

        for name, plan in test_cases:
            with self.subTest(case=name):
                runner = FakeRunner(worker_triage=plan, director_verdict="ZATWIERDZAM")
                report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner)
                self.assertEqual(report["status"], "failed")
                self.assertIn("validation-error", report["result"])

                # Verify tickets untouched
                self.assertEqual(helpdesk.load(t1["signature"])["status"], "open")
                self.assertIsNone(helpdesk.load(t1["signature"])["priority"])
                self.assertEqual(helpdesk.load(t2["signature"])["status"], "open")
                self.assertIsNone(helpdesk.load(t2["signature"])["priority"])

                state = triage_cycle.load_state()
                self.assertIn("validation-error", state["last_result"]["result"])

    def test_director_rejection_and_no_verdict_fail_closed(self):
        t1 = self.file(symptom="Dir reject test")
        plan = [{"id": t1["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "ok"}]

        # Rejection
        runner = FakeRunner(worker_triage=plan, director_verdict="ODRZUCAM", director_summary="ODRZUCAM: bledny triaz")
        report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner)
        self.assertEqual(report["status"], "failed")
        self.assertIn("ODRZUCAM", report["result"])
        self.assertIsNone(helpdesk.load(t1["signature"])["priority"])

        # No verdict / garbage
        runner2 = FakeRunner(worker_triage=plan, director_verdict="", director_summary="Niezrozumiala odpowiedz bez werdyktu")
        report2 = triage_cycle.run_cycle(force_due=True, fleet_runner=runner2)
        self.assertEqual(report2["status"], "failed")
        self.assertIn("brak-werdyktu", report2["result"])
        self.assertIsNone(helpdesk.load(t1["signature"])["priority"])

    def test_runner_failure_fails_closed(self):
        t1 = self.file(symptom="Runner failure test")
        plan = [{"id": t1["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "ok"}]

        # Worker raises exception
        runner = FakeRunner(worker_triage=plan, raise_on_role="maker")
        report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner)
        self.assertEqual(report["status"], "failed")
        self.assertIn("worker-runner-failed", report["result"])
        self.assertIsNone(helpdesk.load(t1["signature"])["priority"])

        # Worker produces no triage.json
        runner2 = FakeRunner(worker_triage=plan, skip_worker_triage_file=True)
        report2 = triage_cycle.run_cycle(force_due=True, fleet_runner=runner2)
        self.assertEqual(report2["status"], "failed")
        self.assertIn("triage.json not generated", report2["result"])

        # Director raises exception
        runner3 = FakeRunner(worker_triage=plan, raise_on_role="director")
        report3 = triage_cycle.run_cycle(force_due=True, fleet_runner=runner3)
        self.assertEqual(report3["status"], "failed")
        self.assertIn("director-runner-failed", report3["result"])

    def test_stop_disabled_locked_and_not_due(self):
        t1 = self.file(symptom="Stop test")
        runner = FakeRunner(worker_triage=[{"id": t1["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "ok"}])

        # STOP file
        self.hd.mkdir(parents=True, exist_ok=True)
        (self.hd / "STOP").write_text("", encoding="utf-8")
        report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner)
        self.assertEqual(report["result"], "stopped")
        self.assertEqual(len(runner.calls), 0)
        (self.hd / "STOP").rename(self.hd / "STOP.off")

        # Config enabled: false
        (self.hd / "config.json").write_text('{"enabled": false}', encoding="utf-8")
        report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner)
        self.assertEqual(report["result"], "stopped")
        self.assertEqual(len(runner.calls), 0)
        (self.hd / "config.json").rename(self.hd / "config.json.off")

        # Active lock with alive process
        lock_path = self.hd / "triage.lock"
        with mock.patch.object(resolve_cycle, "probe", return_value=("alive", 12345)):
            lock_path.write_text(json.dumps({"pid": 99999, "status": "active"}), encoding="utf-8")
            report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner)
            self.assertIn("locked", report["result"])
            self.assertEqual(len(runner.calls), 0)

        # Clean release of lock file allows run
        lock_path.write_text(json.dumps({"pid": 99999, "status": "released"}), encoding="utf-8")

        # Not due (0h since last run, < 10 untriaged)
        triage_cycle.save_state({"last_run": helpdesk.iso(helpdesk.now()), "last_result": "ok"})
        report = triage_cycle.run_cycle(force_due=False, fleet_runner=runner)
        self.assertIn("not-due", report["result"])
        self.assertEqual(len(runner.calls), 0)

    def test_skip_non_open_tickets(self):
        t1 = self.file(symptom="Concurrent close test primary")
        t2 = self.file(symptom="Concurrent close test secondary")

        worker_plan = [
            {"id": t1["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P1", "powod": "prio 1"},
            {"id": t2["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "prio 2"},
        ]

        def runner_hook(req_path: Path, out_path: Path) -> dict[str, Any]:
            # Close t1 while triage is in progress
            helpdesk.update(t1["signature"], "owner", "closed", "closed concurrently", force=True)
            return FakeRunner(worker_triage=worker_plan)(req_path, out_path)

        report = triage_cycle.run_cycle(force_due=True, fleet_runner=runner_hook)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["applied"]["skipped_not_open"], 1)
        self.assertEqual(report["applied"]["prioritized"], 1)

        # t1 remains closed with owner note, t2 prioritized
        self.assertEqual(helpdesk.load(t1["signature"])["status"], "closed")
        self.assertEqual(helpdesk.load(t2["signature"])["status"], "open")
        self.assertEqual(helpdesk.load(t2["signature"])["priority"], "P2")

    def test_resolve_cycle_dispatches_triage_when_due(self):
        # Case 1: dry_run -> no dispatch
        with mock.patch.object(triage_cycle, "is_due", return_value=(True, "due: test")), \
             mock.patch.object(resolve_cycle, "credit_mode", return_value=True), \
             mock.patch.object(resolve_cycle, "launch", return_value={}), \
             mock.patch.object(resolve_cycle.subprocess, "Popen") as popen_mock:
            res = resolve_cycle.cycle(dry_run=True)
            popen_mock.assert_not_called()
            self.assertNotIn("triage_dispatched", res)

        # Case 2: not dry_run, triage is NOT due -> not dispatched
        with mock.patch.object(triage_cycle, "is_due", return_value=(False, "not-due: test")), \
             mock.patch.object(triage_cycle, "is_owner_queue_due", return_value=(False, "not-due: owner")), \
             mock.patch.object(resolve_cycle, "credit_mode", return_value=True), \
             mock.patch.object(resolve_cycle, "launch", return_value={}), \
             mock.patch.object(resolve_cycle.subprocess, "Popen") as popen_mock:
            res = resolve_cycle.cycle(dry_run=False)
            popen_mock.assert_not_called()
            self.assertFalse(res["triage_dispatched"]["dispatched"])

        # Case 3: not dry_run, triage IS due -> Popen called in background
        fake_proc = mock.Mock(pid=5555)
        with mock.patch.object(triage_cycle, "is_due", return_value=(True, "due: 10 untriaged")), \
             mock.patch.object(triage_cycle, "is_owner_queue_due", return_value=(False, "not-due: owner")), \
             mock.patch.object(resolve_cycle, "credit_mode", return_value=True), \
             mock.patch.object(resolve_cycle, "launch", return_value={}), \
             mock.patch.object(resolve_cycle.subprocess, "Popen", return_value=fake_proc) as popen_mock:
            res = resolve_cycle.cycle(dry_run=False)
            popen_mock.assert_called_once()
            args, kwargs = popen_mock.call_args
            self.assertIn("triage_cycle.py", args[0][1])
            self.assertIn("run", args[0][2])
            self.assertEqual(res["triage_dispatched"]["pid"], 5555)

    def test_resolve_cycle_dispatches_owner_queue_when_triage_not_due(self):
        fake_proc = mock.Mock(pid=7777)
        with mock.patch.object(triage_cycle, "is_due", return_value=(False, "not-due: triage")), \
             mock.patch.object(triage_cycle, "is_owner_queue_due", return_value=(True, "due: owner-queue")), \
             mock.patch.object(resolve_cycle, "credit_mode", return_value=True), \
             mock.patch.object(resolve_cycle, "launch", return_value={}), \
             mock.patch.object(resolve_cycle.subprocess, "Popen", return_value=fake_proc) as popen_mock:
            res = resolve_cycle.cycle(dry_run=False)
        popen_mock.assert_called_once()
        self.assertEqual(res["triage_dispatched"]["pid"], 7777)
        self.assertTrue(res["triage_dispatched"]["owner_queue_due"])


class OwnerQueueTests(TriageBase):
    def test_owner_queue_decisions_applied_and_journaled(self):
        t_cand_a = self.file_candidate(symptom="Candidate issue alpha")
        t_cand_b = self.file_candidate(symptom="Candidate issue beta")
        t_needs_c = self.file_needs_owner(symptom="Needs owner issue gamma")
        t_needs_d = self.file_needs_owner(symptom="Needs owner issue delta")
        t_young = self.file_candidate(symptom="Young candidate issue", hours_ago=2.0)

        director_summary = (
            "Decyzje Dyrektora dla kolejki candidate / needs-owner:\n"
            "| id | A/B/C/D | tresc decyzji | warunki |\n"
            f"| {t_cand_a['id']} | A | zamykamy ten defekt | usterka nie do odtworzenia |\n"
            f"| {t_cand_b['id']} | B | wdrazamy rozwiazanie | testy, ACCEPT innego rodowodu, kanarek, rollback |\n"
            f"| {t_needs_c['id']} | C | wybrano model lokalny | brak wymogow chmurowych |\n"
            f"| {t_needs_d['id']} | D | zapytaj Mateusza o scope | czy rozszerzamy uprawnienia dla bazy? |\n"
        )

        runner = FakeRunner(
            director_summary=director_summary,
            director_model="gemini-4-argon-high",
        )

        recorded_decisions = []

        def fake_decisions_recorder(**kwargs):
            recorded_decisions.append(kwargs)

        report = triage_cycle.run_owner_queue(fleet_runner=runner, decisions_recorder=fake_decisions_recorder)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["applied"]["closed"], 1)
        self.assertEqual(report["applied"]["reopened_wdroz"], 1)
        self.assertEqual(report["applied"]["reopened_techniczna"], 1)
        self.assertEqual(report["applied"]["needs_owner"], 0)
        self.assertEqual(report["applied"]["ignored_non_t2_mateusz"], 1)
        self.assertEqual(report["applied"]["decisions_recorded"], 3)

        # Verify t_cand_a: A -> closed
        a_loaded = helpdesk.load(t_cand_a["signature"])
        self.assertEqual(a_loaded["status"], "closed")
        self.assertIn("decyzja A (ZAMKNIJ): zamykamy ten defekt", a_loaded["history"][-1]["note"])
        self.assertIn("Warunek ponownego otwarcia: usterka nie do odtworzenia", a_loaded["history"][-1]["note"])
        self.assertEqual(a_loaded["history"][-1]["by"], "rsi-director:gemini-4-argon-high")

        # Verify t_cand_b: B -> open
        b_loaded = helpdesk.load(t_cand_b["signature"])
        self.assertEqual(b_loaded["status"], "open")
        self.assertIn("decyzja B (WDROZ): wdrazamy rozwiazanie", b_loaded["history"][-1]["note"])
        self.assertIn("Instrukcja wdrozenia: testy, ACCEPT innego rodowodu, kanarek, rollback", b_loaded["history"][-1]["note"])
        self.assertEqual(b_loaded["history"][-1]["by"], "rsi-director:gemini-4-argon-high")
        self.assertIsNone(b_loaded.get("claim"))
        self.assertFalse(helpdesk.owner_resumed(b_loaded))

        # Verify t_needs_c: C -> open
        c_loaded = helpdesk.load(t_needs_c["signature"])
        self.assertEqual(c_loaded["status"], "open")
        self.assertIn("decyzja C (TECHNICZNA): wybrano model lokalny", c_loaded["history"][-1]["note"])
        self.assertEqual(c_loaded["history"][-1]["by"], "rsi-director:gemini-4-argon-high")

        # D for a non-T2 ticket is ignored without changing status or history.
        d_loaded = helpdesk.load(t_needs_d["signature"])
        self.assertEqual(d_loaded["status"], "needs-owner")
        self.assertEqual(len(d_loaded["history"]), len(t_needs_d["history"]))

        # Verify t_young untouched
        young_loaded = helpdesk.load(t_young["signature"])
        self.assertEqual(young_loaded["status"], "candidate")
        self.assertEqual(len(young_loaded["history"]), len(t_young["history"]))

        # Verify decisions journal recordings
        self.assertEqual(len(recorded_decisions), 4)
        for rec in recorded_decisions:
            self.assertEqual(rec["project"], "agent-stack")
            self.assertEqual(rec["category"], "technical")
            self.assertEqual(rec["decided_by"], "director:gemini-4-argon-high")
            self.assertIn("Zgloszenie", rec["question"])

    def test_owner_queue_t2_gate_skips_sensitive_tickets(self):
        t_sec = self.file_candidate(symptom="Wyciek sekret i token API w naglowkach")
        t_iam = self.file_candidate(symptom="Brak uprawnienia IAM w roli wykonawcy")
        t_del = self.file_candidate(symptom="Kasowanie plikow i usuniecie bazy danych")
        t_logic = self.file_needs_owner(symptom="Zwykly blad", impact="Wymaga zmiany logiki biznesowej procesu")
        t_contact = self.file_needs_owner(symptom="Kontakt z ludzmi i klientem w sprawach wspolpracy")
        t_cost = self.file_needs_owner(symptom="Nowe platne koszty API i platnosci")
        t_client = self.file_needs_owner(symptom="Awaria produkcja klienta w runtime")
        t_normal = self.file_candidate(symptom="Blad formatowania tekstu w module CLI")

        director_summary = (
            "Decyzja Dyrektora:\n"
            "| id | A/B/C/D | tresc decyzji | warunki |\n"
            f"| {t_normal['id']} | B | wdroz poprawke CLI | testy, ACCEPT, kanarek, rollback |\n"
        )

        runner = FakeRunner(director_summary=director_summary)
        report = triage_cycle.run_owner_queue(fleet_runner=runner)

        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["t2_skipped_count"], 7)
        self.assertEqual(set(report["t2_waiting"]), {t["id"] for t in
                         (t_sec, t_iam, t_del, t_logic, t_contact, t_cost, t_client)})
        self.assertEqual(set(triage_cycle.load_state()["last_result"]["t2_waiting"]), set(report["t2_waiting"]))
        self.assertEqual(report["eligible_count"], 1)

        # Verify tickets sent to runner in director-req
        req_call = runner.calls[0]
        req_data = json.loads(req_call["req_path"].read_text(encoding="utf-8"))
        ws = Path(req_data["workspace"])
        tickets_in_ws = json.loads((ws / "owner-queue-tickets.json").read_text(encoding="utf-8"))
        self.assertEqual(len(tickets_in_ws), 1)
        self.assertEqual(tickets_in_ws[0]["id"], t_normal["id"])

        # Sensitive tickets remain untouched
        self.assertEqual(helpdesk.load(t_sec["signature"])["status"], "candidate")
        self.assertEqual(helpdesk.load(t_iam["signature"])["status"], "candidate")
        self.assertEqual(helpdesk.load(t_del["signature"])["status"], "candidate")
        self.assertEqual(helpdesk.load(t_logic["signature"])["status"], "needs-owner")
        self.assertEqual(helpdesk.load(t_contact["signature"])["status"], "needs-owner")
        self.assertEqual(helpdesk.load(t_cost["signature"])["status"], "needs-owner")
        self.assertEqual(helpdesk.load(t_client["signature"])["status"], "needs-owner")

        # Normal ticket was updated
        self.assertEqual(helpdesk.load(t_normal["signature"])["status"], "open")

    def test_owner_queue_fail_closed_on_error_or_missing_table(self):
        t1 = self.file_candidate(symptom="Fail closed candidate")

        # Case A: runner raises
        runner_err = FakeRunner(raise_on_role="director")
        report = triage_cycle.run_owner_queue(fleet_runner=runner_err)
        self.assertEqual(report["status"], "failed")
        self.assertIn("director-runner-failed", report["result"])
        self.assertEqual(helpdesk.load(t1["signature"])["status"], "candidate")

        # Case B: missing table in director output
        runner_no_table = FakeRunner(director_summary="Odrzucam propozycje bez tabeli.")
        report2 = triage_cycle.run_owner_queue(fleet_runner=runner_no_table)
        self.assertEqual(report2["status"], "failed")
        self.assertIn("brak tabeli", report2["result"])
        self.assertEqual(helpdesk.load(t1["signature"])["status"], "candidate")

        # Case C: STOP file
        (self.hd / "STOP").write_text("", encoding="utf-8")
        report3 = triage_cycle.run_owner_queue(fleet_runner=runner_err)
        self.assertEqual(report3["status"], "stopped")
        (self.hd / "STOP").rename(self.hd / "STOP.off")

    def test_owner_queue_fails_closed_when_runner_reports_no_status_at_all(self):
        """A parsable decision table without a runner status applies nothing.

        Fails on the pre-fix code: runner_output_is_successful() accepted a
        missing or empty status, so the owner queue applied the A/B/C rows.
        """
        t1 = self.file_candidate(symptom="Owner queue runner without status")
        table = (
            "| id | A/B/C/D | tresc decyzji | warunki |\n"
            f"| {t1['id']} | A | zamknij zgloszenie | test |\n"
        )

        for name, out_payload in (("missing-status", {}), ("empty-status", {"status": ""})):
            with self.subTest(case=name):
                def runner(req_path: Path, out_path: Path) -> dict[str, Any]:
                    out = dict(out_payload)
                    out["handoff"] = {"summary": table}
                    return out

                report = triage_cycle.run_owner_queue(fleet_runner=runner)
                self.assertEqual(report["status"], "failed")
                self.assertIn("director-runner-failed", report["result"])
                stored = helpdesk.load(t1["signature"])
                self.assertEqual(stored["status"], "candidate")
                self.assertIsNone(stored.get("resolved_at"))
                self.assertFalse(helpdesk.owner_resumed(stored))

    def test_owner_queue_concurrent_status_change_skipped(self):
        t1 = self.file_candidate(symptom="Concurrent candidate primary")
        t2 = self.file_candidate(symptom="Concurrent candidate secondary")

        director_summary = (
            "| id | A/B/C/D | tresc decyzji | warunki |\n"
            f"| {t1['id']} | A | zamknij 1 | test |\n"
            f"| {t2['id']} | B | wdroz 2 | test |\n"
        )

        def runner_hook(req_path: Path, out_path: Path) -> dict[str, Any]:
            # While Director is thinking, t1 is closed by owner
            helpdesk.update(t1["signature"], "owner", "closed", "closed concurrently", force=True)
            return FakeRunner(director_summary=director_summary)(req_path, out_path)

        report = triage_cycle.run_owner_queue(fleet_runner=runner_hook)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["applied"]["skipped_status_changed"], 1)
        self.assertEqual(report["applied"]["reopened_wdroz"], 1)

        # t1 remains closed with owner note, t2 reopened
        self.assertEqual(helpdesk.load(t1["signature"])["status"], "closed")
        self.assertIn("closed concurrently", helpdesk.load(t1["signature"])["history"][-1]["note"])
        self.assertEqual(helpdesk.load(t2["signature"])["status"], "open")

    def test_owner_queue_skips_claim_and_same_status_updated_after_snapshot(self):
        claimed = self.file_candidate(symptom="Claim during director decision")
        changed = self.file_candidate(symptom="Note during director decision")
        table = (
            f"| {claimed['id']} | A | close | reason |\n"
            f"| {changed['id']} | A | close | reason |\n"
        )

        def runner_hook(req_path: Path, out_path: Path) -> dict[str, Any]:
            helpdesk.claim(claimed["signature"], "concurrent-resolver", 60)
            helpdesk.set_priority(changed["signature"], "P1", "owner", "same status, newer timestamp")
            return FakeRunner(director_summary=table)(req_path, out_path)

        report = triage_cycle.run_owner_queue(fleet_runner=runner_hook)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["applied"]["skipped_status_changed"], 2)
        self.assertEqual(helpdesk.load(claimed["signature"])["status"], "claimed")
        changed_ticket = helpdesk.load(changed["signature"])
        self.assertEqual(changed_ticket["status"], "candidate")
        self.assertEqual(changed_ticket["priority"], "P1")

    def test_owner_queue_recorder_failure_does_not_undo_or_abort_decisions(self):
        first = self.file_candidate(symptom="Recorder fails first")
        second = self.file_candidate(symptom="Recorder keeps going")
        table = (
            f"| {first['id']} | A | close | fixed |\n"
            f"| {second['id']} | A | close | fixed |\n"
        )

        def bad_recorder(**kwargs):
            raise RuntimeError("journal unavailable")

        report = triage_cycle.run_owner_queue(
            fleet_runner=FakeRunner(director_summary=table), decisions_recorder=bad_recorder)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["applied"]["closed"], 2)
        self.assertEqual(len(report["applied"]["recorder_errors"]), 2)
        state = triage_cycle.load_state()
        self.assertEqual(state["last_result"]["recorder_errors"], report["applied"]["recorder_errors"])
        self.assertIsNotNone(state["last_owner_queue_run"])

    def test_owner_queue_failure_does_not_advance_success_clock(self):
        self.file_candidate(symptom="Owner queue failure clock")
        triage_cycle.save_state({"last_run": None, "last_owner_queue_run": "2026-10-20T00:00:00Z",
                                 "last_result": None, "owner_queue_last_result": None})
        report = triage_cycle.run_owner_queue(fleet_runner=FakeRunner(raise_on_role="director"))
        self.assertEqual(report["status"], "failed")
        state = triage_cycle.load_state()
        self.assertEqual(state["last_owner_queue_run"], "2026-10-20T00:00:00Z")
        self.assertEqual(state["owner_queue_last_result"]["status"], "failed")

    def test_owner_queue_due_uses_independent_six_hour_clock(self):
        self.file_candidate(symptom="Owner queue deadline")
        due, _ = triage_cycle.is_owner_queue_due()
        self.assertTrue(due)
        triage_cycle.save_state({"last_owner_queue_run": helpdesk.iso(helpdesk.now() - dt.timedelta(hours=5))})
        due, reason = triage_cycle.is_owner_queue_due()
        self.assertFalse(due)
        self.assertIn("< 6h", reason)
        triage_cycle.save_state({"last_owner_queue_run": helpdesk.iso(helpdesk.now() - dt.timedelta(hours=7))})
        due, _ = triage_cycle.is_owner_queue_due()
        self.assertTrue(due)

    def test_owner_queue_no_tickets(self):
        # No tickets > 24h
        self.file_candidate(symptom="Fresh candidate", hours_ago=1.0)
        runner = FakeRunner()
        report = triage_cycle.run_owner_queue(fleet_runner=runner)
        self.assertEqual(report["status"], "completed")
        self.assertIn("0 tickets", report["result"])
        self.assertEqual(len(runner.calls), 0)

    def test_owner_queue_missing_decisions_cli_skips_journal_gracefully(self):
        t1 = self.file_candidate(symptom="Candidate for journal skip test")
        director_summary = (
            "| id | A/B/C/D | tresc decyzji | warunki |\n"
            f"| {t1['id']} | A | zamknij | usterka rozwiazana |\n"
        )
        runner = FakeRunner(director_summary=director_summary)
        report = triage_cycle.run_owner_queue(fleet_runner=runner, decisions_recorder=None)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["applied"]["closed"], 1)
        self.assertEqual(report["applied"]["decisions_recorded"], 0)
        self.assertIn("brak triage.decisions_cli", report["result"])

    def test_owner_queue_cli_and_run_after_triage(self):
        # 1. Test CLI owner-queue --dry-run
        t1 = self.file_candidate(symptom="CLI dry run test")
        director_summary = (
            "| id | A/B/C/D | tresc decyzji | warunki |\n"
            f"| {t1['id']} | A | zamknij | usterka rozwiazana |\n"
        )
        with mock.patch.object(triage_cycle, "default_fleet_runner", FakeRunner(director_summary=director_summary)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ret = triage_cycle.main(["owner-queue", "--dry-run"])
            self.assertEqual(ret, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["status"], "completed")
            self.assertTrue(data["dry_run"])
            # In dry-run, t1 is still candidate!
            self.assertEqual(helpdesk.load(t1["signature"])["status"], "candidate")

        # 2. Test run_cycle runs owner-queue after triage
        t_open = self.file(symptom="Untriaged open issue")
        t_cand = self.file_candidate(symptom="Candidate ready for director")

        worker_plan = [
            {"id": t_open["id"], "kategoria": "ZOSTAW", "canonical": None, "priorytet": "P2", "powod": "ok"},
        ]
        triage_summary = "ZATWIERDZAM"
        oq_table = (
            "| id | A/B/C/D | tresc decyzji | warunki |\n"
            f"| {t_cand['id']} | A | zamknij candidate | warunek |\n"
        )

        combined_runner = FakeRunner(
            worker_triage=worker_plan,
            director_summary=triage_summary,
            owner_queue_summary=oq_table,
        )

        cycle_report = triage_cycle.run_cycle(force_due=True, fleet_runner=combined_runner)
        self.assertEqual(cycle_report["status"], "completed")
        self.assertIn("owner_queue", cycle_report)
        self.assertEqual(cycle_report["owner_queue"]["status"], "completed")
        self.assertEqual(cycle_report["owner_queue"]["applied"]["closed"], 1)

        # Both triage and owner-queue took effect
        self.assertEqual(helpdesk.load(t_open["signature"])["priority"], "P2")
        self.assertEqual(helpdesk.load(t_cand["signature"])["status"], "closed")

    def test_owner_queue_result_is_recorded_separately_from_the_triage_result(self):
        t_open = self.file(symptom="Untriaged open issue for split result")
        t_cand = self.file_candidate(symptom="Candidate for split result")
        worker_plan = [
            {"id": t_open["id"], "kategoria": "ZOSTAW", "canonical": None,
             "priorytet": "P2", "powod": "ok"},
        ]
        oq_table = (
            "| id | A/B/C/D | tresc decyzji | warunki |\n"
            f"| {t_cand['id']} | A | zamknij candidate | warunek |\n"
        )
        combined_runner = FakeRunner(
            worker_triage=worker_plan,
            director_summary="ZATWIERDZAM",
            owner_queue_summary=oq_table,
        )

        report = triage_cycle.run_cycle(force_due=True, fleet_runner=combined_runner)
        self.assertEqual(report["status"], "completed")

        state = triage_cycle.load_state()
        # The triage outcome stays in last_result ...
        self.assertIn("1 prioritized", state["last_result"]["result"])
        # ... and the owner-queue outcome stays in its own field.
        self.assertEqual(state["owner_queue_last_result"]["status"], "completed")
        self.assertIn("1 closed", state["owner_queue_last_result"]["result"])
        self.assertNotEqual(state["owner_queue_last_result"]["result"],
                            state["last_result"]["result"])


if __name__ == "__main__":
    unittest.main()
