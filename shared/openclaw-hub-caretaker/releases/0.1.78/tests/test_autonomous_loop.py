"""Synthetic state-transition tests for the autonomous Hub publication loop."""
from __future__ import annotations
def _redacted_fixture(kind, ordinal, binary=False):
    """Return a deterministic public synthetic fixture value.

    The result is newly authored public data derived only from the pinned
    rule label and a per-file ordinal. It is never derived from any original
    value. Runtime values retain the original detector category.
    """
    alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
    seed = (len(kind) * 131 + int(ordinal) * 7919 + 17) & 0x7FFFFFFF
    chars = []
    for _ in range(40):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        chars.append(alphabet[seed % 36])
    body = ''.join(chars)
    if kind == 'vendor-token':
        body = 'gh' + 'p_' + body
    elif kind == 'aws-access-key-id':
        body = 'AK' + 'IA' + body[:16].upper()
    elif kind == 'private-key':
        marker = 'PRIVATE' + ' KEY'
        body = ('-----' + 'BEGIN ' + marker + '-----' + chr(10) + body + chr(10)
                + '-----' + 'END ' + marker + '-----')
    if binary:
        return body.encode('ascii')
    return body



import datetime as dt
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from caretaker.audit import (
    MAX_BATCH, active_cycle, build_groups, eligible_proposals, ensure_tables,
    group_key, maybe_start_cycle, policy_from, project_batches, summarize,
)
from caretaker.publication import (
    PublicationError, build_candidate, classify_body_error, freeze,
    is_format_error, repair_prompt, safe_diagnostics, sha,
)
from caretaker.pubstate import (
    BACKOFF_CAP_SECONDS, backoff_due_iso, batch_key, canonical_verdict,
    due_to_run, ensure_pub_tables, fingerprint, read_row, reconcile_running,
    record_failure, record_start, record_success,
)
from caretaker.transports import Envelope, TransportError
from caretaker.config import load_config
from curate_once import produce_reviewed_candidate
from maintenance import publication_tick
from ._helpers import write_config


TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"
POLICY = policy_from({})
HOUR = 3600


def _mkdir_tmp():
    TASK_TMP.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(TASK_TMP))


def write_proposal(vault: Path, pid: str, project: str, source_ids, agent="codex",
                   extra="", mtime=None):
    folder = vault / "proposals" / agent
    folder.mkdir(parents=True, exist_ok=True)
    text = (
        "---\n"
        f'id: "{pid}"\n'
        'classification: "internal"\n'
        f'agent: "{agent}"\n'
        f'project_slug: "{project}"\n'
        f'target_pages: ["wiki/projects/{project}.md"]\n'
        f"source_ids: {json.dumps(list(source_ids))}\n"
        f"{extra}"
        "---\nbody\n"
    )
    path = folder / f"{pid}.md"
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def write_groups(vault: Path, n: int, project="one", agent="codex", extra="", mtime=None):
    for i in range(n):
        write_proposal(
            vault, f"prop-{i:03d}", project, [f"src-{i:03d}"],
            agent=agent, extra=extra, mtime=mtime,
        )


def memory_db():
    conn = sqlite3.connect(":memory:")
    ensure_tables(conn)
    ensure_pub_tables(conn)
    return conn


class GroupingAndTriggerTests(unittest.TestCase):
    def test_threshold_nine_idle_ten_starts_cycle(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            now = 1_800_000_000.0
            write_groups(vault, 9, mtime=now)
            summary9 = summarize(vault, POLICY, now=now)
            self.assertEqual(summary9["raw_eligible"], 9)
            self.assertEqual(summary9["group_count"], 9)
            self.assertIsNone(summary9["trigger"])
            db = memory_db()
            self.assertIsNone(maybe_start_cycle(db, summary9))
            write_proposal(vault, "prop-009", "one", ["src-009"], mtime=now)
            summary10 = summarize(vault, POLICY, now=now)
            self.assertEqual(summary10["group_count"], 10)
            self.assertEqual(summary10["trigger"], "threshold")
            cycle = maybe_start_cycle(db, summary10)
            self.assertIsNotNone(cycle)
            self.assertEqual(cycle["status"], "ACTIVE")
            self.assertEqual(len(cycle["members"]), 10)
            db.close()

    def test_age_24h_overrides_single_ready_group(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            oldest = 1_700_000_000.0
            write_proposal(vault, "prop-old", "one", ["src-old"], mtime=oldest)
            young = summarize(vault, POLICY, now=oldest + 23 * HOUR)
            self.assertIsNone(young["trigger"])
            aged = summarize(vault, POLICY, now=oldest + 24 * HOUR)
            self.assertEqual(aged["trigger"], "age")
            self.assertEqual(aged["raw_eligible"], 1)
            db = memory_db()
            cycle = maybe_start_cycle(db, aged)
            self.assertEqual(cycle["trigger"], "age")
            db.close()

    def test_episode_grouping_preserves_every_source_id(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            extra = 'episode: "sess-42"\n'
            write_proposal(vault, "prop-a", "one", ["src-a", "src-b"], extra=extra)
            write_proposal(vault, "prop-b", "one", ["src-c"], extra=extra)
            write_proposal(vault, "prop-c", "one", ["src-b", "src-d"], extra=extra)
            eligible, unsupported = eligible_proposals(vault)
            self.assertEqual(unsupported, 0)
            groups = build_groups(eligible)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["size"], 3)
            self.assertEqual(groups[0]["source_ids"], ["src-a", "src-b", "src-c", "src-d"])
            self.assertEqual(len(groups[0]["proposal_ids"]), 3)

    def test_producer_alone_does_not_collapse_distinct_sources(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            write_proposal(vault, "prop-a", "one", ["src-a"], agent="codex")
            write_proposal(vault, "prop-b", "one", ["src-b"], agent="codex")
            write_proposal(vault, "prop-c", "one", ["src-c"], agent="codex")
            groups = build_groups(eligible_proposals(vault)[0])
            self.assertEqual(len(groups), 3)
            keys = {group_key(m) for m in eligible_proposals(vault)[0]}
            self.assertTrue(all(k.startswith("sources\x1f") for k in keys))

    def test_project_batches_keep_groups_intact(self):
        members = []
        for i, size in enumerate((3, 7)):
            key = f"g{i}"
            for j in range(size):
                members.append({
                    "proposal_id": f"prop-{i}-{j}",
                    "path": f"proposals/codex/p-{i}-{j}.md",
                    "sha256": "h",
                    "project": "one",
                    "group_key": key,
                    "status": "pending",
                })
        batches, blocked = project_batches({"members": members}, 8)
        self.assertEqual(blocked, [])
        sizes = [len(batch) for batch in batches["one"]]
        self.assertEqual(sizes, [3, 7])

    def test_oversized_group_blocked_with_reason_not_dropped(self):
        members = [{
            "proposal_id": f"prop-{i}",
            "path": f"proposals/codex/p-{i}.md",
            "sha256": "h",
            "project": "one",
            "group_key": "huge",
            "status": "pending",
        } for i in range(9)]
        members.append({
            "proposal_id": "prop-ok",
            "path": "proposals/codex/ok.md",
            "sha256": "h",
            "project": "one",
            "group_key": "small",
            "status": "pending",
        })
        batches, blocked = project_batches({"members": members}, 8)
        self.assertEqual(blocked, [])
        sizes = [len(b) for b in batches["one"]]
        self.assertEqual(sizes, [8, 1, 1])
        seen = [m["proposal_id"] for batch in batches["one"] for m in batch]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), {m["proposal_id"] for m in members})

    def test_unreadable_ledger_fails_closed(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            ledger = vault / "governance" / "curations"
            ledger.mkdir(parents=True)
            (ledger / "broken.json").write_text("{not-json", encoding="utf-8")
            write_proposal(vault, "prop-a", "one", ["src-a"])
            with self.assertRaisesRegex(PublicationError, "unreadable_curation_ledger"):
                eligible_proposals(vault)

    def test_counts_come_from_proposals_not_events(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            events = vault / "events" / "2026" / "09"
            events.mkdir(parents=True)
            (events / "evt.json").write_text(json.dumps({"event_id": "evt"}), encoding="utf-8")
            write_groups(vault, 2)
            summary = summarize(vault, POLICY, now=1_800_000_000.0)
            self.assertEqual(summary["count_source"], "proposals")
            self.assertEqual(summary["raw_eligible"], 2)
            self.assertEqual(summary["group_count"], 2)

    def test_active_cycle_without_row_factory(self):
        db = sqlite3.connect(":memory:")
        self.assertIsNone(db.row_factory)
        summary = {
            "trigger": "threshold",
            "eligible": [{
                "proposal_id": "prop-1", "path": "proposals/codex/a.md",
                "sha256": "abc", "project": "one", "producer": "codex",
                "episode": "e1", "source_ids": ["src-1"],
            }],
            "raw_eligible": 1, "group_count": 1, "ready_group_count": 1,
            "oldest_age_hours": 24, "deferred": 0, "unsupported": 0,
            "policy": POLICY,
        }
        cycle = maybe_start_cycle(db, summary)
        self.assertEqual(cycle["members"][0]["proposal_id"], "prop-1")
        self.assertEqual(cycle["members"][0]["status"], "pending")
        db.close()


class FrozenCycleAndRetryTests(unittest.TestCase):
    def _cfg(self, vault, project="one"):
        return {
            "project_slug": project,
            "agent": "hub-caretaker",
            "hub": {"vault": str(vault)},
            "state_dir": str(vault.parent / "state"),
        }

    def _control(self, **overrides):
        policy = {
            "enabled": True,
            "include_existing_hub_backlog": True,
            "min_ready_groups": 1,
            "max_wait_hours": 24,
            "max_batch": 8,
        }
        policy.update(overrides)
        return {"publication": policy, "known_projects": ["one", "two"]}

    def test_empty_work_zero_model_calls(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            vault.mkdir()
            db = memory_db()
            with patch("maintenance._invoke_curate") as invoke:
                result = publication_tick("config", self._cfg(vault), self._control(), db)
            self.assertEqual(result["status"], "IDLE")
            self.assertEqual(result["model_calls"], 0)
            self.assertEqual(invoke.call_count, 0)
            db.close()

    def test_restart_frozen_cycle_holds_new_arrivals(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            now = 1_800_000_000.0
            write_groups(vault, 2, mtime=now)
            db = memory_db()
            control = self._control(min_ready_groups=2, max_batch=1)
            with patch("maintenance._invoke_curate", return_value={"status": "PUBLISHED"}) as invoke:
                first = publication_tick("config", self._cfg(vault), control, db)
                self.assertEqual(first["model_calls"], 1)
                write_proposal(vault, "prop-new", "one", ["src-new"], mtime=now)
                cycle = active_cycle(db)
                self.assertIsNotNone(cycle)
                ids = {m["proposal_id"] for m in cycle["members"]}
                self.assertNotIn("prop-new", ids)
                publication_tick("config", self._cfg(vault), control, db)
            self.assertEqual(invoke.call_count, 2)
            db.close()

    def test_transient_backoff_and_cap(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            write_proposal(vault, "prop-a", "one", ["src-a"])
            db = memory_db()
            now = dt.datetime(2026, 9, 18, tzinfo=dt.timezone.utc)
            err = TransportError("t", reason_code="native_timeout")
            with patch("curate_once.run", side_effect=err) as invoke:
                first = publication_tick("config", self._cfg(vault), self._control(), db, now=now)
                self.assertEqual(first["status"], "BACKOFF_DEFERRED")
                self.assertEqual(first["category"], "transient")
                second = publication_tick("config", self._cfg(vault), self._control(), db, now=now)
                self.assertEqual(second["status"], "SKIPPED_NO_DUE_BATCH")
                self.assertEqual(invoke.call_count, 1)
                later = now + dt.timedelta(seconds=BACKOFF_CAP_SECONDS + 1)
                third = publication_tick("config", self._cfg(vault), self._control(), db, now=later)
                self.assertEqual(third["status"], "BACKOFF_DEFERRED")
                self.assertEqual(invoke.call_count, 2)
            delay = backoff_due_iso(20, now=now)
            cap_at = (now + dt.timedelta(seconds=BACKOFF_CAP_SECONDS)).isoformat()
            self.assertEqual(delay, cap_at)
            db.close()

    def test_poison_batch_does_not_starve_another_project(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            write_proposal(vault, "prop-a", "one", ["src-a"])
            write_proposal(vault, "prop-b", "two", ["src-b"])
            db = memory_db()
            control = self._control(min_ready_groups=2)

            def fake_run(config_path, paths):
                if any("prop-a" in p for p in paths):
                    return {"status": "BLOCKED", "reason": "independent_review_not_accepted"}
                return {"status": "PUBLISHED"}

            with patch("maintenance._invoke_curate", side_effect=fake_run):
                one = publication_tick("config", self._cfg(vault, "one"), control, db)
                two = publication_tick("config", self._cfg(vault, "two"), control, db)
            self.assertIn(one["status"], {"BLOCKED", "QUARANTINED"})
            self.assertEqual(two["status"], "PUBLISHED")
            db.close()

    def test_uncertain_publish_crash_does_not_duplicate(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            write_proposal(vault, "prop-a", "one", ["src-a"])
            db = memory_db()
            control = self._control()
            cfg = self._cfg(vault)
            with patch("maintenance._invoke_curate", return_value={"status": "PUBLISHED"}) as invoke:
                first = publication_tick("config", cfg, control, db)
                self.assertEqual(invoke.call_count, 1)
                key = first["batch"]
            record_start(db, key, dt.datetime.now(dt.timezone.utc).isoformat(), "fp", {
                "proposal_ids": ["prop-a"], "paths": ["proposals/codex/prop-a.md"],
                "hashes": [sha((vault / "proposals/codex/prop-a.md").read_bytes())],
            })
            with patch("maintenance._invoke_curate") as invoke:
                second = publication_tick("config", cfg, control, db)
            self.assertEqual(invoke.call_count, 0)
            row = read_row(db, key)
            self.assertEqual(row["status"], "UNKNOWN_REQUIRES_RECONCILIATION")
            self.assertNotEqual(second.get("status"), "PUBLISHED")
            db.close()

    def test_fresh_hash_needs_fresh_review(self):
        db = memory_db()
        policy = POLICY
        paths = ["proposals/codex/a.md"]
        key = batch_key("one", paths)
        fp1 = fingerprint("one", paths, ["hash-old"], policy, "code1")
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        record_start(db, key, now, fp1, {"proposal_ids": ["prop-a"]})
        record_failure(db, key, "independent_review_not_accepted",
                       {"status": "BLOCKED", "format_repair_exhausted": True}, fp1)
        row = read_row(db, key)
        due, why = due_to_run(row, now, fp1)
        self.assertFalse(due)
        self.assertEqual(why, "quarantined_same_fingerprint")
        fp2 = fingerprint("one", paths, ["hash-new"], policy, "code1")
        due2, why2 = due_to_run(row, now, fp2)
        self.assertTrue(due2)
        self.assertEqual(why2, "fingerprint_changed")
        db.close()

    def test_canonical_status_string_is_not_proof(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            vault.mkdir()
            row = {
                "status": "RUNNING",
                "result": json.dumps({
                    "status": "PUBLISHED",
                    "proposal_ids": ["prop-a"],
                    "hashes": ["abc"],
                }),
            }
            verdict, evidence = canonical_verdict(vault, row)
            self.assertNotEqual(verdict, "committed")
            self.assertEqual(evidence.get("ignored_status_string"), "PUBLISHED")

    def test_unbound_committed_event_is_not_publication_proof(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            event_id = "evt-committed"
            events = vault / "events" / "2026" / "09"
            events.mkdir(parents=True)
            (events / f"{event_id}.json").write_text(
                json.dumps({"event_id": event_id, "action": "record-curation"}),
                encoding="utf-8",
            )
            txdir = Path(directory) / "tx"
            txdir.mkdir()
            journal = txdir / "tx.json"
            journal.write_text(json.dumps({
                "status": "committed", "event_id": event_id, "transaction": "tx-1",
            }), encoding="utf-8")
            db = memory_db()
            key = "batch-1"
            now = dt.datetime.now(dt.timezone.utc).isoformat()
            record_start(db, key, now, "fp", {
                "proposal_ids": ["prop-a"],
                "publication": {"transaction": str(journal), "event_id": event_id},
            })
            row = read_row(db, key)
            verdict, evidence = canonical_verdict(vault, row)
            self.assertNotEqual(verdict, "committed")
            rec = reconcile_running(db, key, verdict, evidence)
            self.assertEqual(rec["status"], "UNKNOWN_REQUIRES_RECONCILIATION")
            self.assertFalse(rec["republished"])
            db.close()

    def test_secret_not_leaked_in_publication_result(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            write_proposal(vault, "prop-a", "one", ["src-a"])
            db = memory_db()
            secret = (_redacted_fixture('vendor-token', 1))
            err = TransportError(f"wrapped {secret} stdout", reason_code="native_timeout")
            with patch("curate_once.run", side_effect=err):
                result = publication_tick("config", self._cfg(vault), self._control(), db)
            blob = json.dumps(result)
            self.assertNotIn(secret, blob)
            self.assertEqual(result.get("reason"), "native_timeout")
            dump = "\n".join(db.iterdump())
            self.assertNotIn(secret, dump)
            db.close()

    def test_oversized_does_not_freeze_cycle_forever(self):
        with _mkdir_tmp() as directory:
            vault = Path(directory) / "vault"
            extra = 'episode: "big"\n'
            for i in range(9):
                write_proposal(vault, f"prop-big-{i}", "one", [f"src-big-{i}"], extra=extra)
            write_proposal(vault, "prop-ok", "two", ["src-ok"])
            db = memory_db()
            control = self._control(min_ready_groups=1, max_batch=8)
            with patch("maintenance._invoke_curate", return_value={"status": "PUBLISHED"}):
                result = publication_tick("config", self._cfg(vault, "two"), control, db)
                self.assertEqual(result["status"], "PUBLISHED")
                first = publication_tick("config", self._cfg(vault, "one"), control, db)
                self.assertEqual(first["status"], "PUBLISHED")
                second = publication_tick("config", self._cfg(vault, "one"), control, db)
                self.assertEqual(second["status"], "PUBLISHED")
            cycle = active_cycle(db)
            self.assertIsNone(cycle)
            db.close()


def _valid_decision(pid="prop-one"):
    return {
        "body": "# Test\nOld unresolved question.\nDated tool canary report (src-one); deployment unverified.",
        "summary": "Tool canary report.",
        "dispositions": {pid: {"disposition": "synthesized", "reason": "Dated and scope-limited report integrated."}},
    }


def _frontmatter_decision(pid="prop-one"):
    good = _valid_decision(pid)
    good["body"] = "---\ntitle: leaked\n---\n" + good["body"]
    return good


class SequenceTransport:
    def __init__(self, finals, provider, model, session_id=None):
        self.finals = list(finals)
        self.calls = []
        self.provider = provider
        self.model = model
        self.session_id = session_id

    def invoke(self, *, spec, cwd, prompt, run_config_path, state_dir, timeout):
        self.calls.append(prompt)
        if not self.finals:
            raise AssertionError("unexpected extra model call")
        final = self.finals.pop(0)
        return Envelope(
            ok=True, status="ok", final=final, model=self.model,
            provider=self.provider, session_id=self.session_id,
            raw={
                "ok": True, "status": "ok", "provider": self.provider,
                "model": self.model, "sessionId": self.session_id, "final": final,
            },
        )


class HashPassReviewer:
    def __init__(self):
        self.calls = []

    def invoke(self, *, spec, cwd, prompt, run_config_path, state_dir, timeout):
        self.calls.append(prompt)
        pubs = sorted(Path(cwd).glob("**/publication.json"), key=lambda p: p.stat().st_mtime)
        digest = sha(pubs[-1].read_bytes())
        final = json.dumps({"verdict": "PASS", "manifest_sha256": digest, "findings": []})
        return Envelope(
            ok=True, status="ok", final=final, model="muse-spark-1.3",
            provider="muse-code", session_id="sess-1",
            raw={
                "ok": True, "status": "ok", "provider": "muse-code",
                "model": "muse-spark-1.3", "sessionId": "sess-1", "final": final,
            },
        )


class RepairThenPassReviewer:
    def __init__(self):
        self.calls = []

    def invoke(self, *, spec, cwd, prompt, run_config_path, state_dir, timeout):
        self.calls.append(prompt)
        pubs = sorted(Path(cwd).glob("**/publication.json"), key=lambda p: p.stat().st_mtime)
        digest = sha(pubs[-1].read_bytes())
        if len(self.calls) == 1:
            final = json.dumps({"verdict": "REPAIR", "manifest_sha256": digest,
                                "findings": ["body has frontmatter"]})
        else:
            final = json.dumps({"verdict": "PASS", "manifest_sha256": digest, "findings": []})
        return Envelope(
            ok=True, status="ok", final=final, model="muse-spark-1.3",
            provider="muse-code", session_id="sess-1",
            raw={
                "ok": True, "status": "ok", "provider": "muse-code",
                "model": "muse-spark-1.3", "sessionId": "sess-1", "final": final,
            },
        )


class FormatRepairTests(unittest.TestCase):
    def _vault(self, root: Path):
        vault = root / "vault"
        source = vault / "raw/sources/2026/09/src-one"
        source.mkdir(parents=True)
        data = b"Dated report: local tool canary passed; deployment remains unverified."
        (source / "original.md").write_bytes(data)
        meta = {
            "source_id": "src-one", "classification": "internal", "local_only": False,
            "original_file": "original.md", "sha256": sha(data), "byte_length": len(data),
        }
        (source / "source.json").write_text(json.dumps(meta), encoding="utf-8")
        proposal = vault / "proposals/agent/one.md"
        proposal.parent.mkdir(parents=True)
        proposal.write_text(
            "---\nid: prop-one\nclassification: internal\nsource_ids:\n  - src-one\n"
            "target_pages:\n  - wiki/projects/test.md\n---\nReport.\n",
            encoding="utf-8",
        )
        target = vault / "wiki/projects/test.md"
        target.parent.mkdir(parents=True)
        target.write_text(
            '---\ntitle: Test\nstatus: reviewed\nlast_verified: "2026-01-01"\n'
            "sources: []\nproject_slug: test\n---\n\n# Test\nOld unresolved question.\n",
            encoding="utf-8",
        )
        return vault, freeze(vault, ["proposals/agent/one.md"], "wiki/projects/test.md")

    def test_classify_body_errors_are_precise(self):
        self.assertEqual(classify_body_error(None), "body_wrong_type")
        self.assertEqual(classify_body_error("---\nx\n"), "body_has_frontmatter")
        self.assertEqual(classify_body_error("short"), "body_too_short")
        self.assertEqual(classify_body_error("x" * 16001), "body_too_long")
        self.assertTrue(is_format_error("body_has_frontmatter"))
        self.assertTrue(is_format_error("invalid bounded body"))
        self.assertFalse(is_format_error("unsafe body"))

    def test_wrong_format_repair_then_independent_review(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = self._vault(root)
            cfg_dir = root / "cfg"; cfg_dir.mkdir()
            cfg = load_config(write_config(cfg_dir))
            maker = SequenceTransport(
                [json.dumps(_frontmatter_decision()), json.dumps(_valid_decision())],
                "vllm", "Qwen3.8-27B",
            )
            reviewer = HashPassReviewer()
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run", observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid1", maker=maker, reviewer=reviewer,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(maker.calls.__len__(), 2)
            self.assertEqual(len(reviewer.calls), 1)
            self.assertIn("body_has_frontmatter", maker.calls[1])
            self.assertIn("DEFECT:", maker.calls[1])

    def test_failed_repair_preserves_pending_and_diagnostics(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = self._vault(root)
            cfg_dir = root / "cfg"; cfg_dir.mkdir()
            cfg = load_config(write_config(cfg_dir))
            bad = json.dumps(_frontmatter_decision())
            maker = SequenceTransport([bad, bad], "vllm", "Qwen3.8-27B")
            reviewer = HashPassReviewer()
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run", observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid2", maker=maker, reviewer=reviewer,
            )
            self.assertEqual(result["status"], "PENDING")
            self.assertTrue(result.get("format_repair_exhausted"))
            self.assertEqual(result.get("reason"), "body_has_frontmatter")
            self.assertEqual(len(reviewer.calls), 0)
            diag = safe_diagnostics(result)
            blob = json.dumps(diag)
            self.assertNotIn("leaked", blob)
            self.assertIn("run_path", diag)

    def test_review_repair_does_not_reuse_pass(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = self._vault(root)
            cfg_dir = root / "cfg"; cfg_dir.mkdir()
            cfg = load_config(write_config(cfg_dir))
            maker = SequenceTransport(
                [json.dumps(_valid_decision()), json.dumps(_valid_decision())],
                "vllm", "Qwen3.8-27B",
            )
            reviewer = RepairThenPassReviewer()
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run", observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid3", maker=maker, reviewer=reviewer,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(len(reviewer.calls), 2)
            self.assertEqual(len(maker.calls), 2)
            self.assertIn("review_REPAIR", maker.calls[1])

    def test_review_repair_can_repair_maker_format(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            _, frozen = self._vault(root)
            cfg_dir = root / "cfg"; cfg_dir.mkdir()
            cfg = load_config(write_config(cfg_dir))
            maker = SequenceTransport(
                [json.dumps(_valid_decision()), "not json", json.dumps(_valid_decision())],
                "vllm", "Qwen3.8-27B",
            )
            reviewer = RepairThenPassReviewer()
            (root / "run").mkdir()
            result = produce_reviewed_candidate(
                frozen=frozen, run_root=root / "run", observed="2026-09-18T00:00:00+00:00",
                cfg=cfg, rid="rid-review-format", maker=maker, reviewer=reviewer,
            )
            self.assertEqual(result["status"], "REVIEWED_CANDIDATE")
            self.assertEqual(len(maker.calls), 3)
            self.assertEqual(len(reviewer.calls), 2)
            self.assertIn("review_REPAIR", maker.calls[1])
            self.assertIn("maker_response_not_json", maker.calls[2])

    def test_reviewer_outage_defers_without_self_review(self):
        with _mkdir_tmp() as directory:
            root = Path(directory)
            vault, frozen = self._vault(root)
            cfg_dir = root / "cfg"; cfg_dir.mkdir()
            cfg = load_config(write_config(cfg_dir))

            class BoomReviewer:
                def invoke(self, **kwargs):
                    raise TransportError("down", reason_code="native_timeout")

            maker = SequenceTransport([json.dumps(_valid_decision())], "vllm", "Qwen3.8-27B")
            (root / "run").mkdir()
            with self.assertRaisesRegex(PublicationError, "independent_review_deferred:native_timeout"):
                produce_reviewed_candidate(
                    frozen=frozen, run_root=root / "run",
                    observed="2026-09-18T00:00:00+00:00",
                    cfg=cfg, rid="rid4", maker=maker, reviewer=BoomReviewer(),
                )
            self.assertEqual(len(maker.calls), 1)

    def test_existing_long_page_rotates_to_continuation(self):
        frozen = {"before": "x" * 16001, "target": "wiki/projects/test.md",
                  "sources": {}, "proposals": []}
        from caretaker.publication import synthesis_prompt
        prompt = synthesis_prompt(frozen)
        self.assertEqual(frozen["target"], "wiki/projects/test-cont-2.md")
        self.assertIn("wiki/projects/test.md", frozen["before"])
        self.assertIn("PACKET", prompt)
        prompt = repair_prompt(
            {"target": "t", "before": "b", "sources": {}, "proposals": []},
            "body_has_frontmatter",
        )
        self.assertIn("Never truncate", prompt)


if __name__ == "__main__":
    unittest.main()
