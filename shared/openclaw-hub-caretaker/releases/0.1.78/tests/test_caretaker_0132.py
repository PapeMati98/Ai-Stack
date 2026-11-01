"""Regressions for completed turns and bounded issue reporting in 0.1.32."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import maintenance
from caretaker.connectors import ConnectorCollection, ConnectorIssue, SourceSpec, collect_sources
from .test_connectors import TASK_TMP, _codex_rows, _write_jsonl
from .test_maintenance import _maintenance_fixture


class CompletedTurnTests(unittest.TestCase):
    def setUp(self):
        TASK_TMP.mkdir(parents=True, exist_ok=True)

    def collect(self, tool, rows, *, max_records=2048):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            path = _write_jsonl(Path(directory) / 'session.jsonl', rows)
            return collect_sources([SourceSpec(tool, path, max_records=max_records)])

    def test_codex_item_and_aborted_rows_do_not_close_turn_or_raise_issue(self):
        rows = _codex_rows()
        rows[-1:-1] = [
            {'type': 'event_msg', 'timestamp': '2026-09-17T12:00:03Z',
             'payload': {'type': 'item_completed', 'turn_id': 'turn-1',
                         'item': {'type': 'Reasoning', 'id': 'reason-1'},
                         'status': 'completed'}},
            {'type': 'event_msg', 'timestamp': '2026-09-17T12:00:03Z',
             'payload': {'type': 'turn_aborted', 'turn_id': 'turn-1',
                         'status': 'completed'}},
        ]
        result = self.collect('codex', rows)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].cursor, 'turn-1')
        self.assertEqual(result.issues, ())
        self.assertNotIn('item.completed', result.events[0].completion_kind)
        self.assertEqual(self.collect('codex', rows[:-1]).events, ())
        self.assertEqual(self.collect('codex', rows[:-1]).issues, ())

    def test_codex_main_missing_link_warns_and_subagent_missing_link_is_info(self):
        rows = [row for row in _codex_rows() if not (
            row['type'] == 'response_item' and row['payload'].get('role') == 'user')]
        main = self.collect('codex', rows)
        self.assertEqual([(i.status, i.reason) for i in main.issues],
                         [('skipped', 'missing_linked_user')])
        self.assertEqual(main.events, ())
        for meta in ({'thread_source': 'subagent'}, {'parent_thread_id': 'parent-1'}):
            subagent_rows = json.loads(json.dumps(rows))
            subagent_rows[0]['payload'].update(meta)
            result = self.collect('codex', subagent_rows)
            self.assertEqual([(i.status, i.reason) for i in result.issues],
                             [('info', 'missing_linked_user')])
            self.assertEqual(result.events, ())

    def test_claude_tool_rows_are_silent_and_completed_turn_is_preserved(self):
        user = {'type': 'user', 'sessionId': 'claude-1', 'uuid': 'user-1',
                'timestamp': '2026-09-17T12:00:00Z',
                'message': {'role': 'user', 'content': 'Review synthetic fixture.'}}
        tool = {'type': 'assistant', 'sessionId': 'claude-1', 'uuid': 'tool-1',
                'status': 'completed',
                'timestamp': '2026-09-17T12:00:01Z',
                'message': {'role': 'assistant', 'stop_reason': 'tool_use',
                            'content': [{'type': 'tool_use', 'name': 'read'}]}}
        done = {'type': 'assistant', 'sessionId': 'claude-1', 'uuid': 'done-1',
                'timestamp': '2026-09-17T12:00:02Z',
                'message': {'role': 'assistant', 'stop_reason': 'end_turn',
                            'content': 'Completed safely.'}}
        result = self.collect('claude', [user, tool, tool, done])
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.issues, ())
        self.assertEqual(result.events[0].excerpt, 'Completed safely.')
        bounded = self.collect('claude', [user, tool, tool, done], max_records=1)
        self.assertEqual(len(bounded.events), 1)
        self.assertFalse(any('tool_use' in issue.reason for issue in bounded.issues))

    def test_claude_repeated_incomplete_rows_report_once_per_turn(self):
        user = {'type': 'user', 'sessionId': 'claude-1', 'uuid': 'user-1',
                'timestamp': '2026-09-17T12:00:00Z',
                'message': {'role': 'user', 'content': 'Review.'}}
        incomplete = {'type': 'assistant', 'sessionId': 'claude-1', 'uuid': 'a-1',
                      'timestamp': '2026-09-17T12:00:01Z',
                      'message': {'role': 'assistant', 'content': 'Working.'}}
        result = self.collect('claude', [user, incomplete, incomplete,
                                        dict(user, uuid='user-2'), incomplete])
        self.assertEqual(result.events, ())
        self.assertEqual([i.reason for i in result.issues],
                         ['missing_terminal_marker', 'missing_terminal_marker'])

    def test_claude_repeated_completed_rows_without_answer_report_once(self):
        user = {'type': 'user', 'sessionId': 'claude-1', 'uuid': 'user-1',
                'timestamp': '2026-09-17T12:00:00Z',
                'message': {'role': 'user', 'content': 'Review.'}}
        empty = {'type': 'assistant', 'sessionId': 'claude-1', 'uuid': 'a-1',
                 'timestamp': '2026-09-17T12:00:01Z',
                 'message': {'role': 'assistant', 'stop_reason': 'end_turn',
                             'content': ''}}
        result = self.collect('claude', [user, empty, empty])
        self.assertEqual(result.events, ())
        self.assertEqual([i.reason for i in result.issues], ['missing_final_answer'])


class SourceIssueReportTests(unittest.TestCase):
    def setUp(self):
        TASK_TMP.mkdir(parents=True, exist_ok=True)

    def tick_with(self, issues):
        with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
            control, _, _ = _maintenance_fixture(Path(directory))
            control['max_projects_per_tick'] = 0
            state = Path(control['state_dir'])
            state.mkdir()
            with patch('maintenance.collect_sources',
                       return_value=ConnectorCollection(issues=tuple(issues))):
                return maintenance._locked_tick(control, state)

    def test_summary_counts_every_issue_after_detail_is_bounded(self):
        issues = [ConnectorIssue('source-a', 'codex', 'skipped', 'line_too_large', n)
                  for n in range(120)]
        issues += [ConnectorIssue('source-b', 'codex', 'skipped', reason)
                   for reason in ('coverage_limited', 'byte_budget_exhausted',
                                  'answer_needs_full_source', 'missing_linked_user')]
        report = self.tick_with(issues)
        self.assertEqual(report['status'], 'DEGRADED')
        self.assertEqual(report['source_issue_count'], 124)
        self.assertEqual(next(i for i in report['source_issues']
                              if i['reason'] == 'line_too_large')['count'], 120)
        self.assertEqual(next(i for i in report['source_issue_summary']
                              if i['reason'] == 'line_too_large')['count'], 120)

    def test_info_only_does_not_degrade_and_summary_keeps_all_groups(self):
        issues = [ConnectorIssue('source-' + str(n), 'codex', 'info',
                                 'missing_linked_user') for n in range(110)]
        report = self.tick_with(issues)
        self.assertEqual(report['status'], 'OK')
        self.assertEqual(report['source_issue_count'], 110)
        self.assertEqual(len(report['source_issues']), 100)
        self.assertEqual(report['source_issue_summary'], [
            {'tool': 'codex', 'reason': 'missing_linked_user',
             'status': 'info', 'count': 110}])


if __name__ == '__main__':
    unittest.main()
