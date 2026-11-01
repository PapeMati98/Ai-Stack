import unittest

from caretaker.publication import (PublicationError, _source_wikilinks_to_links,
                                   _validate_publication_decision)

SID = "src-20260925T144921168Z-clawops-po-uruchomieniu-office-wake-2026-09-25-49201030"
LINK = f"[{SID}](../../raw/sources/2026/09/{SID}/original.md)"


def frozen():
    return {"proposals": [{"id": "prop-20260925T144922000Z-aaaaaaaa", "source_ids": [SID]}], "sources": [SID]}


def decision(body):
    return {"body": body, "summary": "Short summary.",
            "dispositions": {"prop-20260925T144922000Z-aaaaaaaa": {"disposition": "synthesized", "reason": "Integrated."}}}


class SourceWikilinkTests(unittest.TestCase):
    def test_plain_and_aliased_source_wikilinks_become_markdown_links(self):
        text = f"Fact one [[{SID}]]. Fact two [[{SID}|the report]]."
        self.assertEqual(_source_wikilinks_to_links(text), f"Fact one {LINK}. Fact two {LINK}.")

    def test_page_wikilinks_and_plain_ids_are_unchanged(self):
        text = f"See [[projects/clawops-orchestrator-cont-7]] and {SID}."
        self.assertEqual(_source_wikilinks_to_links(text), text)

    def test_validated_body_carries_links_and_keeps_citation_rules(self):
        body, _, _, _ = _validate_publication_decision(frozen(), decision(f"The wake ledger had no entries [[{SID}]]."))
        self.assertIn(LINK, body)
        self.assertNotIn("[[src-", body)

    def test_foreign_source_is_still_rejected_after_rewrite(self):
        foreign = "src-20260925T155756649Z-other-report-9b21e44d"
        with self.assertRaisesRegex(PublicationError, "citation_outside_publication"):
            _validate_publication_decision(frozen(), decision(f"Cites [[{SID}]] and [[{foreign}]]."))

    def test_mentions_of_the_syntax_are_escaped_not_rejected(self):
        body, _, _, _ = _validate_publication_decision(
            frozen(), decision(f"Cites [[{SID}]]; the page used to show [[src-...]] and [[src-ID|alias]]."))
        self.assertIn(LINK, body)
        self.assertIn(r"[\[src-...]]", body)
        self.assertIn(r"[\[src-ID|alias]]", body)
        self.assertNotIn("[[src-", body)


    def test_placeholder_tokens_are_not_foreign_citations_but_real_ids_are(self):
        body, _, _, _ = _validate_publication_decision(
            frozen(), decision(f"Cites [[{SID}]]; placeholders src-ID and src-... stay text."))
        self.assertIn("src-ID", body)
        with self.assertRaisesRegex(PublicationError, "citation_outside_publication"):
            _validate_publication_decision(
                frozen(), decision(f"Cites {SID} and src-20260101T000000000Z-made-up-deadbeef."))

    def test_only_canonical_ids_become_links(self):
        text = "Mention [[src-20260926T123456789Z-report-deadbeefdead]] and [[src-2026-bad]]."
        out = _source_wikilinks_to_links(text)
        self.assertNotIn("](../../raw/sources/", out)
        self.assertIn(r"[\[src-20260926T123456789Z-report-deadbeefdead]]", out)
        self.assertNotIn("[[src-", out)

if __name__ == "__main__":
    unittest.main()
