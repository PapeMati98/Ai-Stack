import json
import os
import subprocess
import sys
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")))

import loop_ledger
from loop_ledger import COMPACT_LIMIT_BYTES, build_parser, compact_payload

LEDGER = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "loop_ledger.py"))
def active_interpreter():
    interpreter = sys.executable
    if not interpreter:
        raise RuntimeError(
            "Active Python interpreter unavailable (sys.executable is empty); "
            "cannot run the loop_ledger CLI offline.")
    return interpreter


PY = active_interpreter()


@contextmanager
def retained_directory():
    root = Path(os.environ.get("TEAM_TEST_ROOT", str(Path(__file__).resolve().parent / ".test-artifacts")))
    path = root / ("ledger-" + uuid.uuid4().hex)
    path.mkdir(parents=True)
    yield str(path)


def run_cli(*argv):
    cmd = [PY, "-B", LEDGER, *argv]
    kwargs = dict(executable=PY)
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kwargs)


class CompactEndToEndTests(unittest.TestCase):
    def test_compact_without_token(self):
        with retained_directory() as root, \
                retained_directory() as workspace:
            goal = os.path.join(root, "goal.json")
            with open(goal, "w", encoding="utf-8") as fh:
                json.dump({"version": 1,
                           "goal_id": "g1",
                           "goal": "Compact-resume smoke test",
                           "workspace": workspace,
                           "requirements": ["r1", "r2"],
                           "constraints": ["c1"],
                           "allowed_scope": ["scope"]}, fh)
            init = run_cli("init", "--root", root, "--goal", goal)
            self.assertEqual(init.returncode, 0, init.stderr)
            proc = run_cli("compact", "--root", root)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "OK")
            for key in ("goal_id", "phase", "round", "unmet", "blockers",
                        "review_sha256", "marker", "required_action"):
                self.assertIn(key, payload)
            self.assertEqual(payload["goal_id"], "g1")
            self.assertLessEqual(len(proc.stdout.encode("utf-8")), COMPACT_LIMIT_BYTES)

    def test_parser_needs_no_token(self):
        ns = build_parser("compact").parse_args(["--root", "r"])
        self.assertFalse(hasattr(ns, "token"))

    def test_compact_never_calls_reconciliation(self):
        with retained_directory() as root, \
                retained_directory() as workspace:
            goal = os.path.join(root, "goal.json")
            with open(goal, "w", encoding="utf-8") as fh:
                json.dump({"version": 1,
                           "goal_id": "g1",
                           "goal": "Reconciliation check",
                           "workspace": workspace,
                           "requirements": ["r1"],
                           "constraints": ["c1"],
                           "allowed_scope": ["scope"]}, fh)
            init = run_cli("init", "--root", root, "--goal", goal)
            self.assertEqual(init.returncode, 0, init.stderr)

            calls = []
            orig_reconciliation = loop_ledger.reconciliation

            def fake_reconciliation(state):
                calls.append(state)
                return orig_reconciliation(state)

            loop_ledger.reconciliation = fake_reconciliation
            try:
                ns = build_parser().parse_args(["compact", "--root", root])
                payload = loop_ledger.execute(ns)
                self.assertEqual(payload["status"], "OK")
                self.assertEqual(len(calls), 0, "compact must never call reconciliation")
            finally:
                loop_ledger.reconciliation = orig_reconciliation


class CompactBoundTests(unittest.TestCase):
    def test_huge_state_stays_bounded(self):
        reqs = ["r%d" % i for i in range(60)]
        issues = [{"id": "ISSUE-%03d" % i, "statement": "S" * 400,
                   "status": "open", "blocking": True} for i in range(300)]
        state = {"goal": {"goal_id": "g", "requirements": reqs},
                 "phase": "run", "round": 3,
                 "review": {"requirements": {}, "issues": issues,
                            "final_acceptance": {"passed": False}},
                 "review_sha256": "abc", "pending": {"marker": "m"}}
        orig = (loop_ledger.blockers, loop_ledger.unmet, loop_ledger.action)
        loop_ledger.blockers = lambda s: [i for i in issues
                                          if i["status"] == "open" and i["blocking"]]
        loop_ledger.unmet = lambda s: list(reqs)
        loop_ledger.action = lambda s, stale=False: "act"
        try:
            payload = compact_payload(state, stale=False)
        finally:
            loop_ledger.blockers, loop_ledger.unmet, loop_ledger.action = orig
        blob = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(blob), COMPACT_LIMIT_BYTES)
        self.assertTrue(payload.get("truncated"))

    def test_long_unmet_string_stays_bounded(self):
        reqs = ["R" * 5000 for _ in range(10)]
        state = {"goal": {"goal_id": "g", "requirements": reqs},
                 "phase": "observing", "round": 0,
                 "review": None, "review_sha256": None, "pending": None}
        orig = (loop_ledger.blockers, loop_ledger.unmet, loop_ledger.action)
        loop_ledger.blockers = lambda s: []
        loop_ledger.unmet = lambda s: list(reqs)
        loop_ledger.action = lambda s, stale=False: "review"
        try:
            payload = compact_payload(state, stale=True)
        finally:
            loop_ledger.blockers, loop_ledger.unmet, loop_ledger.action = orig
        blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(blob), COMPACT_LIMIT_BYTES)
        self.assertLessEqual(len(blob) + 1, COMPACT_LIMIT_BYTES)
        for item in payload["unmet"]:
            self.assertLessEqual(len(item), 200)

    def test_truncated_totals_included(self):
        reqs = ["r%d" % i for i in range(50)]
        issues = [{"id": "ISSUE-%03d" % i, "statement": "S" * 400,
                   "status": "open", "blocking": True} for i in range(100)]
        state = {"goal": {"goal_id": "g", "requirements": reqs},
                 "phase": "run", "round": 3,
                 "review": {"requirements": {}, "issues": issues,
                            "final_acceptance": {"passed": False}},
                 "review_sha256": "abc", "pending": {"marker": "m"}}
        orig = (loop_ledger.blockers, loop_ledger.unmet, loop_ledger.action)
        loop_ledger.blockers = lambda s: [i for i in issues if i["status"] == "open" and i["blocking"]]
        loop_ledger.unmet = lambda s: list(reqs)
        loop_ledger.action = lambda s, stale=False: "act"
        try:
            payload = compact_payload(state, stale=False)
        finally:
            loop_ledger.blockers, loop_ledger.unmet, loop_ledger.action = orig
        self.assertTrue(payload.get("truncated"))
        self.assertEqual(payload.get("unmet_total"), 50)
        self.assertEqual(payload.get("blockers_total"), 100)


class ActiveInterpreterTests(unittest.TestCase):
    def test_missing_interpreter_fails_explicitly(self):
        original = sys.executable
        try:
            sys.executable = ""
            with self.assertRaises(RuntimeError):
                active_interpreter()
        finally:
            sys.executable = original

    def test_active_interpreter_matches_runtime(self):
        self.assertTrue(PY)
        self.assertEqual(active_interpreter(), sys.executable)


if __name__ == "__main__":
    unittest.main()
