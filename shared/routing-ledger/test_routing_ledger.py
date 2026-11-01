import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import routing_ledger as rl  # noqa: E402

SECRET_TEXT = "Klient ACME sp. z o.o. przychod 1234567 PLN"
SECRET_PATH = r"C:\Users\someone\clients\acme\plan.md"


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def tokens(total):
    return {"status": "known", "input_tokens": total - 10, "output_tokens": 10, "total_tokens": total}


class Fixture:
    def __init__(self, base):
        self.base = Path(base)
        self.runs = self.base / "agents" / "agent-fleet" / "runs"
        # Graph run outside the walked roots: reachable only through Fleet's output_dir.
        self.tree = self.base / "projects" / "app" / "graph-run"
        self.out = self.base / "out"
        put(self.runs / "run-1.json", {
            "run_id": "run-1", "agent": "codex", "model": "gpt-x", "model_provider_id": "openai",
            "kind": "implementation", "mode": "implement", "complexity": "medium",
            "run_status": "COMPLETED", "completion_reason": "completed",
            "token_usage": tokens(1000), "duration_seconds": 12.5,
            "handoff": {"summary": SECRET_TEXT, "status": "completed"},
            "route_event": {"routing_mode": "todo-fit-quota", "assignment_score": 90,
                            "gate_results": [{"code": "mode", "passed": True}]},
            "root_task_id": "task-abc", "workspace": SECRET_PATH,
            "output_dir": str(self.tree / "run1" / "node-a" / "1" / "maker" / "fleet" / "run-1")})
        put(self.runs / "run-2.json", {
            "run_id": "run-2", "agent": "zcode", "model": "glm-x", "kind": "review", "mode": "review",
            "run_status": "FAILED", "completion_reason": "process-exit-1",
            "handoff_parse_error": "PROVIDER FAILURE (not a handoff problem): " + SECRET_TEXT,
            "token_usage": tokens(500)})
        put(self.runs / "run-3.json", {
            "run_id": "run-3", "agent": "muse", "model": "muse-x", "kind": "implementation",
            "run_status": "PARTIAL", "completion_reason": "parent-timeout",
            "token_usage": {"status": "unknown", "total_tokens": None}})
        role = self.tree / "run1" / "node-a" / "1" / "maker"
        put(role / "request.json", {
            "goal": SECRET_TEXT, "role": "maker", "workspace": SECRET_PATH, "feedback": [],
            "task": {"id": "node-a", "text": SECRET_TEXT, "scope": ["api/x.py", "tests/test_x.py"],
                     "inputs": ["docs/a.md"], "done_when": ["works"], "checks": ["pytest"],
                     "maker": [{"agent": "zcode", "model": "glm-x", "score": 80},
                               {"agent": "codex", "model": "gpt-x", "score": 70}],
                     "reviewer": [{"agent": "claude", "model": "opus-x", "score": 85}]}})
        put(role / "assessment.json", {"task_summary": SECRET_TEXT, "complexity": "medium", "risk": "low",
                                       "required_tools": ["filesystem-write"], "delegation_mode": "ONE"})
        put(role / "preflight-skipped.json", [{"agent": "zcode", "model": "glm-x",
                                               "reason": "ZCode read admission refused: " + SECRET_PATH}])
        put(role / "tier-selection.json", {"enabled": True, "ordered": [
            {"agent": "codex", "model": "gpt-x", "tier": "M", "allowed": True, "base": 70.0, "effective": 68.0}]})
        maker_result = {"status": "DONE", "agent": "codex", "model": "gpt-x", "lineage": "openai",
                        "run_id": "run-1", "tokens": tokens(1000), "reason": None}
        put(role / "result.json", maker_result)
        # Mirror of the same role folder must not double count the decision.
        mirror = self.base / "agents" / "mirrors" / "node-a" / "1" / "maker"
        for name in ("request.json", "assessment.json", "preflight-skipped.json",
                     "tier-selection.json", "result.json"):
            mirror.mkdir(parents=True, exist_ok=True)
            (mirror / name).write_bytes((role / name).read_bytes())
        review = {"status": "REVIEWED", "agent": "claude", "model": "opus-x", "run_id": "run-r",
                  "verdict": "REPAIR", "findings": [SECRET_TEXT], "summary": SECRET_TEXT,
                  "tokens": tokens(300)}
        put(self.tree / "run1" / "state.json", {"plan_hash": "p1", "nodes": {
            "node-a": {"status": "BLOCKED", "round": 2, "reason": "Pool availability: exhausted " + SECRET_TEXT,
                       "history": [{"role": "maker", "result": maker_result},
                                   {"role": "reviewer", "result": review, "artifact": {}, "tests": []},
                                   {"role": "maker", "result": {"status": "BLOCKED", "agent": "zcode",
                                                                "reason": "ZCode read admission refused",
                                                                "tokens": tokens(200)}}]}}})
        # A resumed run copies the earlier history; its Fleet runs must not be counted twice.
        resumed = json.loads((self.tree / "run1" / "state.json").read_text(encoding="utf-8"))
        resumed["plan_hash"] = "p2"
        put(self.tree / "run2" / "state.json", resumed)
        jev = {
            "schema_version": 1, "consultation_status": "AVAILABLE", "created_at": "2026-10-01T00:00:00Z",
            "binding": {"task_id": "node-a"}, "execution": "NOT_DISPATCHED",
            "decision": {"difficulty": "medium", "parallelism": "parallel", "consultation": "none",
                         "fit": {"c1": 0.8}, "usage": {"cost_usd": 0.0003}},
            "recommendation": {"requires_parent_review": False}}
        jev["decision"]["decision_id"] = "gen-dec-1"
        put(self.tree / "run1" / "jev-report-a.json", jev)
        put(self.tree / "run1" / "reused.json", jev)  # same API answer, counted once

    def digest_sources(self):
        out = {}
        for path in sorted(self.base.rglob("*")):
            if self.out in path.parents:
                continue
            if path.is_file():
                out[str(path)] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
        return out


class RoutingLedgerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(self._tmp.name)
        self.now = datetime(2026, 10, 11, 20, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self._tmp.cleanup()

    def build(self):
        # run1 is reachable only via Fleet output_dir; run2 (resumed copy) via an explicit root.
        return rl.build([self.fx.base / "agents", self.fx.tree / "run2"], self.fx.runs, self.fx.out, now=self.now)

    def test_counts_waste_and_zcode_refusals(self):
        _, _, summary = self.build()
        self.assertEqual(summary["counts"]["dispatches"], 3)
        self.assertEqual(summary["counts"]["selections"], 1)  # mirror deduplicated by run_id
        self.assertEqual(summary["counts"]["graph_nodes"], 2)  # original + resumed copy
        self.assertEqual(summary["counts"]["jev_reports"], 1)
        self.assertEqual(summary["counts"]["selections_linked_to_dispatch"], 1)
        t = summary["tokens"]
        self.assertEqual(t["fresh_total"], 1500)
        self.assertEqual(t["wasted_fresh_failed_partial_blocked"], 500)
        self.assertEqual(t["wasted_unknown_token_dispatches"], 1)
        self.assertEqual(t["rework_fresh_maker_before_repair"], 1000)
        self.assertEqual(t["graph_unaccepted_node_fresh"], 1500)
        self.assertEqual(summary["dispatch_failure_codes"], {"PROVIDER_FAILURE": 1, "TIMEOUT": 1})
        z = summary["zcode_read_admission_refused"]
        self.assertEqual((z["preflight_skips"], z["graph_node_records"], z["graph_unique_tasks"]), (1, 2, 1))
        self.assertEqual((z["stranded_maker_fresh_tokens"], z["stranded_maker_runs"]), (1200, 1))
        self.assertEqual(summary["review_verdicts"], {})  # reviewer selection folder absent

    def test_fresh_tokens_exclude_cache_reads(self):
        self.assertEqual(rl.fresh({"input_tokens": 1000, "cache_read_tokens": 900, "output_tokens": 5}), 105)
        self.assertEqual(rl.fresh({"fresh_input_tokens": 7, "input_tokens": 1000, "output_tokens": 3}), 10)
        self.assertIsNone(rl.fresh({"status": "unknown"}))
        self.assertEqual(rl.completion_code("process-exit-125"), "PROCESS_EXIT_NONZERO")
        self.assertEqual(rl.dispatch_failure({"completion_reason": "partial"}), "PARTIAL_HANDOFF")

    def test_no_free_text_paths_or_task_ids_leak(self):
        ledger, summary_path, _ = self.build()
        blob = ledger.read_text(encoding="utf-8") + summary_path.read_text(encoding="utf-8")
        for needle in ("ACME", "1234567", "someone", "plan.md", "node-a", "task-abc", "works", "pytest"):
            self.assertNotIn(needle, blob)
        records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        selection = next(r for r in records if r["type"] == "selection")
        self.assertEqual(selection["features"]["scope_files"], 2)
        self.assertEqual(selection["features"]["scope_ext"], {".py": 2})
        self.assertEqual([c["agent"] for c in selection["candidates"]], ["zcode", "codex"])
        self.assertEqual(selection["preflight_skipped"][0]["code"], "ZCODE_READ_ADMISSION_REFUSED")
        self.assertEqual(selection["choice"]["agent"], "codex")
        node = next(r for r in records if r["type"] == "node")
        self.assertEqual(node["failure_code"], "POOL_AVAILABILITY")
        self.assertEqual(node["repairs"], 1)

    def test_sources_untouched_and_no_overwrite(self):
        before = self.fx.digest_sources()
        self.build()
        self.assertEqual(before, self.fx.digest_sources())
        with self.assertRaises(FileExistsError):
            self.build()

    def test_review_regressions(self):
        # Relative paths and reason fragments must not survive as identifiers.
        self.assertEqual(rl.safe("clients/acme/plan.md"), "REDACTED")
        self.assertEqual(rl.safe("plan.md"), "REDACTED")
        self.assertEqual(rl.safe_model("vllm/Qwen3.8-27B"), "vllm/Qwen3.8-27B")
        self.assertEqual(rl.safe_model("clients/acme/plan.md"), "REDACTED")
        self.assertEqual(rl.gate_codes("Provider/model preflight: client-acme-data, mode"), ["other-gate", "mode"])
        record = rl.dispatch_record({"run_id": "run-x", "transport_outcome": {"reason": "clients/acme/plan.md"},
                                     "route_event": {"gate_results": [{"code": "acme-secret", "passed": False}]},
                                     "duration_seconds": float("inf")})
        self.assertEqual(record["outcome"]["transport_reason"], "REDACTED")
        self.assertEqual(record["choice"]["failed_gates"], ["other-gate"])
        self.assertIsNone(record["cost"]["duration_s"])
        self.assertIsNone(rl.safe(float("nan")))
        # Half-known usage is unknown, not an undercount.
        self.assertIsNone(rl.fresh({"output_tokens": 12}))
        self.assertIsNone(rl.fresh({"input_tokens": 12}))
        # Malformed shapes do not crash a selection.
        folder = Path(self._tmp.name) / "bad" / "t" / "1" / "maker"
        put(folder / "request.json", {"role": "maker", "task": {"id": "t", "scope": 7, "maker": "x"}})
        put(folder / "assessment.json", {"required_tools": 5, "workload_weights": [1]})
        put(folder / "result.json", {"status": "BLOCKED", "reason": None})
        record = rl.selection_record(folder, {"request.json", "assessment.json", "result.json"})
        self.assertEqual((record["features"]["scope_files"], record["features"]["required_tools"]), (0, []))
        # Aggregates use the full history even beyond 40 steps.
        maker = {"role": "maker", "result": {"status": "DONE", "run_id": "m-last", "tokens": tokens(70)}}
        review = {"role": "reviewer", "result": {"status": "REVIEWED", "verdict": "REPAIR", "run_id": "r"}}
        filler = [{"role": "tests", "result": {}} for _ in range(45)]
        node = rl.node_records(Path(self._tmp.name) / "g" / "state.json",
                               {"plan_hash": "x", "nodes": {"n": {"status": "BLOCKED",
                                                                  "history": filler + [maker, review]}}})
        summary = rl.summarize([], [], node, [])
        self.assertEqual(summary["tokens"]["rework_fresh_maker_before_repair"], 70)

    def test_review_round2_regressions(self):
        self.assertEqual(rl.safe_model("C:/Users"), "REDACTED")
        self.assertEqual(rl.safe("C:"), "REDACTED")
        self.assertEqual(rl.safe_time("2026-10-11T20:00:00.123Z"), "2026-10-11T20:00:00.123Z")
        self.assertIsNone(rl.safe_time("ACME report"))
        self.assertEqual(rl._ext_profile([r"C:\clients\ACME\plan.ACME", "a.py", "Makefile"]),
                         {"(other)": 1, ".py": 1, "(none)": 1})
        self.assertIsNone(rl.num(10 ** 400))
        self.assertEqual(rl.gate_code(["mode"]), "other-gate")
        self.assertEqual(rl._tier_rows(["not", "a", "dict"]), [])
        self.assertEqual(rl._tier_rows({"ordered": {"x": 1}}), [])
        base = Path(self._tmp.name) / "r2"
        put(base / "g1" / "state.json", {"plan_hash": ["list"], "nodes": {"n": {"status": "BLOCKED", "history": []}}})
        for name, ident in (("a-report.json", "client one"), ("b-report.json", "client two")):
            put(base / "j" / name, {"consultation_status": "AVAILABLE", "binding": {"task_id": name},
                                    "decision": {"decision_id": ident, "difficulty": "low"}})
        _, nodes, jevs = rl.scan_tree([base])
        self.assertEqual((len(nodes), len(jevs)), (1, 2))
        with self.assertRaises(ValueError):
            rl.build([self.fx.base / "agents"], self.fx.runs, self.fx.base / "agents", now=self.now)
        with self.assertRaises(ValueError):
            rl.build([self.fx.base / "agents"], self.fx.runs, self.fx.runs / "sub", now=self.now)

    def test_review_round3_regressions(self):
        self.assertEqual(rl.safe_model("clients/acme"), "REDACTED")
        self.assertEqual(rl.safe_model("openrouter/typesafe-jev"), "openrouter/typesafe-jev")
        deep = Path(self._tmp.name) / "deep.json"
        deep.write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
        self.assertIsNone(rl.load_json(deep))
        folder = Path(self._tmp.name) / "r3" / "ACME-client" / "t" / "1" / "ACME"
        put(folder / "request.json", {"role": "boss", "task": {"id": "t"}})
        put(folder / "result.json", {"status": "BLOCKED"})
        self.assertEqual(rl.selection_record(folder, {"request.json", "result.json"})["role"], "unknown")

    def test_dedup_keeps_distinct_decisions_without_run_id(self):
        base = Path(self._tmp.name) / "dedup"
        for name, agent in (("a", "codex"), ("b", "grok"), ("c", "grok")):
            folder = base / name / "t" / "1" / "maker"
            put(folder / "request.json", {"role": "maker", "task": {"id": "t", "maker": []}})
            put(folder / "result.json", {"status": "BLOCKED", "agent": agent})
        selections, _, _ = rl.scan_tree([base])
        self.assertEqual(sorted(s["choice"]["agent"] for s in selections), ["codex", "grok"])

    def test_safe_and_classify(self):
        self.assertEqual(rl.safe("gpt-6.1-sol"), "gpt-6.1-sol")
        self.assertEqual(rl.safe(SECRET_PATH), "REDACTED")
        self.assertEqual(rl.safe("free text with spaces"), "REDACTED")
        self.assertEqual(rl.classify("Provider/model preflight: quota-not-exhausted"), "PREFLIGHT_GATES")
        self.assertEqual(rl.classify("The run finished but no structurally valid handoff was returned: EMPTY"),
                         "HANDOFF_MISSING")
        self.assertEqual(rl.classify("Transport or identity not verified; worktree retained"),
                         "TRANSPORT_OR_IDENTITY_UNVERIFIED")
        self.assertEqual(rl.gate_codes("Provider/model preflight: quota-not-exhausted, mode"),
                         ["quota-not-exhausted", "mode"])
        self.assertIsNone(rl.classify(None))
        self.assertIsNone(rl.num(float("-inf")))
        self.assertIsNone(rl.num(float("nan")))


if __name__ == "__main__":
    unittest.main()
