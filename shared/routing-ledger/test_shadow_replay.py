import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shadow_replay as sr  # noqa: E402


def selection(i, role, agent, *, status="CANDIDATE", verdict=None, run_id=None, candidates=None, rnd=1):
    return {"type": "selection", "decision": f"d{i}{role}", "graph_run": f"g{i}", "task": f"t{i}", "round": rnd,
            "role": role, "features": {"kind": None, "complexity": "low", "scope_files": 2, "risk": "low"},
            "candidates": candidates if candidates is not None else
            [{"agent": agent, "model": "m", "score": 80}, {"agent": "other", "model": "m2", "score": 60}],
            "tier_order": [{"agent": agent, "model": "m", "tier": "K", "effective": 70.0, "recent_share": 0.1}],
            "choice": {"agent": agent, "model": "m", "run_id": run_id},
            "outcome": {"status": status, "verdict": verdict}}


def dispatch(run_id, minute):
    return {"type": "dispatch", "run_id": run_id, "started_at": f"2026-10-01T00:{minute:02d}:00Z",
            "features": {"kind": "implementation", "mode": "implement", "complexity": "low"}}


def ledger_rows(n=20):
    rows = []
    for i in range(n):
        agent = "good" if i % 2 else "bad"
        rows.append(selection(i, "maker", agent, run_id=f"r{i}"))
        rows.append(dispatch(f"r{i}", i))
        rows.append(selection(i, "reviewer", "rev", status="REVIEWED", verdict="ACCEPT" if agent == "good" else "REPAIR"))
    rows.append(selection(99, "maker", "x", run_id="r99", rnd=2))  # round 2: excluded
    rows.append(dispatch("r99", 59))
    return rows


class ShadowReplayTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.ledger = self.base / "ledger-x.jsonl"
        self.ledger.write_text("\n".join(json.dumps(r) for r in ledger_rows()) + "\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_dataset_labels_and_time_split_without_leakage(self):
        rows = sr.build_dataset(*sr.load_ledger(self.ledger))
        self.assertEqual(len(rows), 20)
        self.assertEqual(sum(r["label"] for r in rows), 10)
        self.assertEqual(rows[0]["features"]["kind"], "implementation")  # filled from dispatch
        train, test = sr.split(rows)
        self.assertTrue(max(r["time"] for r in train) < min(r["time"] for r in test))
        model = sr.fit_b1(train)
        self.assertEqual(model[1][("good",)][1], sum(1 for r in train if r["chosen"]["agent"] == "good"))

    def test_prompt_has_only_ledger_features_and_no_outcome(self):
        row = sr.build_dataset(*sr.load_ledger(self.ledger))[0]
        table = sr.candidate_table(row)
        prompt = json.loads(sr.build_prompt(row, table))
        self.assertEqual(set(prompt["task"]), set(sr.PROMPT_FEATURES))
        text = json.dumps(prompt)
        for leaked in ("ACCEPT", "REPAIR", "verdict", "label", "status", "run_id", "time"):
            self.assertNotIn(leaked, text)
        self.assertEqual([c["id"] for c in table], ["c1", "c2"])
        self.assertEqual(sr.chosen_id(row, table), "c1")

    def test_substituted_choice_is_added_as_candidate(self):
        row = sr.build_dataset(*sr.load_ledger(self.ledger))[0]
        row["chosen"] = {"agent": "sub", "model": "s"}
        table = sr.candidate_table(row)
        self.assertEqual((table[-1]["agent"], table[-1]["planner_score"]), ("sub", None))
        self.assertEqual(sr.chosen_id(row, table), table[-1]["id"])

    def test_replay_is_resumable_and_evaluation_ranks_signal(self):
        rows = sr.build_dataset(*sr.load_ledger(self.ledger))
        train, test = sr.split(rows)
        calls = []

        def fake_ask(prompt, table):
            calls.append(prompt)
            good = json.loads(prompt)["candidates"][0]["agent"] == "good"
            return {c["id"]: (90 if good else 10) for c in table}, 1.0, 100, 10

        out = self.base / "res.jsonl"
        sr.replay(test, out, ask=fake_ask, waiting=lambda: 0, log=lambda _m: None)
        sr.replay(test, out, ask=fake_ask, waiting=lambda: 0, log=lambda _m: None)
        self.assertEqual(len(calls), len(test))  # second pass skipped everything
        results = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        metrics = sr.evaluate(train, test, results)
        self.assertEqual(metrics["auc"]["B2_qwen"], 1.0)
        self.assertEqual(metrics["auc"]["B0_planner"], 0.5)  # constant planner score carries no signal
        self.assertEqual(metrics["schema_valid_rate"], 1.0)

    def test_errors_are_recorded_not_retried(self):
        rows = sr.build_dataset(*sr.load_ledger(self.ledger))
        out = self.base / "err.jsonl"

        def broken(prompt, table):
            raise ValueError("SCHEMA_MISMATCH")

        sr.replay(rows[:3], out, ask=broken, waiting=lambda: 0, log=lambda _m: None)
        statuses = [json.loads(line)["status"] for line in out.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(statuses, ["ERROR"] * 3)

    def test_review_regressions(self):
        # FAILED maker without verdict is a negative; ACCEPT after a failed maker is not a clean success.
        rows = [selection(1, "maker", "a", status="FAILED", run_id="r1"), dispatch("r1", 1),
                selection(2, "maker", "a", status="BLOCKED", run_id="r2"), dispatch("r2", 2),
                selection(2, "reviewer", "rev", status="REVIEWED", verdict="ACCEPT")]
        data = sr.build_dataset([r for r in rows if r["type"] == "selection"],
                                {r["run_id"]: r for r in rows if r["type"] == "dispatch"})
        self.assertEqual([r["label"] for r in data], [0, 0])
        # Boundary timestamps never straddle train/test.
        same = [{"time": "t1", "id": str(i)} for i in range(3)] + [{"time": "t2", "id": str(i)} for i in range(3, 10)]
        train, test = sr.split(sorted(same, key=lambda r: (r["time"], r["id"])))
        self.assertTrue(all(r["time"] == "t1" for r in train) and all(r["time"] == "t2" for r in test))
        # Free text in a feature value is dropped before the prompt.
        row = sr.build_dataset(*sr.load_ledger(self.ledger))[0]
        row["features"]["risk"] = "client ACME secret plan"
        row["features"]["required_tools"] = ["filesystem-read", "see C:/x/plan.md"]
        prompt = sr.build_prompt(row, sr.candidate_table(row))
        self.assertNotIn("ACME", prompt)
        self.assertNotIn("plan.md", prompt)
        # Substituted choice is the planner's lowest preference, not a train median.
        row["chosen"] = {"agent": "sub", "model": "s"}
        self.assertEqual(sr.b0_score(row), 0.0)
        self.assertIsNone(sr.bootstrap_diff([1, 2], [1, 2], [1, 1], iterations=5))

    def test_closed_vocabulary_and_cluster_bootstrap(self):
        row = sr.build_dataset(*sr.load_ledger(self.ledger))[0]
        row["features"].update(kind="AcmeClientMigration", mode="fix_acme_invoices", complexity="low",
                               required_tools=["acme_secret_tool"], checks="pytest", scope_ext={".py": 2, "acme": 1})
        table = sr.candidate_table(row)
        table[0]["agent"] = "ACME_Client"
        prompt = json.loads(sr.build_prompt(row, table))
        text = json.dumps(prompt)
        self.assertNotIn("cme", text.lower().replace("scope", ""))
        self.assertEqual((prompt["task"]["kind"], prompt["task"]["mode"]), ("other", "other"))
        self.assertEqual(prompt["task"]["required_tools"], ["other"])
        self.assertEqual((prompt["task"]["checks"], prompt["task"]["scope_ext"]), (0, {".py": 2}))
        self.assertEqual(prompt["candidates"][0]["agent"], "other")
        # Graph-clustered bootstrap resamples whole graphs and still returns a paired interval.
        a, b = [0.1, 0.2, 0.8, 0.9, 0.3, 0.7], [0.5] * 6
        labels, graphs = [0, 0, 1, 1, 0, 1], ["g1", "g1", "g2", "g2", "g3", "g3"]
        result = sr.bootstrap_diff(a, b, labels, clusters=graphs, iterations=200)
        self.assertTrue(result["ci95"][0] <= result["mean"] <= result["ci95"][1])

    def test_k4_and_k5_inputs(self):
        sel = selection(1, "maker", "a", status="BLOCKED", run_id="r1")
        sel["outcome"]["failure_code"] = "HANDOFF_MALFORMED"
        sel["preflight_skipped"] = [{"agent": "other", "model": "m2", "code": "PREFLIGHT_GATES"}]
        row = sr.build_dataset([sel], {"r1": dispatch("r1", 1)})[0]
        self.assertTrue(row["routing_failure"])
        self.assertEqual(row["gate_failed"], [("other", "m2")])
        self.assertNotIn("HANDOFF", sr.build_prompt(row, sr.candidate_table(row)))
        result = {"id": row["id"], "status": "OK", "chosen": "c1", "n_candidates": 2,
                  "fit": {"c1": 10, "c2": 90}, "latency_s": 1.0}
        metrics = sr.evaluate([row], [row], [result])
        self.assertEqual(metrics["qwen_top_failed_gate"], 1)
        self.assertFalse(metrics["verdict"]["K5_no_deny_or_gate_failed_top"])
        self.assertEqual(metrics["k4_low_rated_precision"]["B2_qwen"]["precision"], 1.0)

    def test_tie_handling_k4_k5(self):
        rows = []
        for i in range(10):
            sel = selection(i, "maker", "a", status="BLOCKED" if i < 5 else "CANDIDATE", run_id=f"r{i}")
            sel["outcome"]["failure_code"] = "PROVIDER_FAILURE" if i < 5 else None
            sel["preflight_skipped"] = [{"agent": "other", "model": "m2"}] if i == 9 else []
            rows.append((sel, dispatch(f"r{i}", i)))
        reviews = [selection(i, "reviewer", "rev", status="REVIEWED", verdict="ACCEPT") for i in range(5, 10)]
        data = sr.build_dataset([s for s, _ in rows] + reviews, {d["run_id"]: d for _, d in rows})
        self.assertEqual(len(data), 10)
        # All Qwen scores tied: the bottom 20% is exactly 2 rows, precision = base rate (0.5).
        results = [{"id": r["id"], "status": "OK", "chosen": "c1", "n_candidates": 2,
                    "fit": {"c1": 50, "c2": 50}, "latency_s": 1.0} for r in data]
        metrics = sr.evaluate(data, data, results)
        k4 = metrics["k4_low_rated_precision"]["B2_qwen"]
        self.assertEqual((k4["n_low"], k4["n_tied_at_cut"], k4["precision"]), (2, 10, 0.5))
        # Tied maximum includes the gate-failed c2 in row 9: K5 must see it.
        self.assertEqual(metrics["qwen_top_failed_gate"], 1)
        self.assertFalse(metrics["verdict"]["K5_no_deny_or_gate_failed_top"])

    def test_auc_and_local_only_endpoint(self):
        self.assertEqual(sr.auc([1, 2, 3, 4], [0, 0, 1, 1]), 1.0)
        self.assertEqual(sr.auc([1, 1, 1, 1], [0, 1, 0, 1]), 0.5)
        self.assertIsNone(sr.auc([1, 2], [1, 1]))
        with self.assertRaises(ValueError):
            sr.ask_local("{}", [{"id": "c1"}], endpoint="https://openrouter.ai/api/v1")
        for bad in ("http://localhost:11440@remote.example/v1", "http://user:pw@127.0.0.1:11440/v1",
                    "http://127.0.0.1.evil.example:11440/v1"):
            with self.assertRaises(ValueError):
                sr.ask_local("{}", [{"id": "c1"}], endpoint=bad)


if __name__ == "__main__":
    unittest.main()
