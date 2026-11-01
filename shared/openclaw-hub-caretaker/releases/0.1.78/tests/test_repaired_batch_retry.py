"""Bounded retry for the one independently accepted pagination repair."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import maintenance
from caretaker.audit import ensure_tables, group_key, maybe_start_cycle, policy_from, summarize
from caretaker.publication import sha
from caretaker.pubstate import batch_key, code_version, ensure_pub_tables, fingerprint
from caretaker.repaired_retry import (
    INVALID_SUMMARY_FORMAT_RETRY_REASON,
    REPAIRED_REASON,
    REPAIRED_PUBLICATION_SHA256,
    SUMMARY_REPAIR_REASON,
    SUMMARY_REPAIR_SOURCE_REASON,
    TERMINAL_GENERATION_RETRY_REASON,
    TERMINAL_GENERATION_SOURCE_REASON,
    ensure_retry_table,
    repaired_retry_candidates,
    retry_batch_key,
    retry_token,
)


TASK_TMP = ROOT / ".test-tmp"
POLICY = {"min_ready_groups": 1, "max_wait_hours": 24, "max_batch": 8}
NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)


def _workspace(test: unittest.TestCase):
    TASK_TMP.mkdir(exist_ok=True)
    temp = tempfile.TemporaryDirectory(dir=str(TASK_TMP))
    test.addCleanup(temp.cleanup)
    root = Path(temp.name)
    vault = root / "vault"
    state = root / "state"
    state.mkdir()

    source = vault / "raw/sources/2026/09/src-one"
    source.mkdir(parents=True)
    original = b"Dated report: local validation passed; deployment remains unverified."
    (source / "original.md").write_bytes(original)
    (source / "source.json").write_text(json.dumps({
        "source_id": "src-one",
        "classification": "internal",
        "local_only": False,
        "original_file": "original.md",
        "sha256": sha(original),
        "byte_length": len(original),
    }), encoding="utf-8")

    proposal = vault / "proposals/agent/one.md"
    proposal.parent.mkdir(parents=True)
    proposal.write_text(
        "---\nid: prop-one\nclassification: internal\nproject_slug: test\n"
        "source_ids:\n  - src-one\ntarget_pages:\n  - wiki/projects/test.md\n---\n"
        "A scoped synthetic report.\n",
        encoding="utf-8",
    )
    page = vault / "wiki/projects/test.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntitle: Test\nstatus: reviewed\nproject_slug: test\n---\n\n# Test\n",
        encoding="utf-8",
    )
    return root, vault, state


def _summary(vault: Path):
    return summarize(vault, POLICY, now=NOW.timestamp())


def _db():
    conn = sqlite3.connect(":memory:")
    ensure_tables(conn)
    ensure_pub_tables(conn)
    return conn


def _seed_failure(conn: sqlite3.Connection, summary: dict, state: Path,
                  *, status="QUARANTINED", reason=REPAIRED_REASON,
                  fingerprint="a" * 64, mutate=None):
    meta = summary["eligible"][0]
    cycle_id = "cycle-old"
    project = meta["project"]
    paths = [meta["path"]]
    pids = [meta["proposal_id"]]
    hashes = [meta["sha256"]]
    key = batch_key(project, paths)
    conn.execute(
        "INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)",
        (cycle_id, "threshold", "2026-10-01T00:00:00+00:00", "DRAINED", "{}", "legacy"),
    )
    conn.execute(
        "INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (cycle_id, meta["proposal_id"], meta["path"], meta["sha256"], project,
         group_key(meta), "quarantined", "QUARANTINED"),
    )
    result = {
        "status": "QUARANTINED",
        "reason": reason,
        "original_reason": reason,
        "batch": key,
        "cycle_id": cycle_id,
        "proposal_ids": pids,
        "paths": paths,
        "hashes": hashes,
        "project": project,
        "state_dir": str(state),
    }
    if mutate:
        mutate(result)
    conn.execute(
        "INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (key, status, 2, json.dumps(result), 2, "", "deterministic", fingerprint,
         "2026-10-01T00:00:00+00:00"),
    )
    conn.commit()
    return key, result


def _seed_blocked_invalid_summary(conn: sqlite3.Connection, summary: dict,
                                  state: Path, vault: Path, *, mutate=None):
    meta = summary["eligible"][0]
    project = meta["project"]
    paths = [meta["path"]]
    pids = [meta["proposal_id"]]
    hashes = [meta["sha256"]]
    cycle_id = "audit-new-invalid-summary-old"
    key = batch_key(project, paths)
    policy = policy_from(POLICY)
    page = vault / "wiki/projects/test.md"
    current_fp = fingerprint(
        project, paths, hashes + [sha(page.read_bytes())], policy, code_version(),
    )
    conn.execute(
        "INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)",
        (cycle_id, "threshold", "2026-10-22T00:00:00+00:00", "DRAINED", "{}", "new"),
    )
    conn.execute(
        "INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (cycle_id, pids[0], paths[0], hashes[0], project, group_key(meta),
         "failed", "BLOCKED"),
    )
    result = {
        "status": "BLOCKED",
        "reason": "invalid summary",
        "original_reason": "invalid summary",
        "batch": key,
        "cycle_id": cycle_id,
        "proposal_ids": pids,
        "paths": paths,
        "hashes": hashes,
        "project": project,
        "state_dir": str(state),
        "candidate_path": None,
        "model_calls": 1,
        "format_repair_exhausted": False,
    }
    if mutate:
        mutate(result)
    conn.execute(
        "INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (key, "BLOCKED", 1, json.dumps(result), 1, "", "deterministic",
         current_fp, "2026-10-22T00:00:00+00:00"),
    )
    conn.commit()
    return key, result, current_fp


def _preflight(vault: Path, state: Path):
    from caretaker.publication import freeze

    def check(meta):
        freeze(vault, [meta["path"]], "wiki/projects/test.md", attestation_state_dir=state)
        return True
    return check


def _candidate(conn, summary, vault, state, **kwargs):
    return repaired_retry_candidates(
        conn, summary, "legacy", lambda project: project == "test", vault,
        source_preflight=kwargs.pop("source_preflight", _preflight(vault, state)),
        **kwargs,
    )


def _format_candidate(conn, summary, vault, state, **kwargs):
    from caretaker import repaired_retry

    return repaired_retry_candidates(
        conn, summary, "new", lambda project: project == "test", vault,
        source_preflight=kwargs.pop("source_preflight", _preflight(vault, state)),
        pre_candidate_summary_format_preflight=kwargs.pop(
            "pre_candidate_summary_format_preflight", lambda _old: True,
        ),
        pre_candidate_summary_format_paths=kwargs.pop(
            "pre_candidate_summary_format_paths",
            (Path(maintenance.__file__), Path(repaired_retry.__file__),
             ROOT / "curate_once.py", ROOT / "caretaker" / "draft_summary.py"),
        ),
        **kwargs,
    )


class RepairedBatchRetryTests(unittest.TestCase):
    def test_terminal_generation_q_row_requires_exact_preflight_and_one_shot_token(self):
        _, vault, state = _workspace(self)
        summary = _summary(vault)
        db = _db()
        self.addCleanup(db.close)
        meta = summary['eligible'][0]
        project = meta['project']
        paths = [meta['path']]
        pids = [meta['proposal_id']]
        hashes = [meta['sha256']]
        old_cycle = 'audit-new-terminal-source'
        old_batch = batch_key(project, paths)
        db.execute(
            'INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) '
            'VALUES(?,?,?,?,?,?)',
            (old_cycle, 'threshold', NOW.isoformat(), 'DRAINED', '{}', 'new'),
        )
        db.execute(
            'INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (old_cycle, pids[0], paths[0], hashes[0], project, group_key(meta),
             'quarantined', 'QUARANTINED'),
        )
        old_result = {
            'status': 'QUARANTINED', 'reason': TERMINAL_GENERATION_SOURCE_REASON,
            'original_reason': TERMINAL_GENERATION_SOURCE_REASON,
            'batch': old_batch, 'cycle_id': old_cycle,
            'proposal_ids': pids, 'paths': paths, 'hashes': hashes,
            'project': project, 'state_dir': str(state), 'candidate_path': None,
            'publication': None, 'checkpoint': None,
        }
        db.execute(
            'INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            (old_batch, 'QUARANTINED', 4, json.dumps(old_result), 4, '',
             'deterministic', 'a' * 64, NOW.isoformat()),
        )
        ensure_retry_table(db)
        db.commit()

        from caretaker.repaired_retry import _valid_phase_resume_row
        self.assertIsNotNone(_valid_phase_resume_row(
            old_batch, 'QUARANTINED', 4, 4, 'deterministic', 'a' * 64,
            json.dumps(old_result),
        ))
        from caretaker.pubstate import canonical_verdict
        self.assertEqual(canonical_verdict(vault, {
            'batch': old_batch, 'status': 'QUARANTINED', 'category': 'deterministic',
            'fingerprint': 'a' * 64, 'result': json.dumps(old_result),
        })[0], 'absent')

        from caretaker.terminal_generation import helper_sha256
        from caretaker.repaired_retry import (
            PHASE_RESUME_CODE_VERSION, PHASE_RESUME_CURATE_SHA256,
            curate_sha256,
        )
        self.assertEqual(code_version(), PHASE_RESUME_CODE_VERSION)
        self.assertEqual(curate_sha256(ROOT / 'curate_once.py'), PHASE_RESUME_CURATE_SHA256)
        preflight_calls = []

        def terminal_preflight(old, metas, plan_ok):
            preflight_calls.append((old, metas))
            return ({'batch': old['batch']} if plan_ok else None)

        def candidate(plan_ok=True):
            return repaired_retry_candidates(
                db, summary, 'legacy', lambda name: name == project, vault,
                source_preflight=_preflight(vault, state),
                publication_path=ROOT / 'caretaker' / 'publication.py',
                curate_path=ROOT / 'curate_once.py',
                phase_cache_preflight=lambda *_args: False,
                terminal_generation_preflight=lambda old, metas: terminal_preflight(
                    old, metas, plan_ok),
                terminal_generation_path=ROOT / 'caretaker' / 'terminal_generation.py',
            )

        eligible = candidate()
        self.assertTrue(preflight_calls, 'terminal preflight was not reached')
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0]['reason'], TERMINAL_GENERATION_RETRY_REASON)
        self.assertEqual(eligible[0]['previous_batch'], old_batch)
        self.assertEqual(eligible[0]['helper_sha256'], helper_sha256())
        self.assertEqual(eligible[0]['token'], retry_token(
            REPAIRED_PUBLICATION_SHA256, project, pids[0], paths[0], hashes[0],
            reason=TERMINAL_GENERATION_RETRY_REASON, helper_sha256=helper_sha256(),
        ))
        self.assertEqual(candidate(plan_ok=False), [])

        ensure_retry_table(db)
        token = eligible[0]['token']
        db.execute(
            'INSERT INTO repaired_retry_tokens(token,cycle_id,publication_sha256,code_version,proposal_id,path,sha256,previous_batch) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (token, 'audit-legacy-terminal-retry', eligible[0]['publication_sha256'],
             eligible[0]['code_version'], pids[0], paths[0], hashes[0], old_batch),
        )
        db.commit()
        self.assertEqual(candidate(), [])

    def _seed_spent_summary_failed_entry(self, conn, summary, state, vault,
                                         *, mutate_cycle=None, mutate_result=None,
                                         add_duplicate=False, helper=None):
        from caretaker.draft_summary import helper_sha256

        old_key, old_result = _seed_failure(
            conn, summary, state, reason=SUMMARY_REPAIR_SOURCE_REASON,
        )
        conn.execute(
            "UPDATE audit_cycles SET id=? WHERE id=?",
            ("audit-legacy-summary-source-old", "cycle-old"),
        )
        conn.execute(
            "UPDATE audit_members SET cycle_id=? WHERE cycle_id=?",
            ("audit-legacy-summary-source-old", "cycle-old"),
        )
        old_result["cycle_id"] = "audit-legacy-summary-source-old"
        conn.execute(
            "UPDATE publications SET result=? WHERE batch=?",
            (json.dumps(old_result), old_key),
        )
        meta = summary["eligible"][0]
        identity = (meta["proposal_id"], meta["path"], meta["sha256"])
        old_helper = helper or ("c" * 64)
        current_version = code_version()
        retry = {
            "proposal_id": identity[0], "path": identity[1], "sha256": identity[2],
            "project": "test", "reason": SUMMARY_REPAIR_REASON,
            "previous_batch": old_key,
            "publication_sha256": REPAIRED_PUBLICATION_SHA256,
            "code_version": current_version, "helper_sha256": old_helper,
        }
        retry["token"] = retry_token(
            REPAIRED_PUBLICATION_SHA256, "test", *identity,
            reason=SUMMARY_REPAIR_REASON, helper_sha256=old_helper,
        )
        cycle_id = "audit-legacy-spent-summary64"
        cycle_counts = {"repaired_retry": [retry]}
        if mutate_cycle:
            mutate_cycle(cycle_counts, retry)
        conn.execute(
            "INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)",
            (cycle_id, "threshold", "2026-10-02T00:00:00+00:00", "DRAINED",
             json.dumps(cycle_counts), "legacy"),
        )
        conn.execute(
            "INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (cycle_id, *identity, "test", group_key(meta), "failed", "BLOCKED"),
        )
        ensure_retry_table(conn)
        conn.execute(
            "INSERT INTO repaired_retry_tokens(token,cycle_id,publication_sha256,code_version,proposal_id,path,sha256,previous_batch) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (retry["token"], cycle_id, retry["publication_sha256"], retry["code_version"],
             retry["proposal_id"], retry["path"], retry["sha256"], old_key),
        )
        successor = retry_batch_key("test", [{
            "proposal_id": identity[0], "path": identity[1], "sha256": identity[2],
        }], [retry])
        failed = {
            "status": "BLOCKED", "reason": "phase_resume_terminal_no_replay",
            "original_reason": "phase_resume_terminal_no_replay",
            "batch": successor, "cycle_id": cycle_id,
            "proposal_ids": [identity[0]], "paths": [identity[1]],
            "hashes": [identity[2]], "project": "test",
            "candidate_path": None, "model_calls": 1,
            "publication": None, "checkpoint": None,
        }
        if mutate_result:
            mutate_result(failed)
        conn.execute(
            "INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (successor, "BLOCKED", 1, json.dumps(failed), 1, "", "deterministic",
             "d" * 64, "2026-10-02T00:00:00+00:00"),
        )
        if add_duplicate:
            duplicate = dict(failed, batch=successor + "-duplicate")
            conn.execute(
                "INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (successor + "-duplicate", "BLOCKED", 1, json.dumps(duplicate), 1,
                 "", "deterministic", "e" * 64, "2026-10-02T00:00:00+00:00"),
            )
        conn.commit()
        return old_key, old_result, successor, retry, helper_sha256()

    def test_summary_retry_reopens_only_its_exact_spent_terminal_entry(self):
        _, vault, state = _workspace(self)
        summary = _summary(vault)
        db = _db()
        self.addCleanup(db.close)
        old_key, _old_result, failed_batch, spent, current_helper = (
            self._seed_spent_summary_failed_entry(db, summary, state, vault)
        )
        old_row = db.execute(
            "SELECT status,result,failures,attempt,category,fingerprint FROM publications WHERE batch=?",
            (old_key,),
        ).fetchone()
        failed_row = db.execute(
            "SELECT status,result,failures,attempt,category,fingerprint FROM publications WHERE batch=?",
            (failed_batch,),
        ).fetchone()
        candidates = _candidate(
            db, summary, vault, state,
            summary_repair_preflight=lambda _old: True,
            summary_repair_path=ROOT / "caretaker" / "draft_summary.py",
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["proposal_id"], summary["eligible"][0]["proposal_id"])
        self.assertEqual(candidates[0]["previous_batch"], old_key)
        self.assertEqual(candidates[0]["helper_sha256"], current_helper)
        self.assertNotEqual(candidates[0]["token"], spent["token"])
        self.assertEqual(db.execute(
            "SELECT status,result,failures,attempt,category,fingerprint FROM publications WHERE batch=?",
            (old_key,),
        ).fetchone(), old_row)
        self.assertEqual(db.execute(
            "SELECT status,result,failures,attempt,category,fingerprint FROM publications WHERE batch=?",
            (failed_batch,),
        ).fetchone(), failed_row)

    def test_summary_retry_rejects_changed_or_ambiguous_terminal_entry(self):
        from caretaker.draft_summary import helper_sha256

        bad_cases = [
            {"mutate_result": lambda value: value.update(reason="unrelated")},
            {"mutate_result": lambda value: value.update(candidate_path="candidate")},
            {"mutate_result": lambda value: value.update(publication={"status": "PUBLISHED"})},
            {"mutate_result": lambda value: value.update(hashes=["0" * 64])},
            {"mutate_cycle": lambda _counts, item: item.update(previous_batch="foreign")},
            {"add_duplicate": True},
            {"helper": helper_sha256()},
        ]
        for case in bad_cases:
            with self.subTest(case=case):
                _, vault, state = _workspace(self)
                summary = _summary(vault)
                db = _db()
                self.addCleanup(db.close)
                self._seed_spent_summary_failed_entry(db, summary, state, vault, **case)
                candidates = _candidate(
                    db, summary, vault, state,
                    summary_repair_preflight=lambda _old: True,
                    summary_repair_path=ROOT / "caretaker" / "draft_summary.py",
                )
                self.assertEqual(candidates, [])

    def test_spent_summary_entry_uses_normal_legacy_tick_and_fresh_marker(self):
        root, vault, state = _workspace(self)
        summary = _summary(vault)
        state.mkdir(exist_ok=True)
        db = sqlite3.connect(state / "intake.sqlite")
        ensure_tables(db)
        ensure_pub_tables(db)
        self.addCleanup(db.close)
        old_key, _old_result, failed_batch, _spent, _helper = (
            self._seed_spent_summary_failed_entry(db, summary, state, vault)
        )
        base = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        base.update(project_slug="test", state_dir=str(state))
        base["hub"]["vault"] = str(vault)
        base["hub"]["script"] = str(root / "synthetic-knowledge.py")
        config_path, cfg = maintenance._project_runtime_cfg(
            base, "test", state, lane="legacy", database=db,
        )
        control = {"publication": {
            "enabled": True, "include_existing_hub_backlog": True,
            "projects": ["test"], "held_projects": [], "max_batch": 8,
            "min_ready_groups": 1, "max_wait_hours": 24,
        }}
        calls = []

        def invoke(path, paths):
            from caretaker.config import load_config

            marker = maintenance._validated_summary_retry_marker(
                load_config(path), paths,
            )
            self.assertIsNotNone(marker)
            self.assertEqual(marker["old_result"]["batch"], old_key)
            calls.append(list(paths))
            return {"status": "PUBLISHED", "model_calls": 0}

        with patch.object(maintenance, "_audit_summary", return_value=summary), \
                patch.object(maintenance, "_summary_repair_preflight", return_value=True), \
                patch.object(maintenance, "_invoke_curate", side_effect=invoke):
            result = maintenance.publication_tick(
                str(config_path), cfg, control, db, now=NOW, lane="legacy",
            )

        self.assertEqual(result["status"], "PUBLISHED", result)
        self.assertEqual(calls, [[summary["eligible"][0]["path"]]])
        self.assertNotEqual(result["batch"], old_key)
        self.assertEqual(db.execute(
            "SELECT status FROM publications WHERE batch=?", (failed_batch,),
        ).fetchone()[0], "BLOCKED")
        self.assertEqual(db.execute(
            "SELECT status FROM publications WHERE batch=?", (old_key,),
        ).fetchone()[0], "QUARANTINED")
        self.assertEqual(db.execute(
            "SELECT COUNT(*) FROM repaired_retry_tokens",
        ).fetchone()[0], 2)  # one spent historical token and one fresh token

    def test_pre_candidate_invalid_summary_retries_once_in_normal_tick(self):
        _, vault, state = _workspace(self)
        summary = _summary(vault)
        db = _db()
        self.addCleanup(db.close)
        old_key, _old_result, _old_fp = _seed_blocked_invalid_summary(
            db, summary, state, vault,
        )
        old_row = db.execute(
            "SELECT batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at "
            "FROM publications WHERE batch=?", (old_key,),
        ).fetchone()
        control = {"publication": {
            "enabled": True, "include_existing_hub_backlog": True,
            "projects": ["test"], "held_projects": [], "max_batch": 8,
            "min_ready_groups": 1, "max_wait_hours": 24,
        }}
        cfg = {
            "hub": {"vault": str(vault)}, "project_slug": "test",
            "state_dir": str(state), "maintenance_state_dir": str(state),
        }
        calls = []

        def invoke(_config_path, paths):
            calls.append(list(paths))
            ledger_dir = vault / "governance/curations"
            ledger_dir.mkdir(parents=True, exist_ok=True)
            (ledger_dir / "evt-synthetic-retry.json").write_text(json.dumps({
                "event_id": "evt-synthetic-retry",
                "proposals": [{
                    "proposal_id": summary["eligible"][0]["proposal_id"],
                    "disposition": "synthesized",
                }],
            }), encoding="utf-8")
            return {"status": "PUBLISHED", "model_calls": 2}

        with patch.object(maintenance, "_audit_summary", return_value=summary), \
                patch.object(maintenance, "_invoke_curate", side_effect=invoke):
            result = maintenance.publication_tick(
                "synthetic-config", cfg, control, db, now=NOW, lane="new",
            )

        self.assertEqual(result["status"], "PUBLISHED", result)
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(calls, [[summary["eligible"][0]["path"]]])
        self.assertTrue(result["batch"].startswith("repaired-"))
        self.assertNotEqual(result["batch"], old_key)
        self.assertEqual(db.execute(
            "SELECT batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at "
            "FROM publications WHERE batch=?", (old_key,),
        ).fetchone(), old_row)
        successor = db.execute(
            "SELECT counts FROM audit_cycles WHERE id=?", (result["cycle_id"],),
        ).fetchone()
        retry_item = json.loads(successor[0])["repaired_retry"][0]
        self.assertEqual(retry_item["reason"], INVALID_SUMMARY_FORMAT_RETRY_REASON)
        self.assertEqual(retry_item["previous_batch"], old_key)
        self.assertEqual(db.execute(
            "SELECT COUNT(*) FROM repaired_retry_tokens",
        ).fetchone()[0], 1)
        self.assertEqual(_format_candidate(db, summary, vault, state), [])

        with patch.object(maintenance, "_audit_summary", return_value=summary), \
                patch.object(maintenance, "_invoke_curate", side_effect=invoke):
            second = maintenance.publication_tick(
                "synthetic-config", cfg, control, db,
                now=NOW + dt.timedelta(minutes=1), lane="new",
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(second.get("model_calls", 0), 0)
        self.assertEqual(db.execute(
            "SELECT COUNT(*) FROM repaired_retry_tokens",
        ).fetchone()[0], 1)

    def test_pre_candidate_invalid_summary_retry_rejects_other_reasons_and_uncertainty(self):
        cases = [
            {"mutate": lambda value: value.update(reason="unsafe body:secret")},
            {"mutate": lambda value: value.update(candidate_path="candidate/publication.json")},
            {"mutate": lambda value: value.update(model_calls=2)},
            {"mutate": lambda value: value.update(format_repair_exhausted=True)},
            {"mutate": lambda value: value.update(publication={"status": "PUBLISHED"})},
            {"mutate": lambda value: value.update(hashes=["0" * 64])},
        ]
        for case in cases:
            with self.subTest(case=case):
                _, vault, state = _workspace(self)
                summary = _summary(vault)
                db = _db()
                self.addCleanup(db.close)
                _seed_blocked_invalid_summary(db, summary, state, vault, **case)
                self.assertEqual(_format_candidate(db, summary, vault, state), [])
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repaired_retry_tokens'"
                ).fetchone())

        _, vault, state = _workspace(self)
        summary = _summary(vault)
        db = _db()
        self.addCleanup(db.close)
        _seed_blocked_invalid_summary(db, summary, state, vault)
        self.assertEqual(_format_candidate(
            db, summary, vault, state,
            pre_candidate_summary_format_preflight=lambda _old: False,
        ), [])
        self.assertEqual(_format_candidate(
            db, summary, vault, state,
            pre_candidate_summary_format_paths=(Path(__file__), Path(maintenance.__file__)),
        ), [])

        _, vault, state = _workspace(self)
        summary = _summary(vault)
        db = _db()
        self.addCleanup(db.close)
        _seed_blocked_invalid_summary(
            db, summary, state, vault,
            mutate=lambda value: value.update(candidate_path="candidate/publication.json"),
        )
        meta = summary["eligible"][0]
        db.execute(
            "INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)",
            ("audit-new-current-pending", "threshold", NOW.isoformat(), "ACTIVE", "{}", "new"),
        )
        db.execute(
            "INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("audit-new-current-pending", meta["proposal_id"], meta["path"],
             meta["sha256"], meta["project"], group_key(meta), "pending", ""),
        )
        db.commit()
        control = {"publication": {
            "enabled": True, "include_existing_hub_backlog": True,
            "projects": ["test"], "held_projects": [], "max_batch": 8,
            "min_ready_groups": 1, "max_wait_hours": 24,
        }}
        cfg = {
            "hub": {"vault": str(vault)}, "project_slug": "test",
            "state_dir": str(state), "maintenance_state_dir": str(state),
        }
        calls = []
        with patch.object(maintenance, "_audit_summary", return_value=summary), \
                patch.object(maintenance, "_invoke_curate",
                             side_effect=lambda *_: calls.append(True) or {"status": "PUBLISHED"}):
            result = maintenance.publication_tick(
                "synthetic-config", cfg, control, db, now=NOW, lane="new",
            )
        self.assertEqual(calls, [])
        self.assertTrue(any(
            item.get("reason") == "pre_candidate_invalid_summary_retry_not_admitted"
            for item in result.get("skipped", [])
        ), result)

    def test_normal_successor_uses_separate_row_and_preserves_old_evidence_once(self):
        _, vault, state = _workspace(self)
        summary = _summary(vault)
        db = _db()
        self.addCleanup(db.close)
        old_key, _ = _seed_failure(db, summary, state)
        old_row = db.execute(
            "SELECT batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at "
            "FROM publications WHERE batch=?", (old_key,),
        ).fetchone()
        old_member = db.execute(
            "SELECT cycle_id,proposal_id,path,sha256,status,reason FROM audit_members"
        ).fetchone()
        control = {"publication": {
            "enabled": True, "include_existing_hub_backlog": True,
            "projects": ["test"], "held_projects": [], "max_batch": 8,
            "min_ready_groups": 1, "max_wait_hours": 24,
        }}
        cfg = {"hub": {"vault": str(vault)}, "project_slug": "test", "state_dir": str(state)}
        called = []
        with patch.object(maintenance, "_audit_summary", return_value=summary), \
                patch.object(maintenance, "_invoke_curate", side_effect=lambda *_: (called.append(True) or {"status": "PUBLISHED"})):
            result = maintenance.publication_tick("synthetic-config", cfg, control, db, now=NOW)
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(len(called), 1)
        self.assertTrue(result["batch"].startswith("repaired-"))
        self.assertNotEqual(result["batch"], old_key)
        self.assertEqual(db.execute(
            "SELECT batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at "
            "FROM publications WHERE batch=?", (old_key,),
        ).fetchone(), old_row)
        self.assertEqual(db.execute(
            "SELECT cycle_id,proposal_id,path,sha256,status,reason FROM audit_members "
            "WHERE cycle_id='cycle-old'"
        ).fetchone(), old_member)
        successor = db.execute(
            "SELECT counts FROM audit_cycles WHERE id=?", (result["cycle_id"],)
        ).fetchone()
        retry_evidence = json.loads(successor[0])["repaired_retry"]
        self.assertEqual(len(retry_evidence), 1)
        self.assertEqual(retry_evidence[0]["previous_batch"], old_key)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM repaired_retry_tokens").fetchone()[0], 1)

        again = maintenance.publication_tick("synthetic-config", cfg, control, db, now=NOW)
        self.assertIn(again["status"], {"IDLE", "NO_PENDING", "SKIPPED_NO_DUE_BATCH"})
        self.assertEqual(len(called), 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM repaired_retry_tokens").fetchone()[0], 1)

    def test_unknown_unsupported_and_malformed_history_fail_closed(self):
        cases = [
            {"reason": "some_other_error"},
            {"status": "UNKNOWN_REQUIRES_RECONCILIATION"},
            {"status": "RUNNING"},
            {"status": "PUBLISHED"},
            {"fingerprint": "not-a-sha256"},
            {"mutate": lambda value: value.update(batch="wrong-batch")},
            {"mutate": lambda value: value.update(paths=["../outside.md"])},
        ]
        for case in cases:
            with self.subTest(case=case):
                _, vault, state = _workspace(self)
                summary = _summary(vault)
                db = _db()
                self.addCleanup(db.close)
                key, _ = _seed_failure(db, summary, state, **case)
                self.assertEqual(_candidate(db, summary, vault, state), [])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_cycles").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_members").fetchone()[0], 1)
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM publications WHERE batch=?", (key,)
                ).fetchone()[0], 1)
                self.assertIsNone(db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repaired_retry_tokens'"
                ).fetchone())

        _, vault, state = _workspace(self)
        summary = _summary(vault)
        db = _db()
        self.addCleanup(db.close)
        _seed_failure(db, summary, state)
        wrong_code = TASK_TMP / "wrong-publication.py"
        wrong_code.write_text("# different code\n", encoding="utf-8")
        self.assertEqual(_candidate(db, summary, vault, state, publication_path=wrong_code), [])

    def test_pending_duplicate_and_unsafe_source_are_not_admitted(self):
        _, vault, state = _workspace(self)
        summary = _summary(vault)
        db = _db()
        self.addCleanup(db.close)
        _seed_failure(db, summary, state)
        meta = summary["eligible"][0]
        db.execute(
            "INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)",
            ("cycle-pending", "threshold", NOW.isoformat(), "ACTIVE", "{}", "legacy"),
        )
        db.execute(
            "INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("cycle-pending", meta["proposal_id"], meta["path"], meta["sha256"],
             meta["project"], group_key(meta), "pending", ""),
        )
        db.commit()
        self.assertEqual(_candidate(db, summary, vault, state), [])
        db.execute("DELETE FROM audit_members WHERE cycle_id='cycle-pending'")
        db.execute("DELETE FROM audit_cycles WHERE id='cycle-pending'")
        db.commit()
        self.assertEqual(_candidate(
            db, summary, vault, state, source_preflight=lambda _meta: False,
        ), [])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_cycles").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_members").fetchone()[0], 1)

    def test_mixed_old_batch_admits_only_its_safe_exact_member_subset(self):
        _, vault, state = _workspace(self)
        second = vault / "proposals/agent/two.md"
        second.write_text(
            "---\nid: prop-two\nclassification: internal\nproject_slug: test\n"
            "source_ids:\n  - src-one\ntarget_pages:\n  - wiki/projects/test.md\n---\n"
            "Another scoped synthetic report.\n",
            encoding="utf-8",
        )
        summary = _summary(vault)
        self.assertEqual(len(summary["eligible"]), 2)
        db = _db()
        self.addCleanup(db.close)
        metas = summary["eligible"]
        project = metas[0]["project"]
        paths = [meta["path"] for meta in metas]
        pids = [meta["proposal_id"] for meta in metas]
        hashes = [meta["sha256"] for meta in metas]
        key = batch_key(project, paths)
        db.execute(
            "INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) VALUES(?,?,?,?,?,?)",
            ("cycle-old", "threshold", "2026-10-01T00:00:00+00:00", "DRAINED", "{}", "legacy"),
        )
        for meta in metas:
            db.execute(
                "INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("cycle-old", meta["proposal_id"], meta["path"], meta["sha256"],
                 project, group_key(meta), "quarantined", "QUARANTINED"),
            )
        prior = {
            "status": "QUARANTINED", "reason": REPAIRED_REASON,
            "original_reason": REPAIRED_REASON, "batch": key, "cycle_id": "cycle-old",
            "proposal_ids": pids, "paths": paths, "hashes": hashes,
            "project": project, "state_dir": str(state),
        }
        db.execute(
            "INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (key, "QUARANTINED", 2, json.dumps(prior), 2, "", "deterministic", "b" * 64,
             "2026-10-01T00:00:00+00:00"),
        )
        db.commit()
        candidates = _candidate(
            db, summary, vault, state,
            source_preflight=lambda meta: meta["proposal_id"] == "prop-one",
        )
        self.assertEqual([item["proposal_id"] for item in candidates], ["prop-one"])
        retry_summary = {**summary, "repaired_retry": candidates}
        cycle = maybe_start_cycle(
            db, retry_summary, now_iso=NOW.isoformat(), lane="legacy",
            project_allowed=lambda _project: True,
        )
        self.assertEqual([item["proposal_id"] for item in cycle["members"]], ["prop-one"])
        self.assertEqual(cycle["counts"]["repaired_retry"], candidates)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_members").fetchone()[0], 3)

    def test_retry_keeps_canonical_and_batch_source_proofs_with_unknown_history(self):
        _, vault, state = _workspace(self)
        good = _summary(vault)["eligible"][0]

        bad_sid = "src-shared-local-only"
        bad_body = b"Synthetic local-only source."
        bad_dir = vault / "raw/sources/2026/10" / bad_sid
        bad_dir.mkdir(parents=True)
        (bad_dir / "original.md").write_bytes(bad_body)
        (bad_dir / "source.json").write_text(json.dumps({
            "source_id": bad_sid,
            "classification": "internal",
            "local_only": True,
            "original_file": "original.md",
            "sha256": sha(bad_body),
            "byte_length": len(bad_body),
        }), encoding="utf-8")

        def proposal(pid, source_id):
            path = f"proposals/agent/{pid}.md"
            content = (
                "---\n"
                f"id: {pid}\nclassification: internal\nproject_slug: test\n"
                f"source_ids:\n  - {source_id}\n"
                "target_pages:\n  - wiki/projects/test.md\n"
                "---\n\nA synthetic integration proposal.\n"
            ).encode("utf-8")
            target = vault / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            return {
                "proposal_id": pid, "path": path, "sha256": sha(content),
                "project": "test", "producer": "agent", "episode": "",
                "source_ids": [source_id],
            }

        bad_members = [proposal("prop-shared-bad-a", bad_sid),
                       proposal("prop-shared-bad-b", bad_sid)]
        unknown = proposal("prop-unknown-history", "src-one")
        summary = _summary(vault)
        eligible = [good, *bad_members, unknown]
        summary.update(
            trigger="threshold", eligible=eligible, raw_eligible=len(eligible),
            group_count=1, ready_group_count=1,
        )

        db = _db()
        self.addCleanup(db.close)
        old_key, _ = _seed_failure(db, {"eligible": [good]}, state)
        unknown_key = batch_key("test", [unknown["path"]])
        unknown_result = {
            "status": "UNKNOWN_REQUIRES_RECONCILIATION",
            "batch": unknown_key,
            "cycle_id": "cycle-unknown",
            "proposal_ids": [unknown["proposal_id"]],
            "paths": [unknown["path"]],
            "hashes": [unknown["sha256"]],
            "project": "test",
        }
        db.execute(
            "INSERT INTO audit_cycles(id,trigger,created_at,status,counts,lane) "
            "VALUES(?,?,?,?,?,?)",
            ("cycle-unknown", "threshold", NOW.isoformat(), "DRAINED", "{}", "legacy"),
        )
        db.execute(
            "INSERT INTO audit_members(cycle_id,proposal_id,path,sha256,project,group_key,status,reason) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("cycle-unknown", unknown["proposal_id"], unknown["path"],
             unknown["sha256"], "test", group_key(unknown), "unknown",
             "UNKNOWN_REQUIRES_RECONCILIATION"),
        )
        db.execute(
            "INSERT INTO publications(batch,status,failures,result,attempt,next_due,category,fingerprint,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (unknown_key, "UNKNOWN_REQUIRES_RECONCILIATION", 1,
             json.dumps(unknown_result), 1, "", "uncertain", "c" * 64,
             NOW.isoformat()),
        )
        db.commit()

        control = {"publication": {
            "enabled": True, "include_existing_hub_backlog": True,
            "projects": ["test"], "held_projects": [], "max_batch": 8,
            "min_ready_groups": 1, "max_wait_hours": 24,
        }}
        cfg = {
            "hub": {"vault": str(vault)},
            "project_slug": "test",
            "state_dir": str(state),
        }
        calls = []

        def invoke(_config_path, paths):
            calls.append(list(paths))
            return {"status": "PUBLISHED"}

        from caretaker.publication import freeze as real_freeze
        from caretaker.pubstate import canonical_verdict as real_canonical_verdict
        source_checks = []
        canonical_checks = []

        def checked_freeze(root, paths, *args, **kwargs):
            source_checks.append(tuple(paths))
            return real_freeze(root, paths, *args, **kwargs)

        def checked_canonical(root, row):
            verdict = real_canonical_verdict(root, row)
            canonical_checks.append((row.get("batch"), verdict[0]))
            return verdict

        with patch.object(maintenance, "_audit_summary", return_value=summary), \
                patch.object(maintenance, "_invoke_curate", side_effect=invoke), \
                patch("caretaker.publication.freeze", side_effect=checked_freeze), \
                patch("caretaker.pubstate.canonical_verdict", side_effect=checked_canonical):
            result = maintenance.publication_tick(
                "synthetic-config", cfg, control, db, now=NOW,
            )

        self.assertEqual(result["status"], "PUBLISHED")
        self.assertTrue(result["batch"].startswith("repaired-"))
        self.assertEqual(calls, [[good["path"]]])
        self.assertEqual(source_checks, [(good["path"],)])
        self.assertIn((old_key, "absent"), canonical_checks)
        successor = db.execute(
            "SELECT counts FROM audit_cycles WHERE id=?", (result["cycle_id"],),
        ).fetchone()
        retry_evidence = json.loads(successor[0])["repaired_retry"]
        self.assertEqual([item["proposal_id"] for item in retry_evidence], [good["proposal_id"]])
        statuses = dict(db.execute(
            "SELECT proposal_id,status FROM audit_members WHERE cycle_id=?",
            (result["cycle_id"],),
        ).fetchall())
        self.assertEqual(statuses, {
            good["proposal_id"]: "published",
            bad_members[0]["proposal_id"]: "quarantined",
            bad_members[1]["proposal_id"]: "quarantined",
        })
        self.assertNotIn(unknown["proposal_id"], statuses)
        self.assertEqual(db.execute(
            "SELECT status FROM publications WHERE batch=?", (unknown_key,),
        ).fetchone()[0], "UNKNOWN_REQUIRES_RECONCILIATION")
        self.assertEqual(db.execute(
            "SELECT status FROM publications WHERE batch=?", (old_key,),
        ).fetchone()[0], "QUARANTINED")

    def test_held_scope_and_empty_summary_invoke_no_model(self):
        _, vault, state = _workspace(self)
        summary = _summary(vault)
        db = _db()
        self.addCleanup(db.close)
        _seed_failure(db, summary, state)
        cfg = {"hub": {"vault": str(vault)}, "project_slug": "test", "state_dir": str(state)}
        held = {"publication": {
            "enabled": True, "include_existing_hub_backlog": True,
            "projects": ["other"], "held_projects": [], "max_batch": 8,
        }}
        allowed = {"publication": {
            "enabled": True, "include_existing_hub_backlog": True,
            "projects": ["test"], "held_projects": [], "max_batch": 8,
        }}
        calls = []
        with patch.object(maintenance, "_audit_summary", return_value=summary), \
                patch.object(maintenance, "_invoke_curate", side_effect=lambda *_: calls.append(True)):
            held_result = maintenance.publication_tick("synthetic-config", cfg, held, db, now=NOW)
        self.assertEqual(held_result["status"], "HELD_PROJECT_SCOPE")
        empty = {**summary, "eligible": [], "raw_eligible": 0,
                 "group_count": 0, "ready_group_count": 0, "trigger": None}
        with patch.object(maintenance, "_audit_summary", return_value=empty), \
                patch.object(maintenance, "_invoke_curate", side_effect=lambda *_: calls.append(True)):
            empty_result = maintenance.publication_tick("synthetic-config", cfg, allowed, db, now=NOW)
        self.assertIn(empty_result["status"], {"IDLE", "NO_PENDING", "SKIPPED_NO_DUE_BATCH"})
        self.assertEqual(empty_result["model_calls"], 0)
        self.assertEqual(calls, [])

    def test_accounted_or_partial_canonical_evidence_never_retries(self):
        _, vault, state = _workspace(self)
        summary = _summary(vault)
        db = _db()
        self.addCleanup(db.close)
        _seed_failure(db, summary, state)
        meta = summary["eligible"][0]
        ledger = vault / "governance/curations/partial.json"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(json.dumps({"proposals": [{
            "proposal_id": meta["proposal_id"], "path": meta["path"],
            "sha256": meta["sha256"], "disposition": "synthesized",
        }]}), encoding="utf-8")
        self.assertEqual(_candidate(db, summary, vault, state), [])

    def test_changed_sha_uses_normal_fresh_identity_flow(self):
        _, vault, state = _workspace(self)
        initial = _summary(vault)
        db = _db()
        self.addCleanup(db.close)
        _seed_failure(db, initial, state)
        proposal = vault / initial["eligible"][0]["path"]
        proposal.write_text(proposal.read_text(encoding="utf-8") + "Changed input.\n", encoding="utf-8")
        changed = _summary(vault)
        self.assertNotEqual(changed["eligible"][0]["sha256"], initial["eligible"][0]["sha256"])
        self.assertEqual(_candidate(db, changed, vault, state), [])
        cycle = maybe_start_cycle(
            db, changed, now_iso=NOW.isoformat(), lane="legacy", project_allowed=lambda _p: True,
        )
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle["members"][0]["sha256"], changed["eligible"][0]["sha256"])
        self.assertNotIn("repaired_retry", cycle["counts"])
        self.assertIsNone(db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repaired_retry_tokens'"
        ).fetchone())

    def test_failed_preflight_has_no_cycle_or_token_side_effect(self):
        _, vault, state = _workspace(self)
        summary = _summary(vault)
        db = _db()
        self.addCleanup(db.close)
        old_key, _ = _seed_failure(db, summary, state)
        before_publication = db.execute(
            "SELECT status,result,fingerprint FROM publications WHERE batch=?", (old_key,),
        ).fetchone()
        self.assertEqual(_candidate(
            db, summary, vault, state, source_preflight=lambda _meta: False,
        ), [])
        cycle = maybe_start_cycle(
            db, summary, now_iso=NOW.isoformat(), lane="legacy", project_allowed=lambda _p: True,
        )
        self.assertIsNone(cycle)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_cycles").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM audit_members").fetchone()[0], 1)
        self.assertEqual(db.execute(
            "SELECT status,result,fingerprint FROM publications WHERE batch=?", (old_key,),
        ).fetchone(), before_publication)
        self.assertIsNone(db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repaired_retry_tokens'"
        ).fetchone())


if __name__ == "__main__":
    unittest.main()
