from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import curate_once as curator
import maintenance
from caretaker import tick_budget
from caretaker.audit import ensure_tables, group_key, summarize
from caretaker.config import load_config
from caretaker.publication import freeze, sha
from caretaker.pubstate import batch_key, ensure_pub_tables
from caretaker.registry import main_page, read_registry
from caretaker.repaired_retry import (
    INVALID_SUMMARY_FORMAT_SOURCE_REASON,
    INVALID_SUMMARY_FORMAT_RETRY_REASON,
    PHASE_RESUME_CODE_VERSION,
    PHASE_RESUME_CURATE_SHA256,
    PHASE_RESUME_REASON,
    NO_INDEPENDENT_REVIEWER_PHASE_REASON,
    REPAIRED_PUBLICATION_SHA256,
    retry_batch_key,
    retry_token,
    repaired_retry_candidates,
)


TEST_TMP = Path(os.environ.get("TEAM_TEST_ROOT") or tempfile.gettempdir()) / "caretaker-tests"
NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)
PROJECT = "test"
PROPOSAL = "proposals/agent/one.md"
MAIN = "wiki/projects/test.md"
LEGACY_VERSION = "416088b2471fa1a108e270bd91744595"
POLICY = {
    "enabled": True, "include_existing_hub_backlog": True,
    "projects": [PROJECT], "held_projects": [], "min_ready_groups": 1,
    "max_wait_hours": 24, "max_batch": 8,
}
DECISION = {
    "body": "# Synthetic report\nDated local source note (src-one); deployment unverified.\n",
    "summary": "Synthetic bounded report.",
    "dispositions": {
        "prop-one": {"disposition": "synthesized", "reason": "Cited synthetic source."},
    },
}


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Maker:
    def __init__(self, clock=None, advance=0):
        self.calls = 0
        self.clock = clock
        self.advance = advance

    def invoke(self, **_kwargs):
        self.calls += 1
        if self.clock:
            self.clock.advance(self.advance)
        return SimpleNamespace(
            ok=True, status="ok", provider="vllm", model="Qwen3.8-27B",
            session_id="synthetic-maker", final=json.dumps(DECISION),
        )


class Reviewer:
    def __init__(self):
        self.calls = 0

    def invoke(self, *, prompt, **_kwargs):
        self.calls += 1
        match = re.search(r'manifest_sha256":"([a-f0-9]{64})"', prompt)
        if not match:
            raise AssertionError("review input did not bind the manifest")
        return SimpleNamespace(
            ok=True, status="ok", provider="muse-code", model="muse-spark-1.3",
            session_id=f"synthetic-review-{self.calls}",
            final=json.dumps({
                "verdict": "PASS", "manifest_sha256": match.group(1), "findings": [],
            }),
        )


class PhaseResumeRetryTests(unittest.TestCase):
    def setUp(self):
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="phase-retry-", dir=TEST_TMP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.vault.mkdir()
        self.state.mkdir()
        self.project_state = self.state / "projects" / PROJECT / "state"
        self._write_vault()
        self.base = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.base["inputs"] = [{"kind": "session_jsonl", "tool": "codex", "path": str(self.root / "synthetic-session.jsonl")}]
        self.base["fallback"] = {
            "kind": "openclaw_argv", "executable": "synthetic-never-executed", "argv": [],
            "subscription_only": True,
            "allowed_backends": ["muse-code"],
            "allowed_models": ["muse-spark-1.3"],
            "forbidden_backends": ["http-meta", "meta-http"],
            "run_config_inline": {
                "agents": {"defaults": {
                    "skipBootstrap": True, "heartbeat": {"every": "0m"},
                    "model": {"primary": "muse-code/muse-spark-1.3", "fallbacks": []},
                }},
                "telemetry": {"enabled": False}, "update": {"checkOnStart": False},
                "models": {"providers": {}},
                "tools": {
                    "profile": "full", "allow": [], "deny": ["*"],
                    "exec": {"mode": "deny"}, "fs": {"workspaceOnly": True}, "swarm": False,
                    "web": {"search": {"enabled": False}, "fetch": {"enabled": False}},
                },
                "plugins": {
                    "enabled": True, "allow": ["muse-code"],
                    "entries": {"muse-code": {"enabled": True}},
                    "load": {"paths": [(Path.home() / ".agents" / "openclaw-plugins" / "muse-code" / "0.1.0").as_posix()]},
                },
                "mcp": {"servers": {}},
                "skills": {"allowBundled": [], "load": {"watch": False, "extraDirs": []}},
            },
        }
        self.base["project_slug"] = PROJECT
        self.base["state_dir"] = str(self.state)
        self.base["hub"]["vault"] = str(self.vault)
        self.base["hub"]["script"] = str(self.root / "synthetic-knowledge.py")
        self.base["hub"]["context_receipt"] = str(self.root / "context-receipt.json")
        self.control = {
            "publication": {
                **POLICY,
                "reviewer": {"mode": "muse"},
                "maker_timeout_seconds": 500,
                "reviewer_timeout_seconds": 500,
            },
            "known_projects": [PROJECT],
            "max_projects_per_tick": 4,
            "max_batches_per_tick": 4,
        }
        runtime_base = maintenance._runtime_base_with_control_publication(self.base, self.control)
        self.config_path, _runtime = maintenance._project_runtime_cfg(
            runtime_base, PROJECT, self.state,
        )
        self.cfg = load_config(self.config_path)
        self.paths = [PROPOSAL]
        self.maker = Maker(Clock(), advance=350)
        self.reviewer = Reviewer()
        self._prepare_actual_phase_cache()
        self.summary = summarize(self.vault, POLICY, now=NOW.timestamp())
        self.meta = self.summary["eligible"][0]
        self.db = self._db()
        self.addCleanup(self.db.close)
        self.old_key = self._seed_old_failure(self.db)

    def _write_vault(self):
        source = self.vault / "raw/sources/2026/09/src-one"
        source.mkdir(parents=True)
        original = b"Synthetic local report; publication outcome remains unverified."
        (source / "original.md").write_bytes(original)
        (source / "source.json").write_text(json.dumps({
            "source_id": "src-one", "classification": "internal",
            "local_only": False, "original_file": "original.md",
            "sha256": sha(original), "byte_length": len(original),
        }), encoding="utf-8")
        proposal = self.vault / PROPOSAL
        proposal.parent.mkdir(parents=True)
        proposal.write_text(
            "---\nid: prop-one\nclassification: internal\nproject_slug: test\n"
            "source_ids:\n  - src-one\ntarget_pages:\n  - wiki/projects/test.md\n---\n"
            "Synthetic proposal with a bounded source claim.\n",
            encoding="utf-8",
        )
        unrelated = self.vault / "proposals/agent/unrelated.md"
        unrelated.write_text(
            "---\nid: prop-unrelated\nclassification: internal\nproject_slug: test\n"
            "source_ids:\n  - src-missing\ntarget_pages:\n  - wiki/projects/test.md\n---\n"
            "Synthetic proposal with a deliberately missing source.\n",
            encoding="utf-8",
        )
        page = self.vault / MAIN
        page.parent.mkdir(parents=True)
        page.write_text(
            "---\ntitle: Test\nstatus: reviewed\nproject_slug: test\n"
            "sources: [src-one]\n---\n\n# Test\n",
            encoding="utf-8",
        )

    @staticmethod
    def _gateway():
        return SimpleNamespace(
            health=lambda: SimpleNamespace(status="pass"),
            lint=lambda: SimpleNamespace(status="pass"),
        )

    def _publish_impl(self, _cfg, _manifest, _review, _run_root, result):
        result.update(
            status="PUBLISHED", publication={"status": "PUBLISHED"},
            checkpoint={"status": "CHECKPOINTED"},
        )

    def _curator_patches(self):
        return (
            patch.object(curator, "CanonicalKnowledgeGateway", return_value=self._gateway()),
            patch.object(curator, "_write_run_config", return_value=str(self.root / "run-config.json")),
            patch.object(curator, "_publish_under_lock", side_effect=self._publish_impl),
        )

    def _run_curator(self, cfg, paths, *, budget=None):
        patches = self._curator_patches()
        for item in patches:
            item.start()
        try:
            if budget is None:
                return curator._run_with_config(cfg, paths, True, self.maker, self.reviewer)
            with budget.activate():
                return curator._run_with_config(cfg, paths, True, self.maker, self.reviewer)
        finally:
            for item in reversed(patches):
                item.stop()

    def _prepare_actual_phase_cache(self):
        from caretaker.tick_budget import TickBudget

        result = self._run_curator(
            self.cfg, self.paths,
            budget=TickBudget(seconds=500, clock=self.maker.clock),
        )
        self.assertEqual(result["status"], "DEFERRED_PREPARED", msg=json.dumps(result, default=str))
        self.assertEqual(result["phase"], "PREPARED")
        self.assertEqual(self.maker.calls, 1)
        self.assertEqual(self.reviewer.calls, 0)
        self.phase_pointer = curator._phase_pointer(
            self.project_state / "curation", PROJECT, self.paths,
        )
        record = json.loads(self.phase_pointer.read_text(encoding="utf-8"))
        self.assertEqual(record["phase"], "PREPARED")
        self.assertEqual(record["progress"]["maker_calls"], 1)
        self.assertEqual(record["progress"]["review_calls"], 0)
        # The deployed artifact was written by the old 416 implementation.
        record["code_version"] = LEGACY_VERSION
        record["cache_sha256"] = curator._phase_digest(record)
        curator._durable_json(self.phase_pointer, record)
        frozen = freeze(
            self.vault, self.paths, MAIN,
            attestation_state_dir=self.project_state,
        )
        loaded = curator._load_phase_progress(
            self.cfg, self.paths, self.project_state / "curation", frozen,
        )
        self.assertEqual(loaded["phase"], "PREPARED")
        self.assertEqual(loaded["code_version"], LEGACY_VERSION)
        self.assertEqual(loaded["maker_calls"], 1)
        self.assertEqual(loaded["review_calls"], 0)

    def _prepare_repair_phase_cache(self):
        """Turn the real pinned synthetic candidate into an accepted repair phase."""
        record = json.loads(self.phase_pointer.read_text(encoding="utf-8"))
        state = self.project_state / "curation"
        old_candidate = state / record["candidate"]
        repair_candidate = old_candidate.with_name("candidate-review-repair")
        shutil.copytree(old_candidate, repair_candidate)
        record["candidate"] = repair_candidate.relative_to(state).as_posix()
        record["manifest"] = (repair_candidate / "publication.json").relative_to(state).as_posix()
        record["pins"] = curator._phase_pins(state, repair_candidate)
        assessment = {
            "verdict": "REPAIR", "manifest_sha256": "b" * 64,
            "findings": ["synthetic review requested a source-bound repair"],
        }
        progress = dict(record["progress"])
        progress.update(
            phase="REPAIR_PREPARED", maker_calls=3, review_calls=1,
            maker_format_repairs=1, maker_provider="kimi",
            maker_model="k3", maker_route="fleet_append", assessment=assessment,
            review_rounds=[{"round": 1, "verdict": "REPAIR",
                            "findings": ["synthetic review requested a source-bound repair"]}],
            repair_attempt=1,
        )
        progress.pop("review_response", None)
        record["phase"] = "REPAIR_PREPARED"
        record["code_version"] = PHASE_RESUME_CODE_VERSION
        record["progress"] = progress
        record["cache_sha256"] = curator._phase_digest(record)
        curator._durable_json(self.phase_pointer, record)
        registry, _ = read_registry(self.vault)
        frozen = freeze(
            self.vault, self.paths, main_page(PROJECT, registry),
            attestation_state_dir=self.project_state,
        )
        loaded = curator._load_phase_progress(
            self.cfg, self.paths, state, frozen,
        )
        self.assertEqual(loaded["phase"], "REPAIR_PREPARED")
        self.assertEqual(loaded["candidate_path"], str(repair_candidate))
        return loaded

    def _change_old_failure_reason(self, reason):
        raw, = self.db.execute(
            "SELECT result FROM publications WHERE batch=?", (self.old_key,),
        ).fetchone()
        result = json.loads(raw)
        result.update(
            status="BLOCKED", reason=reason, original_reason=reason,
            maker_calls=3, review_calls=1,
            maker_agent="kimi", primary_model="k3",
            maker_lineage="kimi", candidate_path=str(
                self.project_state / "curation" / json.loads(
                    self.phase_pointer.read_text(encoding="utf-8")
                )["candidate"]
            ),
            run_path=str(self.project_state / "curation" / json.loads(
                self.phase_pointer.read_text(encoding="utf-8")
            )["run_root"]),
        )
        with self.db:
            self.db.execute(
                "UPDATE publications SET status='BLOCKED',category='deterministic',"
                "failures=1,attempt=1,result=? WHERE batch=?",
                (json.dumps(result), self.old_key),
            )

    def test_no_independent_reviewer_phase_retry_requires_exact_repair_cache(self):
        self._prepare_repair_phase_cache()
        self._change_old_failure_reason(NO_INDEPENDENT_REVIEWER_PHASE_REASON)
        candidates = self._candidates(self.db)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["reason"], PHASE_RESUME_REASON)
        self.assertEqual(candidates[0]["curate_sha256"], PHASE_RESUME_CURATE_SHA256)
        original = self.phase_pointer.read_bytes()
        try:
            record = json.loads(original)
            record["progress"]["review_rounds"][-1]["verdict"] = "PASS"
            record["cache_sha256"] = curator._phase_digest(record)
            curator._durable_json(self.phase_pointer, record)
            self.assertEqual(self._candidates(self.db), [])
        finally:
            self.phase_pointer.write_bytes(original)

    def test_format_retry_batch_phase_resume_requires_exact_spent_token_proof(self):
        from caretaker.pubstate import code_version
        from caretaker.repaired_retry import (
            _pre_candidate_format_helper_sha256, ensure_retry_table,
        )

        self._prepare_repair_phase_cache()
        original_batch = self.old_key
        old_cycle = "audit-new-old-phase"
        source_raw, = self.db.execute(
            "SELECT result FROM publications WHERE batch=?", (original_batch,),
        ).fetchone()
        source_result = json.loads(source_raw)
        source_result.update(
            status="BLOCKED", reason=INVALID_SUMMARY_FORMAT_SOURCE_REASON,
            original_reason=INVALID_SUMMARY_FORMAT_SOURCE_REASON,
            candidate_path=None, model_calls=1, format_repair_exhausted=False,
            manifest=None, manifest_sha256=None, review=None, cycle_id=old_cycle,
        )
        with self.db:
            self.db.execute(
                "UPDATE publications SET status='BLOCKED',category='deterministic',"
                "failures=1,attempt=1,result=? WHERE batch=?",
                (json.dumps(source_result), original_batch),
            )
        helper_digest = _pre_candidate_format_helper_sha256((
            ROOT / "maintenance.py", ROOT / "caretaker" / "repaired_retry.py",
            ROOT / "curate_once.py", ROOT / "caretaker" / "draft_summary.py",
        ))
        self.assertIsNotNone(helper_digest)
        retry_member = {
            "proposal_id": self.meta["proposal_id"],
            "path": self.meta["path"], "sha256": self.meta["sha256"],
            "project": PROJECT, "reason": INVALID_SUMMARY_FORMAT_RETRY_REASON,
            "publication_sha256": REPAIRED_PUBLICATION_SHA256,
            "code_version": code_version(), "previous_batch": original_batch,
            "helper_sha256": helper_digest,
        }
        retry_member["token"] = retry_token(
            REPAIRED_PUBLICATION_SHA256, PROJECT, self.meta["proposal_id"],
            self.meta["path"], self.meta["sha256"],
            reason=INVALID_SUMMARY_FORMAT_RETRY_REASON,
            helper_sha256=helper_digest,
        )
        members = [{
            "proposal_id": self.meta["proposal_id"],
            "path": self.meta["path"], "sha256": self.meta["sha256"],
        }]
        repaired_batch = retry_batch_key(PROJECT, members, [retry_member])
        format_cycle = "audit-new-format-retry"
        phase_record = json.loads(self.phase_pointer.read_text(encoding="utf-8"))
        result = dict(source_result)
        result.update(
            status="BLOCKED", reason=NO_INDEPENDENT_REVIEWER_PHASE_REASON,
            original_reason=NO_INDEPENDENT_REVIEWER_PHASE_REASON,
            model_calls=0, batch=repaired_batch, cycle_id=format_cycle,
            candidate_path=str(self.project_state / "curation" / phase_record["candidate"]),
            run_path=str(self.project_state / "curation" / phase_record["run_root"]),
            maker_calls=3, review_calls=1, maker_agent="kimi",
            primary_model="k3", maker_lineage="kimi",
        )
        with self.db:
            self.db.execute(
                "UPDATE audit_members SET cycle_id=? WHERE cycle_id='cycle-old-phase'",
                (old_cycle,),
            )
            self.db.execute(
                "UPDATE audit_cycles SET id=?,lane='new' WHERE id='cycle-old-phase'",
                (old_cycle,),
            )
            self.db.execute(
                "INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)",
                (format_cycle, "scoped", NOW.isoformat(), "ACTIVE",
                 json.dumps({"repaired_retry": [retry_member]}), "new"),
            )
            self.db.execute(
                "INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (format_cycle, self.meta["proposal_id"], self.meta["path"],
                 self.meta["sha256"], PROJECT, group_key(self.meta), "failed",
                 NO_INDEPENDENT_REVIEWER_PHASE_REASON),
            )
            self.db.execute(
                "INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (repaired_batch, "BLOCKED", 1, json.dumps(result), 1, "",
                 "deterministic", "a" * 64, NOW.isoformat()),
            )
        ensure_retry_table(self.db)
        self.db.execute(
            "INSERT INTO repaired_retry_tokens(token,cycle_id,publication_sha256,code_version,"
            "proposal_id,path,sha256,previous_batch) VALUES(?,?,?,?,?,?,?,?)",
            (retry_member["token"], format_cycle, retry_member["publication_sha256"],
             retry_member["code_version"], retry_member["proposal_id"],
             retry_member["path"], retry_member["sha256"], retry_member["previous_batch"]),
        )
        self.db.commit()
        self.old_key = repaired_batch

        candidates = self._candidates(self.db, lane="new")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["reason"], PHASE_RESUME_REASON)
        self.assertEqual(candidates[0]["previous_batch"], repaired_batch)

        with self.db:
            self.db.execute("DELETE FROM repaired_retry_tokens WHERE token=?",
                            (retry_member["token"],))
        self.assertEqual(self._candidates(self.db, lane="new"), [])

    def test_format_predecessor_qualifies_two_failed_targets_in_shared_29_member_cycle(self):
        from caretaker.repaired_retry import _failed_exact_source_members

        targets = [
            ("prop-target-a", "proposals/agent/target-a.md", "a" * 64, "failed"),
            ("prop-target-b", "proposals/agent/target-b.md", "b" * 64, "failed"),
        ]
        unrelated = [
            (f"prop-unrelated-{index:02d}",
             f"proposals/agent/unrelated-{index:02d}.md",
             f"{index + 1:064x}", "pending")
            for index in range(27)
        ]
        members = targets + unrelated
        expected = {(pid, path, digest) for pid, path, digest, _status in targets}
        self.assertEqual(len(members), 29)
        self.assertTrue(_failed_exact_source_members(members, expected))

        wrong_hash = list(members)
        wrong_hash[0] = (targets[0][0], targets[0][1], "f" * 64, "failed")
        self.assertFalse(_failed_exact_source_members(wrong_hash, expected))

        wrong_status = list(members)
        wrong_status[1] = (*targets[1][:3], "pending")
        self.assertFalse(_failed_exact_source_members(wrong_status, expected))

        duplicate_target = list(members) + [targets[0]]
        self.assertFalse(_failed_exact_source_members(duplicate_target, expected))

    def test_repair_phase_no_reviewer_defers_then_resumes_without_maker(self):
        self._prepare_repair_phase_cache()
        self._change_old_failure_reason(NO_INDEPENDENT_REVIEWER_PHASE_REASON)
        old_row_before = self.db.execute(
            "SELECT status,result,attempt,fingerprint FROM publications WHERE batch=?",
            (self.old_key,),
        ).fetchone()

        class SameLineageReviewer:
            def __init__(self):
                self.calls = 0

            def invoke(self, **_kwargs):
                self.calls += 1
                return SimpleNamespace(
                    ok=True, status="ok", provider="kimi", model="k3",
                    session_id="synthetic-same-lineage-review",
                    final=json.dumps({"verdict": "PASS", "findings": []}),
                )

        same_lineage = SameLineageReviewer()
        self.reviewer = same_lineage

        def invoke(config_path, paths):
            return self._run_curator(load_config(config_path), paths)

        patches = self._curator_patches()
        for item in patches:
            item.start()
        try:
            with patch.object(maintenance, "_invoke_curate", side_effect=invoke):
                first = maintenance.publication_drain(
                    self.control, self.base, self.db, self.state,
                    now=NOW, lane="legacy", recovery={"status": "NO_LOCK"},
                )
                self.assertEqual(first["batches"][0]["status"], "DEFERRED_PREPARED")
                self.assertEqual(self.db.execute(
                    "SELECT status,attempt FROM publications WHERE batch<>? ",
                    (self.old_key,),
                ).fetchone(), ("PREPARED_DEFERRED", 0))
                self.assertEqual(self.db.execute(
                    "SELECT status FROM audit_members WHERE cycle_id=?",
                    (first["cycle_id"],),
                ).fetchone()[0], "pending")
                self.assertEqual(self.db.execute(
                    "SELECT status,result,attempt,fingerprint FROM publications WHERE batch=?",
                    (self.old_key,),
                ).fetchone(), old_row_before)
                self.assertEqual(self.db.execute(
                    "SELECT COUNT(*) FROM repaired_retry_tokens",
                ).fetchone()[0], 1)

                self.reviewer = Reviewer()
                second = maintenance.publication_drain(
                    self.control, self.base, self.db, self.state,
                    now=NOW + dt.timedelta(seconds=1), lane="legacy",
                    recovery={"status": "NO_LOCK"},
                )
        finally:
            for item in reversed(patches):
                item.stop()

        self.assertEqual(second["batches"][0]["status"], "PUBLISHED")
        self.assertEqual(self.maker.calls, 1)
        self.assertEqual(same_lineage.calls, 1)
        self.assertEqual(self.reviewer.calls, 1)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM repaired_retry_tokens",
        ).fetchone()[0], 1)

    @staticmethod
    def _db():
        conn = sqlite3.connect(":memory:")
        ensure_tables(conn)
        ensure_pub_tables(conn)
        return conn

    def _seed_old_failure(self, conn, *, status="BLOCKED", reason=PHASE_RESUME_REASON,
                          category="deterministic", failures=1, attempt=1,
                          lane="legacy", member_status="failed"):
        old_cycle = "cycle-old-phase"
        key = batch_key(PROJECT, self.paths)
        conn.execute(
            "INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)",
            (old_cycle, "threshold", "2026-10-01T00:00:00+00:00", "DRAINED", "{}", lane),
        )
        conn.execute(
            "INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (old_cycle, self.meta["proposal_id"], self.meta["path"], self.meta["sha256"],
             PROJECT, group_key(self.meta), member_status, status),
        )
        result = {
            "status": status, "reason": reason, "original_reason": reason,
            "batch": key, "cycle_id": old_cycle,
            "proposal_ids": [self.meta["proposal_id"]],
            "paths": [self.meta["path"]], "hashes": [self.meta["sha256"]],
            "project": PROJECT, "state_dir": str(self.project_state), "model_calls": 0,
        }
        conn.execute(
            "INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (key, status, failures, json.dumps(result), attempt, "", category,
             "a" * 64, "2026-10-01T00:00:00+00:00"),
        )
        conn.commit()
        return key

    def _phase_cache_check(self, project, metas):
        if not metas or any(meta.get("project") != project for meta in metas):
            return False
        paths = sorted(meta["path"] for meta in metas)
        registry, _ = read_registry(self.vault)
        frozen = freeze(
            self.vault, paths, main_page(project, registry),
            attestation_state_dir=self.project_state,
        )
        loaded = curator._load_phase_progress(
            self.cfg, paths, self.project_state / "curation", frozen,
        )
        return maintenance._prepared_phase_is_reviewable(
            loaded,
            allow_repair=(metas[0].get("_phase_resume_reason")
                          == NO_INDEPENDENT_REVIEWER_PHASE_REASON),
        )

    def _source_preflight(self, meta):
        registry, _ = read_registry(self.vault)
        freeze(
            self.vault, [meta["path"]], main_page(meta["project"], registry),
            attestation_state_dir=self.project_state,
        )
        return True

    def _pre_candidate_format_preflight(self, old):
        paths = old.get("paths")
        if not isinstance(paths, list) or paths != sorted(paths):
            return False
        registry, _ = read_registry(self.vault)
        frozen = freeze(
            self.vault, paths, main_page(old["project"], registry),
            attestation_state_dir=self.project_state,
        )
        actual = {(item.get("id"), item.get("path"), item.get("sha256"))
                  for item in frozen.get("proposals", []) if isinstance(item, dict)}
        expected = set(zip(old.get("proposal_ids", []), paths, old.get("hashes", [])))
        return bool(expected) and actual == expected and len(actual) == len(paths)

    def _candidates(self, conn, summary=None, *, lane="legacy", allowed=lambda _p: True,
                    source_preflight=None, phase_preflight=None,
                    publication_path=None, curate_path=None):
        format_paths = (
            ROOT / "maintenance.py", ROOT / "caretaker" / "repaired_retry.py",
            ROOT / "curate_once.py", ROOT / "caretaker" / "draft_summary.py",
        )
        return repaired_retry_candidates(
            conn, summary or self.summary, lane, allowed, self.vault,
            source_preflight=source_preflight or self._source_preflight,
            publication_path=publication_path or ROOT / "caretaker" / "publication.py",
            curate_path=curate_path or ROOT / "curate_once.py",
            phase_cache_preflight=phase_preflight or self._phase_cache_check,
            pre_candidate_summary_format_preflight=self._pre_candidate_format_preflight,
            pre_candidate_summary_format_paths=format_paths,
        )

    def test_normal_drain_retries_before_unrelated_active_cycle_and_restores_it(self):
        # An unrelated pending frozen member ordinarily keeps maybe_start_cycle
        # on that ACTIVE cycle. The exact repaired set gets one ordinary scoped
        # successor and the untouched predecessor is restored on the next drain.
        backlog = "cycle-unrelated-backlog"
        unrelated_path = "proposals/agent/unrelated.md"
        unrelated_hash = sha((self.vault / unrelated_path).read_bytes())
        self.db.execute(
            "INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)",
            (backlog, "threshold", NOW.isoformat(), "ACTIVE", "{}", "legacy"),
        )
        self.db.execute(
            "INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (backlog, "prop-unrelated", unrelated_path, unrelated_hash,
             PROJECT, "unrelated-group", "pending", ""),
        )
        self.db.commit()
        old_row_before = self.db.execute(
            "SELECT status,result,attempt,fingerprint FROM publications WHERE batch=?",
            (self.old_key,),
        ).fetchone()

        def invoke(config_path, paths):
            cfg = load_config(config_path)
            return self._run_curator(cfg, paths)

        patches = self._curator_patches()
        for item in patches:
            item.start()
        try:
            with patch.object(maintenance, "_invoke_curate", side_effect=invoke):
                first = maintenance.publication_drain(
                    self.control, self.base, self.db, self.state,
                    now=NOW, lane="legacy", recovery={"status": "NO_LOCK"},
                )
                self.assertEqual(first["batches"][0]["project"], PROJECT)
                self.assertEqual(first["batches"][0]["status"], "PUBLISHED")
                self.assertEqual(self.maker.calls, 1)
                self.assertEqual(self.reviewer.calls, 1)
                retry_cycles = self.db.execute(
                    "SELECT id,status,counts FROM audit_cycles WHERE id<>? AND id<>?",
                    ("cycle-old-phase", backlog),
                ).fetchall()
                self.assertEqual(len(retry_cycles), 1)
                retry_cycle = retry_cycles[0]
                self.assertEqual(retry_cycle[1], "DRAINED")
                retry_counts = json.loads(retry_cycle[2])
                self.assertEqual(retry_counts["repaired_retry"][0]["reason"], PHASE_RESUME_REASON)
                self.assertEqual(self.db.execute(
                    "SELECT status FROM audit_cycles WHERE id=?", (backlog,),
                ).fetchone()[0], "SCOPED_DRAINED")
                self.assertEqual(self.db.execute(
                    "SELECT status FROM audit_members WHERE cycle_id=?", (backlog,),
                ).fetchone()[0], "pending")
                self.assertEqual(self.db.execute(
                    "SELECT status,result,attempt,fingerprint FROM publications WHERE batch=?",
                    (self.old_key,),
                ).fetchone(), old_row_before)
                self.assertEqual(self.db.execute(
                    "SELECT COUNT(*) FROM repaired_retry_tokens",
                ).fetchone()[0], 1)

                second = maintenance.publication_drain(
                    self.control, self.base, self.db, self.state,
                    now=NOW + dt.timedelta(seconds=1), lane="legacy",
                    recovery={"status": "NO_LOCK"},
                )
        finally:
            for item in reversed(patches):
                item.stop()

        self.assertEqual(self.maker.calls, 1)
        self.assertEqual(self.reviewer.calls, 1)
        self.assertEqual(second["model_calls"], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM repaired_retry_tokens",
        ).fetchone()[0], 1)
        self.assertEqual(second["cycle_id"], backlog)
        self.assertEqual(self.db.execute(
            "SELECT status FROM audit_members WHERE cycle_id=?", (backlog,),
        ).fetchone()[0], "quarantined")

    def test_phase_retry_requires_exact_failure_current_code_and_live_cache(self):
        def reject(*, status="BLOCKED", reason=PHASE_RESUME_REASON,
                   category="deterministic", failures=1, attempt=1,
                   lane="legacy", member_status="failed", allowed=lambda _p: True,
                   summary=None, source_preflight=None, phase_preflight=None,
                   publication_path=None, curate_path=None):
            conn = self._db()
            self.addCleanup(conn.close)
            self._seed_old_failure(
                conn, status=status, reason=reason, category=category,
                failures=failures, attempt=attempt, lane=lane,
                member_status=member_status,
            )
            return self._candidates(
                conn, summary, allowed=allowed, source_preflight=source_preflight,
                phase_preflight=phase_preflight, publication_path=publication_path,
                curate_path=curate_path,
            )

        cases = [
            ("wrong reason", {"reason": "other_failure"}),
            ("unknown", {"status": "UNKNOWN_REQUIRES_RECONCILIATION"}),
            ("running", {"status": "RUNNING"}),
            ("published", {"status": "PUBLISHED"}),
            ("wrong category", {"category": "transient"}),
            ("second attempt", {"failures": 2, "attempt": 2}),
            ("member pending", {"member_status": "pending"}),
            ("wrong lane", {"lane": "new"}),
            ("held project", {"allowed": lambda _p: False}),
            ("unsafe source", {"source_preflight": lambda _meta: False}),
        ]
        for label, kwargs in cases:
            with self.subTest(label=label):
                self.assertEqual(reject(**kwargs), [])

        duplicate = dict(self.summary)
        duplicate["eligible"] = list(self.summary["eligible"]) * 2
        self.assertEqual(reject(summary=duplicate), [])

        wrong_curate = self.root / "wrong-curate.py"
        wrong_curate.write_text("synthetic changed implementation", encoding="utf-8")
        self.assertEqual(reject(curate_path=wrong_curate), [])
        wrong_publication = self.root / "wrong-publication.py"
        wrong_publication.write_text("synthetic changed publisher", encoding="utf-8")
        self.assertEqual(reject(publication_path=wrong_publication), [])

        source_file = self.vault / "raw/sources/2026/09/src-one/original.md"
        original_source = source_file.read_bytes()
        try:
            source_file.write_bytes(original_source + b"changed")
            self.assertEqual(reject(), [])
        finally:
            source_file.write_bytes(original_source)

        target_file = self.vault / MAIN
        original_target = target_file.read_bytes()
        try:
            target_file.write_bytes(original_target + b"changed target preimage\n")
            self.assertEqual(reject(), [])
        finally:
            target_file.write_bytes(original_target)

        # A second publication result for the same identity makes the old
        # outcome ambiguous, even if the added row itself is unrelated.
        conn = self._db()
        self.addCleanup(conn.close)
        self._seed_old_failure(conn)
        duplicate_result = {
            "status": "BLOCKED", "reason": "other", "original_reason": "other",
            "batch": "other-batch", "cycle_id": "cycle-old-phase",
            "proposal_ids": [self.meta["proposal_id"]], "paths": [self.meta["path"]],
            "hashes": [self.meta["sha256"]], "project": PROJECT,
        }
        conn.execute(
            "INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("other-batch", "BLOCKED", 1, json.dumps(duplicate_result), 1, "",
             "deterministic", "c" * 64, NOW.isoformat()),
        )
        conn.commit()
        self.assertEqual(self._candidates(conn), [])

    def test_quarantined_failure_accepts_only_live_fresh_phase_and_fresh_canonical(self):
        conn = self._db()
        self.addCleanup(conn.close)
        self._seed_old_failure(conn, status="QUARANTINED")
        candidates = self._candidates(conn)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["reason"], PHASE_RESUME_REASON)
        self.assertEqual(candidates[0]["curate_sha256"], PHASE_RESUME_CURATE_SHA256)
        self.assertEqual(candidates[0]["code_version"], PHASE_RESUME_CODE_VERSION)
        self.assertEqual(candidates[0]["publication_sha256"], REPAIRED_PUBLICATION_SHA256)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='repaired_retry_tokens'",
        ).fetchone()[0], 0)

        with patch("caretaker.pubstate.canonical_verdict", return_value=("unknown", {})):
            self.assertEqual(self._candidates(conn), [])

        held_pointer = self.phase_pointer.with_name(self.phase_pointer.name + ".held")
        self.phase_pointer.replace(held_pointer)
        try:
            self.assertEqual(self._candidates(conn), [])
        finally:
            held_pointer.replace(self.phase_pointer)

        canonical = self.vault / "governance/curations/synthetic.json"
        canonical.parent.mkdir(parents=True)
        canonical.write_text(json.dumps({"proposals": [{
            "proposal_id": self.meta["proposal_id"], "path": self.meta["path"],
            "sha256": self.meta["sha256"], "disposition": "synthesized",
        }]}), encoding="utf-8")
        self.assertEqual(self._candidates(conn), [])
        canonical.write_text(json.dumps({"proposals": []}), encoding="utf-8")

        original_phase = self.phase_pointer.read_bytes()
        try:
            self.phase_pointer.write_text("{}", encoding="utf-8")
            self.assertEqual(self._candidates(conn), [])
        finally:
            self.phase_pointer.write_bytes(original_phase)

        record = json.loads(original_phase)
        pin_name = next(iter(record["pins"]))
        pin_path = self.phase_pointer.parent / pin_name
        original_pin = pin_path.read_bytes()
        try:
            pin_path.write_bytes(original_pin + b"tampered")
            self.assertEqual(self._candidates(conn), [])
        finally:
            pin_path.write_bytes(original_pin)


if __name__ == "__main__":
    unittest.main()
