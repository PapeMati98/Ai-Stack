import json
from pathlib import Path
import tempfile
import unittest
from caretaker.publication import (
    PublicationError, safe_file, _ids, bind_review, sha, freeze, build_candidate,
    response_object, classify_body_error, is_format_error,
    _validate_publication_decision, _validate_curator_fix,
)

class PublicationBoundaryTests(unittest.TestCase):
    def test_publication_and_curator_accept_prose_tool_field_words(self):
        frozen = {"proposals": [], "sources": {"src-one": {}}}
        for body in (
            "Windows arguments were repaired; the tool output was reviewed.",
            "The `tool_calls` and `tool_input` names appear in this documentation.",
        ):
            with self.subTest(body=body):
                decision = {"body": body, "summary": "Reviewed.", "dispositions": {}}
                self.assertEqual(_validate_publication_decision(frozen, decision)[0], body)
                _validate_curator_fix({"text": body, "source_id": "src-one"}, frozen)

    def test_publication_and_curator_reject_structural_dumps_with_reason(self):
        frozen = {"proposals": [], "sources": {"src-one": {}}}
        bodies = ('A field follows: "arguments": {}',
                  "A field follows: 'tool_calls': []",
                  "Some context.\ntool_calls: []",
                  "Some context, tool_input=value",
                  "Some context <tool_output>value</tool_output>.")
        for body in bodies:
            with self.subTest(body=body):
                decision = {"body": body, "summary": "Reviewed.", "dispositions": {}}
                with self.assertRaises(PublicationError) as pub_err:
                    _validate_publication_decision(frozen, decision)
                self.assertEqual(str(pub_err.exception), "unsafe body:tool_dump")
                with self.assertRaises(PublicationError) as curator_err:
                    _validate_curator_fix({"text": body, "source_id": "src-one"}, frozen)
                self.assertEqual(str(curator_err.exception),
                                 "curator_amendment_rejected:unsafe body:tool_dump")

    def test_publication_and_curator_name_secret_and_reasoning_checks(self):
        frozen = {"proposals": [], "sources": {"src-one": {}}}
        for body, suffix in (("A leaked token sk-" + "A" * 24, "secret"),
                             ("A private chain_of_thought appeared.", "reasoning_marker")):
            with self.subTest(suffix=suffix):
                decision = {"body": body, "summary": "Reviewed.", "dispositions": {}}
                with self.assertRaises(PublicationError) as pub_err:
                    _validate_publication_decision(frozen, decision)
                self.assertEqual(str(pub_err.exception), f"unsafe body:{suffix}")
                with self.assertRaises(PublicationError) as curator_err:
                    _validate_curator_fix({"text": body, "source_id": "src-one"}, frozen)
                self.assertEqual(str(curator_err.exception),
                                 f"curator_amendment_rejected:unsafe body:{suffix}")

    def test_body_format_errors_are_distinguished(self):
        self.assertEqual(classify_body_error({"x": 1}), "body_wrong_type")
        self.assertEqual(classify_body_error("---\nbody"), "body_has_frontmatter")
        self.assertEqual(classify_body_error("tiny"), "body_too_short")
        self.assertEqual(classify_body_error("n" * 16001), "body_too_long")
        self.assertTrue(is_format_error("invalid bounded body"))
        self.assertFalse(is_format_error("incomplete disposition coverage"))

    def test_response_accepts_only_single_complete_json_object(self):
        for text in [
            {}, '{}', '```json\n{}\n```', '```\n{}\n```',
            '\r\n```json\r\n{}\r\n```\r\n',
            '\n```\n{}\n```\n', '```json\n{}\n\n```',
        ]:
            self.assertEqual(response_object(text), {})
        for text in ['answer: {}', '{} {}', '```json\n{}\n``` trailing', '[]']:
            with self.assertRaises((ValueError, PublicationError)):
                response_object(text)

    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ['../x','C:/x','wiki/../x','/absolute','a\\b']:
                with self.subTest(name=name), self.assertRaises(PublicationError):
                    safe_file(Path(d),name)

    def test_canonical_list_shapes(self):
        self.assertEqual(_ids('---\nsource_ids:\n  - src-a\n  - src-b\n---\nbody','source_ids'),['src-a','src-b'])
        self.assertEqual(_ids('---\nsource_ids: ["src-a"]\n---\nbody','source_ids'),['src-a'])

    def test_review_identity_and_hash_required(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'publication.json').write_text('{}')
            good={'ok':True,'status':'ok','provider':'muse-code','model':'muse-spark-1.3','sessionId':'native-session','final':json.dumps({'verdict':'PASS','findings':[],'manifest_sha256':sha(b'{}')})}
            for patch in [{'provider':'vllm'},{'model':'muse-spark-contributor'},{'sessionId':''},{'ok':False},{'final':json.dumps({'verdict':'PASS','findings':[],'manifest_sha256':'0'*64})}]:
                with self.subTest(patch=patch),self.assertRaises(PublicationError):
                    bind_review(root,good|patch)
            self.assertTrue(bind_review(root,good).exists())
            (root/'publication.json').write_text('{"changed":true}')
            with self.assertRaises(PublicationError):bind_review(root,good)

    def test_frozen_sources_manifest_and_citation_gate(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);v=root/'vault';s=v/'raw/sources/2026/09/src-one';s.mkdir(parents=True)
            data=b'Dated report: local tool canary passed; deployment remains unverified.'
            (s/'original.md').write_bytes(data)
            meta={'source_id':'src-one','classification':'internal','local_only':False,'original_file':'original.md','sha256':sha(data),'byte_length':len(data)}
            (s/'source.json').write_text(json.dumps(meta))
            p=v/'proposals/agent/one.md';p.parent.mkdir(parents=True)
            p.write_text('---\nid: prop-one\nclassification: internal\nsource_ids:\n  - src-one\ntarget_pages:\n  - wiki/projects/test.md\n---\nReport.')
            target=v/'wiki/projects/test.md';target.parent.mkdir(parents=True)
            old='---\ntitle: Test\nstatus: reviewed\nlast_verified: "2026-01-01"\nsources: []\nproject_slug: test\n---\n\n# Test\nOld unresolved question.\n'
            target.write_text(old)
            frozen=freeze(v,['proposals/agent/one.md'],'wiki/projects/test.md')
            dec={'body':'# Test\nOld unresolved question.\nDated tool canary report (src-one); deployment unverified.','summary':'Tool canary report.','dispositions':{'prop-one':{'disposition':'synthesized','reason':'Dated and scope-limited report integrated.'}}}
            with self.assertRaises(PublicationError):build_candidate(frozen,dec|{'body':'Uncited claim with no source ID at all.'},root/'bad','2026-09-17')
            manifest=build_candidate(frozen,dec,root/'good','2026-09-17')
            value=json.loads(manifest.read_text())
            self.assertEqual(len(value['inputs']),3)
            self.assertEqual(value['source_ids'],['src-one'])
            self.assertEqual(value['proposal_ids'],['prop-one'])
            self.assertEqual(value['outputs'][0]['preimage_sha256'],sha(target.read_bytes()))
            self.assertEqual(target.read_text(),old)
            meta['classification']='restricted';(s/'source.json').write_text(json.dumps(meta))
            with self.assertRaises(PublicationError):freeze(v,['proposals/agent/one.md'],'wiki/projects/test.md')

if __name__=='__main__':unittest.main()
