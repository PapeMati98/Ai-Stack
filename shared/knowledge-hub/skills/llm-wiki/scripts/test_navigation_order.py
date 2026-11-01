"""Offline ordering tests for navigation.py (I3): main card, system map, EMPTY.

Runs against a temp vault/registry/feed; the live Hub is never touched.
"""

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import navigation as nav  # noqa: E402

PAGE = """---
title: "{t}"
project_slug: {slug}
status: {status}
last_verified: 2026-10-14T00:00:00Z
summary: "{s}"
---

body
"""

START = """---
title: "Start here: project map"
status: reviewed
last_verified: 2026-10-14T00:00:00Z
summary: "generated view"
---

body
"""


class NavOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        wiki = root / "vault" / "wiki"
        (wiki / "projects").mkdir(parents=True)
        (wiki / "history").mkdir(parents=True)
        (wiki / "START.md").write_text(START, encoding="utf-8")
        (wiki / "projects" / "alpha.md").write_text(
            PAGE.format(t="Alpha", slug="alpha", status="reviewed", s="projekt alpha"), encoding="utf-8")
        (wiki / "projects" / "alpha-cont-2.md").write_text(
            PAGE.format(t="Alpha 2", slug="alpha", status="draft", s="kontynuacja"), encoding="utf-8")
        (wiki / "history" / "alpha-2026.md").write_text(
            PAGE.format(t="Alpha historia", slug="alpha", status="reviewed", s="historia"), encoding="utf-8")
        (wiki / "projects" / "beta.md").write_text(
            PAGE.format(t="Beta KSeF", slug="beta", status="reviewed", s="ksef temat"), encoding="utf-8")
        (wiki / "history" / "beta-feed.md").write_text(
            PAGE.format(t="Beta feed", slug="beta", status="reviewed", s="historia bety"), encoding="utf-8")
        reg = root / "registry-v2.json"
        reg.write_text(json.dumps({
            "version": 2,
            "organizations": [{"id": "example-org"}],
            "topics": [{"id": "ksef", "name": "KSeF", "aliases": ["ksef"]}],
            "projects": {
                "alpha": {"aliases": [], "org_links": []},
                "beta": {"aliases": [], "org_links": []},
                "ghost": {"aliases": [], "org_links": []},
            },
        }), encoding="utf-8")
        feed = root / "feed.json"
        feed.write_text(json.dumps({
            "topic": "ksef",
            "curated_records": [{
                "source_ref": "wiki/history/beta-feed.md",
                "project_slug": "beta",
                "title": "Beta feed",
                "status": "reviewed",
                "trust": "trusted",
                "observed_at": "2026-10-01",
            }],
        }), encoding="utf-8")
        self._saved = (nav.REGISTRY_LIVE, nav.REGISTRY_SEED, nav.TOPIC_FEEDS, nav.VAULT)
        nav.REGISTRY_LIVE = reg
        nav.TOPIC_FEEDS = feed
        nav.VAULT = root / "vault"

    def tearDown(self):
        nav.REGISTRY_LIVE, nav.REGISTRY_SEED, nav.TOPIC_FEEDS, nav.VAULT = self._saved
        self.tmp.cleanup()

    def run_nav(self, *argv):
        out = StringIO()
        with redirect_stdout(out):
            rc = nav.main(list(argv))
        return rc, json.loads(out.getvalue())

    def test_project_main_card_and_map_first(self):
        rc, d = self.run_nav("--project", "alpha", "--limit", "50")
        self.assertEqual(rc, 0)
        self.assertEqual(d["result"], "FOUND")
        refs = [r["page_ref"] for r in d["records"]]
        self.assertEqual(refs[0], "wiki/projects/alpha.md")
        self.assertEqual(refs[1], "wiki/START.md")
        self.assertTrue(d["records"][0].get("main_card"))
        self.assertTrue(d["records"][1].get("system_map"))
        self.assertEqual(d["total"], 4)

    def test_empty_stays_empty_without_map(self):
        rc, d = self.run_nav("--project", "ghost", "--limit", "50")
        self.assertEqual(rc, 0)
        self.assertEqual(d["result"], "EMPTY")
        self.assertEqual(d["total"], 0)
        self.assertEqual(d["records"], [])

    def test_topic_promotes_main_cards(self):
        rc, d = self.run_nav("--topic", "ksef", "--limit", "50")
        self.assertEqual(rc, 0)
        refs = [r["page_ref"] for r in d["records"]]
        self.assertEqual(refs[0], "wiki/projects/beta.md")
        self.assertEqual(refs[1], "wiki/START.md")
        self.assertTrue(d["records"][0].get("main_card"))
        self.assertEqual(d["total"], 3)


if __name__ == "__main__":
    unittest.main()
