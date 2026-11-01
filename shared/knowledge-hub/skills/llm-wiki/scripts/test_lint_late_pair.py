"""Lint must not blame a complete source/proposal pair that another session wrote while
the scan was already running (RSI HD-20261013-d777e3 and the same-family hub tickets).

Disposable vaults only. The race is injected deterministically: the concurrent pair is
written by a hook at a phase boundary of lint() instead of by a real second process.
"""
import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import knowledge as k


class LintLatePairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-lint-late-pair-')
        self.home = Path(self.temp.name)
        self.vault = self.home / 'vault'
        self.vault.mkdir()
        k.write_json(self.vault / 'knowledge-vault.json',
                     {'curator_machine': 'FIXTURE', 'curation_scope': 'designated-machine-only'})
        self.home_patch = mock.patch.object(Path, 'home', return_value=self.home)
        self.home_patch.start()
        self.counter = 0

    def tearDown(self):
        self.home_patch.stop()
        self.temp.cleanup()

    def write_pair(self, title, agent='other-agent'):
        """One complete stage-source + new-proposal pair, exactly as record-session writes it."""
        self.counter += 1
        note = self.home / f'note-{self.counter}.md'
        note.write_text(f'## Result\n{title}: synthetic session note {self.counter}.\n', encoding='utf-8')
        staged = k.stage_source(self.vault, argparse.Namespace(
            agent=agent, title=title, origin='local-test', path=str(note),
            classification='internal', local_only=False))
        proposal = k.new_proposal(self.vault, argparse.Namespace(
            agent=agent, title=title, classification='internal', source_id=[staged['source_id']],
            target_page=[], defer_checkpoint=True),
            completion_body='## Intent\n\nRecord one synthetic session.\n\n## Security review\n\n- Redacted note.\n')
        return staged['source_id'], str(proposal['proposal_id'])

    def unknown_reference_errors(self, result):
        return [e for e in result['errors'] if e.startswith('Unknown ')]

    def test_pair_written_after_the_source_scan_is_not_an_error(self):
        self.write_pair('Earlier pair')
        self.assertEqual(k.lint(self.vault)['error_count'], 0)
        original = k.source_records
        fired = []

        def racing_source_records(vault):
            records = original(vault)
            if not fired:
                fired.append(True)
                self.write_pair('Concurrent pair')  # lands after sources were listed, before proposals/events
            return records

        with mock.patch.object(k, 'source_records', racing_source_records):
            result = k.lint(self.vault)
        self.assertTrue(fired, 'the race hook never fired; the test did not exercise lint')
        self.assertEqual(self.unknown_reference_errors(result), [])
        self.assertEqual(result['error_count'], 0, result['errors'])
        self.assertEqual(k.lint(self.vault)['error_count'], 0)

    def test_pair_written_after_the_proposal_listing_is_not_an_error(self):
        self.write_pair('Earlier pair')
        self.assertEqual(k.lint(self.vault)['error_count'], 0)
        original = k.fm_value
        fired = []

        def racing_fm_value(content, field):
            # No wiki pages exist in this vault, so the first frontmatter read inside lint() is the
            # first proposal of the proposal phase: proposals are already listed, events are not.
            if not fired:
                fired.append(True)
                self.write_pair('Concurrent pair')
            return original(content, field)

        with mock.patch.object(k, 'fm_value', racing_fm_value):
            result = k.lint(self.vault)
        self.assertTrue(fired, 'the race hook never fired; the test did not exercise lint')
        self.assertEqual(self.unknown_reference_errors(result), [])
        self.assertEqual(result['error_count'], 0, result['errors'])
        self.assertEqual(k.lint(self.vault)['error_count'], 0)

    def test_genuinely_dangling_references_still_fail(self):
        source_id, proposal_id = self.write_pair('Earlier pair')
        missing_source = 'src-20260101T000000000Z-missing-00000000'
        proposal_path = next((self.vault / 'proposals').rglob('*.md'))
        content = k.read_text(proposal_path)
        proposal_path.write_text(content.replace(f'  - {source_id}', f'  - {missing_source}'), encoding='utf-8')
        k.write_event(self.vault, 'other-agent', 'new-proposal', 'Dangling proposal reference',
                      source_ids=[source_id], proposal_ids=['prop-20260101T000000000Z-00000000'])
        result = k.lint(self.vault)
        messages = result['errors']
        self.assertTrue(any(m.startswith(f"Unknown proposal source ID '{missing_source}'") for m in messages), messages)
        self.assertTrue(any(m.startswith("Unknown event proposal ID 'prop-20260101T000000000Z-00000000'") for m in messages), messages)

    def test_late_source_with_unreadable_metadata_stays_unknown(self):
        self.write_pair('Earlier pair')
        original = k.source_records
        fired = []

        def racing_source_records(vault):
            records = original(vault)
            if not fired:
                fired.append(True)
                source_id, _ = self.write_pair('Concurrent pair')
                stamp = source_id[4:12]
                metadata = vault / 'raw' / 'sources' / stamp[:4] / stamp[4:6] / source_id / 'source.json'
                metadata.write_text('{"source_id": "' + source_id + '", "truncated', encoding='utf-8')
            return records

        with mock.patch.object(k, 'source_records', racing_source_records):
            result = k.lint(self.vault)
        self.assertTrue(fired)
        unknown = self.unknown_reference_errors(result)
        self.assertEqual(len(unknown), 3, result['errors'])  # proposal + stage-source event + new-proposal event
        self.assertFalse(any(m.startswith('Invalid source metadata JSON') for m in result['errors']), result['errors'])


if __name__ == '__main__':
    unittest.main()
