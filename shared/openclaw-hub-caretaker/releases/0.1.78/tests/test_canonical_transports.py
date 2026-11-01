"""Contract tests for the real subprocess adapters.

Every runner is injected: these tests validate argv and receipt handling
without invoking the Hub, Office, OpenClaw, or any model.
"""

import json
import tempfile
import unittest
from pathlib import Path

from caretaker.hub import (CanonicalKnowledgeGateway,
                           KnowledgeTransportError)
from caretaker.office import (CanonicalOfficeArgvTransport, OfficeConnector,
                              OfficeDedupStore, OfficeIdentity,
                              OfficeSendRequest, OfficeSendResult,
                              RateLimiter, FakeOfficeTransport)
from caretaker.store import Store


TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"


class CanonicalKnowledgeGatewayTests(unittest.TestCase):

    def test_commands_use_documented_flags_and_receipts(self):
        calls = []

        def runner(*, argv, cwd, timeout):
            calls.append(list(argv))
            action = next(item for item in argv if item in {
                "status", "lint", "begin-task", "verify-context",
                "inventory", "record-handoff",
            })
            payloads = {
                "status": {"action": "status", "dirty_tree_state": "clean",
                            "dirty_tree_write_blocked": False},
                "lint": {"action": "lint", "error_count": 0,
                          "warning_count": 0},
                "begin-task": {"action": "begin-task", "status": "COMPLETE",
                                "receipt_path": "C:/receipts/context.json"},
                "verify-context": {"action": "verify-context", "valid": True,
                                    "fresh": True},
                "inventory": {"action": "inventory", "records": [{
                    "kind": "proposal", "id": "prop-1", "title": "Existing",
                    "status": "proposed", "path": str(Path("vault/p.md").resolve()),
                }]},
                "record-handoff": {
                    "action": "record-handoff", "receipt_status": "COMPLETE",
                    "source_id": "src-1", "proposal_id": "prop-1",
                    "event_ids": ["evt-1"], "checkpoint": {},
                },
            }
            return 0, json.dumps(payloads[action]), ""

        gateway = CanonicalKnowledgeGateway(
            python_executable="python", script="knowledge.py",
            vault="vault", receipt_root="receipts", run_callable=runner,
        )
        self.assertEqual(gateway.health().status, "pass")
        self.assertEqual(gateway.lint().status, "pass")
        begin = gateway.begin_task(project_slug="project", agent="agent",
                                   session_id="session", require_context=True,
                                   limit=5)
        self.assertEqual(begin.receipt_path, "C:/receipts/context.json")
        self.assertEqual(gateway.verify_context(
            context_receipt=begin.receipt_path, project_slug="project",
            agent="agent", session_id="session").status, "ok")
        inventory = gateway.inventory(project_slug="project", query="x",
                                      limit=5)
        self.assertEqual(inventory.items[0].receipt_id, "prop-1")
        result = gateway.record_handoff(
            project_slug="project", path="C:/state/note.md", title="Title",
            agent="agent", state_summary="Full evidence.\n" * 60, source_refs=["src"],
            target_page="wiki/projects/project.md", classification="internal",
            context_receipt=begin.receipt_path, session_id="session",
        )
        self.assertEqual(result.status, "recorded")
        self.assertEqual(result.receipt_id, "prop-1")
        self.assertEqual(result.source_id, "src-1")
        self.assertEqual(result.proposal_id, "prop-1")
        self.assertEqual(result.event_ids, ("evt-1",))

        begin_args = next(argv for argv in calls if "begin-task" in argv)
        self.assertIn("--receipt-root", begin_args)
        self.assertIn("--require-context", begin_args)
        self.assertIn("--project-slug", begin_args)
        handoff_args = next(argv for argv in calls
                            if "record-handoff" in argv)
        self.assertIn("--path", handoff_args)
        self.assertNotIn("--source-refs", handoff_args)
        summary = handoff_args[handoff_args.index("--state-summary") + 1]
        self.assertLessEqual(len(summary), 500)
        self.assertNotIn("\n", summary)
        self.assertIn("attached note", summary)
        self.assertEqual(inventory.items[0].page, "p.md")

    def test_inventory_retains_reviewed_wiki_and_rejects_escaped_paths(self):
        offsets = []
        def runner(*, argv, cwd, timeout):
            offset = int(argv[argv.index('--offset') + 1]); offsets.append(offset)
            rows = [] if not offset else [
                {'path':str(Path('vault/wiki/projects/p.md').resolve()),'title':'P','kind':'wiki'},
                {'path':str(Path('outside.md').resolve()),'title':'Outside','kind':'wiki'}]
            return 0, json.dumps({'total':99,'has_more':not bool(offset),'records':rows}), ''
        gateway = CanonicalKnowledgeGateway(python_executable='python',script='knowledge.py',vault='vault',run_callable=runner)
        result = gateway.inventory(project_slug='p',query=None,limit=8)
        self.assertEqual(offsets[:2],[0,91])
        self.assertEqual([i.page for i in result.items],['wiki/projects/p.md'])

    def test_inventory_selects_main_latest_and_recent_handoffs_in_both_orders(self):
        main = {'path': 'wiki/projects/p.md', 'title': 'Main', 'kind': 'wiki'}
        cont2 = {'path': 'wiki/projects/p-cont-2.md', 'title': 'Older', 'kind': 'wiki'}
        cont4 = {'path': 'wiki/projects/p-cont-4.md', 'title': 'Latest', 'kind': 'wiki'}
        proposals = [
            {'path': f'proposals/agent/{i}.md', 'title': f'Proposal {i}',
             'kind': 'proposal', 'created_at': f'2026-09-{i:02d}T00:00:00Z'}
            for i in range(1, 7)
        ]
        ledger = {'path': 'history/openclaw-old.md', 'title': 'Ledger', 'kind': 'wiki'}
        for rows in (proposals + [ledger, cont2, cont4, main],
                     [main, cont4, cont2, ledger] + proposals):
            with self.subTest(first=rows[0]['path']):
                offsets = []
                def runner(*, argv, cwd, timeout):
                    offset = int(argv[argv.index('--offset') + 1])
                    size = int(argv[argv.index('--limit') + 1])
                    offsets.append(offset)
                    return 0, json.dumps({
                        'total': len(rows), 'has_more': offset + size < len(rows),
                        'registry': {'main_page': 'wiki/projects/p.md',
                                     'latest_page': 'wiki/projects/p-cont-4.md'},
                        'pages_newest_first': ['wiki/projects/p-cont-4.md',
                                               'wiki/projects/p.md'],
                        'records': rows[offset:offset + size],
                    }), ''
                gateway = CanonicalKnowledgeGateway(
                    python_executable='python', script='knowledge.py',
                    vault='vault', run_callable=runner)
                result = gateway.inventory(project_slug='p', query=None, limit=4)
                self.assertEqual([item.page for item in result.items], [
                    'wiki/projects/p.md', 'wiki/projects/p-cont-4.md',
                    'proposals/agent/6.md', 'proposals/agent/5.md'])
                self.assertEqual(offsets[0], 0)
                self.assertGreater(len(offsets), 1)

    def test_inventory_uses_registry_match_for_nonstandard_main_page(self):
        rows = [
            {'path': 'proposals/a/old.md', 'title': 'Old', 'kind': 'proposal'},
            {'path': 'wiki/projects/custom-overview-cont-3.md', 'title': 'Latest', 'kind': 'wiki'},
            {'path': 'wiki/projects/custom-overview.md', 'title': 'Main', 'kind': 'wiki'},
        ]
        def runner(*, argv, cwd, timeout):
            offset = int(argv[argv.index('--offset') + 1])
            return 0, json.dumps({
                'records': rows[offset:offset + 2], 'total': len(rows),
                'has_more': offset == 0,
                'registry_matches': [{'project_slug': 'project',
                                      'main_page': 'wiki/projects/custom-overview.md',
                                      'latest_page': 'wiki/projects/custom-overview-cont-3.md'}],
            }), ''
        gateway = CanonicalKnowledgeGateway(
            python_executable='python', script='knowledge.py',
            vault='vault', run_callable=runner)
        result = gateway.inventory(project_slug='project', query=None, limit=2)
        self.assertEqual([item.page for item in result.items], [
            'wiki/projects/custom-overview.md',
            'wiki/projects/custom-overview-cont-3.md'])

    def test_missing_or_incomplete_receipt_never_acknowledges(self):
        def missing_runner(*, argv, cwd, timeout):
            if argv[2] == "begin-task":
                return 0, json.dumps({"action": "begin-task",
                                      "status": "COMPLETE"}), ""
            return 0, "{}", ""

        gateway = CanonicalKnowledgeGateway(
            python_executable="python", script="knowledge.py", vault="vault",
            run_callable=missing_runner,
        )
        with self.assertRaises(KnowledgeTransportError):
            gateway.begin_task(project_slug="p", agent="a", session_id="s",
                               require_context=False, limit=1)

        def incomplete_runner(*, argv, cwd, timeout):
            return 0, json.dumps({"action": "record-handoff",
                                  "receipt_status": "COMPLETE",
                                  "source_id": "src-1",
                                  "proposal_id": "prop-1",
                                  "event_ids": []}), ""

        gateway = CanonicalKnowledgeGateway(
            python_executable="python", script="knowledge.py", vault="vault",
            run_callable=incomplete_runner,
        )
        result = gateway.record_handoff(
            project_slug="p", path="note.md", title="t", agent="a",
            state_summary="s", source_refs=[], target_page="p/page",
            classification="internal", context_receipt=None, session_id="sid",
        )
        self.assertEqual(result.status, "offline")
        self.assertEqual(result.reason, "incomplete_handoff_receipt")


class CanonicalOfficeTransportTests(unittest.TestCase):

    def test_wrapper_argv_and_canonical_sent_shape(self):
        calls = []

        def runner(*, executable, argv, timeout):
            calls.append((executable, list(argv)))
            return 0, json.dumps({"status": "ok", "code": "sent",
                                  "id": "a" * 32}), ""

        transport = CanonicalOfficeArgvTransport(
            executable="office.cmd", home="coord", harness="openclaw",
            session_id="sid", run_callable=runner,
        )
        identity = OfficeIdentity(harness="openclaw", host="host",
                                  session_id="sid")
        result = transport.send(
            OfficeSendRequest(dedup_id="d", body="hello", kind="note",
                              to="root", reply_to="parent"),
            identity=identity,
        )
        self.assertEqual(result.status, "sent")
        self.assertEqual(result.message_id, "a" * 32)
        executable, argv = calls[0]
        self.assertEqual(executable, "office.cmd")
        self.assertEqual(argv[:2], ["--home", "coord"])
        self.assertIn("--agent", argv)
        self.assertIn("openclaw-host-sid", argv)
        self.assertIn("--reply-to", argv)

    def test_direct_python_script_pins_utf8_before_cli_args(self):
        calls = []

        def runner(*, executable, argv, timeout):
            calls.append((executable, list(argv)))
            return 0, json.dumps({"status": "ok", "code": "sent",
                                  "id": "b" * 32}), ""

        transport = CanonicalOfficeArgvTransport(
            executable="office.py", python_executable="python",
            script="office.py", home="coord", harness="h",
            session_id="sid", run_callable=runner,
        )
        result = transport.send(
            OfficeSendRequest(dedup_id="d", body="żółć", kind="note",
                              channel="#updates"),
            identity=OfficeIdentity(harness="h", host="host",
                                    session_id="sid"),
        )
        self.assertEqual(result.status, "sent")
        self.assertEqual(calls[0][0], "python")
        self.assertEqual(calls[0][1][:3], ["-X", "utf8", "office.py"])

    def test_persistent_scope_deduplicates_across_connector_instances(self):
        TASK_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            dedup_path = str(Path(d) / "office-dedup.json")
            identity = OfficeIdentity(harness="h", host="host",
                                      session_id="sid")
            req = OfficeSendRequest(dedup_id="d", body="hello", kind="note",
                                    to="root")
            first_transport = FakeOfficeTransport(responses=[
                OfficeSendResult(status="sent", message_id="m1"),
            ])
            first = OfficeConnector(
                identity=identity, transport=first_transport,
                dedup=OfficeDedupStore(path=dedup_path),
                rate=RateLimiter(max_per_minute=5,
                                 path=str(Path(d) / "office-rate.json")),
            )
            self.assertEqual(first.publish(req).status, "sent")

            second_transport = FakeOfficeTransport(responses=[])
            second = OfficeConnector(
                identity=identity, transport=second_transport,
                dedup=OfficeDedupStore(path=dedup_path),
                rate=RateLimiter(max_per_minute=5,
                                 path=str(Path(d) / "office-rate.json")),
            )
            self.assertEqual(second.publish(req).status, "deduplicated")
            self.assertEqual(second_transport.calls, [])

    def test_receipt_metadata_survives_store_reopen(self):
        TASK_TMP.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as d:
            path = str(Path(d) / "store.sqlite")
            store = Store(path)
            store.outbox_put(
                dedup_id="handoff-1", payload_hash="hash-1",
                status="recorded", receipt_id="prop-1",
                recorded_at="2026-09-17T10:00:00Z",
                request={"receipt": {
                    "receipt_status": "COMPLETE", "source_id": "src-1",
                    "proposal_id": "prop-1", "event_ids": ["evt-1"],
                }},
            )
            store.close()
            reopened = Store(path)
            try:
                row = reopened.outbox_get("handoff-1")
                self.assertIsNotNone(row)
                self.assertEqual(row.receipt_id, "prop-1")
                self.assertEqual(reopened.outbox_request("handoff-1")[
                    "receipt"]["event_ids"], ["evt-1"])
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
