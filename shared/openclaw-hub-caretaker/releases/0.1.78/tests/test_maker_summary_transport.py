import json
import unittest
from types import SimpleNamespace
import curate_once

class MakerSummaryTransportTests(unittest.TestCase):
    def setUp(self):
        self.value = {"body": "Source-grounded Unicode: Łódź, żółć, 漢字. Literal brace }.", "summary": "Known summary", "dispositions": {"prop-fixture": {"disposition": "synthesized", "reason": "fixture"}}}
    def decode(self, value):
        envelope = {"status": "completed", "summary": value, "files_changed": [], "verification": ["fixture"], "risks": [], "next_steps": []}
        return curate_once._decision_from_maker(SimpleNamespace(final=json.dumps(envelope,ensure_ascii=False)))
    def test_valid_unchanged(self):
        self.assertEqual(self.decode(json.dumps(self.value,ensure_ascii=False)), self.value)
    def test_one_trailing_closing_brace_recovered_without_content_change(self):
        self.assertEqual(self.decode(json.dumps(self.value,ensure_ascii=False)+"}"),self.value)
    def test_literal_braces_in_body_are_unchanged(self):
        self.value["body"]="{{quoted}} \n stays literal"
        self.assertEqual(self.decode(json.dumps(self.value)+"}"),self.value)
    def test_double_excess_braces_refused(self):
        with self.assertRaises(curate_once.PublicationError): self.decode(json.dumps(self.value)+"}}")
    def test_second_object_refused(self):
        with self.assertRaises(curate_once.PublicationError): self.decode(json.dumps(self.value)+" {}")
    def test_trailing_prose_refused(self):
        with self.assertRaises(curate_once.PublicationError): self.decode(json.dumps(self.value)+" publish }")
    def test_truncated_object_refused(self):
        with self.assertRaises(curate_once.PublicationError): self.decode(json.dumps(self.value)[:-1])
    def test_wrong_schema_not_repaired(self):
        with self.assertRaises(curate_once.PublicationError): self.decode('{"status":"completed"}}')
    def test_wrong_types_not_repaired(self):
        self.value["dispositions"]=[]
        with self.assertRaises(curate_once.PublicationError): self.decode(json.dumps(self.value)+"}")
    def test_placeholder_still_refused(self):
        with self.assertRaises(curate_once.PublicationError): self.decode("test")
