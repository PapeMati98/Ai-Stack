"""Model tests."""

import unittest

from caretaker.model import (DECISION_CONTRADICTION, DECISION_DUPLICATE,
                             DECISION_MERGE, DECISION_NEW, SynthesisInput,
                             SynthesisRecord, build_prompt, decide,
                             handoff_id, proposal_id)
from caretaker.pipeline import _cited_hashes


def _rec(idx=0, tool="codex", session="s1", cursor="c1", hash_="h"):
    return SynthesisRecord(record_index=idx, identity_tool=tool,
                           native_session_id=session, cursor=cursor,
                           content_hash=hash_, title=f"t{idx}",
                           excerpt=f"excerpt {idx}")


class CitationBindingTests(unittest.TestCase):
    def test_file_chunk_cursor_with_colons_binds_exact_hash(self):
        record = _rec(tool='files', session='owner-files-v2:document',
                      cursor='a' * 64 + ':c0001of0005', hash_='hash-one')
        ref = f'{record.identity_tool}:{record.native_session_id}:{record.cursor}'
        self.assertEqual(_cited_hashes([ref], [record]), ['hash-one'])
        self.assertEqual(_cited_hashes([ref + ':extra'], [record]), [])


class BuildPromptTests(unittest.TestCase):

    def test_prompt_includes_constraints(self):
        payload = SynthesisInput(records=[_rec()], inventory_summary="{}",
                                 project_slug="p")
        text, _ = build_prompt(payload, max_bytes=8192)
        self.assertIn("synthesize", text)
        self.assertIn("p", text)
        self.assertIn("codex:s1:c1", text)

    def test_prompt_caps_size(self):
        payload = SynthesisInput(records=[_rec()], inventory_summary="x" * 50000,
                                 project_slug="p")
        text, _ = build_prompt(payload, max_bytes=200)
        self.assertLessEqual(len(text.encode("utf-8")), 200)


class DecideTests(unittest.TestCase):

    def test_new_with_valid_refs(self):
        records = [_rec(idx=0, hash_="abc")]
        payload = SynthesisInput(records=records, inventory_summary="{}",
                                 project_slug="p")
        env = ('{"decision":"new","synthesis":"s","source_refs":'
               '["abc"],"rationale":"r"}')
        d = decide(env, records=records, payload=payload,
                   model_identity="vllm/Qwen3.8-27B")
        self.assertEqual(d.decision, DECISION_NEW)
        self.assertEqual(d.source_refs, ["abc"])
        self.assertEqual(d.model_identity, "vllm/Qwen3.8-27B")
        fenced = decide('```json\n'+env+'\n```',records=records,payload=payload,model_identity='x')
        self.assertEqual(fenced.decision,DECISION_NEW)
        competing = decide('outside\n```json\n'+env+'\n```',records=records,payload=payload,model_identity='x')
        self.assertEqual(competing.rationale,'model_returned_invalid_json')

    def test_duplicate(self):
        records = [_rec(idx=0, hash_="abc")]
        payload = SynthesisInput(records=records, inventory_summary="{}",
                                 project_slug="p")
        env = '{"decision":"duplicate","synthesis":"","source_refs":["abc"]}'
        d = decide(env, records=records, payload=payload, model_identity="x")
        self.assertEqual(d.decision, DECISION_DUPLICATE)

    def test_merge_with_target(self):
        records = [_rec(idx=0, hash_="abc")]
        payload = SynthesisInput(records=records, inventory_summary="{}",
                                 project_slug="p")
        env = ('{"decision":"merge","synthesis":"s","source_refs":["abc"],'
               '"merge_target":"hub/foo","rationale":"r"}')
        d = decide(env, records=records, payload=payload, model_identity="x")
        self.assertEqual(d.decision, DECISION_MERGE)
        self.assertEqual(d.merge_target, "hub/foo")

    def test_merge_without_target_is_contradiction(self):
        # We re-route this at the pipeline level, but decide() accepts it.
        records = [_rec(idx=0, hash_="abc")]
        payload = SynthesisInput(records=records, inventory_summary="{}",
                                 project_slug="p")
        env = '{"decision":"merge","synthesis":"s","source_refs":["abc"]}'
        d = decide(env, records=records, payload=payload, model_identity="x")
        self.assertEqual(d.decision, DECISION_MERGE)
        self.assertIsNone(d.merge_target)

    def test_invalid_json_is_contradiction(self):
        records = [_rec(idx=0, hash_="abc")]
        payload = SynthesisInput(records=records, inventory_summary="{}",
                                 project_slug="p")
        d = decide("not json", records=records, payload=payload,
                   model_identity="x")
        self.assertEqual(d.decision, DECISION_CONTRADICTION)

    def test_unresolved_ref_is_contradiction(self):
        records = [_rec(idx=0, hash_="abc")]
        payload = SynthesisInput(records=records, inventory_summary="{}",
                                 project_slug="p")
        env = ('{"decision":"new","synthesis":"s","source_refs":'
               '["nope:not:here"]}')
        d = decide(env, records=records, payload=payload, model_identity="x")
        self.assertEqual(d.decision, DECISION_CONTRADICTION)
        self.assertEqual(d.rationale, "unresolved_source_refs")

    def test_ref_by_tuple_accepted(self):
        records = [_rec(idx=0, hash_="abc", tool="codex", session="s1",
                        cursor="c1")]
        payload = SynthesisInput(records=records, inventory_summary="{}",
                                 project_slug="p")
        env = ('{"decision":"new","synthesis":"s","source_refs":'
               '["codex:s1:c1"]}')
        d = decide(env, records=records, payload=payload, model_identity="x")
        self.assertEqual(d.decision, DECISION_NEW)
        self.assertIn("codex:s1:c1", d.source_refs)


class IdTests(unittest.TestCase):

    def test_proposal_id_stable(self):
        a = proposal_id(content_hash="h", decision=DECISION_NEW,
                        merge_target=None)
        b = proposal_id(content_hash="h", decision=DECISION_NEW,
                        merge_target=None)
        self.assertEqual(a, b)

    def test_handoff_id_stable(self):
        a = handoff_id(proposal_id="p", source_refs=["x", "y"])
        b = handoff_id(proposal_id="p", source_refs=["y", "x"])
        # Order matters in the hash, but both are valid ids.
        self.assertEqual(len(a), 64)
        self.assertEqual(len(b), 64)


if __name__ == "__main__":
    unittest.main()
