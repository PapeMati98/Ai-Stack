"""Differential tests for the invocation-local critical-handoff event index."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import knowledge


class CountingEvent(dict):
    gets = 0

    def get(self, key, default=None):
        CountingEvent.gets += 1
        return dict.get(self, key, default)


class CountingRecords(list):
    def __init__(self, items):
        super().__init__(items)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def baseline_critical_handoff_integrity(vault, proposal_content, sources, events):
    """Independent copy of the receipt predicates from before the event index."""
    source_index = sources or {
        str(metadata.get("source_id")): (metadata, path)
        for metadata, path in knowledge.source_records(vault)
        if metadata
    }
    event_records = events if events is not None else knowledge.readable_event_records(vault)
    proposal_id = str(knowledge.fm_value(proposal_content, "id") or "")
    project_slug = str(knowledge.fm_value(proposal_content, "project_slug") or "")
    state_summary = str(knowledge.fm_value(proposal_content, "state_summary") or "")
    agent = str(knowledge.fm_value(proposal_content, "agent") or "")
    source_ids = sorted(set(knowledge.fm_list(proposal_content, "source_ids")))
    issues: list[str] = []

    if not source_ids:
        issues.append("missing-source")
    proposal_events = [
        event
        for event, _ in event_records
        if event.get("action") == "new-proposal"
        and proposal_id in (event.get("proposal_ids") or [])
        and sorted(event.get("source_ids") or []) == source_ids
    ]
    if len(proposal_events) != 1:
        issues.append(f"proposal-event-cardinality:{len(proposal_events)}")

    for source_id in source_ids:
        source_record = source_index.get(source_id)
        if source_record is None:
            issues.append(f"missing-source:{source_id}")
            continue
        metadata, metadata_path = source_record
        if (
            metadata.get("project_slug") != project_slug
            or metadata.get("knowledge_kind") != "critical-state"
            or metadata.get("state_summary") != state_summary
            or metadata.get("captured_by") != agent
        ):
            issues.append(f"source-metadata-mismatch:{source_id}")
        original = metadata_path.parent / str(metadata.get("original_file") or "")
        if not original.is_file():
            issues.append(f"missing-original:{source_id}")
        elif (
            knowledge.sha256_file(original) != metadata.get("sha256")
            or original.stat().st_size != metadata.get("byte_length")
        ):
            issues.append(f"source-integrity:{source_id}")
        stage_events = [
            event
            for event, _ in event_records
            if event.get("action") == "stage-source" and event.get("source_ids") == [source_id]
        ]
        if len(stage_events) != 1:
            issues.append(f"source-event-cardinality:{source_id}:{len(stage_events)}")

    return {
        "status": "COMPLETE" if not issues else "INCOMPLETE",
        "issues": sorted(set(issues)),
    }


def proposal_text(proposal_id, source_ids, project_slug="alpha-project", state_summary="Synthetic state", agent="codex", knowledge_kind=None):
    joined = ", ".join(source_ids)
    kind = f"knowledge_kind: {knowledge_kind}\n" if knowledge_kind else ""
    return (
        "---\n"
        f"id: {proposal_id}\n"
        f"project_slug: {project_slug}\n"
        f"state_summary: {state_summary}\n"
        f"agent: {agent}\n"
        f"{kind}"
        f"source_ids: [{joined}]\n"
        "---\n"
        "body\n"
    )


def event_record(event, name):
    return (event, Path(name))


class CriticalEventIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / "vault"
        self.vault.mkdir()
        CountingEvent.gets = 0

    def tearDown(self):
        self.temp.cleanup()

    def write_source(self, source_id, *, body=b"payload", write_original=True, **overrides):
        directory = self.vault / "raw" / "sources" / source_id
        directory.mkdir(parents=True, exist_ok=True)
        original_name = overrides.pop("original_file", "original.bin")
        if write_original:
            (directory / original_name).write_bytes(body)
        metadata = {
            "source_id": source_id,
            "project_slug": "alpha-project",
            "knowledge_kind": "critical-state",
            "state_summary": "Synthetic state",
            "captured_by": "codex",
            "original_file": original_name,
            "sha256": hashlib.sha256(body).hexdigest(),
            "byte_length": len(body),
        }
        metadata.update(overrides)
        path = directory / "source.json"
        path.write_text(json.dumps(metadata), encoding="utf-8")
        return metadata, path

    def assert_matches_baseline(self, content, sources, events):
        expected = baseline_critical_handoff_integrity(self.vault, content, sources, events)
        direct = knowledge.critical_handoff_integrity(
            self.vault, content, sources=sources, events=events,
        )
        lookup = knowledge.critical_handoff_event_lookup(events)
        indexed = knowledge.critical_handoff_integrity(
            self.vault, content, sources=sources, events=events, event_lookup=lookup,
        )
        self.assertEqual(direct, expected)
        self.assertEqual(indexed, expected)
        return expected

    def test_receipt_cases_match_baseline(self):
        metadata, path = self.write_source("src-a", body=b"alpha-bytes")
        other_metadata, other_path = self.write_source("src-b", body=b"beta-bytes")
        sources = {"src-a": (metadata, path), "src-b": (other_metadata, other_path)}
        stage_a = {"action": "stage-source", "source_ids": ["src-a"], "event_id": "stage-a"}
        stage_b = {"action": "stage-source", "source_ids": ["src-b"], "event_id": "stage-b"}
        valid = {
            "action": "new-proposal",
            "proposal_ids": ["prop-valid"],
            "source_ids": ["src-a"],
            "event_id": "proposal-valid",
        }
        cases = {
            "valid": (
                proposal_text("prop-valid", ["src-a"]),
                [event_record(valid, "valid.json"), event_record(stage_a, "stage-a.json")],
                "COMPLETE",
                [],
            ),
            "missing": (
                proposal_text("prop-valid", ["src-a"]),
                [event_record(stage_a, "stage-a.json")],
                "INCOMPLETE",
                ["proposal-event-cardinality:0"],
            ),
            "duplicate-events": (
                proposal_text("prop-valid", ["src-a"]),
                [
                    event_record(valid, "valid-1.json"),
                    event_record(dict(valid, event_id="proposal-valid-2"), "valid-2.json"),
                    event_record(stage_a, "stage-a.json"),
                ],
                "INCOMPLETE",
                ["proposal-event-cardinality:2"],
            ),
            "repeated-proposal-id": (
                proposal_text("prop-valid", ["src-a"]),
                [
                    event_record(
                        dict(valid, proposal_ids=["prop-valid", "prop-valid", "prop-other"]),
                        "repeated.json",
                    ),
                    event_record(stage_a, "stage-a.json"),
                    event_record(
                        {"action": "record-curation", "proposal_ids": ["prop-valid"], "source_ids": ["src-a"]},
                        "unrelated.json",
                    ),
                ],
                "COMPLETE",
                [],
            ),
            "multiple-sources": (
                proposal_text("prop-multi", ["src-b", "src-a"]),
                [
                    event_record(
                        {
                            "action": "new-proposal",
                            "proposal_ids": ["prop-multi"],
                            "source_ids": ["src-b", "src-a"],
                            "event_id": "multi",
                        },
                        "multi.json",
                    ),
                    event_record(stage_a, "stage-a.json"),
                    event_record(stage_b, "stage-b.json"),
                    event_record(
                        {"action": "stage-source", "source_ids": ["src-a", "src-b"], "event_id": "not-singleton"},
                        "combined.json",
                    ),
                    event_record(
                        {
                            "action": "new-proposal",
                            "proposal_ids": ["prop-other"],
                            "source_ids": ["src-a"],
                            "event_id": "other",
                        },
                        "other.json",
                    ),
                    event_record(
                        {
                            "action": "new-proposal",
                            "proposal_ids": ["prop-multi"],
                            "source_ids": ["src-a"],
                            "event_id": "partial",
                        },
                        "partial.json",
                    ),
                ],
                "COMPLETE",
                [],
            ),
            "duplicate-stage-not-masked": (
                proposal_text("prop-valid", ["src-a"]),
                [
                    event_record(valid, "valid.json"),
                    event_record(stage_a, "stage-a-1.json"),
                    event_record(dict(stage_a, event_id="stage-a-2"), "stage-a-2.json"),
                    event_record(
                        {"action": "stage-source", "source_ids": ["src-unrelated"], "event_id": "other-stage"},
                        "other-stage.json",
                    ),
                ],
                "INCOMPLETE",
                ["source-event-cardinality:src-a:2"],
            ),
            "event-source-duplicates-are-not-unique": (
                proposal_text("prop-valid", ["src-a"]),
                [
                    event_record(dict(valid, source_ids=["src-a", "src-a"]), "dup-source.json"),
                    event_record(dict(stage_a, source_ids=["src-a", "src-a"]), "dup-stage.json"),
                ],
                "INCOMPLETE",
                ["proposal-event-cardinality:0", "source-event-cardinality:src-a:0"],
            ),
        }
        for name, (content, events, status, issues) in cases.items():
            with self.subTest(name=name):
                actual = self.assert_matches_baseline(content, sources, events)
                self.assertEqual(actual["status"], status)
                self.assertEqual(actual["issues"], issues)

    def test_source_metadata_hash_and_byte_length_mismatches(self):
        metadata, path = self.write_source("src-a", body=b"alpha-bytes")
        sources = {"src-a": (metadata, path)}
        events = [
            event_record(
                {"action": "new-proposal", "proposal_ids": ["prop-valid"], "source_ids": ["src-a"]},
                "proposal.json",
            ),
            event_record({"action": "stage-source", "source_ids": ["src-a"]}, "stage.json"),
        ]
        content = proposal_text("prop-valid", ["src-a"])
        mismatched = dict(metadata, captured_by="other-agent")
        hashed = dict(metadata, sha256="0" * 64)
        resized = dict(metadata, byte_length=len(b"alpha-bytes") + 9)
        missing_file = dict(metadata, original_file="missing.bin")
        absent = {"src-other": sources["src-a"]}
        checks = {
            "metadata": ({"src-a": (mismatched, path)}, ["source-metadata-mismatch:src-a"]),
            "hash": ({"src-a": (hashed, path)}, ["source-integrity:src-a"]),
            "byte-length": ({"src-a": (resized, path)}, ["source-integrity:src-a"]),
            "missing-original": ({"src-a": (missing_file, path)}, ["missing-original:src-a"]),
            "missing-source": (absent, ["missing-source:src-a"]),
        }
        for name, (case_sources, issues) in checks.items():
            with self.subTest(name=name):
                actual = self.assert_matches_baseline(content, case_sources, events)
                self.assertEqual(actual["status"], "INCOMPLETE")
                self.assertEqual(actual["issues"], issues)

    def test_malformed_proposal_ids_keep_original_membership(self):
        metadata, path = self.write_source("src-a")
        # Single-character ids make sorted("ba") comparable with ["a", "b"].
        char_a, char_path = self.write_source("a", body=b"a")
        char_b, char_b_path = self.write_source("b", body=b"b")
        sources = {"src-a": (metadata, path), "a": (char_a, char_path), "b": (char_b, char_b_path)}
        content = proposal_text("zeta", ["src-a"])
        events = [
            event_record(
                {"action": "new-proposal", "proposal_ids": ["zeta", "zeta"], "source_ids": ["src-a"]},
                "exact.json",
            ),
            event_record(
                {"action": "new-proposal", "proposal_ids": "[[zeta]]", "source_ids": ["src-a"]},
                "substring.json",
            ),
            event_record(
                {"action": "new-proposal", "proposal_ids": "yyy", "source_ids": ["src-a"]},
                "string-miss.json",
            ),
            event_record(
                {"action": "new-proposal", "proposal_ids": ("zeta",), "source_ids": ["src-a"]},
                "tuple.json",
            ),
            event_record(
                {"action": "new-proposal", "proposal_ids": ("zeta-extra",), "source_ids": ["src-a"]},
                "tuple-miss.json",
            ),
            event_record(
                {"action": "new-proposal", "proposal_ids": ["zeta-extra"], "source_ids": ["src-a"]},
                "list-prefix.json",
            ),
            event_record(
                {"action": "new-proposal", "proposal_ids": {"zeta": True}, "source_ids": ["src-a"]},
                "dict-key.json",
            ),
            event_record(
                {"action": "new-proposal", "proposal_ids": ["zeta"], "source_ids": ["other"]},
                "wrong-source.json",
            ),
            event_record(
                {"action": "record-curation", "proposal_ids": ["zeta"], "source_ids": ["src-a"]},
                "unrelated.json",
            ),
            event_record({"action": "stage-source", "source_ids": ["src-a"]}, "stage.json"),
            event_record({"action": "stage-source", "source_ids": "src-a"}, "stage-string.json"),
        ]
        actual = self.assert_matches_baseline(content, sources, events)
        self.assertEqual(actual["issues"], ["proposal-event-cardinality:4"])

        char_content = proposal_text("prop-char", ["b", "a"])
        char_events = [
            event_record(
                {"action": "new-proposal", "proposal_ids": ["prop-char"], "source_ids": "ba"},
                "sorted-chars.json",
            ),
            event_record({"action": "stage-source", "source_ids": ["a"]}, "stage-a.json"),
            event_record({"action": "stage-source", "source_ids": ["b"]}, "stage-b.json"),
            event_record(
                {"action": "new-proposal", "proposal_ids": ["prop-char"], "source_ids": ["a"]},
                "one-source.json",
            ),
        ]
        char_actual = self.assert_matches_baseline(char_content, sources, char_events)
        self.assertEqual(char_actual["status"], "COMPLETE")
        self.assertEqual(char_actual["issues"], [])

    def test_non_dict_event_still_raises(self):
        metadata, path = self.write_source("src-a")
        sources = {"src-a": (metadata, path)}
        events = [
            event_record(
                {"action": "new-proposal", "proposal_ids": ["prop-valid"], "source_ids": ["src-a"]},
                "valid.json",
            ),
            (["not-a-dict"], Path("corrupt.json")),
        ]
        content = proposal_text("prop-valid", ["src-a"])
        with self.assertRaises(AttributeError):
            baseline_critical_handoff_integrity(self.vault, content, sources, events)
        with self.assertRaises(AttributeError):
            knowledge.critical_handoff_integrity(self.vault, content, sources=sources, events=events)
        lookup = knowledge.critical_handoff_event_lookup(events)
        with self.assertRaises(AttributeError):
            knowledge.critical_handoff_integrity(
                self.vault, content, sources=sources, events=events, event_lookup=lookup,
            )

    def test_empty_sources_mapping_still_reloads_vault_records(self):
        metadata, path = self.write_source("src-a", body=b"from-disk")
        events = [
            event_record(
                {"action": "new-proposal", "proposal_ids": ["prop-valid"], "source_ids": ["src-a"]},
                "proposal.json",
            ),
            event_record({"action": "stage-source", "source_ids": ["src-a"]}, "stage.json"),
        ]
        content = proposal_text("prop-valid", ["src-a"])
        self.assertEqual(metadata["source_id"], "src-a")
        self.assertTrue(path.is_file())
        actual = self.assert_matches_baseline(content, {}, events)
        self.assertEqual(actual, {"status": "COMPLETE", "issues": []})

    def test_distinct_calls_see_fresh_mutations(self):
        metadata, path = self.write_source("src-a")
        sources = {"src-a": (metadata, path)}
        content = proposal_text("prop-valid", ["src-a"])
        proposal = {"action": "new-proposal", "proposal_ids": ["prop-valid"], "source_ids": ["src-a"]}
        stage = {"action": "stage-source", "source_ids": ["src-a"]}
        events = [event_record(proposal, "proposal.json"), event_record(stage, "stage.json")]
        first = knowledge.critical_handoff_integrity(self.vault, content, sources=sources, events=events)
        self.assertEqual(first["status"], "COMPLETE")
        events.append(event_record(dict(proposal, event_id="second"), "proposal-2.json"))
        second = knowledge.critical_handoff_integrity(self.vault, content, sources=sources, events=events)
        self.assertEqual(second, self.assert_matches_baseline(content, sources, events))
        self.assertEqual(second["issues"], ["proposal-event-cardinality:2"])

        live = {"action": "new-proposal", "proposal_ids": ["prop-valid"], "source_ids": ["src-a"]}
        live_events = [event_record(live, "live.json"), event_record(dict(stage), "stage-live.json")]
        before = knowledge.critical_handoff_integrity(
            self.vault, content, sources=sources, events=live_events,
        )
        live["action"] = "record-curation"
        after = knowledge.critical_handoff_integrity(
            self.vault, content, sources=sources, events=live_events,
        )
        self.assertEqual(before["status"], "COMPLETE")
        self.assertEqual(after, baseline_critical_handoff_integrity(self.vault, content, sources, live_events))
        self.assertIn("proposal-event-cardinality:0", after["issues"])

        slotted = [event_record(dict(proposal), "slot.json"), event_record(dict(stage), "slot-stage.json")]
        lookup = knowledge.critical_handoff_event_lookup(slotted)
        kept = knowledge.critical_handoff_integrity(
            self.vault, content, sources=sources, events=slotted, event_lookup=lookup,
        )
        slotted[0] = event_record(
            {"action": "new-proposal", "proposal_ids": ["other"], "source_ids": ["src-a"]},
            "replaced.json",
        )
        replaced = knowledge.critical_handoff_integrity(
            self.vault, content, sources=sources, events=slotted, event_lookup=lookup,
        )
        self.assertEqual(kept["status"], "COMPLETE")
        self.assertEqual(replaced, baseline_critical_handoff_integrity(self.vault, content, sources, slotted))
        self.assertIn("proposal-event-cardinality:0", replaced["issues"])

        grown = [event_record(dict(proposal), "grown.json"), event_record(dict(stage), "grown-stage.json")]
        grown_lookup = knowledge.critical_handoff_event_lookup(grown)
        grown.append(event_record(dict(proposal, event_id="extra"), "extra.json"))
        grown_result = knowledge.critical_handoff_integrity(
            self.vault, content, sources=sources, events=grown, event_lookup=grown_lookup,
        )
        self.assertEqual(grown_result, baseline_critical_handoff_integrity(self.vault, content, sources, grown))
        self.assertEqual(grown_result["issues"], ["proposal-event-cardinality:2"])

        forged = {
            "new_by_proposal": {},
            "stage_by_source": {},
            "fallback_new": [],
            "fallback_stage": [],
            "_records": grown,
            "_records_len": len(grown),
        }
        forged_result = knowledge.critical_handoff_integrity(
            self.vault, content, sources=sources, events=grown, event_lookup=forged,
        )
        self.assertEqual(forged_result, grown_result)
        self.assertNotEqual(forged_result["issues"], [])

    def test_index_scans_are_bounded_for_one_thousand_proposals(self):
        metadata, path = self.write_source("src-scale", body=b"scale")
        sources = {"src-scale": (metadata, path)}
        items = []
        proposal_ids = [f"prop-{index:04d}" for index in range(1000)]
        for index, proposal_id in enumerate(proposal_ids):
            proposal_ids_field = [proposal_id, proposal_id] if index == 0 else [proposal_id]
            items.append(event_record(
                CountingEvent(
                    action="new-proposal",
                    proposal_ids=proposal_ids_field,
                    source_ids=["src-scale"],
                    event_id=f"proposal-{index}",
                ),
                f"proposal-{index}.json",
            ))
        items.append(event_record(
            CountingEvent(action="stage-source", source_ids=["src-scale"], event_id="stage-scale"),
            "stage-scale.json",
        ))
        items.append(event_record(
            CountingEvent(action="stage-source", source_ids=["src-scale-extra"], event_id="stage-extra"),
            "stage-extra.json",
        ))
        for index in range(998):
            items.append(event_record(
                CountingEvent(
                    action="record-curation",
                    proposal_ids=[proposal_ids[index % 1000]],
                    source_ids=["src-scale"],
                    event_id=f"unrelated-{index}",
                ),
                f"unrelated-{index}.json",
            ))
        self.assertEqual(len(items), 2000)
        records = CountingRecords(items)
        lookup = knowledge.critical_handoff_event_lookup(records)
        self.assertEqual(records.iterations, 1)
        CountingEvent.gets = 0
        indexed = []
        for proposal_id in proposal_ids:
            indexed.append(knowledge.critical_handoff_integrity(
                self.vault,
                proposal_text(proposal_id, ["src-scale"]),
                sources=sources,
                events=records,
                event_lookup=lookup,
            ))
        self.assertEqual(records.iterations, 1)
        self.assertLess(CountingEvent.gets, 25000)
        self.assertGreater(CountingEvent.gets, 0)
        self.assertTrue(all(result == {"status": "COMPLETE", "issues": []} for result in indexed))
        for proposal_id in (proposal_ids[0], proposal_ids[500], proposal_ids[-1]):
            expected = baseline_critical_handoff_integrity(
                self.vault,
                proposal_text(proposal_id, ["src-scale"]),
                sources,
                items,
            )
            actual = knowledge.critical_handoff_integrity(
                self.vault,
                proposal_text(proposal_id, ["src-scale"]),
                sources=sources,
                events=items,
            )
            self.assertEqual(actual, expected)
            self.assertEqual(actual["status"], "COMPLETE")

    def test_lint_builds_one_lookup_for_every_critical_proposal(self):
        vault = self.vault
        (vault / "proposals" / "codex").mkdir(parents=True)
        (vault / "events").mkdir()
        (vault / "knowledge-vault.json").write_text(
            json.dumps({"curator_machine": "TEST", "curation_scope": "designated-machine-only"}),
            encoding="utf-8",
        )
        for index in (1, 2):
            (vault / "proposals" / "codex" / f"prop-{index}.md").write_text(
                proposal_text(f"prop-{index}", ["src-missing"], knowledge_kind="critical-state"),
                encoding="utf-8",
            )
        calls = {"lookup": 0, "integrity": 0, "lookups": []}
        real_lookup = knowledge.critical_handoff_event_lookup
        real_integrity = knowledge.critical_handoff_integrity

        def wrapped_lookup(event_records):
            calls["lookup"] += 1
            return real_lookup(event_records)

        def wrapped_integrity(*args, **kwargs):
            calls["integrity"] += 1
            calls["lookups"].append(kwargs.get("event_lookup"))
            return real_integrity(*args, **kwargs)

        with mock.patch.object(knowledge, "critical_handoff_event_lookup", wrapped_lookup), mock.patch.object(
            knowledge, "critical_handoff_integrity", wrapped_integrity,
        ):
            result = knowledge.lint(vault)
        self.assertEqual(calls["lookup"], 1)
        self.assertEqual(calls["integrity"], 2)
        self.assertIs(calls["lookups"][0], calls["lookups"][1])
        self.assertIsNotNone(calls["lookups"][0])
        rendered = "\n".join(result["errors"])
        self.assertEqual(rendered.count("Incomplete critical handoff receipt"), 2)
        self.assertEqual(rendered.count("proposal-event-cardinality:0"), 2)


if __name__ == "__main__":
    unittest.main()
